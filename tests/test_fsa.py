from __future__ import annotations

import json

import httpx

from procure_radar.db import connect
from procure_radar.fsa import (
    FSA_ACTIVE_STATUS_ID,
    FsaClient,
    FsaPageLimitError,
    build_known_detail_candidates,
    known_detail_match_report,
    build_certificate_search_body,
    column_search,
    normalize_party_name,
    registry_stats,
    sync_certificate_details,
    sync_certificates,
    sync_tnved_nsi,
    tnved_stats,
    upsert_certificate_detail,
    upsert_certificate_summary,
)


def _summary(cert_id: int = 3717294) -> dict:
    return {
        "id": cert_id,
        "idStatus": 6,
        "number": "ЕАЭС RU С-RU.НК17.В.00013/26",
        "date": "2026-08-21",
        "endDate": "20.08.2031",
        "technicalReglaments": "О безопасности машин и оборудования",
        "group": "Машины для животноводства, птицеводства и кормопроизводства",
        "applicantType": "Изготовитель",
        "applicantName": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "РЕММАШ"',
        "manufacterName": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "РЕММАШ"',
        "productOrig": "РОССИЯ",
        "productFullName": "Машины для животноводства: Навозоуборочные транспортёры",
        "productIdentificationModel": "ТСН 3,0Б, КСН-Ф-100, ТСН-160",
    }


def _detail(cert_id: int = 3717294) -> dict:
    return {
        "idCertificate": cert_id,
        "idStatus": 6,
        "number": "ЕАЭС RU С-RU.НК17.В.00013/26",
        "certRegDate": "2026-08-21",
        "certEndDate": "2031-08-20",
        "applicant": {
            "fullName": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "РЕММАШ"',
            "ogrn": "1021801091861",
            "inn": "1805001016",
            "kpp": "183701001",
            "contacts": [
                {"idContactType": 4, "value": "remmash@glazov.net"},
                {"idContactType": 1, "value": "+73414137272"},
            ],
            "addresses": [{"fullAddress": "427627, РОССИЯ, Удмуртская Республика, Глазов"}],
        },
        "manufacturer": {
            "fullName": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "РЕММАШ"',
            "ogrn": "1021801091861",
            "inn": "1805001016",
            "kpp": "183701001",
            "contacts": [],
            "addresses": [{"fullAddress": "427627, РОССИЯ, Удмуртская Республика, Глазов"}],
        },
        "product": {
            "idProduct": 4057376,
            "fullName": "Машины для животноводства: Навозоуборочные транспортёры",
            "identifications": [
                {
                    "name": "Навозоуборочные транспортёры",
                    "model": "ТСН 3,0Б, КСН-Ф-100, ТСН-160",
                    "idOkpds": [],
                    "idTnveds": [72696],
                }
            ],
        },
        # Intentionally present in source data; implementation must not normalize/store it.
        "experts": [{"firstName": "X", "snils": "00000000000"}],
    }


def test_search_body_matches_observed_active_filter():
    body = build_certificate_search_body(page=0, size=10, active_only=True)
    assert body["size"] == 10
    assert body["page"] == 0
    assert body["filter"]["status"] == [FSA_ACTIVE_STATUS_ID]
    assert body["filter"]["idCertScheme"] == []
    assert body["filter"]["regDate"] == {"minDate": None, "maxDate": None}
    assert body["columnsSort"] == [{"column": "date", "sort": "DESC"}]


def test_party_name_normalization_ignores_common_legal_form_and_punctuation():
    assert normalize_party_name('ООО "РЕММАШ"') == "реммаш"
    assert normalize_party_name('АКЦИОНЕРНОЕ ОБЩЕСТВО «РЕММАШ»') == "реммаш"
    assert normalize_party_name("ИП Иванов Иван Иванович") == "ивановиваниванович"


