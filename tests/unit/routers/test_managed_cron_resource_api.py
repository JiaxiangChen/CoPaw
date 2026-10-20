# -*- coding: utf-8 -*-
"""验证共用操作日志、Header 鉴权及 SWE 资源准备接口。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_management import (
    ManagementDatabase,
    create_payload,
)
from tests.unit.app.test_managed_cron_resources import Chats, valid_model


@pytest.mark.asyncio
async def test_prepare_reads_authoritative_pending_spec_and_reuses_chat(
    monkeypatch,
):
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.operations import ManagementStore
    from swe.app.crons import managed_api
    from swe.app.crons.managed_resources import ResourcePreparer

    db = ManagementDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    operation, _ = await ManagementStore(db).begin(
        caller,
        "create",
        create_payload(),
    )
    chats = Chats()
    resources = ResourcePreparer(
        chats,
        agent_id="default",
        validate_model=valid_model,
    )
    prepare = resources.prepare

    async def locked_prepare(*args):
        assert (
            db.lock.locked()
        ), "Actual resource IO must serialize with deletion"
        return await prepare(*args)

    monkeypatch.setattr(resources, "prepare", locked_prepare)

    async def factory(_request):
        assert (
            not db.lock.locked()
        ), "Workspace initialization must not inherit a database transaction"
        return resources

    monkeypatch.setattr(managed_api, "get_resource_preparer", factory)
    app = FastAPI()
    app.state.db_connection = db
    app.include_router(managed_api.router, prefix="/api")
    headers = {
        "X-User-Id": "alice",
        "X-Tenant-Id": "alice",
        "X-Source-Id": "bank",
    }
    payload = {
        "operation_id": operation["operation_id"],
        "job_id": operation["job_id"],
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/api/internal/cron/managed/prepare",
            headers=headers,
            json=payload,
        )
        assert first.status_code == 200
        second = await client.post(
            "/api/internal/cron/managed/prepare",
            headers=headers,
            json=payload,
        )
        assert second.json()["chat_id"] == first.json()["chat_id"]
        assert chats.created == 1
        assert first.json()["spec"]["name"] == "report"
        spoofed = await client.post(
            "/api/internal/cron/managed/prepare",
            headers={**headers, "X-User-Id": "bob"},
            json=payload,
        )
        assert spoofed.status_code == 404
        assert (
            await client.post(
                "/api/internal/cron/managed/prepare",
                headers={**headers, "X-Source-Id": "other"},
                json=payload,
            )
        ).status_code == 404
        assert (
            await client.post(
                "/api/internal/cron/managed/prepare",
                headers=headers,
                json={**payload, "spec": {"name": "injected"}},
            )
        ).status_code == 422
    row = db.conn.execute(
        "SELECT management_status, task_chat_id FROM swe_cron_job_states",
    ).fetchone()
    assert row["management_status"] == "creating"
    assert row["task_chat_id"] is None
    db.conn.close()


@pytest.mark.asyncio
async def test_missing_operation_does_not_initialize_workspace(monkeypatch):
    from swe.app.crons import managed_api

    async def forbidden(_request):
        pytest.fail("Invalid operation must not initialize a workspace")

    monkeypatch.setattr(managed_api, "get_resource_preparer", forbidden)
    app = FastAPI()
    app.state.db_connection = ManagementDatabase()
    app.include_router(managed_api.router, prefix="/api")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        result = await client.post(
            "/api/internal/cron/managed/prepare",
            headers={
                "X-User-Id": "alice",
                "X-Source-Id": "bank",
            },
            json={"operation_id": "missing", "job_id": "missing"},
        )
        assert result.status_code == 404
    app.state.db_connection.conn.close()


@pytest.mark.asyncio
async def test_scoped_path_cannot_select_another_workspace_agent(monkeypatch):
    from swe.app.crons import managed_api

    calls = []

    async def forbidden(_request):
        calls.append(True)
        raise RuntimeError("must not initialize workspace")

    monkeypatch.setattr(managed_api, "get_resource_preparer", forbidden)
    app = FastAPI()
    app.state.db_connection = ManagementDatabase()
    app.include_router(managed_api.router, prefix="/api/agents/{agent_id}")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/agents/other/internal/cron/managed/prepare",
            headers={
                "X-User-Id": "alice",
                "X-Source-Id": "bank",
                "X-Agent-Id": "default",
            },
            json={"operation_id": "missing", "job_id": "missing"},
        )
        assert response.status_code == 400
        assert not calls
    app.state.db_connection.conn.close()
