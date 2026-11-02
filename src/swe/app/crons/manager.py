# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import httpx
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar, cast

from ..b3_headers import PASSTHROUGH_HEADERS_META_KEY
from ..channels.schema import DEFAULT_CHANNEL
from ..tenant_context import bind_tenant_context
from ..console_push_store import append as push_store_append
from ..cron_result_metrics import (
    CRON_SESSION_ROUTE_MISMATCH_TOTAL,
    increment_cron_result_metric,
)
from ...config.context import (
    canonicalize_scope_id,
    is_valid_identity_value,
    resolve_runtime_identity,
    resolve_scope_id,
)
from ...config.llm_workload import LLM_WORKLOAD_CRON, bind_llm_workload
from ..source_system_config.runtime import (
    bind_source_system_config,
    get_current_source_system_config,
    reset_current_source_system_config,
    resolve_cron_notification_config,
    resolve_cron_task_session_cleanup_config,
    resolve_cron_unread_auto_pause_config,
    set_current_source_system_config,
)
from ..continuous_governance.models import GOVERNANCE_ID_MAX_LENGTH
from .auth_state import prefetch_auth_token
from .cron_utils import compute_next_run_at, compute_next_run_times
from .executor import CronExecutor
from .models import CronJobSpec, CronJobState, CronTaskView, JobsFile
from .task_view import MANUAL_PAUSE_REASON, build_cron_task_view
from ..runner.models import ChatSpec
from .repo.base import BaseJobRepository
from .scheduler_adapter import SchedulerAdapter, NoopSchedulerAdapter
from .monitor_sync_client import get_monitor_sync_client, MonitorSyncClient
from .workflow.cron_adapter import WorkflowCronExecutor
from .broadcast import (
    DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS,
    MAX_BROADCAST_OFFSET_WINDOW_HOURS,
    MIN_BROADCAST_OFFSET_WINDOW_HOURS,
    shift_cron_expression,
)

HEARTBEAT_JOB_ID = "_heartbeat"
DREAM_JOB_ID = "_dream"
TASK_SESSION_CLEANUP_TASK_TYPE = "cleanup"
AUTO_PAUSE_REASON = "auto_unread_threshold"
TASK_MESSAGES_STATE_KEY = "task_messages"
TASK_SUCCESS_EXECUTION_KEYS_META_KEY = "task_success_execution_keys"
MAX_TASK_SUCCESS_EXECUTION_KEYS = 100
_SYSTEM_JOB_IDS_FILE = "system_jobs.json"
MAX_NOTIFICATION_DELAY_MINUTES = 7 * 24 * 60
BROADCAST_SOURCE_JOB_ID_META_KEY = "broadcast_source_job_id"
BROADCAST_DISPATCH_INTENTS_ENABLED_META_KEY = (
    "broadcast_dispatch_intents_enabled"
)
BATCH_DISPATCH_EXTERNAL_JOB_ID_META_KEY = "batch_dispatch_external_job_id"
BATCH_DISPATCH_OFFSET_WINDOW_HOURS_META_KEY = (
    "batch_dispatch_offset_window_hours"
)
BATCH_DISPATCH_OFFSET_MINUTES_META_KEY = "batch_dispatch_offset_minutes"
BATCH_DISPATCH_CRON_META_KEY = "batch_dispatch_cron"
BATCH_DISPATCH_CRON_WARNING_META_KEY = "batch_dispatch_cron_warning"
BATCH_DISPATCH_PARENT_CRON_META_KEY = "batch_dispatch_parent_cron"
BATCH_DISPATCH_SWE_SERVER_DOMAIN_PARAM = "swe_server_domain"
BATCH_DISPATCH_JOB_NAME_PREFIX = "[\u6279\u8c03\u5ea6]"
BATCH_DISPATCH_CRON_FALLBACK_WARNING = (
    "cron offset not applied: unsupported cron, using original schedule"
)
DISPATCH_INTENTS_ENABLED_ENV = "SWE_CRON_DISPATCH_INTENTS_ENABLED"
SCHEDULER_API_URL_ENV = "SWE_SCHEDULER_API_URL"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

