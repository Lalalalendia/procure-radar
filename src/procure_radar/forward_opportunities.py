from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
import math
import re
import sqlite3
import time
from typing import Any, Iterable

import httpx

from .client import GosplanClient
from .entry_opportunity import procurement_contract_coverage
from .extract import extract_purchase, extract_tenderplan
from .ingest import ingest_purchase, ingest_tenderplan
from .regional_demand import okpd2_family, regional_demand_buyers, regional_manufacturer_demand


_SEMANTIC_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_SEMANTIC_SUFFIXES = tuple(sorted({
    "иями", "ями", "ами", "его", "ого", "ему", "ому", "ими", "ыми",
    "иях", "ях", "ах", "иям", "ям", "ам", "ией",
    "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ую", "юю",
    "ых", "их", "ым", "им", "ом", "ем", "ов", "ев", "ей", "ью",
    "ия", "ию", "ья", "а", "я", "ы", "и", "у", "ю", "е", "о",
}, key=len, reverse=True))
_SEMANTIC_GENERIC_WORDS = {
    "поставка", "закупка", "оказание", "выполнение", "приобретение", "проведение",
    "работа", "работы", "услуга", "услуги", "товар", "товары", "нужда", "нужды",
    "объект", "объекта", "для", "по", "на", "в", "из", "с", "и", "или",
    "государственный", "муниципальный", "федеральный", "республика", "башкортостан",
    # Project/location boilerplate is common in tender-plan object names and
    # must never act as product evidence.  Keep it available to the separate
    # procurement-intent classifier below, but remove it from semantic product
    # vectors entirely.
    "национальный", "проект", "программа", "строительство", "строительный",
    "реконструкция", "капитальный", "ремонт", "район", "городской", "округ",
    "город", "село", "деревня", "улица", "дом", "здание", "помещение",
    "участок", "адрес", "область", "край", "оборудование", "система", "сеть",
    "сети", "часть", "отдел", "главный", "фгбоу", "угнту", "мр", "рб",
    # Buyer/institution words are not product identity even if they repeat in
    # exact-family history.
    "гбуз", "мбоу", "сош", "црб", "кбсмп", "фап", "учреждение",
    "больница", "поликлиника", "школа", "корпус",
}
_SEMANTIC_MIN_SCORE = 52.0
_HISTORY_DATE_EXPR = (
    "COALESCE(NULLIF(c.published_at,''), NULLIF(c.doc_created_at,''), "
    "NULLIF(c.doc_updated_at,''), NULLIF(c.updated_at,''), NULLIF(c.elact_at,''))"
)


def _semantic_stem(token: str) -> str:
    value = str(token or "").lower().replace("ё", "е").strip()
    if len(value) <= 4 or value.isdigit():
        return value
    for suffix in _SEMANTIC_SUFFIXES:
        if value.endswith(suffix) and len(value) - len(suffix) >= 4:
            return value[: -len(suffix)]
    return value


_SEMANTIC_STOP_STEMS = {_semantic_stem(word) for word in _SEMANTIC_GENERIC_WORDS}
_SEMANTIC_NON_ANCHOR_WORDS = {
    # Procurement/action words can be useful for intent, but are not product
    # identity.  In particular, ``установка`` as a noun/action must not make
    # arbitrary equipment in a heat node look like a boiler-house product.
    "установка", "монтаж", "замена", "модернизация",
    # Customer/institution vocabulary is frequent in exact-family history but
    # describes the buyer, not the product.
    "гбуз", "мбоу", "сош", "црб", "кбсмп", "фап", "учреждение",
    "больница", "поликлиника", "школа", "корпус",
}
_SEMANTIC_NON_ANCHOR_STEMS = {_semantic_stem(word) for word in _SEMANTIC_NON_ANCHOR_WORDS}


def _semantic_procurement_intent(text: Any) -> dict[str, Any]:
    """Classify whether a text contains a sale/install opportunity.

    Service language has priority over ambiguous nouns such as ``установка``.
    A diagnostic/maintenance contract that happens to mention an installation
    is still a service.  Works become an equipment opportunity only through a
    real construction/reconstruction/installation/replacement action.
    """

    value = str(text or "").lower().replace("ё", "е")
    direct_supply = any(
        marker in value
        for marker in (
            "поставк", "приобрет", "закуп", "изготов", "производств", "комплектован",
        )
    )
    service_only = any(
        marker in value
        for marker in (
            "оказание услуг", "оказан услуг", "техническ обслуживание",
            "техническому обслуживанию", "обслуживан", "сервис", "диагност",
            "поверк", "испытан", "аренд", "эксплуатац", "авторск надзор",
            "техническ надзор", "метролог", "обследован", "капитальн ремонт",
            "капремонт", "ремонт",
        )
    )
    explicit_install_work = any(
        marker in value
        for marker in (
            "с установк", "с монтаж", "монтаж", "замен", "модернизац",
            "строительств", "реконструкц",
        )
    )

    if direct_supply:
        intent = "direct_product_supply"
        multiplier = 1.0
    elif service_only and not explicit_install_work:
        intent = "service_only"
        multiplier = 0.35
    elif explicit_install_work:
        intent = "embedded_product_work"
        multiplier = 0.80
    elif service_only:
        # Defensive fallback for mixed wording.  Reaching this branch means a
        # service marker coexists with a work marker; keep it conservative.
        intent = "service_only"
        multiplier = 0.35
    else:
        intent = "product_unspecified"
        multiplier = 0.90
    return {
        "intent": intent,
        "confidence_multiplier": multiplier,
        "direct_supply": direct_supply,
        "service_only": service_only,
        "embedded_product": explicit_install_work,
    }


def _semantic_terms(text: Any) -> list[str]:
    terms: list[str] = []
    for raw in _SEMANTIC_TOKEN_RE.findall(str(text or "")):
        stem = _semantic_stem(raw)
        if len(stem) < 3 or stem in _SEMANTIC_STOP_STEMS or stem.isdigit():
            continue
        terms.append(stem)
    return terms


def _semantic_features(text: Any) -> set[str]:
    terms = _semantic_terms(text)
    features = {f"t:{term}" for term in terms}
    features.update(f"b:{left} {right}" for left, right in zip(terms, terms[1:]) if left != right)
    # Plan subjects often insert one modifier between the same product words:
    # "блочная котельная" vs "блочно-модульная котельная".  Preserve that
    # relation as a weaker skip-one phrase feature instead of forcing a lower
    # global semantic threshold.
    features.update(
        f"p:{terms[idx]} {terms[idx + 2]}"
        for idx in range(len(terms) - 2)
        if terms[idx] != terms[idx + 2]
    )
    return features


def _semantic_vector(
    features: set[str],
    *,
    background_df: Counter[str],
    background_n: int,
) -> dict[str, float]:
    return {
        feature: math.log((background_n + 1.0) / (background_df.get(feature, 0) + 1.0)) + 1.0
        for feature in features
    }


