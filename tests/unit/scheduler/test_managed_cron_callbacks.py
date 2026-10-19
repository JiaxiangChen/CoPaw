# -*- coding: utf-8 -*-
"""验证新回调每次独立受理、冻结成员并保留绑定和运行状态检查。"""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_dispatch_state import (
    ModeDatabase,
    ModePlatform,
)
from tests.unit.scheduler.test_managed_cron_management import ResourceClient
from tests.unit.scheduler.test_managed_cron_distribution import payload


class CallbackDatabase(ModeDatabase):
    def __init__(self):
        super().__init__()
        self.conn.create_function(
            "TIMESTAMPDIFF",
            3,
            lambda _unit, start, end: (
                int(
                    (
                        datetime.fromisoformat(end)
                        - datetime.fromisoformat(start)
                    ).total_seconds(),
                )
                if start and end
                else None
            ),
        )
        self.conn.executescript("""
            CREATE TABLE swe_cron_managed_trigger_receipts (
                receipt_id TEXT PRIMARY KEY, callback_key TEXT UNIQUE,
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                external_job_id TEXT, operation_id TEXT, kind TEXT,
                trigger_id TEXT, logical_fire_at TEXT, physical_fire_at TEXT,
                batch_id TEXT, status TEXT, response_json TEXT, members_json TEXT,
                created_at TEXT
            );
        """)

    async def fetch_all(self, query, params=None):
        query = re.sub(
            r"TIMESTAMPDIFF\(\s*SECOND\s*,",
            "TIMESTAMPDIFF('SECOND',",
            query,
        )
        return await super().fetch_all(query, params)


@pytest.fixture
def callback_setup():
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    db = CallbackDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    service = ManagementService(db, ResourceClient(), ModePlatform())
    yield db, caller, service
    db.conn.close()


async def create_source(callback_setup, mode="ordinary"):
    _, caller, service = callback_setup
    return await service.create(
        caller,
        {
            "request_id": "callback-source",
            "dispatch_state": mode,
            "spec": {
                "name": "report",
                "task_type": "text",
                "text": "body",
                "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            },
        },
    )


def body(db, created, *, kind="ordinary", trigger="platform-log-1"):
    column = (
        "ordinary_external_job_id"
        if kind == "ordinary"
        else "batch_external_job_id"
    )
    return {
        "job_id": created["job_id"],
        "external_job_id": db.conn.execute(
            f"SELECT {column} FROM swe_cron_job_states WHERE job_id=?",
            (created["job_id"],),
        ).fetchone()[0],
        "operation_id": created["operation_id"],
        "callback_protocol": "scheduler_managed_v1",
        "trigger_id": trigger,
        "scheduled_fire_at": "2026-10-16T09:00:00+08:00",
    }


@pytest.mark.asyncio
async def test_normal_callback_is_independent_but_explicit_stale_time_is_rejected(
    callback_setup,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    created = await create_source(callback_setup)
    now = datetime(2026, 10, 16, 1, tzinfo=timezone.utc)
    service = ManagedSchedulingService(db, clock=lambda: now)
    command = body(db, created)
    first = await service.accept(caller, command, "ordinary")
    again = await service.accept(caller, command, "ordinary")
    assert first["accepted"]
    assert again["accepted"] and not again["reused"]
    assert first["execution_id"] != again["execution_id"]
    now += timedelta(days=1)
    stale = await service.accept(caller, command, "ordinary")
    assert stale["status"] == "skipped"
    assert stale["reason"] == "trigger_outside_window"
    assert not stale["reused"]
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 2
    )


@pytest.mark.asyncio
async def test_batch_freezes_members_and_preserves_pause_semantics(
    callback_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    task = await BroadcastStore(db).submit(
        caller,
        created["job_id"],
        payload(),
    )
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    command = body(db, created, kind="batch")
    command["operation_id"] = task["task_id"]
    service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 15, 23, tzinfo=timezone.utc),
    )
    result = await service.accept(caller, command, "batch")
    assert result["enqueued_intents"] == 3
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 3
    )
    db.conn.execute(
        "UPDATE swe_cron_job_states SET dispatch_state='batch_paused'",
    )
    replay = await service.accept(caller, command, "batch")
    assert replay["status"] == "skipped"
    assert replay["reason"] == "batch_paused"
    assert not replay["reused"]
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 3
    )
    assert all(
        json.loads(row[0])["protocol"] == "scheduler_managed_v1"
        for row in db.conn.execute(
            "SELECT dispatch_meta_json FROM swe_cron_execution_runs",
        ).fetchall()
    )


