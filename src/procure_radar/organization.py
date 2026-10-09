from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from xml.etree import ElementTree as ET

_MSP_CATEGORY = {
    "1": "micro",
    "2": "small",
    "3": "medium",
}


def normalize_inn(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    if len(text) not in (10, 12) or not text.isdigit():
        return None
    return text


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _attr(elem: ET.Element, *names: str) -> str | None:
    wanted = set(names)
    for key, value in elem.attrib.items():
        if _local_name(key) in wanted:
            text = str(value).strip()
            if text:
                return text
    return None


def _descendant(elem: ET.Element, *names: str) -> ET.Element | None:
    wanted = set(names)
    for child in elem.iter():
        if child is elem:
            continue
        if _local_name(child.tag) in wanted:
            return child
    return None


def _descendants(elem: ET.Element, *names: str) -> list[ET.Element]:
    wanted = set(names)
    return [child for child in elem.iter() if child is not elem and _local_name(child.tag) in wanted]


def _desc_attr(elem: ET.Element, *names: str) -> str | None:
    for child in elem.iter():
        value = _attr(child, *names)
        if value is not None:
            return value
    return None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return text


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip().replace(" ", "")
    if not text:
        return None
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bool_flag(value: str | None, *, yes: set[str] | None = None) -> int | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    true_values = yes or {"1", "true", "yes", "да"}
    if normalized in true_values:
        return 1
    if normalized in {"0", "2", "false", "no", "нет"}:
        return 0
    return None


@contextmanager
def _nested_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Iterator[Path]:
    tmp = tempfile.NamedTemporaryFile(prefix="procure_fns_", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    try:
        with tmp, zf.open(info) as source:
            shutil.copyfileobj(source, tmp, length=1024 * 1024)
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)


_RECOVERABLE_SOURCE_ERRORS = (ET.ParseError, zipfile.BadZipFile, zlib.error, EOFError)


def _source_error(source: str, exc: BaseException) -> dict[str, str]:
    return {
        "source": source,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _iter_xml_streams(
    path: str | Path,
    *,
    source_errors: list[dict[str, str]] | None = None,
) -> Iterator[tuple[str, BinaryIO]]:
    source = Path(path)
    if source.is_dir():
        for item in sorted(source.rglob("*.xml")):
            with item.open("rb") as stream:
                yield str(item), stream
        for item in sorted(source.rglob("*.zip")):
            try:
                yield from _iter_xml_streams(item, source_errors=source_errors)
            except _RECOVERABLE_SOURCE_ERRORS as exc:
                if source_errors is None:
                    raise
                source_errors.append(_source_error(str(item), exc))
        return

    suffix = source.suffix.lower()
    if suffix == ".xml":
        with source.open("rb") as stream:
            yield str(source), stream
        return
    if suffix != ".zip":
        raise ValueError(f"expected XML, ZIP or directory, got: {source}")

    # If the top-level ZIP itself is unreadable, fail loudly: there is no safe
    # way to know which members are intact. Corruption of an individual member
    # is handled by the importer so the remaining XML files can still be used.
    with zipfile.ZipFile(source) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            lower = info.filename.lower()
            if lower.endswith(".xml"):
                label = f"{source}!{info.filename}"
                try:
                    stream = zf.open(info)
                except _RECOVERABLE_SOURCE_ERRORS as exc:
                    if source_errors is None:
                        raise
                    source_errors.append(_source_error(label, exc))
                    continue
                try:
                    with stream:
                        yield label, stream
                except _RECOVERABLE_SOURCE_ERRORS as exc:
                    if source_errors is None:
                        raise
                    source_errors.append(_source_error(label, exc))
            elif lower.endswith(".zip"):
                label = f"{source}!{info.filename}"
                try:
                    with _nested_zip_member(zf, info) as nested:
                        for nested_label, stream in _iter_xml_streams(
                            nested, source_errors=source_errors
                        ):
                            yield f"{label}!{Path(nested_label).name}", stream
                except _RECOVERABLE_SOURCE_ERRORS as exc:
                    if source_errors is None:
                        raise
                    source_errors.append(_source_error(label, exc))

def _iter_documents(stream: BinaryIO) -> Iterator[ET.Element]:
    for _, elem in ET.iterparse(stream, events=("end",)):
        if _local_name(elem.tag) != "Документ":
            continue
        yield elem
        elem.clear()


def _start_import_run(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_path: str | Path,
    filters: dict[str, Any],
) -> int:
    # A new run of the same source supersedes an unclosed one. In normal use
    # imports of one source are serialized, so an older open row means the
    # previous process terminated before it could write its final state.
    conn.execute(
        """
        UPDATE organization_import_runs
        SET status='interrupted', completed_at=COALESCE(completed_at, datetime('now')),
            error=COALESCE(error, 'previous import did not finish cleanly')
        WHERE source=? AND status='running' AND completed_at IS NULL
        """,
        (source,),
    )
    cur = conn.execute(
        """
        INSERT INTO organization_import_runs(source, source_path, filter_json, status)
        VALUES (?, ?, ?, 'running')
        """,
        (source, str(source_path), json.dumps(filters, ensure_ascii=False, separators=(",", ":"))),
    )
    conn.commit()
    return int(cur.lastrowid)


def _finish_import_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    files_seen: int,
    files_failed: int,
    rows_seen: int,
    rows_imported: int,
    rows_skipped: int,
    error: str | None = None,
) -> None:
    if status not in {"success", "partial", "failed", "interrupted"}:
        raise ValueError(f"invalid import status: {status}")
    conn.execute(
        """
        UPDATE organization_import_runs
        SET completed_at=datetime('now'), status=?, files_seen=?, files_failed=?,
            rows_seen=?, rows_imported=?, rows_skipped=?, error=?
        WHERE id=?
        """,
        (status, files_seen, files_failed, rows_seen, rows_imported, rows_skipped, error, run_id),
    )
    conn.commit()


def _source_errors_summary(source_errors: list[dict[str, str]]) -> str | None:
    if not source_errors:
        return None
    first = source_errors[0]
    return (
        f"{len(source_errors)} source file(s) failed; first: "
        f"{first['error_type']}: {first['error']}"
    )


def upsert_organization(
    conn: sqlite3.Connection,
    *,
    inn: str,
    entity_type: str = "unknown",
    ogrn: str | None = None,
    kpp: str | None = None,
    name: str | None = None,
    region_code: int | None = None,
    address: str | None = None,
    registration_date: str | None = None,
    status: str | None = None,
    okved_main: str | None = None,
    authoritative: bool = False,
) -> None:
    normalized = normalize_inn(inn)
    if normalized is None:
        return
    conn.execute(
        """
        INSERT INTO organizations(
            inn, entity_type, ogrn, kpp, name, region_code, address,
            registration_date, status, okved_main
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(inn) DO UPDATE SET
            entity_type=CASE
                WHEN excluded.entity_type!='unknown' AND (organizations.entity_type='unknown' OR ?)
                THEN excluded.entity_type ELSE organizations.entity_type END,
            ogrn=CASE WHEN ? THEN COALESCE(excluded.ogrn, organizations.ogrn)
                      ELSE COALESCE(organizations.ogrn, excluded.ogrn) END,
            kpp=CASE WHEN ? THEN COALESCE(excluded.kpp, organizations.kpp)
                     ELSE COALESCE(organizations.kpp, excluded.kpp) END,
            name=CASE WHEN ? THEN COALESCE(excluded.name, organizations.name)
                      ELSE COALESCE(organizations.name, excluded.name) END,
            region_code=CASE WHEN ? THEN COALESCE(excluded.region_code, organizations.region_code)
                             ELSE COALESCE(organizations.region_code, excluded.region_code) END,
            address=CASE WHEN ? THEN COALESCE(excluded.address, organizations.address)
                         ELSE COALESCE(organizations.address, excluded.address) END,
            registration_date=CASE WHEN ? THEN COALESCE(excluded.registration_date, organizations.registration_date)
                                   ELSE COALESCE(organizations.registration_date, excluded.registration_date) END,
            status=CASE WHEN ? THEN COALESCE(excluded.status, organizations.status)
                        ELSE COALESCE(organizations.status, excluded.status) END,
            okved_main=CASE WHEN ? THEN COALESCE(excluded.okved_main, organizations.okved_main)
                            ELSE COALESCE(organizations.okved_main, excluded.okved_main) END,
            last_seen_at=datetime('now'), updated_at=datetime('now')
        """,
        (
            normalized,
            entity_type,
            ogrn,
            kpp,
            name,
            region_code,
            address,
            registration_date,
            status,
            okved_main,
            1 if authoritative else 0,
            *(1 if authoritative else 0 for _ in range(8)),
        ),
    )


def _valid_inn_sql(column: str) -> str:
    return (
        f"{column} IS NOT NULL AND length(trim({column})) IN (10,12) "
        f"AND trim({column}) NOT GLOB '*[^0-9]*'"
    )


def backfill_organizations(conn: sqlite3.Connection) -> dict[str, int]:
    """Populate the canonical organization layer from data already in Procure Radar."""
    sources: list[tuple[str, str, str]] = [
        ("purchase_parties", "inn", "role"),
        ("contract_suppliers", "inn", "'supplier'"),
    ]
    for table, column, role_expr in sources:
        valid = _valid_inn_sql(column)
        conn.execute(
            f"""
            INSERT OR IGNORE INTO organizations(inn)
            SELECT DISTINCT trim({column}) FROM {table} WHERE {valid}
            """
        )
        conn.execute(
            f"""
            INSERT OR IGNORE INTO organization_roles(inn, role)
            SELECT DISTINCT trim({column}), {role_expr} FROM {table} WHERE {valid}
            """
        )

    for column, role in (("customer_inn", "customer"), ("participant_inn", "procedure_participant")):
        valid = _valid_inn_sql(column)
        conn.execute(
            f"INSERT OR IGNORE INTO organizations(inn) SELECT DISTINCT trim({column}) FROM contract_procedures WHERE {valid}"
        )
        conn.execute(
            f"INSERT OR IGNORE INTO organization_roles(inn, role) SELECT DISTINCT trim({column}), ? FROM contract_procedures WHERE {valid}",
            (role,),
        )

    valid = _valid_inn_sql("customer_inn")
    conn.execute(
        f"INSERT OR IGNORE INTO organizations(inn) SELECT DISTINCT trim(customer_inn) FROM contracts WHERE {valid}"
    )
    conn.execute(
        f"INSERT OR IGNORE INTO organization_roles(inn, role) SELECT DISTINCT trim(customer_inn), 'customer' FROM contracts WHERE {valid}"
    )

    valid = _valid_inn_sql("manufacturer_inn")
    conn.execute(
        f"""
        INSERT INTO organizations(inn, entity_type, ogrn, name, address)
        SELECT trim(manufacturer_inn), 'legal_entity', MAX(NULLIF(trim(manufacturer_ogrn), '')),
               MAX(NULLIF(trim(manufacturer_name), '')), MAX(NULLIF(trim(manufacturer_address), ''))
        FROM gisp_products
        WHERE {valid}
        GROUP BY trim(manufacturer_inn)
        ON CONFLICT(inn) DO UPDATE SET
            entity_type=CASE WHEN organizations.entity_type='unknown' THEN 'legal_entity' ELSE organizations.entity_type END,
            ogrn=COALESCE(organizations.ogrn, excluded.ogrn),
            name=COALESCE(organizations.name, excluded.name),
            address=COALESCE(organizations.address, excluded.address),
            last_seen_at=datetime('now'), updated_at=datetime('now')
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO organization_roles(inn, role)
        SELECT DISTINCT trim(manufacturer_inn), 'manufacturer'
        FROM gisp_products WHERE {valid}
        """
    )

    for prefix, role in (("applicant", "fsa_applicant"), ("manufacturer", "fsa_manufacturer")):
        inn_col = f"{prefix}_inn"
        valid = _valid_inn_sql(inn_col)
        conn.execute(
            f"""
            INSERT INTO organizations(inn, ogrn, kpp, name, address)
            SELECT trim({inn_col}), MAX(NULLIF(trim({prefix}_ogrn), '')),
                   MAX(NULLIF(trim({prefix}_kpp), '')), MAX(NULLIF(trim({prefix}_name), '')),
                   MAX(NULLIF(trim({prefix}_address), ''))
            FROM fsa_certificates
            WHERE {valid}
            GROUP BY trim({inn_col})
            ON CONFLICT(inn) DO UPDATE SET
                ogrn=COALESCE(organizations.ogrn, excluded.ogrn),
                kpp=COALESCE(organizations.kpp, excluded.kpp),
                name=COALESCE(organizations.name, excluded.name),
                address=COALESCE(organizations.address, excluded.address),
                last_seen_at=datetime('now'), updated_at=datetime('now')
            """
        )
        conn.execute(
            f"""
            INSERT OR IGNORE INTO organization_roles(inn, role)
            SELECT DISTINCT trim({inn_col}), ? FROM fsa_certificates WHERE {valid}
            """,
            (role,),
        )

    conn.commit()
    return {
        "organizations": int(conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]),
        "roles": int(conn.execute("SELECT COUNT(*) FROM organization_roles").fetchone()[0]),
        "msp": int(conn.execute("SELECT COUNT(*) FROM organization_msp").fetchone()[0]),
        "financial_rows": int(conn.execute("SELECT COUNT(*) FROM organization_financials").fetchone()[0]),
    }


