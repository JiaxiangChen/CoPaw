# -*- coding: utf-8 -*-
"""Execute analytics SQL against replaced and current execution records."""

import sqlite3
from datetime import datetime

import pytest

from monitor.app.services.cron import query_service as cron
from monitor.app.services.tracing.query_service import TracingQueryService


class AnalyticsDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE swe_cron_jobs (
                id TEXT, tenant_id TEXT, source_id TEXT, bbk_id TEXT,
                deleted_at TEXT, status TEXT
            );
            INSERT INTO swe_cron_jobs VALUES
                ('job', 'user', 'app', '100', NULL, 'active');
            CREATE TABLE swe_cron_executions (
                id INTEGER, job_id TEXT, tenant_id TEXT, actual_time TEXT,
                status TEXT, async_status TEXT, is_read INTEGER,
                is_final INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO swe_cron_executions VALUES
                (1,'job','user','2026-10-19 10:00:00','success','success',1,0),
                (2,'job','user','2026-10-19 10:00:00','success','success',1,1),
                (3,'job','user','2026-10-19 10:00:00','success','error',0,1),
                (4,'job','user','2026-10-19 10:00:00','success',NULL,0,1),
                (5,'job','user','2026-10-19 10:00:00','error',NULL,0,0);
            CREATE TABLE swe_tracing_traces (
                user_id TEXT, source_id TEXT, session_id TEXT,
                total_tokens INTEGER, start_time TEXT, user_name TEXT, bbk_id TEXT
            );
            INSERT INTO swe_tracing_traces VALUES
                ('user','app','manual',1,'2026-10-19','User','100');
            CREATE TABLE swe_tracing_spans (
                trace_id TEXT, skill_name TEXT, source_id TEXT
            );
            ALTER TABLE swe_tracing_traces ADD COLUMN trace_id TEXT;
            ALTER TABLE swe_cron_jobs ADD COLUMN tenant_name TEXT;
            ALTER TABLE swe_cron_jobs ADD COLUMN name TEXT;
            ALTER TABLE swe_cron_executions ADD COLUMN trace_id TEXT DEFAULT '';
            CREATE TABLE swe_cron_subtasks (
                trace_id TEXT, custuid TEXT, cust_nm TEXT
            );
            CREATE TABLE swe_html_preview_click_events (
                cron_task_id TEXT, customer_id TEXT, event_type TEXT,
                template_type TEXT, button_type TEXT, clicked_at TEXT,
                source_id TEXT
            );
        """)

    async def fetch_all(self, sql, params=()):
        assert sql.count("%s") == len(params)
        params = tuple(
            value.isoformat(sep=" ") if isinstance(value, datetime) else value
            for value in params
        )
        return [
            dict(row)
            for row in self.connection.execute(
                sql.replace("%s", "?").replace("%%", "%"),
                params,
            )
        ]

    async def fetch_one(self, sql, params=()):
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None


@pytest.fixture
def db():
    database = AnalyticsDb()
    yield database
    database.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["app", "all"])
async def test_user_success_requires_current_async_success(db, source):
    service = TracingQueryService(db)
    where, params = service._build_cron_subquery(source, None, None)
    sql, values = service._build_users_query(
        source,
        "1=1",
        where,
        "t.user_id",
        [],
        params,
        10,
        0,
    )
    row = await db.fetch_one(sql, values)
    assert (
        row["cron_executions"],
        row["cron_success"],
        row["cron_reads"],
    ) == (3, 1, 1)


@pytest.mark.asyncio
async def test_branch_counts_only_current_records(db):
    result = await cron.QueryService()._fetch_branch_execution_stats(
        db,
        "2026-10-19",
        "2026-10-20",
        ["job"],
    )
    assert result == {
        "total_executions": 3,
        "success_count": 1,
        "read_tasks": 1,
        "error_count": 1,
    }


@pytest.mark.asyncio
async def test_overview_counts_only_current_records(db):
    result = await cron.QueryService()._fetch_overview_execution_counts(
        db,
        "2026-10-19",
        "2026-10-20",
        "",
        [],
        "",
        [],
    )
    assert result["total_executions"] == 3
    assert result["success_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["app", "all"])
async def test_summary_retains_current_running_records(db, source):
    result = await TracingQueryService(db).get_task_status_summary(
        source,
        datetime(2026, 10, 19),
        datetime(2026, 10, 20),
    )
    assert (
        result.total_tasks,
        result.success,
        result.failed,
        result.running,
        result.read_count,
    ) == (3, 1, 1, 1, 1)


@pytest.mark.asyncio
async def test_single_replaced_execution_is_not_visible(db, monkeypatch):
    monkeypatch.setattr(cron, "get_db_connection", lambda: db)
    assert await cron.QueryService().get_execution(1, "app") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(None, {2, 3, 4}), ("failed", {3})],
)
async def test_execution_list_total_and_rows_agree(
    db,
    monkeypatch,
    status,
    expected,
):
    monkeypatch.setattr(cron, "get_db_connection", lambda: db)
    result = await cron.QueryService().list_executions(
        cron.ExecutionQueryParams(source_id="app", status=status),
    )
    assert result.total == len(expected)
    assert {row.id for row in result.items} == expected


@pytest.mark.asyncio
async def test_execution_export_only_current_rows(db, monkeypatch):
    monkeypatch.setattr(cron, "get_db_connection", lambda: db)
    result = await cron.QueryService().get_executions_for_export(
        source_id="app",
    )
    assert {row.id for row in result} == {2, 3, 4}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "app"])
async def test_skill_export_filters_outer_execution_rows(
    db,
    monkeypatch,
    source,
):
    monkeypatch.setattr(cron, "get_db_connection", lambda: db)
    rows = await cron.QueryService().get_skill_usage_details_for_export(
        "2026-10-19",
        "2026-10-19",
        "100",
        source,
    )
    assert {row["execution_id"] for row in rows} == {2, 3, 4}
