# -*- coding: utf-8 -*-
"""Implementation shared by the test-only documented SDK fake."""

from __future__ import annotations

import functools
import inspect
import uuid
import contextvars
import contextlib
from dataclasses import asdict, dataclass
from enum import Enum
import json
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from . import _records


class OperationName:
    INVOKE_AGENT = "invoke_agent"
    CHAT = "chat"


_active_span = contextvars.ContextVar("fake_active_span", default=None)


def get_current_span():
    return _active_span.get()


class SpanKind(str, Enum):
    SERVER = "SERVER"
    INTERNAL = "INTERNAL"
    CLIENT = "CLIENT"


@dataclass(frozen=True)
class TraceFields:
    task_id: str
    user_id: str
    session_id: str
    agent_id: str
    agent_version: str
    source_id: str


class Span:
    def __init__(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        trace_fields: TraceFields | None = None,
        operation_name: str | None = None,
    ) -> None:
        if not isinstance(operation_name, str) or not operation_name.strip():
            raise ValueError(f"{name}: gen_ai.operation.name is required")
        self.is_recording = True
        parent = _records.current_span.get()
        self.record = {
            "name": name,
            "kind": kind.value,
            "span_id": uuid.uuid4().hex,
            "parent_span_id": parent["span_id"] if parent else None,
            "trace_id": parent["trace_id"] if parent else uuid.uuid4().hex,
            "sampled": parent["sampled"] if parent else True,
            "trace_fields": asdict(trace_fields) if trace_fields else None,
            "attributes": {"gen_ai.operation.name": operation_name},
        }
        self._token: contextvars.Token | None = None

    async def __aenter__(self):
        _records.spans.append(self.record)
        self._token = _records.current_span.set(self.record)
        self._span_token = _active_span.set(self)
        return self

    async def __aexit__(self, _type, _value, _traceback):
        if self._token is not None:
            _records.current_span.reset(self._token)
            _active_span.reset(self._span_token)

    def set_attribute(self, key: str, value: Any) -> None:
        self.record["attributes"][key] = value


class GlobalTracer:
    def start_as_current_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        trace_fields: TraceFields | None = None,
        operation_name: str | None = None,
    ) -> Span:
        return Span(
            name,
            kind=kind,
            trace_fields=trace_fields,
            operation_name=operation_name,
        )


global_tracer = GlobalTracer()


def extract_trace_context(
    headers: Mapping[str, str],
    *,
    trace_fields_resolver: Callable[[Mapping[str, str]], TraceFields],
):
    return SimpleNamespace(
        span_context={
            "trace_id": headers["X-B3-Traceid"],
            "span_id": headers["X-B3-Spanid"],
            "sampled": headers["X-B3-Sampled"].lower() in {"1", "true"},
        },
        trace_fields=trace_fields_resolver(headers),
    )


@contextlib.contextmanager
def use_trace_context(span_context, _trace_fields):
    token = _records.current_span.set(
        {
            "trace_id": span_context["trace_id"],
            "span_id": span_context["span_id"],
            "sampled": span_context["sampled"],
        },
    )
    try:
        yield
    finally:
        _records.current_span.reset(token)


def decorator(name: str, **config: Any) -> Callable:
    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapped(*args: Any, **kwargs: Any):
            async with global_tracer.start_as_current_span(
                name,
                operation_name=name,
            ) as span:
                input_factory = config.get("input_arguments_factory")
                if input_factory is not None:
                    try:
                        span.set_attribute(
                            "cmb.input.arguments",
                            json.dumps(input_factory(*args, **kwargs)),
                        )
                    except Exception:
                        pass
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                output_factory = config.get("output_arguments_factory")
                if output_factory is not None:
                    output = output_factory(result)
                    span.set_attribute(
                        "cmb.output.arguments",
                        json.dumps(
                            output,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                return result

        wrapped._trace_sdk_config = config
        return wrapped

    return decorate
