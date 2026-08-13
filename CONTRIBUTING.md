# Development workflow

`main` is the stable primary branch. Do not implement features or fixes directly on `main`.

## Branch lifecycle

1. Synchronize and verify `main` is clean.
2. Create a task branch before editing:
   - `feature/<short-name>` for product work
   - `fix/<short-name>` for defects
   - `chore/<short-name>` for maintenance
3. Implement and run the relevant tests on the task branch.
4. Commit the reviewed task branch changes.
5. Present the result and verification evidence for approval.
6. Only after explicit approval, merge with `git merge --no-ff <branch>` into `main`.
7. Delete the merged local branch after confirming the merge.

Do not merge automatically merely because tests pass. Approval and technical verification are
separate requirements.

## Required checks

Run these checks before requesting merge approval:

```bash
uv run ruff check .
uv run ty check
uv run pytest
uv run alembic check
```

Changes that affect Jupyter execution, scheduling, retry, or tracing must also run the relevant
smoke script against the local Docker services.

## Secrets and generated files

- Never commit `.env`, runtime credentials, tokens, private keys, or production endpoints.
- Keep local Jupyter workspaces and generated notebooks under the ignored
  `test_harness/jupyter/workspace/` tree.
- `.env.example` may contain only documented local placeholders.
