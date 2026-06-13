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

## Run-history pipeline — build process (consistent across chats)

The work is built over many chats (≈ a new chat per milestone). To stay consistent, every
chat follows this and updates the status below. The **design/spec** is
`proof-of-concepts/RUN_HISTORY_ARCHITECTURE.md` (rev 9): **§14** = milestones + acceptance
criteria, **§15** = settled decisions (do not relitigate).

**Roles:** Codex implements · Claude reviews · the **user merges** (Claude never merges; `gh`
is the user's account).

**Workflow — one PR per milestone, off `main`:**
1. **Implement.** Codex implements directly against design **§14** (acceptance criteria) +
   **§15** (settled decisions) — the design *is* the spec; no separate handoff brief is needed.
   (Codex commits under its own identity so the three actors stay distinguishable.)
2. **Review.** Claude reviews the PR against `proof-of-concepts/REVIEW_CHECKLIST.md` — CI-green
   is the floor, not the bar; verify the hard-case tests actually exist.
3. **Merge.** The user merges after Claude's LGTM, then the next milestone starts.

**Order:** `M1 → M2a → M2b → M3 (∥ after M1) → M4 → M6`. Review+merge each before the next
builds on it (serialize the critical path; M3 may run parallel to M2). CI gate =
mypy/pytest/pylint/ruff; new modules → add to `[tool.setuptools] py-modules`; **fixtures
synthetic only** (never a real account dump).

**Status — RECONCILE against merged PRs at the start of EACH chat** (`gh pr list --state merged`);
GitHub is the source of truth, this line is only a cache — refresh it first, then update it here
when a milestone merges (don't rely on it being current when you arrive):
- Design: **rev 9** complete. · M0 live-validation: ✅. · `.gitignore`: ✅. ·
  CI file lists cover the pipeline modules: ✅ (#16).
- **M1** store ✅ (#7) · **M2a** ✅ (#10) · **M2b** ✅ (#12) · **M3** ✅ (#13) ·
  **M4** ✅ runs table + `report` (#15) · **M5** ⬜ trend (deferred) ·
  **M6a** ⬜ CLI wiring — `sync`/`login`/`refresh-catalog`, `--full`, contamination check
  (PR open) · **M6b** ⬜ decommission + docs reconciliation (**next after M6a**)
- _M6 was split 2026-06-12 (user decision; supersedes the single-M6 row in design §14):
  **M6a** = wire the remaining CLI verbs + sync-side pieces, code only; **M6b** = retire
  `proof-of-concepts/` scripts, reconcile `README.md`/`config.example.toml` onto
  `AIMLAB_SESSION`, relocate the design docs. Also settled: `aimlab_scores.py` gets unified
  auth but keeps its own entry point (not folded into `voltmeter`). User-release-complete
  still requires M6b (decision 20)._
- _Last reconciled: 2026-06-12 (Claude Code)._
- (`M1_BRIEF.md` was vestigial — briefs were dropped; design §14 is the spec — and was deleted
  2026-06-12; recoverable from git history. The `DESIGN_REVIEW*`/`DESIGN_PUSHBACK*` trail stays
  until finalization: the design doc cites it inline for decision provenance, so do **not**
  delete it while the spec is active. **Docs finalization, after M6 merges:** promote
  `RUN_HISTORY_ARCHITECTURE.md` to `docs/`, fold `REVIEW_CHECKLIST.md` into
  `CODING_STANDARDS.md`, delete the review/pushback trail (git history preserves it), and
  decide `ARCHITECTURE.md`'s fate alongside the PoC scripts it documents.)
