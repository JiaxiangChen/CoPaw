# -*- coding: utf-8 -*-
"""Expert Community router tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market.app.routers import api_router


class FakeMarketplace:
    """Minimal marketplace double for expert routes."""

    def __init__(self) -> None:
        self.list_expert_items = AsyncMock()
        self.get_expert_detail = AsyncMock()
        self.publish_expert = AsyncMock()
        self.publish_expert_from_profile = AsyncMock()
        self.restore_expert_version = AsyncMock()
        self.unpublish_expert = AsyncMock()
        self.install_expert = AsyncMock()
        self.distribute_expert = AsyncMock()
        self.get_expert_distributions = AsyncMock()
        self.recall_expert = AsyncMock()
        self._get_expert_version_service = MagicMock()
        self.marketplace_root = Path("/tmp/market")


@pytest.fixture
def test_app() -> FastAPI:
    """Create a FastAPI app with the market router."""
    app = FastAPI()
    app.state.marketplace = FakeMarketplace()
    app.include_router(api_router)
    return app


@pytest.fixture
def client(test_app: FastAPI) -> TestClient:
    """Normal user client."""
    return TestClient(test_app)


@pytest.fixture
def manager_client(test_app: FastAPI) -> TestClient:
    """Manager client."""
    return TestClient(test_app, headers={"X-Manager": "true"})


def _expert_item() -> dict[str, object]:
    return {
        "item_id": "expert-1",
        "name": "Community Expert",
        "description": "Expert description",
        "version": "1.0.0",
        "creator_id": "author-a",
        "creator_name": "Author A",
        "category_id": 7,
        "bbk_ids": ["100"],
        "status": "active",
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:00:00Z",
    }


def test_list_experts(client: TestClient, test_app: FastAPI) -> None:
    """Browse endpoint should return active expert items."""
    test_app.state.marketplace.list_expert_items.return_value = [
        _expert_item(),
    ]

    response = client.get("/market/experts", headers={"X-Source-Id": "SRC"})

    assert response.status_code == 200
    assert response.json()[0]["name"] == "Community Expert"


def test_get_expert_detail(client: TestClient, test_app: FastAPI) -> None:
    """Detail endpoint should return expert metadata and version history."""
    test_app.state.marketplace.get_expert_detail.return_value = (
        SimpleNamespace(
            **_expert_item(),
            versions=[
                {
                    "version_id": "1.0.0",
                    "created_at": "2026-08-20T10:00:00Z",
                    "created_by": "manager",
                    "created_by_name": "Manager",
                    "description": "Initial",
                    "signature": "abc",
                    "is_current": True,
                    "is_initial": True,
                },
            ],
            definition={"name": "Community Expert"},
        )
    )

    response = client.get(
        "/market/experts/expert-1",
        headers={"X-Source-Id": "SRC"},
    )

    assert response.status_code == 200
    assert response.json()["definition"]["name"] == "Community Expert"


def test_list_expert_versions(
    client: TestClient,
    test_app: FastAPI,
    tmp_path: Path,
) -> None:
    """Version history endpoint should return the current version list."""
    test_app.state.marketplace.marketplace_root = tmp_path
    index_dir = tmp_path / "SRC"
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "index.json").write_text(
        """
        {
          "items": [
            {
              "item_id": "expert-1",
              "item_type": "expert",
              "name": "Community Expert",
              "description": "Expert description",
              "version": "1.0.0",
              "creator_id": "author-a",
              "creator_name": "Author A",
              "category_id": 7,
              "bbk_ids": ["100"],
              "status": "active"
            }
          ]
        }
        """.strip(),
        encoding="utf-8",
    )
    test_app.state.marketplace._get_expert_version_service.return_value = (
        SimpleNamespace(
            list_versions=MagicMock(
                return_value={
                    "expert_name": "Community Expert",
                    "versions": [
                        {
                            "version_id": "1.0.0",
                            "created_at": "2026-08-20T10:00:00Z",
                            "created_by": "manager",
                            "created_by_name": "Manager",
                            "description": "Initial",
                            "signature": "abc",
                            "is_current": True,
                            "is_initial": True,
                        },
                    ],
                },
            ),
        )
    )

    response = client.get(
        "/market/experts/expert-1/versions",
        headers={"X-Source-Id": "SRC"},
    )

    assert response.status_code == 200
    assert response.json()["versions"][0]["version_id"] == "1.0.0"


def test_inactive_expert_hides_version_history(
    client: TestClient,
    test_app: FastAPI,
    tmp_path: Path,
) -> None:
    """Unpublished experts are not browseable through their version routes."""
    test_app.state.marketplace.marketplace_root = tmp_path
    index_dir = tmp_path / "SRC"
    index_dir.mkdir(parents=True)
    (index_dir / "index.json").write_text(
        '{"items":[{"item_id":"expert-1","item_type":"expert",'
        '"name":"Community Expert","description":"",'
        '"version":"1.0.0","creator_id":"author",'
        '"status":"inactive"}]}',
        encoding="utf-8",
    )

    response = client.get(
        "/market/experts/expert-1/versions",
        headers={"X-Source-Id": "SRC"},
    )

    assert response.status_code == 404


def test_publish_expert_manager_only(
    manager_client: TestClient,
    test_app: FastAPI,
) -> None:
    """Publish endpoint requires manager header."""
    test_app.state.marketplace.publish_expert_from_profile.return_value = (
        SimpleNamespace(**_expert_item()),
        False,
    )

    response = manager_client.post(
        "/market/experts",
        headers={"X-Source-Id": "SRC", "X-User-Id": "alice"},
        json={
            "definition_id": "expert-local-1",
            "agent_id": "research",
            "category_id": 7,
            "bbk_ids": ["100"],
        },
    )

    assert response.status_code == 201
    assert response.json()["version"] == "1.0.0"
    test_app.state.marketplace.publish_expert_from_profile.assert_awaited_once_with(
        "SRC",
        "alice",
        "research",
        "expert-local-1",
        category_id=7,
        bbk_ids=["100"],
        creator_name="",
        overwrite=False,
    )


def test_publish_expert_invalid_source_returns_bad_request(
    manager_client: TestClient,
    test_app: FastAPI,
) -> None:
    test_app.state.marketplace.publish_expert_from_profile.side_effect = (
        ValueError("expert definition not found")
    )

    response = manager_client.post(
        "/market/experts",
        headers={"X-Source-Id": "SRC", "X-User-Id": "alice"},
        json={"definition_id": "missing", "agent_id": "research"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "expert definition not found"


def test_restore_and_unpublish_expert(
    manager_client: TestClient,
    test_app: FastAPI,
) -> None:
    """Restore and unpublish endpoints should be manager-only."""
    test_app.state.marketplace.restore_expert_version.return_value = (
        SimpleNamespace(
            **_expert_item(),
        )
    )
    test_app.state.marketplace.get_expert_detail.return_value = (
        SimpleNamespace(
            **_expert_item(),
            versions=[
                {
                    "version_id": "1.0.0",
                    "created_at": "2026-08-20T10:00:00Z",
                    "created_by": "manager",
                    "created_by_name": "Manager",
                    "description": "Initial",
                    "signature": "abc",
                    "is_current": True,
                    "is_initial": True,
                },
            ],
            definition={"name": "Community Expert"},
        )
    )
    test_app.state.marketplace.unpublish_expert.return_value = True

    restore_response = manager_client.post(
        "/market/experts/expert-1/versions/1.0.0/restore",
        headers={"X-Source-Id": "SRC"},
    )
    unpublish_response = manager_client.delete(
        "/market/experts/expert-1",
        headers={"X-Source-Id": "SRC"},
    )

    assert restore_response.status_code == 200
    assert unpublish_response.status_code == 200


def test_restore_missing_version_returns_not_found(
    manager_client: TestClient,
    test_app: FastAPI,
) -> None:
    test_app.state.marketplace.restore_expert_version.side_effect = ValueError(
        "Version 9.9.9 not found",
    )

    response = manager_client.post(
        "/market/experts/expert-1/versions/9.9.9/restore",
        headers={"X-Source-Id": "SRC"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Version 9.9.9 not found"


def test_user_can_install_expert_into_selected_agent(
    client: TestClient,
    test_app: FastAPI,
) -> None:
    test_app.state.marketplace.install_expert.return_value = {
        "user_id": "alice",
        "success": True,
        "definition_id": "definition-1",
    }

    response = client.post(
        "/market/experts/expert-1/install",
        headers={"X-Source-Id": "SRC", "X-User-Id": "alice"},
        json={"agent_id": "research"},
    )

    assert response.status_code == 200
    test_app.state.marketplace.install_expert.assert_awaited_once_with(
        "SRC",
        "expert-1",
        "alice",
        "research",
        "alice",
    )


def test_distribution_and_recall_require_manager(
    client: TestClient,
    manager_client: TestClient,
    test_app: FastAPI,
) -> None:
    test_app.state.marketplace.distribute_expert.return_value = {
        "item_id": "expert-1",
        "distributed_count": 1,
        "conflict_count": 0,
        "results": [{"user_id": "alice", "success": True}],
    }
    test_app.state.marketplace.recall_expert.return_value = {
        "item_id": "expert-1",
        "recalled_count": 1,
        "failed_count": 0,
        "results": [{"user_id": "alice", "success": True}],
    }

    forbidden = client.post(
        "/market/experts/expert-1/distribute",
        headers={"X-Source-Id": "SRC"},
        json={"target_type": "user_id", "target_values": ["alice"]},
    )
    distributed = manager_client.post(
        "/market/experts/expert-1/distribute",
        headers={"X-Source-Id": "SRC", "X-User-Id": "manager"},
        json={"target_type": "user_id", "target_values": ["alice"]},
    )
    recalled = manager_client.post(
        "/market/experts/expert-1/recall",
        headers={"X-Source-Id": "SRC", "X-User-Id": "manager"},
        json={"target_user_ids": ["alice"]},
    )

    assert forbidden.status_code == 403
    assert distributed.status_code == 200
    assert recalled.status_code == 200


def test_expert_distributions_require_manager_and_return_holders(
    client: TestClient,
    manager_client: TestClient,
    test_app: FastAPI,
) -> None:
    test_app.state.marketplace.get_expert_distributions.return_value = [
        {
            "target_user_id": "alice",
            "target_user_name": "Alice",
            "target_bbk_id": "100",
            "distributed_at": None,
        },
    ]

    forbidden = client.get(
        "/market/experts/expert-1/distributions",
        headers={"X-Source-Id": "SRC"},
    )
    response = manager_client.get(
        "/market/experts/expert-1/distributions",
        headers={"X-Source-Id": "SRC"},
    )

    assert forbidden.status_code == 403
    assert response.status_code == 200
    assert response.json() == [
        {
            "target_user_id": "alice",
            "target_user_name": "Alice",
            "target_bbk_id": "100",
            "distributed_at": None,
        },
    ]
    test_app.state.marketplace.get_expert_distributions.assert_awaited_once_with(
        "SRC",
        "expert-1",
    )
