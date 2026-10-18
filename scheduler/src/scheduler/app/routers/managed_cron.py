# -*- coding: utf-8 -*-
"""Scheduler 新任务查询接口；保持旧接口不变。"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..database.connection import get_db_connection
from ..services.cron.managed.contracts import (
    ChildrenPage,
    JobPage,
    ManagedCronNotFound,
    ManagedCronUnavailable,
    QueryOptions,
)
from ..services.cron.managed.identity import CallerIdentity
from ..services.cron.managed.repository import ManagedCronRepository

router = APIRouter(prefix="/scheduler/cron/jobs", tags=["managed-cron"])


def caller_identity(request: Request) -> CallerIdentity:
    try:
        return CallerIdentity.from_headers(request.headers)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def get_managed_repository() -> ManagedCronRepository:
    try:
        return ManagedCronRepository(get_db_connection())
    except RuntimeError as exc:
        raise HTTPException(503, "cron_database_unavailable") from exc


def query_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ManagedCronNotFound):
        return HTTPException(404, "cron_job_not_found")
    if isinstance(exc, ManagedCronUnavailable):
        return HTTPException(503, "cron_database_unavailable")
    return HTTPException(400, "invalid_cron_query")


def page_options(
    limit: int = Query(
        50,
        ge=1,
        le=200,
        description="每页记录数，默认 50，最大 200",
    ),
    cursor: str | None = Query(
        None,
        max_length=4096,
        description="上一页 next_cursor；继续查询时保持完整身份及筛选条件不变",
    ),
    include_deleted: bool = False,
    tenant_ids: list[str] | None = Query(
        None,
        description="子任务接收方筛选，多个值重复传 tenant_ids 参数，不使用逗号拼接",
    ),
) -> QueryOptions:
    return QueryOptions(limit, cursor, include_deleted, tenant_ids)


@router.get("", response_model=JobPage, summary="查询新版定时任务列表")
async def list_managed_jobs(
    options: QueryOptions = Depends(page_options),
    caller: CallerIdentity = Depends(caller_identity),
    repo: ManagedCronRepository = Depends(get_managed_repository),
):
    """按完整 Header 身份分页查询 Scheduler 管理的新任务。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    默认隐藏已删除任务，items 平铺定义及展示字段；继续分页需保持身份和筛选不变，不回退旧任务存储。
    """
    try:
        return await repo.page(caller, options)
    except (ManagedCronNotFound, ManagedCronUnavailable, ValueError) as exc:
        raise query_error(exc) from exc


@router.get(
    "/{job_id}/broadcast/children",
    response_model=ChildrenPage,
    summary="查询源任务的分发子任务",
)
async def list_managed_children(
    job_id: str,
    options: QueryOptions = Depends(page_options),
    caller: CallerIdentity = Depends(caller_identity),
    repo: ManagedCronRepository = Depends(get_managed_repository),
):
    """按源任务权限查询关联子任务，返回轻量定义、实际偏移及完整目标身份。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    支持接收方筛选、删除记录及稳定游标；接收方不能由此查询其他接收方，后续写操作仍需逐项鉴权。
    """
    try:
        return await repo.page(caller, options, parent_id=job_id)
    except (ManagedCronNotFound, ManagedCronUnavailable, ValueError) as exc:
        raise query_error(exc) from exc


@router.get("/{job_id}", summary="查询新版定时任务详情")
async def get_managed_job(
    job_id: str,
    include_deleted: bool = False,
    caller: CallerIdentity = Depends(caller_identity),
    repo: ManagedCronRepository = Depends(get_managed_repository),
):
    """读取一个有权访问的 Scheduler 新任务，返回 spec、state、task 及相关版本。

    身份沿用可信网关 Header：X-User-Id、X-Source-Id 必填，tenant、Agent 和 scope 按现有约定解析。
    include_deleted 用于核对逻辑删除和清理状态；查不到不回退旧表，读取不准备资源或修改状态。
    """
    try:
        return await repo.detail(
            caller,
            job_id,
            include_deleted=include_deleted,
        )
    except (ManagedCronNotFound, ManagedCronUnavailable, ValueError) as exc:
        raise query_error(exc) from exc
