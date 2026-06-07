# Run-History Design Review Notes

**Status:** Handoff review notes  
**Reviewed docs:** `ARCHITECTURE.md`, `RUN_HISTORY_ARCHITECTURE.md`  
**Review stance:** Treat the Python files as proof-of-concept evidence only. The concerns below are about the production design documentation and implementation contract.

## Executive Summary

The design is fundamentally sound. The core decision to fetch one authenticated, reverse-chronological account stream and bucket by `task_id` locally is the right architectural shape. It avoids per-scenario cursor sprawl, discovers unknown scenarios naturally, and keeps analysis offline once data is synced.

I would not block implementation on the overall architecture. I would block implementation on a few definitions that become hard to change once persisted: exact history scope, backfill/resume semantics, upsert semantics, auth policy, and naming/secret consistency.

## Blockers Before M1/M2

### 1. Define the exact history scope

The docs currently use phrases like "entire play history" and "full Aimlabs play history", but the validated query is scoped to `filter: { mode: 42 }`.

Before implementation, decide and document:

- Is the product syncing all Aimlabs plays, or all mode-42 Aimlabs plays?
- Are practice runs included or excluded?
- Does the history query expose `is_practice`, `input_device`, or another field needed to separate benchmark-valid runs from practice/local variants?
- If practice/non-benchmark plays are stored, are they included in PBs and rolling stats?

Recommendation: define the production scope as "all authenticated mode-42 plays for the configured account" unless live validation proves a broader scope is needed. If practice status is available, store it. If it is not available, explicitly document that benchmark validity is inferred from `task_id` catalog membership plus `mode == 42`.

### 2. Specify the first-backfill state machine

`resume_cursor` is not enough by itself to define safe resumability. The design should spell out how these cases behave:

- New plays arrive while an initial backfill is interrupted.
- A Relay cursor expires or becomes invalid between runs.
- A 401 happens after a page is fetched but before it is written.
- A page write succeeds but `sync_state` update fails.

Recommendation:

- Treat page ingest plus sync-state checkpoint as one SQLite transaction.
- During first backfill, persist a `backfill_started_newest_id` or equivalent anchor from the first page.
- When a resumed backfill reaches the end, immediately run a top-of-stream incremental sweep until that anchor/high-water mark is reached.
- If the cursor is rejected, fall back to a top-of-stream restart with idempotent upserts rather than failing permanently.

### 3. Clarify upsert semantics and timestamp fields

The schema has `fetched_at`, while the testing strategy says ingesting the same fixture twice should produce a byte-identical store. Those two goals conflict if `fetched_at` updates on every upsert.

Recommendation:

- Replace or supplement `fetched_at` with explicit fields:
  - `first_fetched_at`: when the play was first inserted.
  - `last_seen_at`: when the play was most recently observed upstream.
- Define normal incremental upsert as insert-only for immutable play facts.
- Define full sync as allowed to update mutable fields such as `gridshield_status`, `raw`, and `last_seen_at`.
- Update the idempotency acceptance test to ignore `last_seen_at`, or test that immutable columns are byte-identical.

### 4. Make auth policy production-specific

The auth design correctly identifies the session cookie as the durable credential and the bearer as short-lived. For production sync, raw bearer fallback should not be treated as equivalent to session-cookie auth.

Recommendation:

- `AIMLAB_SESSION` is the canonical secret.
- Raw bearer (`AIMLAB_TOKEN` or manual `Authorization`) is debug-only or short-run-only.
- Long backfills require session-cookie auth so 401 re-mint can work.
- `report` and other offline commands must never trigger login or auth resolution.
- `sync` may auto-login in interactive desktop contexts, but `--no-login` should be the default for any scheduled/unattended mode.

### 5. Standardize account and secret names

The current docs and repo use several names for the same concepts:

- `user_id`
- `anthic_id`
- `session_cookie`
- `AIMLABS_COOKIE`
- `AIMLAB_SESSION`
- `AIMLAB_TOKEN`

This will create implementation drift unless resolved before `config.py` grows.

Recommendation:

- Use `anthic_id` in `config.toml` for the Aimlabs account id.
- Use `AIMLAB_SESSION` in `.env`/environment for the session cookie.
- Keep `session_cookie` only as a documented legacy config fallback, if at all.
- Drop `AIMLABS_COOKIE` from README/config examples when history auth lands.
- Keep command-line secrets out of the normal UX.

## Important Design Concerns

### Global runs table needs its own index

The proposed index on `(account_id, task_id, ended_at DESC)` is good for per-scenario stats, but the near-term runs table is global reverse-chronological.

Add:

```sql
CREATE INDEX idx_plays_acct_date ON plays(account_id, ended_at DESC);
```

### Deletion detection is only a cheap signal

`stored > totalCount` catches some upstream deletion/membership drift, but not all drift. For example, one deleted play plus one new play can keep counts equal.

Recommendation: keep the passive `totalCount` warning, but describe it as a cheap drift signal rather than deletion detection. Precise deletion detection requires full id set comparison.

### Non-APPROVED runs should not silently affect stats

The docs propose storing all runs and default analysis including all runs. Because this is a progress/rank tracker, including flagged/non-APPROVED runs by default can mislead the user.

Recommendation:

- Store all runs.
- Default ranking/stat outputs should either exclude non-APPROVED runs or visibly flag them.
- Console reports can include a note such as `3 non-APPROVED runs excluded`.
- Keep a config/report option to include all statuses for audit/debug.

