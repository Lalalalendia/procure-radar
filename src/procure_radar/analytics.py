from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from .classification import (
    classify_purchase_type,
    competition_detail_exclusion_reason,
    competition_exclusion_reason,
    lifecycle_from_docs,
    select_competition_protocol,
)
from .extract import extract_purchase_pricing_mode
from .outcomes import classify_protocol_outcome, protocol_weakness
from .scoring import ManufacturingInput, OpportunityInput, manufacturing_gap_score, opportunity_score
from .segmentation import classify_market_segment


def _documents_by_purchase(conn: sqlite3.Connection, *, region_code: int) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT p.purchase_number, pd.doc_type
        FROM purchases p
        LEFT JOIN purchase_documents pd ON pd.purchase_id=p.id
        WHERE p.region_code=?
        """,
        (region_code,),
    ).fetchall()
    result: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row["doc_type"]:
            result[row["purchase_number"]].append(row["doc_type"])
    return result


def _pricing_modes_by_purchase(conn: sqlite3.Connection, *, region_code: int) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT p.purchase_number, rd.payload_json
        FROM purchases p
        JOIN raw_documents rd ON rd.id=p.raw_document_id
        WHERE p.region_code=?
        """,
        (region_code,),
    ).fetchall()
    result: dict[str, str] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            result[row["purchase_number"]] = "unknown"
            continue
        result[row["purchase_number"]] = extract_purchase_pricing_mode(payload)
    return result


def _protocols_by_purchase(conn: sqlite3.Connection, *, region_code: int) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT tp.*
        FROM tender_protocols tp
        JOIN purchases p ON p.purchase_number=tp.purchase_number
        WHERE p.region_code=?
        ORDER BY tp.purchase_number, COALESCE(tp.published_at, ''), tp.id
        """,
        (region_code,),
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        result[item["purchase_number"]].append(item)
    return result




def _buyers_by_purchase(conn: sqlite3.Connection, *, region_code: int) -> dict[str, set[str]]:
    rows = conn.execute(
        """
        SELECT p.purchase_number, p.responsible_inn, pp.inn AS customer_inn
        FROM purchases p
        LEFT JOIN purchase_parties pp
          ON pp.purchase_id=p.id AND pp.role='customer'
        WHERE p.region_code=?
        """,
        (region_code,),
    ).fetchall()
    result: dict[str, set[str]] = defaultdict(set)
    fallback: dict[str, str] = {}
    for row in rows:
        number = row["purchase_number"]
        if row["customer_inn"]:
            result[number].add(str(row["customer_inn"]))
        if row["responsible_inn"]:
            fallback[number] = str(row["responsible_inn"])
    for number, inn in fallback.items():
        if not result[number]:
            result[number].add(inn)
    return result


def _buyer_diversity_score(*, buyer_count: int, top_buyer_share: float) -> float:
    if buyer_count <= 0:
        return 0.0
    breadth = min(1.0, buyer_count / 5.0)
    concentration_relief = max(0.0, 1.0 - top_buyer_share)
    return max(0.0, min(1.0, 0.55 * breadth + 0.45 * concentration_relief))


def _gap_metrics(outcome_counts: Counter[str], protocol_count: int) -> dict[str, Any]:
    if protocol_count <= 0:
        return {
            "gap_type": "unknown",
            "gap_strength": 0.0,
            "supplier_gap_share": 0.0,
            "no_bid_gap_share": 0.0,
            "barrier_gap_share": 0.0,
            "competitive_share": 0.0,
        }

    supplier = outcome_counts.get("single_submitted", 0)
    no_bid = outcome_counts.get("no_bids", 0)
    barrier = (
        outcome_counts.get("single_admitted", 0)
        + outcome_counts.get("single_submitted_rejected", 0)
        + outcome_counts.get("all_rejected", 0)
    )
    competitive = outcome_counts.get("competitive", 0)
    shares = {
        "supplier_gap": supplier / protocol_count,
        "no_bid_gap": no_bid / protocol_count,
        "barrier_gap": barrier / protocol_count,
    }
    ranked = sorted(shares.items(), key=lambda item: item[1], reverse=True)
    top_type, top_share = ranked[0]
    second_share = ranked[1][1]
    competitive_share = competitive / protocol_count
    if top_share < 0.25:
        gap_type = "competitive_market"
    elif competitive_share > top_share and competitive_share >= 0.50:
        gap_type = "competitive_market"
    elif abs(competitive_share - top_share) < 0.10 and competitive_share >= 0.40:
        gap_type = "mixed"
    elif top_share - second_share < 0.10 and second_share >= 0.20:
        gap_type = "mixed"
    else:
        gap_type = top_type
    return {
        "gap_type": gap_type,
        "gap_strength": round(top_share, 3),
        "supplier_gap_share": round(shares["supplier_gap"], 3),
        "no_bid_gap_share": round(shares["no_bid_gap"], 3),
        "barrier_gap_share": round(shares["barrier_gap"], 3),
        "competitive_share": round(competitive_share, 3),
    }


def _rzn_enrichment_state(conn: sqlite3.Connection) -> str:
    products = int(conn.execute("SELECT COUNT(*) FROM rzn_med_products").fetchone()[0])
    if products == 0:
        return "unavailable"
    active_sync = conn.execute(
        """
        SELECT completed
        FROM rzn_sync_state
        WHERE legal_system='RUSSIA' AND text_search='' AND sync_key LIKE '%|status=active'
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    unresolved = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT l.classifier_external_id)
            FROM rzn_med_product_classifier_ids l
            LEFT JOIN rzn_nsi_records n ON n.classifier_external_id=l.classifier_external_id
            WHERE n.classifier_external_id IS NULL
            """
        ).fetchone()[0]
    )
    if active_sync is not None and int(active_sync["completed"] or 0) == 1 and unresolved == 0:
        return "complete"
    return "partial"


def _rzn_counts_by_okpd2(
    conn: sqlite3.Connection,
    okpd2_codes: set[str] | None = None,
) -> dict[str, dict[str, int]]:
    """Return RZN counts, optionally only for market codes actually being rendered."""
    if okpd2_codes is not None and not okpd2_codes:
        return {}
    result: dict[str, dict[str, int]] = {}
    chunks: list[list[str] | None]
    if okpd2_codes is None:
        chunks = [None]
    else:
        values = sorted(okpd2_codes)
        chunks = [values[i : i + 400] for i in range(0, len(values), 400)]
    for chunk in chunks:
        where_codes = ""
        params: list[Any] = []
        if chunk is not None:
            where_codes = " AND n.code IN (" + ",".join("?" for _ in chunk) + ")"
            params.extend(chunk)
        query = f"""
            SELECT
                n.code AS okpd2_code,
                COUNT(DISTINCT p.id) AS products,
                COUNT(DISTINCT NULLIF(p.producer_name, '')) AS producers,
                COUNT(DISTINCT NULLIF(p.representative_name, '')) AS representatives
            FROM rzn_nsi_records n
            JOIN rzn_med_product_classifier_ids l
              ON l.classifier_external_id=n.classifier_external_id
            JOIN rzn_med_products p ON p.id=l.med_product_id
            WHERE n.catalog_code='okpd2Code'
              AND n.code IS NOT NULL
              AND p.status_id=1
              {where_codes}
            GROUP BY n.code
        """
        for row in conn.execute(query, params):
            result[str(row["okpd2_code"])] = {
                "products": int(row["products"] or 0),
                "producers": int(row["producers"] or 0),
                "representatives": int(row["representatives"] or 0),
            }
    return result


def _gisp_enrichment_state(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT source_scope, completed_at
        FROM gisp_import_runs
        WHERE completed_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return "unavailable"
    return "complete" if row["source_scope"] == "active" else "partial"


def _gisp_counts_by_okpd2(
    conn: sqlite3.Connection,
    okpd2_codes: set[str] | None = None,
) -> dict[str, dict[str, int]]:
    """Return GISP counts for requested OKPD2 codes instead of scanning all 473k rows."""
    if okpd2_codes is not None and not okpd2_codes:
        return {}
    result: dict[str, dict[str, int]] = {}
    has_rows = int(conn.execute("SELECT EXISTS(SELECT 1 FROM gisp_registry_rows LIMIT 1)").fetchone()[0]) > 0
    chunks: list[list[str] | None]
    if okpd2_codes is None:
        chunks = [None]
    else:
        values = sorted(okpd2_codes)
        chunks = [values[i : i + 400] for i in range(0, len(values), 400)]
    for chunk in chunks:
        params: list[Any] = []
        if has_rows:
            where_codes = ""
            if chunk is not None:
                where_codes = " AND r.okpd2_code IN (" + ",".join("?" for _ in chunk) + ")"
                params.extend(chunk)
            query = f"""
                SELECT r.okpd2_code,
                       COUNT(DISTINCT r.registry_number) AS products,
                       COUNT(DISTINCT NULLIF(p.manufacturer_inn, '')) AS manufacturers
                FROM gisp_registry_rows r
                JOIN gisp_products p ON p.registry_number=r.registry_number
                WHERE p.is_active=1 AND NULLIF(r.okpd2_code, '') IS NOT NULL
                  {where_codes}
                GROUP BY r.okpd2_code
            """
        else:
            where_codes = ""
            if chunk is not None:
                where_codes = " AND okpd2_code IN (" + ",".join("?" for _ in chunk) + ")"
                params.extend(chunk)
            query = f"""
                SELECT okpd2_code,
                       COUNT(*) AS products,
                       COUNT(DISTINCT NULLIF(manufacturer_inn, '')) AS manufacturers
                FROM gisp_products
                WHERE is_active=1 AND NULLIF(okpd2_code, '') IS NOT NULL
                  {where_codes}
                GROUP BY okpd2_code
            """
        for row in conn.execute(query, params):
            result[str(row["okpd2_code"])] = {
                "products": int(row["products"] or 0),
                "manufacturers": int(row["manufacturers"] or 0),
            }
    return result


def _base_okpd2_code(system: str, code: str) -> str | None:
    if system == "okpd2":
        return code
    if system == "ktru" and "-" in code:
        return code.split("-", 1)[0]
    return None


def _gisp_applicability(*, group: str, segment: str, state: str, has_match: bool) -> str:
    if state == "unavailable":
        return "unavailable"
    if group != "goods":
        return "not_applicable"
    if segment == "pharma":
        # PP719/GISP is an industrial-origin register, not an exhaustive register
        # of medicines. A positive match is useful context, but its manufacturer
        # count must not be treated as the denominator for the whole pharma market.
        return "limited" if has_match else "unknown"
    if has_match:
        return "applicable"
    # An empty PP719 match is not evidence that a goods market has no Russian
    # producers. GISP is useful coverage, but it is not exhaustive for every
    # product class.
    return "unknown"


def _distribution_gap_score(
    *,
    supplier_gap_share: float,
    manufacturer_count: int,
    buyer_diversity_score: float,
    protocol_coverage: float,
) -> float | None:
    if manufacturer_count <= 0:
        return None
    supply_depth = min(1.0, math.log1p(manufacturer_count) / math.log1p(50))
    base = (
        0.55 * max(0.0, min(1.0, supplier_gap_share))
        + 0.25 * supply_depth
        + 0.20 * max(0.0, min(1.0, buyer_diversity_score))
    )
    evidence = 0.70 + 0.30 * max(0.0, min(1.0, protocol_coverage))
    return round(base * evidence * 100.0, 2)


def _contract_supplier_enrichment_state(
    conn: sqlite3.Connection, *, region_code: int
) -> str:
    """Describe whether contract-winner coverage is actually present for a region."""
    try:
        contracts = int(
            conn.execute(
                "SELECT COUNT(*) FROM contracts WHERE region_code=?", (region_code,)
            ).fetchone()[0]
        )
        if contracts <= 0:
            return "unavailable"
        suppliers = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM contract_suppliers cs
                JOIN contracts c ON c.id=cs.contract_id
                WHERE c.region_code=?
                """,
                (region_code,),
            ).fetchone()[0]
        )
    except sqlite3.OperationalError:
        return "unavailable"
    return "available" if suppliers > 0 else "partial"


