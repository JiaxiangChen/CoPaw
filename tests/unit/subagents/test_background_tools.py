# -*- coding: utf-8 -*-
"""Background SubAgent tool factory tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe.agents.tools.subagent_background import (
    _compact_agent_result,
    create_background_subagent_tools,
)
from swe.app.subagents import BackgroundSubAgentStartBlocked
from swe.app.subagents.models import AgentError
from swe.app.subagents import (
    AgentResult,
    AgentRegistry,
    AgentOwnedDefinitionRepository,
    BackgroundSubAgentScope,
    BackgroundSubAgentSupervisor,
    DelegationSpec,
    BackgroundSubAgentNotManageable,
    DefinitionMatchMetadata,
    PerRunSubAgentRunStore,
    PermissionPolicy,
    SubAgentStartRequest,
    SubAgentDefinition,
    builtin_definition_provider,
)
from swe.config.config import (
    AgentProfileConfig,
    MCPClientConfig,
    MCPConfig,
    ToolsConfig,
)


def _agent_config(tmp_path: Path) -> AgentProfileConfig:
    return AgentProfileConfig(
        id="agent-1",
        name="Agent",
        workspace_dir=str(tmp_path),
    )


def _enable_agent_definition(tmp_path: Path, name: str = "plan-researcher"):
    repository = AgentOwnedDefinitionRepository(
        tmp_path / "agents",
        owner_scope="tenant-1/agent-1",
    )
    package = repository.create(
        {
            "name": name,
            "description": f"{name} definition.",
            "instruction": "Inspect the requested task.",
            "enabled": True,
        },
    )
    repository.enable(
        package.definition_id,
        expected_revision=package.revision,
    )
    return package


class _FakeProcess:
    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self):
        return self.returncode


class _FakePopenFactory:
    def __init__(self):
        self.processes: list[_FakeProcess] = []

    def __call__(self, _command, **_kwargs):
        process = _FakeProcess(pid=4321 + len(self.processes))
        self.processes.append(process)
        return process


@pytest.mark.asyncio
async def test_start_subagent_returns_blocked_without_run_file(tmp_path):
    _enable_agent_definition(tmp_path)
    supervisor = SimpleNamespace()
    supervisor.start = _AsyncReturn(
        BackgroundSubAgentStartBlocked(
            limit=1,
            active_run_ids=["subagent-running"],
        ),
    )
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
        expert_discovery_enabled=True,
    )

    response = await tools["start_subagent"](
        expert_name="plan-researcher",
        objective="Inspect",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["status"] == "blocked"
    assert payload["reason"] == "background_subagent_concurrency_limit"
    assert "run_id" not in json.dumps(payload)
    assert "active_run_ids" not in json.dumps(payload)
    assert not (tmp_path / "subagent_runs").exists()


@pytest.mark.asyncio
async def test_start_subagent_projects_only_owned_active_nicknames(tmp_path):
    package = _enable_agent_definition(tmp_path)
    run_store_dir = tmp_path / "runs"
    record = await PerRunSubAgentRunStore(run_store_dir).create(
        DelegationSpec(
            name="plan-researcher",
            objective="Earlier inspection",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        package.definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
    )
    supervisor = SimpleNamespace(
        start=_AsyncReturn(
            BackgroundSubAgentStartBlocked(
                limit=1,
                active_run_ids=[record.run_id, "foreign"],
            ),
        ),
    )
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(run_store_dir),
        },
        expert_discovery_enabled=True,
    )

    payload = json.loads(
        (
            await tools["start_subagent"](
                expert_name="plan-researcher",
                objective="Inspect",
            )
        ).content[0]["text"],
    )

    assert payload["active_nicknames"] == ["研究员"]
    assert "run_id" not in json.dumps(payload)
    assert "active_run_ids" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_wait_subagent_returns_compact_snapshot(tmp_path, monkeypatch):
    supervisor = SimpleNamespace()
    supervisor.wait = _AsyncReturn(
        SimpleNamespace(
            active_runs=[
                SimpleNamespace(
                    run_id="subagent-active",
                    status="running",
                    spec=SimpleNamespace(
                        name="plan-researcher",
                        objective="Inspect",
                    ),
                    result=None,
                    errors=[],
                    model_dump=lambda mode="json": {},
                ),
            ],
            terminal_runs=[],
            timed_out=True,
        ),
    )
    _mock_owned_wait_records(monkeypatch, supervisor.wait.value)
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
    )

    response = await tools["wait_subagent"](timeout_ms=1)
    payload = json.loads(response.content[0]["text"])

    assert payload["timed_out"] is True
    assert payload["active_subagents"][0]["expert_name"] == "plan-researcher"
    assert "run_id" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_wait_subagent_returns_parent_facing_partial_result(
    tmp_path,
    monkeypatch,
):
    supervisor = SimpleNamespace()
    supervisor.wait = _AsyncReturn(
        SimpleNamespace(
            active_runs=[],
            terminal_runs=[
                SimpleNamespace(
                    run_id="subagent-done",
                    status="partial",
                    spec=SimpleNamespace(
                        name="plan-researcher",
                        objective="Inspect",
                    ),
                    nickname="研究员",
                    result=AgentResult(
                        task_id="task-1",
                        agent_run_id="subagent-done",
                        agent_name="plan-researcher",
                        status="partial",
                        summary="partial research",
                        errors=[
                            AgentError(
                                code="text_finalization_failed",
                                message="finalization timed out",
                            ),
                        ],
                    ),
                    errors=[],
                    worker=SimpleNamespace(
                        pid=123,
                        stderr_log_path="/tmp/secret.stderr.log",
                    ),
                    definition_match=DefinitionMatchMetadata(
                        matched=True,
                        definition_name="plan-researcher",
                    ),
                ),
            ],
            timed_out=False,
        ),
    )
    _mock_owned_wait_records(monkeypatch, supervisor.wait.value)
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
    )

    response = await tools["wait_subagent"](timeout_ms=1)
    payload = json.loads(response.content[0]["text"])
    terminal = payload["terminal_subagents"][0]

    assert terminal == {
        "status": "partial",
        "expert_name": "plan-researcher",
        "nickname": "研究员",
        "objective": "Inspect",
        "result": {
            "summary": "partial research",
            "error_code": "text_finalization_failed",
        },
    }


def test_compact_result_omits_non_finalization_error_code() -> None:
    """A turn-limit partial result exposes only its usable summary."""
    result = AgentResult(
        task_id="task-1",
        agent_run_id="subagent-done",
        agent_name="plan-researcher",
        status="partial",
        summary="bounded research",
        errors=[
            AgentError(
                code="research_turn_limit_reached",
                message="The research turn budget was exhausted.",
            ),
        ],
    )

    assert _compact_agent_result(result) == {"summary": "bounded research"}


@pytest.mark.asyncio
async def test_wait_subagent_returns_error_summary_for_failed_no_result_run(
    tmp_path,
    monkeypatch,
):
    supervisor = SimpleNamespace()
    supervisor.wait = _AsyncReturn(
        SimpleNamespace(
            active_runs=[],
            terminal_runs=[
                SimpleNamespace(
                    run_id="subagent-failed",
                    status="failed",
                    spec=SimpleNamespace(
                        name="plan-researcher",
                        objective="Inspect",
                    ),
                    nickname=None,
                    result=None,
                    errors=[
                        SimpleNamespace(
                            code="worker_exited_without_result",
                            message="Worker exited without producing a result.",
                        ),
                    ],
                ),
            ],
            timed_out=False,
        ),
    )
    _mock_owned_wait_records(monkeypatch, supervisor.wait.value)
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
    )

    response = await tools["wait_subagent"](timeout_ms=1)
    payload = json.loads(response.content[0]["text"])
    terminal = payload["terminal_subagents"][0]

    assert terminal["result"] == {
        "status": "failed",
        "summary": "SubAgent execution failed.",
    }
    assert "errors" not in terminal


@pytest.mark.asyncio
async def test_wait_subagent_does_not_duplicate_error_code_in_summary(
    monkeypatch,
    tmp_path,
):
    from swe.app.subagents import supervisor as supervisor_module
    from swe.providers.models import ModelSlotConfig

    monkeypatch.setattr(
        supervisor_module,
        "capture_model_launch_snapshot",
        lambda **_kwargs: (
            None,
            ModelSlotConfig(provider_id="test", model="test-model"),
        ),
    )
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=1,
        registry=AgentRegistry([builtin_definition_provider()]),
        popen_factory=popen_factory,
    )
    run_store_dir = tmp_path / "subagent_runs"
    scope = BackgroundSubAgentScope(
        tenant_id="tenant-1",
        agent_id="agent-1",
        run_store_dir=run_store_dir,
    )
    started = await supervisor.start(
        scope=scope,
        spec=DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )
    assert started.status == "running"
    popen_factory.processes[0].returncode = 1

    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(run_store_dir),
        },
    )

    response = await tools["wait_subagent"](timeout_ms=1)
    payload = json.loads(response.content[0]["text"])
    terminal = payload["terminal_subagents"][0]

    assert terminal["result"] == {
        "status": "failed",
        "summary": "SubAgent execution failed.",
    }
    assert "errors" not in terminal


@pytest.mark.asyncio
async def test_start_subagent_serializes_real_run_record(tmp_path):
    package = _enable_agent_definition(tmp_path)
    definition = package.definition
    record = await PerRunSubAgentRunStore(tmp_path / "runs").create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
        ),
        definition,
        PermissionPolicy.readonly(),
        start_request=SubAgentStartRequest.model_validate(
            {
                "name": "plan-researcher",
                "instruction": "Act as a readonly planning researcher.",
                "objective": "Inspect",
            },
        ),
        definition_match=DefinitionMatchMetadata(
            matched=True,
            definition_name="plan-researcher",
            definition_source="builtin",
            score=1.0,
        ),
        nickname="研究员",
    )
    supervisor = SimpleNamespace()
    supervisor.start = _AsyncReturn(record)
    request_context = {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "selected_expert_execution": True,
        "_subagent_definition_store_dir": str(tmp_path / "definitions"),
    }
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context=request_context,
        expert_discovery_enabled=True,
    )

    response = await tools["start_subagent"](
        expert_name="plan-researcher",
        objective="Inspect",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["expert_name"] == "plan-researcher"
    assert payload["accepted"] is True
    assert payload["nickname"] == "研究员"
    assert "run_id" not in json.dumps(payload)
    assert "definition_match" not in payload
    assert "worker" not in payload


@pytest.mark.asyncio
async def test_start_subagent_respects_disabled_parent_readonly_tools(
    tmp_path,
):
    _enable_agent_definition(tmp_path)
    captured = {}

    async def _start(**kwargs):
        captured["parent_policy"] = kwargs["parent_policy"]
        return BackgroundSubAgentStartBlocked(limit=1)

    config = _agent_config(tmp_path)
    config.tools = ToolsConfig()
    config.tools.builtin_tools["execute_shell_command"].enabled = False
    supervisor = SimpleNamespace(start=_start)
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=config,
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
        expert_discovery_enabled=True,
    )

    await tools["start_subagent"](
        expert_name="plan-researcher",
        objective="Inspect",
    )

    assert "execute_shell_command" not in captured["parent_policy"].tools.allow
    assert "execute_shell_command" in captured["parent_policy"].tools.deny


@pytest.mark.asyncio
async def test_start_subagent_rejects_unlisted_expert(
    tmp_path,
):
    supervisor = SimpleNamespace()
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
    )

    response = await tools["start_subagent"](
        expert_name="aum-customer-analyst",
        objective="Analyze 1M AUM customer maintenance.",
        background="Need structured advice.",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload == {
        "status": "not_found",
        "reason": "subagent_definition_not_found",
        "expert_name": "aum-customer-analyst",
    }


@pytest.mark.asyncio
async def test_start_subagent_resolves_skill_owned_definition_without_instruction(
    tmp_path: Path,
):
    captured = {}

    async def _start(**kwargs):
        captured.update(kwargs)
        return BackgroundSubAgentStartBlocked(limit=1)

    agents_dir = tmp_path / "skills" / "security" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.toml").write_text(
        'name = "reviewer"\n'
        'description = "Review code for security regressions."\n'
        'instruction = "Inspect evidence."\n'
        'trigger_keywords = ["security", "review"]\n',
        encoding="utf-8",
    )
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(start=_start),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
        effective_skill_names=["security"],
    )

    response = await tools["start_subagent"](
        expert_name="security:reviewer",
        objective="Review this payment patch.",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["status"] == "blocked"
    assert captured["definition"].name == "security:reviewer"
    assert captured["definition"].instruction == "Inspect evidence."
    assert captured["definition_match"].reason == "exact_name"
    assert "security:reviewer" in tools["start_subagent"].__doc__
    assert "security, review" in tools["start_subagent"].__doc__


@pytest.mark.asyncio
async def test_selected_expert_restricts_catalog_to_one_agent_owned_definition(
    tmp_path: Path,
):
    captured = {}

    async def _start(**kwargs):
        captured.update(kwargs)
        return BackgroundSubAgentStartBlocked(limit=1)

    repository = AgentOwnedDefinitionRepository(
        tmp_path / "agents",
        owner_scope="tenant-1/agent-1",
    )
    package = repository.create(
        {
            "name": "researcher",
            "description": "Research expert.",
            "instruction": "Research the requested topic.",
            "enabled": True,
        },
    )
    repository.enable(
        package.definition_id,
        expected_revision=package.revision,
    )
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(start=_start),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
        },
        selected_expert_id=package.definition_id,
    )

    response = await tools["start_subagent"](
        expert_name="researcher",
        objective="Research this topic.",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["status"] == "blocked"
    assert captured["definition"].name == "researcher"
    assert "researcher" in tools["start_subagent"].__doc__
    assert "plan-researcher" not in tools["start_subagent"].__doc__

    response = await tools["start_subagent"](
        expert_name="plan-researcher",
        objective="Use a different expert.",
    )
    payload = json.loads(response.content[0]["text"])
    assert payload == {
        "status": "not_found",
        "reason": "selected_expert_not_available",
        "expert_name": "plan-researcher",
    }


@pytest.mark.asyncio
async def test_skill_toml_subagent_launches_with_declared_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Skill TOML definition reaches a worker with only declared inputs."""
    from swe.app.subagents import supervisor as supervisor_module
    from swe.app.subagents.worker import load_launch_spec
    from swe.providers.models import ModelSlotConfig

    monkeypatch.setattr(
        supervisor_module,
        "capture_model_launch_snapshot",
        lambda **_kwargs: (
            None,
            ModelSlotConfig(provider_id="parent", model="parent-model"),
        ),
    )
    skill_dir = tmp_path / "skills" / "quality"
    agents_dir = skill_dir / "agents"
    agents_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Quality\n", encoding="utf-8")
    (agents_dir / "patch-editor.toml").write_text(
        'name = "patch-editor"\n'
        'description = "Edit a requested patch and report the result."\n'
        'instruction = "Edit only files in the requested scope."\n'
        'skills = ["quality"]\n'
        'mcps = ["github"]\n'
        'trigger_keywords = ["patch", "edit"]\n'
        "\n"
        "[model]\n"
        'provider = "openai"\n'
        'id = "gpt-5-mini"\n',
        encoding="utf-8",
    )
    config = _agent_config(tmp_path)
    config.tools = ToolsConfig()
    config.mcp = MCPConfig(
        clients={
            "github": MCPClientConfig(name="github", command="github-mcp"),
            "undeclared": MCPClientConfig(
                name="undeclared",
                command="undeclared-mcp",
            ),
        },
    )
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=popen_factory,
    )
    run_store_dir = tmp_path / "subagent_runs"
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=config,
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "_subagent_run_store_dir": str(run_store_dir),
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
        effective_skill_names=["quality"],
    )

    response = await tools["start_subagent"](
        expert_name="quality:patch-editor",
        objective="Update the requested patch.",
        background="Keep the change minimal.",
    )
    payload = json.loads(response.content[0]["text"])
    records = list(run_store_dir.glob("subagent-*.json"))
    records = [
        path for path in records if not path.name.endswith(".launch.json")
    ]
    assert len(records) == 1
    record_id = records[0].stem
    record = await PerRunSubAgentRunStore(run_store_dir).get(record_id)
    assert record is not None
    launch_spec = load_launch_spec(
        run_store_dir / f"{record.run_id}.launch.json",
    )

    assert payload["accepted"] is True
    assert record is not None
    assert record.definition_name == "quality:patch-editor"
    assert record.definition_match.reason == "exact_name"
    assert {"write_file", "edit_file"} <= set(
        record.effective_policy.tools.allow,
    )
    assert launch_spec.definition.instruction == (
        "Edit only files in the requested scope."
    )
    assert launch_spec.definition.skill_owned.model.id == "gpt-5-mini"
    assert launch_spec.launch_snapshot.skill_snapshot_dirs == [
        str(run_store_dir / f"{record.run_id}.skills" / "quality"),
    ]
    assert launch_spec.launch_diagnostics.loaded_skills == ["quality"]
    assert launch_spec.launch_diagnostics.snapshotted_mcps == ["github"]
    assert launch_spec.launch_snapshot.private_mcp_snapshot_path is not None
    mcp_snapshot = json.loads(
        Path(
            launch_spec.launch_snapshot.private_mcp_snapshot_path,
        ).read_text(),
    )
    assert set(mcp_snapshot) == {"github"}


