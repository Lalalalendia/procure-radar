from __future__ import annotations

from collections import defaultdict
import json
import re
import sqlite3
import time
from typing import Any, Callable, Iterable


_INN_KEY_TOKENS = {
    "inn",
    "customerinn",
    "supplierinn",
    "participantinn",
    "organizationinn",
    "legalentityinn",
    "taxpayerinn",
}
_NAME_PRIORITIES = {
    "fullname": 100,
    "organizationfullname": 100,
    "legalentityfullname": 100,
    "supplierfullname": 100,
    "customerfullname": 100,
    "organizationname": 90,
    "legalentityname": 90,
    "suppliername": 90,
    "customername": 90,
    "shortname": 80,
    "nameshort": 80,
    "name": 50,
}


def _key_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _valid_inn(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) not in (10, 12) or not text.isdigit():
        return None
    return text


def _clean_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    if not text or text.isdigit() or len(text) < 2:
        return None
    return text


def _direct_name(node: dict[str, Any]) -> tuple[str | None, int]:
    best_name: str | None = None
    best_priority = -1
    for key, value in node.items():
        token = _key_token(key)
        priority = _NAME_PRIORITIES.get(token)
        if priority is None:
            continue
        name = _clean_name(value)
        if name is not None and priority > best_priority:
            best_name = name
            best_priority = priority
    return best_name, best_priority


def _direct_inns(node: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key, value in node.items():
        token = _key_token(key)
        if token not in _INN_KEY_TOKENS and not token.endswith("inn"):
            continue
        inn = _valid_inn(value)
        if inn:
            result.add(inn)
    return result


def extract_party_names(
    payload: Any,
    *,
    wanted_inns: Iterable[str] | None = None,
) -> dict[str, str]:
    """Extract organization names from aggregate or embedded EIS JSON.

    The Gosplan aggregate endpoints primarily expose INNs. Full/raw documents
    can additionally contain party dictionaries (for example ``INN`` plus
    ``fullName``).  We only accept a name from the same dictionary as an INN,
    or from explicit top-level customer/supplier name fields.  When
    ``wanted_inns`` is supplied, unrelated organizations embedded in the same
    document are ignored.
    """
    wanted = None
    if wanted_inns is not None:
        wanted = {inn for value in wanted_inns if (inn := _valid_inn(value))}

    selected: dict[str, tuple[int, str]] = {}

    def add(inn_value: Any, name_value: Any, priority: int) -> None:
        inn = _valid_inn(inn_value)
        name = _clean_name(name_value)
        if inn is None or name is None or (wanted is not None and inn not in wanted):
            return
        current = selected.get(inn)
        if current is None or priority > current[0] or (
            priority == current[0] and len(name) > len(current[1])
        ):
            selected[inn] = (priority, name)

    if isinstance(payload, dict):
        customer = payload.get("customer")
        if not isinstance(customer, (dict, list)):
            for key in ("customer_name", "customerName", "customer_full_name", "customerFullName"):
                if key in payload:
                    add(customer, payload.get(key), 95)

        suppliers = payload.get("suppliers")
        supplier_names = (
            payload.get("supplier_names")
            or payload.get("supplierNames")
            or payload.get("suppliers_names")
            or payload.get("suppliersNames")
        )
        if isinstance(suppliers, list) and isinstance(supplier_names, list):
            for supplier, name in zip(suppliers, supplier_names):
                if not isinstance(supplier, (dict, list)):
                    add(supplier, name, 90)
        if isinstance(supplier_names, dict):
            for supplier, name in supplier_names.items():
                add(supplier, name, 90)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name, priority = _direct_name(node)
            if name is not None:
                for inn in _direct_inns(node):
                    add(inn, name, priority)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return {inn: value[1] for inn, value in selected.items()}


def _supplier_inns_by_contract(
    conn: sqlite3.Connection,
    contract_ids: list[int],
) -> dict[int, set[str]]:
    result: dict[int, set[str]] = defaultdict(set)
    if not contract_ids:
        return result
    for start in range(0, len(contract_ids), 800):
        chunk = contract_ids[start : start + 800]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT contract_id, inn FROM contract_suppliers WHERE contract_id IN ({placeholders})",
            tuple(chunk),
        ):
            inn = _valid_inn(row["inn"])
            if inn:
                result[int(row["contract_id"])].add(inn)
    return result


