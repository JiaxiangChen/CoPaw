# -*- coding: utf-8 -*-
import json
import shutil
import pytest
from unittest.mock import AsyncMock, Mock

from market.marketplace.errors import MarketplaceMetadataSyncError


def _make_service(tmp_path, mock_db=None):
    from market.marketplace.service import MarketplaceService
    from market.marketplace.fs import load_index

    if mock_db is None:
        mock_db = AsyncMock()
        mock_db.is_connected = True
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.fetch_all = AsyncMock(return_value=[])
    svc = MarketplaceService(
        db=mock_db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )

    async def list_skills_from_index(source_id, **_kwargs):
        return [
            item
            for item in load_index(svc.marketplace_root, source_id)
            if item.item_type == "skill"
        ]

    async def get_skill_from_index(source_id, item_id):
        return next(
            (
                item
                for item in load_index(svc.marketplace_root, source_id)
                if item.item_type == "skill" and item.item_id == item_id
            ),
            None,
        )

    async def list_mcps_from_index(source_id, **_kwargs):
        return [
            item
            for item in load_index(svc.marketplace_root, source_id)
            if item.item_type == "mcp"
        ]

    async def get_mcp_from_index(source_id, item_id):
        return next(
            (
                item
                for item in load_index(svc.marketplace_root, source_id)
                if item.item_type == "mcp" and item.item_id == item_id
            ),
            None,
        )

    svc.market_skill_registry.list_market_skills = list_skills_from_index
    svc.market_skill_registry.get_market_skill = get_skill_from_index
    svc.mcp_market_registry.list_market_mcps = list_mcps_from_index
    svc.mcp_market_registry.get_market_mcp = get_mcp_from_index
    return svc


@pytest.mark.asyncio
async def test_list_skills_reads_market_metadata_from_tdsql_without_index(
    tmp_path,
):
    from market.marketplace.models import MarketItem

    svc = _make_service(tmp_path)
    svc.market_skill_registry.list_market_skills = AsyncMock(
        return_value=[
            MarketItem(
                item_id="item-1",
                item_type="skill",
                name="risk_check",
                skill_id="skill-1",
                description="desc",
                creator_id="u1",
                bbk_ids=[],
                status="active",
            ),
        ],
    )

    items = await svc.list_skills(
        "src-1",
        user_bbk_id="100",
        is_manager=True,
    )

    assert [item.item_id for item in items] == ["item-1"]
    svc.market_skill_registry.list_market_skills.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_skill_fails_when_tdsql_metadata_sync_returns_false(
    tmp_path,
):
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    svc.market_skill_registry.upsert_market_skill = AsyncMock(
        return_value=False,
    )

    with pytest.raises(MarketplaceMetadataSyncError):
        await svc.publish_skill(
            "src_a",
            PublishSkillRequest(
                name="sync-failure",
                creator_id="u1",
                creator_name="User",
                skill_json={},
                skill_md="",
            ),
        )


@pytest.mark.asyncio
async def test_publish_skill_fails_when_database_is_unavailable(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    mock_db = AsyncMock()
    mock_db.is_connected = False
    svc = _make_service(tmp_path, mock_db)

    with pytest.raises(MarketplaceMetadataSyncError):
        await svc.publish_skill(
            "src_a",
            PublishSkillRequest(
                name="db-down",
                creator_id="u1",
                creator_name="User",
                skill_json={},
                skill_md="",
            ),
        )


@pytest.mark.asyncio
async def test_publish_mcp_fails_when_database_is_unavailable(tmp_path):
    from market.marketplace.schemas import PublishMCPRequest

    mock_db = AsyncMock()
    mock_db.is_connected = False
    svc = _make_service(tmp_path, mock_db)

    with pytest.raises(MarketplaceMetadataSyncError):
        await svc.publish_mcp(
            "src_a",
            PublishMCPRequest(
                client_key="mcp-1",
                name="db-down-mcp",
                creator_id="u1",
                creator_name="User",
                config={
                    "name": "db-down-mcp",
                    "transport": "stdio",
                    "command": "echo",
                },
            ),
        )


@pytest.mark.asyncio
async def test_list_mcp_items_reads_market_metadata_from_tdsql_without_index(
    tmp_path,
):
    from market.marketplace.models import MarketItem

    svc = _make_service(tmp_path)
    svc.mcp_market_registry.list_market_mcps = AsyncMock(
        return_value=[
            MarketItem(
                item_id="mcp-1",
                item_type="mcp",
                client_key="weather",
                name="Weather",
                creator_id="u1",
                bbk_ids=[],
                status="active",
            ),
        ],
    )

    items = await svc.list_mcp_items(
        "src-1",
        user_bbk_id="100",
        is_manager=True,
    )

    assert [item.item_id for item in items] == ["mcp-1"]
    svc.mcp_market_registry.list_market_mcps.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_skill_detail_reads_market_metadata_from_tdsql_without_index(
    tmp_path,
):
    from market.marketplace.models import MarketItem

    svc = _make_service(tmp_path)
    svc.market_skill_registry.get_market_skill = AsyncMock(
        return_value=MarketItem(
            item_id="item-1",
            item_type="skill",
            name="risk_check",
            skill_id="skill-1",
            description="desc",
            creator_id="u1",
            bbk_ids=[],
            status="active",
        ),
    )
    svc._get_stats = AsyncMock(return_value=(2, 1))
    svc._get_user_stats = AsyncMock(return_value=[])

    detail = await svc.get_skill_detail(
        "src-1",
        "item-1",
        user_bbk_id="100",
        is_manager=True,
    )

    assert detail is not None
    assert detail.name == "risk_check"
    svc.market_skill_registry.get_market_skill.assert_awaited_once_with(
        "src-1",
        "item-1",
    )


@pytest.mark.asyncio
async def test_get_mcp_detail_reads_market_metadata_from_tdsql_without_index(
    tmp_path,
):
    from market.marketplace.fs import save_mcp_config
    from market.marketplace.models import MarketItem

    svc = _make_service(tmp_path)
    svc.mcp_market_registry.get_market_mcp = AsyncMock(
        return_value=MarketItem(
            item_id="mcp-1",
            item_type="mcp",
            client_key="weather",
            name="Weather",
            creator_id="u1",
            bbk_ids=[],
            status="active",
        ),
    )
    save_mcp_config(
        svc.marketplace_root,
        "src-1",
        "mcp-1",
        {
            "client_key": "weather",
            "config": {
                "transport": "stdio",
                "command": "weather",
                "env": {"TOKEN": "secret"},
            },
        },
    )

    detail = await svc.get_mcp_detail(
        "src-1",
        "mcp-1",
        user_bbk_id="100",
    )

    assert detail is not None
    assert detail.name == "Weather"
    assert detail.config.env["TOKEN"] != "secret"
    svc.mcp_market_registry.get_market_mcp.assert_awaited_once_with(
        "src-1",
        "mcp-1",
    )


def test_register_skill_in_manifest_rejects_malformed_shared_manifest(
    tmp_path,
):
    from market.marketplace.fs import (
        WorkspaceSkillManifestError,
        get_user_skill_manifest_path,
    )

    svc = _make_service(tmp_path)
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        "user1",
        "agent1",
        "source_a",
    )
    manifest_path.parent.mkdir(parents=True)
    original = b'{"layout_version": 2, "skills": {'
    manifest_path.write_bytes(original)

    with pytest.raises(WorkspaceSkillManifestError):
        svc.register_skill_in_manifest(
            "user1",
            "demo",
            "agent1",
            "source_a",
        )

    assert manifest_path.read_bytes() == original