@pytest.mark.asyncio
async def test_start_subagent_rejects_missing_expert_name(tmp_path):
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
    )

    with pytest.raises(TypeError):
        await tools["start_subagent"](objective="Inspect")


@pytest.mark.asyncio
async def test_start_subagent_rejects_old_agent_name_field(tmp_path):
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
    )

    with pytest.raises(TypeError):
        await tools["start_subagent"](
            agent_name="legacy",
            objective="Inspect",
        )


@pytest.mark.asyncio
async def test_start_subagent_returns_not_found_for_unknown_expert(tmp_path):
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
    )

    response = await tools["start_subagent"](
        expert_name="bad",
        objective="Inspect",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload == {
        "status": "not_found",
        "reason": "subagent_definition_not_found",
        "expert_name": "bad",
    }


@pytest.mark.asyncio
async def test_register_subagent_definition_is_not_exposed_to_main_agent(
    tmp_path,
):
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
    )

    assert "register_subagent_definition" not in tools


@pytest.mark.asyncio
async def test_start_subagent_uses_matched_definition(tmp_path):
    captured = {}

    async def _start(**kwargs):
        captured.update(kwargs)
        return BackgroundSubAgentStartBlocked(limit=1)

    repository = AgentOwnedDefinitionRepository(
        tmp_path / "agents",
        owner_scope="tenant-1/agent-1",
    )
    package = repository.create(
        {
            "name": "risk-reviewer",
            "description": "Review risks.",
            "instruction": "Inspect the plan for risks.",
            "enabled": True,
        },
    )
    repository.enable(
        package.definition_id,
        expected_revision=package.revision,
    )
    supervisor = SimpleNamespace(start=_start)
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "_subagent_definition_store_dir": str(tmp_path / "definitions"),
        },
        expert_discovery_enabled=True,
    )

    response = await tools["start_subagent"](
        expert_name="risk-reviewer",
        objective="Review risk in this plan.",
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["status"] == "blocked"
    assert captured["definition"].name == "risk-reviewer"
    assert captured["definition"].source == "agent_owned"
    assert captured["definition"].instruction != (
        "This should not override the matched definition."
    )
    assert isinstance(captured["definition_match"], DefinitionMatchMetadata)
    assert captured["definition_match"].matched is True


@pytest.mark.asyncio
async def test_get_and_cancel_unknown_nickname_return_not_found(tmp_path):
    supervisor = SimpleNamespace()
    supervisor.get = _AsyncRaise(ValueError("bad run id"))
    supervisor.cancel = _AsyncRaise(ValueError("bad run id"))
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={"tenant_id": "tenant-1", "agent_id": "agent-1"},
    )

    get_response = await tools["get_subagent"]("../other")
    cancel_response = await tools["cancel_subagent"]("../other")

    assert json.loads(get_response.content[0]["text"]) == {
        "status": "not_found",
        "nickname": "../other",
    }
    assert json.loads(cancel_response.content[0]["text"]) == {
        "status": "not_found",
        "nickname": "../other",
    }


