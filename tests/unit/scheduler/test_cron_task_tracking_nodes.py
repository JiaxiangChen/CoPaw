# -*- coding: utf-8 -*-
import pytest

from tests.fakes.cron_task_tracking import TrackingDatabase
from tests.unit.scheduler.test_cron_task_tracking_timeline import (
    event,
    timeline,
)


@pytest.mark.asyncio
async def test_recorded_error_is_not_overwritten_by_later_current_run_success():
    db = TrackingDatabase()
    try:
        db.add("job-1")
        db.run(status="success")
        event(db, "error", status="error", error_code="recorded_failure")
        result = await timeline(db)
        node = next(
            row for row in result["items"] if row["node_type"] == "E06"
        )
        assert node["recorded_outcome"] == "error"
        assert node["current_status"] == "success"
        assert node["details"]["recorded"]["error_code"] == "recorded_failure"
        assert db.writes == 0
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_unknown_event_keeps_recorded_code_without_assuming_success():
    db = TrackingDatabase()
    try:
        db.add("job-1")
        event(db, "future_event")
        node = (await timeline(db))["items"][0]
        assert node["node_type"] == "execution_event"
        assert "future_event" in node["title"]
        assert node["recorded_outcome"] is None
    finally:
        db.conn.close()
