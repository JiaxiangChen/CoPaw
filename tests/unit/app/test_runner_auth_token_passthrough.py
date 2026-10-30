# -*- coding: utf-8 -*-
# pylint: disable=protected-access
from __future__ import annotations

from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from swe.agents.hook_runtime.models import HookSessionOverlay
from swe.agents.tools.subagent_background import (
    create_background_subagent_tools,
)
from swe.agents.tool_guard_mixin import ToolGuardMixin
from swe.app.subagents import BackgroundSubAgentStartBlocked
from swe.app.runner.runner import (
    AgentRunner,
    _approved_tool_call_from_record,
)


def _fake_agent_config():
    return SimpleNamespace(
        id="test-agent",
        mcp=None,
        running=SimpleNamespace(
            suggestions=SimpleNamespace(
                enabled=False,
            ),
        ),
    )


@pytest.fixture(autouse=True)
def fake_model_preparation(monkeypatch):
    """These request metadata tests replace Agent/model construction."""
    provider_manager = SimpleNamespace(refresh_if_due=AsyncMock())
    monkeypatch.setattr(
        "swe.app.runner.query_runtime.ProviderManager.get_or_create_instance",
        AsyncMock(return_value=provider_manager),
    )
    monkeypatch.setattr(
        "swe.agents.model_factory.prepare_model_config_async",
        AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_query_handler_injects_auth_headers_into_mcp_headers_and_context(
    monkeypatch,
) -> None:
    runner = AgentRunner(agent_id="test-agent")
    runner.session = SimpleNamespace(
        load_session_state=AsyncMock(),
        mutate_session_state=AsyncMock(return_value={}),
        save_session_state=AsyncMock(),
    )
    setattr(runner, "_chat_manager", None)

    captured: dict[str, Any] = {}

    def fake_build_clients(
        _mcp,
        *,
        tenant_id=None,
        user_id=None,
        passthrough_headers=None,
        session_id=None,
        chat_id=None,
        trace_id=None,
        **kwargs,
    ):
        del tenant_id, user_id, kwargs
        captured["passthrough_headers"] = passthrough_headers
        captured["session_id"] = session_id
        captured["chat_id"] = chat_id
        captured["trace_id"] = trace_id
        return []

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]
            self.memory = SimpleNamespace(content=[])

        async def register_mcp_clients(self):
            return

        def set_console_output_enabled(self, enabled=False):
            del enabled

        def rebuild_sys_prompt(self):
            return

        async def __call__(self, _msgs):
            return

    async def fake_stream_printing_messages(*, agents, coroutine_task):
        del agents
        await coroutine_task
        for item in ():
            yield item

    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _fake_agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        fake_build_clients,
    )
    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    monkeypatch.setattr(
        "swe.app.runner.runner.stream_printing_messages",
        fake_stream_printing_messages,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "swe.app.runner.context_references.build_context_reference_directives",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "swe.app.runner.skill_selection.build_skill_use_directives",
        lambda **kwargs: [],
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        trace_id="trace-1",
        auth_token="token-123",
        cookie="foo=bar; com.cmb.dw.rtl.sso.token=auth-123",
    )
    msgs = [SimpleNamespace(get_text_content=lambda: "hello")]

    results = []
    async for item in runner.query_handler(msgs, request=request):
        results.append(item)

    assert not results
    assert captured["passthrough_headers"] == {
        "cookie": "foo=bar; com.cmb.dw.rtl.sso.token=auth-123",
    }
    assert captured["session_id"] == "session-1"
    assert captured["trace_id"] == "trace-1"
    assert captured["request_context"]["auth_token"] == "token-123"


def test_create_agent_for_query_injects_selected_expert_id_from_channel_meta(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.tenant_id = "tenant-1"
    runner.session = SimpleNamespace(
        _get_save_path=lambda session_id, user_id: (
            f"/tmp/{session_id}-{user_id}.json"
        ),
    )
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)

    runner._create_agent_for_query(
        agent_config=_fake_agent_config(),
        env_context="",
        mcp_clients=[],
        request=SimpleNamespace(
            channel_meta={"selected_expert_id": "expert-1"},
        ),
        session_id="session-1",
        user_id="user-1",
        channel="console",
        chat=SimpleNamespace(id="chat-1"),
        turn_id="turn-1",
        hook_overlay=HookSessionOverlay(),
        auth_token=None,
        approved_tool_call=None,
    )

    assert captured["request_context"]["selected_expert_id"] == "expert-1"


