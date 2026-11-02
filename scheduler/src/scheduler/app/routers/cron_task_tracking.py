# -*- coding: utf-8 -*-
"""管理员单任务追踪；仅提供 GET，不改变执行或未读状态。"""

import logging
import traceback
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from ..database.connection import get_db_connection
from ..models.cron_task_tracking import TimelineQuery
from ..services.cron.tracking_common import (
    TrackingNotFound,
    TrackingUnavailable,
    identity_value,
)
from ..services.cron.tracking_lookup import TrackingLookup
from ..services.cron.tracking_timeline import TrackingTimeline

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/scheduler/cron/task-tracking/jobs",
    tags=["cron-task-tracking"],
)


def tracking_source(request: Request):
    try:
        identity_value(request.headers.get("x-user-id", ""))
        source = identity_value(request.headers.get("x-source-id", ""))
    except ValueError as exc:
        raise HTTPException(400, "invalid_cron_identity") from exc
    if request.headers.get("x-user-role", "").strip().lower() not in {
        "manager",
        "admin",
    }:
        raise HTTPException(403, "cron_tracking_manager_required")
    return source


def tracking_db():
    try:
        return get_db_connection()
    except Exception as exc:
        query_id = uuid4().hex
        logger.error(
            "Cron tracking database unavailable query_id=%s error_type=%s",
            query_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            "cron_tracking_storage_unavailable",
            headers={"X-Cron-Tracking-Query-Id": query_id},
        ) from exc


def timeline_filters(request: Request):
    selected = {
        name: request.query_params[name]
        for name in TimelineQuery.model_fields
        if name in request.query_params
    }
    try:
        return TimelineQuery.model_validate(selected)
    except ValueError as exc:
        raise HTTPException(422, "invalid_cron_tracking_query") from exc


async def result(awaitable):
    try:
        return await awaitable
    except TrackingNotFound as exc:
        raise HTTPException(404, "cron_tracking_not_found") from exc
    except ValueError as exc:
        raise HTTPException(400, "invalid_cron_tracking_query") from exc
    except Exception as exc:
        query_id = getattr(exc, "query_id", None) or uuid4().hex
        frames = traceback.extract_tb(exc.__traceback__)
        location = " > ".join(
            f"{frame.name}:{frame.lineno}" for frame in frames[-3:]
        )
        logger.error(
            "Cron tracking query failed query_id=%s error_type=%s location=%s",
            query_id,
            type(exc).__name__,
            location,
        )
        code = (
            "cron_tracking_storage_unavailable"
            if isinstance(exc, TrackingUnavailable)
            else "cron_tracking_query_failed"
        )
        raise HTTPException(
            503,
            code,
            headers={"X-Cron-Tracking-Query-Id": query_id},
        ) from exc


@router.get("/{job_id}/lookup", summary="定位当前来源内的新版任务身份")
async def lookup(
    job_id: str,
    tenant_id: str | None = None,
    agent_id: str | None = None,
    source=Depends(tracking_source),
    db=Depends(tracking_db),
):
    return await result(
        TrackingLookup(db, source).lookup(job_id, tenant_id, agent_id),
    )


@router.get("/{job_id}/overview", summary="只读查询任务当前属性和状态")
async def overview(
    job_id: str,
    tenant_id: str,
    agent_id: str,
    source=Depends(tracking_source),
    db=Depends(tracking_db),
):
    return await result(
        TrackingLookup(db, source).overview(job_id, tenant_id, agent_id),
    )


@router.get("/{job_id}/timeline", summary="按事实时间分页查询本任务时间轴")
async def timeline(
    job_id: str,
    tenant_id: str,
    agent_id: str,
    filters=Depends(timeline_filters),
    source=Depends(tracking_source),
    db=Depends(tracking_db),
):
    return await result(
        TrackingTimeline(db, source).timeline(
            job_id,
            tenant_id,
            agent_id,
            filters,
        ),
    )