### Catalog scope needs a first-class label

The design unions `resources/aimlabs/*.json`, but those files are not all the same product surface. `valorant_s1` is "Voltaic Valorant Benchmarks"; `aimlabs_s2` and `aimlabs_s3` are broader "Voltaic Aimlabs Benchmarks".

Recommendation: scenario catalog records should include at least:

- `benchmark_alias`
- `benchmark_name`
- `season`
- `family` or `game_scope` such as `valorant` / `aimlabs`
- `is_active`
- `has_leaderboards`

That lets the UI/report choose "VALORANT only", "current active", or "all known Voltaic Aimlabs" without reworking storage.

### Store raw play nodes consistently

The schema marks `raw` as optional, but future-proofing is one of the reasons for SQLite instead of flat summaries.

Recommendation: store the full raw node JSON for every play unless there is a concrete size/privacy reason not to. This protects against future metric needs without re-fetching.

### Rate limiting belongs in M2, not later

The docs mention adding exponential backoff on 429 before the large-backfill path matters. The large-backfill path is exactly M2.

Recommendation: M2 acceptance should include retry/backoff for 429 and transient 5xx responses. Keep it simple, but make it test-covered.

### Report command must remain offline

The docs say analysis/reporting is pure offline. The CLI/auth UX should preserve that invariant.

Recommendation:

- `voltmeter report` never touches auth, network, or login.
- `voltmeter sync` performs auth/network.
- `voltmeter sync --report` can print a report after sync, but the report code itself should still read only from the store.

### README and design docs are stale on `.gitignore`

Both design docs say `.gitignore` still needs `.env`, token/cookie, DB, and history-dump protections. The repo already has those patterns.

Recommendation: update both docs to say this hardening is already done, while keeping the warning not to commit real history dumps or secrets.

## Open Question Recommendations

### Timezone default

Store API timestamps as UTC strings exactly as received. Display in system-local timezone by default, with an optional `timezone` config key. Reports should label the timezone used.

### Non-APPROVED plays

Store all. Exclude or visibly flag non-APPROVED plays by default in stats/ranking output. Include an override for audit/debug.

### Report output format

Console table is acceptable for the first user-facing report. JSON should be part of the same milestone if this output will feed a dashboard or tests. CSV can be fast-follow.

### Module split

The seven-module split is reasonable:

- `aimlabs_auth.py`: auth and login only.
- `aimlabs_history.py`: build payloads, fetch pages, parse nodes.
- `history_sync.py`: orchestration and sync-state transitions.
- `play_store.py`: SQLite schema, migrations, upserts, queries.
- `scenario_catalog.py`: catalog projection and lookup.
- `history_report.py`: offline presentation and stats.
- `cli.py`: command routing.

Do not fold `aimlabs_history.py` into `history_sync.py`; keeping fetch/parse stateless makes pagination tests easier.

### Energy/rank over history

Keep out of M1-M4 unless the first report needs rank/energy context. If added later, compute from stored plays plus catalog/threshold resources, not from fetched PB snapshots.

### Deletion handling

Passive drift warning is acceptable for the first release. Document that it does not identify all deletions and that exact reconciliation is an opt-in full sync mode.

## Suggested Milestone Adjustments

### M1: Store

Add to acceptance criteria:

- `idx_plays_acct_date` exists.
- Timestamp fields are defined as `first_fetched_at` / `last_seen_at` or equivalent.
- Upsert semantics are explicit and tested.
- Real account fixtures are not committed; synthetic fixtures only.

### M2: Incremental sync

Add to acceptance criteria:

- First-backfill state machine covers interruption plus new plays during interruption.
- Page ingest and sync checkpoint are transactional.
- 401 re-mint requires session-cookie auth.
- 429 and transient 5xx retry/backoff are mock-tested.
- Cursor invalidation fallback is defined.

### M3: Scenario catalog

Add to acceptance criteria:

- Catalog records include benchmark alias/family/active flags, not just season/name.
- Duplicate `task_id` handling is defined even if current resources have no collisions.
- Unknown task ids are retained and reportable.

### M4: Runs table and stats

Add to acceptance criteria:

- Report is offline-only.
- Status filtering/default behavior is resolved.
- Timezone is labeled.
- JSON output is available if downstream/dashboard use is expected.

## Suggested Documentation Edits

1. Replace "entire/full Aimlabs play history" with the exact production scope.
2. Update the auth docs to soften "clean, legitimate path" into "least invasive local-only path"; it still relies on undocumented frontend behavior.
3. Update `.gitignore` sections to reflect current repo state.
4. Reconcile README/config examples with `AIMLAB_SESSION` and `anthic_id`.
5. Add a "Sync state machine" subsection with the transaction and resume rules.
6. Add a "Data mutability" subsection describing immutable fields, mutable fields, and update rules.
7. Add a "Catalog scope" subsection distinguishing Valorant S1 from broader Aimlabs S2/S3 resources.
8. Add a "Report command invariants" note: no network, no auth, no login.

## Bottom Line

The proposed architecture is worth implementing. The main risk is not the high-level model; it is underspecified persistence and sync behavior. Lock down the scope, auth contract, backfill state machine, and catalog metadata shape before M1/M2, and the implementation should be straightforward to stage safely.
