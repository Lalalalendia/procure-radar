from __future__ import annotations

from pathlib import Path

from procure_radar.db import connect
from procure_radar.forward_opportunities import forward_opportunities
from procure_radar.ingest import ingest_contract, ingest_purchase, ingest_tenderplan


def _history_complete(conn):
    conn.execute(
        """
        INSERT INTO history_backfills(
            checkpoint_key, region_code, since_date, until_date, query_json,
            page_limit, next_skip, completed, updated_at
        ) VALUES ('contract-history-2-2025', 2, '2025-01-01', '2025-12-31',
                  '{"__history_mode":"contracts_published_time_shards_v1"}',
                  100, 0, 1, '2025-12-31 23:59:59')
        """
    )


def _seed(tmp_path: Path):
    conn = connect(tmp_path / "db.sqlite3")
    conn.execute(
        "INSERT INTO organizations(inn,name,region_code) VALUES ('0264050163','ИНТЕХСЕРВИС',2)"
    )
    conn.execute(
        "INSERT INTO organizations(inn,name,region_code) VALUES ('0278176470','ГКУ УКС РБ',2)"
    )
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G-1','ИНТЕХСЕРВИС','0264050163','Блочная котельная',
                  '25.30.12.110','active',1,'{}')
        """
    )
    _history_complete(conn)
    ingest_contract(
        conn,
        {
            "reg_num": "C-1",
            "purchase_number": "H-1",
            "customer": "0278176470",
            "suppliers": ["0278968394"],
            "price": 30_000_000,
            "region": 2,
            "published_at": "2025-03-01",
            "okpd2": ["25.30.12.110"],
        },
    )
    ingest_contract(
        conn,
        {
            "reg_num": "C-2",
            "purchase_number": "H-2",
            "customer": "0278176470",
            "suppliers": ["1841093710"],
            "price": 20_000_000,
            "region": 2,
            "published_at": "2025-06-01",
            "okpd2": ["25.30.12.110"],
        },
    )
    ingest_purchase(
        conn,
        {
            "purchase_number": "OPEN-1",
            "region": 2,
            "published_at": "2026-08-20T10:00:00",
            "collecting_finished_at": "2026-09-05T10:00:00",
            "max_price": 31_000_000,
            "object_info": "Поставка блочной котельной",
            "customers": ["0278176470"],
            "okpd2": ["25.30.12.110"],
            "stage": 1,
        },
    )
    ingest_tenderplan(
        conn,
        {
            "plan_number": "PLAN-1",
            "region": 2,
            "published_at": "2026-08-21",
            "customers": ["0278176470"],
            "positions": [
                {
                    "positionNumber": "POS-1",
                    "purchaseObjectName": "Блочно-модульная котельная",
                    "plannedPublishDate": "2026-11-01",
                    "totalAmount": 18_000_000,
                    "OKPD2": {"OKPDCode": "25.30.12.120"},
                }
            ],
        },
    )
    conn.commit()
    return conn


def test_forward_opportunities_combines_open_notices_and_unpublished_plan_positions(tmp_path: Path):
    conn = _seed(tmp_path)
    try:
        result = forward_opportunities(
            conn,
            manufacturer_inn="0264050163",
            region_code=2,
            as_of="2026-08-23",
            history_year=2025,
        )
    finally:
        conn.close()

    assert result["manufacturer_okpd2_families"] == ["25.30.12"]
    assert result["current_opportunities_total"] == 1
    assert result["planned_opportunities_total"] == 1
    current = result["current_opportunities"][0]
    assert current["purchase_number"] == "OPEN-1"
    assert current["buyer_name"] == "ГКУ УКС РБ"
    assert current["estimated_addressable_value_rub"] == 31_000_000
    assert current["forward_opportunity_tier"] in {"medium", "high"}
    planned = result["planned_opportunities"][0]
    assert planned["position_number"] == "POS-1"
    assert planned["estimated_addressable_value_rub"] == 18_000_000
    assert planned["status"] == "planned"


def test_plan_position_disappears_after_notice_links_same_position(tmp_path: Path):
    conn = _seed(tmp_path)
    try:
        ingest_purchase(
            conn,
            {
                "purchase_number": "OPEN-2",
                "region": 2,
                "published_at": "2026-08-22",
                "collecting_finished_at": "2026-09-10",
                "max_price": 18_000_000,
                "customers": ["0278176470"],
                "position_numbers": ["POS-1"],
                "okpd2": ["25.30.12.120"],
            },
        )
        conn.commit()
        result = forward_opportunities(
            conn,
            manufacturer_inn="0264050163",
            region_code=2,
            as_of="2026-08-23",
            history_year=2025,
        )
    finally:
        conn.close()
    assert result["planned_opportunities_total"] == 0
    assert {row["purchase_number"] for row in result["current_opportunities"]} == {"OPEN-1", "OPEN-2"}


class _FakeForwardClient:
    def __init__(self):
        self.detail_calls = 0

    def get_purchases(self, *, limit, skip, extra):
        return []

    def get_tenderplans(self, *, limit, skip, extra):
        if skip:
            return []
        return [{"plan_number": "PLAN-CACHE", "region": 2, "published_at": "2026-08-21"}]

    def get_tenderplan(self, plan_number):
        self.detail_calls += 1
        return {
            "plan_number": plan_number,
            "region": 2,
            "published_at": "2026-08-21",
            "positions": [
                {
                    "positionNumber": "POS-CACHE",
                    "plannedPublishDate": "2026-10-01",
                    "totalAmount": 10_000_000,
                    "OKPD2": [{"OKPDCode": "25.30.12.110"}],
                }
            ],
        }


def test_forward_sync_reuses_tenderplan_detail_cache(tmp_path: Path):
    from procure_radar.forward_opportunities import sync_forward_sources

    conn = connect(tmp_path / "cache.sqlite3")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0264050163','ИНТЕХСЕРВИС',2)")
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G-CACHE','ИНТЕХСЕРВИС','0264050163','Блочная котельная',
                  '25.30.12.110','active',1,'{}')
        """
    )
    _history_complete(conn)
    conn.commit()
    client = _FakeForwardClient()
    try:
        first = sync_forward_sources(
            conn,
            client,
            manufacturer_inn="0264050163",
            region_code=2,
            history_year=2025,
            purchase_pages=0,
            tenderplan_pages=1,
            page_size=50,
            max_requests=10,
            rate_per_minute=0,
        )
        second = sync_forward_sources(
            conn,
            client,
            manufacturer_inn="0264050163",
            region_code=2,
            history_year=2025,
            purchase_pages=0,
            tenderplan_pages=1,
            page_size=50,
            max_requests=10,
            rate_per_minute=0,
        )
    finally:
        conn.close()

    assert first["tenderplan_detail_requests"] == 1
    assert first["tenderplan_detail_cache_rows_after"] == 1
    assert second["tenderplan_detail_requests"] == 0
    assert second["tenderplan_detail_cached"] == 1
    assert client.detail_calls == 1
    assert second["match_diagnostics"]["manufacturer_exact_family_matches"] == 1