def test_register_skill_in_manifest_preserves_external_fields_on_success(
    tmp_path,
):
    from market.marketplace.fs import get_user_skill_manifest_path

    svc = _make_service(tmp_path)
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        "user1",
        "agent1",
        "source_a",
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 7,
                "external_top_level": {"writer": "swe"},
                "skills": {
                    "demo": {
                        "enabled": True,
                        "channels": ["slack"],
                        "source": "external",
                        "config": {},
                        "metadata": {
                            "name": "Old Demo",
                            "description": "old description",
                            "version_text": "0.1.0",
                            "source": "external",
                            "protected": True,
                            "requirements": {"old": True},
                            "external_metadata": {
                                "owner": "swe",
                                "nested": {"keep": True},
                            },
                        },
                        "requirements": {"old": True},
                        "created_at": "2025-01-01T00:00:00+00:00",
                        "updated_at": "2025-01-01T00:00:00+00:00",
                        "external_entry_field": {"keep": [1, 2]},
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    assert svc.register_skill_in_manifest(
        "user1",
        "demo",
        "agent1",
        "source_a",
        enabled=False,
        source="marketplace:item-1",
        extra_metadata={
            "name": "Renamed Demo",
            "creator_id": "user2",
            "received_version": "2.3.4",
        },
    )

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = saved["skills"]["demo"]
    metadata = entry["metadata"]

    assert saved["external_top_level"] == {"writer": "swe"}
    assert entry["config"] == {}
    assert entry["external_entry_field"] == {"keep": [1, 2]}
    assert metadata["external_metadata"] == {
        "owner": "swe",
        "nested": {"keep": True},
    }
    assert entry["enabled"] is False
    assert entry["channels"] == ["slack"]
    assert entry["source"] == "marketplace:item-1"
    assert entry["created_at"] == "2025-01-01T00:00:00+00:00"
    assert entry["requirements"] == {
        "require_bins": [],
        "require_envs": [],
    }
    assert entry["updated_at"] != "2025-01-01T00:00:00+00:00"
    assert metadata["name"] == "Renamed Demo"
    assert metadata["description"] == ""
    assert metadata["version_text"] == "2.3.4"
    assert metadata["source"] == "marketplace:item-1"
    assert metadata["protected"] is False
    assert metadata["requirements"] == {
        "require_bins": [],
        "require_envs": [],
    }
    assert metadata["creator_id"] == "user2"


@pytest.mark.asyncio
async def test_enable_registered_hidden_skill_updates_manifest_and_reloads(
    tmp_path,
):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
        get_user_skills_dir,
    )

    svc = _make_service(tmp_path)
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.update_skill = AsyncMock()
    user_id = "user1"
    source_id = "source_a"
    hidden = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / "demo"
    )
    hidden.mkdir(parents=True)
    (hidden / "SKILL.md").write_text("# Demo", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        source_id=source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {"demo": {"enabled": False}},
            },
        ),
        encoding="utf-8",
    )

    result = await svc.enable_skill(user_id, "demo", source_id=source_id)

    assert result == {"success": True}
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][
            "demo"
        ]["enabled"]
        is True
    )
    assert not hidden.exists()
    assert (
        get_user_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / "demo"
        / "SKILL.md"
    ).is_file()
    svc._trigger_agent_reload.assert_awaited_once_with(
        user_id,
        "default",
        source_id,
    )


@pytest.mark.asyncio
async def test_enable_skill_rejects_registered_package_in_both_roots(tmp_path):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
        get_user_skills_dir,
    )

    svc = _make_service(tmp_path)
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.update_skill = AsyncMock()
    user_id = "user1"
    source_id = "source_a"
    active = (
        get_user_skills_dir(tmp_path / "swe", user_id, source_id=source_id)
        / "demo"
    )
    hidden = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / "demo"
    )
    active.mkdir(parents=True)
    hidden.mkdir(parents=True)
    (active / "SKILL.md").write_text("# Active", encoding="utf-8")
    (hidden / "SKILL.md").write_text("# Hidden", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        source_id=source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {"demo": {"enabled": False}},
            },
        ),
        encoding="utf-8",
    )

    result = await svc.enable_skill(user_id, "demo", source_id=source_id)

    assert result == {"success": False}
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][
            "demo"
        ]["enabled"]
        is False
    )
    assert (active / "SKILL.md").read_text(encoding="utf-8") == "# Active"
    assert (hidden / "SKILL.md").read_text(encoding="utf-8") == "# Hidden"
    svc._trigger_agent_reload.assert_not_awaited()
    svc.skill_registry.update_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_enable_skill_rechecks_registration_before_moving_package(
    tmp_path,
):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
        get_user_skills_dir,
    )

    svc = _make_service(tmp_path)
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.update_skill = AsyncMock()
    user_id = "user1"
    source_id = "source_a"
    active = (
        get_user_skills_dir(tmp_path / "swe", user_id, source_id=source_id)
        / "demo"
    )
    hidden = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / "demo"
    )
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("# Demo", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        source_id=source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {},
            },
        ),
        encoding="utf-8",
    )

    def register_and_disable_during_scan(*_args, **_kwargs):
        hidden.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(active, hidden)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "workspace-skill-manifest.v1",
                    "layout_version": 2,
                    "version": 0,
                    "skills": {"demo": {"enabled": False}},
                },
            ),
            encoding="utf-8",
        )

    svc._scan_skill_or_raise = register_and_disable_during_scan

    result = await svc.enable_skill(user_id, "demo", source_id=source_id)

    assert result == {"success": True}
    assert not hidden.exists()
    assert (active / "SKILL.md").is_file()
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][
            "demo"
        ]["enabled"]
        is True
    )


