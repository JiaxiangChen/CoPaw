# -*- coding: utf-8 -*-
"""验证平台封装适配不会改变旧入口，新入口校验稳定触发与身份。"""

import base64
import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_callbacks import (
    body,
    callback_setup,
    create_source,
)


@pytest.mark.asyncio
async def test_callback_http_supports_existing_jobparam_and_full_header_identity(
    callback_setup,
):
    from scheduler.app.routers import managed_callbacks
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, _, _ = callback_setup
    created = await create_source(callback_setup)
    command = body(db, created)
    service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    app = FastAPI()
    app.include_router(managed_callbacks.router, prefix="/api")
    app.dependency_overrides[managed_callbacks.scheduling_service] = (
        lambda: service
    )
    headers = {
        "X-User-Id": "alice",
        "X-Tenant-Id": "alice",
        "X-Source-Id": "bank",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/api/scheduler/cron/ordinary/callback",
            json=command,
            headers=headers,
        )
        assert first.status_code == 202
        assert first.json()["accepted"]
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                {
                    **command,
                    "tenant_id": "alice",
                    "source_id": "bank",
                    "agent_id": "default",
                    "fromId": "alice",
                },
            ).encode(),
        ).decode()
        another = await client.post(
            "/api/scheduler/cron/ordinary/callback",
            json={"jobParam": encoded},
        )
        assert another.status_code == 202
        assert another.json()["accepted"] and not another.json()["reused"]
        assert another.json()["execution_id"] != first.json()["execution_id"]
        wrong = await client.post(
            "/api/scheduler/cron/ordinary/callback",
            json={"jobParam": encoded},
            headers={**headers, "X-Source-Id": "other"},
        )
        assert wrong.status_code == 400
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 2
    )
