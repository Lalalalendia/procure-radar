from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProtocolOutcome:
    code: str
    label: str
    weakness_weight: float


OUTCOMES: dict[str, ProtocolOutcome] = {
    "competitive": ProtocolOutcome("competitive", "Конкурентный исход", 0.0),
    "single_submitted": ProtocolOutcome(
        "single_submitted", "Подана только одна заявка", 0.90
    ),
    "single_submitted_rejected": ProtocolOutcome(
        "single_submitted_rejected",
        "Подана одна заявка, но она не соответствует требованиям",
        0.95,
    ),
    "single_admitted": ProtocolOutcome(
        "single_admitted", "Из нескольких заявок допущена только одна", 0.80
    ),
    "no_bids": ProtocolOutcome("no_bids", "Не подано заявок", 1.00),
    "all_rejected": ProtocolOutcome(
        "all_rejected", "Все заявки отклонены/не соответствуют", 0.65
    ),
    "abandoned_other": ProtocolOutcome(
        "abandoned_other", "Иная причина признания закупки несостоявшейся", 0.50
    ),
    "unknown": ProtocolOutcome("unknown", "Исход не классифицирован", 0.0),
}


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def classify_protocol_outcome(protocol: Mapping[str, Any]) -> ProtocolOutcome:
    """Classify a normalized final protocol into a business-useful outcome.

    We intentionally do not equate EIS/Gosplan ``abandonedReason`` with a generic
    business failure. A procurement can be legally "несостоявшейся" because only
    one compliant supplier remained and still proceed to contract conclusion.
    """
    applications = _int(protocol.get("applications_count"))
    admitted = _int(protocol.get("admitted_count"))
    rejected = _int(protocol.get("rejected_count"))
    abandoned = _truthy(protocol.get("is_abandoned"))
    reason = str(protocol.get("abandoned_reason_name") or "").casefold()

    # Text is valuable when admission flags differ across protocol schemas.
    no_bid_text = any(
        token in reason
        for token in (
            "не подано ни одной заяв",
            "не было подано ни одной заяв",
            "не поступило ни одной заяв",
            "отсутствуют заявки",
        )
    )
    all_rejected_text = (
        ("все заяв" in reason and ("отклон" in reason or "не соответств" in reason))
        or "ни одна заяв" in reason and "не соответств" in reason
    )
    single_submitted_text = any(
        token in reason
        for token in (
            "подана только одна заяв",
            "подана одна заяв",
            "поступила только одна заяв",
            "поступила одна заяв",
        )
    )
    single_admitted_text = (
        "только одна заяв" in reason
        and ("допущ" in reason or "соответств" in reason or "признана" in reason)
        and not single_submitted_text
    )

    if no_bid_text or (abandoned and applications == 0):
        return OUTCOMES["no_bids"]

    single_submitted_rejected = (
        single_submitted_text
        and ("не соответств" in reason or "отклон" in reason)
    ) or (
        abandoned
        and applications == 1
        and admitted == 0
        and rejected is not None
        and rejected >= 1
    )
    if single_submitted_rejected:
        return OUTCOMES["single_submitted_rejected"]

    if all_rejected_text or (
        abandoned
        and applications is not None
        and applications > 0
        and rejected is not None
        and rejected >= applications
    ):
        return OUTCOMES["all_rejected"]
    if single_submitted_text or applications == 1:
        return OUTCOMES["single_submitted"]
    if single_admitted_text or (
        abandoned
        and applications is not None
        and applications > 1
        and admitted == 1
    ):
        return OUTCOMES["single_admitted"]
    if abandoned:
        return OUTCOMES["abandoned_other"]
    if applications is not None and applications >= 2:
        return OUTCOMES["competitive"]
    return OUTCOMES["unknown"]


def bid_depth_weakness(protocol: Mapping[str, Any]) -> float:
    """Return 0..1 weakness from submitted bid depth alone."""
    applications = _int(protocol.get("applications_count"))
    if applications is None:
        return 0.0
    if applications <= 1:
        return 1.0
    if applications == 2:
        return 0.60
    if applications == 3:
        return 0.35
    if applications == 4:
        return 0.20
    return 0.0


def protocol_weakness(protocol: Mapping[str, Any]) -> float:
    outcome = classify_protocol_outcome(protocol)
    return max(outcome.weakness_weight, bid_depth_weakness(protocol))
