# -*- coding: utf-8 -*-
"""Cron integration for the independent workflow HTTP engine."""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from swe.envs.runtime import load_tenant_runtime_env
from swe.config.context import resolve_scope_id
from swe.tracing import get_trace_manager, has_trace_manager
from swe.tracing.models import TraceStatus
from swe.tracing.output_index import index_trace_output

from ..execution_result import ExecutionResult
from ..auth_state import resolve_auth_token_for_execution
from ..models import CronJobSpec
from ...tenant_context import bind_tenant_context
from .engine import WorkflowCallError, WorkflowEngine, WorkflowOutcome
from .models import WorkflowConfig, WorkflowValueMapping

logger = logging.getLogger(__name__)


def _default_env_loader(job: CronJobSpec) -> dict[str, str]:
    return load_tenant_runtime_env(
        runtime_scope_id=job.scope_id,
        tenant_id=job.tenant_id,
        source_id=job.source_id,
        allow_missing_context=False,
    )


def _execution_key(job: CronJobSpec, dispatch_meta: Mapping[str, Any]) -> str:
    explicit = dispatch_meta.get("cron_execution_key") or dispatch_meta.get(
        "execution_key",
    )
    if explicit:
        return str(explicit)
    batch_id = dispatch_meta.get("batch_id")
    intent_id = dispatch_meta.get("intent_id")
    if batch_id and intent_id:
        return f"{job.id}:{batch_id}:{intent_id}"
    for field in (
        "scheduled_fire_at",
        "fire_time",
        "trigger_time",
        "external_execution_id",
    ):
        if dispatch_meta.get(field):
            return f"{job.id}:{dispatch_meta[field]}"
    return f"manual:{job.id}:{uuid4()}"


