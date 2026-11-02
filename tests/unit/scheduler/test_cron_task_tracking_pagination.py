# -*- coding: utf-8 -*-
import pytest

from tests.fakes.cron_task_tracking import TrackingDatabase
from tests.unit.scheduler.test_cron_task_tracking_timeline import (
    IDENTITY,
    event,
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
    "order,wanted",
    [("asc", ["E01", "M01", "N01"]), ("desc", ["N01", "M01", "E01"])],
)
async def test_mixed_sources_at_same_instant_have_stable_bidirectional_pages(
    db,
    order,
    wanted,
):
    db.run(history_execution_id=10)
    event(db, "queued")
    db.conn.execute(
        "UPDATE swe_cron_managed_dispatch_events SET occurred_at='2026-10-27 00:00:00'",
    )
    history(
        db,
        notification_status="sent",
        notification_sent_at="2026-10-27 08:00:00",
    )
    db.insert(
        "swe_cron_registration_journal",
        **IDENTITY,
        operation_id="op",
        action="create",
        created_at="2026-10-27 00:00:00",
        payload_json="{}",
    )
    found, cursor = [], None
    for _ in range(4):
        result = await timeline(db, limit=1, cursor=cursor, order=order)
        found.extend(row["node_type"] for row in result["items"])
        cursor = result["next_cursor"]
        if not result["has_more"]:
            break
    assert found == wanted


@pytest.mark.asyncio
async def test_cursor_cannot_be_reused_for_changed_filter_or_identity(db):
    event(db, "running", 1)
    event(db, "success", 2)
    first = await timeline(db, limit=1)
    with pytest.raises(ValueError, match="cursor"):
        await timeline(
            db,
            limit=1,
            category="execution",
            cursor=first["next_cursor"],
        )


@pytest.mark.asyncio
async def test_range_end_is_exclusive_and_utc_facts_are_filtered_as_beijing(
    db,
):
    event(db, "running", 1)
    event(db, "success", 2)
    db.conn.execute(
        "UPDATE swe_cron_managed_dispatch_events SET occurred_at='2026-10-26 16:00:00' WHERE event_order=1",
    )
    db.conn.execute(
        "UPDATE swe_cron_managed_dispatch_events SET occurred_at='2026-10-27 16:00:00' WHERE event_order=2",
    )
    result = await timeline(db)
    assert [row["node_type"] for row in result["items"]] == ["E04"]


@pytest.mark.asyncio
async def test_fallback_confirmation_is_suppressed_even_when_event_is_on_another_page(
    db,
):
    db.run(settled_at="2026-10-27 00:05:00", settled_status="success")
    event(db, "running", 1)
    event(db, "settled", 2, settled_status="success")
    found, cursor = [], None
    for _ in range(6):
        result = await timeline(db, limit=1, cursor=cursor)
        found.extend(row["node_type"] for row in result["items"])
        cursor = result["next_cursor"]
        if not result["has_more"]:
            break
    assert "E09" in found
    assert "F05" not in found


@pytest.mark.asyncio
async def test_transport_resend_keeps_original_manual_entry_and_same_execution(
    db,
):
    db.run(kind="manual")
    event(db, "transport_redelivery", reason="confirmation_missing")
    result = await timeline(db, entry="manual")
    resend = next(row for row in result["items"] if row["node_type"] == "E11")
    assert resend["entry"] == "manual"
    assert resend["execution_id"] == "run-1"
