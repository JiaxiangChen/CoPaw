# -*- coding: utf-8 -*-
"""Test fake for only the documented AgentTraceSDK surface used by Swe."""

from ._impl import (
    OperationName,
    get_current_span,
    SpanKind,
    TraceFields,
    decorator,
    extract_trace_context,
    global_tracer,
    use_trace_context,
)


def chat_traced(**config):
    return decorator("chat", **config)


def execute_tool_traced(**config):
    return decorator("execute_tool", **config)
