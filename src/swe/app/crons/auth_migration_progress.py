# -*- coding: utf-8 -*-
"""Durable bounded page diagnostics in existing operator-managed tables."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .auth_migration_models import AuthMigrationTaskStatus
from .auth_repository import CronAuthRepositoryUnavailable

TASK_TYPE = "cron.auth.migration"
STALE_SECONDS = 120


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _page_key(number: int) -> str:
    return f"page:{number:012d}"


def _decode(value: Any) -> dict:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


class AuthMigrationTaskNotFound(LookupError):
    pass


class AuthMigrationTaskStore:
    def __init__(self, db):
        self.db = db

    async def _query(self, method, query, params=None):
        if self.db is None or not getattr(self.db, "is_connected", False):
            raise CronAuthRepositoryUnavailable(
                "auth_migration_progress_unavailable",
            )
        try:
            return await getattr(self.db, method)(query, params)
        except Exception:
            raise CronAuthRepositoryUnavailable(
                "auth_migration_progress_unavailable",
            ) from None

    async def check_schema(self):
        await self._query(
            "execute",
            "SELECT task_id, service, task_type, status, title, target_count, "
            "done_count, failed_count, result_json, error_message, created_at, "
            "updated_at, finished_at FROM swe_async_tasks WHERE 1 = 0",
        )
        await self._query(
            "execute",
            "SELECT task_id, target_id, status, result_json, updated_at "
            "FROM swe_async_task_items WHERE 1 = 0",
        )

    async def create(self, payload, instance_id):
        task_id = uuid4().hex
        progress = {
            "operation": payload.operation,
            "request": payload.model_dump(),
            "instance_id": instance_id,
            "pages_completed": 0,
            "counts": {},
            "scanned_files": 0,
            "scanned_database_rows": 0,
            "scan_complete": False,
        }
        now = utc_now()
        await self._query(
            "execute",
            "INSERT INTO swe_async_tasks "
            "(task_id, task_type, status, result_json, updated_at, created_at, "
            "service, title, target_count, done_count, failed_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'swe', "
            "'Cron auth migration', 0, 0, 0)",
            (task_id, TASK_TYPE, "queued", json.dumps(progress), now, now),
        )
        return task_id

    async def row(self, task_id):
        row = await self._query(
            "fetch_one",
            "SELECT task_id, status, result_json, updated_at, error_message "
            "FROM swe_async_tasks WHERE task_id = %s AND task_type = %s "
            "AND service = 'swe'",
            (task_id, TASK_TYPE),
        )
        if row is None:
            raise AuthMigrationTaskNotFound("auth_migration_task_not_found")
        return row

    async def view(self, task_id, *, after_page=0, limit=10):
        row = await self.row(task_id)
        progress = _decode(row["result_json"])
        rows = await self._query(
            "fetch_all",
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
        updated = row["updated_at"]
        if updated.tzinfo is not None:
            updated = updated.astimezone(timezone.utc).replace(tzinfo=None)
        if (
            status in {"queued", "running"}
            and (utc_now() - updated).total_seconds() > STALE_SECONDS
        ):
            status = "interrupted"
        return AuthMigrationTaskStatus(
            task_id=task_id,
            status=status,
            progress=progress,
            error=row.get("error_message"),
            pages=pages,
            next_after_page=pages[-1]["page"] if len(rows) > limit else None,
        )

    async def running(self, task_id):
        await self._query(
            "execute",
            "UPDATE swe_async_tasks SET status = %s, updated_at = %s "
            "WHERE task_id = %s AND task_type = %s AND service = 'swe'",
            ("running", utc_now(), task_id, TASK_TYPE),
        )

    async def heartbeat(self, task_id):
        await self._query(
            "execute",
            "UPDATE swe_async_tasks SET updated_at = %s "
            "WHERE task_id = %s AND task_type = %s AND service = 'swe'",
            (utc_now(), task_id, TASK_TYPE),
        )

    async def save_page(self, task_id, page, progress):
        now = utc_now()
        total = progress["scanned_files"] + progress["scanned_database_rows"]
        failed = sum(
            progress["counts"].get(k, 0)
            for k in ("invalid_source", "invalid_database")
        )
        try:
            async with self.db.transaction():
                await self._query(
                    "execute",
                    "INSERT INTO swe_async_task_items "
                    "(task_id, target_id, status, result_json, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        task_id,
                        _page_key(progress["pages_completed"]),
                        "succeeded",
                        json.dumps(
                            {
                                "page": progress["pages_completed"],
                                "result": page.model_dump(),
                            },
                        ),
                        now,
                    ),
                )
                await self._query(
                    "execute",
                    "UPDATE swe_async_tasks SET result_json = %s, target_count = %s, "
                    "done_count = %s, failed_count = %s, updated_at = %s "
                    "WHERE task_id = %s AND task_type = %s AND service = 'swe'",
                    (
                        json.dumps(progress),
                        total,
                        total,
                        failed,
                        now,
                        task_id,
                        TASK_TYPE,
                    ),
                )
        except Exception:
            raise CronAuthRepositoryUnavailable(
                "auth_migration_progress_unavailable",
            ) from None

    async def finish(self, task_id, status, error=None):
        now = utc_now()
        await self._query(
            "execute",
            "UPDATE swe_async_tasks SET status = %s, error_message = %s, "
            "updated_at = %s, finished_at = %s "
            "WHERE task_id = %s AND task_type = %s AND service = 'swe'",
            (status, error, now, now, task_id, TASK_TYPE),
        )
