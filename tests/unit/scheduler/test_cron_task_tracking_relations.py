# -*- coding: utf-8 -*-
import json

import pytest

from tests.fakes.cron_task_tracking import TrackingDatabase
from tests.unit.scheduler.test_cron_task_tracking_timeline import (
    IDENTITY,
    event,
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
async def test_reused_ordinary_run_proves_own_batch_participation_without_other_members(
    db,
):
    db.run()
    db.insert(
        "swe_cron_managed_batch_members",
        **IDENTITY,
        batch_id="b",
        member_id="m",
        initial_execution_id="run-1",
        current_execution_id="run-1",
        member_role="child",
    )
    members = [
        {"identity": list(IDENTITY.values()), "execution_id": "run-1"},
        {
            "identity": ["private-user", "bank", "default", "private-job"],
            "execution_id": "private-run",
        },
    ]
    db.insert(
        "swe_cron_managed_trigger_receipts",
        **{**IDENTITY, "job_id": "parent"},
        receipt_id="batch-receipt",
        kind="batch",
        status="accepted",
        batch_id="b",
        created_at="2026-10-27 00:00:00",
        members_json=json.dumps(members),
        response_json="{}",
    )
    result = await timeline(db)
    assert (
        len([row for row in result["items"] if row["node_type"] == "T02"]) == 1
    )
    assert "private-user" not in json.dumps(result)
    assert "private-job" not in json.dumps(result)


@pytest.mark.asyncio
async def test_member_pointer_without_matching_frozen_identity_does_not_create_trigger_node(
    db,
):
    db.run()
    db.insert(
        "swe_cron_managed_batch_members",
        **IDENTITY,
        batch_id="b",
        member_id="m",
        initial_execution_id="run-1",
        current_execution_id="run-1",
        member_role="child",
    )
    db.insert(
        "swe_cron_managed_trigger_receipts",
        **{**IDENTITY, "job_id": "parent"},
        receipt_id="batch-receipt",
        kind="batch",
        status="accepted",
        batch_id="b",
        created_at="2026-10-27 00:00:00",
        members_json="[]",
        response_json="{}",
    )
    result = await timeline(db)
    assert not any(row["node_type"] == "T02" for row in result["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,node,payload",
    [
        ("create", "M01", {}),
        ("update", "M02", {}),
        ("set_enabled", "M03", {"enabled": True}),
        ("set_enabled", "M04", {"enabled": False}),
        ("auto_pause", "M05", {}),
        ("delete", "M06", {}),
        ("dispatch_state", "M07", {"state": "batch_paused"}),
        ("set_priority", "M09", {"user_ids": ["PRIVATE-USER"]}),
        ("mark_read", "M10", {}),
    ],
)
async def test_management_actions_have_one_request_node_and_do_not_expose_other_targets(
    db,
    action,
    node,
    payload,
):
    db.insert(
        "swe_cron_registration_journal",
        **IDENTITY,
        operation_id="op",
        action=action,
        actor_id="admin",
        status="completed",
        created_at="2026-10-27 00:00:00",
        payload_json=json.dumps(payload),
    )
    result = await timeline(db, category="management")
    assert [item["node_type"] for item in result["items"]] == [node]
    assert "PRIVATE-USER" not in json.dumps(result)


@pytest.mark.asyncio
async def test_group_mode_change_projects_only_own_item_and_deduplicates_root(
    db,
):
    db.insert(
        "swe_cron_registration_journal",
        **{**IDENTITY, "job_id": "parent"},
        operation_id="op",
        action="dispatch_state",
        actor_id="admin",
        status="completed",
        created_at="2026-10-27 00:00:00",
        payload_json='{"state":"ordinary"}',
    )
    db.insert(
        "swe_cron_managed_mode_items",
        **IDENTITY,
        operation_id="op",
        index_no=1,
        status="failed",
        error_code="sync_failed",
    )
    result = await timeline(db)
    assert [item["node_type"] for item in result["items"]] == ["M08"]
    assert result["items"][0]["details"]["item_status"] == "failed"
    assert result["items"][0]["current_status"] == "failed"


@pytest.mark.asyncio
async def test_failed_daily_rerun_decision_is_not_mislabeled_as_original_execution_skipped(
    db,
):
    db.run()
    db.insert(
        "swe_cron_failure_retry_calls",
        call_id="call",
        source_id="bank",
        status="completed",
    )
    event(db, "retry_skipped", call_id="call", reason="already_success")
    result = await timeline(db, entry="daily_failure_retry")
    assert [item["node_type"] for item in result["items"]] == ["E14"]
    assert result["items"][0]["details"]["retry_initiator"] == "unrecorded"


@pytest.mark.asyncio
async def test_current_spec_schedule_uses_database_columns_and_overview_times_are_aware(
    db,
):
    from scheduler.app.services.cron.tracking_lookup import TrackingLookup

    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET cron_expr='0 10 * * *',created_at='2026-10-27 00:00:00'",
    )
    db.conn.execute(
        "UPDATE swe_cron_job_states SET last_run_at='2026-10-27 01:00:00'",
    )
    result = await TrackingLookup(db, "bank").overview(
        "job-1",
        "alice",
        "default",
    )
    assert result["definition"]["spec"]["schedule"]["cron"] == "0 10 * * *"
    assert result["definition"]["created_at"] == "2026-10-27T08:00:00+08:00"
    assert result["state"]["last_run_at"] == "2026-10-27T09:00:00+08:00"


@pytest.mark.asyncio
async def test_malformed_event_details_are_reported_as_missing_not_silent_empty(
    db,
):
    event(db, "running")
    db.conn.execute(
        "UPDATE swe_cron_managed_dispatch_events SET details_json='{broken'",
    )
    result = await timeline(db)
    assert "event_details_unavailable" in result["warnings"]
    assert result["items"][0]["title"] == "开始执行"


@pytest.mark.asyncio
async def test_parent_and_child_role_conflict_is_unknown(db):
    from scheduler.app.services.cron.tracking_lookup import TrackingLookup

    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET parent_tenant_id='bob',parent_source_id='bank',parent_agent_id='default',parent_job_id='parent'",
    )
    db.insert(
        "swe_cron_managed_broadcast_tasks",
        **IDENTITY,
        task_id="broadcast",
    )
    result = await TrackingLookup(db, "bank").overview(
        "job-1",
        "alice",
        "default",
    )
    assert result["relation"] == "unknown"


