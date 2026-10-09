from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import ssl
import time
from typing import Any, Callable

import httpx

RZN_BASE_URL = "https://elk.roszdravnadzor.gov.ru"
RZN_SEARCH_PATH = "/public-gateway/registered-med-product/api/v1/med-product/filter-public"
RZN_NSI_RECORD_PATH = "/public-gateway/nsi-search/public/v1/records/{record_id}"


@dataclass(slots=True)
class RznClient:
    base_url: str = RZN_BASE_URL
    timeout: float = 30.0
    cookie: str | None = None
    max_retries: int = 4
    retry_backoff: float = 2.0
    verify_ssl: bool = True

    def _verify_arg(self) -> bool | ssl.SSLContext:
        if not self.verify_ssl:
            return False
        # httpx uses certifi by default. On Windows that can miss a CA chain that
        # Chrome trusts from the OS certificate store. A stdlib context loads the
        # platform trust store instead, while keeping certificate verification on.
        return ssl.create_default_context()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
            "User-Agent": "procure-radar/0.1 (+public Roszdravnadzor registry client)",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            headers=self._headers(),
            verify=self._verify_arg(),
        ) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = client.request(method, path, params=params, json=json_body)
                except httpx.ConnectError as exc:
                    if "CERTIFICATE_VERIFY_FAILED" in str(exc) and self.verify_ssl:
                        raise RuntimeError(
                            "Roszdravnadzor TLS certificate chain is not trusted by Python. "
                            "The client already tried the operating-system CA store. "
                            "If the site opens normally in your browser, retry this command with "
                            "--insecure to disable TLS verification only for the RZN request."
                        ) from exc
                    if attempt >= self.max_retries:
                        raise
                    time.sleep(min(self.retry_backoff * (attempt + 1), 20.0))
                    continue
                except httpx.RequestError:
                    if attempt >= self.max_retries:
                        raise
                    time.sleep(min(self.retry_backoff * (attempt + 1), 20.0))
                    continue

                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt >= self.max_retries:
                        response.raise_for_status()
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else self.retry_backoff * (attempt + 1)
                    except ValueError:
                        delay = self.retry_backoff * (attempt + 1)
                    time.sleep(min(max(delay, 0.25), 30.0))
                    continue

                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError(
                        f"Expected object response from Roszdravnadzor, got {type(payload).__name__}"
                    )
                return payload
        raise RuntimeError("unreachable")

    def get_nsi_record(self, record_id: str | int) -> dict[str, Any]:
        rendered = str(record_id).strip()
        if not rendered:
            raise ValueError("record_id must not be empty")
        return self._request_json("GET", RZN_NSI_RECORD_PATH.format(record_id=rendered))

    def search_med_products(
        self,
        *,
        text_search: str = "",
        legal_system: str = "RUSSIA",
        page: int = 0,
        size: int = 100,
        status_ids: list[int] | tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        if page < 0:
            raise ValueError("page must be >= 0")
        if not 1 <= size <= 100:
            raise ValueError("size must be between 1 and 100")

        body: dict[str, Any] = {
            "legalSystem": legal_system,
            "textSearch": text_search,
        }
        if status_ids:
            body["statusIds"] = [int(status_id) for status_id in status_ids]
        payload = self._request_json(
            "POST",
            RZN_SEARCH_PATH,
            params={"page": page, "size": size},
            json_body=body,
        )
        content = payload.get("content")
        if content is not None and not isinstance(content, list):
            raise TypeError("Roszdravnadzor response.content must be an array")
        return payload


def _party_fields(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    return _text(value.get("name")), _text(value.get("actualAddress"))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def upsert_med_product(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    external_id = _int(row.get("id"))
    if external_id is None:
        raise ValueError("Roszdravnadzor med-product row is missing integer id")

    status = row.get("status") if isinstance(row.get("status"), dict) else {}
    producer_name, producer_address = _party_fields(row.get("producer"))
    declarant_name, declarant_address = _party_fields(row.get("declarant"))
    representative_name, representative_address = _party_fields(row.get("representative"))

    conn.execute(
        """
        INSERT INTO rzn_med_products(
            external_id, application_id,
            status_id, status_code, status_name,
            legal_system, registration_number, registration_date, end_date,
            name,
            producer_name, producer_address,
            declarant_name, declarant_address,
            representative_name, representative_address,
            parent_med_product_id, frnsi_id,
            date_registration_ru, has_changes, date_start,
            acceptance_countries_json, production_sites_json, raw_json, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(external_id) DO UPDATE SET
            application_id=excluded.application_id,
            status_id=excluded.status_id,
            status_code=excluded.status_code,
            status_name=excluded.status_name,
            legal_system=excluded.legal_system,
            registration_number=excluded.registration_number,
            registration_date=excluded.registration_date,
            end_date=excluded.end_date,
            name=excluded.name,
            producer_name=excluded.producer_name,
            producer_address=excluded.producer_address,
            declarant_name=excluded.declarant_name,
            declarant_address=excluded.declarant_address,
            representative_name=excluded.representative_name,
            representative_address=excluded.representative_address,
            parent_med_product_id=excluded.parent_med_product_id,
            frnsi_id=excluded.frnsi_id,
            date_registration_ru=excluded.date_registration_ru,
            has_changes=excluded.has_changes,
            date_start=excluded.date_start,
            acceptance_countries_json=excluded.acceptance_countries_json,
            production_sites_json=excluded.production_sites_json,
            raw_json=excluded.raw_json,
            updated_at=datetime('now')
        """,
        (
            external_id,
            _int(row.get("applicationId")),
            _int(status.get("id")),
            _text(status.get("code")),
            _text(status.get("name")),
            _text(row.get("legalSystem")),
            _text(row.get("noRu")),
            _text(row.get("dateRu")),
            _text(row.get("endDateRu")),
            _text(row.get("name")),
            producer_name,
            producer_address,
            declarant_name,
            declarant_address,
            representative_name,
            representative_address,
            _int(row.get("parentMedProductId")),
            _text(row.get("frnsiId")),
            _text(row.get("dateRegistrationRu")),
            1 if row.get("hasChanges") is True else 0 if row.get("hasChanges") is False else None,
            _text(row.get("dateStart")),
            json.dumps(row.get("acceptanceCountriesIds") or [], ensure_ascii=False, separators=(",", ":")),
            json.dumps(row.get("productionSites") or [], ensure_ascii=False, separators=(",", ":")),
            json.dumps(row, ensure_ascii=False, separators=(",", ":")),
        ),
    )

    local = conn.execute(
        "SELECT id FROM rzn_med_products WHERE external_id=?",
        (external_id,),
    ).fetchone()
    assert local is not None
    local_id = int(local["id"])

    conn.execute("DELETE FROM rzn_med_product_classifier_ids WHERE med_product_id=?", (local_id,))
    classifier_ids = row.get("nomClassifierMedicalRfIds")
    if isinstance(classifier_ids, list):
        for classifier_id in classifier_ids:
            rendered = _text(classifier_id)
            if rendered:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO rzn_med_product_classifier_ids(med_product_id, classifier_external_id)
                    VALUES(?,?)
                    """,
                    (local_id, rendered),
                )
    return local_id


def sync_registry(
    conn: sqlite3.Connection,
    client: RznClient,
    *,
    legal_system: str = "RUSSIA",
    text_search: str = "",
    page_size: int = 100,
    pages: int | None = 10,
    request_delay: float = 0.25,
    active_only: bool = False,
    restart: bool = False,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if pages is not None and pages < 1:
        raise ValueError("pages must be >= 1 or None")
    if request_delay < 0:
        raise ValueError("request_delay must be >= 0")

    status_ids = (1,) if active_only else None
    status_key = "active" if active_only else "all"
    key = f"{legal_system}|{text_search}|status={status_key}"
    if restart:
        conn.execute("DELETE FROM rzn_sync_state WHERE sync_key=?", (key,))
        conn.commit()

    state = conn.execute("SELECT * FROM rzn_sync_state WHERE sync_key=?", (key,)).fetchone()
    if state is not None and int(state["completed"]):
        return {
            "status": "complete",
            "stop_reason": "already_complete",
            "next_page": int(state["next_page"]),
            "total_elements": state["total_elements"],
            "rows": int(state["rows_seen"]),
            "pages": int(state["pages_scanned"]),
        }

    next_page = int(state["next_page"]) if state is not None else 0
    pages_scanned = int(state["pages_scanned"]) if state is not None else 0
    rows_seen = int(state["rows_seen"]) if state is not None else 0
    total_elements = int(state["total_elements"]) if state is not None and state["total_elements"] is not None else None

    pages_this_run = 0
    while pages is None or pages_this_run < pages:
        if pages_this_run and request_delay:
            time.sleep(request_delay)
        payload = client.search_med_products(
            text_search=text_search,
            legal_system=legal_system,
            page=next_page,
            size=page_size,
            status_ids=status_ids,
        )
        rows = [row for row in payload.get("content", []) if isinstance(row, dict)]
        response_total = _int(payload.get("totalElements"))
        if response_total is not None:
            total_elements = response_total

        for row in rows:
            upsert_med_product(conn, row)

        rows_seen += len(rows)
        pages_scanned += 1
        pages_this_run += 1
        last = bool(payload.get("last")) or not rows
        current_page = _int(payload.get("number"))
        if current_page is None:
            current_page = next_page
        next_page = current_page + 1

        conn.execute(
            """
            INSERT INTO rzn_sync_state(
                sync_key, legal_system, text_search, page_size, next_page,
                total_elements, pages_scanned, rows_seen, completed, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(sync_key) DO UPDATE SET
                legal_system=excluded.legal_system,
                text_search=excluded.text_search,
                page_size=excluded.page_size,
                next_page=excluded.next_page,
                total_elements=excluded.total_elements,
                pages_scanned=excluded.pages_scanned,
                rows_seen=excluded.rows_seen,
                completed=excluded.completed,
                updated_at=datetime('now')
            """,
            (
                key,
                legal_system,
                text_search,
                page_size,
                next_page,
                total_elements,
                pages_scanned,
                rows_seen,
                1 if last else 0,
            ),
        )
        conn.commit()

        emit(
            "rzn page={page} rows={rows} total={total} stored={stored} next_page={next_page}".format(
                page=current_page,
                rows=len(rows),
                total=total_elements if total_elements is not None else "?",
                stored=conn.execute("SELECT COUNT(*) FROM rzn_med_products").fetchone()[0],
                next_page=next_page,
            )
        )
        if last:
            return {
                "status": "complete",
                "stop_reason": "last_page",
                "next_page": next_page,
                "total_elements": total_elements,
                "rows": rows_seen,
                "pages": pages_scanned,
            }

    return {
        "status": "paused",
        "stop_reason": "page_budget",
        "next_page": next_page,
        "total_elements": total_elements,
        "rows": rows_seen,
        "pages": pages_scanned,
    }


def upsert_nsi_record(conn: sqlite3.Connection, row: dict[str, Any]) -> str:
    record_id = _text(row.get("recordId"))
    if record_id is None:
        raise ValueError("Roszdravnadzor NSI row is missing recordId")
    attrs = row.get("attributeSet") if isinstance(row.get("attributeSet"), dict) else {}
    conn.execute(
        """
        INSERT INTO rzn_nsi_records(
            classifier_external_id, catalog_code, status_code, actual_start_dt,
            create_dttm, modify_dttm, code, name, raw_json, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(classifier_external_id) DO UPDATE SET
            catalog_code=excluded.catalog_code,
            status_code=excluded.status_code,
            actual_start_dt=excluded.actual_start_dt,
            create_dttm=excluded.create_dttm,
            modify_dttm=excluded.modify_dttm,
            code=excluded.code,
            name=excluded.name,
            raw_json=excluded.raw_json,
            updated_at=datetime('now')
        """,
        (
            record_id,
            _text(row.get("catalogCode")),
            _text(row.get("statusCode")),
            _text(row.get("actualStartDt")),
            _text(row.get("createDttm")),
            _text(row.get("modifyDttm")),
            _text(attrs.get("code")),
            _text(attrs.get("name")),
            json.dumps(row, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return record_id


def sync_nsi_records(
    conn: sqlite3.Connection,
    client: RznClient,
    *,
    limit: int | None = 100,
    request_delay: float = 0.25,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1 or None")
    if request_delay < 0:
        raise ValueError("request_delay must be >= 0")

    pending = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT l.classifier_external_id
            FROM rzn_med_product_classifier_ids l
            LEFT JOIN rzn_nsi_records n
              ON n.classifier_external_id=l.classifier_external_id
            WHERE n.classifier_external_id IS NULL
            ORDER BY l.classifier_external_id
            """
        )
    ]
    total_pending = len(pending)
    selected = pending if limit is None else pending[:limit]
    resolved = 0
    for idx, record_id in enumerate(selected, 1):
        if idx > 1 and request_delay:
            time.sleep(request_delay)
        payload = client.get_nsi_record(record_id)
        upsert_nsi_record(conn, payload)
        conn.commit()
        resolved += 1
        if idx == 1 or idx % 25 == 0 or idx == len(selected):
            attrs = payload.get("attributeSet") if isinstance(payload.get("attributeSet"), dict) else {}
            emit(
                f"rzn-nsi resolved={resolved}/{len(selected)} pending_before={total_pending} "
                f"id={record_id} catalog={payload.get('catalogCode')!r} "
                f"code={attrs.get('code')!r} name={attrs.get('name')!r}"
            )

    remaining = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT l.classifier_external_id)
            FROM rzn_med_product_classifier_ids l
            LEFT JOIN rzn_nsi_records n
              ON n.classifier_external_id=l.classifier_external_id
            WHERE n.classifier_external_id IS NULL
            """
        ).fetchone()[0]
    )
    return {
        "resolved_this_run": resolved,
        "pending_before": total_pending,
        "pending_after": remaining,
        "complete": remaining == 0,
    }


def okpd2_registry_stats(
    conn: sqlite3.Connection,
    okpd2_code: str,
    *,
    active_only: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    code = okpd2_code.strip()
    if not code:
        raise ValueError("okpd2_code must not be empty")
    status_clause = "AND p.status_id=1" if active_only else ""
    params = (code,)
    summary = conn.execute(
        f"""
        SELECT
            COUNT(DISTINCT p.id) AS products,
            COUNT(DISTINCT NULLIF(p.producer_name, '')) AS producers,
            COUNT(DISTINCT NULLIF(p.representative_name, '')) AS representatives
        FROM rzn_med_products p
        JOIN rzn_med_product_classifier_ids l ON l.med_product_id=p.id
        JOIN rzn_nsi_records n ON n.classifier_external_id=l.classifier_external_id
        WHERE n.catalog_code='okpd2Code' AND n.code=? {status_clause}
        """,
        params,
    ).fetchone()
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT DISTINCT p.external_id, p.registration_number, p.registration_date,
                   p.status_name, p.name, p.producer_name, p.representative_name
            FROM rzn_med_products p
            JOIN rzn_med_product_classifier_ids l ON l.med_product_id=p.id
            JOIN rzn_nsi_records n ON n.classifier_external_id=l.classifier_external_id
            WHERE n.catalog_code='okpd2Code' AND n.code=? {status_clause}
            ORDER BY COALESCE(p.registration_date, '') DESC, p.external_id DESC
            LIMIT ?
            """,
            (code, limit),
        )
    ]
    producers = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT p.producer_name AS producer, COUNT(DISTINCT p.id) AS products
            FROM rzn_med_products p
            JOIN rzn_med_product_classifier_ids l ON l.med_product_id=p.id
            JOIN rzn_nsi_records n ON n.classifier_external_id=l.classifier_external_id
            WHERE n.catalog_code='okpd2Code' AND n.code=? {status_clause}
              AND p.producer_name IS NOT NULL AND p.producer_name<>''
            GROUP BY p.producer_name
            ORDER BY products DESC, producer
            LIMIT ?
            """,
            (code, limit),
        )
    ]
    return {
        "okpd2_code": code,
        "active_only": active_only,
        "products": int(summary["products"] or 0),
        "producers": int(summary["producers"] or 0),
        "representatives": int(summary["representatives"] or 0),
        "sample_products": rows,
        "top_producers": producers,
    }


def registry_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = int(conn.execute("SELECT COUNT(*) FROM rzn_med_products").fetchone()[0])
    classifier_links = int(conn.execute("SELECT COUNT(*) FROM rzn_med_product_classifier_ids").fetchone()[0])
    producer_count = int(
        conn.execute(
            "SELECT COUNT(DISTINCT producer_name) FROM rzn_med_products WHERE producer_name IS NOT NULL"
        ).fetchone()[0]
    )
    representative_count = int(
        conn.execute(
            "SELECT COUNT(DISTINCT representative_name) FROM rzn_med_products WHERE representative_name IS NOT NULL"
        ).fetchone()[0]
    )
    statuses = {
        str(row["status_name"] or row["status_code"] or "unknown"): int(row["n"])
        for row in conn.execute(
            """
            SELECT status_name, status_code, COUNT(*) AS n
            FROM rzn_med_products
            GROUP BY status_name, status_code
            ORDER BY n DESC
            """
        )
    }
    legal_systems = {
        str(row["legal_system"] or "unknown"): int(row["n"])
        for row in conn.execute(
            """
            SELECT legal_system, COUNT(*) AS n
            FROM rzn_med_products
            GROUP BY legal_system
            ORDER BY n DESC
            """
        )
    }
    sync = [dict(row) for row in conn.execute("SELECT * FROM rzn_sync_state ORDER BY updated_at DESC")]
    nsi_records = int(conn.execute("SELECT COUNT(*) FROM rzn_nsi_records").fetchone()[0])
    unresolved_nsi = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT l.classifier_external_id)
            FROM rzn_med_product_classifier_ids l
            LEFT JOIN rzn_nsi_records n ON n.classifier_external_id=l.classifier_external_id
            WHERE n.classifier_external_id IS NULL
            """
        ).fetchone()[0]
    )
    nsi_catalogs = {
        str(row["catalog_code"] or "unknown"): int(row["n"])
        for row in conn.execute(
            """
            SELECT catalog_code, COUNT(*) AS n
            FROM rzn_nsi_records
            GROUP BY catalog_code
            ORDER BY n DESC
            """
        )
    }
    return {
        "products": total,
        "classifier_links": classifier_links,
        "nsi_records": nsi_records,
        "unresolved_nsi": unresolved_nsi,
        "nsi_catalogs": nsi_catalogs,
        "producers": producer_count,
        "representatives": representative_count,
        "statuses": statuses,
        "legal_systems": legal_systems,
        "sync": sync,
    }
