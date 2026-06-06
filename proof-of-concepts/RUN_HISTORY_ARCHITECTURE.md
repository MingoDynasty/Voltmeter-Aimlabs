# Voltmeter-Aimlabs — Run-History Pipeline: Design

**Status:** Draft for review
**Date:** 2026-06-06
**Author:** MingoDynasty
**Audience:** senior engineers; one will likely implement this.
**Scope:** pulling, storing, and analyzing a user's full Aimlabs play history. Sibling to
[`ARCHITECTURE.md`](ARCHITECTURE.md), which covers **authentication** and is a dependency
of this design (summarized in §4).

> **Reviewers:** the decisions in §15 and the open questions in §16 are where your input is
> most valuable. The validation numbers throughout come from the author's own account
> (N=919 plays, captured 2026-06-06) and are evidence, not universal facts — see §17.

---

## 1. Summary

Voltmeter-Aimlabs is an unofficial progress tracker for **Voltaic** aim-training benchmarks
played on **Aimlabs**. Unlike KovaaKs (which writes per-play stats to local files), Aimlabs
stores all play data server-side, reachable only through its GraphQL API behind a login.

This design adds a **run-history pipeline**: it syncs a user's entire play history into a
local SQLite store (incrementally, cheap on repeat runs), maps each play to its Voltaic
scenario, and renders a reverse-chronological runs table plus basic per-scenario stats. It
is the data layer beneath a future evxl-style rank-progress tracker.

The core architectural bet (validated live, §5): fetch the **entire account history as one
unfiltered, reverse-chronological stream** and bucket by scenario locally — rather than
querying scenario-by-scenario.

---

## 2. Background & glossary

Domain primer for reviewers new to aim trainers:

- **Aimlabs** — an aim-training game. Exposes a GraphQL API at `api.aimlab.gg`.
- **Voltaic** — an organization that publishes standardized **benchmarks** (curated sets of
  scenarios with score thresholds that map to skill **ranks**). Voltaic benchmarks exist for
  both KovaaKs and Aimlabs. This project targets the **Voltaic × Aimlabs** benchmarks.
- **Scenario / task** — one drill (e.g. "Adjustshot"). Identified by a stable string
  **`task_id`** like `CsLevel.Lowgravity56.VT Adjus.RTUQMP`.
- **Play / run** — one attempt at a scenario, yielding a **score** and detailed
  **`performanceScores`** (accuracy, hits, shots, etc.). Each play has a stable UUID.
- **PB** — personal best (max score) on a scenario.
- **anthicId** — Aimlabs' stable per-account identifier (semi-public; appears in anonymous
  leaderboard queries). The account key throughout.
