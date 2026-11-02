# -*- coding: utf-8 -*-
import json

import pytest

from tests.fakes.cron_task_tracking import TrackingDatabase
from tests.unit.scheduler.test_cron_task_tracking_timeline import (
    IDENTITY,
    history,
    timeline,
)


@pytest.fixture
def db():
    database = TrackingDatabase()
    database.add("job-1")
    yield database
    assert database.writes == 0
    database.conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,node",
    [
        ("created_at", "F01"),
        ("accepted_at", "F02"),
        ("started_at", "F03"),
        ("finished_at", "F04"),
        ("settled_at", "F05"),
    ],
)
async def test_each_recorded_timestamp_fallback_has_its_own_time_basis(
    db,
    field,
    node,
):
    values = {
        "created_at": "2026-10-25 00:00:00",
        field: "2026-10-27 01:00:00",
    }
    if field == "settled_at":
        values["settled_status"] = "success"
    db.run(**values)
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == [node]
    assert result["items"][0]["occurred_at"] == "2026-10-27T09:00:00+08:00"
    assert result["items"][0]["evidence"] == "recorded_timestamp"


@pytest.mark.asyncio
async def test_logical_delete_without_operation_has_no_guessed_actor_or_cleanup_outcome(
    db,
):
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET deleted_at='2026-10-27 01:00:00'",
    )
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == ["F06"]
    assert result["items"][0]["actor_id"] is None
    assert result["items"][0]["current_status"] == "logically_deleted"


@pytest.mark.asyncio
async def test_verified_history_end_is_only_weak_supplement_not_scheduler_confirmation(
    db,
):
    db.run(created_at="2026-10-25 00:00:00", history_execution_id=10)
    history(db, end_time="2026-10-27 09:00:00")
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == ["FH01"]
    assert result["items"][0]["time_basis"] == "history_timestamp"


@pytest.mark.asyncio
async def test_sent_or_read_flags_without_timestamps_do_not_invent_nodes(db):
    db.run(created_at="2026-10-25 00:00:00", history_execution_id=10)
    history(
        db,
        notification_status="sent",
        notification_sent_at=None,
        is_read=1,
        read_at=None,
    )
    assert (await timeline(db))["items"] == []


@pytest.mark.asyncio
async def test_own_skipped_receipt_shows_only_recorded_reason_not_group_counts(
    db,
):
    db.insert(
        "swe_cron_managed_trigger_receipts",
        **IDENTITY,
        receipt_id="receipt",
        kind="batch",
        status="skipped",
        created_at="2026-10-27 01:00:00",
        response_json=json.dumps(
            {"reason": "batch_paused", "other_members": ["PRIVATE"]},
        ),
    )
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == ["T03"]
    assert result["items"][0]["reason"] == "batch_paused"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_distribution_source_and_own_item_have_separate_facts_without_other_targets(
    db,
):
    db.insert(
        "swe_cron_managed_broadcast_tasks",
        **IDENTITY,
        task_id="task",
        actor_id="admin",
        status="completed",
        created_at="2026-10-27 00:00:00",
        payload_json='{"targets":["PRIVATE"]}',
    )
    db.insert(
        "swe_cron_managed_broadcast_items",
        **IDENTITY,
        task_id="task",
        index_no=0,
        operation="retime",
        status="completed",
        created_at="2026-10-27 00:01:00",
        result_json="{}",
    )
    db.insert(
        "swe_cron_managed_broadcast_items",
        **{**IDENTITY, "tenant_id": "PRIVATE"},
        task_id="task",
        index_no=1,
        operation="create",
        status="completed",
        created_at="2026-10-27 00:01:00",
        result_json="{}",
    )
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == ["M11", "M12"]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_root_mode_operation_and_own_projection_are_not_two_nodes(db):
    db.insert(
        "swe_cron_registration_journal",
        **IDENTITY,
        operation_id="op",
        action="dispatch_state",
        actor_id="admin",
        created_at="2026-10-27 00:00:00",
        payload_json='{"state":"ordinary"}',
    )
    db.insert(
        "swe_cron_managed_mode_items",
        **IDENTITY,
        operation_id="op",
        index_no=0,
        status="completed",
    )
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == ["M07"]
