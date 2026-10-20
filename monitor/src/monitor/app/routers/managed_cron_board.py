# -*- coding: utf-8 -*-
"""新批调度看板独立查询契约；没有旧表或旧接口回退。"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..database import get_db_connection
from ..models.managed_board import BoardListQuery, BoardDetailQuery
from ..services.cron.managed_board import ManagedBoardQuery
from ..services.cron.managed_board_sql import db_time
from ..services.cron.managed_board_workers import shared_workers

router = APIRouter(
    prefix="/monitor/cron/managed-dispatch",
    tags=["managed-cron-board"],
)


def manager_source(request: Request):
    source = request.headers.get("x-source-id", "")
    actor = request.headers.get("x-user-id", "")
    if (
        not source.strip()
        or not actor.strip()
        or source != source.strip()
        or len(source) > 128
    ):
        raise HTTPException(400, "invalid_cron_identity")
    if request.headers.get("x-user-role", "").strip().lower() not in {
        "manager",
        "admin",
    }:
        raise HTTPException(403, "cron_batch_manager_required")
    return source


def board_service():
    try:
        return ManagedBoardQuery(get_db_connection())
    except Exception as exc:
        raise HTTPException(503, "cron_batch_storage_unavailable") from exc


def board_error(exc):
    if isinstance(exc, LookupError):
        return HTTPException(404, "cron_batch_not_found")
    if isinstance(exc, ValueError):
        return HTTPException(400, "invalid_cron_batch_query")
    return HTTPException(503, "cron_batch_storage_unavailable")


def check_range(start, end):
    if start and end and db_time(start) > db_time(end):
        raise HTTPException(400, "invalid_cron_time_range")


@router.get("/batches", summary="分页查询新版执行批次和全局概览")
async def batches(
    filters: BoardListQuery = Depends(),
    source=Depends(manager_source),
    service=Depends(board_service),
):
    check_range(filters.start_time, filters.end_time)
    try:
        return await service.batches(
            source,
            start=filters.start_time,
            end=filters.end_time,
            status=filters.status,
            query=filters.query,
            page=filters.page,
            page_size=filters.page_size,
        )
    except Exception as exc:
        raise board_error(exc) from exc


@router.get("/workers", summary="查询共享模型池策略、容量和游标历史")
async def workers(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    capacity_cursor: str | None = Query(default=None, max_length=2048),
    source=Depends(manager_source),
    service=Depends(board_service),
):
    check_range(start_time, end_time)
    try:
        # 不带时区的筛选按业务时区解释，再交旧共享查询转换到北京时间。
        from datetime import timezone

        start = db_time(start_time)
        end = db_time(end_time)
        return await shared_workers(
            service.db,
            source,
            start.replace(tzinfo=timezone.utc) if start else None,
            end.replace(tzinfo=timezone.utc) if end else None,
            capacity_cursor,
        )
    except Exception as exc:
        raise board_error(exc) from exc


@router.get(
    "/batches/{batch_id}",
    summary="查询新版批次详情，成员与事件独立分页",
)
async def detail(
    batch_id: str,
    filters: BoardDetailQuery = Depends(),
    source=Depends(manager_source),
    service=Depends(board_service),
):
    try:
        return await service.detail(source, batch_id, filters)
    except Exception as exc:
        raise board_error(exc) from exc
