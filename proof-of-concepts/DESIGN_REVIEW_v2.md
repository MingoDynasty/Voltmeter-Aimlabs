# Run-History Design Review Notes - Rev 2

**Status:** Follow-up review notes  
**Reviewed docs:** `RUN_HISTORY_ARCHITECTURE.md` rev 2, `DESIGN_PUSHBACK.md`, `ARCHITECTURE.md`  
**Review stance:** Most rev-1 blockers are resolved. This note captures only the remaining concerns before implementation.

## Executive Summary

Rev 2 is meaningfully stronger than the first draft. The design now gives clear answers for history scope sequencing, production auth policy, offline reporting, catalog product-surface metadata, status filtering, timestamp policy, and milestone sizing. The pushback is also reasonable: keeping `aimlabs_user_id`, splitting M2, and treating practice scope as a live-validation task are all good calls.

I would still tighten two areas before implementation starts:

- the contract between canonical `raw` JSON and typed/indexed projection columns
- the exact sync-state/high-water update semantics

Those two ambiguities can become persistent storage bugs if interpreted differently by the implementer.

## Remaining Blockers Before M1/M2

### 1. Raw-canonical conflicts with mutable-only full sync

Rev 2 says:

- every play stores full `raw` node JSON
- `raw` is canonical
- typed columns are an indexed projection derived from `raw`
- on disagreement, `raw` wins

That is a good model. The remaining issue is that `sync --full` is documented as updating `raw`, `gridshield_status`, and `last_seen_at` while leaving "immutable" typed columns untouched.

If upstream ever changes a field currently classified as immutable, the database can contain:

- canonical `raw` with the new upstream value
- stale typed/indexed projection columns with the old value
- report/query code reading the stale projection for performance

Fields affected include `score`, `ended_at`, `task_id`, `performance_scores`, durations, and potentially `is_practice`.

Recommendation:

- Keep `raw` canonical.
- On `sync --full`, parse the incoming `raw` and compare derived immutable fields against stored projection columns.
- If derived immutable fields match, update mutable fields as planned.
- If derived immutable fields differ, either:
  - rebuild projection columns from `raw` and emit a visible warning, or
  - fail the sync with a clear "immutable field drift" warning requiring an explicit repair command.

Preferred default: rebuild projection columns from canonical `raw`, update a `projection_version` or `projection_updated_at` field if added, and warn. This keeps the "raw wins" rule real instead of only documentary.

Testing to add:

- A `--full` fixture where only `gridshield_status` changes.
- A `--full` fixture where `raw.score` changes.
- A `--full` fixture where `raw.performanceScores` changes.
- Assert the chosen repair/warning behavior is deterministic.

### 2. High-water updates are still underspecified

The pseudocode writes `sync_state(resume_cursor=endCursor, newest_*, api_total_count)` during every page checkpoint. That can be misread as updating `newest_id` and `newest_ended_at` from the current page on every page.

For an incremental sync, `newest_id` must mean the newest/top play observed for the account, not "newest play on the most recently committed page." If `newest_id` drifts downward as pages are checkpointed, a future run may early-break at the wrong point and miss newer plays.

Recommendation:

- Separate checkpoint state from high-water state.
- During pagination, update only:
  - `resume_cursor`
  - `api_total_count`
  - `updated_at`
  - any explicit in-progress/backfill fields
- Capture `run_top_id` and `run_top_ended_at` from the first page of the sync.
- Finalize `newest_id = run_top_id` and `newest_ended_at = run_top_ended_at` only after the sync completes safely or after the early-break condition is satisfied.
- For first backfill, do not finalize `newest_id` until the post-backfill top sweep has completed.

Suggested wording:

```text
resume_cursor is a progress checkpoint. newest_id is a completed-sync high-water mark.
They are updated at different times and should not be conflated.
```

Testing to add:

