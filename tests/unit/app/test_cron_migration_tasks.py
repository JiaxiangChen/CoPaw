# -*- coding: utf-8 -*-
"""Full-scan tasks retain bounded diagnostics and aggregate every page."""

import asyncio
import copy
from datetime import timedelta
from contextlib import asynccontextmanager

import pytest

from swe.app.crons.migration_models import (
    MigrationResponse,
    MigrationTaskRequest,
)
from swe.app.crons.migration_tasks import (
    MigrationTaskRunner,
    MigrationTaskStore,
    utc_now,
)
from tests.unit.app.test_cron_migration_repository import FakeDatabase


class TaskDatabase(FakeDatabase):
    """Explicit SQL double; does not prove real MySQL transaction behavior."""

    def __init__(self):
        super().__init__()
        self.tasks = {}
        self.pages = {}
        self.fail_progress = False

    @asynccontextmanager
    async def transaction(self):
        async with super().transaction():
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
        self.sql.append((q, params))
        if q.startswith("SELECT"):
            return 0
        if q.startswith("INSERT INTO swe_async_tasks"):
            task_id, kind, status, result, now, created = params
            self.tasks[task_id] = {
                "task_id": task_id,
                "task_type": kind,
                "status": status,
                "result_json": result,
                "updated_at": now,
                "error_message": None,
            }
        elif q.startswith("INSERT INTO swe_async_task_items"):
            task_id, target_id, status, result, now = params
            self.pages[(task_id, target_id)] = {
                "target_id": target_id,
                "result_json": result,
            }
        elif "SET result_json" in q:
            if self.fail_progress:
                raise RuntimeError("DO_NOT_EXPOSE")
            result, target, done, failed, now, task_id, kind = params
            self.tasks[task_id].update(result_json=result, updated_at=now)
        elif "SET status = %s, error_message" in q:
            status, error, now, finished, task_id, kind = params
            self.tasks[task_id].update(
                status=status, error_message=error, updated_at=now
            )
        elif "SET status = %s" in q:
            status, now, task_id, kind = params
            row = self.tasks.get(task_id)
            if row and row["status"] in ("queued", "running"):
                row.update(status=status, updated_at=now)
        else:
            now, task_id, kind = params
            self.tasks[task_id]["updated_at"] = now
        return 1

    async def fetch_one(self, query, params=None):
        if "FROM swe_async_tasks" not in query:
            return await super().fetch_one(query, params)
        row = self.tasks.get(params[0])
        return (
            copy.deepcopy(row)
            if row and row["task_type"] == params[1]
            else None
        )

    async def fetch_all(self, query, params=None):
        if "FROM swe_async_task_items" not in query:
            return await super().fetch_all(query, params)
        task_id, after, maximum, limit = params
        return [
            copy.deepcopy(value)
            for (key, target), value in sorted(self.pages.items())
            if key == task_id and after < target <= maximum
        ][:limit]


def page(cursor=None, *, phase="source", counts=None, incomplete=False):
    return MigrationResponse(
        operation="import",
        instance_id="instance-a",
        counts=counts or {},
        scanned_files=1,
        scanned_jobs=2,
        memory_incomplete=incomplete,
        partial_failure=False,
        next_cursor=cursor,
        items=[],
        phase=phase,
    )


class PageService:
    instance_id = "instance-a"

    def __init__(self, responses):
        self.responses = list(responses)
        self.cursors = []

    async def run(self, operation, request, manager):
        self.cursors.append(request.cursor)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_one_submission_consumes_empty_pages_and_verify_database_phase():
    db = TaskDatabase()
    service = PageService(
        [
            page(
                "next",
                counts={"inserted": 2, "memory_missing": 1},
                incomplete=True,
            ),
            page("db:audit"),
            page(phase="database", counts={"source_missing": 1}),
        ]
    )
    runner = MigrationTaskRunner(MigrationTaskStore(db))
    task_id = await runner.submit(
        MigrationTaskRequest(operation="verify"), service, None
    )
    await runner.wait()
    result = await MigrationTaskStore(db).view(task_id, after_page=0, limit=2)
    assert result.status == "partial_failed"
    assert result.progress["scan_complete"]
    assert result.progress["pages_completed"] == 3
    assert result.progress["counts"]["inserted"] == 2
    assert result.progress["counts"]["source_missing"] == 1
    assert result.progress["memory_incomplete"]
    assert service.cursors == [None, "next", "db:audit"]
    assert len(result.pages) == 2
    assert result.next_after_page == 2
    last = await MigrationTaskStore(db).view(task_id, after_page=2, limit=2)
    assert len(last.pages) == 1 and last.next_after_page is None
    assert last.pages[0]["result"]["partial_failure"]
    assert "items" not in result.progress


@pytest.mark.asyncio
async def test_partial_failure_is_accumulated_and_not_a_full_success():
    first = page("next", counts={"failed": 1})
    first.partial_failure = True
    db = TaskDatabase()
    runner = MigrationTaskRunner(MigrationTaskStore(db))
    task_id = await runner.submit(
        MigrationTaskRequest(), PageService([first, page()]), None
    )
    await runner.wait()
    result = await runner.store.view(task_id)
    assert result.status == "partial_failed"
    assert result.progress["partial_failure"]
    assert result.progress["scan_complete"]


