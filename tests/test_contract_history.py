from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from procure_radar.client import GosplanClient
from procure_radar.contract_history import (
    contract_history_status,
    ingest_contract_history_sharded,
)
from procure_radar.db import connect


def _contract(number: str, timestamp: str, *, region: int = 2) -> dict[str, Any]:
    return {
        "reg_num": f"reg-{number}",
        "purchase_number": f"purchase-{number}",
        "customer": "0200000000",
        "price": 100000.0,
        "currency_code": "RUB",
        "region": region,
        "stage": "E",
        "subject": f"contract {number}",
        "published_at": timestamp,
        "suppliers": ["0200000001"],
        "ktru": ["32.50.13.110-00000001"],
        "okpd2": [],
        "docs": [{"doc_type": "contract", "published_at": timestamp}],
    }


@dataclass
class ContractShardClient:
    rows: list[dict[str, Any]]
    base_url: str = "https://example.test"
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    @staticmethod
    def _parse(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def get_rows_with_total(
        self,
        endpoint: str,
        *,
        limit: int,
        skip: int,
        extra: dict[str, str] | None = None,
        pagination_param: str = "skip",
    ):
        assert endpoint == "/fz44/contracts"
        assert pagination_param == "skip"
        assert extra is not None
        after = self._parse(extra["published_after"])
        before = self._parse(extra["published_before"])
        assert extra["region"] == "2"
        selected = [
            row
            for row in self.rows
            if after <= self._parse(str(row["published_at"])) <= before
            and row["region"] == 2
        ]
        selected.sort(key=lambda row: str(row["published_at"]), reverse=True)
        self.calls.append((extra["published_after"], extra["published_before"], skip))
        return selected[skip : skip + limit], len(selected)


def test_contract_history_uses_time_shards_and_upserts(tmp_path: Path):
    client = ContractShardClient(
        rows=[
            _contract("1", "2026-08-20T10:00:00"),
            _contract("2", "2026-08-20T09:00:00"),
            _contract("3", "2026-08-19T10:00:00"),
        ]
    )
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_contract_history_sharded(
            conn,
            client,  # type: ignore[arg-type]
            region_code=2,
            since="2026-08-19",
            until="2026-08-20",
            limit=100,
            pages=None,
            request_delay=0,
            emit=lambda _: None,
        )
        count = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        suppliers = conn.execute("SELECT COUNT(*) FROM contract_suppliers").fetchone()[0]
        status = contract_history_status(conn, region_code=2)
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["stop_reason"] == "history_window_complete"
    assert result["contracts_ingested"] == 3
    assert count == 3
    assert suppliers == 3
    assert len(client.calls) == 2
    assert all(skip == 0 for _, _, skip in client.calls)
    assert status[0]["completed"] == 1
    assert status[0]["mode"] == "contracts_published_time_shards_v1"


def test_contract_history_resumes_inside_day_after_page_budget(tmp_path: Path):
    client = ContractShardClient(
        rows=[
            _contract("1", "2026-08-20T10:00:00"),
            _contract("2", "2026-08-20T09:00:00"),
            _contract("3", "2026-08-20T08:00:00"),
        ]
    )
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        first = ingest_contract_history_sharded(
            conn,
            client,  # type: ignore[arg-type]
            region_code=2,
            since="2026-08-20",
            until="2026-08-20",
            limit=1,
            pages=1,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
        second = ingest_contract_history_sharded(
            conn,
            client,  # type: ignore[arg-type]
            region_code=2,
            since="2026-08-20",
            until="2026-08-20",
            limit=1,
            pages=None,
            request_delay=0,
            resume_overlap_pages=0,
            emit=lambda _: None,
        )
        count = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    finally:
        conn.close()

    assert first["status"] == "paused"
    assert first["stop_reason"] == "page_budget"
    assert first["next_skip"] == 1
    assert second["status"] == "complete"
    assert count == 3


@dataclass
class OversizedContractClient(ContractShardClient):
    def get_rows_with_total(
        self,
        endpoint: str,
        *,
        limit: int,
        skip: int,
        extra: dict[str, str] | None = None,
        pagination_param: str = "skip",
    ):
        rows, total = super().get_rows_with_total(
            endpoint,
            limit=limit,
            skip=skip,
            extra=extra,
            pagination_param=pagination_param,
        )
        assert extra is not None
        after = self._parse(extra["published_after"])
        before = self._parse(extra["published_before"])
        # Force only the original full-day shard to split. Child intervals use
        # their real tiny totals and therefore finish immediately.
        if (before - after).total_seconds() > 86000:
            total = 1101
        return rows, total


def test_contract_history_splits_oversized_day_by_x_total(tmp_path: Path):
    client = OversizedContractClient(
        rows=[
            _contract("1", "2026-08-20T18:00:00"),
            _contract("2", "2026-08-20T06:00:00"),
        ]
    )
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = ingest_contract_history_sharded(
            conn,
            client,  # type: ignore[arg-type]
            region_code=2,
            since="2026-08-20",
            until="2026-08-20",
            limit=100,
            pages=None,
            request_delay=0,
            emit=lambda _: None,
        )
        count = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert result["shard_splits"] >= 1
    assert count == 2


def test_contract_history_rejects_rows_outside_published_shard(tmp_path: Path):
    class BadFilterClient(ContractShardClient):
        def get_rows_with_total(self, endpoint: str, **kwargs):
            extra = kwargs["extra"]
            self.calls.append((extra["published_after"], extra["published_before"], kwargs["skip"]))
            return [_contract("bad", "2026-08-19T10:00:00")], 1

    conn = connect(tmp_path / "radar.sqlite3")
    try:
        client = BadFilterClient(rows=[])
        try:
            ingest_contract_history_sharded(
                conn,
                client,  # type: ignore[arg-type]
                region_code=2,
                since="2026-08-20",
                until="2026-08-20",
                limit=100,
                pages=None,
                request_delay=0,
                emit=lambda _: None,
            )
        except RuntimeError as exc:
            assert "published_after/published_before" in str(exc)
        else:
            raise AssertionError("expected filter validation error")
    finally:
        conn.close()
