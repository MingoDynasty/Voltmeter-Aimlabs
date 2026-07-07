# Voltmeter-Aimlabs — Run-History Pipeline: Design

**Status:** Finalized — **rev 9** (incorporates three review rounds plus round-4 → round-8
general findings; M1–M4 + M6 shipped, M5/trend deferred; **M7a/M7b added to §14 on 2026-07-05**
from the accepted [`SCORES_CONSOLIDATION_PROPOSAL.md`](SCORES_CONSOLIDATION_PROPOSAL.md) —
**M7a shipped 2026-07-06 (#45)**, **M7b shipped 2026-07-07 (#48)** — all planned milestones now
merged; only M5/trend deferred)
**Date:** 2026-06-06
**Author:** MingoDynasty
**Audience:** senior engineers; one will likely implement this.
**Scope:** pulling, storing, and analyzing a user's Aimlabs play history. Sibling to
[`ARCHITECTURE.md`](ARCHITECTURE.md), which covers **authentication** and is a dependency of
this design (summarized in §4).

> **Review provenance:** three review rounds are folded into this rev; the points we pushed
> back on or qualified were tracked in a separate review/pushback trail during design. That
> trail was removed at finalization (M6b) and is preserved in git history. Validation numbers
> come from the author's own account (N=919, 2026-06-06) and are evidence, not universal facts
> (§17). The §5.1 pre-M1 live validation is **done**.

---

## 1. Summary

Voltmeter-Aimlabs is an unofficial progress tracker for **Voltaic** aim-training benchmarks
played on **Aimlabs**. Unlike KovaaKs (which writes per-play stats to local files), Aimlabs
stores all play data server-side, reachable only through its GraphQL API behind a login.

This design adds a **run-history pipeline**: it syncs the account's **mode-42 play history**
(see scope, §5.1) into a local SQLite store (incrementally, cheap on repeat runs), maps each
play to its Voltaic scenario, and renders a reverse-chronological runs table plus basic
per-scenario stats. It is the data layer beneath a future evxl-style rank-progress tracker.

The core architectural bet (validated live, §5): fetch the account's mode-42 history as **one
unfiltered, reverse-chronological stream** and bucket by scenario locally — rather than
querying scenario-by-scenario.

---

## 2. Background & glossary

Domain primer for reviewers new to aim trainers:

- **Aimlabs** — an aim-training game. Exposes a GraphQL API at `api.aimlab.gg`.
- **Voltaic** — an organization publishing standardized **benchmarks** (curated scenario sets
  with score thresholds mapping to skill **ranks**), for both KovaaKs and Aimlabs. This
  project targets the **Voltaic × Aimlabs** benchmarks.
- **Scenario / task** — one drill (e.g. "Adjustshot"), identified by a stable string
  **`task_id`** like `CsLevel.Lowgravity56.VT Adjus.RTUQMP`.
- **Play / run** — one attempt, yielding a **score** and detailed **`performanceScores`**
  (accuracy, hits, shots, …). Each play has a stable UUID.
- **Practice run** — a non-benchmark attempt. Aimlabs distinguishes these via `is_practice`
  on the aggregate query; whether the history query exposes it is a **pre-M1 validation item**
  (§5.1).
- **PB** — personal best (max score) on a scenario.
- **anthicId / userId** — Aimlabs' stable per-account identifier. The profile query calls it
  `anthicId`; the leaderboard query calls it `userId`; **they are the same value** (confirmed).
  Config: `[aimlabs].user_id` in `config.toml` (AppConfig attr `aimlabs_user_id`); §12
  (design review rounds 1 and 3).
- **gridshield** — Aimlabs' anti-cheat verdict on a play (`APPROVED`, or flagged).
- **`taskMode`** — a play-mode discriminator. Voltaic benchmark plays are mode `42`.
- **Relay cursor** — the pagination style the `plays` connection uses
  (`pageInfo.hasNextPage` + `endCursor` → next page's `after`).
- **evxl** ([evxl.app](https://evxl.app)) — an existing rank-progress tracker for *KovaaKs*;
  nothing comparable exists for Aimlabs. This project aims to fill that gap.
- **Season** — Voltaic revises its benchmark set periodically (S1, S2, S3…). Each season is a
  separate resource file; **task_ids are minted fresh each season** (§9).

---

## 3. Goals & non-goals

**Goals**
- Sync the configured account's mode-42 play history into a local store, incrementally.
- Survive large first-time backfills (resumable; auth-refresh-aware).
- Map plays to Voltaic scenarios via a metadata catalog that improves over time without
  re-fetching plays.
- Render a reverse-chronological runs table (`Date | Scenario | Score | Stats`) and basic
  per-scenario rolling stats, computed offline from the store.

**Non-goals (now)**
- Trend classification / cold-score analysis (the hard, easy-to-get-wrong part — §10.3).
- Voltaic energy/rank computation over history (the repo already does this for PBs; future, §16).
- Multi-account *features* (schema is multi-account-safe, but no account-switching UX — §7).
- A hosted/multi-user service (out of scope; auth doc §8 explains why).
- Non-Voltaic scenarios as a first-class surface (kept in storage, parked in "other" — §9).
- A packaged/installable distribution (remains source-checkout tooling; §7).

---

## 4. Dependency: authentication

Full detail in [`ARCHITECTURE.md`](ARCHITECTURE.md). What this pipeline relies on:

- The target query (`aimlabProfile.plays`) is **account-scoped and requires auth**; anonymous
  calls return `UNAUTHENTICATED`.
- The durable credential is a **NextAuth session cookie** from `aimlabs.com` (~30-day life),
  stored locally, exchanged on demand for a short-lived (~1 h) **bearer** via
  `GET aimlabs.com/api/auth/session`, sent to `api.aimlab.gg/graphql`.
- A `login` command captures the session cookie via an embedded browser (handles MFA/captcha).
  The credential **never leaves the machine**.

> **Rotating token chain (observed live, §17):** the cookie's *identity* lifetime (`expires`,
> ~30 days) and its *token-minting* contents are decoupled. `/api/auth/session` can consume a
> single-use upstream refresh token and return both a fresh `accessToken` and a rotated
> `Set-Cookie` session link. The auth layer persists that rotated link to `data/session.json`
> under a single-flight lock, so a normal 401 re-mint is recoverable. `RefreshAccessTokenError`
> remains the residual **"re-login required"** state when the session is gone, the token family
> was forked by another consumer, or the managed state was lost/corrupt — *not* a transient 5xx
> and not merely "cookie expired."

**Production auth policy** (per review blocker #4):

- **`AIMLAB_SESSION` (session cookie) is the canonical credential for `sync`.** A raw bearer
  (`AIMLAB_TOKEN` / manual `Authorization`) is **debug/short-run only** and is *not* equivalent
  — long backfills require the session cookie so the **401 re-mint** (§8.4) can work.
- **`report` and other offline commands never resolve auth, never touch the network, never
  trigger login** (§10, §11).
- **`sync` never opens a login window (review round-8 #1).** It resolves the credential from
  file/env channels only (`--session-file` / `data/session.json` / `$AIMLAB_SESSION` / `.env`);
  if none is present or the residual terminal auth state is reached, it **fails with "run
  `voltmeter login`"** — it does not pop a browser. `login` is the *only* command that opens the
  interactive window. This makes unattended/scheduled the natural default, so there is no
  `--no-login` flag and no interactive-desktop detection in `sync`.

The implementation assumes `get_bearer()` returns a fresh bearer (minting from the session
cookie as needed) and can **re-mint mid-run on a 401** (§8.4).

---

## 5. Key insight: one unfiltered global stream

A naive design queries history **per scenario** (send a `taskId` filter, paginate, repeat).
That is the wrong model:

- You'd need the full list of task_ids up front — but you don't have it. The benchmark
  resources know ~151 scenarios across seasons; a real account spans more (the author's has
  122 distinct task_ids — practice/retired/seasonal variants). A per-scenario sweep silently
  misses anything not in your list.
- It means N cursors and N high-water marks for incremental sync.

The `aimlabProfile.plays` connection accepts **no `taskId` filter**, returning **every play**
as one Relay-paginated, **reverse-chronological** stream, with `task { id }` per node. So:
**paginate one stream, bucket by `node.task.id` locally.** One cursor, one high-water mark,
scenario discovery from the data.

**Validated live** (author's account, 2026-06-06): returned all **919 plays**
reverse-chronologically, every node carrying `task.id`, spanning **122 task_ids**, all
`gridshieldStatus = APPROVED`. The query sends `filter: { mode: 42 }` and selects `task { id }`.

> ⚠️ **Do not rely on single-page responses.** At the author's scale the server returned all
> 919 in one page (no `first` supplied). That is a default cap exceeding 919, not a contract.
> The implementation **must** send an explicit page size and follow the cursor (§8).

### 5.1 Pre-M1 live-validation checklist

Probed against the live account 2026-06-06 (results in §17). Status:

- [x] **Practice runs — resolved:** the history `Play` node exposes **no** `is_practice`
  field and `PlayFilterInput` has **no** practice filter (so we cannot tag/exclude practice
  on the history stream). It doesn't matter: the aggregate endpoint shows
  `task_mode == 42 & is_practice == true` = **0** and `… == false` = **919** (matching the
  history `totalCount`). So **`mode: 42` already excludes 100% of practice plays.** Caveat —
  see contamination check below: this is one account at one time, not a structural guarantee.
- [x] **Input device — resolved:** `inputDevice`/`input_device`/`deviceType` are **not**
  exposed on the `Play` node. Per-input-device analysis isn't possible from this stream (would
  need another source) — treat as a non-goal.
- [x] **`userId == anthicId`:** confirmed equal — one config key (`[aimlabs].user_id`).
- [ ] **Cursor expiry — still open (non-blocking):** whether a stored Relay `endCursor`
  survives between runs is unverified. The M2b **cursor-rejection → top-restart fallback**
  (§8.2) makes this safe regardless, so it doesn't block M1/M2; confirm opportunistically.
- [x] **Scale/paging:** single-page at 919 confirmed; **`first` must be ≥ 1** (`first: 0`
  returns HTTP 500). Server introspection is disabled (no schema dumping).

**Scope decision (final):** sync **all authenticated mode-42 plays for the configured
account** = the account's benchmark plays. Practice is excluded by the `mode: 42` filter
(live-verified), so **the `is_practice` column is dropped** and benchmark validity is inferred
from **catalog membership + `mode == 42`**.

**Practice-contamination safety net (because we can't filter practice on the history stream):**
since the "mode 42 ⇒ no practice" result is empirical for one account, add a cheap per-sync
check using the aggregate endpoint (§5.2): `count(user_id, task_mode=42, is_practice=true)` —
if **> 0**, warn that practice plays have entered the mode-42 stream (they would otherwise
silently pollute stats, with no per-row way to exclude them). Same drift-signal philosophy as
the `totalCount` check (§8.3).

### 5.2 A second, complementary endpoint: `plays_agg` (server-side counts)

Probing surfaced a capable aggregate endpoint distinct from the history connection:
`aimlab.plays_agg(where: AimlabPlayWhere)` returns server-side `count` / `avg` / `max` filtered
by `user_id`, `task_id`, `task_mode`, and `is_practice` (note: the aggregate's mode field is
**`task_mode`**, vs the history filter's **`mode`**). It is **not** the sync source (no
pagination, no per-play rows), but it's a cheap cross-check tool — used for the contamination
check above, and available for count reconciliation. The package wraps it in
`aimlabs_history.fetch_practice_contamination_count`. One gotcha: its `max{}` aggregate **500s on
an empty result set**, so contamination/edge queries should select `count` only.

---

## 6. Architecture & module decomposition

Four layers, each independently testable; the network boundary is **injectable** so analysis
and store logic test offline:

```
  auth          fetch              store              analyze / present
  (existing) ─▶ history stream ─▶  SQLite       ─▶    runs table + stats
                (paginate)         (upsert)            (offline, no network)
                                      ▲                      │
                                      └── scenario catalog ──┘
                                          (task_id → metadata, rebuildable)
```

Built **into the package**, reusing existing modules. New modules:

| Module | Responsibility | Depends on | Network? |
|---|---|---|---|
| `aimlabs_auth.py` | session→bearer exchange, `login` capture, credential resolution, **401 re-mint** | `config` | yes |
| `aimlabs_history.py` | build the unfiltered payload, paginate, parse nodes → play dicts (stateless) | `aimlabs_client` | yes (via injected client) |
| `play_store.py` | SQLite schema + migrations, upsert, `sync_state`, queries, drift check | stdlib `sqlite3` | no |
| `history_sync.py` | orchestrates the sync state machine: early-break, resume, 401 re-mint, backoff, drift | `aimlabs_auth`, `aimlabs_history`, `play_store` | yes (boundary injected) |
| `scenario_catalog.py` | union season resources → catalog records; rebuildable; resolver interface for a future API source | `voltaic_benchmarks` resources | no |
| `history_report.py` | offline runs table + basic rolling stats (store-only) | `play_store`, `scenario_catalog` | **no — ever** |
| `cli.py` | argparse subcommands: `sync` / `login` / `report` / `refresh-catalog` / `scores` | all of the above | — |

Keep `aimlabs_history.py` **stateless and separate** from `history_sync.py` (per review) so
pagination/parse logic is trivially testable. **Reused as-is:** `aimlabs_client.py`,
`voltaic_benchmarks.py`, `config.py` (extended, §11). New modules
must be added to `[tool.setuptools] py-modules` in `pyproject.toml`.

**Single-writer assumption:** one process at a time. We rely on SQLite file locking but do not
design for concurrent invocations (fine for a local single-user tool).

---

## 7. Data model & storage

**Engine: SQLite, single file.** Stdlib, zero deps, portable, fits the local-only model;
scales to millions of rows with the indexes below. JSONL was rejected (re-parses everything);
a DB server was rejected (local-only).

**Location:** `data/aimlabs.db` (project-relative, gitignored), overridable in `config.toml`.
Matches the source-checkout reality; an OS user-data dir via `platformdirs` is a distant maybe.

**Account scoping:** single-account *product*, account-stamped *storage*. `account_id` (the
stable anthicId) is on every row — one column, needed internally (per-account high-water mark
and drift check), and averts a migration if multi-account ever matters.

**Timestamps:** store the API's ISO-8601 UTC string **verbatim** (e.g.
`2026-06-06T02:43:54.249Z`) — simultaneously "as-is," "ISO," and an unambiguous instant; it
sorts lexicographically (the high-water mark and `ended_at DESC` indexes rely on this).
**Never store local/naive time.** Timezone is a display concern only (§10).

**Raw is canonical (per review):** every play
stores its full raw node JSON. Every other column except our two metadata timestamps is a
**pure projection of `raw`, re-derived whenever `raw` is written** — so "raw wins" is
structural, not documentary (see §7.1). `mode`, `weapon_id`, and `is_practice` are not stored:
`task_mode` is always 42; `task_id → weapon_id` is 1:1 in the catalog (both derivable from
`task_id`); and the history node exposes no practice field — mode 42 already excludes practice
(§5.1), so an `is_practice` column would be both unpopulatable and unnecessary.

```sql
PRAGMA user_version = 1;               -- schema version; bump + migrate on change (§16)

CREATE TABLE plays (
  account_id         TEXT NOT NULL,     -- stable anthicId
  id                 TEXT NOT NULL,     -- play UUID; dedup + upsert key
  task_id            TEXT NOT NULL,     -- node.task.id; scenario bucket key
  ended_at           TEXT NOT NULL,     -- ISO-8601 UTC, verbatim; high-water mark
  score              REAL,
  play_duration      INTEGER,           -- manifest.playDuration (ms)
  pause_duration     INTEGER,
  gridshield_status  TEXT,              -- projection of raw (anti-cheat verdict)
  performance_scores TEXT,              -- projection of raw (parsed JSON)
  raw                TEXT NOT NULL,     -- full node JSON; CANONICAL — projection re-derived from this
  first_fetched_at   TEXT NOT NULL,     -- our metadata; set on insert, immutable
  last_seen_at       TEXT NOT NULL,     -- our metadata; bumped on --full reconcile
  PRIMARY KEY (account_id, id)
);
CREATE INDEX idx_plays_acct_task_date ON plays(account_id, task_id, ended_at DESC); -- per-scenario stats
CREATE INDEX idx_plays_acct_date      ON plays(account_id, ended_at DESC);          -- global runs table

CREATE TABLE sync_state (
  account_id            TEXT PRIMARY KEY,  -- stable anthicId (NEVER a username, §11)
  resume_cursor         TEXT,              -- Relay endCursor; BACKFILLING-only, NULL in steady state (§8.1/§8.2)
  backfill_anchor_id    TEXT,              -- newest id seen when the first backfill began (§8.2)
  backfill_phase        TEXT NOT NULL      -- durable phase (§8.2)
      CHECK (backfill_phase IN ('BACKFILLING','TOP_SWEEP','COMPLETE')),
  newest_id             TEXT,              -- high-water mark for incremental sync
  newest_ended_at       TEXT,
  api_total_count       INTEGER,           -- finalized freshest-top totalCount; drives drift signal (§8.1/§8.3)
  updated_at            TEXT NOT NULL
);
```

The scenario catalog (§9) is a separate, rebuildable projection — plays never depend on it.

### 7.1 Data mutability & the raw→projection contract (review v2, blocker #1)

`raw` is the single source of truth; every play column except our two metadata timestamps is a
**pure projection of `raw`, re-derived whenever `raw` is written.** This makes "raw wins"
structural rather than documentary — there is no path that updates `raw` while leaving a
projection column stale.

- **Projection columns** (derived from `raw`): `task_id`, `ended_at`, `score`,
  `performance_scores`, durations, `gridshield_status`.
- **Our metadata** (not from `raw`): `first_fetched_at` (set on insert, immutable),
  `last_seen_at` (bumped on `--full`).
- **Semantic expectation:** the gameplay facts (`score`, `ended_at`, `task_id`, durations,
  `performance_scores`) are not *expected* to change upstream; `gridshield_status` can.
  "Expected stable" drives a warning, not a storage rule.
- **Upsert semantics:**
  - **Incremental `sync`** = insert-only: `INSERT … ON CONFLICT(account_id, id) DO NOTHING`.
    Re-seeing a play (e.g. one-page overlap) is a true no-op — nothing changes, not even
    timestamps — so re-ingesting the same data leaves the store byte-identical.
  - **`sync --full`** = for each play, **re-derive the full projection from the incoming `raw`**
    and write `raw` + all projection columns + `last_seen_at = now()`. If a field we expected to
    be stable changed (e.g. `score`), the re-derive still wins (raw is canonical) and a
    **visible "field drift" warning** is emitted naming the play + field.
- **Deterministic projection (per review v3 #3).** Projected JSON/text columns
  (`performance_scores`, and `raw` itself) must serialize **canonically** — stable key order,
  compact separators (`json.dumps(obj, sort_keys=True, separators=(",", ":"))`) — so the same
  logical value always yields the same bytes. Otherwise key-order/whitespace/float-format
  variance produces **false drift warnings** on `--full` and flaky tests. *(Scope: this matters
  for the `--full` re-derive/compare path; the incremental no-op never re-serializes, so it's
  byte-identical by construction. Equivalent alternative: detect drift by comparing **parsed
  structures**, not serialized strings.)*
- **Idempotency / drift tests:** incremental ingest twice → byte-identical. `--full` with only
  `gridshield_status` changed → only that column + `last_seen_at` change. `--full` with
  `raw.score` or `raw.performanceScores` changed → projection rebuilt deterministically + drift
  warning. **A fixture where `performance_scores` key order/whitespace varies but the value is
  equal → no drift** (proves canonical serialization).

---

## 8. Sync algorithm

### 8.1 Incremental early-break

Reverse-chronological stream + immutable play `id` ⇒ fetch newest-first, stop at known data:

This is the steady-state (`COMPLETE`-phase) path. It **always restarts from the top** of the
stream — it does *not* use `resume_cursor` (that's a `BACKFILLING`-only artifact, §8.2):

```
high_water_id = sync_state.newest_id      # the completed-sync high-water from last run
run_top_id = run_top_ended_at = None      # captured from THIS run's first NON-EMPTY page
run_top_total_count = None                # totalCount as seen at the TOP of this run
seen_known = False
for page in pages(newest → older, following endCursor, from the TOP):   # NOT from resume_cursor
    if page and run_top_id is None:       # first row we actually see this run
        run_top_id, run_top_ended_at = page[0].id, page[0].ended_at
        run_top_total_count = page.totalCount
    BEGIN TRANSACTION
      for node in page: upsert(node)      # INSERT OR IGNORE; re-seeing is a no-op
      if any node.id == high_water_id: seen_known = True
    COMMIT                                # no resume_cursor write in steady state
    if seen_known: break                  # finish the page first, THEN stop
    if not pageInfo.hasNextPage: break

# Finalize HIGH-WATER + totalCount only after the run completes safely (NOT per page):
finalize:
    if run_top_id is not None:            # saw at least one play
        newest_id, newest_ended_at = run_top_id, run_top_ended_at
        api_total_count = run_top_total_count
    # else (empty stream): newest_id = NULL, api_total_count = 0  (see "Empty stream" below)
    run drift check (§8.3)
```

- **`resume_cursor` is `BACKFILLING`-only; incremental restarts from the top (review round-6 #1).**
  In steady state we *don't* persist mid-run progress: a run restarts from the top each time, walks
  down to `high_water_id`, and finalizes `newest_id`/`api_total_count` only on safe completion. So a
  **crash mid-incremental simply discards the in-memory run-top** — the next run re-reads the true
  top and re-finalizes; `newest_id` never advanced, nothing is lost, and there's no orphaned cursor.
  This is correct *because* the incremental walk is cheap (O(new plays)) and idempotent. Writing
  `resume_cursor` here would create exactly the ambiguity the reviewer flagged — a cursor pointing
  mid-stream with the run-top values gone. `resume_cursor` therefore exists **only** for the
  expensive `BACKFILLING` walk (§8.2); it is cleared at backfill completion and stays NULL.
- **`newest_id` and `api_total_count` are *completed-sync* values** — captured from the top and
  finalized only at the end, never per page. If `newest_id` advanced per page it would drift
  *downward* and a later run would early-break too early; if `api_total_count` were a stale mid-run
  value it would **false-warn** the §8.3 drift check (review round-4 #4). Rule: *capture at top,
  finalize from the freshest top observation.*
- **Empty stream (per review v3 #1).** A new account, or any zero-result state, returns an
  **empty first page** — the `run_top` capture must guard against indexing `page[0]`. On an
  empty stream: insert no rows, set `newest_id = NULL`, `newest_ended_at = NULL`,
  `api_total_count = 0`, set `backfill_phase = COMPLETE`, and the report renders "no runs found."
  A subsequent run re-checks from the top; if it now finds plays, that's an **initial backfill**
  (see §8.2 — the backfill trigger keys off `newest_id IS NULL`, not the phase). **`page_size`
  must be validated `≥ 1`** (`first: 0` returns HTTP 500, §5.1).
- **Finish the page before breaking** — a page can mix new + cached; breaking mid-page on the
  first cached id would skip newer plays after it.
- **Cost is O(new plays)** — a daily sync walks one or two pages and stops.

### 8.2 Resumable backfill & sync state machine (per review blocker #2)

**What triggers an initial backfill (review round-8 #4): `newest_id IS NULL`,** evaluated at sync
start — *not* the phase. This unifies "brand-new account's first sync" and "an account that was
empty and later gained plays" — both have `newest_id IS NULL`, and both should get the full
backfill (resumability + the mid-walk-additions top sweep). So:
- `newest_id IS NULL` + **empty** first page → no-op, stay `COMPLETE` (idle, §8.1).
- `newest_id IS NULL` + **non-empty** first page → **initial backfill**: set `BACKFILLING`, record
  `backfill_anchor_id`, walk, top sweep, finalize. (A prior empty run leaves `phase = COMPLETE`;
  this transition `COMPLETE → BACKFILLING` is valid and expected.)
- `newest_id` set → incremental (§8.1).

The first backfill of a large account (~2,000 pages at 100k plays) is the only expensive op.

**Durable phase machine (`backfill_phase`, per review round-5 #1).** A boolean `backfill_complete`
can't tell "still walking old pages" from "old walk done, top sweep not yet run" — both are
`0` — so a crash between them is ambiguous. Use an explicit, persisted enum with three states:

| `backfill_phase` | meaning | a fresh run does | uses `resume_cursor`? |
|---|---|---|---|
| `BACKFILLING` | walking old pages toward the end | resume from `resume_cursor` | **yes** — the only phase that does |
| `TOP_SWEEP` | old-page walk done; sweep not yet finalized | (re)run the top sweep, from the top | no — restart-from-top |
| `COMPLETE` | steady state | incremental sync from the top (§8.1) | no — restart-from-top |

`resume_cursor` is meaningful **only in `BACKFILLING`** (the one walk expensive enough to be worth
resuming); it's cleared at completion and NULL otherwise. `TOP_SWEEP` and `COMPLETE` are bounded
and idempotent, so they always restart from the top and a crash just re-runs them (review round-6 #1).

Transitions, each committed in its own transaction so recovery is unambiguous:

- **Start:** first run sets `BACKFILLING` and records `backfill_anchor_id` (the newest id at
  backfill start) on the first page. Pages commit atomically with their `resume_cursor` checkpoint
  (a 401/crash before commit just re-fetches that page — idempotent upsert makes it safe).
- **End of old-page walk → commit `TOP_SWEEP` *before* running the sweep.** This is the durable
  marker the boolean lacked: a crash here leaves `TOP_SWEEP` persisted, so the next run knows to
  run the sweep rather than mis-resume old pages.
- **Top sweep:** sweep from the **top** down to `backfill_anchor_id` — captures plays that arrived
  *while* the (possibly minutes-long) backfill ran (happens even on an uninterrupted run). The
  sweep is bounded and idempotent, so re-running it after a crash is safe (no separate checkpoint
  needed).
- **Sweep done → commit `COMPLETE`,** finalizing `newest_id`/`newest_ended_at` **and
  `api_total_count`** to the **sweep's** top-of-stream values (freshest, not a mid-backfill value —
  review round-4 #4).

Crash recovery is then total: crash in `BACKFILLING` → resume cursor; crash after the old walk
(or during/after the sweep, before `COMPLETE`) → `TOP_SWEEP` persisted → re-run the idempotent
sweep. No state is ambiguous.

- **Cursor-invalidation fallback:** if a stored `resume_cursor` is rejected, **restart from the
  top of the stream** and rely on idempotent upserts to skip already-stored plays — never fail
  permanently. (Whether cursors actually expire is a §5.1 validation item.)
- **Page size:** `page_size` (the Relay `first`) defaults to **50**, config-validated to **1–200**
  (review round-4 #3). Because we follow the cursor, it's a politeness/efficiency knob, not a
  correctness one: if the server *caps* an over-large `first` and under-delivers, the loop simply
  continues from `endCursor` (harmless). Only a hard *rejection* of a `first` value matters — the
  bounded default avoids it; if a future server change rejects it, **halve and retry**.

### 8.3 Drift signals (cheap, per-sync warnings)

Per-play facts are immutable. Three things can drift, each surfaced by a cheap warning:

- **Status** (`gridshield_status` flips) — refreshed by `sync --full`, which walks the stream
  and re-derives the projection from each play's latest `raw` (§7.1).
- **Membership** (a play deleted upstream — not expected from Aimlabs). Detect via a **cheap
  drift signal, not deletion detection** (per review; reasoning corrected in design-review
  round 1): compare stored count to the **finalized**
  `api_total_count` — the freshest top-of-stream value (§8.1/§8.2), *not* a mid-backfill page
  value, or it false-warns. `stored > api_total_count` ⇒ **warn** ("N local plays no longer
  upstream"). This is a signal, not proof; precise identification needs a full id set-diff,
  relegated to opt-in `sync --full --show-deleted`. Right effort:value given deletions aren't
  expected.
- **Practice contamination** (we can't filter practice on the history stream, §5.1). Once per
  sync, query the aggregate endpoint for `count(user_id, task_mode=42, is_practice=true)`
  (§5.2). It is **0** today (live-verified), but if it ever goes **> 0**, **warn** that practice
  plays have entered the mode-42 stream and stats may be polluted. Cheap insurance against the
  "mode 42 ⇒ no practice" assumption being one-account-specific.

### 8.4 Auth refresh, rate limiting & late writes

- **Bearer re-mint on 401:** a long backfill can exceed the ~1 h bearer lifetime. On a 401
  mid-pagination, re-mint from the session cookie (§4) and continue. **Requires session-cookie
  auth** (a raw bearer can't re-mint). Daily syncs never hit this.
  A successful re-mint persists the rotated session cookie before any heavy API work, so the next
  run continues the same token chain. **But re-mint can still fail terminally:** if the session
  route returns `RefreshAccessTokenError` (§4), no bearer can be minted — the sync must **stop
  and surface "re-login required,"** **not** retry-loop. After the user re-logs in, the next run
  resumes **per phase (review round-8 #2):** `BACKFILLING` resumes from `resume_cursor`;
  `TOP_SWEEP` and `COMPLETE` (incremental) restart from the top (idempotent, §8.1/§8.2) — there
  is no `resume_cursor` to rely on outside `BACKFILLING`.
- **Rate limiting / transient errors:** exponential backoff on **HTTP 429 and transient 5xx**
  — this is an **M2 requirement** (the large-backfill path *is* M2), mock-tested (§13).
- **Late/out-of-order writes:** the algorithm assumes append-only, stable ordering. If violated
  (server backfill, clock skew), `sync --full` is the backstop.

---

## 9. Scenario catalog (metadata projection)

**Principle:** the play store is the source of truth keyed by stable `task_id`. Mapping
`task_id → metadata` is a **separate projection that improves over time** and must **never
require re-fetching plays**. Ingest stores `task_id` regardless of whether it can be named yet;
"fill in the blanks later" is the normal flow.

**Sources** (resolved as a union, behind one interface):
- **Per-season resource files** `resources/aimlabs/*.json` (`valorant_s1`, `aimlabs_s2`,
  `aimlabs_s3`, future seasons). Identical schema.
- **A future Aimlabs task-info API** (query metadata by task_id) — slots in as another source
  with a local cache. The enabler for non-Voltaic naming.

**Catalog record fields (per review — these distinguish product surfaces):** the resources are
*not* one surface, and the metadata to tell them apart is already in the files. Each catalog
record carries at least:

| field | source | example |
|---|---|---|
| `name`, `category`, `sub`, `difficulty` | scenario + tiers | "Adjustshot", Intermediate |
| `season` | resource `season` | 1 / 2 / 3 |
| `benchmark_alias` | resource `alias` | `valorant_s1`, `aimlabs_s2` |
| `benchmark_name` | resource `name` | "Voltaic Valorant Benchmarks" vs "Voltaic Aimlabs Benchmarks" |
| `family` | derived | `valorant` (S1) vs `aimlabs` (S2/S3) |
| `is_active`, `has_leaderboards` | resource | (S2 has `has_leaderboards = false`) |

This lets the report choose "VALORANT only", "active only", or "all known Voltaic Aimlabs"
**without reworking storage** — selected via `config.report_family` (default `all`; see §10.1).

**Scope (Voltaic-first).** The catalog *is* the Voltaic boundary: a resolved `task_id` is a
tracked Voltaic scenario (product surface); an unresolved one is a not-yet-loaded season or a
non-Voltaic scenario, parked in a secondary "other / unclassified" bucket — **kept, not
dropped**.

**Mechanics.** Don't bake resolved metadata into `plays` — join at read time or keep a
rebuildable `scenarios` table that `refresh-catalog` recomputes from all sources. **Bucket
analyses by exact `task_id`; map to a name only for display — never group by name** (distinct
variants share a name but not a task_id). **Duplicate `task_id` across sources** must be handled
even though current resources have none: prefer newest season, or record all — merge defensively.

**Validation** (author's account, 122 task_ids): `s1` resolves 55; `s1+s2+s3` resolves 73
(+18 from adding two files, **zero re-fetch**); 49 remain unknown (64 plays; low-count
non-benchmark "…swipe" drills — confirmed **non-practice** (§5.1), just outside the Voltaic
catalog). **task_ids never collide across seasons**
(each season mints fresh ids), so the union is conflict-free today and `task_id` identifies the
season. **Resource surfaces differ:** S1 = "Voltaic Valorant Benchmarks" (`has_leaderboards`
true), S2 = "Voltaic Aimlabs Benchmarks" (`has_leaderboards` **false**), S3 = "Voltaic Aimlabs
Benchmarks" (true).

**Code note:** `voltaic_benchmarks.py` is hardwired to one season (`load_valorant_s1()`);
`scenario_catalog.py` generalizes it to a multi-season union. Keep the season-specific
energy/threshold math **separate** from plain naming.

---

## 10. Analysis & presentation

All analysis is a **pure function of the store: offline, no network, no auth** — and the
`report` command must preserve that invariant (§11). Window sizes are read-time parameters.

### 10.1 Runs table (near-term)

`Date | Scenario | Score | Scenario Stats`, reverse-chronological. "Scenario Stats" = chosen
`performanceScores` columns; the displayed set adapts per scenario (tracking vs clicking drills
expose different keys).

**Default report scope (decided):** governed by `config.report_family`, **default `all`** —
all known Voltaic Aimlabs benchmarks (S1 VALORANT + S2/S3 Aimlabs). Set
`report_family = "valorant"` to restrict to the VALORANT benchmark (S1) only. (Chosen because
the product scope is "Voltaic on Aimlabs" broadly and the S2/S3 resources are loaded; note all
three resources are `is_active`, so "active" and "all" are identical today.) Plays outside the
selected families — including non-Voltaic ones — collapse into a single "other" footer
("+N scenarios, M plays, untracked"), not interleaved.

**Status handling (per review — changed from rev 1):** store all plays, but **stats/PB exclude
non-APPROVED runs by default**, with a visible note ("3 non-APPROVED runs excluded"). A config/
flag option includes all statuses for audit. (This is a *progress* tracker — flagged runs
silently inflating stats would mislead.) Practice runs need no handling here — `mode: 42`
already excludes them (§5.1); the §8.3 contamination check is the backstop.

**Timezone:** display in **system-local by default**, configurable via `timezone`; reports
**label the timezone used**. (Stored values stay UTC, §7.)

### 10.2 Basic rolling stats (near-term)

Per-`task_id`, over APPROVED runs (all mode-42, hence already non-practice — §5.1): PB (max),
mean/median, rolling median (last 10), rolling max (last 25), date range. Grouped by exact
`task_id`.

### 10.3 Trend / cold-score analysis (deferred)

Out of near-term scope, recorded so it isn't done naively later. Score is confounded by
cold-vs-warm and session position; a rolling median over last-N plays mixes cold openers with
warm runs. A real "rising/declining/plateau" signal needs session/cold detection and a stated
threshold (delta-vs-noise or OLS slope with CI) — not a gut read. See §16.

---

## 11. CLI & configuration

**Configuration lives in `config.toml`; the CLI carries only verbs** (a config file describes
state, not "what to do this run"). The POC's ~18 flags collapse to ~4 subcommands + 2 globals.

```
voltmeter sync [--full] [--report] [--session-file PATH]
                            # incremental sync; --full = status reconcile (§8.3, +--show-deleted);
                            # --report prints a report after. Never opens a login window (§4).
voltmeter login [--timeout SECONDS]
                            # the ONLY command that opens the interactive login window -> .env
voltmeter report            # OFFLINE ONLY: runs table + stats from the store. Never auths/networks/logs in.
voltmeter refresh-catalog   # rebuild the scenario projection (§9)
   globals: --config PATH, --verbose
```

**`sync` auth model (review round-5 #3, round-6 #2, round-8 #1):**
- **`sync` never opens a login window (§4).** It resolves the credential from file/env channels
  only; if none is present or managed state is corrupt/the residual terminal auth state is
  reached, it **fails with "run `voltmeter login`"** (no popup). So there's no `--no-login` flag
  and no interactive-desktop detection — unattended is the natural default, and `login` is the
  sole window-opener.
- **No literal credential on the command line (review round-7 #1).** The session cookie is the
  ~30-day "logged in as you" secret; a literal `--session VALUE` would leak it into shell history,
  `ps`/`/proc/PID/cmdline`, and CI logs — contradicting the README and auth doc §8. Inline env
  (`AIMLAB_SESSION=… voltmeter …`) leaks identically. So the cookie comes **only** from file/env:
  managed `data/session.json`, `$AIMLAB_SESSION` (exported via a profile/secrets manager), or
  `.env` (`chmod 600`).
- **`--session-file PATH`:** the sanctioned override — a **path**, never the secret. File
  contract (review round-8 #3): the file holds a **session cookie**, read as the *first non-empty
  line, whitespace-trimmed* — the same value `$AIMLAB_SESSION` would hold (a full cookie string
  containing `session-token` is also accepted, for parity with the POC); it is **not** a
  `KEY=value` line (that's `.env`) and **not** a `Cookie:` header. It can mint a bearer, but it is
  **read-only / not rotation-managed** and must be an independent login from the managed
  `.env`/`data/session.json` chain. On POSIX, **warn (don't fail)** if the file is
  group/world-readable (`mode & 0o077`), mirroring the `chmod 600` `login` sets. The value is
  **never logged** — only the path, plus a "loaded session from PATH" line.
- **No raw-bearer flag for `sync`.** A raw bearer can't re-mint and so can't survive a backfill
  (§8.4), which defeats `sync`'s purpose — so it isn't offered. A one-off raw-bearer path is a
  POC/debug convenience only (auth doc §5), never part of production `sync`.

**Report invariant (per review):** `report` (and `sync --report`'s reporting half) read
**only** from the store — no network, no auth, no login, ever. Only `sync` does I/O.

`config.toml` extends `AppConfig`, following the existing `[section].key → section_key`
flattening convention (`config.py` already maps `[aimlabs].user_id → aimlabs_user_id`). **Target
schema (pin one so implementers don't each choose top-level vs `[aimlabs]` vs `[report]` — review
round-4 #2):**

```toml
[aimlabs]
user_id = "…"                  # -> aimlabs_user_id  (REQUIRED; the stable anthicId == userId)

[storage]
db_path = "data/aimlabs.db"    # -> storage_db_path

[sync]
page_size = 50                 # -> sync_page_size (validated 1..200, default 50; §8.1/§8.2)
request_delay_seconds = 0.25   # -> sync_request_delay_seconds
request_timeout_seconds = 20   # -> sync_request_timeout_seconds

[report]
family = "all"                 # -> report_family ("all" | "valorant"; default "all", §10.1)
timezone = "local"             # -> report_timezone ("local" | IANA name, e.g. "America/Los_Angeles")
rolling_median_window = 10     # -> report_rolling_median_window
rolling_max_window = 25        # -> report_rolling_max_window
```

The secret (`AIMLAB_SESSION`) stays **out** of `config.toml` (§12). **Precedence:** defaults <
`config.toml` < environment (secret only). argparse **subcommands** (stdlib). *(Sectioning is
adjustable — the point is to commit to one layout; this is the proposed default.)*

**Account id: `user_id` (anthicId) required; `username` is not supported (review round-5 #2).**
Storage and `sync_state` are keyed by the **stable `account_id` = anthicId** (§7). A `username`
is *mutable* (a rename would orphan/duplicate rows or repoint state to a different account), so
rather than carry a "resolve username → anthicId before any write" step and an extra API
dependency, the pipeline simply **requires the anthicId** (`[aimlabs].user_id`) — same input the
shipped scores tool already uses. **Never key any row or state by username.** (The
`aimlabProfile` query still accepts `username`, but the pipeline passes only `anthicId`.)

---

## 12. Security model

Detail in auth doc §8; pipeline-relevant points:

- **Local-only.** Session cookie and bearer never leave the machine.
- **Secret ≠ config.** The current managed session chain lives in **`data/session.json`**, seeded
  from the **`AIMLAB_SESSION` env var in a gitignored `.env`** that `login` writes. Identifiers
  live in `config.toml` (`[aimlabs].user_id`). Precedence: `--session-file PATH` >
  `data/session.json` > `$AIMLAB_SESSION` > `.env`. **The secret never appears as a literal CLI
  argument** (§11, review round-7 #1) — only file/env channels. `--session-file` is a read-only,
  non-rotation-managed override and must come from an independent login.
  - **Note (M7a):** both the history commands and `voltmeter scores` resolve auth through these `AIMLAB_SESSION`
    channels by default; the legacy `[aimlabs].session_cookie` config key and `AIMLABS_COOKIE` env
    var the original snapshot tool once accepted were **removed at M6b** (no users depended on
    them pre-release), and its literal-header passthrough was removed at M7a. The
    README/`config.example.toml` therefore describe only the unified file/env scheme.
- **Auth policy** is production-specific — see §4 (managed session canonical for sync; bearer
  debug-only; report never auths; **`sync` never opens a login window** — fails with "run
  `login`" for missing/corrupt/residual terminal auth, so unattended is the default).
- **`.gitignore` — DONE.** The repo `.gitignore` now ignores `.env`, `.env.*`, `*.token`,
  `*.cookie`, `*_history.json`, `data/`, `*.db`, `*.sqlite*`, and `config.toml` (committed with
  the POC). Reviewers: still never commit a real history dump or a populated `.env`.

---

## 13. Testing strategy

Offline, mock-only (matches existing suites; CI cannot reach Aimlabs). The network boundary is
injected into `history_sync`/`aimlabs_history` so the interesting logic tests without a live API.

- **`play_store`:** incremental upsert idempotency (ingest fixture twice → byte-identical);
  **`--full` raw→projection re-derive** — only `gridshield_status` changed → only it + `last_seen_at`
  move; `raw.score` / `raw.performanceScores` changed → projection rebuilt deterministically +
  drift warning (§7.1); schema + `user_version`; `totalCount` drift signal; account scoping.
- **`history_sync`:** a **fake page-fetcher** returning synthetic multi-page responses covers
  pagination, finish-page-then-break, one-page overlap, **resume after interruption**, **new
  plays arriving mid-backfill** (anchor + top sweep), **401 re-mint with persisted session
  rotation**, **residual `RefreshAccessTokenError` → "re-login required" (no retry-loop,
  §4/§8.4)**, **429/5xx backoff**, and
  **cursor-rejection → top restart**. **High-water semantics (§8.1):** a 3-page incremental
  where pages 2–3 are older → `newest_id` stays page-1's top after completion (finalized once, not
  per page); a **`BACKFILLING` crash after the page-2 checkpoint** → resume from `resume_cursor`,
  `newest_id` not advanced; a first backfill + top sweep → `newest_id` is the true top **and
  `api_total_count` is the *sweep's* count (not a mid-backfill value)** only after the sweep
  (§8.2/round-4 #4).
  **`backfill_phase` recovery (§8.2/round-5 #1):** crash after the old-page walk but before the
  sweep → phase persisted as `TOP_SWEEP` → next run runs the sweep (not a mis-resume of old
  pages); crash *during* the sweep → still `TOP_SWEEP` → re-running the sweep is idempotent;
  phase flips to `COMPLETE` only after the sweep finalizes. **Incremental restart-from-top
  (§8.1/round-6 #1):** a crash mid-incremental (phase `COMPLETE`) → next run restarts from the top,
  writes no `resume_cursor`, and `newest_id` is unchanged until safe completion (no orphaned cursor,
  no lost run-top). **Empty stream (§8.1):** empty first page → no rows, `newest_id = NULL`,
  `api_total_count = 0`, `backfill_phase = COMPLETE`, no index error; plus `page_size < 1` is
  rejected by config validation. **Empty-then-nonempty (§8.2/round-8 #4):** a run after an empty
  one that *does* find plays → `newest_id IS NULL` triggers a full initial backfill (`COMPLETE →
  BACKFILLING`, anchor, top sweep), not a bare incremental. **`backfill_phase` CHECK** rejects any
  out-of-enum value (§7). (Live data is single-page, so multi-page behavior has no real-data
  coverage — it *must* be mock-tested.)
- **`scenario_catalog`:** union across small `s1/s2/s3` fixtures; product-surface fields;
  unknown handling; duplicate-`task_id` policy; never-group-by-name.
- **`history_report`:** rolling median/max correctness; per-`task_id` bucketing; "other" footer;
  default exclusion of non-APPROVED; `report_family` scoping (default `all` vs `valorant`);
  **empty store → "no runs found"** renders cleanly (§8.1).
- **`history_sync` contamination check:** mock the aggregate `count(task_mode=42,
  is_practice=true)` → `0` (silent) and `>0` (warns) (§8.3).
- **`aimlabs_auth`:** port the POC's mock tests (session-route exchange, managed-state precedence,
  rotation persistence, corrupt-state policy); **`sync` never opens a window** — missing/corrupt
  or residual terminal credential → exit with "run `voltmeter login`" (no popup, §4);
  **`--session-file`** reads the cookie per the §11 contract and **warns on loose POSIX
  permissions plus non-managed rotation** without logging the value.
- **Fixtures must be synthetic/sanitized** — never commit a real account dump.

---

## 14. Milestones

Built into the package, milestone by milestone; each PR passes the existing
pylint/ruff/mypy/pytest gate. **M0 done** (validated live, §5). **`.gitignore` hardening: done**
(committed with the POC). **§5.1 live-validation: done** — scope finalized (mode 42 ⇒ benchmark,
no practice; no `is_practice` column); only the non-blocking cursor-expiry check remains (M2b).

> **Milestone-complete ≠ user-release-complete (review round-4 #1).** M1–M6 are *development*
> milestones. The pipeline becomes **user-visible** the moment `sync`/`report` ship (M2/M4), but
> its README/`config.example.toml` are owned by the current `aimlab_scores` tool and aren't
> reconciled until M6. **Releasing any pipeline command to users is gated on its docs/config
> matching** — i.e. the M6 reconciliation (or the slice covering the exposed commands) must ship
> *with* that release. Docs travel with the feature that needs them; **M4 is not
> user-release-complete until the M6 doc/config reconciliation lands.**

| # | Milestone | Acceptance criteria |
|---|---|---|
| **M1** | **Store** | `play_store.py` with the §7 schema (`account_id`, `raw` canonical, `first_fetched_at`/`last_seen_at`, `user_version`) at `data/aimlabs.db`; both indexes (incl. `idx_plays_acct_date`); **explicit + tested upsert semantics** (incremental insert-only/byte-identical; `--full` re-derives projection from `raw` + drift warning) per §7.1; **canonical JSON serialization** (sorted-keys/compact) with a key-order-variance fixture proving no false drift (§7.1); synthetic fixtures only. |
| **M2a** | **Core incremental sync** | `aimlabs_history.py` (stateless) + `history_sync.py`: newest→older pagination from the top, finish-page-then-break, one-page overlap (§8.1); **atomic page-ingest**; **high-water captured-at-top, finalized-at-completion (not per page), no `resume_cursor` in steady state** (§8.1/round-6 #1); **empty-first-page path** (no rows, `newest_id`/`api_total_count` set, `page_size ≥ 1` validation, §8.1); `totalCount` drift signal (§8.3); **crash mid-incremental → restart-from-top, idempotent** (no orphaned cursor). Mock-page tests. Second run touches ≤1–2 pages, 0 new rows. |
| **M2b** | **Sync resilience** | First-backfill state machine with the durable **`backfill_phase` enum** (`BACKFILLING`/`TOP_SWEEP`/`COMPLETE`, §8.2): resume + **new-plays-mid-backfill** (anchor + post-backfill top sweep on *every* initial backfill); **crash-before-sweep and crash-during-sweep tests** (phase persisted, sweep re-run idempotent); **401 re-mint (session-cookie auth)**; **429/5xx backoff**; **cursor-*rejection* → top-restart fallback** (distinct from M2a's local-interruption resume). All mock-tested. *(M2 split per design review round 1.)* |
| **M3** | **Scenario catalog** | `scenario_catalog.py`: union of all `resources/aimlabs/*.json` → catalog records incl. **`benchmark_alias`/`benchmark_name`/`family`/`season`/`is_active`/`has_leaderboards`** (§9), not just name/season; rebuildable via `refresh-catalog`; **duplicate-`task_id` policy defined**; unknowns retained + reportable; resolver interface ready for a future API source. |
| **M4** | **Runs table + basic stats** | `history_report.py` + `report` command: reverse-chron table, per-`task_id` PB/median/rolling stats; **offline-only (no auth/network)**; scoped by `report_family` (default `all`) + "other" footer; **non-APPROVED excluded by default with a visible note**; **timezone labeled**; **JSON output** included if a dashboard/test consumer exists, else console-only with JSON fast-follow. |
| **M5** | **(deferred)** trend / cold-score | Not near-term (§10.3, §16). |
| **M6** | **Decommission the POC** | Split into **M6a** (CLI wiring — `sync`/`login`/`refresh-catalog`, unified `aimlab_scores` auth) and **M6b** (this milestone): retire the `proof-of-concepts/` history scripts; reconcile **`README.md` + `config.example.toml`** onto the unified scheme (`[aimlabs].user_id`, `AIMLAB_SESSION`, `report_family` default); relocate the design docs to `docs/`. The legacy `AIMLABS_COOKIE`/`session_cookie` channels were **removed outright** at M6b (no users pre-release), not merely dropped from docs. |
| **M7a** | **Scores entry point** | **Part 1 of [`SCORES_CONSOLIDATION_PROPOSAL.md`](SCORES_CONSOLIDATION_PROPOSAL.md) — the proposal is the spec** (accepted 2026-07-05; supersedes the M6-era "keeps its own entry point" note). `voltmeter scores` subcommand runs the `aimlab_scores` logic via a thin lazy-importing adapter; **byte-for-byte output parity** (tables/JSON, incl. the **no-login path**), captured as a golden output that doubles as M7b's regression baseline; `scores` stays non-interactive (decision 23); `main.py` deleted + dropped from `py-modules`; **live-reference sweep** (ci.yml's 4 mentions, README's `uv run aimlab_scores.py` invocations, `docs/example_output.log` regenerated); offline `report` import-isolation stays green; **`--header` retired** — not mirrored onto `scores` and removed from `aimlab_scores.main` (decision 24: no literal credential on the `voltmeter` command line; the CODING_STANDARDS checklist carve-out line is updated to match — Codex review 2026-07-05). |
| **M7b** | **One scenario catalog** | **Part 2 of the proposal — starts only after M7a lands + review.** `benchmark_constants.py` retired; `scenario_catalog` is the sole metadata layer with **task_id-unique qualified `name`** (no regression of the 2026-06-16 display fix) and **tier thresholds on the record** (+ a unit test pinning a known scenario's thresholds); `scores` derives its short label/base-name slug at its own layer, `--scenario <slug>` unchanged for `valorant_s1`; **tabular data regression-locked** against M7a's golden output; s2/s3 exposure only if the leaderboard endpoint is confirmed to serve them. **Shipped 2026-07-07 (#48):** s2/s3 are **not** exposed — gated on `has_leaderboards` **and** a `SCORES_BENCHMARK_ALIASES = ("valorant_s1",)` allowlist. **`has_leaderboards` alone is not a sufficient gate** (aimlabs_s3 self-reports `has_leaderboards: true`, so it would leak s3's 54 scenarios); before enabling s2/s3, confirm the leaderboard endpoint serves them **and** generalize `voltaic_benchmarks`' rank/energy tables, which still load `valorant_s1` only while all three sources reuse tier ids 2/3/4 (else s3 would score against s1 thresholds). |

Order: **M1 → M2a → M2b → M3 → M4 → M6 → M7a → M7b** (§5.1 validation done). M4 can begin once
M1+M3 exist; M7b strictly after M7a lands and is reviewed.

**Milestones cut across sections** (they are *build* units, not the doc's *section* numbers — one
milestone implements parts of several sections, each its own gate-green PR):

| Milestone | New module(s) | Implements sections | Depends on |
|---|---|---|---|
| **M1** Store | `play_store.py` | §7 schema + §7.1 mutability/serialization | — (foundation) |
| **M2a** Core sync | `aimlabs_history.py`, `history_sync.py`, `aimlabs_auth.py` | §8.1, §8.3, §4 (credential resolution) | M1 |
| **M2b** Resilience | (extends M2a modules) | §8.2 (phase machine), §8.4 (re-mint/backoff) | M2a |
| **M3** Catalog | `scenario_catalog.py` | §9 | M1 (parallel to M2) |
| **M4** Report | `history_report.py`, `cli.py` | §10, §11 | M1 + M3 |
| **M6** Decommission | — | §12/§14 doc reconciliation; retire POC | M1–M4 |
| **M7a** Scores CLI | — (extends `cli.py`; retires `main.py`) | `SCORES_CONSOLIDATION_PROPOSAL.md` Part 1 | M6 |
| **M7b** Catalog unification | — (retires `benchmark_constants.py`) | proposal Part 2; §9 | M7a |

So the critical path is **M1 → M2a → M2b**, with **M3 parallelizable** after M1 and **M4** joining
once M1+M3 land. §13 (testing) and §15 (decisions) are cross-cutting — every milestone adds its
slice of tests. The §1–§3 / §5 / §17 sections are background/context, not build work.

---

## 15. Decisions log

| # | Decision | § |
|---|---|---|
| 1 | **Fetch one unfiltered global stream**, bucket by `task_id` locally. | 5 |
| 2 | **Storage: SQLite**, single `data/aimlabs.db`; **`raw` JSON is canonical**, typed columns derived. | 7 |
| 3 | **`raw` is canonical; typed columns are a pure projection re-derived on every `raw` write.** Incremental = insert-only no-op; `--full` re-derives the projection + warns on drift. | 7.1 |
| 4 | **Timestamps ISO-8601 UTC verbatim**; display-time conversion only; reports label the TZ. | 7, 10 |
| 5 | **Single-account product, account-stamped storage.** | 7 |
| 6 | **Scenario metadata = separate, rebuildable, multi-source projection** carrying product-surface fields; store-all, analyze-Voltaic. | 9 |
| 7 | **Credentials: managed `data/session.json` seeded from `AIMLAB_SESSION` in `.env`; account id `[aimlabs].user_id` in `config.toml`** (userId == anthicId, confirmed). Session canonical for sync; report never auths. The shipped tool's legacy `session_cookie`/`AIMLABS_COOKIE` channels were **removed at M6b** (no users pre-release), so credential resolution flows through the unified session channels (the `aimlab_scores --header` debug passthrough aside — **retired at M7a**, extending decision 24 to `scores`). | 4, 12 |
| 8 | **Analysis simple now**; **non-APPROVED excluded from stats by default**, visible note, override available. | 10 |
| 9 | **CLI = verbs only; config = `config.toml`**; `report` is offline-only. | 11 |
| 10 | **Build into the package**, gate-green per PR; `.gitignore` hardening **done**. | 12, 14 |
| 11 | **History scope = all mode-42 plays = benchmark plays.** Live-verified `mode 42 ⇒ 0 practice` (0/919); no `is_practice` column; cheap aggregate contamination warning as backstop. | 5.1, 8.3 |
| 12 | **M2 split into M2a (core) / M2b (resilience).** | 14 |
| 13 | **`resume_cursor` is `BACKFILLING`-only** (incremental + top-sweep restart-from-top, idempotent); `newest_id`/`api_total_count` are captured at the top and finalized from the freshest top observation, never per-page. | 8.1, 8.2 |
| 14 | **Default report scope `report_family = all`** (all Voltaic Aimlabs S1+S2+S3); `valorant` restricts to S1. | 10.1 |
| 15 | **Auth: successful re-mint persists the rotated session; residual `RefreshAccessTokenError` is "re-login required"** (no retry-loop when the chain is gone/forked/corrupt). | 4, 8.4 |
| 16 | **Empty stream handled** — empty first page ⇒ no rows, `newest_id = NULL`, `api_total_count = 0`, `backfill_phase = COMPLETE`; `page_size` validated ≥ 1. | 8.1 |
| 17 | **Projection JSON serialized canonically** (sorted keys, compact separators) so re-derive is byte-stable — no false drift. | 7.1 |
| 18 | **`config.toml` schema pinned** — `[aimlabs]` / `[storage]` / `[sync]` / `[report]`, flattened `section_key`; one layout, not implementer's choice. | 11 |
| 19 | **`page_size` default 50, bounds 1–200**; cursor loop tolerates server capping (politeness knob, not correctness). | 8.2, 11 |
| 20 | **User-release is gated on doc/config reconciliation** (M6) — a milestone being code-complete ≠ user-release-complete. | 14 |
| 21 | **Durable `backfill_phase` enum** (`BACKFILLING`/`TOP_SWEEP`/`COMPLETE`, guarded by a DB `CHECK`) replaces the `backfill_complete` boolean — crash-before/during-sweep is unambiguous. | 7, 8.2 |
| 22 | **`user_id` (anthicId) required; `username` dropped** — storage/state always keyed by stable anthicId, never the mutable username. | 7, 11 |
| 23 | **`sync` never opens a login window** — missing/corrupt/residual terminal credential → fail "run `voltmeter login`"; `login` is the sole window-opener. No `--no-login`, no auto-login, no interactive detection. | 4, 11 |
| 24 | **`--session-file PATH` is the only credential override** — a path, never a literal secret; read-only and not rotation-managed; bare-cookie file contract; warn on loose POSIX perms and minting-path independence; value never logged. | 11, 12 |
| 25 | **Initial backfill triggers off `newest_id IS NULL`** (not the phase) — unifies new-account and empty-then-nonempty; both get the full backfill + top sweep. | 8.2 |

---

## 16. Open questions for reviewers

Most questions are now resolved (timezone → system-local + labeled; non-APPROVED → excluded by
default; module split → keep 7; report format → JSON in M4 iff a consumer exists; **history
scope/practice → resolved by §5.1 live probe: mode 42 excludes practice**). Remaining:

1. **Energy/rank over history** — out of scope now; layering it later should compute from stored
   plays + threshold resources, not PB snapshots. Near-future priority or genuinely later?
2. **Deletion handling** — passive drift signal accepted for v1; precise reconciliation is an
   opt-in `--full --show-deleted`. Confirm that's the right floor.
3. **Cursor expiry** — the one unvalidated §5.1 item; non-blocking (M2b cursor-rejection
   fallback covers it) but unconfirmed. OK to leave for M2b testing?

---

## 17. Appendix: validation findings

Captured against the author's live account on **2026-06-06** (N = 919) via the POC spike.
Evidence for the design; not universal facts.

- Unfiltered `plays` stream works with auth; **919 plays, reverse-chronological**, `task.id` on
  every node, all `APPROVED`. (§5)
- **122 distinct task_ids**; catalog coverage 55 (s1) → 73 (s1+s2+s3); 49 unknown. (§9)
- **`task_id → weapon_id` 1:1**, `task_mode` always 42 across 151 catalog scenarios. (§7, §9)
- **Resource surfaces differ:** S1 "Voltaic Valorant Benchmarks"; S2/S3 "Voltaic Aimlabs
  Benchmarks"; `has_leaderboards` varies (S2 = false). (§9)
- **Practice (§5.1, resolved):** the history `Play` node exposes no `is_practice` /
  `inputDevice` field and `PlayFilterInput` has no practice filter (validation errors). But the
  aggregate (`AimlabPlayWhere`, mode field `task_mode`) shows **`task_mode=42 & is_practice=true`
  = 0**, `… = false` = **919** (matches history `totalCount`). Account-wide census: **1141 total
  plays, 1137 non-practice, 4 practice — all 4 outside mode 42.** So mode 42 ⇒ no practice. The
  "swipe" drills are non-practice (just non-Voltaic).
- **Second endpoint:** `aimlab.plays_agg(where: AimlabPlayWhere)` gives server-side
  `count/avg/max` filtered by `user_id`/`task_id`/`task_mode`/`is_practice` — used for the
  contamination check (§5.2). Its `max{}` 500s on an empty set; use `count`-only there.
- **Auth rotation (live):** `/api/auth/session` returns a rotated session cookie with successful
  bearer mints. Reusing a stale pre-rotation cookie can produce
  `accessTokenError: RefreshAccessTokenError` with `expires` still a month out. Persisting the
  rotated link makes normal 401 re-mint recoverable; re-`login` remains the fallback when the
  chain is already broken (§4, §8.4).
- **Operational:** server returned all 919 in one page when `first` omitted (scale artifact —
  page explicitly); **`first: 0` → HTTP 500** (use `first ≥ 1`); **introspection disabled**.
- `userId == anthicId` confirmed equal. (§2, §12)