def _known_inns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT inn FROM organizations")}


def _parse_msp_document(document: ET.Element) -> dict[str, Any] | None:
    org = _descendant(document, "ОргВклМСП")
    ip = _descendant(document, "ИПВклМСП")
    subject = org if org is not None else ip
    if subject is None:
        return None

    if org is not None:
        inn = normalize_inn(_attr(org, "ИННЮЛ", "ИНН"))
        entity_type = "legal_entity"
        name = _attr(org, "НаимОрг", "НаимПолнЮЛ", "НаимЮЛ")
        ogrn = _attr(org, "ОГРН") or _desc_attr(org, "ОГРН")
    else:
        assert ip is not None
        inn = normalize_inn(_attr(ip, "ИННФЛ", "ИНН"))
        entity_type = "individual_entrepreneur"
        ogrn = _attr(ip, "ОГРНИП") or _desc_attr(ip, "ОГРНИП")
        fio = _descendant(ip, "ФИОИП", "ФИО")
        if fio is None:
            name = None
        else:
            name = " ".join(
                part for part in (_attr(fio, "Фамилия"), _attr(fio, "Имя"), _attr(fio, "Отчество")) if part
            ) or None
    if inn is None:
        return None

    location = _descendant(document, "СведМН")
    region_code = _int_or_none(_attr(location, "КодРегион") if location is not None else None)
    main_okved = _descendant(document, "СвОКВЭДОсн")
    main_code = _attr(main_okved, "КодОКВЭД") if main_okved is not None else None
    okved_by_code: dict[str, dict[str, Any]] = {}
    if main_okved is not None and main_code:
        okved_by_code[main_code] = {
            "code": main_code,
            "name": _attr(main_okved, "НаимОКВЭД"),
            "is_main": 1,
        }
    for item in _descendants(document, "СвОКВЭДДоп"):
        code = _attr(item, "КодОКВЭД")
        if not code:
            continue
        existing = okved_by_code.get(code)
        if existing is None:
            okved_by_code[code] = {
                "code": code,
                "name": _attr(item, "НаимОКВЭД"),
                "is_main": 0,
            }
        elif not existing.get("name"):
            existing["name"] = _attr(item, "НаимОКВЭД")

    okved = list(okved_by_code.values())

    category_code = _attr(document, "КатСубМСП")
    employee_count = _int_or_none(_desc_attr(document, "ССЧР", "КолРаб", "ЧислРаб"))
    return {
        "inn": inn,
        "entity_type": entity_type,
        "ogrn": ogrn,
        "name": name,
        "category_code": category_code,
        "category_name": _MSP_CATEGORY.get(category_code or ""),
        "included_at": _iso_date(_attr(document, "ДатаВклМСП")),
        "snapshot_date": _iso_date(_attr(document, "ДатаСост")),
        "is_new": _bool_flag(_attr(document, "ПризНовМСП"), yes={"1"}),
        "is_social": _bool_flag(_attr(document, "СведСоцПред", "ПризнСоцПред"), yes={"1"}),
        "employee_count": employee_count,
        "region_code": region_code,
        "okved_main": main_code,
        "okved": okved,
    }


