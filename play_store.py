"""SQLite persistence for Aimlabs play history."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Optional, Union

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "aimlabs.db"

BACKFILLING = "BACKFILLING"
TOP_SWEEP = "TOP_SWEEP"
COMPLETE = "COMPLETE"
BACKFILL_PHASES = (BACKFILLING, TOP_SWEEP, COMPLETE)

STABLE_PROJECTION_FIELDS = (
    "task_id",
    "ended_at",
    "score",
    "play_duration",
    "pause_duration",
    "performance_scores",
)

PLAY_COLUMNS = (
    "account_id",
    "id",
    "task_id",
    "ended_at",
    "score",
    "play_duration",
    "pause_duration",
    "gridshield_status",
    "performance_scores",
    "raw",
    "first_fetched_at",
    "last_seen_at",
)


class PlayStoreError(RuntimeError):
    """Raised when play data cannot be projected into the store schema."""


@dataclass(frozen=True)
class FieldDriftWarning:
    account_id: str
    play_id: str
    field_name: str
    old_value: Any
    new_value: Any

    @property
    def message(self) -> str:
        return (
            "field drift: "
            f"account {self.account_id} play {self.play_id} field {self.field_name} "
            f"changed from {self.old_value!r} to {self.new_value!r}"
        )


@dataclass(frozen=True)
class UpsertResult:
    inserted: int
    updated: int
    skipped: int
    drift_warnings: tuple[FieldDriftWarning, ...]


@dataclass(frozen=True)
class SyncState:  # pylint: disable=too-many-instance-attributes
    account_id: str
    resume_cursor: Optional[str]
    backfill_anchor_id: Optional[str]
    backfill_phase: str
    newest_id: Optional[str]
    newest_ended_at: Optional[str]
    api_total_count: Optional[int]
    updated_at: str


def connect(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    resolved_path = _resolve_db_path(db_path)
    if isinstance(resolved_path, Path):
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(resolved_path)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    schema_sql = """
        CREATE TABLE IF NOT EXISTS plays (
          account_id         TEXT NOT NULL,
          id                 TEXT NOT NULL,
          task_id            TEXT NOT NULL,
          ended_at           TEXT NOT NULL,
          score              REAL,
          play_duration      INTEGER,
          pause_duration     INTEGER,
          gridshield_status  TEXT,
          performance_scores TEXT,
          raw                TEXT NOT NULL,
          first_fetched_at   TEXT NOT NULL,
          last_seen_at       TEXT NOT NULL,
          PRIMARY KEY (account_id, id)
        );
        CREATE INDEX IF NOT EXISTS idx_plays_acct_task_date
          ON plays(account_id, task_id, ended_at DESC);
        CREATE INDEX IF NOT EXISTS idx_plays_acct_date
          ON plays(account_id, ended_at DESC);

        CREATE TABLE IF NOT EXISTS sync_state (
          account_id            TEXT PRIMARY KEY,
          resume_cursor         TEXT,
          backfill_anchor_id    TEXT,
          backfill_phase        TEXT NOT NULL
              CHECK (backfill_phase IN ('BACKFILLING','TOP_SWEEP','COMPLETE')),
          newest_id             TEXT,
          newest_ended_at       TEXT,
          api_total_count       INTEGER,
          updated_at            TEXT NOT NULL
        );
        """
    connection.executescript(schema_sql)


def upsert_plays(
    connection: sqlite3.Connection,
    account_id: str,
    raw_plays: Iterable[Mapping[str, Any]],
    *,
    full: bool = False,
    seen_at: Optional[str] = None,
) -> UpsertResult:
    if not account_id:
        raise PlayStoreError("account_id is required.")

    effective_seen_at = seen_at or _utc_now_text()
    inserted = 0
    updated = 0
    skipped = 0
    drift_warnings: list[FieldDriftWarning] = []

    with connection:
        for raw_play in raw_plays:
            play_projection = _project_play(account_id, raw_play, effective_seen_at)
            if full:
                existing_play = get_play(connection, account_id, play_projection["id"])
                if existing_play is None:
                    _insert_play_incremental(connection, play_projection)
                    inserted += 1
                else:
                    drift_warnings.extend(_find_field_drifts(existing_play, play_projection))
                    _update_play_from_raw(connection, play_projection)
                    updated += 1
            elif _insert_play_incremental(connection, play_projection):
                inserted += 1
            else:
                skipped += 1

    _emit_field_drift_warnings(drift_warnings)
    return UpsertResult(
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        drift_warnings=tuple(drift_warnings),
    )


def get_play(connection: sqlite3.Connection, account_id: str, play_id: str) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM plays WHERE account_id = ? AND id = ?",
        (account_id, play_id),
    ).fetchone()


def list_plays_by_account(
    connection: sqlite3.Connection,
    account_id: str,
    *,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    limit_clause, limit_params = _limit_clause(limit, offset)
    query = """
        SELECT * FROM plays
        WHERE account_id = ?
        ORDER BY ended_at DESC, id DESC
        """
    return list(
        connection.execute(
            query + limit_clause,
            (account_id, *limit_params),
        )
    )


def list_plays_by_task(
    connection: sqlite3.Connection,
    account_id: str,
    task_id: str,
    *,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    limit_clause, limit_params = _limit_clause(limit, offset)
    query = """
        SELECT * FROM plays
        WHERE account_id = ? AND task_id = ?
        ORDER BY ended_at DESC, id DESC
        """
    return list(
        connection.execute(
            query + limit_clause,
            (account_id, task_id, *limit_params),
        )
    )


def list_recent_plays(
    connection: sqlite3.Connection,
    *,
    account_id: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    limit_clause, limit_params = _limit_clause(limit, offset)
    if account_id is None:
        query = """
            SELECT * FROM plays
            ORDER BY ended_at DESC, account_id, id DESC
            """
        return list(
            connection.execute(
                query + limit_clause,
                limit_params,
            )
        )

    query = """
        SELECT * FROM plays
        WHERE account_id = ?
        ORDER BY ended_at DESC, id DESC
        """
    return list(
        connection.execute(
            query + limit_clause,
            (account_id, *limit_params),
        )
    )


def count_plays(connection: sqlite3.Connection, account_id: Optional[str] = None) -> int:
    if account_id is None:
        row = connection.execute("SELECT COUNT(*) AS play_count FROM plays").fetchone()
    else:
        row = connection.execute(
            "SELECT COUNT(*) AS play_count FROM plays WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return int(row["play_count"])


def get_sync_state(connection: sqlite3.Connection, account_id: str) -> Optional[SyncState]:
    row = connection.execute(
        "SELECT * FROM sync_state WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_sync_state(row)


def save_sync_state(connection: sqlite3.Connection, sync_state: SyncState) -> None:
    _validate_sync_state(sync_state)
    with connection:
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
            ON CONFLICT(account_id) DO UPDATE SET
                resume_cursor = excluded.resume_cursor,
                backfill_anchor_id = excluded.backfill_anchor_id,
                backfill_phase = excluded.backfill_phase,
                newest_id = excluded.newest_id,
                newest_ended_at = excluded.newest_ended_at,
                api_total_count = excluded.api_total_count,
                updated_at = excluded.updated_at
            """,
            (
                sync_state.account_id,
                sync_state.resume_cursor,
                sync_state.backfill_anchor_id,
                sync_state.backfill_phase,
                sync_state.newest_id,
                sync_state.newest_ended_at,
                sync_state.api_total_count,
                sync_state.updated_at,
            ),
        )