def _contract_suppliers_by_okpd2(
    conn: sqlite3.Connection, *, region_code: int
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    query = """
        WITH contract_okpd2 AS (
            SELECT DISTINCT
                cc.contract_id,
                CASE
                    WHEN cc.system='okpd2' THEN cc.code
                    WHEN cc.system='ktru' AND instr(cc.code, '-') > 0
                    THEN substr(cc.code, 1, instr(cc.code, '-') - 1)
                    ELSE NULL
                END AS okpd2_code
            FROM contract_codes cc
        )
        SELECT
            co.okpd2_code,
            COUNT(DISTINCT cs.inn) AS suppliers,
            COUNT(DISTINCT co.contract_id) AS contracts
        FROM contract_okpd2 co
        JOIN contracts c ON c.id=co.contract_id
        JOIN contract_suppliers cs ON cs.contract_id=co.contract_id
        WHERE c.region_code=? AND co.okpd2_code IS NOT NULL
        GROUP BY co.okpd2_code
    """
    for row in conn.execute(query, (region_code,)):
        result[str(row["okpd2_code"])] = {
            "suppliers": int(row["suppliers"] or 0),
            "contracts": int(row["contracts"] or 0),
        }
    return result


def _contracts_by_purchase(
    conn: sqlite3.Connection, *, region_code: int
) -> dict[str, list[dict[str, Any]]]:
    """Load regional contracts without supplier×code cross-product amplification."""
    contracts: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT id AS contract_id, purchase_number, price
        FROM contracts
        WHERE region_code=? AND NULLIF(purchase_number, '') IS NOT NULL
        ORDER BY id
        """,
        (region_code,),
    ):
        contract_id = int(row["contract_id"])
        contracts[contract_id] = {
            "contract_id": contract_id,
            "purchase_number": str(row["purchase_number"]),
            "price": float(row["price"]) if row["price"] is not None else None,
            "suppliers": set(),
            "codes": {"okpd2": set(), "ktru": set()},
        }

    if not contracts:
        return {}

    for row in conn.execute(
        """
        SELECT cs.contract_id, cs.inn
        FROM contract_suppliers cs
        JOIN contracts c ON c.id=cs.contract_id
        WHERE c.region_code=? AND NULLIF(c.purchase_number, '') IS NOT NULL
        """,
        (region_code,),
    ):
        contract = contracts.get(int(row["contract_id"]))
        if contract is not None and row["inn"]:
            contract["suppliers"].add(str(row["inn"]))

    for row in conn.execute(
        """
        SELECT cc.contract_id, cc.system, cc.code
        FROM contract_codes cc
        JOIN contracts c ON c.id=cc.contract_id
        WHERE c.region_code=? AND NULLIF(c.purchase_number, '') IS NOT NULL
        """,
        (region_code,),
    ):
        contract = contracts.get(int(row["contract_id"]))
        system = str(row["system"] or "")
        if contract is not None and system in {"okpd2", "ktru"} and row["code"]:
            contract["codes"][system].add(str(row["code"]))

    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for contract in contracts.values():
        result[contract["purchase_number"]].append(contract)
    return result


def _contract_market_match(
    contract: dict[str, Any],
    *,
    system: str,
    code: str,
) -> tuple[str | None, int]:
    """Return match quality and a conservative value-allocation divisor.

    Contracts often contain several KTRU positions but only one total contract
    value. Native KTRU analysis therefore allocates that value across the
    contract's KTRU codes instead of attributing the full contract to every code.
    For OKPD2 analysis, KTRU codes are collapsed to their parent OKPD2 first.
    """
    codes = contract.get("codes") or {}
    okpd2_codes = {str(value) for value in codes.get("okpd2", set()) if value}
    ktru_codes = {str(value) for value in codes.get("ktru", set()) if value}
    has_codes = bool(okpd2_codes or ktru_codes)

    if system == "ktru":
        if code in ktru_codes:
            return "exact_ktru", max(1, len(ktru_codes))
        parent = _base_okpd2_code("ktru", code)
        if not ktru_codes and parent and parent in okpd2_codes:
            return "parent_okpd2", max(1, len(okpd2_codes))
        if has_codes:
            return None, 1
        return "purchase_fallback", 1

    if system == "okpd2":
        parent_codes = set(okpd2_codes)
        parent_codes.update(
            parent
            for value in ktru_codes
            if (parent := _base_okpd2_code("ktru", value))
        )
        if code in parent_codes:
            return "exact_okpd2", max(1, len(parent_codes))
        if has_codes:
            return None, 1
        return "purchase_fallback", 1

    return ("purchase_fallback", 1) if not has_codes else (None, 1)


def _contract_concentration(
    purchase_numbers: set[str],
    contracts_by_purchase: dict[str, list[dict[str, Any]]],
    *,
    system: str,
    code: str,
) -> dict[str, Any]:
    """Summarize observed contract winners for one procurement market.

    Contract codes are used whenever available. A contract that explicitly lists
    other KTRU/OKPD2 codes is not attributed to this market merely because it
    shares the same purchase number. Purchase-only fallback is retained for old
    contracts where the source supplied no classifier codes.
    """
    procurement_count = len(purchase_numbers)
    candidate_contracts: dict[int, tuple[dict[str, Any], str, int]] = {}
    contract_linked_purchases: set[str] = set()
    for number in purchase_numbers:
        for contract in contracts_by_purchase.get(number, []):
            match, divisor = _contract_market_match(contract, system=system, code=code)
            if match is None:
                continue
            candidate_contracts[int(contract["contract_id"])] = (contract, match, divisor)
            contract_linked_purchases.add(number)

    contract_purchase_coverage = (
        len(contract_linked_purchases) / procurement_count if procurement_count else 0.0
    )

    supplier_value: Counter[str] = Counter()
    supplier_contract_weight: Counter[str] = Counter()
    priced_contracts = 0
    contracts_with_suppliers = 0
    supplier_linked_purchases: set[str] = set()
    match_counts: Counter[str] = Counter()
    observed_value = 0.0
    bundle_value = 0.0
    evidence_contract_ids: list[int] = []

    for contract, match, code_divisor in candidate_contracts.values():
        suppliers = sorted(contract["suppliers"])
        if not suppliers:
            continue
        contracts_with_suppliers += 1
        evidence_contract_ids.append(int(contract["contract_id"]))
        match_counts[match] += 1
        supplier_linked_purchases.add(str(contract["purchase_number"]))
        supplier_weight = 1.0 / len(suppliers)
        for inn in suppliers:
            supplier_contract_weight[inn] += supplier_weight
        price = contract["price"]
        if price is not None and price >= 0:
            priced_contracts += 1
            bundle_value += float(price)
            market_value = float(price) / max(1, code_divisor)
            observed_value += market_value
            allocated = market_value / len(suppliers)
            for inn in suppliers:
                supplier_value[inn] += allocated

    unique_winners = len(supplier_contract_weight)
    value_coverage = (
        priced_contracts / contracts_with_suppliers if contracts_with_suppliers else 0.0
    )
    winner_purchase_coverage = (
        len(supplier_linked_purchases) / procurement_count if procurement_count else 0.0
    )

    distribution: Counter[str]
    basis: str | None
    if supplier_value and sum(supplier_value.values()) > 0:
        distribution = supplier_value
        basis = "contract_value"
    elif supplier_contract_weight and sum(supplier_contract_weight.values()) > 0:
        distribution = supplier_contract_weight
        basis = "contract_count"
    else:
        distribution = Counter()
        basis = None

    top_supplier_inn = None
    top1_share = None
    top3_share = None
    hhi = None
    if distribution:
        total = float(sum(distribution.values()))
        ranked = sorted(distribution.items(), key=lambda item: (-item[1], item[0]))
        shares = [(inn, float(value) / total) for inn, value in ranked]
        top_supplier_inn = shares[0][0]
        top1_share = shares[0][1]
        top3_share = sum(share for _, share in shares[:3])
        hhi = sum(share * share for _, share in shares) * 10_000.0

    contract_count = len(candidate_contracts)
    code_matched_contracts = sum(
        count for match, count in match_counts.items() if match != "purchase_fallback"
    )
    code_match_coverage = (
        code_matched_contracts / contracts_with_suppliers if contracts_with_suppliers else 0.0
    )
    if len(match_counts) == 1:
        concentration_match = next(iter(match_counts))
    elif match_counts:
        concentration_match = "mixed"
    else:
        concentration_match = "unavailable"

    if winner_purchase_coverage >= 0.70 and contract_count >= 5 and code_match_coverage >= 0.70:
        evidence = "high"
    elif winner_purchase_coverage >= 0.40 and contract_count >= 3 and code_match_coverage >= 0.50:
        evidence = "medium"
    elif contract_count > 0:
        evidence = "low"
    else:
        evidence = "none"

    return {
        "known_contracts": contract_count,
        "known_contract_suppliers": unique_winners,
        "contract_linked_purchases": len(contract_linked_purchases),
        "contract_purchase_coverage": round(contract_purchase_coverage, 3),
        "winner_linked_purchases": len(supplier_linked_purchases),
        "winner_purchase_coverage": round(winner_purchase_coverage, 3),
        "winner_value_coverage": round(value_coverage, 3),
        "winner_code_match_coverage": round(code_match_coverage, 3),
        "winner_observed_value_rub": round(observed_value, 2),
        "winner_bundle_value_rub": round(bundle_value, 2),
        "winner_contract_ids": sorted(evidence_contract_ids),
        "winner_exact_contracts": code_matched_contracts,
        "winner_fallback_contracts": int(match_counts.get("purchase_fallback", 0)),
        "top_supplier_inn": top_supplier_inn,
        "top1_supplier_share": round(top1_share, 4) if top1_share is not None else None,
        "top3_supplier_share": round(top3_share, 4) if top3_share is not None else None,
        "supplier_hhi": round(hhi, 2) if hhi is not None else None,
        "supplier_concentration_basis": basis,
        "supplier_concentration_match": concentration_match,
        "supplier_concentration_evidence": evidence,
    }

def _category_purchase_rows(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    analysis_level: str,
) -> list[sqlite3.Row]:
    if analysis_level not in {"native", "okpd2"}:
        raise ValueError("analysis_level must be 'native' or 'okpd2'")

    if analysis_level == "okpd2":
        item_system = "'okpd2'"
        item_code = """
            COALESCE(
                NULLIF(pi.okpd2_code, ''),
                CASE
                    WHEN instr(COALESCE(pi.ktru_code, ''), '-') > 0
                    THEN substr(pi.ktru_code, 1, instr(pi.ktru_code, '-') - 1)
                    ELSE NULL
                END
            )
        """
        item_label = "COALESCE(pi.okpd2_name, pi.ktru_name, pi.name)"
        fallback_codes = """
            SELECT DISTINCT
                pc.purchase_id,
                'okpd2' AS code_system,
                CASE
                    WHEN pc.system='okpd2' THEN pc.code
                    WHEN pc.system='ktru' AND instr(pc.code, '-') > 0
                    THEN substr(pc.code, 1, instr(pc.code, '-') - 1)
                    ELSE NULL
                END AS code
            FROM purchase_codes pc
            WHERE NOT EXISTS (
                    SELECT 1
                    FROM purchase_items pi
                    WHERE pi.purchase_id=pc.purchase_id
                      AND COALESCE(pi.ktru_code, pi.okpd2_code) IS NOT NULL
              )
        """
    else:
        item_system = "CASE WHEN pi.ktru_code IS NOT NULL THEN 'ktru' ELSE 'okpd2' END"
        item_code = "COALESCE(pi.ktru_code, pi.okpd2_code)"
        item_label = "COALESCE(pi.ktru_name, pi.okpd2_name, pi.name)"
        fallback_codes = """
            SELECT pc.purchase_id, pc.system AS code_system, pc.code
            FROM purchase_codes pc
            WHERE NOT EXISTS (
                    SELECT 1
                    FROM purchase_items pi
                    WHERE pi.purchase_id=pc.purchase_id
                      AND COALESCE(pi.ktru_code, pi.okpd2_code) IS NOT NULL
              )
              AND (
                    pc.system='ktru'
                    OR (
                        pc.system='okpd2'
                        AND NOT EXISTS (
                            SELECT 1 FROM purchase_codes k
                            WHERE k.purchase_id=pc.purchase_id AND k.system='ktru'
                        )
                    )
              )
        """

    query = f"""
        WITH raw_item_codes AS (
            SELECT
                pi.purchase_id,
                {item_system} AS code_system,
                {item_code} AS code,
                {item_label} AS label,
                CASE WHEN pi.amount IS NOT NULL AND pi.amount > 0 THEN pi.amount ELSE 0 END AS raw_item_amount
            FROM purchase_items pi
            WHERE {item_code} IS NOT NULL
        ),
        item_code_amounts AS (
            SELECT
                purchase_id,
                code_system,
                code,
                MAX(label) AS label,
                SUM(raw_item_amount) AS raw_code_amount
            FROM raw_item_codes
            GROUP BY purchase_id, code_system, code
        ),
        item_purchase_summary AS (
            SELECT
                p.id AS purchase_id,
                p.max_price,
                COUNT(*) AS code_count,
                SUM(ica.raw_code_amount) AS raw_total_amount
            FROM purchases p
            JOIN item_code_amounts ica ON ica.purchase_id=p.id
            GROUP BY p.id, p.max_price
        ),
        item_purchase AS (
            SELECT
                ica.purchase_id,
                ica.code_system,
                ica.code,
                ica.label,
                CASE
                    WHEN s.max_price IS NOT NULL AND s.max_price > 0
                         AND s.raw_total_amount > 0
                         AND ABS(s.raw_total_amount - s.max_price) / s.max_price <= 0.05
                    THEN s.max_price * ica.raw_code_amount / s.raw_total_amount
                    WHEN s.max_price IS NOT NULL AND s.max_price > 0 AND s.code_count > 0
                    THEN s.max_price / s.code_count
                    WHEN ica.raw_code_amount > 0
                    THEN ica.raw_code_amount
                    ELSE NULL
                END AS item_demand,
                CASE
                    WHEN s.max_price IS NOT NULL AND s.max_price > 0
                         AND s.raw_total_amount > 0
                         AND ABS(s.raw_total_amount - s.max_price) / s.max_price <= 0.05
                    THEN 'items_exact'
                    WHEN s.max_price IS NOT NULL AND s.max_price > 0
                    THEN 'items_budget_normalized'
                    ELSE 'items_raw'
                END AS demand_basis
            FROM item_code_amounts ica
            JOIN item_purchase_summary s ON s.purchase_id=ica.purchase_id
        ),
        fallback_codes AS (
            {fallback_codes}
        ),
        usable_fallback_codes AS (
            SELECT purchase_id, code_system, code
            FROM fallback_codes
            WHERE code IS NOT NULL
            GROUP BY purchase_id, code_system, code
        ),
        fallback_counts AS (
            SELECT purchase_id, COUNT(*) AS code_count
            FROM usable_fallback_codes
            GROUP BY purchase_id
        ),
        fallback_purchase AS (
            SELECT
                fc.purchase_id,
                fc.code_system,
                fc.code,
                p.object_info AS label,
                CASE
                    WHEN p.max_price IS NOT NULL AND c.code_count > 0
                    THEN p.max_price / c.code_count
                    ELSE NULL
                END AS item_demand,
                'aggregate' AS demand_basis
            FROM usable_fallback_codes fc
            JOIN fallback_counts c ON c.purchase_id=fc.purchase_id
            JOIN purchases p ON p.id=fc.purchase_id
        ),
        category_purchase AS (
            SELECT * FROM item_purchase
            UNION ALL
            SELECT * FROM fallback_purchase
        )
        SELECT
            cp.code_system,
            cp.code,
            cp.label,
            cp.item_demand,
            cp.demand_basis,
            p.purchase_number,
            p.published_at,
            p.max_price,
            p.purchase_type,
            p.stage
        FROM category_purchase cp
        JOIN purchases p ON p.id=cp.purchase_id
        WHERE p.region_code=?
    """
    return conn.execute(query, (region_code,)).fetchall()


def compute_opportunities(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    min_procurements: int = 2,
    min_protocol_coverage: float = 0.5,
    max_procurements: int | None = None,
    segment: str | None = None,
    group: str | None = None,
    gap_type: str | None = None,
    analysis_level: str = "native",
) -> list[dict[str, Any]]:
    rows = _category_purchase_rows(
        conn, region_code=region_code, analysis_level=analysis_level
    )

    documents = _documents_by_purchase(conn, region_code=region_code)
    protocols = _protocols_by_purchase(conn, region_code=region_code)
    pricing_modes = _pricing_modes_by_purchase(conn, region_code=region_code)
    buyers = _buyers_by_purchase(conn, region_code=region_code)

    grouped: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "labels": [],
            "purchases": set(),
            "months": set(),
            "demand_rub": 0.0,
            "amount_rows": 0,
            "line_item_rows": 0,
            "exact_amount_rows": 0,
            "normalized_amount_rows": 0,
            "protocol_rows": 0,
            "one_submitted": 0,
            "one_admitted": 0,
            "abandoned": 0,
            "outcomes": Counter(),
            "weaknesses": [],
            "submitted_counts": [],
            "admitted_counts": [],
            "discounts": [],
            "discount_incomparable": 0,
            "discount_suspicious": 0,
            "pricing_modes": Counter(),
            "buyers": set(),
            "buyer_procurements": Counter(),
        }
    )

    for row in rows:
        purchase_number = row["purchase_number"]
        doc_types = documents.get(purchase_number, [])
        if competition_exclusion_reason(row["purchase_type"], doc_types=doc_types) is not None:
            continue

        selected = select_competition_protocol(
            row["purchase_type"], protocols.get(purchase_number, [])
        )
        pricing_mode = pricing_modes.get(purchase_number, "unknown")
        key = (row["code_system"], row["code"])
        g = grouped[key]
        if row["label"]:
            g["labels"].append(row["label"])
        g["purchases"].add(purchase_number)
        purchase_buyers = buyers.get(purchase_number, set())
        g["buyers"].update(purchase_buyers)
        for buyer in purchase_buyers:
            g["buyer_procurements"][buyer] += 1
        if row["published_at"]:
            g["months"].add(str(row["published_at"])[:7])
        if row["item_demand"] is not None:
            g["demand_rub"] += float(row["item_demand"])
            g["amount_rows"] += 1
        if str(row["demand_basis"]).startswith("items_"):
            g["line_item_rows"] += 1
        if row["demand_basis"] == "items_exact":
            g["exact_amount_rows"] += 1
        if row["demand_basis"] == "items_budget_normalized":
            g["normalized_amount_rows"] += 1

        if selected is not None:
            g["pricing_modes"][pricing_mode] += 1
            applications_count = int(selected["applications_count"] or 0)
            admitted_count = int(selected["admitted_count"] or 0)
            g["protocol_rows"] += 1
            g["submitted_counts"].append(applications_count)
            g["admitted_counts"].append(admitted_count)
            if applications_count == 1:
                g["one_submitted"] += 1
            if admitted_count == 1:
                g["one_admitted"] += 1
            if int(selected["is_abandoned"] or 0) == 1:
                g["abandoned"] += 1
            outcome = classify_protocol_outcome(selected)
            g["outcomes"][outcome.code] += 1
            g["weaknesses"].append(protocol_weakness(selected))
            if (
                row["max_price"] is not None
                and float(row["max_price"]) > 0
                and selected["final_price"] is not None
            ):
                max_price = float(row["max_price"])
                final_price = float(selected["final_price"])
                if pricing_mode == "maximum_contract_price":
                    # EIS maximum-contract-value procurements (undefined quantity
                    # or formula pricing) can report a unit-price basis in the
                    # protocol. It is not comparable with the total max_price.
                    g["discount_incomparable"] += 1
                elif (
                    pricing_mode == "unknown"
                    and final_price >= 0
                    and final_price < max_price * 0.05
                ):
                    # Conservative guard for legacy/full-detail rows where the
                    # pricing-mode flags are absent but the scales are plainly
                    # incompatible. Do not turn a unit-price bid into a fake
                    # 95-100% market discount.
                    g["discount_suspicious"] += 1
                else:
                    discount = max(
                        0.0,
                        (max_price - final_price) / max_price,
                    )
                    g["discounts"].append(discount)

    result: list[dict[str, Any]] = []
    for (system, code), g in grouped.items():
        procurement_count = len(g["purchases"])
        if procurement_count < min_procurements:
            continue
        if max_procurements is not None and procurement_count > max_procurements:
            continue
        protocol_coverage = g["protocol_rows"] / procurement_count if procurement_count else 0.0
        if protocol_coverage < min_protocol_coverage:
            continue

        protocol_count = g["protocol_rows"]
        one_submitted_share = g["one_submitted"] / protocol_count if protocol_count else 0.0
        one_admitted_share = g["one_admitted"] / protocol_count if protocol_count else 0.0
        abandoned_share = g["abandoned"] / protocol_count if protocol_count else 0.0
        weak_competition_score = (
            sum(g["weaknesses"]) / len(g["weaknesses"]) if g["weaknesses"] else 0.0
        )
        has_discount_data = bool(g["discounts"])
        median_discount_share = median(g["discounts"]) if has_discount_data else 0.25
        discount_coverage = len(g["discounts"]) / protocol_count if protocol_count else 0.0
        median_applications = median(g["submitted_counts"]) if g["submitted_counts"] else None
        median_admitted = median(g["admitted_counts"]) if g["admitted_counts"] else None
        repeat_months = len(g["months"])
        recurrence_score = min(1.0, max(0.0, (repeat_months - 1) / 5.0))
        buyer_count = len(g["buyers"])
        top_buyer_purchases = max(g["buyer_procurements"].values(), default=0)
        top_buyer_share = top_buyer_purchases / procurement_count if procurement_count else 0.0
        buyer_diversity = _buyer_diversity_score(
            buyer_count=buyer_count, top_buyer_share=top_buyer_share
        )
        amount_coverage = g["amount_rows"] / procurement_count if procurement_count else 0.0
        line_item_coverage = g["line_item_rows"] / procurement_count if procurement_count else 0.0
        exact_amount_coverage = g["exact_amount_rows"] / procurement_count if procurement_count else 0.0
        normalized_amount_coverage = (
            g["normalized_amount_rows"] / procurement_count if procurement_count else 0.0
        )
        lower_confidence_amount_coverage = max(
            0.0,
            amount_coverage - exact_amount_coverage - normalized_amount_coverage,
        )
        demand_confidence = min(
            1.0,
            exact_amount_coverage
            + 0.65 * normalized_amount_coverage
            + 0.35 * lower_confidence_amount_coverage,
        )
        score = opportunity_score(
            OpportunityInput(
                demand_rub=g["demand_rub"],
                procurement_count=procurement_count,
                weak_competition_score=weak_competition_score,
                median_discount_share=median_discount_share,
                recurrence_score=recurrence_score,
                protocol_coverage=protocol_coverage,
                protocol_count=protocol_count,
                buyer_diversity_score=buyer_diversity,
                discount_coverage=discount_coverage,
            )
        )
        labels = g["labels"]
        label = max(set(labels), key=labels.count) if labels else None
        market_segment = classify_market_segment(
            code_system=system, code=code, label=label
        )
        gap = _gap_metrics(g["outcomes"], protocol_count)
        if segment is not None and market_segment.code != segment:
            continue
        if group is not None and market_segment.group != group:
            continue
        if gap_type is not None and gap["gap_type"] != gap_type:
            continue
        outcome_counts = dict(sorted(g["outcomes"].items()))
        result.append(
            {
                "score": score,
                "analysis_level": analysis_level,
                "system": system,
                "code": code,
                "label": label,
                "segment": market_segment.code,
                "segment_label": market_segment.label,
                "group": market_segment.group,
                "demand_rub": round(g["demand_rub"], 2),
                "procurements": procurement_count,
                "signal_tier": "new" if procurement_count == 1 else "established",
                "repeat_months": repeat_months,
                "protocol_count": protocol_count,
                "protocol_coverage": round(protocol_coverage, 3),
                "weak_competition_score": round(weak_competition_score, 3),
                "one_submitted_share": round(one_submitted_share, 3),
                "one_admitted_share": round(one_admitted_share, 3),
                "abandoned_share": round(abandoned_share, 3),
                # Compatibility aliases for older JSON consumers.
                "one_bid_share": round(one_submitted_share, 3),
                "failed_share": round(abandoned_share, 3),
                "outcome_counts": outcome_counts,
                "median_applications": median_applications,
                "median_admitted": median_admitted,
                "median_discount_pct": (
                    round(median_discount_share * 100.0, 2) if has_discount_data else None
                ),
                "discount_coverage": round(discount_coverage, 3),
                "discount_incomparable": int(g["discount_incomparable"]),
                "discount_suspicious": int(g["discount_suspicious"]),
                "pricing_modes": dict(sorted(g["pricing_modes"].items())),
                "amount_coverage": round(amount_coverage, 3),
                "line_item_coverage": round(line_item_coverage, 3),
                "exact_amount_coverage": round(exact_amount_coverage, 3),
                "normalized_amount_coverage": round(normalized_amount_coverage, 3),
                "demand_confidence": round(demand_confidence, 3),
                "buyers_count": buyer_count,
                "top_buyer_share": round(top_buyer_share, 3),
                "buyer_diversity_score": round(buyer_diversity, 3),
                **gap,
            }
        )

    needed_okpd2 = {
        code
        for item in result
        if (code := _base_okpd2_code(str(item["system"]), str(item["code"])))
    }
    rzn_state = _rzn_enrichment_state(conn)
    rzn_counts = (
        _rzn_counts_by_okpd2(conn, needed_okpd2) if rzn_state != "unavailable" else {}
    )
    gisp_state = _gisp_enrichment_state(conn)
    gisp_counts = (
        _gisp_counts_by_okpd2(conn, needed_okpd2) if gisp_state != "unavailable" else {}
    )
    contract_supplier_state = _contract_supplier_enrichment_state(
        conn, region_code=region_code
    )
    contracts_by_purchase = (
        _contracts_by_purchase(conn, region_code=region_code)
        if contract_supplier_state != "unavailable"
        else {}
    )
    for item in result:
        okpd2_code = _base_okpd2_code(str(item["system"]), str(item["code"]))
        counts = rzn_counts.get(okpd2_code or "", {})
        item["rzn_enrichment"] = rzn_state
        item["rzn_okpd2_code"] = okpd2_code
        if rzn_state == "unavailable":
            rzn_match = "unavailable"
        elif counts:
            rzn_match = "parent_okpd2" if item["system"] == "ktru" else "exact_okpd2"
        else:
            rzn_match = "no_exact_okpd2"
        item["rzn_match_state"] = rzn_match
        item["rzn_active_products"] = int(counts.get("products", 0))
        item["rzn_active_producers"] = int(counts.get("producers", 0))
        item["rzn_active_representatives"] = int(counts.get("representatives", 0))
        gcounts = gisp_counts.get(okpd2_code or "", {})
        gisp_has_match = bool(gcounts)
        gisp_match = "unavailable"
        if gisp_state != "unavailable":
            if gisp_has_match:
                gisp_match = "parent_okpd2" if item["system"] == "ktru" else "exact_okpd2"
            else:
                gisp_match = "none"
        gisp_applicability = _gisp_applicability(
            group=str(item["group"]),
            segment=str(item["segment"]),
            state=gisp_state,
            has_match=gisp_has_match,
        )
        gisp_products = int(gcounts.get("products", 0))
        gisp_manufacturers = int(gcounts.get("manufacturers", 0))
        item["gisp_enrichment"] = gisp_state
        item["gisp_okpd2_code"] = okpd2_code
        item["gisp_match_state"] = gisp_match
        item["gisp_applicability"] = gisp_applicability
        item["gisp_active_products"] = gisp_products
        item["gisp_active_manufacturers"] = gisp_manufacturers
        item["gisp_parent_products"] = gisp_products if item["system"] == "ktru" else None
        item["gisp_parent_manufacturers"] = (
            gisp_manufacturers if item["system"] == "ktru" else None
        )
        item["contract_supplier_enrichment"] = contract_supplier_state
        if contract_supplier_state == "unavailable":
            item.update(
                {
                    "known_contract_suppliers": None,
                    "known_contracts": None,
                    "contract_linked_purchases": 0,
                    "contract_purchase_coverage": 0.0,
                    "winner_linked_purchases": 0,
                    "winner_purchase_coverage": 0.0,
                    "winner_value_coverage": 0.0,
                    "winner_code_match_coverage": 0.0,
                    "winner_observed_value_rub": 0.0,
                    "winner_bundle_value_rub": 0.0,
                    "winner_contract_ids": [],
                    "winner_exact_contracts": 0,
                    "winner_fallback_contracts": 0,
                    "top_supplier_inn": None,
                    "top1_supplier_share": None,
                    "top3_supplier_share": None,
                    "supplier_hhi": None,
                    "supplier_concentration_basis": None,
                    "supplier_concentration_match": "unavailable",
                    "supplier_concentration_evidence": "none",
                }
            )
        else:
            market_key = (str(item["system"]), str(item["code"]))
            item.update(
                _contract_concentration(
                    set(grouped[market_key]["purchases"]),
                    contracts_by_purchase,
                    system=str(item["system"]),
                    code=str(item["code"]),
                )
            )

        distribution_score = None
        if gisp_applicability == "applicable":
            distribution_score = _distribution_gap_score(
                supplier_gap_share=float(item["supplier_gap_share"]),
                manufacturer_count=gisp_manufacturers,
                buyer_diversity_score=float(item["buyer_diversity_score"]),
                protocol_coverage=float(item["protocol_coverage"]),
            )
        item["distribution_gap_score"] = distribution_score

        manufacturer_score = None
        demand_per_manufacturer = None
        procurements_per_manufacturer = None
        if (
            item["system"] == "okpd2"
            and gisp_applicability == "applicable"
            and gisp_manufacturers > 0
        ):
            demand_per_manufacturer = round(
                float(item["demand_rub"]) / gisp_manufacturers, 2
            )
            procurements_per_manufacturer = round(
                int(item["procurements"]) / gisp_manufacturers, 3
            )
            manufacturer_score = manufacturing_gap_score(
                ManufacturingInput(
                    demand_rub=float(item["demand_rub"]),
                    procurement_count=int(item["procurements"]),
                    manufacturer_count=gisp_manufacturers,
                    buyer_diversity_score=float(item["buyer_diversity_score"]),
                    demand_confidence=float(item["demand_confidence"]),
                )
            )
        item["manufacturer_gap_score"] = manufacturer_score
        item["demand_per_manufacturer"] = demand_per_manufacturer
        item["procurements_per_manufacturer"] = procurements_per_manufacturer

    result.sort(key=lambda item: (item["score"], item["demand_rub"]), reverse=True)
    return result


def compute_manufacturing_opportunities(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    min_procurements: int = 2,
    min_protocol_coverage: float = 0.5,
    segment: str | None = None,
    include_unknown_gisp: bool = False,
) -> list[dict[str, Any]]:
    """Rank OKPD2 markets by demand relative to registered manufacturers.

    KTRU items are first rolled up to their parent OKPD2 so one purchase is not
    counted repeatedly at the manufacturing-market level. Markets without a
    positive GISP match are kept out by default because 0 rows in PP719 is not
    proof that zero Russian manufacturers exist in that product class.
    """
    rows = compute_opportunities(
        conn,
        region_code=region_code,
        min_procurements=min_procurements,
        min_protocol_coverage=min_protocol_coverage,
        segment=segment,
        group="goods",
        analysis_level="okpd2",
    )
    if not include_unknown_gisp:
        rows = [row for row in rows if row["manufacturer_gap_score"] is not None]
    rows.sort(
        key=lambda item: (
            item["manufacturer_gap_score"] if item["manufacturer_gap_score"] is not None else -1.0,
            item["demand_rub"],
        ),
        reverse=True,
    )
    return rows


def compute_winner_concentration(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    min_procurements: int = 2,
    min_protocol_coverage: float = 0.5,
    min_contract_coverage: float = 0.3,
    min_contracts: int = 2,
    segment: str | None = None,
    group: str | None = "goods",
) -> list[dict[str, Any]]:
    """Rank markets by observed winner concentration without altering OpportunityScore."""
    rows = compute_opportunities(
        conn,
        region_code=region_code,
        min_procurements=min_procurements,
        min_protocol_coverage=min_protocol_coverage,
        segment=segment,
        group=group,
    )
    rows = [
        row
        for row in rows
        if row.get("top1_supplier_share") is not None
        and int(row.get("known_contracts") or 0) >= min_contracts
        and float(row.get("winner_purchase_coverage") or 0.0) >= min_contract_coverage
    ]
    evidence_rank = {"high": 2, "medium": 1, "low": 0, "none": -1}
    rows.sort(
        key=lambda item: (
            evidence_rank.get(str(item.get("supplier_concentration_evidence")), -1),
            float(item.get("top1_supplier_share") or 0.0),
            float(item.get("supplier_hhi") or 0.0),
            float(item["demand_rub"]),
        ),
        reverse=True,
    )
    return rows


def _winner_bundle_id(system: str, contract_ids: tuple[int, ...]) -> str:
    payload = f"{system}:" + ",".join(str(value) for value in contract_ids)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def compute_winner_bundles(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    min_procurements: int = 2,
    min_protocol_coverage: float = 0.5,
    min_contract_coverage: float = 0.3,
    min_contracts: int = 2,
    min_markets: int = 2,
    segment: str | None = None,
    group: str | None = "goods",
) -> list[dict[str, Any]]:
    """Collapse markets that rely on the exact same contract-winner evidence.

    A multi-position contract can list many KTRU codes. If the same two contracts
    contain fourteen KTRU codes, fourteen 100%-winner rows are not fourteen
    independent observations. This view groups such rows by their contributing
    contract IDs, preserving the shared winner distribution once.
    """
    rows = compute_winner_concentration(
        conn,
        region_code=region_code,
        min_procurements=min_procurements,
        min_protocol_coverage=min_protocol_coverage,
        min_contract_coverage=min_contract_coverage,
        min_contracts=min_contracts,
        segment=segment,
        group=group,
    )

    grouped: dict[tuple[str, tuple[int, ...]], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        contract_ids = tuple(int(value) for value in row.get("winner_contract_ids") or [])
        if not contract_ids:
            continue
        grouped[(str(row["system"]), contract_ids)].append(row)

    bundles: list[dict[str, Any]] = []
    evidence_rank = {"high": 2, "medium": 1, "low": 0, "none": -1}
    for (system, contract_ids), markets in grouped.items():
        if len(markets) < min_markets:
            continue
        markets = sorted(
            markets,
            key=lambda item: (-float(item.get("demand_rub") or 0.0), str(item.get("code") or "")),
        )
        first = markets[0]
        bundle = {
            "bundle_id": _winner_bundle_id(system, contract_ids),
            "system": system,
            "contract_ids": list(contract_ids),
            "contracts": len(contract_ids),
            "markets_count": len(markets),
            "codes": [str(item["code"]) for item in markets],
            "labels": [str(item["label"]) for item in markets],
            "max_market_demand_rub": round(
                max(float(item["demand_rub"]) for item in markets), 2
            ),
            "min_market_contract_coverage": round(
                min(float(item.get("winner_purchase_coverage") or 0.0) for item in markets), 3
            ),
            "max_market_contract_coverage": round(
                max(float(item.get("winner_purchase_coverage") or 0.0) for item in markets), 3
            ),
            "min_supplier_gap_share": round(
                min(float(item.get("supplier_gap_share") or 0.0) for item in markets), 3
            ),
            "max_supplier_gap_share": round(
                max(float(item.get("supplier_gap_share") or 0.0) for item in markets), 3
            ),
            "top_supplier_inn": first.get("top_supplier_inn"),
            "top1_supplier_share": first.get("top1_supplier_share"),
            "top3_supplier_share": first.get("top3_supplier_share"),
            "supplier_hhi": first.get("supplier_hhi"),
            "known_contract_suppliers": first.get("known_contract_suppliers", 0),
            "supplier_concentration_basis": first.get("supplier_concentration_basis"),
            "supplier_concentration_match": first.get("supplier_concentration_match"),
            "supplier_concentration_evidence": first.get("supplier_concentration_evidence", "none"),
            "winner_code_match_coverage": min(
                float(item.get("winner_code_match_coverage") or 0.0) for item in markets
            ),
            "winner_value_coverage": min(
                float(item.get("winner_value_coverage") or 0.0) for item in markets
            ),
            "winner_bundle_value_rub": max(
                float(item.get("winner_bundle_value_rub") or 0.0) for item in markets
            ),
            "winner_allocated_value_per_market_rub": round(
                float(first.get("winner_observed_value_rub") or 0.0), 2
            ),
        }
        bundles.append(bundle)

    bundles.sort(
        key=lambda item: (
            evidence_rank.get(str(item.get("supplier_concentration_evidence")), -1),
            float(item.get("top1_supplier_share") or 0.0),
            int(item.get("markets_count") or 0),
            float(item.get("winner_bundle_value_rub") or 0.0),
        ),
        reverse=True,
    )
    return bundles


def compute_supplier_profile(
    conn: sqlite3.Connection,
    *,
    supplier_inn: str,
    region_code: int,
    limit: int = 10,
) -> dict[str, Any]:
    """Summarize one observed contract supplier without double-counting multi-party contracts.

    Contract value is first divided by the number of suppliers on the contract. For
    market views, that supplier-attributed value is then divided across the contract's
    codes at the requested classification level. KTRU codes that rely on the same set
    of supplier contracts are grouped into evidence bundles so one multi-position
    procurement does not look like many independent wins.
    """
    supplier_inn = str(supplier_inn).strip()
    if not supplier_inn:
        raise ValueError("supplier_inn must not be empty")
    if limit <= 0:
        raise ValueError("limit must be positive")

    contracts = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                c.id AS contract_id,
                c.reg_num,
                c.purchase_number,
                c.customer_inn,
                c.price,
                c.subject,
                c.published_at,
                c.stage,
                (SELECT COUNT(*) FROM contract_suppliers cs2
                 WHERE cs2.contract_id=c.id) AS supplier_count
            FROM contracts c
            JOIN contract_suppliers cs ON cs.contract_id=c.id
            WHERE cs.inn=? AND c.region_code=?
            ORDER BY COALESCE(c.published_at, '') DESC, c.id DESC
            """,
            (supplier_inn, region_code),
        ).fetchall()
    ]

    codes_by_contract: dict[int, dict[str, set[str]]] = defaultdict(
        lambda: {"ktru": set(), "okpd2": set()}
    )
    if contracts:
        for row in conn.execute(
            """
            SELECT cc.contract_id, cc.system, cc.code
            FROM contract_codes cc
            JOIN contracts c ON c.id=cc.contract_id
            JOIN contract_suppliers cs ON cs.contract_id=c.id
            WHERE cs.inn=? AND c.region_code=?
            """,
            (supplier_inn, region_code),
        ):
            system = str(row["system"])
            code = str(row["code"])
            if system in {"ktru", "okpd2"} and code:
                codes_by_contract[int(row["contract_id"])][system].add(code)

    gross_value = 0.0
    supplier_value = 0.0
    priced_contracts = 0
    multi_supplier_contracts = 0
    contract_supplier_value: dict[int, float] = {}
    contract_gross_value: dict[int, float] = {}
    purchases: set[str] = set()
    customers: set[str] = set()
    customer_contracts: Counter[str] = Counter()
    customer_purchases: dict[str, set[str]] = defaultdict(set)
    customer_value: Counter[str] = Counter()
    known_customer_contracts = 0

    market_stats: dict[str, dict[str, dict[str, Any]]] = {"okpd2": {}, "ktru": {}}
    market_contract_ids: dict[str, dict[str, set[int]]] = {
        "okpd2": defaultdict(set),
        "ktru": defaultdict(set),
    }

    for contract in contracts:
        contract_id = int(contract["contract_id"])
        supplier_count = max(1, int(contract.get("supplier_count") or 0))
        if supplier_count > 1:
            multi_supplier_contracts += 1
        purchase_number = str(contract.get("purchase_number") or "")
        if purchase_number:
            purchases.add(purchase_number)
        customer_inn = str(contract.get("customer_inn") or "").strip()
        if customer_inn:
            customers.add(customer_inn)
            known_customer_contracts += 1
            customer_contracts[customer_inn] += 1
            if purchase_number:
                customer_purchases[customer_inn].add(purchase_number)

        price = contract.get("price")
        allocated_value: float | None = None
        if price is not None and float(price) >= 0:
            gross = float(price)
            allocated_value = gross / supplier_count
            priced_contracts += 1
            gross_value += gross
            supplier_value += allocated_value
            contract_supplier_value[contract_id] = allocated_value
            contract_gross_value[contract_id] = gross
            if customer_inn:
                customer_value[customer_inn] += allocated_value

        ktru_codes = set(codes_by_contract[contract_id]["ktru"])
        explicit_okpd2 = set(codes_by_contract[contract_id]["okpd2"])
        parent_okpd2 = {
            base
            for code in ktru_codes
            if (base := _base_okpd2_code("ktru", code)) is not None
        }
        codes_by_level = {"ktru": ktru_codes, "okpd2": explicit_okpd2 | parent_okpd2}
        for system, codes in codes_by_level.items():
            if not codes:
                continue
            per_code_value = allocated_value / len(codes) if allocated_value is not None else 0.0
            for code in codes:
                stat = market_stats[system].setdefault(
                    code,
                    {
                        "system": system,
                        "code": code,
                        "contracts": 0,
                        "purchases": set(),
                        "supplier_value_rub": 0.0,
                        "priced_contracts": 0,
                    },
                )
                stat["contracts"] += 1
                if purchase_number:
                    stat["purchases"].add(purchase_number)
                if allocated_value is not None:
                    stat["supplier_value_rub"] += per_code_value
                    stat["priced_contracts"] += 1
                market_contract_ids[system][code].add(contract_id)

    value_coverage = priced_contracts / len(contracts) if contracts else 0.0
    customer_coverage = known_customer_contracts / len(contracts) if contracts else 0.0

    customer_basis = "contract_count"
    customer_distribution: Counter[str] = customer_contracts.copy()
    if value_coverage >= 0.80 and customer_value and sum(customer_value.values()) > 0:
        customer_basis = "contract_value"
        customer_distribution = customer_value

    top_customer_inn = None
    top_customer_share = None
    customer_hhi = None
    if customer_distribution:
        total = float(sum(customer_distribution.values()))
        ranked_customers = sorted(
            customer_distribution.items(), key=lambda item: (-float(item[1]), item[0])
        )
        shares = [(inn, float(value) / total) for inn, value in ranked_customers]
        top_customer_inn = shares[0][0]
        top_customer_share = shares[0][1]
        customer_hhi = sum(share * share for _, share in shares) * 10_000.0

    repeated_customer_contracts = sum(count for count in customer_contracts.values() if count >= 2)
    repeat_customer_contract_share = repeated_customer_contracts / len(contracts) if contracts else 0.0

    customer_rows: list[dict[str, Any]] = []
    customer_total_for_share = float(sum(customer_distribution.values())) if customer_distribution else 0.0
    for inn, contract_count in customer_contracts.items():
        basis_value = float(customer_distribution.get(inn, 0.0))
        customer_rows.append(
            {
                "customer_inn": inn,
                "contracts": int(contract_count),
                "purchases": len(customer_purchases[inn]),
                "supplier_value_rub": round(float(customer_value.get(inn, 0.0)), 2),
                "share": round(basis_value / customer_total_for_share, 4)
                if customer_total_for_share > 0
                else None,
                "share_basis": customer_basis,
            }
        )
    customer_rows.sort(
        key=lambda item: (
            float(item.get("share") or 0.0),
            float(item.get("supplier_value_rub") or 0.0),
            int(item.get("contracts") or 0),
        ),
        reverse=True,
    )

    bundle_ids_by_market: dict[tuple[str, str], str] = {}
    bundle_rows: list[dict[str, Any]] = []
    for system in ("ktru", "okpd2"):
        grouped_codes: dict[tuple[int, ...], list[str]] = defaultdict(list)
        for code, ids in market_contract_ids[system].items():
            if ids:
                grouped_codes[tuple(sorted(ids))].append(code)
        for ids, codes in grouped_codes.items():
            if len(codes) < 2:
                continue
            bundle_id = _winner_bundle_id(system, ids)
            sorted_codes = sorted(codes)
            for code in sorted_codes:
                bundle_ids_by_market[(system, code)] = bundle_id
            bundle_rows.append(
                {
                    "bundle_id": bundle_id,
                    "system": system,
                    "contracts": len(ids),
                    "contract_ids": list(ids),
                    "codes_count": len(sorted_codes),
                    "codes": sorted_codes,
                    "supplier_value_rub": round(
                        sum(contract_supplier_value.get(contract_id, 0.0) for contract_id in ids),
                        2,
                    ),
                    "gross_contract_value_rub": round(
                        sum(contract_gross_value.get(contract_id, 0.0) for contract_id in ids),
                        2,
                    ),
                }
            )
    bundle_rows.sort(
        key=lambda item: (
            int(item["contracts"]),
            int(item["codes_count"]),
            float(item["supplier_value_rub"]),
        ),
        reverse=True,
    )

    market_rows: dict[str, list[dict[str, Any]]] = {"okpd2": [], "ktru": []}
    for system in ("okpd2", "ktru"):
        for code, stat in market_stats[system].items():
            contracts_count = int(stat["contracts"])
            market_rows[system].append(
                {
                    "system": system,
                    "code": code,
                    "contracts": contracts_count,
                    "purchases": len(stat["purchases"]),
                    "supplier_value_rub": round(float(stat["supplier_value_rub"]), 2),
                    "value_coverage": round(
                        int(stat["priced_contracts"]) / contracts_count if contracts_count else 0.0,
                        3,
                    ),
                    "bundle_id": bundle_ids_by_market.get((system, code)),
                }
            )
        market_rows[system].sort(
            key=lambda item: (
                float(item["supplier_value_rub"]),
                int(item["contracts"]),
                item["code"],
            ),
            reverse=True,
        )

    recent_contracts: list[dict[str, Any]] = []
    for contract in contracts[:limit]:
        contract_id = int(contract["contract_id"])
        recent_contracts.append(
            {
                "contract_id": contract_id,
                "reg_num": contract.get("reg_num"),
                "purchase_number": contract.get("purchase_number"),
                "customer_inn": contract.get("customer_inn"),
                "price": round(float(contract["price"]), 2)
                if contract.get("price") is not None
                else None,
                "supplier_value_rub": round(contract_supplier_value.get(contract_id, 0.0), 2)
                if contract_id in contract_supplier_value
                else None,
                "supplier_count": int(contract.get("supplier_count") or 0),
                "ktru_count": len(codes_by_contract[contract_id]["ktru"]),
                "okpd2_count": len(codes_by_contract[contract_id]["okpd2"]),
                "published_at": contract.get("published_at"),
                "subject": contract.get("subject"),
            }
        )

    return {
        "supplier_inn": supplier_inn,
        "region_code": region_code,
        "contracts": len(contracts),
        "purchases": len(purchases),
        "customers": len(customers),
        "gross_contract_value_rub": round(gross_value, 2),
        "supplier_attributed_value_rub": round(supplier_value, 2),
        "value_coverage": round(value_coverage, 3),
        "customer_coverage": round(customer_coverage, 3),
        "multi_supplier_contracts": multi_supplier_contracts,
        "top_customer_inn": top_customer_inn,
        "top_customer_share": round(top_customer_share, 4) if top_customer_share is not None else None,
        "customer_hhi": round(customer_hhi, 2) if customer_hhi is not None else None,
        "customer_concentration_basis": customer_basis if customer_distribution else None,
        "repeat_customer_contract_share": round(repeat_customer_contract_share, 3),
        "ktru_codes": len(market_rows["ktru"]),
        "okpd2_codes": len(market_rows["okpd2"]),
        "code_value_allocation": "equal_split_within_contract",
        "customers_ranked": customer_rows[:limit],
        "bundles": bundle_rows[:limit],
        "okpd2_markets": market_rows["okpd2"][:limit],
        "ktru_markets": market_rows["ktru"][:limit],
        "recent_contracts": recent_contracts,
    }


