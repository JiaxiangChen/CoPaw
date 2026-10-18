# -*- coding: utf-8 -*-
"""验证选中子任务操作对源任务和每项完整关系分别鉴权。"""

import pytest

from tests.unit.scheduler.test_managed_cron_executions import execution_setup


@pytest.mark.asyncio
async def test_selected_delete_keeps_actor_and_returns_partial_results(
    execution_setup,
):
    from scheduler.app.services.cron.managed.children_controls import (
        ChildrenControlService,
    )

    db, caller, management = execution_setup
    db.add("source")
    db.add(
        "child",
        tenant="bob",
        actor="bob",
        parent=("alice", "bank", "default", "source"),
    )
    db.add("unrelated", tenant="carol", actor="carol")
    service = ChildrenControlService(db, management)
    body = {
        "request_id": "selected-delete",
        "action": "delete",
        "items": [
            {
                "tenant_id": "bob",
                "agent_id": "default",
                "job_id": "child",
                "expected_definition_version": 1,
            },
            {
                "tenant_id": "carol",
                "agent_id": "default",
                "job_id": "unrelated",
                "expected_definition_version": 1,
            },
        ],
    }
    first = await service.apply(caller, "source", body)
    assert first["success_count"] == 1
    assert first["failed_count"] == 1
    assert first["results"][1]["status_code"] == 404
    operation = db.conn.execute(
        "SELECT * FROM swe_cron_registration_journal WHERE job_id='child'",
    ).fetchone()
    assert operation["actor_id"] == "alice"
    assert operation["tenant_id"] == "bob"
    assert (
        db.conn.execute(
            "SELECT deleted_at FROM swe_cron_jobs_v2 WHERE job_id='unrelated'",
        ).fetchone()[0]
        is None
    )
    again = await service.apply(caller, "source", body)
    assert (
        again["results"][0]["operation"]["operation_id"]
        == first["results"][0]["operation"]["operation_id"]
    )
    assert management.resources.cleaned == ["child"]


@pytest.mark.asyncio
async def test_duplicate_selected_identity_rejected_before_any_delete(
    execution_setup,
):
    from scheduler.app.services.cron.managed.children_controls import (
        ChildrenControlService,
    )

    db, caller, management = execution_setup
    db.add("source")
    item = {
        "tenant_id": "bob",
        "job_id": "child",
        "expected_definition_version": 1,
    }
    with pytest.raises(ValueError, match="duplicate"):
        await ChildrenControlService(db, management).apply(
            caller,
            "source",
            {
                "request_id": "duplicate",
                "action": "delete",
                "items": [item, item],
            },
        )
    assert not management.resources.cleaned
