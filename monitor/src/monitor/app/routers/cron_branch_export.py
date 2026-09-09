"""Binary Excel export contract for cron branch statistics."""

import logging
import re
from datetime import date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..services.cron import QueryService, get_query_service
from ..services.cron.branch_export import (
    BRANCH_SORT_KEYS,
    export_branch_dimension,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/monitor/cron", tags=["cron"])
XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def validate_export_dates(
    start_date: str | None, end_date: str | None
) -> None:
    """Reject invalid dates before the query service's fallback can apply."""
    try:
        if not all(
            value and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
            for value in (start_date, end_date)
        ):
            raise ValueError
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(
            400, "开始日期和结束日期必填，格式须为有效的 YYYY-MM-DD"
        ) from exc
    if start > end:
        raise HTTPException(400, "开始日期不能晚于结束日期")


def validate_export_sort(sort_by: str | None, sort_order: str | None) -> None:
    """Validate the public whitelist without constructing SQL identifiers."""
    if sort_by is None and sort_order is None:
        return
    if sort_by not in BRANCH_SORT_KEYS or sort_order not in {"asc", "desc"}:
        raise HTTPException(
            400, "排序字段与方向须成对提供，字段须有效且方向为 asc 或 desc"
        )


@router.get("/export-branch-dimension", response_class=Response)
async def download_branch_dimension(
    request: Request,
    start_date: str | None = Query(
        default=None, description="开始日期 YYYY-MM-DD（必填）"
    ),
    end_date: str | None = Query(
        default=None, description="结束日期 YYYY-MM-DD（必填，包含当天）"
    ),
    bbk_ids: str | None = Query(default=None, description="分行ID，逗号分隔"),
    sort_by: str | None = Query(default=None, description="分行维度指标字段"),
    sort_order: str | None = Query(default=None, description="asc 或 desc"),
    service: QueryService = Depends(get_query_service),
) -> Response:
    """Query the complete branch ranking and return an XLSX attachment."""
    validate_export_dates(start_date, end_date)
    validate_export_sort(sort_by, sort_order)
    if bbk_ids is not None:
        branches = [value.strip() for value in bbk_ids.split(",")]
        if not all(branches):
            raise HTTPException(
                400, "分行ID不能为空，请使用逗号分隔有效分行ID"
            )
        bbk_ids = ",".join(dict.fromkeys(branches))
    try:
        ranking = await service.get_branch_behavior(
            start_date=start_date,
            end_date=end_date,
            bbk_ids=bbk_ids,
            source_id=request.headers.get("X-Source-Id") or "default",
        )
        content = await run_in_threadpool(
            export_branch_dimension,
            ranking.items,
            sort_by,
            sort_order,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to export cron branch dimension")
        raise HTTPException(500, "分行维度导出失败，请稍后重试") from exc
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
        "%Y%m%d_%H%M%S"
    )
    filename = quote(f"定时任务分行维度_{timestamp}.xlsx")
    return Response(
        content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        },
    )
