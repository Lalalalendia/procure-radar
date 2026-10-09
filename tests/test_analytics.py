import sqlite3

from procure_radar.analytics import (
    compute_manufacturing_opportunities,
    compute_opportunities,
    compute_buyer_profile,
    compute_buyer_supplier_relationships,
    compute_supplier_profile,
    compute_winner_bundles,
    compute_winner_concentration,
)
from procure_radar.db import SCHEMA
from procure_radar.ingest import ingest_contract, ingest_purchase, ingest_tender_protocol


def _purchase(number: str, month: str, amount: float):
    return {
        "purchase_number": number,
        "region": 2,
        "stage": 2,
        "max_price": amount,
        "published_at": f"2026-{month}-01T00:00:00",
        "docs": [
            {
                "doc_type": "epNotificationEF2020",
                "source": {
                    "notificationInfo": {
                        "purchaseObjectsInfo": {
                            "notDrugPurchaseObjectsInfo": {
                                "purchaseObject": {
                                    "externalSid": f"item-{number}",
                                    "name": "Тестовый товар",
                                    "KTRU": {
                                        "code": "01.02.03.004-00000001",
                                        "name": "Тестовый товар",
                                        "OKPD2": {"OKPDCode": "01.02.03.004"},
                                    },
                                    "quantity": {"value": "1"},
                                    "price": str(amount),
                                    "sum": str(amount),
                                }
                            }
                        }
                    }
                },
            }
        ],
    }


def _protocol(number: str, ident: str, price: float):
    return {
        "doc_type": "epProtocolEF2020Final",
        "published_at": "2026-03-01T00:00:00",
        "source": {
            "id": ident,
            "commonInfo": {"purchaseNumber": number},
            "protocolInfo": {
                "applicationsInfo": {
                    "applicationInfo": {
                        "commonInfo": {"appNumber": f"app-{ident}"},
                        "finalPrice": str(price),
                        "admittedInfo": {"appAdmittedInfo": {"admitted": "true", "appRating": "1"}},
                    }
                },
                "abandonedReason": {"code": "ONE", "name": "Одна заявка"},
            },
        },
    }


def _mark_gisp_complete(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """
        INSERT INTO gisp_import_runs(source_path, source_scope, completed_at, header_json)
        VALUES('test.xlsx', 'active', datetime('now'), '[]')
        """
    )
    return int(cur.lastrowid)


def _add_gisp_product(
    conn: sqlite3.Connection,
    *,
    registry_number: str,
    okpd2_code: str,
    manufacturer_inn: str,
) -> None:
    run_id = conn.execute("SELECT MAX(id) FROM gisp_import_runs").fetchone()[0]
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, last_seen_run_id, raw_json
        ) VALUES(?, ?, ?, ?, ?, 'active', 1, ?, '{}')
        """,
        (
            registry_number,
            f"Manufacturer {manufacturer_inn}",
            manufacturer_inn,
            f"Product {registry_number}",
            okpd2_code,
            run_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO gisp_registry_rows(
            row_key, registry_number, product_name, okpd2_code,
            source_scope, last_seen_run_id, raw_json
        ) VALUES(?, ?, ?, ?, 'active', ?, '{}')
        """,
        (
            f"row-{registry_number}",
            registry_number,
            f"Product {registry_number}",
            okpd2_code,
            run_id,
        ),
    )


def test_compute_opportunities_uses_repeat_demand_and_protocols():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _purchase("P1", "01", 1_000_000))
    ingest_purchase(conn, _purchase("P2", "02", 2_000_000))
    ingest_tender_protocol(conn, _protocol("P1", "A", 1_000_000))
    ingest_tender_protocol(conn, _protocol("P2", "B", 2_000_000))
    rows = compute_opportunities(conn, region_code=2, min_procurements=2, min_protocol_coverage=1)
    assert len(rows) == 1
    assert rows[0]["procurements"] == 2
    assert rows[0]["demand_rub"] == 3_000_000
    assert rows[0]["one_bid_share"] == 1.0
    assert rows[0]["failed_share"] == 1.0
    assert rows[0]["median_discount_pct"] == 0.0


