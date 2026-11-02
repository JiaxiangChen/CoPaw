# -*- coding: utf-8 -*-
"""Optional AgentTraceSDK boundary for local development."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterator, Mapping, TypeVar

_ALLOW_MISSING_TRACE_SDK_ENV = "SWE_ALLOW_MISSING_TRACE_SDK"
_F = TypeVar("_F", bound=Callable[..., Any])


def _missing_trace_sdk_is_allowed() -> bool:
    return os.environ.get(_ALLOW_MISSING_TRACE_SDK_ENV, "").lower() == "true"


try:
    from trace_sdk import (
        OperationName,
        SpanKind,
        TraceFields,
        chat_traced,
        execute_tool_traced,
        extract_trace_context,
        global_tracer,
        get_current_span,
        use_trace_context,
    )
    from trace_sdk.global_tracer import shutdown_global_tracer
except ModuleNotFoundError as exc:
    if exc.name != "trace_sdk" or not _missing_trace_sdk_is_allowed():
        raise

    class SpanKind(str, Enum):  # type: ignore[no-redef]
        """Span kinds used by the no-op development tracer."""

        SERVER = "SERVER"
        INTERNAL = "INTERNAL"
        CLIENT = "CLIENT"

    @dataclass(frozen=True)
    class TraceFields:  # type: ignore[no-redef]
        """Trace field contract retained for development imports."""

        task_id: str
        user_id: str
        session_id: str
        agent_id: str
        agent_version: str
        source_id: str

    class OperationName:  # type: ignore[no-redef]
        INVOKE_AGENT = "invoke_agent"
        CHAT = "chat"

    def get_current_span() -> None:
        return None

    class _NoopSpan:
        is_recording = False

        async def __aenter__(self) -> "_NoopSpan":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def set_attribute(self, _key: str, _value: Any) -> None:
            return None

    class _NoopGlobalTracer:
        def start_as_current_span(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> _NoopSpan:
            return _NoopSpan()

    global_tracer = _NoopGlobalTracer()

    def _noop_decorator(**_config: Any) -> Callable[[_F], _F]:
        def decorate(function: _F) -> _F:
            return function

        return decorate

    chat_traced = _noop_decorator
    execute_tool_traced = _noop_decorator

    def extract_trace_context(*_args: Any, **_kwargs: Any) -> None:
        """Ignore remote context when the optional SDK is unavailable."""
        return None

    @contextmanager
    def use_trace_context(*_args: Any, **_kwargs: Any) -> Iterator[None]:
        """Provide a no-op context manager for the development fallback."""
        yield

    def shutdown_global_tracer() -> None:
        return None


@contextmanager
def use_b3_trace_context(
    headers: Mapping[str, str] | None,
    trace_fields: TraceFields | None,
) -> Iterator[None]:
    """Bind a complete remote B3 parent context for the enclosed SDK span."""
    if not headers or trace_fields is None:
        yield
        return
    extracted = extract_trace_context(
        headers,
        trace_fields_resolver=lambda _headers: trace_fields,
    )
    if extracted is None:
        yield
        return
    with use_trace_context(extracted.span_context, extracted.trace_fields):
        yield


_capture_disabled: ContextVar[bool] = ContextVar(
    "sdk_capture_disabled",
    default=False,
)


@contextmanager
def sdk_capture_policy(enabled: bool):
    """Suppress every nested SDK entry for requests outside the supported scope."""
    token = _capture_disabled.set(not enabled)
    try:
        yield
    finally:
        _capture_disabled.reset(token)


def _scoped_decorator(decorator):
    def configure(**config):
        def decorate(function):
            traced_function = decorator(**config)(function)

            @wraps(traced_function)
            async def call(*args, **kwargs):
                if _capture_disabled.get():
                    return await function(*args, **kwargs)
                return await traced_function(*args, **kwargs)

            return call

        return decorate

    return configure


chat_traced = _scoped_decorator(chat_traced)
execute_tool_traced = _scoped_decorator(execute_tool_traced)


class _SkippedSpan:
    is_recording = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def set_attribute(self, *_args):
        return None


def start_child_span(name: str, operation_name: str):
    if _capture_disabled.get():
        return _SkippedSpan()
    return global_tracer.start_as_current_span(
        name,
        operation_name=operation_name,
    )
