# -*- coding: utf-8 -*-
import json
from datetime import datetime, timezone

import pytest

from tests.fakes.cron_task_tracking import TrackingDatabase

IDENTITY = {
    "tenant_id": "alice",
    "source_id": "bank",
    "agent_id": "default",
    "job_id": "job-1",
}


@pytest.fixture
def db():
    database = TrackingDatabase()
    database.add("job-1")
    yield database
    assert database.writes == 0
    database.conn.close()


async def timeline(db, **filters):
    from scheduler.app.models.cron_task_tracking import TimelineQuery
    from scheduler.app.services.cron.tracking_timeline import TrackingTimeline

    query = TimelineQuery(
        start_time="2026-10-27T00:00:00+08:00",
        end_time="2026-10-28T00:00:00+08:00",
        **filters,
    )
    return await TrackingTimeline(
        db,
        "bank",
        clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
    ).timeline("job-1", "alice", "default", query)


def event(db, kind, number=1, **extra):
    db.insert(
        "swe_cron_managed_dispatch_events",
        **IDENTITY,
        event_order=number,
        event_id=f"event-{number}",
        event_key=f"key-{number}",
        execution_id="run-1",
        event_type=kind,
        occurred_at=f"2026-10-27 00:00:{number:02}",
        details_json=json.dumps(extra),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,expected",
    [
        ("pause_group", "暂停任务及全部子任务"),
        ("date_range", "可执行时间区间"),
    ],
)
async def test_new_group_management_nodes_keep_actual_submission_time(
    db,
    action,
    expected,
):
    period = {"start_date": "2026-11-01", "end_date": "2026-11-30"}
    db.insert(
        "swe_cron_registration_journal",
        **IDENTITY,
        operation_id="op-new",
        action=action,
        actor_id="alice",
        payload_json=json.dumps({"run_date_range": period}),
        status="completed",
        stage="completed",
        version=1,
        created_at="2026-10-27 00:00:00",
        updated_at="2026-10-27 00:01:00",
    )
    result = await timeline(db, category="management")
    node = next(
        item
        for item in result["items"]
        if item.get("operation_id") == "op-new"
    )
    assert expected in node["title"]
    assert node["occurred_at"] == "2026-10-27T08:00:00+08:00"
    if action == "date_range":
        assert node["details"]["run_date_range"] == period


def history(db, *, meta=None, **values):
    db.insert(
        "swe_cron_executions",
        id=10,
        job_id="job-1",
        tenant_id="alice",
        meta=json.dumps(
            meta
            or {
                "management_owner": "scheduler",
                "managed_execution_id": "run-1",
                "source_id": "bank",
                "agent_id": "default",
            },
        ),
        status="success",
        **values,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,node",
    [
        ("queued", "E01"),
        ("dispatching", "E02"),
        ("accepted", "E03"),
        ("running", "E04"),
        ("success", "E05"),
        ("error", "E06"),
        ("skipped", "E07"),
        ("cancelled", "E08"),
        ("settled", "E09"),
        ("dispatch_unconfirmed", "E10"),
        ("transport_redelivery", "E11"),
        ("transport_requeued", "E12"),
        ("retry_accepted", "E13"),
        ("retry_skipped", "E14"),
    ],
)
async def test_maps_each_recorded_event_without_inventing_state_transitions(
    db,
    kind,
    node,
):
    event(
        db,
        kind,
        status="error",
        reason="recorded_reason",
        settled_status="error",
    )
    result = await timeline(db)
    assert [item["node_type"] for item in result["items"]] == [node]
    assert result["items"][0]["occurred_at"] == "2026-10-27T08:00:01+08:00"
    assert result["items"][0]["recorded_outcome"] == "error"


@pytest.mark.asyncio
async def test_scheduler_confirmation_is_not_async_actual_completion_and_deduplicates(
    db,
):
    db.run(settled_at="2026-10-27 00:05:00", settled_status="error")
    event(
        db,
        "settled",
        settled_status="error",
        settled_error_code="cron_async_subtasks_failed",
    )
    result = await timeline(db)
    nodes = [
        row for row in result["items"] if row["node_type"] in {"E09", "F05"}
    ]
    assert len(nodes) == 1
    assert nodes[0]["time_basis"] == "scheduler_confirmed"
    assert nodes[0]["title"] == "Scheduler 已确认整轮结果"


@pytest.mark.asyncio
async def test_previous_day_run_can_have_notification_and_read_in_selected_window(
    db,
):
    db.run(created_at="2026-10-25 00:00:00", history_execution_id=10)
    history(
        db,
        is_final=0,
        notification_status="sent",
        notification_sent_at="2026-10-27 09:00:00",
        is_read=1,
        read_at="2026-10-27 09:30:00",
        notification_attempts=2,
    )
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == ["N01", "N02"]
    assert result["items"][0]["occurred_at"] == "2026-10-27T09:00:00+08:00"
    assert result["items"][0]["details"]["notification_failure_count"] == 2