def test_compute_opportunities_falls_back_to_aggregate_codes_without_items():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    purchase = {
        "purchase_number": "PF1",
        "region": 2,
        "stage": 2,
        "purchase_type": "epNotificationEF2020",
        "max_price": 1_000_000,
        "published_at": "2026-01-01T00:00:00",
        "object_info": "Поставка агрегатного товара",
        "ktru": ["11.22.33.444-00000001"],
    }
    ingest_purchase(conn, purchase)
    ingest_tender_protocol(conn, _protocol("PF1", "PF1-P", 1_000_000))
    rows = compute_opportunities(conn, region_code=2, min_procurements=1, min_protocol_coverage=1)
    assert len(rows) == 1
    assert rows[0]["code"] == "11.22.33.444-00000001"
    assert rows[0]["demand_rub"] == 1_000_000
    assert rows[0]["line_item_coverage"] == 0.0
    assert rows[0]["one_bid_share"] == 1.0


def test_compute_opportunities_caps_single_code_demand_at_purchase_nmck():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    purchase = _purchase("PX", "01", 1_000_000)
    obj = purchase["docs"][0]["source"]["notificationInfo"]["purchaseObjectsInfo"]["notDrugPurchaseObjectsInfo"]["purchaseObject"]
    obj["price"] = "2666555352.42"
    obj["sum"] = "2666555352.42"
    obj["quantity"] = {"undefined": "true"}
    ingest_purchase(conn, purchase)
    ingest_tender_protocol(conn, _protocol("PX", "PX-P", 1_000_000))
    rows = compute_opportunities(conn, region_code=2, min_procurements=1, min_protocol_coverage=1)
    assert len(rows) == 1
    assert rows[0]["demand_rub"] == 1_000_000
    assert rows[0]["exact_amount_coverage"] == 0.0
    assert rows[0]["normalized_amount_coverage"] == 1.0


def test_compute_opportunities_normalizes_multicode_budget_without_inflation():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    purchase = _purchase("PM", "01", 100_000)
    base = purchase["docs"][0]["source"]["notificationInfo"]["purchaseObjectsInfo"]["notDrugPurchaseObjectsInfo"]
    base["purchaseObject"] = [
        {
            "externalSid": "a",
            "name": "A",
            "KTRU": {"code": "01.01.01.001-00000001", "name": "A"},
            "quantity": {"undefined": "true"},
            "price": "9000000",
            "sum": "9000000",
        },
        {
            "externalSid": "b",
            "name": "B",
            "KTRU": {"code": "02.02.02.002-00000001", "name": "B"},
            "quantity": {"undefined": "true"},
            "price": "1000000",
            "sum": "1000000",
        },
    ]
    ingest_purchase(conn, purchase)
    ingest_tender_protocol(conn, _protocol("PM", "PM-P", 100_000))
    rows = compute_opportunities(conn, region_code=2, min_procurements=1, min_protocol_coverage=1)
    assert len(rows) == 2
    assert sum(row["demand_rub"] for row in rows) == 100_000
    assert {row["demand_rub"] for row in rows} == {50_000}
    assert all(row["normalized_amount_coverage"] == 1.0 for row in rows)


def test_opportunities_expose_buyer_diversity_segment_and_supplier_gap():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    p1 = _purchase("BD1", "01", 100_000)
    p2 = _purchase("BD2", "02", 120_000)
    p1["customers"] = ["1000000001"]
    p2["customers"] = ["1000000002"]
    ingest_purchase(conn, p1)
    ingest_purchase(conn, p2)
    ingest_tender_protocol(conn, _protocol("BD1", "BD1-P", 100_000))
    ingest_tender_protocol(conn, _protocol("BD2", "BD2-P", 120_000))

    rows = compute_opportunities(
        conn,
        region_code=2,
        min_procurements=2,
        min_protocol_coverage=1,
        segment="standard_goods",
        gap_type="supplier_gap",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["segment"] == "standard_goods"
    assert row["group"] == "goods"
    assert row["gap_type"] == "supplier_gap"
    assert row["supplier_gap_share"] == 1.0
    assert row["buyers_count"] == 2
    assert row["top_buyer_share"] == 0.5
    assert row["buyer_diversity_score"] > 0.3


def test_opportunities_can_isolate_new_signals_with_max_procurements():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _purchase("NEW1", "01", 100_000))
    ingest_tender_protocol(conn, _protocol("NEW1", "NEW1-P", 100_000))
    rows = compute_opportunities(
        conn,
        region_code=2,
        min_procurements=1,
        max_procurements=1,
        min_protocol_coverage=1,
    )
    assert len(rows) == 1
    assert rows[0]["procurements"] == 1


def test_competitive_majority_is_not_mislabeled_as_supplier_gap():
    from collections import Counter
    from procure_radar.analytics import _gap_metrics

    gap = _gap_metrics(Counter({"competitive": 2, "single_submitted": 1}), 3)
    assert gap["gap_type"] == "competitive_market"
    assert gap["supplier_gap_share"] == 0.333


