from procure_radar.extract import external_id, extract_purchase, extract_purchase_pricing_mode


def test_extract_real_gosplan_shape():
    payload = {
        "collecting_finished_at": "2026-09-04T04:00:00",
        "contract_guarantee_amount": None,
        "contract_guarantee_part": 5.0,
        "currency_code": "RUB",
        "customers": ["0542009250"],
        "doc_created_at": "2026-08-21T16:10:18.385000",
        "doc_updated_at": "2026-08-21T16:10:18.385000",
        "ikzs": ["262054200925005420100100220330000244"],
        "ktru": ["17.22.12.120-00000006"],
        "max_price": 1_500_000.0,
        "object_info": "Поставка изделий медицинского назначения (Лот 10.1)",
        "okpd2": ["13.95.10.112", "32.50.50.190"],
        "owners": ["0542009250"],
        "plan_numbers": ["202603033000627001"],
        "position_numbers": ["202603033000627001000022"],
        "published_at": "2026-08-21T14:58:28.181000",
        "purchase_number": "0103200008426007106",
        "purchase_type": "epNotificationEF2020",
        "region": 5,
        "responsible": "0572005870",
        "stage": 1,
        "updated_at": "2026-08-21T14:58:28.181000",
        "docs": [{"doc_type": "epNotificationEF2020", "published_at": "2026-08-21T14:58:28.181000"}],
    }
    row = extract_purchase(payload)
    assert row["purchase_number"] == "0103200008426007106"
    assert row["max_price"] == 1_500_000.0
    assert row["region_code"] == 5
    assert row["customers"] == ["0542009250"]
    assert row["okpd2"] == ["13.95.10.112", "32.50.50.190"]
    assert row["ktru"] == ["17.22.12.120-00000006"]
    assert row["docs"][0]["doc_type"] == "epNotificationEF2020"
    assert external_id(payload) == "0103200008426007106"


def test_extract_purchase_falls_back_to_codes_and_object_info_inside_docs():
    payload = {
        "purchase_number": "P-DOC",
        "docs": [
            {
                "doc_type": "epNotificationEF2020",
                "published_at": "2026-02-01",
                "source": {
                    "commonInfo": {"purchaseObjectInfo": "Поставка товара из документа"},
                    "notificationInfo": {
                        "customerRequirementsInfo": {
                            "customerRequirementInfo": {
                                "contractConditionsInfo": {
                                    "IKZInfo": {
                                        "OKPD2Info": {
                                            "OKPD2": {"OKPDCode": "21.20", "OKPDName": "Материалы"}
                                        }
                                    }
                                }
                            }
                        },
                        "purchaseObjectsInfo": {
                            "wrapper": {
                                "purchaseObject": {
                                    "KTRU": {
                                        "code": "21.20.24.133-00000017",
                                        "OKPD2": {"OKPDCode": "21.20.24.133"},
                                    }
                                }
                            }
                        },
                    },
                },
            }
        ],
    }
    row = extract_purchase(payload)
    assert row["object_info"] == "Поставка товара из документа"
    assert row["purchase_type"] == "epNotificationEF2020"
    assert "21.20" in row["okpd2"]
    assert "21.20.24.133" in row["okpd2"]
    assert row["ktru"] == ["21.20.24.133-00000017"]


def test_extract_purchase_pricing_mode_recognizes_maximum_contract_value():
    payload = {
        "purchase_number": "PRICE1",
        "docs": [{
            "doc_type": "epNotificationEF2020",
            "source": {
                "notificationInfo": {
                    "purchaseObjectsInfo": {
                        "notDrugPurchaseObjectsInfo": {
                            "quantityUndefined": True,
                            "purchaseObject": {"name": "Fuel"},
                        }
                    },
                    "contractConditionsInfo": {
                        "maxPriceInfo": {"isContractPriceFormula": False}
                    },
                }
            },
        }],
    }
    assert extract_purchase_pricing_mode(payload) == "maximum_contract_price"


