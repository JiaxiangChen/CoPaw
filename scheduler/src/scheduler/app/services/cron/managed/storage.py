# -*- coding: utf-8 -*-
"""最小事务适配层，不自动建表，不改变旧数据库封装。"""

from contextlib import asynccontextmanager


class TransactionConnection:
    is_connected = True

    def __init__(self, connection):
        self.connection = connection

    async def execute(self, query, params=None):
        async with self.connection.cursor() as cursor:
            await cursor.execute(query, params)
            return cursor.rowcount

    async def fetch_all(self, query, params=None):
        import aiomysql

        async with self.connection.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute(query, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def fetch_one(self, query, params=None):
        rows = await self.fetch_all(query, params)
        return rows[0] if rows else None


@asynccontextmanager
async def transaction(db):
    """保证同一事务的语句使用同一连接，并在取消时回滚。"""
    existing = getattr(db, "transaction", None)
    if existing is not None:
        async with existing():
            yield db
        return
    async with db.acquire() as connection:
        await connection.begin()
        try:
            yield TransactionConnection(connection)
        except BaseException:
            await connection.rollback()
            raise
        await connection.commit()


TABLES = {
    "swe_cron_jobs_v2",
    "swe_cron_job_states",
    "swe_cron_jobs",
    "swe_cron_registration_journal",
    "swe_cron_execution_runs",
    "swe_cron_managed_broadcast_tasks",
    "swe_cron_managed_broadcast_items",
    "swe_cron_managed_dispatch_controls",
    "swe_cron_managed_mode_items",
    "swe_cron_managed_trigger_receipts",
    "swe_cron_managed_batch_members",
    "swe_cron_managed_dispatch_events",
}


def _columns(table, values):
    if table not in TABLES or any(
        not key.replace("_", "").isalnum() for key in values
    ):
        raise ValueError("invalid_internal_cron_columns")


async def insert(db, table, values):
    _columns(table, values)
    names = ", ".join(f"`{key}`" for key in values)
    marks = ", ".join("%s" for _ in values)
    await db.execute(
        f"INSERT INTO `{table}` ({names}) VALUES ({marks})",
        tuple(values.values()),
    )


async def update(db, table, values, where, params):
    _columns(table, values)
    setters = ", ".join(f"`{key}` = %s" for key in values)
    return await db.execute(
        f"UPDATE `{table}` SET {setters} WHERE {where}",
        (*values.values(), *params),
    )
