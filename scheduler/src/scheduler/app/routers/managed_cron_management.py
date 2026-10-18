# -*- coding: utf-8 -*-
"""Scheduler 新任务增删改接口；不转发到 SWE 旧管理入口。"""

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .managed_cron import caller_identity
from ..database.connection import get_db_connection
from ..services.cron.managed.contracts import ManagedCronNotFound
from ..services.cron.managed.identity import CallerIdentity, identity_value
from ..services.cron.managed.management import (
    ManagementConflict,
    ManagementService,
)
from ..services.cron.managed.operations import (
    ManagementStore,
    operation_summary,
)
from ..services.cron.managed.platform import (
    ManagedPlatformClient,
    platform_config,
)
from ..services.cron.managed.swe_client import ManagedSweClient

router = APIRouter(
    prefix="/scheduler/cron/jobs",
    tags=["managed-cron-management"],
)


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="同一业务动作的稳定请求 ID；超时重提复用原值，新动作换新值",
    )

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value):
        return identity_value(value)


class CreateRequest(OperationRequest):
    spec: dict = Field(
        description="完整业务定义，包括名称、任务类型、排程及对应执行内容；不自行分配任务 ID",
    )
    dispatch_state: Literal["ordinary", "batch_enabled", "batch_paused"] = (
        Field(
            default="ordinary",
            description="创建时初始调度模式：普通、开启批调度或暂停批调度；后续切换使用独立接口",
        )
    )


class UpdateRequest(OperationRequest):
    spec: dict = Field(
        description="完整替换的业务定义，不是局部补丁；不通过内容更新修改任务自身启停或调度模式",
    )
    expected_definition_version: int = Field(
        ge=1,
        description="最近一次详情返回的 definition_version，不是操作进度版本",
    )


class DeleteRequest(OperationRequest):
    expected_definition_version: int = Field(
        ge=1,
        description="最近一次详情返回的定义版本；删除先落逻辑标记再清理资源",
    )


def management_database():
    try:
        return get_db_connection()
    except RuntimeError as exc:
        raise HTTPException(503, "cron_database_unavailable") from exc


async def require_managed_runtime(
    request: Request,
    _caller: CallerIdentity = Depends(caller_identity),
):
    """正常服务启动已确认后台不可用时，不再受理依赖该后台的新工作。"""
    state = request.app.state
    if getattr(state, "managed_cron_runtime_prepared", False) and not getattr(
        state,
        "managed_cron_runtime_ready",
        False,
    ):
        raise HTTPException(503, "cron_managed_runtime_not_ready")


def get_management_service(request: Request):
    try:
        db = get_db_connection()
        resources = ManagedSweClient(
            os.environ.get("SCHEDULER_SWE_API_BASE_URL", ""),
            headers=request.headers,
        )
        platform = ManagedPlatformClient(platform_config())
        return ManagementService(db, resources, platform)
    except (RuntimeError, ValueError, KeyError) as exc:
        raise HTTPException(
            503,
            "cron_management_service_unavailable",
        ) from exc


def _error(exc):
    if isinstance(exc, ManagementConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, ManagedCronNotFound):
        return HTTPException(404, "cron_job_or_operation_not_found")
    if isinstance(exc, ValueError):
        return HTTPException(400, "invalid_cron_management_request")
    return HTTPException(503, "cron_management_storage_unavailable")


def _response(result, *, created=False):
    status = 201 if created else 200
    if result["status"] == "failed":
        status = 503
    elif result["status"] == "superseded":
        status = 409
    elif result["status"] != "completed":
        status = 202
    return JSONResponse(status_code=status, content={"operation": result})


@router.post(
    "",
    summary="创建新版定时任务",
    status_code=201,
    dependencies=[Depends(require_managed_runtime)],
)
async def create_job(
    body: CreateRequest,
    caller: CallerIdentity = Depends(caller_identity),
    service: ManagementService = Depends(get_management_service),
):
    """同步创建 Scheduler 管理的新任务，并完成 SWE 资源准备、平台同步和定义发布。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    Body 包含稳定 request_id、完整 spec 和初始 dispatch_state；完成返回 201，处理中 202，失败可能已保存操作，不能直接换 ID 重建。
    """
    try:
        result = await service.create(caller, body.model_dump())
        return _response(result, created=True)
    except Exception as exc:
        raise _error(exc) from exc


