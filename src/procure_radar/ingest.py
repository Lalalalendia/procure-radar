from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import insert_raw
from .extract import (
    external_id,
    extract_purchase,
    extract_purchase_items,
    has_full_purchase_documents,
)


def _merge_children(conn: sqlite3.Connection, purchase_id: int, parsed: dict[str, Any]) -> None:
    # The list and detail endpoints do not expose identical aggregate fields.
    # Never erase previously observed non-empty children just because a richer
    # detail document omitted that aggregate projection.
    for role, values in (("customer", parsed["customers"]), ("owner", parsed["owners"])):
        if values:
            conn.execute(
                "DELETE FROM purchase_parties WHERE purchase_id=? AND role=?",
                (purchase_id, role),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO purchase_parties(purchase_id, role, inn) VALUES (?, ?, ?)",
                [(purchase_id, role, inn) for inn in values],
            )

    for system, values in (("okpd2", parsed["okpd2"]), ("ktru", parsed["ktru"])):
        if values:
            conn.execute(
                "DELETE FROM purchase_codes WHERE purchase_id=? AND system=?",
                (purchase_id, system),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO purchase_codes(purchase_id, system, code) VALUES (?, ?, ?)",
                [(purchase_id, system, code) for code in values],
            )

    for kind, values in (
        ("ikz", parsed["ikzs"]),
        ("plan_number", parsed["plan_numbers"]),
        ("position_number", parsed["position_numbers"]),
    ):
        if values:
            conn.execute(
                "DELETE FROM purchase_links WHERE purchase_id=? AND kind=?",
                (purchase_id, kind),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO purchase_links(purchase_id, kind, value) VALUES (?, ?, ?)",
                [(purchase_id, kind, value) for value in values],
            )

    if parsed["docs"]:
        conn.execute("DELETE FROM purchase_documents WHERE purchase_id=?", (purchase_id,))
        conn.executemany(
            "INSERT INTO purchase_documents(purchase_id, doc_type, published_at) VALUES (?, ?, ?)",
            [
                (purchase_id, doc.get("doc_type"), doc.get("published_at"))
                for doc in parsed["docs"]
            ],
        )