def compute_buyer_supplier_relationships(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    min_contracts: int = 2,
    min_value_rub: float = 100_000.0,
    min_buyer_share: float = 0.0,
    min_supplier_dependency: float = 0.0,
) -> list[dict[str, Any]]:
    """Rank observed buyer-supplier relationships in one streaming JOIN pass.

    The previous implementation loaded contracts, built a potentially huge ``IN``
    clause for every contract id, and then loaded suppliers separately.  This version
    lets SQLite use the region/contract indexes and streams one row per
    contract-supplier edge.  Contract totals are still counted once per contract and
    multi-supplier value is still split equally, so semantics are unchanged.
    """
    if min_contracts <= 0:
        raise ValueError("min_contracts must be positive")
    if min_value_rub < 0:
        raise ValueError("min_value_rub must be non-negative")

    rows = conn.execute(
        """
        WITH supplier_counts AS (
            SELECT contract_id, COUNT(*) AS supplier_count
            FROM contract_suppliers
            GROUP BY contract_id
        )
        SELECT
            c.id AS contract_id,
            c.purchase_number,
            c.customer_inn,
            c.price,
            cs.inn AS supplier_inn,
            sc.supplier_count
        FROM contracts c
        JOIN supplier_counts sc ON sc.contract_id=c.id
        JOIN contract_suppliers cs ON cs.contract_id=c.id
        WHERE c.region_code=?
          AND c.customer_inn IS NOT NULL AND TRIM(c.customer_inn)<>''
        ORDER BY c.id, cs.inn
        """,
        (region_code,),
    )

    buyer_contracts: Counter[str] = Counter()
    buyer_priced: Counter[str] = Counter()
    buyer_value: Counter[str] = Counter()
    supplier_contracts: Counter[str] = Counter()
    supplier_priced: Counter[str] = Counter()
    supplier_value: Counter[str] = Counter()
    pair_contracts: Counter[tuple[str, str]] = Counter()
    pair_priced: Counter[tuple[str, str]] = Counter()
    pair_value: Counter[tuple[str, str]] = Counter()
    pair_count_weight: Counter[tuple[str, str]] = Counter()
    pair_purchases: dict[tuple[str, str], set[str]] = defaultdict(set)

    last_contract_id: int | None = None
    saw_any = False
    for row in rows:
        saw_any = True
        contract_id = int(row["contract_id"])
        buyer = str(row["customer_inn"] or "").strip()
        supplier = str(row["supplier_inn"] or "").strip()
        if not buyer or not supplier:
            continue
        supplier_count = max(1, int(row["supplier_count"] or 0))
        price = row["price"]
        priced = price is not None and float(price) >= 0
        gross = float(price) if priced else 0.0

        if contract_id != last_contract_id:
            buyer_contracts[buyer] += 1
            if priced:
                buyer_priced[buyer] += 1
                buyer_value[buyer] += gross
            last_contract_id = contract_id

        pair = (buyer, supplier)
        supplier_contracts[supplier] += 1
        pair_contracts[pair] += 1
        pair_count_weight[pair] += 1.0 / supplier_count
        purchase_number = str(row["purchase_number"] or "").strip()
        if purchase_number:
            pair_purchases[pair].add(purchase_number)
        if priced:
            attributed = gross / supplier_count
            supplier_priced[supplier] += 1
            supplier_value[supplier] += attributed
            pair_priced[pair] += 1
            pair_value[pair] += attributed

    if not saw_any:
        return []

    def _materiality(value: float) -> float:
        floor = 250_000.0
        cap = 5_000_000.0
        if value <= floor:
            return max(0.0, value / floor * 0.15)
        if value >= cap:
            return 1.0
        return 0.15 + 0.85 * (math.log(value / floor) / math.log(cap / floor))

    result: list[dict[str, Any]] = []
    for (buyer, supplier), contracts_count in pair_contracts.items():
        if contracts_count < min_contracts:
            continue
        attributed_value = float(pair_value.get((buyer, supplier), 0.0))
        if attributed_value < min_value_rub:
            continue

        buyer_value_cov = buyer_priced[buyer] / buyer_contracts[buyer] if buyer_contracts[buyer] else 0.0
        supplier_value_cov = (
            supplier_priced[supplier] / supplier_contracts[supplier]
            if supplier_contracts[supplier]
            else 0.0
        )
        pair_value_cov = pair_priced[(buyer, supplier)] / contracts_count

        if buyer_value_cov >= 0.80 and buyer_value[buyer] > 0:
            buyer_basis = "contract_value"
            buyer_share = attributed_value / float(buyer_value[buyer])
        else:
            buyer_basis = "contract_count"
            buyer_share = float(pair_count_weight[(buyer, supplier)]) / buyer_contracts[buyer]

        if supplier_value_cov >= 0.80 and supplier_value[supplier] > 0:
            supplier_basis = "contract_value"
            supplier_dependency = attributed_value / float(supplier_value[supplier])
        else:
            supplier_basis = "contract_count"
            supplier_dependency = contracts_count / supplier_contracts[supplier]

        if buyer_share < min_buyer_share or supplier_dependency < min_supplier_dependency:
            continue

        mutual_dependency = math.sqrt(max(0.0, buyer_share * supplier_dependency))
        repeat_strength = min(1.0, math.log1p(contracts_count) / math.log1p(5))
        materiality = _materiality(attributed_value)
        balance = min(buyer_share, supplier_dependency)
        coverage = min(pair_value_cov, buyer_value_cov, supplier_value_cov)
        evidence_factor = 0.65 + 0.35 * coverage
        relationship_score = 100.0 * (
            0.50 * mutual_dependency
            + 0.20 * repeat_strength
            + 0.20 * materiality
            + 0.10 * balance
        ) * evidence_factor

        if buyer_share >= 0.60 and supplier_dependency >= 0.60:
            relationship_type = "mutual_concentration"
        elif supplier_dependency >= 0.70 and buyer_share < 0.60:
            relationship_type = "supplier_dependent"
        elif buyer_share >= 0.70 and supplier_dependency < 0.60:
            relationship_type = "buyer_dependent"
        else:
            relationship_type = "distributed"

        if contracts_count >= 4 and coverage >= 0.80:
            evidence = "high"
        elif contracts_count >= 2 and coverage >= 0.50:
            evidence = "medium"
        else:
            evidence = "low"

        result.append(
            {
                "buyer_inn": buyer,
                "supplier_inn": supplier,
                "contracts": int(contracts_count),
                "purchases": len(pair_purchases[(buyer, supplier)]),
                "pair_value_rub": round(attributed_value, 2),
                "buyer_share": round(buyer_share, 4),
                "supplier_dependency": round(supplier_dependency, 4),
                "mutual_dependency": round(mutual_dependency, 4),
                "buyer_region_contracts": int(buyer_contracts[buyer]),
                "supplier_region_contracts": int(supplier_contracts[supplier]),
                "buyer_value_coverage": round(buyer_value_cov, 3),
                "supplier_value_coverage": round(supplier_value_cov, 3),
                "pair_value_coverage": round(pair_value_cov, 3),
                "buyer_share_basis": buyer_basis,
                "supplier_dependency_basis": supplier_basis,
                "relationship_type": relationship_type,
                "relationship_score": round(relationship_score, 2),
                "evidence": evidence,
            }
        )

    result.sort(
        key=lambda item: (
            float(item["relationship_score"]),
            float(item["pair_value_rub"]),
            int(item["contracts"]),
        ),
        reverse=True,
    )
    return result

