from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
import hashlib
import json
import sqlite3
import time

import httpx
from typing import Any, Callable

from .bulk import default_request_delay, new_ingest_stats, process_purchase_row
from .client import GosplanClient


@dataclass(slots=True, frozen=True)
class HistoryWindow:
    since: date
    until: date | None = None

    @classmethod
    def parse(cls, since: str, until: str | None = None) -> "HistoryWindow":
        since_date = date.fromisoformat(since)
        until_date = date.fromisoformat(until) if until else None
        if until_date is not None and until_date < since_date:
            raise ValueError("--until must be on or after --since")
        return cls(since=since_date, until=until_date)


def _published_date(row: dict[str, Any]) -> date | None:
    value = row.get("published_at")
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def checkpoint_key(
    *,
    region_code: int,
    window: HistoryWindow,
    params: dict[str, str],
) -> str:
    query = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
    until = window.until.isoformat() if window.until else "latest"
    return f"region={region_code};since={window.since.isoformat()};until={until};q={digest}"


def _checkpoint_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM history_backfills WHERE checkpoint_key=?",
        (key,),
    ).fetchone()


def _reset_checkpoint(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM history_backfills WHERE checkpoint_key=?", (key,))
    conn.commit()


def _save_checkpoint(
    conn: sqlite3.Connection,
    *,
    key: str,
    region_code: int,
    window: HistoryWindow,
    params: dict[str, str],
    page_limit: int,
    next_skip: int,
    pages_scanned_delta: int,
    rows_seen_delta: int,
    rows_in_window_delta: int,
    details_fetched_delta: int,
    newest_published_at: str | None,
    oldest_published_at: str | None,
    completed: bool,
) -> None:
    query_json = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO history_backfills(
            checkpoint_key, region_code, since_date, until_date, query_json,
            page_limit, next_skip, pages_scanned, rows_seen, rows_in_window,
            details_fetched, newest_published_at, oldest_published_at, completed,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(checkpoint_key) DO UPDATE SET
            page_limit=excluded.page_limit,
            next_skip=excluded.next_skip,
            pages_scanned=history_backfills.pages_scanned + excluded.pages_scanned,
            rows_seen=history_backfills.rows_seen + excluded.rows_seen,
            rows_in_window=history_backfills.rows_in_window + excluded.rows_in_window,
            details_fetched=history_backfills.details_fetched + excluded.details_fetched,
            newest_published_at=CASE
                WHEN history_backfills.newest_published_at IS NULL THEN excluded.newest_published_at
                WHEN excluded.newest_published_at IS NULL THEN history_backfills.newest_published_at
                WHEN excluded.newest_published_at > history_backfills.newest_published_at THEN excluded.newest_published_at
                ELSE history_backfills.newest_published_at
            END,
            oldest_published_at=CASE
                WHEN history_backfills.oldest_published_at IS NULL THEN excluded.oldest_published_at
                WHEN excluded.oldest_published_at IS NULL THEN history_backfills.oldest_published_at
                WHEN excluded.oldest_published_at < history_backfills.oldest_published_at THEN excluded.oldest_published_at
                ELSE history_backfills.oldest_published_at
            END,
            completed=excluded.completed,
            updated_at=datetime('now')
        """,
        (
            key,
            region_code,
            window.since.isoformat(),
            window.until.isoformat() if window.until else None,
            query_json,
            page_limit,
            next_skip,
            pages_scanned_delta,
            rows_seen_delta,
            rows_in_window_delta,
            details_fetched_delta,
            newest_published_at,
            oldest_published_at,
            int(completed),
        ),
    )
    conn.commit()


def history_status(conn: sqlite3.Connection, *, region_code: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM history_backfills"
    params: tuple[Any, ...] = ()
    if region_code is not None:
        sql += " WHERE region_code=?"
        params = (region_code,)
    sql += " ORDER BY updated_at DESC, checkpoint_key"
    result: list[dict[str, Any]] = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        try:
            query = json.loads(item.get("query_json") or "{}")
        except (TypeError, ValueError):
            query = {}
        item["mode"] = query.get("__history_mode", "legacy_global_skip")
        result.append(item)
    return result


def _format_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    total = int(round(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _timestamp_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _progress_metrics(
    *,
    window: HistoryWindow,
    started_at: float,
    pages_scanned: int,
    list_requests: int | None = None,
    ingest_stats: dict[str, int],
    newest_published_at: str | None,
    oldest_published_at: str | None,
) -> dict[str, Any]:
    elapsed = max(0.0, time.monotonic() - started_at)
    api_requests = (
        (pages_scanned if list_requests is None else list_requests)
        + ingest_stats.get("detail_requests", 0)
        + ingest_stats.get("protocol_fallback_requests", 0)
    )
    avg_requests_per_minute = (api_requests / elapsed * 60.0) if elapsed >= 1.0 else None

    oldest = _timestamp_date(oldest_published_at)
    anchor = window.until or _timestamp_date(newest_published_at)
    eta_seconds: float | None = None
    if elapsed >= 1.0 and oldest is not None and anchor is not None and oldest > window.since:
        covered_days = (anchor - oldest).days
        remaining_days = (oldest - window.since).days
        if covered_days > 0 and remaining_days > 0:
            eta_seconds = elapsed * remaining_days / covered_days

    return {
        "elapsed_seconds": elapsed,
        "elapsed": _format_duration(elapsed),
        "api_requests": api_requests,
        "avg_requests_per_minute": avg_requests_per_minute,
        "eta_seconds": eta_seconds,
        "eta": _format_duration(eta_seconds),
    }


def _page_signature(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(str(row.get("purchase_number") or row.get("id") or "") for row in rows)


def _skip_cap_from_error(exc: httpx.HTTPStatusError) -> int | None:
    response = exc.response
    if response.status_code != 422:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    details = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(details, list):
        return None
    for detail in details:
        if not isinstance(detail, dict):
            continue
        loc = detail.get("loc")
        if not isinstance(loc, list) or not loc or loc[-1] != "skip":
            continue
        ctx = detail.get("ctx")
        if isinstance(ctx, dict):
            for key in ("le", "less_than_equal"):
                value = ctx.get(key)
                if isinstance(value, (int, float)):
                    return int(value)
        # We positively identified a validation error on the skip query parameter,
        # even if this FastAPI/Pydantic version did not expose the bound in ctx.
        return 1000
    return None


def ingest_region_history(
    conn: sqlite3.Connection,
    client: GosplanClient,
    *,
    region_code: int,
    since: str,
    until: str | None = None,
    limit: int = 50,
    pages: int | None = 100,
    extra: dict[str, str] | None = None,
    fetch_details: bool = True,
    max_details: int | None = 100,
    request_delay: float | None = None,
    rate_per_minute: float | None = None,
    refresh_details: bool = False,
    protocol_fallback: bool = True,
    resume_overlap_pages: int = 2,
    live_overlap_every_pages: int = 25,
    restart: bool = False,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Backfill a region by walking the purchase index backwards in time.

    ``pages=None`` and ``max_details=None`` make the run unbounded. This is the
    mode used by the CLI's ``--continuous`` switch. The collector checkpoints
    after every completed page and also checkpoints the current position when a
    ``KeyboardInterrupt`` happens while processing a page, so Ctrl+C is safe.

    Gosplan's public historical-data guide does not document a reliable date-range
    query for this use case, so this collector deliberately relies only on the
    documented list pagination plus the row's ``published_at``. The remote index is
    not guaranteed to be strictly ordered by ``published_at``. Therefore ``since``
    and ``until`` are row filters only; the collector never treats a local date
    inversion as an error and never stops early just because it encountered a row
    older than ``since``. A complete run ends only when the remote index is exhausted.

    The checkpoint stores the list offset. On resume we intentionally rewind a few
    pages because newly inserted rows at the head of the remote index can shift
    offsets between runs. Re-reading overlap is safe because list/detail ingestion
    is idempotent and successful detail fetches are cached locally.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if pages is not None and pages <= 0:
        raise ValueError("pages must be positive or None")
    if resume_overlap_pages < 0:
        raise ValueError("resume_overlap_pages cannot be negative")
    if live_overlap_every_pages <= 0:
        raise ValueError("live_overlap_every_pages must be positive")
    if max_details is not None and max_details < 0:
        raise ValueError("max_details cannot be negative")
    if request_delay is not None and rate_per_minute is not None:
        raise ValueError("use either request_delay or rate_per_minute, not both")
    if rate_per_minute is not None and rate_per_minute <= 0:
        raise ValueError("rate_per_minute must be positive")

    window = HistoryWindow.parse(since, until)
    params = dict(extra or {})
    params.setdefault("stage", "2")
    params["region"] = str(region_code)
    key = checkpoint_key(region_code=region_code, window=window, params=params)

    if restart:
        _reset_checkpoint(conn, key)

    existing = _checkpoint_row(conn, key)
    if existing is not None and int(existing["completed"]):
        return {
            "checkpoint_key": key,
            "status": "already_complete",
            "next_skip": int(existing["next_skip"]),
            "since": window.since.isoformat(),
            "until": window.until.isoformat() if window.until else None,
            "stats": new_ingest_stats(),
            "rows_seen": 0,
            "rows_in_window": 0,
            "rows_newer_than_window": 0,
            "rows_older_than_window": 0,
            "rows_missing_date": 0,
            "out_of_order_rows": 0,
            "pages_scanned": 0,
            "progress": {
                "elapsed_seconds": 0.0,
                "elapsed": "0s",
                "api_requests": 0,
                "avg_requests_per_minute": None,
                "eta_seconds": 0.0,
                "eta": "0s",
            },
        }

    saved_skip = int(existing["next_skip"]) if existing is not None else 0
    overlap_rows = resume_overlap_pages * limit if existing is not None else 0
    current_skip = max(0, saved_skip - overlap_rows)

    if request_delay is not None:
        delay = max(0.0, request_delay)
    elif rate_per_minute is not None:
        # This is intentionally conservative: the delay happens after a request,
        # so network/processing time is additional headroom below the requested RPM.
        delay = 60.0 / rate_per_minute
    else:
        delay = default_request_delay(client.base_url)

    def pause() -> None:
        if delay > 0:
            time.sleep(delay)

    ingest_stats = new_ingest_stats()
    rows_seen = 0
    rows_in_window = 0
    rows_newer = 0
    rows_older = 0
    rows_missing_date = 0
    pages_scanned = 0
    list_requests = 0
    pagination_param = "skip"
    pagination_fallback = "none"
    newest_seen: str | None = None
    oldest_seen: str | None = None
    out_of_order_rows = 0
    last_date: date | None = None
    completed = False
    stop_reason = "page_budget" if pages is not None else "running"
    next_skip = current_skip
    started_at = time.monotonic()

    # Current-page counters are kept outside the loop so Ctrl+C can persist an
    # exact safe resume position without pretending the whole page was processed.
    page_active = False
    page_rows_seen = 0
    page_rows_in_window = 0
    page_details_before = 0
    page_newest: str | None = None
    page_oldest: str | None = None

    try:
        while pages is None or pages_scanned < pages:
            page_skip = current_skip
            try:
                rows = client.get_purchases(
                    limit=limit,
                    skip=page_skip,
                    extra=params,
                    pagination_param=pagination_param,
                )
                list_requests += 1
                pause()
            except httpx.HTTPStatusError as exc:
                list_requests += 1
                pause()
                skip_cap = _skip_cap_from_error(exc) if pagination_param == "skip" else None
                if skip_cap is None:
                    raise

                emit(
                    f"history pagination cap detected: API rejected skip={page_skip} "
                    f"(reported max skip={skip_cap}); probing legacy offset pagination"
                )
                try:
                    offset_head = client.get_purchases(
                        limit=limit,
                        skip=0,
                        extra=params,
                        pagination_param="offset",
                    )
                    list_requests += 1
                    pause()
                    offset_rows = client.get_purchases(
                        limit=limit,
                        skip=page_skip,
                        extra=params,
                        pagination_param="offset",
                    )
                    list_requests += 1
                    pause()
                except httpx.HTTPStatusError:
                    list_requests += 1
                    pause()
                    offset_head = []
                    offset_rows = []

                if offset_rows and _page_signature(offset_rows) != _page_signature(offset_head):
                    pagination_param = "offset"
                    pagination_fallback = "legacy_offset"
                    rows = offset_rows
                    emit(
                        f"history pagination fallback active: using offset={page_skip} "
                        "after skip cap"
                    )
                else:
                    stop_reason = "pagination_cap"
                    next_skip = page_skip
                    pagination_fallback = "unavailable"
                    _save_checkpoint(
                        conn,
                        key=key,
                        region_code=region_code,
                        window=window,
                        params=params,
                        page_limit=limit,
                        next_skip=next_skip,
                        pages_scanned_delta=0,
                        rows_seen_delta=0,
                        rows_in_window_delta=0,
                        details_fetched_delta=0,
                        newest_published_at=None,
                        oldest_published_at=None,
                        completed=False,
                    )
                    emit(
                        "history stopped at pagination_cap: test API does not expose "
                        "a verified deep-pagination path beyond the skip limit for this query"
                    )
                    break
            pages_scanned += 1
            if not rows:
                completed = True
                stop_reason = "index_exhausted"
                next_skip = page_skip
                _save_checkpoint(
                    conn,
                    key=key,
                    region_code=region_code,
                    window=window,
                    params=params,
                    page_limit=limit,
                    next_skip=next_skip,
                    pages_scanned_delta=1,
                    rows_seen_delta=0,
                    rows_in_window_delta=0,
                    details_fetched_delta=0,
                    newest_published_at=None,
                    oldest_published_at=None,
                    completed=True,
                )
                break

            wrong_regions = sorted(
                {row.get("region") for row in rows if row.get("region") != region_code}
            )
            if wrong_regions:
                raise RuntimeError(
                    "Gosplan did not honor the expected region filter parameter 'region'. "
                    f"Requested region={region_code}, response also contained={wrong_regions[:5]}."
                )

            page_active = True
            page_rows_seen = 0
            page_rows_in_window = 0
            page_details_before = ingest_stats["details"]
            page_newest = None
            page_oldest = None
            stop_mid_page = False

            for idx, row in enumerate(rows):
                rows_seen += 1
                page_rows_seen += 1
                row_date = _published_date(row)
                published_at = str(row.get("published_at") or "") or None
                if published_at:
                    page_newest = published_at if page_newest is None or published_at > page_newest else page_newest
                    page_oldest = published_at if page_oldest is None or published_at < page_oldest else page_oldest
                    newest_seen = published_at if newest_seen is None or published_at > newest_seen else newest_seen
                    oldest_seen = published_at if oldest_seen is None or published_at < oldest_seen else oldest_seen

                if row_date is None:
                    rows_missing_date += 1
                    next_skip = page_skip + idx + 1
                    continue

                if last_date is not None and row_date > last_date:
                    out_of_order_rows += 1
                last_date = row_date

                if window.until is not None and row_date > window.until:
                    rows_newer += 1
                    next_skip = page_skip + idx + 1
                    continue

                if row_date < window.since:
                    rows_older += 1
                    next_skip = page_skip + idx + 1
                    continue

                outcome = process_purchase_row(
                    conn,
                    client,
                    row,
                    region_code=region_code,
                    stats=ingest_stats,
                    pause=pause,
                    fetch_details=fetch_details,
                    max_details=max_details,
                    refresh_details=refresh_details,
                    protocol_fallback=protocol_fallback,
                    stop_at_detail_limit=True,
                    emit=emit,
                )
                if outcome == "detail_limit":
                    stop_reason = "detail_budget"
                    next_skip = page_skip + idx
                    stop_mid_page = True
                    break

                rows_in_window += 1
                page_rows_in_window += 1
                next_skip = page_skip + idx + 1

            page_details = ingest_stats["details"] - page_details_before
            _save_checkpoint(
                conn,
                key=key,
                region_code=region_code,
                window=window,
                params=params,
                page_limit=limit,
                next_skip=next_skip,
                pages_scanned_delta=1,
                rows_seen_delta=page_rows_seen,
                rows_in_window_delta=page_rows_in_window,
                details_fetched_delta=page_details,
                newest_published_at=page_newest,
                oldest_published_at=page_oldest,
                completed=completed,
            )
            page_active = False

            progress = _progress_metrics(
                window=window,
                started_at=started_at,
                pages_scanned=pages_scanned,
                list_requests=list_requests,
                ingest_stats=ingest_stats,
                newest_published_at=newest_seen,
                oldest_published_at=oldest_seen,
            )
            rpm = progress["avg_requests_per_minute"]
            rpm_text = f"{rpm:.2f}" if rpm is not None else "-"
            eta_text = progress["eta"] or "-"
            emit(
                f"history page={pages_scanned} skip={page_skip} rows={len(rows)} "
                f"window_rows={rows_in_window} details={ingest_stats['details']} "
                f"cached={ingest_stats['detail_skipped_cached']} next_skip={next_skip} "
                f"oldest={page_oldest} out_of_order={out_of_order_rows} pagination={pagination_param} "
                f"api_requests={progress['api_requests']} "
                f"avg_rpm={rpm_text} elapsed={progress['elapsed']} eta={eta_text}"
            )

            if stop_mid_page:
                break
            if len(rows) < limit:
                completed = True
                stop_reason = "index_exhausted"
                _save_checkpoint(
                    conn,
                    key=key,
                    region_code=region_code,
                    window=window,
                    params=params,
                    page_limit=limit,
                    next_skip=next_skip,
                    pages_scanned_delta=0,
                    rows_seen_delta=0,
                    rows_in_window_delta=0,
                    details_fetched_delta=0,
                    newest_published_at=None,
                    oldest_published_at=None,
                    completed=True,
                )
                break
            current_skip = page_skip + len(rows)
            if (
                pages is None
                and resume_overlap_pages > 0
                and pages_scanned % live_overlap_every_pages == 0
            ):
                rewind_rows = resume_overlap_pages * limit
                rewind_skip = max(0, current_skip - rewind_rows)
                if rewind_skip < current_skip:
                    emit(
                        f"history live-overlap rewind from skip={current_skip} "
                        f"to skip={rewind_skip} to guard against moving offset pagination"
                    )
                    current_skip = rewind_skip
                    last_date = None

    except KeyboardInterrupt:
        stop_reason = "interrupted"
        if page_active:
            page_details = ingest_stats["details"] - page_details_before
            _save_checkpoint(
                conn,
                key=key,
                region_code=region_code,
                window=window,
                params=params,
                page_limit=limit,
                next_skip=next_skip,
                pages_scanned_delta=1,
                rows_seen_delta=page_rows_seen,
                rows_in_window_delta=page_rows_in_window,
                details_fetched_delta=page_details,
                newest_published_at=page_newest,
                oldest_published_at=page_oldest,
                completed=False,
            )
        emit(f"history interrupted; checkpoint saved at next_skip={next_skip}")

    progress = _progress_metrics(
        window=window,
        started_at=started_at,
        pages_scanned=pages_scanned,
        list_requests=list_requests,
        ingest_stats=ingest_stats,
        newest_published_at=newest_seen,
        oldest_published_at=oldest_seen,
    )
    return {
        "checkpoint_key": key,
        "status": "complete" if completed else "paused",
        "stop_reason": stop_reason,
        "next_skip": next_skip,
        "since": window.since.isoformat(),
        "until": window.until.isoformat() if window.until else None,
        "pages_scanned": pages_scanned,
        "rows_seen": rows_seen,
        "rows_in_window": rows_in_window,
        "rows_newer_than_window": rows_newer,
        "rows_older_than_window": rows_older,
        "rows_missing_date": rows_missing_date,
        "out_of_order_rows": out_of_order_rows,
        "pagination_param": pagination_param,
        "pagination_fallback": pagination_fallback,
        "newest_published_at": newest_seen,
        "oldest_published_at": oldest_seen,
        "request_delay_seconds": delay,
        "rate_per_minute": rate_per_minute,
        "progress": progress,
        "stats": ingest_stats,
    }


def _api_datetime(value: datetime) -> str:
    """Format an aware UTC datetime for Gosplan's OpenAPI date-time filters."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _time_shard_row(
    conn: sqlite3.Connection,
    *,
    checkpoint_key_value: str,
    shard_start: datetime,
    shard_end: datetime,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM history_time_shards
        WHERE checkpoint_key=? AND shard_start=? AND shard_end=?
        """,
        (
            checkpoint_key_value,
            _api_datetime(shard_start),
            _api_datetime(shard_end),
        ),
    ).fetchone()


def _save_time_shard(
    conn: sqlite3.Connection,
    *,
    checkpoint_key_value: str,
    shard_start: datetime,
    shard_end: datetime,
    next_skip: int,
    total_count: int | None,
    completed: bool,
    rows_seen_delta: int = 0,
    rows_in_window_delta: int = 0,
    details_fetched_delta: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO history_time_shards(
            checkpoint_key, shard_start, shard_end, next_skip, total_count,
            completed, rows_seen, rows_in_window, details_fetched, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(checkpoint_key, shard_start, shard_end) DO UPDATE SET
            next_skip=excluded.next_skip,
            total_count=COALESCE(excluded.total_count, history_time_shards.total_count),
            completed=excluded.completed,
            rows_seen=history_time_shards.rows_seen + excluded.rows_seen,
            rows_in_window=history_time_shards.rows_in_window + excluded.rows_in_window,
            details_fetched=history_time_shards.details_fetched + excluded.details_fetched,
            updated_at=datetime('now')
        """,
        (
            checkpoint_key_value,
            _api_datetime(shard_start),
            _api_datetime(shard_end),
            next_skip,
            total_count,
            int(completed),
            rows_seen_delta,
            rows_in_window_delta,
            details_fetched_delta,
        ),
    )
    conn.commit()


def _reset_sharded_checkpoint(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM history_time_shards WHERE checkpoint_key=?", (key,))
    conn.execute("DELETE FROM history_backfills WHERE checkpoint_key=?", (key,))
    conn.commit()


def _purchase_page_with_total(
    client: GosplanClient,
    *,
    limit: int,
    skip: int,
    extra: dict[str, str],
) -> tuple[list[dict[str, Any]], int | None]:
    method = getattr(client, "get_purchases_with_total", None)
    if callable(method):
        return method(limit=limit, skip=skip, extra=extra)
    # Compatibility for tests/custom clients written before x-total support.
    return client.get_purchases(limit=limit, skip=skip, extra=extra), None


def ingest_region_history_sharded(
    conn: sqlite3.Connection,
    client: GosplanClient,
    *,
    region_code: int,
    since: str,
    until: str | None = None,
    limit: int = 50,
    pages: int | None = 100,
    extra: dict[str, str] | None = None,
    fetch_details: bool = True,
    max_details: int | None = 100,
    request_delay: float | None = None,
    rate_per_minute: float | None = None,
    refresh_details: bool = False,
    protocol_fallback: bool = True,
    resume_overlap_pages: int = 1,
    restart: bool = False,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Historical region backfill using documented published_at range shards.

    Gosplan documents ``published_after`` and ``published_before`` on
    ``GET /fz44/purchases``.  We exploit those filters so each time shard starts
    its own pagination at ``skip=0`` and therefore never depends on deep global
    offsets.  Initial shards are one UTC calendar day.  If the API's ``x-total``
    header says a shard is too large for the observed ``skip<=1000`` cap, the
    interval is recursively split in half (down to one second if necessary).

    Every leaf shard has its own SQLite checkpoint.  Existing purchase/detail
    data remains an idempotent cache, so switching from the legacy global-skip
    collector does not require rebuilding the database.
    """
    if not (1 <= limit <= 100):
        raise ValueError("limit must be between 1 and 100")
    if pages is not None and pages <= 0:
        raise ValueError("pages must be positive or None")
    if max_details is not None and max_details < 0:
        raise ValueError("max_details cannot be negative")
    if resume_overlap_pages < 0:
        raise ValueError("resume_overlap_pages cannot be negative")
    if request_delay is not None and rate_per_minute is not None:
        raise ValueError("use either request_delay or rate_per_minute, not both")
    if rate_per_minute is not None and rate_per_minute <= 0:
        raise ValueError("rate_per_minute must be positive")

    window = HistoryWindow.parse(since, until)
    effective_until = window.until or date.today()

    params = dict(extra or {})
    for controlled in ("published_after", "published_before"):
        if controlled in params:
            raise ValueError(
                f"{controlled} is managed by ingest-history date sharding; "
                f"use --since/--until instead of --param {controlled}=..."
            )
    params.setdefault("stage", "2")
    params["region"] = str(region_code)
    # The date filter makes ordering deterministic inside a shard and avoids the
    # relevance-sort behavior of object_info queries unless the user explicitly
    # supplied another supported sort.
    params.setdefault("sort", "published_at_desc")

    checkpoint_params = dict(params)
    checkpoint_params["__history_mode"] = "published_time_shards_v1"
    key = checkpoint_key(region_code=region_code, window=window, params=checkpoint_params)

    if restart:
        _reset_sharded_checkpoint(conn, key)

    existing = _checkpoint_row(conn, key)
    if existing is not None and int(existing["completed"]):
        return {
            "checkpoint_key": key,
            "mode": "published_time_shards_v1",
            "status": "already_complete",
            "stop_reason": "history_window_complete",
            "next_skip": 0,
            "since": window.since.isoformat(),
            "until": window.until.isoformat() if window.until else None,
            "stats": new_ingest_stats(),
            "rows_seen": 0,
            "rows_in_window": 0,
            "rows_newer_than_window": 0,
            "rows_older_than_window": 0,
            "rows_missing_date": 0,
            "out_of_order_rows": 0,
            "pages_scanned": 0,
            "shards_completed": 0,
            "current_shard": None,
            "progress": {
                "elapsed_seconds": 0.0,
                "elapsed": "0s",
                "api_requests": 0,
                "avg_requests_per_minute": None,
                "eta_seconds": 0.0,
                "eta": "0s",
            },
        }

    # Ensure the parent checkpoint exists before child shard rows are written.
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

    ingest_stats = new_ingest_stats()
    rows_seen = 0
    rows_in_window = 0
    rows_newer = 0
    rows_older = 0
    rows_missing_date = 0
    out_of_order_rows = 0
    pages_scanned = 0
    list_requests = 0
    shards_completed = 0
    shard_splits = 0
    newest_seen: str | None = None
    oldest_seen: str | None = None
    last_date: date | None = None
    started_at = time.monotonic()
    stop_reason: str | None = None
    current_shard: tuple[datetime, datetime] | None = None
    next_skip_result = 0

    # The server accepted skip=1000 and rejected skip=1050 in the real API.
    # With a page of `limit` rows, at most 1000+limit rows are addressable without
    # another split.  x-total lets us split before wasting requests.
    addressable_rows = 1000 + limit

    def save_parent(
        *,
        next_skip: int,
        page_rows_seen: int = 0,
        page_rows_in_window: int = 0,
        page_details: int = 0,
        page_newest: str | None = None,
        page_oldest: str | None = None,
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
            pages_scanned_delta=1 if page_rows_seen or page_newest or page_oldest else 0,
            rows_seen_delta=page_rows_seen,
            rows_in_window_delta=page_rows_in_window,
            details_fetched_delta=page_details,
            newest_published_at=page_newest,
            oldest_published_at=page_oldest,
            completed=completed,
        )

    def split_interval(start_at: datetime, end_at: datetime) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]] | None:
        duration = end_at - start_at
        if duration <= timedelta(seconds=1):
            return None
        midpoint = start_at + duration / 2
        newer_start = midpoint + timedelta(microseconds=1)
        if newer_start > end_at:
            return None
        # Returned newest-first because historical backfill walks backwards.
        return (newer_start, end_at), (start_at, midpoint)

    def process_interval(start_at: datetime, end_at: datetime) -> bool:
        nonlocal rows_seen, rows_in_window, rows_newer, rows_older, rows_missing_date
        nonlocal out_of_order_rows, pages_scanned, list_requests, shards_completed
        nonlocal shard_splits, newest_seen, oldest_seen, last_date, stop_reason
        nonlocal current_shard, next_skip_result

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
        total_count = int(saved["total_count"]) if saved is not None and saved["total_count"] is not None else None
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
                rows, reported_total = _purchase_page_with_total(
                    client,
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
                cap = _skip_cap_from_error(exc)
                if cap is None:
                    raise
                halves = split_interval(start_at, end_at)
                if halves is None:
                    stop_reason = "shard_too_large"
                    emit(
                        "history shard cannot be split further after pagination cap: "
                        f"{_api_datetime(start_at)}..{_api_datetime(end_at)}"
                    )
                    return False
                shard_splits += 1
                emit(
                    f"history shard split after skip cap: {_api_datetime(start_at)}.."
                    f"{_api_datetime(end_at)} skip={page_skip}"
                )
                newer, older = halves
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

            # Split before ingesting the oversized parent's rows.  This avoids
            # spending detail calls on rows that the child shards will revisit.
            if total_count is not None and total_count > addressable_rows:
                halves = split_interval(start_at, end_at)
                if halves is None:
                    stop_reason = "shard_too_large"
                    emit(
                        f"history shard x-total={total_count} exceeds pagination capacity "
                        f"for {_api_datetime(start_at)}..{_api_datetime(end_at)}"
                    )
                    return False
                shard_splits += 1
                emit(
                    f"history shard split x-total={total_count}: "
                    f"{_api_datetime(start_at)}..{_api_datetime(end_at)}"
                )
                newer, older = halves
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
                    "Gosplan did not honor the expected region filter parameter 'region'. "
                    f"Requested region={region_code}, response also contained={wrong_regions[:5]}."
                )

            page_rows_seen = 0
            page_rows_in_window = 0
            page_details_before = ingest_stats["details"]
            page_newest: str | None = None
            page_oldest: str | None = None

            for idx, row in enumerate(rows):
                row_offset = page_skip + idx
                published_at = str(row.get("published_at") or "") or None
                row_date = _published_date(row)
                rows_seen += 1
                page_rows_seen += 1

                if published_at:
                    page_newest = published_at if page_newest is None or published_at > page_newest else page_newest
                    page_oldest = published_at if page_oldest is None or published_at < page_oldest else page_oldest
                    newest_seen = published_at if newest_seen is None or published_at > newest_seen else newest_seen
                    oldest_seen = published_at if oldest_seen is None or published_at < oldest_seen else oldest_seen

                if row_date is None:
                    rows_missing_date += 1
                    next_skip_result = row_offset + 1
                    continue
                if last_date is not None and row_date > last_date:
                    out_of_order_rows += 1
                last_date = row_date
                if row_date > effective_until:
                    rows_newer += 1
                    next_skip_result = row_offset + 1
                    continue
                if row_date < window.since:
                    rows_older += 1
                    next_skip_result = row_offset + 1
                    continue

                try:
                    outcome = process_purchase_row(
                        conn,
                        client,
                        row,
                        region_code=region_code,
                        stats=ingest_stats,
                        pause=pause,
                        fetch_details=fetch_details,
                        max_details=max_details,
                        refresh_details=refresh_details,
                        protocol_fallback=protocol_fallback,
                        stop_at_detail_limit=True,
                        emit=emit,
                    )
                except KeyboardInterrupt:
                    next_skip_result = row_offset
                    page_details = ingest_stats["details"] - page_details_before
                    _save_time_shard(
                        conn,
                        checkpoint_key_value=key,
                        shard_start=start_at,
                        shard_end=end_at,
                        next_skip=next_skip_result,
                        total_count=total_count,
                        completed=False,
                        rows_seen_delta=page_rows_seen,
                        rows_in_window_delta=page_rows_in_window,
                        details_fetched_delta=page_details,
                    )
                    save_parent(
                        next_skip=next_skip_result,
                        page_rows_seen=page_rows_seen,
                        page_rows_in_window=page_rows_in_window,
                        page_details=page_details,
                        page_newest=page_newest,
                        page_oldest=page_oldest,
                    )
                    stop_reason = "interrupted"
                    return False

                if outcome == "detail_limit":
                    next_skip_result = row_offset
                    page_details = ingest_stats["details"] - page_details_before
                    _save_time_shard(
                        conn,
                        checkpoint_key_value=key,
                        shard_start=start_at,
                        shard_end=end_at,
                        next_skip=next_skip_result,
                        total_count=total_count,
                        completed=False,
                        rows_seen_delta=page_rows_seen,
                        rows_in_window_delta=page_rows_in_window,
                        details_fetched_delta=page_details,
                    )
                    save_parent(
                        next_skip=next_skip_result,
                        page_rows_seen=page_rows_seen,
                        page_rows_in_window=page_rows_in_window,
                        page_details=page_details,
                        page_newest=page_newest,
                        page_oldest=page_oldest,
                    )
                    stop_reason = "detail_budget"
                    return False

                rows_in_window += 1
                page_rows_in_window += 1
                next_skip_result = row_offset + 1

            page_details = ingest_stats["details"] - page_details_before
            current_skip = page_skip + len(rows)
            next_skip_result = current_skip

            done = False
            if total_count is not None and current_skip >= total_count:
                done = True
            elif len(rows) < limit:
                done = True

            _save_time_shard(
                conn,
                checkpoint_key_value=key,
                shard_start=start_at,
                shard_end=end_at,
                next_skip=current_skip,
                total_count=total_count,
                completed=done,
                rows_seen_delta=page_rows_seen,
                rows_in_window_delta=page_rows_in_window,
                details_fetched_delta=page_details,
            )
            save_parent(
                next_skip=current_skip,
                page_rows_seen=page_rows_seen,
                page_rows_in_window=page_rows_in_window,
                page_details=page_details,
                page_newest=page_newest,
                page_oldest=page_oldest,
            )

            progress = _progress_metrics(
                window=HistoryWindow(since=window.since, until=effective_until),
                started_at=started_at,
                pages_scanned=pages_scanned,
                list_requests=list_requests,
                ingest_stats=ingest_stats,
                newest_published_at=newest_seen,
                oldest_published_at=oldest_seen,
            )
            rpm = progress["avg_requests_per_minute"]
            rpm_text = f"{rpm:.2f}" if rpm is not None else "-"
            shard_label = start_at.date().isoformat()
            if start_at.date() != end_at.date() or start_at.time() != dt_time.min or end_at.time() != dt_time.max:
                shard_label = f"{_api_datetime(start_at)}..{_api_datetime(end_at)}"
            emit(
                f"history shard={shard_label} skip={page_skip} rows={len(rows)} "
                f"x_total={total_count if total_count is not None else '?'} "
                f"details={ingest_stats['details']} cached={ingest_stats['detail_skipped_cached']} "
                f"next_skip={current_skip} api_requests={progress['api_requests']} "
                f"avg_rpm={rpm_text} elapsed={progress['elapsed'] or '-'} eta={progress['eta'] or '-'}"
            )

            if done:
                shards_completed += 1
                return True

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

    progress = _progress_metrics(
        window=HistoryWindow(since=window.since, until=effective_until),
        started_at=started_at,
        pages_scanned=pages_scanned,
        list_requests=list_requests,
        ingest_stats=ingest_stats,
        newest_published_at=newest_seen,
        oldest_published_at=oldest_seen,
    )
    current_shard_text = None
    if current_shard is not None:
        current_shard_text = f"{_api_datetime(current_shard[0])}..{_api_datetime(current_shard[1])}"

    return {
        "checkpoint_key": key,
        "mode": "published_time_shards_v1",
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
        "rows_in_window": rows_in_window,
        "rows_newer_than_window": rows_newer,
        "rows_older_than_window": rows_older,
        "rows_missing_date": rows_missing_date,
        "out_of_order_rows": out_of_order_rows,
        "request_delay_seconds": delay,
        "progress": progress,
        "stats": ingest_stats,
    }
