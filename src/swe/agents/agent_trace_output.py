# -*- coding: utf-8 -*-
"""Bounded output projections for the parallel AgentTraceSDK integration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from agentscope.message import Msg

from ..tracing.sanitizer import sanitize_trace_value
from ..tracing.chat_projection import project_chat_output

_OUTPUT_MAX_BYTES = 2048
_HIGH_RISK_OUTPUT_TOOLS = frozenset(
    {
        "execute_shell_command",
        "read_file",
        "write_file",
        "copy_file",
        "copy_file_to_static",
        "grep_search",
        "glob_search",
    },
)


@dataclass(frozen=True)
class ToolTraceOutcome:
    """Business result plus the terminal result used only for tracing."""

    business_result: dict[str, Any] | None
    terminal_output: Any
    tool_name: str
    mcp_server: str | None = None


def _is_high_risk_tool(tool_name: str, mcp_server: str | None) -> bool:
    return tool_name in _HIGH_RISK_OUTPUT_TOOLS or bool(mcp_server)


def build_chat_output_arguments(result: Msg) -> dict[str, Any]:
    """Capture structured, redacted output within the chat byte budget."""
    return project_chat_output(result)


def _is_empty_output(value: Any) -> bool:
    return value is None or value == "" or value == []


def _failure_status(value: Any) -> str | None:
    if not isinstance(value, dict) or value.get("isError") is not True:
        return None
    if value.get("error_type") == "tool_timeout":
        return "timeout"
    return "error"


def build_tool_output_arguments(
    outcome: ToolTraceOutcome,
) -> dict[str, Any]:
    """Project terminal tool output into a bounded, redacted summary."""
    terminal_output = outcome.terminal_output
    failure_status = _failure_status(terminal_output)
    status = failure_status or (
        "empty" if _is_empty_output(terminal_output) else "ok"
    )
    safe_output = sanitize_trace_value(
        terminal_output,
        max_bytes=_OUTPUT_MAX_BYTES,
    )
    output: dict[str, Any] = {
        "status": status,
        "output_bytes": safe_output.original_bytes,
        "truncated": safe_output.truncated,
    }
    if status == "ok" and not _is_high_risk_tool(
        outcome.tool_name,
        outcome.mcp_server,
    ):
        output["output_preview"] = safe_output.value
    return json.loads(json.dumps(output, ensure_ascii=False, default=str))