def _semantic_vector_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    shared = left.keys() & right.keys()
    if not shared:
        return 0.0
    dot = sum(left[feature] * right[feature] for feature in shared)
    left_norm = math.sqrt(sum(weight * weight for weight in left.values()))
    right_norm = math.sqrt(sum(weight * weight for weight in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _semantic_product_profile(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    manufacturer_families: set[str],
    region_code: int,
    history_year: int,
) -> dict[str, Any]:
    product_names: set[str] = set()
    for row in conn.execute(
        """
        SELECT gp.product_name AS name, gp.okpd2_code AS code
        FROM gisp_products gp
        WHERE gp.manufacturer_inn=? AND (gp.is_active=1 OR gp.is_active IS NULL)
        UNION ALL
        SELECT gr.product_name AS name, gr.okpd2_code AS code
        FROM gisp_registry_rows gr
        JOIN gisp_products gp ON gp.registry_number=gr.registry_number
        WHERE gp.manufacturer_inn=? AND (gp.is_active=1 OR gp.is_active IS NULL)
        """,
        (manufacturer_inn, manufacturer_inn),
    ):
        if okpd2_family(row["code"]) in manufacturer_families and row["name"]:
            product_names.add(str(row["name"]).strip())

    background_subjects: dict[int, str] = {}
    for row in conn.execute(
        f"""
        SELECT c.id, c.subject
        FROM contracts c
        WHERE c.region_code=?
          AND substr({_HISTORY_DATE_EXPR},1,4)=?
          AND NULLIF(trim(c.subject),'') IS NOT NULL
        """,
        (region_code, str(history_year)),
    ):
        background_subjects[int(row["id"])] = str(row["subject"]).strip()

    positive_ids: set[int] = set()
    for row in conn.execute(
        f"""
        SELECT cc.contract_id, cc.code
        FROM contract_codes cc
        JOIN contracts c ON c.id=cc.contract_id
        WHERE c.region_code=?
          AND substr({_HISTORY_DATE_EXPR},1,4)=?
          AND cc.system IN ('okpd2','ktru')
        """,
        (region_code, str(history_year)),
    ):
        if okpd2_family(row["code"]) in manufacturer_families:
            positive_ids.add(int(row["contract_id"]))
    historical_subjects_raw = [background_subjects[cid] for cid in positive_ids if cid in background_subjects]
    # Contract history can contain repeated editions / duplicated subjects.
    # Deduplicate by normalized text so a repeated buyer-specific phrase such
    # as an institution name cannot become "product vocabulary" merely because
    # the same subject appeared twice.
    historical_subjects: list[str] = []
    seen_subjects: set[str] = set()
    historical_subjects_rejected_by_intent = 0
    for subject in historical_subjects_raw:
        # Exact OKPD2 history can still contain utilities, maintenance and
        # repair records.  Those are useful for market accounting but unsafe
        # as product-language training examples.  Learn semantic product
        # wording only from direct supply or works that can actually embed a
        # product installation.
        history_intent = _semantic_procurement_intent(subject)
        if history_intent["intent"] not in {"direct_product_supply", "embedded_product_work"}:
            historical_subjects_rejected_by_intent += 1
            continue
        key = re.sub(r"\s+", " ", subject.lower().replace("ё", "е")).strip()
        if not key or key in seen_subjects:
            continue
        seen_subjects.add(key)
        historical_subjects.append(subject)

    background_df: Counter[str] = Counter()
    for subject in background_subjects.values():
        background_df.update(_semantic_features(subject))
    background_n = max(1, len(background_subjects))

    documents: list[dict[str, Any]] = []
    for name in sorted(product_names):
        features = _semantic_features(name)
        if features:
            documents.append({"source": "gisp", "text": name, "features": features})
    for text in historical_subjects:
        features = _semantic_features(text)
        if features:
            documents.append({"source": "history", "text": text, "features": features})

    for doc in documents:
        doc["vector"] = _semantic_vector(
            doc["features"], background_df=background_df, background_n=background_n
        )

    # One OKPD2 family may contain several genuinely different products.  A
    # single centroid dilutes short plan subjects badly, so first partition the
    # positive evidence into connected semantic components.  Rare shared tokens
    # and repeated bigrams are strong enough to connect neighbouring wording.
    parent = list(range(len(documents)))

    def find(idx: int) -> int:
        while parent[idx] != idx:
            parent[idx] = parent[parent[idx]]
            idx = parent[idx]
        return idx

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(documents)):
        for right in range(left + 1, len(documents)):
            shared = documents[left]["features"] & documents[right]["features"]
            if not shared:
                continue
            shared_phrases = {feature for feature in shared if feature.startswith(("b:", "p:"))}
            shared_tokens = {feature for feature in shared if feature.startswith("t:")}
            rare_shared_token = any(
                math.log((background_n + 1.0) / (background_df.get(feature, 0) + 1.0)) + 1.0 >= 2.35
                for feature in shared_tokens
            )
            cosine = _semantic_vector_cosine(documents[left]["vector"], documents[right]["vector"])
            if shared_phrases or len(shared_tokens) >= 2 or rare_shared_token or cosine >= 0.42:
                union(left, right)

    components: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(documents)):
        components[find(idx)].append(idx)

    clusters: list[dict[str, Any]] = []
    aggregate_centroid: dict[str, float] = defaultdict(float)
    for cluster_number, members in enumerate(components.values(), 1):
        docs = [documents[idx] for idx in members]
        gisp_count = sum(1 for doc in docs if doc["source"] == "gisp")
        history_count = sum(1 for doc in docs if doc["source"] == "history")
        support: Counter[str] = Counter()
        gisp_features: set[str] = set()
        for doc in docs:
            support.update(doc["features"])
            if doc["source"] == "gisp":
                gisp_features.update(doc["features"])

        # Historical one-offs are not allowed to become semantic vocabulary on
        # their own.  GISP product wording is authoritative; historical wording
        # must repeat in at least two exact-family contracts inside the cluster.
        core_features = {
            feature
            for feature, count in support.items()
            if feature in gisp_features or count >= 2
        }
        # A skip-one phrase may occur in only one historical wording even when
        # both of its product tokens are repeatedly supported by the cluster.
        # Keep that relation as a bridge between concise and modified names.
        for feature in support:
            if not feature.startswith("p:"):
                continue
            parts = feature[2:].split(" ", 1)
            if len(parts) == 2 and f"t:{parts[0]}" in core_features and f"t:{parts[1]}" in core_features:
                core_features.add(feature)
        eligible = bool(core_features) and (gisp_count > 0 or history_count >= 2)
        cluster_vector: dict[str, float] = {}
        for feature in core_features:
            idf = math.log((background_n + 1.0) / (background_df.get(feature, 0) + 1.0)) + 1.0
            support_ratio = support[feature] / max(1, len(docs))
            source_boost = 1.20 if feature in gisp_features else 1.0
            cluster_vector[feature] = idf * (0.70 + 0.30 * support_ratio) * source_boost
        cluster_vector = dict(
            sorted(cluster_vector.items(), key=lambda item: (-item[1], item[0]))[:80]
        )
        cluster_confidence = min(
            1.0,
            0.45 + 0.22 * min(gisp_count, 1) + 0.10 * min(history_count, 4),
        ) if eligible else 0.0
        for feature, weight in cluster_vector.items():
            aggregate_centroid[feature] += cluster_confidence * weight
        clusters.append(
            {
                "cluster_id": f"cluster_{cluster_number}",
                "eligible": eligible,
                "gisp_documents": gisp_count,
                "historical_documents": history_count,
                "confidence": round(cluster_confidence, 4),
                "vector": cluster_vector,
                "core_features": set(cluster_vector),
                "examples": [doc["text"] for doc in docs[:4]],
            }
        )

    centroid = dict(
        sorted(aggregate_centroid.items(), key=lambda item: (-item[1], item[0]))[:120]
    )
    product_count = len(product_names)
    history_count = len(historical_subjects)
    eligible_clusters = [cluster for cluster in clusters if cluster["eligible"]]
    profile_confidence = min(1.0, 0.35 + 0.20 * min(product_count, 2) + 0.10 * min(history_count, 4))
    if not eligible_clusters:
        status = "unavailable"
        profile_confidence = 0.0
    elif product_count and history_count >= 2:
        status = "good"
    else:
        status = "limited"

    return {
        "status": status,
        "profile_confidence": round(profile_confidence, 4),
        "product_names": sorted(product_names),
        "historical_subjects": historical_subjects,
        "centroid": centroid,
        "clusters": clusters,
        "background_df": background_df,
        "background_n": background_n,
        "background_documents": len(background_subjects),
        "historical_subjects_raw": len(historical_subjects_raw),
        "historical_subjects_rejected_by_intent": historical_subjects_rejected_by_intent,
        "vocabulary_features": len(centroid),
    }


