from __future__ import annotations

import pytest

from procure_radar.db import connect
from procure_radar.ui_data import dashboard_metrics, organization_rows, source_statuses


def _seed_purchase(conn, *, raw_id: int, number: str, region: int, published_at: str, price: float) -> None:
    conn.execute(
        "INSERT INTO raw_documents(id, source, endpoint, external_id, payload_json) VALUES(?, 'test', '/fz44/purchases', ?, '{}')",
        (raw_id, number),
    )
    conn.execute(
        """
        INSERT INTO purchases(raw_document_id, purchase_number, published_at, max_price, region_code, stage)
        VALUES(?, ?, ?, ?, ?, 2)
        """,
        (raw_id, number, published_at, price, region),
    )


def _seed_contract(
    conn,
    *,
    raw_id: int,
    reg: str,
    purchase: str,
    buyer: str,
    supplier: str,
    region: int,
    published_at: str,
    price: float,
) -> None:
    conn.execute(
        "INSERT INTO raw_documents(id, source, endpoint, external_id, payload_json) VALUES(?, 'test', '/fz44/contracts', ?, '{}')",
        (raw_id, reg),
    )
    cur = conn.execute(
        """
        INSERT INTO contracts(raw_document_id, reg_num, purchase_number, customer_inn, price, region_code, published_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (raw_id, reg, purchase, buyer, price, region, published_at),
    )
    conn.execute("INSERT INTO contract_suppliers(contract_id, inn) VALUES(?, ?)", (cur.lastrowid, supplier))


def test_dashboard_and_source_statuses_show_date_ranges_and_linkage(tmp_path):
    conn = connect(tmp_path / "ui.sqlite3")
    try:
        _seed_purchase(conn, raw_id=1, number="P1", region=2, published_at="2026-08-01T10:00:00", price=1000)
        _seed_purchase(conn, raw_id=2, number="P2", region=2, published_at="2026-08-20T10:00:00", price=2000)
        _seed_contract(
            conn,
            raw_id=3,
            reg="C1",
            purchase="P1",
            buyer="B1",
            supplier="S1",
            region=2,
            published_at="2026-08-21T10:00:00",
            price=900,
        )
        conn.execute(
            """
            INSERT INTO tender_protocols(raw_document_id, protocol_key, purchase_number, applications_count, admitted_count, rejected_count)
            VALUES(?, 'PROTO1', 'P1', 1, 1, 0)
            """,
            (4,),
        ) if False else None
        conn.commit()

        metrics = dashboard_metrics(conn, region_code=2)
        assert metrics["purchases"] == 2
        assert metrics["contracts"] == 1
        assert metrics["suppliers"] == 1
        assert metrics["buyers"] == 1
        assert metrics["contract_linkage"] == 1.0
        assert metrics["purchase_date_to"] == "2026-08-20"
        assert metrics["contract_date_to"] == "2026-08-21"

        sources = {row["key"]: row for row in source_statuses(conn, region_code=2)}
        assert sources["purchases"]["count"] == 2
        assert sources["purchases"]["date_from"] == "2026-08-01"
        assert sources["purchases"]["date_to"] == "2026-08-20"
        assert sources["purchases"]["state"] == "connected"
        assert sources["contracts"]["count"] == 1
        assert sources["contracts"]["date_to"] == "2026-08-21"
        assert "поставщики 1" in sources["contracts"]["details"]
    finally:
        conn.close()


def test_organization_rows_merge_buyer_and_supplier_roles(tmp_path):
    conn = connect(tmp_path / "ui.sqlite3")
    try:
        _seed_purchase(conn, raw_id=1, number="P1", region=2, published_at="2026-08-01", price=1000)
        _seed_contract(
            conn,
            raw_id=2,
            reg="C1",
            purchase="P1",
            buyer="ORG",
            supplier="SUP",
            region=2,
            published_at="2026-08-02",
            price=900,
        )
        _seed_contract(
            conn,
            raw_id=3,
            reg="C2",
            purchase="P2",
            buyer="BUY",
            supplier="ORG",
            region=2,
            published_at="2026-08-03",
            price=500,
        )
        conn.commit()

        rows = {row["inn"]: row for row in organization_rows(conn, region_code=2)}
        assert rows["ORG"]["role"] == "заказчик + поставщик"
        assert rows["ORG"]["buyer_contracts"] == 1
        assert rows["ORG"]["supplier_contracts"] == 1
        assert rows["ORG"]["value"] == 1400.0
        assert rows["SUP"]["supplier_value"] == 900.0
    finally:
        conn.close()


def test_readonly_connection_skips_schema_bootstrap_and_rejects_writes(tmp_path):
    import sqlite3

    from procure_radar.db import connect_readonly

    path = tmp_path / "ui database.sqlite3"
    conn = connect(path)
    conn.execute(
        "INSERT INTO raw_documents(id, source, endpoint, external_id, payload_json) VALUES(1, 'test', '/x', '1', '{}')"
    )
    conn.commit()
    conn.close()

    ro = connect_readonly(path)
    try:
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        assert ro.execute("SELECT COUNT(*) FROM raw_documents").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM raw_documents")
    finally:
        ro.close()


def test_organization_card_uses_targeted_partner_aggregation(tmp_path):
    from procure_radar.ui_data import organization_card

    conn = connect(tmp_path / "ui.sqlite3")
    try:
        _seed_contract(
            conn,
            raw_id=1,
            reg="C1",
            purchase="P1",
            buyer="BUY",
            supplier="SUP",
            region=2,
            published_at="2026-08-01",
            price=900,
        )
        _seed_contract(
            conn,
            raw_id=2,
            reg="C2",
            purchase="P2",
            buyer="BUY",
            supplier="OTHER",
            region=2,
            published_at="2026-08-02",
            price=100,
        )
        conn.commit()

        card = organization_card(conn, region_code=2, inn="BUY")
        assert card["role"] == "заказчик"
        assert card["contracts"] == 2
        partners = {row["inn"]: row for row in card["partners"]}
        assert partners["SUP"]["value"] == 900.0
        assert partners["SUP"]["share"] == 0.9
        assert partners["OTHER"]["share"] == 0.1
    finally:
        conn.close()


def test_source_status_marks_incomplete_history_as_needing_resume(tmp_path):
    conn = connect(tmp_path / "ui.sqlite3")
    try:
        _seed_purchase(conn, raw_id=1, number="P1", region=2, published_at="2026-08-20", price=1000)
        conn.execute(
            """
            INSERT INTO history_backfills(
                checkpoint_key, region_code, since_date, until_date, query_json, page_limit,
                next_skip, rows_seen, completed
            ) VALUES('h1', 2, '2025-06-01', '2026-08-22', '{}', 100, 0, 1445, 0)
            """
        )
        conn.commit()
        sources = {row["key"]: row for row in source_statuses(conn, region_code=2)}
        assert sources["purchases"]["state"] == "needs_resume"
        assert 0 < sources["purchases"]["progress"] < 1

        conn.execute("UPDATE history_backfills SET completed=1 WHERE checkpoint_key='h1'")
        conn.commit()
        sources = {row["key"]: row for row in source_statuses(conn, region_code=2)}
        assert sources["purchases"]["state"] == "up_to_date"
        assert sources["purchases"]["progress"] == 1.0
    finally:
        conn.close()
