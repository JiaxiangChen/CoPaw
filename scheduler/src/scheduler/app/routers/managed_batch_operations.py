# -*- coding: utf-8 -*-
"""新版批次失败预览和人工重试，与旧 Intent 操作完全隔离。"""

import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
)

from .managed_cron import caller_identity
from .managed_cron_management import (
    management_database,
    require_managed_runtime,
)
from ..services.cron.managed.batch_retry import (
    BatchRetryConflict,
    BatchRetryService,
    ManagedBatchRetryRequest,
)
from ..services.cron.managed.identity import CallerIdentity

router = APIRouter(
    prefix="/scheduler/cron/managed-dispatch/batches",
    tags=["managed-batch-operations"],
)
logger = logging.getLogger(__name__)


def batch_service(db=Depends(management_database)):
    return BatchRetryService(db)


def batch_error(exc):
    if isinstance(exc, PermissionError):
        return HTTPException(403, "cron_batch_manager_required")
    if isinstance(exc, LookupError):
        return HTTPException(404, "cron_batch_or_member_not_found")
    if isinstance(exc, BatchRetryConflict):
        return HTTPException(409, "cron_retry_request_content_conflict")
    if isinstance(exc, ValueError):
        return HTTPException(400, "invalid_cron_batch_request")
    return HTTPException(503, "cron_batch_storage_unavailable")


@router.get("/{batch_id}/failures", summary="查看新版批次当前失败成员及分类")
async def failures(
    batch_id: str,
    types: list[str] = Query(default=[]),
    caller: CallerIdentity = Depends(caller_identity),
    service=Depends(batch_service),
):
    """仅可信 manager/admin 查询本来源；统计整批，预览最多 200 个匹配的当前尝试。"""
    try:
        return await service.preview(caller, batch_id, types)
    except Exception as exc:
        raise batch_error(exc) from exc


@router.post(
    "/{batch_id}/retry",
    summary="人工重试新版批次失败成员",
    dependencies=[Depends(require_managed_runtime)],
)
async def retry(
    batch_id: str,
    body: ManagedBatchRetryRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    caller: CallerIdentity = Depends(caller_identity),
    service=Depends(batch_service),
):
    """共库事务受理、审计并返回；不调用平台或 SWE，不改变批入口模式。响应丢失重提原 Body。"""
    try:
        result = await service.retry(caller, batch_id, body)
        worker = getattr(request.app.state, "managed_cron_worker", None)
        if worker is not None:
            background_tasks.add_task(
                _dispatch_urgent,
                service.db,
                worker.dispatcher,
                result["results"],
            )
        return result
    except Exception as exc:
        raise batch_error(exc) from exc


async def _dispatch_urgent(db, dispatcher, results):
    """响应已提交后立即尝试加急领取；失败仍保留持久队列供原后台恢复。"""
    from ..services.cron.managed.diagnostics import format_management_error

    for item in results:
        if item["status"] != "queued":
            continue
        identifier = item["execution_id"]
        try:
            row = await db.fetch_one(
                "SELECT dispatch_lane FROM swe_cron_execution_runs WHERE execution_id=%s",
                (identifier,),
            )
            if row and row["dispatch_lane"] == "urgent":
                await dispatcher.dispatch_registered(identifier)
        except Exception as error:
            logger.error(
                "加急重跑首次派发未确认 execution_id=%s\n%s",
                identifier,
                format_management_error(error),
            )
