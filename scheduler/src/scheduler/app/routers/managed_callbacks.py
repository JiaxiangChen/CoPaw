# -*- coding: utf-8 -*-
"""新平台普通/批回调入口，校验已注册绑定后只登记工作，不执行 Agent。"""

import base64
import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .managed_cron_management import management_database
from ..services.cron.managed.contracts import ManagedCronNotFound
from ..services.cron.managed.identity import CallerIdentity
from ..services.cron.managed.scheduling import ManagedSchedulingService

router = APIRouter(prefix="/scheduler/cron", tags=["managed-cron-callbacks"])


def scheduling_service(db=Depends(management_database)):
    return ManagedSchedulingService(db)


def callback_params(body):
    encoded = body.get("jobParam") or body.get("job_param")
    if not encoded:
        return dict(body)
    try:
        if not isinstance(encoded, str) or len(encoded) > 16384:
            raise ValueError("invalid_job_param")
        params = json.loads(base64.urlsafe_b64decode(encoded))
        if not isinstance(params, dict):
            raise ValueError("invalid_job_param")
        names = {
            "scheduled_fire_at",
            "fire_time",
            "trigger_time",
            "fireTime",
            "triggerTime",
            "trigger_id",
            "logId",
            "log_id",
            "external_execution_id",
            "execution_id",
            "external_job_id",
            "jobId",
        }
        params.update(
            {key: value for key, value in body.items() if key in names},
        )
        return params
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("invalid_cron_callback_job_param") from exc


def callback_caller(headers, params):
    values = {key.lower(): value for key, value in headers.items()}
    fallback = {
        "x-user-id": params.get("fromId"),
        "x-tenant-id": params.get("tenant_id"),
        "x-source-id": params.get("source_id"),
        "x-agent-id": params.get("agent_id", "default"),
    }
    for name, value in fallback.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("invalid_cron_identity")
        if name in values and name != "x-user-id" and values[name] != value:
            raise ValueError("conflicting_cron_callback_identity")
        values.setdefault(name, value)
    caller = CallerIdentity.from_headers(values)
    scope = params.get("scopeId", params.get("scope_id"))
    if scope is not None:
        if not isinstance(scope, str):
            raise ValueError("conflicting_cron_scope")
        CallerIdentity.from_headers({**values, "x-scope-id": scope})
    return caller


async def accept_callback(request, body, kind, service):
    try:
        params = callback_params(body)
        caller = callback_caller(request.headers, params)
        result = await service.accept(caller, params, kind)
        return JSONResponse(status_code=202, content=result)
    except ManagedCronNotFound as exc:
        raise HTTPException(404, "cron_callback_job_not_found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "cron_callback_storage_unavailable") from exc


@router.post(
    "/ordinary/callback",
    status_code=202,
    summary="受理新版普通任务的平台触发",
)
async def ordinary_callback(
    request: Request,
    body: dict = Body(...),
    service: ManagedSchedulingService = Depends(scheduling_service),
):
    """校验完整身份、普通定时器绑定、操作引用和新协议标记后冻结一次独立执行。

    支持无 Header 的平铺 Body 和 jobParam 封装；fromId、tenant_id、source_id、agent_id 提供身份，scopeId 存在时校验。缺平台 ID 时读取当前数据库绑定；不接受完整任务定义。每次请求独立受理，不按流水号或计划期去重；缺时间时以接收时间记录触发，明确时间仍按原排程语义校验。任务停用、删除、归属和管理状态检查仍有效。
    """
    return await accept_callback(request, body, "ordinary", service)


@router.post(
    "/batch/callback",
    status_code=202,
    summary="受理新版批父任务的平台触发",
)
async def batch_callback(
    request: Request,
    body: dict = Body(...),
    service: ManagedSchedulingService = Depends(scheduling_service),
):
    """按已注册批绑定受理本次批次，在一个事务内冻结成员、优先级及执行快照。

    同样支持无 Header 的平铺 Body 和 jobParam 封装，身份及当前平台注册绑定须通过数据库核验。每次请求建立新批次、不去重，缺时间使用接收时间及已保存的批提前量，保留散列等待。只接入当前实际批模式对象，批暂停不受理新批；已有队列继续。只写收到的工作，不扫描补生漏掉的触发，不等待 SWE 最终执行结果。
    """
    return await accept_callback(request, body, "batch", service)
