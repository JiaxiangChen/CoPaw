# -*- coding: utf-8 -*-
"""显式选择的隔离MySQL验证：不读应用连接配置、不建表、不连接默认库。"""

import asyncio
import json
import os
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.unit.scheduler.test_managed_cron_management import (
    PlatformClient,
    ResourceClient,
    create_payload,
)

SOURCE = "test-cron-source"


def validated_settings(raw):
    """拒绝默认业务库和不完整配置，异常不包含原始连接信息。"""
    try:
        settings = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("explicit_test_mysql_configuration_required") from exc
    if not isinstance(settings, dict) or not all(
        isinstance(settings.get(key), str) and bool(settings[key])
        for key in ("host", "user", "database")
    ):
        raise ValueError("explicit_test_mysql_configuration_required")
    if not re.fullmatch(
        r"test_cron_managed_[A-Za-z0-9_]+",
        settings["database"],
    ):
        raise ValueError("isolated_managed_cron_test_database_required")
    return settings


async def cleanup(db, tenant):
    """只清除本fixture预留的随机身份，不清表、不按应用配置推断目标。"""
    assert re.fullmatch(r"cron_it_[0-9a-f]{32}", tenant)
    key = (tenant, SOURCE)
    await db.execute(
        "DELETE FROM swe_cron_managed_mode_items WHERE operation_id IN (SELECT operation_id FROM swe_cron_registration_journal WHERE tenant_id=%s AND source_id=%s)",
        key,
    )
    await db.execute(
        "DELETE FROM swe_cron_managed_broadcast_items WHERE task_id IN (SELECT task_id FROM swe_cron_managed_broadcast_tasks WHERE tenant_id=%s AND source_id=%s)",
        key,
    )
    await db.execute(
        "DELETE FROM swe_cron_executions WHERE tenant_id=%s",
        (tenant,),
    )
    for table in (
        "swe_cron_managed_dispatch_events",
        "swe_cron_managed_batch_members",
        "swe_cron_managed_trigger_receipts",
        "swe_cron_managed_mode_items",
        "swe_cron_managed_dispatch_controls",
        "swe_cron_managed_broadcast_tasks",
        "swe_cron_execution_runs",
        "swe_cron_registration_journal",
        "swe_cron_job_states",
        "swe_cron_jobs",
        "swe_cron_jobs_v2",
    ):
        await db.execute(
            f"DELETE FROM {table} WHERE tenant_id=%s AND source_id=%s",
            key,
        )


@pytest.fixture
async def mysql_pair():
    raw = os.environ.get("CRON_MANAGED_TEST_MYSQL_JSON")
    if not raw:
        pytest.skip("需要显式配置已人工建表的隔离MySQL测试库")
    settings = validated_settings(raw)
    from scheduler.app.database.connection import (
        DatabaseConnection as SchedulerConnection,
    )
    from scheduler.config.constant import SchedulerDatabaseConfig
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.schema import (
        assert_managed_schema_ready,
    )
    from swe.database.config import DatabaseConfig
    from swe.database.connection import DatabaseConnection as SweConnection

    scheduler = SchedulerConnection(SchedulerDatabaseConfig(**settings))
    swe = SweConnection(DatabaseConfig(**settings))
    tenant, ready = "cron_it_" + uuid4().hex, False
    try:
        await scheduler.connect()
        await swe.connect()
        await assert_managed_schema_ready(scheduler)
        from scheduler.app.services.cron.managed.board_schema import (
            assert_managed_board_schema_ready,
        )

        await assert_managed_board_schema_ready(scheduler)
        ready = True
        yield SimpleNamespace(
            scheduler=scheduler,
            swe=swe,
            caller=CallerIdentity(tenant, SOURCE, "default", tenant),
        )
    finally:
        try:
            if ready:
                await cleanup(scheduler, tenant)
        finally:
            await swe.close()
            await scheduler.close()


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        "{}",
        '{"host":"localhost","user":"u","database":"business","password":"secret-not-to-log"}',
    ],
)
def test_database_gate_rejects_missing_or_nonisolated_configuration(raw):
    with pytest.raises(ValueError) as error:
        validated_settings(raw)
    assert "secret-not-to-log" not in str(error.value)


@pytest.mark.asyncio
async def test_mysql_concurrent_create_keeps_one_operation_and_binding(
    mysql_pair,
):
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    db, caller = mysql_pair.scheduler, mysql_pair.caller
    platform, resources = PlatformClient(), ResourceClient()
    service = ManagementService(db, resources, platform)
    first, second = await asyncio.gather(
        service.create(caller, create_payload()),
        service.create(caller, create_payload()),
    )
    assert first["operation_id"] == second["operation_id"]
    assert first["job_id"] == second["job_id"]
    row = await mysql_pair.swe.fetch_one(
        "SELECT COUNT(*) AS count FROM swe_cron_jobs_v2 WHERE tenant_id=%s AND source_id=%s",
        (caller.tenant_id, SOURCE),
    )
    assert row["count"] == 1
    assert platform.calls.count("register") == 1
    operation = await service.store.get(
        caller,
        first["job_id"],
        first["operation_id"],
    )
    assert operation["status"] == "completed"


