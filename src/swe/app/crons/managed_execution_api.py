# -*- coding: utf-8 -*-
"""仅受理共库中 Scheduler 管理的执行记录，不接受客户端任务定义。"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .managed_api import _identity
from .managed_execution import ManagedExecutionService
from .managed_results import (
    ExecutionLeaseLost,
    ExecutionWriter,
    KEYS,
    TERMINAL,
)

router = APIRouter(
    prefix="/internal/cron/managed",
    tags=["managed-cron-execution"],
)
logger = logging.getLogger(__name__)


class ExecutionIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str = Field(
        min_length=1,
        max_length=128,
        description="Scheduler 已在共库登记的稳定执行 ID，重复调用不能更换身份重跑",
    )
    job_id: str = Field(
        min_length=1,
        max_length=128,
        description="执行记录关联的任务 ID；权威定义从执行快照读取，不在 Body 提交",
    )


class ExecuteRequest(ExecutionIdentityRequest):
    dispatch_token: str = Field(
        min_length=1,
        max_length=64,
        description="Scheduler 本次派发的共库租约标识，用于拒绝已释放或已转移的迟到请求；不是认证密钥",
    )


async def _authorized_execution(request, body):
    actor, tenant, source, agent = _identity(request)
    key = (tenant, source, agent, body.job_id)
    db = getattr(request.app.state, "db_connection", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(503, "cron_database_unavailable")
    joins = " AND ".join(f"r.{name}=d.{name}" for name in KEYS)
    clauses = " AND ".join(f"r.{name}=%s" for name in KEYS)
    definition_identity = ", ".join(
        f"d.{name} AS definition_{name}" for name in KEYS
    )
    try:
        row = await db.fetch_one(
            f"SELECT r.*, {definition_identity}, d.management_owner, d.creator_user_id "
            f"FROM swe_cron_execution_runs r JOIN swe_cron_jobs_v2 d ON {joins} "
            f"WHERE r.execution_id=%s AND {clauses}",
            (body.execution_id, *key),
        )
    except Exception as exc:
        raise HTTPException(503, "cron_database_unavailable") from exc
    if not _matching_identity(row, key):
        raise HTTPException(404, "cron_execution_not_found")
    manager = request.headers.get("X-User-Role", "").strip().lower() in {
        "manager",
        "admin",
    }
    if not manager and actor not in {row["actor_id"], row["creator_user_id"]}:
        raise HTTPException(404, "cron_execution_not_found")
    return db, key, row


def _matching_identity(row, key):
    if row is None or row["management_owner"] != "scheduler":
        return False
    return (
        tuple(row[name] for name in KEYS) == key
        and tuple(row[f"definition_{name}"] for name in KEYS) == key
    )


async def get_execution_service(request):
    from ..agent_context import get_agent_for_request
    from .executor import CronExecutor
    from .workflow.cron_adapter import WorkflowCronExecutor
    from .managed_receipts import SessionResultReceipts

    workspace = await get_agent_for_request(request)
    if workspace.agent_id != request.headers.get("X-Agent-Id", "default"):
        raise HTTPException(400, "conflicting_cron_agent")
    if workspace.runner is None:
        raise HTTPException(503, "cron_runner_unavailable")
    store = getattr(request.app.state, "workflow_config_store", None)
    workflow = (
        WorkflowCronExecutor(
            config_store=store,
            session=workspace.runner.session,
            channel_manager=workspace.channel_manager,
        )
        if store is not None
        else None
    )
    return ManagedExecutionService(
        ExecutionWriter(request.app.state.db_connection),
        CronExecutor(
            runner=workspace.runner,
            channel_manager=workspace.channel_manager,
        ),
        workflow,
        workspace_dir=workspace.workspace_dir,
        receipts=SessionResultReceipts(workspace.runner.session),
    )


def _receipt(row):
    return {
        "execution_id": row["execution_id"],
        "job_id": row["job_id"],
        "status": row["status"],
        "accepted": True,
    }


def _completed(tasks, task):
    tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        # 不输出模型、数据库或认证异常的原始文本，避免把凭据写入日志。
        logger.error(
            "后台执行结果尚未提交，请核对共库执行登记 task=%s",
            task.get_name(),
        )


def _launch(app, service, row, key, headers):
    tasks = getattr(app.state, "managed_cron_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.managed_cron_tasks = tasks
    task = asyncio.create_task(
        service.execute(row["execution_id"], key, live_headers=headers),
        name=f"managed-cron:{row['execution_id']}",
    )
    tasks.add(task)
    task.add_done_callback(lambda done: _completed(tasks, done))


@router.post("/execute", summary="受理并启动新版任务执行", status_code=202)
async def execute_managed_job(request: Request, body: ExecuteRequest):
    """仅根据共库中已登记的 execution_id、job_id 受理执行，不接受客户端提交任务定义。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    首次受理原子核对 dispatch_token 对应的有效派发租约，迟到或已转移的占位返回 409；这不是新增认证密钥。
    受理先落库再准备并启动已有执行器，202 不是最终完成回执。重复请求复用已有运行或终态，不重复启动；SWE 直接写共库结果，不向 Scheduler 回传另一份结果。
    """
    db, key, row = await _authorized_execution(request, body)
    logger.info(
        "SWE 新执行请求已鉴权 execution_id=%s job_id=%s status=%s",
        row["execution_id"],
        row["job_id"],
        row["status"],
    )
    if row["status"] in TERMINAL or row["status"] == "running":
        return JSONResponse(status_code=202, content=_receipt(row))
    try:
        row = await ExecutionWriter(db).accept(
            body.execution_id,
            key,
            dispatch_token=body.dispatch_token,
        )
        logger.info(
            "SWE 新执行已持久受理 execution_id=%s status=%s",
            row["execution_id"],
            row["status"],
        )
        if row["status"] == "accepted":
            service = await get_execution_service(request)
            _launch(request.app, service, row, key, dict(request.headers))
        return JSONResponse(status_code=202, content=_receipt(row))
    except HTTPException:
        raise
    except ExecutionLeaseLost as exc:
        logger.info(
            "拒绝已失效的派发占位 execution_id=%s job_id=%s",
            body.execution_id,
            body.job_id,
        )
        raise HTTPException(409, "cron_dispatch_lease_lost") from exc
    except Exception as exc:
        raise HTTPException(
            503,
            "cron_execution_acceptance_unavailable",
        ) from exc


@router.post("/recover", summary="恢复既有执行结果提交，不重跑业务")
async def recover_managed_result(
    request: Request,
    body: ExecutionIdentityRequest,
):
    """仅供调度恢复器按完整 Header 身份核对已受理执行的结果凭证。

    读取现有会话产物中的待提交结果并补写共库，不运行执行器；没有可恢复结果且超过运行期限时标为结果未确认的错误，不自动重跑。
    """
    db, key, row = await _authorized_execution(request, body)
    try:
        service = await get_execution_service(request)
        recovered = await service.recover(body.execution_id, key)
        if not recovered:
            await ExecutionWriter(db).expire(body.execution_id, key)
        current = await ExecutionWriter(db).get(body.execution_id, key)
        logger.info(
            "执行结果恢复核对结束 execution_id=%s status=%s recovered=%s",
            row["execution_id"],
            current["status"],
            recovered,
        )
        return {**_receipt(current), "recovered": recovered}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503,
            "cron_execution_recovery_unavailable",
        ) from exc
