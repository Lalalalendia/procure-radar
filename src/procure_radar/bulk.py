from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable

import httpx

from .classification import (
    competition_detail_exclusion_reason,
    is_competition_protocol,
)
from .client import GosplanClient
from .extract import extract_embedded_protocols, extract_purchase_items
from .ingest import ingest_purchase, ingest_tender_protocol


def default_request_delay(base_url: str) -> float:
    # Current v2test limit is 10 requests/minute; 6.2s leaves a little margin.
    return 6.2 if "v2test.gosplan.info" in base_url else 0.15


def new_ingest_stats() -> dict[str, int]:
    return {
        "purchases": 0,
        "detail_candidates": 0,
        "detail_requests": 0,
        "details": 0,
        "protocols": 0,
        "protocol_fallback_requests": 0,
        "detail_skipped_cached": 0,
        "detail_skipped_method": 0,
        "detail_skipped_cancelled": 0,
        "detail_skipped_no_final": 0,
        "detail_skipped_max": 0,
        "detail_errors": 0,
    }


def _detail_fetched(conn: sqlite3.Connection, purchase_number: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM purchase_detail_fetches WHERE purchase_number=? AND status='ok'",
        (purchase_number,),
    ).fetchone() is not None


def _mark_detail(
    conn: sqlite3.Connection,
    purchase_number: str,
    *,
    status: str,
    item_count: int = 0,
    protocol_count: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO purchase_detail_fetches(
            purchase_number, fetched_at, status, item_count, protocol_count
        ) VALUES (?, datetime('now'), ?, ?, ?)
        ON CONFLICT(purchase_number) DO UPDATE SET
            fetched_at=datetime('now'),
            status=excluded.status,
            item_count=excluded.item_count,
            protocol_count=excluded.protocol_count
        """,
        (purchase_number, status, item_count, protocol_count),
    )


def _protocol_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _has_matching_competition_protocol(
    purchase_type: str | None,
    protocols: list[dict[str, Any]],
) -> bool:
    return any(
        is_competition_protocol(purchase_type, protocol.get("doc_type"))
        for protocol in protocols
    )


def process_purchase_row(
    conn: sqlite3.Connection,
    client: GosplanClient,
    row: dict[str, Any],
    *,
    region_code: int,
    stats: dict[str, int],
    pause: Callable[[], None],
    fetch_details: bool = True,
    max_details: int | None = 100,
    refresh_details: bool = False,
    protocol_fallback: bool = True,
    stop_at_detail_limit: bool = False,
    emit: Callable[[str], None] = print,
) -> str:
    """Persist one list row and, when warranted, enrich it with detail/protocols.

    Returns ``"detail_limit"`` when a competitive detail candidate was reached
    after the per-run detail budget was exhausted and ``stop_at_detail_limit``
    is enabled. Historical collectors use that signal to checkpoint *before*
    the unprocessed row so a later run cannot silently skip it.
    """
    ingest_purchase(conn, row)
    stats["purchases"] += 1
    purchase_number = str(row.get("purchase_number") or "")
    purchase_type = row.get("purchase_type")
    stage = row.get("stage")

    if not fetch_details or not purchase_number:
        conn.commit()
        return "processed"

    exclusion = competition_detail_exclusion_reason(
        purchase_type,
        docs=row.get("docs") or [],
    )
    if exclusion is not None:
        if exclusion == "cancelled":
            stats["detail_skipped_cancelled"] += 1
        elif exclusion == "no_competitive_final_protocol":
            stats["detail_skipped_no_final"] += 1
        else:
            stats["detail_skipped_method"] += 1
        conn.commit()
        return "processed"

    stats["detail_candidates"] += 1
    detail_limit_reached = max_details is not None and stats["details"] >= max_details
    if detail_limit_reached:
        stats["detail_skipped_max"] += 1
        conn.commit()
        return "detail_limit" if stop_at_detail_limit else "processed"
    if not refresh_details and _detail_fetched(conn, purchase_number):
        stats["detail_skipped_cached"] += 1
        conn.commit()
        return "processed"

    try:
        stats["detail_requests"] += 1
        detail = client.get_purchase(purchase_number)
        pause()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _mark_detail(conn, purchase_number, status="404")
            stats["detail_errors"] += 1
            conn.commit()
            emit(f"detail 404 purchase={purchase_number}")
            return "processed"
        raise

    if not isinstance(detail, dict):
        _mark_detail(conn, purchase_number, status="invalid")
        stats["detail_errors"] += 1
        conn.commit()
        return "processed"
    if detail.get("region") != region_code:
        raise RuntimeError(
            f"detail region mismatch for {purchase_number}: {detail.get('region')} != {region_code}"
        )

    ingest_purchase(conn, detail)
    protocols = extract_embedded_protocols(detail)

    if (
        protocol_fallback
        and not _has_matching_competition_protocol(purchase_type, protocols)
    ):
        try:
            payload = client.get_purchase_protocols(purchase_number)
            pause()
            stats["protocol_fallback_requests"] += 1
            protocols = _protocol_rows(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            protocols = []

    protocol_count = 0
    for protocol in protocols:
        try:
            ingest_tender_protocol(conn, protocol)
            protocol_count += 1
        except ValueError as exc:
            emit(f"protocol skipped purchase={purchase_number}: {exc}")

    item_count = len(extract_purchase_items(detail))
    status = "ok" if _has_matching_competition_protocol(purchase_type, protocols) else "missing_final"
    if status != "ok":
        stats["detail_errors"] += 1

    _mark_detail(
        conn,
        purchase_number,
        status=status,
        item_count=item_count,
        protocol_count=protocol_count,
    )
    stats["details"] += 1
    stats["protocols"] += protocol_count
    conn.commit()
    emit(
        f"detail purchase={purchase_number} stage={stage} method={purchase_type} "
        f"items={item_count} protocols={protocol_count} status={status} "
        f"details={stats['details']}"
    )
    return "processed"


def ingest_region_batch(
    conn: sqlite3.Connection,
    client: GosplanClient,
    *,
    region_code: int,
    limit: int,
    pages: int,
    skip: int = 0,
    extra: dict[str, str] | None = None,
    fetch_details: bool = True,
    max_details: int = 100,
    request_delay: float | None = None,
    refresh_details: bool = False,
    protocol_fallback: bool = True,
    emit: Callable[[str], None] = print,
) -> dict[str, int]:
    """Collect a region using final competitive protocols as the detail trigger.

    Every list row is persisted. A costly detail request is made only when the
    row itself advertises a final protocol for an explicitly supported competitive
    procurement method. ``stage`` is intentionally ignored for this decision.
    """
    params = dict(extra or {})
    params["region"] = str(region_code)
    delay = default_request_delay(client.base_url) if request_delay is None else max(0.0, request_delay)

    stats = new_ingest_stats()

    def pause() -> None:
        if delay > 0:
            time.sleep(delay)

    for page in range(pages):
        current_skip = skip + page * limit
        rows = client.get_purchases(limit=limit, skip=current_skip, extra=params)
        pause()
        if not rows:
            break

        wrong_regions = sorted(
            {row.get("region") for row in rows if row.get("region") != region_code}
        )
        if wrong_regions:
            raise RuntimeError(
                "Gosplan did not honor the expected region filter parameter 'region'. "
                f"Requested region={region_code}, response also contained={wrong_regions[:5]}. "
                "Stop before collecting mixed-region data."
            )

        for row in rows:
            process_purchase_row(
                conn,
                client,
                row,
                region_code=region_code,
                stats=stats,
                pause=pause,
                fetch_details=fetch_details,
                max_details=max_details,
                refresh_details=refresh_details,
                protocol_fallback=protocol_fallback,
                emit=emit,
            )

        emit(
            f"page={page + 1} skip={current_skip} rows={len(rows)} "
            f"purchases={stats['purchases']} candidates={stats['detail_candidates']} "
            f"details={stats['details']} protocols={stats['protocols']} "
            f"skip_method={stats['detail_skipped_method']} "
            f"skip_cancelled={stats['detail_skipped_cancelled']} "
            f"skip_no_final={stats['detail_skipped_no_final']}"
        )
        if len(rows) < limit:
            break

    return stats