@pytest.mark.asyncio
async def test_get_subagent_uses_nickname_without_worker_details(tmp_path):
    run_store_dir = tmp_path / "runs"
    definition = _enable_agent_definition(tmp_path).definition
    store = PerRunSubAgentRunStore(run_store_dir)
    record = await store.create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
    )
    stderr_path = run_store_dir / f"{record.run_id}.stderr.log"
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text("x" * 5000, encoding="utf-8")
    await store.mark_running(
        record.run_id,
        worker_pid=123,
        stderr_log_path=str(stderr_path),
    )
    failed = await store.fail(record.run_id, "boom")
    supervisor = SimpleNamespace()
    supervisor.get = _AsyncReturn(failed)
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(run_store_dir),
        },
    )

    response = await tools["get_subagent"](
        "研究员",
        include_details=True,
    )
    payload = json.loads(response.content[0]["text"])

    assert payload["nickname"] == "研究员"
    assert payload["expert_name"] == "plan-researcher"
    assert "stderr_log_path" not in json.dumps(payload)
    assert "worker" not in payload


@pytest.mark.asyncio
async def test_get_subagent_hides_launch_diagnostics_even_in_details(
    tmp_path,
):
    run_store_dir = tmp_path / "runs"
    definition = _enable_agent_definition(tmp_path).definition
    store = PerRunSubAgentRunStore(run_store_dir)
    record = await store.create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
        launch_diagnostics={
            "loaded_skills": ["quality"],
        },
    )
    supervisor = SimpleNamespace(
        get=_AsyncReturn(record),
        is_manageable=lambda _scope, _run_id: False,
    )
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(run_store_dir),
        },
    )

    compact = json.loads(
        (await tools["get_subagent"]("研究员")).content[0]["text"],
    )
    detailed = json.loads(
        (await tools["get_subagent"]("研究员", include_details=True)).content[
            0
        ]["text"],
    )

    assert "launch_diagnostics" not in compact
    assert "launch_diagnostics" not in detailed


