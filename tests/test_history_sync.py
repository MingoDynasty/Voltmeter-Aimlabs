# pylint: disable=duplicate-code

import io
import unittest
from typing import Optional

import play_store
from aimlabs_history import HistoryPage
from history_sync import sync_incremental

ACCOUNT_ID = "anthic-account-a"
SEEN_AT = "2026-06-06T10:00:00.000Z"


def synthetic_play(
    play_id: str,
    *,
    ended_at: str,
    task_id: str = "CsLevel.Lowgravity56.VT Unit.ROAHX3",
) -> dict:
    return {
        "id": play_id,
        "endedAt": ended_at,
        "task": {"id": task_id},
        "score": 1000.0,
        "manifest": {"playDuration": 60000, "pauseDuration": 0},
        "performanceScores": {"hitsTotal": 10, "shotsTotal": 12},
        "gridshieldStatus": "APPROVED",
    }


def history_page(
    plays: list[dict],
    *,
    total_count: int,
    has_next_page: bool = False,
    end_cursor: Optional[str] = None,
) -> HistoryPage:
    return HistoryPage(
        plays=tuple(plays),
        total_count=total_count,
        has_next_page=has_next_page,
        end_cursor=end_cursor,
    )


def save_complete_state(
    connection,
    *,
    newest_id: str = "play-known",
    newest_ended_at: str = "2026-06-06T01:00:00.000Z",
    api_total_count: int = 1,
) -> None:
    play_store.save_sync_state(
        connection,
        play_store.SyncState(
            account_id=ACCOUNT_ID,
            resume_cursor=None,
            backfill_anchor_id=None,
            backfill_phase=play_store.COMPLETE,
            newest_id=newest_id,
            newest_ended_at=newest_ended_at,
            api_total_count=api_total_count,
            updated_at=SEEN_AT,
        ),
    )


def require_state(connection) -> play_store.SyncState:
    state = play_store.get_sync_state(connection, ACCOUNT_ID)
    assert state is not None
    return state


class FakePageFetcher:  # pylint: disable=too-few-public-methods
    def __init__(self, pages: list[HistoryPage], *, fail_on_call: Optional[int] = None) -> None:
        self.pages = list(pages)
        self.fail_on_call = fail_on_call
        self.calls: list[tuple[Optional[str], int]] = []

    def __call__(self, *, after: Optional[str] = None, page_size: int) -> HistoryPage:
        self.calls.append((after, page_size))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("synthetic fetch failure")
        if not self.pages:
            raise AssertionError("unexpected extra page fetch")
        return self.pages.pop(0)


