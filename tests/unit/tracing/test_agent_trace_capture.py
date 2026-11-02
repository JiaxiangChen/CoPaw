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


@pytest.mark.parametrize(
    "media",
    [
        {
            "type": "image",
            "source": {"type": "base64", "data": "PRIVATE_BASE64"},
        },
        {
            "type": "audio",
            "source": {"url": "https://media.test/file?sig=PRIVATE_SIGNATURE"},
        },
    ],
)
def test_tool_result_output_recursively_excludes_media(media):
    from swe.tracing.chat_projection import project_content

    message = {
        "role": "tool",
        "content": [
            {
                "type": "tool_result",
                "id": "call-1",
                "output": [
                    {"type": "text", "text": "safe result"},
                    media,
                    {"type": "tool_result", "output": [media]},
                ],
            },
        ],
    }
    output = project_chat_input([message], [], None)
    encoded = json.dumps(output)
    assert "PRIVATE_BASE64" not in encoded
    assert "PRIVATE_SIGNATURE" not in encoded
    assert "safe result" in encoded
    assert "unsupported_block" in encoded
    assert project_content([message["content"][0]])[0]["id"] == "call-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_snapshot_redacts_secret_before_budget_truncation(
    monkeypatch,
    status,
):
    from swe.tracing.agent_trace_capture import current_capture
    from swe.tracing.sanitizer import register_sensitive_values

    monkeypatch.setenv("SWE_AGENT_TRACE_CHAT_MAX_BYTES", "1024")
    secret = "opaqueSensitiveValue" * 200
    from swe.tracing.sanitizer import _runtime_secret_values

    monkeypatch.setattr(
        "swe.tracing.sanitizer._runtime_secret_values",
        type(_runtime_secret_values)("test_secrets", default=()),
    )
    register_sensitive_values([secret])
    reset()
    try:
        async with global_tracer.start_as_current_span(
            "chat",
            operation_name="chat",
        ):
            error = (
                asyncio.CancelledError
                if status == "cancelled"
                else RuntimeError
            )
            with pytest.raises(error):
                with chat_capture_scope():
                    current_capture().snapshot(
                        Msg("assistant", "prefix " + secret, "assistant"),
                    )
                    raise error()
        output = json.loads(spans[0]["attributes"]["cmb.output.arguments"])
        assert "opaqueSensitiveValue" not in json.dumps(output)
        assert "REDACTED" in json.dumps(output)
        assert output["status"] == status
    finally:
        reset()


@pytest.mark.asyncio
async def test_turn_stream_defers_projection_until_finish(monkeypatch):
    from swe.tracing import agent_trace_capture as module

    reset()
    async with global_tracer.start_as_current_span(
        "agent.run",
        operation_name="invoke_agent",
    ) as span:
        capture = module.TurnCapture(span, [])
        project = module.project_chat_output
        calls = []

        def tracked(*args, **kwargs):
            calls.append(True)
            return project(*args, **kwargs)

        monkeypatch.setattr(module, "project_chat_output", tracked)
        msg = Msg("assistant", "", "assistant")
        for n in range(100):
            msg.content = "x" * ((n + 1) * 5000)
            capture.observe(msg, False)
        assert calls == []
        capture.finish("completed")
        assert len(calls) == 1
    output = json.loads(spans[0]["attributes"]["cmb.output.arguments"])
    assert output["messages"][0]["text"] == "x" * 500000


def test_snapshot_preserves_sensitive_key_redaction_after_key_truncation():
    from swe.tracing.agent_trace_capture import _snapshot_value
    from swe.tracing.sanitizer import sanitize_trace_value

    value = {"a" * 200 + "credential": "opaqueValue"}
    safe = sanitize_trace_value(
        _snapshot_value(value, [1024, False]),
        max_bytes=1024,
    )
    assert "opaqueValue" not in json.dumps(safe.value)


@pytest.mark.asyncio
async def test_turn_snapshot_is_detached_and_retained_memory_is_bounded(
    monkeypatch,
):
    from swe.tracing.agent_trace_capture import TurnCapture

    monkeypatch.setenv("SWE_AGENT_TRACE_CHAT_MAX_BYTES", "2048")
    reset()
    async with global_tracer.start_as_current_span(
        "agent.run",
        operation_name="invoke_agent",
    ) as span:
        capture = TurnCapture(span, [])
        for _ in range(50):
            message = Msg(
                "assistant",
                [{"type": "text", "text": "x" * 500}],
                "assistant",
            )
            capture.observe(message, False)
            message.content[0]["text"] = "mutated"
        assert capture.retained_bytes <= 2048
        assert len(capture.messages) < 50
        assert all(
            item["content"][0]["text"] == "x" * 500
            for item in capture.messages.values()
        )
        capture.finish("completed")
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


