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

  Keep ending commit messages with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

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
1. **Brief.** Claude writes a scoped handoff brief for the milestone (format/example:
   `proof-of-concepts/M1_BRIEF.md`). Scope = that milestone only.
2. **Implement.** Codex implements against the brief + design §14. (Ideally Codex commits under
   a distinct identity, e.g. `Codex`, so the three actors are distinguishable — configured on
   Codex's side.)
3. **Review.** Claude reviews the PR against `proof-of-concepts/REVIEW_CHECKLIST.md` — CI-green
   is the floor, not the bar; verify the hard-case tests actually exist.
4. **Merge.** The user merges after Claude's LGTM, then the next milestone starts.

**Order:** `M1 → M2a → M2b → M3 (∥ after M1) → M4 → M6`. Review+merge each before the next
builds on it (serialize the critical path; M3 may run parallel to M2). CI gate =
mypy/pytest/pylint/ruff; new modules → add to `[tool.setuptools] py-modules`; **fixtures
synthetic only** (never a real account dump).

**Status — update this when a milestone merges** (source of truth for "done" = merged PRs on `main`):
- Design: **rev 9** complete (8 review rounds). · M0 live-validation: ✅ done. · `.gitignore`: ✅ done.
- **M1** store: ⬜ not started (next) · **M2a** ⬜ · **M2b** ⬜ · **M3** ⬜ · **M4** ⬜ · **M6** ⬜
- (When finalizing the design — dereferencing pass — see `RUN_HISTORY_ARCHITECTURE.md` status
  header history; the `DESIGN_REVIEW*`/`DESIGN_PUSHBACK*` and `M1_BRIEF`/`REVIEW_CHECKLIST` docs
  live under `proof-of-concepts/` and may relocate with the design.)
