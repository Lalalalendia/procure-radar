from __future__ import annotations

from pathlib import Path

from procure_radar.db import connect
from procure_radar.deal_dossier import export_deal_dossiers, locality_deal_dossiers


def test_dossier_contains_items_and_next_work_units(tmp_path: Path) -> None:
    db = tmp_path / "radar.sqlite3"
    conn = connect(db)
    try:
        conn.execute("INSERT INTO organizations(inn,name,address,region_code) VALUES ('0268000001','Buyer','г. Стерлитамак',2)")
        conn.execute("INSERT INTO organization_localities(inn,locality_key,locality_name,source,confidence,evidence) VALUES ('0268000001','sterlitamak','Стерлитамак','organization_address',1.0,'address')")
        conn.execute("INSERT INTO raw_documents(source,endpoint,external_id,payload_json) VALUES ('gosplan-v2','/fz44/purchases','d1','{}')")
        raw = conn.execute("SELECT id FROM raw_documents WHERE external_id='d1'").fetchone()[0]
        conn.execute("""INSERT INTO purchases(raw_document_id,purchase_number,published_at,collecting_finished_at,max_price,currency_code,object_info,purchase_type,region_code,stage)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (raw,'D-1','2099-01-01','2099-01-10',900000,'RUB','Поставка мебели','epNotificationEZK2020',2,1))
        pid = conn.execute("SELECT id FROM purchases WHERE purchase_number='D-1'").fetchone()[0]
        conn.execute("INSERT INTO purchase_parties(purchase_id,role,inn) VALUES (?,'customer','0268000001')", (pid,))
        conn.execute("INSERT INTO purchase_documents(purchase_id,doc_type,published_at) VALUES (?,'epNotificationEZK2020','2099-01-01')", (pid,))
        conn.execute("""INSERT INTO purchase_items(purchase_id,item_key,name,okpd2_code,quantity,unit_name,unit_price,amount)
                        VALUES (?,?,?,?,?,?,?,?)""", (pid,'1','Стол','31.01.12.110',10,'шт',30000,300000))
        conn.commit()
        rows = locality_deal_dossiers(conn, locality_key='sterlitamak', min_score=55, max_deals=5)
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]['items'][0]['name'] == 'Стол'
    assert rows[0]['next_work_units']
    md = tmp_path / 'dossiers.md'
    export_deal_dossiers(rows, markdown_path=md)
    text = md.read_text(encoding='utf-8')
    assert 'Line items' in text
    assert 'Find at least 3 independent suppliers' in text