- A three-page incremental sync where page 2 and page 3 contain older data; assert `newest_id` remains the first page's top id after completion.
- A crash after page 2 checkpoint; assert resume uses `resume_cursor` but does not advance `newest_id` to page 2.
- A first backfill plus top sweep; assert `newest_id` is the true top after the sweep.

## Important Remaining Concerns

### 3. Top sweep applies to every initial backfill

The rev-2 wording says "when a resumed backfill reaches the end of the stream" run the post-backfill top sweep.

New plays can arrive during an uninterrupted first backfill too. The sweep should happen at the end of every initial backfill, resumed or not.

Recommendation: change the wording to "when an initial backfill reaches the end of the stream."

### 4. Auth doc is stale relative to rev 2

`RUN_HISTORY_ARCHITECTURE.md` and `DESIGN_PUSHBACK.md` resolve the auth-policy and `.gitignore` feedback, but `ARCHITECTURE.md` still contains older statements:

- "clean, legitimate path"
- `.env` is not yet gitignored
- `.gitignore` must be fixed before this lands in tracked code

Because rev 2 still depends on `ARCHITECTURE.md`, a handoff reader can get conflicting instructions.

Recommendation:

- Update the auth wording to match the pushback resolution:

```text
the least-invasive own-account path: it uses the site as its own frontend does,
with no password scripting, credential theft, or third-party data access, while
still relying on undocumented frontend behavior that may change
```

- Update `.gitignore` notes to say the hardening is already done.
- Keep the warning that real `.env` files, tokens, cookies, DBs, and history dumps must never be committed.

### 5. Default product/report scope needs one final sentence

The catalog now carries enough metadata to distinguish product surfaces:

- `benchmark_alias`
- `benchmark_name`
- `family`
- `season`
- `is_active`
- `has_leaderboards`

That resolves the storage/catalog side. The reporting side still says "Voltaic-scoped by default" without choosing whether the first report defaults to:

- Valorant only
- active only
- all known Voltaic Aimlabs
- config-selected family

Recommendation: add one explicit default, even if it is temporary.

Suggested default:

```text
Default report scope follows config.report_family, defaulting to "valorant" for the current product.
Users may opt into "all" to include broader Aimlabs S2/S3 scenarios.
```

If the product is intentionally moving beyond Valorant, choose `active` or `all` instead. The important part is to prevent implementers from each choosing a different default.

### 6. M2a/M2b acceptance has an overlapping phrase

M2a includes "idempotent top-restart on failure", while M2b owns "cursor-invalidation fallback." Those sound like the same behavior.

Recommendation:

- If M2a means ordinary process restart after a crash, rename it to "resume/re-run safely after local interruption."
- Keep cursor rejection/top restart in M2b.
- If M2a really includes top restart on cursor failure, move the M2b cursor item out or mark it as hardening tests only.

## Things That Look Resolved

- Keeping `aimlabs_user_id` is fine. It avoids unnecessary config churn, and the docs now explain that `userId` and `anthicId` are the same account value.
- Practice scope is correctly moved to a pre-M1 live-validation checklist.
- Session-cookie auth is now correctly canonical for production `sync`; raw bearer is debug/short-run only.
- `report` is now clearly offline-only.
- Status handling is improved: store all, exclude non-APPROVED/practice from stats by default, show a visible note.
- The catalog now preserves product-surface metadata instead of flattening all resources into a season-only map.
- The extra global `(account_id, ended_at DESC)` index is included.
- Splitting M2 into core sync and resilience hardening is a good reviewability improvement.

## Bottom Line

The design is close. I would not reopen the broad architecture. Before implementation, I would only ask for a small rev-3 cleanup that:

1. makes `raw` vs projection repair behavior explicit
2. separates `resume_cursor` checkpointing from `newest_id` high-water finalization
3. clarifies top sweep applies after every first backfill
4. syncs `ARCHITECTURE.md` with rev-2 auth/security wording
5. names the default report family/scope
6. removes the M2a/M2b overlap

After that, M1 can start with much lower risk.
