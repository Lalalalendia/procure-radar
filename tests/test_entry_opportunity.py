from __future__ import annotations

from pathlib import Path

from procure_radar.db import connect
from procure_radar.entry_opportunity import (
    procurement_contract_coverage,
    procurement_entry_opportunities,
)


def _manufacturer(conn, *, inn: str = "1234567890", revenue: float = 1_000_000_000) -> None:
    conn.execute(
        """
        INSERT INTO organizations(inn, name, region_code, okved_main)
        VALUES (?, 'ООО Завод Возможность', 2, '28.13')
        """,
        (inn,),
    )
    conn.execute(
        """
        INSERT INTO organization_financials(inn, year, revenue, expenses, profit, source)
        VALUES (?, 2025, ?, ?, ?, 'fns_revexp')
        """,
        (inn, revenue, revenue * 0.9, revenue * 0.1),
    )
    conn.execute(
        """
        INSERT INTO organization_msp(inn, entity_type, category_name, employee_count, region_code)
        VALUES (?, 'legal_entity', 'medium', 120, 2)
        """,
        (inn,),
    )
    for idx in range(1, 11):
        conn.execute(
            """
            INSERT INTO gisp_products(
                registry_number, manufacturer_name, manufacturer_inn, product_name,
                okpd2_code, source_scope, is_active, raw_json
            ) VALUES (?, 'ООО Завод Возможность', ?, ?, '28.13.00.110', 'active', 1, '{}')
            """,
            (f"G-{idx}", inn, f"Изделие {idx}"),
        )
    for idx in range(1, 4):
        conn.execute(
            """
            INSERT INTO fsa_certificates(
                external_id, number, manufacturer_name, manufacturer_inn,
                product_full_name, raw_json
            ) VALUES (?, ?, 'ООО Завод Возможность', ?, 'Сертифицированное изделие', '{}')
            """,
            (1000 + idx, f"CERT-{idx}", inn),
        )
    conn.commit()


def _complete_contract_history(conn, *, region: int = 2, year: int = 2025) -> None:
    conn.execute(
        """
        INSERT INTO history_backfills(
            checkpoint_key, region_code, since_date, until_date, query_json,
            page_limit, next_skip, completed, updated_at
        ) VALUES (?, ?, ?, ?, ?, 100, 0, 1, ?)
        """,
        (
            f"contract-history-{region}-{year}",
            region,
            f"{year}-01-01",
            f"{year}-12-31",
            '{"__history_mode":"contracts_published_time_shards_v1"}',
            f"{year}-12-31 23:59:59",
        ),
    )
    conn.commit()


def _contract(
    conn, *, inn: str, region: int, price: float, number: str,
    okpd2: str = "28.13.00.110", customer_inn: str = "0200000000"
) -> None:
    cur = conn.execute(
        """
        INSERT INTO raw_documents(source, endpoint, external_id, payload_json)
        VALUES ('test', '/fz44/contracts', ?, '{}')
        """,
        (number,),
    )
    raw_id = int(cur.lastrowid)
    cur = conn.execute(
        """
        INSERT INTO contracts(
            raw_document_id, reg_num, customer_inn, price, region_code, published_at
        ) VALUES (?, ?, ?, ?, ?, '2025-06-15')
        """,
        (raw_id, number, customer_inn, price, region),
    )
    contract_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, ?)",
        (contract_id, inn),
    )
    conn.execute(
        "INSERT INTO contract_codes(contract_id, system, code) VALUES (?, 'okpd2', ?)",
        (contract_id, okpd2),
    )
    conn.commit()