@pytest.mark.asyncio
async def test_enable_skill_rolls_back_package_move_when_manifest_write_fails(
    tmp_path,
    monkeypatch,
):
    from market.marketplace import fs
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
        get_user_skills_dir,
    )

    svc = _make_service(tmp_path)
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.update_skill = AsyncMock()
    user_id = "user1"
    source_id = "source_a"
    hidden = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / "demo"
    )
    hidden.mkdir(parents=True)
    (hidden / "SKILL.md").write_text("# Demo", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        source_id=source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {"demo": {"enabled": False}},
            },
        ),
        encoding="utf-8",
    )

    def fail_manifest_write(*_args, **_kwargs):
        raise OSError("manifest write failed")

    monkeypatch.setattr(fs, "_atomic_write_json", fail_manifest_write)

    with pytest.raises(OSError, match="manifest write failed"):
        await svc.enable_skill(user_id, "demo", source_id=source_id)

    active = (
        get_user_skills_dir(tmp_path / "swe", user_id, source_id=source_id)
        / "demo"
    )
    assert not active.exists()
    assert (hidden / "SKILL.md").is_file()
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][
            "demo"
        ]["enabled"]
        is False
    )
    svc._trigger_agent_reload.assert_not_awaited()
    svc.skill_registry.update_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_skill_moves_registered_package_to_disabled_root(
    tmp_path,
):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
        get_user_skills_dir,
    )

    svc = _make_service(tmp_path)
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.update_skill = AsyncMock()
    user_id = "user1"
    agent_id = "agent1"
    source_id = "source_a"
    skill_name = "demo"
    active = (
        get_user_skills_dir(
            tmp_path / "swe",
            user_id,
            agent_id,
            source_id,
        )
        / skill_name
    )
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("# Demo", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        agent_id,
        source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {skill_name: {"enabled": True}},
            },
        ),
        encoding="utf-8",
    )

    result = await svc.disable_skill(
        user_id,
        skill_name,
        agent_id,
        source_id,
    )

    disabled = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            agent_id,
            source_id,
        )
        / skill_name
    )
    assert result == {"success": True}
    assert not active.exists()
    assert (disabled / "SKILL.md").read_text(encoding="utf-8") == "# Demo"
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][
            skill_name
        ]["enabled"]
        is False
    )
    svc._trigger_agent_reload.assert_awaited_once_with(
        user_id,
        agent_id,
        source_id,
    )
    svc.skill_registry.update_skill.assert_awaited_once_with(
        user_id=user_id,
        skill_name=skill_name,
        source_id=source_id,
        enabled=False,
    )


@pytest.mark.asyncio
async def test_disable_skill_keeps_manifest_enabled_when_move_fails(
    tmp_path,
    monkeypatch,
):
    from market.marketplace.fs import (
        get_user_skill_manifest_path,
        get_user_skills_dir,
    )

    svc = _make_service(tmp_path)
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.update_skill = AsyncMock()
    active = get_user_skills_dir(tmp_path / "swe", "user1") / "demo"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("# Demo", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(tmp_path / "swe", "user1")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {"demo": {"enabled": True}},
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "market.marketplace.service.shutil.move",
        Mock(side_effect=OSError("disk full")),
    )

    result = await svc.disable_skill("user1", "demo")

    assert result == {"success": False}
    assert active.exists()
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["skills"][
            "demo"
        ]["enabled"]
        is True
    )
    svc._trigger_agent_reload.assert_not_awaited()
    svc.skill_registry.update_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_skill_removes_registered_disabled_package(tmp_path):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
    )

    svc = _make_service(tmp_path)
    svc.skill_registry.delete_skill = AsyncMock()
    user_id = "user1"
    source_id = "source_a"
    skill_name = "demo"
    disabled = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / skill_name
    )
    disabled.mkdir(parents=True)
    (disabled / "SKILL.md").write_text("# Demo", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        source_id=source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {skill_name: {"enabled": False}},
            },
        ),
        encoding="utf-8",
    )

    result = await svc.delete_skill(
        user_id,
        skill_name,
        source_id=source_id,
    )

    assert result is True
    assert not disabled.exists()
    assert (
        skill_name
        not in json.loads(
            manifest_path.read_text(encoding="utf-8"),
        )["skills"]
    )
    svc.skill_registry.delete_skill.assert_awaited_once_with(
        user_id,
        skill_name,
        source_id,
    )


def test_save_skill_file_updates_registered_disabled_package(tmp_path):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
    )

    svc = _make_service(tmp_path)
    user_id = "user1"
    source_id = "source_a"
    skill_name = "demo"
    disabled = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / skill_name
    )
    disabled.mkdir(parents=True)
    (disabled / "SKILL.md").write_text(
        "---\nversion: 1.0.0\n---\n# Demo\n",
        encoding="utf-8",
    )
    (disabled / "notes.txt").write_text("before", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        source_id=source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {skill_name: {"enabled": False}},
            },
        ),
        encoding="utf-8",
    )

    success, _ = svc.save_skill_file(
        user_id,
        skill_name,
        "notes.txt",
        "after",
        source_id=source_id,
    )

    assert success is True
    assert (disabled / "notes.txt").read_text(encoding="utf-8") == "after"


@pytest.mark.asyncio
async def test_batch_delete_skills_removes_package_after_disabling(tmp_path):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
        get_user_skills_dir,
    )

    svc = _make_service(tmp_path)
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.update_skill = AsyncMock()
    svc.skill_registry.delete_skill = AsyncMock()
    user_id = "user1"
    source_id = "source_a"
    skill_name = "demo"
    active = (
        get_user_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / skill_name
    )
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("# Demo", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        source_id=source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 0,
                "skills": {skill_name: {"enabled": True}},
            },
        ),
        encoding="utf-8",
    )

    result = await svc.batch_delete_skills(
        user_id,
        [skill_name],
        source_id=source_id,
    )

    disabled = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / skill_name
    )
    assert result == {skill_name: {"success": True}}
    assert not active.exists()
    assert not disabled.exists()
    assert (
        skill_name
        not in json.loads(
            manifest_path.read_text(encoding="utf-8"),
        )["skills"]
    )
    svc.skill_registry.delete_skill.assert_awaited_once_with(
        user_id,
        skill_name,
        source_id,
    )