@router.put(
    "/{job_id}",
    summary="更新新版定时任务定义",
    dependencies=[Depends(require_managed_runtime)],
)
async def update_job(
    job_id: str,
    body: UpdateRequest,
    caller: CallerIdentity = Depends(caller_identity),
    service: ManagementService = Depends(get_management_service),
):
    """按 expected_definition_version 完整替换业务定义，保留任务身份、资源绑定和运行结果。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    不通过本接口启停、切换模式或重新分发；源仍有未删除子任务时拒绝正文更新。成功返回 operation，失败按原操作查询和续办。
    """
    try:
        return _response(
            await service.update(caller, job_id, body.model_dump()),
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.delete("/{job_id}", summary="删除新版定时任务")
async def delete_job(
    job_id: str,
    body: DeleteRequest,
    caller: CallerIdentity = Depends(caller_identity),
    service: ManagementService = Depends(get_management_service),
):
    """先写逻辑删除，再停用平台定时器并清理自身执行资源，不自动级联子任务。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    Body 带 request_id 和 expected_definition_version；在途执行正常结束，清理等待可返回 202，503 也可能已经逻辑删除。
    """
    try:
        return _response(
            await service.delete(caller, job_id, body.model_dump()),
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/{job_id}/operations/pending", summary="查询当前未完成管理操作")
async def get_pending_operation(
    job_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """返回指定任务当前未完成的管理操作，便于刷新后找回进度。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    响应为 operation 包装，没有未完成操作时 operation=null；这不表示任务不存在，也不主动重试。
    """
    try:
        row = await ManagementStore(db).pending(caller, job_id)
        return {"operation": operation_summary(row) if row else None}
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/{job_id}/operations/{operation_id}", summary="查询指定管理操作")
async def get_operation(
    job_id: str,
    operation_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """查询有权访问任务的指定 operation_id，返回裸操作对象。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    操作进度 version 不等于任务 definition_version；查询不执行准备、平台同步或其他业务写入。
    """
    try:
        return operation_summary(
            await ManagementStore(db).get(caller, job_id, operation_id),
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.post(
    "/{job_id}/operations/{operation_id}/retry",
    summary="续办未完成管理操作",
)
async def retry_operation(
    job_id: str,
    operation_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    service: ManagementService = Depends(get_management_service),
):
    """人工续办原管理操作，仅处理未完成步骤；不更换任务、操作或平台绑定身份。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    无 Body；返回 operation 包装。已完成步骤不重复，失败启停不重置已读边界，被删除替代的旧操作不能恢复任务。
    """
    try:
        operation = await service.store.get(caller, job_id, operation_id)
        if operation["action"] == "batch_delete":
            raise ManagementConflict(
                "cron_batch_delete_requires_original_request",
            )
        if operation["action"] == "mode_sync":
            raise ManagementConflict("cron_mode_retry_requires_source")
        if operation["action"] == "dispatch_state":
            from ..services.cron.managed.dispatch_state import (
                DispatchStateService,
            )

            await DispatchStateService(
                service.store.db,
                service.platform,
            ).retry(caller, job_id, operation_id)
            return _response(
                operation_summary(
                    await service.store.get(caller, job_id, operation_id),
                ),
            )
        if operation["action"] in {"set_enabled", "auto_pause"}:
            from ..services.cron.managed.controls import ControlService

            return _response(
                await ControlService(service.store.db, service.platform).retry(
                    caller,
                    job_id,
                    operation_id,
                ),
            )
        if operation["status"] == "completed":
            return _response(operation_summary(operation))
        return _response(await service.retry(caller, job_id, operation_id))
    except Exception as exc:
        raise _error(exc) from exc
