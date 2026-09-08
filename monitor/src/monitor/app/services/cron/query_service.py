# -*- coding: utf-8 -*-
"""Query service for cron job and execution data.

Provides methods to query job definitions and execution history
for the frontend overview page.
"""

import asyncio
import calendar
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter

from ...database import DatabaseConnection, get_db_connection
from ...models.cron import (
    CronOverviewBranchExecutionItem,
    CronOverviewBranchReadItem,
    CronOverviewDistributionItem,
    CronOverviewMetricItem,
    CronOverviewResponse,
    CronOverviewStatsResponse,
    CronDispatchBatchDetailResponse,
    CronDispatchBatchItem,
    CronDispatchBatchStats,
    CronDispatchBatchesResponse,
    CronDispatchCapacityItem,
    CronDispatchDetailQueryParams,
    CronDispatchEventItem,
    CronDispatchIntentItem,
    CronDispatchPolicyItem,
    CronDispatchWorkersResponse,
    CronBranchRankingResponse,
    CronBranchRankingItem,
    CronBranchErrorResponse,
    CronBranchErrorRankItem,
    CronErrorReasonItem,
    BranchSkillItem,
    BranchSkillResponse,
    BranchManagerSummaryItem,
    BranchManagerSummaryResponse,
    BranchSkillManagerItem,
    BranchSkillManagerResponse,
    BranchSkillManagerCustomerItem,
    BranchSkillManagerCustomerResponse,
    ManagerSkillItem,
    ManagerSkillResponse,
    ManagerCustomerItem,
    ManagerCustomerResponse,
    CronBranchTaskRankingItem,
    CronBranchTaskRankingResponse,
    CronJobModel,
    CronJobQueryParams,
    CronScheduleDistributionBucket,
    CronScheduleDistributionDetailsResponse,
    CronScheduleDistributionDiagnostics,
    CronScheduleDistributionResponse,
    CronScheduleOccurrenceItem,
    ExecutionModel,
    ExecutionQueryParams,
    LatestExecutionSubtaskCountResponse,
    PaginatedResponse,
    SubscriptionDetailItem,
    SubscriptionOverviewItem,
    TaskType,
    UnreadCountResponse,
)
from ....utils.bbk import get_bbk_name_by_id

# 东八区时区（北京时间）
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# 注意：数据库存储的时间已经是东八区时间（北京时间），无需再转换
# monitor_sync_client.py 在写入时已将 UTC 转为东八区，直接读取即可

logger = logging.getLogger(__name__)


# 任务定义表的时间字段（无需转换，直接读取）
JOB_TIME_FIELDS = ["created_at", "updated_at", "deleted_at"]

# 执行历史表的时间字段（无需转换，直接读取）
EXECUTION_TIME_FIELDS = [
    "scheduled_time",
    "actual_time",
    "end_time",
    "notification_due_at",
    "notification_sent_at",
    "notification_locked_at",
    "created_at",
]

DISPATCH_BATCH_TIME_FIELDS = [
    "scheduled_fire_at",
    "callback_received_at",
    "locked_at",
    "completed_at",
    "created_at",
    "updated_at",
]

DISPATCH_INTENT_TIME_FIELDS = [
    "scheduled_fire_at",
    "due_at",
    "locked_at",
    "acked_at",
    "completed_at",
    "created_at",
    "updated_at",
]

DISPATCH_EVENT_TIME_FIELDS = ["created_at"]
DISPATCH_CAPACITY_TIME_FIELDS = ["created_at"]
DISPATCH_POLICY_TIME_FIELDS = ["created_at", "updated_at"]
DISPATCH_CAPACITY_EVENT_LIMIT = 100
SCHEDULE_BUCKET_MINUTES = frozenset({5, 10, 15, 30, 60})
SCHEDULE_RANGE_LIMIT = timedelta(days=7)
SCHEDULE_OCCURRENCE_LIMIT = 100_000
SCHEDULE_DISPATCH_INTENTS_ENABLED_ENV = "SWE_CRON_DISPATCH_INTENTS_ENABLED"
SCHEDULE_BROADCAST_SOURCE_JOB_ID_META_KEY = "broadcast_source_job_id"
SCHEDULE_BROADCAST_INTENTS_ENABLED_META_KEY = (
    "broadcast_dispatch_intents_enabled"
)
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


class ScheduleDistributionError(ValueError):
    """Base class for typed planned schedule calculation errors."""

    code = "schedule_distribution_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ScheduleDistributionValidationError(ScheduleDistributionError):
    """The requested range, bucket, filter, or pagination is invalid."""

    code = "schedule_distribution_validation_error"


class ScheduleCalculationLimitExceededError(ScheduleDistributionError):
    """The calculation exceeded its bounded occurrence budget."""

    code = "schedule_calculation_limit_exceeded"


class ScheduleDefinitionRevisionConflictError(ScheduleDistributionError):
    """The job definitions changed between paginated detail requests."""

    code = "schedule_definition_revision_conflict"

    def __init__(self, actual_revision: str) -> None:
        super().__init__(
            "Scheduled job definitions changed; restart detail pagination "
            "from page one.",
        )
        self.actual_revision = actual_revision


@dataclass(frozen=True)
class _ScheduleOccurrence:
    scheduled_at: datetime
    job_id: str
    job_name: str
    user_name: str
    user_id: str
    task_type: TaskType
    cron_expr: str
    timezone: str


@dataclass(frozen=True)
class _PreparedScheduleDefinition:
    job_id: str
    job_name: str
    user_name: str
    user_id: str
    task_type: TaskType
    cron_expr: str
    timezone_name: str
    job_timezone: tzinfo


@dataclass(frozen=True)
class _ScheduleCalculation:
    start_time: datetime
    end_time: datetime
    calculated_at: datetime
    definition_revision: str
    eligible_job_count: int
    occurrences: list[_ScheduleOccurrence]
    diagnostics: CronScheduleDistributionDiagnostics


def convert_row_times_direct(row: dict, time_fields: List[str]) -> dict:
    """直接读取时间字段，不做时区转换。

    数据库存储的已经是东八区时间，无需转换。

    Args:
        row: 数据库返回的行字典
        time_fields: 时间字段名列表

    Returns:
        原始行字典（时间字段不变）
    """
    return row


