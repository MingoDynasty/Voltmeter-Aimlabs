import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import play_store

ACCOUNT_ID = "anthic-account-a"
OTHER_ACCOUNT_ID = "anthic-account-b"
FETCHED_AT = "2026-06-06T10:00:00.000Z"
REFETCHED_AT = "2026-06-06T11:00:00.000Z"


def synthetic_play(
    play_id: str = "play-1",
    *,
    task_id: str = "CsLevel.Lowgravity56.VT Unit.ROAHX3",
    ended_at: str = "2026-06-06T02:43:54.249Z",
    score: float = 1234.5,
    performance_scores: object = None,
    gridshield_status: str = "APPROVED",
) -> dict:
    if performance_scores is None:
        performance_scores = {"hitsTotal": 45, "shotsTotal": 50}
    return {
        "id": play_id,
        "endedAt": ended_at,
        "task": {"id": task_id},
        "score": score,
        "manifest": {"playDuration": 60000, "pauseDuration": 250},
        "performanceScores": performance_scores,
        "gridshieldStatus": gridshield_status,
    }


def row_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


class PlayStoreSchemaTests(unittest.TestCase):
    def test_schema_creates_user_version_and_backfill_phase_check(self) -> None:
        connection = play_store.connect(":memory:")

        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'plays'"
            )
        }

        self.assertEqual(user_version, 1)
        self.assertIn("idx_plays_acct_task_date", indexes)
        self.assertIn("idx_plays_acct_date", indexes)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO sync_state (
                    account_id,
                    resume_cursor,
                    backfill_anchor_id,
                    backfill_phase,
                    newest_id,
                    newest_ended_at,
                    api_total_count,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ACCOUNT_ID, None, None, "BROKEN", None, None, None, FETCHED_AT),
            )

    def test_sync_state_round_trips_and_enforces_resume_cursor_phase(self) -> None:
        connection = play_store.connect(":memory:")
        sync_state = play_store.SyncState(
            account_id=ACCOUNT_ID,
            resume_cursor="cursor-1",
            backfill_anchor_id="play-anchor",
            backfill_phase=play_store.BACKFILLING,
            newest_id="play-new",
            newest_ended_at="2026-06-06T02:43:54.249Z",
            api_total_count=12,
            updated_at=FETCHED_AT,
        )

        play_store.save_sync_state(connection, sync_state)
        loaded_state = play_store.get_sync_state(connection, ACCOUNT_ID)

        self.assertEqual(loaded_state, sync_state)
        with self.assertRaises(play_store.PlayStoreError):
            play_store.save_sync_state(
                connection,
                play_store.SyncState(
                    account_id=ACCOUNT_ID,
                    resume_cursor="cursor-should-not-survive",
                    backfill_anchor_id=None,
                    backfill_phase=play_store.COMPLETE,
                    newest_id=None,
                    newest_ended_at=None,
                    api_total_count=None,
                    updated_at=FETCHED_AT,
                ),
            )


