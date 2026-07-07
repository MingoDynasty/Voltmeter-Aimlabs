# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Git / GitHub identity (set by user request, 2026-06-06)

Attribute git actions **Claude Code** performs to Claude Code — not the repo owner —
so the user can distinguish their own GitHub activity from Claude's.

- **Commits:** author *and* committer = `Claude Code <noreply@anthropic.com>`. Set this
  **per commit** via `-c` overrides; do **not** change the repo's `user.name`/`user.email`
  (so the user's own manual commits remain attributed to them):

  ```
  git -c user.name="Claude Code" -c user.email="noreply@anthropic.com" commit -m "…"
  ```

  Keep ending commit messages with a co-author trailer naming the current model, e.g.
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

- **Pull requests:** `gh` is authenticated as the repo owner (`MingoDynasty`), so the PR's
  GitHub "opened by" actor will be the owner — this **cannot** be changed via `gh`. Mark the
  PR **title/body** as authored by Claude Code, and ensure the PR's commits use the Claude
  Code identity above (that's what shows throughout the diff).

- Commits made **before** this request are left as-is (the change is "from now on").

## `ignore/` scratch directory — layout (set 2026-07-03)

`ignore/` is the gitignored local scratch area. **This section is the canonical layout** —
`ignore/README.md` is an untracked local mirror that clean clones won't have (recreate it from
here if useful). Route new files into the subdirectories — never drop them at the top level:

- `ignore/pr-reviews/` — PR review handoff docs (`pr<num>-review.md`) and per-PR pushback notes
- `ignore/handoffs/` — milestone handoff docs and working-thread notes
- `ignore/scripts/` — ad-hoc probe/exploration scripts (run from repo root; they
  write their output under `ignore/aimlabs_dump/`)
- `ignore/aimlabs_dump/` — raw API dump output from those scripts (personal play data)

A file that fits no category gets a new subdirectory (record it here; mirror to
`ignore/README.md` if that file exists locally).

## Run-history pipeline — build process (consistent across chats)

The work is built over many chats (≈ a new chat per milestone). To stay consistent, every
chat follows this and updates the status below. The **design/spec** is
`docs/RUN_HISTORY_ARCHITECTURE.md` (rev 9): **§14** = milestones + acceptance
criteria, **§15** = settled decisions (do not relitigate).

**Roles:** Codex implements · Claude reviews · the **user merges** (Claude never merges; `gh`
is the user's account).

**Workflow — one PR per milestone, off `main`:**
1. **Implement.** Codex implements directly against design **§14** (acceptance criteria) +
   **§15** (settled decisions) — the design *is* the spec; no separate handoff brief is needed.
   (Codex commits under its own identity so the three actors stay distinguishable.)
2. **Review.** Claude reviews the PR against the "Per-milestone PR review checklist" in
   `CODING_STANDARDS.md` — CI-green is the floor, not the bar; verify the hard-case tests actually exist.
3. **Merge.** The user merges after Claude's LGTM, then the next milestone starts.

**Order:** `M1 → M2a → M2b → M3 (∥ after M1) → M4 → M6 → M7a → M7b`. Review+merge each before the next
builds on it (serialize the critical path; M3 may run parallel to M2). CI gate =
ruff format/ruff check/mypy/pytest; new modules → add to `[tool.setuptools] py-modules`; **fixtures
synthetic only** (never a real account dump).

**Status — RECONCILE against merged PRs at the start of EACH chat** (`gh pr list --state merged`);
GitHub is the source of truth, this line is only a cache — refresh it first, then update it here
when a milestone merges (don't rely on it being current when you arrive):
- Design: **rev 9** complete. · M0 live-validation: ✅. · `.gitignore`: ✅. ·
  CI file lists cover the pipeline modules: ✅ (#16).
- **M1** store ✅ (#7) · **M2a** ✅ (#10) · **M2b** ✅ (#12) · **M3** ✅ (#13) ·
  **M4** ✅ runs table + `report` (#15) · **M5** ⬜ trend (deferred) ·
  **M6a** ✅ CLI wiring — `sync`/`login`/`refresh-catalog`, `--full`, contamination check (#19) ·
  **M6b** ✅ decommission + docs reconciliation — PoC scripts retired, README/`config.example.toml`
  on `AIMLAB_SESSION`, legacy `AIMLABS_COOKIE`/`session_cookie` channels **removed** (no users
  pre-release; user decision this chat), design docs relocated to `docs/` (#22)
- **M6b closed the original pipeline build** (M5/trend deferred); M4+M6a are user-release-complete
  (decision 20). **M7a/M7b scheduled 2026-07-05** (user decision; 2026-07-04 audit finding 1):
  `docs/SCORES_CONSOLIDATION_PROPOSAL.md` is the spec, accepted via #41 after the Codex
  proposal-review round ran 2026-07-05 (PUSHBACK, resolved — notably `--header` retired per
  decision 24; threshold storage resolved directly-on-record; this file made canonical for the
  `ignore/` layout).
- **M7a** ✅ `voltmeter scores` subcommand (thin lazy adapter), `main.py` deleted, `--header`
  retired, README/docs/ci.yml swept, `docs/example_output.log` regenerated live, full-catalog
  parity goldens (`tests/fixtures/scores_full_catalog_{table,json}_golden.json`) captured as
  **M7b's regression baseline** (#45; review trail `ignore/pr-reviews/pr45-review.md`, LGTM
  after two re-reviews). **M7b** ⬜ scenario-metadata unification — retire
  `benchmark_constants.py` (rehome its non-metadata exports, e.g. `DEFAULT_DIFFICULTY`), tier
  thresholds **directly on `ScenarioCatalogRecord`**, scores-layer label/slug derivation, s2/s3
  gated on `has_leaderboards` — **next up**; Codex handoff = vault note "Voltmeter-Aimlabs —
  Audit 2026-07-04 — handoff prompts", **Prompt 4** (pr45-review P4 notes: exit-code 2-vs-1
  harmonization and adapter default absorption stay post-M7b/optional).
- _M6 was split 2026-06-12 (user decision; supersedes the single-M6 row in design §14):
  **M6a** = wire the remaining CLI verbs + sync-side pieces, code only; **M6b** = retire
  `proof-of-concepts/` scripts, reconcile `README.md`/`config.example.toml` onto
  `AIMLAB_SESSION`, relocate the design docs. Also settled: `aimlab_scores.py` gets unified
  auth but keeps its own entry point (not folded into `voltmeter`) — **the entry-point half of
  that is superseded by M7a** (proposal accepted 2026-07-05). User-release-complete
  still requires M6b (decision 20)._
- _M6a review (PR #19) deferred items — now resolved: **P3** routed `play_store` field-drift
  warnings to the caller's `warning_stream` (PR #20); and the **legacy `session_cookie`/`AIMLABS_COOKIE`
  channels** were **removed outright** in M6b (no users pre-release), which also resolved the design
  §4/§11-vs-§12 wording inconsistency by deletion (decision 7 updated)._
- _Last reconciled: 2026-07-06 (Claude Code); #41 (scheduling bless) and **M7a as #45** merged
  this date, plus maintenance: Linux CI matrix #40, Dependabot batch #42–#44. M7b is the only
  open milestone (M5/trend still deferred); Prompt 4 handoff written. NB: PR #46 (tooling swap)
  went CONFLICTING when #45 merged — per plan it gets regenerated, not rebased; sequence it
  around M7b deliberately (both churn pyproject/ci.yml)._
- (`M1_BRIEF.md` was vestigial — briefs were dropped; design §14 is the spec — and was deleted
  2026-06-12; recoverable from git history. **Docs finalization — done in M6b:**
  `RUN_HISTORY_ARCHITECTURE.md` + `ARCHITECTURE.md` promoted to `docs/`; `REVIEW_CHECKLIST.md`
  folded into `CODING_STANDARDS.md`; the `DESIGN_REVIEW*`/`DESIGN_PUSHBACK*` trail deleted
  (git history preserves it) and its inline citations scrubbed from the spec.)
