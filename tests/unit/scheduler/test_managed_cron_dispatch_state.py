# -*- coding: utf-8 -*-
"""验证三态目标、逐对象生效方式及失败后的原操作续办。"""

import pytest

from tests.unit.scheduler.test_managed_cron_distribution import (
    DistributionDatabase,
    payload,
)
from tests.unit.scheduler.test_managed_cron_management import (
    PlatformClient,
    ResourceClient,
)


class ModeDatabase(DistributionDatabase):
    def __init__(self):
        super().__init__()
        self.conn.executescript("""
            CREATE TABLE swe_cron_managed_dispatch_controls (
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                state TEXT, version INTEGER, operation_id TEXT,
                sync_status TEXT, error_code TEXT,
                PRIMARY KEY (tenant_id, source_id, agent_id, job_id)
            );
            CREATE TABLE swe_cron_managed_mode_items (
                operation_id TEXT, index_no INTEGER,
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                expected_definition_version INTEGER, sync_operation_id TEXT,
                status TEXT, error_code TEXT,
                PRIMARY KEY (operation_id, index_no)
            );
        """)


class ModePlatform(PlatformClient):
    def __init__(self):
        super().__init__()
        self.trace = []
        self.fail_target = None

    async def register(self, operation, spec, mode):
        await super().register(operation, spec, mode)
        external_id = f"timer-{operation['job_id']}-{mode}"
        self.registered[operation["operation_id"]] = external_id
        return external_id

    async def set_enabled(self, external_id, enabled):
        self.trace.append((external_id, enabled))
        if external_id == self.fail_target:
            raise RuntimeError("private-platform-error")
        await super().set_enabled(external_id, enabled)


@pytest.fixture
def mode_setup():
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    db = ModeDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    service = ManagementService(db, ResourceClient(), ModePlatform())
    yield db, caller, service
    db.conn.close()


async def group(mode_setup):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )

    db, caller, management = mode_setup
    created = await management.create(
        caller,
        {
            "request_id": "ordinary-source",
            "spec": {
                "name": "report",
                "task_type": "text",
                "text": "body",
                "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            },
        },
    )
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
    return created["job_id"]


@pytest.mark.asyncio
async def test_mode_switch_is_async_preserves_disabled_and_does_not_prepare_resources(
    mode_setup,
):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )

    db, caller, management = mode_setup
    job_id = await group(mode_setup)
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET enabled=0 WHERE tenant_id='bob'",
    )
    before = len(management.resources.prepared)
    service = DispatchStateService(db, management.platform)
    initial = await service.get(caller, job_id)
    accepted = await service.set(
        caller,
        job_id,
        {
            "request_id": "to-batch",
            "state": "batch_enabled",
            "expected_version": initial["version"],
        },
    )
    assert accepted["sync_status"] == "pending"
    assert accepted["pending_count"] == 3
    await service.process(accepted["operation_id"])
    done = await service.get(caller, job_id)
    assert done["sync_status"] == "completed"
    assert done["effective_state"] == "batch_enabled"
    assert (
        db.conn.execute(
            "SELECT enabled FROM swe_cron_jobs_v2 WHERE tenant_id='bob'",
        ).fetchone()[0]
        == 0
    )
    assert len(management.resources.prepared) == before