@pytest.mark.asyncio
async def test_wrong_binding_is_rejected_but_stable_trigger_identity_is_optional(
    callback_setup,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    created = await create_source(callback_setup)
    service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    command = body(db, created)
    with pytest.raises(ValueError, match="binding"):
        await service.accept(
            caller,
            {**command, "external_job_id": "unrelated"},
            "ordinary",
        )
    command.pop("trigger_id")
    command.pop("scheduled_fire_at")
    result = await service.accept(caller, command, "ordinary")
    assert result["accepted"] and not result["reused"]
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_response_lost", [False, True])
async def test_update_time_trigger_is_skipped_but_new_arrival_after_publish_is_accepted(
    callback_setup,
    platform_response_lost,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup)
    scheduling = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    command = body(db, created, trigger="during-update")
    received = []
    original_update = management.platform.update
    original_enable = management.platform.set_enabled

    async def remember_update(external_id, operation, spec, mode):
        command["operation_id"] = operation["operation_id"]
        await original_update(external_id, operation, spec, mode)

    async def platform_enabled_before_journal_finishes(external_id, enabled):
        await original_enable(external_id, enabled)
        received.append(await scheduling.accept(caller, command, "ordinary"))
        if platform_response_lost:
            raise ConnectionError("platform response lost")

    management.platform.update = remember_update
    management.platform.set_enabled = platform_enabled_before_journal_finishes
    updated = await management.update(
        caller,
        created["job_id"],
        {
            "request_id": "update-with-trigger",
            "expected_definition_version": 1,
            "spec": {
                "name": "report",
                "task_type": "text",
                "text": "new body",
                "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            },
        },
    )
    assert received, "合法绑定触发应先保存跳过回执，不能在绑定校验处丢失"
    assert received[0]["status"] == "skipped"
    assert not received[0]["accepted"]
    management.platform.set_enabled = original_enable
    if platform_response_lost:
        assert updated["status"] == "failed"
        second = await scheduling.accept(
            caller,
            {**command, "trigger_id": "during-failed-update"},
            "ordinary",
        )
        assert second["status"] == "skipped"
        updated = await management.retry(
            caller,
            created["job_id"],
            updated["operation_id"],
        )
    assert updated["status"] == "completed"
    replay = await scheduling.accept(caller, command, "ordinary")
    assert not replay["reused"]
    assert replay["accepted"]
    assert received[0]["status"] == "skipped"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_incomplete_reference_without_active_operation_is_not_a_trusted_binding(
    callback_setup,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup)
    management.platform.fail_enable = True
    failed = await management.update(
        caller,
        created["job_id"],
        {
            "request_id": "failed-update",
            "expected_definition_version": 1,
            "spec": {
                "name": "report",
                "task_type": "text",
                "text": "new body",
                "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            },
        },
    )
    db.conn.execute(
        "UPDATE swe_cron_registration_journal SET active_key=NULL WHERE operation_id=?",
        (failed["operation_id"],),
    )
    command = {**body(db, created), "operation_id": failed["operation_id"]}
    with pytest.raises(ValueError, match="incomplete"):
        await ManagedSchedulingService(db).accept(caller, command, "ordinary")
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_managed_trigger_receipts",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_partial_mode_failure_accepts_effective_batch_and_keeps_ordinary_fallback(
    callback_setup,
):
    from dataclasses import replace
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup)
    task = await BroadcastStore(db).submit(
        caller,
        created["job_id"],
        payload(),
    )
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    bob = await db.fetch_one(
        "SELECT job_id,ordinary_external_job_id FROM swe_cron_job_states WHERE tenant_id='bob'",
    )
    original_bob_operation = (
        await db.fetch_one(
            "SELECT operation_id FROM swe_cron_registration_journal WHERE tenant_id='bob' AND action='create'",
        )
    )["operation_id"]
    modes = DispatchStateService(db, management.platform)
    accepted = await modes.set(
        caller,
        created["job_id"],
        {
            "request_id": "partial-batch",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    management.platform.fail_target = bob["ordinary_external_job_id"]
    await modes.process(accepted["operation_id"])
    state = await modes.get(caller, created["job_id"])
    assert state["sync_status"] == "failed"
    assert state["effective_state"] == "mixed"
    batch = {
        **body(db, created, kind="batch", trigger="partial-mode-tick"),
        "operation_id": accepted["operation_id"],
    }
    batch_service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 15, 23, tzinfo=timezone.utc),
    )
    first = await batch_service.accept(caller, batch, "batch")
    assert first["accepted"]
    assert first["enqueued_intents"] == 2
    fallback = {
        **body(
            db,
            {"job_id": bob["job_id"], "operation_id": original_bob_operation},
            trigger="old-entry-tick",
        ),
        "operation_id": original_bob_operation,
    }
    ordinary = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    assert (
        await ordinary.accept(
            replace(caller, tenant_id="bob"),
            fallback,
            "ordinary",
        )
    )["accepted"]
    management.platform.fail_target = None
    await modes.retry(caller, created["job_id"], accepted["operation_id"])
    again = await batch_service.accept(caller, batch, "batch")
    assert not again["reused"]
    assert again["batch_id"] != first["batch_id"]
    assert again["enqueued_intents"] == 3
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 6
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response_lost", [False, True])
async def test_broadcast_timer_update_accepts_known_binding_before_sync_finishes(
    callback_setup,
    response_lost,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    store = BroadcastStore(db)
    task = await store.submit(caller, created["job_id"], payload())
    now = datetime(2026, 10, 15, 23, tzinfo=timezone.utc)
    scheduling = ManagedSchedulingService(db, clock=lambda: now)
    command = {
        **body(db, created, kind="batch", trigger="broadcast-sync-tick"),
        "operation_id": task["task_id"],
    }
    original_update = management.platform.update
    observed = []

    async def updated_before_response(external_id, operation, spec, mode):
        await original_update(external_id, operation, spec, mode)
        if operation["operation_id"] == task["task_id"]:
            observed.append(await scheduling.accept(caller, command, "batch"))
            if response_lost:
                raise ConnectionError("response lost after timer update")

    management.platform.update = updated_before_response
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    result = await store.get(caller, created["job_id"], task["task_id"])
    assert observed, "已知批入口在持久计划写入后即可验证，不应等待平台响应"
    assert observed[0]["accepted"]
    assert observed[0]["enqueued_intents"] == 3
    assert result["sync_status"] == (
        "failed" if response_lost else "completed"
    )
    replay = await scheduling.accept(caller, command, "batch")
    assert not replay["reused"]
    assert replay["batch_id"] != observed[0]["batch_id"]
    assert replay["enqueued_intents"] == 3
    if response_lost:
        now += timedelta(days=1)
        later = await scheduling.accept(
            caller,
            {
                **command,
                "trigger_id": "next-confirmed-tick",
                "scheduled_fire_at": "2026-10-17T09:00:00+08:00",
            },
            "batch",
        )
        assert later["accepted"]
    assert db.conn.execute(
        "SELECT COUNT(*) FROM swe_cron_execution_runs",
    ).fetchone()[0] == (9 if response_lost else 6)


@pytest.mark.asyncio
async def test_pending_mode_tick_is_skipped_and_later_arrivals_are_independent(
    callback_setup,
):
    from dataclasses import replace
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    task = await BroadcastStore(db).submit(
        caller,
        created["job_id"],
        payload(),
    )
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    bob = await db.fetch_one(
        "SELECT job_id FROM swe_cron_jobs_v2 WHERE tenant_id='bob'",
    )
    modes = DispatchStateService(db, management.platform)
    accepted = await modes.set(
        caller,
        created["job_id"],
        {
            "request_id": "switch-ordinary",
            "state": "ordinary",
            "expected_version": 1,
        },
    )
    original_enable = management.platform.set_enabled
    scheduling = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    command = None
    observed = []
    receiver = replace(caller, tenant_id="bob")

    async def callback_before_publish(external_id, enabled):
        nonlocal command
        await original_enable(external_id, enabled)
        operation = await db.fetch_one(
            "SELECT * FROM swe_cron_registration_journal WHERE tenant_id='bob' AND action='mode_sync'",
        )
        if operation and external_id == f"timer-{bob['job_id']}-ordinary":
            command = {
                **body(
                    db,
                    {
                        "job_id": bob["job_id"],
                        "operation_id": operation["operation_id"],
                    },
                    trigger="before-mode-publish",
                ),
                "scheduled_fire_at": "2026-10-16T09:00:00+08:00",
            }
            # 接收人的实际 Cron 有散列偏移，使用其平台计划时间。
            spec = json.loads(operation["payload_json"])["spec"]
            offset = spec["meta"]["broadcast_offset_minutes"]
            command.pop("scheduled_fire_at")
            command["fire_time"] = (
                datetime(2026, 10, 16, 1, tzinfo=timezone.utc)
                - timedelta(minutes=offset)
            ).isoformat()
            scheduling.clock = lambda: datetime(
                2026,
                10,
                16,
                1,
                tzinfo=timezone.utc,
            ) - timedelta(minutes=offset)
            observed.append(
                await scheduling.accept(receiver, command, "ordinary"),
            )

    management.platform.set_enabled = callback_before_publish
    await modes.process(accepted["operation_id"])
    assert (await modes.get(caller, created["job_id"]))[
        "sync_status"
    ] == "completed"
    assert len(observed) == 1
    assert observed[0]["status"] == "skipped"
    assert observed[0]["reason"] == "job_not_eligible"
    later = await scheduling.accept(receiver, command, "ordinary")
    assert later["accepted"] and not later["reused"]
    fresh = await scheduling.accept(
        receiver,
        {**command, "trigger_id": "after-mode-publish"},
        "ordinary",
    )
    # 不补跑已跳过的旧执行；当前这两次请求各自创建新执行。
    assert fresh["accepted"] and not fresh["reused"]
    assert fresh["execution_id"] != later["execution_id"]
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_reference",
    ["missing_plan", "inactive_pending", "foreign_source"],
)
async def test_broadcast_reference_cannot_bypass_plan_and_ownership_checks(
    callback_setup,
    invalid_reference,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    task = await BroadcastStore(db).submit(
        caller,
        created["job_id"],
        payload(),
    )
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    updates = {
        "missing_plan": "payload_json='{}'",
        "inactive_pending": "sync_status='pending',status='running',active_key=NULL",
        "foreign_source": "source_id='another-bank'",
    }
    db.conn.execute(
        f"UPDATE swe_cron_managed_broadcast_tasks SET {updates[invalid_reference]} WHERE task_id=?",
        (task["task_id"],),
    )
    command = {
        **body(db, created, kind="batch"),
        "operation_id": task["task_id"],
    }
    with pytest.raises(ValueError, match="binding_reference_invalid"):
        await ManagedSchedulingService(db).accept(caller, command, "batch")
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_managed_trigger_receipts",
        ).fetchone()[0]
        == 0
    )