def test_client_auto_login_search_detail_and_nsi():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/login":
            payload = json.loads(request.content)
            assert payload["username"] == "anonymous"
            return httpx.Response(200, headers={"Authorization": "Bearer test-token"}, json={})
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/certificates/get"):
            body = json.loads(request.content)
            assert body["filter"]["status"] == [6]
            assert body["filter"]["columnsSearch"][0]["name"] == "productFullName"
            return httpx.Response(200, json={"items": [_summary()], "total": 1, "size": 1})
        if request.url.path.endswith("/certificates/3717294"):
            return httpx.Response(200, json=_detail())
        if request.url.path == "/nsi/api/multi":
            body = json.loads(request.content)
            assert body["items"]["tnved"][0]["id"] == [72696]
            return httpx.Response(
                200,
                json={"tnved": [{"id": 72696, "masterId": "72696", "code": "8428908000", "name": "- - прочее", "hidden": False}]},
            )
        raise AssertionError(request.url)

    client = FsaClient(base_url="https://pub.fsa.gov.ru", transport=httpx.MockTransport(handler))
    result = client.search_certificates(
        page=0,
        size=10,
        active_only=True,
        columns_search=[column_search("productFullName", "транспортёр")],
    )
    assert result["total"] == 1
    assert client.get_certificate(3717294)["idCertificate"] == 3717294
    assert client.resolve_tnved_ids([72696])[0]["code"] == "8428908000"
    assert seen.count(("POST", "/login")) == 1





def test_client_retries_transient_login_connect_timeout(monkeypatch):
    login_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_attempts
        if request.url.path == "/login":
            login_attempts += 1
            if login_attempts < 3:
                raise httpx.ConnectTimeout("temporary login timeout", request=request)
            return httpx.Response(200, headers={"Authorization": "Bearer retry-token"}, json={})
        assert request.headers["Authorization"] == "Bearer retry-token"
        return httpx.Response(200, json={"items": [_summary()], "total": 1, "size": 1})

    monkeypatch.setattr("procure_radar.fsa.time.sleep", lambda _seconds: None)
    client = FsaClient(
        base_url="https://pub.fsa.gov.ru",
        transport=httpx.MockTransport(handler),
        max_retries=4,
        retry_backoff=0.01,
    )

    result = client.search_certificates(page=0, size=1, active_only=True)

    assert result["total"] == 1
    assert login_attempts == 3
    assert client.token == "Bearer retry-token"


def test_client_retries_transient_login_503(monkeypatch):
    login_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_attempts
        if request.url.path == "/login":
            login_attempts += 1
            if login_attempts == 1:
                return httpx.Response(503, json={"message": "temporary"})
            return httpx.Response(200, headers={"Authorization": "Bearer retry-token"}, json={})
        assert request.headers["Authorization"] == "Bearer retry-token"
        return httpx.Response(200, json={"items": [_summary()], "total": 1, "size": 1})

    monkeypatch.setattr("procure_radar.fsa.time.sleep", lambda _seconds: None)
    client = FsaClient(
        base_url="https://pub.fsa.gov.ru",
        transport=httpx.MockTransport(handler),
        max_retries=4,
        retry_backoff=0.01,
    )

    result = client.search_certificates(page=0, size=1, active_only=True)

    assert result["total"] == 1
    assert login_attempts == 2


def test_list_auto_falls_back_to_current_get_after_legacy_page_400():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.query.decode()))
        if request.url.path == "/login":
            return httpx.Response(200, headers={"Authorization": "Bearer test-token"}, json={})
        if request.method == "POST" and request.url.path.endswith("/certificates/get"):
            body = json.loads(request.content)
            if body["page"] == 0:
                return httpx.Response(200, json={"items": [_summary(1)], "total": 3})
            return httpx.Response(400, json={"message": "legacy pagination rejected"})
        if request.method == "GET" and request.url.path.endswith("/certificates"):
            page = int(request.url.params["page"])
            assert request.url.params["size"] == "1"
            if page == 1:
                return httpx.Response(200, json={"items": [_summary(2)], "total": 3})
            if page == 2:
                return httpx.Response(200, json={"items": [_summary(3)], "total": 3})
            return httpx.Response(200, json={"items": [], "total": 3})
        raise AssertionError(request.url)

    client = FsaClient(base_url="https://pub.fsa.gov.ru", transport=httpx.MockTransport(handler))
    assert client.search_certificates(page=0, size=1)["items"][0]["id"] == 1
    assert client.search_certificates(page=1, size=1)["items"][0]["id"] == 2
    assert client.search_certificates(page=2, size=1)["items"][0]["id"] == 3
    legacy_page_calls = [row for row in calls if row[0] == "POST" and row[1].endswith("/certificates/get")]
    assert len(legacy_page_calls) == 2
    assert client._resolved_list_protocol == "current-get"



