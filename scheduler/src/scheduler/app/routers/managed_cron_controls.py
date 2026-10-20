# -*- coding: utf-8 -*-
"""新任务独立控制和手动执行接口；不回退旧写入路径。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .managed_cron import caller_identity
from .managed_cron_management import (
    OperationRequest,
    _error,
    _response,
    get_management_service,
    management_database,
    require_managed_runtime,
)
from ..services.cron.managed.children_controls import ChildrenControlService
from ..services.cron.managed.async_batch_delete import AsyncBatchDelete
from ..services.cron.managed.contracts import ManagedCronUnavailable
from ..services.cron.managed.controls import ControlService
from ..services.cron.managed.executions import (
    ExecutionConflict,
    ExecutionRequest,
    ExecutionStore,
    execution_summary,
)
from ..services.cron.managed.identity import CallerIdentity
from ..services.cron.managed.platform import (
    ManagedPlatformClient,
    platform_config,
)
from ..services.cron.managed.repository import ManagedCronRepository

router = APIRouter(
    prefix="/scheduler/cron/jobs",
    tags=["managed-cron-controls"],
)


class EnableRequest(OperationRequest):
    enabled: StrictBool = Field(
        description="只控制指定任务自身启停，必须是 JSON 布尔值",
    )
    expected_management_version: int = Field(
        ge=1,
        strict=True,
        description="最近一次任务详情的 management_version，不是定义或运行版本",
    )


class RunRequest(OperationRequest):
    expected_definition_version: int = Field(
        ge=1,
        strict=True,
        description="本次希望手动执行的 definition_version；受理后冻结对应定义",
    )


class PriorityRequest(OperationRequest):
    expected_management_version: int = Field(
        ge=1,
        strict=True,
        description="最近一次详情或优先级查询返回的管理版本",
    )
    user_ids: list[str] = Field(
        default_factory=list,
        max_length=500,
        description="按优先顺序排列的用户 ID，最多 500 项，不得重复",
    )
    branch_ids: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="按优先顺序排列的有效一级分行编码，最多 100 项，不得重复",
    )


class SelectedChild(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(
        min_length=1,
        max_length=128,
        description="选中关联子任务的实际租户，不作为跨工作区任意操作的授权",
    )
    agent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="子任务 Agent；省略时沿用源 Agent",
    )
    job_id: str = Field(
        min_length=1,
        max_length=128,
        description="已查询到的关联子任务 ID",
    )
    expected_definition_version: int = Field(
        ge=1,
        strict=True,
        description="该子任务详情或子任务列表返回的定义版本",
    )


class ChildrenActionRequest(OperationRequest):
    action: Literal["delete", "run"]
    items: list[SelectedChild] = Field(min_length=1, max_length=1000)


class BatchDeleteChild(SelectedChild):
    agent_id: str = Field(
        min_length=1,
        max_length=128,
        description="子任务列表返回的实际 Agent，必须显式提交，不沿用或推测身份",
    )


class BatchDeleteRequest(OperationRequest):
    items: list[BatchDeleteChild] = Field(
        min_length=1,
        max_length=1000,
        description="明确选中的关联子任务及各自定义版本，空列表不代表删除全部，重复目标无效",
    )


class DeleteAllChildrenRequest(OperationRequest):
    items: list[BatchDeleteChild] = Field(
        min_length=1,
        description="用户确认时读取的完整未删除子任务快照；不包含确认后新增项",
    )


def control_error(exc):
    if isinstance(exc, ExecutionConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, ManagedCronUnavailable):
        return HTTPException(503, "cron_database_unavailable")
    return _error(exc)


def control_service(db=Depends(management_database)):
    try:
        return ControlService(db, ManagedPlatformClient(platform_config()))
    except (ValueError, RuntimeError, KeyError) as exc:
        raise HTTPException(503, "cron_control_service_unavailable") from exc


@router.put("/{job_id}/enabled", summary="设置单任务启停")
async def set_job_enabled(
    job_id: str,
    body: EnableRequest,
    request: Request,
    caller: CallerIdentity = Depends(caller_identity),
    service: ControlService = Depends(control_service),
):
    """独立设置任务自身 enabled，不把源自身停用解释为关闭整组批入口。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    使用 expected_management_version；停用跳过未开始工作，在途继续。恢复只标读当时已有结果，平台同步失败保留已写库事实和可重试进度。
    """
    if body.enabled:
        await require_managed_runtime(request, caller)
    try:
        return _response(
            await service.set_enabled(caller, job_id, body.model_dump()),
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.post("/{job_id}/read", summary="标记新版任务结果已读")
async def mark_job_read(
    job_id: str,
    body: OperationRequest,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """将本次已读边界内的已有结果标读，不恢复启用或解除暂停。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    Body 只需稳定 request_id；同次重提不移动原边界，之后完成的结果仍为未读。
    """
    try:
        return _response(
            await ControlService(db).mark_read(
                caller,
                job_id,
                body.model_dump(),
            ),
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.post(
    "/{job_id}/run",
    summary="受理新版任务手动执行",
    status_code=202,
    dependencies=[Depends(require_managed_runtime)],
)
async def run_job_now(
    job_id: str,
    body: RunRequest,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """持久保存一次手动执行身份及定义快照后返回 202，由后台派发，不代表已经开始或成功。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    使用 request_id 和 expected_definition_version；停用或运行中拒绝新请求，已有未开始手动请求则复用原 execution_id。
    """
    try:
        row, reused = await ExecutionStore(db).enqueue(
            caller,
            job_id,
            ExecutionRequest(
                "manual",
                body.request_id,
                expected_definition_version=body.expected_definition_version,
            ),
        )
        return JSONResponse(
            status_code=202,
            content=jsonable_encoder(
                {
                    "execution": execution_summary(row),
                    "reused": reused,
                },
            ),
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.get("/{job_id}/executions/{execution_id}", summary="查询同次执行状态")
async def get_execution_status(
    job_id: str,
    execution_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """从共库读取指定 execution_id 的受理、运行和结果事实，返回裸执行对象。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    execution_id 不是管理 operation_id；仅查询当前授权任务的执行，不依赖 SWE 最终完成回调。
    status 保持 SWE 主执行结果；settled_status/settled_at/settled_error_code 是 Scheduler
    观察共库异步终态后的整轮收敛事实，主执行成功并不自动表示异步任务成功。
    """
    try:
        return execution_summary(
            await ExecutionStore(db).get(caller, job_id, execution_id),
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.get("/{job_id}/batch-dispatch/priority", summary="查询批调度优先策略")
async def get_job_priority(
    job_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """读取已保存的优先用户名单、一级分行名单及 management_version。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    用户优先于分行；读取不会重排旧批次、增加接收方或修改任务。
    """
    try:
        view = await ManagedCronRepository(db).detail(caller, job_id)
        return {
            "priority": view["spec"]
            .get("meta", {})
            .get(
                "batch_dispatch_priority",
                {"user_ids": [], "branch_ids": []},
            ),
            "management_version": view["management_version"],
        }
    except Exception as exc:
        raise control_error(exc) from exc


@router.put(
    "/{job_id}/batch-dispatch/priority",
    summary="保存源任务批优先策略",
)
async def set_job_priority(
    job_id: str,
    body: PriorityRequest,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """独立修改源任务的优先策略，使用 expected_management_version 校验并发。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    用户最多 500、一级分行最多 100，名单不得重复；只供后续新批次冻结使用，不能在接收方子任务上修改整组策略。
    """
    try:
        return _response(
            await ControlService(db).set_priority(
                caller,
                job_id,
                body.model_dump(),
            ),
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.post(
    "/{job_id}/broadcast/children/actions",
    summary="批量操作选中的关联子任务",
)
async def apply_selected_child_actions(
    job_id: str,
    body: ChildrenActionRequest,
    request: Request,
    caller: CallerIdentity = Depends(caller_identity),
    management=Depends(get_management_service),
):
    """仅对明确选中的关联子任务异步删除或受理手动执行，不扩大到未选择对象。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    Header 使用源身份，Body 选择目标 tenant/Agent/job 及版本；真实操作人不变。返回逐项结果和计数，某项失败不回滚其他项，执行受理成功不等于执行完成。
    """
    await require_managed_runtime(request, caller)
    try:
        if body.action == "delete":
            items = [
                {
                    **item.model_dump(),
                    "agent_id": item.agent_id or caller.agent_id,
                }
                for item in body.items
            ]
            result = await AsyncBatchDelete(
                management.store.db,
                management,
            ).submit(
                caller,
                job_id,
                {"request_id": body.request_id, "items": items},
            )
            return JSONResponse(status_code=202, content=result)
        return await ChildrenControlService(
            management.store.db,
            management,
        ).apply(
            caller,
            job_id,
            body.model_dump(),
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.post(
    "/{job_id}/broadcast/children/batch-delete",
    status_code=202,
    summary="异步受理选中的分发子任务删除",
    dependencies=[Depends(require_managed_runtime)],
)
async def batch_delete_children(
    job_id: str,
    body: BatchDeleteRequest,
    caller: CallerIdentity = Depends(caller_identity),
    management=Depends(get_management_service),
):
    """持久保存原清单后返回202；后台校验关系、版本并删除，GET查询结果。"""
    try:
        return await AsyncBatchDelete(
            management.store.db,
            management,
        ).submit(
            caller,
            job_id,
            body.model_dump(),
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.post(
    "/{job_id}/broadcast/children/delete-all",
    status_code=202,
    summary="异步受理确认快照内的全部子任务删除",
    dependencies=[Depends(require_managed_runtime)],
)
async def delete_all_children(
    job_id: str,
    body: DeleteAllChildrenRequest,
    caller: CallerIdentity = Depends(caller_identity),
    management=Depends(get_management_service),
):
    """完整清单一次受理；关闭页面后仍处理，逐项授权不因全量而放宽。"""
    try:
        return await AsyncBatchDelete(management.store.db, management).submit(
            caller,
            job_id,
            {**body.model_dump(), "all_children": True},
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.get(
    "/{job_id}/broadcast/children/batch-delete/tasks/current",
    summary="查询最近一次子任务异步删除进度",
)
async def current_children_deletion(
    job_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    management=Depends(get_management_service),
):
    """查回最近一次异步删除；没有记录时返回null，不发起删除。"""
    try:
        return {
            "task": await AsyncBatchDelete(
                management.store.db,
                management,
            ).current(caller, job_id),
        }
    except Exception as exc:
        raise control_error(exc) from exc


@router.get(
    "/{job_id}/broadcast/children/batch-delete/tasks/{operation_id}",
    summary="查询指定子任务异步删除进度",
)
async def children_deletion_progress(
    job_id: str,
    operation_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    management=Depends(get_management_service),
):
    """只读返回固定清单的已确认结果和待处理数量，不发起删除或清理。"""
    try:
        return await AsyncBatchDelete(management.store.db, management).get(
            caller,
            job_id,
            operation_id,
        )
    except Exception as exc:
        raise control_error(exc) from exc


@router.post(
    "/{job_id}/broadcast/children/batch-delete/tasks/{operation_id}/retry",
    status_code=202,
    summary="人工续办原子任务异步删除清单",
    dependencies=[Depends(require_managed_runtime)],
)
async def retry_children_deletion(
    job_id: str,
    operation_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    management=Depends(get_management_service),
):
    """人工重新排队意外中断的原清单；不重试单项业务/清理失败。"""
    try:
        return await AsyncBatchDelete(management.store.db, management).retry(
            caller,
            job_id,
            operation_id,
        )
    except Exception as exc:
        raise control_error(exc) from exc
