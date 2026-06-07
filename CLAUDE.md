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
