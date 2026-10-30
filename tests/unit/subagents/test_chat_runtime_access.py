# -*- coding: utf-8 -*-
"""Chat ownership and retained Background SubAgent management."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.agents.react_agent import SWEAgent
from swe.agents.tools import subagent_background as tool_module
from swe.app.subagents import (
    AgentOwnedDefinitionRepository,
    BackgroundSubAgentStartBlocked,
    BackgroundSubAgentSupervisor,
    DelegationSpec,
    PerRunSubAgentRunStore,
    PermissionPolicy,
    SubAgentDefinition,
)
from swe.config.config import AgentProfileConfig


def _context(tmp_path, **overrides):
    return {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "session_id": "session-1",
        "chat_id": "chat-1",
        "_subagent_run_store_dir": str(tmp_path / "runs"),
        **overrides,
    }


async def _record(
    tmp_path,
    *,
    session_id="session-1",
    chat_id="chat-1",
    nickname=None,
):
    store = PerRunSubAgentRunStore(tmp_path / "runs")
    record = await store.create(
        DelegationSpec(
            parent_thread_id=session_id,
            parent_chat_id=chat_id,
            name="temporary",
            objective="Inspect",
        ),
        SubAgentDefinition(
            name="temporary",
            description="Test",
            instruction="Inspect",
            source="run_scoped",
            owner_scope="run:tenant-1:agent-1",
        ),
        PermissionPolicy.readonly(),
        nickname=nickname
        or ("研究员" if session_id == "session-1" else "审查员"),
    )
    return store, record


def _tools(tmp_path, supervisor, **context):
    return tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(tmp_path, **context),
        selected_expert_id=context.get("selected_expert_id"),
    )


def _payload(response):
    return json.loads(response.content[0]["text"])


def test_selected_expert_keeps_catalog_filter_without_wait_tool(tmp_path):
    tools = tool_module.create_background_subagent_tools(
        supervisor=SimpleNamespace(),
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(tmp_path),
        selected_expert_id="expert-1",
    )

    assert "wait_selected_expert" not in tools


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_subagent", "cancel_subagent"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": "other-session"},
        {"chat_id": "other-chat"},
        {"session_id": ""},
    ],
)
async def test_foreign_runs_are_rejected_before_supervisor(
    tmp_path,
    method,
    overrides,
):
    _, record = await _record(tmp_path)
    supervisor = SimpleNamespace(
        get=AsyncMock(return_value=None),
        cancel=AsyncMock(return_value=None),
    )
    response = await _tools(tmp_path, supervisor, **overrides)[method](
        record.nickname,
    )
    assert _payload(response)["status"] == "not_found"
    supervisor.get.assert_not_awaited()
    supervisor.cancel.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_subagent", "cancel_subagent"])
@pytest.mark.parametrize("selected_expert_id", [None, "expert-1"])
async def test_owned_runs_remain_manageable(
    tmp_path,
    method,
    selected_expert_id,
):
    store, record = await _record(tmp_path)
    record.nickname = "研究员"
    store._write(record)
    record = await store.cancel(record.run_id)
    supervisor = SimpleNamespace(
        get=AsyncMock(return_value=record),
        cancel=AsyncMock(return_value=record),
    )
    payload = _payload(
        await _tools(
            tmp_path,
            supervisor,
            selected_expert_id=selected_expert_id,
        )[method]("研究员"),
    )
    assert payload["nickname"] == "研究员"
    assert payload["status"] == "cancelled"
    assert "run_id" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_subagent", "cancel_subagent"])
async def test_unknown_nickname_is_not_found_without_internal_identity(
    tmp_path,
    method,
):
    supervisor = SimpleNamespace(get=AsyncMock(), cancel=AsyncMock())
    payload = _payload(await _tools(tmp_path, supervisor)[method]("未知"))

    assert payload == {"status": "not_found", "nickname": "未知"}
    assert "run_id" not in json.dumps(payload)
    supervisor.get.assert_not_awaited()
    supervisor.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_record_without_chat_id_uses_session_ownership(tmp_path):
    store, record = await _record(tmp_path, chat_id="")
    record = await store.cancel(record.run_id)
    supervisor = SimpleNamespace(get=AsyncMock(return_value=record))
    payload = _payload(
        await _tools(tmp_path, supervisor)["get_subagent"](
            record.nickname,
        ),
    )
    assert payload["nickname"] == record.nickname


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_expert_id", [None, "expert-1"])
async def test_wait_reads_only_owned_retained_terminal_records(
    tmp_path,
    selected_expert_id,
):
    store, own = await _record(tmp_path)
    own = await store.cancel(own.run_id)
    _, foreign = await _record(tmp_path, session_id="other-session")
    supervisor = SimpleNamespace(
        wait=AsyncMock(
            return_value=SimpleNamespace(
                active_runs=[foreign],
                terminal_runs=[],
                timed_out=True,
            ),
        ),
    )
    payload = _payload(
        await _tools(
            tmp_path,
            supervisor,
            selected_expert_id=selected_expert_id,
        )["wait_subagent"](timeout_ms=0),
    )
    assert payload["active_subagents"] == []
    assert [run["nickname"] for run in payload["terminal_subagents"]] == [
        own.nickname,
    ]
    assert payload["timed_out"] is False


@pytest.mark.asyncio
async def test_wait_without_session_never_calls_supervisor(tmp_path):
    supervisor = SimpleNamespace(wait=AsyncMock())
    payload = _payload(
        await _tools(
            tmp_path,
            supervisor,
            session_id="",
        )[
            "wait_subagent"
        ](timeout_ms=0),
    )
    assert payload == {
        "active_subagents": [],
        "terminal_subagents": [],
        "timed_out": False,
    }
    supervisor.wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_wait_remains_pending_until_every_target_is_terminal(
    tmp_path,
):
    store, first = await _record(tmp_path, nickname="研究员")
    _, second = await _record(tmp_path, nickname="审查员")
    await store.cancel(first.run_id)
    polled = asyncio.Event()

    async def reap(_scope, _run_id):
        polled.set()

    supervisor = SimpleNamespace(
        reap_run=AsyncMock(side_effect=reap),
        wait=AsyncMock(),
        cancel=AsyncMock(),
    )
    task = asyncio.create_task(
        _tools(tmp_path, supervisor)["wait_subagent"](
            nicknames=["研究员", "审查员"],
            timeout_ms=1000,
        ),
    )
    try:
        await asyncio.wait_for(polled.wait(), timeout=0.5)
        assert not task.done()
        await store.cancel(second.run_id)
        payload = _payload(await asyncio.wait_for(task, timeout=0.5))
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert payload["timed_out"] is False
    assert [item["nickname"] for item in payload["subagents"]] == [
        "研究员",
        "审查员",
    ]
    assert all(item["status"] == "cancelled" for item in payload["subagents"])
    assert "run_id" not in json.dumps(payload)
    assert {call.args[1] for call in supervisor.reap_run.await_args_list} == {
        first.run_id,
        second.run_id,
    }
    supervisor.wait.assert_not_awaited()
    supervisor.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_wait_timeout_returns_latest_targets_ignoring_unrelated_run(
    tmp_path,
):
    store, first = await _record(tmp_path, nickname="研究员")
    _, second = await _record(tmp_path, nickname="审查员")
    _, unrelated = await _record(tmp_path, nickname="旧任务")
    await store.cancel(unrelated.run_id)

    async def reap(_scope, run_id):
        if run_id == first.run_id:
            await store.cancel(run_id)

    supervisor = SimpleNamespace(
        reap_run=AsyncMock(side_effect=reap),
        wait=AsyncMock(),
        cancel=AsyncMock(),
    )
    payload = _payload(
        await _tools(tmp_path, supervisor)["wait_subagent"](
            nicknames=["研究员", "审查员"],
            timeout_ms=20,
        ),
    )

    assert payload["timed_out"] is True
    assert {
        item["nickname"]: item["status"] for item in payload["subagents"]
    } == {
        "研究员": "cancelled",
        "审查员": second.status,
    }
    assert "run_id" not in json.dumps(payload)
    assert {call.args[1] for call in supervisor.reap_run.await_args_list} == {
        first.run_id,
        second.run_id,
    }
    supervisor.wait.assert_not_awaited()
    supervisor.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_wait_all_terminal_returns_without_sleep(
    tmp_path,
    monkeypatch,
):
    store, record = await _record(tmp_path)
    await store.cancel(record.run_id)
    supervisor = SimpleNamespace(reap_run=AsyncMock(), wait=AsyncMock())
    sleep = AsyncMock(
        side_effect=AssertionError("terminal targets must not wait"),
    )
    monkeypatch.setattr(tool_module.asyncio, "sleep", sleep)

    payload = _payload(
        await _tools(tmp_path, supervisor)["wait_subagent"](
            nicknames=[record.nickname],
            timeout_ms=1000,
        ),
    )

    assert payload["timed_out"] is False
    assert payload["subagents"][0]["status"] == "cancelled"
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("nickname", ["未知", "外部任务"])
async def test_group_wait_resolves_every_nickname_before_reaping(
    tmp_path,
    nickname,
):
    await _record(tmp_path)
    await _record(tmp_path, chat_id="other-chat", nickname="外部任务")
    supervisor = SimpleNamespace(reap_run=AsyncMock(), wait=AsyncMock())

    payload = _payload(
        await _tools(tmp_path, supervisor)["wait_subagent"](
            nicknames=["研究员", nickname],
            timeout_ms=1000,
        ),
    )

    assert payload == {"status": "not_found", "nickname": nickname}
    supervisor.reap_run.assert_not_awaited()
    supervisor.wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_wait_rejects_explicit_empty_target_list(tmp_path):
    supervisor = SimpleNamespace(reap_run=AsyncMock(), wait=AsyncMock())

    payload = _payload(
        await _tools(tmp_path, supervisor)["wait_subagent"](nicknames=[]),
    )

    assert payload["status"] == "invalid_request"
    supervisor.reap_run.assert_not_awaited()
    supervisor.wait.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["resolve", "reap", "monitor"])
async def test_wait_failures_return_safe_response(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    await _record(tmp_path)
    failure = RuntimeError("private subagent-internal /private/worker.log")
    supervisor = SimpleNamespace(reap_run=AsyncMock(), wait=AsyncMock())
    if failure_stage == "resolve":
        monkeypatch.setattr(
            tool_module,
            "_resolve_chat_nickname",
            AsyncMock(side_effect=failure),
        )
    elif failure_stage == "reap":
        supervisor.reap_run.side_effect = failure
    else:
        supervisor.wait.side_effect = failure

    payload = _payload(
        await _tools(tmp_path, supervisor)["wait_subagent"](
            nicknames=None if failure_stage == "monitor" else ["研究员"],
            timeout_ms=0,
        ),
    )

    assert payload == {
        "status": "failed",
        "reason": "subagent_operation_failed",
    }


@pytest.mark.asyncio
async def test_group_wait_retains_missing_target_until_timeout(
    tmp_path,
    monkeypatch,
):
    _, record = await _record(tmp_path)
    monkeypatch.setattr(tool_module, "read_chat_run_record", lambda *_: None)
    supervisor = SimpleNamespace(reap_run=AsyncMock())

    payload = _payload(
        await _tools(tmp_path, supervisor)["wait_subagent"](
            nicknames=[record.nickname],
            timeout_ms=0,
        ),
    )

    assert payload["timed_out"] is True
    assert len(payload["subagents"]) == 1
    assert payload["subagents"][0]["nickname"] == record.nickname
    assert payload["subagents"][0]["status"] == record.status


@pytest.mark.asyncio
async def test_one_chat_wait_cannot_consume_another_chats_terminal(tmp_path):
    store, first = await _record(tmp_path)
    _, second = await _record(
        tmp_path,
        session_id="session-2",
        chat_id="chat-2",
    )
    first = await store.cancel(first.run_id)
    second = await store.cancel(second.run_id)
    supervisor = SimpleNamespace(
        wait=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    active_runs=[],
                    terminal_runs=[first, second],
                    timed_out=False,
                ),
                SimpleNamespace(
                    active_runs=[],
                    terminal_runs=[],
                    timed_out=False,
                ),
            ],
        ),
    )
    first_result = _payload(
        await _tools(tmp_path, supervisor)["wait_subagent"](timeout_ms=0),
    )
    second_result = _payload(
        await _tools(
            tmp_path,
            supervisor,
            session_id="session-2",
            chat_id="chat-2",
        )["wait_subagent"](timeout_ms=0),
    )
    assert [
        item["nickname"] for item in first_result["terminal_subagents"]
    ] == [
        first.nickname,
    ]
    assert [
        item["nickname"] for item in second_result["terminal_subagents"]
    ] == [
        second.nickname,
    ]


@pytest.mark.asyncio
async def test_real_supervisor_reaping_preserves_other_chat_terminal(tmp_path):
    from swe.app.subagents.supervisor import _ActiveRun

    store, first = await _record(tmp_path)
    _, second = await _record(
        tmp_path,
        session_id="session-2",
        chat_id="chat-2",
    )
    supervisor = BackgroundSubAgentSupervisor()
    scope = tool_module.build_background_subagent_scope(
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        request_context=_context(tmp_path),
    )
    for record in [first, second]:
        await store.mark_running(record.run_id, worker_pid=12345)
        await store.cancel(record.run_id)
        supervisor._active_for_scope(scope)[record.run_id] = _ActiveRun(
            scope=scope,
            process=SimpleNamespace(poll=lambda: 0, returncode=0),
            stderr_log_path=tmp_path / "stderr.log",
        )
    first_result = _payload(
        await _tools(tmp_path, supervisor)["wait_subagent"](timeout_ms=0),
    )
    assert not supervisor.has_active_runs(scope)
    second_result = _payload(
        await _tools(
            tmp_path,
            supervisor,
            session_id="session-2",
            chat_id="chat-2",
        )["wait_subagent"](timeout_ms=0),
    )
    assert [
        item["nickname"] for item in first_result["terminal_subagents"]
    ] == [
        first.nickname,
    ]
    assert [
        item["nickname"] for item in second_result["terminal_subagents"]
    ] == [
        second.nickname,
    ]


class _Toolkit:
    def __init__(self):
        self.tools = {"sentinel": object()}

    def register_tool_function(self, function, **_kwargs):
        self.tools[function.__name__] = function

    def remove_tool_function(self, name):
        self.tools.pop(name, None)


def _agent(tmp_path, **context):
    agent = object.__new__(SWEAgent)
    agent._workspace_dir = tmp_path
    agent._agent_config = AgentProfileConfig(id="agent-1", name="Main")
    agent._request_context = _context(tmp_path, **context)
    agent._normalize_registered_tool_functions = lambda *_args: None
    agent.toolkit = _Toolkit()
    return agent


@pytest.mark.asyncio
async def test_retained_own_run_registers_management_without_start(tmp_path):
    await _record(tmp_path)
    agent = _agent(tmp_path)
    agent._register_background_subagent_tools(
        agent.toolkit,
        "override",
        agent._request_context,
    )
    assert set(agent.toolkit.tools) == {
        "sentinel",
        "get_subagent",
        "wait_subagent",
        "cancel_subagent",
    }


@pytest.mark.asyncio
async def test_foreign_runs_do_not_register_management(tmp_path):
    await _record(tmp_path, session_id="other-session")
    agent = _agent(tmp_path)
    agent._register_background_subagent_tools(
        agent.toolkit,
        "override",
        agent._request_context,
    )
    assert set(agent.toolkit.tools) == {"sentinel"}


@pytest.mark.asyncio
async def test_refresh_rechecks_prepared_retained_run_presence(tmp_path):
    agent = _agent(tmp_path, _subagent_retained_runs_present=False)
    agent._register_background_subagent_tools(
        agent.toolkit,
        "override",
        agent._request_context,
    )
    assert set(agent.toolkit.tools) == {"sentinel"}
    await _record(tmp_path)
    agent.refresh_background_subagent_tools()
    assert set(agent.toolkit.tools) == {
        "sentinel",
        "get_subagent",
        "wait_subagent",
        "cancel_subagent",
    }


@pytest.mark.asyncio
async def test_missing_session_does_not_register_retained_management(tmp_path):
    await _record(tmp_path)
    agent = _agent(tmp_path, session_id="")
    agent._register_background_subagent_tools(
        agent.toolkit,
        "override",
        agent._request_context,
    )
    assert set(agent.toolkit.tools) == {"sentinel"}


@pytest.mark.asyncio
async def test_unknown_expert_does_not_start_or_load_parent_skills(tmp_path):
    captured = {}

    async def start(**kwargs):
        captured.update(kwargs)
        return BackgroundSubAgentStartBlocked(limit=1)

    tools = tool_module.create_background_subagent_tools(
        supervisor=SimpleNamespace(start=start),
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(tmp_path),
        effective_skill_names=["private-parent-skill"],
    )
    payload = _payload(
        await tools["start_subagent"](
            expert_name="temporary",
            objective="Inspect",
        ),
    )
    assert payload["status"] == "not_found"
    assert captured == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restricted",
    [
        {"agent_role": "subagent"},
        {"agent_role": "completion_judge"},
        {"goal_finalization": True},
    ],
)
async def test_retained_runs_do_not_bypass_worker_or_finalization_gate(
    tmp_path,
    restricted,
):
    await _record(tmp_path)
    agent = _agent(tmp_path, current_user_text="子代理", **restricted)
    agent._register_background_subagent_tools(
        agent.toolkit,
        "override",
        agent._request_context,
    )
    assert set(agent.toolkit.tools) == {"sentinel"}


@pytest.mark.asyncio
async def test_refresh_removes_start_but_preserves_unrelated_tools(tmp_path):
    await _record(tmp_path)
    agent = _agent(tmp_path, current_user_text="使用子代理")
    agent._register_background_subagent_tools(
        agent.toolkit,
        "override",
        agent._request_context,
    )
    sentinel = agent.toolkit.tools["sentinel"]
    assert "start_subagent" in agent.toolkit.tools
    agent._request_context["current_user_text"] = "继续查询"
    agent.refresh_background_subagent_tools()
    assert set(agent.toolkit.tools) == {
        "sentinel",
        "get_subagent",
        "wait_subagent",
        "cancel_subagent",
    }
    assert agent.toolkit.tools["sentinel"] is sentinel


def test_refresh_reloads_current_enabled_expert_catalog(tmp_path, monkeypatch):
    from swe.agents import react_agent as agent_module

    monkeypatch.setattr(
        agent_module,
        "_subagent_expert_discovery_enabled",
        lambda: True,
    )
    agent = _agent(tmp_path, current_user_text="使用子代理")
    agent._register_background_subagent_tools(
        agent.toolkit,
        "override",
        agent._request_context,
    )
    assert "live-reviewer" not in agent.toolkit.tools["start_subagent"].__doc__
    repository = AgentOwnedDefinitionRepository(
        tmp_path / "agents",
        owner_scope="tenant-1/agent-1",
    )
    package = repository.create(
        {
            "name": "live-reviewer",
            "description": "Review live",
            "instruction": "Review",
        },
    )
    repository.enable(
        package.definition_id,
        expected_revision=package.revision,
    )
    agent.refresh_background_subagent_tools()
    assert "live-reviewer" in agent.toolkit.tools["start_subagent"].__doc__


def test_management_only_factory_does_not_load_expert_catalog(
    tmp_path,
    monkeypatch,
):
    def forbidden(**_kwargs):
        raise AssertionError("management must not resolve expert capabilities")

    monkeypatch.setattr(tool_module, "_build_definition_catalog", forbidden)
    tools = tool_module.create_background_subagent_tools(
        supervisor=SimpleNamespace(),
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(tmp_path),
        start_enabled=False,
    )
    assert set(tools) == {"wait_subagent", "get_subagent", "cancel_subagent"}


@pytest.mark.asyncio
async def test_read_chat_run_by_nickname_requires_unique_exact_owned_match(
    tmp_path,
):
    from swe.app.subagents.run_access import read_chat_run_by_nickname

    store, foreign = await _record(tmp_path, chat_id="other-chat")
    foreign.nickname = "研究员"
    store._write(foreign)
    assert (
        read_chat_run_by_nickname(
            tmp_path / "runs",
            _context(tmp_path),
            "研究员",
        )
        is None
    )

    _, own = await _record(tmp_path)
    own.nickname = "研究员"
    store._write(own)
    assert (
        read_chat_run_by_nickname(
            tmp_path / "runs",
            _context(tmp_path),
            "研究员",
        ).run_id
        == own.run_id
    )
    assert (
        read_chat_run_by_nickname(
            tmp_path / "runs",
            _context(tmp_path),
            "研究",
        )
        is None
    )

    _, duplicate = await _record(tmp_path)
    duplicate.nickname = "研究员"
    store._write(duplicate)
    assert (
        read_chat_run_by_nickname(
            tmp_path / "runs",
            _context(tmp_path),
            "研究员",
        )
        is None
    )
