# -*- coding: utf-8 -*-
"""Tests for cron subtask result indexing."""

import asyncio
from datetime import datetime

from monitor.app.services.subtask import query_service as query_service_module
from monitor.app.services.subtask.query_service import QueryService


class FakeDb:
    def __init__(self):
        self.fetch_all_calls = []
        self.execute_calls = []
        self.execute_many_calls = []
        self.fetch_all_results = [
            [
                {
                    "execution_id": 42,
                    "job_id": "job-1",
                    "trace_id": "trace-1",
                    "actual_time": datetime(2026, 8, 16, 10, 0, 0),
                    "created_at": datetime(2026, 8, 16, 10, 0, 1),
                    "tenant_id": "tenant-1",
                    "bbk_id": "771",
                    "source_id": "source-1",
                    "skill_ids": "skill-a, skill-b, skill-a",
                },
            ],
            [
                {
                    "subtask_id": 7,
                    "trace_id": "trace-1",
                    "task_id": "task-1",
                    "filename": "result.html",
                    "task_type": "plan",
                    "custuid": "cust-1",
                    "cust_nm": "Customer",
                    "bbk_org_id": "772",
                    "template_id": 11,
                    "result_id": "doc-1",
                    "status": "SUC",
                    "created_at": datetime(2026, 8, 16, 10, 0, 2),
                },
            ],
            [
                {"bbk_id": "771", "skill_id": "skill-a"},
                {"bbk_id": "771", "skill_id": "skill-b"},
            ],
        ]

    async def fetch_all(self, sql, params=None):
        self.fetch_all_calls.append((sql, params))
        return self.fetch_all_results.pop(0)

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params))
        if "async_status = 'success'" in sql:
            return 1
        if "async_status = 'error'" in sql:
            return 0
        return 1

    async def execute_many(self, sql, params_list):
        self.execute_many_calls.append((sql, params_list))
        return len(params_list)


def _result_index_batch_insert_calls(db):
    return [
        call
        for call in db.execute_many_calls
        if "INSERT INTO swe_cron_result_index" in call[0]
    ]


def test_batch_update_indexes_success_execution_results():
    db = FakeDb()
    success_count, error_count, indexed_count, indexed_users = asyncio.run(
        QueryService(db=db).batch_update_execution_async_status(),
    )

    assert success_count == 1
    assert error_count == 0
    assert indexed_count == 2
    assert indexed_users == [
        {"custUid": "cust-1", "bbkId": "772"},
        {"custUid": "cust-1", "bbkId": "772"},
    ]

    success_update_sql = db.execute_calls[0][0]
    assert "async_status = 'success'" in success_update_sql
    assert "AND e.status = 'success'" in success_update_sql
    assert "AND e.id IN (%s)" in success_update_sql
    assert db.execute_calls[0][1] == (42,)

    error_update_sql = db.execute_calls[1][0]
    assert "async_status = 'error'" in error_update_sql
    assert "AND e.id IN (%s)" in error_update_sql
    assert db.execute_calls[1][1] == (42,)

    insert_calls = _result_index_batch_insert_calls(db)
    assert len(insert_calls) == 1
    assert len(insert_calls[0][1]) == 2

    first_insert_params = insert_calls[0][1][0]
    assert first_insert_params[0] == "source-1"
    assert first_insert_params[1] == "tenant-1"
    assert first_insert_params[2] == "771"
    assert first_insert_params[3] == "772"
    assert first_insert_params[4] == "cust-1"
    assert first_insert_params[5] == "C******r"
    assert first_insert_params[6] == "skill-a"
    assert first_insert_params[13] == 11
    assert first_insert_params[14] == "doc-1"

    second_insert_params = insert_calls[0][1][1]
    assert second_insert_params[6] == "skill-b"

    subtask_sql = db.fetch_all_calls[1][0]
    assert "template_id IS NOT NULL" in subtask_sql
    assert "template_id > 0" in subtask_sql
    assert "result_id IS NOT NULL" in subtask_sql
    assert "result_id <> ''" in subtask_sql

    skill_config_sql, skill_config_params = db.fetch_all_calls[2]
    assert "FROM swe_skill_config" in skill_config_sql
    assert "bbk_id IN (%s)" in skill_config_sql
    assert "customer_insight_enabled = 1" in skill_config_sql
    assert skill_config_params == ("771",)


def test_batch_update_indexes_successful_subtasks_for_failed_execution():
    db = FakeDb()
    db.execute = _execute_with_error_result(db)

    success_count, error_count, indexed_count, _ = asyncio.run(
        QueryService(db=db).batch_update_execution_async_status(),
    )

    assert (success_count, error_count, indexed_count) == (0, 1, 2)
    assert len(_result_index_batch_insert_calls(db)) == 1


def _execute_with_error_result(db):
    async def execute(sql, params=None):
        db.execute_calls.append((sql, params))
        if "async_status = 'success'" in sql:
            return 0
        if "async_status = 'error'" in sql:
            return 1
        return 1

    return execute


