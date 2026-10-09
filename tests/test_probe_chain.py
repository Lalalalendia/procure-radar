from __future__ import annotations

import argparse
import json

import httpx

from procure_radar import cli


def test_probe_chain_saves_successful_payloads_and_tolerates_optional_result_404(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_purchase(self, purchase_number):
            return {"purchase_number": purchase_number}

        def get_purchase_protocols(self, purchase_number):
            return [{"doc_type": "epProtocolEF2020FinalPart"}]

        def get_purchase_result(self, purchase_number):
            request = httpx.Request("GET", f"https://example.test/fz44/purchases/{purchase_number}/result")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(cli, "GosplanClient", FakeClient)
    args = argparse.Namespace(
        purchase_number="123",
        output_dir=str(tmp_path),
        base_url="https://example.test",
        api_key=None,
    )

    cli.cmd_probe_purchase_chain(args)

    assert json.loads((tmp_path / "purchase.json").read_text(encoding="utf-8")) == {"purchase_number": "123"}
    assert json.loads((tmp_path / "protocols.json").read_text(encoding="utf-8")) == [
        {"doc_type": "epProtocolEF2020FinalPart"}
    ]
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) is None