class _MovingHeadForwardClient:
    def __init__(self):
        self.list_calls = 0
        self.detail_calls: list[str] = []

    def get_purchases(self, *, limit, skip, extra):
        return []

    def get_tenderplans(self, *, limit, skip, extra):
        call = self.list_calls
        self.list_calls += 1
        if call == 0:
            return [
                {"plan_number": "PLAN-A", "region": 2, "published_at": "2026-08-23"},
                {"plan_number": "PLAN-B", "region": 2, "published_at": "2026-08-23"},
            ]
        if call == 1:
            return [
                {"plan_number": "PLAN-C", "region": 2, "published_at": "2026-08-22"},
                {"plan_number": "PLAN-D", "region": 2, "published_at": "2026-08-22"},
            ]
        # On the next invocation the old head has moved away. Pending B/C/D
        # must still be completed from the durable queue.
        return [{"plan_number": "PLAN-E", "region": 2, "published_at": "2026-08-24"}]

    def get_tenderplan(self, plan_number):
        self.detail_calls.append(plan_number)
        return {
            "plan_number": plan_number,
            "region": 2,
            "published_at": "2026-08-23",
            "positions": [
                {
                    "positionNumber": f"POS-{plan_number}",
                    "plannedPublishDate": "2026-10-01",
                    "totalAmount": 1_000_000,
                    "OKPD2": [{"OKPDCode": "21.20.10.110"}],
                }
            ],
        }


