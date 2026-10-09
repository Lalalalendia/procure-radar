from __future__ import annotations

import json
from pathlib import Path

from procure_radar.db import connect
from procure_radar.extract import extract_contract
from procure_radar.ingest import ingest_contract
from procure_radar.party_intelligence import backfill_contract_parties, extract_party_names


def test_extract_party_names_from_nested_eis_party_dicts() -> None:
    payload = {
        "docs": [
            {
                "source": {
                    "contractInfo": {
                        "customer": {
                            "INN": "0200000001",
                            "fullName": "ГКУ Республики Башкортостан Заказчик",
                        },
                        "suppliers": {
                            "supplierInfo": [
                                {
                                    "legalEntityRF": {
                                        "INN": "9000000001",
                                        "fullName": "ООО Поставщик Один",
                                    }
                                }
                            ]
                        },
                    }
                }
            }
        ]
    }
    names = extract_party_names(
        payload,
        wanted_inns={"0200000001", "9000000001"},
    )
    assert names == {
        "0200000001": "ГКУ Республики Башкортостан Заказчик",
        "9000000001": "ООО Поставщик Один",
    }


def test_ingest_contract_accepts_supplier_objects_and_warms_organization_names(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        payload = {
            "reg_num": "C-NAMES",
            "purchase_number": "P-NAMES",
            "customer": "0200000001",
            "customer_name": "ГБУ Заказчик",
            "suppliers": [
                {"inn": "9000000001", "name": "ООО Поставщик"},
            ],
            "price": 1_000_000,
            "region": 2,
            "published_at": "2025-05-01",
            "okpd2": ["25.30.12.110"],
        }
        parsed = extract_contract(payload)
        assert parsed["suppliers"] == ["9000000001"]
        ingest_contract(conn, payload)
        conn.commit()
        customer = conn.execute(
            "SELECT name, region_code FROM organizations WHERE inn='0200000001'"
        ).fetchone()
        supplier = conn.execute(
            "SELECT name FROM organizations WHERE inn='9000000001'"
        ).fetchone()
    finally:
        conn.close()

    assert customer["name"] == "ГБУ Заказчик"
    assert customer["region_code"] == 2
    assert supplier["name"] == "ООО Поставщик"


def test_contract_parties_backfill_uses_existing_raw_contract_and_purchase_json(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        contract_payload = {
            "reg_num": "C1",
            "customer": "0200000001",
            "suppliers": ["9000000001"],
            "docs": [
                {
                    "source": {
                        "contractInfo": {
                            "suppliers": {
                                "supplierInfo": [
                                    {
                                        "legalEntityRF": {
                                            "INN": "9000000001",
                                            "fullName": "ООО Контрактный Поставщик",
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            ],
        }
        purchase_payload = {
            "purchase_number": "P1",
            "docs": [
                {
                    "source": {
                        "notificationInfo": {
                            "customer": {
                                "INN": "0200000001",
                                "fullName": "ГКУ Покупатель",
                            }
                        }
                    }
                }
            ],
        }
        c_raw = conn.execute(
            "INSERT INTO raw_documents(source, endpoint, external_id, payload_json) VALUES ('t','/fz44/contracts','C1',?)",
            (json.dumps(contract_payload, ensure_ascii=False),),
        )
        p_raw = conn.execute(
            "INSERT INTO raw_documents(source, endpoint, external_id, payload_json) VALUES ('t','/fz44/purchases','P1',?)",
            (json.dumps(purchase_payload, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO purchases(raw_document_id, purchase_number, region_code) VALUES (?, 'P1', 2)",
            (int(p_raw.lastrowid),),
        )
        contract = conn.execute(
            """
            INSERT INTO contracts(raw_document_id, reg_num, purchase_number, customer_inn, price, region_code, published_at)
            VALUES (?, 'C1', 'P1', '0200000001', 1000000, 2, '2025-03-01')
            """,
            (int(c_raw.lastrowid),),
        )
        conn.execute(
            "INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, '9000000001')",
            (int(contract.lastrowid),),
        )
        conn.commit()

        result = backfill_contract_parties(conn, region_code=2, year=2025)
        customer = conn.execute(
            "SELECT name FROM organizations WHERE inn='0200000001'"
        ).fetchone()[0]
        supplier = conn.execute(
            "SELECT name FROM organizations WHERE inn='9000000001'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert result["contracts_scanned"] == 1
    assert result["organizations_updated"] == 2
    assert result["network_requests"] == 0
    assert customer == "ГКУ Покупатель"
    assert supplier == "ООО Контрактный Поставщик"


def _insert_demand_fixture(conn, *, contract_payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO organizations(inn, name, region_code) VALUES ('1234567890', 'ООО Завод', 2)"
    )
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G1', 'ООО Завод', '1234567890', 'Котел', '25.30.12.110', 'active', 1, '{}')
        """
    )
    payload = contract_payload or {
        "reg_num": "C1",
        "purchase_number": "P1",
        "customer": "0200000001",
        "suppliers": ["9000000001"],
    }
    raw = conn.execute(
        "INSERT INTO raw_documents(source, endpoint, external_id, payload_json) VALUES ('gosplan-v2','/fz44/contracts','C1',?)",
        (json.dumps(payload, ensure_ascii=False),),
    )
    contract = conn.execute(
        """
        INSERT INTO contracts(
            raw_document_id, reg_num, purchase_number, customer_inn, price,
            region_code, published_at
        ) VALUES (?, 'C1', 'P1', '0200000001', 1000000, 2, '2025-05-01')
        """,
        (int(raw.lastrowid),),
    )
    contract_id = int(contract.lastrowid)
    conn.execute(
        "INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, '9000000001')",
        (contract_id,),
    )
    conn.execute(
        "INSERT INTO contract_codes(contract_id, system, code) VALUES (?, 'okpd2', '25.30.12.999')",
        (contract_id,),
    )
    conn.commit()


def test_offline_backfill_reports_when_aggregate_raw_has_no_party_names(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        _insert_demand_fixture(conn)
        result = backfill_contract_parties(conn, region_code=2, year=2025)
    finally:
        conn.close()

    assert result["name_candidates_extracted"] == 0
    assert result["offline_resolution_status"] == "stored_raw_has_no_party_names"
    assert result["live_resolution_required"] is True


def test_live_regional_demand_party_resolver_uses_contract_detail_and_caches_it(tmp_path: Path) -> None:
    from procure_radar.party_intelligence import resolve_regional_demand_party_names_live

    class Client:
        def __init__(self) -> None:
            self.contract_calls: list[str] = []

        def get_contract(self, reg_num: str):
            self.contract_calls.append(reg_num)
            return {
                "docs": [
                    {
                        "source": {
                            "contractInfo": {
                                "customer": {
                                    "INN": "0200000001",
                                    "fullName": "ГКУ Главный Заказчик",
                                },
                                "suppliers": {
                                    "supplierInfo": [
                                        {
                                            "legalEntityRF": {
                                                "INN": "9000000001",
                                                "fullName": "ООО Главный Поставщик",
                                            }
                                        }
                                    ]
                                },
                            }
                        }
                    }
                ]
            }

        def get_purchase(self, purchase_number: str):
            raise AssertionError(f"purchase fallback should not be needed: {purchase_number}")

        def get_purchase_result(self, purchase_number: str):
            raise AssertionError(f"result fallback should not be needed: {purchase_number}")

    conn = connect(tmp_path / "db.sqlite3")
    try:
        _insert_demand_fixture(conn)
        client = Client()
        result = resolve_regional_demand_party_names_live(
            conn,
            client=client,
            manufacturer_inn="1234567890",
            region_code=2,
            year=2025,
            max_requests=10,
            rate_per_minute=100000,
        )
        customer = conn.execute(
            "SELECT name FROM organizations WHERE inn='0200000001'"
        ).fetchone()[0]
        supplier = conn.execute(
            "SELECT name FROM organizations WHERE inn='9000000001'"
        ).fetchone()[0]
        cached = conn.execute(
            "SELECT COUNT(*) FROM raw_documents WHERE endpoint='/fz44/contracts/C1' AND external_id='C1'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert client.contract_calls == ["C1"]
    assert result["matched_contracts"] == 1
    assert result["network_requests"] == 1
    assert result["contract_detail_requests"] == 1
    assert result["purchase_detail_requests"] == 0
    assert result["result_detail_requests"] == 0
    assert result["organizations_updated"] == 2
    assert result["party_names_missing_after"] == 0
    assert result["stop_reason"] == "complete"
    assert customer == "ГКУ Главный Заказчик"
    assert supplier == "ООО Главный Поставщик"
    assert cached == 1


def test_live_regional_demand_party_resolver_falls_back_to_purchase_for_customer(tmp_path: Path) -> None:
    from procure_radar.party_intelligence import resolve_regional_demand_party_names_live

    class Client:
        def get_contract(self, reg_num: str):
            return {
                "docs": [
                    {
                        "source": {
                            "contractInfo": {
                                "suppliers": {
                                    "supplierInfo": [
                                        {
                                            "legalEntityRF": {
                                                "INN": "9000000001",
                                                "fullName": "ООО Поставщик",
                                            }
                                        }
                                    ]
                                }
                            }
                        }
                    }
                ]
            }

        def get_purchase(self, purchase_number: str):
            return {
                "docs": [
                    {
                        "source": {
                            "notificationInfo": {
                                "customer": {
                                    "INN": "0200000001",
                                    "fullName": "ГБУ Заказчик",
                                }
                            }
                        }
                    }
                ]
            }

        def get_purchase_result(self, purchase_number: str):
            raise AssertionError("supplier was already resolved from contract detail")

    conn = connect(tmp_path / "db.sqlite3")
    try:
        _insert_demand_fixture(conn)
        result = resolve_regional_demand_party_names_live(
            conn,
            client=Client(),
            manufacturer_inn="1234567890",
            region_code=2,
            year=2025,
            max_requests=10,
            rate_per_minute=100000,
        )
        names = {
            row["inn"]: row["name"]
            for row in conn.execute(
                "SELECT inn, name FROM organizations WHERE inn IN ('0200000001','9000000001')"
            )
        }
    finally:
        conn.close()

    assert result["network_requests"] == 2
    assert result["contract_detail_requests"] == 1
    assert result["purchase_detail_requests"] == 1
    assert result["party_names_missing_after"] == 0
    assert names == {
        "0200000001": "ГБУ Заказчик",
        "9000000001": "ООО Поставщик",
    }
