# -*- coding: utf-8 -*-
"""Integration seams between SubAgents, SWEAgent, and ToolGuardMixin."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg
from agentscope.tool import ToolResponse

from swe.agents import react_agent as react_agent_module
from swe.agents.hook_runtime.models import MergedHookResult
from swe.agents.react_agent import SWEAgent
from swe.agents.skill_tool_registry import (
    reset_skill_tool_registry,
    SkillToolRegistry,
)
from swe.agents.tool_guard_mixin import (
    ToolGuardMixin,
    _goal_tool_may_write_environment,
)
from swe.app.subagents import PermissionPolicy
from swe.security.tool_guard.models import (
    GuardFinding,
    GuardSeverity,
    GuardThreatCategory,
    ToolGuardResult,
)
from swe.app.source_system_config.models import (
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
)
from swe.app.source_system_config.runtime import bind_source_system_config
from swe.config.config import AgentProfileConfig


class _Memory:
    def __init__(self):
        self.content = []

    async def add(self, msg, marks=None):
        self.content.append((msg, marks or []))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("remote_mutate", {}),
        ("write_file", {}),
        ("execute_shell_command", {"command": "pytest"}),
    ],
)
async def test_plan_subagent_applies_plan_guard_to_mcp_and_builtin_tools(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict,
) -> None:
    policy = PermissionPolicy.bounded(
        allow_tools=["write_file", "execute_shell_command"],
    )
    agent = _FakeGuardAgent(tmp_path, policy)
    agent._request_context["plan_mode_enabled"] = True
    agent._request_context["subagent_allowed_mcp_servers"] = ["remote"]
    agent.mcp_servers = {"remote_mutate": "remote"}
    denied = await agent._acting_policy_denial(
        {"id": "call-1"},
        tool_name,
        tool_input,
        include_budget=False,
    )
    assert denied is True
    assert "Plan Mode" in str(agent.memory.content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_enabled", "tool_name"),
    [(True, "read_file"), (False, "remote_mutate")],
)
async def test_plan_subagent_allows_reads_and_normal_worker_keeps_mcp(
    tmp_path: Path,
    plan_enabled: bool,
    tool_name: str,
) -> None:
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())
    agent._request_context["plan_mode_enabled"] = plan_enabled
    agent._request_context["subagent_allowed_mcp_servers"] = ["remote"]
    agent.mcp_servers = {"remote_mutate": "remote"}
    assert not await agent._acting_policy_denial(
        {"id": "call-1"},
        tool_name,
        {},
        include_budget=False,
    )


class _BaseAgent:
    async def _acting(self, tool_call):
        return {"content": tool_call["input"]}


class _ReasoningBaseAgent(_BaseAgent):
    def __init__(self) -> None:
        self.reasoning_called = False

    async def _reasoning(self, tool_choice=None):
        del tool_choice
        self.reasoning_called = True
        raise AssertionError("Plan interaction boundary must stop reasoning")


class _PlanningToolkit:
    async def call_tool_function(self, tool_call):
        async def _chunks():
            yield ToolResponse(
                content=[
                    {
                        "type": "text",
                        "text": "Planning clarification requested.",
                    },
                ],
                metadata={
                    "plan_interaction_card": {
                        "card_type": "plan_clarification",
                        "kind": "form",
                        "prompt": tool_call["input"]["prompt"],
                        "form_id": "customer_operation_plan",
                        "fields": [
                            {
                                "id": "industry",
                                "label": "行业/业务类型",
                                "type": "select",
                                "options": [
                                    {
                                        "id": "SaaS/软件服务",
                                        "label": "SaaS/软件服务",
                                    },
                                ],
                                "required": True,
                            },
                        ],
                        "allow_custom_response": True,
                    },
                },
            )

        return _chunks()


class _FakeGuardAgent(ToolGuardMixin, _BaseAgent):
    name = "Friday"

    def __init__(
        self,
        tmp_path: Path,
        policy: PermissionPolicy,
        *,
        subagent_budget: dict | None = None,
    ):
        self._request_context = {
            "session_id": "session-1",
            "agent_role": "subagent",
            "subagent_policy": policy.model_dump(mode="json"),
        }
        if subagent_budget is not None:
            self._request_context["subagent_budget"] = subagent_budget
        self._agent_config = SimpleNamespace()
        self._workspace_dir = tmp_path
        self.memory = _Memory()
        self.printed = []
        self._tool_guard_lock = asyncio.Lock()
        self._emit_tool_hook_called = False
        self._acting_with_approval_called = False

    def _resolve_mcp_server(self, tool_name: str) -> str | None:
        return getattr(self, "mcp_servers", {}).get(tool_name)

    def _ensure_tool_guard(self) -> None:
        self._tool_guard_engine = SimpleNamespace(enabled=False)

    async def _emit_tool_hook(self, *args, **kwargs):
        self._emit_tool_hook_called = True
        return MergedHookResult()

    async def _acting_with_approval(self, *args, **kwargs):
        self._acting_with_approval_called = True
        raise AssertionError("SubAgent hard policy must not request approval")

    async def print(self, msg, *args, **kwargs):
        self.printed.append(msg)


@pytest.mark.asyncio
async def test_selected_expert_replay_does_not_synthesize_run_control(
    tmp_path: Path,
) -> None:
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())
    agent._request_context["selected_expert_execution"] = True
    agent._tool_guard_replay_done = {
        "tool_name": "start_subagent",
        "tool_call_id": "start-1",
        "remaining_queue": [],
    }

    assert await agent._reason_about_replay_done() is None
    assert not hasattr(agent, "_selected_expert_follow_up")


class _FakePlanGuardAgent(ToolGuardMixin, _BaseAgent):
    name = "Friday"

    def __init__(self, tmp_path: Path):
        self._request_context = {
            "session_id": "session-1",
            "agent_role": "main",
            "plan_mode_enabled": True,
        }
        self._agent_config = SimpleNamespace()
        self._workspace_dir = tmp_path
        self.memory = _Memory()
        self.printed = []
        self._tool_guard_lock = asyncio.Lock()
        self._emit_tool_hook_called = False
        self._acting_with_approval_called = False

    def _ensure_tool_guard(self) -> None:
        self._tool_guard_engine = SimpleNamespace(enabled=False)

    async def _emit_tool_hook(self, *args, **kwargs):
        self._emit_tool_hook_called = True
        return MergedHookResult()

    async def _acting_with_approval(self, *args, **kwargs):
        self._acting_with_approval_called = True
        raise AssertionError("Plan Mode policy must not request approval")

    async def print(self, msg, *args, **kwargs):
        self.printed.append(msg)


class _FakePlanInteractionAgent(_FakePlanGuardAgent):
    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        self.toolkit = _PlanningToolkit()


class _FakePlanInteractionReasoningAgent(
    ToolGuardMixin,
    _ReasoningBaseAgent,
):
    name = "Friday"

    def __init__(self, tmp_path: Path):
        _ReasoningBaseAgent.__init__(self)
        self._request_context = {
            "session_id": "session-1",
            "agent_role": "main",
            "plan_mode_enabled": False,
        }
        self._agent_config = SimpleNamespace()
        self._workspace_dir = tmp_path
        self.memory = _Memory()
        self.printed = []
        self._tool_guard_lock = asyncio.Lock()
        self.toolkit = _PlanningToolkit()

    def _ensure_tool_guard(self) -> None:
        self._tool_guard_engine = SimpleNamespace(enabled=False)

    async def _emit_tool_hook(self, *args, **kwargs):
        return MergedHookResult()

    async def print(self, msg, *args, **kwargs):
        self.printed.append(msg)


class _FakeNormalMainGuardAgent(ToolGuardMixin, _BaseAgent):
    name = "Friday"

    def __init__(self, tmp_path: Path):
        self._request_context = {
            "session_id": "session-1",
            "agent_role": "main",
            "plan_mode_enabled": False,
            "accepted_plan": {
                "plan_id": "plan-1",
                "title": "Accepted plan",
            },
        }
        self._agent_config = SimpleNamespace()
        self._workspace_dir = tmp_path
        self.memory = _Memory()
        self.printed = []
        self._tool_guard_lock = asyncio.Lock()
        self._emit_tool_hook_called = False
        self._acting_with_approval_called = False

    def _ensure_tool_guard(self) -> None:
        self._tool_guard_engine = SimpleNamespace(enabled=False)

    async def _emit_tool_hook(self, *args, **kwargs):
        self._emit_tool_hook_called = True
        return MergedHookResult()

    async def _acting_with_approval(self, *args, **kwargs):
        self._acting_with_approval_called = True
        return {"content": "approval path"}

    async def print(self, msg, *args, **kwargs):
        self.printed.append(msg)


def _bare_agent(tmp_path: Path, *, request_context=None) -> SWEAgent:
    agent = object.__new__(SWEAgent)
    agent._request_context = dict(request_context or {})
    agent._workspace_dir = tmp_path
    agent._env_context = None
    agent._agent_config = AgentProfileConfig(
        id="default",
        name="Default",
        workspace_dir=str(tmp_path),
    )
    agent._namesake_strategy = "skip"
    agent._effective_skills = []
    return agent


def _source_config_with_plan_interaction_tools(
    enabled: bool,
) -> EffectiveSourceSystemConfig:
    return EffectiveSourceSystemConfig(
        source_id="portal",
        config=SourceSystemConfig.model_validate(
            {
                "feature_switches": {
                    "normal_mode_plan_interaction_tools_enabled": enabled,
                },
            },
        ),
        version=1,
    )


def test_system_prompt_override_bypasses_normal_main_prompt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """SubAgent prompt override is not appended to the normal main prompt."""
    monkeypatch.setattr(
        react_agent_module,
        "build_system_prompt_from_working_dir",
        lambda **_: "main prompt",
    )
    agent = _bare_agent(
        tmp_path,
        request_context={"agent_role": "subagent"},
    )
    agent._system_prompt_override = "subagent prompt"

    prompt = SWEAgent._build_sys_prompt(agent)

    assert prompt == "subagent prompt"


def test_disable_workspace_skills_leaves_no_effective_skills(
    tmp_path: Path,
) -> None:
    """SubAgent construction can skip workspace skill registration."""
    agent = _bare_agent(tmp_path)
    agent._enable_workspace_skills = False
    toolkit = SimpleNamespace()

    SWEAgent._register_skills(agent, toolkit)

    assert agent.get_effective_skills() == []


def test_explicit_snapshot_skills_build_tool_attribution_from_copied_roots(
    tmp_path: Path,
) -> None:
    """Worker attribution reads its copied Skill, never the live workspace."""

    class _Toolkit:
        def __init__(self) -> None:
            self.skills: dict[str, dict[str, str]] = {}

        def register_agent_skill(self, skill_dir: str) -> None:
            self.skills[Path(skill_dir).name] = {"dir": skill_dir}

    reset_skill_tool_registry()
    workspace_skill = tmp_path / "skills" / "quality"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text(
        "---\nmetadata:\n  swe:\n    uses_tools:\n      - write_file\n---\n",
        encoding="utf-8",
    )
    copied_skill = tmp_path / "subagent-runs" / "run-1.skills" / "quality"
    copied_skill.mkdir(parents=True)
    (copied_skill / "SKILL.md").write_text(
        "---\nmetadata:\n  swe:\n    uses_tools:\n      - read_file\n---\n",
        encoding="utf-8",
    )
    agent = _bare_agent(tmp_path)

    SWEAgent._register_explicit_workspace_skills(
        agent,
        _Toolkit(),
        {"quality": copied_skill},
    )

    registry = agent.get_skill_tool_registry()
    assert registry.get_skills_for_tool("read_file") == ["quality"]
    assert registry.get_skills_for_tool("write_file") == []


def test_explicit_snapshot_skill_registries_are_agent_local(
    tmp_path: Path,
) -> None:
    """A later Agent skill build must not overwrite an earlier Agent registry."""

    class _Toolkit:
        def __init__(self) -> None:
            self.skills: dict[str, dict[str, str]] = {}

        def register_agent_skill(self, skill_dir: str) -> None:
            self.skills[Path(skill_dir).name] = {"dir": skill_dir}

    first_skill = tmp_path / "first.skills" / "quality"
    first_skill.mkdir(parents=True)
    (first_skill / "SKILL.md").write_text(
        "---\nmetadata:\n  swe:\n    uses_tools:\n      - read_file\n---\n",
        encoding="utf-8",
    )
    second_skill = tmp_path / "second.skills" / "quality"
    second_skill.mkdir(parents=True)
    (second_skill / "SKILL.md").write_text(
        "---\nmetadata:\n  swe:\n    uses_tools:\n      - write_file\n---\n",
        encoding="utf-8",
    )
    first_agent = _bare_agent(tmp_path)
    second_agent = _bare_agent(tmp_path)

    SWEAgent._register_explicit_workspace_skills(
        first_agent,
        _Toolkit(),
        {"quality": first_skill},
    )
    SWEAgent._register_explicit_workspace_skills(
        second_agent,
        _Toolkit(),
        {"quality": second_skill},
    )

    first_registry = first_agent.get_skill_tool_registry()
    second_registry = second_agent.get_skill_tool_registry()

    assert first_registry is not second_registry
    assert first_registry.get_skills_for_tool("read_file") == ["quality"]
    assert first_registry.get_skills_for_tool("write_file") == []
    assert second_registry.get_skills_for_tool("read_file") == []
    assert second_registry.get_skills_for_tool("write_file") == ["quality"]


@pytest.mark.asyncio
async def test_snapshot_skill_agent_does_not_setup_workspace_detector(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Detector setup is disabled when it would inspect mutable workspace Skills."""
    from swe.tracing import manager as trace_manager_module

    copied_skill = tmp_path / "subagent-runs" / "run-1.skills" / "quality"
    copied_skill.mkdir(parents=True)
    (copied_skill / "SKILL.md").write_text("# Quality", encoding="utf-8")
    agent = _bare_agent(tmp_path)
    agent._workspace_skill_dirs = {"quality": copied_skill}
    agent._runtime_skills = ["quality"]
    agent._skill_runtime_profiles = {}
    setup_calls: list[dict[str, object]] = []

    async def setup_skill_detector(**kwargs: object) -> None:
        setup_calls.append(kwargs)

    manager = SimpleNamespace(
        enabled=True,
        setup_skill_detector=setup_skill_detector,
    )
    monkeypatch.setattr(
        trace_manager_module,
        "has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        trace_manager_module,
        "get_trace_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        trace_manager_module,
        "get_current_trace",
        lambda: SimpleNamespace(skill_detector=None),
    )

    await SWEAgent.setup_skill_detector(agent, "trace-1")

    assert setup_calls == []


