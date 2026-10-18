# -*- coding: utf-8 -*-
"""装配新版持久后台；先检查结构，停止后台后才允许外层关闭共库。"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from ....database.connection import get_db_connection
from ..capacity_claim import enable_managed_capacity_accounting
from ..scheduling_service import get_cron_scheduling_service
from .capacity import ManagedCapacityGate
from .dispatcher import ManagedSweExecutionClient
from .platform import ManagedPlatformClient, platform_config
from .recovery import ManagedRecoveryWorker
from .schema import assert_managed_schema_ready
from .swe_client import ManagedSweClient
from .feedback import ManagedCapacityFeedback
from .board_schema import assert_managed_board_schema_ready

logger = logging.getLogger(__name__)


async def create_managed_worker(db):
    await assert_managed_schema_ready(db)
    # 在启动旧循环之前启用合并统计，即使后续新客户端配置失败，
    # 已有新运行占位也不能被旧循环忽略。这里没有新增环境开关。
    enable_managed_capacity_accounting()
    legacy = get_cron_scheduling_service()
    legacy.bind_managed_feedback(ManagedCapacityFeedback(db))
    await assert_managed_board_schema_ready(db)
    base = os.environ.get("SCHEDULER_SWE_API_BASE_URL", "")
    resources = ManagedSweClient(base)
    execution = ManagedSweExecutionClient(base)
    platform = ManagedPlatformClient(platform_config())
    return ManagedRecoveryWorker(
        db,
        resources,
        platform,
        execution,
        capacity=ManagedCapacityGate(legacy),
    )


async def prepare_managed_runtime(app):
    """在旧循环启动前只准备一次，结构或配置不足时保留旧启动行为。"""
    app.state.managed_cron_runtime_prepared = True
    app.state.managed_cron_runtime_ready = False
    app.state.managed_cron_worker = None
    try:
        db = get_db_connection()
        if not db.is_connected:
            return
        app.state.managed_cron_worker = await create_managed_worker(db)
    except Exception as exc:
        logger.warning(
            "新版定时后台未启动，旧链路保持原入口 table=%s error_type=%s",
            getattr(exc, "table", None),
            type(exc).__name__,
        )


def _completed(app, task):
    app.state.managed_cron_runtime_ready = False
    if not task.cancelled() and task.exception() is not None:
        logger.error("新版定时后台异常退出，需要核对服务日志和共库登记")


async def _stop(task, stop_event):
    stop_event.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.info("新版定时后台已取消，未完成工作保留持久租约供恢复")
    except asyncio.CancelledError:
        child_was_cancelled = task.cancelled()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not child_was_cancelled:
            raise
        logger.info("新版后台此前已取消，继续完成外层资源关闭")
    except Exception as exc:
        logger.warning(
            "新版后台停止时存在未确认状态 error_type=%s",
            type(exc).__name__,
        )


@asynccontextmanager
async def managed_lifespan(app):
    if not getattr(app.state, "managed_cron_runtime_prepared", False):
        await prepare_managed_runtime(app)
    worker = app.state.managed_cron_worker
    if worker is None:
        yield
        return
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        worker.run_loop(stop_event=stop_event),
        name="managed-cron-service",
    )
    app.state.managed_cron_task = task
    app.state.managed_cron_stop_event = stop_event
    app.state.managed_cron_runtime_ready = True
    task.add_done_callback(lambda done: _completed(app, done))
    logger.info("新版定时后台已启动，只恢复已受理的新协议工作")
    try:
        yield
    finally:
        await _stop(task, stop_event)
        app.state.managed_cron_runtime_ready = False
        logger.info("新版定时后台已停止")
