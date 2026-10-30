# -*- coding: utf-8 -*-
# flake8: noqa: E704
# pylint: disable=too-many-statements
"""Agent-turn, Stop-gate, and Goal-continuation lifecycle orchestration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Protocol

from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from .assistant_response import (
    project_candidate_assistant_response,
    replace_candidate_assistant_response,
)
from ...agents.tool_guard_mixin import PreToolUseTerminalStop
from ...agents.hook_runtime.models import HookDecision, MergedHookResult
from ..subagents.intent import has_subagent_intent
from ...runtime_workers import run_runtime_state_work

logger = logging.getLogger(__name__)


class TurnLifecycleOwner(Protocol):
    """Narrow runner surface used by the query turn lifecycle."""

    tenant_id: str | None
    agent_id: str

    def _stream_printing_messages(self, **kwargs: Any) -> Any: ...

    async def _enforce_query_timeout(
        self,
        stream: Any,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[Msg, bool], None]: ...

    def _resolve_max_stop_turns(self, agent_config: Any) -> int: ...

    def _resolve_max_automatic_follow_up_turns(
        self,
        agent_config: Any,
        stop_turns: int,
    ) -> int: ...

    def _requires_stop_output_buffer(self, **kwargs: Any) -> bool: ...

    def _replace_assistant_response(
        self,
        agent: Any,
        response: str,
        *,
        memory_start: int = 0,
    ) -> bool: ...

    def _extract_assistant_response(
        self,
        agent: Any,
        *,
        memory_start: int = 0,
    ) -> str: ...

    def _request_goal_id(self, request: AgentRequest) -> str | None: ...

    def _goal_matches_runtime_scope(self, goal: Any, runtime: Any) -> bool: ...

    def _build_goal_contract_context(self, goal: Any) -> str: ...

    def _append_goal_tool_observations(
        self,
        observations: list[dict[str, str]],
        msg: Msg,
    ) -> None: ...

    def _build_goal_follow_up_msg(
        self,
        next_focus: str | None,
        steering: list[str] | None = None,
        contract_context: str | None = None,
    ) -> Msg: ...

    async def _stream_agent_turns(
        self,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[Msg, bool], None]: ...

    async def _stream_goal_finalization_turn(
        self,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[Msg, bool], None]: ...

    def _create_goal_completion_reviewer(self, **kwargs: Any) -> Any: ...

    async def _emit_stop_hook_if_needed(
        self,
        **kwargs: Any,
    ) -> MergedHookResult | None: ...

    def _should_stop_follow_up(self, outcome: Any) -> bool: ...

    def _build_stop_follow_up_msg(self, reason: str) -> Msg: ...

    def _build_stop_incomplete_msg(self, reason: str) -> Msg: ...

    def _stream_stop_appended_messages(self, **kwargs: Any) -> Any: ...


async def stream_agent_turns(
    owner: TurnLifecycleOwner,
    *,
    runtime: Any,
    plan: Any,
    outcome: Any,
    plan_interaction_card_metadata_key: str,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    """Stream one agent turn while retaining its existing terminal semantics."""
    turn_msgs = plan.turn_msgs
    outcome.assistant_response = ""
    memory_start = len(getattr(runtime.agent.memory, "content", []))
    outcome.assistant_memory_start = memory_start
    outcome.buffered_assistant_messages.clear()
    stop_turns = owner._resolve_max_stop_turns(runtime.agent_config)
    outcome.max_stop_turns = stop_turns
    outcome.max_automatic_follow_up_turns = (
        owner._resolve_max_automatic_follow_up_turns(
            runtime.agent_config,
            stop_turns,
        )
    )
    reset_terminal_stop = getattr(
        runtime.agent,
        "reset_pre_tool_terminal_stop",
        None,
    )
    if callable(reset_terminal_stop):
        reset_terminal_stop()
    try:
        async for msg, last in owner._enforce_query_timeout(
            owner._stream_printing_messages(
                agents=[runtime.agent],
                coroutine_task=runtime.agent(turn_msgs),
            ),
            session_id=runtime.session_id,
            agent=runtime.agent,
            run_key=(runtime.chat.id if runtime.chat is not None else None),
        ):
            metadata = getattr(msg, "metadata", None)
            if isinstance(metadata, dict) and isinstance(
                metadata.get(plan_interaction_card_metadata_key),
                dict,
            ):
                outcome.plan_interaction_turn_boundary = True
            if (
                outcome.stop_output_buffer_required
                and _is_bufferable_assistant_text(
                    msg,
                )
            ):
                outcome.buffered_assistant_messages.append(msg)
                continue
            yield msg, last
    except PreToolUseTerminalStop as exc:
        consume_terminal_stop = getattr(
            runtime.agent,
            "consume_pre_tool_terminal_stop",
            None,
        )
        reason = (
            consume_terminal_stop()
            if callable(consume_terminal_stop)
            else None
        )
        reason = (reason or exc.reason or "Hook requested stop").strip()
        outcome.task_completed = False
        outcome.completion_blocked = True
        outcome.completion_block_reason = reason
        outcome.pre_tool_terminal_stop = True
        terminal_msg = Msg(name="Friday", role="assistant", content=reason)
        await runtime.agent.memory.add(terminal_msg)
        yield terminal_msg, True
        return
    outcome.assistant_response = owner._extract_assistant_response(
        runtime.agent,
        memory_start=memory_start,
    )
    outcome.task_completed = True


async def stream_completion_lifecycle(
    owner: TurnLifecycleOwner,
    *,
    request: AgentRequest,
    runtime: Any,
    plan: Any,
    outcome: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    """Continue turns through Goal settlement or the Stop completion gate."""
    goal_id = owner._request_goal_id(request)
    steering_response = await _resolve_implicit_goal_steering(
        owner,
        goal_id=goal_id,
        runtime=runtime,
        plan=plan,
    )
    if steering_response is not None:
        yield steering_response, True
        return
    if goal_id:
        async for event in _stream_goal_completion_lifecycle(
            owner,
            request=request,
            goal_id=goal_id,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        ):
            yield event
        return
    async for event in _stream_standard_completion_lifecycle(
        owner,
        request=request,
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    ):
        yield event


async def _resolve_implicit_goal_steering(
    owner: TurnLifecycleOwner,
    *,
    goal_id: str | None,
    runtime: Any,
    plan: Any,
) -> Msg | None:
    if goal_id:
        return None
    from ..goals.registry import get_goal_service

    service = get_goal_service()
    chat_id = str(getattr(getattr(runtime, "chat", None), "id", ""))
    active_goal = (
        await service.recent_for_chat(chat_id)
        if service is not None and chat_id
        else None
    )
    if active_goal is None or active_goal.state.value not in {
        "ACTIVE",
        "WAITING",
    }:
        return None
    if not owner._goal_matches_runtime_scope(active_goal, runtime):
        return Msg(
            name="Friday",
            role="assistant",
            content="The active Goal is not available in this chat.",
        )
    steering_message = plan.original_user_message.strip()
    if steering_message:
        context = getattr(runtime.agent, "_request_context", {})
        await service.enqueue_steering(
            active_goal.goal_id,
            steering_message,
            enable_subagents=has_subagent_intent(
                steering_message,
                context.get("enable_subagents", False),
            ),
        )
    return Msg(
        name="Friday",
        role="assistant",
        content=(
            "Your message was added as Goal steering. "
            "The active Goal will continue under its confirmed Contract."
        ),
    )


async def _begin_goal_turn(
    owner: TurnLifecycleOwner,
    *,
    goal_id: str,
    runtime: Any,
) -> tuple[Any, Any] | Msg | None:
    from ..goals.registry import get_goal_service

    service = get_goal_service()
    if service is None:
        return None
    current_goal = None
    try:
        current_goal = await service.get(goal_id)
        if not owner._goal_matches_runtime_scope(current_goal, runtime):
            return Msg(
                name="Friday",
                role="assistant",
                content="The requested Goal is not available in this chat.",
            )
        context = getattr(runtime.agent, "_request_context", {})
        msgid = str(context.get("msgid") or "").strip()
        return service, await service.begin_turn(
            goal_id,
            msgid=msgid or None,
            enable_subagents=has_subagent_intent(
                context.get("current_user_text"),
                context.get("enable_subagents", False),
            ),
        )
    except ValueError:
        state = getattr(getattr(current_goal, "state", None), "value", None)
        return Msg(
            name="Friday",
            role="assistant",
            content=(
                f"Goal is currently {state or 'unavailable'} and cannot start a new turn. "
                "Use the Goal Monitor to resume, edit, or cancel it."
            ),
        )


async def _prepare_goal_turn_context(
    owner: TurnLifecycleOwner,
    *,
    runtime: Any,
    goal: Any,
) -> None:
    request_context = getattr(runtime.agent, "_request_context", {})
    request_context["enable_subagents"] = bool(
        getattr(goal, "subagent_delegation_enabled", False),
    )
    request_context["goal_contract_context"] = (
        owner._build_goal_contract_context(
            goal,
        )
    )
    request_context.pop("goal_turn_resolution", None)
    request_context.pop("_goal_turn_environment_changed", None)
    request_context["_goal_turn_tool_observations"] = []
    refresh = getattr(runtime.agent, "refresh_background_subagent_tools", None)
    if refresh is not None:
        await _refresh_goal_subagent_tools(owner, goal, refresh)


async def _refresh_goal_subagent_tools(
    owner: TurnLifecycleOwner,
    goal: Any,
    refresh: Any,
) -> None:
    """Refresh only delegation tools with this turn's Source configuration."""
    from ..source_system_config.runtime import bind_source_system_config

    workspace = getattr(owner, "_workspace", None)
    resolver = getattr(workspace, "_source_system_config_service", None)
    if resolver is None:
        await run_runtime_state_work(refresh)
        return
    config = await resolver.resolve_config(
        goal.scope.source_id,
        force_refresh=True,
    )
    with bind_source_system_config(config):
        await run_runtime_state_work(refresh)