def test_batch_update_deduplicates_successful_subtasks_per_trace_and_customer():
    db = FakeDb()
    db.fetch_all_results[1] = [
        {
            "subtask_id": 10,
            "trace_id": "trace-1",
            "task_id": "list-old",
            "filename": "list-old.html",
            "task_type": "list",
            "custuid": "cust-1",
            "cust_nm": "Old",
            "bbk_org_id": "772",
            "template_id": 11,
            "result_id": "doc-list-old",
            "status": "SUC",
            "created_at": datetime(2026, 8, 16, 10, 0, 2),
        },
        {
            "subtask_id": 11,
            "trace_id": "trace-1",
            "task_id": "list-new",
            "filename": "list-new.html",
            "task_type": "list",
            "custuid": "cust-1",
            "cust_nm": "New",
            "bbk_org_id": "772",
            "template_id": 11,
            "result_id": "doc-list-new",
            "status": "SUC",
            "created_at": datetime(2026, 8, 16, 10, 0, 3),
        },
        {
            "subtask_id": 12,
            "trace_id": "trace-1",
            "task_id": "plan-cust-1-old",
            "filename": "plan-old.html",
            "task_type": "plan",
            "custuid": "cust-1",
            "cust_nm": "Customer",
            "bbk_org_id": "772",
            "template_id": 11,
            "result_id": "doc-plan-old",
            "status": "SUC",
            "created_at": datetime(2026, 8, 16, 10, 0, 4),
        },
        {
            "subtask_id": 13,
            "trace_id": "trace-1",
            "task_id": "plan-cust-1-new",
            "filename": "plan-new.html",
            "task_type": "plan",
            "custuid": "cust-1",
            "cust_nm": "Customer",
            "bbk_org_id": "772",
            "template_id": 11,
            "result_id": "doc-plan-new",
            "status": "SUC",
            "created_at": datetime(2026, 8, 16, 10, 0, 5),
        },
        {
            "subtask_id": 14,
            "trace_id": "trace-1",
            "task_id": "plan-cust-2",
            "filename": "plan-cust-2.html",
            "task_type": "plan",
            "custuid": "cust-2",
            "cust_nm": "Customer Two",
            "bbk_org_id": "772",
            "template_id": 11,
            "result_id": "doc-plan-cust-2",
            "status": "SUC",
            "created_at": datetime(2026, 8, 16, 10, 0, 6),
        },
    ]

    success_count, error_count, indexed_count, _ = asyncio.run(
        QueryService(db=db).batch_update_execution_async_status(),
    )

    assert (success_count, error_count, indexed_count) == (1, 0, 6)
    insert_calls = _result_index_batch_insert_calls(db)
    assert len(insert_calls) == 1
    assert len(insert_calls[0][1]) == 6
    assert {params[11] for params in insert_calls[0][1]} == {
        "list-new",
        "plan-cust-1-new",
        "plan-cust-2",
    }


def test_build_result_index_users_filters_by_enabled_skill_config():
    db = FakeDb()
    db.fetch_all_results = [
        [
            {"bbk_id": "771", "skill_id": "skill-a"},
        ],
    ]
    rows = [
        {
            "first_bbk_id": "771",
            "skill_id": "skill-a",
            "custuid": "cust-1",
            "bbk_org_id": "772",
            "tenant_id": "01100129",
        },
        {
            "first_bbk_id": "771",
            "skill_id": "skill-b",
            "custuid": "cust-2",
            "bbk_org_id": "772",
            "tenant_id": "01100129",
        },
    ]

    users = asyncio.run(QueryService(db=db)._build_result_index_users(rows))

    assert users == [{"custUid": "cust-1", "bbkId": "772"}]


def test_batch_update_skips_result_index_row_with_invalid_bbk_id():
    db = FakeDb()
    db.fetch_all_results[1][0]["bbk_org_id"] = "77"

    success_count, error_count, indexed_count, indexed_users = asyncio.run(
        QueryService(db=db).batch_update_execution_async_status(),
    )

    assert success_count == 1
    assert error_count == 0
    assert indexed_count == 0
    assert indexed_users == []
    assert not _result_index_batch_insert_calls(db)


def test_batch_update_skips_plan_result_index_row_without_customer_name():
    db = FakeDb()
    db.fetch_all_results[1][0]["cust_nm"] = " "

    success_count, error_count, indexed_count, indexed_users = asyncio.run(
        QueryService(db=db).batch_update_execution_async_status(),
    )

    assert success_count == 1
    assert error_count == 0
    assert indexed_count == 0
    assert indexed_users == []
    assert not _result_index_batch_insert_calls(db)


class FakeCustomerNameResponse:
    status_code = 200

    def json(self):
        return {
            "data": [
                {
                    "row": "0000001015FICNP",
                    "values": {"f": {"EAC_NM": "Customer Name"}},
                },
            ],
        }


class FakeCustomerNameClient:
    def __init__(self):
        self.post_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, url, headers=None, json=None):
        self.post_calls.append((url, headers, json))
        return FakeCustomerNameResponse()


def test_batch_update_queries_and_masks_missing_customer_name(monkeypatch):
    db = FakeDb()
    db.fetch_all_results[1][0]["custuid"] = "PNCIF5101000000"
    db.fetch_all_results[1][0]["cust_nm"] = ""

    client = FakeCustomerNameClient()
    monkeypatch.setattr(
        query_service_module,
        "CUSTOMER_NAME_QUERY_URL",
        "https://example.test/customer-name",
    )
    monkeypatch.setattr(
        query_service_module.httpx,
        "AsyncClient",
        lambda timeout: client,
    )

    success_count, error_count, indexed_count, indexed_users = asyncio.run(
        QueryService(db=db).batch_update_execution_async_status(),
    )

    assert success_count == 1
    assert error_count == 0
    assert indexed_count == 2
    assert indexed_users == [
        {"custUid": "PNCIF5101000000", "bbkId": "772"},
        {"custUid": "PNCIF5101000000", "bbkId": "772"},
    ]

    assert client.post_calls == [
        (
            "https://example.test/customer-name",
            {"Content-Type": "application/json"},
            {"rows": ["0000001015FICNP"]},
        ),
    ]

    insert_calls = _result_index_batch_insert_calls(db)
    assert insert_calls[0][1][0][5] == "C*********e"
