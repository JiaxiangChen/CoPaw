# -*- coding: utf-8 -*-
from fastapi import APIRouter

from .health import router as health_router
from .sync import router as sync_router
from .cron import router as cron_router
from .cron_branch_export import router as cron_branch_export_router
from .external import router as external_router
from .tracing import router as tracing_router
from .warmup import router as warmup_router
from .subtask import router as subtask_router
from .async_tasks import router as async_tasks_router
from .high_frequency_question import router as high_frequency_question_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(sync_router, tags=["sync"])
api_router.include_router(cron_router, tags=["cron"])
api_router.include_router(cron_branch_export_router)
api_router.include_router(external_router)
api_router.include_router(tracing_router, tags=["tracing"])
# 暴露手动预热和状态查询入口，便于自动恢复失败后人工补跑。
api_router.include_router(warmup_router, tags=["warmup"])
api_router.include_router(subtask_router, tags=["subtask"])
api_router.include_router(async_tasks_router, tags=["async-tasks"])
api_router.include_router(
    high_frequency_question_router,
    tags=["high-frequency-question"],
)