@pytest.mark.asyncio
async def test_get_subagent_defaults_to_parent_facing_partial_projection(
    tmp_path,
):
    definition = _enable_agent_definition(tmp_path).definition
    store = PerRunSubAgentRunStore(tmp_path / "runs")
    record = await store.create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
    )
    partial = await store.finish(
        record.run_id,
        AgentResult(
            task_id="task-1",
            agent_run_id=record.run_id,
            agent_name="plan-researcher",
            status="partial",
            summary="partial research",
        ),
    )
    supervisor = SimpleNamespace()
    supervisor.get = _AsyncReturn(partial)
    supervisor.is_manageable = lambda _scope, _run_id: False
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(tmp_path / "runs"),
        },
    )

    response = await tools["get_subagent"]("研究员")
    payload = json.loads(response.content[0]["text"])

    assert payload["status"] == "partial"
    assert payload["result"] == {"summary": "partial research"}
    assert "definition_match" not in payload
    assert "worker" not in payload
    assert "errors" not in payload


@pytest.mark.asyncio
async def test_get_subagent_returns_error_summary_for_failed_no_result_run(
    tmp_path,
):
    definition = _enable_agent_definition(tmp_path).definition
    store = PerRunSubAgentRunStore(tmp_path / "runs")
    record = await store.create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
    )
    failed = await store.fail(record.run_id, "worker failed to start")
    supervisor = SimpleNamespace()
    supervisor.get = _AsyncReturn(failed)
    supervisor.is_manageable = lambda _scope, _run_id: False
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(tmp_path / "runs"),
        },
    )

    response = await tools["get_subagent"]("研究员")
    payload = json.loads(response.content[0]["text"])

    assert payload["result"] == {
        "status": "failed",
        "summary": "SubAgent execution failed.",
    }
    assert "errors" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_subagent", "cancel_subagent"])
