from __future__ import annotations

import io
import zipfile
import zlib
from pathlib import Path

import procure_radar.organization as organization
from procure_radar.db import connect
from procure_radar.organization import (
    backfill_organizations,
    import_fns_msp,
    import_fns_revexp,
    organization_profile,
    organization_stats,
)


def _zip_xml(path: Path, name: str, xml: str) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, xml.encode("utf-8"))


def test_schema_has_organization_intelligence_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {
        "organizations",
        "organization_roles",
        "organization_msp",
        "organization_okved",
        "organization_financials",
        "organization_import_runs",
    } <= names


def test_existing_gisp_rows_can_backfill_organization_graph(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        conn.execute(
            """
            INSERT INTO gisp_products(
                registry_number, manufacturer_name, manufacturer_inn, manufacturer_ogrn,
                manufacturer_address, product_name, source_scope, is_active, raw_json
            ) VALUES ('R-1', 'ООО Завод', '1234567890', '1234567890123',
                      'Уфа', 'Изделие', 'active', 1, '{}')
            """
        )
        conn.commit()
        result = backfill_organizations(conn)
        org = conn.execute("SELECT * FROM organizations WHERE inn='1234567890'").fetchone()
        roles = {
            row[0]
            for row in conn.execute("SELECT role FROM organization_roles WHERE inn='1234567890'")
        }
    finally:
        conn.close()

    assert result["organizations"] == 1
    assert org is not None
    assert org["name"] == "ООО Завод"
    assert org["ogrn"] == "1234567890123"
    assert "manufacturer" in roles


def test_gisp_trigger_keeps_new_manufacturer_in_organization_graph(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        conn.execute(
            """
            INSERT INTO gisp_products(
                registry_number, manufacturer_name, manufacturer_inn, product_name,
                source_scope, is_active, raw_json
            ) VALUES ('R-1', 'АО Производитель', '0987654321', 'Товар', 'active', 1, '{}')
            """
        )
        org = conn.execute("SELECT * FROM organizations WHERE inn='0987654321'").fetchone()
        role = conn.execute(
            "SELECT 1 FROM organization_roles WHERE inn='0987654321' AND role='manufacturer'"
        ).fetchone()
    finally:
        conn.close()

    assert org is not None
    assert org["name"] == "АО Производитель"
    assert role is not None


def test_import_fns_msp_from_zip_streams_and_replaces_okved_snapshot(tmp_path: Path) -> None:
    archive = tmp_path / "msp.zip"
    _zip_xml(
        archive,
        "part.xml",
        """<?xml version="1.0" encoding="UTF-8"?>
<Файл ВерсФорм="4.01" ТипИнф="РЕЕСТРМСП">
  <Документ ДатаСост="10.08.2026" ДатаВклМСП="01.08.2016"
             ВидСубМСП="1" КатСубМСП="2" ПризНовМСП="2" СведСоцПред="1">
    <ОргВклМСП ИННЮЛ="1234567890" ОГРН="1234567890123" НаимОрг="ООО Тестовый завод"/>
    <СведМН КодРегион="2"><Регион Наим="Башкортостан"/></СведМН>
    <СвЧислРаб ССЧР="17"/>
    <СвОКВЭД>
      <СвОКВЭДОсн КодОКВЭД="25.62" НаимОКВЭД="Обработка металлических изделий"/>
      <СвОКВЭДДоп КодОКВЭД="46.69" НаимОКВЭД="Торговля оборудованием"/>
    </СвОКВЭД>
  </Документ>
</Файл>""",
    )
    conn = connect(tmp_path / "db.sqlite3")
    try:
        result = import_fns_msp(conn, archive)
        org = conn.execute("SELECT * FROM organizations WHERE inn='1234567890'").fetchone()
        msp = conn.execute("SELECT * FROM organization_msp WHERE inn='1234567890'").fetchone()
        okved = [
            tuple(row)
            for row in conn.execute(
                "SELECT code, is_main FROM organization_okved WHERE inn='1234567890' ORDER BY code"
            )
        ]
    finally:
        conn.close()

    assert result["files_seen"] == 1
    assert result["rows_imported"] == 1
    assert org is not None
    assert org["entity_type"] == "legal_entity"
    assert org["name"] == "ООО Тестовый завод"
    assert org["region_code"] == 2
    assert org["okved_main"] == "25.62"
    assert msp is not None
    assert msp["category_name"] == "small"
    assert msp["employee_count"] == 17
    assert msp["is_social"] == 1
    assert okved == [("25.62", 1), ("46.69", 0)]


def test_import_fns_msp_deduplicates_okved_codes_inside_document(tmp_path: Path) -> None:
    archive = tmp_path / "msp_duplicates.zip"
    _zip_xml(
        archive,
        "part.xml",
        """<Файл>
  <Документ ДатаСост="10.08.2026" КатСубМСП="2">
    <ОргВклМСП ИННЮЛ="1234567890" НаимОрг="ООО Дубли ОКВЭД"/>
    <СведМН КодРегион="2"/>
    <СвОКВЭД>
      <СвОКВЭДОсн КодОКВЭД="25.62" НаимОКВЭД="Основной"/>
      <СвОКВЭДДоп КодОКВЭД="25.62" НаимОКВЭД="Повтор основного"/>
      <СвОКВЭДДоп КодОКВЭД="46.69" НаимОКВЭД="Дополнительный"/>
      <СвОКВЭДДоп КодОКВЭД="46.69" НаимОКВЭД="Повтор дополнительного"/>
    </СвОКВЭД>
  </Документ>
</Файл>""",
    )
    conn = connect(tmp_path / "db.sqlite3")
    try:
        result = import_fns_msp(conn, archive)
        okved = [
            tuple(row)
            for row in conn.execute(
                "SELECT code, name, is_main FROM organization_okved "
                "WHERE inn='1234567890' ORDER BY code"
            )
        ]
    finally:
        conn.close()

    assert result["rows_imported"] == 1
    assert okved == [
        ("25.62", "Основной", 1),
        ("46.69", "Дополнительный", 0),
    ]


def test_import_fns_msp_known_only_does_not_create_unobserved_companies(tmp_path: Path) -> None:
    archive = tmp_path / "msp.zip"
    _zip_xml(
        archive,
        "part.xml",
        """<Файл>
  <Документ ДатаСост="10.08.2026" КатСубМСП="1">
    <ОргВклМСП ИННЮЛ="1234567890" НаимОрг="Known"/>
    <СведМН КодРегион="2"/>
  </Документ>
  <Документ ДатаСост="10.08.2026" КатСубМСП="1">
    <ОргВклМСП ИННЮЛ="0987654321" НаимОрг="Unknown"/>
    <СведМН КодРегион="2"/>
  </Документ>
</Файл>""",
    )
    conn = connect(tmp_path / "db.sqlite3")
    try:
        conn.execute("INSERT INTO organizations(inn) VALUES ('1234567890')")
        conn.commit()
        result = import_fns_msp(conn, archive, known_only=True)
        inns = [row[0] for row in conn.execute("SELECT inn FROM organizations ORDER BY inn")]
    finally:
        conn.close()

    assert result["rows_seen"] == 2
    assert result["rows_imported"] == 1
    assert result["rows_skipped"] == 1
    assert inns == ["1234567890"]


def test_import_fns_revenue_expenses_and_unified_profile(tmp_path: Path) -> None:
    archive = tmp_path / "revexp.zip"
    _zip_xml(
        archive,
        "part.xml",
        """<Файл ТипИнф="ОТКРДАННЫЕ5">
  <Документ ДатаСост="31.12.2025">
    <СведНП НаимОрг="ООО Финансы" ИННЮЛ="1234567890"/>
    <СведДохРасх СумДоход="120000000.00" СумРасход="105000000.00"/>
  </Документ>
</Файл>""",
    )
    conn = connect(tmp_path / "db.sqlite3")
    try:
        result = import_fns_revexp(conn, archive)
        profile = organization_profile(conn, "1234567890")
        stats = organization_stats(conn)
    finally:
        conn.close()

    assert result["rows_imported"] == 1
    assert profile is not None
    assert profile["organization"]["name"] == "ООО Финансы"
    assert profile["financials"][0]["year"] == 2025
    assert profile["financials"][0]["revenue"] == 120000000.0
    assert profile["financials"][0]["expenses"] == 105000000.0
    assert profile["financials"][0]["profit"] == 15000000.0
    assert stats["financial_organizations"] == 1


class _BrokenDeflateStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        raise zlib.error("Error -3 while decompressing data: invalid block type")


def test_import_fns_msp_skips_broken_zip_member_and_continues(
    tmp_path: Path, monkeypatch
) -> None:
    good_xml = b"""<File>
  <Document DateSost="10.08.2026" KatSubMSP="1">
    <OrgVklMSP INNYUL="1234567890" NaimOrg="Good"/>
  </Document>
</File>"""
    # Use real Russian element/attribute names because the parser is namespace/name based.
    good_xml = """<Файл>
  <Документ ДатаСост="10.08.2026" КатСубМСП="1">
    <ОргВклМСП ИННЮЛ="1234567890" НаимОрг="ООО Исправный"/>
    <СведМН КодРегион="2"/>
  </Документ>
</Файл>""".encode("utf-8")

    def fake_streams(path, *, source_errors=None):
        del path, source_errors
        yield "msp.zip!broken.xml", _BrokenDeflateStream()
        yield "msp.zip!good.xml", io.BytesIO(good_xml)

    monkeypatch.setattr(organization, "_iter_xml_streams", fake_streams)
    conn = connect(tmp_path / "db.sqlite3")
    try:
        result = import_fns_msp(conn, tmp_path / "msp.zip")
        org = conn.execute(
            "SELECT name FROM organizations WHERE inn='1234567890'"
        ).fetchone()
    finally:
        conn.close()

    assert result["files_seen"] == 2
    assert result["files_failed"] == 1
    assert result["rows_imported"] == 1
    assert result["file_errors"] == [
        {
            "source": "msp.zip!broken.xml",
            "error_type": "error",
            "error": "Error -3 while decompressing data: invalid block type",
        }
    ]
    assert org is not None
    assert org["name"] == "ООО Исправный"


def test_import_fns_msp_skips_member_with_bad_local_header_and_continues(tmp_path: Path) -> None:
    archive = tmp_path / "msp_bad_header.zip"
    broken_xml = """<Файл>
  <Документ ДатаСост="10.08.2026" КатСубМСП="1">
    <ОргВклМСП ИННЮЛ="1111111111" НаимОрг="ООО Битый"/>
    <СведМН КодРегион="2"/>
  </Документ>
</Файл>""".encode("utf-8")
    good_xml = """<Файл>
  <Документ ДатаСост="10.08.2026" КатСубМСП="1">
    <ОргВклМСП ИННЮЛ="1234567890" НаимОрг="ООО Исправный"/>
    <СведМН КодРегион="2"/>
  </Документ>
</Файл>""".encode("utf-8")

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("broken.xml", broken_xml)
        zf.writestr("good.xml", good_xml)

    with zipfile.ZipFile(archive) as zf:
        broken_offset = zf.getinfo("broken.xml").header_offset

    # Keep the central directory valid while corrupting only the local header
    # of one member. ZipFile.infolist() still works, but zf.open(info) raises
    # BadZipFile("Bad magic number for file header") for broken.xml.
    with archive.open("r+b") as fh:
        fh.seek(broken_offset)
        fh.write(b"BORK")

    conn = connect(tmp_path / "db.sqlite3")
    try:
        result = import_fns_msp(conn, archive, region_code=2)
        good = conn.execute(
            "SELECT name FROM organizations WHERE inn='1234567890'"
        ).fetchone()
        broken = conn.execute(
            "SELECT name FROM organizations WHERE inn='1111111111'"
        ).fetchone()
    finally:
        conn.close()

    assert result["files_failed"] == 1
    assert result["rows_imported"] == 1
    assert result["file_errors"][0]["source"].endswith("!broken.xml")
    assert result["file_errors"][0]["error_type"] == "BadZipFile"
    assert result["file_errors"][0]["error"] == "Bad magic number for file header"
    assert good is not None
    assert good["name"] == "ООО Исправный"
    assert broken is None


def _raw_id(conn, external_id: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO raw_documents(source, endpoint, external_id, payload_json)
        VALUES ('test', '/test', ?, '{}')
        """,
        (external_id,),
    )
    return int(cur.lastrowid)


def test_organization_profile_has_yearly_contract_activity_and_revenue_ratio(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES ('1234567890', 'ООО Годовой тест', 2)"
        )
        conn.execute(
            """
            INSERT INTO organization_financials(inn, year, revenue, expenses, profit, source)
            VALUES ('1234567890', 2025, 20000000, 19000000, 1000000, 'fns_revexp')
            """
        )
        raw_2025 = _raw_id(conn, "contract-2025")
        raw_2026 = _raw_id(conn, "contract-2026")
        conn.execute(
            """
            INSERT INTO contracts(raw_document_id, reg_num, price, published_at)
            VALUES (?, 'C-2025', 30000000, '2025-06-01T12:00:00')
            """,
            (raw_2025,),
        )
        contract_2025 = int(conn.execute("SELECT id FROM contracts WHERE reg_num='C-2025'").fetchone()[0])
        conn.execute(
            "INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, '1234567890')",
            (contract_2025,),
        )
        conn.execute(
            """
            INSERT INTO contracts(raw_document_id, reg_num, price, published_at)
            VALUES (?, 'C-2026', 10000000, '2026-02-01T12:00:00')
            """,
            (raw_2026,),
        )
        contract_2026 = int(conn.execute("SELECT id FROM contracts WHERE reg_num='C-2026'").fetchone()[0])
        conn.execute(
            "INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, '1234567890')",
            (contract_2026,),
        )
        conn.commit()

        profile = organization_profile(conn, "1234567890")
    finally:
        conn.close()

    assert profile is not None
    yearly = {row["year"]: row for row in profile["activity_by_year"]}
    assert yearly[2025]["supplier_contracts"] == 1
    assert yearly[2025]["supplier_contract_value_rub"] == 30000000.0
    assert yearly[2025]["financial_revenue_rub"] == 20000000.0
    assert yearly[2025]["supplier_contract_value_to_revenue_ratio"] == 1.5
    assert yearly[2026]["supplier_contracts"] == 1
    assert yearly[2026]["supplier_contract_value_rub"] == 10000000.0
    assert yearly[2026]["supplier_contract_value_to_revenue_ratio"] is None


def test_list_organizations_filters_msp_financials_role_and_region(tmp_path: Path) -> None:
    from procure_radar.organization import list_organizations

    conn = connect(tmp_path / "db.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES ('1234567890', 'ООО Цель', 2)"
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES ('1234567890', 'supplier')"
        )
        conn.execute(
            """
            INSERT INTO organization_msp(inn, entity_type, category_name, employee_count, region_code)
            VALUES ('1234567890', 'legal_entity', 'micro', 7, 2)
            """
        )
        conn.execute(
            """
            INSERT INTO organization_financials(inn, year, revenue, expenses, profit, source)
            VALUES ('1234567890', 2025, 20000000, 19000000, 1000000, 'fns_revexp')
            """
        )
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES ('0987654321', 'ООО Мимо', 2)"
        )
        conn.commit()

        result = list_organizations(
            conn,
            msp_only=True,
            financials_only=True,
            role="supplier",
            region_code=2,
            limit=20,
        )
    finally:
        conn.close()

    assert result["total"] == 1
    assert result["rows"][0]["inn"] == "1234567890"
    assert result["rows"][0]["msp_category"] == "micro"
    assert result["rows"][0]["financial_year"] == 2025
    assert result["rows"][0]["revenue"] == 20000000.0


def test_import_run_schema_migrates_legacy_statuses(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(db_path)
    try:
        legacy.execute(
            """
            CREATE TABLE organization_import_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                files_seen INTEGER NOT NULL DEFAULT 0,
                rows_seen INTEGER NOT NULL DEFAULT 0,
                rows_imported INTEGER NOT NULL DEFAULT 0,
                rows_skipped INTEGER NOT NULL DEFAULT 0,
                filter_json TEXT NOT NULL DEFAULT '{}',
                error TEXT
            )
            """
        )
        legacy.executemany(
            """
            INSERT INTO organization_import_runs(
                source, source_path, started_at, completed_at, error
            ) VALUES (?, ?, '2026-08-22 10:00:00', ?, ?)
            """,
            [
                ("success-source", "a.zip", "2026-08-22 10:10:00", None),
                ("failed-source", "b.zip", "2026-08-22 10:10:00", "boom"),
                ("interrupted-source", "c.zip", None, None),
            ],
        )
        legacy.commit()
    finally:
        legacy.close()

    conn = connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(organization_import_runs)")}
        statuses = {
            row["source"]: row["status"]
            for row in conn.execute("SELECT source, status FROM organization_import_runs")
        }
    finally:
        conn.close()

    assert {"status", "files_failed"} <= columns
    assert statuses == {
        "success-source": "success",
        "failed-source": "failed",
        "interrupted-source": "interrupted",
    }


def test_partial_import_persists_files_failed_and_status(tmp_path: Path, monkeypatch) -> None:
    good_xml = """<Файл>
  <Документ ДатаСост="10.08.2026" КатСубМСП="1">
    <ОргВклМСП ИННЮЛ="1234567890" НаимОрг="ООО Исправный"/>
    <СведМН КодРегион="2"/>
  </Документ>
</Файл>""".encode("utf-8")

    def fake_streams(path, *, source_errors=None):
        del path, source_errors
        yield "msp.zip!broken.xml", _BrokenDeflateStream()
        yield "msp.zip!good.xml", io.BytesIO(good_xml)

    monkeypatch.setattr(organization, "_iter_xml_streams", fake_streams)
    conn = connect(tmp_path / "db.sqlite3")
    try:
        result = import_fns_msp(conn, tmp_path / "msp.zip")
        run = conn.execute(
            "SELECT status, files_failed, error FROM organization_import_runs WHERE id=?",
            (result["run_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert result["status"] == "partial"
    assert result["files_failed"] == 1
    assert run is not None
    assert run["status"] == "partial"
    assert run["files_failed"] == 1
    assert "1 source file(s) failed" in run["error"]


def test_list_organizations_year_separates_all_time_and_target_year_activity(tmp_path: Path) -> None:
    from procure_radar.organization import list_organizations

    conn = connect(tmp_path / "db.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES ('1234567890', 'ООО Период', 2)"
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES ('1234567890', 'supplier')"
        )
        conn.execute(
            """
            INSERT INTO organization_financials(inn, year, revenue, expenses, profit, source)
            VALUES ('1234567890', 2025, 20000000, 19000000, 1000000, 'fns_revexp')
            """
        )
        raw_2025 = _raw_id(conn, "list-contract-2025")
        raw_2026 = _raw_id(conn, "list-contract-2026")
        conn.execute(
            "INSERT INTO contracts(raw_document_id, reg_num, price, published_at) VALUES (?, 'LC-25', 30000000, '2025-06-01')",
            (raw_2025,),
        )
        c25 = int(conn.execute("SELECT id FROM contracts WHERE reg_num='LC-25'").fetchone()[0])
        conn.execute("INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, '1234567890')", (c25,))
        conn.execute(
            "INSERT INTO contracts(raw_document_id, reg_num, price, published_at) VALUES (?, 'LC-26', 10000000, '2026-02-01')",
            (raw_2026,),
        )
        c26 = int(conn.execute("SELECT id FROM contracts WHERE reg_num='LC-26'").fetchone()[0])
        conn.execute("INSERT INTO contract_suppliers(contract_id, inn) VALUES (?, '1234567890')", (c26,))
        conn.commit()

        result = list_organizations(
            conn,
            role="supplier",
            region_code=2,
            financials_only=True,
            year=2025,
        )
        no_financial_2026 = list_organizations(
            conn,
            role="supplier",
            region_code=2,
            financials_only=True,
            year=2026,
        )
    finally:
        conn.close()

    assert result["filters"]["year"] == 2025
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["activity_year"] == 2025
    assert row["financial_year"] == 2025
    assert row["supplier_contracts_all"] == 2
    assert row["supplier_contract_value_all_rub"] == 40000000.0
    assert row["supplier_contracts_year"] == 1
    assert row["supplier_contract_value_year_rub"] == 30000000.0
    assert row["supplier_contract_value_to_revenue_ratio_year"] == 1.5
    # Legacy fields stay all-time for compatibility.
    assert row["supplier_contracts"] == 2
    assert row["supplier_contract_value_rub"] == 40000000.0
    assert no_financial_2026["total"] == 0


def test_manufacturer_intelligence_combines_gisp_fsa_and_okved_evidence(tmp_path: Path) -> None:
    from procure_radar.organization import list_organizations, manufacturer_intelligence

    conn = connect(tmp_path / "db.sqlite3")
    try:
        conn.execute(
            """
            INSERT INTO organizations(inn, name, region_code, okved_main)
            VALUES ('1234567890', 'ООО Торгово-производственное', 2, '46.90')
            """
        )
        conn.execute(
            """
            INSERT INTO organization_okved(inn, source, code, name, is_main)
            VALUES ('1234567890', 'test', '25.62', 'Обработка металлических изделий', 0)
            """
        )
        conn.execute(
            """
            INSERT INTO gisp_products(
                registry_number, manufacturer_name, manufacturer_inn, product_name,
                okpd2_code, tnved_code, source_scope, is_active, raw_json
            ) VALUES ('G-1', 'ООО Торгово-производственное', '1234567890', 'Станок',
                      '28.41.2', '8458', 'active', 1, '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO gisp_registry_rows(
                row_key, registry_number, product_name, okpd2_code, tnved_code,
                source_scope, raw_json
            ) VALUES ('ROW-1', 'G-1', 'Станок', '28.41.2', '8458', 'active', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO fsa_certificates(
                external_id, number, manufacturer_name, manufacturer_inn,
                product_full_name, raw_json
            ) VALUES (101, 'CERT-1', 'ООО Торгово-производственное', '1234567890',
                      'Станок промышленный', '{}')
            """
        )

        # FSA-only manufacturer must also be visible through canonical role=manufacturer.
        conn.execute(
            """
            INSERT INTO organizations(inn, name, region_code, okved_main)
            VALUES ('0987654321', 'ООО ФСА Производитель', 2, '72.19')
            """
        )
        conn.execute(
            """
            INSERT INTO fsa_certificates(
                external_id, number, manufacturer_name, manufacturer_inn,
                product_full_name, raw_json
            ) VALUES (102, 'CERT-2', 'ООО ФСА Производитель', '0987654321',
                      'Изделие', '{}')
            """
        )
        conn.commit()

        intel = manufacturer_intelligence(conn, '1234567890')
        result = list_organizations(conn, role="manufacturer", region_code=2, limit=20)
        fsa_only = list_organizations(
            conn,
            role="manufacturer",
            region_code=2,
            manufacturer_source="fsa",
            limit=20,
        )
    finally:
        conn.close()

    assert intel["source"] == "both"
    assert intel["sources"] == ["gisp", "fsa"]
    assert intel["evidence_count"] == 2
    assert intel["gisp_products"] == 1
    assert intel["gisp_okpd2_codes"] == 1
    assert intel["gisp_tnved_codes"] == 1
    assert intel["fsa_certificates"] == 1
    assert intel["main_okved_profile"] == "trade"
    assert intel["has_manufacturing_okved"] is True
    assert intel["confidence"] == "high"
    assert intel["classification"] == "confirmed_manufacturer"
    assert intel["gisp_product_examples"] == ["Станок"]
    assert intel["fsa_product_examples"] == ["Станок промышленный"]

    by_inn = {row["inn"]: row for row in result["rows"]}
    assert set(by_inn) == {"1234567890", "0987654321"}
    assert by_inn["1234567890"]["manufacturer_source"] == "both"
    assert by_inn["1234567890"]["manufacturer_evidence_count"] == 2
    assert by_inn["1234567890"]["main_okved_profile"] == "trade"
    assert by_inn["1234567890"]["has_manufacturing_okved"] is True
    assert by_inn["1234567890"]["manufacturer_confidence"] == "high"
    assert by_inn["1234567890"]["manufacturer_classification"] == "confirmed_manufacturer"

    assert fsa_only["total"] == 1
    assert fsa_only["rows"][0]["inn"] == "0987654321"
    assert fsa_only["rows"][0]["manufacturer_source"] == "fsa"
    assert fsa_only["rows"][0]["manufacturer_confidence"] == "medium"
    assert fsa_only["rows"][0]["manufacturer_classification"] == "registry_manufacturer"


def test_cli_preserves_manufacturer_source_and_year_filters() -> None:
    from procure_radar.cli import build_parser

    args = build_parser().parse_args(
        [
            "organizations-list",
            "--role",
            "manufacturer",
            "--manufacturer-source",
            "both",
            "--year",
            "2025",
            "--region",
            "2",
        ]
    )
    assert args.role == "manufacturer"
    assert args.manufacturer_source == "both"
    assert args.year == 2025
    assert args.region == 2
