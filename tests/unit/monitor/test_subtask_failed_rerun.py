# -*- coding: utf-8 -*-
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from monitor.app.routers.subtask import router
from monitor.app.services.subtask.rerun_service import get_rerun_service
from monitor.app.services.subtask.rerun_service import RerunService


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return next(self.responses)


@pytest.mark.asyncio
async def test_rerun_failed_executions_counts_only_started_jobs():
    db = AsyncMock()
    db.fetch_all.return_value = [
        {"job_id": "job-1", "tenant_id": "tenant-a"},
        {"job_id": "job-2", "tenant_id": "tenant-b"},
    ]
    request = httpx.Request("POST", "http://test")
    client = FakeClient(
        [
            httpx.Response(200, request=request, json={"started": True}),
            httpx.Response(200, request=request, json={"started": False}),
        ],
    )
    service = RerunService(
        db=db,
        base_url="http://gateway/api",
        authorization="secret",
        client_factory=lambda **_kwargs: client,
    )

    result = await service.rerun_failed_executions(["tenant-a", "tenant-b"])

    assert result.rerun_count == 1
    sql, params = db.fetch_all.await_args.args
    assert "WHERE rn = 1" in sql
    assert "AND status = 'success'" in sql
    assert "AND async_status = 'error'" in sql
    assert params == ("tenant-a", "tenant-b")
    assert client.requests[0][1]["headers"]["X-Tenant-Id"] == "tenant-a"
    assert client.requests[0][1]["headers"]["X-User-Id"] == "tenant-a"
    assert client.requests[0][1]["json"]["request_id"]


@pytest.mark.asyncio
async def test_rerun_failed_executions_skips_failed_gateway_call():
    db = AsyncMock()
    db.fetch_all.return_value = [
        {"job_id": "missing", "tenant_id": "tenant-a"},
    ]
    request = httpx.Request("POST", "http://test")
    client = FakeClient([httpx.Response(404, request=request)])
    service = RerunService(
        db=db,
        base_url="http://gateway/api",
        authorization="secret",
        client_factory=lambda **_kwargs: client,
    )

    result = await service.rerun_failed_executions(["tenant-a"])

    assert result.rerun_count == 0


@pytest.mark.asyncio
async def test_rerun_failed_executions_requires_authorization():
    service = RerunService(db=AsyncMock(), authorization="")

    with pytest.raises(RuntimeError, match="AUTHORIZATION"):
        await service.rerun_failed_executions(["tenant-a"])


@pytest.mark.asyncio
async def test_rerun_failed_executions_accepts_object_body():
    rerun_service = AsyncMock()
    rerun_service.rerun_failed_executions.return_value = {"rerun_count": 1}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_rerun_service] = lambda: rerun_service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/monitor/subtasks/executions/rerun-failed",
            json={"tenant_ids": ["tenant-a", "tenant-a"]},
        )

    assert response.status_code == 200
    assert response.json() == {"rerun_count": 1}
    rerun_service.rerun_failed_executions.assert_awaited_once_with(
        ["tenant-a"],
    )
