"""Incremental and resilient sync orchestration for Aimlabs play history."""

# pylint: disable=too-many-arguments,too-many-positional-arguments

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import sys
import time
from typing import Any, Optional, Protocol, TextIO

from aimlabs_history import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AimlabsCursorRejectedError,
    AimlabsTransientHistoryError,
    AimlabsUnauthorizedError,
    HistoryPage,
    validate_page_size,
)
import play_store

DEFAULT_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)


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
class DeletedPlaysWarning:
    account_id: str
    play_ids: tuple[str, ...]

    @property
    def message(self) -> str:
        listed_ids = ", ".join(self.play_ids)
        return (
            "deleted upstream: "
            f"account {self.account_id} has {len(self.play_ids)} local plays "
            f"no longer present upstream: {listed_ids}"
        )


@dataclass(frozen=True)
class FullSyncResult:  # pylint: disable=too-many-instance-attributes
    pages_fetched: int
    inserted: int
    updated: int
    newest_id: Optional[str]
    newest_ended_at: Optional[str]
    api_total_count: int
    drift_warnings: tuple[TotalCountDriftWarning, ...]
    field_drift_warnings: tuple[play_store.FieldDriftWarning, ...]
    deleted_warning: Optional[DeletedPlaysWarning]


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


@dataclass
class SyncCounters:
    pages_fetched: int = 0
    inserted: int = 0
    skipped: int = 0


NowFunc = Callable[[], str]
AuthRefresher = Callable[[], None]
SleepFunc = Callable[[float], None]


def sync_incremental(  # pylint: disable=too-many-locals
    connection: Any,
    account_id: str,
    fetch_page: PageFetcher,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    seen_at: Optional[str] = None,
    now_func: Optional[NowFunc] = None,
    warning_stream: TextIO = sys.stderr,
    refresh_auth: Optional[AuthRefresher] = None,
    sleep_func: SleepFunc = time.sleep,
    retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
) -> IncrementalSyncResult:
    """Sync newest-to-older pages, including initial backfill resilience."""
    if not account_id:
        raise ValueError("account_id is required.")
    effective_page_size = validate_page_size(page_size)
    timestamp_func = _utc_now_text if now_func is None else now_func
    sync_timestamp = seen_at or timestamp_func()
    previous_state = play_store.get_sync_state(connection, account_id)
    fetch_context = FetchContext(
        fetch_page=fetch_page,
        page_size=effective_page_size,
        refresh_auth=refresh_auth,
        sleep_func=sleep_func,
        retry_delays=retry_delays,
    )

    if previous_state is not None and previous_state.backfill_phase == play_store.BACKFILLING:
        counters = SyncCounters()
        return _sync_backfill(
            connection,
            account_id,
            previous_state,
            fetch_context,
            sync_timestamp,
            warning_stream,
            counters,
        )
    if previous_state is not None and previous_state.backfill_phase == play_store.TOP_SWEEP:
        counters = SyncCounters()
        return _sync_top_sweep(
            connection,
            account_id,
            previous_state,
            fetch_context,
            sync_timestamp,
            warning_stream,
            counters,
        )
    if previous_state is None or previous_state.newest_id is None:
        counters = SyncCounters()
        return _sync_empty_or_initial_backfill(
            connection,
            account_id,
            fetch_context,
            sync_timestamp,
            warning_stream,
            counters,
        )
    return _sync_steady_state(
        connection,
        account_id,
        previous_state,
        fetch_context,
        sync_timestamp,
        warning_stream,
    )


