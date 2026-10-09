from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OpportunityInput:
    demand_rub: float
    procurement_count: int
    weak_competition_score: float
    median_discount_share: float
    recurrence_score: float
    protocol_coverage: float
    protocol_count: int
    buyer_diversity_score: float = 0.5
    discount_coverage: float = 1.0


@dataclass(frozen=True, slots=True)
class ManufacturingInput:
    demand_rub: float
    procurement_count: int
    manufacturer_count: int
    buyer_diversity_score: float
    demand_confidence: float


def opportunity_score(x: OpportunityInput) -> float:
    """Transparent evidence-adjusted score in range 0..100.

    v0.3 rewards repeated demand across independent buyers. A pattern seen six
    times at one buyer is useful, but weaker evidence of a market-wide supplier
    gap than the same pattern repeated across several unrelated buyers.
    """
    demand = min(1.0, math.log1p(max(0.0, x.demand_rub)) / math.log1p(50_000_000))
    volume = min(1.0, math.log1p(max(0, x.procurement_count)) / math.log1p(20))
    weak_competition = max(0.0, min(1.0, x.weak_competition_score))
    price_stickiness = max(0.0, min(1.0, 1.0 - x.median_discount_share / 0.25))
    discount_coverage = max(0.0, min(1.0, x.discount_coverage))
    evidenced_price_stickiness = price_stickiness * discount_coverage
    recurrence = max(0.0, min(1.0, x.recurrence_score))
    buyer_diversity = max(0.0, min(1.0, x.buyer_diversity_score))

    base = (
        0.22 * demand
        + 0.13 * volume
        + 0.27 * weak_competition
        + 0.13 * evidenced_price_stickiness
        + 0.13 * recurrence
        + 0.12 * buyer_diversity
    )

    coverage = max(0.0, min(1.0, x.protocol_coverage))
    evidence = min(1.0, max(0, x.protocol_count) / 5.0)
    confidence = 0.55 + 0.25 * coverage + 0.20 * evidence
    return round(base * confidence * 100, 2)


def _log_band(value: float, *, floor: float, cap: float) -> float:
    """Scale a positive economic value into 0..1 without rewarding tiny markets.

    Plain log1p scaling made tens-of-thousands-ruble markets look surprisingly
    close to multi-million-ruble markets. A floor keeps micro-markets from
    ranking highly only because one registered manufacturer exists.
    """
    value = max(0.0, value)
    if value <= floor:
        return 0.0
    if cap <= floor:
        return 1.0
    return min(1.0, math.log(value / floor) / math.log(cap / floor))


def manufacturing_gap_score(x: ManufacturingInput) -> float:
    """Score industrial markets with strong demand pressure per manufacturer.

    Manufacturing scarcity is useful only when the market itself is material.
    The score therefore emphasizes ruble demand per registered manufacturer,
    while still rewarding total market size, repeated purchases and independent
    buyers. This keeps a 40k-ruble one-manufacturer niche below a multi-million
    market with only a handful of manufacturers.
    """
    manufacturers = max(1, x.manufacturer_count)
    demand_per_manufacturer = max(0.0, x.demand_rub) / manufacturers
    pressure = _log_band(demand_per_manufacturer, floor=100_000, cap=5_000_000)
    demand = _log_band(max(0.0, x.demand_rub), floor=250_000, cap=50_000_000)
    volume = min(1.0, math.log1p(max(0, x.procurement_count)) / math.log1p(20))
    scarcity = 1.0 - min(1.0, math.log1p(manufacturers) / math.log1p(50))
    buyer_diversity = max(0.0, min(1.0, x.buyer_diversity_score))
    demand_confidence = max(0.0, min(1.0, x.demand_confidence))

    base = (
        0.35 * pressure
        + 0.20 * demand
        + 0.25 * scarcity
        + 0.08 * volume
        + 0.12 * buyer_diversity
    )
    confidence = 0.50 + 0.50 * demand_confidence
    return round(base * confidence * 100, 2)