@pytest.mark.asyncio
async def test_mysql_transaction_rolls_back_definition_and_state_together(
    mysql_pair,
):
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from scheduler.app.services.cron.managed.storage import transaction

    db, caller = mysql_pair.scheduler, mysql_pair.caller
    created = await ManagementService(
        db,
        ResourceClient(),
        PlatformClient(),
    ).create(caller, create_payload())
    key = caller.job_key(created["job_id"])
    where = "tenant_id=%s AND source_id=%s AND agent_id=%s AND job_id=%s"
    with pytest.raises(RuntimeError, match="injected_rollback"):
        async with transaction(db) as tx:
            await tx.execute(
                f"UPDATE swe_cron_jobs_v2 SET name='must-rollback' WHERE {where}",
                key,
            )
            await tx.execute(
                f"UPDATE swe_cron_job_states SET unread_execution_count=99 WHERE {where}",
                key,
            )
            raise RuntimeError("injected_rollback")
    definition = await mysql_pair.swe.fetch_one(
        f"SELECT name FROM swe_cron_jobs_v2 WHERE {where}",
        key,
    )
    state = await mysql_pair.swe.fetch_one(
        f"SELECT unread_execution_count FROM swe_cron_job_states WHERE {where}",
        key,
    )
    assert definition["name"] == create_payload()["spec"]["name"]
    assert state["unread_execution_count"] == 0


@pytest.mark.asyncio
async def test_mysql_swe_accept_and_complete_use_shared_id_and_one_history(
    mysql_pair,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from scheduler.app.services.cron.managed.executions import (
        ExecutionStore,
        ExecutionRequest,
    )
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db, caller = mysql_pair.scheduler, mysql_pair.caller
    created = await ManagementService(
        db,
        ResourceClient(),
        PlatformClient(),
    ).create(caller, create_payload())
    row, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest("manual", "mysql-manual-click"),
    )
    key = caller.job_key(created["job_id"])
    claimed = await ExecutionDispatcher(db, None)._claim(row, True)
    writer = ExecutionWriter(mysql_pair.swe)
    await writer.accept(
        row["execution_id"],
        key,
        dispatch_token=claimed["dispatch_owner"],
    )
    starts = await asyncio.gather(
        writer.start(row["execution_id"], key, "mysql-worker-a"),
        writer.start(row["execution_id"], key, "mysql-worker-b"),
    )
    winners = [item for item in starts if item is not None]
    assert len(winners) == 1
    owner = winners[0]["run_owner"]
    outcome = ExecutionOutcome("success", preview="mysql-test-result")
    await writer.complete(row["execution_id"], key, owner, outcome)
    await writer.complete(row["execution_id"], key, owner, outcome)
    final = await ExecutionStore(db).get(
        caller,
        created["job_id"],
        row["execution_id"],
    )
    assert final["status"] == "success"
    assert final["history_execution_id"] is not None
    history = await db.fetch_one(
        "SELECT COUNT(*) AS count FROM swe_cron_executions WHERE job_id=%s AND tenant_id=%s",
        (created["job_id"], caller.tenant_id),
    )
    assert history["count"] == 1