@pytest.mark.asyncio
async def test_agent_setup_skill_detector_passes_agent_local_registry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Trace detector setup must receive the Agent's own skill registry."""
    from swe.tracing import manager as trace_manager_module

    registry = SkillToolRegistry()
    agent = _bare_agent(tmp_path)
    agent._skill_tool_registry = registry
    agent._runtime_skills = ["quality"]
    agent._skill_runtime_profiles = {}
    setup_calls: list[dict[str, object]] = []

    async def setup_skill_detector(**kwargs: object) -> None:
        setup_calls.append(kwargs)

    manager = SimpleNamespace(
        enabled=True,
        setup_skill_detector=setup_skill_detector,
    )
    monkeypatch.setattr(
        trace_manager_module,
        "has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        trace_manager_module,
        "get_trace_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        trace_manager_module,
        "get_current_trace",
        lambda: SimpleNamespace(skill_detector=None),
    )

    await SWEAgent.setup_skill_detector(agent, "trace-1")

    assert setup_calls[0]["skill_tool_registry"] is registry


def test_subagent_toolkit_filters_builtins_and_excludes_delegate(
    tmp_path: Path,
) -> None:
    """Readonly toolkit includes the approved WPlus event tool exception."""
    config = AgentProfileConfig(
        id="default",
        name="Default",
        workspace_dir=str(tmp_path),
    )
    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "subagent",
            "enable_subagents": True,
            "subagent_policy": PermissionPolicy.readonly().model_dump(
                mode="json",
            ),
        },
    )
    agent._agent_config = config

    toolkit = SWEAgent._create_toolkit(agent)

    assert set(toolkit.tools) == {
        "execute_shell_command",
        "read_file",
        "grep_search",
        "glob_search",
        "get_current_time",
        "emit_wplus_sop_event",
    }
    assert "delegate_to_subagent" not in toolkit.tools