@pytest.mark.asyncio
async def test_turn_cancellation_scope_is_task_owned_and_resets():
    from swe.tracing.agent_trace_capture import (
        mark_turn_cancelled,
        turn_capture_scope,
    )

    parent = SimpleNamespace(cancelled=False)
    child = SimpleNamespace(cancelled=False)

    async def cancel_from_child():
        mark_turn_cancelled()
        with turn_capture_scope(child):
            mark_turn_cancelled()

    with turn_capture_scope(parent):
        await asyncio.create_task(cancel_from_child())
        assert parent.cancelled is False
        assert child.cancelled is True
        mark_turn_cancelled()
        assert parent.cancelled is True
    parent.cancelled = False
    mark_turn_cancelled()
    assert parent.cancelled is False


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN "
        + "PRIVATE KEY-----\n"
        + "sensitive-material" * 200
        + "\n-----END "
        + "PRIVATE KEY-----",
        "authorization: Bearer " + "opaqueValue" * 200,
    ],
)
@pytest.mark.asyncio
async def test_turn_snapshot_redacts_whole_patterns_before_truncation(
    monkeypatch,
    text,
):
    from swe.tracing.agent_trace_capture import TurnCapture

    monkeypatch.setenv("SWE_AGENT_TRACE_CHAT_MAX_BYTES", "1024")
    reset()
    async with global_tracer.start_as_current_span(
        "agent.run",
        operation_name="invoke_agent",
    ) as span:
        capture = TurnCapture(span, [])
        capture.observe(Msg("assistant", text, "assistant"), False)
        capture.finish("cancelled")
    output = spans[0]["attributes"]["cmb.output.arguments"]
    assert "sensitive-material" not in output
    assert "opaqueValue" not in output
    assert "REDACTED" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "latest",
    [
        {"role": "user", "content": "latest question"},
        {"role": "tool", "tool_call_id": "call-1", "content": "latest result"},
        {"role": "system", "content": "latest hint"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "password=private-value"},
                {"type": "image", "source": {"data": "PRIVATE_MEDIA"}},
            ],
        },
        None,
    ],
)
async def test_model_input_capture_keeps_only_latest_message(latest):
    reset()
    messages = (
        [
            {"role": "system", "content": "old system"},
            {"role": "user", "content": "old question"},
            latest,
        ]
        if latest is not None
        else []
    )
    original = json.loads(json.dumps(messages))
    calls = []

    async def model(actual_messages, **kwargs):
        calls.append(actual_messages)
        return Msg("assistant", "done", "assistant")

    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with chat_capture_scope():
            await AgentTraceModelWrapper(model)(
                messages,
                tools=[{"name": "lookup"}],
                tool_choice="auto",
            )
    captured = json.loads(spans[0]["attributes"]["cmb.input.arguments"])
    assert (
        captured["messages"]
        == project_chat_input(messages[-1:], [], None)["messages"]
    )
    assert captured["tools"] == [{"name": "lookup"}]
    assert captured["tool_choice"] == "auto"
    assert "old system" not in json.dumps(captured)
    assert "old question" not in json.dumps(captured)
    assert "private-value" not in json.dumps(captured)
    assert "PRIVATE_MEDIA" not in json.dumps(captured)
    assert messages == original
    assert calls[0] is messages


@pytest.mark.asyncio
async def test_react_reasoning_captures_actual_formatted_latest_message():
    from agentscope.agent import ReActAgent
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.model import ChatResponse

    class Model:
        stream = True

        async def __call__(self, messages, **kwargs):
            self.received = messages

            async def stream():
                yield ChatResponse(content=[{"type": "text", "text": "done"}])

            return stream()

    reset()
    model = Model()
    agent = ReActAgent(
        name="agent",
        sys_prompt="system instruction",
        model=AgentTraceModelWrapper(model),
        formatter=OpenAIChatFormatter(),
    )
    await agent.memory.add(Msg("user", "latest question", "user"))
    async with global_tracer.start_as_current_span(
        "chat",
        operation_name="chat",
    ):
        with chat_capture_scope():
            await agent._reasoning()
    recorded = json.loads(spans[0]["attributes"]["cmb.input.arguments"])
    assert len(model.received) >= 2
    assert model.received[-1]["role"] == "user"
    assert "latest question" in json.dumps(model.received[-1])
    assert (
        recorded["messages"]
        == project_chat_input(
            model.received[-1:],
            [],
            None,
        )["messages"]
    )
    assert "latest question" in json.dumps(recorded["messages"])
