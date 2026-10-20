# -*- coding: utf-8 -*-
"""公开编辑不能篡改或丢失分发关联，旧表摘要仍能支撑原统计。"""

import json

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
)
from tests.unit.scheduler.test_managed_cron_distribution import payload


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "batch_enabled"])
async def test_receiver_edit_preserves_authoritative_relation_and_never_registers_batch_child_timer(
    callback_setup,
    mode,
):
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.identity import CallerIdentity

    db, caller, management = callback_setup
    root = await create_source(callback_setup, mode)
    task = await BroadcastStore(db).submit(caller, root["job_id"], payload())
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    child = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE parent_job_id=? AND tenant_id='bob'",
        (root["job_id"],),
    ).fetchone()
    original = json.loads(child["spec_json"])
    original_relation = original["meta"]["broadcast_source_job_id"]
    edited = json.loads(child["spec_json"])
    edited["name"] = "receiver edit"
    edited["meta"] = {
        "broadcast_source_job_id": "forged-parent",
        "public_note": "keep",
    }
    registered = management.platform.calls.count("register")
    result = await management.update(
        CallerIdentity("bob", "bank", "default", "bob"),
        child["job_id"],
        {
            "request_id": "receiver-update",
            "expected_definition_version": child["definition_version"],
            "spec": edited,
        },
    )
    assert result["status"] == "completed"
    current = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE job_id=?",
        (child["job_id"],),
    ).fetchone()
    meta = json.loads(current["spec_json"])["meta"]
    assert meta["broadcast_source_job_id"] == original_relation
    assert (
        meta["broadcast_original_cron"]
        == original["meta"]["broadcast_original_cron"]
    )
    assert meta["public_note"] == "keep"
    projected = json.loads(
        db.conn.execute(
            "SELECT meta FROM swe_cron_jobs WHERE id=?",
            (child["job_id"],),
        ).fetchone()[0],
    )
    assert projected["broadcast_source_job_id"] == root["job_id"]
    if mode == "batch_enabled":
        assert management.platform.calls.count("register") == registered
        assert (
            db.conn.execute(
                "SELECT batch_external_job_id FROM swe_cron_job_states WHERE job_id=?",
                (child["job_id"],),
            ).fetchone()[0]
            is None
        )


def test_large_compatibility_meta_keeps_relation_keys_but_not_large_business_blob():
    from scheduler.app.services.cron.managed.projection import _meta_preview

    relation = {
        "broadcast_source_job_id": "parent",
        "broadcast_source_tenant_id": "alice",
        "broadcast_source_source_id": "bank",
        "broadcast_source_agent_id": "default",
        "broadcast_offset_minutes": 120,
        "broadcast_original_cron": "0 9 * * *",
        "broadcast_original_timezone": "Asia/Shanghai",
        "broadcast_notification_policy": "original_schedule",
        "job_origin": "broadcast",
    }
    encoded = _meta_preview(
        {
            **relation,
            "public_blob": "x" * 10000,
            "agent_id": "default",
            "management_owner": "scheduler",
        },
    )
    stored = json.loads(encoded)
    assert stored["compat_meta_truncated"]
    for key, value in relation.items():
        assert stored[key] == value
    assert "public_blob" not in stored
    assert len(encoded) <= 4096


@pytest.mark.asyncio
async def test_root_cannot_skip_platform_registration_by_forging_child_metadata(
    callback_setup,
):
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    db, caller, management = callback_setup
    body = create_payload("forged-relation")
    body["dispatch_state"] = "batch_enabled"
    body["spec"]["meta"] = {
        "broadcast_source_job_id": "forged-parent",
        "job_origin": "broadcast",
    }
    result = await management.create(caller, body)
    assert result["status"] == "completed"
    assert management.platform.calls.count("register") == 1
    row = db.conn.execute(
        "SELECT * FROM swe_cron_jobs_v2 WHERE job_id=?",
        (result["job_id"],),
    ).fetchone()
    assert row["parent_job_id"] is None
    assert json.loads(row["spec_json"])["meta"]["job_origin"] == "manual"
    assert (
        "broadcast_source_job_id" not in json.loads(row["spec_json"])["meta"]
    )
