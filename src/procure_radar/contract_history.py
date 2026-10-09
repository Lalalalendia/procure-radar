from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta, timezone
import json
import sqlite3
import time
from typing import Any, Callable

import httpx

from .bulk import default_request_delay
from .client import GosplanClient
from .history import (
    HistoryWindow,
    _api_datetime,
    _checkpoint_row,
    _format_duration,
    _reset_sharded_checkpoint,
    _save_checkpoint,
    _save_time_shard,
    _skip_cap_from_error,
    _time_shard_row,
    checkpoint_key,
    history_status,
)
from .ingest import ingest_contract


_CONTRACT_HISTORY_MODE = "contracts_published_time_shards_v1"
_CONTRACTS_ENDPOINT = "/fz44/contracts"


def contract_history_status(
    conn: sqlite3.Connection,
    *,
    region_code: int | None = None,
) -> list[dict[str, Any]]:
    return [
        row
        for row in history_status(conn, region_code=region_code)
        if row.get("mode") == _CONTRACT_HISTORY_MODE
    ]


def _published_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _progress(*, started_at: float, requests: int) -> dict[str, Any]:
    elapsed = max(0.0, time.monotonic() - started_at)
    rpm = (requests / elapsed * 60.0) if elapsed >= 1.0 else None
    return {
        "elapsed_seconds": elapsed,
        "elapsed": _format_duration(elapsed),
        "api_requests": requests,
        "avg_requests_per_minute": rpm,
    }


