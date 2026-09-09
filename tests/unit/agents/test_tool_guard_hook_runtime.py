# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
from agentscope.agent import ReActAgent
from agentscope.message import Msg

from swe.agents.hook_runtime.models import (
    AdditionalContext,
    CommandHookHandlerConfig,
    HookConfig,
    HookDecision,
    HookEventName,
    HookHandlerResult,
    HookMatcherGroupConfig,
    HookSessionState,
    LoadedSkillHookSource,
    MergedHookResult,
)
from swe.agents.skill_invocation_detector import SkillInvocationDetector
from swe.agents.skill_tool_registry import SkillToolRegistry
from swe.agents.tool_guard_mixin import (
    PreToolUseTerminalStop,
    ToolGuardMixin,
)
from swe.agents.tool_failure import TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD
from swe.security.tool_guard.models import (
    GuardFinding,
    GuardSeverity,
    GuardThreatCategory,
    ToolGuardResult,
)


class _Memory:
    def __init__(self):
        self.content = []

    async def add(self, msg, marks=None):
        self.content.append((msg, marks))


class _BaseAgent:
    async def _acting(self, tool_call):
        return {"content": tool_call["input"]}

    async def _reasoning(self, tool_choice=None):
        return Msg("Friday", "base reasoning", "assistant")


class _AgentScopeLikeBaseAgent:
    async def _acting(self, tool_call):
        await self.memory.add(
            Msg(
                "system",
                [
                    {
                        "type": "tool_result",
                        "id": tool_call["id"],
                        "name": tool_call["name"],
                        "output": tool_call["input"].get("output"),
                    },
                ],
                "system",
            ),
        )
        return None

    async def _reasoning(self, tool_choice=None):
        return Msg("Friday", "base reasoning", "assistant")


class _FakeAgent(ToolGuardMixin, _BaseAgent):
    name = "Friday"

    def __init__(self, tmp_path: Path):
        self._request_context = {
            "session_id": "session-1",
            "user_id": "user-1",
            "channel": "console",
            "agent_id": "agent-1",
        }
        self._agent_config = SimpleNamespace()
        self._workspace_dir = tmp_path
        self.memory = _Memory()
        self.printed = []
        self._tool_guard_lock = asyncio.Lock()

    def _ensure_tool_guard(self) -> None:
        self._tool_guard_engine = SimpleNamespace(enabled=False)

    async def print(self, msg, *args, **kwargs):
        self.printed.append(msg)


class _AgentScopeLikeFakeAgent(ToolGuardMixin, _AgentScopeLikeBaseAgent):
    name = "Friday"

    def __init__(self, tmp_path: Path):
        self._request_context = {
            "session_id": "session-1",
            "user_id": "user-1",
            "channel": "console",
            "agent_id": "agent-1",
        }
        self._agent_config = SimpleNamespace()
        self._workspace_dir = tmp_path
        self.memory = _Memory()
        self.printed = []
        self._tool_guard_lock = asyncio.Lock()

    def _ensure_tool_guard(self) -> None:
        self._tool_guard_engine = SimpleNamespace(enabled=False)

    async def print(self, msg, *args, **kwargs):
        self.printed.append(msg)


class _TerminalStopReplyLoopAgent(ToolGuardMixin, ReActAgent):
    """In-process agent that exercises AgentScope's real reply loop."""

    def __init__(self, tmp_path: Path):
        ReActAgent.__init__(
            self,
            name="Friday",
            sys_prompt="",
            model=SimpleNamespace(stream=False),
            formatter=SimpleNamespace(),
            enable_rewrite_query=False,
            max_iters=2,
        )
        self._request_context = {}
        self._agent_config = SimpleNamespace()
        self._workspace_dir = tmp_path
        self._tool_guard_lock = asyncio.Lock()
        self.reasoning_count = 0
        self.printed = []

    def _ensure_tool_guard(self) -> None:
        self._tool_guard_engine = SimpleNamespace(enabled=False)

    async def _reasoning(self, tool_choice=None):
        self.reasoning_count += 1
        return Msg(
            self.name,
            [
                {
                    "type": "tool_use",
                    "id": "tool-stop",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                },
            ],
            "assistant",
        )

    async def _emit_tool_hook(self, event_name, **_kwargs):
        if event_name == HookEventName.PRE_TOOL_USE:
            return MergedHookResult(
                decision=HookDecision.STOP,
                reason="stop reply loop",
            )
        return MergedHookResult()

    async def print(self, *args, **kwargs):
        self.printed.append(args[0] if args else kwargs["msg"])


class _RecordingApprovalService:
    def __init__(self) -> None:
        self.create_pending_kwargs = None

    async def cancel_stale_pending_for_tool_call(self, *args, **kwargs):
        return 0

    async def create_pending(self, **kwargs):
        self.create_pending_kwargs = kwargs
        return SimpleNamespace(request_id="approval-1", **kwargs)


class _SplitApprovalService:
    def __init__(self) -> None:
        self.created_pending = None
        self.queue_head = SimpleNamespace(
            request_id="approval-old",
            tool_name="execute_shell_command",
            extra={
                "tool_call": {
                    "id": "tool-old",
                    "name": "execute_shell_command",
                    "input": {"cmd": "echo old"},
                },
            },
        )

    async def cancel_stale_pending_for_tool_call(self, *args, **kwargs):
        return 0

    async def create_pending(self, **kwargs):
        self.created_pending = SimpleNamespace(
            request_id="approval-new",
            **kwargs,
        )
        return self.created_pending

    async def get_pending_by_session(self, session_id):
        assert session_id == "session-1"
        return self.queue_head


@pytest.mark.asyncio
async def test_tool_trace_prefers_request_context_over_current_trace(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._request_context.update(
        {
            "trace_id": "trace-request",
            "source_id": "source-request",
        },
    )
    emitted_events = []

    class FakeTraceContext:
        trace_id = "trace-current"
        user_id = "user-current"
        session_id = "session-current"
        channel = "console"
        source_id = "source-current"
        user_name = None
        bbk_id = None

    class FakeTraceManager:
        async def emit_tool_call_start(self, **kwargs):
            emitted_events.append(kwargs)
            return "span-1"

    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.get_current_trace",
        FakeTraceContext,
    )
    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.get_trace_manager",
        FakeTraceManager,
    )

    span_id = await agent._emit_tool_trace_start(
        "read_file",
        {"path": "README.md"},
        None,
    )

    assert span_id == "span-1"
    assert emitted_events[0]["trace_id"] == "trace-request"
    assert emitted_events[0]["user_id"] == "user-1"
    assert emitted_events[0]["session_id"] == "session-1"
    assert emitted_events[0]["source_id"] == "source-request"


