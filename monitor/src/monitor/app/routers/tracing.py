# -*- coding: utf-8 -*-
# pylint: disable=too-many-branches too-many-statements too-many-locals
"""Tracing API router for operational dashboard.

提供运营看板的数据查询和导出 API 端点。
"""

import logging
from datetime import datetime, timedelta
from typing import List, Literal, Optional

import httpx
from fastapi import APIRouter, Query, HTTPException, Request, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from ..models.tracing import (
    ErrorItem,
    ErrorListResponse,
    ErrorSummary,
    OverviewStats,
    TraceDetail,
    TraceDetailWithTimeline,
    SessionStats,
    UserStats,
    ModelOutputRequest,
    MCPSummary,
    TaskStatusSummary,
    ExtractCustomerNamesRequest,
    ExtractCustomerNamesResponse,
    InputTokensMismatchItem,
    InputTokensFixItem,
    InitSpanSkillIdRequest,
    InitSpanSkillIdResponse,
)
from ..services.tracing import TracingQueryService, TracingExportService
from ..services.tracing.extract_service import ExtractCustomerNamesService
from ..services.tracing.skill_id_initializer import SkillIdInitializer
from ..database import get_es_client, get_db_connection
from ...config.constant import USER_INFO_API_URL


def _get_source_id_from_header(request: Request) -> str:
    """从请求头获取 source_id.

    优先级：
    1. X-Source-Id 请求头
    2. 默认值 "default"

    Args:
        request: FastAPI 请求对象

    Returns:
        数据源标识字符串
    """
    header_source_id = request.headers.get("X-Source-Id")
    if header_source_id:
        return header_source_id
    return "default"


def _parse_date(
    date_str: Optional[str],
    field_name: str,
    add_day: bool = False,
) -> Optional[datetime]:
    """解析日期字符串为 datetime.

    Args:
        date_str: YYYY-MM-DD 格式的日期字符串
        field_name: 字段名，用于错误消息
        add_day: 是否增加一天以包含结束日期

    Returns:
        解析后的 datetime 或 None

    Raises:
        HTTPException: 日期格式无效时抛出
    """
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if add_day:
            dt = dt + timedelta(days=1)
        return dt
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name} format",
        ) from exc


