# -*- coding: utf-8 -*-
"""Tracing query service for operational dashboard."""

import asyncio
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional

from ...database import get_db_connection, DatabaseConnection
from ....utils.bbk import get_bbk_name_by_id
from ....utils.timing import MethodTimer
from ...models.tracing import (
    ErrorItem,
    ErrorListResponse,
    ErrorSummary,
    EventType,
    InputTokensMismatchItem,
    InputTokensFixItem,
    ModelErrorCodeCount,
    ModelUsage,
    MCPToolUsage,
    MCPServerUsage,
    MCPSummary,
    OverviewStats,
    SessionListItem,
    SessionStats,
    SkillCallTimeline,
    SkillUsage,
    Span,
    TimelineEvent,
    ToolCallInSkill,
    ToolUsage,
    Trace,
    TraceDetail,
    TraceFeedback,
    TraceDetailWithTimeline,
    TraceListItem,
    TraceStatus,
    UserListItem,
    UserMessageItem,
    UserStats,
    TaskStatusSummary,
)

logger = logging.getLogger(__name__)

# 需要从统计中排除的 source_id（测试平台等）
EXCLUDED_SOURCE_IDS = ["default"]
EXTENDED_TREND_SOURCE_ID = "RMASSIST"
EXCLUDED_SKILL_NAMES = (
    "cron",
    "search_customs_by_labels",
    "cust_insight_url_generator",
    "immortal-skill",
    "batch_task_executor",
    "skill_creator",
    "docx",
    "himalaya",
)

MODEL_ERROR_CODE_PATTERN = re.compile(
    r"Error code:\s*([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)"
    r"(?=\s|[,:;.)}\]]|-(?:\s|$)|$)",
    re.IGNORECASE,
)

LATEST_FEEDBACK_JOIN_SQL = """
    LEFT JOIN (
        SELECT rf1.*
        FROM swe_response_feedback rf1
        INNER JOIN (
            SELECT trace_id, source_id, MAX(id) AS max_id
            FROM swe_response_feedback
            WHERE trace_id IS NOT NULL
            GROUP BY trace_id, source_id
        ) latest ON latest.max_id = rf1.id
    ) rf ON rf.trace_id COLLATE utf8mb4_unicode_ci = t.trace_id COLLATE utf8mb4_unicode_ci
        AND rf.source_id COLLATE utf8mb4_unicode_ci <=> t.source_id COLLATE utf8mb4_unicode_ci
"""

FEEDBACK_SELECT_SQL = """
    rf.id as feedback_id,
    rf.source_id as feedback_source_id,
    rf.feedback_user_name as feedback_user_name,
    rf.feedback_user_sap as feedback_user_sap,
    rf.feedback_branch as feedback_branch,
    rf.feedback_sub_branch as feedback_sub_branch,
    rf.feedback_position as feedback_position,
    rf.cron_task_name as feedback_cron_task_name,
    rf.cron_task_id as feedback_cron_task_id,
    rf.response_id as feedback_response_id,
    rf.trace_id as feedback_trace_id,
    rf.chat_id as feedback_chat_id,
    rf.session_id as feedback_session_id,
    rf.feedback_options as feedback_options,
    rf.feedback_content as feedback_content,
    rf.created_at as feedback_created_at,
    rf.updated_at as feedback_updated_at
"""


def build_bbk_in_filter(bbk_ids: Optional[str]) -> tuple[str, list[str]]:
    """构建 bbk IN 过滤条件，支持逗号分隔的多值.

    Args:
        bbk_ids: 逗号分隔的 bbk_id 字符串，如 "100,200,201"

    Returns:
        (filter_sql, params) - 如 " AND bbk_id IN (%s, %s, %s)", ["100", "200", "201"]
        无值时返回 ("", [])

    Note:
        选择总行(100)时，需同时包含虚拟标识 V00，确保数据完整统计
    """
    if not bbk_ids:
        return "", []
    ids = [id.strip() for id in bbk_ids.split(",") if id.strip()]
    if not ids:
        return "", []
    # 总行 100 需同时查询 V00（虚拟标识）
    if "100" in ids and "V00" not in ids:
        ids.append("V00")
    placeholders = ", ".join(["%s"] * len(ids))
    return f" AND bbk_id IN ({placeholders})", ids


def _loads_feedback_options(raw: Any) -> list[str]:
    """解析反馈快捷选项，兼容数据库 JSON 字符串和列表。"""
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
    return []


def _row_to_feedback(row: dict[str, Any]) -> Optional[TraceFeedback]:
    """把查询结果中的反馈字段转换为 TraceFeedback。"""
    feedback_id = row.get("feedback_id") or row.get("id")
    if feedback_id is None:
        return None

    return TraceFeedback(
        id=int(feedback_id),
        source_id=row.get("feedback_source_id") or row.get("source_id"),
        feedback_user_name=row.get("feedback_user_name"),
        feedback_user_sap=row.get("feedback_user_sap"),
        feedback_branch=row.get("feedback_branch"),
        feedback_sub_branch=row.get("feedback_sub_branch"),
        feedback_position=row.get("feedback_position"),
        cron_task_name=row.get("feedback_cron_task_name")
        or row.get("cron_task_name"),
        cron_task_id=row.get("feedback_cron_task_id")
        or row.get("cron_task_id"),
        response_id=row.get("feedback_response_id") or row.get("response_id"),
        trace_id=row.get("feedback_trace_id") or row.get("trace_id"),
        chat_id=row.get("feedback_chat_id") or row.get("chat_id"),
        session_id=row.get("feedback_session_id") or row.get("session_id"),
        feedback_options=_loads_feedback_options(
            row.get("feedback_options"),
        ),
        feedback_content=row.get("feedback_content") or "",
        created_at=row.get("feedback_created_at") or row.get("created_at"),
        updated_at=row.get("feedback_updated_at") or row.get("updated_at"),
    )


def build_cron_bbk_in_filter(bbk_ids: Optional[str]) -> tuple[str, list[str]]:
    """构建定时任务表 bbk IN 过滤条件，支持逗号分隔的多值.

    Args:
        bbk_ids: 逗号分隔的 bbk_id 字符串，如 "100,200,201"

    Returns:
        (filter_sql, params) - 如 " AND j.bbk_id IN (%s, %s, %s)", ["100", "200", "201"]
        无值时返回 ("", [])

    Note:
        选择总行(100)时，需同时包含虚拟标识 V00，确保数据完整统计
    """
    if not bbk_ids:
        return "", []
    ids = [id.strip() for id in bbk_ids.split(",") if id.strip()]
    if not ids:
        return "", []
    # 总行 100 需同时查询 V00（虚拟标识）
    if "100" in ids and "V00" not in ids:
        ids.append("V00")
    placeholders = ", ".join(["%s"] * len(ids))
    return f" AND j.bbk_id IN ({placeholders})", ids


def build_excluded_skill_filter() -> tuple[str, list[str]]:
    """构建需要从排行榜中屏蔽的技能过滤条件。"""
    placeholders = ", ".join(["%s"] * len(EXCLUDED_SKILL_NAMES))
    return f" AND skill_name NOT IN ({placeholders})", list(
        EXCLUDED_SKILL_NAMES,
    )


# 技能展示映射：每个 skill_id 选出一条稳定记录用于展示。
# 排序优先级：cn_name 非空 > enabled=1 > updated_at DESC > id DESC。
SKILL_DISPLAY_MAPPING_SQL_TEMPLATE = """
    SELECT skill_id, skill_name, cn_name, description
    FROM (
        SELECT skill_id, skill_name, cn_name, description,
               ROW_NUMBER() OVER (
                   PARTITION BY skill_id
                   ORDER BY
                       CASE WHEN cn_name IS NOT NULL AND TRIM(cn_name) <> ''
                            THEN 0 ELSE 1 END ASC,
                       CASE WHEN enabled = 1 THEN 0 ELSE 1 END ASC,
                       updated_at DESC, id DESC
               ) AS rn
        FROM swe_skills
        WHERE skill_id IS NOT NULL AND TRIM(skill_id) <> ''
        {skill_id_filter}
    ) ranked
    WHERE rn = 1
"""


def _summarize_task_status_rows(
    rows: list[dict],
) -> tuple[int, int, int, int, int]:
    """汇总定时任务状态及已读数量.

    返回: (success, running, failed, cancelled, read_count)
    """
    success = 0
    running = 0
    failed = 0
    cancelled = 0
    read_count = 0

    for row in rows:
        status = row["status"]
        async_status = row.get("async_status")
        count = row["count"]

        # 综合状态判断
        if status == "success":
            if async_status == "success":
                success += count
            elif async_status is None or async_status == "":
                running += count
            elif async_status == "error":
                failed += count
            else:
                # 其他 async_status 值视为运行中
                running += count
        elif status in ("error", "timeout"):
            failed += count
        elif status in ("cancelled", "skipped"):
            cancelled += count

        if row["is_read"]:
            read_count += count

    return success, running, failed, cancelled, read_count