@pytest.mark.asyncio
async def test_failed_switch_does_not_block_triggers_or_allow_another_target(
    mode_setup,
):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.operations import (
        ManagementConflict,
    )

    db, caller, management = mode_setup
    job_id = await group(mode_setup)
    service = DispatchStateService(db, management.platform)
    accepted = await service.set(
        caller,
        job_id,
        {
            "request_id": "to-batch",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    management.platform.fail_enable = True
    await service.process(accepted["operation_id"])
    failed = await service.get(caller, job_id)
    assert failed["sync_status"] == "failed"
    assert all(
        row[0] == "ready"
        for row in db.conn.execute(
            "SELECT management_status FROM swe_cron_job_states",
        ).fetchall()
    )
    with pytest.raises(ManagementConflict, match="progress"):
        await service.set(
            caller,
            job_id,
            {
                "request_id": "change-target",
                "state": "ordinary",
                "expected_version": failed["version"],
            },
        )
    management.platform.fail_enable = False
    await service.retry(caller, job_id, accepted["operation_id"])
    assert (await service.get(caller, job_id))["sync_status"] == "completed"


@pytest.mark.asyncio
async def test_batch_to_ordinary_registers_missing_child_normal_timers_and_keeps_queued_runs(
    mode_setup,
):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )

    db, caller, management = mode_setup
    job_id = await group(mode_setup)
    service = DispatchStateService(db, management.platform)
    first = await service.set(
        caller,
        job_id,
        {
            "request_id": "batch",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    await service.process(first["operation_id"])
    normal_ids = [
        row[0]
        for row in db.conn.execute(
            "SELECT ordinary_external_job_id FROM swe_cron_job_states",
        ).fetchall()
    ]
    second = await service.set(
        caller,
        job_id,
        {"request_id": "ordinary", "state": "ordinary", "expected_version": 2},
    )
    await service.process(second["operation_id"])
    assert (await service.get(caller, job_id))["effective_state"] == "ordinary"
    assert [
        row[0]
        for row in db.conn.execute(
            "SELECT ordinary_external_job_id FROM swe_cron_job_states",
        ).fetchall()
    ] == normal_ids


@pytest.mark.asyncio
async def test_expired_mode_worker_cannot_publish_after_root_lease_transfer(
    mode_setup,
):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.operations import (
        LeaseLost,
        ManagementStore,
    )

    db, caller, management = mode_setup
    job_id = await group(mode_setup)
    service = DispatchStateService(db, management.platform)
    accepted = await service.set(
        caller,
        job_id,
        {
            "request_id": "batch",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    old = await ManagementStore(db).claim(
        caller,
        job_id,
        accepted["operation_id"],
    )
    db.conn.execute(
        "UPDATE swe_cron_registration_journal SET lease_until='2000-01-01 00:00:00' WHERE operation_id=?",
        (accepted["operation_id"],),
    )
    await ManagementStore(db).claim(caller, job_id, accepted["operation_id"])
    item = dict(
        db.conn.execute(
            "SELECT * FROM swe_cron_managed_mode_items ORDER BY index_no LIMIT 1",
        ).fetchone(),
    )
    before = list(management.platform.calls)
    with pytest.raises(LeaseLost):
        await service._one(old, item, "batch_enabled")
    assert management.platform.calls == before


@pytest.mark.asyncio
async def test_partial_switch_keeps_unsynced_object_in_old_mode_and_retry_skips_successes(
    mode_setup,
):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )

    db, caller, management = mode_setup
    job_id = await group(mode_setup)
    service = DispatchStateService(db, management.platform)
    rows = {
        row["tenant_id"]: dict(row)
        for row in db.conn.execute(
            "SELECT * FROM swe_cron_job_states",
        ).fetchall()
    }
    management.platform.fail_target = rows["carol"]["ordinary_external_job_id"]
    accepted = await service.set(
        caller,
        job_id,
        {
            "request_id": "batch",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    await service.process(accepted["operation_id"])
    partial = await service.get(caller, job_id)
    assert partial["effective_state"] == "mixed"
    assert partial["failed_count"] == 1
    assert (
        db.conn.execute(
            "SELECT dispatch_state FROM swe_cron_job_states WHERE tenant_id='carol'",
        ).fetchone()[0]
        == "ordinary"
    )
    completed_timer = rows["bob"]["ordinary_external_job_id"]
    completed_calls = sum(
        timer == completed_timer for timer, _ in management.platform.trace
    )
    management.platform.fail_target = None
    await service.retry(caller, job_id, accepted["operation_id"])
    assert (await service.get(caller, job_id))["sync_status"] == "completed"
    assert (
        sum(timer == completed_timer for timer, _ in management.platform.trace)
        == completed_calls
    )
