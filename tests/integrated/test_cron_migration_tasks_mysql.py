# -*- coding: utf-8 -*-
"""Existing async tables must be provisioned in the opt-in isolated test DB."""

import json

import pytest
import pytest_asyncio

from swe.app.crons.migration_models import (
    MigrationResponse,
    MigrationTaskRequest,
)
from swe.app.crons.migration_tasks import MigrationTaskStore, TASK_TYPE
from tests.integrated import test_cron_migration_mysql as mysql_tests

mysql_db = mysql_tests.db

pytestmark = pytest.mark.skipif(
    not mysql_tests.RAW_CONFIG,
    reason="explicit isolated MySQL configuration not provided",
)


@pytest_asyncio.fixture
async def progress_db(mysql_db):
    connection, _ = mysql_db
    store = MigrationTaskStore(connection)
    await store.check_schema()
    task_id = await store.create(MigrationTaskRequest(), "test-instance")
    try:
        yield connection, store, task_id
    finally:
        await connection.execute(
            "DELETE FROM swe_async_task_items WHERE task_id = %s", (task_id,)
        )
        await connection.execute(
            "DELETE FROM swe_async_tasks "
            "WHERE task_id = %s AND task_type = %s",
            (task_id, TASK_TYPE),
        )


def result_page():
    return MigrationResponse(
        operation="import",
        instance_id="test-instance",
        counts={"inserted": 1},
        scanned_files=1,
        scanned_jobs=1,
        memory_incomplete=True,
        partial_failure=False,
        items=[],
    )


@pytest.mark.asyncio
async def test_shared_progress_json_utc_and_page_results(progress_db):
    connection, store, task_id = progress_db
    progress = json.loads((await store.row(task_id))["result_json"])
    progress.update(pages_completed=1, scan_complete=True, scanned_jobs=1)
    await store.save_page(task_id, result_page(), progress)
    await store.finish(task_id, "succeeded")
    queried = await MigrationTaskStore(connection).view(task_id)
    assert queried.status == "succeeded"
    assert queried.progress["pages_completed"] == 1
    assert queried.pages[0]["result"]["counts"]["inserted"] == 1


@pytest.mark.asyncio
async def test_page_and_summary_are_one_real_transaction(
    progress_db, monkeypatch
):
    connection, store, task_id = progress_db
    progress = json.loads((await store.row(task_id))["result_json"])
    progress.update(pages_completed=1)
    original = connection.execute

    async def fail_summary(query, params=None):
        if "SET result_json" in query:
            raise RuntimeError("injected summary write failure")
        return await original(query, params)

    monkeypatch.setattr(connection, "execute", fail_summary)
    with pytest.raises(RuntimeError, match="injected"):
        await store.save_page(task_id, result_page(), progress)
    queried = await store.view(task_id)
    assert queried.progress["pages_completed"] == 0
    assert queried.pages == []
