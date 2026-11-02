# Cron Auth Database Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror every persisted cron-auth mutation to MySQL while preserving file reads and file-success semantics, with operator-triggered asynchronous migration tools.

**Architecture:** A strict repository stores one complete snapshot per actual storage scope; online mutations await a bounded best-effort repository operation after successful file persistence. Independent operator tasks scan file and database sources, persist progress in existing async-task tables, and never automatically prune or reconcile.

**Tech Stack:** Python, FastAPI, Pydantic, existing asynchronous DatabaseConnection, MySQL, pytest.

## Task 1: Repository and manual schema

Files: create `src/swe/app/crons/auth_repository.py`, `scripts/sql/cron_auth_states.sql`, `tests/unit/app/test_cron_auth_repository.py`.

- [x] Add failing tests for complete snapshot round trips, null clearing, case-sensitive storage identities, nested user_info preservation, missing schema, delete, and database page reads.
- [x] Run `PYTHONPATH=src ../../venv/bin/python -m pytest tests/unit/app/test_cron_auth_repository.py -q` and confirm missing-feature failures.
- [x] Implement `CronAuthRepository(db)` with strict async `check_schema()`, `upsert(storage_scope_id, state)`, `delete(storage_scope_id)`, `get(storage_scope_id)` and keyset `list_rows(after_scope_id, limit)`; retain optional tenant/source metadata derived from actual scope directory identity. Preserve default/legacy scope keys, UTC datetime semantics, all eight state fields and JSON-text user_info.
- [x] Add idempotent manual CREATE TABLE SQL: binary/case-sensitive storage scope primary key, optional tenant/source indexes, user_info LONGTEXT, scalar fields and timestamps. No runtime DDL, locks, version protocol or history table.
- [x] Run repository tests, then spec review, then quality review. Commit only task-owned files after checks.

Behavioral test skeleton:

```python
@pytest.mark.asyncio
async def test_snapshot_preserves_nested_user_info(repository):
    state = CronAuthState(user_info={"nested": {"items": [1, None, True]}})
    await repository.upsert("default_RMASSIST", state)
    row = await repository.get("default_RMASSIST")
    assert row["storage_scope_id"] == "default_RMASSIST"
    assert repository.state_from_row(row) == state
```

## Task 2: All online mutation entry points

Files: `src/swe/app/crons/auth_state.py`, `src/swe/app/_app.py`, `src/swe/app/routers/auth.py`, `src/swe/app/crons/manager.py`, `src/swe/app/crons/executor.py`, `src/swe/app/crons/workflow/cron_adapter.py`, `src/swe/agents/tools/shell_interceptor.py`, `src/swe/agents/tools/shell.py`, `src/swe/agents/tools/background_process.py`, `src/swe/cli/auth_cmd.py`, and corresponding existing/new unit tests.

- [x] Write regression tests for file-first persistence, mirror failure/timeout, cleanup/delete and dry-run, async call propagation and service lifecycle registration. Run and observe failures before production edits.
- [x] Use the actual resolved file path's scope directory as the repository key. Convert mutation call chains to async and await database attempts; keep loaders/file-only diagnostics synchronous and file-backed.
- [x] Register one mirror repository backed by the service's existing DB connection; ensure teardown does not retain closed connections. CLI creates/closes one connection for its async operation. Avoid nested event loops, synchronous DB calls on the main loop, and fire-and-forget mirror tasks.
- [x] Preserve public HTTP response contracts and file permission handling. Use worker threads for existing blocking I/O and external token functions. Cover foreground/background shell preparation and Workflow secret-loader awaitability.
- [x] Mirror delete only after successful file removal. dry-run writes neither store. Catch database failures with sanitized operation/scope diagnostics and bounded wait; file failure does not change DB. Cancellation remains cancellation, not success.
- [x] Run auth-state, CLI, workflow, cron executor/prefetch and shell/background test slices. Perform spec review then quality review; fix issues and commit task-owned files.

Behavioral test:

