# -*- coding: utf-8 -*-
"""验证两种数据库封装的事务绑定与取消回滚契约。"""

import asyncio
from contextlib import asynccontextmanager

import pytest


@pytest.mark.asyncio
async def test_existing_db_transaction_keeps_its_wrapper_not_raw_connection():
    from scheduler.app.services.cron.managed.storage import transaction

    class ExistingDatabase:
        @asynccontextmanager
        async def transaction(self):
            yield object()  # Like SWE: yields a raw aiomysql connection.

    db = ExistingDatabase()
    async with transaction(db) as bound:
        assert bound is db


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_scheduler_pool_transaction_commits_or_rolls_back_on_cancel(
    cancel,
):
    from scheduler.app.services.cron.managed.storage import transaction

    events = []

    class Connection:
        async def begin(self):
            events.append("begin")

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    class Database:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    if cancel:
        with pytest.raises(asyncio.CancelledError):
            async with transaction(Database()):
                raise asyncio.CancelledError()
        assert events == ["begin", "rollback"]
    else:
        async with transaction(Database()):
            pass
        assert events == ["begin", "commit"]
