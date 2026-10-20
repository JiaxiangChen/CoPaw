# -*- coding: utf-8 -*-
"""验证三态接口与公共管理操作重试路由衔接。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_dispatch_state import (
    group,
    mode_setup,
)

HEADERS = {"X-User-Id": "alice", "X-Tenant-Id": "alice", "X-Source-Id": "bank"}


@pytest.mark.asyncio
async def test_mode_http_acceptance_and_common_retry(mode_setup):
    from scheduler.app.routers import (
        managed_cron_dispatch_state,
        managed_cron_management,
    )
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )

    db, _, management = mode_setup
    job_id = await group(mode_setup)
    service = DispatchStateService(db, management.platform)
    app = FastAPI()
    app.include_router(managed_cron_dispatch_state.router, prefix="/api")
    app.include_router(managed_cron_management.router, prefix="/api")
    app.dependency_overrides[
        managed_cron_dispatch_state.management_database
    ] = lambda: db
    app.dependency_overrides[managed_cron_dispatch_state.dispatch_service] = (
        lambda: service
    )
    app.dependency_overrides[
        managed_cron_management.get_management_service
    ] = lambda: management
    path = f"/api/scheduler/cron/jobs/{job_id}"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get(path + "/dispatch-state", headers=HEADERS)
        accepted = await client.put(
            path + "/dispatch-state",
            headers=HEADERS,
            json={
                "request_id": "batch",
                "state": "batch_enabled",
                "expected_version": initial.json()["version"],
            },
        )
        assert accepted.status_code == 202
        operation_id = accepted.json()["operation_id"]
        management.platform.fail_enable = True
        await service.process(operation_id)
        management.platform.fail_enable = False
        retried = await client.post(
            path + f"/operations/{operation_id}/retry",
            headers=HEADERS,
        )
        assert retried.status_code == 200
        assert retried.json()["operation"]["status"] == "completed"
        rejected = await client.put(
            path + "/dispatch-state",
            headers=HEADERS,
            json={
                "request_id": "illegal-offset",
                "state": "ordinary",
                "expected_version": 2,
                "enable_offset": True,
            },
        )
        assert rejected.status_code == 422
