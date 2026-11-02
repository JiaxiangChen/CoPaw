# -*- coding: utf-8 -*-
"""Document-contract tests for Main Agent TraceSDK instrumentation."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from agentscope.message import Msg

from trace_sdk._records import reset, spans
from trace_sdk import TraceFields, global_tracer

from swe.__version__ import __version__
from swe.app.answer_turn.models import TurnIdentity
from swe.app.runner.query_attempt import stream_query_after_preflight
from swe.app.runner.query_contracts import _QueryPreflight
from swe.app.runner.query_execution.admission import stream_admission
from swe.app.runner.query_execution import QueryExecution, QueryFrame
from swe.app.runner.runner import AgentRunner


class _Adapter:
    async def stream(self, _invocation):
        yield QueryFrame(
            message=Msg(name="Friday", role="assistant", content="done"),
            last=True,
        )


@pytest.mark.asyncio
async def test_query_handler_creates_one_server_root_span(tmp_path) -> None:
    reset()
    runner = AgentRunner(agent_id="agent-1", workspace_dir=tmp_path)
    runner._query_execution = QueryExecution(_Adapter())
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        source_id="source-request",
        channel="console",
        channel_meta={"answer_turn_identity": identity},
    )

    frames = [
        frame
        async for frame in runner.query_handler(
            [Msg(name="user", role="user", content="original question")],
            request=request,
        )
    ]

    assert len(frames) == 1
    assert len(spans) == 1
    root = spans[0]
    assert root["name"] == "agent.run"
    assert root["kind"] == "SERVER"
    assert root["parent_span_id"] is None
    assert root["trace_fields"]["user_id"] == "user-1"
    assert root["trace_fields"]["task_id"] == "session-1"
    assert root["trace_fields"]["session_id"].startswith("turn-")
    assert (
        request.channel_meta["turn_id"] == root["trace_fields"]["session_id"]
    )
    assert root["trace_fields"]["agent_id"] == "agent-1"
    assert root["trace_fields"]["agent_version"] == __version__
    assert root["trace_fields"]["source_id"] == "source-request"
    assert root["attributes"]["agent.user_message"] == "original question"


@pytest.mark.asyncio
async def test_query_handler_parents_server_span_to_request_b3_context(
    tmp_path,
) -> None:
    reset()
    runner = AgentRunner(agent_id="agent-1", workspace_dir=tmp_path)
    runner._query_execution = QueryExecution(_Adapter())
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        source_id="source-request",
        channel="console",
        b3_context={
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "X-B3-Spanid": "32befd146889a61a",
            "X-B3-Sampled": "1",
        },
        channel_meta={"answer_turn_identity": identity},
    )

    frames = [
        frame
        async for frame in runner.query_handler(
            [Msg(name="user", role="user", content="original question")],
            request=request,
        )
    ]

    assert len(frames) == 1
    assert len(spans) == 1
    assert spans[0]["parent_span_id"] == "32befd146889a61a"
    assert spans[0]["trace_id"] == "8267fd70bacf497704fec30eaa353979"
    assert spans[0]["sampled"] is True


@pytest.mark.asyncio
async def test_query_handler_uses_channel_meta_source_for_root_span(
    tmp_path,
) -> None:
    reset()
    runner = AgentRunner(agent_id="agent-1", workspace_dir=tmp_path)
    runner._query_execution = QueryExecution(_Adapter())
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={
            "answer_turn_identity": identity,
            "source_id": "source-meta",
        },
    )

    frames = [
        frame
        async for frame in runner.query_handler(
            [Msg(name="user", role="user", content="question")],
            request=request,
        )
    ]

    assert len(frames) == 1
    assert spans[0]["trace_fields"]["source_id"] == "source-meta"


@pytest.mark.asyncio
async def test_query_handler_uses_default_source_for_root_span(
    tmp_path,
) -> None:
    reset()
    runner = AgentRunner(agent_id="agent-1", workspace_dir=tmp_path)
    runner._query_execution = QueryExecution(_Adapter())
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={"answer_turn_identity": identity},
    )

    frames = [
        frame
        async for frame in runner.query_handler(
            [Msg(name="user", role="user", content="question")],
            request=request,
        )
    ]

    assert len(frames) == 1
    assert spans[0]["trace_fields"]["source_id"] == "default"


@pytest.mark.asyncio
async def test_scheduled_request_skips_agent_trace_sdk(tmp_path) -> None:
    reset()
    runner = AgentRunner(agent_id="agent-1", workspace_dir=tmp_path)
    runner._query_execution = QueryExecution(_Adapter())
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
        execution_origin="scheduled",
    )

    frames = [
        frame
        async for frame in runner.query_handler(
            [Msg(name="user", role="user", content="scheduled question")],
            request=request,
        )
    ]

    assert len(frames) == 1
    assert spans == []


@pytest.mark.asyncio
async def test_request_missing_required_trace_fields_skips_agent_trace_sdk(
    tmp_path,
) -> None:
    reset()
    runner = AgentRunner(agent_id="agent-1", workspace_dir=tmp_path)
    runner._query_execution = QueryExecution(_Adapter())
    request = SimpleNamespace(
        session_id="session-1",
        user_id="",
        channel="console",
        channel_meta={},
    )

    frames = [
        frame
        async for frame in runner.query_handler(
            [Msg(name="user", role="user", content="question")],
            request=request,
        )
    ]

    assert len(frames) == 1
    assert spans == []


class _AdmissionOwner:
    async def _prepare_query_preflight(self, **_kwargs):
        return _QueryPreflight(
            response=Msg(name="Friday", role="assistant", content="blocked"),
        )

    async def _cleanup_denied_memory(self, *_args, **_kwargs):
        raise AssertionError("cleanup is not expected")


@pytest.mark.asyncio
async def test_admission_is_a_child_span_of_agent_run() -> None:
    reset()
    fields = TraceFields(
        "task-1",
        "user-1",
        "session-1",
        "agent-1",
        "1.0",
        "source-1",
    )
    request = SimpleNamespace()

    async with global_tracer.start_as_current_span(
        "agent.run",
        trace_fields=fields,
    ):
        frames = [
            frame
            async for frame in stream_admission(
                _AdmissionOwner(),
                [],
                request=request,
                query="question",
                session_id="session-1",
                user_id="user-1",
            )
        ]

    assert frames[-1][1] is True
    assert [span["name"] for span in spans] == ["agent.run", "agent.admission"]
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]


class _AttemptOwner:
    agent_id = "agent-1"
    tenant_id = None

    def _request_file_url_network(self, _request):
        return None

    async def _start_query_trace(self, _request, _msgs):
        return None

    def _new_query_turn_outcome(self):
        return SimpleNamespace()

    def _new_retry_state(self):
        return SimpleNamespace(
            agent=None,
            prev_agent=None,
            session_state_loaded=False,
            prev_session_state_loaded=False,
        )

    def _load_query_retry_settings(self, _agent_config=None):
        return 1, 0, 0.0, 0.0

    def _new_query_attempt_input(self, **kwargs):
        return SimpleNamespace(**kwargs)

    def _new_query_attempt_state(self):
        return SimpleNamespace(
            runtime=None,
            runtime_start=None,
            session_state_loaded=False,
            should_return=False,
            succeeded=False,
        )

    async def _stream_retry_backoff_notice(self, **_kwargs):
        if _kwargs.get("yield_notice"):
            yield None

    async def _stream_single_query_attempt(self, *, attempt_state, **_kwargs):
        attempt_state.succeeded = True
        if _kwargs.get("yield_attempt"):
            yield None

    async def _cleanup_query_resources(self, **_kwargs):
        return None

    async def _cleanup_blocked_runtime_start(self, _runtime_start):
        return None

    async def _store_qa_content_if_needed(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_retry_attempt_is_a_child_span_of_agent_run() -> None:
    reset()
    fields = TraceFields(
        "task-1",
        "user-1",
        "session-1",
        "agent-1",
        "1.0",
        "source-1",
    )
    request = SimpleNamespace()

    async with global_tracer.start_as_current_span(
        "agent.run",
        trace_fields=fields,
    ):
        frames = [
            frame
            async for frame in stream_query_after_preflight(
                _AttemptOwner(),
                msgs=[],
                request=request,
                query="question",
                session_id="session-1",
                preflight=_QueryPreflight(),
            )
        ]

    assert frames == []
    assert [span["name"] for span in spans] == ["agent.run", "agent.attempt"]
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]
    assert spans[1]["attributes"]["retry.index"] == 0


def test_lifespan_shutdown_calls_documented_global_tracer_shutdown() -> None:
    from swe.app._app import _shutdown_lifespan_resources_impl

    assert "shutdown_global_tracer()" in inspect.getsource(
        _shutdown_lifespan_resources_impl,
    )