def test_main_agent_registers_plan_interaction_tools_by_mode_and_source_config(
    tmp_path: Path,
) -> None:
    """计划交互工具仅对 Plan Mode 或已开启 Source 的主 Agent 可用。"""
    disabled = _bare_agent(tmp_path, request_context={"agent_role": "main"})
    plan_mode = _bare_agent(
        tmp_path,
        request_context={"agent_role": "main", "plan_mode_enabled": True},
    )
    subagent = _bare_agent(
        tmp_path,
        request_context={"agent_role": "subagent", "plan_mode_enabled": True},
    )
    scheduled = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "execution_origin": "scheduled",
        },
    )
    scheduled_goal = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "execution_origin": "scheduled",
            "goal_mode_enabled": True,
        },
    )

    with bind_source_system_config(
        _source_config_with_plan_interaction_tools(False),
    ):
        normal_tools = SWEAgent._create_toolkit(disabled).tools
        plan_mode_tools = SWEAgent._create_toolkit(plan_mode).tools
        subagent_tools = SWEAgent._create_toolkit(subagent).tools

    with bind_source_system_config(
        _source_config_with_plan_interaction_tools(True),
    ):
        enabled_normal_tools = SWEAgent._create_toolkit(disabled).tools
        scheduled_tools = SWEAgent._create_toolkit(scheduled).tools
        scheduled_goal_tools = SWEAgent._create_toolkit(scheduled_goal).tools

    for tool_name in ("ask_plan_clarification", "submit_proposed_plan"):
        assert tool_name not in normal_tools
        assert tool_name in plan_mode_tools
        assert tool_name not in subagent_tools
        assert tool_name in enabled_normal_tools
        assert tool_name not in scheduled_tools
        assert tool_name not in scheduled_goal_tools


