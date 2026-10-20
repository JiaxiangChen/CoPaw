# -*- coding: utf-8 -*-
"""管理事务执行真实 SQL，仅在外部服务边界使用测试替身。"""

import asyncio
import copy
import json
from datetime import datetime
from contextlib import asynccontextmanager

import pytest

from tests.unit.scheduler.test_managed_cron_queries import QueryDatabase


class ManagementDatabase(QueryDatabase):
    def __init__(self):
        super().__init__()
        self.conn.isolation_level = None
        self.lock = asyncio.Lock()
        for table, columns in {
            "swe_cron_jobs_v2": (
                "original_cron_expr TEXT",
                "job_origin TEXT",
                "plan_id TEXT",
                "source_definition_version INTEGER DEFAULT 1",
                "created_at TEXT",
                "updated_at TEXT",
            ),
            "swe_cron_job_states": (
                "state_source TEXT",
                "management_updated_at TEXT",
                "created_at TEXT",
                "updated_at TEXT",
            ),
        }.items():
            for column in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        self.conn.executescript("""
            CREATE TABLE swe_cron_execution_runs (
                execution_id TEXT PRIMARY KEY, tenant_id TEXT, source_id TEXT,
                agent_id TEXT, job_id TEXT, status TEXT, settled_status TEXT
            );
            CREATE TABLE swe_cron_managed_broadcast_tasks (
                task_id TEXT PRIMARY KEY, active_key TEXT UNIQUE,
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                status TEXT
            );
            CREATE TABLE swe_cron_registration_journal (
                operation_id TEXT PRIMARY KEY, active_key TEXT UNIQUE,
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                actor_id TEXT, action TEXT, request_id TEXT, request_hash TEXT,
                expected_version INTEGER, payload_json TEXT, progress_json TEXT,
                status TEXT, stage TEXT, error_code TEXT,
                lease_owner TEXT, lease_until TEXT, version INTEGER DEFAULT 1,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE swe_cron_jobs (
                id TEXT PRIMARY KEY, name TEXT, tenant_id TEXT, source_id TEXT,
                tenant_name TEXT, bbk_id TEXT, enabled INTEGER, task_type TEXT,
                cron_expr TEXT, timezone TEXT, channel TEXT, target_user_id TEXT,
                target_session_id TEXT, timeout_seconds INTEGER,
                max_concurrency INTEGER, misfire_grace_seconds INTEGER,
                text_content TEXT, request_input TEXT, creator_user_id TEXT,
                task_chat_id TEXT, task_session_id TEXT, plan_id TEXT, meta TEXT,
                skill_ids TEXT, workflow_binding_id TEXT, job_origin TEXT,
                status TEXT, pause_reason TEXT, deleted_at TEXT,
                created_at TEXT, updated_at TEXT
            );
        """)

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.rollback()
                raise
            self.conn.commit()

    async def fetch_all(self, query, params=None):
        return await super().fetch_all(
            query.replace(" FOR UPDATE", ""),
            params,
        )

    async def execute(self, query, params=None):
        values = tuple(
            value.isoformat(" ") if isinstance(value, datetime) else value
            for value in (params or ())
        )
        cursor = self.conn.execute(query.replace("%s", "?"), values)
        return cursor.rowcount


class ResourceClient:
    def __init__(self):
        self.prepared = []
        self.cleaned = []
        self.fail_prepare = False

    async def prepare(self, operation, caller):
        await asyncio.sleep(0)
        if self.fail_prepare:
            raise RuntimeError("private token must not reach API errors")
        self.prepared.append(operation["job_id"])
        spec = copy.deepcopy(json.loads(operation["payload_json"])["spec"])
        spec["meta"]["task_chat_id"] = f"chat-{spec['id']}"
        spec["meta"]["task_session_id"] = f"cron-task:{spec['id']}"
        return {
            "spec": spec,
            "chat_id": spec["meta"]["task_chat_id"],
            "session_id": spec["meta"]["task_session_id"],
        }

    async def cleanup(self, operation, caller):
        self.cleaned.append(operation["job_id"])
        return {"cleaned": True}