def _merge_name_candidates(
    target: dict[str, str],
    payload_json: Any,
    wanted_inns: set[str],
) -> int:
    if not payload_json:
        return 0
    try:
        payload = json.loads(str(payload_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    found = extract_party_names(payload, wanted_inns=wanted_inns)
    for inn, name in found.items():
        current = target.get(inn)
        if current is None or len(name) > len(current):
            target[inn] = name
    return len(found)


def enrich_organizations_from_payload(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any],
    inns: Iterable[str],
    region_code: int | None = None,
) -> dict[str, int]:
    """Enrich canonical organizations from a purchase/contract full document.

    Names and addresses are accepted only when they are tied to the same INN in
    the payload. Existing non-empty canonical values remain authoritative.
    """
    wanted = {inn for value in inns if (inn := _valid_inn(value))}
    names = extract_party_names(payload, wanted_inns=wanted)
    names_updated = 0
    for inn, name in names.items():
        cur = conn.execute(
            """
            UPDATE organizations
            SET name=?, updated_at=datetime('now')
            WHERE inn=? AND (name IS NULL OR trim(name)='')
            """,
            (name, inn),
        )
        names_updated += max(0, int(cur.rowcount))

    if region_code is not None and wanted:
        for inn in wanted:
            conn.execute(
                """
                UPDATE organizations
                SET region_code=COALESCE(region_code, ?), updated_at=datetime('now')
                WHERE inn=?
                """,
                (int(region_code), inn),
            )

    # Local import avoids a module cycle: locality depends only on organization
    # normalization and can therefore provide the address extractor here.
    from .locality import enrich_party_addresses

    address_stats = enrich_party_addresses(conn, payload=payload, inns=wanted)
    return {
        "names_extracted": len(names),
        "names_updated": names_updated,
        **address_stats,
    }


def enrich_contract_parties_from_payload(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any],
    customer_inn: str | None,
    supplier_inns: Iterable[str],
    region_code: int | None = None,
) -> dict[str, int]:
    """Fill missing canonical party names after one contract ingest.

    Existing non-empty organization names are intentionally authoritative and
    are never overwritten by a contract-document alias.
    """
    customer = _valid_inn(customer_inn)
    suppliers = {inn for value in supplier_inns if (inn := _valid_inn(value))}
    wanted = set(suppliers)
    if customer:
        wanted.add(customer)
    names = extract_party_names(payload, wanted_inns=wanted)

    updated = 0
    for inn, name in names.items():
        cur = conn.execute(
            """
            UPDATE organizations
            SET name=?, updated_at=datetime('now')
            WHERE inn=? AND (name IS NULL OR trim(name)='')
            """,
            (name, inn),
        )
        updated += max(0, int(cur.rowcount))

    if customer and region_code is not None:
        conn.execute(
            """
            UPDATE organizations
            SET region_code=COALESCE(region_code, ?), updated_at=datetime('now')
            WHERE inn=?
            """,
            (int(region_code), customer),
        )
    return {"names_extracted": len(names), "organizations_updated": updated}




def _organization_name_map(
    conn: sqlite3.Connection,
    inns: Iterable[str],
) -> dict[str, str | None]:
    normalized = sorted({inn for value in inns if (inn := _valid_inn(value))})
    result: dict[str, str | None] = {inn: None for inn in normalized}
    for start in range(0, len(normalized), 800):
        chunk = normalized[start : start + 800]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT inn, name FROM organizations WHERE inn IN ({placeholders})",
            tuple(chunk),
        ):
            name = _clean_name(row["name"])
            result[str(row["inn"])] = name
    return result


def _apply_missing_names(
    conn: sqlite3.Connection,
    names: dict[str, str],
) -> set[str]:
    updated: set[str] = set()
    for inn, name in names.items():
        cur = conn.execute(
            """
            UPDATE organizations
            SET name=?, updated_at=datetime('now')
            WHERE inn=? AND (name IS NULL OR trim(name)='')
            """,
            (name, inn),
        )
        if int(cur.rowcount) > 0:
            updated.add(inn)
    return updated


