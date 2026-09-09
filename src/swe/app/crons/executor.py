# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

from .auth_state import resolve_auth_token_for_execution
from .model_slot_context import bind_model_slot_override
from .models import CronJobSpec
from ..b3_headers import (
    B3_TRACE_ID_META_KEY,
    PASSTHROUGH_HEADERS_META_KEY,
)
from ..tenant_context import bind_tenant_context
from ..console_push_store import append as push_store_append
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
    if dispatch_meta.get("cron_is_manual"):
        return ""
    explicit = dispatch_meta.get("cron_execution_key") or dispatch_meta.get(
        "execution_key",
    )
    if explicit:
        return str(explicit)

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
CRON_EVENT_ERROR_MESSAGE_MAX_LENGTH = 512


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
class ExecutionResult:
    """执行结果，包含 trace_id 和输出预览。

    用于将执行过程中的关键信息传递给调用方。
    """

    trace_id: str = ""
    output_preview: str = ""
    input_snapshot: Optional[Dict[str, Any]] = None  # 执行时的输入快照
    executor_leader: str = ""  # 执行者 leader ID
    execution_meta: Optional[Dict[str, Any]] = None


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

    @property
    def output_len(self) -> int:
        return len("\n".join(self.output_parts))

    @property
    def failed_seen(self) -> bool:
        return self.failed_message_seen or self.response_failed_seen

    @property
    def completed_seen(self) -> bool:
        return self.completed_message_seen or self.response_completed_seen