def ingest_contract_history_sharded(
    conn: sqlite3.Connection,
    client: GosplanClient,
    *,
    region_code: int,
    since: str,
    until: str | None = None,
    limit: int = 100,
    pages: int | None = 100,
    extra: dict[str, str] | None = None,
    request_delay: float | None = None,
    rate_per_minute: float | None = None,
    resume_overlap_pages: int = 1,
    restart: bool = False,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Resumable historical backfill for ``/fz44/contracts``.

    The collector uses region + published_at time shards so every leaf interval
    starts at ``skip=0``. If ``x-total`` says an interval cannot be paged within
    the observed API skip cap, the interval is split recursively. Rows are
    idempotently upserted by ``reg_num`` via :func:`ingest_contract`.
    """
    if not (1 <= limit <= 100):
        raise ValueError("limit must be between 1 and 100")
    if pages is not None and pages <= 0:
        raise ValueError("pages must be positive or None")
    if resume_overlap_pages < 0:
        raise ValueError("resume_overlap_pages cannot be negative")
    if request_delay is not None and rate_per_minute is not None:
        raise ValueError("use either request_delay or rate_per_minute, not both")
    if rate_per_minute is not None and rate_per_minute <= 0:
        raise ValueError("rate_per_minute must be positive")

    window = HistoryWindow.parse(since, until)
    effective_until = window.until or date.today()

    params = dict(extra or {})
    for controlled in ("published_after", "published_before", "region"):
        if controlled in params:
            raise ValueError(
                f"{controlled} is managed by ingest-contract-history; "
                f"use --region/--since/--until instead"
            )
    params["region"] = str(region_code)

    checkpoint_params = dict(params)
    checkpoint_params["__history_mode"] = _CONTRACT_HISTORY_MODE
    checkpoint_params["__endpoint"] = _CONTRACTS_ENDPOINT
    key = checkpoint_key(region_code=region_code, window=window, params=checkpoint_params)

    if restart:
        _reset_sharded_checkpoint(conn, key)

    existing = _checkpoint_row(conn, key)
    if existing is not None and int(existing["completed"]):
        return {
            "checkpoint_key": key,
            "mode": _CONTRACT_HISTORY_MODE,
            "status": "already_complete",
            "stop_reason": "history_window_complete",
            "since": window.since.isoformat(),
            "until": window.until.isoformat() if window.until else None,
            "effective_until": effective_until.isoformat(),
            "pages_scanned": 0,
            "rows_seen": 0,
            "contracts_ingested": 0,
            "shards_completed": 0,
            "shard_splits": 0,
            "next_skip": 0,
            "current_shard": None,
            "progress": _progress(started_at=time.monotonic(), requests=0),
        }

    if existing is None:
        _save_checkpoint(
            conn,
            key=key,
            region_code=region_code,
            window=window,
            params=checkpoint_params,
            page_limit=limit,
            next_skip=0,
            pages_scanned_delta=0,
            rows_seen_delta=0,
            rows_in_window_delta=0,
            details_fetched_delta=0,
            newest_published_at=None,
            oldest_published_at=None,
            completed=False,
        )

    if request_delay is not None:
        delay = max(0.0, request_delay)
    elif rate_per_minute is not None:
        delay = 60.0 / rate_per_minute
    else:
        delay = default_request_delay(client.base_url)

    def pause() -> None:
        if delay > 0:
            time.sleep(delay)

    rows_seen = 0
    contracts_ingested = 0
    pages_scanned = 0
    list_requests = 0
    shards_completed = 0
    shard_splits = 0
    newest_seen: str | None = None
    oldest_seen: str | None = None
    started_at = time.monotonic()
    stop_reason: str | None = None
    current_shard: tuple[datetime, datetime] | None = None
    next_skip_result = 0

    # The real API accepted skip=1000 and rejected deeper offsets. A leaf with
    # at most 1000+limit rows remains addressable from skip=0.
    addressable_rows = 1000 + limit

    def save_parent(
        *,
        next_skip: int,
        rows_delta: int = 0,
        newest: str | None = None,
        oldest: str | None = None,
        completed: bool = False,
    ) -> None:
        _save_checkpoint(
            conn,
            key=key,
            region_code=region_code,
            window=window,
            params=checkpoint_params,
            page_limit=limit,
            next_skip=next_skip,
            pages_scanned_delta=1 if rows_delta or newest or oldest else 0,
            rows_seen_delta=rows_delta,
            rows_in_window_delta=rows_delta,
            details_fetched_delta=0,
            newest_published_at=newest,
            oldest_published_at=oldest,
            completed=completed,
        )

    def split_interval(
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]] | None:
        duration = end_at - start_at
        if duration <= timedelta(seconds=1):
            return None
        midpoint = start_at + duration / 2
        newer_start = midpoint + timedelta(microseconds=1)
        if newer_start > end_at:
            return None
        return (newer_start, end_at), (start_at, midpoint)

    def process_interval(start_at: datetime, end_at: datetime) -> bool:
        nonlocal rows_seen, contracts_ingested, pages_scanned, list_requests
        nonlocal shards_completed, shard_splits, newest_seen, oldest_seen
        nonlocal stop_reason, current_shard, next_skip_result

        if stop_reason is not None:
            return False

        saved = _time_shard_row(
            conn,
            checkpoint_key_value=key,
            shard_start=start_at,
            shard_end=end_at,
        )
        if saved is not None and int(saved["completed"]):
            return True

        saved_skip = int(saved["next_skip"]) if saved is not None else 0
        overlap_rows = resume_overlap_pages * limit if saved is not None else 0
        current_skip = max(0, saved_skip - overlap_rows)
        total_count = (
            int(saved["total_count"])
            if saved is not None and saved["total_count"] is not None
            else None
        )
        current_shard = (start_at, end_at)
        next_skip_result = current_skip

        shard_params = dict(params)
        shard_params["published_after"] = _api_datetime(start_at)
        shard_params["published_before"] = _api_datetime(end_at)

        while stop_reason is None:
            if pages is not None and pages_scanned >= pages:
                stop_reason = "page_budget"
                return False

            page_skip = current_skip
            try:
                rows, reported_total = client.get_rows_with_total(
                    _CONTRACTS_ENDPOINT,
                    limit=limit,
                    skip=page_skip,
                    extra=shard_params,
                )
                list_requests += 1
                pages_scanned += 1
                pause()
            except httpx.HTTPStatusError as exc:
                list_requests += 1
                pages_scanned += 1
                pause()
                if _skip_cap_from_error(exc) is None:
                    raise
                halves = split_interval(start_at, end_at)
                if halves is None:
                    stop_reason = "shard_too_large"
                    emit(
                        "contract-history shard cannot be split further after pagination cap: "
                        f"{_api_datetime(start_at)}..{_api_datetime(end_at)}"
                    )
                    return False
                shard_splits += 1
                newer, older = halves
                emit(
                    "contract-history shard split after skip cap: "
                    f"{_api_datetime(start_at)}..{_api_datetime(end_at)} skip={page_skip}"
                )
                if not process_interval(*newer):
                    return False
                if not process_interval(*older):
                    return False
                _save_time_shard(
                    conn,
                    checkpoint_key_value=key,
                    shard_start=start_at,
                    shard_end=end_at,
                    next_skip=0,
                    total_count=total_count,
                    completed=True,
                )
                shards_completed += 1
                return True

            if reported_total is not None:
                total_count = reported_total

            if total_count is not None and total_count > addressable_rows:
                halves = split_interval(start_at, end_at)
                if halves is None:
                    stop_reason = "shard_too_large"
                    emit(
                        f"contract-history shard x-total={total_count} exceeds pagination "
                        f"capacity for {_api_datetime(start_at)}..{_api_datetime(end_at)}"
                    )
                    return False
                shard_splits += 1
                newer, older = halves
                emit(
                    f"contract-history shard split x-total={total_count}: "
                    f"{_api_datetime(start_at)}..{_api_datetime(end_at)}"
                )
                if not process_interval(*newer):
                    return False
                if not process_interval(*older):
                    return False
                _save_time_shard(
                    conn,
                    checkpoint_key_value=key,
                    shard_start=start_at,
                    shard_end=end_at,
                    next_skip=0,
                    total_count=total_count,
                    completed=True,
                )
                shards_completed += 1
                return True

            if not rows:
                _save_time_shard(
                    conn,
                    checkpoint_key_value=key,
                    shard_start=start_at,
                    shard_end=end_at,
                    next_skip=page_skip,
                    total_count=total_count,
                    completed=True,
                )
                shards_completed += 1
                return True

            wrong_regions = sorted(
                {row.get("region") for row in rows if row.get("region") != region_code}
            )
            if wrong_regions:
                raise RuntimeError(
                    "Gosplan did not honor the expected contract region filter. "
                    f"Requested region={region_code}, response also contained={wrong_regions[:5]}."
                )

            page_newest: str | None = None
            page_oldest: str | None = None
            for row in rows:
                published_at = str(row.get("published_at") or "") or None
                published_dt = _published_datetime(published_at)
                if published_dt is None:
                    raise RuntimeError(
                        "contract-history requires published_at on every row; "
                        f"reg_num={row.get('reg_num')!r}"
                    )
                if not (start_at <= published_dt <= end_at):
                    raise RuntimeError(
                        "Gosplan did not honor contract published_after/published_before filters. "
                        f"Row {row.get('reg_num')!r} has published_at={published_at!r}, "
                        f"outside {_api_datetime(start_at)}..{_api_datetime(end_at)}."
                    )
                if published_at:
                    page_newest = (
                        published_at
                        if page_newest is None or published_at > page_newest
                        else page_newest
                    )
                    page_oldest = (
                        published_at
                        if page_oldest is None or published_at < page_oldest
                        else page_oldest
                    )
                    newest_seen = (
                        published_at
                        if newest_seen is None or published_at > newest_seen
                        else newest_seen
                    )
                    oldest_seen = (
                        published_at
                        if oldest_seen is None or published_at < oldest_seen
                        else oldest_seen
                    )
                ingest_contract(conn, row)
                rows_seen += 1
                contracts_ingested += 1

            conn.commit()
            current_skip = page_skip + len(rows)
            next_skip_result = current_skip
            done = (
                (total_count is not None and current_skip >= total_count)
                or len(rows) < limit
            )
            _save_time_shard(
                conn,
                checkpoint_key_value=key,
                shard_start=start_at,
                shard_end=end_at,
                next_skip=current_skip,
                total_count=total_count,
                completed=done,
                rows_seen_delta=len(rows),
                rows_in_window_delta=len(rows),
            )
            save_parent(
                next_skip=current_skip,
                rows_delta=len(rows),
                newest=page_newest,
                oldest=page_oldest,
            )

            progress = _progress(started_at=started_at, requests=list_requests)
            rpm = progress["avg_requests_per_minute"]
            rpm_text = f"{rpm:.2f}" if rpm is not None else "-"
            emit(
                f"contract-history shard={_api_datetime(start_at)}..{_api_datetime(end_at)} "
                f"skip={page_skip} rows={len(rows)} "
                f"x_total={total_count if total_count is not None else '?'} "
                f"contracts={contracts_ingested} next_skip={current_skip} "
                f"api_requests={list_requests} avg_rpm={rpm_text} "
                f"elapsed={progress['elapsed'] or '-'}"
            )

            if done:
                shards_completed += 1
                return True

        return False

    day = effective_until
    while day >= window.since and stop_reason is None:
        start_at = datetime.combine(day, dt_time.min, tzinfo=timezone.utc)
        end_at = datetime.combine(day, dt_time.max, tzinfo=timezone.utc)
        if not process_interval(start_at, end_at):
            break
        day -= timedelta(days=1)

    completed = stop_reason is None and day < window.since
    if completed:
        stop_reason = "history_window_complete"
        _save_checkpoint(
            conn,
            key=key,
            region_code=region_code,
            window=window,
            params=checkpoint_params,
            page_limit=limit,
            next_skip=0,
            pages_scanned_delta=0,
            rows_seen_delta=0,
            rows_in_window_delta=0,
            details_fetched_delta=0,
            newest_published_at=None,
            oldest_published_at=None,
            completed=True,
        )

    current_shard_text = None
    if current_shard is not None:
        current_shard_text = (
            f"{_api_datetime(current_shard[0])}..{_api_datetime(current_shard[1])}"
        )

    return {
        "checkpoint_key": key,
        "mode": _CONTRACT_HISTORY_MODE,
        "status": "complete" if completed else "paused",
        "stop_reason": stop_reason,
        "since": window.since.isoformat(),
        "until": window.until.isoformat() if window.until else None,
        "effective_until": effective_until.isoformat(),
        "next_skip": next_skip_result,
        "current_shard": current_shard_text,
        "shards_completed": shards_completed,
        "shard_splits": shard_splits,
        "pages_scanned": pages_scanned,
        "rows_seen": rows_seen,
        "contracts_ingested": contracts_ingested,
        "newest_published_at": newest_seen,
        "oldest_published_at": oldest_seen,
        "request_delay_seconds": delay,
        "progress": _progress(started_at=started_at, requests=list_requests),
    }