def _secret_keys(config: Any) -> set[str]:
    keys = {
        mapping.key
        for mapping in [
            *getattr(config, "headers", {}).values(),
            *getattr(config, "path_params", {}).values(),
            *getattr(config, "query", {}).values(),
        ]
        if isinstance(mapping, WorkflowValueMapping)
        and mapping.source == "secret"
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("source") == "secret" and value.get("key"):
                keys.add(str(value["key"]))
            else:
                for item in value.values():
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(getattr(config, "body", None))
    return keys


async def _default_secret_loader(
    job: CronJobSpec,
    keys: set[str],
    env: Mapping[str, str],
) -> dict[str, str]:
    secrets = {key: env[key] for key in keys if key in env}
    if not keys.intersection({"auth_token", "cookie"}):
        return secrets
    scope_id = job.scope_id or resolve_scope_id(job.tenant_id, job.source_id)
    workspace_dir = (job.dispatch.meta or {}).get("workspace_dir")
    try:
        resolved = await resolve_auth_token_for_execution(
            tenant_id=scope_id,
            workspace_dir=workspace_dir,
        )
    except ValueError as exc:
        raise RuntimeError(
            "cron auth user_info is expired; "
            "please refresh cron auth configuration",
        ) from exc
    if resolved.token:
        secrets["auth_token"] = resolved.token
    if resolved.cookie_header:
        secrets["cookie"] = resolved.cookie_header
    return secrets


class WorkflowCronExecutor:
    """Connect workflow output to Cron tracing and task-session contracts."""

    def __init__(
        self,
        *,
        config_store: Any,
        session: Any,
        channel_manager: Any,
        engine: WorkflowEngine | None = None,
        env_loader: Callable[[CronJobSpec], dict[str, str]] = (
            _default_env_loader
        ),
        secret_loader: Callable[
            [CronJobSpec, set[str], Mapping[str, str]],
            dict[str, str] | Awaitable[dict[str, str]],
        ] = _default_secret_loader,
        trace_manager: Callable[[], Any] | None = None,
    ) -> None:
        self._config_store = config_store
        self._session = session
        self._channel_manager = channel_manager
        self._engine = engine or WorkflowEngine()
        self._env_loader = env_loader
        self._secret_loader = secret_loader
        self._trace_manager = trace_manager or (
            lambda: get_trace_manager() if has_trace_manager() else None
        )

    async def execute(
        self,
        job: CronJobSpec,
        dispatch_meta: Mapping[str, Any] | None = None,
    ) -> ExecutionResult:
        if job.task_type != "workflow" or not job.workflow_binding_id:
            raise ValueError("workflow job requires workflow_binding_id")
        meta = dict(dispatch_meta or {})
        trace = self._trace_manager()
        trace_id = ""
        span_id = ""
        execution_key = _execution_key(job, meta)
        execution_meta = {
            "workflow": {
                "binding_id": job.workflow_binding_id,
            },
        }
        target = job.dispatch.target
        workspace_dir = (job.dispatch.meta or {}).get("workspace_dir")
        with bind_tenant_context(
            tenant_id=job.tenant_id,
            user_id=target.user_id,
            workspace_dir=(Path(workspace_dir) if workspace_dir else None),
            source_id=job.source_id,
            scope_id=job.scope_id,
        ):
            try:
                trace_id = await self._start_trace(trace, job)
                config = await self._load_config(job, meta)
                execution_meta["workflow"].update(
                    {
                        "version": config.version,
                        "skill_id": config.skill_id,
                        "provider_id": config.provider_id,
                        "model_id": config.model_id,
                    },
                )
                span_id = await self._start_skill_span(
                    trace,
                    trace_id,
                    config,
                    job,
                )
                env = self._env_loader(job)
                secrets = self._secret_loader(job, _secret_keys(config), env)
                if inspect.isawaitable(secrets):
                    secrets = await secrets
                result = await self._engine.execute(
                    config,
                    env=env,
                    secrets=secrets,
                    target_user_id=target.user_id,
                    runtime={
                        "job_id": job.id,
                        "cron_job_id": job.id,
                        "trace_id": trace_id,
                        "tenant_id": job.tenant_id,
                        "source_id": job.source_id,
                        "user_id": target.user_id,
                        "scheduled_fire_at": meta.get("scheduled_fire_at")
                        or meta.get("parent_scheduled_fire_at"),
                        "batch_id": meta.get("batch_id"),
                        "dispatch_attempt": meta.get("dispatch_attempt"),
                    },
                    timeout_seconds=job.runtime.timeout_seconds,
                )
                execution_meta["response_terminal_status"] = "completed"
                execution_meta["output_source"] = "workflow"
                execution_meta["session_state_commit_attempted"] = True
                await self._persist_task_message(
                    job,
                    result,
                    execution_key,
                )
                execution_meta["session_state_committed"] = True
                if trace_id:
                    await index_trace_output(trace_id, result.display_text)
                await self._send_result(job, result, meta, execution_key)
                await self._finish_trace(
                    trace,
                    trace_id,
                    span_id,
                    TraceStatus.COMPLETED,
                    result.display_text[:512],
                )
            except BaseException as exc:
                await self._record_failure(
                    exc,
                    execution_meta,
                    trace,
                    trace_id,
                    span_id,
                )
                raise

        return ExecutionResult(
            trace_id=trace_id,
            output_preview=result.display_text[:100],
            input_snapshot={
                **result.input_snapshot,
                "cron_execution_key": execution_key,
            },
            execution_meta={
                **execution_meta,
                "workflow_http_status": result.http_status,
            },
        )

    @staticmethod
    async def _start_trace(trace: Any, job: CronJobSpec) -> str:
        if trace is None or not getattr(trace, "enabled", False):
            return ""
        target = job.dispatch.target
        try:
            return await trace.start_trace(
                user_id=target.user_id,
                session_id=target.session_id,
                channel=job.dispatch.channel,
                source_id=job.source_id or "default",
                user_name=job.tenant_name,
                bbk_id=job.bbk_id,
                session_name=job.name,
            )
        except Exception:
            logger.warning(
                "workflow trace start failed: job_id=%s",
                job.id,
                exc_info=True,
            )
            return ""

    async def _load_config(
        self,
        job: CronJobSpec,
        meta: Mapping[str, Any],
    ) -> WorkflowConfig:
        version = meta.get("workflow_config_version")
        if meta.get("source") == "dispatch_service" and version is None:
            raise ValueError(
                "batch workflow requires frozen configuration version",
            )
        config = (
            await self._config_store.get_version(
                job.workflow_binding_id,
                int(version),
                source_id=job.source_id,
            )
            if version is not None
            else await self._config_store.get_current(
                job.workflow_binding_id,
                source_id=job.source_id,
            )
        )
        if config.skill_id != job.skill_ids.split(",", 1)[0]:
            raise ValueError("workflow binding no longer matches first skill")
        return config

    @staticmethod
    async def _start_skill_span(
        trace: Any,
        trace_id: str,
        config: WorkflowConfig,
        job: CronJobSpec,
    ) -> str:
        if not trace_id:
            return ""
        target = job.dispatch.target
        try:
            return await trace.emit_skill_invocation(
                trace_id=trace_id,
                skill_name=config.skill_id,
                skill_id=config.skill_id,
                source_id=job.source_id or "default",
                user_id=target.user_id,
                session_id=target.session_id,
                channel=job.dispatch.channel,
            )
        except Exception:
            logger.warning(
                "workflow skill trace failed: job_id=%s",
                job.id,
                exc_info=True,
            )
            return ""

    async def _record_failure(
        self,
        exc: BaseException,
        execution_meta: dict[str, Any],
        trace: Any,
        trace_id: str,
        span_id: str,
    ) -> None:
        if isinstance(exc, WorkflowCallError):
            execution_meta["workflow"]["error_code"] = exc.error_code
            if exc.status_code is not None:
                execution_meta["workflow"]["http_status"] = exc.status_code
        if trace_id:
            status = (
                TraceStatus.CANCELLED
                if isinstance(exc, asyncio.CancelledError)
                else TraceStatus.ERROR
            )
            await self._finish_trace(
                trace,
                trace_id,
                span_id,
                status,
                str(exc)[:512],
            )
            setattr(exc, "cron_trace_id", trace_id)
        setattr(exc, "cron_execution_meta", execution_meta)

    @staticmethod
    async def _finish_trace(
        trace: Any,
        trace_id: str,
        span_id: str,
        status: TraceStatus,
        detail: str,
    ) -> None:
        if not trace_id or trace is None:
            return
        try:
            if span_id:
                await trace.end_skill_invocation(
                    trace_id=trace_id,
                    span_id=span_id,
                    **(
                        {"skill_output": detail}
                        if status == TraceStatus.COMPLETED
                        else {"error": detail}
                    ),
                )
            await trace.end_trace(
                trace_id,
                status,
                None if status == TraceStatus.COMPLETED else detail,
            )
        except asyncio.CancelledError:
            # The business result was already committed and delivered.
            # Optional trace cleanup must not reclassify that run.
            logger.warning(
                "workflow trace finish cancelled: trace_id=%s",
                trace_id,
            )
        except Exception:
            logger.warning(
                "workflow trace finish failed: trace_id=%s",
                trace_id,
                exc_info=True,
            )

    async def _persist_task_message(
        self,
        job: CronJobSpec,
        result: WorkflowOutcome,
        execution_key: str,
    ) -> None:
        if self._session is None:
            raise RuntimeError("workflow task session persistence unavailable")
        session_id = str((job.meta or {}).get("task_session_id") or "")
        user_id = str((job.meta or {}).get("creator_user_id") or "")
        if not session_id or not user_id:
            raise RuntimeError("workflow task session binding missing")
        message_id = f"cron-workflow-{execution_key}"
        message = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": result.display_text}],
            "metadata": {"cron_task": True},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        def merge(state: dict[str, Any]) -> dict[str, Any]:
            updated = dict(state)
            messages = list(updated.get("task_messages") or [])
            if not any(
                isinstance(item, dict) and item.get("id") == message_id
                for item in messages
            ):
                messages.append(message)
            updated["task_messages"] = messages
            return updated

        await self._session.mutate_session_state(
            session_id=session_id,
            mutator=merge,
            user_id=user_id,
            create_if_not_exist=True,
        )

    async def _send_result(
        self,
        job: CronJobSpec,
        result: WorkflowOutcome,
        dispatch_meta: Mapping[str, Any],
        execution_key: str,
    ) -> None:
        if job.dispatch.channel == "console":
            return
        delivered = await self._channel_manager.send_text(
            channel=job.dispatch.channel,
            user_id=job.dispatch.target.user_id,
            session_id=job.dispatch.target.session_id,
            text=result.display_text,
            meta={
                **dispatch_meta,
                "cron_delivery_key": f"cron:{execution_key}:output",
            },
        )
        if delivered is not True:
            raise RuntimeError("workflow result delivery not confirmed")
