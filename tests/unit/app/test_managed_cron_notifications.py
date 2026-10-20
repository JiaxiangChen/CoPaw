# -*- coding: utf-8 -*-
"""验证完成通知按管理归属读取冻结执行，避免依赖新版 jobs.json。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.crons.manager import CronManager
from swe.app.crons.models import CronJobSpec
from swe.app.crons.notification_worker import CronNotificationWorker
from swe.app.crons.repo.json_repo import JsonJobRepository
from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)


def _spec(task_type="agent"):
    return CronJobSpec.model_validate(
        {
            "id": "job-1",
            "name": "执行时的名称",
            "tenant_id": "alice",
            "source_id": "bank",
            "schedule": {"cron": "0 10 * * *", "timezone": "Asia/Shanghai"},
            "task_type": task_type,
            "request": {},
            "workflow_binding_id": (
                "binding-1" if task_type == "workflow" else None
            ),
            "dispatch": {
                "target": {
                    "user_id": "alice",
                    "session_id": "cron-task:job-1",
                },
            },
            "meta": {
                "creator_user_id": "alice",
                "task_session_id": "cron-task:job-1",
            },
        },
    )


@pytest.fixture
def notification_setup(tmp_path):
    """保留真实 JSON 仓库与通知入口，只替换外部发送和数据库连接。"""
    manager = object.__new__(CronManager)
    manager._repo = JsonJobRepository(tmp_path / "jobs.json")
    manager._push_task_success_notification = AsyncMock()
    agents = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=manager)
        ),
    )
    client = SimpleNamespace(
        mark_notification_sent=AsyncMock(),
        mark_notification_failed=AsyncMock(),
    )
    db = SimpleNamespace(fetch_one=AsyncMock())
    row = {
        "id": 101,
        "job_id": "job-1",
        "tenant_id": "alice",
        "source_id": "bank",
        "meta": json.dumps(
            {
                "management_owner": "scheduler",
                "managed_execution_id": "run-1",
                "source_id": "bank",
                "agent_id": "research",
            },
        ),
    }
    db.fetch_one.return_value = {
        "tenant_id": "alice",
        "source_id": "bank",
        "agent_id": "research",
        "job_id": "job-1",
        "definition_tenant_id": "alice",
        "definition_source_id": "bank",
        "definition_agent_id": "research",
        "definition_job_id": "job-1",
        "spec_json": _spec().model_dump_json(),
    }
    return manager, agents, client, db, row


def _worker(setup):
    _, agents, client, db, _ = setup
    return CronNotificationWorker(
        multi_agent_manager=agents,
        monitor_client=client,
        db=db,
        app_timezone="UTC",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["agent", "workflow"])
async def test_managed_notification_uses_frozen_snapshot_without_jobs_json(
    notification_setup,
    task_type,
):
    manager, agents, client, db, row = notification_setup
    spec = _spec(task_type)
    db.fetch_one.return_value["spec_json"] = spec.model_dump_json()

    assert await _worker(notification_setup)._send_one(row)

    assert not manager._repo.path.exists()
    manager._push_task_success_notification.assert_awaited_once_with(
        spec,
        raise_on_error=True,
    )
    assert agents.get_agent.await_args.args == ("research",)
    client.mark_notification_sent.assert_awaited_once()
    client.mark_notification_failed.assert_not_awaited()
    query, params = db.fetch_one.await_args.args
    assert "r.history_execution_id=%s" in query
    assert "d.management_owner='scheduler'" in query
    assert "d.deleted_at IS NULL" in query
    assert params == ("run-1", 101, "alice", "bank", "research", "job-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "legacy"])
async def test_legacy_notification_still_uses_jobs_json(
    notification_setup, owner
):
    manager, agents, client, db, row = notification_setup
    row["meta"] = json.dumps({"management_owner": owner})
    await manager._repo.upsert_job(_spec())

    assert await _worker(notification_setup)._send_one(row)

    db.fetch_one.assert_not_awaited()
    assert agents.get_agent.await_args.args == ("default",)
    manager._push_task_success_notification.assert_awaited_once_with(
        _spec(),
        raise_on_error=True,
    )
    client.mark_notification_sent.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["missing", "database", "identity", "definition_identity", "snapshot"],
)
async def test_managed_notification_failure_never_falls_back_to_legacy(
    notification_setup,
    failure,
):
    manager, agents, client, db, row = notification_setup
    await manager._repo.upsert_job(_spec())
    if failure == "missing":
        db.fetch_one.return_value = None
    elif failure == "database":
        db.fetch_one.side_effect = RuntimeError("database unavailable")
    elif failure == "identity":
        db.fetch_one.return_value["agent_id"] = "foreign"
    elif failure == "definition_identity":
        db.fetch_one.return_value["definition_tenant_id"] = "ALICE"
    else:
        raw = json.loads(db.fetch_one.return_value["spec_json"])
        raw["tenant_id"] = "bob"
        db.fetch_one.return_value["spec_json"] = json.dumps(raw)

    assert not await _worker(notification_setup)._send_one(row)

    agents.get_agent.assert_not_awaited()
    manager._push_task_success_notification.assert_not_awaited()
    client.mark_notification_sent.assert_not_awaited()
    client.mark_notification_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_notification_reuses_existing_channel_and_snapshot_target(
    notification_setup,
):
    """真实通知组装使用冻结名称、会话和接收人，不读取当前任务文件。"""
    manager, _, client, _, row = notification_setup
    del manager._push_task_success_notification
    manager._channel_manager = SimpleNamespace(send_text=AsyncMock())
    manager._build_session_end_push_link = (
        lambda _job: "https://example.test/chat"
    )

    assert await _worker(notification_setup)._send_one(row)

    sent = manager._channel_manager.send_text.await_args.kwargs
    assert sent["channel"] == "zhaohu"
    assert sent["user_id"] == "alice"
    assert sent["session_id"] == "cron-task:job-1"
    assert "执行时的名称" in sent["text"]
    assert sent["meta"]["link_url"] == "https://example.test/chat"
    client.mark_notification_sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_notification_snapshot_reader_with_real_sql(
    execution_setup,
):
    """执行真实查询验证历史绑定、当前归属和删除门控，不模拟查询结果。"""
    from swe.app.crons.managed_notifications import (
        load_notification_spec,
        notification_metadata,
    )
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db, _, _ = execution_setup
    run, _ = await enqueue(execution_setup)
    key = tuple(
        run[name] for name in ("tenant_id", "source_id", "agent_id", "job_id")
    )
    writer = ExecutionWriter(db)
    await writer.accept(run["execution_id"], key)
    await writer.start(run["execution_id"], key, "worker")
    await writer.complete(
        run["execution_id"], key, "worker", ExecutionOutcome("success")
    )
    row = await db.fetch_one("SELECT * FROM swe_cron_executions")
    meta = notification_metadata(row)
    original = await load_notification_spec(db, row, meta)

    db.conn.execute("UPDATE swe_cron_jobs_v2 SET name='修改后的任务'")
    assert (await load_notification_spec(db, row, meta)).name == original.name
    assert original.name != "修改后的任务"

    with pytest.raises(
        ValueError, match="managed_cron_notification_not_found"
    ):
        await load_notification_spec(db, {**row, "id": row["id"] + 1}, meta)
    with pytest.raises(
        ValueError, match="managed_cron_notification_not_found"
    ):
        await load_notification_spec(db, row, {**meta, "agent_id": "foreign"})
    db.conn.execute("UPDATE swe_cron_jobs_v2 SET management_owner='legacy'")
    with pytest.raises(
        ValueError, match="managed_cron_notification_not_found"
    ):
        await load_notification_spec(db, row, meta)
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET management_owner='scheduler', deleted_at='2026-10-20'"
    )
    with pytest.raises(
        ValueError, match="managed_cron_notification_not_found"
    ):
        await load_notification_spec(db, row, meta)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field", ["managed_execution_id", "source_id", "agent_id"]
)
async def test_managed_notification_requires_complete_metadata(
    notification_setup,
    missing_field,
):
    manager, agents, client, db, row = notification_setup
    meta = json.loads(row["meta"])
    del meta[missing_field]
    row["meta"] = json.dumps(meta)

    assert not await _worker(notification_setup)._send_one(row)

    db.fetch_one.assert_not_awaited()
    agents.get_agent.assert_not_awaited()
    manager._push_task_success_notification.assert_not_awaited()
    client.mark_notification_sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_notification_can_use_history_source_without_projection(
    notification_setup,
):
    """旧兼容投影缺来源时，仍以新版历史及共库身份发送。"""
    manager, _, client, _, row = notification_setup
    row["source_id"] = ""

    assert await _worker(notification_setup)._send_one(row)

    manager._push_task_success_notification.assert_awaited_once()
    client.mark_notification_sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_notification_conflicting_source_stops_before_lookup(
    notification_setup,
):
    manager, agents, client, db, row = notification_setup
    row["source_id"] = "foreign"

    assert not await _worker(notification_setup)._send_one(row)

    db.fetch_one.assert_not_awaited()
    agents.get_agent.assert_not_awaited()
    manager._push_task_success_notification.assert_not_awaited()
    client.mark_notification_sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_notification_requires_database_without_file_fallback(
    notification_setup,
):
    manager, agents, client, _, row = notification_setup
    await manager._repo.upsert_job(_spec())
    worker = _worker(notification_setup)
    worker._db = None

    assert not await worker._send_one(row)

    agents.get_agent.assert_not_awaited()
    manager._push_task_success_notification.assert_not_awaited()
    client.mark_notification_sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_sender_rejects_conflicting_snapshot(
    notification_setup,
):
    manager = notification_setup[0]

    with pytest.raises(
        ValueError, match="cron_notification_snapshot_conflict"
    ):
        await manager.send_task_success_notification(
            "another-job", job=_spec()
        )

    manager._push_task_success_notification.assert_not_awaited()