def test_forward_sync_durable_pending_queue_survives_moving_list_head(tmp_path: Path):
    from procure_radar.forward_opportunities import sync_forward_sources

    conn = connect(tmp_path / "queue.sqlite3")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0264050163','ИНТЕХСЕРВИС',2)")
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G-Q','ИНТЕХСЕРВИС','0264050163','Блочная котельная',
                  '25.30.12.110','active',1,'{}')
        """
    )
    _history_complete(conn)
    conn.commit()
    client = _MovingHeadForwardClient()
    try:
        first = sync_forward_sources(
            conn,
            client,
            manufacturer_inn="0264050163",
            region_code=2,
            history_year=2025,
            purchase_pages=0,
            tenderplan_pages=2,
            page_size=2,
            max_requests=3,
            rate_per_minute=0,
        )
        second = sync_forward_sources(
            conn,
            client,
            manufacturer_inn="0264050163",
            region_code=2,
            history_year=2025,
            purchase_pages=0,
            tenderplan_pages=2,
            page_size=2,
            max_requests=3,
            rate_per_minute=0,
        )
    finally:
        conn.close()

    assert first["tenderplan_list_requests"] == 2
    assert first["tenderplan_detail_requests"] == 1
    assert first["tenderplan_detail_pending_before"] == 4
    assert first["tenderplan_detail_pending_after"] == 3
    assert second["tenderplan_list_requests"] == 1
    assert second["tenderplan_detail_requests"] == 2
    assert second["tenderplan_detail_pending_before"] == 4  # B/C/D carried + new E
    assert second["tenderplan_detail_pending_after"] == 2
    assert client.detail_calls == ["PLAN-A", "PLAN-B", "PLAN-C"]


def test_forward_semantic_fallback_finds_unclassified_plan_position(tmp_path: Path):
    conn = connect(tmp_path / "semantic.sqlite3")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0264050163','ИНТЕХСЕРВИС',2)")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0278176470','ГКУ УКС РБ',2)")
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G-SEM','ИНТЕХСЕРВИС','0264050163','Блочная котельная',
                  '25.30.12.110','active',1,'{}')
        """
    )
    _history_complete(conn)
    for idx, subject in enumerate(
        [
            "Поставка блочной котельной установки для поликлиники",
            "Поставка блочно-модульной котельной установки для крытого катка",
            "Поставка блочного теплового пункта для геронтологического центра",
        ],
        1,
    ):
        ingest_contract(
            conn,
            {
                "reg_num": f"SEM-H-{idx}",
                "purchase_number": f"SEM-P-{idx}",
                "customer": "0278176470",
                "suppliers": [f"027800000{idx}"],
                "price": 10_000_000 + idx,
                "region": 2,
                "published_at": f"2025-0{idx + 1}-01",
                "subject": subject,
                "okpd2": ["25.30.12.110"],
            },
        )
    # Background noise makes generic procurement words cheap in the learned profile.
    ingest_contract(
        conn,
        {
            "reg_num": "SEM-NOISE",
            "purchase_number": "SEM-NOISE-P",
            "customer": "0278176470",
            "suppliers": ["0278999999"],
            "price": 1_000_000,
            "region": 2,
            "published_at": "2025-07-01",
            "subject": "Поставка лекарственных препаратов для медицинской организации",
            "okpd2": ["21.20.10.110"],
        },
    )
    ingest_tenderplan(
        conn,
        {
            "plan_number": "PLAN-SEM",
            "region": 2,
            "published_at": "2026-08-22",
            "customers": ["0278176470"],
            "positions": [
                {
                    "positionNumber": "POS-SEM-GOOD",
                    "purchaseObjectName": "Блочно-модульная котельная для школы",
                    "plannedPublishDate": "2026-10-01",
                    "totalAmount": 24_000_000,
                },
                {
                    "positionNumber": "POS-SEM-NOISE",
                    "purchaseObjectName": "Поставка лекарственных препаратов",
                    "plannedPublishDate": "2026-10-01",
                    "totalAmount": 15_000_000,
                },
            ],
        },
    )
    conn.commit()
    try:
        result = forward_opportunities(
            conn,
            manufacturer_inn="0264050163",
            region_code=2,
            as_of="2026-08-23",
            history_year=2025,
        )
    finally:
        conn.close()

    assert result["planned_opportunities_total"] == 1
    planned = result["planned_opportunities"][0]
    assert planned["position_number"] == "POS-SEM-GOOD"
    assert planned["match_type"] == "semantic_product"
    assert planned["matched_okpd2_families"] == []
    assert planned["inferred_okpd2_families"] == ["25.30.12"]
    assert planned["semantic_score"] >= 52
    assert planned["match_confidence"] > 0.4
    assert planned["semantic_evidence"]
    diagnostics = result["match_diagnostics"]
    assert diagnostics["semantic_profile"]["status"] == "good"
    assert diagnostics["semantic_matching"]["unclassified_tenderplan_positions_scanned"] == 2
    assert diagnostics["semantic_matching"]["semantic_matches"] == 1


