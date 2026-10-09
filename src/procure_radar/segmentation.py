from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarketSegment:
    code: str
    label: str
    group: str


SEGMENTS: dict[str, MarketSegment] = {
    "pharma": MarketSegment("pharma", "Лекарственные препараты", "goods"),
    "medical_diagnostics": MarketSegment("medical_diagnostics", "Диагностические реагенты и наборы", "goods"),
    "medical_equipment": MarketSegment("medical_equipment", "Медицинское оборудование и изделия", "goods"),
    "it_goods": MarketSegment("it_goods", "Компьютеры и ИТ-оборудование", "goods"),
    "standard_goods": MarketSegment("standard_goods", "Стандартные товары", "goods"),
    "real_estate": MarketSegment("real_estate", "Недвижимость и здания", "property"),
    "construction_works": MarketSegment("construction_works", "Строительные и дорожные работы", "works"),
    "it_services": MarketSegment("it_services", "ИТ-услуги", "services"),
    "repair_maintenance": MarketSegment("repair_maintenance", "Ремонт и техническое обслуживание", "services"),
    "facility_services": MarketSegment("facility_services", "Эксплуатационные и хозяйственные услуги", "services"),
    "professional_services": MarketSegment("professional_services", "Профессиональные услуги", "services"),
    "other_services": MarketSegment("other_services", "Прочие услуги", "services"),
    "other": MarketSegment("other", "Прочее", "other"),
}


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def _starts(code: str, prefixes: tuple[str, ...]) -> bool:
    return any(code.startswith(prefix) for prefix in prefixes)


def classify_market_segment(
    *,
    code_system: str,
    code: str,
    label: str | None = None,
) -> MarketSegment:
    """Coarse commercial segmentation for opportunity ranking.

    The goal is not to replace OKPD2/KTRU taxonomy. It separates markets that
    require fundamentally different go-to-market capabilities so they are not
    compared in one undifferentiated leaderboard.
    """
    normalized_code = (code or "").strip()
    text = _norm(label)

    try:
        major = int(normalized_code.split(".", 1)[0])
    except (TypeError, ValueError):
        major = -1

    # Service families must win before broad product keywords such as
    # "медицинск" or "компьютер". Otherwise repair of medical equipment is
    # misclassified as equipment, and software-license services as IT goods.
    if _starts(normalized_code, ("58.29", "61.", "62.", "63.")) or (
        major >= 33
        and any(token in text for token in ("информационн", "программ", "1с", "технической поддержке информа"))
    ):
        return SEGMENTS["it_services"]

    if _starts(normalized_code, ("33.", "95.")) or (
        major >= 33
        and any(
            token in text
            for token in ("ремонт", "техническое обслуживание", "техническому обслуживанию")
        )
    ):
        return SEGMENTS["repair_maintenance"]

    if _starts(normalized_code, ("80.", "81.", "82.")) or any(
        token in text for token in ("уборк", "охран", "эксплуатац", "дезинф")
    ):
        return SEGMENTS["facility_services"]

    if _starts(normalized_code, ("69.", "70.", "71.", "72.", "73.", "74.", "75.")):
        return SEGMENTS["professional_services"]

    # Pharma is kept separate from diagnostics and devices: licensing,
    # distribution and procurement economics are materially different.
    if _starts(normalized_code, ("21.20.10",)) or "лекарствен" in text or "препарат" in text:
        return SEGMENTS["pharma"]

    if _starts(normalized_code, ("21.20.23",)) or any(
        token in text
        for token in (
            "реагент",
            "ивд",
            "in vitro",
            "нуклеинов",
            "иммунофермент",
            "калибратор",
            "контрольный материал",
        )
    ):
        return SEGMENTS["medical_diagnostics"]

    if _starts(normalized_code, ("26.60", "32.50")) or any(
        token in text
        for token in (
            "медицинск",
            "анестезиолог",
            "спирометр",
            "томограф",
            "ультразвуков",
            "пациента",
        )
    ):
        return SEGMENTS["medical_equipment"]

    if _starts(normalized_code, ("26.20", "26.30", "26.40")) or any(
        token in text for token in ("ноутбук", "компьютер", "сервер", "монитор", "принтер")
    ):
        return SEGMENTS["it_goods"]

    # Buildings are commercially distinct from construction contractors. A
    # municipality buying an apartment should not compete in the same ranking
    # with road reconstruction or finishing works.
    if normalized_code.startswith("41."):
        return SEGMENTS["real_estate"]

    if _starts(normalized_code, ("42.", "43.")):
        return SEGMENTS["construction_works"]

    # OKPD2/KTRU product classes are mostly below 33. Everything left in that
    # range is treated as a standard good for commercial screening.
    if 1 <= major <= 32:
        return SEGMENTS["standard_goods"]
    if 45 <= major <= 99:
        return SEGMENTS["other_services"]
    return SEGMENTS["other"]


def segment_codes() -> tuple[str, ...]:
    return tuple(SEGMENTS)
