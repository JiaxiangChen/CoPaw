# -*- coding: utf-8 -*-
"""验证新版异步分发的持久受理、逐项目标预留和旧分发隔离。"""

import json
from datetime import datetime

import pytest

from tests.unit.scheduler.test_managed_cron_executions import ExecutionDatabase
from tests.unit.scheduler.test_managed_cron_management import (
    PlatformClient,
    ResourceClient,
)


class DistributionDatabase(ExecutionDatabase):
    def __init__(self):
        super().__init__()
        self.conn.executescript("""
            DROP TABLE IF EXISTS swe_cron_managed_broadcast_tasks;
            CREATE TABLE swe_cron_managed_broadcast_tasks (
                created_order INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT UNIQUE, active_key TEXT UNIQUE,
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                actor_id TEXT, actor_is_manager INTEGER, request_id TEXT,
                request_hash TEXT, source_definition_version INTEGER,
                payload_json TEXT, tenant_count INTEGER,
                status TEXT, sync_status TEXT, error_code TEXT,
                lease_owner TEXT, lease_until TEXT, version INTEGER,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE swe_cron_managed_broadcast_items (
                task_id TEXT, index_no INTEGER,
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                expected_definition_version INTEGER,
                operation TEXT, status TEXT, plan_json TEXT,
                result_json TEXT, cron_expr TEXT, timezone TEXT,
                offset_minutes INTEGER, warning TEXT,
                lease_owner TEXT, lease_until TEXT, version INTEGER,
                created_at TEXT, updated_at TEXT,
                PRIMARY KEY (task_id, index_no),
                UNIQUE (task_id, tenant_id, source_id, agent_id)
            );
            CREATE TABLE swe_cron_broadcast_tasks (task_id TEXT PRIMARY KEY);
            INSERT INTO swe_cron_broadcast_tasks VALUES ('legacy-task');
        """)


@pytest.fixture
def distribution_setup():
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    db = DistributionDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    management = ManagementService(db, ResourceClient(), PlatformClient())
    yield db, caller, management
    db.conn.close()