def import_fns_msp(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    known_only: bool = False,
    region_code: int | None = None,
    commit_every: int = 5000,
) -> dict[str, Any]:
    filters = {"known_only": known_only, "region_code": region_code}
    run_id = _start_import_run(conn, source="fns_msp", source_path=path, filters=filters)
    known = _known_inns(conn) if known_only else None
    files_seen = rows_seen = rows_imported = rows_skipped = 0
    source_errors: list[dict[str, str]] = []
    try:
        for label, stream in _iter_xml_streams(path, source_errors=source_errors):
            files_seen += 1
            try:
                for document in _iter_documents(stream):
                    rows_seen += 1
                    row = _parse_msp_document(document)
                    if row is None:
                        rows_skipped += 1
                        continue
                    if known is not None and row["inn"] not in known:
                        rows_skipped += 1
                        continue
                    if region_code is not None and row["region_code"] != region_code:
                        rows_skipped += 1
                        continue

                    upsert_organization(
                        conn,
                        inn=row["inn"],
                        entity_type=row["entity_type"],
                        ogrn=row["ogrn"],
                        name=row["name"],
                        region_code=row["region_code"],
                        okved_main=row["okved_main"],
                        authoritative=True,
                    )
                    conn.execute(
                        """
                        INSERT INTO organization_msp(
                            inn, entity_type, category_code, category_name, included_at,
                            snapshot_date, is_new, is_social, employee_count, region_code, okved_main
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(inn) DO UPDATE SET
                            entity_type=excluded.entity_type,
                            category_code=excluded.category_code,
                            category_name=excluded.category_name,
                            included_at=excluded.included_at,
                            snapshot_date=excluded.snapshot_date,
                            is_new=excluded.is_new,
                            is_social=excluded.is_social,
                            employee_count=excluded.employee_count,
                            region_code=excluded.region_code,
                            okved_main=excluded.okved_main,
                            updated_at=datetime('now')
                        """,
                        (
                            row["inn"], row["entity_type"], row["category_code"], row["category_name"],
                            row["included_at"], row["snapshot_date"], row["is_new"], row["is_social"],
                            row["employee_count"], row["region_code"], row["okved_main"],
                        ),
                    )
                    conn.execute(
                        "DELETE FROM organization_okved WHERE inn=? AND source='fns_msp'",
                        (row["inn"],),
                    )
                    conn.executemany(
                        """
                        INSERT INTO organization_okved(inn, source, code, name, is_main, snapshot_date)
                        VALUES (?, 'fns_msp', ?, ?, ?, ?)
                        ON CONFLICT(inn, source, code) DO UPDATE SET
                            name=COALESCE(excluded.name, organization_okved.name),
                            is_main=MAX(organization_okved.is_main, excluded.is_main),
                            snapshot_date=COALESCE(excluded.snapshot_date, organization_okved.snapshot_date)
                        """,
                        [
                            (row["inn"], item["code"], item["name"], item["is_main"], row["snapshot_date"])
                            for item in row["okved"]
                        ],
                    )
                    rows_imported += 1
                    if commit_every > 0 and rows_imported % commit_every == 0:
                        conn.commit()
            except _RECOVERABLE_SOURCE_ERRORS as exc:
                source_errors.append(_source_error(label, exc))
                continue

        run_status = "partial" if source_errors else "success"
        _finish_import_run(
            conn, run_id, status=run_status, files_seen=files_seen,
            files_failed=len(source_errors), rows_seen=rows_seen,
            rows_imported=rows_imported, rows_skipped=rows_skipped,
            error=_source_errors_summary(source_errors),
        )
    except Exception as exc:
        conn.rollback()
        run_status = "failed"
        _finish_import_run(
            conn, run_id, status=run_status, files_seen=files_seen,
            files_failed=len(source_errors), rows_seen=rows_seen,
            rows_imported=rows_imported, rows_skipped=rows_skipped, error=str(exc),
        )
        raise
    return {
        "run_id": run_id,
        "status": run_status,
        "files_seen": files_seen,
        "files_failed": len(source_errors),
        "rows_seen": rows_seen,
        "rows_imported": rows_imported,
        "rows_skipped": rows_skipped,
        "file_errors": source_errors[:20],
        "file_errors_truncated": max(0, len(source_errors) - 20),
    }