def test_create_agent_for_query_injects_only_server_annotation_context(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.tenant_id = "tenant-1"
    runner.session = SimpleNamespace(
        _get_save_path=lambda session_id, user_id: (
            f"/tmp/{session_id}-{user_id}.json"
        ),
    )
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    trusted = {
        "source_path": str(tmp_path / "media" / "source.html"),
        "expected_annotation_ids": ["ann-1"],
    }

    runner._create_agent_for_query(
        agent_config=_fake_agent_config(),
        env_context="",
        mcp_clients=[],
        request=SimpleNamespace(
            channel_meta={"_document_annotation_context": trusted},
        ),
        session_id="session-1",
        user_id="user-1",
        channel="console",
        chat=SimpleNamespace(id="chat-1"),
        turn_id="turn-1",
        hook_overlay=HookSessionOverlay(),
        auth_token=None,
        approved_tool_call=None,
    )

    assert (
        captured["request_context"]["_document_annotation_context"] == trusted
    )


def test_create_agent_for_query_forces_the_selected_expert_start(
    monkeypatch,
    tmp_path,
) -> None:
    """A submitted expert selection starts its exact enabled definition."""
    selected_id = "11111111-1111-4111-8111-111111111111"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / f"{selected_id}.toml").write_text(
        'name = "researcher"\n'
        'description = "Research expert."\n'
        'instruction = "Research the requested topic."\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.tenant_id = "tenant-1"
    runner.session = SimpleNamespace(
        _get_save_path=lambda session_id, user_id: (
            f"/tmp/{session_id}-{user_id}.json"
        ),
    )
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)

    runner._create_agent_for_query(
        agent_config=_fake_agent_config(),
        env_context="",
        mcp_clients=[],
        request=SimpleNamespace(
            channel_meta={"selected_expert_id": selected_id},
        ),
        session_id="session-1",
        user_id="user-1",
        channel="console",
        chat=SimpleNamespace(id="chat-1"),
        turn_id="turn-1",
        hook_overlay=HookSessionOverlay(),
        auth_token=None,
        approved_tool_call=None,
        current_user_text="Research this topic",
    )

    forced = json.loads(captured["request_context"]["forced_tool_call_json"])
    assert forced["name"] == "start_subagent"
    assert forced["input"] == {
        "expert_name": "researcher",
        "objective": "Research this topic",
    }


@pytest.mark.asyncio
async def test_selected_expert_forced_call_invokes_registered_start_tool(
    monkeypatch,
    tmp_path,
) -> None:
    """The forced payload must match the registered start tool signature."""
    selected_id = "11111111-1111-4111-8111-111111111111"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / f"{selected_id}.toml").write_text(
        'name = "researcher"\n'
        'description = "Research expert."\n'
        'instruction = "Research the requested topic."\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.tenant_id = "tenant-1"
    runner.session = SimpleNamespace(
        _get_save_path=lambda session_id, user_id: (
            f"/tmp/{session_id}-{user_id}.json"
        ),
    )
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]

    async def start(**kwargs):
        captured["start"] = kwargs
        return BackgroundSubAgentStartBlocked(limit=1)

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    runner._create_agent_for_query(
        agent_config=_fake_agent_config(),
        env_context="",
        mcp_clients=[],
        request=SimpleNamespace(
            channel_meta={"selected_expert_id": selected_id},
        ),
        session_id="session-1",
        user_id="user-1",
        channel="console",
        chat=SimpleNamespace(id="chat-1"),
        turn_id="turn-1",
        hook_overlay=HookSessionOverlay(),
        auth_token=None,
        approved_tool_call=None,
        current_user_text="Research this topic",
    )

    forced = json.loads(captured["request_context"]["forced_tool_call_json"])
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(start=start),
        parent_agent_config=_fake_agent_config(),
        workspace_dir=tmp_path,
        request_context={
            **captured["request_context"],
            "tenant_id": "tenant-1",
            "agent_id": "test-agent",
            "session_id": "session-1",
            "chat_id": "chat-1",
        },
        selected_expert_id=selected_id,
    )

    await tools["start_subagent"](**forced["input"])

    assert captured["start"]["spec"].name == "researcher"
    assert captured["start"]["spec"].objective == "Research this topic"