def _cached_detail_payload(
    conn: sqlite3.Connection,
    *,
    endpoint: str,
    external_id: str,
) -> Any | None:
    row = conn.execute(
        """
        SELECT payload_json
        FROM raw_documents
        WHERE source='gosplan-v2' AND endpoint=? AND external_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (endpoint, external_id),
    ).fetchone()
    if row is None or not row["payload_json"]:
        return None
    try:
        return json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _cache_detail_payload(
    conn: sqlite3.Connection,
    *,
    endpoint: str,
    external_id: str,
    payload: Any,
) -> None:
    from .db import insert_raw

    if isinstance(payload, dict):
        stored = payload
    else:
        stored = {"items": payload}
    insert_raw(conn, stored, external_id, endpoint=endpoint)


def _relevant_regional_demand_contracts(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    region_code: int,
    year: int,
) -> list[dict[str, Any]]:
    from .regional_demand import _manufacturer_okpd2_families, okpd2_family

    families = _manufacturer_okpd2_families(conn, [manufacturer_inn]).get(
        manufacturer_inn, set()
    )
    if not families:
        return []

    date_expr = (
        "COALESCE(NULLIF(c.published_at,''), NULLIF(c.doc_created_at,''), "
        "NULLIF(c.doc_updated_at,''), NULLIF(c.updated_at,''), NULLIF(c.elact_at,''))"
    )
    contracts: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        f"""
        SELECT c.id AS contract_id,
               c.reg_num,
               c.purchase_number,
               c.customer_inn,
               c.price,
               cc.code
        FROM contracts c
        JOIN contract_codes cc ON cc.contract_id=c.id
        WHERE c.region_code=?
          AND substr({date_expr}, 1, 4)=?
          AND cc.system IN ('okpd2', 'ktru')
        """,
        (int(region_code), str(int(year))),
    ):
        contract_id = int(row["contract_id"])
        item = contracts.setdefault(
            contract_id,
            {
                "contract_id": contract_id,
                "reg_num": str(row["reg_num"] or "").strip() or None,
                "purchase_number": str(row["purchase_number"] or "").strip() or None,
                "customer_inn": _valid_inn(row["customer_inn"]),
                "price": float(row["price"] or 0.0),
                "families": set(),
                "suppliers": set(),
            },
        )
        family = okpd2_family(row["code"])
        if family:
            item["families"].add(family)

    relevant = {
        contract_id: item
        for contract_id, item in contracts.items()
        if set(item["families"]) & families
    }
    supplier_map = _supplier_inns_by_contract(conn, sorted(relevant))
    for contract_id, suppliers in supplier_map.items():
        if contract_id in relevant:
            relevant[contract_id]["suppliers"] = set(suppliers)

    return sorted(
        relevant.values(),
        key=lambda item: (-float(item["price"]), str(item["reg_num"] or "")),
    )


def resolve_regional_demand_party_names_live(
    conn: sqlite3.Connection,
    *,
    client: Any,
    manufacturer_inn: str,
    region_code: int,
    year: int,
    max_requests: int | None = 100,
    rate_per_minute: float = 7.0,
    refresh: bool = False,
    emit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Resolve missing names only for contracts relevant to one manufacturer.

    Contract-history list rows intentionally contain compact aggregate fields and
    often omit party names.  Re-downloading all regional history just to recover
    names is wasteful, so this resolver fetches detail documents only for the
    manufacturer's matched OKPD2 contracts.  Detail payloads are cached in
    ``raw_documents`` and reruns are idempotent/resumable.
    """
    import httpx

    manufacturer = _valid_inn(manufacturer_inn)
    if manufacturer is None:
        raise ValueError("manufacturer_inn must be a 10- or 12-digit INN")
    if rate_per_minute <= 0:
        raise ValueError("rate_per_minute must be positive")
    request_budget = None if max_requests is None or int(max_requests) <= 0 else int(max_requests)
    delay = 60.0 / float(rate_per_minute)
    emit = emit or (lambda _: None)

    contracts = _relevant_regional_demand_contracts(
        conn,
        manufacturer_inn=manufacturer,
        region_code=region_code,
        year=year,
    )
    all_party_inns: set[str] = set()
    for item in contracts:
        customer = _valid_inn(item.get("customer_inn"))
        if customer:
            all_party_inns.add(customer)
        all_party_inns.update(
            inn for value in item.get("suppliers", set()) if (inn := _valid_inn(value))
        )

    conn.executemany(
        "INSERT OR IGNORE INTO organizations(inn) VALUES (?)",
        [(inn,) for inn in sorted(all_party_inns)],
    )
    conn.commit()
    names = _organization_name_map(conn, all_party_inns)
    missing_before = {inn for inn, name in names.items() if not name}
    updated_inns: set[str] = set()
    extracted_inns: set[str] = set()
    cached_detail_hits = 0
    network_requests = 0
    contract_detail_requests = 0
    purchase_detail_requests = 0
    result_detail_requests = 0
    not_found = 0
    request_errors = 0
    contract_detail_name_hits = 0
    purchase_detail_name_hits = 0
    result_detail_name_hits = 0
    detail_payloads_without_target_names = 0
    last_network_at: float | None = None
    stop_reason: str | None = None

    def remaining(targets: Iterable[str]) -> set[str]:
        return {
            inn
            for value in targets
            if (inn := _valid_inn(value)) and not names.get(inn)
        }

    def absorb(payload: Any, targets: set[str]) -> int:
        nonlocal extracted_inns, updated_inns
        if not targets:
            return 0
        found = extract_party_names(payload, wanted_inns=targets)
        if not found:
            return 0
        extracted_inns.update(found)
        changed = _apply_missing_names(conn, found)
        updated_inns.update(changed)
        for inn, name in found.items():
            if not names.get(inn):
                names[inn] = name
        conn.commit()
        return len(found)

    def load_or_fetch(
        *,
        endpoint: str,
        external_id: str,
        getter: Callable[[str], Any],
        targets: set[str],
        request_kind: str,
    ) -> bool:
        nonlocal cached_detail_hits, network_requests, contract_detail_requests
        nonlocal purchase_detail_requests, result_detail_requests, not_found
        nonlocal request_errors, contract_detail_name_hits, purchase_detail_name_hits
        nonlocal result_detail_name_hits, detail_payloads_without_target_names
        nonlocal last_network_at, stop_reason

        if not targets:
            return True
        payload = None if refresh else _cached_detail_payload(
            conn, endpoint=endpoint, external_id=external_id
        )
        if payload is not None:
            cached_detail_hits += 1
            hits = absorb(payload, targets)
            if hits == 0:
                detail_payloads_without_target_names += 1
            elif request_kind == "contract":
                contract_detail_name_hits += hits
            elif request_kind == "purchase":
                purchase_detail_name_hits += hits
            else:
                result_detail_name_hits += hits
            return True

        if request_budget is not None and network_requests >= request_budget:
            stop_reason = "request_budget"
            return False

        if last_network_at is not None:
            elapsed = time.monotonic() - last_network_at
            if elapsed < delay:
                time.sleep(delay - elapsed)
        emit(f"party-resolve {request_kind} {external_id} request={network_requests + 1}")
        try:
            payload = getter(external_id)
            network_requests += 1
            if request_kind == "contract":
                contract_detail_requests += 1
            elif request_kind == "purchase":
                purchase_detail_requests += 1
            else:
                result_detail_requests += 1
            last_network_at = time.monotonic()
        except httpx.HTTPStatusError as exc:
            network_requests += 1
            if request_kind == "contract":
                contract_detail_requests += 1
            elif request_kind == "purchase":
                purchase_detail_requests += 1
            else:
                result_detail_requests += 1
            last_network_at = time.monotonic()
            if exc.response.status_code == 404:
                not_found += 1
                _cache_detail_payload(
                    conn,
                    endpoint=endpoint,
                    external_id=external_id,
                    payload={"_party_resolution_status": 404},
                )
                conn.commit()
                return True
            request_errors += 1
            emit(
                f"party-resolve error {request_kind} {external_id}: "
                f"HTTP {exc.response.status_code}"
            )
            return True
        except httpx.RequestError as exc:
            network_requests += 1
            if request_kind == "contract":
                contract_detail_requests += 1
            elif request_kind == "purchase":
                purchase_detail_requests += 1
            else:
                result_detail_requests += 1
            last_network_at = time.monotonic()
            request_errors += 1
            emit(f"party-resolve network error {request_kind} {external_id}: {exc}")
            return True

        _cache_detail_payload(
            conn,
            endpoint=endpoint,
            external_id=external_id,
            payload=payload,
        )
        hits = absorb(payload, targets)
        if hits == 0:
            detail_payloads_without_target_names += 1
        elif request_kind == "contract":
            contract_detail_name_hits += hits
        elif request_kind == "purchase":
            purchase_detail_name_hits += hits
        else:
            result_detail_name_hits += hits
        conn.commit()
        return True

    for item in contracts:
        customer = _valid_inn(item.get("customer_inn"))
        suppliers = {
            inn for value in item.get("suppliers", set()) if (inn := _valid_inn(value))
        }
        targets = set(suppliers)
        if customer:
            targets.add(customer)
        unresolved = remaining(targets)
        if not unresolved:
            continue

        reg_num = str(item.get("reg_num") or "").strip()
        if reg_num:
            if not load_or_fetch(
                endpoint=f"/fz44/contracts/{reg_num}",
                external_id=reg_num,
                getter=client.get_contract,
                targets=unresolved,
                request_kind="contract",
            ):
                break

        purchase_number = str(item.get("purchase_number") or "").strip()
        if customer and purchase_number and remaining({customer}):
            if not load_or_fetch(
                endpoint=f"/fz44/purchases/{purchase_number}",
                external_id=purchase_number,
                getter=client.get_purchase,
                targets=remaining({customer}),
                request_kind="purchase",
            ):
                break

        unresolved_suppliers = remaining(suppliers)
        if purchase_number and unresolved_suppliers:
            if not load_or_fetch(
                endpoint=f"/fz44/purchases/{purchase_number}/result",
                external_id=purchase_number,
                getter=client.get_purchase_result,
                targets=unresolved_suppliers,
                request_kind="result",
            ):
                break

    names_after = _organization_name_map(conn, all_party_inns)
    missing_after = sorted(inn for inn, name in names_after.items() if not name)
    resolved_after = len(all_party_inns) - len(missing_after)
    if stop_reason is None:
        stop_reason = "complete" if not missing_after else "sources_exhausted"

    return {
        "manufacturer_inn": manufacturer,
        "region_code": int(region_code),
        "year": int(year),
        "matched_contracts": len(contracts),
        "party_inns_seen": len(all_party_inns),
        "party_names_present_before": len(all_party_inns) - len(missing_before),
        "party_names_missing_before": len(missing_before),
        "name_inns_extracted": len(extracted_inns),
        "organizations_updated": len(updated_inns),
        "party_names_present_after": resolved_after,
        "party_names_missing_after": len(missing_after),
        "cached_detail_hits": cached_detail_hits,
        "network_requests": network_requests,
        "contract_detail_requests": contract_detail_requests,
        "purchase_detail_requests": purchase_detail_requests,
        "result_detail_requests": result_detail_requests,
        "http_404": not_found,
        "request_errors": request_errors,
        "contract_detail_name_hits": contract_detail_name_hits,
        "purchase_detail_name_hits": purchase_detail_name_hits,
        "result_detail_name_hits": result_detail_name_hits,
        "detail_payloads_without_target_names": detail_payloads_without_target_names,
        "stop_reason": stop_reason,
        "unresolved_party_inns": missing_after[:100],
    }

def backfill_contract_parties(
    conn: sqlite3.Connection,
    *,
    region_code: int | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    """Backfill empty contract-party names from already stored raw JSON.

    Contract raw documents are inspected first. If a linked purchase exists,
    its raw JSON is inspected as a second source because full purchase
    documents often contain richer organization details. No network requests
    are made.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if region_code is not None:
        clauses.append("c.region_code=?")
        params.append(int(region_code))
    if year is not None:
        date_expr = (
            "COALESCE(NULLIF(c.published_at,''), NULLIF(c.doc_created_at,''), "
            "NULLIF(c.doc_updated_at,''), NULLIF(c.updated_at,''), NULLIF(c.elact_at,''))"
        )
        clauses.append(f"substr({date_expr}, 1, 4)=?")
        params.append(str(int(year)))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""

    rows = list(
        conn.execute(
            f"""
            SELECT c.id AS contract_id,
                   c.customer_inn,
                   c.region_code,
                   c.purchase_number,
                   cr.payload_json AS contract_payload_json,
                   pr.payload_json AS purchase_payload_json
            FROM contracts c
            JOIN raw_documents cr ON cr.id=c.raw_document_id
            LEFT JOIN purchases p ON p.purchase_number=c.purchase_number
            LEFT JOIN raw_documents pr ON pr.id=p.raw_document_id
            {where}
            """,
            tuple(params),
        )
    )
    contract_ids = [int(row["contract_id"]) for row in rows]
    suppliers_by_contract = _supplier_inns_by_contract(conn, contract_ids)

    party_inns: set[str] = set()
    customer_inns: set[str] = set()
    supplier_inns: set[str] = set()
    candidates: dict[str, str] = {}
    customer_regions: dict[str, int] = {}
    raw_contract_matches = 0
    raw_purchase_matches = 0

    for row in rows:
        contract_id = int(row["contract_id"])
        customer = _valid_inn(row["customer_inn"])
        suppliers = suppliers_by_contract.get(contract_id, set())
        wanted = set(suppliers)
        if customer:
            wanted.add(customer)
            customer_inns.add(customer)
        supplier_inns.update(suppliers)
        party_inns.update(wanted)
        raw_contract_matches += _merge_name_candidates(
            candidates, row["contract_payload_json"], wanted
        )
        raw_purchase_matches += _merge_name_candidates(
            candidates, row["purchase_payload_json"], wanted
        )

        if customer and row["region_code"] is not None:
            customer_regions.setdefault(customer, int(row["region_code"]))

    conn.executemany(
        """
        UPDATE organizations
        SET region_code=COALESCE(region_code, ?), updated_at=datetime('now')
        WHERE inn=?
        """,
        [(region, inn) for inn, region in customer_regions.items()],
    )

    existing_names: set[str] = set()
    for start in range(0, len(party_inns), 800):
        chunk = sorted(party_inns)[start : start + 800]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT inn FROM organizations WHERE inn IN ({placeholders}) AND NULLIF(trim(name),'') IS NOT NULL",
            tuple(chunk),
        ):
            existing_names.add(str(row["inn"]))

    updated_inns: set[str] = set()
    for inn, name in candidates.items():
        cur = conn.execute(
            """
            UPDATE organizations
            SET name=?, updated_at=datetime('now')
            WHERE inn=? AND (name IS NULL OR trim(name)='')
            """,
            (name, inn),
        )
        if int(cur.rowcount) > 0:
            updated_inns.add(inn)

    conn.commit()
    named_after = existing_names | updated_inns
    return {
        "region_code": region_code,
        "year": year,
        "contracts_scanned": len(rows),
        "party_inns_seen": len(party_inns),
        "customer_inns_seen": len(customer_inns),
        "supplier_inns_seen": len(supplier_inns),
        "names_already_present": len(existing_names),
        "name_candidates_extracted": len(candidates),
        "raw_contract_name_matches": raw_contract_matches,
        "raw_purchase_name_matches": raw_purchase_matches,
        "organizations_updated": len(updated_inns),
        "customers_updated": len(updated_inns & customer_inns),
        "suppliers_updated": len(updated_inns & supplier_inns),
        "party_names_present_after": len(named_after & party_inns),
        "party_names_missing_after": len(party_inns - named_after),
        "offline_resolution_status": (
            "stored_raw_has_no_party_names"
            if party_inns - named_after and not candidates
            else "partial"
            if party_inns - named_after
            else "complete"
        ),
        "live_resolution_required": bool((party_inns - named_after) and not candidates),
        "network_requests": 0,
    }