@pytest.mark.asyncio
async def test_exception_preserves_committed_progress_and_is_sanitized():
    db = TaskDatabase()
    runner = MigrationTaskRunner(MigrationTaskStore(db))
    task_id = await runner.submit(
        MigrationTaskRequest(),
        PageService(
            [
                page("next", counts={"inserted": 2}),
                RuntimeError("DO_NOT_EXPOSE"),
            ]
        ),
        None,
    )
    await runner.wait()
    result = await runner.store.view(task_id)
    assert result.status == "failed"
    assert not result.progress["scan_complete"]
    assert result.progress["counts"]["inserted"] == 2
    assert "DO_NOT_EXPOSE" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_stop_between_pages_does_not_cancel_business_writes():
    db = TaskDatabase()
    store = MigrationTaskStore(db)
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingService(PageService):
        async def run(self, *args):
            started.set()
            await release.wait()
            return await super().run(*args)

    service = BlockingService([page("next"), page()])
    runner = MigrationTaskRunner(store)
    task_id = await runner.submit(MigrationTaskRequest(), service, None)
    await started.wait()
    await store.stop(task_id)
    release.set()
    await runner.wait()
    result = await store.view(task_id)
    assert result.status == "stopped"
    assert result.progress["pages_completed"] == 1
    assert not result.progress["scan_complete"]
    assert len(service.cursors) == 1


@pytest.mark.asyncio
async def test_expired_worker_is_reported_interrupted_from_other_store():
    db = TaskDatabase()
    task_id = await MigrationTaskStore(db).create(
        MigrationTaskRequest(), "instance-a"
    )
    db.tasks[task_id]["updated_at"] = utc_now() - timedelta(minutes=10)
    result = await MigrationTaskStore(db).view(task_id)
    assert result.status == "interrupted"
    assert result.progress["instance_id"] == "instance-a"


@pytest.mark.asyncio
async def test_progress_write_failure_never_finishes_as_success():
    db = TaskDatabase()
    runner = MigrationTaskRunner(MigrationTaskStore(db))
    db.fail_progress = True
    task_id = await runner.submit(
        MigrationTaskRequest(), PageService([page()]), None
    )
    await runner.wait()
    result = await runner.store.view(task_id)
    assert result.status == "failed"
    assert not result.progress["scan_complete"]
    assert "DO_NOT_EXPOSE" not in result.model_dump_json()
    assert not db.pages


@pytest.mark.asyncio
async def test_active_worker_has_heartbeat_and_cancellation_is_not_success(
    monkeypatch,
):
    from swe.app.crons import migration_tasks

    monkeypatch.setattr(migration_tasks, "HEARTBEAT_SECONDS", 0.001)
    db = TaskDatabase()
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowService(PageService):
        async def run(self, *args):
            entered.set()
            await release.wait()
            return page()

    runner = MigrationTaskRunner(MigrationTaskStore(db))
    task_id = await runner.submit(
        MigrationTaskRequest(), SlowService([]), None
    )
    await entered.wait()
    old = db.tasks[task_id]["updated_at"]
    await asyncio.sleep(0.02)
    assert db.tasks[task_id]["updated_at"] > old
    task = next(iter(runner._tasks))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await runner.store.view(task_id)
    assert result.status == "interrupted"
    assert not result.progress["scan_complete"]


@pytest.mark.asyncio
async def test_stopped_before_start_does_not_scan_and_final_stop_is_noop():
    db = TaskDatabase()
    store = MigrationTaskStore(db)
    service = PageService([page()])
    runner = MigrationTaskRunner(store)
    task_id = await runner.submit(MigrationTaskRequest(), service, None)
    await store.stop(task_id)
    await runner.wait()
    result = await store.view(task_id)
    assert result.status == "stopped"
    assert not service.cursors
    await store.stop(task_id)
    assert (await store.view(task_id)).status == "stopped"


@pytest.mark.asyncio
async def test_timeout_reports_failure_without_a_completed_scan(monkeypatch):
    from swe.app.crons import migration_tasks

    monkeypatch.setattr(migration_tasks, "PAGE_TIMEOUT_SECONDS", 0.001)

    class SlowService(PageService):
        async def run(self, *args):
            await asyncio.sleep(10)

    runner = MigrationTaskRunner(MigrationTaskStore(TaskDatabase()))
    task_id = await runner.submit(
        MigrationTaskRequest(), SlowService([]), None
    )
    await runner.wait()
    result = await runner.store.view(task_id)
    assert result.status == "failed"
    assert result.error == "migration_page_timeout"
    assert not result.progress["scan_complete"]


@pytest.mark.asyncio
async def test_failed_submission_releases_worker_reservation():
    class BrokenStore(MigrationTaskStore):
        async def create(self, *args):
            raise ConnectionError("insert failed")

    runner = MigrationTaskRunner(BrokenStore(TaskDatabase()))
    with pytest.raises(ConnectionError):
        await runner.submit(MigrationTaskRequest(), PageService([]), None)
    await runner.wait()
    assert not runner._tasks
