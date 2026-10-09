from __future__ import annotations

from collections import defaultdict
import json
import sqlite3
from typing import Any

from .analytics import compute_buyer_supplier_relationships, compute_opportunities
from .contract_history import contract_history_status
from .history import history_status


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return row[0]


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:10] if text else None


def _latest_history(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    contracts: bool,
) -> dict[str, Any] | None:
    rows = (
        contract_history_status(conn, region_code=region_code)
        if contracts
        else [
            item
            for item in history_status(conn, region_code=region_code)
            if item.get("mode") != "contracts_published_time_shards_v1"
        ]
    )
    return rows[0] if rows else None




def _history_state(row: dict[str, Any] | None, *, connected: bool) -> str:
    if row:
        return "up_to_date" if int(row.get("completed") or 0) else "needs_resume"
    return "connected" if connected else "empty"


def _sync_state(row: dict[str, Any] | None, *, connected: bool) -> str:
    if row:
        return "up_to_date" if int(row.get("completed") or 0) else "needs_resume"
    return "connected" if connected else "empty"

def source_statuses(conn: sqlite3.Connection, *, region_code: int) -> list[dict[str, Any]]:
    purchases = int(
        _scalar(conn, "SELECT COUNT(*) FROM purchases WHERE region_code=?", (region_code,)) or 0
    )
    purchase_min = _date_text(
        _scalar(conn, "SELECT MIN(published_at) FROM purchases WHERE region_code=?", (region_code,))
    )
    purchase_max = _date_text(
        _scalar(conn, "SELECT MAX(published_at) FROM purchases WHERE region_code=?", (region_code,))
    )
    protocols = int(
        _scalar(
            conn,
            """
            SELECT COUNT(DISTINCT tp.purchase_number)
            FROM tender_protocols tp
            JOIN purchases p ON p.purchase_number=tp.purchase_number
            WHERE p.region_code=?
            """,
            (region_code,),
        )
        or 0
    )
    details = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM purchase_detail_fetches pdf
            JOIN purchases p ON p.purchase_number=pdf.purchase_number
            WHERE p.region_code=?
            """,
            (region_code,),
        )
        or 0
    )
    p_history = _latest_history(conn, region_code=region_code, contracts=False)

    contracts = int(
        _scalar(conn, "SELECT COUNT(*) FROM contracts WHERE region_code=?", (region_code,)) or 0
    )
    contract_min = _date_text(
        _scalar(conn, "SELECT MIN(published_at) FROM contracts WHERE region_code=?", (region_code,))
    )
    contract_max = _date_text(
        _scalar(conn, "SELECT MAX(published_at) FROM contracts WHERE region_code=?", (region_code,))
    )
    contract_suppliers = int(
        _scalar(
            conn,
            """
            SELECT COUNT(DISTINCT cs.inn)
            FROM contract_suppliers cs
            JOIN contracts c ON c.id=cs.contract_id
            WHERE c.region_code=?
            """,
            (region_code,),
        )
        or 0
    )
    linked_contracts = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM contracts c
            JOIN purchases p ON p.purchase_number=c.purchase_number
            WHERE c.region_code=? AND p.region_code=?
            """,
            (region_code, region_code),
        )
        or 0
    )
    c_history = _latest_history(conn, region_code=region_code, contracts=True)

    gisp_products = int(_scalar(conn, "SELECT COUNT(*) FROM gisp_products") or 0)
    gisp_mfr = int(
        _scalar(
            conn,
            "SELECT COUNT(DISTINCT manufacturer_inn) FROM gisp_products WHERE manufacturer_inn IS NOT NULL AND TRIM(manufacturer_inn)<>''",
        )
        or 0
    )
    gisp_max = _date_text(_scalar(conn, "SELECT MAX(registry_date) FROM gisp_products"))
    gisp_run_row = conn.execute(
        """
        SELECT source_path, source_scope, completed_at, rows_imported
        FROM gisp_import_runs
        WHERE completed_at IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    gisp_run = dict(gisp_run_row) if gisp_run_row else None

    rzn_products = int(_scalar(conn, "SELECT COUNT(*) FROM rzn_med_products") or 0)
    rzn_max = _date_text(_scalar(conn, "SELECT MAX(registration_date) FROM rzn_med_products"))
    rzn_sync = conn.execute(
        "SELECT * FROM rzn_sync_state ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    rzn_sync_d = dict(rzn_sync) if rzn_sync else None

    fsa_count = int(_scalar(conn, "SELECT COUNT(*) FROM fsa_certificates") or 0)
    fsa_max = _date_text(_scalar(conn, "SELECT MAX(reg_date) FROM fsa_certificates"))
    fsa_sync = conn.execute(
        "SELECT * FROM fsa_sync_state ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    fsa_sync_d = dict(fsa_sync) if fsa_sync else None

    return [
        {
            "key": "purchases",
            "title": "ГосПлан · закупки",
            "connected": purchases > 0,
            "state": _history_state(p_history, connected=purchases > 0),
            "count": purchases,
            "date_from": purchase_min,
            "date_to": purchase_max,
            "updated_at": p_history.get("updated_at") if p_history else None,
            "progress": _history_progress(p_history),
            "details": f"протоколы {protocols:,} · детали {details:,}".replace(",", " "),
            "history": p_history,
            "action": "continue_purchases",
        },
        {
            "key": "contracts",
            "title": "ГосПлан · контракты",
            "connected": contracts > 0,
            "state": _history_state(c_history, connected=contracts > 0),
            "count": contracts,
            "date_from": contract_min,
            "date_to": contract_max,
            "updated_at": c_history.get("updated_at") if c_history else None,
            "progress": _history_progress(c_history),
            "details": f"поставщики {contract_suppliers:,} · связаны {linked_contracts:,}".replace(",", " "),
            "history": c_history,
            "action": "continue_contracts",
        },
        {
            "key": "gisp",
            "title": "ГИСП · реестр российской продукции",
            "connected": gisp_products > 0,
            "state": "up_to_date" if gisp_products else "empty",
            "count": gisp_products,
            "date_from": None,
            "date_to": gisp_max,
            "updated_at": gisp_run.get("completed_at") if gisp_run else None,
            "progress": 1.0 if gisp_products else 0.0,
            "details": f"производители {gisp_mfr:,} · scope {(gisp_run or {}).get('source_scope', '—')}".replace(",", " "),
            "history": gisp_run,
            "action": "refresh_gisp" if gisp_run and gisp_run.get("source_path") else None,
        },
        {
            "key": "rzn",
            "title": "Росздравнадзор · медизделия",
            "connected": rzn_products > 0,
            "state": _sync_state(rzn_sync_d, connected=rzn_products > 0),
            "count": rzn_products,
            "date_from": None,
            "date_to": rzn_max,
            "updated_at": rzn_sync_d.get("updated_at") if rzn_sync_d else None,
            "progress": _sync_progress(rzn_sync_d),
            "details": _sync_details(rzn_sync_d),
            "history": rzn_sync_d,
            "action": "continue_rzn",
        },
        {
            "key": "fsa",
            "title": "ФСА · сертификаты соответствия",
            "connected": fsa_count > 0,
            "state": _sync_state(fsa_sync_d, connected=fsa_count > 0),
            "count": fsa_count,
            "date_from": None,
            "date_to": fsa_max,
            "updated_at": fsa_sync_d.get("updated_at") if fsa_sync_d else None,
            "progress": _sync_progress(fsa_sync_d),
            "details": _sync_details(fsa_sync_d),
            "history": fsa_sync_d,
            "action": "continue_fsa",
        },
    ]


def _history_progress(row: dict[str, Any] | None) -> float:
    if not row:
        return 0.0
    if int(row.get("completed") or 0):
        return 1.0
    since = _date_text(row.get("since_date"))
    until = _date_text(row.get("until_date"))
    oldest = _date_text(row.get("oldest_published_at"))
    if not since or not until or not oldest:
        return 0.05 if int(row.get("rows_seen") or 0) else 0.0
    try:
        from datetime import date

        lo = date.fromisoformat(since)
        hi = date.fromisoformat(until)
        cur = date.fromisoformat(oldest)
    except ValueError:
        return 0.0
    total = max(1, (hi - lo).days)
    done = max(0, min(total, (hi - cur).days))
    return max(0.02, min(0.99, done / total))


def _sync_progress(row: dict[str, Any] | None) -> float:
    if not row:
        return 0.0
    if int(row.get("completed") or 0):
        return 1.0
    total = int(row.get("total_elements") or 0)
    seen = int(row.get("rows_seen") or 0)
    if total > 0:
        return max(0.0, min(0.99, seen / total))
    return 0.05 if seen else 0.0


def _sync_details(row: dict[str, Any] | None) -> str:
    if not row:
        return "синхронизация ещё не запускалась"
    seen = int(row.get("rows_seen") or 0)
    total = row.get("total_elements")
    state = "завершено" if int(row.get("completed") or 0) else "можно продолжить"
    if total:
        return f"{state} · {seen:,}/{int(total):,}".replace(",", " ")
    return f"{state} · строк {seen:,}".replace(",", " ")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def dashboard_metrics(conn: sqlite3.Connection, *, region_code: int) -> dict[str, Any]:
    purchases = int(_scalar(conn, "SELECT COUNT(*) FROM purchases WHERE region_code=?", (region_code,)) or 0)
    contracts = int(_scalar(conn, "SELECT COUNT(*) FROM contracts WHERE region_code=?", (region_code,)) or 0)
    suppliers = int(
        _scalar(
            conn,
            "SELECT COUNT(DISTINCT cs.inn) FROM contract_suppliers cs JOIN contracts c ON c.id=cs.contract_id WHERE c.region_code=?",
            (region_code,),
        )
        or 0
    )
    buyers = int(
        _scalar(
            conn,
            "SELECT COUNT(DISTINCT customer_inn) FROM contracts WHERE region_code=? AND customer_inn IS NOT NULL",
            (region_code,),
        )
        or 0
    )
    linked = int(
        _scalar(
            conn,
            "SELECT COUNT(*) FROM contracts c JOIN purchases p ON p.purchase_number=c.purchase_number WHERE c.region_code=? AND p.region_code=?",
            (region_code, region_code),
        )
        or 0
    )
    protocol_purchases = int(
        _scalar(
            conn,
            "SELECT COUNT(DISTINCT tp.purchase_number) FROM tender_protocols tp JOIN purchases p ON p.purchase_number=tp.purchase_number WHERE p.region_code=?",
            (region_code,),
        )
        or 0
    )
    return {
        "purchases": purchases,
        "contracts": contracts,
        "suppliers": suppliers,
        "buyers": buyers,
        "contract_linkage": (linked / contracts) if contracts else 0.0,
        "protocol_coverage": (protocol_purchases / purchases) if purchases else 0.0,
        "purchase_date_to": _date_text(_scalar(conn, "SELECT MAX(published_at) FROM purchases WHERE region_code=?", (region_code,))),
        "contract_date_to": _date_text(_scalar(conn, "SELECT MAX(published_at) FROM contracts WHERE region_code=?", (region_code,))),
    }


def opportunity_rows(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    group: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = compute_opportunities(
        conn,
        region_code=region_code,
        min_procurements=2,
        min_protocol_coverage=0.5,
        group=group,
    )
    return rows[:limit]


def organization_rows(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    query: str | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    buyers: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        """
        SELECT customer_inn AS inn, COUNT(*) AS contracts,
               COUNT(DISTINCT purchase_number) AS purchases,
               COALESCE(SUM(price), 0) AS value
        FROM contracts
        WHERE region_code=? AND customer_inn IS NOT NULL AND TRIM(customer_inn)<>''
        GROUP BY customer_inn
        """,
        (region_code,),
    ):
        inn = str(row["inn"])
        buyers[inn] = {
            "inn": inn,
            "buyer_contracts": int(row["contracts"]),
            "buyer_purchases": int(row["purchases"]),
            "buyer_value": float(row["value"] or 0),
        }

    suppliers: dict[str, dict[str, Any]] = {}
    supplier_rows = conn.execute(
        """
        WITH supplier_counts AS (
            SELECT contract_id, COUNT(*) AS n
            FROM contract_suppliers
            GROUP BY contract_id
        )
        SELECT cs.inn AS inn,
               COUNT(DISTINCT c.id) AS contracts,
               COUNT(DISTINCT c.purchase_number) AS purchases,
               COALESCE(SUM(CASE WHEN c.price IS NULL THEN 0 ELSE c.price / sc.n END), 0) AS value
        FROM contract_suppliers cs
        JOIN contracts c ON c.id=cs.contract_id
        JOIN supplier_counts sc ON sc.contract_id=c.id
        WHERE c.region_code=?
        GROUP BY cs.inn
        """,
        (region_code,),
    ).fetchall()
    for row in supplier_rows:
        inn = str(row["inn"])
        suppliers[inn] = {
            "inn": inn,
            "supplier_contracts": int(row["contracts"]),
            "supplier_purchases": int(row["purchases"]),
            "supplier_value": float(row["value"] or 0),
        }

    all_inns = sorted(set(buyers) | set(suppliers))
    names = _organization_names(conn, all_inns)
    q = (query or "").strip().lower()
    result: list[dict[str, Any]] = []
    for inn in all_inns:
        item = {
            "inn": inn,
            "name": names.get(inn),
            "buyer_contracts": 0,
            "buyer_purchases": 0,
            "buyer_value": 0.0,
            "supplier_contracts": 0,
            "supplier_purchases": 0,
            "supplier_value": 0.0,
        }
        item.update(buyers.get(inn, {}))
        item.update(suppliers.get(inn, {}))
        roles = []
        if item["buyer_contracts"]:
            roles.append("заказчик")
        if item["supplier_contracts"]:
            roles.append("поставщик")
        item["role"] = " + ".join(roles)
        item["contracts"] = item["buyer_contracts"] + item["supplier_contracts"]
        item["value"] = item["buyer_value"] + item["supplier_value"]
        if q and q not in inn.lower() and q not in (item.get("name") or "").lower():
            continue
        result.append(item)
    result.sort(key=lambda r: (float(r["value"]), int(r["contracts"])), reverse=True)
    return result[:limit]


def _organization_names(conn: sqlite3.Connection, inns: list[str]) -> dict[str, str]:
    if not inns:
        return {}
    names: dict[str, str] = {}
    chunks = [inns[i : i + 500] for i in range(0, len(inns), 500)]
    for chunk in chunks:
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT manufacturer_inn AS inn, manufacturer_name AS name, COUNT(*) AS n
            FROM gisp_products
            WHERE manufacturer_inn IN ({placeholders})
              AND manufacturer_name IS NOT NULL AND TRIM(manufacturer_name)<>''
            GROUP BY manufacturer_inn, manufacturer_name
            ORDER BY n DESC
            """,
            chunk,
        ):
            names.setdefault(str(row["inn"]), str(row["name"]))
        for row in conn.execute(
            f"""
            SELECT applicant_inn AS inn, applicant_name AS name
            FROM fsa_certificates
            WHERE applicant_inn IN ({placeholders})
              AND applicant_name IS NOT NULL AND TRIM(applicant_name)<>''
            UNION ALL
            SELECT manufacturer_inn AS inn, manufacturer_name AS name
            FROM fsa_certificates
            WHERE manufacturer_inn IN ({placeholders})
              AND manufacturer_name IS NOT NULL AND TRIM(manufacturer_name)<>''
            """,
            chunk + chunk,
        ):
            names.setdefault(str(row["inn"]), str(row["name"]))
    return names


