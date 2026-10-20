# -*- coding: utf-8 -*-
import asyncio
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
async def test_rerun_failed_executions_returns_submitted_job_count():
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
    tasks = list(service._background_tasks)

    assert result.rerun_count == 2
    await asyncio.gather(*tasks)
    sql, params = db.fetch_all.await_args.args
    assert "WHERE rn = 1" in sql
    assert "AND status = 'success'" in sql
    assert "AND async_status = 'error'" in sql
    assert params == ("tenant-a", "tenant-b")
    assert client.requests[0][1]["headers"]["X-Tenant-Id"] == "tenant-a"
    assert client.requests[0][1]["headers"]["X-User-Id"] == "tenant-a"
    assert client.requests[0][1]["json"]["request_id"]


@pytest.mark.asyncio
async def test_rerun_failed_executions_returns_before_gateway_finishes():
    db = AsyncMock()
    db.fetch_all.return_value = [
        {"job_id": "missing", "tenant_id": "tenant-a"},
    ]
    release_request = asyncio.Event()
    request_started = asyncio.Event()
    client = FakeClient([])

    async def post(_url, **_kwargs):
        request_started.set()
        await release_request.wait()
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://test"),
            json={"started": True},
        )

    client.post = post
    service = RerunService(
        db=db,
        base_url="http://gateway/api",
        authorization="secret",
        client_factory=lambda **_kwargs: client,
    )

    result = await service.rerun_failed_executions(["tenant-a"])
    tasks = list(service._background_tasks)

    assert result.rerun_count == 1
    await asyncio.wait_for(request_started.wait(), timeout=0.5)
    assert service._background_tasks
    release_request.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_rerun_failed_executions_calls_gateway_sequentially():
    db = AsyncMock()
    db.fetch_all.return_value = [
        {"job_id": "job-1", "tenant_id": "tenant-a"},
        {"job_id": "job-2", "tenant_id": "tenant-b"},
    ]
    calls = []

    async def post(_url, **_kwargs):
        calls.append(_url)
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://test"),
            json={"started": True},
        )

    client = FakeClient([])
    client.post = post
    service = RerunService(
        db=db,
        base_url="http://gateway/api",
        authorization="secret",
        client_factory=lambda **_kwargs: client,
    )

    result = await service.rerun_failed_executions(["tenant-a", "tenant-b"])
    tasks = list(service._background_tasks)

    assert result.rerun_count == 2
    await asyncio.gather(*tasks)
    assert calls == [
        "http://gateway/api/job-1/run",
        "http://gateway/api/job-2/run",
    ]


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