def test_forward_semantic_fallback_does_not_override_explicit_nonmatching_okpd2(tmp_path: Path):
    conn = connect(tmp_path / "semantic-explicit.sqlite3")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0264050163','ИНТЕХСЕРВИС',2)")
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G-SEM-X','ИНТЕХСЕРВИС','0264050163','Блочная котельная',
                  '25.30.12.110','active',1,'{}')
        """
    )
    _history_complete(conn)
    ingest_contract(
        conn,
        {
            "reg_num": "SEM-X-H",
            "purchase_number": "SEM-X-P",
            "customer": "0278176470",
            "suppliers": ["0278968394"],
            "price": 30_000_000,
            "region": 2,
            "published_at": "2025-03-01",
            "subject": "Поставка блочной котельной установки",
            "okpd2": ["25.30.12.110"],
        },
    )
    ingest_tenderplan(
        conn,
        {
            "plan_number": "PLAN-SEM-X",
            "region": 2,
            "published_at": "2026-08-22",
            "positions": [
                {
                    "positionNumber": "POS-SEM-X",
                    "purchaseObjectName": "Блочная котельная",
                    "plannedPublishDate": "2026-10-01",
                    "totalAmount": 20_000_000,
                    "OKPD2": {"OKPDCode": "21.20.10.110"},
                }
            ],
        },
    )
    conn.commit()
    try:
        result = forward_opportunities(
            conn,
            manufacturer_inn="0264050163",
            region_code=2,
            as_of="2026-08-23",
            history_year=2025,
        )
    finally:
        conn.close()
    assert result["planned_opportunities_total"] == 0
    assert result["match_diagnostics"]["semantic_matching"]["unclassified_tenderplan_positions_scanned"] == 0


def test_forward_semantic_fallback_finds_unclassified_open_notice(tmp_path: Path):
    conn = connect(tmp_path / "semantic-open.sqlite3")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0264050163','ИНТЕХСЕРВИС',2)")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0278176470','ГКУ УКС РБ',2)")
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G-SEM-O','ИНТЕХСЕРВИС','0264050163','Блочная котельная',
                  '25.30.12.110','active',1,'{}')
        """
    )
    _history_complete(conn)
    for idx, subject in enumerate(
        [
            "Поставка блочной котельной установки",
            "Поставка блочно-модульной котельной установки",
        ],
        1,
    ):
        ingest_contract(
            conn,
            {
                "reg_num": f"SEM-O-H-{idx}",
                "purchase_number": f"SEM-O-P-{idx}",
                "customer": "0278176470",
                "suppliers": [f"027811111{idx}"],
                "price": 10_000_000,
                "region": 2,
                "published_at": f"2025-0{idx + 1}-01",
                "subject": subject,
                "okpd2": ["25.30.12.110"],
            },
        )
    ingest_purchase(
        conn,
        {
            "purchase_number": "OPEN-SEM",
            "region": 2,
            "published_at": "2026-08-22",
            "collecting_finished_at": "2026-09-10",
            "max_price": 27_000_000,
            "object_info": "Блочно-модульная котельная для нового корпуса",
            "customers": ["0278176470"],
            # Deliberately no OKPD2/KTRU.
        },
    )
    conn.commit()
    try:
        result = forward_opportunities(
            conn,
            manufacturer_inn="0264050163",
            region_code=2,
            as_of="2026-08-23",
            history_year=2025,
        )
    finally:
        conn.close()

    assert result["current_opportunities_total"] == 1
    current = result["current_opportunities"][0]
    assert current["purchase_number"] == "OPEN-SEM"
    assert current["match_type"] == "semantic_product"
    assert current["value_basis"] == "max_price_semantic_product_match"
    assert current["inferred_okpd2_families"] == ["25.30.12"]
    assert current["match_confidence"] < 1.0


