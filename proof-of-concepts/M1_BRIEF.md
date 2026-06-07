# M1 — Store: implementation brief (for Codex)

**One PR. Foundation milestone — no fetching, no analysis, no auth.** This is the SQLite
store the rest of the pipeline persists to.

## Read first (the spec)
- `RUN_HISTORY_ARCHITECTURE.md` **§7** (data model & schema) + **§7.1** (raw→projection contract).
- **§13** — the `play_store` testing bullet.
- **§14** — the **M1 row is your definition of done.**
- **§15** decisions **2, 3, 4, 5, 17, 21** are *settled* — implement them, don't redesign. If
  something seems wrong, raise it; don't silently deviate.

## Deliverables
- New module **`play_store.py`** — add it to `[tool.setuptools] py-modules` in `pyproject.toml`
  (it's an allowlist; packaging breaks otherwise).
- The **`plays`** and **`sync_state`** tables exactly per §7, including:
  - `PRAGMA user_version = 1;`
  - `plays`: PK `(account_id, id)`; `raw TEXT NOT NULL` (canonical); `first_fetched_at` /
    `last_seen_at`; ISO-8601-UTC text timestamps stored **verbatim**; both indexes
    (`idx_plays_acct_task_date`, `idx_plays_acct_date`).
  - `sync_state`: `resume_cursor`, `backfill_anchor_id`,
    `backfill_phase TEXT NOT NULL CHECK (backfill_phase IN ('BACKFILLING','TOP_SWEEP','COMPLETE'))`,
    `newest_id`, `newest_ended_at`, `api_total_count`, `updated_at`.
- DB at **`data/aimlabs.db`** (path overridable via config; `data/` is gitignored).
- **`upsert_plays(...)`** with the two §7.1 semantics:
  - **incremental** = `INSERT … ON CONFLICT(account_id, id) DO NOTHING` — re-seeing a play is a
    *true no-op* (nothing changes, not even timestamps) → re-ingesting the same data is byte-identical.
  - **`--full` re-derive** = re-derive **all** projection columns from the incoming `raw`, write
    `raw` + projection + `last_seen_at = now()`; emit a visible **"field drift" warning** (naming
    play + field) if a field expected to be stable changed. `raw` always wins.
- **Canonical JSON serialization** for projected JSON text (`performance_scores`, and `raw` if
  re-serialized): `json.dumps(obj, sort_keys=True, separators=(",", ":"))` — so re-derive is
  byte-stable and never raises false drift.
- Store helpers the later milestones need: `sync_state` get/set; queries (by account, by
  `task_id`, global reverse-chron via the date index); stored play count for the drift check.

## Required tests (mock-only, offline — **synthetic fixtures, NEVER a real account dump**)
- Incremental ingest of a fixture **twice → store byte-identical**.
- `--full` with only `gridshield_status` changed → **only it + `last_seen_at`** move.
- `--full` with `raw.score` or `raw.performanceScores` changed → projection rebuilt
  deterministically **+ drift warning**.
- A `performance_scores` fixture whose **key order / whitespace varies but value is equal →
  no drift** (proves canonical serialization).
- Schema creates cleanly; `user_version` set; **`backfill_phase` CHECK rejects** an out-of-enum value.
- Account scoping: two `account_id`s don't collide (PK + indexes).

## Out of scope (do NOT build in M1)
Fetching / pagination / the sync state machine (M2a/M2b), the scenario catalog (M3),
reporting / CLI (M4), auth (M2a). Build *only* the store + its tests.

## Constraints
- Build **into the package**; the PR must pass **CI green** (mypy / pytest / pylint / ruff).
- Follow `CODING_STANDARDS.md` (no single-letter names, no broad excepts, etc.).
- **One milestone = one PR**, branched off `main`; title it **`M1: store`**.
- **No real data or secrets in the diff** — fixtures synthetic only; `.env`/DB stay gitignored.

**Done = every box in the §14 M1 row.** Claude then reviews against `REVIEW_CHECKLIST.md`
before the user merges.