def sync_full(  # pylint: disable=too-many-locals
    connection: Any,
    account_id: str,
    fetch_page: PageFetcher,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    seen_at: Optional[str] = None,
    now_func: Optional[NowFunc] = None,
    warning_stream: TextIO = sys.stderr,
    refresh_auth: Optional[AuthRefresher] = None,
    sleep_func: SleepFunc = time.sleep,
    retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    collect_deleted: bool = False,
) -> FullSyncResult:
    """Walk the whole stream from the top, re-deriving every projection from raw (§8.3).

    No checkpoints are written mid-walk — ``resume_cursor`` is ``BACKFILLING``-only
    (decision 13), so a crashed ``--full`` leaves the previous sync state intact and
    is simply re-run. State is finalized once, from the top observation, on completion.
    """
    if not account_id:
        raise ValueError("account_id is required.")
    effective_page_size = validate_page_size(page_size)
    timestamp_func = _utc_now_text if now_func is None else now_func
    sync_timestamp = seen_at or timestamp_func()
    fetch_context = FetchContext(
        fetch_page=fetch_page,
        page_size=effective_page_size,
        refresh_auth=refresh_auth,
        sleep_func=sleep_func,
        retry_delays=retry_delays,
    )

    pages_fetched = 0
    inserted = 0
    updated = 0
    top_id: Optional[str] = None
    top_ended_at: Optional[str] = None
    top_total_count: Optional[int] = None
    field_drift_warnings: list[play_store.FieldDriftWarning] = []
    upstream_play_ids: set[str] = set()
    after: Optional[str] = None

    while True:
        page = _fetch_page_with_resilience(fetch_context, after=after)
        pages_fetched += 1
        raw_plays = list(page.plays)
        if raw_plays and top_id is None:
            top_id = _required_play_text(raw_plays[0], "id")
            top_ended_at = _required_play_text(raw_plays[0], "endedAt")
            top_total_count = page.total_count

        upsert_result = play_store.upsert_plays(connection, account_id, raw_plays, full=True, seen_at=sync_timestamp)
        inserted += upsert_result.inserted
        updated += upsert_result.updated
        field_drift_warnings.extend(upsert_result.drift_warnings)
        if collect_deleted:
            upstream_play_ids.update(_required_play_text(raw_play, "id") for raw_play in raw_plays)

        if not page.has_next_page:
            break
        after = page.end_cursor

    finalized_total_count = top_total_count if top_total_count is not None else 0
    play_store.save_sync_state(
        connection,
        play_store.SyncState(
            account_id=account_id,
            resume_cursor=None,
            backfill_anchor_id=None,
            backfill_phase=play_store.COMPLETE,
            newest_id=top_id,
            newest_ended_at=top_ended_at,
            api_total_count=finalized_total_count,
            updated_at=sync_timestamp,
        ),
    )
    drift_warnings = _total_count_drift_warnings(
        connection,
        account_id,
        finalized_total_count,
        warning_stream,
    )
    deleted_warning = None
    if collect_deleted:
        deleted_warning = _deleted_plays_warning(connection, account_id, upstream_play_ids, warning_stream)
    return FullSyncResult(
        pages_fetched=pages_fetched,
        inserted=inserted,
        updated=updated,
        newest_id=top_id,
        newest_ended_at=top_ended_at,
        api_total_count=finalized_total_count,
        drift_warnings=drift_warnings,
        field_drift_warnings=tuple(field_drift_warnings),
        deleted_warning=deleted_warning,
    )


def _deleted_plays_warning(
    connection: Any,
    account_id: str,
    upstream_play_ids: set[str],
    warning_stream: TextIO,
) -> Optional[DeletedPlaysWarning]:
    stored_play_ids = {row["id"] for row in play_store.list_plays_by_account(connection, account_id)}
    deleted_play_ids = tuple(sorted(stored_play_ids - upstream_play_ids))
    if not deleted_play_ids:
        return None
    warning = DeletedPlaysWarning(account_id=account_id, play_ids=deleted_play_ids)
    print(warning.message, file=warning_stream)
    return warning


@dataclass(frozen=True)
class FetchContext:
    fetch_page: PageFetcher
    page_size: int
    refresh_auth: Optional[AuthRefresher]
    sleep_func: SleepFunc
    retry_delays: Sequence[float]


