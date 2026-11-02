# -*- coding: utf-8 -*-
"""User intent and Source-scoped expert discovery boundaries."""

from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from swe.agents.react_agent import SWEAgent
from swe.agents.tools.subagent_background import (
    create_background_subagent_tools,
)
from swe.app.source_system_config.models import (
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
)
from swe.app.source_system_config.registry import (
    build_default_source_system_config_payload,
)
from swe.app.source_system_config.runtime import bind_source_system_config
from swe.app.subagents import AgentOwnedDefinitionRepository
from swe.app.subagents import BackgroundSubAgentStartBlocked
from swe.app.subagents.permissions import (
    compose_effective_policy,
    validate_tool_call,
)
from swe.config.config import AgentProfileConfig


@pytest.mark.parametrize(
    "text",
    [
        "请用子代理",
        "子智能体",
        "SUBAGENT",
        "Sub-Agent",
        "不要使用子代理",
        "解释 `subagent`",
    ],
)
@pytest.mark.parametrize(
    "mode",
    [{}, {"plan_mode_enabled": True}, {"goal_id": "goal-1"}],
)
def test_current_user_keywords_open_subagent_tools(text, mode):
    agent = object.__new__(SWEAgent)
    agent._agent_config = AgentProfileConfig(id="agent-1", name="Agent 1")
    context = {"current_user_text": text, **mode}
    assert not agent._background_subagent_registration_blocked(context)


@pytest.mark.parametrize("enabled", [True, "true", "1", "yes"])
def test_structured_subagent_intent_opens_tools(enabled):
    agent = object.__new__(SWEAgent)
    agent._agent_config = AgentProfileConfig(id="agent-1", name="Agent 1")
    assert not agent._background_subagent_registration_blocked(
        {"enable_subagents": enabled},
    )


@pytest.mark.parametrize("role", ["subagent", "completion_judge"])
def test_worker_roles_never_delegate(role):
    agent = object.__new__(SWEAgent)
    agent._agent_config = AgentProfileConfig(id="agent-1", name="Agent 1")
    assert agent._background_subagent_registration_blocked(
        {
            "agent_role": role,
            "current_user_text": "子代理",
            "enable_subagents": True,
            "selected_expert_id": "expert-1",
        },
    )


def test_history_and_expert_mentions_do_not_open_tools():
    agent = object.__new__(SWEAgent)
    agent._agent_config = AgentProfileConfig(id="agent-1", name="Agent 1")
    assert agent._background_subagent_registration_blocked(
        {
            "current_user_text": "请解释专家功能",
            "history": "使用子代理",
            "enable_subagents": "false",
        },
    )


def test_source_expert_discovery_defaults_off():
    defaults = build_default_source_system_config_payload()
    assert (
        defaults["feature_switches"].get(
            "subagent_expert_discovery_enabled",
        )
        is False
    )


@pytest.mark.asyncio
async def test_plan_start_intersects_parent_with_readonly_policy(
    tmp_path: Path,
):
    captured = {}

    async def start(**kwargs):
        captured.update(kwargs)
        return BackgroundSubAgentStartBlocked(limit=1)

    skill_root = tmp_path / "skills" / "research"
    (skill_root / "agents").mkdir(parents=True)
    (skill_root / "agents" / "researcher.toml").write_text(
        'name = "researcher"\ndescription = "Research"\n'
        'instruction = "Research the requested task."\n',
        encoding="utf-8",
    )
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(start=start),
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Agent 1"),
        workspace_dir=tmp_path,
        request_context={"plan_mode_enabled": True},
        effective_skill_names=["research"],
        skill_snapshot_dirs={"research": skill_root},
    )
    await tools["start_subagent"](
        expert_name="research:researcher",
        objective="Inspect",
    )
    parent = captured["parent_policy"]
    effective = compose_effective_policy(
        parent,
        parent,
        captured["runtime_policy"],
        parent,
    )
    assert not validate_tool_call(effective, "write_file", {}).allowed
    assert not validate_tool_call(
        effective,
        "execute_shell_command",
        {"command": "rm file"},
    ).allowed


@pytest.mark.asyncio
async def test_hidden_expert_name_cannot_resolve_its_instruction(
    tmp_path: Path,
):
    repository = AgentOwnedDefinitionRepository(
        tmp_path / "agents",
        owner_scope="tenant-1/agent-1",
    )
    package = repository.create(
        {
            "name": "reviewer",
            "description": "Hidden reviewer",
            "instruction": "Private expert instruction",
        },
    )
    repository.enable(
        package.definition_id,
        expected_revision=package.revision,
    )
    captured = {}

    async def start(**kwargs):
        captured.update(kwargs)
        return BackgroundSubAgentStartBlocked(limit=1)

    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(start=start),
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Agent 1"),
        workspace_dir=tmp_path,
        request_context={"tenant_id": "tenant-1", "agent_id": "agent-1"},
    )
    missing = await tools["start_subagent"](
        expert_name="reviewer",
        objective="Review",
    )
    assert json.loads(missing.content[0]["text"])["status"] == "not_found"
    assert not captured
    assert "reviewer: Hidden reviewer" not in tools["start_subagent"].__doc__


def test_skill_owned_directory_remains_available_with_discovery_off(
    tmp_path: Path,
):
    skill_root = tmp_path / "skills" / "security"
    (skill_root / "agents").mkdir(parents=True)
    (skill_root / "agents" / "reviewer.toml").write_text(
        'name = "reviewer"\ndescription = "Security review"\n'
        'instruction = "Review security"\n',
        encoding="utf-8",
    )
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(),
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Agent 1"),
        workspace_dir=tmp_path,
        request_context={"tenant_id": "tenant-1", "agent_id": "agent-1"},
        effective_skill_names=["security"],
        skill_snapshot_dirs={"security": skill_root},
    )
    assert (
        "security:reviewer: Security review" in tools["start_subagent"].__doc__
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_source_switch_controls_agent_owned_directory(tmp_path: Path, enabled):
    repository = AgentOwnedDefinitionRepository(
        tmp_path / "agents",
        owner_scope="tenant-1/agent-1",
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
    config = EffectiveSourceSystemConfig(
        source_id="portal",
        config=SourceSystemConfig.model_validate(
            {
                "feature_switches": {
                    "subagent_expert_discovery_enabled": enabled,
                },
            },
        ),
    )
    with bind_source_system_config(config):
        tools = create_background_subagent_tools(
            supervisor=SimpleNamespace(),
            parent_agent_config=AgentProfileConfig(
                id="agent-1",
                name="Agent 1",
            ),
            workspace_dir=tmp_path,
            request_context={"tenant_id": "tenant-1", "agent_id": "agent-1"},
            expert_discovery_enabled=enabled,
        )
    assert (
        "reviewer: Review code" in tools["start_subagent"].__doc__
    ) is enabled
