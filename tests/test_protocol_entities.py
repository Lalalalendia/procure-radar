import sqlite3

from procure_radar.db import SCHEMA
from procure_radar.extract import extract_tender_protocol
from procure_radar.ingest import ingest_tender_protocol


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _payload():
    return {
        "doc_type": "epProtocolEF2020Final",
        "published_at": "2026-08-10T11:47:57",
        "source": {
            "id": "53249926",
            "externalId": "RTS_14560720",
            "versionNumber": "1",
            "commonInfo": {
                "purchaseNumber": "0826500000926002653",
                "procedureDT": "2026-08-10T00:00:00+03:00",
            },
            "protocolInfo": {
                "applicationsInfo": {
                    "applicationInfo": {
                        "commonInfo": {
                            "appNumber": "121733389",
                            "appDT": "2026-08-05T17:10:13+03:00",
                        },
                        "finalPrice": "3038633.37",
                        "admittedInfo": {
                            "appAdmittedInfo": {"admitted": "true", "appRating": "1"}
                        },
                    }
                },
                "abandonedReason": {
                    "code": "EA20IEAOR",
                    "name": "Подана только одна заявка; заявка соответствует требованиям",
                },
            },
        },
    }


def test_extract_one_bid_abandoned_protocol():
    row = extract_tender_protocol(_payload())
    assert row["purchase_number"] == "0826500000926002653"
    assert row["applications_count"] == 1
    assert row["admitted_count"] == 1
    assert row["rejected_count"] == 0
    assert row["final_price"] == 3038633.37
    assert row["is_abandoned"] is True
    assert row["abandoned_reason_code"] == "EA20IEAOR"
    assert row["applications"][0]["app_rating"] == 1


def test_ingest_protocol_is_idempotent_and_keeps_application():
    conn = _conn()
    first = ingest_tender_protocol(conn, _payload())
    second = ingest_tender_protocol(conn, _payload())
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM tender_protocols").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tender_applications").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM tender_protocols").fetchone()
    assert row["applications_count"] == 1
    assert row["is_abandoned"] == 1


def test_final_price_prefers_winning_admitted_application():
    payload = _payload()
    payload["source"]["protocolInfo"]["applicationsInfo"]["applicationInfo"] = [
        {
            "commonInfo": {"appNumber": "winner"},
            "finalPrice": "900.00",
            "admittedInfo": {"appAdmittedInfo": {"admitted": "true", "appRating": "1"}},
        },
        {
            "commonInfo": {"appNumber": "rejected-cheaper"},
            "finalPrice": "100.00",
            "admittedInfo": {"appAdmittedInfo": {"admitted": "false", "appRating": "2"}},
        },
    ]
    row = extract_tender_protocol(payload)
    assert row["final_price"] == 900.0


def test_final_price_is_none_when_all_priced_applications_are_rejected():
    payload = _payload()
    payload["source"]["protocolInfo"]["applicationsInfo"]["applicationInfo"] = [
        {
            "commonInfo": {"appNumber": "rejected"},
            "finalPrice": "100.00",
            "admittedInfo": {"appAdmittedInfo": {"admitted": "false", "appRating": "1"}},
        }
    ]
    row = extract_tender_protocol(payload)
    assert row["final_price"] is None