def _parse_json_field(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class QueryService:
    """Service for querying cron data."""

    @staticmethod
    def _skill_display_expr(skill_alias: str = "s") -> str:
        """构建技能展示名表达式。"""
        return (
            f"COALESCE(NULLIF({skill_alias}.cn_name, ''), "
            f"NULLIF({skill_alias}.skill_name, ''), {skill_alias}.skill_id)"
        )

    @staticmethod
    def _skill_binding_join(
        job_alias: str = "j",
        skill_alias: str = "s",
    ) -> str:
        """构建定时任务与技能表的关联条件。"""
        return (
            f"JOIN ("
            f"SELECT source_id, skill_id, "
            f"MIN(cn_name) AS cn_name, MIN(skill_name) AS skill_name "
            f"FROM swe_marketplace_skills "
            f"WHERE include_in_statistics = 1 "
            f"AND skill_id IS NOT NULL AND skill_id != '' "
            f"GROUP BY source_id, skill_id"
            f") {skill_alias} "
            f"ON FIND_IN_SET({skill_alias}.skill_id, {job_alias}.skill_ids) "
            f"AND {skill_alias}.source_id = {job_alias}.source_id"
        )

    @staticmethod
    def _statistics_skill_exists(
        job_alias: str = "j",
        skill_alias: str = "stat_skill",
    ) -> str:
        """判断任务绑定技能中是否存在统计技能。"""
        return (
            "EXISTS ("
            f"SELECT 1 FROM swe_marketplace_skills {skill_alias} "
            f"WHERE FIND_IN_SET({skill_alias}.skill_id, {job_alias}.skill_ids) "
            f"AND {skill_alias}.source_id = {job_alias}.source_id "
            f"AND {skill_alias}.include_in_statistics = 1"
            ")"
        )

    def __init__(self) -> None:
        """Initialize query service."""
        pass

    @staticmethod
    def _normalize_schedule_range(
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[datetime, datetime]:
        """Validate a bounded offset-aware range and normalize it to UTC."""
        if (
            start_time.tzinfo is None
            or start_time.utcoffset() is None
            or end_time.tzinfo is None
            or end_time.utcoffset() is None
        ):
            raise ScheduleDistributionValidationError(
                "start_time and end_time must include a UTC offset.",
            )
        normalized_start = start_time.astimezone(timezone.utc)
        normalized_end = end_time.astimezone(timezone.utc)
        if normalized_end <= normalized_start:
            raise ScheduleDistributionValidationError(
                "end_time must be later than start_time.",
            )
        if normalized_end - normalized_start > SCHEDULE_RANGE_LIMIT:
            raise ScheduleDistributionValidationError(
                "Schedule distribution range cannot exceed seven days.",
            )
        return normalized_start, normalized_end

    @staticmethod
    def _schedule_runtime_dispatch_enabled() -> bool:
        raw_value = os.environ.get(
            SCHEDULE_DISPATCH_INTENTS_ENABLED_ENV,
            "",
        )
        return raw_value.strip().lower() in _TRUE_ENV_VALUES

    @staticmethod
    def _parse_schedule_metadata(
        raw_value: Any,
    ) -> tuple[dict[str, Any], bool]:
        """Return parsed metadata and whether a non-empty value was invalid."""
        if raw_value is None or raw_value == "":
            return {}, False
        if isinstance(raw_value, dict):
            return raw_value, False
        if isinstance(raw_value, (bytes, bytearray)):
            raw_value = raw_value.decode("utf-8", errors="ignore")
        if not isinstance(raw_value, str):
            return {}, True
        try:
            parsed = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            return {}, True
        if not isinstance(parsed, dict):
            return {}, True
        return parsed, False

    @staticmethod
    def _schedule_definition_revision(
        rows: list[dict[str, Any]],
        *,
        runtime_dispatch_enabled: bool,
    ) -> str:
        """Hash only definition fields that can change the returned result."""
        definitions = [
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "user_name": str(row.get("tenant_name") or ""),
                "user_id": str(row.get("tenant_id") or ""),
                "task_type": str(row.get("task_type") or ""),
                "cron_expr": str(row.get("cron_expr") or ""),
                "timezone": str(row.get("timezone") or "UTC"),
                "meta": row.get("meta"),
            }
            for row in sorted(
                rows,
                key=lambda item: str(item.get("id") or ""),
            )
        ]
        encoded = json.dumps(
            {
                "runtime_dispatch_enabled": runtime_dispatch_enabled,
                "definitions": definitions,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def _load_schedule_definition_rows(
        self,
        source_id: str,
    ) -> list[dict[str, Any]]:
        """Load only current active schedule definitions for one source."""
        db = get_db_connection()
        return await db.fetch_all(
            """
            SELECT
                id, name, tenant_name, tenant_id, source_id, enabled,
                task_type, cron_expr,
                timezone, meta, status, updated_at, deleted_at
            FROM swe_cron_jobs
            WHERE source_id = %s
              AND enabled = 1
              AND status = 'active'
              AND deleted_at IS NULL
            ORDER BY id
            """,
            (source_id,),
        )

    @staticmethod
    def _increment_schedule_diagnostic(
        diagnostics: CronScheduleDistributionDiagnostics,
        field_name: str,
    ) -> None:
        current = int(getattr(diagnostics, field_name))
        setattr(diagnostics, field_name, current + 1)

    def _prepare_schedule_definition(
        self,
        row: dict[str, Any],
        *,
        diagnostics: CronScheduleDistributionDiagnostics,
        runtime_dispatch_enabled: bool,
    ) -> _PreparedScheduleDefinition | None:
        """Validate one definition and preserve existing diagnostic semantics."""
        task_type_value = str(row.get("task_type") or "")
        if task_type_value not in {TaskType.TEXT.value, TaskType.AGENT.value}:
            self._increment_schedule_diagnostic(
                diagnostics,
                "unsupported_task_type_jobs",
            )
            return None
        task_type = TaskType(task_type_value)

        metadata, invalid_metadata = self._parse_schedule_metadata(
            row.get("meta"),
        )
        if invalid_metadata:
            self._increment_schedule_diagnostic(
                diagnostics,
                "invalid_metadata_jobs",
            )
        if (
            runtime_dispatch_enabled
            and metadata.get(SCHEDULE_BROADCAST_SOURCE_JOB_ID_META_KEY)
            and bool(
                metadata.get(
                    SCHEDULE_BROADCAST_INTENTS_ENABLED_META_KEY,
                ),
            )
        ):
            self._increment_schedule_diagnostic(
                diagnostics,
                "managed_child_jobs",
            )
            return None

        cron_expr = str(row.get("cron_expr") or "").strip()
        if len(cron_expr.split()) != 5 or not croniter.is_valid(cron_expr):
            self._increment_schedule_diagnostic(
                diagnostics,
                "invalid_cron_jobs",
            )
            return None

        timezone_name = str(row.get("timezone") or "UTC")
        try:
            job_timezone: tzinfo = ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            job_timezone = timezone.utc
            self._increment_schedule_diagnostic(
                diagnostics,
                "invalid_timezone_jobs",
            )

        return _PreparedScheduleDefinition(
            job_id=str(row.get("id") or ""),
            job_name=str(row.get("name") or ""),
            user_name=str(row.get("tenant_name") or ""),
            user_id=str(row.get("tenant_id") or ""),
            task_type=task_type,
            cron_expr=cron_expr,
            timezone_name=timezone_name,
            job_timezone=job_timezone,
        )

    @staticmethod
    def _iter_schedule_times(
        definition: _PreparedScheduleDefinition,
        *,
        start_time: datetime,
        end_time: datetime,
    ) -> Iterator[datetime]:
        """Yield UTC firing instants with Scheduler-compatible boundaries."""
        local_start = start_time.astimezone(definition.job_timezone)
        iterator = croniter(
            definition.cron_expr,
            local_start - timedelta(microseconds=1),
        )
        while True:
            upcoming = iterator.get_next(datetime)
            if upcoming.tzinfo is None:
                upcoming = upcoming.replace(
                    tzinfo=definition.job_timezone,
                )
            scheduled_at = upcoming.astimezone(timezone.utc)
            if scheduled_at >= end_time:
                break
            if scheduled_at >= start_time:
                yield scheduled_at

    def _generate_schedule_occurrences(
        self,
        rows: list[dict[str, Any]],
        *,
        start_time: datetime,
        end_time: datetime,
        runtime_dispatch_enabled: bool,
    ) -> tuple[
        list[_ScheduleOccurrence],
        CronScheduleDistributionDiagnostics,
    ]:
        """Expand eligible definitions using Scheduler-compatible semantics."""
        diagnostics = CronScheduleDistributionDiagnostics()
        occurrences: list[_ScheduleOccurrence] = []
        for row in rows:
            definition = self._prepare_schedule_definition(
                row,
                diagnostics=diagnostics,
                runtime_dispatch_enabled=runtime_dispatch_enabled,
            )
            if definition is None:
                continue
            job_occurrences: list[_ScheduleOccurrence] = []
            try:
                for scheduled_at in self._iter_schedule_times(
                    definition,
                    start_time=start_time,
                    end_time=end_time,
                ):
                    if (
                        len(occurrences) + len(job_occurrences)
                        >= SCHEDULE_OCCURRENCE_LIMIT
                    ):
                        raise ScheduleCalculationLimitExceededError(
                            "Schedule calculation exceeded 100,000 "
                            "planned occurrences.",
                        )
                    job_occurrences.append(
                        _ScheduleOccurrence(
                            scheduled_at=scheduled_at,
                            job_id=definition.job_id,
                            job_name=definition.job_name,
                            user_name=definition.user_name,
                            user_id=definition.user_id,
                            task_type=definition.task_type,
                            cron_expr=definition.cron_expr,
                            timezone=definition.timezone_name,
                        ),
                    )
            except ScheduleCalculationLimitExceededError:
                raise
            except (CroniterBadCronError, CroniterBadDateError, ValueError):
                self._increment_schedule_diagnostic(
                    diagnostics,
                    "invalid_cron_jobs",
                )
                continue
            occurrences.extend(job_occurrences)

        occurrences.sort(key=lambda item: (item.scheduled_at, item.job_id))
        return occurrences, diagnostics

    def _populate_schedule_distribution_buckets(
        self,
        rows: list[dict[str, Any]],
        *,
        start_time: datetime,
        end_time: datetime,
        bucket_width: timedelta,
        buckets: list[CronScheduleDistributionBucket],
        runtime_dispatch_enabled: bool,
    ) -> CronScheduleDistributionDiagnostics:
        """Count firing instants directly into buckets without materializing rows."""
        diagnostics = CronScheduleDistributionDiagnostics()
        accepted_occurrence_count = 0
        bucket_width_seconds = bucket_width.total_seconds()

        for row in rows:
            definition = self._prepare_schedule_definition(
                row,
                diagnostics=diagnostics,
                runtime_dispatch_enabled=runtime_dispatch_enabled,
            )
            if definition is None:
                continue

            job_bucket_counts: dict[int, int] = {}
            job_occurrence_count = 0
            try:
                for scheduled_at in self._iter_schedule_times(
                    definition,
                    start_time=start_time,
                    end_time=end_time,
                ):
                    if (
                        accepted_occurrence_count + job_occurrence_count
                        >= SCHEDULE_OCCURRENCE_LIMIT
                    ):
                        raise ScheduleCalculationLimitExceededError(
                            "Schedule calculation exceeded 100,000 "
                            "planned occurrences.",
                        )
                    bucket_index = int(
                        (scheduled_at - start_time).total_seconds()
                        // bucket_width_seconds,
                    )
                    job_bucket_counts[bucket_index] = (
                        job_bucket_counts.get(bucket_index, 0) + 1
                    )
                    job_occurrence_count += 1
            except ScheduleCalculationLimitExceededError:
                raise
            except (CroniterBadCronError, CroniterBadDateError, ValueError):
                self._increment_schedule_diagnostic(
                    diagnostics,
                    "invalid_cron_jobs",
                )
                continue

            for bucket_index, count in job_bucket_counts.items():
                bucket = buckets[bucket_index]
                if definition.task_type is TaskType.TEXT:
                    bucket.text_count += count
                else:
                    bucket.agent_count += count
                bucket.total_count += count
            accepted_occurrence_count += job_occurrence_count

        return diagnostics

    async def _calculate_schedule_occurrences(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> _ScheduleCalculation:
        normalized_start, normalized_end = self._normalize_schedule_range(
            start_time,
            end_time,
        )
        rows = await self._load_schedule_definition_rows(source_id)
        runtime_dispatch_enabled = self._schedule_runtime_dispatch_enabled()
        definition_revision = self._schedule_definition_revision(
            rows,
            runtime_dispatch_enabled=runtime_dispatch_enabled,
        )
        occurrences, diagnostics = self._generate_schedule_occurrences(
            rows,
            start_time=normalized_start,
            end_time=normalized_end,
            runtime_dispatch_enabled=runtime_dispatch_enabled,
        )
        eligible_job_count = max(
            len(rows)
            - diagnostics.invalid_cron_jobs
            - diagnostics.unsupported_task_type_jobs
            - diagnostics.managed_child_jobs,
            0,
        )
        return _ScheduleCalculation(
            start_time=normalized_start,
            end_time=normalized_end,
            calculated_at=datetime.now(timezone.utc),
            definition_revision=definition_revision,
            eligible_job_count=eligible_job_count,
            occurrences=occurrences,
            diagnostics=diagnostics,
        )

    async def get_schedule_distribution(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        bucket_minutes: int,
    ) -> CronScheduleDistributionResponse:
        """Return Text/Agent planned firing counts in start-aligned buckets."""
        if bucket_minutes not in SCHEDULE_BUCKET_MINUTES:
            raise ScheduleDistributionValidationError(
                "bucket_minutes must be one of 5, 10, 15, 30, or 60.",
            )
        normalized_start, normalized_end = self._normalize_schedule_range(
            start_time,
            end_time,
        )
        rows = await self._load_schedule_definition_rows(source_id)
        runtime_dispatch_enabled = self._schedule_runtime_dispatch_enabled()
        definition_revision = self._schedule_definition_revision(
            rows,
            runtime_dispatch_enabled=runtime_dispatch_enabled,
        )
        bucket_width = timedelta(minutes=bucket_minutes)
        bucket_count = int(
            (normalized_end - normalized_start).total_seconds()
            // bucket_width.total_seconds(),
        ) + (
            1
            if (normalized_end - normalized_start).total_seconds()
            % bucket_width.total_seconds()
            else 0
        )
        buckets = [
            CronScheduleDistributionBucket(
                start_time=normalized_start + index * bucket_width,
                end_time=min(
                    normalized_start + (index + 1) * bucket_width,
                    normalized_end,
                ),
            )
            for index in range(bucket_count)
        ]
        diagnostics = self._populate_schedule_distribution_buckets(
            rows,
            start_time=normalized_start,
            end_time=normalized_end,
            bucket_width=bucket_width,
            buckets=buckets,
            runtime_dispatch_enabled=runtime_dispatch_enabled,
        )
        eligible_job_count = max(
            len(rows)
            - diagnostics.invalid_cron_jobs
            - diagnostics.unsupported_task_type_jobs
            - diagnostics.managed_child_jobs,
            0,
        )

        text_count = sum(bucket.text_count for bucket in buckets)
        agent_count = sum(bucket.agent_count for bucket in buckets)
        return CronScheduleDistributionResponse(
            start_time=normalized_start,
            end_time=normalized_end,
            bucket_minutes=bucket_minutes,
            calculated_at=datetime.now(timezone.utc),
            definition_revision=definition_revision,
            eligible_job_count=eligible_job_count,
            text_count=text_count,
            agent_count=agent_count,
            total_count=text_count + agent_count,
            buckets=buckets,
            diagnostics=diagnostics,
        )

    async def get_schedule_distribution_details(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        task_type: TaskType | str | None = None,
        page: int = 1,
        page_size: int = 20,
        expected_revision: str | None = None,
    ) -> CronScheduleDistributionDetailsResponse:
        """Return stable, paginated, whitelisted planned firing rows."""
        if page < 1:
            raise ScheduleDistributionValidationError(
                "page must be greater than or equal to one.",
            )
        if page_size < 1 or page_size > 100:
            raise ScheduleDistributionValidationError(
                "page_size must be between one and 100.",
            )
        if page > 1 and not expected_revision:
            raise ScheduleDistributionValidationError(
                "expected_revision is required after the first page.",
            )
        normalized_task_type: TaskType | None = None
        if task_type is not None:
            try:
                normalized_task_type = TaskType(task_type)
            except ValueError as exc:
                raise ScheduleDistributionValidationError(
                    "task_type must be text or agent.",
                ) from exc

        calculation = await self._calculate_schedule_occurrences(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
        )
        if (
            expected_revision
            and expected_revision != calculation.definition_revision
        ):
            raise ScheduleDefinitionRevisionConflictError(
                calculation.definition_revision,
            )
        occurrences = calculation.occurrences
        if normalized_task_type is not None:
            occurrences = [
                item
                for item in occurrences
                if item.task_type is normalized_task_type
            ]
        total = len(occurrences)
        offset = (page - 1) * page_size
        page_occurrences = occurrences[offset : offset + page_size]
        return CronScheduleDistributionDetailsResponse(
            start_time=calculation.start_time,
            end_time=calculation.end_time,
            task_type=normalized_task_type,
            calculated_at=calculation.calculated_at,
            definition_revision=calculation.definition_revision,
            items=[
                CronScheduleOccurrenceItem(
                    scheduled_at=item.scheduled_at,
                    job_id=item.job_id,
                    job_name=item.job_name,
                    user_name=item.user_name,
                    user_id=item.user_id,
                    task_type=item.task_type,
                    cron_expr=item.cron_expr,
                    timezone=item.timezone,
                )
                for item in page_occurrences
            ],
            total=total,
            page=page,
            page_size=page_size,
            diagnostics=calculation.diagnostics,
        )

    def _build_job_where_clause(
        self,
        params: CronJobQueryParams,
    ) -> tuple[str, list]:
        """构建任务查询的 WHERE 子句。

        Args:
            params: 查询参数

        Returns:
            (where_clause, sql_params) 元组
        """
        conditions = ["deleted_at IS NULL"]
        sql_params: list = []

        if params.tenant_id:
            conditions.append("tenant_id = %s")
            sql_params.append(params.tenant_id)
        if params.bbk_id:
            conditions.append("bbk_id = %s")
            sql_params.append(params.bbk_id)
        if params.source_id:
            conditions.append("source_id = %s")
            sql_params.append(params.source_id)
        if params.creator_user_id:
            conditions.append("creator_user_id = %s")
            sql_params.append(params.creator_user_id)
        if params.job_origin:
            conditions.append("job_origin = %s")
            sql_params.append(params.job_origin)
        if params.status:
            conditions.append("status = %s")
            sql_params.append(params.status)
        if params.enabled is not None:
            conditions.append("enabled = %s")
            sql_params.append(params.enabled)

        return " AND ".join(conditions), sql_params

    async def _fetch_job_execution_counts(
        self,
        db: DatabaseConnection,
        job_ids: list[str],
    ) -> dict[str, int]:
        """查询任务的执行次数。

        Args:
            db: 数据库连接
            job_ids: 任务ID列表

        Returns:
            job_id -> count 的映射字典
        """
        if not job_ids:
            return {}
        placeholders = ",".join("%s" for _ in job_ids)
        sql = f"""
            SELECT job_id, COUNT(*) as count
            FROM swe_cron_executions
            WHERE job_id IN ({placeholders})
            GROUP BY job_id
        """
        rows = await db.fetch_all(sql, tuple(job_ids))
        return {row.get("job_id"): row.get("count", 0) for row in rows}

    def _determine_execution_status(
        self,
        status: str,
        async_status: str | None,
    ) -> str:
        """根据执行状态和异步状态确定综合状态。

        Args:
            status: 执行状态
            async_status: 异步状态

        Returns:
            综合状态字符串
        """
        if status == "success":
            if async_status == "success":
                return "success"
            if async_status == "error":
                return "error"
            return "running"
        if status in ("error", "timeout"):
            return "error"
        if status in ("cancelled", "skipped"):
            return status
        return status

    async def _fetch_job_today_status(
        self,
        db: DatabaseConnection,
        job_ids: list[str],
    ) -> dict[str, str]:
        """查询任务今日最新执行状态。

        Args:
            db: 数据库连接
            job_ids: 任务ID列表

        Returns:
            job_id -> status 的映射字典
        """
        if not job_ids:
            return {}
        placeholders = ",".join("%s" for _ in job_ids)
        today_start = (
            datetime.now(BEIJING_TZ)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .replace(tzinfo=None)
        )
        sql = f"""
            SELECT job_id, status, async_status
            FROM swe_cron_executions
            WHERE job_id IN ({placeholders})
            AND actual_time >= %s
            ORDER BY actual_time DESC
        """
        rows = await db.fetch_all(sql, tuple(job_ids) + (today_start,))
        status_map: dict[str, str] = {}
        for row in rows:
            job_id = row.get("job_id")
            if job_id not in status_map:
                status_map[job_id] = self._determine_execution_status(
                    row.get("status"),
                    row.get("async_status"),
                )
        return status_map

    def _build_dispatch_batch_where_clause(
        self,
        source_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        """构建批调度 batch 查询条件。"""
        conditions = ["b.source_id = %s"]
        sql_params: list[Any] = [source_id]

        if start_time:
            conditions.append("b.scheduled_fire_at >= %s")
            sql_params.append(start_time)
        if end_time:
            conditions.append("b.scheduled_fire_at <= %s")
            sql_params.append(end_time)
        if status and status != "all":
            conditions.append("b.status = %s")
            sql_params.append(status)
        normalized_query = (query or "").strip().lower()
        if normalized_query:
            escaped_query = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            searchable_fields = (
                "b.batch_id",
                "b.parent_job_id",
                "b.parent_external_job_id",
                "b.tenant_id",
                "b.provider_id",
                "b.model_id",
                "b.agent_id",
                "j.name",
            )
            conditions.append(
                "("
                + " OR ".join(
                    f"LOWER(COALESCE({field}, '')) LIKE %s ESCAPE '\\\\'"
                    for field in searchable_fields
                )
                + ")",
            )
            sql_params.extend(
                [f"%{escaped_query}%"] * len(searchable_fields),
            )

        return " AND ".join(conditions), sql_params

    def _map_dispatch_batch(self, row: dict) -> CronDispatchBatchItem:
        return CronDispatchBatchItem.model_validate(
            convert_row_times_direct(row, DISPATCH_BATCH_TIME_FIELDS),
        )

    def _map_dispatch_intent(self, row: dict) -> CronDispatchIntentItem:
        item = convert_row_times_direct(row, DISPATCH_INTENT_TIME_FIELDS)
        item["viewer_heat_score"] = _to_float(item.get("viewer_heat_score"))
        return CronDispatchIntentItem.model_validate(item)

    def _map_dispatch_event(self, row: dict) -> CronDispatchEventItem:
        item = convert_row_times_direct(row, DISPATCH_EVENT_TIME_FIELDS)
        item["details"] = _parse_json_field(item.get("details"))
        return CronDispatchEventItem.model_validate(item)

    def _map_dispatch_capacity(self, row: dict) -> CronDispatchCapacityItem:
        item = convert_row_times_direct(row, DISPATCH_CAPACITY_TIME_FIELDS)
        item["error_rate"] = _to_float(item.get("error_rate"))
        item["matched_rule"] = _parse_json_field(item.get("matched_rule"))
        return CronDispatchCapacityItem.model_validate(item)

    def _map_dispatch_policy(self, row: dict) -> CronDispatchPolicyItem:
        item = convert_row_times_direct(row, DISPATCH_POLICY_TIME_FIELDS)
        strategy_schedule = _parse_json_field(item.get("strategy_schedule"))
        if isinstance(strategy_schedule, list):
            strategy_schedule = [
                entry for entry in strategy_schedule if isinstance(entry, dict)
            ]
        else:
            strategy_schedule = None
        strategy = {
            "strategy_id": item.get("strategy_id")
            or item.get("default_strategy_id"),
            "min_workers": item.get("min_workers"),
            "baseline_workers": item.get("baseline_workers"),
            "max_workers": item.get("max_workers"),
            "adjust_interval_seconds": item.get("adjust_interval_seconds"),
            "feedback_window_seconds": item.get("feedback_window_seconds"),
            "stale_execution_seconds": item.get("stale_execution_seconds"),
            "error_rate_rules": _parse_json_field(
                item.get("error_rate_rules"),
            ),
            "description": item.get("description") or "",
            "enabled": bool(item.get("strategy_enabled", True)),
        }
        return CronDispatchPolicyItem(
            source_id=str(item.get("source_id") or ""),
            provider_id=str(item.get("provider_id") or ""),
            model_id=str(item.get("model_id") or ""),
            default_strategy_id=str(item.get("default_strategy_id") or ""),
            strategy_schedule=strategy_schedule,
            enabled=bool(item.get("enabled", True)),
            strategy=strategy,
            created_at=item.get("created_at"),
            updated_at=item.get("updated_at"),
        )

    def _build_dispatch_intent_where_clause(
        self,
        *,
        source_id: str,
        batch_id: str,
        params: CronDispatchDetailQueryParams,
    ) -> tuple[str, list[Any]]:
        conditions = ["source_id = %s", "batch_id = %s"]
        sql_params: list[Any] = [source_id, batch_id]
        if params.intent_role and params.intent_role != "all":
            conditions.append("intent_role = %s")
            sql_params.append(params.intent_role)
        if params.intent_status and params.intent_status != "all":
            conditions.append("status = %s")
            sql_params.append(params.intent_status)
        normalized_query = (params.intent_query or "").strip().lower()
        if normalized_query:
            escaped_query = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            searchable_fields = (
                "CAST(id AS CHAR)",
                "tenant_id",
                "job_id",
                "parent_job_id",
                "agent_id",
                "provider_id",
                "model_id",
                "error_message",
            )
            conditions.append(
                "("
                + " OR ".join(
                    f"LOWER(COALESCE({field}, '')) LIKE %s ESCAPE '\\\\'"
                    for field in searchable_fields
                )
                + ")",
            )
            sql_params.extend([f"%{escaped_query}%"] * len(searchable_fields))
        return " AND ".join(conditions), sql_params

    async def _fetch_dispatch_worker_policy_rows(
        self,
        db: DatabaseConnection,
        source_id: str,
    ) -> List[dict]:
        return await db.fetch_all(
            """
            SELECT
                p.source_id, p.provider_id, p.model_id,
                p.default_strategy_id, p.strategy_schedule, p.enabled,
                p.created_at, p.updated_at,
                s.strategy_id, s.min_workers, s.baseline_workers, s.max_workers,
                s.adjust_interval_seconds, s.feedback_window_seconds,
                s.stale_execution_seconds, s.error_rate_rules,
                s.enabled AS strategy_enabled, s.description
            FROM swe_cron_dispatch_model_worker_policy p
            LEFT JOIN swe_cron_dispatch_worker_strategy s
                ON p.default_strategy_id = s.strategy_id
            WHERE p.source_id = %s
            ORDER BY p.provider_id, p.model_id
            """,
            (source_id,),
        )

    async def _fetch_current_dispatch_capacity_rows(
        self,
        db: DatabaseConnection,
        source_id: str,
    ) -> List[dict]:
        return await db.fetch_all(
            """
            SELECT
                c.id,
                COALESCE(NULLIF(scope_lease.lock_owner, ''), c.worker_id)
                    AS worker_id,
                c.source_id, c.provider_id, c.model_id, c.strategy_id,
                c.previous_workers, c.baseline_workers, c.min_workers,
                c.max_workers, c.effective_workers, c.pending_count,
                c.claimed_count, c.running_count, c.success_count,
                c.failure_count, c.error_rate, c.matched_rule,
                c.avg_latency_ms, c.decision_reason, c.created_at
            FROM swe_cron_dispatch_worker_capacity c
            INNER JOIN (
                SELECT source_id, provider_id, model_id, MAX(id) AS id
                FROM swe_cron_dispatch_worker_capacity
                WHERE source_id = %s
                GROUP BY source_id, provider_id, model_id
            ) latest ON c.id = latest.id
            LEFT JOIN swe_cron_dispatch_scope_leases scope_lease
                ON scope_lease.source_id = c.source_id
                AND scope_lease.provider_id = c.provider_id
                AND scope_lease.model_id = c.model_id
                AND scope_lease.lease_expires_at >= NOW()
            ORDER BY c.provider_id, c.model_id
            """,
            (source_id,),
        )

    async def _fetch_dispatch_capacity_event_rows(
        self,
        db: DatabaseConnection,
        *,
        source_id: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[dict]:
        conditions = ["source_id = %s"]
        params: List[Any] = [source_id]
        if start_time:
            conditions.append("created_at >= %s")
            params.append(start_time)
        if end_time:
            conditions.append("created_at <= %s")
            params.append(end_time)

        return await db.fetch_all(
            f"""
            SELECT
                id, worker_id, source_id, provider_id, model_id, strategy_id,
                previous_workers, baseline_workers, min_workers, max_workers,
                effective_workers, pending_count, claimed_count, running_count,
                success_count, failure_count, error_rate, matched_rule,
                avg_latency_ms, decision_reason, created_at
            FROM swe_cron_dispatch_worker_capacity
            WHERE {" AND ".join(conditions)}
            ORDER BY created_at DESC, id DESC
            LIMIT {DISPATCH_CAPACITY_EVENT_LIMIT}
            """,
            tuple(params),
        )

    async def get_dispatch_batches(
        self,
        *,
        source_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> CronDispatchBatchesResponse:
        """查询当前渠道下批调度 batch 概览。"""
        db = get_db_connection()
        where_clause, sql_params = self._build_dispatch_batch_where_clause(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            status=status,
            query=query,
        )
        stats_sql = f"""
            SELECT
                COUNT(*) AS total_batches,
                COALESCE(SUM(CASE WHEN b.status = 'running' THEN 1 ELSE 0 END), 0)
                    AS running_batches,
                COALESCE(SUM(CASE WHEN b.status = 'completed' THEN 1 ELSE 0 END), 0)
                    AS completed_batches,
                COALESCE(SUM(CASE WHEN b.status = 'failed' THEN 1 ELSE 0 END), 0)
                    AS failed_batches,
                COALESCE(SUM(b.total_count), 0) AS total_intents,
                COALESCE(SUM(b.completed_count), 0) AS completed_intents,
                COALESCE(SUM(b.failed_count), 0) AS failed_intents
            FROM swe_cron_dispatch_batches b
            LEFT JOIN swe_cron_jobs j
                ON b.parent_job_id = j.id
                AND b.source_id = j.source_id
            WHERE {where_clause}
        """
        stats_row = await db.fetch_one(stats_sql, tuple(sql_params)) or {}
        total_intents = int(stats_row.get("total_intents") or 0)
        completed_intents = int(stats_row.get("completed_intents") or 0)
        failed_intents = int(stats_row.get("failed_intents") or 0)
        stats = CronDispatchBatchStats(
            total_batches=int(stats_row.get("total_batches") or 0),
            running_batches=int(stats_row.get("running_batches") or 0),
            completed_batches=int(stats_row.get("completed_batches") or 0),
            failed_batches=int(stats_row.get("failed_batches") or 0),
            total_intents=total_intents,
            completed_intents=completed_intents,
            failed_intents=failed_intents,
            pending_intents=max(
                total_intents - completed_intents - failed_intents,
                0,
            ),
        )

        total = stats.total_batches
        offset = (page - 1) * page_size
        rows = await db.fetch_all(
            f"""
            SELECT
                b.batch_id, b.parent_job_id,
                COALESCE(j.name, '') AS parent_job_name,
                b.parent_external_job_id, b.tenant_id,
                b.source_id, b.provider_id, b.model_id, b.agent_id,
                b.scheduled_fire_at, b.callback_received_at, b.status,
                b.lock_owner, b.locked_at, b.total_count, b.completed_count,
                b.failed_count, b.error_message, b.completed_at,
                b.created_at, b.updated_at
            FROM swe_cron_dispatch_batches b
            LEFT JOIN swe_cron_jobs j
                ON b.parent_job_id = j.id
                AND b.source_id = j.source_id
            WHERE {where_clause}
            ORDER BY b.scheduled_fire_at DESC, b.created_at DESC
            LIMIT %s OFFSET %s
            """,
            tuple(sql_params) + (page_size, offset),
        )
        return CronDispatchBatchesResponse(
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
            stats=stats,
            items=[self._map_dispatch_batch(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_dispatch_batch_detail(
        self,
        *,
        source_id: str,
        batch_id: str,
        params: CronDispatchDetailQueryParams | None = None,
    ) -> Optional[CronDispatchBatchDetailResponse]:
        """查询单个批调度 batch 的 intent 和事件明细。"""
        db = get_db_connection()
        query_params = params or CronDispatchDetailQueryParams()
        batch_row = await db.fetch_one(
            """
            SELECT
                b.batch_id, b.parent_job_id,
                COALESCE(j.name, '') AS parent_job_name,
                b.parent_external_job_id, b.tenant_id,
                b.source_id, b.provider_id, b.model_id, b.agent_id,
                b.scheduled_fire_at, b.callback_received_at, b.status,
                b.lock_owner, b.locked_at, b.total_count, b.completed_count,
                b.failed_count, b.error_message, b.completed_at,
                b.created_at, b.updated_at
            FROM swe_cron_dispatch_batches b
            LEFT JOIN swe_cron_jobs j
                ON b.parent_job_id = j.id
                AND b.source_id = j.source_id
            WHERE b.source_id = %s AND b.batch_id = %s
            """,
            (source_id, batch_id),
        )
        if not batch_row:
            return None

        count_row = await db.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM swe_cron_dispatch_intents
            WHERE source_id = %s AND batch_id = %s
            """,
            (source_id, batch_id),
        )
        intent_where, intent_sql_params = (
            self._build_dispatch_intent_where_clause(
                source_id=source_id,
                batch_id=batch_id,
                params=query_params,
            )
        )
        filtered_count_row = await db.fetch_one(
            f"""
            SELECT COUNT(*) AS count
            FROM swe_cron_dispatch_intents
            WHERE {intent_where}
            """,
            tuple(intent_sql_params),
        )
        event_count_row = await db.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM swe_cron_dispatch_events
            WHERE source_id = %s AND batch_id = %s
            """,
            (source_id, batch_id),
        )
        intent_offset = (
            query_params.intent_page - 1
        ) * query_params.intent_limit
        event_offset = (query_params.event_page - 1) * query_params.event_limit
        intent_rows = await db.fetch_all(
            f"""
            SELECT
                id, batch_id, intent_role, status, source_id, provider_id,
                model_id, tenant_id, agent_id, job_id, parent_job_id,
                scheduled_fire_at, due_at, dispatch_order, viewer_heat_score,
                attempt_count, max_attempts, lock_owner, locked_at, acked_at,
                completed_at, error_message, created_at, updated_at
            FROM swe_cron_dispatch_intents
            WHERE {intent_where}
            ORDER BY
                CASE intent_role WHEN 'parent' THEN 0 ELSE 1 END,
                dispatch_order,
                id
            LIMIT %s OFFSET %s
            """,
            tuple(intent_sql_params)
            + (query_params.intent_limit, intent_offset),
        )
        event_rows = await db.fetch_all(
            """
            SELECT
                id, batch_id, intent_id, event_type, worker_id, job_id,
                tenant_id, source_id, details, created_at
            FROM swe_cron_dispatch_events
            WHERE source_id = %s AND batch_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (
                source_id,
                batch_id,
                query_params.event_limit,
                event_offset,
            ),
        )
        return CronDispatchBatchDetailResponse(
            batch=self._map_dispatch_batch(batch_row),
            intents=[self._map_dispatch_intent(row) for row in intent_rows],
            intent_total=int((count_row or {}).get("count") or 0),
            intent_filtered_total=int(
                (filtered_count_row or {}).get("count") or 0,
            ),
            intent_page=query_params.intent_page,
            intent_page_size=query_params.intent_limit,
            events=[self._map_dispatch_event(row) for row in event_rows],
            event_total=int((event_count_row or {}).get("count") or 0),
            event_page=query_params.event_page,
            event_page_size=query_params.event_limit,
        )

    async def get_dispatch_workers(
        self,
        *,
        source_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> CronDispatchWorkersResponse:
        """查询当前渠道下模型策略和 worker capacity 变动。"""
        db = get_db_connection()
        policy_rows = await self._fetch_dispatch_worker_policy_rows(
            db,
            source_id,
        )
        latest_rows = await self._fetch_current_dispatch_capacity_rows(
            db,
            source_id,
        )
        event_rows = await self._fetch_dispatch_capacity_event_rows(
            db,
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
        )
        return CronDispatchWorkersResponse(
            source_id=source_id,
            policies=[self._map_dispatch_policy(row) for row in policy_rows],
            current_capacity=[
                self._map_dispatch_capacity(row) for row in latest_rows
            ],
            capacity_events=[
                self._map_dispatch_capacity(row) for row in event_rows
            ],
        )

    async def list_jobs(
        self,
        params: CronJobQueryParams,
    ) -> PaginatedResponse[CronJobModel]:
        """List cron jobs with pagination and filters.

        Args:
            params: Query parameters

        Returns:
            Paginated response with job list
        """
        db = get_db_connection()
        where_clause, sql_params = self._build_job_where_clause(params)

        # Count total
        count_sql = (
            f"SELECT COUNT(*) as count FROM swe_cron_jobs WHERE {where_clause}"
        )
        count_result = await db.fetch_one(count_sql, tuple(sql_params))
        total = count_result.get("count", 0) if count_result else 0

        # Query with pagination
        offset = (params.page - 1) * params.page_size
        query_sql = f"""
            SELECT * FROM swe_cron_jobs
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        query_params = tuple(sql_params) + (params.page_size, offset)
        rows = await db.fetch_all(query_sql, query_params)

        items = [
            CronJobModel.model_validate(
                convert_row_times_direct(row, JOB_TIME_FIELDS),
            )
            for row in rows
        ]

        # Query execution count and today's status for each job
        if items:
            job_ids = [job.id for job in items]
            count_map = await self._fetch_job_execution_counts(db, job_ids)
            status_map = await self._fetch_job_today_status(db, job_ids)
            for job in items:
                job.execution_count = count_map.get(job.id, 0)
                job.today_status = status_map.get(job.id)

        return PaginatedResponse(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
        )

    async def query_jobs_by_broadcast_source(
        self,
        tenant_id: Optional[str] = None,
        bbk_id: Optional[str] = None,
        source_id: Optional[str] = None,
        broadcast_source_job_id: Optional[str] = None,
    ) -> List[CronJobModel]:
        """Query cron jobs by broadcast source job ID.

        Args:
            tenant_id: 租户ID筛选
            bbk_id: 二级分行号（会转换为一级分行号）
            source_id: 来源标识筛选
            broadcast_source_job_id: 分发源定时任务ID

        Returns:
            List of cron jobs matching the criteria
        """
        from ....utils.bbk import normalize_bbk_id_to_primary

        db = get_db_connection()
        conditions = ["deleted_at IS NULL"]
        sql_params: List = []

        if tenant_id:
            conditions.append("tenant_id = %s")
            sql_params.append(tenant_id)

        if bbk_id:
            # 将二级分行号转换为一级分行号
            primary_bbk_id = normalize_bbk_id_to_primary(bbk_id)
            if primary_bbk_id:
                conditions.append("bbk_id = %s")
                sql_params.append(primary_bbk_id)

        if source_id:
            conditions.append("source_id = %s")
            sql_params.append(source_id)

        if broadcast_source_job_id:
            conditions.append("broadcast_source_job_id = %s")
            sql_params.append(broadcast_source_job_id)

        query_sql = f"""
            SELECT * FROM swe_cron_jobs
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
        """
        rows = await db.fetch_all(query_sql, tuple(sql_params))

        return [
            CronJobModel.model_validate(
                convert_row_times_direct(row, JOB_TIME_FIELDS),
            )
            for row in rows
        ]

    async def get_job(
        self,
        job_id: str,
        source_id: Optional[str] = None,
    ) -> Optional[CronJobModel]:
        """Get a single job by ID.

        Args:
            job_id: Job ID
            source_id: Source ID filter

        Returns:
            CronJobModel or None if not found
        """
        db = get_db_connection()
        conditions = ["id = %s", "deleted_at IS NULL"]
        sql_params: List = [job_id]

        if source_id:
            conditions.append("source_id = %s")
            sql_params.append(source_id)

        row = await db.fetch_one(
            f"SELECT * FROM swe_cron_jobs WHERE {' AND '.join(conditions)}",
            tuple(sql_params),
        )

        if not row:
            return None

        # 直接读取，不做时区转换（数据库已是东八区时间）
        return CronJobModel.model_validate(
            convert_row_times_direct(row, JOB_TIME_FIELDS),
        )

    async def get_latest_execution_subtask_count(
        self,
        job_id: str,
        source_id: str,
    ) -> Optional[LatestExecutionSubtaskCountResponse]:
        """Return the latest execution identity and its subtask count.

        The job lookup is source-scoped. The execution lookup also uses the
        job's stored tenant ID so a caller cannot cross tenant boundaries by
        providing a job ID alone.
        """
        db = get_db_connection()
        job_row = await db.fetch_one(
            """
            SELECT id, tenant_id
            FROM swe_cron_jobs
            WHERE id = %s
              AND source_id = %s
              AND deleted_at IS NULL
            LIMIT 1
            """,
            (job_id, source_id),
        )
        if not job_row:
            return None

        tenant_id = str(job_row.get("tenant_id") or "")
        execution_row = await db.fetch_one(
            """
            SELECT
                e.id AS execution_id,
                e.trace_id,
                CASE
                    WHEN e.trace_id IS NULL OR e.trace_id = '' THEN 0
                    ELSE (
                        SELECT COUNT(*)
                        FROM swe_cron_subtasks s
                        WHERE s.trace_id = e.trace_id
                    )
                END AS subtask_count
            FROM swe_cron_executions e
            WHERE e.job_id = %s
              AND e.tenant_id = %s
            ORDER BY e.actual_time DESC, e.id DESC
            LIMIT 1
            """,
            (job_id, tenant_id),
        )
        if not execution_row:
            return LatestExecutionSubtaskCountResponse(job_id=job_id)

        trace_id = str(execution_row.get("trace_id") or "").strip() or None
        return LatestExecutionSubtaskCountResponse(
            job_id=job_id,
            execution_id=execution_row.get("execution_id"),
            trace_id=trace_id,
            subtask_count=(
                int(execution_row.get("subtask_count") or 0) if trace_id else 0
            ),
        )

    async def list_executions(
        self,
        params: ExecutionQueryParams,
    ) -> PaginatedResponse[ExecutionModel]:
        """List execution history with pagination and filters.

        Args:
            params: Query parameters

        Returns:
            Paginated response with execution list
        """
        db = get_db_connection()

        # Build WHERE clause
        conditions: List[str] = []
        sql_params: List = []

        if params.job_id:
            conditions.append("e.job_id = %s")
            sql_params.append(params.job_id)

        if params.tenant_id:
            conditions.append("e.tenant_id = %s")
            sql_params.append(params.tenant_id)

        if params.bbk_id:
            conditions.append("j.bbk_id = %s")
            sql_params.append(params.bbk_id)

        # source_id 需要通过 JOIN jobs 表筛选
        if params.source_id:
            conditions.append("j.source_id = %s")
            sql_params.append(params.source_id)

        if params.status:
            if params.status == "failed":
                # 综合失败状态：status='error' OR (status='success' AND async_status='error')
                conditions.append(
                    "(e.status = 'error' OR (e.status = 'success' AND e.async_status = 'error'))",
                )
            else:
                conditions.append("e.status = %s")
                sql_params.append(params.status)
            conditions.append("j.status != 'deleted'")

        if params.start_time:
            conditions.append("e.actual_time >= %s")
            sql_params.append(params.start_time)

        if params.end_time:
            conditions.append("e.actual_time <= %s")
            sql_params.append(params.end_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        logger.warning(
            "[cron executions debug] built filters: bbk_id=%s source_id=%s where=%s params=%s",
            params.bbk_id,
            params.source_id,
            where_clause,
            sql_params,
        )

        # Count total - 需要 JOIN jobs 表来支持 source_id 筛选
        count_sql = f"""
            SELECT COUNT(*) as count
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {where_clause}
        """
        count_result = await db.fetch_one(count_sql, tuple(sql_params))
        total = count_result.get("count", 0) if count_result else 0
        logger.warning(
            "[cron executions debug] count result: bbk_id=%s total=%s",
            params.bbk_id,
            total,
        )

        # Query with pagination - JOIN with jobs table to get tenant metadata.
        offset = (params.page - 1) * params.page_size
        query_sql = f"""
            SELECT e.*, j.tenant_name, j.bbk_id AS bbk_id
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {where_clause}
            ORDER BY e.actual_time DESC
            LIMIT %s OFFSET %s
        """
        query_params = tuple(sql_params) + (params.page_size, offset)

        rows = await db.fetch_all(query_sql, query_params)
        logger.warning(
            "[cron executions debug] rows sample: requested_bbk_id=%s returned=%s sample=%s",
            params.bbk_id,
            len(rows),
            [
                {
                    "id": row.get("id"),
                    "job_id": row.get("job_id"),
                    "job_bbk_id": row.get("bbk_id"),
                    "bbk_id": row.get("bbk_id"),
                    "tenant_id": row.get("tenant_id"),
                    "status": row.get("status"),
                }
                for row in rows[:5]
            ],
        )

        # 直接读取，不做时区转换（数据库已是东八区时间）
        items = [
            ExecutionModel.model_validate(
                convert_row_times_direct(row, EXECUTION_TIME_FIELDS),
            )
            for row in rows
        ]

        return PaginatedResponse(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
        )

    async def get_execution(
        self,
        execution_id: int,
        source_id: Optional[str] = None,
    ) -> Optional[ExecutionModel]:
        """Get a single execution by ID.

        Args:
            execution_id: Execution ID
            source_id: Source ID filter

        Returns:
            ExecutionModel or None if not found
        """
        db = get_db_connection()
        conditions = ["e.id = %s"]
        sql_params: List = [execution_id]

        if source_id:
            conditions.append("j.source_id = %s")
            sql_params.append(source_id)

        row = await db.fetch_one(
            f"""
            SELECT e.*
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {' AND '.join(conditions)}
            """,
            tuple(sql_params),
        )

        if not row:
            return None

        # 直接读取，不做时区转换（数据库已是东八区时间）
        return ExecutionModel.model_validate(
            convert_row_times_direct(row, EXECUTION_TIME_FIELDS),
        )

    async def get_executions_for_export(
        self,
        job_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        source_id: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 10000,
    ) -> List[ExecutionModel]:
        """Get executions for export without pagination.

        Args:
            job_id: Job ID filter
            tenant_id: Tenant ID filter
            source_id: Source ID filter (来源标识)
            status: Status filter
            start_time: Start time filter
            end_time: End time filter
            limit: Max records to return

        Returns:
            List of ExecutionModel
        """
        db = get_db_connection()

        # Build WHERE clause - 需要 JOIN jobs 表来支持 source_id 筛选
        conditions: List[str] = []
        sql_params: List = []

        if job_id:
            conditions.append("e.job_id = %s")
            sql_params.append(job_id)

        if tenant_id:
            conditions.append("e.tenant_id = %s")
            sql_params.append(tenant_id)

        # source_id 需要通过 JOIN jobs 表筛选
        if source_id:
            conditions.append("j.source_id = %s")
            sql_params.append(source_id)

        if status:
            conditions.append("e.status = %s")
            sql_params.append(status)

        if start_time:
            conditions.append("e.actual_time >= %s")
            sql_params.append(start_time)

        if end_time:
            conditions.append("e.actual_time <= %s")
            sql_params.append(end_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # 需要 JOIN jobs 表来支持 source_id 筛选
        query_sql = f"""
            SELECT e.*
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {where_clause}
            ORDER BY e.actual_time DESC
            LIMIT %s
        """
        query_params = tuple(sql_params) + (limit,)

        rows = await db.fetch_all(query_sql, query_params)

        # 直接读取，不做时区转换（数据库已是东八区时间）
        return [
            ExecutionModel.model_validate(
                convert_row_times_direct(row, EXECUTION_TIME_FIELDS),
            )
            for row in rows
        ]

    async def get_skill_usage_details_for_export(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        bbk_ids: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get execution/customer detail rows for the overview export.

        Clicks follow the overview aggregation rule: task + customer clicks
        are aggregated across the selected period and joined to each matching
        execution row.  A left join keeps executions without recommendations.
        """
        db = get_db_connection()
        start_time, end_time = self._parse_date_range(start_date, end_date)
        bbk_filter_sql, bbk_filter_params = self._build_bbk_filter(bbk_ids)
        source_filter_sql, source_filter_params = self._build_source_filter(
            source_id,
        )

        click_source_sql = " AND c.source_id = %s" if source_id else ""
        click_params: list[Any] = [start_time, end_time]
        if source_id:
            click_params.append(source_id)

        sql = f"""
            SELECT
                e.id AS execution_id,
                e.actual_time,
                e.status AS execution_status,
                e.async_status,
                e.is_read,
                j.name AS task_name,
                j.tenant_id,
                j.tenant_name,
                j.bbk_id,
                j.status AS task_status,
                s.custuid,
                s.cust_nm,
                COALESCE(clicks.clicked_plan, 0) AS clicked_plan,
                COALESCE(clicks.clicked_insight, 0) AS clicked_insight,
                COALESCE(clicks.clicked_phone, 0) AS clicked_phone
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            LEFT JOIN (
                SELECT trace_id, custuid, MAX(cust_nm) AS cust_nm
                FROM swe_cron_subtasks
                WHERE custuid IS NOT NULL AND custuid != ''
                GROUP BY trace_id, custuid
            ) s ON s.trace_id = e.trace_id
            LEFT JOIN (
                SELECT
                    c.cron_task_id,
                    c.customer_id,
                    MAX(CASE WHEN c.event_type = 'preview_view'
                        AND c.template_type = 'sub' THEN 1 ELSE 0 END)
                        AS clicked_plan,
                    MAX(CASE WHEN c.button_type = 'insight'
                        THEN 1 ELSE 0 END) AS clicked_insight,
                    MAX(CASE WHEN c.button_type = 'phone'
                        THEN 1 ELSE 0 END) AS clicked_phone
                FROM swe_html_preview_click_events c
                WHERE c.clicked_at >= %s AND c.clicked_at <= %s
                  AND c.customer_id IS NOT NULL
                  AND (c.event_type = 'button_click'
                    OR (c.event_type = 'preview_view'
                      AND c.template_type = 'sub'))
                  {click_source_sql}
                GROUP BY c.cron_task_id, c.customer_id
            ) clicks ON clicks.cron_task_id = e.job_id
                AND clicks.customer_id = s.custuid
            WHERE e.actual_time >= %s AND e.actual_time <= %s
              {bbk_filter_sql}
              {source_filter_sql}
            ORDER BY e.actual_time DESC, e.id DESC, s.custuid
        """
        params = click_params + [start_time, end_time]
        params.extend(bbk_filter_params)
        params.extend(source_filter_params)
        return await db.fetch_all(sql, tuple(params))

    async def get_jobs_for_export(
        self,
        tenant_id: Optional[str] = None,
        bbk_id: Optional[str] = None,
        source_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        status: Optional[str] = None,
        limit: int = 10000,
    ) -> List[CronJobModel]:
        """Get jobs for export without pagination.

        Args:
            tenant_id: Tenant ID filter
            bbk_id: BBK ID filter (分行号)
            source_id: Source ID filter (来源标识)
            enabled: Enabled filter (是否启用)
            status: Status filter
            limit: Max records to return

        Returns:
            List of CronJobModel
        """
        db = get_db_connection()

        # Build WHERE clause
        conditions = ["deleted_at IS NULL"]
        sql_params: List = []

        if tenant_id:
            conditions.append("tenant_id = %s")
            sql_params.append(tenant_id)

        if bbk_id:
            conditions.append("bbk_id = %s")
            sql_params.append(bbk_id)

        if source_id:
            conditions.append("source_id = %s")
            sql_params.append(source_id)

        if enabled is not None:
            conditions.append("enabled = %s")
            sql_params.append(enabled)

        if status:
            conditions.append("status = %s")
            sql_params.append(status)

        where_clause = " AND ".join(conditions)

        query_sql = f"""
            SELECT * FROM swe_cron_jobs
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT %s
        """
        query_params = tuple(sql_params) + (limit,)

        rows = await db.fetch_all(query_sql, query_params)

        # 直接读取，不做时区转换（数据库已是东八区时间）
        items = [
            CronJobModel.model_validate(
                convert_row_times_direct(row, JOB_TIME_FIELDS),
            )
            for row in rows
        ]
        # Query execution count for each job
        if items:
            job_ids = [job.id for job in items]
            placeholders = ",".join("%s" for _ in job_ids)
            count_sql = f"""
                SELECT job_id, COUNT(*) as count
                FROM swe_cron_executions
                WHERE job_id IN ({placeholders})
                GROUP BY job_id
            """
            count_rows = await db.fetch_all(count_sql, tuple(job_ids))
            count_map = {
                row.get("job_id"): row.get("count", 0) for row in count_rows
            }
            for job in items:
                job.execution_count = count_map.get(job.id, 0)

        return items

    async def get_filter_options(
        self,
        source_id: Optional[str] = None,
    ) -> dict:
        """获取所有筛选项的下拉选项列表。

        从任务表和执行表中聚合获取可选值，用于前端下拉框。

        Args:
            source_id: Source ID filter

        Returns:
            包含各筛选项列表的字典
        """
        db = get_db_connection()
        source_condition = ""
        source_params: Tuple = ()
        if source_id:
            source_condition = " AND source_id = %s"
            source_params = (source_id,)

        # 获取用户列表（按 tenant_id 分组去重，避免同一用户多条记录）
        users_sql = f"""
            SELECT tenant_id, MAX(tenant_name) as tenant_name
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
                AND tenant_id IS NOT NULL
                AND tenant_id != ''
                {source_condition}
            GROUP BY tenant_id
            ORDER BY tenant_name, tenant_id
        """
        users_rows = await db.fetch_all(users_sql, source_params)
        users = [
            {
                "value": row["tenant_id"],
                "label": f"{row['tenant_name'] or ''}/{row['tenant_id']}",
            }
            for row in users_rows
        ]

        # 获取分行列表（bbk_id）
        bbk_sql = f"""
            SELECT DISTINCT bbk_id
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
                AND bbk_id IS NOT NULL
                AND bbk_id != ''
                {source_condition}
            ORDER BY bbk_id
        """
        bbk_rows = await db.fetch_all(bbk_sql, source_params)
        bbk_ids = [
            {"value": row["bbk_id"], "label": row["bbk_id"]}
            for row in bbk_rows
        ]

        # 获取渠道列表（channel）
        channel_sql = f"""
            SELECT DISTINCT channel
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
                AND channel IS NOT NULL
                AND channel != ''
                {source_condition}
            ORDER BY channel
        """
        channel_rows = await db.fetch_all(channel_sql, source_params)
        channels = [
            {"value": row["channel"], "label": row["channel"]}
            for row in channel_rows
        ]

        # 获取来源/平台列表（source_id）
        source_sql = f"""
            SELECT DISTINCT source_id
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
                AND source_id IS NOT NULL
                AND source_id != ''
                {source_condition}
            ORDER BY source_id
        """
        source_rows = await db.fetch_all(source_sql, source_params)
        source_ids = [
            {"value": row["source_id"], "label": row["source_id"]}
            for row in source_rows
        ]

        # 获取任务名称列表（name）
        job_names_sql = f"""
            SELECT DISTINCT name
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
                AND name IS NOT NULL
                AND name != ''
                {source_condition}
            ORDER BY name
        """
        job_names_rows = await db.fetch_all(job_names_sql, source_params)
        job_names = [
            {"value": row["name"], "label": row["name"]}
            for row in job_names_rows
        ]

        # 获取任务ID列表（用于执行记录筛选）
        job_ids_sql = f"""
            SELECT DISTINCT id, name
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
                {source_condition}
            ORDER BY name
        """
        job_ids_rows = await db.fetch_all(job_ids_sql, source_params)
        job_ids = [
            {"value": row["id"], "label": row["name"] or row["id"]}
            for row in job_ids_rows
        ]

        return {
            "users": users,
            "bbk_ids": bbk_ids,
            "channels": channels,
            "source_ids": source_ids,
            "job_names": job_names,
            "job_ids": job_ids,
        }

    async def get_overview(
        self,
        *,
        tenant_id: Optional[str] = None,
        bbk_id: Optional[str] = None,
        source_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> CronOverviewResponse:
        """Return page-shaped aggregate data for the cron overview."""
        db = get_db_connection()
        start_time, end_time = self._resolve_today_range(start_time, end_time)
        job_where, job_params, exec_where, exec_params = (
            self._build_overview_clauses(
                tenant_id=tenant_id,
                bbk_id=bbk_id,
                source_id=source_id,
                start_time=start_time,
                end_time=end_time,
            )
        )
        job_summary = await self._fetch_overview_job_summary(
            db,
            job_where,
            job_params,
        )
        prev_job_summary = await self._fetch_previous_job_summary(
            db,
            tenant_id=tenant_id,
            bbk_id=bbk_id,
            source_id=source_id,
            start_time=start_time,
        )
        exec_summary = await self._fetch_overview_execution_summary(
            db,
            exec_where,
            exec_params,
        )
        prev_exec_summary = await self._fetch_previous_execution_summary(
            db,
            tenant_id=tenant_id,
            bbk_id=bbk_id,
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
        )

        return CronOverviewResponse(
            start_time=start_time,
            end_time=end_time,
            metrics=self._build_overview_metrics(
                job_summary,
                prev_job_summary,
                exec_summary,
                prev_exec_summary,
            ),
            task_status=self._build_task_status_distribution(job_summary),
            execution_result=await self._fetch_execution_result_distribution(
                db,
                exec_where,
                exec_params,
            ),
            read_status=await self._fetch_read_status_distribution(
                db,
                exec_where,
                exec_params,
            ),
            failure_reasons=await self._fetch_failure_reason_distribution(
                db,
                exec_where,
                exec_params,
            ),
            branch_tasks=await self._fetch_branch_task_distribution(
                db,
                job_where,
                job_params,
            ),
            branch_execution=await self._fetch_branch_execution_distribution(
                db,
                exec_where,
                exec_params,
            ),
            branch_read=await self._fetch_branch_read_distribution(
                db,
                exec_where,
                exec_params,
            ),
        )

    def _build_overview_clauses(
        self,
        *,
        tenant_id: Optional[str],
        bbk_id: Optional[str],
        source_id: Optional[str],
        start_time: datetime,
        end_time: datetime,
    ) -> Tuple[str, List, str, List]:
        job_conditions, job_params = self._build_overview_job_conditions(
            tenant_id=tenant_id,
            bbk_id=bbk_id,
            source_id=source_id,
        )
        exec_conditions, exec_params = (
            self._build_overview_execution_conditions(
                tenant_id=tenant_id,
                bbk_id=bbk_id,
                source_id=source_id,
                start_time=start_time,
                end_time=end_time,
            )
        )
        return (
            " AND ".join(job_conditions),
            job_params,
            " AND ".join(exec_conditions),
            exec_params,
        )

    async def _fetch_overview_job_summary(
        self,
        db: Any,
        job_where: str,
        job_params: List,
    ) -> Dict[str, Any]:
        row = await db.fetch_one(
            f"""
            SELECT
                COUNT(*) AS total_tasks,
                SUM(CASE WHEN job_origin = 'subscription' THEN 1 ELSE 0 END)
                    AS subscription_tasks,
                SUM(CASE WHEN job_origin != 'subscription' THEN 1 ELSE 0 END)
                    AS manual_tasks,
                SUM(
                    CASE
                        WHEN enabled = 1 AND status = 'active'
                        THEN 1 ELSE 0
                    END
                ) AS active_tasks,
                SUM(
                    CASE
                        WHEN status = 'paused'
                            AND pause_reason ='auto_unread_threshold'
                        THEN 1 ELSE 0
                    END
                ) AS auto_paused_tasks,
                SUM(
                    CASE
                        WHEN status = 'paused'
                            AND pause_reason ='manual'
                        THEN 1 ELSE 0
                    END
                ) AS paused_tasks
            FROM swe_cron_jobs j
            WHERE {job_where}
            """,
            tuple(job_params),
        )
        return row or {}

    async def _fetch_previous_job_summary(
        self,
        db: Any,
        *,
        tenant_id: Optional[str],
        bbk_id: Optional[str],
        source_id: Optional[str],
        start_time: datetime,
    ) -> Dict[str, Any]:
        conditions = [
            "j.created_at < %s",
            "(j.deleted_at IS NULL OR j.deleted_at >= %s)",
            "j.status != 'deleted'",
        ]
        sql_params: List = [start_time, start_time]

        if tenant_id:
            conditions.append("j.tenant_id = %s")
            sql_params.append(tenant_id)

        if bbk_id:
            conditions.append("j.bbk_id = %s")
            sql_params.append(bbk_id)

        if source_id:
            conditions.append("j.source_id = %s")
            sql_params.append(source_id)

        row = await db.fetch_one(
            f"""
            SELECT COUNT(*) AS total_tasks
            FROM swe_cron_jobs j
            WHERE {' AND '.join(conditions)}
            """,
            tuple(sql_params),
        )
        return row or {}

    async def _fetch_overview_execution_summary(
        self,
        db: Any,
        exec_where: str,
        exec_params: List,
    ) -> Dict[str, Any]:
        """获取执行统计概览。

        成功：status='success' AND async_status='success'
        失败：status='error' OR (status='success' AND async_status='error')
        """
        row = await db.fetch_one(
            f"""
            SELECT
                COUNT(*) AS execution_count,
                SUM(CASE WHEN e.status = 'success' AND e.async_status = 'success'
                    THEN 1 ELSE 0 END) AS success_count,
                SUM(
                    CASE
                        WHEN e.status = 'error'
                         OR (e.status = 'success' AND e.async_status = 'error')
                        THEN 1 ELSE 0
                    END
                ) AS failure_count,
                COALESCE(
                    AVG(CASE WHEN e.status = 'success' AND e.async_status = 'success'
                        THEN e.duration_ms END),
                    0
                ) AS avg_duration_ms
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {exec_where}
            """,
            tuple(exec_params),
        )
        return row or {}

    async def _fetch_previous_execution_summary(
        self,
        db: Any,
        *,
        tenant_id: Optional[str],
        bbk_id: Optional[str],
        source_id: Optional[str],
        start_time: datetime,
        end_time: datetime,
    ) -> Dict[str, Any]:
        prev_start, prev_end = self._resolve_previous_period(
            start_time,
            end_time,
        )
        prev_conditions, prev_params = (
            self._build_overview_execution_conditions(
                tenant_id=tenant_id,
                bbk_id=bbk_id,
                source_id=source_id,
                start_time=prev_start,
                end_time=prev_end,
            )
        )
        return await self._fetch_overview_execution_summary(
            db,
            " AND ".join(prev_conditions),
            prev_params,
        )

    def _resolve_previous_period(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> Tuple[datetime, datetime]:
        period_days = (end_time - start_time).days
        if period_days == 0:
            return start_time - timedelta(days=1), end_time - timedelta(days=1)
        return (
            start_time - timedelta(days=period_days),
            start_time - timedelta(seconds=1),
        )

    def _build_overview_metrics(
        self,
        job_summary: Dict[str, Any],
        prev_job_summary: Dict[str, Any],
        exec_summary: Dict[str, Any],
        prev_exec_summary: Dict[str, Any],
    ) -> List[CronOverviewMetricItem]:
        total_tasks = int(job_summary.get("total_tasks") or 0)
        prev_total_tasks = int(prev_job_summary.get("total_tasks") or 0)
        execution_count = int(exec_summary.get("execution_count") or 0)
        success_rate = self._calculate_success_rate(exec_summary)
        avg_duration_ms = float(exec_summary.get("avg_duration_ms") or 0)
        prev_success_rate = self._calculate_success_rate(prev_exec_summary)

        total_compare, total_trend = self._calc_total_delta(
            total_tasks,
            prev_total_tasks,
        )
        runs_compare, runs_trend = self._calc_compare(
            execution_count,
            int(prev_exec_summary.get("execution_count") or 0),
        )
        success_rate_compare, success_rate_trend = self._calc_compare(
            success_rate,
            prev_success_rate,
        )
        avg_cost_compare, avg_cost_trend = self._calc_compare(
            avg_duration_ms,
            float(prev_exec_summary.get("avg_duration_ms") or 0),
        )

        return [
            CronOverviewMetricItem(
                key="total",
                value=total_tasks,
                compare=total_compare,
                trend=total_trend,
            ),
            CronOverviewMetricItem(
                key="subscribed",
                value=int(job_summary.get("subscription_tasks") or 0),
            ),
            CronOverviewMetricItem(
                key="created",
                value=int(job_summary.get("manual_tasks") or 0),
            ),
            CronOverviewMetricItem(
                key="runs",
                value=execution_count,
                compare=runs_compare,
                trend=runs_trend,
            ),
            CronOverviewMetricItem(
                key="success_rate",
                value=success_rate,
                compare=success_rate_compare,
                trend=success_rate_trend,
            ),
            CronOverviewMetricItem(
                key="avg_cost",
                value=avg_duration_ms,
                compare=avg_cost_compare,
                trend=avg_cost_trend,
            ),
        ]

    def _calculate_success_rate(self, summary: Dict[str, Any]) -> float:
        success_count = int(summary.get("success_count") or 0)
        failure_count = int(summary.get("failure_count") or 0)
        total_count = success_count + failure_count
        return success_count / total_count * 100 if total_count else 0.0

    def _calc_total_delta(
        self,
        current: int,
        prev: int,
    ) -> Tuple[str, Optional[str]]:
        delta = current - prev
        if delta > 0:
            return f"{delta}", "up"
        if delta < 0:
            return f"{abs(delta)}", "down"
        return "", None

    def _calc_compare(
        self,
        current: float,
        prev: float,
    ) -> Tuple[str, Optional[str]]:
        if prev == 0:
            return "", None
        change = ((current - prev) / prev) * 100
        if change > 0:
            return f"+{change:.1f}%", "up"
        if change < 0:
            return f"{change:.1f}%", "down"
        return "0.0%", "up"

    def _build_task_status_distribution(
        self,
        job_summary: Dict[str, Any],
    ) -> List[CronOverviewDistributionItem]:
        return self._build_distribution(
            [
                ("生效中", int(job_summary.get("active_tasks") or 0)),
                (
                    "未读自动暂停",
                    int(job_summary.get("auto_paused_tasks") or 0),
                ),
                ("手动暂停", int(job_summary.get("paused_tasks") or 0)),
            ],
            {
                "生效中": "#2361EA",
                "未读自动暂停": "#F97212",
                "手动暂停": "#783AF1",
            },
        )

    async def _fetch_execution_result_distribution(
        self,
        db: Any,
        exec_where: str,
        exec_params: List,
    ) -> List[CronOverviewDistributionItem]:
        """获取执行结果分布。

        成功：status='success' AND async_status='success'
        失败：status='error' OR (status='success' AND async_status='error')
        运行中：status='success' AND (async_status IS NULL OR async_status='')
        """
        rows = await db.fetch_all(
            f"""
            SELECT
                CASE
                    WHEN e.status = 'success' AND e.async_status = 'success'
                        THEN '成功'
                    WHEN e.status = 'error'
                         OR (e.status = 'success' AND e.async_status = 'error')
                        THEN '失败'
                    WHEN e.status = 'cancelled' THEN '已取消/跳过'
                    WHEN e.status = 'success' AND (e.async_status IS NULL OR e.async_status = '')
                        THEN '运行中'
                    ELSE e.status
                END AS name,
                COUNT(*) AS value
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {exec_where}
            GROUP BY name
            ORDER BY value DESC
            """,
            tuple(exec_params),
        )
        return self._build_distribution(
            self._distribution_pairs(rows),
            {
                "成功": "#13A146",
                "失败": "#f33f3d",
                "已取消/跳过": "#9b9db4",
            },
        )

    async def _fetch_read_status_distribution(
        self,
        db: Any,
        exec_where: str,
        exec_params: List,
    ) -> List[CronOverviewDistributionItem]:
        """获取已读/未读分布（仅统计真正成功的执行）。"""
        rows = await db.fetch_all(
            f"""
            SELECT
                CASE WHEN e.is_read = 1 THEN '已读' ELSE '未读' END AS name,
                COUNT(*) AS value
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {exec_where} AND e.status = 'success' AND e.async_status = 'success'
            GROUP BY e.is_read
            ORDER BY value DESC
            """,
            tuple(exec_params),
        )
        return self._build_distribution(
            self._distribution_pairs(rows),
            {"已读": "#2361EA", "未读": "#F97212"},
        )

    async def _fetch_failure_reason_distribution(
        self,
        db: Any,
        exec_where: str,
        exec_params: List,
    ) -> List[CronOverviewDistributionItem]:
        rows = await db.fetch_all(
            f"""
            SELECT
                CASE
                    WHEN e.error_message LIKE '%%channel not found%%'
                        THEN '渠道不存在'
                    WHEN e.error_message LIKE '%%cron auth user_info is expired%%'
                        THEN 'token过期'
                    WHEN e.error_message LIKE '%%Illegal Argument%%'
                        THEN '密文长度错误'
                    WHEN LOWER(e.error_message) LIKE '%%validation error for agentrequest%%'
                        THEN '智能体请求校验失败'
                    WHEN e.error_message LIKE '%%Agent execution did not complete%%'
                        THEN '模型错误'
                    ELSE '其他'
                END AS name,
                COUNT(*) AS value
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {exec_where}
              AND e.status = 'error'
              AND j.status != 'deleted'
            GROUP BY 1
            ORDER BY value DESC, name ASC
            LIMIT 10
            """,
            tuple(exec_params),
        )
        return self._build_distribution(
            self._distribution_pairs(rows),
            {
                "渠道不存在": "#ef4444",
                "token过期": "#f97316",
                "密文长度错误": "#eab308",
                "智能体请求校验失败": "#8b5cf6",
                "其他": "#64748b",
            },
        )

    async def _fetch_branch_task_distribution(
        self,
        db: Any,
        job_where: str,
        job_params: List,
    ) -> List[CronOverviewDistributionItem]:
        rows = await db.fetch_all(
            f"""
            SELECT COALESCE(NULLIF(j.bbk_id, ''), 'unknown') AS name,
                   COUNT(*) AS value
            FROM swe_cron_jobs j
            WHERE {job_where}
                AND j.status IN ('active', 'paused')
            GROUP BY j.bbk_id
            ORDER BY value DESC, name ASC
            """,
            tuple(job_params),
        )
        return self._build_distribution(
            [
                (
                    self._format_branch_name(row.get("name") or "unknown"),
                    int(row.get("value") or 0),
                )
                for row in rows
            ],
            {"unknown": "#94a3b8"},
        )

    async def _fetch_branch_execution_distribution(
        self,
        db: Any,
        exec_where: str,
        exec_params: List,
    ) -> List[CronOverviewBranchExecutionItem]:
        """获取分行执行分布。

        成功：status='success' AND async_status='success'
        失败：status='error' OR (status='success' AND async_status='error')
        """
        rows = await db.fetch_all(
            f"""
            SELECT
                COALESCE(NULLIF(j.bbk_id, ''), 'unknown') AS name,
                SUM(CASE WHEN e.status = 'success' AND e.async_status = 'success'
                    THEN 1 ELSE 0 END) AS success,
                SUM(
                    CASE
                        WHEN e.status = 'error'
                         OR (e.status = 'success' AND e.async_status = 'error')
                        THEN 1 ELSE 0
                    END
                ) AS failed,
                SUM(CASE WHEN e.status = 'skipped' THEN 1 ELSE 0 END)
                    AS skipped
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {exec_where}
            GROUP BY j.bbk_id
            ORDER BY (success + failed + skipped) DESC, name ASC
            """,
            tuple(exec_params),
        )
        return [
            CronOverviewBranchExecutionItem(
                name=self._format_branch_name(row.get("name") or "unknown"),
                success=int(row.get("success") or 0),
                failed=int(row.get("failed") or 0),
                skipped=int(row.get("skipped") or 0),
            )
            for row in rows
        ]

    async def _fetch_branch_read_distribution(
        self,
        db: Any,
        exec_where: str,
        exec_params: List,
    ) -> List[CronOverviewBranchReadItem]:
        """获取分行已读/未读分布（仅统计真正成功的执行）。"""
        rows = await db.fetch_all(
            f"""
            SELECT
                COALESCE(NULLIF(j.bbk_id, ''), 'unknown') AS name,
                SUM(CASE WHEN e.is_read = 1 THEN 1 ELSE 0 END) AS read_count,
                SUM(CASE WHEN e.is_read = 0 THEN 1 ELSE 0 END) AS unread_count
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {exec_where} AND e.status = 'success' AND e.async_status = 'success'
            GROUP BY j.bbk_id
            ORDER BY (read_count + unread_count) DESC, name ASC
            """,
            tuple(exec_params),
        )
        return [
            CronOverviewBranchReadItem(
                name=self._format_branch_name(row.get("name") or "unknown"),
                read=int(row.get("read_count") or 0),
                unread=int(row.get("unread_count") or 0),
            )
            for row in rows
        ]

    def _format_branch_name(self, bbk_id: Any) -> str:
        normalized_bbk_id = str(bbk_id or "").strip()
        if normalized_bbk_id == "unknown":
            return "unknown"
        return get_bbk_name_by_id(normalized_bbk_id) or normalized_bbk_id

    def _distribution_pairs(
        self,
        rows: List[Dict[str, Any]],
    ) -> List[Tuple[str, int]]:
        return [
            (row.get("name") or "unknown", int(row.get("value") or 0))
            for row in rows
        ]

    async def get_subscription_overview(
        self,
        *,
        keyword: Optional[str] = None,
        tenant_id: Optional[str] = None,
        bbk_id: Optional[str] = None,
        source_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 10,
    ) -> PaginatedResponse[SubscriptionOverviewItem]:
        """按订阅任务分组统计当天概览数据。"""
        db = get_db_connection()
        start_time, end_time = self._resolve_today_range(start_time, end_time)
        conditions, sql_params = self._build_subscription_conditions(
            keyword=keyword,
            tenant_id=tenant_id,
            bbk_id=bbk_id,
            source_id=source_id,
        )
        where_clause = " AND ".join(conditions)
        group_key = (
            "COALESCE(NULLIF(j.subscription_key, ''), CONCAT('job:', j.id))"
        )

        count_sql = f"""
            SELECT COUNT(*) as count
            FROM (
                SELECT {group_key} AS subscription_group
                FROM swe_cron_jobs j
                WHERE {where_clause}
                GROUP BY subscription_group
            ) grouped
        """
        count_result = await db.fetch_one(count_sql, tuple(sql_params))
        total = count_result.get("count", 0) if count_result else 0

        offset = (page - 1) * page_size
        latest_execution_sql = """
            SELECT e.*
            FROM swe_cron_executions e
            INNER JOIN (
                SELECT job_id, MAX(actual_time) AS latest_actual_time
                FROM swe_cron_executions
                WHERE actual_time >= %s AND actual_time <= %s
                GROUP BY job_id
            ) latest
                ON latest.job_id = e.job_id
                AND latest.latest_actual_time = e.actual_time
        """
        query_sql = f"""
            SELECT
                {group_key} AS subscription_key,
                MAX(j.name) AS task_name,
                COUNT(*) AS total_task_count,
                COUNT(DISTINCT NULLIF(j.creator_user_id, '')) AS subscriber_count,
                SUM(CASE WHEN le.status = 'running' THEN 1 ELSE 0 END)
                    AS running_task_count,
                SUM(
                    CASE
                        WHEN le.job_id IS NULL
                            AND j.enabled = 1
                            AND j.status = 'active'
                        THEN 1 ELSE 0
                    END
                ) AS pending_task_count,
                SUM(CASE WHEN le.status = 'success' AND le.async_status = 'success'
                    THEN 1 ELSE 0 END) AS executed_task_count,
                SUM(
                    CASE
                        WHEN le.status IN ('error', 'timeout', 'cancelled')
                         OR (le.status = 'success' AND le.async_status = 'error')
                        THEN 1 ELSE 0
                    END
                ) AS failed_task_count,
                COALESCE(
                    AVG(CASE WHEN le.status = 'success' AND le.async_status = 'success'
                        THEN le.duration_ms END),
                    0
                ) AS avg_duration_ms,
                SUM(CASE WHEN le.status = 'success' AND le.async_status = 'success'
                    THEN 1 ELSE 0 END) AS success_count,
                SUM(
                    CASE
                        WHEN le.status IN ('success', 'error', 'timeout', 'cancelled')
                        THEN 1 ELSE 0
                    END
                ) AS completed_count
            FROM swe_cron_jobs j
            LEFT JOIN ({latest_execution_sql}) le ON le.job_id = j.id
            WHERE {where_clause}
            GROUP BY subscription_key
            ORDER BY total_task_count DESC, task_name ASC
            LIMIT %s OFFSET %s
        """
        rows = await db.fetch_all(
            query_sql,
            (start_time, end_time, *sql_params, page_size, offset),
        )

        items = []
        for row in rows:
            completed_count = int(row.get("completed_count") or 0)
            success_count = int(row.get("success_count") or 0)
            success_rate = (
                success_count / completed_count if completed_count else 0.0
            )
            items.append(
                SubscriptionOverviewItem(
                    subscription_key=row.get("subscription_key") or "",
                    task_name=row.get("task_name") or "",
                    subscriber_count=int(row.get("subscriber_count") or 0),
                    total_task_count=int(row.get("total_task_count") or 0),
                    running_task_count=int(row.get("running_task_count") or 0),
                    pending_task_count=int(row.get("pending_task_count") or 0),
                    executed_task_count=int(
                        row.get("executed_task_count") or 0,
                    ),
                    failed_task_count=int(row.get("failed_task_count") or 0),
                    avg_duration_ms=float(row.get("avg_duration_ms") or 0),
                    success_rate=success_rate,
                ),
            )

        return PaginatedResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_subscription_details(
        self,
        subscription_key: str,
        *,
        tenant_id: Optional[str] = None,
        bbk_id: Optional[str] = None,
        source_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 10,
    ) -> PaginatedResponse[SubscriptionDetailItem]:
        """查询订阅任务详情弹窗数据。"""
        db = get_db_connection()
        start_time, end_time = self._resolve_today_range(start_time, end_time)
        conditions, sql_params = self._build_subscription_conditions(
            tenant_id=tenant_id,
            bbk_id=bbk_id,
            source_id=source_id,
        )
        conditions.append("j.subscription_key = %s")
        sql_params.append(subscription_key)
        where_clause = " AND ".join(conditions)

        count_sql = f"""
            SELECT COUNT(*) as count
            FROM swe_cron_jobs j
            WHERE {where_clause}
        """
        count_result = await db.fetch_one(count_sql, tuple(sql_params))
        total = count_result.get("count", 0) if count_result else 0

        offset = (page - 1) * page_size
        latest_execution_sql = """
            SELECT e.*
            FROM swe_cron_executions e
            INNER JOIN (
                SELECT job_id, MAX(actual_time) AS latest_actual_time
                FROM swe_cron_executions
                WHERE actual_time >= %s AND actual_time <= %s
                GROUP BY job_id
            ) latest
                ON latest.job_id = e.job_id
                AND latest.latest_actual_time = e.actual_time
        """
        query_sql = f"""
            SELECT
                j.id AS job_id,
                j.creator_user_id AS subscriber_id,
                j.tenant_name AS subscriber_name,
                j.bbk_id,
                j.enabled,
                CASE WHEN le.job_id IS NULL THEN 'pending' ELSE 'executed' END
                    AS execution_status,
                le.actual_time AS execution_time
            FROM swe_cron_jobs j
            LEFT JOIN ({latest_execution_sql}) le ON le.job_id = j.id
            WHERE {where_clause}
            ORDER BY j.created_at DESC
            LIMIT %s OFFSET %s
        """
        rows = await db.fetch_all(
            query_sql,
            (start_time, end_time, *sql_params, page_size, offset),
        )
        items = [
            SubscriptionDetailItem(
                job_id=row.get("job_id") or "",
                subscriber_id=row.get("subscriber_id") or "",
                subscriber_name=row.get("subscriber_name") or "",
                bbk_id=row.get("bbk_id") or "",
                enabled=bool(row.get("enabled")),
                execution_status=row.get("execution_status") or "pending",
                execution_time=row.get("execution_time"),
            )
            for row in rows
        ]

        return PaginatedResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )

    def _resolve_today_range(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> Tuple[datetime, datetime]:
        """未传时间范围时默认使用北京时间当天。"""
        if start_time and end_time:
            return start_time, end_time
        today_start = datetime.now(BEIJING_TZ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        today_end = today_start.replace(
            hour=23,
            minute=59,
            second=59,
            microsecond=999999,
        )
        return today_start - timedelta(hours=8), today_end - timedelta(hours=8)

    def _build_distribution(
        self,
        pairs: List[Tuple[str, int]],
        color_map: Optional[Dict[str, str]] = None,
    ) -> List[CronOverviewDistributionItem]:
        """Build chart items with percentages."""
        total = sum(value for _, value in pairs)
        items = []
        for name, value in pairs:
            percent = (value / total * 100) if total else 0.0
            color = color_map.get(name) if color_map else None
            items.append(
                CronOverviewDistributionItem(
                    name=name,
                    value=value,
                    percent=round(percent, 2),
                    color=color,
                ),
            )
        return items

    def _build_overview_job_conditions(
        self,
        *,
        tenant_id: Optional[str] = None,
        bbk_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> Tuple[List[str], List]:
        """Build filters for swe_cron_jobs overview queries."""
        conditions = ["j.deleted_at IS NULL", "j.status != 'deleted'"]
        sql_params: List = []
        if tenant_id:
            conditions.append("j.tenant_id = %s")
            sql_params.append(tenant_id)
        if bbk_id:
            conditions.append("j.bbk_id = %s")
            sql_params.append(bbk_id)
        if source_id:
            conditions.append("j.source_id = %s")
            sql_params.append(source_id)
        return conditions, sql_params

    def _build_overview_execution_conditions(
        self,
        *,
        tenant_id: Optional[str] = None,
        bbk_id: Optional[str] = None,
        source_id: Optional[str] = None,
        start_time: datetime,
        end_time: datetime,
    ) -> Tuple[List[str], List]:
        """Build filters for swe_cron_executions overview queries."""
        conditions = [
            "e.actual_time >= %s",
            "e.actual_time <= %s",
            "j.status != 'deleted'",
        ]
        sql_params: List = [start_time, end_time]
        if tenant_id:
            conditions.append("e.tenant_id = %s")
            sql_params.append(tenant_id)
        if bbk_id:
            conditions.append("j.bbk_id = %s")
            sql_params.append(bbk_id)
        if source_id:
            conditions.append("j.source_id = %s")
            sql_params.append(source_id)
        return conditions, sql_params

    def _build_subscription_conditions(
        self,
        *,
        keyword: Optional[str] = None,
        tenant_id: Optional[str] = None,
        bbk_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> Tuple[List[str], List]:
        """构建订阅任务查询条件。"""
        conditions = ["j.deleted_at IS NULL", "j.job_origin = 'subscription'"]
        sql_params: List = []
        if keyword:
            conditions.append(
                "j.name LIKE %s",
            )
            keyword_like = f"%{keyword}%"
            sql_params.append(keyword_like)
        if tenant_id:
            conditions.append("j.tenant_id = %s")
            sql_params.append(tenant_id)
        if bbk_id:
            conditions.append("j.bbk_id = %s")
            sql_params.append(bbk_id)
        if source_id:
            conditions.append("j.source_id = %s")
            sql_params.append(source_id)
        return conditions, sql_params

    async def mark_job_as_read(
        self,
        job_id: str,
        source_id: Optional[str] = None,
    ) -> int:
        """标记任务的最新一次未读执行为已读。

        只将指定任务的最后一次成功执行的未读记录标记为已读。

        Args:
            job_id: 任务ID
            source_id: Source ID filter

        Returns:
            更新的记录数量（0 或 1）
        """
        db = get_db_connection()
        now = datetime.now(BEIJING_TZ).replace(tzinfo=None)

        update_sql = """
            UPDATE swe_cron_executions e
            JOIN (
                SELECT id FROM swe_cron_executions
                WHERE job_id = %s
                AND status = 'success'
                ORDER BY actual_time DESC
                LIMIT 1
            ) AS latest ON e.id = latest.id
            SET e.is_read = TRUE, e.read_at = %s
        """
        logger.info(f"[mark_executions_read] 开始标记已读, job_id={job_id}")
        logger.debug(f"[mark_executions_read] SQL: {update_sql}")

        result = await db.execute(update_sql, (job_id, now))
        rowcount = getattr(result, "rowcount", 0) if result else 0
        logger.info(
            f"[mark_executions_read] UPDATE执行完成, job_id={job_id}, updated={rowcount}",
        )
        return rowcount

    async def get_unread_count(
        self,
        tenant_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> UnreadCountResponse:
        """获取未读任务数量统计。

        按任务分组统计未读的成功执行记录数量，
        用于前端展示未读提醒。

        Args:
            tenant_id: 租户ID筛选（可选）
            source_id: Source ID filter

        Returns:
            包含各任务未读数量的字典
        """
        db = get_db_connection()

        conditions = [
            "e.status = 'success'",
            "e.async_status = 'success'",
            "e.is_read = FALSE",
        ]
        sql_params: List = []

        if tenant_id:
            conditions.append("e.tenant_id = %s")
            sql_params.append(tenant_id)

        if source_id:
            conditions.append("j.source_id = %s")
            sql_params.append(source_id)

        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT e.job_id, e.job_name, COUNT(*) as unread_count
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE {where_clause}
            GROUP BY e.job_id, e.job_name
            ORDER BY unread_count DESC
        """
        rows = await db.fetch_all(
            sql,
            tuple(sql_params) if sql_params else None,
        )

        return UnreadCountResponse(
            items=[
                {
                    "job_id": row["job_id"],
                    "job_name": row["job_name"] or row["job_id"],
                    "unread_count": row["unread_count"],
                }
                for row in rows
            ],
            total_unread=sum(row["unread_count"] for row in rows),
        )

    @staticmethod
    def _row_int(row: Optional[Dict[str, Any]], key: str) -> int:
        """Read an integer aggregate from a DB row."""
        if not row:
            return 0
        return int(row.get(key) or 0)

    @staticmethod
    def _percent(numerator: int, denominator: int) -> float:
        """Calculate a rounded percentage with zero protection."""
        if denominator <= 0:
            return 0.0
        return round(numerator / denominator * 100, 2)

    async def _fetch_overview_task_count(
        self,
        db: Any,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
    ) -> int:
        task_count_sql = f"""
            SELECT COUNT(*) AS count
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
              AND status != 'deleted'
              {bbk_filter_sql.replace('j.bbk_id', 'bbk_id')}
              {source_filter_sql.replace('j.source_id', 'source_id')}
        """
        params = bbk_filter_params + source_filter_params
        row = await db.fetch_one(
            task_count_sql,
            tuple(params) if params else None,
        )
        return self._row_int(row, "count")

    async def _fetch_overview_branch_tenant_counts(
        self,
        db: Any,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
    ) -> Tuple[int, int]:
        branch_tenant_sql = f"""
            SELECT
                COUNT(DISTINCT bbk_id) AS branch_count,
                COUNT(DISTINCT tenant_id) AS tenant_count
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
              AND status != 'deleted'
              {bbk_filter_sql.replace('j.bbk_id', 'bbk_id')}
              {source_filter_sql.replace('j.source_id', 'source_id')}
        """
        params = bbk_filter_params + source_filter_params
        row = await db.fetch_one(
            branch_tenant_sql,
            tuple(params) if params else None,
        )
        return self._row_int(row, "branch_count"), self._row_int(
            row,
            "tenant_count",
        )

    async def _fetch_overview_execution_counts(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
    ) -> Dict[str, int]:
        # 综合状态判断：
        # 成功：status='success' AND async_status='success'
        # 运行中：status='success' AND (async_status IS NULL OR async_status='')
        # 失败：status='error' OR (status='success' AND async_status='error')
        exec_sql = f"""
            SELECT
                COUNT(*) AS total_executions,
                COUNT(DISTINCT e.job_id) AS executed_job_count,
                SUM(
                    CASE WHEN e.status = 'success' AND e.async_status = 'success'
                    THEN 1 ELSE 0 END
                ) AS success_count,
                SUM(
                    CASE WHEN e.status = 'success'
                         AND (e.async_status IS NULL OR e.async_status = '')
                    THEN 1 ELSE 0 END
                ) AS running_count,
                SUM(
                    CASE WHEN e.status = 'error'
                         OR (e.status = 'success' AND e.async_status = 'error')
                    THEN 1 ELSE 0 END
                ) AS error_count
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.actual_time >= %s AND e.actual_time <= %s
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              {bbk_filter_sql}
              {source_filter_sql}
        """
        params = (
            [start_time, end_time] + bbk_filter_params + source_filter_params
        )
        row = await db.fetch_one(exec_sql, tuple(params))
        return {
            "total_executions": self._row_int(row, "total_executions"),
            "executed_job_count": self._row_int(row, "executed_job_count"),
            "success_count": self._row_int(row, "success_count"),
            "running_count": self._row_int(row, "running_count"),
            "error_count": self._row_int(row, "error_count"),
        }

    async def _fetch_overview_read_tasks(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
    ) -> int:
        read_tasks_sql = f"""
            SELECT COUNT(*) AS read_tasks
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.actual_time >= %s AND e.actual_time <= %s
              AND e.is_read = 1
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              {bbk_filter_sql}
              {source_filter_sql}
        """
        params = (
            [start_time, end_time] + bbk_filter_params + source_filter_params
        )
        row = await db.fetch_one(read_tasks_sql, tuple(params))
        return self._row_int(row, "read_tasks")

    async def _fetch_overview_new_cron_tasks(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
    ) -> int:
        """查询时间范围内新增的定时任务数（按 created_at 时间判断）."""
        new_tasks_sql = f"""
            SELECT COUNT(*) AS new_cron_tasks
            FROM swe_cron_jobs
            WHERE created_at >= %s AND created_at <= %s
              AND deleted_at IS NULL
              AND status != 'deleted'
              {bbk_filter_sql.replace('j.bbk_id', 'bbk_id')}
              {source_filter_sql.replace('j.source_id', 'source_id')}
        """
        params = (
            [start_time, end_time] + bbk_filter_params + source_filter_params
        )
        row = await db.fetch_one(new_tasks_sql, tuple(params))
        return self._row_int(row, "new_cron_tasks")

    async def get_overview_stats(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        bbk_ids: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> CronOverviewStatsResponse:
        """获取定时任务概览统计。

        Args:
            start_date: 开始日期 (YYYY-MM-DD格式字符串)
            end_date: 结束日期 (YYYY-MM-DD格式字符串)
            bbk_ids: 分行号筛选（逗号分隔）
            source_id: 来源标识

        Returns:
            概览统计数据
        """
        db = get_db_connection()

        # 解析时间范围
        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        # 构建 bbk 过滤条件
        bbk_filter_sql, bbk_filter_params = self._build_bbk_filter(bbk_ids)

        # 构建 source 过滤条件
        source_filter_sql, source_filter_params = self._build_source_filter(
            source_id,
        )

        total_tasks = await self._fetch_overview_task_count(
            db,
            bbk_filter_sql,
            bbk_filter_params,
            source_filter_sql,
            source_filter_params,
        )
        branch_count, tenant_count = (
            await self._fetch_overview_branch_tenant_counts(
                db,
                bbk_filter_sql,
                bbk_filter_params,
                source_filter_sql,
                source_filter_params,
            )
        )
        execution_counts = await self._fetch_overview_execution_counts(
            db,
            start_time,
            end_time,
            bbk_filter_sql,
            bbk_filter_params,
            source_filter_sql,
            source_filter_params,
        )
        read_tasks = await self._fetch_overview_read_tasks(
            db,
            start_time,
            end_time,
            bbk_filter_sql,
            bbk_filter_params,
            source_filter_sql,
            source_filter_params,
        )
        report_behavior_counts = (
            await self._fetch_overview_report_behavior_counts(
                db,
                start_time,
                end_time,
                bbk_filter_sql,
                bbk_filter_params,
                source_filter_sql,
                source_filter_params,
            )
        )
        new_cron_tasks = await self._fetch_overview_new_cron_tasks(
            db,
            start_time,
            end_time,
            bbk_filter_sql,
            bbk_filter_params,
            source_filter_sql,
            source_filter_params,
        )

        total_executions = execution_counts["total_executions"]
        success_count = execution_counts["success_count"]
        running_count = execution_counts["running_count"]
        error_count = execution_counts["error_count"]
        report_count = report_behavior_counts["report_count"]
        insight_count = report_behavior_counts["insight_count"]
        phone_count = report_behavior_counts["phone_count"]
        success_rate = self._percent(success_count, total_executions)
        read_rate = self._percent(read_tasks, total_executions)
        report_rate = self._percent(report_count, total_executions)
        error_rate = self._percent(error_count, total_executions)

        return CronOverviewStatsResponse(
            start_date=start_str,
            end_date=end_str,
            total_tasks=total_tasks,
            new_cron_tasks=new_cron_tasks,
            total_executions=total_executions,
            branch_count=branch_count,
            tenant_count=tenant_count,
            success_rate=success_rate,
            success_count=success_count,
            running_count=running_count,
            read_tasks=read_tasks,
            read_rate=read_rate,
            report_rate=report_rate,
            report_count=report_count,
            insight_count=insight_count,
            phone_count=phone_count,
            error_count=error_count,
            error_rate=error_rate,
        )

    async def _fetch_overview_report_behavior_counts(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
    ) -> dict[str, int]:
        """查询概览中的查看方案/去洞察/去电访任务数。

        三项指标都直接来自 swe_html_preview_click_events，
        按 button_type 统计去重任务数。
        """
        behavior_params = [
            start_time,
            end_time,
            *bbk_filter_params,
            *source_filter_params,
        ]
        click_sql = f"""
            SELECT
                COUNT(DISTINCT CASE
                    WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN c.cron_task_id
                END) AS report_count,
                COUNT(DISTINCT CASE
                    WHEN c.button_type = 'insight' THEN c.cron_task_id
                END) AS insight_count,
                COUNT(DISTINCT CASE
                    WHEN c.button_type = 'phone' THEN c.cron_task_id
                END) AS phone_count
            FROM swe_html_preview_click_events c
            JOIN swe_cron_jobs j
              ON c.cron_task_id = j.id
            WHERE c.clicked_at >= %s AND c.clicked_at <= %s
              AND (c.event_type = 'button_click' OR (c.event_type = 'preview_view' AND c.template_type = 'sub'))
              AND c.cron_task_id IS NOT NULL
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              {bbk_filter_sql.replace('j.bbk_id', 'c.bbk_id')}
              {source_filter_sql.replace('j.source_id', 'c.source_id')}
        """
        click_row = await db.fetch_one(click_sql, tuple(behavior_params))
        return {
            "report_count": self._row_int(click_row, "report_count"),
            "insight_count": self._row_int(click_row, "insight_count"),
            "phone_count": self._row_int(click_row, "phone_count"),
        }

    async def _fetch_branch_behavior_ids(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
    ) -> List[str]:
        """获取分行ID列表（任务视角，不使用统计技能过滤）。"""
        branch_list_sql = f"""
            SELECT DISTINCT j.bbk_id
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.actual_time >= %s AND e.actual_time <= %s
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              AND j.bbk_id IS NOT NULL
              AND j.bbk_id != ''
              {bbk_filter_sql}
              {source_filter_sql}
        """
        params = (
            [start_time, end_time] + bbk_filter_params + source_filter_params
        )
        rows = await db.fetch_all(branch_list_sql, tuple(params))
        return [row.get("bbk_id") for row in rows if row.get("bbk_id")]

    async def _fetch_branch_skill_behavior_ids(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
        sync_date: Optional[str] = None,
    ) -> List[str]:
        """获取使用统计技能的分行ID列表（技能视角）。"""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
        )
        skill_exists = self._statistics_skill_exists("j")

        branch_list_sql = f"""
            SELECT DISTINCT j.bbk_id
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.actual_time >= %s AND e.actual_time <= %s
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              AND j.bbk_id IS NOT NULL
              AND j.bbk_id != ''
              AND {skill_exists}
              {jkh_filter_sql}
              {bbk_filter_sql}
              {source_filter_sql}
        """
        params = (
            [start_time, end_time]
            + jkh_filter_params
            + bbk_filter_params
            + source_filter_params
        )
        rows = await db.fetch_all(branch_list_sql, tuple(params))
        return [row.get("bbk_id") for row in rows if row.get("bbk_id")]

    async def _fetch_branch_total_tasks(
        self,
        db: Any,
        bbk_id: str,
        source_id: Optional[str],
    ) -> int:
        """统计分行任务数量（任务视角，不使用统计技能过滤）。"""
        source_where = " AND source_id = %s" if source_id else ""
        task_count_sql = f"""
            SELECT COUNT(*) AS count
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
              AND status != 'deleted'
              AND bbk_id = %s
              {source_where}
        """
        params = (bbk_id, source_id) if source_id else (bbk_id,)
        row = await db.fetch_one(task_count_sql, params)
        return self._row_int(row, "count")

    async def _fetch_branch_skill_total_tasks(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> int:
        """统计分行使用统计技能的任务数量（技能视角）。"""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")

        task_count_sql = f"""
            SELECT COUNT(DISTINCT j.id) AS count
            FROM swe_cron_jobs j
            JOIN swe_cron_executions e ON e.job_id = j.id
            WHERE j.deleted_at IS NULL
              AND j.status != 'deleted'
              AND j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        row = await db.fetch_one(task_count_sql, tuple(params))
        return self._row_int(row, "count")

    async def _fetch_branch_job_ids(
        self,
        db: Any,
        bbk_id: str,
        source_id: Optional[str],
    ) -> list[str]:
        """获取指定分行的所有 job_id（任务视角，不使用统计技能过滤）。"""
        source_where = " AND source_id = %s" if source_id else ""
        job_ids_sql = f"""
            SELECT id
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
              AND status != 'deleted'
              AND bbk_id = %s
              {source_where}
        """
        params = (bbk_id, source_id) if source_id else (bbk_id,)
        rows = await db.fetch_all(job_ids_sql, params)
        return [row["id"] for row in rows]

    async def _fetch_branch_skill_job_ids(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> list[str]:
        """获取指定分行使用统计技能的所有 job_id（技能视角）。"""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")

        job_ids_sql = f"""
            SELECT DISTINCT j.id
            FROM swe_cron_jobs j
            JOIN swe_cron_executions e ON e.job_id = j.id
            WHERE j.deleted_at IS NULL
              AND j.status != 'deleted'
              AND j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(job_ids_sql, tuple(params))
        return [row["id"] for row in rows]

    async def _fetch_branch_execution_stats(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        job_ids: list[str],
        sync_date: Optional[str] = None,
        bbk_id: Optional[str] = None,
    ) -> dict:
        """直接从 swe_cron_executions 统计执行指标，不 JOIN swe_cron_jobs。

        成功：status='success' AND async_status='success'
        失败：status='error' OR (status='success' AND async_status='error')
        已读任务数：按已读执行次数统计（与概览口径一致）
        """
        if not job_ids:
            return {
                "total_executions": 0,
                "success_count": 0,
                "read_tasks": 0,
                "error_count": 0,
            }
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "swe_cron_executions.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        placeholders = ", ".join(["%s"] * len(job_ids))
        stats_sql = f"""
            SELECT
                COUNT(*) AS total_executions,
                SUM(CASE WHEN status = 'success' AND async_status = 'success'
                    THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN is_read = 1 THEN 1 ELSE 0 END) AS read_tasks,
                SUM(CASE WHEN status = 'error'
                         OR (status = 'success' AND async_status = 'error')
                    THEN 1 ELSE 0 END) AS error_count
            FROM swe_cron_executions
            WHERE actual_time >= %s AND actual_time <= %s
              AND job_id IN ({placeholders})
              {jkh_filter_sql}
        """
        params = [start_time, end_time] + job_ids + jkh_filter_params
        row = await db.fetch_one(stats_sql, tuple(params))
        return {
            "total_executions": self._row_int(row, "total_executions"),
            "success_count": self._row_int(row, "success_count"),
            "read_tasks": self._row_int(row, "read_tasks"),
            "error_count": self._row_int(row, "error_count"),
        }

    async def _fetch_branch_manager_count(
        self,
        db: Any,
        bbk_id: str,
        source_id: Optional[str],
    ) -> int:
        source_where = " AND source_id = %s" if source_id else ""
        manager_sql = f"""
            SELECT COUNT(DISTINCT tenant_id) AS manager_count
            FROM swe_cron_jobs
            WHERE deleted_at IS NULL
              AND status != 'deleted'
              AND bbk_id = %s
              {source_where}
        """
        params = (bbk_id, source_id) if source_id else (bbk_id,)
        row = await db.fetch_one(manager_sql, params)
        return self._row_int(row, "manager_count")

    async def _fetch_branch_click_counts(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
    ) -> dict:
        """查询各分行点击统计（查看方案/去洞察/去电访）。

        从 swe_html_preview_click_events 按 bbk_id + button_type 聚合，
        同时返回去重任务数 (COUNT(DISTINCT cron_task_id)) 和点击数 (COUNT(*))。
        """
        source_where = " AND source_id = %s" if source_id else ""
        click_sql = f"""
            SELECT
                bbk_id,
                CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END AS button_type,
                COUNT(DISTINCT cron_task_id) AS task_count,
                COUNT(*) AS total_clicks
            FROM swe_html_preview_click_events
            WHERE clicked_at >= %s AND clicked_at <= %s
              AND (event_type = 'button_click' AND button_type IN ('insight', 'phone')
              OR (event_type = 'preview_view' AND template_type = 'sub'))
              AND cron_task_id IS NOT NULL
              AND bbk_id IS NOT NULL
              AND bbk_id != ''
              {source_where}
            GROUP BY bbk_id, CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END
        """
        params: list = [start_time, end_time]
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(click_sql, tuple(params))

        result: dict[str, dict[str, dict[str, int]]] = {}
        for row in rows:
            bbk = row["bbk_id"]
            btn = row["button_type"] or "other"
            if bbk not in result:
                result[bbk] = {}
            result[bbk][btn] = {
                "task_count": row["task_count"] or 0,
                "total_clicks": row["total_clicks"] or 0,
            }
        return result

    async def _fetch_branch_skill_count(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> int:
        """统计分行统计技能去重数量。

        从任务绑定技能中按市场表统计开关过滤后去重计数。
        """
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""

        sql = f"""
            SELECT COUNT(DISTINCT s.skill_id) AS skill_count
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            {self._skill_binding_join("j", "s")}
            WHERE j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              {jkh_filter_sql}
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        row = await db.fetch_one(sql, tuple(params))
        return self._row_int(row, "skill_count")

    async def _fetch_branch_manager_click_counts(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
    ) -> dict[str, int]:
        """统计分行客户经理点击行为（任务视角，不使用统计技能过滤）。

        按button_type去重user_id。
        """
        source_where = " AND source_id = %s" if source_id else ""
        sql = f"""
            SELECT CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END AS button_type,
            COUNT(DISTINCT user_id) AS manager_count
            FROM swe_html_preview_click_events
            WHERE bbk_id = %s
              AND clicked_at >= %s AND clicked_at <= %s
              AND (event_type = 'preview_view' AND template_type = 'sub'
              OR (event_type = 'button_click' AND button_type IN ('insight', 'phone')))
              {source_where}
            GROUP BY CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END
        """
        params: list = [bbk_id, start_time, end_time]
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(sql, tuple(params))
        result: dict[str, int] = {}
        for row in rows:
            btn = row["button_type"] or "other"
            result[btn] = self._row_int(row, "manager_count")
        return result

    async def _fetch_branch_skill_manager_click_counts(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> dict[str, int]:
        """统计分行使用统计技能的客户经理点击行为（技能视角）。

        与任务视角口径一致：按点击时间范围筛选，通过 cron_task_id 关联获取技能信息。
        """
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "c.user_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND c.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")

        sql = f"""
            SELECT CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN 'plan' ELSE c.button_type END AS button_type,
            COUNT(DISTINCT c.user_id) AS manager_count
            FROM swe_html_preview_click_events c
            JOIN swe_cron_jobs j ON c.cron_task_id = j.id
            WHERE c.bbk_id = %s
              AND c.clicked_at >= %s AND c.clicked_at <= %s
              AND (c.event_type = 'preview_view' AND c.template_type = 'sub'
              OR (c.event_type = 'button_click' AND c.button_type IN ('insight', 'phone')))
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
            GROUP BY CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN 'plan' ELSE c.button_type END
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(sql, tuple(params))
        result: dict[str, int] = {}
        for row in rows:
            btn = row["button_type"] or "other"
            result[btn] = self._row_int(row, "manager_count")
        return result

    async def _fetch_branch_customer_click_counts(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
    ) -> dict[str, int]:
        """统计分行客户点击行为（任务视角，不使用统计技能过滤）。

        按button_type去重customer_id。
        """
        source_where = " AND source_id = %s" if source_id else ""
        sql = f"""
            SELECT CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END AS button_type,
            COUNT(DISTINCT customer_id) AS customer_count
            FROM swe_html_preview_click_events
            WHERE bbk_id = %s
              AND clicked_at >= %s AND clicked_at <= %s
              AND (event_type = 'preview_view' AND template_type = 'sub'
              OR (event_type = 'button_click' AND button_type IN ('insight', 'phone')))
              AND customer_id IS NOT NULL
              {source_where}
            GROUP BY CASE WHEN event_type = 'preview_view' AND template_type = 'sub' THEN 'plan' ELSE button_type END
        """
        params: list = [bbk_id, start_time, end_time]
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(sql, tuple(params))
        result: dict[str, int] = {}
        for row in rows:
            btn = row["button_type"] or "other"
            result[btn] = self._row_int(row, "customer_count")
        return result

    async def _fetch_branch_skill_customer_click_counts(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> dict[str, int]:
        """统计分行使用统计技能的客户点击行为（技能视角）。

        与任务视角口径一致：按点击时间范围筛选，通过 cron_task_id 关联获取技能信息。
        """
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "c.user_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND c.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")

        sql = f"""
            SELECT CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN 'plan' ELSE c.button_type END AS button_type,
            COUNT(DISTINCT c.customer_id) AS customer_count
            FROM swe_html_preview_click_events c
            JOIN swe_cron_jobs j ON c.cron_task_id = j.id
            WHERE c.bbk_id = %s
              AND c.clicked_at >= %s AND c.clicked_at <= %s
              AND (c.event_type = 'preview_view' AND c.template_type = 'sub'
              OR (c.event_type = 'button_click' AND c.button_type IN ('insight', 'phone')))
              AND c.customer_id IS NOT NULL
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
            GROUP BY CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN 'plan' ELSE c.button_type END
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(sql, tuple(params))
        result: dict[str, int] = {}
        for row in rows:
            btn = row["button_type"] or "other"
            result[btn] = self._row_int(row, "customer_count")
        return result

    async def _fetch_branch_contact_stats(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: List[Any],
        source_filter_sql: str,
        source_filter_params: List[Any],
        sync_date: Optional[str] = None,
    ) -> dict[str, dict[str, Any]]:
        """统计技能视角下各分行的接触客户数和客户接触率."""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "a.user_id",
            sync_date,
        )
        skill_exists = self._statistics_skill_exists("j")
        contact_sql = f"""
            SELECT
                a.bbk_id,
                COUNT(DISTINCT a.customer_id) AS clicked_customers,
                COUNT(
                    DISTINCT CASE
                        WHEN b.cust_uid IS NOT NULL THEN b.cust_uid
                    END
                ) AS contacted_customers,
                CASE
                    WHEN COUNT(DISTINCT a.customer_id) = 0 THEN 0
                    ELSE COUNT(
                        DISTINCT CASE
                            WHEN b.cust_uid IS NOT NULL THEN b.cust_uid
                        END
                    ) * 1.0 / COUNT(DISTINCT a.customer_id)
                END AS contact_rate
            FROM swe_html_preview_click_events a
            LEFT JOIN swe_skill_contact_detail b ON a.id = b.click_id
            WHERE a.customer_id IS NOT NULL
              AND (a.event_type = 'preview_view' AND a.template_type = 'sub'
              OR (a.event_type = 'button_click' AND a.button_type IN ('insight', 'phone')))
              AND (
                a.clicked_at >= %s AND a.clicked_at <= %s
                OR b.clicked_at >= %s AND b.clicked_at <= %s
              )
              AND a.bbk_id IS NOT NULL
              AND a.bbk_id != ''
              {jkh_filter_sql}
              {bbk_filter_sql.replace('j.bbk_id', 'a.bbk_id')}
              {source_filter_sql.replace('j.source_id', 'a.source_id')}
              AND EXISTS (
                SELECT 1
                FROM swe_cron_jobs j
                WHERE j.id = a.cron_task_id
                  AND j.deleted_at IS NULL
                  AND j.status != 'deleted'
                  AND {skill_exists}
              )
            GROUP BY a.bbk_id
        """
        params = (
            [start_time, end_time, start_time, end_time]
            + jkh_filter_params
            + bbk_filter_params
            + source_filter_params
        )
        logger.info(
            f"[_fetch_branch_contact_stats] SQL: {contact_sql}, params: {params}",
        )
        rows = await db.fetch_all(contact_sql, tuple(params))
        logger.info(f"[_fetch_branch_contact_stats] Result rows: {len(rows)}")
        return {
            row["bbk_id"]: {
                "clicked_customers": self._row_int(row, "clicked_customers"),
                "contacted_customers": self._row_int(
                    row,
                    "contacted_customers",
                ),
                "contact_rate": _to_float(row.get("contact_rate")),
            }
            for row in rows
        }

    async def _fetch_branch_recommended_customers(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
    ) -> int:
        """统计分行推荐的客户数（任务视角，不使用统计技能过滤）。

        从subtasks表custuid去重。
        """
        source_where = " AND j.source_id = %s" if source_id else ""
        sql = f"""
            SELECT COUNT(DISTINCT s.custuid) AS customer_count
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            JOIN swe_cron_subtasks s ON e.trace_id = s.trace_id
            WHERE j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              AND s.custuid IS NOT NULL
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        if source_id:
            params.append(source_id)
        row = await db.fetch_one(sql, tuple(params))
        return self._row_int(row, "customer_count")

    async def _fetch_branch_skill_recommended_customers(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> int:
        """统计分行使用统计技能的推荐客户数（技能视角）。"""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")

        sql = f"""
            SELECT COUNT(DISTINCT s.custuid) AS customer_count
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            JOIN swe_cron_subtasks s ON e.trace_id = s.trace_id
            WHERE j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              AND s.custuid IS NOT NULL
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        row = await db.fetch_one(sql, tuple(params))
        return self._row_int(row, "customer_count")

    async def _fetch_branch_involved_managers(
        self,
        db: Any,
        bbk_id: str,
        source_id: Optional[str],
    ) -> int:
        """统计分行涉及客户经理数（任务视角，不使用统计技能过滤）。

        生效中任务的tenant_id去重。
        """
        source_where = " AND source_id = %s" if source_id else ""
        sql = f"""
            SELECT COUNT(DISTINCT tenant_id) AS manager_count
            FROM swe_cron_jobs
            WHERE bbk_id = %s
              AND status = 'active'
              AND enabled = 1
              AND deleted_at IS NULL
              {source_where}
        """
        params = (bbk_id, source_id) if source_id else (bbk_id,)
        row = await db.fetch_one(sql, params)
        return self._row_int(row, "manager_count")

    async def _fetch_branch_skill_involved_managers(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> int:
        """统计分行使用统计技能的涉及客户经理数（技能视角）。

        统计时间范围内执行过统计技能任务的客户经理数。
        """
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")

        sql = f"""
            SELECT COUNT(DISTINCT j.tenant_id) AS manager_count
            FROM swe_cron_jobs j
            JOIN swe_cron_executions e ON e.job_id = j.id
            WHERE j.bbk_id = %s
              AND j.deleted_at IS NULL
              AND e.actual_time >= %s AND e.actual_time <= %s
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        row = await db.fetch_one(sql, tuple(params))
        return self._row_int(row, "manager_count")

    async def _fetch_branch_result_view_managers(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
    ) -> int:
        """统计分行查看结果的客户经理数（任务视角，不使用统计技能过滤）。

        is_read=1的tenant_id去重。
        """
        source_where = " AND j.source_id = %s" if source_id else ""
        sql = f"""
            SELECT COUNT(DISTINCT j.tenant_id) AS manager_count
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              AND e.is_read = 1
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        if source_id:
            params.append(source_id)
        row = await db.fetch_one(sql, tuple(params))
        return self._row_int(row, "manager_count")

    async def _fetch_branch_skill_result_view_managers(
        self,
        db: Any,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> int:
        """统计分行使用统计技能的查看结果客户经理数（技能视角）。"""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")

        sql = f"""
            SELECT COUNT(DISTINCT j.tenant_id) AS manager_count
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              AND e.is_read = 1
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        row = await db.fetch_one(sql, tuple(params))
        return self._row_int(row, "manager_count")

    def _build_branch_task_ranking_item(
        self,
        bbk_id: str,
        manager_count: int,
        total_tasks: int,
        success_count: int,
        total_executions: int,
        read_tasks: int,
        plan_count: int = 0,
        insight_count: int = 0,
        phone_count: int = 0,
        plan_clicks: int = 0,
        insight_clicks: int = 0,
        phone_clicks: int = 0,
        error_count: int = 0,
    ) -> CronBranchTaskRankingItem:
        return CronBranchTaskRankingItem(
            bbk_id=bbk_id,
            bbk_name=get_bbk_name_by_id(bbk_id) or bbk_id,
            manager_count=manager_count,
            total_tasks=total_tasks,
            success_count=success_count,
            success_rate=self._percent(success_count, total_executions),
            read_tasks=read_tasks,
            plan_count=plan_count,
            insight_count=insight_count,
            phone_count=phone_count,
            plan_clicks=plan_clicks,
            insight_clicks=insight_clicks,
            phone_clicks=phone_clicks,
            error_count=error_count,
        )

    def _build_branch_ranking_item(
        self,
        bbk_id: str,
        skill_count: int,
        total_tasks: int,
        success_count: int,
        read_tasks: int,
        involved_managers: int,
        result_view_managers: int,
        plan_managers: int,
        insight_managers: int,
        phone_managers: int,
        recommended_customers: int,
        viewed_customers: int,
        contacted_customers: int,
        contact_rate: float,
        insight_customers: int,
        phone_customers: int,
    ) -> CronBranchRankingItem:
        return CronBranchRankingItem(
            bbk_id=bbk_id,
            bbk_name=get_bbk_name_by_id(bbk_id) or bbk_id,
            skill_count=skill_count,
            total_tasks=total_tasks,
            success_count=success_count,
            read_tasks=read_tasks,
            involved_managers=involved_managers,
            result_view_managers=result_view_managers,
            plan_managers=plan_managers,
            insight_managers=insight_managers,
            phone_managers=phone_managers,
            recommended_customers=recommended_customers,
            viewed_customers=viewed_customers,
            contacted_customers=contacted_customers,
            contact_rate=contact_rate,
            insight_customers=insight_customers,
            phone_customers=phone_customers,
        )

    @staticmethod
    async def _resolve_jkh_sync_date(
        db: DatabaseConnection,
        end_time: datetime,
    ) -> Optional[str]:
        """按截止日、当月月末、最早/最新日期的优先级选择名单快照。"""
        end_date = end_time.strftime("%Y-%m-%d")
        month_end = end_time.replace(
            day=calendar.monthrange(end_time.year, end_time.month)[1],
        ).strftime("%Y-%m-%d")
        row = await db.fetch_one(
            """
            SELECT
                MAX(CASE WHEN sync_date = %s THEN sync_date END) AS exact_date,
                MAX(CASE WHEN sync_date = %s THEN sync_date END) AS month_end_date,
                MIN(sync_date) AS earliest_date,
                MAX(sync_date) AS latest_date
            FROM jkh_user_inf
            """,
            (end_date, month_end),
        )
        if not row or row.get("latest_date") is None:
            return None
        if row.get("exact_date") is not None:
            return row["exact_date"]
        if row.get("month_end_date") is not None:
            return row["month_end_date"]
        if row["latest_date"] > end_date:
            return row["earliest_date"]
        return row["latest_date"]

    @staticmethod
    def _build_jkh_filter(
        user_column: str,
        sync_date: Optional[str],
        bbk_id: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        """为内部固定列名构建名单过滤；未指定快照时保留原查询口径。"""
        if sync_date is None:
            return "", []
        branch_filter = (
            " AND jkh.first_bbk_id = %s" if bbk_id is not None else ""
        )
        params = [sync_date]
        if bbk_id is not None:
            params.append(bbk_id)
        return (
            f"""AND EXISTS (
                SELECT 1 FROM jkh_user_inf jkh
                WHERE jkh.user_id = {user_column} AND jkh.sync_date = %s
                  {branch_filter}
            )""",
            params,
        )

    async def get_branch_behavior(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        bbk_ids: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> CronBranchRankingResponse:
        """获取分行综合排行。

        Args:
            start_date: 开始日期 (YYYY-MM-DD格式字符串)
            end_date: 结束日期 (YYYY-MM-DD格式字符串)
            bbk_ids: 分行号筛选（逗号分隔）
            source_id: 来源标识

        Returns:
            分行综合排行数据
        """
        db = get_db_connection()

        # 解析时间范围
        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        sync_date = await self._resolve_jkh_sync_date(db, end_time)
        if sync_date is None:
            return CronBranchRankingResponse(
                start_date=start_str,
                end_date=end_str,
                items=[],
            )

        # 构建 bbk 过滤条件
        bbk_filter_sql, bbk_filter_params = self._build_bbk_filter(bbk_ids)

        # 构建 source 过滤条件
        source_filter_sql, source_filter_params = self._build_source_filter(
            source_id,
        )

        branch_ids = await self._fetch_branch_skill_behavior_ids(
            db,
            start_time,
            end_time,
            bbk_filter_sql,
            bbk_filter_params,
            source_filter_sql,
            source_filter_params,
            sync_date=sync_date,
        )
        contact_stats = await self._fetch_branch_contact_stats(
            db,
            start_time,
            end_time,
            bbk_filter_sql,
            bbk_filter_params,
            source_filter_sql,
            source_filter_params,
            sync_date=sync_date,
        )

        items = []
        for bbk_id in branch_ids:
            total_tasks = await self._fetch_branch_skill_total_tasks(
                db,
                bbk_id,
                start_time,
                end_time,
                source_id,
                sync_date=sync_date,
            )
            job_ids = await self._fetch_branch_skill_job_ids(
                db,
                bbk_id,
                start_time,
                end_time,
                source_id,
                sync_date=sync_date,
            )
            stats = await self._fetch_branch_execution_stats(
                db,
                start_time,
                end_time,
                job_ids,
                sync_date=sync_date,
                bbk_id=bbk_id,
            )
            # 新增指标查询
            skill_count = await self._fetch_branch_skill_count(
                db,
                bbk_id,
                start_time,
                end_time,
                source_id,
                sync_date=sync_date,
            )
            involved_managers = (
                await self._fetch_branch_skill_involved_managers(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                )
            )
            result_view_managers = (
                await self._fetch_branch_skill_result_view_managers(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                )
            )
            manager_click_counts = (
                await self._fetch_branch_skill_manager_click_counts(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                )
            )
            customer_click_counts = (
                await self._fetch_branch_skill_customer_click_counts(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                )
            )
            recommended_customers = (
                await self._fetch_branch_skill_recommended_customers(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                )
            )
            branch_contact_stats = contact_stats.get(bbk_id, {})
            items.append(
                self._build_branch_ranking_item(
                    bbk_id,
                    skill_count,
                    total_tasks,
                    stats["success_count"],
                    stats["read_tasks"],
                    involved_managers,
                    result_view_managers,
                    manager_click_counts.get("plan", 0),
                    manager_click_counts.get("insight", 0),
                    manager_click_counts.get("phone", 0),
                    recommended_customers,
                    customer_click_counts.get("plan", 0),
                    branch_contact_stats.get("contacted_customers", 0),
                    branch_contact_stats.get("contact_rate", 0.0),
                    customer_click_counts.get("insight", 0),
                    customer_click_counts.get("phone", 0),
                ),
            )

        items.sort(key=lambda item: item.success_count, reverse=True)

        return CronBranchRankingResponse(
            start_date=start_str,
            end_date=end_str,
            items=items,
        )

    async def get_branch_task_behavior(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        bbk_ids: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> CronBranchTaskRankingResponse:
        """获取分行任务视角综合排行。

        Args:
            start_date: 开始日期 (YYYY-MM-DD格式字符串)
            end_date: 结束日期 (YYYY-MM-DD格式字符串)
            bbk_ids: 分行号筛选（逗号分隔）
            source_id: 来源标识

        Returns:
            分行任务视角排行数据
        """
        db = get_db_connection()

        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        bbk_filter_sql, bbk_filter_params = self._build_bbk_filter(bbk_ids)
        source_filter_sql, source_filter_params = self._build_source_filter(
            source_id,
        )

        branch_ids = await self._fetch_branch_behavior_ids(
            db,
            start_time,
            end_time,
            bbk_filter_sql,
            bbk_filter_params,
            source_filter_sql,
            source_filter_params,
        )

        # 一次查询获取所有分行的点击统计
        click_counts = await self._fetch_branch_click_counts(
            db,
            start_time,
            end_time,
            source_id,
        )

        items = []
        for bbk_id in branch_ids:
            total_tasks = await self._fetch_branch_total_tasks(
                db,
                bbk_id,
                source_id,
            )
            manager_count = await self._fetch_branch_manager_count(
                db,
                bbk_id,
                source_id,
            )
            job_ids = await self._fetch_branch_job_ids(
                db,
                bbk_id,
                source_id,
            )
            stats = await self._fetch_branch_execution_stats(
                db,
                start_time,
                end_time,
                job_ids,
            )
            branch_clicks = click_counts.get(bbk_id, {})
            plan_clicks_data = branch_clicks.get("plan", {})
            insight_clicks_data = branch_clicks.get("insight", {})
            phone_clicks_data = branch_clicks.get("phone", {})
            items.append(
                self._build_branch_task_ranking_item(
                    bbk_id,
                    manager_count,
                    total_tasks,
                    stats["success_count"],
                    stats["total_executions"],
                    stats["read_tasks"],
                    plan_count=plan_clicks_data.get("task_count", 0),
                    insight_count=insight_clicks_data.get("task_count", 0),
                    phone_count=phone_clicks_data.get("task_count", 0),
                    plan_clicks=plan_clicks_data.get("total_clicks", 0),
                    insight_clicks=insight_clicks_data.get("total_clicks", 0),
                    phone_clicks=phone_clicks_data.get("total_clicks", 0),
                    error_count=stats["error_count"],
                ),
            )

        items.sort(key=lambda item: item.success_count, reverse=True)

        return CronBranchTaskRankingResponse(
            start_date=start_str,
            end_date=end_str,
            items=items,
        )

    async def get_branch_error(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        bbk_ids: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> CronBranchErrorResponse:
        """获取分行层异常执行数据。

        Args:
            start_date: 开始日期 (YYYY-MM-DD格式字符串)
            end_date: 结束日期 (YYYY-MM-DD格式字符串)
            bbk_ids: 分行号筛选（逗号分隔）
            source_id: 来源标识

        Returns:
            分行异常执行数据
        """
        db = get_db_connection()

        # 解析时间范围
        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        # 构建 bbk 过滤条件
        bbk_filter_sql, bbk_filter_params = self._build_bbk_filter(bbk_ids)

        # 构建 source 过滤条件
        source_filter_sql, source_filter_params = self._build_source_filter(
            source_id,
        )

        # 1. 受影响的分行数量和客户经理数量（综合判断失败状态）
        # 失败：status='error' OR (status='success' AND async_status='error')
        affected_sql = f"""
            SELECT
                COUNT(DISTINCT j.bbk_id) AS affected_branch_count,
                COUNT(DISTINCT j.creator_user_id) AS affected_manager_count
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.actual_time >= %s AND e.actual_time <= %s
              AND (
                e.status = 'error'
                OR (e.status = 'success' AND e.async_status = 'error')
              )
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              {bbk_filter_sql}
              {source_filter_sql}
        """
        affected_params = (
            [start_time, end_time] + bbk_filter_params + source_filter_params
        )
        affected_row = await db.fetch_one(affected_sql, tuple(affected_params))
        affected_branch_count = int(
            (
                affected_row.get("affected_branch_count", 0)
                if affected_row
                else 0
            ),
        )
        affected_manager_count = int(
            (
                affected_row.get("affected_manager_count", 0)
                if affected_row
                else 0
            ),
        )

        # 2. 报错原因分布（复用现有逻辑）
        error_reasons = await self._fetch_cron_error_reasons(
            db,
            start_time,
            end_time,
            bbk_filter_sql,
            bbk_filter_params,
            source_filter_sql,
            source_filter_params,
        )

        # 3. 分行异常排行（按报错次数由高到低，综合判断失败状态）
        branch_rank_sql = f"""
            SELECT
                j.bbk_id,
                COUNT(*) AS error_count,
                SUM(
                    CASE WHEN e.status = 'error'
                         OR (e.status = 'success' AND e.async_status = 'error')
                    THEN 1 ELSE 0 END
                ) AS branch_error_count,
                COUNT(
                    DISTINCT CASE
                        WHEN e.status = 'error'
                        OR (e.status = 'success' AND e.async_status = 'error')
                        THEN j.creator_user_id
                        ELSE NULL
                    END
                ) AS affected_managers
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.actual_time >= %s AND e.actual_time <= %s
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              {bbk_filter_sql}
              {source_filter_sql}
            GROUP BY j.bbk_id
            HAVING branch_error_count > 0
            ORDER BY branch_error_count DESC
        """
        branch_rank_params = (
            [start_time, end_time] + bbk_filter_params + source_filter_params
        )
        branch_rank_rows = await db.fetch_all(
            branch_rank_sql,
            tuple(branch_rank_params),
        )

        branch_error_rank = []
        for row in branch_rank_rows:
            bbk_id = row.get("bbk_id") or ""
            if not bbk_id:
                continue
            bbk_name = get_bbk_name_by_id(bbk_id) or bbk_id
            total_executions = int(row.get("error_count", 0))
            error_count = int(row.get("branch_error_count", 0))
            affected_managers = int(row.get("affected_managers", 0))
            error_rate = (
                (error_count / total_executions * 100)
                if total_executions > 0
                else 0.0
            )

            branch_error_rank.append(
                CronBranchErrorRankItem(
                    bbk_id=bbk_id,
                    bbk_name=bbk_name,
                    total_executions=total_executions,
                    error_count=error_count,
                    error_rate=round(error_rate, 2),
                    affected_managers=affected_managers,
                ),
            )

        return CronBranchErrorResponse(
            start_date=start_str,
            end_date=end_str,
            affected_branch_count=affected_branch_count,
            affected_manager_count=affected_manager_count,
            error_reasons=error_reasons,
            branch_error_rank=branch_error_rank,
        )

    async def _fetch_cron_error_reasons(
        self,
        db: Any,
        start_time: datetime,
        end_time: datetime,
        bbk_filter_sql: str,
        bbk_filter_params: List,
        source_filter_sql: str = "",
        source_filter_params: List = None,
    ) -> List[CronErrorReasonItem]:
        """获取报错原因分布（综合判断失败状态）."""
        if source_filter_params is None:
            source_filter_params = []

        # 综合判断失败状态，区分失败原因来源：
        # - status='error': 使用 error_message 分类
        # - status='success' AND async_status='error': 失败原因为"子任务执行失败"
        rows = await db.fetch_all(
            f"""
            SELECT
                CASE
                    WHEN e.status = 'success' AND e.async_status = 'error'
                        THEN '子任务执行失败'
                    WHEN e.error_message LIKE '%%channel not found%%'
                        THEN '渠道不存在'
                    WHEN e.error_message LIKE '%%cron auth user_info is expired%%'
                        THEN 'token过期'
                    WHEN e.error_message LIKE '%%Illegal Argument%%'
                        THEN '密文长度错误'
                    WHEN LOWER(e.error_message) LIKE '%%validation error for agentrequest%%'
                        THEN '智能体请求校验失败'
                    WHEN e.error_message LIKE '%%Agent execution did not complete%%'
                        THEN '模型错误'
                    ELSE '其他'
                END AS reason,
                COUNT(*) AS count
            FROM swe_cron_executions e
            LEFT JOIN swe_cron_jobs j ON e.job_id = j.id
            WHERE e.actual_time >= %s AND e.actual_time <= %s
              AND (
                e.status = 'error'
                OR (e.status = 'success' AND e.async_status = 'error')
              )
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              {bbk_filter_sql}
              {source_filter_sql}
            GROUP BY 1
            ORDER BY count DESC, reason ASC
            LIMIT 10
            """,
            tuple(
                [start_time, end_time]
                + bbk_filter_params
                + source_filter_params,
            ),
        )

        pairs = [
            (row.get("reason") or "其他", int(row.get("count") or 0))
            for row in rows
        ]
        total = sum(count for _, count in pairs)

        items = []
        for reason, count in pairs:
            percent = (count / total * 100) if total > 0 else 0.0
            items.append(
                CronErrorReasonItem(
                    reason=reason,
                    count=count,
                    percent=round(percent, 2),
                ),
            )
        return items

    def _parse_date_range(
        self,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> Tuple[datetime, datetime]:
        """解析日期字符串为时间范围。

        Args:
            start_date: 开始日期字符串 (YYYY-MM-DD格式)
            end_date: 结束日期字符串 (YYYY-MM-DD格式)

        Returns:
            (start_time, end_time) datetime 元组

        Note:
            未传参数时默认最近30天。
            结束日期会设置为当天的23:59:59以包含全天数据。
        """
        if start_date and end_date:
            try:
                start_time = datetime.strptime(start_date, "%Y-%m-%d")
                end_time = datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23,
                    minute=59,
                    second=59,
                    microsecond=999999,
                )
                return start_time, end_time
            except ValueError:
                # 格式错误时使用默认值
                pass
        # 默认最近30天
        end_time = (
            datetime.now(BEIJING_TZ)
            .replace(
                hour=23,
                minute=59,
                second=59,
                microsecond=999999,
            )
            .replace(tzinfo=None)
        )
        start_time = end_time - timedelta(days=30)
        start_time = start_time.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return start_time, end_time

    def _resolve_time_range(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> Tuple[datetime, datetime]:
        """解析时间范围，未传则默认最近30天。"""
        if start_date and end_date:
            return start_date, end_date
        # 默认最近30天
        end_time = (
            datetime.now(BEIJING_TZ)
            .replace(
                hour=23,
                minute=59,
                second=59,
                microsecond=999999,
            )
            .replace(tzinfo=None)
        )
        start_time = end_time - timedelta(days=30)
        start_time = start_time.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return start_time, end_time

    @staticmethod
    def _init_skill_dicts(
        skill_jobs: dict,
        skill_total: dict,
        skill_success: dict,
        skill_read: dict,
        skill_error: dict,
        sk: str,
    ) -> None:
        skill_jobs[sk] = set()
        skill_total[sk] = 0
        skill_success[sk] = 0
        skill_read[sk] = 0
        skill_error[sk] = 0

    @staticmethod
    def _count_skill_execution(
        skill_jobs: dict,
        skill_total: dict,
        skill_success: dict,
        skill_read: dict,
        skill_error: dict,
        sk: str,
        job_id: str,
        status: str,
        async_status: str,
        is_read: bool,
    ) -> None:
        """Count a single skill execution in the aggregation dicts."""
        skill_total[sk] += 1
        skill_jobs[sk].add(job_id)
        # 成功：status='success' AND async_status='success'
        if status == "success" and async_status == "success":
            skill_success[sk] += 1
        if is_read:
            skill_read[sk] += 1
        # 失败：status='error' OR (status='success' AND async_status='error')
        if status in ("error", "timeout", "cancelled") or (
            status == "success" and async_status == "error"
        ):
            skill_error[sk] += 1

    async def _fetch_branch_skills_raw_data(
        self,
        db: DatabaseConnection,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
    ) -> list[dict]:
        """获取分行技能原始执行数据."""
        source_where = " AND j.source_id = %s" if source_id else ""
        skill_expr = self._skill_display_expr("s")
        sql = f"""
            SELECT
                e.job_id,
                e.status,
                e.async_status,
                e.is_read,
                s.skill_id AS skill_id,
                {skill_expr} AS skill_name
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            {self._skill_binding_join("j", "s")}
            WHERE j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              {source_where}
        """
        params: list = [bbk_id, start_time, end_time]
        if source_id:
            params.append(source_id)
        return await db.fetch_all(sql, tuple(params))

    def _aggregate_branch_skills(
        self,
        rows: list[dict],
    ) -> dict[str, dict[str, Any]]:
        """聚合分行技能执行统计."""
        skill_jobs: dict[str, set[str]] = {}
        skill_total: dict[str, int] = {}
        skill_success: dict[str, int] = {}
        skill_read: dict[str, int] = {}
        skill_error: dict[str, int] = {}
        skill_names: dict[str, str] = {}

        for row in rows:
            self._process_skill_row(
                row,
                skill_jobs,
                skill_total,
                skill_success,
                skill_read,
                skill_error,
                skill_names,
            )

        return {
            "skill_jobs": skill_jobs,
            "skill_total": skill_total,
            "skill_success": skill_success,
            "skill_read": skill_read,
            "skill_error": skill_error,
            "skill_names": skill_names,
        }

    def _process_skill_row(
        self,
        row: dict,
        skill_jobs: dict[str, set[str]],
        skill_total: dict[str, int],
        skill_success: dict[str, int],
        skill_read: dict[str, int],
        skill_error: dict[str, int],
        skill_names: dict[str, str],
    ) -> None:
        """处理单行数据的技能聚合."""
        skill_id = str(row.get("skill_id") or "").strip()
        skill_name = str(row.get("skill_name") or "").strip()
        if not skill_id:
            return
        job_id = row["job_id"]
        status = (row["status"] or "").lower()
        async_status = (row["async_status"] or "").lower()
        is_read = bool(row["is_read"])
        if skill_id not in skill_jobs:
            self._init_skill_dicts(
                skill_jobs,
                skill_total,
                skill_success,
                skill_read,
                skill_error,
                skill_id,
            )
        if skill_name:
            skill_names.setdefault(skill_id, skill_name)
        self._count_skill_execution(
            skill_jobs,
            skill_total,
            skill_success,
            skill_read,
            skill_error,
            skill_id,
            job_id,
            status,
            async_status,
            is_read,
        )

    def _build_branch_skill_items(
        self,
        skill_jobs: dict[str, set[str]],
        skill_total: dict[str, int],
        skill_success: dict[str, int],
        skill_read: dict[str, int],
        skill_error: dict[str, int],
        skill_names: dict[str, str],
    ) -> list[BranchSkillItem]:
        """构建分行技能统计结果列表."""
        items: list[BranchSkillItem] = []
        for skill_id, jobs in skill_jobs.items():
            task_count = len(jobs)
            success = skill_success.get(skill_id, 0)
            exec_total = skill_total.get(skill_id, 0)
            skill_name = skill_names.get(skill_id, skill_id)
            items.append(
                BranchSkillItem(
                    skill_name=skill_name,
                    cron_task_count=task_count,
                    success_count=success,
                    success_rate=self._percent(success, exec_total),
                    read_count=skill_read.get(skill_id, 0),
                    error_count=skill_error.get(skill_id, 0),
                ),
            )
        items.sort(key=lambda item: item.cron_task_count, reverse=True)
        return items

    async def get_branch_skills(
        self,
        bbk_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> BranchSkillResponse:
        """获取分行技能维度数据。

        从 swe_cron_executions + swe_cron_jobs 绑定技能链路，
        关联市场技能统计开关并聚合统计每项技能的执行情况。
        """
        db = get_db_connection()
        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        rows = await self._fetch_branch_skills_raw_data(
            db,
            bbk_id,
            start_time,
            end_time,
            source_id,
        )

        stats = self._aggregate_branch_skills(rows)

        items = self._build_branch_skill_items(
            stats["skill_jobs"],
            stats["skill_total"],
            stats["skill_success"],
            stats["skill_read"],
            stats["skill_error"],
            stats["skill_names"],
        )

        return BranchSkillResponse(
            start_date=start_str,
            end_date=end_str,
            bbk_id=bbk_id,
            bbk_name=get_bbk_name_by_id(bbk_id) or bbk_id,
            items=items,
        )

    async def _fetch_manager_base_info(
        self,
        db: DatabaseConnection,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> list[dict]:
        """查询客户经理基础信息."""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")
        sql = f"""
            SELECT
                j.tenant_id AS user_id,
                MAX(j.tenant_name) AS user_name,
                COUNT(DISTINCT j.id) AS total_tasks,
                SUM(CASE WHEN e.status = 'success' AND e.async_status = 'success'
                    THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN e.is_read = 1 THEN 1 ELSE 0 END) AS read_tasks
            FROM swe_cron_jobs j
            JOIN swe_cron_executions e ON j.id = e.job_id
                AND e.actual_time >= %s AND e.actual_time <= %s
            WHERE j.bbk_id = %s AND j.deleted_at IS NULL AND j.status != 'deleted'
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
            GROUP BY j.tenant_id
        """
        params: list = [start_time, end_time, bbk_id]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        return await db.fetch_all(sql, tuple(params))

    async def _fetch_manager_skill_count(
        self,
        db: DatabaseConnection,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> dict[str, int]:
        """查询客户经理技能数量."""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""
        sql = f"""
            SELECT j.tenant_id AS user_id,
                COUNT(DISTINCT s.skill_id) AS skill_count
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            {self._skill_binding_join("j", "s")}
            WHERE j.bbk_id = %s AND e.actual_time >= %s AND e.actual_time <= %s
              {jkh_filter_sql}
              {source_where}
            GROUP BY j.tenant_id
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(sql, tuple(params))
        return {row["user_id"]: row["skill_count"] for row in rows}

    async def _fetch_manager_recommended_customers(
        self,
        db: DatabaseConnection,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> dict[str, int]:
        """查询推荐客户数."""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "e.tenant_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND j.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")
        sql = f"""
            SELECT j.tenant_id AS user_id,
                COUNT(DISTINCT s.custuid) AS recommended_customers
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            JOIN swe_cron_subtasks s ON e.trace_id = s.trace_id
            WHERE j.bbk_id = %s AND e.actual_time >= %s AND e.actual_time <= %s
              AND s.custuid IS NOT NULL
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
            GROUP BY j.tenant_id
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(sql, tuple(params))
        return {row["user_id"]: row["recommended_customers"] for row in rows}

    async def _fetch_manager_click_stats(
        self,
        db: DatabaseConnection,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> dict[str, dict[str, int]]:
        """查询客户点击统计."""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "c.user_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND c.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")
        sql = f"""
            SELECT c.user_id, CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN 'plan' ELSE c.button_type END AS button_type,
                COUNT(DISTINCT c.customer_id) AS customer_count
            FROM swe_html_preview_click_events c
            JOIN swe_cron_jobs j ON c.cron_task_id = j.id
            WHERE c.bbk_id = %s AND c.clicked_at >= %s AND c.clicked_at <= %s
              AND (c.event_type = 'preview_view' AND c.template_type = 'sub'
              OR (c.event_type = 'button_click' AND c.button_type IN ('insight', 'phone')))
              AND c.customer_id IS NOT NULL
              AND j.deleted_at IS NULL
              AND j.status != 'deleted'
              AND {skill_exists}
              {jkh_filter_sql}
              {source_where}
            GROUP BY c.user_id, CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN 'plan' ELSE c.button_type END
        """
        params: list = [bbk_id, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(sql, tuple(params))
        click_map: dict[str, dict[str, int]] = {}
        for row in rows:
            user_id = row["user_id"]
            if user_id not in click_map:
                click_map[user_id] = {}
            click_map[user_id][row["button_type"]] = row["customer_count"]
        return click_map

    async def _fetch_manager_contact_stats(
        self,
        db: DatabaseConnection,
        bbk_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
        sync_date: Optional[str] = None,
    ) -> dict[str, dict[str, Any]]:
        """查询客户经理维度的接触客户数和客户接触率."""
        jkh_filter_sql, jkh_filter_params = self._build_jkh_filter(
            "a.user_id",
            sync_date,
            bbk_id=bbk_id,
        )
        source_where = " AND a.source_id = %s" if source_id else ""
        skill_exists = self._statistics_skill_exists("j")
        sql = f"""
            SELECT
                a.user_id,
                COUNT(DISTINCT a.customer_id) AS clicked_customers,
                COUNT(
                    DISTINCT CASE
                        WHEN b.cust_uid IS NOT NULL THEN b.cust_uid
                    END
                ) AS contacted_customers,
                CASE
                    WHEN COUNT(DISTINCT a.customer_id) = 0 THEN 0
                    ELSE COUNT(
                        DISTINCT CASE
                            WHEN b.cust_uid IS NOT NULL THEN b.cust_uid
                        END
                    ) * 1.0 / COUNT(DISTINCT a.customer_id)
                END AS contact_rate
            FROM swe_html_preview_click_events a
            LEFT JOIN swe_skill_contact_detail b ON a.id = b.click_id
            WHERE a.bbk_id = %s
              AND a.customer_id IS NOT NULL
              AND (a.event_type = 'preview_view' AND a.template_type = 'sub'
              OR (a.event_type = 'button_click' AND a.button_type IN ('insight', 'phone')))
              AND (
                a.clicked_at >= %s AND a.clicked_at <= %s
                OR b.clicked_at >= %s AND b.clicked_at <= %s
              )
              {jkh_filter_sql}
              {source_where}
              AND EXISTS (
                SELECT 1
                FROM swe_cron_jobs j
                WHERE j.id = a.cron_task_id
                  AND j.deleted_at IS NULL
                  AND j.status != 'deleted'
                  AND {skill_exists}
              )
            GROUP BY a.user_id
        """
        params: list = [bbk_id, start_time, end_time, start_time, end_time]
        params.extend(jkh_filter_params)
        if source_id:
            params.append(source_id)
        logger.info(
            f"[_fetch_manager_contact_stats] SQL: {sql}, params: {params}",
        )
        rows = await db.fetch_all(sql, tuple(params))
        logger.info(f"[_fetch_manager_contact_stats] Result rows: {len(rows)}")
        return {
            row["user_id"]: {
                "clicked_customers": self._row_int(row, "clicked_customers"),
                "contacted_customers": self._row_int(
                    row,
                    "contacted_customers",
                ),
                "contact_rate": _to_float(row.get("contact_rate")),
            }
            for row in rows
        }

    def _build_manager_summary_items(
        self,
        base_rows: list[dict],
        skill_count_map: dict[str, int],
        recommended_map: dict[str, int],
        click_map: dict[str, dict[str, int]],
        contact_map: dict[str, dict[str, Any]],
    ) -> list[BranchManagerSummaryItem]:
        """构建客户经理汇总结果."""
        items: list[BranchManagerSummaryItem] = []
        for row in base_rows:
            user_id = row["user_id"]
            user_clicks = click_map.get(user_id, {})
            user_contact = contact_map.get(user_id, {})
            items.append(
                BranchManagerSummaryItem(
                    user_id=user_id,
                    user_name=row["user_name"] or "",
                    skill_count=skill_count_map.get(user_id, 0),
                    total_tasks=row["total_tasks"] or 0,
                    success_count=row["success_count"] or 0,
                    read_tasks=row["read_tasks"] or 0,
                    recommended_customers=recommended_map.get(user_id, 0),
                    viewed_customers=user_clicks.get("plan", 0),
                    contacted_customers=user_contact.get(
                        "contacted_customers",
                        0,
                    ),
                    contact_rate=user_contact.get("contact_rate", 0.0),
                    insight_customers=user_clicks.get("insight", 0),
                    phone_customers=user_clicks.get("phone", 0),
                ),
            )
        items.sort(key=lambda x: x.success_count, reverse=True)
        return items

    async def get_branch_manager_summary(
        self,
        bbk_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> BranchManagerSummaryResponse:
        """获取分行客户经理汇总数据（技能视角，使用市场表统计开关过滤）。

        只统计使用统计技能的客户经理，各项指标都基于市场表统计开关过滤。
        """
        db = get_db_connection()
        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        sync_date = await self._resolve_jkh_sync_date(db, end_time)
        if sync_date is None:
            return BranchManagerSummaryResponse(
                start_date=start_str,
                end_date=end_str,
                bbk_id=bbk_id,
                bbk_name=get_bbk_name_by_id(bbk_id) or bbk_id,
                items=[],
            )

        # 并行查询各项数据
        base_rows, skill_count_map, recommended_map, click_map, contact_map = (
            await asyncio.gather(
                self._fetch_manager_base_info(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                ),
                self._fetch_manager_skill_count(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                ),
                self._fetch_manager_recommended_customers(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                ),
                self._fetch_manager_click_stats(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                ),
                self._fetch_manager_contact_stats(
                    db,
                    bbk_id,
                    start_time,
                    end_time,
                    source_id,
                    sync_date=sync_date,
                ),
            )
        )

        items = self._build_manager_summary_items(
            base_rows,
            skill_count_map,
            recommended_map,
            click_map,
            contact_map,
        )

        return BranchManagerSummaryResponse(
            start_date=start_str,
            end_date=end_str,
            bbk_id=bbk_id,
            bbk_name=get_bbk_name_by_id(bbk_id) or bbk_id,
            items=items,
        )

    async def get_branch_skill_managers(
        self,
        bbk_id: str,
        skill_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> BranchSkillManagerResponse:
        """获取分行+技能的客户经理维度数据。

        从 swe_cron_executions 出发，联结 jobs 和 skill_ids，
        再按技能展示名筛选，LEFT JOIN click_events 统计点击指标。
        无点击记录时 plan/insight/phone 计数为 0，last_click_time 为 None。
        user_name 取自 swe_cron_jobs.tenant_name。
        """
        db = get_db_connection()

        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        source_where = " AND j.source_id = %s" if source_id else ""
        click_source_on = " AND c.source_id = %s" if source_id else ""
        skill_expr = self._skill_display_expr("s")

        sql = f"""
            SELECT
                j.tenant_id AS user_id,
                MAX(j.tenant_name) AS user_name,
                COUNT(DISTINCT CASE WHEN e.is_read = 1 THEN e.id END) AS read_count,
                COUNT(DISTINCT CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN c.id END) AS plan_count,
                COUNT(DISTINCT CASE WHEN c.button_type = 'insight' THEN c.id END) AS insight_count,
                COUNT(DISTINCT CASE WHEN c.button_type = 'phone' THEN c.id END) AS phone_count,
                MAX(c.clicked_at) AS last_click_time
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            {self._skill_binding_join("j", "s")}
            LEFT JOIN swe_html_preview_click_events c
                ON c.cron_task_id = e.job_id
                AND c.clicked_at >= %s AND c.clicked_at <= %s
                AND (c.event_type = 'button_click' OR (c.event_type = 'preview_view' AND c.template_type = 'sub'))
                {click_source_on}
            WHERE j.bbk_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              AND {skill_expr} = %s
              {source_where}
            GROUP BY j.tenant_id
            ORDER BY read_count DESC
        """
        params: list = [
            start_time,
            end_time,
            bbk_id,
            start_time,
            end_time,
            skill_name,
        ]
        if source_id:
            params.insert(2, source_id)  # click_source_on placeholder
            params.append(source_id)  # source_where placeholder
        rows = await db.fetch_all(sql, tuple(params))

        items: list[BranchSkillManagerItem] = []
        for row in rows:
            last_click = row["last_click_time"]
            items.append(
                BranchSkillManagerItem(
                    user_id=row["user_id"] or "",
                    user_name=row["user_name"] or "",
                    read_count=row["read_count"] or 0,
                    plan_count=row["plan_count"] or 0,
                    insight_count=row["insight_count"] or 0,
                    phone_count=row["phone_count"] or 0,
                    last_click_time=(
                        last_click.strftime("%Y-%m-%d %H:%M:%S")
                        if last_click
                        else None
                    ),
                ),
            )

        return BranchSkillManagerResponse(
            start_date=start_str,
            end_date=end_str,
            bbk_id=bbk_id,
            skill_name=skill_name,
            items=items,
        )

    async def get_branch_skill_manager_customers(
        self,
        bbk_id: str,
        skill_name: str,
        user_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> BranchSkillManagerCustomerResponse:
        """获取分行+技能+客户经理的客户维度数据。

        从 swe_html_preview_click_events 出发，通过任务绑定技能和技能表
        过滤指定技能以及客户经理的行，按客户聚合点击行为。
        """
        db = get_db_connection()

        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        source_where = " AND c.source_id = %s" if source_id else ""
        skill_expr = self._skill_display_expr("s")

        sql = f"""
            SELECT
                c.customer_id,
                c.customer_name,
                MAX(CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN 1 ELSE 0 END) AS clicked_plan,
                MAX(CASE WHEN c.button_type = 'insight' THEN 1 ELSE 0 END) AS clicked_insight,
                MAX(CASE WHEN c.button_type = 'phone' THEN 1 ELSE 0 END) AS clicked_phone,
                MAX(c.clicked_at) AS click_time
            FROM swe_html_preview_click_events c
            JOIN swe_cron_jobs j ON c.cron_task_id = j.id
            {self._skill_binding_join("j", "s")}
            WHERE c.bbk_id = %s
              AND c.user_id = %s
              AND {skill_expr} = %s
              AND c.clicked_at >= %s AND c.clicked_at <= %s
              AND (c.event_type = 'button_click' OR (c.event_type = 'preview_view' AND c.template_type = 'sub'))
              {source_where}
            GROUP BY c.customer_id, c.customer_name
            ORDER BY click_time DESC
        """
        params: list = [bbk_id, user_id, skill_name, start_time, end_time]
        if source_id:
            params.append(source_id)
        rows = await db.fetch_all(sql, tuple(params))

        items: list[BranchSkillManagerCustomerItem] = []
        for row in rows:
            click_time = row["click_time"]
            items.append(
                BranchSkillManagerCustomerItem(
                    customer_id=row["customer_id"] or "",
                    customer_name=row["customer_name"] or "",
                    clicked_plan=bool(row["clicked_plan"]),
                    clicked_insight=bool(row["clicked_insight"]),
                    clicked_phone=bool(row["clicked_phone"]),
                    click_time=(
                        click_time.strftime("%Y-%m-%d %H:%M:%S")
                        if click_time
                        else None
                    ),
                ),
            )

        return BranchSkillManagerCustomerResponse(
            start_date=start_str,
            end_date=end_str,
            bbk_id=bbk_id,
            skill_name=skill_name,
            user_id=user_id,
            items=items,
        )

    async def _fetch_manager_user_name(
        self,
        db: DatabaseConnection,
        bbk_id: str,
        user_id: str,
        source_id: Optional[str],
    ) -> str:
        """查询客户经理姓名."""
        source_where = " AND source_id = %s" if source_id else ""
        sql = f"""
            SELECT MAX(tenant_name) AS user_name
            FROM swe_cron_jobs
            WHERE bbk_id = %s AND tenant_id = %s
              AND deleted_at IS NULL AND status != 'deleted'
              {source_where}
        """
        params: list = [bbk_id, user_id]
        if source_id:
            params.append(source_id)
        row = await db.fetch_one(sql, tuple(params))
        return row["user_name"] if row else ""

    async def _fetch_manager_skill_stats(
        self,
        db: DatabaseConnection,
        bbk_id: str,
        user_id: str,
        start_time: datetime,
        end_time: datetime,
        source_id: Optional[str],
    ) -> list[dict]:
        """查询客户经理技能统计."""
        source_where = " AND j.source_id = %s" if source_id else ""
        sql = f"""
            SELECT
                COALESCE(
                    NULLIF(MIN(s.cn_name), ''),
                    NULLIF(MIN(s.skill_name), ''),
                    s.skill_id
                ) AS skill_name,
                COUNT(DISTINCT e.job_id) AS cron_task_count,
                SUM(CASE WHEN e.status = 'success' AND e.async_status = 'success'
                    THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN e.is_read = 1 THEN 1 ELSE 0 END) AS read_count,
                SUM(CASE WHEN e.status = 'error'
                         OR (e.status = 'success' AND e.async_status = 'error')
                    THEN 1 ELSE 0 END) AS error_count
            FROM swe_cron_executions e
            JOIN swe_cron_jobs j ON e.job_id = j.id
            {self._skill_binding_join("j", "s")}
            WHERE j.bbk_id = %s AND j.tenant_id = %s
              AND e.actual_time >= %s AND e.actual_time <= %s
              {source_where}
            GROUP BY s.skill_id
            ORDER BY success_count DESC
        """
        params: list = [bbk_id, user_id, start_time, end_time]
        if source_id:
            params.append(source_id)
        return await db.fetch_all(sql, tuple(params))

    def _build_manager_skill_items(
        self,
        rows: list[dict],
    ) -> list[ManagerSkillItem]:
        """构建客户经理技能统计结果（SQL已按市场表统计开关过滤技能）."""
        items: list[ManagerSkillItem] = []
        for row in rows:
            skill_name = row["skill_name"]
            success_count = row["success_count"] or 0
            error_count = row["error_count"] or 0
            items.append(
                ManagerSkillItem(
                    skill_name=skill_name,
                    cron_task_count=row["cron_task_count"] or 0,
                    success_count=success_count,
                    success_rate=self._percent(
                        success_count,
                        success_count + error_count,
                    ),
                    read_count=row["read_count"] or 0,
                    error_count=error_count,
                ),
            )
        return items

    async def get_manager_skills(
        self,
        bbk_id: str,
        user_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> ManagerSkillResponse:
        """获取客户经理技能维度数据。

        统计指定客户经理在各技能下的执行数据。
        """
        db = get_db_connection()
        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        user_name = await self._fetch_manager_user_name(
            db,
            bbk_id,
            user_id,
            source_id,
        )
        rows = await self._fetch_manager_skill_stats(
            db,
            bbk_id,
            user_id,
            start_time,
            end_time,
            source_id,
        )
        items = self._build_manager_skill_items(rows)

        return ManagerSkillResponse(
            start_date=start_str,
            end_date=end_str,
            bbk_id=bbk_id,
            user_id=user_id,
            user_name=user_name or user_id,
            items=items,
        )

    async def get_manager_customers(
        self,
        bbk_id: str,
        user_id: str,
        skill_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> ManagerCustomerResponse:
        """获取客户经理客户维度数据（技能视角，使用市场表统计开关过滤）。

        从 click_events 获取该客户经理点击过的客户，但只统计命中市场表统计开关技能任务的点击。
        如果指定了 skill_name，则只返回该技能下的客户点击情况。
        """
        db = get_db_connection()

        start_time, end_time = self._parse_date_range(start_date, end_date)
        start_str = start_date or start_time.strftime("%Y-%m-%d")
        end_str = end_date or end_time.strftime("%Y-%m-%d")

        source_where = " AND c.source_id = %s" if source_id else ""
        skill_expr = self._skill_display_expr("s")

        # 未指定技能时只依赖市场表统计开关过滤。
        if skill_name:
            skill_filter = f"AND {skill_expr} = %s"
            skill_params = [skill_name]
        else:
            skill_filter = ""
            skill_params = []

        # 查询客户经理姓名（不需要表别名）
        user_source_where = " AND source_id = %s" if source_id else ""
        user_name_sql = f"""
            SELECT MAX(tenant_name) AS user_name
            FROM swe_cron_jobs
            WHERE bbk_id = %s AND tenant_id = %s
              AND deleted_at IS NULL AND status != 'deleted'
              {user_source_where}
        """
        params: list = [bbk_id, user_id]
        if source_id:
            params.append(source_id)
        user_row = await db.fetch_one(user_name_sql, tuple(params))
        user_name = user_row["user_name"] if user_row else ""

        # 查询客户点击统计（技能过滤）
        # 只通过 cron_task_id 关联，不限制点击时间必须在执行期间
        sql = f"""
            SELECT
                c.customer_id,
                c.customer_name,
                MAX(CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub' THEN 1 ELSE 0 END) AS clicked_plan,
                MAX(CASE WHEN c.button_type = 'insight' THEN 1 ELSE 0 END) AS clicked_insight,
                MAX(CASE WHEN c.button_type = 'phone' THEN 1 ELSE 0 END) AS clicked_phone,
                MAX(c.clicked_at) AS click_time
            FROM swe_html_preview_click_events c
            JOIN swe_cron_jobs j ON c.cron_task_id = j.id
            {self._skill_binding_join("j", "s")}
            WHERE c.bbk_id = %s
              AND c.user_id = %s
              AND c.clicked_at >= %s AND c.clicked_at <= %s
              AND c.customer_id IS NOT NULL
              AND (c.event_type = 'button_click' OR (c.event_type = 'preview_view' AND c.template_type = 'sub'))
              {skill_filter}
              {source_where}
            GROUP BY c.customer_id, c.customer_name
            ORDER BY click_time DESC
        """
        customer_params: list = [
            bbk_id,
            user_id,
            start_time,
            end_time,
        ] + skill_params
        if source_id:
            customer_params.append(source_id)
        rows = await db.fetch_all(sql, tuple(customer_params))

        items: list[ManagerCustomerItem] = []
        for row in rows:
            click_time = row["click_time"]
            items.append(
                ManagerCustomerItem(
                    customer_id=row["customer_id"] or "",
                    customer_name=row["customer_name"] or "",
                    clicked_plan=bool(row["clicked_plan"]),
                    clicked_insight=bool(row["clicked_insight"]),
                    clicked_phone=bool(row["clicked_phone"]),
                    click_time=(
                        click_time.strftime("%Y-%m-%d %H:%M:%S")
                        if click_time
                        else None
                    ),
                ),
            )

        return ManagerCustomerResponse(
            start_date=start_str,
            end_date=end_str,
            bbk_id=bbk_id,
            user_id=user_id,
            user_name=user_name or user_id,
            items=items,
        )

    def _build_bbk_filter(
        self,
        bbk_ids: Optional[str],
    ) -> Tuple[str, List]:
        """构建 bbk 过滤条件。"""
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

    def _build_source_filter(
        self,
        source_id: Optional[str],
    ) -> Tuple[str, List]:
        """构建 source 过滤条件。"""
        if not source_id:
            return "", []
        return " AND j.source_id = %s", [source_id]


# Global query service instance
_query_service: Optional[QueryService] = None


def get_query_service() -> QueryService:
    """Get the query service instance.

    Returns:
        QueryService instance
    """
    global _query_service
    if _query_service is None:
        _query_service = QueryService()
    return _query_service
