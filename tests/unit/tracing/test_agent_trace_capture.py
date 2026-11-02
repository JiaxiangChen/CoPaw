# -*- coding: utf-8 -*-
"""Actual model request capture and scoped lifetime regression tests."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.message import Msg
from trace_sdk import global_tracer
from trace_sdk._records import reset, spans

from swe.tracing.agent_trace_capture import chat_capture_scope
from swe.tracing.agent_trace_model_wrapper import AgentTraceModelWrapper
from swe.tracing.chat_projection import project_chat_input


def test_input_keeps_more_than_32_messages_and_redacts_secrets():
    messages = [{"role": "user", "content": f"message {n}"} for n in range(40)]
    output = project_chat_input(messages, [{"name": "lookup"}], "auto")
    assert len(output["messages"]) == 40
    assert output["tools"] == [{"name": "lookup"}]
    assert output["tool_choice"] == "auto"


def test_input_is_bounded_valid_json_and_excludes_multimodal_payload(
    monkeypatch,
):
    monkeypatch.setenv("SWE_AGENT_TRACE_CHAT_MAX_BYTES", "2048")
    output = project_chat_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "中文" * 10000},
                    {"type": "image_url", "image_url": {"url": "data:secret"}},
                ],
            },
        ],
        [],
        None,
    )
    encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode()) <= 2048
    assert output["capture"]["truncated"] is True
    assert "data:secret" not in encoded


@pytest.mark.asyncio
async def test_wrapper_records_actual_input_and_returns_original_response():
    reset()
    response = SimpleNamespace(content=[{"type": "text", "text": "done"}])

    async def model(messages, **kwargs):
        assert messages == [{"role": "system", "content": "actual prompt"}]
        return response

    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with chat_capture_scope():
            result = await AgentTraceModelWrapper(model)(
                [{"role": "system", "content": "actual prompt"}],
                tools=[],
                tool_choice="none",
            )
    assert result is response
    captured = json.loads(spans[0]["attributes"]["cmb.input.arguments"])
    assert captured["messages"][0]["content"] == "actual prompt"


@pytest.mark.asyncio
async def test_stream_failure_preserves_last_snapshot_and_original_error():
    reset()
    chunk = SimpleNamespace(content=[{"type": "text", "text": "partial"}])

    async def stream():
        yield chunk
        raise RuntimeError("provider failed")

    async def model(*args, **kwargs):
        return stream()

    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with pytest.raises(RuntimeError, match="provider failed"):
            with chat_capture_scope():
                result = await AgentTraceModelWrapper(model)([], tools=[])
                assert await anext(result) is chunk
                await anext(result)
    output = json.loads(spans[0]["attributes"]["cmb.output.arguments"])
    assert output["content"][0]["text"] == "partial"
    assert output["status"] == "failed"
    assert output["complete"] is False


@pytest.mark.asyncio
async def test_inherited_task_does_not_write_parent_chat():
    reset()

    async def model(*args, **kwargs):
        return Msg("assistant", "background", "assistant")

    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with chat_capture_scope():
            await asyncio.create_task(AgentTraceModelWrapper(model)([]))
    assert "cmb.input.arguments" not in spans[0]["attributes"]


def test_output_budget_covers_compatibility_text(monkeypatch):
    from swe.tracing.chat_projection import project_chat_output

    monkeypatch.setenv("SWE_AGENT_TRACE_CHAT_MAX_BYTES", "2048")
    output = project_chat_output(Msg("assistant", "x" * 10000, "assistant"))
    assert (
        len(
            json.dumps(
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode(),
        )
        <= 2048
    )
    assert output["text_truncated"] is True


@pytest.mark.asyncio
async def test_cancellation_records_partial_and_restores_scope():
    from swe.tracing.agent_trace_capture import current_capture

    reset()
    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with pytest.raises(asyncio.CancelledError):
            with chat_capture_scope():
                current_capture().snapshot(
                    SimpleNamespace(
                        content=[{"type": "text", "text": "part"}],
                    ),
                )
                raise asyncio.CancelledError()
        assert current_capture() is None
    output = json.loads(spans[0]["attributes"]["cmb.output.arguments"])
    assert output["status"] == "cancelled"
    assert output["complete"] is False


@pytest.mark.asyncio
async def test_noop_scope_does_not_capture(monkeypatch):
    from swe.tracing import agent_trace_capture as module

    monkeypatch.setattr(
        module,
        "get_current_span",
        lambda: SimpleNamespace(is_recording=False),
    )

    async def model(*args, **kwargs):
        return "unchanged"

    with chat_capture_scope():
        assert await AgentTraceModelWrapper(model)([]) == "unchanged"


@pytest.mark.asyncio
async def test_stream_closes_underlying_generator():
    closed = []

    async def stream():
        try:
            yield SimpleNamespace(content=[{"type": "text", "text": "one"}])
            yield SimpleNamespace(content=[{"type": "text", "text": "two"}])
        finally:
            closed.append(True)

    async def model(*args, **kwargs):
        return stream()

    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with chat_capture_scope():
            result = await AgentTraceModelWrapper(model)([])
            await anext(result)
            await result.aclose()
    assert closed == [True]


def test_fake_requires_operation_name():
    with pytest.raises(ValueError, match="gen_ai.operation.name"):
        global_tracer.start_as_current_span("missing-operation")


@pytest.mark.asyncio
async def test_disabled_policy_suppresses_nested_spans():
    from swe.tracing.agent_trace_sdk import (
        chat_traced,
        sdk_capture_policy,
        start_child_span,
    )

    reset()

    @chat_traced()
    async def chat():
        async with start_child_span("agent.attempt", "invoke_agent"):
            return "done"

    with sdk_capture_policy(False):
        assert await chat() == "done"
    assert spans == []


@pytest.mark.asyncio
async def test_turn_capture_replaces_cumulative_frames_and_ignores_retry():
    from swe.tracing.agent_trace_capture import TurnCapture

    reset()
    async with global_tracer.start_as_current_span(
        "agent.run",
        operation_name="invoke_agent",
    ) as span:
        capture = TurnCapture(span, [])
        msg = Msg("assistant", "first", "assistant")
        capture.observe(msg, False)
        msg.content = "first second"
        capture.observe(msg, True)
        capture.observe(
            Msg(
                "assistant",
                "retry",
                "assistant",
                metadata={"retry_status": True},
            ),
            True,
        )
        capture.finish("completed")
    output = json.loads(spans[0]["attributes"]["cmb.output.arguments"])
    assert len(output["messages"]) == 1
    assert output["messages"][0]["text"] == "first second"


@pytest.mark.asyncio
async def test_projection_failure_does_not_change_business_result(monkeypatch):
    from swe.tracing import agent_trace_model_wrapper as module

    def fail(*args):
        raise ValueError("bad projection")

    monkeypatch.setattr(module, "project_chat_input", fail)

    async def model(*args, **kwargs):
        return "business result"

    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with chat_capture_scope():
            assert await AgentTraceModelWrapper(model)([]) == "business result"


@pytest.mark.asyncio
async def test_two_tasks_capture_their_own_prompts():
    reset()

    async def run(text):
        async def model(*args, **kwargs):
            await asyncio.sleep(0)
            return Msg("assistant", text, "assistant")

        async with global_tracer.start_as_current_span(
            "chat",
            operation_name="chat",
        ):
            with chat_capture_scope():
                await AgentTraceModelWrapper(model)(
                    [{"role": "user", "content": text}],
                )

    await asyncio.gather(run("tenant-one"), run("tenant-two"))
    prompts = [
        json.loads(s["attributes"]["cmb.input.arguments"])["messages"][0][
            "content"
        ]
        for s in spans
    ]
    assert sorted(prompts) == ["tenant-one", "tenant-two"]


@pytest.mark.asyncio
async def test_large_cancelled_snapshot_reports_truncation(monkeypatch):
    from swe.tracing.agent_trace_capture import current_capture

    monkeypatch.setenv("SWE_AGENT_TRACE_CHAT_MAX_BYTES", "2048")
    reset()
    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with pytest.raises(asyncio.CancelledError):
            with chat_capture_scope():
                current_capture().snapshot(
                    SimpleNamespace(
                        content=[{"type": "text", "text": "x" * 10000}],
                    ),
                )
                raise asyncio.CancelledError()
    output = json.loads(spans[0]["attributes"]["cmb.output.arguments"])
    assert output["capture"]["truncated"] is True
    assert (
        len(
            json.dumps(
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode(),
        )
        <= 2048
    )


def test_model_factory_installs_capture_when_old_tracing_is_off(monkeypatch):
    from swe.agents import model_factory

    monkeypatch.setattr(model_factory, "has_trace_manager", lambda: False)
    model = SimpleNamespace(model_name="test-model")
    wrapped = model_factory._wrap_model_with_tracing("provider", model)
    assert isinstance(wrapped._model, AgentTraceModelWrapper)
    assert wrapped._model._model is model