def _resolve_db_path(db_path: Optional[Union[str, Path]]) -> Union[str, Path]:
    if db_path is None:
        return DEFAULT_DB_PATH
    if str(db_path) == ":memory:":
        return ":memory:"
    return Path(db_path)


def _project_play(account_id: str, raw_play: Mapping[str, Any], seen_at: str) -> dict[str, Any]:
    play_id = _required_text(raw_play.get("id"), "id")
    task_value = raw_play.get("task")
    task = task_value if isinstance(task_value, Mapping) else {}
    task_id = _required_text(task.get("id", raw_play.get("task_id")), "task.id")

    manifest_value = raw_play.get("manifest")
    manifest = manifest_value if isinstance(manifest_value, Mapping) else {}
    performance_scores = raw_play.get("performanceScores", raw_play.get("performance_scores"))

    return {
        "account_id": account_id,
        "id": play_id,
        "task_id": task_id,
        "ended_at": _required_text(raw_play.get("endedAt", raw_play.get("ended_at")), "endedAt"),
        "score": _optional_real(raw_play.get("score"), "score"),
        "play_duration": _optional_integer(
            manifest.get("playDuration", raw_play.get("play_duration")),
            "manifest.playDuration",
        ),
        "pause_duration": _optional_integer(
            manifest.get("pauseDuration", raw_play.get("pause_duration")),
            "manifest.pauseDuration",
        ),
        "gridshield_status": _optional_text(
            raw_play.get("gridshieldStatus", raw_play.get("gridshield_status")),
            "gridshieldStatus",
        ),
        "performance_scores": _canonical_json_or_none(performance_scores, "performanceScores"),
        "raw": _canonical_json(raw_play, "raw"),
        "first_fetched_at": seen_at,
        "last_seen_at": seen_at,
    }