@dataclass(frozen=True, slots=True)
class ProcurementEntryInput:
    manufacturer_source: str | None
    manufacturer_classification: str | None
    has_manufacturing_okved: bool
    has_production_okved: bool
    revenue_rub: float | None
    employee_count: int | None
    gisp_products: int
    fsa_certificates: int
    supplier_contracts: int
    supplier_contract_value_rub: float
    coverage_confidence: float
    regional_demand_value_rub: float = 0.0
    regional_demand_contracts: int = 0
    regional_demand_buyers: int = 0
    demand_confidence: float = 0.0
    addressable_market_rub: float | None = None
    addressable_contracts: int | None = None
    addressable_buyers: int | None = None


def procurement_entry_market_materiality(x: ProcurementEntryInput) -> dict[str, float | str | None]:
    """Describe how economically meaningful the addressable market is for this company.

    Absolute TAM alone is not enough: a 5m-ruble market may be meaningful for a
    20m-ruble business but immaterial for a 1.5bn-ruble manufacturer. Revenue is
    the strongest denominator, with addressable market per employee as a useful
    secondary scale check. Missing denominators are treated as uncertainty, not
    as zero commercial potential.
    """
    addressable_value = max(
        0.0,
        x.regional_demand_value_rub
        if x.addressable_market_rub is None
        else x.addressable_market_rub,
    )
    revenue_ratio: float | None = None
    revenue_ratio_score: float | None = None
    if x.revenue_rub is not None and x.revenue_rub > 0:
        revenue_ratio = addressable_value / x.revenue_rub
        revenue_ratio_score = _log_band(revenue_ratio, floor=0.005, cap=1.0)

    market_per_employee: float | None = None
    market_per_employee_score: float | None = None
    if x.employee_count is not None and x.employee_count > 0:
        market_per_employee = addressable_value / x.employee_count
        market_per_employee_score = _log_band(
            market_per_employee,
            floor=50_000,
            cap=5_000_000,
        )

    if revenue_ratio_score is not None and market_per_employee_score is not None:
        materiality = 0.80 * revenue_ratio_score + 0.20 * market_per_employee_score
        basis = "revenue_and_employees"
        confidence = 1.0
    elif revenue_ratio_score is not None:
        materiality = revenue_ratio_score
        basis = "revenue"
        confidence = 0.85
    elif market_per_employee_score is not None:
        materiality = market_per_employee_score
        basis = "employees"
        confidence = 0.65
    else:
        # No company-size denominator: keep the company rankable using absolute
        # market evidence, but sharply reduce confidence in the materiality claim.
        materiality = _log_band(addressable_value, floor=500_000, cap=500_000_000)
        basis = "absolute_market_only"
        confidence = 0.35

    return {
        "addressable_market_to_revenue_ratio": (
            round(revenue_ratio, 6) if revenue_ratio is not None else None
        ),
        "addressable_market_per_employee_rub": (
            round(market_per_employee, 2) if market_per_employee is not None else None
        ),
        "market_materiality_basis": basis,
        "market_materiality_confidence": round(confidence, 4),
        "market_materiality": round(materiality * 100, 2),
    }