def test_selected_expert_keeps_matching_approved_start_for_hook_replay(
    monkeypatch,
    tmp_path,
) -> None:
    """Approval recovery must retain the original call ID and objective."""
    selected_id = "11111111-1111-4111-8111-111111111111"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / f"{selected_id}.toml").write_text(
        'name = "researcher"\n'
        'description = "Research expert."\n'
        'instruction = "Research the requested topic."\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.tenant_id = "tenant-1"
    runner.session = SimpleNamespace(
        _get_save_path=lambda session_id, user_id: (
            f"/tmp/{session_id}-{user_id}.json"
        ),
    )
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    approved_start = {
        "id": "approved-start-id",
        "name": "start_subagent",
        "input": {
            "expert_name": "researcher",
            "objective": "Research this topic",
        },
        "_approval_replay": {"approval_kind": "hook_pre_tool_use"},
    }

    runner._create_agent_for_query(
        agent_config=_fake_agent_config(),
        env_context="",
        mcp_clients=[],
        request=SimpleNamespace(
            channel_meta={"selected_expert_id": selected_id},
        ),
        session_id="session-1",
        user_id="user-1",
        channel="console",
        chat=SimpleNamespace(id="chat-1"),
        turn_id="turn-1",
        hook_overlay=HookSessionOverlay(),
        auth_token=None,
        approved_tool_call=approved_start,
        current_user_text="/approve",
    )

    assert json.loads(
        captured["request_context"]["forced_tool_call_json"],
    ) == (approved_start)


def test_create_agent_for_query_keeps_subagents_disabled_by_default(
    monkeypatch,
    tmp_path,
) -> None:
    """Real chat runner contexts keep delegation disabled unless opted in."""
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.tenant_id = "tenant-1"
    runner.session = SimpleNamespace(
        _get_save_path=lambda session_id, user_id: (
            f"/tmp/{session_id}-{user_id}.json"
        ),
    )
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)

    runner._create_agent_for_query(
        agent_config=_fake_agent_config(),
        env_context="",
        mcp_clients=[],
        request=SimpleNamespace(),
        session_id="session-1",
        user_id="user-1",
        channel="console",
        chat=SimpleNamespace(id="chat-1"),
        turn_id="turn-1",
        hook_overlay=HookSessionOverlay(),
        auth_token=None,
        approved_tool_call=None,
    )

    assert captured["request_context"]["agent_role"] == "main"
    assert captured["request_context"]["enable_subagents"] is False


def test_create_agent_for_query_enables_subagents_when_request_opts_in(
    monkeypatch,
    tmp_path,
) -> None:
    """Delegation is exposed only when the incoming request opts in."""
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.tenant_id = "tenant-1"
    runner.session = SimpleNamespace(
        _get_save_path=lambda session_id, user_id: (
            f"/tmp/{session_id}-{user_id}.json"
        ),
    )
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)

    runner._create_agent_for_query(
        agent_config=_fake_agent_config(),
        env_context="",
        mcp_clients=[],
        request=SimpleNamespace(enable_subagents=True),
        session_id="session-1",
        user_id="user-1",
        channel="console",
        chat=SimpleNamespace(id="chat-1"),
        turn_id="turn-1",
        hook_overlay=HookSessionOverlay(),
        auth_token=None,
        approved_tool_call=None,
        current_user_text="请用子代理分析",
    )

    assert captured["request_context"]["enable_subagents"] is True
    assert captured["request_context"]["current_user_text"] == "请用子代理分析"