def _semantic_score(text: Any, profile: dict[str, Any]) -> dict[str, Any] | None:
    if not str(text or "").strip():
        return None
    features = _semantic_features(text)
    if not features:
        return None
    candidate_vector = _semantic_vector(
        features,
        background_df=profile.get("background_df") or Counter(),
        background_n=int(profile.get("background_n") or 1),
    )
    intent = _semantic_procurement_intent(text)
    best: dict[str, Any] | None = None
    for cluster in profile.get("clusters") or ():
        if not cluster.get("eligible"):
            continue
        vector: dict[str, float] = cluster.get("vector") or {}
        shared = features & vector.keys()
        if not shared:
            continue
        shared_tokens = {feature for feature in shared if feature.startswith("t:")}
        shared_bigrams = {feature for feature in shared if feature.startswith("b:")}
        shared_skip_phrases = {feature for feature in shared if feature.startswith("p:")}
        shared_anchor_tokens = {
            feature
            for feature in shared_tokens
            if feature[2:] not in _SEMANTIC_NON_ANCHOR_STEMS
        }
        shared_product_phrases = {
            feature
            for feature in (shared_bigrams | shared_skip_phrases)
            if any(part not in _SEMANTIC_NON_ANCHOR_STEMS for part in feature[2:].split())
        }
        cosine = _semantic_vector_cosine(candidate_vector, vector)
        candidate_weight = sum(candidate_vector.values()) or 1.0
        shared_weight = sum(candidate_vector.get(feature, 0.0) for feature in shared)
        precision = min(1.0, shared_weight / candidate_weight)
        # A high semantic score is not enough: require a phrase from the
        # product cluster or at least two independent product-anchor tokens.
        # Domain/context tokens such as ``установка`` cannot make unrelated
        # heat-node equipment a match by themselves.
        strong_evidence = bool(shared_product_phrases) or len(shared_anchor_tokens) >= 2
        # Short plan subjects contain many location/project qualifiers that are
        # absent from the historical product wording.  Full-vector cosine
        # therefore under-rates a very useful signal: two rare core product
        # tokens.  Score explicit repeated evidence first, then use cosine and
        # candidate precision only as confirmation.  One-token coincidences
        # remain far below the acceptance threshold.
        # Three independent product-core tokens are materially stronger than
        # two (e.g. "блочн + котельн + установк").  Generic project/location
        # tokens were removed before this point, so the third token can safely
        # increase evidence without reopening the old boilerplate false positives.
        score = 22.0 * min(3, len(shared_tokens))
        score += 30.0 if shared_bigrams else 0.0
        score += 24.0 if shared_skip_phrases else 0.0
        score += 18.0 * cosine
        score += 12.0 * precision
        if not strong_evidence:
            score *= 0.72
        score = max(0.0, min(100.0, score))
        cluster_confidence = float(cluster.get("confidence") or 0.0)
        profile_confidence = float(profile.get("profile_confidence") or 0.0)
        confidence = (
            profile_confidence
            * cluster_confidence
            * min(0.90, 0.30 + 0.0065 * score)
            * float(intent["confidence_multiplier"])
        )
        evidence_features = sorted(shared, key=lambda feature: (-vector.get(feature, 0.0), feature))[:8]
        evidence = [feature[2:] for feature in evidence_features]
        candidate = {
            "score": round(score, 2),
            "confidence": round(max(0.0, min(0.90, confidence)), 4),
            "evidence": evidence,
            "cosine_similarity": round(cosine, 4),
            "strong_evidence": strong_evidence,
            "product_anchor_tokens": sorted(feature[2:] for feature in shared_anchor_tokens),
            "product_anchor_phrases": sorted(feature[2:] for feature in shared_product_phrases),
            "procurement_intent": intent["intent"],
            "procurement_intent_confidence_multiplier": intent["confidence_multiplier"],
            "cluster_id": cluster.get("cluster_id"),
            "cluster_confidence": round(cluster_confidence, 4),
        }
        if best is None or (candidate["score"], candidate["confidence"]) > (best["score"], best["confidence"]):
            best = candidate
    return best


def _semantic_match(text: Any, profile: dict[str, Any]) -> dict[str, Any] | None:
    scored = _semantic_score(text, profile)
    if (
        scored is None
        or float(scored["score"]) < _SEMANTIC_MIN_SCORE
        or not bool(scored.get("strong_evidence"))
        or scored.get("procurement_intent") == "service_only"
    ):
        return None
    return scored


def _semantic_profile_public(profile: dict[str, Any]) -> dict[str, Any]:
    centroid: dict[str, float] = profile.get("centroid") or {}
    top_features = [
        {
            "feature": feature[2:],
            "kind": "bigram" if feature.startswith("b:") else ("skip_phrase" if feature.startswith("p:") else "token"),
            "weight": round(weight, 4),
        }
        for feature, weight in sorted(centroid.items(), key=lambda item: (-item[1], item[0]))[:12]
    ]
    public_clusters: list[dict[str, Any]] = []
    for cluster in profile.get("clusters") or ():
        vector: dict[str, float] = cluster.get("vector") or {}
        public_clusters.append(
            {
                "cluster_id": cluster.get("cluster_id"),
                "eligible": bool(cluster.get("eligible")),
                "gisp_documents": int(cluster.get("gisp_documents") or 0),
                "historical_documents": int(cluster.get("historical_documents") or 0),
                "confidence": cluster.get("confidence"),
                "top_features": [
                    feature[2:]
                    for feature, _ in sorted(vector.items(), key=lambda item: (-item[1], item[0]))[:8]
                ],
                "examples": list(cluster.get("examples") or ())[:3],
            }
        )
    return {
        "status": profile.get("status"),
        "profile_confidence": profile.get("profile_confidence"),
        "match_engine": "clustered_semantic_prototypes_v4_product_anchor_intent",
        "gisp_product_names": len(profile.get("product_names") or ()),
        "historical_exact_family_subjects": len(profile.get("historical_subjects") or ()),
        "historical_exact_family_subjects_raw": profile.get("historical_subjects_raw", 0),
        "historical_subjects_rejected_by_intent": profile.get("historical_subjects_rejected_by_intent", 0),
        "background_contract_subjects": profile.get("background_documents", 0),
        "vocabulary_features": profile.get("vocabulary_features", 0),
        "semantic_min_score": _SEMANTIC_MIN_SCORE,
        "semantic_clusters": public_clusters,
        "top_features": top_features,
        "product_name_examples": list(profile.get("product_names") or ())[:5],
        "historical_subject_examples": list(profile.get("historical_subjects") or ())[:5],
    }

def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _chunks(values: list[int], size: int = 800) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _log_score(value: float | None, *, low: float = 100_000.0, high: float = 100_000_000.0) -> float:
    if value is None or value <= low:
        return 0.0
    if value >= high:
        return 100.0
    return max(0.0, min(100.0, (math.log10(value) - math.log10(low)) / (math.log10(high) - math.log10(low)) * 100.0))


def _tier(score: float) -> str:
    if score >= 70:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


def _days_until(value: str | None, as_of: date) -> int | None:
    parsed = _parse_date(value)
    return (parsed - as_of).days if parsed else None


def _deadline_score(days: int | None) -> float:
    if days is None:
        return 45.0
    if days < 0:
        return 0.0
    if days <= 2:
        return 25.0
    if days <= 14:
        return 100.0
    if days <= 30:
        return 90.0
    if days <= 60:
        return 75.0
    return 55.0


def _planned_timing_score(planned: date | None, as_of: date) -> float:
    if planned is None:
        return 45.0
    days = (planned - as_of).days
    if days < -90:
        return 15.0
    if days < 0:
        return 55.0  # overdue plan position: uncertain, but a notice may still be pending
    if days <= 90:
        return 100.0
    if days <= 180:
        return 85.0
    if days <= 365:
        return 70.0
    if days <= 730:
        return 50.0
    return 30.0


def _names(conn: sqlite3.Connection, inns: Iterable[str]) -> dict[str, str | None]:
    values = sorted({str(v).strip() for v in inns if str(v).strip()})
    result = {inn: None for inn in values}
    for start in range(0, len(values), 800):
        chunk = values[start : start + 800]
        if not chunk:
            continue
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(f"SELECT inn, name FROM organizations WHERE inn IN ({marks})", tuple(chunk)):
            result[str(row["inn"])] = str(row["name"]) if row["name"] else None
    return result


