# -*- coding: utf-8 -*-
"""验证分发受理、指定结果及 current 的 HTTP 契约和 Header 隔离。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_distribution import (
    distribution_setup,
    payload,
    source,
)

HEADERS = {"X-User-Id": "alice", "X-Tenant-Id": "alice", "X-Source-Id": "bank"}


@pytest.mark.asyncio
async def test_broadcast_http_acceptance_pagination_current_and_schema_validation(
    distribution_setup,
):
    from scheduler.app.routers import managed_cron_broadcast

    db, _, _ = distribution_setup
    created = await source(distribution_setup)
    app = FastAPI()
    app.include_router(managed_cron_broadcast.router, prefix="/api")
    app.dependency_overrides[managed_cron_broadcast.management_database] = (
        lambda: db
    )
    path = f"/api/scheduler/cron/jobs/{created['job_id']}/broadcast"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (
            await client.get(path + "/tasks/current", headers=HEADERS)
        ).json() == {"task": None}
        accepted = await client.post(path, headers=HEADERS, json=payload())
        assert accepted.status_code == 202
        task_id = accepted.json()["task_id"]
        page = await client.get(
            path + "/tasks/" + task_id + "?limit=1",
            headers=HEADERS,
        )
        assert page.status_code == 200
        assert page.json()["tenant_count"] == 2
        assert len(page.json()["results"]) == 1
        current = await client.get(path + "/tasks/current", headers=HEADERS)
        assert current.json()["task"]["task_id"] == task_id
        changed_mode = await client.post(
            path,
            headers=HEADERS,
            json={**payload("new"), "enable_batch_dispatch": True},
        )
        assert changed_mode.status_code == 422
        forged = await client.get(
            path + "/tasks/" + task_id,
            headers={**HEADERS, "X-User-Id": "bob"},
        )
        assert forged.status_code == 404