class PlayStoreUpsertTests(unittest.TestCase):
    def test_incremental_ingest_twice_leaves_store_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "aimlabs.db"
            connection = play_store.connect(db_path)
            play = synthetic_play()

            try:
                first_result = play_store.upsert_plays(connection, ACCOUNT_ID, [play], seen_at=FETCHED_AT)
                connection.commit()
                first_bytes = db_path.read_bytes()

                second_result = play_store.upsert_plays(connection, ACCOUNT_ID, [play], seen_at=REFETCHED_AT)
                connection.commit()
                second_bytes = db_path.read_bytes()
                row = row_dict(play_store.get_play(connection, ACCOUNT_ID, "play-1"))
            finally:
                connection.close()

        self.assertEqual(first_result.inserted, 1)
        self.assertEqual(second_result.skipped, 1)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(row["first_fetched_at"], FETCHED_AT)
        self.assertEqual(row["last_seen_at"], FETCHED_AT)

    def test_full_rederive_gridshield_change_updates_mutable_projection_without_drift(self) -> None:
        connection = play_store.connect(":memory:")
        play = synthetic_play(gridshield_status="PENDING")
        play_store.upsert_plays(connection, ACCOUNT_ID, [play], seen_at=FETCHED_AT)
        before = row_dict(play_store.get_play(connection, ACCOUNT_ID, "play-1"))

        updated_play = copy.deepcopy(play)
        updated_play["gridshieldStatus"] = "APPROVED"
        warning_messages: list[str] = []
        result = play_store.upsert_plays(
            connection,
            ACCOUNT_ID,
            [updated_play],
            full=True,
            seen_at=REFETCHED_AT,
            warning_sink=warning_messages.append,
        )
        after = row_dict(play_store.get_play(connection, ACCOUNT_ID, "play-1"))

        changed_columns = {
            column
            for column, before_value in before.items()
            if before_value != after[column]
        }
        self.assertEqual(result.updated, 1)
        self.assertEqual(warning_messages, [])
        self.assertEqual(changed_columns, {"gridshield_status", "raw", "last_seen_at"})
        self.assertEqual(after["first_fetched_at"], FETCHED_AT)
        self.assertEqual(after["last_seen_at"], REFETCHED_AT)

    def test_full_rederive_score_and_performance_scores_emit_field_drift(self) -> None:
        connection = play_store.connect(":memory:")
        play_store.upsert_plays(
            connection,
            ACCOUNT_ID,
            [synthetic_play(performance_scores={"shotsTotal": 50, "hitsTotal": 45})],
            seen_at=FETCHED_AT,
        )

        changed_play = synthetic_play(
            score=1300,
            performance_scores={"shotsTotal": 60, "hitsTotal": 55},
        )
        warning_messages: list[str] = []
        result = play_store.upsert_plays(
            connection,
            ACCOUNT_ID,
            [changed_play],
            full=True,
            seen_at=REFETCHED_AT,
            warning_sink=warning_messages.append,
        )
        row = row_dict(play_store.get_play(connection, ACCOUNT_ID, "play-1"))

        self.assertEqual(result.updated, 1)
        self.assertEqual({warning.field_name for warning in result.drift_warnings}, {"score", "performance_scores"})
        self.assertTrue(any("field drift" in message and "play-1" in message for message in warning_messages))
        self.assertEqual(row["score"], 1300.0)
        self.assertEqual(row["performance_scores"], '{"hitsTotal":55,"shotsTotal":60}')

    def test_full_rederive_canonical_performance_scores_avoids_false_drift(self) -> None:
        connection = play_store.connect(":memory:")
        play_store.upsert_plays(
            connection,
            ACCOUNT_ID,
            [synthetic_play(performance_scores={"shotsTotal": 50, "hitsTotal": 45})],
            seen_at=FETCHED_AT,
        )

        same_scores_play = synthetic_play(performance_scores='{\n  "hitsTotal": 45,\n  "shotsTotal": 50\n}')
        warning_messages: list[str] = []
        play_store.upsert_plays(
            connection,
            ACCOUNT_ID,
            [same_scores_play],
            full=True,
            seen_at=REFETCHED_AT,
            warning_sink=warning_messages.append,
        )
        row = row_dict(play_store.get_play(connection, ACCOUNT_ID, "play-1"))

        self.assertEqual(warning_messages, [])
        self.assertEqual(row["performance_scores"], '{"hitsTotal":45,"shotsTotal":50}')

    def test_account_scoping_keeps_duplicate_play_ids_separate(self) -> None:
        connection = play_store.connect(":memory:")
        play = synthetic_play()

        first_result = play_store.upsert_plays(connection, ACCOUNT_ID, [play], seen_at=FETCHED_AT)
        second_result = play_store.upsert_plays(connection, OTHER_ACCOUNT_ID, [play], seen_at=REFETCHED_AT)

        self.assertEqual(first_result.inserted, 1)
        self.assertEqual(second_result.inserted, 1)
        self.assertEqual(play_store.count_plays(connection), 2)
        self.assertEqual(play_store.count_plays(connection, ACCOUNT_ID), 1)
        self.assertEqual(play_store.count_plays(connection, OTHER_ACCOUNT_ID), 1)
        self.assertEqual(
            row_dict(play_store.get_play(connection, ACCOUNT_ID, "play-1"))["first_fetched_at"],
            FETCHED_AT,
        )
        self.assertEqual(
            row_dict(play_store.get_play(connection, OTHER_ACCOUNT_ID, "play-1"))["first_fetched_at"],
            REFETCHED_AT,
        )


class PlayStoreQueryTests(unittest.TestCase):
    def test_queries_return_account_task_and_recent_views(self) -> None:
        connection = play_store.connect(":memory:")
        plays = [
            synthetic_play("play-old", task_id="task-a", ended_at="2026-06-06T01:00:00.000Z", score=10),
            synthetic_play("play-new", task_id="task-a", ended_at="2026-06-06T03:00:00.000Z", score=30),
            synthetic_play("play-other-task", task_id="task-b", ended_at="2026-06-06T02:00:00.000Z", score=20),
        ]
        play_store.upsert_plays(connection, ACCOUNT_ID, plays, seen_at=FETCHED_AT)
        play_store.upsert_plays(
            connection,
            OTHER_ACCOUNT_ID,
            [synthetic_play("other-account-play", task_id="task-a", ended_at="2026-06-06T04:00:00.000Z", score=40)],
            seen_at=FETCHED_AT,
        )

        account_rows = play_store.list_plays_by_account(connection, ACCOUNT_ID)
        task_rows = play_store.list_plays_by_task(connection, ACCOUNT_ID, "task-a")
        recent_rows = play_store.list_recent_plays(connection, account_id=ACCOUNT_ID, limit=2)

        self.assertEqual([row["id"] for row in account_rows], ["play-new", "play-other-task", "play-old"])
        self.assertEqual([row["id"] for row in task_rows], ["play-new", "play-old"])
        self.assertEqual([row["id"] for row in recent_rows], ["play-new", "play-other-task"])

    def test_raw_and_performance_scores_are_canonical_json(self) -> None:
        connection = play_store.connect(":memory:")
        play = {
            "score": 99,
            "task": {"id": "task-canonical"},
            "performanceScores": {"zMetric": 2, "aMetric": 1},
            "id": "play-canonical",
            "manifest": {"pauseDuration": 0, "playDuration": 1000},
            "endedAt": "2026-06-06T05:00:00.000Z",
            "gridshieldStatus": "APPROVED",
        }

        play_store.upsert_plays(connection, ACCOUNT_ID, [play], seen_at=FETCHED_AT)
        row = row_dict(play_store.get_play(connection, ACCOUNT_ID, "play-canonical"))

        self.assertEqual(row["raw"], json.dumps(play, sort_keys=True, separators=(",", ":")))
        self.assertEqual(row["performance_scores"], '{"aMetric":1,"zMetric":2}')


if __name__ == "__main__":
    unittest.main()
