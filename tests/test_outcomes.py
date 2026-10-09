from procure_radar.outcomes import classify_protocol_outcome, protocol_weakness


def test_single_submitted_from_counts():
    row = {
        "applications_count": 1,
        "admitted_count": 1,
        "rejected_count": 0,
        "is_abandoned": 1,
        "abandoned_reason_name": "Подана только одна заявка; заявка соответствует требованиям",
    }
    assert classify_protocol_outcome(row).code == "single_submitted"
    assert protocol_weakness(row) == 1.0


def test_single_admitted_from_many():
    row = {
        "applications_count": 4,
        "admitted_count": 1,
        "rejected_count": 3,
        "is_abandoned": 1,
        "abandoned_reason_name": "По результатам рассмотрения только одна заявка соответствует требованиям",
    }
    assert classify_protocol_outcome(row).code == "single_admitted"


def test_no_bids_and_all_rejected_are_distinct():
    assert classify_protocol_outcome(
        {
            "applications_count": 0,
            "admitted_count": 0,
            "rejected_count": 0,
            "is_abandoned": 1,
            "abandoned_reason_name": "Не подано ни одной заявки",
        }
    ).code == "no_bids"
    assert classify_protocol_outcome(
        {
            "applications_count": 3,
            "admitted_count": 0,
            "rejected_count": 3,
            "is_abandoned": 1,
            "abandoned_reason_name": "Все заявки отклонены",
        }
    ).code == "all_rejected"


def test_two_valid_bids_without_abandonment_is_competitive_but_not_zero_weakness():
    row = {
        "applications_count": 2,
        "admitted_count": 2,
        "rejected_count": 0,
        "is_abandoned": 0,
    }
    assert classify_protocol_outcome(row).code == "competitive"
    assert protocol_weakness(row) == 0.6


def test_single_submitted_but_rejected_is_separate_outcome():
    row = {
        "applications_count": 1,
        "admitted_count": 0,
        "rejected_count": 1,
        "is_abandoned": 1,
        "abandoned_reason_name": "Подана только одна заявка. Заявка не соответствует требованиям.",
    }
    assert classify_protocol_outcome(row).code == "single_submitted_rejected"

