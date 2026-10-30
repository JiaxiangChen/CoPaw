# -*- coding: utf-8 -*-
"""Background SubAgent worker entrypoint tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swe.app.subagents import (
    AgentRegistry,
    AgentResult,
    DefinitionMatchMetadata,
    DelegationSpec,
    PerRunSubAgentRunStore,
    PermissionPolicy,
    SkillOwnedDefinitionMetadata,
    SubAgentStartRequest,
    WorkerLaunchSpec,
    builtin_definition_provider,
)
from swe.config.config import AgentProfileConfig
from swe.app.subagents.launch_snapshot import (
    capture_model_launch_snapshot,
    capture_launch_dependencies,
    read_and_remove_private_model_snapshot,
    read_and_remove_private_mcp_snapshot,
    resolve_skill_owned_model_slot,
)
from swe.app.subagents.models import AgentOwnedDefinitionMetadata
from swe.app.subagents.models import SubAgentDefinition
from swe.config.config import MCPClientConfig, MCPConfig
from swe.app import subagents as subagents_module


def _definition():
    registry = AgentRegistry([builtin_definition_provider()])
    return registry.resolve("plan-researcher")


def _spec() -> DelegationSpec:
    return DelegationSpec(
        task_id="task-1",
        parent_thread_id="session-1",
        name="plan-researcher",
        objective="Inspect worker behavior",
    )


def _agent_config(tmp_path: Path) -> AgentProfileConfig:
    return AgentProfileConfig(
        id="default",
        name="Default",
        workspace_dir=str(tmp_path),
    )


def _result(run_id: str, *, status: str = "completed") -> AgentResult:
    return AgentResult(
        task_id="task-1",
        agent_run_id=run_id,
        agent_name="plan-researcher",
        status=status,
        summary="worker completed",
    )


async def _write_launch_spec(tmp_path: Path) -> tuple[Path, str, Path]:
    run_store_dir = tmp_path / "subagent_runs"
    store = PerRunSubAgentRunStore(run_store_dir)
    definition = _definition()
    start_request = SubAgentStartRequest.model_validate(
        {
            "name": "plan-researcher",
            "instruction": "Research worker behavior.",
            "objective": "Inspect worker behavior",
        },
    )
    definition_match = DefinitionMatchMetadata(
        matched=True,
        definition_name="plan-researcher",
        definition_source="builtin",
        score=1.0,
    )
    record = await store.create(
        _spec(),
        definition,
        PermissionPolicy.readonly(),
        start_request=start_request,
        definition_match=definition_match,
        nickname="研究员",
    )
    launch = WorkerLaunchSpec(
        run_id=record.run_id,
        run_store_dir=str(run_store_dir),
        workspace_dir=str(tmp_path / "workspace"),
        parent_agent_config=_agent_config(tmp_path).model_dump(mode="json"),
        definition=definition,
        delegation_spec=record.spec,
        effective_policy=record.effective_policy,
        start_request=record.start_request,
        definition_match=record.definition_match,
        nickname=record.nickname,
        request_context={
            "session_id": "session-1",
            "OPENAI_API_KEY": "must-not-persist",
            "_hook_overlay_model": object(),
        },
        stderr_log_path=str(run_store_dir / f"{record.run_id}.stderr.log"),
    )
    launch_path = run_store_dir / f"{record.run_id}.launch.json"
    launch_path.write_text(
        json.dumps(launch.model_dump(mode="json")),
        encoding="utf-8",
    )
    return launch_path, record.run_id, run_store_dir


def test_launch_spec_filters_secret_like_context(tmp_path):
    spec = WorkerLaunchSpec(
        run_id="subagent-test",
        run_store_dir=str(tmp_path / "runs"),
        workspace_dir=str(tmp_path),
        parent_agent_config={
            **_agent_config(tmp_path).model_dump(mode="json"),
            "providers": {
                "OPENAI_API_KEY": "secret",
                "nested": {"client_secret": "secret"},
            },
        },
        definition=_definition(),
        delegation_spec=_spec(),
        effective_policy=PermissionPolicy.readonly(),
        request_context={
            "session_id": "session-1",
            "tenant_id": "tenant-1",
            "OPENAI_API_KEY": "secret",
            "SWE_PROVIDER_API_KEY": "secret",
        },
        OPENAI_API_KEY="secret",
    )

    payload = spec.model_dump_json()

    assert "session-1" in payload
    assert "tenant-1" in payload
    assert "OPENAI_API_KEY" not in payload
    assert "SWE_PROVIDER_API_KEY" not in payload
    assert "client_secret" not in payload
    assert "secret" not in payload


@pytest.mark.parametrize("enabled", [True, False, "false"])
def test_launch_spec_preserves_boolean_plan_restriction(tmp_path, enabled):
    from swe.app.subagents.models import SubAgentDefinition

    spec = WorkerLaunchSpec(
        run_id="subagent-test",
        run_store_dir=str(tmp_path / "runs"),
        workspace_dir=str(tmp_path),
        parent_agent_config={},
        definition=SubAgentDefinition(
            name="researcher",
            source="run_scoped",
            owner_scope="run:test",
            instruction="Research",
            description="Research task",
        ),
        delegation_spec=_spec(),
        effective_policy=PermissionPolicy.readonly(),
        request_context={
            "plan_mode_enabled": enabled,
            "enable_subagents": True,
        },
    )
    assert spec.request_context["plan_mode_enabled"] is (enabled is True)
    assert "enable_subagents" not in spec.request_context


def test_launch_spec_removes_full_parent_mcp_configuration(tmp_path) -> None:
    """Only the private one-shot snapshot may carry MCP credentials."""
    spec = WorkerLaunchSpec(
        run_id="subagent-test",
        run_store_dir=str(tmp_path / "runs"),
        workspace_dir=str(tmp_path),
        parent_agent_config={
            **_agent_config(tmp_path).model_dump(mode="json"),
            "mcp": {
                "clients": {
                    "unrelated": {
                        "name": "unrelated",
                        "headers": {"Authorization": "Bearer secret"},
                    },
                },
            },
        },
        definition=_definition(),
        delegation_spec=_spec(),
        effective_policy=PermissionPolicy.readonly(),
    )

    assert "mcp" not in spec.parent_agent_config


def test_private_mcp_snapshot_rejects_path_outside_run_store(tmp_path) -> None:
    """A launch spec cannot trick the worker into deleting another file."""
    external = tmp_path / "external.mcp.json"
    external.write_text('{"x": 1}', encoding="utf-8")

    assert (
        read_and_remove_private_mcp_snapshot(
            str(external),
            run_store_dir=tmp_path / "runs",
        )
        == {}
    )
    assert external.exists()


def test_private_mcp_snapshot_requires_its_own_run_id(tmp_path) -> None:
    run_store = tmp_path / "runs"
    run_store.mkdir()
    other_run = run_store / ".subagent-other.mcp.json"
    other_run.write_text('{"x": 1}', encoding="utf-8")

    assert (
        read_and_remove_private_mcp_snapshot(
            str(other_run),
            run_store_dir=run_store,
            run_id="subagent-this",
        )
        == {}
    )
    assert other_run.exists()


def test_private_mcp_snapshot_never_overwrites_an_existing_file(
    tmp_path: Path,
) -> None:
    """A reused run id must not truncate a secret chosen by an attacker."""
    from swe.app.subagents import launch_snapshot as snapshot_module

    run_store = tmp_path / "runs"
    run_store.mkdir()
    existing = run_store / ".subagent-one.mcp.json"
    existing.write_text('{"preserve": true}', encoding="utf-8")

    with pytest.raises(FileExistsError):
        snapshot_module._write_private_mcp_snapshot(
            run_store,
            "subagent-one",
            {"github": {"token": "new-secret"}},
        )

    assert existing.read_text(encoding="utf-8") == '{"preserve": true}'


def test_dependency_capture_cleans_copied_skills_when_mcp_snapshot_fails(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "quality"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Quality", encoding="utf-8")
    run_store = tmp_path / "runs"
    run_store.mkdir()
    (run_store / ".run-one.mcp.json").write_text("{}", encoding="utf-8")
    config = _agent_config(tmp_path).model_copy(
        update={
            "mcp": MCPConfig(
                clients={
                    "github": MCPClientConfig(
                        name="github",
                        command="github-mcp",
                    ),
                },
            ),
        },
    )
    definition = _definition().model_copy(
        update={
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="reviewer",
                declared_skills=["quality"],
                declared_mcps=["github"],
            ),
        },
    )

    with pytest.raises(FileExistsError):
        capture_launch_dependencies(
            run_store_dir=run_store,
            run_id="run-one",
            workspace_dir=tmp_path,
            parent_agent_config=config,
            definition=definition,
            effective_skill_names=["quality"],
        )

    assert not (run_store / "run-one.skills").exists()


@pytest.mark.parametrize(
    ("mcps_value", "expected_names"),
    [
        (None, set()),
        (["github"], {"github"}),
        ([], set()),
    ],
)
def test_dependency_capture_only_includes_declared_mcps(
    tmp_path: Path,
    mcps_value: list[str] | None,
    expected_names: set[str],
) -> None:
    config = _agent_config(tmp_path).model_copy(
        update={
            "mcp": MCPConfig(
                clients={
                    "github": MCPClientConfig(
                        name="github",
                        command="github-mcp",
                    ),
                    "calendar": MCPClientConfig(
                        name="calendar",
                        command="calendar-mcp",
                    ),
                },
            ),
        },
    )
    definition = _definition().model_copy(
        update={
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="reviewer",
                declared_mcps=mcps_value,
            ),
        },
    )

    _skills, private_path, diagnostics = capture_launch_dependencies(
        run_store_dir=tmp_path / "runs",
        run_id="run-mcp-policy",
        workspace_dir=tmp_path,
        parent_agent_config=config,
        definition=definition,
        effective_skill_names=[],
    )

    assert set(diagnostics.snapshotted_mcps) == expected_names
    if expected_names:
        assert private_path is not None
        snapshot = json.loads(Path(private_path).read_text(encoding="utf-8"))
        assert set(snapshot) == expected_names
    else:
        assert private_path is None


def test_dependency_capture_uses_parent_snapshot_directory_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SubAgent launch must copy the parent Query's exact Skill root."""
    workspace_skill = tmp_path / "workspace" / "quality"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text(
        "# mutable workspace version",
        encoding="utf-8",
    )
    parent_snapshot_skill = tmp_path / "snapshot" / "quality"
    parent_snapshot_skill.mkdir(parents=True)
    (parent_snapshot_skill / "SKILL.md").write_text(
        "# parent query version",
        encoding="utf-8",
    )
    from swe.agents.skills_manager import _build_signature
    from swe.app.subagents.launch_snapshot import capture_launch_dependencies

    definition = SubAgentDefinition(
        name="quality:reviewer",
        description="reviewer",
        instruction="review",
        source="skill_owned",
        owner_scope="quality",
        skill_owned=SkillOwnedDefinitionMetadata(
            skill_name="quality",
            local_name="reviewer",
            declared_skills=["quality"],
        ),
    )
    monkeypatch.setattr(
        "swe.app.subagents.launch_snapshot.resolve_effective_skill_dir",
        lambda *_args, **_kwargs: pytest.fail(
            "mutable workspace resolution must not be used with a snapshot",
        ),
    )

    dirs, _private_path, diagnostics = capture_launch_dependencies(
        run_store_dir=tmp_path / "runs",
        run_id="run-parent-snapshot",
        workspace_dir=tmp_path / "workspace",
        parent_agent_config=_agent_config(tmp_path),
        definition=definition,
        effective_skill_names=["quality"],
        skill_snapshot_dirs={"quality": parent_snapshot_skill},
        skill_snapshot_signatures={
            "quality": _build_signature(parent_snapshot_skill),
        },
    )

    assert (Path(dirs[0]) / "SKILL.md").read_text(encoding="utf-8") == (
        "# parent query version"
    )
    assert diagnostics.loaded_skills == ["quality"]


