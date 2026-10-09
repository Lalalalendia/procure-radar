from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import base64
import json
import re
import sqlite3
import ssl
import time
import unicodedata
from typing import Any, Callable, Iterable

import httpx

FSA_BASE_URL = "https://pub.fsa.gov.ru"
FSA_LOGIN_PATH = "/login"
FSA_CERTIFICATES_PATH = "/api/v1/rss/common/certificates/get"
FSA_CERTIFICATES_CURRENT_PATH = "/api/v1/rss/common/certificates"
FSA_CERTIFICATE_PATH = "/api/v1/rss/common/certificates/{certificate_id}"
FSA_NSI_MULTI_PATH = "/nsi/api/multi"
FSA_ACTIVE_STATUS_ID = 6
FSA_ANONYMOUS_USER = "anonymous"
FSA_ANONYMOUS_PASSWORD = "hrgesf7HDR67Bd"

_LEGAL_FORM_PREFIXES = (
    "общество с ограниченной ответственностью",
    "публичное акционерное общество",
    "непубличное акционерное общество",
    "закрытое акционерное общество",
    "открытое акционерное общество",
    "акционерное общество",
    "индивидуальный предприниматель",
    "федеральное государственное унитарное предприятие",
    "государственное унитарное предприятие",
    "муниципальное унитарное предприятие",
    "ооо",
    "пао",
    "нао",
    "зао",
    "оао",
    "ао",
    "ип",
    "фгуп",
    "гуп",
    "муп",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _jwt_exp(token: str | None) -> int | None:
    if not token:
        return None
    raw = token.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _int(data.get("exp")) if isinstance(data, dict) else None


def build_certificate_search_body(
    *,
    page: int = 0,
    size: int = 100,
    active_only: bool = True,
    columns_search: list[dict[str, Any]] | None = None,
    filter_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if page < 0:
        raise ValueError("page must be >= 0")
    if not 1 <= size <= 100:
        raise ValueError("size must be between 1 and 100")

    filter_body: dict[str, Any] = {
        "status": [FSA_ACTIVE_STATUS_ID] if active_only else [],
        "idCertScheme": [],
        "regDate": {"minDate": None, "maxDate": None},
        "endDate": {"minDate": None, "maxDate": None},
        "columnsSearch": columns_search or [],
    }
    if filter_overrides:
        filter_body.update(filter_overrides)
    return {
        "size": size,
        "page": page,
        "filter": filter_body,
        "columnsSort": [{"column": "date", "sort": "DESC"}],
    }



class FsaPageLimitError(RuntimeError):
    """Raised when FSA rejects deep page-number pagination for one filter."""

    def __init__(self, page: int, detail: str):
        super().__init__(f"FSA page limit reached at page={page}: {detail}")
        self.page = page
        self.detail = detail


def _is_page_limit_error(detail: str) -> bool:
    lowered = detail.lower()
    return (
        "ограничение по загрузке страниц" in lowered
        or "upperlimit.searchcertificaterequest.page" in lowered
    )


def _normalize_reg_date(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    candidate = text[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_party_name(value: Any) -> str | None:
    """Build a conservative comparison key for organization/party names.

    The key is used only to choose FSA detail cards worth fetching. It is not
    accepted as an organization identity: the authoritative link is created
    later from the INN returned by the FSA detail endpoint.
    """
    text = _text(value)
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized).strip()
    if not normalized:
        return None
    for prefix in _LEGAL_FORM_PREFIXES:
        if normalized == prefix:
            return None
        if normalized.startswith(prefix + " "):
            normalized = normalized[len(prefix) + 1 :].strip()
            break
    key = re.sub(r"[^0-9a-zа-я]+", "", normalized)
    return key or None


def _with_reg_date_cursor(
    filter_overrides: dict[str, Any] | None, cursor_max_date: str | None
) -> dict[str, Any] | None:
    if not cursor_max_date:
        return filter_overrides
    copied = json.loads(json.dumps(filter_overrides or {}, ensure_ascii=False))
    reg_date = copied.get("regDate")
    if not isinstance(reg_date, dict):
        reg_date = {}
    # Current legacy POST examples use minDate/maxDate. Preserve a caller's
    # lower bound while tightening only the upper bound for cursor pagination.
    if "startDate" in reg_date and "minDate" not in reg_date:
        reg_date["minDate"] = reg_date.get("startDate")
    reg_date.pop("startDate", None)
    reg_date.pop("endDate", None)
    reg_date["maxDate"] = cursor_max_date
    reg_date.setdefault("minDate", None)
    copied["regDate"] = reg_date
    return copied

def column_search(name: str, search: str) -> dict[str, Any]:
    field = name.strip()
    value = search.strip()
    if not field or not value:
        raise ValueError("search field and value must not be empty")
    # This is the shape used by the public FSA register's columnsSearch filter.
    return {"name": field, "search": value, "type": 0, "translated": False}


@dataclass(slots=True)
class FsaClient:
    base_url: str = FSA_BASE_URL
    timeout: float = 30.0
    token: str | None = None
    cookie: str | None = None
    username: str = FSA_ANONYMOUS_USER
    password: str = FSA_ANONYMOUS_PASSWORD
    max_retries: int = 4
    retry_backoff: float = 2.0
    verify_ssl: bool = True
    transport: httpx.BaseTransport | None = None
    list_protocol: str = "auto"
    _resolved_list_protocol: str | None = field(default=None, init=False, repr=False)

    def _verify_arg(self) -> bool | ssl.SSLContext:
        if not self.verify_ssl:
            return False
        return ssl.create_default_context()

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": self.base_url + "/rss/certificate",
            "User-Agent": "procure-radar/0.1 (+public FSA registry client)",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _token_expired(self) -> bool:
        exp = _jwt_exp(self.token)
        return exp is not None and time.time() >= exp - 30

    def _authenticate(self, client: httpx.Client) -> None:
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = client.post(
                    FSA_LOGIN_PATH,
                    json={"username": self.username, "password": self.password},
                )
            except httpx.ConnectError as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc) and self.verify_ssl:
                    raise RuntimeError(
                        "FSA TLS certificate chain is not trusted by Python. Retry with "
                        "--insecure only if pub.fsa.gov.ru opens normally in your browser."
                    ) from exc
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(self.retry_backoff * (attempt + 1), 20.0))
                continue
            except httpx.RequestError:
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(self.retry_backoff * (attempt + 1), 20.0))
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt >= self.max_retries:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else self.retry_backoff * (attempt + 1)
                except ValueError:
                    delay = self.retry_backoff * (attempt + 1)
                time.sleep(min(max(delay, 0.25), 30.0))
                continue

            response.raise_for_status()
            break

        if response is None:
            raise RuntimeError("FSA authentication did not produce a response")

        token = response.headers.get("Authorization") or response.headers.get("authorization")
        if not token:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                token = _text(body.get("authorization") or body.get("access_token") or body.get("token"))
        if not token:
            raise RuntimeError(
                "FSA /login did not return an Authorization token. The anonymous credentials "
                "may have changed; pass --token from the browser or update --username/--password."
            )
        self.token = token if token.lower().startswith("bearer ") else f"Bearer {token}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            headers=self._base_headers(),
            verify=self._verify_arg(),
            transport=self.transport,
        ) as client:
            if not self.token or self._token_expired():
                self._authenticate(client)

            reauthed = False
            for attempt in range(self.max_retries + 1):
                headers = {"Authorization": self.token} if self.token else {}
                try:
                    response = client.request(method, path, json=json_body, params=params, headers=headers)
                except httpx.ConnectError as exc:
                    if "CERTIFICATE_VERIFY_FAILED" in str(exc) and self.verify_ssl:
                        raise RuntimeError(
                            "FSA TLS certificate chain is not trusted by Python. Retry with "
                            "--insecure only if pub.fsa.gov.ru opens normally in your browser."
                        ) from exc
                    if attempt >= self.max_retries:
                        raise
                    time.sleep(min(self.retry_backoff * (attempt + 1), 20.0))
                    continue
                except httpx.RequestError:
                    if attempt >= self.max_retries:
                        raise
                    time.sleep(min(self.retry_backoff * (attempt + 1), 20.0))
                    continue

                if response.status_code in (401, 403) and not reauthed:
                    self.token = None
                    self._authenticate(client)
                    reauthed = True
                    continue

                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt >= self.max_retries:
                        response.raise_for_status()
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else self.retry_backoff * (attempt + 1)
                    except ValueError:
                        delay = self.retry_backoff * (attempt + 1)
                    time.sleep(min(max(delay, 0.25), 30.0))
                    continue

                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError(f"Expected object response from FSA, got {type(payload).__name__}")
                return payload
        raise RuntimeError("unreachable")

    @staticmethod
    def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
        text = (exc.response.text or "").strip().replace("\r", " ").replace("\n", " ")
        if len(text) > 800:
            text = text[:800] + "..."
        return text or exc.response.reason_phrase or "HTTP error"

    def _search_certificates_current_get(
        self,
        *,
        page: int,
        size: int,
        active_only: bool,
    ) -> dict[str, Any]:
        # The current public register uses GET /certificates with page/size query
        # parameters. We intentionally filter status locally because the public GET
        # contract has changed over time and status query syntax is not stable.
        payload = self._request_json(
            "GET",
            FSA_CERTIFICATES_CURRENT_PATH,
            params={"page": page, "size": size},
        )
        items = payload.get("items")
        if items is None:
            items = payload.get("content")
            if items is not None:
                payload = dict(payload)
                payload["items"] = items
        if items is not None and not isinstance(items, list):
            raise TypeError("FSA response.items must be an array")
        if isinstance(items, list):
            payload = dict(payload)
            payload["_fsa_raw_items_count"] = len(items)
            if active_only:
                payload["items"] = [
                    row for row in items
                    if isinstance(row, dict)
                    and (_int(row.get("idStatus")) in (None, FSA_ACTIVE_STATUS_ID))
                ]
        if payload.get("total") is None and payload.get("totalElements") is not None:
            payload = dict(payload)
            payload["total"] = payload.get("totalElements")
        return payload

    def search_certificates(
        self,
        *,
        page: int = 0,
        size: int = 100,
        active_only: bool = True,
        columns_search: list[dict[str, Any]] | None = None,
        filter_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        protocol = (self._resolved_list_protocol or self.list_protocol or "auto").strip().lower()
        if protocol not in {"auto", "legacy-post", "current-get"}:
            raise ValueError("list_protocol must be auto, legacy-post, or current-get")

        if protocol == "current-get":
            if columns_search or filter_overrides:
                raise ValueError(
                    "current-get FSA list protocol does not support legacy columnsSearch/filter overrides"
                )
            return self._search_certificates_current_get(
                page=page, size=size, active_only=active_only
            )

        body = build_certificate_search_body(
            page=page,
            size=size,
            active_only=active_only,
            columns_search=columns_search,
            filter_overrides=filter_overrides,
        )
        try:
            payload = self._request_json("POST", FSA_CERTIFICATES_PATH, json_body=body)
        except httpx.HTTPStatusError as exc:
            detail = self._http_error_detail(exc)
            if exc.response.status_code == 400 and _is_page_limit_error(detail):
                raise FsaPageLimitError(page, detail) from exc
            can_fallback = (
                protocol == "auto"
                and exc.response.status_code == 400
                and page > 0
                and not columns_search
                and not filter_overrides
            )
            if not can_fallback:
                raise RuntimeError(
                    f"FSA list request failed: HTTP {exc.response.status_code} "
                    f"protocol=legacy-post page={page} size={size}: {detail}"
                ) from exc
            try:
                payload = self._search_certificates_current_get(
                    page=page, size=size, active_only=active_only
                )
            except httpx.HTTPStatusError as get_exc:
                legacy_detail = self._http_error_detail(exc)
                current_detail = self._http_error_detail(get_exc)
                raise RuntimeError(
                    "FSA list pagination failed on both protocols: "
                    f"legacy POST HTTP {exc.response.status_code}: {legacy_detail}; "
                    f"current GET HTTP {get_exc.response.status_code}: {current_detail}"
                ) from get_exc
            self._resolved_list_protocol = "current-get"

        items = payload.get("items")
        if items is not None and not isinstance(items, list):
            raise TypeError("FSA response.items must be an array")
        return payload

    def get_certificate(self, certificate_id: int | str) -> dict[str, Any]:
        rendered = str(certificate_id).strip()
        if not rendered:
            raise ValueError("certificate_id must not be empty")
        return self._request_json("GET", FSA_CERTIFICATE_PATH.format(certificate_id=rendered))

    def resolve_tnved_ids(self, ids: Iterable[int | str]) -> list[dict[str, Any]]:
        unique = sorted({value for raw in ids if (value := _int(raw)) is not None})
        if not unique:
            return []
        payload = self._request_json(
            "POST",
            FSA_NSI_MULTI_PATH,
            json_body={
                "items": {
                    "tnved": [
                        {
                            "id": unique,
                            "fields": ["id", "masterId", "name", "code", "hidden"],
                        }
                    ]
                }
            },
        )
        rows = payload.get("tnved") or []
        if not isinstance(rows, list):
            raise TypeError("FSA NSI response.tnved must be an array")
        return [row for row in rows if isinstance(row, dict)]


def _contacts(party: Any) -> tuple[str | None, str | None]:
    if not isinstance(party, dict):
        return None, None
    email: str | None = None
    phone: str | None = None
    for row in party.get("contacts") or []:
        if not isinstance(row, dict):
            continue
        value = _text(row.get("value"))
        if not value:
            continue
        kind = _int(row.get("idContactType"))
        if kind == 4 or ("@" in value and email is None):
            email = email or value
        elif kind == 1:
            phone = phone or value
    return email, phone


def _address(party: Any) -> str | None:
    if not isinstance(party, dict):
        return None
    addresses = party.get("addresses") or []
    for row in addresses:
        if isinstance(row, dict):
            value = _text(row.get("fullAddress"))
            if value:
                return value
    return None


def _tnved_ids(detail: dict[str, Any]) -> list[int]:
    product = detail.get("product") if isinstance(detail.get("product"), dict) else {}
    result: set[int] = set()
    for identification in product.get("identifications") or []:
        if not isinstance(identification, dict):
            continue
        for raw in identification.get("idTnveds") or []:
            value = _int(raw)
            if value is not None:
                result.add(value)
    return sorted(result)


def upsert_certificate_summary(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    external_id = _int(row.get("id"))
    if external_id is None:
        raise ValueError("FSA certificate row is missing integer id")
    conn.execute(
        """
        INSERT INTO fsa_certificates(
            external_id, status_id, number, reg_date, end_date, blank_number,
            technical_reglaments, product_group, cert_type, cert_object_type,
            applicant_legal_subject_type, applicant_type, applicant_name,
            manufacturer_legal_subject_type, manufacturer_name,
            certification_authority_id, certification_authority_attestat_reg_number,
            product_origin, product_full_name, product_identification_name,
            product_identification_model, product_identification_article,
            product_identification_gtin, raw_json, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(external_id) DO UPDATE SET
            status_id=excluded.status_id,
            number=excluded.number,
            reg_date=excluded.reg_date,
            end_date=excluded.end_date,
            blank_number=excluded.blank_number,
            technical_reglaments=excluded.technical_reglaments,
            product_group=excluded.product_group,
            cert_type=excluded.cert_type,
            cert_object_type=excluded.cert_object_type,
            applicant_legal_subject_type=excluded.applicant_legal_subject_type,
            applicant_type=excluded.applicant_type,
            applicant_name=excluded.applicant_name,
            manufacturer_legal_subject_type=excluded.manufacturer_legal_subject_type,
            manufacturer_name=excluded.manufacturer_name,
            certification_authority_id=excluded.certification_authority_id,
            certification_authority_attestat_reg_number=excluded.certification_authority_attestat_reg_number,
            product_origin=excluded.product_origin,
            product_full_name=excluded.product_full_name,
            product_identification_name=excluded.product_identification_name,
            product_identification_model=excluded.product_identification_model,
            product_identification_article=excluded.product_identification_article,
            product_identification_gtin=excluded.product_identification_gtin,
            raw_json=excluded.raw_json,
            updated_at=datetime('now')
        """,
        (
            external_id,
            _int(row.get("idStatus")),
            _text(row.get("number")),
            _text(row.get("date")),
            _text(row.get("endDate")),
            _text(row.get("blankNumber")),
            _text(row.get("technicalReglaments")),
            _text(row.get("group")),
            _text(row.get("certType")),
            _text(row.get("certObjectType")),
            _text(row.get("applicantLegalSubjectType")),
            _text(row.get("applicantType")),
            _text(row.get("applicantName")),
            _text(row.get("manufacterLegalSubjectType")),
            _text(row.get("manufacterName")),
            _int(row.get("idRalCertificationAuthority")),
            _text(row.get("certificationAuthorityAttestatRegNumber")),
            _text(row.get("productOrig")),
            _text(row.get("productFullName")),
            _text(row.get("productIdentificationName")),
            _text(row.get("productIdentificationModel")),
            _text(row.get("productIdentificationArticle")),
            _text(row.get("productIdentificationGtin")),
            json.dumps(row, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    return external_id


def upsert_certificate_detail(conn: sqlite3.Connection, detail: dict[str, Any]) -> int:
    external_id = _int(detail.get("idCertificate"))
    if external_id is None:
        raise ValueError("FSA certificate detail is missing integer idCertificate")

    applicant = detail.get("applicant") if isinstance(detail.get("applicant"), dict) else {}
    manufacturer = detail.get("manufacturer") if isinstance(detail.get("manufacturer"), dict) else {}
    product = detail.get("product") if isinstance(detail.get("product"), dict) else {}
    app_email, app_phone = _contacts(applicant)
    man_email, man_phone = _contacts(manufacturer)

    existing = conn.execute(
        "SELECT 1 FROM fsa_certificates WHERE external_id=?", (external_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO fsa_certificates(
                external_id, status_id, number, reg_date, end_date,
                applicant_name, manufacturer_name, product_full_name,
                raw_json, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))
            """,
            (
                external_id,
                _int(detail.get("idStatus")),
                _text(detail.get("number")),
                _text(detail.get("certRegDate")),
                _text(detail.get("certEndDate")),
                _text(applicant.get("fullName")),
                _text(manufacturer.get("fullName")),
                _text(product.get("fullName")),
                "{}",
            ),
        )

    conn.execute(
        """
        UPDATE fsa_certificates SET
            status_id=COALESCE(?, status_id),
            number=COALESCE(?, number),
            reg_date=COALESCE(?, reg_date),
            end_date=COALESCE(?, end_date),
            applicant_name=COALESCE(?, applicant_name),
            applicant_inn=?, applicant_ogrn=?, applicant_kpp=?,
            applicant_email=?, applicant_phone=?, applicant_address=?,
            manufacturer_name=COALESCE(?, manufacturer_name),
            manufacturer_inn=?, manufacturer_ogrn=?, manufacturer_kpp=?,
            manufacturer_email=?, manufacturer_phone=?, manufacturer_address=?,
            product_id=?, product_full_name=COALESCE(?, product_full_name),
            detail_json=?, detail_fetched_at=datetime('now'), updated_at=datetime('now')
        WHERE external_id=?
        """,
        (
            _int(detail.get("idStatus")),
            _text(detail.get("number")),
            _text(detail.get("certRegDate")),
            _text(detail.get("certEndDate")),
            _text(applicant.get("fullName")),
            _text(applicant.get("inn")),
            _text(applicant.get("ogrn")),
            _text(applicant.get("kpp")),
            app_email,
            app_phone,
            _address(applicant),
            _text(manufacturer.get("fullName")),
            _text(manufacturer.get("inn")),
            _text(manufacturer.get("ogrn")),
            _text(manufacturer.get("kpp")),
            man_email,
            man_phone,
            _address(manufacturer),
            _int(product.get("idProduct")),
            _text(product.get("fullName")),
            json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
            external_id,
        ),
    )

    conn.execute("DELETE FROM fsa_certificate_tnved WHERE certificate_external_id=?", (external_id,))
    for nsi_id in _tnved_ids(detail):
        conn.execute(
            """
            INSERT INTO fsa_certificate_tnved(certificate_external_id, nsi_id)
            VALUES(?,?)
            """,
            (external_id, nsi_id),
        )
    return external_id


def upsert_tnved_records(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        nsi_id = _int(row.get("id"))
        if nsi_id is None:
            continue
        conn.execute(
            """
            INSERT INTO fsa_tnved_nsi(nsi_id, master_id, code, name, hidden, raw_json, updated_at)
            VALUES(?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(nsi_id) DO UPDATE SET
                master_id=excluded.master_id, code=excluded.code, name=excluded.name,
                hidden=excluded.hidden, raw_json=excluded.raw_json, updated_at=datetime('now')
            """,
            (
                nsi_id,
                _text(row.get("masterId")),
                _text(row.get("code")),
                _text(row.get("name")),
                1 if row.get("hidden") is True else 0 if row.get("hidden") is False else None,
                json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        count += 1
    return count


def sync_certificates(
    conn: sqlite3.Connection,
    client: FsaClient,
    *,
    page_size: int = 100,
    pages: int | None = 10,
    request_delay: float = 0.5,
    active_only: bool = True,
    columns_search: list[dict[str, Any]] | None = None,
    filter_overrides: dict[str, Any] | None = None,
    restart: bool = False,
) -> dict[str, Any]:
    key_body = {
        "active_only": active_only,
        "columns_search": columns_search or [],
        "filter_overrides": filter_overrides or {},
        "page_size": page_size,
    }
    sync_key = json.dumps(key_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    state = conn.execute("SELECT * FROM fsa_sync_state WHERE sync_key=?", (sync_key,)).fetchone()
    next_page = 0 if restart or state is None else int(state["next_page"])
    cursor_max_date = None if restart or state is None else _text(state["cursor_max_date"])
    partition_count = 0 if restart or state is None else int(state["partition_count"] or 0)
    root_total = None if state is None else _int(state["total_elements"])
    if restart:
        conn.execute("DELETE FROM fsa_sync_state WHERE sync_key=?", (sync_key,))
        conn.commit()

    def save_partition_cursor(new_cursor: str) -> None:
        nonlocal cursor_max_date, next_page, partition_count
        cursor_max_date = new_cursor
        next_page = 0
        partition_count += 1
        conn.execute(
            """
            INSERT INTO fsa_sync_state(
                sync_key, active_only, filter_json, page_size, next_page,
                total_elements, pages_scanned, rows_seen, completed,
                cursor_max_date, partition_count, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(sync_key) DO UPDATE SET
                next_page=excluded.next_page, completed=0,
                cursor_max_date=excluded.cursor_max_date,
                partition_count=excluded.partition_count, updated_at=datetime('now')
            """,
            (
                sync_key, 1 if active_only else 0,
                json.dumps(key_body, ensure_ascii=False, separators=(",", ":")),
                page_size, 0, root_total, 0, 0, 0,
                cursor_max_date, partition_count,
            ),
        )
        conn.commit()

    scanned = 0
    stored = 0
    stop_reason = "page_budget"
    window_total: int | None = None
    while pages is None or scanned < pages:
        effective_filter = _with_reg_date_cursor(filter_overrides, cursor_max_date)
        try:
            payload = client.search_certificates(
                page=next_page,
                size=page_size,
                active_only=active_only,
                columns_search=columns_search,
                filter_overrides=effective_filter,
            )
        except FsaPageLimitError as exc:
            # FSA caps one search filter at about 20 pages. Refetch the last
            # accepted page, take its oldest registration date, and continue
            # from page zero with regDate.maxDate tightened to that boundary.
            boundary_page = max(0, next_page - 1)
            boundary = client.search_certificates(
                page=boundary_page,
                size=page_size,
                active_only=active_only,
                columns_search=columns_search,
                filter_overrides=effective_filter,
            )
            boundary_rows = [
                row for row in (boundary.get("items") or []) if isinstance(row, dict)
            ]
            dates = [
                normalized
                for row in boundary_rows
                if (normalized := _normalize_reg_date(row.get("date"))) is not None
            ]
            if not dates:
                raise RuntimeError(
                    "FSA page limit was reached, but the last accepted page has no "
                    "parseable certificate registration dates; cannot auto-partition."
                ) from exc
            new_cursor = min(dates)
            if cursor_max_date == new_cursor:
                raise RuntimeError(
                    "FSA date cursor cannot advance past "
                    f"{new_cursor}: more records share one registration date than the "
                    "registry page limit allows. A secondary filter is required."
                ) from exc
            save_partition_cursor(new_cursor)
            if request_delay > 0:
                time.sleep(request_delay)
            continue

        rows = [row for row in (payload.get("items") or []) if isinstance(row, dict)]
        raw_rows_count = _int(payload.get("_fsa_raw_items_count"))
        if raw_rows_count is None:
            raw_rows_count = len(rows)
        window_total = _int(payload.get("total"))
        if root_total is None and cursor_max_date is None:
            root_total = window_total
        for row in rows:
            upsert_certificate_summary(conn, row)
            stored += 1
        scanned += 1
        next_page += 1
        completed = raw_rows_count == 0 or (
            window_total is not None and next_page * page_size >= window_total
        )
        conn.execute(
            """
            INSERT INTO fsa_sync_state(
                sync_key, active_only, filter_json, page_size, next_page,
                total_elements, pages_scanned, rows_seen, completed,
                cursor_max_date, partition_count, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(sync_key) DO UPDATE SET
                active_only=excluded.active_only, filter_json=excluded.filter_json,
                page_size=excluded.page_size, next_page=excluded.next_page,
                total_elements=COALESCE(fsa_sync_state.total_elements, excluded.total_elements),
                pages_scanned=fsa_sync_state.pages_scanned+1,
                rows_seen=fsa_sync_state.rows_seen+excluded.rows_seen,
                completed=excluded.completed,
                cursor_max_date=excluded.cursor_max_date,
                partition_count=excluded.partition_count, updated_at=datetime('now')
            """,
            (
                sync_key,
                1 if active_only else 0,
                json.dumps(key_body, ensure_ascii=False, separators=(",", ":")),
                page_size,
                next_page,
                root_total,
                1,
                raw_rows_count,
                1 if completed else 0,
                cursor_max_date,
                partition_count,
            ),
        )
        conn.commit()
        if completed:
            stop_reason = "complete"
            break
        if request_delay > 0:
            time.sleep(request_delay)

    return {
        "status": "complete" if stop_reason == "complete" else "paused",
        "stop_reason": stop_reason,
        "next_page": next_page,
        "pages": scanned,
        "rows": stored,
        "total_elements": root_total,
        "window_total_elements": window_total,
        "cursor_max_date": cursor_max_date,
        "partitions": partition_count,
    }


def _known_reference_name_index(
    conn: sqlite3.Connection,
    *,
    region_code: int | None = None,
) -> tuple[dict[str, list[tuple[str, bool, str, int | None]]], int]:
    """Return organizations known independently of FSA-only enrichment.

    FSA detail cards may introduce completely new organizations. Those rows must
    not expand --known-only on the next batch, otherwise the target set slowly
    grows toward the whole FSA registry. A reference organization therefore needs
    evidence from procurement, FNS, GISP, OKVED/financial enrichment, or another
    non-FSA role.
    """
    known_manufacturers = {
        str(row["inn"])
        for row in conn.execute(
            "SELECT inn FROM organization_roles WHERE role='manufacturer'"
        )
    }
    rows = conn.execute(
        """
        SELECT DISTINCT o.inn, o.name, o.region_code
        FROM organizations o
        WHERE NULLIF(trim(o.name),'') IS NOT NULL
          AND (? IS NULL OR o.region_code=?)
          AND (
              EXISTS (
                  SELECT 1 FROM organization_roles r
                  WHERE r.inn=o.inn
                    AND r.role NOT IN ('fsa_applicant','fsa_manufacturer')
              )
              OR EXISTS (SELECT 1 FROM organization_msp m WHERE m.inn=o.inn)
              OR EXISTS (SELECT 1 FROM organization_financials f WHERE f.inn=o.inn)
              OR EXISTS (SELECT 1 FROM organization_okved k WHERE k.inn=o.inn)
          )
        """,
        (region_code, region_code),
    ).fetchall()
    name_index: dict[str, list[tuple[str, bool, str, int | None]]] = {}
    reference_inns: set[str] = set()
    for row in rows:
        key = normalize_party_name(row["name"])
        if not key:
            continue
        inn = str(row["inn"])
        reference_inns.add(inn)
        name_index.setdefault(key, []).append(
            (inn, inn in known_manufacturers, str(row["name"]), row["region_code"])
        )
    return name_index, len(reference_inns)


def _summary_party_names(row: sqlite3.Row) -> tuple[str | None, str | None]:
    """Prefer names captured in immutable summary JSON over detail-overwritten columns."""
    applicant_name = _text(row["applicant_name"])
    manufacturer_name = _text(row["manufacturer_name"])
    raw = row["raw_json"] if "raw_json" in row.keys() else None
    if raw:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            applicant_name = _text(payload.get("applicantName")) or applicant_name
            manufacturer_name = (
                _text(payload.get("manufacterName"))
                or _text(payload.get("manufacturerName"))
                or manufacturer_name
            )
    return applicant_name, manufacturer_name


def build_known_detail_candidates(
    conn: sqlite3.Connection, *, region_code: int | None = None
) -> dict[str, int | float | None]:
    """Match FSA summary names to organizations known independently of FSA.

    Matches are fetch candidates only. Final organization linking is validated by
    the official applicant/manufacturer INN returned by the FSA detail card.
    """
    name_index, reference_count = _known_reference_name_index(conn, region_code=region_code)

    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS fsa_known_detail_candidates(
            external_id INTEGER PRIMARY KEY,
            priority INTEGER NOT NULL,
            match_party TEXT NOT NULL,
            matched_organizations INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("DELETE FROM fsa_known_detail_candidates")

    inserts: list[tuple[int, int, str, int]] = []
    manufacturer_candidates = 0
    applicant_only_candidates = 0
    matched_organizations: set[str] = set()
    validated = 0
    mismatched = 0
    missing_official_inn = 0

    # Names that already produced only official-INN mismatches are still useful
    # candidates, but they should not stay at the front of the queue. This is
    # evidence-based de-prioritization rather than a brittle name-length rule.
    verified_name_keys: set[str] = set()
    mismatched_name_keys: set[str] = set()
    for detail_row in conn.execute(
        """
        SELECT applicant_name, manufacturer_name, applicant_inn, manufacturer_inn,
               detail_fetched_at, raw_json
        FROM fsa_certificates
        WHERE detail_fetched_at IS NOT NULL
        """
    ):
        applicant_summary, manufacturer_summary = _summary_party_names(detail_row)
        manufacturer_key = normalize_party_name(manufacturer_summary)
        applicant_key = normalize_party_name(applicant_summary)
        manufacturer_matches = name_index.get(manufacturer_key or "", [])
        applicant_matches = name_index.get(applicant_key or "", [])
        if manufacturer_matches:
            key = manufacturer_key
            expected = {item[0] for item in manufacturer_matches}
            official = _text(detail_row["manufacturer_inn"])
        elif applicant_matches:
            key = applicant_key
            expected = {item[0] for item in applicant_matches}
            official = _text(detail_row["applicant_inn"])
        else:
            continue
        if not key or not official:
            continue
        if official in expected:
            verified_name_keys.add(key)
        else:
            mismatched_name_keys.add(key)
    deprioritized_name_keys = mismatched_name_keys - verified_name_keys
    deprioritized_candidates = 0

    for row in conn.execute(
        """
        SELECT external_id, applicant_name, manufacturer_name,
               applicant_inn, manufacturer_inn, detail_fetched_at, raw_json
        FROM fsa_certificates
        WHERE NULLIF(trim(manufacturer_name),'') IS NOT NULL
           OR NULLIF(trim(applicant_name),'') IS NOT NULL
           OR NULLIF(trim(raw_json),'') IS NOT NULL
        """
    ):
        applicant_name, manufacturer_name = _summary_party_names(row)
        manufacturer_key = normalize_party_name(manufacturer_name)
        applicant_key = normalize_party_name(applicant_name)
        manufacturer_matches = name_index.get(manufacturer_key or "", [])
        applicant_matches = name_index.get(applicant_key or "", [])
        if not manufacturer_matches and not applicant_matches:
            continue

        matched_inns = {item[0] for item in manufacturer_matches}
        matched_inns.update(item[0] for item in applicant_matches)
        matched_organizations.update(matched_inns)

        if manufacturer_matches:
            manufacturer_candidates += 1
            priority = 0 if any(item[1] for item in manufacturer_matches) else 1
            party = "manufacturer"
            party_key = manufacturer_key
            party_matches = manufacturer_matches
            official_inn = _text(row["manufacturer_inn"])
        else:
            applicant_only_candidates += 1
            priority = 2
            party = "applicant"
            party_key = applicant_key
            party_matches = applicant_matches
            official_inn = _text(row["applicant_inn"])
        if party_key in deprioritized_name_keys:
            priority += 10
            deprioritized_candidates += 1
        inserts.append((int(row["external_id"]), priority, party, len(matched_inns)))

        if row["detail_fetched_at"] is not None:
            expected_inns = {item[0] for item in party_matches}
            if not official_inn:
                missing_official_inn += 1
            elif official_inn in expected_inns:
                validated += 1
            else:
                mismatched += 1

    if inserts:
        conn.executemany(
            """
            INSERT INTO fsa_known_detail_candidates(
                external_id, priority, match_party, matched_organizations
            ) VALUES(?,?,?,?)
            """,
            inserts,
        )
    # Detail fetch errors call rollback(); commit the temp candidate snapshot so
    # those rollbacks cannot discard the selection for the remainder of the run.
    conn.commit()

    total = len(inserts)
    pending = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM fsa_known_detail_candidates k
            JOIN fsa_certificates c ON c.external_id=k.external_id
            WHERE c.detail_fetched_at IS NULL
            """
        ).fetchone()[0]
    )
    validated_with_inn = validated + mismatched
    return {
        "known_region_code": region_code,
        "known_reference_organizations": reference_count,
        "known_candidates_total": total,
        "known_candidates_pending": pending,
        "known_candidates_detailed": total - pending,
        "known_manufacturer_candidates": manufacturer_candidates,
        "known_applicant_only_candidates": applicant_only_candidates,
        "known_deprioritized_mismatch_name_candidates": deprioritized_candidates,
        "known_match_organizations": len(matched_organizations),
        "known_candidates_verified_inn": validated,
        "known_candidates_mismatched_inn": mismatched,
        "known_candidates_missing_official_inn": missing_official_inn,
        "known_candidates_validation_rate_pct": (
            round(validated * 100.0 / validated_with_inn, 2)
            if validated_with_inn
            else 0.0
        ),
    }


def known_detail_match_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    validation: str | None = None,
    region_code: int | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if validation not in {None, "verified", "mismatch", "missing_inn"}:
        raise ValueError("validation must be verified, mismatch, or missing_inn")

    name_index, _ = _known_reference_name_index(conn, region_code=region_code)
    rows_out: list[dict[str, Any]] = []
    totals = {"verified": 0, "mismatch": 0, "missing_inn": 0}
    matching_details = 0

    for row in conn.execute(
        """
        SELECT external_id, number, applicant_name, manufacturer_name,
               applicant_inn, manufacturer_inn, detail_fetched_at, raw_json
        FROM fsa_certificates
        WHERE detail_fetched_at IS NOT NULL
        ORDER BY detail_fetched_at DESC, external_id DESC
        """
    ):
        applicant_summary, manufacturer_summary = _summary_party_names(row)
        manufacturer_matches = name_index.get(
            normalize_party_name(manufacturer_summary) or "", []
        )
        applicant_matches = name_index.get(
            normalize_party_name(applicant_summary) or "", []
        )
        if manufacturer_matches:
            party = "manufacturer"
            summary_name = manufacturer_summary
            official_inn = _text(row["manufacturer_inn"])
            official_name = _text(row["manufacturer_name"])
            expected = manufacturer_matches
        elif applicant_matches:
            party = "applicant"
            summary_name = applicant_summary
            official_inn = _text(row["applicant_inn"])
            official_name = _text(row["applicant_name"])
            expected = applicant_matches
        else:
            continue

        matching_details += 1
        expected_inns = {item[0] for item in expected}
        if not official_inn:
            state = "missing_inn"
        elif official_inn in expected_inns:
            state = "verified"
        else:
            state = "mismatch"
        totals[state] += 1
        if validation is not None and state != validation:
            continue
        if len(rows_out) >= limit:
            continue

        rows_out.append(
            {
                "external_id": int(row["external_id"]),
                "number": row["number"],
                "match_party": party,
                "summary_name": summary_name,
                "official_name": official_name,
                "official_inn": official_inn,
                "validation": state,
                "matched_known": [
                    {
                        "inn": item[0],
                        "name": item[2],
                        "region_code": item[3],
                        "known_manufacturer": item[1],
                    }
                    for item in expected
                ],
            }
        )

    validated_with_inn = totals["verified"] + totals["mismatch"]
    return {
        "validation_filter": validation,
        "known_region_code": region_code,
        "matching_details": matching_details,
        "verified": totals["verified"],
        "mismatch": totals["mismatch"],
        "missing_inn": totals["missing_inn"],
        "validation_rate_pct": (
            round(totals["verified"] * 100.0 / validated_with_inn, 2)
            if validated_with_inn
            else 0.0
        ),
        "rows": rows_out,
    }

def sync_certificate_details(
    conn: sqlite3.Connection,
    client: FsaClient,
    *,
    limit: int = 100,
    request_delay: float = 0.5,
    certificate_ids: list[int] | None = None,
    continuous: bool = False,
    known_only: bool = False,
    known_region_code: int | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if continuous and certificate_ids:
        raise ValueError("continuous mode cannot be combined with certificate_ids")
    if known_only and certificate_ids:
        raise ValueError("known_only mode cannot be combined with certificate_ids")
    if known_region_code is not None and not known_only:
        raise ValueError("known_region_code requires known_only mode")

    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS fsa_known_detail_candidates(
            external_id INTEGER PRIMARY KEY,
            priority INTEGER NOT NULL,
            match_party TEXT NOT NULL,
            matched_organizations INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    candidate_stats: dict[str, int] | None = None
    if known_only:
        candidate_stats = build_known_detail_candidates(conn, region_code=known_region_code)

    pending_before = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM fsa_certificates c
            WHERE c.detail_fetched_at IS NULL
              AND (? = 0 OR EXISTS (
                  SELECT 1 FROM fsa_known_detail_candidates k
                  WHERE k.external_id=c.external_id
              ))
            """,
            (1 if known_only else 0,),
        ).fetchone()[0]
    )
    registry_pending_before = int(
        conn.execute(
            "SELECT COUNT(*) FROM fsa_certificates WHERE detail_fetched_at IS NULL"
        ).fetchone()[0]
    )

    # Keep failures out of subsequent batches in the same run. Otherwise a single
    # permanently broken certificate would make --continuous loop forever. The
    # temporary table is connection-local, so a later invocation will retry them.
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS fsa_detail_attempted_run("
        "external_id INTEGER PRIMARY KEY)"
    )
    conn.execute("DELETE FROM fsa_detail_attempted_run")
    conn.commit()

    selected = 0
    fetched = 0
    errors = 0
    batches = 0
    error_samples: list[dict[str, Any]] = []

    while True:
        if certificate_ids:
            if batches > 0:
                break
            ids = list(dict.fromkeys(int(value) for value in certificate_ids))[:limit]
        else:
            if known_only:
                rows = conn.execute(
                    """
                    SELECT c.external_id
                    FROM fsa_certificates c
                    JOIN fsa_known_detail_candidates k ON k.external_id=c.external_id
                    WHERE c.detail_fetched_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM fsa_detail_attempted_run a
                          WHERE a.external_id=c.external_id
                      )
                    ORDER BY k.priority, c.reg_date DESC, c.external_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT c.external_id
                    FROM fsa_certificates c
                    WHERE c.detail_fetched_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM fsa_detail_attempted_run a
                          WHERE a.external_id=c.external_id
                      )
                    ORDER BY c.reg_date DESC, c.external_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ids = [int(row["external_id"]) for row in rows]

        if not ids:
            break

        batch_fetched = 0
        batch_errors = 0
        for idx, certificate_id in enumerate(ids):
            conn.execute(
                "INSERT OR IGNORE INTO fsa_detail_attempted_run(external_id) VALUES(?)",
                (certificate_id,),
            )
            selected += 1
            try:
                detail = client.get_certificate(certificate_id)
                upsert_certificate_detail(conn, detail)
                conn.commit()
                fetched += 1
                batch_fetched += 1
            except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
                conn.rollback()
                # rollback also removes the attempt marker, so restore it before
                # proceeding to the next certificate in this invocation.
                conn.execute(
                    "INSERT OR IGNORE INTO fsa_detail_attempted_run(external_id) VALUES(?)",
                    (certificate_id,),
                )
                conn.commit()
                errors += 1
                batch_errors += 1
                if len(error_samples) < 20:
                    error_samples.append(
                        {
                            "certificate_id": certificate_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
            if request_delay > 0 and idx + 1 < len(ids):
                time.sleep(request_delay)

        batches += 1
        pending_now = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM fsa_certificates c
                WHERE c.detail_fetched_at IS NULL
                  AND (? = 0 OR EXISTS (
                      SELECT 1 FROM fsa_known_detail_candidates k
                      WHERE k.external_id=c.external_id
                  ))
                """,
                (1 if known_only else 0,),
            ).fetchone()[0]
        )
        if progress is not None:
            progress(
                {
                    "batch": batches,
                    "selected": len(ids),
                    "fetched": batch_fetched,
                    "errors": batch_errors,
                    "fetched_total": fetched,
                    "errors_total": errors,
                    "pending": pending_now,
                }
            )

        if not continuous or certificate_ids:
            break
        if request_delay > 0:
            time.sleep(request_delay)

    pending_after = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM fsa_certificates c
            WHERE c.detail_fetched_at IS NULL
              AND (? = 0 OR EXISTS (
                  SELECT 1 FROM fsa_known_detail_candidates k
                  WHERE k.external_id=c.external_id
              ))
            """,
            (1 if known_only else 0,),
        ).fetchone()[0]
    )
    result: dict[str, Any] = {
        "selected": selected,
        "fetched": fetched,
        "errors": errors,
        "batches": batches,
        "pending_before": pending_before,
        "pending_after": pending_after,
        "complete": pending_after == 0,
        "error_samples": error_samples,
    }
    if known_only:
        result["scope"] = "known_only"
        result["registry_pending_before"] = registry_pending_before
        result.update(build_known_detail_candidates(conn, region_code=known_region_code))
    return result


def sync_tnved_nsi(
    conn: sqlite3.Connection,
    client: FsaClient,
    *,
    batch_size: int = 200,
    batches: int | None = 1,
    request_delay: float = 0.25,
) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    resolved = 0
    batch_count = 0
    while batches is None or batch_count < batches:
        rows = conn.execute(
            """
            SELECT DISTINCT l.nsi_id
            FROM fsa_certificate_tnved l
            LEFT JOIN fsa_tnved_nsi n ON n.nsi_id=l.nsi_id
            WHERE n.nsi_id IS NULL
            ORDER BY l.nsi_id
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        ids = [int(row["nsi_id"]) for row in rows]
        if not ids:
            break
        payload = client.resolve_tnved_ids(ids)
        resolved += upsert_tnved_records(conn, payload)
        conn.commit()
        batch_count += 1
        if request_delay > 0 and (batches is None or batch_count < batches):
            time.sleep(request_delay)
    unresolved = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT l.nsi_id)
            FROM fsa_certificate_tnved l
            LEFT JOIN fsa_tnved_nsi n ON n.nsi_id=l.nsi_id
            WHERE n.nsi_id IS NULL
            """
        ).fetchone()[0]
    )
    return {"resolved_this_run": resolved, "batches": batch_count, "unresolved": unresolved}


def registry_stats(conn: sqlite3.Connection, *, region_code: int | None = None) -> dict[str, Any]:
    certificates = int(conn.execute("SELECT COUNT(*) FROM fsa_certificates").fetchone()[0])
    details = int(
        conn.execute("SELECT COUNT(*) FROM fsa_certificates WHERE detail_fetched_at IS NOT NULL").fetchone()[0]
    )
    applicants = int(
        conn.execute(
            "SELECT COUNT(DISTINCT applicant_inn) FROM fsa_certificates WHERE NULLIF(applicant_inn,'') IS NOT NULL"
        ).fetchone()[0]
    )
    manufacturers = int(
        conn.execute(
            "SELECT COUNT(DISTINCT manufacturer_inn) FROM fsa_certificates WHERE NULLIF(manufacturer_inn,'') IS NOT NULL"
        ).fetchone()[0]
    )
    tnved_links = int(conn.execute("SELECT COUNT(*) FROM fsa_certificate_tnved").fetchone()[0])
    tnved_records = int(conn.execute("SELECT COUNT(*) FROM fsa_tnved_nsi").fetchone()[0])
    unresolved = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT l.nsi_id)
            FROM fsa_certificate_tnved l
            LEFT JOIN fsa_tnved_nsi n ON n.nsi_id=l.nsi_id
            WHERE n.nsi_id IS NULL
            """
        ).fetchone()[0]
    )
    latest = conn.execute(
        """
        SELECT active_only, page_size, next_page, total_elements, pages_scanned,
               rows_seen, completed, cursor_max_date, partition_count, updated_at
        FROM fsa_sync_state ORDER BY updated_at DESC LIMIT 1
        """
    ).fetchone()
    candidate_stats = build_known_detail_candidates(conn, region_code=region_code)
    role_counts = {
        str(row["role"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT role, COUNT(DISTINCT inn) AS count
            FROM organization_roles
            WHERE role IN ('fsa_applicant','fsa_manufacturer')
            GROUP BY role
            """
        )
    }
    manufacturer_regions = {
        ("unknown" if row["region_code"] is None else str(row["region_code"])): int(row["count"])
        for row in conn.execute(
            """
            SELECT o.region_code, COUNT(DISTINCT r.inn) AS count
            FROM organization_roles r
            LEFT JOIN organizations o ON o.inn=r.inn
            WHERE r.role='fsa_manufacturer'
            GROUP BY o.region_code
            ORDER BY o.region_code
            """
        )
    }
    return {
        "certificates": certificates,
        "details": details,
        "pending_details": certificates - details,
        "detail_coverage_pct": round(details * 100.0 / certificates, 2) if certificates else 0.0,
        "applicant_inns": applicants,
        "manufacturer_inns": manufacturers,
        "fsa_applicant_roles": role_counts.get("fsa_applicant", 0),
        "fsa_manufacturer_roles": role_counts.get("fsa_manufacturer", 0),
        "fsa_manufacturer_role_regions": manufacturer_regions,
        "tnved_links": tnved_links,
        "tnved_records": tnved_records,
        "unresolved_tnved": unresolved,
        **candidate_stats,
        "latest_sync": dict(latest) if latest is not None else None,
    }


def tnved_stats(conn: sqlite3.Connection, code: str, *, limit: int = 20) -> dict[str, Any]:
    summary = conn.execute(
        """
        SELECT COUNT(DISTINCT c.external_id) AS certificates,
               COUNT(DISTINCT NULLIF(c.applicant_inn,'')) AS applicants,
               COUNT(DISTINCT NULLIF(c.manufacturer_inn,'')) AS manufacturers
        FROM fsa_certificates c
        JOIN fsa_certificate_tnved l ON l.certificate_external_id=c.external_id
        JOIN fsa_tnved_nsi n ON n.nsi_id=l.nsi_id
        WHERE n.code=?
        """,
        (code,),
    ).fetchone()
    rows = conn.execute(
        """
        SELECT c.external_id, c.number, c.product_full_name,
               c.applicant_name, c.applicant_inn, c.applicant_phone, c.applicant_email,
               c.manufacturer_name, c.manufacturer_inn, c.product_origin
        FROM fsa_certificates c
        JOIN fsa_certificate_tnved l ON l.certificate_external_id=c.external_id
        JOIN fsa_tnved_nsi n ON n.nsi_id=l.nsi_id
        WHERE n.code=?
        ORDER BY c.reg_date DESC, c.external_id DESC
        LIMIT ?
        """,
        (code, limit),
    ).fetchall()
    return {
        "tnved_code": code,
        "certificates": int(summary["certificates"] or 0),
        "applicants": int(summary["applicants"] or 0),
        "manufacturers": int(summary["manufacturers"] or 0),
        "sample": [dict(row) for row in rows],
    }
