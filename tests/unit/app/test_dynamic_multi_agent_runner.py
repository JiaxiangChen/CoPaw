# -*- coding: utf-8 -*-

from __future__ import annotations

import pytest
from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

from swe.app._app import DynamicMultiAgentRunner


class _FailingManager:
    async def get_agent(self, *_args, **_kwargs):
        raise RuntimeError("Authorization: Bearer secret-token")


@pytest.mark.asyncio
async def test_dynamic_runner_emits_standard_failed_response_for_exception():
    """路由失败必须保留 Runtime 终态协议，不能 yield 普通字典。"""
    runner = DynamicMultiAgentRunner()
    runner.set_multi_agent_manager(_FailingManager())

    events = [event async for event in runner.stream_query({"id": "req-1"})]

    assert len(events) == 1
    assert events[0].object == "response"
    assert events[0].status == RunStatus.Failed
    assert events[0].error.code == "agent_runtime_error"
    assert "secret-token" not in events[0].error.message