def ingest_purchase(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    parsed = extract_purchase(payload)
    purchase_number = parsed["purchase_number"]
    if not purchase_number:
        raise ValueError("purchase_number is required for /fz44/purchases rows")

    raw_id = insert_raw(conn, payload, external_id(payload))
    conn.execute(
        """
        INSERT INTO purchases(
            raw_document_id, purchase_number, published_at, collecting_finished_at,
            doc_created_at, doc_updated_at, updated_at, max_price, currency_code,
            object_info, purchase_type, region_code, stage, responsible_inn,
            contract_guarantee_amount, contract_guarantee_part
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(purchase_number) DO UPDATE SET
            raw_document_id=excluded.raw_document_id,
            published_at=COALESCE(excluded.published_at, purchases.published_at),
            collecting_finished_at=COALESCE(excluded.collecting_finished_at, purchases.collecting_finished_at),
            doc_created_at=COALESCE(excluded.doc_created_at, purchases.doc_created_at),
            doc_updated_at=COALESCE(excluded.doc_updated_at, purchases.doc_updated_at),
            updated_at=COALESCE(excluded.updated_at, purchases.updated_at),
            max_price=COALESCE(excluded.max_price, purchases.max_price),
            currency_code=COALESCE(excluded.currency_code, purchases.currency_code),
            object_info=COALESCE(excluded.object_info, purchases.object_info),
            purchase_type=COALESCE(excluded.purchase_type, purchases.purchase_type),
            region_code=COALESCE(excluded.region_code, purchases.region_code),
            stage=COALESCE(excluded.stage, purchases.stage),
            responsible_inn=COALESCE(excluded.responsible_inn, purchases.responsible_inn),
            contract_guarantee_amount=COALESCE(excluded.contract_guarantee_amount, purchases.contract_guarantee_amount),
            contract_guarantee_part=COALESCE(excluded.contract_guarantee_part, purchases.contract_guarantee_part)
        """,
        (
            raw_id,
            purchase_number,
            parsed["published_at"],
            parsed["collecting_finished_at"],
            parsed["doc_created_at"],
            parsed["doc_updated_at"],
            parsed["updated_at"],
            parsed["max_price"],
            parsed["currency_code"],
            parsed["object_info"],
            parsed["purchase_type"],
            parsed["region_code"],
            parsed["stage"],
            parsed["responsible_inn"],
            parsed["contract_guarantee_amount"],
            parsed["contract_guarantee_part"],
        ),
    )
    row = conn.execute(
        "SELECT id FROM purchases WHERE purchase_number=?", (purchase_number,)
    ).fetchone()
    assert row is not None
    purchase_id = int(row["id"])
    _merge_children(conn, purchase_id, parsed)

    # Full EIS purchase documents often contain richer customer identity data
    # than the list projection. Keep the canonical organization graph warm so
    # locality filters can work by evidence-backed INN/address rather than by
    # brittle keyword searches in the purchase title.
    from .party_intelligence import enrich_organizations_from_payload

    enrich_organizations_from_payload(
        conn,
        payload=payload,
        inns=[*parsed["customers"], *parsed["owners"]],
        region_code=parsed["region_code"],
    )

    if has_full_purchase_documents(payload):
        items = extract_purchase_items(payload)
        if items:
            conn.execute("DELETE FROM purchase_items WHERE purchase_id=?", (purchase_id,))
        conn.executemany(
            """
            INSERT INTO purchase_items(
                purchase_id, item_key, source_doc_type, name, ktru_code, ktru_name,
                okpd2_code, okpd2_name, okei_code, unit_name, quantity, unit_price,
                amount, is_medical_product, characteristics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(purchase_id, item_key) DO UPDATE SET
                source_doc_type=excluded.source_doc_type,
                name=excluded.name,
                ktru_code=excluded.ktru_code,
                ktru_name=excluded.ktru_name,
                okpd2_code=excluded.okpd2_code,
                okpd2_name=excluded.okpd2_name,
                okei_code=excluded.okei_code,
                unit_name=excluded.unit_name,
                quantity=excluded.quantity,
                unit_price=excluded.unit_price,
                amount=excluded.amount,
                is_medical_product=excluded.is_medical_product,
                characteristics_json=excluded.characteristics_json
            """,
            [
                (
                    purchase_id,
                    item["item_key"],
                    item["source_doc_type"],
                    item["name"],
                    item["ktru_code"],
                    item["ktru_name"],
                    item["okpd2_code"],
                    item["okpd2_name"],
                    item["okei_code"],
                    item["unit_name"],
                    item["quantity"],
                    item["unit_price"],
                    item["amount"],
                    None if item["is_medical_product"] is None else (1 if item["is_medical_product"] else 0),
                    json.dumps(item["characteristics"], ensure_ascii=False, separators=(",", ":"))
                    if item["characteristics"] is not None
                    else None,
                )
                for item in items
            ],
        )
    return purchase_id


def ingest_procedure(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    from .extract import extract_procedure, procedure_external_id

    parsed = extract_procedure(payload)
    key = parsed["contract_project_number"]
    if not key:
        raise ValueError("contract_project_number is required for /fz44/procedures rows")

    raw_id = insert_raw(
        conn,
        payload,
        procedure_external_id(payload),
        endpoint="/fz44/procedures",
    )
    conn.execute(
        """
        INSERT INTO contract_procedures(
            raw_document_id, contract_project_number, purchase_number, customer_inn,
            participant_inn, price, currency_code, region_code, subject, published_at,
            doc_created_at, doc_updated_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contract_project_number) DO UPDATE SET
            raw_document_id=excluded.raw_document_id,
            purchase_number=excluded.purchase_number,
            customer_inn=excluded.customer_inn,
            participant_inn=excluded.participant_inn,
            price=excluded.price,
            currency_code=excluded.currency_code,
            region_code=excluded.region_code,
            subject=excluded.subject,
            published_at=excluded.published_at,
            doc_created_at=excluded.doc_created_at,
            doc_updated_at=excluded.doc_updated_at,
            updated_at=excluded.updated_at
        """,
        (
            raw_id,
            key,
            parsed["purchase_number"],
            parsed["customer_inn"],
            parsed["participant_inn"],
            parsed["price"],
            parsed["currency_code"],
            parsed["region_code"],
            parsed["subject"],
            parsed["published_at"],
            parsed["doc_created_at"],
            parsed["doc_updated_at"],
            parsed["updated_at"],
        ),
    )
    row = conn.execute(
        "SELECT id FROM contract_procedures WHERE contract_project_number=?", (key,)
    ).fetchone()
    assert row is not None
    procedure_id = int(row["id"])
    conn.execute("DELETE FROM procedure_documents WHERE procedure_id=?", (procedure_id,))
    conn.executemany(
        "INSERT INTO procedure_documents(procedure_id, doc_type, published_at) VALUES (?, ?, ?)",
        [(procedure_id, d.get("doc_type"), d.get("published_at")) for d in parsed["docs"]],
    )
    return procedure_id


def ingest_contract(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    from .extract import contract_external_id, extract_contract

    parsed = extract_contract(payload)
    reg_num = parsed["reg_num"]
    if not reg_num:
        raise ValueError("reg_num is required for /fz44/contracts rows")

    raw_id = insert_raw(
        conn,
        payload,
        contract_external_id(payload),
        endpoint="/fz44/contracts",
    )
    conn.execute(
        """
        INSERT INTO contracts(
            raw_document_id, reg_num, purchase_number, customer_inn, price,
            currency_code, region_code, stage, subject, plan_number, position_number,
            exe_start, exe_end, elact_at, published_at, doc_created_at, doc_updated_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(reg_num) DO UPDATE SET
            raw_document_id=excluded.raw_document_id,
            purchase_number=excluded.purchase_number,
            customer_inn=excluded.customer_inn,
            price=excluded.price,
            currency_code=excluded.currency_code,
            region_code=excluded.region_code,
            stage=excluded.stage,
            subject=excluded.subject,
            plan_number=excluded.plan_number,
            position_number=excluded.position_number,
            exe_start=excluded.exe_start,
            exe_end=excluded.exe_end,
            elact_at=excluded.elact_at,
            published_at=excluded.published_at,
            doc_created_at=excluded.doc_created_at,
            doc_updated_at=excluded.doc_updated_at,
            updated_at=excluded.updated_at
        """,
        (
            raw_id,
            reg_num,
            parsed["purchase_number"],
            parsed["customer_inn"],
            parsed["price"],
            parsed["currency_code"],
            parsed["region_code"],
            parsed["stage"],
            parsed["subject"],
            parsed["plan_number"],
            parsed["position_number"],
            parsed["exe_start"],
            parsed["exe_end"],
            parsed["elact_at"],
            parsed["published_at"],
            parsed["doc_created_at"],
            parsed["doc_updated_at"],
            parsed["updated_at"],
        ),
    )
    row = conn.execute("SELECT id FROM contracts WHERE reg_num=?", (reg_num,)).fetchone()
    assert row is not None
    contract_id = int(row["id"])

    conn.execute("DELETE FROM contract_suppliers WHERE contract_id=?", (contract_id,))
    conn.execute("DELETE FROM contract_codes WHERE contract_id=?", (contract_id,))
    conn.execute("DELETE FROM contract_documents WHERE contract_id=?", (contract_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO contract_suppliers(contract_id, inn) VALUES (?, ?)",
        [(contract_id, inn) for inn in parsed["suppliers"]],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO contract_codes(contract_id, system, code) VALUES (?, 'okpd2', ?)",
        [(contract_id, code) for code in parsed["okpd2"]],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO contract_codes(contract_id, system, code) VALUES (?, 'ktru', ?)",
        [(contract_id, code) for code in parsed["ktru"]],
    )
    conn.executemany(
        "INSERT INTO contract_documents(contract_id, doc_type, published_at) VALUES (?, ?, ?)",
        [(contract_id, d.get("doc_type"), d.get("published_at")) for d in parsed["docs"]],
    )

    # Contract raw/full documents can contain canonical party names even when
    # the aggregate fields only expose INNs. Keep the central organization
    # graph warm without overwriting names learned from stronger sources.
    from .party_intelligence import enrich_contract_parties_from_payload

    enrich_contract_parties_from_payload(
        conn,
        payload=payload,
        customer_inn=parsed["customer_inn"],
        supplier_inns=parsed["suppliers"],
        region_code=parsed["region_code"],
    )
    return contract_id


def ingest_tender_protocol(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    from .extract import extract_tender_protocol, protocol_external_id

    parsed = extract_tender_protocol(payload)
    protocol_key = parsed["protocol_key"]
    purchase_number = parsed["purchase_number"]
    if not protocol_key:
        raise ValueError("protocol key is required for tender protocol rows")
    if not purchase_number:
        raise ValueError("purchase_number is required for tender protocol rows")

    raw_id = insert_raw(
        conn,
        payload,
        protocol_external_id(payload),
        endpoint=f"/fz44/purchases/{purchase_number}/protocols",
    )
    conn.execute(
        """
        INSERT INTO tender_protocols(
            raw_document_id, protocol_key, purchase_number, doc_type, published_at,
            source_id, source_external_id, version_number, procedure_at,
            applications_count, admitted_count, rejected_count, final_price,
            is_abandoned, abandoned_reason_code, abandoned_reason_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(protocol_key) DO UPDATE SET
            raw_document_id=excluded.raw_document_id,
            purchase_number=excluded.purchase_number,
            doc_type=excluded.doc_type,
            published_at=excluded.published_at,
            source_id=excluded.source_id,
            source_external_id=excluded.source_external_id,
            version_number=excluded.version_number,
            procedure_at=excluded.procedure_at,
            applications_count=excluded.applications_count,
            admitted_count=excluded.admitted_count,
            rejected_count=excluded.rejected_count,
            final_price=excluded.final_price,
            is_abandoned=excluded.is_abandoned,
            abandoned_reason_code=excluded.abandoned_reason_code,
            abandoned_reason_name=excluded.abandoned_reason_name
        """,
        (
            raw_id,
            protocol_key,
            purchase_number,
            parsed["doc_type"],
            parsed["published_at"],
            parsed["source_id"],
            parsed["source_external_id"],
            parsed["version_number"],
            parsed["procedure_at"],
            parsed["applications_count"],
            parsed["admitted_count"],
            parsed["rejected_count"],
            parsed["final_price"],
            1 if parsed["is_abandoned"] else 0,
            parsed["abandoned_reason_code"],
            parsed["abandoned_reason_name"],
        ),
    )
    row = conn.execute(
        "SELECT id FROM tender_protocols WHERE protocol_key=?", (protocol_key,)
    ).fetchone()
    assert row is not None
    protocol_id = int(row["id"])

    conn.execute("DELETE FROM tender_applications WHERE protocol_id=?", (protocol_id,))
    conn.executemany(
        """
        INSERT INTO tender_applications(
            protocol_id, app_number, app_at, final_price, admitted, app_rating
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                protocol_id,
                app["app_number"],
                app["app_at"],
                app["final_price"],
                None if app["admitted"] is None else (1 if app["admitted"] else 0),
                app["app_rating"],
            )
            for app in parsed["applications"]
        ],
    )
    return protocol_id


def ingest_tenderplan(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    raw_endpoint: str = "/fz44/tenderplans",
    preserve_existing_positions: bool = False,
) -> int:
    """Persist one 44-FZ plan-schedule aggregate/detail document.

    Full tenderPlan2020 documents are deliberately normalized into positions and
    codes so forward-opportunity queries do not need to repeatedly walk raw JSON.
    Re-ingestion is idempotent and replaces the child position snapshot for the
    same plan number.
    """
    from .extract import extract_tenderplan, tenderplan_external_id

    parsed = extract_tenderplan(payload)
    plan_number = parsed["plan_number"]
    if not plan_number:
        raise ValueError("plan_number is required for /fz44/tenderplans rows")

    raw_id = insert_raw(
        conn,
        payload,
        tenderplan_external_id(payload),
        endpoint=raw_endpoint,
    )
    conn.execute(
        """
        INSERT INTO tenderplans(
            raw_document_id, plan_number, published_at, plan_year, region_code, updated_at
        ) VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(plan_number) DO UPDATE SET
            raw_document_id=excluded.raw_document_id,
            published_at=COALESCE(excluded.published_at, tenderplans.published_at),
            plan_year=COALESCE(excluded.plan_year, tenderplans.plan_year),
            region_code=COALESCE(excluded.region_code, tenderplans.region_code),
            updated_at=datetime('now')
        """,
        (
            raw_id,
            plan_number,
            parsed["published_at"],
            parsed["year"],
            parsed["region_code"],
        ),
    )
    row = conn.execute(
        "SELECT id FROM tenderplans WHERE plan_number=?", (plan_number,)
    ).fetchone()
    assert row is not None
    tenderplan_id = int(row["id"])

    if parsed["customer_inns"]:
        conn.execute("DELETE FROM tenderplan_customers WHERE tenderplan_id=?", (tenderplan_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO tenderplan_customers(tenderplan_id, inn) VALUES (?, ?)",
            [(tenderplan_id, inn) for inn in parsed["customer_inns"]],
        )
        for inn in parsed["customer_inns"]:
            conn.execute("INSERT OR IGNORE INTO organizations(inn) VALUES (?)", (inn,))
            conn.execute(
                "INSERT OR IGNORE INTO organization_roles(inn, role) VALUES (?, 'customer')",
                (inn,),
            )

    # Full/detail rows are authoritative for the current plan snapshot. Aggregate
    # rows with no positions must not erase details already fetched earlier.
    existing_position_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM tenderplan_positions WHERE tenderplan_id=?",
            (tenderplan_id,),
        ).fetchone()[0]
    )
    should_replace_positions = bool(parsed["positions"]) and not (
        preserve_existing_positions and existing_position_count > 0
    )
    if should_replace_positions:
        conn.execute(
            "DELETE FROM tenderplan_positions WHERE tenderplan_id=?", (tenderplan_id,)
        )
        for position in parsed["positions"]:
            customer_inn = position["customer_inn"]
            if not customer_inn and len(parsed["customer_inns"]) == 1:
                customer_inn = parsed["customer_inns"][0]
            cur = conn.execute(
                """
                INSERT INTO tenderplan_positions(
                    tenderplan_id, position_key, position_number, ikz, customer_inn,
                    object_info, planned_at, planned_year, planned_month, amount, raw_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenderplan_id,
                    position["position_key"],
                    position["position_number"],
                    position["ikz"],
                    customer_inn,
                    position["object_info"],
                    position["planned_at"],
                    position["planned_year"] or parsed["year"],
                    position["planned_month"],
                    position["amount"],
                    position["raw_index"],
                ),
            )
            position_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT OR IGNORE INTO tenderplan_position_codes(position_id, system, code) VALUES (?, 'okpd2', ?)",
                [(position_id, code) for code in position["okpd2"]],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO tenderplan_position_codes(position_id, system, code) VALUES (?, 'ktru', ?)",
                [(position_id, code) for code in position["ktru"]],
            )
            if customer_inn:
                conn.execute("INSERT OR IGNORE INTO organizations(inn) VALUES (?)", (customer_inn,))
                conn.execute(
                    "INSERT OR IGNORE INTO organization_roles(inn, role) VALUES (?, 'customer')",
                    (customer_inn,),
                )

    if parsed["docs"]:
        conn.execute("DELETE FROM tenderplan_documents WHERE tenderplan_id=?", (tenderplan_id,))
        conn.executemany(
            "INSERT INTO tenderplan_documents(tenderplan_id, doc_type, published_at) VALUES (?, ?, ?)",
            [
                (tenderplan_id, doc.get("doc_type"), doc.get("published_at"))
                for doc in parsed["docs"]
            ],
        )
    return tenderplan_id