async def test_supervisor_operation_errors_do_not_leak_private_details(
    tmp_path,
    method,
):
    run_store_dir = tmp_path / "runs"
    definition = _enable_agent_definition(tmp_path).definition
    store = PerRunSubAgentRunStore(run_store_dir)
    await store.create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
    )

    async def broken_operation(*_args, **_kwargs):
        raise RuntimeError(
            "run_id=subagent-secret pid=421 worker=/private/secret.log",
        )

    supervisor = SimpleNamespace(
        **{"get" if method == "get_subagent" else "cancel": broken_operation},
    )
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(run_store_dir),
        },
    )

    payload = json.loads(
        (await tools[method]("研究员")).content[0]["text"],
    )

    assert payload == {
        "status": "failed",
        "reason": "subagent_operation_failed",
        "nickname": "研究员",
    }
    rendered = json.dumps(payload)
    for private_value in (
        "subagent-secret",
        "pid",
        "worker",
        "/private/secret.log",
    ):
        assert private_value not in rendered


@pytest.mark.asyncio
async def test_failed_record_summary_does_not_leak_persisted_error_details(
    tmp_path,
):
    run_store_dir = tmp_path / "runs"
    definition = _enable_agent_definition(tmp_path).definition
    store = PerRunSubAgentRunStore(run_store_dir)
    record = await store.create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
    )
    failed = await store.fail(
        record.run_id,
        "worker pid=421 run_id=subagent-secret path=/private/secret.log",
    )
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(get=_AsyncReturn(failed)),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(run_store_dir),
        },
    )

    payload = json.loads(
        (await tools["get_subagent"]("研究员")).content[0]["text"],
    )

    assert payload["result"] == {
        "status": "failed",
        "summary": "SubAgent execution failed.",
    }
    rendered = json.dumps(payload)
    for private_value in (
        "subagent-secret",
        "pid",
        "worker",
        "/private/secret.log",
    ):
        assert private_value not in rendered


