# AGENTS.md

Shared guidance for agents working in this repository.

## Git Identity

Commits made by Codex must use:

- Name: `Codex`
- Email: `codex@openai.com`

Use per-commit identity overrides by default:

```powershell
git -c user.name=Codex -c user.email=codex@openai.com commit -m "..."
```

If a different commit path is used, verify the effective identity first:

```powershell
git config --show-origin --get user.name
git config --show-origin --get user.email
```

Repo-local config is acceptable in a dedicated Codex worktree, but avoid changing shared worktree config unless the user asks for it.
Do not commit Codex-authored work as the repo owner or the user's global Git identity.

## Git Workflow

- Use one PR per milestone or scoped task.
- Codex-created branches should use the `codex/` prefix unless the user asks otherwise.
- The user merges PRs.
- After a merge, switch to `main`, fast-forward from `origin/main`, delete the merged local branch, and confirm a clean worktree.
- Before staging, inspect `git status -sb` and stage only files that belong to the current task. Leave unrelated user or tool changes alone.

## Validation

Use the `uv` workflow where possible:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

`ruff format` wraps at 88 characters, while Ruff's E501 rule enforces a 120-character hard ceiling. `ruff check .` is full-repo in CI. The proof-of-concept Python scripts were retired in M6b, so full-repo checks no longer flag untouched POC noise; the remaining `proof-of-concepts/` contents are gitignored local data and are not linted.

## Coding Standards

Follow `CODING_STANDARDS.md`.

Local conventions:

- Avoid single-letter variable names.
- Use `idx` for indexes.
- Use singular loop variables for plural iterables, such as `for row in rows`.
- Prefer focused, synthetic regression tests for edge cases called out in reviews.

## Project Settings

- Python requirement: `>=3.14`.
- Add new top-level modules to `[tool.setuptools] py-modules` in `pyproject.toml`.
- Keep fixtures synthetic and sanitized.
- Never commit real Aimlabs account dumps, local DBs, cookies, tokens, `.env`, or `config.toml`.

## Documentation

- Keep README examples compact and user-facing.
- For long command output, prefer a short README excerpt linked to a sanitized artifact under `docs/`.
- Do not reconcile README/config examples for future run-history commands until the milestone that exposes those commands requires it.

## Run-History Pipeline

- Design source of truth: `docs/RUN_HISTORY_ARCHITECTURE.md`.
- Review checklist: the "Per-milestone PR review checklist" section of `CODING_STANDARDS.md`.
- Keep PR scope to the current milestone.
- Do not silently change settled decisions from the design doc; raise deviations for discussion.
- Use mock-only/offline tests for history sync behavior. Live data is single-page, so multi-page, crash, retry, and cursor behavior must be synthetic.