@pytest.mark.asyncio
async def test_distribution_preserves_disabled_result_without_reload(
    tmp_path,
    monkeypatch,
):
    from market.marketplace.fs import save_index
    from market.marketplace.models import MarketItem
    from market.marketplace.schemas import DistributeRequest
    from market.marketplace import service as service_module

    svc = _make_service(tmp_path)
    svc._resolve_target_users = AsyncMock(
        return_value=[{"tenant_id": "user1", "tenant_name": "User"}],
    )
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.insert_skill = AsyncMock(return_value=True)
    svc.register_skill_in_manifest = Mock(return_value=True)
    save_index(
        tmp_path / "market",
        "source",
        [
            MarketItem(
                item_id="item",
                name="demo",
                description="new description",
                version="1.0.0",
                creator_id="owner",
            ),
        ],
    )
    monkeypatch.setattr(
        service_module,
        "copy_skill_to_user",
        lambda **_kwargs: {
            "status": "distributed",
            "metadata": {},
            "package_path": tmp_path / "hidden" / "demo",
            "final_enabled": False,
            "promoted": False,
        },
    )

    result = await svc.distribute_skill(
        "source",
        "item",
        "operator",
        "Operator",
        DistributeRequest(target_type="user_id", target_values=["user1"]),
    )

    assert result.distributed_count == 1
    assert svc.register_skill_in_manifest.call_args.kwargs["enabled"] is False
    assert svc.skill_registry.insert_skill.call_args.kwargs["enabled"] is False
    svc._trigger_agent_reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_distribute_skill_reports_copy_exception_per_user(
    tmp_path,
    monkeypatch,
):
    """单用户复制异常必须进入分发结果，不能被外层误判成功。"""
    from market.marketplace.fs import save_index
    from market.marketplace.models import MarketItem
    from market.marketplace.schemas import DistributeRequest
    from market.marketplace import service as service_module

    svc = _make_service(tmp_path)
    svc._resolve_target_users = AsyncMock(
        return_value=[
            {"tenant_id": "user-ok", "tenant_name": "OK"},
            {"tenant_id": "user-fail", "tenant_name": "FAIL"},
        ],
    )
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.insert_skill = AsyncMock(return_value=True)
    svc.register_skill_in_manifest = Mock(return_value=True)
    save_index(
        tmp_path / "market",
        "source",
        [
            MarketItem(
                item_id="item",
                name="demo",
                description="new description",
                version="1.0.0",
                creator_id="owner",
            ),
        ],
    )

    def fake_copy_skill_to_user(**kwargs):
        if kwargs["user_id"] == "user-fail":
            raise RuntimeError("copy failed")
        return {
            "status": "distributed",
            "metadata": {},
            "package_path": tmp_path / "active" / "demo",
            "final_enabled": True,
            "promoted": False,
        }

    monkeypatch.setattr(
        service_module,
        "copy_skill_to_user",
        fake_copy_skill_to_user,
    )

    result = await svc.distribute_skill(
        "source",
        "item",
        "operator",
        "Operator",
        DistributeRequest(target_type="user_id", target_values=["user-ok"]),
    )

    assert result.distributed_count == 1
    assert result.failed_count == 1
    assert {item.user_id: item.success for item in result.results} == {
        "user-ok": True,
        "user-fail": False,
    }
    failed = next(
        item for item in result.results if item.user_id == "user-fail"
    )
    assert failed.error == "copy failed"


