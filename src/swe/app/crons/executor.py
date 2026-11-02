# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, NoReturn, Optional

from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

from .auth_state import resolve_auth_token_for_execution
from .execution_result import ExecutionResult
from .model_slot_context import bind_model_slot_override
from .models import CronJobSpec
from ..b3_headers import (
    B3_TRACE_ID_META_KEY,
    PASSTHROUGH_HEADERS_META_KEY,
)
from ..tenant_context import bind_tenant_context
from ..console_push_store import append as push_store_append
from ..cron_result_metrics import (
    CRON_CANCEL_AFTER_COMPLETED_TOTAL,
    CRON_EMPTY_MODEL_OUTPUT_TOTAL,
    CRON_SESSION_COMMIT_FAILURE_TOTAL,
    CRON_SUCCESS_WITHOUT_ASSISTANT_TOTAL,
    increment_cron_result_metric,
)
from ...config.llm_workload import LLM_WORKLOAD_CRON, bind_llm_workload
from ...config.context import (
    canonicalize_scope_id,
    resolve_request_effective_tenant_id,
    resolve_scope_id,
    resolve_storage_tenant_id,
)
from ...providers.models import ModelSlotConfig
from ...providers.provider_manager import ProviderManager
from ...tracing import has_trace_manager, get_trace_manager
from ...tracing.models import TraceStatus
from ...tracing.output_index import (
    index_trace_output as _index_model_output_to_monitor,
)
from ..runner.model_call_error_detail import redact_sensitive_fragments

logger = logging.getLogger(__name__)


def _build_cron_execution_key(
    *,
    job_id: str,
    target_session_id: str,
    dispatch_meta: Dict[str, Any],
) -> str:
    """Build a stable key from scheduler-owned execution identity.

    No worker-local wall clock is used.  Manual runs pass no dispatch identity
    and therefore intentionally return an empty key.
    """
    explicit = dispatch_meta.get("cron_execution_key") or dispatch_meta.get(
        "execution_key",
    )
    if explicit:
        return str(explicit)
    if dispatch_meta.get("cron_is_manual"):
        return ""

    intent_id = dispatch_meta.get("intent_id")
    batch_id = dispatch_meta.get("batch_id")
    if intent_id and batch_id:
        return f"{job_id}:{batch_id}:{intent_id}:{target_session_id}"

    fire_at = (
        dispatch_meta.get("scheduled_fire_at")
        or dispatch_meta.get("fire_time")
        or dispatch_meta.get("trigger_time")
        or dispatch_meta.get("parent_scheduled_fire_at")
    )
    if fire_at:
        return f"{job_id}:{fire_at}:{target_session_id}"

    external_execution_id = dispatch_meta.get("external_execution_id")
    if external_execution_id:
        return f"{job_id}:{external_execution_id}:{target_session_id}"
    return ""


CONSOLE_CHANNEL = "console"
BROADCAST_ORIGINAL_MODEL_SLOT_META_KEY = "broadcast_original_model_slot"
BROADCAST_MODEL_SLOT_FALLBACK_REASON_META_KEY = (
    "broadcast_model_slot_fallback_reason"
)
CRON_TRACE_SUCCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
CRON_SESSION_CLEANUP_WAIT_SECONDS = 5.0
CRON_EVENT_ERROR_MESSAGE_MAX_LENGTH = 512
CRON_TEXT_TASK_MESSAGES_STATE_KEY = "task_messages"
CRON_DELIVERY_STATE_PENDING = "pending"
CRON_DELIVERY_STATE_UNCERTAIN = "delivery_uncertain"
CRON_DELIVERY_STATE_COMPLETED = "completed"


async def resolve_user_identity(
    *,
    tenant_id: str | None,
    source_id: str | None,
    user_name: str | None,
    bbk_id: str | None,
    headers: Optional[dict[str, str]] = None,
    allow_remote_lookup: bool = True,
):
    """延迟加载身份解析器，避免 cron manager 导入时拉起 workspace 模块。"""
    from ..identity_resolver import resolve_user_identity as _resolve

    return await _resolve(
        tenant_id=tenant_id,
        source_id=source_id,
        user_name=user_name,
        bbk_id=bbk_id,
        headers=headers,
        allow_remote_lookup=allow_remote_lookup,
    )


@dataclass
class _ResolvedExecutionModel:
    original_model_slot: Optional[ModelSlotConfig]
    effective_model_slot: Optional[ModelSlotConfig]
    fallback_reason: str = ""
    bound_model_slot: Optional[ModelSlotConfig] = None

    def build_meta(self) -> Dict[str, Any]:
        return {
            "original_model_slot": (
                self.original_model_slot.model_dump(mode="json")
                if self.original_model_slot is not None
                else None
            ),
            "effective_model_slot": (
                self.effective_model_slot.model_dump(mode="json")
                if self.effective_model_slot is not None
                else None
            ),
            "fallback_reason": self.fallback_reason,
        }


@dataclass
class _ExecutionContext:
    target_user_id: str
    target_session_id: str
    dispatch_meta: Dict[str, Any]
    workspace_dir: Path | None
    tenant_id: str | None
    source_id: str | None
    scope_id: str | None


def _dispatch_b3_trace_id(dispatch_meta: Dict[str, Any]) -> str | None:
    trace_id = dispatch_meta.get(B3_TRACE_ID_META_KEY)
    if trace_id is None:
        return None
    trace_id = str(trace_id).strip()
    return trace_id or None


def _is_dispatch_service_batch(dispatch_meta: Dict[str, Any]) -> bool:
    intent_id = dispatch_meta.get("intent_id")
    batch_id = dispatch_meta.get("batch_id")
    dispatch_attempt = dispatch_meta.get("dispatch_attempt")
    return (
        str(dispatch_meta.get("source") or "").strip() == "dispatch_service"
        and isinstance(intent_id, int)
        and not isinstance(intent_id, bool)
        and intent_id > 0
        and isinstance(batch_id, str)
        and bool(batch_id.strip())
        and isinstance(dispatch_attempt, int)
        and not isinstance(dispatch_attempt, bool)
        and dispatch_attempt > 0
    )


def _resolve_dispatch_trace_ids(
    dispatch_meta: Dict[str, Any],
) -> tuple[str | None, str | None]:
    b3_trace_id = _dispatch_b3_trace_id(dispatch_meta)
    requested_trace_id = (
        str(uuid.uuid4())
        if _is_dispatch_service_batch(dispatch_meta)
        else b3_trace_id
    )
    return requested_trace_id, b3_trace_id


def _dispatch_passthrough_headers(
    dispatch_meta: Dict[str, Any],
) -> dict[str, str]:
    raw_headers = dispatch_meta.get(PASSTHROUGH_HEADERS_META_KEY)
    if not isinstance(raw_headers, dict):
        return {}
    headers: dict[str, str] = {}
    for name, value in raw_headers.items():
        if value is None:
            continue
        header_name = str(name).strip()
        header_value = str(value).strip()
        if header_name and header_value:
            headers[header_name] = header_value
    return headers


def _apply_dispatch_passthrough_headers(
    req: Dict[str, Any],
    dispatch_meta: Dict[str, Any],
) -> None:
    headers = _dispatch_passthrough_headers(dispatch_meta)
    if headers:
        req[PASSTHROUGH_HEADERS_META_KEY] = headers
    trace_id = _dispatch_b3_trace_id(dispatch_meta)
    if trace_id:
        req[B3_TRACE_ID_META_KEY] = trace_id


@dataclass
class AgentStreamState:
    """记录 Agent 流式执行边界，用于区分真实取消和完成后取消。"""

    event_count: int = 0
    completed_message_seen: bool = False
    completed_message_sent: bool = False
    stream_returned: bool = False
    output_parts: list[str] = field(default_factory=list)
    failed_message_seen: bool = False  # 是否看到 Failed 事件
    error_message: str = ""  # 错误信息
    response_completed_seen: bool = False
    response_failed_seen: bool = False
    response_cancelled_seen: bool = False
    terminal_response_status: str = ""
    terminal_error_code: str = ""
    last_event_object: str = ""
    last_event_status: str = ""
    unknown_event_count: int = 0
    assistant_message_seen: bool = False
    assistant_message_count: int = 0
    persisted_assistant_message_count: int = 0
    output_source: str = ""
    session_state_commit_attempted: bool = False
    session_state_committed: bool = False
    idempotent_replay: bool = False
    output_delivery_replay_supported: bool = False
    output_delivery_completed: bool = False
    output_delivery_receipt_supported: bool = False
    terminal_notification_sent: bool = False
    completed_message_event: Any | None = field(default=None, repr=False)
    persisted_assistant_content: list[dict[str, Any]] = field(
        default_factory=list,
        repr=False,
    )
    _output_event_keys: set[str] = field(default_factory=set, repr=False)

    @property
    def output_len(self) -> int:
        return len("\n".join(self.output_parts))

    @property
    def failed_seen(self) -> bool:
        return self.failed_message_seen or self.response_failed_seen

    @property
    def completed_seen(self) -> bool:
        return self.completed_message_seen or self.response_completed_seen


@dataclass(frozen=True)
class _AgentStreamEventFlags:
    completed_message: bool
    failed_message: bool
    completed_response: bool
    failed_response: bool
    cancelled_response: bool

    @property
    def requires_terminal_notification(self) -> bool:
        return (
            self.failed_message
            or self.failed_response
            or self.cancelled_response
        )

    @property
    def terminates_stream(self) -> bool:
        return self.completed_response or self.requires_terminal_notification


