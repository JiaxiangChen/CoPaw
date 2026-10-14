# -*- coding: utf-8 -*-
"""Scheduler cron feedback APIs."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from ..models.cron import ExecutionSyncRequest, RecordExecutionResponse
from ..services.cron.execution_sync_service import (
    ExecutionSyncService,
    get_execution_sync_service,
)
from ..services.cron.scheduling_service import (
    CronSchedulingService,
    get_cron_scheduling_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler/cron", tags=["cron-scheduler"])


class SchedulerCronCallbackResponse(BaseModel):
    batch_id: str
    parent_job_id: str
    child_count: int = 0
    enqueued_intents: int = 0
    dispatched_intents: int = 0


@router.post("/callback", response_model=SchedulerCronCallbackResponse)
async def scheduler_parent_callback(
    request: Request,
    body: dict[str, Any] = Body(...),
    scheduling_service: CronSchedulingService = Depends(
        get_cron_scheduling_service,
    ),
) -> SchedulerCronCallbackResponse:
    """External scheduler callback for a batch parent cron job."""
    try:
        result = await scheduling_service.handle_parent_callback(
            params=body,
            headers=request.headers,
        )
        return SchedulerCronCallbackResponse.model_validate(result)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "Failed to handle scheduler parent callback: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/execution", response_model=RecordExecutionResponse)
async def record_dispatch_execution(
    request: ExecutionSyncRequest,
    sync_service: ExecutionSyncService = Depends(get_execution_sync_service),
    scheduling_service: CronSchedulingService = Depends(
        get_cron_scheduling_service,
    ),
) -> RecordExecutionResponse:
    """Validate and persist SWE results for the Scheduler's table scan."""
    dispatch_identity = _extract_dispatch_identity(request.meta)
    if dispatch_identity is None:
        logger.warning(
            "Ignored scheduler execution feedback without cron_dispatch meta: job_id=%s",
            request.job_id,
        )
        return RecordExecutionResponse(recorded=False, execution_id=None)

    try:
        accepted = await scheduling_service.handle_execution_recorded(
            execution_id=None,
            status=request.status,
            meta=request.meta,
            job_id=request.job_id,
            tenant_id=request.tenant_id,
            source_id=request.source_id,
            error_message=request.error_message,
            completed_at=request.end_time or request.actual_time,
        )
        if not accepted:
            raise RuntimeError(
                "dispatch intent was not updated from scheduler feedback",
            )
        execution_id = await sync_service.record_dispatch_execution(request)
        return RecordExecutionResponse(
            recorded=True,
            execution_id=execution_id,
        )
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "Failed to record scheduler dispatch execution: job_id=%s %s",
            request.job_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _extract_dispatch_identity(
    raw_meta: str | None,
) -> tuple[int, str, int] | None:
    if not raw_meta:
        return None
    try:
        meta = json.loads(raw_meta)
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    dispatch_meta = meta.get("cron_dispatch")
    if not isinstance(dispatch_meta, dict):
        return None
    try:
        intent_id = int(dispatch_meta.get("intent_id") or 0)
        dispatch_attempt = int(dispatch_meta.get("dispatch_attempt") or 1)
    except (TypeError, ValueError):
        return None
    batch_id = str(dispatch_meta.get("batch_id") or "").strip()
    if intent_id <= 0 or not batch_id or dispatch_attempt <= 0:
        return None
    return intent_id, batch_id, dispatch_attempt
