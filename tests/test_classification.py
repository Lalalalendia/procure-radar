from procure_radar.classification import (
    classify_purchase_type,
    competition_detail_exclusion_reason,
    competitive_final_protocol_types,
    competition_exclusion_reason,
    lifecycle_from_docs,
    select_competition_protocol,
)


def test_ezt_is_not_competition_eligible():
    method = classify_purchase_type("epNotificationEZT2020")
    assert method.code == "single_supplier_93_12"
    assert method.competition_eligible is False
    assert competition_exclusion_reason("epNotificationEZT2020") == "special_single_supplier_93_12"


def test_auction_final_protocol_is_selected_but_ezt_is_not():
    protocols = [
        {"id": 1, "doc_type": "epProtocolEF2020SubmitOffers", "published_at": "2026-01-01"},
        {"id": 2, "doc_type": "epProtocolEF2020Final", "published_at": "2026-01-02"},
    ]
    selected = select_competition_protocol("epNotificationEF2020", protocols)
    assert selected is not None
    assert selected["id"] == 2
    assert select_competition_protocol("epNotificationEZT2020", protocols) is None


def test_cancel_document_wins_over_numeric_stage():
    assert lifecycle_from_docs(2, ["epNotificationEF2020", "epNotificationCancel"]) == "cancelled"
    assert competition_exclusion_reason(
        "epNotificationEF2020", doc_types=["epNotificationCancel"]
    ) == "cancelled"


def test_detail_fetch_is_document_driven_not_stage_driven():
    docs = [
        {"doc_type": "epNotificationEF2020"},
        {"doc_type": "epProtocolEF2020FinalPart"},
    ]
    assert competition_detail_exclusion_reason("epNotificationEF2020", docs=docs) is None
    assert competitive_final_protocol_types("epNotificationEF2020", docs) == (
        "epProtocolEF2020FinalPart",
    )


def test_detail_fetch_excludes_single_supplier_cancelled_and_nonfinal():
    assert competition_detail_exclusion_reason(
        "epNotificationEZT2020",
        docs=[{"doc_type": "epProtocolEZT2020Final"}],
    ) == "special_single_supplier_93_12"
    assert competition_detail_exclusion_reason(
        "epNotificationEF2020",
        docs=[{"doc_type": "epProtocolEF2020Final"}, {"doc_type": "epNotificationCancel"}],
    ) == "cancelled"
    assert competition_detail_exclusion_reason(
        "epNotificationEF2020",
        docs=[{"doc_type": "epProtocolEF2020SubmitOffers"}],
    ) == "no_competitive_final_protocol"
