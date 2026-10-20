# -*- coding: utf-8 -*-
"""Callback-driven cron batch scheduling service."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Mapping, NamedTuple, Optional
from uuid import uuid4

import httpx
from croniter import croniter
from pydantic import BaseModel, Field, field_validator
from zoneinfo import ZoneInfo

from scheduler.app.database import get_db_connection
from scheduler.config.constant import (
    DEFAULT_CAPACITY_CHECK_INTERVAL_SECONDS,
    DEFAULT_SCHEDULER_LOOP_INTERVAL_SECONDS,
    DISPATCHED_STALE_SECONDS_ENV,
    DISPATCH_INTENTS_ENABLED_ENV,
    SWE_API_BASE_URL,
)

from .dispatch_intent_service import (
    CronDispatchIntentService,
    DEFAULT_DISPATCHED_STALE_SECONDS,
    get_cron_dispatch_intent_service,
)
from .workflow_binding import resolve_workflow_batch_jobs

if TYPE_CHECKING:
    from .managed.feedback import ManagedCapacityFeedback

logger = logging.getLogger(__name__)

DISPATCH_CALLBACK_SOURCE = "dispatch_service"
DEFAULT_CAPACITY_ADJUST_INTERVAL_SECONDS = 300
DEFAULT_RETRY_DELAY_SECONDS = 300
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 30.0
DEFAULT_PROVIDER_ID = "default"
DEFAULT_MODEL_ID = "default"
HEADER_PREFIX = "x-header-"
B3_HEADER_NAMES = {
    "x-b3-businessid": "X-B3-BusinessId",
    "x-b3-debug": "X-B3-Debug",
    "x-b3-parentspanid": "X-B3-Parentspanid",
    "x-b3-sampled": "X-B3-Sampled",
    "x-b3-spanid": "X-B3-Spanid",
    "x-b3-timestamp": "X-B3-Timestamp",
    "x-b3-traceid": "X-B3-Traceid",
}
PASSTHROUGH_HEADERS_PAYLOAD_KEY = "passthrough_headers"
SWE_SERVER_DOMAIN_PAYLOAD_KEY = "swe_server_domain"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
BROADCAST_DISPATCH_INTENTS_ENABLED_META_KEY = (
    "broadcast_dispatch_intents_enabled"
)
BROADCAST_SOURCE_JOB_ID_META_KEY = "broadcast_source_job_id"
_BEIJING_TZ = timezone(timedelta(hours=8))


class _ParentCallbackContext(NamedTuple):
    now: datetime
    params: dict[str, Any]
    passthrough_headers: dict[str, str]
    job_id: str
    tenant_id: str
    source_id: str
    parent: dict[str, Any]
    parent_meta: dict[str, Any]
    provider_id: str
    model_id: str
    scheduled_fire_at: datetime
    children: list[dict[str, Any]]


class _ExecutionFeedbackContext(NamedTuple):
    intent_id: int
    dispatch_attempt: int | None
    batch_id: str
    job_id: str
    tenant_id: str
    source_id: str | None


class WorkerScope(BaseModel):
    """Worker capacity scope."""

    source_id: str = ""
    provider_id: str = DEFAULT_PROVIDER_ID
    model_id: str = DEFAULT_MODEL_ID

    @field_validator("provider_id", "model_id", mode="before")
    @classmethod
    def _default_blank(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text or DEFAULT_PROVIDER_ID


class WorkerStrategy(BaseModel):
    """DB-configured worker adjustment strategy."""

    strategy_id: str = "default"
    min_workers: int = 1
    baseline_workers: int = 1
    max_workers: int = 1
    adjust_interval_seconds: int = DEFAULT_CAPACITY_ADJUST_INTERVAL_SECONDS
    feedback_window_seconds: int = DEFAULT_CAPACITY_ADJUST_INTERVAL_SECONDS
    stale_execution_seconds: int = DEFAULT_DISPATCHED_STALE_SECONDS
    error_rate_rules: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator(
        "min_workers",
        "baseline_workers",
        "max_workers",
        "adjust_interval_seconds",
        "feedback_window_seconds",
        "stale_execution_seconds",
        mode="before",
    )
    @classmethod
    def _positive_int(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, parsed)


class SweCronCallbackOutcomeUnknownError(RuntimeError):
    """Callback may be accepted although its response was not observed."""

    def __init__(self, cause: httpx.TransportError) -> None:
        self.error_type = type(cause).__name__
        self.error_message = str(cause).strip() or repr(cause)
        super().__init__(
            f"SWE cron callback outcome unknown: "
            f"{self.error_type}: {self.error_message}",
        )


def _callback_skip_outcome(response: httpx.Response) -> str | None:
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(
            "SWE cron callback failed: "
            f"status={response.status_code} body={response.text[:512]}",
        )
    try:
        body = response.json()
    except (ValueError, AttributeError):
        body = {}
    skipped = body.get("skipped") if isinstance(body, dict) else None
    if skipped == "job_disabled":
        return skipped
    if skipped == "job_not_found":
        raise RuntimeError("SWE cron callback skipped: job_not_found")
    if skipped:
        raise SweCronCallbackOutcomeUnknownError(
            httpx.RemoteProtocolError(
                "Unrecognized SWE callback skip outcome",
            ),
        )
    return None


class SweCronCallbackClient:
    """Small client for SWE's internal cron callback."""

    def __init__(
        self,
        *,
        base_url: str = SWE_API_BASE_URL,
        timeout_seconds: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def dispatch_job(
        self,
        *,
        tenant_id: str,
        source_id: str,
        agent_id: str,
        job_id: str,
        dispatch_intent_id: int,
        dispatch_batch_id: str,
        dispatch_attempt: int,
        execution_key: str = "",
        parent_scheduled_fire_at: str = "",
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
        workflow_config_version: int | None = None,
        scope_id: str = "",
        from_id: str = "",
        swe_server_domain: str = "",
        passthrough_headers: Mapping[str, Any] | None = None,
    ) -> str | None:
        base_url = (swe_server_domain or self._base_url).rstrip("/")
        if not base_url:
            raise RuntimeError("SWE callback base URL is not configured")
        headers = _extract_b3_passthrough_headers(passthrough_headers)
        payload = {
            "tenant_id": tenant_id,
            "source_id": source_id,
            "agent_id": agent_id or "default",
            "task_type": "job",
            "job_id": job_id,
            "callback_source": DISPATCH_CALLBACK_SOURCE,
            "dispatch_intent_id": dispatch_intent_id,
            "dispatch_batch_id": dispatch_batch_id,
            "dispatch_attempt": dispatch_attempt,
            "provider_id": provider_id or DEFAULT_PROVIDER_ID,
            "model_id": model_id or DEFAULT_MODEL_ID,
            "scopeId": scope_id or _default_scope_id(tenant_id, source_id),
            "fromId": from_id or tenant_id,
        }
        if parent_scheduled_fire_at:
            payload["parent_scheduled_fire_at"] = parent_scheduled_fire_at
        if execution_key:
            payload["execution_key"] = execution_key
        if workflow_config_version is not None:
            payload["workflow_config_version"] = workflow_config_version
        logger.info(
            "scheduler_swe_callback_attempt batch_id=%s intent_id=%s "
            "job_id=%s attempt=%s provider_id=%s model_id=%s",
            dispatch_batch_id,
            dispatch_intent_id,
            job_id,
            dispatch_attempt,
            provider_id or DEFAULT_PROVIDER_ID,
            model_id or DEFAULT_MODEL_ID,
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.post(
                    f"{base_url}/api/internal/cron/callback",
                    json=payload,
                    headers=headers,
                )
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.LocalProtocolError,
            httpx.PoolTimeout,
            httpx.ProxyError,
            httpx.UnsupportedProtocol,
        ):
            raise
        except httpx.TransportError as exc:
            raise SweCronCallbackOutcomeUnknownError(exc) from exc
        return _callback_skip_outcome(response)


class CronSchedulingService:
    """Dispatch queued cron intents and adjust worker capacity separately."""

    def __init__(
        self,
        *,
        dispatch_store: CronDispatchIntentService | None = None,
        callback_client: SweCronCallbackClient | None = None,
        worker_id: str | None = None,
        baseline_workers: int = 1,
        max_workers: int = 1,
        effective_workers: int = 1,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        dispatched_stale_seconds: int = DEFAULT_DISPATCHED_STALE_SECONDS,
        capacity_adjust_interval_seconds: int = (
            DEFAULT_CAPACITY_ADJUST_INTERVAL_SECONDS
        ),
    ) -> None:
        self._dispatch_store = (
            dispatch_store or get_cron_dispatch_intent_service()
        )
        self._managed_feedback: ManagedCapacityFeedback | None = None
        self._callback_client = callback_client or SweCronCallbackClient()
        self._worker_id = worker_id or _default_worker_id()
        self._fallback_strategy = WorkerStrategy(
            strategy_id="default",
            min_workers=1,
            baseline_workers=max(1, baseline_workers),
            max_workers=max(1, baseline_workers, max_workers),
            adjust_interval_seconds=max(1, capacity_adjust_interval_seconds),
            feedback_window_seconds=max(1, capacity_adjust_interval_seconds),
            stale_execution_seconds=max(60, dispatched_stale_seconds),
            error_rate_rules=[
                {
                    "min_error_rate": 0.000001,
                    "operation": "multiply",
                    "value": 0.5,
                    "reason": "terminal_errors",
                },
                {
                    "min_error_rate": 0,
                    "max_error_rate": 0,
                    "operation": "add",
                    "value": 1,
                    "reason": "stable_success",
                },
            ],
        )
        self._retry_delay_seconds = max(0, retry_delay_seconds)
        self._last_effective_workers = _clamp(
            effective_workers,
            self._fallback_strategy.min_workers,
            self._fallback_strategy.max_workers,
        )

    @property
    def effective_workers(self) -> int:
        return self._last_effective_workers

    def bind_managed_feedback(self, feedback: ManagedCapacityFeedback) -> None:
        """新结构只读校验完成后接入共库反馈，仍只使用本容量调整器。"""
        self._managed_feedback = feedback
        logger.info("新旧执行反馈已接入同一模型池容量调整器")

    async def _managed_capacity_call(self, name: str, **kwargs: Any) -> Any:
        try:
            return await getattr(self._managed_feedback, name)(**kwargs)
        except Exception as exc:
            logger.warning(
                "新执行容量反馈未确认，保留已有容量 operation=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            raise RuntimeError("cron_managed_feedback_unavailable") from None

    async def _capacity_scopes(self, now: datetime) -> list[WorkerScope]:
        scopes = await self._list_dispatch_scopes(
            now,
            None,
            include_fallback=False,
        )
        if self._managed_feedback is None:
            return scopes
        raw = await self._managed_capacity_call("scopes", now_utc=now)
        seen = {
            (scope.source_id, scope.provider_id, scope.model_id)
            for scope in scopes
        }
        for item in raw:
            scope = WorkerScope.model_validate(item)
            key = (scope.source_id, scope.provider_id, scope.model_id)
            if key not in seen:
                seen.add(key)
                scopes.append(scope)
        return scopes

    async def _capacity_feedback(
        self,
        since: datetime,
        now: datetime,
        scope: WorkerScope,
    ) -> dict[str, Any]:
        old_request = (
            self._dispatch_store.summarize_recent_completion_feedback(
                since=since,
                now_utc=now,
                scope=scope.model_dump(),
            )
        )
        if self._managed_feedback is None:
            return await old_request
        tasks = [
            asyncio.create_task(old_request),
            asyncio.create_task(
                self._managed_capacity_call(
                    "summarize",
                    since=since,
                    now_utc=now,
                    scope=scope.model_dump(),
                ),
            ),
        ]
        try:
            old, new = await asyncio.gather(*tasks)
        finally:
            # 一边失败或外层取消后收拢另一边，不能让数据库读取遗留到连接池关闭之后。
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        names = (
            "pending_count",
            "claimed_count",
            "running_count",
            "success_count",
            "failure_count",
        )
        return {
            **{
                name: int(old.get(name) or 0) + int(new.get(name) or 0)
                for name in names
            },
            "latency_p95_ms": int(old.get("latency_p95_ms") or 0),
        }

    async def handle_parent_callback(
        self,
        *,
        params: Mapping[str, Any],
        headers: Mapping[str, Any] | None = None,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Handle an external scheduler callback for a batch parent."""
        context = await _prepare_parent_callback_context(
            params=params,
            headers=headers,
            now_utc=now_utc,
        )
        batch_id = _build_dispatch_batch_id(
            context.parent,
            context.scheduled_fire_at,
        )
        callback_metadata = _build_callback_metadata(
            context.params,
            context.passthrough_headers,
        )
        callback_metadata["batch_dispatch_priority"] = context.parent_meta.get(
            "batch_dispatch_priority",
            {},
        )
        batch_data = {
            "batch_id": batch_id,
            "parent_job_id": context.job_id,
            "parent_external_job_id": str(
                context.parent_meta.get("external_job_id") or "",
            ),
            "tenant_id": str(
                context.parent.get("tenant_id") or context.tenant_id or "",
            ),
            "source_id": str(
                context.parent.get("source_id") or context.source_id or "",
            ),
            "agent_id": str(context.parent.get("agent_id") or "default"),
            "provider_id": context.provider_id,
            "model_id": context.model_id,
            "scheduled_fire_at": context.scheduled_fire_at,
            "callback_received_at": context.now,
            "callback_metadata": callback_metadata,
        }
        _apply_swe_server_domain(context.parent, context.params)
        jobs = _build_execution_intent_jobs(
            context.parent,
            context.children,
            context.scheduled_fire_at,
            passthrough_headers=context.passthrough_headers,
        )
        job_mappings: list[Mapping[str, Any]] = []
        job_mappings.extend(jobs)
        dispatch_scopes = _build_worker_scopes_from_jobs(job_mappings)
        dispatch_source_ids = _source_ids_from_jobs(
            job_mappings,
            fallback=str(
                context.parent.get("source_id") or context.source_id or "",
            ),
        )
        admission = await self._dispatch_store.create_batch_with_intents(
            batch=batch_data,
            jobs=jobs,
            due_at=context.now,
            scheduled_fire_at=context.scheduled_fire_at,
        )
        if admission.get("skipped"):
            return {
                "parent_job_id": context.job_id,
                "skipped": admission["skipped"],
                "enqueued_intents": 0,
                "dispatched_intents": 0,
            }
        intent_ids = admission["intent_ids"]
        await self._dispatch_store.update_batch_counts(
            batch_id=batch_id,
            updated_at=context.now,
        )
        claim_scopes: list[WorkerScope | Mapping[str, Any]] = []
        claim_scopes.extend(dispatch_scopes)
        dispatched = await self.dispatch_ready_once(
            now_utc=context.now,
            source_ids=dispatch_source_ids,
            scopes=claim_scopes,
        )
        return {
            "batch_id": batch_id,
            "parent_job_id": context.job_id,
            "child_count": len(context.children),
            "enqueued_intents": len(intent_ids),
            "dispatched_intents": dispatched,
        }

    async def dispatch_ready_once(
        self,
        *,
        now_utc: datetime | None = None,
        source_ids: list[str] | None = None,
        scopes: list[WorkerScope | Mapping[str, Any]] | None = None,
    ) -> int:
        """Dispatch ready work without adjusting worker capacity."""
        now = _ensure_aware_utc(now_utc or datetime.now(timezone.utc))
        total_dispatched = 0
        dispatch_scopes = _normalize_worker_scopes(scopes)
        if not dispatch_scopes:
            dispatch_scopes = await self._list_dispatch_scopes(
                now,
                source_ids,
                include_fallback=bool(source_ids),
            )
        for scope in dispatch_scopes:
            scope_source_ids = (
                [scope.source_id] if scope.source_id else source_ids
            )
            strategy = await self._resolve_worker_strategy(scope, now)
            if not await self._acquire_scope_lease(scope, strategy, now):
                continue
            await self._dispatch_store.reconcile_dispatched_executions(
                now_utc=now,
                retry_delay_seconds=self._retry_delay_seconds,
                source_ids=scope_source_ids,
                provider_id=scope.provider_id,
                model_id=scope.model_id,
            )
            await self._dispatch_store.recover_stale_dispatched_intents(
                now_utc=now,
                dispatched_stale_seconds=strategy.stale_execution_seconds,
                source_ids=scope_source_ids,
                provider_id=scope.provider_id,
                model_id=scope.model_id,
            )
            effective_workers = await self._effective_workers_for_scope(
                scope,
                strategy,
                now,
            )
            feedback = await self._dispatch_store.summarize_recent_completion_feedback(
                since=now,
                now_utc=now,
                scope=scope.model_dump(),
            )
            in_flight = int(feedback.get("claimed_count") or 0) + int(
                feedback.get("running_count") or 0,
            )
            available_slots = max(0, effective_workers - in_flight)
            if available_slots <= 0:
                continue
            rows = await self._dispatch_store.claim_due_intents(
                lock_owner=self._worker_id,
                now_utc=now,
                limit=available_slots,
                capacity={
                    "effective_workers": effective_workers,
                    "strategy_id": strategy.strategy_id,
                    "min_workers": strategy.min_workers,
                    "max_workers": strategy.max_workers,
                },
                dispatched_stale_seconds=strategy.stale_execution_seconds,
                source_ids=scope_source_ids,
                provider_id=scope.provider_id,
                model_id=scope.model_id,
            )
            for row in rows:
                if await self._dispatch_one(row, now):
                    total_dispatched += 1
        return total_dispatched

    async def run_scheduler_once(
        self,
        *,
        now_utc: datetime | None = None,
        source_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run one maintenance tick for due retries and worker capacity."""
        now = _ensure_aware_utc(now_utc or datetime.now(timezone.utc))
        dispatched = await self.dispatch_ready_once(
            now_utc=now,
            source_ids=source_ids,
        )
        capacity_adjusted = await self.adjust_worker_capacity_if_due(
            now_utc=now,
        )
        return {
            "queued_parent_intents": 0,
            "dispatched_intents": dispatched,
            "capacity_adjusted": capacity_adjusted,
        }

    async def run_loop(
        self,
        *,
        stop_event: asyncio.Event,
        interval_seconds: int = DEFAULT_SCHEDULER_LOOP_INTERVAL_SECONDS,
        capacity_interval_seconds: int = (
            DEFAULT_CAPACITY_CHECK_INTERVAL_SECONDS
        ),
        source_ids: list[str] | None = None,
    ) -> None:
        """Run independent dispatch and capacity loops until stopped."""
        await asyncio.gather(
            self._run_dispatch_loop(
                stop_event=stop_event,
                interval_seconds=interval_seconds,
                source_ids=source_ids,
            ),
            self._run_capacity_loop(
                stop_event=stop_event,
                interval_seconds=capacity_interval_seconds,
            ),
        )

    async def _run_dispatch_loop(
        self,
        *,
        stop_event: asyncio.Event,
        interval_seconds: int,
        source_ids: list[str] | None,
    ) -> None:
        interval = max(1, interval_seconds)
        while not stop_event.is_set():
            try:
                await self.dispatch_ready_once(
                    source_ids=source_ids,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Cron dispatch loop tick failed",
                    exc_info=True,
                )
            await _wait_for_next_loop_tick(stop_event, interval)

    async def _run_capacity_loop(
        self,
        *,
        stop_event: asyncio.Event,
        interval_seconds: int,
    ) -> None:
        interval = max(1, interval_seconds)
        while not stop_event.is_set():
            try:
                await self.adjust_worker_capacity_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Cron capacity loop tick failed",
                    exc_info=True,
                )
            await _wait_for_next_loop_tick(stop_event, interval)

    async def enqueue_due_parent_intents_once(
        self,
        *,
        now_utc: datetime | None = None,
        source_ids: list[str] | None = None,
        lookback_seconds: int | None = None,
    ) -> int:
        """Deprecated scanner entry point kept as a no-op."""
        del now_utc, source_ids, lookback_seconds
        return 0

    async def _dispatch_one(self, row: Any, now_utc: datetime) -> bool:
        try:
            outcome = await self._dispatch_store.prepare_handoff(
                row,
                self._worker_id,
                now_utc,
            )
        except Exception:
            logger.warning(
                "Dispatch gate unavailable; claim will be recovered without model penalty",
                exc_info=True,
            )
            return False
        if outcome != "ready":
            return False
        try:
            role = _row_get(row, "intent_role")
            if role not in {"parent", "child"}:
                return await self._record_callback_rejection(
                    row,
                    now_utc,
                    RuntimeError(f"unsupported dispatch intent role: {role}"),
                )
            return await self._dispatch_execution_intent(row, now_utc)
        except Exception:  # An acknowledged callback may already be running.
            logger.warning(
                "Callback settlement unavailable; preserving handoff occupancy",
                exc_info=True,
            )
            return False

    async def _record_callback_rejection(self, row, now_utc, exc) -> bool:
        intent_id = int(_row_get(row, "id") or 0)
        batch_id = str(_row_get(row, "batch_id") or "")
        failed = await self._dispatch_store.fail_intent(
            intent_id=intent_id,
            worker_id=self._worker_id,
            error=str(exc),
            failed_at=now_utc,
            retry_delay_seconds=self._retry_delay_seconds,
            **(
                {"claim_token": _row_get(row, "claim_token")}
                if _row_get(row, "claim_token")
                else {}
            ),
        )
        if failed and batch_id:
            await self._dispatch_store.update_batch_counts(
                batch_id=batch_id,
                updated_at=now_utc,
            )
        return False

    async def _dispatch_execution_intent(
        self,
        row: Any,
        now_utc: datetime,
    ) -> bool:
        payload = _row_payload(row)
        callback_kwargs = _build_execution_callback_kwargs(row, payload)
        passthrough_headers = _extract_b3_passthrough_headers(
            payload.get(PASSTHROUGH_HEADERS_PAYLOAD_KEY),
        )
        if passthrough_headers:
            callback_kwargs["passthrough_headers"] = passthrough_headers
        try:
            skipped = await self._callback_client.dispatch_job(
                **callback_kwargs,
            )
        except SweCronCallbackOutcomeUnknownError as exc:
            details = _dispatch_mark_details(callback_kwargs)
            details.update(
                {
                    "callback_outcome": "unknown",
                    "error_type": exc.error_type,
                    "error": exc.error_message,
                },
            )
            await self._dispatch_store.mark_intent_dispatch_unknown(
                intent_id=int(callback_kwargs["dispatch_intent_id"]),
                worker_id=self._worker_id,
                observed_at=now_utc,
                details=details,
                **(
                    {"claim_token": _row_get(row, "claim_token")}
                    if _row_get(row, "claim_token")
                    else {}
                ),
            )
            logger.warning(
                "scheduler_swe_callback_outcome_unknown batch_id=%s "
                "intent_id=%s job_id=%s attempt=%s error_type=%s",
                callback_kwargs["dispatch_batch_id"],
                callback_kwargs["dispatch_intent_id"],
                callback_kwargs["job_id"],
                callback_kwargs["dispatch_attempt"],
                exc.error_type,
            )
            return True
        except Exception as exc:
            return await self._record_callback_rejection(row, now_utc, exc)
        if skipped == "job_disabled":
            await self._dispatch_store.settle_callback_skip(
                row,
                self._worker_id,
                now_utc,
            )
            return False
        await self._dispatch_store.mark_intent_dispatched(
            intent_id=int(callback_kwargs["dispatch_intent_id"]),
            worker_id=self._worker_id,
            dispatched_at=now_utc,
            details=_dispatch_mark_details(callback_kwargs),
            **(
                {"claim_token": _row_get(row, "claim_token")}
                if _row_get(row, "claim_token")
                else {}
            ),
        )
        return True

    async def handle_execution_recorded(
        self,
        *,
        execution_id: int | None,
        status: str,
        meta: Mapping[str, Any] | str | None,
        job_id: str = "",
        tenant_id: str = "",
        source_id: str | None = None,
        error_message: str = "",
        completed_at: datetime | None = None,
    ) -> bool:
        """Validate SWE feedback before persistence; the loop settles results."""
        feedback = _build_execution_feedback_context(
            meta=meta,
            job_id=job_id,
            tenant_id=tenant_id,
            source_id=source_id,
        )
        if feedback is None:
            return False
        logger.info(
            "scheduler_execution_feedback intent_id=%s batch_id=%s "
            "job_id=%s status=%s",
            feedback.intent_id,
            feedback.batch_id,
            feedback.job_id,
            status,
        )
        return await self._dispatch_store.accept_execution_feedback(
            intent_id=feedback.intent_id,
            execution_id=execution_id,
            expected_batch_id=feedback.batch_id,
            expected_job_id=feedback.job_id,
            expected_tenant_id=feedback.tenant_id,
            expected_source_id=feedback.source_id,
            expected_attempt_count=feedback.dispatch_attempt,
        )

    async def adjust_worker_capacity_if_due(
        self,
        *,
        now_utc: datetime | None = None,
    ) -> bool:
        """Adjust worker capacity when each scope's strategy interval elapsed."""
        now = _ensure_aware_utc(now_utc or datetime.now(timezone.utc))
        adjusted = False
        if self._managed_feedback is not None:
            await self._managed_capacity_call("tick")
        scopes = await self._capacity_scopes(now)
        for scope in scopes:
            strategy = await self._resolve_worker_strategy(scope, now)
            latest = await self._dispatch_store.get_latest_worker_capacity(
                scope=scope.model_dump(),
                strategy_id=strategy.strategy_id,
            )
            if not _capacity_adjustment_is_due(latest, strategy, now):
                continue
            if not await self._acquire_scope_lease(scope, strategy, now):
                continue
            latest = await self._dispatch_store.get_latest_worker_capacity(
                scope=scope.model_dump(),
                strategy_id=strategy.strategy_id,
            )
            if not _capacity_adjustment_is_due(latest, strategy, now):
                continue
            previous = _capacity_effective_workers(latest, strategy)
            since = now - timedelta(seconds=strategy.feedback_window_seconds)
            feedback = await self._capacity_feedback(since, now, scope)
            success_count = int(feedback.get("success_count") or 0)
            failure_count = int(feedback.get("failure_count") or 0)
            terminal_count = success_count + failure_count
            error_rate = failure_count / max(1, terminal_count)
            if terminal_count == 0:
                next_workers = previous
                reason = "no_terminal_feedback"
                matched_rule: dict[str, Any] = {}
            else:
                matched_rule = _match_error_rate_rule(
                    strategy.error_rate_rules,
                    error_rate,
                )
                next_workers, reason = _apply_worker_rule(
                    previous,
                    matched_rule,
                    strategy,
                )
            logger.info(
                "scheduler_worker_adjustment source_id=%s provider_id=%s "
                "model_id=%s strategy_id=%s previous=%s next=%s "
                "error_rate=%.4f reason=%s",
                scope.source_id,
                scope.provider_id,
                scope.model_id,
                strategy.strategy_id,
                previous,
                next_workers,
                error_rate,
                reason,
            )
            await self._dispatch_store.record_worker_capacity(
                worker_id=self._worker_id,
                source_id=scope.source_id,
                provider_id=scope.provider_id,
                model_id=scope.model_id,
                strategy_id=strategy.strategy_id,
                previous_workers=previous,
                baseline_workers=strategy.baseline_workers,
                min_workers=strategy.min_workers,
                max_workers=strategy.max_workers,
                effective_workers=next_workers,
                pending_count=int(feedback.get("pending_count") or 0),
                claimed_count=int(feedback.get("claimed_count") or 0),
                running_count=int(feedback.get("running_count") or 0),
                success_count=success_count,
                failure_count=failure_count,
                error_rate=error_rate,
                matched_rule=matched_rule,
                avg_latency_ms=int(feedback.get("latency_p95_ms") or 0),
                decision_reason=reason,
                recorded_at=now,
            )
            self._last_effective_workers = next_workers
            adjusted = True
        return adjusted

    async def _list_dispatch_scopes(
        self,
        now_utc: datetime,
        source_ids: list[str] | None,
        *,
        include_fallback: bool = True,
    ) -> list[WorkerScope]:
        raw_scopes = await self._dispatch_store.list_dispatch_scopes(
            now_utc=now_utc,
            source_ids=source_ids,
        )
        scopes = [WorkerScope.model_validate(scope) for scope in raw_scopes]
        if scopes or not include_fallback:
            return scopes
        if source_ids:
            return [
                WorkerScope(
                    source_id=source_id,
                    provider_id=DEFAULT_PROVIDER_ID,
                    model_id=DEFAULT_MODEL_ID,
                )
                for source_id in source_ids
            ]
        return [
            WorkerScope(
                source_id="",
                provider_id=DEFAULT_PROVIDER_ID,
                model_id=DEFAULT_MODEL_ID,
            ),
        ]

    async def _resolve_worker_strategy(
        self,
        scope: WorkerScope,
        now_utc: datetime,
    ) -> WorkerStrategy:
        if not hasattr(self._dispatch_store, "resolve_worker_strategy"):
            return self._fallback_strategy
        raw = await self._dispatch_store.resolve_worker_strategy(
            scope=scope.model_dump(),
            now_utc=now_utc,
            fallback=self._fallback_strategy.model_dump(),
        )
        strategy = WorkerStrategy.model_validate(
            raw or self._fallback_strategy,
        )
        strategy.min_workers = max(1, strategy.min_workers)
        strategy.baseline_workers = _clamp(
            strategy.baseline_workers,
            strategy.min_workers,
            max(strategy.min_workers, strategy.max_workers),
        )
        strategy.max_workers = max(
            strategy.baseline_workers,
            strategy.max_workers,
        )
        return strategy

    async def _effective_workers_for_scope(
        self,
        scope: WorkerScope,
        strategy: WorkerStrategy,
        now_utc: datetime,
    ) -> int:
        latest = await self._dispatch_store.get_latest_worker_capacity(
            scope=scope.model_dump(),
            strategy_id=strategy.strategy_id,
        )
        if latest:
            effective = _capacity_effective_workers(latest, strategy)
            self._last_effective_workers = effective
            return effective
        effective = strategy.baseline_workers
        logger.info(
            "scheduler_worker_initial_capacity source_id=%s provider_id=%s "
            "model_id=%s strategy_id=%s workers=%s",
            scope.source_id,
            scope.provider_id,
            scope.model_id,
            strategy.strategy_id,
            effective,
        )
        await self._dispatch_store.record_worker_capacity(
            worker_id=self._worker_id,
            source_id=scope.source_id,
            provider_id=scope.provider_id,
            model_id=scope.model_id,
            strategy_id=strategy.strategy_id,
            previous_workers=effective,
            baseline_workers=strategy.baseline_workers,
            min_workers=strategy.min_workers,
            max_workers=strategy.max_workers,
            effective_workers=effective,
            pending_count=0,
            claimed_count=0,
            running_count=0,
            success_count=0,
            failure_count=0,
            error_rate=0,
            matched_rule={},
            avg_latency_ms=0,
            decision_reason="initial_capacity",
            recorded_at=now_utc,
        )
        self._last_effective_workers = effective
        return effective

    async def _acquire_scope_lease(
        self,
        scope: WorkerScope,
        strategy: WorkerStrategy,
        now_utc: datetime,
    ) -> bool:
        if not hasattr(self._dispatch_store, "acquire_scope_lease"):
            return True
        lease_seconds = max(
            strategy.adjust_interval_seconds,
            min(strategy.stale_execution_seconds, 300),
            60,
        )
        return bool(
            await self._dispatch_store.acquire_scope_lease(
                source_id=scope.source_id,
                provider_id=scope.provider_id,
                model_id=scope.model_id,
                worker_id=self._worker_id,
                now_utc=now_utc,
                lease_seconds=lease_seconds,
            ),
        )


def _extract_dispatch_meta(
    meta: Mapping[str, Any] | str | None,
) -> dict[str, Any]:
    if not meta:
        return {}
    parsed: Any = meta
    if isinstance(meta, str):
        try:
            parsed = json.loads(meta)
        except json.JSONDecodeError:
            return {}
    if not isinstance(parsed, Mapping):
        return {}
    value = parsed.get("cron_dispatch")
    return dict(value) if isinstance(value, Mapping) else {}


def _requested_callback_fire_at(
    callback_params: Mapping[str, Any],
) -> datetime | None:
    explicit_fire_at = _parse_datetime(
        callback_params.get("scheduled_fire_at"),
    )
    trigger_fire_at = _parse_datetime(
        callback_params.get("fire_time")
        or callback_params.get("trigger_time"),
    )
    if explicit_fire_at is not None or trigger_fire_at is None:
        return explicit_fire_at
    offset_minutes = (
        _positive_int(callback_params.get("batch_dispatch_offset_minutes"))
        or 0
    )
    if offset_minutes <= 0:
        return trigger_fire_at
    return trigger_fire_at + timedelta(minutes=offset_minutes)


async def _prepare_parent_callback_context(
    *,
    params: Mapping[str, Any],
    headers: Mapping[str, Any] | None,
    now_utc: datetime | None,
) -> _ParentCallbackContext:
    now = _ensure_aware_utc(now_utc or datetime.now(timezone.utc))
    callback_params = _decode_callback_params(params)
    passthrough_headers = _extract_b3_passthrough_headers(headers)
    job_id, tenant_id, source_id = _callback_job_identity(callback_params)
    parent = await _load_parent_callback_job(
        job_id=job_id,
        tenant_id=tenant_id,
        source_id=source_id,
    )
    scheduled_fire_at = _callback_scheduled_fire_at(
        callback_params,
        parent,
        now,
    )
    logger.info(
        "scheduler_parent_callback_received job_id=%s tenant_id=%s "
        "source_id=%s scheduled_fire_at=%s passthrough_headers=%s",
        job_id,
        tenant_id,
        source_id,
        scheduled_fire_at.isoformat(),
        sorted(passthrough_headers),
    )
    parent_meta = _validated_parent_callback_meta(parent, job_id)
    provider_id, model_id = _apply_callback_parent_identity(
        parent,
        parent_meta,
        callback_params,
    )
    children = await _fetch_batch_child_jobs(parent)
    batch_jobs = [parent, *children]
    if any(job.get("task_type") == "workflow" for job in batch_jobs):
        await resolve_workflow_batch_jobs(
            batch_jobs,
            get_db_connection(),
        )
    if parent.get("task_type") == "workflow":
        provider_id = str(parent["provider_id"])
        model_id = str(parent["model_id"])
    logger.info(
        "scheduler_parent_jobs_fetched parent_job_id=%s child_count=%s",
        job_id,
        len(children),
    )
    return _ParentCallbackContext(
        now=now,
        params=callback_params,
        passthrough_headers=passthrough_headers,
        job_id=job_id,
        tenant_id=tenant_id,
        source_id=source_id,
        parent=parent,
        parent_meta=parent_meta,
        provider_id=provider_id,
        model_id=model_id,
        scheduled_fire_at=scheduled_fire_at,
        children=children,
    )


def _callback_job_identity(
    callback_params: Mapping[str, Any],
) -> tuple[str, str, str]:
    job_id = str(callback_params.get("job_id") or "").strip()
    if not job_id:
        raise RuntimeError("scheduler callback requires job_id")
    tenant_id = str(callback_params.get("tenant_id") or "").strip()
    source_id = str(callback_params.get("source_id") or "").strip()
    return job_id, tenant_id, source_id


async def _load_parent_callback_job(
    *,
    job_id: str,
    tenant_id: str,
    source_id: str,
) -> dict[str, Any]:
    parent = await _fetch_parent_job_for_callback(
        job_id=job_id,
        tenant_id=tenant_id,
        source_id=source_id,
    )
    if not parent:
        raise RuntimeError(f"batch parent job not found: {job_id}")
    return parent


def _callback_scheduled_fire_at(
    callback_params: Mapping[str, Any],
    parent: Mapping[str, Any],
    now: datetime,
) -> datetime:
    requested_fire_at = _requested_callback_fire_at(callback_params)
    if requested_fire_at is not None:
        return _ensure_aware_utc(requested_fire_at)

    cron_expr = str(parent.get("cron_expr") or "")
    timezone_name = str(parent.get("timezone") or "UTC")
    offset_minutes = (
        _positive_int(callback_params.get("batch_dispatch_offset_minutes"))
        or 0
    )
    if offset_minutes > 0:
        # The batch timer fires before the parent cron. When the external
        # callback omits its trigger time, receipt time can identify the next
        # parent occurrence only while it remains inside that pre-fire window.
        upcoming_fire_at = _next_due_fire_at(
            cron_expr=cron_expr,
            timezone_name=timezone_name,
            now_utc=now,
        )
        if upcoming_fire_at is not None:
            until_upcoming = upcoming_fire_at - _ensure_aware_utc(now)
            if (
                timedelta(0)
                <= until_upcoming
                <= timedelta(
                    minutes=offset_minutes,
                )
            ):
                return upcoming_fire_at

    return _ensure_aware_utc(
        _previous_due_fire_at(
            cron_expr=cron_expr,
            timezone_name=timezone_name,
            now_utc=now,
        )
        or now,
    )


def _validated_parent_callback_meta(
    parent: Mapping[str, Any],
    job_id: str,
) -> dict[str, Any]:
    parent_meta = _parse_meta(parent.get("meta"))
    if not _meta_dispatch_intents_enabled(parent_meta):
        raise RuntimeError(f"batch parent dispatch disabled: {job_id}")
    if str(parent_meta.get(BROADCAST_SOURCE_JOB_ID_META_KEY) or ""):
        raise RuntimeError(f"batch callback received child job: {job_id}")
    return parent_meta


def _apply_callback_parent_identity(
    parent: dict[str, Any],
    parent_meta: Mapping[str, Any],
    callback_params: Mapping[str, Any],
) -> tuple[str, str]:
    agent_id = str(callback_params.get("agent_id") or "default").strip()
    parent["agent_id"] = agent_id or str(
        parent_meta.get("agent_id") or "default",
    )
    provider_id = str(callback_params.get("provider_id") or "").strip()
    model_id = str(callback_params.get("model_id") or "").strip()
    if provider_id and model_id:
        parent["provider_id"] = provider_id
        parent["model_id"] = model_id
    return _extract_model_identity(parent)


def _build_callback_metadata(
    callback_params: Mapping[str, Any],
    passthrough_headers: Mapping[str, str],
) -> dict[str, Any]:
    metadata = dict(callback_params)
    if passthrough_headers:
        metadata[PASSTHROUGH_HEADERS_PAYLOAD_KEY] = dict(passthrough_headers)
    return metadata


def _apply_swe_server_domain(
    parent: dict[str, Any],
    callback_params: Mapping[str, Any],
) -> None:
    swe_server_domain = str(
        callback_params.get(SWE_SERVER_DOMAIN_PAYLOAD_KEY) or "",
    ).strip()
    if swe_server_domain:
        parent[SWE_SERVER_DOMAIN_PAYLOAD_KEY] = swe_server_domain


def _build_execution_feedback_context(
    *,
    meta: Mapping[str, Any] | str | None,
    job_id: str,
    tenant_id: str,
    source_id: str | None,
) -> _ExecutionFeedbackContext | None:
    dispatch_meta = _extract_dispatch_meta(meta)
    if not dispatch_meta:
        return None
    intent_id = _positive_int(dispatch_meta.get("intent_id"))
    if intent_id is None:
        return None
    expected_source_id = (
        str(dispatch_meta["source_id"])
        if "source_id" in dispatch_meta
        else source_id
    )
    return _ExecutionFeedbackContext(
        intent_id=intent_id,
        dispatch_attempt=_positive_int(dispatch_meta.get("dispatch_attempt")),
        batch_id=str(dispatch_meta.get("batch_id") or ""),
        job_id=str(dispatch_meta.get("job_id") or job_id or ""),
        tenant_id=str(dispatch_meta.get("tenant_id") or tenant_id or ""),
        source_id=expected_source_id,
    )


async def _fetch_parent_job_for_callback(
    *,
    job_id: str,
    tenant_id: str = "",
    source_id: str = "",
) -> dict[str, Any] | None:
    db = get_db_connection()
    clauses = [
        "id = %s",
        "status IN ('active', 'paused')",
        "deleted_at IS NULL",
    ]
    params: list[Any] = [job_id]
    if tenant_id:
        clauses.append("(tenant_id = %s OR tenant_id = '')")
        params.append(tenant_id)
    if source_id:
        clauses.append("(source_id = %s OR source_id = '')")
        params.append(source_id)
    row = await db.fetch_one(
        f"""
        SELECT id, tenant_id, source_id, task_type, workflow_binding_id,
               cron_expr, timezone, meta, enabled, status, deleted_at
        FROM swe_cron_jobs
        WHERE {' AND '.join(clauses)}
        LIMIT 1
        """,
        tuple(params),
    )
    return dict(row) if row else None


async def _fetch_batch_child_jobs(
    parent: Mapping[str, Any],
) -> list[dict[str, Any]]:
    db = get_db_connection()
    parent_id = str(parent.get("id") or "")
    rows = await db.fetch_all(
        """
        SELECT id, tenant_id, source_id, task_type, workflow_binding_id, meta
        FROM swe_cron_jobs
        WHERE enabled = 1
          AND status = 'active'
          AND deleted_at IS NULL
          AND meta LIKE %s
        """,
        (f"%{parent_id}%",),
    )
    children: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        meta = _parse_meta(item.get("meta"))
        if str(meta.get(BROADCAST_SOURCE_JOB_ID_META_KEY) or "") != parent_id:
            continue
        if not _meta_dispatch_intents_enabled(meta):
            continue
        provider_id, model_id = _extract_model_identity(item)
        children.append(
            {
                "tenant_id": str(item.get("tenant_id") or ""),
                "job_id": str(item.get("id") or ""),
                "source_id": str(
                    item.get("source_id") or parent.get("source_id") or "",
                ),
                "task_type": str(item.get("task_type") or ""),
                "workflow_binding_id": item.get("workflow_binding_id"),
                "agent_id": str(
                    meta.get("agent_id")
                    or parent.get("agent_id")
                    or "default",
                ),
                "provider_id": provider_id,
                "model_id": model_id,
            },
        )
    children.sort(key=lambda child: (child["tenant_id"], child["job_id"]))
    return children


async def _fetch_due_parent_jobs(
    *,
    now_utc: datetime,
    source_ids: list[str] | None,
    lookback_seconds: int,
) -> list[dict[str, Any]]:
    """Deprecated scanner hook. Kept only for compatibility with old tests."""
    del now_utc, source_ids, lookback_seconds
    return []


def _build_execution_intent_jobs(
    parent: Mapping[str, Any],
    children: list[dict[str, Any]],
    scheduled_fire_at: datetime,
    passthrough_headers: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    parent_provider_id, parent_model_id = _extract_model_identity(parent)
    scheduled = scheduled_fire_at.isoformat()
    parent_job_id = str(parent.get("id") or "")
    transport = _execution_transport_context(parent, passthrough_headers)
    jobs: list[dict[str, Any]] = [
        _build_parent_execution_intent(
            parent,
            parent_job_id=parent_job_id,
            provider_id=parent_provider_id,
            model_id=parent_model_id,
            scheduled=scheduled,
            transport=transport,
        ),
    ]
    jobs.extend(
        _build_child_execution_intent(
            child,
            parent_job_id=parent_job_id,
            parent_provider_id=parent_provider_id,
            parent_model_id=parent_model_id,
            scheduled=scheduled,
            transport=transport,
        )
        for child in children
    )
    return jobs


def _execution_transport_context(
    parent: Mapping[str, Any],
    passthrough_headers: Mapping[str, Any] | None,
) -> tuple[dict[str, str], str]:
    return (
        _extract_b3_passthrough_headers(passthrough_headers),
        str(parent.get(SWE_SERVER_DOMAIN_PAYLOAD_KEY) or "").strip(),
    )


def _base_execution_payload(
    job: Mapping[str, Any],
    *,
    job_id: str,
    provider_id: str,
    model_id: str,
    scheduled: str,
) -> dict[str, Any]:
    payload = {
        "tenant_id": str(job.get("tenant_id") or ""),
        "job_id": job_id,
        "source_id": str(job.get("source_id") or ""),
        "agent_id": str(job.get("agent_id") or "default"),
        "provider_id": provider_id,
        "model_id": model_id,
        "parent_scheduled_fire_at": scheduled,
    }
    if job.get("task_type") == "workflow":
        payload["workflow_config_version"] = int(
            job["workflow_config_version"],
        )
    return payload


def _apply_execution_transport(
    payload: dict[str, Any],
    transport: tuple[dict[str, str], str],
) -> dict[str, Any]:
    passthrough_headers, swe_server_domain = transport
    if passthrough_headers:
        payload[PASSTHROUGH_HEADERS_PAYLOAD_KEY] = dict(passthrough_headers)
    if swe_server_domain:
        payload[SWE_SERVER_DOMAIN_PAYLOAD_KEY] = swe_server_domain
    return payload


def _build_parent_execution_intent(
    parent: Mapping[str, Any],
    *,
    parent_job_id: str,
    provider_id: str,
    model_id: str,
    scheduled: str,
    transport: tuple[dict[str, str], str],
) -> dict[str, Any]:
    payload = _base_execution_payload(
        parent,
        job_id=parent_job_id,
        provider_id=provider_id,
        model_id=model_id,
        scheduled=scheduled,
    )
    return {
        "intent_role": "parent",
        "tenant_id": str(parent.get("tenant_id") or ""),
        "job_id": parent_job_id,
        "parent_job_id": "",
        "source_id": str(parent.get("source_id") or ""),
        "agent_id": str(parent.get("agent_id") or "default"),
        "provider_id": provider_id,
        "model_id": model_id,
        "payload": _apply_execution_transport(payload, transport),
    }


def _build_child_execution_intent(
    child: Mapping[str, Any],
    *,
    parent_job_id: str,
    parent_provider_id: str,
    parent_model_id: str,
    scheduled: str,
    transport: tuple[dict[str, str], str],
) -> dict[str, Any]:
    provider_id, model_id = _resolve_child_model_identity(
        child,
        parent_provider_id=parent_provider_id,
        parent_model_id=parent_model_id,
    )
    payload = dict(child.get("payload") or {})
    payload.update(
        _base_execution_payload(
            child,
            job_id=str(child.get("job_id") or ""),
            provider_id=provider_id,
            model_id=model_id,
            scheduled=scheduled,
        ),
    )
    return {
        **child,
        "intent_role": "child",
        "parent_job_id": parent_job_id,
        "provider_id": provider_id,
        "model_id": model_id,
        "payload": _apply_execution_transport(payload, transport),
    }


def _build_worker_scopes_from_jobs(
    jobs: list[Mapping[str, Any]],
) -> list[WorkerScope]:
    scopes: list[WorkerScope] = []
    seen: set[tuple[str, str, str]] = set()
    for job in jobs:
        scope = WorkerScope(
            source_id=str(job.get("source_id") or "").strip(),
            provider_id=str(job.get("provider_id") or DEFAULT_PROVIDER_ID),
            model_id=str(job.get("model_id") or DEFAULT_MODEL_ID),
        )
        key = (scope.source_id, scope.provider_id, scope.model_id)
        if key in seen:
            continue
        seen.add(key)
        scopes.append(scope)
    return scopes


def _normalize_worker_scopes(
    scopes: list[WorkerScope | Mapping[str, Any]] | None,
) -> list[WorkerScope]:
    normalized: list[WorkerScope] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_scope in scopes or []:
        scope = WorkerScope.model_validate(raw_scope)
        key = (scope.source_id, scope.provider_id, scope.model_id)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(scope)
    return normalized


def _source_ids_from_jobs(
    jobs: list[Mapping[str, Any]],
    *,
    fallback: str = "",
) -> list[str]:
    source_ids: list[str] = []
    for job in jobs:
        source_id = str(job.get("source_id") or "").strip()
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
    fallback_source_id = str(fallback or "").strip()
    if fallback_source_id and fallback_source_id not in source_ids:
        source_ids.append(fallback_source_id)
    return source_ids


def _resolve_child_model_identity(
    child: Mapping[str, Any],
    *,
    parent_provider_id: str,
    parent_model_id: str,
) -> tuple[str, str]:
    provider_id = str(child.get("provider_id") or "").strip()
    model_id = str(
        child.get("model_id") or child.get("model") or "",
    ).strip()
    if (
        provider_id
        and model_id
        and (
            provider_id,
            model_id,
        )
        != (DEFAULT_PROVIDER_ID, DEFAULT_MODEL_ID)
    ):
        return provider_id, model_id
    return (
        parent_provider_id or DEFAULT_PROVIDER_ID,
        parent_model_id or DEFAULT_MODEL_ID,
    )


def _build_dispatch_batch_id(
    parent: Mapping[str, Any],
    scheduled_fire_at: datetime,
) -> str:
    value = "|".join(
        [
            str(parent.get("id") or ""),
            scheduled_fire_at.isoformat(),
        ],
    )
    return "cron:" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:32]


def _previous_due_fire_at(
    *,
    cron_expr: str,
    timezone_name: str,
    now_utc: datetime,
) -> datetime | None:
    if not cron_expr.strip():
        return None
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except Exception:
        tz = timezone.utc
    now = _ensure_aware_utc(now_utc)
    local_now = now.astimezone(tz)
    try:
        previous = croniter(
            cron_expr,
            local_now + timedelta(seconds=1),
        ).get_prev(datetime)
    except Exception:
        logger.warning("Invalid cron expression for dispatch: %s", cron_expr)
        return None
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=tz)
    return previous.astimezone(timezone.utc)


def _next_due_fire_at(
    *,
    cron_expr: str,
    timezone_name: str,
    now_utc: datetime,
) -> datetime | None:
    if not cron_expr.strip():
        return None
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except Exception:
        tz = timezone.utc
    now = _ensure_aware_utc(now_utc)
    local_now = now.astimezone(tz)
    try:
        upcoming = croniter(
            cron_expr,
            local_now - timedelta(seconds=1),
        ).get_next(datetime)
    except Exception:
        logger.warning("Invalid cron expression for dispatch: %s", cron_expr)
        return None
    if upcoming.tzinfo is None:
        upcoming = upcoming.replace(tzinfo=tz)
    return upcoming.astimezone(timezone.utc)


def _decode_callback_params(params: Mapping[str, Any]) -> dict[str, Any]:
    job_param = params.get("jobParam") or params.get("job_param") or ""
    if not job_param:
        return dict(params)
    decoded = json.loads(base64.urlsafe_b64decode(str(job_param)))
    if not isinstance(decoded, dict):
        raise RuntimeError("jobParam must decode to an object")
    merged = {**dict(params), **decoded}
    merged.pop("jobParam", None)
    merged.pop("job_param", None)
    return merged


def _build_execution_callback_kwargs(
    row: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    tenant_id = _row_text(row, "tenant_id")
    source_id = _row_text(row, "source_id")
    scope_id = _first_truthy_text(
        payload.get("scopeId"),
        payload.get("scope_id"),
        _row_get(row, "scope_id"),
    )
    from_id = _first_truthy_text(
        payload.get("fromId"),
        payload.get("from_id"),
        _row_get(row, "from_id"),
    )
    callback_kwargs = {
        "tenant_id": tenant_id,
        "source_id": source_id,
        "scope_id": scope_id or _default_scope_id(tenant_id, source_id),
        "from_id": from_id or tenant_id,
        "agent_id": _row_text(row, "agent_id", default="default"),
        "job_id": _row_text(row, "job_id"),
        "dispatch_intent_id": int(_row_get(row, "id") or 0),
        "dispatch_batch_id": _row_text(row, "batch_id"),
        "dispatch_attempt": _positive_int(_row_get(row, "attempt_count")) or 1,
        "execution_key": (
            f"{_row_text(row, 'job_id')}:{_row_text(row, 'batch_id')}:{int(_row_get(row, 'id') or 0)}"
        ),
        "parent_scheduled_fire_at": _first_truthy_text(
            payload.get("parent_scheduled_fire_at"),
            payload.get("scheduled_fire_at"),
        ),
        "provider_id": _first_truthy_text(
            _row_get(row, "provider_id"),
            payload.get("provider_id"),
            default=DEFAULT_PROVIDER_ID,
        ),
        "model_id": _first_truthy_text(
            _row_get(row, "model_id"),
            payload.get("model_id"),
            default=DEFAULT_MODEL_ID,
        ),
    }
    swe_server_domain = _first_truthy_text(
        payload.get(SWE_SERVER_DOMAIN_PAYLOAD_KEY),
    )
    workflow_version = _positive_int(payload.get("workflow_config_version"))
    if workflow_version:
        callback_kwargs["workflow_config_version"] = workflow_version
    if swe_server_domain:
        callback_kwargs["swe_server_domain"] = swe_server_domain
    return callback_kwargs


def _dispatch_mark_details(
    callback_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "callback_source": DISPATCH_CALLBACK_SOURCE,
        "dispatch_attempt": callback_kwargs["dispatch_attempt"],
        "execution_key": callback_kwargs["execution_key"],
        "provider_id": callback_kwargs["provider_id"],
        "model_id": callback_kwargs["model_id"],
        "scope_id": callback_kwargs["scope_id"],
        "from_id": callback_kwargs["from_id"],
    }


def _parse_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _meta_dispatch_intents_enabled(meta: Mapping[str, Any]) -> bool:
    return bool(meta.get(BROADCAST_DISPATCH_INTENTS_ENABLED_META_KEY))


def _extract_model_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    meta = _parse_meta(row.get("meta"))
    flat_identity = _extract_flat_model_identity(row, meta)
    if flat_identity is not None:
        return flat_identity
    slot_identity = _extract_slot_model_identity(row, meta)
    return slot_identity or (DEFAULT_PROVIDER_ID, DEFAULT_MODEL_ID)


def _extract_flat_model_identity(
    row: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> tuple[str, str] | None:
    provider_id = _first_truthy_text(
        row.get("provider_id"),
        meta.get("provider_id"),
    ).strip()
    model_id = _first_truthy_text(
        row.get("model_id"),
        row.get("model"),
        meta.get("model_id"),
        meta.get("model"),
    ).strip()
    if provider_id and model_id:
        return provider_id, model_id
    return None


def _extract_slot_model_identity(
    row: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> tuple[str, str] | None:
    for candidate in _model_slot_candidates(row, meta):
        identity = _model_identity_from_slot(candidate)
        if identity is not None:
            return identity
    return None


def _model_slot_candidates(
    row: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> list[Any]:
    return [
        row.get("model_slot"),
        meta.get("model_slot"),
        meta.get("broadcast_original_model_slot"),
        meta.get("original_model_slot"),
        meta.get("effective_model_slot"),
    ]


def _model_identity_from_slot(candidate: Any) -> tuple[str, str] | None:
    slot = _decode_model_slot(candidate)
    if slot is None:
        return None
    provider_id = _first_truthy_text(slot.get("provider_id")).strip()
    model_id = _first_truthy_text(
        slot.get("model"),
        slot.get("model_id"),
    ).strip()
    if provider_id and model_id:
        return provider_id, model_id
    return None


def _decode_model_slot(candidate: Any) -> Mapping[str, Any] | None:
    if not candidate:
        return None
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    return candidate if isinstance(candidate, Mapping) else None


def _default_scope_id(tenant_id: str, source_id: str) -> str:
    if tenant_id and source_id:
        return f"{tenant_id}-{source_id}"
    return tenant_id or source_id


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _row_get(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def _row_text(row: Any, key: str, *, default: str = "") -> str:
    return str(_row_get(row, key) or default)


def _first_truthy_text(*values: Any, default: str = "") -> str:
    for value in values:
        if value:
            return str(value)
    return default


def _row_payload(row: Any) -> dict[str, Any]:
    payload = _row_get(row, "payload") or {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_b3_passthrough_headers(
    headers: Mapping[str, Any] | None,
) -> dict[str, str]:
    if not headers:
        return {}
    items = getattr(headers, "items", None)
    if not callable(items):
        return {}
    extracted: dict[str, str] = {}
    for name, value in items():
        name_lower = str(name).strip().lower()
        header_name = (
            name_lower[len(HEADER_PREFIX) :]
            if name_lower.startswith(HEADER_PREFIX)
            else name_lower
        )
        canonical_name = B3_HEADER_NAMES.get(header_name)
        if canonical_name is None:
            continue
        header_value = str(value).strip()
        if header_value:
            extracted[canonical_name] = header_value
    return extracted


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _capacity_effective_workers(
    latest: Mapping[str, Any] | None,
    strategy: WorkerStrategy,
) -> int:
    value: Any = (
        latest.get("effective_workers")
        if isinstance(latest, Mapping)
        else strategy.baseline_workers
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = strategy.baseline_workers
    return _clamp(parsed, strategy.min_workers, strategy.max_workers)


def _capacity_adjustment_is_due(
    latest: Mapping[str, Any] | None,
    strategy: WorkerStrategy,
    now: datetime,
) -> bool:
    latest_at = _capacity_created_at(latest)
    return (
        latest_at is None
        or (now - latest_at).total_seconds()
        >= strategy.adjust_interval_seconds
    )


async def _wait_for_next_loop_tick(
    stop_event: asyncio.Event,
    interval_seconds: int,
) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
    except asyncio.TimeoutError:
        return


def _capacity_created_at(latest: Mapping[str, Any] | None) -> datetime | None:
    if not isinstance(latest, Mapping):
        return None
    parsed = _parse_datetime(latest.get("created_at"))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_BEIJING_TZ).astimezone(timezone.utc)
    return parsed.astimezone(timezone.utc)


def _match_error_rate_rule(
    rules: list[dict[str, Any]],
    error_rate: float,
) -> dict[str, Any]:
    for rule in rules:
        min_rate = float(rule.get("min_error_rate", 0) or 0)
        raw_max = rule.get("max_error_rate")
        max_rate = None if raw_max is None else float(raw_max)
        if error_rate < min_rate:
            continue
        if max_rate is not None and error_rate > max_rate:
            continue
        return dict(rule)
    return {"operation": "hold", "value": 0, "reason": "no_matching_rule"}


def _apply_worker_rule(
    previous: int,
    rule: Mapping[str, Any],
    strategy: WorkerStrategy,
) -> tuple[int, str]:
    operation = str(rule.get("operation") or "hold").strip().lower()
    value = float(rule.get("value") or 0)
    if operation in {"add", "+"}:
        next_value = previous + int(value)
    elif operation in {"subtract", "sub", "-"}:
        next_value = previous - int(value)
    elif operation in {"multiply", "mul", "*"}:
        next_value = int(previous * value)
    elif operation in {"divide", "div", "/"}:
        next_value = previous if value == 0 else int(previous / value)
    elif operation == "set":
        next_value = int(value)
    else:
        next_value = previous
    reason = str(rule.get("reason") or operation or "hold")
    return (
        _clamp(next_value, strategy.min_workers, strategy.max_workers),
        reason,
    )


def _default_worker_id() -> str:
    return f"scheduler:{socket.gethostname()}:{os.getpid()}:{uuid4()}"


def cron_scheduling_runtime_enabled() -> bool:
    raw_value = os.environ.get(DISPATCH_INTENTS_ENABLED_ENV, "")
    return raw_value.strip().lower() in _TRUE_ENV_VALUES


def configured_dispatched_stale_seconds() -> int:
    return _positive_int_env(
        DISPATCHED_STALE_SECONDS_ENV,
        DEFAULT_DISPATCHED_STALE_SECONDS,
    )


def _positive_int_env(
    name: str,
    default: int,
) -> int:
    raw = os.environ.get(name)
    try:
        value = int(str(raw or "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


_scheduling_service: Optional[CronSchedulingService] = None


def get_cron_scheduling_service() -> CronSchedulingService:
    """Return the singleton cron scheduling service."""
    global _scheduling_service
    if _scheduling_service is None:
        _scheduling_service = CronSchedulingService(
            dispatched_stale_seconds=configured_dispatched_stale_seconds(),
        )
    return _scheduling_service
