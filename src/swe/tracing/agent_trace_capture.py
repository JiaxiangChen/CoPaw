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
        encoded = value.encode("utf-8")
        if len(encoded) > budget[0]:
            budget[1] = True
            encoded = encoded[: budget[0]]
        budget[0] -= len(encoded)
        return encoded.decode("utf-8", errors="ignore")
    budget[0] -= 16
    if isinstance(value, dict):
        return {
            str(key): _snapshot_value(item, budget, depth + 1)
            for key, item in value.items()
            if budget[0] > 0
        }
    if isinstance(value, (list, tuple)):
        return [
            _snapshot_value(item, budget, depth + 1)
            for item in value
            if budget[0] > 0
        ]
    return value if isinstance(value, (int, float, bool, type(None))) else None


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
        self.messages[key] = project_chat_output(message)
        # A full serialized turn budget also bounds retained snapshots.
        from .chat_projection import bounded_payload

        bounded = bounded_payload({"messages": list(self.messages.values())})
        self.truncated = self.truncated or bounded["capture"]["truncated"]
        retained = bounded.get("messages", [])
        keys = list(self.messages)[-len(retained) :] if retained else []
        self.messages = dict(zip(keys, retained))

    @capture_safely
    def finish(self, status):
        if not self.enabled:
            return
        from .chat_projection import bounded_payload

        payload = bounded_payload(
            {
                "schema_version": 2,
                "messages": list(self.messages.values()),
                "status": status,
                "complete": status == "completed",
            },
        )
        payload["capture"]["truncated"] |= self.truncated
        write_payload(self.span, "cmb.output.arguments", payload)
