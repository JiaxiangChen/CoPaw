# -*- coding: utf-8 -*-
"""验证 SWE 只准备执行资源，不写任务文件或 Scheduler 管理字段。"""

import copy
import os

import pytest


class Chats:
    def __init__(self):
        self.rows = {}
        self.created = 0
        self.deleted = []

    async def get_chat(self, chat_id):
        return copy.deepcopy(self.rows.get(chat_id))

    async def create_chat(self, chat):
        self.created += chat.id not in self.rows
        self.rows[chat.id] = copy.deepcopy(chat)
        return chat

    async def update_chat(self, chat):
        self.rows[chat.id] = copy.deepcopy(chat)
        return chat

    async def delete_chats(self, chat_ids):
        for chat_id in chat_ids:
            self.rows.pop(chat_id, None)
            self.deleted.append(chat_id)
        return True


def spec():
    from swe.config.context import encode_scope_id

    return {
        "id": "new-job",
        "name": "Report",
        "task_type": "agent",
        "tenant_id": "alice",
        "source_id": "bank",
        "scope_id": encode_scope_id("alice", "bank"),
        "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
        "request": {"input": "report"},
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {"user_id": "alice", "session_id": "cron-task:new-job"},
        },
        "meta": {"creator_user_id": "alice"},
    }


async def valid_model(_spec):
    return None


@pytest.mark.asyncio
async def test_prepare_reuses_stable_chat_without_job_file_or_old_cache(
    tmp_path,
):
    from swe.app.crons.managed_resources import ResourcePreparer

    chats = Chats()
    service = ResourcePreparer(
        chats,
        agent_id="default",
        validate_model=valid_model,
    )
    first = await service.prepare(spec(), {})
    second = await service.prepare(spec(), {})
    assert first["chat_id"] == second["chat_id"]
    assert first["session_id"] == "cron-task:new-job"
    assert first["spec"]["request"]["session_id"] == first["session_id"]
    assert chats.created == 1
    assert chats.rows[first["chat_id"]].meta["management_owner"] == "scheduler"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_prepare_does_not_copy_pause_unread_or_runtime_defaults():
    from swe.app.crons.managed_resources import ResourcePreparer

    service = ResourcePreparer(
        Chats(),
        agent_id="default",
        validate_model=valid_model,
    )
    result = await service.prepare(spec(), {})
    assert "task_unread_execution_count" not in result["spec"]["meta"]
    assert "pause_reason" not in result["spec"]["meta"]


@pytest.mark.asyncio
async def test_prepare_rejects_foreign_binding_instead_of_creating_replacement():
    from swe.app.crons.managed_resources import ResourcePreparer

    chats = Chats()
    service = ResourcePreparer(
        chats,
        agent_id="default",
        validate_model=valid_model,
    )
    first = await service.prepare(spec(), {})
    chats.rows[first["chat_id"]].user_id = "someone-else"
    with pytest.raises(ValueError, match="binding"):
        await service.prepare(spec(), {"task_chat_id": first["chat_id"]})
    assert chats.created == 1


@pytest.mark.asyncio
async def test_prepare_validates_model_before_creating_resource():
    from swe.app.crons.managed_resources import ResourcePreparer

    async def missing_model(_spec):
        raise ValueError("model_not_found")

    chats = Chats()
    service = ResourcePreparer(
        chats,
        agent_id="default",
        validate_model=missing_model,
    )
    with pytest.raises(ValueError, match="model_not_found"):
        await service.prepare(spec(), {})
    assert chats.created == 0


@pytest.mark.asyncio
async def test_text_skips_model_and_workflow_requires_published_configuration():
    from swe.app.crons.managed_resources import ResourcePreparer

    async def do_not_call(_spec):
        pytest.fail("Text should not prepare a model")

    service = ResourcePreparer(
        Chats(),
        agent_id="default",
        validate_model=do_not_call,
    )
    text = {**spec(), "task_type": "text", "text": "hello", "request": None}
    assert (await service.prepare(text, {}))["spec"]["text"] == "hello"
    workflow = {
        **spec(),
        "task_type": "workflow",
        "skill_ids": "report-skill",
        "request": None,
    }
    with pytest.raises(RuntimeError, match="workflow"):
        await service.prepare(workflow, {})


@pytest.mark.asyncio
async def test_cleanup_checks_binding_and_is_idempotent():
    from swe.app.crons.managed_resources import ResourcePreparer

    chats = Chats()
    service = ResourcePreparer(
        chats,
        agent_id="default",
        validate_model=valid_model,
    )
    prepared = await service.prepare(spec(), {})
    binding = {
        "task_chat_id": prepared["chat_id"],
        "task_session_id": prepared["session_id"],
    }
    assert (await service.cleanup(spec(), binding))["cleaned"] is True
    assert (await service.cleanup(spec(), binding))["cleaned"] is True
    assert chats.deleted == [prepared["chat_id"]]


@pytest.mark.asyncio
async def test_cleanup_does_not_delete_another_job_chat():
    from swe.app.crons.managed_resources import ResourcePreparer

    chats = Chats()
    service = ResourcePreparer(
        chats,
        agent_id="default",
        validate_model=valid_model,
    )
    prepared = await service.prepare(spec(), {})
    chats.rows[prepared["chat_id"]].meta["task_job_id"] = "other-job"
    with pytest.raises(ValueError, match="binding"):
        await service.cleanup(spec(), {"task_chat_id": prepared["chat_id"]})
    assert not chats.deleted


@pytest.mark.skipif(
    os.name == "nt",
    reason="Existing JsonChatRepository uses Linux fcntl",
)
@pytest.mark.asyncio
async def test_real_chat_repository_reuses_resource_after_manager_restart(
    tmp_path,
):
    from swe.app.crons.managed_resources import ResourcePreparer
    from swe.app.runner.manager import ChatManager
    from swe.app.runner.repo.json_repo import JsonChatRepository

    path = tmp_path / "chats.json"
    manager = ChatManager(repo=JsonChatRepository(path))
    first = await ResourcePreparer(
        manager,
        agent_id="default",
        validate_model=valid_model,
    ).prepare(spec(), {})
    restarted = ChatManager(repo=JsonChatRepository(path))
    resources = ResourcePreparer(
        restarted,
        agent_id="default",
        validate_model=valid_model,
    )
    second = await resources.prepare(spec(), {})
    assert first["chat_id"] == second["chat_id"]
    assert len(await restarted.list_chats(user_id="alice")) == 1
    assert not (tmp_path / "jobs.json").exists()
    assert (await resources.cleanup(spec(), {}))["cleaned"] is True
    assert not await restarted.list_chats(user_id="alice")