def _sync_empty_or_initial_backfill(
    connection: Any,
    account_id: str,
    fetch_context: FetchContext,
    sync_timestamp: str,
    warning_stream: TextIO,
    counters: SyncCounters,
) -> IncrementalSyncResult:
    first_page = _fetch_page_with_resilience(fetch_context, after=None)
    counters.pages_fetched += 1
    raw_plays = list(first_page.plays)
    if not raw_plays:
        finalized_total_count = 0
        play_store.save_sync_state(
            connection,
            play_store.SyncState(
                account_id=account_id,
                resume_cursor=None,
                backfill_anchor_id=None,
                backfill_phase=play_store.COMPLETE,
                newest_id=None,
                newest_ended_at=None,
                api_total_count=finalized_total_count,
                updated_at=sync_timestamp,
            ),
        )
        drift_warnings = _total_count_drift_warnings(
            connection,
            account_id,
            finalized_total_count,
            warning_stream,
        )
        return _result(
            counters,
            newest_id=None,
            newest_ended_at=None,
            api_total_count=finalized_total_count,
            stopped_on_high_water=False,
            drift_warnings=drift_warnings,
        )

    anchor_id = _required_play_text(raw_plays[0], "id")
    checkpoint_state = _backfilling_state(
        account_id=account_id,
        anchor_id=anchor_id,
        resume_cursor=_next_resume_cursor(first_page),
        updated_at=sync_timestamp,
    )
    upsert_result = play_store.upsert_plays_and_save_sync_state(
        connection,
        account_id,
        raw_plays,
        checkpoint_state,
        seen_at=sync_timestamp,
    )
    _add_upsert_counts(counters, upsert_result)
    if first_page.has_next_page:
        return _finish_backfill_walk(
            connection,
            account_id,
            anchor_id,
            first_page.end_cursor,
            fetch_context,
            sync_timestamp,
            warning_stream,
            counters,
        )
    return _start_top_sweep(
        connection,
        account_id,
        anchor_id,
        fetch_context,
        sync_timestamp,
        warning_stream,
        counters,
    )


def _sync_backfill(
    connection: Any,
    account_id: str,
    previous_state: play_store.SyncState,
    fetch_context: FetchContext,
    sync_timestamp: str,
    warning_stream: TextIO,
    counters: SyncCounters,
) -> IncrementalSyncResult:
    anchor_id = _required_state_anchor(previous_state)
    return _finish_backfill_walk(
        connection,
        account_id,
        anchor_id,
        previous_state.resume_cursor,
        fetch_context,
        sync_timestamp,
        warning_stream,
        counters,
    )


def _finish_backfill_walk(
    connection: Any,
    account_id: str,
    anchor_id: str,
    after: Optional[str],
    fetch_context: FetchContext,
    sync_timestamp: str,
    warning_stream: TextIO,
    counters: SyncCounters,
) -> IncrementalSyncResult:
    used_cursor_restart = False
    while True:
        try:
            page = _fetch_page_with_resilience(fetch_context, after=after)
        except AimlabsCursorRejectedError:
            if after is None or used_cursor_restart:
                raise
            after = None
            used_cursor_restart = True
            continue

        counters.pages_fetched += 1
        raw_plays = list(page.plays)
        checkpoint_state = _backfilling_state(
            account_id=account_id,
            anchor_id=anchor_id,
            resume_cursor=_next_resume_cursor(page),
            updated_at=sync_timestamp,
        )
        upsert_result = play_store.upsert_plays_and_save_sync_state(
            connection,
            account_id,
            raw_plays,
            checkpoint_state,
            seen_at=sync_timestamp,
        )
        _add_upsert_counts(counters, upsert_result)

        if not page.has_next_page:
            break
        after = page.end_cursor

    return _start_top_sweep(
        connection,
        account_id,
        anchor_id,
        fetch_context,
        sync_timestamp,
        warning_stream,
        counters,
    )