class PlatformClient:
    def __init__(self):
        self.registered = {}
        self.calls = []
        self.fail_enable = False
        self.fail_stop = False
        self.lose_register_response = False

    async def register(self, operation, spec, mode):
        from scheduler.app.services.cron.managed.management import (
            RegistrationUnknown,
        )

        self.calls.append("register")
        external_id = f"timer-{operation['job_id']}"
        self.registered[operation["operation_id"]] = external_id
        if self.lose_register_response:
            raise RegistrationUnknown()
        return external_id

    async def find_registration(self, operation):
        self.calls.append("lookup")
        return self.registered.get(operation["operation_id"])

    async def update(self, external_id, operation, spec, mode):
        self.calls.append("update")

    async def set_enabled(self, external_id, enabled):
        self.calls.append("enable" if enabled else "pause")
        if self.fail_enable:
            raise RuntimeError("platform unavailable")

    async def stop(self, external_id, operation):
        self.calls.append("stop")
        if self.fail_stop:
            raise RuntimeError("platform unavailable")


@pytest.fixture
def setup():
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    db = ManagementDatabase()
    resources = ResourceClient()
    platform = PlatformClient()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    service = ManagementService(db, resources, platform)
    yield db, resources, platform, caller, service
    db.conn.close()


def create_payload(request_id="create-1", **spec):
    return {
        "request_id": request_id,
        "dispatch_state": "ordinary",
        "spec": {
            "name": "report",
            "task_type": "agent",
            "request": {"input": "generate report"},
            "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            **spec,
        },
    }


@pytest.mark.asyncio
async def test_projection_keeps_existing_model_and_skill_definition_fields(
    setup,
):
    db, _, _, caller, service = setup
    slot = {"provider_id": "provider", "model": "model"}
    result = await service.create(
        caller,
        create_payload(skill_ids="report-skill", model_slot=slot),
    )
    assert result["status"] == "completed"
    projected = row(db, "swe_cron_jobs")
    assert projected["skill_ids"] == "report-skill"
    assert json.loads(projected["meta"])["model_slot"] == slot


@pytest.mark.asyncio
async def test_ordinary_user_cannot_create_root_in_another_workspace(setup):
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.contracts import (
        ManagedCronNotFound,
    )

    db, resources, _, _, service = setup
    foreign = CallerIdentity("bob", "bank", "default", "alice")
    with pytest.raises(ManagedCronNotFound):
        await service.create(foreign, create_payload())
    assert not resources.prepared
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_jobs_v2").fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_delete_with_new_request_id_returns_existing_tombstone_operation(
    setup,
):
    db, resources, _, caller, service = setup
    created = await service.create(caller, create_payload())
    command = {"request_id": "delete-one", "expected_definition_version": 1}
    first = await service.delete(caller, created["job_id"], command)
    again = await service.delete(
        caller,
        created["job_id"],
        {**command, "request_id": "delete-two"},
    )
    assert first["operation_id"] == again["operation_id"]
    assert again["status"] == "completed"
    assert len(resources.cleaned) == 1
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_registration_journal",
        ).fetchone()[0]
        == 2
    )


