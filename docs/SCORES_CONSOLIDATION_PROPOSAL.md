# Proposal: Fold `aimlab_scores` under `voltmeter` and unify the scenario-metadata layer

**Status:** Proposal — authored by **Claude Code**, 2026-06-16. Not yet scheduled; intended to
become milestones in `RUN_HISTORY_ARCHITECTURE.md` §14 once accepted.
**Scope:** two parts, sequenced. **Part 1** (entry-point unification) is low-risk CLI plumbing
and stands alone. **Part 2** (metadata unification) is a larger, riskier refactor that should
only be picked up after Part 1 lands and is reviewed.
**Workflow:** the usual one-PR-per-part off `main` (Codex implements · Claude reviews · user
merges).

---

## TL;DR

Today there are **two CLIs** and **two parallel scenario-metadata layers** that both project the
same upstream Voltaic resource files:

| | `voltmeter` (run-history pipeline) | `aimlab_scores.py` (PB/leaderboard puller) |
| --- | --- | --- |
| Entry point | `voltmeter = cli:main` (console script) | `python main.py` / `python aimlab_scores.py` (**not** a console script) |
| What it pulls | full **per-play history** → local SQLite store | per-scenario **leaderboard PB** + Voltaic rank/energy (live) |
| Endpoint | authenticated `aimlabProfile.plays` | `aimlab.leaderboard` |
| Auth | **requires** a login session (bearer minted from `AIMLAB_SESSION`); hard-fails with "run `voltmeter login`" | **works without login** (public leaderboard read); attaches a cookie only if one is configured, else degrades |
| Scenario metadata | `scenario_catalog.py` — keyed by **task_id**, unions **all** `resources/aimlabs/*.json` | `benchmark_constants.py` — keyed by **slug**, loads **only** `valorant_s1` |

**Part 1 — one front door.** Add a `voltmeter scores` subcommand that runs the existing
`aimlab_scores` logic, and retire the `main.py` compatibility wrapper. The auth difference is a
per-command property, **not** a reason to keep a separate binary. (Reaffirm decision 23: `scores`
stays non-interactive too — it never opens a login window; it just runs without one.)

**Part 2 — one catalog.** Collapse `benchmark_constants.py` into `scenario_catalog.py` so there
is a single source of scenario metadata. This removes a duplicated `_display_name`, a divergent
keying scheme (slug vs task_id), and a divergent resource-loading scope (s1-only vs all-sources).

---

## Why this is worth doing

- **One thing to learn and one thing to wire.** Users run `voltmeter <verb>`; there is no second
  entry point with its own flags and config handling.
- **The duplication is real and already drifting.** Both metadata layers carry their own
  `_display_name` with the same difficulty-suffix logic; the run-history report bug fixed on
  2026-06-16 (difficulties collapsing to one label) lived in `scenario_catalog`'s copy — the
  `benchmark_constants` copy has the identical shape and would need the same care. Two copies of
  the same projection invite exactly this kind of split-brain.
- **It unlocks the history-side insights (item 4 / design M5).** The most-wanted insight —
  "where does my PB sit against the Voltaic benchmark thresholds, and how far to the next rank?"
  — needs the tier thresholds + the energy/rank math that today live only on the
  `aimlab_scores`/`voltaic_benchmarks` side. A unified catalog that carries thresholds is the
  bridge that lets the **offline report** compute rank/energy from stored history without a
  second metadata system.

---

## Part 1 — unify the entry point (small)

**Goal:** `aimlab_scores` is reachable as `voltmeter scores`; `main.py` is gone.

- Add a `scores` subparser to `cli.py` mirroring `aimlab_scores.main`'s arguments (`--difficulty`,
  `--scenario`, `--json`, `--raw`, `--out`, `--source`, `--timeout`, `--request-delay`,
  `--run-deadline`, `--header`, `--user-id`). Keep `aimlab_scores.py`'s functions as the
  implementation; the subcommand handler is a thin adapter (mirrors how `sync`/`login` lazy-import
  their heavy deps so the offline `report` path stays clean — §10/§11).
