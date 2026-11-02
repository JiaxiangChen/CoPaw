# -*- coding: utf-8 -*-
"""Dependency views follow the actual expert selected by each start."""

import asyncio
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from swe.agents.tools import subagent_background as tool_module
from swe.app.subagents import (
    BackgroundSubAgentStartBlocked,
    SubAgentDefinition,
)
from swe.app.subagents.models import AgentOwnedDefinitionMetadata
from swe.app.subagents import session_dependencies
from swe.config.config import AgentProfileConfig


def _received(tmp_path, definition_id, name):
    skill = (
        tmp_path
        / "agents"
        / f"{definition_id}.dependencies"
        / "skills"
        / "frozen"
    )
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(name)
    return SubAgentDefinition(
        name=name,
        description="Received expert",
        instruction="Inspect",
        agent_owned=AgentOwnedDefinitionMetadata(
            definition_id=definition_id,
            declared_skills=["frozen"],
            community={
                "item_id": name,
                "version": "1.0.0",
                "content_fingerprint": "one",
            },
        ),
    )


def _tools(tmp_path, monkeypatch, definitions, start):
    monkeypatch.setattr(
        tool_module,
        "_build_definition_catalog",
        lambda **_: (
            SimpleNamespace(
                resolve_exact=definitions.get,
                list_definitions=lambda: list(definitions.values()),
            )
        ),
    )
    context = {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "session_id": "session-1",
        "chat_id": "00000000-0000-0000-0000-000000000001",
        "_expert_dependency_view_root": "stale",
    }
    tools = tool_module.create_background_subagent_tools(
        supervisor=SimpleNamespace(start=start),
        parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
        workspace_dir=tmp_path,
        request_context=context,
    )
    return tools, context


@pytest.mark.asyncio
async def test_concurrent_experts_get_private_contexts_and_frozen_views(
    tmp_path,
    monkeypatch,
):
    first = _received(
        tmp_path,
        "00000000-0000-0000-0000-000000000010",
        "first",
    )
    second = _received(
        tmp_path,
        "00000000-0000-0000-0000-000000000011",
        "second",
    )
    definitions = {item.name: item for item in [first, second]}
    contexts = []

    async def start(**kwargs):
        await asyncio.sleep(0)
        contexts.append(kwargs["request_context"])
        return BackgroundSubAgentStartBlocked(limit=1)

    tools, request_context = _tools(tmp_path, monkeypatch, definitions, start)
    await asyncio.gather(
        *(
            tools["start_subagent"](expert_name=name, objective="Inspect")
            for name in definitions
        ),
    )
    assert len(contexts) == 2
    roots = [
        Path(context["_expert_dependency_view_root"]) for context in contexts
    ]
    assert len(set(roots)) == 2
    assert all(context is not request_context for context in contexts)
    assert request_context["_expert_dependency_view_root"] == "stale"
    assert {
        (root / "skills/frozen/SKILL.md").read_text() for root in roots
    } == {
        "first",
        "second",
    }
    source = (
        tmp_path / "agents" / f"{first.agent_owned.definition_id}.dependencies"
    )
    (source / "skills/frozen/SKILL.md").write_text("updated")
    await tools["start_subagent"](expert_name="first", objective="Again")
    assert (
        Path(contexts[-1]["_expert_dependency_view_root"])
        / "skills/frozen/SKILL.md"
    ).read_text() == "first"


@pytest.mark.asyncio
async def test_broken_frozen_package_fails_before_supervisor(
    tmp_path,
    monkeypatch,
):
    definition = _received(
        tmp_path,
        "00000000-0000-0000-0000-000000000012",
        "broken",
    )
    root = (
        tmp_path
        / "agents"
        / f"{definition.agent_owned.definition_id}.dependencies"
    )
    (root / "skills/frozen/SKILL.md").unlink()
    (root / "skills/frozen").rmdir()
    started = []

    async def start(**kwargs):
        started.append(kwargs)
        return BackgroundSubAgentStartBlocked(limit=1)

    tools, _ = _tools(tmp_path, monkeypatch, {"broken": definition}, start)
    response = await tools["start_subagent"](
        expert_name="broken",
        objective="Inspect",
    )
    payload = json.loads(response.content[0]["text"])
    assert payload["status"] == "failed"
    assert not started


@pytest.mark.asyncio
async def test_same_expert_concurrent_first_starts_reuse_published_view(
    tmp_path,
    monkeypatch,
):
    definition = _received(
        tmp_path,
        "00000000-0000-0000-0000-000000000013",
        "shared",
    )
    publish_barrier = threading.Barrier(2)
    replace = session_dependencies.os.replace

    def publish_together(source, target):
        publish_barrier.wait(timeout=5)
        return replace(source, target)

    monkeypatch.setattr(session_dependencies.os, "replace", publish_together)
    contexts = []

    async def start(**kwargs):
        contexts.append(kwargs["request_context"])
        return BackgroundSubAgentStartBlocked(limit=2)

    tools, parent_context = _tools(
        tmp_path,
        monkeypatch,
        {"shared": definition},
        start,
    )
    responses = await asyncio.gather(
        *(
            tools["start_subagent"](expert_name="shared", objective="Inspect")
            for _ in range(2)
        ),
    )
    assert [
        json.loads(response.content[0]["text"])["status"]
        for response in responses
    ] == [
        "blocked",
        "blocked",
    ]
    assert len(contexts) == 2
    roots = [
        Path(context["_expert_dependency_view_root"]) for context in contexts
    ]
    assert roots[0] == roots[1]
    assert (roots[0] / "skills/frozen/SKILL.md").read_text() == "shared"
    assert parent_context["_expert_dependency_view_root"] == "stale"
    assert (
        list(
            roots[0].parent.glob(f".{definition.agent_owned.definition_id}-*"),
        )
        == []
    )