def _validate_session_resource_filter(
    resource_type: Optional[Literal["model", "skill", "mcp_tool"]],
    resource_name: Optional[str],
    mcp_server: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Validate and normalize optional session resource filtering."""
    normalized_name = resource_name.strip() if resource_name else None
    normalized_server = mcp_server.strip() if mcp_server else None

    if resource_type is None:
        if normalized_name or normalized_server:
            raise HTTPException(
                status_code=400,
                detail="resource_type is required when resource filter fields are provided",
            )
        return None, None, None

    if not normalized_name:
        raise HTTPException(
            status_code=400,
            detail="resource_name is required",
        )

    if resource_type == "mcp_tool":
        if not normalized_server:
            raise HTTPException(
                status_code=400,
                detail="mcp_server is required for mcp_tool filters",
            )
    elif normalized_server:
        raise HTTPException(
            status_code=400,
            detail="mcp_server is only valid for mcp_tool filters",
        )

    return resource_type, normalized_name, normalized_server


router = APIRouter(prefix="/monitor/tracing", tags=["tracing"])


# ===== 运营概览 =====


@router.get("/overview", response_model=OverviewStats)
async def get_overview(
    request: Request,
    bbk_ids: Optional[str] = Query(
        None,
        description="分行ID筛选",
    ),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    detail: Literal["full", "summary"] = Query(
        "full",
        description="返回明细级别；summary 跳过页面不展示的资源排行数据",
    ),
    time_range: Literal["day", "week", "month", "custom"] = Query(
        "day",
        description="环比时间粒度",
    ),
) -> OverviewStats:
    """获取运营概览统计.

    Args:
        bbk_ids: 分行ID筛选
        start_date: 可选的开始日期筛选
        end_date: 可选的结束日期筛选

    Returns:
        运营概览统计，包括用户数、Token 使用量、模型分布等
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    return await service.get_overview_stats(
        actual_source_id,
        start,
        end,
        bbk_ids,
        include_resource_breakdown=detail == "full",
        time_range=time_range,
    )


# ===== 用户分析 =====


@router.get("/users", response_model=dict)
async def get_users(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    user_id: Optional[str] = Query(
        None,
        description="按用户 ID 筛选（模糊匹配）",
    ),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    sort_by: Optional[str] = Query(
        None,
        description="排序字段: manual_calls, cron_executions, cron_reads, last_active",
    ),
    filter_user_type: Optional[str] = Query(
        "filtered",
        description="用户过滤类型: filtered(过滤80/IT开头用户), all(仅过滤default用户)",
    ),
    bbk_ids: Optional[str] = Query(None, description="按分行号筛选"),
    metric_type: Optional[str] = Query(
        "manual",
        description="口径类型: manual(主动使用), cron_exec(定时执行), cron_read(结果查看)",
    ),
) -> dict:
    """获取用户列表及其统计信息.

    Args:
        page: 页码
        page_size: 每页数量
        user_id: 按用户 ID 筛选
        start_date: 开始日期筛选
        end_date: 结束日期筛选
        sort_by: 排序字段
        filter_user_type: 用户过滤类型
        metric_type: 口径类型（manual/cron_exec/cron_read）

    Returns:
        分页的用户列表及统计信息
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    users, total = await service.get_users(
        actual_source_id,
        page,
        page_size,
        user_id,
        start,
        end,
        sort_by,
        filter_user_type,
        bbk_ids,
        metric_type,
    )
    return {
        "items": [u.model_dump() for u in users],
        "total": total,
        "page": page,
        "page_size": page_size,
        "metric_type": metric_type,
    }


@router.get("/users/{user_id}", response_model=UserStats)
async def get_user_stats(
    user_id: str,
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(
        None,
        description="分行ID筛选",
    ),
) -> UserStats:
    """获取指定用户的统计详情.

    Args:
        user_id: 用户标识
        start_date: 可选的开始日期筛选
        end_date: 可选的结束日期筛选
        bbk_ids: 分行ID筛选

    Returns:
        用户统计信息
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    return await service.get_user_stats(
        actual_source_id,
        user_id,
        start,
        end,
        bbk_ids,
    )


# ===== 对话分析 =====


@router.get("/traces", response_model=dict)
async def get_traces(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    user_id: Optional[str] = Query(None, description="按用户 ID 筛选"),
    session_id: Optional[str] = Query(
        None,
        description="按会话 ID 筛选",
    ),
    status: Optional[str] = Query(None, description="按状态筛选"),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="按分行号筛选"),
) -> dict:
    """获取对话列表.

    Args:
        page: 页码
        page_size: 每页数量
        user_id: 按用户 ID 筛选
        session_id: 按会话 ID 筛选
        status: 按状态筛选（running, completed, error, cancelled）
        start_date: 开始日期筛选
        end_date: 结束日期筛选

    Returns:
        分页的对话列表
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    traces, total = await service.get_traces(
        source_id=actual_source_id,
        page=page,
        page_size=page_size,
        user_id=user_id,
        session_id=session_id,
        status=status,
        start_date=start,
        end_date=end,
        bbk_ids=bbk_ids,
    )
    return {
        "items": [t.model_dump() for t in traces],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/traces/{trace_id}", response_model=TraceDetail)
async def get_trace_detail(
    trace_id: str,
    request: Request,
) -> TraceDetail:
    """获取对话详情（包含 Span）.

    Args:
        trace_id: 对话标识（全局唯一）

    Returns:
        对话详情及所有 Span

    Raises:
        HTTPException: 对话未找到时抛出
    """
    service = TracingQueryService.get_instance()

    detail = await service.get_trace_detail(trace_id, None)
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    return detail


@router.get("/session/{session_id}/traces")
async def get_session_traces(
    session_id: str,
    request: Request,
    error_trace_id: Optional[str] = Query(
        None,
        description="报错的 Trace ID（用于标记报错轮次）",
    ),
) -> dict:
    """获取 Session 所有轮次的对话.

    返回该会话的所有对话轮次，按时间顺序排列，
    并标记报错发生的轮次。

    Args:
        session_id: 会话 ID
        error_trace_id: 报错的 Trace ID（可选）

    Returns:
        包含所有轮次的对话数据
    """
    service = TracingQueryService.get_instance()

    result = await service.get_session_traces(session_id, error_trace_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return result


@router.get(
    "/traces/{trace_id}/timeline",
    response_model=TraceDetailWithTimeline,
)
async def get_trace_timeline(
    trace_id: str,
    request: Request,
) -> TraceDetailWithTimeline:
    """获取对话详情（带时间线）.

    返回分层时间线，其中技能调用是父节点，包含其工具调用作为子节点。

    Args:
        trace_id: 对话标识（全局唯一）

    Returns:
        对话详情及分层时间线

    Raises:
        HTTPException: 对话未找到时抛出
    """
    service = TracingQueryService.get_instance()

    detail = await service.get_trace_detail_with_timeline(
        trace_id,
        None,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    return detail


# ===== 会话分析 =====


@router.get("/sessions", response_model=dict)
async def get_sessions(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    user_id: Optional[str] = Query(None, description="按用户 ID 筛选"),
    session_id: Optional[str] = Query(
        None,
        description="按会话 ID 筛选（模糊匹配）",
    ),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="按分行号筛选"),
    has_error: Optional[bool] = Query(None, description="是否筛选报错会话"),
    resource_type: Optional[Literal["model", "skill", "mcp_tool"]] = Query(
        None,
        description="资源筛选类型",
    ),
    resource_name: Optional[str] = Query(
        None,
        description="模型名、技能名或 MCP 工具名",
    ),
    mcp_server: Optional[str] = Query(
        None,
        description="MCP 服务名，仅 mcp_tool 类型使用",
    ),
) -> dict:
    """获取会话列表及其统计信息.

    Args:
        page: 页码
        page_size: 每页数量
        user_id: 按用户 ID 筛选
        session_id: 按会话 ID 筛选（模糊匹配）
        start_date: 开始日期筛选
        end_date: 结束日期筛选
        has_error: 是否筛选报错会话（True=只返回报错会话，None=返回全部）
        resource_type: 资源筛选类型
        resource_name: 资源名称
        mcp_server: MCP 服务名

    Returns:
        分页的会话列表及统计信息
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)
    resource_type, resource_name, mcp_server = (
        _validate_session_resource_filter(
            resource_type,
            resource_name,
            mcp_server,
        )
    )

    sessions, total = await service.get_sessions(
        source_id=actual_source_id,
        page=page,
        page_size=page_size,
        user_id=user_id,
        session_id=session_id,
        start_date=start,
        end_date=end,
        bbk_ids=bbk_ids,
        has_error=has_error,
        resource_type=resource_type,
        resource_name=resource_name,
        mcp_server=mcp_server,
    )
    return {
        "items": [s.model_dump() for s in sessions],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/sessions/{session_id:path}", response_model=SessionStats)
async def get_session_stats(
    session_id: str,
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(
        None,
        description="分行标识，多个用逗号分隔",
    ),
) -> SessionStats:
    """获取指定会话的统计详情.

    Args:
        session_id: 会话标识
        start_date: 可选的开始日期筛选
        end_date: 可选的结束日期筛选

    Returns:
        会话统计信息
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    return await service.get_session_stats(
        actual_source_id,
        session_id,
        start,
        end,
        bbk_ids,
    )


# ===== 用户消息 =====


@router.get("/user-messages", response_model=dict)
async def get_user_messages(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    user_id: Optional[str] = Query(None, description="按用户 ID 筛选"),
    session_id: Optional[str] = Query(
        None,
        description="按会话 ID 筛选",
    ),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    query: Optional[str] = Query(
        None,
        description="搜索用户消息内容",
    ),
    bbk_ids: Optional[str] = Query(None, description="按分行号筛选"),
    exclude_cron_task_sessions: bool = Query(
        False,
        description="是否排除 session_id 以 cron-task 开头的会话",
    ),
) -> dict:
    """获取用户消息列表（含 Token 信息）.

    用于成本分析和消息内容查询。

    Args:
        page: 页码
        page_size: 每页数量
        user_id: 按用户 ID 筛选
        session_id: 按会话 ID 筛选
        start_date: 开始日期筛选
        end_date: 结束日期筛选
        query: 搜索用户消息内容（模糊匹配）

    Returns:
        分页的用户消息列表及 Token 使用量
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    messages, total = await service.get_user_messages(
        source_id=actual_source_id,
        page=page,
        page_size=page_size,
        user_id=user_id,
        session_id=session_id,
        start_date=start,
        end_date=end,
        query_text=query,
        export=False,
        bbk_ids=bbk_ids,
        exclude_cron_task_sessions=exclude_cron_task_sessions,
    )
    return {
        "items": [m.model_dump() for m in messages],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/user-messages/export")
async def export_user_messages(
    request: Request,
    user_id: Optional[str] = Query(None, description="按用户 ID 筛选"),
    session_id: Optional[str] = Query(
        None,
        description="按会话 ID 筛选",
    ),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    query: Optional[str] = Query(
        None,
        description="搜索用户消息内容",
    ),
    export_format: str = Query(
        "csv",
        description="导出格式: csv, json 或 xlsx",
        alias="format",
    ),
    bbk_ids: Optional[str] = Query(None, description="按分行号筛选"),
    exclude_cron_task_sessions: bool = Query(
        False,
        description="是否排除 session_id 以 cron-task 开头的会话",
    ),
) -> StreamingResponse:
    """导出用户消息.

    Args:
        user_id: 按用户 ID 筛选
        session_id: 按会话 ID 筛选
        start_date: 开始日期筛选
        end_date: 结束日期筛选
        query: 搜索用户消息内容（模糊匹配）
        export_format: 导出格式（csv, json 或 xlsx）

    Returns:
        StreamingResponse 包含导出文件
    """
    actual_source_id = _get_source_id_from_header(request)
    export_service = TracingExportService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    if export_format == "json":
        return await export_service.export_user_messages_json(
            source_id=actual_source_id,
            user_id=user_id,
            session_id=session_id,
            start_date=start,
            end_date=end,
            query_text=query,
            bbk_id=bbk_ids,
            exclude_cron_task_sessions=exclude_cron_task_sessions,
        )
    if export_format == "xlsx":
        return await export_service.export_user_messages_xlsx(
            source_id=actual_source_id,
            user_id=user_id,
            session_id=session_id,
            start_date=start,
            end_date=end,
            query_text=query,
            bbk_id=bbk_ids,
            exclude_cron_task_sessions=exclude_cron_task_sessions,
        )
    return await export_service.export_user_messages_csv(
        source_id=actual_source_id,
        user_id=user_id,
        session_id=session_id,
        start_date=start,
        end_date=end,
        query_text=query,
        bbk_id=bbk_ids,
        exclude_cron_task_sessions=exclude_cron_task_sessions,
    )


# ===== 平台来源 =====


@router.get("/sources", response_model=dict)
async def get_sources(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
) -> dict:
    """获取平台来源列表.

    Args:
        start_date: 开始日期筛选
        end_date: 结束日期筛选

    Returns:
        source_id 字符串列表
    """
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    sources = await service.get_sources(start, end)
    return {"sources": sources}


# ===== 渠道分布 =====


@router.get("/channel-distribution", response_model=dict)
async def get_channel_distribution(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
) -> dict:
    """获取渠道分布统计.

    Args:
        start_date: 开始日期筛选
        end_date: 结束日期筛选

    Returns:
        渠道分布：platformUserDistribution, platformCallDistribution, totalPlatforms
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    return await service.get_channel_distribution(actual_source_id, start, end)


# ===== 日趋势 =====


@router.get("/daily-trend", response_model=dict)
async def get_daily_trend(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="分行ID筛选"),
) -> dict:
    """获取日趋势数据."""
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    trend = await service.get_daily_trend(
        actual_source_id,
        start,
        end,
        bbk_ids,
    )
    return {"trendData": trend}


@router.get("/hourly-trend", response_model=dict)
async def get_hourly_trend(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(
        None,
        description="结束日期 (YYYY-MM-DD)",
    ),
    bbk_ids: Optional[str] = Query(None, description="分行ID筛选"),
) -> dict:
    """获取小时趋势数据."""
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    trend = await service.get_hourly_trend(
        actual_source_id,
        start,
        end,
        bbk_ids,
    )
    return {"trendData": trend}


# ===== 模型使用 =====


@router.get("/models", response_model=dict)
async def get_model_usage(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
) -> dict:
    """获取模型使用统计.

    Args:
        start_date: 开始日期筛选
        end_date: 结束日期筛选

    Returns:
        模型使用统计
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    stats = await service.get_overview_stats(actual_source_id, start, end)
    return {"models": [m.model_dump() for m in stats.model_distribution]}


# ===== 工具使用 =====


@router.get("/tools", response_model=dict)
async def get_tool_usage(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
) -> dict:
    """获取工具使用统计.

    Args:
        start_date: 开始日期筛选
        end_date: 结束日期筛选

    Returns:
        工具使用统计
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    stats = await service.get_overview_stats(actual_source_id, start, end)
    return {"tools": [t.model_dump() for t in stats.top_tools]}


# ===== 技能使用 =====


@router.get("/skills", response_model=dict)
async def get_skill_usage(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页数量"),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="分行ID筛选"),
) -> dict:
    """获取技能调用排行榜（分页）."""
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    skills, total = await service.get_skills_paginated(
        actual_source_id,
        page,
        page_size,
        start,
        end,
        bbk_ids,
    )
    return {
        "items": [s.model_dump() for s in skills],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/skills/{skill_name}/traces", response_model=dict)
async def get_skill_traces(
    skill_name: str,
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="分行 ID，逗号分隔"),
) -> dict:
    """获取指定技能调用的对话列表（分页）.

    Args:
        skill_name: 技能名称
        page: 页码
        page_size: 每页数量
        start_date: 开始日期筛选
        end_date: 结束日期筛选
        bbk_ids: 分行筛选

    Returns:
        分页的对话列表
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    traces, total = await service.get_skill_traces(
        skill_name,
        actual_source_id,
        page,
        page_size,
        start,
        end,
        bbk_ids,
    )
    return {
        "items": [t.model_dump() for t in traces],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ===== MCP 使用 =====


@router.get("/mcp/summary", response_model=MCPSummary)
async def get_mcp_summary(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="分行ID筛选"),
) -> MCPSummary:
    """获取 MCP 全局调用汇总统计."""
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    summary = await service.get_mcp_summary(
        actual_source_id,
        start,
        end,
        bbk_ids,
    )
    return summary


@router.get("/mcp", response_model=dict)
async def get_mcp_usage(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页数量"),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="分行ID筛选"),
) -> dict:
    """获取 MCP 服务调用排行榜（分页）."""
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    servers, total = await service.get_mcp_servers_paginated(
        actual_source_id,
        page,
        page_size,
        start,
        end,
        bbk_ids,
    )
    return {
        "items": [s.model_dump() for s in servers],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ===== 定时任务执行统计 =====


@router.get("/task-status/summary", response_model=TaskStatusSummary)
async def get_task_status_summary(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="分行ID筛选"),
) -> TaskStatusSummary:
    """获取定时任务执行汇总统计."""
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    summary = await service.get_task_status_summary(
        actual_source_id,
        start,
        end,
        bbk_ids,
    )
    return summary


# ===== 报错分析统计 =====


@router.get("/error/summary", response_model=ErrorSummary)
async def get_error_summary(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="分行ID筛选"),
) -> ErrorSummary:
    """获取报错分析汇总统计."""
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    summary = await service.get_error_summary(
        actual_source_id,
        start,
        end,
        bbk_ids,
    )
    return summary


@router.get("/error/list", response_model=ErrorListResponse)
async def get_error_list(
    request: Request,
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
    bbk_ids: Optional[str] = Query(None, description="分行ID筛选"),
    error_type: Optional[str] = Query(
        None,
        description="错误类型: llm_input / tool_call_end",
    ),
    search: Optional[str] = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页数量"),
) -> ErrorListResponse:
    """获取报错列表.

    返回错误详情列表，支持按错误类型过滤和关键词搜索。
    """
    actual_source_id = _get_source_id_from_header(request)
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    result = await service.get_error_list(
        actual_source_id,
        start,
        end,
        bbk_ids,
        error_type,
        search,
        page,
        page_size,
    )
    return result


# ===== Model Output 写入 =====


@router.post("/model-output")
async def index_model_output(
    request: Request,
    body: ModelOutputRequest,
):
    """写入 model_output 到 ES.

    由 SWE 服务调用，将 model_output 写入 Elasticsearch。

    Args:
        body: 包含 trace_id 和 model_output

    Returns:
        写入结果
    """
    es_client = get_es_client()
    if es_client is None or not es_client.is_connected:
        # ES 未配置，静默跳过（与原 SWE 行为一致）
        logger.info("ES not configured, skipping model_output write")
        return {"status": "skipped", "reason": "ES not configured"}

    try:
        success = await es_client.index_message(
            body.trace_id,
            body.model_output,
        )
        if success:
            return {"status": "success"}
        else:
            return {"status": "failed"}
    except Exception as e:
        logger.warning("Failed to write model_output: %s", e)
        return {"status": "failed", "error": str(e)}


# ===== 批量更新用户信息 =====


class BatchUpdateTracingUserInfoRequest(BaseModel):
    """批量更新 tracing 用户信息请求。"""

    batch_size: int = Field(default=100, description="每批处理 user_id 数量")


class BatchUpdateTracingUserInfoResponse(BaseModel):
    """批量更新 tracing 用户信息响应。"""

    total: int = Field(..., description="待处理总数")
    traces_updated: int = Field(..., description="traces 表更新数")
    spans_updated: int = Field(..., description="spans 表更新数")
    details: List[dict] = Field(default_factory=list, description="处理详情")


def _extract_bbk_id_from_path_name(path_name: str | None) -> str | None:
    """从 pathName 中提取 BBK ID。

    pathName 格式如: "某企业/总行/生产部/某组"
    提取第一个和第二个"/"之间的内容，映射为 BBK ID。
    """
    if not path_name:
        return None

    from ...utils.bbk import get_bbk_id_by_name

    parts = path_name.split("/")
    if len(parts) >= 2 and parts[1]:
        return get_bbk_id_by_name(parts[1])

    return None


async def _fetch_user_info_for_user(
    user_id: str,
    headers: dict,
) -> tuple[str | None, str | None]:
    """调用外部 API 获取用户信息。

    Returns:
        (userName, bbk_id) 元组
    """
    if not USER_INFO_API_URL:
        return None, None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                USER_INFO_API_URL,
                json={
                    "keyWord": user_id,
                    "compareType": "EQ",
                },
                headers=headers,
            )

        if not response.is_success:
            logger.warning(
                f"User info API failed for user {user_id}: {response.status_code}",
            )
            return None, None

        data = response.json()
        outer_data = data.get("data")

        if outer_data is None:
            return None, None

        if isinstance(outer_data, list):
            result_data = outer_data
        elif isinstance(outer_data, dict):
            result_data = outer_data.get("data", [])
            if not isinstance(result_data, list):
                result_data = []
        else:
            result_data = []

        if not result_data:
            return None, None

        user_info = result_data[0]
        if not isinstance(user_info, dict):
            return None, None

        user_name = user_info.get("userName")
        path_name = user_info.get("pathName")
        bbk_id = _extract_bbk_id_from_path_name(path_name)

        return user_name, bbk_id

    except Exception as e:
        logger.error(f"Error fetching user info for user {user_id}: {e}")
        return None, None