def test_dependency_capture_does_not_fallback_when_snapshot_entry_missing(
    tmp_path: Path,
) -> None:
    """Missing parent-snapshot Skills fail closed instead of drifting."""
    workspace_skill = tmp_path / "quality"
    workspace_skill.mkdir()
    (workspace_skill / "SKILL.md").write_text("# workspace", encoding="utf-8")
    definition = SubAgentDefinition(
        name="quality:reviewer",
        description="reviewer",
        instruction="review",
        source="skill_owned",
        owner_scope="quality",
        skill_owned=SkillOwnedDefinitionMetadata(
            skill_name="quality",
            local_name="reviewer",
            declared_skills=["quality"],
        ),
    )

    dirs, _private_path, diagnostics = capture_launch_dependencies(
        run_store_dir=tmp_path / "runs",
        run_id="run-missing-parent-snapshot",
        workspace_dir=tmp_path,
        parent_agent_config=_agent_config(tmp_path),
        definition=definition,
        effective_skill_names=["quality"],
        skill_snapshot_dirs={},
    )

    assert dirs == []
    assert diagnostics.loaded_skills == []
    assert diagnostics.skipped_skills == ["quality"]


def test_private_mcp_snapshot_rejects_a_symlink_before_reading(
    tmp_path: Path,
) -> None:
    """A launch path must not dereference an attacker-owned secret file."""
    run_store = tmp_path / "runs"
    run_store.mkdir()
    external = tmp_path / "external.json"
    external.write_text('{"external": true}', encoding="utf-8")
    snapshot = run_store / ".subagent-one.mcp.json"
    snapshot.symlink_to(external)

    assert (
        read_and_remove_private_mcp_snapshot(
            str(snapshot),
            run_store_dir=run_store,
            run_id="subagent-one",
        )
        == {}
    )
    assert external.exists()


