# -*- coding: utf-8 -*-
# flake8: noqa: E704
# pylint: disable=too-many-statements
"""Retry and per-attempt orchestration for runner query execution."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Protocol, cast

from agentscope.message import Msg, TextBlock
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from ...config.context import (
    reset_current_file_url_network,
    set_current_file_url_network,
)
from ...runtime_invocation_claims import runtime_invocation_claims_context
from ...tracing.models import TraceStatus
from ...tracing.agent_trace_sdk import OperationName, start_child_span
from ..agent_context import set_current_agent_id
from .query_contracts import (
    QueryPersistenceResult,
    _QueryPreflight,
    _QueryRuntime,
)
from .retry_classifier import is_query_retryable
from .stop_append import StopAppendPersistenceError
from ...agents.hook_runtime.append import log_append_diagnostic

logger = logging.getLogger(__name__)


class QueryAttemptOwner(Protocol):
    """Runner operations needed to execute an attempt without importing it."""

    agent_id: str
    tenant_id: str | None
    session: Any

    async def _start_query_trace(
        self,
        request: AgentRequest,
        msgs: list[Any],
    ) -> str | None: ...

    def _load_query_retry_settings(
        self,
        agent_config: Any | None = None,
    ) -> tuple[int, int, float, float]: ...

    def _new_query_turn_outcome(self) -> Any: ...

    def _new_retry_state(self) -> Any: ...

    def _new_query_attempt_input(self, **kwargs: Any) -> Any: ...

    def _new_query_attempt_state(self) -> Any: ...

    def _request_file_url_network(self, request: AgentRequest) -> Any: ...

    def _build_skill_freshness_notice_msg(self, text: str) -> Msg: ...

    async def _stream_retry_backoff_notice(
        self,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[Msg, bool], None]: ...

    async def _stream_single_query_attempt(
        self,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[Msg, bool], None]: ...

    def _should_retry(
        self,
        retry_attempt: int,
        max_retry_attempts: int,
        exc: BaseException,
    ) -> bool: ...

    async def _raise_console_model_call_failed_if_needed(
        self,
        **kwargs: Any,
    ) -> None: ...

    async def _handle_query_error(self, **kwargs: Any) -> None: ...

    async def _stream_retryable_query_error(
        self,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[Msg, bool], None]: ...

    async def _handle_query_cancelled(self, **kwargs: Any) -> None: ...

    async def _cleanup_query_resources(self, **kwargs: Any) -> None: ...

    async def _save_state_during_cleanup(self, **kwargs: Any) -> bool: ...

    def _record_query_persistence_result(
        self,
        request: AgentRequest,
        result: QueryPersistenceResult,
    ) -> None: ...

    async def _cleanup_blocked_runtime_start(
        self,
        runtime_start: Any,
    ) -> None: ...

    async def _settle_stopped_turn(
        self,
        **kwargs: Any,
    ) -> None: ...

    async def _store_qa_content_if_needed(self, **kwargs: Any) -> None: ...

    async def _prepare_query_runtime(self, **kwargs: Any) -> Any: ...

    async def _end_trace_if_needed(
        self,
        trace_id: str | None,
        status: TraceStatus,
        error: str | None = None,
    ) -> None: ...

    def _rebind_trace_skill_detector_if_needed(
        self,
        **kwargs: Any,
    ) -> None: ...

    async def get_state_loaded(
        self,
        agent: Any,
        session_id: str | None,
        session_state_loaded: bool,
        skip_history: bool | Any,
        user_id: str | None,
        *,
        session_execution: Any = None,
        retry_state_snapshot: dict[str, Any] | None = None,
    ) -> bool: ...

    async def _refresh_session_skill_freshness(self, **kwargs: Any) -> Any: ...

    async def _build_turn_plan(self, **kwargs: Any) -> Any: ...

    async def _stream_completion_lifecycle(
        self,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[Msg, bool], None]: ...

    def _schedule_session_title_task(self, **kwargs: Any) -> None: ...

    async def _build_skill_snapshot_to_persist(
        self,
        **kwargs: Any,
    ) -> dict[str, dict[str, Any]] | None: ...

    async def _finish_blocked_query_attempt(self, **kwargs: Any) -> None: ...

    async def _complete_successful_query_attempt(
        self,
        **kwargs: Any,
    ) -> None: ...


def extract_retry_config(agent_config: Any) -> tuple[bool, int, float, float]:
    """Extract retry settings with the runner's historic defaults."""
    query_retry_config = getattr(
        getattr(agent_config, "running", None),
        "query_retry",
        None,
    )
    if not query_retry_config:
        return False, 0, 2.0, 30.0
    return (
        getattr(query_retry_config, "enabled", False),
        getattr(query_retry_config, "max_retries", 0),
        getattr(query_retry_config, "backoff_base", 2.0),
        getattr(query_retry_config, "backoff_cap", 30.0),
    )