def test_publish_annotated_html_is_registered_only_for_validated_main_turn(
    tmp_path: Path,
) -> None:
    source = tmp_path / "media" / "source.html"
    source.parent.mkdir()
    source.write_text("<html></html>", encoding="utf-8")
    context = {
        "source_path": str(source),
        "expected_annotation_ids": ["ann-1"],
    }
    ordinary = _bare_agent(tmp_path, request_context={"agent_role": "main"})
    annotated = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "_document_annotation_context": context,
        },
    )
    subagent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "subagent",
            "_document_annotation_context": context,
        },
    )

    assert (
        "publish_annotated_html"
        not in SWEAgent._create_toolkit(ordinary).tools
    )
    assert (
        "publish_annotated_html" in SWEAgent._create_toolkit(annotated).tools
    )
    assert (
        "publish_annotated_html"
        not in SWEAgent._create_toolkit(subagent).tools
    )


def test_background_subagent_tools_require_explicit_intent(
    tmp_path: Path,
) -> None:
    """Main Agent sees start_subagent only after explicit expert selection."""
    hidden = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
        },
    )
    visible = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "selected_expert_id": "expert-1",
        },
    )

    assert "start_subagent" not in SWEAgent._create_toolkit(hidden).tools
    visible_tools = SWEAgent._create_toolkit(visible).tools
    assert "start_subagent" in visible_tools
    assert "wait_subagent" in visible_tools
    assert "get_subagent" in visible_tools
    assert "cancel_subagent" in visible_tools
    assert "delegate_to_subagent" not in visible_tools


def test_background_subagent_tools_accept_keywords_or_selected_expert(
    tmp_path: Path,
) -> None:
    """Current user intent and explicit expert selection unlock delegation."""
    hidden = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "current_user_text": "请用子代理分析这个模块",
        },
    )
    visible = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "selected_expert_id": "expert-1",
        },
    )

    hidden_tools = SWEAgent._create_toolkit(hidden).tools
    assert "start_subagent" in hidden_tools
    assert "wait_subagent" in hidden_tools
    assert "get_subagent" in hidden_tools
    assert "cancel_subagent" in hidden_tools

    visible_tools = SWEAgent._create_toolkit(visible).tools
    assert "start_subagent" in visible_tools
    assert "wait_subagent" in visible_tools
    assert "get_subagent" in visible_tools
    assert "cancel_subagent" in visible_tools


