# -*- coding: utf-8 -*-
"""平台固定 Body 无 Header，每次回调独立受理且保留归属与状态校验。"""

import asyncio
import base64
import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
)

RECEIVED_AT = datetime(2026, 10, 19, 3, 47, 2, 123456, tzinfo=timezone.utc)


async def flat_app(setup, kind):
    from scheduler.app.routers import managed_callbacks
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, _, _ = setup
    created = await create_source(
        setup,
        "ordinary" if kind == "ordinary" else "batch_enabled",
    )
    app = FastAPI()
    app.include_router(managed_callbacks.router, prefix="/api")
    service = ManagedSchedulingService(db, clock=lambda: RECEIVED_AT)
    app.dependency_overrides[managed_callbacks.scheduling_service] = (
        lambda: service
    )
    body = {
        "tenant_id": "alice",
        "source_id": "bank",
        "agent_id": "default",
        "job_id": created["job_id"],
        "task_type": "job",
        "scopeId": "YWxpY2U.YmFuaw",
        "fromId": "alice",
        "callback_protocol": "scheduler_managed_v1",
        "operation_id": created["operation_id"],
    }
    return app, body


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "batch"])
@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("concurrent", [False, True])
async def test_fixed_platform_body_accepts_every_arrival_as_independent_work(
    callback_setup,
    kind,
    encoded,
    concurrent,
):
    db, _, _ = callback_setup
    app, params = await flat_app(callback_setup, kind)
    body = (
        {
            "jobParam": base64.urlsafe_b64encode(
                json.dumps(params).encode(),
            ).decode(),
        }
        if encoded
        else params
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        path = f"/api/scheduler/cron/{kind}/callback"
        if concurrent:
            results = await asyncio.gather(
                client.post(path, json=body),
                client.post(path, json=body),
            )
        else:
            results = [
                await client.post(path, json=body),
                await client.post(path, json=body),
            ]
    assert [result.status_code for result in results] == [202, 202]
    records = [result.json() for result in results]
    assert all(
        record["accepted"] and not record["reused"] for record in records
    )
    field = "execution_id" if kind == "ordinary" else "batch_id"
    assert records[0][field] != records[1][field]
    runs = await db.fetch_all("SELECT * FROM swe_cron_execution_runs")
    receipts = await db.fetch_all(
        "SELECT * FROM swe_cron_managed_trigger_receipts",
    )
    assert len(runs) == len(receipts) == 2
    assert len({run["execution_id"] for run in runs}) == 2
    assert len({receipt["receipt_id"] for receipt in receipts}) == 2
    assert all(receipt["callback_key"] is None for receipt in receipts)
    assert all(
        receipt["physical_fire_at"] == "2026-10-19 03:47:02.123456"
        for receipt in receipts
    )
    assert all(receipt["external_job_id"] for receipt in receipts)
    if kind == "batch":
        members = await db.fetch_all(
            "SELECT * FROM swe_cron_managed_batch_members",
        )
        assert len(members) == 2
        assert {member["batch_id"] for member in members} == {
            record["batch_id"] for record in records
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes, headers, status, error",
    [
        (
            {},
            {"X-Source-Id": "other"},
            400,
            "conflicting_cron_callback_identity",
        ),
        (
            {},
            {"X-Tenant-Id": "bob"},
            400,
            "conflicting_cron_callback_identity",
        ),
        ({"scopeId": "wrong"}, {}, 400, "conflicting_cron_scope"),
        ({"scopeId": []}, {}, 400, "conflicting_cron_scope"),
        ({"job_id": "missing"}, {}, 404, "cron_callback_job_not_found"),
        (
            {"operation_id": "missing"},
            {},
            400,
            "cron_callback_binding_reference_invalid",
        ),
        (
            {"external_job_id": "foreign"},
            {},
            400,
            "cron_callback_binding_invalid",
        ),
        (
            {"callback_protocol": "old"},
            {},
            400,
            "cron_callback_binding_invalid",
        ),
        (
            {"tenant_id": "bob", "scopeId": "Ym9i.YmFuaw"},
            {},
            404,
            "cron_callback_job_not_found",
        ),
        ({"tenant_id": 123}, {}, 400, "invalid_cron_identity"),
        ({"fromId": ""}, {}, 400, "invalid_cron_identity"),
    ],
)
async def test_body_fallback_does_not_bypass_identity_or_binding_checks(
    callback_setup,
    changes,
    headers,
    status,
    error,
):
    db, _, _ = callback_setup
    app, params = await flat_app(callback_setup, "ordinary")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        result = await client.post(
            "/api/scheduler/cron/ordinary/callback",
            json={**params, **changes},
            headers=headers,
        )
    assert result.status_code == status
    assert result.json() == {"detail": error}
    assert not await db.fetch_all("SELECT * FROM swe_cron_execution_runs")
    assert not await db.fetch_all(
        "SELECT * FROM swe_cron_managed_trigger_receipts",
    )


@pytest.mark.asyncio
async def test_fixed_batch_body_preserves_advance_offsets_and_independent_members(
    callback_setup,
):
    from datetime import timedelta
    from scheduler.app.services.cron.managed.distribution import BroadcastStore
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from tests.unit.scheduler.test_managed_cron_distribution import payload

    db, caller, management = callback_setup
    app, params = await flat_app(callback_setup, "batch")
    task = await BroadcastStore(db).submit(caller, params["job_id"], payload())
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    params["operation_id"] = task["task_id"]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/api/scheduler/cron/batch/callback",
            json=params,
        )
        second = await client.post(
            "/api/scheduler/cron/batch/callback",
            json=params,
        )
    assert first.status_code == second.status_code == 202
    assert (
        first.json()["enqueued_intents"]
        == second.json()["enqueued_intents"]
        == 3
    )
    assert first.json()["batch_id"] != second.json()["batch_id"]
    receipts = await db.fetch_all(
        "SELECT * FROM swe_cron_managed_trigger_receipts",
    )
    for receipt in receipts:
        logical = datetime.fromisoformat(receipt["logical_fire_at"])
        advance = logical - RECEIVED_AT.replace(tzinfo=None)
        assert advance == timedelta(minutes=120)
    rows = await db.fetch_all(
        "SELECT r.not_before_at,r.scheduled_fire_at,j.offset_minutes FROM swe_cron_execution_runs r "
        "JOIN swe_cron_jobs_v2 j ON r.tenant_id=j.tenant_id AND r.source_id=j.source_id "
        "AND r.agent_id=j.agent_id AND r.job_id=j.job_id",
    )
    assert len(rows) == 6
    assert {row["offset_minutes"] for row in rows} == {0, 120}
    for row in rows:
        assert datetime.fromisoformat(
            row["not_before_at"],
        ) == RECEIVED_AT.replace(tzinfo=None)
        assert datetime.fromisoformat(
            row["scheduled_fire_at"],
        ) == RECEIVED_AT.replace(tzinfo=None) + timedelta(minutes=120)
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_managed_batch_members"))
        == 6
    )