def _insert_play_incremental(connection: sqlite3.Connection, play_projection: Mapping[str, Any]) -> bool:
    placeholders = ", ".join(f":{column}" for column in PLAY_COLUMNS)
    columns = ", ".join(PLAY_COLUMNS)
    cursor = connection.execute(
        f"""
        INSERT INTO plays ({columns})
        VALUES ({placeholders})
        ON CONFLICT(account_id, id) DO NOTHING
        """,
        play_projection,
    )
    return cursor.rowcount == 1


def _update_play_from_raw(connection: sqlite3.Connection, play_projection: Mapping[str, Any]) -> None:
    connection.execute(
        """
        UPDATE plays
        SET task_id = :task_id,
            ended_at = :ended_at,
            score = :score,
            play_duration = :play_duration,
            pause_duration = :pause_duration,
            gridshield_status = :gridshield_status,
            performance_scores = :performance_scores,
            raw = :raw,
            last_seen_at = :last_seen_at
        WHERE account_id = :account_id AND id = :id
        """,
        play_projection,
    )


def _find_field_drifts(
    existing_play: sqlite3.Row,
    play_projection: Mapping[str, Any],
) -> list[FieldDriftWarning]:
    drift_warnings = []
    for field_name in STABLE_PROJECTION_FIELDS:
        old_value = existing_play[field_name]
        new_value = play_projection[field_name]
        if old_value != new_value:
            drift_warnings.append(
                FieldDriftWarning(
                    account_id=play_projection["account_id"],
                    play_id=play_projection["id"],
                    field_name=field_name,
                    old_value=old_value,
                    new_value=new_value,
                )
            )
    return drift_warnings


def _emit_field_drift_warnings(
    drift_warnings: Iterable[FieldDriftWarning],
) -> None:
    for drift_warning in drift_warnings:
        print(drift_warning.message, file=sys.stderr)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlayStoreError(f"{field_name} is required and must be a non-empty string.")
    return value


def _optional_text(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlayStoreError(f"{field_name} must be a string when present.")
    return value


def _optional_integer(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlayStoreError(f"{field_name} must be an integer when present.")
    return value


def _optional_real(value: Any, field_name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlayStoreError(f"{field_name} must be numeric when present.")
    return float(value)


def _canonical_json_or_none(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise PlayStoreError(f"{field_name} must be valid JSON when supplied as text.") from error
    return _canonical_json(value, field_name)


def _canonical_json(value: Any, field_name: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise PlayStoreError(f"{field_name} must be JSON-serializable.") from error


def _limit_clause(limit: Optional[int], offset: int) -> tuple[str, tuple[int, ...]]:
    if offset < 0:
        raise ValueError("offset must be greater than or equal to zero.")
    if limit is None:
        if offset:
            return " LIMIT -1 OFFSET ?", (offset,)
        return "", ()
    if limit < 1:
        raise ValueError("limit must be greater than or equal to one.")
    return " LIMIT ? OFFSET ?", (limit, offset)


def _row_to_sync_state(row: sqlite3.Row) -> SyncState:
    return SyncState(
        account_id=row["account_id"],
        resume_cursor=row["resume_cursor"],
        backfill_anchor_id=row["backfill_anchor_id"],
        backfill_phase=row["backfill_phase"],
        newest_id=row["newest_id"],
        newest_ended_at=row["newest_ended_at"],
        api_total_count=row["api_total_count"],
        updated_at=row["updated_at"],
    )


def _validate_sync_state(sync_state: SyncState) -> None:
    if not sync_state.account_id:
        raise PlayStoreError("sync_state.account_id is required.")
    if sync_state.backfill_phase not in BACKFILL_PHASES:
        raise PlayStoreError(f"sync_state.backfill_phase must be one of {BACKFILL_PHASES}.")
    if sync_state.resume_cursor is not None and sync_state.backfill_phase != BACKFILLING:
        raise PlayStoreError("sync_state.resume_cursor is only valid while BACKFILLING.")
    if not sync_state.updated_at:
        raise PlayStoreError("sync_state.updated_at is required.")


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