@pytest.mark.parametrize("enabled", [False, True])
def test_main_agent_passes_source_expert_discovery_to_catalog(
    tmp_path: Path,
    enabled: bool,
) -> None:
    from swe.app.subagents import AgentOwnedDefinitionRepository

    repository = AgentOwnedDefinitionRepository(
        tmp_path / "agents",
        owner_scope="tenant-1/default",
    )
    package = repository.create(
        {
            "name": "reviewer",
            "description": "Review code",
            "instruction": "Review carefully",
        },
    )
    repository.enable(
        package.definition_id,
        expected_revision=package.revision,
    )
    agent = _bare_agent(
        tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "current_user_text": "使用子代理",
        },
    )
    source = EffectiveSourceSystemConfig(
        source_id="portal",
        config=SourceSystemConfig.model_validate(
            {
                "feature_switches": {
                    "subagent_expert_discovery_enabled": enabled,
                },
            },
        ),
    )
    with bind_source_system_config(source):
        toolkit = SWEAgent._create_toolkit(agent)
    description = toolkit.tools["start_subagent"].original_func.__doc__
    assert ("reviewer: Review code" in description) is enabled


def test_start_subagent_description_lists_selected_expert_definition(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "skills" / "security" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.toml").write_text(
        'name = "reviewer"\n'
        'description = "Review code for security regressions."\n'
        'instruction = "Inspect evidence."\n'
        'trigger_keywords = ["security", "review"]\n',
        encoding="utf-8",
    )
    (tmp_path / "skill.json").write_text(
        '{"layout_version":2,"skills":{"security":{"enabled":true,"channels":["all"]}}}',
        encoding="utf-8",
    )
    selected_id = "11111111-1111-4111-8111-111111111111"
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / f"{selected_id}.toml").write_text(
        'name = "researcher"\n'
        'description = "Research expert."\n'
        'instruction = "Research the requested topic."\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "selected_expert_id": selected_id,
        },
    )

    tool = SWEAgent._create_toolkit(agent).tools["start_subagent"]
    description = tool.json_schema["function"]["description"]

    assert "researcher" in description
    assert "Research expert." in description
    assert "security:reviewer" not in description


def test_register_subagent_definition_is_never_exposed(
    tmp_path: Path,
) -> None:
    """Registration tool is not exposed for explicit expert selection."""
    normal = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "selected_expert_id": "expert-1",
        },
    )
    normal_tools = SWEAgent._create_toolkit(normal).tools

    assert "start_subagent" in normal_tools
    assert "register_subagent_definition" not in normal_tools


@pytest.mark.parametrize(
    "text",
    [
        "帮我注册一个账号",
        "讨论一下可复用代码结构",
        "注册一个可复用 Definition",
    ],
)
def test_register_subagent_definition_ignores_unrelated_registration_terms(
    tmp_path: Path,
    text: str,
) -> None:
    """Registration words alone do not expose SubAgent definition tools."""
    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "current_user_text": text,
        },
    )

    tools = SWEAgent._create_toolkit(agent).tools

    assert "register_subagent_definition" not in tools


@pytest.mark.parametrize(
    "requested_key",
    ["subagent_tools_requested", "subagent_registration_tools_requested"],
)
def test_subagent_tool_request_flags_do_not_bypass_text_intent_gate(
    tmp_path: Path,
    requested_key: str,
) -> None:
    """Only the current user message can open SubAgent tools."""
    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            requested_key: True,
        },
    )

    tools = SWEAgent._create_toolkit(agent).tools

    assert "start_subagent" not in tools
    assert "register_subagent_definition" not in tools


@pytest.mark.parametrize(
    "context_key",
    ["user_message", "query", "prompt", "message_text"],
)
def test_only_current_user_text_can_open_subagent_tools(
    tmp_path: Path,
    context_key: str,
) -> None:
    """Other request context text is not the current user message."""
    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            context_key: "请使用子代理注册一个可复用定义",
        },
    )

    tools = SWEAgent._create_toolkit(agent).tools

    assert "start_subagent" not in tools
    assert "register_subagent_definition" not in tools


def test_background_subagent_observe_tools_visible_with_active_runs(
    tmp_path: Path,
) -> None:
    """Structured user intent exposes tools independently of active runs."""

    class _Supervisor:
        def has_active_runs(self, scope):
            return True

    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "enable_subagents": True,
            "_subagent_supervisor": _Supervisor(),
        },
    )

    tools = SWEAgent._create_toolkit(agent).tools

    assert "start_subagent" in tools
    assert "wait_subagent" in tools
    assert "get_subagent" in tools
    assert "cancel_subagent" in tools


def test_background_subagent_management_tools_require_fresh_intent(
    tmp_path: Path,
) -> None:
    """A new explicit expert turn exposes all management tools."""
    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "selected_expert_id": "expert-1",
        },
    )

    tools = SWEAgent._create_toolkit(agent).tools

    assert "start_subagent" in tools
    assert "wait_subagent" in tools
    assert "get_subagent" in tools
    assert "cancel_subagent" in tools


