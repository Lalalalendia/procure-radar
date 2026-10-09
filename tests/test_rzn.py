from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from procure_radar.db import connect
from procure_radar.rzn import (
    RznClient,
    okpd2_registry_stats,
    registry_stats,
    sync_nsi_records,
    sync_registry,
    upsert_med_product,
    upsert_nsi_record,
)


def _row(external_id: int, *, name: str = "Аппарат ультразвуковой") -> dict[str, Any]:
    return {
        "id": external_id,
        "applicationId": 50000 + external_id,
        "status": {
            "id": 1,
            "code": "active",
            "name": "Действует",
        },
        "legalSystem": "RUSSIA",
        "noRu": f"РЗН 2026/{external_id}",
        "dateRu": "2026-01-10",
        "endDateRu": None,
        "name": name,
        "producer": {"name": "ООО Производитель", "actualAddress": "Москва"},
        "declarant": {"name": "ООО Заявитель", "actualAddress": "Москва"},
        "representative": {"name": "ООО Представитель", "actualAddress": "Уфа"},
        "acceptanceCountriesIds": [],
        "parentMedProductId": external_id,
        "productionSites": ["Москва"],
        "dateRegistrationRu": "2026-01-10",
        "hasChanges": False,
        "dateStart": "2026-01-10",
        "frnsiId": f"o{external_id}",
        "nomClassifierMedicalRfIds": [f"33069{external_id}", f"33070{external_id}"],
    }


@dataclass
class FakeRznClient:
    pages: list[list[dict[str, Any]]]
    seen_status_ids: list[tuple[int, ...] | None] | None = None

    def search_med_products(
        self,
        *,
        text_search: str,
        legal_system: str,
        page: int,
        size: int,
        status_ids: list[int] | tuple[int, ...] | None = None,
    ):
        if self.seen_status_ids is None:
            self.seen_status_ids = []
        self.seen_status_ids.append(tuple(status_ids) if status_ids else None)
        content = self.pages[page] if page < len(self.pages) else []
        total = sum(len(p) for p in self.pages)
        return {
            "content": content,
            "totalElements": total,
            "totalPages": len(self.pages),
            "number": page,
            "size": size,
            "last": page >= len(self.pages) - 1,
        }


def test_upsert_med_product_and_classifier_links(tmp_path: Path):
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        local_id = upsert_med_product(conn, _row(123))
        conn.commit()
        product = conn.execute("SELECT * FROM rzn_med_products WHERE id=?", (local_id,)).fetchone()
        links = conn.execute(
            "SELECT classifier_external_id FROM rzn_med_product_classifier_ids WHERE med_product_id=? ORDER BY 1",
            (local_id,),
        ).fetchall()
    finally:
        conn.close()

    assert product["external_id"] == 123
    assert product["registration_number"] == "РЗН 2026/123"
    assert product["producer_name"] == "ООО Производитель"
    assert product["representative_name"] == "ООО Представитель"
    assert [row[0] for row in links] == ["33069123", "33070123"]


def test_upsert_replaces_classifier_links(tmp_path: Path):
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        first = _row(123)
        upsert_med_product(conn, first)
        changed = _row(123, name="Новое название")
        changed["nomClassifierMedicalRfIds"] = ["only-one"]
        upsert_med_product(conn, changed)
        conn.commit()
        product = conn.execute("SELECT name FROM rzn_med_products WHERE external_id=123").fetchone()
        links = conn.execute(
            "SELECT classifier_external_id FROM rzn_med_product_classifier_ids"
        ).fetchall()
    finally:
        conn.close()

    assert product[0] == "Новое название"
    assert [row[0] for row in links] == ["only-one"]


def test_sync_registry_resumes_and_completes(tmp_path: Path):
    client = FakeRznClient(pages=[[_row(1), _row(2)], [_row(3)]])
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        first = sync_registry(
            conn,
            client,
            page_size=2,
            pages=1,
            request_delay=0,
            emit=lambda _: None,
        )
        second = sync_registry(
            conn,
            client,
            page_size=2,
            pages=1,
            request_delay=0,
            emit=lambda _: None,
        )
        count = conn.execute("SELECT COUNT(*) FROM rzn_med_products").fetchone()[0]
        state = conn.execute("SELECT * FROM rzn_sync_state").fetchone()
    finally:
        conn.close()

    assert first["status"] == "paused"
    assert first["next_page"] == 1
    assert second["status"] == "complete"
    assert count == 3
    assert state["completed"] == 1
    assert state["next_page"] == 2


