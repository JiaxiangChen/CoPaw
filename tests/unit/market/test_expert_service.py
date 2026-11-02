# -*- coding: utf-8 -*-
"""Expert Community marketplace service tests."""

from __future__ import annotations

from pathlib import Path
import json
import os
import tomllib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from market.app.routers.experts_market import router as experts_router
from market.marketplace.fs import get_user_expert_dir, load_index
from market.marketplace.schemas import ExpertDistributionRequest
from market.marketplace.service import (
    ExpertDependencyError,
    ExpertNameConflictError,
    MarketplaceService,
)


@pytest.fixture
def mock_db() -> MagicMock:
    """Create a disconnected DB double."""
    db = MagicMock()
    db.is_connected = False
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    return db


@pytest.fixture
def mock_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Create isolated market/runtime roots."""
    marketplace_root = tmp_path / ".swe.marketplace"
    swe_root = tmp_path / ".swe"
    marketplace_root.mkdir(parents=True, exist_ok=True)
    swe_root.mkdir(parents=True, exist_ok=True)
    return marketplace_root, swe_root


@pytest.fixture
def service(
    mock_db: MagicMock,
    mock_paths: tuple[Path, Path],
) -> MarketplaceService:
    """Create a MarketplaceService instance."""
    marketplace_root, swe_root = mock_paths
    instance = MarketplaceService(mock_db, marketplace_root, swe_root)
    instance._trigger_agent_reload = AsyncMock(return_value=True)
    return instance


def _write_source_package(
    source_dir: Path,
    *,
    name: str = "Community Expert",
    description: str = "Expert description",
    creator_id: str = "author-a",
    creator_name: str = "Author A",
    category_id: int = 7,
    skill_content: str = "# skill a\n",
    mcp_config: str = '{"name": "mcp_a", "command": "tool"}',
    scan_result: str = '{"status": "clean"}',
    declare_missing_skill: bool = False,
) -> None:
    """Write a synthetic expert source package."""
    (source_dir / "skills" / "skill_a").mkdir(parents=True, exist_ok=True)
    (source_dir / "mcp" / "mcp_a").mkdir(parents=True, exist_ok=True)

    skills_line = (
        'skills = ["missing_skill"]'
        if declare_missing_skill
        else 'skills = ["skill_a"]'
    )
    definition_toml = (
        f'name = "{name}"\n'
        f'description = "{description}"\n'
        f'creator_id = "{creator_id}"\n'
        f'creator_name = "{creator_name}"\n'
        f"category_id = {category_id}\n"
        'bbk_ids = ["100"]\n'
        f"{skills_line}\n"
        'mcps = ["mcp_a"]\n'
        "\n"
        "[model]\n"
        'provider = "openai"\n'
        'id = "gpt-4o-mini"\n'
    )

    (source_dir / "definition.toml").write_text(
        definition_toml,
        encoding="utf-8",
    )
    (source_dir / "skills" / "skill_a" / "SKILL.md").write_text(
        skill_content,
        encoding="utf-8",
    )
    (source_dir / "mcp" / "mcp_a" / "mcp.json").write_text(
        mcp_config,
        encoding="utf-8",
    )
    (source_dir / "scan_result.json").write_text(
        scan_result,
        encoding="utf-8",
    )


def _publish(
    service: MarketplaceService,
    source_id: str,
    source_dir: Path,
    *,
    overwrite: bool = True,
):
    return service.publish_expert(
        source_id,
        source_dir,
        operator_id="manager",
        operator_name="Manager",
        overwrite=overwrite,
    )


@pytest.fixture
async def published_dependencies(service: MarketplaceService, tmp_path: Path):
    """Publish a complete dependency package shared by delivery scenarios."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    mcp_config = {
        "name": "mcp_a",
        "command": "published-tool",
        "env": {"TOKEN": "published-value"},
        "headers": {"X-Expert": "published-header"},
    }
    _write_source_package(
        source_dir,
        skill_content="# Published Skill\n",
        mcp_config=json.dumps(mcp_config),
    )
    definition_path = source_dir / "definition.toml"
    definition_path.write_text(
        'instruction = "Use the private dependencies."\n'
        + definition_path.read_text(),
    )
    skill_script = source_dir / "skills" / "skill_a" / "scripts" / "run.py"
    skill_script.parent.mkdir()
    skill_script.write_text("published script\n")
    item, _ = await _publish(service, "source-a", source_dir)
    return source_dir, item, mcp_config


