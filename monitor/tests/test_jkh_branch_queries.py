# -*- coding: utf-8 -*-
"""使用实际查询验证金葵花名单快照及统计范围。"""

import sqlite3
import inspect
from datetime import datetime

import pytest

from monitor.app.services.cron import query_service
from monitor.app.services.cron.query_service import QueryService


class QueryDb:
    """运行查询 SQL，仅适配占位符和 MySQL FIND_IN_SET。"""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.create_function(
            "FIND_IN_SET",
            2,
            lambda value, values: value in (values or "").split(","),
        )
        self.calls = []
        self.connection.executescript(
            """
            CREATE TABLE jkh_user_inf (
                user_id TEXT, sync_date TEXT, first_bbk_id TEXT DEFAULT '100'
            );
            CREATE TABLE swe_cron_jobs (
                id TEXT, tenant_id TEXT, tenant_name TEXT, bbk_id TEXT,
                source_id TEXT, skill_ids TEXT, deleted_at TEXT, status TEXT
            );
            CREATE TABLE swe_cron_executions (
                job_id TEXT, tenant_id TEXT, actual_time TEXT, status TEXT,
                async_status TEXT, is_read INTEGER, trace_id TEXT
            );
            CREATE TABLE swe_marketplace_skills (
                source_id TEXT, skill_id TEXT, cn_name TEXT, skill_name TEXT,
                include_in_statistics INTEGER
            );
            CREATE TABLE swe_cron_subtasks (trace_id TEXT, custuid TEXT);
            CREATE TABLE swe_html_preview_click_events (
                id TEXT, user_id TEXT, cron_task_id TEXT, bbk_id TEXT,
                source_id TEXT, clicked_at TEXT, event_type TEXT,
                template_type TEXT, button_type TEXT, customer_id TEXT
            );
            CREATE TABLE swe_skill_contact_detail (
                click_id TEXT, cust_uid TEXT, clicked_at TEXT
            );
            """,
        )

    async def fetch_all(self, sql, params=()):
        self.calls.append((sql, params))
        values = tuple(
            value.isoformat(" ") if isinstance(value, datetime) else value
            for value in params
        )
        return [
            dict(row)
            for row in self.connection.execute(sql.replace("%s", "?"), values)
        ]

    async def fetch_one(self, sql, params=()):
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None


@pytest.fixture
def db(monkeypatch):
    database = QueryDb()
    monkeypatch.setattr(query_service, "get_db_connection", lambda: database)
    yield database
    database.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end_date, dates, expected",
    [
        ("2026-06-15", ["2026-06-15", "2026-06-30"], "2026-06-15"),
        ("2026-06-15", ["2026-01-31", "2026-06-30"], "2026-06-30"),
        (
            "2026-06-15",
            ["2026-01-31", "2026-05-31", "2026-08-31"],
            "2026-01-31",
        ),
        ("2026-06-15", ["2026-07-31", "2026-08-31"], "2026-07-31"),
        ("2026-06-15", ["2026-01-31", "2026-05-31"], "2026-05-31"),
        ("2026-06-15", ["2026-06-15"], "2026-06-15"),
        ("2024-02-10", ["2024-01-31", "2024-02-29"], "2024-02-29"),
        ("2026-02-10", ["2026-01-31", "2026-02-28"], "2026-02-28"),
        ("2026-12-10", ["2026-01-31", "2026-12-31"], "2026-12-31"),
        ("2026-06-15", [], None),
    ],
)
async def test_snapshot_selection(db, end_date, dates, expected):
    db.connection.executemany(
        "INSERT INTO jkh_user_inf (user_id, sync_date) VALUES ('manager', ?)",
        [(date,) for date in dates],
    )
    end_time = datetime.fromisoformat(end_date).replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=999999,
    )
    assert await QueryService._resolve_jkh_sync_date(db, end_time) == expected
    assert len(db.calls) == 1


