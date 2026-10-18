# -*- coding: utf-8 -*-
"""Monitor 新看板的严格来源角色、缺表失败和只读接口验证。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_batch_retry import failed_batch


@pytest.mark.asyncio
async def test_board_api_does_not_fallback_on_missing_schema(callback_setup):
    from monitor.app.routers import managed_cron_board as api
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    db = callback_setup[0]
    _, batch = await failed_batch(callback_setup)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.board_service] = lambda: ManagedBoardQuery(db)
    path = "/api/monitor/cron/managed-dispatch/batches"
    headers = {
        "X-Source-Id": "bank",
        "X-User-Id": "alice",
        "X-User-Role": "manager",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (
            await client.get(path, headers={**headers, "X-User-Role": "user"})
        ).status_code == 403
        assert (await client.get(path, headers=headers)).json()["total"] == 1
        assert (
            await client.get(path, headers=headers, params={"page": 0})
        ).status_code == 422
        assert (
            await client.get(
                path,
                headers=headers,
                params={"start_time": "2026-10-18", "end_time": "2026-10-17"},
            )
        ).status_code == 400
        assert (
            await client.get(
                path + "/" + batch,
                headers={**headers, "X-Source-Id": "other"},
            )
        ).status_code == 404
        db.conn.execute("DROP TABLE swe_cron_managed_dispatch_events")
        assert (await client.get(path, headers=headers)).status_code == 503