def organization_names(conn: sqlite3.Connection, inns: list[str]) -> dict[str, str]:
    """Resolve display names only for the organizations visible in the UI."""
    return _organization_names(conn, inns)


def organization_card(conn: sqlite3.Connection, *, region_code: int, inn: str) -> dict[str, Any]:
    """Load one organization card with targeted SQL instead of a full market scan."""
    inn = str(inn).strip()
    buyer = conn.execute(
        """
        SELECT COUNT(*) AS contracts,
               COUNT(DISTINCT purchase_number) AS purchases,
               COALESCE(SUM(price), 0) AS value
        FROM contracts
        WHERE region_code=? AND customer_inn=?
        """,
        (region_code, inn),
    ).fetchone()
    supplier = conn.execute(
        """
        WITH supplier_counts AS (
            SELECT contract_id, COUNT(*) AS n
            FROM contract_suppliers
            GROUP BY contract_id
        )
        SELECT COUNT(DISTINCT c.id) AS contracts,
               COUNT(DISTINCT c.purchase_number) AS purchases,
               COALESCE(SUM(CASE WHEN c.price IS NULL THEN 0 ELSE c.price / sc.n END), 0) AS value
        FROM contracts c
        JOIN contract_suppliers cs ON cs.contract_id=c.id
        JOIN supplier_counts sc ON sc.contract_id=c.id
        WHERE c.region_code=? AND cs.inn=?
        """,
        (region_code, inn),
    ).fetchone()

    buyer_contracts = int(buyer["contracts"] or 0) if buyer else 0
    supplier_contracts = int(supplier["contracts"] or 0) if supplier else 0
    buyer_value = float(buyer["value"] or 0.0) if buyer else 0.0
    supplier_value = float(supplier["value"] or 0.0) if supplier else 0.0
    roles: list[str] = []
    if buyer_contracts:
        roles.append("заказчик")
    if supplier_contracts:
        roles.append("поставщик")

    partners: list[dict[str, Any]] = []
    if buyer_contracts:
        for row in conn.execute(
            """
            WITH supplier_counts AS (
                SELECT contract_id, COUNT(*) AS n
                FROM contract_suppliers
                GROUP BY contract_id
            )
            SELECT cs.inn AS inn,
                   COUNT(DISTINCT c.id) AS contracts,
                   COALESCE(SUM(CASE WHEN c.price IS NULL THEN 0 ELSE c.price / sc.n END), 0) AS value
            FROM contracts c
            JOIN supplier_counts sc ON sc.contract_id=c.id
            JOIN contract_suppliers cs ON cs.contract_id=c.id
            WHERE c.region_code=? AND c.customer_inn=?
            GROUP BY cs.inn
            """,
            (region_code, inn),
        ):
            value = float(row["value"] or 0.0)
            partners.append(
                {
                    "inn": str(row["inn"]),
                    "direction": "поставщик",
                    "contracts": int(row["contracts"] or 0),
                    "value": value,
                    "share": (value / buyer_value) if buyer_value > 0 else 0.0,
                }
            )

    if supplier_contracts:
        for row in conn.execute(
            """
            WITH supplier_counts AS (
                SELECT contract_id, COUNT(*) AS n
                FROM contract_suppliers
                GROUP BY contract_id
            )
            SELECT c.customer_inn AS inn,
                   COUNT(DISTINCT c.id) AS contracts,
                   COALESCE(SUM(CASE WHEN c.price IS NULL THEN 0 ELSE c.price / sc.n END), 0) AS value
            FROM contracts c
            JOIN supplier_counts sc ON sc.contract_id=c.id
            JOIN contract_suppliers cs ON cs.contract_id=c.id
            WHERE c.region_code=? AND cs.inn=?
              AND c.customer_inn IS NOT NULL AND TRIM(c.customer_inn)<>''
            GROUP BY c.customer_inn
            """,
            (region_code, inn),
        ):
            value = float(row["value"] or 0.0)
            partners.append(
                {
                    "inn": str(row["inn"]),
                    "direction": "заказчик",
                    "contracts": int(row["contracts"] or 0),
                    "value": value,
                    "share": (value / supplier_value) if supplier_value > 0 else 0.0,
                }
            )

    partners.sort(key=lambda item: (float(item["value"]), int(item["contracts"])), reverse=True)
    names = _organization_names(conn, [inn] + [str(item["inn"]) for item in partners[:20]])
    for item in partners:
        item["name"] = names.get(str(item["inn"]))

    return {
        "inn": inn,
        "name": names.get(inn),
        "role": " + ".join(roles) if roles else "—",
        "buyer_contracts": buyer_contracts,
        "buyer_purchases": int(buyer["purchases"] or 0) if buyer else 0,
        "buyer_value": buyer_value,
        "supplier_contracts": supplier_contracts,
        "supplier_purchases": int(supplier["purchases"] or 0) if supplier else 0,
        "supplier_value": supplier_value,
        "contracts": buyer_contracts + supplier_contracts,
        "value": buyer_value + supplier_value,
        "partners": partners[:10],
    }


def relationship_rows(
    conn: sqlite3.Connection,
    *,
    region_code: int,
    min_contracts: int = 2,
    min_value_rub: float = 100_000.0,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return compute_buyer_supplier_relationships(
        conn,
        region_code=region_code,
        min_contracts=min_contracts,
        min_value_rub=min_value_rub,
    )[:limit]


def recent_purchases(conn: sqlite3.Connection, *, region_code: int, limit: int = 40) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT purchase_number, published_at, max_price, stage, object_info
            FROM purchases
            WHERE region_code=?
            ORDER BY published_at DESC, id DESC
            LIMIT ?
            """,
            (region_code, limit),
        ).fetchall()
    ]
