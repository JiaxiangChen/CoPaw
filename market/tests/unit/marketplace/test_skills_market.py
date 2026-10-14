# -*- coding: utf-8 -*-
import asyncio
import io
import json
import pytest
import zipfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from market.app.routers import skills_market as skills_router
from market.marketplace.errors import MarketplaceMetadataSyncError


def _make_app(tmp_path):
    from fastapi import FastAPI
    from market.app.routers.skills_market import router
    from market.marketplace.service import MarketplaceService
    from market.database.connection import DatabaseConnection

    @asynccontextmanager
    async def _transaction():
        yield

    mock_db = AsyncMock(spec=DatabaseConnection)
    mock_db.is_connected = True
    mock_db.execute = AsyncMock(return_value=1)
    mock_db.fetch_one = AsyncMock(return_value=None)
    mock_db.fetch_all = AsyncMock(return_value=[])
    mock_db.transaction = _transaction

    svc = MarketplaceService(
        db=mock_db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )
    app = FastAPI()
    app.state.marketplace = svc
    app.include_router(router, prefix="/api")
    return app


@pytest.mark.asyncio
async def test_process_workspace_skills_writes_workspace_manifest_path(
    tmp_path,
    monkeypatch,
):
    from market.marketplace import skill_sync
    from market.marketplace.fs import get_workspace_skill_manifest_path

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "skills" / "demo").mkdir(parents=True)

    async def _record_skill(
        skill_dir,
        user_id,
        source_id,
        skills_dict,
        registry,
        force,
        dry_run,
        result,
    ):
        del (
            skill_dir,
            user_id,
            source_id,
            registry,
            force,
            dry_run,
            result,
        )
        skills_dict["demo"] = {"enabled": True}

    monkeypatch.setattr(
        skill_sync,
        "_process_single_skill",
        _record_skill,
    )

    await skill_sync._process_workspace_skills(
        workspace_dir,
        "user1",
        "source_a",
        object(),
        False,  # force
        False,  # dry_run
        True,  # write_manifest_back
        {
            "tenant_id": "user1",
            "total_workspaces": 1,
            "total_skills": 0,
            "synced": 0,
            "errors": [],
            "details": [],
        },
    )

    manifest_path = get_workspace_skill_manifest_path(workspace_dir)
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "schema_version": "workspace-skill-manifest.v1",
        "layout_version": 2,
        "version": 0,
        "skills": {"demo": {"enabled": True}},
    }
    assert manifest_path == workspace_dir / "skill.json"
    assert not (workspace_dir / ".skill_state" / "manifest.json").exists()


