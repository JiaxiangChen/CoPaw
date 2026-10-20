# -*- coding: utf-8 -*-
"""定时任务完成通知后台扫描器。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from ...config.context import resolve_runtime_tenant_id
from ...config.utils import load_config
from .monitor_sync_client import MonitorSyncClient, get_monitor_sync_client
from .managed_notifications import (
    load_notification_spec,
    notification_metadata,
)

logger = logging.getLogger(__name__)

SCAN_INTERVAL_ENV = "SWE_CRON_NOTIFICATION_SCAN_SECONDS"
BATCH_SIZE_ENV = "SWE_CRON_NOTIFICATION_BATCH_SIZE"
CONCURRENCY_ENV = "SWE_CRON_NOTIFICATION_CONCURRENCY"
MAX_ATTEMPTS_ENV = "SWE_CRON_NOTIFICATION_MAX_ATTEMPTS"
SOURCE_IDS_ENV = "SWE_CRON_NOTIFICATION_SOURCE_IDS"


class CronNotificationWorker:
    """从 Monitor 领取完成记录并发送定时任务完成通知。"""

    def __init__(
        self,
        *,
        multi_agent_manager: Any,
        monitor_client: Optional[MonitorSyncClient] = None,
        interval_seconds: Optional[int] = None,
        batch_size: Optional[int] = None,
        max_attempts: Optional[int] = None,
        app_timezone: Optional[str] = None,
        db: Any = None,
    ) -> None:
        self._multi_agent_manager = multi_agent_manager
        self._db = db
        self._monitor_client = monitor_client or get_monitor_sync_client()
        self._interval_seconds = interval_seconds or _get_int_env(
            SCAN_INTERVAL_ENV,
            default=300,
            minimum=300,
            maximum=600,
        )
        self._batch_size = batch_size or _get_int_env(
            BATCH_SIZE_ENV,
            default=20,
            minimum=1,
            maximum=100,
        )
        self._send_semaphore = asyncio.Semaphore(
            _get_int_env(
                CONCURRENCY_ENV,
                default=5,
                minimum=1,
                maximum=20,
            ),
        )
        self._max_attempts = max_attempts or _get_int_env(
            MAX_ATTEMPTS_ENV,
            default=3,
            minimum=1,
            maximum=10,
        )
        self._source_ids = _get_source_ids_env()
        self._app_timezone = app_timezone or _load_app_timezone()
        self._lock_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        """启动后台扫描任务。"""
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """停止后台扫描任务。"""
        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def scan_once(self) -> bool:
        """处理一批通知，返回是否可立即继续领取。"""
        now_utc = self._now_utc()
        rows = await self._monitor_client.claim_due_notifications(
            lock_owner=self._lock_owner,
            now_utc=now_utc,
            limit=self._batch_size,
            source_ids=self._source_ids,
        )
        results = await asyncio.gather(
            *(self._send_one_with_limit(row) for row in rows),
        )
        return bool(rows) and all(results)

    async def _send_one_with_limit(self, row: dict[str, Any]) -> bool:
        async with self._send_semaphore:
            try:
                return await self._send_one(row)
            except Exception:
                # 状态回写失败也只影响当前记录；取消仍由 worker 向下传播。
                logger.warning(
                    "Cron notification processing failed: execution_id=%s",
                    row.get("id"),
                    exc_info=True,
                )
                return False

    async def _run_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                if await self.scan_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Cron notification scan failed", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self._interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def _send_one(self, row: dict[str, Any]) -> bool:
        execution_id = int(row.get("id"))
        try:
            await self._send_notification(row)
            await self._monitor_client.mark_notification_sent(
                execution_id=execution_id,
                sent_at=self._now_utc(),
            )
            return True
        except Exception as exc:  # pylint: disable=broad-except
            await self._monitor_client.mark_notification_failed(
                execution_id=execution_id,
                error=repr(exc),
                max_attempts=self._max_attempts,
            )
            return False

    async def _send_notification(self, row: dict[str, Any]) -> None:
        """按执行历史的管理归属分流，不以旧仓库查不到作为新版判断。"""
        metadata = notification_metadata(row)
        job = None
        agent_id = "default"
        source_id = str(row.get("source_id") or "")
        tenant_id = str(row.get("tenant_id") or "")
        job_id = str(row.get("job_id") or "")
        if metadata.get("management_owner") == "scheduler":
            job = await load_notification_spec(self._db, row, metadata)
            agent_id = metadata["agent_id"]
            source_id = job.source_id
        runtime_tenant_id = resolve_runtime_tenant_id(
            tenant_id or None,
            source_id or None,
        )
        workspace = await self._multi_agent_manager.get_agent(
            agent_id,
            tenant_id=runtime_tenant_id,
        )
        if workspace.cron_manager is None:
            raise RuntimeError(f"cron manager unavailable: {tenant_id}")
        if job is None:
            await workspace.cron_manager.send_task_success_notification(job_id)
        else:
            await workspace.cron_manager.send_task_success_notification(
                job_id,
                job=job,
            )

    def _now_utc(self) -> datetime:
        try:
            app_tz = ZoneInfo(self._app_timezone or "UTC")
        except Exception:
            app_tz = timezone.utc
        return datetime.now(app_tz).astimezone(timezone.utc)


def _get_int_env(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _load_app_timezone() -> str:
    try:
        return load_config().user_timezone or "UTC"
    except Exception:
        return "UTC"


def _get_source_ids_env() -> list[str] | None:
    raw_value = os.environ.get(SOURCE_IDS_ENV, "")
    source_ids = []
    for item in re.split(r"[\s,]+", raw_value):
        source_id = item.strip()
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
    return source_ids or None