def test_private_mcp_snapshot_rejects_a_symlinked_run_store(
    tmp_path: Path,
) -> None:
    from swe.app.subagents import launch_snapshot as snapshot_module

    external = tmp_path / "external-runs"
    external.mkdir()
    linked_store = tmp_path / "runs"
    linked_store.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        snapshot_module._write_private_mcp_snapshot(
            linked_store,
            "subagent-one",
            {"github": {"token": "secret"}},
        )

    assert not list(external.iterdir())


def test_no_follow_skill_copy_rejects_a_link_inserted_in_package(
    tmp_path: Path,
) -> None:
    """The copy primitive itself, rather than a prior scan, rejects links."""
    from swe.app.subagents import launch_snapshot as snapshot_module

    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "SKILL.md").write_text("# Safe", encoding="utf-8")
    external = tmp_path / "external.md"
    external.write_text("# External", encoding="utf-8")
    (source / "instruction.md").symlink_to(external)

    with pytest.raises(OSError):
        snapshot_module._copy_skill_tree_no_symlinks(source, target)

    assert not target.exists()


def test_model_snapshot_freezes_selected_and_parent_provider_configs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Worker model configuration comes from the private launch snapshot."""
    from swe.app.subagents import launch_snapshot as snapshot_module
    from swe.app.subagents import SkillOwnedModelReference
    from swe.providers.models import ModelSlotConfig

    parent = ModelSlotConfig(provider_id="parent", model="parent-model")
    selected = ModelSlotConfig(provider_id="selected", model="selected-model")

    class Provider:
        def __init__(self, provider_id: str, model: str):
            self.provider_id = provider_id
            self.model = model

        def has_model(self, model_id: str) -> bool:
            return model_id == self.model

        def model_dump(self, **kwargs):
            return {
                "id": self.provider_id,
                "name": self.provider_id,
                "base_url": f"https://{self.provider_id}.initial",
                "api_key": f"{self.provider_id}-secret",
                "models": [{"id": self.model, "name": self.model}],
            }

    class Manager:
        def get_active_model(self):
            return parent

        def get_provider(self, provider_id: str):
            if provider_id == "parent":
                return Provider("parent", "parent-model")
            if provider_id == "selected":
                return Provider("selected", "selected-model")
            return None

    monkeypatch.setattr(
        snapshot_module.ProviderManager,
        "get_instance",
        lambda tenant_id: Manager(),
    )
    definition = _definition().model_copy(
        update={
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="reviewer",
                model=SkillOwnedModelReference(
                    provider="selected",
                    id="selected-model",
                ),
            ),
        },
    )

    path, resolved = capture_model_launch_snapshot(
        tenant_id="tenant-a",
        run_store_dir=tmp_path / "runs",
        run_id="subagent-one",
        definition=definition,
    )

    assert resolved == selected
    assert path is not None
    raw = (tmp_path / "runs" / ".subagent-one.model.json").read_text()
    assert "selected-secret" in raw
    assert read_and_remove_private_model_snapshot(
        path,
        run_store_dir=tmp_path / "runs",
        run_id="subagent-one",
    ) == {
        "selected": {
            "slot": selected.model_dump(),
            "provider": {
                "id": "selected",
                "name": "selected",
                "base_url": "https://selected.initial",
                "api_key": "selected-secret",
                "models": [
                    {"id": "selected-model", "name": "selected-model"},
                ],
            },
        },
        "parent": {
            "slot": parent.model_dump(),
            "provider": {
                "id": "parent",
                "name": "parent",
                "base_url": "https://parent.initial",
                "api_key": "parent-secret",
                "models": [
                    {"id": "parent-model", "name": "parent-model"},
                ],
            },
        },
    }


def test_model_snapshot_never_honors_an_injected_override_for_builtin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Only a Skill-owned Definition may select a model distinct from parent."""
    from swe.app.subagents import launch_snapshot as snapshot_module
    from swe.providers.models import ModelSlotConfig

    parent = ModelSlotConfig(provider_id="parent", model="parent-model")

    class Provider:
        def has_model(self, model_id):
            return True

        def model_dump(self, **kwargs):
            return {"id": "parent", "name": "parent"}

    class Manager:
        def get_active_model(self):
            return parent

        def get_provider(self, provider_id):
            return Provider()

    monkeypatch.setattr(
        snapshot_module.ProviderManager,
        "get_instance",
        lambda tenant_id: Manager(),
    )

    path, resolved = capture_model_launch_snapshot(
        tenant_id="tenant-a",
        run_store_dir=tmp_path / "runs",
        run_id="subagent-one",
        definition=_definition().model_copy(
            update={
                "model_slot_override": {"provider_id": "evil", "model": "x"},
            },
        ),
    )

    assert resolved == parent
    assert (
        read_and_remove_private_model_snapshot(
            path,
            run_store_dir=tmp_path / "runs",
            run_id="subagent-one",
        )["selected"]["slot"]
        == parent.model_dump()
    )