def procurement_entry_components(x: ProcurementEntryInput) -> dict[str, float]:
    """Explainable regional procurement-entry opportunity components, each 0..100."""
    source_score = {
        "both": 1.0,
        "gisp": 0.9,
        "fsa": 0.75,
    }.get(x.manufacturer_source, 0.4)
    class_score = {
        "confirmed_manufacturer": 1.0,
        "likely_manufacturer": 0.85,
        "registry_manufacturer": 0.7,
        "possible_trader": 0.4,
        "not_manufacturer": 0.1,
    }.get(x.manufacturer_classification, 0.5)
    okved_score = 1.0 if x.has_manufacturing_okved else (0.75 if x.has_production_okved else 0.35)
    manufacturer_strength = 0.55 * source_score + 0.30 * class_score + 0.15 * okved_score

    revenue_score = (
        _log_band(float(x.revenue_rub), floor=10_000_000, cap=1_500_000_000)
        if x.revenue_rub is not None
        else None
    )
    employee_score = (
        _log_band(float(x.employee_count), floor=3, cap=200)
        if x.employee_count is not None
        else None
    )
    if revenue_score is not None and employee_score is not None:
        business_scale = 0.80 * revenue_score + 0.20 * employee_score
    elif revenue_score is not None:
        business_scale = revenue_score
    elif employee_score is not None:
        business_scale = 0.70 * employee_score
    else:
        business_scale = 0.0

    gisp_breadth = min(1.0, math.log1p(max(0, x.gisp_products)) / math.log1p(50))
    fsa_breadth = min(1.0, math.log1p(max(0, x.fsa_certificates)) / math.log1p(10))
    if x.gisp_products and x.fsa_certificates:
        product_breadth = 0.75 * gisp_breadth + 0.25 * fsa_breadth
    elif x.gisp_products:
        product_breadth = gisp_breadth
    else:
        product_breadth = fsa_breadth

    count_gap = 1.0 - min(
        1.0,
        math.log1p(max(0, x.supplier_contracts)) / math.log1p(20),
    )
    if x.revenue_rub is not None and x.revenue_rub > 0:
        ratio = max(0.0, x.supplier_contract_value_rub) / x.revenue_rub
        ratio_gap = 1.0 - min(1.0, math.log1p(ratio * 1000.0) / math.log1p(100.0))
        procurement_gap = 0.75 * ratio_gap + 0.25 * count_gap
    else:
        procurement_gap = count_gap

    coverage = max(0.0, min(1.0, x.coverage_confidence))
    evidenced_gap = procurement_gap * (0.25 + 0.75 * coverage)

    addressable_value = (
        x.regional_demand_value_rub
        if x.addressable_market_rub is None
        else x.addressable_market_rub
    )
    demand_value = _log_band(
        max(0.0, addressable_value),
        floor=500_000,
        cap=500_000_000,
    )
    addressable_contracts = (
        x.regional_demand_contracts
        if x.addressable_contracts is None
        else x.addressable_contracts
    )
    addressable_buyers = (
        x.regional_demand_buyers
        if x.addressable_buyers is None
        else x.addressable_buyers
    )
    demand_contracts = min(
        1.0,
        math.log1p(max(0, addressable_contracts)) / math.log1p(50),
    )
    demand_buyers = min(
        1.0,
        math.log1p(max(0, addressable_buyers)) / math.log1p(20),
    )
    market_breadth = 0.45 * demand_contracts + 0.55 * demand_buyers
    regional_demand_fit = 0.65 * demand_value + 0.35 * market_breadth
    demand_confidence = max(0.0, min(1.0, x.demand_confidence))
    evidenced_demand_fit = regional_demand_fit * (0.25 + 0.75 * demand_confidence)

    materiality = procurement_entry_market_materiality(x)
    materiality_score = float(materiality["market_materiality"] or 0.0) / 100.0
    materiality_confidence = float(materiality["market_materiality_confidence"] or 0.0)
    evidenced_market_materiality = (
        materiality_score
        * materiality_confidence
        * (0.25 + 0.75 * demand_confidence)
    )

    return {
        "manufacturer_strength": round(manufacturer_strength * 100, 2),
        "business_scale": round(business_scale * 100, 2),
        "product_breadth": round(product_breadth * 100, 2),
        "market_size": round(demand_value * 100, 2),
        "market_breadth": round(market_breadth * 100, 2),
        "regional_demand_fit": round(regional_demand_fit * 100, 2),
        "evidenced_regional_demand_fit": round(evidenced_demand_fit * 100, 2),
        "market_materiality": round(materiality_score * 100, 2),
        "evidenced_market_materiality": round(evidenced_market_materiality * 100, 2),
        "procurement_gap": round(procurement_gap * 100, 2),
        "evidenced_procurement_gap": round(evidenced_gap * 100, 2),
    }


def procurement_entry_score(x: ProcurementEntryInput) -> float:
    """Regional entry opportunity emphasizing market materiality, not company size alone."""
    parts = procurement_entry_components(x)
    score = (
        0.15 * parts["manufacturer_strength"]
        + 0.10 * parts["business_scale"]
        + 0.05 * parts["product_breadth"]
        + 0.20 * parts["evidenced_regional_demand_fit"]
        + 0.30 * parts["evidenced_market_materiality"]
        + 0.20 * parts["evidenced_procurement_gap"]
    )
    return round(score, 2)
