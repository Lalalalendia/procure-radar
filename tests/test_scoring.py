from procure_radar.scoring import (
    ManufacturingInput,
    OpportunityInput,
    manufacturing_gap_score,
    opportunity_score,
)


def test_weak_competition_scores_higher():
    weak = OpportunityInput(50_000_000, 20, 0.8, 0.02, 0.8, 1.0, 10)
    competitive = OpportunityInput(50_000_000, 20, 0.1, 0.22, 0.8, 1.0, 10)
    assert opportunity_score(weak) > opportunity_score(competitive)


def test_more_evidence_scores_higher_for_same_market_signal():
    thin = OpportunityInput(5_000_000, 1, 1.0, 0.0, 0.0, 1.0, 1)
    repeated = OpportunityInput(5_000_000, 5, 1.0, 0.0, 0.4, 1.0, 5)
    assert opportunity_score(repeated) > opportunity_score(thin)


def test_more_independent_buyers_scores_higher():
    concentrated = OpportunityInput(5_000_000, 5, 0.9, 0.01, 0.6, 1.0, 5, 0.1)
    diverse = OpportunityInput(5_000_000, 5, 0.9, 0.01, 0.6, 1.0, 5, 0.9)
    assert opportunity_score(diverse) > opportunity_score(concentrated)


def test_fewer_manufacturers_score_as_larger_manufacturing_gap():
    scarce = ManufacturingInput(10_000_000, 10, 1, 0.8, 1.0)
    crowded = ManufacturingInput(10_000_000, 10, 30, 0.8, 1.0)
    assert manufacturing_gap_score(scarce) > manufacturing_gap_score(crowded)


def test_manufacturing_score_penalizes_uncertain_demand_amounts():
    exact = ManufacturingInput(10_000_000, 10, 2, 0.8, 1.0)
    normalized = ManufacturingInput(10_000_000, 10, 2, 0.8, 0.65)
    assert manufacturing_gap_score(exact) > manufacturing_gap_score(normalized)


def test_manufacturing_score_requires_material_market_not_just_one_manufacturer():
    tiny_single = ManufacturingInput(40_000, 3, 1, 0.8, 1.0)
    material_market = ManufacturingInput(5_000_000, 2, 8, 0.8, 1.0)
    assert manufacturing_gap_score(material_market) > manufacturing_gap_score(tiny_single)


def test_low_discount_signal_is_weighted_by_discount_coverage() -> None:
    full = OpportunityInput(25_000_000, 10, 0.8, 0.0, 0.8, 1.0, 10, 0.8, 1.0)
    partial = OpportunityInput(25_000_000, 10, 0.8, 0.0, 0.8, 1.0, 10, 0.8, 0.25)
    none = OpportunityInput(25_000_000, 10, 0.8, 0.0, 0.8, 1.0, 10, 0.8, 0.0)

    assert opportunity_score(full) > opportunity_score(partial) > opportunity_score(none)


def test_procurement_entry_score_rewards_verified_scale_and_procurement_gap() -> None:
    from procure_radar.scoring import ProcurementEntryInput, procurement_entry_score

    absent = ProcurementEntryInput(
        manufacturer_source="both",
        manufacturer_classification="confirmed_manufacturer",
        has_manufacturing_okved=True,
        has_production_okved=True,
        revenue_rub=1_000_000_000,
        employee_count=120,
        gisp_products=20,
        fsa_certificates=3,
        supplier_contracts=0,
        supplier_contract_value_rub=0,
        coverage_confidence=1.0,
    )
    active = ProcurementEntryInput(
        manufacturer_source="both",
        manufacturer_classification="confirmed_manufacturer",
        has_manufacturing_okved=True,
        has_production_okved=True,
        revenue_rub=1_000_000_000,
        employee_count=120,
        gisp_products=20,
        fsa_certificates=3,
        supplier_contracts=8,
        supplier_contract_value_rub=300_000_000,
        coverage_confidence=1.0,
    )
    assert procurement_entry_score(absent) > procurement_entry_score(active)


