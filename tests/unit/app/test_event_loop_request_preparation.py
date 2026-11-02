# -*- coding: utf-8 -*-
"""Actual Query configuration callbacks must run outside the loop."""

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.runner.query_contracts import _QueryPreflight
from swe.config.context import current_tenant_id


@pytest.mark.asyncio
@pytest.mark.parametrize("has_records", [False, True])
async def test_retained_subagent_presence_is_prepared_outside_event_loop(
    tmp_path,
    monkeypatch,
    has_records,
):
    from swe.agents import react_agent as agent_module
    from swe.app.runner.runner import AgentRunner
    from swe.app.subagents import run_access

    observed = []
    runner = object.__new__(AgentRunner)
    runner.workspace_dir = tmp_path
    runner.agent_id = "agent"
    runner.session = SimpleNamespace()
    config = SimpleNamespace(id="agent", system_prompt_files=[])

    def read(*_args):
        observed.append(threading.get_ident())
        return [object()] if has_records else []

    def install(**kwargs):
        agent = object.__new__(agent_module.SWEAgent)
        agent._workspace_dir = tmp_path
        agent._agent_config = config
        agent._normalize_registered_tool_functions = lambda *_: None
        toolkit = SimpleNamespace(tools={})
        toolkit.register_tool_function = (
            lambda function, **_: toolkit.tools.update(
                {function.__name__: function},
            )
        )
        agent._register_background_subagent_tools(
            toolkit,
            "override",
            kwargs["prepared_request_context"],
        )
        return toolkit.tools

    monkeypatch.setattr(run_access, "read_chat_run_records", read)
    monkeypatch.setattr(agent_module, "read_chat_run_records", read)
    monkeypatch.setattr(
        runner,
        "_build_query_request_context",
        lambda **_: {
            "tenant_id": "tenant",
            "agent_id": "agent",
            "session_id": "session",
            "chat_id": "chat",
            "_subagent_run_store_dir": str(tmp_path / "runs"),
        },
    )
    monkeypatch.setattr(runner, "_apply_query_mode_context", lambda **_: None)
    monkeypatch.setattr(
        runner,
        "_apply_selected_expert_context",
        lambda **_: None,
    )
    monkeypatch.setattr(runner, "_source_tool_versions", lambda _: ())
    monkeypatch.setattr(runner, "_create_agent_for_query", install)
    tools = await runner._create_agent_for_query_async(
        agent_config=config,
        request=SimpleNamespace(),
        session_id="session",
        current_user_text="hello",
    )
    assert observed and all(
        thread != threading.get_ident() for thread in observed
    )
    assert set(tools) == (
        {"get_subagent", "wait_subagent", "cancel_subagent"}
        if has_records
        else set()
    )


@pytest.mark.asyncio
async def test_preflight_configuration_worker_preserves_tenant_context():
    from swe.app.runner.query_preflight import prepare_query_preflight

    observed = []
    configuration = object(), object()

    def load():
        observed.append((threading.get_ident(), current_tenant_id.get()))
        return configuration

    owner = SimpleNamespace(
        _resolve_pending_approval=AsyncMock(return_value=(None, False, None)),
        _load_query_preflight_config=load,
        _load_query_preflight_overlay=AsyncMock(return_value=object()),
        _query_preflight_hooks_enabled=lambda *_: False,
    )
    token = current_tenant_id.set("tenant-query")
    try:
        result = await prepare_query_preflight(
            owner,
            session_id="session",
            user_id="user",
            query="hello",
            request=SimpleNamespace(),
        )
    finally:
        current_tenant_id.reset(token)
    assert observed[0][0] != threading.get_ident()
    assert observed[0][1] == "tenant-query"
    assert result.agent_config is configuration[0]
    assert result.tenant_hooks is configuration[1]


