# -*- coding: utf-8 -*-
"""验证新版管理接口经真实 SQL 调用外部服务适配边界。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.unit.scheduler.test_managed_cron_management import (
    ManagementDatabase,
    PlatformClient,
    ResourceClient,
    create_payload,
)


@pytest.mark.asyncio
async def test_create_update_delete_and_operation_queries_are_wired():
    from scheduler.app.routers import managed_cron_management as routes
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    db = ManagementDatabase()
    resources, platform = ResourceClient(), PlatformClient()
    service = ManagementService(db, resources, platform)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.get_management_service] = lambda: service
    app.dependency_overrides[routes.management_database] = lambda: db
    headers = {"X-User-Id": "alice", "X-Source-Id": "bank"}
    headers.update({"X-Bbk-Id": "007", "X-User-Name": "%E5%BC%A0%E4%B8%89"})
    root = "/api/scheduler/cron/jobs"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            root,
            headers=headers,
            json=create_payload(),
        )
        assert created.status_code == 201
        import json

        stored = json.loads(
            db.conn.execute(
                "SELECT spec_json FROM swe_cron_jobs_v2",
            ).fetchone()[0],
        )
        assert stored["bbk_id"] == "007"
        assert stored["tenant_name"] == "张三"
        op = created.json()["operation"]
        path = f"{root}/{op['job_id']}"
        assert (
            await client.get(
                f"{path}/operations/{op['operation_id']}",
                headers=headers,
            )
        ).json()["status"] == "completed"
        updated = await client.put(
            path,
            headers=headers,
            json={
                "request_id": "update",
                "expected_definition_version": 1,
                "spec": create_payload(name="changed")["spec"],
            },
        )
        assert updated.status_code == 200
        assert (
            await client.request(
                "DELETE",
                path,
                headers=headers,
                json={
                    "request_id": "delete",
                    "expected_definition_version": 2,
                },
            )
        ).status_code == 200
    db.conn.close()


@pytest.mark.asyncio
async def test_http_failures_expose_progress_without_private_errors():
    from scheduler.app.routers import managed_cron_management as routes
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )

    db = ManagementDatabase()
    resources = ResourceClient()
    resources.fail_prepare = True
    app = FastAPI()
    app.include_router(routes.router)
    service = ManagementService(db, resources, PlatformClient())
    app.dependency_overrides[routes.get_management_service] = lambda: service
    app.dependency_overrides[routes.management_database] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        root = "/scheduler/cron/jobs"
        headers = {"X-User-Id": "alice", "X-Source-Id": "bank"}
        failed = await client.post(
            root,
            headers=headers,
            json=create_payload(),
        )
        assert failed.status_code == 503
        assert "private token" not in failed.text
        op = failed.json()["operation"]
        pending_path = f"{root}/{op['job_id']}/operations/pending"
        assert (await client.get(pending_path, headers=headers)).json()[
            "operation"
        ]["operation_id"] == op["operation_id"]
        resources.fail_prepare = False
        retry = await client.post(
            f"{root}/{op['job_id']}/operations/{op['operation_id']}/retry",
            headers=headers,
        )
        assert retry.status_code == 200
        assert retry.json()["operation"]["status"] == "completed"
        assert (await client.get(pending_path, headers=headers)).json()[
            "operation"
        ] is None
        bad = await client.post(
            root,
            headers=headers,
            json={**create_payload("bad"), "unexpected": True},
        )
        assert bad.status_code == 422
        invalid_zone = create_payload("bad-timezone")
        invalid_zone["spec"]["schedule"]["timezone"] = "Not/A-Timezone"
        assert (
            await client.post(root, headers=headers, json=invalid_zone)
        ).status_code == 400
        assert (
            await client.post(root, json=create_payload())
        ).status_code == 400
    db.conn.close()
