import sqlite3

from procure_radar.db import SCHEMA
from procure_radar.extract import extract_contract, extract_procedure
from procure_radar.ingest import ingest_contract, ingest_procedure


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def test_extract_and_ingest_procedure():
    payload = {
        "contract_project_number": "03351000078260000080001",
        "currency_code": "RUB",
        "customer": "3916005556",
        "participant": "3900019232",
        "price": 870124.56,
        "published_at": "2026-08-20T21:52:31",
        "purchase_number": "0335100007826000008",
        "region": 39,
        "subject": "поставка прицепа тракторного",
        "docs": [{"doc_type": "cpContractSign", "published_at": "2026-08-20T21:52:31"}],
    }
    parsed = extract_procedure(payload)
    assert parsed["participant_inn"] == "3900019232"
    assert parsed["price"] == 870124.56

    conn = _conn()
    procedure_id = ingest_procedure(conn, payload)
    row = conn.execute("SELECT * FROM contract_procedures WHERE id=?", (procedure_id,)).fetchone()
    assert row["purchase_number"] == "0335100007826000008"
    assert row["participant_inn"] == "3900019232"
    assert conn.execute("SELECT COUNT(*) FROM procedure_documents").fetchone()[0] == 1


def test_extract_and_ingest_contract():
    payload = {
        "currency_code": "RUB",
        "customer": "3123448020",
        "exe_start": "2026-08-21",
        "exe_end": "2027-12-31",
        "ktru": ["21.20.24.133-00000017"],
        "okpd2": [],
        "plan_number": "202603265000004001",
        "position_number": "202603265000004001000674",
        "price": 63696.8,
        "published_at": "2026-08-21T13:59:41.553000",
        "purchase_number": "0826500000926002653",
        "reg_num": "2312344802026000659",
        "region": 31,
        "stage": "E",
        "subject": "Поставка медицинских изделий",
        "suppliers": ["7604032720"],
        "docs": [{"doc_type": "contract", "published_at": "2026-08-21T13:59:41.553000"}],
    }
    parsed = extract_contract(payload)
    assert parsed["reg_num"] == "2312344802026000659"
    assert parsed["suppliers"] == ["7604032720"]
    assert parsed["stage"] == "E"

    conn = _conn()
    contract_id = ingest_contract(conn, payload)
    row = conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
    assert row["purchase_number"] == "0826500000926002653"
    assert row["price"] == 63696.8
    assert conn.execute("SELECT COUNT(*) FROM contract_suppliers").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM contract_codes WHERE system='ktru'").fetchone()[0] == 1
