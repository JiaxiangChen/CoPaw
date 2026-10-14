"""Optional MySQL proof using an empty codex_test_* database."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio

from scheduler.app.database import schema
from scheduler.app.services.cron import execution_sync_service as module
from tests.unit.scheduler.test_cron_execution_is_final import request


class TestDatabase:
    __test__ = False

    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            yield conn

    async def execute(self, sql, params=()):
        async with self.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)

    async def rows(self):
        async with self.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT is_final FROM swe_cron_executions ORDER BY id",
                )
                return list(await cursor.fetchall())


@pytest_asyncio.fixture
async def mysql_db(monkeypatch):
    database = os.environ.get("COPAW_CRON_TEST_MYSQL_DATABASE")
    if not database:
        pytest.skip("isolated MySQL test database is not configured")
    if not database.startswith("codex_test_"):
        pytest.fail("MySQL test database name must start with codex_test_")
    aiomysql = pytest.importorskip("aiomysql")
    pool = await aiomysql.create_pool(
        host=os.environ["COPAW_CRON_TEST_MYSQL_HOST"],
        port=int(os.environ.get("COPAW_CRON_TEST_MYSQL_PORT", "3306")),
        user=os.environ["COPAW_CRON_TEST_MYSQL_USER"],
        password=os.environ.get("COPAW_CRON_TEST_MYSQL_PASSWORD", ""),
        db=database,
        charset="utf8mb4",
        autocommit=True,
        minsize=2,
        maxsize=3,
    )
    db = TestDatabase(pool)
    created = []
    try:
        async with db.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SHOW TABLES")
                if await cursor.fetchall():
                    pytest.fail("MySQL test database must be empty")
        for name, ddl in (
            ("swe_cron_executions", schema.CREATE_CRON_EXECUTIONS_TABLE),
            (
                "swe_cron_dispatch_intents",
                schema.CREATE_CRON_DISPATCH_INTENTS_TABLE,
            ),
        ):
            await db.execute(ddl)
            created.append(name)
        await db.execute(
            (
                Path(__file__).resolve().parents[2]
                / "scripts/sql/cron_execution_is_final.sql"
            ).read_text(
                encoding="utf-8",
            ),
        )
        await db.execute(
            "INSERT INTO swe_cron_dispatch_intents "
            "(id,batch_id,intent_role,job_id,tenant_id,source_id,"
            "attempt_count,due_at) "
            "VALUES (7,'batch-1','child','job-1','tenant-1','source-1',1,NOW())",
        )
        monkeypatch.setattr(module, "get_db_connection", lambda: db)
        yield db
    finally:
        for name in reversed(created):
            await db.execute(f"DROP TABLE {name}")
        pool.close()
        await pool.wait_closed()


@pytest.mark.asyncio
async def test_concurrent_same_attempt_only_inserts_one_mysql_row(mysql_db):
    service = module.ExecutionSyncService()
    ids = await asyncio.wait_for(
        asyncio.gather(
            service.record_dispatch_execution(request()),
            service.record_dispatch_execution(request()),
        ),
        timeout=10,
    )
    assert ids[0] == ids[1]
    assert await mysql_db.rows() == [(1,)]


@pytest.mark.asyncio
async def test_concurrent_retry_replaces_old_mysql_row_once(mysql_db):
    service = module.ExecutionSyncService()
    first = await service.record_dispatch_execution(request())
    await mysql_db.execute(
        "UPDATE swe_cron_dispatch_intents SET attempt_count=2 WHERE id=7",
    )
    ids = await asyncio.wait_for(
        asyncio.gather(
            service.record_dispatch_execution(request(2)),
            service.record_dispatch_execution(request(2, is_manual=True)),
        ),
        timeout=10,
    )
    assert ids[0] == ids[1] != first
    assert await mysql_db.rows() == [(0,), (1,)]


@pytest.mark.asyncio
async def test_failed_insert_restores_mysql_final_marker(
    mysql_db, monkeypatch
):
    service = module.ExecutionSyncService()
    await service.record_dispatch_execution(request())
    await mysql_db.execute(
        "UPDATE swe_cron_dispatch_intents SET attempt_count=2 WHERE id=7",
    )

    async def fail(*_):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(service, "_insert_execution", fail)
    with pytest.raises(RuntimeError, match="insert failed"):
        await service.record_dispatch_execution(request(2))
    assert await mysql_db.rows() == [(1,)]