@router.post(
    "/batch-update-user-info",
    response_model=BatchUpdateTracingUserInfoResponse,
    summary="批量更新 tracing 表用户信息",
    description="按 user_id 去重查询缺少 user_name 的记录，减少 API 调用次数",
)
async def batch_update_tracing_user_info(
    request: Request,
    body: BatchUpdateTracingUserInfoRequest,
) -> BatchUpdateTracingUserInfoResponse:
    """批量更新 tracing 表的用户信息。

    按 user_id 去重查询，对每个唯一 user_id 只调用一次 API，
    然后批量更新该 user_id 对应的所有 traces 和 spans（不限定 source_id）。
    只要 user_name 或 bbk_id 任一缺失，就纳入回填范围。

    Args:
        request: FastAPI 请求对象
        body: 包含 batch_size 的请求体

    Returns:
        处理结果统计
    """
    try:
        db = get_db_connection()
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        )

    if not db.is_connected:
        raise HTTPException(
            status_code=503,
            detail="Database not connected",
        )

    batch_size = body.batch_size

    # 按 user_id 去重查询，减少 API 调用次数（不限定 source_id）
    query = """
        SELECT DISTINCT user_id
        FROM swe_tracing_traces
        WHERE user_id IS NOT NULL
          AND user_id != ''
          AND (
                user_name IS NULL OR user_name = ''
                OR bbk_id IS NULL OR bbk_id = ''
          )
        LIMIT %s
    """
    unique_users = await db.fetch_all(query, (batch_size,))
    unique_user_ids = [row["user_id"] for row in unique_users]

    if not unique_user_ids:
        return BatchUpdateTracingUserInfoResponse(
            total=0,
            traces_updated=0,
            spans_updated=0,
            details=[],
        )

    # 查询这些 user_id 对应的 traces 总数（不限定 source_id）
    placeholders = ",".join(["%s"] * len(unique_user_ids))
    count_query = f"""
        SELECT COUNT(*) as cnt
        FROM swe_tracing_traces
        WHERE user_id IN ({placeholders})
          AND (
                user_name IS NULL OR user_name = ''
                OR bbk_id IS NULL OR bbk_id = ''
          )
    """
    count_result = await db.fetch_one(count_query, tuple(unique_user_ids))
    total = count_result["cnt"] if count_result else 0

    # 构建请求头
    headers = {"Content-Type": "application/json"}
    auth_header = request.headers.get("Authorization")
    if auth_header:
        headers["Authorization"] = auth_header

    # 对每个唯一 user_id 调用一次 API
    user_info_map: dict[str, tuple[str | None, str | None]] = {}
    details: list[dict] = []

    for user_id in unique_user_ids:
        user_name, bbk_id = await _fetch_user_info_for_user(user_id, headers)
        if user_name or bbk_id:
            user_info_map[user_id] = (user_name or "", bbk_id or "")
            details.append(
                {
                    "user_id": user_id,
                    "user_name": user_name,
                    "bbk_id": bbk_id,
                    "api_called": True,
                },
            )

    # 按 user_id 批量更新（不限定 source_id）
    traces_updated = 0
    spans_updated = 0

    if user_info_map:
        # 构建 batch update 参数（仅按 user_id）
        updates_by_user: list[tuple] = []
        for user_id, (user_name, bbk_id) in user_info_map.items():
            updates_by_user.append((user_name, bbk_id, user_id))

        # 更新 traces（仅按 user_id）
        traces_query = """
            UPDATE swe_tracing_traces
            SET user_name = %s, bbk_id = %s
            WHERE user_id = %s
              AND (
                    user_name IS NULL OR user_name = ''
                    OR bbk_id IS NULL OR bbk_id = ''
              )
        """
        traces_updated = await db.execute_many(traces_query, updates_by_user)

        # 更新 spans（仅按 user_id）
        spans_query = """
            UPDATE swe_tracing_spans
            SET user_name = %s, bbk_id = %s
            WHERE user_id = %s
              AND (
                    user_name IS NULL OR user_name = ''
                    OR bbk_id IS NULL OR bbk_id = ''
              )
        """
        spans_updated = await db.execute_many(spans_query, updates_by_user)

    logger.info(
        f"Batch update tracing user info: "
        f"unique_users={len(unique_user_ids)}, total_traces={total}, "
        f"traces_updated={traces_updated}, spans_updated={spans_updated}",
    )

    return BatchUpdateTracingUserInfoResponse(
        total=total,
        traces_updated=traces_updated,
        spans_updated=spans_updated,
        details=details,
    )


