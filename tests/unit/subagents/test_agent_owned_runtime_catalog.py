# -*- coding: utf-8 -*-
"""Runtime catalog coverage for Agent-owned SubAgent packages."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe.agents.tools.subagent_background import (
    create_background_subagent_tools,
)
from swe.app.subagents import AgentOwnedDefinitionRepository
from swe.app.subagents import BackgroundSubAgentStartBlocked
from swe.config.config import AgentProfileConfig


@pytest.mark.asyncio
async def test_start_subagent_resolves_enabled_agent_owned_package(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def _start(**kwargs):
        captured.update(kwargs)
        return BackgroundSubAgentStartBlocked(limit=1)

    repository = AgentOwnedDefinitionRepository(
        tmp_path / "agents",
        owner_scope="tenant-1/agent-1",
    )
    package = repository.create(
        {
            "name": "code-reviewer",
            "description": "Review a code change.",
            "instruction": "Review only the requested scope.",
            "trigger_keywords": ["review", "审查"],
        },
    )
    package = repository.enable(
        package.definition_id,
        expected_revision=package.revision,
    )
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(start=_start),
        parent_agent_config=AgentProfileConfig(
            id="agent-1",
            name="Agent 1",
            workspace_dir=str(tmp_path),
        ),
        workspace_dir=tmp_path,
        request_context={"tenant_id": "tenant-1", "agent_id": "agent-1"},
        effective_skill_names=[],
        expert_discovery_enabled=True,
    )

    response = await tools["start_subagent"](
        expert_name="code-reviewer",
        objective="Review this change.",
    )

    payload = json.loads(response.content[0]["text"])
    definition = captured["definition"]
    assert payload["accepted"] is False
    assert definition.name == "code-reviewer"
    assert definition.instruction == "Review only the requested scope."
    assert captured["definition_match"].reason == "exact_name"
    assert (
        "code-reviewer: Review a code change."
        in tools["start_subagent"].__doc__
    )