@pytest.mark.asyncio
async def test_wrong_history_identity_never_exposes_result_or_notification(db):
    db.run(history_execution_id=10)
    history(
        db,
        meta={"management_owner": "scheduler", "source_id": "evil"},
        notification_status="sent",
        notification_sent_at="2026-10-27 09:00:00",
        output_preview="PRIVATE OTHER USER",
    )
    event(db, "success")
    result = await timeline(db)
    assert "PRIVATE OTHER USER" not in json.dumps(result)
    assert not any(row["node_type"] == "N01" for row in result["items"])
    assert "history_identity_unconfirmed" in result["warnings"]


@pytest.mark.asyncio
async def test_failed_notification_without_timestamp_stays_in_execution_detail(
    db,
):
    db.run(history_execution_id=10)
    history(
        db,
        notification_status="failed",
        notification_attempts=3,
        notification_error="send failed Authorization=secret-value",
    )
    event(db, "success")
    result = await timeline(db)
    node = next(row for row in result["items"] if row["node_type"] == "E05")
    assert node["details"]["notification_failure_count"] == 3
    assert "secret-value" not in json.dumps(result)
    assert not any(
        row["category"] == "notification" for row in result["items"]
    )


@pytest.mark.asyncio
async def test_management_uses_submission_time_and_current_outcome_not_fake_finish_event(
    db,
):
    db.insert(
        "swe_cron_registration_journal",
        **IDENTITY,
        operation_id="op-1",
        action="create",
        actor_id="admin",
        created_at="2026-10-27 00:00:00",
        updated_at="2026-10-27 01:00:00",
        status="completed",
        stage="completed",
        payload_json="{}",
    )
    result = await timeline(db)
    assert len(result["items"]) == 1
    assert result["items"][0]["node_type"] == "M01"
    assert result["items"][0]["occurred_at"] == "2026-10-27T08:00:00+08:00"
    assert result["items"][0]["time_basis"] == "operation_submitted"


@pytest.mark.asyncio
async def test_same_second_keyset_pages_never_duplicate_or_omit(db):
    for index in range(1, 8):
        event(db, "dispatching", index)
    db.conn.execute(
        "UPDATE swe_cron_managed_dispatch_events SET occurred_at='2026-10-27 00:00:00'",
    )
    found, cursor = [], None
    for _ in range(4):
        result = await timeline(db, limit=2, cursor=cursor)
        found += [row["event_id"] for row in result["items"]]
        cursor = result["next_cursor"]
        if not result["has_more"]:
            break
    assert len(found) == 7
    assert len(set(found)) == 7
    assert cursor is None


@pytest.mark.asyncio
async def test_batch_aggregate_retry_and_parent_skipped_receipt_do_not_leak_to_child(
    db,
):
    event(db, "retry_request", response={"other_users": ["private"]})
    db.insert(
        "swe_cron_managed_trigger_receipts",
        **{**IDENTITY, "job_id": "parent"},
        receipt_id="root-receipt",
        kind="batch",
        status="skipped",
        batch_id="batch-1",
        created_at="2026-10-27 00:00:00",
        members_json="[]",
        response_json="{}",
    )
    result = await timeline(db)
    assert result["items"] == []


@pytest.mark.asyncio
async def test_missing_event_table_uses_recorded_timestamps_with_visible_warning(
    db,
):
    db.run(
        started_at="2026-10-27 00:03:00",
        settled_at="2026-10-27 00:05:00",
        settled_status="success",
    )
    db.conn.execute("DROP TABLE swe_cron_managed_dispatch_events")
    result = await timeline(db)
    assert {row["node_type"] for row in result["items"]} >= {
        "F01",
        "F03",
        "F05",
    }
    assert (
        result["coverage"]["swe_cron_managed_dispatch_events"] == "unavailable"
    )


@pytest.mark.asyncio
async def test_management_filter_does_not_load_unrelated_execution_history(
    db,
    monkeypatch,
):
    original = db.fetch_all

    async def unavailable_events(query, params=None):
        if "swe_cron_managed_dispatch_events n" in query:
            raise RuntimeError(
                "unrelated event query should not affect management",
            )
        return await original(query, params)

    monkeypatch.setattr(db, "fetch_all", unavailable_events)
    db.insert(
        "swe_cron_registration_journal",
        **IDENTITY,
        operation_id="operation",
        action="create",
        created_at="2026-10-27 00:00:00",
        payload_json="{}",
    )
    result = await timeline(db, category="management")
    assert [node["node_type"] for node in result["items"]] == ["M01"]
    assert (
        "swe_cron_managed_dispatch_events_unavailable"
        not in result["warnings"]
    )


@pytest.mark.asyncio
async def test_notification_eligibility_is_shown_without_treating_zero_as_no_notification(
    db,
):
    db.run(history_execution_id=10)
    history(
        db,
        need_notification=0,
        notification_status="pending",
        notification_timezone="Asia/Shanghai",
    )
    event(db, "success")
    result = await timeline(db)
    node = next(row for row in result["items"] if row["node_type"] == "E05")
    assert node["details"]["need_notification"] == 0
    assert node["details"]["notification_timezone"] == "Asia/Shanghai"
    assert node["details"]["notification_status"] == "pending"
    assert not any(
        row["category"] == "notification" for row in result["items"]
    )