# ===== 提取客户姓名 =====


@router.post(
    "/extract-customer-names",
    response_model=ExtractCustomerNamesResponse,
    summary="提取客户姓名",
    description="从指定技能的对话记录中提取客户姓名",
)
async def extract_customer_names(
    body: ExtractCustomerNamesRequest,
) -> ExtractCustomerNamesResponse:
    """提取客户姓名.

    从 swe_tracing_traces 的 user_message 和 ES 的 model_output 中提取客户姓名，
    结果保存到 swe_extracted_customer_names 表。

    Args:
        body: 提取请求参数

    Returns:
        提取统计结果

    Raises:
        HTTPException: skill_names 为空时返回 400 错误
    """
    if not body.skill_names:
        raise HTTPException(
            status_code=400,
            detail="skill_names is required",
        )

    service = ExtractCustomerNamesService.get_instance()

    # 解析日期（BETWEEN 两边界均包含，end_date 设为当天结束时间）
    start_date = None
    end_date = None
    if body.start_date:
        try:
            start_date = datetime.strptime(body.start_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid start_date format",
            ) from exc
    if body.end_date:
        try:
            end_date = datetime.strptime(body.end_date, "%Y-%m-%d").replace(
                hour=23,
                minute=59,
                second=59,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid end_date format",
            ) from exc

    try:
        result = await service.extract_names(
            skill_names=body.skill_names,
            user_ids=body.user_ids,
            bbk_id=body.bbk_id,
            start_date=start_date,
            end_date=end_date,
        )
        return ExtractCustomerNamesResponse(**result)
    except Exception as e:
        logger.error("Failed to extract customer names: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to extract customer names: {e}",
        ) from e


