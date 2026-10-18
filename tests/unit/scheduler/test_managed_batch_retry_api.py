# -*- coding: utf-8 -*-
"""新重试接口只认可信 Header，拒绝 Body 注入权限。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_batch_retry import (
    failed_batch,
    retry_body,
)


@pytest.mark.asyncio
async def test_batch_operations_headers_idempotency_and_strict_body(
    callback_setup,
):
    from scheduler.app.routers import managed_batch_operations as api
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(callback_setup[0])
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.batch_service] = lambda: service
    headers = {
        "X-User-Id": caller.actor_id,
        "X-Source-Id": caller.source_id,
        "X-User-Role": "manager",
    }
    path = f"/api/scheduler/cron/managed-dispatch/batches/{batch}"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (
            await client.get(
                path + "/failures",
                headers={**headers, "X-User-Role": "user"},
            )
        ).status_code == 403
        assert (
            await client.get(
                path + "/failures",
                headers={**headers, "X-Source-Id": "other"},
            )
        ).status_code == 404
        preview = (
            await client.get(path + "/failures", headers=headers)
        ).json()
        command = retry_body(preview).model_dump()
        assert (
            await client.post(
                path + "/retry",
                headers=headers,
                json={**command, "source_id": "other"},
            )
        ).status_code == 422
        first = await client.post(
            path + "/retry",
            headers=headers,
            json=command,
        )
        assert first.status_code == 200
        assert first.json()["queued"] == 1
        assert (
            await client.post(path + "/retry", headers=headers, json=command)
        ).json()["reused"]
        assert (
            await client.post(
                path + "/retry",
                headers=headers,
                json={**command, "confirm_stopped": True},
            )
        ).status_code == 409