def compute_retry_backoff(
    retry_attempt: int,
    backoff_cap: float,
    backoff_base: float,
) -> float:
    return min(backoff_cap, backoff_base * (2 ** (retry_attempt - 1)))


def should_retry(
    retry_attempt: int,
    max_retry_attempts: int,
    exc: BaseException,
) -> bool:
    return retry_attempt < max_retry_attempts - 1 and is_query_retryable(exc)


def summarize_retry_error(exc: BaseException) -> str:
    for candidate in (
        exc,
        getattr(exc, "__cause__", None),
        getattr(exc, "__context__", None),
    ):
        if candidate is None:
            continue
        status_code = getattr(candidate, "status_code", None)
        if status_code is not None:
            return {
                429: "请求频率超限",
                432: "输入Token数已达上限",
                433: "服务过载",
                500: "服务内部错误",
                502: "网关错误",
                503: "服务暂不可用",
                504: "请求超时",
                529: "站点过载",
            }.get(status_code, f"服务错误({status_code})")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "请求超时"
    if isinstance(
        exc,
        (ConnectionError, ConnectionResetError, BrokenPipeError),
    ):
        return "网络连接异常"
    message = str(exc).lower()
    if "rate limiter" in message:
        return "请求频率超限"
    if "timed out" in message:
        return "请求超时"
    return "服务暂时不可用"


def build_retry_status_msg(text: str) -> Msg:
    return Msg(
        name="Friday",
        role="assistant",
        content=[TextBlock(type="text", text=text)],
        metadata={"retry_status": True},
    )


async def add_retry_notice_to_memory(agent: Any, retry_msg: Msg) -> None:
    if agent is None:
        return
    try:
        await agent.memory.add(retry_msg)
    except Exception:
        pass


def snapshot_state_before_retry(agent: Any) -> dict[str, Any] | None:
    """Capture retry state in memory without mutating the session file."""
    if agent is None or not hasattr(agent, "state_dict"):
        return None
    return copy.deepcopy(agent.state_dict())


