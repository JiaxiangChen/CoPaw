# -*- coding: utf-8 -*-
"""Migration routes honor existing authentication and do not initialize tenants."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from swe.app.crons import migration_api
from tests.unit.app.test_cron_migration_repository import FakeDatabase
from tests.unit.app.test_cron_migration_source import write_jobs


def client(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(migration_api.router, prefix="/api")
    db = SimpleNamespace(is_connected=True)
    app.state.db_connection = db
    monkeypatch.setattr(migration_api, "working_root", lambda: tmp_path)
    return TestClient(app), app, db


def test_missing_tables_returns_503_without_exposing_exception(
    tmp_path, monkeypatch
):
    c, app, db = client(tmp_path, monkeypatch)

    async def execute(*args):
        raise RuntimeError("credential=DO_NOT_EXPOSE")

    db.execute = execute
    monkeypatch.setattr(
        migration_api, "verify_internal_access", lambda request: None
    )
    response = c.post("/api/internal/cron/migration/preview", json={})
    assert response.status_code == 503
    assert "DO_NOT_EXPOSE" not in response.text
    assert not hasattr(app.state, "workspace")


@pytest.mark.parametrize("suffix", ["preview", "import", "verify"])
def test_rejects_arbitrary_root_and_invalid_limits_before_work(
    tmp_path, monkeypatch, suffix
):
    c, _, _ = client(tmp_path, monkeypatch)
    for body in ({"root": "C:/"}, {"limit": 0}, {"source_ids": []}):
        assert (
            c.post(
                f"/api/internal/cron/migration/{suffix}", json=body
            ).status_code
            == 422
        )


def test_existing_internal_token_check_happens_before_database_access(
    tmp_path, monkeypatch
):
    c, _, db = client(tmp_path, monkeypatch)
    from fastapi import HTTPException

    def reject(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    monkeypatch.setattr(migration_api, "verify_internal_access", reject)
    response = c.post("/api/internal/cron/migration/import", json={})
    assert response.status_code == 401


def test_real_http_source_service_and_repository_roundtrip(
    tmp_path, monkeypatch
):
    c, app, _ = client(tmp_path, monkeypatch)
    db = FakeDatabase()
    app.state.db_connection = db
    path = write_jobs(tmp_path)
    before = path.read_bytes(), path.stat().st_mtime_ns
    from swe.app.routers import internal

    monkeypatch.setattr(internal, "_INTERNAL_TOKEN", "")
    assert (
        c.post("/api/internal/cron/migration/preview", json={}).json()[
            "counts"
        ]["would_insert"]
        == 1
    )
    assert not any(q.startswith(("INSERT", "UPDATE")) for q, _ in db.sql)
    assert (
        c.post("/api/internal/cron/migration/import", json={}).json()[
            "counts"
        ]["inserted"]
        == 1
    )
    assert (
        c.post("/api/internal/cron/migration/import", json={}).json()[
            "counts"
        ]["unchanged"]
        == 1
    )
    assert (
        c.post("/api/internal/cron/migration/verify", json={}).json()[
            "counts"
        ]["matched"]
        == 1
    )
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not hasattr(app.state, "multi_agent_manager")


def test_real_login_and_internal_credentials_remain_separate(
    tmp_path, monkeypatch
):
    from swe.app import auth
    from swe.app.routers import internal

    c, app, _ = client(tmp_path, monkeypatch)
    app.state.db_connection = FakeDatabase()
    app.add_middleware(auth.AuthMiddleware)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "has_registered_users", lambda: True)
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda token: "admin" if token == "login-token" else None,
    )
    monkeypatch.setattr(internal, "_INTERNAL_TOKEN", "internal-token")
    url = "/api/internal/cron/migration/preview"
    assert c.post(url, json={}).status_code == 401
    assert (
        c.post(
            url, json={}, headers={"Authorization": "Bearer login-token"}
        ).status_code
        == 401
    )
    assert (
        c.post(
            url,
            json={},
            headers={
                "Authorization": "Bearer login-token",
                "X-Internal-Token": "Bearer wrong",
            },
        ).status_code
        == 401
    )
    assert (
        c.post(
            url,
            json={},
            headers={
                "Authorization": "Bearer login-token",
                "X-Internal-Token": "Bearer internal-token",
            },
        ).status_code
        == 200
    )


def test_service_requires_existing_shared_connection_without_loading_credentials(
    tmp_path, monkeypatch
):
    c, app, _ = client(tmp_path, monkeypatch)
    del app.state.db_connection
    called = []

    async def unexpected_configuration_lookup(request):
        called.append(True)
        return None

    monkeypatch.setattr(
        "swe.app.async_tasks.db.get_or_create_async_task_db",
        unexpected_configuration_lookup,
    )
    monkeypatch.setattr(
        migration_api, "verify_internal_access", lambda request: None
    )
    assert (
        c.post("/api/internal/cron/migration/preview", json={}).status_code
        == 503
    )
    assert not called


def test_router_is_registered_without_bootstrapping_runtime():
    from swe.app import routers

    assert ("..crons.migration_api", "router") in routers._ROUTER_MODULES


def test_verify_missing_source_is_paginated_and_never_deletes_target(
    tmp_path, monkeypatch
):
    import copy

    c, app, _ = client(tmp_path, monkeypatch)
    db = FakeDatabase()
    app.state.db_connection = db
    from swe.app.routers import internal

    monkeypatch.setattr(internal, "_INTERNAL_TOKEN", "")
    path = write_jobs(tmp_path)
    assert (
        c.post("/api/internal/cron/migration/import", json={}).status_code
        == 200
    )
    path.unlink()
    before = copy.deepcopy(db.tables)
    first = c.post("/api/internal/cron/migration/verify", json={}).json()
    second = c.post(
        "/api/internal/cron/migration/verify",
        json={"cursor": first["next_cursor"]},
    ).json()
    assert second["phase"] == "database"
    assert second["counts"]["source_missing"] == 1
    assert second["next_cursor"] is None
    assert db.tables == before