def _buyer_history(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    region_code: int,
    history_year: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    coverage = procurement_contract_coverage(conn, region_code=region_code, year=history_year)
    report = regional_demand_buyers(
        conn,
        manufacturer_inn=manufacturer_inn,
        region_code=region_code,
        year=history_year,
        history_confidence=float(coverage["confidence"]),
        history_status=str(coverage["status"]),
        limit=100000,
        offset=0,
        contracts_per_buyer=0,
    )
    return {str(row["buyer_inn"]): row for row in report["rows"] if row.get("buyer_inn")}, coverage


def _manufacturer_families(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    region_code: int,
    history_year: int,
    coverage: dict[str, Any],
) -> set[str]:
    summary = regional_manufacturer_demand(
        conn,
        manufacturer_inn=manufacturer_inn,
        region_code=region_code,
        year=history_year,
        history_confidence=float(coverage["confidence"]),
        history_status=str(coverage["status"]),
    )
    return set(summary["manufacturer_okpd2_families"])


def _purchase_codes(conn: sqlite3.Connection, purchase_ids: list[int]) -> dict[int, set[str]]:
    result: dict[int, set[str]] = defaultdict(set)
    for chunk in _chunks(purchase_ids):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT purchase_id, code FROM purchase_codes WHERE purchase_id IN ({marks}) AND system IN ('okpd2','ktru')",
            tuple(chunk),
        ):
            family = okpd2_family(row["code"])
            if family:
                result[int(row["purchase_id"])].add(family)
    return result


def _purchase_customers(conn: sqlite3.Connection, purchase_ids: list[int]) -> dict[int, set[str]]:
    result: dict[int, set[str]] = defaultdict(set)
    for chunk in _chunks(purchase_ids):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT purchase_id, inn FROM purchase_parties WHERE purchase_id IN ({marks}) AND role='customer'",
            tuple(chunk),
        ):
            result[int(row["purchase_id"])].add(str(row["inn"]))
    return result


def _matched_item_amounts(
    conn: sqlite3.Connection,
    purchase_ids: list[int],
    manufacturer_families: set[str],
) -> tuple[dict[int, float], set[int]]:
    amounts: dict[int, float] = defaultdict(float)
    detailed: set[int] = set()
    for chunk in _chunks(purchase_ids):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT purchase_id, okpd2_code, ktru_code, amount FROM purchase_items WHERE purchase_id IN ({marks})",
            tuple(chunk),
        ):
            purchase_id = int(row["purchase_id"])
            detailed.add(purchase_id)
            family = okpd2_family(row["okpd2_code"]) or okpd2_family(row["ktru_code"])
            if family in manufacturer_families and row["amount"] is not None:
                amounts[purchase_id] += max(0.0, float(row["amount"]))
    return amounts, detailed


def _current_opportunities(
    conn: sqlite3.Connection,
    *,
    manufacturer_families: set[str],
    semantic_profile: dict[str, Any],
    buyer_history: dict[str, dict[str, Any]],
    region_code: int,
    as_of: date,
    recent_notice_days: int,
) -> list[dict[str, Any]]:
    earliest = (as_of - timedelta(days=max(1, recent_notice_days))).isoformat()
    raw_rows = conn.execute(
        """
        SELECT id, purchase_number, published_at, collecting_finished_at, max_price,
               object_info, purchase_type, stage
        FROM purchases
        WHERE region_code=?
          AND (
            substr(COALESCE(collecting_finished_at,''),1,10) >= ?
            OR (
                NULLIF(collecting_finished_at,'') IS NULL
                AND substr(COALESCE(published_at,''),1,10) >= ?
            )
          )
        ORDER BY COALESCE(collecting_finished_at,published_at) ASC, max_price DESC
        """,
        (region_code, as_of.isoformat(), earliest),
    ).fetchall()
    purchase_ids = [int(row["id"]) for row in raw_rows]
    codes = _purchase_codes(conn, purchase_ids)
    customers = _purchase_customers(conn, purchase_ids)
    item_amounts, detailed_ids = _matched_item_amounts(conn, purchase_ids, manufacturer_families)
    all_inns = {inn for values in customers.values() for inn in values}
    names = _names(conn, all_inns)

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        purchase_id = int(raw["id"])
        all_families = codes.get(purchase_id, set())
        matched = all_families & manufacturer_families
        semantic = None
        if matched:
            match_type = "okpd2_exact"
            match_confidence = 1.0
            inferred_families: set[str] = set()
        elif not all_families:
            semantic = _semantic_match(raw["object_info"], semantic_profile)
            if semantic is None:
                continue
            match_type = "semantic_product"
            match_confidence = float(semantic["confidence"])
            inferred_families = set(manufacturer_families)
        else:
            # Explicit classifier evidence takes precedence over text.  The
            # semantic fallback is only for notices that have no classifier.
            continue
        deadline = _parse_date(raw["collecting_finished_at"])
        if deadline is not None and deadline < as_of:
            continue
        status = "open" if deadline is not None else "recent_notice_unknown_deadline"
        max_price = float(raw["max_price"]) if raw["max_price"] is not None else None
        if matched and purchase_id in detailed_ids and item_amounts.get(purchase_id, 0.0) > 0:
            value = item_amounts[purchase_id]
            value_basis = "matched_purchase_items"
            classifier_confidence = 1.0
        elif matched and max_price is not None and all_families:
            value = max_price * len(matched) / len(all_families)
            value_basis = "max_price_family_split"
            classifier_confidence = 0.85
        elif semantic is not None:
            value = max_price
            value_basis = "max_price_semantic_product_match"
            classifier_confidence = min(0.75, match_confidence)
        else:
            value = max_price
            value_basis = "max_price"
            classifier_confidence = 0.70

        buyer_inns = sorted(customers.get(purchase_id, set()))
        historical = [buyer_history[inn] for inn in buyer_inns if inn in buyer_history]
        history_score = max((float(row["buyer_opportunity_score"]) for row in historical), default=0.0)
        contestability = max((float(row["evidenced_contestability_score"]) for row in historical), default=0.0)
        days = _days_until(raw["collecting_finished_at"], as_of)
        score = 0.45 * _log_score(value) + 0.35 * history_score + 0.20 * _deadline_score(days)
        confidence_parts = [classifier_confidence, 1.0 if value is not None else 0.5, 1.0 if deadline else 0.6]
        confidence_parts.append(1.0 if historical else 0.6)
        confidence = sum(confidence_parts) / len(confidence_parts)
        evidenced = score * confidence
        buyer_name = next((names.get(inn) for inn in buyer_inns if names.get(inn)), None)
        rows.append(
            {
                "source": "purchase_notice",
                "status": status,
                "purchase_number": raw["purchase_number"],
                "object_info": raw["object_info"],
                "published_at": raw["published_at"],
                "collecting_finished_at": raw["collecting_finished_at"],
                "days_to_deadline": days,
                "buyer_inns": buyer_inns,
                "buyer_name": buyer_name,
                "match_type": match_type,
                "match_confidence": round(match_confidence, 4),
                "matched_okpd2_families": sorted(matched),
                "inferred_okpd2_families": sorted(inferred_families),
                "semantic_score": semantic["score"] if semantic else None,
                "semantic_evidence": semantic["evidence"] if semantic else [],
                "semantic_procurement_intent": semantic.get("procurement_intent") if semantic else None,
                "semantic_intent_confidence_multiplier": semantic.get("procurement_intent_confidence_multiplier") if semantic else None,
                "semantic_cluster_id": semantic.get("cluster_id") if semantic else None,
                "semantic_cluster_confidence": semantic.get("cluster_confidence") if semantic else None,
                "estimated_addressable_value_rub": round(value, 2) if value is not None else None,
                "value_basis": value_basis,
                "historical_buyer_opportunity_score": round(history_score, 2),
                "historical_contestability_score": round(contestability, 2),
                "forward_opportunity_score": round(score, 2),
                "forward_opportunity_confidence": round(confidence, 4),
                "evidenced_forward_opportunity_score": round(evidenced, 2),
                "forward_opportunity_tier": _tier(evidenced),
                "purchase_type": raw["purchase_type"],
                "stage": raw["stage"],
            }
        )
    rows.sort(key=lambda row: (row["evidenced_forward_opportunity_score"], row.get("estimated_addressable_value_rub") or 0), reverse=True)
    return rows