def test_copy_expert_package_keeps_previous_root_when_staging_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from market.marketplace.service import _copy_expert_package

    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "definition.toml").write_text("new", encoding="utf-8")
    (target / "definition.toml").write_text("old", encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr("market.marketplace.service.shutil.copy2", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        _copy_expert_package(source, target)

    assert (target / "definition.toml").read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_normal_dependencies", [False, True])
async def test_distribution_delivers_published_private_dependencies(
    service: MarketplaceService,
    tmp_path: Path,
    has_normal_dependencies: bool,
    published_dependencies,
) -> None:
    from swe.app.subagents import AgentOwnedDefinitionRepository
    from swe.app.subagents.launch_snapshot import (
        read_and_remove_private_mcp_snapshot,
    )
    from swe.app.subagents.session_dependencies import (
        capture_community_expert_session_dependencies,
        initialize_community_expert_dependency_view,
    )
    from swe.config.config import AgentProfileConfig

    source_dir, item, mcp_config = published_dependencies
    skill_script = source_dir / "skills" / "skill_a" / "scripts" / "run.py"

    # Unpublished edits must not leak into the distributed version.
    skill_script.write_text("unpublished script\n")
    (source_dir / "mcp" / "mcp_a" / "mcp.json").write_text(
        json.dumps({**mcp_config, "command": "unpublished-tool"}),
    )
    expert_dir = get_user_expert_dir(
        service.swe_root,
        "alice",
        "default",
        "source-a",
    )
    workspace = expert_dir.parent
    workspace.mkdir(parents=True, exist_ok=True)
    normal_skill = workspace / "skills" / "skill_a" / "SKILL.md"
    normal_config = (
        {
            "mcp": {"clients": {"mcp_a": {"command": "normal-tool"}}},
        }
        if has_normal_dependencies
        else {}
    )
    if has_normal_dependencies:
        normal_skill.parent.mkdir(parents=True)
        normal_skill.write_text("normal skill\n")
    config_path = workspace / "agent.json"
    config_text = json.dumps(normal_config)
    config_path.write_text(config_text)

    app = FastAPI()
    app.state.marketplace = service
    app.include_router(experts_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/market/experts/{item.item_id}/distribute",
            headers={
                "X-Source-Id": "source-a",
                "X-Manager": "true",
                "X-User-Id": "manager",
            },
            json={"target_type": "user_id", "target_values": ["alice"]},
        )

    assert response.status_code == 200
    assert response.json()["distributed_count"] == 1
    definition_id = response.json()["results"][0]["definition_id"]
    package = AgentOwnedDefinitionRepository(
        expert_dir,
        owner_scope="alice/default",
    ).get(definition_id)
    assert package is not None and package.valid
    assert package.definition.enabled is True
    assert package.definition.agent_owned.community.version == item.version
    private_root = expert_dir / f"{definition_id}.dependencies"
    assert (
        private_root / "skills" / "skill_a" / "scripts" / "run.py"
    ).read_text() == "published script\n"
    assert config_path.read_text() == config_text
    if has_normal_dependencies:
        assert normal_skill.read_text() == "normal skill\n"
    else:
        assert not normal_skill.exists()

    view = initialize_community_expert_dependency_view(
        workspace_dir=workspace,
        chat_id="00000000-0000-4000-8000-000000000001",
        definition=package.definition,
    )
    assert view is not None
    run_store = tmp_path / "run-store"
    paths, mcp_path, diagnostics = (
        capture_community_expert_session_dependencies(
            run_store_dir=run_store,
            run_id="subagent-delivery-test",
            dependency_view_root=view,
            definition=package.definition,
            parent_agent_config=AgentProfileConfig(
                id="default",
                name="Default",
            ),
        )
    )
    assert diagnostics.loaded_skills == ["skill_a"]
    assert (Path(paths[0]) / "SKILL.md").read_text() == "# Published Skill\n"
    assert (
        Path(paths[0]) / "scripts" / "run.py"
    ).read_text() == "published script\n"
    snapshot = read_and_remove_private_mcp_snapshot(
        mcp_path,
        run_store_dir=run_store,
        run_id="subagent-delivery-test",
    )
    assert set(snapshot) == {"mcp_a"}
    for key, value in mcp_config.items():
        assert snapshot["mcp_a"][key] == value
    assert snapshot["mcp_a"]["transport"] == "stdio"


@pytest.mark.asyncio
async def test_distribution_updates_both_private_dependencies_and_keeps_disabled(
    service: MarketplaceService,
    published_dependencies,
) -> None:
    source_dir, item, _ = published_dependencies
    installed = await service.install_expert("source-a", item.item_id, "alice")
    expert_dir = get_user_expert_dir(
        service.swe_root,
        "alice",
        "default",
        "source-a",
    )
    definition_path = expert_dir / f"{installed.definition_id}.toml"
    definition_path.write_text(
        definition_path.read_text().replace(
            "enabled = true",
            "enabled = false",
        ),
    )
    _write_source_package(
        source_dir,
        skill_content="new skill\n",
        mcp_config='{"name":"mcp_a","command":"new-tool"}',
    )
    updated, _ = await _publish(service, "source-a", source_dir)

    result = await service.distribute_expert(
        "source-a",
        item.item_id,
        "manager",
        ExpertDistributionRequest(
            target_type="user_id",
            target_values=["alice"],
        ),
    )

    assert result.distributed_count == 1
    assert result.results[0].definition_id == installed.definition_id
    payload = tomllib.loads(definition_path.read_text())
    assert payload["enabled"] is False
    assert payload["community"]["version"] == updated.version != item.version
    private_root = expert_dir / f"{installed.definition_id}.dependencies"
    assert (
        private_root / "skills" / "skill_a" / "SKILL.md"
    ).read_text() == "new skill\n"
    assert (
        json.loads((private_root / "mcp" / "config.json").read_text())[
            "mcp_a"
        ]["command"]
        == "new-tool"
    )


@pytest.mark.asyncio
async def test_distribution_keeps_old_receipt_on_copy_failure_and_continues(
    service: MarketplaceService,
    monkeypatch: pytest.MonkeyPatch,
    published_dependencies,
) -> None:
    import shutil

    source_dir, item, _ = published_dependencies
    installed = await service.install_expert("source-a", item.item_id, "alice")
    alice_dir = get_user_expert_dir(
        service.swe_root,
        "alice",
        "default",
        "source-a",
    )
    definition_path = alice_dir / f"{installed.definition_id}.toml"
    original_definition = definition_path.read_bytes()
    _write_source_package(
        source_dir,
        skill_content="new skill\n",
        mcp_config='{"name":"mcp_a","command":"new-tool"}',
    )
    await _publish(service, "source-a", source_dir)
    original_copy = shutil.copytree

    def fail_alice_copy(source, target, *args, **kwargs):
        if Path(target).is_relative_to(alice_dir):
            raise OSError("dependency copy failed")
        return original_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(
        "market.marketplace.service.shutil.copytree",
        fail_alice_copy,
    )
    result = await service.distribute_expert(
        "source-a",
        item.item_id,
        "manager",
        ExpertDistributionRequest(
            target_type="user_id",
            target_values=["alice", "bob"],
        ),
    )

    assert result.distributed_count == 1
    assert result.conflict_count == 1
    assert [(entry.user_id, entry.success) for entry in result.results] == [
        ("alice", False),
        ("bob", True),
    ]
    assert result.results[0].reason == "dependency copy failed"
    assert definition_path.read_bytes() == original_definition
    private_root = alice_dir / f"{installed.definition_id}.dependencies"
    assert (
        private_root / "skills" / "skill_a" / "SKILL.md"
    ).read_text() == "# Published Skill\n"
    assert (
        json.loads((private_root / "mcp" / "config.json").read_text())[
            "mcp_a"
        ]["command"]
        == "published-tool"
    )
    bob_dir = get_user_expert_dir(
        service.swe_root,
        "bob",
        "default",
        "source-a",
    )
    bob_root = bob_dir / f"{result.results[1].definition_id}.dependencies"
    assert (
        bob_root / "skills" / "skill_a" / "SKILL.md"
    ).read_text() == "new skill\n"
    assert (
        json.loads((bob_root / "mcp" / "config.json").read_text())["mcp_a"][
            "command"
        ]
        == "new-tool"
    )


class TestPublishExpert:
    """Expert publication lifecycle tests."""

    @pytest.mark.asyncio
    async def test_publish_initial_version_creates_1_0_0(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)

        item, version_unchanged = await _publish(
            service,
            "source-a",
            source_dir,
        )

        assert item.item_type == "expert"
        assert item.version == "1.0.0"
        assert version_unchanged is False

        snapshot_dir = (
            service.marketplace_root
            / "source-a"
            / "experts"
            / item.item_id
            / "versions"
            / "1.0.0"
        )
        assert (snapshot_dir / "definition.toml").is_file()
        assert (snapshot_dir / "skills" / "skill_a" / "SKILL.md").is_file()
        assert (snapshot_dir / "mcp" / "mcp_a" / "mcp.json").is_file()
        assert (snapshot_dir / "scan_result.json").is_file()

    @pytest.mark.asyncio
    async def test_publish_from_agent_profile_builds_safe_source_package(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )
        definition_id = "expert-local-1"
        dependency_dir = expert_dir / f"{definition_id}.dependencies"
        (dependency_dir / "skills" / "skill_a").mkdir(
            parents=True,
            exist_ok=True,
        )
        (dependency_dir / "skills" / "skill_a" / "SKILL.md").write_text(
            "# local skill\n",
            encoding="utf-8",
        )
        (dependency_dir / "mcp" / "mcp_a").mkdir(parents=True)
        (dependency_dir / "mcp" / "mcp_a" / "mcp.json").write_text(
            '{"command":"tool"}',
            encoding="utf-8",
        )
        (expert_dir / f"{definition_id}.toml").write_text(
            'name = "Local Expert"\n'
            'description = "From profile"\n'
            'instruction = "Inspect the request."\n'
            'skills = ["skill_a"]\n'
            'mcps = ["mcp_a"]\n\n'
            '[model]\nprovider = "openai"\nid = "gpt-test"\n',
            encoding="utf-8",
        )

        item, unchanged = await service.publish_expert_from_profile(
            "source-a",
            "alice",
            "default",
            definition_id,
            category_id=7,
            bbk_ids=["100"],
            creator_name="Alice",
            overwrite=False,
        )

        assert unchanged is False
        assert item.name == "Local Expert"
        assert item.creator_id == "alice"
        assert item.category_id == 7
        snapshot_dir = (
            service.marketplace_root
            / "source-a"
            / "experts"
            / item.item_id
            / "versions"
            / "1.0.0"
        )
        assert (snapshot_dir / "skills" / "skill_a" / "SKILL.md").is_file()
        assert (snapshot_dir / "mcp" / "mcp_a" / "mcp.json").is_file()

    @pytest.mark.asyncio
    async def test_publish_from_agent_profile_rejects_path_traversal(
        self,
        service: MarketplaceService,
    ) -> None:
        with pytest.raises(ValueError, match="definition_id"):
            await service.publish_expert_from_profile(
                "source-a",
                "alice",
                "default",
                "../outside",
                creator_name="Alice",
            )

    @pytest.mark.asyncio
    async def test_received_variant_cannot_overwrite_its_original_community_item(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir, name="Shared Expert")
        original, _ = await _publish(service, "source-a", source_dir)
        installed = await service.install_expert(
            "source-a",
            original.item_id,
            "alice",
        )
        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )

        with pytest.raises(ExpertNameConflictError):
            await service.publish_expert_from_profile(
                "source-a",
                "alice",
                "default",
                str(installed.definition_id),
                creator_name="Alice",
                overwrite=True,
            )

    @pytest.mark.asyncio
    async def test_publish_scans_each_bundled_skill_once(
        self,
        service: MarketplaceService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        scanned: list[Path] = []
        monkeypatch.setattr(
            "market.marketplace.service.scan_skill_directory",
            lambda path, **_kwargs: scanned.append(path),
        )

        await _publish(service, "source-a", source_dir)

        assert scanned == [source_dir / "skills" / "skill_a"]

    @pytest.mark.asyncio
    async def test_publish_replaces_stale_community_identity_and_scan_snapshot(
        self,
        service: MarketplaceService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        (source_dir / "definition.toml").write_text(
            (source_dir / "definition.toml").read_text(encoding="utf-8")
            + '\n[community]\nitem_id = "old-item"\nversion = "9.9.9"\ncontent_fingerprint = "old"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "market.marketplace.service.scan_skill_directory",
            lambda path, **_kwargs: MagicMock(
                to_dict=lambda: {"skill_name": path.name, "is_safe": True},
            ),
        )

        item, _ = await _publish(service, "source-a", source_dir)
        definition = (
            service.marketplace_root
            / "source-a"
            / "experts"
            / item.item_id
            / "definition.toml"
        ).read_text(encoding="utf-8")
        assert 'item_id = "old-item"' not in definition
        assert "[community]" not in definition
        scan_snapshot = json.loads(
            (
                service.marketplace_root
                / "source-a"
                / "experts"
                / item.item_id
                / "scan_result.json"
            ).read_text(encoding="utf-8"),
        )
        assert scan_snapshot["skills"][0]["skill_name"] == "skill_a"

    @pytest.mark.asyncio
    async def test_identical_republish_is_noop(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)

        first_item, first_unchanged = await _publish(
            service,
            "source-a",
            source_dir,
        )
        second_item, second_unchanged = await _publish(
            service,
            "source-a",
            source_dir,
        )

        assert first_unchanged is False
        assert second_unchanged is True
        assert second_item.item_id == first_item.item_id
        assert second_item.version == "1.0.0"

    @pytest.mark.asyncio
    async def test_changed_content_bumps_patch_version(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)

        first_item, _ = await _publish(service, "source-a", source_dir)

        (source_dir / "skills" / "skill_a" / "SKILL.md").write_text(
            "# skill a updated\n",
            encoding="utf-8",
        )
        second_item, second_unchanged = await _publish(
            service,
            "source-a",
            source_dir,
        )

        assert second_unchanged is False
        assert second_item.item_id == first_item.item_id
        assert second_item.version == "1.0.1"

    @pytest.mark.asyncio
    async def test_missing_declared_dependency_blocks_publish(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir, declare_missing_skill=True)
        (source_dir / "skills" / "skill_a" / "SKILL.md").unlink()

        with pytest.raises(ValueError, match="dependency"):
            await _publish(service, "source-a", source_dir)

    @pytest.mark.asyncio
    async def test_publish_rejects_unusable_declared_mcp_config(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        """Declared MCPs must contain a runnable transport configuration."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir, mcp_config='{"name": "mcp_a"}')

        with pytest.raises(ExpertDependencyError, match="Invalid bundled MCP"):
            await _publish(service, "source-a", source_dir)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["skills", "mcps"])
    async def test_publish_rejects_dependency_path_segments(
        self,
        service: MarketplaceService,
        tmp_path: Path,
        field: str,
    ) -> None:
        """A package cannot resolve dependencies outside its frozen root."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        definition_path = source_dir / "definition.toml"
        definition_path.write_text(
            definition_path.read_text(encoding="utf-8").replace(
                f'{field} = ["{field[:-1]}_a"]',
                f'{field} = ["../outside"]',
            ),
            encoding="utf-8",
        )

        with pytest.raises(ExpertDependencyError, match="unsafe path"):
            await _publish(service, "source-a", source_dir)

    @pytest.mark.asyncio
    async def test_same_name_without_overwrite_conflicts(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir, name="Shared Expert")

        await _publish(service, "source-a", source_dir)

        with pytest.raises(ValueError, match="already exists"):
            await _publish(service, "source-a", source_dir, overwrite=False)

    @pytest.mark.asyncio
    async def test_restore_and_unpublish_keep_history(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)

        item, _ = await _publish(service, "source-a", source_dir)
        (source_dir / "skills" / "skill_a" / "SKILL.md").write_text(
            "# skill a updated\n",
            encoding="utf-8",
        )
        updated_item, _ = await _publish(service, "source-a", source_dir)

        restored = await service.restore_expert_version(
            "source-a",
            item.item_id,
            "1.0.0",
            operator_id="manager",
            operator_name="Manager",
        )
        unpublished = await service.unpublish_expert(
            "source-a",
            item.item_id,
            operator_id="manager",
            operator_name="Manager",
        )

        assert updated_item.version == "1.0.1"
        assert restored.version == "1.0.0"
        assert unpublished is True

        items = load_index(service.marketplace_root, "source-a")
        saved = next(
            saved_item
            for saved_item in items
            if saved_item.item_id == item.item_id
        )
        assert saved.status == "inactive"
        versions_root = (
            service.marketplace_root
            / "source-a"
            / "experts"
            / item.item_id
            / "versions"
        )
        assert (versions_root / "1.0.0").is_dir()
        assert (versions_root / "1.0.1").is_dir()


class TestReceivedExpertLifecycle:
    """Community installation keeps dependencies private and frozen."""

    @pytest.mark.asyncio
    async def test_install_copies_private_dependencies_and_community_identity(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        item, _ = await _publish(service, "source-a", source_dir)

        installed = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
            "research",
        )

        assert installed.success is True
        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "research",
            "source-a",
        )
        definition = expert_dir / f"{installed.definition_id}.toml"
        dependency_dir = expert_dir / f"{installed.definition_id}.dependencies"
        assert definition.is_file()
        assert (dependency_dir / "skills" / "skill_a" / "SKILL.md").is_file()
        assert (dependency_dir / "mcp" / "mcp_a" / "mcp.json").is_file()
        mcp_snapshot = json.loads(
            (dependency_dir / "mcp" / "config.json").read_text(
                encoding="utf-8",
            ),
        )
        assert mcp_snapshot == {
            "mcp_a": {
                "name": "mcp_a",
                "command": "tool",
                "transport": "stdio",
            },
        }
        payload = tomllib.loads(definition.read_text(encoding="utf-8"))
        assert payload["enabled"] is True
        assert payload["community"]["item_id"] == item.item_id
        assert payload["community"]["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_install_normalizes_bundled_mcp_server_config(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(
            source_dir,
            mcp_config=(
                '{"mcpServers":{"mcp_a":{"command":"tool",'
                '"env":{"TOKEN":"value"},"headers":{"X-Test":"yes"}}}}'
            ),
        )
        item, _ = await _publish(service, "source-a", source_dir)

        installed = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
        )

        config = json.loads(
            (
                get_user_expert_dir(
                    service.swe_root,
                    "alice",
                    "default",
                    "source-a",
                )
                / f"{installed.definition_id}.dependencies"
                / "mcp"
                / "config.json"
            ).read_text(encoding="utf-8"),
        )
        assert config["mcp_a"]["env"] == {"TOKEN": "value"}
        assert config["mcp_a"]["headers"] == {"X-Test": "yes"}
        assert config["mcp_a"]["name"] == "mcp_a"
        assert config["mcp_a"]["transport"] == "stdio"

    @pytest.mark.asyncio
    async def test_install_rejects_incomplete_package_without_creating_definition(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        """A corrupted published package cannot leave a partial receipt."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        item, _ = await _publish(service, "source-a", source_dir)
        package_config = (
            service.marketplace_root
            / "source-a"
            / "experts"
            / item.item_id
            / "mcp"
            / "mcp_a"
            / "mcp.json"
        )
        package_config.unlink()

        with pytest.raises(
            ExpertDependencyError,
            match="failed integrity verification",
        ):
            await service.install_expert("source-a", item.item_id, "alice")

        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )
        assert list(expert_dir.glob("*.toml")) == []

    @pytest.mark.asyncio
    async def test_install_does_not_overwrite_existing_received_expert(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        item, _ = await _publish(service, "source-a", source_dir)
        installed = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
        )
        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )
        definition = expert_dir / f"{installed.definition_id}.toml"
        definition.write_text(
            definition.read_text(encoding="utf-8").replace(
                "Expert description",
                "Local variant",
            ),
            encoding="utf-8",
        )

        second = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
        )

        assert second.success is False
        assert "already installed" in (second.reason or "")
        assert "Local variant" in definition.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_distribution_silently_overwrites_local_variant_and_preserves_enablement(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir, skill_content="# original\n")
        item, _ = await _publish(service, "source-a", source_dir)
        installed = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
        )
        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )
        definition = expert_dir / f"{installed.definition_id}.toml"
        definition.write_text(
            definition.read_text(encoding="utf-8")
            .replace("enabled = true", "enabled = false")
            .replace("Expert description", "Local variant"),
            encoding="utf-8",
        )
        (source_dir / "skills" / "skill_a" / "SKILL.md").write_text(
            "# updated\n",
            encoding="utf-8",
        )
        updated, _ = await _publish(service, "source-a", source_dir)

        result = await service.distribute_expert(
            "source-a",
            item.item_id,
            "manager",
            ExpertDistributionRequest(
                target_type="user_id",
                target_values=["alice"],
            ),
        )

        assert result.distributed_count == 1
        payload = tomllib.loads(definition.read_text(encoding="utf-8"))
        assert payload["enabled"] is False
        assert payload["description"] == "Expert description"
        assert payload["community"]["version"] == updated.version
        assert (
            expert_dir
            / f"{installed.definition_id}.dependencies"
            / "skills"
            / "skill_a"
            / "SKILL.md"
        ).read_text(encoding="utf-8") == "# updated\n"

    @pytest.mark.asyncio
    async def test_failed_update_restores_previous_dependencies(
        self,
        service: MarketplaceService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed definition commit must not leave updated dependencies."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir, skill_content="# original\n")
        item, _ = await _publish(service, "source-a", source_dir)
        installed = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
        )
        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )
        definition_path = expert_dir / f"{installed.definition_id}.toml"
        (source_dir / "skills" / "skill_a" / "SKILL.md").write_text(
            "# updated\n",
            encoding="utf-8",
        )
        await _publish(service, "source-a", source_dir)
        original_replace = os.replace

        def fail_definition_commit(
            source: str | Path,
            target: str | Path,
        ) -> None:
            if Path(target) == definition_path:
                raise OSError("definition commit failed")
            original_replace(source, target)

        monkeypatch.setattr(
            "market.marketplace.service.os.replace",
            fail_definition_commit,
        )

        with pytest.raises(OSError, match="definition commit failed"):
            service._install_expert_for_user(
                "source-a",
                item.item_id,
                "alice",
                "default",
                "manager",
                update=True,
            )

        assert (
            expert_dir
            / f"{installed.definition_id}.dependencies"
            / "skills"
            / "skill_a"
            / "SKILL.md"
        ).read_text(encoding="utf-8") == "# original\n"

    @pytest.mark.asyncio
    async def test_recall_removes_received_definition_by_community_item_id(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        item, _ = await _publish(service, "source-a", source_dir)
        installed = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
        )
        workspace_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        ).parent
        session_view = (
            workspace_dir
            / ".expert_sessions"
            / "chat-1"
            / installed.definition_id
        )
        session_view.mkdir(parents=True)
        (session_view / "marker").write_text("active", encoding="utf-8")

        recalled = await service.recall_expert(
            "source-a",
            item.item_id,
            "manager",
            ["alice"],
        )

        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )
        assert recalled.recalled_count == 1
        assert not (expert_dir / f"{installed.definition_id}.toml").exists()
        assert not (
            expert_dir / f"{installed.definition_id}.dependencies"
        ).exists()
        assert not session_view.exists()

    @pytest.mark.asyncio
    async def test_recall_reports_pending_when_agent_reload_fails(
        self,
        service: MarketplaceService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        item, _ = await _publish(service, "source-a", source_dir)
        await service.install_expert("source-a", item.item_id, "alice")

        monkeypatch.setattr(
            service,
            "_trigger_agent_reload",
            AsyncMock(return_value=False),
        )
        recalled = await service.recall_expert(
            "source-a",
            item.item_id,
            "manager",
            ["alice"],
        )

        assert recalled.recalled_count == 0
        assert recalled.failed_count == 1
        assert "pending retry" in recalled.results[0].reason

    @pytest.mark.asyncio
    async def test_distribution_installs_first_copy_to_default_profile(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        item, _ = await _publish(service, "source-a", source_dir)

        distributed = await service.distribute_expert(
            "source-a",
            item.item_id,
            "manager",
            ExpertDistributionRequest(
                target_type="user_id",
                target_values=["alice"],
            ),
        )

        assert distributed.distributed_count == 1
        assert distributed.results[0].definition_id
        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )
        assert (
            expert_dir / f"{distributed.results[0].definition_id}.toml"
        ).is_file()

    @pytest.mark.asyncio
    async def test_recall_removes_received_copies_from_every_profile(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        item, _ = await _publish(service, "source-a", source_dir)
        default_install = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
        )
        research_install = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
            "research",
        )

        recalled = await service.recall_expert(
            "source-a",
            item.item_id,
            "manager",
            ["alice"],
        )

        assert recalled.recalled_count == 1
        for agent_id, definition_id in (
            ("default", default_install.definition_id),
            ("research", research_install.definition_id),
        ):
            expert_dir = get_user_expert_dir(
                service.swe_root,
                "alice",
                agent_id,
                "source-a",
            )
            assert not (expert_dir / f"{definition_id}.toml").exists()
            assert not (expert_dir / f"{definition_id}.dependencies").exists()

    @pytest.mark.asyncio
    async def test_all_user_recall_falls_back_to_local_received_scopes(
        self,
        service: MarketplaceService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        _write_source_package(source_dir)
        item, _ = await _publish(service, "source-a", source_dir)
        installed = await service.install_expert(
            "source-a",
            item.item_id,
            "alice",
        )

        recalled = await service.recall_expert(
            "source-a",
            item.item_id,
            "manager",
        )

        expert_dir = get_user_expert_dir(
            service.swe_root,
            "alice",
            "default",
            "source-a",
        )
        assert recalled.recalled_count == 1
        assert not (expert_dir / f"{installed.definition_id}.toml").exists()


@pytest.mark.asyncio
async def test_distribution_without_swe_package(
    service: MarketplaceService,
    published_dependencies,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    _, item, _ = published_dependencies
    original_import = builtins.__import__

    def import_without_swe(name, *args, **kwargs):
        if name == "swe" or name.startswith("swe."):
            raise ModuleNotFoundError("No module named 'swe'", name="swe")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_swe)
    result = await service.distribute_expert(
        "source-a",
        item.item_id,
        "manager",
        ExpertDistributionRequest(
            target_type="user_id",
            target_values=["alice"],
        ),
    )

    assert result.distributed_count == 1, result.results
    assert result.results[0].success


@pytest.mark.asyncio
async def test_agent_reload_stops_after_success(
    service: MarketplaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(200, text="ok")
    post = AsyncMock(return_value=response)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = post
    monkeypatch.setattr(
        "market.marketplace.service.httpx.AsyncClient",
        lambda **_: client,
    )

    result = await MarketplaceService._trigger_agent_reload(
        service,
        "alice",
        "default",
        "source-a",
    )

    assert result is True
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_publish_and_distribution_loads_skill_and_mcp(
    service: MarketplaceService,
    tmp_path: Path,
) -> None:
    from swe.app.subagents import AgentOwnedDefinitionRepository
    from swe.app.subagents.launch_snapshot import (
        read_and_remove_private_mcp_snapshot,
    )
    from swe.app.subagents.session_dependencies import (
        capture_community_expert_session_dependencies,
        initialize_community_expert_dependency_view,
    )
    from swe.config.config import AgentProfileConfig

    source = get_user_expert_dir(
        service.swe_root,
        "author",
        "default",
        "source-a",
    )
    source.mkdir(parents=True)
    skill = source.parent / "skills" / "skill_a"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# workspace skill\n")
    (source.parent / "agent.json").write_text(
        json.dumps(
            {
                "mcp": {"clients": {"mcp_a": {"command": "workspace-tool"}}},
            },
        ),
    )
    (source / "expert.toml").write_text(
        'name = "Profile Expert"\n'
        'description = "A profile expert"\n'
        'instruction = "Use dependencies."\n'
        'skills = ["skill_a"]\n'
        'mcps = ["mcp_a"]\n',
    )
    item, _ = await service.publish_expert_from_profile(
        "source-a",
        "author",
        "default",
        "expert",
    )
    result = await service.distribute_expert(
        "source-a",
        item.item_id,
        "manager",
        ExpertDistributionRequest(
            target_type="user_id",
            target_values=["alice"],
        ),
    )
    assert result.distributed_count == 1, result.results
    expert_dir = get_user_expert_dir(
        service.swe_root,
        "alice",
        "default",
        "source-a",
    )
    package = AgentOwnedDefinitionRepository(
        expert_dir,
        owner_scope="alice/default",
    ).get(
        result.results[0].definition_id,
    )
    assert package is not None and package.valid
    view = initialize_community_expert_dependency_view(
        workspace_dir=expert_dir.parent,
        chat_id="00000000-0000-4000-8000-000000000001",
        definition=package.definition,
    )
    run_store = tmp_path / "run-store"
    paths, mcp_path, diagnostics = (
        capture_community_expert_session_dependencies(
            run_store_dir=run_store,
            run_id="profile-publish-test",
            dependency_view_root=view,
            definition=package.definition,
            parent_agent_config=AgentProfileConfig(
                id="default",
                name="Default",
            ),
        )
    )
    assert diagnostics.loaded_skills == ["skill_a"]
    assert (Path(paths[0]) / "SKILL.md").read_text() == "# workspace skill\n"
    snapshot = read_and_remove_private_mcp_snapshot(
        mcp_path,
        run_store_dir=run_store,
        run_id="profile-publish-test",
    )
    assert snapshot["mcp_a"]["command"] == "workspace-tool"


@pytest.mark.asyncio
async def test_agent_reload_retries_failed_responses(
    service: MarketplaceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = AsyncMock(side_effect=[httpx.Response(503), httpx.Response(200)])
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = post
    monkeypatch.setattr(
        "market.marketplace.service.httpx.AsyncClient",
        lambda **_: client,
    )

    result = await MarketplaceService._trigger_agent_reload(
        service,
        "alice",
        "default",
        "source-a",
    )

    assert result is True
    assert post.await_count == 2