@pytest.mark.asyncio
async def test_tool_trace_start_consumes_precomputed_skill_attribution(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._request_context.update(
        {
            "trace_id": "trace-request",
            "source_id": "source-request",
        },
    )
    agent._store_precomputed_tool_skill_attribution(
        "tool-1",
        {"primary_skill": "fill-metadata"},
    )
    emitted_events = []

    class FakeTraceManager:
        async def emit_tool_call_start(self, **kwargs):
            emitted_events.append(kwargs)
            return "span-1"

    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.get_trace_manager",
        FakeTraceManager,
    )

    span_id = await agent._emit_tool_trace_start(
        "read_file",
        {"path": "README.md"},
        None,
        "tool-1",
    )

    assert span_id == "span-1"
    assert emitted_events[0]["use_precomputed_attribution"] is True
    assert emitted_events[0]["precomputed_attribution"] == {
        "primary_skill": "fill-metadata",
    }
    assert agent._consume_precomputed_tool_skill_attribution("tool-1") == (
        False,
        None,
    )


@pytest.mark.asyncio
async def test_tool_trace_start_clears_precomputed_attribution_without_trace_manager(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._store_precomputed_tool_skill_attribution(
        "tool-1",
        {"primary_skill": "fill-metadata"},
    )

    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.has_trace_manager",
        lambda: False,
    )

    span_id = await agent._emit_tool_trace_start(
        "read_file",
        {"path": "README.md"},
        None,
        "tool-1",
    )

    assert span_id == ""
    assert agent._consume_precomputed_tool_skill_attribution("tool-1") == (
        False,
        None,
    )


@pytest.mark.asyncio
async def test_tool_trace_start_clears_precomputed_attribution_without_trace_context(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._store_precomputed_tool_skill_attribution(
        "tool-1",
        {"primary_skill": "fill-metadata"},
    )

    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        agent,
        "_resolve_trace_context_for_tracing",
        lambda: None,
    )

    span_id = await agent._emit_tool_trace_start(
        "read_file",
        {"path": "README.md"},
        None,
        "tool-1",
    )

    assert span_id == ""
    assert agent._consume_precomputed_tool_skill_attribution("tool-1") == (
        False,
        None,
    )


@pytest.mark.asyncio
async def test_tool_guard_pending_extra_includes_request_scope_ids(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._request_context.update(
        {
            "agent_id": "agent-a",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "chat_id": "chat-a",
            "msgid": "msg-a",
        },
    )
    approval_service = _RecordingApprovalService()
    agent._tool_guard_approval_service = approval_service
    notify = AsyncMock()
    monkeypatch.setattr(
        "swe.app.approvals.notify_cron_approval_pending",
        notify,
    )

    from swe.app.runner.operation_group import OPERATION_GROUP_INTERNAL_FIELD

    await agent._acting_with_approval(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo hi"},
            OPERATION_GROUP_INTERNAL_FIELD: {
                "id": "inspect",
                "title": "检查图片",
            },
        },
        "execute_shell_command",
        ToolGuardResult(
            tool_name="execute_shell_command",
            params={"cmd": "echo hi"},
        ),
    )

    assert approval_service.create_pending_kwargs is not None
    extra = approval_service.create_pending_kwargs["extra"]
    assert extra["agent_id"] == "agent-a"
    assert extra["tenant_id"] == "tenant-a"
    assert extra["source_id"] == "source-a"
    assert extra["operation_group"] == {
        "id": "inspect",
        "title": "检查图片",
    }
    assert OPERATION_GROUP_INTERNAL_FIELD not in extra["tool_call"]
    assert agent.printed[0].content[0]["_swe_tool_governance"] == "pending"
    assert agent.printed[0].metadata[
        TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD
    ] == {"tool-1": "pending"}
    assert extra["chat_id"] == "chat-a"
    assert extra["msgid"] == "msg-a"


@pytest.mark.asyncio
async def test_tool_guard_pending_extra_carries_goal_id_only_in_goal_mode(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._request_context["goal_id"] = "goal-1"
    approval_service = _RecordingApprovalService()
    agent._tool_guard_approval_service = approval_service
    monkeypatch.setattr(
        "swe.app.approvals.notify_cron_approval_pending",
        AsyncMock(),
    )

    await agent._acting_with_approval(
        {"id": "tool-goal", "name": "execute_shell_command", "input": {}},
        "execute_shell_command",
        ToolGuardResult(tool_name="execute_shell_command", params={}),
    )

    assert (
        approval_service.create_pending_kwargs["extra"]["goal_id"] == "goal-1"
    )


@pytest.mark.asyncio
async def test_waiting_approval_card_uses_notified_request_id(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _FakeAgent(tmp_path)
    approval_service = _SplitApprovalService()
    agent._tool_guard_approval_service = approval_service
    notify = AsyncMock()
    monkeypatch.setattr(
        "swe.app.approvals.notify_cron_approval_pending",
        notify,
    )

    await agent._acting_with_approval(
        {
            "id": "tool-new",
            "name": "execute_shell_command",
            "input": {"cmd": "echo new"},
        },
        "execute_shell_command",
        ToolGuardResult(
            tool_name="execute_shell_command",
            params={"cmd": "echo new"},
        ),
    )

    notified_pending = notify.await_args.args[0]
    waiting_msg = await agent._emit_waiting_for_approval()

    approval_action = waiting_msg.metadata["approval_action"]
    assert notified_pending.request_id == "approval-new"
    assert approval_action["requestId"] == notified_pending.request_id
    assert approval_action["approveCommand"] == "/approve approval-new"
    assert approval_action["denyCommand"] == "/deny approval-new"
    assert approval_action["toolInput"] == {"cmd": "echo new"}


@pytest.mark.asyncio
async def test_no_hook_config_preserves_tool_execution(tmp_path) -> None:
    agent = _FakeAgent(tmp_path)

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    )

    assert result == {"content": {"path": "README.md"}}
    assert agent.memory.content == []


@pytest.mark.asyncio
async def test_tool_hook_conversation_snapshot_uses_current_memory(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._agent_config.hooks = HookConfig(
        enabled=True,
        events={
            HookEventName.PRE_TOOL_USE: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="audit",
                            command="echo {}",
                            includeConversationSnapshot=True,
                        ),
                    ],
                ),
            ],
        },
    )
    await agent.memory.add(Msg("user", "hello", "user"))
    await agent.memory.add(
        Msg(
            "Friday",
            [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "visible reply"},
            ],
            "assistant",
        ),
    )
    seen_payloads: list[dict] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del handler, workspace_dir
        seen_payloads.append(context.to_handler_payload())
        return HookHandlerResult(handler_id="audit", order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )

    await agent._emit_tool_hook(
        HookEventName.PRE_TOOL_USE,
        tool_name="read_file",
        tool_input={"path": "README.md"},
        tool_use_id="tool-1",
    )

    assert seen_payloads[0]["conversation_snapshot"][0] == {
        "role": "user",
        "content": [{"type": "text", "text": "hello"}],
    }
    assert seen_payloads[0]["conversation_snapshot"][1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "visible reply"}],
    }
    assert seen_payloads[0]["conversation_snapshot_meta"] == {
        "included_messages": 2,
        "omitted_messages": 0,
        "limit": 50,
        "reasoning_omitted": True,
        "media_content_omitted": False,
    }


