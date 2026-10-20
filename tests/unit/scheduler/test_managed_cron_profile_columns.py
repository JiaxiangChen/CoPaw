# -*- coding: utf-8 -*-
"""四个查询副本随完整定义发布，失败更新不得提前污染。"""

import json
from dataclasses import replace

import pytest

from tests.unit.scheduler.test_managed_cron_management import (
    create_payload,
    row,
    setup,
)
from tests.unit.scheduler.test_managed_cron_distribution import (
    distribution_setup,
    payload,
)

FIELDS = ("tenant_name", "bbk_id", "skill_ids", "workflow_binding_id")


def assert_profile(db, expected):
    definition = row(db, "swe_cron_jobs_v2")
    spec = json.loads(definition["spec_json"])
    for field, value in expected.items():
        assert definition[field] == value
        assert (
            spec.get(field) or (None if field == FIELDS[-1] else "")
        ) == value


@pytest.mark.asyncio
async def test_create_and_update_keep_four_columns_with_their_spec(setup):
    db, _, _, caller, service = setup
    caller = replace(caller, user_name="张三", bbk_id="100")
    payload = create_payload(
        task_type="workflow",
        skill_ids="skill-a",
        workflow_binding_id="bind-a",
    )
    created = await service.create(caller, payload)
    assert created["status"] == "completed"
    assert_profile(db, dict(zip(FIELDS, ("张三", "100", "skill-a", "bind-a"))))
    changed = replace(caller, user_name="李四", bbk_id="200")
    updated = await service.update(
        changed,
        created["job_id"],
        {
            "request_id": "update-profile",
            "expected_definition_version": 1,
            "spec": create_payload(
                task_type="workflow",
                skill_ids="skill-b",
                workflow_binding_id="bind-b",
            )["spec"],
        },
    )
    assert updated["status"] == "completed"
    assert_profile(db, dict(zip(FIELDS, ("李四", "200", "skill-b", "bind-b"))))
    cleared = await service.update(
        changed,
        created["job_id"],
        {
            "request_id": "clear-workflow-profile",
            "expected_definition_version": 2,
            "spec": create_payload()["spec"],
        },
    )
    assert cleared["status"] == "completed"
    assert_profile(db, dict(zip(FIELDS, ("李四", "200", "", None))))


@pytest.mark.asyncio
async def test_failed_create_keeps_profile_on_draft_and_empty_defaults(setup):
    db, resources, _, caller, service = setup
    resources.fail_prepare = True
    failed = await service.create(caller, create_payload())
    assert failed["status"] == "failed"
    assert not row(db, "swe_cron_jobs_v2")["enabled"]
    assert_profile(db, dict(zip(FIELDS, ("", "", "", None))))


@pytest.mark.asyncio
async def test_preparation_persists_generated_binding_before_platform_retry(
    setup,
    monkeypatch,
):
    db, resources, platform, caller, service = setup
    prepare = resources.prepare

    async def with_binding(operation, identity):
        prepared = await prepare(operation, identity)
        prepared["spec"]["workflow_binding_id"] = "prepared-binding"
        return prepared

    monkeypatch.setattr(resources, "prepare", with_binding)
    platform.fail_enable = True
    failed = await service.create(
        caller,
        create_payload(task_type="workflow", skill_ids="skill-a"),
    )
    assert failed["status"] == "failed"
    assert_profile(
        db,
        dict(zip(FIELDS, ("", "", "skill-a", "prepared-binding"))),
    )
    assert not row(db, "swe_cron_jobs_v2")["enabled"]
    platform.fail_enable = False
    done = await service.retry(
        caller,
        failed["job_id"],
        failed["operation_id"],
    )
    assert done["status"] == "completed"
    assert_profile(
        db,
        dict(zip(FIELDS, ("", "", "skill-a", "prepared-binding"))),
    )


@pytest.mark.asyncio
async def test_failed_update_does_not_publish_new_profile_then_retry_does(
    setup,
):
    db, resources, _, caller, service = setup
    caller = replace(caller, user_name="原用户", bbk_id="100")
    created = await service.create(caller, create_payload(skill_ids="skill-a"))
    resources.fail_prepare = True
    caller = replace(caller, user_name="新用户", bbk_id="200")
    failed = await service.update(
        caller,
        created["job_id"],
        {
            "request_id": "retry-profile",
            "expected_definition_version": 1,
            "spec": create_payload()["spec"],
        },
    )
    assert failed["status"] == "failed"
    assert_profile(db, dict(zip(FIELDS, ("原用户", "100", "skill-a", None))))
    resources.fail_prepare = False
    done = await service.retry(
        caller,
        failed["job_id"],
        failed["operation_id"],
    )
    assert done["status"] == "completed"
    assert_profile(db, dict(zip(FIELDS, ("新用户", "200", "", None))))


@pytest.mark.asyncio
async def test_broadcast_columns_use_receiver_profile_and_prepared_binding(
    distribution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )

    db, caller, service = distribution_setup
    prepare = service.resources.prepare

    async def with_binding(operation, identity):
        prepared = await prepare(operation, identity)
        prepared["spec"]["workflow_binding_id"] = "bind-" + identity.tenant_id
        return prepared

    monkeypatch.setattr(service.resources, "prepare", with_binding)
    root = await service.create(
        replace(caller, user_name="源用户", bbk_id="100"),
        create_payload(task_type="workflow", skill_ids="skill-a"),
    )
    assert root["status"] == "completed"
    command = payload(
        targets=[
            {"tenant_id": "bob", "tenant_name": "接收人", "bbk_id": "200"},
        ],
    )
    task = await BroadcastStore(db).submit(caller, root["job_id"], command)
    await BroadcastProcessor(db, service.resources, service.platform).process(
        task["task_id"],
    )
    child = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE tenant_id='bob'",
    ).fetchone()
    spec = json.loads(child["spec_json"])
    assert [child[field] for field in FIELDS] == [
        "接收人",
        "200",
        "skill-a",
        "bind-bob",
    ]
    assert all(child[field] == spec[field] for field in FIELDS)