@pytest.mark.asyncio
async def test_publish_skill_creates_index_entry(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    req = PublishSkillRequest(
        name="skill_a",
        description="desc",
        creator_id="user1",
        creator_name="User One",
        skill_json={"name": "skill_a"},
        skill_md="# Skill A",
    )
    item, _ = await svc.publish_skill("src_a", req)
    assert item.name == "skill_a"
    assert item.version == "1.0.0"
    assert item.status == "active"
    # index.json should exist
    index_path = tmp_path / "market" / "src_a" / "index.json"
    assert index_path.exists()
    data = json.loads(index_path.read_text())
    assert len(data["items"]) == 1


@pytest.mark.asyncio
async def test_publish_skill_increments_version_on_republish(tmp_path):
    """F1/F2 修复后：内容变化才 bump；内容不变走 R7 no-op，版本号不动."""
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    req1 = PublishSkillRequest(
        name="skill_a",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="# v1",
    )
    item1, _ = await svc.publish_skill("src_a", req1)
    assert item1.version == "1.0.0"

    # 同样内容再 publish 一次 → R7 no-op，版本不动
    item_same, _ = await svc.publish_skill(
        "src_a",
        req1.model_copy(update={"overwrite": True}),
    )
    assert (
        item_same.version == "1.0.0"
    ), "内容未变化时市场版本不应 bump（R7 no-op）"

    # 改了内容再 publish → 自动 bump 到 1.0.1
    req2 = PublishSkillRequest(
        name="skill_a",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="# v2 changed",
        overwrite=True,
    )
    item2, _ = await svc.publish_skill("src_a", req2)
    assert item2.version == "1.0.1", "内容变化时市场版本应自动 bump"


@pytest.mark.asyncio
async def test_unpublish_skill_sets_inactive(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    req = PublishSkillRequest(
        name="skill_b",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
    )
    item, _ = await svc.publish_skill("src_a", req)
    await svc.unpublish_skill("src_a", item.item_id, "u1", "User One")
    items = await svc.list_skills("src_a", user_bbk_id="100", is_manager=False)
    assert all(
        i.status == "inactive" for i in items if i.item_id == item.item_id
    )


@pytest.mark.asyncio
async def test_unpublish_skill_soft_marks_marketplace_skill(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    req = PublishSkillRequest(
        name="skill_soft_unpublish",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
    )
    item, _ = await svc.publish_skill("src_a", req)
    svc.db.execute.reset_mock()

    await svc.unpublish_skill("src_a", item.item_id, "u1", "User One")

    sql_text = "\n".join(
        call.args[0] for call in svc.db.execute.await_args_list
    )
    assert "DELETE FROM swe_marketplace_skills" not in sql_text
    assert "UPDATE swe_marketplace_skills" in sql_text
    assert "is_unpublished = 1" in sql_text


@pytest.mark.asyncio
async def test_delete_market_skill_soft_marks_marketplace_skill(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    req = PublishSkillRequest(
        name="skill_soft_delete",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
    )
    item, _ = await svc.publish_skill("src_a", req)
    svc.db.execute.reset_mock()

    await svc.delete_market_skill("src_a", item.item_id, "u1", "User One")

    sql_text = "\n".join(
        call.args[0] for call in svc.db.execute.await_args_list
    )
    assert "DELETE FROM swe_marketplace_skills" not in sql_text
    assert "UPDATE swe_marketplace_skills" in sql_text
    assert "is_deleted = 1" in sql_text


@pytest.mark.asyncio
async def test_list_skills_filters_by_explicit_bbk_ids(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    # skill visible to all (bbk_ids=[])
    req_all = PublishSkillRequest(
        name="skill_all",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
        bbk_ids=[],
    )
    # skill visible only to bbk_id=200
    req_200 = PublishSkillRequest(
        name="skill_200",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
        bbk_ids=["200"],
    )
    await svc.publish_skill("src_a", req_all)
    await svc.publish_skill("src_a", req_200)
    # user_bbk_id filters by visibility; explicit bbk_ids narrows further.
    # With user_bbk_id=200 (non-manager), sees skill_all ([]) + skill_200 (["200"]).
    items_200_all = await svc.list_skills(
        "src_a",
        user_bbk_id="200",
        is_manager=False,
    )
    assert len(items_200_all) == 2
    # user_bbk_id=300 (non-manager) only sees HQ skill (bbk_ids=[]).
    # Even if bbk_ids=["200"] is passed, visibility filter blocks skill_200 first.
    items_300 = await svc.list_skills(
        "src_a",
        user_bbk_id="300",
        bbk_ids=["200"],
        is_manager=False,
    )
    assert len(items_300) == 0  # bbk_ids=200 is not visible to bbk 300


@pytest.mark.asyncio
async def test_list_skills_tiered_visibility(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    # skill visible to all (bbk_ids=[])
    req_hq = PublishSkillRequest(
        name="skill_hq",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
        bbk_ids=[],
    )
    # skill visible only to bbk_id=200
    req_200 = PublishSkillRequest(
        name="skill_200",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
        bbk_ids=["200"],
    )
    await svc.publish_skill("src_a", req_hq)
    await svc.publish_skill("src_a", req_200)
    # Manager sees all skills
    items_manager = await svc.list_skills(
        "src_a",
        user_bbk_id="200",
        is_manager=True,
    )
    assert len(items_manager) == 2
    # Non-manager (bbk 300) sees only HQ skill (bbk_ids=[])
    items_300 = await svc.list_skills(
        "src_a",
        user_bbk_id="300",
        is_manager=False,
    )
    assert len(items_300) == 1
    assert items_300[0].name == "skill_hq"
    # Non-manager (bbk 200) sees HQ + bbk_200 skills
    items_200 = await svc.list_skills(
        "src_a",
        user_bbk_id="200",
        is_manager=False,
    )
    assert len(items_200) == 2


@pytest.mark.asyncio
async def test_get_skill_detail_returns_item(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    svc = _make_service(tmp_path)
    req = PublishSkillRequest(
        name="skill_c",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
    )
    item, _ = await svc.publish_skill("src_a", req)
    detail = await svc.get_skill_detail(
        "src_a",
        item.item_id,
        user_bbk_id="100",
    )
    assert detail is not None
    assert detail.item_id == item.item_id


@pytest.mark.asyncio
async def test_get_skill_detail_returns_none_for_unknown(tmp_path):
    svc = _make_service(tmp_path)
    detail = await svc.get_skill_detail(
        "src_a",
        "nonexistent-id",
        user_bbk_id="100",
    )
    assert detail is None


@pytest.mark.asyncio
async def test_get_my_skills_returns_time_fields(tmp_path):
    """get_my_skills 应返回 created_at 和 updated_at 字段."""
    from market.marketplace.fs import get_user_skill_manifest_path
    from market.marketplace.service import get_user_skills_dir

    svc = _make_service(tmp_path)
    user_id = "test_user"
    source_id = "test_source"
    agent_id = "default"

    # 创建用户技能目录
    skills_dir = get_user_skills_dir(
        tmp_path / "swe",
        user_id,
        agent_id,
        source_id,
    )
    skill_dir = skills_dir / "test_skill"
    skill_dir.mkdir(parents=True)

    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        agent_id,
        source_id,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "version": 1,
                "skills": {
                    "test_skill": {
                        "source": "customized",
                        "created_at": "2025-05-14T10:00:00+00:00",
                        "updated_at": "2025-05-14T12:00:00+00:00",
                        "metadata": {
                            "name": "Test Skill",
                            "description": "A test skill",
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# Test Skill", encoding="utf-8")

    # 调用服务
    result = await svc.get_my_skills(source_id, user_id, agent_id)

    assert len(result) == 1
    assert result[0].skill_name == "test_skill"
    assert result[0].created_at == "2025-05-14T10:00:00+00:00"
    assert result[0].updated_at == "2025-05-14T12:00:00+00:00"


@pytest.mark.asyncio
async def test_get_my_skills_handles_missing_time_fields(tmp_path):
    """get_my_skills 应处理缺失的时间字段."""
    from market.marketplace.fs import get_user_skill_manifest_path
    from market.marketplace.service import get_user_skills_dir

    svc = _make_service(tmp_path)
    user_id = "test_user"
    source_id = "test_source"
    agent_id = "default"

    # 创建用户技能目录
    skills_dir = get_user_skills_dir(
        tmp_path / "swe",
        user_id,
        agent_id,
        source_id,
    )
    skill_dir = skills_dir / "old_skill"
    skill_dir.mkdir(parents=True)

    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        agent_id,
        source_id,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "version": 1,
                "skills": {
                    "old_skill": {
                        "source": "customized",
                        "metadata": {
                            "name": "Old Skill",
                            "description": "An old skill without time fields",
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("# Old Skill", encoding="utf-8")

    # 调用服务
    result = await svc.get_my_skills(source_id, user_id, agent_id)

    assert len(result) == 1
    assert result[0].skill_name == "old_skill"
    assert result[0].created_at is None
    assert result[0].updated_at is None


@pytest.mark.asyncio
async def test_get_my_skills_includes_registered_disabled_package(tmp_path):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
    )

    svc = _make_service(tmp_path)
    user_id = "test_user"
    source_id = "test_source"
    agent_id = "default"
    skill_name = "disabled_skill"
    skill_dir = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            agent_id,
            source_id,
        )
        / skill_name
    )
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Disabled Skill", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        agent_id,
        source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 1,
                "skills": {
                    skill_name: {
                        "enabled": False,
                        "source": "customized",
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    result = await svc.get_my_skills(source_id, user_id, agent_id)

    assert [item.skill_name for item in result] == [skill_name]
    assert result[0].enabled is False


@pytest.mark.asyncio
async def test_get_my_skills_reads_frontmatter_and_market_metadata(tmp_path):
    """get_my_skills 应组合 frontmatter、manifest 和市场版本信息."""
    from market.marketplace.fs import get_user_skill_manifest_path
    from market.marketplace.schemas import PublishSkillRequest
    from market.marketplace.service import get_user_skills_dir

    svc = _make_service(tmp_path)
    user_id = "test_user"
    source_id = "test_source"
    agent_id = "default"

    published, _ = await svc.publish_skill(
        source_id,
        PublishSkillRequest(
            name="Market Skill",
            description="market desc",
            creator_id="creator-1",
            creator_name="张三",
            skill_json={},
            skill_md="",
        ),
    )
    published_item_id = published.item_id
    latest, _ = await svc.publish_skill(
        source_id,
        PublishSkillRequest(
            name="Market Skill",
            description="market desc updated",
            creator_id="creator-1",
            creator_name="寮犱笁",
            skill_json={},
            skill_md="# updated market skill",
            overwrite=True,
        ),
    )
    assert latest.item_id == published_item_id
    assert latest.version == "1.0.1"

    skills_dir = get_user_skills_dir(
        tmp_path / "swe",
        user_id,
        agent_id,
        source_id,
    )
    skill_dir = skills_dir / "market_skill_copy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: Installed Alias\n"
        "version: 1.0.0\n"
        "description: 从前言读取\n"
        "---\n"
        "# Market Skill\n",
        encoding="utf-8",
    )

    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        agent_id,
        source_id,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "version": 1,
                "skills": {
                    "market_skill_copy": {
                        "source": f"marketplace:{published_item_id}",
                        "enabled": False,
                        "created_at": "2025-05-14T10:00:00+00:00",
                        "updated_at": "2025-05-14T12:00:00+00:00",
                        "metadata": {
                            "received_version": "1.0.0",
                            "distributed_by": "admin1",
                            "creator_name": "%E5%BC%A0%E4%B8%89",
                            "category_id": 9,
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = await svc.get_my_skills(source_id, user_id, agent_id)

    assert len(result) == 1
    assert result[0].display_name == "Installed Alias"
    assert result[0].description == "从前言读取"
    assert result[0].version == "1.0.0"
    assert result[0].received_version == "1.0.0"
    assert result[0].market_version == "1.0.1"
    assert result[0].is_received is True
    assert result[0].has_update is True
    assert result[0].enabled is False
    assert result[0].distributed_by == "admin1"
    assert result[0].creator_name == "张三"
    assert result[0].category == "9"
    assert result[0].created_at == "2025-05-14T10:00:00+00:00"
    assert result[0].updated_at == "2025-05-14T12:00:00+00:00"


@pytest.mark.asyncio
async def test_recall_skill_by_name_removes_skill_dir_and_manifest(tmp_path):
    """按名称撤回技能时，应删除目录、移除 manifest 记录并删除数据库记录."""
    from market.marketplace.fs import (
        get_user_skill_manifest_path,
        get_user_skills_dir,
    )
    from market.marketplace.schemas import RecallRequest

    mock_db = AsyncMock()
    mock_db.is_connected = False
    svc = _make_service(tmp_path, mock_db=mock_db)
    svc.disable_skill = AsyncMock(return_value={"success": True})
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.delete_skill = AsyncMock(return_value=False)

    user_id = "user-1"
    source_id = "source-1"
    skill_name = "skill_to_recall"

    skills_dir = get_user_skills_dir(
        tmp_path / "swe",
        user_id,
        "default",
        source_id,
    )
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")

    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        "default",
        source_id,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 1,
                "skills": {
                    skill_name: {
                        "enabled": True,
                        "source": "customized",
                        "metadata": {"name": skill_name},
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = await svc.recall_skill(
        source_id,
        None,
        "admin-1",
        "Admin",
        RecallRequest(skill_name=skill_name, target_user_ids=[user_id]),
    )

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.recalled_count == 1
    assert result.failed_count == 0
    assert result.results[0].success is True
    assert not skill_dir.exists()
    assert skill_name not in manifest_data["skills"]
    # 验证删除数据库记录被调用
    svc.skill_registry.delete_skill.assert_called_once_with(
        user_id,
        skill_name,
        source_id,
    )


@pytest.mark.asyncio
async def test_recall_skill_by_name_removes_disabled_package(tmp_path):
    """按名称撤回已禁用技能时，应从 disabled 根目录删除包."""
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
    )
    from market.marketplace.schemas import RecallRequest

    mock_db = AsyncMock()
    mock_db.is_connected = False
    svc = _make_service(tmp_path, mock_db=mock_db)
    svc._trigger_agent_reload = AsyncMock()
    svc.skill_registry.delete_skill = AsyncMock(return_value=False)

    user_id = "user-1"
    source_id = "source-1"
    skill_name = "disabled_skill"
    disabled_dir = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            "default",
            source_id,
        )
        / skill_name
    )
    disabled_dir.mkdir(parents=True)
    (disabled_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        "default",
        source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 1,
                "skills": {
                    skill_name: {
                        "enabled": False,
                        "source": "customized",
                        "metadata": {"name": skill_name},
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    result = await svc.recall_skill(
        source_id,
        None,
        "admin-1",
        "Admin",
        RecallRequest(skill_name=skill_name, target_user_ids=[user_id]),
    )

    assert result.recalled_count == 1
    assert result.failed_count == 0
    assert not disabled_dir.exists()
    assert (
        skill_name
        not in json.loads(
            manifest_path.read_text(encoding="utf-8"),
        )["skills"]
    )
    svc.skill_registry.delete_skill.assert_called_once_with(
        user_id,
        skill_name,
        source_id,
    )


@pytest.mark.asyncio
async def test_recall_mcp_by_name_removes_client_from_agent_config(tmp_path):
    """按名称撤回 MCP 时，应从 agent 配置中移除目标 client.

    撤回使用 mcp_name（name 字段）匹配，不依赖 dict key。
    即使用户配置中的 dict key 与 name 不同，也能正确找到并移除。
    """
    from market.marketplace.fs import resolve_effective_user_id
    from market.marketplace.schemas import RecallRequest

    mock_db = AsyncMock()
    mock_db.is_connected = False
    svc = _make_service(tmp_path, mock_db=mock_db)
    svc._trigger_agent_reload = AsyncMock()

    user_id = "user-1"
    source_id = "source-1"
    effective_user_id = resolve_effective_user_id(user_id, source_id)
    agent_config_path = (
        tmp_path
        / "swe"
        / effective_user_id
        / "workspaces"
        / "default"
        / "agent.json"
    )
    agent_config_path.parent.mkdir(parents=True, exist_ok=True)
    agent_config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "clients": {
                        "my-mcp-tool": {
                            "name": "My MCP Tool",
                            "source": "marketplace:item-1",
                        },
                        "other-client": {
                            "name": "Other Client",
                            "source": "marketplace:item-2",
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 按 mcp_name 撤回，dict key "my-mcp-tool" 与 name "My MCP Tool" 不同
    result = await svc.recall_mcp(
        source_id,
        None,
        "admin-1",
        "Admin",
        RecallRequest(mcp_name="My MCP Tool", target_user_ids=[user_id]),
    )

    config_data = json.loads(agent_config_path.read_text(encoding="utf-8"))
    assert result.recalled_count == 1
    assert result.failed_count == 0
    assert result.results[0].success is True
    assert "my-mcp-tool" not in config_data["mcp"]["clients"]
    assert "other-client" in config_data["mcp"]["clients"]


@pytest.mark.asyncio
async def test_publish_skill_appends_version_for_different_user(tmp_path):
    """T5 R4：不同用户同名 skill → 续接到现有 MarketItem，不再抛 SkillNameConflictError."""
    from market.marketplace.schemas import PublishSkillRequest
    from market.marketplace.fs import load_index

    svc = _make_service(tmp_path)
    # 用户 A 首发
    req_a = PublishSkillRequest(
        name="demo",
        description="a",
        creator_id="alice",
        creator_name="Alice",
        skill_json={"name": "demo"},
        skill_md='---\nname: demo\nversion: "1.0.0"\n---\n',
    )
    item_a, _ = await svc.publish_skill("src_a", req_a)

    # 用户 B 同名同步（不同 creator_id），显式确认 overwrite
    req_b = PublishSkillRequest(
        name="demo",
        description="b",
        creator_id="bob",
        creator_name="Bob",
        skill_json={"name": "demo"},
        skill_md='---\nname: demo\nversion: "2.0.0"\n---\n',
        overwrite=True,
    )
    item_b, _ = await svc.publish_skill("src_a", req_b)

    # 续接到同一个 item_id
    assert item_b.item_id == item_a.item_id
    # creator 跟随当前上传者
    assert item_b.creator_id == "bob"

    # 市场上仍只有一条
    items = load_index(tmp_path / "market", "src_a")
    demos = [i for i in items if i.name == "demo"]
    assert len(demos) == 1


@pytest.mark.asyncio
async def test_publish_skill_records_source_user_from_creator(tmp_path):
    """T6 R6：admin 走 PublishSkillRequest 时，source_user_id=req.creator_id;
    source_user_version 来自被引用用户工作区的 SKILL.md 中的版本.
    operator_* 用作 created_by."""
    from market.marketplace.schemas import PublishSkillRequest
    from market.marketplace.version_service import SkillVersionService

    svc = _make_service(tmp_path)
    req = PublishSkillRequest(
        name="demo",
        description="d",
        creator_id="alice",
        creator_name="Alice",
        skill_json={"name": "demo"},
        skill_md='---\nname: demo\nversion: "1.5.2"\n---\nbody',
    )
    item, _ = await svc.publish_skill(
        "src_a",
        req,
        operator_id="admin_id",
        operator_name="Admin",
    )

    vsvc = SkillVersionService(tmp_path / "market")
    listed = vsvc.list_versions("src_a", item.item_id)
    snap = listed["versions"][0]
    assert snap["source_user_id"] == "alice"
    assert snap["source_user_name"] == "Alice"
    assert snap["source_user_version"] == "1.5.2"
    assert snap["created_by"] == "admin_id"
    assert snap["created_by_name"] == "Admin"


@pytest.mark.asyncio
async def test_publish_mcp_appends_for_different_user(tmp_path):
    """T9 R4 + F1 R3：不同用户同名 MCP → 续接到现有 item，市场版本独立递增（不跟随用户本地版本）。"""
    from market.marketplace.schemas import PublishMCPRequest
    from market.marketplace.fs import load_index
    from market.marketplace.mcp_version_service import MCPVersionService

    svc = _make_service(tmp_path)

    # alice 首发（本地版本 1.0.0 → 市场首版 1.0.0）
    item_a, _ = await svc.publish_mcp(
        "src_a",
        PublishMCPRequest(
            client_key="m1",
            name="demo_mcp",
            description="a",
            creator_id="alice",
            creator_name="Alice",
            config={"name": "demo_mcp", "transport": "stdio", "command": "/a"},
            version="1.0.0",
        ),
    )

    # bob 同名同步（本地版本 2.0.0，但市场版本独立 _bump_patch 到 1.0.1）
    item_b, _ = await svc.publish_mcp(
        "src_a",
        PublishMCPRequest(
            client_key="m1",
            name="demo_mcp",
            description="b",
            creator_id="bob",
            creator_name="Bob",
            config={"name": "demo_mcp", "transport": "stdio", "command": "/b"},
            version="2.0.0",
            overwrite=True,
        ),
    )

    assert item_b.item_id == item_a.item_id
    items = load_index(tmp_path / "market", "src_a")
    demos = [i for i in items if i.name == "demo_mcp"]
    assert len(demos) == 1

    # F1 R3：市场版本独立递增，不再 follow 用户本地版本
    assert item_b.version == "1.0.1"

    # 快照里应有两个版本：1.0.0（alice 首发）和 1.0.1（bob 续接）
    vsvc = MCPVersionService(tmp_path / "market")
    listed = vsvc.list_versions("src_a", item_a.item_id)
    ids = sorted(v["version_id"] for v in listed["versions"])
    assert ids == ["1.0.0", "1.0.1"]
    # 最新快照 source_user 是 bob，且 source_user_version 保留 bob 的本地版本 2.0.0
    current = next(v for v in listed["versions"] if v["is_current"])
    assert current["version_id"] == "1.0.1"
    assert current["source_user_id"] == "bob"
    assert current["source_user_version"] == "2.0.0"


@pytest.mark.asyncio
async def test_distribute_mcp_overwrite_skips_same_name_custom_mcp(
    tmp_path,
):
    """显式 overwrite 时，目标租户有同名自建 MCP 应跳过且不改配置。"""
    from market.marketplace.fs import resolve_effective_user_id
    from market.marketplace.schemas import (
        MCPDistributionRequest,
        PublishMCPRequest,
    )

    svc = _make_service(tmp_path)
    item, _ = await svc.publish_mcp(
        "source-1",
        PublishMCPRequest(
            client_key="market-mcp",
            name="shared-mcp",
            description="market",
            creator_id="publisher",
            creator_name="Publisher",
            config={
                "name": "shared-mcp",
                "transport": "stdio",
                "command": "/market-mcp",
            },
            version="1.0.0",
        ),
    )

    user_id = "tenant-1"
    config_path = (
        tmp_path
        / "swe"
        / resolve_effective_user_id(user_id, "source-1")
        / "workspaces"
        / "default"
        / "agent.json"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "clients": {
                        "custom-mcp": {
                            "name": "shared-mcp",
                            "transport": "stdio",
                            "command": "/custom-mcp",
                            "creator_id": "tenant-1",
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    result = await svc.distribute_mcp(
        "source-1",
        item.item_id,
        operator_id="admin",
        operator_name="Admin",
        req=MCPDistributionRequest(
            target_tenant_ids=[user_id],
            overwrite=True,
        ),
    )

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    clients = saved["mcp"]["clients"]
    assert result.results[0].success is True
    assert result.results[0].skipped is True
    assert clients == {
        "custom-mcp": {
            "name": "shared-mcp",
            "transport": "stdio",
            "command": "/custom-mcp",
            "creator_id": "tenant-1",
        },
    }


@pytest.mark.asyncio
async def test_distribute_mcp_overwrite_skips_multiple_same_name_custom_mcps(
    tmp_path,
):
    """存在多个同名自建 MCP 时，overwrite 也应整体跳过并保留配置。"""
    from market.marketplace.fs import resolve_effective_user_id
    from market.marketplace.schemas import (
        MCPDistributionRequest,
        PublishMCPRequest,
    )

    svc = _make_service(tmp_path)
    item, _ = await svc.publish_mcp(
        "source-1",
        PublishMCPRequest(
            client_key="market-mcp",
            name="shared-mcp",
            description="market",
            creator_id="publisher",
            creator_name="Publisher",
            config={
                "name": "shared-mcp",
                "transport": "stdio",
                "command": "/market-mcp",
            },
            version="1.0.0",
        ),
    )

    user_id = "tenant-1"
    config_path = (
        tmp_path
        / "swe"
        / resolve_effective_user_id(user_id, "source-1")
        / "workspaces"
        / "default"
        / "agent.json"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "clients": {
                        "custom-a": {
                            "name": "shared-mcp",
                            "transport": "stdio",
                            "command": "/custom-a",
                        },
                        "custom-b": {
                            "name": "shared-mcp",
                            "transport": "stdio",
                            "command": "/custom-b",
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    result = await svc.distribute_mcp(
        "source-1",
        item.item_id,
        operator_id="admin",
        operator_name="Admin",
        req=MCPDistributionRequest(
            target_tenant_ids=[user_id],
            overwrite=True,
        ),
    )

    clients = json.loads(config_path.read_text(encoding="utf-8"))["mcp"][
        "clients"
    ]
    assert result.results[0].success is True
    assert result.results[0].skipped is True
    assert set(clients) == {"custom-a", "custom-b"}
    assert [cfg["command"] for cfg in clients.values()] == [
        "/custom-a",
        "/custom-b",
    ]


@pytest.mark.asyncio
async def test_distribute_mcp_overwrite_skip_does_not_require_market_config(
    tmp_path,
):
    """同名自建 MCP 跳过时，不读取缺失的市场配置，也不改用户配置。"""
    from market.marketplace.fs import get_mcp_dir, resolve_effective_user_id
    from market.marketplace.schemas import (
        MCPDistributionRequest,
        PublishMCPRequest,
    )

    svc = _make_service(tmp_path)
    item, _ = await svc.publish_mcp(
        "source-1",
        PublishMCPRequest(
            client_key="market-mcp",
            name="shared-mcp",
            description="market",
            creator_id="publisher",
            creator_name="Publisher",
            config={
                "name": "shared-mcp",
                "transport": "stdio",
                "command": "/market-mcp",
            },
            version="1.0.0",
        ),
    )
    shutil.rmtree(get_mcp_dir(tmp_path / "market", "source-1", item.item_id))

    user_id = "tenant-1"
    config_path = (
        tmp_path
        / "swe"
        / resolve_effective_user_id(user_id, "source-1")
        / "workspaces"
        / "default"
        / "agent.json"
    )
    original_config = {
        "mcp": {
            "clients": {
                "custom-mcp": {
                    "name": "shared-mcp",
                    "transport": "stdio",
                    "command": "/custom-mcp",
                },
            },
        },
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(original_config),
        encoding="utf-8",
    )

    result = await svc.distribute_mcp(
        "source-1",
        item.item_id,
        operator_id="admin",
        operator_name="Admin",
        req=MCPDistributionRequest(
            target_tenant_ids=[user_id],
            overwrite=True,
        ),
    )

    assert result.results[0].success is True
    assert result.results[0].skipped is True
    assert json.loads(config_path.read_text(encoding="utf-8")) == (
        original_config
    )


@pytest.mark.asyncio
async def test_publish_mcp_admin_zip_source_user_empty(tmp_path):
    """T9 R6：admin zip 路径（显式 source_user_id="" + v0.0.0）应记录正确."""
    from market.marketplace.schemas import PublishMCPRequest
    from market.marketplace.mcp_version_service import MCPVersionService

    svc = _make_service(tmp_path)

    item, _ = await svc.publish_mcp(
        "src_a",
        PublishMCPRequest(
            client_key="m2",
            name="zipmcp",
            description="d",
            creator_id="admin_id",
            creator_name="Admin",
            config={"name": "zipmcp", "transport": "stdio", "command": "/x"},
            version="1.0.0",
            source_user_id="",
            source_user_name="",
            source_user_version="v0.0.0",
            operator_id="admin_id",
            operator_name="Admin",
        ),
    )

    vsvc = MCPVersionService(tmp_path / "market")
    listed = vsvc.list_versions("src_a", item.item_id)
    snap = listed["versions"][0]
    assert snap["source_user_id"] == ""
    assert snap["source_user_name"] == ""
    assert snap["source_user_version"] == "v0.0.0"
    assert snap["created_by"] == "admin_id"


@pytest.mark.asyncio
async def test_detail_stats_use_skill_identity_and_request_branch(tmp_path):
    from market.marketplace.models import MarketItem

    svc = _make_service(tmp_path)
    svc.market_skill_registry.get_market_skill = AsyncMock(
        return_value=MarketItem(
            item_id="item-1",
            name="search",
            skill_id="new",
            creator_id="u1",
        ),
    )
    svc._get_stats = AsyncMock(return_value=(2, 1))
    svc._get_user_stats = AsyncMock(return_value=[])
    detail = await svc.get_skill_detail("src_a", "item-1", "110")
    assert detail.call_count == 2
    svc._get_stats.assert_awaited_once_with("search", "src_a", "new", "110")
    svc._get_user_stats.assert_awaited_once_with(
        "search",
        "src_a",
        "new",
        "110",
    )


@pytest.mark.asyncio
async def test_detail_user_and_summary_queries_share_month_branch_and_id(
    tmp_path,
):
    svc = _make_service(tmp_path)
    await svc._get_stats("search", "src_a", "new", "110")
    await svc._get_user_stats("search", "src_a", "new", "110")
    summary_sql, summary_params = svc.db.fetch_one.await_args.args
    users_sql, users_params = svc.db.fetch_all.await_args.args
    for sql in (summary_sql, users_sql):
        assert "COUNT(DISTINCT trace_id)" in sql
        assert "start_time >= %s AND start_time < %s" in sql
        assert "bbk_id = %s" in sql
        assert "COALESCE(skill_id, '') = %s" in sql
    assert summary_params == users_params
    assert summary_params[-2:] == ("search", "new")
