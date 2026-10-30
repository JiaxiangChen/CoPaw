# -*- coding: utf-8 -*-
"""Expert admission rejects a stale identity before a user turn is started."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from swe.app.runner.models import ChatSpec
from swe.app.runner.manager import ChatManager
from swe.app.runner.repo.json_repo import JsonChatRepository


@pytest.fixture
def admitted_chat(tmp_path):
    repo = JsonChatRepository(tmp_path / "chats.json")
    asyncio.run(
        repo.upsert_chat(
            ChatSpec(id="chat", session_id="session", user_id="alice"),
        ),
    )
    asyncio.run(repo.compare_and_set_expert_binding("chat", 0, "expert"))
    manager = ChatManager(repo=repo)
    workspace = SimpleNamespace(
        chat_manager=manager,
        runner=SimpleNamespace(),
        agent_id="profile",
        tenant_id="alice",
        workspace_dir=tmp_path,
    )
    return workspace


def payload(revision=1):
    return {
        "sender_id": "alice",
        "channel_id": "console",
        "content_parts": [],
        "meta": {"source_id": "source", "expert_binding_revision": revision},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, None, True])
async def test_stale_or_missing_revision_blocks_before_preparation(
    admitted_chat,
    monkeypatch,
    revision,
):
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    prepare = Mock(side_effect=AssertionError("must not prepare"))
    monkeypatch.setattr(admission, "_prepare", prepare)
    chat = await admitted_chat.chat_manager.get_chat("chat")
    with pytest.raises(HTTPException) as error:
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(revision),
            "message",
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "expert_binding_changed"
    prepare.assert_not_called()


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_explicit_chat_wins_over_newer_duplicate_logical_session(
    tmp_path,
):
    from swe.app.routers.console import (
        _extract_session_and_payload,
        _get_or_create_console_chat,
    )

    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    for chat_id in ("selected", "other"):
        await manager.create_chat(
            ChatSpec(
                id=chat_id,
                session_id="shared",
                user_id="alice",
                channel="console",
            ),
        )
    data = _extract_session_and_payload(
        {
            "session_id": "shared",
            "user_id": "alice",
            "chat_id": "selected",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                },
            ],
        },
    )
    chat, _, _ = await _get_or_create_console_chat(
        SimpleNamespace(chat_manager=manager, agent_id="profile"),
        "shared",
        data,
    )
    assert chat.id == "selected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat_id,session_id,sender,source,agent,status",
    [
        ("missing", "session", "alice", "source", "profile", 404),
        (
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000001",
            "alice",
            "source",
            "profile",
            404,
        ),
        ("chat", "wrong-session", "alice", "source", "profile", 409),
        ("chat", "session", "bob", "source", "profile", 404),
        ("chat", "session", "alice", "other", "profile", 404),
        ("chat", "session", "alice", "source", "other", 404),
    ],
)
async def test_explicit_chat_never_falls_back_on_invalid_identity(
    admitted_chat,
    chat_id,
    session_id,
    sender,
    source,
    agent,
    status,
):
    from swe.app.routers.console import _get_or_create_console_chat

    chat = await admitted_chat.chat_manager.get_chat("chat")
    chat.channel = "console"
    chat.meta.update(source_id="source", agent_id="profile")
    await admitted_chat.chat_manager.update_chat(chat)
    data = payload()
    data.update(chat_id=chat_id, sender_id=sender)
    data["meta"]["source_id"] = source
    admitted_chat.agent_id = agent
    with pytest.raises(HTTPException) as error:
        await _get_or_create_console_chat(admitted_chat, session_id, data)
    assert error.value.status_code == status


@pytest.mark.asyncio
async def test_disabled_mode_releases_without_executing_or_restoring(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(False),
    )
    chat = await admitted_chat.chat_manager.get_chat("chat")
    with pytest.raises(HTTPException) as error:
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(),
            "message",
        )
    assert error.value.detail["code"] == "expert_main_disabled"
    assert error.value.detail["expert_binding"]["expert_id"] is None
    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    chat = await admitted_chat.chat_manager.get_chat("chat")
    assert (
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(2),
            "message",
        )
        is None
    )


@pytest.mark.asyncio
async def test_unavailable_expert_retains_binding_and_blocks(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    monkeypatch.setattr(admission, "resolve_bound_expert", lambda *args: None)
    chat = await admitted_chat.chat_manager.get_chat("chat")
    with pytest.raises(HTTPException) as error:
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(),
            "message",
        )
    assert error.value.detail["code"] == "expert_unavailable"
    assert (await admitted_chat.chat_manager.get_chat("chat")).meta[
        "expert_main_binding"
    ]["expert_id"] == "expert"


@pytest.mark.asyncio
async def test_valid_message_registers_one_snapshot_before_start(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    monkeypatch.setattr(
        admission,
        "resolve_bound_expert",
        lambda *args: SimpleNamespace(name="expert"),
    )
    prepared = SimpleNamespace(cleanup=Mock())
    monkeypatch.setattr(admission, "_prepare", lambda *args: prepared)
    register = Mock(return_value="trusted")
    admitted_chat.runner.register_expert_main_execution = register
    chat = await admitted_chat.chat_manager.get_chat("chat")
    data = payload()
    assert (
        await admission.admit_expert_main(admitted_chat, chat, data, "message")
        == "trusted"
    )
    assert data["meta"]["_expert_main_execution_token"] == "trusted"
    register.assert_called_once()


@pytest.mark.parametrize(
    "enabled, revision, code",
    [(True, 0, "expert_binding_changed"), (False, 1, "expert_main_disabled")],
)
def test_http_rejection_publishes_no_user_turn_or_queue(
    admitted_chat,
    monkeypatch,
    enabled,
    revision,
    code,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from swe.app.routers import console
    from swe.app.runner import expert_main_admission as admission
    from unittest.mock import AsyncMock

    class Channel:
        def resolve_session_id(self, sender_id, channel_meta):
            return "session"

    admitted_chat.channel_manager = SimpleNamespace(
        get_channel=AsyncMock(return_value=Channel()),
    )
    admitted_chat.answer_turn_coordinator = SimpleNamespace(
        start_or_attach=AsyncMock(),
        status=AsyncMock(return_value=None),
    )
    admitted_chat.task_tracker = SimpleNamespace()
    monkeypatch.setattr(
        console,
        "get_agent_for_request",
        lambda request: _async(admitted_chat),
    )

    async def get_chat(*args):
        return await admitted_chat.chat_manager.get_chat("chat"), None, False

    monkeypatch.setattr(console, "_get_or_create_console_chat", get_chat)
    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(enabled),
    )
    app = FastAPI()
    app.include_router(console.router)
    response = TestClient(app).post(
        "/console/chat",
        headers={"X-Source-Id": "source"},
        json={
            "session_id": "session",
            "user_id": "alice",
            "expert_binding_revision": revision,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "retain my draft"}],
                },
            ],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code
    admitted_chat.answer_turn_coordinator.start_or_attach.assert_not_called()


@pytest.mark.asyncio
async def test_binding_change_during_snapshot_capture_rejects_and_cleans(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    monkeypatch.setattr(
        admission,
        "resolve_bound_expert",
        lambda *args: SimpleNamespace(name="expert"),
    )
    prepared = SimpleNamespace(cleanup=Mock())

    def prepare(*args):
        asyncio.run(
            admitted_chat.chat_manager.compare_and_set_expert_binding(
                "chat",
                1,
                "changed",
            ),
        )
        return prepared

    monkeypatch.setattr(admission, "_prepare", prepare)
    admitted_chat.runner.register_expert_main_execution = Mock()
    chat = await admitted_chat.chat_manager.get_chat("chat")
    with pytest.raises(HTTPException) as error:
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(),
            "message",
        )
    assert error.value.detail["code"] == "expert_binding_changed"
    admitted_chat.runner.register_expert_main_execution.assert_not_called()
    prepared.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_approval_resumes_original_identity_after_selection_changed(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    admitted_chat.runner.resume_expert_main_execution = Mock(
        return_value="original-token",
    )
    data = payload(0)
    data["content_parts"] = [
        {"type": "text", "text": "/approve approval-original"},
    ]
    chat = await admitted_chat.chat_manager.get_chat("chat")
    assert (
        await admission.admit_expert_main(admitted_chat, chat, data, "message")
        == "original-token"
    )
    assert data["meta"]["_expert_main_execution_token"] == "original-token"


@pytest.mark.asyncio
async def test_mode_transition_and_explicit_reset_release_binding(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    data = payload()
    data["content_parts"] = [{"text": "/new"}]
    chat = await admitted_chat.chat_manager.get_chat("chat")
    assert (
        await admission.admit_expert_main(admitted_chat, chat, data, "message")
        is None
    )
    assert (await admitted_chat.chat_manager.get_chat("chat")).meta[
        "expert_main_binding"
    ] == {"revision": 2, "expert_id": None}
    assert "expert_binding_notice" in data["meta"]
    assert data["meta"]["_expert_main_reset"] is True


@pytest.mark.asyncio
async def test_source_disabled_during_preparation_releases_and_cleans(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        AsyncMock(side_effect=[True, False]),
    )
    monkeypatch.setattr(
        admission,
        "resolve_bound_expert",
        lambda *args: SimpleNamespace(),
    )
    prepared = SimpleNamespace(cleanup=Mock())
    monkeypatch.setattr(admission, "_prepare", lambda *args: prepared)
    admitted_chat.runner.register_expert_main_execution = Mock()
    chat = await admitted_chat.chat_manager.get_chat("chat")
    with pytest.raises(HTTPException) as error:
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(),
            "message",
        )
    assert error.value.detail["code"] == "expert_main_disabled"
    assert error.value.detail["expert_binding"]["expert_id"] is None
    prepared.cleanup.assert_called_once()
    admitted_chat.runner.register_expert_main_execution.assert_not_called()


@pytest.mark.asyncio
async def test_missing_coordinator_discards_prepared_execution(
    admitted_chat,
    monkeypatch,
):
    from swe.app.routers import console
    from swe.app.runner import expert_main_admission as admission
    from unittest.mock import AsyncMock

    chat = await admitted_chat.chat_manager.get_chat("chat")
    monkeypatch.setattr(
        console,
        "_get_or_create_console_chat",
        AsyncMock(return_value=(chat, None, False)),
    )
    monkeypatch.setattr(
        admission,
        "admit_expert_main",
        AsyncMock(return_value="token"),
    )
    admitted_chat.answer_turn_coordinator = None
    admitted_chat.runner.discard_expert_main_execution = AsyncMock()
    with pytest.raises(RuntimeError, match="coordinator"):
        await console._start_new_chat(
            admitted_chat,
            None,
            None,
            "session",
            payload(),
        )
    admitted_chat.runner.discard_expert_main_execution.assert_awaited_once_with(
        "token",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/approve", "/deny approval-original"])
async def test_approval_without_snapshot_never_reprepares(
    admitted_chat,
    monkeypatch,
    command,
):
    from swe.app.runner import expert_main_admission as admission
    from swe.app.runner import runner
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        runner,
        "_select_pending_approval",
        AsyncMock(
            return_value=SimpleNamespace(
                request_id="approval-original",
                extra={"execution_identity": {"kind": "expert_main"}},
            ),
        ),
    )
    prepare = Mock(side_effect=AssertionError("must preserve old snapshot"))
    monkeypatch.setattr(admission, "_prepare", prepare)
    admitted_chat.runner.resume_expert_main_execution = Mock(return_value=None)
    chat = await admitted_chat.chat_manager.get_chat("chat")
    data = payload()
    data["content_parts"] = [{"text": command}]
    with pytest.raises(HTTPException) as error:
        await admission.admit_expert_main(admitted_chat, chat, data, "message")
    assert error.value.detail["code"] == "expert_unavailable"
    assert (
        error.value.detail["expert_binding"]["reason"]
        == "expert_execution_unavailable"
    )
    prepare.assert_not_called()


@pytest.mark.asyncio
async def test_approval_without_id_resumes_fifo_original_snapshot(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission
    from swe.app.runner import runner
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        runner,
        "_select_pending_approval",
        AsyncMock(
            return_value=SimpleNamespace(
                request_id="approval-original",
                extra={"execution_identity": {"kind": "expert_main"}},
            ),
        ),
    )
    admitted_chat.runner.resume_expert_main_execution = Mock(
        return_value="old",
    )
    data = payload(0)
    data["content_parts"] = [{"text": "/approve"}]
    chat = await admitted_chat.chat_manager.get_chat("chat")
    assert (
        await admission.admit_expert_main(admitted_chat, chat, data, "message")
        == "old"
    )
    assert (
        admitted_chat.runner.resume_expert_main_execution.call_args.kwargs[
            "approval_id"
        ]
        == "approval-original"
    )


@pytest.mark.asyncio
async def test_reset_racing_binding_switch_returns_identity_conflict(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission
    from swe.app.runner.expert_binding import (
        ChatExpertBinding,
        ExpertBindingConflict,
    )
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        AsyncMock(return_value=True),
    )
    admitted_chat.chat_manager.compare_and_set_expert_binding = AsyncMock(
        side_effect=ExpertBindingConflict(
            ChatExpertBinding(revision=2, expert_id="other"),
        ),
    )
    data = payload()
    data["content_parts"] = [{"text": "/new"}]
    chat = await admitted_chat.chat_manager.get_chat("chat")
    with pytest.raises(HTTPException) as error:
        await admission.admit_expert_main(admitted_chat, chat, data, "message")
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "expert_binding_changed"


@pytest.mark.asyncio
async def test_goal_resume_without_mode_flag_releases_binding(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        AsyncMock(return_value=True),
    )
    data = payload()
    data["meta"]["goal_id"] = "goal"
    chat = await admitted_chat.chat_manager.get_chat("chat")
    assert (
        await admission.admit_expert_main(admitted_chat, chat, data, "message")
        is None
    )
    assert (await admitted_chat.chat_manager.get_chat("chat")).meta[
        "expert_main_binding"
    ]["expert_id"] is None


@pytest.mark.asyncio
async def test_body_mention_without_binding_never_prepares_an_expert(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission

    await admitted_chat.chat_manager.compare_and_set_expert_binding(
        "chat",
        1,
        None,
    )
    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    prepare = Mock(side_effect=AssertionError("body mention is not a binding"))
    monkeypatch.setattr(admission, "_prepare", prepare)
    data = payload(2)
    data["content_parts"] = [{"text": "请让专家 reviewer 帮我处理"}]
    chat = await admitted_chat.chat_manager.get_chat("chat")
    assert (
        await admission.admit_expert_main(admitted_chat, chat, data, "mention")
        is None
    )
    prepare.assert_not_called()
    assert "_expert_main_execution_token" not in data["meta"]
    assert (await admitted_chat.chat_manager.get_chat("chat")).meta[
        "expert_main_binding"
    ] == {"revision": 2, "expert_id": None}


@pytest.mark.asyncio
async def test_same_identity_recovers_but_same_name_replacement_cannot_take_over(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission
    from swe.app.subagents import AgentOwnedDefinitionRepository

    repository = AgentOwnedDefinitionRepository(
        admitted_chat.workspace_dir / "agents",
        owner_scope="alice/profile",
    )
    definition_payload = {
        "name": "reviewer",
        "description": "Review",
        "instruction": "Expert role",
        "skills": [],
        "mcps": [],
        "tools": {"inherit": False, "allow": ["read_file"]},
    }
    original = repository.create(definition_payload)
    await admitted_chat.chat_manager.compare_and_set_expert_binding(
        "chat",
        1,
        original.definition_id,
    )
    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    prepared = SimpleNamespace(cleanup=Mock())
    prepare = Mock(return_value=prepared)
    monkeypatch.setattr(admission, "_prepare", prepare)
    register = Mock(return_value="restored")
    admitted_chat.runner.register_expert_main_execution = register
    chat = await admitted_chat.chat_manager.get_chat("chat")
    with pytest.raises(HTTPException) as unavailable:
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(2),
            "disabled",
        )
    assert unavailable.value.detail["code"] == "expert_unavailable"
    prepare.assert_not_called()
    original = repository.enable(
        original.definition_id,
        expected_revision=original.revision,
    )
    assert (
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(2),
            "restored",
        )
        == "restored"
    )
    assert prepare.call_args.args[3].agent_owned.definition_id == (
        original.definition_id
    )
    repository.delete(
        original.definition_id,
        expected_revision=original.revision,
    )
    replacement = repository.create(definition_payload)
    replacement = repository.enable(
        replacement.definition_id,
        expected_revision=replacement.revision,
    )
    assert replacement.definition_id != original.definition_id
    with pytest.raises(HTTPException) as replaced:
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(2),
            "same-name",
        )
    assert replaced.value.detail["code"] == "expert_unavailable"
    assert replaced.value.detail["expert_binding"]["expert_id"] == (
        original.definition_id
    )
    assert register.call_count == 1
    assert prepare.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/compact", "普通消息"])
async def test_compact_and_forged_reset_marker_keep_expert_binding(
    admitted_chat,
    monkeypatch,
    text,
):
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    monkeypatch.setattr(
        admission,
        "resolve_bound_expert",
        lambda *args: object(),
    )
    monkeypatch.setattr(
        admission,
        "_prepare",
        lambda *args: SimpleNamespace(cleanup=Mock()),
    )
    admitted_chat.runner.register_expert_main_execution = Mock(
        return_value="trusted",
    )
    data = payload()
    data["content_parts"] = [{"text": text}]
    data["meta"]["_expert_main_reset"] = True
    chat = await admitted_chat.chat_manager.get_chat("chat")
    assert (
        await admission.admit_expert_main(admitted_chat, chat, data, "message")
        == "trusted"
    )
    assert "_expert_main_reset" not in data["meta"]
    assert (await admitted_chat.chat_manager.get_chat("chat")).meta[
        "expert_main_binding"
    ] == {"revision": 1, "expert_id": "expert"}


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new", "/clear"])
async def test_explicit_reset_sets_trusted_marker_and_releases_binding(
    admitted_chat,
    monkeypatch,
    command,
):
    from unittest.mock import AsyncMock
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    admitted_chat.runner.discard_expert_main_pending = AsyncMock()
    prepare = Mock(side_effect=AssertionError("reset must not prepare"))
    monkeypatch.setattr(admission, "_prepare", prepare)
    data = payload()
    data["content_parts"] = [{"text": command}]
    chat = await admitted_chat.chat_manager.get_chat("chat")
    assert (
        await admission.admit_expert_main(admitted_chat, chat, data, "reset")
        is None
    )
    assert data["meta"]["_expert_main_reset"] is True
    assert (await admitted_chat.chat_manager.get_chat("chat")).meta[
        "expert_main_binding"
    ] == {"revision": 2, "expert_id": None}
    admitted_chat.runner.discard_expert_main_pending.assert_awaited_once_with(
        chat_id="chat",
        user_id="alice",
        source_id="source",
    )
    prepare.assert_not_called()


@pytest.mark.asyncio
async def test_binding_switch_only_changes_the_next_message_lease(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission
    from swe.app.runner.runner import AgentRunner

    runner = object.__new__(AgentRunner)
    admitted_chat.runner = runner
    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    monkeypatch.setattr(
        admission,
        "resolve_bound_expert",
        lambda workspace, expert_id: SimpleNamespace(expert_id=expert_id),
    )

    def prepare(workspace, chat, binding, definition):
        return SimpleNamespace(
            expert_id=definition.expert_id,
            expert_name=definition.expert_id,
            binding_revision=binding.revision,
            cleanup=Mock(),
        )

    monkeypatch.setattr(admission, "_prepare", prepare)
    first = payload()
    first_token = await admission.admit_expert_main(
        admitted_chat,
        await admitted_chat.chat_manager.get_chat("chat"),
        first,
        "first",
    )
    registry = runner._expert_main_registry()
    original = registry.leases[first_token]
    original.started_at = 1.0
    await admitted_chat.chat_manager.compare_and_set_expert_binding(
        "chat",
        1,
        "expert-b",
    )
    second = payload(2)
    second_token = await admission.admit_expert_main(
        admitted_chat,
        await admitted_chat.chat_manager.get_chat("chat"),
        second,
        "second",
    )
    assert first_token != second_token
    assert registry.leases[first_token] is original
    assert original.prepared.expert_id == "expert"
    assert original.prepared.binding_revision == 1
    assert original.started_at == 1.0
    assert registry.leases[second_token].prepared.expert_id == "expert-b"
    assert registry.leases[second_token].prepared.binding_revision == 2
    await runner.discard_expert_main_execution(first_token)
    await runner.discard_expert_main_execution(second_token)


@pytest.mark.asyncio
async def test_cancelled_admission_cleans_preparation_after_worker_returns(
    admitted_chat,
    monkeypatch,
):
    import threading
    from swe.app.runner import expert_main_admission as admission

    monkeypatch.setattr(
        admission,
        "_source_enabled",
        lambda *args: _async(True),
    )
    monkeypatch.setattr(
        admission,
        "resolve_bound_expert",
        lambda *args: object(),
    )
    started = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    prepared = SimpleNamespace(cleanup=Mock())

    def prepare(*args):
        started.set()
        assert release.wait(timeout=2)
        returned.set()
        return prepared

    monkeypatch.setattr(admission, "_prepare", prepare)
    admitted_chat.runner.register_expert_main_execution = Mock()
    chat = await admitted_chat.chat_manager.get_chat("chat")
    task = asyncio.create_task(
        admission.admit_expert_main(admitted_chat, chat, payload(), "cancel"),
    )
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    while not returned.is_set():
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    prepared.cleanup.assert_called_once()
    admitted_chat.runner.register_expert_main_execution.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_prepare", "after_prepare"])
@pytest.mark.parametrize("deleted_at", ["release", "reload"])
async def test_disabled_admission_delete_race_returns_404(
    admitted_chat,
    monkeypatch,
    stage,
    deleted_at,
):
    from unittest.mock import AsyncMock
    from swe.app.runner import expert_main_admission as admission

    values = [False] if stage == "before_prepare" else [True, False]
    monkeypatch.setattr(
        admission,
        "_source_enabled",
        AsyncMock(side_effect=values),
    )
    monkeypatch.setattr(
        admission,
        "resolve_bound_expert",
        lambda *args: object(),
    )
    prepared = SimpleNamespace(cleanup=Mock())
    monkeypatch.setattr(admission, "_prepare", lambda *args: prepared)
    chat = await admitted_chat.chat_manager.get_chat("chat")
    if deleted_at == "release":
        admitted_chat.chat_manager.release_current_expert_binding = AsyncMock(
            side_effect=KeyError("chat"),
        )
    else:
        admitted_chat.chat_manager.get_chat = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as error:
        await admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(),
            "delete",
        )
    assert error.value.status_code == 404
    if stage == "after_prepare":
        prepared.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_admission_cleans_snapshot_during_revalidation(
    admitted_chat,
    monkeypatch,
):
    from swe.app.runner import expert_main_admission as admission

    revalidating = asyncio.Event()
    checks = 0

    async def enabled(*args):
        nonlocal checks
        checks += 1
        if checks == 2:
            revalidating.set()
            await asyncio.Event().wait()
        return True

    monkeypatch.setattr(admission, "_source_enabled", enabled)
    monkeypatch.setattr(
        admission,
        "resolve_bound_expert",
        lambda *args: object(),
    )
    prepared = SimpleNamespace(cleanup=Mock())
    monkeypatch.setattr(admission, "_prepare", lambda *args: prepared)
    admitted_chat.runner.register_expert_main_execution = Mock()
    chat = await admitted_chat.chat_manager.get_chat("chat")
    task = asyncio.create_task(
        admission.admit_expert_main(
            admitted_chat,
            chat,
            payload(),
            "cancel-revalidation",
        ),
    )
    await revalidating.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    prepared.cleanup.assert_called_once()
    admitted_chat.runner.register_expert_main_execution.assert_not_called()
