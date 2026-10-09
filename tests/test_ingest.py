import sqlite3

from procure_radar.db import SCHEMA
from procure_radar.ingest import ingest_purchase


def test_ingest_purchase_children():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    payload = {
        "purchase_number": "0103200008426007106",
        "published_at": "2026-08-21T14:58:28.181000",
        "max_price": 1_500_000.0,
        "currency_code": "RUB",
        "object_info": "Поставка изделий медицинского назначения",
        "region": 5,
        "stage": 1,
        "customers": ["0542009250"],
        "owners": ["0542009250"],
        "okpd2": ["13.95.10.112", "32.50.50.190"],
        "ktru": ["17.22.12.120-00000006"],
        "ikzs": ["IKZ1"],
        "plan_numbers": ["PLAN1"],
        "position_numbers": ["POS1"],
        "docs": [{"doc_type": "epNotificationEF2020", "published_at": "2026-08-21T14:58:28.181000"}],
    }
    purchase_id = ingest_purchase(conn, payload)
    conn.commit()

    row = conn.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
    assert row["purchase_number"] == "0103200008426007106"
    assert row["region_code"] == 5
    assert conn.execute("SELECT COUNT(*) FROM purchase_codes WHERE system='okpd2'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM purchase_parties WHERE role='customer'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM purchase_documents").fetchone()[0] == 1


def test_detail_ingest_does_not_erase_aggregate_fields_or_children():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    aggregate = {
        "purchase_number": "P-MERGE",
        "region": 2,
        "stage": 2,
        "max_price": 500000.0,
        "object_info": "Поставка бумаги",
        "customers": ["1234567890"],
        "okpd2": ["17.12.14.100"],
        "ktru": ["17.12.14.100-00000001"],
        "docs": [{"doc_type": "epNotificationEF2020", "published_at": "2026-01-01"}],
    }
    detail = {
        "purchase_number": "P-MERGE",
        "docs": [
            {
                "doc_type": "epProtocolEF2020Final",
                "published_at": "2026-01-10",
                "source": {"id": "proto", "commonInfo": {"purchaseNumber": "P-MERGE"}},
            }
        ],
    }
    ingest_purchase(conn, aggregate)
    ingest_purchase(conn, detail)
    row = conn.execute("SELECT * FROM purchases WHERE purchase_number='P-MERGE'").fetchone()
    assert row["object_info"] == "Поставка бумаги"
    assert row["max_price"] == 500000.0
    assert row["region_code"] == 2
    assert conn.execute("SELECT COUNT(*) FROM purchase_parties WHERE role='customer'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM purchase_codes WHERE system='okpd2'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM purchase_codes WHERE system='ktru'").fetchone()[0] == 1


def test_ingest_tenderplan_positions_are_queryable():
    from procure_radar.ingest import ingest_tenderplan

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    payload = {
        "plan_number": "PLAN-2026",
        "region": 2,
        "published_at": "2026-08-20",
        "customers": ["0278176470"],
        "positions": [
            {
                "positionNumber": "POS-1",
                "purchaseObjectName": "Блочная котельная",
                "plannedPublishDate": "2026-10-01",
                "totalAmount": 20_000_000,
                "OKPD2": {"OKPDCode": "25.30.12.110"},
            }
        ],
    }
    plan_id = ingest_tenderplan(conn, payload)
    conn.commit()
    assert conn.execute("SELECT plan_number FROM tenderplans WHERE id=?", (plan_id,)).fetchone()[0] == "PLAN-2026"
    pos = conn.execute("SELECT * FROM tenderplan_positions").fetchone()
    assert pos["position_number"] == "POS-1"
    assert pos["customer_inn"] == "0278176470"
    assert pos["amount"] == 20_000_000
    assert conn.execute("SELECT code FROM tenderplan_position_codes").fetchone()[0] == "25.30.12.110"
    assert conn.execute("SELECT name FROM organizations WHERE inn='0278176470'").fetchone()[0] is None


def test_tenderplan_aggregate_refresh_preserves_existing_detail_positions():
    from procure_radar.ingest import ingest_tenderplan

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    detail = {
        "plan_number": "PLAN-PRESERVE",
        "region": 2,
        "positions": [
            {"positionNumber": "FULL-1", "OKPD2": {"OKPDCode": "25.30.12.110"}},
            {"positionNumber": "FULL-2", "OKPD2": {"OKPDCode": "28.21.13.000"}},
        ],
    }
    aggregate = {
        "plan_number": "PLAN-PRESERVE",
        "region": 2,
        "positions": [
            {"positionNumber": "AGG-ONLY", "OKPD2": {"OKPDCode": "01.11.11.000"}},
        ],
    }
    ingest_tenderplan(conn, detail, raw_endpoint="/fz44/tenderplans/PLAN-PRESERVE")
    ingest_tenderplan(conn, aggregate, preserve_existing_positions=True)
    numbers = {
        row[0] for row in conn.execute("SELECT position_number FROM tenderplan_positions")
    }
    assert numbers == {"FULL-1", "FULL-2"}
