from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from procure_radar.db import connect
from procure_radar.history import history_status, ingest_region_history


def _protocol(purchase_number: str, source_id: str) -> dict[str, Any]:
    return {
        "doc_type": "epProtocolEF2020Final",
        "published_at": "2026-08-20T12:00:00",
        "source": {
            "id": source_id,
            "versionNumber": "1",
            "commonInfo": {"purchaseNumber": purchase_number},
            "protocolInfo": {"applicationsInfo": {"applicationInfo": []}},
        },
    }


def _row(number: str, day: str, *, candidate: bool = False) -> dict[str, Any]:
    docs = [{"doc_type": "epNotificationEF2020"}]
    if candidate:
        docs.append({"doc_type": "epProtocolEF2020Final"})
    return {
        "purchase_number": number,
        "purchase_type": "epNotificationEF2020",
        "region": 2,
        "stage": 2,
        "published_at": f"{day}T10:00:00",
        "max_price": 100000.0,
        "docs": docs,
    }


@dataclass
class FakeClient:
    rows: list[dict[str, Any]]
    details: dict[str, dict[str, Any]] = field(default_factory=dict)
    base_url: str = "https://example.test"
    detail_calls: list[str] = field(default_factory=list)
    interrupt_once_on_purchase: str | None = None

    def get_purchases(
        self,
        *,
        limit: int,
        skip: int,
        extra: dict[str, str] | None = None,
        pagination_param: str = "skip",
    ):
        return self.rows[skip : skip + limit]

    def get_purchase(self, purchase_number: str):
        self.detail_calls.append(purchase_number)
        if self.interrupt_once_on_purchase == purchase_number:
            self.interrupt_once_on_purchase = None
            raise KeyboardInterrupt
        return self.details[purchase_number]

    def get_purchase_protocols(self, purchase_number: str):
        return []