def test_client_exposes_fsa_deep_page_limit_without_invalid_get_fallback():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/login":
            return httpx.Response(200, headers={"Authorization": "Bearer test-token"}, json={})
        if request.method == "POST" and request.url.path.endswith("/certificates/get"):
            return httpx.Response(
                400,
                json={
                    "detail": "Достигнуто ограничение по загрузке страниц: 20. "
                    "Уточните параметры отбора и повторите запрос.",
                    "description": "UpperLimit.searchCertificateRequest.page",
                },
            )
        raise AssertionError(request.url)

    client = FsaClient(base_url="https://pub.fsa.gov.ru", transport=httpx.MockTransport(handler))
    try:
        client.search_certificates(page=21, size=100)
    except FsaPageLimitError as exc:
        assert exc.page == 21
    else:
        raise AssertionError("expected FsaPageLimitError")
    assert not any(method == "GET" and path.endswith("/certificates") for method, path in calls)


def test_sync_auto_partitions_on_fsa_page_limit_and_resumes(tmp_path):
    class FakeClient:
        def search_certificates(self, *, page, size, active_only, columns_search, filter_overrides):
            cursor = None
            if filter_overrides:
                reg = filter_overrides.get("regDate") or {}
                cursor = reg.get("maxDate")
            if cursor is None:
                if page == 0:
                    row = _summary(1); row["date"] = "2026-08-03"
                    return {"items": [row], "total": 10000}
                if page == 1:
                    row = _summary(2); row["date"] = "2026-08-02"
                    return {"items": [row], "total": 10000}
                raise FsaPageLimitError(page, "Достигнуто ограничение по загрузке страниц: 20")
            assert cursor == "2026-08-02"
            if page == 0:
                row = _summary(2); row["date"] = "2026-08-02"
                return {"items": [row], "total": 3}
            if page == 1:
                row = _summary(3); row["date"] = "2026-08-01"
                return {"items": [row], "total": 3}
            row = _summary(4); row["date"] = "2026-07-31"
            return {"items": [row], "total": 3}

    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        first = sync_certificates(
            conn, FakeClient(), page_size=1, pages=2, request_delay=0, active_only=True
        )
        assert first["status"] == "paused"
        assert first["next_page"] == 2

        second = sync_certificates(
            conn, FakeClient(), page_size=1, pages=1, request_delay=0, active_only=True
        )
        assert second["status"] == "paused"
        assert second["cursor_max_date"] == "2026-08-02"
        assert second["partitions"] == 1
        assert second["next_page"] == 1
        state = conn.execute("SELECT * FROM fsa_sync_state").fetchone()
        assert state["cursor_max_date"] == "2026-08-02"
        assert state["partition_count"] == 1

        final = sync_certificates(
            conn, FakeClient(), page_size=1, pages=None, request_delay=0, active_only=True
        )
        assert final["status"] == "complete"
        assert conn.execute("SELECT COUNT(*) FROM fsa_certificates").fetchone()[0] == 4
    finally:
        conn.close()

def test_current_get_active_filter_does_not_stop_sync_on_empty_filtered_page(tmp_path):
    class FakeClient:
        def search_certificates(self, *, page, size, active_only, columns_search, filter_overrides):
            if page == 0:
                return {"items": [], "_fsa_raw_items_count": 1, "total": 2}
            if page == 1:
                return {"items": [_summary(2)], "_fsa_raw_items_count": 1, "total": 2}
            return {"items": [], "_fsa_raw_items_count": 0, "total": 2}

    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        result = sync_certificates(
            conn, FakeClient(), page_size=1, pages=None, request_delay=0, active_only=True
        )
        assert result["status"] == "complete"
        assert result["next_page"] == 2
        assert conn.execute("SELECT COUNT(*) FROM fsa_certificates").fetchone()[0] == 1
    finally:
        conn.close()



