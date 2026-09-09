# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from agentscope.message import Msg

from swe.agents.hook_runtime.models import (
    CommandHookHandlerConfig,
    LoadedSkillHookSource,
    HookConfig,
    HookDecision,
    HookEventName,
    HookMatcherGroupConfig,
    HookSessionState,
    HookSessionOverlay,
    MonitoredSkillHookSource,
    SkillHookFileVersion,
    AdditionalContext,
    MergedHookResult,
    StopHookExecutionResult,
)
from swe.agents.tool_guard_mixin import PreToolUseTerminalStop
from swe.app.runner.runner import (
    AgentRunner,
    _build_and_connect_mcp_clients,
    _create_session_skill_detector,
    _QueryAttemptInput,
    _QueryAttemptState,
    _QueryRuntimeInputs,
    _hook_config_enabled,
    _QueryPreflight,
    _QueryRuntime,
    _RetryState,
    _RuntimeStartResult,
    _TurnPlan,
    _QueryTurnOutcome,
    _emit_runner_hook,
    _extract_assistant_response,
    _replace_assistant_response,
)
from swe.app.runner.turn_lifecycle import (
    _is_bufferable_assistant_text,
    _stream_standard_completion_lifecycle,
)
from swe.app.runner.session import SafeJSONSession
from swe.agents.skill_tool_registry import SkillToolRegistry
from swe.config.config import SuggestionMode
from swe.tracing.manager import (
    TraceContext,
    get_current_trace,
    set_current_trace,
)
from swe.tracing.models import TraceStatus


def _agent_config(hooks: HookConfig | None = None):
    return SimpleNamespace(
        id="test-agent",
        hooks=hooks or HookConfig(),
        mcp=None,
        running=SimpleNamespace(
            suggestions=SimpleNamespace(
                enabled=False,
                mode=SuggestionMode.DISABLED,
            ),
        ),
    )


class _FakeAgent:
    last_env_context = ""

    def __init__(self, **kwargs):
        self.memory = _FakeMemory()
        self.env_context = kwargs.get("env_context", "")
        _FakeAgent.last_env_context = self.env_context

    async def register_mcp_clients(self):
        return

    def set_console_output_enabled(self, enabled=False):
        del enabled

    def rebuild_sys_prompt(self):
        return

    async def __call__(self, turn_msgs):
        for msg in turn_msgs:
            self.memory.content.append((msg, []))
        reply = Msg(name="Friday", role="assistant", content="agent reply")
        self.memory.content.append((reply, []))
        return [reply]

    def state_dict(self):
        return {
            "memory": {
                "content": [
                    [msg.to_dict(), marks]
                    for msg, marks in self.memory.content
                ],
            },
        }

    def load_state_dict(self, state):
        memory_state = state.get("memory", {})
        restored = []
        for raw_msg, marks in memory_state.get("content", []) or []:
            restored.append(
                (
                    Msg(
                        name=raw_msg.get("name"),
                        role=raw_msg.get("role"),
                        content=raw_msg.get("content"),
                        metadata=raw_msg.get("metadata"),
                    ),
                    marks,
                ),
            )
        self.memory.content = restored


class _FakeMemory:
    def __init__(self):
        self.content = []

    async def add(self, msg, marks=None):
        if marks is None:
            normalized_marks = []
        elif isinstance(marks, list):
            normalized_marks = marks
        else:
            normalized_marks = [marks]
        self.content.append((msg, normalized_marks))


async def _fake_stream_printing_messages(*, agents, coroutine_task):
    del agents
    turn_msgs = await coroutine_task
    for msg in turn_msgs:
        yield msg, True


def _patch_normal_agent_path(monkeypatch):
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", _FakeAgent)
    monkeypatch.setattr(
        "swe.app.runner.runner.stream_printing_messages",
        _fake_stream_printing_messages,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **kwargs: "base context",
    )