def _linked_plan_values(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["value"])
        for row in conn.execute(
            "SELECT DISTINCT value FROM purchase_links WHERE kind IN ('position_number','ikz') AND NULLIF(value,'') IS NOT NULL"
        )
    }


def _planned_opportunities(
    conn: sqlite3.Connection,
    *,
    manufacturer_families: set[str],
    semantic_profile: dict[str, Any],
    buyer_history: dict[str, dict[str, Any]],
    region_code: int,
    as_of: date,
    horizon_days: int,
) -> list[dict[str, Any]]:
    # Use plan region when available; retain positions with an explicit local
    # customer even if the aggregate plan omitted its region projection.
    position_rows = conn.execute(
        """
        SELECT tp.plan_number, tp.published_at AS plan_published_at, tp.plan_year,
               tp.region_code, pos.id AS position_id, pos.position_key,
               pos.position_number, pos.ikz, pos.customer_inn, pos.object_info,
               pos.planned_at, pos.planned_year, pos.planned_month, pos.amount
        FROM tenderplan_positions pos
        JOIN tenderplans tp ON tp.id=pos.tenderplan_id
        WHERE (tp.region_code=? OR tp.region_code IS NULL)
        """,
        (region_code,),
    ).fetchall()
    if not position_rows:
        return []
    ids = [int(row["position_id"]) for row in position_rows]
    codes: dict[int, set[str]] = defaultdict(set)
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT position_id, code FROM tenderplan_position_codes WHERE position_id IN ({marks}) AND system IN ('okpd2','ktru')",
            tuple(chunk),
        ):
            family = okpd2_family(row["code"])
            if family:
                codes[int(row["position_id"])].add(family)

    linked = _linked_plan_values(conn)
    customer_inns = {str(row["customer_inn"]) for row in position_rows if row["customer_inn"]}
    names = _names(conn, customer_inns)
    horizon = as_of + timedelta(days=max(1, horizon_days))
    rows: list[dict[str, Any]] = []
    for raw in position_rows:
        position_id = int(raw["position_id"])
        position_families = codes.get(position_id, set())
        matched = position_families & manufacturer_families
        semantic = None
        if matched:
            match_type = "okpd2_exact"
            match_confidence = 1.0
            inferred_families: set[str] = set()
        elif not position_families:
            semantic = _semantic_match(raw["object_info"], semantic_profile)
            if semantic is None:
                continue
            match_type = "semantic_product"
            match_confidence = float(semantic["confidence"])
            inferred_families = set(manufacturer_families)
        else:
            # Never override an explicit non-matching classifier with text.
            continue
        position_number = str(raw["position_number"] or "").strip() or None
        ikz = str(raw["ikz"] or "").strip() or None
        if (position_number and position_number in linked) or (ikz and ikz in linked):
            continue
        planned = _parse_date(raw["planned_at"])
        if planned is None and raw["planned_year"]:
            try:
                planned = date(int(raw["planned_year"]), int(raw["planned_month"] or 1), 1)
            except ValueError:
                planned = None
        if planned is not None and planned > horizon:
            continue
        if planned is not None and planned < as_of - timedelta(days=180):
            continue
        customer_inn = str(raw["customer_inn"] or "").strip() or None
        hist = buyer_history.get(customer_inn or "")
        history_score = float(hist["buyer_opportunity_score"]) if hist else 0.0
        contestability = float(hist["evidenced_contestability_score"]) if hist else 0.0
        amount = float(raw["amount"]) if raw["amount"] is not None else None
        timing = _planned_timing_score(planned, as_of)
        score = 0.50 * _log_score(amount) + 0.30 * history_score + 0.20 * timing
        classifier_confidence = 1.0 if matched else min(0.85, match_confidence)
        confidence_parts = [classifier_confidence, 1.0 if amount is not None else 0.55, 1.0 if planned is not None else 0.6, 1.0 if hist else 0.6]
        confidence = sum(confidence_parts) / len(confidence_parts)
        evidenced = score * confidence
        rows.append(
            {
                "source": "tenderplan",
                "status": "planned" if planned is None or planned >= as_of else "overdue_plan_without_notice",
                "plan_number": raw["plan_number"],
                "position_number": position_number,
                "ikz": ikz,
                "object_info": raw["object_info"],
                "plan_published_at": raw["plan_published_at"],
                "planned_at": planned.isoformat() if planned else raw["planned_at"],
                "planned_year": raw["planned_year"],
                "planned_month": raw["planned_month"],
                "buyer_inn": customer_inn,
                "buyer_name": names.get(customer_inn or ""),
                "match_type": match_type,
                "match_confidence": round(match_confidence, 4),
                "matched_okpd2_families": sorted(matched),
                "inferred_okpd2_families": sorted(inferred_families),
                "semantic_score": semantic["score"] if semantic else None,
                "semantic_evidence": semantic["evidence"] if semantic else [],
                "semantic_procurement_intent": semantic.get("procurement_intent") if semantic else None,
                "semantic_intent_confidence_multiplier": semantic.get("procurement_intent_confidence_multiplier") if semantic else None,
                "semantic_cluster_id": semantic.get("cluster_id") if semantic else None,
                "semantic_cluster_confidence": semantic.get("cluster_confidence") if semantic else None,
                "estimated_addressable_value_rub": round(amount, 2) if amount is not None else None,
                "value_basis": "tenderplan_position_amount" if amount is not None else "unknown",
                "historical_buyer_opportunity_score": round(history_score, 2),
                "historical_contestability_score": round(contestability, 2),
                "forward_opportunity_score": round(score, 2),
                "forward_opportunity_confidence": round(confidence, 4),
                "evidenced_forward_opportunity_score": round(evidenced, 2),
                "forward_opportunity_tier": _tier(evidenced),
            }
        )
    rows.sort(key=lambda row: (row["evidenced_forward_opportunity_score"], row.get("estimated_addressable_value_rub") or 0), reverse=True)
    return rows