def test_existing_fsa_sync_state_is_migrated_for_date_cursor(tmp_path):
    import sqlite3

    db = tmp_path / "legacy-fsa-state.sqlite3"
    raw = sqlite3.connect(db)
    raw.execute(
        """
        CREATE TABLE fsa_sync_state (
            sync_key TEXT PRIMARY KEY, active_only INTEGER NOT NULL DEFAULT 1,
            filter_json TEXT NOT NULL DEFAULT '{}', page_size INTEGER NOT NULL,
            next_page INTEGER NOT NULL DEFAULT 0, total_elements INTEGER,
            pages_scanned INTEGER NOT NULL DEFAULT 0, rows_seen INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT 'x'
        )
        """
    )
    raw.execute(
        "INSERT INTO fsa_sync_state(sync_key,page_size,next_page,total_elements) VALUES('k',100,21,1150178)"
    )
    raw.commit()
    raw.close()

    conn = connect(db)
    try:
        row = conn.execute("SELECT * FROM fsa_sync_state WHERE sync_key='k'").fetchone()
        assert row["next_page"] == 21
        assert row["total_elements"] == 1150178
        assert row["cursor_max_date"] is None
        assert row["partition_count"] == 0
    finally:
        conn.close()

def test_summary_detail_and_tnved_are_normalized(tmp_path):
    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        upsert_certificate_summary(conn, _summary())
        upsert_certificate_detail(conn, _detail())
        conn.commit()

        row = conn.execute("SELECT * FROM fsa_certificates WHERE external_id=3717294").fetchone()
        assert row["applicant_inn"] == "1805001016"
        assert row["applicant_email"] == "remmash@glazov.net"
        assert row["applicant_phone"] == "+73414137272"
        assert row["manufacturer_inn"] == "1805001016"
        assert "snils" not in set(row.keys())
        link = conn.execute("SELECT nsi_id FROM fsa_certificate_tnved").fetchone()
        assert link["nsi_id"] == 72696
        org = conn.execute("SELECT name FROM organizations WHERE inn='1805001016'").fetchone()
        assert org is not None
        role = conn.execute(
            "SELECT 1 FROM organization_roles "
            "WHERE inn='1805001016' AND role='fsa_manufacturer'"
        ).fetchone()
        assert role is not None
    finally:
        conn.close()


def test_sync_list_is_resumable_and_ignores_broken_response_size(tmp_path):
    class FakeClient:
        def search_certificates(self, *, page, size, active_only, columns_search, filter_overrides):
            if page == 0:
                # Observed FSA response has a misleading size field equal to total.
                return {"items": [_summary(1), _summary(2)], "total": 3, "size": 3}
            if page == 1:
                return {"items": [_summary(3)], "total": 3, "size": 3}
            return {"items": [], "total": 3, "size": 3}

    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        result = sync_certificates(
            conn,
            FakeClient(),
            page_size=2,
            pages=None,
            request_delay=0,
            active_only=True,
        )
        assert result["status"] == "complete"
        assert result["next_page"] == 2
        assert conn.execute("SELECT COUNT(*) FROM fsa_certificates").fetchone()[0] == 3
        state = conn.execute("SELECT * FROM fsa_sync_state").fetchone()
        assert state["completed"] == 1
        assert state["rows_seen"] == 3
    finally:
        conn.close()