class HistorySyncTests(unittest.TestCase):
    def test_one_page_overlap_finishes_page_before_break(self) -> None:
        connection = play_store.connect(":memory:")
        known_play = synthetic_play("play-known", ended_at="2026-06-06T01:00:00.000Z")
        play_store.upsert_plays(connection, ACCOUNT_ID, [known_play], seen_at=SEEN_AT)
        save_complete_state(connection)
        fetcher = FakePageFetcher(
            [
                history_page(
                    [
                        synthetic_play("play-new-a", ended_at="2026-06-06T03:00:00.000Z"),
                        known_play,
                        synthetic_play("play-new-b", ended_at="2026-06-06T02:00:00.000Z"),
                    ],
                    total_count=3,
                    has_next_page=True,
                    end_cursor="cursor-should-not-be-used",
                ),
            ]
        )

        result = sync_incremental(connection, ACCOUNT_ID, fetcher, seen_at=SEEN_AT)

        self.assertEqual(result.pages_fetched, 1)
        self.assertTrue(result.stopped_on_high_water)
        self.assertEqual(result.inserted, 2)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(play_store.count_plays(connection, ACCOUNT_ID), 3)
        self.assertEqual([call[0] for call in fetcher.calls], [None])

    def test_high_water_captured_at_top_and_finalized_only_after_completion(self) -> None:
        connection = play_store.connect(":memory:")
        known_play = synthetic_play("play-known", ended_at="2026-06-06T01:00:00.000Z")
        play_store.upsert_plays(connection, ACCOUNT_ID, [known_play], seen_at=SEEN_AT)
        save_complete_state(connection)
        top_play = synthetic_play("play-top", ended_at="2026-06-06T05:00:00.000Z")
        fetcher = FakePageFetcher(
            [
                history_page(
                    [top_play, synthetic_play("play-new", ended_at="2026-06-06T04:00:00.000Z")],
                    total_count=5,
                    has_next_page=True,
                    end_cursor="cursor-1",
                ),
                history_page([known_play], total_count=999),
            ]
        )

        result = sync_incremental(connection, ACCOUNT_ID, fetcher, seen_at=SEEN_AT)
        state = require_state(connection)

        self.assertEqual(result.newest_id, "play-top")
        self.assertEqual(result.newest_ended_at, "2026-06-06T05:00:00.000Z")
        self.assertEqual(result.api_total_count, 5)
        self.assertEqual(state.newest_id, "play-top")
        self.assertEqual(state.api_total_count, 5)
        self.assertIsNone(state.resume_cursor)
        self.assertEqual(fetcher.calls, [(None, 50), ("cursor-1", 50)])

    def test_empty_stream_saves_complete_empty_state(self) -> None:
        connection = play_store.connect(":memory:")
        fetcher = FakePageFetcher([history_page([], total_count=0)])

        result = sync_incremental(connection, ACCOUNT_ID, fetcher, seen_at=SEEN_AT)
        state = require_state(connection)

        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.newest_id, None)
        self.assertEqual(result.newest_ended_at, None)
        self.assertEqual(result.api_total_count, 0)
        self.assertEqual(state.backfill_phase, play_store.COMPLETE)
        self.assertIsNone(state.newest_id)
        self.assertIsNone(state.newest_ended_at)
        self.assertEqual(state.api_total_count, 0)
        self.assertEqual(play_store.count_plays(connection, ACCOUNT_ID), 0)

    def test_crash_mid_incremental_restarts_from_top_without_orphan_cursor(self) -> None:
        connection = play_store.connect(":memory:")
        known_play = synthetic_play("play-known", ended_at="2026-06-06T01:00:00.000Z")
        save_complete_state(connection)
        play_store.upsert_plays(connection, ACCOUNT_ID, [known_play], seen_at=SEEN_AT)
        crashing_fetcher = FakePageFetcher(
            [
                history_page(
                    [synthetic_play("play-new-before-crash", ended_at="2026-06-06T04:00:00.000Z")],
                    total_count=2,
                    has_next_page=True,
                    end_cursor="cursor-1",
                ),
            ],
            fail_on_call=2,
        )

        with self.assertRaisesRegex(RuntimeError, "synthetic fetch failure"):
            sync_incremental(connection, ACCOUNT_ID, crashing_fetcher, seen_at=SEEN_AT)
        state_after_crash = require_state(connection)

        self.assertEqual(state_after_crash.newest_id, "play-known")
        self.assertIsNone(state_after_crash.resume_cursor)
        self.assertEqual(play_store.count_plays(connection, ACCOUNT_ID), 2)

        retry_fetcher = FakePageFetcher(
            [
                history_page(
                    [
                        synthetic_play("play-new-before-crash", ended_at="2026-06-06T04:00:00.000Z"),
                        known_play,
                    ],
                    total_count=2,
                ),
            ]
        )
        retry_result = sync_incremental(connection, ACCOUNT_ID, retry_fetcher, seen_at=SEEN_AT)
        state_after_retry = require_state(connection)

        self.assertEqual(retry_fetcher.calls, [(None, 50)])
        self.assertEqual(retry_result.inserted, 0)
        self.assertEqual(retry_result.skipped, 2)
        self.assertEqual(state_after_retry.newest_id, "play-new-before-crash")
        self.assertIsNone(state_after_retry.resume_cursor)

    def test_page_size_validation(self) -> None:
        connection = play_store.connect(":memory:")
        fetcher = FakePageFetcher([])

        for bad_page_size in (0, 201):
            with self.assertRaises(ValueError):
                sync_incremental(connection, ACCOUNT_ID, fetcher, page_size=bad_page_size, seen_at=SEEN_AT)

    def test_total_count_drift_warning_uses_finalized_api_count(self) -> None:
        connection = play_store.connect(":memory:")
        stored_plays = [
            synthetic_play("play-top", ended_at="2026-06-06T03:00:00.000Z"),
            synthetic_play("play-deleted-a", ended_at="2026-06-06T02:00:00.000Z"),
            synthetic_play("play-deleted-b", ended_at="2026-06-06T01:00:00.000Z"),
        ]
        play_store.upsert_plays(connection, ACCOUNT_ID, stored_plays, seen_at=SEEN_AT)
        fetcher = FakePageFetcher([history_page([stored_plays[0]], total_count=2)])

        stderr = io.StringIO()
        result = sync_incremental(connection, ACCOUNT_ID, fetcher, seen_at=SEEN_AT, warning_stream=stderr)

        self.assertEqual(len(result.drift_warnings), 1)
        self.assertIn("totalCount drift", stderr.getvalue())
        self.assertEqual(result.drift_warnings[0].stored_count, 3)
        self.assertEqual(result.drift_warnings[0].api_total_count, 2)


if __name__ == "__main__":
    unittest.main()
