# -*- coding: utf-8 -*-
"""Internal auth migration request, authentication, and lifecycle contracts."""

import importlib
import inspect

import httpx
import pytest
from fastapi import FastAPI

from tests.unit.app.test_cron_auth_migration import write
from tests.unit.app.test_cron_auth_migration_tasks import ProgressDatabase


def module():
    spec = importlib.util.find_spec("swe.app.crons.auth_migration_api")
    assert spec is not None, "auth migration API is missing"
    return importlib.import_module(spec.name)


@pytest.mark.asyncio
async def test_api_submit_query_strict_validation_and_readiness(
    tmp_path,
    monkeypatch,
):
    m = module()
    monkeypatch.setattr(m, "working_root", lambda: tmp_path)
    monkeypatch.setattr(m, "verify_internal_access", lambda request: None)
    app = FastAPI()
    app.include_router(m.router, prefix="/api")
    app.state.db_connection = ProgressDatabase()
    write(tmp_path, "A", {"cookie_header": "COOKIE_SECRET"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for body in [
            {"operation": "prune"},
            {"operation": "preview", "page_size": True},
            {"operation": "preview", "tenant_ids": []},
            {"operation": "other"},
        ]:
            assert (
                await client.post(
                    "/api/internal/cron/auth-migration/tasks",
                    json=body,
                )
            ).status_code == 422
        response = await client.post(
            "/api/internal/cron/auth-migration/tasks",
            json={"operation": "import", "storage_scope_ids": ["A"]},
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        await app.state.cron_auth_migration_tasks.wait()
        response = await client.get(
            "/api/internal/cron/auth-migration/tasks/" + task_id,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"
        assert "COOKIE_SECRET" not in response.text
        assert (
            await client.get(
                "/api/internal/cron/auth-migration/tasks/" + "0" * 32,
            )
        ).status_code == 404
        assert (
            await client.get(
                "/api/internal/cron/auth-migration/tasks/"
                + task_id
                + "?limit=0",
            )
        ).status_code == 422
        app.state.db_connection.failure = RuntimeError("SQL_SECRET")
        response = await client.post(
            "/api/internal/cron/auth-migration/tasks",
            json={"operation": "verify"},
        )
        assert response.status_code == 503
        assert "SQL_SECRET" not in response.text


@pytest.mark.asyncio
async def test_api_reuses_internal_token_header_priority(
    tmp_path,
    monkeypatch,
):
    m = module()
    internal = importlib.import_module("swe.app.routers.internal")
    monkeypatch.setattr(internal, "_INTERNAL_TOKEN", "configured")
    monkeypatch.setattr(m, "working_root", lambda: tmp_path)
    app = FastAPI()
    app.include_router(m.router, prefix="/api")
    app.state.db_connection = ProgressDatabase()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        path = "/api/internal/cron/auth-migration/tasks"
        assert (
            await client.post(path, json={"operation": "verify"})
        ).status_code == 401
        response = await client.post(
            path,
            json={"operation": "verify"},
            headers={
                "X-Internal-Token": "Bearer configured",
                "Authorization": "Bearer login-jwt",
            },
        )
        assert response.status_code == 202
        await app.state.cron_auth_migration_tasks.wait()


def test_router_registration_and_shutdown_before_db_close():
    m = module()
    routers = importlib.import_module("swe.app.routers")
    assert ("..crons.auth_migration_api", "router") in routers._ROUTER_MODULES
    app_module = importlib.import_module("swe.app._app")
    source = inspect.getsource(app_module._shutdown_lifespan_resources)
    assert source.index("cron_auth_migration_tasks") < source.index(
        "_shutdown_lifespan_resources_impl",
    )
    assert m.router.prefix == "/internal/cron/auth-migration"


@pytest.mark.asyncio
async def test_validation_does_not_echo_credential_fields(monkeypatch):
    m = module()
    monkeypatch.setattr(m, "verify_internal_access", lambda request: None)
    app = FastAPI()
    app.include_router(m.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/internal/cron/auth-migration/tasks",
            json={
                "operation": "preview",
                "auth_token": "TOKEN_SECRET",
                "cookie_header": "COOKIE_SECRET",
                "user_info": {"name": "USER_SECRET"},
                "last_error": "ERROR_SECRET",
            },
        )
    assert response.status_code == 422
    assert not any(
        secret in response.text
        for secret in [
            "TOKEN_SECRET",
            "COOKIE_SECRET",
            "USER_SECRET",
            "ERROR_SECRET",
        ]
    )


@pytest.mark.asyncio
async def test_shutdown_closes_runner_before_cleanup_failure_and_clears_mirror(
    monkeypatch,
):
    app_module = importlib.import_module("swe.app._app")
    mirror = importlib.import_module("swe.app.crons.auth_mirror")
    events = []

    class Runner:
        async def close(self):
            events.append("runner")

    async def fail(app, db):
        events.append("cleanup")
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(app_module, "_shutdown_lifespan_resources_impl", fail)
    monkeypatch.setattr(
        mirror,
        "set_cron_auth_repository",
        lambda repo: events.append(("mirror", repo)),
    )
    app = FastAPI()
    app.state.cron_auth_migration_tasks = Runner()
    with pytest.raises(RuntimeError, match="cleanup failure"):
        await app_module._shutdown_lifespan_resources(app, None)
    assert events == ["runner", "cleanup", ("mirror", None)]