def test_detail_and_tnved_sync(tmp_path):
    class FakeClient:
        def get_certificate(self, certificate_id):
            return _detail(certificate_id)

        def resolve_tnved_ids(self, ids):
            assert ids == [72696]
            return [{"id": 72696, "masterId": "72696", "code": "8428908000", "name": "- - прочее", "hidden": False}]

    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        upsert_certificate_summary(conn, _summary())
        conn.commit()
        detail_result = sync_certificate_details(conn, FakeClient(), limit=10, request_delay=0)
        assert detail_result == {
            "selected": 1,
            "fetched": 1,
            "errors": 0,
            "batches": 1,
            "pending_before": 1,
            "pending_after": 0,
            "complete": True,
            "error_samples": [],
        }
        nsi_result = sync_tnved_nsi(conn, FakeClient(), batch_size=200, batches=None, request_delay=0)
        assert nsi_result["resolved_this_run"] == 1
        assert nsi_result["unresolved"] == 0

        result = tnved_stats(conn, "8428908000")
        assert result["certificates"] == 1
        assert result["applicants"] == 1
        assert result["manufacturers"] == 1
        assert result["sample"][0]["applicant_email"] == "remmash@glazov.net"
        stats = registry_stats(conn)
        assert stats["certificates"] == 1
        assert stats["details"] == 1
        assert stats["pending_details"] == 0
        assert stats["detail_coverage_pct"] == 100.0
        assert stats["tnved_records"] == 1
    finally:
        conn.close()


def test_detail_sync_continuous_skips_failed_ids_until_next_run(tmp_path):
    class FlakyClient:
        def __init__(self):
            self.calls = []

        def get_certificate(self, certificate_id):
            self.calls.append(certificate_id)
            if certificate_id == 3:
                raise RuntimeError("broken detail")
            return _detail(certificate_id)

    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        for cert_id in (1, 2, 3):
            upsert_certificate_summary(conn, _summary(cert_id))
        conn.commit()

        progress = []
        client = FlakyClient()
        result = sync_certificate_details(
            conn,
            client,
            limit=2,
            request_delay=0,
            continuous=True,
            progress=progress.append,
        )
        assert client.calls.count(3) == 1
        assert result["selected"] == 3
        assert result["fetched"] == 2
        assert result["errors"] == 1
        assert result["batches"] == 2
        assert result["pending_before"] == 3
        assert result["pending_after"] == 1
        assert result["complete"] is False
        assert result["error_samples"][0]["certificate_id"] == 3
        assert progress[-1]["pending"] == 1

        class HealthyClient:
            def get_certificate(self, certificate_id):
                return _detail(certificate_id)

        retry = sync_certificate_details(
            conn, HealthyClient(), limit=2, request_delay=0, continuous=True
        )
        assert retry["pending_before"] == 1
        assert retry["fetched"] == 1
        assert retry["pending_after"] == 0
        assert retry["complete"] is True
    finally:
        conn.close()


def test_detail_sync_rejects_continuous_with_explicit_ids(tmp_path):
    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        try:
            sync_certificate_details(
                conn, object(), limit=10, request_delay=0, certificate_ids=[1], continuous=True
            )
        except ValueError as exc:
            assert "certificate_ids" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        conn.close()


def test_known_detail_candidates_match_summary_names_without_linking_orgs(tmp_path):
    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name) VALUES(?, ?)",
            ("1805001016", 'ООО "РЕММАШ"'),
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
            ("1805001016", "manufacturer"),
        )
        upsert_certificate_summary(conn, _summary(1))
        unknown = _summary(2)
        unknown["applicantName"] = 'ООО "СОВСЕМ ДРУГАЯ КОМПАНИЯ"'
        unknown["manufacterName"] = 'ООО "СОВСЕМ ДРУГАЯ КОМПАНИЯ"'
        upsert_certificate_summary(conn, unknown)
        conn.commit()

        stats = build_known_detail_candidates(conn)
        assert stats["known_reference_organizations"] == 1
        assert stats["known_candidates_total"] == 1
        assert stats["known_candidates_pending"] == 1
        assert stats["known_candidates_detailed"] == 0
        assert stats["known_manufacturer_candidates"] == 1
        assert stats["known_applicant_only_candidates"] == 0
        assert stats["known_match_organizations"] == 1
        assert stats["known_candidates_verified_inn"] == 0
        assert stats["known_candidates_mismatched_inn"] == 0
        candidate = conn.execute(
            "SELECT * FROM fsa_known_detail_candidates"
        ).fetchone()
        assert candidate["external_id"] == 1
        assert candidate["priority"] == 0
        assert candidate["match_party"] == "manufacturer"
        # Name matching is only a fetch hint; summary data alone must not create
        # an FSA organization role.
        assert conn.execute(
            "SELECT 1 FROM organization_roles WHERE role='fsa_manufacturer'"
        ).fetchone() is None
    finally:
        conn.close()


