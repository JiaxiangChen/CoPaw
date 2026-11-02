# -*- coding: utf-8 -*-
"""Observe actual model inputs without changing model or stream results."""

from __future__ import annotations

import logging
from typing import Any

from .agent_trace_capture import current_capture, write_payload
from .chat_projection import project_chat_input

logger = logging.getLogger(__name__)


class AgentTraceModelWrapper:
    def __init__(self, model: Any):
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    @property
    def stream(self):
        return self._model.stream

    @stream.setter
    def stream(self, value):
        self._model.stream = value

    async def __call__(self, messages, *args, **kwargs):
        capture = current_capture()
        if capture is None:
            return await self._model(messages, *args, **kwargs)
        try:
            tools = kwargs.get("tools", args[0] if args else None)
            choice = kwargs.get(
                "tool_choice",
                args[1] if len(args) > 1 else None,
            )
            write_payload(
                capture.span,
                "cmb.input.arguments",
                project_chat_input(messages[-1:], tools, choice),
            )
        except Exception:
            logger.warning("Agent chat input projection failed", exc_info=True)
        result = await self._model(messages, *args, **kwargs)
        if hasattr(result, "__aiter__"):
            return self._observe_stream(result, capture)
        self._snapshot(capture, result)
        return result

    @staticmethod
    def _snapshot(capture, response):
        if current_capture() is not capture:
            return
        try:
            capture.snapshot(response)
        except Exception:
            logger.warning(
                "Agent chat output projection failed",
                exc_info=True,
            )

    async def _observe_stream(self, stream, capture):
        try:
            async for chunk in stream:
                self._snapshot(capture, chunk)
                yield chunk
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                await close()
