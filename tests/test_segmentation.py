from procure_radar.segmentation import classify_market_segment


def test_segments_pharma_diagnostics_and_it_goods():
    assert classify_market_segment(code_system="ktru", code="21.20.10.211-00024").code == "pharma"
    assert classify_market_segment(code_system="ktru", code="21.20.23.110-00007132").code == "medical_diagnostics"
    assert classify_market_segment(code_system="ktru", code="26.20.11.110-00000165", label="Ноутбук").code == "it_goods"


def test_segments_real_estate_works_and_services():
    assert classify_market_segment(code_system="okpd2", code="41.20.10.110").code == "real_estate"
    assert classify_market_segment(code_system="okpd2", code="43.39.19.190").code == "construction_works"
    assert classify_market_segment(code_system="okpd2", code="62.02.30.000").code == "it_services"
    assert classify_market_segment(code_system="ktru", code="81.21.10.000-00000007").code == "facility_services"


def test_standard_good_with_repair_word_is_not_service():
    item = classify_market_segment(
        code_system="okpd2",
        code="22.29.21.000",
        label="Поставка ремонтно-строительных материалов",
    )
    assert item.code == "standard_goods"


def test_service_code_wins_over_medical_product_keyword():
    item = classify_market_segment(
        code_system="okpd2",
        code="33.13.12.000",
        label="Услуги по ремонту медицинского диагностического оборудования",
    )
    assert item.code == "repair_maintenance"
    assert item.group == "services"


def test_software_license_service_wins_over_computer_keyword():
    item = classify_market_segment(
        code_system="okpd2",
        code="58.29.50.000",
        label="Услуги по предоставлению лицензий на компьютерное программное обеспечение",
    )
    assert item.code == "it_services"
    assert item.group == "services"