def test_detail_sync_known_only_fetches_only_matching_summaries(tmp_path):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def get_certificate(self, certificate_id):
            self.calls.append(certificate_id)
            return _detail(certificate_id)

    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name) VALUES(?, ?)",
            ("1805001016", 'ООО "РЕММАШ"'),
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
            ("1805001016", "manufacturer"),
        )
        upsert_certificate_summary(conn, _summary(1))
        unknown = _summary(2)
        unknown["applicantName"] = 'ООО "НЕИЗВЕСТНЫЙ ЗАЯВИТЕЛЬ"'
        unknown["manufacterName"] = 'ООО "НЕИЗВЕСТНЫЙ ЗАВОД"'
        upsert_certificate_summary(conn, unknown)
        conn.commit()

        client = FakeClient()
        result = sync_certificate_details(
            conn,
            client,
            limit=10,
            request_delay=0,
            continuous=True,
            known_only=True,
        )
        assert client.calls == [1]
        assert result["scope"] == "known_only"
        assert result["known_candidates_total"] == 1
        assert result["known_candidates_pending"] == 0
        assert result["known_candidates_detailed"] == 1
        assert result["known_candidates_verified_inn"] == 1
        assert result["known_candidates_mismatched_inn"] == 0
        assert result["selected"] == 1
        assert result["fetched"] == 1
        assert result["pending_after"] == 0
        assert result["registry_pending_before"] == 2
        assert conn.execute(
            "SELECT detail_fetched_at FROM fsa_certificates WHERE external_id=2"
        ).fetchone()[0] is None
        # The authoritative detail INN, not the name match, creates the role.
        assert conn.execute(
            "SELECT 1 FROM organization_roles WHERE inn='1805001016' AND role='fsa_manufacturer'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_registry_stats_reports_known_detail_candidates(tmp_path):
    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name) VALUES(?, ?)",
            ("1805001016", 'ООО "РЕММАШ"'),
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
            ("1805001016", "supplier"),
        )
        upsert_certificate_summary(conn, _summary(1))
        conn.commit()
        stats = registry_stats(conn)
        assert stats["known_candidates_total"] == 1
        assert stats["known_candidates_pending"] == 1
        assert stats["known_match_organizations"] == 1
    finally:
        conn.close()



def test_fsa_only_organizations_do_not_expand_known_only_scope(tmp_path):
    class MismatchClient:
        def get_certificate(self, certificate_id):
            detail = _detail(certificate_id)
            detail["manufacturer"] = {
                "fullName": 'ООО "НОВЫЙ ФСА ЗАВОД"',
                "ogrn": "1027700000000",
                "inn": "7700000001",
                "kpp": "770001001",
                "contacts": [],
                "addresses": [{"fullAddress": "Москва"}],
            }
            return detail

    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES(?, ?, ?)",
            ("1805001016", 'ООО "РЕММАШ"', 18),
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
            ("1805001016", "manufacturer"),
        )
        upsert_certificate_summary(conn, _summary(1))
        conn.commit()

        result = sync_certificate_details(
            conn,
            MismatchClient(),
            limit=10,
            request_delay=0,
            continuous=False,
            known_only=True,
        )
        assert result["known_candidates_total"] == 1
        assert result["known_candidates_mismatched_inn"] == 1
        assert result["known_match_organizations"] == 1

        # The authoritative FSA INN creates an FSA-only organization.
        assert conn.execute(
            "SELECT 1 FROM organization_roles WHERE inn='7700000001' AND role='fsa_manufacturer'"
        ).fetchone() is not None

        # A new summary matching only that FSA-created organization must not grow
        # the --known-only target set.
        fsa_only = _summary(2)
        fsa_only["applicantName"] = 'ООО "НОВЫЙ ФСА ЗАВОД"'
        fsa_only["manufacterName"] = 'ООО "НОВЫЙ ФСА ЗАВОД"'
        upsert_certificate_summary(conn, fsa_only)
        conn.commit()
        stats = build_known_detail_candidates(conn)
        assert stats["known_candidates_total"] == 1
        assert stats["known_match_organizations"] == 1
    finally:
        conn.close()


