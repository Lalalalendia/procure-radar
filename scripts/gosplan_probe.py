from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

BASE_URL = os.getenv("GOSPLAN_BASE_URL", "https://v2test.gosplan.info").rstrip("/")
API_KEY = os.getenv("GOSPLAN_API_KEY", "").strip()
OUT = Path("data")
OUT.mkdir(parents=True, exist_ok=True)

headers = {"Accept": "application/json", "User-Agent": "procure-radar-github-probe/1.0"}
if API_KEY:
    headers["apikey"] = API_KEY

params = {"limit": 10, "skip": 0, "region": 2}
url = f"{BASE_URL}/fz44/purchases"

with httpx.Client(timeout=45.0, follow_redirects=True, headers=headers) as client:
    response = client.get(url, params=params)
    meta = {
        "request_url": str(response.request.url),
        "status_code": response.status_code,
        "x_total": response.headers.get("x-total"),
        "content_type": response.headers.get("content-type"),
    }
    response.raise_for_status()
    payload = response.json()

if not isinstance(payload, list):
    raise TypeError(f"expected list, got {type(payload).__name__}")

rows = [row for row in payload if isinstance(row, dict)]
regions = sorted({row.get("region") for row in rows})
numbers = [str(row.get("purchase_number") or "") for row in rows]

if any(region != 2 for region in regions):
    raise RuntimeError(f"region filter was not honored: {regions}")

summary = {
    **meta,
    "rows": len(rows),
    "regions": regions,
    "purchase_numbers": numbers,
    "sample_keys": sorted(rows[0].keys()) if rows else [],
}

(OUT / "probe-summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
(OUT / "probe-purchases.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(json.dumps(summary, ensure_ascii=False))