def _start_top_sweep(
    connection: Any,
    account_id: str,
    anchor_id: str,
    fetch_context: FetchContext,
    sync_timestamp: str,
    warning_stream: TextIO,
    counters: SyncCounters,
) -> IncrementalSyncResult:
    top_sweep_state = play_store.SyncState(
        account_id=account_id,
        resume_cursor=None,
        backfill_anchor_id=anchor_id,
        backfill_phase=play_store.TOP_SWEEP,
        newest_id=None,
        newest_ended_at=None,
        api_total_count=None,
        updated_at=sync_timestamp,
    )
    play_store.save_sync_state(connection, top_sweep_state)
    return _sync_top_sweep(
        connection,
        account_id,
        top_sweep_state,
        fetch_context,
        sync_timestamp,
        warning_stream,
        counters,
    )


def _sync_top_sweep(  # pylint: disable=too-many-arguments,too-many-locals
    connection: Any,
    account_id: str,
    previous_state: play_store.SyncState,
    fetch_context: FetchContext,
    sync_timestamp: str,
    warning_stream: TextIO,
    counters: SyncCounters,
) -> IncrementalSyncResult:
    anchor_id = _required_state_anchor(previous_state)
    after: Optional[str] = None
    sweep_top_id: Optional[str] = None
    sweep_top_ended_at: Optional[str] = None
    sweep_top_total_count: Optional[int] = None
    stopped_on_anchor = False

    while True:
        page = _fetch_page_with_resilience(fetch_context, after=after)
        counters.pages_fetched += 1
        raw_plays = list(page.plays)
        if raw_plays and sweep_top_id is None:
            sweep_top_id = _required_play_text(raw_plays[0], "id")
            sweep_top_ended_at = _required_play_text(raw_plays[0], "endedAt")
            sweep_top_total_count = page.total_count

        upsert_result = play_store.upsert_plays(connection, account_id, raw_plays, seen_at=sync_timestamp)
        _add_upsert_counts(counters, upsert_result)

        if _page_contains_play_id(raw_plays, anchor_id):
            stopped_on_anchor = True
            break
        if not page.has_next_page:
            break
        after = page.end_cursor

    finalized_total_count = sweep_top_total_count if sweep_top_total_count is not None else 0
    play_store.save_sync_state(
        connection,
        play_store.SyncState(
            account_id=account_id,
            resume_cursor=None,
            backfill_anchor_id=None,
            backfill_phase=play_store.COMPLETE,
            newest_id=sweep_top_id,
            newest_ended_at=sweep_top_ended_at,
            api_total_count=finalized_total_count,
            updated_at=sync_timestamp,
        ),
    )
    drift_warnings = _total_count_drift_warnings(
        connection,
        account_id,
        finalized_total_count,
        warning_stream,
    )
    return _result(
        counters,
        newest_id=sweep_top_id,
        newest_ended_at=sweep_top_ended_at,
        api_total_count=finalized_total_count,
        stopped_on_high_water=stopped_on_anchor,
        drift_warnings=drift_warnings,
    )