def test_history_filters_dates_and_completes_at_since(tmp_path: Path):
    rows = [
        _row("1", "2026-08-20"),
        _row("2", "2026-08-19"),
        _row("3", "2026-08-10"),
        _row("4", "2026-07-31"),
    ]
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_region_history(
            conn,
            FakeClient(rows),
            region_code=2,
            since="2026-08-10",
            until="2026-08-19",
            limit=2,
            pages=10,
            fetch_details=False,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
        count = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
        checkpoints = history_status(conn, region_code=2)
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["stop_reason"] == "index_exhausted"
    assert result["rows_seen"] == 4
    assert result["rows_in_window"] == 2
    assert result["rows_newer_than_window"] == 1
    assert count == 2
    assert checkpoints[0]["completed"] == 1
    assert checkpoints[0]["next_skip"] == 4


def test_history_detail_budget_resumes_before_unprocessed_candidate(tmp_path: Path):
    numbers = ["100", "200", "300"]
    rows = [
        _row(numbers[0], "2026-08-19", candidate=True),
        _row(numbers[1], "2026-08-18", candidate=True),
        _row(numbers[2], "2026-08-17", candidate=True),
        _row("400", "2026-07-31"),
    ]
    details = {
        number: {
            **_row(number, day, candidate=True),
            "docs": [_protocol(number, f"p-{number}")],
        }
        for number, day in zip(numbers, ["2026-08-19", "2026-08-18", "2026-08-17"])
    }
    client = FakeClient(rows=rows, details=details)
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        first = ingest_region_history(
            conn,
            client,
            region_code=2,
            since="2026-08-01",
            until="2026-08-20",
            limit=4,
            pages=10,
            max_details=1,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
        state_after_first = history_status(conn, region_code=2)[0]
        second = ingest_region_history(
            conn,
            client,
            region_code=2,
            since="2026-08-01",
            until="2026-08-20",
            limit=4,
            pages=10,
            max_details=10,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
        detail_rows = conn.execute(
            "SELECT COUNT(*) FROM purchase_detail_fetches WHERE status='ok'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert first["status"] == "paused"
    assert first["stop_reason"] == "detail_budget"
    assert state_after_first["next_skip"] == 1
    assert second["status"] == "complete"
    assert second["stop_reason"] == "index_exhausted"
    assert client.detail_calls == numbers
    assert detail_rows == 3


def test_history_tolerates_non_descending_remote_order(tmp_path: Path):
    rows = [
        _row("1", "2026-08-06"),
        _row("2", "2026-08-07"),
        _row("3", "2026-07-31"),
        _row("4", "2026-08-05"),
    ]
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_region_history(
            conn,
            FakeClient(rows),
            region_code=2,
            since="2026-08-01",
            limit=2,
            pages=10,
            fetch_details=False,
            request_delay=0,
            emit=lambda _: None,
        )
        numbers = [
            row[0]
            for row in conn.execute(
                "SELECT purchase_number FROM purchases ORDER BY purchase_number"
            ).fetchall()
        ]
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["stop_reason"] == "index_exhausted"
    assert result["rows_older_than_window"] == 1
    assert result["out_of_order_rows"] >= 1
    assert numbers == ["1", "2", "4"]


def test_history_ctrl_c_checkpoints_current_row_and_resumes(tmp_path: Path):
    numbers = ["100", "200", "300"]
    rows = [
        _row(numbers[0], "2026-08-19", candidate=True),
        _row(numbers[1], "2026-08-18", candidate=True),
        _row(numbers[2], "2026-08-17", candidate=True),
        _row("400", "2026-07-31"),
    ]
    details = {
        number: {
            **_row(number, day, candidate=True),
            "docs": [_protocol(number, f"p-{number}")],
        }
        for number, day in zip(numbers, ["2026-08-19", "2026-08-18", "2026-08-17"])
    }
    client = FakeClient(
        rows=rows,
        details=details,
        interrupt_once_on_purchase=numbers[1],
    )
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        first = ingest_region_history(
            conn,
            client,
            region_code=2,
            since="2026-08-01",
            until="2026-08-20",
            limit=4,
            pages=None,
            max_details=None,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
        checkpoint = history_status(conn, region_code=2)[0]
        second = ingest_region_history(
            conn,
            client,
            region_code=2,
            since="2026-08-01",
            until="2026-08-20",
            limit=4,
            pages=None,
            max_details=None,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
    finally:
        conn.close()

    assert first["status"] == "paused"
    assert first["stop_reason"] == "interrupted"
    assert first["next_skip"] == 1
    assert checkpoint["next_skip"] == 1
    assert second["status"] == "complete"
    assert second["stop_reason"] == "index_exhausted"
    assert client.detail_calls == ["100", "200", "200", "300"]


def test_history_rate_per_minute_converts_to_safe_post_request_delay(tmp_path: Path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("procure_radar.history.time.sleep", sleeps.append)
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_region_history(
            conn,
            FakeClient([_row("1", "2026-07-31")]),
            region_code=2,
            since="2026-08-01",
            limit=50,
            pages=None,
            fetch_details=False,
            rate_per_minute=9,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["request_delay_seconds"] == pytest.approx(60 / 9)
    assert sleeps == [pytest.approx(60 / 9)]


def test_continuous_history_periodically_rewinds_live_offsets(tmp_path: Path):
    rows = [
        _row("1", "2026-08-20"),
        _row("2", "2026-08-19"),
        _row("3", "2026-08-18"),
        _row("4", "2026-07-31"),
    ]
    messages: list[str] = []
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_region_history(
            conn,
            FakeClient(rows),
            region_code=2,
            since="2026-08-01",
            limit=1,
            pages=None,
            fetch_details=False,
            request_delay=0,
            resume_overlap_pages=1,
            live_overlap_every_pages=2,
            emit=messages.append,
        )
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["stop_reason"] == "index_exhausted"
    assert any("live-overlap rewind" in message for message in messages)


def _pagination_422(*, param: str = "skip", cap: int = 2) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/fz44/purchases")
    response = httpx.Response(
        422,
        request=request,
        json={
            "detail": [
                {
                    "type": "less_than_equal",
                    "loc": ["query", param],
                    "msg": f"Input should be less than or equal to {cap}",
                    "input": str(cap + 1),
                    "ctx": {"le": cap},
                }
            ]
        },
    )
    return httpx.HTTPStatusError("422", request=request, response=response)


@dataclass
class CappedPaginationClient(FakeClient):
    skip_cap: int = 2
    offset_supported: bool = True
    page_calls: list[tuple[str, int]] = field(default_factory=list)

    def get_purchases(
        self,
        *,
        limit: int,
        skip: int,
        extra: dict[str, str] | None = None,
        pagination_param: str = "skip",
    ):
        self.page_calls.append((pagination_param, skip))
        if pagination_param == "skip" and skip > self.skip_cap:
            raise _pagination_422(param="skip", cap=self.skip_cap)
        effective = skip if pagination_param == "skip" or self.offset_supported else 0
        return self.rows[effective : effective + limit]


def test_history_falls_back_to_verified_legacy_offset_after_skip_cap(tmp_path: Path):
    rows = [_row(str(i), f"2026-08-{20-i:02d}") for i in range(6)]
    client = CappedPaginationClient(rows=rows, skip_cap=2, offset_supported=True)
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_region_history(
            conn,
            client,
            region_code=2,
            since="2026-08-01",
            limit=1,
            pages=10,
            fetch_details=False,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["stop_reason"] == "index_exhausted"
    assert result["pagination_param"] == "offset"
    assert result["pagination_fallback"] == "legacy_offset"
    assert ("offset", 3) in client.page_calls
    assert result["rows_in_window"] == 6


def test_history_stops_cleanly_when_skip_cap_has_no_verified_fallback(tmp_path: Path):
    rows = [_row(str(i), f"2026-08-{20-i:02d}") for i in range(6)]
    client = CappedPaginationClient(rows=rows, skip_cap=2, offset_supported=False)
    messages: list[str] = []
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_region_history(
            conn,
            client,
            region_code=2,
            since="2026-08-01",
            limit=1,
            pages=10,
            fetch_details=False,
            request_delay=0,
            resume_overlap_pages=0,
            emit=messages.append,
        )
        checkpoint = history_status(conn, region_code=2)[0]
    finally:
        conn.close()

    assert result["status"] == "paused"
    assert result["stop_reason"] == "pagination_cap"
    assert result["pagination_fallback"] == "unavailable"
    assert result["next_skip"] == 3
    assert checkpoint["next_skip"] == 3
    assert checkpoint["completed"] == 0
    assert any("pagination_cap" in message for message in messages)


from datetime import datetime as _dt, timezone as _timezone
from procure_radar.history import ingest_region_history_sharded


@dataclass
class DateShardClient(FakeClient):
    shard_calls: list[tuple[str, str, int]] = field(default_factory=list)

    @staticmethod
    def _parse(value: str) -> _dt:
        return _dt.fromisoformat(value.replace("Z", "+00:00"))

    def get_purchases_with_total(
        self,
        *,
        limit: int,
        skip: int,
        extra: dict[str, str] | None = None,
    ):
        assert extra is not None
        after = self._parse(extra["published_after"])
        before = self._parse(extra["published_before"])
        selected: list[dict[str, Any]] = []
        for row in self.rows:
            published = self._parse(str(row["published_at"]) + "+00:00" if "+" not in str(row["published_at"]) and not str(row["published_at"]).endswith("Z") else str(row["published_at"]))
            if after <= published <= before:
                selected.append(row)
        selected.sort(key=lambda row: str(row["published_at"]), reverse=True)
        self.shard_calls.append((extra["published_after"], extra["published_before"], skip))
        return selected[skip : skip + limit], len(selected)


def test_sharded_history_uses_documented_published_ranges(tmp_path: Path):
    rows = [
        _row("1", "2026-08-20"),
        _row("2", "2026-08-20"),
        _row("3", "2026-08-19"),
        _row("4", "2026-08-18"),
    ]
    client = DateShardClient(rows=rows)
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_region_history_sharded(
            conn,
            client,
            region_code=2,
            since="2026-08-18",
            until="2026-08-20",
            limit=100,
            pages=None,
            fetch_details=False,
            request_delay=0,
            emit=lambda _: None,
        )
        numbers = {
            row[0]
            for row in conn.execute("SELECT purchase_number FROM purchases").fetchall()
        }
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["stop_reason"] == "history_window_complete"
    assert result["mode"] == "published_time_shards_v1"
    assert numbers == {"1", "2", "3", "4"}
    assert len(client.shard_calls) == 3
    assert all(call[2] == 0 for call in client.shard_calls)
    assert any("2026-08-20T00:00:00" in call[0] for call in client.shard_calls)


def test_sharded_history_resumes_inside_day(tmp_path: Path):
    rows = [
        _row("1", "2026-08-20", candidate=True),
        _row("2", "2026-08-20", candidate=True),
        _row("3", "2026-08-20", candidate=True),
    ]
    details = {
        row["purchase_number"]: {
            **row,
            "docs": [_protocol(row["purchase_number"], f"p-{row['purchase_number']}")],
        }
        for row in rows
    }
    client = DateShardClient(rows=rows, details=details)
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        first = ingest_region_history_sharded(
            conn,
            client,
            region_code=2,
            since="2026-08-20",
            until="2026-08-20",
            limit=100,
            pages=None,
            max_details=1,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
        second = ingest_region_history_sharded(
            conn,
            client,
            region_code=2,
            since="2026-08-20",
            until="2026-08-20",
            limit=100,
            pages=None,
            max_details=10,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
    finally:
        conn.close()

    assert first["status"] == "paused"
    assert first["stop_reason"] == "detail_budget"
    assert second["status"] == "complete"
    assert client.detail_calls == ["1", "2", "3"]


def _row_at(number: str, timestamp: str) -> dict[str, Any]:
    row = _row(number, timestamp[:10])
    row["published_at"] = timestamp
    return row


def test_sharded_history_splits_oversized_day_by_x_total(tmp_path: Path):
    # 1,101 rows exceed skip<=1000 plus a page size of 100, so the UTC day
    # must split into two sub-day shards before pagination begins.
    rows: list[dict[str, Any]] = []
    for i in range(1101):
        seconds = i * 70 % 86400
        hour, rem = divmod(seconds, 3600)
        minute, second = divmod(rem, 60)
        rows.append(_row_at(str(i), f"2026-08-20T{hour:02d}:{minute:02d}:{second:02d}"))
    client = DateShardClient(rows=rows)
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_region_history_sharded(
            conn,
            client,
            region_code=2,
            since="2026-08-20",
            until="2026-08-20",
            limit=100,
            pages=None,
            fetch_details=False,
            request_delay=0,
            emit=lambda _: None,
        )
        count = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["shard_splits"] >= 1
    assert count == 1101
