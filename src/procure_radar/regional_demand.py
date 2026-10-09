from __future__ import annotations

from collections import defaultdict
import math
import re
import sqlite3
from typing import Any, Iterable


_CONTRACT_DATE_EXPR = (
    "COALESCE(NULLIF(c.published_at,''), NULLIF(c.doc_created_at,''), "
    "NULLIF(c.doc_updated_at,''), NULLIF(c.updated_at,''), NULLIF(c.elact_at,''))"
)


def okpd2_family(code: str | None) -> str | None:
    """Return a conservative six-digit OKPD2 family (XX.XX.XX).

    GISP often stores a six-digit category while contracts may contain a more
    specific nine-digit OKPD2 code or a KTRU code whose prefix is OKPD2.  Six
    digits are specific enough to avoid treating an entire division as one
    market while still joining those common representations.
    """
    text = str(code or "").strip()
    if not text:
        return None
    if "-" in text:
        text = text.split("-", 1)[0]
    digits = re.sub(r"\D", "", text)
    if len(digits) < 6:
        return None
    digits = digits[:6]
    return f"{digits[:2]}.{digits[2:4]}.{digits[4:6]}"


def _chunks(values: list[str], size: int = 800) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _organization_names(conn: sqlite3.Connection, inns: Iterable[str]) -> dict[str, str | None]:
    normalized = sorted({str(value).strip() for value in inns if str(value).strip()})
    result: dict[str, str | None] = {inn: None for inn in normalized}
    for chunk in _chunks(normalized):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT inn, name FROM organizations WHERE inn IN ({placeholders})",
            tuple(chunk),
        ):
            result[str(row["inn"])] = str(row["name"]) if row["name"] else None
    return result


def _concentration(values: Iterable[float]) -> dict[str, float]:
    ordered = sorted((max(0.0, float(value)) for value in values), reverse=True)
    total = sum(ordered)
    if total <= 0:
        return {
            "top_1_share_pct": 0.0,
            "top_3_share_pct": 0.0,
            "top_5_share_pct": 0.0,
            "hhi_10000": 0.0,
        }

    shares = [value / total for value in ordered]
    return {
        "top_1_share_pct": round(sum(shares[:1]) * 100, 2),
        "top_3_share_pct": round(sum(shares[:3]) * 100, 2),
        "top_5_share_pct": round(sum(shares[:5]) * 100, 2),
        "hhi_10000": round(sum(share * share for share in shares) * 10000, 2),
    }


def _log_range_score(value: float, *, low: float, high: float) -> float:
    safe = max(0.0, float(value))
    if safe <= low:
        return 0.0
    if safe >= high:
        return 100.0
    return max(0.0, min(100.0, (math.log10(safe) - math.log10(low)) / (math.log10(high) - math.log10(low)) * 100.0))