def test_contract_coverage_is_unverified_without_history_checkpoint(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        coverage = procurement_contract_coverage(conn, region_code=2, year=2025)
    finally:
        conn.close()

    assert coverage["status"] == "unverified"
    assert coverage["confidence"] == 0.25
    assert coverage["completed_coverage_fraction"] == 0.0
    assert coverage["warning"] is not None


def test_complete_history_allows_high_entry_opportunity_for_absent_supplier(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        _manufacturer(conn)
        _complete_contract_history(conn)
        _contract(
            conn, inn="9999999999", region=2, price=500_000_000, number="MARKET-DEMAND"
        )
        result = procurement_entry_opportunities(
            conn,
            region_code=2,
            year=2025,
            manufacturer_source="both",
            limit=20,
        )
    finally:
        conn.close()

    assert result["coverage"]["status"] == "complete"
    assert result["ranking_status"] == "final"
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["procurement_entry_tier"] == "high"
    assert row["procurement_entry_score"] >= 75
    assert row["regional_supplier_contracts_year"] == 0
    assert row["regional_demand_value_year_rub"] == 500_000_000
    assert row["addressable_market_rub"] == 500_000_000
    assert row["addressable_contracts"] == 1
    assert row["regional_demand_buyers_year"] == 1
    assert row["addressable_market_to_revenue_ratio"] == 0.5
    assert row["market_materiality_basis"] == "revenue_and_employees"
    assert row["components"]["market_materiality"] > 0
    assert "no_observed_regional_supplier_contracts" in row["reasons"]


def test_zero_contract_signal_stays_needs_history_when_coverage_is_unverified(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        _manufacturer(conn)
        _contract(
            conn, inn="9999999999", region=2, price=500_000_000, number="MARKET-DEMAND"
        )
        result = procurement_entry_opportunities(
            conn,
            region_code=2,
            year=2025,
            manufacturer_source="both",
            limit=20,
        )
    finally:
        conn.close()

    row = result["rows"][0]
    assert result["ranking_status"] == "provisional"
    assert row["procurement_entry_tier"] == "needs_history"
    assert "regional_contract_history_not_complete" in row["warnings"]


def test_entry_score_uses_contracts_from_target_customer_region_only(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        _manufacturer(conn)
        _complete_contract_history(conn, region=2)
        _contract(conn, inn="9999999999", region=2, price=500_000_000, number="MARKET-DEMAND")
        _contract(conn, inn="1234567890", region=77, price=500_000_000, number="OUTSIDE")
        result = procurement_entry_opportunities(
            conn,
            region_code=2,
            year=2025,
            manufacturer_source="both",
            limit=20,
        )
    finally:
        conn.close()

    row = result["rows"][0]
    assert row["regional_supplier_contracts_year"] == 0
    assert row["regional_supplier_contract_value_year_rub"] == 0
    assert row["procurement_entry_tier"] == "high"


def test_observed_regional_contracts_reduce_entry_score(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        _manufacturer(conn)
        _complete_contract_history(conn)
        _contract(conn, inn="9999999999", region=2, price=500_000_000, number="MARKET-DEMAND")
        baseline = procurement_entry_opportunities(
            conn,
            region_code=2,
            year=2025,
            manufacturer_source="both",
            limit=20,
        )["rows"][0]
        _contract(conn, inn="1234567890", region=2, price=300_000_000, number="LOCAL")
        active = procurement_entry_opportunities(
            conn,
            region_code=2,
            year=2025,
            manufacturer_source="both",
            limit=20,
        )["rows"][0]
    finally:
        conn.close()

    assert active["regional_supplier_contracts_year"] == 1
    assert active["regional_supplier_contract_value_year_rub"] == 300_000_000
    assert active["procurement_entry_score"] < baseline["procurement_entry_score"]


def test_cli_parses_procurement_entry_and_coverage_commands() -> None:
    from procure_radar.cli import build_parser

    parser = build_parser()
    coverage = parser.parse_args(["procurement-coverage", "--region", "2", "--year", "2025"])
    assert coverage.region == 2
    assert coverage.year == 2025

    ranking = parser.parse_args(
        [
            "procurement-entry-opportunities",
            "--region",
            "2",
            "--year",
            "2025",
            "--manufacturer-source",
            "both",
            "--min-score",
            "60",
        ]
    )
    assert ranking.region == 2
    assert ranking.year == 2025
    assert ranking.manufacturer_source == "both"
    assert ranking.min_score == 60.0

    demand = parser.parse_args(["regional-demand", "1234567890", "--region", "2", "--year", "2025"])
    assert demand.inn == "1234567890"
    assert demand.region == 2
    assert demand.year == 2025

    buyers = parser.parse_args(
        [
            "regional-demand-buyers",
            "1234567890",
            "--region",
            "2",
            "--year",
            "2025",
            "--contracts-per-buyer",
            "5",
        ]
    )
    assert buyers.inn == "1234567890"
    assert buyers.contracts_per_buyer == 5

    competitors = parser.parse_args(
        ["regional-demand-competitors", "1234567890", "--region", "2", "--year", "2025"]
    )
    assert competitors.inn == "1234567890"
    assert competitors.region == 2
    assert competitors.year == 2025