```python
@pytest.mark.asyncio
async def test_database_failure_keeps_successful_file_write(tmp_path, monkeypatch):
    # Inject an unavailable repository into the online mirror registration.
    state = CronAuthState(user_info={"id": 1})
    path = await save_cron_auth_state(state, tenant_id="tenant-a")
    assert path.is_file()
    assert load_cron_auth_state(tenant_id="tenant-a") == state
```

## Task 3: Operator-triggered migration tasks

Files: create focused `src/swe/app/crons/auth_migration*.py` modules for models/service/tasks/API; register the router and lifecycle in the existing application; create `tests/unit/app/test_cron_auth_migration.py`, API/task tests, and `docs/deploy/cron-auth-database-mirror.md`.

- [x] Test preview/import/verify/prune, repeated import, invalid file protection, DB-only rows, inaccessible roots, explicit prune scope validation, asynchronous submit/query progress and safe diagnostics; observe failures first.
- [x] Implement `POST /api/internal/cron/auth-migration/tasks` (202) and `GET .../tasks/{task_id}` with existing internal-access checks. Operations: preview/import/verify/prune. Filters: tenant_ids, source_ids, storage_scope_ids. Prune requires explicit range and rechecks current file absence; missing/unreadable mounts and invalid files do not authorize deletion.
- [x] Scan both storage and database in bounded pages, without initializing Workspaces, refreshing credentials or writing source files. Import upserts complete current snapshots; default modes never prune. No locks or automatic retries/reconciliation.
- [x] Persist task progress using the existing async-task tables with independent task type. Source/database failure and per-record issues appear in task results without raw secrets, SQL errors or user_info values. A process-local runner owns execution; interrupted runs can be manually resubmitted, not automatically taken over.
- [x] Document SQL deployment, small-scope preview/import/verify, explicit prune, task status interpretation and operational responsibility for pre-cutover verification; no read switch or automatic cutover.
- [x] Run all new tests and affected test slices, Black/pre-commit checks on changed files, final independent reviews. Record actual check results and remaining environmental limits. Keep unrelated main-workspace changes untouched.

## Final validation

- [x] Spec coverage: mirror fields and clearing, all mutation paths, file reads unchanged, delete/dry-run, no-lock semantics, DB best effort, manual SQL, all four operations, async progress, operator-only repair, no read cutover.
- [x] Run `PYTHONPATH=src ../../venv/bin/python -m pytest` on the affected subsystem/test list selected during implementation.
- [x] Run `git diff --check` and formatting hooks for changed files.
- [x] Report the isolated worktree/branch, exact tests and whether real MySQL was exercised. Do not deploy or merge without an integration instruction.

## Verification record

- Task 1: 16 repository tests pass; schema and repository independently reviewed.
- Task 2: commits `0689f68c0` and `8bb69648a`; specification and quality reviews approved. Controller reran the 22-file affected suite: 471 passed in 24.34s. Unchanged file-reader tests also pass.
- Task 2 hooks: all hooks pass on the six lifecycle/cleanup follow-up files. The initial broader run skips only mypy/pylint after reproducing existing manager return, background process Popen/statement-count and workflow-test lambda diagnostics on pre-task copies. No claim that repository-wide lint is clean.
- No real MySQL instance has been exercised; SQL deployment remains operator-managed.

- Task 3: commits `561c5ea64` and `304bf7577`; specification review approved. 41 migration tests pass, including strict JSON semantic comparison, unsafe/invalid sources, task progress and shutdown. All task-owned hooks pass without skips.
- Final controller regression after `af94d0824`: 539 tests across 27 affected files passed in 24.72s; branch-wide `git diff 93aee5b90 --check` is clean. New operator, ADR and playbook local links resolve. Final code-quality/integration review approved, independently verifying all 41 migration tests. The depth-300 source serialization regression is isolated per record; healthy later records continue.

- Delivery: isolated branch `feat/cron-auth-database-mirror` in `.worktrees/cron-auth-db-mirror`; no merge, deployment, DDL execution or business read cutover performed.
