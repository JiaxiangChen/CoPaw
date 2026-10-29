# -*- coding: utf-8 -*-
from datetime import datetime, timezone

import pytest

from scheduler.app.models.cron_task_tracking import TimelineQuery
from scheduler.app.services.cron.tracking_timeline import TrackingTimeline
from tests.fakes.cron_task_tracking import TrackingDatabase
from tests.unit.scheduler.test_cron_task_tracking_timeline import history


@pytest.mark.asyncio
async def test_utc_request_window_selects_beijing_notification_at_the_same_instant():
    db = TrackingDatabase()
    try:
        db.add("job-1")
        db.run(created_at="2026-10-25 00:00:00", history_execution_id=10)
        history(
            db,
            notification_status="sent",
            notification_sent_at="2026-10-27 00:00:00",
        )
        query = TimelineQuery(
            start_time="2026-10-26T16:00:00Z",
            end_time="2026-10-26T16:00:01Z",
        )
        result = await TrackingTimeline(
            db,
            "bank",
            clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        ).timeline("job-1", "alice", "default", query)
        assert [row["node_type"] for row in result["items"]] == ["N01"]
        assert result["items"][0]["occurred_at"] == "2026-10-27T00:00:00+08:00"
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_planned_execution_and_notification_times_do_not_create_happened_nodes():
    db = TrackingDatabase()
    try:
        db.add("job-1")
        db.run(
            created_at="2026-10-25 00:00:00",
            history_execution_id=10,
            scheduled_fire_at="2026-10-27 01:00:00",
            not_before_at="2026-10-27 01:00:00",
        )
        history(
            db,
            notification_status="pending",
            notification_due_at="2026-10-27 09:00:00",
        )
        query = TimelineQuery(
            start_time="2026-10-27T00:00:00+08:00",
            end_time="2026-10-28T00:00:00+08:00",
        )
        result = await TrackingTimeline(
            db,
            "bank",
            clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc),
        ).timeline("job-1", "alice", "default", query)
        assert result["items"] == []
        assert db.writes == 0
    finally:
        db.conn.close()
