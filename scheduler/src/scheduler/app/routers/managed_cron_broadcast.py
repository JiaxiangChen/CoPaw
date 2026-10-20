# -*- coding: utf-8 -*-
"""新版异步分发受理与查询接口，不混入三态切换或实际执行。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .managed_cron import caller_identity
from .managed_cron_management import (
    OperationRequest,
    management_database,
    require_managed_runtime,
)
from ..services.cron.managed.contracts import (
    ManagedCronNotFound,
    ManagedCronUnavailable,
)
from ..services.cron.managed.distribution import (
    BroadcastConflict,
    BroadcastStore,
)
from ..services.cron.managed.identity import CallerIdentity

router = APIRouter(
    prefix="/scheduler/cron/jobs",
    tags=["managed-cron-broadcast"],
)


class BroadcastTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(
        min_length=1,
        max_length=128,
        description="分发接收方租户，source 继承源任务，不接受另传 scope",
    )
    tenant_name: str | None = Field(
        default=None,
        max_length=255,
        description="接收方展示姓名，不作为授权凭据",
    )
    bbk_id: str | None = Field(
        default=None,
        max_length=128,
        description="接收方分行展示编码",
    )
    agent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="接收方 Agent；省略时沿用源 Agent",
    )


class BroadcastRequest(OperationRequest):
    expected_definition_version: int = Field(
        ge=1,
        strict=True,
        description="源任务详情的 definition_version",
    )
    targets: list[BroadcastTarget] = Field(
        max_length=1000,
        description="必须显式提供 0～1000 个目标；空数组只调整全部未删除既有子任务的排程",
    )
    enable_offset: StrictBool = Field(
        default=False,
        description="是否均匀提前散列，智拓保持现状时传 false",
    )
    offset_window_hours: int = Field(
        default=0,
        ge=0,
        le=24,
        strict=True,
        description="散列窗口 0～24 小时，不在模式切换接口重复传入",
    )


def broadcast_error(exc):
    if isinstance(exc, BroadcastConflict):
        return HTTPException(409, {"code": str(exc), "task_id": exc.task_id})
    if isinstance(exc, ManagedCronNotFound):
        return HTTPException(404, "cron_broadcast_or_source_not_found")
    if isinstance(exc, ManagedCronUnavailable):
        return HTTPException(503, "cron_broadcast_storage_unavailable")
    if isinstance(exc, ValueError):
        return HTTPException(400, "invalid_cron_broadcast_request")
    return HTTPException(503, "cron_broadcast_storage_unavailable")


@router.post(
    "/{job_id}/broadcast",
    status_code=202,
    summary="受理新版异步任务分发",
    dependencies=[Depends(require_managed_runtime)],
)
async def submit_broadcast(
    job_id: str,
    body: BroadcastRequest,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """先持久化源快照、目标身份和偏移，再返回 202，不等待逐人准备完成。

    Header 使用源任务的完整身份；不接受调度模式字段，不立即执行任务。
    同次 request_id 重提复用原轮，真正失败通过明确的新一轮处理。
    """
    try:
        result = await BroadcastStore(db).submit(
            caller,
            job_id,
            body.model_dump(),
        )
        return JSONResponse(status_code=202, content=result)
    except Exception as exc:
        raise broadcast_error(exc) from exc


@router.get(
    "/{job_id}/broadcast/tasks/current",
    summary="查询最近创建的一轮分发",
)
async def current_broadcast(
    job_id: str,
    limit: int = Query(50, ge=1, le=200, description="每页目标数量，最大 200"),
    cursor: str | None = Query(
        None,
        max_length=4096,
        description="同一分发页的 next_cursor",
    ),
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """返回 task 包装或 task=null，以数据库创建顺序确定最近一轮。

    不以完成顺序选 current，不切换模式、不运行任务；分页中建议保存 task_id 查询指定分发，避免新一轮替换当前对象。
    """
    try:
        return await BroadcastStore(db).current(
            caller,
            job_id,
            limit=limit,
            cursor=cursor,
        )
    except Exception as exc:
        raise broadcast_error(exc) from exc


@router.get(
    "/{job_id}/broadcast/tasks/{task_id}",
    summary="查询指定分发及逐目标结果",
)
async def get_broadcast(
    job_id: str,
    task_id: str,
    limit: int = Query(50, ge=1, le=200, description="每页目标数量，最大 200"),
    cursor: str | None = Query(
        None,
        max_length=4096,
        description="上一页 next_cursor，保持源身份和 task_id 不变",
    ),
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """查询授权源任务的一轮分发，目标结果分页，计数始终是整轮全局计数。

    分发成功只表示定义及必要同步完成，不代表 Agent/Workflow 已执行成功；批入口必要同步失败不能显示为整轮成功。
    """
    try:
        return await BroadcastStore(db).get(
            caller,
            job_id,
            task_id,
            limit=limit,
            cursor=cursor,
        )
    except Exception as exc:
        raise broadcast_error(exc) from exc
