from __future__ import annotations

from datetime import date, timedelta
import sqlite3
from typing import Any

from .contract_history import contract_history_status
from .organization import list_organizations
from .regional_demand import regional_demand_for_manufacturers
from .scoring import (
    ProcurementEntryInput,
    procurement_entry_components,
    procurement_entry_market_materiality,
    procurement_entry_score,
)


_CONTRACT_DATE_EXPR = (
    "COALESCE(NULLIF(c.published_at,''), NULLIF(c.doc_created_at,''), "
    "NULLIF(c.doc_updated_at,''), NULLIF(c.updated_at,''), NULLIF(c.elact_at,''))"
)


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _target_period(year: int) -> tuple[date, date]:
    if not 1900 <= year <= 2100:
        raise ValueError("year must be between 1900 and 2100")
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    today = date.today()
    if year == today.year:
        end = today
    return start, end


def _merge_intervals(intervals: list[tuple[date, date]]) -> list[tuple[date, date]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[date, date]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + timedelta(days=1):
            if end > prev_end:
                merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    return merged


def _overlap_days(
    intervals: list[tuple[date, date]], *, target_start: date, target_end: date
) -> int:
    days = 0
    for start, end in _merge_intervals(intervals):
        clipped_start = max(start, target_start)
        clipped_end = min(end, target_end)
        if clipped_start <= clipped_end:
            days += (clipped_end - clipped_start).days + 1
    return days


def procurement_contract_coverage(
    conn: sqlite3.Connection, *, region_code: int, year: int
) -> dict[str, Any]:
    """Describe how trustworthy a zero supplier-contract signal is for a region/year.

    Contract history is collected by customer/contract region.  Therefore this
    coverage is regional procurement coverage, not proof of a supplier's activity
    (or inactivity) across all Russian regions.
    """
    target_start, target_end = _target_period(year)
    checkpoints = contract_history_status(conn, region_code=region_code)

    complete_intervals: list[tuple[date, date]] = []
    overlapping_checkpoints = 0
    checkpoint_rows: list[dict[str, Any]] = []
    for row in checkpoints:
        since = _parse_date(row.get("since_date"))
        until = _parse_date(row.get("until_date"))
        updated = _parse_date(row.get("updated_at"))
        completed = bool(row.get("completed"))
        effective_end = until or updated
        overlaps = bool(
            since is not None
            and effective_end is not None
            and since <= target_end
            and effective_end >= target_start
        )
        if overlaps:
            overlapping_checkpoints += 1
        if completed and since is not None and effective_end is not None:
            complete_intervals.append((since, effective_end))
        checkpoint_rows.append(
            {
                "checkpoint_key": row.get("checkpoint_key"),
                "since_date": row.get("since_date"),
                "until_date": row.get("until_date"),
                "completed": int(row.get("completed") or 0),
                "updated_at": row.get("updated_at"),
            }
        )

    target_days = (target_end - target_start).days + 1
    completed_days = _overlap_days(
        complete_intervals, target_start=target_start, target_end=target_end
    )
    completed_fraction = min(1.0, completed_days / target_days) if target_days else 0.0

    observed = conn.execute(
        f"""
        SELECT COUNT(*) AS contracts,
               MIN({_CONTRACT_DATE_EXPR}) AS min_published_at,
               MAX({_CONTRACT_DATE_EXPR}) AS max_published_at,
               COALESCE(SUM(c.price), 0) AS contract_value_rub
        FROM contracts c
        WHERE c.region_code=?
          AND substr({_CONTRACT_DATE_EXPR}, 1, 4)=?
        """,
        (region_code, str(year)),
    ).fetchone()
    suppliers = conn.execute(
        f"""
        SELECT COUNT(DISTINCT cs.inn) AS suppliers,
               COUNT(*) AS supplier_links
        FROM contract_suppliers cs
        JOIN contracts c ON c.id=cs.contract_id
        WHERE c.region_code=?
          AND substr({_CONTRACT_DATE_EXPR}, 1, 4)=?
        """,
        (region_code, str(year)),
    ).fetchone()

    if completed_fraction >= 0.999:
        status = "complete"
        confidence = 1.0
    elif completed_fraction > 0:
        status = "partial"
        confidence = round(0.4 + 0.5 * completed_fraction, 3)
    elif overlapping_checkpoints:
        status = "partial"
        confidence = 0.5
    else:
        status = "unverified"
        confidence = 0.25

    warning = None
    if status != "complete":
        warning = (
            "zero regional supplier activity is provisional until contract-history "
            "coverage for this region/year is complete"
        )

    return {
        "region_code": region_code,
        "year": year,
        "scope": "contracts.customer_region",
        "target_since": target_start.isoformat(),
        "target_until": target_end.isoformat(),
        "status": status,
        "confidence": confidence,
        "completed_coverage_fraction": round(completed_fraction, 4),
        "checkpoints_total": len(checkpoints),
        "checkpoints_overlapping": overlapping_checkpoints,
        "checkpoints": checkpoint_rows,
        "observed_contracts": int(observed["contracts"] or 0),
        "observed_contract_value_rub": float(observed["contract_value_rub"] or 0),
        "observed_suppliers": int(suppliers["suppliers"] or 0),
        "observed_supplier_links": int(suppliers["supplier_links"] or 0),
        "observed_min_published_at": observed["min_published_at"],
        "observed_max_published_at": observed["max_published_at"],
        "warning": warning,
    }


def _regional_supplier_activity(
    conn: sqlite3.Connection, *, region_code: int, year: int
) -> dict[str, dict[str, float | int]]:
    rows = conn.execute(
        f"""
        WITH supplier_counts AS (
            SELECT contract_id, COUNT(*) AS supplier_count
            FROM contract_suppliers
            GROUP BY contract_id
        )
        SELECT cs.inn,
               COUNT(*) AS contracts,
               COALESCE(SUM(c.price / NULLIF(sc.supplier_count, 0)), 0) AS value_rub
        FROM contract_suppliers cs
        JOIN contracts c ON c.id=cs.contract_id
        JOIN supplier_counts sc ON sc.contract_id=c.id
        WHERE c.region_code=?
          AND substr({_CONTRACT_DATE_EXPR}, 1, 4)=?
        GROUP BY cs.inn
        """,
        (region_code, str(year)),
    ).fetchall()
    return {
        str(row["inn"]): {
            "contracts": int(row["contracts"] or 0),
            "value_rub": float(row["value_rub"] or 0),
        }
        for row in rows
    }


def _all_manufacturer_rows(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    year: int,
    manufacturer_source: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = list_organizations(
            conn,
            role="manufacturer",
            region_code=region_code,
            year=year,
            manufacturer_source=manufacturer_source,
            limit=1000,
            offset=offset,
        )
        batch = list(page["rows"])
        rows.extend(batch)
        offset += len(batch)
        if not batch or offset >= int(page["total"]):
            break
    return rows


def _tier(*, score: float, coverage_status: str, supplier_contracts: int, row: dict[str, Any]) -> str:
    if supplier_contracts == 0 and coverage_status != "complete":
        return "needs_history"
    if (
        score >= 75
        and row.get("manufacturer_confidence") == "high"
        and row.get("revenue") is not None
    ):
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def procurement_entry_opportunities(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    year: int,
    manufacturer_source: str | None = None,
    min_score: float = 0.0,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    if manufacturer_source not in {None, "gisp", "fsa", "both"}:
        raise ValueError("manufacturer_source must be gisp, fsa, or both")
    if not 0 <= min_score <= 100:
        raise ValueError("min_score must be between 0 and 100")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    coverage = procurement_contract_coverage(conn, region_code=region_code, year=year)
    regional_activity = _regional_supplier_activity(conn, region_code=region_code, year=year)
    manufacturers = _all_manufacturer_rows(
        conn,
        region_code=region_code,
        year=year,
        manufacturer_source=manufacturer_source,
    )
    regional_demand_result = regional_demand_for_manufacturers(
        conn,
        manufacturer_inns=[str(row["inn"]) for row in manufacturers],
        region_code=region_code,
        year=year,
        history_confidence=float(coverage["confidence"]),
        history_status=str(coverage["status"]),
    )
    demand_coverage = regional_demand_result["coverage"]
    regional_demand = regional_demand_result["by_inn"]

    scored: list[dict[str, Any]] = []
    for row in manufacturers:
        activity = regional_activity.get(str(row["inn"]), {"contracts": 0, "value_rub": 0.0})
        supplier_contracts = int(activity["contracts"])
        supplier_value = float(activity["value_rub"])
        revenue = float(row["revenue"]) if row.get("revenue") is not None else None
        ratio = None
        if revenue is not None and revenue > 0:
            ratio = round(supplier_value / revenue, 6)

        demand = regional_demand.get(str(row["inn"]), {})
        score_input = ProcurementEntryInput(
            manufacturer_source=row.get("manufacturer_source"),
            manufacturer_classification=row.get("manufacturer_classification"),
            has_manufacturing_okved=bool(row.get("has_manufacturing_okved")),
            has_production_okved=bool(row.get("has_production_okved")),
            revenue_rub=revenue,
            employee_count=(
                int(row["employee_count"]) if row.get("employee_count") is not None else None
            ),
            gisp_products=int(row.get("gisp_products") or 0),
            fsa_certificates=int(row.get("fsa_manufacturer_certificates") or 0),
            supplier_contracts=supplier_contracts,
            supplier_contract_value_rub=supplier_value,
            coverage_confidence=float(coverage["confidence"]),
            regional_demand_value_rub=float(demand.get("regional_demand_value_year_rub") or 0.0),
            regional_demand_contracts=int(demand.get("regional_demand_contracts_year") or 0),
            regional_demand_buyers=int(demand.get("regional_demand_buyers_year") or 0),
            demand_confidence=float(demand.get("demand_confidence") or 0.0),
            addressable_market_rub=float(demand.get("addressable_market_rub") or 0.0),
            addressable_contracts=int(demand.get("addressable_contracts") or 0),
            addressable_buyers=int(demand.get("addressable_buyers") or 0),
        )
        components = procurement_entry_components(score_input)
        materiality = procurement_entry_market_materiality(score_input)
        score = procurement_entry_score(score_input)
        if score < min_score:
            continue

        tier = _tier(
            score=score,
            coverage_status=str(coverage["status"]),
            supplier_contracts=supplier_contracts,
            row=row,
        )
        reasons: list[str] = []
        warnings: list[str] = []
        if row.get("manufacturer_source") == "both":
            reasons.append("manufacturer_confirmed_by_gisp_and_fsa")
        elif row.get("manufacturer_source"):
            reasons.append(f"manufacturer_registry:{row['manufacturer_source']}")
        if row.get("has_manufacturing_okved"):
            reasons.append("manufacturing_okved")
        if revenue is not None:
            reasons.append(f"revenue_rub:{int(revenue)}")
        if row.get("employee_count") is not None:
            reasons.append(f"employees:{int(row['employee_count'])}")
        reasons.append(f"regional_supplier_contracts_{year}:{supplier_contracts}")
        reasons.append(f"regional_supplier_value_rub_{year}:{round(supplier_value, 2)}")
        if supplier_contracts == 0:
            reasons.append("no_observed_regional_supplier_contracts")
        reasons.append(
            f"regional_demand_value_rub_{year}:{round(float(demand.get('regional_demand_value_year_rub') or 0.0), 2)}"
        )
        reasons.append(
            f"regional_demand_contracts_{year}:{int(demand.get('regional_demand_contracts_year') or 0)}"
        )
        reasons.append(
            f"regional_demand_buyers_{year}:{int(demand.get('regional_demand_buyers_year') or 0)}"
        )
        if float(demand.get("regional_demand_value_year_rub") or 0.0) > 0:
            reasons.append("observed_regional_demand_for_manufacturer_okpd2")
        if materiality["addressable_market_to_revenue_ratio"] is not None:
            reasons.append(
                "addressable_market_to_revenue_ratio:"
                f"{materiality['addressable_market_to_revenue_ratio']}"
            )
        if coverage["status"] != "complete":
            warnings.append("regional_contract_history_not_complete")
        if demand.get("match_status") == "no_manufacturer_okpd2":
            warnings.append("manufacturer_okpd2_missing_for_regional_demand")
        elif demand.get("match_status") == "no_regional_demand_match":
            warnings.append("no_observed_regional_demand_for_manufacturer_okpd2")
        if demand_coverage["status"] not in {"good"}:
            warnings.extend(
                item for item in demand_coverage.get("warnings", []) if item not in warnings
            )
        if revenue is None:
            warnings.append(f"financials_missing_for_{year}")
            warnings.append("market_materiality_revenue_denominator_missing")
        warnings.append("supplier_activity_scope_is_customer_region_not_national")

        scored.append(
            {
                "inn": row["inn"],
                "name": row.get("name"),
                "region_code": row.get("region_code"),
                "year": year,
                "procurement_entry_score": score,
                "procurement_entry_tier": tier,
                "coverage_status": coverage["status"],
                "coverage_confidence": coverage["confidence"],
                "manufacturer_source": row.get("manufacturer_source"),
                "manufacturer_confidence": row.get("manufacturer_confidence"),
                "manufacturer_classification": row.get("manufacturer_classification"),
                "main_okved_profile": row.get("main_okved_profile"),
                "has_manufacturing_okved": bool(row.get("has_manufacturing_okved")),
                "revenue": row.get("revenue"),
                "profit": row.get("profit"),
                "employee_count": row.get("employee_count"),
                "gisp_products": int(row.get("gisp_products") or 0),
                "fsa_manufacturer_certificates": int(
                    row.get("fsa_manufacturer_certificates") or 0
                ),
                "regional_supplier_contracts_year": supplier_contracts,
                "regional_supplier_contract_value_year_rub": round(supplier_value, 2),
                "regional_supplier_value_to_revenue_ratio_year": ratio,
                "manufacturer_okpd2_family_count": int(
                    demand.get("manufacturer_okpd2_family_count") or 0
                ),
                "matched_okpd2_family_count": int(demand.get("matched_okpd2_family_count") or 0),
                "matched_okpd2_families": list(demand.get("matched_okpd2_families") or []),
                "regional_demand_value_year_rub": float(
                    demand.get("regional_demand_value_year_rub") or 0.0
                ),
                "regional_demand_contracts_year": int(
                    demand.get("regional_demand_contracts_year") or 0
                ),
                "regional_demand_buyers_year": int(demand.get("regional_demand_buyers_year") or 0),
                "manufacturer_matched_contracts_year": int(
                    demand.get("manufacturer_matched_contracts_year") or 0
                ),
                "manufacturer_matched_value_year_rub": float(
                    demand.get("manufacturer_matched_value_year_rub") or 0.0
                ),
                "manufacturer_regional_market_share_year": demand.get(
                    "manufacturer_regional_market_share_year"
                ),
                "addressable_market_rub": float(demand.get("addressable_market_rub") or 0.0),
                "addressable_contracts": int(demand.get("addressable_contracts") or 0),
                "addressable_buyers": int(demand.get("addressable_buyers") or 0),
                "addressable_market_to_revenue_ratio": materiality[
                    "addressable_market_to_revenue_ratio"
                ],
                "addressable_market_per_employee_rub": materiality[
                    "addressable_market_per_employee_rub"
                ],
                "market_materiality_basis": materiality["market_materiality_basis"],
                "market_materiality_confidence": materiality[
                    "market_materiality_confidence"
                ],
                "demand_match_status": demand.get("match_status"),
                "demand_confidence": demand.get("demand_confidence"),
                "components": components,
                "reasons": reasons,
                "warnings": warnings,
            }
        )

    tier_order = {"high": 0, "medium": 1, "needs_history": 2, "low": 3}
    scored.sort(
        key=lambda item: (
            tier_order.get(str(item["procurement_entry_tier"]), 9),
            -float(item["procurement_entry_score"]),
            str(item["inn"]),
        )
    )
    total = len(scored)
    return {
        "filters": {
            "region_code": region_code,
            "year": year,
            "manufacturer_source": manufacturer_source,
            "min_score": min_score,
        },
        "scope": "regional procurement among customers in contracts.region_code",
        "score_model": "procurement_entry_v0.3_market_materiality",
        "coverage": coverage,
        "demand_coverage": demand_coverage,
        "ranking_status": (
            "final"
            if coverage["status"] == "complete" and demand_coverage["status"] == "good"
            else "provisional"
        ),
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": scored[offset : offset + limit],
    }
