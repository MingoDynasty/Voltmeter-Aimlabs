# AGENTS.md

Shared guidance for agents working in this repository.

## Git Identity

Commits made by Codex must use:

- Name: `Codex`
- Email: `codex@openai.com`

Before committing, verify the effective identity:

```powershell
git config --show-origin --get user.name
git config --show-origin --get user.email
```

If needed, set repo-local config before committing:

```powershell
git config user.name Codex
git config user.email codex@openai.com
```

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
uv run pytest
uv run black --check <touched files>
uv run mypy .
uv run pylint <touched files>
uv run ruff check .
```

Known caveat: full-repo `uv run black --check .` may report existing untouched proof-of-concept files. Do not format unrelated POC files unless that is explicitly in scope.

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

- Design source of truth: `proof-of-concepts/RUN_HISTORY_ARCHITECTURE.md`.
- Review checklist: `proof-of-concepts/REVIEW_CHECKLIST.md`.
- Keep PR scope to the current milestone.
- Do not silently change settled decisions from the design doc; raise deviations for discussion.
- Use mock-only/offline tests for history sync behavior. Live data is single-page, so multi-page, crash, retry, and cursor behavior must be synthetic.
