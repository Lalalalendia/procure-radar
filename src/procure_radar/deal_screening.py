from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .classification import classify_purchase_type, lifecycle_from_docs
from .segmentation import classify_market_segment


@dataclass(frozen=True, slots=True)
class DealScore:
    score: float
    tier: str
    decision: str
    eligible: bool
    hard_reject_reasons: tuple[str, ...]
    review_flags: tuple[str, ...]
    components: dict[str, float]
    segment: str
    segment_group: str
    execution_mode: str
    days_remaining: float | None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _interp(value: float, points: Iterable[tuple[float, float]]) -> float:
    pts = sorted((float(x), float(y)) for x, y in points)
    if not pts:
        return 0.0
    if value <= pts[0][0]:
        return pts[0][1]
    if value >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y1
            t = (value - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return pts[-1][1]


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ticket_fit(price: float | None) -> float:
    if price is None or price <= 0:
        return 20.0
    # First-money sweet spot for a small operator: meaningful economics without
    # immediately requiring industrial-scale financing or guarantees.
    return _clamp(_interp(price, [
        (10_000, 5),
        (50_000, 20),
        (100_000, 45),
        (300_000, 80),
        (500_000, 100),
        (3_000_000, 100),
        (5_000_000, 85),
        (10_000_000, 60),
        (20_000_000, 35),
        (50_000_000, 15),
        (200_000_000, 5),
    ]))


def _runway_score(days: float | None) -> float:
    if days is None:
        return 35.0
    if days <= 0:
        return 0.0
    return _clamp(_interp(days, [
        (0.5, 5),
        (1.0, 15),
        (2.0, 45),
        (3.0, 85),
        (5.0, 100),
        (10.0, 95),
        (14.0, 88),
        (30.0, 70),
        (60.0, 55),
        (120.0, 40),
    ]))


def _method_score(code: str) -> float:
    return {
        "electronic_quote": 100.0,
        "electronic_auction": 90.0,
        "electronic_request_proposals": 65.0,
        "electronic_competition": 62.0,
        "electronic_competition_two_stage": 42.0,
        "electronic_competition_limited": 32.0,
    }.get(code, 20.0)


def _segment_tractability(segment: str) -> float:
    return {
        "standard_goods": 100.0,
        "it_goods": 88.0,
        "facility_services": 55.0,
        "repair_maintenance": 48.0,
        "professional_services": 42.0,
        "it_services": 42.0,
        "other_services": 38.0,
        "medical_equipment": 38.0,
        "medical_diagnostics": 25.0,
        "construction_works": 24.0,
        "pharma": 12.0,
        "real_estate": 8.0,
        "other": 32.0,
    }.get(segment, 32.0)


def _execution_mode(segment: str, group: str) -> str:
    if segment in {"standard_goods", "it_goods"}:
        return "micro_distribution"
    if segment in {"medical_equipment", "medical_diagnostics", "pharma"}:
        return "specialist_distribution_only"
    if group == "works":
        return "subcontract_or_partner"
    if group == "services":
        return "service_operator_or_partner"
    return "manual_review"


def _complexity_score(item_count: int) -> float:
    if item_count <= 0:
        return 45.0
    if item_count <= 5:
        return 100.0
    if item_count <= 10:
        return 92.0
    if item_count <= 30:
        return 78.0
    if item_count <= 75:
        return 58.0
    if item_count <= 150:
        return 38.0
    return 22.0


def _clarity_score(
    *,
    item_count: int,
    coded_items: int,
    quantity_items: int,
    price_items: int,
    amount_items: int,
) -> float:
    if item_count <= 0:
        return 20.0
    denom = float(item_count)
    code_cov = coded_items / denom
    quantity_cov = quantity_items / denom
    price_cov = price_items / denom
    amount_cov = amount_items / denom
    return _clamp(
        25.0
        + 30.0 * code_cov
        + 15.0 * quantity_cov
        + 15.0 * price_cov
        + 15.0 * amount_cov
    )


def _dominant_segment(items: list[sqlite3.Row], codes: list[sqlite3.Row], object_info: str | None) -> tuple[str, str]:
    weighted: dict[tuple[str, str], float] = {}
    for row in items:
        code = str(row["okpd2_code"] or row["ktru_code"] or "").strip()
        if not code:
            continue
        label = row["okpd2_name"] or row["ktru_name"] or row["name"] or object_info
        system = "okpd2" if row["okpd2_code"] else "ktru"
        segment = classify_market_segment(code_system=system, code=code, label=label)
        weight = float(row["amount"] or row["unit_price"] or 1.0)
        weighted[(segment.code, segment.group)] = weighted.get((segment.code, segment.group), 0.0) + max(1.0, weight)
    if not weighted:
        for row in codes:
            segment = classify_market_segment(
                code_system=str(row["system"] or "okpd2"),
                code=str(row["code"] or ""),
                label=object_info,
            )
            weighted[(segment.code, segment.group)] = weighted.get((segment.code, segment.group), 0.0) + 1.0
    if not weighted:
        return "other", "other"
    return max(weighted, key=weighted.get)


def score_purchase(
    *,
    max_price: float | None,
    collecting_finished_at: str | None,
    purchase_type: str | None,
    stage: int | None,
    doc_types: Iterable[str],
    locality_confidence: float,
    item_count: int,
    coded_items: int,
    quantity_items: int,
    price_items: int,
    amount_items: int,
    segment: str,
    segment_group: str,
    as_of: datetime | None = None,
) -> DealScore:
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    deadline = _parse_dt(collecting_finished_at)
    days_remaining = None if deadline is None else (deadline - now).total_seconds() / 86400.0
    method = classify_purchase_type(purchase_type)
    lifecycle = lifecycle_from_docs(stage, doc_types)

    hard: list[str] = []
    flags: list[str] = []
    if lifecycle in {"completed", "cancelled"}:
        hard.append(lifecycle)
    if days_remaining is not None and days_remaining <= 0:
        hard.append("deadline_passed")
    if not method.competition_eligible:
        hard.append(method.exclusion_reason or "non_competitive_method")

    if max_price is None or max_price <= 0:
        flags.append("missing_price")
    elif max_price >= 10_000_000:
        flags.append("high_capital_requirement")
    if days_remaining is None:
        flags.append("missing_deadline")
    elif days_remaining < 2.0:
        flags.append("deadline_risk")
    if item_count == 0:
        flags.append("no_line_items")
    elif item_count > 75:
        flags.append("many_line_items")
    if item_count and coded_items / item_count < 0.5:
        flags.append("weak_code_coverage")
    if segment in {"pharma", "medical_diagnostics", "medical_equipment"}:
        flags.append("regulated_market")
    if segment_group == "works":
        flags.append("execution_heavy")
    if segment == "other":
        flags.append("unclear_segment")

    components = {
        "ticket_fit": round(_ticket_fit(max_price), 2),
        "runway": round(_runway_score(days_remaining), 2),
        "method_access": round(_method_score(method.code), 2),
        "tractability": round(_segment_tractability(segment), 2),
        "data_clarity": round(_clarity_score(
            item_count=item_count,
            coded_items=coded_items,
            quantity_items=quantity_items,
            price_items=price_items,
            amount_items=amount_items,
        ), 2),
        "line_complexity": round(_complexity_score(item_count), 2),
        "locality_evidence": round(_clamp(locality_confidence * 100.0), 2),
    }
    score = (
        0.22 * components["ticket_fit"]
        + 0.18 * components["tractability"]
        + 0.17 * components["data_clarity"]
        + 0.15 * components["runway"]
        + 0.12 * components["method_access"]
        + 0.10 * components["line_complexity"]
        + 0.06 * components["locality_evidence"]
    )
    # This scorer is intentionally optimized for *first money*, not theoretical
    # TAM. Regulated categories and execution-heavy works may be attractive
    # markets, but without an already-qualified partner they should never outrank
    # a simple commodity supply on the first-pass queue.
    risk_caps = {
        "pharma": 64.0,
        "medical_diagnostics": 68.0,
        "medical_equipment": 72.0,
        "construction_works": 70.0,
    }
    if segment in risk_caps:
        score = min(score, risk_caps[segment])
    # First-revenue queue: avoid letting a superficially simple but very large
    # order or a nearly-expired notice rank as an A just because every other
    # field is clean. These are still reviewable opportunities, not first picks.
    if max_price is not None and max_price >= 20_000_000:
        score = min(score, 68.0)
    elif max_price is not None and max_price >= 10_000_000:
        score = min(score, 74.0)
    if days_remaining is not None and days_remaining < 1.0:
        score = min(score, 55.0)
    elif days_remaining is not None and days_remaining < 2.0:
        score = min(score, 65.0)
    if item_count <= 0:
        score = min(score, 60.0)
    elif item_count > 75:
        score = min(score, 65.0)
    if hard:
        score = min(score, 24.0)
    score = round(_clamp(score), 2)

    if score >= 80:
        tier = "A"
    elif score >= 68:
        tier = "B"
    elif score >= 55:
        tier = "C"
    else:
        tier = "D"
    if hard:
        decision = "skip"
    elif tier == "A":
        decision = "pursue"
    elif tier in {"B", "C"}:
        decision = "review"
    else:
        decision = "skip"

    return DealScore(
        score=score,
        tier=tier,
        decision=decision,
        eligible=not hard,
        hard_reject_reasons=tuple(sorted(set(hard))),
        review_flags=tuple(sorted(set(flags))),
        components=components,
        segment=segment,
        segment_group=segment_group,
        execution_mode=_execution_mode(segment, segment_group),
        days_remaining=None if days_remaining is None else round(days_remaining, 2),
    )


def locality_deal_shortlist(
    conn: sqlite3.Connection,
    *,
    locality_key: str,
    min_confidence: float = 0.9,
    min_score: float = 55.0,
    limit: int = 500,
    as_of: datetime | None = None,
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
            p.id AS purchase_id,
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
    result: list[dict[str, Any]] = []
    for row in rows:
        pid = int(row["purchase_id"])
        items = list(conn.execute(
            """
            SELECT name, ktru_code, ktru_name, okpd2_code, okpd2_name,
                   quantity, unit_price, amount
            FROM purchase_items
            WHERE purchase_id=?
            ORDER BY id
            """,
            (pid,),
        ))
        codes = list(conn.execute(
            "SELECT system, code FROM purchase_codes WHERE purchase_id=? ORDER BY system, code",
            (pid,),
        ))
        doc_types = [
            str(r["doc_type"])
            for r in conn.execute(
                "SELECT doc_type FROM purchase_documents WHERE purchase_id=? AND doc_type IS NOT NULL",
                (pid,),
            )
        ]
        item_count = len(items)
        coded_items = sum(1 for x in items if x["okpd2_code"] or x["ktru_code"])
        quantity_items = sum(1 for x in items if x["quantity"] is not None)
        price_items = sum(1 for x in items if x["unit_price"] is not None)
        amount_items = sum(1 for x in items if x["amount"] is not None)
        segment, group = _dominant_segment(items, codes, row["object_info"])
        deal = score_purchase(
            max_price=float(row["max_price"]) if row["max_price"] is not None else None,
            collecting_finished_at=row["collecting_finished_at"],
            purchase_type=row["purchase_type"],
            stage=int(row["stage"]) if row["stage"] is not None else None,
            doc_types=doc_types,
            locality_confidence=float(row["locality_confidence"] or 0.0),
            item_count=item_count,
            coded_items=coded_items,
            quantity_items=quantity_items,
            price_items=price_items,
            amount_items=amount_items,
            segment=segment,
            segment_group=group,
            as_of=as_of,
        )
        if deal.score < float(min_score):
            continue
        out = {k: row[k] for k in row.keys() if k != "purchase_id"}
        out.update({
            "deal_score": deal.score,
            "deal_tier": deal.tier,
            "decision": deal.decision,
            "eligible": deal.eligible,
            "hard_reject_reasons": list(deal.hard_reject_reasons),
            "review_flags": list(deal.review_flags),
            "score_components": deal.components,
            "segment": deal.segment,
            "segment_group": deal.segment_group,
            "execution_mode": deal.execution_mode,
            "days_remaining": deal.days_remaining,
            "item_count": item_count,
            "coded_item_count": coded_items,
        })
        result.append(out)

    result.sort(key=lambda r: (bool(r["eligible"]), float(r["deal_score"]), float(r.get("max_price") or 0.0)), reverse=True)
    return result


def export_deal_shortlist(
    rows: list[dict[str, Any]],
    *,
    json_path: str | Path | None = None,
    csv_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
) -> None:
    if json_path is not None:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if csv_path is not None:
        path = Path(csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "deal_score", "deal_tier", "decision", "eligible", "purchase_number",
            "published_at", "collecting_finished_at", "days_remaining", "max_price",
            "currency_code", "segment", "segment_group", "execution_mode", "item_count",
            "customer_inn", "customer_name", "customer_address", "locality_source", "object_info",
            "review_flags", "hard_reject_reasons", "score_components",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                flat = dict(row)
                for key in ("review_flags", "hard_reject_reasons", "score_components"):
                    flat[key] = json.dumps(flat.get(key), ensure_ascii=False, separators=(",", ":"))
                writer.writerow(flat)

    if markdown_path is not None:
        path = Path(markdown_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Procurement deal shortlist",
            "",
            f"Candidates: {len(rows)}",
            "",
            "| Score | Tier | Decision | Deadline | Price | Segment | Customer | Purchase |",
            "|---:|:---:|---|---|---:|---|---|---|",
        ]
        for row in rows[:30]:
            price = row.get("max_price")
            price_text = "—" if price is None else f"{float(price):,.0f} ₽".replace(",", " ")
            object_info = str(row.get("object_info") or "").replace("|", "/").replace("\n", " ")
            if len(object_info) > 90:
                object_info = object_info[:87] + "…"
            customer = str(row.get("customer_name") or row.get("customer_inn") or "—").replace("|", "/")
            lines.append(
                f"| {row['deal_score']:.2f} | {row['deal_tier']} | {row['decision']} | "
                f"{row.get('collecting_finished_at') or '—'} | {price_text} | {row.get('segment') or '—'} | "
                f"{customer} | {object_info} |"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