# 心跳 every 字段解析正则（如 "30m"、"6h"）
_EVERY_PATTERN = re.compile(
    r"^(?:(?P<hours>\d+)h|(?P<minutes>\d+)m)$",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _notification_delay_minutes(job: CronJobSpec) -> int:
    meta = job.meta or {}
    try:
        delay_minutes = int(meta.get("notification_delay_minutes", 0) or 0)
    except (TypeError, ValueError):
        return 0
    if delay_minutes < 0:
        return 0
    return min(delay_minutes, MAX_NOTIFICATION_DELAY_MINUTES)


def _dispatch_parent_scheduled_fire_at(
    execution_meta: Optional[Dict[str, Any]],
) -> datetime | None:
    if not isinstance(execution_meta, dict):
        return None
    dispatch_meta = execution_meta.get("cron_dispatch")
    if not isinstance(dispatch_meta, dict):
        return None
    value = dispatch_meta.get("parent_scheduled_fire_at")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_cron_execution_identity(dispatch_meta: dict[str, Any]) -> bool:
    """Return whether the scheduler supplied a stable execution identity."""
    if dispatch_meta.get("cron_execution_key") or dispatch_meta.get(
        "execution_key",
    ):
        return True
    if dispatch_meta.get("intent_id") and dispatch_meta.get("batch_id"):
        return True
    return bool(
        dispatch_meta.get("scheduled_fire_at")
        or dispatch_meta.get("fire_time")
        or dispatch_meta.get("trigger_time")
        or dispatch_meta.get("parent_scheduled_fire_at")
        or dispatch_meta.get("external_execution_id"),
    )


def broadcast_dispatch_intents_enabled(job: Any | None) -> bool:
    meta = getattr(job, "meta", {}) or {}
    if not isinstance(meta, dict):
        return False
    return bool(meta.get(BROADCAST_DISPATCH_INTENTS_ENABLED_META_KEY))


def dispatch_intents_runtime_enabled() -> bool:
    raw_value = os.environ.get(DISPATCH_INTENTS_ENABLED_ENV, "")
    return raw_value.strip().lower() in _TRUE_ENV_VALUES


def is_batch_dispatch_managed_broadcast_child(job: Any | None) -> bool:
    if not dispatch_intents_runtime_enabled():
        return False
    meta = getattr(job, "meta", {}) or {}
    if not isinstance(meta, dict):
        return False
    return bool(
        meta.get(BROADCAST_SOURCE_JOB_ID_META_KEY)
        and broadcast_dispatch_intents_enabled(job),
    )


def is_batch_dispatch_parent(job: Any | None) -> bool:
    if not dispatch_intents_runtime_enabled():
        return False
    meta = getattr(job, "meta", {}) or {}
    if not isinstance(meta, dict):
        return False
    return bool(
        broadcast_dispatch_intents_enabled(job)
        and not meta.get(BROADCAST_SOURCE_JOB_ID_META_KEY),
    )


def _merge_cron_dispatch_meta(
    execution_meta: Optional[Dict[str, Any]],
    dispatch_meta: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not dispatch_meta:
        return execution_meta
    merged = dict(execution_meta or {})
    merged["cron_dispatch"] = dict(dispatch_meta)
    return merged


def _is_weekend_notification_time(
    due_at: datetime,
    timezone_name: str,
) -> bool:
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "Invalid cron notification timezone %s; fallback to UTC",
            timezone_name,
        )
        tz = timezone.utc
    return due_at.astimezone(tz).weekday() >= 5


@dataclass(frozen=True)
class _MonitorNotificationSchedule:
    due_at: datetime | None
    timezone: str
    suppress: bool = False


def _broadcast_offset_minutes(job: CronJobSpec) -> int:
    try:
        offset = int((job.meta or {}).get("broadcast_offset_minutes", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(offset, 0)


def _scheduled_notification_due_at(
    job: CronJobSpec,
    actual_time: datetime,
    end_time: datetime | None,
    execution_meta: Optional[Dict[str, Any]],
    timezone_name: str,
) -> tuple[datetime | None, str]:
    delay_minutes = _notification_delay_minutes(job)
    parent_scheduled_fire_at = _dispatch_parent_scheduled_fire_at(
        execution_meta,
    )
    original_timezone = (job.meta or {}).get(
        "broadcast_original_timezone",
    ) or timezone_name
    if parent_scheduled_fire_at is not None:
        return (
            parent_scheduled_fire_at + timedelta(minutes=delay_minutes),
            original_timezone,
        )
    if (job.meta or {}).get(
        "broadcast_notification_policy",
    ) == "original_schedule":
        total_delay = _broadcast_offset_minutes(job) + delay_minutes
        return actual_time + timedelta(minutes=total_delay), original_timezone
    if delay_minutes > 0:
        return (
            (end_time or actual_time) + timedelta(minutes=delay_minutes),
            timezone_name,
        )
    return None, timezone_name


def _monitor_notification_schedule(
    job: CronJobSpec,
    exec_status: str,
    actual_time: datetime,
    end_time: datetime | None,
    is_manual: bool,
    execution_meta: Optional[Dict[str, Any]],
    default_timezone: str,
) -> _MonitorNotificationSchedule:
    timezone_name = job.schedule.timezone or default_timezone or "UTC"
    if exec_status != "success" or is_manual:
        return _MonitorNotificationSchedule(None, timezone_name)

    due_at, timezone_name = _scheduled_notification_due_at(
        job,
        actual_time,
        end_time,
        execution_meta,
        timezone_name,
    )
    notification_time = due_at or end_time or actual_time
    notification_config = resolve_cron_notification_config(
        get_current_source_system_config(),
    )
    if not notification_config.skip_weekend_zhaohu_enabled:
        return _MonitorNotificationSchedule(due_at, timezone_name)
    if not _is_weekend_notification_time(notification_time, timezone_name):
        return _MonitorNotificationSchedule(due_at, timezone_name)

    logger.info(
        "Suppress cron weekend zhaohu notification: "
        "job_id=%s job_name=%s original_notification_time=%s",
        job.id,
        job.name,
        notification_time.isoformat(),
    )
    return _MonitorNotificationSchedule(None, "", suppress=True)


def _parse_cleanup_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(
                value.strip().replace("Z", "+00:00"),
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_task_message_preview(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
    return "".join(parts).strip()


@dataclass(frozen=True)
class _TaskSessionCleanupParts:
    next_state: dict[str, Any]
    memory_state: dict[str, Any]
    memory_content: list[Any]
    task_runs: list[Any]
    task_messages: list[Any]


@dataclass(frozen=True)
class _TaskRunPruneResult:
    adjusted_runs: list[Any]
    retained_content: list[Any]
    removed_runs: int
    skipped_runs: int
    latest_preview: str
    latest_run_at: datetime | None
    aborted: bool = False


@dataclass(frozen=True)
class _TaskMessagePruneResult:
    kept_messages: list[Any]
    removed_messages: int
    latest_preview: str
    latest_run_at: datetime | None


def _cleanup_state_parts(state: dict[str, Any]) -> _TaskSessionCleanupParts:
    next_state = deepcopy(state)
    raw_runs = next_state.get("task_runs")
    task_runs = raw_runs if isinstance(raw_runs, list) else []
    agent_state = next_state.get("agent")
    if not isinstance(agent_state, dict):
        agent_state = {}
        next_state["agent"] = agent_state
    memory_state = agent_state.get("memory")
    if not isinstance(memory_state, dict):
        memory_state = {}
        agent_state["memory"] = memory_state
    content = memory_state.get("content")
    memory_content = content if isinstance(content, list) else []
    raw_messages = next_state.get(TASK_MESSAGES_STATE_KEY)
    task_messages = raw_messages if isinstance(raw_messages, list) else []
    return _TaskSessionCleanupParts(
        next_state=next_state,
        memory_state=memory_state,
        memory_content=memory_content,
        task_runs=task_runs,
        task_messages=task_messages,
    )


def _task_run_memory_range(
    raw_run: dict[str, Any],
    memory_content_length: int,
) -> tuple[int, int] | None:
    raw_start = raw_run.get("memory_start")
    raw_end = raw_run.get("memory_end")
    if not isinstance(raw_start, int) or not isinstance(raw_end, int):
        return None
    if raw_start < 0 or raw_end < raw_start:
        return None
    if raw_end > memory_content_length:
        return None
    return raw_start, raw_end


def _adjust_task_run_memory_ranges(
    kept_runs: list[Any],
    keep_mask: list[bool],
) -> list[Any]:
    retained_counts = [0]
    for kept in keep_mask:
        retained_counts.append(retained_counts[-1] + int(kept))

    adjusted_runs: list[Any] = []
    for raw_run in kept_runs:
        if not isinstance(raw_run, dict):
            adjusted_runs.append(raw_run)
            continue
        start = int(raw_run["memory_start"])
        end = int(raw_run["memory_end"])
        adjusted = dict(raw_run)
        adjusted["memory_start"] = retained_counts[start]
        adjusted["memory_end"] = retained_counts[end]
        adjusted_runs.append(adjusted)
    return adjusted_runs


def _prune_task_runs(
    task_runs: list[Any],
    memory_content: list[Any],
    cutoff: datetime,
) -> _TaskRunPruneResult:
    kept_runs: list[Any] = []
    removed_runs = 0
    skipped_runs = 0
    keep_mask = [True] * len(memory_content)
    latest_preview = ""
    latest_run_at: datetime | None = None

    for raw_run in task_runs:
        if not isinstance(raw_run, dict):
            kept_runs.append(raw_run)
            skipped_runs += 1
            continue
        memory_range = _task_run_memory_range(raw_run, len(memory_content))
        if memory_range is None:
            return _TaskRunPruneResult(
                adjusted_runs=[],
                retained_content=[],
                removed_runs=0,
                skipped_runs=1,
                latest_preview="",
                latest_run_at=None,
                aborted=True,
            )
        raw_start, raw_end = memory_range
        ended_at = _parse_cleanup_datetime(raw_run.get("ended_at"))
        if ended_at is not None and ended_at < cutoff:
            removed_runs += 1
            for index in range(raw_start, raw_end):
                keep_mask[index] = False
            continue
        kept_runs.append(raw_run)
        if ended_at is None:
            skipped_runs += 1
            continue
        if latest_run_at is None or ended_at >= latest_run_at:
            latest_run_at = ended_at
            latest_preview = str(raw_run.get("preview_text") or "")

    retained_content = [
        item for index, item in enumerate(memory_content) if keep_mask[index]
    ]
    return _TaskRunPruneResult(
        adjusted_runs=_adjust_task_run_memory_ranges(kept_runs, keep_mask),
        retained_content=retained_content,
        removed_runs=removed_runs,
        skipped_runs=skipped_runs,
        latest_preview=latest_preview,
        latest_run_at=latest_run_at,
    )


def _prune_task_messages(
    task_messages: list[Any],
    cutoff: datetime,
    latest_preview: str,
    latest_run_at: datetime | None,
) -> _TaskMessagePruneResult:
    kept_messages: list[Any] = []
    removed_messages = 0
    for raw_message in task_messages:
        if not isinstance(raw_message, dict):
            kept_messages.append(raw_message)
            continue
        timestamp = _parse_cleanup_datetime(raw_message.get("timestamp"))
        if timestamp is not None and timestamp < cutoff:
            removed_messages += 1
            continue
        kept_messages.append(raw_message)
        if timestamp is not None and (
            latest_run_at is None or timestamp >= latest_run_at
        ):
            latest_run_at = timestamp
            message_preview = _extract_task_message_preview(raw_message)
            if message_preview:
                latest_preview = message_preview

    return _TaskMessagePruneResult(
        kept_messages=kept_messages,
        removed_messages=removed_messages,
        latest_preview=latest_preview,
        latest_run_at=latest_run_at,
    )


def _aborted_cleanup_snapshot(
    state: dict[str, Any],
) -> _TaskSessionCleanupSnapshot:
    return _TaskSessionCleanupSnapshot(
        state=state,
        changed=False,
        has_result=False,
        latest_preview="",
        latest_run_at=None,
        skipped_runs=1,
    )


def _prune_task_session_state(
    state: dict[str, Any],
    cutoff: datetime,
) -> _TaskSessionCleanupSnapshot:
    parts = _cleanup_state_parts(state)
    run_result = _prune_task_runs(
        parts.task_runs,
        parts.memory_content,
        cutoff,
    )
    if run_result.aborted:
        return _aborted_cleanup_snapshot(state)

    message_result = _prune_task_messages(
        parts.task_messages,
        cutoff,
        run_result.latest_preview,
        run_result.latest_run_at,
    )

    parts.memory_state["content"] = run_result.retained_content
    parts.next_state["task_runs"] = run_result.adjusted_runs
    parts.next_state[TASK_MESSAGES_STATE_KEY] = message_result.kept_messages
    changed = (
        run_result.removed_runs > 0 or message_result.removed_messages > 0
    )
    return _TaskSessionCleanupSnapshot(
        state=parts.next_state,
        changed=changed,
        has_result=message_result.latest_run_at is not None,
        latest_preview=message_result.latest_preview[:10],
        latest_run_at=message_result.latest_run_at,
        removed_runs=run_result.removed_runs,
        removed_messages=message_result.removed_messages,
        skipped_runs=run_result.skipped_runs,
    )


@dataclass
class _Runtime:
    sem: asyncio.Semaphore


@dataclass
class _ExecutionOutcome:
    actual_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: int = 0
    exec_status: str = "success"
    error_message: str = ""
    output_preview: str = ""
    trace_id: str = ""
    input_snapshot: Optional[Dict[str, Any]] = None
    executor_leader: str = ""
    execution_meta: Optional[Dict[str, Any]] = None
    source_system_config: Any = None


@dataclass(frozen=True)
class _TaskSessionCleanupSnapshot:
    state: dict[str, Any]
    changed: bool
    has_result: bool
    latest_preview: str
    latest_run_at: datetime | None
    removed_runs: int = 0
    removed_messages: int = 0
    skipped_runs: int = 0


class CronManager:  # pylint: disable=too-many-public-methods
    """Manages scheduled cron jobs and heartbeat.

    Job scheduling is delegated to an external scheduler platform via
    SchedulerAdapter. This manager handles job persistence (CRUD on jobs.json),
    job execution dispatch, and state tracking.
    """

    def __init__(
        self,
        *,
        repo: BaseJobRepository,
        runner: Any,
        channel_manager: Any,
        chat_manager: Any = None,
        timezone: str = "UTC",  # pylint: disable=redefined-outer-name
        agent_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        scheduler_adapter: Optional[SchedulerAdapter] = None,
        source_system_config_service: Any = None,
        workflow_config_store: Any = None,
        continuous_governance_service: Any = None,
    ):
        self._repo = repo
        self._runner = runner
        self._channel_manager = channel_manager
        self._chat_manager = chat_manager
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._timezone = timezone
        self._source_system_config_service = source_system_config_service
        self._workflow_config_store = workflow_config_store
        self._continuous_governance_service = continuous_governance_service

        self._executor = CronExecutor(
            runner=runner,
            channel_manager=channel_manager,
        )
        self._workflow_executor = (
            WorkflowCronExecutor(
                config_store=workflow_config_store,
                session=getattr(runner, "session", None),
                channel_manager=channel_manager,
            )
            if workflow_config_store is not None
            else None
        )

        self._lock = asyncio.Lock()
        self._states: Dict[str, CronJobState] = {}
        self._rt: Dict[str, _Runtime] = {}
        self._started = False
        self._scheduler_adapter = scheduler_adapter or NoopSchedulerAdapter()
        self._prefetch_task: Optional[asyncio.Task] = None
        self._system_job_ids: Dict[str, str] = {}

        # Monitor sync client for dual-write
        self._monitor_sync_client: Optional[MonitorSyncClient] = None
        try:
            self._monitor_sync_client = get_monitor_sync_client()
        except Exception:  # pylint: disable=broad-except
            logger.debug("Monitor sync client not available")

    async def initialize(self) -> None:
        """初始化：加载 repo，注册系统任务，启动后台 prefetch 循环。"""
        async with self._lock:
            if self._started:
                return
            self._started = True
        self._load_system_job_ids()
        # 调度平台不可用时不应阻塞工作空间初始化
        try:
            await self._restore_external_job_ids()
        except Exception:
            logger.warning(
                "Failed to restore external job ids from scheduler",
                exc_info=True,
            )
        self._prefetch_task = asyncio.create_task(self._prefetch_loop())
        try:
            await self._register_system_jobs()
        except Exception:
            logger.warning(
                "Failed to register system jobs to external scheduler",
                exc_info=True,
            )

    # ----- read/state -----

    async def list_jobs(self) -> list[CronJobSpec]:
        return await self._repo.list_jobs()

    async def get_job(self, job_id: str) -> Optional[CronJobSpec]:
        return await self._repo.get_job(job_id)

    def get_state(self, job_id: str) -> CronJobState:
        return self._states.get(job_id, CronJobState())

    def _filter_jobs_by_user(
        self,
        jobs: list[CronJobSpec],
        user_id: str,
    ) -> list[CronJobSpec]:
        """Filter jobs that belong to the given user.

        Args:
            jobs: List of all jobs
            user_id: User's sapId

        Returns:
            List of agent/workflow jobs owned by this user.
        """
        user_jobs = []
        for job in jobs:
            if job.tenant_id and job.tenant_id == user_id:
                if job.task_type in {"agent", "workflow"}:
                    user_jobs.append(job)
        return user_jobs

    def _calculate_run_times_on_date(
        self,
        job: CronJobSpec,
        date: datetime,
    ) -> list[datetime]:
        """Calculate all scheduled run times for a job on a given date.

        Args:
            job: The cron job specification
            date: The date to calculate run times for

        Returns:
            List of scheduled run times on that date
        """
        cron_expr = job.schedule.cron if job.schedule else ""
        tz_name = job.schedule.timezone or self._timezone or "UTC"
        if not cron_expr:
            return []

        start_of_day = date.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if start_of_day.tzinfo is None:
            start_of_day = start_of_day.replace(tzinfo=timezone.utc)

        run_times: list[datetime] = []
        cursor = start_of_day
        safety_limit = 20

        for _ in range(safety_limit):
            try:
                next_run = compute_next_run_at(
                    cron_expr,
                    tz_name,
                    now=cursor,
                )
            except Exception:
                break

            if next_run.date() != date.date():
                break

            run_times.append(next_run)
            cursor = next_run + timedelta(seconds=1)

        return run_times

    def _determine_task_status(
        self,
        scheduled_time: datetime,
        state: CronJobState,
        date: datetime,
    ) -> str:
        """Determine task status based on schedule and execution state.

        Args:
            scheduled_time: The scheduled execution time
            state: The job's current state
            date: The query date

        Returns:
            Status string: "completed", "in_progress", "pending", "error", "cancelled"
        """
        now = datetime.now(timezone.utc)
        last_run = state.last_run_at

        if state.last_status == "running":
            return "in_progress"
        if scheduled_time > now:
            return "pending"
        if last_run and last_run.date() == date.date():
            if state.last_status == "success":
                return "completed"
            if state.last_status in ("error", "cancelled"):
                return state.last_status
            return "in_progress"
        return "pending"

    def _build_task_status_display(
        self,
        task_status: str,
        scheduled_time: Optional[datetime],
        last_run: Optional[datetime],
    ) -> tuple[str, str]:
        """Build status_text and time_info for display.

        Args:
            task_status: The task status
            scheduled_time: Scheduled execution time (UTC)
            last_run: Last actual run time (UTC)

        Returns:
            Tuple of (status_text, time_info)
        """
        status_map = {
            "completed": ("已完成", "已执行完成"),
            "in_progress": ("进行中", "任务执行中"),
            "pending": ("待开始", "等待执行"),
            "error": ("执行失败", "执行失败"),
            "cancelled": ("已取消", "任务已取消"),
        }

        if task_status not in status_map:
            return ("未知", "")

        status_text, default_info = status_map[task_status]

        # Convert UTC to local time for display
        def utc_to_local(dt: Optional[datetime]) -> Optional[datetime]:
            if dt is None:
                return None
            if dt.tzinfo is None:
                # Assume UTC if no timezone
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone()

        local_last_run = utc_to_local(last_run)
        local_scheduled_time = utc_to_local(scheduled_time)

        if task_status == "completed" and local_last_run:
            time_info = f"{local_last_run.strftime('%H:%M')}已完成"
        elif task_status == "in_progress" and local_last_run:
            time_info = f"{local_last_run.strftime('%H:%M')}已启动"
        elif task_status == "error" and local_last_run:
            time_info = f"{local_last_run.strftime('%H:%M')}执行失败"
        elif task_status == "pending" and local_scheduled_time:
            time_info = f"将于{local_scheduled_time.strftime('%H:%M')}执行"
        else:
            time_info = default_info

        return (status_text, time_info)

    async def query_user_tasks_by_date(
        self,
        user_id: str,
        date: datetime,
    ) -> list[Dict[str, Any]]:
        """Query all tasks for a user on a specific date.

        This method finds all jobs that belong to the user and calculates
        their scheduled run times and current status for the given date.

        Args:
            user_id: User's sapId/tenant_id
            date: The date to query tasks for

        Returns:
            List of task info dicts with job_id, task_name, status, etc.
        """
        jobs = await self.list_jobs()
        # pylint: disable=protected-access
        repo_path = getattr(self._repo, "_path", "unknown")
        # pylint: enable=protected-access
        logger.info(
            "query_user_tasks_by_date: list_jobs returned %d total jobs, "
            "user_id=%s, repo_path=%s",
            len(jobs),
            user_id,
            repo_path,
        )
        for job in jobs:
            logger.info(
                "query_user_tasks_by_date: job.id=%s, job.tenant_id=%s, "
                "job.name=%s, job.enabled=%s",
                job.id,
                job.tenant_id,
                job.name,
                job.enabled,
            )
        user_jobs = self._filter_jobs_by_user(jobs, user_id)
        logger.info(
            "query_user_tasks_by_date: filtered %d jobs for user_id=%s",
            len(user_jobs),
            user_id,
        )
        tasks: list[Dict[str, Any]] = []

        for job in user_jobs:
            if not job.enabled:
                continue

            state = self.get_state(job.id)
            # Use persisted last_run_at from job.meta if memory state is empty
            job_meta = job.meta or {}
            persisted_last_run = job_meta.get("task_last_scheduled_run_at")
            if persisted_last_run and not state.last_run_at:
                # Restore state from persisted meta (may be string from JSON)
                if isinstance(persisted_last_run, str):
                    # Parse ISO format datetime string
                    try:
                        persisted_last_run = datetime.fromisoformat(
                            persisted_last_run.replace("Z", "+00:00"),
                        )
                    except ValueError:
                        logger.warning(
                            "Failed to parse task_last_scheduled_run_at: %s",
                            persisted_last_run,
                        )
                        persisted_last_run = None
                state.last_run_at = persisted_last_run
                if job_meta.get("task_has_scheduled_result"):
                    state.last_status = "success"

            try:
                run_times = self._calculate_run_times_on_date(job, date)

                for scheduled_time in run_times:
                    task_status = self._determine_task_status(
                        scheduled_time,
                        state,
                        date,
                    )
                    status_text, time_info = self._build_task_status_display(
                        task_status,
                        scheduled_time,
                        state.last_run_at,
                    )

                    tasks.append(
                        {
                            "job_id": job.id,
                            "task_name": job.name,
                            "status": task_status,
                            "status_text": status_text,
                            "scheduled_time": scheduled_time,
                            "last_run_at": state.last_run_at,
                            "last_status": state.last_status,
                            "time_info": time_info,
                            "meta": job.meta or {},
                        },
                    )

            except Exception as e:
                logger.warning(
                    "Failed to calculate scheduled time for job %s: %s",
                    job.id,
                    repr(e),
                )
                tasks.append(
                    {
                        "job_id": job.id,
                        "task_name": job.name,
                        "status": "pending",
                        "status_text": "待开始",
                        "scheduled_time": None,
                        "last_run_at": state.last_run_at,
                        "last_status": state.last_status,
                        "time_info": "等待执行",
                        "meta": job.meta or {},
                    },
                )

        tasks.sort(
            key=lambda t: t.get("scheduled_time")
            or datetime.max.replace(tzinfo=timezone.utc),
        )
        return tasks

    # ----- write/control -----

    async def create_or_replace_job(self, spec: CronJobSpec) -> None:
        spec = await self._ensure_task_binding(spec)
        existing = await self._repo.get_job(spec.id)
        spec = await self._sync_job_to_external_scheduler(spec, existing)
        spec = await self._sync_batch_dispatch_parent_after_normal_sync(
            spec,
            existing=existing,
        )
        await self._persist_job_definition(spec)

    async def _persist_job_definition(self, spec: CronJobSpec) -> CronJobSpec:
        async with self._lock:
            changed, _, _ = await self._mutate_jobs_file_locked(
                lambda jobs_file: self._upsert_job_in_jobs_file(
                    jobs_file,
                    spec,
                ),
            )
        ext_id = (spec.meta or {}).get("external_job_id", "")
        if ext_id:
            st = self._states.get(spec.id, CronJobState())
            st.external_job_id = ext_id
            self._states[spec.id] = st

        await self._refresh_next_run_at(spec.id)

        # Sync to Monitor (async, non-blocking)
        if self._monitor_sync_client is not None:
            await self._monitor_sync_client.sync_job(
                spec,
                agent_id=self._agent_id or "default",
            )
        return spec

    @staticmethod
    def _normalize_batch_dispatch_offset_window_hours(value: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS
        return min(
            MAX_BROADCAST_OFFSET_WINDOW_HOURS,
            max(MIN_BROADCAST_OFFSET_WINDOW_HOURS, parsed),
        )

    def _resolve_batch_dispatch_schedule(
        self,
        spec: CronJobSpec,
        *,
        offset_window_hours: int,
    ) -> tuple[str, int, str]:
        cron = spec.schedule.cron if spec.schedule else ""
        if offset_window_hours == 0:
            return cron, 0, ""
        timezone_name = (
            spec.schedule.timezone
            if spec.schedule and spec.schedule.timezone
            else self._timezone or "UTC"
        )
        offset_minutes = offset_window_hours * 60
        shifted = shift_cron_expression(
            cron,
            timezone_name,
            offset_minutes=offset_minutes,
        )
        if shifted.error:
            return cron, 0, BATCH_DISPATCH_CRON_FALLBACK_WARNING
        return shifted.cron, shifted.offset_minutes, ""

    def _get_batch_dispatch_external_job_id(self, spec: CronJobSpec) -> str:
        meta = spec.meta or {}
        return str(meta.get(BATCH_DISPATCH_EXTERNAL_JOB_ID_META_KEY) or "")

    def _get_batch_dispatch_offset_window_hours(
        self,
        spec: CronJobSpec,
    ) -> int:
        meta = spec.meta or {}
        return self._normalize_batch_dispatch_offset_window_hours(
            meta.get(
                BATCH_DISPATCH_OFFSET_WINDOW_HOURS_META_KEY,
                DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS,
            ),
        )

    def _resolve_batch_dispatch_execution_model_params(
        self,
        spec: CronJobSpec,
    ) -> dict[str, str]:
        model_slot = spec.model_slot
        provider_id = str(getattr(model_slot, "provider_id", "") or "").strip()
        model_id = str(getattr(model_slot, "model", "") or "").strip()
        if provider_id and model_id:
            return {"provider_id": provider_id, "model_id": model_id}

        runtime_tenant_id = (
            self._get_job_runtime_tenant_id(spec)
            or self._tenant_id
            or spec.tenant_id
            or "default"
        )
        try:
            from ...providers.provider_manager import ProviderManager

            provider_tenant_id = (
                ProviderManager._resolve_effective_provider_tenant_id(
                    runtime_tenant_id,
                )
            )
            active_model = ProviderManager._read_active_model_from_root(
                ProviderManager._get_tenant_root_path(provider_tenant_id),
            )
        except Exception:  # pylint: disable=broad-except
            logger.debug(
                "Failed to resolve batch dispatch execution model: job=%s tenant=%s",
                spec.id,
                runtime_tenant_id,
                exc_info=True,
            )
            return {}

        provider_id = str(
            getattr(active_model, "provider_id", "") or "",
        ).strip()
        model_id = str(getattr(active_model, "model", "") or "").strip()
        if provider_id and model_id:
            return {"provider_id": provider_id, "model_id": model_id}
        return {}

    async def _sync_batch_dispatch_scheduler_job(
        self,
        spec: CronJobSpec,
        *,
        offset_window_hours: int,
    ) -> CronJobSpec:
        if isinstance(self._scheduler_adapter, NoopSchedulerAdapter):
            raise RuntimeError("External scheduler is not configured")

        offset_window_hours = (
            self._normalize_batch_dispatch_offset_window_hours(
                offset_window_hours,
            )
        )
        batch_cron, offset_minutes, warning = (
            self._resolve_batch_dispatch_schedule(
                spec,
                offset_window_hours=offset_window_hours,
            )
        )
        meta = dict(spec.meta or {})
        batch_ext_id = str(
            meta.get(BATCH_DISPATCH_EXTERNAL_JOB_ID_META_KEY) or "",
        )
        tenant_id, source_id = self._get_external_scheduler_business_identity(
            spec,
        )
        extra_job_params = {
            BATCH_DISPATCH_OFFSET_MINUTES_META_KEY: offset_minutes,
            BATCH_DISPATCH_PARENT_CRON_META_KEY: (
                spec.schedule.cron if spec.schedule else ""
            ),
            "batch_dispatch_original_timezone": (
                spec.schedule.timezone if spec.schedule else ""
            ),
        }
        swe_server_domain = os.environ.get("SWE_SERVER_DOMAIN", "").strip()
        if swe_server_domain:
            extra_job_params[BATCH_DISPATCH_SWE_SERVER_DOMAIN_PARAM] = (
                swe_server_domain
            )
        extra_job_params.update(
            self._resolve_batch_dispatch_execution_model_params(spec),
        )
        callback_url = self._build_scheduler_callback_url()
        job_name = f"{BATCH_DISPATCH_JOB_NAME_PREFIX}{spec.name}"
        if batch_ext_id:
            await self._scheduler_adapter.update_job(
                external_id=batch_ext_id,
                tenant_id=tenant_id,
                source_id=source_id,
                agent_id=self._agent_id or "",
                task_type="job",
                job_id=spec.id,
                job_name=job_name,
                cron=batch_cron,
                callback_url=callback_url,
                extra_job_params=extra_job_params,
            )
        else:
            batch_ext_id = await self._scheduler_adapter.register_job(
                tenant_id=tenant_id,
                source_id=source_id,
                agent_id=self._agent_id or "",
                task_type="job",
                job_id=spec.id,
                job_name=job_name,
                cron=batch_cron,
                callback_url=callback_url,
                extra_job_params=extra_job_params,
            )
            if not batch_ext_id:
                raise RuntimeError(
                    "External scheduler did not return batch job id "
                    f"for {spec.id}",
                )

        # This is a batch wakeup timer, not the parent's own execution switch.
        # Scheduler's durable run state gates new batches and queued work.
        await self._scheduler_adapter.resume_job(batch_ext_id)

        meta[BROADCAST_DISPATCH_INTENTS_ENABLED_META_KEY] = True
        meta[BATCH_DISPATCH_EXTERNAL_JOB_ID_META_KEY] = batch_ext_id
        meta[BATCH_DISPATCH_OFFSET_WINDOW_HOURS_META_KEY] = offset_window_hours
        meta[BATCH_DISPATCH_OFFSET_MINUTES_META_KEY] = offset_minutes
        meta[BATCH_DISPATCH_CRON_META_KEY] = batch_cron
        meta[BATCH_DISPATCH_PARENT_CRON_META_KEY] = (
            spec.schedule.cron if spec.schedule else ""
        )
        if warning:
            meta[BATCH_DISPATCH_CRON_WARNING_META_KEY] = warning
        else:
            meta.pop(BATCH_DISPATCH_CRON_WARNING_META_KEY, None)
        return spec.model_copy(update={"meta": meta})

    async def _sync_batch_dispatch_parent_after_normal_sync(
        self,
        spec: CronJobSpec,
        *,
        existing: Optional[CronJobSpec] = None,
    ) -> CronJobSpec:
        if not is_batch_dispatch_parent(spec):
            return spec
        normal_ext_id = self._get_existing_external_job_id(spec, existing)
        if normal_ext_id:
            await self._scheduler_adapter.pause_job(normal_ext_id)
        return await self._sync_batch_dispatch_scheduler_job(
            spec,
            offset_window_hours=self._get_batch_dispatch_offset_window_hours(
                spec,
            ),
        )

    async def enable_batch_dispatch_for_parent(
        self,
        job_id: str,
        *,
        offset_window_hours: int = DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS,
    ) -> CronJobSpec:
        if not dispatch_intents_runtime_enabled():
            raise RuntimeError("batch dispatch runtime is disabled")
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if (job.meta or {}).get(BROADCAST_SOURCE_JOB_ID_META_KEY):
            raise RuntimeError(
                "batch dispatch cannot be enabled from a broadcast child",
            )

        meta = dict(job.meta or {})
        meta[BROADCAST_DISPATCH_INTENTS_ENABLED_META_KEY] = True
        meta[BATCH_DISPATCH_OFFSET_WINDOW_HOURS_META_KEY] = (
            self._normalize_batch_dispatch_offset_window_hours(
                offset_window_hours,
            )
        )
        updated = job.model_copy(update={"meta": meta})
        updated = await self._sync_job_to_external_scheduler(
            updated,
            existing=job,
        )
        normal_ext_id = self._get_existing_external_job_id(updated, job)
        if normal_ext_id:
            await self._scheduler_adapter.pause_job(normal_ext_id)
        updated = await self._sync_batch_dispatch_scheduler_job(
            updated,
            offset_window_hours=meta[
                BATCH_DISPATCH_OFFSET_WINDOW_HOURS_META_KEY
            ],
        )
        logger.info(
            "Enabled batch dispatch for cron parent: job=%s normal_ext_id=%s batch_ext_id=%s offset_minutes=%s",
            updated.id,
            normal_ext_id,
            (updated.meta or {}).get(
                BATCH_DISPATCH_EXTERNAL_JOB_ID_META_KEY,
                "",
            ),
            (updated.meta or {}).get(
                BATCH_DISPATCH_OFFSET_MINUTES_META_KEY,
                0,
            ),
        )
        saved = await self._persist_job_definition(updated)
        from .batch_run_state_client import initialize_run_state

        await initialize_run_state(
            saved,
            self._monitor_sync_client,
            self._agent_id or "default",
        )
        return saved

    async def disable_batch_dispatch_for_parent(
        self,
        job_id: str,
    ) -> CronJobSpec:
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if (job.meta or {}).get(BROADCAST_SOURCE_JOB_ID_META_KEY):
            raise RuntimeError(
                "batch dispatch cannot be disabled from a broadcast child",
            )

        meta = dict(job.meta or {})
        batch_ext_id = str(
            meta.get(BATCH_DISPATCH_EXTERNAL_JOB_ID_META_KEY) or "",
        )
        for key in (
            BROADCAST_DISPATCH_INTENTS_ENABLED_META_KEY,
            BATCH_DISPATCH_OFFSET_WINDOW_HOURS_META_KEY,
            BATCH_DISPATCH_OFFSET_MINUTES_META_KEY,
            BATCH_DISPATCH_CRON_META_KEY,
            BATCH_DISPATCH_CRON_WARNING_META_KEY,
            BATCH_DISPATCH_PARENT_CRON_META_KEY,
        ):
            meta.pop(key, None)
        updated = job.model_copy(update={"meta": meta})
        updated = await self._sync_job_to_external_scheduler(
            updated,
            existing=job,
        )
        if batch_ext_id and not isinstance(
            self._scheduler_adapter,
            NoopSchedulerAdapter,
        ):
            await self._scheduler_adapter.pause_job(batch_ext_id)
        logger.info(
            "Disabled batch dispatch for cron parent: job=%s batch_ext_id=%s",
            updated.id,
            batch_ext_id,
        )
        return await self._persist_job_definition(updated)

    def _get_existing_external_job_id(
        self,
        spec: CronJobSpec,
        existing: Optional[CronJobSpec] = None,
    ) -> str:
        state_ext_id = self._states.get(
            spec.id,
            CronJobState(),
        ).external_job_id
        if state_ext_id:
            return state_ext_id
        spec_ext_id = (spec.meta or {}).get("external_job_id", "")
        if spec_ext_id:
            return str(spec_ext_id)
        if existing is not None:
            existing_ext_id = (existing.meta or {}).get("external_job_id", "")
            if existing_ext_id:
                return str(existing_ext_id)
        return ""

    def _get_external_scheduler_tenant_id(self, spec: CronJobSpec) -> str:
        # 外部调度归属必须跟当前运行时租户一致，避免旧任务里的 tenant_id 污染回调路由。
        return self._tenant_id or spec.tenant_id or ""

    def _get_external_scheduler_business_identity(
        self,
        spec: Optional[CronJobSpec] = None,
    ) -> tuple[str, str]:
        """解析写入调度平台展示字段和 jobParam 的业务身份。"""
        if spec is not None:
            tenant_id = spec.tenant_id or ""
            source_id = spec.source_id or ""
            if tenant_id:
                tenant_id, source_id, _ = resolve_runtime_identity(
                    tenant_id,
                    source_id or None,
                )
                return tenant_id or spec.tenant_id or "", source_id or ""
        tenant_id, source_id, _ = resolve_runtime_identity(self._tenant_id)
        return tenant_id or self._tenant_id or "", source_id or ""

    async def _suppress_external_scheduler_for_batch_child(
        self,
        spec: CronJobSpec,
        existing: Optional[CronJobSpec] = None,
    ) -> CronJobSpec:
        ext_id = self._get_existing_external_job_id(spec, existing)
        meta = dict(spec.meta or {})
        if not ext_id:
            meta.pop("external_job_id", None)
            logger.info(
                "Skip external scheduler registration for batch dispatch child: job=%s parent=%s",
                spec.id,
                meta.get(BROADCAST_SOURCE_JOB_ID_META_KEY),
            )
            return spec.model_copy(update={"meta": meta})

        await self._scheduler_adapter.pause_job(ext_id)
        meta["external_job_id"] = ext_id
        logger.info(
            "Paused external scheduler for batch dispatch child: job=%s ext_id=%s parent=%s",
            spec.id,
            ext_id,
            meta.get(BROADCAST_SOURCE_JOB_ID_META_KEY),
        )
        return spec.model_copy(update={"meta": meta})

    @staticmethod
    def _get_job_runtime_tenant_id(spec: CronJobSpec) -> str | None:
        """解析任务执行时实际使用的租户隔离键。"""
        if spec.scope_id is not None:
            return canonicalize_scope_id(spec.scope_id)
        return (
            resolve_scope_id(spec.tenant_id, spec.source_id) or spec.tenant_id
        )

    @staticmethod
    def _with_execution_source_identity(
        spec: CronJobSpec,
        source_id: str | None,
    ) -> CronJobSpec:
        """Use callback source identity for legacy jobs that lack it."""
        if not source_id or spec.source_id or spec.scope_id:
            return spec
        tenant_id, resolved_source_id, scope_id = resolve_runtime_identity(
            spec.tenant_id,
            source_id,
        )
        return spec.model_copy(
            update={
                "tenant_id": tenant_id or spec.tenant_id,
                "source_id": resolved_source_id or source_id,
                "scope_id": scope_id,
            },
        )

    def _resolve_scheduled_run_source_id(
        self,
        source_id: str | None,
        scope_id: str | None,
    ) -> str | None:
        """优先用显式 source_id，缺失时从 runtime scope 解码。"""
        if source_id:
            return source_id
        if scope_id:
            _, resolved_source_id, _ = resolve_runtime_identity(scope_id)
            if resolved_source_id:
                return resolved_source_id
        return None

    async def _resolve_scheduled_run_source_system_config(
        self,
        *,
        source_id: str | None,
        scope_id: str | None,
        boundary_name: str,
    ) -> Any | None:
        """按 Scheduled Run Boundary 规则解析 source system config。"""
        resolved_source_id = self._resolve_scheduled_run_source_id(
            source_id,
            scope_id,
        )
        if resolved_source_id is None:
            logger.warning(
                "Scheduled run source config binding skipped: boundary=%s "
                "reason=no_source_identity",
                boundary_name,
            )
            return None

        if self._source_system_config_service is None:
            logger.debug(
                "Scheduled run source config binding skipped: boundary=%s "
                "source_id=%s reason=no_service",
                boundary_name,
                resolved_source_id,
            )
            return None

        return await self._source_system_config_service.resolve_config(
            resolved_source_id,
        )

    @contextmanager
    def _bind_scheduled_run_source_system_config(
        self,
        source_system_config: Any | None,
    ):
        """Bind resolved source config, or explicitly clear inherited request config."""
        if source_system_config is not None:
            with bind_source_system_config(source_system_config):
                yield
            return

        token = set_current_source_system_config(cast(Any, None))
        try:
            yield
        finally:
            reset_current_source_system_config(token)

    async def _resolve_task_session_cleanup_config(self):
        tenant_id, source_id = self._get_external_scheduler_business_identity()
        del tenant_id
        if self._source_system_config_service is None or not source_id:
            return resolve_cron_task_session_cleanup_config(None)
        try:
            source_config = (
                await self._source_system_config_service.resolve_config(
                    source_id,
                )
            )
        except Exception:
            logger.warning(
                "Failed to resolve task session cleanup source config",
                exc_info=True,
            )
            return resolve_cron_task_session_cleanup_config(None)
        return resolve_cron_task_session_cleanup_config(source_config)

    async def _sync_job_to_external_scheduler(
        self,
        spec: CronJobSpec,
        existing: Optional[CronJobSpec] = None,
    ) -> CronJobSpec:
        """先同步外部调度平台，再返回带 external_job_id 的任务定义。"""
        if isinstance(self._scheduler_adapter, NoopSchedulerAdapter):
            return spec

        if is_batch_dispatch_managed_broadcast_child(spec):
            return await self._suppress_external_scheduler_for_batch_child(
                spec,
                existing,
            )

        callback_url = self._build_callback_url("job", spec.id)
        ext_id = self._get_existing_external_job_id(spec, existing)
        tenant_id, source_id = self._get_external_scheduler_business_identity(
            spec,
        )
        if ext_id:
            await self._scheduler_adapter.update_job(
                external_id=ext_id,
                tenant_id=tenant_id,
                source_id=source_id,
                agent_id=self._agent_id or "",
                task_type="job",
                job_id=spec.id,
                job_name=spec.name,
                cron=spec.schedule.cron if spec.schedule else "",
                callback_url=callback_url,
            )
        else:
            ext_id = await self._scheduler_adapter.register_job(
                tenant_id=tenant_id,
                source_id=source_id,
                agent_id=self._agent_id or "",
                task_type="job",
                job_id=spec.id,
                job_name=spec.name,
                cron=spec.schedule.cron if spec.schedule else "",
                callback_url=callback_url,
            )
            if not ext_id:
                raise RuntimeError(
                    f"External scheduler did not return job id for {spec.id}",
                )

        if spec.enabled:
            await self._scheduler_adapter.resume_job(ext_id)
        else:
            await self._scheduler_adapter.pause_job(ext_id)

        meta = dict(spec.meta or {})
        meta["external_job_id"] = ext_id
        return spec.model_copy(update={"meta": meta})

    async def register_missing_external_jobs(self) -> dict[str, Any]:
        """补注册当前 Agent 下所有缺少 external_job_id 的任务。"""
        if isinstance(self._scheduler_adapter, NoopSchedulerAdapter):
            raise RuntimeError("External scheduler is not configured")

        result: dict[str, Any] = {
            "tenant_id": self._tenant_id,
            "agent_id": self._agent_id,
            "total": 0,
            "registered": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }
        for job in await self._repo.list_jobs():
            result["total"] += 1
            had_external_id = bool(self._get_existing_external_job_id(job))
            if (
                had_external_id
                and not is_batch_dispatch_managed_broadcast_child(
                    job,
                )
            ):
                result["skipped"] += 1
                continue
            try:
                if is_batch_dispatch_managed_broadcast_child(job):
                    synced = await self._sync_job_to_external_scheduler(
                        job,
                        existing=job,
                    )
                    ext_id = (synced.meta or {}).get("external_job_id", "")
                    if not ext_id:
                        result["skipped"] += 1
                        continue
                    await self._persist_external_job_binding(job.id, ext_id)
                    st = self._states.get(job.id, CronJobState())
                    st.external_job_id = ext_id
                    self._states[job.id] = st
                    result["updated"] += 1
                    continue

                synced = await self._sync_job_to_external_scheduler(job)
                ext_id = (synced.meta or {}).get("external_job_id", "")
                await self._persist_external_job_binding(
                    job.id,
                    ext_id,
                )
                st = self._states.get(job.id, CronJobState())
                st.external_job_id = ext_id
                self._states[job.id] = st
                result["registered"] += 1
            except Exception as exc:  # pylint: disable=broad-except
                result["failed"] += 1
                result["errors"].append(
                    {
                        "job_id": job.id,
                        "job_name": job.name,
                        "error": str(exc),
                    },
                )
                logger.warning(
                    "Failed to register missing external job %s",
                    job.id,
                    exc_info=True,
                )
        return result

    async def refresh_external_jobs(self) -> dict[str, Any]:
        """按当前代码规则刷新当前 Agent 的外部调度平台任务。"""
        if isinstance(self._scheduler_adapter, NoopSchedulerAdapter):
            raise RuntimeError("External scheduler is not configured")

        result: dict[str, Any] = {
            "tenant_id": self._tenant_id,
            "agent_id": self._agent_id,
            "total": 0,
            "registered": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }
        for job in await self._repo.list_jobs():
            result["total"] += 1
            had_external_id = bool(self._get_existing_external_job_id(job))
            try:
                synced = await self._sync_job_to_external_scheduler(
                    job,
                    existing=job,
                )
                synced = (
                    await self._sync_batch_dispatch_parent_after_normal_sync(
                        synced,
                        existing=job,
                    )
                )
                ext_id = (synced.meta or {}).get("external_job_id", "")
                if is_batch_dispatch_parent(synced):
                    await self._persist_job_definition(synced)
                elif ext_id:
                    await self._persist_external_job_binding(job.id, ext_id)
                    st = self._states.get(job.id, CronJobState())
                    st.external_job_id = ext_id
                    self._states[job.id] = st
                if had_external_id:
                    result["updated"] += 1
                else:
                    result["registered"] += 1
            except Exception as exc:  # pylint: disable=broad-except
                result["failed"] += 1
                result["errors"].append(
                    {
                        "job_id": job.id,
                        "job_name": job.name,
                        "error": str(exc),
                    },
                )
                logger.warning(
                    "Failed to refresh external job %s",
                    job.id,
                    exc_info=True,
                )
        return result

    async def _repair_external_scheduler_tenant(
        self,
        job: CronJobSpec,
    ) -> bool:
        if not self._tenant_id or job.tenant_id == self._tenant_id:
            return False
        synced = await self._sync_job_to_external_scheduler(job)
        ext_id = (synced.meta or {}).get("external_job_id", "")
        await self._persist_external_job_binding(
            job.id,
            ext_id,
            self._tenant_id,
        )
        return True

    async def _ensure_persisted_task_binding(
        self,
        spec: CronJobSpec,
    ) -> CronJobSpec:
        bound = await self._ensure_task_binding(spec)
        if bound == spec:
            return spec
        await self.create_or_replace_job(bound)
        saved = await self.get_job(spec.id)
        return saved or bound

    async def delete_job(self, job_id: str) -> bool:
        # 先拿到 job 数据（删除前），用于调用外部调度平台
        job_before_delete = await self._repo.get_job(job_id)
        if job_before_delete is None:
            return False

        # 先通知外部调度平台改名并停止
        ext_id = self._get_existing_external_job_id(job_before_delete)
        if ext_id and self._scheduler_adapter is not None:
            try:
                callback_url = self._build_callback_url(
                    "job",
                    job_id,
                )
                cron = (
                    job_before_delete.schedule.cron
                    if job_before_delete.schedule
                    and job_before_delete.schedule.cron
                    else "0 0 1 1 *"
                )
                await self._scheduler_adapter.delete_job(
                    ext_id,
                    tenant_id=job_before_delete.tenant_id
                    or self._tenant_id
                    or "",
                    agent_id=self._agent_id or "",
                    task_type="job",
                    job_id=job_id,
                    job_name=job_before_delete.name,
                    cron=cron,
                    callback_url=callback_url,
                )
            except Exception:
                logger.warning(
                    "Failed to delete job %s from external scheduler",
                    job_id,
                    exc_info=True,
                )

        # 再从本地删除
        async with self._lock:
            changed, deleted_job, _ = await self._mutate_jobs_file_locked(
                lambda jobs_file: self._delete_job_in_jobs_file(
                    jobs_file,
                    job_id,
                ),
            )
            self._states.pop(job_id, None)
            self._rt.pop(job_id, None)

            task_chat_id = str(
                (job_before_delete.meta or {}).get("task_chat_id") or "",
            )
            if task_chat_id and self._chat_manager is not None:
                try:
                    await self._chat_manager.delete_chats([task_chat_id])
                except Exception:  # pragma: no cover - defensive cleanup path
                    logger.warning(
                        "Failed to delete task chat after cron deletion: "
                        "job_id=%s chat_id=%s",
                        job_id,
                        task_chat_id,
                        exc_info=True,
                    )

            # Sync to Monitor (async, non-blocking)
            if self._monitor_sync_client is not None:
                await self._monitor_sync_client.delete_job(job_id)

            return (
                deleted_job is not None if changed else deleted_job is not None
            )

    async def pause_job(self, job_id: str) -> bool:
        """Pause a job - disables execution and persists to repository.

        Args:
            job_id: The job ID to pause.

        Returns:
            True if job was found and paused, False otherwise.
        """
        async with self._lock:
            _, job, _ = await self._mutate_jobs_file_locked(
                lambda jobs_file: self._set_job_paused_in_jobs_file(
                    jobs_file,
                    job_id,
                    reason=MANUAL_PAUSE_REASON,
                ),
            )
            if job is None:
                return False

            # Pause on external scheduler
            ext_id = self._states.get(job_id, CronJobState()).external_job_id
            if ext_id and self._scheduler_adapter:
                await self._scheduler_adapter.pause_job(ext_id)
            elif not ext_id:
                logger.warning(
                    "pause_job: no external_job_id for %s, skipping external sync",
                    job_id,
                )

            # Sync to Monitor (async, non-blocking)
            if self._monitor_sync_client is not None:
                await self._monitor_sync_client.sync_job(
                    job,
                    agent_id=self._agent_id or "default",
                )

            return True

    async def resume_job(self, job_id: str) -> bool:
        """Resume a paused job - enables execution and persists to repository.

        This also marks all unread execution records as read, since the user
        has acknowledged the task status by resuming it.

        Args:
            job_id: The job ID to resume.

        Returns:
            True if job was found and resumed, False otherwise.
        """
        async with self._lock:
            _, job, _ = await self._mutate_jobs_file_locked(
                lambda jobs_file: self._set_job_resumed_in_jobs_file(
                    jobs_file,
                    job_id,
                ),
            )
            if job is None:
                return False

            # Resume on external scheduler
            ext_id = self._states.get(job_id, CronJobState()).external_job_id
            if ext_id and self._scheduler_adapter:
                if is_batch_dispatch_parent(
                    job,
                ) or is_batch_dispatch_managed_broadcast_child(job):
                    await self._scheduler_adapter.pause_job(ext_id)
                else:
                    await self._scheduler_adapter.resume_job(ext_id)
            elif not ext_id:
                logger.warning(
                    "resume_job: no external_job_id for %s, skipping external sync",
                    job_id,
                )

            # Sync to Monitor (async, non-blocking)
            if self._monitor_sync_client is not None:
                await self._monitor_sync_client.sync_job(
                    job,
                    agent_id=self._agent_id or "default",
                )
                # 恢复任务时同时标记历史未读记录为已读
                await self._monitor_sync_client.mark_job_as_read(job_id)

            return True

    async def run_job(
        self,
        job_id: str,
        is_manual: bool = True,
        source_id: str | None = None,
        dispatch_meta: Optional[Dict[str, Any]] = None,
    ) -> bool | None:
        """Trigger a job to run in the background (fire-and-forget).

        This is called either by:
        - User manual trigger (is_manual=True)
        - External scheduler callback (is_manual=False)

        Raises KeyError if the job does not exist.
        The actual execution happens asynchronously; errors are logged
        and reflected in the job state but NOT propagated to the caller.
        """
        job = await self._repo.get_job(job_id)
        if not job:
            raise KeyError(f"Job not found: {job_id}")
        if not job.enabled:
            logger.debug("Job %s is disabled, skipping run", job_id)
            return False
        dispatch_meta = self._prepare_run_dispatch_meta(
            job,
            is_manual,
            dispatch_meta,
        )
        job = await self._ensure_persisted_task_binding(job)
        dispatch_meta = self._bind_run_dispatch_headers(
            job,
            is_manual,
            dispatch_meta,
        )
        logger.info(
            "cron run_job: job_id=%s channel=%s task_type=%s is_manual=%s "
            "target_user_id=%s target_session_id=%s",
            job_id,
            job.dispatch.channel,
            job.task_type,
            is_manual,
            (job.dispatch.target.user_id or "")[:40],
            (job.dispatch.target.session_id or "")[:40],
        )
        st = self._states.get(job_id, CronJobState())
        st.last_status = "running"
        st.last_error = None
        self._states[job_id] = st
        with bind_llm_workload(LLM_WORKLOAD_CRON):
            task = asyncio.create_task(
                self._execute_once(
                    job,
                    is_manual=is_manual,
                    source_id=source_id,
                    dispatch_meta=dispatch_meta,
                ),
                name=f"cron-run-{job_id}",
            )
        task.add_done_callback(lambda t: self._task_done_cb(t, job))

    @staticmethod
    def _prepare_run_dispatch_meta(
        job: CronJobSpec,
        is_manual: bool,
        dispatch_meta: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        prepared = dict(dispatch_meta or {})
        prepared["cron_is_manual"] = is_manual
        if (
            not is_manual
            and job.task_type in {"agent", "text", "workflow"}
            and not _has_cron_execution_identity(prepared)
        ):
            raise RuntimeError(
                "cron scheduled delivery requires execution identity",
            )
        return prepared

    @staticmethod
    def _merge_passthrough_headers(
        persisted_headers: Any,
        execution_headers: Any,
        job_id: str,
    ) -> Dict[str, Any]:
        headers = (
            dict(persisted_headers)
            if isinstance(persisted_headers, dict)
            else {}
        )
        if isinstance(execution_headers, dict):
            for name, value in execution_headers.items():
                for existing_name in list(headers):
                    if str(existing_name).casefold() == str(name).casefold():
                        del headers[existing_name]
                headers[name] = value
        for name in list(headers):
            if str(name).casefold() == "cron_job_id":
                del headers[name]
        headers["cron_job_id"] = job_id
        return headers

    def _bind_run_dispatch_headers(
        self,
        job: CronJobSpec,
        is_manual: bool,
        dispatch_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        bound = dict(dispatch_meta)
        bound[PASSTHROUGH_HEADERS_META_KEY] = self._merge_passthrough_headers(
            (job.dispatch.meta or {}).get(PASSTHROUGH_HEADERS_META_KEY),
            bound.get(PASSTHROUGH_HEADERS_META_KEY),
            job.id,
        )
        if is_manual:
            bound["cron_execution_key"] = f"manual:{job.id}:{uuid4()}"
        return bound

    async def mark_task_read(self, job_id: str, user_id: str) -> bool:
        async with self._lock:
            _, found, _ = await self._mutate_jobs_file_locked(
                lambda jobs_file: self._mark_task_read_in_jobs_file(
                    jobs_file,
                    job_id,
                    user_id,
                ),
            )
            # 同步标记已读到 Monitor 服务
            if found and self._monitor_sync_client is not None:
                await self._monitor_sync_client.mark_job_as_read(job_id)
            return found

    def build_task_view(
        self,
        spec: CronJobSpec,
        user_id: Optional[str],
    ) -> CronTaskView:
        return build_cron_task_view(spec, self.get_state(spec.id), user_id)

    # ----- callbacks -----

    def _task_done_cb(self, task: asyncio.Task, job: CronJobSpec) -> None:
        """Suppress and log exceptions from fire-and-forget tasks.

        On failure, push an error message to the console push store so
        the frontend can display it.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "cron background task %s failed: %s",
                task.get_name(),
                repr(exc),
            )
            execution_meta = getattr(exc, "cron_execution_meta", None)
            if isinstance(execution_meta, dict) and execution_meta.get(
                "terminal_notification_sent",
            ):
                return
            # Push error to the console for the frontend to display
            session_id = job.dispatch.target.session_id
            if session_id:
                error_text = f"❌ Cron job [{job.name}] failed: {exc}"

                async def _push_error() -> None:
                    await push_store_append(
                        session_id,
                        error_text,
                        tenant_id=job.tenant_id,
                    )

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(_push_error())
                else:
                    loop.create_task(_push_error())

    # ----- internal -----

    async def _mutate_jobs_file_locked(
        self,
        mutator: Callable[[JobsFile], tuple[bool, _T]],
    ) -> tuple[bool, _T, int]:
        """在文件级别加锁下修改 jobs.json。

        调用者必须已持有 self._lock。
        """
        mutate = getattr(self._repo, "mutate_jobs_file", None)
        if callable(mutate):
            changed, result = await mutate(mutator)
            return changed, result, 0
        jobs_file = await self._repo.load()
        changed, result = mutator(jobs_file)
        if not changed:
            return False, result, 0
        await self._repo.save(jobs_file)
        return True, result, 0

    def _upsert_job_in_jobs_file(
        self,
        jobs_file: JobsFile,
        spec: CronJobSpec,
    ) -> tuple[bool, CronJobSpec]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id == spec.id:
                if job == spec:
                    return False, spec
                jobs_file.jobs[index] = spec
                return True, spec
        jobs_file.jobs.append(spec)
        return True, spec

    def _mark_task_read_in_jobs_file(
        self,
        jobs_file: JobsFile,
        job_id: str,
        user_id: str,
    ) -> tuple[bool, bool]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id != job_id:
                continue
            creator = (job.meta or {}).get("creator_user_id")
            if creator != user_id:
                return False, False
            meta = dict(job.meta or {})
            if meta.get("pause_reason"):
                return False, True
            unread_count = int(meta.get("task_unread_execution_count", 0) or 0)
            if unread_count == 0:
                return False, True
            meta["task_unread_execution_count"] = 0
            jobs_file.jobs[index] = job.model_copy(update={"meta": meta})
            return True, True
        return False, False

    def _delete_job_in_jobs_file(
        self,
        jobs_file: JobsFile,
        job_id: str,
    ) -> tuple[bool, Optional[CronJobSpec]]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id != job_id:
                continue
            del jobs_file.jobs[index]
            return True, job
        return False, None

    def _set_job_enabled_in_jobs_file(
        self,
        jobs_file: JobsFile,
        job_id: str,
        *,
        enabled: bool,
    ) -> tuple[bool, Optional[CronJobSpec]]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id != job_id:
                continue
            if job.enabled == enabled:
                return False, job
            updated = job.model_copy(update={"enabled": enabled})
            jobs_file.jobs[index] = updated
            return True, updated
        return False, None

    def _set_job_paused_in_jobs_file(
        self,
        jobs_file: JobsFile,
        job_id: str,
        *,
        reason: str,
        auto_paused_at: Optional[datetime] = None,
        unread_count_at_pause: Optional[int] = None,
    ) -> tuple[bool, Optional[CronJobSpec]]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id != job_id:
                continue
            meta = dict(job.meta or {})
            changed = False
            if job.enabled:
                changed = True
            if meta.get("pause_reason") != reason:
                meta["pause_reason"] = reason
                changed = True
            if (
                auto_paused_at is not None
                and meta.get("auto_paused_at") != auto_paused_at
            ):
                meta["auto_paused_at"] = auto_paused_at
                changed = True
            if (
                unread_count_at_pause is not None
                and meta.get("unread_count_at_pause") != unread_count_at_pause
            ):
                meta["unread_count_at_pause"] = unread_count_at_pause
                changed = True
            if not changed:
                return False, job
            updated = job.model_copy(
                update={
                    "enabled": False,
                    "meta": meta,
                },
            )
            jobs_file.jobs[index] = updated
            return True, updated
        return False, None

    def _set_job_resumed_in_jobs_file(
        self,
        jobs_file: JobsFile,
        job_id: str,
    ) -> tuple[bool, Optional[CronJobSpec]]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id != job_id:
                continue
            meta = dict(job.meta or {})
            changed = False
            if not job.enabled:
                changed = True
            if int(meta.get("task_unread_execution_count", 0) or 0) != 0:
                meta["task_unread_execution_count"] = 0
                changed = True
            for key in (
                "pause_reason",
                "auto_paused_at",
                "unread_count_at_pause",
            ):
                if key in meta:
                    meta.pop(key, None)
                    changed = True
            if not changed:
                return False, job
            updated = job.model_copy(
                update={
                    "enabled": True,
                    "meta": meta,
                },
            )
            jobs_file.jobs[index] = updated
            return True, updated
        return False, None

    def _disable_invalid_jobs_in_jobs_file(
        self,
        jobs_file: JobsFile,
        job_ids: set[str],
    ) -> tuple[bool, list[str]]:
        changed = False
        disabled_ids: list[str] = []
        for index, job in enumerate(jobs_file.jobs):
            if job.id not in job_ids or not job.enabled:
                continue
            jobs_file.jobs[index] = job.model_copy(update={"enabled": False})
            disabled_ids.append(job.id)
            changed = True
        return changed, disabled_ids

    async def _ensure_task_binding(self, spec: CronJobSpec) -> CronJobSpec:
        creator_user_id_raw = (spec.meta or {}).get("creator_user_id")
        if not self._should_bind_task_session(spec, creator_user_id_raw):
            return spec

        # 类型检查通过后，显式转换为 str 类型
        creator_user_id = str(creator_user_id_raw)

        # 从已持久化的任务中合并执行状态元数据，避免 PUT 等操作覆盖运行时状态
        existing_meta = await self._load_existing_task_meta(spec.id)
        meta = self._merge_task_binding_meta(spec.meta or {}, existing_meta)
        # 已持久化的执行状态优先于默认值，但传入 spec.meta 优先于已持久化值
        task_session_id = self._resolve_task_session_id(
            spec.id,
            meta,
            existing_meta,
        )
        task_chat = await self._ensure_task_chat(
            meta.get("task_chat_id")
            or existing_meta.get("task_chat_id")
            or "",
            task_session_id,
            creator_user_id,
            spec,
        )
        await self._update_task_chat(task_chat, spec, creator_user_id)
        self._apply_task_binding_defaults(meta)
        meta["task_session_id"] = task_session_id
        meta["task_chat_id"] = task_chat.id

        request, dispatch = self._bind_task_routing(
            spec,
            creator_user_id,
            task_session_id,
        )
        return spec.model_copy(
            update={
                "meta": meta,
                "request": request,
                "dispatch": dispatch,
            },
        )

    def _should_bind_task_session(
        self,
        spec: CronJobSpec,
        creator_user_id: Optional[str],
    ) -> bool:
        return bool(
            spec.task_type in {"agent", "text", "workflow"}
            and creator_user_id
            and self._chat_manager is not None,
        )

    async def _load_existing_task_meta(self, job_id: str) -> dict[str, Any]:
        # 读取失败不阻断创建流程，避免临时仓库异常影响定时任务保存。
        try:
            existing = await self._repo.get_job(job_id)
            if existing and existing.meta:
                return dict(existing.meta)
        except Exception:  # pylint: disable=broad-except
            pass
        return {}

    @staticmethod
    def _merge_task_binding_meta(
        spec_meta: dict[str, Any],
        existing_meta: dict[str, Any],
    ) -> dict[str, Any]:
        meta = dict(spec_meta)
        for key in (
            "task_has_scheduled_result",
            "task_last_scheduled_preview",
            "task_unread_execution_count",
            "task_last_scheduled_run_at",
            "pause_reason",
            "auto_paused_at",
            "unread_count_at_pause",
        ):
            if key not in meta and key in existing_meta:
                meta[key] = existing_meta[key]
        return meta

    @staticmethod
    def _resolve_task_session_id(
        job_id: str,
        meta: dict[str, Any],
        existing_meta: dict[str, Any],
    ) -> str:
        return str(
            meta.get("task_session_id")
            or existing_meta.get("task_session_id")
            or f"cron-task:{job_id}",
        )

    async def _ensure_task_chat(
        self,
        task_chat_id: str,
        task_session_id: str,
        creator_user_id: str,
        spec: CronJobSpec,
    ) -> Any:
        if task_chat_id:
            task_chat = await self._chat_manager.get_chat(task_chat_id)
            if task_chat is not None:
                if self._task_chat_matches_binding(
                    task_chat,
                    task_session_id,
                    creator_user_id,
                    spec,
                ):
                    return task_chat
                logger.warning(
                    "Cron task chat binding mismatch; creating a new chat: "
                    "chat_id=%s expected_session=%s actual_session=%s "
                    "expected_user=%s actual_user=%s",
                    task_chat_id,
                    task_session_id,
                    getattr(task_chat, "session_id", ""),
                    creator_user_id,
                    getattr(task_chat, "user_id", ""),
                )
                increment_cron_result_metric(CRON_SESSION_ROUTE_MISMATCH_TOTAL)
                create_chat = getattr(self._chat_manager, "create_chat", None)
                if callable(create_chat):
                    return await create_chat(
                        ChatSpec(
                            session_id=task_session_id,
                            user_id=creator_user_id,
                            channel=DEFAULT_CHANNEL,
                            name=spec.name,
                        ),
                    )
        return await self._chat_manager.get_or_create_chat(
            task_session_id,
            creator_user_id,
            DEFAULT_CHANNEL,
            name=spec.name,
        )

    @staticmethod
    def _task_chat_matches_binding(
        task_chat: Any,
        task_session_id: str,
        creator_user_id: str,
        spec: CronJobSpec,
    ) -> bool:
        """确认可复用 chat 与任务的读取、写入路由完全一致。"""
        if str(getattr(task_chat, "session_id", "")) != str(
            task_session_id,
        ) or str(getattr(task_chat, "user_id", "")) != str(creator_user_id):
            return False
        chat_meta = getattr(task_chat, "meta", {}) or {}
        expected_route = {
            "task_tenant_id": str(spec.tenant_id or ""),
            "task_source_id": str(spec.source_id or ""),
            "task_scope_id": str(spec.scope_id or ""),
        }
        return all(
            str(chat_meta.get(key, "")) == expected
            for key, expected in expected_route.items()
        )

    async def _update_task_chat(
        self,
        task_chat: Any,
        spec: CronJobSpec,
        creator_user_id: str,
    ) -> None:
        task_chat.name = spec.name
        task_chat.meta = {
            **(getattr(task_chat, "meta", {}) or {}),
            "session_kind": "task",
            "task_job_id": spec.id,
            "creator_user_id": creator_user_id,
            "task_tenant_id": str(spec.tenant_id or ""),
            "task_source_id": str(spec.source_id or ""),
            "task_scope_id": str(spec.scope_id or ""),
        }
        await self._chat_manager.update_chat(task_chat)

    @staticmethod
    def _apply_task_binding_defaults(meta: dict[str, Any]) -> None:
        meta.setdefault("task_has_scheduled_result", False)
        meta.setdefault("task_last_scheduled_preview", "")
        meta.setdefault("task_unread_execution_count", 0)
        meta.setdefault("task_last_scheduled_run_at", None)

    @staticmethod
    def _bind_task_routing(
        spec: CronJobSpec,
        creator_user_id: str,
        task_session_id: str,
    ) -> tuple[Any, Any]:
        request = spec.request
        if request is not None:
            request = request.model_copy(
                update={
                    "user_id": creator_user_id,
                    "session_id": task_session_id,
                },
            )

        dispatch = spec.dispatch
        if dispatch.channel == DEFAULT_CHANNEL:
            dispatch = dispatch.model_copy(
                update={
                    "target": dispatch.target.model_copy(
                        update={
                            "user_id": creator_user_id,
                            "session_id": task_session_id,
                        },
                    ),
                },
            )
        return request, dispatch

    async def _record_task_execution_success(
        self,
        job: CronJobSpec,
        execution_key: str = "",
    ) -> None:
        creator_user_id = (job.meta or {}).get("creator_user_id")
        task_session_id = (job.meta or {}).get("task_session_id")
        if (
            job.task_type not in {"agent", "text", "workflow"}
            or not creator_user_id
            or not task_session_id
        ):
            return

        preview = await self._resolve_task_success_preview(
            job,
            task_session_id,
            creator_user_id,
        )
        if job.task_type == "text":
            await self._append_text_task_message(
                task_session_id,
                creator_user_id,
                preview,
                execution_key,
            )
        apply_success = lambda jobs_file: self._apply_task_execution_success(
            jobs_file,
            job.id,
            preview,
            execution_key,
        )
        async with self._lock:
            _, auto_paused, _ = await self._mutate_jobs_file_locked(
                apply_success,
            )
        if auto_paused:
            await self._sync_auto_paused_task(job.id)

    async def _resolve_task_success_preview(
        self,
        job: CronJobSpec,
        task_session_id: str,
        creator_user_id: str,
    ) -> str:
        if job.task_type == "text":
            return (job.text or "").strip()
        if job.task_type == "workflow":
            session = getattr(self._runner, "session", None)
            if session is None:
                return ""
            state = await session.get_session_state_dict(
                task_session_id,
                creator_user_id,
            )
            messages = (state or {}).get(TASK_MESSAGES_STATE_KEY, [])
            if isinstance(messages, list):
                for message in reversed(messages):
                    if isinstance(message, dict):
                        preview = _extract_task_message_preview(message)
                        if preview:
                            return preview
            return ""
        if not getattr(self._runner, "session", None):
            return ""
        return await self._load_task_preview_text(
            task_session_id,
            creator_user_id,
        )

    async def _sync_auto_paused_task(self, job_id: str) -> None:
        ext_id = self._states.get(job_id, CronJobState()).external_job_id
        if ext_id and self._scheduler_adapter:
            await self._scheduler_adapter.pause_job(ext_id)
        updated_job = await self._repo.get_job(job_id)
        if updated_job and self._monitor_sync_client is not None:
            await self._monitor_sync_client.sync_job(
                updated_job,
                agent_id=self._agent_id or "default",
            )

    @asynccontextmanager
    async def _task_session_write_lock(
        self,
        session: Any,
        session_id: str,
        user_id: str,
        *,
        timeout_seconds: float = 5.0,
    ):
        lock_factory = getattr(session, "session_write_lock", None)
        if not callable(lock_factory):
            yield
            return
        async with lock_factory(
            session_id,
            user_id,
            timeout_seconds=timeout_seconds,
        ):
            yield

    async def _append_text_task_message(
        self,
        session_id: str,
        user_id: str,
        text: str,
        execution_key: str = "",
    ) -> None:
        if not text or not getattr(self._runner, "session", None):
            return

        timestamp = (
            datetime.now(timezone.utc)
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
        task_message = {
            "id": (
                f"cron-text-{execution_key}"
                if execution_key
                else f"cron-text-{uuid4()}"
            ),
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": text,
                },
            ],
            "metadata": {
                "cron_task": True,
            },
            "timestamp": timestamp,
        }

        def _merge(existing_state: dict[str, object]) -> dict[str, object]:
            merged_state = dict(existing_state)
            raw_task_messages = merged_state.get(TASK_MESSAGES_STATE_KEY, [])
            task_messages = (
                list(raw_task_messages)
                if isinstance(raw_task_messages, list)
                else []
            )
            if any(
                isinstance(message, dict)
                and message.get("id") == task_message["id"]
                for message in task_messages
            ):
                return merged_state
            task_messages.append(task_message)
            merged_state[TASK_MESSAGES_STATE_KEY] = task_messages
            return merged_state

        await self._runner.session.mutate_session_state(
            session_id=session_id,
            mutator=_merge,
            user_id=user_id,
            create_if_not_exist=True,
        )

    async def _load_task_preview_text(
        self,
        session_id: str,
        user_id: str,
    ) -> str:
        state = await self._runner.session.get_session_state_dict(
            session_id,
            user_id,
        )
        if not state:
            return ""
        memory_state = state.get("agent", {}).get("memory", {})
        from agentscope.memory import InMemoryMemory

        memory = InMemoryMemory()
        memory.load_state_dict(memory_state, strict=False)
        memories = await memory.get_memory(prepend_summary=False)
        return self._extract_latest_assistant_preview(memories)

    def _apply_task_execution_success(
        self,
        jobs_file: JobsFile,
        job_id: str,
        preview: str,
        execution_key: str = "",
    ) -> tuple[bool, bool]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id != job_id:
                continue
            meta = dict(job.meta or {})
            completed_keys = [
                str(key)
                for key in meta.get(TASK_SUCCESS_EXECUTION_KEYS_META_KEY, [])
                if isinstance(key, str) and key
            ]
            if execution_key and execution_key in completed_keys:
                return False, False
            meta["task_has_scheduled_result"] = True
            meta["task_last_scheduled_preview"] = preview[:10]
            unread_count = (
                int(meta.get("task_unread_execution_count", 0) or 0) + 1
            )
            meta["task_unread_execution_count"] = unread_count
            meta["task_last_scheduled_run_at"] = datetime.now(timezone.utc)
            if execution_key:
                meta[TASK_SUCCESS_EXECUTION_KEYS_META_KEY] = [
                    *completed_keys[-(MAX_TASK_SUCCESS_EXECUTION_KEYS - 1) :],
                    execution_key,
                ]
            updated = job.model_copy(update={"meta": meta})
            auto_paused = False
            auto_pause_config = resolve_cron_unread_auto_pause_config(
                get_current_source_system_config(),
            )
            if (
                auto_pause_config.enabled
                and unread_count >= auto_pause_config.threshold
                and job.enabled
            ):
                auto_paused = True
                meta["pause_reason"] = AUTO_PAUSE_REASON
                meta["auto_paused_at"] = meta["task_last_scheduled_run_at"]
                meta["unread_count_at_pause"] = unread_count
                updated = job.model_copy(
                    update={
                        "enabled": False,
                        "meta": meta,
                    },
                )
                jobs_file.jobs[index] = updated
                return True, auto_paused
            jobs_file.jobs[index] = updated
            return True, auto_paused
        return False, False

    def _build_wplus_link(self, session_id: str) -> str:
        """Build W+ deep link for cron task completion notification.

        生成格式：CMBMobileOA:///?pcSysId=xxx&pcWebConfig=xxx&pcParams=xxx
        用于在 PC 端招乎上跳转 W+ 并自动登录。
        """
        from ...config.utils import load_config

        config = load_config()
        zhaohu_config = config.channels.zhaohu

        # 获取配置
        menu_id = zhaohu_config.cron_task_menu_id or ""
        error_page = zhaohu_config.cron_task_error_page or ""
        sys_id = zhaohu_config.cron_task_sys_id or ""

        # 构建参数
        param = {
            "errorPage": error_page,
            "to": menu_id,
            "type": "toMenu",
            "queryParam": {
                "sessionId": session_id,
                "origin": "Y",
            },
        }

        # 参数格式化: encodeURIComponent(btoa(JSON.stringify(param)))
        pc_params = base64.b64encode(
            json.dumps(param, ensure_ascii=False).encode("utf-8"),
        ).decode("utf-8")
        pc_params = self._url_encode(pc_params)

        # 再封装一层: encodeURIComponent(btoa('pcParams='+pc_params))
        pc_params_wrapper = base64.b64encode(
            f"pcParams={pc_params}".encode("utf-8"),
        ).decode("utf-8")
        pc_params_wrapper = self._url_encode(pc_params_wrapper)

        pc_web_config = "eyJuYW1lIjoi6LSi5a%2BMVysiLCJ5c3RBdXRoIjoidHJ1ZSJ9"

        # 拼接地址
        wplus_link = (
            f"CMBMobileOA:///?pcSysId={sys_id}"
            f"&pcWebConfig={pc_web_config}"
            f"&pcParams={pc_params_wrapper}"
        )
        return wplus_link

    def _url_encode(self, text: str) -> str:
        """URL encode text."""
        import urllib.parse

        return urllib.parse.quote(text, safe="")

    def _get_zhaohu_push_config(self) -> Any:
        """Read the zhaohu channel config for session-end push links.

        Prefer the tenant agent config (where the prefix is distributed via
        console / inherited from template); fall back to global config.json
        so the call never fails.
        """
        from ...config.utils import load_config

        try:
            from ...config.config import load_agent_config

            agent_cfg = load_agent_config(
                self._agent_id or "default",
                tenant_id=self._tenant_id,
            )
            channels = getattr(agent_cfg, "channels", None)
            if channels is not None:
                zhaohu = getattr(channels, "zhaohu", None)
                if zhaohu is not None:
                    return zhaohu
        except Exception:
            logger.info(
                "zhaohu push config from agent failed, fallback to global",
            )
        return load_config().channels.zhaohu

    def _build_session_end_push_link(self, job: CronJobSpec) -> str:
        """非 RMASSIST 来源的定时任务通知跳转链接。

        链接前缀与 ID 类型来自 zhaohu 渠道配置
        （session_end_push_link_prefix / session_end_push_link_id_type）：
        - id_type=chat_id    -> {prefix}?chatId={job.meta.task_chat_id}
        - id_type=session_id -> {prefix}?sessionId={job.meta.task_session_id}
        前缀为空或对应 ID 值为空时返回空串（不附加链接）。
        """
        zhaohu_cfg = self._get_zhaohu_push_config()
        prefix = getattr(zhaohu_cfg, "session_end_push_link_prefix", "") or ""
        if not prefix:
            return ""
        sep = "&" if "?" in prefix else "?"
        id_type = (
            getattr(zhaohu_cfg, "session_end_push_link_id_type", "")
            or "session_id"
        )
        if id_type == "chat_id":
            chat_id = str(
                (getattr(job, "meta", None) or {}).get("task_chat_id", ""),
            )
            return f"{prefix}{sep}chatId={chat_id}" if chat_id else ""
        session_id = (getattr(job, "meta", None) or {}).get(
            "task_session_id",
            "",
        ) or ""
        return f"{prefix}{sep}sessionId={session_id}" if session_id else ""

    def _build_wplus_pc_link(self, session_id: str) -> str:
        """Build W+ PC menu HTTPS link for notification jump.

        格式: {domain}/gate/wpluspcmenuui/pcmenu?isSafeUrl=Y&sysid=retailbanking&pcParams=xxx
        用于在 W+ 消息推送中跳转到小助 claw 版指定会话。
        """
        from ...constant import CRON_WPLUS_PC_MENU_DOMAIN

        domain = CRON_WPLUS_PC_MENU_DOMAIN.rstrip("/")
        if not domain:
            return ""

        param = {
            "type": "toMenu",
            "to": "cmbclaw",
            "queryParam": {
                "sessionId": session_id,
                "origin": "Y",
            },
        }
        pc_params = base64.b64encode(
            json.dumps(param, ensure_ascii=False).encode("utf-8"),
        ).decode("utf-8")
        pc_params = self._url_encode(pc_params)

        return (
            f"{domain}/gate/wpluspcmenuui/pcmenu"
            f"?isSafeUrl=Y&sysid=retailbanking&pcParams={pc_params}"
        )

    async def _push_wplus_notification(
        self,
        job: CronJobSpec,
        *,
        raise_on_error: bool = False,
    ) -> None:
        """Push W+ message notification when a cron task completes."""

        from ...constant import (
            CRON_WPLUS_MSG_APP_ID,
            CRON_WPLUS_MSG_HEADER_KEY,
            CRON_WPLUS_MSG_HEADER_VALUE,
            CRON_WPLUS_MSG_NOTICE_ID,
            CRON_WPLUS_MSG_URL,
        )

        msg_url = CRON_WPLUS_MSG_URL
        notice_id = CRON_WPLUS_MSG_NOTICE_ID
        app_id = CRON_WPLUS_MSG_APP_ID
        header_key = CRON_WPLUS_MSG_HEADER_KEY
        header_value = CRON_WPLUS_MSG_HEADER_VALUE
        if (
            not msg_url
            or not notice_id
            or not app_id
            or not header_key
            or not header_value
        ):
            logger.debug(
                "Skip W+ push: config incomplete "
                "(msg_url=%s, notice_id=%s, app_id=%s, header_key=%s, header_value=%s)",
                bool(msg_url),
                bool(notice_id),
                bool(app_id),
                bool(header_key),
                bool(header_value),
            )
            return

        session_id = job.meta.get("task_session_id")
        creator_id = job.meta.get("creator_user_id")
        if not creator_id:
            logger.info("Skip W+ push: job %s has no creator_user_id", job.id)
            return

        now = datetime.now()
        wplus_pc_link = (
            self._build_wplus_pc_link(session_id) if session_id else ""
        )

        payload = {
            "appId": app_id,
            "msgUrl": wplus_pc_link,
            "msgText": (
                f"你发起的定时任务【{job.name}】已完成，"
                f"请进入小助claw版查看，完成时间:"
                f"{now.strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            "msgTime": now.strftime("%Y-%m-%d %H:%M:%S.")
            + f"{now.microsecond // 1000:03d}",
            "msgStyle": "TEXT",
            "msgTitle": f"{job.name}完成啦~",
            "noticeId": notice_id,
            "msgExpand": "",
            "msgSubTitle": "",
            "pushChannel": "HELP_CAT",
            "targetSapId": str(creator_id),
            "externalMsgId": str(uuid4()),
            "msgPictureUrl": "",
            "targetSapIdList": [],
        }

        headers = {
            header_key: header_value,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    msg_url,
                    json=payload,
                    headers=headers,
                )
                result = resp.json()
                if result.get("code") != "success":
                    logger.warning(
                        "W+ push returned non-success: job_id=%s result=%s",
                        job.id,
                        result,
                    )
                else:
                    logger.info(
                        "W+ push sent: job_id=%s job_name=%s target=%s",
                        job.id,
                        job.name,
                        creator_id,
                    )
        except asyncio.CancelledError:
            logger.warning(
                "W+ push cancelled: job_id=%s job_name=%s",
                job.id,
                job.name,
            )
            raise
        except Exception as exc:
            logger.warning(
                "W+ push failed: job_id=%s job_name=%s error=%s",
                job.id,
                job.name,
                repr(exc),
            )
            if raise_on_error:
                raise

    async def _push_task_success_notification(
        self,
        job: CronJobSpec,
        *,
        raise_on_error: bool = False,
    ) -> None:
        """Push success notification when an agent task completes."""
        # 只对 agent 类型的任务发送通知
        if job.task_type not in {"agent", "workflow"}:
            logger.debug(
                "Skip notification: job %s has no completion push",
                job.id,
            )
            return

        task_session_id = job.meta.get("task_session_id")
        if not task_session_id:
            logger.info("Skip notification: job %s has no session_id", job.id)
            return
        creator_id = job.meta.get("creator_user_id")
        logger.info(
            "Sending cron task completion notification: "
            "job_id=%s job_name=%s session_id=%s",
            job.id,
            job.name,
            task_session_id,
        )

        # 构建 meta，包含 link 和 summary
        meta = dict(job.dispatch.meta or {})

        # RMASSIST 使用 W+ 深链；其他来源从 zhaohu 配置生成跳转链接
        is_rmassist = job.source_id == "RMASSIST"
        if is_rmassist:
            wplus_link = self._build_wplus_link(task_session_id)
            logger.debug("Generated W+ link: %s", wplus_link)
            meta["link_url"] = wplus_link
            meta["link_text"] = "点击跳转小助claw版查看"
        else:
            link_url = self._build_session_end_push_link(job)
            if link_url:
                meta["link_url"] = link_url
                meta["link_text"] = "点击查看详情"
        meta["notification_summary"] = "小助claw定时任务完成提醒"

        await self.push_message(
            creator_id,
            job,
            task_session_id,
            meta,
            raise_on_error=raise_on_error,
        )

        # W+ 消息推送: 仅 RMASSIST 来源且 zhaohu 渠道启用时触发
        if is_rmassist and self._channel_manager is not None:
            zhaohu_ch = await self._channel_manager.get_channel("zhaohu")
            if zhaohu_ch is not None:
                await self._push_wplus_notification(
                    job,
                    raise_on_error=raise_on_error,
                )

    async def push_message(
        self,
        creator_id: Any | None,
        job: CronJobSpec,
        session_id: Any | None,
        meta: Optional[Dict[str, Any]] | None,
        *,
        raise_on_error: bool = False,
    ):
        # 固定使用 zhaohu 通道发送通知
        # 用 try-except 包裹，避免任务被取消时通知发送失败影响主流程
        try:
            await self._channel_manager.send_text(
                channel="zhaohu",
                user_id=creator_id,
                session_id=session_id,
                text=f"叮咚，你发起的定时任务【{job.name}】已完成，快来查收结果~",
                meta=meta,
            )
            logger.info(
                "Cron task completion notification sent: "
                "job_id=%s job_name=%s",
                job.id,
                job.name,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Cron task notification cancelled: job_id=%s job_name=%s",
                job.id,
                job.name,
            )
            raise
        except Exception as exc:
            logger.warning(
                "Failed to send cron task notification: "
                "job_id=%s job_name=%s error=%s",
                job.id,
                job.name,
                repr(exc),
            )
            if raise_on_error:
                raise

    async def run_task_session_cleanup(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """清理当前 manager 范围内的定时任务会话历史文件。"""
        config = await self._resolve_task_session_cleanup_config()
        result: dict[str, Any] = {
            "enabled": config.enabled,
            "retention_days": config.retention_days,
            "sessions_seen": 0,
            "sessions_cleaned": 0,
            "sessions_skipped_locked": 0,
            "runs_removed": 0,
            "messages_removed": 0,
        }
        if not config.enabled:
            return result
        session = getattr(self._runner, "session", None)
        if session is None:
            return result

        cutoff_base = now or datetime.now(timezone.utc)
        if cutoff_base.tzinfo is None:
            cutoff_base = cutoff_base.replace(tzinfo=timezone.utc)
        cutoff = cutoff_base.astimezone(timezone.utc) - timedelta(
            days=config.retention_days,
        )

        for job in await self._repo.list_jobs():
            meta = job.meta or {}
            task_session_id = meta.get("task_session_id")
            creator_user_id = meta.get("creator_user_id")
            if not task_session_id or not creator_user_id:
                continue
            result["sessions_seen"] += 1
            mutate_session_state = getattr(
                session,
                "mutate_session_state",
                None,
            )
            snapshot: _TaskSessionCleanupSnapshot | None = None
            try:
                if callable(mutate_session_state):

                    def _mutate_state(
                        state: dict[str, Any],
                    ) -> dict[str, Any]:
                        nonlocal snapshot
                        snapshot = _prune_task_session_state(state, cutoff)
                        if snapshot.changed:
                            return snapshot.state
                        return state

                    await mutate_session_state(
                        session_id=str(task_session_id),
                        user_id=str(creator_user_id),
                        mutator=_mutate_state,
                        create_if_not_exist=True,
                        timeout_seconds=5.0,
                    )
                else:
                    async with self._task_session_write_lock(
                        session,
                        str(task_session_id),
                        str(creator_user_id),
                    ):
                        state = await session.get_session_state_dict(
                            str(task_session_id),
                            str(creator_user_id),
                            allow_not_exist=True,
                        )
                        snapshot = _prune_task_session_state(state, cutoff)
                        if snapshot.changed:
                            await session.save_merged_state(
                                session_id=str(task_session_id),
                                user_id=str(creator_user_id),
                                state=snapshot.state,
                            )
            except TimeoutError:
                result["sessions_skipped_locked"] += 1
                logger.info(
                    "Task session cleanup skipped locked session: %s",
                    task_session_id,
                )
                continue

            if snapshot is None or not snapshot.changed:
                continue
            result["sessions_cleaned"] += 1
            result["runs_removed"] += snapshot.removed_runs
            result["messages_removed"] += snapshot.removed_messages

            def _apply_cleanup_meta(
                jobs_file: JobsFile,
                job_id: str = job.id,
                snap: _TaskSessionCleanupSnapshot = snapshot,
            ) -> tuple[bool, None]:
                return self._apply_task_session_cleanup_meta(
                    jobs_file,
                    job_id,
                    snap,
                )

            async with self._lock:
                await self._mutate_jobs_file_locked(_apply_cleanup_meta)
        logger.info("Task session cleanup result: %s", result)
        return result

    def _apply_task_session_cleanup_meta(
        self,
        jobs_file: JobsFile,
        job_id: str,
        snapshot: _TaskSessionCleanupSnapshot,
    ) -> tuple[bool, None]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id != job_id:
                continue
            meta = dict(job.meta or {})
            meta["task_has_scheduled_result"] = snapshot.has_result
            meta["task_last_scheduled_preview"] = (
                snapshot.latest_preview if snapshot.has_result else ""
            )
            meta["task_last_scheduled_run_at"] = (
                snapshot.latest_run_at if snapshot.has_result else None
            )
            meta["task_unread_execution_count"] = 0
            jobs_file.jobs[index] = job.model_copy(update={"meta": meta})
            return True, None
        return False, None

    async def send_task_success_notification(
        self,
        job_id: str,
        *,
        job: CronJobSpec | None = None,
    ) -> None:
        """发送完成通知；新版传入已核实快照，旧版默认从文件仓库获取。"""
        if job is None:
            job = await self._repo.get_job(job_id)
        if job is None:
            raise ValueError(f"cron job not found: {job_id}")
        if job.id != job_id:
            raise ValueError("cron_notification_snapshot_conflict")
        await self._push_task_success_notification(
            job,
            raise_on_error=True,
        )

    @staticmethod
    def _extract_latest_assistant_preview(messages: list[Any]) -> str:
        for msg in reversed(messages):
            role = (
                msg.get("role")
                if isinstance(msg, dict)
                else getattr(msg, "role", None)
            )
            if role != "assistant":
                continue
            content = (
                msg.get("content")
                if isinstance(msg, dict)
                else getattr(msg, "content", None)
            )
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text_parts = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text" and item.get("text"):
                        text_parts.append(str(item["text"]))
                    elif item.get("type") == "refusal" and item.get("refusal"):
                        text_parts.append(str(item["refusal"]))
                text = "".join(text_parts).strip()
            else:
                text = ""
            if text:
                return text[:10]
        return ""

    def _record_failure_timing(
        self,
        st: CronJobState,
        actual_time: datetime,
        status: str,
        error_msg: str,
    ) -> tuple[datetime, int]:
        """Record failure timing and update state. Returns (end_time, duration_ms)."""
        end_time = datetime.now(timezone.utc)
        duration_ms = int((end_time - actual_time).total_seconds() * 1000)
        st.last_status = status
        st.last_error = error_msg
        return end_time, duration_ms

    async def _sync_execution_to_monitor(
        self,
        job: CronJobSpec,
        exec_status: str,
        actual_time: datetime,
        end_time: Optional[datetime],
        duration_ms: int,
        error_message: str,
        output_preview: str,
        is_manual: bool = False,
        trace_id: str = "",
        input_snapshot: Optional[Dict[str, Any]] = None,
        executor_leader: str = "",
        execution_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Sync execution record to Monitor service (non-blocking).

        Args:
            job: 任务定义
            exec_status: 执行状态
            actual_time: 实际开始时间
            end_time: 结束时间
            duration_ms: 执行耗时（毫秒）
            error_message: 错误信息
            output_preview: 输出预览
            is_manual: 是否手动执行
            trace_id: 从执行过程中捕获的 trace_id
            input_snapshot: 执行时的输入快照
            executor_leader: 执行者 leader ID
            execution_meta: 执行模型相关的监控元数据
        """
        if self._monitor_sync_client is None:
            return

        session_id = str((job.meta or {}).get("task_session_id", "") or "")
        notification = _monitor_notification_schedule(
            job,
            exec_status,
            actual_time,
            end_time,
            is_manual,
            execution_meta,
            self._timezone,
        )

        await self._monitor_sync_client.record_execution(
            job=job,
            status=exec_status,
            actual_time=actual_time,
            end_time=end_time,
            duration_ms=duration_ms,
            error_message=error_message,
            is_manual=is_manual,
            trace_id=trace_id,
            session_id=session_id,
            output_preview=output_preview,
            input_snapshot=input_snapshot,
            executor_leader=executor_leader,
            notification_due_at=notification.due_at,
            notification_timezone=notification.timezone,
            suppress_notification=notification.suppress,
            meta=execution_meta,
        )

    async def _handle_success_notifications(
        self,
        job: CronJobSpec,
        execution_key: str = "",
    ) -> None:
        """处理任务成功执行后的通知和记录。

        Args:
            job: 任务定义
        """
        # The jobs-file update owns the idempotency receipt. Shield keeps a
        # cancellation from interrupting that atomic update midway.
        try:
            await asyncio.shield(
                self._record_task_execution_success(job, execution_key),
            )
        except asyncio.CancelledError:
            logger.info(
                "cron task notification/record cancelled before completion: "
                "job_id=%s",
                job.id,
            )
            raise

    def _handle_cancelled_after_success(
        self,
        job_id: str,
        st: CronJobState,
        actual_time: datetime,
        end_time: Optional[datetime],
        duration_ms: int,
    ) -> tuple[str, str, datetime, int]:
        """处理任务成功后被取消的情况。

        Args:
            st: 任务状态
            actual_time: 实际开始时间
            end_time: 结束时间
            duration_ms: 执行耗时

        Returns:
            (exec_status, error_message, end_time, duration_ms)
        """
        logger.info(
            "cron _execute_once: job_id=%s CancelledError after success, "
            "keeping success status",
            job_id,
        )
        exec_status = "success"
        end_time = end_time or datetime.now(timezone.utc)
        duration_ms = duration_ms or int(
            (end_time - actual_time).total_seconds() * 1000,
        )
        return exec_status, "", end_time, duration_ms

    def _handle_execution_cancelled(
        self,
        job_id: str,
        st: CronJobState,
        actual_time: datetime,
    ) -> tuple[str, str, datetime, int]:
        """处理任务被取消的情况。

        Args:
            st: 任务状态
            actual_time: 实际开始时间

        Returns:
            (exec_status, error_message, end_time, duration_ms)
        """
        logger.info(
            "cron _execute_once: job_id=%s status=cancelled",
            job_id,
        )
        end_time, duration_ms = self._record_failure_timing(
            st,
            actual_time,
            "cancelled",
            "Job was cancelled",
        )
        return "cancelled", "Job was cancelled", end_time, duration_ms

    def _handle_execution_error(
        self,
        st: CronJobState,
        actual_time: datetime,
        error: Exception,
    ) -> tuple[str, str, datetime, int]:
        """处理任务执行错误的情况。

        Args:
            st: 任务状态
            actual_time: 实际开始时间
            error: 异常对象

        Returns:
            (exec_status, error_message, end_time, duration_ms)
        """
        logger.warning(
            "cron _execute_once: job_id=%s status=error error=%s",
            st.last_run_at,
            repr(error),
            exc_info=(type(error), error, error.__traceback__),
        )
        end_time, duration_ms = self._record_failure_timing(
            st,
            actual_time,
            "error",
            repr(error),
        )
        return "error", str(error)[:200], end_time, duration_ms

    async def _finalize_execution_state(
        self,
        job: CronJobSpec,
        st: CronJobState,
        exec_status: str,
        actual_time: datetime,
        end_time: Optional[datetime],
        duration_ms: int,
        error_message: str,
        output_preview: str,
        is_manual: bool,
        trace_id: str,
        input_snapshot: Optional[Dict[str, Any]],
        executor_leader: str,
        execution_meta: Optional[Dict[str, Any]],
    ) -> None:
        """最终化执行状态并同步到 Monitor。

        Args:
            job: 任务定义
            st: 任务状态
            exec_status: 执行状态
            actual_time: 实际开始时间
            end_time: 结束时间
            duration_ms: 执行耗时
            error_message: 错误信息
            output_preview: 输出预览
            is_manual: 是否手动执行
            trace_id: trace ID
            input_snapshot: 输入快照
            executor_leader: 执行者 leader ID
            execution_meta: 执行模型相关的监控元数据
        """
        st.last_run_at = datetime.now(timezone.utc)
        self._states[job.id] = st

        await self._sync_execution_to_monitor(
            job=job,
            exec_status=exec_status,
            actual_time=actual_time,
            end_time=end_time,
            duration_ms=duration_ms,
            error_message=error_message,
            output_preview=output_preview,
            is_manual=is_manual,
            trace_id=trace_id,
            input_snapshot=input_snapshot,
            executor_leader=executor_leader,
            execution_meta=execution_meta,
        )
        chat_session_id = await self._get_task_chat_session_id(job)
        self._log_final_execution_decision(
            job,
            exec_status=exec_status,
            trace_id=trace_id,
            error_message=error_message,
            execution_meta=execution_meta,
            chat_session_id=chat_session_id,
        )
        logger.info(
            "cron execution finalized: job_id=%s exec_status=%s "
            "last_status=%s trace_id=%s duration_ms=%s output_preview_len=%s",
            job.id,
            exec_status,
            st.last_status,
            trace_id[:20] if trace_id else "(empty)",
            duration_ms,
            len(output_preview or ""),
        )

    async def _get_task_chat_session_id(self, job: CronJobSpec) -> str:
        """Resolve the persisted task Chat session for final diagnostics."""
        task_chat_id = str((job.meta or {}).get("task_chat_id") or "")
        getter = getattr(self._chat_manager, "get_chat", None)
        if not task_chat_id or not callable(getter):
            return ""
        try:
            chat = await getter(task_chat_id)
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "cron final decision could not load task chat: job_id=%s "
                "task_chat_id=%s",
                job.id,
                task_chat_id,
                exc_info=True,
            )
            return ""
        return str(getattr(chat, "session_id", "") or "")

    @staticmethod
    def _log_final_execution_decision(
        job: CronJobSpec,
        *,
        exec_status: str,
        trace_id: str,
        error_message: str,
        execution_meta: Optional[Dict[str, Any]],
        chat_session_id: str = "",
    ) -> None:
        """记录一次可关联 session 与终态的最终决策日志。"""
        meta = job.meta or {}
        request = job.request
        diagnostics = execution_meta or {}
        dispatch = diagnostics.get("cron_dispatch")
        if not isinstance(dispatch, dict):
            dispatch = {}
        logger.info(
            "cron final decision: job_id=%s trace_id=%s task_chat_id=%s "
            "task_session_id=%s request_session_id=%s chat_session_id=%s "
            "creator_user_id=%s tenant_id=%s source_id=%s scope_id=%s "
            "execution_key=%s response_terminal_status=%s "
            "completed_message_seen=%s assistant_message_count=%s "
            "output_len=%s output_source=%s "
            "session_state_commit_attempted=%s session_state_committed=%s "
            "final_status=%s final_error_code=%s",
            job.id,
            trace_id,
            meta.get("task_chat_id", ""),
            meta.get("task_session_id", ""),
            getattr(request, "session_id", "") if request is not None else "",
            chat_session_id,
            meta.get("creator_user_id", ""),
            job.tenant_id or "",
            job.source_id or "",
            job.scope_id or "",
            dispatch.get(
                "cron_execution_key",
                dispatch.get("execution_key", ""),
            ),
            diagnostics.get("response_terminal_status", ""),
            diagnostics.get("completed_message_seen", False),
            diagnostics.get("assistant_message_count", 0),
            diagnostics.get("output_len", 0),
            diagnostics.get("output_source", ""),
            diagnostics.get("session_state_commit_attempted", False),
            diagnostics.get("session_state_committed", False),
            exec_status,
            diagnostics.get("terminal_error_code") or error_message,
        )

    # pylint: disable=too-many-statements
    async def _execute_once(
        self,
        job: CronJobSpec,
        is_manual: bool = False,
        source_id: str | None = None,
        dispatch_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        job = self._with_execution_source_identity(job, source_id)
        job = await self._ensure_persisted_task_binding(job)
        job = self._with_execution_source_identity(job, source_id)
        dispatch_meta = dict(dispatch_meta or {})
        rt = self._rt.get(job.id)
        if not rt:
            rt = _Runtime(sem=asyncio.Semaphore(job.runtime.max_concurrency))
            self._rt[job.id] = rt

        async with rt.sem:
            st = self._states.get(job.id, CronJobState())
            st.last_status = "running"
            self._states[job.id] = st
            outcome = _ExecutionOutcome(datetime.now(timezone.utc))
            try:
                await self._run_once_execution(job, dispatch_meta, st, outcome)
            except asyncio.CancelledError as exc:
                self._record_once_exception(outcome, exc)
                if st.last_status == "success":
                    (
                        outcome.exec_status,
                        outcome.error_message,
                        outcome.end_time,
                        outcome.duration_ms,
                    ) = self._handle_cancelled_after_success(
                        job.id,
                        st,
                        outcome.actual_time,
                        outcome.end_time,
                        outcome.duration_ms,
                    )
                else:
                    (
                        outcome.exec_status,
                        outcome.error_message,
                        outcome.end_time,
                        outcome.duration_ms,
                    ) = self._handle_execution_cancelled(
                        job.id,
                        st,
                        outcome.actual_time,
                    )
                raise
            except Exception as e:  # pylint: disable=broad-except
                self._record_once_exception(outcome, e)
                (
                    outcome.exec_status,
                    outcome.error_message,
                    outcome.end_time,
                    outcome.duration_ms,
                ) = self._handle_execution_error(st, outcome.actual_time, e)
                raise
            finally:
                with self._bind_scheduled_run_source_system_config(
                    outcome.source_system_config,
                ):
                    outcome.execution_meta = _merge_cron_dispatch_meta(
                        outcome.execution_meta,
                        dispatch_meta,
                    )
                    await self._finalize_execution_state(
                        job=job,
                        st=st,
                        exec_status=outcome.exec_status,
                        actual_time=outcome.actual_time,
                        end_time=outcome.end_time,
                        duration_ms=outcome.duration_ms,
                        error_message=outcome.error_message,
                        output_preview=outcome.output_preview,
                        is_manual=is_manual,
                        trace_id=outcome.trace_id,
                        input_snapshot=outcome.input_snapshot,
                        executor_leader=outcome.executor_leader,
                        execution_meta=outcome.execution_meta,
                    )

    async def _run_once_execution(
        self,
        job: CronJobSpec,
        dispatch_meta: Dict[str, Any],
        st: CronJobState,
        outcome: _ExecutionOutcome,
    ) -> None:
        outcome.source_system_config = (
            await self._resolve_scheduled_run_source_system_config(
                source_id=job.source_id,
                scope_id=job.scope_id,
                boundary_name=f"job:{job.id}",
            )
        )
        with self._bind_scheduled_run_source_system_config(
            outcome.source_system_config,
        ):
            result = await self._execute_job_by_type(
                job,
                dispatch_meta,
            )
            outcome.trace_id = result.trace_id
            outcome.output_preview = result.output_preview
            outcome.input_snapshot = result.input_snapshot
            outcome.executor_leader = result.executor_leader
            outcome.execution_meta = result.execution_meta
            outcome.exec_status = getattr(result, "status", "success")
            st.last_status = outcome.exec_status
            st.last_error = None
            outcome.end_time = datetime.now(timezone.utc)
            outcome.duration_ms = int(
                (outcome.end_time - outcome.actual_time).total_seconds()
                * 1000,
            )
            if outcome.exec_status == "success":
                await self._handle_once_success(job, outcome)
            logger.info(
                "cron _execute_once: job_id=%s status=%s trace_id=%s",
                job.id,
                outcome.exec_status,
                outcome.trace_id[:20] if outcome.trace_id else "(empty)",
            )

    async def _execute_job_by_type(
        self,
        job: CronJobSpec,
        dispatch_meta: Dict[str, Any],
    ) -> Any:
        if job.task_type == "workflow":
            if self._workflow_executor is None:
                raise RuntimeError("workflow executor is unavailable")
            return await self._workflow_executor.execute(job, dispatch_meta)
        return await self._executor.execute(
            job,
            dispatch_meta=dispatch_meta,
        )

    async def _handle_once_success(
        self,
        job: CronJobSpec,
        outcome: _ExecutionOutcome,
    ) -> None:
        execution_key = str(
            (outcome.input_snapshot or {}).get("cron_execution_key") or "",
        )
        metadata = outcome.execution_meta or {}
        if not metadata.get("idempotent_replay") or metadata.get(
            "output_delivery_replay_supported",
        ):
            await self._notify_once_success(job, execution_key)

    async def _notify_once_success(
        self,
        job: CronJobSpec,
        execution_key: str,
    ) -> None:
        await self._handle_success_notifications(job, execution_key)

    @staticmethod
    def _record_once_exception(
        outcome: _ExecutionOutcome,
        error: BaseException,
    ) -> None:
        outcome.execution_meta = getattr(
            error,
            "cron_execution_meta",
            outcome.execution_meta,
        )
        trace_id = getattr(error, "cron_trace_id", None)
        if trace_id and not outcome.trace_id:
            outcome.trace_id = trace_id

    async def run_heartbeat(self) -> None:
        """执行一次心跳任务（供外部调度平台调用）。"""
        from .heartbeat import run_heartbeat_once

        workspace_dir, tenant_id, source_id, scope_id, runtime_tenant_id = (
            self._resolve_runtime_context()
        )
        source_system_config = (
            await self._resolve_scheduled_run_source_system_config(
                source_id=source_id,
                scope_id=scope_id,
                boundary_name="heartbeat",
            )
        )
        with (
            bind_tenant_context(
                tenant_id=tenant_id,
                workspace_dir=workspace_dir,
                source_id=source_id,
                scope_id=scope_id,
            ),
            bind_llm_workload(LLM_WORKLOAD_CRON),
            self._bind_scheduled_run_source_system_config(
                source_system_config,
            ),
        ):
            await run_heartbeat_once(
                runner=self._runner,
                channel_manager=self._channel_manager,
                agent_id=self._agent_id,
                tenant_id=runtime_tenant_id,
                workspace_dir=workspace_dir,
            )

    async def run_dream(self) -> None:
        """执行一次梦境任务（供外部调度平台调用）。"""
        workspace_dir, tenant_id, source_id, scope_id, runtime_tenant_id = (
            self._resolve_runtime_context()
        )
        source_system_config = (
            await self._resolve_scheduled_run_source_system_config(
                source_id=source_id,
                scope_id=scope_id,
                boundary_name="dream",
            )
        )
        with (
            bind_tenant_context(
                tenant_id=tenant_id,
                workspace_dir=workspace_dir,
                source_id=source_id,
                scope_id=scope_id,
            ),
            bind_llm_workload(LLM_WORKLOAD_CRON),
            self._bind_scheduled_run_source_system_config(
                source_system_config,
            ),
        ):
            before_record_ids = await self._load_dream_record_ids(
                workspace_dir,
            )
            await self._runner.memory_manager.dream_memory(
                tenant_id=runtime_tenant_id,
                trigger="cron",
            )
            governance_agent_id = self._continuous_governance_target_agent_id()
            await self._dual_write_dream_records(
                workspace_dir=workspace_dir,
                source_id=source_id,
                tenant_id=tenant_id,
                agent_id=governance_agent_id,
                before_record_ids=before_record_ids,
            )
            if workspace_dir is not None:
                from ..routers.dream_logs import (
                    dual_write_dream_archive_maintenance_result,
                    run_dream_archive_maintenance,
                )

                maintenance = await asyncio.to_thread(
                    run_dream_archive_maintenance,
                    workspace_dir,
                    actor="dream_cron",
                )
                if (
                    maintenance is not None
                    and self._continuous_governance_service is not None
                    and source_id
                    and tenant_id
                    and governance_agent_id
                ):
                    await dual_write_dream_archive_maintenance_result(
                        service=self._continuous_governance_service,
                        source_id=source_id,
                        target_user_id=tenant_id,
                        target_agent_id=governance_agent_id,
                        maintenance=maintenance,
                        actor="dream_cron",
                        source_name=source_id,
                    )
        logger.debug("Dream task executed successfully")

    async def _load_dream_record_ids(
        self,
        workspace_dir: Path | None,
    ) -> set[str]:
        """读取 dream 执行前已有记录 id。"""
        if workspace_dir is None:
            return set()
        data = await self._load_dream_logs(workspace_dir)
        return {
            str(record.get("id") or "")
            for record in data.get("records", [])
            if isinstance(record, dict) and record.get("id")
        }

    def _continuous_governance_target_agent_id(self) -> str | None:
        """返回可安全写入持续治理读模型的 agent 标识。"""
        agent_id = self._agent_id or "default"
        if len(
            agent_id,
        ) > GOVERNANCE_ID_MAX_LENGTH or not is_valid_identity_value(agent_id):
            logger.warning(
                "Skip continuous governance dual-write for invalid agent id",
            )
            return None
        return agent_id

    async def _dual_write_dream_records(
        self,
        *,
        workspace_dir: Path | None,
        source_id: str | None,
        tenant_id: str | None,
        agent_id: str | None,
        before_record_ids: set[str],
    ) -> None:
        """cron dream 完成后把新增治理记录写入数据库读模型。"""
        service = self._continuous_governance_service
        if (
            workspace_dir is None
            or not source_id
            or not tenant_id
            or not agent_id
        ):
            return
        if service is None:
            return
        data = await self._load_dream_logs(workspace_dir)
        for record in data.get("records", []):
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("id") or "")
            if not record_id or record_id in before_record_ids:
                continue
            await service.upsert_workspace_governance_record_with_health(
                source_id=source_id,
                target_user_id=tenant_id,
                target_user_name=None,
                bbk_id=None,
                target_agent_id=agent_id,
                record=record,
            )

    async def _load_dream_logs(self, workspace_dir: Path) -> dict[str, Any]:
        """在线程池中读取 workspace dream_logs.json。"""
        return await asyncio.to_thread(
            self._load_dream_logs_sync,
            workspace_dir,
        )

    def _load_dream_logs_sync(self, workspace_dir: Path) -> dict[str, Any]:
        """同步读取 workspace dream_logs.json，供线程池包装调用。"""
        path = workspace_dir / "dream_logs.json"
        if not path.exists():
            return {"records": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"records": []}
        return data if isinstance(data, dict) else {"records": []}

    def _resolve_runtime_context(
        self,
    ) -> tuple[Path | None, str | None, str | None, str | None, str | None]:
        """从工作区 runtime tenant 还原 logical tenant/source/scope。"""
        workspace_dir_value = getattr(self._runner, "workspace_dir", None)
        workspace_dir = (
            Path(workspace_dir_value) if workspace_dir_value else None
        )
        runtime_tenant_id = self._tenant_id
        workspace = getattr(self._runner, "_workspace", None)
        workspace_tenant_id = getattr(workspace, "tenant_id", None)
        if workspace_tenant_id:
            runtime_tenant_id = workspace_tenant_id

        tenant_id, source_id, scope_id = resolve_runtime_identity(
            runtime_tenant_id,
        )
        return (
            workspace_dir,
            tenant_id,
            source_id,
            scope_id,
            scope_id or tenant_id,
        )

    # ── 系统任务（heartbeat / dream）调度平台注册 ──

    def _system_job_ids_path(self) -> Path:
        return self._repo._path.parent / _SYSTEM_JOB_IDS_FILE

    @staticmethod
    def _read_system_job_ids_file(path: Path) -> dict[str, str]:
        try:
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {
                str(key): str(value)
                for key, value in data.items()
                if value is not None
            }
        except Exception:
            logger.debug("Failed to load system_job_ids from %s", path)
            return {}

    @staticmethod
    def _write_system_job_ids_file(
        path: Path,
        system_job_ids: dict[str, str],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(system_job_ids, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load_system_job_ids(self) -> None:
        path = self._system_job_ids_path()
        try:
            self._system_job_ids = self._read_system_job_ids_file(path)
        except Exception:
            logger.debug("Failed to load system_job_ids, resetting")
            self._system_job_ids = {}

    def _save_system_job_ids(self) -> None:
        path = self._system_job_ids_path()
        try:
            self._write_system_job_ids_file(path, self._system_job_ids)
        except Exception:
            logger.warning("Failed to save system_job_ids", exc_info=True)

    def _remember_external_job_id(
        self,
        job_id: str,
        ext_id: str,
    ) -> None:
        st = self._states.get(job_id, CronJobState())
        st.external_job_id = str(ext_id)
        self._states[job_id] = st

    async def _restore_external_job_ids(self) -> None:
        """恢复 external_job_id，缺失的补注册到外部调度平台。"""
        if isinstance(self._scheduler_adapter, NoopSchedulerAdapter):
            return
        try:
            for job in await self._repo.list_jobs():
                await self._restore_external_job_id(job)
        except Exception:
            logger.debug("Failed to restore external_job_ids from jobs.json")

    async def _restore_external_job_id(self, job: CronJobSpec) -> None:
        ext_id = str((job.meta or {}).get("external_job_id", "") or "")
        if is_batch_dispatch_managed_broadcast_child(job):
            await self._restore_batch_dispatch_child_external_job(
                job,
                ext_id,
            )
            return
        if ext_id:
            await self._refresh_existing_external_job(job, ext_id)
            return
        await self._migrate_missing_external_job(job)

    async def _restore_batch_dispatch_child_external_job(
        self,
        job: CronJobSpec,
        ext_id: str,
    ) -> None:
        if ext_id:
            self._remember_external_job_id(job.id, ext_id)
            await self._scheduler_adapter.pause_job(ext_id)
        logger.info(
            "Skipped external scheduler restore for batch dispatch child: "
            "job=%s ext_id=%s",
            job.id,
            ext_id or "",
        )

    async def _refresh_existing_external_job(
        self,
        job: CronJobSpec,
        ext_id: str,
    ) -> None:
        try:
            synced = await self._sync_job_to_external_scheduler(
                job,
                existing=job,
            )
            synced_ext_id = (synced.meta or {}).get(
                "external_job_id",
                "",
            ) or ext_id
            self._remember_external_job_id(job.id, synced_ext_id)
            await self._restore_batch_dispatch_parent_external_job(
                synced,
                synced_ext_id,
            )
        except Exception:
            logger.warning(
                "Failed to refresh external scheduler binding for job %s",
                job.id,
                exc_info=True,
            )

    async def _migrate_missing_external_job(self, job: CronJobSpec) -> None:
        try:
            ext_id = await self._register_external_scheduler_job(job)
            if not ext_id:
                return
            self._remember_external_job_id(job.id, ext_id)
            await self._persist_external_job_id(
                job.id,
                ext_id,
                self._get_external_scheduler_tenant_id(job),
            )
            if not job.enabled:
                await self._scheduler_adapter.pause_job(ext_id)
            await self._restore_registered_batch_dispatch_parent(job, ext_id)
            logger.info(
                "Migrated job %s to external scheduler: ext_id=%s",
                job.id,
                ext_id,
            )
        except Exception:
            logger.warning(
                "Failed to migrate job %s to external scheduler",
                job.id,
                exc_info=True,
            )

    async def _register_external_scheduler_job(
        self,
        job: CronJobSpec,
    ) -> str:
        tenant_id, source_id = self._get_external_scheduler_business_identity(
            job,
        )
        cron = (
            job.schedule.cron
            if job.schedule and job.schedule.cron
            else "0 0 1 1 *"
        )
        return await self._scheduler_adapter.register_job(
            tenant_id=tenant_id,
            source_id=source_id,
            agent_id=self._agent_id or "",
            task_type="job",
            job_id=job.id,
            job_name=job.name,
            cron=cron,
            callback_url=self._build_callback_url("job", job.id),
        )

    async def _restore_registered_batch_dispatch_parent(
        self,
        job: CronJobSpec,
        ext_id: str,
    ) -> None:
        saved_job = await self._repo.get_job(job.id)
        batch_source = saved_job or job.model_copy(
            update={
                "meta": {
                    **dict(job.meta or {}),
                    "external_job_id": ext_id,
                },
            },
        )
        await self._restore_batch_dispatch_parent_external_job(
            batch_source,
            ext_id,
        )

    async def _restore_batch_dispatch_parent_external_job(
        self,
        job: CronJobSpec,
        ext_id: str,
    ) -> None:
        if not is_batch_dispatch_parent(job):
            return
        batch_synced = await self._sync_batch_dispatch_scheduler_job(
            job,
            offset_window_hours=(
                self._get_batch_dispatch_offset_window_hours(job)
            ),
        )
        await self._scheduler_adapter.pause_job(ext_id)
        await self._persist_job_definition(batch_synced)

    async def _persist_external_job_id(
        self,
        job_id: str,
        ext_id: str,
        tenant_id: Optional[str] = None,
    ) -> None:
        """将 external_job_id 写入 jobs.json 中对应任务的 meta 字段。"""
        await self._persist_external_job_binding(job_id, ext_id, tenant_id)

    async def _persist_external_job_binding(
        self,
        job_id: str,
        ext_id: str,
        tenant_id: Optional[str] = None,
    ) -> None:
        try:
            async with self._lock:
                await self._mutate_jobs_file_locked(
                    lambda jobs_file: self._set_job_binding_in_jobs_file(
                        jobs_file,
                        job_id,
                        ext_id,
                        tenant_id,
                    ),
                )
        except Exception:
            logger.warning(
                "Failed to persist external scheduler binding for %s",
                job_id,
                exc_info=True,
            )

    @staticmethod
    def _set_job_meta_in_jobs_file(
        jobs_file,
        job_id: str,
        meta_updates: dict,
    ) -> tuple[bool, None]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id == job_id:
                meta = dict(job.meta or {})
                meta.update(meta_updates)
                jobs_file.jobs[index] = job.model_copy(update={"meta": meta})
                return True, None
        return False, None

    @staticmethod
    def _set_job_binding_in_jobs_file(
        jobs_file,
        job_id: str,
        ext_id: str,
        tenant_id: Optional[str] = None,
    ) -> tuple[bool, None]:
        for index, job in enumerate(jobs_file.jobs):
            if job.id == job_id:
                meta = dict(job.meta or {})
                meta["external_job_id"] = ext_id
                update = {"meta": meta}
                if tenant_id:
                    update["tenant_id"] = tenant_id
                jobs_file.jobs[index] = job.model_copy(update=update)
                return True, None
        return False, None

    @staticmethod
    def _every_to_cron(every: str) -> str:
        """将 interval 字符串转换为 5 字段 cron 表达式。

        支持格式：
        - "Nm"（1-59 分钟）→ "*/N * * * *"
        - "Nh"（1-23 小时）→ "0 */N * * *"
        如果 every 已经是 5 字段 cron，直接返回。
        """
        from .heartbeat import is_cron_expression

        every = (every or "").strip()
        if not every:
            return ""
        if is_cron_expression(every):
            return every

        m = _EVERY_PATTERN.match(every)
        if not m:
            logger.warning(
                "Failed to parse every=%r, fallback to 0/30 * * * *",
                every,
            )
            return "0/30 * * * *"

        if m.group("hours"):
            hours = int(m.group("hours"))
            if 1 <= hours <= 23:
                return f"0 0/{hours} * * *"
            logger.warning(
                "Hours out of range (1-23): %s, fallback to 0/30 * * * *",
                hours,
            )
            return "0/30 * * * *"

        minutes = int(m.group("minutes"))
        if 1 <= minutes <= 59:
            return f"0/{minutes} * * * *"
        logger.warning(
            "Minutes out of range (1-59): %s, fallback to 0/30 * * * *",
            minutes,
        )
        return "0/30 * * * *"

    async def _register_system_jobs(self) -> None:
        """注册 tenant 级 heartbeat 和 dream 到外部调度平台。"""
        await self.register_heartbeat()
        await self.register_dream()

    async def register_heartbeat(self) -> None:
        """将心跳任务注册到外部调度平台（或禁用时取消注册）。"""
        if isinstance(self._scheduler_adapter, NoopSchedulerAdapter):
            return

        from ...config.utils import get_heartbeat_config

        hb = get_heartbeat_config(self._agent_id, tenant_id=self._tenant_id)
        job_id = HEARTBEAT_JOB_ID
        ext_id = self._system_job_ids.get(job_id, "")

        if not hb.enabled:
            if ext_id:
                try:
                    await self._scheduler_adapter.pause_job(ext_id)
                except Exception:
                    logger.warning(
                        "Failed to pause heartbeat on external scheduler",
                        exc_info=True,
                    )
            return

        cron = self._every_to_cron(hb.every)
        if not cron:
            return

        callback_url = self._build_callback_url("heartbeat")
        tenant_id, source_id = self._get_external_scheduler_business_identity()
        try:
            if ext_id:
                await self._scheduler_adapter.update_job(
                    external_id=ext_id,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    agent_id=self._agent_id or "",
                    task_type="heartbeat",
                    job_id=job_id,
                    job_name="Heartbeat",
                    cron=cron,
                    callback_url=callback_url,
                )
                await self._scheduler_adapter.resume_job(ext_id)
            else:
                ext_id = await self._scheduler_adapter.register_job(
                    tenant_id=tenant_id,
                    source_id=source_id,
                    agent_id=self._agent_id or "",
                    task_type="heartbeat",
                    job_id=job_id,
                    job_name="Heartbeat",
                    cron=cron,
                    callback_url=callback_url,
                )
                if ext_id:
                    self._system_job_ids[job_id] = ext_id
                    self._save_system_job_ids()
        except Exception:
            logger.warning(
                "Failed to register heartbeat to external scheduler",
                exc_info=True,
            )

    async def register_dream(self) -> None:
        """将梦境任务注册到外部调度平台（或 cron 为空时取消注册）。"""
        if isinstance(self._scheduler_adapter, NoopSchedulerAdapter):
            return

        from ...config.utils import get_dream_cron

        job_id = DREAM_JOB_ID
        ext_id = self._system_job_ids.get(job_id, "")
        dream_cron = get_dream_cron(self._agent_id, tenant_id=self._tenant_id)

        if not dream_cron:
            if ext_id:
                try:
                    await self._scheduler_adapter.pause_job(ext_id)
                except Exception:
                    logger.warning(
                        "Failed to pause dream on external scheduler",
                        exc_info=True,
                    )
            return

        callback_url = self._build_callback_url("dream")
        tenant_id, source_id = self._get_external_scheduler_business_identity()
        try:
            if ext_id:
                await self._scheduler_adapter.update_job(
                    external_id=ext_id,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    agent_id=self._agent_id or "",
                    task_type="dream",
                    job_id=job_id,
                    job_name="Dream",
                    cron=dream_cron,
                    callback_url=callback_url,
                )
                await self._scheduler_adapter.resume_job(ext_id)
            else:
                ext_id = await self._scheduler_adapter.register_job(
                    tenant_id=tenant_id,
                    source_id=source_id,
                    agent_id=self._agent_id or "",
                    task_type="dream",
                    job_id=job_id,
                    job_name="Dream",
                    cron=dream_cron,
                    callback_url=callback_url,
                )
                if ext_id:
                    self._system_job_ids[job_id] = ext_id
                    self._save_system_job_ids()
        except Exception:
            logger.warning(
                "Failed to register dream to external scheduler",
                exc_info=True,
            )

    def _build_callback_url(self, task_type: str, job_id: str = "") -> str:
        """拼接外部调度平台回调 URL。

        tenant/agent/job 等参数已通过 jobParam 传递，URL 统一使用短路径。
        """
        base = (
            os.environ.get("SWE_SERVER_DOMAIN", "").strip()
            or "http://localhost:8000"
        )
        return f"{base}/api/internal/cron/callback"

    def _build_scheduler_callback_url(self) -> str:
        """Build Scheduler callback URL for batch parent physical timers."""
        base = (
            os.environ.get(SCHEDULER_API_URL_ENV, "").strip()
            or "http://localhost:9100/api"
        )
        return f"{base.rstrip('/')}/scheduler/cron/callback"

    async def refresh_next_run_at(self, job: CronJobSpec) -> None:
        """实时计算 next_run_at（直接从 job 对象，无需查 repo）。"""
        if not job.schedule or not job.schedule.cron:
            return
        try:
            next_runs = compute_next_run_times(
                job.schedule.cron,
                job.schedule.timezone or self._timezone or "UTC",
                count=3,
            )
            st = self._states.get(job.id, CronJobState())
            st.next_run_times = next_runs
            st.next_run_at = next_runs[0] if next_runs else None
            self._states[job.id] = st
        except Exception:
            logger.debug(
                "Failed to compute next_run_at for job %s",
                job.id,
                exc_info=True,
            )

    async def _refresh_next_run_at(self, job_id: str) -> None:
        """用 croniter 重新计算 next_run_at（仅用于展示）。"""
        job = await self._repo.get_job(job_id)
        if not job:
            return
        await self.refresh_next_run_at(job)

    async def _collect_prefetch_workspaces(self) -> set:
        """从所有任务中收集 workspace 目录集合。"""
        workspaces = set()
        try:
            jobs = await self._repo.list_jobs()
        except Exception:
            return workspaces
        for job in jobs:
            if job.dispatch and job.dispatch.meta:
                ws = job.dispatch.meta.get("workspace_dir")
                if ws:
                    workspaces.add(ws)
        return workspaces

    async def _collect_prefetch_targets(
        self,
    ) -> set[
        tuple[str | None, str | None, str | None, str | None, str | None]
    ]:
        """收集 auth token 预热所需的租户上下文。"""
        targets = set()
        try:
            jobs = await self._repo.list_jobs()
        except Exception:
            return targets
        for job in jobs:
            if job.task_type != "agent":
                continue
            workspace_dir = None
            if job.dispatch and job.dispatch.meta:
                workspace_dir = job.dispatch.meta.get("workspace_dir")
            runtime_tenant_id = self._get_job_runtime_tenant_id(job)
            tenant_id, source_id, scope_id = resolve_runtime_identity(
                runtime_tenant_id,
                job.source_id,
            )
            targets.add(
                (
                    runtime_tenant_id,
                    workspace_dir,
                    job.dispatch.target.user_id,
                    source_id,
                    scope_id,
                ),
            )
        return targets

    async def _prefetch_loop(self) -> None:
        """后台循环：每30~60分钟随机间隔，预热所有已知 workspace 的 auth token。"""
        while True:
            try:
                await asyncio.sleep(random.randint(1800, 3600))
                for (
                    runtime_tenant_id,
                    workspace_dir,
                    user_id,
                    source_id,
                    scope_id,
                ) in await self._collect_prefetch_targets():
                    try:
                        tenant_id, _, _ = resolve_runtime_identity(
                            runtime_tenant_id,
                            source_id,
                        )
                        with bind_tenant_context(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            workspace_dir=workspace_dir,
                            source_id=source_id,
                            scope_id=scope_id,
                        ):
                            await prefetch_auth_token(
                                tenant_id=runtime_tenant_id,
                                workspace_dir=workspace_dir,
                            )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Prefetch loop error", exc_info=True)

    # ----- Legacy API compatibility -----

    async def start(self) -> None:
        """启动 CronManager。"""
        if not self._started:
            await self.initialize()

    async def stop(self) -> None:
        """停止 CronManager。"""
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
            try:
                await self._prefetch_task
            except asyncio.CancelledError:
                pass
            self._prefetch_task = None
        self._started = False
