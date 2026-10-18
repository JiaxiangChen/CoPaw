# -*- coding: utf-8 -*-
"""验证迟到派发不能绕过已释放或已转移的占位，恢复接口不执行任务。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.app.test_managed_cron_execution import Engine
from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)

HEADERS = {"X-User-Id": "alice", "X-Tenant-Id": "alice", "X-Source-Id": "bank"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["queued", "released", "expired", "transferred"],
)
async def test_stale_dispatch_is_rejected_before_workspace_initialization(
    execution_setup,
    monkeypatch,
    state,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from swe.app.crons import managed_execution_api

    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    dispatcher = ExecutionDispatcher(db, None, clock=lambda: now)
    claimed = await dispatcher._claim(row, True)
    token = claimed["dispatch_owner"]
    if state == "released":
        await dispatcher._settle(claimed)
    elif state == "queued":
        db.conn.execute(
            "UPDATE swe_cron_execution_runs SET status='queued', dispatch_owner='', dispatch_lease_until=NULL",
        )
    elif state == "expired":
        db.conn.execute(
            "UPDATE swe_cron_execution_runs SET dispatch_lease_until=?",
            ((now - timedelta(minutes=1)).isoformat(" "),),
        )
    else:
        await dispatcher._settle(claimed)
        now += timedelta(minutes=2)
        replacement = await dispatcher._claim(row, True)
        assert replacement["dispatch_owner"] != token
    initialized = []

    async def forbidden(_request):
        initialized.append(True)
        raise AssertionError("过期请求不应准备工作区")

    monkeypatch.setattr(
        managed_execution_api,
        "get_execution_service",
        forbidden,
    )
    app = FastAPI()
    app.state.db_connection = db
    app.include_router(managed_execution_api.router, prefix="/api")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        result = await client.post(
            "/api/internal/cron/managed/execute",
            headers=HEADERS,
            json={
                "execution_id": row["execution_id"],
                "job_id": row["job_id"],
                "dispatch_token": token,
            },
        )
    assert result.status_code == 409
    assert result.json()["detail"] == "cron_dispatch_lease_lost"
    assert not initialized
    assert (
        db.conn.execute(
            "SELECT accepted_at FROM swe_cron_execution_runs",
        ).fetchone()[0]
        is None
    )


@pytest.mark.asyncio
async def test_valid_dispatch_and_result_recovery_have_separate_body_contracts(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
        ManagedSweExecutionClient,
    )
    from swe.app.crons import managed_execution_api
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    started, release = asyncio.Event(), asyncio.Event()
    engine = Engine(started=started, release=release)

    async def service(_request):
        return ManagedExecutionService(ExecutionWriter(db), engine)

    monkeypatch.setattr(
        managed_execution_api,
        "get_execution_service",
        service,
    )
    app = FastAPI()
    app.state.db_connection = db
    app.include_router(managed_execution_api.router, prefix="/api")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        port = ManagedSweExecutionClient("http://test", http=client)
        dispatcher = ExecutionDispatcher(db, port)
        try:
            accepted = await dispatcher.dispatch_registered(
                row["execution_id"],
            )
            assert accepted["status"] in {"accepted", "running"}
            await asyncio.wait_for(started.wait(), 2)
            body = {
                "execution_id": row["execution_id"],
                "job_id": row["job_id"],
            }
            assert (
                await client.post(
                    "/api/internal/cron/managed/execute",
                    headers=HEADERS,
                    json=body,
                )
            ).status_code == 422
            response = await client.post(
                "/api/internal/cron/managed/recover",
                headers=HEADERS,
                json=body,
            )
            assert response.status_code == 200
            assert response.json()["status"] == "running"
        finally:
            release.set()
            await asyncio.gather(
                *getattr(app.state, "managed_cron_tasks", set()),
            )
    assert len(engine.calls) == 1