async def _index_model_output_to_monitor(
    trace_id: str,
    model_output: str,
) -> None:
    """通过 Monitor API 写入 model_output 到 ES.

    Args:
        trace_id: 追踪 ID
        model_output: 模型输出文本
    """
    monitor_url = os.environ.get(
        "SWE_MONITOR_API_URL",
        "http://127.0.0.1:9090",
    )
    url = f"{monitor_url}/monitor/tracing/model-output"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                url,
                json={
                    "trace_id": trace_id,
                    "model_output": model_output,
                },
            )
            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "success":
                    logger.info(
                        "Model output indexed via Monitor API: trace_id=%s",
                        trace_id,
                    )
                else:
                    logger.info(
                        "Model output write skipped: trace_id=%s, reason=%s",
                        trace_id,
                        result.get("reason", "unknown"),
                    )
            else:
                logger.warning(
                    "Monitor API returned %s: trace_id=%s",
                    response.status_code,
                    trace_id,
                )
    except httpx.TimeoutException:
        logger.warning("Monitor API timeout: trace_id=%s", trace_id)
    except Exception as e:
        logger.warning(
            "Failed to call Monitor API for model_output: %s",
            e,
        )


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
        setattr(exc, "cron_execution_meta", resolved_model.build_meta())

    def _build_execution_result(
        self,
        result: Dict[str, Any] | None,
        *,
        execution_meta: Dict[str, Any] | None,
    ) -> ExecutionResult:
        result = result or {}
        output_preview = str(result.get("output_preview") or "")
        return ExecutionResult(
            trace_id=str(result.get("trace_id") or ""),
            output_preview=output_preview[:100],
            input_snapshot=result.get("input_snapshot"),
            executor_leader=str(result.get("executor_leader") or ""),
            execution_meta=execution_meta,
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
        runtime_tenant_id = (
            dispatch_meta.get("scope_id")
            or dispatch_meta.get("tenant_id")
            or "default"
        )
        logger.info(
            "cron send_text: job_id=%s channel=%s len=%s",
            job.id,
            job.dispatch.channel,
            len(job.text or ""),
        )

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

        try:
            await self._send_text_to_channel(
                job,
                target_user_id,
                target_session_id,
                dispatch_meta,
                runtime_tenant_id,
            )
        finally:
            await self._end_trace_for_text_job(created_trace_id)

        internal_trace_id = created_trace_id or requested_trace_id

        # 返回执行结果
        output_preview = (job.text or "").strip()[:100]
        input_snapshot = (
            {"text": (job.text or "").strip()} if job.text else None
        )
        return {
            "trace_id": internal_trace_id or "",
            "output_preview": output_preview,
            "input_snapshot": input_snapshot,
            "executor_leader": "",
        }

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
        runtime_tenant_id: str,
    ) -> None:
        """发送 text 到 channel 并推送到 console。

        Args:
            job: 任务定义
            target_user_id: 目标用户 ID
            target_session_id: 目标会话 ID
            dispatch_meta: dispatch 元数据
            runtime_tenant_id: 运行时 scope/tenant 标识
        """
        await self._channel_manager.send_text(
            channel=job.dispatch.channel,
            user_id=target_user_id,
            session_id=target_session_id,
            text=job.text.strip(),
            meta=dispatch_meta,
        )
        task_chat_id: Optional[str] = (job.meta or {}).get("task_chat_id")
        if job.dispatch.channel != CONSOLE_CHANNEL and task_chat_id:
            await self._push_to_console(
                task_chat_id,
                job.text.strip(),
                runtime_tenant_id,
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
            self._apply_auth_token(job, dispatch_meta, req)
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

    async def _push_output_to_console(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        runtime_tenant_id: str,
    ) -> None:
        """推送 Agent 输出到 console。

        Args:
            job: 任务定义
            stream_state: 流状态
            runtime_tenant_id: 租户 ID
        """
        task_chat_id: Optional[str] = (job.meta or {}).get("task_chat_id")
        if (
            job.dispatch.channel != CONSOLE_CHANNEL
            and stream_state.output_parts
            and task_chat_id
        ):
            await self._push_to_console(
                task_chat_id,
                "\n".join(stream_state.output_parts),
                runtime_tenant_id,
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

    async def _handle_agent_timeout_error(
        self,
        job: CronJobSpec,
        trace_id: Optional[str],
        runtime_tenant_id: str,
    ) -> None:
        """处理 Agent 执行超时异常。

        Args:
            job: 任务定义
            trace_id: trace ID
            runtime_tenant_id: 租户 ID

        Raises:
            asyncio.TimeoutError: 总是抛出
        """
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
        raise exc

    async def _handle_agent_cancelled_error(
        self,
        job: CronJobSpec,
        stream_state: AgentStreamState,
        trace_id: Optional[str],
        req: Dict[str, Any],
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
            raise exc

        if self._has_agent_completed_output(stream_state):
            cancelling_count = self._current_task_cancelling_count()
            uncancelled = self._uncancel_current_task()
            self._log_agent_cancelled_after_completed(
                job,
                stream_state,
                cancelling_count,
                uncancelled,
            )
            await self._end_trace_on_success(trace_id, job.id)
            return self._build_agent_execution_result(
                trace_id,
                stream_state.output_parts,
                req,
            )

        self._log_agent_cancelled_before_completed(job, stream_state)
        await self._end_trace_on_exception(trace_id, TraceStatus.CANCELLED)
        exc = asyncio.CancelledError()
        setattr(exc, "cron_trace_id", trace_id)
        raise exc

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
            await self._push_output_to_console(
                job,
                stream_state,
                runtime_tenant_id,
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

            # Runtime 协议以 response/completed 为执行终态；message/completed
            # 仅用于提取实际输出，兼容旧 runner 才保留其成功判定。
            if not stream_state.completed_seen:
                trace_ended = True
                await self._handle_agent_no_completion(
                    job,
                    stream_state,
                    trace_id,
                )

            result = self._build_agent_execution_result(
                trace_id,
                stream_state.output_parts,
                req,
            )
        except asyncio.TimeoutError:
            trace_ended = True
            await self._handle_agent_timeout_error(
                job,
                trace_id,
                runtime_tenant_id,
            )
            raise
        except asyncio.CancelledError:
            trace_ended = True
            cancelled_result = await self._handle_agent_cancelled_error(
                job,
                stream_state,
                trace_id,
                req,
            )
            if cancelled_result is not None:
                return cancelled_result
            raise
        except Exception as e:  # pylint: disable=broad-except
            trace_ended = True
            await self._handle_agent_generic_exception(job, trace_id, e)
            # 附加 trace_id 到异常，以便 manager 获取
            setattr(e, "cron_trace_id", trace_id)
            raise
        finally:
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
        execution_key = _build_cron_execution_key(
            job_id=job.id,
            target_session_id=req["session_id"],
            dispatch_meta=dispatch_meta or {},
        )
        if execution_key:
            req["cron_execution_key"] = execution_key
        # 传递 source_id 用于 tracing 数据隔离
        if job.source_id:
            req["source_id"] = job.source_id
        scope_id = (
            canonicalize_scope_id(job.scope_id)
            if job.scope_id is not None
            else (
                None
                if getattr(job, "tenant_id", None) == "default"
                and job.source_id is not None
                else resolve_scope_id(
                    getattr(job, "tenant_id", None),
                    job.source_id,
                )
            )
        )
        if scope_id:
            req["scope_id"] = scope_id
        # 传递 bbk_id 用于 tracing 用户标识
        if job.bbk_id:
            req["bbk_id"] = job.bbk_id
        # 传递 user_name（从 tenant_name 字段获取）
        if job.tenant_name:
            req["user_name"] = job.tenant_name
        return req

    def _apply_auth_token(
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
            resolved = resolve_auth_token_for_execution(
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
        async for event in self._runner.stream_query(req):
            stream_state.event_count += 1
            self._record_agent_stream_event(job, event, stream_state)
            is_completed_message = self._is_completed_message_event(event)
            is_failed_message = self._is_failed_message_event(event)
            is_completed_response = self._is_completed_response_event(event)
            is_failed_response = self._is_failed_response_event(event)
            is_cancelled_response = self._is_cancelled_response_event(event)
            text = self._extract_text_from_event(event)
            if text:
                stream_state.output_parts.append(text)
            if is_completed_message:
                stream_state.completed_message_seen = True
                logger.info(
                    "cron agent completed message received: job_id=%s "
                    "event_count=%s output_len=%s",
                    job.id,
                    stream_state.event_count,
                    len(text),
                )
            if is_failed_message:
                stream_state.failed_message_seen = True
                self._set_stream_error_from_event(stream_state, event)
                logger.warning(
                    "cron agent failed message received: job_id=%s "
                    "event_count=%s error=%s",
                    job.id,
                    stream_state.event_count,
                    stream_state.error_message,
                )
            if is_completed_response:
                stream_state.response_completed_seen = True
                stream_state.terminal_response_status = RunStatus.Completed
            if is_failed_response:
                stream_state.response_failed_seen = True
                stream_state.terminal_response_status = getattr(
                    event,
                    "status",
                    RunStatus.Failed,
                )
                self._set_stream_error_from_event(stream_state, event)
            if is_cancelled_response:
                stream_state.response_cancelled_seen = True
                stream_state.terminal_response_status = RunStatus.Canceled
                self._set_stream_error_from_event(stream_state, event)
            await self._channel_manager.send_event(
                channel=job.dispatch.channel,
                user_id=target_user_id,
                session_id=target_session_id,
                event=event,
                meta=dispatch_meta,
            )
            if is_completed_message:
                stream_state.completed_message_sent = True
                logger.info(
                    "cron agent completed message sent: job_id=%s "
                    "event_count=%s output_len=%s",
                    job.id,
                    stream_state.event_count,
                    len(text),
                )
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

    async def _notify_timeout(self, job: CronJobSpec, tenant_id: str) -> None:
        """Push a timeout notification to the console so the user is aware.

        Falls back to logging if the push fails; never raises.
        """
        task_chat_id: Optional[str] = (job.meta or {}).get("task_chat_id")
        target_session_id = task_chat_id or job.dispatch.target.session_id
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
        await push_store_append(session_id, text.strip(), tenant_id=tenant_id)

    def _extract_text_from_event(self, event: Any) -> str:
        """Extract text content from a runner event.

        Args:
            event: Runner event (from stream_query)

        Returns:
            Extracted text string, empty if no text found
        """
        from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

        obj = getattr(event, "object", None)
        status = getattr(event, "status", None)

        # Only extract from completed message events
        if obj != "message" or status != RunStatus.Completed:
            return ""

        # Extract text from message content
        content = getattr(event, "content", None) or []
        text_parts: list[str] = []
        for part in content:
            part_type = getattr(part, "type", None)
            if part_type == "text":
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(text)
            elif part_type == "refusal":
                refusal = getattr(part, "refusal", None)
                if refusal:
                    text_parts.append(refusal)

        return "\n".join(text_parts) if text_parts else ""

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
        # 必须看到 Completed 事件才视为成功
        return stream_state.completed_seen

    @staticmethod
    def _agent_stream_phase(stream_state: AgentStreamState) -> str:
        if stream_state.failed_seen:
            return "failed"
        if stream_state.response_cancelled_seen:
            return "response_cancelled"
        if stream_state.stream_returned:
            return "stream_returned"
        if stream_state.completed_seen:
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
