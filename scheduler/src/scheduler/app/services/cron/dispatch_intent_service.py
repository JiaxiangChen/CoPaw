# -*- coding: utf-8 -*-
"""Scheduler-owned cron dispatch intent queue service."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, NamedTuple, Optional

from pydantic import BaseModel, Field, field_validator

from scheduler.app.database import get_db_connection

_BEIJING_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 300
DEFAULT_DISPATCHED_STALE_SECONDS = 7800
EXECUTION_SCAN_LIMIT = 200
SUBTASK_EXECUTION_FAILED = "子任务执行失败"
SUBTASK_STATUS_TIMEOUT = "获取子任务状态超时"
CRON_AUTH_EXPIRED_ERROR = (
    "cron auth user_info is expired; "
    "please refresh cron auth configuration"
)
CRON_AUTH_EXPIRED_FAILURE_MESSAGE = f"鉴权过期: {CRON_AUTH_EXPIRED_ERROR}"
DISPATCH_OUTCOME_UNKNOWN = "dispatch outcome unknown past stale timeout"
EXECUTION_RECORD_MISSING = (
    "dispatch accepted but no execution record before stale timeout"
)

# Correlate with the current attempt, including the runtime job and tenant.
_CURRENT_EXECUTION_IDENTITY_SQL = """
    e.dispatch_intent_id = swe_cron_dispatch_intents.id
    AND e.dispatch_batch_id = swe_cron_dispatch_intents.batch_id
    AND e.dispatch_attempt = swe_cron_dispatch_intents.attempt_count
    AND e.job_id = swe_cron_dispatch_intents.job_id
    AND e.tenant_id = swe_cron_dispatch_intents.tenant_id
"""
_TERMINAL_EXECUTION_SQL = """
    (
        e.status IN ('error', 'failed', 'cancelled', 'timeout', 'skipped')
        OR (e.status = 'success' AND e.async_status IN ('success', 'error'))
    )
"""
_HAS_TERMINAL_EXECUTION_SQL = f"""
    EXISTS (
        SELECT 1 FROM swe_cron_executions e
        WHERE {_CURRENT_EXECUTION_IDENTITY_SQL}
          AND {_TERMINAL_EXECUTION_SQL}
    )
"""
_HAS_SUCCESSFUL_AGENT_SQL = f"""
    EXISTS (
        SELECT 1 FROM swe_cron_executions e
        WHERE {_CURRENT_EXECUTION_IDENTITY_SQL}
          AND e.status = 'success'
    )
"""
VIEWER_HEAT_LOOKBACK_DAYS = 30
VIEWER_FAST_READ_BUCKET_SECONDS = (
    2 * 60 * 60,
    3 * 60 * 60,
    4 * 60 * 60,
    5 * 60 * 60,
)
MAX_VIEWER_HEAT_SCORE = Decimal("9999.0000")
DEFAULT_PROVIDER_ID = "default"
DEFAULT_MODEL_ID = "default"


class _ExecutionCompletionTransition(NamedTuple):
    success: bool
    terminal_failure: bool
    next_status: str
    next_due_at: datetime
    event_type: str
    retry: bool
    error_message: str


class ClaimedDispatchIntent(BaseModel):
    """A dispatch intent claimed by the cron scheduling service."""

    id: int
    batch_id: str
    intent_role: str
    tenant_id: str
    agent_id: str = "default"
    source_id: str = ""
    provider_id: str = DEFAULT_PROVIDER_ID
    model_id: str = DEFAULT_MODEL_ID
    job_id: str
    parent_job_id: str = ""
    scheduled_fire_at: datetime | None = None
    dispatch_order: int = 0
    viewer_heat_score: float = 0
    attempt_count: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload", mode="before")
    @classmethod
    def _parse_payload(cls, value: Any) -> dict[str, Any]:
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}


def compute_batch_dispatch_order(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return stable child dispatch order without waiting-time aging."""
    ordered = [dict(row) for row in rows]
    ordered.sort(
        key=lambda row: (
            -float(row.get("viewer_heat_score") or 0),
            str(row.get("due_at") or ""),
            str(row.get("tenant_id") or ""),
            str(row.get("job_id") or ""),
        ),
    )
    for index, row in enumerate(ordered):
        row["dispatch_order"] = index
    return ordered


async def _fetch_viewer_heat_scores(
    *,
    job_ids: Iterable[str],
    parent_job_id: str,
    now: datetime,
    include_parent_job: bool,
) -> dict[str, Decimal]:
    normalized_job_ids = _unique_texts(job_ids)
    if not normalized_job_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(normalized_job_ids))
    parent_filter = _viewer_heat_parent_filter(include_parent_job)
    join_type = "LEFT JOIN" if include_parent_job else "JOIN"
    rows = await get_db_connection().fetch_all(
        f"""
        SELECT
            e.job_id,
            COUNT(*) AS read_count,
            {_viewer_fast_read_select_clause()}
        FROM swe_cron_executions e
        {join_type} swe_cron_jobs j ON e.job_id = j.id
        WHERE e.job_id IN ({placeholders})
          AND e.status = 'success'
          AND e.async_status = 'success'
          AND e.is_read = TRUE
          AND e.read_at IS NOT NULL
          AND e.read_at >= DATE_SUB(%s, INTERVAL %s DAY)
          AND {parent_filter}
        GROUP BY e.job_id
        """,
        (
            *VIEWER_FAST_READ_BUCKET_SECONDS,
            *normalized_job_ids,
            now,
            VIEWER_HEAT_LOOKBACK_DAYS,
            *_viewer_heat_parent_params(parent_job_id, include_parent_job),
        ),
    )
    return {
        str(row.get("job_id")): _viewer_heat_score_from_row(row)
        for row in rows
    }


def _viewer_fast_read_select_clause() -> str:
    return ",\n            ".join(
        [
            (
                "SUM(\n"
                "                CASE\n"
                "                    WHEN TIMESTAMPDIFF(\n"
                "                        SECOND,\n"
                "                        COALESCE(e.end_time, e.actual_time, e.created_at),\n"
                "                        e.read_at\n"
                "                    ) BETWEEN 0 AND %s\n"
                "                    THEN 1 ELSE 0\n"
                "                END\n"
                f"            ) AS fast_read_{hour}_hour_count"
            )
            for hour in (2, 3, 4, 5)
        ],
    )


def _viewer_heat_parent_filter(include_parent_job: bool) -> str:
    child_filter = """
    CASE
        WHEN JSON_VALID(j.meta)
        THEN JSON_UNQUOTE(
            JSON_EXTRACT(
                j.meta,
                '$.broadcast_source_job_id'
            )
        )
        ELSE ''
    END = %s
    """
    if include_parent_job:
        return f"(e.job_id = %s OR {child_filter})"
    return child_filter


def _viewer_heat_parent_params(
    parent_job_id: str,
    include_parent_job: bool,
) -> tuple[str, ...]:
    if include_parent_job:
        return (parent_job_id, parent_job_id)
    return (parent_job_id,)


def _viewer_heat_score_from_row(row: Mapping[str, Any]) -> Decimal:
    read_count = Decimal(str(row.get("read_count") or 0))
    fast_read_count = sum(
        Decimal(str(row.get(f"fast_read_{hour}_hour_count") or 0))
        for hour in (2, 3, 4, 5)
    )
    return min(read_count + fast_read_count, MAX_VIEWER_HEAT_SCORE)