# ===== 历史 span skill_id 初始化 =====


@router.post(
    "/admin/spans/init-skill-id",
    response_model=InitSpanSkillIdResponse,
    summary="初始化历史 span 的 skill_id",
    description=(
        "按 source_id + skill_name 从 swe_skills 匹配并回填 "
        "swe_tracing_spans.skill_id。同一 (source_id, skill_name) 在 "
        "swe_skills 存在多个候选时按 cn_name 非空、enabled=1、updated_at DESC、"
        "id DESC 稳定选择一个 skill_id 写入；接口幂等，仅处理 skill_id 为空的"
        "记录。服务内部按 start_time + span_id 复合游标自动分批扫描，单次请求会"
        "持续处理到没有剩余记录。dry_run=true 时仅统计，不写库。"
    ),
)
async def init_span_skill_id(
    body: InitSpanSkillIdRequest,
) -> InitSpanSkillIdResponse:
    """初始化历史 span 的 skill_id.

    仅在以下条件同时满足时回写 span.skill_id：
    - swe_tracing_spans.skill_id 为空
    - swe_skills 中存在与该 (source_id, skill_name) 对应的候选
    - 多个候选时按稳定优先级选出一个 skill_id

    Args:
        body: 初始化请求参数（source_id、batch_size、dry_run）

    Returns:
        初始化结果统计

    Raises:
        HTTPException: dry_run=false 时如出现数据库异常，返回 500
    """
    try:
        db = get_db_connection()
        initializer = SkillIdInitializer(db=db)
        result = await initializer.initialize(
            source_id=body.source_id,
            batch_size=body.batch_size,
            dry_run=body.dry_run,
        )
        return InitSpanSkillIdResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to init span skill_id: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to init span skill_id: {e}",
        ) from e