async def source(distribution_setup):
    _, caller, management = distribution_setup
    return await management.create(
        caller,
        {
            "request_id": "source-create",
            "dispatch_state": "batch_paused",
            "spec": {
                "name": "source report",
                "task_type": "text",
                "text": "report",
                "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            },
        },
    )


def payload(request_id="broadcast-1", *, targets=None):
    return {
        "request_id": request_id,
        "expected_definition_version": 1,
        "targets": (
            targets
            if targets is not None
            else [{"tenant_id": "bob"}, {"tenant_id": "carol"}]
        ),
        "enable_offset": True,
        "offset_window_hours": 2,
    }


@pytest.mark.asyncio
async def test_submit_reserves_targets_without_preparing_or_touching_legacy(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore

    db, caller, management = distribution_setup
    created = await source(distribution_setup)
    preparation_count = len(management.resources.prepared)
    response = await BroadcastStore(db).submit(
        caller,
        created["job_id"],
        payload(),
    )
    assert response["status"] == "queued"
    assert response["tenant_count"] == 2
    assert response["pending_count"] == 2
    assert len(management.resources.prepared) == preparation_count
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_jobs_v2").fetchone()[0]
        == 1
    )
    items = db.conn.execute(
        "SELECT * FROM swe_cron_managed_broadcast_items ORDER BY index_no",
    ).fetchall()
    assert [row["offset_minutes"] for row in items] == [0, 120]
    assert [row["cron_expr"] for row in items] == ["0 9 * * *", "0 7 * * *"]
    assert len({row["job_id"] for row in items}) == 2
    assert (
        db.conn.execute(
            "SELECT task_id FROM swe_cron_broadcast_tasks",
        ).fetchone()[0]
        == "legacy-task"
    )


@pytest.mark.asyncio
async def test_same_request_reuses_task_and_different_round_is_serialized(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import (
        BroadcastConflict,
        BroadcastStore,
    )

    db, caller, _ = distribution_setup
    created = await source(distribution_setup)
    store = BroadcastStore(db)
    first = await store.submit(caller, created["job_id"], payload())
    again = await store.submit(caller, created["job_id"], payload())
    assert again["task_id"] == first["task_id"]
    assert again["reused"]
    with pytest.raises(BroadcastConflict) as conflict:
        await store.submit(caller, created["job_id"], payload("new-round"))
    assert conflict.value.task_id == first["task_id"]


@pytest.mark.asyncio
async def test_zero_targets_reserves_only_existing_undeleted_children(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore

    db, caller, _ = distribution_setup
    created = await source(distribution_setup)
    parent = caller.job_key(created["job_id"])
    db.add(
        "child-one",
        tenant="bob",
        actor="bob",
        parent=parent,
        enabled=False,
    )
    db.add(
        "child-deleted",
        tenant="carol",
        actor="carol",
        parent=parent,
        deleted=True,
    )
    response = await BroadcastStore(db).submit(
        caller,
        created["job_id"],
        payload(targets=[]),
    )
    assert response["tenant_count"] == 1
    item = db.conn.execute(
        "SELECT * FROM swe_cron_managed_broadcast_items",
    ).fetchone()
    assert item["job_id"] == "child-one"
    assert item["operation"] == "retimed"
    assert not json.loads(item["plan_json"])["enabled"]


@pytest.mark.asyncio
async def test_result_pagination_keeps_global_counts_and_current_creation_order(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore

    db, caller, _ = distribution_setup
    created = await source(distribution_setup)
    store = BroadcastStore(db)
    first = await store.submit(caller, created["job_id"], payload())
    page = await store.get(
        caller,
        created["job_id"],
        first["task_id"],
        limit=1,
    )
    assert len(page["results"]) == 1
    assert page["pending_count"] == 2
    assert page["next_cursor"]
    second = await store.get(
        caller,
        created["job_id"],
        first["task_id"],
        limit=1,
        cursor=page["next_cursor"],
    )
    assert second["results"][0]["tenant_id"] != page["results"][0]["tenant_id"]
    assert second["next_cursor"] is None
    current = await store.current(caller, created["job_id"])
    assert current["task"]["task_id"] == first["task_id"]


@pytest.mark.asyncio
async def test_active_broadcast_blocks_source_delete_and_content_update(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )

    db, caller, management = distribution_setup
    created = await source(distribution_setup)
    await BroadcastStore(db).submit(caller, created["job_id"], payload())
    with pytest.raises(ManagementConflict, match="broadcast"):
        await management.delete(
            caller,
            created["job_id"],
            {
                "request_id": "delete-during-broadcast",
                "expected_definition_version": 1,
            },
        )
    assert (
        db.conn.execute("SELECT deleted_at FROM swe_cron_jobs_v2").fetchone()[
            0
        ]
        is None
    )
    with pytest.raises(ManagementConflict, match="broadcast"):
        await management.update(
            caller,
            created["job_id"],
            {
                "request_id": "update-during-broadcast",
                "expected_definition_version": 1,
                "spec": {"name": "changed"},
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 1000])
async def test_distribution_accepts_documented_target_count_boundaries(
    distribution_setup,
    count,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore

    _, caller, _ = distribution_setup
    created = await source(distribution_setup)
    targets = [{"tenant_id": f"user-{index}"} for index in range(count)]
    result = await BroadcastStore(distribution_setup[0]).submit(
        caller,
        created["job_id"],
        payload(targets=targets),
    )
    assert result["tenant_count"] == count


@pytest.mark.asyncio
async def test_distribution_rejects_over_limit_and_conflicting_duplicate_profiles(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore

    db, caller, _ = distribution_setup
    created = await source(distribution_setup)
    store = BroadcastStore(db)
    with pytest.raises(ValueError, match="target_count"):
        await store.submit(
            caller,
            created["job_id"],
            payload(
                targets=[
                    {"tenant_id": f"user-{index}"} for index in range(1001)
                ],
            ),
        )
    with pytest.raises(ValueError, match="duplicate"):
        await store.submit(
            caller,
            created["job_id"],
            payload(
                targets=[
                    {"tenant_id": "bob", "tenant_name": "甲"},
                    {"tenant_id": "bob", "tenant_name": "乙"},
                ],
            ),
        )
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_managed_broadcast_tasks",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_disabled_scatter_keeps_unsupported_cron_without_warning(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore

    db, caller, management = distribution_setup
    created = await source(distribution_setup)
    await management.update(
        caller,
        created["job_id"],
        {
            "request_id": "cron-change",
            "expected_definition_version": 1,
            "spec": {
                "name": "source report",
                "task_type": "text",
                "text": "report",
                "schedule": {
                    "cron": "*/5 * * * *",
                    "timezone": "Asia/Shanghai",
                },
            },
        },
    )
    command = {
        **payload(),
        "expected_definition_version": 2,
        "enable_offset": False,
        "offset_window_hours": 0,
    }
    await BroadcastStore(db).submit(caller, created["job_id"], command)
    rows = db.conn.execute(
        "SELECT * FROM swe_cron_managed_broadcast_items",
    ).fetchall()
    assert all(
        row["cron_expr"] == "*/5 * * * *" and row["warning"] is None
        for row in rows
    )


@pytest.mark.asyncio
async def test_current_uses_db_creation_order_when_instance_clock_moves_back(
    distribution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed import distribution

    db, caller, _ = distribution_setup
    created = await source(distribution_setup)
    store = distribution.BroadcastStore(db)
    first = await store.submit(caller, created["job_id"], payload())
    db.conn.execute(
        "UPDATE swe_cron_managed_broadcast_tasks SET active_key=NULL, status='succeeded', sync_status='completed'",
    )
    db.conn.execute(
        "UPDATE swe_cron_managed_broadcast_items SET status='succeeded'",
    )
    monkeypatch.setattr(distribution, "utc_now", lambda: datetime(2000, 1, 1))
    second = await store.submit(
        caller,
        created["job_id"],
        payload("second-round"),
    )
    current = await store.current(caller, created["job_id"])
    assert current["task"]["task_id"] == second["task_id"]
    assert current["task"]["task_id"] != first["task_id"]


@pytest.mark.asyncio
async def test_broadcast_lease_fences_late_worker_and_terminal_items_are_idempotent(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import (
        BroadcastLeaseLost,
        BroadcastStore,
    )

    db, caller, _ = distribution_setup
    created = await source(distribution_setup)
    store = BroadcastStore(db)
    accepted = await store.submit(caller, created["job_id"], payload())
    task_id = accepted["task_id"]
    assert await store.claim(task_id, "worker-one")
    assert await store.claim(task_id, "worker-two") is None
    db.conn.execute(
        "UPDATE swe_cron_managed_broadcast_tasks SET lease_until='2000-01-01 00:00:00'",
    )
    assert await store.claim(task_id, "worker-two")
    with pytest.raises(BroadcastLeaseLost):
        await store.record_item(task_id, "worker-one", 1, "succeeded", {})
    await store.record_item(task_id, "worker-two", 1, "succeeded", {})
    await store.record_item(
        task_id,
        "worker-two",
        1,
        "failed",
        {"error_code": "late"},
    )
    result = await store.get(caller, created["job_id"], task_id)
    assert result["success_count"] == 1
    assert result["failed_count"] == 0


@pytest.mark.asyncio
async def test_parent_timer_sync_failure_is_not_reported_as_whole_distribution_success(
    distribution_setup,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore

    db, caller, _ = distribution_setup
    created = await source(distribution_setup)
    store = BroadcastStore(db)
    accepted = await store.submit(caller, created["job_id"], payload())
    task_id = accepted["task_id"]
    await store.claim(task_id, "worker")
    for index in (1, 2):
        await store.record_item(task_id, "worker", index, "succeeded", {})
    await store.finish(task_id, "worker", error_code="batch_timer_sync_failed")
    result = await store.get(caller, created["job_id"], task_id)
    assert result["success_count"] == 2
    assert result["status"] == "failed"
    assert result["sync_status"] == "failed"
    assert result["error_code"] == "batch_timer_sync_failed"
    assert (
        db.conn.execute(
            "SELECT active_key FROM swe_cron_managed_broadcast_tasks",
        ).fetchone()[0]
        is None
    )