def test_tool_hooks_enabled_accepts_loaded_skill_sources(tmp_path) -> None:
    agent = _FakeAgent(tmp_path)
    agent._request_context["_hook_overlay_model"] = HookSessionState(
        loaded_skill_sources=[
            LoadedSkillHookSource(
                source_id="skill:xlsx",
                skill_name="xlsx",
                skill_root="/workspace/skills/xlsx",
                source_path="/workspace/skills/xlsx/hooks/hooks.json",
                hook_config=HookConfig(
                    enabled=True,
                    events={
                        HookEventName.PRE_TOOL_USE: [
                            HookMatcherGroupConfig(
                                id="skill:xlsx:shell",
                                hooks=[
                                    CommandHookHandlerConfig(
                                        id="skill:xlsx:hook",
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

    assert agent._tool_hooks_enabled(HookConfig())


def test_build_tool_hook_context_includes_correlation_fields(tmp_path) -> None:
    agent = _FakeAgent(tmp_path)
    agent._request_context.update(
        {
            "source_id": "source-a",
            "trace_id": "trace-1",
            "chat_id": "chat-1",
            "turn_id": "turn-1",
        },
    )

    context = agent._build_tool_hook_context(
        HookEventName.PRE_TOOL_USE,
        tool_name="read_file",
        tool_input={"path": "README.md"},
        tool_use_id="tool-1",
    )

    assert context.source_id == "source-a"
    assert context.trace_id == "trace-1"
    assert context.chat_id == "chat-1"
    assert context.turn_id == "turn-1"


@pytest.mark.asyncio
async def test_extract_current_tool_response_matches_latest_tool_result(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    await agent.memory.add(
        Msg(
            "system",
            [
                {
                    "type": "tool_result",
                    "id": "tool-1",
                    "name": "read_file",
                    "output": {"version": "old"},
                },
            ],
            "system",
        ),
    )
    await agent.memory.add(
        Msg(
            "system",
            [
                {
                    "type": "tool_result",
                    "id": "tool-2",
                    "name": "read_file",
                    "output": {"version": "other"},
                },
                {
                    "type": "tool_result",
                    "id": "tool-1",
                    "name": "read_file",
                    "output": {},
                },
            ],
            "system",
        ),
    )

    assert agent._extract_current_tool_response("tool-1") == {}
    assert agent._extract_current_tool_response("tool-2") == {
        "version": "other",
    }
    assert agent._extract_current_tool_response("missing") is None

    for index, output in enumerate(([], "", 0, False), start=3):
        tool_id = f"tool-{index}"
        await agent.memory.add(
            Msg(
                "system",
                [
                    {
                        "type": "tool_result",
                        "id": tool_id,
                        "name": "read_file",
                        "output": output,
                    },
                ],
                "system",
            ),
        )
        assert agent._extract_current_tool_response(tool_id) == output


@pytest.mark.asyncio
async def test_extract_current_tool_response_omits_structured_failure_output(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    await agent.memory.add(
        Msg(
            "system",
            [
                {
                    "type": "tool_result",
                    "id": "tool-1",
                    "name": "execute_shell_command",
                    "output": {
                        "isError": True,
                        "error_type": "tool_timeout",
                        "content": [
                            {
                                "type": "text",
                                "text": "Error: Tool timed out.",
                            },
                        ],
                    },
                },
            ],
            "system",
        ),
    )

    assert agent._extract_current_tool_response("tool-1") is None


@pytest.mark.asyncio
async def test_post_tool_hook_receives_current_tool_response_from_memory(
    monkeypatch,
    tmp_path,
) -> None:
    agent = _AgentScopeLikeFakeAgent(tmp_path)
    agent._agent_config.hooks = HookConfig(
        enabled=True,
        events={
            HookEventName.POST_TOOL_USE: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="audit",
                            command="echo {}",
                        ),
                    ],
                ),
            ],
        },
    )
    seen_payloads: list[dict] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del handler, workspace_dir
        seen_payloads.append(context.to_handler_payload())
        return HookHandlerResult(handler_id="audit", order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "market-movement",
            "input": {
                "output": {
                    "reportUrl": "https://example.test/report.html",
                    "summary": {"sourceData": {"shanghai": 3021.4}},
                },
            },
        },
    )

    assert result is None
    assert seen_payloads[0]["hook_event_name"] == "PostToolUse"
    assert seen_payloads[0]["tool_response"] == {
        "reportUrl": "https://example.test/report.html",
        "summary": {"sourceData": {"shanghai": 3021.4}},
    }


@pytest.mark.asyncio
async def test_post_tool_hook_omits_structured_failure_tool_response(
    monkeypatch,
    tmp_path,
) -> None:
    agent = _AgentScopeLikeFakeAgent(tmp_path)
    agent._agent_config.hooks = HookConfig(
        enabled=True,
        events={
            HookEventName.POST_TOOL_USE: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="audit",
                            command="echo {}",
                        ),
                    ],
                ),
            ],
        },
    )
    seen_payloads: list[dict] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del handler, workspace_dir
        seen_payloads.append(context.to_handler_payload())
        return HookHandlerResult(handler_id="audit", order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {
                "output": {
                    "isError": True,
                    "error_type": "tool_timeout",
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: Tool timed out.",
                        },
                    ],
                },
            },
        },
    )

    assert result is None
    assert seen_payloads[0]["hook_event_name"] == "PostToolUse"
    assert "tool_response" not in seen_payloads[0]