async def _settle_goal_turn(
    owner: TurnLifecycleOwner,
    *,
    goal_id: str,
    runtime: Any,
) -> tuple[Any, Any, Any] | None:
    from ..goals.registry import get_goal_service
    from ..goals.runtime import GoalRuntime, GoalTurnResolution

    service = get_goal_service()
    raw = getattr(runtime.agent, "_request_context", {}).get(
        "goal_turn_resolution",
    )
    if service is None or not isinstance(raw, dict):
        if service is not None:
            await service.abandon_turn(
                goal_id,
                "Main Agent did not submit a Goal turn resolution",
            )
        return None
    try:
        had_pending_steering = await service.has_pending_steering(goal_id)
        resolution = GoalTurnResolution.model_validate(raw)
        reviewer = owner._create_goal_completion_reviewer(
            runtime=runtime,
            resolution=resolution,
        )
        settled = await GoalRuntime(service, reviewer=reviewer).settle(
            goal_id,
            resolution,
            wake_from_steering=had_pending_steering,
            environment_changed=bool(
                getattr(runtime.agent, "_request_context", {}).get(
                    "_goal_turn_environment_changed",
                ),
            ),
        )
    except (TypeError, ValueError):
        await service.abandon_turn(goal_id, "Goal turn settlement failed")
        return None
    return service, reviewer, settled