def test_publish_skill_returns_201(tmp_path):
    app = _make_app(tmp_path)
    client = TestClient(app)
    payload = {
        "name": "skill_x",
        "description": "test",
        "creator_id": "u1",
        "creator_name": "User",
        "skill_json": {"name": "skill_x"},
        "skill_md": "# Skill X",
    }
    resp = client.post(
        "/api/market/skills",
        json=payload,
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "skill_x"
    assert data["version"] == "1.0.0"


def test_publish_skill_non_manager_returns_403(tmp_path):
    app = _make_app(tmp_path)
    client = TestClient(app)
    payload = {
        "name": "skill_x",
        "description": "",
        "creator_id": "u1",
        "creator_name": "",
        "skill_json": {},
        "skill_md": "",
    }
    resp = client.post(
        "/api/market/skills",
        json=payload,
        headers={"X-Source-Id": "src_a"},
    )
    assert resp.status_code == 403


def test_unpublish_skill_returns_204(tmp_path):
    from market.marketplace.schemas import PublishSkillRequest

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    req = PublishSkillRequest(
        name="skill_y",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
    )
    item, _ = asyncio.run(svc.publish_skill("src_a", req))
    client = TestClient(app)
    resp = client.delete(
        f"/api/market/skills/{item.item_id}",
        headers={
            "X-Source-Id": "src_a",
            "X-Manager": "true",
            "X-User-Id": "u1",
            "X-User-Name": "User",
        },
    )
    assert resp.status_code == 204


def test_unpublish_skill_not_found_returns_404(tmp_path):
    app = _make_app(tmp_path)
    client = TestClient(app)
    resp = client.delete(
        "/api/market/skills/nonexistent-id",
        headers={
            "X-Source-Id": "src_a",
            "X-Manager": "true",
            "X-User-Id": "u1",
            "X-User-Name": "User",
        },
    )
    assert resp.status_code == 404


def test_distribute_skill_returns_200(tmp_path, monkeypatch):
    from market.marketplace.schemas import PublishSkillRequest

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    req = PublishSkillRequest(
        name="skill_z",
        description="",
        creator_id="u1",
        creator_name="",
        skill_json={},
        skill_md="",
    )
    item, _ = asyncio.run(svc.publish_skill("src_a", req))
    svc.db.fetch_all = AsyncMock(
        return_value=[
            {"tenant_id": "user1", "tenant_name": "User One", "bbk_id": "200"},
        ],
    )

    monkeypatch.setattr(
        skills_router.asyncio,
        "create_task",
        lambda coro: coro.close() or object(),
    )
    client = TestClient(app)
    resp = client.post(
        f"/api/market/skills/{item.item_id}/distribute",
        json={"target_type": "all", "target_values": []},
        headers={
            "X-Source-Id": "src_a",
            "X-Manager": "true",
            "X-User-Id": "u1",
            "X-User-Name": "User",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["task_id"]


def test_distribute_skill_writes_workspace_manifest(tmp_path):
    from market.marketplace.fs import get_user_skills_dir
    from market.marketplace.schemas import (
        DistributeRequest,
        PublishSkillRequest,
    )

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    item, _ = asyncio.run(
        svc.publish_skill(
            "src_a",
            PublishSkillRequest(
                name="skill_z",
                description="",
                creator_id="u1",
                creator_name="",
                skill_json={},
                skill_md="",
            ),
        ),
    )
    svc.db.fetch_all = AsyncMock(
        return_value=[
            {"tenant_id": "user1", "tenant_name": "User One", "bbk_id": "200"},
        ],
    )
    result = asyncio.run(
        svc.distribute_skill(
            "src_a",
            item.item_id,
            operator_id="u1",
            operator_name="User",
            req=DistributeRequest(target_type="all"),
        ),
    )
    assert result.distributed_count == 1
    assert result.conflict_count == 0

    workspace_dir = get_user_skills_dir(
        tmp_path / "swe",
        "user1",
        "default",
        "src_a",
    ).parent
    manifest_path = workspace_dir / "skill.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["skills"]["skill_z"]["source"] == (
        f"marketplace:{item.item_id}"
    )
    assert manifest["skills"]["skill_z"]["metadata"]["distributed_by"] == (
        "u1"
    )
    assert not (workspace_dir / ".skill_state" / "manifest.json").exists()


def test_distribute_skill_writes_market_category_to_workspace_manifest(
    tmp_path,
):
    from market.marketplace.fs import get_user_skills_dir
    from market.marketplace.schemas import (
        DistributeRequest,
        PublishSkillRequest,
    )

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    item, _ = asyncio.run(
        svc.publish_skill(
            "src_a",
            PublishSkillRequest(
                name="categorized_skill",
                description="",
                creator_id="u1",
                creator_name="",
                category_id=7,
                bbk_ids=["200"],
                skill_json={},
                skill_md="",
            ),
        ),
    )
    svc.db.fetch_all = AsyncMock(
        return_value=[
            {"tenant_id": "user1", "tenant_name": "User One", "bbk_id": "200"},
        ],
    )

    result = asyncio.run(
        svc.distribute_skill(
            "src_a",
            item.item_id,
            operator_id="u1",
            operator_name="User",
            req=DistributeRequest(target_type="all"),
        ),
    )

    assert result.distributed_count == 1
    workspace_dir = get_user_skills_dir(
        tmp_path / "swe",
        "user1",
        "default",
        "src_a",
    ).parent
    manifest = json.loads((workspace_dir / "skill.json").read_text())
    assert (
        manifest["skills"]["categorized_skill"]["metadata"]["category_id"] == 7
    )


def test_update_skill_metadata_updates_category_and_branch_without_recall(
    tmp_path,
    monkeypatch,
):
    from market.marketplace.fs import load_index, save_index
    from market.marketplace.models import MarketItem

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    svc.market_skill_registry.upsert_market_skill = AsyncMock(
        return_value=True,
    )
    save_index(
        svc.marketplace_root,
        "src_a",
        [
            MarketItem(
                item_id="item-1",
                item_type="skill",
                name="skill",
                skill_id="skill-1",
                chinese_name="旧名",
                category_id=1,
                bbk_ids=["100"],
                creator_id="u1",
            ),
        ],
    )
    monkeypatch.setattr(
        svc,
        "get_distributions",
        AsyncMock(
            return_value=[
                type(
                    "Distribution",
                    (),
                    {"target_user_id": "user-1"},
                )(),
            ],
        ),
    )
    monkeypatch.setattr(
        svc,
        "_sync_skill_category_to_user",
        AsyncMock(return_value=True),
    )

    result = asyncio.run(
        svc.update_skill_metadata(
            source_id="src_a",
            item_id="item-1",
            skill_id="skill-1",
            skill_name="skill",
            chinese_name="新名",
            category_id=2,
            bbk_ids=["200"],
        ),
    )

    assert result["market_updated"] is True
    assert result["synced_category_users"] == 1
    item = load_index(svc.marketplace_root, "src_a")[0]
    assert item.category_id == 2
    assert item.bbk_ids == ["200"]
    svc.market_skill_registry.upsert_market_skill.assert_awaited_once()
    upsert_kwargs = (
        svc.market_skill_registry.upsert_market_skill.call_args.kwargs
    )
    assert upsert_kwargs["cn_name"] == "新名"
    assert upsert_kwargs["category_id"] == 2
    assert upsert_kwargs["bbk_ids"] == ["200"]
    svc.get_distributions.assert_awaited_once()


def test_update_skill_metadata_does_not_write_index_when_tdsql_sync_fails(
    tmp_path,
):
    from market.marketplace.fs import load_index, save_index
    from market.marketplace.models import MarketItem

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    svc.market_skill_registry.upsert_market_skill = AsyncMock(
        return_value=False,
    )
    save_index(
        svc.marketplace_root,
        "src_a",
        [
            MarketItem(
                item_id="item-1",
                item_type="skill",
                name="skill",
                skill_id="skill-1",
                chinese_name="旧名",
                category_id=1,
                bbk_ids=["100"],
                creator_id="u1",
            ),
        ],
    )

    with pytest.raises(MarketplaceMetadataSyncError):
        asyncio.run(
            svc.update_skill_metadata(
                source_id="src_a",
                item_id="item-1",
                skill_id="skill-1",
                skill_name="skill",
                chinese_name="新名",
                category_id=2,
                bbk_ids=["200"],
            ),
        )

    item = load_index(svc.marketplace_root, "src_a")[0]
    assert item.chinese_name == "旧名"
    assert item.category_id == 1
    assert item.bbk_ids == ["100"]


def test_publish_skill_missing_source_id_returns_400(tmp_path):
    app = _make_app(tmp_path)
    client = TestClient(app)
    payload = {
        "name": "skill_x",
        "description": "",
        "creator_id": "u1",
        "creator_name": "",
        "skill_json": {},
        "skill_md": "",
    }
    resp = client.post(
        "/api/market/skills",
        json=payload,
        headers={"X-Manager": "true"},
    )
    assert resp.status_code == 400


def test_publish_skill_upload_reactivates_inactive_skill(tmp_path):
    """验证下架后重新上传同名技能可以成功上架（复用条目，版本号递增）."""
    import io
    import zipfile
    from market.marketplace.fs import load_index

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    client = TestClient(app)

    # 第一步：通过 JSON API 创建技能
    payload = {
        "name": "test_skill",
        "description": "initial",
        "creator_id": "u1",
        "creator_name": "User",
        "skill_json": {"name": "test_skill"},
        "skill_md": "# Test Skill",
    }
    resp = client.post(
        "/api/market/skills",
        json=payload,
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )
    assert resp.status_code == 201
    item_id = resp.json()["item_id"]
    assert resp.json()["version"] == "1.0.0"

    # 第二步：下架技能
    resp = client.delete(
        f"/api/market/skills/{item_id}",
        headers={
            "X-Source-Id": "src_a",
            "X-Manager": "true",
            "X-User-Id": "u1",
            "X-User-Name": "User",
        },
    )
    assert resp.status_code == 204

    # 验证状态已变为 inactive
    items = load_index(svc.marketplace_root, "src_a")
    inactive_item = next(i for i in items if i.item_id == item_id)
    assert inactive_item.status == "inactive"

    # 第三步：创建同名技能的 zip 文件并上传
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr(
            "test_skill/skill.json",
            json.dumps({"name": "test_skill", "description": "updated"}),
        )
        zf.writestr("test_skill/SKILL.md", "# Updated Skill")

    zip_buffer.seek(0)
    resp = client.post(
        "/api/market/skills/publish-upload?overwrite=true",
        files={"file": ("skill.zip", zip_buffer, "application/zip")},
        headers={
            "X-Source-Id": "src_a",
            "X-Manager": "true",
            "X-User-Id": "u1",
            "X-User-Name": "User",
        },
    )
    assert resp.status_code == 201
    data = resp.json()

    # 验证：成功上传，没有冲突，版本号递增
    assert "test_skill" in data["imported"]
    assert data["count"] == 1
    assert data.get("conflicts") is None or len(data.get("conflicts", [])) == 0

    # 验证条目被复用，状态重新激活，版本号递增
    items = load_index(svc.marketplace_root, "src_a")
    reactivated_item = next(i for i in items if i.item_id == item_id)
    assert reactivated_item.status == "active"
    assert reactivated_item.version == "1.0.1"  # patch 版本递增


def test_publish_upload_rejects_nested_zip_path_traversal(tmp_path):
    import io
    import zipfile

    app = _make_app(tmp_path)
    client = TestClient(app)

    nested_buffer = io.BytesIO()
    with zipfile.ZipFile(nested_buffer, "w") as nested:
        nested.writestr("../escape.txt", "owned")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr(
            "bad_skill/SKILL.md",
            "---\nname: bad_skill\n---\n# Bad Skill\n",
        )
        zf.writestr("bad_skill/payload.zip", nested_buffer.getvalue())

    zip_buffer.seek(0)
    resp = client.post(
        "/api/market/skills/publish-upload",
        files={"file": ("bad_skill.zip", zip_buffer, "application/zip")},
        headers={
            "X-Source-Id": "src_a",
            "X-Manager": "true",
            "X-User-Id": "u1",
            "X-User-Name": "User",
        },
    )

    assert resp.status_code == 400
    assert "Security scan" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_sync_skill_to_market_db_fails_when_upsert_returns_false():
    svc = type("Svc", (), {})()
    svc.db = type("Db", (), {"is_connected": True})()
    svc.market_skill_registry = type("Registry", (), {})()
    svc.market_skill_registry.upsert_market_skill = AsyncMock(
        return_value=False,
    )

    with pytest.raises(MarketplaceMetadataSyncError):
        await skills_router._sync_skill_to_market_db(
            svc=svc,
            source_id="src_a",
            item_id="item-1",
            skill_id="skill-1",
            imported_name="demo_skill",
            resolved_cn_name="",
            category_id=None,
            bbk_ids=None,
            include_in_statistics=False,
            x_user_id="u1",
            user_name="User",
        )


@pytest.mark.asyncio
async def test_sync_skill_to_market_db_persists_category_and_bbk_ids():
    svc = type("Svc", (), {})()
    svc.db = type("Db", (), {"is_connected": True})()
    svc.market_skill_registry = type("Registry", (), {})()
    svc.market_skill_registry.upsert_market_skill = AsyncMock(
        return_value=True,
    )

    await skills_router._sync_skill_to_market_db(
        svc=svc,
        source_id="src_a",
        item_id="item-1",
        skill_id="skill-1",
        imported_name="demo_skill",
        resolved_cn_name="演示技能",
        category_id=3,
        bbk_ids=["110"],
        include_in_statistics=False,
        x_user_id="u1",
        user_name="User",
    )

    svc.market_skill_registry.upsert_market_skill.assert_awaited_once()
    call = svc.market_skill_registry.upsert_market_skill.await_args.kwargs
    assert call["category_id"] == 3
    assert call["bbk_ids"] == ["110"]


def test_publish_upload_returns_503_when_database_is_unavailable(tmp_path):
    app = _make_app(tmp_path)
    app.state.marketplace.db.is_connected = False
    client = TestClient(app)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("demo_skill/SKILL.md", "# Demo Skill")
    zip_buffer.seek(0)

    resp = client.post(
        "/api/market/skills/publish-upload",
        files={"file": ("demo_skill.zip", zip_buffer, "application/zip")},
        headers={
            "X-Source-Id": "src_a",
            "X-Manager": "true",
            "X-User-Id": "u1",
            "X-User-Name": "User",
        },
    )

    assert resp.status_code == 503


def test_init_missing_skill_fields_syncs_market_metadata_to_tdsql(tmp_path):
    from market.marketplace.fs import load_index, save_index
    from market.marketplace.models import MarketItem

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    svc.market_skill_registry.upsert_market_skill = AsyncMock(
        return_value=True,
    )
    save_index(
        svc.marketplace_root,
        "src_a",
        [
            MarketItem(
                item_id="item-1",
                item_type="skill",
                name="skill",
                skill_id="skill-1",
                chinese_name="技能",
                creator_id="u1",
                creator_name="User One",
            ),
        ],
    )

    resp = TestClient(app).post(
        "/api/market/skills/init-missing-fields",
        json={
            "source_id": "src_a",
            "category_id": 7,
            "bbk_ids": ["200"],
            "dry_run": False,
        },
    )

    assert resp.status_code == 200
    svc.market_skill_registry.upsert_market_skill.assert_awaited_once()
    upsert_kwargs = (
        svc.market_skill_registry.upsert_market_skill.call_args.kwargs
    )
    assert upsert_kwargs["creator_id"] == "u1"
    assert upsert_kwargs["creator_name"] == "User One"
    assert upsert_kwargs["category_id"] == 7
    assert upsert_kwargs["bbk_ids"] == ["200"]
    item = load_index(svc.marketplace_root, "src_a")[0]
    assert item.category_id == 7
    assert item.bbk_ids == ["200"]


def test_init_missing_skill_fields_does_not_write_index_on_partial_sync_failure(
    tmp_path,
):
    from market.marketplace.fs import load_index, save_index
    from market.marketplace.models import MarketItem

    transaction_entries = 0

    @asynccontextmanager
    async def _transaction():
        nonlocal transaction_entries
        transaction_entries += 1
        yield

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    svc.db.transaction = _transaction
    svc.market_skill_registry.upsert_market_skill = AsyncMock(
        side_effect=[True, False],
    )
    save_index(
        svc.marketplace_root,
        "src_a",
        [
            MarketItem(
                item_id="item-1",
                item_type="skill",
                name="skill_one",
                skill_id="skill-1",
                chinese_name="技能一",
                creator_id="u1",
                creator_name="User One",
            ),
            MarketItem(
                item_id="item-2",
                item_type="skill",
                name="skill_two",
                skill_id="skill-2",
                chinese_name="技能二",
                creator_id="u2",
                creator_name="User Two",
            ),
        ],
    )

    resp = TestClient(app).post(
        "/api/market/skills/init-missing-fields",
        json={
            "source_id": "src_a",
            "category_id": 7,
            "bbk_ids": ["200"],
            "dry_run": False,
        },
    )

    assert resp.status_code == 503
    assert transaction_entries == 1
    assert svc.market_skill_registry.upsert_market_skill.await_count == 2
    items = load_index(svc.marketplace_root, "src_a")
    assert [item.category_id for item in items] == [None, None]
    assert [item.bbk_ids for item in items] == [[], []]


def test_init_missing_skill_fields_noops_without_database_when_nothing_changed(
    tmp_path,
):
    from market.marketplace.fs import save_index
    from market.marketplace.models import MarketItem

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    svc.db.is_connected = False

    transaction_entries = 0

    @asynccontextmanager
    async def _transaction():
        nonlocal transaction_entries
        transaction_entries += 1
        yield

    svc.db.transaction = _transaction
    svc.market_skill_registry.upsert_market_skill = AsyncMock()
    save_index(
        svc.marketplace_root,
        "src_a",
        [
            MarketItem(
                item_id="item-1",
                item_type="skill",
                name="skill",
                skill_id="skill-1",
                chinese_name="技能",
                category_id=7,
                bbk_ids=["200"],
                creator_id="u1",
            ),
        ],
    )

    resp = TestClient(app).post(
        "/api/market/skills/init-missing-fields",
        json={
            "source_id": "src_a",
            "category_id": 7,
            "bbk_ids": ["200"],
            "dry_run": False,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["total_skills"] == 0
    assert transaction_entries == 0
    svc.market_skill_registry.upsert_market_skill.assert_not_awaited()


def test_switch_version_updates_market_item_creator(tmp_path):
    """T4 R8：switch_version 同步更新 MarketItem.creator_id/creator_name 到目标快照来源."""
    import json as _json
    from market.marketplace.fs import save_index, load_index
    from market.marketplace.models import MarketItem
    from market.app.routers.skill_versions import _update_skill_index

    marketplace_root = tmp_path / "market"
    source_id = "src1"
    item_id = "item1"
    skill_dir = marketplace_root / source_id / "skills" / item_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: t\ndescription: d\nversion: "2.0.0"\n---\n',
        encoding="utf-8",
    )

    # 起始：creator=alice，市场版本 2.0.0
    save_index(
        marketplace_root,
        source_id,
        [
            MarketItem(
                item_id=item_id,
                item_type="skill",
                name="t",
                description="d",
                version="2.0.0",
                creator_id="alice_id",
                creator_name="alice",
                status="active",
            ),
        ],
    )

    # 准备 versions.json：v1.0.0 source_user=bob
    versions_path = (
        marketplace_root
        / source_id
        / "skill_versions"
        / item_id
        / "versions.json"
    )
    versions_path.parent.mkdir(parents=True, exist_ok=True)
    versions_path.write_text(
        _json.dumps(
            {
                "skill_name": "t",
                "versions": [
                    {
                        "version_id": "1.0.0",
                        "created_at": "2025-01-01T00:00:00+00:00",
                        "created_by": "admin",
                        "created_by_name": "admin",
                        "source_user_id": "bob_id",
                        "source_user_name": "bob",
                        "source_user_version": "1.0.0",
                        "signature": "x",
                        "is_current": True,
                        "is_initial": True,
                        "description": "",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class _FakeMarketplace:
        pass

    fake = _FakeMarketplace()
    fake.marketplace_root = marketplace_root

    _update_skill_index(fake, source_id, item_id, skill_dir, "1.0.0")

    items = load_index(marketplace_root, source_id)
    item = items[0]
    assert item.version == "1.0.0"
    # R8: creator_id/name 跟随目标快照的 source_user_*
    assert item.creator_id == "bob_id"
    assert item.creator_name == "bob"


def test_switch_version_falls_back_to_created_by_when_no_source_user(tmp_path):
    """T4 R8 边界：source_user_* 为空时回退到 created_by."""
    import json as _json
    from market.marketplace.fs import save_index, load_index
    from market.marketplace.models import MarketItem
    from market.app.routers.skill_versions import _update_skill_index

    marketplace_root = tmp_path / "market"
    source_id = "src1"
    item_id = "item2"
    skill_dir = marketplace_root / source_id / "skills" / item_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: t\ndescription: d\nversion: "1.0.0"\n---\n',
        encoding="utf-8",
    )
    save_index(
        marketplace_root,
        source_id,
        [
            MarketItem(
                item_id=item_id,
                item_type="skill",
                name="t",
                description="d",
                version="2.0.0",
                creator_id="alice_id",
                creator_name="alice",
                status="active",
            ),
        ],
    )
    versions_path = (
        marketplace_root
        / source_id
        / "skill_versions"
        / item_id
        / "versions.json"
    )
    versions_path.parent.mkdir(parents=True, exist_ok=True)
    versions_path.write_text(
        _json.dumps(
            {
                "skill_name": "t",
                "versions": [
                    {
                        "version_id": "1.0.0",
                        "created_at": "2025-01-01T00:00:00+00:00",
                        "created_by": "admin_id",
                        "created_by_name": "Admin",
                        "source_user_id": "",
                        "source_user_name": "",
                        "source_user_version": "",
                        "signature": "x",
                        "is_current": True,
                        "is_initial": True,
                        "description": "",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class _FakeMarketplace:
        pass

    fake = _FakeMarketplace()
    fake.marketplace_root = marketplace_root
    _update_skill_index(fake, source_id, item_id, skill_dir, "1.0.0")

    items = load_index(marketplace_root, source_id)
    item = items[0]
    assert item.creator_id == "admin_id"
    assert item.creator_name == "Admin"


def test_update_statistics_config_returns_200(tmp_path):
    """测试更新统计配置接口."""
    from market.marketplace.schemas import PublishSkillRequest

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    req = PublishSkillRequest(
        name="skill_stats",
        description="test",
        creator_id="u1",
        creator_name="User",
        skill_json={},
        skill_md="",
    )
    item, _ = asyncio.run(svc.publish_skill("src_a", req))

    client = TestClient(app)
    resp = client.patch(
        f"/api/market/skills/{item.item_id}/statistics",
        json={"include_in_statistics": False},
        headers={
            "X-Source-Id": "src_a",
            "X-Manager": "true",
            "X-User-Id": "u1",
            "X-User-Name": "User",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True


def test_list_skills_reads_statistics_eligible_marketplace_skills(tmp_path):
    """测试定时任务技能下拉列表读取统计白名单市场技能."""
    app = _make_app(tmp_path)
    app.state.marketplace.db.fetch_all.return_value = [
        {
            "skill_id": "skill_a",
            "skill_name": "skill_a_name",
            "cn_name": "技能A",
        },
    ]
    client = TestClient(app)

    resp = client.post(
        "/api/market/skills/list",
        json={"source_id": "src_a"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "source_id": "src_a",
        "count": 1,
        "skills": [
            {
                "skill_id": "skill_a",
                "skill_name": "skill_a_name",
                "cn_name": "技能A",
            },
        ],
    }
    sql = app.state.marketplace.db.fetch_all.call_args.args[0]
    assert "FROM swe_marketplace_skills" in sql
    assert "include_in_statistics = 1" in sql
    assert "is_unpublished = 0" in sql
    assert "is_deleted = 0" in sql


def test_update_statistics_config_non_manager_returns_403(tmp_path):
    """测试非管理员无法更新统计配置."""
    from market.marketplace.schemas import PublishSkillRequest

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    req = PublishSkillRequest(
        name="skill_stats2",
        description="test",
        creator_id="u1",
        creator_name="User",
        skill_json={},
        skill_md="",
    )
    item, _ = asyncio.run(svc.publish_skill("src_a", req))

    client = TestClient(app)
    resp = client.patch(
        f"/api/market/skills/{item.item_id}/statistics",
        json={"include_in_statistics": False},
        headers={
            "X-Source-Id": "src_a",
            "X-User-Id": "u1",
        },
    )
    assert resp.status_code == 403


# 已由 /api/market/admin/metadata/migrate 替代。
# def test_init_statistics_returns_200(tmp_path):
#     """测试初始化历史数据接口（dry_run 模式）."""
#     app = _make_app(tmp_path)
#     client = TestClient(app)
#     # dry_run 模式会检查数据库连接
#     resp = client.post(
#         "/api/market/admin/skills/init-statistics",
#         json={
#             "source_ids": ["src_a"],
#             "default_include": True,
#             "dry_run": True,
#         },
#     )
#     # 由于 mock 数据库连接，dry_run 模式应该成功
#     assert resp.status_code == 200
#     data = resp.json()
#     assert "processed" in data
#     assert "errors" in data
