from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from procure_radar.bulk import ingest_region_batch
from procure_radar.db import connect


def _protocol(purchase_number: str, doc_type: str, source_id: str) -> dict[str, Any]:
    return {
        "doc_type": doc_type,
        "published_at": "2026-08-20T12:00:00",
        "source": {
            "id": source_id,
            "versionNumber": "1",
            "commonInfo": {"purchaseNumber": purchase_number},
            "protocolInfo": {"applicationsInfo": {"applicationInfo": []}},
        },
    }


def _row(
    purchase_number: str,
    purchase_type: str,
    docs: list[dict[str, Any]],
    *,
    stage: int = 2,
) -> dict[str, Any]:
    return {
        "purchase_number": purchase_number,
        "purchase_type": purchase_type,
        "region": 2,
        "stage": stage,
        "published_at": "2026-08-20T10:00:00",
        "max_price": 100000.0,
        "docs": docs,
    }


@dataclass
class FakeClient:
    rows: list[dict[str, Any]]
    details: dict[str, dict[str, Any]]
    protocol_payloads: dict[str, Any] = field(default_factory=dict)
    base_url: str = "https://example.test"
    detail_calls: list[str] = field(default_factory=list)
    protocol_calls: list[str] = field(default_factory=list)

    def get_purchases(self, *, limit: int, skip: int, extra: dict[str, str] | None = None):
        return self.rows[skip : skip + limit]

    def get_purchase(self, purchase_number: str):
        self.detail_calls.append(purchase_number)
        return self.details[purchase_number]

    def get_purchase_protocols(self, purchase_number: str):
        self.protocol_calls.append(purchase_number)
        return self.protocol_payloads.get(purchase_number, [])


def test_bulk_fetches_only_competitive_rows_with_final_protocol(tmp_path: Path):
    ef_number = "0000000000000000001"
    ezt_number = "0000000000000000002"
    active_number = "0000000000000000003"
    cancelled_number = "0000000000000000004"
    quote_number = "0000000000000000005"

    ef_final = _protocol(ef_number, "epProtocolEF2020Final", "ef-final")
    quote_final = _protocol(quote_number, "epProtocolEZK2020FinalPart", "quote-final")
    rows = [
        _row(
            ef_number,
            "epNotificationEF2020",
            [{"doc_type": "epNotificationEF2020"}, {"doc_type": "epProtocolEF2020Final"}],
            stage=1,  # proves stage is not used as the detail trigger
        ),
        _row(
            ezt_number,
            "epNotificationEZT2020",
            [{"doc_type": "epNotificationEZT2020"}, {"doc_type": "epProtocolEZT2020Final"}],
        ),
        _row(
            active_number,
            "epNotificationEF2020",
            [{"doc_type": "epNotificationEF2020"}, {"doc_type": "epProtocolEF2020SubmitOffers"}],
        ),
        _row(
            cancelled_number,
            "epNotificationEF2020",
            [
                {"doc_type": "epNotificationEF2020"},
                {"doc_type": "epProtocolEF2020Final"},
                {"doc_type": "epNotificationCancel"},
            ],
        ),
        _row(
            quote_number,
            "epNotificationEZK2020",
            [{"doc_type": "epNotificationEZK2020"}, {"doc_type": "epProtocolEZK2020FinalPart"}],
        ),
    ]
    details = {
        ef_number: _row(ef_number, "epNotificationEF2020", [ef_final], stage=1),
        quote_number: _row(quote_number, "epNotificationEZK2020", [quote_final], stage=2),
    }
    client = FakeClient(rows=rows, details=details)
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        stats = ingest_region_batch(
            conn,
            client,
            region_code=2,
            limit=10,
            pages=1,
            request_delay=0,
            emit=lambda _: None,
        )
    finally:
        conn.close()

    assert client.detail_calls == [ef_number, quote_number]
    assert client.protocol_calls == []
    assert stats["purchases"] == 5
    assert stats["detail_candidates"] == 2
    assert stats["details"] == 2
    assert stats["detail_skipped_method"] == 1
    assert stats["detail_skipped_no_final"] == 1
    assert stats["detail_skipped_cancelled"] == 1


def test_bulk_falls_back_to_protocol_endpoint_when_detail_omits_final(tmp_path: Path):
    number = "0000000000000000010"
    final = _protocol(number, "epProtocolEF2020FinalPart", "fallback-final")
    rows = [
        _row(
            number,
            "epNotificationEF2020",
            [{"doc_type": "epNotificationEF2020"}, {"doc_type": "epProtocolEF2020FinalPart"}],
        )
    ]
    details = {
        number: _row(number, "epNotificationEF2020", [{"doc_type": "epNotificationEF2020"}])
    }
    client = FakeClient(rows=rows, details=details, protocol_payloads={number: [final]})
    conn = connect(tmp_path / "radar.sqlite3")
    try:
        stats = ingest_region_batch(
            conn,
            client,
            region_code=2,
            limit=10,
            pages=1,
            request_delay=0,
            emit=lambda _: None,
        )
        stored = conn.execute(
            "SELECT doc_type FROM tender_protocols WHERE purchase_number=?",
            (number,),
        ).fetchone()
    finally:
        conn.close()

    assert client.detail_calls == [number]
    assert client.protocol_calls == [number]
    assert stats["protocol_fallback_requests"] == 1
    assert stats["details"] == 1
    assert stored is not None
    assert stored["doc_type"] == "epProtocolEF2020FinalPart"