def test_query_runtime_inputs_keep_request_scoped_values() -> None:
    from swe.app.runner.runner import _QueryRuntimeInputs

    inputs = _QueryRuntimeInputs(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        agent_config=SimpleNamespace(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        env_context="base context",
        selected_context_directives=["<SKILL-USE>"],
        auth_token="token",
        passthrough_headers={"cookie": "session=abc"},
    )

    assert inputs.session_id == "session-1"
    assert inputs.selected_context_directives == ["<SKILL-USE>"]
    assert inputs.passthrough_headers == {"cookie": "session=abc"}


@pytest.mark.asyncio
async def test_selected_skill_hooks_load_after_session_start_without_detector_use(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.app.runner.skill_selection import SkillUseDirective

    skill_root = tmp_path / "skills" / "sample"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "SKILL.md").write_text("# sample\n", encoding="utf-8")
    (skill_root / "scripts" / "stop.py").write_text(
        "print('ok')\n",
        encoding="utf-8",
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        '{"enabled": true, "events": {"Stop": [{"hooks": '
        '[{"id": "stop", "type": "command", "argv": ["python", "scripts/stop.py"]}]}]}}',
        encoding="utf-8",
    )
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    inputs = _QueryRuntimeInputs(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        agent_config=_agent_config(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        env_context="base",
        selected_context_directives=[],
        auth_token=None,
        passthrough_headers={},
        selected_skill_directives=[
            SkillUseDirective(
                name="sample",
                description="sample",
                path=skill_root / "SKILL.md",
            ),
        ],
    )
    monkeypatch.setattr(
        runner,
        "_get_or_create_chat",
        AsyncMock(return_value=SimpleNamespace(id="chat-1")),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        runner,
        "_generate_session_title_before_stream",
        AsyncMock(),
    )
    directive = inputs.selected_skill_directives[0]
    monkeypatch.setattr(
        "swe.app.runner.context_references.build_context_reference_directives",
        AsyncMock(return_value=[directive]),
    )
    monkeypatch.setattr(
        "swe.app.runner.skill_selection.build_skill_use_directives",
        lambda **kwargs: [],
    )
    session_start = AsyncMock(return_value=("base", None))
    monkeypatch.setattr(runner, "_emit_session_start_hook", session_start)

    resources, blocked = await runner._start_query_runtime_resources(
        request=SimpleNamespace(channel_meta={}),
        msgs=[Msg(name="user", role="user", content="hello")],
        inputs=inputs,
        mcp_clients=[],
    )

    assert blocked is None
    assert resources.env_context == "base"
    assert (
        session_start.await_args.kwargs["hook_overlay"].loaded_skill_sources
        == []
    )
    assert [
        source.source_id for source in inputs.hook_overlay.loaded_skill_sources
    ] == [
        "skill:sample",
    ]


def test_hook_config_enabled_accepts_loaded_skill_sources() -> None:
    state = HookSessionState(
        loaded_skill_sources=[
            LoadedSkillHookSource(
                source_id="skill:xlsx",
                skill_name="xlsx",
                skill_root="/workspace/skills/xlsx",
                source_path="/workspace/skills/xlsx/hooks/hooks.json",
                hook_config=HookConfig(
                    enabled=True,
                    events={
                        HookEventName.STOP: [
                            HookMatcherGroupConfig(
                                id="skill:xlsx:stop",
                                hooks=[
                                    CommandHookHandlerConfig(
                                        id="skill:xlsx:stop-hook",
                                        command="echo {}",
                                    ),
                                ],
                            ),
                        ],
                    },
                ),
            ),
        ],
    )

    assert _hook_config_enabled(HookConfig(), _agent_config(), state)


def test_hook_config_enabled_accepts_monitored_skill_sources() -> None:
    state = HookSessionState(
        monitored_skill_sources=[
            MonitoredSkillHookSource(
                source_id="skill:xlsx",
                skill_name="xlsx",
                skill_root="/workspace/skills/xlsx",
                source_path="/workspace/skills/xlsx/hooks/hooks.json",
                file_version=SkillHookFileVersion(exists=False),
            ),
        ],
    )

    assert _hook_config_enabled(HookConfig(), _agent_config(), state)


@pytest.mark.asyncio
async def test_create_session_skill_detector_loads_skill_hooks(
    tmp_path,
) -> None:
    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "check.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        """
        {
          "enabled": true,
          "events": {
            "Stop": [
              {
                "hooks": [
                  {
                    "id": "stop",
                    "type": "command",
                    "argv": ["python", "scripts/check.py"]
                  }
                ]
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    state = HookSessionState()

    def get_state() -> HookSessionState:
        return state

    def set_state(new_state: HookSessionState) -> None:
        nonlocal state
        state = new_state

    detector = _create_session_skill_detector(
        workspace_dir=tmp_path,
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="session-1",
        channel="console",
        source_id="source-1",
        enabled_skills=["xlsx"],
        get_hook_state=get_state,
        set_hook_state=set_state,
        approved_http_urls=set(),
    )

    await detector.start_skill(
        "xlsx",
        trigger_tool="user_message",
        trigger_reason="declared",
        load_hooks=True,
    )

    assert state.loaded_skill_sources[0].source_id == "skill:xlsx"
    handler = (
        state.loaded_skill_sources[0]
        .hook_config.events[HookEventName.STOP][0]
        .hooks[0]
    )
    assert handler.id == "skill:xlsx:stop"


@pytest.mark.asyncio
async def test_create_session_skill_detector_loads_http_skill_hooks_without_approvals(
    tmp_path,
) -> None:
    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "hooks" / "hooks.json").write_text(
        """
        {
          "enabled": true,
          "events": {
            "Stop": [
              {
                "hooks": [
                  {
                    "id": "notify",
                    "type": "http",
                    "url": "https://hooks.example.test/skill"
                  }
                ]
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    state = HookSessionState()

    def get_state() -> HookSessionState:
        return state

    def set_state(new_state: HookSessionState) -> None:
        nonlocal state
        state = new_state

    detector = _create_session_skill_detector(
        workspace_dir=tmp_path,
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="session-1",
        channel="console",
        source_id="source-1",
        enabled_skills=["xlsx"],
        get_hook_state=get_state,
        set_hook_state=set_state,
        approved_http_urls=set(),
    )

    await detector.start_skill(
        "xlsx",
        trigger_tool="user_message",
        trigger_reason="declared",
        load_hooks=True,
    )

    handler = (
        state.loaded_skill_sources[0]
        .hook_config.events[HookEventName.STOP][0]
        .hooks[0]
    )
    assert handler.id == "skill:xlsx:notify"
    assert handler.url == "https://hooks.example.test/skill"


@pytest.mark.asyncio
async def test_session_detector_skips_hooks_changed_after_snapshot(
    tmp_path,
) -> None:
    from swe.agents.skills_manager import _build_signature

    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    hooks_path = skill_root / "hooks" / "hooks.json"
    hooks_path.write_text('{"enabled": true, "events": {}}', encoding="utf-8")
    signature = _build_signature(skill_root)
    state = HookSessionState()

    detector = _create_session_skill_detector(
        workspace_dir=tmp_path,
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="session-1",
        channel="console",
        source_id="source-1",
        enabled_skills=["xlsx"],
        skill_metadata={"xlsx": {"description": "spreadsheet"}},
        skill_dirs={"xlsx": skill_root},
        skill_signatures={"xlsx": signature},
        get_hook_state=lambda: state,
        set_hook_state=lambda new_state: None,
        approved_http_urls=set(),
    )
    hooks_path.write_text('{"enabled": false, "events": {}}', encoding="utf-8")

    await detector.start_skill(
        "xlsx",
        trigger_tool="user_message",
        trigger_reason="declared",
        load_hooks=True,
    )

    assert not state.loaded_skill_sources


@pytest.mark.asyncio
async def test_skill_detector_monitors_initially_invalid_skill_hooks(
    tmp_path,
) -> None:
    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "hooks" / "hooks.json").write_text(
        "{",
        encoding="utf-8",
    )
    state = HookSessionState()

    def get_state() -> HookSessionState:
        return state

    def set_state(new_state: HookSessionState) -> None:
        nonlocal state
        state = new_state

    detector = _create_session_skill_detector(
        workspace_dir=tmp_path,
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="session-1",
        channel="console",
        source_id="source-1",
        enabled_skills=["xlsx"],
        get_hook_state=get_state,
        set_hook_state=set_state,
        approved_http_urls=set(),
    )

    await detector.start_skill(
        "xlsx",
        trigger_tool="user_message",
        trigger_reason="declared",
        load_hooks=True,
    )

    assert state.monitored_skill_sources[0].skill_name == "xlsx"
    assert state.loaded_skill_sources == []


@pytest.mark.asyncio
async def test_attach_session_skill_detector_reuses_trace_detector_and_tracing(
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    skill_tool_registry = SkillToolRegistry()
    fake_agent = SimpleNamespace(
        _request_context={},
        get_effective_skills=lambda: ["xlsx"],
        get_skill_tool_registry=lambda: skill_tool_registry,
    )
    runtime = _QueryRuntime(
        agent=fake_agent,
        agent_config=_agent_config(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        chat=None,
        session_skill_detector=None,
        mcp_clients=[],
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        pending_confirmed_skill_snapshots={},
    )
    request = SimpleNamespace(trace_id="trace-1", source_id="source-1")
    trace_ctx = TraceContext(
        trace_id="trace-1",
        user_id="user-1",
        session_id="session-1",
        channel="console",
        source_id="source-1",
    )
    trace_manager = AsyncMock()
    trace_manager.emit_skill_invocation = AsyncMock(
        return_value="skill-span-1",
    )
    set_current_trace(trace_ctx)

    with (
        patch("swe.app.runner.runner.has_trace_manager", return_value=True),
        patch(
            "swe.app.runner.runner.get_trace_manager",
            return_value=trace_manager,
        ),
    ):
        runner._attach_session_skill_detector(runtime=runtime, request=request)
        detector = runtime.session_skill_detector
        assert detector._registry is skill_tool_registry
        assert (
            detector
            is fake_agent._request_context["_skill_invocation_detector"]
        )
        assert get_current_trace().skill_detector is detector

        await detector.start_skill(
            "xlsx",
            trigger_tool="user_message",
            trigger_reason="declared",
        )

    trace_manager.emit_skill_invocation.assert_awaited_once()
    set_current_trace(None)


@pytest.mark.asyncio
async def test_stream_single_query_attempt_skips_duplicate_detector_setup(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    fake_agent = SimpleNamespace(
        setup_skill_detector=AsyncMock(),
        rebuild_sys_prompt=lambda: None,
    )
    runtime = _QueryRuntime(
        agent=fake_agent,
        agent_config=_agent_config(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        chat=None,
        session_skill_detector=object(),
        mcp_clients=[],
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        pending_confirmed_skill_snapshots={},
    )
    monkeypatch.setattr(
        runner,
        "_prepare_query_runtime",
        AsyncMock(return_value=_RuntimeStartResult(runtime=runtime)),
    )
    sentinel = RuntimeError("stop after detector guard")
    monkeypatch.setattr(
        runner,
        "get_state_loaded",
        AsyncMock(side_effect=sentinel),
    )

    attempt_input = _QueryAttemptInput(
        request=SimpleNamespace(),
        msgs=[],
        query=None,
        preflight=_QueryPreflight(),
        trace_id="trace-1",
    )
    attempt_state = _QueryAttemptState()
    retry_state = _RetryState()

    with pytest.raises(RuntimeError, match="stop after detector guard"):
        async for _ in runner._stream_single_query_attempt(
            attempt_input=attempt_input,
            outcome=_QueryTurnOutcome(),
            retry_state=retry_state,
            attempt_state=attempt_state,
        ):
            pass

    fake_agent.setup_skill_detector.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_single_query_attempt_binds_chat_id_to_runtime_claims(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.runtime_invocation_claims import build_runtime_invocation_claims

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    fake_agent = SimpleNamespace(
        setup_skill_detector=AsyncMock(),
        rebuild_sys_prompt=lambda: None,
    )
    runtime = _QueryRuntime(
        agent=fake_agent,
        agent_config=_agent_config(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        chat=SimpleNamespace(id="chat-uuid-1"),
        session_skill_detector=object(),
        mcp_clients=[],
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        pending_confirmed_skill_snapshots={},
    )
    monkeypatch.setattr(
        runner,
        "_prepare_query_runtime",
        AsyncMock(return_value=_RuntimeStartResult(runtime=runtime)),
    )

    async def assert_chat_claim(*args, **kwargs):
        del args, kwargs
        assert build_runtime_invocation_claims().chat_id == "chat-uuid-1"
        raise RuntimeError("stop after chat claim")

    monkeypatch.setattr(runner, "get_state_loaded", assert_chat_claim)

    with pytest.raises(RuntimeError, match="stop after chat claim"):
        async for _ in runner._stream_single_query_attempt(
            attempt_input=_QueryAttemptInput(
                request=SimpleNamespace(),
                msgs=[],
                query=None,
                preflight=_QueryPreflight(),
                trace_id=None,
            ),
            outcome=_QueryTurnOutcome(),
            retry_state=_RetryState(),
            attempt_state=_QueryAttemptState(),
        ):
            pass


@pytest.mark.asyncio
async def test_stream_single_query_attempt_rebinds_trace_detector_from_runtime(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    detector = object()
    fake_agent = SimpleNamespace(
        setup_skill_detector=AsyncMock(),
        rebuild_sys_prompt=lambda: None,
        get_effective_skills=lambda: ["xlsx"],
        _request_context={"_skill_invocation_detector": detector},
    )
    runtime = _QueryRuntime(
        agent=fake_agent,
        agent_config=_agent_config(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        chat=None,
        session_skill_detector=detector,
        mcp_clients=[],
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        pending_confirmed_skill_snapshots={},
    )
    monkeypatch.setattr(
        runner,
        "_prepare_query_runtime",
        AsyncMock(return_value=_RuntimeStartResult(runtime=runtime)),
    )
    trace_ctx = TraceContext(
        trace_id="trace-1",
        user_id="user-1",
        session_id="session-1",
        channel="console",
        source_id="source-1",
    )
    set_current_trace(trace_ctx)
    sentinel = RuntimeError("stop after trace rebind")
    monkeypatch.setattr(
        runner,
        "get_state_loaded",
        AsyncMock(side_effect=sentinel),
    )

    attempt_input = _QueryAttemptInput(
        request=SimpleNamespace(source_id="source-1"),
        msgs=[],
        query=None,
        preflight=_QueryPreflight(),
        trace_id="trace-1",
    )
    attempt_state = _QueryAttemptState()
    retry_state = _RetryState()

    with pytest.raises(RuntimeError, match="stop after trace rebind"):
        async for _ in runner._stream_single_query_attempt(
            attempt_input=attempt_input,
            outcome=_QueryTurnOutcome(),
            retry_state=retry_state,
            attempt_state=attempt_state,
        ):
            pass

    assert get_current_trace().skill_detector is detector
    assert get_current_trace().enabled_skills == ["xlsx"]
    fake_agent.setup_skill_detector.assert_not_awaited()
    set_current_trace(None)


@pytest.mark.asyncio
async def test_query_handler_user_prompt_hook_blocks_before_command_dispatch(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SimpleNamespace(
        get_session_state_dict=AsyncMock(return_value={}),
        mutate_session_state=AsyncMock(return_value={}),
    )
    setattr(runner, "_chat_manager", None)
    tenant_hooks = HookConfig(
        enabled=True,
        events={
            HookEventName.USER_PROMPT_SUBMIT: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="blocker",
                            command="unused",
                        ),
                    ],
                ),
            ],
        },
    )

    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: tenant_hooks,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            return_value=MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="blocked prompt",
            ),
        ),
    )
    command_path = AsyncMock()
    monkeypatch.setattr(
        "swe.app.runner.query_execution.admission.run_command_path",
        command_path,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="/history")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][1] is True
    assert "blocked prompt" in outputs[-1][0].get_text_content()
    command_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_handler_no_config_does_not_emit_hook(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SimpleNamespace(
        get_session_state_dict=AsyncMock(return_value={}),
        mutate_session_state=AsyncMock(return_value={}),
    )
    setattr(runner, "_chat_manager", None)

    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    emit_hook = AsyncMock(return_value=MergedHookResult())
    monkeypatch.setattr("swe.app.runner.runner._emit_runner_hook", emit_hook)

    async def fake_run_command_path(request, msgs, runner):
        yield Msg(name="Friday", role="assistant", content="command"), True

    monkeypatch.setattr(
        "swe.app.runner.query_execution.admission.run_command_path",
        fake_run_command_path,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="/history")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "command"
    emit_hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_handler_traces_conversation_commands(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SimpleNamespace(
        get_session_state_dict=AsyncMock(return_value={}),
        mutate_session_state=AsyncMock(return_value={}),
    )
    setattr(runner, "_chat_manager", None)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    runner._start_query_trace = AsyncMock(return_value="trace-command")
    runner._end_trace_if_needed = AsyncMock()

    async def fake_run_command_path(request, msgs, runner):
        del request, msgs, runner
        yield Msg(name="Friday", role="assistant", content="command"), True

    monkeypatch.setattr(
        "swe.app.runner.query_execution.admission.run_command_path",
        fake_run_command_path,
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="/compact")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "command"
    runner._start_query_trace.assert_awaited_once_with(request, msgs)
    runner._end_trace_if_needed.assert_awaited_once_with(
        "trace-command",
        TraceStatus.COMPLETED,
    )


@pytest.mark.asyncio
async def test_query_handler_loads_session_skill_hooks_for_media_message(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    emit_hook = AsyncMock(return_value=MergedHookResult())
    monkeypatch.setattr("swe.app.runner.runner._emit_runner_hook", emit_hook)

    persisted_overlay = HookSessionState(
        loaded_skill_sources=[
            LoadedSkillHookSource(
                source_id="skill:xlsx",
                skill_name="xlsx",
                skill_root=str(tmp_path / "skills" / "xlsx"),
                source_path=str(
                    tmp_path / "skills" / "xlsx" / "hooks" / "hooks.json",
                ),
                hook_config=HookConfig(
                    enabled=True,
                    events={
                        HookEventName.STOP: [
                            HookMatcherGroupConfig(
                                hooks=[
                                    CommandHookHandlerConfig(
                                        id="skill:xlsx:stop",
                                        command="unused",
                                    ),
                                ],
                            ),
                        ],
                    },
                ),
            ),
        ],
    )
    await runner.session.save_merged_state(
        session_id="session-1",
        user_id="user-1",
        state={
            "hook_overlay": persisted_overlay.model_dump(
                mode="json",
                by_alias=True,
            ),
        },
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [
        Msg(
            name="user",
            role="user",
            content=[{"type": "image", "url": "file:///tmp/image.png"}],
        ),
    ]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    emitted_events = [call.args[0] for call in emit_hook.await_args_list]
    assert HookEventName.USER_PROMPT_SUBMIT not in emitted_events
    assert emitted_events == [
        HookEventName.SESSION_START,
        HookEventName.STOP,
    ]


@pytest.mark.asyncio
async def test_user_prompt_hook_conversation_snapshot_uses_persisted_memory(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(
            HookConfig(
                enabled=True,
                events={
                    HookEventName.USER_PROMPT_SUBMIT: [
                        HookMatcherGroupConfig(
                            hooks=[
                                CommandHookHandlerConfig(
                                    id="prompt-policy",
                                    command="unused",
                                    includeConversationSnapshot=True,
                                ),
                            ],
                        ),
                    ],
                },
            ),
        ),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    seen_payloads: list[dict] = []

    async def fake_execute_handler_result(handler, context, *, workspace_dir):
        del workspace_dir
        seen_payloads.append(context.to_handler_payload())
        from swe.agents.hook_runtime.models import HookHandlerResult

        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler_result,
    )
    await runner.session.save_merged_state(
        session_id="session-1",
        user_id="user-1",
        state={
            "agent": {
                "memory": {
                    "content": [
                        [
                            Msg(
                                name="user",
                                role="user",
                                content="previous question",
                            ).to_dict(),
                            [],
                        ],
                        [
                            Msg(
                                name="Friday",
                                role="assistant",
                                content="previous answer",
                            ).to_dict(),
                            [],
                        ],
                    ],
                },
            },
        },
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="next question")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert seen_payloads[0]["hook_event_name"] == "UserPromptSubmit"
    assert seen_payloads[0]["conversation_snapshot"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "previous question"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "previous answer"}],
        },
    ]
    assert seen_payloads[0]["conversation_snapshot_meta"] == {
        "included_messages": 2,
        "omitted_messages": 0,
        "limit": 50,
        "reasoning_omitted": False,
        "media_content_omitted": False,
    }


@pytest.mark.asyncio
async def test_query_handler_injects_prompt_additional_context(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            side_effect=[
                MergedHookResult(
                    session_title="Hooked",
                    additional_context=[
                        AdditionalContext(
                            handler_id="prompt",
                            context="prompt context",
                        ),
                    ],
                ),
                MergedHookResult(
                    additional_context=[
                        AdditionalContext(
                            handler_id="start",
                            context="start context",
                        ),
                    ],
                ),
                MergedHookResult(),
                MergedHookResult(),
            ],
        ),
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert request.channel_meta["session_title"] == "Hooked"
    assert "prompt context" in _FakeAgent.last_env_context
    assert "start context" in _FakeAgent.last_env_context


@pytest.mark.asyncio
async def test_query_handler_session_start_block_yields_before_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    chat = SimpleNamespace(id="chat-1")
    chat_manager = SimpleNamespace(
        get_or_create_chat=AsyncMock(return_value=chat),
        update_chat=AsyncMock(return_value=chat),
    )
    setattr(runner, "_chat_manager", chat_manager)

    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def slow_cleanup(clients):
        assert clients == ["mcp-client"]
        cleanup_started.set()
        await cleanup_release.wait()

    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        lambda *_args, **_kwargs: ["mcp-client"],
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        slow_cleanup,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **kwargs: "base context",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            side_effect=[
                MergedHookResult(),
                MergedHookResult(
                    decision=HookDecision.BLOCK,
                    reason="session start blocked",
                ),
            ],
        ),
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]
    stream = runner.query_handler(msgs, request=request)
    next_item = asyncio.create_task(anext(stream))

    try:
        done, _pending = await asyncio.wait({next_item}, timeout=0.05)
        assert next_item in done
        msg, last = next_item.result()
        assert last is True
        assert msg.get_text_content() == "session start blocked"
        assert not cleanup_started.is_set()

        close_task = asyncio.create_task(stream.aclose())
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        chat_manager.update_chat.assert_awaited_once_with(chat)
        cleanup_release.set()
        await asyncio.wait_for(close_task, timeout=0.5)
    finally:
        cleanup_release.set()
        if not next_item.done():
            next_item.cancel()
            await asyncio.gather(next_item, return_exceptions=True)


def test_resolve_active_model_label_prefers_scoped_override(monkeypatch):
    from swe.app.crons import model_slot_context
    from swe.providers.models import ModelSlotConfig
    from swe.app.runner.runner import _resolve_active_model_label

    monkeypatch.setattr(
        model_slot_context,
        "get_current_model_slot_override",
        lambda: ModelSlotConfig(
            provider_id="openai",
            model="gpt-5.4",
        ),
    )
    provider_manager = SimpleNamespace(
        get_active_model=lambda: ModelSlotConfig(
            provider_id="anthropic",
            model="claude-3-7-sonnet",
        ),
    )
    monkeypatch.setattr(
        "swe.providers.provider_manager.ProviderManager.get_instance",
        lambda _tenant_id: provider_manager,
    )

    assert _resolve_active_model_label("tenant-a") == "openai/gpt-5.4"


@pytest.mark.asyncio
async def test_build_and_connect_mcp_clients_logs_duration(
    monkeypatch,
) -> None:
    import swe.app.runner.runner as runner_module

    class FakeClient:
        async def connect(self, timeout: float = 30.0):
            del timeout
            return None

    fake_client = FakeClient()
    monkeypatch.setattr(
        "swe.app.runner.runner._create_mcp_client_with_headers",
        AsyncMock(return_value=fake_client),
    )

    config = SimpleNamespace(
        clients={
            "weather": SimpleNamespace(enabled=True),
        },
    )
    with patch.object(runner_module.logger, "debug") as mock_debug:
        clients = await _build_and_connect_mcp_clients(config)

    assert clients == [fake_client]
    assert any(
        call.args
        and "mcp_client_connect_duration_ms=" in call.args[0]
        and call.args[2] == 1
        for call in mock_debug.call_args_list
    )


@pytest.mark.asyncio
async def test_build_and_connect_mcp_clients_passes_explicit_connect_timeout(
    monkeypatch,
) -> None:
    import swe.app.runner.runner as runner_module

    captured: dict[str, float] = {}

    class FakeClient:
        async def connect(self, timeout: float = 30.0):
            captured["timeout"] = timeout

    fake_client = FakeClient()
    monkeypatch.setattr(
        "swe.app.runner.runner._create_mcp_client_with_headers",
        AsyncMock(return_value=fake_client),
    )

    config = SimpleNamespace(
        clients={
            "weather": SimpleNamespace(enabled=True),
        },
    )

    clients = await _build_and_connect_mcp_clients(config)

    assert clients == [fake_client]
    assert captured["timeout"] == runner_module._MCP_CONNECT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_build_lazy_mcp_clients_defers_client_creation_until_discovery(
    monkeypatch,
) -> None:
    import swe.app.runner.runner as runner_module

    tool = SimpleNamespace(
        name="weather",
        description="Return weather.",
        inputSchema={"type": "object", "properties": {}},
    )
    created_client = SimpleNamespace(
        connect=AsyncMock(),
        list_tools=AsyncMock(return_value=[tool]),
        close=AsyncMock(),
    )
    create_client = AsyncMock(return_value=created_client)
    monkeypatch.setattr(
        runner_module,
        "_create_mcp_client_with_headers",
        create_client,
    )

    clients = runner_module._build_lazy_mcp_clients(
        SimpleNamespace(
            clients={
                "weather": SimpleNamespace(
                    name="weather-mcp",
                    enabled=True,
                    model_dump=lambda **_kwargs: {"name": "weather-mcp"},
                ),
            },
        ),
        tenant_id="tenant-a",
        user_id="user-a",
        passthrough_headers={"Authorization": "Bearer secret"},
        session_id="session-a",
        chat_id="chat-a",
        trace_id="trace-a",
    )

    assert len(clients) == 1
    assert create_client.await_count == 0

    assert [tool.name for tool in await clients[0].list_tools()] == ["weather"]

    create_client.assert_awaited_once()
    assert create_client.await_args.args[1] == {
        "Authorization": "Bearer secret",
    }
    assert create_client.await_args.kwargs == {
        "session_id": "session-a",
        "chat_id": "chat-a",
        "trace_id": "trace-a",
    }
    created_client.connect.assert_awaited_once_with(
        timeout=runner_module._MCP_CONNECT_TIMEOUT_SECONDS,
    )
    created_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_lazy_mcp_clients_forwards_all_user_headers_to_marketplace(
    monkeypatch,
) -> None:
    import swe.app.runner.runner as runner_module
    from swe.config.config import MCPClientConfig, MCPConfig

    tool = SimpleNamespace(name="search", description="", inputSchema={})
    created_client = SimpleNamespace(
        connect=AsyncMock(),
        list_tools=AsyncMock(return_value=[tool]),
        close=AsyncMock(),
    )
    create_client = AsyncMock(return_value=created_client)
    monkeypatch.setattr(
        runner_module,
        "_create_mcp_client_with_headers",
        create_client,
    )

    clients = runner_module._build_lazy_mcp_clients(
        MCPConfig(
            clients={
                "market": MCPClientConfig(
                    name="market",
                    command="node",
                    source="marketplace:mcp-1",
                ),
            },
        ),
        tenant_id="tenant-a",
        user_id="user-a",
        passthrough_headers={
            "Authorization": "Bearer user-token",
            "cookie": "sid=abc",
            "X-B3-Traceid": "trace-1",
        },
    )

    await clients[0].list_tools()

    assert create_client.await_args.args[1] == {
        "Authorization": "Bearer user-token",
        "cookie": "sid=abc",
        "X-B3-Traceid": "trace-1",
    }


def test_build_lazy_mcp_clients_ignores_filtered_sandbox_auth_in_discovery_key():
    import swe.app.runner.runner as runner_module
    from swe.config.config import MCPClientConfig, MCPConfig

    def build_client(auth_header: str):
        return runner_module._build_lazy_mcp_clients(
            MCPConfig(
                clients={
                    "market": MCPClientConfig(
                        name="market",
                        transport="streamable_http",
                        url=(
                            "https://mcpmarket-sandbox.platform.cmbchina.cn"
                            "/mcp"
                        ),
                        source="marketplace:mcp-1",
                    ),
                },
            ),
            tenant_id="tenant-a",
            user_id="user-a",
            passthrough_headers={
                "Authorization": auth_header,
                "cookie": "sid=abc",
            },
        )[0]

    client_a = build_client("Bearer token-a")
    client_b = build_client("Bearer token-b")

    assert client_a._discovery_key == client_b._discovery_key


@pytest.mark.asyncio
async def test_prepare_query_runtime_logs_agent_build_duration(
    monkeypatch,
    tmp_path,
) -> None:
    import swe.app.runner.runner as runner_module

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    chat = SimpleNamespace(id="chat-1")
    setattr(
        runner,
        "_chat_manager",
        SimpleNamespace(
            get_or_create_chat=AsyncMock(return_value=chat),
        ),
    )
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(return_value=MergedHookResult()),
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    with patch.object(runner_module.logger, "debug") as mock_debug:
        result = await runner._prepare_query_runtime(
            request=request,
            msgs=msgs,
            query="hello",
            preflight=_QueryPreflight(),
        )

    assert result.runtime is not None
    assert any(
        call.args
        and "swe_agent_build_duration_ms=" in call.args[0]
        and call.args[2] == "test-agent"
        for call in mock_debug.call_args_list
    )


@pytest.mark.asyncio
async def test_prepare_query_runtime_resolves_chat_before_connecting_mcp(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    chat = SimpleNamespace(id="chat-uuid-1")
    events: list[object] = []

    def get_or_create_chat(*args, **kwargs):
        del args, kwargs
        events.append("chat")
        return chat

    async def build_context_reference_directives(*args, **kwargs):
        del args, kwargs
        from swe.runtime_invocation_claims import (
            build_runtime_invocation_claims,
        )

        events.append(("discovery", build_runtime_invocation_claims().chat_id))
        return []

    setattr(
        runner,
        "_chat_manager",
        SimpleNamespace(
            get_or_create_chat=AsyncMock(side_effect=get_or_create_chat),
        ),
    )
    _patch_normal_agent_path(monkeypatch)

    def build_clients(*args, **kwargs):
        del args
        events.append(("mcp", kwargs.get("chat_id")))
        return []

    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        build_clients,
    )
    monkeypatch.setattr(
        "swe.app.runner.context_references.build_context_reference_directives",
        build_context_reference_directives,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(return_value=MergedHookResult()),
    )

    result = await runner._prepare_query_runtime(
        request=SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            channel_meta={},
        ),
        msgs=[Msg(name="user", role="user", content="hello")],
        query="hello",
        preflight=_QueryPreflight(),
    )

    assert result.runtime is not None
    assert events == [
        "chat",
        ("discovery", "chat-uuid-1"),
        ("mcp", "chat-uuid-1"),
    ]


@pytest.mark.asyncio
async def test_prepare_query_runtime_returns_blocked_start_result(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    chat = SimpleNamespace(id="chat-1")
    setattr(
        runner,
        "_chat_manager",
        SimpleNamespace(get_or_create_chat=AsyncMock(return_value=chat)),
    )
    _patch_normal_agent_path(monkeypatch)
    enabled_hooks = HookConfig(enabled=True)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(enabled_hooks),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: enabled_hooks,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            return_value=MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="blocked",
            ),
        ),
    )

    result = await runner._prepare_query_runtime(
        request=SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            channel_meta={},
        ),
        msgs=[Msg(name="user", role="user", content="hello")],
        query="hello",
        preflight=_QueryPreflight(),
    )

    assert result.runtime is None
    assert result.block_response is not None
    assert result.blocked_chat is chat
    assert result.blocked_mcp_clients == []
    assert result.blocked_session_id == "session-1"


@pytest.mark.asyncio
async def test_prepare_query_runtime_does_not_restore_skill_continuation_from_snapshot(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    chat = SimpleNamespace(id="chat-1")
    setattr(
        runner,
        "_chat_manager",
        SimpleNamespace(
            get_or_create_chat=AsyncMock(return_value=chat),
        ),
    )
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        _FakeAgent,
        "get_effective_skills",
        lambda self: ["fill-metadata", "xlsx"],
        raising=False,
    )
    monkeypatch.setattr(
        _FakeAgent,
        "get_runtime_skills",
        lambda self: ["fill-metadata", "xlsx"],
        raising=False,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(return_value=MergedHookResult()),
    )

    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "fill-metadata": {
                "skill_name": "fill-metadata",
                "resolved_skill_dir": str(
                    tmp_path / "skills" / "fill-metadata",
                ),
                "freshness_token": "v1",
                "confirmed_at": 2.0,
            },
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(tmp_path / "skills" / "xlsx"),
                "freshness_token": "v1",
                "confirmed_at": 1.0,
            },
        },
    )

    restored: list[tuple[str, bool]] = []

    class FakeDetector:
        def set_tracing_context(self, *args, **kwargs):
            return None

        def detect_from_user_message(self, _message):
            return None, 0.0

        def restore_confirmed_skill(
            self,
            skill_name: str,
            allow_one_shot_continuation: bool = True,
        ):
            restored.append((skill_name, allow_one_shot_continuation))

    monkeypatch.setattr(
        "swe.app.runner.runner._create_session_skill_detector",
        lambda **kwargs: FakeDetector(),
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="继续处理")]

    result = await runner._prepare_query_runtime(
        request=request,
        msgs=msgs,
        query="继续处理",
        preflight=_QueryPreflight(),
    )

    assert result.runtime is not None
    assert restored == []


@pytest.mark.asyncio
async def test_query_handler_stop_allow_completes(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    emit_hook = AsyncMock()

    async def fake_emit_runner_hook(event_name, **kwargs):
        await emit_hook(event_name, **kwargs)
        if event_name == HookEventName.STOP:
            assert kwargs["assistant_response"] == "agent reply"
            return MergedHookResult(
                decision=HookDecision.ALLOW,
                reason="completion approved",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert [item[0].get_text_content() for item in outputs] == [
        "agent reply",
    ]
    assert [call.args[0] for call in emit_hook.await_args_list] == [
        HookEventName.USER_PROMPT_SUBMIT,
        HookEventName.SESSION_START,
        HookEventName.STOP,
    ]


@pytest.mark.asyncio
async def test_query_handler_stop_transformer_buffers_and_releases_final_text(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    transformer_config = HookConfig(
        enabled=True,
        events={
            HookEventName.STOP: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="format",
                            command="echo",
                            outputTransform=True,
                        ),
                    ],
                ),
            ],
        },
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(transformer_config),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )

    async def fake_emit_runner_hook(event_name, **kwargs):
        del event_name, kwargs
        return MergedHookResult()

    async def fake_stop_finalization(**kwargs):
        assert kwargs["assistant_response"] == "agent reply"
        return StopHookExecutionResult(
            final_response="final reply",
            validation_result=MergedHookResult(decision=HookDecision.ALLOW),
        )

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_stop_finalization",
        fake_stop_finalization,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="hello")],
            request=SimpleNamespace(
                session_id="session-1",
                user_id="user-1",
                channel="console",
                channel_meta={},
            ),
        )
    ]

    assert [item[0].get_text_content() for item in outputs] == ["final reply"]


def test_agent_profile_owns_stop_transform_budget() -> None:
    configured = SimpleNamespace(
        running=SimpleNamespace(
            hook_runtime=SimpleNamespace(max_stop_transform_seconds=12.5),
        ),
    )

    assert AgentRunner._resolve_max_stop_transform_seconds(configured) == 12.5
    assert (
        AgentRunner._resolve_max_stop_transform_seconds(
            SimpleNamespace(running=SimpleNamespace()),
        )
        == 30.0
    )


def test_stop_transformer_handles_text_block_assistant_response() -> None:
    agent = _FakeAgent()
    candidate = "candidate block text"
    msg = Msg(
        name="Friday",
        role="assistant",
        content=[{"type": "text", "text": candidate}],
    )
    agent.memory.content.append((msg, []))

    assert _is_bufferable_assistant_text(msg) is True
    assert _extract_assistant_response(agent) == candidate
    assert _replace_assistant_response(agent, "final block text") is True
    assert msg.content == [{"type": "text", "text": "final block text"}]


def test_stop_transformer_projects_thinking_and_text_response() -> None:
    agent = _FakeAgent()
    msg = Msg(
        name="Friday",
        role="assistant",
        content=[
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "visible"},
        ],
    )
    agent.memory.content.append((msg, []))

    assert _is_bufferable_assistant_text(msg) is True
    assert _extract_assistant_response(agent) == "visible"
    assert _replace_assistant_response(agent, "final") is True
    assert msg.content == [
        {"type": "thinking", "thinking": "hidden"},
        {"type": "text", "text": "final"},
    ]


def test_stop_transformer_extract_ignores_live_assistant_events() -> None:
    agent = _FakeAgent()
    candidate = Msg(name="Friday", role="assistant", content="final candidate")
    live_progress = Msg(
        name="Friday",
        role="assistant",
        content="tool progress",
        metadata={"event_type": "tool_progress"},
    )
    agent.memory.content.extend([(candidate, []), (live_progress, [])])

    assert _extract_assistant_response(agent) == "final candidate"


@pytest.mark.parametrize("event_type", ["tool_progress", "approval_request"])
def test_stop_transformer_keeps_live_assistant_events_unbuffered(
    event_type: str,
) -> None:
    msg = Msg(
        name="Friday",
        role="assistant",
        content="live update",
        metadata={"event_type": event_type},
    )

    assert _is_bufferable_assistant_text(msg) is False


def test_stop_transformer_projects_text_with_passive_media() -> None:
    agent = _FakeAgent()
    msg = Msg(
        name="Friday",
        role="assistant",
        content=[
            {"type": "text", "text": "caption"},
            {"type": "image", "url": "https://example.com/image.png"},
        ],
    )
    agent.memory.content.append((msg, []))

    assert _is_bufferable_assistant_text(msg) is True
    assert _extract_assistant_response(agent) == "caption"
    assert _replace_assistant_response(agent, "final caption") is True
    assert msg.content == [
        {"type": "text", "text": "final caption"},
        {"type": "image", "url": "https://example.com/image.png"},
    ]


def test_stop_transformer_rejects_tool_use_message() -> None:
    agent = _FakeAgent()
    msg = Msg(
        name="Friday",
        role="assistant",
        content=[
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "will use a tool"},
            {"type": "tool_use", "name": "read_file", "input": {}},
        ],
    )
    agent.memory.content.append((msg, []))

    assert _is_bufferable_assistant_text(msg) is False
    assert _extract_assistant_response(agent) == ""
    assert _replace_assistant_response(agent, "final") is False


@pytest.mark.asyncio
async def test_strict_stop_release_emits_only_the_final_buffered_message() -> (
    None
):
    first = Msg(name="Friday", role="assistant", content="raw first")
    second = Msg(name="Friday", role="assistant", content="raw final")
    outcome = _QueryTurnOutcome(
        assistant_response="transformed final",
        stop_output_buffer_required=True,
        buffered_assistant_messages=[first, second],
    )

    class Owner:
        def _requires_stop_output_buffer(self, **kwargs):
            del kwargs
            return True

        async def _stream_agent_turns(self, **kwargs):
            if kwargs.get("never_stream"):
                yield None

        async def _emit_stop_hook_if_needed(self, **kwargs):
            del kwargs
            return MergedHookResult(decision=HookDecision.ALLOW)

    outputs = [
        item
        async for item in _stream_standard_completion_lifecycle(
            Owner(),
            request=SimpleNamespace(),
            runtime=SimpleNamespace(),
            plan=SimpleNamespace(),
            outcome=outcome,
        )
    ]

    assert [message.get_text_content() for message, _last in outputs] == [
        "transformed final",
    ]


@pytest.mark.asyncio
async def test_transformer_failure_does_not_return_handler_response_text(
    monkeypatch,
    tmp_path,
) -> None:
    candidate = "private candidate"
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    agent = _FakeAgent()
    agent.memory.content.append(
        (Msg(name="Friday", role="assistant", content=candidate), []),
    )
    transformer_config = HookConfig(
        enabled=True,
        events={
            HookEventName.STOP: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="format",
                            command="echo",
                            outputTransform=True,
                        ),
                    ],
                ),
            ],
        },
    )

    async def fake_finalization(**kwargs):
        del kwargs
        return StopHookExecutionResult(
            final_response=candidate,
            transformation_failed=True,
            transformation_failure_reason=candidate,
        )

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_stop_finalization",
        fake_finalization,
    )
    result = await runner._emit_stop_hook_if_needed(
        request=SimpleNamespace(),
        runtime=SimpleNamespace(
            tenant_hooks=HookConfig(),
            agent_config=_agent_config(transformer_config),
            hook_overlay=HookSessionOverlay(),
            agent=agent,
        ),
        plan=SimpleNamespace(original_user_message="hello"),
        outcome=_QueryTurnOutcome(
            assistant_response=candidate,
            stop_output_buffer_required=True,
            assistant_memory_start=0,
        ),
    )

    assert result is not None
    assert (
        result.blocking_failure_reason == "Stop output transformation failed"
    )
    assert candidate not in result.blocking_failure_reason


def test_replacement_skips_trailing_live_assistant_event() -> None:
    agent = _FakeAgent()
    candidate = Msg(name="Friday", role="assistant", content="candidate")
    live_progress = Msg(
        name="Friday",
        role="assistant",
        content="tool progress",
        metadata={"event_type": "tool_progress"},
    )
    agent.memory.content.extend([(candidate, []), (live_progress, [])])

    assert _replace_assistant_response(agent, "final") is True
    assert candidate.content == "final"
    assert live_progress.content == "tool progress"
    assert _extract_assistant_response(agent) == "final"


@pytest.mark.asyncio
async def test_query_handler_stop_block_continues_until_allow(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal stop_calls
        if event_name == HookEventName.STOP:
            stop_calls += 1
            if stop_calls == 1:
                return MergedHookResult(
                    decision=HookDecision.BLOCK,
                    reason="test tests before stopping",
                )
            return MergedHookResult(
                decision=HookDecision.ALLOW,
                reason="completion approved",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert [item[0].get_text_content() for item in outputs] == [
        "agent reply",
        "agent reply",
    ]
    assert stop_calls == 2


@pytest.mark.asyncio
async def test_query_handler_stop_blocking_failure_finishes_without_follow_up(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal stop_calls
        if event_name == HookEventName.STOP:
            stop_calls += 1
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="policy requires review",
                has_blocking_failure=True,
                blocking_failure_reason="audit service failed",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="hello")],
            request=request,
        )
    ]

    assert stop_calls == 1
    assert len(outputs) == 2
    assert "任务未完成" in outputs[-1][0].get_text_content()
    assert "audit service failed" in outputs[-1][0].get_text_content()


@pytest.mark.asyncio
async def test_query_handler_skips_stop_when_turn_has_no_new_assistant_response(
    monkeypatch,
    tmp_path,
) -> None:
    class NoResponseAgent(_FakeAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.memory.content.append(
                (
                    Msg(name="Friday", role="assistant", content="old reply"),
                    [],
                ),
            )

        async def __call__(self, turn_msgs):
            for msg in turn_msgs:
                self.memory.content.append((msg, []))
            return []

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", NoResponseAgent)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal stop_calls
        if event_name == HookEventName.STOP:
            stop_calls += 1
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="hello")],
            request=SimpleNamespace(
                session_id="session-1",
                user_id="user-1",
                channel="console",
                channel_meta={},
            ),
        )
    ]

    assert outputs == []
    assert stop_calls == 0


@pytest.mark.asyncio
async def test_query_handler_stop_block_exhausts_default_budget(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal stop_calls
        if event_name == HookEventName.STOP:
            stop_calls += 1
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason=f"reason-{stop_calls}",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]
    output_texts = [item[0].get_text_content() for item in outputs]

    assert output_texts[:3] == ["agent reply", "agent reply", "agent reply"]
    assert "任务未完成" in output_texts[-1]
    assert "reason-3" in output_texts[-1]
    assert stop_calls == 3


@pytest.mark.asyncio
async def test_query_handler_stop_budget_exhaustion_finalizes_trace(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    runner._generate_backend_suggestions_if_needed = AsyncMock()
    runner._index_model_output_if_needed = AsyncMock()
    runner._end_trace_if_needed = AsyncMock()

    async def fake_emit_runner_hook(event_name, **kwargs):
        if event_name == HookEventName.STOP:
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="still incomplete",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert "任务未完成" in outputs[-1][0].get_text_content()
    runner._generate_backend_suggestions_if_needed.assert_not_awaited()
    runner._index_model_output_if_needed.assert_awaited_once()
    runner._end_trace_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_handler_stop_budget_exhaustion_persists_notice(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )

    async def fake_emit_runner_hook(event_name, **kwargs):
        if event_name == HookEventName.STOP:
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="still incomplete",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]
    notice_text = outputs[-1][0].get_text_content()
    stored_state = await runner.session.get_session_state_dict(
        session_id="session-1",
        user_id="user-1",
    )
    stored_content = stored_state["agent"]["memory"]["content"]
    stored_texts = [entry[0]["content"] for entry in stored_content]

    assert "任务未完成" in notice_text
    assert stored_texts[-1] == notice_text


@pytest.mark.asyncio
async def test_query_handler_stop_defers_completion_side_effects(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    runner._generate_backend_suggestions_if_needed = AsyncMock()
    runner._index_model_output_if_needed = AsyncMock()
    runner._end_trace_if_needed = AsyncMock()
    stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal stop_calls
        if event_name == HookEventName.STOP:
            stop_calls += 1
            if stop_calls == 1:
                runner._generate_backend_suggestions_if_needed.assert_not_awaited()
                runner._index_model_output_if_needed.assert_not_awaited()
                runner._end_trace_if_needed.assert_not_awaited()
                return MergedHookResult(
                    decision=HookDecision.BLOCK,
                    reason="run checks first",
                )
            return MergedHookResult(decision=HookDecision.ALLOW, reason="ok")
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert [item[0].get_text_content() for item in outputs] == [
        "agent reply",
        "agent reply",
    ]
    runner._generate_backend_suggestions_if_needed.assert_awaited_once()
    runner._index_model_output_if_needed.assert_awaited_once()
    runner._end_trace_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_single_query_attempt_ends_trace_when_runtime_blocked(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner._prepare_query_runtime = AsyncMock(
        return_value=_RuntimeStartResult(
            block_response=Msg(
                name="Friday",
                role="assistant",
                content="blocked",
            ),
        ),
    )
    runner._end_trace_if_needed = AsyncMock()

    attempt_input = _QueryAttemptInput(
        request=SimpleNamespace(),
        msgs=[],
        query="hello",
        preflight=_QueryPreflight(),
        trace_id="trace-blocked",
    )
    outcome = _QueryTurnOutcome()
    retry_state = _RetryState()
    attempt_state = _QueryAttemptState()

    outputs = [
        item
        async for item in runner._stream_single_query_attempt(
            attempt_input=attempt_input,
            outcome=outcome,
            retry_state=retry_state,
            attempt_state=attempt_state,
        )
    ]

    assert [item[0].get_text_content() for item in outputs] == ["blocked"]
    assert attempt_state.should_return is True
    runner._end_trace_if_needed.assert_awaited_once_with(
        "trace-blocked",
        "completed",
    )


@pytest.mark.asyncio
async def test_query_handler_aggregate_budget_counts_stop_only(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    agent_config = _agent_config(HookConfig(enabled=True))
    agent_config.running.max_stop_turns = 2
    agent_config.running.max_automatic_follow_up_turns = 2
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: agent_config,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal stop_calls
        if event_name == HookEventName.STOP:
            stop_calls += 1
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason=f"gate-{stop_calls}",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]
    output_texts = [item[0].get_text_content() for item in outputs]

    assert output_texts == [
        "agent reply",
        "agent reply",
        "agent reply",
        output_texts[-1],
    ]
    assert "任务未完成" in output_texts[-1]
    assert "gate-3" in output_texts[-1]
    assert stop_calls == 3


@pytest.mark.asyncio
async def test_emit_stop_hook_respects_active_guard(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    runtime = _QueryRuntime(
        agent=_FakeAgent(),
        agent_config=_agent_config(
            HookConfig(
                enabled=True,
                events={
                    HookEventName.STOP: [
                        HookMatcherGroupConfig(
                            hooks=[
                                CommandHookHandlerConfig(
                                    id="policy",
                                    command="unused",
                                ),
                            ],
                        ),
                    ],
                },
            ),
        ),
        tenant_hooks=HookConfig(enabled=True),
        hook_overlay=HookSessionOverlay(),
        chat=SimpleNamespace(id="chat-1"),
        session_skill_detector=None,
        mcp_clients=[],
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        pending_confirmed_skill_snapshots={},
    )
    plan = _TurnPlan(
        original_user_message="hello",
        turn_msgs=[],
    )
    outcome = _QueryTurnOutcome(
        assistant_response="agent reply",
        stop_hook_active=True,
    )
    emit_hook = AsyncMock()
    monkeypatch.setattr("swe.app.runner.runner._emit_runner_hook", emit_hook)

    result = await runner._emit_stop_hook_if_needed(
        request=SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            channel_meta={},
        ),
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert result is None
    emit_hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_skip_emits_hook_telemetry_without_response_plaintext(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    messages: list[str] = []
    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.logger.info",
        lambda message, *args: messages.append(message % args),
    )
    result = await runner._emit_stop_hook_if_needed(
        request=SimpleNamespace(
            trace_id="trace-1",
            channel_meta={"turn_id": "turn-1"},
        ),
        runtime=SimpleNamespace(
            tenant_hooks=HookConfig(),
            agent_config=_agent_config(),
            hook_overlay=HookSessionOverlay(),
        ),
        plan=SimpleNamespace(original_user_message="hello"),
        outcome=_QueryTurnOutcome(),
    )

    assert result is None
    messages = [
        message
        for message in messages
        if message.startswith("HOOK_TELEMETRY ")
    ]
    assert len(messages) == 1
    payload = json.loads(messages[0].removeprefix("HOOK_TELEMETRY "))
    assert payload["schema"] == "hook_telemetry.v1"
    assert payload["hook_event_name"] == "Stop"
    assert payload["execution_state"] == "skipped"
    assert payload["skipped_reason"] == "empty_assistant_response"
    assert payload["handler_count"] == 0
    assert payload["handlers"] == []
    assert "hello" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_stop_hook_conversation_snapshot_uses_live_memory(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    agent = _FakeAgent()
    await agent.memory.add(Msg(name="user", role="user", content="hello"))
    await agent.memory.add(
        Msg(
            name="Friday",
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "visible"},
            ],
        ),
    )
    runtime = _QueryRuntime(
        agent=agent,
        agent_config=_agent_config(
            HookConfig(
                enabled=True,
                events={
                    HookEventName.STOP: [
                        HookMatcherGroupConfig(
                            hooks=[
                                CommandHookHandlerConfig(
                                    id="policy",
                                    command="unused",
                                    includeConversationSnapshot=True,
                                ),
                            ],
                        ),
                    ],
                },
            ),
        ),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        chat=SimpleNamespace(id="chat-1"),
        session_skill_detector=None,
        mcp_clients=[],
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        pending_confirmed_skill_snapshots={},
    )
    plan = _TurnPlan(original_user_message="hello", turn_msgs=[])
    outcome = _QueryTurnOutcome(assistant_response="agent reply")
    seen_payloads: list[dict] = []

    async def fake_execute_handler_result(handler, context, *, workspace_dir):
        del workspace_dir
        seen_payloads.append(context.to_handler_payload())
        from swe.agents.hook_runtime.models import HookHandlerResult

        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler_result,
    )

    await runner._emit_stop_hook_if_needed(
        request=SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            channel_meta={},
        ),
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert seen_payloads[0]["conversation_snapshot"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "visible"}],
        },
    ]


@pytest.mark.asyncio
async def test_stop_hook_conversation_snapshot_does_not_fall_back_to_stale_persisted_memory(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    await runner.session.save_merged_state(
        session_id="session-1",
        user_id="user-1",
        state={
            "agent": {
                "memory": {
                    "content": [
                        [
                            Msg(
                                name="user",
                                role="user",
                                content="stale question",
                            ).to_dict(),
                            [],
                        ],
                    ],
                },
            },
        },
    )
    seen_payloads: list[dict] = []

    async def fake_execute_handler_result(handler, context, *, workspace_dir):
        del workspace_dir
        seen_payloads.append(context.to_handler_payload())
        from swe.agents.hook_runtime.models import HookHandlerResult

        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler_result,
    )

    await _emit_runner_hook(
        HookEventName.STOP,
        request=SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            channel_meta={},
        ),
        runner=runner,
        tenant_hooks=HookConfig(),
        agent_config=_agent_config(
            HookConfig(
                enabled=True,
                events={
                    HookEventName.STOP: [
                        HookMatcherGroupConfig(
                            hooks=[
                                CommandHookHandlerConfig(
                                    id="policy",
                                    command="unused",
                                    includeConversationSnapshot=True,
                                ),
                            ],
                        ),
                    ],
                },
            ),
        ),
        overlay=HookSessionOverlay(),
        prompt="current question",
        assistant_response="current answer",
        agent=SimpleNamespace(memory=SimpleNamespace()),
    )

    assert seen_payloads[0]["conversation_snapshot"] == []
    assert seen_payloads[0]["conversation_snapshot_meta"] == {
        "included_messages": 0,
        "omitted_messages": 0,
        "limit": 50,
        "unavailable": True,
        "unavailable_reason": "agent_memory_unavailable",
    }


@pytest.mark.asyncio
async def test_runner_hook_conversation_snapshot_unavailable_without_agent(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    seen_payloads: list[dict] = []

    async def fake_execute_handler_result(handler, context, *, workspace_dir):
        del workspace_dir
        seen_payloads.append(context.to_handler_payload())
        from swe.agents.hook_runtime.models import HookHandlerResult

        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler_result,
    )

    await _emit_runner_hook(
        HookEventName.SESSION_START,
        request=SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            channel_meta={},
        ),
        runner=runner,
        tenant_hooks=HookConfig(),
        agent_config=_agent_config(
            HookConfig(
                enabled=True,
                events={
                    HookEventName.SESSION_START: [
                        HookMatcherGroupConfig(
                            hooks=[
                                CommandHookHandlerConfig(
                                    id="policy",
                                    command="unused",
                                    includeConversationSnapshot=True,
                                ),
                            ],
                        ),
                    ],
                },
            ),
        ),
        overlay=HookSessionOverlay(),
        source="startup",
    )

    assert seen_payloads[0]["conversation_snapshot"] == []
    assert seen_payloads[0]["conversation_snapshot_meta"] == {
        "included_messages": 0,
        "omitted_messages": 0,
        "limit": 50,
        "unavailable": True,
        "unavailable_reason": "agent_memory_unavailable",
    }


@pytest.mark.asyncio
async def test_session_start_hook_conversation_snapshot_uses_persisted_memory(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    await runner.session.save_merged_state(
        session_id="session-1",
        user_id="user-1",
        state={
            "agent": {
                "memory": {
                    "content": [
                        [
                            Msg(
                                name="user",
                                role="user",
                                content="resumed question",
                            ).to_dict(),
                            [],
                        ],
                        [
                            Msg(
                                name="Friday",
                                role="assistant",
                                content="resumed answer",
                            ).to_dict(),
                            [],
                        ],
                    ],
                },
            },
        },
    )
    seen_payloads: list[dict] = []

    async def fake_execute_handler_result(handler, context, *, workspace_dir):
        del workspace_dir
        seen_payloads.append(context.to_handler_payload())
        from swe.agents.hook_runtime.models import HookHandlerResult

        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler_result,
    )

    await _emit_runner_hook(
        HookEventName.SESSION_START,
        request=SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            channel_meta={},
        ),
        runner=runner,
        tenant_hooks=HookConfig(),
        agent_config=_agent_config(
            HookConfig(
                enabled=True,
                events={
                    HookEventName.SESSION_START: [
                        HookMatcherGroupConfig(
                            hooks=[
                                CommandHookHandlerConfig(
                                    id="policy",
                                    command="unused",
                                    includeConversationSnapshot=True,
                                ),
                            ],
                        ),
                    ],
                },
            ),
        ),
        overlay=HookSessionOverlay(),
        source="startup",
    )

    assert seen_payloads[0]["hook_event_name"] == "SessionStart"
    assert seen_payloads[0]["conversation_snapshot"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "resumed question"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "resumed answer"}],
        },
    ]
    assert seen_payloads[0]["conversation_snapshot_meta"] == {
        "included_messages": 2,
        "omitted_messages": 0,
        "limit": 50,
        "reasoning_omitted": False,
        "media_content_omitted": False,
    }


@pytest.mark.asyncio
async def test_query_handler_stop_hook_ignores_handler_effects(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    emit_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(),
            MergedHookResult(),
            MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="stop blocked",
            ),
        ],
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        emit_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert [item[0].get_text_content() for item in outputs] == ["agent reply"]
    stop_call = emit_hook.await_args_list[-1]
    assert stop_call.args[0] == HookEventName.STOP
    assert stop_call.kwargs["assistant_response"] == "agent reply"


def _make_terminal_stop_runner(
    monkeypatch,
    tmp_path,
    stop_reason,
) -> tuple[AgentRunner, type[_FakeAgent], SimpleNamespace, AsyncMock]:
    class TerminalStopAgent(_FakeAgent):
        calls = 0
        consume_calls = 0
        pre_tool_output_suppressed = False
        reset_calls = 0

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._pre_tool_terminal_stop_reason = None

        def consume_pre_tool_terminal_stop(self):
            type(self).consume_calls += 1
            reason = self._pre_tool_terminal_stop_reason
            self._pre_tool_terminal_stop_reason = None
            return reason

        def reset_pre_tool_terminal_stop(self):
            type(self).reset_calls += 1
            self.consume_pre_tool_terminal_stop()

        async def __call__(self, turn_msgs):
            type(self).calls += 1
            for msg in turn_msgs:
                self.memory.content.append((msg, []))
            if type(self).calls == 1:
                self._pre_tool_terminal_stop_reason = stop_reason
                self.pre_tool_hook_result = MergedHookResult(
                    decision=HookDecision.STOP,
                    reason=stop_reason or "",
                    suppress_output=True,
                )
                type(self).pre_tool_output_suppressed = (
                    self.pre_tool_hook_result.suppress_output
                )
                await self.memory.add(
                    Msg(
                        name="system",
                        role="system",
                        content="hook_stopped",
                    ),
                )
                raise PreToolUseTerminalStop(stop_reason)
            reply = Msg(
                name="Friday",
                role="assistant",
                content="normal second-turn reply",
            )
            await self.memory.add(reply)
            return [reply]

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", TerminalStopAgent)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    emit_hook = AsyncMock(return_value=MergedHookResult())
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        emit_hook,
    )
    runner._generate_backend_suggestions_if_needed = AsyncMock()
    runner._index_model_output_if_needed = AsyncMock()
    runner._end_trace_if_needed = AsyncMock()

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    return runner, TerminalStopAgent, request, emit_hook


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "expected_reason"),
    [
        ("policy stopped tool", "policy stopped tool"),
        (None, "Hook requested stop"),
    ],
)
async def test_query_handler_pre_tool_terminal_stop_is_final_and_allows_next_turn(
    monkeypatch,
    tmp_path,
    stop_reason,
    expected_reason,
) -> None:
    runner, terminal_stop_agent, request, emit_hook = (
        _make_terminal_stop_runner(
            monkeypatch,
            tmp_path,
            stop_reason,
        )
    )
    first_outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="run a tool")],
            request=request,
        )
    ]
    saved_state = await runner.session.get_session_state_dict(
        session_id="session-1",
        user_id="user-1",
    )

    assert [item[0].get_text_content() for item in first_outputs] == [
        expected_reason,
    ]
    assert first_outputs[-1][1] is True
    persisted_content = saved_state["agent"]["memory"]["content"]
    assert [entry[0]["content"] for entry in persisted_content[-2:]] == [
        "hook_stopped",
        expected_reason,
    ]
    assert terminal_stop_agent.pre_tool_output_suppressed is True
    assert [call.args[0] for call in emit_hook.await_args_list] == [
        HookEventName.USER_PROMPT_SUBMIT,
        HookEventName.SESSION_START,
    ]
    runner._generate_backend_suggestions_if_needed.assert_not_awaited()
    runner._index_model_output_if_needed.assert_not_awaited()
    runner._end_trace_if_needed.assert_awaited_once_with(
        None,
        "completed",
    )
    assert terminal_stop_agent.calls == 1
    assert terminal_stop_agent.consume_calls == 2
    assert terminal_stop_agent.reset_calls == 1

    second_outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="try again")],
            request=request,
        )
    ]

    assert [item[0].get_text_content() for item in second_outputs] == [
        "normal second-turn reply",
    ]
    assert terminal_stop_agent.calls == 2
    assert terminal_stop_agent.reset_calls == 2


@pytest.mark.asyncio
async def test_query_handler_persists_mutated_hook_overlay(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )

    async def fake_emit_runner_hook(*args, **kwargs):
        kwargs["overlay"].once_executed[
            "default:user-1:session-1:PreToolUse:once"
        ] = True
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]
    state = await runner.session.get_session_state_dict(
        session_id="session-1",
        user_id="user-1",
    )

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert state["hook_overlay"]["once_executed"] == {
        "default:user-1:session-1:PreToolUse:once": True,
    }


@pytest.mark.asyncio
async def test_query_handler_ends_request_skill_detector_in_finally(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )

    detector = SimpleNamespace(
        detect_from_user_message=lambda _message: ("xlsx", 0.9),
        start_skill=AsyncMock(),
        on_reasoning_end=AsyncMock(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._create_session_skill_detector",
        lambda **kwargs: detector,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="use xlsx")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    detector.start_skill.assert_not_awaited()
    detector.on_reasoning_end.assert_awaited_once()