async def _schedule_goal_follow_up(
    owner: TurnLifecycleOwner,
    *,
    service: Any,
    goal_id: str,
    plan: Any,
    goal: Any,
) -> None:
    _, steering = await service.consume_steering(goal_id)
    plan.turn_msgs = [
        owner._build_goal_follow_up_msg(
            goal.next_focus,
            steering,
            owner._build_goal_contract_context(goal),
        ),
    ]


async def _wait_for_goal_wake(
    owner: TurnLifecycleOwner,
    *,
    service: Any,
    reviewer: Any,
    goal_id: str,
    plan: Any,
    settled: Any,
) -> Any:
    from ..goals.runtime import GoalRuntime
    from ..goals.wakeup import wait_for_goal_wake

    while settled.state.value == "WAITING":
        await wait_for_goal_wake(goal_id)
        woken = await service.get(goal_id)
        if woken.state.value != "ACTIVE":
            return woken
        retried = await GoalRuntime(
            service,
            reviewer=reviewer,
        ).retry_pending_completion_review(goal_id)
        if retried.state.value == "COMPLETE":
            return retried
        settled = retried
        if settled.state.value != "ACTIVE":
            continue
        await _schedule_goal_follow_up(
            owner,
            service=service,
            goal_id=goal_id,
            plan=plan,
            goal=settled,
        )
        return settled
    return settled