def _parse_revexp_document(document: ET.Element, forced_year: int | None = None) -> dict[str, Any] | None:
    taxpayer = _descendant(document, "СведНП")
    values = _descendant(document, "СведДохРасх")
    if taxpayer is None or values is None:
        return None
    inn = normalize_inn(_attr(taxpayer, "ИННЮЛ", "ИНН"))
    if inn is None:
        return None
    snapshot_date = _iso_date(_attr(document, "ДатаСост"))
    year = forced_year
    if year is None and snapshot_date and len(snapshot_date) >= 4 and snapshot_date[:4].isdigit():
        year = int(snapshot_date[:4])
    if year is None:
        return None
    revenue = _float_or_none(_attr(values, "СумДоход"))
    expenses = _float_or_none(_attr(values, "СумРасход"))
    return {
        "inn": inn,
        "name": _attr(taxpayer, "НаимОрг", "НаимЮЛ"),
        "year": year,
        "revenue": revenue,
        "expenses": expenses,
        "profit": None if revenue is None or expenses is None else revenue - expenses,
        "snapshot_date": snapshot_date,
    }


def import_fns_revexp(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    known_only: bool = False,
    year: int | None = None,
    commit_every: int = 5000,
) -> dict[str, Any]:
    filters = {"known_only": known_only, "year": year}
    run_id = _start_import_run(conn, source="fns_revexp", source_path=path, filters=filters)
    known = _known_inns(conn) if known_only else None
    files_seen = rows_seen = rows_imported = rows_skipped = 0
    source_errors: list[dict[str, str]] = []
    try:
        for label, stream in _iter_xml_streams(path, source_errors=source_errors):
            files_seen += 1
            try:
                for document in _iter_documents(stream):
                    rows_seen += 1
                    row = _parse_revexp_document(document, forced_year=year)
                    if row is None or (known is not None and row["inn"] not in known):
                        rows_skipped += 1
                        continue
                    upsert_organization(
                        conn,
                        inn=row["inn"],
                        entity_type="legal_entity",
                        name=row["name"],
                        authoritative=True,
                    )
                    conn.execute(
                        """
                        INSERT INTO organization_financials(
                            inn, year, revenue, expenses, profit, source, snapshot_date
                        ) VALUES (?, ?, ?, ?, ?, 'fns_revexp', ?)
                        ON CONFLICT(inn, year, source) DO UPDATE SET
                            revenue=excluded.revenue,
                            expenses=excluded.expenses,
                            profit=excluded.profit,
                            snapshot_date=excluded.snapshot_date,
                            updated_at=datetime('now')
                        """,
                        (
                            row["inn"], row["year"], row["revenue"], row["expenses"],
                            row["profit"], row["snapshot_date"],
                        ),
                    )
                    rows_imported += 1
                    if commit_every > 0 and rows_imported % commit_every == 0:
                        conn.commit()
            except _RECOVERABLE_SOURCE_ERRORS as exc:
                source_errors.append(_source_error(label, exc))
                continue

        run_status = "partial" if source_errors else "success"
        _finish_import_run(
            conn, run_id, status=run_status, files_seen=files_seen,
            files_failed=len(source_errors), rows_seen=rows_seen,
            rows_imported=rows_imported, rows_skipped=rows_skipped,
            error=_source_errors_summary(source_errors),
        )
    except Exception as exc:
        conn.rollback()
        run_status = "failed"
        _finish_import_run(
            conn, run_id, status=run_status, files_seen=files_seen,
            files_failed=len(source_errors), rows_seen=rows_seen,
            rows_imported=rows_imported, rows_skipped=rows_skipped, error=str(exc),
        )
        raise
    return {
        "run_id": run_id,
        "status": run_status,
        "files_seen": files_seen,
        "files_failed": len(source_errors),
        "rows_seen": rows_seen,
        "rows_imported": rows_imported,
        "rows_skipped": rows_skipped,
        "file_errors": source_errors[:20],
        "file_errors_truncated": max(0, len(source_errors) - 20),
    }