class CronExecutor:
    def __init__(self, *, runner: Any, channel_manager: Any):
        self._runner = runner
        self._channel_manager = channel_manager

    async def execute(
        self,
        job: CronJobSpec,
        dispatch_meta: Optional[Dict[str, Any]] = None,
    ) -> ExecutionResult:
        """Execute one job once with tenant context.

        - task_type text: send fixed text to channel
        - task_type agent: ask agent with prompt, send reply to channel (
            stream_query + send_event)

        Job execution is wrapped in tenant context to ensure proper isolation.

        Returns:
            ExecutionResult containing trace_id and output_preview
        """
        context = self._prepare_execution_context(job, dispatch_meta)
        resolved_model = self._resolve_execution_model(job, context.scope_id)

        logger.info(
            "cron execute: job_id=%s channel=%s task_type=%s "
            "target_user_id=%s target_session_id=%s tenant_id=%s",
            job.id,
            job.dispatch.channel,
            job.task_type,
            context.target_user_id[:40] if context.target_user_id else "",
            (
                context.target_session_id[:40]
                if context.target_session_id
                else ""
            ),
            context.tenant_id or "default",
        )

        try:
            result = await self._execute_job_in_context(
                job,
                context,
                resolved_model,
            )
        except BaseException as exc:
            self._attach_execution_meta(exc, resolved_model)
            raise

        return self._build_execution_result(
            result,
            execution_meta=(
                resolved_model.build_meta()
                if resolved_model is not None
                else None
            ),
        )

    def _prepare_execution_context(
        self,
        job: CronJobSpec,
        execution_dispatch_meta: Optional[Dict[str, Any]] = None,
    ) -> _ExecutionContext:
        dispatch_meta: Dict[str, Any] = dict(job.dispatch.meta or {})
        dispatch_meta.update(execution_dispatch_meta or {})
        workspace_dir_value = dispatch_meta.get("workspace_dir")
        workspace_dir = (
            Path(workspace_dir_value) if workspace_dir_value else None
        )
        tenant_id = getattr(job, "tenant_id", None)
        source_id = getattr(job, "source_id", None)
        job_scope_id = getattr(job, "scope_id", None)
        if job_scope_id is not None:
            scope_id = canonicalize_scope_id(job_scope_id)
        elif tenant_id == "default" and source_id is not None:
            scope_id = None
        else:
            scope_id = resolve_scope_id(tenant_id, source_id)
        if tenant_id:
            dispatch_meta["tenant_id"] = tenant_id
        if source_id:
            dispatch_meta["source_id"] = source_id
        if scope_id:
            dispatch_meta["scope_id"] = scope_id
        return _ExecutionContext(
            target_user_id=job.dispatch.target.user_id,
            target_session_id=job.dispatch.target.session_id,
            dispatch_meta=dispatch_meta,
            workspace_dir=workspace_dir,
            tenant_id=tenant_id,
            source_id=source_id,
            scope_id=scope_id,
        )

    async def _execute_job_in_context(
        self,
        job: CronJobSpec,
        context: _ExecutionContext,
        resolved_model: _ResolvedExecutionModel | None,
    ) -> Dict[str, Any]:
        with (
            bind_tenant_context(
                tenant_id=context.tenant_id,
                user_id=context.target_user_id,
                workspace_dir=context.workspace_dir,
                source_id=context.source_id,
                scope_id=context.scope_id,
            ),
            bind_llm_workload(LLM_WORKLOAD_CRON),
            self._build_model_slot_context(resolved_model),
        ):
            return await self._execute_job(
                job,
                context.target_user_id,
                context.target_session_id,
                context.dispatch_meta,
            )

    @staticmethod
    def _build_model_slot_context(
        resolved_model: _ResolvedExecutionModel | None,
    ) -> Any:
        if resolved_model is None or resolved_model.bound_model_slot is None:
            return nullcontext()
        return bind_model_slot_override(resolved_model.bound_model_slot)

    @staticmethod
    def _attach_execution_meta(
        exc: BaseException,
        resolved_model: _ResolvedExecutionModel | None,
    ) -> None:
        if resolved_model is None:
            return
        execution_meta = resolved_model.build_meta()
        existing_meta = getattr(exc, "cron_execution_meta", None)
        if isinstance(existing_meta, dict):
            execution_meta.update(existing_meta)
        setattr(exc, "cron_execution_meta", execution_meta)

    def _build_execution_result(
        self,
        result: Dict[str, Any] | None,
        *,
        execution_meta: Dict[str, Any] | None,
    ) -> ExecutionResult:
        result = result or {}
        output_preview = str(result.get("output_preview") or "")
        runtime_meta = result.get("execution_meta")
        merged_meta = dict(execution_meta or {})
        if isinstance(runtime_meta, dict):
            merged_meta.update(runtime_meta)
        return ExecutionResult(
            trace_id=str(result.get("trace_id") or ""),
            output_preview=output_preview[:100],
            input_snapshot=result.get("input_snapshot"),
            executor_leader=str(result.get("executor_leader") or ""),
            execution_meta=merged_meta or None,
            status=str(result.get("status") or "success"),
        )

    def _resolve_execution_model(
        self,
        job: CronJobSpec,
        scope_id: str | None,
    ) -> Optional[_ResolvedExecutionModel]:
        if job.task_type != "agent":
            return None

        runtime_tenant_id = scope_id or getattr(job, "tenant_id", None)
        manager_tenant_id = (
            resolve_storage_tenant_id(
                getattr(job, "tenant_id", None),
                getattr(job, "source_id", None),
                scope_id=runtime_tenant_id if runtime_tenant_id else None,
            )
            or runtime_tenant_id
            or "default"
        )
        ProviderManager.ensure_tenant_provider_storage(manager_tenant_id)
        manager = ProviderManager.get_instance(manager_tenant_id)
        active_model = manager.get_active_model()
        effective_default = self._normalize_model_slot(active_model)
        original_model = self._normalize_model_slot(job.model_slot)

        if original_model is None:
            broadcast_resolved = self._resolve_broadcast_execution_model(
                job,
                effective_default,
            )
            if broadcast_resolved is not None:
                return broadcast_resolved
            return _ResolvedExecutionModel(
                original_model_slot=None,
                effective_model_slot=effective_default,
            )

        provider = manager.get_provider(original_model.provider_id)
        if provider is None:
            logger.warning(
                "cron model_slot fallback: job_id=%s "
                "reason=provider_not_found "
                "original_provider=%s original_model=%s effective_provider=%s "
                "effective_model=%s",
                job.id,
                original_model.provider_id,
                original_model.model,
                (
                    effective_default.provider_id
                    if effective_default is not None
                    else ""
                ),
                (
                    effective_default.model
                    if effective_default is not None
                    else ""
                ),
            )
            return _ResolvedExecutionModel(
                original_model_slot=original_model,
                effective_model_slot=effective_default,
                fallback_reason="provider_not_found",
            )
        if not provider.has_model(original_model.model):
            logger.warning(
                "cron model_slot fallback: job_id=%s reason=model_not_found "
                "original_provider=%s original_model=%s effective_provider=%s "
                "effective_model=%s",
                job.id,
                original_model.provider_id,
                original_model.model,
                (
                    effective_default.provider_id
                    if effective_default is not None
                    else ""
                ),
                (
                    effective_default.model
                    if effective_default is not None
                    else ""
                ),
            )
            return _ResolvedExecutionModel(
                original_model_slot=original_model,
                effective_model_slot=effective_default,
                fallback_reason="model_not_found",
            )
        return _ResolvedExecutionModel(
            original_model_slot=original_model,
            effective_model_slot=original_model,
            bound_model_slot=original_model,
        )

    def _resolve_broadcast_execution_model(
        self,
        job: CronJobSpec,
        effective_default: ModelSlotConfig | None,
    ) -> _ResolvedExecutionModel | None:
        meta = dict(job.meta or {})
        original_model = self._normalize_model_slot(
            meta.get(BROADCAST_ORIGINAL_MODEL_SLOT_META_KEY),
        )
        fallback_reason = str(
            meta.get(BROADCAST_MODEL_SLOT_FALLBACK_REASON_META_KEY) or "",
        )
        if original_model is None or not fallback_reason:
            return None
        logger.warning(
            "cron model_slot fallback: job_id=%s reason=%s "
            "original_provider=%s original_model=%s effective_provider=%s "
            "effective_model=%s",
            job.id,
            fallback_reason,
            original_model.provider_id,
            original_model.model,
            (
                effective_default.provider_id
                if effective_default is not None
                else ""
            ),
            effective_default.model if effective_default is not None else "",
        )
        return _ResolvedExecutionModel(
            original_model_slot=original_model,
            effective_model_slot=effective_default,
            fallback_reason=fallback_reason,
        )

    @staticmethod
    def _normalize_model_slot(
        model_slot: Any,
    ) -> ModelSlotConfig | None:
        if model_slot is None:
            return None
        if isinstance(model_slot, dict):
            provider_id = str(model_slot.get("provider_id") or "")
            model = str(model_slot.get("model") or "")
        else:
            provider_id = getattr(model_slot, "provider_id", "") or ""
            model = getattr(model_slot, "model", "") or ""
        if not provider_id or not model:
            return None
        return ModelSlotConfig(
            provider_id=provider_id,
            model=model,
        )

    async def _execute_job(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Internal: execute job logic (called within tenant context).

        Returns:
            Dict with trace_id, output_preview, input_snapshot, executor_leader
        """
        if job.task_type == "text" and job.text:
            return await self._execute_text_job(
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
            )
        else:
            return await self._execute_agent_job(
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
            )

    async def _execute_text_job(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute text-type job: send fixed text to channel.

        Returns:
            Dict with trace_id, output_preview, input_snapshot, executor_leader
        """
        logger.info(
            "cron send_text: job_id=%s channel=%s len=%s",
            job.id,
            job.dispatch.channel,
            len(job.text or ""),
        )
        if not job.text.strip():
            raise RuntimeError("cron text delivery failed: empty text output")
        execution_key = _build_cron_execution_key(
            job_id=job.id,
            target_session_id=target_session_id,
            dispatch_meta=dispatch_meta,
        )
        if not execution_key:
            raise RuntimeError(
                "cron text delivery requires execution identity",
            )
        delivery_meta = dict(dispatch_meta)
        delivery_meta["cron_delivery_key"] = f"cron:{execution_key}:output"

        # 保留抽出的 helper，同时继续传递 scope 级租户标识。
        requested_trace_id, b3_trace_id = _resolve_dispatch_trace_ids(
            dispatch_meta,
        )
        created_trace_id = await self._create_trace_for_text_job(
            job,
            target_user_id,
            target_session_id,
            trace_id=requested_trace_id,
            b3_trace_id=b3_trace_id,
        )

        business_effects_completed = False
        try:
            await self._deliver_text_output(
                job,
                execution_key,
                target_user_id,
                target_session_id,
                delivery_meta,
            )
            business_effects_completed = True
        finally:
            try:
                await self._end_trace_for_text_job(created_trace_id)
            except asyncio.CancelledError:
                if not business_effects_completed:
                    raise
                task = asyncio.current_task()
                if task is not None and hasattr(task, "uncancel"):
                    task.uncancel()
                logger.info(
                    "cron text trace cleanup cancelled after delivery: job_id=%s",
                    job.id,
                )

        internal_trace_id = created_trace_id or requested_trace_id

        # 返回执行结果
        output_preview = (job.text or "").strip()[:100]
        input_snapshot = (
            {
                "text": (job.text or "").strip(),
                **(
                    {"cron_execution_key": execution_key}
                    if execution_key
                    else {}
                ),
            }
            if job.text
            else None
        )
        return {
            "trace_id": internal_trace_id or "",
            "output_preview": output_preview,
            "input_snapshot": input_snapshot,
            "executor_leader": "",
        }

    async def _deliver_text_output(
        self,
        job: CronJobSpec,
        execution_key: str,
        target_user_id: str,
        target_session_id: str,
        delivery_meta: Dict[str, Any],
    ) -> None:
        """Deliver text while holding the shared session transaction lock."""
        task_session_id = str(
            (job.meta or {}).get("task_session_id") or "",
        )
        task_user_id = str(
            (job.meta or {}).get("creator_user_id") or target_user_id,
        )
        session = getattr(self._runner, "session", None)
        execution_factory = getattr(session, "execution", None)
        if not task_session_id:
            raise RuntimeError("cron text delivery persistence unavailable")
        if not callable(execution_factory):
            # Keep compatibility with lightweight runner test doubles. The
            # production SafeJSONSession always provides this transaction.
            delivery_completed = await self._prepare_text_task_delivery(
                job,
                execution_key,
                target_user_id,
            )
            if delivery_completed is None:
                raise RuntimeError(
                    "cron text delivery persistence unavailable",
                )
            if not delivery_completed:
                await self._send_text_to_channel(
                    job,
                    target_user_id,
                    target_session_id,
                    delivery_meta,
                    require_delivery_receipt=True,
                )
                await self._mark_text_task_delivery_completed(
                    job,
                    execution_key,
                    target_user_id,
                )
            return

        async with execution_factory(
            task_session_id,
            user_id=task_user_id,
            timeout_seconds=5.0,
        ) as session_execution:
            delivery_completed = self._merge_text_task_delivery_state(
                session_execution.state,
                job,
                execution_key,
            )
            if delivery_completed:
                return
            # Make the assistant message durable before invoking the external
            # channel. The transaction remains locked until the receipt is
            # committed, preventing a second Pod from sending concurrently.
            self._mark_text_task_delivery_state(
                session_execution.state,
                execution_key,
                delivery_state=CRON_DELIVERY_STATE_UNCERTAIN,
            )
            await session_execution.commit_state(session_execution.state)
            await self._send_text_to_channel(
                job,
                target_user_id,
                target_session_id,
                delivery_meta,
                require_delivery_receipt=True,
            )
            self._mark_text_task_delivery_state(
                session_execution.state,
                execution_key,
                delivery_state=CRON_DELIVERY_STATE_COMPLETED,
            )
            await session_execution.commit_state(session_execution.state)

    async def _create_trace_for_text_job(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        trace_id: str | None = None,
        b3_trace_id: str | None = None,
    ) -> Optional[str]:
        """为 text 类型任务创建 trace 记录。

        Args:
            job: 任务定义
            target_user_id: 目标用户 ID
            target_session_id: 目标会话 ID

        Returns:
            trace_id 或 None
        """
        if not has_trace_manager():
            return None

        try:
            trace_mgr = get_trace_manager()
            if not trace_mgr.enabled:
                return None

            source_id = job.source_id or "default"
            resolved_identity = await resolve_user_identity(
                tenant_id=getattr(job, "tenant_id", None),
                source_id=job.source_id,
                user_name=job.tenant_name,
                bbk_id=job.bbk_id,
                allow_remote_lookup=False,
            )
            trace_id = await trace_mgr.start_trace(
                user_id=target_user_id or "cron",
                session_id=target_session_id or f"cron:{job.id}",
                channel=job.dispatch.channel,
                source_id=source_id,
                user_message=None,
                user_name=resolved_identity.user_name,
                bbk_id=resolved_identity.bbk_id,
                session_name=job.name,
                trace_id=trace_id,
                b3_trace_id=b3_trace_id,
            )
            # 写入 model_output 到 ES
            if trace_id and job.text:
                await _index_model_output_to_monitor(
                    trace_id,
                    job.text.strip(),
                )
            return trace_id
        except Exception as e:
            logger.warning("Failed to start trace for text job: %s", e)
            return None

    async def _send_text_to_channel(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        *,
        require_delivery_receipt: bool,
    ) -> None:
        """Send text to the configured external channel.

        Args:
            job: 任务定义
            target_user_id: 目标用户 ID
            target_session_id: 目标会话 ID
            dispatch_meta: dispatch 元数据
        """
        if (
            job.dispatch.channel == CONSOLE_CHANNEL
            and require_delivery_receipt
        ):
            return
        delivered = await self._channel_manager.send_text(
            channel=job.dispatch.channel,
            user_id=target_user_id,
            session_id=target_session_id,
            text=job.text.strip(),
            meta=dispatch_meta,
        )
        if require_delivery_receipt and delivered is not True:
            raise RuntimeError("cron text delivery not confirmed")

    async def _prepare_text_task_delivery(
        self,
        job: CronJobSpec,
        execution_key: str,
        target_user_id: str,
    ) -> bool | None:
        """Persist text output before external delivery and read its receipt."""
        if not execution_key:
            return None
        task_session_id = str(
            (job.meta or {}).get("task_session_id") or "",
        )
        task_user_id = str(
            (job.meta or {}).get("creator_user_id") or target_user_id,
        )
        session = getattr(self._runner, "session", None)
        mutate = getattr(session, "mutate_session_state", None)
        if not task_session_id:
            return None
        if not callable(mutate):
            raise RuntimeError("cron text delivery persistence unavailable")

        existing_delivery_completed = False
        timestamp = (
            datetime.now(timezone.utc)
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )

        def _merge(existing_state: dict[str, Any]) -> dict[str, Any]:
            nonlocal existing_delivery_completed
            existing_delivery_completed = self._merge_text_task_delivery_state(
                existing_state,
                job,
                execution_key,
                timestamp=timestamp,
            )
            return existing_state

        await mutate(
            session_id=task_session_id,
            user_id=task_user_id,
            mutator=_merge,
            create_if_not_exist=True,
        )
        return existing_delivery_completed

    @staticmethod
    def _merge_text_task_delivery_state(
        state: dict[str, Any],
        job: CronJobSpec,
        execution_key: str,
        *,
        timestamp: str | None = None,
    ) -> bool:
        message_id = f"cron-text-{execution_key}"
        delivery_key = f"cron:{execution_key}:output"
        messages = list(
            (
                state.get(CRON_TEXT_TASK_MESSAGES_STATE_KEY, [])
                if isinstance(
                    state.get(CRON_TEXT_TASK_MESSAGES_STATE_KEY, []),
                    list,
                )
                else []
            ),
        )
        for message in messages:
            if (
                not isinstance(message, dict)
                or message.get("id") != message_id
            ):
                continue
            content = message.get("content")
            existing_text = (
                "".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
                if isinstance(content, list)
                else ""
            )
            if existing_text != job.text.strip():
                raise RuntimeError("execution_key_conflict")
            metadata = message.get("metadata")
            state[CRON_TEXT_TASK_MESSAGES_STATE_KEY] = messages
            return bool(
                (
                    metadata.get("output_delivery_completed")
                    if isinstance(metadata, dict)
                    else False
                ),
            )
        messages.append(
            {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": job.text.strip()}],
                "metadata": {
                    "cron_task": True,
                    "cron_delivery_key": delivery_key,
                    "output_delivery_state": CRON_DELIVERY_STATE_PENDING,
                    "output_delivery_completed": False,
                },
                "timestamp": timestamp
                or datetime.now(timezone.utc)
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
                ),
            },
        )
        state[CRON_TEXT_TASK_MESSAGES_STATE_KEY] = messages
        return False

    @staticmethod
    def _mark_text_task_delivery_state(
        state: dict[str, Any],
        execution_key: str,
        *,
        delivery_state: str,
    ) -> None:
        message_id = f"cron-text-{execution_key}"
        messages = state.get(CRON_TEXT_TASK_MESSAGES_STATE_KEY, [])
        if not isinstance(messages, list):
            raise RuntimeError("cron text delivery message missing")
        for index, message in enumerate(messages):
            if (
                not isinstance(message, dict)
                or message.get("id") != message_id
            ):
                continue
            metadata = dict(message.get("metadata") or {})
            metadata["output_delivery_state"] = delivery_state
            metadata["output_delivery_completed"] = (
                delivery_state == CRON_DELIVERY_STATE_COMPLETED
            )
            messages[index] = {**message, "metadata": metadata}
            state[CRON_TEXT_TASK_MESSAGES_STATE_KEY] = messages
            return
        raise RuntimeError("cron text delivery message missing")

    async def _mark_text_task_delivery_completed(
        self,
        job: CronJobSpec,
        execution_key: str,
        target_user_id: str,
    ) -> None:
        """Write the text delivery receipt after the channel confirms send."""
        if not execution_key:
            return
        task_session_id = str(
            (job.meta or {}).get("task_session_id") or "",
        )
        task_user_id = str(
            (job.meta or {}).get("creator_user_id") or target_user_id,
        )
        session = getattr(self._runner, "session", None)
        mutate = getattr(session, "mutate_session_state", None)
        if not task_session_id or not callable(mutate):
            raise RuntimeError("cron text delivery persistence unavailable")
        message_id = f"cron-text-{execution_key}"

        def _mark(existing_state: dict[str, Any]) -> dict[str, Any]:
            state = dict(existing_state)
            raw_messages = state.get(CRON_TEXT_TASK_MESSAGES_STATE_KEY, [])
            messages = (
                list(raw_messages) if isinstance(raw_messages, list) else []
            )
            for index, message in enumerate(messages):
                if (
                    not isinstance(message, dict)
                    or message.get("id") != message_id
                ):
                    continue
                metadata = dict(message.get("metadata") or {})
                if metadata.get("output_delivery_completed"):
                    break
                messages[index] = {
                    **message,
                    "metadata": {
                        **metadata,
                        "output_delivery_state": CRON_DELIVERY_STATE_COMPLETED,
                        "output_delivery_completed": True,
                    },
                }
                break
            else:
                raise RuntimeError("cron text delivery message missing")
            state[CRON_TEXT_TASK_MESSAGES_STATE_KEY] = messages
            return state

        await mutate(
            session_id=task_session_id,
            user_id=task_user_id,
            mutator=_mark,
            create_if_not_exist=True,
        )

    async def _end_trace_for_text_job(self, trace_id: Optional[str]) -> None:
        """结束 text 任务的 trace。

        Args:
            trace_id: trace ID
        """
        if not trace_id or not has_trace_manager():
            return

        try:
            trace_mgr = get_trace_manager()
            await trace_mgr.end_trace(trace_id, TraceStatus.COMPLETED)
        except Exception as e:
            logger.warning("Failed to end trace for text job: %s", e)

    async def _create_trace_for_agent_job(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        trace_id: str | None = None,
        b3_trace_id: str | None = None,
    ) -> Optional[str]:
        """为 agent 任务创建 trace 记录。

        Args:
            job: 任务定义
            target_user_id: 目标用户 ID
            target_session_id: 目标会话 ID

        Returns:
            trace_id 或 None
        """
        if not has_trace_manager():
            return None

        try:
            trace_mgr = get_trace_manager()
            if not trace_mgr.enabled:
                return None

            source_id = job.source_id or "default"
            resolved_identity = await resolve_user_identity(
                tenant_id=getattr(job, "tenant_id", None),
                source_id=job.source_id,
                user_name=job.tenant_name,
                bbk_id=job.bbk_id,
                allow_remote_lookup=False,
            )
            trace_id = await trace_mgr.start_trace(
                user_id=target_user_id or "cron",
                session_id=target_session_id or f"cron:{job.id}",
                channel=job.dispatch.channel,
                source_id=source_id,
                user_message=None,  # agent 任务的用户消息由 runner 处理
                user_name=resolved_identity.user_name,
                bbk_id=resolved_identity.bbk_id,
                session_name=job.name,
                trace_id=trace_id,
                b3_trace_id=b3_trace_id,
            )
            logger.info(
                "cron agent: created trace_id=%s for job_id=%s",
                trace_id[:20] if trace_id else "(empty)",
                job.id,
            )
            return trace_id
        except Exception as e:
            logger.warning("Failed to start trace for agent job: %s", e)
            return None

    async def _end_trace_on_exception(
        self,
        trace_id: Optional[str],
        status: TraceStatus,
        error_msg: Optional[str] = None,
    ) -> None:
        """异常情况下结束 trace。

        Args:
            trace_id: trace ID
            status: trace 状态
            error_msg: 错误信息（可选）
        """
        if not trace_id or not has_trace_manager():
            return

        try:
            trace_mgr = get_trace_manager()
            await trace_mgr.end_trace(trace_id, status, error_msg)
        except Exception as e:
            logger.warning("Failed to end trace for %s: %s", status, e)

    async def _end_trace_on_success(
        self,
        trace_id: Optional[str],
        job_id: str,
    ) -> None:
        """成功情况下结束 trace（使用 shield 保护）。

        Args:
            trace_id: trace ID
            job_id: 任务 ID
        """
        if not trace_id or not has_trace_manager():
            return

        trace_mgr = get_trace_manager()
        task = asyncio.create_task(
            trace_mgr.end_trace(trace_id, TraceStatus.COMPLETED),
        )

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 外部取消信号到达，但 shield 已阻止其传播到 end_trace
            # 成功后的 trace 收尾是辅助写入，不应反向覆盖业务成功状态。
            cancelling_count = self._current_task_cancelling_count()
            uncancelled = self._uncancel_current_task()
            logger.info(
                "Trace ending was cancelled after successful cron execution; "
                "waiting for end_trace to finish: job_id=%s "
                "cancelling_count=%s uncancelled=%s",
                job_id,
                cancelling_count,
                uncancelled,
            )
            await self._wait_success_trace_end_after_cancel(task, job_id)
        except Exception as e:
            logger.warning("Failed to end trace for success: %s", e)

    async def _wait_success_trace_end_after_cancel(
        self,
        task: asyncio.Task[Any],
        job_id: str,
    ) -> None:
        """等待完成态 trace 收尾，重复取消时保留业务成功。"""
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=CRON_TRACE_SUCCESS_CLEANUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out ending trace after successful cron execution: "
                "job_id=%s timeout=%ss",
                job_id,
                CRON_TRACE_SUCCESS_CLEANUP_TIMEOUT_SECONDS,
            )
            task.add_done_callback(self._consume_trace_end_task_result)
        except asyncio.CancelledError:
            cancelling_count = self._current_task_cancelling_count()
            uncancelled = self._uncancel_current_task()
            logger.info(
                "Trace ending received repeated cancellation after successful "
                "cron execution; keeping success status: job_id=%s "
                "cancelling_count=%s uncancelled=%s",
                job_id,
                cancelling_count,
                uncancelled,
            )
            task.add_done_callback(self._consume_trace_end_task_result)
        except Exception as e:
            logger.warning("Failed to end trace for success: %s", e)

    @staticmethod
    def _consume_trace_end_task_result(task: asyncio.Task[Any]) -> None:
        """消费后台 trace 收尾结果，避免未读取异常污染事件循环。"""
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Failed to end trace for success: %s", e)

    def _build_agent_execution_result(
        self,
        trace_id: Optional[str],
        console_text_parts: list[str],
        req: Dict[str, Any],
    ) -> Dict[str, Any]:
        """构建 agent 任务执行结果。

        Args:
            trace_id: trace ID
            console_text_parts: 输出的文本部分列表
            req: agent 请求字典

        Returns:
            包含执行结果的字典
        """
        output_preview = (
            "\n".join(console_text_parts)[:100] if console_text_parts else ""
        )
        input_snapshot = req if req else None
        return {
            "trace_id": trace_id,
            "output_preview": output_preview,
            "input_snapshot": input_snapshot,
            "executor_leader": "",
        }

    @staticmethod
    def _stream_state_execution_meta(
        stream_state: AgentStreamState,
    ) -> Dict[str, Any]:
        """构建终态决策和最终日志共用的流诊断快照。"""
        return {
            "response_terminal_status": (
                stream_state.terminal_response_status
                or (
                    RunStatus.Completed
                    if stream_state.response_completed_seen
                    else ""
                )
            ),
            "completed_message_seen": stream_state.completed_message_seen,
            "assistant_message_count": stream_state.assistant_message_count,
            "persisted_assistant_message_count": (
                stream_state.persisted_assistant_message_count
            ),
            "output_len": stream_state.output_len,
            "output_source": stream_state.output_source,
            "session_state_commit_attempted": stream_state.session_state_commit_attempted,
            "session_state_committed": stream_state.session_state_committed,
            "idempotent_replay": stream_state.idempotent_replay,
            "output_delivery_replay_supported": (
                stream_state.output_delivery_replay_supported
            ),
            "output_delivery_completed": stream_state.output_delivery_completed,
            "terminal_notification_sent": stream_state.terminal_notification_sent,
            "terminal_error_code": stream_state.terminal_error_code,
        }

    def _attach_stream_state_execution_meta(
        self,
        exc: BaseException,
        stream_state: AgentStreamState,
    ) -> None:
        """把已观测的 Runtime 终态附加到异常，供 Manager 最终记录。"""
        existing_meta = getattr(exc, "cron_execution_meta", None)
        execution_meta = (
            dict(existing_meta) if isinstance(existing_meta, dict) else {}
        )
        execution_meta.update(self._stream_state_execution_meta(stream_state))
        setattr(exc, "cron_execution_meta", execution_meta)

    def _log_agent_stream_failed(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
    ) -> None:
        """记录 Agent stream 返回后检测到 Failed 事件的日志。"""
        logger.warning(
            "cron agent stream returned with failed: job_id=%s "
            "event_count=%s error=%s",
            job.id,
            stream_state.event_count,
            stream_state.error_message,
        )

    def _log_agent_timeout(self, job: CronJobSpec) -> None:
        """记录 Agent 执行超时日志。"""
        logger.warning(
            "cron execute: job_id=%s timed out after %ss",
            job.id,
            job.runtime.timeout_seconds,
        )

    def _log_agent_cancelled_after_failed(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
    ) -> None:
        """记录取消后检测到 Failed 的日志。"""
        logger.warning(
            "cron agent cancelled after failed: job_id=%s "
            "phase=%s event_count=%s failed_seen=%s error=%s",
            job.id,
            self._agent_stream_phase(stream_state),
            stream_state.event_count,
            stream_state.failed_message_seen,
            stream_state.error_message,
        )

    def _log_agent_cancelled_after_completed(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        cancelling_count: int,
        uncancelled: int,
    ) -> None:
        """记录取消后已完成输出的日志。"""
        logger.info(
            "cron agent cancellation after completed output; "
            "treating as success: job_id=%s phase=%s "
            "event_count=%s completed_seen=%s completed_sent=%s "
            "stream_returned=%s output_len=%s cancelling_count=%s "
            "uncancelled=%s",
            job.id,
            self._agent_stream_phase(stream_state),
            stream_state.event_count,
            stream_state.completed_message_seen,
            stream_state.completed_message_sent,
            stream_state.stream_returned,
            stream_state.output_len,
            cancelling_count,
            uncancelled,
        )

    def _log_agent_cancelled_before_completed(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
    ) -> None:
        """记录取消前未完成输出的日志。"""
        logger.info(
            "cron agent cancelled before completion: job_id=%s "
            "phase=%s event_count=%s completed_seen=%s "
            "completed_sent=%s stream_returned=%s cancelling_count=%s",
            job.id,
            self._agent_stream_phase(stream_state),
            stream_state.event_count,
            stream_state.completed_message_seen,
            stream_state.completed_message_sent,
            stream_state.stream_returned,
            self._current_task_cancelling_count(),
        )

    def _log_agent_generic_error(
        self,
        job: CronJobSpec,
        error: Exception,
    ) -> None:
        """记录 Agent 执行通用异常日志。"""
        logger.warning(
            "cron execute: job_id=%s error: %s",
            job.id,
            repr(error),
        )

    async def _prepare_agent_execution(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
    ) -> tuple[str, Optional[str], Dict[str, Any], AgentStreamState]:
        """准备 Agent 执行的上下文和请求。

        Args:
            job: 任务定义
            target_user_id: 目标用户 ID
            target_session_id: 目标会话 ID
            dispatch_meta: dispatch 元数据

        Returns:
            (runtime_tenant_id, trace_id, req, stream_state)
        """
        runtime_tenant_id = (
            dispatch_meta.get("scope_id")
            or dispatch_meta.get("tenant_id")
            or "default"
        )
        logger.info(
            "cron agent: job_id=%s channel=%s timeout=%ss",
            job.id,
            job.dispatch.channel,
            job.runtime.timeout_seconds,
        )
        assert job.request is not None
        req = self._build_agent_request(
            job,
            target_user_id,
            target_session_id,
            dispatch_meta=dispatch_meta,
        )
        _apply_dispatch_passthrough_headers(req, dispatch_meta)
        requested_trace_id, b3_trace_id = _resolve_dispatch_trace_ids(
            dispatch_meta,
        )
        created_trace_id = await self._create_trace_for_agent_job(
            job,
            target_user_id,
            target_session_id,
            trace_id=requested_trace_id,
            b3_trace_id=b3_trace_id,
        )
        internal_trace_id = created_trace_id or requested_trace_id

        try:
            await self._apply_auth_token(job, dispatch_meta, req)
        except Exception as exc:
            await self._handle_agent_generic_exception(
                job,
                created_trace_id,
                exc,
            )
            setattr(exc, "cron_trace_id", internal_trace_id)
            raise

        if internal_trace_id:
            req["trace_id"] = internal_trace_id
            req["trace_attach_existing"] = True

        return runtime_tenant_id, internal_trace_id, req, AgentStreamState()

    async def _run_agent_stream_with_timeout(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        req: Dict[str, Any],
        stream_state: AgentStreamState,
        trace_id: Optional[str],
    ) -> None:
        """带超时控制的 Agent stream 执行。

        Args:
            job: 任务定义
            target_user_id: 目标用户 ID
            target_session_id: 目标会话 ID
            dispatch_meta: dispatch 元数据
            req: agent 请求
            stream_state: 流状态
            trace_id: trace ID
        """
        logger.info(
            "cron agent stream start: job_id=%s channel=%s "
            "session_id=%s trace_id=%s timeout=%ss",
            job.id,
            job.dispatch.channel,
            target_session_id[:40] if target_session_id else "",
            trace_id[:20] if trace_id else "(empty)",
            job.runtime.timeout_seconds,
        )
        timeout_ctx = (
            asyncio.timeout(job.runtime.timeout_seconds)
            if hasattr(asyncio, "timeout")
            else None
        )
        if timeout_ctx:
            async with timeout_ctx:
                await self._run_agent_stream(
                    job,
                    target_user_id,
                    target_session_id,
                    dispatch_meta,
                    req,
                    stream_state,
                )
        else:
            await asyncio.wait_for(
                self._run_agent_stream(
                    job,
                    target_user_id,
                    target_session_id,
                    dispatch_meta,
                    req,
                    stream_state,
                ),
                timeout=job.runtime.timeout_seconds,
            )

    async def _handle_agent_failed_after_stream(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        trace_id: Optional[str],
    ) -> None:
        """处理 stream 返回后检测到 Failed 事件。

        Args:
            job: 任务定义
            stream_state: 流状态
            trace_id: trace ID

        Raises:
            RuntimeError: 总是抛出
        """
        self._log_agent_stream_failed(job, stream_state)
        await self._end_trace_on_exception(
            trace_id,
            TraceStatus.ERROR,
            stream_state.error_message,
        )
        exc = RuntimeError(
            f"Agent execution failed: {stream_state.error_message}",
        )
        setattr(exc, "cron_trace_id", trace_id)
        raise exc

    async def _handle_agent_no_completion(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        trace_id: Optional[str],
    ) -> None:
        """处理 stream 返回但没有 Completed 事件的情况。

        当模型不可用或其他原因导致 agent 未正常完成时，
        stream 可能返回空流或只有中间事件但没有 Completed 事件。
        这种情况应视为执行失败。

        Args:
            job: 任务定义
            stream_state: 流状态
            trace_id: trace ID

        Raises:
            RuntimeError: 总是抛出
        """
        error_msg = (
            f"Agent execution did not complete: "
            f"event_count={stream_state.event_count} "
            f"output_len={stream_state.output_len}"
        )
        logger.warning(
            "cron agent stream returned without completion: job_id=%s "
            "event_count=%s output_len=%s failed_seen=%s "
            "last_event_object=%s last_event_status=%s unknown_event_count=%s",
            job.id,
            stream_state.event_count,
            stream_state.output_len,
            stream_state.failed_seen,
            stream_state.last_event_object,
            stream_state.last_event_status,
            stream_state.unknown_event_count,
        )
        await self._end_trace_on_exception(
            trace_id,
            TraceStatus.ERROR,
            error_msg,
        )
        exc = RuntimeError(error_msg)
        setattr(exc, "cron_trace_id", trace_id)
        raise exc

    async def _handle_agent_empty_output(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        trace_id: Optional[str],
    ) -> None:
        """Reject a completed Runtime response that has no assistant text."""
        error_msg = "Agent execution failed: empty_model_output"
        increment_cron_result_metric(CRON_EMPTY_MODEL_OUTPUT_TOTAL)
        stream_state.terminal_error_code = "empty_model_output"
        stream_state.error_message = "empty_model_output"
        logger.warning(
            "cron agent response completed without assistant output: "
            "job_id=%s event_count=%s output_source=%s",
            job.id,
            stream_state.event_count,
            stream_state.output_source,
        )
        await self._end_trace_on_exception(
            trace_id,
            TraceStatus.ERROR,
            error_msg,
        )
        exc = RuntimeError(error_msg)
        setattr(exc, "cron_trace_id", trace_id)
        raise exc

    async def _handle_agent_session_commit_failure(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        trace_id: Optional[str],
        error: str,
    ) -> NoReturn:
        """拒绝未确认会话提交的完成结果。"""
        error_msg = f"Agent execution failed: session_state_commit: {error}"
        increment_cron_result_metric(CRON_SESSION_COMMIT_FAILURE_TOTAL)
        stream_state.terminal_error_code = "session_state_commit_failed"
        stream_state.error_message = error
        logger.error(
            "cron agent session state was not committed: job_id=%s error=%s",
            job.id,
            error,
        )
        await self._end_trace_on_exception(
            trace_id,
            TraceStatus.ERROR,
            error_msg,
        )
        exc = RuntimeError(error_msg)
        setattr(exc, "cron_trace_id", trace_id)
        raise exc

    async def _handle_agent_timeout_error(
        self,
        job: CronJobSpec,
        trace_id: Optional[str],
        runtime_tenant_id: str,
        stream_state: AgentStreamState,
    ) -> None:
        """处理 Agent 执行超时异常。

        Args:
            job: 任务定义
            trace_id: trace ID
            runtime_tenant_id: 租户 ID

        Raises:
            asyncio.TimeoutError: 总是抛出
        """
        if stream_state.response_cancelled_seen:
            await self._end_trace_on_exception(
                trace_id,
                TraceStatus.CANCELLED,
                stream_state.error_message or None,
            )
            exc = asyncio.CancelledError()
            setattr(exc, "cron_trace_id", trace_id)
            self._attach_stream_state_execution_meta(exc, stream_state)
            raise exc
        if stream_state.response_failed_seen:
            await self._end_trace_on_exception(
                trace_id,
                TraceStatus.ERROR,
                stream_state.error_message or None,
            )
            exc = RuntimeError(
                f"Agent execution failed: {stream_state.error_message}",
            )
            setattr(exc, "cron_trace_id", trace_id)
            self._attach_stream_state_execution_meta(exc, stream_state)
            raise exc
        self._log_agent_timeout(job)
        await self._notify_timeout(job, runtime_tenant_id)
        await self._end_trace_on_exception(
            trace_id,
            TraceStatus.ERROR,
            "Timeout",
        )
        # 附加 trace_id 到异常，以便 manager 获取
        exc = asyncio.TimeoutError()
        setattr(exc, "cron_trace_id", trace_id)
        self._attach_stream_state_execution_meta(exc, stream_state)
        raise exc

    async def _handle_agent_cancelled_error(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        trace_id: Optional[str],
        req: Dict[str, Any],
        dispatch_meta: Dict[str, Any],
        runtime_tenant_id: str,
    ) -> Optional[Dict[str, Any]]:
        """处理 Agent 执行取消异常。

        Args:
            job: 任务定义
            stream_state: 流状态
            trace_id: trace ID
            req: agent 请求

        Returns:
            如果取消时已完成，返回成功结果；否则返回 None

        Raises:
            asyncio.CancelledError: 如果取消前未完成
        """
        if stream_state.response_cancelled_seen:
            logger.info(
                "cron agent response cancelled: job_id=%s event_count=%s "
                "error_code=%s error=%s",
                job.id,
                stream_state.event_count,
                stream_state.terminal_error_code,
                stream_state.error_message,
            )
            await self._end_trace_on_exception(
                trace_id,
                TraceStatus.CANCELLED,
                stream_state.error_message or None,
            )
            exc = asyncio.CancelledError()
            setattr(exc, "cron_trace_id", trace_id)
            self._attach_stream_state_execution_meta(exc, stream_state)
            raise exc

        if stream_state.failed_seen:
            self._log_agent_cancelled_after_failed(job, stream_state)
            await self._end_trace_on_exception(
                trace_id,
                TraceStatus.ERROR,
                stream_state.error_message,
            )
            exc = asyncio.CancelledError()
            setattr(exc, "cron_trace_id", trace_id)
            self._attach_stream_state_execution_meta(exc, stream_state)
            raise exc

        await self._wait_for_session_commit_after_cancellation(
            req,
            stream_state,
        )
        if (
            self._has_agent_completed_output(stream_state)
            and stream_state.session_state_committed
            and stream_state.persisted_assistant_message_count > 0
            and stream_state.stream_returned
        ):
            await self._deliver_persisted_agent_output(
                job,
                str(req.get("user_id") or ""),
                str(req.get("session_id") or ""),
                dispatch_meta,
                stream_state,
                req,
            )
            increment_cron_result_metric(CRON_CANCEL_AFTER_COMPLETED_TOTAL)
            cancelling_count = self._current_task_cancelling_count()
            uncancelled = self._uncancel_current_task()
            self._log_agent_cancelled_after_completed(
                job,
                stream_state,
                cancelling_count,
                uncancelled,
            )
            await self._end_trace_on_success(trace_id, job.id)
            result = self._build_agent_execution_result(
                trace_id,
                stream_state.output_parts,
                req,
            )
            result["execution_meta"] = self._stream_state_execution_meta(
                stream_state,
            )
            return result

        self._log_agent_cancelled_before_completed(job, stream_state)
        await self._end_trace_on_exception(trace_id, TraceStatus.CANCELLED)
        exc = asyncio.CancelledError()
        setattr(exc, "cron_trace_id", trace_id)
        self._attach_stream_state_execution_meta(exc, stream_state)
        raise exc

    async def _wait_for_session_commit_after_cancellation(
        self,
        req: Dict[str, Any],
        stream_state: AgentStreamState,
    ) -> None:
        """等待已关闭的 Runner 流报告本次 query 的 session 提交结果。"""
        waiter = getattr(
            self._runner,
            "wait_for_query_persistence_result",
            None,
        )
        if not callable(waiter):
            return
        cancelling_count = self._current_task_cancelling_count()
        uncancelled = self._uncancel_current_task()
        try:
            result = await asyncio.wait_for(
                waiter(
                    session_id=str(req.get("session_id") or ""),
                    user_id=str(req.get("user_id") or ""),
                    execution_key=str(req.get("cron_persistence_key") or ""),
                    timeout_seconds=CRON_SESSION_CLEANUP_WAIT_SECONDS,
                ),
                timeout=CRON_SESSION_CLEANUP_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "cron session cleanup wait timed out: session_id=%s "
                "timeout=%ss cancelling_count=%s uncancelled=%s",
                req.get("session_id", ""),
                CRON_SESSION_CLEANUP_WAIT_SECONDS,
                cancelling_count,
                uncancelled,
            )
            return
        except asyncio.CancelledError:
            logger.info(
                "cron session cleanup wait cancelled again: session_id=%s",
                req.get("session_id", ""),
            )
            return
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "cron session cleanup result unavailable: session_id=%s error=%s",
                req.get("session_id", ""),
                exc,
            )
            return
        if result is None:
            return
        self._apply_persistence_result(stream_state, result)
        if result.committed:
            # 提交回执只会在 Runner 流 finally 完成后发出，因此可视为流已关闭。
            stream_state.stream_returned = True

    def _apply_persistence_result(
        self,
        stream_state: AgentStreamState,
        result: Any,
    ) -> None:
        """Apply the persisted query receipt to the Cron stream state."""
        stream_state.session_state_commit_attempted = bool(
            result.commit_attempted,
        )
        stream_state.session_state_committed = bool(result.committed)
        stream_state.idempotent_replay = bool(
            getattr(result, "idempotent_replay", False),
        )
        stream_state.output_delivery_replay_supported = bool(
            getattr(result, "output_delivery_replay_supported", False),
        )
        stream_state.output_delivery_completed = bool(
            getattr(result, "output_delivery_completed", False),
        )
        stream_state.output_delivery_receipt_supported = (
            callable(
                getattr(
                    self._runner,
                    "mark_cron_output_delivery_completed",
                    None,
                ),
            )
            and stream_state.output_delivery_replay_supported
        )
        stream_state.persisted_assistant_message_count = int(
            result.assistant_message_count,
        )
        content = getattr(result, "persisted_assistant_content", None)
        if isinstance(content, list):
            stream_state.persisted_assistant_content = [
                block for block in content if isinstance(block, dict)
            ]
        self._replace_final_message_with_persisted_content(stream_state)

    @staticmethod
    def _replace_final_message_with_persisted_content(
        stream_state: AgentStreamState,
    ) -> None:
        """Make downstream delivery match the assistant content on disk."""
        if not stream_state.persisted_assistant_content:
            return
        content = [
            SimpleNamespace(**block)
            for block in stream_state.persisted_assistant_content
        ]
        stream_state.completed_message_event = SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            role="assistant",
            content=content,
        )
        persisted_text = CronExecutor._extract_text_from_content(content)
        if persisted_text:
            stream_state.output_parts = [persisted_text]
            stream_state.output_source = "persisted"

    async def _confirm_agent_persistence_result(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        trace_id: Optional[str],
        req: Dict[str, Any],
    ) -> None:
        """Require a committed receipt and use its persisted assistant output."""
        persistence_getter = getattr(
            self._runner,
            "get_query_persistence_result",
            None,
        )
        if not callable(persistence_getter):
            await self._handle_agent_session_commit_failure(
                job,
                stream_state,
                trace_id,
                "persistence_result_unavailable",
            )

        persistence_result = persistence_getter(
            session_id=str(req.get("session_id") or ""),
            user_id=str(req.get("user_id") or ""),
            execution_key=str(req.get("cron_persistence_key") or ""),
        )
        if persistence_result is None:
            await self._handle_agent_session_commit_failure(
                job,
                stream_state,
                trace_id,
                "persistence_result_unavailable",
            )

        self._apply_persistence_result(stream_state, persistence_result)
        if not persistence_result.committed:
            await self._handle_agent_session_commit_failure(
                job,
                stream_state,
                trace_id,
                persistence_result.commit_error or "commit_not_confirmed",
            )
        if persistence_result.assistant_message_count <= 0:
            await self._handle_agent_session_commit_failure(
                job,
                stream_state,
                trace_id,
                "persisted_assistant_missing",
            )

    async def _handle_agent_generic_exception(
        self,
        job: CronJobSpec,
        trace_id: Optional[str],
        error: Exception,
    ) -> None:
        """处理 Agent 执行通用异常。

        Args:
            job: 任务定义
            trace_id: trace ID
            error: 异常对象

        Raises:
            Exception: 总是抛出原异常
        """
        self._log_agent_generic_error(job, error)
        await self._end_trace_on_exception(
            trace_id,
            TraceStatus.ERROR,
            str(error),
        )

    async def _execute_agent_job(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute agent-type job: run agent query and send events.

        Returns:
            Dict with trace_id, output_preview, input_snapshot, executor_leader
        """
        runtime_tenant_id, trace_id, req, stream_state = (
            await self._prepare_agent_execution(
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
            )
        )

        trace_ended = False
        result: Optional[Dict[str, Any]] = None
        try:
            await self._run_agent_stream_with_timeout(
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
                req,
                stream_state,
                trace_id,
            )
            if stream_state.failed_seen:
                trace_ended = True
                await self._handle_agent_failed_after_stream(
                    job,
                    stream_state,
                    trace_id,
                )

            if stream_state.response_cancelled_seen:
                raise asyncio.CancelledError()

            # Runtime 协议以 response/completed 为唯一成功终态；
            # message/completed 仅用于提取实际 assistant 输出。
            if not stream_state.response_completed_seen:
                trace_ended = True
                await self._handle_agent_no_completion(
                    job,
                    stream_state,
                    trace_id,
                )

            if (
                stream_state.response_completed_seen
                and not stream_state.assistant_message_seen
            ):
                trace_ended = True
                await self._handle_agent_empty_output(
                    job,
                    stream_state,
                    trace_id,
                )

            await self._confirm_agent_persistence_result(
                job,
                stream_state,
                trace_id,
                req,
            )

            await self._deliver_persisted_agent_output(
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
                stream_state,
                req,
            )

            result = self._build_agent_execution_result(
                trace_id,
                stream_state.output_parts,
                req,
            )
            result["execution_meta"] = self._stream_state_execution_meta(
                stream_state,
            )
            if (
                result.get("status") == "success"
                and not stream_state.assistant_message_seen
            ):
                increment_cron_result_metric(
                    CRON_SUCCESS_WITHOUT_ASSISTANT_TOTAL,
                )
        except asyncio.TimeoutError:
            trace_ended = True
            await self._handle_agent_timeout_error(
                job,
                trace_id,
                runtime_tenant_id,
                stream_state,
            )
            raise
        except asyncio.CancelledError:
            trace_ended = True
            cancelled_result = await self._handle_agent_cancelled_error(
                job,
                stream_state,
                trace_id,
                req,
                dispatch_meta,
                runtime_tenant_id,
            )
            if cancelled_result is not None:
                return cancelled_result
            raise
        except Exception as e:  # pylint: disable=broad-except
            trace_ended = True
            self._attach_stream_state_execution_meta(e, stream_state)
            await self._handle_agent_generic_exception(job, trace_id, e)
            # 附加 trace_id 到异常，以便 manager 获取
            setattr(e, "cron_trace_id", trace_id)
            raise
        finally:
            self._discard_query_persistence_result(req)
            if trace_id and not trace_ended:
                await self._end_trace_on_success(trace_id, job.id)

        return result

    def _build_agent_request(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        *,
        dispatch_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build agent request dict from job spec."""
        req = self._build_base_agent_request(
            job,
            target_user_id,
            target_session_id,
        )
        self._apply_cron_execution_identity(job, req, dispatch_meta)
        self._apply_agent_request_identity_metadata(job, req)
        return req

    @staticmethod
    def _build_base_agent_request(
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
    ) -> Dict[str, Any]:
        req: Dict[str, Any] = job.request.model_dump(mode="json")
        removed_trace_id = req.pop("trace_id", None)
        if removed_trace_id:
            logger.warning(
                "cron agent request removed stale trace_id=%s for job_id=%s",
                str(removed_trace_id)[:20],
                job.id,
            )
        req["user_id"] = req.get("user_id") or target_user_id or "cron"
        req["session_id"] = (
            req.get("session_id") or target_session_id or f"cron:{job.id}"
        )
        req["skip_history"] = True  # 标记定时任务不加载历史会话
        req["execution_origin"] = "scheduled"
        req["cron_timeout_seconds"] = job.runtime.timeout_seconds
        req["cron_job_id"] = job.id
        req["cron_persistence_key"] = str(uuid.uuid4())
        return req

    @staticmethod
    def _apply_cron_execution_identity(
        job: CronJobSpec,
        req: Dict[str, Any],
        dispatch_meta: Optional[Dict[str, Any]],
    ) -> None:
        execution_key = _build_cron_execution_key(
            job_id=job.id,
            target_session_id=req["session_id"],
            dispatch_meta=dispatch_meta or {},
        )
        if (
            not execution_key
            and dispatch_meta
            and not bool(
                dispatch_meta.get("cron_is_manual"),
            )
        ):
            raise RuntimeError(
                "cron agent delivery requires execution identity",
            )
        if execution_key:
            req["cron_execution_key"] = execution_key

    @staticmethod
    def _apply_agent_request_identity_metadata(
        job: CronJobSpec,
        req: Dict[str, Any],
    ) -> None:
        if job.source_id:
            req["source_id"] = job.source_id
        scope_id = CronExecutor._resolve_agent_request_scope_id(job)
        if scope_id:
            req["scope_id"] = scope_id
        if job.bbk_id:
            req["bbk_id"] = job.bbk_id
        if job.tenant_name:
            req["user_name"] = job.tenant_name

    @staticmethod
    def _resolve_agent_request_scope_id(job: CronJobSpec) -> str | None:
        if job.scope_id is not None:
            return canonicalize_scope_id(job.scope_id)
        if (
            getattr(job, "tenant_id", None) == "default"
            and job.source_id is not None
        ):
            return None
        return resolve_scope_id(
            getattr(job, "tenant_id", None),
            job.source_id,
        )

    async def _apply_auth_token(
        self,
        job: CronJobSpec,
        dispatch_meta: Dict[str, Any],
        req: Dict[str, Any],
    ) -> None:
        """Resolve and apply auth token to request."""
        runtime_tenant_id = (
            canonicalize_scope_id(job.scope_id)
            if job.scope_id is not None
            else (
                resolve_request_effective_tenant_id(
                    getattr(job, "tenant_id", None),
                    getattr(job, "source_id", None),
                    None,
                )
                if getattr(job, "tenant_id", None) == "default"
                else resolve_scope_id(
                    getattr(job, "tenant_id", None),
                    getattr(job, "source_id", None),
                )
            )
        )
        try:
            resolved = await resolve_auth_token_for_execution(
                tenant_id=runtime_tenant_id,
                workspace_dir=dispatch_meta.get("workspace_dir"),
            )
        except ValueError as exc:
            logger.warning(
                "cron agent aborted: job_id=%s auth_state_error=%s",
                job.id,
                repr(exc),
            )
            raise RuntimeError(
                "cron auth user_info is expired; "
                "please refresh cron auth configuration",
            ) from exc
        if resolved.token:
            req["auth_token"] = resolved.token
        if resolved.cookie_header:
            req["cookie"] = resolved.cookie_header

    async def _run_agent_stream(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        req: Dict[str, Any],
        stream_state: AgentStreamState,
    ) -> None:
        """Run agent stream query and send events to channel."""
        stream = self._runner.stream_query(req)
        async for event in stream:
            stream_state.event_count += 1
            self._record_agent_stream_event(job, event, stream_state)
            flags = self._classify_agent_stream_event(event)
            self._collect_agent_stream_output(event, stream_state)
            self._apply_agent_stream_event_state(
                job,
                event,
                flags,
                stream_state,
            )
            if flags.requires_terminal_notification:
                await self._notify_agent_stream_terminal_event(
                    job,
                    target_user_id,
                    target_session_id,
                    dispatch_meta,
                    event,
                    stream_state,
                )
            if flags.terminates_stream:
                await self._close_terminal_agent_stream(stream)
                stream_state.stream_returned = True
                break
        stream_state.stream_returned = True
        logger.info(
            "cron agent stream returned: job_id=%s event_count=%s "
            "completed_seen=%s completed_sent=%s failed_seen=%s "
            "response_completed_seen=%s response_failed_seen=%s "
            "response_cancelled_seen=%s terminal_response_status=%s "
            "last_event_object=%s last_event_status=%s "
            "unknown_event_count=%s output_len=%s",
            job.id,
            stream_state.event_count,
            stream_state.completed_seen,
            stream_state.completed_message_sent,
            stream_state.failed_seen,
            stream_state.response_completed_seen,
            stream_state.response_failed_seen,
            stream_state.response_cancelled_seen,
            stream_state.terminal_response_status,
            stream_state.last_event_object,
            stream_state.last_event_status,
            stream_state.unknown_event_count,
            stream_state.output_len,
        )

    @staticmethod
    def _classify_agent_stream_event(event: Any) -> _AgentStreamEventFlags:
        return _AgentStreamEventFlags(
            completed_message=CronExecutor._is_completed_message_event(event),
            failed_message=CronExecutor._is_failed_message_event(event),
            completed_response=(
                CronExecutor._is_completed_response_event(event)
            ),
            failed_response=CronExecutor._is_failed_response_event(event),
            cancelled_response=(
                CronExecutor._is_cancelled_response_event(event)
            ),
        )

    def _collect_agent_stream_output(
        self,
        event: Any,
        stream_state: AgentStreamState,
    ) -> None:
        output_entries = self._extract_assistant_output_entries(event)
        for output_key, text, output_source in output_entries:
            if output_key and output_key in stream_state._output_event_keys:
                continue
            if output_key:
                stream_state._output_event_keys.add(output_key)
            stream_state.output_parts.append(text)
            stream_state.assistant_message_seen = True
            stream_state.assistant_message_count += 1
            if not stream_state.output_source:
                stream_state.output_source = output_source

    def _apply_agent_stream_event_state(
        self,
        job: CronJobSpec,
        event: Any,
        flags: _AgentStreamEventFlags,
        stream_state: AgentStreamState,
    ) -> None:
        if flags.completed_message:
            stream_state.completed_message_seen = True
            stream_state.completed_message_event = event
            logger.info(
                "cron agent completed message received: job_id=%s "
                "event_count=%s output_len=%s",
                job.id,
                stream_state.event_count,
                stream_state.output_len,
            )
        if flags.failed_message:
            stream_state.failed_message_seen = True
            self._set_stream_error_from_event(stream_state, event)
            logger.warning(
                "cron agent failed message received: job_id=%s "
                "event_count=%s error=%s",
                job.id,
                stream_state.event_count,
                stream_state.error_message,
            )
        if flags.completed_response:
            stream_state.response_completed_seen = True
            stream_state.terminal_response_status = RunStatus.Completed
        if flags.failed_response:
            stream_state.response_failed_seen = True
            stream_state.terminal_response_status = getattr(
                event,
                "status",
                RunStatus.Failed,
            )
            self._set_stream_error_from_event(stream_state, event)
        if flags.cancelled_response:
            stream_state.response_cancelled_seen = True
            stream_state.terminal_response_status = RunStatus.Canceled
            self._set_stream_error_from_event(stream_state, event)

    async def _notify_agent_stream_terminal_event(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        event: Any,
        stream_state: AgentStreamState,
    ) -> None:
        await self._channel_manager.send_event(
            channel=job.dispatch.channel,
            user_id=target_user_id,
            session_id=target_session_id,
            event=event,
            meta=dispatch_meta,
        )
        await self._send_agent_terminal_text(
            job,
            target_user_id,
            target_session_id,
            dispatch_meta,
            stream_state,
        )

    @staticmethod
    async def _close_terminal_agent_stream(stream: Any) -> None:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            await aclose()

    async def _send_agent_stream_events(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        stream_state: AgentStreamState,
        delivery_key: str = "",
    ) -> bool:
        """Forward the single verified final assistant message, if present."""
        event = stream_state.completed_message_event
        if event is None:
            return False
        delivery_meta = dict(dispatch_meta)
        if delivery_key:
            delivery_meta["cron_delivery_key"] = delivery_key
        delivered = await self._channel_manager.send_event(
            channel=job.dispatch.channel,
            user_id=target_user_id,
            session_id=target_session_id,
            event=event,
            meta=delivery_meta,
        )
        if delivered is not True:
            logger.warning(
                "cron agent completed message not delivered: job_id=%s "
                "channel=%s",
                job.id,
                job.dispatch.channel,
            )
            return False
        stream_state.completed_message_event = None
        stream_state.completed_message_sent = True
        logger.info(
            "cron agent completed message sent: job_id=%s "
            "event_count=%s output_len=%s",
            job.id,
            stream_state.event_count,
            stream_state.output_len,
        )
        return True

    async def _send_agent_terminal_text(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        stream_state: AgentStreamState,
    ) -> None:
        """Notify channels whose event protocol only accepts completed messages."""
        sender = getattr(self._channel_manager, "send_text", None)
        if not callable(sender):
            return
        if stream_state.response_cancelled_seen:
            prefix = f"定时任务 [{job.name}] 已取消"
        else:
            prefix = f"定时任务 [{job.name}] 执行失败"
        detail = stream_state.error_message or "模型未返回可用结果"
        await sender(
            channel=job.dispatch.channel,
            user_id=target_user_id,
            session_id=target_session_id,
            text=f"{prefix}: {detail}",
            meta=dispatch_meta,
        )
        stream_state.terminal_notification_sent = True

    async def _deliver_persisted_agent_output(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        stream_state: AgentStreamState,
        req: Dict[str, Any],
    ) -> None:
        """Deliver persisted output once, retrying only unconfirmed replays."""
        if stream_state.output_delivery_completed:
            return
        if self._skip_unreplayable_console_delivery(job, stream_state):
            return
        delivery_key = self._build_output_delivery_key(req)
        if await self._deliver_with_session_receipt(
            job,
            target_user_id,
            target_session_id,
            dispatch_meta,
            stream_state,
            req,
            delivery_key,
        ):
            return
        await self._deliver_with_runner_receipt(
            job,
            target_user_id,
            target_session_id,
            dispatch_meta,
            stream_state,
            req,
            delivery_key,
        )

    @staticmethod
    def _build_output_delivery_key(req: Dict[str, Any]) -> str:
        delivery_identity = str(
            req.get("cron_execution_key")
            or req.get("cron_persistence_key")
            or "",
        )
        return f"cron:{delivery_identity}:output" if delivery_identity else ""

    @staticmethod
    def _skip_unreplayable_console_delivery(
        job: CronJobSpec,
        stream_state: AgentStreamState,
    ) -> bool:
        if stream_state.output_delivery_receipt_supported:
            return False
        if job.dispatch.channel != CONSOLE_CHANNEL:
            raise RuntimeError("cron output delivery receipt unavailable")
        return stream_state.idempotent_replay

    async def _deliver_with_session_receipt(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        stream_state: AgentStreamState,
        req: Dict[str, Any],
        delivery_key: str,
    ) -> bool:
        session = getattr(self._runner, "session", None)
        execution_factory = getattr(session, "execution", None)
        session_id = str(req.get("session_id") or "")
        user_id = str(req.get("user_id") or "")
        if not callable(execution_factory) or not session_id or not user_id:
            return False
        async with execution_factory(
            session_id,
            user_id=user_id,
            timeout_seconds=5.0,
        ) as session_execution:
            task_run = self._find_cron_task_run(
                session_execution.state,
                execution_key=str(req.get("cron_execution_key") or ""),
                persistence_key=str(req.get("cron_persistence_key") or ""),
            )
            if task_run is None:
                raise RuntimeError("cron output delivery receipt unavailable")
            if task_run.get("output_delivery_completed"):
                stream_state.output_delivery_completed = True
                return True
            await self._send_and_commit_session_delivery(
                session_execution,
                task_run,
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
                stream_state,
                delivery_key,
            )
        return True

    async def _send_and_commit_session_delivery(
        self,
        session_execution: Any,
        task_run: dict[str, Any],
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        stream_state: AgentStreamState,
        delivery_key: str,
    ) -> None:
        task_run["output_delivery_state"] = CRON_DELIVERY_STATE_UNCERTAIN
        await session_execution.commit_state(session_execution.state)
        try:
            message_delivered = await self._send_agent_stream_events(
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
                stream_state,
                delivery_key,
            )
        except Exception as exc:  # pylint: disable=broad-except
            if job.dispatch.channel != CONSOLE_CHANNEL:
                raise
            logger.warning(
                "cron console output delivery failed; treating as local "
                "completion: job_id=%s error=%s",
                job.id,
                exc,
                exc_info=True,
            )
            message_delivered = False
        if not message_delivered and job.dispatch.channel != CONSOLE_CHANNEL:
            raise RuntimeError("cron output delivery not confirmed")
        task_run["output_delivery_state"] = CRON_DELIVERY_STATE_COMPLETED
        task_run["output_delivery_completed"] = True
        await session_execution.commit_state(session_execution.state)
        stream_state.output_delivery_completed = True

    async def _deliver_with_runner_receipt(
        self,
        job: CronJobSpec,
        target_user_id: str,
        target_session_id: str,
        dispatch_meta: Dict[str, Any],
        stream_state: AgentStreamState,
        req: Dict[str, Any],
        delivery_key: str,
    ) -> None:
        try:
            message_delivered = await self._send_agent_stream_events(
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
                stream_state,
                delivery_key,
            )
        except Exception as exc:  # pylint: disable=broad-except
            if job.dispatch.channel != CONSOLE_CHANNEL:
                raise
            logger.warning(
                "cron console output delivery failed; treating as local "
                "completion: job_id=%s error=%s",
                job.id,
                exc,
                exc_info=True,
            )
            return
        if job.dispatch.channel == CONSOLE_CHANNEL:
            return
        if not message_delivered:
            raise RuntimeError("cron output delivery not confirmed")
        marker = getattr(
            self._runner,
            "mark_cron_output_delivery_completed",
            None,
        )
        if not callable(marker):
            raise RuntimeError("cron output delivery receipt unavailable")
        await marker(
            session_id=str(req.get("session_id") or ""),
            user_id=str(req.get("user_id") or ""),
            execution_key=str(req.get("cron_execution_key") or ""),
            persistence_key=str(req.get("cron_persistence_key") or ""),
        )
        stream_state.output_delivery_completed = True

    @staticmethod
    def _find_cron_task_run(
        state: dict[str, Any],
        *,
        execution_key: str,
        persistence_key: str,
    ) -> dict[str, Any] | None:
        task_runs = state.get("task_runs")
        if not isinstance(task_runs, list):
            return None
        for task_run in reversed(task_runs):
            if not isinstance(task_run, dict):
                continue
            if (
                persistence_key
                and task_run.get("persistence_key") == persistence_key
            ):
                return task_run
            if (
                execution_key
                and task_run.get("execution_key") == execution_key
            ):
                return task_run
        return None

    async def _notify_timeout(self, job: CronJobSpec, tenant_id: str) -> None:
        """Push a timeout notification to the console so the user is aware.

        Falls back to logging if the push fails; never raises.
        """
        task_session_id: Optional[str] = (job.meta or {}).get(
            "task_session_id",
        )
        target_session_id = task_session_id or job.dispatch.target.session_id
        if not target_session_id:
            logger.warning(
                "cron timeout: no session_id for job_id=%s",
                job.id,
            )
            return
        timeout_text = (
            f"⏰ 定时任务 [{job.name}] 执行超时"
            f"（{job.runtime.timeout_seconds}s），已自动终止。"
        )
        try:
            await self._push_to_console(
                target_session_id,
                timeout_text,
                tenant_id,
            )
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Failed to push timeout notification for job_id=%s",
                job.id,
                exc_info=True,
            )

    async def _push_to_console(
        self,
        session_id: str,
        text: str,
        tenant_id: str,
        *,
        delivery_key: str = "",
    ) -> None:
        """Push message to console channel for frontend notification."""
        if not session_id or not text:
            return
        logger.info(
            "cron push_to_console: session_id=%s text_len=%s tenant_id=%s",
            session_id[:40] if session_id else "",
            len(text),
            tenant_id,
        )
        await push_store_append(
            session_id,
            text.strip(),
            tenant_id=tenant_id,
            delivery_key=delivery_key,
        )

    def _extract_text_from_event(self, event: Any) -> str:
        """Extract text content from a runner event.

        Args:
            event: Runner event (from stream_query)

        Returns:
            Extracted text string, empty if no text found
        """
        return "\n".join(
            text
            for _key, text, _source in self._extract_assistant_output_entries(
                event,
            )
        )

    def _discard_query_persistence_result(self, req: Dict[str, Any]) -> None:
        """Consume a terminal query receipt on every Executor exit path."""
        getter = getattr(self._runner, "get_query_persistence_result", None)
        if not callable(getter):
            return
        getter(
            session_id=str(req.get("session_id") or ""),
            user_id=str(req.get("user_id") or ""),
            execution_key=str(req.get("cron_persistence_key") or ""),
        )

    @classmethod
    def _extract_assistant_output_entries(
        cls,
        event: Any,
    ) -> list[tuple[str, str, str]]:
        """Extract assistant text from completed message/response events.

        Runtime adapters can expose the same assistant message both as a
        ``message/completed`` event and inside ``response.output``.  Return a
        stable key for each message so the stream consumer can de-duplicate it.
        """
        if cls._is_completed_message_event(event):
            text = cls._extract_text_from_message_event(event)
            if not text:
                return []
            return [(cls._event_output_key(event), text, "message")]
        if cls._is_completed_response_event(event):
            return cls._extract_text_from_response_event(event)
        return []

    @staticmethod
    def _event_output_key(event: Any) -> str:
        event_id = getattr(event, "id", None)
        if event_id:
            return f"id:{event_id}"
        sequence_number = getattr(event, "sequence_number", None)
        if sequence_number is not None:
            return f"sequence:{sequence_number}"
        return f"event:{id(event)}"

    @classmethod
    def _extract_text_from_message_event(cls, event: Any) -> str:
        role = getattr(event, "role", None)
        if role is not None and role != "assistant":
            return ""
        return cls._extract_text_from_content(getattr(event, "content", None))

    @classmethod
    def _extract_text_from_response_event(
        cls,
        event: Any,
    ) -> list[tuple[str, str, str]]:
        entries: list[tuple[str, str, str]] = []
        output = getattr(event, "output", None) or []
        if not isinstance(output, (list, tuple)):
            return entries
        for index, message in enumerate(output):
            role = getattr(message, "role", None)
            if role != "assistant":
                continue
            text = cls._extract_text_from_content(
                getattr(message, "content", None),
            )
            if not text:
                continue
            message_id = getattr(message, "id", None)
            if message_id:
                key = f"id:{message_id}"
            elif getattr(message, "sequence_number", None) is not None:
                key = f"sequence:{message.sequence_number}"
            elif (
                len(output) == 1
                and getattr(
                    event,
                    "sequence_number",
                    None,
                )
                is not None
            ):
                key = f"sequence:{event.sequence_number}"
            else:
                event_id = getattr(event, "id", None)
                key = (
                    f"id:{event_id}:output:{index}"
                    if event_id
                    else f"event:{id(event)}:output:{index}"
                )
            entries.append((key, text, "response"))
        return entries

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        if not isinstance(content, (list, tuple)):
            return ""
        text_parts: list[str] = []
        for part in content:
            part_type = getattr(part, "type", None)
            if part_type == "text":
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(str(text))
            elif part_type == "refusal":
                refusal = getattr(part, "refusal", None)
                if refusal:
                    text_parts.append(str(refusal))
        return "\n".join(text_parts)

    @staticmethod
    def _is_completed_message_event(event: Any) -> bool:
        from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

        return (
            getattr(event, "object", None) == "message"
            and getattr(event, "status", None) == RunStatus.Completed
        )

    @staticmethod
    def _is_failed_message_event(event: Any) -> bool:
        """检测消息或整轮响应的 Failed 状态事件。

        当模型调用失败时，Runtime 会 yield response/Failed 而不是抛出异常。
        我们需要检测这个事件以正确处理失败情况。

        Args:
            event: Runner event

        Returns:
            True if event is a failed message or response event
        """
        from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

        return (
            getattr(event, "object", None) in ("message", "response")
            and getattr(event, "status", None) == RunStatus.Failed
        )

    @staticmethod
    def _is_completed_response_event(event: Any) -> bool:
        return (
            getattr(event, "object", None) == "response"
            and getattr(event, "status", None) == RunStatus.Completed
        )

    @staticmethod
    def _is_failed_response_event(event: Any) -> bool:
        return getattr(event, "object", None) == "response" and getattr(
            event,
            "status",
            None,
        ) in {
            RunStatus.Failed,
            RunStatus.Rejected,
            RunStatus.Incomplete,
        }

    @staticmethod
    def _is_cancelled_response_event(event: Any) -> bool:
        return (
            getattr(event, "object", None) == "response"
            and getattr(event, "status", None) == RunStatus.Canceled
        )

    @staticmethod
    def _event_content_types(event: Any) -> str:
        content = getattr(event, "content", None)
        if not isinstance(content, list):
            return ""
        return ",".join(
            str(getattr(part, "type", type(part).__name__)) for part in content
        )

    @staticmethod
    def _sanitize_event_error_message(message: Any) -> str:
        safe_message = redact_sensitive_fragments(str(message or ""))
        if len(safe_message) <= CRON_EVENT_ERROR_MESSAGE_MAX_LENGTH:
            return safe_message
        return safe_message[:CRON_EVENT_ERROR_MESSAGE_MAX_LENGTH] + "..."

    def _record_agent_stream_event(
        self,
        job: CronJobSpec,
        event: Any,
        stream_state: AgentStreamState,
    ) -> None:
        event_object = str(getattr(event, "object", "") or "")
        event_status = str(getattr(event, "status", "") or "")
        stream_state.last_event_object = event_object
        stream_state.last_event_status = event_status
        if event_object not in {"response", "message", "content"}:
            stream_state.unknown_event_count += 1
        error_code, error_message = self._extract_error_details_from_event(
            event,
        )
        logger.info(
            "cron agent event: job_id=%s event_index=%s event_class=%s "
            "object=%s status=%s event_id=%s sequence_number=%s "
            "content_types=%s error_code=%s error_message=%s",
            job.id,
            stream_state.event_count,
            type(event).__name__,
            event_object,
            event_status,
            str(getattr(event, "id", "") or ""),
            str(getattr(event, "sequence_number", "") or ""),
            self._event_content_types(event),
            error_code,
            error_message,
        )

    def _set_stream_error_from_event(
        self,
        stream_state: AgentStreamState,
        event: Any,
    ) -> None:
        error_code, error_message = self._extract_error_details_from_event(
            event,
        )
        stream_state.terminal_error_code = error_code
        if (
            not error_code
            and not error_message
            and stream_state.terminal_response_status
        ):
            stream_state.error_message = str(
                stream_state.terminal_response_status,
            )
            return
        stream_state.error_message = (
            f"{error_code}: {error_message}"
            if error_code and error_message
            else error_message or error_code
        )

    def _extract_error_details_from_event(
        self,
        event: Any,
    ) -> tuple[str, str]:
        error = getattr(event, "error", None)
        if error is None:
            return "", ""
        return (
            str(getattr(error, "code", "") or ""),
            self._sanitize_event_error_message(
                getattr(error, "message", ""),
            ),
        )

    @staticmethod
    def _has_agent_completed_output(stream_state: AgentStreamState) -> bool:
        """判断 Agent 是否真正完成输出。

        只有在以下情况才视为成功：
        - 没有看到 Failed 事件
        - 看到了 Completed 事件

        如果 stream 只是正常返回但没有 Completed 事件（如模型调用失败），
        不应该视为成功。

        Args:
            stream_state: Agent 执行状态

        Returns:
            True if agent truly completed with output
        """
        # 如果看到了 Failed 事件，绝对不是成功
        if stream_state.failed_seen:
            return False
        # 必须同时收到最终 Completed 响应和可展示 assistant 输出。
        return (
            stream_state.response_completed_seen
            and stream_state.assistant_message_seen
        )

    @staticmethod
    def _agent_stream_phase(stream_state: AgentStreamState) -> str:
        if stream_state.failed_seen:
            return "failed"
        if stream_state.response_cancelled_seen:
            return "response_cancelled"
        if stream_state.stream_returned:
            return "stream_returned"
        if stream_state.response_completed_seen:
            return "completed"
        return "before_completed_message"

    @staticmethod
    def _current_task_cancelling_count() -> int:
        task = asyncio.current_task()
        if task is None:
            return 0
        cancelling = getattr(task, "cancelling", None)
        if not callable(cancelling):
            return 0
        return int(cancelling())

    @staticmethod
    def _uncancel_current_task() -> int:
        task = asyncio.current_task()
        if task is None:
            return 0
        uncancel = getattr(task, "uncancel", None)
        cancelling = getattr(task, "cancelling", None)
        if not callable(uncancel) or not callable(cancelling):
            return 0
        uncancelled = 0
        while int(cancelling()) > 0:
            uncancel()
            uncancelled += 1
        return uncancelled
