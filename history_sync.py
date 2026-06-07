"""Core incremental sync orchestration for Aimlabs play history."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import sys
from typing import Any, Optional, Protocol, TextIO

from aimlabs_history import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, HistoryPage, validate_page_size
import play_store


class PageFetcher(Protocol):  # pylint: disable=too-few-public-methods
    def __call__(self, *, after: Optional[str], page_size: int) -> HistoryPage:
        """Fetch one newest-to-older history page."""


@dataclass(frozen=True)
class TotalCountDriftWarning:
    account_id: str
    stored_count: int
    api_total_count: int

    @property
    def message(self) -> str:
        missing_count = self.stored_count - self.api_total_count
        return (
            "totalCount drift: "
            f"account {self.account_id} has {self.stored_count} local plays, "
            f"but Aimlabs reports {self.api_total_count}; "
            f"{missing_count} local plays may no longer exist upstream"
        )


@dataclass(frozen=True)
class IncrementalSyncResult:  # pylint: disable=too-many-instance-attributes
    pages_fetched: int
    inserted: int
    skipped: int
    newest_id: Optional[str]
    newest_ended_at: Optional[str]
    api_total_count: int
    stopped_on_high_water: bool
    drift_warnings: tuple[TotalCountDriftWarning, ...]


NowFunc = Callable[[], str]


def sync_incremental(  # pylint: disable=too-many-arguments,too-many-locals
    connection: Any,
    account_id: str,
    fetch_page: PageFetcher,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    seen_at: Optional[str] = None,
    now_func: Optional[NowFunc] = None,
    warning_stream: TextIO = sys.stderr,
) -> IncrementalSyncResult:
    """Sync newest-to-older pages from the top until the previous high-water is seen."""
    if not account_id:
        raise ValueError("account_id is required.")
    effective_page_size = validate_page_size(page_size)
    timestamp_func = _utc_now_text if now_func is None else now_func
    sync_timestamp = seen_at or timestamp_func()
    previous_state = play_store.get_sync_state(connection, account_id)
    high_water_id = previous_state.newest_id if previous_state is not None else None

    after: Optional[str] = None
    run_top_id: Optional[str] = None
    run_top_ended_at: Optional[str] = None
    run_top_total_count: Optional[int] = None
    stopped_on_high_water = False
    pages_fetched = 0
    inserted = 0
    skipped = 0

    while True:
        page = fetch_page(after=after, page_size=effective_page_size)
        pages_fetched += 1
        raw_plays = list(page.plays)
        if raw_plays and run_top_id is None:
            run_top_id = _required_play_text(raw_plays[0], "id")
            run_top_ended_at = _required_play_text(raw_plays[0], "endedAt")
            run_top_total_count = page.total_count

        upsert_result = play_store.upsert_plays(connection, account_id, raw_plays, seen_at=sync_timestamp)
        inserted += upsert_result.inserted
        skipped += upsert_result.skipped

        if high_water_id is not None and _page_contains_play_id(raw_plays, high_water_id):
            stopped_on_high_water = True
            break
        if not page.has_next_page:
            break
        if page.end_cursor is None:
            break
        after = page.end_cursor

    finalized_total_count = run_top_total_count if run_top_id is not None else 0
    if finalized_total_count is None:
        finalized_total_count = 0
    finalized_state = play_store.SyncState(
        account_id=account_id,
        resume_cursor=None,
        backfill_anchor_id=None,
        backfill_phase=play_store.COMPLETE,
        newest_id=run_top_id,
        newest_ended_at=run_top_ended_at,
        api_total_count=finalized_total_count,
        updated_at=sync_timestamp,
    )
    play_store.save_sync_state(connection, finalized_state)
    drift_warnings = _total_count_drift_warnings(
        connection,
        account_id,
        finalized_total_count,
        warning_stream,
    )
    return IncrementalSyncResult(
        pages_fetched=pages_fetched,
        inserted=inserted,
        skipped=skipped,
        newest_id=run_top_id,
        newest_ended_at=run_top_ended_at,
        api_total_count=finalized_total_count,
        stopped_on_high_water=stopped_on_high_water,
        drift_warnings=drift_warnings,
    )


def _page_contains_play_id(raw_plays: Iterable[dict[str, Any]], play_id: str) -> bool:
    return any(raw_play.get("id") == play_id for raw_play in raw_plays)


def _total_count_drift_warnings(
    connection: Any,
    account_id: str,
    api_total_count: int,
    warning_stream: TextIO,
) -> tuple[TotalCountDriftWarning, ...]:
    stored_count = play_store.count_plays(connection, account_id)
    if stored_count <= api_total_count:
        return ()
    warning = TotalCountDriftWarning(
        account_id=account_id,
        stored_count=stored_count,
        api_total_count=api_total_count,
    )
    print(warning.message, file=warning_stream)
    return (warning,)


def _required_play_text(raw_play: dict[str, Any], field_name: str) -> str:
    value = raw_play.get(field_name)
    if not isinstance(value, str) or not value:
        raise play_store.PlayStoreError(f"{field_name} is required and must be a non-empty string.")
    return value


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "IncrementalSyncResult",
    "PageFetcher",
    "TotalCountDriftWarning",
    "sync_incremental",
]