# ===== Input Tokens 修复 =====


class InputTokensMismatchListResponse(BaseModel):
    """input_tokens 不匹配列表响应."""

    items: List[InputTokensMismatchItem] = Field(
        default_factory=list,
        description="不匹配列表",
    )
    total: int = Field(..., description="总数")
    page: int = Field(..., description="当前页码")
    page_size: int = Field(..., description="每页数量")


class InputTokensFixRequest(BaseModel):
    """input_tokens 修复请求."""

    trace_ids: List[str] = Field(
        ...,
        description="待修复的 trace_id 列表",
    )
    dry_run: bool = Field(
        default=True,
        description="是否为预览模式（true=仅返回预览，不实际更新）",
    )


class InputTokensFixResponse(BaseModel):
    """input_tokens 修复响应."""

    dry_run: bool = Field(..., description="是否为预览模式")
    fixed_count: int = Field(..., description="修复（或预览）的记录数")
    items: List[InputTokensFixItem] = Field(
        default_factory=list,
        description="修复详情列表",
    )


@router.get(
    "/input-tokens/check",
    response_model=InputTokensMismatchListResponse,
    summary="检查 input_tokens 不匹配",
    description="查询 trace 表与 span 表 input_tokens 不一致的记录",
)
async def check_input_tokens_mismatch(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    start_date: Optional[str] = Query(
        None,
        description="开始日期 (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
) -> InputTokensMismatchListResponse:
    """检查 input_tokens 不匹配的 trace.

    通过比对 trace.total_input_tokens 与 span 表汇总值，
    找出存在差异的记录，供后续修复接口使用。

    Args:
        page: 页码
        page_size: 每页数量
        start_date: 开始日期筛选
        end_date: 结束日期筛选

    Returns:
        不匹配的 trace 列表
    """
    service = TracingQueryService.get_instance()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    items, total = await service.check_input_tokens_mismatch(
        page=page,
        page_size=page_size,
        start_date=start,
        end_date=end,
    )

    return InputTokensMismatchListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/input-tokens/fix",
    response_model=InputTokensFixResponse,
    summary="修复 input_tokens",
    description="修复 trace 表 input_tokens 与 span 表汇总值不一致的记录",
)
async def fix_input_tokens_mismatch(
    request: Request,
    body: InputTokensFixRequest,
) -> InputTokensFixResponse:
    """修复 input_tokens 不匹配.

    根据提供的 trace_id 列表，将 trace.total_input_tokens 更新为
    span 表汇总值。支持 dry_run 模式预览修复结果。

    Args:
        body: 修复请求，包含 trace_ids 和 dry_run 标志

    Returns:
        修复结果详情

    Raises:
        HTTPException: trace_ids 为空时返回 400 错误
    """
    if not body.trace_ids:
        raise HTTPException(
            status_code=400,
            detail="trace_ids is required",
        )

    service = TracingQueryService.get_instance()

    result = await service.fix_input_tokens_mismatch(
        trace_ids=body.trace_ids,
        dry_run=body.dry_run,
    )

    return InputTokensFixResponse(
        dry_run=body.dry_run,
        fixed_count=result["fixed_count"],
        items=result["items"],
    )