@pytest.mark.asyncio
async def test_tool_trace_uses_structured_failure_output_from_memory(
    monkeypatch,
    tmp_path,
) -> None:
    agent = _AgentScopeLikeFakeAgent(tmp_path)
    agent._request_context.update(
        {
            "trace_id": "trace-structured-error",
            "source_id": "source-structured-error",
        },
    )
    emitted_end_events: list[dict[str, object]] = []

    class FakeTraceManager:
        async def emit_tool_call_start(self, **kwargs):
            del kwargs
            return "span-structured-error"

        async def emit_tool_call_end(
            self,
            trace_id,
            span_id,
            tool_output,
            error,
        ):
            emitted_end_events.append(
                {
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "tool_output": tool_output,
                    "error": error,
                },
            )

    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.has_trace_manager",
        lambda: True,
    )
    fake_trace_manager = FakeTraceManager()
    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.get_trace_manager",
        lambda: fake_trace_manager,
    )

    result = await agent._acting(
        {
            "id": "tool-structured-error",
            "name": "test_http_585_error",
            "input": {
                "output": {
                    "isError": True,
                    "error_type": "mcp_tool_error",
                    "content": [
                        {
                            "type": "text",
                            "text": "HTTP error status 585 - MCP server 返回错误",
                        },
                    ],
                },
            },
        },
    )

    assert result is None
    assert emitted_end_events == [
        {
            "trace_id": "trace-structured-error",
            "span_id": "span-structured-error",
            "tool_output": None,
            "error": "HTTP error status 585 - MCP server 返回错误",
        },
    ]


@pytest.mark.asyncio
async def test_pre_tool_hook_updated_input_replaces_tool_call(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(updated_input={"cmd": "echo replaced"}),
            MergedHookResult(),
        ],
    )

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )

    assert result == {"content": {"cmd": "echo replaced"}}


@pytest.mark.asyncio
async def test_pre_tool_hook_denial_returns_tool_result(tmp_path) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.DENY,
            reason="no shell",
        ),
    )

    with nullcontext():
        result = await agent._acting(
            {
                "id": "tool-1",
                "name": "execute_shell_command",
                "input": {"cmd": "echo original"},
            },
        )

    assert result is None
    assert "no shell" in str(agent.printed[0].content)
    assert "hook_denied" in str(agent.printed[0].content)


@pytest.mark.asyncio
async def test_pre_tool_hook_block_returns_failed_tool_result_without_stopping(
    tmp_path,
) -> None:
    agent = _AgentScopeLikeFakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.BLOCK,
            reason="blocked by policy",
        ),
    )

    result = await agent._acting(
        {
            "id": "tool-block",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )

    assert result is None
    assert "hook_denied" in str(agent.printed[0].content)
    assert "hook_denied" in str(agent.memory.content[0][0].content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_reason"),
    [
        ("stop the agent", "stop the agent"),
        ("", "Hook requested stop"),
    ],
)
async def test_pre_tool_hook_stop_records_terminal_result_and_raises(
    tmp_path,
    reason: str,
    expected_reason: str,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.STOP,
            reason=reason,
        ),
    )

    with pytest.raises(PreToolUseTerminalStop) as exc_info:
        await agent._acting(
            {
                "id": "tool-stop",
                "name": "execute_shell_command",
                "input": {"cmd": "echo original"},
            },
        )

    assert exc_info.value.reason == expected_reason
    assert "hook_stopped" in str(agent.printed[0].content)
    assert "hook_stopped" in str(agent.memory.content[0][0].content)


@pytest.mark.asyncio
async def test_pre_tool_hook_stop_aborts_real_agentscope_reply_loop(
    tmp_path,
) -> None:
    agent = _TerminalStopReplyLoopAgent(tmp_path)

    with pytest.raises(PreToolUseTerminalStop, match="stop reply loop"):
        await agent.reply(Msg("user", "run a tool", "user"))

    assert agent.reasoning_count == 1


@pytest.mark.asyncio
async def test_consuming_pre_tool_hook_stop_allows_a_later_turn(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.STOP,
            reason="stop this turn",
        ),
    )

    with pytest.raises(PreToolUseTerminalStop):
        await agent._acting(
            {"id": "tool-stop", "name": "execute_shell_command", "input": {}},
        )

    assert agent.consume_pre_tool_terminal_stop() == "stop this turn"
    agent._emit_tool_hook = AsyncMock(
        side_effect=[MergedHookResult(), MergedHookResult()],
    )

    result = await agent._acting(
        {"id": "tool-next", "name": "execute_shell_command", "input": {}},
    )

    assert result == {"content": {}}


@pytest.mark.asyncio
async def test_pre_tool_hook_stop_cancels_registered_peer_before_execution(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    peer_hook_started = asyncio.Event()
    peer_hook_blocker = asyncio.Event()
    run_tool_call = AsyncMock()

    async def emit_hook(event_name, *, tool_name, **_kwargs):
        if event_name != HookEventName.PRE_TOOL_USE:
            return MergedHookResult()
        if tool_name == "peer":
            peer_hook_started.set()
            await peer_hook_blocker.wait()
        elif tool_name == "stop":
            await peer_hook_started.wait()
            return MergedHookResult(
                decision=HookDecision.STOP,
                reason="stop parallel tools",
            )
        return MergedHookResult()

    agent._emit_tool_hook = emit_hook
    agent._run_tool_call_with_hard_timeout = run_tool_call
    peer_task = asyncio.create_task(
        agent._acting({"id": "tool-peer", "name": "peer", "input": {}}),
    )
    stop_task = asyncio.create_task(
        agent._acting({"id": "tool-stop", "name": "stop", "input": {}}),
    )

    with pytest.raises(PreToolUseTerminalStop):
        await stop_task
    with pytest.raises(asyncio.CancelledError):
        await peer_task

    run_tool_call.assert_not_awaited()
    assert agent._active_tool_guard_acting_tasks == set()


@pytest.mark.asyncio
async def test_pre_tool_hook_stop_gates_peer_started_after_terminal_state(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.STOP,
            reason="stop later peer",
        ),
    )
    run_tool_call = AsyncMock()
    agent._run_tool_call_with_hard_timeout = run_tool_call

    with pytest.raises(PreToolUseTerminalStop):
        await agent._acting(
            {"id": "tool-stop", "name": "stop", "input": {}},
        )

    peer_task = asyncio.create_task(
        agent._acting({"id": "tool-peer", "name": "peer", "input": {}}),
    )
    with pytest.raises(PreToolUseTerminalStop, match="stop later peer"):
        await peer_task

    run_tool_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_tool_hook_denial_does_not_request_approval(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.DENY,
            reason="no shell",
        ),
    )
    agent._emit_waiting_for_approval = AsyncMock(
        return_value=Msg("Friday", "approval", "assistant"),
    )

    await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )
    result = await agent._reasoning()

    assert result.content == "base reasoning"
    agent._emit_waiting_for_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_tool_hook_ask_uses_existing_approval_path(tmp_path) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.ASK,
            reason="review shell",
        ),
    )
    agent._acting_with_approval = AsyncMock(return_value=None)

    await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )

    agent._acting_with_approval.assert_awaited_once()
    guard_result = agent._acting_with_approval.await_args.args[2]
    assert guard_result.findings[0].guardian == "unified_hook_runtime"