def test_extract_purchase_pricing_mode_recognizes_formula_price():
    payload = {
        "purchase_number": "PRICE2",
        "docs": [{
            "doc_type": "epNotificationEF2020",
            "source": {
                "notificationInfo": {
                    "purchaseObjectsInfo": {
                        "notDrugPurchaseObjectsInfo": {
                            "quantityUndefined": False,
                            "purchaseObject": {"name": "Fuel"},
                        }
                    },
                    "contractConditionsInfo": {
                        "maxPriceInfo": {"isContractPriceFormula": True}
                    },
                }
            },
        }],
    }
    assert extract_purchase_pricing_mode(payload) == "maximum_contract_price"


def test_extract_purchase_pricing_mode_recognizes_fixed_contract_price():
    payload = {
        "purchase_number": "PRICE3",
        "docs": [{
            "doc_type": "epNotificationEF2020",
            "source": {
                "notificationInfo": {
                    "purchaseObjectsInfo": {
                        "notDrugPurchaseObjectsInfo": {
                            "quantityUndefined": False,
                            "purchaseObject": {"name": "Paper"},
                        }
                    },
                    "contractConditionsInfo": {
                        "maxPriceInfo": {"isContractPriceFormula": False}
                    },
                }
            },
        }],
    }
    assert extract_purchase_pricing_mode(payload) == "fixed_contract_price"


def test_extract_tenderplan2020_positions_and_codes():
    from procure_radar.extract import extract_tenderplan

    payload = {
        "plan_number": "202602000000001001",
        "region": 2,
        "published_at": "2026-08-20T10:00:00",
        "docs": [
            {
                "doc_type": "tenderPlan2020",
                "published_at": "2026-08-20T10:00:00",
                "source": {
                    "commonInfo": {
                        "planNumber": "202602000000001001",
                        "year": 2026,
                        "customerInfo": {"INN": "0278176470"},
                    },
                    "positions": {
                        "position": {
                            "commonInfo": {
                                "positionNumber": "202602000000001001000001",
                                "IKZ": "262027817647002780100100010012530244",
                                "purchaseObjectName": "Поставка блочно-модульной котельной",
                                "plannedPublishDate": "2026-10-15",
                            },
                            "purchaseObjectsInfo": {
                                "purchaseObject": {
                                    "OKPD2": {"OKPDCode": "25.30.12.110", "OKPDName": "Котлы"}
                                }
                            },
                            "financeInfo": {"totalAmount": 32_000_000},
                        }
                    },
                },
            }
        ],
    }
    row = extract_tenderplan(payload)
    assert row["plan_number"] == "202602000000001001"
    assert row["region_code"] == 2
    assert row["year"] == 2026
    assert "0278176470" in row["customer_inns"]
    assert len(row["positions"]) == 1
    position = row["positions"][0]
    assert position["position_number"] == "202602000000001001000001"
    assert position["planned_at"] == "2026-10-15"
    assert position["amount"] == 32_000_000
    assert position["okpd2"] == ["25.30.12.110"]


def test_extract_tenderplan_codes_from_nested_classifier_arrays():
    from procure_radar.extract import extract_tenderplan

    payload = {
        "plan_number": "PLAN-NESTED",
        "region": 2,
        "positions": [
            {
                "positionNumber": "POS-NESTED",
                "purchaseObjectName": "Котельная",
                "purchaseObjectsInfo": {
                    "purchaseObject": [
                        {
                            "classifier": {
                                "OKPD2": [
                                    {"OKPDCode": "25.30.12.110", "OKPDName": "Котлы"},
                                    {"code": "25.30.12.120"},
                                ]
                            }
                        }
                    ]
                },
            }
        ],
    }

    row = extract_tenderplan(payload)
    assert row["positions"][0]["okpd2"] == ["25.30.12.110", "25.30.12.120"]
