# -*- coding: utf-8 -*-
"""按技能查询定时任务接口的 SQL 约束与返回约定。"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from swe.app.crons import skill_job_lookup


class FakeDb:
    """记录查询语句并按需返回单行的假数据库。"""

    is_connected = True

    def __init__(self, row=None):
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    async def fetch_one(self, sql, params=None):
        self.calls.append((sql, tuple(params or ())))
        return self.row

    @property
    def last_call(self):
        return self.calls[-1]


def _client(db) -> TestClient:
    app = FastAPI()
    app.state.db_connection = db
    app.include_router(skill_job_lookup.router, prefix="/api")
    return TestClient(app)


def test_legacy_lookup_matches_comma_separated_skill_and_returns_binding():
    db = FakeDb({"id": "job-2", "bbk_id": "1001"})
    response = _client(db).post(
        "/api/cron/skill-jobs/legacy",
        json={
            "skill_id": "skill-b",
            "tenant_id": "tenant-1",
            "source_id": "source-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"id": "job-2", "bbk_id": "1001"}
    sql, params = db.last_call
    assert "FIND_IN_SET(%s, skill_ids)" in sql
    assert "deleted_at IS NULL" in sql
    assert "status != 'deleted'" in sql
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT 1" in sql
    assert params == ("tenant-1", "source-1", "skill-b")


def test_legacy_lookup_returns_404_when_skill_has_no_job():
    response = _client(FakeDb(None)).post(
        "/api/cron/skill-jobs/legacy",
        json={
            "skill_id": "skill-b",
            "tenant_id": "tenant-1",
            "source_id": "source-1",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == skill_job_lookup.SKILL_JOB_NOT_FOUND


def test_managed_lookup_defaults_agent_and_reads_expanded_columns():
    db = FakeDb({"job_id": "job-9", "bbk_id": "2002"})
    response = _client(db).post(
        "/api/cron/skill-jobs/managed",
        json={
            "skill_id": "skill-b",
            "tenant_id": "tenant-1",
            "source_id": "source-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-9", "bbk_id": "2002"}
    sql, params = db.last_call
    assert "SELECT job_id, bbk_id FROM swe_cron_jobs_v2" in sql
    assert "FIND_IN_SET(%s, skill_ids) > 0" in sql
    assert "JSON_" not in sql
    assert "spec_json" not in sql
    assert "deleted_at IS NULL" in sql
    assert "ORDER BY created_at DESC, job_id DESC LIMIT 1" in sql
    assert params == ("tenant-1", "source-1", "default", "skill-b")


def test_managed_lookup_uses_requested_agent_and_strips_whitespace():
    db = FakeDb({"job_id": "job-9", "bbk_id": None})
    response = _client(db).post(
        "/api/cron/skill-jobs/managed",
        json={
            "skill_id": " skill-b ",
            "tenant_id": "tenant-1",
            "source_id": " source-1 ",
            "agent_id": "ops",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-9", "bbk_id": ""}
    assert db.last_call[1] == ("tenant-1", "source-1", "ops", "skill-b")


def test_managed_lookup_returns_404_when_skill_has_no_job():
    response = _client(FakeDb(None)).post(
        "/api/cron/skill-jobs/managed",
        json={
            "skill_id": "skill-b",
            "tenant_id": "tenant-1",
            "source_id": "source-1",
        },
    )

    assert response.status_code == 404


@pytest.mark.parametrize("connected", [None, False])
def test_lookup_reports_503_without_connected_database(connected):
    db = None if connected is None else SimpleNamespace(is_connected=connected)
    response = _client(db).post(
        "/api/cron/skill-jobs/legacy",
        json={
            "skill_id": "skill-b",
            "tenant_id": "tenant-1",
            "source_id": "source-1",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == skill_job_lookup.DATABASE_UNAVAILABLE


def test_lookup_rejects_unknown_fields():
    response = _client(FakeDb(None)).post(
        "/api/cron/skill-jobs/legacy",
        json={
            "skill_id": "skill-b",
            "tenant_id": "tenant-1",
            "source_id": "source-1",
            "agent_id": "ops",
        },
    )

    assert response.status_code == 422