async def _stream_one_goal_turn(
    owner: TurnLifecycleOwner,
    *,
    goal_id: str,
    service: Any,
    runtime: Any,
    plan: Any,
    outcome: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    """Stream one Goal turn and preserve cancellation cleanup."""
    try:
        async for msg, _last in owner._stream_agent_turns(
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        ):
            observations = getattr(
                runtime.agent,
                "_request_context",
                {},
            ).get("_goal_turn_tool_observations")
            if isinstance(observations, list):
                owner._append_goal_tool_observations(observations, msg)
            yield msg, False
    except asyncio.CancelledError:
        await service.abandon_turn(
            goal_id,
            "Goal turn stopped before settlement",
        )
        raise


async def _wait_for_goal_wake_after_cancel(
    owner: TurnLifecycleOwner,
    *,
    service: Any,
    runtime: Any,
    reviewer: Any,
    goal_id: str,
    plan: Any,
    settled: Any,
) -> Any:
    """Wait for a Goal wake and interrupt the matching turn on cancellation."""
    try:
        return await _wait_for_goal_wake(
            owner,
            service=service,
            reviewer=reviewer,
            goal_id=goal_id,
            plan=plan,
            settled=settled,
        )
    except asyncio.CancelledError:
        msgid = str(
            getattr(runtime.agent, "_request_context", {}).get("msgid") or "",
        ).strip()
        interrupt = getattr(service, "interrupt_turn_if_matches", None)
        if callable(interrupt) and msgid:
            await interrupt(
                goal_id,
                msgid,
                "Goal turn stopped while waiting for wake",
            )
        else:
            await service.abandon_turn(
                goal_id,
                "Goal turn stopped while waiting for wake",
            )
        raise


async def _stream_goal_finalization(
    owner: TurnLifecycleOwner,
    *,
    request: AgentRequest,
    runtime: Any,
    plan: Any,
    outcome: Any,
    settled: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    """Stream finalization turns, including Stop retries and buffering."""
    outcome.stop_follow_up_turns = 0
    outcome.max_stop_turns = owner._resolve_max_stop_turns(
        getattr(runtime, "agent_config", None),
    )
    stop_rejection_reason: str | None = None
    while True:
        outcome.goal_finalization_fallback = False
        outcome.assistant_memory_start = len(
            getattr(getattr(runtime.agent, "memory", None), "content", []),
        )
        outcome.stop_output_buffer_required = (
            owner._requires_stop_output_buffer(
                request=request,
                runtime=runtime,
                plan=plan,
            )
        )
        finalization_msg: Msg | None = None
        async for msg, last in owner._stream_goal_finalization_turn(
            runtime=runtime,
            goal=settled,
            stop_rejection_reason=stop_rejection_reason,
        ):
            if last:
                finalization_msg = msg
            elif not outcome.stop_output_buffer_required:
                yield msg, False
        if finalization_msg is None:
            return
        metadata = getattr(finalization_msg, "metadata", None)
        outcome.goal_finalization_fallback = bool(
            isinstance(metadata, dict)
            and metadata.get("goal_finalization_fallback"),
        )
        outcome.assistant_response = (
            project_candidate_assistant_response(finalization_msg) or ""
        )
        retry_finalization, incomplete_msg, retry_reason = (
            await _resolve_goal_finalization_stop_gate(
                owner,
                request=request,
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            )
        )
        if retry_finalization:
            stop_rejection_reason = retry_reason
            continue
        if incomplete_msg is not None:
            yield incomplete_msg, True
        else:
            _replace_buffered_assistant_text(
                finalization_msg,
                outcome.assistant_response,
            )
            yield finalization_msg, True
            async for item in _stream_approved_appends(
                owner,
                request=request,
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            ):
                yield item
        return


async def _stream_goal_completion_lifecycle(
    owner: TurnLifecycleOwner,
    *,
    request: AgentRequest,
    goal_id: str,
    runtime: Any,
    plan: Any,
    outcome: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    while True:
        started = await _begin_goal_turn(
            owner,
            goal_id=goal_id,
            runtime=runtime,
        )
        if started is None:
            return
        if isinstance(started, Msg):
            yield started, True
            return
        service, goal = started
        try:
            await _prepare_goal_turn_context(owner, runtime=runtime, goal=goal)
        except (Exception, asyncio.CancelledError):
            await service.abandon_turn(
                goal_id,
                "Goal turn context preparation failed",
            )
            raise
        outcome.stop_hook_active = False
        async for msg, last in _stream_one_goal_turn(
            owner,
            goal_id=goal_id,
            service=service,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        ):
            yield msg, last
        if outcome.pre_tool_terminal_stop:
            await service.abandon_turn(
                goal_id,
                "Goal turn stopped before settlement",
            )
            return
        settlement = await _settle_goal_turn(
            owner,
            goal_id=goal_id,
            runtime=runtime,
        )
        if settlement is None:
            return
        service, reviewer, settled = settlement
        if settled.state.value == "ACTIVE":
            await _schedule_goal_follow_up(
                owner,
                service=service,
                goal_id=goal_id,
                plan=plan,
                goal=settled,
            )
            continue
        if settled.state.value == "WAITING":
            settled = await _wait_for_goal_wake_after_cancel(
                owner,
                service=service,
                runtime=runtime,
                reviewer=reviewer,
                goal_id=goal_id,
                plan=plan,
                settled=settled,
            )
            if settled.state.value == "ACTIVE":
                continue
        if settled.state.value == "INTERRUPTED":
            return
        async for msg, last in _stream_goal_finalization(
            owner,
            request=request,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
            settled=settled,
        ):
            yield msg, last
        return


async def _resolve_goal_finalization_stop_gate(
    owner: TurnLifecycleOwner,
    *,
    request: AgentRequest,
    runtime: Any,
    plan: Any,
    outcome: Any,
) -> tuple[bool, Msg | None, str | None]:
    """Apply Stop to formal Goal delivery without reopening Goal execution."""
    outcome.stop_append_approved = False
    stop_result = await owner._emit_stop_hook_if_needed(
        request=request,
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )
    if stop_result is None or stop_result.decision != HookDecision.BLOCK:
        outcome.stop_append_approved = bool(outcome.stop_hook_active)
        outcome.stop_hook_active = False
        return False, None, None
    reason = (
        stop_result.blocking_failure_reason
        if stop_result.has_blocking_failure
        else stop_result.reason
    ) or "Stop blocked completion"
    if (
        not stop_result.has_blocking_failure
        and outcome.stop_follow_up_turns < outcome.max_stop_turns
    ):
        outcome.stop_follow_up_turns += 1
        outcome.stop_hook_active = False
        return True, None, reason
    outcome.task_completed = False
    outcome.completion_blocked = True
    outcome.completion_block_reason = reason
    outcome.completion_marked_incomplete = True
    outcome.stop_hook_active = False
    incomplete_msg = owner._build_stop_incomplete_msg(reason)
    await runtime.agent.memory.add(incomplete_msg)
    return False, incomplete_msg, None


async def _resolve_stop_gate(
    owner: TurnLifecycleOwner,
    *,
    request: AgentRequest,
    runtime: Any,
    plan: Any,
    outcome: Any,
) -> tuple[bool, Msg | None]:
    outcome.stop_append_approved = False
    stop_result = await owner._emit_stop_hook_if_needed(
        request=request,
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )
    if stop_result is None or stop_result.decision != HookDecision.BLOCK:
        outcome.stop_append_approved = bool(outcome.stop_hook_active)
        outcome.stop_hook_active = False
        return False, None
    reason = (
        stop_result.blocking_failure_reason
        if stop_result.has_blocking_failure
        else stop_result.reason
    ) or "Stop blocked completion"
    if not stop_result.has_blocking_failure and owner._should_stop_follow_up(
        outcome,
    ):
        outcome.stop_follow_up_turns += 1
        outcome.automatic_follow_up_turns += 1
        plan.turn_msgs = [owner._build_stop_follow_up_msg(reason)]
        outcome.stop_hook_active = False
        return True, None
    outcome.task_completed = False
    outcome.completion_blocked = True
    outcome.completion_block_reason = reason
    outcome.completion_marked_incomplete = True
    outcome.stop_hook_active = False
    incomplete_msg = owner._build_stop_incomplete_msg(reason)
    await runtime.agent.memory.add(incomplete_msg)
    return False, incomplete_msg


async def _stream_standard_completion_lifecycle(
    owner: TurnLifecycleOwner,
    *,
    request: AgentRequest,
    runtime: Any,
    plan: Any,
    outcome: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    while True:
        outcome.stop_hook_active = False
        outcome.stop_output_buffer_required = (
            owner._requires_stop_output_buffer(
                request=request,
                runtime=runtime,
                plan=plan,
            )
        )
        async for msg, last in owner._stream_agent_turns(
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        ):
            yield msg, last
        if outcome.pre_tool_terminal_stop:
            logger.warning(
                "[STOP-DEBUG] skipped reason=pre_tool_terminal_stop "
                "trace_id=%s turn_id=%s",
                getattr(request, "trace_id", None),
                (getattr(request, "channel_meta", None) or {}).get("turn_id"),
            )
            return
        logger.warning(
            "[STOP-DEBUG] before_stop_gate trace_id=%s turn_id=%s "
            "response_len=%d pre_tool_terminal_stop=%s "
            "plan_interaction_turn_boundary=%s",
            getattr(request, "trace_id", None),
            (getattr(request, "channel_meta", None) or {}).get("turn_id"),
            len(outcome.assistant_response or ""),
            outcome.pre_tool_terminal_stop,
            outcome.plan_interaction_turn_boundary,
        )
        should_continue, incomplete_msg = await _resolve_stop_gate(
            owner,
            request=request,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        )
        if should_continue:
            continue
        if incomplete_msg is not None:
            yield incomplete_msg, True
        elif outcome.stop_output_buffer_required:
            buffered_messages = outcome.buffered_assistant_messages
            if buffered_messages:
                final_message = buffered_messages[-1]
                _replace_buffered_assistant_text(
                    final_message,
                    outcome.assistant_response,
                )
                yield final_message, True
        if incomplete_msg is None:
            async for item in _stream_approved_appends(
                owner,
                request=request,
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            ):
                yield item
        return


async def _stream_approved_appends(
    owner: TurnLifecycleOwner,
    *,
    request: AgentRequest,
    runtime: Any,
    plan: Any,
    outcome: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    append = getattr(owner, "_stream_stop_appended_messages", None)
    if getattr(outcome, "stop_append_approved", False) and callable(append):
        async for item in append(
            request=request,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        ):
            yield item


def _is_bufferable_assistant_text(msg: Msg) -> bool:
    return project_candidate_assistant_response(msg) is not None


def _replace_buffered_assistant_text(msg: Msg, response: str) -> None:
    replace_candidate_assistant_response(msg, response)
