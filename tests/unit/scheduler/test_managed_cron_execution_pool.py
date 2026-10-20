# -*- coding: utf-8 -*-
"""执行快照固定权威模型池，不信任传输元数据声明的 provider/model。"""

import json

import pytest

from tests.unit.scheduler.test_managed_cron_executions import execution_setup
from tests.unit.scheduler.test_managed_cron_management import create_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slot,expected",
    [
        (None, ("default", "default")),
        (
            {"provider_id": "provider-a", "model": "model-a"},
            ("provider-a", "model-a"),
        ),
    ],
)
async def test_frozen_pool_comes_from_persisted_spec_not_transport_fields(
    execution_setup,
    slot,
    expected,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, management = execution_setup
    payload = create_payload()
    payload["spec"]["model_slot"] = slot
    created = await management.create(caller, payload)
    command = ExecutionRequest(
        "manual",
        "click",
        dispatch_meta={
            "provider_id": "forged-provider",
            "model_id": "forged-model",
        },
    )
    row, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        command,
    )
    assert (row["provider_id"], row["model_id"]) == expected
    meta = json.loads(row["dispatch_meta_json"])
    assert (meta["provider_id"], meta["model_id"]) == expected
    stored = db.conn.execute(
        "SELECT provider_id, model_id FROM swe_cron_execution_runs",
    ).fetchone()
    assert tuple(stored) == expected


@pytest.mark.asyncio
async def test_workflow_pool_comes_from_frozen_published_binding(
    execution_setup,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, management = execution_setup
    db.conn.executescript("""
        CREATE TABLE swe_workflow_bindings (
            binding_id TEXT, source_id TEXT, current_version INTEGER, enabled INTEGER
        );
        CREATE TABLE swe_workflow_binding_versions (
            binding_id TEXT, version INTEGER, config_json TEXT
        );
        INSERT INTO swe_workflow_bindings VALUES ('binding','bank',3,1);
        INSERT INTO swe_workflow_binding_versions VALUES ('binding',3,
            '{"provider_id":"published-provider","model_id":"published-model"}');
    """)
    payload = create_payload()
    payload["spec"] = {
        "name": "workflow",
        "task_type": "workflow",
        "workflow_binding_id": "binding",
        "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
        "model_slot": {"provider_id": "ignored", "model": "ignored"},
    }
    created = await management.create(caller, payload)
    row, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest("manual", "workflow-click"),
    )
    assert (row["provider_id"], row["model_id"]) == (
        "published-provider",
        "published-model",
    )
    assert (
        json.loads(row["dispatch_meta_json"])["workflow_config_version"] == 3
    )