def compute_buyer_profile(
    conn: sqlite3.Connection,
    *,
    customer_inn: str,
    region_code: int,
    limit: int = 10,
) -> dict[str, Any]:
    """Summarize one buyer and its observed supplier relationships.

    Contract value is split equally across suppliers when a contract lists more than
    one supplier, so supplier shares sum back to the buyer's observed contract value.
    For each supplier we also compute how dependent that supplier is on this buyer
    within the currently loaded regional contract sample.
    """
    customer_inn = str(customer_inn).strip()
    if not customer_inn:
        raise ValueError("customer_inn must not be empty")
    if limit <= 0:
        raise ValueError("limit must be positive")

    contracts = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                c.id AS contract_id,
                c.reg_num,
                c.purchase_number,
                c.customer_inn,
                c.price,
                c.subject,
                c.published_at,
                c.stage,
                (SELECT COUNT(*) FROM contract_suppliers cs2
                 WHERE cs2.contract_id=c.id) AS supplier_count
            FROM contracts c
            WHERE c.customer_inn=? AND c.region_code=?
            ORDER BY COALESCE(c.published_at, '') DESC, c.id DESC
            """,
            (customer_inn, region_code),
        ).fetchall()
    ]

    supplier_by_contract: dict[int, list[str]] = defaultdict(list)
    codes_by_contract: dict[int, dict[str, set[str]]] = defaultdict(
        lambda: {"ktru": set(), "okpd2": set()}
    )
    if contracts:
        contract_ids = [int(row["contract_id"]) for row in contracts]
        placeholders = ",".join("?" for _ in contract_ids)
        for row in conn.execute(
            f"SELECT contract_id, inn FROM contract_suppliers WHERE contract_id IN ({placeholders})",
            contract_ids,
        ):
            inn = str(row["inn"] or "").strip()
            if inn:
                supplier_by_contract[int(row["contract_id"])].append(inn)
        for row in conn.execute(
            f"SELECT contract_id, system, code FROM contract_codes WHERE contract_id IN ({placeholders})",
            contract_ids,
        ):
            system = str(row["system"] or "")
            code = str(row["code"] or "")
            if system in {"ktru", "okpd2"} and code:
                codes_by_contract[int(row["contract_id"])][system].add(code)

    gross_value = 0.0
    priced_contracts = 0
    multi_supplier_contracts = 0
    purchases: set[str] = set()
    suppliers: set[str] = set()
    supplier_contracts: Counter[str] = Counter()
    supplier_purchases: dict[str, set[str]] = defaultdict(set)
    supplier_value: Counter[str] = Counter()
    contract_supplier_value: dict[tuple[int, str], float] = {}

    for contract in contracts:
        contract_id = int(contract["contract_id"])
        supplier_inns = sorted(set(supplier_by_contract.get(contract_id, [])))
        supplier_count = max(1, len(supplier_inns))
        if supplier_count > 1:
            multi_supplier_contracts += 1
        purchase_number = str(contract.get("purchase_number") or "")
        if purchase_number:
            purchases.add(purchase_number)
        for inn in supplier_inns:
            suppliers.add(inn)
            supplier_contracts[inn] += 1
            if purchase_number:
                supplier_purchases[inn].add(purchase_number)

        price = contract.get("price")
        if price is not None and float(price) >= 0:
            gross = float(price)
            gross_value += gross
            priced_contracts += 1
            per_supplier = gross / supplier_count
            for inn in supplier_inns:
                supplier_value[inn] += per_supplier
                contract_supplier_value[(contract_id, inn)] = per_supplier

    value_coverage = priced_contracts / len(contracts) if contracts else 0.0
    distribution_basis = "contract_count"
    distribution: Counter[str] = Counter()
    if value_coverage >= 0.80 and supplier_value and sum(supplier_value.values()) > 0:
        distribution_basis = "contract_value"
        distribution = supplier_value.copy()
    else:
        for contract in contracts:
            contract_id = int(contract["contract_id"])
            supplier_inns = sorted(set(supplier_by_contract.get(contract_id, [])))
            if not supplier_inns:
                continue
            weight = 1.0 / len(supplier_inns)
            for inn in supplier_inns:
                distribution[inn] += weight

    ranked_distribution = sorted(
        distribution.items(), key=lambda item: (-float(item[1]), item[0])
    )
    top_supplier_inn = None
    top_supplier_share = None
    supplier_hhi = None
    total_distribution = float(sum(distribution.values()))
    if ranked_distribution and total_distribution > 0:
        shares = [(inn, float(value) / total_distribution) for inn, value in ranked_distribution]
        top_supplier_inn = shares[0][0]
        top_supplier_share = shares[0][1]
        supplier_hhi = sum(share * share for _, share in shares) * 10_000.0

    recurring_suppliers = {inn for inn, count in supplier_contracts.items() if count >= 2}
    repeated_contracts = 0
    for contract in contracts:
        contract_id = int(contract["contract_id"])
        if recurring_suppliers.intersection(supplier_by_contract.get(contract_id, [])):
            repeated_contracts += 1
    repeat_supplier_contract_share = repeated_contracts / len(contracts) if contracts else 0.0

    # Regional supplier totals are computed from the loaded contract sample. This makes
    # it possible to distinguish "buyer depends on supplier" from "supplier depends
    # on buyer" without pretending the current sample is a complete market census.
    supplier_region_value: Counter[str] = Counter()
    supplier_region_contracts: Counter[str] = Counter()
    region_rows = conn.execute(
        """
        SELECT c.id AS contract_id, c.price, cs.inn,
               (SELECT COUNT(*) FROM contract_suppliers cs2 WHERE cs2.contract_id=c.id) AS supplier_count
        FROM contracts c
        JOIN contract_suppliers cs ON cs.contract_id=c.id
        WHERE c.region_code=?
        """,
        (region_code,),
    ).fetchall()
    for row in region_rows:
        inn = str(row["inn"] or "").strip()
        if not inn:
            continue
        supplier_region_contracts[inn] += 1
        price = row["price"]
        if price is not None and float(price) >= 0:
            supplier_count = max(1, int(row["supplier_count"] or 0))
            supplier_region_value[inn] += float(price) / supplier_count

    supplier_rows: list[dict[str, Any]] = []
    for inn, contract_count in supplier_contracts.items():
        basis_value = float(distribution.get(inn, 0.0))
        buyer_share = basis_value / total_distribution if total_distribution > 0 else None
        pair_value = float(supplier_value.get(inn, 0.0))
        regional_value = float(supplier_region_value.get(inn, 0.0))
        supplier_dependency = pair_value / regional_value if regional_value > 0 else None
        mutual_dependency = None
        if buyer_share is not None and supplier_dependency is not None:
            mutual_dependency = math.sqrt(buyer_share * supplier_dependency)
        supplier_rows.append(
            {
                "supplier_inn": inn,
                "contracts": int(contract_count),
                "purchases": len(supplier_purchases[inn]),
                "buyer_attributed_value_rub": round(pair_value, 2),
                "buyer_share": round(buyer_share, 4) if buyer_share is not None else None,
                "share_basis": distribution_basis,
                "supplier_region_contracts": int(supplier_region_contracts.get(inn, 0)),
                "supplier_region_value_rub": round(regional_value, 2),
                "supplier_dependency_on_buyer": round(supplier_dependency, 4)
                if supplier_dependency is not None
                else None,
                "mutual_dependency": round(mutual_dependency, 4)
                if mutual_dependency is not None
                else None,
            }
        )
    supplier_rows.sort(
        key=lambda item: (
            float(item.get("buyer_share") or 0.0),
            float(item.get("buyer_attributed_value_rub") or 0.0),
            int(item.get("contracts") or 0),
        ),
        reverse=True,
    )

    market_stats: dict[str, dict[str, dict[str, Any]]] = {"okpd2": {}, "ktru": {}}
    for contract in contracts:
        contract_id = int(contract["contract_id"])
        price = contract.get("price")
        ktru_codes = set(codes_by_contract[contract_id]["ktru"])
        explicit_okpd2 = set(codes_by_contract[contract_id]["okpd2"])
        parent_okpd2 = {
            base
            for code in ktru_codes
            if (base := _base_okpd2_code("ktru", code)) is not None
        }
        for system, codes in {"ktru": ktru_codes, "okpd2": explicit_okpd2 | parent_okpd2}.items():
            if not codes:
                continue
            per_code = float(price) / len(codes) if price is not None and float(price) >= 0 else 0.0
            for code in codes:
                stat = market_stats[system].setdefault(
                    code,
                    {"code": code, "contracts": 0, "purchases": set(), "value": 0.0, "priced": 0},
                )
                stat["contracts"] += 1
                purchase_number = str(contract.get("purchase_number") or "")
                if purchase_number:
                    stat["purchases"].add(purchase_number)
                if price is not None and float(price) >= 0:
                    stat["value"] += per_code
                    stat["priced"] += 1

    market_rows: dict[str, list[dict[str, Any]]] = {"okpd2": [], "ktru": []}
    for system in ("okpd2", "ktru"):
        for code, stat in market_stats[system].items():
            count = int(stat["contracts"])
            market_rows[system].append(
                {
                    "code": code,
                    "contracts": count,
                    "purchases": len(stat["purchases"]),
                    "buyer_value_rub": round(float(stat["value"]), 2),
                    "value_coverage": round(int(stat["priced"]) / count if count else 0.0, 3),
                }
            )
        market_rows[system].sort(
            key=lambda item: (float(item["buyer_value_rub"]), int(item["contracts"]), item["code"]),
            reverse=True,
        )

    recent_contracts: list[dict[str, Any]] = []
    for contract in contracts[:limit]:
        contract_id = int(contract["contract_id"])
        supplier_inns = sorted(set(supplier_by_contract.get(contract_id, [])))
        recent_contracts.append(
            {
                "contract_id": contract_id,
                "reg_num": contract.get("reg_num"),
                "purchase_number": contract.get("purchase_number"),
                "price": round(float(contract["price"]), 2) if contract.get("price") is not None else None,
                "suppliers": supplier_inns,
                "supplier_count": len(supplier_inns),
                "ktru_count": len(codes_by_contract[contract_id]["ktru"]),
                "okpd2_count": len(codes_by_contract[contract_id]["okpd2"]),
                "published_at": contract.get("published_at"),
                "subject": contract.get("subject"),
            }
        )

    return {
        "customer_inn": customer_inn,
        "region_code": region_code,
        "contracts": len(contracts),
        "purchases": len(purchases),
        "suppliers": len(suppliers),
        "gross_contract_value_rub": round(gross_value, 2),
        "value_coverage": round(value_coverage, 3),
        "multi_supplier_contracts": multi_supplier_contracts,
        "top_supplier_inn": top_supplier_inn,
        "top_supplier_share": round(top_supplier_share, 4) if top_supplier_share is not None else None,
        "supplier_hhi": round(supplier_hhi, 2) if supplier_hhi is not None else None,
        "supplier_concentration_basis": distribution_basis if distribution else None,
        "repeat_supplier_contract_share": round(repeat_supplier_contract_share, 3),
        "ktru_codes": len(market_rows["ktru"]),
        "okpd2_codes": len(market_rows["okpd2"]),
        "code_value_allocation": "equal_split_within_contract",
        "suppliers_ranked": supplier_rows[:limit],
        "okpd2_markets": market_rows["okpd2"][:limit],
        "ktru_markets": market_rows["ktru"][:limit],
        "recent_contracts": recent_contracts,
    }

def outcome_stats(conn: sqlite3.Connection, *, region_code: int) -> dict[str, Any]:
    purchases = conn.execute(
        """
        SELECT purchase_number, purchase_type
        FROM purchases
        WHERE region_code=?
        ORDER BY purchase_number
        """,
        (region_code,),
    ).fetchall()
    documents = _documents_by_purchase(conn, region_code=region_code)
    protocols = _protocols_by_purchase(conn, region_code=region_code)

    outcomes: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    reason_codes: Counter[str] = Counter()
    reason_names: dict[str, str] = {}
    applications: list[int] = []
    admitted: list[int] = []
    selected_count = 0

    for purchase in purchases:
        number = purchase["purchase_number"]
        purchase_type = purchase["purchase_type"]
        if competition_exclusion_reason(
            purchase_type, doc_types=documents.get(number, [])
        ) is not None:
            continue
        selected = select_competition_protocol(purchase_type, protocols.get(number, []))
        if selected is None:
            continue
        selected_count += 1
        methods[classify_purchase_type(purchase_type).code] += 1
        outcome = classify_protocol_outcome(selected)
        outcomes[outcome.code] += 1
        applications.append(int(selected["applications_count"] or 0))
        admitted.append(int(selected["admitted_count"] or 0))
        reason_code = str(selected.get("abandoned_reason_code") or "").strip()
        reason_name = str(selected.get("abandoned_reason_name") or "").strip()
        if reason_code:
            reason_codes[reason_code] += 1
            if reason_name:
                reason_names.setdefault(reason_code, reason_name)

    return {
        "region": region_code,
        "selected_protocols": selected_count,
        "methods": dict(methods.most_common()),
        "outcomes": dict(outcomes.most_common()),
        "median_applications": median(applications) if applications else None,
        "median_admitted": median(admitted) if admitted else None,
        "reason_codes": [
            {
                "code": code,
                "count": count,
                "name": reason_names.get(code),
            }
            for code, count in reason_codes.most_common()
        ],
    }


def method_stats(conn: sqlite3.Connection, *, region_code: int) -> list[dict[str, Any]]:
    purchases = conn.execute(
        """
        SELECT id, purchase_number, purchase_type, stage
        FROM purchases
        WHERE region_code=?
        ORDER BY purchase_type, stage, purchase_number
        """,
        (region_code,),
    ).fetchall()
    documents = _documents_by_purchase(conn, region_code=region_code)
    protocols = _protocols_by_purchase(conn, region_code=region_code)

    grouped: dict[tuple[str | None, str], dict[str, Any]] = defaultdict(
        lambda: {
            "purchases": 0,
            "stage_counts": defaultdict(int),
            "lifecycle_counts": defaultdict(int),
            "eligible": 0,
            "cancelled": 0,
            "final_candidates": 0,
            "competition_protocols": 0,
        }
    )
    for row in purchases:
        purchase_type = row["purchase_type"]
        method = classify_purchase_type(purchase_type)
        doc_types = documents.get(row["purchase_number"], [])
        lifecycle = lifecycle_from_docs(row["stage"], doc_types)
        exclusion = competition_exclusion_reason(purchase_type, doc_types=doc_types)
        selected = select_competition_protocol(
            purchase_type, protocols.get(row["purchase_number"], [])
        )
        g = grouped[(purchase_type, method.code)]
        g["purchases"] += 1
        g["stage_counts"][str(row["stage"])] += 1
        g["lifecycle_counts"][lifecycle] += 1
        if exclusion is None:
            g["eligible"] += 1
        if competition_detail_exclusion_reason(purchase_type, docs=doc_types) is None:
            g["final_candidates"] += 1
        if lifecycle == "cancelled":
            g["cancelled"] += 1
        if selected is not None:
            g["competition_protocols"] += 1

    result: list[dict[str, Any]] = []
    for (purchase_type, method_code), g in grouped.items():
        method = classify_purchase_type(purchase_type)
        result.append(
            {
                "purchase_type": purchase_type,
                "method": method_code,
                "method_label": method.label,
                "competition_eligible": method.competition_eligible,
                "purchases": g["purchases"],
                "stage_counts": dict(sorted(g["stage_counts"].items())),
                "lifecycle_counts": dict(sorted(g["lifecycle_counts"].items())),
                "eligible_purchases": g["eligible"],
                "cancelled": g["cancelled"],
                "final_candidates": g["final_candidates"],
                "competition_protocols": g["competition_protocols"],
                "protocol_coverage": round(
                    g["competition_protocols"] / g["eligible"], 3
                ) if g["eligible"] else 0.0,
                "exclusion_reason": method.exclusion_reason,
            }
        )
    result.sort(key=lambda item: (-item["purchases"], str(item["purchase_type"])))
    return result