def _buyer_contestability(
    *,
    addressable_value: float,
    addressable_contracts: int,
    competitor_values: dict[str, float],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    known = [item for item in timeline if item["suppliers"]]
    known.sort(key=lambda item: (str(item["date"] or ""), str(item["reg_num"] or "")))
    distinct_suppliers = sorted({supplier for item in known for supplier in item["suppliers"]})
    opportunities = max(0, len(known) - 1)
    switches = sum(
        1
        for previous, current in zip(known, known[1:])
        if set(previous["suppliers"]) != set(current["suppliers"])
    )
    switch_rate = switches / opportunities if opportunities else 0.0
    diversity = (
        min(1.0, max(0, len(distinct_suppliers) - 1) / opportunities)
        if opportunities
        else 0.0
    )
    concentration = _concentration(competitor_values.values())
    low_dominance = max(0.0, 1.0 - concentration["top_1_share_pct"] / 100.0)

    raw_score = 100.0 * (0.50 * switch_rate + 0.20 * diversity + 0.30 * low_dominance)
    supplier_coverage = len(known) / max(1, addressable_contracts)
    sample_confidence = min(1.0, len(known) / 3.0)
    confidence = max(0.0, min(1.0, supplier_coverage * sample_confidence))
    evidenced_score = raw_score * confidence

    if raw_score >= 70:
        tier = "high"
    elif raw_score >= 40:
        tier = "medium"
    else:
        tier = "low"

    market_size_score = _log_range_score(addressable_value, low=100_000.0, high=100_000_000.0)
    activity_score = min(100.0, math.log1p(max(0, addressable_contracts)) / math.log1p(10) * 100.0)
    buyer_opportunity_score = 0.55 * market_size_score + 0.15 * activity_score + 0.30 * evidenced_score
    if buyer_opportunity_score >= 70:
        opportunity_tier = "high"
    elif buyer_opportunity_score >= 45:
        opportunity_tier = "medium"
    else:
        opportunity_tier = "low"

    dates = [str(item["date"]) for item in timeline if item.get("date")]
    return {
        "distinct_incumbent_suppliers": len(distinct_suppliers),
        "supplier_known_contracts": len(known),
        "supplier_unknown_contracts": max(0, addressable_contracts - len(known)),
        "supplier_switches": switches,
        "supplier_switch_opportunities": opportunities,
        "supplier_switch_rate": round(switch_rate, 4),
        "supplier_repeat_rate": round(1.0 - switch_rate, 4) if opportunities else None,
        "supplier_top_1_value_share_pct": concentration["top_1_share_pct"],
        "supplier_hhi_10000": concentration["hhi_10000"],
        "contestability_score": round(raw_score, 2),
        "contestability_confidence": round(confidence, 4),
        "evidenced_contestability_score": round(evidenced_score, 2),
        "contestability_tier": tier,
        "buyer_opportunity_score": round(buyer_opportunity_score, 2),
        "buyer_opportunity_tier": opportunity_tier,
        "first_addressable_contract_at": min(dates) if dates else None,
        "last_addressable_contract_at": max(dates) if dates else None,
    }


def _manufacturer_okpd2_families(
    conn: sqlite3.Connection, manufacturer_inns: Iterable[str]
) -> dict[str, set[str]]:
    inns = sorted({str(value).strip() for value in manufacturer_inns if str(value).strip()})
    result: dict[str, set[str]] = {inn: set() for inn in inns}
    if not inns:
        return result

    for chunk in _chunks(inns):
        placeholders = ",".join("?" for _ in chunk)
        query = f"""
            SELECT gp.manufacturer_inn AS inn, gp.okpd2_code AS code
            FROM gisp_products gp
            WHERE gp.manufacturer_inn IN ({placeholders})
              AND NULLIF(gp.okpd2_code, '') IS NOT NULL
              AND (gp.is_active=1 OR gp.is_active IS NULL)
            UNION
            SELECT gp.manufacturer_inn AS inn, gr.okpd2_code AS code
            FROM gisp_registry_rows gr
            JOIN gisp_products gp ON gp.registry_number=gr.registry_number
            WHERE gp.manufacturer_inn IN ({placeholders})
              AND NULLIF(gr.okpd2_code, '') IS NOT NULL
              AND (gp.is_active=1 OR gp.is_active IS NULL)
        """
        params = tuple(chunk) + tuple(chunk)
        for row in conn.execute(query, params):
            inn = str(row["inn"] or "").strip()
            family = okpd2_family(row["code"])
            if inn and family:
                result.setdefault(inn, set()).add(family)
    return result


def _demand_coverage(
    *,
    contracts_total: int,
    contracts_with_family: int,
    contract_value_total: float,
    contract_value_with_family: float,
    history_confidence: float,
    history_status: str | None,
) -> dict[str, Any]:
    count_coverage = contracts_with_family / contracts_total if contracts_total else 0.0
    if contract_value_total > 0:
        value_coverage = contract_value_with_family / contract_value_total
        classifier_coverage = 0.5 * count_coverage + 0.5 * value_coverage
    else:
        value_coverage = None
        classifier_coverage = count_coverage

    classifier_coverage = max(0.0, min(1.0, classifier_coverage))
    history_confidence = max(0.0, min(1.0, float(history_confidence)))
    confidence = history_confidence * classifier_coverage
    if history_status != "complete":
        status = "history_incomplete"
    elif classifier_coverage >= 0.80:
        status = "good"
    elif classifier_coverage >= 0.40:
        status = "partial"
    else:
        status = "poor"

    warnings: list[str] = []
    if history_status != "complete":
        warnings.append("regional_contract_history_not_complete")
    if classifier_coverage < 0.80:
        warnings.append("regional_contract_classifier_coverage_below_80pct")

    return {
        "status": status,
        "history_status": history_status,
        "history_confidence": round(history_confidence, 4),
        "contracts_total": contracts_total,
        "contracts_with_okpd2_family": contracts_with_family,
        "contract_classifier_coverage_pct": round(count_coverage * 100, 2),
        "contract_value_total_rub": round(contract_value_total, 2),
        "contract_value_with_okpd2_family_rub": round(contract_value_with_family, 2),
        "contract_value_classifier_coverage_pct": (
            round(value_coverage * 100, 2) if value_coverage is not None else None
        ),
        "classifier_coverage": round(classifier_coverage, 4),
        "demand_confidence": round(confidence, 4),
        "warnings": warnings,
    }


def regional_demand_for_manufacturers(
    conn: sqlite3.Connection,
    *,
    manufacturer_inns: Iterable[str],
    region_code: int,
    year: int,
    history_confidence: float = 1.0,
    history_status: str | None = "complete",
) -> dict[str, Any]:
    """Estimate signed-contract demand addressable by known GISP manufacturers.

    Contract prices are allocated equally across distinct six-digit OKPD2
    families on the contract.  If a contract has multiple suppliers, the own-win
    value is additionally divided across suppliers.  This prevents one total
    contract price from being counted in full for every code or every supplier.
    """
    inns = sorted({str(value).strip() for value in manufacturer_inns if str(value).strip()})
    manufacturer_families = _manufacturer_okpd2_families(conn, inns)
    family_to_inns: dict[str, set[str]] = defaultdict(set)
    for inn, families in manufacturer_families.items():
        for family in families:
            family_to_inns[family].add(inn)

    contracts: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        f"""
        SELECT c.id AS contract_id, c.customer_inn, c.price
        FROM contracts c
        WHERE c.region_code=?
          AND substr({_CONTRACT_DATE_EXPR}, 1, 4)=?
        """,
        (region_code, str(year)),
    ):
        contract_id = int(row["contract_id"])
        contracts[contract_id] = {
            "customer_inn": str(row["customer_inn"] or "").strip() or None,
            "price": float(row["price"]) if row["price"] is not None else None,
            "families": set(),
            "suppliers": set(),
        }

    if contracts:
        for row in conn.execute(
            f"""
            SELECT cc.contract_id, cc.system, cc.code
            FROM contract_codes cc
            JOIN contracts c ON c.id=cc.contract_id
            WHERE c.region_code=?
              AND substr({_CONTRACT_DATE_EXPR}, 1, 4)=?
              AND cc.system IN ('okpd2', 'ktru')
            """,
            (region_code, str(year)),
        ):
            contract = contracts.get(int(row["contract_id"]))
            if contract is None:
                continue
            family = okpd2_family(row["code"])
            if family:
                contract["families"].add(family)

        for row in conn.execute(
            f"""
            SELECT cs.contract_id, cs.inn
            FROM contract_suppliers cs
            JOIN contracts c ON c.id=cs.contract_id
            WHERE c.region_code=?
              AND substr({_CONTRACT_DATE_EXPR}, 1, 4)=?
            """,
            (region_code, str(year)),
        ):
            contract = contracts.get(int(row["contract_id"]))
            if contract is not None and row["inn"]:
                contract["suppliers"].add(str(row["inn"]).strip())

    contracts_total = len(contracts)
    contracts_with_family = 0
    contract_value_total = 0.0
    contract_value_with_family = 0.0

    stats: dict[str, dict[str, Any]] = {
        inn: {
            "demand_value": 0.0,
            "contract_ids": set(),
            "buyers": set(),
            "matched_families": set(),
            "own_value": 0.0,
            "own_contract_ids": set(),
            "own_buyers": set(),
        }
        for inn in inns
    }

    for contract_id, contract in contracts.items():
        price = contract["price"]
        if price is not None and price >= 0:
            contract_value_total += price
        families: set[str] = contract["families"]
        if not families:
            continue
        contracts_with_family += 1
        if price is not None and price >= 0:
            contract_value_with_family += price
        family_divisor = max(1, len(families))
        suppliers: set[str] = contract["suppliers"]
        supplier_divisor = max(1, len(suppliers))
        customer = contract["customer_inn"]

        for family in families:
            candidate_inns = family_to_inns.get(family)
            if not candidate_inns:
                continue
            allocated_market_value = (
                price / family_divisor if price is not None and price >= 0 else 0.0
            )
            for inn in candidate_inns:
                stat = stats[inn]
                stat["demand_value"] += allocated_market_value
                stat["contract_ids"].add(contract_id)
                stat["matched_families"].add(family)
                if customer:
                    stat["buyers"].add(customer)
                if inn in suppliers:
                    stat["own_value"] += allocated_market_value / supplier_divisor
                    stat["own_contract_ids"].add(contract_id)
                    if customer:
                        stat["own_buyers"].add(customer)

    coverage = _demand_coverage(
        contracts_total=contracts_total,
        contracts_with_family=contracts_with_family,
        contract_value_total=contract_value_total,
        contract_value_with_family=contract_value_with_family,
        history_confidence=history_confidence,
        history_status=history_status,
    )

    by_inn: dict[str, dict[str, Any]] = {}
    for inn in inns:
        stat = stats[inn]
        demand_value = float(stat["demand_value"])
        own_value = float(stat["own_value"])
        market_share = own_value / demand_value if demand_value > 0 else None
        buyers: set[str] = stat["buyers"]
        own_buyers: set[str] = stat["own_buyers"]
        families = manufacturer_families.get(inn, set())
        matched_families: set[str] = stat["matched_families"]
        if not families:
            match_status = "no_manufacturer_okpd2"
        elif matched_families:
            match_status = "matched"
        else:
            match_status = "no_regional_demand_match"

        by_inn[inn] = {
            "inn": inn,
            "year": year,
            "region_code": region_code,
            "match_status": match_status,
            "manufacturer_okpd2_families": sorted(families),
            "manufacturer_okpd2_family_count": len(families),
            "matched_okpd2_families": sorted(matched_families),
            "matched_okpd2_family_count": len(matched_families),
            "regional_demand_value_year_rub": round(demand_value, 2),
            "regional_demand_contracts_year": len(stat["contract_ids"]),
            "regional_demand_buyers_year": len(buyers),
            "manufacturer_matched_contracts_year": len(stat["own_contract_ids"]),
            "manufacturer_matched_value_year_rub": round(own_value, 2),
            "manufacturer_regional_market_share_year": (
                round(market_share, 6) if market_share is not None else None
            ),
            "addressable_market_rub": round(max(0.0, demand_value - own_value), 2),
            "addressable_contracts": len(stat["contract_ids"] - stat["own_contract_ids"]),
            "addressable_buyers": len(buyers - own_buyers),
            "demand_confidence": coverage["demand_confidence"],
        }

    return {"coverage": coverage, "by_inn": by_inn}


