from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def _inn_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if item in (None, ""):
            continue
        if isinstance(item, dict):
            candidate = None
            for key in ("inn", "INN", "supplier_inn", "supplierInn", "customer_inn", "customerInn"):
                raw = item.get(key)
                if raw not in (None, ""):
                    candidate = str(raw).strip()
                    break
            if candidate:
                result.append(candidate)
        else:
            result.append(str(item))
    return result


def _documents(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str | None]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "doc_type": _str_or_none(item.get("doc_type")),
                "published_at": _str_or_none(item.get("published_at")),
            }
        )
    return result


def _collect_codes(value: Any) -> tuple[list[str], list[str]]:
    okpd2: set[str] = set()
    ktru: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "OKPD2" and isinstance(child, dict):
                    code = _str_or_none(child.get("OKPDCode") or child.get("code"))
                    if code:
                        okpd2.add(code)
                elif key in {"KTRU", "KTRUInfo"} and isinstance(child, dict):
                    code = _str_or_none(child.get("code") or child.get("KTRUCode"))
                    if code:
                        ktru.add(code)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    docs = value.get("docs") if isinstance(value, dict) else None
    if isinstance(docs, list):
        for doc in docs:
            if isinstance(doc, dict) and isinstance(doc.get("source"), dict):
                walk(doc["source"])
    return sorted(okpd2), sorted(ktru)


def _purchase_doc_fallbacks(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"object_info": None, "purchase_type": None, "published_at": None}
    docs = payload.get("docs")
    if not isinstance(docs, list):
        return result
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        source = _dict(doc.get("source"))
        common = _dict(source.get("commonInfo"))
        if result["object_info"] is None:
            result["object_info"] = _str_or_none(
                common.get("purchaseObjectInfo") or common.get("purchaseObjectName")
            )
        doc_type = _str_or_none(doc.get("doc_type"))
        if result["purchase_type"] is None and doc_type and doc_type.startswith("epNotification"):
            result["purchase_type"] = doc_type
        if result["published_at"] is None:
            result["published_at"] = _str_or_none(doc.get("published_at"))
        if result["object_info"] and result["purchase_type"] and result["published_at"]:
            break
    return result