def test_dependency_snapshot_copies_declared_skill_and_private_mcp(tmp_path):
    skill_dir = tmp_path / "skills" / "quality"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Quality", encoding="utf-8")
    config = _agent_config(tmp_path).model_copy(
        update={
            "mcp": MCPConfig(
                clients={
                    "github": MCPClientConfig(
                        name="github",
                        command="github-mcp",
                        env={"TOKEN_SECRET": "do-not-leak"},
                    ),
                },
            ),
        },
    )
    definition = _definition().model_copy(
        update={
            "name": "quality:reviewer",
            "owner_scope": "skill:quality",
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="reviewer",
                declared_skills=["quality"],
                declared_mcps=["github"],
            ),
        },
    )

    dirs, private_path, diagnostics = capture_launch_dependencies(
        run_store_dir=tmp_path / "runs",
        run_id="run-1",
        workspace_dir=tmp_path,
        parent_agent_config=config,
        definition=definition,
        effective_skill_names=["quality"],
    )

    assert (Path(dirs[0]) / "SKILL.md").read_text() == "# Quality"
    assert private_path is not None
    assert Path(private_path).stat().st_mode & 0o777 == 0o600
    assert "TOKEN_SECRET" in Path(private_path).read_text()
    assert diagnostics.loaded_skills == ["quality"]
    assert diagnostics.snapshotted_mcps == ["github"]
    assert (
        read_and_remove_private_mcp_snapshot(private_path)["github"]["name"]
        == "github"
    )
    assert not Path(private_path).exists()


def test_received_expert_uses_only_its_frozen_private_dependencies(tmp_path):
    definition = _definition().model_copy(
        update={
            "name": "received-reviewer",
            "agent_owned": AgentOwnedDefinitionMetadata(
                definition_id="00000000-0000-0000-0000-000000000001",
                declared_skills=["quality"],
                declared_mcps=["github"],
                community={
                    "item_id": "expert-1",
                    "version": "1.0.0",
                    "content_fingerprint": "fingerprint",
                },
            ),
        },
    )
    dependency_root = (
        tmp_path
        / "agents"
        / "00000000-0000-0000-0000-000000000001.dependencies"
    )
    (dependency_root / "skills" / "quality").mkdir(parents=True)
    (dependency_root / "skills" / "quality" / "SKILL.md").write_text(
        "# Frozen quality",
        encoding="utf-8",
    )
    (dependency_root / "mcp").mkdir()
    (dependency_root / "mcp" / "config.json").write_text(
        json.dumps(
            {
                "github": {
                    "name": "github",
                    "command": "frozen-github",
                    "env": {"TOKEN": "frozen"},
                },
            },
        ),
        encoding="utf-8",
    )
    config = _agent_config(tmp_path).model_copy(
        update={
            "mcp": MCPConfig(
                clients={
                    "github": MCPClientConfig(
                        name="github",
                        command="profile-github",
                    ),
                },
            ),
        },
    )

    dirs, private_path, diagnostics = capture_launch_dependencies(
        run_store_dir=tmp_path / "runs",
        run_id="run-frozen",
        workspace_dir=tmp_path,
        parent_agent_config=config,
        definition=definition,
        effective_skill_names=["quality"],
    )

    assert (Path(dirs[0]) / "SKILL.md").read_text() == "# Frozen quality"
    assert private_path is not None
    assert json.loads(Path(private_path).read_text())["github"]["command"] == (
        "frozen-github"
    )
    assert diagnostics.skipped_skills == []