def _semantic_match_diagnostics(
    conn: sqlite3.Connection,
    *,
    profile: dict[str, Any],
    region_code: int,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT tp.plan_number, pos.position_number, pos.object_info
        FROM tenderplan_positions pos
        JOIN tenderplans tp ON tp.id=pos.tenderplan_id
        WHERE (tp.region_code=? OR tp.region_code IS NULL)
          AND NOT EXISTS (
              SELECT 1 FROM tenderplan_position_codes pc
              WHERE pc.position_id=pos.id
          )
        """,
        (region_code,),
    ).fetchall()
    text_rows = 0
    scored_rows: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for row in rows:
        text = str(row["object_info"] or "").strip()
        if not text:
            continue
        text_rows += 1
        scored = _semantic_score(text, profile)
        if scored is None:
            continue
        item = {
            "plan_number": row["plan_number"],
            "position_number": row["position_number"],
            "object_info": text,
            "semantic_score": scored["score"],
            "match_confidence": scored["confidence"],
            "semantic_evidence": scored["evidence"],
            "semantic_cluster_id": scored.get("cluster_id"),
            "strong_evidence": bool(scored.get("strong_evidence")),
            "product_anchor_tokens": list(scored.get("product_anchor_tokens") or ()),
            "product_anchor_phrases": list(scored.get("product_anchor_phrases") or ()),
            "semantic_procurement_intent": scored.get("procurement_intent"),
            "semantic_intent_confidence_multiplier": scored.get("procurement_intent_confidence_multiplier"),
            "passed_threshold": (
                float(scored["score"]) >= _SEMANTIC_MIN_SCORE
                and bool(scored.get("strong_evidence"))
                and scored.get("procurement_intent") != "service_only"
            ),
        }
        if scored.get("procurement_intent") == "service_only":
            item["rejection_reason"] = "service_only"
        elif not item["strong_evidence"]:
            item["rejection_reason"] = "weak_product_evidence"
        elif float(scored["score"]) < _SEMANTIC_MIN_SCORE:
            item["rejection_reason"] = "below_score_threshold"
        else:
            item["rejection_reason"] = None
        scored_rows.append(item)
        if item["passed_threshold"]:
            matches.append(item)
    scored_rows.sort(key=lambda item: (float(item["semantic_score"]), float(item["match_confidence"])), reverse=True)
    matches.sort(key=lambda item: (float(item["semantic_score"]), float(item["match_confidence"])), reverse=True)
    return {
        "unclassified_tenderplan_positions_scanned": len(rows),
        "unclassified_positions_with_text": text_rows,
        "semantic_candidates_scored": len(scored_rows),
        "semantic_matches": len(matches),
        "semantic_rejected_service_only": sum(
            1 for row in scored_rows if row.get("rejection_reason") == "service_only"
        ),
        "semantic_rejected_weak_product_evidence": sum(
            1 for row in scored_rows if row.get("rejection_reason") == "weak_product_evidence"
        ),
        "semantic_direct_supply_matches": sum(
            1 for row in matches if row.get("semantic_procurement_intent") == "direct_product_supply"
        ),
        "semantic_embedded_product_work_matches": sum(
            1 for row in matches if row.get("semantic_procurement_intent") == "embedded_product_work"
        ),
        "semantic_product_unspecified_matches": sum(
            1 for row in matches if row.get("semantic_procurement_intent") == "product_unspecified"
        ),
        "semantic_high_score_matches": sum(1 for row in matches if float(row["semantic_score"]) >= 70.0),
        "semantic_medium_score_matches": sum(1 for row in matches if _SEMANTIC_MIN_SCORE <= float(row["semantic_score"]) < 70.0),
        "near_miss_score_45_plus": sum(1 for row in scored_rows if 45.0 <= float(row["semantic_score"]) < _SEMANTIC_MIN_SCORE),
        "near_miss_score_35_plus": sum(1 for row in scored_rows if 35.0 <= float(row["semantic_score"]) < 45.0),
        "top_semantic_matches": matches[:10],
        "top_semantic_candidates": scored_rows[:15],
    }

def _freshness(conn: sqlite3.Connection, *, region_code: int, as_of: date) -> dict[str, Any]:
    purchase_max = conn.execute(
        "SELECT MAX(substr(published_at,1,10)) FROM purchases WHERE region_code=?",
        (region_code,),
    ).fetchone()[0]
    plan_max = conn.execute(
        "SELECT MAX(substr(published_at,1,10)) FROM tenderplans WHERE region_code=? OR region_code IS NULL",
        (region_code,),
    ).fetchone()[0]

    def item(value: Any, missing_warning: str, stale_warning: str) -> tuple[dict[str, Any], list[str]]:
        parsed = _parse_date(value)
        warnings: list[str] = []
        if parsed is None:
            warnings.append(missing_warning)
            return {"latest_published_date": None, "lag_days": None, "status": "missing"}, warnings
        lag = max(0, (as_of - parsed).days)
        status = "good" if lag <= 2 else ("aging" if lag <= 7 else "stale")
        if status == "stale":
            warnings.append(stale_warning)
        return {"latest_published_date": parsed.isoformat(), "lag_days": lag, "status": status}, warnings

    purchase, wp = item(purchase_max, "current_purchase_data_missing", "current_purchase_data_stale")
    plans, wt = item(plan_max, "tenderplan_data_missing", "tenderplan_data_stale")
    return {"purchases": purchase, "tenderplans": plans, "warnings": wp + wt}


def forward_opportunities(
    conn: sqlite3.Connection,
    *,
    manufacturer_inn: str,
    region_code: int,
    as_of: str | date | None = None,
    history_year: int | None = None,
    current_limit: int = 20,
    planned_limit: int = 20,
    recent_notice_days: int = 45,
    planned_horizon_days: int = 730,
) -> dict[str, Any]:
    as_of_date = as_of if isinstance(as_of, date) else (_parse_date(as_of) if as_of else date.today())
    if as_of_date is None:
        raise ValueError("invalid --as-of date")
    history_year = history_year or (as_of_date.year - 1)
    buyer_history, coverage = _buyer_history(
        conn,
        manufacturer_inn=manufacturer_inn,
        region_code=region_code,
        history_year=history_year,
    )
    families = _manufacturer_families(
        conn,
        manufacturer_inn=manufacturer_inn,
        region_code=region_code,
        history_year=history_year,
        coverage=coverage,
    )
    semantic_profile = _semantic_product_profile(
        conn,
        manufacturer_inn=manufacturer_inn,
        manufacturer_families=families,
        region_code=region_code,
        history_year=history_year,
    ) if families else {"status": "unavailable", "profile_confidence": 0.0, "centroid": {}}
    current = _current_opportunities(
        conn,
        manufacturer_families=families,
        semantic_profile=semantic_profile,
        buyer_history=buyer_history,
        region_code=region_code,
        as_of=as_of_date,
        recent_notice_days=recent_notice_days,
    ) if families else []
    planned = _planned_opportunities(
        conn,
        manufacturer_families=families,
        semantic_profile=semantic_profile,
        buyer_history=buyer_history,
        region_code=region_code,
        as_of=as_of_date,
        horizon_days=planned_horizon_days,
    ) if families else []
    freshness = _freshness(conn, region_code=region_code, as_of=as_of_date)
    diagnostics = _forward_match_diagnostics(
        conn,
        manufacturer_families=families,
        region_code=region_code,
    )
    diagnostics["semantic_profile"] = _semantic_profile_public(semantic_profile)
    diagnostics["semantic_matching"] = _semantic_match_diagnostics(
        conn,
        profile=semantic_profile,
        region_code=region_code,
    ) if families else {
        "unclassified_tenderplan_positions_scanned": 0,
        "unclassified_positions_with_text": 0,
        "semantic_matches": 0,
        "semantic_high_score_matches": 0,
        "semantic_medium_score_matches": 0,
        "top_semantic_matches": [],
    }
    warnings = list(freshness["warnings"])
    if not families:
        warnings.append("manufacturer_has_no_gisp_okpd2_families")
    if diagnostics["tenderplan_positions_loaded"] and not diagnostics["tenderplan_positions_with_classifier"]:
        warnings.append("tenderplan_classifier_data_missing")
    elif diagnostics["tenderplan_classifier_coverage_status"] == "low":
        warnings.append("tenderplan_classifier_coverage_low")
    elif diagnostics["tenderplan_classifier_coverage_status"] == "partial":
        warnings.append("tenderplan_classifier_coverage_partial")
    if diagnostics["tenderplan_positions_with_classifier"] and not diagnostics["manufacturer_exact_family_matches"]:
        warnings.append("no_loaded_tenderplan_exact_family_matches")
    semantic_public = diagnostics["semantic_profile"]
    semantic_matching = diagnostics["semantic_matching"]
    if diagnostics["tenderplan_positions_without_classifier"] and semantic_public.get("status") == "unavailable":
        warnings.append("semantic_product_profile_unavailable")
    elif diagnostics["tenderplan_positions_without_classifier"]:
        warnings.append("semantic_fallback_used_for_unclassified_tenderplans")
    if semantic_matching.get("semantic_matches") and not planned:
        warnings.append("semantic_matches_exist_but_outside_forward_plan_filters")
    return {
        "manufacturer_inn": str(manufacturer_inn),
        "region_code": region_code,
        "as_of": as_of_date.isoformat(),
        "history_year": history_year,
        "manufacturer_okpd2_families": sorted(families),
        "current_opportunities_total": len(current),
        "planned_opportunities_total": len(planned),
        "current_opportunities_value_rub": round(sum(float(row.get("estimated_addressable_value_rub") or 0) for row in current), 2),
        "planned_opportunities_value_rub": round(sum(float(row.get("estimated_addressable_value_rub") or 0) for row in planned), 2),
        "current_opportunities": current[: max(0, int(current_limit))],
        "planned_opportunities": planned[: max(0, int(planned_limit))],
        "data_freshness": freshness,
        "match_diagnostics": diagnostics,
        "historical_contract_coverage": coverage,
        "warnings": warnings,
    }


def _pause(rate_per_minute: float | None) -> None:
    if rate_per_minute and rate_per_minute > 0:
        time.sleep(60.0 / rate_per_minute)


def _plan_detail_cached(conn: sqlite3.Connection, plan_number: str) -> bool:
    if conn.execute(
        "SELECT 1 FROM tenderplan_detail_fetches WHERE plan_number=? AND status='ok'",
        (plan_number,),
    ).fetchone() is not None:
        return True
    # Redundant durable marker: detail raw is stored under its own endpoint so
    # a subsequent list-row ingest cannot overwrite it.  This also lets older
    # databases recover if the fetch journal was not committed for any reason.
    return conn.execute(
        "SELECT 1 FROM raw_documents WHERE source='gosplan-v2' AND endpoint=? LIMIT 1",
        (f"/fz44/tenderplans/{plan_number}",),
    ).fetchone() is not None


def _mark_plan_detail(conn: sqlite3.Connection, plan_number: str, *, status: str, position_count: int = 0) -> None:
    conn.execute(
        """
        INSERT INTO tenderplan_detail_fetches(plan_number, fetched_at, status, position_count)
        VALUES (?, datetime('now'), ?, ?)
        ON CONFLICT(plan_number) DO UPDATE SET
            fetched_at=datetime('now'), status=excluded.status, position_count=excluded.position_count
        """,
        (plan_number, status, position_count),
    )


def _queue_plan_detail(conn: sqlite3.Connection, plan_number: str, *, refresh: bool = False) -> bool:
    """Persist a plan-detail request without discarding a completed cache row.

    The queue is stored in the existing fetch journal, so an invocation that
    exhausts its request budget can resume the outstanding plan numbers even
    if those plans have moved off the first API pages by the next run.
    """
    row = conn.execute(
        "SELECT status FROM tenderplan_detail_fetches WHERE plan_number=?",
        (plan_number,),
    ).fetchone()
    if row is None:
        _mark_plan_detail(conn, plan_number, status="pending")
        return True
    status = str(row["status"] or "")
    if refresh and status != "pending":
        _mark_plan_detail(conn, plan_number, status="pending")
        return True
    return status == "pending"


def _pending_plan_details(conn: sqlite3.Connection, *, region_code: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT f.plan_number
        FROM tenderplan_detail_fetches f
        LEFT JOIN tenderplans tp ON tp.plan_number=f.plan_number
        WHERE f.status='pending'
          AND (tp.region_code=? OR tp.region_code IS NULL)
        ORDER BY f.rowid ASC
        """,
        (region_code,),
    ).fetchall()
    return [str(row["plan_number"]) for row in rows]


