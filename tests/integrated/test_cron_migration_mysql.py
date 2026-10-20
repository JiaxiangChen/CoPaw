# -*- coding: utf-8 -*-
"""Opt-in real-MySQL checks against an explicitly provisioned disposable schema.

CRON_MIGRATION_TEST_MYSQL_JSON must explicitly supply connection parameters.
The database name must start with test_cron_migration_. No DDL is executed.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from swe.database.config import DatabaseConfig
from swe.database.connection import DatabaseConnection
from swe.app.crons.migration_models import SnapshotCandidate
from swe.app.crons.migration_repository import MigrationRepository

RAW_CONFIG = os.environ.get("CRON_MIGRATION_TEST_MYSQL_JSON")
pytestmark = pytest.mark.skipif(
    not RAW_CONFIG, reason="explicit isolated MySQL configuration not provided"
)


class TestConnection(DatabaseConnection):
    __test__ = False
    fail_state = False

    async def execute(self, query, params=None):
        if self.fail_state and query.startswith(
            "INSERT INTO `swe_cron_job_states`"
        ):
            raise RuntimeError("injected second-row failure")
        return await super().execute(query, params)


@pytest_asyncio.fixture
async def db():
    settings = json.loads(RAW_CONFIG)
    if not settings.get("database", "").startswith("test_cron_migration_"):
        pytest.fail(
            "integration tests require a dedicated test_cron_migration_ database"
        )
    if not all(settings.get(k) for k in ("host", "user", "database")):
        pytest.fail("explicit host, user and isolated database are required")
    connection = TestConnection(DatabaseConfig(**settings))
    await connection.connect()
    tenant = "test_" + uuid.uuid4().hex
    try:
        await MigrationRepository(connection).check_schema()
        yield connection, tenant
    finally:
        if connection.is_connected:
            # Only this fixture's random identity is removed; never truncate or
            # derive cleanup targets from application/default DB configuration.
            for table in ("swe_cron_job_states", "swe_cron_jobs_v2"):
                await connection.execute(
                    f"DELETE FROM `{table}` WHERE tenant_id = %s AND source_id = %s",
                    (tenant, "test-source"),
                )
        await connection.close()


def snapshot(tenant):
    return SnapshotCandidate(
        definition={
            "tenant_id": tenant,
            "source_id": "test-source",
            "agent_id": "default",
            "job_id": "test-job",
            "scope_id": "test-scope",
            "name": "测试迁移",
            "task_type": "text",
            "enabled": True,
            "cron_expr": "0 9 * * *",
            "original_cron_expr": "0 9 * * *",
            "timezone": "UTC",
            "spec_json": {"id": "test-job", "text": "integration"},
            "management_owner": "legacy",
            "source_definition_version": 1,
        },
        state_patch={
            "last_status": "success",
            "unread_execution_count": 1,
            "state_source": "memory_snapshot",
        },
        observed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        source_key=f"{tenant}/workspaces/default/jobs.json",
        fingerprint="b" * 64,
        file_version=1,
        memory_available=True,
    )


@pytest.mark.asyncio
async def test_two_table_rollback_on_real_mysql(db):
    connection, tenant = db
    connection.fail_state = True
    value = snapshot(tenant)
    with pytest.raises(RuntimeError, match="injected"):
        await MigrationRepository(connection).import_snapshot(value)
    for table in ("swe_cron_jobs_v2", "swe_cron_job_states"):
        assert (
            await connection.fetch_one(
                f"SELECT job_id FROM `{table}` WHERE tenant_id = %s", (tenant,)
            )
            is None
        )


@pytest.mark.asyncio
async def test_repeat_and_concurrent_first_insert_on_real_mysql(db):
    connection, tenant = db
    value = snapshot(tenant)
    repo = MigrationRepository(connection)
    outcomes = await asyncio.gather(
        repo.import_snapshot(value), repo.import_snapshot(value)
    )
    assert sorted(o.status for o in outcomes) == ["inserted", "unchanged"]
    assert (await repo.import_snapshot(value)).status == "unchanged"
    row = await connection.fetch_one(
        "SELECT spec_json, source_observed_at FROM swe_cron_jobs_v2 WHERE tenant_id = %s",
        (tenant,),
    )
    data = row["spec_json"]
    assert (json.loads(data) if isinstance(data, str) else data)[
        "id"
    ] == "test-job"
    assert row["source_observed_at"] == value.observed_at