@pytest.mark.asyncio
async def test_query_handler_keeps_existing_passthrough_headers(monkeypatch):
    runner = AgentRunner(agent_id="test-agent")
    runner.session = SimpleNamespace(
        load_session_state=AsyncMock(),
        mutate_session_state=AsyncMock(return_value={}),
        save_session_state=AsyncMock(),
    )
    setattr(runner, "_chat_manager", None)

    captured: dict[str, Any] = {}

    def fake_build_clients(
        _mcp,
        *,
        tenant_id=None,
        user_id=None,
        passthrough_headers=None,
        session_id=None,
        chat_id=None,
        trace_id=None,
        **kwargs,
    ):
        del tenant_id, user_id, kwargs
        captured["passthrough_headers"] = passthrough_headers
        captured["session_id"] = session_id
        captured["chat_id"] = chat_id
        captured["trace_id"] = trace_id
        return []

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]
            self.memory = SimpleNamespace(content=[])

        async def register_mcp_clients(self):
            return

        def set_console_output_enabled(self, enabled=False):
            del enabled

        def rebuild_sys_prompt(self):
            return

        async def __call__(self, _msgs):
            return

    async def fake_stream_printing_messages(*, agents, coroutine_task):
        del agents
        await coroutine_task
        for item in ():
            yield item

    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _fake_agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        fake_build_clients,
    )
    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    monkeypatch.setattr(
        "swe.app.runner.runner.stream_printing_messages",
        fake_stream_printing_messages,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "swe.app.runner.context_references.build_context_reference_directives",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "swe.app.runner.skill_selection.build_skill_use_directives",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.get_current_passthrough_headers",
        lambda: {
            "authorization": "Bearer existing",
            "cookie": "foo=existing",
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "X-B3-Spanid": "32befd146889a61a",
        },
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        trace_id="trace-1",
        auth_token="token-123",
        cookie="foo=bar; com.cmb.dw.rtl.sso.token=auth-123",
        passthrough_headers={
            "X-B3-BusinessId": "LQ1303LMES-WEB",
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        },
    )
    msgs = [SimpleNamespace(get_text_content=lambda: "hello")]

    results = []
    async for item in runner.query_handler(msgs, request=request):
        results.append(item)

    assert not results
    assert captured["passthrough_headers"] == {
        "authorization": "Bearer existing",
        "cookie": "foo=bar; com.cmb.dw.rtl.sso.token=auth-123",
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "X-B3-Spanid": "32befd146889a61a",
        "X-B3-BusinessId": "LQ1303LMES-WEB",
    }
    assert captured["session_id"] == "session-1"
    assert captured["trace_id"] == "trace-1"
    assert captured["request_context"]["auth_token"] == "token-123"


@pytest.mark.asyncio
async def test_query_handler_injects_identity_into_request_context(
    monkeypatch,
):
    """验证 query handler 会把身份字段注入 agent 请求上下文。"""
    runner = AgentRunner(agent_id="test-agent")
    runner.session = SimpleNamespace(
        load_session_state=AsyncMock(),
        mutate_session_state=AsyncMock(return_value={}),
        save_session_state=AsyncMock(),
    )
    setattr(runner, "_chat_manager", None)

    captured: dict[str, Any] = {}

    def fake_build_clients(
        _mcp,
        *,
        tenant_id=None,
        user_id=None,
        passthrough_headers=None,
        session_id=None,
        chat_id=None,
        trace_id=None,
        **kwargs,
    ):
        del tenant_id, user_id
        del _mcp, passthrough_headers, session_id, chat_id, trace_id, kwargs
        return []

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["request_context"] = kwargs["request_context"]
            self.memory = SimpleNamespace(content=[])

        async def register_mcp_clients(self):
            return

        def set_console_output_enabled(self, enabled=False):
            del enabled

        def rebuild_sys_prompt(self):
            return

        async def __call__(self, _msgs):
            return

    async def fake_stream_printing_messages(*, agents, coroutine_task):
        del agents
        await coroutine_task
        for item in ():
            yield item

    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _fake_agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        fake_build_clients,
    )
    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    monkeypatch.setattr(
        "swe.app.runner.runner.stream_printing_messages",
        fake_stream_printing_messages,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "swe.app.runner.context_references.build_context_reference_directives",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "swe.app.runner.skill_selection.build_skill_use_directives",
        lambda **kwargs: [],
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        trace_id="trace-1",
        channel_meta={
            "source_id": "cmb",
            "user_name": "Alice",
            "bbk_id": "3301",
        },
    )
    msgs = [SimpleNamespace(get_text_content=lambda: "hello")]

    results = []
    async for item in runner.query_handler(msgs, request=request):
        results.append(item)

    assert not results
    assert captured["request_context"]["source_id"] == "cmb"
    assert captured["request_context"]["user_name"] == "Alice"
    assert captured["request_context"]["bbk_id"] == "3301"