- Keep `--config`/`--verbose` honored in both positions (consistent with the 2026-06-16 fix).
- Delete `main.py` and drop it from `[tool.setuptools] py-modules`; update `README.md` /
  `config.example.toml` references from `python main.py …` to `voltmeter scores …`.
- **Non-goal:** merging the *fetch logic* of the two tools. Live-PB and historical-play are
  different jobs against different endpoints; they stay separate code paths under one CLI.

**Acceptance**
- `voltmeter scores [...]` reproduces today's `python main.py [...]` output byte-for-byte (same
  tables / JSON), including the no-login path.
- `voltmeter --help` lists `scores`; `main.py` no longer exists and nothing imports it.
- Offline `report` path still imports no network/auth modules (existing §10/§11 test still green).
- Docs/config no longer reference `main.py`.

## Part 2 — unify the scenario-metadata layer (larger)

**Goal:** one catalog module; `benchmark_constants.py` is retired.

The two layers differ in three load-bearing ways that the refactor must reconcile:

1. **Keying.** `benchmark_constants` is keyed by a derived **slug** (`get_scenarios`,
   `--scenario <key>`); `scenario_catalog` is keyed by **task_id**. The `scores` command selects
   scenarios by slug today — a unified catalog needs a stable slug/lookup that survives, or the
   `--scenario` contract changes (call out in review).
2. **Resource scope.** `benchmark_constants` loads **only** `valorant_s1`; `scenario_catalog`
   unions **all** sources (s1 + s2 + s3) with a duplicate-task_id policy. Unifying means `scores`
   would gain access to s2/s3 — desirable, but confirm the leaderboard endpoint accepts those
   task/weapon ids before exposing them.
3. **Fields.** The leaderboard fetch needs `task_id` + `weapon_id` + `task_mode` (all already on
   `ScenarioCatalogRecord` except `task_mode`, which defaults to 42). The rank/energy math needs
   the **tier thresholds**, which `ScenarioCatalogRecord` does **not** carry yet — add them
   (per-tier threshold lists) so both the `scores` command and a future history-side rank readout
   can consume them from one place.

**Acceptance**
- `benchmark_constants.py` is deleted; `voltaic_benchmarks` (the shared primitive) and
  `scenario_catalog` are the only metadata modules; `scores` and the pipeline read the same
  catalog.
- `ScenarioCatalogRecord` exposes tier thresholds; a unit test pins a known scenario's thresholds.
- `voltmeter scores` output is unchanged for `valorant_s1` scenarios (regression-locked against
  Part 1's golden output); any newly-exposed s2/s3 scenarios are an explicit, reviewed addition.
- Single `_display_name`; the difficulty/season qualifier is retained (no regression of the
  2026-06-16 fix).

---

## Open questions for review

- **`--scenario` selector after unification:** keep the slug, switch to task_id, or accept both?
  (Affects anyone scripting `scores --scenario …`.)
- **Expose s2/s3 to `scores`?** Confirm the leaderboard endpoint serves them before turning them
  on; if not, gate `scores` to `has_leaderboards` records.
- **Where thresholds live:** on `ScenarioCatalogRecord` directly vs a side table keyed by
  (task_id, tier). Direct is simpler; a side table avoids widening the already-large record.
- **Milestone numbering:** slot Part 1 / Part 2 into design §14 (e.g. M7a / M7b) when accepted.

---

## Appendix: related history-side insight (deferred — design M5 / feedback item 4)

Once Part 2 puts thresholds on the catalog, the offline `report` can compute, per scenario, the
Voltaic rank + energy + "distance to next rank" for the stored PB (or rolling median) with **no
network and no AI** — reusing `voltaic_benchmarks`' existing energy/rank functions. That is the
single highest-value automated insight and the main reason Part 2 is worth the churn. Tracked
separately; not in scope for this proposal's two parts.