@pytest.mark.asyncio
async def test_daily_rerun_context_expands_only_safe_call_metadata_without_global_counts(
    db,
):
    db.run()
    db.insert(
        "swe_cron_failure_retry_calls",
        call_id="call",
        source_id="bank",
        status="completed",
        business_date="2026-10-27",
        window_start="2026-10-26 16:00:00",
        cutoff_at="2026-10-27 01:00:00",
    )
    event(db, "retry_accepted", call_id="call", reason="queued")
    result = await timeline(db, entry="daily_failure_retry")
    context = result["items"][0]["details"]["daily_retry_context"]
    assert context["business_date"] == "2026-10-27"
    assert context["window_start"] == "2026-10-27T00:00:00+08:00"
    assert "queued_count" not in context


@pytest.mark.asyncio
async def test_wrong_json_reference_types_do_not_break_other_valid_events(db):
    db.run(
        dispatch_meta_json=json.dumps(
            {
                "protocol": "scheduler_managed_v1",
                "trigger_receipt_id": {"wrong": "type"},
            },
        ),
    )
    event(db, "running", call_id=["wrong-type"])
    result = await timeline(db)
    assert any(row["node_type"] == "E04" for row in result["items"])
    assert "reference_identity_unavailable" in result["warnings"]


@pytest.mark.asyncio
async def test_incomplete_run_schema_does_not_hide_own_trigger_receipt(db):
    db.conn.execute(
        "ALTER TABLE swe_cron_execution_runs DROP COLUMN dispatch_meta_json",
    )
    db.insert(
        "swe_cron_managed_trigger_receipts",
        **IDENTITY,
        receipt_id="own-receipt",
        kind="ordinary",
        status="accepted",
        created_at="2026-10-27 00:00:00",
    )
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == ["T01"]
    assert result["coverage"]["swe_cron_execution_runs"] == "unavailable"


@pytest.mark.asyncio
async def test_malformed_nested_event_fields_are_not_returned_as_status_or_group_payload(
    db,
):
    event(
        db,
        "running",
        status={"other_user": "PRIVATE"},
        reason={"targets": ["PRIVATE"]},
    )
    result = await timeline(db)
    assert result["items"][0]["recorded_outcome"] is None
    assert result["items"][0]["reason"] is None
    assert "PRIVATE" not in json.dumps(result)
    assert "event_details_unavailable" in result["warnings"]
