from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = os.getenv("GOSPLAN_BASE_URL", "https://v2test.gosplan.info").rstrip("/")
OUT = Path("data")
OUT.mkdir(parents=True, exist_ok=True)

CITY_ALIASES = ("стерлитамак", "sterlitamak")
ADDRESS_KEYS = {
    "address", "postaladdress", "registrationaddress", "legaladdress",
    "factaddress", "actualaddress", "locationaddress",
    "organizationaddress", "customeraddress", "placeofstay",
}

def key_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

def normalize_text(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return " ".join(text.split())

def city_match(value: Any) -> bool:
    text = normalize_text(value)
    return any(alias in text for alias in CITY_ALIASES)

def clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip()
    return value or None

def normalize_inn(value: Any) -> str | None:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) in (10, 12):
        return digits
    return None

def extract_party_addresses(payload: Any, wanted_inns: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}

    def direct_inns(node: dict[str, Any]) -> set[str]:
        found: set[str] = set()
        for key, value in node.items():
            token = key_token(key)
            if token == "inn" or token.endswith("inn"):
                inn = normalize_inn(value)
                if inn:
                    found.add(inn)
        return found

    def direct_address(node: dict[str, Any]) -> str | None:
        candidates: list[str] = []
        for key, value in node.items():
            token = key_token(key)
            if token in ADDRESS_KEYS or token.endswith("address"):
                text = clean_text(value)
                if text:
                    candidates.append(text)
        return max(candidates, key=len) if candidates else None

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            inns = direct_inns(node)
            address = direct_address(node)
            if address:
                for inn in inns:
                    if inn in wanted_inns:
                        old = result.get(inn)
                        if old is None or len(address) > len(old):
                            result[inn] = address
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return result

def raw_city_mentions(payload: Any) -> list[str]:
    hits: list[str] = []
    def walk(node: Any, path: str = "$") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                walk(value, f"{path}[{idx}]")
        elif isinstance(node, str) and city_match(node):
            hits.append(f"{path}: {node[:500]}")
    walk(payload)
    return hits[:30]

headers = {"Accept": "application/json", "User-Agent": "procure-radar-sterlitamak-live/1.0"}
timeout = httpx.Timeout(45.0)

with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
    response = client.get(f"{BASE_URL}/fz44/purchases", params={"limit": 50, "skip": 0, "region": 2})
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise TypeError(f"expected list, got {type(rows).__name__}")

    matched: list[dict[str, Any]] = []
    raw_hits: list[dict[str, Any]] = []
    detail_failures: list[dict[str, Any]] = []
    checked = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        purchase_number = str(row.get("purchase_number") or "").strip()
        if not purchase_number:
            continue
        customer_inns = {
            inn for value in (row.get("customers") or [])
            if (inn := normalize_inn(value))
        }

        # Free endpoint is 10 requests/minute. Keep a conservative interval.
        time.sleep(6.3)
        try:
            detail_response = client.get(f"{BASE_URL}/fz44/purchases/{purchase_number}")
            detail_response.raise_for_status()
            detail = detail_response.json()
        except Exception as exc:
            detail_failures.append({"purchase_number": purchase_number, "error": repr(exc)})
            continue

        checked += 1
        if not isinstance(detail, dict):
            detail_failures.append({"purchase_number": purchase_number, "error": "detail_not_object"})
            continue

        addresses = extract_party_addresses(detail, customer_inns)
        city_addresses = {inn: addr for inn, addr in addresses.items() if city_match(addr)}
        mentions = raw_city_mentions(detail)

        record = {
            "purchase_number": purchase_number,
            "published_at": row.get("published_at"),
            "collecting_finished_at": row.get("collecting_finished_at"),
            "max_price": row.get("max_price"),
            "currency_code": row.get("currency_code"),
            "object_info": row.get("object_info"),
            "purchase_type": row.get("purchase_type"),
            "region": row.get("region"),
            "customers": sorted(customer_inns),
            "matched_addresses": city_addresses,
            "raw_city_mentions": mentions,
            "okpd2": row.get("okpd2") or [],
            "ktru": row.get("ktru") or [],
        }
        if city_addresses:
            matched.append(record)
        elif mentions:
            raw_hits.append(record)

    summary = {
        "base_url": BASE_URL,
        "summary_rows": len(rows),
        "details_checked": checked,
        "strong_sterlitamak_matches": len(matched),
        "raw_city_mentions_without_colocated_inn_address": len(raw_hits),
        "detail_failures": len(detail_failures),
        "matched_purchase_numbers": [x["purchase_number"] for x in matched],
    }

(OUT / "sterlitamak-live-summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
)
(OUT / "sterlitamak-live-matches.json").write_text(
    json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8"
)
(OUT / "sterlitamak-live-raw-hits.json").write_text(
    json.dumps(raw_hits, ensure_ascii=False, indent=2), encoding="utf-8"
)
(OUT / "sterlitamak-live-failures.json").write_text(
    json.dumps(detail_failures, ensure_ascii=False, indent=2), encoding="utf-8"
)

print(json.dumps(summary, ensure_ascii=False))