def test_plan_mode_toolkit_excludes_mutating_tools(tmp_path: Path) -> None:
    """Plan Mode 只暴露规划所需的只读工具。"""
    agent = _bare_agent(
        tmp_path,
        request_context={"agent_role": "main", "plan_mode_enabled": True},
    )

    tools = SWEAgent._create_toolkit(agent).tools

    assert "read_file" in tools
    assert "grep_search" in tools
    assert "glob_search" in tools
    assert "get_current_time" in tools
    assert "execute_shell_command" in tools
    assert "ask_plan_clarification" in tools
    assert "submit_proposed_plan" in tools
    for tool_name in (
        "write_file",
        "edit_file",
        "copy_file_to_static",
        "update_task_progress",
        "set_user_timezone",
        "get_token_usage",
    ):
        assert tool_name not in tools


def test_plan_mode_shell_policy_allows_strict_readonly_commands(
    tmp_path: Path,
) -> None:
    """Plan Mode shell 仅允许参数可证明只读的简单命令。"""
    agent = _FakePlanGuardAgent(tmp_path)

    for command in (
        "pwd",
        "ls src",
        "rg accepted_plan src/swe",
        "grep -R accepted_plan src/swe",
        "git status --short",
        "git diff -- src/swe/app/plans/models.py",
        "git grep accepted_plan -- src/swe",
        "git log --oneline -5",
        "git show HEAD:README.md",
    ):
        assert (
            agent._plan_mode_policy_denial(
                "execute_shell_command",
                {"command": command},
            )
            is None
        ), command


def test_plan_mode_shell_policy_rejects_mutating_shell_bypasses(
    tmp_path: Path,
) -> None:
    """Plan Mode shell 默认拒绝复合语法和带写入能力的参数。"""
    agent = _FakePlanGuardAgent(tmp_path)

    for command in (
        "sed -i '' 's/foo/bar/' some-file.py",
        "git diff --output=/tmp/plan-mode-write.txt",
        "rg foo > out",
        "ls; touch x",
        "A=1 rg foo",
        "git show HEAD:foo > bar",
        "git diff --ext-diff",
    ):
        denial = agent._plan_mode_policy_denial(
            "execute_shell_command",
            {"command": command},
        )
        assert denial is not None, command


def test_goal_write_tracker_ignores_readonly_tools_and_marks_mutations() -> (
    None
):
    assert not _goal_tool_may_write_environment("read_file", {})
    assert not _goal_tool_may_write_environment(
        "execute_shell_command",
        {"command": "pytest -q"},
    )
    assert _goal_tool_may_write_environment("write_file", {})
    assert _goal_tool_may_write_environment(
        "execute_shell_command",
        {"command": "git commit -m 'goal'"},
    )


@pytest.mark.asyncio
async def test_plan_mode_hard_policy_allows_memory_and_clarification_tools(
    tmp_path: Path,
) -> None:
    """Plan Mode 可使用记忆检索和计划澄清工具补足规划上下文。"""
    agent = _FakePlanGuardAgent(tmp_path)

    for tool_name, tool_input in (
        ("memory_search", {"query": "prior decision"}),
        (
            "ask_plan_clarification",
            {"prompt": "Choose scope", "kind": "choice"},
        ),
    ):
        result = await agent._acting(
            {
                "id": f"tool-{tool_name}",
                "name": tool_name,
                "input": tool_input,
            },
        )

        assert result == {"content": tool_input}


@pytest.mark.asyncio
async def test_plan_interaction_tool_metadata_is_printed_and_persisted(
    tmp_path: Path,
) -> None:
    """计划交互卡片依赖消息 metadata，不能只保留工具文本输出。"""
    agent = _FakePlanInteractionAgent(tmp_path)

    result = await agent._acting(
        {
            "id": "tool-plan-1",
            "name": "ask_plan_clarification",
            "input": {
                "prompt": "制定客户经营计划需要明确几个方向，请告诉我：",
                "kind": "customer_operation_plan",
            },
        },
    )

    assert result is None
    assert agent.printed
    assert agent.printed[-1].metadata["plan_interaction_card"] == {
        "card_type": "plan_clarification",
        "kind": "form",
        "prompt": "制定客户经营计划需要明确几个方向，请告诉我：",
        "form_id": "customer_operation_plan",
        "fields": [
            {
                "id": "industry",
                "label": "行业/业务类型",
                "type": "select",
                "options": [
                    {
                        "id": "SaaS/软件服务",
                        "label": "SaaS/软件服务",
                    },
                ],
                "required": True,
            },
        ],
        "allow_custom_response": True,
    }
    assert (
        agent.memory.content[-1][0].metadata["plan_interaction_card"][
            "form_id"
        ]
        == "customer_operation_plan"
    )


