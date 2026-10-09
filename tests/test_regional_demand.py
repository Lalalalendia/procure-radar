from __future__ import annotations

from pathlib import Path

from procure_radar.db import connect
from procure_radar.regional_demand import okpd2_family, regional_manufacturer_demand


def _manufacturer(conn, inn: str = "1234567890", code: str | None = "28.13.00.110") -> None:
    conn.execute(
        "INSERT INTO organizations(inn, name, region_code) VALUES (?, 'ООО Завод', 2)",
        (inn,),
    )
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES (?, 'ООО Завод', ?, 'Компрессор', ?, 'active', 1, '{}')
        """,
        (f"G-{inn}", inn, code),
    )
    conn.commit()


def _contract(
    conn,
    *,
    number: str,
    price: float,
    codes: list[tuple[str, str]],
    supplier: str,
    customer: str,
    region: int = 2,
) -> None:
    raw = conn.execute(
        "INSERT INTO raw_documents(source, endpoint, external_id, payload_json) VALUES ('t','c',?,'{}')",
        (number,),
    )
    cur = conn.execute(
        """
        INSERT INTO contracts(
            raw_document_id, reg_num, customer_inn, price, region_code, published_at
        ) VALUES (?, ?, ?, ?, ?, '2025-07-01')
        """,
        (int(raw.lastrowid), number, customer, price, region),
    )
    contract_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, ?)",
        (contract_id, supplier),
    )
    for system, code in codes:
        conn.execute(
            "INSERT INTO contract_codes(contract_id, system, code) VALUES (?, ?, ?)",
            (contract_id, system, code),
        )
    conn.commit()


def test_okpd2_family_joins_detailed_okpd2_and_ktru_prefix() -> None:
    assert okpd2_family("28.13.00.110") == "28.13.00"
    assert okpd2_family("28.13.00") == "28.13.00"
    assert okpd2_family("28.13.00.110-00000001") == "28.13.00"
    assert okpd2_family("28.13") is None


def test_regional_demand_allocates_multicode_value_and_measures_own_share(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        _manufacturer(conn)
        _contract(
            conn,
            number="C1",
            price=100_000_000,
            codes=[("okpd2", "28.13.00.110"), ("okpd2", "27.40.00.100")],
            supplier="9999999999",
            customer="0200000001",
        )
        _contract(
            conn,
            number="C2",
            price=60_000_000,
            codes=[("ktru", "28.13.00.110-00000001")],
            supplier="1234567890",
            customer="0200000002",
        )
        _contract(
            conn,
            number="C3",
            price=200_000_000,
            codes=[("okpd2", "31.01.00.100")],
            supplier="8888888888",
            customer="0200000003",
        )
        result = regional_manufacturer_demand(
            conn,
            manufacturer_inn="1234567890",
            region_code=2,
            year=2025,
            history_confidence=1.0,
            history_status="complete",
        )
    finally:
        conn.close()

    assert result["match_status"] == "matched"
    assert result["matched_okpd2_families"] == ["28.13.00"]
    assert result["regional_demand_value_year_rub"] == 110_000_000
    assert result["regional_demand_contracts_year"] == 2
    assert result["regional_demand_buyers_year"] == 2
    assert result["manufacturer_matched_contracts_year"] == 1
    assert result["manufacturer_matched_value_year_rub"] == 60_000_000
    assert result["addressable_market_rub"] == 50_000_000
    assert result["addressable_contracts"] == 1
    assert result["addressable_buyers"] == 1
    assert result["manufacturer_regional_market_share_year"] == 0.545455
    assert result["demand_coverage"]["status"] == "good"
    assert result["demand_coverage"]["contract_classifier_coverage_pct"] == 100.0


def test_regional_demand_reports_missing_manufacturer_okpd2(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        _manufacturer(conn, code=None)
        _contract(
            conn,
            number="C1",
            price=10_000_000,
            codes=[("okpd2", "28.13.00.110")],
            supplier="9999999999",
            customer="0200000001",
        )
        result = regional_manufacturer_demand(
            conn,
            manufacturer_inn="1234567890",
            region_code=2,
            year=2025,
        )
    finally:
        conn.close()

    assert result["match_status"] == "no_manufacturer_okpd2"
    assert result["regional_demand_value_year_rub"] == 0


def test_regional_demand_buyers_and_competitors_explain_addressable_market(tmp_path: Path) -> None:
    from procure_radar.regional_demand import regional_demand_buyers, regional_demand_competitors

    conn = connect(tmp_path / "db.sqlite3")
    try:
        _manufacturer(conn)
        _contract(
            conn,
            number="C1",
            price=100_000_000,
            codes=[("okpd2", "28.13.00.110")],
            supplier="9000000001",
            customer="0200000001",
        )
        _contract(
            conn,
            number="C2",
            price=40_000_000,
            codes=[("okpd2", "28.13.00.110")],
            supplier="9000000002",
            customer="0200000002",
        )
        _contract(
            conn,
            number="C3",
            price=60_000_000,
            codes=[("okpd2", "28.13.00.110")],
            supplier="1234567890",
            customer="0200000002",
        )
        c3_id = int(conn.execute("SELECT id FROM contracts WHERE reg_num='C3'").fetchone()[0])
        conn.execute(
            "INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, '9000000002')",
            (c3_id,),
        )
        conn.execute("UPDATE contracts SET subject='Поставка компрессоров' WHERE reg_num='C1'")
        conn.execute("UPDATE organizations SET name='Заказчик Один' WHERE inn='0200000001'")
        conn.execute("UPDATE organizations SET name='Заказчик Два' WHERE inn='0200000002'")
        conn.execute("UPDATE organizations SET name='Конкурент А' WHERE inn='9000000001'")
        conn.execute("UPDATE organizations SET name='Конкурент Б' WHERE inn='9000000002'")
        conn.commit()

        buyers = regional_demand_buyers(
            conn,
            manufacturer_inn="1234567890",
            region_code=2,
            year=2025,
            limit=10,
            contracts_per_buyer=2,
        )
        competitors = regional_demand_competitors(
            conn,
            manufacturer_inn="1234567890",
            region_code=2,
            year=2025,
            limit=10,
        )
    finally:
        conn.close()

    assert buyers["addressable_market_rub"] == 170_000_000
    assert buyers["total"] == 2
    assert buyers["buyer_concentration"]["top_5_buyers_value_rub"] == 170_000_000
    assert buyers["buyer_concentration"]["top_1_share_pct"] == 58.82
    first = buyers["rows"][0]
    assert first["buyer_inn"] == "0200000001"
    assert first["buyer_name"] == "Заказчик Один"
    assert first["addressable_value_rub"] == 100_000_000
    assert first["addressable_contracts"] == 1
    assert first["top_contracts"][0]["subject"] == "Поставка компрессоров"
    assert first["incumbent_suppliers"][0]["supplier_name"] == "Конкурент А"

    assert competitors["total"] == 2
    assert competitors["competitor_concentration"]["attributed_addressable_market_rub"] == 170_000_000
    assert competitors["competitor_concentration"]["top_1_share_pct"] == 58.82
    assert competitors["rows"][0]["supplier_inn"] == "9000000001"
    assert competitors["rows"][0]["supplier_name"] == "Конкурент А"
    assert competitors["rows"][0]["matched_value_rub"] == 100_000_000
    assert competitors["rows"][1]["matched_value_rub"] == 70_000_000


def test_buyer_contestability_detects_supplier_switching(tmp_path: Path) -> None:
    from procure_radar.regional_demand import regional_demand_buyers

    conn = connect(tmp_path / "db.sqlite3")
    try:
        _manufacturer(conn, code="25.30.12.110")
        _contract(
            conn,
            number="S1",
            price=30_000_000,
            codes=[("okpd2", "25.30.12.110")],
            supplier="9000000001",
            customer="0200000001",
        )
        _contract(
            conn,
            number="S2",
            price=20_000_000,
            codes=[("okpd2", "25.30.12.110")],
            supplier="9000000002",
            customer="0200000001",
        )
        _contract(
            conn,
            number="S3",
            price=10_000_000,
            codes=[("okpd2", "25.30.12.110")],
            supplier="9000000003",
            customer="0200000001",
        )
        conn.execute("UPDATE contracts SET published_at='2025-01-01' WHERE reg_num='S1'")
        conn.execute("UPDATE contracts SET published_at='2025-05-01' WHERE reg_num='S2'")
        conn.execute("UPDATE contracts SET published_at='2025-09-01' WHERE reg_num='S3'")
        conn.commit()
        result = regional_demand_buyers(
            conn,
            manufacturer_inn="1234567890",
            region_code=2,
            year=2025,
            limit=10,
            contracts_per_buyer=0,
        )
    finally:
        conn.close()

    buyer = result["rows"][0]
    assert buyer["distinct_incumbent_suppliers"] == 3
    assert buyer["supplier_known_contracts"] == 3
    assert buyer["supplier_switches"] == 2
    assert buyer["supplier_switch_opportunities"] == 2
    assert buyer["supplier_switch_rate"] == 1.0
    assert buyer["supplier_repeat_rate"] == 0.0
    assert buyer["contestability_confidence"] == 1.0
    assert buyer["contestability_score"] >= 80
    assert buyer["contestability_tier"] == "high"
    assert buyer["buyer_opportunity_score"] >= 70
    assert buyer["buyer_opportunity_tier"] == "high"
    assert buyer["first_addressable_contract_at"] == "2025-01-01"
    assert buyer["last_addressable_contract_at"] == "2025-09-01"