@pytest.mark.asyncio
async def test_missing_platform_id_requires_a_persisted_active_binding(
    callback_setup,
):
    db, _, _ = callback_setup
    app, params = await flat_app(callback_setup, "ordinary")
    db.conn.execute(
        "UPDATE swe_cron_job_states SET ordinary_external_job_id=NULL",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        result = await client.post(
            "/api/scheduler/cron/ordinary/callback",
            json=params,
        )
    assert result.status_code == 400
    assert result.json() == {"detail": "cron_callback_binding_invalid"}
    assert not await db.fetch_all("SELECT * FROM swe_cron_execution_runs")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, statement, reason",
    [
        (
            "ordinary",
            "UPDATE swe_cron_jobs_v2 SET enabled=0",
            "job_not_eligible",
        ),
        (
            "ordinary",
            "UPDATE swe_cron_jobs_v2 SET deleted_at='2026-10-19 01:00:00'",
            "job_deleted",
        ),
        (
            "batch",
            "UPDATE swe_cron_job_states SET dispatch_state='batch_paused'",
            "batch_paused",
        ),
        (
            "batch",
            "UPDATE swe_cron_job_states SET dispatch_state='ordinary'",
            "job_not_eligible",
        ),
    ],
)
async def test_every_arrival_still_obeys_task_execution_gates(
    callback_setup,
    kind,
    statement,
    reason,
):
    db, _, _ = callback_setup
    app, params = await flat_app(callback_setup, kind)
    db.conn.execute(statement)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        result = await client.post(
            f"/api/scheduler/cron/{kind}/callback",
            json=params,
        )
    assert result.status_code == 202
    assert result.json()["accepted"] is False
    assert result.json()["reason"] == reason
    assert not await db.fetch_all("SELECT * FROM swe_cron_execution_runs")
