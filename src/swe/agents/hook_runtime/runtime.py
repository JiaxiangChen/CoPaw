# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .conversation_snapshot import build_handler_conversation_snapshot
from .executor import execute_handler
from .merge import merge_hook_results
from .models import (
    EffectiveHookPlan,
    HookDecision,
    HookHandlerResult,
    HookConfig,
    HookContext,
    HookSessionOverlay,
    HookEventName,
    MergedHookResult,
    StopHookExecutionResult,
    copy_handler_with_overrides,
    skill_hook_handler_definition,
)
from .resolver import HookResolver, once_key
from .skill_loader import refresh_skill_hooks_for_session
from swe.tracing.sanitizer import sanitize_string

logger = logging.getLogger(__name__)

_TELEMETRY_PREFIX = "HOOK_TELEMETRY "
_TELEMETRY_SCHEMA = "hook_telemetry.v1"
_PREVIEW_MAX_LENGTH = 500


class HookRuntime:
    """Event-boundary resolver and concurrent handler executor."""

    def __init__(
        self,
        *,
        tenant_config: HookConfig | None = None,
        agent_config: HookConfig | None = None,
        session_overlay: HookSessionOverlay | None = None,
    ) -> None:
        self.tenant_config = tenant_config or HookConfig()
        self.agent_config = agent_config or HookConfig()
        self.session_overlay = session_overlay or HookSessionOverlay()

    def requires_stop_output_buffer(
        self,
        context: HookContext,
        *,
        workspace_dir: Path | None = None,
    ) -> bool:
        if workspace_dir is not None:
            self._refresh_skill_hooks_sync(workspace_dir)
        return self._event_resolver().requires_stop_output_buffer(context)

    async def emit(
        self,
        context: HookContext,
        *,
        workspace_dir: Path,
        conversation_snapshot_provider: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> MergedHookResult:
        started_at = time.perf_counter()
        await self._refresh_skill_hooks(workspace_dir)
        resolver = self._event_resolver()
        plan = (
            resolver.resolve_stop_validator_plan(context)
            if context.hook_event_name == HookEventName.STOP
            else resolver.resolve_event_plan(context)
        )
        if context.hook_event_name == HookEventName.STOP:
            logger.warning(
                "[STOP-DEBUG] resolved trace_id=%s turn_id=%s handlers=%d "
                "handler_ids=%s tenant_enabled=%s agent_enabled=%s "
                "overlay_ids=%s",
                context.trace_id,
                context.turn_id,
                len(plan.handlers),
                [item.handler.id for item in plan.handlers],
                self.tenant_config.enabled,
                self.agent_config.enabled,
                [entry.hook_id for entry in self.session_overlay.entries],
            )
        if not plan.handlers:
            return merge_hook_results(plan, [])
        conversation_snapshot = await self._capture_conversation_snapshot(
            plan,
            conversation_snapshot_provider,
        )

        async def _run(item):
            handler_started_at = time.perf_counter()
            handler_context = self._context_for_handler(
                context,
                item.handler,
                conversation_snapshot,
            )
            result = await execute_handler(
                item.handler,
                handler_context,
                workspace_dir=workspace_dir,
            )
            result.order = item.order
            return result, _duration_ms(handler_started_at)

        executed = await asyncio.gather(
            *(_run(item) for item in plan.handlers),
        )
        results = [item[0] for item in executed]
        handler_durations = {
            result.order: duration_ms for result, duration_ms in executed
        }
        self._mark_once_executed(context, plan.handlers)
        merged = merge_hook_results(plan, results)
        try:
            _log_hook_telemetry(
                plan,
                results,
                handler_durations,
                merged,
                duration_ms=_duration_ms(started_at),
            )
        except Exception as exc:
            logger.warning("Failed to emit hook telemetry: %s", exc)
        return merged

    async def emit_stop_finalization(
        self,
        context: HookContext,
        *,
        workspace_dir: Path,
        max_transform_seconds: float,
        conversation_snapshot_provider: (
            Callable[[], Awaitable[dict[str, Any] | None]] | None
        ) = None,
    ) -> StopHookExecutionResult:
        if context.hook_event_name != HookEventName.STOP:
            raise ValueError("Stop finalization requires a Stop context")
        if not context.assistant_response:
            return StopHookExecutionResult(final_response="")

        await self._refresh_skill_hooks(workspace_dir)
        resolver = self._event_resolver()
        transformer_plan = resolver.resolve_stop_transformer_plan(
            context,
            evaluate_if=False,
        )
        snapshot_plan = EffectiveHookPlan(
            event_name=context.hook_event_name,
            context=context,
            handlers=(
                *transformer_plan.handlers,
                *resolver.resolve_stop_validator_plan(
                    context,
                    evaluate_if=False,
                ).handlers,
            ),
        )
        conversation_snapshot = await self._capture_conversation_snapshot(
            snapshot_plan,
            conversation_snapshot_provider,
        )
        current_text = context.assistant_response
        deadline = time.monotonic() + max_transform_seconds

        for item in transformer_plan.handlers:
            handler_context = context.model_copy(
                update={"assistant_response": current_text},
            )
            if not resolver._matches_if(
                item.handler.if_condition,
                handler_context,
            ):
                continue
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                return StopHookExecutionResult(
                    final_response=current_text,
                    transformation_failed=True,
                    transformation_failure_reason=(
                        "Stop output transformation time budget exhausted"
                    ),
                )
            effective_handler = copy_handler_with_overrides(
                item.handler,
                {"timeout": min(item.handler.timeout, remaining_seconds)},
            )
            handler_context = self._context_for_handler(
                handler_context,
                effective_handler,
                conversation_snapshot,
            )
            started_at = time.perf_counter()
            result = await execute_handler(
                effective_handler,
                handler_context,
                workspace_dir=workspace_dir,
            )
            result.order = item.order
            previous_text = current_text
            if result.replacement_text is not None:
                current_text = result.replacement_text
            _log_stop_transform_telemetry(
                item=item,
                result=result,
                input_text=previous_text,
                output_text=current_text,
                duration_ms=_duration_ms(started_at),
            )
            if time.monotonic() > deadline:
                return StopHookExecutionResult(
                    final_response=current_text,
                    transformation_failed=True,
                    transformation_failure_reason=(
                        "Stop output transformation time budget exhausted"
                    ),
                )
            if result.decision == HookDecision.BLOCK:
                return StopHookExecutionResult(
                    final_response=current_text,
                    transformation_failed=True,
                    transformation_failure_reason=(
                        result.reason or "Stop output transformation failed"
                    ),
                )

        final_context = context.model_copy(
            update={"assistant_response": current_text},
        )
        validator_plan = resolver.resolve_stop_validator_plan(final_context)
        if not validator_plan.handlers:
            return StopHookExecutionResult(final_response=current_text)

        async def _run_validator(item):
            handler_started_at = time.perf_counter()
            handler_context = self._context_for_handler(
                final_context,
                item.handler,
                conversation_snapshot,
            )
            result = await execute_handler(
                item.handler,
                handler_context,
                workspace_dir=workspace_dir,
            )
            result.order = item.order
            return result, _duration_ms(handler_started_at)

        executed = await asyncio.gather(
            *(_run_validator(item) for item in validator_plan.handlers),
        )
        results = [item[0] for item in executed]
        handler_durations = {
            result.order: duration_ms for result, duration_ms in executed
        }
        self._mark_once_executed(final_context, validator_plan.handlers)
        validation_result = merge_hook_results(validator_plan, results)
        try:
            _log_hook_telemetry(
                validator_plan,
                results,
                handler_durations,
                validation_result,
                duration_ms=0,
            )
        except Exception as exc:
            logger.warning("Failed to emit hook telemetry: %s", exc)
        return StopHookExecutionResult(
            final_response=current_text,
            validation_result=validation_result,
        )

    async def _capture_conversation_snapshot(
        self,
        plan: EffectiveHookPlan,
        provider: Callable[[], Awaitable[dict[str, Any] | None]] | None,
    ) -> dict[str, Any] | None:
        if not any(
            item.handler.include_conversation_snapshot
            for item in plan.handlers
        ):
            return None
        if provider is None:
            return None
        try:
            return await provider()
        except Exception as exc:
            logger.warning(
                "Failed to capture hook conversation snapshot: %s",
                exc,
            )
            return None

    @staticmethod
    def _context_for_handler(
        context: HookContext,
        handler,
        conversation_snapshot: dict[str, Any] | None,
    ) -> HookContext:
        if not handler.include_conversation_snapshot:
            return context
        snapshot_payload = build_handler_conversation_snapshot(
            conversation_snapshot,
            limit=handler.conversation_snapshot_limit,
        )
        return context.model_copy(update=snapshot_payload)

    def _mark_once_executed(self, context: HookContext, handlers) -> None:
        for item in handlers:
            if not item.handler.once:
                continue
            if not self._can_mark_skill_once(context, item):
                continue
            self.session_overlay.once_executed[
                once_key(
                    context.effective_tenant_id,
                    context.user_id,
                    context.session_id,
                    str(
                        getattr(
                            context.hook_event_name,
                            "value",
                            context.hook_event_name,
                        ),
                    ),
                    item.handler.id,
                )
            ] = True

    def _event_resolver(self) -> HookResolver:
        event_overlay = HookSessionOverlay.model_validate(
            self.session_overlay.model_dump(mode="json", by_alias=True),
        )
        return HookResolver(
            tenant_config=self.tenant_config,
            agent_config=self.agent_config,
            session_overlay=event_overlay,
        )

    async def _refresh_skill_hooks(self, workspace_dir: Path) -> None:
        await asyncio.to_thread(
            self._refresh_skill_hooks_sync,
            workspace_dir,
        )

    def _refresh_skill_hooks_sync(self, workspace_dir: Path) -> None:
        if not (
            self.session_overlay.has_monitored_skill_sources()
            or self.session_overlay.has_loaded_skill_sources()
        ):
            return
        refreshed_state = refresh_skill_hooks_for_session(
            workspace_dir=workspace_dir,
            session_state=self.session_overlay,
        )
        self.session_overlay.loaded_skill_sources = (
            refreshed_state.loaded_skill_sources
        )
        self.session_overlay.monitored_skill_sources = (
            refreshed_state.monitored_skill_sources
        )
        self.session_overlay.entries = refreshed_state.entries
        self.session_overlay.once_executed = refreshed_state.once_executed

    def _can_mark_skill_once(self, context: HookContext, item) -> bool:
        if item.skill_definition is None:
            return True
        event_name = context.hook_event_name
        for source in self.session_overlay.loaded_skill_sources:
            for group in source.hook_config.events.get(event_name, []):
                if any(
                    skill_hook_handler_definition(event_name, group, handler)
                    == item.skill_definition
                    for handler in group.hooks
                ):
                    return True
        return False


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _stop_transform_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _log_stop_transform_telemetry(
    *,
    item,
    result: HookHandlerResult,
    input_text: str,
    output_text: str,
    duration_ms: int,
) -> None:
    payload = {
        "schema": "hook_stop_transform.v1",
        "handler_id": item.handler.id,
        "source": item.source,
        "replacement_applied": result.replacement_text is not None,
        "input_length": len(input_text),
        "output_length": len(output_text),
        "input_sha256": _stop_transform_hash(input_text),
        "output_sha256": _stop_transform_hash(output_text),
        "duration_ms": duration_ms,
        "failed": result.failed,
        "failure_type": result.failure_type,
    }
    try:
        logger.info(
            "%s%s",
            _TELEMETRY_PREFIX,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    except Exception as exc:
        logger.warning("Failed to emit Stop transform telemetry: %s", exc)


def _preview(value: Any) -> str:
    if value is None:
        return ""
    return sanitize_string(str(value), _PREVIEW_MAX_LENGTH) or ""


def _event_name_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _handler_ids_with_specific_key(
    results: list[HookHandlerResult],
    key: str,
) -> list[str]:
    ids: list[str] = []
    for result in sorted(results, key=lambda item: item.order):
        specific = result.output.hook_specific_output or {}
        if specific.get(key) is not None:
            ids.append(result.handler_id)
    return ids


def _handler_ids_with_system_messages(
    results: list[HookHandlerResult],
) -> list[str]:
    return [
        result.handler_id
        for result in sorted(results, key=lambda item: item.order)
        if result.output.system_message
    ]


def _permission_decisions_payload(
    merged: MergedHookResult,
) -> list[dict[str, str]]:
    return [
        {
            "handler_id": item.handler_id,
            "decision": _event_name_value(item.decision),
            "reason_preview": _preview(item.reason),
        }
        for item in merged.permission_decisions
    ]


def _handlers_payload(
    plan: EffectiveHookPlan,
    results: list[HookHandlerResult],
    handler_durations: dict[int, int],
) -> list[dict[str, Any]]:
    result_by_order = {result.order: result for result in results}
    handlers: list[dict[str, Any]] = []
    for item in plan.handlers:
        result = result_by_order.get(item.order)
        handlers.append(
            {
                "handler_id": item.handler.id,
                "group_id": item.group_id,
                "type": item.handler.type,
                "order": item.order,
                "duration_ms": handler_durations.get(item.order, 0),
                "decision": (
                    _event_name_value(result.decision)
                    if result is not None
                    else _event_name_value(HookDecision.NONE)
                ),
                "failed": bool(result.failed) if result is not None else False,
                "failure_type": (
                    result.failure_type if result is not None else ""
                ),
                "reason_preview": (
                    _preview(result.reason) if result is not None else ""
                ),
            },
        )
    return handlers


def _log_hook_telemetry(
    plan: EffectiveHookPlan,
    results: list[HookHandlerResult],
    handler_durations: dict[int, int],
    merged: MergedHookResult,
    *,
    duration_ms: int,
) -> None:
    if not plan.handlers:
        return

    context_payload = plan.context.to_handler_payload()
    updated_input_handler_ids = _handler_ids_with_specific_key(
        results,
        "updatedInput",
    )
    additional_context_handler_ids = _handler_ids_with_specific_key(
        results,
        "additionalContext",
    )
    system_message_handler_ids = _handler_ids_with_system_messages(results)
    payload = {
        "schema": _TELEMETRY_SCHEMA,
        "hook_event_name": _event_name_value(plan.event_name),
        "execution_state": "executed",
        "trace_id": context_payload.get("trace_id"),
        "tenant_id": context_payload.get("tenant_id"),
        "effective_tenant_id": context_payload.get("effective_tenant_id"),
        "source_id": context_payload.get("source_id"),
        "user_id": context_payload.get("user_id"),
        "session_id": context_payload.get("session_id"),
        "chat_id": context_payload.get("chat_id"),
        "turn_id": context_payload.get("turn_id"),
        "agent_id": context_payload.get("agent_id"),
        "channel": context_payload.get("channel"),
        "tool_name": context_payload.get("tool_name"),
        "tool_use_id": context_payload.get("tool_use_id"),
        "handler_count": len(plan.handlers),
        "duration_ms": duration_ms,
        "decision": _event_name_value(merged.decision),
        "blocked": merged.blocked,
        "reason_preview": _preview(merged.reason),
        "has_updated_input": bool(updated_input_handler_ids),
        "updated_input_handler_ids": updated_input_handler_ids,
        "has_additional_context": bool(additional_context_handler_ids),
        "additional_context_handler_ids": additional_context_handler_ids,
        "has_system_messages": bool(system_message_handler_ids),
        "system_message_handler_ids": system_message_handler_ids,
        "permission_decisions": _permission_decisions_payload(merged),
        "handlers": _handlers_payload(plan, results, handler_durations),
    }
    logger.info(
        "%s%s",
        _TELEMETRY_PREFIX,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def log_stop_skipped_telemetry(
    context: HookContext,
    *,
    skipped_reason: str,
) -> None:
    """Emit a schema-compatible Stop gate skip without handler execution."""
    context_payload = context.to_handler_payload()
    payload = {
        "schema": _TELEMETRY_SCHEMA,
        "hook_event_name": _event_name_value(HookEventName.STOP),
        "execution_state": "skipped",
        "skipped_reason": skipped_reason,
        "trace_id": context_payload.get("trace_id"),
        "tenant_id": context_payload.get("tenant_id"),
        "effective_tenant_id": context_payload.get("effective_tenant_id"),
        "source_id": context_payload.get("source_id"),
        "user_id": context_payload.get("user_id"),
        "session_id": context_payload.get("session_id"),
        "chat_id": context_payload.get("chat_id"),
        "turn_id": context_payload.get("turn_id"),
        "agent_id": context_payload.get("agent_id"),
        "channel": context_payload.get("channel"),
        "tool_name": None,
        "tool_use_id": None,
        "handler_count": 0,
        "duration_ms": 0,
        "decision": _event_name_value(HookDecision.NONE),
        "blocked": False,
        "reason_preview": "",
        "candidate": {
            "assistant_response_length": len(
                context.assistant_response or "",
            ),
        },
        "has_updated_input": False,
        "updated_input_handler_ids": [],
        "has_additional_context": False,
        "additional_context_handler_ids": [],
        "has_system_messages": False,
        "system_message_handler_ids": [],
        "permission_decisions": [],
        "handlers": [],
    }
    logger.info(
        "%s%s",
        _TELEMETRY_PREFIX,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
