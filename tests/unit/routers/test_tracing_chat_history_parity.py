# -*- coding: utf-8 -*-
"""Analytics reads the same durable history and archives as Chat."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agentscope.message import Msg
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.runner import api
from swe.app.routers import tracing
from swe.app.runner.models import ChatSpec
from swe.app.runner.session import SafeJSONSession


@pytest.fixture
async def history_client(tmp_path, monkeypatch):
    chat = ChatSpec(
        id=str(uuid4()),
        session_id="s",
        user_id="target",
        channel="console",
        name="test",
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=chat))
    workspace = SimpleNamespace(
        chat_manager=manager,
        runner=SimpleNamespace(session=session),
        workspace_dir=tmp_path,
    )
    monkeypatch.setattr(
        tracing,
        "_get_target_workspace",
        AsyncMock(return_value=workspace),
    )
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(tracing.router)
    app.dependency_overrides[api.get_workspace] = lambda: workspace
    app.dependency_overrides[api.get_chat_manager] = lambda: manager
    app.dependency_overrides[api.get_session] = lambda: session
    return TestClient(app), session, chat, workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "failed", "memory", "cold-turns"])
async def test_detail_parity(history_client, kind):
    client, session, chat, workspace = history_client
    if kind == "cold-turns":
        state = {
            "turn_states": {
                f"t{i}": {
                    "status": "completed",
                    "message": {
                        "id": f"t{i}",
                        "role": "user",
                        "content": "question",
                    },
                    "created_at": str(i),
                    "chat_id": chat.id,
                }
                for i in range(80)
            },
        }
    elif kind == "memory":
        raw = Msg(
            "user",
            "visible + secret",
            "user",
            metadata={
                "console_hidden_context_v1": {
                    "visible_text": "visible",
                    "suffix": " + secret",
                },
            },
        )
        state = {"agent": {"memory": {"content": [[raw.to_dict(), []]]}}}
    else:
        key = (
            "task_messages" if kind == "task" else "model_call_failed_messages"
        )
        state = {
            key: [
                {
                    "id": "m",
                    "role": "assistant",
                    "type": "message" if kind == "task" else "error",
                    "content": [{"type": "text", "text": "result"}],
                    "code": "model_call_failed",
                    "message": "provider failed",
                    "status": "completed" if kind == "task" else "failed",
                },
            ],
        }
    await session.save_merged_state("s", user_id="target", state=state)
    if kind == "cold-turns":
        await session.migrate_terminal_turns("s", "target", dry_run=False)
    normal = client.get(f"/chats/{chat.id}")
    analytics = client.get(
        f"/tracing/chats/{chat.id}",
        params={"user_id": "target"},
    )
    assert normal.status_code == analytics.status_code == 200
    assert analytics.json() == normal.json()
    assert analytics.json()["messages"]
    assert "secret" not in str(analytics.json())


@pytest.mark.asyncio
async def test_archive_pages_and_validation(history_client):
    client, _, chat, workspace = history_client
    store = ConversationArchiveStore(workspace.workspace_dir / "dialog")
    await store.commit(
        chat.id,
        [Msg("user", f"archive-{i}", "user") for i in range(5)],
    )
    before = None
    seen = []
    while True:
        params = {"limit": 2}
        if before:
            params["before"] = before
        normal = client.get(f"/chats/{chat.id}/history", params=params)
        analytics = client.get(
            f"/tracing/chats/{chat.id}/history",
            params={**params, "user_id": "target"},
        )
        assert analytics.status_code == normal.status_code == 200
        assert analytics.json() == normal.json()
        page = analytics.json()
        seen.extend(m["id"] for m in page["messages"])
        if not page["has_more"]:
            break
        before = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 5
    for params in ({"limit": 51}, {"limit": 0}, {"before": "invalid"}):
        assert (
            client.get(
                f"/tracing/chats/{chat.id}/history",
                params={**params, "user_id": "target"},
            ).status_code
            == 422
        )
    for suffix in ("", "/history"):
        assert (
            client.get(
                f"/tracing/chats/{chat.id}{suffix}",
                params={"user_id": "other"},
            ).status_code
            == 404
        )


@pytest.mark.asyncio
async def test_running_history_does_not_take_execution_lock(history_client):
    client, session, chat, workspace = history_client
    await session.save_merged_state(
        "s",
        user_id="target",
        state={"task_messages": [{"id": "saved", "content": "committed"}]},
    )
    workspace.answer_turn_coordinator = SimpleNamespace(
        status=AsyncMock(return_value=SimpleNamespace(value="running")),
    )
    async with session.execution("s", "target"):
        response = client.get(
            f"/tracing/chats/{chat.id}",
            params={"user_id": "target"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["messages"][0]["id"] == "saved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,header",
    [("source_id", "X-Source-Id"), ("agent_id", "X-Agent-Id")],
)
async def test_target_metadata_isolation(history_client, key, header):
    client, _, chat, _ = history_client
    chat.meta[key] = "original"
    for suffix in ("", "/history"):
        response = client.get(
            f"/tracing/chats/{chat.id}{suffix}",
            params={"user_id": "target"},
            headers={header: "other"},
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_empty_archive_and_missing_target(history_client):
    client, _, chat, workspace = history_client
    response = client.get(
        f"/tracing/chats/{chat.id}/history",
        params={"user_id": "target"},
    )
    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert response.json()["has_more"] is False
    assert client.get(f"/tracing/chats/{chat.id}/history").status_code == 422
    workspace.chat_manager.get_chat.return_value = None
    assert (
        client.get(
            f"/tracing/chats/{chat.id}/history",
            params={"user_id": "target"},
        ).status_code
        == 404
    )