def extract_purchase(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize aggregate or full /fz44/purchases response without assuming equal shapes."""
    fallback = _purchase_doc_fallbacks(payload)
    doc_okpd2, doc_ktru = _collect_codes(payload)
    return {
        "purchase_number": _str_or_none(payload.get("purchase_number")),
        "published_at": _str_or_none(payload.get("published_at")) or fallback["published_at"],
        "collecting_finished_at": _str_or_none(payload.get("collecting_finished_at")),
        "doc_created_at": _str_or_none(payload.get("doc_created_at")),
        "doc_updated_at": _str_or_none(payload.get("doc_updated_at")),
        "updated_at": _str_or_none(payload.get("updated_at")),
        "max_price": _float_or_none(payload.get("max_price")),
        "currency_code": _str_or_none(payload.get("currency_code")),
        "object_info": _str_or_none(payload.get("object_info")) or fallback["object_info"],
        "purchase_type": _str_or_none(payload.get("purchase_type")) or fallback["purchase_type"],
        "region_code": _int_or_none(payload.get("region")),
        "stage": _int_or_none(payload.get("stage")),
        "responsible_inn": _str_or_none(payload.get("responsible")),
        "contract_guarantee_amount": _float_or_none(payload.get("contract_guarantee_amount")),
        "contract_guarantee_part": _float_or_none(payload.get("contract_guarantee_part")),
        "customers": _inn_list(payload.get("customers")),
        "owners": _inn_list(payload.get("owners")),
        "ikzs": _str_list(payload.get("ikzs")),
        "plan_numbers": _str_list(payload.get("plan_numbers")),
        "position_numbers": _str_list(payload.get("position_numbers")),
        "okpd2": _str_list(payload.get("okpd2")) or doc_okpd2,
        "ktru": _str_list(payload.get("ktru")) or doc_ktru,
        "docs": _documents(payload.get("docs")),
    }


def extract_procedure(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the observed /fz44/procedures (contract-conclusion) response."""
    return {
        "contract_project_number": _str_or_none(payload.get("contract_project_number")),
        "purchase_number": _str_or_none(payload.get("purchase_number")),
        "customer_inn": _str_or_none(payload.get("customer")),
        "participant_inn": _str_or_none(payload.get("participant")),
        "price": _float_or_none(payload.get("price")),
        "currency_code": _str_or_none(payload.get("currency_code")),
        "region_code": _int_or_none(payload.get("region")),
        "subject": _str_or_none(payload.get("subject")),
        "published_at": _str_or_none(payload.get("published_at")),
        "doc_created_at": _str_or_none(payload.get("doc_created_at")),
        "doc_updated_at": _str_or_none(payload.get("doc_updated_at")),
        "updated_at": _str_or_none(payload.get("updated_at")),
        "docs": _documents(payload.get("docs")),
    }


def extract_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the observed /fz44/contracts response."""
    return {
        "reg_num": _str_or_none(payload.get("reg_num")),
        "purchase_number": _str_or_none(payload.get("purchase_number")),
        "customer_inn": _str_or_none(payload.get("customer")),
        "suppliers": _inn_list(payload.get("suppliers")),
        "price": _float_or_none(payload.get("price")),
        "currency_code": _str_or_none(payload.get("currency_code")),
        "region_code": _int_or_none(payload.get("region")),
        "stage": _str_or_none(payload.get("stage")),
        "subject": _str_or_none(payload.get("subject")),
        "plan_number": _str_or_none(payload.get("plan_number")),
        "position_number": _str_or_none(payload.get("position_number")),
        "exe_start": _str_or_none(payload.get("exe_start")),
        "exe_end": _str_or_none(payload.get("exe_end")),
        "elact_at": _str_or_none(payload.get("elact_at")),
        "published_at": _str_or_none(payload.get("published_at")),
        "doc_created_at": _str_or_none(payload.get("doc_created_at")),
        "doc_updated_at": _str_or_none(payload.get("doc_updated_at")),
        "updated_at": _str_or_none(payload.get("updated_at")),
        "okpd2": _str_list(payload.get("okpd2")),
        "ktru": _str_list(payload.get("ktru")),
        "docs": _documents(payload.get("docs")),
    }


def external_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("purchase_number")
    return str(value) if value not in (None, "") else None


def procedure_external_id(payload: dict[str, Any]) -> str | None:
    for key in ("contract_project_number", "purchase_number"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def contract_external_id(payload: dict[str, Any]) -> str | None:
    for key in ("reg_num", "purchase_number"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def extract_tender_protocol(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a document returned by /fz44/purchases/{number}/protocols.

    The endpoint returns full EIS protocol documents. The exact inner shape varies
    by protocol type, so this extractor intentionally focuses on fields required
    for competition analysis and keeps the full raw document separately.
    """
    source = _dict(payload.get("source"))
    common = _dict(source.get("commonInfo"))
    protocol_info = _dict(source.get("protocolInfo"))
    applications_info = _dict(protocol_info.get("applicationsInfo"))
    raw_applications = _as_list(applications_info.get("applicationInfo"))

    applications: list[dict[str, Any]] = []
    for raw in raw_applications:
        app = _dict(raw)
        app_common = _dict(app.get("commonInfo"))
        admitted_info = _dict(app.get("admittedInfo"))
        app_admitted_info = _dict(admitted_info.get("appAdmittedInfo"))
        admitted = _bool_or_none(app_admitted_info.get("admitted"))
        applications.append(
            {
                "app_number": _str_or_none(app_common.get("appNumber")),
                "app_at": _str_or_none(app_common.get("appDT")),
                "final_price": _float_or_none(app.get("finalPrice")),
                "admitted": admitted,
                "app_rating": _int_or_none(app_admitted_info.get("appRating")),
            }
        )

    admitted_count = sum(1 for app in applications if app["admitted"] is True)
    rejected_count = sum(1 for app in applications if app["admitted"] is False)
    abandoned_reason = _dict(protocol_info.get("abandonedReason"))

    admitted_with_price = [
        app for app in applications
        if app["admitted"] is True and app["final_price"] is not None
    ]
    if admitted_with_price:
        rated = [app for app in admitted_with_price if app["app_rating"] is not None]
        if rated:
            winner = min(
                rated,
                key=lambda app: (int(app["app_rating"]), float(app["final_price"])),
            )
        else:
            winner = min(admitted_with_price, key=lambda app: float(app["final_price"]))
        selected_final_price = float(winner["final_price"])
    elif any(app["admitted"] is not None for app in applications):
        # If admission decisions are present and nobody was admitted, there is
        # no valid winning price. Do not let a rejected low bid distort the
        # market discount metric.
        selected_final_price = None
    else:
        prices = [
            float(app["final_price"])
            for app in applications
            if app["final_price"] is not None
        ]
        selected_final_price = min(prices) if prices else None

    source_id = _str_or_none(source.get("id"))
    source_external_id = _str_or_none(source.get("externalId"))
    version_number = _str_or_none(source.get("versionNumber"))
    doc_type = _str_or_none(payload.get("doc_type"))
    purchase_number = _str_or_none(common.get("purchaseNumber"))
    protocol_key = source_id or (
        ":".join(
            part
            for part in (purchase_number, doc_type, source_external_id, version_number)
            if part
        )
        or None
    )

    return {
        "protocol_key": protocol_key,
        "purchase_number": purchase_number,
        "doc_type": doc_type,
        "published_at": _str_or_none(payload.get("published_at")),
        "source_id": source_id,
        "source_external_id": source_external_id,
        "version_number": version_number,
        "procedure_at": _str_or_none(common.get("procedureDT")),
        "applications_count": len(applications),
        "admitted_count": admitted_count,
        "rejected_count": rejected_count,
        "final_price": selected_final_price,
        "is_abandoned": bool(abandoned_reason),
        "abandoned_reason_code": _str_or_none(abandoned_reason.get("code")),
        "abandoned_reason_name": _str_or_none(abandoned_reason.get("name")),
        "applications": applications,
    }


def protocol_external_id(payload: dict[str, Any]) -> str | None:
    parsed = extract_tender_protocol(payload)
    value = parsed.get("protocol_key")
    return str(value) if value not in (None, "") else None


def _iter_purchase_objects(value: Any):
    """Yield purchaseObject dicts regardless of the wrapper used by the EIS document type."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "purchaseObject":
                for item in _as_list(child):
                    if isinstance(item, dict):
                        yield item
                continue
            yield from _iter_purchase_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_purchase_objects(child)


def _latest_purchase_object_document(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest full notification snapshot that contains purchase objects.

    GosPlan can keep several revisions of the same notification in ``docs``.
    Line-item SIDs are stable across revisions, so reading every revision both
    duplicates demand and can violate the DB uniqueness constraint.
    """
    docs = payload.get("docs")
    if not isinstance(docs, list):
        return None

    candidates: list[tuple[tuple[str, int, int], dict[str, Any]]] = []
    for index, doc in enumerate(docs):
        if not isinstance(doc, dict):
            continue
        source = _dict(doc.get("source"))
        notification = _dict(source.get("notificationInfo"))
        objects = notification.get("purchaseObjectsInfo")
        if not isinstance(objects, (dict, list)):
            continue
        published_at = _str_or_none(doc.get("published_at")) or ""
        version = _int_or_none(source.get("versionNumber"))
        candidates.append(((published_at, version if version is not None else -1, index), doc))

    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


def _collect_boolean_flags(node: Any, key: str) -> list[bool]:
    values: list[bool] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if key in value:
                raw = value.get(key)
                if isinstance(raw, dict) and "value" in raw:
                    raw = raw.get("value")
                parsed = _bool_or_none(raw)
                if parsed is not None:
                    values.append(parsed)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return values


def extract_purchase_pricing_mode(payload: dict[str, Any]) -> str:
    """Classify whether protocol bid price is comparable with purchase max_price.

    EIS uses the maximum contract value instead of a normal contract price for
    purchases with undefined quantity and for formula-priced contracts. In
    those cases protocol ``finalPrice`` can represent a unit-price basis (or a
    sum of unit prices), so comparing it with ``max_price`` creates fake
    99.99% discounts.
    """
    doc = _latest_purchase_object_document(payload)
    if doc is None:
        return "unknown"

    source = _dict(doc.get("source"))
    notification = _dict(source.get("notificationInfo"))
    if not notification:
        return "unknown"

    quantity_undefined = _collect_boolean_flags(notification, "quantityUndefined")
    formula_price = _collect_boolean_flags(notification, "isContractPriceFormula")

    if any(quantity_undefined) or any(formula_price):
        return "maximum_contract_price"
    if quantity_undefined or formula_price:
        return "fixed_contract_price"
    return "unknown"


def extract_purchase_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract line items from the latest full purchase-notification snapshot.

    Repeated notification revisions are intentionally ignored: the newest
    snapshot is authoritative for current line items. Duplicate SIDs inside
    that one snapshot are preserved with a deterministic ``#N`` suffix.
    """
    doc = _latest_purchase_object_document(payload)
    if doc is None:
        return []

    source = _dict(doc.get("source"))
    notification = _dict(source.get("notificationInfo"))
    objects = notification.get("purchaseObjectsInfo")
    if not isinstance(objects, (dict, list)):
        return []

    source_id = _str_or_none(source.get("id")) or _str_or_none(source.get("externalId")) or "doc"
    doc_type = _str_or_none(doc.get("doc_type"))
    common = _dict(source.get("commonInfo"))
    common_name = _str_or_none(common.get("purchaseObjectInfo"))
    key_occurrences: dict[str, int] = {}
    items: list[dict[str, Any]] = []

    for index, raw in enumerate(_iter_purchase_objects(objects), 1):
        obj = _dict(raw)
        ktru = _dict(obj.get("KTRU")) or _dict(obj.get("KTRUInfo"))
        okpd2 = _dict(ktru.get("OKPD2")) or _dict(obj.get("OKPD2"))
        okei = _dict(obj.get("OKEI"))
        quantity_raw = obj.get("quantity")
        quantity = _dict(quantity_raw)
        quantity_value = quantity.get("value") if quantity else quantity_raw
        characteristics = ktru.get("characteristics")
        if not isinstance(characteristics, (dict, list)):
            characteristics = None

        base_key = (
            _str_or_none(obj.get("externalSid"))
            or _str_or_none(obj.get("sid"))
            or f"{doc_type or 'document'}:{source_id}:{index}"
        )
        occurrence = key_occurrences.get(base_key, 0) + 1
        key_occurrences[base_key] = occurrence
        item_key = base_key if occurrence == 1 else f"{base_key}#{occurrence}"

        items.append(
            {
                "item_key": item_key,
                "source_doc_type": doc_type,
                "name": (
                    _str_or_none(obj.get("name"))
                    or _str_or_none(obj.get("purchaseObjectName"))
                    or _str_or_none(ktru.get("name"))
                    or common_name
                ),
                "ktru_code": _str_or_none(ktru.get("code") or ktru.get("KTRUCode")),
                "ktru_name": _str_or_none(ktru.get("name") or ktru.get("KTRUName")),
                "okpd2_code": _str_or_none(okpd2.get("OKPDCode") or okpd2.get("code")),
                "okpd2_name": _str_or_none(okpd2.get("OKPDName") or okpd2.get("name")),
                "okei_code": _str_or_none(okei.get("code")),
                "unit_name": _str_or_none(okei.get("name")) or _str_or_none(okei.get("nationalCode")),
                "quantity": _float_or_none(quantity_value),
                "unit_price": _float_or_none(obj.get("price") or obj.get("unitPrice")),
                "amount": _float_or_none(obj.get("sum") or obj.get("amount")),
                "is_medical_product": _bool_or_none(obj.get("isMedicalProduct")),
                "characteristics": characteristics,
            }
        )
    return items


def has_full_purchase_documents(payload: dict[str, Any]) -> bool:
    docs = payload.get("docs")
    return isinstance(docs, list) and any(
        isinstance(doc, dict) and isinstance(doc.get("source"), dict) for doc in docs
    )


def extract_embedded_protocols(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return full epProtocol* documents embedded in a full purchase response."""
    docs = payload.get("docs")
    if not isinstance(docs, list):
        return []
    result: list[dict[str, Any]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        doc_type = doc.get("doc_type")
        if isinstance(doc_type, str) and doc_type.startswith("epProtocol") and isinstance(doc.get("source"), dict):
            result.append(doc)
    return result


# --- 44-FZ tender-plan (plan-schedule) normalization -----------------------

_TENDERPLAN_POSITION_CONTAINER_KEYS = {
    "positions",
    "specialPurchasePositions",
    "positionInfo",
    "planPositions",
}
_TENDERPLAN_POSITION_KEYS = {"position", "specialPurchasePosition", "planPosition"}


def _deep_values(node: Any, keys: set[str]) -> list[Any]:
    result: list[Any] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in keys:
                    result.append(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return result


def _first_deep(node: Any, keys: tuple[str, ...]) -> Any:
    wanted = set(keys)
    queue: list[Any] = [node]
    while queue:
        value = queue.pop(0)
        if isinstance(value, dict):
            for key in keys:
                raw = value.get(key)
                if raw not in (None, "", [], {}):
                    if isinstance(raw, dict) and "value" in raw and raw.get("value") not in (None, ""):
                        return raw.get("value")
                    return raw
            for key, child in value.items():
                if key not in wanted:
                    queue.append(child)
        elif isinstance(value, list):
            queue.extend(value)
    return None


def _normalize_inn(value: Any) -> str | None:
    if isinstance(value, dict):
        value = _first_deep(value, ("inn", "INN", "customerInn", "customer_inn"))
    text = str(value or "").strip()
    if len(text) not in (10, 12) or not text.isdigit():
        return None
    return text


def _all_inns(node: Any) -> list[str]:
    result: set[str] = set()

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = key.lower()
                if lowered in {"inn", "customerinn", "customer_inn", "ownerinn", "owner_inn"}:
                    inn = _normalize_inn(child)
                    if inn:
                        result.add(inn)
                walk(child, path + (key,))
        elif isinstance(value, list):
            for child in value:
                walk(child, path)

    walk(node)
    return sorted(result)


def _generic_codes(node: Any) -> tuple[list[str], list[str]]:
    okpd2: set[str] = set()
    ktru: set[str] = set()

    def is_code_like(text: str, *, system: str) -> bool:
        value = text.strip()
        if not value or not any(ch.isdigit() for ch in value):
            return False
        if system == "okpd2":
            # OKPD2 usually looks like 25.30.12 / 25.30.12.110.  Accept compact
            # numeric forms too, but reject labels such as "Котлы 25.30.12".
            return re.fullmatch(r"\d{2}(?:[.\s-]?\d{2}){2}(?:[.\s-]?\d{1,3})?", value) is not None
        # KTRU codes commonly extend an OKPD2 prefix with a long suffix.
        return re.fullmatch(r"\d{2}(?:[.\s-]?\d{2}){2}(?:[.\s-]?\d{1,3})?(?:[-.]\d+)?", value) is not None

    def add_nested(raw: Any, *, system: str, key_hint: str | None = None) -> None:
        target = okpd2 if system == "okpd2" else ktru
        if isinstance(raw, dict):
            preferred = (
                ("OKPDCode", "okpdCode", "okpd_code", "code", "value")
                if system == "okpd2"
                else ("KTRUCode", "ktruCode", "ktru_code", "code", "value")
            )
            for key in preferred:
                if key in raw:
                    add_nested(raw[key], system=system, key_hint=key)
            # Real tenderPlan2020 revisions sometimes wrap classifier records in
            # arrays/containers one or two levels below OKPD2/KTRU.  Recurse so
            # those shapes are handled without stringifying the whole container.
            for key, child in raw.items():
                lowered = key.lower()
                if key in preferred or lowered.endswith("name"):
                    continue
                add_nested(child, system=system, key_hint=key)
            return
        if isinstance(raw, list):
            for child in raw:
                add_nested(child, system=system, key_hint=key_hint)
            return
        text = str(raw or "").strip()
        hint = (key_hint or "").lower()
        if text and ("code" in hint or hint in {"value", "okpd2", "ktru"} or is_code_like(text, system=system)):
            if is_code_like(text, system=system):
                target.add(text)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = key.lower()
                if key == "OKPD2" or lowered in {"okpd2", "okpd2code", "okpdcode", "okpd_code"}:
                    add_nested(child, system="okpd2", key_hint=key)
                elif key in {"KTRU", "KTRUInfo"} or lowered in {"ktru", "ktrucode", "ktru_code"}:
                    add_nested(child, system="ktru", key_hint=key)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return sorted(okpd2), sorted(ktru)


def _iter_tenderplan_positions(node: Any):
    """Yield position dictionaries from aggregate or full tenderPlan2020 payloads.

    GosPlan has changed aggregate projections over time while full EIS documents
    keep nested ``positions/position`` and ``specialPurchasePositions/...``
    structures. This walker accepts both without depending on one revision.
    """
    seen_ids: set[int] = set()

    def emit(raw: Any):
        for item in _as_list(raw):
            if isinstance(item, dict) and id(item) not in seen_ids:
                seen_ids.add(id(item))
                yield item

    def walk(value: Any, parent_key: str | None = None):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in _TENDERPLAN_POSITION_KEYS:
                    yield from emit(child)
                    continue
                if key in _TENDERPLAN_POSITION_CONTAINER_KEYS and isinstance(child, (dict, list)):
                    if isinstance(child, list):
                        # Aggregate API rows can expose a flat list of positions.
                        yield from emit(child)
                    yield from walk(child, key)
                    continue
                yield from walk(child, key)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child, parent_key)

    yield from walk(node)


def _date_like(value: Any) -> str | None:
    raw = _str_or_none(value)
    if not raw:
        return None
    text = raw.strip()
    match = re.search(r"(20\d{2})[-./]?(0[1-9]|1[0-2])(?:[-./]?([0-3]\d))?", text)
    if not match:
        return text
    year, month, day = match.groups()
    return f"{year}-{month}-{day or '01'}"


def _position_amount(position: dict[str, Any]) -> float | None:
    # Prefer explicit total/financial amounts before generic ``amount`` values.
    for keys in (
        ("totalAmount", "total_amount", "purchaseAmount", "purchase_amount"),
        ("maxPrice", "max_price", "NMCK", "nmck"),
        ("amount", "sum", "value"),
    ):
        raw = _first_deep(position, keys)
        parsed = _float_or_none(raw)
        if parsed is not None and parsed >= 0:
            return parsed
    return None


def _position_name(position: dict[str, Any]) -> str | None:
    raw = _first_deep(
        position,
        (
            "purchaseObjectName",
            "purchaseObjectInfo",
            "objectInfo",
            "object_info",
            "subject",
            "description",
        ),
    )
    if raw not in (None, ""):
        return str(raw).strip() or None
    # ``name`` is very common under OKPD dictionaries, so only use a top-level
    # generic name as a last resort.
    raw = position.get("name")
    return str(raw).strip() if raw not in (None, "") else None


def extract_tenderplan_position(position: dict[str, Any], *, index: int = 0) -> dict[str, Any]:
    position_number = _str_or_none(
        _first_deep(
            position,
            (
                "position_number",
                "positionNumber",
                "planPositionNumber",
                "positionNum",
                "positionId",
                "positionID",
            ),
        )
    )
    ikz = _str_or_none(_first_deep(position, ("IKZ", "ikz", "IKZCode", "purchaseCode")))
    okpd2, ktru = _generic_codes(position)
    customer_inn = _normalize_inn(
        _first_deep(position, ("customerInn", "customer_inn", "customerINN", "INN", "inn"))
    )
    planned_at = _date_like(
        _first_deep(
            position,
            (
                "plannedPublishDate",
                "plannedPublicationDate",
                "plannedPlacementDate",
                "plannedDate",
                "publishDate",
                "placementDate",
            ),
        )
    )
    planned_year = _int_or_none(
        _first_deep(position, ("plannedYear", "plannedPublishYear", "placementYear", "year"))
    )
    planned_month = _int_or_none(
        _first_deep(position, ("plannedMonth", "plannedPublishMonth", "placementMonth", "month"))
    )
    if planned_at:
        try:
            planned_year = planned_year or int(planned_at[:4])
            planned_month = planned_month or int(planned_at[5:7])
        except (TypeError, ValueError, IndexError):
            pass

    raw_key = position_number or ikz
    if not raw_key:
        encoded = json.dumps(position, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        raw_key = "sha1:" + hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:20]
    return {
        "position_key": raw_key,
        "position_number": position_number,
        "ikz": ikz,
        "customer_inn": customer_inn,
        "object_info": _position_name(position),
        "planned_at": planned_at,
        "planned_year": planned_year,
        "planned_month": planned_month,
        "amount": _position_amount(position),
        "okpd2": okpd2,
        "ktru": ktru,
        "raw_index": index,
    }


def extract_tenderplan(payload: dict[str, Any]) -> dict[str, Any]:
    docs = payload.get("docs") if isinstance(payload.get("docs"), list) else []
    doc_sources = [doc.get("source") for doc in docs if isinstance(doc, dict) and isinstance(doc.get("source"), dict)]
    search_root: Any = doc_sources[-1] if doc_sources else payload

    plan_number = _str_or_none(
        payload.get("plan_number")
        or payload.get("tender_plan_number")
        or payload.get("registry_number")
        or _first_deep(search_root, ("planNumber", "tenderPlanNumber", "registryNumber", "reestrNumber"))
    )
    published_at = _str_or_none(payload.get("published_at")) or _str_or_none(
        _first_deep(search_root, ("publishDate", "publishedAt", "publicationDate"))
    )
    year = _int_or_none(payload.get("year")) or _int_or_none(
        _first_deep(search_root, ("year", "planYear", "financialYear"))
    )
    region_code = _int_or_none(payload.get("region")) or _int_or_none(payload.get("region_code"))
    customer_inns = _inn_list(payload.get("customers"))
    if not customer_inns:
        customer_inns = _all_inns(search_root)

    positions: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    roots = [payload]
    roots.extend(doc_sources)
    for root in roots:
        for index, raw in enumerate(_iter_tenderplan_positions(root), 1):
            parsed = extract_tenderplan_position(raw, index=index)
            key = str(parsed["position_key"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            positions.append(parsed)

    # Some aggregate projections expose parallel position/code arrays but not a
    # nested position object. Preserve at least those position identifiers.
    if not positions:
        aggregate_numbers = _str_list(payload.get("position_numbers"))
        aggregate_okpd2 = _str_list(payload.get("okpd2"))
        for index, number in enumerate(aggregate_numbers, 1):
            positions.append(
                {
                    "position_key": number,
                    "position_number": number,
                    "ikz": None,
                    "customer_inn": customer_inns[0] if len(customer_inns) == 1 else None,
                    "object_info": None,
                    "planned_at": None,
                    "planned_year": year,
                    "planned_month": None,
                    "amount": None,
                    "okpd2": aggregate_okpd2,
                    "ktru": [],
                    "raw_index": index,
                }
            )

    if region_code is None:
        region_code = _int_or_none(_first_deep(search_root, ("regionCode", "region", "kladrRegion")))
    return {
        "plan_number": plan_number,
        "published_at": published_at,
        "year": year,
        "region_code": region_code,
        "customer_inns": customer_inns,
        "positions": positions,
        "docs": _documents(payload.get("docs")),
    }


def tenderplan_external_id(payload: dict[str, Any]) -> str | None:
    value = extract_tenderplan(payload).get("plan_number")
    return str(value) if value not in (None, "") else None
