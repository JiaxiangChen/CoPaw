# -*- coding: utf-8 -*-
"""Instance-owned workers; durable progress never implies automatic takeover."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from contextlib import suppress

from .auth_migration_progress import (
    AuthMigrationTaskNotFound,
    AuthMigrationTaskStore,
    _decode,
    utc_now,
)
from .auth_migration_source import AuthMigrationSourceUnavailable
from .auth_repository import CronAuthRepositoryUnavailable

logger = logging.getLogger(__name__)
MAX_LOCAL_TASKS = 2
HEARTBEAT_SECONDS = 15
PAGE_TIMEOUT_SECONDS = 300
FAILURE_TIMEOUT_SECONDS = 5
SHUTDOWN_TIMEOUT_SECONDS = 10
ISSUES = {
    "invalid_source",
    "invalid_database",
    "missing",
    "different",
    "database_only",
}


class AuthMigrationTaskCapacity(RuntimeError):
    pass


class AuthMigrationTaskRunner:
    def __init__(self, store: AuthMigrationTaskStore):
        self.store = store
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    async def submit(self, payload, service):
        if self._closing or len(self._tasks) >= MAX_LOCAL_TASKS:
            raise AuthMigrationTaskCapacity(
                "auth_migration_task_capacity_reached",
            )
        gate = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._start(gate, payload, service))
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

    async def wait(self):
        await asyncio.gather(*self._tasks)

    async def close(self):
        self._closing = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    SHUTDOWN_TIMEOUT_SECONDS,
                )

    async def _pulse(self, task_id):
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await self.store.heartbeat(task_id)

    async def _start(self, gate, payload, service):
        task_id = await gate
        pulse = asyncio.create_task(self._pulse(task_id))
        scan = None
        try:
            await self.store.running(task_id)
            scan = asyncio.create_task(self._scan(task_id, payload, service))
            done, _ = await asyncio.wait(
                {scan, pulse},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if pulse in done:
                pulse.result()
            status = await scan
            await self.store.finish(task_id, status)
        except asyncio.CancelledError:
            await self._cancel_children(scan, pulse)
            await self._fail(
                task_id,
                "interrupted",
                "auth_migration_worker_interrupted",
            )
            raise
        except Exception as exc:
            await self._cancel_children(scan, pulse)
            error = "auth_migration_operation_failed"
            if isinstance(exc, AuthMigrationSourceUnavailable):
                error = "auth_migration_source_unavailable"
            elif isinstance(exc, CronAuthRepositoryUnavailable):
                error = "auth_migration_database_unavailable"
            elif isinstance(exc, TimeoutError):
                error = "auth_migration_page_timeout"
            await self._fail(task_id, "failed", error)
        finally:
            await self._cancel_children(scan, pulse)

    @staticmethod
    async def _cancel_children(*tasks):
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await task

    async def _fail(self, task_id, status, error):
        try:
            await asyncio.wait_for(
                self.store.finish(task_id, status, error),
                FAILURE_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning(
                "Cron auth migration progress unavailable: %s",
                task_id,
            )

    async def _scan(self, task_id, payload, service):
        progress = _decode((await self.store.row(task_id))["result_json"])
        phase, after = (
            "database" if payload.operation == "prune" else "files"
        ), None
        while True:
            page = await asyncio.wait_for(
                service.page(payload, phase=phase, after=after),
                PAGE_TIMEOUT_SECONDS,
            )
            counts = Counter(progress["counts"])
            counts.update(page.counts)
            progress["counts"] = dict(counts)
            progress["pages_completed"] += 1
            for name in ("scanned_files", "scanned_database_rows"):
                progress[name] += getattr(page, name)
            progress["scan_complete"] = page.scan_complete
            await self.store.save_page(task_id, page, progress)
            if page.scan_complete:
                return (
                    "partial_failed"
                    if any(counts[k] for k in ISSUES)
                    else "succeeded"
                )
            phase, after = page.next_phase, page.next_after
            await asyncio.sleep(0)