def test_opportunities_include_rzn_okpd2_enrichment(tmp_path):
    from procure_radar.db import connect
    from procure_radar.rzn import upsert_med_product, upsert_nsi_record

    conn = connect(tmp_path / "radar.sqlite3")
    try:
        raw_purchase = conn.execute(
            "INSERT INTO raw_documents(source, endpoint, external_id, payload_json) VALUES('test','purchases','p1','{}')"
        ).lastrowid
        raw_protocol = conn.execute(
            "INSERT INTO raw_documents(source, endpoint, external_id, payload_json) VALUES('test','protocols','pr1','{}')"
        ).lastrowid
        conn.execute(
            """
            INSERT INTO purchases(raw_document_id,purchase_number,region_code,max_price,purchase_type,stage,published_at)
            VALUES(?, 'p1', 2, 100000, 'epNotificationEF2020', 2, '2026-08-01')
            """,
            (raw_purchase,),
        )
        pid = conn.execute("SELECT id FROM purchases WHERE purchase_number='p1'").fetchone()[0]
        conn.execute(
            """
            INSERT INTO purchase_items(purchase_id,item_key,name,okpd2_code,okpd2_name,amount)
            VALUES(?,?,?,?,?,?)
            """,
            (pid, 'i1', 'Ингалятор', '26.60.13.110', 'Ингаляторы', 100000),
        )
        conn.execute(
            "INSERT INTO purchase_documents(purchase_id, doc_type) VALUES(?, 'epProtocolEF2020Final')",
            (pid,),
        )
        conn.execute(
            """
            INSERT INTO tender_protocols(
                raw_document_id,protocol_key,purchase_number,doc_type,applications_count,admitted_count,final_price,is_abandoned
            ) VALUES(?, 'pr1','p1','epProtocolEF2020Final',1,1,100000,1)
            """,
            (raw_protocol,),
        )
        med = {
            'id': 123,
            'status': {'id': 1, 'code': 'active', 'name': 'Действует'},
            'legalSystem': 'RUSSIA',
            'noRu': 'РЗН 2026/123',
            'dateRu': '2026-01-01',
            'name': 'Ингалятор',
            'producer': {'name': 'Producer'},
            'representative': {'name': 'Representative'},
            'nomClassifierMedicalRfIds': ['nsi1'],
        }
        upsert_med_product(conn, med)
        upsert_nsi_record(
            conn,
            {
                'recordId': 'nsi1',
                'catalogCode': 'okpd2Code',
                'statusCode': 'APPROVED',
                'attributeSet': {'code': '26.60.13.110', 'name': 'Ингаляторы'},
            },
        )
        conn.commit()
        rows = compute_opportunities(
            conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
        )
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]['rzn_enrichment'] == 'partial'
    assert rows[0]['rzn_okpd2_code'] == '26.60.13.110'
    assert rows[0]['rzn_active_products'] == 1
    assert rows[0]['rzn_active_producers'] == 1


def test_ktru_gisp_enrichment_is_explicitly_parent_okpd2():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _purchase("GP1", "01", 100_000))
    ingest_tender_protocol(conn, _protocol("GP1", "GP1-P", 100_000))
    _mark_gisp_complete(conn)
    _add_gisp_product(
        conn,
        registry_number="R1",
        okpd2_code="01.02.03.004",
        manufacturer_inn="7700000001",
    )

    rows = compute_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["system"] == "ktru"
    assert row["gisp_okpd2_code"] == "01.02.03.004"
    assert row["gisp_match_state"] == "parent_okpd2"
    assert row["gisp_applicability"] == "applicable"
    assert row["gisp_parent_products"] == 1
    assert row["gisp_parent_manufacturers"] == 1
    assert row["manufacturer_gap_score"] is None
    assert row["distribution_gap_score"] is not None


def test_service_market_does_not_report_gisp_zero_manufacturers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    purchase = _purchase("SVC1", "01", 100_000)
    obj = purchase["docs"][0]["source"]["notificationInfo"]["purchaseObjectsInfo"]["notDrugPurchaseObjectsInfo"]["purchaseObject"]
    obj["KTRU"]["code"] = "58.29.50.000-00000001"
    obj["KTRU"]["name"] = "Лицензия программного обеспечения"
    obj["KTRU"]["OKPD2"] = {"OKPDCode": "58.29.50.000"}
    obj["name"] = "Лицензия программного обеспечения"
    ingest_purchase(conn, purchase)
    ingest_tender_protocol(conn, _protocol("SVC1", "SVC1-P", 100_000))
    _mark_gisp_complete(conn)

    rows = compute_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["group"] == "services"
    assert row["gisp_match_state"] == "none"
    assert row["gisp_applicability"] == "not_applicable"
    assert row["distribution_gap_score"] is None
    assert row["manufacturer_gap_score"] is None


