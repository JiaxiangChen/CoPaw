# -*- coding: utf-8 -*-
"""Admission ordering for one query execution."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from swe.tracing.models import TraceStatus
from swe.tracing.agent_trace_sdk import start_child_span

from ..command_dispatch import _is_command, run_command_path
from ..query_attempt import _query_session_execution
from ..session_lifecycle import admit_user_turn


async def stream_admission(
    owner: Any,
    msgs: list[Any],
    *,
    request: AgentRequest,
    query: str | None,
    session_id: str,
    user_id: str,
) -> AsyncIterator[tuple[Msg, bool]]:
    """Resolve admission before command or normal query execution."""
    async with start_child_span(
        "agent.admission",
        operation_name="swe.admission",
    ):
        async with _query_session_execution(
            owner,
            session_id,
            user_id,
            request=request,
        ) as session_execution:
            if session_execution is not None and not (
                query and _is_command(query)
            ):
                await admit_user_turn(
                    session_execution,
                    msgs=msgs,
                    request=request,
                )
            preflight_args = {
                "session_id": session_id,
                "user_id": user_id,
                "query": query,
                "request": request,
            }
            if session_execution is not None:
                preflight_args["session_execution"] = session_execution
            preflight = await owner._prepare_query_preflight(**preflight_args)
            if preflight.response is not None:
                yield preflight.response, True
                if preflight.cleanup_denied_memory:
                    cleanup_args = {
                        "session_id": session_id,
                        "user_id": user_id,
                        "denial_response": preflight.response,
                    }
                    if session_execution is not None:
                        cleanup_args["session_execution"] = session_execution
                    await owner._cleanup_denied_session_memory(**cleanup_args)
                return

            if (
                not preflight.approval_consumed
                and query
                and _is_command(query)
            ):
                trace_id = await owner._start_query_trace(request, msgs)
                try:
                    command_args = (request, msgs, owner)
                    if session_execution is None:
                        command_stream = run_command_path(*command_args)
                    else:
                        command_stream = run_command_path(
                            *command_args,
                            session_execution=session_execution,
                        )
                    async for message, last in command_stream:
                        yield message, last
                except asyncio.CancelledError:
                    await owner._end_trace_if_needed(
                        trace_id,
                        TraceStatus.CANCELLED,
                    )
                    raise
                except Exception as exc:
                    await owner._end_trace_if_needed(
                        trace_id,
                        TraceStatus.ERROR,
                        str(exc),
                    )
                    raise
                await owner._end_trace_if_needed(
                    trace_id,
                    TraceStatus.COMPLETED,
                )
                return

            query_args = {
                "request": request,
                "query": query,
                "session_id": session_id,
                "preflight": preflight,
            }
            if session_execution is not None:
                query_args["session_execution"] = session_execution
            async for message, last in owner._stream_query_after_preflight(
                msgs,
                **query_args,
            ):
                yield message, last
