#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real SDK 0.2.2 Console smoke; run directly, outside pytest/fakes."""

import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    if "--worker" in sys.argv:
        asyncio.run(worker())
        return
    try:
        version = importlib.metadata.version("LR34.05-AgentTraceSDK")
    except importlib.metadata.PackageNotFoundError:
        raise SystemExit(
            "Real LR34.05-AgentTraceSDK==0.2.2 is required; install from the private artifact source.",
        )
    if version != "0.2.2":
        raise RuntimeError(f"Expected SDK 0.2.2, found {version}")
    env = {
        **os.environ,
        "AGENT_TRACE_ENABLED": "true",
        "AGENT_TRACE_EXPORTER": "console",
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, __file__, "--worker"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    spans = []
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if "span_id" in record:
            spans.append(record)
    assert spans, f"No Console spans: {result.stdout}\n{result.stderr}"
    assert all(s["attributes"].get("gen_ai.operation.name") for s in spans)
    chat = next(
        s for s in spans if s["attributes"]["gen_ai.operation.name"] == "chat"
    )
    assert "actual prompt" in chat["attributes"]["cmb.input.arguments"]
    assert "done" in chat["attributes"]["cmb.output.arguments"]
    assert any(
        s["attributes"]["gen_ai.operation.name"] == "execute_tool"
        for s in spans
    )
    assert any(
        '"cancelled"' in s["attributes"].get("cmb.output.arguments", "")
        for s in spans
    )
    print(f"SDK {version}: {len(spans)} Console spans verified")


async def worker():
    import trace_sdk

    assert "tests/fakes" not in str(trace_sdk.__file__), trace_sdk.__file__
    from agentscope.message import Msg
    from swe.tracing.agent_trace_sdk import (
        TraceFields,
        chat_traced,
        execute_tool_traced,
        global_tracer,
        shutdown_global_tracer,
    )
    from swe.tracing.agent_trace_capture import chat_capture_scope
    from swe.tracing.agent_trace_model_wrapper import AgentTraceModelWrapper
    from swe.agents.agent_trace_output import build_chat_output_arguments

    fields = TraceFields(
        "smoke-task",
        "smoke-user",
        "smoke-session",
        "smoke-agent",
        "test",
        "smoke-source",
    )

    async def model(*args, **kwargs):
        return Msg("assistant", "done", "assistant")

    @chat_traced(output_arguments_factory=build_chat_output_arguments)
    async def chat():
        with chat_capture_scope():
            return await AgentTraceModelWrapper(model)(
                [{"role": "user", "content": "actual prompt"}],
            )

    async with global_tracer.start_as_current_span(
        "agent.run",
        operation_name="invoke_agent",
        trace_fields=fields,
    ):
        await chat()

        @execute_tool_traced(
            tool_name="smoke-tool",
            output_arguments_factory=lambda result: {"result": result},
        )
        async def tool():
            return "safe result"

        await tool()

        @chat_traced()
        async def cancelled():
            from swe.tracing.agent_trace_capture import current_capture

            with chat_capture_scope():
                current_capture().snapshot(
                    Msg("assistant", "partial", "assistant"),
                )
                raise asyncio.CancelledError()

        try:
            await cancelled()
        except asyncio.CancelledError:
            pass
    shutdown_global_tracer()


if __name__ == "__main__":
    main()