@pytest.mark.asyncio
async def test_failed_agent_result_projection_redacts_raw_summary_and_errors(
    tmp_path,
):
    run_store_dir = tmp_path / "runs"
    definition = _enable_agent_definition(tmp_path).definition
    store = PerRunSubAgentRunStore(run_store_dir)
    record = await store.create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
    )
    raw_failure = AgentResult(
        task_id="task-1",
        agent_run_id=record.run_id,
        agent_name="plan-researcher",
        status="failed",
        summary=(
            "run_id=subagent-secret pid=421 worker=/private/secret.log "
            "api_key=top-secret"
        ),
        errors=[
            AgentError(
                code="runtime_error",
                message="token=top-secret process=421 path=/private/secret.log",
            ),
        ],
    )
    failed = record.model_copy(
        update={"status": "failed", "result": raw_failure},
    )
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(get=_AsyncReturn(failed)),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(run_store_dir),
        },
    )

    payload = json.loads(
        (await tools["get_subagent"]("研究员")).content[0]["text"],
    )

    assert payload["result"] == {"summary": "SubAgent execution failed."}
    rendered = json.dumps(payload)
    for private_value in (
        "subagent-secret",
        "pid",
        "worker",
        "/private/secret.log",
        "top-secret",
    ):
        assert private_value not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_subagent", "cancel_subagent"])
