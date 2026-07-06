# Coding Standards

## Variable Names

- Avoid single-letter variable names. Prefer descriptive names with at least two or three characters.
- Use `idx` instead of `i` for indexes.
- Use meaningful names for temporary values, such as `row`, `scenario`, `header_key`, or `header_value`.

## Loop Variables

- When looping over an iterable whose variable name is plural, use the singular form as the loop variable.
- Examples:
  - Use `for row in rows`, not `for r in rows`.
  - Use `for scenario in scenarios`, not `for s in scenarios`.
  - Use `for idx in range(...)`, not `for i in range(...)`.

## Per-milestone PR review checklist

Apply to **every** milestone PR before recommending merge. The bar is "meets the design,"
not "looks reasonable." Spec = [`docs/RUN_HISTORY_ARCHITECTURE.md`](docs/RUN_HISTORY_ARCHITECTURE.md)
(§14 acceptance, §15 decisions).

### Objective gate (necessary, not sufficient)
- [ ] **CI green** — Ruff format, Ruff check, mypy, and pytest all pass.
- [ ] New modules added to `[tool.setuptools] py-modules`.
- [ ] Follows the standards above.

### Scope & spec
- [ ] PR scope = **exactly this milestone** — no creep into later ones.
- [ ] **All §14 acceptance boxes** for this milestone are met.
- [ ] **No §15 decision silently changed.** A deviation must be raised for discussion, not slipped in.

### Hard invariants — verify the **tests exist**, not just the code
> Live data is single-page, so multi-page / crash / edge behavior exists **only** in mocks.
> Missing those mocks = inadequate, even if CI is green.
- [ ] **raw→projection:** incremental ingest byte-identical; `--full` re-derives from `raw`;
      canonical serialization (sorted keys / compact) → no false drift.
- [ ] **sync state:** `resume_cursor` is `BACKFILLING`-only; `newest_id` / `api_total_count`
      finalized from the freshest top, never per-page.
- [ ] **`backfill_phase` machine:** crash-before-sweep and crash-during-sweep recover (phase
      persisted; sweep idempotent); `CHECK` constraint present.
- [ ] **edge cases:** empty stream; empty-then-nonempty → full initial backfill (trigger keyed
      off `newest_id IS NULL`); `page_size ≥ 1`.
- [ ] **auth:** no literal secret on the CLI; `--session-file` (path) contract; `sync` never
      opens a window; phase-specific `RefreshAccessTokenError` recovery.
- [ ] **catalog (M3):** unknowns kept + labelled; product-surface fields; group by exact `task_id`.
- [ ] **report (M4):** offline-only (no network/auth); non-APPROVED excluded by default;
      `report_family` scoping; timezone labelled; empty store → "no runs found".

### Security
- [ ] Fixtures **synthetic/sanitized — NO real account dump** committed.
- [ ] No secrets in the diff (`.env`, cookies, tokens, DB); `.gitignore` still covers them.
- [ ] No literal credential accepted on the `voltmeter` command line (decision 24).

### Tests
- [ ] The milestone's specific **§13 tests are present** and **mock-only / offline** (CI can't
      reach Aimlabs).

**Outcome:** Claude posts findings (or "LGTM") → the **user merges** (Claude never merges).
Anything touching auth/secrets gets extra scrutiny.