async def stream_retry_backoff_notice(
    owner: QueryAttemptOwner,
    *,
    retry_attempt: int,
    max_retries: int,
    backoff_base: float,
    backoff_cap: float,
    session_id: str,
    retry_state: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    if retry_attempt <= 0:
        return
    backoff = compute_retry_backoff(retry_attempt, backoff_cap, backoff_base)
    logger.info(
        "Query retry attempt %d/%d, backoff=%.1fs (session=%s)",
        retry_attempt,
        max_retries,
        backoff,
        session_id,
    )
    retry_msg = build_retry_status_msg(
        f"正在重试 ({retry_attempt}/{max_retries})...",
    )
    yield retry_msg, False
    await add_retry_notice_to_memory(retry_state.prev_agent, retry_msg)
    await asyncio.sleep(backoff)


async def stream_retryable_query_error(
    owner: QueryAttemptOwner,
    *,
    exc: BaseException,
    retry_attempt: int,
    max_retry_attempts: int,
    max_retries: int,
    retry_state: Any,
    runtime: _QueryRuntime | None,
    session_id: str,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    error_summary = summarize_retry_error(exc)
    logger.warning(
        "Query failed with retryable error (attempt %d/%d): %s (summary: %s)",
        retry_attempt + 1,
        max_retry_attempts,
        exc,
        error_summary,
    )
    retry_msg = build_retry_status_msg(
        f"{error_summary}，正在重试 ({retry_attempt + 1}/{max_retries})...",
    )
    yield retry_msg, False
    await add_retry_notice_to_memory(retry_state.agent, retry_msg)
    retry_state.agent_state_snapshot = snapshot_state_before_retry(
        retry_state.agent,
    )


async def _rebuild_system_prompt(agent: Any) -> None:
    rebuild_prompt = getattr(agent, "rebuild_sys_prompt_async", None)
    if callable(rebuild_prompt):
        await rebuild_prompt()
    else:
        agent.rebuild_sys_prompt()


async def stream_single_query_attempt(
    owner: QueryAttemptOwner,
    *,
    attempt_input: Any,
    outcome: Any,
    retry_state: Any,
    attempt_state: Any,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    if attempt_input.workspace_skill_snapshot is not None:
        attempt_input.preflight.workspace_skill_snapshot = (
            attempt_input.workspace_skill_snapshot
        )
    runtime_args = {
        "request": attempt_input.request,
        "msgs": attempt_input.msgs,
        "query": attempt_input.query,
        "preflight": attempt_input.preflight,
    }
    if attempt_input.session_execution is not None:
        runtime_args["session_execution"] = attempt_input.session_execution
    attempt_state.runtime_start = await owner._prepare_query_runtime(
        **runtime_args,
    )
    if attempt_state.runtime_start.block_response is not None:
        await owner._end_trace_if_needed(
            attempt_input.trace_id,
            TraceStatus.COMPLETED,
        )
        yield attempt_state.runtime_start.block_response, True
        attempt_state.should_return = True
        return
    runtime = attempt_state.runtime_start.runtime
    attempt_state.runtime = runtime
    if runtime is not None:
        attempt_input.workspace_skill_snapshot = getattr(
            runtime.agent,
            "_workspace_skill_snapshot",
            None,
        )
    if runtime is None:
        attempt_state.should_return = True
        return
    runtime.session_execution = attempt_input.session_execution
    from .session_lifecycle import discard_admitted_user_anchor

    with runtime_invocation_claims_context(
        chat_id=runtime.chat.id if runtime.chat is not None else None,
    ):
        owner._rebind_trace_skill_detector_if_needed(
            runtime=runtime,
            trace_id=attempt_input.trace_id,
        )
        if attempt_input.trace_id and runtime.session_skill_detector is None:
            await runtime.agent.setup_skill_detector(attempt_input.trace_id)
        logger.debug("Agent Query msgs %s", attempt_input.msgs)
        attempt_state.session_state_loaded = await owner.get_state_loaded(
            runtime.agent,
            runtime.session_id,
            attempt_state.session_state_loaded,
            runtime.skip_history,
            runtime.user_id,
            session_execution=attempt_input.session_execution,
            retry_state_snapshot=getattr(
                retry_state,
                "agent_state_snapshot",
                None,
            ),
        )
        discard_admitted_user_anchor(
            runtime.agent,
            (getattr(runtime.agent, "_request_context", None) or {}).get(
                "msgid",
            ),
        )
        retry_state.agent = runtime.agent
        retry_state.session_state_loaded = attempt_state.session_state_loaded
        skill_freshness_refresh = await owner._refresh_session_skill_freshness(
            runtime=runtime,
        )
        await _rebuild_system_prompt(runtime.agent)
        plan = await owner._build_turn_plan(
            runtime=runtime,
            request=attempt_input.request,
            msgs=attempt_input.msgs,
            query=attempt_input.query,
        )
        if skill_freshness_refresh.notice_text:
            plan.turn_msgs.insert(
                0,
                owner._build_skill_freshness_notice_msg(
                    skill_freshness_refresh.notice_text,
                ),
            )
        async for msg, last in owner._stream_completion_lifecycle(
            request=attempt_input.request,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        ):
            if not attempt_state.session_title_task_started:
                owner._schedule_session_title_task(
                    request=attempt_input.request,
                    chat=runtime.chat,
                    msgs=attempt_input.msgs,
                    trace_id=attempt_input.trace_id,
                )
                attempt_state.session_title_task_started = True
            yield msg, last
        settle_stopped_turn = getattr(owner, "_settle_stopped_turn", None)
        if callable(settle_stopped_turn):
            await settle_stopped_turn(
                runtime=runtime,
                request=attempt_input.request,
                session_execution=attempt_input.session_execution,
            )
        skill_snapshot_to_persist = (
            await owner._build_skill_snapshot_to_persist(
                runtime=runtime,
                refresh_result=skill_freshness_refresh,
            )
        )
        if outcome.completion_blocked:
            await owner._finish_blocked_query_attempt(
                runtime=runtime,
                outcome=outcome,
                trace_id=attempt_input.trace_id,
                skill_snapshot_to_persist=skill_snapshot_to_persist,
            )
            attempt_state.should_return = True
            return
        await owner._complete_successful_query_attempt(
            runtime=runtime,
            plan=plan,
            outcome=outcome,
            trace_id=attempt_input.trace_id,
            skill_snapshot_to_persist=skill_snapshot_to_persist,
        )
    retry_state.task_completed = outcome.task_completed
    attempt_state.succeeded = True


def _query_retry_settings(
    owner: QueryAttemptOwner,
    preflight: _QueryPreflight,
) -> tuple[int, int, float, float]:
    if preflight.agent_config is None:
        return owner._load_query_retry_settings()
    return owner._load_query_retry_settings(preflight.agent_config)


async def _stream_retry_attempt(
    owner: QueryAttemptOwner,
    *,
    retry_attempt: int,
    max_retry_attempts: int,
    max_retries: int,
    retry_state: Any,
    attempt_state: Any,
    attempt_input: Any,
    outcome: Any,
    trace_id: str | None,
    request: AgentRequest,
    session_execution: Any,
    session_id: str,
    backoff_base: float,
    backoff_cap: float,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    async for item in owner._stream_retry_backoff_notice(
        retry_attempt=retry_attempt,
        max_retries=max_retries,
        backoff_base=backoff_base,
        backoff_cap=backoff_cap,
        session_id=session_id,
        retry_state=retry_state,
    ):
        yield item
    try:
        async with start_child_span(
            "agent.attempt",
            operation_name=OperationName.INVOKE_AGENT,
        ) as span:
            span.set_attribute("retry.index", retry_attempt)
            async for item in owner._stream_single_query_attempt(
                attempt_input=attempt_input,
                outcome=outcome,
                retry_state=retry_state,
                attempt_state=attempt_state,
            ):
                yield item
    except asyncio.CancelledError as exc:
        settle_stopped_turn = getattr(owner, "_settle_stopped_turn", None)
        if callable(settle_stopped_turn):
            await settle_stopped_turn(
                runtime=attempt_state.runtime,
                request=request,
                session_execution=session_execution,
            )
        await owner._handle_query_cancelled(
            trace_id=trace_id,
            session_id=session_id,
            agent=(
                attempt_state.runtime.agent
                if attempt_state.runtime is not None
                else None
            ),
            exc=exc,
        )
        attempt_state.should_return = True
        return
    except Exception as exc:
        if getattr(outcome, "stop_append_started", False):
            log_append_diagnostic(
                "Stop append finalization failed category=%s trace_id=%s",
                type(exc).__name__,
                trace_id,
            )
            try:
                await owner._end_trace_if_needed(
                    trace_id,
                    TraceStatus.ERROR,
                    error="Stop append finalization failed",
                )
            except Exception:
                log_append_diagnostic(
                    "Stop append error trace closure failed trace_id=%s",
                    trace_id,
                )
            if isinstance(exc, StopAppendPersistenceError):
                raise
            raise RuntimeError("Stop append finalization failed") from None
        if not owner._should_retry(
            retry_attempt,
            max_retry_attempts,
            exc,
        ):
            await owner._raise_console_model_call_failed_if_needed(
                request=request,
                exc=exc,
                trace_id=trace_id,
                session_execution=session_execution,
            )
            await owner._handle_query_error(
                request=request,
                exc=exc,
                trace_id=trace_id,
                locals_snapshot=locals(),
            )
            raise
        async for item in owner._stream_retryable_query_error(
            exc=exc,
            retry_attempt=retry_attempt,
            max_retry_attempts=max_retry_attempts,
            max_retries=max_retries,
            retry_state=retry_state,
            runtime=attempt_state.runtime,
            session_id=session_id,
        ):
            yield item


async def _stream_retry_attempts(
    owner: QueryAttemptOwner,
    *,
    attempt_state_ref: dict[str, Any],
    retry_state: Any,
    attempt_input: Any,
    outcome: Any,
    trace_id: str | None,
    request: AgentRequest,
    session_execution: Any,
    session_id: str,
    retry_settings: tuple[int, int, float, float],
) -> AsyncGenerator[tuple[Msg, bool], None]:
    max_retry_attempts, max_retries, backoff_base, backoff_cap = retry_settings
    for retry_attempt in range(max_retry_attempts):
        retry_state.prev_agent = retry_state.agent
        retry_state.prev_session_state_loaded = (
            retry_state.session_state_loaded
        )
        retry_state.agent = None
        retry_state.session_state_loaded = False
        attempt_state = owner._new_query_attempt_state()
        attempt_state_ref["value"] = attempt_state
        async for item in _stream_retry_attempt(
            owner,
            retry_attempt=retry_attempt,
            max_retry_attempts=max_retry_attempts,
            max_retries=max_retries,
            retry_state=retry_state,
            attempt_state=attempt_state,
            attempt_input=attempt_input,
            outcome=outcome,
            trace_id=trace_id,
            request=request,
            session_execution=session_execution,
            session_id=session_id,
            backoff_base=backoff_base,
            backoff_cap=backoff_cap,
        ):
            yield item
        if attempt_state.should_return or attempt_state.succeeded:
            return


def _reset_query_contexts(
    claims_context: Any,
    file_url_network_token: Any,
) -> None:
    try:
        claims_context.__exit__(None, None, None)
    except ValueError:
        logger.debug(
            "Skipped runtime invocation claims context reset from a different async context",
            exc_info=True,
        )
    try:
        reset_current_file_url_network(file_url_network_token)
    except ValueError:
        logger.debug(
            "Skipped file URL network context reset from a different async context",
            exc_info=True,
        )


def _cleanup_state_from_attempt(
    attempt_state: Any,
    retry_state: Any,
) -> tuple[Any, bool, Any]:
    cleanup_runtime = attempt_state.runtime
    cleanup_state_loaded = attempt_state.session_state_loaded
    runtime_start = attempt_state.runtime_start
    if cleanup_runtime is None and retry_state.prev_agent is not None:
        cleanup_state_loaded = (
            retry_state.session_state_loaded
            or retry_state.prev_session_state_loaded
        )
    return cleanup_runtime, cleanup_state_loaded, runtime_start


async def _commit_cleanup_state(
    owner: QueryAttemptOwner,
    *,
    cleanup_runtime: Any,
    cleanup_state_loaded: bool,
    retry_state: Any,
    request: AgentRequest,
    session_id: str,
    session_execution: Any,
) -> tuple[bool, bool, bool]:
    """Commit cleanup state and identify idempotent Cron replays."""
    fallback_agent = (
        retry_state.prev_agent if cleanup_runtime is None else None
    )
    commit_attempted = True
    save_result = await owner._save_state_during_cleanup(
        runtime=cleanup_runtime,
        session_state_loaded=cleanup_state_loaded,
        fallback_agent=fallback_agent,
        fallback_session_id=session_id,
        fallback_user_id=str(getattr(request, "user_id", "") or ""),
        fallback_skip_history=bool(
            getattr(request, "skip_history", False),
        ),
        fallback_session_execution=session_execution,
    )
    committed = (
        bool(save_result)
        if save_result is not None
        else bool(cleanup_state_loaded)
    )
    idempotent_replay = False
    if not committed:
        commit_attempted = False
        idempotent_replay = _has_persisted_cron_task_run(
            getattr(session_execution, "state", None),
            execution_key=str(
                getattr(request, "cron_execution_key", "") or "",
            ),
        )
        committed = idempotent_replay
    if (
        cleanup_runtime is None
        and fallback_agent is None
        and session_execution.has_uncommitted_state
    ):
        await session_execution.commit_state(session_execution.state)
        commit_attempted = True
        committed = True
    return commit_attempted, committed, idempotent_replay


def _cron_persistence_details(
    state: Any,
    *,
    persistence_key: str,
    execution_key: str,
) -> tuple[int, list[dict[str, Any]], bool, bool]:
    """Collect Cron delivery metadata from one persisted state snapshot."""
    return (
        _count_persisted_assistant_messages(
            state,
            persistence_key=persistence_key,
            execution_key=execution_key,
        ),
        _get_persisted_cron_assistant_content(
            state,
            persistence_key=persistence_key,
            execution_key=execution_key,
        ),
        _is_cron_output_delivery_replay_supported(
            state,
            persistence_key=persistence_key,
            execution_key=execution_key,
        ),
        _is_cron_output_delivery_completed(
            state,
            persistence_key=persistence_key,
            execution_key=execution_key,
        ),
    )


async def _close_after_persistence_failure(session_execution: Any) -> None:
    """Best-effort close without masking the original persistence exception."""
    try:
        await session_execution.close()
    except Exception:
        logger.warning(
            "Failed to close session execution after persistence failure",
            exc_info=True,
        )


async def _close_after_persistence_success(session_execution: Any) -> None:
    """Close a committed transaction without changing its result."""
    try:
        await session_execution.close()
    except Exception:
        logger.warning(
            "Failed to close session execution after confirmed persistence",
            exc_info=True,
        )


def _record_persistence_result(
    owner: QueryAttemptOwner,
    request: AgentRequest,
    result: QueryPersistenceResult,
    cleanup_runtime: Any = None,
) -> None:
    """Publish a persistence result and update the active runtime."""
    if cleanup_runtime is not None:
        cleanup_runtime.persistence_result = result
    recorder = getattr(owner, "_record_query_persistence_result", None)
    if callable(recorder):
        recorder(request, result)


def _build_persistence_failure_result(
    owner: QueryAttemptOwner,
    request: AgentRequest,
    session_id: str,
    cleanup_runtime: Any,
    commit_attempted: bool,
    exc: BaseException,
) -> None:
    """Record a failed cleanup while preserving the original exception."""
    result = QueryPersistenceResult(
        session_id=str(session_id),
        user_id=str(getattr(request, "user_id", "") or ""),
        assistant_message_count=0,
        commit_attempted=commit_attempted,
        committed=False,
        commit_error=str(exc) or type(exc).__name__,
    )
    _record_persistence_result(owner, request, result, cleanup_runtime)


async def _save_and_close_session_execution(
    owner: QueryAttemptOwner,
    *,
    cleanup_runtime: Any,
    cleanup_state_loaded: bool,
    retry_state: Any,
    request: AgentRequest,
    session_id: str,
    session_execution: Any,
) -> None:
    commit_attempted = False
    committed = False
    idempotent_replay = False
    commit_error: str | None = None
    assistant_message_count = 0
    persisted_assistant_content: list[dict[str, Any]] = []
    output_delivery_replay_supported = False
    output_delivery_completed = False
    if cleanup_runtime is not None:
        cleanup_runtime.session_state_commit_attempted = True
    try:
        commit_attempted = True
        (
            commit_attempted,
            committed,
            idempotent_replay,
        ) = await _commit_cleanup_state(
            owner,
            cleanup_runtime=cleanup_runtime,
            cleanup_state_loaded=cleanup_state_loaded,
            retry_state=retry_state,
            request=request,
            session_id=session_id,
            session_execution=session_execution,
        )
        if committed:
            (
                assistant_message_count,
                persisted_assistant_content,
                output_delivery_replay_supported,
                output_delivery_completed,
            ) = _cron_persistence_details(
                session_execution.state,
                persistence_key=str(
                    getattr(request, "cron_persistence_key", "") or "",
                ),
                execution_key=str(
                    getattr(request, "cron_execution_key", "") or "",
                ),
            )
    except BaseException as exc:
        _build_persistence_failure_result(
            owner,
            request,
            session_id,
            cleanup_runtime,
            commit_attempted,
            exc,
        )
        await _close_after_persistence_failure(session_execution)
        raise
    result = QueryPersistenceResult(
        session_id=str(session_id),
        user_id=str(getattr(request, "user_id", "") or ""),
        assistant_message_count=assistant_message_count,
        commit_attempted=commit_attempted,
        committed=committed,
        commit_error=(
            commit_error
            or (None if committed else "session_state_not_persisted")
        ),
        idempotent_replay=idempotent_replay,
        persisted_assistant_content=persisted_assistant_content,
        output_delivery_replay_supported=output_delivery_replay_supported,
        output_delivery_completed=output_delivery_completed,
    )
    if cleanup_runtime is not None:
        cleanup_runtime.session_state_committed = committed
    if committed:
        await _close_after_persistence_success(session_execution)
    _record_persistence_result(owner, request, result, cleanup_runtime)


def _count_persisted_assistant_messages(
    state: Any,
    *,
    persistence_key: str = "",
    execution_key: str = "",
) -> int:
    """Count assistant messages committed by this query only."""
    entries = _get_persisted_memory_entries(state)
    if entries is None:
        return 0
    if persistence_key or execution_key:
        entries = _get_cron_memory_window(
            state,
            entries,
            persistence_key=persistence_key,
            execution_key=execution_key,
        )
        if entries is None:
            return 0
    return sum(
        _get_memory_entry_role(entry) == "assistant" for entry in entries
    )


def _get_persisted_cron_assistant_content(
    state: Any,
    *,
    persistence_key: str = "",
    execution_key: str = "",
) -> list[dict[str, Any]]:
    """Return the final persisted assistant content for this Cron run."""
    entries = _get_persisted_memory_entries(state)
    if entries is None:
        return []
    entries = _get_cron_memory_window(
        state,
        entries,
        persistence_key=persistence_key,
        execution_key=execution_key,
    )
    if entries is None:
        return []
    for entry in reversed(entries):
        message = _get_memory_entry_message(entry)
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        return [block for block in content if isinstance(block, dict)]
    return []


def _get_persisted_memory_entries(state: Any) -> list[Any] | None:
    """Return the persisted Agent memory content when its shape is valid."""
    if not isinstance(state, dict):
        return None
    agent = state.get("agent")
    if not isinstance(agent, dict):
        return None
    memory = agent.get("memory")
    if not isinstance(memory, dict):
        return None
    entries = memory.get("content")
    return entries if isinstance(entries, list) else None


def _get_cron_memory_window(
    state: Any,
    entries: list[Any],
    *,
    persistence_key: str,
    execution_key: str,
) -> list[Any] | None:
    """Return the memory slice recorded for one Cron task run."""
    task_run = _get_persisted_cron_task_run(
        state,
        persistence_key=persistence_key,
        execution_key=execution_key,
    )
    if task_run is None:
        return None
    memory_start = task_run.get("memory_start")
    memory_end = task_run.get("memory_end")
    if not isinstance(memory_start, int) or not isinstance(memory_end, int):
        return None
    if memory_start < 0 or memory_end < memory_start:
        return None
    if memory_end > len(entries):
        return None
    return entries[memory_start:memory_end]


def _get_memory_entry_message(entry: Any) -> Any:
    """Normalize tuple/list memory entries to their message payload."""
    if isinstance(entry, (tuple, list)) and entry:
        return entry[0]
    return entry


def _get_memory_entry_role(entry: Any) -> str | None:
    """Return a memory entry role for dict and object message formats."""
    message = _get_memory_entry_message(entry)
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _is_cron_output_delivery_completed(
    state: Any,
    *,
    persistence_key: str = "",
    execution_key: str = "",
) -> bool:
    """Return the durable output-delivery receipt for this scheduled query."""
    task_run = _get_persisted_cron_task_run(
        state,
        persistence_key=persistence_key,
        execution_key=execution_key,
    )
    return bool(task_run and task_run.get("output_delivery_completed"))


def _is_cron_output_delivery_replay_supported(
    state: Any,
    *,
    persistence_key: str = "",
    execution_key: str = "",
) -> bool:
    """Only task runs created with a durable delivery record may replay it."""
    task_run = _get_persisted_cron_task_run(
        state,
        persistence_key=persistence_key,
        execution_key=execution_key,
    )
    return bool(task_run and task_run.get("cron_delivery_version") == 1)


def _get_persisted_cron_task_run(
    state: Any,
    *,
    persistence_key: str,
    execution_key: str,
) -> dict[str, Any] | None:
    """Find this query's task-run record without matching another Cron run."""
    if not isinstance(state, dict):
        return None
    task_runs = state.get("task_runs")
    if not isinstance(task_runs, list):
        return None
    return next(
        (
            run
            for run in reversed(task_runs)
            if isinstance(run, dict)
            and (
                run.get("persistence_key") == persistence_key
                or (
                    execution_key and run.get("execution_key") == execution_key
                )
            )
        ),
        None,
    )


def _has_persisted_cron_task_run(
    state: Any,
    *,
    execution_key: str,
) -> bool:
    """Return whether this scheduled request already committed its task run."""
    if not execution_key or not isinstance(state, dict):
        return False
    task_runs = state.get("task_runs")
    return bool(
        isinstance(task_runs, list)
        and any(
            isinstance(run, dict) and run.get("execution_key") == execution_key
            for run in task_runs
        ),
    )


async def stream_query_after_preflight(
    owner: QueryAttemptOwner,
    *,
    msgs: list[Any],
    request: AgentRequest,
    query: str | None,
    session_id: str,
    preflight: _QueryPreflight,
    session_execution: Any = None,
) -> AsyncGenerator[tuple[Msg, bool], None]:
    """Run retry attempts and final cleanup after preflight succeeds."""
    cleanup_runtime: _QueryRuntime | None = None
    cleanup_state_loaded = False
    runtime_start = None
    outcome = None
    try:
        async with _query_session_execution(
            owner,
            session_id,
            str(getattr(request, "user_id", "") or ""),
            request=request,
            session_execution=session_execution,
        ) as session_execution:
            logger.debug(
                "AgentRunner.stream_query: request=%s, agent_id=%s",
                request,
                owner.agent_id,
            )
            set_current_agent_id(owner.agent_id)
            file_url_network_token = set_current_file_url_network(
                owner._request_file_url_network(request),
            )
            trace_id = await owner._start_query_trace(request, msgs)
            claims_context = runtime_invocation_claims_context(
                session_id=session_id,
                trace_id=trace_id,
            )
            claims_context.__enter__()
            outcome = owner._new_query_turn_outcome()
            retry_state = owner._new_retry_state()
            attempt_input = owner._new_query_attempt_input(
                request=request,
                msgs=msgs,
                query=query,
                preflight=preflight,
                trace_id=trace_id,
                session_execution=session_execution,
            )
            attempt_state_ref = {
                "value": owner._new_query_attempt_state(),
            }
            try:
                async for item in _stream_retry_attempts(
                    owner,
                    attempt_state_ref=attempt_state_ref,
                    retry_state=retry_state,
                    attempt_input=attempt_input,
                    outcome=outcome,
                    trace_id=trace_id,
                    request=request,
                    session_execution=session_execution,
                    session_id=session_id,
                    retry_settings=_query_retry_settings(owner, preflight),
                ):
                    yield item
            finally:
                _reset_query_contexts(
                    claims_context,
                    file_url_network_token,
                )
                cleanup_runtime, cleanup_state_loaded, runtime_start = (
                    _cleanup_state_from_attempt(
                        attempt_state_ref["value"],
                        retry_state,
                    )
                )
                if session_execution is not None:
                    await _save_and_close_session_execution(
                        owner,
                        cleanup_runtime=cleanup_runtime,
                        cleanup_state_loaded=cleanup_state_loaded,
                        retry_state=retry_state,
                        request=request,
                        session_id=session_id,
                        session_execution=session_execution,
                    )
    finally:
        await owner._cleanup_query_resources(
            runtime=cleanup_runtime,
            session_state_loaded=cleanup_state_loaded,
            session_id=session_id,
        )
        await owner._cleanup_blocked_runtime_start(runtime_start)
        await owner._store_qa_content_if_needed(
            runtime=cleanup_runtime,
            query=query,
            outcome=outcome,
        )


@asynccontextmanager
async def _query_session_execution(
    owner: QueryAttemptOwner,
    session_id: str,
    user_id: str,
    *,
    request: AgentRequest | None = None,
    session_execution: Any = None,
) -> AsyncGenerator[Any, None]:
    if session_execution is not None:
        yield session_execution
        return
    session = getattr(owner, "session", None)
    if session is None or not hasattr(session, "execution"):
        yield None
        return
    async with session.execution(
        session_id,
        user_id=user_id,
        timeout_seconds=_session_execution_timeout(request),
    ) as execution:
        yield execution


def _session_execution_timeout(request: AgentRequest | None) -> float:
    """Return the bounded lock wait for an interactive or scheduled query."""
    if getattr(request, "execution_origin", None) != "scheduled":
        return 5.0
    timeout = getattr(request, "cron_timeout_seconds", None)
    if not isinstance(timeout, (int, float, str)) or isinstance(timeout, bool):
        return 5.0
    timeout_value = cast(int | float | str, timeout)
    try:
        timeout_seconds = float(timeout_value)
    except (TypeError, ValueError):
        return 5.0
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return 5.0
    return min(30.0, timeout_seconds / 3.0)