@pytest.mark.asyncio
async def test_approved_pre_tool_hook_ask_replay_executes_once(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._tool_guard_replay_approval = {
        "request_id": "approval-1",
        "approval_kind": "hook_pre_tool_use",
        "tool_call_id": "tool-1",
        "tool_name": "execute_shell_command",
        "tool_input": {"cmd": "echo original"},
        "hook_ask_handler_ids": ["hook-a"],
    }
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.ASK,
            reason="review shell",
            permission_decisions=[
                {
                    "handler_id": "hook-a",
                    "decision": HookDecision.ASK,
                    "reason": "review shell",
                },
            ],
        ),
    )
    agent._acting_with_approval = AsyncMock(return_value=None)

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )

    assert result == {"content": {"cmd": "echo original"}}
    agent._acting_with_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_tool_prompt_allow_does_not_bypass_tool_guard(
    monkeypatch,
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.ALLOW,
            reason="prompt allowed",
        ),
    )
    agent._acting_with_approval = AsyncMock(return_value=None)
    agent._tool_guard_engine = SimpleNamespace(
        enabled=True,
        is_denied=lambda _tool_name: False,
        is_guarded=lambda _tool_name: False,
        guard=lambda *_args, **_kwargs: SimpleNamespace(
            findings=[object()],
        ),
    )
    agent._ensure_tool_guard = lambda: None
    monkeypatch.setattr(
        "swe.security.tool_guard.utils.log_findings",
        lambda *_args, **_kwargs: None,
    )

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )

    assert result is None
    agent._acting_with_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsafe_guard_finding_auto_denies_without_approval_context(
    monkeypatch,
    tmp_path,
) -> None:
    """后台或无会话场景不能只记录高危发现后继续执行。"""
    agent = _FakeAgent(tmp_path)
    agent._request_context.pop("session_id")
    agent._emit_tool_hook = AsyncMock(return_value=MergedHookResult())
    agent._run_tool_call_with_hard_timeout = AsyncMock()
    finding = GuardFinding(
        id="finding-1",
        rule_id="TOOL_CMD_NETWORK_TRANSFER",
        category=GuardThreatCategory.DATA_EXFILTRATION,
        severity=GuardSeverity.HIGH,
        title="[HIGH] network transfer",
        description="network transfer",
        tool_name="execute_shell_command",
        param_name="command",
    )
    guard_result = ToolGuardResult(
        tool_name="execute_shell_command",
        params={"command": "curl https://example.test"},
        findings=[finding],
    )
    agent._tool_guard_engine = SimpleNamespace(
        enabled=True,
        is_denied=lambda _tool_name: False,
        is_guarded=lambda _tool_name: False,
        guard=lambda *_args, **_kwargs: guard_result,
    )
    agent._ensure_tool_guard = lambda: None
    monkeypatch.setattr(
        "swe.security.tool_guard.utils.log_findings",
        lambda *_args, **_kwargs: None,
    )

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"command": "curl https://example.test"},
        },
    )

    assert result is None
    agent._run_tool_call_with_hard_timeout.assert_not_awaited()
    assert "tool_guard_denied" in str(agent.printed[0].content)
    assert agent.printed[0].content[0]["_swe_tool_governance"] == "blocked"
    assert agent.printed[0].metadata[
        TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD
    ] == {"tool-1": "blocked"}


@pytest.mark.asyncio
async def test_approved_pre_tool_hook_ask_replay_does_not_cover_new_ask_handler(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._tool_guard_replay_approval = {
        "request_id": "approval-1",
        "approval_kind": "hook_pre_tool_use",
        "tool_call_id": "tool-1",
        "tool_name": "execute_shell_command",
        "tool_input": {"cmd": "echo original"},
        "hook_ask_handler_ids": ["hook-a"],
    }
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.ASK,
            reason="review shell",
            permission_decisions=[
                {
                    "handler_id": "hook-a",
                    "decision": HookDecision.ASK,
                    "reason": "review shell",
                },
                {
                    "handler_id": "hook-b",
                    "decision": HookDecision.ASK,
                    "reason": "new policy",
                },
            ],
        ),
    )
    agent._acting_with_approval = AsyncMock(return_value=None)

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )

    assert result is None
    agent._acting_with_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_approved_pre_tool_hook_ask_replay_does_not_bypass_deny(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._tool_guard_replay_approval = {
        "request_id": "approval-1",
        "approval_kind": "hook_pre_tool_use",
        "tool_call_id": "tool-1",
        "tool_name": "execute_shell_command",
        "tool_input": {"cmd": "echo original"},
        "hook_ask_handler_ids": ["hook-a"],
    }
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.DENY,
            reason="blocked now",
            permission_decisions=[
                {
                    "handler_id": "hook-a",
                    "decision": HookDecision.ASK,
                    "reason": "review shell",
                },
            ],
        ),
    )
    agent._acting_with_approval = AsyncMock(return_value=None)

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )

    assert result is None
    assert "blocked now" in str(agent.printed[0].content)
    agent._acting_with_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_pre_tool_hook_ask_replay_reasks_when_input_changes(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._tool_guard_replay_approval = {
        "request_id": "approval-1",
        "approval_kind": "hook_pre_tool_use",
        "tool_call_id": "tool-1",
        "tool_name": "execute_shell_command",
        "tool_input": {"cmd": "echo original"},
        "hook_ask_handler_ids": ["hook-a"],
    }
    agent._emit_tool_hook = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.ASK,
            reason="review shell",
            updated_input={"cmd": "echo changed"},
            permission_decisions=[
                {
                    "handler_id": "hook-a",
                    "decision": HookDecision.ASK,
                    "reason": "review shell",
                },
            ],
        ),
    )
    agent._acting_with_approval = AsyncMock(return_value=None)

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"cmd": "echo original"},
        },
    )

    assert result is None
    agent._acting_with_approval.assert_awaited_once()
    assert agent._acting_with_approval.await_args.args[0]["input"] == {
        "cmd": "echo changed",
    }


@pytest.mark.asyncio
async def test_post_tool_hook_additional_context_is_added_to_memory(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(
                additional_context=[
                    AdditionalContext(
                        handler_id="post",
                        context="remember me",
                    ),
                ],
            ),
        ],
    )

    await agent._acting(
        {
            "id": "tool-1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    )

    assert "remember me" in agent.memory.content[-1][0].content
    assert agent.memory.content[-1][0].role == "system"


@pytest.mark.asyncio
async def test_post_tool_hook_stop_preserves_success_then_raises(
    tmp_path,
) -> None:
    agent = _AgentScopeLikeFakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(
                decision=HookDecision.STOP,
                reason="policy completed the task",
                additional_context=[
                    AdditionalContext(
                        handler_id="post",
                        context="successful result was reviewed",
                    ),
                ],
            ),
        ],
    )

    with pytest.raises(PreToolUseTerminalStop, match="policy completed"):
        await agent._acting(
            {
                "id": "tool-1",
                "name": "read_file",
                "input": {"path": "README.md"},
            },
        )

    assert agent.memory.content[0][0].get_content_blocks("tool_result")
    assert (
        "successful result was reviewed" in agent.memory.content[-1][0].content
    )
    assert agent.printed == []
    assert [
        call.args[0] for call in agent._emit_tool_hook.await_args_list
    ] == [HookEventName.PRE_TOOL_USE, HookEventName.POST_TOOL_USE]


