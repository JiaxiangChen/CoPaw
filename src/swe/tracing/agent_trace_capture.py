# -*- coding: utf-8 -*-
"""Task-owned capture state; a child task cannot mutate its parent's chat."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import json
import logging
from typing import Any

from .agent_trace_sdk import get_current_span
from .chat_projection import chat_limit, project_chat_output, project_message
from .sanitizer import _redact_trace_text

logger = logging.getLogger(__name__)
_current: ContextVar[ChatCapture | None] = ContextVar(
    "agent_chat_capture",
    default=None,
)


def write_payload(span: Any, key: str, payload: Any) -> None:
    try:
        span.set_attribute(
            key,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    except Exception:
        logger.warning("Agent trace payload capture failed", exc_info=True)


def capture_safely(function):
    """Projection failures must never replace a business result or exception."""

    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            logger.warning("Agent trace capture failed", exc_info=True)
            return None

    return call


def _snapshot_value(value, budget, depth=0):
    if budget[0] <= 0 or depth > 20:
        budget[1] = True
        return None
    if isinstance(value, str):
        # Immutable strings need no copy. Redact the complete value before
        # cutting it, or exact secrets/patterns could become unrecognizable.
        if len(value) > budget[0] // 4:
            encoded = value.encode("utf-8")
            if len(encoded) > budget[0]:
                encoded = _redact_trace_text(value).encode("utf-8")
                if len(encoded) > budget[0]:
                    budget[1] = True
                    encoded = encoded[: budget[0]]
                value = encoded.decode("utf-8", errors="ignore")
            budget[0] -= len(encoded)
        else:
            budget[0] -= len(value.encode("utf-8"))
        return value
    budget[0] -= 16
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if budget[0] <= 0:
                budget[1] = True
                break
            key = _snapshot_value(str(key), budget, depth + 1)
            result[key] = _snapshot_value(item, budget, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            if budget[0] <= 0:
                budget[1] = True
                break
            result.append(_snapshot_value(item, budget, depth + 1))
        return result
    return value if isinstance(value, (int, float, bool, type(None))) else None


_turn_capture: ContextVar[tuple[Any, Any] | None] = ContextVar(
    "agent_turn_capture",
    default=None,
)


@contextmanager
def turn_capture_scope(capture):
    token = _turn_capture.set((asyncio.current_task(), capture))
    try:
        yield
    finally:
        _turn_capture.reset(token)


def mark_turn_cancelled():
    state = _turn_capture.get()
    if state is not None and state[0] is asyncio.current_task():
        state[1].cancelled = True


@dataclass
class ChatCapture:
    span: Any
    task: Any
    last_output: Any = None
    snapshot_truncated: bool = False

    @capture_safely
    def snapshot(self, response: Any) -> None:
        # Detach one bounded snapshot without serializing cumulative JSON per token.
        budget = [chat_limit(), False]
        self.last_output = _snapshot_value(project_message(response), budget)
        self.snapshot_truncated = budget[1]


def current_capture() -> ChatCapture | None:
    capture = _current.get()
    if capture is None or capture.task is not asyncio.current_task():
        return None
    return capture


@contextmanager
def chat_capture_scope():
    span = get_current_span()
    if span is None or not getattr(span, "is_recording", False):
        yield
        return
    capture = ChatCapture(span, asyncio.current_task())
    token = _current.set(capture)
    try:
        yield
    except BaseException as exc:
        status = (
            "cancelled"
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
            else "failed"
        )
        output = capture_safely(project_chat_output)(
            capture.last_output,
            status,
        )
        output = output or {
            "schema_version": 2,
            "capture": {"capture_error": True},
        }
        output.update(status=status, complete=False)
        if capture.snapshot_truncated:
            output["capture"]["truncated"] = True
            output["text_truncated"] = True
        write_payload(span, "cmb.output.arguments", output)
        raise
    finally:
        _current.reset(token)


class TurnCapture:
    """Keep the latest visible snapshot per message, within one turn budget."""

    @capture_safely
    def __init__(self, span, messages):
        from .chat_projection import project_chat_input

        self.span = span
        self.messages = {}
        self.truncated = False
        self.cancelled = False
        self.snapshot_sizes = {}
        self.retained_bytes = 0
        self.enabled = span is not None and getattr(
            span,
            "is_recording",
            False,
        )
        if self.enabled:
            write_payload(
                span,
                "cmb.input.arguments",
                project_chat_input(messages, [], None),
            )

    @capture_safely
    def observe(self, message, last):
        if not self.enabled or getattr(message, "role", None) != "assistant":
            return
        metadata = getattr(message, "metadata", None) or {}
        if metadata.get("retry_status") or any(
            marker in str(metadata.get(key, "")).lower()
            for key in ("event_type", "message_type", "kind", "type")
            for marker in ("progress", "tool", "approval")
        ):
            return
        blocks = getattr(message, "content", [])
        if isinstance(blocks, list) and any(
            b.get("type") in {"tool_use", "tool_result"}
            for b in blocks
            if isinstance(b, dict)
        ):
            return
        key = getattr(message, "id", None) or "current"
        limit = chat_limit()
        budget = [limit, False]
        snapshot = _snapshot_value(project_message(message), budget)
        size = limit - budget[0]
        self.retained_bytes -= self.snapshot_sizes.pop(key, 0)
        self.messages[key] = snapshot
        self.snapshot_sizes[key] = size
        self.retained_bytes += size
        self.truncated |= budget[1]
        # Account only for the replaced snapshot; never re-project the turn.
        while self.retained_bytes > limit and len(self.messages) > 1:
            oldest = next(k for k in self.messages if k != key)
            del self.messages[oldest]
            self.retained_bytes -= self.snapshot_sizes.pop(oldest)
            self.truncated = True

    @capture_safely
    def finish(self, status):
        if not self.enabled:
            return
        from .chat_projection import bounded_payload

        if self.cancelled:
            status = "cancelled"
        payload = bounded_payload(
            {
                "schema_version": 2,
                "messages": [
                    project_chat_output(message, status)
                    for message in self.messages.values()
                ],
                "status": status,
                "complete": status == "completed",
            },
        )
        payload["capture"]["truncated"] |= self.truncated
        write_payload(self.span, "cmb.output.arguments", payload)
