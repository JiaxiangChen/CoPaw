# -*- coding: utf-8 -*-
"""验证内部执行接口先鉴权、再准备工作区，并持久登记受理。"""

import asyncio

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
async def test_internal_execution_accepts_shared_record_then_runs_once(
    execution_setup,
    monkeypatch,
):
    from swe.app.crons import managed_execution_api
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    claimed = await ExecutionDispatcher(db, None)._claim(row, True)
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
    body = {
        "execution_id": row["execution_id"],
        "job_id": row["job_id"],
        "dispatch_token": claimed["dispatch_owner"],
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/api/internal/cron/managed/execute",
            headers=HEADERS,
            json=body,
        )
        assert first.status_code == 202
        assert first.json()["execution_id"] == row["execution_id"]
        await started.wait()
        duplicate = await client.post(
            "/api/internal/cron/managed/execute",
            headers=HEADERS,
            json=body,
        )
        assert duplicate.status_code == 202
        release.set()
        await asyncio.gather(*app.state.managed_cron_tasks)
    assert len(engine.calls) == 1
    assert (
        db.conn.execute(
            "SELECT status FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == "success"
    )


@pytest.mark.asyncio
async def test_invalid_execution_does_not_bootstrap_workspace(
    execution_setup,
    monkeypatch,
):
    from swe.app.crons import managed_execution_api

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    calls = []

    async def forbidden(_request):
        calls.append(True)
        raise RuntimeError("must not prepare unauthorized workspace")

    monkeypatch.setattr(
        managed_execution_api,
        "get_execution_service",
        forbidden,
    )
    app = FastAPI()
    app.state.db_connection = db
    app.include_router(managed_execution_api.router, prefix="/api")
    body = {
        "execution_id": row["execution_id"],
        "job_id": row["job_id"],
        "dispatch_token": "untrusted-token",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for headers in (
            {**HEADERS, "X-User-Id": "bob"},
            {**HEADERS, "X-Source-Id": "BANK"},
        ):
            response = await client.post(
                "/api/internal/cron/managed/execute",
                headers=headers,
                json=body,
            )
            assert response.status_code == 404
        spoofed = await client.post(
            "/api/internal/cron/managed/execute",
            headers=HEADERS,
            json={**body, "spec": {"name": "injected"}},
        )
        assert spoofed.status_code == 422
    assert not calls