@pytest.mark.asyncio
async def test_explicit_registration_rejection_allows_retry_without_preparing_again(
    setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.operations import (
        RegistrationRejected,
    )

    _, resources, platform, caller, service = setup
    original = platform.register
    rejected = True

    async def register(*args):
        nonlocal rejected
        if rejected:
            rejected = False
            raise RegistrationRejected()
        return await original(*args)

    monkeypatch.setattr(platform, "register", register)
    failed = await service.create(caller, create_payload())
    assert failed["status"] == "failed"
    done = await service.retry(
        caller,
        failed["job_id"],
        failed["operation_id"],
    )
    assert done["status"] == "completed"
    assert len(resources.prepared) == 1


@pytest.mark.asyncio
async def test_first_create_cannot_overwrite_existing_legacy_projection(
    setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.definition import stable_id

    db, _, platform, caller, service = setup
    job_id = stable_id(caller, "create-1", "job")

    async def legacy_write_during_registration(*_args):
        db.conn.execute(
            "INSERT INTO swe_cron_jobs (id,name,tenant_id,source_id,meta,status) VALUES (?,?,?,?,?,?)",
            (
                job_id,
                "legacy",
                "alice",
                "bank",
                '{"management_owner":"scheduler","agent_id":"default"}',
                "active",
            ),
        )

    monkeypatch.setattr(
        platform,
        "set_enabled",
        legacy_write_during_registration,
    )
    failed = await service.create(caller, create_payload())
    assert failed["status"] == "failed"
    assert row(db, "swe_cron_jobs")["name"] == "legacy"
    deleted = await service.delete(
        caller,
        job_id,
        {"request_id": "delete-collision", "expected_definition_version": 1},
    )
    assert deleted["status"] == "completed"
    assert row(db, "swe_cron_jobs")["status"] == "active"


@pytest.mark.asyncio
async def test_preexisting_legacy_id_conflict_is_rejected_before_external_calls(
    setup,
):
    from scheduler.app.services.cron.managed.definition import stable_id
    from scheduler.app.services.cron.managed.management import (
        ManagementConflict,
    )

    db, resources, platform, caller, service = setup
    job_id = stable_id(caller, "create-1", "job")
    db.conn.execute(
        "INSERT INTO swe_cron_jobs (id,name,tenant_id,source_id) VALUES (?,?,?,?)",
        (job_id, "legacy", "alice", "bank"),
    )
    with pytest.raises(ManagementConflict):
        await service.create(caller, create_payload())
    assert not resources.prepared
    assert not platform.calls


def row(db, table):
    return dict(db.conn.execute(f"SELECT * FROM {table}").fetchone())


@pytest.mark.asyncio
async def test_create_writes_ready_state_projection_and_durable_operation(
    setup,
):
    db, resources, platform, caller, service = setup
    result = await service.create(caller, create_payload())
    assert result["status"] == "completed"
    assert row(db, "swe_cron_jobs_v2")["management_owner"] == "scheduler"
    state = row(db, "swe_cron_job_states")
    assert state["management_status"] == "ready"
    assert state["task_chat_id"] == f"chat-{result['job_id']}"
    assert state["unread_execution_count"] == 0
    assert row(db, "swe_cron_jobs")["status"] == "active"
    assert row(db, "swe_cron_registration_journal")["active_key"] is None
    assert resources.prepared == [result["job_id"]]
    assert platform.calls == ["register", "enable"]


@pytest.mark.asyncio
async def test_duplicate_create_and_payload_conflict_do_not_create_resources(
    setup,
):
    from scheduler.app.services.cron.managed.management import (
        ManagementConflict,
    )

    db, resources, platform, caller, service = setup
    first = await service.create(caller, create_payload())
    second = await service.create(caller, create_payload())
    assert second["operation_id"] == first["operation_id"]
    assert len(resources.prepared) == platform.calls.count("register") == 1
    with pytest.raises(ManagementConflict):
        await service.create(caller, create_payload(name="different"))
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_jobs_v2").fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_prepare_failure_keeps_task_and_retry_reuses_identity(setup):
    db, resources, platform, caller, service = setup
    resources.fail_prepare = True
    failed = await service.create(caller, create_payload())
    assert failed["status"] == "failed"
    assert failed["error_code"] == "swe_prepare_failed"
    assert "private" not in json.dumps(failed)
    assert not platform.calls
    assert (
        row(db, "swe_cron_job_states")["management_status"] == "create_failed"
    )
    resources.fail_prepare = False
    result = await service.retry(
        caller,
        failed["job_id"],
        failed["operation_id"],
    )
    assert result["status"] == "completed"
    assert result["job_id"] == failed["job_id"]
    assert len(resources.prepared) == 1


@pytest.mark.asyncio
async def test_platform_enable_retry_does_not_register_or_prepare_twice(setup):
    db, resources, platform, caller, service = setup
    platform.fail_enable = True
    failed = await service.create(caller, create_payload())
    assert failed["status"] == "failed"
    assert row(db, "swe_cron_job_states")["ordinary_external_job_id"]
    assert row(db, "swe_cron_job_states")["task_chat_id"]
    platform.fail_enable = False
    result = await service.retry(
        caller,
        failed["job_id"],
        failed["operation_id"],
    )
    assert result["status"] == "completed"
    assert platform.calls.count("register") == len(resources.prepared) == 1


@pytest.mark.asyncio
async def test_unconfirmed_registration_is_failed_and_manual_retry_registers_again(
    setup,
):
    _, resources, platform, caller, service = setup
    platform.lose_register_response = True
    failed = await service.create(caller, create_payload())
    assert failed["stage"] == "platform"
    assert failed["error_code"] == "platform_registration_failed"
    platform.lose_register_response = False
    result = await service.retry(
        caller,
        failed["job_id"],
        failed["operation_id"],
    )
    assert result["status"] == "completed"
    assert platform.calls.count("register") == 2
    assert "lookup" not in platform.calls
    assert len(resources.prepared) == 1


@pytest.mark.asyncio
async def test_update_only_publishes_prepared_version_and_keeps_runtime(setup):
    db, resources, platform, caller, service = setup
    created = await service.create(caller, create_payload())
    db.conn.execute(
        "UPDATE swe_cron_job_states SET last_status='running', unread_execution_count=7",
    )
    resources.fail_prepare = True
    command = {
        "request_id": "edit-1",
        "expected_definition_version": 1,
        "spec": create_payload(name="new report")["spec"],
    }
    failed = await service.update(caller, created["job_id"], command)
    assert failed["status"] == "failed"
    assert row(db, "swe_cron_jobs_v2")["name"] == "report"
    assert row(db, "swe_cron_job_states")["unread_execution_count"] == 7
    resources.fail_prepare = False
    result = await service.retry(
        caller,
        created["job_id"],
        failed["operation_id"],
    )
    assert result["status"] == "completed"
    assert row(db, "swe_cron_jobs_v2")["definition_version"] == 2
    assert row(db, "swe_cron_jobs_v2")["name"] == "new report"
    state = row(db, "swe_cron_job_states")
    assert state["last_status"] == "running"
    assert state["unread_execution_count"] == 7
    assert platform.calls.count("register") == 1
    assert "update" in platform.calls


@pytest.mark.asyncio
async def test_update_source_with_children_and_legacy_takeover_are_rejected(
    setup,
):
    from scheduler.app.services.cron.managed.management import (
        ManagementConflict,
    )
    from scheduler.app.services.cron.managed.contracts import (
        ManagedCronNotFound,
    )

    db, _, _, caller, service = setup
    created = await service.create(caller, create_payload())
    db.add(
        "child",
        tenant="bob",
        parent=("alice", "bank", "default", created["job_id"]),
    )
    command = {
        "request_id": "edit-1",
        "expected_definition_version": 1,
        "spec": create_payload(name="changed")["spec"],
    }
    with pytest.raises(ManagementConflict):
        await service.update(caller, created["job_id"], command)
    db.add("legacy", owner="legacy")
    with pytest.raises(ManagedCronNotFound):
        await service.update(caller, "legacy", command)


@pytest.mark.asyncio
async def test_delete_tombstone_survives_platform_failure_then_cleans_once(
    setup,
):
    db, resources, platform, caller, service = setup
    created = await service.create(caller, create_payload())
    platform.fail_stop = True
    failed = await service.delete(
        caller,
        created["job_id"],
        {
            "request_id": "delete-1",
            "expected_definition_version": 1,
        },
    )
    assert failed["status"] == "failed"
    assert row(db, "swe_cron_jobs_v2")["deleted_at"]
    assert row(db, "swe_cron_jobs")["status"] == "deleted"
    assert not resources.cleaned
    platform.fail_stop = False
    done = await service.retry(
        caller,
        created["job_id"],
        failed["operation_id"],
    )
    assert done["status"] == "completed"
    assert resources.cleaned == [created["job_id"]]
    await service.retry(caller, created["job_id"], failed["operation_id"])
    assert len(resources.cleaned) == 1


@pytest.mark.asyncio
async def test_delete_defers_resource_cleanup_for_running_execution(setup):
    db, resources, _, caller, service = setup
    created = await service.create(caller, create_payload())
    db.conn.execute("UPDATE swe_cron_job_states SET last_status='running'")
    db.conn.execute(
        "INSERT INTO swe_cron_execution_runs (execution_id,tenant_id,source_id,agent_id,job_id,status) VALUES (?,?,?,?,?,?)",
        (
            "inflight",
            *caller.job_key(created["job_id"]),
            "running",
        ),
    )
    deleted = await service.delete(
        caller,
        created["job_id"],
        {
            "request_id": "delete-1",
            "expected_definition_version": 1,
        },
    )
    assert deleted["stage"] == "cleanup_waiting"
    assert not resources.cleaned
    db.conn.execute("UPDATE swe_cron_job_states SET last_status='success'")
    # 此管理单测模拟主执行及异步收敛均已结束，真实共库观察另有集成测试。
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='success', settled_status='success'",
    )
    done = await service.retry(
        caller,
        created["job_id"],
        deleted["operation_id"],
    )
    assert done["status"] == "completed"
    assert len(resources.cleaned) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_creates_share_persisted_identity(setup):
    db, resources, platform, caller, service = setup
    first, second = await asyncio.gather(
        service.create(caller, create_payload()),
        service.create(caller, create_payload()),
    )
    assert first["operation_id"] == second["operation_id"]
    assert len(resources.prepared) == platform.calls.count("register") == 1
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_jobs_v2").fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_retry_persists_binding_if_previous_checkpoint_preceded_binding_failure(
    setup,
    monkeypatch,
):
    db, _, platform, caller, service = setup
    original = service.store.save_binding
    calls = 0

    async def temporary_failure(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary binding write failure")
        return await original(*args)

    monkeypatch.setattr(service.store, "save_binding", temporary_failure)
    failed = await service.create(caller, create_payload())
    assert failed["status"] == "failed"
    done = await service.retry(
        caller,
        failed["job_id"],
        failed["operation_id"],
    )
    assert done["status"] == "completed"
    assert row(db, "swe_cron_job_states")["ordinary_external_job_id"]
    assert platform.calls.count("register") == 1


@pytest.mark.asyncio
async def test_transaction_deadlock_is_retried_without_duplicate_creation(
    setup,
    monkeypatch,
):
    db, resources, platform, caller, service = setup
    original = db.fetch_one
    calls = 0

    async def transient(query, params=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(1213, "deadlock")
        return await original(query, params)

    monkeypatch.setattr(db, "fetch_one", transient)
    result = await service.create(caller, create_payload())
    assert result["status"] == "completed"
    assert len(resources.prepared) == platform.calls.count("register") == 1


@pytest.mark.asyncio
async def test_failed_creation_can_be_deleted_without_resuming_old_operation(
    setup,
):
    db, resources, _, caller, service = setup
    resources.fail_prepare = True
    failed = await service.create(caller, create_payload())
    deleted = await service.delete(
        caller,
        failed["job_id"],
        {
            "request_id": "delete-failed",
            "expected_definition_version": 1,
        },
    )
    assert deleted["status"] == "completed"
    assert row(db, "swe_cron_jobs_v2")["deleted_at"]
    resources.fail_prepare = False
    old = await service.retry(caller, failed["job_id"], failed["operation_id"])
    assert old["status"] == "superseded"
    assert not resources.prepared


@pytest.mark.asyncio
async def test_source_disabled_does_not_close_batch_enabled_entrance(setup):
    _, _, platform, caller, service = setup
    payload = {
        **create_payload(enabled=False),
        "dispatch_state": "batch_enabled",
    }
    result = await service.create(caller, payload)
    assert result["status"] == "completed"
    assert platform.calls == ["register", "enable"]


@pytest.mark.asyncio
async def test_compatibility_projection_json_remains_valid_when_content_is_large(
    setup,
):
    db, _, _, caller, service = setup
    payload = create_payload(
        request={"input": "x" * 10000},
        meta={"user_note": "z" * 10000},
    )
    result = await service.create(caller, payload)
    assert result["status"] == "completed"
    projected = row(db, "swe_cron_jobs")
    assert len(projected["request_input"]) <= 4096
    assert json.loads(projected["request_input"])["_truncated"] is True
    meta = json.loads(projected["meta"])
    assert len(projected["meta"]) <= 4096
    assert meta["management_owner"] == "scheduler"
    assert meta["task_chat_id"]
    assert (
        json.loads(row(db, "swe_cron_jobs_v2")["spec_json"])["request"][
            "input"
        ]
        == "x" * 10000
    )


@pytest.mark.asyncio
async def test_business_input_keys_are_not_removed_by_metadata_redaction(
    setup,
):
    db, _, _, caller, service = setup
    value = {"password": "business-field", "token": "business-field"}
    result = await service.create(
        caller,
        create_payload(request={"input": value}),
    )
    assert result["status"] == "completed"
    assert (
        json.loads(row(db, "swe_cron_jobs_v2")["spec_json"])["request"][
            "input"
        ]
        == value
    )
