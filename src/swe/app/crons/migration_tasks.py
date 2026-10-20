# -*- coding: utf-8 -*-
"""One submission scans all pages; shared tables retain bounded results."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .migration_models import (
    MigrationRequest,
    MigrationResponse,
    MigrationTaskRequest,
    MigrationTaskStatus,
)
from .migration_repository import MigrationTargetUnavailable
from .migration_service import MigrationService
from .migration_source import MigrationSourceError

logger = logging.getLogger(__name__)
TASK_TYPE = "cron.snapshot.migration"
ACTIVE_STATUSES = {"queued", "running", "stopping"}
HEARTBEAT_SECONDS = 15
STALE_SECONDS = 120
PAGE_TIMEOUT_SECONDS = 300
MAX_LOCAL_TASKS = 2
VERIFY_ISSUES = {
    "different",
    "missing",
    "source_missing",
    "source_unreadable",
    "unowned_snapshot",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _decode(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _page_key(number: int) -> str:
    return f"page:{number:012d}"


class MigrationTaskNotFound(LookupError):
    pass


class MigrationTaskCapacity(RuntimeError):
    pass


class MigrationTaskStore:
    """Use existing async tables without DDL or duplicate business state."""

    def __init__(self, db: Any):
        self.db = db

    async def check_schema(self) -> None:
        if self.db is None or not self.db.is_connected:
            raise MigrationTargetUnavailable("migration_database_unavailable")
        try:
            await self.db.execute(
                "SELECT task_id, service, task_type, status, title, "
                "target_count, done_count, failed_count, result_json, "
                "error_message, created_at, updated_at, finished_at "
                "FROM swe_async_tasks WHERE 1 = 0"
            )
            await self.db.execute(
                "SELECT task_id, target_id, status, result_json, updated_at "
                "FROM swe_async_task_items WHERE 1 = 0"
            )
        except Exception as exc:
            raise MigrationTargetUnavailable(
                "migration_progress_tables_unavailable"
            ) from exc

    async def create(
        self, payload: MigrationTaskRequest, instance_id: str
    ) -> str:
        task_id = uuid4().hex
        progress = {
            "operation": payload.operation,
            "request": payload.model_dump(),
            "instance_id": instance_id,
            "definition_scope": "visible_filesystem",
            "memory_scope": "loaded_on_execution_instance",
            "pages_completed": 0,
            "counts": {},
            "scanned_files": 0,
            "scanned_jobs": 0,
            "scanned_database_rows": 0,
            "memory_incomplete": False,
            "partial_failure": False,
            "scan_complete": False,
        }
        now = utc_now()
        await self.db.execute(
            """INSERT INTO swe_async_tasks
               (task_id, task_type, status, result_json, updated_at,
                created_at, service, title, target_count, done_count,
                failed_count)
               VALUES (%s, %s, %s, %s, %s, %s, 'swe',
                       'Cron snapshot migration', 0, 0, 0)""",
            (task_id, TASK_TYPE, "queued", json.dumps(progress), now, now),
        )
        return task_id

    async def row(self, task_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT task_id, status, result_json, updated_at, error_message "
            "FROM swe_async_tasks WHERE task_id = %s AND task_type = %s "
            "AND service = 'swe'",
            (task_id, TASK_TYPE),
        )
        if row is None:
            raise MigrationTaskNotFound("migration_task_not_found")
        return row

    async def view(
        self, task_id: str, *, after_page: int = 0, limit: int = 10
    ) -> MigrationTaskStatus:
        row = await self.row(task_id)
        progress = dict(_decode(row["result_json"]))
        rows = await self.db.fetch_all(
            "SELECT target_id, result_json FROM swe_async_task_items "
            "WHERE task_id = %s AND target_id > %s AND target_id <= %s "
            "ORDER BY target_id LIMIT %s",
            (
                task_id,
                _page_key(after_page),
                _page_key(progress["pages_completed"]),
                limit + 1,
            ),
        )
        pages = [_decode(item["result_json"]) for item in rows[:limit]]
        status = row["status"]
        if (
            status in ACTIVE_STATUSES
            and (utc_now() - row["updated_at"]).total_seconds() > STALE_SECONDS
        ):
            status = "interrupted"
        return MigrationTaskStatus(
            task_id=task_id,
            status=status,
            progress=progress,
            error=row.get("error_message"),
            pages=pages,
            next_after_page=pages[-1]["page"] if len(rows) > limit else None,
        )

    async def stop(self, task_id: str) -> None:
        await self.row(task_id)
        await self.db.execute(
            "UPDATE swe_async_tasks SET status = %s, updated_at = %s "
            "WHERE task_id = %s AND task_type = %s "
            "AND status IN ('queued', 'running')",
            ("stopping", utc_now(), task_id, TASK_TYPE),
        )

    async def running(self, task_id: str) -> None:
        await self.db.execute(
            "UPDATE swe_async_tasks SET status = %s, updated_at = %s "
            "WHERE task_id = %s AND task_type = %s AND status = 'queued'",
            ("running", utc_now(), task_id, TASK_TYPE),
        )

    async def heartbeat(self, task_id: str) -> None:
        await self.db.execute(
            "UPDATE swe_async_tasks SET updated_at = %s "
            "WHERE task_id = %s AND task_type = %s "
            "AND status IN ('queued', 'running', 'stopping')",
            (utc_now(), task_id, TASK_TYPE),
        )

    async def save_page(
        self, task_id: str, page: MigrationResponse, progress: dict[str, Any]
    ) -> None:
        number = progress["pages_completed"]
        now = utc_now()
        async with self.db.transaction():
            await self.db.execute(
                """INSERT INTO swe_async_task_items
                   (task_id, target_id, status, result_json, updated_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    task_id,
                    _page_key(number),
                    "failed" if page.partial_failure else "succeeded",
                    json.dumps({"page": number, "result": page.model_dump()}),
                    now,
                ),
            )
            await self.db.execute(
                "UPDATE swe_async_tasks SET result_json = %s, "
                "target_count = %s, done_count = %s, failed_count = %s, "
                "updated_at = %s "
                "WHERE task_id = %s AND task_type = %s",
                (
                    json.dumps(progress),
                    progress["scanned_jobs"]
                    + progress["scanned_database_rows"],
                    progress["scanned_jobs"]
                    + progress["scanned_database_rows"],
                    progress["counts"].get("failed", 0),
                    now,
                    task_id,
                    TASK_TYPE,
                ),
            )

    async def finish(
        self, task_id: str, status: str, error: str | None = None
    ) -> None:
        now = utc_now()
        await self.db.execute(
            "UPDATE swe_async_tasks SET status = %s, error_message = %s, "
            "updated_at = %s, finished_at = %s "
            "WHERE task_id = %s AND task_type = %s",
            (status, error, now, now, task_id, TASK_TYPE),
        )


