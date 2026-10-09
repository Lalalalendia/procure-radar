from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .deal_screening import locality_deal_shortlist


def locality_deal_dossiers(
    conn: sqlite3.Connection,
    *,
    locality_key: str,
    min_confidence: float = 0.9,
    min_score: float = 68.0,
    max_deals: int = 10,
) -> list[dict[str, Any]]:
    shortlist = locality_deal_shortlist(
        conn,
        locality_key=locality_key,
        min_confidence=min_confidence,
        min_score=min_score,
        limit=max(100, max_deals * 10),
    )
    dossiers: list[dict[str, Any]] = []
    for row in shortlist:
        if not row.get("eligible") or row.get("decision") == "skip":
            continue
        purchase = conn.execute(
            "SELECT id, contract_guarantee_amount, contract_guarantee_part FROM purchases WHERE purchase_number=?",
            (row["purchase_number"],),
        ).fetchone()
        if purchase is None:
            continue
        items = [
            dict(x)
            for x in conn.execute(
                """
                SELECT item_key, name, ktru_code, ktru_name, okpd2_code, okpd2_name,
                       okei_code, unit_name, quantity, unit_price, amount,
                       is_medical_product, characteristics_json
                FROM purchase_items
                WHERE purchase_id=?
                ORDER BY id
                """,
                (int(purchase["id"]),),
            )
        ]
        docs = [
            dict(x)
            for x in conn.execute(
                "SELECT doc_type, published_at FROM purchase_documents WHERE purchase_id=? ORDER BY published_at, id",
                (int(purchase["id"]),),
            )
        ]
        dossier = dict(row)
        dossier["contract_guarantee_amount"] = purchase["contract_guarantee_amount"]
        dossier["contract_guarantee_part"] = purchase["contract_guarantee_part"]
        dossier["items"] = items
        dossier["documents"] = docs
        dossier["next_work_units"] = [
            "Find at least 3 independent suppliers for every material line item",
            "Confirm stock and delivery lead time",
            "Collect VAT-inclusive purchase prices and delivery/assembly cost",
            "Check certificates, declarations, licenses and product-equivalence constraints",
            "Check bid/contract guarantees and payment terms",
            "Calculate landed cost, cash gap and gross margin",
            "Record go/no-go reason before bid preparation",
        ]
        dossiers.append(dossier)
        if len(dossiers) >= max_deals:
            break
    return dossiers


def export_deal_dossiers(
    dossiers: list[dict[str, Any]],
    *,
    json_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
) -> None:
    if json_path is not None:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dossiers, ensure_ascii=False, indent=2), encoding="utf-8")

    if markdown_path is not None:
        path = Path(markdown_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Sterlitamak deal dossiers", "", f"Deals: {len(dossiers)}", ""]
        for idx, row in enumerate(dossiers, 1):
            price = row.get("max_price")
            price_text = "—" if price is None else f"{float(price):,.0f} ₽".replace(",", " ")
            lines.extend([
                f"## {idx}. {row.get('purchase_number')} — {row.get('deal_score')}/100 ({row.get('deal_tier')})",
                "",
                f"- **Decision:** {row.get('decision')}",
                f"- **Customer:** {row.get('customer_name') or '—'} / INN {row.get('customer_inn') or '—'}",
                f"- **Object:** {row.get('object_info') or '—'}",
                f"- **Max price:** {price_text}",
                f"- **Deadline:** {row.get('collecting_finished_at') or '—'} ({row.get('days_remaining')} days)",
                f"- **Segment / mode:** {row.get('segment')} / {row.get('execution_mode')}",
                f"- **Review flags:** {', '.join(row.get('review_flags') or []) or 'none'}",
                f"- **Contract guarantee:** {row.get('contract_guarantee_amount') or '—'}; part={row.get('contract_guarantee_part') or '—'}",
                "",
                "### Line items",
                "",
                "| # | Item | OKPD2/KTRU | Qty | Unit | Unit price | Amount |",
                "|---:|---|---|---:|---|---:|---:|",
            ])
            for item_no, item in enumerate(row.get("items") or [], 1):
                name = str(item.get("name") or item.get("okpd2_name") or item.get("ktru_name") or "—").replace("|", "/").replace("\n", " ")
                if len(name) > 100:
                    name = name[:97] + "…"
                code = item.get("okpd2_code") or item.get("ktru_code") or "—"
                qty = item.get("quantity") if item.get("quantity") is not None else "—"
                unit = item.get("unit_name") or item.get("okei_code") or "—"
                unit_price = item.get("unit_price")
                amount = item.get("amount")
                unit_price_text = "—" if unit_price is None else f"{float(unit_price):,.2f}".replace(",", " ")
                amount_text = "—" if amount is None else f"{float(amount):,.2f}".replace(",", " ")
                lines.append(f"| {item_no} | {name} | {code} | {qty} | {unit} | {unit_price_text} | {amount_text} |")
            if not row.get("items"):
                lines.append("| — | Line-item detail not collected yet | — | — | — | — | — |")
            lines.extend(["", "### Next work units", ""])
            for unit in row.get("next_work_units") or []:
                lines.append(f"- [ ] {unit}")
            lines.extend(["", "---", ""])
        path.write_text("\n".join(lines), encoding="utf-8")
