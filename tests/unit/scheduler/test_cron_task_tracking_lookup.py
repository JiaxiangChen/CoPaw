# -*- coding: utf-8 -*-
import json

import pytest

from tests.fakes.cron_task_tracking import TrackingDatabase


@pytest.fixture
def db():
    database = TrackingDatabase()
    yield database
    database.conn.close()


def service(db):
    from scheduler.app.services.cron.tracking_lookup import TrackingLookup

    return TrackingLookup(db, "bank")


@pytest.mark.asyncio
async def test_overview_exposes_configured_date_range_and_independent_flags(
    db,
):
    db.add("job-1")
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET run_start_date='2026-11-01',run_end_date='2026-11-30'",
    )
    db.conn.execute(
        "UPDATE swe_cron_job_states SET date_window_paused=1,date_window_sync_required=1",
    )
    result = await service(db).overview("job-1", "alice", "default")
    assert result["definition"].get("run_start_date") == "2026-11-01"
    assert result["definition"].get("run_end_date") == "2026-11-30"
    assert result["state"].get("date_window_paused") == 1
    assert result["state"].get("date_window_sync_required") == 1


@pytest.mark.asyncio
async def test_lookup_never_selects_first_of_multiple_same_id_identities(db):
    db.add("job-1")
    db.add("job-1", tenant="bob")
    db.add("job-1", tenant="mallory", source="BANK")
    result = await service(db).lookup("job-1")
    assert [row["tenant_id"] for row in result["candidates"]] == [
        "alice",
        "bob",
    ]
    assert result["requires_selection"] is True
    assert db.writes == 0


@pytest.mark.asyncio
async def test_legacy_definition_blocks_new_protocol_history_fallback(db):
    db.add("job-1", owner="legacy")
    db.run()
    assert (await service(db).lookup("job-1"))["candidates"] == []
    from scheduler.app.services.cron.tracking_common import TrackingNotFound

    with pytest.raises(TrackingNotFound):
        await service(db).overview("job-1", "alice", "default")


@pytest.mark.asyncio
async def test_missing_definition_can_expose_verified_new_history_only(db):
    db.run()
    result = await service(db).overview("job-1", "alice", "default")
    assert result["history_only"] is True
    assert result["definition"] is None
    assert result["identity"]["source_id"] == "bank"


@pytest.mark.asyncio
async def test_old_or_malformed_protocol_is_not_new_history_identity(db):
    db.run(dispatch_meta_json="{broken")
    db.run("run-2", tenant="bob", dispatch_meta_json='{"protocol":"legacy"}')
    assert (await service(db).lookup("job-1"))["candidates"] == []


@pytest.mark.asyncio
async def test_overview_keeps_current_schedule_and_removes_credentials(db):
    db.add("job-1")
    db.conn.execute("UPDATE swe_cron_jobs_v2 SET cron_expr='0 10 * * *'")
    result = await service(db).overview("job-1", "alice", "default")
    assert result["definition"]["cron_expr"] == "0 10 * * *"
    assert "private-token" not in json.dumps(result)
    assert result["state"]["unread_execution_count"] == 2
    assert db.writes == 0


@pytest.mark.asyncio
async def test_missing_state_is_distinct_from_no_execution(db):
    db.add("job-1", with_state=False)
    result = await service(db).overview("job-1", "alice", "default")
    assert result["state"] is None
    assert result["coverage"]["swe_cron_job_states"] == "empty"
    assert "state_missing" in result["warnings"]


@pytest.mark.asyncio
async def test_parent_and_child_badges_do_not_return_related_users(db):
    db.add("job-1")
    db.add("child", tenant="bob", parent=("alice", "bank", "default", "job-1"))
    parent = await service(db).overview("job-1", "alice", "default")
    child = await service(db).overview("child", "bob", "default")
    assert parent["relation"] == "parent"
    assert child["relation"] == "child"
    assert "bob" not in json.dumps(parent)


@pytest.mark.asyncio
async def test_lookup_refinement_is_required_when_candidate_limit_is_reached(
    db,
):
    for index in range(22):
        db.add("job-1", tenant=f"user-{index:02}")
    result = await service(db).lookup("job-1")
    assert len(result["candidates"]) == 20
    assert result["has_more"] is True
    assert result["requires_selection"] is True


@pytest.mark.asyncio
async def test_partial_role_evidence_is_not_silently_called_ordinary(db):
    db.add("job-1")
    db.conn.execute("UPDATE swe_cron_jobs_v2 SET parent_job_id='parent'")
    result = await service(db).overview("job-1", "alice", "default")
    assert result["relation"] == "unknown"


@pytest.mark.asyncio
async def test_safe_attributes_never_return_exact_api_key_or_access_key_fields(
    db,
):
    db.add("job-1")
    spec = {
        "request": {
            "input": {
                "api_key": "PRIVATE-API",
                "access_key": "PRIVATE-ACCESS",
                "credential": "PRIVATE-CREDENTIAL",
                "safe": "keep",
            },
        },
    }
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET spec_json=?",
        (json.dumps(spec),),
    )
    result = await service(db).overview("job-1", "alice", "default")
    assert "PRIVATE-" not in json.dumps(result)
    assert "keep" in json.dumps(result)


@pytest.mark.asyncio
async def test_slow_optional_source_is_unavailable_not_empty_and_does_not_hang():
    import asyncio
    from scheduler.app.services.cron.tracking_common import TrackingReader

    class SlowDatabase:
        async def fetch_all(self, query, params):
            await asyncio.sleep(0.05)
            return []

    reader = TrackingReader(SlowDatabase(), query_timeout=0.001)
    assert await reader.rows("optional_source", "SELECT 1") == []
    assert reader.coverage["optional_source"] == "unavailable"


@pytest.mark.asyncio
async def test_text_errors_redact_quoted_json_credentials_and_url_passwords(
    db,
):
    db.add("job-1")
    spec = {
        "text": 'workflow failed {"api_key":"PRIVATE-JSON"} https://user:PRIVATE-PASSWORD@example.test',
    }
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET spec_json=?",
        (json.dumps(spec),),
    )
    result = await service(db).overview("job-1", "alice", "default")
    assert "PRIVATE-" not in json.dumps(result)
    assert "workflow failed" in result["definition"]["spec"]["text"]


@pytest.mark.asyncio
async def test_no_definition_and_unavailable_history_is_not_reported_as_not_found(
    db,
):
    from scheduler.app.services.cron.tracking_common import TrackingUnavailable

    db.conn.execute("DROP TABLE swe_cron_execution_runs")
    with pytest.raises(TrackingUnavailable):
        await service(db).lookup("missing")


@pytest.mark.asyncio
async def test_notification_delay_is_available_without_returning_other_metadata(
    db,
):
    db.add("job-1")
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET spec_json=?",
        (
            json.dumps(
                {
                    "meta": {
                        "notification_delay_minutes": 15,
                        "authorization": "PRIVATE",
                        "broadcast_targets": ["PRIVATE-OTHER"],
                    },
                },
            ),
        ),
    )
    result = await service(db).overview("job-1", "alice", "default")
    assert (
        result["definition"]["spec"]["meta"]["notification_delay_minutes"]
        == 15
    )
    assert "PRIVATE" not in json.dumps(result)
