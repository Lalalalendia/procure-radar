from __future__ import annotations

import json

import httpx

from procure_radar.client import GosplanClient


def test_get_rows_uses_skip_not_offset(monkeypatch):
    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, endpoint, params=None):
            seen["endpoint"] = endpoint
            seen["params"] = params
            request = httpx.Request("GET", f"https://example.test{endpoint}")
            return httpx.Response(200, request=request, content=b"[]")

    monkeypatch.setattr(httpx, "Client", FakeClient)
    client = GosplanClient(base_url="https://example.test")
    assert client.get_rows("/fz44/purchases", limit=50, skip=100) == []
    assert seen["params"] == {"limit": 50, "skip": 100}


def test_protocol_endpoint_keeps_arbitrary_json_shape(monkeypatch):
    payload = {"docs": [{"doc_type": "epProtocolEF2020FinalPart"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, endpoint, params=None):
            request = httpx.Request("GET", f"https://example.test{endpoint}")
            return httpx.Response(
                200,
                request=request,
                content=json.dumps(payload).encode(),
                headers={"content-type": "application/json"},
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    client = GosplanClient(base_url="https://example.test")
    assert client.get_purchase_protocols("123") == payload
