# -*- coding: utf-8 -*-
"""仅处理执行资源的 SWE 接口，以共用操作日志为准。"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...config.context import encode_scope_id, is_valid_identity_value
from .managed_diagnostics import format_storage_error
from .managed_resources import ResourcePreparer

router = APIRouter(
    prefix="/internal/cron/managed",
    tags=["managed-cron-resources"],
)
_KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
logger = logging.getLogger(__name__)


class ResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        description="共用管理日志中已经登记的操作 ID，SWE 据此核对权限及定义",
    )
    job_id: str = Field(
        min_length=1,
        max_length=128,
        description="该管理操作绑定的任务 ID，必须与完整 Header 身份一致",
    )


def _agent_id(request):
    agent = request.headers.get("X-Agent-Id", "default")
    if request.path_params.get("agent_id", agent) != agent:
        raise HTTPException(400, "conflicting_cron_agent")
    return agent


def _identity(request):
    actor = request.headers.get("X-User-Id", "")
    tenant = request.headers.get("X-Tenant-Id", actor)
    source = request.headers.get("X-Source-Id", "")
    agent = _agent_id(request)
    if any(
        not is_valid_identity_value(value)
        or value != value.strip()
        or len(value) > 128
        for value in (actor, tenant, source, agent)
    ):
        raise HTTPException(400, "invalid_cron_identity")
    scope = encode_scope_id(tenant, source)
    supplied = request.headers.get("X-Scope-Id")
    if supplied is not None and supplied not in {
        scope,
        f"scope.v1.{scope}",
        f"{tenant}-{source}",
    }:
        raise HTTPException(400, "conflicting_cron_scope")
    return actor, tenant, source, agent


async def _operation(request, body):
    actor, tenant, source, agent = _identity(request)
    db = getattr(request.app.state, "db_connection", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(503, "cron_database_unavailable")
    joins = " AND ".join(f"j.{key}=d.{key}" for key in _KEYS)
    state_joins = " AND ".join(f"j.{key}=s.{key}" for key in _KEYS)
    try:
        row = await db.fetch_one(
            "SELECT j.*, d.management_owner, d.creator_user_id, d.deleted_at AS job_deleted, "
            "s.job_id AS state_job_id, s.task_chat_id, s.task_session_id, s.last_status "
            f"FROM swe_cron_registration_journal j JOIN swe_cron_jobs_v2 d ON {joins} "
            f"LEFT JOIN swe_cron_job_states s ON {state_joins} "
            "WHERE j.operation_id=%s AND j.tenant_id=%s AND j.source_id=%s "
            "AND j.agent_id=%s AND j.job_id=%s",
            (body.operation_id, tenant, source, agent, body.job_id),
        )
    except Exception as exc:
        raise HTTPException(503, "cron_database_unavailable") from exc
    manager = request.headers.get("X-User-Role", "").strip().lower() in {
        "manager",
        "admin",
    }
    if not row or row["management_owner"] != "scheduler":
        raise HTTPException(404, "cron_operation_not_found")
    if tuple(row[key] for key in _KEYS) != (
        tenant,
        source,
        agent,
        body.job_id,
    ):
        raise HTTPException(404, "cron_operation_not_found")
    if not manager and actor not in {row["actor_id"], row["creator_user_id"]}:
        raise HTTPException(404, "cron_operation_not_found")
    if row["state_job_id"] is None:
        raise HTTPException(503, "cron_state_record_missing")
    return row


@asynccontextmanager
async def _locked_operation(request, body):
    _, tenant, source, agent = _identity(request)
    db = getattr(request.app.state, "db_connection", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(503, "cron_database_unavailable")
    stage = "begin_transaction"
    try:
        async with db.transaction():
            # 资源操作与逻辑删除、定义发布共用行锁，避免提前清理或绑定冲突。
            # 这里只加锁核对，不代写 Scheduler 的定义和管理字段。
            stage = "lock_job"
            await db.fetch_one(
                "SELECT job_id FROM swe_cron_jobs_v2 WHERE tenant_id=%s AND source_id=%s "
                "AND agent_id=%s AND job_id=%s FOR UPDATE",
                (tenant, source, agent, body.job_id),
            )
            stage = "validate_operation"
            row = await _operation(request, body)
            stage = "resource_action"
            yield row
            stage = "finish_transaction"
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "SWE 定时任务资源存储失败 operation_id=%s job_id=%s stage=%s "
            "error_code=cron_resource_storage_unavailable error_type=%s\n%s",
            body.operation_id,
            body.job_id,
            stage,
            type(exc).__name__,
            format_storage_error(exc),
        )
        raise HTTPException(503, "cron_resource_storage_unavailable") from exc


async def get_resource_preparer(request):
    # 先核对持久身份和操作权限，再初始化目标工作区。
    # 无效请求不能通过一次读取触发其他用户的工作区创建。
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    if workspace.agent_id != _agent_id(request):
        raise HTTPException(400, "conflicting_cron_agent")
    if workspace.chat_manager is None:
        raise HTTPException(503, "cron_chat_manager_unavailable")
    return ResourcePreparer(
        workspace.chat_manager,
        agent_id=_agent_id(request),
        workflow_store=getattr(
            request.app.state,
            "workflow_config_store",
            None,
        ),
    )


def _spec(row):
    try:
        return json.loads(row["payload_json"])["spec"]
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(503, "cron_operation_data_unavailable") from exc


@router.post("/prepare", summary="准备新版任务执行资源")
async def prepare_resources(request: Request, body: ResourceRequest):
    """仅供 Scheduler 根据共用操作日志准备配置、Chat 和会话绑定。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    Body 只含 operation_id、job_id，不接受权威 spec；先鉴权，再在事务外初始化工作区，实际资源变更仍与删除、发布互斥。不写任务文件、旧缓存、定义或平台注册状态。
    """
    service = await _initialize_resources(request, body, "prepare")
    async with _locked_operation(request, body) as row:
        return await _prepare(row, service)


def _resource_action(row, action):
    if row["status"] == "superseded":
        raise HTTPException(409, "cron_operation_superseded")
    if action == "prepare":
        if row["action"] not in {"create", "update"} or row["job_deleted"]:
            raise HTTPException(409, "cron_operation_not_preparable")
    elif row["action"] != "delete" or not row["job_deleted"]:
        raise HTTPException(409, "cron_operation_not_deleted")


async def _initialize_resources(request, body, action):
    row = await _operation(request, body)
    _resource_action(row, action)
    logger.info(
        "SWE 资源请求已鉴权 action=%s operation_id=%s job_id=%s status=%s",
        action,
        row["operation_id"],
        row["job_id"],
        row["status"],
    )
    if row["status"] == "completed":
        return None
    # 工作区初始化可能启动后台任务，必须放在业务事务外。
    # 避免新任务通过 ContextVar 继承正在使用的数据库连接和事务。
    return await get_resource_preparer(request)


async def _prepare(row, service):
    _resource_action(row, "prepare")
    if row["status"] == "completed":
        try:
            return json.loads(row["progress_json"])["prepared"]
        except (TypeError, ValueError, KeyError) as exc:
            raise HTTPException(
                503,
                "cron_operation_data_unavailable",
            ) from exc
    try:
        return await service.prepare(_spec(row), row)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "cron_resource_preparation_failed") from exc


@router.post("/cleanup", summary="清理新版任务执行资源")
async def cleanup_resources(request: Request, body: ResourceRequest):
    """仅清理已经逻辑删除任务的自身资源，不处理平台定时器或子任务。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    Body 只含 operation_id、job_id；有实际在途执行时返回 deferred，资源已不存在时幂等成功。不恢复定义、不覆盖 Scheduler 管理进度。
    """
    service = await _initialize_resources(request, body, "cleanup")
    async with _locked_operation(request, body) as row:
        return await _cleanup(request, row, service)


async def _cleanup(request, row, service):
    _resource_action(row, "cleanup")
    db = request.app.state.db_connection
    where = " AND ".join(f"{key}=%s" for key in _KEYS)
    active = await db.fetch_one(
        f"SELECT COUNT(*) AS count FROM swe_cron_execution_runs WHERE {where} AND status='running'",
        tuple(row[key] for key in _KEYS),
    )
    if active["count"]:
        return {"cleaned": False, "deferred": True}
    if row["status"] == "completed":
        return {"cleaned": True}
    try:
        return await service.cleanup(_spec(row), row)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "cron_resource_cleanup_failed") from exc