class CronDispatchIntentService:
    """Durable queue service for cron dispatch intents."""

    async def upsert_dispatch_batch(
        self,
        *,
        batch_id: str,
        parent_job_id: str,
        parent_external_job_id: str = "",
        tenant_id: str,
        source_id: str = "",
        agent_id: str = "default",
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
        scheduled_fire_at: datetime,
        callback_received_at: datetime,
        callback_metadata: dict[str, Any] | None = None,
    ) -> None:
        db = get_db_connection()
        await db.execute(
            """
            INSERT INTO swe_cron_dispatch_batches (
                batch_id, parent_job_id, parent_external_job_id, tenant_id,
                source_id, provider_id, model_id, agent_id,
                scheduled_fire_at, callback_received_at, status,
                callback_metadata
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, 'received',
                %s
            )
            ON DUPLICATE KEY UPDATE
                provider_id = VALUES(provider_id),
                model_id = VALUES(model_id),
                callback_received_at = VALUES(callback_received_at),
                callback_metadata = VALUES(callback_metadata),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                batch_id,
                parent_job_id,
                parent_external_job_id or "",
                tenant_id,
                source_id or "",
                _normalized_provider_id(provider_id),
                _normalized_model_id(model_id),
                agent_id or "default",
                _to_beijing_naive(scheduled_fire_at),
                _to_beijing_naive(callback_received_at),
                _json_or_none(callback_metadata),
            ),
        )
        await self._record_event_best_effort(
            batch_id=batch_id,
            intent_id=None,
            event_type="batch_callback_received",
            job_id=parent_job_id,
            tenant_id=tenant_id,
            source_id=source_id,
            details=callback_metadata,
        )

    async def enqueue_batch_execution_intents(
        self,
        *,
        batch_id: str,
        parent_job_id: str,
        jobs: list[dict[str, Any]],
        due_at: datetime,
        scheduled_fire_at: datetime,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> list[int]:
        """Enqueue parent and child execution intents in one ordered batch."""
        rows = await self._build_ordered_execution_rows(
            parent_job_id=parent_job_id,
            jobs=jobs,
            due_at=due_at,
        )
        ids: list[int] = []
        for row in rows:
            intent_id = await _execute_write_return_last_id(
                """
                INSERT INTO swe_cron_dispatch_intents (
                    batch_id, intent_role, status, source_id, provider_id,
                    model_id, tenant_id, agent_id, job_id, parent_job_id,
                    scheduled_fire_at, due_at, dispatch_order,
                    viewer_heat_score, max_attempts, payload
                ) VALUES (
                    %s, %s, 'pending', %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    status = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN status ELSE 'pending'
                    END,
                    due_at = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN due_at ELSE VALUES(due_at)
                    END,
                    scheduled_fire_at = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN scheduled_fire_at ELSE VALUES(scheduled_fire_at)
                    END,
                    provider_id = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN provider_id ELSE VALUES(provider_id)
                    END,
                    model_id = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN model_id ELSE VALUES(model_id)
                    END,
                    dispatch_order = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN dispatch_order ELSE VALUES(dispatch_order)
                    END,
                    viewer_heat_score = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN viewer_heat_score ELSE VALUES(viewer_heat_score)
                    END,
                    max_attempts = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN max_attempts ELSE VALUES(max_attempts)
                    END,
                    payload = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN payload ELSE VALUES(payload)
                    END,
                    lock_owner = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN lock_owner ELSE ''
                    END,
                    locked_at = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN locked_at ELSE NULL
                    END,
                    error_message = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed', 'failed', 'cancelled')
                        THEN error_message ELSE ''
                    END
                """,
                (
                    batch_id,
                    row["intent_role"],
                    row["source_id"],
                    row["provider_id"],
                    row["model_id"],
                    row["tenant_id"],
                    row["agent_id"],
                    row["job_id"],
                    row["parent_job_id"],
                    _to_beijing_naive(scheduled_fire_at),
                    row["due_at"],
                    int(row["dispatch_order"]),
                    row["viewer_heat_score"],
                    max_attempts,
                    _json_or_none(row.get("payload")),
                ),
            )
            ids.append(intent_id)
            await self._record_event_best_effort(
                batch_id=batch_id,
                intent_id=intent_id,
                event_type=f"{row['intent_role']}_execution_intent_queued",
                job_id=row["job_id"],
                tenant_id=row["tenant_id"],
                source_id=row["source_id"],
                details={
                    "dispatch_order": int(row["dispatch_order"]),
                    "viewer_heat_score": float(row["viewer_heat_score"]),
                    "provider_id": row["provider_id"],
                    "model_id": row["model_id"],
                },
            )
        return ids

    async def _build_ordered_execution_rows(
        self,
        *,
        parent_job_id: str,
        jobs: list[dict[str, Any]],
        due_at: datetime,
    ) -> list[dict[str, Any]]:
        now = _to_beijing_naive(due_at)
        job_ids = [
            str(job.get("job_id") or "").strip()
            for job in jobs
            if str(job.get("job_id") or "").strip()
        ]
        heat_by_job_id = await _fetch_viewer_heat_scores(
            job_ids=job_ids,
            parent_job_id=parent_job_id,
            now=now,
            include_parent_job=True,
        )

        ordered_rows: list[dict[str, Any]] = []
        for job in jobs:
            job_id = str(job.get("job_id") or "").strip()
            tenant_id = str(job.get("tenant_id") or "").strip()
            if not job_id or not tenant_id:
                continue
            ordered_rows.append(
                {
                    **job,
                    "intent_role": str(job.get("intent_role") or "child"),
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "source_id": str(job.get("source_id") or ""),
                    "agent_id": str(job.get("agent_id") or "default"),
                    "parent_job_id": str(job.get("parent_job_id") or ""),
                    "provider_id": _normalized_provider_id(
                        job.get("provider_id"),
                    ),
                    "model_id": _normalized_model_id(job.get("model_id")),
                    "due_at": _to_beijing_naive(job.get("due_at") or now),
                    "viewer_heat_score": heat_by_job_id.get(
                        job_id,
                        Decimal("0"),
                    ),
                    "payload": _build_execution_payload(job),
                },
            )

        return compute_batch_dispatch_order(ordered_rows)

    async def update_batch_counts(
        self,
        *,
        batch_id: str,
        updated_at: datetime,
    ) -> None:
        db = get_db_connection()
        row = await db.fetch_one(
            """
            SELECT
                COUNT(*) AS total_count,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS completed_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                    AS failed_count,
                SUM(CASE WHEN status IN ('claimed', 'dispatched') THEN 1 ELSE 0 END)
                    AS running_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)
                    AS pending_count
            FROM swe_cron_dispatch_intents
            WHERE batch_id = %s
            """,
            (batch_id,),
        )
        total = int((row or {}).get("total_count") or 0)
        completed = int((row or {}).get("completed_count") or 0)
        failed = int((row or {}).get("failed_count") or 0)
        running = int((row or {}).get("running_count") or 0)
        pending = int((row or {}).get("pending_count") or 0)
        if total == 0:
            status = "received"
        elif completed + failed >= total:
            status = "failed" if failed else "completed"
        elif running > 0:
            status = "running"
        elif pending > 0:
            status = "pending"
        else:
            status = "received"
        await db.execute(
            """
            UPDATE swe_cron_dispatch_batches
            SET status = %s,
                total_count = %s,
                completed_count = %s,
                failed_count = %s,
                updated_at = %s,
                lock_owner = CASE
                    WHEN %s IN ('completed', 'failed') THEN ''
                    ELSE lock_owner
                END,
                locked_at = CASE
                    WHEN %s IN ('completed', 'failed') THEN NULL
                    ELSE locked_at
                END,
                completed_at = CASE
                    WHEN %s IN ('completed', 'failed') THEN %s
                    ELSE completed_at
                END
            WHERE batch_id = %s
            """,
            (
                status,
                total,
                completed,
                failed,
                _to_beijing_naive(updated_at),
                status,
                status,
                status,
                _to_beijing_naive(updated_at),
                batch_id,
            ),
        )

    async def enqueue_parent_intent(
        self,
        *,
        batch_id: str,
        tenant_id: str,
        agent_id: str,
        source_id: str,
        job_id: str,
        due_at: datetime,
        payload: dict[str, Any] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> int:
        due_at_value = _to_beijing_naive(due_at)
        intent_id = await _execute_write_return_last_id(
            """
            INSERT INTO swe_cron_dispatch_intents (
                batch_id, intent_role, status, source_id, tenant_id, agent_id,
                job_id, parent_job_id, due_at, dispatch_order,
                viewer_heat_score, max_attempts, payload
            ) VALUES (
                %s, 'parent', 'pending', %s, %s, %s,
                %s, '', %s, 0, 0, %s, %s
            )
            ON DUPLICATE KEY UPDATE
                id = LAST_INSERT_ID(id),
                status = CASE
                    WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                    THEN status ELSE 'pending'
                END,
                due_at = CASE
                    WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                    THEN due_at ELSE VALUES(due_at)
                END,
                max_attempts = CASE
                    WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                    THEN max_attempts ELSE VALUES(max_attempts)
                END,
                payload = CASE
                    WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                    THEN payload ELSE VALUES(payload)
                END,
                lock_owner = CASE
                    WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                    THEN lock_owner ELSE ''
                END,
                locked_at = CASE
                    WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                    THEN locked_at ELSE NULL
                END,
                error_message = CASE
                    WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                    THEN error_message ELSE ''
                END
            """,
            (
                batch_id,
                source_id or "",
                tenant_id,
                agent_id or "default",
                job_id,
                due_at_value,
                max_attempts,
                _json_or_none(payload),
            ),
        )
        await self._record_event_best_effort(
            batch_id=batch_id,
            intent_id=intent_id,
            event_type="parent_intent_queued",
            job_id=job_id,
            tenant_id=tenant_id,
            source_id=source_id,
        )
        return intent_id

    async def enqueue_child_intents(
        self,
        *,
        parent_intent_id: int,
        batch_id: str,
        parent_job_id: str,
        child_jobs: list[dict[str, Any]],
        source_id: str = "",
        agent_id: str = "default",
        due_at: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> list[int]:
        now = _to_beijing_naive(due_at or datetime.now(timezone.utc))
        heat_by_job_id = await _fetch_viewer_heat_scores(
            job_ids=_child_job_ids(child_jobs),
            parent_job_id=parent_job_id,
            now=now,
            include_parent_job=False,
        )
        ordered_rows = _ordered_child_intent_rows(
            child_jobs,
            heat_by_job_id=heat_by_job_id,
            default_due_at=now,
        )
        ids: list[int] = []
        for child in ordered_rows:
            child_source_id = str(child.get("source_id") or source_id or "")
            intent_id = await _execute_write_return_last_id(
                """
                INSERT INTO swe_cron_dispatch_intents (
                    batch_id, intent_role, status, source_id, tenant_id,
                    agent_id, job_id, parent_job_id, due_at, dispatch_order,
                    viewer_heat_score, max_attempts, payload
                ) VALUES (
                    %s, 'child', 'pending', %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    status = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                        THEN status ELSE 'pending'
                    END,
                    due_at = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                        THEN due_at ELSE VALUES(due_at)
                    END,
                    dispatch_order = VALUES(dispatch_order),
                    viewer_heat_score = VALUES(viewer_heat_score),
                    max_attempts = VALUES(max_attempts),
                    payload = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                        THEN payload ELSE VALUES(payload)
                    END,
                    lock_owner = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                        THEN lock_owner ELSE ''
                    END,
                    locked_at = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                        THEN locked_at ELSE NULL
                    END,
                    error_message = CASE
                        WHEN status IN ('claimed', 'acknowledged', 'dispatched', 'completed')
                        THEN error_message ELSE ''
                    END
                """,
                (
                    batch_id,
                    child_source_id,
                    child["tenant_id"],
                    str(child.get("agent_id") or agent_id or "default"),
                    child["job_id"],
                    parent_job_id,
                    child["due_at"],
                    int(child["dispatch_order"]),
                    child["viewer_heat_score"],
                    max_attempts,
                    _json_or_none(_build_child_payload(child, agent_id)),
                ),
            )
            ids.append(intent_id)
            await self._record_event_best_effort(
                batch_id=batch_id,
                intent_id=intent_id,
                event_type="child_intent_queued",
                job_id=child["job_id"],
                tenant_id=child["tenant_id"],
                source_id=child_source_id,
                details={
                    "parent_intent_id": parent_intent_id,
                    "dispatch_order": int(child["dispatch_order"]),
                    "viewer_heat_score": float(child["viewer_heat_score"]),
                },
            )
        return ids

    async def _execution_feedback_matches(
        self,
        *,
        row: Mapping[str, Any],
        intent_id: int,
        execution_id: int | None,
        expected_batch_id: str,
        expected_job_id: str,
        expected_tenant_id: str,
        expected_source_id: str | None,
        expected_attempt_count: int | None,
    ) -> bool:
        if not _matches_expected_dispatch_row(
            row,
            expected_batch_id=expected_batch_id,
            expected_job_id=expected_job_id,
            expected_tenant_id=expected_tenant_id,
            expected_source_id=expected_source_id,
        ):
            await self._record_event_best_effort(
                batch_id=expected_batch_id or str(row.get("batch_id") or ""),
                intent_id=intent_id,
                event_type="execution_intent_mismatch",
                job_id=expected_job_id,
                tenant_id=expected_tenant_id,
                source_id=expected_source_id or "",
                details=_execution_identity_mismatch_details(
                    row,
                    execution_id=execution_id,
                    expected_batch_id=expected_batch_id,
                    expected_job_id=expected_job_id,
                    expected_tenant_id=expected_tenant_id,
                    expected_source_id=expected_source_id,
                ),
            )
            return False
        actual_attempt_count = int(row.get("attempt_count") or 0)
        if (
            expected_attempt_count is not None
            and actual_attempt_count != expected_attempt_count
        ):
            await self._record_event_best_effort(
                batch_id=expected_batch_id or str(row.get("batch_id") or ""),
                intent_id=intent_id,
                event_type="execution_attempt_mismatch",
                job_id=expected_job_id,
                tenant_id=expected_tenant_id,
                source_id=expected_source_id or "",
                details={
                    "execution_id": execution_id,
                    "expected_attempt_count": expected_attempt_count,
                    "actual_attempt_count": actual_attempt_count,
                },
            )
            return False
        return True

    async def claim_due_intents(
        self,
        *,
        lock_owner: str,
        now_utc: datetime,
        limit: int,
        stale_lock_seconds: int = 600,
        dispatched_stale_seconds: int = DEFAULT_DISPATCHED_STALE_SECONDS,
        source_ids: list[str] | None = None,
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> list[ClaimedDispatchIntent]:
        ids = await self._claim_due_intent_ids(
            lock_owner=lock_owner,
            now_utc=now_utc,
            limit=limit,
            stale_lock_seconds=stale_lock_seconds,
            dispatched_stale_seconds=dispatched_stale_seconds,
            source_ids=source_ids,
            provider_id=provider_id,
            model_id=model_id,
        )
        if not ids:
            return []
        return await self._fetch_claimed_intents(lock_owner, ids)

    async def reconcile_dispatched_executions(
        self,
        *,
        now_utc: datetime,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        source_ids: list[str] | None = None,
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> int:
        """Settle current attempts from persisted Agent and subtask results."""
        scope_filter_clause, scope_filter_params = _build_scope_filter(
            source_ids=source_ids,
            provider_id=provider_id,
            model_id=model_id,
        )
        rows = await get_db_connection().fetch_all(
            f"""
            SELECT swe_cron_dispatch_intents.id,
                   swe_cron_dispatch_intents.batch_id,
                   swe_cron_dispatch_intents.job_id,
                   swe_cron_dispatch_intents.tenant_id,
                   swe_cron_dispatch_intents.source_id,
                   swe_cron_dispatch_intents.attempt_count,
                   e.id AS execution_id, e.status AS agent_status,
                   e.async_status, e.error_message
            FROM swe_cron_dispatch_intents
            JOIN swe_cron_executions e ON {_CURRENT_EXECUTION_IDENTITY_SQL}
            WHERE swe_cron_dispatch_intents.status = 'dispatched'
              AND {_TERMINAL_EXECUTION_SQL}
              {scope_filter_clause}
            ORDER BY swe_cron_dispatch_intents.id, e.id DESC
            LIMIT %s
            """,
            (*scope_filter_params, EXECUTION_SCAN_LIMIT),
        )
        updated_rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            intent_id = int(row["id"])
            if intent_id in seen:
                continue
            seen.add(intent_id)
            status = str(row["agent_status"]).lower()
            error = str(row.get("error_message") or "")
            if status == "success" and row["async_status"] == "error":
                status, error = "error", SUBTASK_EXECUTION_FAILED
            if await self.complete_from_execution(
                intent_id=intent_id,
                execution_id=int(row["execution_id"]),
                status=status,
                completed_at=now_utc,
                error=error,
                retry_delay_seconds=retry_delay_seconds,
                expected_batch_id=row["batch_id"],
                expected_job_id=row["job_id"],
                expected_tenant_id=row["tenant_id"],
                expected_source_id=row["source_id"],
                expected_attempt_count=int(row["attempt_count"]),
            ):
                updated_rows.append(row)
                logger.info(
                    "scheduler_dispatch_task_finished intent_id=%s batch_id=%s "
                    "job_id=%s status=%s",
                    intent_id,
                    row["batch_id"],
                    row["job_id"],
                    status,
                )
        if updated_rows:
            await self._refresh_batch_counts_for_rows(
                updated_rows,
                updated_at=now_utc,
            )
        return len(updated_rows)

    async def recover_stale_dispatched_intents(
        self,
        *,
        now_utc: datetime,
        dispatched_stale_seconds: int = DEFAULT_DISPATCHED_STALE_SECONDS,
        source_ids: list[str] | None = None,
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> int:
        """Requeue or fail stale dispatched intents before capacity checks."""
        db = get_db_connection()
        normalized_now = _to_beijing_naive(now_utc)
        dispatched_stale_before = normalized_now - timedelta(
            seconds=dispatched_stale_seconds,
        )
        scope_filter_clause, scope_filter_params = _build_scope_filter(
            source_ids=source_ids,
            provider_id=provider_id,
            model_id=model_id,
        )

        async with db.acquire() as conn:
            await conn.begin()
            retryable_rows: list[Any] = []
            exhausted_rows: list[Any] = []
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        SELECT id, batch_id, job_id, tenant_id, source_id,
                               attempt_count, max_attempts,
                               {_HAS_SUCCESSFUL_AGENT_SQL} AS awaiting_subtask
                        FROM swe_cron_dispatch_intents
                        WHERE status = 'dispatched'
                          AND locked_at IS NOT NULL
                          AND locked_at < %s
                          AND NOT {_HAS_TERMINAL_EXECUTION_SQL}
                          {scope_filter_clause}
                        FOR UPDATE
                        """,
                        (
                            dispatched_stale_before,
                            *scope_filter_params,
                        ),
                    )
                    stale_rows = list(await cur.fetchall())
                    retryable_rows, exhausted_rows = (
                        _partition_stale_dispatched_rows(
                            stale_rows,
                        )
                    )
                    if retryable_rows:
                        retryable_ids = _positive_int_ids_from_rows(
                            retryable_rows,
                        )
                        placeholders = ", ".join(
                            ["%s"] * len(retryable_ids),
                        )
                        await cur.execute(
                            f"""
                            UPDATE swe_cron_dispatch_intents
                            SET status = 'pending',
                                due_at = %s,
                                lock_owner = '',
                                locked_at = NULL,
                                error_message = CASE
                                    WHEN {_HAS_SUCCESSFUL_AGENT_SQL} THEN %s
                                    ELSE %s
                                END
                            WHERE id IN ({placeholders})
                            """,
                            (
                                normalized_now,
                                SUBTASK_STATUS_TIMEOUT,
                                DISPATCH_OUTCOME_UNKNOWN,
                                *retryable_ids,
                            ),
                        )
                    if exhausted_rows:
                        exhausted_ids = _positive_int_ids_from_rows(
                            exhausted_rows,
                        )
                        placeholders = ", ".join(["%s"] * len(exhausted_ids))
                        await cur.execute(
                            f"""
                            UPDATE swe_cron_dispatch_intents
                            SET status = 'failed',
                                due_at = %s,
                                lock_owner = '',
                                locked_at = NULL,
                                completed_at = %s,
                                error_message = CASE
                                    WHEN {_HAS_SUCCESSFUL_AGENT_SQL} THEN %s
                                    ELSE %s
                                END
                            WHERE id IN ({placeholders})
                            """,
                            (
                                normalized_now,
                                normalized_now,
                                SUBTASK_STATUS_TIMEOUT,
                                EXECUTION_RECORD_MISSING,
                                *exhausted_ids,
                            ),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        await self._record_retryable_dispatched_events(retryable_rows)
        await self._record_exhausted_dispatched_events(exhausted_rows)
        await self._refresh_batch_counts_for_rows(
            [*retryable_rows, *exhausted_rows],
            updated_at=now_utc,
        )
        return len(retryable_rows) + len(exhausted_rows)

    async def _claim_due_intent_ids(
        self,
        *,
        lock_owner: str,
        now_utc: datetime,
        limit: int,
        stale_lock_seconds: int,
        dispatched_stale_seconds: int,
        source_ids: list[str] | None,
        provider_id: str,
        model_id: str,
    ) -> list[int]:
        db = get_db_connection()
        normalized_now = _to_beijing_naive(now_utc)
        stale_before = normalized_now - timedelta(seconds=stale_lock_seconds)
        dispatched_stale_before = normalized_now - timedelta(
            seconds=dispatched_stale_seconds,
        )
        scope_filter_clause, scope_filter_params = _build_scope_filter(
            source_ids=source_ids,
            provider_id=provider_id,
            model_id=model_id,
        )

        async with db.acquire() as conn:
            await conn.begin()
            exhausted_rows: list[Any] = []
            try:
                async with conn.cursor() as cur:
                    exhausted_rows = (
                        await self._fetch_exhausted_dispatched_rows(
                            cur,
                            dispatched_stale_before=dispatched_stale_before,
                            scope_filter_clause=scope_filter_clause,
                            scope_filter_params=scope_filter_params,
                        )
                    )
                    await self._mark_exhausted_dispatched_rows_failed(
                        cur,
                        exhausted_rows=exhausted_rows,
                        normalized_now=normalized_now,
                    )
                    candidate_batch_ids = (
                        await self._fetch_candidate_batch_ids(
                            cur,
                            limit=limit,
                            stale_before=stale_before,
                            dispatched_stale_before=dispatched_stale_before,
                            normalized_now=normalized_now,
                            scope_filter_clause=scope_filter_clause,
                            scope_filter_params=scope_filter_params,
                        )
                    )
                    ids = await self._first_claimable_intent_ids(
                        cur,
                        candidate_batch_ids=candidate_batch_ids,
                        lock_owner=lock_owner,
                        limit=limit,
                        normalized_now=normalized_now,
                        stale_before=stale_before,
                        dispatched_stale_before=dispatched_stale_before,
                        scope_filter_clause=scope_filter_clause,
                        scope_filter_params=scope_filter_params,
                    )
                    await self._mark_intent_ids_claimed(
                        cur,
                        ids=ids,
                        lock_owner=lock_owner,
                        normalized_now=normalized_now,
                    )
                await conn.commit()
                await self._record_exhausted_dispatched_events(exhausted_rows)
                await self._refresh_batch_counts_for_rows(
                    exhausted_rows,
                    updated_at=now_utc,
                )
                return ids
            except Exception:
                await conn.rollback()
                raise

    async def _fetch_exhausted_dispatched_rows(
        self,
        cur: Any,
        *,
        dispatched_stale_before: datetime,
        scope_filter_clause: str,
        scope_filter_params: tuple[Any, ...],
    ) -> list[Any]:
        await cur.execute(
            f"""
            SELECT id, batch_id, job_id, tenant_id, source_id,
                   attempt_count, max_attempts,
                   {_HAS_SUCCESSFUL_AGENT_SQL} AS awaiting_subtask
            FROM swe_cron_dispatch_intents
            WHERE status = 'dispatched'
              AND locked_at IS NOT NULL
              AND locked_at < %s
              AND attempt_count >= max_attempts
              AND NOT {_HAS_TERMINAL_EXECUTION_SQL}
              {scope_filter_clause}
            FOR UPDATE
            """,
            (dispatched_stale_before, *scope_filter_params),
        )
        return list(await cur.fetchall())

    async def _mark_exhausted_dispatched_rows_failed(
        self,
        cur: Any,
        *,
        exhausted_rows: list[Any],
        normalized_now: datetime,
    ) -> None:
        exhausted_ids = _positive_int_ids_from_rows(exhausted_rows)
        if not exhausted_ids:
            return
        placeholders = ", ".join(["%s"] * len(exhausted_ids))
        await cur.execute(
            f"""
            UPDATE swe_cron_dispatch_intents
            SET status = 'failed',
                due_at = %s,
                lock_owner = '',
                locked_at = NULL,
                completed_at = %s,
                error_message = CASE
                    WHEN {_HAS_SUCCESSFUL_AGENT_SQL} THEN %s
                    ELSE %s
                END
            WHERE id IN ({placeholders})
            """,
            (
                normalized_now,
                normalized_now,
                SUBTASK_STATUS_TIMEOUT,
                EXECUTION_RECORD_MISSING,
                *exhausted_ids,
            ),
        )

    async def _fetch_candidate_batch_ids(
        self,
        cur: Any,
        *,
        limit: int,
        stale_before: datetime,
        dispatched_stale_before: datetime,
        normalized_now: datetime,
        scope_filter_clause: str,
        scope_filter_params: tuple[Any, ...],
    ) -> list[str]:
        candidate_limit = max(limit * 4, limit, 1)
        await cur.execute(
            f"""
            SELECT
                batch_id,
                MIN(dispatch_order) AS next_dispatch_order,
                MIN(id) AS next_id
            FROM swe_cron_dispatch_intents
            WHERE (
                (status = 'pending' AND attempt_count < max_attempts)
                OR (
                    status IN ('claimed', 'acknowledged')
                    AND locked_at IS NOT NULL
                    AND locked_at < %s
                )
                OR (
                    status = 'dispatched'
                    AND locked_at IS NOT NULL
                    AND locked_at < %s
                    AND attempt_count < max_attempts
                    AND NOT {_HAS_TERMINAL_EXECUTION_SQL}
                )
            )
              AND due_at <= %s
              {scope_filter_clause}
            GROUP BY batch_id
            ORDER BY next_dispatch_order, next_id
            LIMIT %s
            """,
            (
                stale_before,
                dispatched_stale_before,
                normalized_now,
                *scope_filter_params,
                candidate_limit,
            ),
        )
        return _unique_batch_ids(await cur.fetchall())

    async def _first_claimable_intent_ids(
        self,
        cur: Any,
        *,
        candidate_batch_ids: list[str],
        lock_owner: str,
        limit: int,
        normalized_now: datetime,
        stale_before: datetime,
        dispatched_stale_before: datetime,
        scope_filter_clause: str,
        scope_filter_params: tuple[Any, ...],
    ) -> list[int]:
        for candidate_batch_id in candidate_batch_ids:
            ids = await self._claimable_intent_ids_for_batch(
                cur,
                candidate_batch_id=candidate_batch_id,
                lock_owner=lock_owner,
                limit=limit,
                normalized_now=normalized_now,
                stale_before=stale_before,
                dispatched_stale_before=dispatched_stale_before,
                scope_filter_clause=scope_filter_clause,
                scope_filter_params=scope_filter_params,
            )
            if ids:
                return ids
        return []

    async def _claimable_intent_ids_for_batch(
        self,
        cur: Any,
        *,
        candidate_batch_id: str,
        lock_owner: str,
        limit: int,
        normalized_now: datetime,
        stale_before: datetime,
        dispatched_stale_before: datetime,
        scope_filter_clause: str,
        scope_filter_params: tuple[Any, ...],
    ) -> list[int]:
        locked = await self._lock_candidate_batch(
            cur,
            batch_id=candidate_batch_id,
            lock_owner=lock_owner,
            normalized_now=normalized_now,
            stale_before=stale_before,
        )
        if not locked:
            return []
        await cur.execute(
            f"""
            SELECT id
            FROM swe_cron_dispatch_intents
            WHERE (
                (status = 'pending' AND attempt_count < max_attempts)
                OR (
                    status IN ('claimed', 'acknowledged')
                    AND locked_at IS NOT NULL
                    AND locked_at < %s
                )
                OR (
                    status = 'dispatched'
                    AND locked_at IS NOT NULL
                    AND locked_at < %s
                    AND attempt_count < max_attempts
                    AND NOT {_HAS_TERMINAL_EXECUTION_SQL}
                )
            )
              AND due_at <= %s
              AND batch_id = %s
              {scope_filter_clause}
            ORDER BY dispatch_order, id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (
                stale_before,
                dispatched_stale_before,
                normalized_now,
                candidate_batch_id,
                *scope_filter_params,
                limit,
            ),
        )
        return _positive_int_ids_from_rows(await cur.fetchall())

    async def _lock_candidate_batch(
        self,
        cur: Any,
        *,
        batch_id: str,
        lock_owner: str,
        normalized_now: datetime,
        stale_before: datetime,
    ) -> bool:
        result = await cur.execute(
            """
            UPDATE swe_cron_dispatch_batches
            SET lock_owner = %s,
                locked_at = %s
            WHERE batch_id = %s
              AND (
                  lock_owner = ''
                  OR lock_owner = %s
                  OR locked_at IS NULL
                  OR locked_at < %s
              )
            """,
            (
                lock_owner,
                normalized_now,
                batch_id,
                lock_owner,
                stale_before,
            ),
        )
        return _rowcount(result) > 0

    async def _mark_intent_ids_claimed(
        self,
        cur: Any,
        *,
        ids: list[int],
        lock_owner: str,
        normalized_now: datetime,
    ) -> None:
        if not ids:
            return
        placeholders = ", ".join(["%s"] * len(ids))
        await cur.execute(
            f"""
            UPDATE swe_cron_dispatch_intents
            SET status = 'claimed',
                lock_owner = %s,
                locked_at = %s,
                attempt_count = attempt_count + 1,
                error_message = ''
            WHERE id IN ({placeholders})
            """,
            (lock_owner, normalized_now, *ids),
        )

    async def _record_retryable_dispatched_events(
        self,
        retryable_rows: list[Any],
    ) -> None:
        for row in retryable_rows:
            await self._record_event_best_effort(
                batch_id=str(_row_value(row, 1, "batch_id") or ""),
                intent_id=int(_row_value(row, 0, "id") or 0),
                event_type="stale_dispatch_requeued",
                job_id=str(_row_value(row, 2, "job_id") or ""),
                tenant_id=str(_row_value(row, 3, "tenant_id") or ""),
                source_id=str(_row_value(row, 4, "source_id") or ""),
                details={
                    "error": (
                        SUBTASK_STATUS_TIMEOUT
                        if _row_value(row, 7, "awaiting_subtask")
                        else DISPATCH_OUTCOME_UNKNOWN
                    ),
                },
            )

    async def _record_exhausted_dispatched_events(
        self,
        exhausted_rows: list[Any],
    ) -> None:
        for row in exhausted_rows:
            await self._record_event_best_effort(
                batch_id=str(_row_value(row, 1, "batch_id") or ""),
                intent_id=int(_row_value(row, 0, "id") or 0),
                event_type="child_execution_missing_failed",
                job_id=str(_row_value(row, 2, "job_id") or ""),
                tenant_id=str(_row_value(row, 3, "tenant_id") or ""),
                source_id=str(_row_value(row, 4, "source_id") or ""),
                details={
                    "error": (
                        SUBTASK_STATUS_TIMEOUT
                        if _row_value(row, 7, "awaiting_subtask")
                        else EXECUTION_RECORD_MISSING
                    ),
                },
            )

    async def _fetch_claimed_intents(
        self,
        lock_owner: str,
        ids: list[int],
    ) -> list[ClaimedDispatchIntent]:
        db = get_db_connection()
        placeholders = ", ".join(["%s"] * len(ids))
        rows = await db.fetch_all(
            f"""
            SELECT
                id, batch_id, intent_role, tenant_id, agent_id, source_id,
                provider_id, model_id, job_id, parent_job_id,
                scheduled_fire_at, dispatch_order, viewer_heat_score,
                attempt_count, payload
            FROM swe_cron_dispatch_intents
            WHERE id IN ({placeholders})
              AND lock_owner = %s
              AND status = 'claimed'
            ORDER BY dispatch_order, id
            """,
            (*ids, lock_owner),
        )
        return [ClaimedDispatchIntent.model_validate(row) for row in rows]

    async def acknowledge_intent(
        self,
        *,
        intent_id: int,
        worker_id: str,
        acknowledged_at: datetime,
    ) -> bool:
        return await self._transition_intent(
            intent_id=intent_id,
            worker_id=worker_id,
            status="acknowledged",
            timestamp_column="acked_at",
            timestamp=acknowledged_at,
            event_type="intent_dispatch_acknowledged",
        )

    async def mark_intent_dispatched(
        self,
        *,
        intent_id: int,
        worker_id: str,
        dispatched_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Record that SWE accepted the callback for an execution intent."""
        return await self._transition_intent(
            intent_id=intent_id,
            worker_id=worker_id,
            status="dispatched",
            timestamp_column="acked_at",
            timestamp=dispatched_at,
            event_type="callback_dispatched",
            details=details,
        )

    async def mark_intent_dispatch_unknown(
        self,
        *,
        intent_id: int,
        worker_id: str,
        observed_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Keep an ambiguously delivered callback in the dispatched state."""
        return await self._transition_intent(
            intent_id=intent_id,
            worker_id=worker_id,
            status="dispatched",
            timestamp_column="updated_at",
            timestamp=observed_at,
            event_type="callback_outcome_unknown",
            details=details,
        )

    async def complete_intent(
        self,
        *,
        intent_id: int,
        worker_id: str,
        completed_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> bool:
        return await self._transition_intent(
            intent_id=intent_id,
            worker_id=worker_id,
            status="completed",
            timestamp_column="completed_at",
            timestamp=completed_at,
            event_type="intent_completed",
            details=details,
        )

    async def fail_intent(
        self,
        *,
        intent_id: int,
        worker_id: str,
        error: str,
        failed_at: datetime,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> bool:
        db = get_db_connection()
        failed_at_value = _to_beijing_naive(failed_at)
        retry_at = failed_at_value + timedelta(seconds=retry_delay_seconds)
        row = await db.fetch_one(
            """
            SELECT batch_id, job_id, tenant_id, source_id,
                   attempt_count, max_attempts, status, lock_owner
            FROM swe_cron_dispatch_intents
            WHERE id = %s
            """,
            (intent_id,),
        )
        if not row:
            return False
        terminal = int(row.get("attempt_count") or 0) >= int(
            row.get("max_attempts") or DEFAULT_MAX_ATTEMPTS,
        )
        result = await db.execute(
            """
            UPDATE swe_cron_dispatch_intents
            SET status = %s,
                due_at = %s,
                lock_owner = '',
                locked_at = NULL,
                error_message = %s
            WHERE id = %s
              AND lock_owner = %s
              AND status IN ('claimed', 'acknowledged', 'dispatched')
            """,
            (
                "failed" if terminal else "pending",
                failed_at_value if terminal else retry_at,
                (error or "")[:2048],
                intent_id,
                worker_id,
            ),
        )
        if _rowcount(result) == 0:
            return False
        await self._record_event_best_effort(
            batch_id=str(row.get("batch_id") or ""),
            intent_id=intent_id,
            event_type="intent_failed" if terminal else "retry_scheduled",
            worker_id=worker_id,
            job_id=str(row.get("job_id") or ""),
            tenant_id=str(row.get("tenant_id") or ""),
            source_id=str(row.get("source_id") or ""),
            details={"error": (error or "")[:2048]},
        )
        return True

    async def accept_execution_feedback(
        self,
        *,
        intent_id: int,
        execution_id: int | None,
        expected_batch_id: str,
        expected_job_id: str,
        expected_tenant_id: str,
        expected_source_id: str | None,
        expected_attempt_count: int | None,
    ) -> bool:
        """Validate a durable SWE receipt without treating it as completion."""
        row = await get_db_connection().fetch_one(
            """
            SELECT batch_id, job_id, tenant_id, source_id, attempt_count
            FROM swe_cron_dispatch_intents WHERE id = %s
            """,
            (intent_id,),
        )
        if not row:
            return False
        return await self._execution_feedback_matches(
            row=row,
            intent_id=intent_id,
            execution_id=execution_id,
            expected_batch_id=expected_batch_id,
            expected_job_id=expected_job_id,
            expected_tenant_id=expected_tenant_id,
            expected_source_id=expected_source_id,
            expected_attempt_count=expected_attempt_count,
        )

    async def complete_from_execution(
        self,
        *,
        intent_id: int,
        execution_id: int | None,
        status: str,
        completed_at: datetime,
        error: str = "",
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        expected_batch_id: str = "",
        expected_job_id: str = "",
        expected_tenant_id: str = "",
        expected_source_id: str | None = None,
        expected_attempt_count: int | None = None,
    ) -> bool:
        """Update a dispatched child intent from the final SWE execution row."""
        db = get_db_connection()
        completed_at_value = _to_beijing_naive(completed_at)
        row = await db.fetch_one(
            """
            SELECT batch_id, job_id, tenant_id, source_id,
                   attempt_count, max_attempts, status AS current_status
            FROM swe_cron_dispatch_intents
            WHERE id = %s
            """,
            (intent_id,),
        )
        if not row:
            return False
        feedback_matches = await self._execution_feedback_matches(
            row=row,
            intent_id=intent_id,
            execution_id=execution_id,
            expected_batch_id=expected_batch_id,
            expected_job_id=expected_job_id,
            expected_tenant_id=expected_tenant_id,
            expected_source_id=expected_source_id,
            expected_attempt_count=expected_attempt_count,
        )
        if not feedback_matches:
            return False
        transition = _build_execution_completion_transition(
            row,
            status=status,
            completed_at=completed_at_value,
            error=error,
            retry_delay_seconds=retry_delay_seconds,
        )
        updated = await _update_execution_intent_row(
            db,
            intent_id=intent_id,
            completed_at=completed_at_value,
            transition=transition,
            expected_attempt_count=expected_attempt_count,
        )
        if not updated:
            return False
        await self._record_event_best_effort(
            batch_id=str(row.get("batch_id") or ""),
            intent_id=intent_id,
            event_type=transition.event_type,
            job_id=str(row.get("job_id") or ""),
            tenant_id=str(row.get("tenant_id") or ""),
            source_id=str(row.get("source_id") or ""),
            details={
                "execution_id": execution_id,
                "execution_status": status,
                "retry": transition.retry,
            },
        )
        return True

    async def summarize_recent_completion_feedback(
        self,
        *,
        since: datetime,
        now_utc: datetime,
        scope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Summarize recent completion feedback for capacity decisions."""
        db = get_db_connection()
        since_value = _to_beijing_naive(since)
        now_value = _to_beijing_naive(now_utc)
        scope_clause, scope_params = _build_single_scope_filter(scope)
        event_scope_clause, event_scope_params = _build_single_scope_filter(
            scope,
            table_alias="i",
        )
        state_row = await db.fetch_one(
            f"""
            SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)
                    AS pending_count,
                SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END)
                    AS claimed_count,
                SUM(CASE WHEN status = 'dispatched' THEN 1 ELSE 0 END)
                    AS running_count
            FROM swe_cron_dispatch_intents
            WHERE (due_at <= %s OR status IN ('claimed', 'dispatched'))
              {scope_clause}
            """,
            (now_value, *scope_params),
        )
        completion_row = await db.fetch_one(
            f"""
            SELECT
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS success_count,
                SUM(CASE
                    WHEN status = 'failed'
                     AND COALESCE(error_message, '') <> %s
                    THEN 1 ELSE 0
                END)
                    AS failure_count
            FROM swe_cron_dispatch_intents
            WHERE (completed_at >= %s OR updated_at >= %s)
              {scope_clause}
            """,
            (
                CRON_AUTH_EXPIRED_FAILURE_MESSAGE,
                since_value,
                since_value,
                *scope_params,
            ),
        )
        event_row = await db.fetch_one(
            f"""
            SELECT COUNT(*) AS retry_count
            FROM swe_cron_dispatch_events e
            LEFT JOIN swe_cron_dispatch_intents i ON e.intent_id = i.id
            WHERE e.event_type = 'retry_scheduled'
              AND e.created_at >= %s
              {event_scope_clause}
            """,
            (since_value, *event_scope_params),
        )
        retry_count = int((event_row or {}).get("retry_count") or 0)
        return {
            "pending_count": int((state_row or {}).get("pending_count") or 0),
            "claimed_count": int((state_row or {}).get("claimed_count") or 0),
            "running_count": int((state_row or {}).get("running_count") or 0),
            "success_count": int(
                (completion_row or {}).get("success_count") or 0,
            ),
            "failure_count": int(
                (completion_row or {}).get("failure_count") or 0,
            )
            + retry_count,
            "latency_p95_ms": 0,
        }

    async def list_dispatch_scopes(
        self,
        *,
        now_utc: datetime,
        source_ids: list[str] | None = None,
    ) -> list[dict[str, str]]:
        db = get_db_connection()
        now_value = _to_beijing_naive(now_utc)
        source_clause = ""
        source_params: tuple[str, ...] = ()
        normalized_source_ids = _normalize_source_ids(source_ids)
        if normalized_source_ids:
            placeholders = ", ".join(["%s"] * len(normalized_source_ids))
            source_clause = (
                f"AND (source_id IN ({placeholders}) "
                "OR source_id IS NULL OR source_id = '')"
            )
            source_params = tuple(normalized_source_ids)
        rows = await db.fetch_all(
            f"""
            SELECT DISTINCT
                COALESCE(source_id, '') AS source_id,
                COALESCE(NULLIF(provider_id, ''), %s) AS provider_id,
                COALESCE(NULLIF(model_id, ''), %s) AS model_id
            FROM swe_cron_dispatch_intents
            WHERE (
                status IN ('claimed', 'dispatched')
                OR (status = 'pending' AND due_at <= %s)
            )
              {source_clause}
            ORDER BY source_id, provider_id, model_id
            """,
            (
                DEFAULT_PROVIDER_ID,
                DEFAULT_MODEL_ID,
                now_value,
                *source_params,
            ),
        )
        return [
            {
                "source_id": str(row.get("source_id") or ""),
                "provider_id": _normalized_provider_id(row.get("provider_id")),
                "model_id": _normalized_model_id(row.get("model_id")),
            }
            for row in rows
        ]

    async def acquire_scope_lease(
        self,
        *,
        source_id: str,
        provider_id: str,
        model_id: str,
        worker_id: str,
        now_utc: datetime,
        lease_seconds: int,
    ) -> bool:
        db = get_db_connection()
        normalized_source_id = str(source_id or "")
        normalized_provider_id = _normalized_provider_id(provider_id)
        normalized_model_id = _normalized_model_id(model_id)
        normalized_worker_id = str(worker_id or "")
        normalized_now = _to_beijing_naive(now_utc)
        lease_expires_at = normalized_now + timedelta(
            seconds=max(1, int(lease_seconds or 1)),
        )
        await db.execute(
            """
            INSERT IGNORE INTO swe_cron_dispatch_scope_leases (
                source_id, provider_id, model_id, lock_owner,
                locked_at, lease_expires_at, heartbeat_at
            )
            VALUES (%s, %s, %s, '', NULL, NULL, NULL)
            """,
            (
                normalized_source_id,
                normalized_provider_id,
                normalized_model_id,
            ),
        )
        result = await db.execute(
            """
            UPDATE swe_cron_dispatch_scope_leases
            SET lock_owner = %s,
                locked_at = CASE
                    WHEN lock_owner = %s THEN locked_at ELSE %s
                END,
                lease_expires_at = %s,
                heartbeat_at = %s
            WHERE source_id = %s
              AND provider_id = %s
              AND model_id = %s
              AND (
                  lock_owner = ''
                  OR lock_owner = %s
                  OR lease_expires_at IS NULL
                  OR lease_expires_at < %s
              )
            """,
            (
                normalized_worker_id,
                normalized_worker_id,
                normalized_now,
                lease_expires_at,
                normalized_now,
                normalized_source_id,
                normalized_provider_id,
                normalized_model_id,
                normalized_worker_id,
                normalized_now,
            ),
        )
        return _rowcount(result) > 0

    async def resolve_worker_strategy(
        self,
        *,
        scope: Mapping[str, Any],
        now_utc: datetime,
        fallback: Mapping[str, Any],
    ) -> dict[str, Any]:
        db = get_db_connection()
        source_id = str(scope.get("source_id") or "")
        provider_id = _normalized_provider_id(scope.get("provider_id"))
        model_id = _normalized_model_id(scope.get("model_id"))
        rows = await db.fetch_all(
            """
            SELECT source_id, provider_id, model_id, default_strategy_id,
                   strategy_schedule
            FROM swe_cron_dispatch_model_worker_policy
            WHERE (source_id, provider_id, model_id) IN (
                (%s, %s, %s),
                (%s, %s, %s),
                (%s, %s, %s),
                ('default', %s, %s),
                ('default', 'default', %s),
                ('default', %s, 'default'),
                ('default', 'default', 'default')
            )
              AND enabled = 1
            """,
            _worker_policy_query_params(source_id, provider_id, model_id),
        )
        policy = _select_worker_policy(rows, source_id, provider_id, model_id)
        strategy_id = _worker_strategy_id(policy, fallback, now_utc)
        row = await db.fetch_one(
            """
            SELECT strategy_id, min_workers, baseline_workers, max_workers,
                   adjust_interval_seconds, feedback_window_seconds,
                   stale_execution_seconds, error_rate_rules
            FROM swe_cron_dispatch_worker_strategy
            WHERE strategy_id = %s
              AND enabled = 1
            LIMIT 1
            """,
            (strategy_id,),
        )
        return _resolved_worker_strategy(row, strategy_id, fallback)

    async def get_latest_worker_capacity(
        self,
        *,
        scope: Mapping[str, Any],
        strategy_id: str = "",
        worker_id: str = "",
    ) -> dict[str, Any] | None:
        db = get_db_connection()
        source_id = str(scope.get("source_id") or "")
        provider_id = _normalized_provider_id(scope.get("provider_id"))
        model_id = _normalized_model_id(scope.get("model_id"))
        normalized_strategy_id = str(strategy_id or "")
        row = await db.fetch_one(
            """
            SELECT effective_workers, created_at
            FROM swe_cron_dispatch_worker_capacity
            WHERE source_id = %s
              AND provider_id = %s
              AND model_id = %s
              AND (%s = '' OR strategy_id = %s)
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                source_id,
                provider_id,
                model_id,
                normalized_strategy_id,
                normalized_strategy_id,
            ),
        )
        return dict(row) if row else None

    async def record_worker_capacity(
        self,
        *,
        worker_id: str,
        source_id: str = "",
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
        strategy_id: str = "",
        previous_workers: int = 0,
        baseline_workers: int = 1,
        min_workers: int = 1,
        max_workers: int = 1,
        effective_workers: int = 1,
        pending_count: int = 0,
        claimed_count: int = 0,
        running_count: int = 0,
        success_count: int = 0,
        failure_count: int = 0,
        error_rate: float = 0,
        matched_rule: dict[str, Any] | None = None,
        avg_latency_ms: int = 0,
        decision_reason: str = "",
        recorded_at: datetime | None = None,
    ) -> None:
        db = get_db_connection()
        await db.execute(
            """
            INSERT INTO swe_cron_dispatch_worker_capacity (
                worker_id, source_id, provider_id, model_id, strategy_id,
                previous_workers, baseline_workers, min_workers,
                max_workers, effective_workers, pending_count, claimed_count,
                running_count, success_count, failure_count, error_rate,
                matched_rule, avg_latency_ms, decision_reason, created_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                worker_id,
                source_id or "",
                _normalized_provider_id(provider_id),
                _normalized_model_id(model_id),
                strategy_id or "",
                previous_workers,
                baseline_workers,
                min_workers,
                max_workers,
                effective_workers,
                pending_count,
                claimed_count,
                running_count,
                success_count,
                failure_count,
                error_rate,
                _json_or_none(matched_rule),
                avg_latency_ms,
                (decision_reason or "")[:255],
                _to_beijing_naive(recorded_at or datetime.now(timezone.utc)),
            ),
        )

    async def _transition_intent(
        self,
        *,
        intent_id: int,
        worker_id: str,
        status: str,
        timestamp_column: str,
        timestamp: datetime,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        db = get_db_connection()
        timestamp_value = _to_beijing_naive(timestamp)
        row = await db.fetch_one(
            """
            SELECT batch_id, job_id, tenant_id, source_id
            FROM swe_cron_dispatch_intents
            WHERE id = %s
            """,
            (intent_id,),
        )
        if not row:
            return False
        expected_statuses = (
            ("claimed",)
            if status in ("acknowledged", "dispatched")
            else ("claimed", "acknowledged", "dispatched")
        )
        placeholders = ", ".join(["%s"] * len(expected_statuses))
        result = await db.execute(
            f"""
            UPDATE swe_cron_dispatch_intents
            SET status = %s,
                {timestamp_column} = %s,
                lock_owner = CASE
                    WHEN %s = 'completed' THEN ''
                    ELSE lock_owner
                END,
                locked_at = CASE
                    WHEN %s = 'completed' THEN NULL
                    WHEN %s = 'dispatched' THEN %s
                    ELSE locked_at
                END
            WHERE id = %s
              AND lock_owner = %s
              AND status IN ({placeholders})
            """,
            (
                status,
                timestamp_value,
                status,
                status,
                status,
                timestamp_value,
                intent_id,
                worker_id,
                *expected_statuses,
            ),
        )
        if _rowcount(result) == 0:
            return False
        await self._record_event_best_effort(
            batch_id=str(row.get("batch_id") or ""),
            intent_id=intent_id,
            event_type=event_type,
            worker_id=worker_id,
            job_id=str(row.get("job_id") or ""),
            tenant_id=str(row.get("tenant_id") or ""),
            source_id=str(row.get("source_id") or ""),
            details=details,
        )
        return True

    async def _record_event_best_effort(
        self,
        **kwargs: Any,
    ) -> None:
        try:
            await self._record_event(**kwargs)  # pylint: disable=missing-kwoa
        except Exception:
            logger.warning(
                "Failed to record cron dispatch event",
                exc_info=True,
            )

    async def _refresh_batch_counts_for_rows(
        self,
        rows: list[Any],
        *,
        updated_at: datetime,
    ) -> None:
        batch_ids = sorted(
            {
                str(_row_value(row, 1, "batch_id") or "")
                for row in rows
                if str(_row_value(row, 1, "batch_id") or "")
            },
        )
        for batch_id in batch_ids:
            try:
                await self.update_batch_counts(
                    batch_id=batch_id,
                    updated_at=updated_at,
                )
            except Exception:
                logger.warning(
                    "Failed to refresh cron dispatch batch counts: batch_id=%s",
                    batch_id,
                    exc_info=True,
                )

    async def _record_event(
        self,
        *,
        batch_id: str,
        intent_id: int | None,
        event_type: str,
        worker_id: str = "",
        job_id: str = "",
        tenant_id: str = "",
        source_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        db = get_db_connection()
        await db.execute(
            """
            INSERT INTO swe_cron_dispatch_events (
                batch_id, intent_id, event_type, worker_id, job_id,
                tenant_id, source_id, details
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                batch_id,
                intent_id,
                event_type,
                worker_id or "",
                job_id or "",
                tenant_id or "",
                source_id or "",
                _json_or_none(details),
            ),
        )


def _to_beijing_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(_BEIJING_TZ).replace(tzinfo=None)


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False)


def _matches_expected_dispatch_row(
    row: Mapping[str, Any],
    *,
    expected_batch_id: str = "",
    expected_job_id: str = "",
    expected_tenant_id: str = "",
    expected_source_id: str | None = None,
) -> bool:
    for key, expected in (
        ("batch_id", expected_batch_id),
        ("job_id", expected_job_id),
        ("tenant_id", expected_tenant_id),
    ):
        if expected and str(row.get(key) or "") != expected:
            return False
    if expected_source_id is not None:
        return str(row.get("source_id") or "") == str(expected_source_id or "")
    return True


def _row_value(row: Any, index: int, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, TypeError):
        return None


def _positive_int_ids_from_rows(rows: Iterable[Any]) -> list[int]:
    ids: list[int] = []
    for row in rows:
        intent_id = int(_row_value(row, 0, "id") or 0)
        if intent_id > 0:
            ids.append(intent_id)
    return ids


def _partition_stale_dispatched_rows(
    rows: Iterable[Any],
) -> tuple[list[Any], list[Any]]:
    retryable_rows: list[Any] = []
    exhausted_rows: list[Any] = []
    for row in rows:
        attempt_count = int(_row_value(row, 5, "attempt_count") or 0)
        max_attempts = int(
            _row_value(row, 6, "max_attempts") or DEFAULT_MAX_ATTEMPTS,
        )
        if attempt_count < max_attempts:
            retryable_rows.append(row)
        else:
            exhausted_rows.append(row)
    return retryable_rows, exhausted_rows


def _unique_batch_ids(rows: Iterable[Any]) -> list[str]:
    batch_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        batch_id = str(_row_value(row, 0, "batch_id") or "")
        if not batch_id or batch_id in seen:
            continue
        seen.add(batch_id)
        batch_ids.append(batch_id)
    return batch_ids


def _unique_texts(values: Iterable[Any]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _child_job_ids(child_jobs: Iterable[Mapping[str, Any]]) -> list[str]:
    return _unique_texts(child.get("job_id") for child in child_jobs)


def _ordered_child_intent_rows(
    child_jobs: Iterable[Mapping[str, Any]],
    *,
    heat_by_job_id: Mapping[str, Decimal],
    default_due_at: datetime,
) -> list[dict[str, Any]]:
    rows = []
    for child in child_jobs:
        row = _normalize_child_intent_row(
            child,
            heat_by_job_id=heat_by_job_id,
            default_due_at=default_due_at,
        )
        if row is not None:
            rows.append(row)
    return compute_batch_dispatch_order(rows)


def _normalize_child_intent_row(
    child: Mapping[str, Any],
    *,
    heat_by_job_id: Mapping[str, Decimal],
    default_due_at: datetime,
) -> dict[str, Any] | None:
    job_id = str(child.get("job_id") or "").strip()
    tenant_id = str(child.get("tenant_id") or "").strip()
    if not job_id or not tenant_id:
        return None
    return {
        **child,
        "job_id": job_id,
        "tenant_id": tenant_id,
        "due_at": _to_beijing_naive(child.get("due_at") or default_due_at),
        "viewer_heat_score": heat_by_job_id.get(job_id, Decimal("0")),
    }


def _execution_identity_mismatch_details(
    row: Mapping[str, Any],
    *,
    execution_id: int | None,
    expected_batch_id: str,
    expected_job_id: str,
    expected_tenant_id: str,
    expected_source_id: str | None,
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "expected_batch_id": expected_batch_id,
        "expected_job_id": expected_job_id,
        "expected_tenant_id": expected_tenant_id,
        "expected_source_id": expected_source_id,
        "actual_batch_id": str(row.get("batch_id") or ""),
        "actual_job_id": str(row.get("job_id") or ""),
        "actual_tenant_id": str(row.get("tenant_id") or ""),
        "actual_source_id": str(row.get("source_id") or ""),
    }


def _build_execution_completion_transition(
    row: Mapping[str, Any],
    *,
    status: str,
    completed_at: datetime,
    error: str,
    retry_delay_seconds: int,
) -> _ExecutionCompletionTransition:
    normalized_status = str(status or "").lower()
    success = normalized_status == "success"
    auth_expired = not success and _is_cron_auth_expired_error(error)
    terminal_failure = auth_expired or (
        int(row.get("attempt_count") or 0)
        >= int(row.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    )
    if success:
        next_status = "completed"
        event_type = "child_execution_completed"
    elif terminal_failure:
        next_status = "failed"
        event_type = "child_execution_failed"
    else:
        next_status = "pending"
        event_type = "retry_scheduled"
    retry = not success and not terminal_failure
    next_due_at = (
        completed_at + timedelta(seconds=retry_delay_seconds)
        if retry
        else completed_at
    )
    return _ExecutionCompletionTransition(
        success=success,
        terminal_failure=terminal_failure,
        next_status=next_status,
        next_due_at=next_due_at,
        event_type=event_type,
        retry=retry,
        error_message=(
            ""
            if success
            else (
                CRON_AUTH_EXPIRED_FAILURE_MESSAGE
                if auth_expired
                else (error or normalized_status or "")[:2048]
            )
        ),
    )


def _is_cron_auth_expired_error(error: str) -> bool:
    normalized_error = str(error or "").strip().casefold()
    return normalized_error == CRON_AUTH_EXPIRED_ERROR.casefold()


async def _update_execution_intent_row(
    db: Any,
    *,
    intent_id: int,
    completed_at: datetime,
    transition: _ExecutionCompletionTransition,
    expected_attempt_count: int | None,
) -> bool:
    attempt_guard = ""
    params: list[Any] = [
        transition.next_status,
        transition.next_due_at,
        transition.next_status,
        completed_at,
        transition.error_message,
        intent_id,
    ]
    if expected_attempt_count is not None:
        attempt_guard = "AND attempt_count = %s"
        params.append(expected_attempt_count)
    result = await db.execute(
        f"""
        UPDATE swe_cron_dispatch_intents
        SET status = %s,
            due_at = %s,
            completed_at = CASE
                WHEN %s IN ('completed', 'failed') THEN %s
                ELSE completed_at
            END,
            lock_owner = '',
            locked_at = NULL,
            error_message = %s
        WHERE id = %s
          {attempt_guard}
          AND status IN ('dispatched', 'claimed', 'acknowledged')
        """,
        tuple(params),
    )
    return _rowcount(result) > 0


def _worker_policy_query_params(
    source_id: str,
    provider_id: str,
    model_id: str,
) -> tuple[str, ...]:
    return (
        source_id,
        provider_id,
        model_id,
        source_id,
        DEFAULT_PROVIDER_ID,
        model_id,
        source_id,
        provider_id,
        DEFAULT_MODEL_ID,
        provider_id,
        model_id,
        model_id,
        provider_id,
    )


def _worker_policy_keys(
    source_id: str,
    provider_id: str,
    model_id: str,
) -> tuple[tuple[str, str, str], ...]:
    return (
        (source_id, provider_id, model_id),
        (source_id, DEFAULT_PROVIDER_ID, model_id),
        (source_id, provider_id, DEFAULT_MODEL_ID),
        ("default", provider_id, model_id),
        ("default", DEFAULT_PROVIDER_ID, model_id),
        ("default", provider_id, DEFAULT_MODEL_ID),
        ("default", DEFAULT_PROVIDER_ID, DEFAULT_MODEL_ID),
    )


def _select_worker_policy(
    rows: Iterable[Mapping[str, Any]],
    source_id: str,
    provider_id: str,
    model_id: str,
) -> Mapping[str, Any] | None:
    policy_by_key = {
        (
            str(row.get("source_id") or ""),
            _normalized_provider_id(row.get("provider_id")),
            _normalized_model_id(row.get("model_id")),
        ): row
        for row in rows
    }
    return next(
        (
            policy_by_key[key]
            for key in _worker_policy_keys(source_id, provider_id, model_id)
            if key in policy_by_key
        ),
        None,
    )


def _worker_strategy_id(
    policy: Mapping[str, Any] | None,
    fallback: Mapping[str, Any],
    now_utc: datetime,
) -> str:
    fallback_id = str((fallback or {}).get("strategy_id") or "default")
    if not policy:
        return fallback_id
    scheduled_strategy = _select_scheduled_strategy_id(
        _parse_json(policy.get("strategy_schedule")),
        now_utc,
    )
    return scheduled_strategy or str(
        policy.get("default_strategy_id") or fallback_id,
    )


def _resolved_worker_strategy(
    row: Mapping[str, Any] | None,
    strategy_id: str,
    fallback: Mapping[str, Any],
) -> dict[str, Any]:
    if not row:
        return dict(fallback)
    return {
        "strategy_id": str(row.get("strategy_id") or strategy_id),
        "min_workers": int(row.get("min_workers") or 1),
        "baseline_workers": int(row.get("baseline_workers") or 1),
        "max_workers": int(row.get("max_workers") or 1),
        "adjust_interval_seconds": int(
            row.get("adjust_interval_seconds") or 300,
        ),
        "feedback_window_seconds": int(
            row.get("feedback_window_seconds") or 300,
        ),
        "stale_execution_seconds": int(
            row.get("stale_execution_seconds")
            or DEFAULT_DISPATCHED_STALE_SECONDS,
        ),
        "error_rate_rules": _parse_json(row.get("error_rate_rules")) or [],
    }


def _build_child_payload(
    child: Mapping[str, Any],
    default_agent_id: str,
) -> dict[str, Any]:
    payload = child.get("payload")
    if isinstance(payload, dict):
        return payload
    return {
        "tenant_id": str(child.get("tenant_id") or ""),
        "job_id": str(child.get("job_id") or ""),
        "source_id": str(child.get("source_id") or ""),
        "agent_id": str(
            child.get("agent_id") or default_agent_id or "default",
        ),
    }


def _build_execution_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    payload = job.get("payload")
    if isinstance(payload, dict):
        result = dict(payload)
    else:
        result = {}
    result.setdefault("tenant_id", str(job.get("tenant_id") or ""))
    result.setdefault("job_id", str(job.get("job_id") or ""))
    result.setdefault("source_id", str(job.get("source_id") or ""))
    result.setdefault("agent_id", str(job.get("agent_id") or "default"))
    result.setdefault(
        "provider_id",
        _normalized_provider_id(job.get("provider_id")),
    )
    result.setdefault("model_id", _normalized_model_id(job.get("model_id")))
    return result


def _rowcount(result: Any) -> int:
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    return int(getattr(result, "rowcount", 0) or 0)


def _normalize_source_ids(source_ids: list[str] | None) -> list[str]:
    normalized = []
    for source_id in source_ids or []:
        value = str(source_id or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _normalized_provider_id(value: Any) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_PROVIDER_ID


def _normalized_model_id(value: Any) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_MODEL_ID


def _build_scope_filter(
    *,
    source_ids: list[str] | None,
    provider_id: str,
    model_id: str,
) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    normalized_source_ids = _normalize_source_ids(source_ids)
    if normalized_source_ids:
        placeholders = ", ".join(["%s"] * len(normalized_source_ids))
        clauses.append(
            f"(source_id IN ({placeholders}) OR source_id IS NULL OR source_id = '')",
        )
        params.extend(normalized_source_ids)
    clauses.append("(COALESCE(NULLIF(provider_id, ''), %s) = %s)")
    params.extend([DEFAULT_PROVIDER_ID, _normalized_provider_id(provider_id)])
    clauses.append("(COALESCE(NULLIF(model_id, ''), %s) = %s)")
    params.extend([DEFAULT_MODEL_ID, _normalized_model_id(model_id)])
    return "AND " + " AND ".join(clauses), tuple(params)


def _build_single_scope_filter(
    scope: Mapping[str, Any] | None,
    *,
    table_alias: str = "",
) -> tuple[str, tuple[Any, ...]]:
    if not scope:
        return "", ()
    prefix = f"{table_alias}." if table_alias else ""
    source_id = str(scope.get("source_id") or "")
    provider_id = _normalized_provider_id(scope.get("provider_id"))
    model_id = _normalized_model_id(scope.get("model_id"))
    clauses = [
        f"COALESCE({prefix}source_id, '') = %s",
        f"COALESCE(NULLIF({prefix}provider_id, ''), %s) = %s",
        f"COALESCE(NULLIF({prefix}model_id, ''), %s) = %s",
    ]
    return (
        "AND " + " AND ".join(clauses),
        (
            source_id,
            DEFAULT_PROVIDER_ID,
            provider_id,
            DEFAULT_MODEL_ID,
            model_id,
        ),
    )


def _parse_json(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return None


def _select_scheduled_strategy_id(
    schedule: Any,
    now_utc: datetime,
) -> str:
    if not isinstance(schedule, list):
        return ""
    current_minutes = now_utc.hour * 60 + now_utc.minute
    for item in schedule:
        if not isinstance(item, Mapping):
            continue
        strategy_id = str(item.get("strategy_id") or "").strip()
        if not strategy_id:
            continue
        start = _clock_minutes(item.get("start_time"))
        end = _clock_minutes(item.get("end_time"))
        if start is None or end is None:
            continue
        if start <= end:
            matched = start <= current_minutes < end
        else:
            matched = current_minutes >= start or current_minutes < end
        if matched:
            return strategy_id
    return ""


def _clock_minutes(value: Any) -> int | None:
    if value is None:
        return None
    parts = str(value).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


async def _execute_write_return_last_id(
    sql: str,
    params: tuple[Any, ...],
) -> int:
    db = get_db_connection()
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                await cur.execute("SELECT LAST_INSERT_ID()")
                row = await cur.fetchone()
            await conn.commit()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            await conn.rollback()
            raise


_dispatch_intent_service: Optional[CronDispatchIntentService] = None


def get_cron_dispatch_intent_service() -> CronDispatchIntentService:
    """Return the cron dispatch intent service singleton."""
    global _dispatch_intent_service
    if _dispatch_intent_service is None:
        _dispatch_intent_service = CronDispatchIntentService()
    return _dispatch_intent_service
