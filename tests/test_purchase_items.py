import sqlite3

from procure_radar.db import SCHEMA
from procure_radar.extract import extract_embedded_protocols, extract_purchase_items
from procure_radar.ingest import ingest_purchase


def full_purchase():
    return {
        "purchase_number": "P1",
        "region": 2,
        "stage": 2,
        "max_price": 1000,
        "docs": [
            {
                "doc_type": "epNotificationEF2020",
                "published_at": "2026-01-01",
                "source": {
                    "notificationInfo": {
                        "purchaseObjectsInfo": {
                            "notDrugPurchaseObjectsInfo": {
                                "purchaseObject": {
                                    "sid": "11",
                                    "externalSid": "item-11",
                                    "name": "Бумага",
                                    "KTRU": {
                                        "code": "17.12.14.100-00000001",
                                        "name": "Бумага офсетная",
                                        "OKPD2": {"OKPDCode": "17.12.14.100", "OKPDName": "Бумага"},
                                    },
                                    "OKEI": {"code": "796", "name": "Штука"},
                                    "quantity": {"value": "10"},
                                    "price": "100",
                                    "sum": "1000",
                                }
                            }
                        }
                    }
                },
            },
            {
                "doc_type": "epProtocolEF2020Final",
                "source": {"id": "proto-1", "commonInfo": {"purchaseNumber": "P1"}},
            },
        ],
    }


def test_extract_items_and_embedded_protocols():
    payload = full_purchase()
    items = extract_purchase_items(payload)
    assert len(items) == 1
    assert items[0]["ktru_code"] == "17.12.14.100-00000001"
    assert items[0]["quantity"] == 10.0
    assert items[0]["amount"] == 1000.0
    assert len(extract_embedded_protocols(payload)) == 1


def test_ingest_full_purchase_persists_line_items():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, full_purchase())
    row = conn.execute("SELECT ktru_code, quantity, amount FROM purchase_items").fetchone()
    assert dict(row) == {
        "ktru_code": "17.12.14.100-00000001",
        "quantity": 10.0,
        "amount": 1000.0,
    }


def test_extract_items_from_alternate_purchase_object_wrapper():
    payload = {
        "purchase_number": "P2",
        "docs": [
            {
                "doc_type": "epNotificationEF2020",
                "source": {
                    "id": "doc-2",
                    "commonInfo": {"purchaseObjectInfo": "Поставка тестового товара"},
                    "notificationInfo": {
                        "purchaseObjectsInfo": {
                            "someOtherWrapper": {
                                "nested": {
                                    "purchaseObject": {
                                        "sid": "22",
                                        "purchaseObjectName": "Товар 2",
                                        "OKPD2": {"OKPDCode": "01.02.03.004", "OKPDName": "Товар"},
                                        "quantity": "2",
                                        "unitPrice": "150",
                                        "amount": "300",
                                    }
                                }
                            }
                        }
                    },
                },
            }
        ],
    }
    items = extract_purchase_items(payload)
    assert len(items) == 1
    assert items[0]["okpd2_code"] == "01.02.03.004"
    assert items[0]["quantity"] == 2.0
    assert items[0]["unit_price"] == 150.0
    assert items[0]["amount"] == 300.0


def test_extract_items_uses_latest_notification_revision_only():
    payload = {
        "purchase_number": "P-REV",
        "docs": [
            {
                "doc_type": "epNotificationEF2020",
                "published_at": "2026-01-01T10:00:00",
                "source": {
                    "id": "old",
                    "versionNumber": "1",
                    "notificationInfo": {
                        "purchaseObjectsInfo": {
                            "purchaseObject": [
                                {"externalSid": "same", "name": "Старое имя", "sum": "100"},
                                {"externalSid": "removed", "name": "Удаленная позиция", "sum": "50"},
                            ]
                        }
                    },
                },
            },
            {
                "doc_type": "epNotificationEF2020",
                "published_at": "2026-01-02T10:00:00",
                "source": {
                    "id": "new",
                    "versionNumber": "2",
                    "notificationInfo": {
                        "purchaseObjectsInfo": {
                            "purchaseObject": {"externalSid": "same", "name": "Новое имя", "sum": "125"}
                        }
                    },
                },
            },
        ],
    }

    items = extract_purchase_items(payload)
    assert [(item["item_key"], item["name"], item["amount"]) for item in items] == [
        ("same", "Новое имя", 125.0)
    ]


def test_duplicate_sid_inside_latest_revision_is_preserved_without_unique_collision():
    payload = {
        "purchase_number": "P-DUP",
        "region": 2,
        "docs": [
            {
                "doc_type": "epNotificationEF2020",
                "published_at": "2026-01-02T10:00:00",
                "source": {
                    "id": "doc-new",
                    "notificationInfo": {
                        "purchaseObjectsInfo": {
                            "purchaseObject": [
                                {"externalSid": "dup", "name": "Позиция A", "sum": "100"},
                                {"externalSid": "dup", "name": "Позиция B", "sum": "200"},
                            ]
                        }
                    },
                },
            }
        ],
    }

    items = extract_purchase_items(payload)
    assert [item["item_key"] for item in items] == ["dup", "dup#2"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    ingest_purchase(conn, payload)
    rows = conn.execute(
        "SELECT item_key, name, amount FROM purchase_items ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("dup", "Позиция A", 100.0),
        ("dup#2", "Позиция B", 200.0),
    ]
