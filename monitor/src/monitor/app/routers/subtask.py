# -*- coding: utf-8 -*-
"""Subtask API router for frontend.

Provides endpoints for:
- Creating subtask records
- Syncing subtask status from external API
- Syncing execution async_status from subtask statuses
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..models.subtask import (
    ExecutionAsyncStatusResponse,
    FailedExecutionRerunRequest,
    FailedExecutionRerunResponse,
    SubtaskCreateRequest,
    SubtaskCreateResponse,
    SubtaskSyncStatusResponse,
)
from ..services.subtask import (
    get_query_service,
    get_rerun_service,
    get_sync_service,
)
from ..services.subtask.query_service import QueryService
from ..services.subtask.rerun_service import RerunService
from ..services.subtask.sync_service import SyncService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitor/subtasks", tags=["subtask"])


@router.post("", response_model=SubtaskCreateResponse)
async def create_subtask(
    request: SubtaskCreateRequest,
    query_service: QueryService = Depends(get_query_service),
) -> SubtaskCreateResponse:
    """Create a subtask record.

    Creates a new subtask record with trace_id, task_id, filename,
    and optional fields: task_type, custuid, cust_nm,
    notification_content_wplus, notification_content_zhaohu, need_notification,
    has_main_task, status, and info. Status, when provided, must be SUC, FAIL,
    or TIMEOUT.
    The record defaults to status=NULL and info='', created_at=now(),
    updated_at=NULL.

    Args:
        request: Create request with required and optional fields
        query_service: Query service

    Returns:
        SubtaskCreateResponse with creation result
    """
    try:
        return await query_service.create_subtask(
            trace_id=request.trace_id,
            task_id=request.task_id,
            filename=request.filename,
            task_type=request.task_type,
            custuid=request.custuid,
            bbk_org_id=request.bbk_org_id,
            cust_nm=request.cust_nm,
            notification_content_wplus=request.notification_content_wplus,
            notification_content_zhaohu=request.notification_content_zhaohu,
            need_notification=request.need_notification,
            has_main_task=request.has_main_task,
            template_id=request.template_id,
            result_id=request.result_id,
            status=request.status,
            info=request.info,
        )
    except Exception as e:
        logger.error("Failed to create subtask: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync-status", response_model=SubtaskSyncStatusResponse)
async def sync_subtask_status(
    sync_service: SyncService = Depends(get_sync_service),
) -> SubtaskSyncStatusResponse:
    """Sync subtask statuses from external API.

    Scans subtasks with NULL/empty status, calls external API
    to get status, and updates database.

    Args:
        sync_service: Sync service

    Returns:
        SubtaskSyncStatusResponse with sync results
    """
    try:
        return await sync_service.sync_subtask_status()
    except Exception as e:
        logger.error("Failed to sync subtask status: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/executions/sync-async-status",
    response_model=ExecutionAsyncStatusResponse,
)
async def sync_execution_async_status(
    sync_service: SyncService = Depends(get_sync_service),
) -> ExecutionAsyncStatusResponse:
    """Sync execution async_status from subtask statuses.

    Scans executions with NULL/empty async_status, checks
    subtask statuses, and updates async_status.

    Rules:
    - No subtasks or all SUC -> async_status = 'success'
    - Any FAIL or PART_SUC -> async_status = 'error'

    Args:
        sync_service: Sync service

    Returns:
        ExecutionAsyncStatusResponse with sync results
    """
    try:
        return await sync_service.sync_execution_async_status()
    except Exception as e:
        logger.error("Failed to sync execution async_status: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/executions/rerun-failed",
    response_model=FailedExecutionRerunResponse,
)
async def rerun_failed_executions(
    request: FailedExecutionRerunRequest,
    rerun_service: RerunService = Depends(get_rerun_service),
) -> FailedExecutionRerunResponse:
    """Rerun jobs whose latest execution today failed asynchronously."""
    tenant_ids = list(dict.fromkeys(request.tenant_ids))
    try:
        return await rerun_service.rerun_failed_executions(tenant_ids)
    except Exception as e:
        logger.error("Failed to rerun async-failed executions: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