def test_manufacturing_rollup_collapses_multiple_ktru_under_same_okpd2():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    purchase = _purchase("MFG1", "01", 100_000)
    base = purchase["docs"][0]["source"]["notificationInfo"]["purchaseObjectsInfo"]["notDrugPurchaseObjectsInfo"]
    base["purchaseObject"] = [
        {
            "externalSid": "a",
            "name": "A",
            "KTRU": {
                "code": "26.20.11.110-00000001",
                "name": "A",
                "OKPD2": {"OKPDCode": "26.20.11.110"},
            },
            "quantity": {"undefined": "true"},
            "price": "9000000",
            "sum": "9000000",
        },
        {
            "externalSid": "b",
            "name": "B",
            "KTRU": {
                "code": "26.20.11.110-00000002",
                "name": "B",
                "OKPD2": {"OKPDCode": "26.20.11.110"},
            },
            "quantity": {"undefined": "true"},
            "price": "1000000",
            "sum": "1000000",
        },
    ]
    ingest_purchase(conn, purchase)
    ingest_tender_protocol(conn, _protocol("MFG1", "MFG1-P", 100_000))
    _mark_gisp_complete(conn)
    _add_gisp_product(
        conn,
        registry_number="MFG-R1",
        okpd2_code="26.20.11.110",
        manufacturer_inn="7700000002",
    )

    rows = compute_manufacturing_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["analysis_level"] == "okpd2"
    assert row["system"] == "okpd2"
    assert row["code"] == "26.20.11.110"
    assert row["procurements"] == 1
    assert row["demand_rub"] == 100_000
    assert row["gisp_match_state"] == "exact_okpd2"
    assert row["gisp_active_manufacturers"] == 1
    assert row["manufacturer_gap_score"] is not None
    assert row["demand_per_manufacturer"] == 100_000
    assert row["normalized_amount_coverage"] == 1.0
    assert row["demand_confidence"] == 0.65


def test_manufacturing_radar_excludes_unmatched_goods_by_default():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _purchase("UNK1", "01", 100_000))
    ingest_tender_protocol(conn, _protocol("UNK1", "UNK1-P", 100_000))
    _mark_gisp_complete(conn)

    rows = compute_manufacturing_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    assert rows == []

    rows = compute_manufacturing_opportunities(
        conn,
        region_code=2,
        min_procurements=1,
        min_protocol_coverage=1.0,
        include_unknown_gisp=True,
    )
    assert len(rows) == 1
    assert rows[0]["gisp_applicability"] == "unknown"
    assert rows[0]["manufacturer_gap_score"] is None


def test_contract_supplier_zero_is_unknown_when_contract_layer_not_loaded():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _purchase("CS0", "01", 500_000))
    ingest_tender_protocol(conn, _protocol("CS0", "CS0-P", 500_000))
    _mark_gisp_complete(conn)
    _add_gisp_product(
        conn,
        registry_number="CS0-R1",
        okpd2_code="01.02.03.004",
        manufacturer_inn="7700000003",
    )

    rows = compute_manufacturing_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["contract_supplier_enrichment"] == "unavailable"
    assert row["known_contract_suppliers"] is None
    assert row["known_contracts"] is None