def test_known_detail_match_report_explains_official_inn_mismatch(tmp_path):
    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES(?, ?, ?)",
            ("1805001016", 'ООО "РЕММАШ"', 18),
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
            ("1805001016", "manufacturer"),
        )
        upsert_certificate_summary(conn, _summary(1))
        detail = _detail(1)
        detail["manufacturer"]["inn"] = "7700000001"
        upsert_certificate_detail(conn, detail)
        conn.commit()

        report = known_detail_match_report(conn, limit=10)
        assert report["matching_details"] == 1
        assert report["verified"] == 0
        assert report["mismatch"] == 1
        assert report["rows"][0]["validation"] == "mismatch"
        assert report["rows"][0]["official_inn"] == "7700000001"
        assert report["rows"][0]["matched_known"][0]["inn"] == "1805001016"
    finally:
        conn.close()


def test_registry_stats_reports_fsa_role_regions(tmp_path):
    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        upsert_certificate_summary(conn, _summary(1))
        upsert_certificate_detail(conn, _detail(1))
        conn.commit()
        stats = registry_stats(conn)
        assert stats["fsa_manufacturer_roles"] == 1
        assert stats["fsa_applicant_roles"] == 1
        assert stats["fsa_manufacturer_role_regions"] == {"unknown": 1}
    finally:
        conn.close()


def test_known_detail_candidates_can_scope_reference_organizations_to_region(tmp_path):
    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES(?, ?, ?)",
            ("0270000001", 'ООО "БАШЗАВОД"', 2),
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
            ("0270000001", "manufacturer"),
        )
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES(?, ?, ?)",
            ("7700000001", 'ООО "МОСЗАВОД"', 77),
        )
        conn.execute(
            "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
            ("7700000001", "manufacturer"),
        )
        bash = _summary(101)
        bash["applicantName"] = 'ООО "БАШЗАВОД"'
        bash["manufacterName"] = 'ООО "БАШЗАВОД"'
        upsert_certificate_summary(conn, bash)
        moscow = _summary(102)
        moscow["applicantName"] = 'ООО "МОСЗАВОД"'
        moscow["manufacterName"] = 'ООО "МОСЗАВОД"'
        upsert_certificate_summary(conn, moscow)
        conn.commit()

        stats = build_known_detail_candidates(conn, region_code=2)

        assert stats["known_region_code"] == 2
        assert stats["known_reference_organizations"] == 1
        assert stats["known_candidates_total"] == 1
        assert stats["known_match_organizations"] == 1
        candidate_ids = [
            row[0]
            for row in conn.execute(
                "SELECT external_id FROM fsa_known_detail_candidates ORDER BY external_id"
            )
        ]
        assert candidate_ids == [101]
    finally:
        conn.close()


