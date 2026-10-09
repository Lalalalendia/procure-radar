from __future__ import annotations

from pathlib import Path

from procure_radar.db import connect
from procure_radar.ingest import ingest_purchase
from procure_radar.locality import (
    extract_party_addresses,
    extract_purchase_locality_evidence,
    locality_matches,
    locality_purchases,
    read_inn_file,
    refresh_locality_memberships,
)


def test_locality_match_normalizes_punctuation_and_case() -> None:
    assert locality_matches(
        "453100, Республика Башкортостан, г. СТЕРЛИТАМАК, ул. Мира, 1",
        ["Стерлитамак"],
    )
    assert not locality_matches("Республика Башкортостан, г. Салават", ["Стерлитамак"])


def test_extract_party_addresses_requires_same_dict_as_inn() -> None:
    payload = {
        "outer": {
            "customer": {
                "INN": "0268000001",
                "fullName": "ГБУ Заказчик",
                "postalAddress": "453100, г. Стерлитамак, ул. Мира, 1",
            },
            "unrelatedAddress": "г. Уфа",
        }
    }
    assert extract_party_addresses(payload, wanted_inns={"0268000001"}) == {
        "0268000001": "453100, г. Стерлитамак, ул. Мира, 1"
    }


def test_purchase_ingest_enriches_customer_address_and_city_allowlist(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        payload = {
            "purchase_number": "P-ST-1",
            "region": 2,
            "published_at": "2026-10-09T06:00:00",
            "max_price": 750000,
            "object_info": "Поставка мебели",
            "customers": ["0268000001"],
            "owners": [],
            "okpd2": ["31.01.12.190"],
            "docs": [
                {
                    "doc_type": "epNotificationEF2020",
                    "published_at": "2026-10-09T06:00:00",
                    "source": {
                        "notificationInfo": {
                            "customer": {
                                "INN": "0268000001",
                                "fullName": "ГБУ Стерлитамакский заказчик",
                                "registrationAddress": (
                                    "453100, Республика Башкортостан, г. Стерлитамак, ул. Мира, 1"
                                ),
                            }
                        }
                    },
                }
            ],
        }
        ingest_purchase(conn, payload)
        conn.commit()
        org = conn.execute(
            "SELECT name, address, region_code FROM organizations WHERE inn='0268000001'"
        ).fetchone()
        assert org is not None
        assert org["name"] == "ГБУ Стерлитамакский заказчик"
        assert "Стерлитамак" in org["address"]
        assert org["region_code"] == 2

        stats = refresh_locality_memberships(
            conn,
            locality_key="sterlitamak",
            locality_name="Стерлитамак",
            aliases=["Стерлитамак"],
            region_code=2,
        )
        rows = locality_purchases(conn, locality_key="sterlitamak")
    finally:
        conn.close()

    assert stats["address_matches"] == 1
    assert stats["strong_members"] == 1
    assert len(rows) == 1
    assert rows[0]["purchase_number"] == "P-ST-1"
    assert rows[0]["customer_inn"] == "0268000001"


def test_name_only_is_weak_but_manual_allowlist_is_strong(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        conn.execute(
            "INSERT INTO organizations(inn, name, region_code) VALUES ('0268000002','ООО Стерлитамак Сервис',2)"
        )
        conn.commit()
        weak = refresh_locality_memberships(
            conn,
            locality_key="sterlitamak",
            locality_name="Стерлитамак",
            aliases=["Стерлитамак"],
            region_code=2,
        )
        strong = refresh_locality_memberships(
            conn,
            locality_key="sterlitamak",
            locality_name="Стерлитамак",
            aliases=["Стерлитамак"],
            region_code=2,
            manual_inns=["0268000002"],
        )
    finally:
        conn.close()
    assert weak["name_matches"] == 1
    assert weak["strong_members"] == 0
    assert strong["strong_members"] == 1


def test_read_inn_file_ignores_comments_and_bad_rows(tmp_path: Path) -> None:
    path = tmp_path / "inns.txt"
    path.write_text("# comment\n0268000001\ninvalid\n0268000002 # ok\n", encoding="utf-8")
    assert read_inn_file(path) == ["0268000001", "0268000002"]


class _FakeIdentityClient:
    base_url = "https://example.invalid"

    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads
        self.calls: list[str] = []

    def get_purchase(self, purchase_number: str):
        self.calls.append(purchase_number)
        return self.payloads[purchase_number]


def test_identity_enrichment_fetches_once_and_populates_city_address(tmp_path: Path) -> None:
    from procure_radar.locality import enrich_purchase_identities

    conn = connect(tmp_path / "db.sqlite3")
    try:
        ingest_purchase(
            conn,
            {
                "purchase_number": "P-ID-1",
                "region": 2,
                "published_at": "2026-10-09T07:00:00",
                "customers": ["0268000003"],
                "owners": [],
                "docs": [],
            },
        )
        conn.commit()
        detail = {
            "purchase_number": "P-ID-1",
            "region": 2,
            "published_at": "2026-10-09T07:00:00",
            "customers": ["0268000003"],
            "owners": [],
            "docs": [
                {
                    "doc_type": "epNotificationEF2020",
                    "source": {
                        "notificationInfo": {
                            "customer": {
                                "INN": "0268000003",
                                "fullName": "МБУ Заказчик",
                                "postalAddress": "453120, г. Стерлитамак, ул. Ленина, 10",
                            }
                        }
                    },
                }
            ],
        }
        client = _FakeIdentityClient({"P-ID-1": detail})
        first = enrich_purchase_identities(
            conn, client, region_code=2, max_requests=5, request_delay=0
        )
        second = enrich_purchase_identities(
            conn, client, region_code=2, max_requests=5, request_delay=0
        )
        address = conn.execute(
            "SELECT address FROM organizations WHERE inn='0268000003'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert first["requests"] == 1
    assert first["ok"] == 1
    assert second["requests"] == 0
    assert client.calls == ["P-ID-1"]
    assert "Стерлитамак" in address


def test_structured_purchase_locality_evidence_ignores_attachment_only_mentions() -> None:
    payload = {
        "docs": [{
            "source": {
                "attachmentsInfo": {"attachmentInfo": [{"fileName": "Стерлитамак.docx.zip"}]},
                "notificationInfo": {
                    "customerRequirementsInfo": {
                        "customerRequirementInfo": {
                            "contractConditionsInfo": {
                                "deliveryPlacesInfo": {
                                    "byGARInfo": {
                                        "GARInfo": {
                                            "GARAddress": "респ. Башкортостан, г. Стерлитамак, ул. Карла Маркса, д. 103"
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
            }
        }]
    }
    evidence = extract_purchase_locality_evidence(payload, aliases=["Стерлитамак"])
    assert evidence
    assert evidence[0]["source"] == "structured_address"
    assert evidence[0]["confidence"] == 1.0

    attachment_only = {
        "docs": [{"source": {"attachmentsInfo": {"fileName": "Стерлитамак.docx.zip"}}}]
    }
    assert extract_purchase_locality_evidence(attachment_only, aliases=["Стерлитамак"]) == []


def test_purchase_level_locality_recovers_delivery_city_without_customer_address(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    try:
        ingest_purchase(conn, {
            "purchase_number": "P-LIVE-1",
            "region": 2,
            "published_at": "2026-10-09T08:00:00",
            "collecting_finished_at": "2026-10-20T05:00:00",
            "customers": ["0278206808"],
            "owners": [],
            "object_info": "Оказание услуг по техническому обслуживанию",
            "docs": [{
                "doc_type": "epNotificationEZK2020",
                "source": {
                    "notificationInfo": {
                        "customerRequirementsInfo": {
                            "customerRequirementInfo": {
                                "customer": {"fullName": "ГКУ РЦСПН"},
                                "contractConditionsInfo": {
                                    "deliveryPlacesInfo": {
                                        "byGARInfo": {
                                            "GARInfo": {
                                                "GARAddress": "респ. Башкортостан, г.о. город Стерлитамак, г. Стерлитамак, ул. Карла Маркса, д. 103"
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    }
                },
            }],
        })
        conn.commit()
        assert conn.execute(
            "SELECT address FROM organizations WHERE inn='0278206808'"
        ).fetchone()[0] is None
        stats = refresh_locality_memberships(
            conn,
            locality_key="sterlitamak",
            locality_name="Стерлитамак",
            aliases=["Стерлитамак"],
            region_code=2,
        )
        rows = locality_purchases(conn, locality_key="sterlitamak")
    finally:
        conn.close()

    assert stats["strong_purchase_matches"] == 1
    assert len(rows) == 1
    assert rows[0]["purchase_number"] == "P-LIVE-1"
    assert rows[0]["customer_inn"] == "0278206808"
    assert rows[0]["locality_source"] == "purchase"
    assert rows[0]["locality_confidence"] == 1.0
