# -*- coding: utf-8 -*-
"""已知后台不可用时明确拒绝新工作，保留查询和紧急停用能力。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_dispatch_state import mode_setup
from tests.unit.scheduler.test_managed_cron_management import create_payload


@pytest.mark.asyncio
async def test_unavailable_runtime_rejects_new_work_but_keeps_reads_and_disabling(
    mode_setup,
):
    from scheduler.app.routers import (
        api_router,
        managed_cron,
        managed_cron_controls,
        managed_cron_management,
        managed_cron_dispatch_state,
    )
    from scheduler.app.services.cron.managed.controls import ControlService
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )

    db, caller, management = mode_setup
    created = await management.create(caller, create_payload())
    app = FastAPI()
    app.state.managed_cron_runtime_prepared = True
    app.state.managed_cron_runtime_ready = False
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[
        managed_cron_management.get_management_service
    ] = lambda: management
    app.dependency_overrides[managed_cron_management.management_database] = (
        lambda: db
    )
    app.dependency_overrides[managed_cron.get_managed_repository] = (
        lambda: ManagedCronRepository(db)
    )
    app.dependency_overrides[managed_cron_controls.control_service] = (
        lambda: ControlService(db, management.platform)
    )
    app.dependency_overrides[managed_cron_dispatch_state.dispatch_service] = (
        lambda: DispatchStateService(db, management.platform)
    )
    headers = {
        "X-User-Id": "alice",
        "X-Tenant-Id": "alice",
        "X-Source-Id": "bank",
    }
    root = f"/api/scheduler/cron/jobs/{created['job_id']}"
    requests = [
        ("POST", "/api/scheduler/cron/jobs", create_payload("new-create")),
        (
            "PUT",
            root,
            {
                "request_id": "new-update",
                "expected_definition_version": 1,
                "spec": create_payload()["spec"],
            },
        ),
        (
            "POST",
            root + "/broadcast",
            {
                "request_id": "new-broadcast",
                "expected_definition_version": 1,
                "targets": [{"tenant_id": "bob"}],
            },
        ),
        (
            "PUT",
            root + "/dispatch-state",
            {
                "request_id": "new-mode",
                "expected_version": 1,
                "state": "batch_enabled",
            },
        ),
        (
            "POST",
            root + "/run",
            {"request_id": "new-run", "expected_definition_version": 1},
        ),
        (
            "PUT",
            root + "/enabled",
            {
                "request_id": "new-resume",
                "enabled": True,
                "expected_management_version": 1,
            },
        ),
        (
            "POST",
            root + "/broadcast/children/actions",
            {
                "request_id": "new-child-run",
                "action": "run",
                "items": [
                    {
                        "job_id": created["job_id"],
                        "tenant_id": "alice",
                        "agent_id": "default",
                        "expected_definition_version": 1,
                    },
                ],
            },
        ),
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        for method, path, body in requests:
            result = await http.request(
                method,
                path,
                headers=headers,
                json=body,
            )
            assert result.status_code == 503, result.text
            assert result.json()["detail"] == "cron_managed_runtime_not_ready"
        assert (await http.get(root, headers=headers)).status_code == 200
        version = db.conn.execute(
            "SELECT management_version FROM swe_cron_job_states WHERE job_id=?",
            (created["job_id"],),
        ).fetchone()[0]
        stopped = await http.put(
            root + "/enabled",
            headers=headers,
            json={
                "request_id": "emergency-stop",
                "enabled": False,
                "expected_management_version": version,
            },
        )
        assert stopped.status_code == 200
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_managed_broadcast_tasks",
        ).fetchone()[0]
        == 0
    )
