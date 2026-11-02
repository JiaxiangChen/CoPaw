# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

import pytest
from agentscope.message import Msg

from swe.app.runner.runner import AgentRunner


class _FakeTaskTracker:
    def __init__(self) -> None:
        self.stopping_run_keys: list[str] = []

    async def mark_stopping(self, run_key: str) -> None:
        self.stopping_run_keys.append(run_key)


class _FakeAgent:
    def __init__(self) -> None:
        self.interrupted = False

    async def interrupt(self) -> None:
        self.interrupted = True


@pytest.mark.asyncio
async def test_query_timeout_marks_run_stopping_before_interrupting_agent():
    tracker = _FakeTaskTracker()
    agent = _FakeAgent()
    runner = AgentRunner(task_tracker=tracker)

    async def _slow_stream():
        await asyncio.sleep(0.02)
        yield Msg(name="Friday", role="assistant", content="late"), False

    results = [
        item
        async for item in runner._enforce_query_timeout(
            _slow_stream(),
            session_id="session-1",
            agent=agent,
            timeout_seconds=0.01,
            run_key="chat-1",
        )
    ]

    assert tracker.stopping_run_keys == ["chat-1"]
    assert agent.interrupted is True
    assert len(results) == 1
    assert results[0][1] is True


@pytest.mark.asyncio
async def test_selected_expert_does_not_receive_special_request_deadline():
    agent = _FakeAgent()
    agent._request_context = {
        "selected_expert_execution": True,
        "selected_expert_id": "expert-1",
    }
    runner = AgentRunner()

    async def stream():
        deadline = agent._request_context.get(
            "_selected_expert_request_deadline",
        )
        assert deadline is None
        yield Msg(name="Friday", role="assistant", content="done"), True

    assert (
        len(
            [
                item
                async for item in runner._enforce_query_timeout(
                    stream(),
                    session_id="session-1",
                    agent=agent,
                    timeout_seconds=1,
                )
            ],
        )
        == 1
    )