def test_procurement_entry_score_penalizes_unverified_absence() -> None:
    from procure_radar.scoring import ProcurementEntryInput, procurement_entry_score

    complete = ProcurementEntryInput(
        manufacturer_source="both",
        manufacturer_classification="confirmed_manufacturer",
        has_manufacturing_okved=True,
        has_production_okved=True,
        revenue_rub=500_000_000,
        employee_count=80,
        gisp_products=10,
        fsa_certificates=2,
        supplier_contracts=0,
        supplier_contract_value_rub=0,
        coverage_confidence=1.0,
    )
    unverified = ProcurementEntryInput(
        manufacturer_source="both",
        manufacturer_classification="confirmed_manufacturer",
        has_manufacturing_okved=True,
        has_production_okved=True,
        revenue_rub=500_000_000,
        employee_count=80,
        gisp_products=10,
        fsa_certificates=2,
        supplier_contracts=0,
        supplier_contract_value_rub=0,
        coverage_confidence=0.25,
    )
    assert procurement_entry_score(complete) > procurement_entry_score(unverified)


def test_procurement_entry_score_rewards_material_regional_demand() -> None:
    from procure_radar.scoring import ProcurementEntryInput, procurement_entry_score

    base = dict(
        manufacturer_source="both",
        manufacturer_classification="confirmed_manufacturer",
        has_manufacturing_okved=True,
        has_production_okved=True,
        revenue_rub=800_000_000,
        employee_count=100,
        gisp_products=10,
        fsa_certificates=2,
        supplier_contracts=0,
        supplier_contract_value_rub=0,
        coverage_confidence=1.0,
    )
    no_market = ProcurementEntryInput(**base)
    material_market = ProcurementEntryInput(
        **base,
        regional_demand_value_rub=500_000_000,
        regional_demand_contracts=30,
        regional_demand_buyers=15,
        demand_confidence=1.0,
    )

    assert procurement_entry_score(material_market) > procurement_entry_score(no_market)
    assert procurement_entry_score(no_market) < 75


def test_procurement_entry_score_rewards_market_materiality_relative_to_company_scale() -> None:
    from procure_radar.scoring import (
        ProcurementEntryInput,
        procurement_entry_market_materiality,
        procurement_entry_score,
    )

    common = dict(
        manufacturer_source="both",
        manufacturer_classification="confirmed_manufacturer",
        has_manufacturing_okved=True,
        has_production_okved=True,
        gisp_products=4,
        fsa_certificates=1,
        supplier_contracts=0,
        supplier_contract_value_rub=0,
        coverage_confidence=1.0,
        demand_confidence=1.0,
    )
    huge_company_tiny_market = ProcurementEntryInput(
        **common,
        revenue_rub=1_000_000_000,
        employee_count=120,
        regional_demand_value_rub=5_000_000,
        regional_demand_contracts=14,
        regional_demand_buyers=13,
        addressable_market_rub=5_000_000,
        addressable_contracts=14,
        addressable_buyers=13,
    )
    smaller_company_material_market = ProcurementEntryInput(
        **common,
        revenue_rub=100_000_000,
        employee_count=20,
        regional_demand_value_rub=70_000_000,
        regional_demand_contracts=9,
        regional_demand_buyers=6,
        addressable_market_rub=70_000_000,
        addressable_contracts=9,
        addressable_buyers=6,
    )

    huge_materiality = procurement_entry_market_materiality(huge_company_tiny_market)
    smaller_materiality = procurement_entry_market_materiality(smaller_company_material_market)
    assert huge_materiality["addressable_market_to_revenue_ratio"] == 0.005
    assert smaller_materiality["addressable_market_to_revenue_ratio"] == 0.7
    assert float(smaller_materiality["market_materiality"]) > float(
        huge_materiality["market_materiality"]
    )
    assert procurement_entry_score(smaller_company_material_market) > procurement_entry_score(
        huge_company_tiny_market
    )
