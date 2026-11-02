# -*- coding: utf-8 -*-
"""Durable auth migration tasks using a transactional SQL double."""

import asyncio
import copy
import importlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from swe.app.crons.auth_migration_models import AuthMigrationRequest
from swe.app.crons.auth_migration_service import AuthMigrationService
from swe.app.crons.auth_repository import CronAuthRepository
from tests.unit.app.test_cron_auth_migration import write
from tests.unit.app.test_cron_auth_repository import FakeDatabase


class ProgressDatabase(FakeDatabase):
    def __init__(self):
        super().__init__()
        self.tasks = {}
        self.pages = {}
        self.fail_progress = False

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy((self.tasks, self.pages))
        try:
            yield
        except BaseException:
            self.tasks, self.pages = before
            raise

    async def execute(self, query, params=None):
        q = " ".join(query.split())
        if "swe_async_" not in q:
            return await super().execute(query, params)
        self.record(q, params)
        if q.startswith("SELECT"):
            return 0
        if q.startswith("INSERT INTO swe_async_tasks"):
            task_id, kind, status, result, now, created = params
            self.tasks[task_id] = {
                "task_id": task_id,
                "task_type": kind,
                "service": "swe",
                "status": status,
                "result_json": result,
                "updated_at": now,
                "error_message": None,
            }
        elif q.startswith("INSERT INTO swe_async_task_items"):
            task_id, key, status, result, now = params
            self.pages[task_id, key] = {
                "target_id": key,
                "result_json": result,
            }
        elif "SET result_json" in q:
            if self.fail_progress:
                raise RuntimeError("SQL_SECRET")
            result, target, done, failed, now, task_id, kind = params
            self.tasks[task_id].update(result_json=result, updated_at=now)
        elif "SET status = %s, error_message" in q:
            status, error, now, finished, task_id, kind = params
            self.tasks[task_id].update(
                status=status,
                error_message=error,
                updated_at=now,
            )
        elif "SET status = %s" in q:
            status, now, task_id, kind = params
            self.tasks[task_id].update(status=status, updated_at=now)
        else:
            now, task_id, kind = params
            self.tasks[task_id]["updated_at"] = now
        return 1

    async def fetch_one(self, query, params=None):
        if "FROM swe_async_tasks" not in query:
            return await super().fetch_one(query, params)
        self.record(query, params)
        row = self.tasks.get(params[0])
        return (
            copy.deepcopy(row)
            if row
            and row["task_type"] == params[1]
            and row["service"] == "swe"
            else None
        )

    async def fetch_all(self, query, params=None):
        if "swe_async_task_items" not in query:
            return await super().fetch_all(query, params)
        self.record(query, params)
        task_id, after, through, limit = params
        keys = sorted(
            k
            for k in self.pages
            if k[0] == task_id and after < k[1] <= through
        )
        return [copy.deepcopy(self.pages[k]) for k in keys[:limit]]


def module():
    spec = importlib.util.find_spec("swe.app.crons.auth_migration_tasks")
    assert spec is not None, "auth migration task runner is missing"
    return importlib.import_module(spec.name)


@pytest.mark.asyncio
async def test_submit_once_scans_both_phases_and_persists_paginated_safe_results(
    tmp_path,
):
    m = module()
    db = ProgressDatabase()
    for key in ["A", "B", "C"]:
        write(tmp_path, key, {"auth_token": "TOKEN_SECRET"})
    store = m.AuthMigrationTaskStore(db)
    await store.check_schema()
    runner = m.AuthMigrationTaskRunner(store)
    task_id = await runner.submit(
        AuthMigrationRequest(operation="import", page_size=1),
        AuthMigrationService(tmp_path, CronAuthRepository(db)),
    )
    await runner.wait()
    result = await store.view(task_id, limit=2)
    assert result.status == "succeeded"
    assert result.progress["scan_complete"] is True
    assert result.progress["counts"]["imported"] == 3
    assert result.progress["scanned_files"] == 3
    assert result.progress["scanned_database_rows"] == 3
    assert result.next_after_page == 2
    assert len(result.pages) == 2
    last = await store.view(task_id, after_page=2, limit=20)
    assert len(last.pages) == 4
    assert (
        "TOKEN_SECRET" not in result.model_dump_json() + last.model_dump_json()
    )
    assert db.tasks[task_id]["task_type"] == "cron.auth.migration"
    assert all(
        "service" in q
        for q, _ in db.calls
        if "WHERE task_id" in q and "swe_async_tasks" in q
    )


