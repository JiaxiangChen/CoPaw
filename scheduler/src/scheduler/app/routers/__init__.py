# -*- coding: utf-8 -*-
"""Scheduler routers."""

from fastapi import APIRouter

from .cron import router as cron_router
from .batch_operations import router as batch_operations_router
from .batch_run_state import router as batch_run_state_router
from .managed_cron import router as managed_cron_router
from .managed_callbacks import router as managed_callbacks_router
from .managed_batch_operations import router as managed_batch_operations_router
from .managed_cron_management import router as managed_cron_management_router
from .managed_cron_controls import router as managed_cron_controls_router
from .managed_cron_broadcast import router as managed_cron_broadcast_router
from .managed_cron_dispatch_state import (
    router as managed_cron_dispatch_state_router,
)

api_router = APIRouter()
api_router.include_router(cron_router)
api_router.include_router(batch_operations_router)
api_router.include_router(batch_run_state_router)
api_router.include_router(managed_cron_router)
api_router.include_router(managed_cron_management_router)
api_router.include_router(managed_cron_controls_router)
api_router.include_router(managed_cron_broadcast_router)
api_router.include_router(managed_cron_dispatch_state_router)
api_router.include_router(managed_callbacks_router)
api_router.include_router(managed_batch_operations_router)

__all__ = ["api_router"]