@pytest.mark.asyncio
async def test_runtime_configuration_fallbacks_run_in_worker(tmp_path):
    from swe.app.runner.query_runtime import build_query_runtime_inputs

    observed = []

    def capture(result):
        observed.append(threading.get_ident())
        return result

    config = SimpleNamespace(id="agent", enable_workspace_skills=False)
    hooks = object()
    owner = SimpleNamespace(
        agent_id="agent",
        tenant_id="tenant",
        workspace_dir=tmp_path,
    )
    request = SimpleNamespace(session_id="session", user_id="user")
    kwargs = {
        "request": request,
        "msgs": [],
        "preflight": _QueryPreflight(),
        "build_environment_context": lambda **_: "context",
        "request_source_id": lambda _: "source",
        "request_user_name": lambda _: "user",
        "request_passthrough_headers": lambda _: {},
        "with_hook_context": lambda context, _: context,
        "merge_system_prompt_injections": lambda *_: [],
        "with_system_prompt_injections": lambda context, _: context,
        "request_system_prompt_injections": lambda _: [],
        "load_tenant_hooks": lambda _: capture(hooks),
        "load_agent_configuration": lambda *_args, **_kwargs: capture(config),
        "current_passthrough_headers": lambda: {},
    }
    result = await build_query_runtime_inputs(owner, **kwargs)
    assert len(observed) == 2
    assert all(thread != threading.get_ident() for thread in observed)
    assert result.agent_config is config
    assert result.tenant_hooks is hooks
    observed.clear()
    kwargs["preflight"] = _QueryPreflight(
        agent_config=config,
        tenant_hooks=hooks,
    )
    await build_query_runtime_inputs(owner, **kwargs)
    assert observed == []


@pytest.mark.asyncio
async def test_async_agent_prepares_prompt_and_source_catalog_before_install(
    tmp_path,
    monkeypatch,
):
    from swe.agents import prompt
    from swe.app.runner.runner import AgentRunner

    (tmp_path / "AGENTS.md").write_text("hello")
    observed = []
    runner = object.__new__(AgentRunner)
    runner.workspace_dir = tmp_path
    runner.agent_id = "agent"
    config = SimpleNamespace(
        system_prompt_files=["AGENTS.md"],
        heartbeat=SimpleNamespace(enabled=False),
    )
    versions = (object(),)

    def catalog(context):
        observed.append(("catalog", threading.get_ident()))
        return versions

    def install(**kwargs):
        observed.append(("install", threading.get_ident()))
        assert kwargs["source_tool_versions"] is versions
        return prompt.build_system_prompt_from_working_dir(
            tmp_path,
            ["AGENTS.md"],
            "agent",
            False,
        )

    original = prompt.read_text_file_with_encoding_fallback

    def read(path):
        observed.append(("read", threading.get_ident()))
        return original(path)

    monkeypatch.setattr(prompt, "read_text_file_with_encoding_fallback", read)
    monkeypatch.setattr(runner, "_source_tool_versions", catalog)
    monkeypatch.setattr(runner, "_create_agent_for_query", install)
    monkeypatch.setattr(runner, "_prepare_query_agent_context", lambda _: {})
    text = await runner._create_agent_for_query_async(
        agent_config=config,
        request=SimpleNamespace(source_id="source"),
    )
    assert "hello" in text
    assert [
        (kind, thread == threading.get_ident()) for kind, thread in observed
    ] == [
        ("read", False),
        ("catalog", False),
        ("install", True),
    ]