def _sync_steady_state(  # pylint: disable=too-many-arguments,too-many-locals
    connection: Any,
    account_id: str,
    previous_state: play_store.SyncState,
    fetch_context: FetchContext,
    sync_timestamp: str,
    warning_stream: TextIO,
) -> IncrementalSyncResult:
    high_water_id = previous_state.newest_id
    after: Optional[str] = None
    run_top_id: Optional[str] = None
    run_top_ended_at: Optional[str] = None
    run_top_total_count: Optional[int] = None
    stopped_on_high_water = False
    counters = SyncCounters()

    while True:
        page = _fetch_page_with_resilience(fetch_context, after=after)
        counters.pages_fetched += 1
        raw_plays = list(page.plays)
        if raw_plays and run_top_id is None:
            run_top_id = _required_play_text(raw_plays[0], "id")
            run_top_ended_at = _required_play_text(raw_plays[0], "endedAt")
            run_top_total_count = page.total_count

        upsert_result = play_store.upsert_plays(connection, account_id, raw_plays, seen_at=sync_timestamp)
        _add_upsert_counts(counters, upsert_result)

        if high_water_id is not None and _page_contains_play_id(raw_plays, high_water_id):
            stopped_on_high_water = True
            break
        if not page.has_next_page:
            break
        after = page.end_cursor

    finalized_total_count = run_top_total_count if run_top_total_count is not None else 0
    play_store.save_sync_state(
        connection,
        play_store.SyncState(
            account_id=account_id,
            resume_cursor=None,
            backfill_anchor_id=None,
            backfill_phase=play_store.COMPLETE,
            newest_id=run_top_id,
            newest_ended_at=run_top_ended_at,
            api_total_count=finalized_total_count,
            updated_at=sync_timestamp,
        ),
    )
    drift_warnings = _total_count_drift_warnings(
        connection,
        account_id,
        finalized_total_count,
        warning_stream,
    )
    return _result(
        counters,
        newest_id=run_top_id,
        newest_ended_at=run_top_ended_at,
        api_total_count=finalized_total_count,
        stopped_on_high_water=stopped_on_high_water,
        drift_warnings=drift_warnings,
    )


def _fetch_page_with_resilience(fetch_context: FetchContext, *, after: Optional[str]) -> HistoryPage:
    transient_failures = 0
    refreshed_for_request = False
    while True:
        try:
            return fetch_context.fetch_page(after=after, page_size=fetch_context.page_size)
        except AimlabsUnauthorizedError:
            if fetch_context.refresh_auth is None or refreshed_for_request:
                raise
            fetch_context.refresh_auth()
            refreshed_for_request = True
        except AimlabsTransientHistoryError:
            if transient_failures >= len(fetch_context.retry_delays):
                raise
            fetch_context.sleep_func(float(fetch_context.retry_delays[transient_failures]))
            transient_failures += 1


def _backfilling_state(
    *,
    account_id: str,
    anchor_id: str,
    resume_cursor: Optional[str],
    updated_at: str,
) -> play_store.SyncState:
    return play_store.SyncState(
        account_id=account_id,
        resume_cursor=resume_cursor,
        backfill_anchor_id=anchor_id,
        backfill_phase=play_store.BACKFILLING,
        newest_id=None,
        newest_ended_at=None,
        api_total_count=None,
        updated_at=updated_at,
    )


def _next_resume_cursor(page: HistoryPage) -> Optional[str]:
    if not page.has_next_page:
        return None
    return page.end_cursor


def _required_state_anchor(sync_state: play_store.SyncState) -> str:
    if sync_state.backfill_anchor_id is None:
        raise play_store.PlayStoreError("sync_state.backfill_anchor_id is required for backfill recovery.")
    return sync_state.backfill_anchor_id


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


def _add_upsert_counts(counters: SyncCounters, upsert_result: play_store.UpsertResult) -> None:
    counters.inserted += upsert_result.inserted
    counters.skipped += upsert_result.skipped


def _result(
    counters: SyncCounters,
    *,
    newest_id: Optional[str],
    newest_ended_at: Optional[str],
    api_total_count: int,
    stopped_on_high_water: bool,
    drift_warnings: tuple[TotalCountDriftWarning, ...],
) -> IncrementalSyncResult:
    return IncrementalSyncResult(
        pages_fetched=counters.pages_fetched,
        inserted=counters.inserted,
        skipped=counters.skipped,
        newest_id=newest_id,
        newest_ended_at=newest_ended_at,
        api_total_count=api_total_count,
        stopped_on_high_water=stopped_on_high_water,
        drift_warnings=drift_warnings,
    )


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "DEFAULT_RETRY_DELAYS",
    "DeletedPlaysWarning",
    "FullSyncResult",
    "IncrementalSyncResult",
    "PageFetcher",
    "TotalCountDriftWarning",
    "sync_full",
    "sync_incremental",
]