def test_received_expert_missing_frozen_dependency_fails_closed(tmp_path):
    definition = _definition().model_copy(
        update={
            "agent_owned": AgentOwnedDefinitionMetadata(
                definition_id="00000000-0000-0000-0000-000000000002",
                declared_skills=["quality"],
                community={
                    "item_id": "expert-1",
                    "version": "1.0.0",
                    "content_fingerprint": "fingerprint",
                },
            ),
        },
    )

    with pytest.raises(OSError, match="frozen expert dependency"):
        capture_launch_dependencies(
            run_store_dir=tmp_path / "runs",
            run_id="run-missing-frozen",
            workspace_dir=tmp_path,
            parent_agent_config=_agent_config(tmp_path),
            definition=definition,
            effective_skill_names=["quality"],
        )


def test_received_expert_session_view_is_initialized_once_and_reused(
    tmp_path: Path,
) -> None:
    """A selected community expert gets one frozen dependency view per chat."""
    initialize = getattr(
        subagents_module,
        "initialize_community_expert_dependency_view",
        None,
    )
    assert callable(initialize)

    definition = _definition().model_copy(
        update={
            "agent_owned": AgentOwnedDefinitionMetadata(
                definition_id="00000000-0000-0000-0000-000000000003",
                declared_skills=["quality"],
                declared_mcps=["github"],
                community={
                    "item_id": "expert-1",
                    "version": "1.0.0",
                    "content_fingerprint": "fingerprint",
                },
            ),
        },
    )
    dependency_root = (
        tmp_path
        / "agents"
        / "00000000-0000-0000-0000-000000000003.dependencies"
    )
    (dependency_root / "skills" / "quality").mkdir(parents=True)
    skill_md = dependency_root / "skills" / "quality" / "SKILL.md"
    skill_md.write_text("# Frozen quality", encoding="utf-8")
    (dependency_root / "mcp").mkdir()
    (dependency_root / "mcp" / "config.json").write_text(
        '{"github":{"name":"github","command":"frozen-github"}}',
        encoding="utf-8",
    )

    first_view = initialize(
        workspace_dir=tmp_path,
        chat_id="00000000-0000-0000-0000-000000000010",
        definition=definition,
    )
    assert first_view is not None
    assert (first_view / "skills" / "quality" / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "# Frozen quality"

    skill_md.write_text("# Admin update", encoding="utf-8")
    second_view = initialize(
        workspace_dir=tmp_path,
        chat_id="00000000-0000-0000-0000-000000000010",
        definition=definition,
    )

    assert second_view == first_view
    assert (second_view / "skills" / "quality" / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "# Frozen quality"


def test_releasing_received_expert_removes_every_chat_dependency_view(
    tmp_path: Path,
) -> None:
    initialize = getattr(
        subagents_module,
        "initialize_community_expert_dependency_view",
        None,
    )
    release = getattr(
        subagents_module,
        "release_community_expert_dependency_views",
        None,
    )
    assert callable(initialize)
    assert callable(release)

    definition = _definition().model_copy(
        update={
            "agent_owned": AgentOwnedDefinitionMetadata(
                definition_id="00000000-0000-0000-0000-000000000004",
                community={
                    "item_id": "expert-1",
                    "version": "1.0.0",
                    "content_fingerprint": "fingerprint",
                },
            ),
        },
    )
    (
        tmp_path
        / "agents"
        / "00000000-0000-0000-0000-000000000004.dependencies"
    ).mkdir(
        parents=True,
    )
    for chat_id in (
        "00000000-0000-0000-0000-000000000011",
        "00000000-0000-0000-0000-000000000012",
    ):
        assert (
            initialize(
                workspace_dir=tmp_path,
                chat_id=chat_id,
                definition=definition,
            )
            is not None
        )

    release(
        workspace_dir=tmp_path,
        definition_id="00000000-0000-0000-0000-000000000004",
    )

    assert not list(
        (tmp_path / ".expert_sessions").rglob(
            "00000000-0000-0000-0000-000000000004",
        ),
    )


def test_skill_owned_model_reference_uses_available_tenant_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Only a configured Skill model is retained in a launch snapshot."""
    from swe.app.subagents import launch_snapshot as snapshot_module
    from swe.app.subagents import SkillOwnedModelReference

    definition = _definition().model_copy(
        update={
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="reviewer",
                model=SkillOwnedModelReference(
                    provider="openai",
                    id="gpt-5-mini",
                ),
            ),
        },
    )

    class Provider:
        def has_model(self, model_id):
            return model_id == "gpt-5-mini"

    class Manager:
        def get_provider(self, provider_id):
            return Provider() if provider_id == "openai" else None

    monkeypatch.setattr(
        snapshot_module.ProviderManager,
        "get_instance",
        lambda tenant_id: Manager(),
    )

    resolved = resolve_skill_owned_model_slot("tenant-a", definition)

    assert resolved is not None
    assert resolved.provider_id == "openai"
    assert resolved.model == "gpt-5-mini"


def test_non_skill_or_unavailable_model_reference_falls_back_to_parent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """No override is emitted when the Definition cannot select a model."""
    from swe.app.subagents import launch_snapshot as snapshot_module
    from swe.providers.models import ModelSlotConfig

    monkeypatch.setattr(
        snapshot_module.ProviderManager,
        "get_instance",
        lambda tenant_id: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert resolve_skill_owned_model_slot("tenant-a", _definition()) is None
    assert (
        resolve_skill_owned_model_slot(
            "tenant-a",
            _definition().model_copy(
                update={
                    "skill_owned": SkillOwnedDefinitionMetadata(
                        skill_name="quality",
                        local_name="reviewer",
                        model=None,
                    ),
                },
            ),
        )
        is None
    )


def test_unavailable_skill_model_is_snapshotted_as_parent_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unavailable TOML model override falls back before Worker launch."""
    from swe.app.subagents import SkillOwnedModelReference
    from swe.app.subagents import launch_snapshot as snapshot_module
    from swe.providers.models import ModelSlotConfig

    parent_slot = ModelSlotConfig(
        provider_id="parent",
        model="parent-model",
    )

    class Provider:
        def __init__(self, provider_id: str, models: set[str]) -> None:
            self.provider_id = provider_id
            self.models = models

        def has_model(self, model_id: str) -> bool:
            return model_id in self.models

        def model_dump(self, **_kwargs):
            return {
                "id": self.provider_id,
                "name": self.provider_id,
                "models": [
                    {"id": model_id, "name": model_id}
                    for model_id in self.models
                ],
            }

    class Manager:
        def get_active_model(self):
            return parent_slot

        def get_provider(self, provider_id: str):
            return {
                "parent": Provider("parent", {"parent-model"}),
                "openai": Provider("openai", {"gpt-5"}),
            }.get(provider_id)

    monkeypatch.setattr(
        snapshot_module.ProviderManager,
        "get_instance",
        lambda _tenant_id: Manager(),
    )
    definition = _definition().model_copy(
        update={
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="reviewer",
                model=SkillOwnedModelReference(
                    provider="openai",
                    id="gpt-5-mini",
                ),
            ),
        },
    )

    path, resolved = capture_model_launch_snapshot(
        tenant_id="tenant-a",
        run_store_dir=tmp_path / "runs",
        run_id="subagent-one",
        definition=definition,
    )

    assert resolved == parent_slot
    assert (
        read_and_remove_private_model_snapshot(
            path,
            run_store_dir=tmp_path / "runs",
            run_id="subagent-one",
        )["selected"]["slot"]
        == parent_slot.model_dump()
    )


@pytest.mark.asyncio
async def test_worker_writes_terminal_result_from_runtime(
    monkeypatch,
    tmp_path,
):
    from swe.app.subagents import worker as worker_module

    class FakeRuntime:
        def __init__(self, store):
            self.store = store

        async def run(self, **kwargs):
            assert kwargs["run"].run_id == run_id
            assert kwargs["run"].nickname == "研究员"
            assert kwargs["run"].start_request.name == "plan-researcher"
            assert kwargs["run"].definition_match.matched is True
            assert kwargs["request_context"] == {"session_id": "session-1"}
            assert isinstance(
                kwargs["parent_agent_config"],
                AgentProfileConfig,
            )
            return _result(run_id)

    launch_path, run_id, run_store_dir = await _write_launch_spec(tmp_path)
    monkeypatch.setattr(worker_module, "SubAgentRuntime", FakeRuntime)

    exit_code = await worker_module.run_worker(launch_path)
    record = await PerRunSubAgentRunStore(run_store_dir).get(run_id)

    assert exit_code == 0
    assert record is not None
    assert record.status == "completed"
    assert record.result is not None
    assert record.result.summary == "worker completed"


@pytest.mark.asyncio
async def test_worker_rejects_injected_model_selection_for_builtin_definition(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A launch file cannot make a built-in worker select another model."""
    from swe.app.subagents import worker as worker_module
    from swe.providers.models import ModelSlotConfig

    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, store):
            self.store = store

        async def run(self, **kwargs):
            observed["model_slot_override"] = kwargs["model_slot_override"]
            return _result(kwargs["run"].run_id)

    launch_path, _run_id, _run_store_dir = await _write_launch_spec(tmp_path)
    parent_slot = ModelSlotConfig(provider_id="parent", model="parent-model")
    injected_slot = ModelSlotConfig(provider_id="evil", model="evil-model")
    monkeypatch.setattr(worker_module, "SubAgentRuntime", FakeRuntime)
    monkeypatch.setattr(
        worker_module,
        "_load_snapshotted_model",
        lambda _spec: (injected_slot, object(), parent_slot, object()),
    )

    assert await worker_module.run_worker(launch_path) == 0
    assert observed["model_slot_override"] == parent_slot


@pytest.mark.asyncio
async def test_worker_falls_back_to_parent_when_skill_model_snapshot_is_unusable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A broken selected snapshot still uses the frozen parent model."""
    from swe.app.subagents import worker as worker_module
    from swe.providers.models import ModelSlotConfig

    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, store):
            self.store = store

        async def run(self, **kwargs):
            observed["model_slot_override"] = kwargs["model_slot_override"]
            observed["model_provider_override"] = kwargs[
                "model_provider_override"
            ]
            return _result(kwargs["run"].run_id)

    launch_path, _run_id, _run_store_dir = await _write_launch_spec(tmp_path)
    raw = json.loads(launch_path.read_text(encoding="utf-8"))
    raw["definition"]["skill_owned"] = {
        "skill_name": "quality",
        "local_name": "reviewer",
    }
    launch_path.write_text(json.dumps(raw), encoding="utf-8")
    parent_slot = ModelSlotConfig(provider_id="parent", model="parent-model")
    parent_provider = object()
    monkeypatch.setattr(worker_module, "SubAgentRuntime", FakeRuntime)
    monkeypatch.setattr(
        worker_module,
        "_load_snapshotted_model",
        lambda _spec: (None, None, parent_slot, parent_provider),
    )

    assert await worker_module.run_worker(launch_path) == 0
    assert observed["model_slot_override"] == parent_slot
    assert observed["model_provider_override"] is parent_provider


@pytest.mark.asyncio
async def test_worker_connects_private_mcp_snapshot_and_removes_it(
    monkeypatch,
    tmp_path: Path,
):
    """Worker consumes a one-shot MCP config and passes only its client."""
    from swe.app.subagents import worker as worker_module

    observed = {}

    class FakeClient:
        name = "github"

        async def connect(self):
            observed["connected"] = True

    async def create_client(config, **kwargs):
        observed["config"] = config
        return FakeClient()

    class FakeRuntime:
        def __init__(self, store):
            self.store = store

        async def run(self, **kwargs):
            observed["clients"] = kwargs["mcp_clients"]
            return _result(kwargs["run"].run_id)

    launch_path, run_id, run_store_dir = await _write_launch_spec(tmp_path)
    private_path = run_store_dir / f".{run_id}.mcp.json"
    private_path.write_text(
        json.dumps(
            {
                "github": MCPClientConfig(
                    name="github",
                    command="github-mcp",
                ).model_dump(mode="json"),
            },
        ),
        encoding="utf-8",
    )
    raw = json.loads(launch_path.read_text(encoding="utf-8"))
    raw["launch_snapshot"] = {
        "private_mcp_snapshot_path": str(private_path),
    }
    launch_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(worker_module, "SubAgentRuntime", FakeRuntime)
    monkeypatch.setattr(
        "swe.app.runner.runner._create_mcp_client_with_headers",
        create_client,
    )

    assert await worker_module.run_worker(launch_path) == 0
    assert observed["connected"] is True
    assert observed["config"].name == "github"
    assert len(observed["clients"]) == 1
    assert not private_path.exists()
    record = await PerRunSubAgentRunStore(run_store_dir).get(run_id)
    assert record is not None
    assert record.launch_diagnostics.connected_mcps == ["github"]


@pytest.mark.asyncio
async def test_worker_silently_skips_failed_private_mcp_connection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A declared but offline MCP does not stop the SubAgent run."""
    from swe.app.subagents import worker as worker_module

    observed = {}

    async def create_client(*args, **kwargs):
        raise OSError("offline")

    class FakeRuntime:
        def __init__(self, store):
            self.store = store

        async def run(self, **kwargs):
            observed["clients"] = kwargs["mcp_clients"]
            return _result(kwargs["run"].run_id)

    launch_path, run_id, run_store_dir = await _write_launch_spec(tmp_path)
    private_path = run_store_dir / f".{run_id}.mcp.json"
    private_path.write_text(
        json.dumps(
            {
                "offline": MCPClientConfig(
                    name="offline",
                    command="offline-mcp",
                ).model_dump(mode="json"),
            },
        ),
        encoding="utf-8",
    )
    raw = json.loads(launch_path.read_text(encoding="utf-8"))
    raw["launch_snapshot"] = {
        "private_mcp_snapshot_path": str(private_path),
    }
    launch_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(worker_module, "SubAgentRuntime", FakeRuntime)
    monkeypatch.setattr(
        "swe.app.runner.runner._create_mcp_client_with_headers",
        create_client,
    )

    assert await worker_module.run_worker(launch_path) == 0
    assert observed["clients"] == []
    assert not private_path.exists()
    record = await PerRunSubAgentRunStore(run_store_dir).get(run_id)
    assert record is not None
    assert record.launch_diagnostics.connected_mcps == []


def test_worker_rejects_skill_paths_outside_its_snapshot(
    tmp_path: Path,
) -> None:
    from swe.app.subagents import worker as worker_module

    inside = tmp_path / "runs" / "subagent-a.skills" / "quality"
    inside.mkdir(parents=True)
    (inside / "SKILL.md").write_text("# Quality", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside", encoding="utf-8")
    spec = WorkerLaunchSpec(
        run_id="subagent-a",
        run_store_dir=str(tmp_path / "runs"),
        workspace_dir=str(tmp_path),
        parent_agent_config=_agent_config(tmp_path).model_dump(mode="json"),
        definition=_definition(),
        delegation_spec=_spec(),
        effective_policy=PermissionPolicy.readonly(),
        skill_snapshot_dirs=[str(inside), str(outside)],
    )

    assert worker_module._validated_skill_snapshot_dirs(spec) == [str(inside)]


def test_worker_rejects_a_symlinked_snapshot_skill_root(
    tmp_path: Path,
) -> None:
    from swe.app.subagents import worker as worker_module

    root = tmp_path / "runs" / "subagent-a.skills"
    root.mkdir(parents=True)
    external = tmp_path / "external-skill"
    external.mkdir()
    (external / "SKILL.md").write_text("# External", encoding="utf-8")
    linked = root / "quality"
    linked.symlink_to(external, target_is_directory=True)
    spec = WorkerLaunchSpec(
        run_id="subagent-a",
        run_store_dir=str(tmp_path / "runs"),
        workspace_dir=str(tmp_path),
        parent_agent_config=_agent_config(tmp_path).model_dump(mode="json"),
        definition=_definition(),
        delegation_spec=_spec(),
        effective_policy=PermissionPolicy.readonly(),
        skill_snapshot_dirs=[str(linked)],
    )

    assert worker_module._validated_skill_snapshot_dirs(spec) == []


@pytest.mark.asyncio
async def test_worker_invalid_launch_spec_removes_private_snapshots(
    tmp_path: Path,
) -> None:
    from swe.app.subagents import worker as worker_module

    run_store = tmp_path / "runs"
    run_store.mkdir()
    mcp = run_store / ".subagent-one.mcp.json"
    model = run_store / ".subagent-one.model.json"
    mcp.write_text("{}", encoding="utf-8")
    model.write_text("{}", encoding="utf-8")
    launch_path = run_store / "subagent-one.launch.json"
    launch_path.write_text(
        json.dumps(
            {
                "run_id": "subagent-one",
                "run_store_dir": str(run_store),
                "launch_snapshot": {
                    "private_mcp_snapshot_path": str(mcp),
                    "private_model_snapshot_path": str(model),
                },
                "invalid": object.__name__,
            },
        ),
        encoding="utf-8",
    )

    assert await worker_module.run_worker(launch_path) == 1
    assert not mcp.exists()
    assert not model.exists()


@pytest.mark.asyncio
async def test_worker_missing_record_removes_private_snapshots(
    tmp_path: Path,
) -> None:
    from swe.app.subagents import worker as worker_module

    run_store = tmp_path / "runs"
    run_store.mkdir()
    run_id = "subagent-one"
    mcp = run_store / f".{run_id}.mcp.json"
    model = run_store / f".{run_id}.model.json"
    mcp.write_text("{}", encoding="utf-8")
    model.write_text("{}", encoding="utf-8")
    launch = WorkerLaunchSpec(
        run_id=run_id,
        run_store_dir=str(run_store),
        workspace_dir=str(tmp_path),
        parent_agent_config=_agent_config(tmp_path).model_dump(mode="json"),
        definition=_definition(),
        delegation_spec=_spec(),
        effective_policy=PermissionPolicy.readonly(),
        launch_snapshot={
            "private_mcp_snapshot_path": str(mcp),
            "private_model_snapshot_path": str(model),
        },
    )
    launch_path = run_store / f"{run_id}.launch.json"
    launch_path.write_text(launch.model_dump_json(), encoding="utf-8")

    assert await worker_module.run_worker(launch_path) == 1
    assert not mcp.exists()
    assert not model.exists()


@pytest.mark.asyncio
async def test_worker_preserves_partial_runtime_result(monkeypatch, tmp_path):
    from swe.app.subagents import worker as worker_module

    class FakeRuntime:
        def __init__(self, store):
            self.store = store

        async def run(self, **kwargs):
            return _result(kwargs["run"].run_id, status="partial")

    launch_path, run_id, run_store_dir = await _write_launch_spec(tmp_path)
    monkeypatch.setattr(worker_module, "SubAgentRuntime", FakeRuntime)

    exit_code = await worker_module.run_worker(launch_path)
    record = await PerRunSubAgentRunStore(run_store_dir).get(run_id)

    assert exit_code == 0
    assert record is not None
    assert record.status == "partial"
    assert record.result is not None
    assert record.result.status == "partial"


@pytest.mark.asyncio
async def test_worker_binds_launch_identity(monkeypatch, tmp_path):
    from swe.app.subagents import worker as worker_module
    from swe.config.context import (
        get_current_effective_tenant_id,
        get_current_source_id,
        get_current_tenant_id,
        get_current_user_id,
        get_current_workspace_dir,
    )

    observed = {}

    class CapturingRuntime:
        def __init__(self, store):
            self.store = store

        async def run(self, **kwargs):
            observed["tenant_id"] = get_current_tenant_id()
            observed["effective_tenant_id"] = get_current_effective_tenant_id()
            observed["source_id"] = get_current_source_id()
            observed["user_id"] = get_current_user_id()
            observed["workspace_dir"] = get_current_workspace_dir()
            observed["request_context"] = kwargs["request_context"]
            return _result(kwargs["run"].run_id)

    launch_path, run_id, run_store_dir = await _write_launch_spec(tmp_path)
    raw = json.loads(launch_path.read_text(encoding="utf-8"))
    raw["request_context"] = {
        "session_id": "session-1",
        "tenant_id": "tenant-1",
        "source_id": "source-1",
        "scope_id": "dGVuYW50LTE.c291cmNlLTE",
        "user_id": "user-1",
    }
    launch_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(worker_module, "SubAgentRuntime", CapturingRuntime)

    exit_code = await worker_module.run_worker(launch_path)
    record = await PerRunSubAgentRunStore(run_store_dir).get(run_id)

    assert exit_code == 0
    assert record is not None
    assert observed == {
        "tenant_id": "tenant-1",
        "effective_tenant_id": "dGVuYW50LTE.c291cmNlLTE",
        "source_id": "source-1",
        "user_id": "user-1",
        "workspace_dir": tmp_path / "workspace",
        "request_context": {
            "session_id": "session-1",
            "tenant_id": "tenant-1",
            "source_id": "source-1",
            "scope_id": "dGVuYW50LTE.c291cmNlLTE",
            "user_id": "user-1",
        },
    }


@pytest.mark.asyncio
async def test_worker_exception_writes_failed(monkeypatch, tmp_path):
    from swe.app.subagents import worker as worker_module

    class RaisingRuntime:
        def __init__(self, store):
            self.store = store

        async def run(self, **kwargs):
            raise RuntimeError(
                "run_id=subagent-secret path=/private/secret.log token=top-secret",
            )

    launch_path, run_id, run_store_dir = await _write_launch_spec(tmp_path)
    monkeypatch.setattr(worker_module, "SubAgentRuntime", RaisingRuntime)

    exit_code = await worker_module.run_worker(launch_path)
    record = await PerRunSubAgentRunStore(run_store_dir).get(run_id)

    assert exit_code == 1
    assert record is not None
    assert record.status == "failed"
    assert record.errors
    assert record.errors[-1].code == "worker_runtime_error"
    assert record.errors[-1].message == "SubAgent execution failed."
