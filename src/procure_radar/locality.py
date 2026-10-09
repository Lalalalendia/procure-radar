from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .organization import normalize_inn

_ADDRESS_KEYS = {
    "address",
    "postaladdress",
    "registrationaddress",
    "legaladdress",
    "factaddress",
    "actualaddress",
    "locationaddress",
    "organizationaddress",
    "customeraddress",
    "placeofstay",
}


def _key_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    return text or None


def _normalize_location_text(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return " ".join(text.split())


def locality_matches(value: Any, aliases: Iterable[str]) -> bool:
    haystack = f" {_normalize_location_text(value)} "
    if haystack == "  ":
        return False
    for alias in aliases:
        needle = _normalize_location_text(alias)
        if needle and f" {needle} " in haystack:
            return True
    return False


def extract_purchase_locality_evidence(
    payload: Any,
    *,
    aliases: Iterable[str],
) -> list[dict[str, Any]]:
    """Return structured evidence that the procurement itself belongs to a locality.

    Customer legal addresses are useful but not sufficient: regional buyers can run a
    procurement whose delivery place, budget/OKTMO or named customer is in the city.
    Only semantically strong fields are accepted; attachment names and arbitrary
    document text are deliberately ignored.
    """
    aliases = [alias for alias in aliases if _normalize_location_text(alias)]
    if not aliases:
        return []

    best: dict[str, tuple[float, str]] = {}

    def add(source: str, confidence: float, value: Any) -> None:
        text = _clean_text(value)
        if text is None or not locality_matches(text, aliases):
            return
        current = best.get(source)
        if current is None or confidence > current[0] or (
            confidence == current[0] and len(text) > len(current[1])
        ):
            best[source] = (confidence, text)

    def walk(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                token = _key_token(key)
                next_path = (*path, token)
                if isinstance(value, str):
                    path_tokens = set(next_path)
                    joined = ".".join(next_path)
                    if (
                        token.endswith("address")
                        or "deliveryplacesinfo" in path_tokens
                        or "bygarinfo" in path_tokens
                    ):
                        add("structured_address", 1.0, value)
                    elif "oktmo" in joined or "budgetinfo" in path_tokens:
                        add("structured_jurisdiction", 0.99, value)
                    elif token in {
                        "fullname",
                        "customerfullname",
                        "organizationfullname",
                    } and any(
                        part in {
                            "customer",
                            "customerrequirementsinfo",
                            "customerrequirementinfo",
                            "customerquantities",
                            "customerquantity",
                        }
                        for part in path_tokens
                    ):
                        add("structured_customer_name", 0.97, value)
                    elif token == "addinfo" and any(
                        "responsible" in part for part in path_tokens
                    ):
                        add("structured_responsible_contact", 0.95, value)
                    elif token in {"objectinfo", "purchaseobjectinfo"}:
                        add("purchase_object", 0.90, value)
                walk(value, next_path)
        elif isinstance(node, list):
            for child in node:
                walk(child, path)

    walk(payload)
    return [
        {"source": source, "confidence": confidence, "evidence": evidence}
        for source, (confidence, evidence) in sorted(
            best.items(), key=lambda item: (-item[1][0], item[0])
        )
    ]


def refresh_purchase_locality_memberships(
    conn: sqlite3.Connection,
    *,
    locality_key: str,
    locality_name: str,
    aliases: Iterable[str],
    region_code: int | None = None,
) -> dict[str, int]:
    """Rebuild structured purchase-level locality evidence from cached raw cards."""
    aliases = [alias for alias in aliases if _normalize_location_text(alias)] or [
        locality_name
    ]
    conn.execute("DELETE FROM purchase_localities WHERE locality_key=?", (locality_key,))

    where = ["1=1"]
    params: list[Any] = []
    if region_code is not None:
        where.append("p.region_code=?")
        params.append(int(region_code))
    rows = conn.execute(
        f"""
        SELECT p.id, p.purchase_number, r.payload_json
        FROM purchases p
        JOIN raw_documents r ON r.id=p.raw_document_id
        WHERE {' AND '.join(where)}
        """,
        tuple(params),
    )

    purchases_matched = evidence_rows = 0
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        evidence = extract_purchase_locality_evidence(payload, aliases=aliases)
        if not evidence:
            continue
        purchases_matched += 1
        for item in evidence:
            conn.execute(
                """
                INSERT INTO purchase_localities(
                    purchase_id, locality_key, locality_name, source, confidence, evidence
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(purchase_id, locality_key, source) DO UPDATE SET
                    locality_name=excluded.locality_name,
                    confidence=excluded.confidence,
                    evidence=excluded.evidence,
                    updated_at=datetime('now')
                """,
                (
                    int(row["id"]),
                    locality_key,
                    locality_name,
                    item["source"],
                    float(item["confidence"]),
                    item["evidence"],
                ),
            )
            evidence_rows += 1
    conn.commit()
    strong = conn.execute(
        """
        SELECT COUNT(DISTINCT purchase_id)
        FROM purchase_localities
        WHERE locality_key=? AND confidence>=0.9
        """,
        (locality_key,),
    ).fetchone()[0]
    return {
        "purchase_matches": purchases_matched,
        "purchase_evidence_rows": evidence_rows,
        "strong_purchase_matches": int(strong),
    }


def extract_party_addresses(
    payload: Any,
    *,
    wanted_inns: Iterable[str] | None = None,
) -> dict[str, str]:
    """Extract an address only when it is colocated with an INN in the same object.

    Full EIS documents vary by version. This intentionally accepts common English
    transliterations used by Gosplan/EIS JSON but refuses free-floating addresses,
    which keeps locality membership evidence tied to the party being classified.
    """
    wanted = None
    if wanted_inns is not None:
        wanted = {inn for value in wanted_inns if (inn := normalize_inn(value))}

    result: dict[str, str] = {}

    def direct_inns(node: dict[str, Any]) -> set[str]:
        inns: set[str] = set()
        for key, value in node.items():
            token = _key_token(key)
            if token == "inn" or token.endswith("inn"):
                inn = normalize_inn(value)
                if inn:
                    inns.add(inn)
        return inns

    def direct_address(node: dict[str, Any]) -> str | None:
        candidates: list[str] = []
        for key, value in node.items():
            token = _key_token(key)
            if token in _ADDRESS_KEYS or token.endswith("address"):
                text = _clean_text(value)
                if text:
                    candidates.append(text)
        if not candidates:
            return None
        return max(candidates, key=len)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            inns = direct_inns(node)
            address = direct_address(node)
            if address:
                for inn in inns:
                    if wanted is None or inn in wanted:
                        current = result.get(inn)
                        if current is None or len(address) > len(current):
                            result[inn] = address
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return result


def enrich_party_addresses(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any],
    inns: Iterable[str],
) -> dict[str, int]:
    wanted = {inn for value in inns if (inn := normalize_inn(value))}
    addresses = extract_party_addresses(payload, wanted_inns=wanted)
    updated = 0
    for inn, address in addresses.items():
        cur = conn.execute(
            """
            UPDATE organizations
            SET address=CASE
                    WHEN address IS NULL OR trim(address)='' THEN ?
                    ELSE address
                END,
                updated_at=datetime('now')
            WHERE inn=?
            """,
            (address, inn),
        )
        updated += max(0, int(cur.rowcount))
    return {"addresses_extracted": len(addresses), "organizations_touched": updated}


def _upsert_membership(
    conn: sqlite3.Connection,
    *,
    inn: str,
    locality_key: str,
    locality_name: str,
    source: str,
    confidence: float,
    evidence: str | None,
) -> None:
    normalized = normalize_inn(inn)
    if normalized is None:
        return
    conn.execute("INSERT OR IGNORE INTO organizations(inn) VALUES (?)", (normalized,))
    conn.execute(
        """
        INSERT INTO organization_localities(
            inn, locality_key, locality_name, source, confidence, evidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(inn, locality_key, source) DO UPDATE SET
            locality_name=excluded.locality_name,
            confidence=excluded.confidence,
            evidence=excluded.evidence,
            updated_at=datetime('now')
        """,
        (normalized, locality_key, locality_name, source, float(confidence), evidence),
    )


def refresh_locality_memberships(
    conn: sqlite3.Connection,
    *,
    locality_key: str,
    locality_name: str,
    aliases: Iterable[str],
    region_code: int | None = None,
    manual_inns: Iterable[str] = (),
) -> dict[str, int]:
    aliases = [alias for alias in aliases if _normalize_location_text(alias)]
    if not aliases:
        aliases = [locality_name]

    # Auto evidence is recalculated from the canonical organization layer. Manual
    # evidence is persistent and intentionally survives refreshes.
    conn.execute(
        "DELETE FROM organization_localities WHERE locality_key=? AND source IN ('organization_address','organization_name')",
        (locality_key,),
    )

    where = ["1=1"]
    params: list[Any] = []
    if region_code is not None:
        where.append("(region_code=? OR region_code IS NULL)")
        params.append(int(region_code))
    rows = conn.execute(
        f"SELECT inn, name, address, region_code FROM organizations WHERE {' AND '.join(where)}",
        tuple(params),
    )

    address_matches = name_matches = manual_matches = 0
    for row in rows:
        inn = str(row["inn"])
        address = row["address"]
        name = row["name"]
        if locality_matches(address, aliases):
            _upsert_membership(
                conn,
                inn=inn,
                locality_key=locality_key,
                locality_name=locality_name,
                source="organization_address",
                confidence=1.0,
                evidence=str(address),
            )
            address_matches += 1
        elif locality_matches(name, aliases):
            # Company names can contain a locality without proving legal location,
            # so keep this as secondary evidence rather than treating it as exact.
            _upsert_membership(
                conn,
                inn=inn,
                locality_key=locality_key,
                locality_name=locality_name,
                source="organization_name",
                confidence=0.65,
                evidence=str(name),
            )
            name_matches += 1

    for value in manual_inns:
        inn = normalize_inn(value)
        if inn is None:
            continue
        _upsert_membership(
            conn,
            inn=inn,
            locality_key=locality_key,
            locality_name=locality_name,
            source="manual_allowlist",
            confidence=1.0,
            evidence="manual allowlist",
        )
        manual_matches += 1

    conn.commit()
    purchase_stats = refresh_purchase_locality_memberships(
        conn,
        locality_key=locality_key,
        locality_name=locality_name,
        aliases=aliases,
        region_code=region_code,
    )
    total = conn.execute(
        "SELECT COUNT(DISTINCT inn) FROM organization_localities WHERE locality_key=?",
        (locality_key,),
    ).fetchone()[0]
    strong = conn.execute(
        "SELECT COUNT(DISTINCT inn) FROM organization_localities WHERE locality_key=? AND confidence>=0.9",
        (locality_key,),
    ).fetchone()[0]
    return {
        "address_matches": address_matches,
        "name_matches": name_matches,
        "manual_matches": manual_matches,
        "total_members": int(total),
        "strong_members": int(strong),
        **purchase_stats,
    }


def read_inn_file(path: str | Path | None) -> list[str]:
    if path is None:
        return []
    result: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        inn = normalize_inn(line)
        if inn:
            result.append(inn)
    return result


def locality_purchases(
    conn: sqlite3.Connection,
    *,
    locality_key: str,
    min_confidence: float = 0.9,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH city_inns AS (
            SELECT inn, MAX(confidence) AS confidence
            FROM organization_localities
            WHERE locality_key=?
            GROUP BY inn
            HAVING MAX(confidence)>=?
        ),
        customer_local AS (
            SELECT pp.purchase_id,
                   MAX(ci.confidence) AS confidence,
                   COUNT(DISTINCT pp.inn) AS local_customer_count,
                   MIN(pp.inn) AS local_customer_inn
            FROM purchase_parties pp
            JOIN city_inns ci ON ci.inn=pp.inn
            WHERE pp.role='customer'
            GROUP BY pp.purchase_id
        ),
        purchase_local AS (
            SELECT purchase_id, MAX(confidence) AS confidence
            FROM purchase_localities
            WHERE locality_key=?
            GROUP BY purchase_id
            HAVING MAX(confidence)>=?
        ),
        customer_rollup AS (
            SELECT purchase_id,
                   COUNT(DISTINCT inn) AS customer_count,
                   MIN(inn) AS sole_customer_inn
            FROM purchase_parties
            WHERE role='customer'
            GROUP BY purchase_id
        ),
        candidates AS (
            SELECT p.id AS purchase_id,
                   CASE
                     WHEN cl.purchase_id IS NOT NULL AND cl.local_customer_count=1
                       THEN cl.local_customer_inn
                     WHEN cl.purchase_id IS NULL AND cr.customer_count=1
                       THEN cr.sole_customer_inn
                     ELSE NULL
                   END AS customer_inn,
                   CASE
                     WHEN COALESCE(cl.confidence, 0) >= COALESCE(pl.confidence, 0)
                       THEN cl.confidence
                     ELSE pl.confidence
                   END AS locality_confidence,
                   CASE
                     WHEN COALESCE(cl.confidence, 0) >= COALESCE(pl.confidence, 0)
                          AND cl.purchase_id IS NOT NULL
                       THEN 'organization'
                     ELSE 'purchase'
                   END AS locality_source
            FROM purchases p
            LEFT JOIN customer_local cl ON cl.purchase_id=p.id
            LEFT JOIN purchase_local pl ON pl.purchase_id=p.id
            LEFT JOIN customer_rollup cr ON cr.purchase_id=p.id
            WHERE cl.purchase_id IS NOT NULL OR pl.purchase_id IS NOT NULL
        )
        SELECT
            p.purchase_number,
            p.published_at,
            p.collecting_finished_at,
            p.max_price,
            p.currency_code,
            p.object_info,
            p.purchase_type,
            p.region_code,
            p.stage,
            c.customer_inn,
            o.name AS customer_name,
            o.address AS customer_address,
            c.locality_confidence,
            c.locality_source
        FROM candidates c
        JOIN purchases p ON p.id=c.purchase_id
        LEFT JOIN organizations o ON o.inn=c.customer_inn
        ORDER BY COALESCE(p.published_at, p.updated_at, '') DESC, p.purchase_number DESC
        LIMIT ?
        """,
        (
            locality_key,
            float(min_confidence),
            locality_key,
            float(min_confidence),
            int(limit),
        ),
    )
    return [dict(row) for row in rows]

def export_locality_purchases(
    rows: list[dict[str, Any]],
    *,
    json_path: str | Path | None = None,
    csv_path: str | Path | None = None,
) -> None:
    if json_path is not None:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if csv_path is not None:
        path = Path(csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "purchase_number",
            "published_at",
            "collecting_finished_at",
            "max_price",
            "currency_code",
            "object_info",
            "purchase_type",
            "region_code",
            "stage",
            "customer_inn",
            "customer_name",
            "customer_address",
            "locality_confidence",
            "locality_source",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)


def enrich_purchase_identities(
    conn: sqlite3.Connection,
    client: Any,
    *,
    region_code: int,
    max_requests: int = 20,
    request_delay: float | None = None,
    refresh: bool = False,
) -> dict[str, int]:
    """Fetch recent full purchase cards once for identity and locality evidence.

    This is deliberately separate from competition/protocol enrichment: locality
    classification is useful for brand-new purchases and for regional buyers whose
    legal address is outside the delivery city. A journal prevents repeated fetches.
    """
    import time
    import httpx

    from .bulk import default_request_delay
    from .ingest import ingest_purchase

    delay = default_request_delay(client.base_url) if request_delay is None else max(0.0, request_delay)
    params: list[Any] = [int(region_code)]
    journal_clause = ""
    if not refresh:
        journal_clause = "AND p.purchase_number NOT IN (SELECT purchase_number FROM purchase_identity_fetches)"

    candidates = conn.execute(
        f"""
        SELECT p.purchase_number, MAX(COALESCE(p.published_at, p.updated_at, '')) AS sort_date
        FROM purchases p
        WHERE p.region_code=?
          {journal_clause}
        GROUP BY p.purchase_number
        ORDER BY sort_date DESC, p.purchase_number DESC
        LIMIT ?
        """,
        (*params, max(0, int(max_requests))),
    ).fetchall()

    stats = {
        "candidates": len(candidates),
        "requests": 0,
        "ok": 0,
        "no_address": 0,
        "not_found": 0,
        "invalid": 0,
    }
    for candidate in candidates:
        purchase_number = str(candidate["purchase_number"])
        try:
            detail = client.get_purchase(purchase_number)
            stats["requests"] += 1
            if delay > 0:
                time.sleep(delay)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            conn.execute(
                """
                INSERT INTO purchase_identity_fetches(purchase_number, status)
                VALUES (?, '404')
                ON CONFLICT(purchase_number) DO UPDATE SET fetched_at=datetime('now'), status='404'
                """,
                (purchase_number,),
            )
            conn.commit()
            stats["not_found"] += 1
            continue

        if not isinstance(detail, dict):
            conn.execute(
                """
                INSERT INTO purchase_identity_fetches(purchase_number, status)
                VALUES (?, 'invalid')
                ON CONFLICT(purchase_number) DO UPDATE SET fetched_at=datetime('now'), status='invalid'
                """,
                (purchase_number,),
            )
            conn.commit()
            stats["invalid"] += 1
            continue
        if detail.get("region") not in (None, region_code):
            raise RuntimeError(
                f"identity detail region mismatch for {purchase_number}: "
                f"{detail.get('region')} != {region_code}"
            )

        ingest_purchase(conn, detail)
        customer_rows = conn.execute(
            """
            SELECT o.inn, o.address
            FROM purchases p
            JOIN purchase_parties pp ON pp.purchase_id=p.id AND pp.role='customer'
            LEFT JOIN organizations o ON o.inn=pp.inn
            WHERE p.purchase_number=?
            """,
            (purchase_number,),
        ).fetchall()
        customer_count = len(customer_rows)
        addressed = sum(1 for row in customer_rows if str(row["address"] or "").strip())
        status = "ok" if addressed else "no_address"
        conn.execute(
            """
            INSERT INTO purchase_identity_fetches(
                purchase_number, status, customer_count, addressed_customers
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(purchase_number) DO UPDATE SET
                fetched_at=datetime('now'), status=excluded.status,
                customer_count=excluded.customer_count,
                addressed_customers=excluded.addressed_customers
            """,
            (purchase_number, status, customer_count, addressed),
        )
        conn.commit()
        stats[status] += 1
    return stats
