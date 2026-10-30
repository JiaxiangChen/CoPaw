# -*- coding: utf-8 -*-
"""Chat ownership and retained Background SubAgent management."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
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


async def _record(tmp_path, *, session_id="session-1", chat_id="chat-1"):
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
    )
    return store, record


def _tools(tmp_path, supervisor, **context):
    return tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(tmp_path, **context),
    )


def _payload(response):
    return json.loads(response.content[0]["text"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expired_request_deadline",
    [False, True],
)
async def test_selected_expert_wait_reports_confirmed_timeout_cancellation(
    tmp_path,
    expired_request_deadline,
):
    store, target = await _record(tmp_path)
    if not expired_request_deadline:
        target.created_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        target.effective_budget.timeout_ms = 0
        store._write(target)

    async def cancel(_scope, run_id):
        return await store.cancel(run_id)

    supervisor = SimpleNamespace(
        wait=AsyncMock(),
        cancel=AsyncMock(side_effect=cancel),
    )
    tools = tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(
            tmp_path,
            selected_expert_execution=True,
            selected_expert_run_id=target.run_id,
            _selected_expert_wait_authorized=True,
            **(
                {"_selected_expert_request_deadline": time.monotonic() - 1}
                if expired_request_deadline
                else {}
            ),
        ),
        selected_expert_id="expert-1",
    )
    result = _payload(await tools["wait_selected_expert"]())
    assert result["status"] == "cancelled"
    assert result["run_id"] == target.run_id


@pytest.mark.asyncio
async def test_selected_expert_wait_rejects_foreign_run(tmp_path):
    _, foreign = await _record(tmp_path, session_id="other-session")
    supervisor = SimpleNamespace(reap_run=AsyncMock(return_value=None))
    tools = tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(
            tmp_path,
            selected_expert_execution=True,
            selected_expert_run_id=foreign.run_id,
            _selected_expert_wait_authorized=True,
        ),
        selected_expert_id="expert-1",
    )
    result = _payload(
        await asyncio.wait_for(tools["wait_selected_expert"](), 2),
    )
    assert result == {"status": "unavailable"}
    supervisor.reap_run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_reap", [False, True])
async def test_selected_expert_wait_cleans_up_on_request_cancellation(
    tmp_path,
    cancel_during_reap,
):
    store, target = await _record(tmp_path)
    entered = asyncio.Event()

    async def reap_run(*_args, **_kwargs):
        entered.set()
        if cancel_during_reap:
            await asyncio.Event().wait()
        return None

    async def cancel(_scope, run_id):
        return await store.cancel(run_id)

    supervisor = SimpleNamespace(
        reap_run=reap_run,
        cancel_run=AsyncMock(side_effect=cancel),
        cancel=AsyncMock(),
    )
    tools = tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(
            tmp_path,
            selected_expert_execution=True,
            selected_expert_run_id=target.run_id,
            _selected_expert_wait_authorized=True,
        ),
        selected_expert_id="expert-1",
    )
    waiting = asyncio.create_task(tools["wait_selected_expert"]())
    await asyncio.wait_for(entered.wait(), 2)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    supervisor.cancel_run.assert_awaited_once()
    supervisor.cancel.assert_not_awaited()
    assert (await store.get(target.run_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_selected_expert_wait_recovers_from_transient_reap_error(
    tmp_path,
):
    store, target = await _record(tmp_path)
    attempts = 0

    async def reap_run(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary reap failure")
        await store.cancel(target.run_id)
        return None

    supervisor = SimpleNamespace(reap_run=reap_run, cancel=AsyncMock())
    tools = tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(
            tmp_path,
            selected_expert_execution=True,
            selected_expert_run_id=target.run_id,
            _selected_expert_wait_authorized=True,
        ),
        selected_expert_id="expert-1",
    )

    result = _payload(
        await asyncio.wait_for(tools["wait_selected_expert"](), 2),
    )
    assert result["status"] == "cancelled"
    assert attempts >= 2
    supervisor.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_expert_wait_logs_read_error_without_private_details(
    tmp_path,
    monkeypatch,
):
    _, target = await _record(tmp_path)
    warnings = []

    def capture_warning(*args):
        warnings.append(args)

    def broken_read(*_args, **_kwargs):
        raise OSError("private token /secret/path")

    monkeypatch.setattr(tool_module, "read_chat_run_record", broken_read)
    monkeypatch.setattr(tool_module.logger, "warning", capture_warning)
    supervisor = SimpleNamespace(reap_run=AsyncMock(), cancel=AsyncMock())
    tools = tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(
            tmp_path,
            selected_expert_execution=True,
            selected_expert_run_id=target.run_id,
            _selected_expert_wait_authorized=True,
        ),
        selected_expert_id="expert-1",
    )
    result = _payload(
        await asyncio.wait_for(tools["wait_selected_expert"](), 2),
    )
    assert result == {"status": "unavailable"}
    log_message = " ".join(str(value) for value in warnings[0])
    assert "selected_expert_wait_unconfirmed" in log_message
    assert "OSError" in log_message
    assert "private token" not in log_message
    assert "/secret/path" not in log_message
    supervisor.reap_run.assert_not_awaited()
    supervisor.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_expert_wait_tracks_only_the_selected_run(tmp_path):
    store, old = await _record(tmp_path)
    await store.cancel(old.run_id)
    _, target = await _record(tmp_path)
    supervisor = SimpleNamespace(reap_run=AsyncMock(return_value=None))
    tools = tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(
            tmp_path,
            selected_expert_execution=True,
            selected_expert_run_id=target.run_id,
            _selected_expert_wait_authorized=True,
        ),
        selected_expert_id="expert-1",
    )
    waiting = asyncio.create_task(tools["wait_selected_expert"]())
    await asyncio.sleep(0.1)
    assert not waiting.done()
    await store.cancel(target.run_id)
    result = _payload(await asyncio.wait_for(waiting, 2))
    assert result["run_id"] == target.run_id
    assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_selected_expert_wait_requires_internal_authorization(tmp_path):
    supervisor = SimpleNamespace(reap_run=AsyncMock(), cancel=AsyncMock())
    tools = tool_module.create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=_context(
            tmp_path,
            selected_expert_execution=True,
            selected_expert_run_id="subagent-1",
        ),
        selected_expert_id="expert-1",
    )

    assert _payload(await tools["wait_selected_expert"]()) == {
        "status": "unavailable",
    }
    supervisor.reap_run.assert_not_awaited()
    supervisor.cancel.assert_not_awaited()


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
        record.run_id,
    )
    assert _payload(response)["status"] == "not_found"
    supervisor.get.assert_not_awaited()
    supervisor.cancel.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_subagent", "cancel_subagent"])
async def test_owned_runs_remain_manageable(tmp_path, method):
    store, record = await _record(tmp_path)
    record = await store.cancel(record.run_id)
    supervisor = SimpleNamespace(
        get=AsyncMock(return_value=record),
        cancel=AsyncMock(return_value=record),
    )
    payload = _payload(
        await _tools(tmp_path, supervisor)[method](record.run_id),
    )
    assert payload["run_id"] == record.run_id
    assert payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_legacy_record_without_chat_id_uses_session_ownership(tmp_path):
    store, record = await _record(tmp_path, chat_id="")
    record = await store.cancel(record.run_id)
    supervisor = SimpleNamespace(get=AsyncMock(return_value=record))
    payload = _payload(
        await _tools(tmp_path, supervisor)["get_subagent"](
            record.run_id,
        ),
    )
    assert payload["run_id"] == record.run_id


@pytest.mark.asyncio
async def test_wait_reads_only_owned_retained_terminal_records(tmp_path):
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
    payload = _payload(await _tools(tmp_path, supervisor)["wait_subagent"](0))
    assert payload["active_runs"] == []
    assert [run["run_id"] for run in payload["terminal_runs"]] == [own.run_id]
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
        ](0),
    )
    assert payload == {
        "active_runs": [],
        "terminal_runs": [],
        "timed_out": False,
    }
    supervisor.wait.assert_not_awaited()


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
        await _tools(tmp_path, supervisor)["wait_subagent"](0),
    )
    second_result = _payload(
        await _tools(
            tmp_path,
            supervisor,
            session_id="session-2",
            chat_id="chat-2",
        )["wait_subagent"](0),
    )
    assert [item["run_id"] for item in first_result["terminal_runs"]] == [
        first.run_id,
    ]
    assert [item["run_id"] for item in second_result["terminal_runs"]] == [
        second.run_id,
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
        await _tools(tmp_path, supervisor)["wait_subagent"](0),
    )
    assert not supervisor.has_active_runs(scope)
    second_result = _payload(
        await _tools(
            tmp_path,
            supervisor,
            session_id="session-2",
            chat_id="chat-2",
        )["wait_subagent"](0),
    )
    assert [item["run_id"] for item in first_result["terminal_runs"]] == [
        first.run_id,
    ]
    assert [item["run_id"] for item in second_result["terminal_runs"]] == [
        second.run_id,
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
async def test_temporary_worker_does_not_load_parent_skills(tmp_path):
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
    await tools["start_subagent"](
        name="temporary",
        instruction="Inspect",
        objective="Inspect",
    )
    assert captured["definition"].source == "run_scoped"
    assert captured["effective_skill_names"] == []


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