def _year_from_expr(expr: str) -> str:
    return f"CAST(substr({expr}, 1, 4) AS INTEGER)"


def organization_activity_by_year(
    conn: sqlite3.Connection,
    inn: str,
    *,
    financials: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_inn(inn)
    if normalized is None:
        raise ValueError("INN must contain 10 or 12 digits")

    by_year: dict[int, dict[str, Any]] = {}

    def bucket(year: int) -> dict[str, Any]:
        return by_year.setdefault(
            year,
            {
                "year": year,
                "customer_purchases": 0,
                "customer_purchase_value_rub": 0.0,
                "customer_contracts": 0,
                "customer_contract_value_rub": 0.0,
                "supplier_contracts": 0,
                "supplier_contract_value_rub": 0.0,
            },
        )

    purchase_date = "COALESCE(NULLIF(p.published_at,''), NULLIF(p.doc_created_at,''), NULLIF(p.doc_updated_at,''), NULLIF(p.updated_at,''))"
    purchase_year = _year_from_expr(purchase_date)
    for row in conn.execute(
        f"""
        SELECT {purchase_year} AS year,
               COUNT(DISTINCT p.id) AS cnt,
               COALESCE(SUM(p.max_price), 0) AS value_rub
        FROM purchases p
        JOIN purchase_parties pp ON pp.purchase_id=p.id
        WHERE pp.inn=? AND pp.role='customer'
          AND substr({purchase_date}, 1, 4) GLOB '[12][0-9][0-9][0-9]'
        GROUP BY year
        """,
        (normalized,),
    ):
        item = bucket(int(row["year"]))
        item["customer_purchases"] = int(row["cnt"] or 0)
        item["customer_purchase_value_rub"] = float(row["value_rub"] or 0)

    contract_date = "COALESCE(NULLIF(c.published_at,''), NULLIF(c.doc_created_at,''), NULLIF(c.doc_updated_at,''), NULLIF(c.updated_at,''), NULLIF(c.elact_at,''))"
    contract_year = _year_from_expr(contract_date)
    for row in conn.execute(
        f"""
        SELECT {contract_year} AS year,
               COUNT(*) AS cnt,
               COALESCE(SUM(c.price), 0) AS value_rub
        FROM contracts c
        WHERE c.customer_inn=?
          AND substr({contract_date}, 1, 4) GLOB '[12][0-9][0-9][0-9]'
        GROUP BY year
        """,
        (normalized,),
    ):
        item = bucket(int(row["year"]))
        item["customer_contracts"] = int(row["cnt"] or 0)
        item["customer_contract_value_rub"] = float(row["value_rub"] or 0)

    for row in conn.execute(
        f"""
        WITH supplier_counts AS (
            SELECT contract_id, COUNT(*) AS supplier_count
            FROM contract_suppliers
            GROUP BY contract_id
        )
        SELECT {contract_year} AS year,
               COUNT(*) AS cnt,
               COALESCE(SUM(c.price / NULLIF(sc.supplier_count, 0)), 0) AS value_rub
        FROM contract_suppliers cs
        JOIN contracts c ON c.id=cs.contract_id
        JOIN supplier_counts sc ON sc.contract_id=c.id
        WHERE cs.inn=?
          AND substr({contract_date}, 1, 4) GLOB '[12][0-9][0-9][0-9]'
        GROUP BY year
        """,
        (normalized,),
    ):
        item = bucket(int(row["year"]))
        item["supplier_contracts"] = int(row["cnt"] or 0)
        item["supplier_contract_value_rub"] = float(row["value_rub"] or 0)

    financial_rows = financials
    if financial_rows is None:
        financial_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT year, revenue, expenses, profit, source, snapshot_date
                FROM organization_financials WHERE inn=? ORDER BY year DESC, source
                """,
                (normalized,),
            )
        ]
    financial_by_year: dict[int, dict[str, Any]] = {}
    for financial in financial_rows:
        year = int(financial["year"])
        financial_by_year.setdefault(year, financial)
        bucket(year)

    result: list[dict[str, Any]] = []
    for year in sorted(by_year, reverse=True):
        item = by_year[year]
        financial = financial_by_year.get(year)
        revenue = None if financial is None else financial.get("revenue")
        item["financial_revenue_rub"] = revenue
        if revenue is not None and float(revenue) > 0:
            item["supplier_contract_value_to_revenue_ratio"] = round(
                float(item["supplier_contract_value_rub"]) / float(revenue), 6
            )
        else:
            item["supplier_contract_value_to_revenue_ratio"] = None
        result.append(item)
    return result


def _okved_major(code: str | None) -> int | None:
    if not code:
        return None
    head = str(code).strip().split('.', 1)[0]
    if not head.isdigit():
        return None
    value = int(head)
    return value if 1 <= value <= 99 else None


def _okved_profile(code: str | None) -> str:
    major = _okved_major(code)
    if major is None:
        return "unknown"
    if 1 <= major <= 3:
        return "agriculture"
    if 5 <= major <= 9:
        return "extractive"
    if 10 <= major <= 33:
        return "manufacturing"
    if major == 35:
        return "energy"
    if 36 <= major <= 39:
        return "utilities_environment"
    if 41 <= major <= 43:
        return "construction"
    if 45 <= major <= 47:
        return "trade"
    return "services_other"


def _is_manufacturing_okved(code: str | None) -> bool:
    major = _okved_major(code)
    return major is not None and 10 <= major <= 33


def _is_production_okved(code: str | None) -> bool:
    major = _okved_major(code)
    return major is not None and (
        1 <= major <= 3 or 5 <= major <= 33 or major == 35 or 36 <= major <= 39
    )


def _is_trade_okved(code: str | None) -> bool:
    major = _okved_major(code)
    return major is not None and 45 <= major <= 47


def _manufacturer_labels(
    *,
    gisp_products: int,
    fsa_certificates: int,
    main_okved: str | None,
    has_manufacturing_okved: bool,
    has_production_okved: bool,
) -> tuple[str | None, str, str]:
    has_gisp = gisp_products > 0
    has_fsa = fsa_certificates > 0
    if has_gisp and has_fsa:
        source = "both"
    elif has_gisp:
        source = "gisp"
    elif has_fsa:
        source = "fsa"
    else:
        return None, "none", "not_manufacturer"

    main_profile = _okved_profile(main_okved)
    if has_manufacturing_okved:
        classification = "confirmed_manufacturer" if has_gisp else "likely_manufacturer"
    elif main_profile == "trade" and not has_production_okved:
        classification = "possible_trader"
    else:
        classification = "registry_manufacturer"

    if source == "both" or classification == "confirmed_manufacturer":
        confidence = "high"
    elif classification in {"likely_manufacturer", "registry_manufacturer"}:
        confidence = "medium"
    else:
        confidence = "low"
    return source, confidence, classification


def manufacturer_intelligence(conn: sqlite3.Connection, inn: str) -> dict[str, Any]:
    normalized = normalize_inn(inn)
    if normalized is None:
        raise ValueError("INN must contain 10 or 12 digits")

    gisp = conn.execute(
        """
        SELECT COUNT(DISTINCT p.registry_number) AS products,
               COUNT(DISTINCT NULLIF(r.okpd2_code,'')) AS okpd2_codes,
               COUNT(DISTINCT NULLIF(r.tnved_code,'')) AS tnved_codes
        FROM gisp_products p
        LEFT JOIN gisp_registry_rows r ON r.registry_number=p.registry_number
        WHERE p.manufacturer_inn=?
        """,
        (normalized,),
    ).fetchone()
    fsa = conn.execute(
        """
        SELECT COUNT(DISTINCT external_id) AS certificates
        FROM fsa_certificates
        WHERE manufacturer_inn=?
        """,
        (normalized,),
    ).fetchone()
    organization = conn.execute(
        "SELECT okved_main FROM organizations WHERE inn=?", (normalized,)
    ).fetchone()
    codes = {
        str(row[0]).strip()
        for row in conn.execute(
            "SELECT code FROM organization_okved WHERE inn=? AND NULLIF(trim(code),'') IS NOT NULL",
            (normalized,),
        )
    }
    main_okved = None if organization is None else organization["okved_main"]
    if main_okved:
        codes.add(str(main_okved).strip())

    gisp_products = int(gisp["products"] or 0) if gisp is not None else 0
    fsa_certificates = int(fsa["certificates"] or 0) if fsa is not None else 0
    has_manufacturing = any(_is_manufacturing_okved(code) for code in codes)
    has_production = any(_is_production_okved(code) for code in codes)
    has_trade = any(_is_trade_okved(code) for code in codes)
    source, confidence, classification = _manufacturer_labels(
        gisp_products=gisp_products,
        fsa_certificates=fsa_certificates,
        main_okved=main_okved,
        has_manufacturing_okved=has_manufacturing,
        has_production_okved=has_production,
    )

    gisp_examples = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT product_name FROM gisp_products
            WHERE manufacturer_inn=? AND NULLIF(trim(product_name),'') IS NOT NULL
            ORDER BY product_name LIMIT 5
            """,
            (normalized,),
        )
    ]
    fsa_examples = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT product_full_name FROM fsa_certificates
            WHERE manufacturer_inn=? AND NULLIF(trim(product_full_name),'') IS NOT NULL
            ORDER BY product_full_name LIMIT 5
            """,
            (normalized,),
        )
    ]
    return {
        "source": source,
        "sources": (["gisp"] if gisp_products else []) + (["fsa"] if fsa_certificates else []),
        "evidence_count": gisp_products + fsa_certificates,
        "gisp_products": gisp_products,
        "gisp_okpd2_codes": int(gisp["okpd2_codes"] or 0) if gisp is not None else 0,
        "gisp_tnved_codes": int(gisp["tnved_codes"] or 0) if gisp is not None else 0,
        "fsa_certificates": fsa_certificates,
        "main_okved_profile": _okved_profile(main_okved),
        "has_manufacturing_okved": has_manufacturing,
        "has_production_okved": has_production,
        "has_trade_okved": has_trade,
        "confidence": confidence,
        "classification": classification,
        "gisp_product_examples": gisp_examples,
        "fsa_product_examples": fsa_examples,
    }


def list_organizations(
    conn: sqlite3.Connection,
    *,
    msp_only: bool = False,
    financials_only: bool = False,
    role: str | None = None,
    region_code: int | None = None,
    year: int | None = None,
    manufacturer_source: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if year is not None and not 1900 <= year <= 2100:
        raise ValueError("year must be between 1900 and 2100")
    if manufacturer_source not in {None, "gisp", "fsa", "both"}:
        raise ValueError("manufacturer_source must be gisp, fsa, or both")

    if year is None:
        financial_cte = """
        latest_year AS (
            SELECT inn, MAX(year) AS year
            FROM organization_financials
            WHERE source='fns_revexp'
            GROUP BY inn
        ),
        target_financial AS (
            SELECT f.*
            FROM organization_financials f
            JOIN latest_year y ON y.inn=f.inn AND y.year=f.year
            WHERE f.source='fns_revexp'
        ),
        """
        activity_year_expr = "tf.year"
    else:
        financial_cte = f"""
        target_financial AS (
            SELECT * FROM organization_financials
            WHERE source='fns_revexp' AND year={int(year)}
        ),
        """
        activity_year_expr = str(int(year))

    where: list[str] = []
    params: list[Any] = []
    if msp_only:
        where.append("m.inn IS NOT NULL")
    if financials_only:
        where.append("tf.inn IS NOT NULL")
    if role:
        if role == "manufacturer":
            # Canonical manufacturer filter: evidence may come from GISP, FSA, or both.
            where.append("(ge.inn IS NOT NULL OR fe.inn IS NOT NULL)")
        else:
            where.append("EXISTS (SELECT 1 FROM organization_roles r WHERE r.inn=o.inn AND r.role=?)")
            params.append(role)
    if region_code is not None:
        where.append("o.region_code=?")
        params.append(region_code)
    if manufacturer_source == "gisp":
        where.append("ge.inn IS NOT NULL AND fe.inn IS NULL")
    elif manufacturer_source == "fsa":
        where.append("fe.inn IS NOT NULL AND ge.inn IS NULL")
    elif manufacturer_source == "both":
        where.append("ge.inn IS NOT NULL AND fe.inn IS NOT NULL")
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    contract_date = "COALESCE(NULLIF(c.published_at,''), NULLIF(c.doc_created_at,''), NULLIF(c.doc_updated_at,''), NULLIF(c.updated_at,''), NULLIF(c.elact_at,''))"
    contract_year = _year_from_expr(contract_date)
    base_cte = f"""
        WITH {financial_cte}
        supplier_counts AS (
            SELECT contract_id, COUNT(*) AS supplier_count
            FROM contract_suppliers
            GROUP BY contract_id
        ),
        supplier_activity_all AS (
            SELECT cs.inn,
                   COUNT(*) AS supplier_contracts,
                   COALESCE(SUM(c.price / NULLIF(sc.supplier_count, 0)), 0) AS supplier_contract_value_rub
            FROM contract_suppliers cs
            JOIN contracts c ON c.id=cs.contract_id
            JOIN supplier_counts sc ON sc.contract_id=c.id
            GROUP BY cs.inn
        ),
        supplier_activity_year AS (
            SELECT cs.inn, {contract_year} AS year,
                   COUNT(*) AS supplier_contracts,
                   COALESCE(SUM(c.price / NULLIF(sc.supplier_count, 0)), 0) AS supplier_contract_value_rub
            FROM contract_suppliers cs
            JOIN contracts c ON c.id=cs.contract_id
            JOIN supplier_counts sc ON sc.contract_id=c.id
            WHERE substr({contract_date}, 1, 4) GLOB '[12][0-9][0-9][0-9]'
            GROUP BY cs.inn, year
        ),
        gisp_evidence AS (
            SELECT p.manufacturer_inn AS inn,
                   COUNT(DISTINCT p.registry_number) AS gisp_products,
                   COUNT(DISTINCT NULLIF(r.okpd2_code,'')) AS gisp_okpd2_codes,
                   COUNT(DISTINCT NULLIF(r.tnved_code,'')) AS gisp_tnved_codes
            FROM gisp_products p
            LEFT JOIN gisp_registry_rows r ON r.registry_number=p.registry_number
            WHERE NULLIF(trim(p.manufacturer_inn),'') IS NOT NULL
            GROUP BY p.manufacturer_inn
        ),
        fsa_evidence AS (
            SELECT manufacturer_inn AS inn,
                   COUNT(DISTINCT external_id) AS fsa_certificates
            FROM fsa_certificates
            WHERE NULLIF(trim(manufacturer_inn),'') IS NOT NULL
            GROUP BY manufacturer_inn
        ),
        okved_codes AS (
            SELECT inn, okved_main AS code
            FROM organizations
            WHERE NULLIF(trim(okved_main),'') IS NOT NULL
            UNION
            SELECT inn, code
            FROM organization_okved
            WHERE NULLIF(trim(code),'') IS NOT NULL
        ),
        okved_flags AS (
            SELECT inn,
                   MAX(CASE WHEN CAST(substr(code,1,instr(code || '.', '.')-1) AS INTEGER) BETWEEN 10 AND 33 THEN 1 ELSE 0 END) AS has_manufacturing_okved,
                   MAX(CASE WHEN (
                        CAST(substr(code,1,instr(code || '.', '.')-1) AS INTEGER) BETWEEN 1 AND 3 OR
                        CAST(substr(code,1,instr(code || '.', '.')-1) AS INTEGER) BETWEEN 5 AND 33 OR
                        CAST(substr(code,1,instr(code || '.', '.')-1) AS INTEGER) = 35 OR
                        CAST(substr(code,1,instr(code || '.', '.')-1) AS INTEGER) BETWEEN 36 AND 39
                   ) THEN 1 ELSE 0 END) AS has_production_okved,
                   MAX(CASE WHEN CAST(substr(code,1,instr(code || '.', '.')-1) AS INTEGER) BETWEEN 45 AND 47 THEN 1 ELSE 0 END) AS has_trade_okved
            FROM okved_codes
            GROUP BY inn
        )
    """

    joins = f"""
        FROM organizations o
        LEFT JOIN organization_msp m ON m.inn=o.inn
        LEFT JOIN target_financial tf ON tf.inn=o.inn
        LEFT JOIN supplier_activity_all saa ON saa.inn=o.inn
        LEFT JOIN supplier_activity_year say ON say.inn=o.inn AND say.year={activity_year_expr}
        LEFT JOIN gisp_evidence ge ON ge.inn=o.inn
        LEFT JOIN fsa_evidence fe ON fe.inn=o.inn
        LEFT JOIN okved_flags ofl ON ofl.inn=o.inn
    """

    total = int(
        conn.execute(
            base_cte + f"SELECT COUNT(*) {joins} {where_sql}", params
        ).fetchone()[0]
    )
    order_expr = "COALESCE(say.supplier_contract_value_rub, 0)" if year is not None else "COALESCE(saa.supplier_contract_value_rub, 0)"
    raw_rows = [
        dict(row)
        for row in conn.execute(
            base_cte
            + f"""
            SELECT o.inn, o.name, o.entity_type, o.region_code, o.okved_main,
                   m.category_name AS msp_category, m.employee_count,
                   tf.year AS financial_year, tf.revenue, tf.expenses, tf.profit,
                   {activity_year_expr} AS activity_year,
                   COALESCE(saa.supplier_contracts, 0) AS supplier_contracts_all,
                   COALESCE(saa.supplier_contract_value_rub, 0) AS supplier_contract_value_all_rub,
                   COALESCE(say.supplier_contracts, 0) AS supplier_contracts_year,
                   COALESCE(say.supplier_contract_value_rub, 0) AS supplier_contract_value_year_rub,
                   COALESCE(saa.supplier_contracts, 0) AS supplier_contracts,
                   COALESCE(saa.supplier_contract_value_rub, 0) AS supplier_contract_value_rub,
                   COALESCE(ge.gisp_products, 0) AS gisp_products,
                   COALESCE(ge.gisp_okpd2_codes, 0) AS gisp_okpd2_codes,
                   COALESCE(ge.gisp_tnved_codes, 0) AS gisp_tnved_codes,
                   COALESCE(fe.fsa_certificates, 0) AS fsa_manufacturer_certificates,
                   COALESCE(ofl.has_manufacturing_okved, 0) AS has_manufacturing_okved,
                   COALESCE(ofl.has_production_okved, 0) AS has_production_okved,
                   COALESCE(ofl.has_trade_okved, 0) AS has_trade_okved
            {joins}
            {where_sql}
            ORDER BY {order_expr} DESC, o.inn
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
    ]

    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        gisp_products = int(row["gisp_products"] or 0)
        fsa_certificates = int(row["fsa_manufacturer_certificates"] or 0)
        has_manufacturing = bool(row["has_manufacturing_okved"])
        has_production = bool(row["has_production_okved"])
        source, confidence, classification = _manufacturer_labels(
            gisp_products=gisp_products,
            fsa_certificates=fsa_certificates,
            main_okved=row.get("okved_main"),
            has_manufacturing_okved=has_manufacturing,
            has_production_okved=has_production,
        )
        row["has_manufacturing_okved"] = has_manufacturing
        row["has_production_okved"] = has_production
        row["has_trade_okved"] = bool(row["has_trade_okved"])
        row["main_okved_profile"] = _okved_profile(row.get("okved_main"))
        row["manufacturer_source"] = source
        row["manufacturer_evidence_count"] = gisp_products + fsa_certificates
        row["manufacturer_confidence"] = confidence
        row["manufacturer_classification"] = classification
        revenue = row.get("revenue")
        if (
            row.get("activity_year") is not None
            and row.get("financial_year") == row.get("activity_year")
            and revenue is not None
            and float(revenue) > 0
        ):
            row["supplier_contract_value_to_revenue_ratio_year"] = round(
                float(row["supplier_contract_value_year_rub"] or 0) / float(revenue), 6
            )
        else:
            row["supplier_contract_value_to_revenue_ratio_year"] = None
        rows.append(row)

    return {
        "filters": {
            "msp_only": msp_only,
            "financials_only": financials_only,
            "role": role,
            "region_code": region_code,
            "year": year,
            "manufacturer_source": manufacturer_source,
        },
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }

def organization_profile(conn: sqlite3.Connection, inn: str) -> dict[str, Any] | None:
    normalized = normalize_inn(inn)
    if normalized is None:
        raise ValueError("INN must contain 10 or 12 digits")
    organization = conn.execute("SELECT * FROM organizations WHERE inn=?", (normalized,)).fetchone()
    if organization is None:
        return None

    msp = conn.execute("SELECT * FROM organization_msp WHERE inn=?", (normalized,)).fetchone()
    roles = [
        dict(row)
        for row in conn.execute(
            "SELECT role, first_seen_at, last_seen_at FROM organization_roles WHERE inn=? ORDER BY role",
            (normalized,),
        )
    ]
    okved = [
        dict(row)
        for row in conn.execute(
            """
            SELECT source, code, name, is_main, snapshot_date
            FROM organization_okved WHERE inn=? ORDER BY is_main DESC, code
            """,
            (normalized,),
        )
    ]
    financials = [
        dict(row)
        for row in conn.execute(
            """
            SELECT year, revenue, expenses, profit, source, snapshot_date
            FROM organization_financials WHERE inn=? ORDER BY year DESC, source
            """,
            (normalized,),
        )
    ]
    activity = conn.execute(
        """
        SELECT
            (SELECT COUNT(DISTINCT pp.purchase_id) FROM purchase_parties pp
             WHERE pp.inn=? AND pp.role='customer') AS customer_purchases,
            (SELECT COUNT(*) FROM contracts c WHERE c.customer_inn=?) AS customer_contracts,
            (SELECT COUNT(*) FROM contract_suppliers cs WHERE cs.inn=?) AS supplier_contracts,
            (SELECT COALESCE(SUM(c.price / NULLIF((SELECT COUNT(*) FROM contract_suppliers x WHERE x.contract_id=c.id), 0)), 0)
             FROM contracts c JOIN contract_suppliers cs ON cs.contract_id=c.id WHERE cs.inn=?) AS supplier_contract_value_rub,
            (SELECT COUNT(*) FROM gisp_products g WHERE g.manufacturer_inn=?) AS gisp_products,
            (SELECT COUNT(*) FROM fsa_certificates f WHERE f.applicant_inn=?) AS fsa_applicant_certificates,
            (SELECT COUNT(*) FROM fsa_certificates f WHERE f.manufacturer_inn=?) AS fsa_manufacturer_certificates
        """,
        (normalized,) * 7,
    ).fetchone()
    return {
        "organization": dict(organization),
        "roles": roles,
        "msp": dict(msp) if msp is not None else None,
        "okved": okved,
        "financials": financials,
        "activity": dict(activity) if activity is not None else {},
        "activity_by_year": organization_activity_by_year(
            conn, normalized, financials=financials
        ),
        "manufacturer": manufacturer_intelligence(conn, normalized),
    }


def organization_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "organizations": int(conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]),
        "roles": int(conn.execute("SELECT COUNT(*) FROM organization_roles").fetchone()[0]),
        "msp": int(conn.execute("SELECT COUNT(*) FROM organization_msp").fetchone()[0]),
        "msp_with_employees": int(
            conn.execute("SELECT COUNT(*) FROM organization_msp WHERE employee_count IS NOT NULL").fetchone()[0]
        ),
        "okved": int(conn.execute("SELECT COUNT(*) FROM organization_okved").fetchone()[0]),
        "financial_rows": int(conn.execute("SELECT COUNT(*) FROM organization_financials").fetchone()[0]),
        "financial_organizations": int(
            conn.execute("SELECT COUNT(DISTINCT inn) FROM organization_financials").fetchone()[0]
        ),
        "import_status_counts": {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM organization_import_runs GROUP BY status"
            )
        },
        "latest_imports": [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, source, source_path, started_at, completed_at, status,
                       files_seen, files_failed, rows_seen, rows_imported, rows_skipped,
                       filter_json, error
                FROM organization_import_runs ORDER BY id DESC LIMIT 10
                """
            )
        ],
    }