class TracingQueryService:  # pylint: disable=too-many-public-methods
    """运营看板查询服务."""

    def __init__(self, db: DatabaseConnection):
        """初始化查询服务.

        Args:
            db: 数据库连接实例
        """
        self._db = db

    @classmethod
    def get_instance(cls) -> "TracingQueryService":
        """获取服务实例（使用全局数据库连接）."""
        db = get_db_connection()
        return cls(db)

    # ===== 运营概览 =====

    async def get_overview_stats(
        self,
        source_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
        include_resource_breakdown: bool = True,
        time_range: str = "day",
    ) -> OverviewStats:
        """获取运营概览统计."""
        with MethodTimer(
            "get_overview_stats",
            source_id=source_id,
            bbk_ids=bbk_ids,
        ):
            if start_date is None:
                start_date = datetime.now() - timedelta(days=30)
            if end_date is None:
                end_date = datetime.now() + timedelta(days=1)

            if include_resource_breakdown:
                # 并行获取当前周期各项统计数据
                (
                    (total_users, it_users, business_users),
                    (online_users, online_user_ids),
                    token_row,
                    model_distribution,
                    top_tools,
                    top_skills,
                    (top_mcp_tools, mcp_servers),
                    branch_breakdown,
                    total_skill_calls,
                    customer_click_stats,
                ) = await self._fetch_overview_data(
                    source_id,
                    start_date,
                    end_date,
                    bbk_ids,
                )
                growth_stats = {}
            else:
                summary_data, growth_stats = await asyncio.gather(
                    self._fetch_overview_summary_data(
                        source_id,
                        start_date,
                        end_date,
                        bbk_ids,
                    ),
                    self._get_growth_stats(
                        source_id,
                        start_date,
                        end_date,
                        time_range,
                        bbk_ids,
                    ),
                )
                (
                    (total_users, it_users, business_users),
                    (online_users, online_user_ids),
                    token_row,
                    branch_breakdown,
                    total_skill_calls,
                    customer_click_stats,
                ) = summary_data
                model_distribution = []
                top_tools = []
                top_skills = []
                top_mcp_tools = []
                mcp_servers = []

            return self._build_overview_stats(
                total_users=total_users,
                it_users=it_users,
                business_users=business_users,
                online_users=online_users,
                online_user_ids=online_user_ids,
                model_distribution=model_distribution,
                token_row=token_row,
                top_tools=top_tools,
                top_skills=top_skills,
                top_mcp_tools=top_mcp_tools,
                mcp_servers=mcp_servers,
                branch_breakdown=branch_breakdown,
                total_skill_calls=total_skill_calls,
                customer_click_stats=customer_click_stats,
                growth_stats=growth_stats,
            )

    async def _get_growth_stats(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        time_range: str = "day",
        bbk_ids: Optional[str] = None,
    ) -> dict[str, float | None]:
        """获取概览卡片的环比数据（内部聚合，不提供独立 HTTP 接口）。"""
        period_days = {
            "day": 1,
            "week": 7,
            "month": 30,
        }.get(time_range, max((end_date - start_date).days, 1))
        previous_start = start_date - timedelta(days=period_days)

        trace_filter, trace_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            source_clause = "source_id NOT IN (%s)"
            source_values = tuple(EXCLUDED_SOURCE_IDS)
        else:
            source_clause = "source_id = %s"
            source_values = (source_id,)

        async def trace_stats(
            period_start: datetime,
            period_end: datetime,
        ) -> dict:
            query = f"""
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COUNT(DISTINCT session_id) AS sessions,
                       COUNT(DISTINCT user_id) AS users
                FROM swe_tracing_traces
                WHERE {source_clause}
                  AND start_time >= %s AND start_time < %s
                  AND user_id != 'default'{trace_filter}
            """
            row = await self._db.fetch_one(
                query,
                (*source_values, period_start, period_end, *trace_params),
            )
            row = row or {}
            return {
                "calls": int(row.get("calls") or 0),
                "tokens": float(row.get("tokens") or 0),
                "sessions": int(row.get("sessions") or 0),
                "users": int(row.get("users") or 0),
            }

        async def cron_count(
            period_start: datetime,
            period_end: datetime,
        ) -> int:
            cron_filter, cron_params = build_cron_bbk_in_filter(bbk_ids)
            query = f"""
                SELECT COUNT(*) AS total
                FROM swe_cron_executions e
                INNER JOIN swe_cron_jobs j ON e.job_id = j.id
                WHERE e.is_final = 1 AND e.actual_time >= %s AND e.actual_time < %s
                  AND j.status != 'deleted' AND j.deleted_at IS NULL
                  AND j.tenant_id != 'default'
                  AND {('j.source_id NOT IN (%s)' if source_id == 'all' else 'j.source_id = %s')}
                  {cron_filter}
            """
            row = await self._db.fetch_one(
                query,
                (period_start, period_end, *source_values, *cron_params),
            )
            return int((row or {}).get("total") or 0)

        async def customer_count(
            period_start: datetime,
            period_end: datetime,
        ) -> int:
            query = f"""
                SELECT COUNT(DISTINCT CONCAT(COALESCE(cron_task_id, ''), '|',
                    COALESCE(customer_id, ''))) AS total
                FROM swe_html_preview_click_events
                WHERE {source_clause}
                  AND clicked_at >= %s AND clicked_at < %s
                  AND event_type = 'preview_view'
                  AND template_type = 'sub'
                  AND cron_task_id IS NOT NULL AND customer_id IS NOT NULL
                  {trace_filter}
            """
            row = await self._db.fetch_one(
                query,
                (*source_values, period_start, period_end, *trace_params),
            )
            return int((row or {}).get("total") or 0)

        current_end = end_date
        previous_end = start_date
        (
            current,
            previous,
            current_cron,
            previous_cron,
            current_customers,
            previous_customers,
        ) = await asyncio.gather(
            trace_stats(start_date, current_end),
            trace_stats(previous_start, previous_end),
            cron_count(start_date, current_end),
            cron_count(previous_start, previous_end),
            customer_count(start_date, current_end),
            customer_count(previous_start, previous_end),
        )

        def growth(
            current_value: float,
            previous_value: float,
        ) -> float | None:
            if previous_value == 0:
                return 100.0 if current_value > 0 else 0.0
            if current_value == 0:
                return None
            return round(
                (current_value - previous_value) / previous_value * 100,
                1,
            )

        return {
            "callsGrowth": growth(current["calls"], previous["calls"]),
            "tokensGrowth": growth(current["tokens"], previous["tokens"]),
            "sessionGrowth": growth(current["sessions"], previous["sessions"]),
            "userGrowth": growth(current["users"], previous["users"]),
            "cronGrowth": growth(current_cron, previous_cron),
            "planCustomersGrowth": growth(
                current_customers,
                previous_customers,
            ),
        }

    async def _fetch_overview_data(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list:
        """并行获取运营概览的各项数据."""
        return await asyncio.gather(
            self._get_total_users(source_id, start_date, end_date, bbk_ids),
            self._get_online_users(source_id, bbk_ids),
            self._get_token_stats(source_id, start_date, end_date, bbk_ids),
            self._get_model_distribution(
                source_id,
                start_date,
                end_date,
                bbk_ids,
            ),
            self._get_top_tools(source_id, start_date, end_date, bbk_ids),
            self._get_top_skills(source_id, start_date, end_date, bbk_ids),
            self._get_mcp_stats(source_id, start_date, end_date, bbk_ids),
            self._get_branch_breakdown(
                source_id,
                start_date,
                end_date,
                bbk_ids,
            ),
            self._get_total_skill_calls(
                source_id,
                start_date,
                end_date,
                bbk_ids,
            ),
            self._get_customer_click_stats(
                source_id,
                start_date,
                end_date,
                bbk_ids,
            ),
        )

    async def _fetch_overview_summary_data(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list:
        """并行获取概览首页实际会展示的轻量数据."""
        return await asyncio.gather(
            self._get_total_users(source_id, start_date, end_date, bbk_ids),
            self._get_online_users(source_id, bbk_ids),
            self._get_token_stats(source_id, start_date, end_date, bbk_ids),
            self._get_branch_breakdown(
                source_id,
                start_date,
                end_date,
                bbk_ids,
            ),
            self._get_total_skill_calls(
                source_id,
                start_date,
                end_date,
                bbk_ids,
            ),
            self._get_customer_click_stats(
                source_id,
                start_date,
                end_date,
                bbk_ids,
            ),
        )

    def _build_overview_stats(
        self,
        total_users: int,
        it_users: int,
        business_users: int,
        online_users: int,
        online_user_ids: list[str],
        token_row: Optional[dict],
        model_distribution: list,
        top_tools: list,
        top_skills: list,
        top_mcp_tools: list,
        mcp_servers: list,
        branch_breakdown: Any,
        total_skill_calls: int = 0,
        customer_click_stats: Optional[dict[str, int]] = None,
        growth_stats: Optional[dict[str, float | None]] = None,
    ) -> OverviewStats:
        """构建运营概览统计对象."""
        if customer_click_stats is None:
            customer_click_stats = {}
        return OverviewStats(
            online_users=online_users,
            online_user_ids=online_user_ids,
            total_users=total_users,
            it_users=it_users,
            business_users=business_users,
            model_distribution=model_distribution,
            total_tokens=token_row["total_tokens"] or 0 if token_row else 0,
            input_tokens=token_row["input_tokens"] or 0 if token_row else 0,
            output_tokens=token_row["output_tokens"] or 0 if token_row else 0,
            total_sessions=(
                token_row["total_sessions"] or 0 if token_row else 0
            ),
            total_conversations=(
                token_row["total_traces"] or 0 if token_row else 0
            ),
            total_skill_calls=total_skill_calls,
            plan_customers=customer_click_stats.get("plan_customers", 0),
            insight_customers=customer_click_stats.get("insight_customers", 0),
            phone_customers=customer_click_stats.get("phone_customers", 0),
            avg_duration_ms=self._extract_avg_duration(token_row),
            top_tools=top_tools,
            top_skills=top_skills,
            top_mcp_tools=top_mcp_tools,
            mcp_servers=mcp_servers,
            daily_trend=[],
            growth_stats=growth_stats or {},
            branch_breakdown=branch_breakdown,
        )

    def _extract_avg_duration(self, token_row: Optional[dict]) -> int:
        """从 token 统计行中提取平均时长."""
        if token_row and token_row.get("avg_duration"):
            return int(token_row["avg_duration"] or 0)
        return 0

    async def _get_branch_breakdown(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> Any:
        """获取分行级别统计数据."""
        from ...models.tracing import (
            BranchMetricItem,
            OverviewBranchBreakdown,
        )

        # 构建查询条件 - 使用辅助函数处理多值过滤
        bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(
                ["%s"] * len(EXCLUDED_SOURCE_IDS),
            )
            trace_where = f"""
                start_time >= %s AND start_time < %s
                AND source_id NOT IN ({exclude_placeholders})
                AND user_id != 'default'
                AND bbk_id IS NOT NULL AND bbk_id != ''{bbk_filter_sql}
            """
            trace_params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_params,
            )
        else:
            trace_where = f"""
                source_id = %s AND start_time >= %s AND start_time < %s
                AND user_id != 'default'
                AND bbk_id IS NOT NULL AND bbk_id != ''{bbk_filter_sql}
            """
            trace_params = (source_id, start_date, end_date, *bbk_params)

        # 各项分行统计查询（V00 并入总行 100）
        users_query = f"""
            SELECT CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END AS bbk_id,
                   COUNT(DISTINCT user_id) AS value
            FROM swe_tracing_traces
            WHERE {trace_where}
            GROUP BY CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END
            ORDER BY value DESC
            LIMIT 5
        """
        sessions_query = f"""
            SELECT CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END AS bbk_id,
                   COUNT(DISTINCT session_id) AS value
            FROM swe_tracing_traces
            WHERE {trace_where}
            GROUP BY CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END
            ORDER BY value DESC
            LIMIT 5
        """
        tokens_query = f"""
            SELECT CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END AS bbk_id,
                   COALESCE(SUM(total_tokens), 0) AS value
            FROM swe_tracing_traces
            WHERE {trace_where}
            GROUP BY CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END
            ORDER BY value DESC
            LIMIT 5
        """
        if source_id == "all":
            customer_query = f"""
                SELECT CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END AS bbk_id,
                       COUNT(
                           DISTINCT CONCAT(
                               COALESCE(cron_task_id, ''),
                               '|',
                               COALESCE(customer_id, '')
                           )
                       ) AS value
                FROM swe_html_preview_click_events
                WHERE clicked_at >= %s AND clicked_at < %s
                  AND source_id NOT IN ({exclude_placeholders})
                  AND bbk_id IS NOT NULL AND bbk_id != ''{bbk_filter_sql}
                  AND event_type = 'preview_view'
                  AND template_type = 'sub'
                  AND cron_task_id IS NOT NULL
                  AND customer_id IS NOT NULL
                GROUP BY CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END
                ORDER BY value DESC
                LIMIT 5
            """
            customer_params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_params,
            )
        else:
            customer_query = f"""
                SELECT CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END AS bbk_id,
                       COUNT(
                           DISTINCT CONCAT(
                               COALESCE(cron_task_id, ''),
                               '|',
                               COALESCE(customer_id, '')
                           )
                       ) AS value
                FROM swe_html_preview_click_events
                WHERE source_id = %s
                  AND clicked_at >= %s AND clicked_at < %s
                  AND bbk_id IS NOT NULL AND bbk_id != ''{bbk_filter_sql}
                  AND event_type = 'preview_view'
                  AND template_type = 'sub'
                  AND cron_task_id IS NOT NULL
                  AND customer_id IS NOT NULL
                GROUP BY CASE WHEN bbk_id = 'V00' THEN '100' ELSE bbk_id END
                ORDER BY value DESC
                LIMIT 5
            """
            customer_params = (source_id, start_date, end_date, *bbk_params)

        # 定时任务分行统计查询
        cron_bbk_filter_sql, cron_bbk_params = build_cron_bbk_in_filter(
            bbk_ids,
        )
        cron_exclude_placeholders = ", ".join(
            ["%s"] * len(EXCLUDED_SOURCE_IDS),
        )
        if source_id == "all":
            cron_query = f"""
                SELECT CASE WHEN j.bbk_id = 'V00' THEN '100' ELSE j.bbk_id END AS bbk_id,
                       COUNT(*) AS value
                FROM swe_cron_executions e
                INNER JOIN swe_cron_jobs j ON e.job_id = j.id
                WHERE e.is_final = 1 AND e.actual_time >= %s AND e.actual_time < %s
                  AND j.status != 'deleted'
                  AND j.deleted_at IS NULL
                  AND j.source_id NOT IN ({cron_exclude_placeholders})
                  AND j.tenant_id != 'default'
                  AND j.bbk_id IS NOT NULL AND j.bbk_id != ''
                  {cron_bbk_filter_sql}
                GROUP BY CASE WHEN j.bbk_id = 'V00' THEN '100' ELSE j.bbk_id END
                ORDER BY value DESC
                LIMIT 5
            """
            cron_params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *cron_bbk_params,
            )
        else:
            cron_query = f"""
                SELECT CASE WHEN j.bbk_id = 'V00' THEN '100' ELSE j.bbk_id END AS bbk_id,
                       COUNT(*) AS value
                FROM swe_cron_executions e
                INNER JOIN swe_cron_jobs j ON e.job_id = j.id
                WHERE e.is_final = 1 AND e.actual_time >= %s AND e.actual_time < %s
                  AND j.status != 'deleted'
                  AND j.deleted_at IS NULL
                  AND j.tenant_id != 'default'
                  AND j.bbk_id IS NOT NULL AND j.bbk_id != ''
                  AND j.source_id = %s
                  {cron_bbk_filter_sql}
                GROUP BY CASE WHEN j.bbk_id = 'V00' THEN '100' ELSE j.bbk_id END
                ORDER BY value DESC
                LIMIT 5
            """
            cron_params = (start_date, end_date, source_id, *cron_bbk_params)

        # 执行查询
        (
            users_rows,
            sessions_rows,
            tokens_rows,
            customer_rows,
            cron_rows,
        ) = await asyncio.gather(
            self._db.fetch_all(users_query, trace_params),
            self._db.fetch_all(sessions_query, trace_params),
            self._db.fetch_all(tokens_query, trace_params),
            self._db.fetch_all(customer_query, customer_params),
            self._db.fetch_all(cron_query, cron_params),
        )

        def build_branch_items(rows: list) -> list[BranchMetricItem]:
            total = sum(float(r.get("value") or 0) for r in rows)
            result = []
            for row in rows:
                value = float(row.get("value") or 0)
                percent = (value / total * 100) if total > 0 else 0
                bbk_id = row.get("bbk_id") or ""
                result.append(
                    BranchMetricItem(
                        bbk_id=bbk_id,
                        bbk_name=get_bbk_name_by_id(bbk_id) or bbk_id,
                        value=value,
                        percent=percent,
                    ),
                )
            return result

        return OverviewBranchBreakdown(
            users=build_branch_items(users_rows),
            sessions=build_branch_items(sessions_rows),
            tokens=build_branch_items(tokens_rows),
            cron_tasks=build_branch_items(cron_rows),
            customers=build_branch_items(customer_rows),
        )

    async def get_daily_trend(
        self,
        source_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """获取日趋势数据."""
        method_start = time.time()
        logger.info("[get_daily_trend] 开始处理: source_id=%s", source_id)
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        query, params = self._build_daily_trend_trace_query(
            source_id=source_id,
            start_date=start_date,
            end_date=end_date,
            bbk_filter_sql=bbk_filter_sql,
            bbk_filter_params=bbk_filter_params,
        )
        rows = await self._db.fetch_all(query, params)

        # 查询已读任务数
        read_tasks_query, read_tasks_params = (
            self._build_daily_trend_read_tasks_query(
                source_id=source_id,
                start_date=start_date,
                end_date=end_date,
                bbk_filter_sql=bbk_filter_sql,
                bbk_filter_params=bbk_filter_params,
            )
        )
        read_tasks_rows = await self._db.fetch_all(
            read_tasks_query,
            read_tasks_params,
        )
        read_tasks_map = self._build_daily_trend_read_tasks_map(
            read_tasks_rows,
        )

        click_map: dict[str, dict[str, int]] = {}
        if self._should_query_extended_trend_metrics(source_id):
            click_query, click_params = self._build_daily_trend_click_query(
                source_id=source_id,
                start_date=start_date,
                end_date=end_date,
                bbk_filter_sql=bbk_filter_sql,
                bbk_filter_params=bbk_filter_params,
            )
            click_rows = await self._db.fetch_all(click_query, click_params)
            click_map = self._build_daily_trend_click_map(click_rows)

        logger.info(
            "[get_daily_trend] 方法总耗时: %.3fms",
            (time.time() - method_start) * 1000,
        )
        return self._build_daily_trend_response(
            rows=rows,
            read_tasks_map=read_tasks_map,
            click_map=click_map,
        )

    def _build_daily_trend_trace_query(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: list[str],
    ) -> tuple[str, tuple[Any, ...]]:
        """构建日趋势主查询 SQL。"""
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT
                    DATE(start_time) as date,
                    COUNT(*) as calls,
                    COALESCE(SUM(total_tokens), 0) as tokens,
                    COUNT(DISTINCT user_id) as users
                FROM swe_tracing_traces
                WHERE start_time >= %s AND start_time <= %s
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY DATE(start_time)
                ORDER BY date
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            return query, params

        query = f"""
            SELECT
                DATE(start_time) as date,
                COUNT(*) as calls,
                COALESCE(SUM(total_tokens), 0) as tokens,
                COUNT(DISTINCT user_id) as users
            FROM swe_tracing_traces
            WHERE source_id = %s AND start_time >= %s AND start_time <= %s
              AND user_id != 'default'{bbk_filter_sql}
            GROUP BY DATE(start_time)
            ORDER BY date
        """
        params = (source_id, start_date, end_date, *bbk_filter_params)
        return query, params

    def _build_daily_trend_read_tasks_query(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: list[str],
    ) -> tuple[str, tuple[Any, ...]]:
        """构建日趋势已读任务查询 SQL。"""
        source_filter_sql, source_filter_params = (
            self._build_trend_source_filter(
                source_id=source_id,
                column_name="j.source_id",
            )
        )
        read_tasks_query = f"""
            SELECT
                DATE(e.read_at) as date,
                COUNT(*) as read_tasks
            FROM swe_cron_executions e
            INNER JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.is_final = 1 AND e.read_at >= %s AND e.read_at <= %s
              AND j.status != 'deleted'
              AND j.deleted_at IS NULL
              AND e.read_at IS NOT NULL
              {source_filter_sql}
              {bbk_filter_sql.replace('bbk_id', 'j.bbk_id')}
            GROUP BY DATE(e.read_at)
        """
        return read_tasks_query, (
            start_date,
            end_date,
            *source_filter_params,
            *bbk_filter_params,
        )

    def _build_daily_trend_click_query(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: list[str],
    ) -> tuple[str, tuple[Any, ...]]:
        """构建日趋势客户点击查询 SQL。"""
        source_filter_sql, source_filter_params = (
            self._build_trend_source_filter(
                source_id=source_id,
                column_name="source_id",
            )
        )
        click_query = f"""
            SELECT
                DATE(clicked_at) as date,
                CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END as button_type,
                COUNT(DISTINCT CONCAT(COALESCE(cron_task_id, ''), '|', COALESCE(customer_id, ''))) as customer_count
            FROM swe_html_preview_click_events
            WHERE clicked_at >= %s AND clicked_at <= %s
              AND ((event_type = 'preview_view' AND template_type = 'sub')
                   OR (button_type IN ('insight', 'phone') AND event_type = 'button_click'))
              AND cron_task_id IS NOT NULL
              AND customer_id IS NOT NULL
              {source_filter_sql}{bbk_filter_sql}
            GROUP BY DATE(clicked_at), CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END
        """
        return click_query, (
            start_date,
            end_date,
            *source_filter_params,
            *bbk_filter_params,
        )

    def _build_trend_source_filter(
        self,
        source_id: str,
        column_name: str,
    ) -> tuple[str, list[str]]:
        """构建趋势附属查询的 source_id 过滤片段。"""
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            return (
                f"AND {column_name} NOT IN ({exclude_placeholders})",
                [*EXCLUDED_SOURCE_IDS],
            )
        return (f"AND {column_name} = %s", [source_id])

    def _should_query_extended_trend_metrics(self, source_id: str) -> bool:
        """仅对特定来源查询客户点击类趋势指标。"""
        return source_id in {"all", EXTENDED_TREND_SOURCE_ID}

    def _build_daily_trend_read_tasks_map(
        self,
        read_tasks_rows: list[dict[str, Any]],
    ) -> dict[str, int]:
        """构建日期到已读任务数的映射。"""
        return {
            self._format_daily_trend_date(row["date"]): row["read_tasks"] or 0
            for row in read_tasks_rows
        }

    def _build_daily_trend_click_map(
        self,
        click_rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, int]]:
        """按日期与按钮类型聚合客户点击数据。"""
        click_map: dict[str, dict[str, int]] = {}
        for row in click_rows:
            date_key = self._format_daily_trend_date(row["date"])
            if date_key not in click_map:
                click_map[date_key] = {"plan": 0, "insight": 0, "phone": 0}
            btn_type = row["button_type"]
            if btn_type in click_map[date_key]:
                click_map[date_key][btn_type] = row["customer_count"] or 0
        return click_map

    def _build_daily_trend_response(
        self,
        rows: list[dict[str, Any]],
        read_tasks_map: dict[str, int],
        click_map: dict[str, dict[str, int]],
    ) -> list[dict[str, Any]]:
        """组装日趋势返回结果。"""
        result: list[dict[str, Any]] = []
        for row in rows:
            date_key = self._format_daily_trend_date(row["date"])
            click_stats = click_map.get(date_key, {})
            result.append(
                {
                    "date": date_key,
                    "calls": row["calls"] or 0,
                    "tokens": row["tokens"] or 0,
                    "users": row["users"] or 0,
                    "read_tasks": read_tasks_map.get(date_key, 0),
                    "plan_customers": click_stats.get("plan", 0),
                    "insight_customers": click_stats.get("insight", 0),
                    "phone_customers": click_stats.get("phone", 0),
                },
            )
        return result

    def _format_daily_trend_date(self, value: Any) -> str:
        """格式化日趋势中的日期字段。"""
        return value.strftime("%Y-%m-%d") if value else ""

    async def get_hourly_trend(
        self,
        source_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """获取单日按小时聚合的趋势数据。"""
        if start_date is None:
            start_date = datetime.now().replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        if end_date is None:
            end_date = start_date + timedelta(days=1)

        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT
                    HOUR(start_time) as hour_bucket,
                    COUNT(*) as calls,
                    COALESCE(SUM(total_tokens), 0) as tokens,
                    COUNT(DISTINCT user_id) as users
                FROM swe_tracing_traces
                WHERE start_time >= %s AND start_time <= %s
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY HOUR(start_time)
                ORDER BY hour_bucket
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            rows = await self._db.fetch_all(query, params)
        else:
            query = f"""
                SELECT
                    HOUR(start_time) as hour_bucket,
                    COUNT(*) as calls,
                    COALESCE(SUM(total_tokens), 0) as tokens,
                    COUNT(DISTINCT user_id) as users
                FROM swe_tracing_traces
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY HOUR(start_time)
                ORDER BY hour_bucket
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            rows = await self._db.fetch_all(query, params)

        hour_map = {
            int(row["hour_bucket"]): {
                "calls": row["calls"] or 0,
                "tokens": row["tokens"] or 0,
                "users": row["users"] or 0,
            }
            for row in rows
        }

        # 查询已读任务数（按小时）
        read_source_filter_sql, read_source_filter_params = (
            self._build_trend_source_filter(
                source_id=source_id,
                column_name="j.source_id",
            )
        )
        read_tasks_query = f"""
            SELECT
                HOUR(e.read_at) as hour_bucket,
                COUNT(*) as read_tasks
            FROM swe_cron_executions e
            INNER JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.is_final = 1 AND e.read_at >= %s AND e.read_at <= %s
              AND j.status != 'deleted'
              AND j.deleted_at IS NULL
              AND e.read_at IS NOT NULL
              {read_source_filter_sql}{bbk_filter_sql.replace('bbk_id', 'j.bbk_id')}
            GROUP BY HOUR(e.read_at)
        """
        read_tasks_params = (
            start_date,
            end_date,
            *read_source_filter_params,
            *bbk_filter_params,
        )
        read_tasks_rows = await self._db.fetch_all(
            read_tasks_query,
            read_tasks_params,
        )
        read_tasks_hour_map = {
            int(row["hour_bucket"]): row["read_tasks"] or 0
            for row in read_tasks_rows
        }

        click_hour_map: dict[int, dict[str, int]] = {}
        if self._should_query_extended_trend_metrics(source_id):
            click_source_filter_sql, click_source_filter_params = (
                self._build_trend_source_filter(
                    source_id=source_id,
                    column_name="source_id",
                )
            )
            click_query = f"""
                SELECT
                    HOUR(clicked_at) as hour_bucket,
                    CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END as button_type,
                    COUNT(DISTINCT CONCAT(COALESCE(cron_task_id, ''), '|', COALESCE(customer_id, ''))) as customer_count
                FROM swe_html_preview_click_events
                WHERE clicked_at >= %s AND clicked_at <= %s
                  AND ((event_type = 'preview_view' AND template_type = 'sub')
                       OR (button_type IN ('insight', 'phone') AND event_type = 'button_click'))
                  AND cron_task_id IS NOT NULL
                  AND customer_id IS NOT NULL
                  {click_source_filter_sql}{bbk_filter_sql}
                GROUP BY HOUR(clicked_at), CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END
            """
            click_params = (
                start_date,
                end_date,
                *click_source_filter_params,
                *bbk_filter_params,
            )
            click_rows = await self._db.fetch_all(click_query, click_params)

            # 按小时和类型组织数据
            for row in click_rows:
                hour_key = int(row["hour_bucket"])
                if hour_key not in click_hour_map:
                    click_hour_map[hour_key] = {
                        "plan": 0,
                        "insight": 0,
                        "phone": 0,
                    }
                btn_type = row["button_type"]
                if btn_type in click_hour_map[hour_key]:
                    click_hour_map[hour_key][btn_type] = (
                        row["customer_count"] or 0
                    )

        day_prefix = start_date.strftime("%Y-%m-%d")
        # 判断是否是今天：如果是今天，只返回到当前小时，避免显示未来无意义的时间点
        now = datetime.now()
        is_today = start_date.date() == now.date()
        max_hour = now.hour if is_today else 23
        return [
            {
                "date": f"{day_prefix} {hour:02d}:00",
                "calls": hour_map.get(hour, {}).get("calls", 0),
                "tokens": hour_map.get(hour, {}).get("tokens", 0),
                "users": hour_map.get(hour, {}).get("users", 0),
                "read_tasks": read_tasks_hour_map.get(hour, 0),
                "plan_customers": click_hour_map.get(hour, {}).get("plan", 0),
                "insight_customers": click_hour_map.get(hour, {}).get(
                    "insight",
                    0,
                ),
                "phone_customers": click_hour_map.get(hour, {}).get(
                    "phone",
                    0,
                ),
            }
            for hour in range(max_hour + 1)
        ]

    async def get_channel_distribution(
        self,
        source_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """获取渠道分布统计."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT
                    source_id,
                    COUNT(DISTINCT user_id) as user_count,
                    COUNT(*) as call_count,
                    SUM(total_tokens) as token_count
                FROM swe_tracing_traces
                WHERE start_time >= %s AND start_time <= %s
                  AND source_id IS NOT NULL AND source_id != ''
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'
                GROUP BY source_id
                ORDER BY call_count DESC
            """
            rows = await self._db.fetch_all(
                query,
                (start_date, end_date, *EXCLUDED_SOURCE_IDS),
            )
        else:
            query = """
                SELECT
                    source_id,
                    COUNT(DISTINCT user_id) as user_count,
                    COUNT(*) as call_count,
                    SUM(total_tokens) as token_count
                FROM swe_tracing_traces
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND user_id != 'default'
                GROUP BY source_id
                ORDER BY call_count DESC
            """
            rows = await self._db.fetch_all(
                query,
                (source_id, start_date, end_date),
            )

        platform_user_dist = []
        platform_call_dist = []
        sources = []

        for row in rows:
            src_id = row["source_id"]
            sources.append(src_id)
            platform_user_dist.append(
                {"name": src_id, "value": row["user_count"] or 0},
            )
            platform_call_dist.append(
                {"name": src_id, "value": row["call_count"] or 0},
            )

        return {
            "platformUserDistribution": platform_user_dist,
            "platformCallDistribution": platform_call_dist,
            "totalPlatforms": len(sources),
        }

    async def get_sources(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> list[str]:
        """获取平台来源列表."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
        query = f"""
            SELECT DISTINCT source_id
            FROM swe_tracing_traces
            WHERE start_time >= %s AND start_time <= %s
              AND source_id IS NOT NULL AND source_id != ''
              AND source_id NOT IN ({exclude_placeholders})
            ORDER BY source_id
        """
        rows = await self._db.fetch_all(
            query,
            (start_date, end_date, *EXCLUDED_SOURCE_IDS),
        )
        return [row["source_id"] for row in rows]

    # ===== 用户分析 =====

    async def get_users(
        self,
        source_id: str,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        sort_by: Optional[str] = None,
        filter_user_type: Optional[str] = "filtered",
        bbk_ids: Optional[str] = None,
        metric_type: Optional[str] = None,
    ) -> tuple[list[UserListItem], int]:
        """获取用户列表（同时返回三个口径统计数据）.

        Args:
            filter_user_type: 'filtered' 过滤80/IT开头用户，'all' 仅过滤default用户
            metric_type: 口径类型（仅影响默认排序，不影响返回字段）
        """
        method_start = time.time()
        logger.info(
            "[get_users] 开始处理请求: source_id=%s, bbk_ids=%s, start_date=%s, end_date=%s, filter_user_type=%s, sort_by=%s",
            source_id,
            bbk_ids,
            start_date,
            end_date,
            filter_user_type,
            sort_by,
        )

        # 排序映射表（按四列依次降序：任务执行数、任务成功数、结果查看数、主动调用数）
        order_by_map = {
            "manual": "cron_executions DESC, cron_success DESC, cron_reads DESC, manual_calls DESC, user_id ASC",
            "cron_exec": "cron_executions DESC, cron_success DESC, cron_reads DESC, manual_calls DESC, user_id ASC",
            "cron_read": "cron_executions DESC, cron_success DESC, cron_reads DESC, manual_calls DESC, user_id ASC",
            "manual_calls": "cron_executions DESC, cron_success DESC, cron_reads DESC, manual_calls DESC, user_id ASC",
            "cron_executions": "cron_executions DESC, cron_success DESC, cron_reads DESC, manual_calls DESC, user_id ASC",
            "cron_success": "cron_executions DESC, cron_success DESC, cron_reads DESC, manual_calls DESC, user_id ASC",
            "cron_reads": "cron_executions DESC, cron_success DESC, cron_reads DESC, manual_calls DESC, user_id ASC",
            "last_active": "last_active DESC, user_id ASC",
        }

        # 根据参数选择排序
        if sort_by and sort_by in order_by_map:
            order_by = order_by_map[sort_by]
        else:
            # 默认按四列依次降序排序
            order_by = order_by_map["manual"]

        # 构建 WHERE 条件和参数
        step1_start = time.time()
        where_sql, params = self._build_traces_where_clause(
            source_id,
            filter_user_type,
            user_id,
            bbk_ids,
            start_date,
            end_date,
        )
        logger.info(
            "[get_users] 构建 WHERE 条件耗时: %.3fms",
            (time.time() - step1_start) * 1000,
        )

        # 查询总数
        step2_start = time.time()
        count_query = f"SELECT COUNT(DISTINCT user_id) as total FROM swe_tracing_traces t WHERE {where_sql}"
        count_row = await self._db.fetch_one(count_query, tuple(params))
        total = count_row["total"] if count_row else 0
        logger.info(
            "[get_users] 查询总数耗时: %.3fms, total=%d",
            (time.time() - step2_start) * 1000,
            total,
        )

        # 构建 cron 子查询
        step3_start = time.time()
        cron_subquery_sql, cron_params = self._build_cron_subquery(
            source_id=source_id,
            start_date=start_date,
            end_date=end_date,
            bbk_ids=bbk_ids,
        )
        logger.info(
            "[get_users] 构建 cron 子查询耗时: %.3fms",
            (time.time() - step3_start) * 1000,
        )

        # 构建主查询
        step4_start = time.time()
        offset = (page - 1) * page_size
        query, final_params = self._build_users_query(
            source_id=source_id,
            where_sql=where_sql,
            cron_subquery_sql=cron_subquery_sql,
            order_by=order_by,
            params=params,
            cron_params=cron_params,
            page_size=page_size,
            offset=offset,
            bbk_ids=bbk_ids,
            start_date=start_date,
            end_date=end_date,
        )
        logger.info(
            "[get_users] 构建主查询耗时: %.3fms",
            (time.time() - step4_start) * 1000,
        )

        # 执行主查询
        step5_start = time.time()
        rows = await self._db.fetch_all(query, tuple(final_params))
        logger.info(
            "[get_users] 执行主查询耗时: %.3fms, 返回%d行",
            (time.time() - step5_start) * 1000,
            len(rows),
        )

        # 构建返回结果
        step6_start = time.time()
        users = [self._build_user_list_item(row) for row in rows]
        logger.info(
            "[get_users] 构建结果耗时: %.3fms",
            (time.time() - step6_start) * 1000,
        )

        logger.info(
            "[get_users] 方法总耗时: %.3fms, source_id=%s, bbk_ids=%s",
            (time.time() - method_start) * 1000,
            source_id,
            bbk_ids,
        )
        return users, total

    def _build_traces_where_clause(
        self,
        source_id: str,
        filter_user_type: Optional[str],
        user_id: Optional[str],
        bbk_ids: Optional[str],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> tuple[str, list[Any]]:
        """构建 swe_tracing_traces 表的 WHERE 条件."""
        where_clauses: list[str] = []
        params: list[Any] = []

        # source_id 过滤
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            where_clauses.append(
                f"t.source_id NOT IN ({exclude_placeholders})",
            )
            params.extend(EXCLUDED_SOURCE_IDS)
        else:
            where_clauses.append("t.source_id = %s")
            params.append(source_id)

        # 用户过滤
        where_clauses.append("t.user_id != %s")
        params.append("default")
        if filter_user_type == "filtered":
            where_clauses.append(
                "(t.user_id NOT LIKE %s AND t.user_id NOT LIKE %s AND t.user_id != %s)",
            )
            params.extend(["80%%", "IT%%", "agent_default"])

        if user_id:
            where_clauses.append("t.user_id LIKE %s")
            params.append(f"%%{user_id}%%")

        if bbk_ids:
            bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
            where_clauses.append(
                f"t.bbk_id IN ({', '.join(['%s'] * len(bbk_params))})",
            )
            params.extend(bbk_params)

        if start_date:
            where_clauses.append("t.start_time >= %s")
            params.append(start_date)

        if end_date:
            where_clauses.append("t.start_time < %s")
            params.append(end_date)

        return " AND ".join(where_clauses), params

    def _build_cron_subquery(
        self,
        source_id: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        bbk_ids: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        """构建 cron 执行统计子查询的 WHERE 条件."""
        cron_where: list[str] = [
            "j.status != %s",
            "j.deleted_at IS NULL",
            "e.is_final = 1",
        ]
        cron_params: list[Any] = ["deleted"]

        if start_date:
            cron_where.append("e.actual_time >= %s")
            cron_params.append(start_date)

        if end_date:
            cron_where.append("e.actual_time < %s")
            cron_params.append(end_date)

        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            cron_where.append(f"j.source_id NOT IN ({exclude_placeholders})")
            cron_params.extend(EXCLUDED_SOURCE_IDS)
        else:
            cron_where.append("j.source_id = %s")
            cron_params.append(source_id)

        if bbk_ids:
            _, bbk_params = build_cron_bbk_in_filter(bbk_ids)
            placeholders = ", ".join(["%s"] * len(bbk_params))
            cron_where.append(f"j.bbk_id IN ({placeholders})")
            cron_params.extend(bbk_params)

        return " AND ".join(cron_where), cron_params

    def _build_users_query(
        self,
        source_id: str,
        where_sql: str,
        cron_subquery_sql: str,
        order_by: str,
        params: list[Any],
        cron_params: list[Any],
        page_size: int,
        offset: int,
        bbk_ids: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> tuple[str, list[Any]]:
        """Build the user list query and its ordered parameters."""
        _, bbk_subquery_params = build_bbk_in_filter(bbk_ids)
        bbk_in_clause = ""
        if bbk_subquery_params:
            placeholders = ", ".join(["%s"] * len(bbk_subquery_params))
            bbk_in_clause = f" AND tr.bbk_id IN ({placeholders})"
        exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))

        if source_id == "all":
            # source_id == "all": total_skills 子查询不加 source_id 过滤
            query = f"""
                SELECT t.user_id,
                       COUNT(DISTINCT t.session_id) as total_sessions,
                       COUNT(*) as total_conversations,
                       SUM(t.total_tokens) as total_tokens,
                       MAX(t.start_time) as last_active,
                       COUNT(CASE WHEN t.session_id NOT LIKE 'cron-task:%%' THEN 1 END) as manual_calls,
                       COALESCE(MAX(ce.cron_executions), 0) as cron_executions,
                       COALESCE(MAX(ce.cron_success), 0) as cron_success,
                       COALESCE(MAX(sk.skill_count), 0) as total_skills,
                       MAX(t.user_name) as user_name,
                       MAX(t.bbk_id) as bbk_id,
                       COALESCE(MAX(ce.cron_reads), 0) as cron_reads
                FROM swe_tracing_traces t
                LEFT JOIN (
                    SELECT j.tenant_id as user_id,
                           COUNT(*) as cron_executions,
                           SUM(CASE WHEN e.status = 'success' AND e.async_status = 'success' THEN 1 ELSE 0 END) as cron_success,
                           SUM(CASE WHEN e.is_read = TRUE THEN 1 ELSE 0 END) as cron_reads
                    FROM swe_cron_executions e
                    INNER JOIN swe_cron_jobs j ON e.job_id = j.id
                    WHERE {cron_subquery_sql}
                    GROUP BY j.tenant_id
                ) ce ON ce.user_id = t.user_id
                LEFT JOIN (
                    SELECT tr.user_id, COUNT(*) as skill_count
                    FROM swe_tracing_spans s
                    INNER JOIN swe_tracing_traces tr ON s.trace_id = tr.trace_id
                    WHERE s.skill_name IS NOT NULL
                      AND tr.source_id NOT IN ({exclude_placeholders})
                      AND s.source_id NOT IN ({exclude_placeholders}){bbk_in_clause}{" AND tr.start_time >= %s" if start_date else ""}{" AND tr.start_time < %s" if end_date else ""}
                    GROUP BY tr.user_id
                ) sk ON sk.user_id = t.user_id
                WHERE {where_sql}
                GROUP BY t.user_id
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
            """
            skill_date_params = []
            if start_date:
                skill_date_params.append(start_date)
            if end_date:
                skill_date_params.append(end_date)
            final_params = (
                cron_params
                + list(EXCLUDED_SOURCE_IDS)
                + list(EXCLUDED_SOURCE_IDS)
                + bbk_subquery_params
                + skill_date_params
                + params
                + [page_size, offset]
            )
        else:
            # source_id != "all": total_skills 子查询加 source_id 过滤
            query = f"""
                SELECT t.user_id,
                       COUNT(DISTINCT t.session_id) as total_sessions,
                       COUNT(*) as total_conversations,
                       SUM(t.total_tokens) as total_tokens,
                       MAX(t.start_time) as last_active,
                       COUNT(CASE WHEN t.session_id NOT LIKE 'cron-task:%%' THEN 1 END) as manual_calls,
                       COALESCE(MAX(ce.cron_executions), 0) as cron_executions,
                       COALESCE(MAX(ce.cron_success), 0) as cron_success,
                       COALESCE(MAX(sk.skill_count), 0) as total_skills,
                       MAX(t.user_name) as user_name,
                       MAX(t.bbk_id) as bbk_id,
                       COALESCE(MAX(ce.cron_reads), 0) as cron_reads
                FROM swe_tracing_traces t
                LEFT JOIN (
                    SELECT j.tenant_id as user_id,
                           COUNT(*) as cron_executions,
                           SUM(CASE WHEN e.status = 'success' AND e.async_status = 'success' THEN 1 ELSE 0 END) as cron_success,
                           SUM(CASE WHEN e.is_read = TRUE THEN 1 ELSE 0 END) as cron_reads
                    FROM swe_cron_executions e
                    INNER JOIN swe_cron_jobs j ON e.job_id = j.id
                    WHERE {cron_subquery_sql}
                    GROUP BY j.tenant_id
                ) ce ON ce.user_id = t.user_id
                LEFT JOIN (
                    SELECT tr.user_id, COUNT(*) as skill_count
                    FROM swe_tracing_spans s
                    INNER JOIN swe_tracing_traces tr ON s.trace_id = tr.trace_id
                    WHERE s.skill_name IS NOT NULL
                      AND tr.source_id = %s
                      AND s.source_id = %s{bbk_in_clause}{" AND tr.start_time >= %s" if start_date else ""}{" AND tr.start_time < %s" if end_date else ""}
                    GROUP BY tr.user_id
                ) sk ON sk.user_id = t.user_id
                WHERE {where_sql}
                GROUP BY t.user_id
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
            """
            skill_date_params = []
            if start_date:
                skill_date_params.append(start_date)
            if end_date:
                skill_date_params.append(end_date)
            final_params = (
                cron_params
                + [source_id, source_id]
                + bbk_subquery_params
                + skill_date_params
                + params
                + [page_size, offset]
            )

        return query, final_params

    def _build_user_list_item(self, row: dict) -> UserListItem:
        """从查询结果构建 UserListItem."""
        return UserListItem(
            user_id=row["user_id"],
            user_name=row["user_name"],
            bbk_id=row["bbk_id"],
            total_sessions=row["total_sessions"] or 0,
            total_conversations=row["total_conversations"] or 0,
            total_tokens=row["total_tokens"] or 0,
            total_skills=row["total_skills"] or 0,
            last_active=row["last_active"],
            manual_calls=row["manual_calls"] or 0,
            cron_executions=row["cron_executions"] or 0,
            cron_success=row["cron_success"] or 0,
            cron_reads=row["cron_reads"] or 0,
        )

    async def _get_users_cron_reads(
        self,
        source_id: str,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        order_by: str = "cron_reads DESC, user_id ASC",
        filter_user_type: Optional[str] = "filtered",
        bbk_ids: Optional[str] = None,
    ) -> tuple[list[UserListItem], int]:
        """按用户维度统计定时任务结果查看数."""
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            where_clauses = [
                f"j.source_id NOT IN ({exclude_placeholders})",
            ]
            params: list[Any] = list(EXCLUDED_SOURCE_IDS)
        else:
            where_clauses = ["j.source_id = %s"]
            params = [source_id]

        # 用户过滤逻辑：tenant_id 即用户 ID
        where_clauses.append("j.tenant_id != %s")
        params.append("default")
        if filter_user_type == "filtered":
            where_clauses.append(
                "(j.tenant_id NOT LIKE %s AND j.tenant_id NOT LIKE %s)",
            )
            params.append("80%")
            params.append("IT%")

        if user_id:
            where_clauses.append("j.tenant_id LIKE %s")
            params.append(f"%{user_id}%")
        if bbk_ids:
            bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
            where_clauses.append(
                f"j.bbk_id IN ({', '.join(['%s'] * len(bbk_params))})",
            )
            params.extend(bbk_params)
        if start_date:
            where_clauses.append("e.actual_time >= %s")
            params.append(start_date)
        if end_date:
            where_clauses.append("e.actual_time < %s")
            params.append(end_date)

        # 排除已删除任务
        where_clauses.append("j.status != %s")
        params.append("deleted")
        where_clauses.append("j.deleted_at IS NULL")

        where_clauses.append("e.is_final = 1")
        where_sql = " AND ".join(where_clauses)

        # 统计总数
        count_query = f"""
            SELECT COUNT(DISTINCT j.tenant_id) as total
            FROM swe_cron_executions e
            INNER JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {where_sql}
        """
        count_row = await self._db.fetch_one(count_query, tuple(params))
        total = count_row["total"] if count_row else 0

        offset = (page - 1) * page_size
        query = f"""
            SELECT
                j.tenant_id as user_id,
                MAX(j.tenant_name) as user_name,
                MAX(j.bbk_id) as bbk_id,
                COUNT(*) as cron_executions,
                SUM(CASE WHEN e.is_read = TRUE THEN 1 ELSE 0 END) as cron_reads,
                MAX(CASE WHEN e.is_read = TRUE THEN e.read_at ELSE e.actual_time END) as last_active
            FROM swe_cron_executions e
            INNER JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {where_sql}
            GROUP BY j.tenant_id
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
        """
        params.extend([page_size, offset])

        rows = await self._db.fetch_all(query, tuple(params))
        users = [
            UserListItem(
                user_id=row["user_id"],
                user_name=row["user_name"],
                bbk_id=row["bbk_id"],
                total_sessions=0,
                total_conversations=0,
                total_tokens=0,
                total_skills=0,
                last_active=row["last_active"],
                manual_calls=0,
                cron_executions=row["cron_executions"] or 0,
                cron_reads=row["cron_reads"] or 0,
            )
            for row in rows
        ]
        return users, total

    async def get_user_stats(
        self,
        source_id: str,
        user_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> UserStats:
        """获取用户统计详情."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now()

        stats_row = await self._fetch_user_stats_row(
            source_id,
            user_id,
            start_date,
            end_date,
            bbk_ids,
        )
        model_usage, tools_used, skills_used, mcp_tools_used = (
            await self._fetch_user_usage_data(
                source_id,
                user_id,
                start_date,
                end_date,
                bbk_ids,
            )
        )

        return self._build_user_stats(
            user_id=user_id,
            stats_row=stats_row,
            model_usage=model_usage,
            tools_used=tools_used,
            skills_used=skills_used,
            mcp_tools_used=mcp_tools_used,
        )

    async def _fetch_user_stats_row(
        self,
        source_id: str,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> Optional[dict]:
        """获取用户统计行数据."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            query = f"""
                SELECT
                    COUNT(DISTINCT session_id) as total_sessions,
                    COUNT(*) as total_conversations,
                    SUM(total_input_tokens) as input_tokens,
                    SUM(total_output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens,
                    AVG(duration_ms) as avg_duration
                FROM swe_tracing_traces
                WHERE user_id = %s AND start_time >= %s AND start_time <= %s{bbk_filter_sql}
            """
            return await self._db.fetch_one(
                query,
                (user_id, start_date, end_date, *bbk_filter_params),
            )

        query = f"""
            SELECT
                COUNT(DISTINCT session_id) as total_sessions,
                COUNT(*) as total_conversations,
                SUM(total_input_tokens) as input_tokens,
                SUM(total_output_tokens) as output_tokens,
                SUM(total_tokens) as total_tokens,
                AVG(duration_ms) as avg_duration
            FROM swe_tracing_traces
            WHERE source_id = %s AND user_id = %s AND start_time >= %s AND start_time <= %s{bbk_filter_sql}
        """
        return await self._db.fetch_one(
            query,
            (source_id, user_id, start_date, end_date, *bbk_filter_params),
        )

    async def _fetch_user_usage_data(
        self,
        source_id: str,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> tuple:
        """并行获取用户使用数据."""
        return await asyncio.gather(
            self._get_user_model_usage(
                source_id,
                user_id,
                start_date,
                end_date,
                bbk_ids,
            ),
            self._get_user_tool_usage(
                source_id,
                user_id,
                start_date,
                end_date,
                bbk_ids,
            ),
            self._get_user_skill_usage(
                source_id,
                user_id,
                start_date,
                end_date,
                bbk_ids,
            ),
            self._get_user_mcp_tool_usage(
                source_id,
                user_id,
                start_date,
                end_date,
                bbk_ids,
            ),
        )

    def _build_user_stats(
        self,
        user_id: str,
        stats_row: Optional[dict],
        model_usage: list,
        tools_used: list,
        skills_used: list,
        mcp_tools_used: list,
    ) -> UserStats:
        """构建用户统计对象."""
        return UserStats(
            user_id=user_id,
            model_usage=model_usage,
            total_tokens=stats_row["total_tokens"] or 0 if stats_row else 0,
            input_tokens=stats_row["input_tokens"] or 0 if stats_row else 0,
            output_tokens=stats_row["output_tokens"] or 0 if stats_row else 0,
            total_sessions=(
                stats_row["total_sessions"] or 0 if stats_row else 0
            ),
            total_conversations=(
                stats_row["total_conversations"] or 0 if stats_row else 0
            ),
            avg_duration_ms=self._extract_avg_duration(stats_row),
            tools_used=tools_used,
            skills_used=skills_used,
            mcp_tools_used=mcp_tools_used,
        )

    # ===== 私有辅助方法 =====

    async def _get_total_users(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> tuple[int, int, int]:
        """获取用户总数、IT人员数和业务人员数."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT
                    COUNT(DISTINCT user_id) as total_users,
                    COUNT(DISTINCT CASE WHEN user_id LIKE '80%%' OR user_id LIKE 'IT%%' OR user_id = 'agent_default' THEN user_id END) as it_users,
                    COUNT(DISTINCT CASE WHEN user_id NOT LIKE '80%%' AND user_id NOT LIKE 'IT%%' AND user_id != 'agent_default' THEN user_id END) as business_users
                FROM swe_tracing_traces
                WHERE start_time >= %s AND start_time <= %s
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            row = await self._db.fetch_one(query, params)
        else:
            query = f"""
                SELECT
                    COUNT(DISTINCT user_id) as total_users,
                    COUNT(DISTINCT CASE WHEN user_id LIKE '80%%' OR user_id LIKE 'IT%%' OR user_id = 'agent_default' THEN user_id END) as it_users,
                    COUNT(DISTINCT CASE WHEN user_id NOT LIKE '80%%' AND user_id NOT LIKE 'IT%%' AND user_id != 'agent_default' THEN user_id END) as business_users
                FROM swe_tracing_traces
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND user_id != 'default'{bbk_filter_sql}
            """
            params = (
                source_id,
                start_date,
                end_date,
                *bbk_filter_params,
            )
            row = await self._db.fetch_one(query, params)

        if row is None:
            return (0, 0, 0)

        total_users = row.get("total_users") or 0
        it_users = row.get("it_users") or 0
        business_users = row.get("business_users") or 0

        return (total_users, it_users, business_users)

    async def _get_online_users(
        self,
        source_id: str,
        bbk_ids: Optional[str] = None,
    ) -> tuple[int, list[str]]:
        """获取在线用户."""
        online_threshold = datetime.now() - timedelta(minutes=5)
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            query = f"""
                SELECT DISTINCT user_id
                FROM swe_tracing_spans
                WHERE start_time >= %s AND user_id IS NOT NULL AND user_id != ''{bbk_filter_sql}
            """
            params = (online_threshold, *bbk_filter_params)
            rows = await self._db.fetch_all(query, params)
        else:
            query = f"""
                SELECT DISTINCT user_id
                FROM swe_tracing_spans
                WHERE source_id = %s AND start_time >= %s AND user_id IS NOT NULL AND user_id != ''{bbk_filter_sql}
            """
            params = (source_id, online_threshold, *bbk_filter_params)
            rows = await self._db.fetch_all(query, params)
        user_ids = [row["user_id"] for row in rows if row["user_id"]]
        return len(user_ids), user_ids

    async def _get_token_stats(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> Optional[dict]:
        """获取 Token 统计."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT
                    SUM(total_input_tokens) as input_tokens,
                    SUM(total_output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens,
                    COUNT(*) as total_traces,
                    COUNT(DISTINCT session_id) as total_sessions,
                    AVG(duration_ms) as avg_duration
                FROM swe_tracing_traces
                WHERE start_time >= %s AND start_time <= %s
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            return await self._db.fetch_one(query, params)
        else:
            query = f"""
                SELECT
                    SUM(total_input_tokens) as input_tokens,
                    SUM(total_output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens,
                    COUNT(*) as total_traces,
                    COUNT(DISTINCT session_id) as total_sessions,
                    AVG(duration_ms) as avg_duration
                FROM swe_tracing_traces
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND user_id != 'default'{bbk_filter_sql}
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            return await self._db.fetch_one(query, params)

    async def _get_model_distribution(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list[ModelUsage]:
        """获取模型分布."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT model_name, COUNT(*) as count,
                       SUM(total_input_tokens) as input_tokens,
                       SUM(total_output_tokens) as output_tokens,
                       SUM(total_tokens) as total_tokens
                FROM swe_tracing_traces
                WHERE start_time >= %s AND start_time <= %s AND model_name IS NOT NULL
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY model_name
                ORDER BY count DESC
                LIMIT 10
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            rows = await self._db.fetch_all(query, params)
        else:
            query = f"""
                SELECT model_name, COUNT(*) as count,
                       SUM(total_input_tokens) as input_tokens,
                       SUM(total_output_tokens) as output_tokens,
                       SUM(total_tokens) as total_tokens
                FROM swe_tracing_traces
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s AND model_name IS NOT NULL
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY model_name
                ORDER BY count DESC
                LIMIT 10
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            rows = await self._db.fetch_all(query, params)
        return [
            ModelUsage(
                model_name=row["model_name"],
                count=row["count"] or 0,
                total_tokens=row["total_tokens"] or 0,
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
            )
            for row in rows
        ]

    async def _get_top_tools(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list[ToolUsage]:
        """获取热门工具."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT tool_name, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND tool_name IS NOT NULL
                  AND mcp_server IS NULL
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY tool_name
                ORDER BY count DESC
                LIMIT 10
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            rows = await self._db.fetch_all(query, params)
        else:
            query = f"""
                SELECT tool_name, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND tool_name IS NOT NULL
                  AND mcp_server IS NULL
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY tool_name
                ORDER BY count DESC
                LIMIT 10
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            rows = await self._db.fetch_all(query, params)
        return [
            ToolUsage(
                tool_name=row["tool_name"],
                count=row["count"] or 0,
                avg_duration_ms=int(row["avg_duration"] or 0),
                error_count=row["error_count"] or 0,
            )
            for row in rows
        ]

    async def _get_top_skills(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list[SkillUsage]:
        """获取热门技能."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        skill_filter_sql, skill_filter_params = build_excluded_skill_filter()
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT MAX(NULLIF(skill_id, '')) as skill_id,
                       skill_name,
                       COUNT(DISTINCT trace_id) as count,
                       AVG(duration_ms) as avg_duration
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time <= %s
                  AND skill_name IS NOT NULL
                  {skill_filter_sql}
                  AND bbk_id IS NOT NULL AND bbk_id != ''
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY skill_name,
                    COALESCE(NULLIF(skill_id, ''), CONCAT('__NAME__:', skill_name))
                ORDER BY count DESC
                LIMIT 10
            """
            params = (
                start_date,
                end_date,
                *skill_filter_params,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            rows = await self._db.fetch_all(query, params)
        else:
            query = f"""
                SELECT MAX(NULLIF(skill_id, '')) as skill_id,
                       skill_name,
                       COUNT(DISTINCT trace_id) as count,
                       AVG(duration_ms) as avg_duration
                FROM swe_tracing_spans
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND skill_name IS NOT NULL
                  {skill_filter_sql}
                  AND bbk_id IS NOT NULL AND bbk_id != ''
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY skill_name,
                    COALESCE(NULLIF(skill_id, ''), CONCAT('__NAME__:', skill_name))
                ORDER BY count DESC
                LIMIT 10
            """
            params = (
                source_id,
                start_date,
                end_date,
                *skill_filter_params,
                *bbk_filter_params,
            )
            rows = await self._db.fetch_all(query, params)
        skill_ids = [row["skill_id"] for row in rows if row.get("skill_id")]
        display_mapping = await self._get_skill_display_mapping(skill_ids)
        return [
            SkillUsage(
                skill_name=row["skill_name"],
                skill_id=row.get("skill_id") or None,
                cn_name=(
                    display_mapping.get(row.get("skill_id") or "", {}).get(
                        "cn_name",
                    )
                    or None
                ),
                skill_description=(
                    display_mapping.get(row.get("skill_id") or "", {}).get(
                        "description",
                    )
                    or None
                ),
                count=row["count"] or 0,
                avg_duration_ms=int(row["avg_duration"] or 0),
            )
            for row in rows
        ]

    async def _get_total_skill_calls(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> int:
        """获取技能调用总次数（统计去重的技能+trace组合数）."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT COUNT(DISTINCT CONCAT(skill_name, '|', trace_id)) as total
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time <= %s
                  AND skill_name IS NOT NULL
                  AND bbk_id IS NOT NULL AND bbk_id != ''
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            row = await self._db.fetch_one(query, params)
        else:
            query = f"""
                SELECT COUNT(DISTINCT CONCAT(skill_name, '|', trace_id)) as total
                FROM swe_tracing_spans
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND skill_name IS NOT NULL
                  AND bbk_id IS NOT NULL AND bbk_id != ''
                  AND user_id != 'default'{bbk_filter_sql}
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            row = await self._db.fetch_one(query, params)
        return int((row or {}).get("total") or 0)

    async def _get_customer_click_stats(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> dict[str, int]:
        """获取客户点击行为统计.

        从 swe_html_preview_click_events 表按 button_type 分组，
        统计 cron_task_id + customer_id 去重计数.

        Returns:
            dict with keys: plan_customers, insight_customers, phone_customers
        """
        db = self._db

        # 构建 WHERE 条件
        conditions = ["clicked_at >= %s", "clicked_at < %s"]
        params: list = [start_date, end_date]

        if source_id != "all":
            conditions.append("source_id = %s")
            params.append(source_id)
        else:
            # 排除测试平台
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            conditions.append(f"source_id NOT IN ({exclude_placeholders})")
            params.extend(EXCLUDED_SOURCE_IDS)

        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if bbk_filter_sql:
            conditions.append(bbk_filter_sql[4:])  # 移除 "AND " 前缀
            params.extend(bbk_filter_params)

        where_clause = " AND ".join(conditions)

        # 查询各 button_type 的去重客户数
        query = f"""
            SELECT
                CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END as button_type,
                COUNT(DISTINCT CONCAT(COALESCE(cron_task_id, ''), '|', COALESCE(customer_id, ''))) as customer_count
            FROM swe_html_preview_click_events
            WHERE {where_clause}
                AND ((event_type = 'preview_view' AND template_type = 'sub')
                     OR (button_type IN ('insight', 'phone') AND event_type = 'button_click'))
                AND cron_task_id IS NOT NULL
                AND customer_id IS NOT NULL
            GROUP BY CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END
        """

        rows = await db.fetch_all(query, tuple(params))

        result = {
            "plan_customers": 0,
            "insight_customers": 0,
            "phone_customers": 0,
        }

        for row in rows:
            button_type = row["button_type"]
            if button_type == "plan":
                result["plan_customers"] = row["customer_count"] or 0
            elif button_type == "insight":
                result["insight_customers"] = row["customer_count"] or 0
            elif button_type == "phone":
                result["phone_customers"] = row["customer_count"] or 0

        return result

    async def _get_multi_round_ratio(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> float:
        """获取多轮会话占比(>3轮)的真实百分比.

        统计每个 session 的 trace 数量，计算超过3轮的 session 占比。
        """
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT
                    COUNT(*) as total_sessions,
                    SUM(CASE WHEN trace_count > 3 THEN 1 ELSE 0 END) as multi_round_sessions
                FROM (
                    SELECT session_id, COUNT(*) as trace_count
                    FROM swe_tracing_traces
                    WHERE start_time >= %s AND start_time <= %s
                      AND source_id NOT IN ({exclude_placeholders})
                      AND user_id != 'default'
                      AND session_id IS NOT NULL AND session_id != ''
                      {bbk_filter_sql}
                    GROUP BY session_id
                ) AS session_counts
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            row = await self._db.fetch_one(query, params)
        else:
            query = f"""
                SELECT
                    COUNT(*) as total_sessions,
                    SUM(CASE WHEN trace_count > 3 THEN 1 ELSE 0 END) as multi_round_sessions
                FROM (
                    SELECT session_id, COUNT(*) as trace_count
                    FROM swe_tracing_traces
                    WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                      AND user_id != 'default'
                      AND session_id IS NOT NULL AND session_id != ''
                      {bbk_filter_sql}
                    GROUP BY session_id
                ) AS session_counts
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            row = await self._db.fetch_one(query, params)

        total_sessions = int((row or {}).get("total_sessions") or 0)
        multi_round_sessions = int(
            (row or {}).get("multi_round_sessions") or 0,
        )
        if total_sessions == 0:
            return 0.0
        return round(multi_round_sessions / total_sessions * 100, 1)

    async def _get_avg_user_stay(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> int:
        """获取用户平均停留时长（秒）.

        统计每个用户在时间范围内从第一次请求到最后一次请求的时间差，
        然后计算平均值。
        """
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT AVG(stay_seconds) as avg_stay
                FROM (
                    SELECT user_id,
                           TIMESTAMPDIFF(SECOND, MIN(start_time), MAX(start_time)) as stay_seconds
                    FROM swe_tracing_traces
                    WHERE start_time >= %s AND start_time <= %s
                      AND source_id NOT IN ({exclude_placeholders})
                      AND user_id != 'default'
                      {bbk_filter_sql}
                    GROUP BY user_id
                    HAVING stay_seconds > 0
                ) AS user_stays
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            row = await self._db.fetch_one(query, params)
        else:
            query = f"""
                SELECT AVG(stay_seconds) as avg_stay
                FROM (
                    SELECT user_id,
                           TIMESTAMPDIFF(SECOND, MIN(start_time), MAX(start_time)) as stay_seconds
                    FROM swe_tracing_traces
                    WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                      AND user_id != 'default'
                      {bbk_filter_sql}
                    GROUP BY user_id
                    HAVING stay_seconds > 0
                ) AS user_stays
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            row = await self._db.fetch_one(query, params)

        avg_stay = float((row or {}).get("avg_stay") or 0)
        return int(avg_stay)

    async def _get_avg_duration_seconds(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> int:
        """获取平均对话时长（秒）.

        计算所有对话的平均耗时。
        """
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT AVG(duration_ms) as avg_duration_ms
                FROM swe_tracing_traces
                WHERE start_time >= %s AND start_time <= %s
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'
                  AND duration_ms IS NOT NULL
                  {bbk_filter_sql}
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            row = await self._db.fetch_one(query, params)
        else:
            query = f"""
                SELECT AVG(duration_ms) as avg_duration_ms
                FROM swe_tracing_traces
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND user_id != 'default'
                  AND duration_ms IS NOT NULL
                  {bbk_filter_sql}
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            row = await self._db.fetch_one(query, params)

        avg_duration_ms = float((row or {}).get("avg_duration_ms") or 0)
        # 转换为秒
        return int(avg_duration_ms / 1000)

    async def _get_statistics_eligible_skill_names(
        self,
        source_id: str,
    ) -> set[str]:
        """获取纳入统计的技能名称集合.

        从 swe_marketplace_skills 表查询 include_in_statistics = 1 的技能。

        Args:
            source_id: 应用入口标识

        Returns:
            纳入统计的技能名称集合
        """
        if not self._db or not self._db.is_connected:
            logger.warning(
                "Database not connected, return empty set for statistics filter",
            )
            return set()

        try:
            rows = await self._db.fetch_all(
                """
                SELECT skill_name FROM swe_marketplace_skills
                WHERE source_id = %s AND include_in_statistics = 1
                """,
                (source_id,),
            )
            return {row["skill_name"] for row in rows if row.get("skill_name")}
        except Exception as e:
            logger.warning("Failed to get statistics eligible skills: %s", e)
            return set()

    async def _get_skill_display_mapping(
        self,
        skill_ids: Optional[list[str]] = None,
    ) -> dict[str, dict[str, str]]:
        """获取技能展示映射（每个 skill_id 选出一条稳定记录）.

        swe_skills 同一 skill_id 可能有多个用户/租户记录，不能直接
        关联到 span 聚合结果，否则会放大调用次数。本方法在服务端先把
        技能表压缩为每个 skill_id 一条展示记录。

        排序优先级：cn_name 非空 > enabled=1 > updated_at DESC > id DESC。

        Args:
            skill_ids: 可选过滤列表；为空表示返回全部

        Returns:
            skill_id -> {"skill_name": ..., "cn_name": ...} 的映射
        """
        if not self._db or not self._db.is_connected:
            return {}

        if skill_ids is not None and not skill_ids:
            return {}

        if skill_ids:
            placeholders = ", ".join(["%s"] * len(skill_ids))
            skill_id_filter = f" AND skill_id IN ({placeholders})"
            params: tuple = tuple(skill_ids)
        else:
            skill_id_filter = ""
            params = ()

        sql = SKILL_DISPLAY_MAPPING_SQL_TEMPLATE.format(
            skill_id_filter=skill_id_filter,
        )
        try:
            rows = await self._db.fetch_all(sql, params)
        except Exception as e:
            logger.warning("Failed to load skill display mapping: %s", e)
            return {}

        mapping: dict[str, dict[str, str]] = {}
        for row in rows:
            mapping[row["skill_id"]] = {
                "skill_name": row.get("skill_name") or "",
                "cn_name": row.get("cn_name") or "",
                "description": row.get("description") or "",
            }
        return mapping

    async def get_skills_paginated(
        self,
        source_id: str,
        page: int = 1,
        page_size: int = 10,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> tuple[list[SkillUsage], int]:
        """获取技能调用排行榜（分页）."""
        method_start = time.time()
        logger.info(
            "[get_skills_paginated] 开始处理: source_id=%s, page=%s",
            source_id,
            page,
        )
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        # 获取纳入统计的技能名称集合
        eligible_skills = await self._get_statistics_eligible_skill_names(
            source_id,
        )

        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        skill_filter_sql, skill_filter_params = build_excluded_skill_filter()
        # 构建基础查询条件
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            base_where = f"""
                start_time >= %s AND start_time <= %s
                AND skill_name IS NOT NULL
                {skill_filter_sql}
                AND bbk_id IS NOT NULL AND bbk_id != ''
                AND source_id NOT IN ({exclude_placeholders})
                AND user_id != 'default'{bbk_filter_sql}
            """
            count_params = [
                start_date,
                end_date,
                *skill_filter_params,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            ]
        else:
            base_where = f"""
                source_id = %s AND start_time >= %s AND start_time <= %s
                AND skill_name IS NOT NULL
                {skill_filter_sql}
                AND bbk_id IS NOT NULL AND bbk_id != ''
                AND user_id != 'default'{bbk_filter_sql}
            """
            count_params = [
                source_id,
                start_date,
                end_date,
                *skill_filter_params,
                *bbk_filter_params,
            ]

        # 如果有纳入统计的技能，添加过滤条件
        if eligible_skills:
            placeholders = ", ".join(["%s"] * len(eligible_skills))
            base_where += f" AND skill_name IN ({placeholders})"
            count_params.extend(eligible_skills)

        # 查询总数（按 skill_id 或 skill_name 去重）
        count_query = f"""
            SELECT COUNT(DISTINCT CONCAT_WS(
                '|',
                COALESCE(NULLIF(skill_id, ''), '__NAME__'),
                skill_name
            )) as total
            FROM swe_tracing_spans
            WHERE {base_where}
        """
        count_row = await self._db.fetch_one(count_query, tuple(count_params))
        total = count_row["total"] if count_row else 0

        # 分页查询：先按 skill_id（若存在）/ skill_name 聚合，再 LEFT JOIN 展示映射
        offset = (page - 1) * page_size
        data_query = f"""
            SELECT MAX(NULLIF(skill_id, '')) as skill_id,
                   skill_name,
                   COUNT(DISTINCT trace_id) as count,
                   AVG(duration_ms) as avg_duration
            FROM swe_tracing_spans
            WHERE {base_where}
            GROUP BY skill_name,
                COALESCE(NULLIF(skill_id, ''), CONCAT('__NAME__:', skill_name))
            ORDER BY count DESC, skill_name ASC
            LIMIT %s OFFSET %s
        """
        params = count_params + [page_size, offset]
        rows = await self._db.fetch_all(data_query, tuple(params))

        skill_ids = [row["skill_id"] for row in rows if row.get("skill_id")]
        display_mapping = await self._get_skill_display_mapping(skill_ids)

        skills = [
            SkillUsage(
                skill_name=row["skill_name"],
                skill_id=row.get("skill_id") or None,
                cn_name=(
                    display_mapping.get(row.get("skill_id") or "", {}).get(
                        "cn_name",
                    )
                    or None
                ),
                skill_description=(
                    display_mapping.get(row.get("skill_id") or "", {}).get(
                        "description",
                    )
                    or None
                ),
                count=row["count"] or 0,
                avg_duration_ms=int(row["avg_duration"] or 0),
            )
            for row in rows
        ]
        logger.info(
            "[get_skills_paginated] 方法总耗时: %.3fms, total=%d",
            (time.time() - method_start) * 1000,
            total,
        )
        return skills, total

    async def get_mcp_summary(
        self,
        source_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> MCPSummary:
        """获取 MCP 全局调用汇总统计."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        # 构建基础查询条件
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            base_where = f"""
                start_time >= %s AND start_time <= %s
                AND event_type = 'tool_call_end'
                AND mcp_server IS NOT NULL
                AND source_id NOT IN ({exclude_placeholders})
                AND user_id != 'default'{bbk_filter_sql}
            """
            params = [
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            ]
        else:
            base_where = f"""
                source_id = %s AND start_time >= %s AND start_time <= %s
                AND event_type = 'tool_call_end'
                AND mcp_server IS NOT NULL
                AND user_id != 'default'{bbk_filter_sql}
            """
            params = [
                source_id,
                start_date,
                end_date,
                *bbk_filter_params,
            ]

        # 全局汇总统计
        summary_query = f"""
            SELECT COUNT(*) as total_calls,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count,
                   COUNT(DISTINCT mcp_server) as server_count
            FROM swe_tracing_spans
            WHERE {base_where}
        """
        row = await self._db.fetch_one(summary_query, tuple(params))
        return MCPSummary(
            total_calls=row["total_calls"] or 0,
            error_count=row["error_count"] or 0,
            server_count=row["server_count"] or 0,
        )

    async def get_task_status_summary(
        self,
        source_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> TaskStatusSummary:
        """获取定时任务执行汇总统计."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        bbk_filter_sql, bbk_filter_params = build_cron_bbk_in_filter(bbk_ids)
        exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))

        if source_id == "all":
            query = f"""
                SELECT e.status, e.async_status, e.is_read, COUNT(*) AS count
                FROM swe_cron_executions e
                INNER JOIN swe_cron_jobs j ON e.job_id = j.id
                WHERE e.is_final = 1 AND e.actual_time >= %s AND e.actual_time < %s
                  AND j.status != 'deleted'
                  AND j.deleted_at IS NULL
                  AND j.source_id NOT IN ({exclude_placeholders})
                  AND j.tenant_id != 'default'
                  {bbk_filter_sql}
                GROUP BY e.status, e.async_status, e.is_read
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
        else:
            query = f"""
                SELECT e.status, e.async_status, e.is_read, COUNT(*) AS count
                FROM swe_cron_executions e
                INNER JOIN swe_cron_jobs j ON e.job_id = j.id
                WHERE e.is_final = 1 AND e.actual_time >= %s AND e.actual_time < %s
                  AND j.status != 'deleted'
                  AND j.deleted_at IS NULL
                  AND j.tenant_id != 'default'
                  AND j.source_id = %s
                  {bbk_filter_sql}
                GROUP BY e.status, e.async_status, e.is_read
            """
            params = (start_date, end_date, source_id, *bbk_filter_params)

        rows = await self._db.fetch_all(query, params)

        success, running, failed, cancelled, read_count = (
            _summarize_task_status_rows(
                rows,
            )
        )
        total_tasks = success + running + failed + cancelled

        return TaskStatusSummary(
            total_tasks=total_tasks,
            success=success,
            running=running,
            failed=failed,
            cancelled=cancelled,
            read_count=read_count,
        )

    async def get_error_summary(
        self,
        source_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> ErrorSummary:
        """获取报错分析汇总统计.

        统计 swe_tracing_spans 中 error 不为空的记录，
        仅统计 llm_input（模型报错）和 tool_call_end（工具报错）。
        """
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        joined_bbk_filter_sql = bbk_filter_sql.replace(
            " AND bbk_id IN",
            " AND s.bbk_id IN",
        )
        exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))

        if source_id == "all":
            query = f"""
                SELECT event_type, COUNT(*) AS count
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time < %s
                  AND error IS NOT NULL
                  AND error != ''
                  AND source_id NOT IN ({exclude_placeholders})
                  AND event_type IN ('llm_input', 'tool_call_end')
                  {bbk_filter_sql}
                GROUP BY event_type
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
        else:
            query = f"""
                SELECT event_type, COUNT(*) AS count
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time < %s
                  AND error IS NOT NULL
                  AND error != ''
                  AND source_id = %s
                  AND event_type IN ('llm_input', 'tool_call_end')
                  {bbk_filter_sql}
                GROUP BY event_type
            """
            params = (start_date, end_date, source_id, *bbk_filter_params)

        rows = await self._db.fetch_all(query, params)

        model_errors = 0
        tool_errors = 0

        for row in rows:
            if row["event_type"] == "llm_input":
                model_errors = row["count"]
            elif row["event_type"] == "tool_call_end":
                tool_errors = row["count"]

        total_errors = model_errors + tool_errors
        model_error_codes = await self._get_model_error_code_counts(
            source_id,
            start_date,
            end_date,
            bbk_filter_sql,
            bbk_filter_params,
            exclude_placeholders,
        )

        return ErrorSummary(
            total_errors=total_errors,
            model_errors=model_errors,
            tool_errors=tool_errors,
            model_error_codes=model_error_codes,
        )

    async def _get_model_error_code_counts(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: list[str],
        exclude_placeholders: str,
    ) -> list[ModelErrorCodeCount]:
        """Aggregate top model error codes from llm_input error text."""
        if source_id == "all":
            query = f"""
                SELECT error
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time < %s
                  AND error IS NOT NULL
                  AND error != ''
                  AND error LIKE '%%Error code:%%'
                  AND source_id NOT IN ({exclude_placeholders})
                  AND event_type = 'llm_input'
                  {bbk_filter_sql}
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
        else:
            query = f"""
                SELECT error
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time < %s
                  AND error IS NOT NULL
                  AND error != ''
                  AND error LIKE '%%Error code:%%'
                  AND source_id = %s
                  AND event_type = 'llm_input'
                  {bbk_filter_sql}
            """
            params = (start_date, end_date, source_id, *bbk_filter_params)

        rows = await self._db.fetch_all(query, params)
        counts: Counter[str] = Counter()

        for row in rows:
            match = MODEL_ERROR_CODE_PATTERN.search(row.get("error") or "")
            if match:
                counts[match.group(1)] += 1

        return [
            ModelErrorCodeCount(code=code, count=count)
            for code, count in counts.most_common(10)
        ]

    def _build_error_list_params(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_filter_params: list,
        error_type: Optional[str],
        search_params: list[str],
        page_size: int,
        offset: int,
    ) -> tuple:
        """构建 get_error_list 的查询参数."""
        error_type_params = (
            [error_type]
            if error_type in ("llm_input", "tool_call_end")
            else []
        )
        if source_id == "all":
            return (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
                *error_type_params,
                *search_params,
                page_size,
                offset,
            )
        return (
            start_date,
            end_date,
            source_id,
            *bbk_filter_params,
            *error_type_params,
            *search_params,
            page_size,
            offset,
        )

    def _build_error_count_params(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_filter_params: list,
        error_type: Optional[str],
        search_params: list[str],
    ) -> tuple:
        """构建 get_error_list 的 count 查询参数."""
        error_type_params = (
            [error_type]
            if error_type in ("llm_input", "tool_call_end")
            else []
        )
        if source_id == "all":
            return (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
                *error_type_params,
                *search_params,
            )
        return (
            start_date,
            end_date,
            source_id,
            *bbk_filter_params,
            *error_type_params,
            *search_params,
        )

    def _row_to_error_item(self, row: dict) -> ErrorItem:
        """将数据库行转换为 ErrorItem."""
        return ErrorItem(
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            event_type=row["event_type"],
            error=row["error"],
            user_id=row["user_id"],
            user_name=row.get("user_name"),
            bbk_id=row.get("bbk_id"),
            session_id=row.get("session_id") or "",
            session_name=row.get("session_name"),
            model_name=row.get("model_name"),
            tool_name=row.get("tool_name"),
            mcp_server=row.get("mcp_server"),
            start_time=(
                row["start_time"].isoformat() if row["start_time"] else ""
            ),
            duration_ms=row.get("duration_ms"),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
        )

    async def get_error_list(
        self,
        source_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
        error_type: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 10,
    ) -> ErrorListResponse:
        """获取报错列表.

        查询 swe_tracing_spans 中 error 不为空的记录，
        仅返回 llm_input（模型报错）和 tool_call_end（工具报错）。
        支持按错误类型过滤和关键词搜索。
        """
        method_start = time.time()
        logger.info(
            "[get_error_list] 开始处理: source_id=%s, page=%s",
            source_id,
            page,
        )
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        joined_bbk_filter_sql = bbk_filter_sql.replace(
            " AND bbk_id IN",
            " AND s.bbk_id IN",
        )
        exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))

        # 构建错误类型过滤
        error_type_filter = ""
        if error_type and error_type in ("llm_input", "tool_call_end"):
            error_type_filter = "AND event_type = %s"

        # 构建搜索过滤
        search_filter = ""
        count_search_filter = ""
        search_params: list[str] = []
        if search:
            search_filter = "AND (s.user_id LIKE %s OR s.user_name LIKE %s OR s.error LIKE %s)"
            count_search_filter = (
                "AND (user_id LIKE %s OR user_name LIKE %s OR error LIKE %s)"
            )
            search_pattern = f"%{search}%"
            search_params = [search_pattern, search_pattern, search_pattern]

        # 构建 source_id 条件
        if source_id == "all":
            source_condition = f"s.source_id NOT IN ({exclude_placeholders})"
            count_source_condition = (
                f"source_id NOT IN ({exclude_placeholders})"
            )
        else:
            source_condition = "s.source_id = %s"
            count_source_condition = "source_id = %s"

        # 计算分页偏移
        offset = (page - 1) * page_size

        # 构建主查询（统一模板）
        query = f"""
            SELECT
                s.span_id, s.trace_id, s.event_type, s.error,
                s.user_id, s.user_name, s.bbk_id,
                t.session_id, t.session_name,
                s.model_name, s.tool_name, s.mcp_server,
                s.start_time, s.duration_ms, s.input_tokens, s.output_tokens
            FROM swe_tracing_spans s
            LEFT JOIN swe_tracing_traces t ON s.trace_id = t.trace_id
            WHERE s.start_time >= %s AND s.start_time < %s
              AND s.error IS NOT NULL AND s.error != ''
              AND {source_condition}
              AND s.event_type IN ('llm_input', 'tool_call_end')
              {joined_bbk_filter_sql}
              {error_type_filter}
              {search_filter}
            ORDER BY s.start_time DESC
            LIMIT %s OFFSET %s
        """

        params = self._build_error_list_params(
            source_id,
            start_date,
            end_date,
            bbk_filter_params,
            error_type,
            search_params,
            page_size,
            offset,
        )
        params = tuple(p for p in params if p is not None)

        rows = await self._db.fetch_all(query, params)

        # 构建 count 查询
        count_query = f"""
            SELECT COUNT(*) as total
            FROM swe_tracing_spans
            WHERE start_time >= %s AND start_time < %s
              AND error IS NOT NULL AND error != ''
              AND {count_source_condition}
              AND event_type IN ('llm_input', 'tool_call_end')
              {bbk_filter_sql}
              {error_type_filter}
              {count_search_filter}
        """

        count_params = self._build_error_count_params(
            source_id,
            start_date,
            end_date,
            bbk_filter_params,
            error_type,
            search_params,
        )
        count_params = tuple(p for p in count_params if p is not None)
        total_row = await self._db.fetch_one(count_query, count_params)
        total = total_row["total"] if total_row else 0

        items = [self._row_to_error_item(row) for row in rows]
        logger.info(
            "[get_error_list] 方法总耗时: %.3fms, total=%d",
            (time.time() - method_start) * 1000,
            total,
        )
        return ErrorListResponse(items=items, total=total)

    async def get_mcp_servers_paginated(
        self,
        source_id: str,
        page: int = 1,
        page_size: int = 10,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> tuple[list[MCPServerUsage], int]:
        """获取 MCP 服务调用排行榜（分页）."""
        method_start = time.time()
        logger.info(
            "[get_mcp_servers_paginated] 开始处理: source_id=%s, page=%s",
            source_id,
            page,
        )
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        # 构建基础查询条件
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            base_where = f"""
                start_time >= %s AND start_time <= %s
                AND event_type = 'tool_call_end'
                AND mcp_server IS NOT NULL
                AND source_id NOT IN ({exclude_placeholders})
                AND user_id != 'default'{bbk_filter_sql}
            """
            count_params = [
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            ]
        else:
            base_where = f"""
                source_id = %s AND start_time >= %s AND start_time <= %s
                AND event_type = 'tool_call_end'
                AND mcp_server IS NOT NULL
                AND user_id != 'default'{bbk_filter_sql}
            """
            count_params = [
                source_id,
                start_date,
                end_date,
                *bbk_filter_params,
            ]

        # 查询总数
        count_query = f"""
            SELECT COUNT(DISTINCT mcp_server) as total
            FROM swe_tracing_spans
            WHERE {base_where}
        """
        count_row = await self._db.fetch_one(count_query, tuple(count_params))
        total = count_row["total"] if count_row else 0

        # 分页查询
        offset = (page - 1) * page_size
        server_query = f"""
            SELECT mcp_server,
                   COUNT(DISTINCT tool_name) as tool_count,
                   COUNT(*) as total_calls,
                   AVG(duration_ms) as avg_duration,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
            FROM swe_tracing_spans
            WHERE {base_where}
            GROUP BY mcp_server
            ORDER BY total_calls DESC, mcp_server ASC
            LIMIT %s OFFSET %s
        """
        params = count_params + [page_size, offset]
        server_rows = await self._db.fetch_all(server_query, tuple(params))

        mcp_servers = []
        for server_row in server_rows:
            server_name = server_row["mcp_server"]
            mcp_servers.append(
                MCPServerUsage(
                    server_name=server_name,
                    tool_count=server_row["tool_count"] or 0,
                    total_calls=server_row["total_calls"] or 0,
                    avg_duration_ms=int(server_row["avg_duration"] or 0),
                    error_count=server_row["error_count"] or 0,
                    tools=[],  # 分页查询不返回工具详情
                ),
            )
        logger.info(
            "[get_mcp_servers_paginated] 方法总耗时: %.3fms, total=%d",
            (time.time() - method_start) * 1000,
            total,
        )
        return mcp_servers, total

    async def get_skill_traces(
        self,
        skill_name: str,
        source_id: str,
        page: int = 1,
        page_size: int = 20,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> tuple[list[TraceListItem], int]:
        """获取指定技能调用的对话列表（分页）."""
        method_start = time.time()
        logger.info(
            "[get_skill_traces] 开始处理: skill_name=%s, page=%s",
            skill_name,
            page,
        )
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now() + timedelta(days=1)

        # 构建查询条件
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            base_where = f"""
                s.start_time >= %s AND s.start_time <= %s
                AND s.skill_name = %s
                AND s.source_id NOT IN ({exclude_placeholders})
                AND s.user_id != 'default'
            """
            count_params = [
                start_date,
                end_date,
                skill_name,
                *EXCLUDED_SOURCE_IDS,
            ]
        else:
            base_where = """
                s.source_id = %s
                AND s.start_time >= %s AND s.start_time <= %s
                AND s.skill_name = %s
                AND s.user_id != 'default'
            """
            count_params = [
                source_id,
                start_date,
                end_date,
                skill_name,
            ]

        # 查询总数
        count_query = f"""
            SELECT COUNT(DISTINCT s.trace_id) as total
            FROM swe_tracing_spans s
            WHERE {base_where}
        """
        count_row = await self._db.fetch_one(count_query, tuple(count_params))
        total = count_row["total"] if count_row else 0

        # 分页查询对话列表
        offset = (page - 1) * page_size
        data_query = f"""
            SELECT DISTINCT t.trace_id, t.source_id, t.user_id, t.session_id,
                   t.channel, t.start_time, t.duration_ms, t.total_tokens,
                   t.total_input_tokens, t.total_output_tokens, t.model_name,
                   t.status, JSON_LENGTH(t.skills_used) as skills_count,
                   t.user_name, t.bbk_id
            FROM swe_tracing_spans s
            JOIN swe_tracing_traces t ON s.trace_id = t.trace_id
            WHERE {base_where}
            ORDER BY t.start_time DESC
            LIMIT %s OFFSET %s
        """
        params = list(count_params) + [page_size, offset]

        rows = await self._db.fetch_all(data_query, tuple(params))

        traces = [
            TraceListItem(
                trace_id=row["trace_id"],
                source_id=row["source_id"],
                user_id=row["user_id"],
                user_name=row["user_name"],
                bbk_id=row["bbk_id"],
                session_id=row["session_id"],
                channel=row["channel"],
                start_time=row["start_time"],
                duration_ms=row["duration_ms"],
                total_tokens=row["total_tokens"] or 0,
                total_input_tokens=row["total_input_tokens"] or 0,
                total_output_tokens=row["total_output_tokens"] or 0,
                model_name=row["model_name"],
                status=row["status"],
                skills_count=row["skills_count"] or 0,
            )
            for row in rows
        ]
        logger.info(
            "[get_skill_traces] 方法总耗时: %.3fms, total=%d",
            (time.time() - method_start) * 1000,
            total,
        )
        return traces, total

    async def _get_mcp_stats(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> tuple[list[MCPToolUsage], list[MCPServerUsage]]:
        """获取 MCP 统计."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            mcp_tool_query = f"""
                SELECT tool_name, mcp_server, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server IS NOT NULL
                  AND tool_name IS NOT NULL
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY tool_name, mcp_server
                ORDER BY count DESC
                LIMIT 10
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            mcp_tool_rows = await self._db.fetch_all(
                query=mcp_tool_query,
                params=params,
            )
        else:
            mcp_tool_query = f"""
                SELECT tool_name, mcp_server, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server IS NOT NULL
                  AND tool_name IS NOT NULL
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY tool_name, mcp_server
                ORDER BY count DESC
                LIMIT 10
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            mcp_tool_rows = await self._db.fetch_all(
                query=mcp_tool_query,
                params=params,
            )

        top_mcp_tools = [
            MCPToolUsage(
                tool_name=row["tool_name"],
                mcp_server=row["mcp_server"],
                count=row["count"] or 0,
                avg_duration_ms=int(row["avg_duration"] or 0),
                error_count=row["error_count"] or 0,
            )
            for row in mcp_tool_rows
        ]

        # 获取 MCP 服务器统计（简化版本）
        mcp_servers = await self._get_mcp_servers(
            source_id,
            start_date,
            end_date,
            bbk_ids,
        )
        return top_mcp_tools, mcp_servers

    async def _get_mcp_servers(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list[MCPServerUsage]:
        """获取 MCP 服务器统计."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            query = f"""
                SELECT mcp_server,
                       COUNT(DISTINCT tool_name) as tool_count,
                       COUNT(*) as total_calls,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server IS NOT NULL
                  AND source_id NOT IN ({exclude_placeholders})
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY mcp_server
                ORDER BY total_calls DESC
            """
            params = (
                start_date,
                end_date,
                *EXCLUDED_SOURCE_IDS,
                *bbk_filter_params,
            )
            server_rows = await self._db.fetch_all(query, params)
        else:
            query = f"""
                SELECT mcp_server,
                       COUNT(DISTINCT tool_name) as tool_count,
                       COUNT(*) as total_calls,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server IS NOT NULL
                  AND user_id != 'default'{bbk_filter_sql}
                GROUP BY mcp_server
                ORDER BY total_calls DESC
            """
            params = (source_id, start_date, end_date, *bbk_filter_params)
            server_rows = await self._db.fetch_all(query, params)

        mcp_servers = []
        for server_row in server_rows:
            server_name = server_row["mcp_server"]
            tools = await self._get_server_tools(
                source_id,
                start_date,
                end_date,
                server_name,
                bbk_ids,
            )
            mcp_servers.append(
                MCPServerUsage(
                    server_name=server_name,
                    tool_count=server_row["tool_count"] or 0,
                    total_calls=server_row["total_calls"] or 0,
                    avg_duration_ms=int(server_row["avg_duration"] or 0),
                    error_count=server_row["error_count"] or 0,
                    tools=tools,
                ),
            )
        return mcp_servers

    async def _get_server_tools(
        self,
        source_id: str,
        start_date: datetime,
        end_date: datetime,
        server_name: str,
        bbk_ids: Optional[str] = None,
    ) -> list[MCPToolUsage]:
        """获取服务器工具统计."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            query = f"""
                SELECT tool_name, mcp_server, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server = %s
                  AND tool_name IS NOT NULL{bbk_filter_sql}
                GROUP BY tool_name, mcp_server
                ORDER BY count DESC
            """
            params = (start_date, end_date, server_name, *bbk_filter_params)
            rows = await self._db.fetch_all(query, params)
        else:
            query = f"""
                SELECT tool_name, mcp_server, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE source_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server = %s
                  AND tool_name IS NOT NULL{bbk_filter_sql}
                GROUP BY tool_name, mcp_server
                ORDER BY count DESC
            """
            params = (
                source_id,
                start_date,
                end_date,
                server_name,
                *bbk_filter_params,
            )
            rows = await self._db.fetch_all(query, params)
        return [
            MCPToolUsage(
                tool_name=r["tool_name"],
                mcp_server=r["mcp_server"],
                count=r["count"] or 0,
                avg_duration_ms=int(r["avg_duration"] or 0),
                error_count=r["error_count"] or 0,
            )
            for r in rows
        ]

    async def _get_user_model_usage(
        self,
        source_id: str,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list[ModelUsage]:
        """获取用户模型使用."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            model_query = f"""
                SELECT model_name, COUNT(*) as count,
                       SUM(total_input_tokens) as input_tokens,
                       SUM(total_output_tokens) as output_tokens,
                       SUM(total_tokens) as total_tokens
                FROM swe_tracing_traces
                WHERE user_id = %s AND start_time >= %s AND start_time <= %s
                      AND model_name IS NOT NULL{bbk_filter_sql}
                GROUP BY model_name
                ORDER BY count DESC
            """
            model_rows = await self._db.fetch_all(
                model_query,
                (user_id, start_date, end_date, *bbk_filter_params),
            )
        else:
            model_query = f"""
                SELECT model_name, COUNT(*) as count,
                       SUM(total_input_tokens) as input_tokens,
                       SUM(total_output_tokens) as output_tokens,
                       SUM(total_tokens) as total_tokens
                FROM swe_tracing_traces
                WHERE source_id = %s AND user_id = %s AND start_time >= %s AND start_time <= %s
                      AND model_name IS NOT NULL{bbk_filter_sql}
                GROUP BY model_name
                ORDER BY count DESC
            """
            model_rows = await self._db.fetch_all(
                model_query,
                (source_id, user_id, start_date, end_date, *bbk_filter_params),
            )
        return [
            ModelUsage(
                model_name=row["model_name"],
                count=row["count"],
                total_tokens=row["total_tokens"] or 0,
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
            )
            for row in model_rows
        ]

    async def _get_user_tool_usage(
        self,
        source_id: str,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list[ToolUsage]:
        """获取用户工具使用."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            tool_query = f"""
                SELECT tool_name, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE user_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND tool_name IS NOT NULL{bbk_filter_sql}
                GROUP BY tool_name
                ORDER BY count DESC
            """
            tool_rows = await self._db.fetch_all(
                tool_query,
                (user_id, start_date, end_date, *bbk_filter_params),
            )
        else:
            tool_query = f"""
                SELECT tool_name, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE source_id = %s AND user_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND tool_name IS NOT NULL{bbk_filter_sql}
                GROUP BY tool_name
                ORDER BY count DESC
            """
            tool_rows = await self._db.fetch_all(
                tool_query,
                (source_id, user_id, start_date, end_date, *bbk_filter_params),
            )
        return [
            ToolUsage(
                tool_name=row["tool_name"],
                count=row["count"],
                avg_duration_ms=int(row["avg_duration"] or 0),
                error_count=row["error_count"] or 0,
            )
            for row in tool_rows
        ]

    async def _get_user_skill_usage(
        self,
        source_id: str,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list[SkillUsage]:
        """获取用户技能使用."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            skill_query = f"""
                SELECT MAX(NULLIF(skill_id, '')) as skill_id,
                       skill_name,
                       COUNT(DISTINCT trace_id) as count,
                       AVG(duration_ms) as avg_duration
                FROM swe_tracing_spans
                WHERE user_id = %s AND start_time >= %s AND start_time <= %s
                  AND skill_name IS NOT NULL{bbk_filter_sql}
                GROUP BY skill_name,
                    COALESCE(NULLIF(skill_id, ''), CONCAT('__NAME__:', skill_name))
                ORDER BY count DESC
            """
            skill_rows = await self._db.fetch_all(
                skill_query,
                (user_id, start_date, end_date, *bbk_filter_params),
            )
        else:
            skill_query = f"""
                SELECT MAX(NULLIF(skill_id, '')) as skill_id,
                       skill_name,
                       COUNT(DISTINCT trace_id) as count,
                       AVG(duration_ms) as avg_duration
                FROM swe_tracing_spans
                WHERE source_id = %s AND user_id = %s AND start_time >= %s AND start_time <= %s
                  AND skill_name IS NOT NULL{bbk_filter_sql}
                GROUP BY skill_name,
                    COALESCE(NULLIF(skill_id, ''), CONCAT('__NAME__:', skill_name))
                ORDER BY count DESC
            """
            skill_rows = await self._db.fetch_all(
                skill_query,
                (source_id, user_id, start_date, end_date, *bbk_filter_params),
            )

        skill_ids = [
            row["skill_id"] for row in skill_rows if row.get("skill_id")
        ]
        display_mapping = await self._get_skill_display_mapping(skill_ids)
        return [
            SkillUsage(
                skill_name=row["skill_name"],
                skill_id=row.get("skill_id") or None,
                cn_name=(
                    display_mapping.get(row.get("skill_id") or "", {}).get(
                        "cn_name",
                    )
                    or None
                ),
                count=row["count"],
                avg_duration_ms=int(row["avg_duration"] or 0),
            )
            for row in skill_rows
        ]

    async def _get_user_mcp_tool_usage(
        self,
        source_id: str,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list[MCPToolUsage]:
        """获取用户 MCP 工具使用."""
        bbk_filter_sql, bbk_filter_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            query = f"""
                SELECT tool_name, mcp_server, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE user_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server IS NOT NULL
                  AND tool_name IS NOT NULL{bbk_filter_sql}
                GROUP BY tool_name, mcp_server
                ORDER BY count DESC
            """
            rows = await self._db.fetch_all(
                query,
                (user_id, start_date, end_date, *bbk_filter_params),
            )
        else:
            query = f"""
                SELECT tool_name, mcp_server, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE source_id = %s AND user_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server IS NOT NULL
                  AND tool_name IS NOT NULL{bbk_filter_sql}
                GROUP BY tool_name, mcp_server
                ORDER BY count DESC
            """
            rows = await self._db.fetch_all(
                query,
                (source_id, user_id, start_date, end_date, *bbk_filter_params),
            )
        return [
            MCPToolUsage(
                tool_name=row["tool_name"],
                mcp_server=row["mcp_server"],
                count=row["count"],
                avg_duration_ms=int(row["avg_duration"] or 0),
                error_count=row["error_count"] or 0,
            )
            for row in rows
        ]

    def _build_sessions_where_clauses(
        self,
        source_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        bbk_ids: Optional[str],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        has_error: Optional[bool],
        resource_type: Optional[str] = None,
        resource_name: Optional[str] = None,
        mcp_server: Optional[str] = None,
    ) -> tuple[list[str], list[Any]]:
        """构建 get_sessions 的 WHERE 条件."""
        # 初始化基础条件
        where_clauses, params = self._init_source_filter(source_id)

        # 添加基础过滤条件
        self._add_basic_filters(
            where_clauses,
            params,
            user_id,
            session_id,
            bbk_ids,
            start_date,
            end_date,
        )

        # 添加资源类型过滤
        resource_date_sql, resource_date_params = (
            self._build_resource_date_sql(
                start_date,
                end_date,
            )
        )
        self._add_resource_filter(
            where_clauses,
            params,
            resource_type,
            resource_name,
            mcp_server,
            resource_date_sql,
            resource_date_params,
        )

        # 添加错误状态过滤
        self._add_error_filter(where_clauses, has_error)

        return where_clauses, params

    def _init_source_filter(
        self,
        source_id: str,
    ) -> tuple[list[str], list[Any]]:
        """初始化 source_id 过滤条件."""
        exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
        if source_id == "all":
            where_clauses = [f"t.source_id NOT IN ({exclude_placeholders})"]
            params = list(EXCLUDED_SOURCE_IDS)
        else:
            where_clauses = ["t.source_id = %s"]
            params = [source_id]
        return where_clauses, params

    def _add_basic_filters(
        self,
        where_clauses: list[str],
        params: list[Any],
        user_id: Optional[str],
        session_id: Optional[str],
        bbk_ids: Optional[str],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> None:
        """添加基础参数过滤条件."""
        self._append_filter(where_clauses, params, "t.user_id = %s", user_id)
        if session_id:
            where_clauses.append("t.session_id LIKE %s")
            params.append(f"%{session_id}%")
        if bbk_ids:
            bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
            where_clauses.append(
                f"t.bbk_id IN ({', '.join(['%s'] * len(bbk_params))})",
            )
            params.extend(bbk_params)
        self._append_filter(
            where_clauses,
            params,
            "t.start_time >= %s",
            start_date,
        )
        self._append_filter(
            where_clauses,
            params,
            "t.start_time <= %s",
            end_date,
        )

    def _append_filter(
        self,
        where_clauses: list[str],
        params: list[Any],
        clause: str,
        value: Optional[Any],
    ) -> None:
        """添加单个过滤条件."""
        if value:
            where_clauses.append(clause)
            params.append(value)

    def _build_resource_date_sql(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> tuple[str, list[Any]]:
        """构建资源子查询的日期条件."""
        resource_date_clauses: list[str] = []
        resource_date_params: list[Any] = []
        if start_date:
            resource_date_clauses.append("resource.start_time >= %s")
            resource_date_params.append(start_date)
        if end_date:
            resource_date_clauses.append("resource.start_time <= %s")
            resource_date_params.append(end_date)
        resource_date_sql = (
            " AND " + " AND ".join(resource_date_clauses)
            if resource_date_clauses
            else ""
        )
        return resource_date_sql, resource_date_params

    def _add_resource_filter(
        self,
        where_clauses: list[str],
        params: list[Any],
        resource_type: Optional[str],
        resource_name: Optional[str],
        mcp_server: Optional[str],
        resource_date_sql: str,
        resource_date_params: list[Any],
    ) -> None:
        """添加资源类型过滤条件."""
        if not resource_type or not resource_name:
            return
        handlers = {
            "model": self._add_model_filter,
            "skill": self._add_skill_filter,
            "mcp_tool": self._add_mcp_tool_filter,
        }
        handler = handlers.get(resource_type)
        if handler:
            handler(
                where_clauses,
                params,
                resource_name,
                mcp_server,
                resource_date_sql,
                resource_date_params,
            )

    def _add_model_filter(
        self,
        where_clauses: list[str],
        params: list[Any],
        resource_name: str,
        mcp_server: Optional[str],
        resource_date_sql: str,
        resource_date_params: list[Any],
    ) -> None:
        """添加模型资源过滤."""
        where_clauses.append(
            "EXISTS (SELECT 1 FROM swe_tracing_traces resource "
            "WHERE resource.source_id = t.source_id "
            "AND resource.session_id = t.session_id "
            "AND resource.model_name = %s"
            f"{resource_date_sql})",
        )
        params.append(resource_name)
        params.extend(resource_date_params)

    def _add_skill_filter(
        self,
        where_clauses: list[str],
        params: list[Any],
        resource_name: str,
        mcp_server: Optional[str],
        resource_date_sql: str,
        resource_date_params: list[Any],
    ) -> None:
        """添加技能资源过滤."""
        where_clauses.append(
            "EXISTS (SELECT 1 FROM swe_tracing_spans resource "
            "WHERE resource.source_id = t.source_id "
            "AND resource.session_id = t.session_id "
            "AND resource.event_type = 'skill_invocation' "
            "AND resource.skill_name = %s"
            f"{resource_date_sql})",
        )
        params.append(resource_name)
        params.extend(resource_date_params)

    def _add_mcp_tool_filter(
        self,
        where_clauses: list[str],
        params: list[Any],
        resource_name: str,
        mcp_server: Optional[str],
        resource_date_sql: str,
        resource_date_params: list[Any],
    ) -> None:
        """添加 MCP 工具资源过滤."""
        if not mcp_server:
            return
        where_clauses.append(
            "EXISTS (SELECT 1 FROM swe_tracing_spans resource "
            "WHERE resource.source_id = t.source_id "
            "AND resource.session_id = t.session_id "
            "AND resource.event_type = 'tool_call_end' "
            "AND resource.tool_name = %s "
            "AND resource.mcp_server = %s"
            f"{resource_date_sql})",
        )
        params.extend([resource_name, mcp_server])
        params.extend(resource_date_params)

    def _add_error_filter(
        self,
        where_clauses: list[str],
        has_error: Optional[bool],
    ) -> None:
        """添加错误状态过滤条件."""
        if has_error is True:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM swe_tracing_traces error_trace "
                "WHERE error_trace.source_id = t.source_id "
                "AND error_trace.session_id = t.session_id "
                "AND error_trace.status = 'error')",
            )
        elif has_error is False:
            where_clauses.append(
                "NOT EXISTS (SELECT 1 FROM swe_tracing_traces error_trace "
                "WHERE error_trace.source_id = t.source_id "
                "AND error_trace.session_id = t.session_id "
                "AND error_trace.status = 'error')",
            )

    def _build_skill_date_conditions(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> tuple[str, list[Any]]:
        """构建技能统计子查询的日期筛选条件."""
        skill_date_conditions = "s.skill_name IS NOT NULL"
        skill_params: list[Any] = []
        if start_date:
            skill_date_conditions += " AND s.start_time >= %s"
            skill_params.append(start_date)
        if end_date:
            skill_date_conditions += " AND s.start_time <= %s"
            skill_params.append(end_date)
        return skill_date_conditions, skill_params

    def _build_sessions_query_params(
        self,
        source_id: str,
        skill_params: list[Any],
        where_params: list[Any],
        page_size: int,
        offset: int,
    ) -> list[Any]:
        """构建 get_sessions 主查询参数."""
        if source_id == "all":
            # 子查询1-5 各需要一份 EXCLUDED_SOURCE_IDS
            return (
                list(EXCLUDED_SOURCE_IDS)  # 子查询1: spans
                + skill_params
                + list(EXCLUDED_SOURCE_IDS)  # 子查询2: user_name
                + list(EXCLUDED_SOURCE_IDS)  # 子查询3: bbk_id
                + list(EXCLUDED_SOURCE_IDS)  # 子查询4: session_name
                + list(EXCLUDED_SOURCE_IDS)  # 子查询5: user_message
                + where_params
                + [page_size, offset]
            )
        # 单个 source_id 需要在 5 个子查询中各出现一次
        return (
            [source_id]
            + skill_params
            + [source_id, source_id, source_id, source_id]
            + where_params
            + [page_size, offset]
        )

    def _row_to_session_item(self, row: dict) -> SessionListItem:
        """将数据库行转换为 SessionListItem."""
        return SessionListItem(
            session_id=row["session_id"],
            session_name=row.get("session_name"),
            user_id=row["user_id"],
            user_name=row["user_name"],
            bbk_id=row["bbk_id"],
            channel=row["channel"],
            total_traces=row["total_traces"] or 0,
            total_tokens=row["total_tokens"] or 0,
            total_skills=row["total_skills"] or 0,
            first_active=row["first_active"],
            last_active=row["last_active"],
        )

    # ===== 会话分析 =====

    async def get_sessions(
        self,
        source_id: str,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
        has_error: Optional[bool] = None,
        resource_type: Optional[str] = None,
        resource_name: Optional[str] = None,
        mcp_server: Optional[str] = None,
    ) -> tuple[list[SessionListItem], int]:
        """获取会话列表.

        使用拆分查询优化性能：
        1. 主查询获取 session 列表
        2. 批量查询技能统计
        3. 批量查询用户名、bbk_id、session_name
        4. 应用层组装结果
        """
        method_start = time.time()
        logger.info(
            "[get_sessions] 开始处理: source_id=%s, page=%s",
            source_id,
            page,
        )
        # 构建 WHERE 条件
        where_clauses, params = self._build_sessions_where_clauses(
            source_id,
            user_id,
            session_id,
            bbk_ids,
            start_date,
            end_date,
            has_error,
            resource_type,
            resource_name,
            mcp_server,
        )
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # 获取总数
        count_query = f"SELECT COUNT(DISTINCT t.session_id) as total FROM swe_tracing_traces t WHERE {where_sql}"
        count_row = await self._db.fetch_one(count_query, tuple(params))
        total = count_row["total"] if count_row else 0

        if total == 0:
            return [], 0

        # 计算分页偏移
        offset = (page - 1) * page_size

        # 1. 主查询：获取 session 列表（不含子查询）
        main_query = f"""
            SELECT t.session_id, t.user_id, t.channel,
                   COUNT(*) as total_traces,
                   SUM(t.total_tokens) as total_tokens,
                   MIN(t.start_time) as first_active,
                   MAX(t.start_time) as last_active
            FROM swe_tracing_traces t
            WHERE {where_sql}
            GROUP BY t.session_id, t.user_id, t.channel
            ORDER BY last_active DESC, session_id ASC
            LIMIT %s OFFSET %s
        """
        main_params = params + [page_size, offset]
        main_rows = await self._db.fetch_all(main_query, tuple(main_params))

        if not main_rows:
            return [], total

        # 提取 session_ids 和 user_ids
        session_ids = [row["session_id"] for row in main_rows]
        user_ids = list(set(row["user_id"] for row in main_rows))

        # 2. 批量查询：技能统计
        skill_stats = await self._batch_query_session_skills(
            session_ids,
            source_id,
            start_date,
            end_date,
        )

        # 3. 批量查询：用户名和 bbk_id
        user_info = await self._batch_query_user_info(
            user_ids,
            source_id,
            start_date,
            end_date,
        )

        # 4. 批量查询：session_name
        session_names = await self._batch_query_session_names(
            session_ids,
            source_id,
        )

        # 5. 组装结果
        sessions = []
        for row in main_rows:
            sid = row["session_id"]
            uid = row["user_id"]
            info = user_info.get(uid, {})
            sessions.append(
                SessionListItem(
                    session_id=sid,
                    session_name=session_names.get(sid),
                    user_id=uid,
                    user_name=info.get("user_name"),
                    bbk_id=info.get("bbk_id"),
                    channel=row["channel"],
                    total_traces=row["total_traces"] or 0,
                    total_tokens=row["total_tokens"] or 0,
                    total_skills=skill_stats.get(sid, 0),
                    first_active=row["first_active"],
                    last_active=row["last_active"],
                ),
            )

        logger.info(
            "[get_sessions] 方法总耗时: %.3fms, total=%d",
            (time.time() - method_start) * 1000,
            total,
        )
        return sessions, total

    async def _batch_query_session_skills(
        self,
        session_ids: list[str],
        source_id: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> dict[str, int]:
        """批量查询 session 的技能统计."""
        if not session_ids:
            return {}

        placeholders = ", ".join(["%s"] * len(session_ids))
        source_condition, source_params = self._build_source_condition(
            source_id,
        )

        # 构建时间条件
        time_conditions = []
        time_params = []
        if start_date:
            time_conditions.append("start_time >= %s")
            time_params.append(start_date)
        if end_date:
            time_conditions.append("start_time <= %s")
            time_params.append(end_date)
        time_sql = " AND ".join(time_conditions) if time_conditions else "1=1"

        query = f"""
            SELECT session_id, COUNT(*) as skill_count
            FROM swe_tracing_spans
            WHERE session_id IN ({placeholders})
              AND {source_condition}
              AND {time_sql}
              AND skill_name IS NOT NULL
            GROUP BY session_id
        """
        params = session_ids + source_params + time_params
        rows = await self._db.fetch_all(query, tuple(params))
        return {row["session_id"]: row["skill_count"] for row in rows}

    async def _batch_query_user_info(
        self,
        user_ids: list[str],
        source_id: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> dict[str, dict[str, str | None]]:
        """批量查询用户信息（user_name, bbk_id）."""
        if not user_ids:
            return {}

        placeholders = ", ".join(["%s"] * len(user_ids))
        source_condition, source_params = self._build_source_condition(
            source_id,
        )

        # 用户名查询
        name_query = f"""
            SELECT user_id, user_name
            FROM (
                SELECT user_id, user_name,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY start_time DESC) as rn
                FROM swe_tracing_traces
                WHERE user_id IN ({placeholders})
                  AND {source_condition}
                  AND user_name IS NOT NULL
            ) t
            WHERE rn = 1
        """
        name_rows = await self._db.fetch_all(
            name_query,
            tuple(user_ids + source_params),
        )
        name_map = {row["user_id"]: row["user_name"] for row in name_rows}

        # bbk_id 查询
        bbk_query = f"""
            SELECT user_id, bbk_id
            FROM (
                SELECT user_id, bbk_id,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY start_time DESC) as rn
                FROM swe_tracing_traces
                WHERE user_id IN ({placeholders})
                  AND {source_condition}
                  AND bbk_id IS NOT NULL
            ) t
            WHERE rn = 1
        """
        bbk_rows = await self._db.fetch_all(
            bbk_query,
            tuple(user_ids + source_params),
        )
        bbk_map = {row["user_id"]: row["bbk_id"] for row in bbk_rows}

        # 合并结果
        result = {}
        for uid in user_ids:
            result[uid] = {
                "user_name": name_map.get(uid),
                "bbk_id": bbk_map.get(uid),
            }
        return result

    async def _batch_query_session_names(
        self,
        session_ids: list[str],
        source_id: str,
    ) -> dict[str, str]:
        """批量查询 session 名称."""
        if not session_ids:
            return {}

        placeholders = ", ".join(["%s"] * len(session_ids))
        source_condition, source_params = self._build_source_condition(
            source_id,
        )

        # session_name 查询
        name_query = f"""
            SELECT session_id, session_name
            FROM (
                SELECT session_id, session_name,
                       ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY start_time ASC) as rn
                FROM swe_tracing_traces
                WHERE session_id IN ({placeholders})
                  AND {source_condition}
                  AND session_name IS NOT NULL
            ) t
            WHERE rn = 1
        """
        name_rows = await self._db.fetch_all(
            name_query,
            tuple(session_ids + source_params),
        )

        # user_message 查询（作为 fallback）
        msg_query = f"""
            SELECT session_id, user_message
            FROM (
                SELECT session_id, user_message,
                       ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY start_time ASC) as rn
                FROM swe_tracing_traces
                WHERE session_id IN ({placeholders})
                  AND {source_condition}
                  AND user_message IS NOT NULL
            ) t
            WHERE rn = 1
        """
        msg_rows = await self._db.fetch_all(
            msg_query,
            tuple(session_ids + source_params),
        )

        # 合并结果：优先使用 session_name，否则截取 user_message 前 10 字符
        name_map = {
            row["session_id"]: row["session_name"] for row in name_rows
        }
        msg_map = {row["session_id"]: row["user_message"] for row in msg_rows}

        result = {}
        for sid in session_ids:
            if sid in name_map:
                result[sid] = name_map[sid]
            elif sid in msg_map and msg_map[sid]:
                result[sid] = msg_map[sid][:10]
        return result

    def _build_source_condition(
        self,
        source_id: str,
    ) -> tuple[str, list[str]]:
        """构建 source_id 过滤条件."""
        if source_id == "all":
            exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
            return f"source_id NOT IN ({exclude_placeholders})", list(
                EXCLUDED_SOURCE_IDS,
            )
        return "source_id = %s", [source_id]

    async def get_session_stats(
        self,
        source_id: str,
        session_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> SessionStats:
        """获取会话统计详情."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=30)
        if end_date is None:
            end_date = datetime.now()

        stats_row = await self._fetch_session_stats_row(
            source_id,
            session_id,
            start_date,
            end_date,
            bbk_ids,
        )

        if not stats_row or not stats_row.get("user_id"):
            return SessionStats(session_id=session_id, user_id="", channel="")

        user_id = stats_row["user_id"]
        channel = stats_row["channel"] or ""

        model_usage, tools_used, skills_used, mcp_tools_used = (
            await asyncio.gather(
                self._fetch_session_model_usage(
                    source_id,
                    session_id,
                    start_date,
                    end_date,
                    bbk_ids,
                ),
                self._fetch_session_tools_used(
                    source_id,
                    session_id,
                    start_date,
                    end_date,
                    bbk_ids,
                ),
                self._fetch_session_skills_used(
                    source_id,
                    session_id,
                    start_date,
                    end_date,
                    bbk_ids,
                ),
                self._fetch_session_mcp_tools(
                    source_id,
                    session_id,
                    start_date,
                    end_date,
                    bbk_ids,
                ),
            )
        )

        return self._build_session_stats(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            stats_row=stats_row,
            model_usage_rows=model_usage,
            tools_used_rows=tools_used,
            skills_used_rows=skills_used,
            mcp_tools_rows=mcp_tools_used,
        )

    async def _fetch_session_stats_row(
        self,
        source_id: str,
        session_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> Optional[dict]:
        """获取会话统计行数据."""
        bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(
                ["%s"] * len(EXCLUDED_SOURCE_IDS),
            )
            query = f"""
                SELECT
                    user_id,
                    channel,
                    COUNT(*) as total_traces,
                    SUM(total_input_tokens) as input_tokens,
                    SUM(total_output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens,
                    AVG(duration_ms) as avg_duration,
                    MIN(start_time) as first_active,
                    MAX(start_time) as last_active
                FROM swe_tracing_traces
                WHERE source_id NOT IN ({exclude_placeholders})
                      AND session_id = %s AND start_time >= %s AND start_time <= %s
                      {bbk_filter_sql}
                GROUP BY user_id, channel
            """
            return await self._db.fetch_one(
                query,
                (
                    *EXCLUDED_SOURCE_IDS,
                    session_id,
                    start_date,
                    end_date,
                    *bbk_params,
                ),
            )
        return await self._db.fetch_one(
            f"""
            SELECT
                user_id,
                channel,
                COUNT(*) as total_traces,
                SUM(total_input_tokens) as input_tokens,
                SUM(total_output_tokens) as output_tokens,
                SUM(total_tokens) as total_tokens,
                AVG(duration_ms) as avg_duration,
                MIN(start_time) as first_active,
                MAX(start_time) as last_active
            FROM swe_tracing_traces
            WHERE source_id = %s AND session_id = %s AND start_time >= %s AND start_time <= %s
                  {bbk_filter_sql}
            GROUP BY user_id, channel
            """,
            (source_id, session_id, start_date, end_date, *bbk_params),
        )

    async def _fetch_session_model_usage(
        self,
        source_id: str,
        session_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list:
        """获取会话模型使用数据."""
        bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(
                ["%s"] * len(EXCLUDED_SOURCE_IDS),
            )
            query = f"""
                SELECT model_name, COUNT(*) as count,
                       SUM(total_input_tokens) as input_tokens,
                       SUM(total_output_tokens) as output_tokens,
                       SUM(total_tokens) as total_tokens
                FROM swe_tracing_traces
                WHERE source_id NOT IN ({exclude_placeholders})
                      AND session_id = %s AND start_time >= %s AND start_time <= %s
                      AND model_name IS NOT NULL
                      {bbk_filter_sql}
                GROUP BY model_name
                ORDER BY count DESC
            """
            return await self._db.fetch_all(
                query,
                (
                    *EXCLUDED_SOURCE_IDS,
                    session_id,
                    start_date,
                    end_date,
                    *bbk_params,
                ),
            )
        return await self._db.fetch_all(
            f"""
            SELECT model_name, COUNT(*) as count,
                   SUM(total_input_tokens) as input_tokens,
                   SUM(total_output_tokens) as output_tokens,
                   SUM(total_tokens) as total_tokens
            FROM swe_tracing_traces
            WHERE source_id = %s AND session_id = %s AND start_time >= %s AND start_time <= %s
                  AND model_name IS NOT NULL
                  {bbk_filter_sql}
            GROUP BY model_name
            ORDER BY count DESC
            """,
            (source_id, session_id, start_date, end_date, *bbk_params),
        )

    async def _fetch_session_tools_used(
        self,
        source_id: str,
        session_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list:
        """获取会话工具使用数据."""
        bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(
                ["%s"] * len(EXCLUDED_SOURCE_IDS),
            )
            query = f"""
                SELECT tool_name, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE source_id NOT IN ({exclude_placeholders})
                      AND session_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND tool_name IS NOT NULL
                  AND mcp_server IS NULL
                  {bbk_filter_sql}
                GROUP BY tool_name
                ORDER BY count DESC
            """
            return await self._db.fetch_all(
                query,
                (
                    *EXCLUDED_SOURCE_IDS,
                    session_id,
                    start_date,
                    end_date,
                    *bbk_params,
                ),
            )
        return await self._db.fetch_all(
            f"""
            SELECT tool_name, COUNT(*) as count,
                   AVG(duration_ms) as avg_duration,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
            FROM swe_tracing_spans
            WHERE source_id = %s AND session_id = %s AND start_time >= %s AND start_time <= %s
              AND event_type = 'tool_call_end'
              AND tool_name IS NOT NULL
              AND mcp_server IS NULL
              {bbk_filter_sql}
            GROUP BY tool_name
            ORDER BY count DESC
            """,
            (source_id, session_id, start_date, end_date, *bbk_params),
        )

    async def _fetch_session_skills_used(
        self,
        source_id: str,
        session_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list:
        """获取会话技能使用数据.

        返回的字典中除数据库字段外，额外补充 cn_name
        （来自 swe_skills 映射，由前端决定如何回退展示）。
        """
        bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(
                ["%s"] * len(EXCLUDED_SOURCE_IDS),
            )
            query = f"""
                SELECT MAX(NULLIF(skill_id, '')) as skill_id,
                       skill_name,
                       COUNT(*) as count,
                       AVG(duration_ms) as avg_duration
                FROM swe_tracing_spans
                WHERE source_id NOT IN ({exclude_placeholders})
                      AND session_id = %s AND start_time >= %s AND start_time <= %s
                  AND skill_name IS NOT NULL
                  {bbk_filter_sql}
                GROUP BY skill_name,
                    COALESCE(NULLIF(skill_id, ''), CONCAT('__NAME__:', skill_name))
                ORDER BY count DESC
            """
            rows = await self._db.fetch_all(
                query,
                (
                    *EXCLUDED_SOURCE_IDS,
                    session_id,
                    start_date,
                    end_date,
                    *bbk_params,
                ),
            )
        else:
            rows = await self._db.fetch_all(
                f"""
                SELECT MAX(NULLIF(skill_id, '')) as skill_id,
                       skill_name,
                       COUNT(*) as count,
                       AVG(duration_ms) as avg_duration
                FROM swe_tracing_spans
                WHERE source_id = %s AND session_id = %s AND start_time >= %s AND start_time <= %s
                  AND skill_name IS NOT NULL
                  {bbk_filter_sql}
                GROUP BY skill_name,
                    COALESCE(NULLIF(skill_id, ''), CONCAT('__NAME__:', skill_name))
                ORDER BY count DESC
                """,
                (source_id, session_id, start_date, end_date, *bbk_params),
            )

        skill_ids = [
            row.get("skill_id") for row in rows if row.get("skill_id")
        ]
        display_mapping = await self._get_skill_display_mapping(skill_ids)
        for row in rows:
            row["cn_name"] = (
                display_mapping.get(
                    row.get("skill_id") or "",
                    {},
                ).get("cn_name")
                or None
            )
        return rows

    async def _fetch_session_mcp_tools(
        self,
        source_id: str,
        session_id: str,
        start_date: datetime,
        end_date: datetime,
        bbk_ids: Optional[str] = None,
    ) -> list:
        """获取会话 MCP 工具使用数据."""
        bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
        if source_id == "all":
            exclude_placeholders = ", ".join(
                ["%s"] * len(EXCLUDED_SOURCE_IDS),
            )
            query = f"""
                SELECT tool_name, mcp_server, COUNT(*) as count,
                       AVG(duration_ms) as avg_duration,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM swe_tracing_spans
                WHERE source_id NOT IN ({exclude_placeholders})
                      AND session_id = %s AND start_time >= %s AND start_time <= %s
                  AND event_type = 'tool_call_end'
                  AND mcp_server IS NOT NULL
                  {bbk_filter_sql}
                GROUP BY tool_name, mcp_server
                ORDER BY count DESC
            """
            return await self._db.fetch_all(
                query,
                (
                    *EXCLUDED_SOURCE_IDS,
                    session_id,
                    start_date,
                    end_date,
                    *bbk_params,
                ),
            )
        return await self._db.fetch_all(
            f"""
            SELECT tool_name, mcp_server, COUNT(*) as count,
                   AVG(duration_ms) as avg_duration,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
            FROM swe_tracing_spans
            WHERE source_id = %s AND session_id = %s AND start_time >= %s AND start_time <= %s
              AND event_type = 'tool_call_end'
              AND mcp_server IS NOT NULL
              {bbk_filter_sql}
            GROUP BY tool_name, mcp_server
            ORDER BY count DESC
            """,
            (source_id, session_id, start_date, end_date, *bbk_params),
        )

    def _build_session_stats(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        stats_row: dict,
        model_usage_rows: list,
        tools_used_rows: list,
        skills_used_rows: list,
        mcp_tools_rows: list,
    ) -> SessionStats:
        """构建会话统计对象."""
        return SessionStats(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            model_usage=self._build_model_usage_list(model_usage_rows),
            total_tokens=stats_row["total_tokens"] or 0,
            input_tokens=stats_row["input_tokens"] or 0,
            output_tokens=stats_row["output_tokens"] or 0,
            total_traces=stats_row["total_traces"] or 0,
            avg_duration_ms=self._extract_avg_duration(stats_row),
            tools_used=self._build_tool_usage_list(tools_used_rows),
            skills_used=self._build_skill_usage_list(skills_used_rows),
            mcp_tools_used=self._build_mcp_tool_usage_list(mcp_tools_rows),
            first_active=stats_row["first_active"],
            last_active=stats_row["last_active"],
        )

    def _build_model_usage_list(self, rows: list) -> list[ModelUsage]:
        """构建模型使用列表."""
        return [
            ModelUsage(
                model_name=row["model_name"],
                count=row["count"],
                total_tokens=row["total_tokens"] or 0,
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
            )
            for row in rows
        ]

    def _build_tool_usage_list(self, rows: list) -> list[ToolUsage]:
        """构建工具使用列表."""
        return [
            ToolUsage(
                tool_name=row["tool_name"],
                count=row["count"],
                avg_duration_ms=int(row["avg_duration"] or 0),
                error_count=row["error_count"] or 0,
            )
            for row in rows
        ]

    def _build_skill_usage_list(self, rows: list) -> list[SkillUsage]:
        """构建技能使用列表."""
        return [
            SkillUsage(
                skill_name=row["skill_name"],
                skill_id=row.get("skill_id") or None,
                cn_name=row.get("cn_name") or None,
                count=row["count"],
                avg_duration_ms=int(row["avg_duration"] or 0),
            )
            for row in rows
        ]

    def _build_mcp_tool_usage_list(self, rows: list) -> list[MCPToolUsage]:
        """构建 MCP 工具使用列表."""
        return [
            MCPToolUsage(
                tool_name=row["tool_name"],
                mcp_server=row["mcp_server"],
                count=row["count"],
                avg_duration_ms=int(row["avg_duration"] or 0),
                error_count=row["error_count"] or 0,
            )
            for row in rows
        ]

    # ===== 对话分析 =====

    async def get_traces(
        self,
        source_id: str,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        bbk_ids: Optional[str] = None,
    ) -> tuple[list[TraceListItem], int]:
        """获取对话列表."""
        method_start = time.time()
        logger.info(
            "[get_traces] 开始处理: source_id=%s, page=%s",
            source_id,
            page,
        )
        if source_id == "all":
            exclude_placeholders = ", ".join(
                ["%s"] * len(EXCLUDED_SOURCE_IDS),
            )
            where_clauses: list[str] = [
                f"t.source_id NOT IN ({exclude_placeholders})",
            ]
            params: list[Any] = list(EXCLUDED_SOURCE_IDS)
        else:
            where_clauses = ["t.source_id = %s"]
            params = [source_id]

        if user_id:
            where_clauses.append("t.user_id = %s")
            params.append(user_id)
        if session_id:
            where_clauses.append("t.session_id = %s")
            params.append(session_id)
        if status:
            where_clauses.append("t.status = %s")
            params.append(status)
        if bbk_ids:
            bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
            where_clauses.append(
                f"bbk_id IN ({', '.join(['%s'] * len(bbk_params))})",
            )
            params.extend(bbk_params)
        if start_date:
            where_clauses.append("t.start_time >= %s")
            params.append(start_date)
        if end_date:
            where_clauses.append("t.start_time <= %s")
            params.append(end_date)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        count_query = f"SELECT COUNT(*) as total FROM swe_tracing_traces t WHERE {where_sql}"
        count_row = await self._db.fetch_one(count_query, tuple(params))
        total = count_row["total"] if count_row else 0

        offset = (page - 1) * page_size
        exclude_placeholders = ", ".join(["%s"] * len(EXCLUDED_SOURCE_IDS))
        if source_id == "all":
            query = f"""
                SELECT t.trace_id, t.source_id, t.user_id, t.session_id, t.channel, t.start_time,
                       t.duration_ms, t.total_tokens, t.total_input_tokens, t.total_output_tokens,
                       t.model_name, t.status,
                       JSON_LENGTH(t.skills_used) as skills_count,
                       {FEEDBACK_SELECT_SQL},
                       COALESCE(t.user_name, (
                           SELECT t2.user_name FROM swe_tracing_traces t2
                           WHERE t2.user_id = t.user_id AND t2.user_name IS NOT NULL
                           AND t2.source_id NOT IN ({exclude_placeholders})
                           ORDER BY t2.start_time DESC LIMIT 1
                       )) as user_name,
                       COALESCE(t.bbk_id, (
                           SELECT t3.bbk_id FROM swe_tracing_traces t3
                           WHERE t3.user_id = t.user_id AND t3.bbk_id IS NOT NULL
                           AND t3.source_id NOT IN ({exclude_placeholders})
                           ORDER BY t3.start_time DESC LIMIT 1
                       )) as bbk_id
                FROM swe_tracing_traces t
                {LATEST_FEEDBACK_JOIN_SQL}
                WHERE {where_sql}
                ORDER BY t.start_time DESC
                LIMIT %s OFFSET %s
            """
            # 参数顺序：子查询参数（按 SQL 出现顺序）+ WHERE 参数 + LIMIT/OFFSET
            params = (
                list(EXCLUDED_SOURCE_IDS)  # 子查询1: t2.source_id NOT IN
                + list(EXCLUDED_SOURCE_IDS)  # 子查询2: t3.source_id NOT IN
                + params  # WHERE 子句参数
                + [page_size, offset]
            )
        else:
            query = f"""
                SELECT t.trace_id, t.source_id, t.user_id, t.session_id, t.channel, t.start_time,
                       t.duration_ms, t.total_tokens, t.total_input_tokens, t.total_output_tokens,
                       t.model_name, t.status,
                       JSON_LENGTH(t.skills_used) as skills_count,
                       {FEEDBACK_SELECT_SQL},
                       COALESCE(t.user_name, (
                           SELECT t2.user_name FROM swe_tracing_traces t2
                           WHERE t2.source_id = %s AND t2.user_id = t.user_id AND t2.user_name IS NOT NULL
                           ORDER BY t2.start_time DESC LIMIT 1
                       )) as user_name,
                       COALESCE(t.bbk_id, (
                           SELECT t3.bbk_id FROM swe_tracing_traces t3
                           WHERE t3.source_id = %s AND t3.user_id = t.user_id AND t3.bbk_id IS NOT NULL
                           ORDER BY t3.start_time DESC LIMIT 1
                       )) as bbk_id
                FROM swe_tracing_traces t
                {LATEST_FEEDBACK_JOIN_SQL}
                WHERE {where_sql}
                ORDER BY t.start_time DESC
                LIMIT %s OFFSET %s
            """
            params = [source_id, source_id] + params + [page_size, offset]
        rows = await self._db.fetch_all(query, tuple(params))
        traces = [
            TraceListItem(
                trace_id=row["trace_id"],
                source_id=row["source_id"],
                user_id=row["user_id"],
                user_name=row["user_name"],
                bbk_id=row["bbk_id"],
                session_id=row["session_id"],
                channel=row["channel"],
                start_time=row["start_time"],
                duration_ms=row["duration_ms"],
                total_tokens=row["total_tokens"] or 0,
                total_input_tokens=row["total_input_tokens"] or 0,
                total_output_tokens=row["total_output_tokens"] or 0,
                model_name=row["model_name"],
                status=row["status"],
                skills_count=row["skills_count"] or 0,
                feedback=_row_to_feedback(row),
            )
            for row in rows
        ]
        logger.info(
            "[get_traces] 方法总耗时: %.3fms, total=%d",
            (time.time() - method_start) * 1000,
            total,
        )
        return traces, total

    async def get_trace(
        self,
        trace_id: str,
        source_id: Optional[str] = None,
    ) -> Optional[Trace]:
        """获取单个对话."""
        if source_id:
            query = "SELECT * FROM swe_tracing_traces WHERE trace_id = %s AND source_id = %s"
            row = await self._db.fetch_one(query, (trace_id, source_id))
        else:
            query = "SELECT * FROM swe_tracing_traces WHERE trace_id = %s"
            row = await self._db.fetch_one(query, (trace_id,))
        if row is None:
            return None
        return self._row_to_trace(row)

    async def get_spans(self, trace_id: str) -> list[Span]:
        """获取对话的所有 Span."""
        query = "SELECT * FROM swe_tracing_spans WHERE trace_id = %s ORDER BY start_time"
        rows = await self._db.fetch_all(query, (trace_id,))
        return [self._row_to_span(row) for row in rows]

    async def get_trace_feedback(
        self,
        trace_id: str,
        source_id: Optional[str],
    ) -> Optional[TraceFeedback]:
        """获取对话关联的最新反馈。"""
        query = """
            SELECT
                id as feedback_id,
                source_id as feedback_source_id,
                feedback_user_name,
                feedback_user_sap,
                feedback_branch,
                feedback_sub_branch,
                feedback_position,
                cron_task_name as feedback_cron_task_name,
                cron_task_id as feedback_cron_task_id,
                response_id as feedback_response_id,
                trace_id as feedback_trace_id,
                chat_id as feedback_chat_id,
                session_id as feedback_session_id,
                feedback_options,
                feedback_content,
                created_at as feedback_created_at,
                updated_at as feedback_updated_at
            FROM swe_response_feedback
            WHERE trace_id COLLATE utf8mb4_unicode_ci = CAST(%s AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_unicode_ci
              AND source_id COLLATE utf8mb4_unicode_ci <=> CAST(%s AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_unicode_ci
            ORDER BY id DESC
            LIMIT 1
        """
        row = await self._db.fetch_one(query, (trace_id, source_id))
        if not row:
            return None
        return _row_to_feedback(row)

    async def get_session_traces(
        self,
        session_id: str,
        error_trace_id: Optional[str] = None,
    ) -> Optional[dict]:
        """获取 Session 所有轮次的对话摘要.

        Args:
            session_id: 会话 ID
            error_trace_id: 报错的 Trace ID（用于标记报错轮次）

        Returns:
            包含所有轮次的对话数据
        """
        # 查询该 session 的所有 trace
        query = """
            SELECT
                trace_id,
                source_id,
                user_id,
                user_name,
                bbk_id,
                session_name,
                start_time,
                duration_ms,
                model_name,
                total_input_tokens,
                total_output_tokens,
                tools_used,
                status,
                error,
                user_message
            FROM swe_tracing_traces
            WHERE session_id = %s
            ORDER BY start_time ASC
        """
        rows = await self._db.fetch_all(query, (session_id,))
        if not rows:
            return None

        # 获取 session 基本信息（从第一条记录）
        first_row = rows[0]
        session_name = first_row.get("session_name")
        user_id = first_row.get("user_id", "")
        user_name = first_row.get("user_name")

        # 仅在 completed 状态下从 ES 获取 model_output
        from ...database.elasticsearch import get_es_client

        es_client = get_es_client()

        traces = []
        error_round_index = None

        for idx, row in enumerate(rows):
            trace_id = row["trace_id"]
            model_output = None

            # 仅在 completed 状态下从 ES 获取 model_output
            if (
                row.get("status") == TraceStatus.COMPLETED
                and es_client
                and es_client.is_connected
            ):
                model_output = await es_client.get_message(trace_id)

            # 解析 tools_used
            tools_used = []
            if row.get("tools_used"):
                try:
                    tools_used = json.loads(row["tools_used"])
                except (json.JSONDecodeError, TypeError):
                    pass

            is_error_round = trace_id == error_trace_id

            if is_error_round:
                error_round_index = idx

            traces.append(
                {
                    "trace_id": trace_id,
                    "round_number": idx + 1,
                    "start_time": row["start_time"],
                    "duration_ms": row.get("duration_ms"),
                    "model_name": row.get("model_name"),
                    "status": row.get("status", "completed"),
                    "user_message": row.get("user_message"),
                    "model_output": model_output,
                    "input_tokens": row.get("total_input_tokens", 0),
                    "output_tokens": row.get("total_output_tokens", 0),
                    "tools_used": tools_used,
                    "error": row.get("error"),
                    "is_error_round": is_error_round,
                },
            )

        return {
            "session_id": session_id,
            "session_name": session_name,
            "user_id": user_id,
            "user_name": user_name,
            "traces": traces,
            "total_rounds": len(traces),
            "error_round_index": error_round_index,
        }

    async def get_trace_detail(
        self,
        trace_id: str,
        source_id: Optional[str] = None,
    ) -> Optional[TraceDetail]:
        """获取对话详情."""
        trace = await self.get_trace(trace_id, source_id)
        if trace is None:
            return None

        spans = await self.get_spans(trace_id)

        # 仅在 completed 状态下从 ES 获取 model_output
        if trace.status == TraceStatus.COMPLETED:
            from ...database.elasticsearch import get_es_client

            es_client = get_es_client()
            if es_client and es_client.is_connected:
                trace.model_output = await es_client.get_message(trace_id)

        feedback = await self.get_trace_feedback(trace_id, trace.source_id)

        llm_duration = sum(
            s.duration_ms or 0
            for s in spans
            if s.event_type in (EventType.LLM_INPUT, EventType.LLM_OUTPUT)
        )
        tool_duration = sum(
            s.duration_ms or 0
            for s in spans
            if s.event_type
            in (EventType.TOOL_CALL_START, EventType.TOOL_CALL_END)
        )

        tools_called = []
        tool_spans = [
            s for s in spans if s.event_type == EventType.TOOL_CALL_END
        ]
        for span in tool_spans:
            tools_called.append(
                {
                    "tool_name": span.tool_name or span.name,
                    "tool_input": span.tool_input,
                    "tool_output": span.tool_output,
                    "duration_ms": span.duration_ms,
                    "error": span.error,
                },
            )

        return TraceDetail(
            trace=trace,
            spans=spans,
            llm_duration_ms=llm_duration,
            tool_duration_ms=tool_duration,
            tools_called=tools_called,
            feedback=feedback,
        )

    async def get_trace_detail_with_timeline(
        self,
        trace_id: str,
        source_id: Optional[str] = None,
    ) -> Optional[TraceDetailWithTimeline]:
        """获取对话详情（带时间线）."""
        trace = await self.get_trace(trace_id, source_id)
        if trace is None:
            return None

        spans = await self.get_spans(trace_id)

        # 仅在 completed 状态下从 ES 获取 model_output
        if trace.status == TraceStatus.COMPLETED:
            from ...database.elasticsearch import get_es_client

            es_client = get_es_client()
            if es_client and es_client.is_connected:
                trace.model_output = await es_client.get_message(trace_id)

        feedback = await self.get_trace_feedback(trace_id, trace.source_id)
        timeline = self._build_timeline(spans)
        skill_invocations = self._build_skill_invocations(spans)

        llm_duration = sum(
            s.duration_ms or 0
            for s in spans
            if s.event_type in (EventType.LLM_INPUT, EventType.LLM_OUTPUT)
        )
        tool_duration = sum(
            s.duration_ms or 0
            for s in spans
            if s.event_type
            in (EventType.TOOL_CALL_START, EventType.TOOL_CALL_END)
        )
        skill_duration = sum(inv.duration_ms for inv in skill_invocations)

        return TraceDetailWithTimeline(
            trace=trace,
            feedback=feedback,
            spans=spans,
            timeline=timeline,
            skill_invocations=skill_invocations,
            llm_duration_ms=llm_duration,
            tool_duration_ms=tool_duration,
            skill_duration_ms=skill_duration,
            total_skills=len(skill_invocations),
            total_tools=len(
                [s for s in spans if s.event_type == EventType.TOOL_CALL_END],
            ),
            total_llm_calls=len(
                [s for s in spans if s.event_type == EventType.LLM_INPUT],
            ),
        )

    # ===== 用户消息 =====

    async def get_user_messages(
        self,
        source_id: str,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        query_text: Optional[str] = None,
        export: bool = False,
        bbk_ids: Optional[str] = None,
        exclude_cron_task_sessions: bool = False,
    ) -> tuple[list[UserMessageItem], int]:
        """获取用户消息列表."""
        if start_date is None:
            start_date = datetime.now() - timedelta(days=7)
        if end_date is None:
            end_date = datetime.now()

        if source_id == "all":
            where_clauses = [
                "start_time >= %s",
                "start_time <= %s",
            ]
            params: list[Any] = [start_date, end_date]
        else:
            where_clauses = [
                "source_id = %s",
                "start_time >= %s",
                "start_time <= %s",
            ]
            params = [source_id, start_date, end_date]

        if user_id:
            where_clauses.append("user_id = %s")
            params.append(user_id)
        if session_id:
            where_clauses.append("session_id = %s")
            params.append(session_id)
        if exclude_cron_task_sessions:
            where_clauses.append(
                "(session_id IS NULL OR session_id NOT LIKE %s)",
            )
            params.append("cron-task%")
        if query_text:
            where_clauses.append("user_message LIKE %s")
            params.append(f"%{query_text}%")
        if bbk_ids:
            bbk_filter_sql, bbk_params = build_bbk_in_filter(bbk_ids)
            where_clauses.append(
                f"bbk_id IN ({', '.join(['%s'] * len(bbk_params))})",
            )
            params.extend(bbk_params)

        where_sql = " AND ".join(where_clauses)

        count_query = f"SELECT COUNT(*) as total FROM swe_tracing_traces WHERE {where_sql}"
        count_row = await self._db.fetch_one(count_query, tuple(params))
        total = count_row["total"] if count_row else 0

        if export:
            sql_query = f"""
                SELECT t.trace_id, t.source_id, t.user_id, t.session_id, t.channel, t.user_message,
                       t.model_name,
                       t.start_time, t.duration_ms,
                       COALESCE(t.user_name, (
                           SELECT t2.user_name FROM swe_tracing_traces t2
                           WHERE t2.user_id = t.user_id AND t2.user_name IS NOT NULL
                           ORDER BY t2.start_time DESC LIMIT 1
                       )) as user_name,
                       COALESCE(t.bbk_id, (
                           SELECT t3.bbk_id FROM swe_tracing_traces t3
                           WHERE t3.user_id = t.user_id AND t3.bbk_id IS NOT NULL
                           ORDER BY t3.start_time DESC LIMIT 1
                       )) as bbk_id
                FROM swe_tracing_traces t
                WHERE {where_sql}
                ORDER BY t.start_time DESC
            """
            rows = await self._db.fetch_all(sql_query, tuple(params))
        else:
            offset = (page - 1) * page_size
            sql_query = f"""
                SELECT t.trace_id, t.source_id, t.user_id, t.session_id, t.channel, t.user_message,
                       t.model_name,
                       t.start_time, t.duration_ms,
                       COALESCE(t.user_name, (
                           SELECT t2.user_name FROM swe_tracing_traces t2
                           WHERE t2.user_id = t.user_id AND t2.user_name IS NOT NULL
                           ORDER BY t2.start_time DESC LIMIT 1
                       )) as user_name,
                       COALESCE(t.bbk_id, (
                           SELECT t3.bbk_id FROM swe_tracing_traces t3
                           WHERE t3.user_id = t.user_id AND t3.bbk_id IS NOT NULL
                           ORDER BY t3.start_time DESC LIMIT 1
                       )) as bbk_id
                FROM swe_tracing_traces t
                WHERE {where_sql}
                ORDER BY t.start_time DESC
                LIMIT %s OFFSET %s
            """
            params.extend([page_size, offset])
            rows = await self._db.fetch_all(sql_query, tuple(params))

        messages = [
            UserMessageItem(
                trace_id=row["trace_id"],
                source_id=row["source_id"],
                user_id=row["user_id"],
                user_name=row["user_name"],
                bbk_id=row["bbk_id"],
                session_id=row["session_id"],
                channel=row["channel"],
                user_message=row["user_message"],
                model_name=row["model_name"],
                start_time=row["start_time"],
                duration_ms=row["duration_ms"],
            )
            for row in rows
        ]
        return messages, total

    # ===== 辅助方法 =====

    def _build_timeline(self, spans: list[Span]) -> list[TimelineEvent]:
        """构建时间线（只展示技能调用和LLM调用）."""
        spans = sorted(spans, key=lambda s: s.start_time)

        timeline: list[TimelineEvent] = []
        skill_stack: list[TimelineEvent] = []

        for span in spans:
            if span.event_type == EventType.SKILL_INVOCATION:
                event = TimelineEvent(
                    event_type="skill_invocation",
                    span_id=span.span_id,
                    start_time=span.start_time,
                    end_time=span.end_time,
                    duration_ms=span.duration_ms or 0,
                    skill_name=span.skill_name,
                    confidence=1.0,
                    trigger_reason="declared",
                    children=[],
                )

                if skill_stack:
                    skill_stack[-1].children.append(event)
                else:
                    timeline.append(event)

                skill_stack.append(event)

            elif span.event_type == EventType.SKILL_END:
                # 技能结束时弹出栈
                if skill_stack:
                    skill_stack.pop()

            elif span.event_type == EventType.LLM_INPUT:
                event = TimelineEvent(
                    event_type="llm_call",
                    span_id=span.span_id,
                    start_time=span.start_time,
                    end_time=span.end_time,
                    duration_ms=span.duration_ms or 0,
                    model_name=span.model_name,
                    input_tokens=span.input_tokens,
                    output_tokens=span.output_tokens,
                    children=[],
                )

                if skill_stack:
                    skill_stack[-1].children.append(event)
                else:
                    timeline.append(event)

        return timeline

    def _build_skill_invocations(
        self,
        spans: list[Span],
    ) -> list[SkillCallTimeline]:
        """构建技能调用摘要."""
        skill_spans = [
            s for s in spans if s.event_type == EventType.SKILL_INVOCATION
        ]

        invocations: list[SkillCallTimeline] = []
        skill_tools: dict[str, list[ToolCallInSkill]] = {}

        for span in spans:
            if span.event_type == EventType.TOOL_CALL_END and span.skill_name:
                skill_name = span.skill_name
                if skill_name not in skill_tools:
                    skill_tools[skill_name] = []

                skill_tools[skill_name].append(
                    ToolCallInSkill(
                        span_id=span.span_id,
                        tool_name=span.tool_name or "",
                        mcp_server=span.mcp_server,
                        start_time=span.start_time,
                        end_time=span.end_time,
                        duration_ms=span.duration_ms or 0,
                        status="error" if span.error else "success",
                        error=span.error,
                        skill_weight=None,
                    ),
                )

        for skill_span in skill_spans:
            skill_name = skill_span.skill_name or ""
            tools = skill_tools.get(skill_name, [])

            invocations.append(
                SkillCallTimeline(
                    span_id=skill_span.span_id,
                    skill_name=skill_name,
                    start_time=skill_span.start_time,
                    end_time=skill_span.end_time,
                    duration_ms=skill_span.duration_ms or 0,
                    confidence=1.0,
                    trigger_reason="declared",
                    tools=tools,
                    total_tool_calls=len(tools),
                    tool_duration_ms=sum(t.duration_ms for t in tools),
                ),
            )

        return invocations

    def _row_to_trace(self, row: dict) -> Trace:
        """转换数据库行为 Trace 模型."""
        return Trace(
            trace_id=row["trace_id"],
            b3_trace_id=row.get("b3_trace_id"),
            source_id=row["source_id"],
            user_id=row["user_id"],
            user_name=row.get("user_name"),
            bbk_id=row.get("bbk_id"),
            session_id=row["session_id"],
            session_name=row.get("session_name"),
            channel=row["channel"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            duration_ms=row["duration_ms"],
            model_name=row["model_name"],
            total_input_tokens=row["total_input_tokens"] or 0,
            total_output_tokens=row["total_output_tokens"] or 0,
            tools_used=(
                json.loads(row["tools_used"]) if row["tools_used"] else []
            ),
            skills_used=(
                json.loads(row["skills_used"]) if row["skills_used"] else []
            ),
            status=(
                TraceStatus(row["status"])
                if row["status"]
                else TraceStatus.RUNNING
            ),
            error=row["error"],
            user_message=row.get("user_message"),
        )

    def _row_to_span(self, row: dict) -> Span:
        """转换数据库行为 Span 模型."""
        return Span(
            span_id=row["span_id"],
            trace_id=row["trace_id"],
            source_id=row["source_id"],
            name=row["name"],
            event_type=EventType(row["event_type"]),
            start_time=row["start_time"],
            end_time=row["end_time"],
            duration_ms=row["duration_ms"],
            user_id=row.get("user_id") or "",
            user_name=row.get("user_name"),
            bbk_id=row.get("bbk_id"),
            session_id=row.get("session_id") or "",
            channel=row.get("channel") or "",
            model_name=row["model_name"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            tool_name=row["tool_name"],
            skill_name=row["skill_name"],
            skill_id=row.get("skill_id"),
            mcp_server=row.get("mcp_server"),
            tool_input=(
                json.loads(row["tool_input"]) if row["tool_input"] else None
            ),
            tool_output=row["tool_output"],
            error=row["error"],
        )

    # ===== Input Tokens 修复 =====

    async def check_input_tokens_mismatch(
        self,
        page: int,
        page_size: int,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> tuple[list[InputTokensMismatchItem], int]:
        """检查 input_tokens 不匹配的 trace.

        比对 trace.total_input_tokens 与 span 表汇总值，
        找出存在差异的记录（span_input_sum != trace_input_tokens）。

        Args:
            page: 页码
            page_size: 每页数量
            start_date: 开始日期筛选
            end_date: 结束日期筛选

        Returns:
            (不匹配列表, 总数)
        """
        method_start = time.time()
        logger.info("[check_input_tokens_mismatch] 开始处理: page=%s", page)
        # 构建日期过滤条件
        date_filter = ""
        params: list[Any] = []
        if start_date:
            date_filter += " AND t.start_time >= %s"
            params.append(start_date)
        if end_date:
            date_filter += " AND t.start_time < %s"
            params.append(end_date)

        # 查询总数
        count_query = f"""
            SELECT COUNT(*) as total
            FROM swe_tracing_traces t
            JOIN (
                SELECT trace_id, SUM(input_tokens) as span_input_sum
                FROM swe_tracing_spans
                WHERE input_tokens > 0
                GROUP BY trace_id
            ) s ON s.trace_id = t.trace_id
            WHERE t.total_input_tokens != s.span_input_sum
            {date_filter}
        """
        count_result = await self._db.fetch_one(count_query, tuple(params))
        total = count_result["total"] if count_result else 0

        if total == 0:
            return [], 0

        # 查询列表
        offset = (page - 1) * page_size
        list_query = f"""
            SELECT
                t.trace_id,
                t.total_input_tokens as trace_input_tokens,
                s.span_input_sum,
                (s.span_input_sum - t.total_input_tokens) as input_diff,
                t.user_id,
                t.start_time
            FROM swe_tracing_traces t
            JOIN (
                SELECT trace_id, SUM(input_tokens) as span_input_sum
                FROM swe_tracing_spans
                WHERE input_tokens > 0
                GROUP BY trace_id
            ) s ON s.trace_id = t.trace_id
            WHERE t.total_input_tokens != s.span_input_sum
            {date_filter}
            ORDER BY t.start_time DESC
            LIMIT %s OFFSET %s
        """
        params.extend([page_size, offset])
        rows = await self._db.fetch_all(list_query, tuple(params))

        items = [
            InputTokensMismatchItem(
                trace_id=row["trace_id"],
                trace_input_tokens=row["trace_input_tokens"],
                span_input_sum=row["span_input_sum"],
                input_diff=row["input_diff"],
                user_id=row.get("user_id"),
                start_time=row.get("start_time"),
            )
            for row in rows
        ]

        logger.info(
            "[check_input_tokens_mismatch] 方法总耗时: %.3fms, total=%d",
            (time.time() - method_start) * 1000,
            total,
        )
        return items, total

    async def fix_input_tokens_mismatch(
        self,
        trace_ids: list[str],
        dry_run: bool = True,
    ) -> dict:
        """修复 input_tokens 不匹配.

        将 trace.total_input_tokens 更新为 span 表汇总值。
        dry_run=True 时仅返回预览，不实际更新。

        Args:
            trace_ids: 待修复的 trace_id 列表
            dry_run: 是否为预览模式

        Returns:
            {"fixed_count": int, "items": list[InputTokensFixItem]}
        """
        if not trace_ids:
            return {"fixed_count": 0, "items": []}

        placeholders = ", ".join(["%s"] * len(trace_ids))

        # 查询当前状态和 span 汇总值
        query = f"""
            SELECT
                t.trace_id,
                t.total_input_tokens as old_input_tokens,
                s.span_input_sum
            FROM swe_tracing_traces t
            JOIN (
                SELECT trace_id, SUM(input_tokens) as span_input_sum
                FROM swe_tracing_spans
                WHERE input_tokens > 0
                GROUP BY trace_id
            ) s ON s.trace_id = t.trace_id
            WHERE t.trace_id IN ({placeholders})
              AND t.total_input_tokens != s.span_input_sum
        """
        rows = await self._db.fetch_all(query, tuple(trace_ids))

        if not rows:
            return {"fixed_count": 0, "items": []}

        items = [
            InputTokensFixItem(
                trace_id=row["trace_id"],
                old_input_tokens=row["old_input_tokens"],
                new_input_tokens=row["span_input_sum"],
                span_input_sum=row["span_input_sum"],
            )
            for row in rows
        ]

        if not dry_run:
            # 执行更新
            update_query = f"""
                UPDATE swe_tracing_traces t
                JOIN (
                    SELECT trace_id, SUM(input_tokens) as span_input_sum
                    FROM swe_tracing_spans
                    WHERE input_tokens > 0
                    GROUP BY trace_id
                ) s ON s.trace_id = t.trace_id
                SET t.total_input_tokens = s.span_input_sum
                WHERE t.trace_id IN ({placeholders})
                  AND t.total_input_tokens != s.span_input_sum
            """
            await self._db.execute(update_query, tuple(trace_ids))
            logger.info(f"Fixed input_tokens for {len(items)} traces")

        return {"fixed_count": len(items), "items": items}
