# -*- coding: utf-8 -*-
"""Tests for standalone subtask result indexing."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock

from monitor.app.models.subtask import SubtaskModel
from monitor.app.services.subtask.query_service import QueryService
from monitor.app.services.subtask.sync_service import SyncService


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, sql, params=None):
        self.connection.calls.append((sql, params))
        if (
            self.connection.fail_on_insert
            and "INSERT INTO swe_cron_result_index" in sql
        ):
            raise RuntimeError("insert failed")


class FakeConnection:
    def __init__(self, fail_on_insert=False):
        self.calls = []
        self.fail_on_insert = fail_on_insert
        self.committed = False
        self.rolled_back = False

    async def begin(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    def cursor(self):
        return FakeCursor(self)


class FakeDb:
    def __init__(self, fail_on_insert=False):
        self.connection = FakeConnection(fail_on_insert=fail_on_insert)
        self.execute_calls = []
        self.fetch_one_results = [None, {"id": 17}]

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params))
        return 1

    async def fetch_one(self, sql, params=None):
        return self.fetch_one_results.pop(0)


def _standalone_subtask(info):
    return SubtaskModel(
        id=17,
        trace_id="trace-1",
        task_id="task-1",
        filename="plan.html",
        task_type="plan",
        custuid="cust-1",
        cust_nm="Customer",
        bbk_org_id="772",
        template_id=11,
        result_id="doc-1",
        has_main_task=0,
        info=info,
        created_at=datetime(2026, 10, 20, 10, 0),
    )


def test_create_subtask_defaults_has_main_task_to_one():
    db = FakeDb()

    response = asyncio.run(
        QueryService(db=db).create_subtask(
            trace_id="trace-1",
            task_id="task-1",
            filename="plan.html",
        ),
    )

    assert response.id == 17
    insert_sql, insert_params = db.execute_calls[0]
    assert "has_main_task" in insert_sql
    assert insert_params[10] == 1


def test_standalone_success_writes_index_and_status_in_one_transaction():
    db = FakeDb()
    info = json.dumps(
        {
            "tenant_id": "tenant-1",
            "skill_id": "skill-1",
            "job_id": "job-1",
            "first_bbk_id": "771",
        },
    )

    indexed = asyncio.run(
        QueryService(db=db).complete_standalone_success_subtask(
            _standalone_subtask(info),
        ),
    )

    assert indexed is True
    assert db.connection.committed is True
    assert db.connection.rolled_back is False
    assert len(db.connection.calls) == 3
    stale_sql, stale_params = db.connection.calls[0]
    assert "custuid = %s" in stale_sql
    assert stale_params[1:7] == (
        "RMASSIST",
        "tenant-1",
        "771",
        "772",
        "skill-1",
        "plan",
    )
    insert_sql, insert_params = db.connection.calls[1]
    assert "INSERT INTO swe_cron_result_index" in insert_sql
    assert insert_params[0:9] == (
        "RMASSIST",
        "tenant-1",
        "771",
        "772",
        "cust-1",
        "C******r",
        "skill-1",
        "job-1",
        17,
    )
    status_sql, status_params = db.connection.calls[2]
    assert "SET status = 'SUC'" in status_sql
    assert status_params[1] == 17


def test_invalid_info_skips_index_and_preserves_info():
    db = FakeDb()
    subtask = _standalone_subtask("not-json")

    indexed = asyncio.run(
        QueryService(db=db).complete_standalone_success_subtask(subtask),
    )

    assert indexed is False
    assert db.connection.calls == []
    update_sql, update_params = db.execute_calls[0]
    assert "SET status = %s, updated_at = %s" in update_sql
    assert "info =" not in update_sql
    assert update_params[0] == "SUC"
    assert update_params[2] == 17


def test_index_failure_rolls_back_without_updating_status():
    db = FakeDb(fail_on_insert=True)
    info = json.dumps(
        {
            "tenant_id": "tenant-1",
            "skill_id": "skill-1",
            "job_id": "job-1",
            "first_bbk_id": "771",
        },
    )

    try:
        asyncio.run(
            QueryService(db=db).complete_standalone_success_subtask(
                _standalone_subtask(info),
            ),
        )
    except RuntimeError as exc:
        assert str(exc) == "insert failed"
    else:
        raise AssertionError("Expected result-index insertion to fail")

    assert db.connection.committed is False
    assert db.connection.rolled_back is True
    assert all(
        "UPDATE swe_cron_subtasks" not in sql
        for sql, _params in db.connection.calls
    )


def test_sync_success_uses_standalone_index_path():
    query_service = AsyncMock()
    service = SyncService(query_service=query_service)
    service._query_task_status = AsyncMock(return_value=("SUC", None))
    subtask = _standalone_subtask("not-json")

    detail = asyncio.run(
        service._process_pending_subtask(AsyncMock(), subtask, datetime.now()),
    )

    assert detail.new_status == "SUC"
    query_service.complete_standalone_success_subtask.assert_awaited_once_with(
        subtask,
    )
    query_service.update_subtask_status.assert_not_awaited()


def test_sync_success_keeps_existing_main_task_path():
    query_service = AsyncMock()
    service = SyncService(query_service=query_service)
    service._query_task_status = AsyncMock(return_value=("SUC", None))
    subtask = _standalone_subtask("not-json")
    subtask.has_main_task = 1

    detail = asyncio.run(
        service._process_pending_subtask(AsyncMock(), subtask, datetime.now()),
    )

    assert detail.new_status == "SUC"
    query_service.update_subtask_status.assert_awaited_once_with(
        subtask.task_id,
        subtask.trace_id,
        "SUC",
        "",
    )
    query_service.complete_standalone_success_subtask.assert_not_awaited()