@pytest.fixture
def populated_db(db):
    db.connection.executemany(
        "INSERT INTO jkh_user_inf (user_id, sync_date) VALUES (?, ?)",
        [
            ("included", "2026-06-30"),
            ("included", "2026-06-30"),
            ("excluded", "2026-05-31"),
        ],
    )
    for source in ("app-a", "app-b"):
        db.connection.execute(
            "INSERT INTO swe_marketplace_skills VALUES (?, 'skill', '', '', 1)",
            (source,),
        )
        for branch in ("100", "200"):
            for user in ("included", "excluded"):
                job = f"{source}-{branch}-{user}"
                db.connection.execute(
                    "INSERT INTO swe_cron_jobs VALUES (?, ?, ?, ?, ?, "
                    "'skill', NULL, 'active')",
                    (job, user, user, branch, source),
                )
                db.connection.execute(
                    "INSERT INTO swe_cron_executions VALUES (?, ?, "
                    "'2026-06-10 12:00:00', 'success', 'success', 1, ?)",
                    (job, user, job),
                )
                db.connection.execute(
                    "INSERT INTO swe_cron_subtasks VALUES (?, ?)",
                    (job, job),
                )
                for button in ("plan", "insight", "phone"):
                    click = f"{job}-{button}"
                    event = (
                        "preview_view" if button == "plan" else "button_click"
                    )
                    db.connection.execute(
                        "INSERT INTO swe_html_preview_click_events VALUES "
                        "(?, ?, ?, ?, ?, '2026-06-10 12:00:00', ?, 'sub', ?, ?)",
                        (click, user, job, branch, source, event, button, job),
                    )
                    db.connection.execute(
                        "INSERT INTO swe_skill_contact_detail VALUES "
                        "(?, ?, '2026-06-10 12:00:00')",
                        (click, job),
                    )
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("source, count", [("app-a", 1), (None, 2)])
async def test_branch_behavior_filters_all_metrics(
    populated_db, source, count
):
    result = await QueryService().get_branch_behavior(
        "2026-06-01",
        "2026-06-15",
        "100",
        source,
    )
    assert len(result.items) == 1
    item = result.items[0]
    assert item.bbk_id == "100"
    assert item.total_tasks == count
    assert item.success_count == count
    assert item.read_tasks == count
    assert item.skill_count == 1
    assert item.involved_managers == 1
    assert item.result_view_managers == 1
    assert (
        item.plan_managers == item.insight_managers == item.phone_managers == 1
    )
    assert item.recommended_customers == count
    assert item.contacted_customers == count
    assert item.contact_rate == 1.0
    assert item.viewed_customers == count
    assert item.insight_customers == count
    assert item.phone_customers == count
    assert len(populated_db.calls) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("source, count", [("app-a", 1), (None, 2)])
async def test_manager_summary_filters_all_metrics(
    populated_db, source, count
):
    result = await QueryService().get_branch_manager_summary(
        "100",
        "2026-06-01",
        "2026-06-15",
        source,
    )
    assert len(result.items) == 1
    item = result.items[0]
    assert item.user_id == "included"
    assert item.total_tasks == count
    assert item.success_count == count
    assert item.read_tasks == count
    assert item.skill_count == 1
    assert item.recommended_customers == count
    assert item.viewed_customers == count
    assert item.contacted_customers == count
    assert item.contact_rate == 1.0
    assert item.insight_customers == count
    assert item.phone_customers == count
    assert len(populated_db.calls) == 6


@pytest.mark.asyncio
async def test_empty_roster_returns_empty_responses(db):
    service = QueryService()
    branch = await service.get_branch_behavior("2026-06-01", "2026-06-15")
    manager = await service.get_branch_manager_summary(
        "100",
        "2026-06-01",
        "2026-06-15",
    )
    assert branch.items == manager.items == []
    assert branch.end_date == manager.end_date == "2026-06-15"
    assert manager.bbk_id == "100"
    assert len(db.calls) == 2


@pytest.mark.asyncio
async def test_task_behavior_remains_unrestricted(populated_db):
    result = await QueryService().get_branch_task_behavior(
        "2026-06-01",
        "2026-06-15",
        "100",
        "app-a",
    )
    assert len(result.items) == 1
    assert result.items[0].success_count == 2
    assert result.items[0].manager_count == 2
    assert all("jkh_user_inf" not in sql for sql, _ in populated_db.calls)


