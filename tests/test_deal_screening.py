from __future__ import annotations

from datetime import datetime, timezone

from procure_radar.db import connect
from procure_radar.deal_screening import locality_deal_shortlist, score_purchase


NOW = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)


def test_standard_goods_open_quote_is_high_priority() -> None:
    result = score_purchase(
        max_price=1_200_000,
        collecting_finished_at="2026-10-15T08:00:00+00:00",
        purchase_type="epNotificationEZK2020",
        stage=1,
        doc_types=["epNotificationEZK2020"],
        locality_confidence=1.0,
        item_count=4,
        coded_items=4,
        quantity_items=4,
        price_items=4,
        amount_items=4,
        segment="standard_goods",
        segment_group="goods",
        as_of=NOW,
    )
    assert result.eligible is True
    assert result.score >= 85
    assert result.tier == "A"
    assert result.decision == "pursue"
    assert result.execution_mode == "micro_distribution"


def test_passed_deadline_is_hard_reject_even_if_otherwise_attractive() -> None:
    result = score_purchase(
        max_price=900_000,
        collecting_finished_at="2026-10-08T08:00:00+00:00",
        purchase_type="epNotificationEF2020",
        stage=1,
        doc_types=["epNotificationEF2020"],
        locality_confidence=1.0,
        item_count=2,
        coded_items=2,
        quantity_items=2,
        price_items=2,
        amount_items=2,
        segment="standard_goods",
        segment_group="goods",
        as_of=NOW,
    )
    assert result.eligible is False
    assert "deadline_passed" in result.hard_reject_reasons
    assert result.decision == "skip"
    assert result.score <= 24


def test_regulated_market_is_flagged_and_scores_below_easy_goods() -> None:
    result = score_purchase(
        max_price=1_000_000,
        collecting_finished_at="2026-10-16T08:00:00+00:00",
        purchase_type="epNotificationEF2020",
        stage=1,
        doc_types=["epNotificationEF2020"],
        locality_confidence=1.0,
        item_count=5,
        coded_items=5,
        quantity_items=5,
        price_items=5,
        amount_items=5,
        segment="pharma",
        segment_group="goods",
        as_of=NOW,
    )
    assert "regulated_market" in result.review_flags
    assert result.execution_mode == "specialist_distribution_only"
    assert result.score < 80


def test_locality_shortlist_uses_evidence_and_item_detail(tmp_path) -> None:
    db = tmp_path / "radar.sqlite3"
    conn = connect(db)
    try:
        conn.execute("INSERT INTO organizations(inn, name, address, region_code) VALUES ('0268000001','Buyer','Республика Башкортостан, г. Стерлитамак',2)")
        conn.execute("INSERT INTO organization_localities(inn, locality_key, locality_name, source, confidence, evidence) VALUES ('0268000001','sterlitamak','Стерлитамак','organization_address',1.0,'address')")
        conn.execute("INSERT INTO raw_documents(source,endpoint,external_id,payload_json) VALUES ('gosplan-v2','/fz44/purchases','p1','{}')")
        raw_id = conn.execute("SELECT id FROM raw_documents WHERE external_id='p1'").fetchone()[0]
        conn.execute(
            """INSERT INTO purchases(raw_document_id,purchase_number,published_at,collecting_finished_at,max_price,currency_code,object_info,purchase_type,region_code,stage)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (raw_id, 'P-1', '2026-10-09T07:00:00+00:00', '2026-10-15T08:00:00+00:00', 750000, 'RUB', 'Поставка офисной мебели', 'epNotificationEZK2020', 2, 1),
        )
        pid = conn.execute("SELECT id FROM purchases WHERE purchase_number='P-1'").fetchone()[0]
        conn.execute("INSERT INTO purchase_parties(purchase_id,role,inn) VALUES (?, 'customer','0268000001')", (pid,))
        conn.execute("INSERT INTO purchase_documents(purchase_id,doc_type,published_at) VALUES (?, 'epNotificationEZK2020','2026-10-09')", (pid,))
        conn.execute(
            """INSERT INTO purchase_items(purchase_id,item_key,name,okpd2_code,okpd2_name,quantity,unit_price,amount)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, '1', 'Стол офисный', '31.01.12.110', 'Столы письменные деревянные', 10, 30000, 300000),
        )
        conn.commit()
        rows = locality_deal_shortlist(conn, locality_key='sterlitamak', min_score=50, as_of=NOW)
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row['purchase_number'] == 'P-1'
    assert row['segment'] == 'standard_goods'
    assert row['eligible'] is True
    assert row['deal_score'] >= 80
    assert row['decision'] == 'pursue'


def test_large_ticket_is_capped_out_of_a_tier() -> None:
    result = score_purchase(
        max_price=25_000_000,
        collecting_finished_at="2026-10-19T08:00:00+00:00",
        purchase_type="epNotificationEF2020",
        stage=1,
        doc_types=["epNotificationEF2020"],
        locality_confidence=1.0,
        item_count=10,
        coded_items=10,
        quantity_items=10,
        price_items=10,
        amount_items=10,
        segment="standard_goods",
        segment_group="goods",
        as_of=NOW,
    )
    assert "high_capital_requirement" in result.review_flags
    assert result.score <= 68
    assert result.tier != "A"


def test_one_day_deadline_is_not_a_first_pick() -> None:
    result = score_purchase(
        max_price=900_000,
        collecting_finished_at="2026-10-10T08:00:00+00:00",
        purchase_type="epNotificationEF2020",
        stage=1,
        doc_types=["epNotificationEF2020"],
        locality_confidence=1.0,
        item_count=4,
        coded_items=4,
        quantity_items=4,
        price_items=4,
        amount_items=4,
        segment="standard_goods",
        segment_group="goods",
        as_of=NOW,
    )
    assert "deadline_risk" in result.review_flags
    assert result.score <= 65
    assert result.decision != "pursue"


def test_real_estate_is_manual_review_not_auto_pursue() -> None:
    result = score_purchase(
        max_price=3_500_000,
        collecting_finished_at="2026-10-19T08:00:00+00:00",
        purchase_type="epNotificationEZK2020",
        stage=1,
        doc_types=["epNotificationEZK2020"],
        locality_confidence=1.0,
        item_count=1,
        coded_items=1,
        quantity_items=1,
        price_items=1,
        amount_items=1,
        segment="real_estate",
        segment_group="goods",
        as_of=NOW,
    )
    assert "capital_asset_market" in result.review_flags
    assert result.execution_mode == "manual_review"
    assert result.score <= 64
    assert result.tier == "C"
    assert result.decision == "review"