- **gridshield** — Aimlabs' anti-cheat verdict on a play (`APPROVED`, or flagged).
- **`taskMode`** — a play-mode discriminator. Voltaic benchmark plays are always mode `42`.
- **Relay cursor** — the GraphQL pagination style the `plays` connection uses
  (`pageInfo.hasNextPage` + `endCursor` → next page's `after`).
- **evxl** ([evxl.app](https://evxl.app)) — an existing rank-progress tracker for *KovaaKs*.
  Nothing comparable exists for Aimlabs; this project aims to fill that gap.
- **Season** — Voltaic revises its benchmark set periodically (S1, S2, S3…). Each season is
  a separate resource file; **task_ids are minted fresh each season** (§9).

---

## 3. Goals & non-goals

**Goals**
- Sync a single account's complete Aimlabs play history into a local store, incrementally.
- Survive large first-time backfills (resumable; auth-refresh-aware).
- Map plays to Voltaic scenarios via a metadata catalog that improves over time without
  re-fetching plays.
- Render a reverse-chronological runs table (`Date | Scenario | Score | Stats`) and basic
  per-scenario rolling stats, computed offline from the store.

**Non-goals (now)**
- Trend classification / cold-score analysis (the hard, easy-to-get-wrong part — deferred,
  §10.3).
- Voltaic energy/rank computation over history (the repo already does this for PBs; layering
  it onto history is future work, §16).
- Multi-account *features* (the schema is multi-account-safe, but there's no account-switching
  UX — §7).
- A hosted/multi-user service (explicitly out of scope; see auth doc §8 for why).
- Non-Voltaic scenario support as a first-class product surface (kept in storage, parked in
  an "other" bucket — §9).
- A packaged/installable distribution (remains source-checkout tooling; §7).

---

## 4. Dependency: authentication (summary)

Full detail in [`ARCHITECTURE.md`](ARCHITECTURE.md). What this pipeline relies on:

- The target query (`aimlabProfile.plays`) is **account-scoped and requires auth**; called
  anonymously it returns `UNAUTHENTICATED`.
- The durable credential is a **NextAuth session cookie** from `aimlabs.com` (~30-day life),
  stored locally. It is exchanged on demand for a short-lived (~1 h) **bearer token** via
  `GET aimlabs.com/api/auth/session`, which is then sent to `api.aimlab.gg/graphql`.
- A `login` command captures the session cookie via an embedded browser (handles MFA/captcha
  natively). The credential **never leaves the user's machine**.

This design assumes a function `get_bearer() -> str` that returns a fresh bearer (minting a
new one from the session cookie when needed), and surfaces one new requirement on it:
**re-mint mid-run on a 401** during long backfills (§8.4).

---

## 5. Key insight: one unfiltered global stream

A naive design queries history **per scenario** (send a `taskId` filter, paginate, repeat
for every scenario). That is the wrong model here:

- You'd need the full list of task_ids up front — but you don't have it. The benchmark
  resource knows ~55 scenarios; a real account's history spans **far more** (the author's has
  122 distinct task_ids — practice drills, retired/seasonal variants). A per-scenario sweep
  silently misses anything not in your list.
- It means N pagination cursors and N high-water marks to track for incremental sync.

The `aimlabProfile.plays` connection accepts **no `taskId` filter**, returning **every play
across every scenario** as one Relay-paginated, **reverse-chronological** stream, with
`task { id }` on each node. So the model is: **paginate one stream, bucket by `node.task.id`
locally.** One cursor, one high-water mark, and scenario discovery comes from the data.

**Validated live** (author's account, 2026-06-06): the unfiltered query returned all **919
plays** in reverse-chronological order, every node carrying `task.id`, spanning **122 distinct
task_ids**, all `gridshieldStatus = APPROVED`. The query sends `filter: { mode: 42 }` (Voltaic
benchmark mode) and selects `task { id }` per node.

> ⚠️ **Do not rely on single-page responses.** At the author's scale the server returned all
> 919 in one page (no `first` supplied). That is a default cap that happens to exceed 919, not
> a contract. The implementation **must** send an explicit page size and follow the cursor
> (§8). Larger accounts will paginate.

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

Built **into the package** (not left in `proof-of-concepts/`), reusing existing modules.
Proposed new modules and responsibilities:

| Module | Responsibility | Depends on | Network? |
|---|---|---|---|
| `aimlabs_auth.py` | session→bearer exchange, `login` capture, credential resolution, **401 re-mint** | `config` | yes |
| `aimlabs_history.py` | build the unfiltered payload, paginate the Relay cursor, parse nodes → play dicts | `aimlabs_client` (HTTP) | yes (via injected client) |
| `play_store.py` | SQLite schema + migrations, `upsert_plays`, `sync_state` get/set, queries, `totalCount` drift check | stdlib `sqlite3` | no |
| `history_sync.py` | orchestrates the incremental early-break loop: resumable cursor, 401 re-mint, deletion-drift warning | `aimlabs_auth`, `aimlabs_history`, `play_store` | yes (boundary injected) |
| `scenario_catalog.py` | union season resources → `task_id → {name, category, sub, difficulty, season}`; rebuildable; resolver interface for a future API source | `voltaic_benchmarks` resources | no |
| `history_report.py` | offline runs table + basic rolling stats from store + catalog | `play_store`, `scenario_catalog` | no |
| `cli.py` (or extend `main.py`) | argparse subcommands: `sync` / `login` / `report` / `refresh-catalog` | all of the above | — |

**Reused as-is:** `aimlabs_client.py` (HTTP/headers/endpoint), `voltaic_benchmarks.py` +
`benchmark_constants.py` (catalog source, future energy math), `config.py` (extended, §11).
New modules must be added to `[tool.setuptools] py-modules` in `pyproject.toml` or packaging
breaks.

**Single-writer assumption:** the tool is invoked one process at a time. We rely on SQLite's
file locking for safety but do not design for concurrent invocations (acceptable for a local
single-user tool; stated so reviewers don't expect more).

---

## 7. Data model & storage

**Engine: SQLite, single file.** Stdlib, zero deps, one portable file fitting the local-only
model. Scales to millions of rows with the index below (a heavy multi-year account is
~50–100k plays — trivial). The alternative (JSONL) degrades because every analysis re-parses
the whole file. A DB server (Postgres) is rejected — local-only by design.

**Location:** `data/aimlabs.db` (project-relative, gitignored), path overridable in
`config.toml`. This matches the source-checkout reality. (If packaging ever happens — a
distant maybe — migrate to an OS user-data dir via `platformdirs`.)

**Account scoping:** single-account *product*, account-stamped *storage*. We expect one
account per user and offer no account-switching UX, but `account_id` (the stable `anthicId`)
is on every row from day one — it costs one column, is needed internally anyway (high-water
mark and the drift check are per-account), and averts a painful migration later.

**Timestamps:** store the API's ISO-8601 UTC string **verbatim** (e.g.
`2026-06-06T02:43:54.249Z`). That single choice is simultaneously "as-is from the API,"
"ISO," and an unambiguous UTC instant; it sorts lexicographically (the high-water-mark check
and the `ended_at DESC` index rely on this) and stays readable. **Never store local/naive
time.** Timezone is a display/grouping concern only (§10).

**Fidelity:** keep `performance_scores` as a raw JSON blob so a future metric needs no
re-fetch. `mode` and `weapon_id` are intentionally **not** stored — every Voltaic scenario is
`task_mode` 42 and `task_id → weapon_id` is strictly 1:1 in the catalog, so both are
derivable from `task_id`.

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
  gridshield_status  TEXT,              -- APPROVED / flagged / ...
  performance_scores TEXT,              -- raw JSON, verbatim
  raw                TEXT,              -- optional: full node JSON, future-proofing
  fetched_at         TEXT NOT NULL,     -- ISO-8601 UTC; when we ingested it
  PRIMARY KEY (account_id, id)
);
CREATE INDEX idx_plays_acct_task_date ON plays(account_id, task_id, ended_at DESC);

CREATE TABLE sync_state (
  account_id         TEXT PRIMARY KEY,  -- stable anthicId
  resume_cursor      TEXT,              -- Relay endCursor of last page written (resumability)
  backfill_complete  INTEGER NOT NULL,  -- 0 until the stream has been fully walked once
  newest_id          TEXT,              -- high-water mark for incremental sync
  newest_ended_at    TEXT,
  api_total_count    INTEGER,           -- last totalCount seen; drives drift warning (§8.3)
  updated_at         TEXT NOT NULL
);
```

The scenario catalog is a separate, rebuildable projection (§9) — it may be a `scenarios`
table or computed at read time; either way plays never depend on it existing.

---

## 8. Sync algorithm

### 8.1 Incremental early-break

Reverse-chronological stream + immutable play `id` ⇒ fetch newest-first, stop once we reach
already-stored data:

```
high_water_id = sync_state.newest_id      # None on first run
seen_known = False
for each page (newest → older, following endCursor):
    for node in page:
        upsert(node)                      # idempotent; re-seeing a play is a no-op
        if node.id == high_water_id:
            seen_known = True
    if seen_known: break                  # finish the page first, THEN stop
    if not pageInfo.hasNextPage: break
update sync_state (newest_id, newest_ended_at, api_total_count, backfill_complete)
```

- **Finish the page before breaking** — a page can mix new + cached; breaking on the first
  cached id mid-page would skip newer plays after it.
- **Upsert by `(account_id, id)`** — re-seeing a play is a no-op, so a one-page overlap is
  free insurance and re-runs are always safe (cron-friendly).
- **Cost is O(new plays)** — a daily sync walks one or two pages and stops.

### 8.2 Resumable backfill

The first backfill of a large account is the only expensive operation (~2,000 pages for
~100k plays). Checkpoint `resume_cursor` after each written page and set `backfill_complete`
only when the stream ends. An interrupted first backfill **resumes** from the last page
instead of restarting.

### 8.3 Deletion & status drift

Per-play *values* are immutable facts — trustworthy forever. Only two things can drift:

- **Status** (`gridshield_status` flips) — refreshed by an occasional `sync --full`, which
  walks the whole stream and upserts.
- **Membership** (a play deleted upstream — not expected from Aimlabs) — **walk-and-upsert
  does NOT catch this** (a vanished play simply stops appearing; upsert never revisits it).
  Instead, detect cheaply via **`totalCount` drift**: the API returns `totalCount` per sync,
  so compare it to the stored count — `stored > totalCount` ⇒ **warn** ("N local plays no
  longer upstream"). Identifying *which* plays vanished needs a full id set-diff; relegate
  that to an opt-in `sync --full --show-deleted`. A passive warning is the right
  effort:value given deletions aren't expected.

### 8.4 Auth refresh & late writes

- **Bearer re-mint on 401:** a long backfill can exceed the ~1 h bearer lifetime. On a 401
  mid-pagination, re-mint from the session cookie (§4) and continue from `resume_cursor`.
  Daily syncs never hit this.
- **Late/out-of-order writes:** the algorithm assumes history is append-only and ordering is
  stable. If ever violated (server backfill, clock skew), `sync --full` is the backstop.
- **Rate limiting:** add exponential backoff on HTTP 429 before the large-backfill path
  matters (§16).

---

## 9. Scenario catalog (metadata projection)

**Principle:** the play store is the source of truth keyed by the stable `task_id`. Mapping
`task_id → {name, category, sub, difficulty, season}` is a **separate projection that
improves over time** and must **never require re-fetching plays**. Ingest stores `task_id`
regardless of whether it can be named yet; "fill in the blanks later" is the normal flow.

**Sources** (resolved as a union, behind one interface):
- **Per-season resource files** `resources/aimlabs/*.json` (`valorant_s1`, `aimlabs_s2`,
  `aimlabs_s3`, future seasons). Identical schema.
- **A future Aimlabs task-info API** (query metadata by task_id) — would slot in as another
  source, with a local cache. This is the enabler for non-Voltaic naming (§ scope below).

**Scope (Voltaic-first).** The catalog *is* the Voltaic boundary: a resolved `task_id` is a
tracked Voltaic scenario (the product surface); an unresolved one is a not-yet-loaded season
or a non-Voltaic scenario, parked in a secondary "other / unclassified" bucket — **kept, not
dropped** (storage ingests everything; re-fetching dropped plays later is impractical).

**Mechanics.** Don't bake resolved name/difficulty into `plays` — join at read time or keep a
rebuildable `scenarios` table that `refresh-catalog` recomputes from all current sources.
**Bucket analyses by exact `task_id`; map to a name only for display — never group by name**
(distinct variants share a name but not a task_id).

**Validation** (author's account, 122 distinct task_ids):

| Catalog sources | resolved |
|---|---|
| `s1` only | 55 / 122 |
| `s1 + s2 + s3` | **73 / 122** (+18 from adding two files — **zero re-fetch**) |
| still unknown | 49 (64 plays; low-count practice "…swipe" drills) |

Two findings that shape the implementation: **task_ids never collide across seasons** (each
season mints fresh ids), so the union needs no conflict resolution and `task_id` alone
identifies the season; and **`task_id → weapon_id` is 1:1** with `task_mode` always 42 across
all 151 catalog scenarios, so neither needs storing (§7).

**Code note:** `voltaic_benchmarks.py` is currently hardwired to one season
(`RESOURCE_PATH = …valorant_s1.json`, `load_valorant_s1()`); `scenario_catalog.py` generalizes
it to a multi-season union. Keep the season-specific **energy/threshold math separate** from
plain naming — the pipeline only needs name/difficulty.

---

## 10. Analysis & presentation

All analysis is a pure function of the store: offline, no network, no auth. Window sizes are
read-time parameters (tunable without re-fetch).

### 10.1 Runs table (near-term)

`Date | Scenario | Score | Scenario Stats`, reverse-chronological. "Scenario Stats" = chosen
`performanceScores` columns (`accTotal`, `hitsTotal`, `shotsTotal`, …); the displayed set
adapts per scenario since tracking vs clicking drills expose different keys. **Voltaic-scoped
by default** — features catalog-resolved scenarios; non-Voltaic plays collapse into one
"other" footer (e.g. "+49 scenarios, 64 plays, untracked"), not interleaved.

### 10.2 Basic rolling stats (near-term)

Per-`task_id`: PB (max), mean/median, rolling median (last 10), rolling max (last 25), date
range. Grouped by exact `task_id`.

### 10.3 Trend / cold-score analysis (deferred)

Explicitly out of near-term scope, recorded so it isn't done naively later. Score is
confounded by cold-vs-warm and session position; a rolling median over last-N *plays* mixes
cold openers with warm runs. A real "rising/declining/plateau" signal needs session/cold
detection and a stated threshold (delta-vs-noise or OLS slope with a confidence interval) —
not a gut read. See §16.

---

## 11. CLI & configuration

**Configuration lives in `config.toml`; the CLI carries only verbs.** A config file describes
state, not "what to do this run," so a small CLI is required (editing config between runs is
not scriptable). The current POC's ~18 flags collapse to ~4 subcommands + 2 globals.

```
voltmeter sync              # incremental sync + report — the 90% path
voltmeter sync --full       # status-refresh reconcile (§8.3); --show-deleted for id diff
voltmeter login             # capture session cookie -> .env
voltmeter report            # offline: runs table + stats from the store, no network
voltmeter refresh-catalog   # rebuild the scenario projection (§9)
   globals: --config PATH, --verbose
```

`config.toml` keys (extend `AppConfig`): `anthic_id`, optional `username`, `db_path`,
`page_size`, `request_delay`, `request_timeout`, `timezone`, rolling-window sizes.
**Precedence:** defaults < `config.toml` < environment (secret only). No per-knob flag
overrides unless a concrete need appears. argparse **subcommands** (stdlib, no new dep).

---

## 12. Security model

Detail in auth doc §8; pipeline-relevant points:

- **Local-only.** Session cookie and bearer never leave the machine.
- **Secret ≠ config.** The session cookie's canonical home is the **`AIMLAB_SESSION` env
  var, seeded from a gitignored `.env`** (`login` writes it and `chmod 600`s it on POSIX).
  Non-secret identifiers live in `config.toml`. Resolution precedence:
  `--session` > `$AIMLAB_SESSION` > `.env` > `config.toml` legacy `session_cookie`. The
  README's `AIMLABS_COOKIE` is unused — drop it.
- **`.gitignore` (action required, first commit).** The repo-root `.gitignore` currently
  ignores only `config.toml` + caches — **not** `.env`, `*.token`, `*.cookie`, or the SQLite
  file. They are safe today only because `proof-of-concepts/` is untracked wholesale. Before
  any tracked code lands, add those patterns + `data/`.

---

## 13. Testing strategy

Offline, mock-only (matches the repo's existing suites; CI cannot reach Aimlabs). The network
boundary is injected into `history_sync`/`aimlabs_history` so the interesting logic is
testable without a live API.

- **`play_store`:** upsert idempotency (ingest a fixture twice → byte-identical store); schema
  creation + `user_version`; `totalCount`-drift detection; account scoping.
- **`history_sync`:** drive a **fake page-fetcher** returning synthetic multi-page responses
  to cover pagination, finish-page-then-break, one-page overlap, **cursor resume after a
  simulated interruption**, and the **401 re-mint** path. (Critical: the author's live data is
  single-page, so multi-page behavior has no real-data coverage and must be mock-tested.)
- **`scenario_catalog`:** union across small `s1/s2/s3` fixtures; unknown handling; season
  uniqueness; never-group-by-name.
- **`history_report`:** rolling median/max correctness on a known fixture; per-`task_id`
  bucketing; "other" footer.
- **`aimlabs_auth`:** port the POC's mock tests (session-route exchange, `.env` precedence).
- **Fixtures must be synthetic/sanitized** — do **not** commit a real account dump (it's
  personal data and gitignored). Hand-author small fixtures.

---

## 14. Milestones

Built into the package, milestone by milestone; each is its own PR that passes the existing
pylint/ruff/mypy/pytest gate (the POC won't — porting means rewrite-to-standard, not
copy-paste). **M0 is already done** (validated live, §5, §17).

| # | Milestone | Acceptance criteria |
|---|---|---|
| **M1** | **Store** | `.gitignore` secrets + `data/` (first commit). `play_store.py` with the §7 schema (`account_id`, `sync_state`, `user_version`) at `data/aimlabs.db`; idempotent `upsert_plays`; raw `performance_scores` preserved. Unit-tested via a synthetic fixture. |
| **M2** | **Incremental sync** | `aimlabs_history.py` + `history_sync.py`: newest→older pagination, finish-page-then-break, one-page overlap (§8.1); resumable cursor (§8.2); `totalCount` drift warning (§8.3); 401 re-mint (§8.4). Network boundary injected; mock-page tests per §13. First run backfills all; second touches ≤1–2 pages, 0 new rows. |
| **M3** | **Scenario catalog** | `scenario_catalog.py`: union of all `resources/aimlabs/*.json` → `task_id → {name, category, sub, difficulty, season}`, rebuildable via `refresh-catalog`; unknowns labeled not dropped; resolver interface ready for a future API source (§9). |
| **M4** | **Runs table + basic rolling stats** | `history_report.py` + `report` command: reverse-chron table and per-`task_id` PB/median/rolling-median(10)/rolling-max(25), offline; Voltaic-scoped with "other" footer; timezone + non-APPROVED handling resolved per §16. |
| **M5** | **(deferred)** trend / cold-score analysis | Not on the near-term path (§10.3, §16). |
| **M6** | **Decommission the POC** | Once M1–M4 ship, retire `proof-of-concepts/` history scripts; auth/config unified through `aimlabs_auth`/`config`. |

Order: **M1 → M2 → M3 → M4 → M6**. M4 can begin once M1+M3 exist (analyze a fixture before
M2's live sync lands).

---

## 15. Decisions log

| # | Decision | § |
|---|---|---|
| 1 | **Fetch one unfiltered global stream**, bucket by `task_id` locally (not per-scenario). | 5 |
| 2 | **Storage: SQLite**, single `data/aimlabs.db`. | 7 |
| 3 | **Immutable-once-written**; `--full` refreshes status; deletions surfaced by cheap `totalCount`-drift warning, not active reconcile. | 8.3 |
| 4 | **Timestamps: ISO-8601 UTC verbatim**; convert only at display. | 7 |
| 5 | **Single-account product, account-stamped storage** (`account_id` column). | 7 |
| 6 | **Scenario metadata is a separate, rebuildable, multi-source projection**; store-all, analyze-Voltaic. | 9 |
| 7 | **Credentials: secret in gitignored `.env` (`AIMLAB_SESSION`)**, identifiers in `config.toml`; drop `AIMLABS_COOKIE`. | 12 |
| 8 | **Analysis kept simple now** (table + basic rolling stats); trend classifier deferred. | 10 |
| 9 | **CLI = verbs only; config = `config.toml`**; ~4 subcommands + 2 globals; delete POC debug flags. | 11 |
| 10 | **Build into the package**, milestone by milestone, gate-green per PR; secrets `.gitignore`d first. | 6, 14 |

---

## 16. Open questions for reviewers

1. **Timezone default** — propose: configurable `timezone`, defaulting to system-local for
   display. Acceptable, or prefer always-UTC display?
2. **Non-APPROVED plays** — propose: store all; default analysis *includes* all (sample is
   100% APPROVED so immaterial today) with `gridshield_status` available for a future filter.
   Should non-APPROVED be excluded from stats by default instead?
3. **`report` output format** — propose: console table for M4, with `--format json|csv` as a
   fast-follow. Is console-only acceptable to start?
4. **Module split** (§6) — is the seven-module decomposition right, or should some merge
   (e.g. fold `aimlabs_history` into `history_sync`)?
5. **Energy/rank over history** — out of scope now (§3). The repo already computes Voltaic
   energy for PBs; is layering it onto history a near-future priority or genuinely later?
6. **Deletion handling** — is the passive `totalCount`-drift warning (vs. active
   reconcile/delete) the right call given deletions aren't expected?

---

## 17. Appendix: validation findings

Captured against the author's live Aimlabs account on **2026-06-06** (N = 919 plays) using a
POC spike (`proof-of-concepts/`). Evidence for the design choices above; not universal facts.

- Unfiltered `plays` stream works with auth; **919 plays, reverse-chronological**, `task.id`
  on every node, all `APPROVED`. (§5)
- **122 distinct task_ids**; catalog coverage 55 (s1) → 73 (s1+s2+s3); 49 unknown. (§9)
- **`task_id → weapon_id` 1:1**, `task_mode` always 42 across 151 catalog scenarios. (§7, §9)
- Server returned all 919 in **one page** when `first` was omitted — a scale artifact, not a
  contract; explicit paging is required. (§5)
- Per-`task_id` rolling-median(10) sat above lifetime median on most scenarios — a *plausible*
  improvement hint, but the naive confounded metric; not relied upon. (§10.3)
