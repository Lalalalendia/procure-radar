from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProcurementMethod:
    code: str
    label: str
    competition_eligible: bool
    exclusion_reason: str | None = None


_METHODS: dict[str, ProcurementMethod] = {
    "epNotificationEF2020": ProcurementMethod(
        "electronic_auction", "Электронный аукцион", True
    ),
    "epNotificationEZK2020": ProcurementMethod(
        "electronic_quote", "Электронный запрос котировок", True
    ),
    "epNotificationEZK": ProcurementMethod(
        "electronic_quote", "Электронный запрос котировок", True
    ),
    "epNotificationEOK2020": ProcurementMethod(
        "electronic_competition", "Открытый конкурс в электронной форме", True
    ),
    "epNotificationEOK": ProcurementMethod(
        "electronic_competition", "Открытый конкурс в электронной форме", True
    ),
    "epNotificationEOKD": ProcurementMethod(
        "electronic_competition_two_stage", "Двухэтапный электронный конкурс", True
    ),
    "epNotificationEOKOU": ProcurementMethod(
        "electronic_competition_limited", "Электронный конкурс с ограниченным участием", True
    ),
    "epNotificationEZP": ProcurementMethod(
        "electronic_request_proposals", "Электронный запрос предложений", True
    ),
    "epNotificationEZT2020": ProcurementMethod(
        "single_supplier_93_12",
        "Электронная закупка товара по ч. 12 ст. 93 44-ФЗ",
        False,
        "special_single_supplier_93_12",
    ),
}

_UNKNOWN = ProcurementMethod(
    "other", "Другой/неразобранный способ закупки", False, "unsupported_purchase_type"
)

_CANCEL_DOC_TYPES = {
    "epNotificationCancel",
    "fcsNotificationCancel",
    "pprf615NotificationCancel",
}

_COMPLETION_DOC_PREFIXES = (
    "fcsPlacementResult",
)

_PROTOCOL_FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {
    "electronic_auction": ("epProtocolEF",),
    "electronic_quote": ("epProtocolEZK",),
    "electronic_competition": ("epProtocolEOK",),
    "electronic_competition_two_stage": ("epProtocolEOKD",),
    "electronic_competition_limited": ("epProtocolEOKOU",),
    "electronic_request_proposals": ("epProtocolEZP",),
}


def classify_purchase_type(purchase_type: str | None) -> ProcurementMethod:
    if not purchase_type:
        return ProcurementMethod("unknown", "Способ закупки не определён", False, "missing_purchase_type")
    return _METHODS.get(purchase_type, _UNKNOWN)


def document_types(docs: Iterable[Mapping[str, Any] | str | None]) -> list[str]:
    result: list[str] = []
    for doc in docs:
        if isinstance(doc, str):
            doc_type = doc
        elif isinstance(doc, Mapping):
            value = doc.get("doc_type")
            doc_type = str(value) if value else ""
        else:
            doc_type = ""
        if doc_type:
            result.append(doc_type)
    return result


def is_cancel_document(doc_type: str | None) -> bool:
    return bool(doc_type and doc_type in _CANCEL_DOC_TYPES)


def is_final_protocol(doc_type: str | None) -> bool:
    if not doc_type or not doc_type.startswith("epProtocol"):
        return False
    return "Final" in doc_type


def is_competition_protocol(purchase_type: str | None, doc_type: str | None) -> bool:
    method = classify_purchase_type(purchase_type)
    if not method.competition_eligible or not is_final_protocol(doc_type):
        return False

    prefixes = _PROTOCOL_FAMILY_PREFIXES.get(method.code, ())
    return bool(prefixes and doc_type and doc_type.startswith(prefixes))


def competitive_final_protocol_types(
    purchase_type: str | None,
    docs: Iterable[Mapping[str, Any] | str | None],
) -> tuple[str, ...]:
    result = {
        doc_type
        for doc_type in document_types(docs)
        if is_competition_protocol(purchase_type, doc_type)
    }
    return tuple(sorted(result))


def competition_detail_exclusion_reason(
    purchase_type: str | None,
    *,
    docs: Iterable[Mapping[str, Any] | str | None],
) -> str | None:
    """Return why a list row should not spend a detail request.

    Bulk competition collection is deliberately document-driven: numeric ``stage``
    is not consulted here. A detail request is justified only when the purchase is
    an explicitly supported competitive method, is not cancelled, and the list
    row already advertises a matching final protocol in ``docs``.
    """
    doc_types = document_types(docs)
    method = classify_purchase_type(purchase_type)
    if not method.competition_eligible:
        return method.exclusion_reason
    if any(is_cancel_document(doc_type) for doc_type in doc_types):
        return "cancelled"
    if not any(is_competition_protocol(purchase_type, doc_type) for doc_type in doc_types):
        return "no_competitive_final_protocol"
    return None


def lifecycle_from_docs(stage: int | None, doc_types: Iterable[str | None]) -> str:
    docs = [doc for doc in doc_types if doc]
    if any(is_cancel_document(doc) for doc in docs):
        return "cancelled"
    if any(is_final_protocol(doc) for doc in docs) or any(
        doc.startswith(_COMPLETION_DOC_PREFIXES) for doc in docs
    ):
        return "completed"
    if any(doc.startswith("epProtocol") or doc.startswith("fcsProtocol") for doc in docs):
        return "commission"
    if any(doc.startswith("epNotification") or doc.startswith("fcsNotification") for doc in docs):
        return "applications"
    return {1: "applications", 2: "commission", 3: "completed", 4: "cancelled"}.get(
        stage, "unknown"
    )


def competition_exclusion_reason(
    purchase_type: str | None,
    *,
    doc_types: Iterable[str | None] = (),
) -> str | None:
    method = classify_purchase_type(purchase_type)
    if any(is_cancel_document(doc) for doc in doc_types):
        return "cancelled"
    if not method.competition_eligible:
        return method.exclusion_reason
    return None


def select_competition_protocol(
    purchase_type: str | None,
    protocols: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    candidates = [
        row for row in protocols if is_competition_protocol(purchase_type, row.get("doc_type"))
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            str(row.get("published_at") or ""),
            int(row.get("id") or 0),
        ),
    )