def test_pharma_gisp_match_is_limited_not_manufacturer_denominator():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    purchase = _purchase("PH1", "01", 1_000_000)
    obj = purchase["docs"][0]["source"]["notificationInfo"]["purchaseObjectsInfo"]["notDrugPurchaseObjectsInfo"]["purchaseObject"]
    obj["KTRU"]["code"] = "21.20.10.211-00001"
    obj["KTRU"]["name"] = "Лекарственный препарат"
    obj["KTRU"]["OKPD2"] = {"OKPDCode": "21.20.10.211"}
    obj["name"] = "Лекарственный препарат"
    ingest_purchase(conn, purchase)
    ingest_tender_protocol(conn, _protocol("PH1", "PH1-P", 1_000_000))
    _mark_gisp_complete(conn)
    _add_gisp_product(
        conn,
        registry_number="PH-R1",
        okpd2_code="21.20.10.211",
        manufacturer_inn="7700000004",
    )

    rows = compute_manufacturing_opportunities(
        conn,
        region_code=2,
        min_procurements=1,
        min_protocol_coverage=1.0,
        include_unknown_gisp=True,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["segment"] == "pharma"
    assert row["gisp_applicability"] == "limited"
    assert row["manufacturer_gap_score"] is None
    assert row["distribution_gap_score"] is None


def test_maximum_contract_price_purchase_does_not_create_fake_9999_discount():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    purchase = _purchase("UNIT1", "01", 1_000_000)
    info = purchase["docs"][0]["source"]["notificationInfo"]["purchaseObjectsInfo"]["notDrugPurchaseObjectsInfo"]
    info["quantityUndefined"] = True
    ingest_purchase(conn, purchase)
    ingest_tender_protocol(conn, _protocol("UNIT1", "UNIT1-P", 52.50))

    rows = compute_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["median_discount_pct"] is None
    assert row["discount_coverage"] == 0.0
    assert row["discount_incomparable"] == 1
    assert row["discount_suspicious"] == 0
    assert row["pricing_modes"] == {"maximum_contract_price": 1}


def test_fixed_contract_price_purchase_keeps_comparable_discount():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    purchase = _purchase("FIXED1", "01", 1_000_000)
    info = purchase["docs"][0]["source"]["notificationInfo"]["purchaseObjectsInfo"]["notDrugPurchaseObjectsInfo"]
    info["quantityUndefined"] = False
    ingest_purchase(conn, purchase)
    ingest_tender_protocol(conn, _protocol("FIXED1", "FIXED1-P", 900_000))

    rows = compute_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["median_discount_pct"] == 10.0
    assert row["discount_coverage"] == 1.0
    assert row["discount_incomparable"] == 0
    assert row["pricing_modes"] == {"fixed_contract_price": 1}


def _contract_payload(
    purchase_number: str,
    reg_num: str,
    price: float,
    suppliers: list[str],
    *,
    ktru: list[str] | None = None,
    okpd2: list[str] | None = None,
) -> dict:
    return {
        "currency_code": "RUB",
        "customer": "0200000000",
        "price": price,
        "published_at": "2026-03-10T00:00:00",
        "purchase_number": purchase_number,
        "reg_num": reg_num,
        "region": 2,
        "stage": "E",
        "subject": "Тестовый товар",
        "suppliers": suppliers,
        "ktru": ktru if ktru is not None else ["01.02.03.004-00000001"],
        "okpd2": okpd2 if okpd2 is not None else [],
        "docs": [{"doc_type": "contract", "published_at": "2026-03-10T00:00:00"}],
    }


def test_contract_winner_concentration_is_coverage_aware():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for idx in range(1, 5):
        number = f"WC{idx}"
        ingest_purchase(conn, _purchase(number, f"0{idx}", 100_000))
        ingest_tender_protocol(conn, _protocol(number, f"{number}-P", 100_000))
    ingest_contract(conn, _contract_payload("WC1", "C-WC1", 100_000, ["1111111111"]))
    ingest_contract(conn, _contract_payload("WC2", "C-WC2", 100_000, ["1111111111"]))
    ingest_contract(conn, _contract_payload("WC3", "C-WC3", 100_000, ["2222222222"]))

    rows = compute_opportunities(
        conn, region_code=2, min_procurements=4, min_protocol_coverage=1.0
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["known_contracts"] == 3
    assert row["known_contract_suppliers"] == 2
    assert row["winner_purchase_coverage"] == 0.75
    assert row["winner_value_coverage"] == 1.0
    assert row["top_supplier_inn"] == "1111111111"
    assert row["top1_supplier_share"] == 0.6667
    assert row["top3_supplier_share"] == 1.0
    assert row["supplier_hhi"] == 5555.56
    assert row["supplier_concentration_basis"] == "contract_value"
    assert row["supplier_concentration_evidence"] == "medium"


def test_multi_supplier_contract_splits_value_instead_of_double_counting():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _purchase("MS1", "01", 100_000))
    ingest_tender_protocol(conn, _protocol("MS1", "MS1-P", 100_000))
    ingest_contract(
        conn,
        _contract_payload("MS1", "C-MS1", 100_000, ["1111111111", "2222222222"]),
    )

    row = compute_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )[0]
    assert row["known_contract_suppliers"] == 2
    assert row["top1_supplier_share"] == 0.5
    assert row["supplier_hhi"] == 5000.0