@pytest.mark.asyncio
async def test_plan_interaction_card_stops_next_reasoning_turn(
    tmp_path: Path,
) -> None:
    """成功发出计划交互卡片后，本轮不再进入下一次模型 reasoning。"""
    agent = _FakePlanInteractionReasoningAgent(tmp_path)

    await agent._acting(
        {
            "id": "tool-plan-1",
            "name": "ask_plan_clarification",
            "input": {
                "prompt": "请选择范围",
                "kind": "customer_operation_plan",
            },
        },
    )

    msg = await agent._reasoning()

    assert agent.reasoning_called is False
    assert msg.role == "assistant"
    assert msg.content == []


@pytest.mark.asyncio
async def test_plan_interaction_card_stops_max_iter_summarizing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """最后一次 ReAct iteration 发出卡片后不能再生成总结。"""
    monkeypatch.setattr(
        react_agent_module,
        "get_active_model_supports_multimodal",
        lambda: True,
    )
    agent = object.__new__(SWEAgent)
    agent.name = "Friday"
    agent._init_agent_phase_state()
    agent._in_summarizing = False
    setattr(agent, "_plan_interaction_turn_boundary_reached", True)

    msg = await SWEAgent._summarizing(agent)

    assert msg.role == "assistant"
    assert msg.content == []


def test_plan_mode_toolkit_excludes_synchronous_delegation(
    tmp_path: Path,
) -> None:
    """Plan Mode also uses background SubAgent tools, not sync delegation."""
    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "plan_mode_enabled": True,
            "enable_subagents": True,
        },
    )

    assert "delegate_to_subagent" not in SWEAgent._create_toolkit(agent).tools


def test_plan_mode_toolkit_allows_background_subagent_intent(
    tmp_path: Path,
) -> None:
    """Plan Mode may use readonly background SubAgent tools when selected."""
    agent = _bare_agent(
        tmp_path,
        request_context={
            "agent_role": "main",
            "plan_mode_enabled": True,
            "enable_subagents": True,
            "selected_expert_id": "expert-1",
        },
    )

    tools = SWEAgent._create_toolkit(agent).tools

    assert "start_subagent" in tools
    assert "wait_subagent" in tools
    assert "get_subagent" in tools
    assert "cancel_subagent" in tools


@pytest.mark.asyncio
async def test_subagent_hard_policy_denies_before_hooks_and_approvals(
    tmp_path: Path,
) -> None:
    """Forbidden SubAgent calls are blocked before hook or approval flow."""
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "write_file",
            "input": {"path": "x", "content": "no"},
        },
    )

    assert result is None
    assert agent._emit_tool_hook_called is False
    assert agent._acting_with_approval_called is False
    assert "blocked by SubAgent policy" in str(agent.printed[0].content)


@pytest.mark.asyncio
async def test_subagent_hard_policy_allows_readonly_shell(
    tmp_path: Path,
) -> None:
    """Allowed readonly commands continue to normal tool execution."""
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"command": "git status --short"},
        },
    )

    assert result == {"content": {"command": "git status --short"}}


@pytest.mark.asyncio
async def test_subagent_mcp_policy_maps_client_name_to_declared_key(
    tmp_path: Path,
) -> None:
    """MCP tools use their declared config key, not display name, for scope."""
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())
    agent.mcp_servers = {"search": "tavily_mcp"}
    agent._request_context.update(
        {
            "subagent_allowed_mcp_servers": ["tavily_search"],
            "subagent_mcp_server_keys": {"tavily_mcp": "tavily_search"},
        },
    )

    result = await agent._acting(
        {"id": "tool-1", "name": "search", "input": {"query": "x"}},
    )

    assert result == {"content": {"query": "x"}}


@pytest.mark.asyncio
async def test_subagent_mcp_policy_denies_unsnapshotted_server(
    tmp_path: Path,
) -> None:
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())
    agent.mcp_servers = {"search": "unlisted"}
    agent._request_context["subagent_allowed_mcp_servers"] = ["github"]

    result = await agent._acting(
        {"id": "tool-1", "name": "search", "input": {"query": "x"}},
    )

    assert result is None
    assert "MCP server `unlisted` is not allowed" in str(
        agent.printed[0].content,
    )


@pytest.mark.asyncio
async def test_subagent_hard_policy_rechecks_hook_updated_input(
    tmp_path: Path,
) -> None:
    """Hook-updated input cannot widen readonly SubAgent permissions."""
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())

    async def _rewrite_to_mutating_shell(*args, **kwargs):
        agent._emit_tool_hook_called = True
        return MergedHookResult(
            updated_input={
                "command": "git status --short > /tmp/subagent-mutates",
            },
        )

    agent._emit_tool_hook = _rewrite_to_mutating_shell

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"command": "git status --short"},
        },
    )

    assert result is None
    assert agent._emit_tool_hook_called is True
    assert "blocked by SubAgent policy" in str(agent.printed[0].content)


@pytest.mark.asyncio
async def test_subagent_hook_ask_is_rejected_without_interactive_approval(
    tmp_path: Path,
) -> None:
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())

    async def _request_approval(*args, **kwargs):
        return MergedHookResult(
            decision="ask",
            reason="review shell",
        )

    agent._emit_tool_hook = _request_approval

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"command": "git status --short"},
        },
    )

    assert result is None
    assert agent._acting_with_approval_called is False
    assert "cannot await interactive approval" in str(agent.printed[0].content)