@pytest.mark.asyncio
async def test_preapproved_guarded_tool_runs_post_hook_and_honors_stop(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._tool_guard_engine = SimpleNamespace(
        enabled=True,
        is_denied=lambda _tool_name: False,
        is_guarded=lambda _tool_name: True,
        guard=lambda *_args, **_kwargs: None,
    )
    agent._ensure_tool_guard = lambda: None
    agent._tool_guard_approval_service = SimpleNamespace(
        consume_approval=AsyncMock(return_value=True),
    )
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(
                decision=HookDecision.STOP,
                reason="approved call completed",
            ),
        ],
    )

    with pytest.raises(
        PreToolUseTerminalStop,
        match="approved call completed",
    ):
        await agent._acting(
            {
                "id": "tool-approved",
                "name": "execute_shell_command",
                "input": {"cmd": "echo approved"},
            },
        )

    assert [
        call.args[0] for call in agent._emit_tool_hook.await_args_list
    ] == [HookEventName.PRE_TOOL_USE, HookEventName.POST_TOOL_USE]


@pytest.mark.asyncio
async def test_preapproved_guarded_tool_runs_failure_hook_and_honors_stop(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._tool_guard_engine = SimpleNamespace(
        enabled=True,
        is_denied=lambda _tool_name: False,
        is_guarded=lambda _tool_name: True,
        guard=lambda *_args, **_kwargs: None,
    )
    agent._ensure_tool_guard = lambda: None
    agent._tool_guard_approval_service = SimpleNamespace(
        consume_approval=AsyncMock(return_value=True),
    )
    agent._run_tool_call_with_hard_timeout = AsyncMock(
        side_effect=RuntimeError("approved tool failed"),
    )
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(
                decision=HookDecision.STOP,
                reason="approved failure reviewed",
            ),
        ],
    )

    with pytest.raises(
        PreToolUseTerminalStop,
        match="approved failure reviewed",
    ):
        await agent._acting(
            {
                "id": "tool-approved-failure",
                "name": "execute_shell_command",
                "input": {"cmd": "exit 1"},
            },
        )

    assert [
        call.args[0] for call in agent._emit_tool_hook.await_args_list
    ] == [HookEventName.PRE_TOOL_USE, HookEventName.POST_TOOL_USE_FAILURE]


@pytest.mark.asyncio
async def test_post_tool_stop_discards_queued_forced_replay(tmp_path) -> None:
    agent = _FakeAgent(tmp_path)
    agent._tool_guard_engine = SimpleNamespace(
        enabled=True,
        is_denied=lambda _tool_name: False,
        is_guarded=lambda _tool_name: True,
        guard=lambda *_args, **_kwargs: None,
    )
    agent._ensure_tool_guard = lambda: None
    agent._tool_guard_approval_service = SimpleNamespace(
        consume_approval=AsyncMock(return_value=True),
    )
    agent._tool_guard_forced_replay_active = True
    agent._tool_guard_replay_queue = [
        {
            "id": "tool-queued",
            "name": "execute_shell_command",
            "input": {"cmd": "echo queued"},
        },
    ]
    agent._tool_result_exists_in_memory = lambda _tool_use_id: False
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(
                decision=HookDecision.STOP,
                reason="terminal replay stop",
            ),
        ],
    )

    with pytest.raises(PreToolUseTerminalStop, match="terminal replay stop"):
        await agent._acting(
            {
                "id": "tool-approved",
                "name": "execute_shell_command",
                "input": {"cmd": "echo approved"},
            },
        )

    assert await agent._reason_about_replay_done() is None
    assert agent._tool_guard_replay_queue == []
    assert agent._tool_guard_forced_replay_active is False


@pytest.mark.asyncio
async def test_post_tool_failure_hook_stop_replaces_original_error(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    original_failure = Msg(
        "system",
        [
            {
                "type": "tool_result",
                "id": "tool-1",
                "name": "read_file",
                "output": "tool failed",
            },
        ],
        "system",
    )

    async def fail_tool_call(*_args):
        await agent.memory.add(original_failure)
        raise RuntimeError("tool failed")

    agent._run_tool_call_with_hard_timeout = fail_tool_call
    agent._emit_tool_trace_end = AsyncMock()
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(
                decision=HookDecision.STOP,
                additional_context=[
                    AdditionalContext(
                        handler_id="failure",
                        context="failure was recorded",
                    ),
                ],
            ),
        ],
    )

    with pytest.raises(PreToolUseTerminalStop, match="Hook requested stop"):
        await agent._acting(
            {
                "id": "tool-1",
                "name": "read_file",
                "input": {"path": "README.md"},
            },
        )

    assert "failure was recorded" in agent.memory.content[-1][0].content
    assert agent.memory.content[0][0].get_content_blocks("tool_result")
    assert (
        agent._emit_tool_trace_end.await_args.kwargs["error"] == "tool failed"
    )
    assert [
        call.args[0] for call in agent._emit_tool_hook.await_args_list
    ] == [HookEventName.PRE_TOOL_USE, HookEventName.POST_TOOL_USE_FAILURE]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [HookDecision.BLOCK, HookDecision.DENY])
async def test_post_tool_success_non_stop_decision_remains_non_terminal(
    tmp_path,
    decision: HookDecision,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._emit_tool_hook = AsyncMock(
        side_effect=[MergedHookResult(), MergedHookResult(decision=decision)],
    )

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    )

    assert result == {"content": {"path": "README.md"}}
    assert agent.consume_pre_tool_terminal_stop() is None


@pytest.mark.asyncio
async def test_post_tool_failure_deny_remains_non_terminal(tmp_path) -> None:
    agent = _FakeAgent(tmp_path)
    agent._run_tool_call_with_hard_timeout = AsyncMock(
        side_effect=RuntimeError("tool failed"),
    )
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(decision=HookDecision.DENY, reason="no retry"),
        ],
    )

    with pytest.raises(RuntimeError, match="tool failed"):
        await agent._acting(
            {
                "id": "tool-1",
                "name": "read_file",
                "input": {"path": "README.md"},
            },
        )

    assert agent.consume_pre_tool_terminal_stop() is None


