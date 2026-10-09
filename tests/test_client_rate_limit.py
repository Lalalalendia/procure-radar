from __future__ import annotations

import httpx

from procure_radar.client import GosplanClient


def test_get_json_retries_429_then_succeeds(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)
    monkeypatch.setattr("procure_radar.client.time.sleep", sleeps.append)

    client = GosplanClient(max_retries=2)
    assert client.get_json("/x") == {"ok": True}
    assert calls == 2
    assert sleeps == [0.25]


def test_retry_delay_falls_back_to_backoff():
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(429, request=request)
    client = GosplanClient(retry_backoff=10.0)
    assert client._retry_delay(response, 0) == 10.0
    assert client._retry_delay(response, 1) == 20.0
    assert client._retry_delay(response, 2) == 30.0
    assert client._retry_delay(response, 3) == 30.0
