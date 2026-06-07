# Run-History Design Review Notes - Rev 3

**Status:** Follow-up review notes  
**Reviewed docs:** `RUN_HISTORY_ARCHITECTURE.md` rev 3, `DESIGN_PUSHBACK_v2.md`, `ARCHITECTURE.md`, `README.md`, `config.example.toml`  
**Review stance:** Rev 3 resolves the prior architecture blockers. This note captures the remaining implementation-readiness concerns and documentation cleanup items.

## Executive Summary

Rev 3 is implementation-ready from an architecture standpoint. The major concerns from rev 2 have been addressed:

- canonical `raw` JSON now structurally drives typed projection columns
- incremental progress state is separated from completed-sync high-water state
- the post-backfill top sweep now applies to every initial backfill
- auth failure handling distinguishes terminal re-login states from transient API failures
- report scope now has an explicit `report_family` default
- the M2a/M2b split is clearer

I do not see any remaining design blockers. The remaining items are mostly edge-case hardening and handoff/documentation consistency.

## Remaining Concerns Before Implementation

### 1. Empty first-page behavior is undefined

The sync pseudocode captures the run top from the first page:

```text
run_top_id, run_top_ended_at = page[0].id, page[0].ended_at
```

That works for the validated account, but a new account or a report/sync filter returning zero mode-42 plays will produce an empty first page. The design should define this path before implementation so the sync state cannot be left ambiguous.

Recommendation:

- If the first page is empty, insert no play rows.
- Mark the initial backfill complete.
- Store `newest_id = NULL` and `newest_ended_at = NULL`.
- Store `api_total_count = 0`.
- Ensure the offline report path cleanly renders "no runs found."
- Add tests for an empty first page and `page_size < 1` config validation.

Severity: **P2**. This is not a blocker for the validated account, but it is a real product edge case and easy to test now.

### 2. Repo-facing setup docs still contradict rev 3

Rev 3 now says the canonical account config key is `aimlabs_user_id`, `AIMLAB_SESSION` is the production session secret path, and `AIMLABS_COOKIE` should be dropped from README/config.

However:

- `README.md` still shows `user_id`.
- `README.md` still documents `session_cookie` / `AIMLABS_COOKIE`.
- `config.example.toml` still uses `user_id` and `session_cookie`.
- `README.md` still says `resources/aimlabs/valorant_s1.json` is the active benchmark resource, while rev 3 says reporting defaults to `report_family = "all"`.

Recommendation:

- Update `README.md` and `config.example.toml` before implementation handoff.
- Use `aimlabs_user_id` everywhere.
- Remove `AIMLABS_COOKIE` from user-facing setup instructions.
- Keep any `session_cookie` support explicitly labeled as legacy if it remains supported.
- Update benchmark/report copy to match `report_family = "all"` by default and `report_family = "valorant"` for S1-only reporting.

Severity: **P2**. The architecture doc is clear, but implementers and users usually follow README/config examples first.

### 3. Projection serialization should be deterministic

Rev 3 correctly makes typed columns a pure projection of canonical `raw` and calls for byte-identical idempotency tests. That guarantee depends on deterministic serialization for any projected JSON/text fields, especially `performance_scores`.

Without an explicit rule, two semantically identical projections could differ because of JSON key order, whitespace, or parser output formatting. That would create false drift warnings or flaky byte-identical tests.

Recommendation:

- Specify a canonical serializer for projected JSON text fields, for example stable key ordering and compact separators.
- Alternatively, preserve exact sub-values from `raw` where possible.
- Add a fixture where `performance_scores` input ordering/formatting would otherwise vary.

Severity: **P3**. This is implementation-detail hardening, but it supports one of the design's core test claims.

### 4. Rev 3 review-status header omits `DESIGN_PUSHBACK_v2.md`

The top of `RUN_HISTORY_ARCHITECTURE.md` says rev 3 incorporates the review docs and points reviewers to `DESIGN_PUSHBACK.md`, but `DESIGN_PUSHBACK_v2.md` is now also part of the design record.

Recommendation:

- Update the review-status note to mention both pushback documents.
- Make it clear that `DESIGN_PUSHBACK_v2.md` captures the round-2 qualifications and the live-validation hedges that informed rev 3.

Severity: **P3**. This is handoff polish, but it keeps future reviewers from missing important context.

### 5. Decision log numbering is out of order

The decisions log currently lists decision 15 before decisions 12-14.

Recommendation:

- Reorder or renumber the decisions so the table is sequential.

Severity: **P3**. Pure documentation polish.

## Things That Look Resolved

- The `raw` versus projection contract is now structurally sound.
- `sync --full` now re-derives projections from canonical raw data instead of maintaining a mutable/immutable split.
- `resume_cursor` and `newest_id` now have distinct meanings.
- `newest_id` is finalized only after safe completion or the appropriate top sweep.
- Practice contamination is treated as a cheap per-sync warning instead of an unproven structural guarantee.
- `plays_agg` is now documented as a complementary cross-check endpoint.
- `RefreshAccessTokenError` is handled as a terminal re-login state.
- Cursor expiry remains honestly unvalidated but non-blocking because M2b owns cursor-rejection fallback.
- Report scope now has an explicit default.

## Bottom Line

I would not reopen the broad design. Rev 3 is solid enough to implement.

Before implementation or external handoff, I would tighten:

1. empty first-page behavior
2. README/config.example consistency
3. deterministic projection serialization wording
4. review-status references
5. decision-log numbering

The first two are the only items I would treat as important pre-implementation cleanup. The rest can be handled as doc polish or folded into M1 test design.