async def test_nickname_resolution_errors_return_safe_failure(
    tmp_path,
    monkeypatch,
    method,
):
    def corrupt_lookup(*_args, **_kwargs):
        raise RuntimeError(
            "run_id=subagent-secret pid=421 worker=/private/secret.log",
        )

    monkeypatch.setattr(
        "swe.agents.tools.subagent_background.read_chat_run_by_nickname",
        corrupt_lookup,
    )
    tools = create_background_subagent_tools(
        supervisor=SimpleNamespace(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={"tenant_id": "tenant-1", "agent_id": "agent-1"},
    )

    payload = json.loads(
        (await tools[method]("研究员")).content[0]["text"],
    )

    assert payload == {
        "status": "failed",
        "reason": "subagent_operation_failed",
        "nickname": "研究员",
    }


@pytest.mark.asyncio
async def test_cancel_not_manageable_returns_persisted_safe_projection(
    tmp_path,
):
    run_store_dir = tmp_path / "runs"
    definition = _enable_agent_definition(tmp_path).definition
    store = PerRunSubAgentRunStore(run_store_dir)
    record = await store.create(
        DelegationSpec(
            name="plan-researcher",
            objective="Inspect",
            parent_thread_id="session-1",
            parent_chat_id="chat-1",
        ),
        definition,
        PermissionPolicy.readonly(),
        nickname="研究员",
    )
    supervisor = SimpleNamespace(
        cancel=_AsyncReturn(
            BackgroundSubAgentNotManageable(run_id=record.run_id),
        ),
    )
    tools = create_background_subagent_tools(
        supervisor=supervisor,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        request_context={
            "tenant_id": "tenant-1",
            "agent_id": "agent-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "_subagent_run_store_dir": str(run_store_dir),
        },
    )

    payload = json.loads(
        (await tools["cancel_subagent"]("研究员")).content[0]["text"],
    )

    assert payload == {
        "expert_name": "plan-researcher",
        "nickname": "研究员",
        "status": "pending",
        "objective": "Inspect",
        "manageable": False,
    }
    assert "run_id" not in json.dumps(payload)


def _mock_owned_wait_records(monkeypatch, snapshot):
    """Keep serializer fixtures at the authorized-record boundary."""
    from swe.app.subagents.models import BackgroundSubAgentRunRecord

    records = [
        BackgroundSubAgentRunRecord(
            run_id=record.run_id,
            status=record.status,
            spec=DelegationSpec(
                name=record.spec.name,
                objective=record.spec.objective,
                parent_thread_id="session-1",
            ),
            definition_name=record.spec.name,
            definition_version="1",
            definition_source="run_scoped",
            owner_scope="run:test",
            effective_policy=PermissionPolicy.readonly(),
            nickname=getattr(record, "nickname", None),
            result=record.result,
            errors=[vars(error) for error in record.errors],
        )
        for record in [*snapshot.active_runs, *snapshot.terminal_runs]
    ]
    monkeypatch.setattr(
        "swe.agents.tools.subagent_background.read_chat_run_records",
        lambda *_args: records,
    )


class _AsyncReturn:
    def __init__(self, value):
        self.value = value

    async def __call__(self, *args, **kwargs):
        return self.value


class _AsyncRaise:
    def __init__(self, error):
        self.error = error

    async def __call__(self, *args, **kwargs):
        raise self.error
