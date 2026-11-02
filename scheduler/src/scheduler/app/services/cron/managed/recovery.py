# -*- coding: utf-8 -*-
"""按共库登记恢复新链路工作，不扫描定义补生平台未送达的触发。"""

import asyncio
import json
import logging

from .dispatcher import ExecutionDispatcher
from .dispatch_state import DispatchStateService
from .distribution import TASKS
from .distribution_worker import BroadcastProcessor
from .executions import RUN_TABLE
from .identity import CallerIdentity
from .management import ManagementService
from .operations import IDENTITY_FIELDS, JOURNAL, utc_now
from .work_query import ManagedWorkQuery
from .auto_pause import AutoPauseController
from .settlement import ExecutionSettlement
from .run_accounting import occupied_run_sql
from .async_batch_delete import AsyncBatchDelete
from .daily_retry_service import DailyFailureRetry
from .auto_retry import AutoBatchRetry

logger = logging.getLogger(__name__)


class ManagedRecoveryWorker:
    def __init__(
        self,
        db,
        resources,
        platform,
        execution_client,
        *,
        limit=32,
        capacity=None,
    ):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_managed_recovery_limit")
        self.db = db
        self.limit = limit
        self.dispatcher = ExecutionDispatcher(
            db,
            execution_client,
            capacity=capacity,
        )
        self.broadcast = BroadcastProcessor(db, resources, platform)
        self.modes = DispatchStateService(db, platform)
        self.management = ManagementService(db, resources, platform)
        self.deletions = AsyncBatchDelete(db, self.management)
        self.work = ManagedWorkQuery(db, limit)
        self.auto_pause = AutoPauseController(db, platform, limit=limit)
        self.settlement = ExecutionSettlement(db, limit=limit)
        self.daily_retry = DailyFailureRetry(db, page_size=min(limit, 100))
        self.auto_retry = AutoBatchRetry(db, limit=limit)

    async def tick(self):
        """执行一次恢复巡检，分开报告各类已有工作的处理数量。"""
        return dict(
            await asyncio.gather(
                self._isolated("auto_pause", self.auto_pause.tick),
                self._isolated("settled", self.settlement.tick),
                self._isolated("retried", self.auto_retry.tick),
                self._isolated("broadcasts", self._broadcasts),
                self._isolated("deletions", self._deletions),
                self._isolated("modes", self._modes),
                self._isolated("cleanup", self._cleanup),
                self._isolated("dispatched", self._dispatch),
                self._isolated("recovered", self._recover),
                self._isolated("daily_failure_retry", self.daily_retry.tick),
            ),
        )

    @staticmethod
    async def _isolated(name, callback):
        try:
            return name, await callback()
        except Exception as exc:
            # 一个工作域的临时异常不能阻断其他执行或泄漏外部异常原文。
            logger.warning(
                "新链路恢复巡检暂未完成 lane=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            return name, 0

    async def _broadcasts(self):
        rows = await self.db.fetch_all(
            f"SELECT task_id FROM {TASKS} WHERE status IN ('queued','running') "
            "AND (lease_until IS NULL OR lease_until<=%s) ORDER BY created_order LIMIT %s",
            (utc_now().isoformat(" "), self.limit),
        )
        for row in rows:
            await self.broadcast.process(row["task_id"])
        return len(rows)

    async def _deletions(self):
        rows = await self.db.fetch_all(
            f"SELECT operation_id FROM {JOURNAL} WHERE action='batch_delete' "
            "AND status IN ('pending','running') AND (lease_until IS NULL OR lease_until<=%s) "
            "ORDER BY created_at, operation_id LIMIT %s",
            (utc_now().isoformat(" "), self.limit),
        )
        for row in rows:
            await self.deletions.process(row["operation_id"])
        return len(rows)

    async def _modes(self):
        rows = await self.db.fetch_all(
            f"SELECT operation_id FROM {JOURNAL} WHERE action='dispatch_state' "
            "AND status IN ('pending','running') AND (lease_until IS NULL OR lease_until<=%s) "
            "ORDER BY created_at, operation_id LIMIT %s",
            (utc_now().isoformat(" "), self.limit),
        )
        for row in rows:
            await self.modes.process(row["operation_id"])
        return len(rows)

    async def _cleanup(self):
        joins = " AND ".join(f"r.{key}=j.{key}" for key in IDENTITY_FIELDS)
        rows = await self.db.fetch_all(
            f"SELECT j.* FROM {JOURNAL} j WHERE j.action='delete' "
            "AND j.status='pending' AND j.stage='cleanup_waiting' "
            "AND (j.lease_until IS NULL OR j.lease_until<=%s) "
            f"AND NOT EXISTS (SELECT 1 FROM {RUN_TABLE} r WHERE {joins} AND {occupied_run_sql('r')}) "
            "ORDER BY j.updated_at, j.operation_id LIMIT %s",
            (utc_now().isoformat(" "), self.limit),
        )
        for row in rows:
            payload = json.loads(row["payload_json"])
            caller = CallerIdentity(
                row["tenant_id"],
                row["source_id"],
                row["agent_id"],
                row["actor_id"],
                bool(payload.get("actor_is_manager", False)),
            )
            await self.management.retry(
                caller,
                row["job_id"],
                row["operation_id"],
            )
        return len(rows)

    async def _execution_rows(self, *, running, scope=None):
        return await self.work.rows(running=running, scope=scope)

    async def _dispatch(self):
        checked = 0
        for scope in await self.work.scopes():
            try:
                checked += await self._dispatch_scope(scope)
            except Exception as exc:
                logger.warning(
                    "模型池本轮未完成，其他池继续 source_id=%s provider_id=%s model_id=%s error_type=%s",
                    scope["source_id"],
                    scope["provider_id"],
                    scope["model_id"],
                    type(exc).__name__,
                )
        return checked

    async def _dispatch_scope(self, scope):
        permit = None
        if self.dispatcher.capacity is not None:
            permit = await self.dispatcher.capacity.prepare(scope)
            if permit is None:
                return 0
        rows = await self._execution_rows(running=False, scope=scope)
        for row in rows:
            await self.dispatcher.dispatch_registered(
                row["execution_id"],
                capacity_permit=permit,
            )
        return len(rows)

    async def _recover(self):
        rows = await self._execution_rows(running=True)
        for row in rows:
            await self.dispatcher.recover_registered(row["execution_id"])
        return len(rows)

    async def run_loop(self, *, stop_event, interval_seconds=2):
        """各恢复工作域独立循环，长分发不阻塞执行派发和结果恢复。"""
        if interval_seconds <= 0:
            raise ValueError("invalid_managed_recovery_interval")
        lanes = (
            self.auto_pause.tick,
            self.settlement.tick,
            self.auto_retry.tick,
            self._broadcasts,
            self._deletions,
            self._modes,
            self._cleanup,
            self._dispatch,
            self._recover,
            self.daily_retry.tick,
        )
        tasks = [
            asyncio.create_task(
                self._run_lane(lane, stop_event, interval_seconds),
                name=f"managed-recovery:{lane.__name__}",
            )
            for lane in lanes
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_lane(self, lane, stop_event, interval):
        while not stop_event.is_set():
            await self._isolated(lane.__name__, lane)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
