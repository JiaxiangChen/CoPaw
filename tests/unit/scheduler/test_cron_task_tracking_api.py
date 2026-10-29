# -*- coding: utf-8 -*-
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.fakes.cron_task_tracking import TrackingDatabase

HEADERS = {
    "X-User-Id": "admin",
    "X-Source-Id": "bank",
    "X-User-Role": "manager",
}
PATH = "/api/scheduler/cron/task-tracking/jobs/job-1"


@pytest.fixture
def app_db():
    from scheduler.app.routers.cron_task_tracking import router, tracking_db

    db = TrackingDatabase()
    db.add("job-1")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[tracking_db] = lambda: db
    yield app, db
    assert db.writes == 0
    db.conn.close()


@pytest.mark.asyncio
async def test_real_http_lookup_overview_and_timeline_are_read_only(app_db):
    app, _ = app_db
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        found = await client.get(PATH + "/lookup", headers=HEADERS)
        assert found.status_code == 200
        assert found.json()["candidates"][0]["tenant_id"] == "alice"
        overview = await client.get(
            PATH + "/overview?tenant_id=alice&agent_id=default",
            headers=HEADERS,
        )
        assert overview.status_code == 200
        timeline = await client.get(
            PATH + "/timeline?tenant_id=alice&agent_id=default",
            headers=HEADERS,
        )
        assert timeline.status_code == 200
        assert timeline.json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,status",
    [({}, 400), ({**HEADERS, "X-User-Role": "user"}, 403)],
)
async def test_headers_enforce_administrator_scope(app_db, headers, status):
    app, _ = app_db
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            PATH + "/lookup?role=admin&source_id=bank",
            headers=headers,
        )
        assert response.status_code == status


@pytest.mark.asyncio
async def test_query_cannot_override_trusted_source_header(app_db):
    app, db = app_db
    db.add("private", source="other")
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            PATH.replace("job-1", "private") + "/lookup?source_id=other",
            headers=HEADERS,
        )
        assert response.json()["candidates"] == []


@pytest.mark.asyncio
async def test_invalid_source_identity_is_a_client_error_not_unhandled_exception(
    app_db,
):
    app, _ = app_db
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            PATH + "/lookup",
            headers={**HEADERS, "X-Source-Id": "../bank"},
        )
        assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        "start_time=2026-10-01&end_time=2026-10-02",
        "limit=101",
        "order=sql",
        "start_time=2026-10-01T00:00:00Z&end_time=2026-11-03T00:00:00Z",
    ],
)
async def test_invalid_time_and_pagination_are_rejected_without_sql_error(
    app_db,
    filters,
):
    app, _ = app_db
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            PATH + "/timeline?tenant_id=alice&agent_id=default&" + filters,
            headers=HEADERS,
        )
        assert response.status_code in {400, 422}


@pytest.mark.asyncio
async def test_database_error_returns_sanitized_error_and_diagnostic_log(
    app_db,
    caplog,
):
    app, db = app_db
    db.broken = True
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(PATH + "/lookup", headers=HEADERS)
        assert response.status_code == 503
        assert (
            "private credentials"
            not in json.dumps(response.json()) + caplog.text
        )
        assert "RuntimeError" in caplog.text
        query_id = response.headers["x-cron-tracking-query-id"]
        source_logs = [
            record.getMessage()
            for record in caplog.records
            if "source unavailable" in record.getMessage()
        ]
        assert any(query_id in message for message in source_logs)


@pytest.mark.asyncio
async def test_connection_factory_failure_has_safe_correlated_diagnostics(
    app_db,
    monkeypatch,
    caplog,
):
    app, _ = app_db
    app.dependency_overrides.clear()

    def unavailable():
        raise RuntimeError("PRIVATE-CONNECTION")

    monkeypatch.setattr(
        "scheduler.app.routers.cron_task_tracking.get_db_connection",
        unavailable,
    )
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(PATH + "/lookup", headers=HEADERS)
        assert response.status_code == 503
        assert response.headers["x-cron-tracking-query-id"] in caplog.text
        assert "PRIVATE-CONNECTION" not in caplog.text + response.text


@pytest.mark.asyncio
async def test_scheduler_registry_serves_queries_even_when_dispatch_worker_is_not_ready(
    app_db,
):
    from scheduler.app.routers import api_router
    from scheduler.app.routers.cron_task_tracking import tracking_db

    _, db = app_db
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[tracking_db] = lambda: db
    app.state.managed_cron_runtime_prepared = True
    app.state.managed_cron_runtime_ready = False
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            PATH + "/overview?tenant_id=alice&agent_id=default",
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["identity"]["tenant_id"] == "alice"


@pytest.mark.asyncio
async def test_monitor_does_not_keep_a_second_tracking_endpoint(app_db):
    from monitor.app.routers import api_router
    from scheduler.app.routers.cron_task_tracking import tracking_db

    _, db = app_db
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[tracking_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/monitor/cron/task-tracking/jobs/job-1/lookup",
            headers=HEADERS,
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_query_identity_does_not_depend_on_extra_display_headers(
    app_db,
):
    app, _ = app_db
    headers = {
        **HEADERS,
        "X-Tenant-Id": "admin",
        "X-Agent-Id": "another-agent",
        "X-Scope-Id": "stale-display-scope",
    }
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            PATH + "/overview?tenant_id=alice&agent_id=default",
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["identity"]["agent_id"] == "default"


def test_scheduler_tracking_package_does_not_import_monitor_or_swe():
    import os
    import subprocess
    import sys
    from pathlib import Path

    source = """
import importlib.abc
import sys
class BlockOtherServices(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'monitor', 'swe'}:
            raise RuntimeError('cross-service runtime import: ' + fullname)
sys.meta_path.insert(0, BlockOtherServices())
from scheduler.app.routers import api_router
from scheduler.app.services.cron.tracking_timeline import TrackingTimeline
"""
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "scheduler/src")},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