def test_winner_concentration_filters_on_contract_evidence():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for idx in range(1, 5):
        number = f"WF{idx}"
        ingest_purchase(conn, _purchase(number, f"0{idx}", 100_000))
        ingest_tender_protocol(conn, _protocol(number, f"{number}-P", 100_000))
    ingest_contract(conn, _contract_payload("WF1", "C-WF1", 100_000, ["1111111111"]))
    ingest_contract(conn, _contract_payload("WF2", "C-WF2", 100_000, ["1111111111"]))

    rows = compute_winner_concentration(
        conn,
        region_code=2,
        min_procurements=4,
        min_protocol_coverage=1.0,
        min_contract_coverage=0.5,
        min_contracts=2,
    )
    assert len(rows) == 1
    assert rows[0]["winner_purchase_coverage"] == 0.5
    assert rows[0]["top1_supplier_share"] == 1.0

    rows = compute_winner_concentration(
        conn,
        region_code=2,
        min_procurements=4,
        min_protocol_coverage=1.0,
        min_contract_coverage=0.75,
        min_contracts=2,
    )
    assert rows == []


def _multi_ktru_purchase(number: str, amount: float = 100_000) -> dict:
    codes = ["01.02.03.004-00000001", "01.02.03.004-00000002"]
    objects = []
    for idx, code in enumerate(codes, 1):
        objects.append(
            {
                "externalSid": f"item-{number}-{idx}",
                "name": f"Тестовый товар {idx}",
                "KTRU": {
                    "code": code,
                    "name": f"Тестовый товар {idx}",
                    "OKPD2": {"OKPDCode": "01.02.03.004"},
                },
                "quantity": {"value": "1"},
                "price": str(amount / 2),
                "sum": str(amount / 2),
            }
        )
    return {
        "purchase_number": number,
        "region": 2,
        "stage": 2,
        "max_price": amount,
        "published_at": "2026-03-01T00:00:00",
        "docs": [
            {
                "doc_type": "epNotificationEF2020",
                "source": {
                    "notificationInfo": {
                        "purchaseObjectsInfo": {
                            "notDrugPurchaseObjectsInfo": {
                                "purchaseObject": objects
                            }
                        }
                    }
                },
            }
        ],
    }


def test_contract_codes_prevent_sibling_ktru_false_attribution():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _multi_ktru_purchase("CX1"))
    ingest_tender_protocol(conn, _protocol("CX1", "CX1-P", 100_000))
    ingest_contract(
        conn,
        _contract_payload(
            "CX1",
            "C-CX1",
            100_000,
            ["1111111111"],
            ktru=["01.02.03.004-00000001"],
        ),
    )

    rows = compute_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    by_code = {row["code"]: row for row in rows}
    first = by_code["01.02.03.004-00000001"]
    second = by_code["01.02.03.004-00000002"]
    assert first["known_contracts"] == 1
    assert first["supplier_concentration_match"] == "exact_ktru"
    assert first["winner_code_match_coverage"] == 1.0
    assert second["known_contracts"] == 0
    assert second["top1_supplier_share"] is None


def test_multicode_contract_value_is_allocated_across_ktru_codes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _multi_ktru_purchase("CA1"))
    ingest_tender_protocol(conn, _protocol("CA1", "CA1-P", 100_000))
    ingest_contract(
        conn,
        _contract_payload(
            "CA1",
            "C-CA1",
            100_000,
            ["1111111111"],
            ktru=[
                "01.02.03.004-00000001",
                "01.02.03.004-00000002",
            ],
        ),
    )

    native = compute_opportunities(
        conn, region_code=2, min_procurements=1, min_protocol_coverage=1.0
    )
    assert {row["winner_observed_value_rub"] for row in native} == {50_000.0}
    assert {row["supplier_concentration_match"] for row in native} == {"exact_ktru"}

    rolled = compute_opportunities(
        conn,
        region_code=2,
        min_procurements=1,
        min_protocol_coverage=1.0,
        analysis_level="okpd2",
    )
    assert len(rolled) == 1
    assert rolled[0]["code"] == "01.02.03.004"
    assert rolled[0]["winner_observed_value_rub"] == 100_000.0
    assert rolled[0]["supplier_concentration_match"] == "exact_okpd2"