@pytest.mark.asyncio
async def test_post_tool_stop_cancels_registered_peer(tmp_path) -> None:
    agent = _FakeAgent(tmp_path)
    peer_started = asyncio.Event()
    peer_blocker = asyncio.Event()

    async def emit_hook(event_name, **kwargs):
        if (
            event_name == HookEventName.POST_TOOL_USE
            and kwargs["tool_name"] == "stop"
        ):
            return MergedHookResult(
                decision=HookDecision.STOP,
                reason="stop parallel tools",
            )
        return MergedHookResult()

    async def run_tool_call(tool_call, tool_name, _tool_input):
        if tool_name == "peer":
            peer_started.set()
            await peer_blocker.wait()
        return {"content": tool_call["input"]}

    agent._emit_tool_hook = emit_hook
    agent._run_tool_call_with_hard_timeout = run_tool_call
    peer_task = asyncio.create_task(
        agent._acting({"id": "tool-peer", "name": "peer", "input": {}}),
    )
    await peer_started.wait()
    stop_task = asyncio.create_task(
        agent._acting({"id": "tool-stop", "name": "stop", "input": {}}),
    )

    with pytest.raises(PreToolUseTerminalStop, match="stop parallel tools"):
        await stop_task
    with pytest.raises(asyncio.CancelledError):
        await peer_task

    with pytest.raises(PreToolUseTerminalStop, match="stop parallel tools"):
        await agent._acting({"id": "tool-late", "name": "late", "input": {}})

    assert agent._active_tool_guard_acting_tasks == set()


@pytest.mark.asyncio
async def test_tool_failure_hook_block_reason_is_added_to_memory(
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    agent._run_tool_call_with_hard_timeout = AsyncMock(
        side_effect=RuntimeError("tool failed"),
    )
    agent._emit_tool_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="failure context",
            ),
        ],
    )

    with pytest.raises(RuntimeError):
        await agent._acting(
            {
                "id": "tool-1",
                "name": "read_file",
                "input": {"path": "README.md"},
            },
        )

    assert "failure context" in agent.memory.content[-1][0].content
    assert agent.memory.content[-1][0].role == "system"


@pytest.mark.asyncio
async def test_tool_hook_once_state_is_written_back_to_request_context(
    monkeypatch,
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    tenant_hooks = HookConfig(
        enabled=True,
        events={
            HookEventName.PRE_TOOL_USE: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="once",
                            command="echo {}",
                            once=True,
                        ),
                    ],
                ),
            ],
        },
    )
    calls = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        calls.append((handler.id, context.hook_event_name, workspace_dir))
        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.ToolGuardMixin._load_tenant_hook_config",
        lambda self: tenant_hooks,
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )

    await agent._emit_tool_hook(
        HookEventName.PRE_TOOL_USE,
        tool_name="execute_shell_command",
        tool_input={"cmd": "echo one"},
        tool_use_id="tool-1",
    )
    await agent._emit_tool_hook(
        HookEventName.PRE_TOOL_USE,
        tool_name="execute_shell_command",
        tool_input={"cmd": "echo two"},
        tool_use_id="tool-2",
    )

    assert [call[0] for call in calls] == ["once"]
    hook_overlay = agent._request_context["hook_overlay"]
    assert isinstance(hook_overlay, dict)
    once_executed = hook_overlay["once_executed"]
    assert once_executed == {
        "default:user-1:session-1:PreToolUse:once": True,
    }