def regional_manufacturer_demand(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    region_code: int,
    year: int,
    history_confidence: float = 1.0,
    history_status: str | None = "complete",
) -> dict[str, Any]:
    inn = str(manufacturer_inn).strip()
    if not inn:
        raise ValueError("manufacturer_inn must not be empty")
    result = regional_demand_for_manufacturers(
        conn,
        manufacturer_inns=[inn],
        region_code=region_code,
        year=year,
        history_confidence=history_confidence,
        history_status=history_status,
    )
    return {
        **result["by_inn"][inn],
        "demand_coverage": result["coverage"],
    }


def _regional_demand_landscape(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    region_code: int,
    year: int,
    history_confidence: float = 1.0,
    history_status: str | None = "complete",
    contracts_per_buyer: int = 3,
) -> dict[str, Any]:
    """Build buyer and incumbent-supplier drill-down for one manufacturer.

    Values use the same conservative family allocation as ``regional-demand``:
    a contract price is split across distinct six-digit OKPD2 families, and
    supplier value is then split across suppliers.  That keeps buyer and
    competitor totals consistent with the headline addressable market.
    """
    inn = str(manufacturer_inn).strip()
    if not inn:
        raise ValueError("manufacturer_inn must not be empty")
    contracts_per_buyer = max(0, int(contracts_per_buyer))

    summary = regional_manufacturer_demand(
        conn,
        manufacturer_inn=inn,
        region_code=region_code,
        year=year,
        history_confidence=history_confidence,
        history_status=history_status,
    )
    manufacturer_families = set(summary["manufacturer_okpd2_families"])
    if not manufacturer_families:
        return {
            "summary": summary,
            "buyers": [],
            "competitors": [],
            "buyer_concentration": {
                "attributed_addressable_market_rub": 0.0,
                "unattributed_addressable_market_rub": round(
                    float(summary["addressable_market_rub"]), 2
                ),
                "top_5_buyers_value_rub": 0.0,
                **_concentration([]),
            },
            "competitor_concentration": {
                "competitors_count": 0,
                "attributed_addressable_market_rub": 0.0,
                "unattributed_addressable_market_rub": round(
                    float(summary["addressable_market_rub"]), 2
                ),
                "top_5_competitors_value_rub": 0.0,
                **_concentration([]),
            },
        }

    contracts: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        f"""
        SELECT c.id AS contract_id,
               c.reg_num,
               c.purchase_number,
               c.customer_inn,
               c.price,
               c.subject,
               {_CONTRACT_DATE_EXPR} AS contract_date,
               cc.code
        FROM contracts c
        JOIN contract_codes cc ON cc.contract_id=c.id
        WHERE c.region_code=?
          AND substr({_CONTRACT_DATE_EXPR}, 1, 4)=?
          AND cc.system IN ('okpd2', 'ktru')
        """,
        (region_code, str(year)),
    ):
        contract_id = int(row["contract_id"])
        contract = contracts.setdefault(
            contract_id,
            {
                "contract_id": contract_id,
                "reg_num": row["reg_num"],
                "purchase_number": row["purchase_number"],
                "customer_inn": str(row["customer_inn"] or "").strip() or None,
                "price": float(row["price"]) if row["price"] is not None else None,
                "subject": row["subject"],
                "contract_date": row["contract_date"],
                "families": set(),
                "suppliers": set(),
            },
        )
        family = okpd2_family(row["code"])
        if family:
            contract["families"].add(family)

    relevant_contracts = {
        contract_id: contract
        for contract_id, contract in contracts.items()
        if set(contract["families"]) & manufacturer_families
    }

    contract_ids = sorted(relevant_contracts)
    for chunk in _chunks([str(value) for value in contract_ids]):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT contract_id, inn FROM contract_suppliers WHERE contract_id IN ({placeholders})",
            tuple(int(value) for value in chunk),
        ):
            contract = relevant_contracts.get(int(row["contract_id"]))
            if contract is not None and row["inn"]:
                contract["suppliers"].add(str(row["inn"]).strip())

    buyer_stats: dict[str, dict[str, Any]] = {}
    competitor_stats: dict[str, dict[str, Any]] = {}
    all_party_inns: set[str] = set()
    unknown_buyer_addressable = 0.0
    unknown_supplier_addressable = 0.0

    for contract_id, contract in relevant_contracts.items():
        families = set(contract["families"])
        matched_families = families & manufacturer_families
        if not matched_families:
            continue
        price = contract["price"]
        safe_price = price if price is not None and price >= 0 else 0.0
        allocated_value = safe_price * len(matched_families) / max(1, len(families))
        suppliers = set(contract["suppliers"])
        supplier_divisor = max(1, len(suppliers))
        supplier_share = allocated_value / supplier_divisor if suppliers else 0.0
        own_value = supplier_share if inn in suppliers else 0.0
        addressable_value = max(0.0, allocated_value - own_value)
        customer = contract["customer_inn"]

        if customer:
            all_party_inns.add(customer)
            buyer = buyer_stats.setdefault(
                customer,
                {
                    "buyer_inn": customer,
                    "demand_value": 0.0,
                    "addressable_value": 0.0,
                    "own_value": 0.0,
                    "contract_ids": set(),
                    "addressable_contract_ids": set(),
                    "matched_families": set(),
                    "competitor_values": defaultdict(float),
                    "addressable_contract_timeline": [],
                    "contract_examples": [],
                },
            )
            buyer["demand_value"] += allocated_value
            buyer["addressable_value"] += addressable_value
            buyer["own_value"] += own_value
            buyer["contract_ids"].add(contract_id)
            if addressable_value > 0:
                buyer["addressable_contract_ids"].add(contract_id)
            buyer["matched_families"].update(matched_families)
        else:
            unknown_buyer_addressable += addressable_value
            buyer = None

        supplier_rows: list[dict[str, Any]] = []
        if suppliers:
            for supplier in suppliers:
                all_party_inns.add(supplier)
                if supplier == inn:
                    continue
                competitor = competitor_stats.setdefault(
                    supplier,
                    {
                        "supplier_inn": supplier,
                        "matched_value": 0.0,
                        "contract_ids": set(),
                        "buyers": set(),
                        "matched_families": set(),
                    },
                )
                competitor["matched_value"] += supplier_share
                competitor["contract_ids"].add(contract_id)
                if customer:
                    competitor["buyers"].add(customer)
                competitor["matched_families"].update(matched_families)
                if buyer is not None:
                    buyer["competitor_values"][supplier] += supplier_share
                supplier_rows.append(
                    {
                        "inn": supplier,
                        "allocated_value_rub": round(supplier_share, 2),
                    }
                )
        elif addressable_value > 0:
            unknown_supplier_addressable += addressable_value

        if buyer is not None and addressable_value > 0:
            buyer["addressable_contract_timeline"].append(
                {
                    "contract_id": contract_id,
                    "reg_num": contract["reg_num"],
                    "date": contract["contract_date"],
                    "suppliers": sorted(supplier for supplier in suppliers if supplier != inn),
                }
            )

        if buyer is not None and contracts_per_buyer:
            buyer["contract_examples"].append(
                {
                    "reg_num": contract["reg_num"],
                    "purchase_number": contract["purchase_number"],
                    "published_at": contract["contract_date"],
                    "subject": contract["subject"],
                    "contract_price_rub": round(safe_price, 2),
                    "allocated_demand_value_rub": round(allocated_value, 2),
                    "addressable_value_rub": round(addressable_value, 2),
                    "matched_okpd2_families": sorted(matched_families),
                    "suppliers": supplier_rows,
                }
            )

    names = _organization_names(conn, all_party_inns)
    addressable_market = float(summary["addressable_market_rub"] or 0.0)

    buyers: list[dict[str, Any]] = []
    for buyer_inn, stat in buyer_stats.items():
        addressable_value = float(stat["addressable_value"])
        competitors = sorted(
            (
                {
                    "supplier_inn": supplier_inn,
                    "supplier_name": names.get(supplier_inn),
                    "matched_value_rub": round(value, 2),
                    "share_of_buyer_addressable_market_pct": (
                        round(value / addressable_value * 100, 2)
                        if addressable_value > 0
                        else 0.0
                    ),
                }
                for supplier_inn, value in stat["competitor_values"].items()
            ),
            key=lambda item: (-float(item["matched_value_rub"]), str(item["supplier_inn"])),
        )
        contract_examples = sorted(
            stat["contract_examples"],
            key=lambda item: (
                -float(item["addressable_value_rub"]),
                str(item["reg_num"] or ""),
            ),
        )[:contracts_per_buyer]
        for example in contract_examples:
            for supplier in example["suppliers"]:
                supplier["name"] = names.get(str(supplier["inn"]))

        contestability = _buyer_contestability(
            addressable_value=addressable_value,
            addressable_contracts=len(stat["addressable_contract_ids"]),
            competitor_values=dict(stat["competitor_values"]),
            timeline=list(stat["addressable_contract_timeline"]),
        )

        buyers.append(
            {
                "buyer_inn": buyer_inn,
                "buyer_name": names.get(buyer_inn),
                "demand_value_rub": round(float(stat["demand_value"]), 2),
                "addressable_value_rub": round(addressable_value, 2),
                "manufacturer_own_value_rub": round(float(stat["own_value"]), 2),
                "demand_contracts": len(stat["contract_ids"]),
                "addressable_contracts": len(stat["addressable_contract_ids"]),
                "matched_okpd2_families": sorted(stat["matched_families"]),
                "share_of_addressable_market_pct": (
                    round(addressable_value / addressable_market * 100, 2)
                    if addressable_market > 0
                    else 0.0
                ),
                **contestability,
                "incumbent_suppliers": competitors,
                "top_contracts": contract_examples,
            }
        )
    buyers.sort(
        key=lambda item: (-float(item["addressable_value_rub"]), str(item["buyer_inn"]))
    )

    competitors: list[dict[str, Any]] = []
    for supplier_inn, stat in competitor_stats.items():
        matched_value = float(stat["matched_value"])
        competitors.append(
            {
                "supplier_inn": supplier_inn,
                "supplier_name": names.get(supplier_inn),
                "matched_value_rub": round(matched_value, 2),
                "matched_contracts": len(stat["contract_ids"]),
                "buyers": len(stat["buyers"]),
                "matched_okpd2_families": sorted(stat["matched_families"]),
                "share_of_addressable_market_pct": (
                    round(matched_value / addressable_market * 100, 2)
                    if addressable_market > 0
                    else 0.0
                ),
            }
        )
    competitors.sort(
        key=lambda item: (-float(item["matched_value_rub"]), str(item["supplier_inn"]))
    )

    buyer_values = [float(item["addressable_value_rub"]) for item in buyers]
    competitor_values = [float(item["matched_value_rub"]) for item in competitors]
    buyer_attributed = sum(buyer_values)
    competitor_attributed = sum(competitor_values)

    return {
        "summary": summary,
        "buyers": buyers,
        "competitors": competitors,
        "buyer_concentration": {
            "attributed_addressable_market_rub": round(buyer_attributed, 2),
            "unattributed_addressable_market_rub": round(
                max(0.0, addressable_market - buyer_attributed), 2
            ),
            "top_5_buyers_value_rub": round(sum(buyer_values[:5]), 2),
            **_concentration(buyer_values),
        },
        "competitor_concentration": {
            "competitors_count": len(competitors),
            "attributed_addressable_market_rub": round(competitor_attributed, 2),
            "unattributed_addressable_market_rub": round(
                max(unknown_supplier_addressable, addressable_market - competitor_attributed), 2
            ),
            "top_5_competitors_value_rub": round(sum(competitor_values[:5]), 2),
            **_concentration(competitor_values),
        },
        "unattributed_buyer_addressable_market_rub": round(unknown_buyer_addressable, 2),
    }