def test_winner_bundles_collapse_shared_multiktru_contract_evidence():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    codes = [
        "01.02.03.004-00000001",
        "01.02.03.004-00000002",
    ]
    for idx in range(1, 3):
        number = f"WB{idx}"
        ingest_purchase(conn, _multi_ktru_purchase(number, 100_000))
        ingest_tender_protocol(conn, _protocol(number, f"{number}-P", 100_000))
        ingest_contract(
            conn,
            _contract_payload(
                number,
                f"C-{number}",
                100_000,
                ["744515433543"],
                ktru=codes,
            ),
        )

    bundles = compute_winner_bundles(
        conn,
        region_code=2,
        min_procurements=2,
        min_protocol_coverage=1.0,
        min_contract_coverage=1.0,
        min_contracts=2,
        min_markets=2,
    )
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle["markets_count"] == 2
    assert bundle["contracts"] == 2
    assert bundle["top_supplier_inn"] == "744515433543"
    assert bundle["top1_supplier_share"] == 1.0
    assert bundle["winner_bundle_value_rub"] == 200_000.0
    assert bundle["winner_allocated_value_per_market_rub"] == 100_000.0
    assert set(bundle["codes"]) == set(codes)


def test_winner_bundles_do_not_mix_different_contract_evidence_sets():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, _multi_ktru_purchase("WD1", 100_000))
    ingest_tender_protocol(conn, _protocol("WD1", "WD1-P", 100_000))
    ingest_contract(
        conn,
        _contract_payload(
            "WD1",
            "C-WD1-A",
            60_000,
            ["1111111111"],
            ktru=["01.02.03.004-00000001"],
        ),
    )
    ingest_contract(
        conn,
        _contract_payload(
            "WD1",
            "C-WD1-B",
            40_000,
            ["2222222222"],
            ktru=["01.02.03.004-00000002"],
        ),
    )

    bundles = compute_winner_bundles(
        conn,
        region_code=2,
        min_procurements=1,
        min_protocol_coverage=1.0,
        min_contract_coverage=1.0,
        min_contracts=1,
        min_markets=2,
    )
    assert bundles == []



def test_supplier_profile_splits_multi_supplier_value_and_ranks_customers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

    first = _contract_payload(
        "SP1", "C-SP1", 100_000, ["744515433543", "1111111111"],
        ktru=["01.02.03.004-00000001"],
    )
    first["customer"] = "0200000001"
    second = _contract_payload(
        "SP2", "C-SP2", 300_000, ["744515433543"],
        ktru=["01.02.03.004-00000002"],
    )
    second["customer"] = "0200000002"
    ingest_contract(conn, first)
    ingest_contract(conn, second)

    profile = compute_supplier_profile(
        conn, supplier_inn="744515433543", region_code=2, limit=10
    )
    assert profile["contracts"] == 2
    assert profile["purchases"] == 2
    assert profile["customers"] == 2
    assert profile["gross_contract_value_rub"] == 400_000.0
    assert profile["supplier_attributed_value_rub"] == 350_000.0
    assert profile["multi_supplier_contracts"] == 1
    assert profile["value_coverage"] == 1.0
    assert profile["top_customer_inn"] == "0200000002"
    assert profile["top_customer_share"] == 0.8571
    assert profile["repeat_customer_contract_share"] == 0.0
    assert profile["code_value_allocation"] == "equal_split_within_contract"


def test_supplier_profile_groups_codes_sharing_same_contract_evidence():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    codes = ["01.02.03.004-00000001", "01.02.03.004-00000002"]
    for idx in range(1, 3):
        ingest_contract(
            conn,
            _contract_payload(
                f"SB{idx}", f"C-SB{idx}", 100_000, ["744515433543"], ktru=codes
            ),
        )

    profile = compute_supplier_profile(
        conn, supplier_inn="744515433543", region_code=2, limit=10
    )
    ktru_bundles = [row for row in profile["bundles"] if row["system"] == "ktru"]
    assert len(ktru_bundles) == 1
    bundle = ktru_bundles[0]
    assert bundle["contracts"] == 2
    assert bundle["codes_count"] == 2
    assert bundle["supplier_value_rub"] == 200_000.0
    assert set(bundle["codes"]) == set(codes)
    assert {row["bundle_id"] for row in profile["ktru_markets"]} == {bundle["bundle_id"]}
    assert {row["supplier_value_rub"] for row in profile["ktru_markets"]} == {100_000.0}