def _seed_pending_plan_details_from_loaded(conn: sqlite3.Connection, *, region_code: int) -> int:
    """Recover pre-queue aggregate plans that were seen but never expanded."""
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO tenderplan_detail_fetches(plan_number, fetched_at, status, position_count)
        SELECT tp.plan_number, datetime('now'), 'pending', 0
        FROM tenderplans tp
        WHERE (tp.region_code=? OR tp.region_code IS NULL)
          AND NOT EXISTS (
              SELECT 1 FROM tenderplan_detail_fetches f
              WHERE f.plan_number=tp.plan_number
          )
          AND NOT EXISTS (
              SELECT 1 FROM raw_documents r
              WHERE r.source='gosplan-v2'
                AND r.endpoint='/fz44/tenderplans/' || tp.plan_number
          )
        """,
        (region_code,),
    )
    inserted = conn.total_changes - before
    if inserted:
        conn.commit()
    return int(inserted)


def _reindex_cached_tenderplan_details(conn: sqlite3.Connection) -> int:
    """Reparse already-fetched plan details with the current extractor.

    Early forward-opportunity builds stored aggregate and detail payloads under
    the same raw endpoint.  The detail fetch journal still tells us which plan
    numbers were successfully expanded.  Re-index those cached payloads before
    any network work so extractor fixes apply to an existing database for free.
    """
    rows = conn.execute(
        """
        SELECT f.plan_number,
               COALESCE(
                   (SELECT r.payload_json FROM raw_documents r
                    WHERE r.source='gosplan-v2'
                      AND r.endpoint='/fz44/tenderplans/' || f.plan_number
                    ORDER BY r.fetched_at DESC, r.id DESC LIMIT 1),
                   (SELECT r.payload_json FROM raw_documents r
                    WHERE r.source='gosplan-v2'
                      AND r.endpoint='/fz44/tenderplans'
                      AND r.external_id=f.plan_number
                    ORDER BY r.fetched_at DESC, r.id DESC LIMIT 1)
               ) AS payload_json
        FROM tenderplan_detail_fetches f
        WHERE f.status='ok'
        """
    ).fetchall()
    reindexed = 0
    for row in rows:
        raw = row["payload_json"]
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        parsed = extract_tenderplan(payload)
        if not parsed["positions"]:
            continue
        ingest_tenderplan(
            conn,
            payload,
            raw_endpoint=f"/fz44/tenderplans/{row['plan_number']}",
            preserve_existing_positions=False,
        )
        _mark_plan_detail(
            conn,
            str(row["plan_number"]),
            status="ok",
            position_count=len(parsed["positions"]),
        )
        reindexed += 1
    if reindexed:
        conn.commit()
    return reindexed


def _forward_match_diagnostics(
    conn: sqlite3.Connection,
    *,
    manufacturer_families: set[str],
    region_code: int,
) -> dict[str, Any]:
    position_rows = conn.execute(
        """
        SELECT pos.id
        FROM tenderplan_positions pos
        JOIN tenderplans tp ON tp.id=pos.tenderplan_id
        WHERE tp.region_code=? OR tp.region_code IS NULL
        """,
        (region_code,),
    ).fetchall()
    position_ids = [int(row["id"]) for row in position_rows]
    family_positions: dict[str, set[int]] = defaultdict(set)
    raw_codes = 0
    for chunk in _chunks(position_ids):
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT position_id, code FROM tenderplan_position_codes WHERE position_id IN ({marks})",
            tuple(chunk),
        ):
            raw_codes += 1
            family = okpd2_family(row["code"])
            if family:
                family_positions[family].add(int(row["position_id"]))

    positions_with_classifier = set().union(*family_positions.values()) if family_positions else set()
    exact_matches = set().union(
        *(family_positions.get(family, set()) for family in manufacturer_families)
    ) if manufacturer_families else set()
    target_groups = {family[:5] for family in manufacturer_families}
    broad_matches: set[int] = set()
    for family, ids in family_positions.items():
        if family[:5] in target_groups:
            broad_matches.update(ids)

    top_families = sorted(
        (
            {"family": family, "positions": len(ids)}
            for family, ids in family_positions.items()
        ),
        key=lambda row: (-int(row["positions"]), str(row["family"])),
    )[:10]
    cache_status = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM tenderplan_detail_fetches GROUP BY status"
        )
    }
    classifier_coverage = (
        len(positions_with_classifier) / len(position_ids)
        if position_ids else 0.0
    )
    exact_match_rate = (
        len(exact_matches) / len(positions_with_classifier)
        if positions_with_classifier else 0.0
    )
    classifier_coverage_pct = round(classifier_coverage * 100.0, 2)
    classifier_coverage_status = (
        "good" if classifier_coverage_pct >= 90.0
        else "partial" if classifier_coverage_pct >= 60.0
        else "low"
    )
    return {
        "tenderplan_positions_loaded": len(position_ids),
        "tenderplan_raw_classifier_codes": raw_codes,
        "tenderplan_positions_with_classifier": len(positions_with_classifier),
        "tenderplan_classifier_coverage_pct": classifier_coverage_pct,
        "tenderplan_classifier_coverage_status": classifier_coverage_status,
        "tenderplan_positions_without_classifier": len(position_ids) - len(positions_with_classifier),
        "tenderplan_distinct_okpd2_families": len(family_positions),
        "manufacturer_exact_family_matches": len(exact_matches),
        "manufacturer_exact_match_rate_among_classified_pct": round(exact_match_rate * 100.0, 4),
        "manufacturer_same_group_matches": len(broad_matches),
        "top_loaded_families": top_families,
        "detail_cache_status": cache_status,
    }


def sync_forward_sources(
    conn: sqlite3.Connection,
    client: GosplanClient,
    *,
    manufacturer_inn: str,
    region_code: int,
    history_year: int,
    purchase_pages: int = 5,
    tenderplan_pages: int = 5,
    page_size: int = 50,
    max_requests: int = 100,
    rate_per_minute: float = 7.0,
    refresh_details: bool = False,
) -> dict[str, Any]:
    """Bounded refresh of latest purchase notices and plan-schedules.

    The collector deliberately scans only a configurable head of each regional
    index. It is intended for continuous/periodic refreshes, not historical
    backfill. Purchase detail is fetched only when an aggregate row already
    matches one of the manufacturer's GISP families. Tender-plan detail is
    fetched when the aggregate projection lacks position details.
    """
    coverage = procurement_contract_coverage(conn, region_code=region_code, year=history_year)
    families = _manufacturer_families(
        conn,
        manufacturer_inn=manufacturer_inn,
        region_code=region_code,
        history_year=history_year,
        coverage=coverage,
    )
    reindexed_cached_details = _reindex_cached_tenderplan_details(conn)
    seeded_pending_details = _seed_pending_plan_details_from_loaded(conn, region_code=region_code)
    budget = max_requests if max_requests > 0 else None
    stats: dict[str, Any] = {
        "manufacturer_inn": str(manufacturer_inn),
        "region_code": region_code,
        "manufacturer_okpd2_families": sorted(families),
        "network_requests": 0,
        "purchase_list_requests": 0,
        "purchase_rows_ingested": 0,
        "purchase_detail_requests": 0,
        "purchase_detail_matches": 0,
        "tenderplan_list_requests": 0,
        "tenderplan_rows_ingested": 0,
        "tenderplan_detail_requests": 0,
        "tenderplan_detail_cached": 0,
        "tenderplan_positions_ingested": 0,
        "tenderplan_cached_details_reindexed": reindexed_cached_details,
        "tenderplan_pending_seeded_from_loaded": seeded_pending_details,
        "tenderplan_detail_cache_rows_before": int(
            conn.execute("SELECT COUNT(*) FROM tenderplan_detail_fetches WHERE status='ok'").fetchone()[0]
        ),
        "http_404": 0,
        "stop_reason": "complete",
    }

    def allowed() -> bool:
        if budget is None:
            return True
        if stats["network_requests"] < budget:
            return True
        stats["stop_reason"] = "request_budget"
        return False

    params = {"region": str(region_code)}
    for page in range(max(0, purchase_pages)):
        if not allowed():
            break
        rows = client.get_purchases(limit=page_size, skip=page * page_size, extra=params)
        stats["network_requests"] += 1
        stats["purchase_list_requests"] += 1
        _pause(rate_per_minute)
        if not rows:
            break
        for row in rows:
            parsed = extract_purchase(row)
            if parsed["region_code"] not in (None, region_code):
                raise RuntimeError(f"purchase region filter mismatch: {parsed['region_code']} != {region_code}")
            ingest_purchase(conn, row)
            stats["purchase_rows_ingested"] += 1
            aggregate_families = {f for code in parsed["okpd2"] + parsed["ktru"] if (f := okpd2_family(code))}
            if not (aggregate_families & families) or not parsed["purchase_number"]:
                continue
            stats["purchase_detail_matches"] += 1
            cached = conn.execute(
                "SELECT 1 FROM purchase_detail_fetches WHERE purchase_number=? AND status='ok'",
                (parsed["purchase_number"],),
            ).fetchone()
            if cached and not refresh_details:
                continue
            if not allowed():
                break
            try:
                detail = client.get_purchase(parsed["purchase_number"])
                stats["network_requests"] += 1
                stats["purchase_detail_requests"] += 1
                _pause(rate_per_minute)
            except httpx.HTTPStatusError as exc:
                stats["network_requests"] += 1
                stats["purchase_detail_requests"] += 1
                _pause(rate_per_minute)
                if exc.response.status_code == 404:
                    stats["http_404"] += 1
                    continue
                raise
            if isinstance(detail, dict):
                ingest_purchase(conn, detail)
                item_count = conn.execute(
                    "SELECT COUNT(*) FROM purchase_items pi JOIN purchases p ON p.id=pi.purchase_id WHERE p.purchase_number=?",
                    (parsed["purchase_number"],),
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO purchase_detail_fetches(purchase_number, fetched_at, status, item_count, protocol_count)
                    VALUES (?, datetime('now'), 'ok', ?, 0)
                    ON CONFLICT(purchase_number) DO UPDATE SET fetched_at=datetime('now'), status='ok', item_count=excluded.item_count
                    """,
                    (parsed["purchase_number"], int(item_count)),
                )
        conn.commit()
        if len(rows) < page_size or stats["stop_reason"] == "request_budget":
            break

    # Tender-plan list discovery and detail expansion are deliberately split.
    # First ingest the whole configured head; then spend the remaining budget
    # on a durable pending-detail queue.  This prevents detail-heavy page 0/1
    # from starving later list pages and preserves unfinished work across runs.
    tenderplan_list_scan_complete = True
    if stats["stop_reason"] != "request_budget":
        for page in range(max(0, tenderplan_pages)):
            if not allowed():
                tenderplan_list_scan_complete = False
                break
            rows = client.get_tenderplans(limit=page_size, skip=page * page_size, extra=params)
            stats["network_requests"] += 1
            stats["tenderplan_list_requests"] += 1
            _pause(rate_per_minute)
            if not rows:
                break
            for row in rows:
                parsed = extract_tenderplan(row)
                if parsed["region_code"] not in (None, region_code):
                    raise RuntimeError(f"tenderplan region filter mismatch: {parsed['region_code']} != {region_code}")
                try:
                    ingest_tenderplan(
                        conn,
                        row,
                        preserve_existing_positions=True,
                    )
                    stats["tenderplan_rows_ingested"] += 1
                except ValueError:
                    continue
                plan_number = parsed["plan_number"]
                aggregate_positions = parsed["positions"]
                needs_detail = not aggregate_positions or not any(
                    okpd2_family(code) in families
                    for pos in aggregate_positions
                    for code in pos["okpd2"] + pos["ktru"]
                )
                if not needs_detail or not plan_number:
                    continue
                if _plan_detail_cached(conn, plan_number) and not refresh_details:
                    stats["tenderplan_detail_cached"] += 1
                    continue
                _queue_plan_detail(conn, plan_number, refresh=refresh_details)
            conn.commit()
            if len(rows) < page_size:
                break

    pending_before = _pending_plan_details(conn, region_code=region_code)
    stats["tenderplan_list_scan_complete"] = tenderplan_list_scan_complete
    stats["tenderplan_detail_pending_before"] = len(pending_before)
    stats["tenderplan_detail_queue_mode"] = "durable_pending"

    if stats["stop_reason"] != "request_budget":
        for plan_number in pending_before:
            if not allowed():
                break
            try:
                detail = client.get_tenderplan(plan_number)
                stats["network_requests"] += 1
                stats["tenderplan_detail_requests"] += 1
                _pause(rate_per_minute)
            except httpx.HTTPStatusError as exc:
                stats["network_requests"] += 1
                stats["tenderplan_detail_requests"] += 1
                _pause(rate_per_minute)
                if exc.response.status_code == 404:
                    stats["http_404"] += 1
                    _mark_plan_detail(conn, plan_number, status="404")
                    conn.commit()
                    continue
                raise
            if isinstance(detail, dict):
                parsed_detail = extract_tenderplan(detail)
                ingest_tenderplan(
                    conn,
                    detail,
                    raw_endpoint=f"/fz44/tenderplans/{plan_number}",
                    preserve_existing_positions=False,
                )
                count = len(parsed_detail["positions"])
                stats["tenderplan_positions_ingested"] += count
                _mark_plan_detail(conn, plan_number, status="ok", position_count=count)
                conn.commit()
            else:
                _mark_plan_detail(conn, plan_number, status="invalid")
                conn.commit()

    stats["tenderplan_detail_pending_after"] = len(
        _pending_plan_details(conn, region_code=region_code)
    )

    stats["tenderplan_detail_cache_rows_after"] = int(
        conn.execute("SELECT COUNT(*) FROM tenderplan_detail_fetches WHERE status='ok'").fetchone()[0]
    )
    stats["match_diagnostics"] = _forward_match_diagnostics(
        conn,
        manufacturer_families=families,
        region_code=region_code,
    )
    return stats