@pytest.mark.asyncio
async def test_selected_expert_preparation_runs_in_worker(
    tmp_path,
    monkeypatch,
):
    from swe.app.runner.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    runner.workspace_dir = tmp_path
    runner.agent_id = "agent"
    runner.session = SimpleNamespace()
    observed = []

    def prepare(**kwargs):
        observed.append(threading.get_ident())
        kwargs["request_context"]["selected_expert_id"] = "expert"

    def install(**kwargs):
        assert kwargs["prepared_request_context"] == {
            "selected_expert_id": "expert",
        }
        return object()

    monkeypatch.setattr(runner, "_build_query_request_context", lambda **_: {})
    monkeypatch.setattr(
        runner,
        "_apply_query_mode_context",
        lambda **_: "expert",
    )
    monkeypatch.setattr(runner, "_apply_selected_expert_context", prepare)
    monkeypatch.setattr(runner, "_source_tool_versions", lambda _: ())
    monkeypatch.setattr(runner, "_create_agent_for_query", install)
    await runner._create_agent_for_query_async(
        agent_config=SimpleNamespace(system_prompt_files=[]),
        request=SimpleNamespace(),
        chat=SimpleNamespace(id="chat"),
        session_id="session",
        user_id="user",
        channel="console",
        turn_id="turn",
        hook_overlay=object(),
        auth_token=None,
        current_user_text="objective",
        approved_tool_call=None,
    )
    assert observed and threading.get_ident() not in observed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["finalization", "completion_judge"])
async def test_goal_agent_prepares_frozen_model_before_loop_install(
    monkeypatch,
    kind,
):
    from swe.app.runner.runner import AgentRunner
    from swe.agents import model_factory

    runner = object.__new__(AgentRunner)
    runner.tenant_id = "tenant"
    runtime = SimpleNamespace(agent_config=SimpleNamespace(id="agent"))
    goal, slot, provider, snapshot = object(), object(), object(), object()
    seen = []

    def resolve(*args):
        seen.append(("resolve", threading.get_ident()))
        return slot, provider

    async def prepare(**kwargs):
        assert kwargs["model_slot_override"] is slot
        assert kwargs["model_provider_override"] is provider
        return snapshot

    def install(**kwargs):
        seen.append(("install", threading.get_ident()))
        assert kwargs["prepared_model_config"] is snapshot
        return snapshot

    monkeypatch.setattr(
        runner,
        "_resolve_goal_"
        + (
            "finalization_model"
            if kind == "finalization"
            else "completion_judge_model"
        ),
        resolve,
        raising=False,
    )
    monkeypatch.setattr(runner, "_create_goal_" + kind + "_agent", install)
    monkeypatch.setattr(model_factory, "prepare_model_config_async", prepare)
    result = await getattr(runner, "_create_goal_" + kind + "_agent_async")(
        runtime=runtime,
        goal=goal,
    )
    assert result is snapshot
    assert [
        (kind, thread == threading.get_ident()) for kind, thread in seen
    ] == [("resolve", False), ("install", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_cancelled_runtime_preparation_closes_connected_mcp(
    monkeypatch,
    cleanup_failure,
):
    import asyncio
    from swe.app.runner import query_runtime
    from swe.agents import model_factory

    entered, cleaning, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    clients = [object()]

    async def start(**kwargs):
        kwargs["mcp_clients"].extend(clients)
        return object(), None

    async def finalize(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def cleanup(actual):
        assert actual == clients
        cleaning.set()
        await release.wait()
        if cleanup_failure:
            raise RuntimeError("cleanup failed")

    owner = SimpleNamespace(
        tenant_id="tenant",
        _build_query_runtime_inputs=AsyncMock(
            return_value=SimpleNamespace(
                agent_config=SimpleNamespace(id="agent"),
            ),
        ),
        _start_query_runtime_resources=start,
        _finalize_query_runtime=finalize,
        _cleanup_query_runtime_mcp_clients=AsyncMock(side_effect=cleanup),
    )
    monkeypatch.setattr(
        query_runtime.ProviderManager,
        "get_or_create_instance",
        AsyncMock(return_value=SimpleNamespace(refresh_if_due=AsyncMock())),
    )
    monkeypatch.setattr(
        model_factory,
        "prepare_model_config_async",
        AsyncMock(return_value=object()),
    )
    task = asyncio.create_task(
        query_runtime.prepare_query_runtime(
            owner,
            request=object(),
            msgs=[],
            query="",
            preflight=object(),
        ),
    )
    await entered.wait()
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    owner._cleanup_query_runtime_mcp_clients.assert_awaited_once_with(clients)
