# -*- coding: utf-8 -*-
"""Batch backup API router for multi-instance management."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .batch_models import InstanceConfig, BatchTaskStatus
from .batch_service import get_batch_service

router = APIRouter(prefix="/backup/batch", tags=["backup-batch"])


# ============================================================================
# Request/Response Models
# ============================================================================


class InstanceConfigRequest(BaseModel):
    """Request to update instance configuration."""

    instances: list[InstanceConfig]


class BatchBackupRequest(BaseModel):
    """Request for batch backup."""

    instance_ids: Optional[list[str]] = Field(
        None,
        description="Specific instance IDs to backup. If None, backup all enabled instances.",
    )
    backup_date: Optional[str] = Field(
        None,
        description="Backup date (YYYY-MM-DD). Defaults to today.",
    )
    backup_hour: Optional[int] = Field(
        None,
        description="Backup hour (0-23). Defaults to current hour.",
    )


class BatchRestoreRequest(BaseModel):
    """Request for batch restore."""

    instance_ids: Optional[list[str]] = Field(
        None,
        description="Specific instance IDs to restore. If None, restore all enabled instances.",
    )
    backup_date: Optional[str] = Field(
        None,
        description="Backup date (YYYY-MM-DD). Defaults to today.",
    )
    backup_hour: Optional[int] = Field(
        None,
        description="Backup hour (0-23). Defaults to current hour.",
    )


class InstanceStatusResponse(BaseModel):
    """Instance with status."""

    id: str
    name: str
    url: str
    enabled: bool
    latest_backup: Optional[dict] = None


class InstancesListResponse(BaseModel):
    """List of instances with status."""

    instances: list[InstanceStatusResponse]
    total: int


class BatchTaskListResponse(BaseModel):
    """List of batch tasks."""

    tasks: list[dict]
    total: int


# ============================================================================
# API Endpoints
# ============================================================================


@router.get(
    "/instances",
    response_model=InstancesListResponse,
    summary="获取容器实例列表",
    description="获取所有配置的容器实例及其最新备份状态。",
)
async def list_instances() -> InstancesListResponse:
    """List all configured instances with their latest backup status."""
    service = get_batch_service()
    instances = service.get_instances()

    # Get latest backup info
    try:
        latest_backups = await service.get_latest_backups()
    except Exception:
        latest_backups = {}

    result = []
    for inst in instances:
        result.append(
            InstanceStatusResponse(
                id=inst.id,
                name=inst.name,
                url=inst.url,
                enabled=inst.enabled,
                latest_backup=latest_backups.get(inst.id),
            )
        )

    return InstancesListResponse(instances=result, total=len(result))


@router.put(
    "/instances",
    summary="更新容器实例配置",
    description="更新容器实例配置列表。",
)
async def update_instances(request: InstanceConfigRequest) -> dict:
    """Update instance configuration."""
    service = get_batch_service()
    service.save_instances(request.instances)
    return {"success": True, "message": f"Saved {len(request.instances)} instances"}


@router.post(
    "/upload",
    response_model=BatchTaskStatus,
    summary="批量备份",
    description="对多个容器实例执行备份操作。",
)
async def batch_backup(request: BatchBackupRequest) -> BatchTaskStatus:
    """Start batch backup for multiple instances."""
    service = get_batch_service()

    # Get specific instances or all enabled
    if request.instance_ids:
        all_instances = service.get_instances()
        instances = [i for i in all_instances if i.id in request.instance_ids]
        if not instances:
            raise HTTPException(status_code=400, detail="No matching instances found")
    else:
        instances = None  # Use all enabled

    try:
        task = await service.batch_backup(
            instances=instances,
            backup_date=request.backup_date,
            backup_hour=request.backup_hour,
        )
        return task
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/download",
    response_model=BatchTaskStatus,
    summary="批量恢复",
    description="对多个容器实例执行恢复操作。",
)
async def batch_restore(request: BatchRestoreRequest) -> BatchTaskStatus:
    """Start batch restore for multiple instances."""
    service = get_batch_service()

    # Get specific instances or all enabled
    if request.instance_ids:
        all_instances = service.get_instances()
        instances = [i for i in all_instances if i.id in request.instance_ids]
        if not instances:
            raise HTTPException(status_code=400, detail="No matching instances found")
    else:
        instances = None  # Use all enabled

    try:
        task = await service.batch_restore(
            instances=instances,
            backup_date=request.backup_date,
            backup_hour=request.backup_hour,
        )
        return task
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/tasks",
    response_model=BatchTaskListResponse,
    summary="获取批量任务列表",
    description="获取最近的批量备份/恢复任务列表。",
)
async def list_batch_tasks(limit: int = Query(20, ge=1, le=100)) -> BatchTaskListResponse:
    """List recent batch tasks."""
    service = get_batch_service()
    tasks = service.list_batch_tasks(limit=limit)
    return BatchTaskListResponse(
        tasks=[t.model_dump(mode="json") for t in tasks],
        total=len(tasks),
    )


@router.get(
    "/tasks/{batch_id}",
    response_model=BatchTaskStatus,
    summary="获取批量任务状态",
    description="获取指定批量任务的详细状态。",
)
async def get_batch_task(batch_id: str) -> BatchTaskStatus:
    """Get batch task status by ID."""
    service = get_batch_service()
    task = service.get_batch_task(batch_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Batch task not found")
    return task


@router.get(
    "/latest-backups",
    summary="获取各容器最新备份",
    description="获取每个容器实例的最新备份信息。",
)
async def get_latest_backups() -> dict:
    """Get latest backup info for each instance."""
    service = get_batch_service()
    return await service.get_latest_backups()