@pytest.mark.asyncio
async def test_subagent_guard_approval_is_rejected_without_interactive_approval(
    tmp_path: Path,
) -> None:
    agent = _FakeGuardAgent(tmp_path, PermissionPolicy.readonly())
    finding = GuardFinding(
        id="guard-1",
        rule_id="test",
        category=GuardThreatCategory.CODE_EXECUTION,
        severity=GuardSeverity.HIGH,
        title="Needs approval",
        description="A test finding.",
        tool_name="execute_shell_command",
    )
    guard_result = ToolGuardResult(
        tool_name="execute_shell_command",
        params={"command": "git status --short"},
        findings=[finding],
    )
    agent._tool_guard_engine = SimpleNamespace(
        enabled=True,
        is_denied=lambda _name: False,
        is_guarded=lambda _name: True,
        guard=lambda *_args, **_kwargs: guard_result,
    )
    agent._ensure_tool_guard = lambda: None

    async def _no_preapproval(*args, **kwargs):
        return False

    agent._consume_preapproval = _no_preapproval

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"command": "git status --short"},
        },
    )

    assert result is None
    assert agent._acting_with_approval_called is False
    assert "cannot be approved" in str(agent.printed[0].content)


@pytest.mark.asyncio
async def test_subagent_tool_call_budget_denies_extra_calls(
    tmp_path: Path,
) -> None:
    """Readonly SubAgents stop tool execution after max_tool_calls."""
    agent = _FakeGuardAgent(
        tmp_path,
        PermissionPolicy.readonly(),
        subagent_budget={"max_tool_calls": 1},
    )

    first = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"command": "git status --short"},
        },
    )
    second = await agent._acting(
        {
            "id": "tool-2",
            "name": "execute_shell_command",
            "input": {"command": "git status --short"},
        },
    )

    assert first == {"content": {"command": "git status --short"}}
    assert second is None
    assert "budget exceeded" in str(agent.printed[0].content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("write_file", {"file_path": "x", "content": "no"}),
        ("edit_file", {"file_path": "x", "old_str": "a", "new_str": "b"}),
        ("copy_file_to_static", {"file_path": "x"}),
        ("update_task_progress", {"tasks": []}),
        ("execute_shell_command", {"command": "pytest tests/unit"}),
        ("execute_shell_command", {"command": "git status > out.txt"}),
        ("execute_shell_command", {"command": "kubectl apply -f deploy.yaml"}),
        ("execute_shell_command", {"command": "alembic upgrade head"}),
    ],
)
async def test_plan_mode_hard_policy_denies_before_hooks_and_approvals(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict,
) -> None:
    """Plan Mode 硬策略在 hooks 和审批前拒绝写入或验证命令。"""
    agent = _FakePlanGuardAgent(tmp_path)

    result = await agent._acting(
        {"id": "tool-1", "name": tool_name, "input": tool_input},
    )

    assert result is None
    assert agent._emit_tool_hook_called is False
    assert agent._acting_with_approval_called is False
    assert "blocked by Plan Mode policy" in str(agent.printed[0].content)


@pytest.mark.asyncio
async def test_plan_mode_hard_policy_allows_readonly_shell(
    tmp_path: Path,
) -> None:
    """只读 shell 命令仍进入正常工具执行路径。"""
    agent = _FakePlanGuardAgent(tmp_path)

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"command": "git status --short"},
        },
    )

    assert result == {"content": {"command": "git status --short"}}


@pytest.mark.asyncio
async def test_plan_mode_hard_policy_rechecks_hook_updated_input(
    tmp_path: Path,
) -> None:
    """Hook 改写后的输入不能绕过 Plan Mode 只读策略。"""
    agent = _FakePlanGuardAgent(tmp_path)

    async def _rewrite_to_test_command(*args, **kwargs):
        agent._emit_tool_hook_called = True
        return MergedHookResult(updated_input={"command": "pytest"})

    agent._emit_tool_hook = _rewrite_to_test_command

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "execute_shell_command",
            "input": {"command": "git status --short"},
        },
    )

    assert result is None
    assert agent._emit_tool_hook_called is True
    assert "blocked by Plan Mode policy" in str(agent.printed[0].content)


@pytest.mark.asyncio
async def test_plan_mode_denial_leaves_workspace_file_unchanged(
    tmp_path: Path,
) -> None:
    """Plan Mode 拦截写工具后不会进入任何可能改写工作区的路径。"""
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    agent = _FakePlanGuardAgent(tmp_path)

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "write_file",
            "input": {
                "file_path": str(target),
                "content": "mutated",
            },
        },
    )

    assert result is None
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_execute_turn_restores_normal_main_agent_tool_path(
    tmp_path: Path,
) -> None:
    """execute 决策后的 normal 轮次不再套用 Plan Mode 硬拒绝。"""
    agent = _FakeNormalMainGuardAgent(tmp_path)

    result = await agent._acting(
        {
            "id": "tool-1",
            "name": "write_file",
            "input": {
                "file_path": "target.txt",
                "content": "allowed by normal mode",
            },
        },
    )

    assert result == {
        "content": {
            "file_path": "target.txt",
            "content": "allowed by normal mode",
        },
    }
    assert agent.printed == []
