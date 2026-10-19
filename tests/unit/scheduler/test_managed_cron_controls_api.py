# -*- coding: utf-8 -*-
"""验证新版控制接口沿用完整的 Header 身份契约。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_controls import version
from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)

HEADERS = {"X-User-Id": "alice", "X-Tenant-Id": "alice", "X-Source-Id": "bank"}


@pytest.mark.asyncio
async def test_new_manual_and_control_routes_use_durable_operations(
    execution_setup,
):
    from scheduler.app.routers import managed_cron_controls
    from scheduler.app.services.cron.managed.controls import ControlService

    db, _, management = execution_setup
    row, _ = await enqueue(execution_setup)
    app = FastAPI()
    app.include_router(managed_cron_controls.router, prefix="/api")
    app.dependency_overrides[managed_cron_controls.management_database] = (
        lambda: db
    )
    app.dependency_overrides[managed_cron_controls.control_service] = (
        lambda: ControlService(db, management.platform)
    )
    path = f"/api/scheduler/cron/jobs/{row['job_id']}"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        manual = await client.post(
            path + "/run",
            headers=HEADERS,
            json={
                "request_id": "manual-click",
                "expected_definition_version": 1,
            },
        )
        assert manual.status_code == 202
        assert manual.json()["execution"]["status"] == "queued"
        again = await client.post(
            path + "/run",
            headers=HEADERS,
            json={
                "request_id": "second-click",
                "expected_definition_version": 1,
            },
        )
        assert again.json()["reused"]
        assert (
            again.json()["execution"]["execution_id"]
            == manual.json()["execution"]["execution_id"]
        )
        disabled = await client.put(
            path + "/enabled",
            headers=HEADERS,
            json={
                "request_id": "pause",
                "enabled": False,
                "expected_management_version": version(db),
            },
        )
        assert disabled.status_code == 200
        blocked = await client.post(
            path + "/run",
            headers=HEADERS,
            json={
                "request_id": "while-disabled",
                "expected_definition_version": 1,
            },
        )
        assert blocked.status_code == 409
        read = await client.post(
            path + "/read",
            headers=HEADERS,
            json={"request_id": "read"},
        )
        assert read.status_code == 200
        status = await client.get(
            path + "/executions/" + manual.json()["execution"]["execution_id"],
            headers=HEADERS,
        )
        assert status.status_code == 200


@pytest.mark.asyncio
async def test_control_header_scope_and_version_validation(execution_setup):
    from scheduler.app.routers import managed_cron_controls
    from scheduler.app.services.cron.managed.controls import ControlService

    db, _, management = execution_setup
    row, _ = await enqueue(execution_setup)
    app = FastAPI()
    app.include_router(managed_cron_controls.router, prefix="/api")
    app.dependency_overrides[managed_cron_controls.management_database] = (
        lambda: db
    )
    app.dependency_overrides[managed_cron_controls.control_service] = (
        lambda: ControlService(db, management.platform)
    )
    path = f"/api/scheduler/cron/jobs/{row['job_id']}/enabled"
    body = {
        "request_id": "pause",
        "enabled": False,
        "expected_management_version": version(db),
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (
            await client.put(
                path,
                headers={**HEADERS, "X-User-Id": "bob"},
                json=body,
            )
        ).status_code == 404
        assert (
            await client.put(
                path,
                headers={**HEADERS, "X-Scope-Id": "wrong"},
                json=body,
            )
        ).status_code == 400
        assert (
            await client.put(
                path,
                headers=HEADERS,
                json={**body, "enabled": "false"},
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_common_operation_retry_resumes_control_without_swe_preparation(
    execution_setup,
):
    from scheduler.app.routers import managed_cron_management
    from scheduler.app.services.cron.managed.controls import ControlService

    db, caller, management = execution_setup
    row, _ = await enqueue(execution_setup)
    management.platform.fail_enable = True
    failed = await ControlService(db, management.platform).set_enabled(
        caller,
        row["job_id"],
        {
            "request_id": "resume",
            "enabled": True,
            "expected_management_version": version(db),
        },
    )
    assert failed["status"] == "failed"
    management.platform.fail_enable = False
    before = len(management.resources.prepared)
    app = FastAPI()
    app.include_router(managed_cron_management.router, prefix="/api")
    app.dependency_overrides[
        managed_cron_management.get_management_service
    ] = lambda: management
    path = f"/api/scheduler/cron/jobs/{row['job_id']}/operations/{failed['operation_id']}/retry"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        result = await client.post(path, headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["operation"]["status"] == "completed"
    assert len(management.resources.prepared) == before


@pytest.mark.asyncio
async def test_selected_children_action_api_returns_per_target_result(
    execution_setup,
):
    from scheduler.app.routers import managed_cron_controls
    from scheduler.app.routers.managed_cron_management import (
        get_management_service,
    )

    db, _, management = execution_setup
    db.add("source")
    db.add(
        "child",
        tenant="bob",
        actor="bob",
        parent=("alice", "bank", "default", "source"),
    )
    app = FastAPI()
    app.include_router(managed_cron_controls.router, prefix="/api")
    app.dependency_overrides[managed_cron_controls.management_database] = (
        lambda: db
    )
    app.dependency_overrides[get_management_service] = lambda: management
    body = {
        "request_id": "delete-selected",
        "action": "delete",
        "items": [
            {
                "tenant_id": "bob",
                "job_id": "child",
                "expected_definition_version": 1,
            },
        ],
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/scheduler/cron/jobs/source/broadcast/children/actions",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 202
        assert response.json()["pending_count"] == 1
        assert response.json()["deleted_count"] == 0
        assert response.json()["operation_status"] == "pending"
        assert response.json()["results"] == []