@pytest.mark.asyncio
async def test_execution_stats_filters_executor_even_for_shared_job(
    populated_db,
):
    job = "app-a-100-included"
    populated_db.connection.execute(
        "INSERT INTO swe_cron_executions VALUES (?, 'excluded', "
        "'2026-06-10 12:00:00', 'success', 'success', 1, 'other-trace')",
        (job,),
    )
    stats = await QueryService()._fetch_branch_execution_stats(
        populated_db,
        datetime(2026, 6, 1),
        datetime(2026, 6, 15),
        [job],
        sync_date="2026-06-30",
    )
    assert stats["success_count"] == stats["read_tasks"] == 1


@pytest.mark.asyncio
async def test_clicks_filter_actor_independently_of_job_owner(populated_db):
    populated_db.connection.execute(
        "UPDATE swe_html_preview_click_events SET user_id = 'excluded' "
        "WHERE cron_task_id = 'app-a-100-included'",
    )
    service = QueryService()
    branch = await service.get_branch_behavior(
        "2026-06-01",
        "2026-06-15",
        "100",
        "app-a",
    )
    manager = await service.get_branch_manager_summary(
        "100",
        "2026-06-01",
        "2026-06-15",
        "app-a",
    )
    for item in (branch.items[0], manager.items[0]):
        assert item.success_count == 1
        assert item.viewed_customers == 0
        assert item.insight_customers == 0
        assert item.phone_customers == 0
        assert item.contacted_customers == 0
        assert item.contact_rate == 0.0


@pytest.mark.asyncio
async def test_branches_with_only_non_roster_executions_are_excluded(
    populated_db,
):
    populated_db.connection.execute(
        "DELETE FROM swe_cron_executions WHERE tenant_id = 'included' "
        "AND job_id LIKE '%-200-%'",
    )
    result = await QueryService().get_branch_behavior(
        "2026-06-01",
        "2026-06-15",
        source_id="app-a",
    )
    assert [item.bbk_id for item in result.items] == ["100"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    [
        "_fetch_branch_skill_total_tasks",
        "_fetch_branch_skill_job_ids",
        "_fetch_branch_execution_stats",
        "_fetch_branch_skill_count",
        "_fetch_branch_skill_involved_managers",
        "_fetch_branch_skill_result_view_managers",
        "_fetch_branch_skill_manager_click_counts",
        "_fetch_branch_skill_customer_click_counts",
        "_fetch_branch_skill_recommended_customers",
        "_fetch_manager_base_info",
        "_fetch_manager_skill_count",
        "_fetch_manager_recommended_customers",
        "_fetch_manager_click_stats",
        "_fetch_manager_contact_stats",
    ],
)
async def test_branch_scoped_queries_exclude_other_roster_branches(
    populated_db,
    method_name,
):
    # 用户在快照中属于 100 分行，虽然在 200 分行有业务记录也不应计入。
    method = getattr(QueryService(), method_name)
    arguments = {
        "db": populated_db,
        "bbk_id": "200",
        "start_time": datetime(2026, 6, 1),
        "end_time": datetime(2026, 6, 15),
        "source_id": "app-a",
        "sync_date": "2026-06-30",
        "job_ids": ["app-a-200-included"],
    }
    result = await method(
        **{
            name: arguments[name]
            for name in inspect.signature(method).parameters
        }
    )
    if isinstance(result, dict):
        assert not any(result.values())
    else:
        assert not result
    sql, params = populated_db.calls[-1]
    assert "jkh.first_bbk_id = %s" in sql
    assert params[params.index("2026-06-30") + 1] == "200"


@pytest.mark.asyncio
async def test_branch_loop_passes_branch_to_execution_stats(populated_db):
    await QueryService().get_branch_behavior(
        "2026-06-01",
        "2026-06-15",
        "100",
        "app-a",
    )
    execution_sql, params = next(
        (sql, params)
        for sql, params in populated_db.calls
        if "AS total_executions" in sql
    )
    assert "jkh.first_bbk_id = %s" in execution_sql
    assert params[-2:] == ("2026-06-30", "100")