def test_registry_stats(tmp_path: Path):
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        upsert_med_product(conn, _row(1))
        upsert_med_product(conn, _row(2))
        conn.commit()
        stats = registry_stats(conn)
    finally:
        conn.close()

    assert stats["products"] == 2
    assert stats["classifier_links"] == 4
    assert stats["producers"] == 1
    assert stats["representatives"] == 1
    assert stats["statuses"] == {"Действует": 2}


def test_rzn_client_uses_system_ssl_context_by_default():
    client = RznClient()
    verify = client._verify_arg()
    import ssl
    assert isinstance(verify, ssl.SSLContext)
    assert verify.verify_mode == ssl.CERT_REQUIRED


def test_rzn_client_can_explicitly_disable_ssl_verification():
    client = RznClient(verify_ssl=False)
    assert client._verify_arg() is False


def test_sync_registry_active_only_uses_status_id_1(tmp_path: Path):
    client = FakeRznClient(pages=[[_row(1)]])
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        result = sync_registry(
            conn,
            client,
            page_size=10,
            pages=1,
            request_delay=0,
            active_only=True,
            emit=lambda _: None,
        )
        state = conn.execute("SELECT sync_key FROM rzn_sync_state").fetchone()
    finally:
        conn.close()

    assert result["status"] == "complete"
    assert client.seen_status_ids == [(1,)]
    assert state[0].endswith("|status=active")

@dataclass
class FakeNsiClient:
    records: dict[str, dict[str, Any]]
    seen: list[str] | None = None

    def get_nsi_record(self, record_id: str):
        if self.seen is None:
            self.seen = []
        self.seen.append(record_id)
        return self.records[record_id]


def _nsi(record_id: str, *, code: str = "26.60.13.110", name: str = "Ингаляторы"):
    return {
        "recordId": record_id,
        "catalogCode": "okpd2Code",
        "statusCode": "APPROVED",
        "actualStartDt": "2023-12-22",
        "createDttm": "2023-12-22T10:33:14.883886Z",
        "modifyDttm": "2023-12-22T10:33:14.883886Z",
        "attributeSet": {"code": code, "name": name},
    }


def test_upsert_nsi_record_and_okpd2_join(tmp_path: Path):
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        row = _row(123)
        row["nomClassifierMedicalRfIds"] = ["3263346125381309878"]
        upsert_med_product(conn, row)
        upsert_nsi_record(conn, _nsi("3263346125381309878"))
        conn.commit()
        stats = okpd2_registry_stats(conn, "26.60.13.110")
    finally:
        conn.close()

    assert stats["products"] == 1
    assert stats["producers"] == 1
    assert stats["representatives"] == 1
    assert stats["sample_products"][0]["external_id"] == 123


def test_sync_nsi_records_resolves_only_missing_ids(tmp_path: Path):
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        row = _row(123)
        row["nomClassifierMedicalRfIds"] = ["id-a", "id-b"]
        upsert_med_product(conn, row)
        upsert_nsi_record(conn, _nsi("id-a", code="A", name="A"))
        conn.commit()
        client = FakeNsiClient(records={"id-b": _nsi("id-b", code="B", name="B")})
        result = sync_nsi_records(conn, client, limit=None, request_delay=0, emit=lambda _: None)
        stats = registry_stats(conn)
    finally:
        conn.close()

    assert client.seen == ["id-b"]
    assert result["resolved_this_run"] == 1
    assert result["pending_after"] == 0
    assert stats["nsi_records"] == 2
    assert stats["unresolved_nsi"] == 0


def test_registry_stats_reports_nsi_catalog_distribution(tmp_path: Path):
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        row = _row(123)
        row["nomClassifierMedicalRfIds"] = ["id-med"]
        upsert_med_product(conn, row)
        upsert_nsi_record(
            conn,
            {
                "recordId": "id-med",
                "catalogCode": "nomClassifierMedicalRF",
                "statusCode": "APPROVED",
                "attributeSet": {"code": "100010", "name": "RA33 антитела ИВД"},
            },
        )
        conn.commit()
        stats = registry_stats(conn)
    finally:
        conn.close()
    assert stats["nsi_catalogs"] == {"nomClassifierMedicalRF": 1}