def test_known_detail_candidates_deprioritize_name_after_official_inn_mismatch(tmp_path):
    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        for inn, name in (
            ("1805001016", 'ООО "РЕММАШ"'),
            ("5074053077", 'ООО "ТРАНСФОРМЕР"'),
        ):
            conn.execute(
                "INSERT INTO organizations(inn, name, region_code) VALUES(?, ?, ?)",
                (inn, name, 2),
            )
            conn.execute(
                "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
                (inn, "manufacturer"),
            )

        mismatch_summary = _summary(201)
        mismatch_summary["applicantName"] = 'ООО "ТРАНСФОРМЕР"'
        mismatch_summary["manufacterName"] = 'ООО "ТРАНСФОРМЕР"'
        upsert_certificate_summary(conn, mismatch_summary)
        mismatch_detail = _detail(201)
        mismatch_detail["applicant"]["fullName"] = 'ООО "ТРАНСФОРМЕР"'
        mismatch_detail["applicant"]["inn"] = "5249051517"
        mismatch_detail["manufacturer"]["fullName"] = 'ООО "ТРАНСФОРМЕР"'
        mismatch_detail["manufacturer"]["inn"] = "5249051517"
        upsert_certificate_detail(conn, mismatch_detail)

        pending_bad = _summary(202)
        pending_bad["applicantName"] = 'ООО "ТРАНСФОРМЕР"'
        pending_bad["manufacterName"] = 'ООО "ТРАНСФОРМЕР"'
        upsert_certificate_summary(conn, pending_bad)
        pending_good = _summary(203)
        upsert_certificate_summary(conn, pending_good)
        conn.commit()

        stats = build_known_detail_candidates(conn, region_code=2)
        priorities = {
            int(row["external_id"]): int(row["priority"])
            for row in conn.execute(
                "SELECT external_id, priority FROM fsa_known_detail_candidates"
            )
        }

        assert stats["known_candidates_mismatched_inn"] == 1
        assert stats["known_deprioritized_mismatch_name_candidates"] >= 2
        assert priorities[202] > priorities[203]
    finally:
        conn.close()


def test_detail_sync_known_only_can_scope_to_region(tmp_path):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def get_certificate(self, certificate_id):
            self.calls.append(certificate_id)
            detail = _detail(certificate_id)
            if certificate_id == 301:
                detail["applicant"]["fullName"] = 'ООО "БАШЗАВОД"'
                detail["applicant"]["inn"] = "0270000001"
                detail["manufacturer"]["fullName"] = 'ООО "БАШЗАВОД"'
                detail["manufacturer"]["inn"] = "0270000001"
            return detail

    conn = connect(tmp_path / "fsa.sqlite3")
    try:
        for inn, name, region in (
            ("0270000001", 'ООО "БАШЗАВОД"', 2),
            ("7700000001", 'ООО "МОСЗАВОД"', 77),
        ):
            conn.execute(
                "INSERT INTO organizations(inn, name, region_code) VALUES(?, ?, ?)",
                (inn, name, region),
            )
            conn.execute(
                "INSERT INTO organization_roles(inn, role) VALUES(?, ?)",
                (inn, "manufacturer"),
            )
        bash = _summary(301)
        bash["applicantName"] = 'ООО "БАШЗАВОД"'
        bash["manufacterName"] = 'ООО "БАШЗАВОД"'
        upsert_certificate_summary(conn, bash)
        moscow = _summary(302)
        moscow["applicantName"] = 'ООО "МОСЗАВОД"'
        moscow["manufacterName"] = 'ООО "МОСЗАВОД"'
        upsert_certificate_summary(conn, moscow)
        conn.commit()

        client = FakeClient()
        result = sync_certificate_details(
            conn,
            client,
            limit=10,
            request_delay=0,
            continuous=True,
            known_only=True,
            known_region_code=2,
        )

        assert client.calls == [301]
        assert result["known_region_code"] == 2
        assert result["known_candidates_total"] == 1
        assert result["known_candidates_verified_inn"] == 1
        assert conn.execute(
            "SELECT region_code FROM organizations WHERE inn='0270000001'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_cli_fsa_known_only_region_options_are_available():
    from procure_radar.cli import build_parser

    parser = build_parser()
    detail = parser.parse_args(["fsa-detail-sync", "--known-only", "--region", "2"])
    stats = parser.parse_args(["fsa-stats", "--region", "2"])
    matches = parser.parse_args(["fsa-known-matches", "--region", "2"])

    assert detail.known_only is True
    assert detail.region == 2
    assert stats.region == 2
    assert matches.region == 2
