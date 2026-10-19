# -*- coding: utf-8 -*-
"""验证异步删除受理、只读进度、完整Header及严格入参约束。"""

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
        assert result.status_code == 202
        assert result.json()["operation_status"] == "pending"
        assert result.json()["completed_count"] == 0
        assert result.json()["pending_count"] == 2
        assert not service.management.resources.cleaned
        operation_id = result.json()["operation_id"]
        progress = await http.get(
            f"{path}/tasks/{operation_id}",
            headers=headers,
        )
        assert progress.status_code == 200
        assert progress.json() == result.json()
        assert (
            await http.get(f"{path}/tasks/current", headers=headers)
        ).json()["task"] == result.json()
        assert (
            await http.post(path, headers=headers, json=body)
        ).json() == result.json()
        from scheduler.app.services.cron.managed.async_batch_delete import (
            AsyncBatchDelete,
        )

        await AsyncBatchDelete(service.db, service.management).process(
            operation_id,
        )
        done = await http.get(f"{path}/tasks/{operation_id}", headers=headers)
        assert done.json()["completed_count"] == 2
        assert (
            await http.get(
                f"{path}/tasks/{operation_id}",
                headers={**headers, "X-User-Id": "bob"},
            )
        ).status_code == 404
        large = {
            "request_id": "delete-all-snapshot",
            "items": [
                {**body["items"][0], "job_id": f"fake-{n}"}
                for n in range(1001)
            ],
        }
        assert (
            await http.post(path, headers=headers, json=large)
        ).status_code == 422
        all_path = path.removesuffix("batch-delete") + "delete-all"
        full = await http.post(all_path, headers=headers, json=large)
        assert full.status_code == 202
        assert (
            full.json()["total_count"] == full.json()["pending_count"] == 1001
        )
        assert len(service.management.resources.cleaned) == 2
        app.state.managed_cron_runtime_prepared = True
        app.state.managed_cron_runtime_ready = False
        assert (
            await http.post(all_path, headers=headers, json=large)
        ).status_code == 503
