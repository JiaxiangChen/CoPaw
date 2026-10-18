# -*- coding: utf-8 -*-
"""人工重试不复活旧任务，运输恢复不改执行身份和原快照。"""

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_batch_retry import (
    failed_batch,
    retry_body,
)


@pytest.mark.asyncio
async def test_transport_retry_keeps_snapshot_and_business_attempt(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='dispatch_failed',accepted_at=NULL,started_at=NULL,finished_at=NULL,result_json=NULL,error_code='cron_dispatch_unconfirmed'",
    )
    before = await db.fetch_one("SELECT * FROM swe_cron_execution_runs")
    service = BatchRetryService(db)
    response = await service.retry(
        caller,
        batch,
        retry_body(await service.preview(caller, batch), stopped=True),
    )
    assert response["queued"] == 1
    assert response["results"][0]["transport_only"]
    after = await db.fetch_one("SELECT * FROM swe_cron_execution_runs")
    assert after["execution_id"] == before["execution_id"]
    assert after["spec_json"] == before["spec_json"]
    assert after["dispatch_meta_json"] == before["dispatch_meta_json"]
    member = await db.fetch_one("SELECT * FROM swe_cron_managed_batch_members")
    assert member["business_attempt"] == 1


@pytest.mark.parametrize(
    "change",
    ["definition", "disabled", "deleted", "not_ready", "paused", "running"],
)
@pytest.mark.asyncio
async def test_preview_change_cannot_force_retry(callback_setup, change):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(db)
    command = retry_body(await service.preview(caller, batch))
    statements = {
        "definition": "UPDATE swe_cron_jobs_v2 SET definition_version=2",
        "disabled": "UPDATE swe_cron_jobs_v2 SET enabled=0",
        "deleted": "UPDATE swe_cron_jobs_v2 SET deleted_at='2026-10-17 00:00:00'",
        "not_ready": "UPDATE swe_cron_job_states SET management_status='updating'",
        "paused": "UPDATE swe_cron_job_states SET pause_reason='auth_expired'",
        "running": "UPDATE swe_cron_execution_runs SET status='running'",
    }
    db.conn.execute(statements[change])
    result = await service.retry(caller, batch, command)
    assert result["queued"] == 0
    assert result["skipped"] == 1
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_execution_runs")) == 1
    )


@pytest.mark.asyncio
async def test_audit_write_failure_rolls_back_new_attempt_and_pointer(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(db)
    command = retry_body(await service.preview(caller, batch))
    db.conn.execute(
        "CREATE TRIGGER fail_retry_audit BEFORE INSERT ON swe_cron_managed_dispatch_events WHEN NEW.event_type='retry_request' BEGIN SELECT RAISE(ABORT, 'audit_unavailable'); END",
    )
    with pytest.raises(Exception, match="audit_unavailable"):
        await service.retry(caller, batch, command)
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_execution_runs")) == 1
    )
    assert (
        await db.fetch_one("SELECT * FROM swe_cron_managed_batch_members")
    )["business_attempt"] == 1


@pytest.mark.asyncio
async def test_dispatch_failed_with_an_acceptance_is_not_a_new_business_retry(
    callback_setup,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='dispatch_failed'",
    )
    service = BatchRetryService(db)
    result = await service.retry(
        caller,
        batch,
        retry_body(await service.preview(caller, batch), stopped=True),
    )
    assert result["queued"] == 0
    assert (
        result["results"][0]["reason"] == "cron_retry_acceptance_inconsistent"
    )
