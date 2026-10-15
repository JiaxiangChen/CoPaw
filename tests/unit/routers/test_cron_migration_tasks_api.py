# -*- coding: utf-8 -*-
"""Postman-equivalent requests trigger whole scans without manual cursors."""

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from swe.app.crons import migration_api
from tests.unit.app.test_cron_migration_tasks import TaskDatabase
from tests.unit.app.test_cron_migration_source import job, write_jobs

BASE = "/api/internal/cron/migration/tasks"


def app_with_db(tmp_path, monkeypatch, db):
    app = FastAPI()
    app.include_router(migration_api.router, prefix="/api")
    app.state.db_connection = db
    monkeypatch.setattr(migration_api, "working_root", lambda: tmp_path)
    from swe.app.routers import internal

    monkeypatch.setattr(internal, "_INTERNAL_TOKEN", "")
    return app


@pytest.mark.asyncio
async def test_one_post_imports_all_pages_and_other_instance_can_query(
    tmp_path, monkeypatch
):
    db = TaskDatabase()
    path = write_jobs(tmp_path, [job(id=f"job-{i}") for i in range(5)])
    before = path.read_bytes(), path.stat().st_mtime_ns
    app = app_with_db(tmp_path, monkeypatch, db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post(BASE, json={"operation": "import", "limit": 2})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        await app.state.cron_migration_tasks.wait()
        result = (await c.get(f"{BASE}/{task_id}", params={"limit": 1})).json()
        assert result["status"] == "succeeded"
        assert result["progress"]["counts"]["inserted"] == 5
        assert result["progress"]["pages_completed"] == 3
        assert result["progress"]["memory_incomplete"]
        assert result["next_after_page"] == 1
        second_app = app_with_db(tmp_path, monkeypatch, db)
        async with AsyncClient(
            transport=ASGITransport(app=second_app), base_url="http://other"
        ) as other:
            queried = await other.get(
                f"{BASE}/{task_id}", params={"after_page": 1}
            )
            assert queried.status_code == 200
            assert len(queried.json()["pages"]) == 2
        assert len(db.tables["swe_cron_jobs_v2"]) == 5
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        repeated = await c.post(BASE, json={"operation": "import", "limit": 2})
        await app.state.cron_migration_tasks.wait()
        checked = (await c.get(f"{BASE}/{repeated.json()['task_id']}")).json()
        assert checked["progress"]["counts"]["unchanged"] == 5
        verify = await c.post(BASE, json={"operation": "verify", "limit": 2})
        await app.state.cron_migration_tasks.wait()
        checked = (await c.get(f"{BASE}/{verify.json()['task_id']}")).json()
        assert checked["progress"]["scan_complete"]
        assert checked["progress"]["scanned_database_rows"] == 5
        assert checked["pages"][-1]["result"]["phase"] == "database"


@pytest.mark.asyncio
async def test_invalid_inputs_missing_tasks_and_authentication(
    tmp_path, monkeypatch
):
    app = app_with_db(tmp_path, monkeypatch, TaskDatabase())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        for body in (
            {"root": "/"},
            {"cursor": "skip"},
            {"operation": "delete"},
            {"limit": 201},
        ):
            assert (await c.post(BASE, json=body)).status_code == 422
        assert (await c.get(f"{BASE}/{'a' * 32}")).status_code == 404
        assert (await c.get(f"{BASE}/bad")).status_code == 422
        assert (await c.get(f"{BASE}/{'a' * 32}?limit=21")).status_code == 422
        from swe.app.routers import internal

        monkeypatch.setattr(internal, "_INTERNAL_TOKEN", "secret")
        assert (await c.post(BASE, json={})).status_code == 401
        assert (await c.get(f"{BASE}/{'a' * 32}")).status_code == 401
        assert (await c.post(f"{BASE}/{'a' * 32}/stop")).status_code == 401


@pytest.mark.asyncio
async def test_missing_progress_tables_does_not_import_or_leak(
    tmp_path, monkeypatch
):
    db = TaskDatabase()
    original = db.execute

    async def execute(query, params=None):
        if "swe_async_" in query:
            raise RuntimeError("DO_NOT_EXPOSE")
        return await original(query, params)

    db.execute = execute
    app = app_with_db(tmp_path, monkeypatch, db)
    write_jobs(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post(BASE, json={})
        assert response.status_code == 503
        assert "DO_NOT_EXPOSE" not in response.text
    assert not db.tables["swe_cron_jobs_v2"]


@pytest.mark.asyncio
async def test_preview_only_writes_operational_progress_not_business_tables(
    tmp_path, monkeypatch
):
    db = TaskDatabase()
    app = app_with_db(tmp_path, monkeypatch, db)
    write_jobs(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post(BASE, json={"operation": "preview"})
        assert response.status_code == 202
        await app.state.cron_migration_tasks.wait()
        result = (await c.get(f"{BASE}/{response.json()['task_id']}")).json()
        assert result["progress"]["counts"]["would_insert"] == 1
    assert all(not rows for rows in db.tables.values())
    assert db.tasks and db.pages


@pytest.mark.asyncio
async def test_stop_and_local_concurrency_cap(tmp_path, monkeypatch):
    db = TaskDatabase()
    app = app_with_db(tmp_path, monkeypatch, db)
    entered, release = asyncio.Event(), asyncio.Event()
    from swe.app.crons.migration_service import MigrationService

    original = MigrationService.run

    async def run(self, *args):
        entered.set()
        await release.wait()
        return await original(self, *args)

    monkeypatch.setattr(MigrationService, "run", run)
    write_jobs(tmp_path, [job(id="a"), job(id="b")])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        submitted = await asyncio.gather(
            *[c.post(BASE, json={"limit": 1}) for _ in range(3)]
        )
        assert sorted(r.status_code for r in submitted) == [202, 202, 429]
        await entered.wait()
        ids = [r.json()["task_id"] for r in submitted if r.status_code == 202]
        assert (await c.post(f"{BASE}/{ids[0]}/stop")).status_code == 200
        release.set()
        await app.state.cron_migration_tasks.wait()
        result = (await c.get(f"{BASE}/{ids[0]}")).json()
        assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_full_scan_routes_keep_global_login_and_internal_token_separate(
    tmp_path, monkeypatch
):
    from swe.app import auth
    from swe.app.routers import internal

    app = app_with_db(tmp_path, monkeypatch, TaskDatabase())
    app.add_middleware(auth.AuthMiddleware)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "has_registered_users", lambda: True)
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda token: "admin" if token == "login" else None,
    )
    monkeypatch.setattr(internal, "_INTERNAL_TOKEN", "internal")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.post(BASE, json={})).status_code == 401
        headers = {"Authorization": "Bearer login"}
        assert (
            await c.post(BASE, json={}, headers=headers)
        ).status_code == 401
        headers["X-Internal-Token"] = "Bearer internal"
        accepted = await c.post(BASE, json={}, headers=headers)
        assert accepted.status_code == 202
        await app.state.cron_migration_tasks.wait()
        url = f"{BASE}/{accepted.json()['task_id']}"
        assert (await c.get(url)).status_code == 401
        assert (await c.get(url, headers=headers)).status_code == 200
