# -*- coding: utf-8 -*-
"""验证同步批量删除的完整 Header、逐项结果及严格入参约束。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_batch_delete import setup_batch
from tests.unit.scheduler.test_managed_cron_dispatch_state import mode_setup


@pytest.mark.asyncio
async def test_batch_delete_http_returns_all_results_and_rejects_invalid_selection(
    mode_setup,
):
    from scheduler.app.routers import (
        managed_cron_controls,
        managed_cron_management,
    )

    service, _, parent, body = await setup_batch(mode_setup)
    app = FastAPI()
    app.include_router(managed_cron_controls.router, prefix="/api")
    app.dependency_overrides[
        managed_cron_management.get_management_service
    ] = lambda: service.management
    path = f"/api/scheduler/cron/jobs/{parent}/broadcast/children/batch-delete"
    headers = {
        "X-User-Id": "alice",
        "X-Tenant-Id": "alice",
        "X-Source-Id": "bank",
        "X-Agent-Id": "default",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        missing_agent = {
            **body,
            "items": [
                {
                    key: value
                    for key, value in body["items"][0].items()
                    if key != "agent_id"
                },
            ],
        }
        assert (
            await http.post(path, headers=headers, json=missing_agent)
        ).status_code == 422
        assert (
            await http.post(path, headers=headers, json={**body, "items": []})
        ).status_code == 422
        assert (
            await http.post(
                path,
                headers={**headers, "X-User-Id": "bob"},
                json=body,
            )
        ).status_code == 404
        result = await http.post(path, headers=headers, json=body)
        assert result.status_code == 200
        assert result.json()["status"] == "completed"
        assert result.json()["completed_count"] == 2
        assert "task_id" not in result.json()
        assert (
            await http.post(path, headers=headers, json=body)
        ).json() == result.json()