@pytest.mark.asyncio
async def test_mysql_broadcast_mode_callback_and_queue_survive_mode_change(
    mysql_pair,
):
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from scheduler.app.services.cron.managed.work_query import ManagedWorkQuery

    db, caller = mysql_pair.scheduler, mysql_pair.caller
    platform, resources = PlatformClient(), ResourceClient()
    payload = {**create_payload(), "dispatch_state": "batch_paused"}
    created = await ManagementService(db, resources, platform).create(
        caller,
        payload,
    )
    job_id = created["job_id"]
    task = await BroadcastStore(db).submit(
        caller,
        job_id,
        {
            "request_id": "mysql-broadcast",
            "expected_definition_version": 1,
            "targets": [
                {"tenant_id": caller.tenant_id, "agent_id": "recipient-a"},
                {"tenant_id": caller.tenant_id, "agent_id": "recipient-b"},
            ],
            "enable_offset": False,
            "offset_window_hours": 0,
        },
    )
    await BroadcastProcessor(db, resources, platform).process(task["task_id"])
    assert (await BroadcastStore(db).get(caller, job_id, task["task_id"]))[
        "success_count"
    ] == 2
    modes = DispatchStateService(db, platform)
    changed = await modes.set(
        caller,
        job_id,
        {
            "request_id": "mysql-enable-batch",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    await modes.process(changed["operation_id"])
    assert (await modes.get(caller, job_id))["sync_status"] == "completed"
    binding = await db.fetch_one(
        "SELECT batch_external_job_id FROM swe_cron_job_states WHERE tenant_id=%s AND source_id=%s AND agent_id=%s AND job_id=%s",
        caller.job_key(job_id),
    )
    scheduling = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    command = {
        "job_id": job_id,
        "external_job_id": binding["batch_external_job_id"],
        "operation_id": changed["operation_id"],
        "callback_protocol": "scheduler_managed_v1",
        "trigger_id": "mysql-period-1",
        "scheduled_fire_at": "2026-10-16T09:00:00+08:00",
    }
    accepted = await scheduling.accept(caller, command, "batch")
    assert accepted["enqueued_intents"] == 3
    current = await modes.get(caller, job_id)
    ordinary = await modes.set(
        caller,
        job_id,
        {
            "request_id": "mysql-go-ordinary",
            "state": "ordinary",
            "expected_version": current["version"],
        },
    )
    await modes.process(ordinary["operation_id"])
    assert (await modes.get(caller, job_id))["effective_state"] == "ordinary"
    replay = await scheduling.accept(caller, command, "batch")
    assert not replay["reused"]
    assert not replay["accepted"]
    assert replay["status"] == "skipped"
    assert "batch_id" not in replay
    rows = await ManagedWorkQuery(db, 10).rows(
        running=False,
        scope={
            "source_id": SOURCE,
            "provider_id": "default",
            "model_id": "default",
        },
    )
    assert len(rows) == 3, "模式切换不取消已经入队的批工作"


@pytest.mark.asyncio
async def test_mysql_legacy_snapshot_coexists_without_scheduler_takeover(
    mysql_pair,
):
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.contracts import QueryOptions
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )
    from swe.app.crons.migration_models import SnapshotCandidate
    from swe.app.crons.migration_repository import (
        DEFINITION_FIELDS,
        MigrationRepository,
    )

    db, caller = mysql_pair.scheduler, mysql_pair.caller
    created = await ManagementService(
        db,
        ResourceClient(),
        PlatformClient(),
    ).create(caller, create_payload())
    row = await db.fetch_one(
        "SELECT * FROM swe_cron_jobs_v2 WHERE tenant_id=%s AND source_id=%s AND agent_id=%s AND job_id=%s",
        caller.job_key(created["job_id"]),
    )
    definition = {name: row[name] for name in DEFINITION_FIELDS}
    spec = json.loads(definition["spec_json"])
    spec["id"] = "legacy-snapshot"
    definition.update(
        job_id=spec["id"],
        spec_json=spec,
        management_owner="legacy",
        source_definition_version=1,
    )
    snapshot = SnapshotCandidate(
        definition=definition,
        state_patch={
            "last_status": "success",
            "unread_execution_count": 2,
            "state_source": "memory_snapshot",
        },
        observed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        source_key="isolated-test/workspaces/default/jobs.json",
        fingerprint="a" * 64,
        file_version=1,
        memory_available=True,
    )
    importer = MigrationRepository(mysql_pair.swe)
    await importer.check_schema()
    assert (await importer.import_snapshot(snapshot)).status == "inserted"
    assert (await importer.import_snapshot(snapshot)).status == "unchanged"
    page = await ManagedCronRepository(db).page(caller, QueryOptions())
    assert [item["id"] for item in page["items"]] == [created["job_id"]]
    old = await db.fetch_one(
        "SELECT management_owner FROM swe_cron_jobs_v2 WHERE tenant_id=%s AND source_id=%s AND job_id='legacy-snapshot'",
        (caller.tenant_id, SOURCE),
    )
    assert old["management_owner"] == "legacy"
    conflict = SnapshotCandidate(
        **{
            **snapshot.__dict__,
            "definition": {**definition, "job_id": created["job_id"]},
        },
    )
    outcome = await importer.import_snapshot(conflict)
    assert outcome.status == "conflict"
    assert outcome.reason == "scheduler_owned"


async def _capacity_runs(db, caller, provider):
    """固定两个不同任务的模型池，避免同任务并发限制掩盖池内竞争。"""
    from scheduler.app.services.cron.managed.executions import (
        ExecutionStore,
        ExecutionRequest,
    )
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    management = ManagementService(db, ResourceClient(), PlatformClient())
    runs = []
    for index in range(2):
        payload = create_payload(f"capacity-{index}")
        payload["spec"]["model_slot"] = {
            "provider_id": provider,
            "model": "model",
        }
        created = await management.create(caller, payload)
        row, _ = await ExecutionStore(db).enqueue(
            caller,
            created["job_id"],
            ExecutionRequest("manual", f"claim-{index}"),
        )
        runs.append(row)
    return runs


@pytest.mark.asyncio
async def test_mysql_capacity_serializes_claims_and_waits_for_async_settlement(
    mysql_pair,
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from scheduler.app.services.cron import capacity_claim
    from scheduler.app.services.cron.managed.capacity import (
        CapacityPermit,
        ManagedCapacityGate,
    )
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from scheduler.app.services.cron.managed.executions import ExecutionStore
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )
    from swe.app.crons.managed_results import ExecutionWriter, ExecutionOutcome

    db, caller = mysql_pair.scheduler, mysql_pair.caller
    provider = "pool-" + caller.tenant_id
    scope = (SOURCE, provider, "model")
    owner = caller.tenant_id
    runs = await _capacity_runs(db, caller, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    beijing = now + timedelta(hours=8)
    permit = CapacityPermit(
        scope,
        owner,
        {
            "strategy_id": "mysql-test",
            "effective_workers": 1,
            "min_workers": 1,
            "max_workers": 1,
        },
    )
    await db.execute(
        "INSERT INTO swe_cron_dispatch_scope_leases (source_id,provider_id,model_id,lock_owner,lease_expires_at) VALUES (%s,%s,%s,%s,%s)",
        (*scope, owner, beijing + timedelta(minutes=5)),
    )
    monkeypatch.setattr(capacity_claim, "MANAGED_CAPACITY_ACCOUNTING", True)
    gate = ManagedCapacityGate(
        SimpleNamespace(_worker_id=owner),
        clock=lambda: now,
    )
    dispatcher = ExecutionDispatcher(
        db,
        None,
        clock=lambda: now,
        capacity=gate,
    )
    try:
        await db.execute(
            "INSERT INTO swe_cron_dispatch_intents (batch_id,intent_role,status,source_id,provider_id,model_id,tenant_id,job_id,due_at) VALUES (%s,'parent','dispatched',%s,%s,%s,%s,'legacy-intent',%s)",
            (owner, *scope, caller.tenant_id, beijing),
        )
        assert (
            await dispatcher._claim(runs[0], True, capacity_permit=permit)
            is None
        )
        await db.execute(
            "UPDATE swe_cron_dispatch_intents SET status='completed' WHERE tenant_id=%s AND source_id=%s AND provider_id=%s AND model_id=%s",
            (caller.tenant_id, *scope),
        )
        attempts, both_attempted = [], asyncio.Event()
        original_lock = gate.lock

        async def overlap_scope_claims(tx, value):
            attempts.append(True)
            if len(attempts) == 2:
                both_attempted.set()
            locked = await original_lock(tx, value)
            await asyncio.wait_for(both_attempted.wait(), timeout=5)
            return locked

        monkeypatch.setattr(gate, "lock", overlap_scope_claims)
        claimed = await asyncio.gather(
            *(
                dispatcher._claim(row, True, capacity_permit=permit)
                for row in runs
            ),
        )
        winners = [row for row in claimed if row is not None]
        assert len(winners) == 1
        winner = winners[0]
        waiting = next(
            row
            for row in runs
            if row["execution_id"] != winner["execution_id"]
        )
        writer = ExecutionWriter(mysql_pair.swe)
        key = caller.job_key(winner["job_id"])
        await writer.accept(
            winner["execution_id"],
            key,
            dispatch_token=winner["dispatch_owner"],
        )
        await writer.start(winner["execution_id"], key, "mysql-runner")
        await writer.complete(
            winner["execution_id"],
            key,
            "mysql-runner",
            ExecutionOutcome("success", preview="primary-done"),
        )
        assert (
            await dispatcher._claim(waiting, True, capacity_permit=permit)
            is None
        )
        completed = await ExecutionStore(db).get(
            caller,
            winner["job_id"],
            winner["execution_id"],
        )
        assert completed["settled_status"] is None
        # 这里只提供Monitor已结束的事实；实际占位释放仍通过新收敛器完成。
        await mysql_pair.swe.execute(
            "UPDATE swe_cron_executions SET async_status='success' WHERE id=%s AND tenant_id=%s",
            (completed["history_execution_id"], caller.tenant_id),
        )
        assert await ExecutionSettlement(db).tick() == 1
        assert (
            await dispatcher._claim(waiting, True, capacity_permit=permit)
            is not None
        )
    finally:
        await db.execute(
            "DELETE FROM swe_cron_dispatch_intents WHERE tenant_id=%s AND source_id=%s AND provider_id=%s AND model_id=%s",
            (caller.tenant_id, *scope),
        )
        await db.execute(
            "DELETE FROM swe_cron_dispatch_scope_leases WHERE source_id=%s AND provider_id=%s AND model_id=%s AND lock_owner=%s",
            (*scope, owner),
        )
