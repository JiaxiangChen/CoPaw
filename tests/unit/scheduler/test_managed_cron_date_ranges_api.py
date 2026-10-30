# -*- coding: utf-8 -*-
"""日期配置沿用用户 Header，维护入口只接受来源并同步返回当次结果。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_dispatch_state import (
    group,
    mode_setup,
)


@pytest.mark.asyncio
async def test_date_configuration_and_headerless_maintenance_http(mode_setup):
    from scheduler.app.routers import (
        api_router,
        managed_cron_controls,
        managed_cron,
    )
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )
    from scheduler.app.services.cron.managed.controls import ControlService

    db, _, management = mode_setup
    job = await group(mode_setup)
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[managed_cron.get_managed_repository] = (
        lambda: ManagedCronRepository(db)
    )
    app.dependency_overrides[managed_cron_controls.management_database] = (
        lambda: db
    )
    app.dependency_overrides[managed_cron_controls.control_service] = (
        lambda: ControlService(db, management.platform)
    )
    version = db.conn.execute(
        "SELECT management_version FROM swe_cron_job_states WHERE job_id=?",
        (job,),
    ).fetchone()[0]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        saved = await client.put(
            f"/api/scheduler/cron/jobs/{job}/run-date-range",
            headers={"X-User-Id": "alice", "X-Source-Id": "bank"},
            json={
                "request_id": "dates-http",
                "expected_management_version": version,
                "run_date_range": {
                    "start_date": "2000-01-01",
                    "end_date": "2000-01-02",
                },
            },
        )
        assert saved.status_code == 200
        assert saved.json()["operation"]["status"] == "completed"
        children = await client.get(
            f"/api/scheduler/cron/jobs/{job}/broadcast/children",
            headers={"X-User-Id": "alice", "X-Source-Id": "bank"},
        )
        assert children.status_code == 200
        assert children.json()["items"]
        assert all(
            item.get("run_date_range")
            == {"start_date": "2000-01-01", "end_date": "2000-01-02"}
            for item in children.json()["items"]
        )
        missing_source = await client.post(
            "/api/scheduler/cron/date-ranges/apply",
            json={},
        )
        assert missing_source.status_code == 422
        # 维护服务用真实日期算法与 SQL，只在平台边界替换 HTTP。
        from scheduler.app.routers import managed_cron_date_ranges
        from scheduler.app.services.cron.managed.date_ranges import (
            DateMaintenanceService,
        )

        app.dependency_overrides[managed_cron_date_ranges.date_service] = (
            lambda: DateMaintenanceService(db, management.platform)
        )
        applied = await client.post(
            "/api/scheduler/cron/date-ranges/apply",
            json={"source_id": "bank"},
        )
        assert applied.status_code == 200
        assert applied.json()["failed_count"] == 0
        assert applied.json()["scanned_count"] > 1