@pytest.mark.asyncio
async def test_stale_is_a_view_only_label_no_takeover(tmp_path):
    m = module()
    db = ProgressDatabase()
    store = m.AuthMigrationTaskStore(db)
    task_id = await store.create(
        AuthMigrationRequest(operation="verify"),
        "instance",
    )
    db.tasks[task_id]["updated_at"] = m.utc_now() - timedelta(seconds=121)
    result = await store.view(task_id)
    assert result.status == "interrupted"
    assert db.tasks[task_id]["status"] == "queued"
    db.tasks[task_id]["task_type"] = "cron.snapshot.migration"
    with pytest.raises(m.AuthMigrationTaskNotFound):
        await store.view(task_id)


@pytest.mark.asyncio
async def test_progress_transaction_rolls_back_on_failure(tmp_path):
    m = module()
    db = ProgressDatabase()
    write(tmp_path, "A", {})
    store = m.AuthMigrationTaskStore(db)
    runner = m.AuthMigrationTaskRunner(store)
    db.fail_progress = True
    task_id = await runner.submit(
        AuthMigrationRequest(operation="preview"),
        AuthMigrationService(tmp_path, CronAuthRepository(db)),
    )
    await runner.wait()
    result = await store.view(task_id)
    assert result.status == "failed"
    assert result.progress["pages_completed"] == 0
    assert db.pages == {}
    assert "SQL_SECRET" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_capacity_reserves_before_await_and_shutdown_cancels_owned_workers(
    tmp_path,
):
    m = module()
    db = ProgressDatabase()
    store = m.AuthMigrationTaskStore(db)
    runner = m.AuthMigrationTaskRunner(store)
    entered = asyncio.Event()

    class SlowService:
        instance_id = "local"

        async def page(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    payload = AuthMigrationRequest(operation="preview")
    ids = await asyncio.gather(
        runner.submit(payload, SlowService()),
        runner.submit(payload, SlowService()),
    )
    await entered.wait()
    with pytest.raises(m.AuthMigrationTaskCapacity):
        await runner.submit(payload, SlowService())
    await runner.close()
    assert not runner._tasks
    for task_id in ids:
        assert (await store.view(task_id)).status == "interrupted"


@pytest.mark.asyncio
async def test_heartbeat_and_page_timeout_are_bounded(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(m, "HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(m, "PAGE_TIMEOUT_SECONDS", 0.03)
    db = ProgressDatabase()
    store = m.AuthMigrationTaskStore(db)
    runner = m.AuthMigrationTaskRunner(store)

    class SlowService:
        instance_id = "local"

        async def page(self, *args, **kwargs):
            await asyncio.Event().wait()

    task_id = await runner.submit(
        AuthMigrationRequest(operation="preview"),
        SlowService(),
    )
    await runner.wait()
    result = await store.view(task_id)
    assert result.status == "failed"
    assert result.error == "auth_migration_page_timeout"
    assert any("SET updated_at" in q for q, _ in db.calls)


@pytest.mark.asyncio
async def test_progress_database_failures_are_sanitized():
    m = module()
    db = ProgressDatabase()
    db.failure = RuntimeError("RAW_SQL_SECRET")
    store = m.AuthMigrationTaskStore(db)
    from swe.app.crons.auth_repository import CronAuthRepositoryUnavailable

    with pytest.raises(CronAuthRepositoryUnavailable) as exc:
        await store.check_schema()
    assert "RAW_SQL_SECRET" not in str(exc.value)


@pytest.mark.asyncio
async def test_shutdown_stops_business_work_before_persisting_interruption(
    tmp_path,
):
    m = module()
    db = ProgressDatabase()
    entered = asyncio.Event()
    scan_stopped = asyncio.Event()

    class SlowStore(m.AuthMigrationTaskStore):
        async def finish(self, task_id, status, error=None):
            if status == "interrupted":
                assert (
                    scan_stopped.is_set()
                ), "scan must stop before terminal persistence"
            await super().finish(task_id, status, error)

    class SlowService:
        instance_id = "local"

        async def page(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                scan_stopped.set()

    store = SlowStore(db)
    runner = m.AuthMigrationTaskRunner(store)
    task_id = await runner.submit(
        AuthMigrationRequest(operation="preview"),
        SlowService(),
    )
    await entered.wait()
    await runner.close()
    assert (await store.view(task_id)).status == "interrupted"