def test_forward_semantic_clusters_keep_mixed_exact_family_products_separate(tmp_path: Path):
    conn = connect(tmp_path / "semantic-clusters.sqlite3")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0264050163','ИНТЕХСЕРВИС',2)")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0278176470','ГКУ УКС РБ',2)")
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G-SEM-MIX','ИНТЕХСЕРВИС','0264050163','Деаэратор центробежно-вихревой УДАВ (ДЦВ)',
                  '25.30.12.110','active',1,'{}')
        """
    )
    _history_complete(conn)
    for idx, subject in enumerate(
        [
            "Поставка блочной котельной установки для поликлиники",
            "Поставка блочно-модульной котельной установки для крытого катка",
            "Поставка блочной котельной установки для школы",
        ],
        1,
    ):
        ingest_contract(
            conn,
            {
                "reg_num": f"SEM-MIX-H-{idx}",
                "purchase_number": f"SEM-MIX-P-{idx}",
                "customer": "0278176470",
                "suppliers": [f"027822222{idx}"],
                "price": 12_000_000,
                "region": 2,
                "published_at": f"2025-0{idx + 1}-01",
                "subject": subject,
                "okpd2": ["25.30.12.110"],
            },
        )
    ingest_tenderplan(
        conn,
        {
            "plan_number": "PLAN-SEM-MIX",
            "region": 2,
            "published_at": "2026-08-22",
            "customers": ["0278176470"],
            "positions": [
                {
                    "positionNumber": "POS-SEM-MIX-GOOD",
                    "purchaseObjectName": "Блочно-модульная котельная для нового корпуса школы",
                    "plannedPublishDate": "2026-10-01",
                    "totalAmount": 28_000_000,
                },
                {
                    "positionNumber": "POS-SEM-MIX-NEAR",
                    "purchaseObjectName": "Ремонт котельной здания школы",
                    "plannedPublishDate": "2026-10-01",
                    "totalAmount": 3_000_000,
                },
            ],
        },
    )
    conn.commit()
    try:
        result = forward_opportunities(
            conn,
            manufacturer_inn="0264050163",
            region_code=2,
            as_of="2026-08-23",
            history_year=2025,
        )
    finally:
        conn.close()

    assert result["planned_opportunities_total"] == 1
    planned = result["planned_opportunities"][0]
    assert planned["position_number"] == "POS-SEM-MIX-GOOD"
    assert planned["match_type"] == "semantic_product"
    assert planned["semantic_cluster_id"] is not None
    profile = result["match_diagnostics"]["semantic_profile"]
    assert profile["match_engine"] == "clustered_semantic_prototypes_v4_product_anchor_intent"
    assert len(profile["semantic_clusters"]) >= 2
    diagnostics = result["match_diagnostics"]["semantic_matching"]
    assert diagnostics["semantic_candidates_scored"] >= 2
    assert diagnostics["top_semantic_candidates"]
    assert any(not row["passed_threshold"] for row in diagnostics["top_semantic_candidates"])


def test_forward_semantic_precision_rejects_project_boilerplate_and_service_only(tmp_path: Path):
    conn = connect(tmp_path / "semantic-precision.sqlite3")
    conn.execute("INSERT INTO organizations(inn,name,region_code) VALUES ('0264050163','ИНТЕХСЕРВИС',2)")
    conn.execute(
        """
        INSERT INTO gisp_products(
            registry_number, manufacturer_name, manufacturer_inn, product_name,
            okpd2_code, source_scope, is_active, raw_json
        ) VALUES ('G-SEM-PREC','ИНТЕХСЕРВИС','0264050163','Деаэратор центробежно-вихревой УДАВ (ДЦВ)',
                  '25.30.12.110','active',1,'{}')
        """
    )
    _history_complete(conn)
    for idx, subject in enumerate(
        [
            "Поставка блочной котельной установки для поликлиники",
            "Поставка блочно-модульной котельной установки для крытого катка",
            "Поставка оборудования блочного теплового пункта",
            "Поставка блочного теплового пункта для нового корпуса",
        ],
        1,
    ):
        ingest_contract(
            conn,
            {
                "reg_num": f"SEM-PREC-H-{idx}",
                "purchase_number": f"SEM-PREC-P-{idx}",
                "customer": "0278176470",
                "suppliers": [f"027833333{idx}"],
                "price": 12_000_000,
                "region": 2,
                "published_at": f"2025-0{idx + 1}-01",
                "subject": subject,
                "okpd2": ["25.30.12.110"],
            },
        )
    ingest_contract(
        conn,
        {
            "reg_num": "SEM-PREC-H-SERVICE",
            "purchase_number": "SEM-PREC-P-SERVICE",
            "customer": "0278176470",
            "suppliers": ["0278999999"],
            "price": 500_000,
            "region": 2,
            "published_at": "2025-06-01",
            "subject": "Оказание услуг по техническому диагностированию котельной установки",
            "okpd2": ["25.30.12.110"],
        },
    )
    ingest_contract(
        conn,
        {
            "reg_num": "SEM-PREC-H-UTILITY",
            "purchase_number": "SEM-PREC-P-UTILITY",
            "customer": "0278176470",
            "suppliers": ["0278999998"],
            "price": 600_000,
            "region": 2,
            "published_at": "2025-07-01",
            "subject": "Энергия тепловая, отпущенная котельными",
            "okpd2": ["25.30.12.110"],
        },
    )
    ingest_tenderplan(
        conn,
        {
            "plan_number": "PLAN-SEM-PREC",
            "region": 2,
            "published_at": "2026-08-22",
            "positions": [
                {
                    "positionNumber": "POS-DIRECT",
                    "purchaseObjectName": "Поставка оборудования для индивидуального теплового пункта",
                    "plannedPublishDate": "2026-10-01",
                },
                {
                    "positionNumber": "POS-EMBEDDED",
                    "purchaseObjectName": "Реконструкция системы теплоснабжения с установкой двух блочных котельных",
                    "plannedPublishDate": "2026-10-01",
                },
                {
                    "positionNumber": "POS-SERVICE",
                    "purchaseObjectName": "Оказание услуг по техническому обслуживанию оборудования блочного теплового пункта",
                    "plannedPublishDate": "2026-10-01",
                },
                {
                    "positionNumber": "POS-AUTO",
                    "purchaseObjectName": "Поставка автофургона (национальный проект Семья)",
                    "plannedPublishDate": "2026-10-01",
                },
                {
                    "positionNumber": "POS-WATER",
                    "purchaseObjectName": "Капитальный ремонт участка водопровода (национальный проект Инфраструктура для жизни)",
                    "plannedPublishDate": "2026-10-01",
                },
                {
                    "positionNumber": "POS-PUMP",
                    "purchaseObjectName": "Поставка с установкой насосного оборудования в помещении теплового узла ГБУЗ РБ КБСМП г.Уфы",
                    "plannedPublishDate": "2026-10-01",
                },
                {
                    "positionNumber": "POS-DIAG",
                    "purchaseObjectName": "Оказание услуг по техническому диагностированию водогрейных котлов и газорегуляторной установки котельной ГБУЗ РБ",
                    "plannedPublishDate": "2026-10-01",
                },
            ],
        },
    )
    conn.commit()
    try:
        result = forward_opportunities(
            conn,
            manufacturer_inn="0264050163",
            region_code=2,
            as_of="2026-08-23",
            history_year=2025,
        )
    finally:
        conn.close()

    rows = {row["position_number"]: row for row in result["planned_opportunities"]}
    assert set(rows) == {"POS-DIRECT", "POS-EMBEDDED"}
    assert rows["POS-DIRECT"]["semantic_procurement_intent"] == "direct_product_supply"
    assert rows["POS-EMBEDDED"]["semantic_procurement_intent"] == "embedded_product_work"
    assert rows["POS-EMBEDDED"]["match_confidence"] < rows["POS-DIRECT"]["match_confidence"]

    profile = result["match_diagnostics"]["semantic_profile"]
    assert profile["historical_exact_family_subjects_raw"] == 6
    assert profile["historical_exact_family_subjects"] == 4
    assert profile["historical_subjects_rejected_by_intent"] == 2

    diagnostics = result["match_diagnostics"]["semantic_matching"]
    assert diagnostics["semantic_rejected_service_only"] >= 1
    assert diagnostics["semantic_direct_supply_matches"] >= 1
    assert diagnostics["semantic_embedded_product_work_matches"] >= 1
    top = {row["position_number"]: row for row in diagnostics["top_semantic_candidates"]}
    assert top["POS-SERVICE"]["rejection_reason"] == "service_only"
    assert "POS-AUTO" not in rows
    assert "POS-WATER" not in rows
    assert "POS-PUMP" not in rows
    assert "POS-DIAG" not in rows
    assert top["POS-DIAG"]["rejection_reason"] == "service_only"
    assert top["POS-PUMP"]["rejection_reason"] == "weak_product_evidence"
    assert top["POS-PUMP"]["product_anchor_tokens"] == []
