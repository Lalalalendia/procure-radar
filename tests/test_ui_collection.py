from procure_radar.ui_collection import (
    CollectionChunkResult,
    parse_collection_chunk,
    should_chain_collection,
)


def test_parse_contract_history_chunk() -> None:
    text = """
contract-history shard=2026-08-10T00:00:00Z..2026-08-10T23:59:59.999999Z skip=200 rows=39 x_total=? contracts=1445 next_skip=239 api_requests=20 avg_rpm=7.95 elapsed=2m31s
contract-history status=paused stop=page_budget region=2 since=2025-06-01 until=2026-08-22 pages=20 rows=1445 contracts=1445 next_skip=0 shards=10 splits=0
current_shard=2026-08-09T00:00:00Z..2026-08-09T23:59:59.999999Z
checkpoint=region=2;since=2025-06-01;until=2026-08-22;q=40cfbe819cd6
progress api_requests=20 avg_rpm=7.95 elapsed=2m31s
"""
    result = parse_collection_chunk(text)
    assert result.status == "paused"
    assert result.stop_reason == "page_budget"
    assert result.current_date == "2026-08-09"
    assert result.rows == 1445
    assert result.items == 1445
    assert result.api_requests == 20
    assert result.avg_rpm == 7.95


def test_parse_paged_sync_chunk() -> None:
    result = parse_collection_chunk(
        "rzn status=paused stop=page_budget next_page=41 rows=1000 pages=10 total=42507\n"
    )
    assert result.status == "paused"
    assert result.next_page == 41
    assert result.rows == 1000
    assert result.total == 42507


def test_chain_only_successful_page_budget_chunks() -> None:
    paused = CollectionChunkResult(status="paused", stop_reason="page_budget")
    complete = CollectionChunkResult(status="complete", stop_reason="last_page")
    assert should_chain_collection(
        "continue_contracts", exit_code=0, chunk=paused, stop_requested=False
    )
    assert not should_chain_collection(
        "continue_contracts", exit_code=0, chunk=paused, stop_requested=True
    )
    assert not should_chain_collection(
        "continue_contracts", exit_code=1, chunk=paused, stop_requested=False
    )
    assert not should_chain_collection(
        "continue_contracts", exit_code=0, chunk=complete, stop_requested=False
    )
    assert not should_chain_collection(
        "refresh_gisp", exit_code=0, chunk=paused, stop_requested=False
    )
