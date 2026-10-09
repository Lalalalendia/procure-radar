from __future__ import annotations

from dataclasses import dataclass
import re


_REPEATABLE_ACTIONS = {
    "continue_purchases",
    "continue_contracts",
    "continue_rzn",
    "continue_fsa",
}


@dataclass(frozen=True)
class CollectionChunkResult:
    status: str | None = None
    stop_reason: str | None = None
    current_date: str | None = None
    rows: int | None = None
    items: int | None = None
    api_requests: int | None = None
    avg_rpm: float | None = None
    next_page: int | None = None
    pages: int | None = None
    total: int | None = None

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def paused_for_budget(self) -> bool:
        return self.status == "paused" and self.stop_reason == "page_budget"


def _last_match(pattern: str, text: str, *, flags: int = 0) -> re.Match[str] | None:
    matches = list(re.finditer(pattern, text, flags))
    return matches[-1] if matches else None


def parse_collection_chunk(text: str) -> CollectionChunkResult:
    """Parse the stable summary lines emitted by collection CLI commands.

    The parser intentionally ignores verbose per-row output and only extracts
    information that is useful for the desktop progress UI and chunk chaining.
    """

    status_match = _last_match(
        r"(?:contract-history|history|rzn|fsa)\s+status=(?P<status>\w+)\s+stop=(?P<stop>[\w-]+)",
        text,
    )
    status = status_match.group("status") if status_match else None
    stop_reason = status_match.group("stop") if status_match else None

    shard_match = _last_match(r"current_shard=(?P<date>\d{4}-\d{2}-\d{2})T", text)
    if shard_match is None:
        shard_match = _last_match(r"shard=(?P<date>\d{4}-\d{2}-\d{2})T", text)
    current_date = shard_match.group("date") if shard_match else None

    progress_match = _last_match(
        r"api_requests=(?P<requests>\d+)\s+avg_rpm=(?P<rpm>[-0-9.]+)",
        text,
    )
    api_requests = int(progress_match.group("requests")) if progress_match else None
    avg_rpm: float | None = None
    if progress_match and progress_match.group("rpm") != "-":
        try:
            avg_rpm = float(progress_match.group("rpm"))
        except ValueError:
            avg_rpm = None

    summary_match = _last_match(
        r"(?:contract-history|history)\s+status=\w+.*?\srows=(?P<rows>\d+).*?(?:\scontracts=(?P<items>\d+)|\swindow_rows=(?P<window>\d+))",
        text,
    )
    rows = int(summary_match.group("rows")) if summary_match else None
    items: int | None = None
    if summary_match:
        raw_items = summary_match.group("items") or summary_match.group("window")
        items = int(raw_items) if raw_items is not None else None

    paged_match = _last_match(
        r"(?:rzn|fsa)\s+status=\w+.*?\snext_page=(?P<next>\d+)\s+rows=(?P<rows>\d+)\s+pages=(?P<pages>\d+)\s+total=(?P<total>\d+)",
        text,
    )
    next_page = int(paged_match.group("next")) if paged_match else None
    pages = int(paged_match.group("pages")) if paged_match else None
    total = int(paged_match.group("total")) if paged_match else None
    if paged_match:
        rows = int(paged_match.group("rows"))
        items = rows

    return CollectionChunkResult(
        status=status,
        stop_reason=stop_reason,
        current_date=current_date,
        rows=rows,
        items=items,
        api_requests=api_requests,
        avg_rpm=avg_rpm,
        next_page=next_page,
        pages=pages,
        total=total,
    )


def should_chain_collection(
    action: str | None,
    *,
    exit_code: int,
    chunk: CollectionChunkResult,
    stop_requested: bool,
) -> bool:
    return bool(
        action in _REPEATABLE_ACTIONS
        and exit_code == 0
        and not stop_requested
        and chunk.paused_for_budget
    )


def is_repeatable_action(action: str | None) -> bool:
    return action in _REPEATABLE_ACTIONS