def test_buyer_profile_measures_buyer_and_supplier_dependency_symmetrically():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

    first = _contract_payload(
        "BP1", "C-BP1", 100_000, ["744515433543"],
        ktru=["01.02.03.004-00000001"],
    )
    first["customer"] = "0267011483"
    second = _contract_payload(
        "BP2", "C-BP2", 300_000, ["744515433543"],
        ktru=["01.02.03.004-00000002"],
    )
    second["customer"] = "0267011483"
    other = _contract_payload(
        "BP3", "C-BP3", 600_000, ["1111111111"],
        ktru=["01.02.03.004-00000003"],
    )
    other["customer"] = "0267011483"
    supplier_elsewhere = _contract_payload(
        "BP4", "C-BP4", 600_000, ["744515433543"],
        ktru=["01.02.03.004-00000004"],
    )
    supplier_elsewhere["customer"] = "0200000002"
    for payload in (first, second, other, supplier_elsewhere):
        ingest_contract(conn, payload)

    profile = compute_buyer_profile(
        conn, customer_inn="0267011483", region_code=2, limit=10
    )
    assert profile["contracts"] == 3
    assert profile["suppliers"] == 2
    assert profile["gross_contract_value_rub"] == 1_000_000.0
    assert profile["top_supplier_inn"] == "1111111111"
    assert profile["top_supplier_share"] == 0.6
    assert profile["supplier_hhi"] == 5200.0
    assert profile["repeat_supplier_contract_share"] == round(2 / 3, 3)
    assert profile["code_value_allocation"] == "equal_split_within_contract"

    rows = {row["supplier_inn"]: row for row in profile["suppliers_ranked"]}
    assert rows["744515433543"]["buyer_share"] == 0.4
    assert rows["744515433543"]["supplier_region_value_rub"] == 1_000_000.0
    assert rows["744515433543"]["supplier_dependency_on_buyer"] == 0.4
    assert rows["744515433543"]["mutual_dependency"] == 0.4


def test_buyer_profile_splits_multi_supplier_contract_value():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    payload = _contract_payload(
        "BM1", "C-BM1", 100_000, ["1111111111", "2222222222"],
        ktru=["01.02.03.004-00000001"],
    )
    payload["customer"] = "0267011483"
    ingest_contract(conn, payload)
    profile = compute_buyer_profile(
        conn, customer_inn="0267011483", region_code=2, limit=10
    )
    assert profile["multi_supplier_contracts"] == 1
    assert profile["top_supplier_share"] == 0.5
    assert profile["supplier_hhi"] == 5000.0
    assert {row["buyer_attributed_value_rub"] for row in profile["suppliers_ranked"]} == {50_000.0}


def test_relationship_radar_detects_asymmetric_supplier_dependency():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

    rows = [
        ("R1", "P-R1", 1_700_000, "0267011483", ["744515433543"]),
        ("R2", "P-R2", 1_500_000, "0267011483", ["744515433543"]),
        ("R3", "P-R3", 5_800_000, "0267011483", ["1111111111"]),
    ]
    for reg, purchase, price, buyer, suppliers in rows:
        payload = _contract_payload(reg, purchase, price, suppliers)
        payload["customer"] = buyer
        ingest_contract(conn, payload)

    result = compute_buyer_supplier_relationships(
        conn,
        region_code=2,
        min_contracts=2,
        min_value_rub=100_000,
    )
    assert len(result) == 1
    row = result[0]
    assert row["buyer_inn"] == "0267011483"
    assert row["supplier_inn"] == "744515433543"
    assert row["contracts"] == 2
    assert row["pair_value_rub"] == 3_200_000.0
    assert row["buyer_share"] == round(3_200_000 / 9_000_000, 4)
    assert row["supplier_dependency"] == 1.0
    assert row["relationship_type"] == "supplier_dependent"
    assert row["evidence"] == "medium"


def test_relationship_radar_splits_multi_supplier_value_and_flags_mutual_concentration():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

    first = _contract_payload("RM1", "P-RM1", 1_000_000, ["111", "222"])
    first["customer"] = "BUYER"
    second = _contract_payload("RM2", "P-RM2", 1_000_000, ["111"])
    second["customer"] = "BUYER"
    third = _contract_payload("RM3", "P-RM3", 100_000, ["222"])
    third["customer"] = "OTHER"
    for payload in (first, second, third):
        ingest_contract(conn, payload)

    result = compute_buyer_supplier_relationships(
        conn,
        region_code=2,
        min_contracts=1,
        min_value_rub=0,
    )
    rows = {(row["buyer_inn"], row["supplier_inn"]): row for row in result}
    primary = rows[("BUYER", "111")]
    assert primary["pair_value_rub"] == 1_500_000.0
    assert primary["buyer_share"] == 0.75
    assert primary["supplier_dependency"] == 1.0
    assert primary["relationship_type"] == "mutual_concentration"