class MigrationTaskRunner:
    """Workers belong to this SWE instance; only progress is shared/durable."""

    def __init__(self, store: MigrationTaskStore):
        self.store = store
        self._tasks: set[asyncio.Task] = set()

    async def submit(
        self,
        payload: MigrationTaskRequest,
        service: MigrationService,
        manager: Any,
    ) -> str:
        if len(self._tasks) >= MAX_LOCAL_TASKS:
            raise MigrationTaskCapacity("migration_task_capacity_reached")
        # Reserve before awaiting the insert, so concurrent submissions
        # cannot all pass the local concurrency limit.
        gate = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._start(gate, payload, service, manager)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            task_id = await self.store.create(payload, service.instance_id)
        except BaseException:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise
        gate.set_result(task_id)
        return task_id

    async def wait(self) -> None:
        """Drain owned workers, primarily for explicit shutdown/testing."""
        await asyncio.gather(*self._tasks)

    async def _pulse(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self.store.heartbeat(task_id)

    async def _start(self, gate, payload, service, manager) -> None:
        task_id = await gate
        pulse = asyncio.create_task(self._pulse(task_id))
        try:
            await self.store.running(task_id)
            status = await self._scan(task_id, payload, service, manager)
            await self.store.finish(task_id, status)
        except asyncio.CancelledError:
            await self._fail(
                task_id, "interrupted", "migration_worker_interrupted"
            )
            raise
        except Exception as exc:
            error = "migration_background_operation_failed"
            if isinstance(
                exc, (MigrationSourceError, MigrationTargetUnavailable)
            ):
                error = str(exc)
            elif isinstance(exc, TimeoutError):
                error = "migration_page_timeout"
            await self._fail(task_id, "failed", error)
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pulse

    async def _fail(self, task_id: str, status: str, error: str) -> None:
        try:
            await self.store.finish(task_id, status, error)
        except Exception:
            # Never report success or leak raw SQL/credentials. If the DB is
            # unavailable, the last persisted heartbeat eventually expires.
            logger.warning("Cron migration progress unavailable: %s", task_id)

    async def _scan(self, task_id, payload, service, manager) -> str:
        progress = _decode((await self.store.row(task_id))["result_json"])
        request = MigrationRequest.model_validate(
            payload.model_dump(exclude={"operation"})
        )
        while True:
            if (await self.store.row(task_id))["status"] == "stopping":
                return "stopped"
            page = await asyncio.wait_for(
                service.run(payload.operation, request, manager),
                timeout=PAGE_TIMEOUT_SECONDS,
            )
            if payload.operation == "verify":
                page = page.model_copy(
                    update={
                        "partial_failure": page.partial_failure
                        or any(
                            page.counts.get(name, 0) for name in VERIFY_ISSUES
                        )
                    }
                )
            counts = Counter(progress["counts"])
            counts.update(page.counts)
            progress["counts"] = dict(counts)
            progress["pages_completed"] += 1
            for name in (
                "scanned_files",
                "scanned_jobs",
                "scanned_database_rows",
            ):
                progress[name] += getattr(page, name)
            for name in ("memory_incomplete", "partial_failure"):
                progress[name] = progress[name] or getattr(page, name)
            progress["scan_complete"] = page.next_cursor is None
            await self.store.save_page(task_id, page, progress)
            if page.next_cursor is None:
                return (
                    "partial_failed"
                    if progress["partial_failure"]
                    else "succeeded"
                )
            request = request.model_copy(update={"cursor": page.next_cursor})
            await asyncio.sleep(0)
