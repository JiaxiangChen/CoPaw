# -*- coding: utf-8 -*-
"""Tests for AgentTraceSDK output projections."""

from agentscope.message import Msg

from swe.agents.agent_trace_output import (
    ToolTraceOutcome,
    build_chat_output_arguments,
    build_tool_output_arguments,
)
from swe.tracing.sanitizer import register_sensitive_values


def test_build_chat_output_arguments_keeps_only_safe_message_fields():
    register_sensitive_values(["tenant-secret"])
    result = Msg(
        name="Friday",
        role="assistant",
        content=[
            {"type": "text", "text": "token=tenant-secret"},
            {"type": "tool_use", "name": "read_file", "input": {"path": "x"}},
        ],
        metadata={"provider_raw": "must-not-export"},
    )

    output = build_chat_output_arguments(result)

    assert output["role"] == "assistant"
    assert output["tool_call_names"] == ["read_file"]
    assert "tenant-secret" not in output["text"]
    assert "provider_raw" not in output


def test_build_tool_output_arguments_hides_shell_preview():
    output = build_tool_output_arguments(
        ToolTraceOutcome(
            business_result=None,
            terminal_output="password=secret",
            tool_name="execute_shell_command",
        ),
    )

    assert output["status"] == "ok"
    assert output["output_bytes"] > 0
    assert "output_preview" not in output


def test_build_tool_output_arguments_includes_safe_preview():
    output = build_tool_output_arguments(
        ToolTraceOutcome(
            business_result=None,
            terminal_output={"count": 2, "message": "done"},
            tool_name="get_current_time",
        ),
    )

    assert output["status"] == "ok"
    assert output["output_preview"] == {"count": 2, "message": "done"}


def test_build_tool_output_arguments_classifies_structured_failures():
    timeout = build_tool_output_arguments(
        ToolTraceOutcome(
            business_result=None,
            terminal_output={"isError": True, "error_type": "tool_timeout"},
            tool_name="get_current_time",
        ),
    )
    mcp_error = build_tool_output_arguments(
        ToolTraceOutcome(
            business_result=None,
            terminal_output={"isError": True, "error_type": "mcp_tool_error"},
            tool_name="get_current_time",
            mcp_server="filesystem",
        ),
    )

    assert timeout["status"] == "timeout"
    assert mcp_error["status"] == "error"
    assert "output_preview" not in mcp_error


def test_chat_output_preserves_long_text_and_structured_tool_calls():
    text = "中文回复" * 1500
    result = Msg(
        name="Friday",
        role="assistant",
        content=[
            {"type": "text", "text": text},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "read_file",
                "input": {"path": "file.txt"},
            },
        ],
    )
    output = build_chat_output_arguments(result)
    assert output["text"] == text
    assert output["content"][1]["id"] == "call-1"
    assert output["content"][1]["input"] == {"path": "file.txt"}
    assert output["complete"] is True
    assert output["capture"]["truncated"] is False