def regional_demand_buyers(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    region_code: int,
    year: int,
    history_confidence: float = 1.0,
    history_status: str | None = "complete",
    limit: int = 20,
    offset: int = 0,
    contracts_per_buyer: int = 3,
) -> dict[str, Any]:
    landscape = _regional_demand_landscape(
        conn,
        manufacturer_inn=manufacturer_inn,
        region_code=region_code,
        year=year,
        history_confidence=history_confidence,
        history_status=history_status,
        contracts_per_buyer=contracts_per_buyer,
    )
    rows = landscape["buyers"]
    start = max(0, int(offset))
    stop = start + max(0, int(limit))
    return {
        **landscape["summary"],
        "buyer_concentration": landscape["buyer_concentration"],
        "competitor_concentration": landscape["competitor_concentration"],
        "unattributed_buyer_addressable_market_rub": landscape[
            "unattributed_buyer_addressable_market_rub"
        ],
        "total": len(rows),
        "limit": max(0, int(limit)),
        "offset": start,
        "rows": rows[start:stop],
    }


def regional_demand_competitors(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    region_code: int,
    year: int,
    history_confidence: float = 1.0,
    history_status: str | None = "complete",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    landscape = _regional_demand_landscape(
        conn,
        manufacturer_inn=manufacturer_inn,
        region_code=region_code,
        year=year,
        history_confidence=history_confidence,
        history_status=history_status,
        contracts_per_buyer=0,
    )
    rows = landscape["competitors"]
    start = max(0, int(offset))
    stop = start + max(0, int(limit))
    return {
        **landscape["summary"],
        "buyer_concentration": landscape["buyer_concentration"],
        "competitor_concentration": landscape["competitor_concentration"],
        "total": len(rows),
        "limit": max(0, int(limit)),
        "offset": start,
        "rows": rows[start:stop],
    }
