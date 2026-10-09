from pathlib import Path

from openpyxl import Workbook

from procure_radar.db import connect
from procure_radar.gisp import _find_header, import_registry_xlsx, okpd2_stats, registry_stats


def _write_registry(path: Path, rows: list[list[object]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр"
    ws.append([
        "Наименование",
        "ИНН",
        "ОГРН",
        "Фактический адрес производителя",
        "Первичный регистрационный номер реестровой записи",
        "Реестровый номер",
        "Дата внесения в реестр",
        "Срок действия",
        "Фактическая дата прекращения действия реестровой записи",
        "Наименование",
        "ОКПД2",
        "ТН ВЭД",
    ])
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_import_active_gisp_snapshot_and_query_by_okpd2(tmp_path: Path) -> None:
    xlsx = tmp_path / "gisp-active.xlsx"
    _write_registry(
        xlsx,
        [
            [
                'ООО "Ноут"', "1234567890", "1234567890123", "Москва", "OLD-1", "10001",
                "2026-01-10", "2029-01-10", None, "Ноутбук промышленный", "26.20.11.110", "8471"
            ],
            [
                'АО "Ноут 2"', "0987654321", "1098765432109", "Казань", "OLD-2", "10002",
                "2026-02-10", "2029-02-10", None, "Ноутбук защищенный", "26.20.11.110", "8471"
            ],
        ],
    )
    conn = connect(tmp_path / "db.sqlite3")
    try:
        result = import_registry_xlsx(conn, xlsx, scope="active")
        assert result["rows_imported"] == 2
        stats = registry_stats(conn)
        assert stats["products"] == 2
        assert stats["active_products"] == 2
        assert stats["manufacturers"] == 2
        by_code = okpd2_stats(conn, "26.20.11.110")
        assert by_code["products"] == 2
        assert by_code["manufacturers"] == 2
    finally:
        conn.close()


def test_active_snapshot_removes_records_missing_from_next_snapshot(tmp_path: Path) -> None:
    first = tmp_path / "first.xlsx"
    second = tmp_path / "second.xlsx"
    _write_registry(
        first,
        [
            ["A", "111", "1", None, None, "R1", None, None, None, "P1", "10.10.10.100", None],
            ["B", "222", "2", None, None, "R2", None, None, None, "P2", "10.10.10.200", None],
        ],
    )
    _write_registry(
        second,
        [["B", "222", "2", None, None, "R2", None, None, None, "P2-new", "10.10.10.200", None]],
    )
    conn = connect(tmp_path / "db.sqlite3")
    try:
        import_registry_xlsx(conn, first, scope="active")
        result = import_registry_xlsx(conn, second, scope="active")
        assert result["stale_deleted"] == 1
        rows = conn.execute("SELECT registry_number, product_name FROM gisp_products").fetchall()
        assert [(row["registry_number"], row["product_name"]) for row in rows] == [("R2", "P2-new")]
    finally:
        conn.close()

def test_schema_has_gisp_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "gisp_products" in names
        assert "gisp_import_runs" in names
    finally:
        conn.close()


def test_duplicate_registry_number_preserves_all_code_rows(tmp_path: Path) -> None:
    xlsx = tmp_path / "dupe.xlsx"
    _write_registry(
        xlsx,
        [
            ["A", "111", "1", None, None, "R1", None, None, None, "Product A", "10.10.10.100", "1111"],
            ["A", "111", "1", None, None, "R1", None, None, None, "Product B", "10.10.10.200", "2222"],
        ],
    )
    conn = connect(tmp_path / "db.sqlite3")
    try:
        result = import_registry_xlsx(conn, xlsx, scope="active")
        stats = registry_stats(conn)
        first = okpd2_stats(conn, "10.10.10.100")
        second = okpd2_stats(conn, "10.10.10.200")
    finally:
        conn.close()

    assert result["rows_imported"] == 2
    assert stats["products"] == 1
    assert stats["registry_rows"] == 2
    assert first["products"] == 1
    assert second["products"] == 1


def test_schema_has_gisp_registry_rows(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "gisp_registry_rows" in names


def test_find_header_does_not_require_max_row() -> None:
    class ReadOnlyLikeWorksheet:
        max_row = None

        def iter_rows(self, values_only: bool = False):
            assert values_only is True
            yield ("Отчет ГИСП", None, None, None, None, None)
            yield (
                "Наименование",
                "ИНН",
                "ОГРН",
                "Реестровый номер",
                "Наименование",
                "ОКПД2",
            )

    row_idx, headers = _find_header(ReadOnlyLikeWorksheet())
    assert row_idx == 2
    assert headers[1] == "ИНН"