@pytest.mark.asyncio
async def test_skill_activation_loads_hooks_for_later_tool_event(
    monkeypatch,
    tmp_path,
) -> None:
    agent = _FakeAgent(tmp_path)
    registry = SkillToolRegistry()
    registry.register_skill_tools("xlsx", ["read_file"])
    loaded_state = HookSessionState()
    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "post.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "events": {
                    "PostToolUse": [
                        {
                            "id": "post",
                            "hooks": [
                                {
                                    "id": "post-hook",
                                    "type": "command",
                                    "argv": ["python", "scripts/post.py"],
                                },
                            ],
                        },
                    ],
                },
            },
        ),
        encoding="utf-8",
    )

    async def load_skill_hooks(skill_name: str) -> None:
        nonlocal loaded_state
        loaded_state = HookSessionState(
            loaded_skill_sources=[
                LoadedSkillHookSource(
                    source_id=f"skill:{skill_name}",
                    skill_name=skill_name,
                    skill_root=str(skill_root),
                    source_path=str(skill_root / "hooks" / "hooks.json"),
                    hook_config=HookConfig(
                        enabled=True,
                        events={
                            HookEventName.POST_TOOL_USE: [
                                HookMatcherGroupConfig(
                                    id=f"skill:{skill_name}:post",
                                    hooks=[
                                        CommandHookHandlerConfig(
                                            id=f"skill:{skill_name}:post-hook",
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
        agent._request_context["_hook_overlay_model"] = loaded_state
        agent._request_context["hook_overlay"] = loaded_state.model_dump(
            mode="json",
            by_alias=True,
        )

    detector = SkillInvocationDetector(
        registry=registry,
        skill_hook_loader=load_skill_hooks,
    )
    detector.set_enabled_skills(["xlsx"])
    agent._request_context["_skill_invocation_detector"] = detector
    agent._request_context["_hook_overlay_model"] = loaded_state
    await detector.start_skill(
        "xlsx",
        trigger_tool="read_file",
        trigger_reason="declared",
        load_hooks=True,
    )
    calls = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        calls.append((handler.id, context.hook_event_name))
        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.tool_guard_mixin.ToolGuardMixin._load_tenant_hook_config",
        lambda self: HookConfig(),
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )

    await agent._acting(
        {
            "id": "tool-1",
            "name": "read_file",
            "input": {"path": "data.xlsx"},
        },
    )

    assert calls == [
        ("skill:xlsx:post-hook", HookEventName.POST_TOOL_USE),
    ]


class TestMcpErrorTracing:
    """测试MCP工具错误信息的tracing记录."""

    def test_extract_mcp_error_content(self, tmp_path):
        """测试从CallToolResult提取错误信息."""
        from mcp.types import CallToolResult, TextContent

        agent = _FakeAgent(tmp_path)

        # 模拟MCP错误返回
        result = CallToolResult(
            content=[
                TextContent(type="text", text="Error: Connection timeout"),
                TextContent(type="text", text="Please check server status"),
            ],
            isError=True,
        )

        error_msg = agent._extract_mcp_error_content(result)
        assert "Connection timeout" in error_msg
        assert "check server status" in error_msg

    def test_extract_mcp_error_content_empty(self, tmp_path):
        """测试空content时的错误提取."""
        from mcp.types import CallToolResult

        agent = _FakeAgent(tmp_path)

        result = CallToolResult(content=[], isError=True)
        error_msg = agent._extract_mcp_error_content(result)
        assert error_msg == "MCP tool error"

    def test_extract_mcp_success_content(self, tmp_path):
        """测试从CallToolResult提取成功内容."""
        from mcp.types import CallToolResult, TextContent

        agent = _FakeAgent(tmp_path)

        result = CallToolResult(
            content=[
                TextContent(type="text", text="Operation completed"),
                TextContent(type="text", text="Result: OK"),
            ],
            isError=False,
        )

        content = agent._extract_mcp_success_content(result)
        assert "Operation completed" in content
        assert "Result: OK" in content

    def test_extract_dict_error_content(self, tmp_path):
        """测试从dict结果提取错误信息."""
        agent = _FakeAgent(tmp_path)

        result = {
            "isError": True,
            "content": [
                {"type": "text", "text": "Database connection failed"},
                {"type": "text", "text": "Retry count: 3"},
            ],
        }

        error_msg = agent._extract_dict_error_content(result)
        assert "Database connection failed" in error_msg
        assert "Retry count: 3" in error_msg

    def test_extract_dict_error_content_string_content(self, tmp_path):
        """测试content为字符串时的错误提取."""
        agent = _FakeAgent(tmp_path)

        result = {
            "isError": True,
            "content": "Simple error message",
        }

        error_msg = agent._extract_dict_error_content(result)
        assert error_msg == "Simple error message"

    @pytest.mark.asyncio
    async def test_emit_tool_trace_end_with_mcp_error(
        self,
        tmp_path,
        monkeypatch,
    ):
        """测试MCP错误时正确记录error到tracing."""
        from mcp.types import CallToolResult, TextContent

        agent = _FakeAgent(tmp_path)

        # 模拟tracing环境
        emitted_events = []

        class FakeTraceContext:
            trace_id = "trace-123"

        class FakeTraceManager:
            async def emit_tool_call_end(
                self,
                trace_id,
                span_id,
                tool_output,
                error,
            ):
                emitted_events.append(
                    {
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "tool_output": tool_output,
                        "error": error,
                    },
                )

        def _fake_has_trace_manager():
            return True

        def _fake_get_current_trace():
            return FakeTraceContext()

        def _fake_get_trace_manager():
            return FakeTraceManager()

        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.has_trace_manager",
            _fake_has_trace_manager,
        )
        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.get_current_trace",
            _fake_get_current_trace,
        )
        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.get_trace_manager",
            _fake_get_trace_manager,
        )

        # 模拟MCP错误返回
        mcp_result = CallToolResult(
            content=[
                TextContent(type="text", text="MCP server error: timeout"),
            ],
            isError=True,
        )

        await agent._emit_tool_trace_end("span-456", mcp_result)

        # 验证error被正确记录
        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event["span_id"] == "span-456"
        assert event["error"] == "MCP server error: timeout"
        assert event["tool_output"] is None

    @pytest.mark.asyncio
    async def test_emit_tool_trace_end_with_mcp_success(
        self,
        tmp_path,
        monkeypatch,
    ):
        """测试MCP成功时正确记录output到tracing."""
        from mcp.types import CallToolResult, TextContent

        agent = _FakeAgent(tmp_path)

        emitted_events = []

        class FakeTraceContext:
            trace_id = "trace-123"

        class FakeTraceManager:
            async def emit_tool_call_end(
                self,
                trace_id,
                span_id,
                tool_output,
                error,
            ):
                emitted_events.append(
                    {
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "tool_output": tool_output,
                        "error": error,
                    },
                )

        def _fake_has_trace_manager():
            return True

        def _fake_get_current_trace():
            return FakeTraceContext()

        def _fake_get_trace_manager():
            return FakeTraceManager()

        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.has_trace_manager",
            _fake_has_trace_manager,
        )
        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.get_current_trace",
            _fake_get_current_trace,
        )
        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.get_trace_manager",
            _fake_get_trace_manager,
        )

        # 模拟MCP成功返回
        mcp_result = CallToolResult(
            content=[
                TextContent(type="text", text="File read successfully"),
            ],
            isError=False,
        )

        await agent._emit_tool_trace_end("span-789", mcp_result)

        # 验证output被正确记录，error为None
        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event["span_id"] == "span-789"
        assert event["error"] is None
        assert "File read successfully" in event["tool_output"]

    @pytest.mark.asyncio
    async def test_emit_tool_trace_end_with_dict_error(
        self,
        tmp_path,
        monkeypatch,
    ):
        """测试dict形式的错误返回."""
        agent = _FakeAgent(tmp_path)

        emitted_events = []

        class FakeTraceContext:
            trace_id = "trace-123"

        class FakeTraceManager:
            async def emit_tool_call_end(
                self,
                trace_id,
                span_id,
                tool_output,
                error,
            ):
                emitted_events.append(
                    {
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "tool_output": tool_output,
                        "error": error,
                    },
                )

        def _fake_has_trace_manager():
            return True

        def _fake_get_current_trace():
            return FakeTraceContext()

        def _fake_get_trace_manager():
            return FakeTraceManager()

        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.has_trace_manager",
            _fake_has_trace_manager,
        )
        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.get_current_trace",
            _fake_get_current_trace,
        )
        monkeypatch.setattr(
            "swe.agents.tool_guard_mixin.get_trace_manager",
            _fake_get_trace_manager,
        )

        # 模拟dict形式错误返回
        dict_result = {
            "isError": True,
            "content": "Tool execution failed",
        }

        await agent._emit_tool_trace_end("span-dict", dict_result)

        # 验证error被正确记录
        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event["error"] == "Tool execution failed"


@pytest.mark.asyncio
async def test_guard_decision_failure_denies_instead_of_executing_tool(
    tmp_path,
):
    agent = _FakeAgent(tmp_path)
    tool_call = {
        "id": "guard-error",
        "name": "source_echo",
        "input": {"value": "untrusted"},
    }
    agent._resolve_mcp_server = lambda _name: None
    agent._apply_pre_tool_hook = AsyncMock(
        return_value=(tool_call, tool_call["input"], False, None),
    )
    agent._notify_skill_detector_tool_call = AsyncMock()
    agent._emit_tool_trace_start = AsyncMock(return_value="trace-1")
    agent._decide_guard_action = AsyncMock(
        side_effect=RuntimeError("guard down"),
    )
    agent._acting_hook_denied = AsyncMock(return_value={"denied": True})
    agent._emit_tool_trace_end = AsyncMock()
    agent._run_guarded_tool_call = AsyncMock()

    result = await agent._acting_impl(tool_call)

    assert result == {"denied": True}
    agent._acting_hook_denied.assert_awaited_once()
    agent._run_guarded_tool_call.assert_not_awaited()
