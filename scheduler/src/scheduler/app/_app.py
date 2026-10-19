# -*- coding: utf-8 -*-
"""Cron Scheduler FastAPI app."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import (
    close_db_connection,
    init_db_connection,
)

from ..__version__ import __version__
from ..logging import configure_logging
from ..config.constant import (
    DB_HOST,
    DOCS_ENABLED,
    ENV_NAME,
    get_scheduler_database_config,
)
from .routers import api_router
from .services.cron.scheduling_service import (
    cron_scheduling_runtime_enabled,
    get_cron_scheduling_service,
)
from .services.cron.batch_run_state import (
    assert_run_state_schema_ready,
    initialize_missing_controls,
)
from .services.cron.managed.runtime import (
    managed_lifespan,
    prepare_managed_runtime,
)

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Scheduler application lifecycle."""
    logger.info("Cron Scheduler service starting up")
    logger.info("Environment: %s", ENV_NAME)

    db_initialized = False
    if DB_HOST:
        try:
            db = await init_db_connection(get_scheduler_database_config())
            if cron_scheduling_runtime_enabled():
                await assert_run_state_schema_ready(db)
                await initialize_missing_controls(db)
            db_initialized = True
            logger.info("Scheduler database initialized successfully")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Scheduler database initialization failed: %s", exc)
    else:
        logger.info(
            "Database not configured " "(SCHEDULER_DB_HOST not set)",
        )

    # 新占位合并统计先于旧循环启动，避免重启时旧领取忽略已有新执行。
    await prepare_managed_runtime(fastapi_app)

    scheduler_stop_event: asyncio.Event | None = None
    scheduler_task: asyncio.Task | None = None
    if db_initialized and cron_scheduling_runtime_enabled():
        scheduler_stop_event = asyncio.Event()
        scheduler_task = asyncio.create_task(
            get_cron_scheduling_service().run_loop(
                stop_event=scheduler_stop_event,
            ),
            name="cron-scheduler-service",
        )
        fastapi_app.state.scheduler_task = scheduler_task
        fastapi_app.state.scheduler_stop_event = scheduler_stop_event
        logger.info("Cron Scheduler loop started")
    elif DB_HOST and not db_initialized and cron_scheduling_runtime_enabled():
        logger.warning(
            "Cron Scheduler loop disabled because database initialization failed",
        )
    else:
        logger.info("Cron Scheduler loop disabled")

    try:
        yield
    finally:
        if scheduler_task is not None:
            try:
                if scheduler_stop_event is not None:
                    scheduler_stop_event.set()
                await asyncio.wait_for(scheduler_task, timeout=10)
                logger.info("Cron Scheduler loop stopped")
            except asyncio.TimeoutError:
                scheduler_task.cancel()
                await asyncio.gather(scheduler_task, return_exceptions=True)
                logger.warning("Cron Scheduler loop cancelled")
            except asyncio.CancelledError:
                logger.info("旧调度循环此前已取消，继续关闭共库连接")
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Failed to stop Cron Scheduler loop: %s", exc)

        await _close_database()


async def _close_database():
    """关闭共库连接，与后台收尾分开，异常不暴露连接信息。"""
    if not DB_HOST:
        return
    try:
        await close_db_connection()
        logger.info("Scheduler 数据库连接已关闭")
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "Scheduler 数据库关闭未确认 error_type=%s",
            type(exc).__name__,
        )


@asynccontextmanager
async def application_lifespan(fastapi_app: FastAPI):
    """共用已有数据库生命周期，新后台先停止，随后关闭旧循环和连接池。"""
    async with lifespan(fastapi_app):
        async with managed_lifespan(fastapi_app):
            yield


app = FastAPI(
    title="Cron Scheduler",
    description="Dispatch-managed scheduled run scheduler",
    version=__version__,
    lifespan=application_lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

app.include_router(api_router, prefix="/api")
