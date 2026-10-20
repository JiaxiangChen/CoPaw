# -*- coding: utf-8 -*-
"""使用真实关系查询验证 Scheduler 新任务查询接口。"""

import base64
import json
import sqlite3

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_state_join_cannot_mix_case_distinct_identity_components():
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )

    db = QueryDatabase()
    db.add("new-job")
    db.conn.execute("UPDATE swe_cron_job_states SET source_id='BANK'")
    view = await ManagedCronRepository(db).detail(
        CallerIdentity("alice", "bank", "default", "alice"),
        "new-job",
    )
    assert view["management_status"] == "state_missing"
    assert not view["task"]["chat_id"]
    db.conn.close()


class QueryDatabase:
    """SQLite 适配器执行仓库的真实 SQL，不伪造查询结果。"""

    is_connected = True

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.create_collation(
            "utf8mb4_bin",
            lambda left, right: (left > right) - (left < right),
        )
        self.writes = 0
        self.broken = False
        self.conn.executescript(
            """
            CREATE TABLE swe_cron_jobs_v2 (
                tenant_id TEXT COLLATE NOCASE, source_id TEXT COLLATE NOCASE,
                agent_id TEXT COLLATE NOCASE, job_id TEXT COLLATE NOCASE,
                scope_id TEXT, name TEXT, task_type TEXT, enabled INTEGER,
                cron_expr TEXT, timezone TEXT, spec_json TEXT,
                tenant_name TEXT DEFAULT '', bbk_id TEXT DEFAULT '',
                skill_ids TEXT DEFAULT '', workflow_binding_id TEXT,
                offset_minutes INTEGER DEFAULT 0,
                offset_window_hours INTEGER DEFAULT 4,
                enable_offset INTEGER DEFAULT 0,
                management_owner TEXT, creator_user_id TEXT COLLATE NOCASE,
                definition_version INTEGER DEFAULT 1, deleted_at TEXT,
                parent_tenant_id TEXT, parent_source_id TEXT,
                parent_agent_id TEXT, parent_job_id TEXT,
                PRIMARY KEY (tenant_id, source_id, agent_id, job_id)
            );
            CREATE TABLE swe_cron_job_states (
                tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
                task_chat_id TEXT, task_session_id TEXT,
                ordinary_external_job_id TEXT, batch_external_job_id TEXT,
                dispatch_state TEXT, pause_reason TEXT, auto_paused_at TEXT,
                next_run_at TEXT, next_run_times_json TEXT,
                last_status TEXT, last_run_at TEXT, last_prefetch_at TEXT,
                last_error TEXT, has_scheduled_result INTEGER,
                latest_scheduled_preview TEXT, last_scheduled_run_at TEXT,
                unread_execution_count INTEGER,
                management_status TEXT, management_version INTEGER DEFAULT 1,
                state_version INTEGER DEFAULT 1,
                queue_invalidation_version INTEGER DEFAULT 0,
                PRIMARY KEY (tenant_id, source_id, agent_id, job_id)
            );
            """,
        )

    async def fetch_all(self, query, params=None):
        if self.broken:
            raise RuntimeError("database unavailable: private credentials")
        if not query.lstrip().upper().startswith("SELECT "):
            self.writes += 1
        return [
            dict(row)
            for row in self.conn.execute(
                query.replace("%s", "?"),
                params or (),
            ).fetchall()
        ]

    async def fetch_one(self, query, params=None):
        rows = await self.fetch_all(query, params)
        return rows[0] if rows else None

    def add(
        self,
        job_id,
        *,
        tenant="alice",
        source="bank",
        agent="default",
        owner="scheduler",
        actor=None,
        parent=None,
        deleted=False,
        with_state=True,
        enabled=True,
        **state,
    ):
        identity = (tenant, source, agent, job_id)
        spec = {
            "id": job_id,
            "name": job_id,
            "enabled": enabled,
            "tenant_id": tenant,
            "source_id": source,
            "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
            "task_type": "agent",
            "request": {"input": "report"},
            "dispatch": {"target": {"user_id": tenant, "session_id": "s1"}},
            "meta": {"Authorization": "private-token", "user_note": "keep"},
        }
        values = {
            **dict(
                zip(
                    ("tenant_id", "source_id", "agent_id", "job_id"),
                    identity,
                ),
            ),
            "scope_id": "stored-scope",
            "name": job_id,
            "task_type": "agent",
            "enabled": int(enabled),
            "cron_expr": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "spec_json": json.dumps(spec),
            "management_owner": owner,
            "creator_user_id": actor or tenant,
            "deleted_at": "2026-10-15" if deleted else None,
        }
        if parent:
            values.update(
                zip(
                    (
                        "parent_tenant_id",
                        "parent_source_id",
                        "parent_agent_id",
                        "parent_job_id",
                    ),
                    parent,
                ),
            )
        self._insert("swe_cron_jobs_v2", values)
        if with_state:
            self._insert(
                "swe_cron_job_states",
                {
                    **dict(
                        zip(
                            ("tenant_id", "source_id", "agent_id", "job_id"),
                            identity,
                        ),
                    ),
                    "management_status": "ready",
                    "dispatch_state": "ordinary",
                    "task_chat_id": f"chat-{job_id}",
                    "task_session_id": "s1",
                    "unread_execution_count": 2,
                    "last_status": "success",
                    **state,
                },
            )

    def _insert(self, table, values):
        columns = ",".join(values)
        marks = ",".join("?" for _ in values)
        self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})",
            tuple(values.values()),
        )


@pytest.fixture
def database():
    db = QueryDatabase()
    yield db
    db.conn.close()


@pytest.fixture
async def client(database):
    from scheduler.app.routers.managed_cron import (
        get_managed_repository,
        router,
    )
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_managed_repository] = (
        lambda: ManagedCronRepository(database)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        yield http


HEADERS = {
    "X-User-Id": "alice",
    "X-Tenant-Id": "alice",
    "X-Source-Id": "bank",
    "X-Agent-Id": "default",
    "X-Scope-Id": "alice-bank",
}


@pytest.mark.asyncio
async def test_children_expose_safe_display_profile_without_task_body(
    client,
    database,
):
    parent = ("alice", "bank", "default", "profile-parent")
    database.add("profile-parent")
    database.add("profile-child", tenant="bob", parent=parent)
    database.add("missing-profile", tenant="carol", parent=parent)
    row = database.conn.execute(
        "SELECT spec_json FROM swe_cron_jobs_v2 "
        "WHERE job_id='profile-child'",
    ).fetchone()
    spec = json.loads(row["spec_json"])
    spec.update(tenant_name="张三", bbk_id="755")
    database.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET spec_json=? WHERE job_id='profile-child'",
        (json.dumps(spec),),
    )
    response = await client.get(
        f"{PATH}/profile-parent/broadcast/children",
        headers=HEADERS,
    )
    assert response.status_code == 200
    items = {item["job_id"]: item for item in response.json()["items"]}
    assert items["profile-child"]["tenant_name"] == "张三"
    assert items["profile-child"]["bbk_id"] == "755"
    assert items["missing-profile"]["tenant_name"] is None
    assert items["missing-profile"]["bbk_id"] is None
    assert not {"request", "text", "meta"}.intersection(items["profile-child"])


@pytest.mark.asyncio
async def test_query_computes_fresh_display_times_without_writing_state(
    database,
):
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )

    database.add(
        "display",
        next_run_at="1999-01-01",
        next_run_times_json='["1999-01-01"]',
    )
    before = datetime.now(timezone.utc)
    result = await ManagedCronRepository(database).detail(
        CallerIdentity("alice", "bank", "default", "alice"),
        "display",
    )
    times = [
        datetime.fromisoformat(item)
        for item in result["state"]["next_run_times"]
    ]
    assert len(times) == 3
    assert all(item > before for item in times)
    assert [(item.hour, item.minute) for item in times] == [(9, 0)] * 3
    assert all(item.utcoffset().total_seconds() == 8 * 3600 for item in times)
    assert (
        result["state"]["next_run_at"] == result["state"]["next_run_times"][0]
    )
    assert (
        database.conn.execute(
            "SELECT next_run_at FROM swe_cron_job_states",
        ).fetchone()[0]
        == "1999-01-01"
    )
    assert database.writes == 0


def test_schedule_preview_uses_actual_offset_cron_and_advances_past_exact_tick():
    from datetime import datetime, timezone
    from scheduler.app.services.cron.managed.views import _next_run_times

    row = {"cron_expr": "0 7 * * *", "timezone": "Asia/Shanghai"}
    times = _next_run_times(
        row,
        now=datetime(2026, 10, 15, 23, tzinfo=timezone.utc),
    )
    assert times == [
        "2026-10-17T07:00:00+08:00",
        "2026-10-18T07:00:00+08:00",
        "2026-10-19T07:00:00+08:00",
    ]


@pytest.mark.parametrize(
    "zone,expression",
    [("invalid/zone", "0 9 * * *"), ("UTC", "bad cron")],
)
def test_invalid_schedule_is_not_exposed_as_a_successful_empty_preview(
    zone,
    expression,
):
    from scheduler.app.services.cron.managed.contracts import (
        ManagedCronUnavailable,
    )
    from scheduler.app.services.cron.managed.views import _next_run_times

    with pytest.raises(ManagedCronUnavailable, match="cron_data_unavailable"):
        _next_run_times({"cron_expr": expression, "timezone": zone})


PATH = "/api/scheduler/cron/jobs"


@pytest.mark.asyncio
async def test_list_pages_only_scheduler_owned_scope(client, database):
    for job in ("c", "a", "b"):
        database.add(job)
    database.add("legacy", owner="legacy")
    database.add("other-source", source="other")
    database.add("other-agent", agent="other")
    database.add("deleted", deleted=True)
    response = await client.get(PATH, headers=HEADERS, params={"limit": 2})
    assert response.status_code == 200
    page = response.json()
    assert [item["id"] for item in page["items"]] == ["a", "b"]
    assert page["next_cursor"]
    next_page = await client.get(
        PATH,
        headers=HEADERS,
        params={"cursor": page["next_cursor"]},
    )
    assert [item["id"] for item in next_page.json()["items"]] == ["c"]
    assert next_page.json()["next_cursor"] is None
    assert database.writes == 0


@pytest.mark.asyncio
async def test_detail_maps_state_without_leaking_secrets(client, database):
    database.add("job", last_status="running", pause_reason="manual")
    response = await client.get(f"{PATH}/job", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["management_owner"] == "scheduler"
    assert body["task"]["chat_id"] == "chat-job"
    assert body["task"]["unread_execution_count"] == 2
    assert body["task"]["is_running"] is True
    assert body["task"]["is_paused"] is True
    assert body["spec"]["meta"] == {"user_note": "keep"}
    assert "private-token" not in response.text
    assert database.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"X-Scope-Id": "someone-else"},
        {"X-User-Id": ""},
        {"X-Source-Id": ""},
        {"X-Tenant-Id": "../tenant"},
        {"X-Agent-Id": "agent/escape"},
    ],
)
async def test_invalid_identity_is_rejected(client, changes):
    assert (
        await client.get(PATH, headers={**HEADERS, **changes})
    ).status_code == 400


@pytest.mark.asyncio
async def test_canonical_scope_header_matches_swe_codec(client):
    from swe.config.context import encode_scope_id

    scope = encode_scope_id("alice", "bank")
    for value in (scope, f"scope.v1.{scope}"):
        assert (
            await client.get(PATH, headers={**HEADERS, "X-Scope-Id": value})
        ).status_code == 200


@pytest.mark.asyncio
async def test_missing_state_is_not_presented_as_ready(client, database):
    database.add("unready", with_state=False)
    body = (await client.get(f"{PATH}/unready", headers=HEADERS)).json()
    assert body["management_status"] == "state_missing"
    assert body["task"]["is_running"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner,deleted",
    [("legacy", False), ("scheduler", True)],
)
async def test_legacy_and_deleted_do_not_fall_back_to_old_storage(
    client,
    database,
    owner,
    deleted,
):
    database.add("hidden", owner=owner, deleted=deleted)
    assert (
        await client.get(f"{PATH}/hidden", headers=HEADERS)
    ).status_code == 404


@pytest.mark.asyncio
async def test_owner_can_view_cleanup_but_not_recipient_siblings(
    client,
    database,
):
    database.add("gone", deleted=True)
    database.add("bob", tenant="bob")
    response = await client.get(
        f"{PATH}/gone?include_deleted=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert (
        await client.get(f"{PATH}/bob", headers=HEADERS)
    ).status_code == 404


@pytest.mark.asyncio
async def test_source_manager_reads_children_across_target_tenants(
    client,
    database,
):
    parent = ("alice", "bank", "default", "parent")
    database.add("parent")
    database.add("child-a", tenant="bob", parent=parent)
    database.add("child-b", tenant="carol", parent=parent)
    database.add(
        "foreign",
        tenant="dan",
        parent=("alice", "other", "default", "parent"),
    )
    database.add("gone", tenant="eve", parent=parent, deleted=True)
    response = await client.get(
        f"{PATH}/parent/broadcast/children",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert [item["job_id"] for item in response.json()["items"]] == [
        "child-a",
        "child-b",
    ]
    assert (
        await client.get(
            f"{PATH}/parent/broadcast/children",
            headers={
                **HEADERS,
                "X-User-Id": "bob",
                "X-Tenant-Id": "alice",
            },
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_recipient_cannot_enumerate_source_or_other_recipients(
    client,
    database,
):
    parent = ("alice", "bank", "default", "parent")
    database.add("parent")
    database.add("child", tenant="bob", parent=parent)
    recipient = {
        **HEADERS,
        "X-User-Id": "bob",
        "X-Tenant-Id": "bob",
        "X-Scope-Id": "bob-bank",
    }
    assert (
        await client.get(f"{PATH}/child", headers=recipient)
    ).status_code == 200
    assert (
        await client.get(
            f"{PATH}/parent/broadcast/children",
            headers=recipient,
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_cursor_cannot_be_replayed_in_another_scope(client, database):
    database.add("a")
    database.add("b")
    page = (
        await client.get(PATH, headers=HEADERS, params={"limit": 1})
    ).json()
    response = await client.get(
        PATH,
        headers={
            **HEADERS,
            "X-Source-Id": "other",
            "X-Scope-Id": "alice-other",
        },
        params={"cursor": page["next_cursor"]},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_invalid_cursor_limits_and_database_errors_are_not_empty_success(
    client,
    database,
):
    assert (
        await client.get(PATH, headers=HEADERS, params={"limit": 201})
    ).status_code == 422
    assert (
        await client.get(PATH, headers=HEADERS, params={"cursor": "invalid"})
    ).status_code == 400
    database.broken = True
    response = await client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert "private credentials" not in response.text


@pytest.mark.asyncio
async def test_duplicate_job_ids_in_different_scopes_remain_distinct(
    client,
    database,
):
    database.add("same")
    database.add("same", tenant="bob")
    body = (await client.get(PATH, headers=HEADERS)).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["tenant_id"] == "alice"


@pytest.mark.asyncio
async def test_children_filter_is_scoped_and_cursor_binds_filter(
    client,
    database,
):
    parent = ("alice", "bank", "default", "parent")
    database.add("parent")
    for tenant in ("bob", "carol", "dan"):
        database.add("child", tenant=tenant, parent=parent)
    path = f"{PATH}/parent/broadcast/children"
    response = await client.get(
        path,
        headers=HEADERS,
        params=[
            ("tenant_ids", "bob"),
            ("tenant_ids", "carol"),
            ("limit", 1),
        ],
    )
    assert response.status_code == 200
    page = response.json()
    assert [item["tenant_id"] for item in page["items"]] == ["bob"]
    last = await client.get(
        path,
        headers=HEADERS,
        params=[
            ("tenant_ids", "bob"),
            ("tenant_ids", "carol"),
            ("cursor", page["next_cursor"]),
        ],
    )
    assert [item["tenant_id"] for item in last.json()["items"]] == ["carol"]
    changed = await client.get(
        path,
        headers=HEADERS,
        params={
            "tenant_ids": "dan",
            "cursor": page["next_cursor"],
        },
    )
    assert changed.status_code == 400
    invalid = await client.get(
        path,
        headers=HEADERS,
        params={"tenant_ids": "../bob"},
    )
    assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_source_creator_can_read_child_detail_with_target_headers(
    client,
    database,
):
    parent = ("alice", "bank", "default", "parent")
    database.add("parent")
    database.add("child", tenant="bob", parent=parent)
    target = {**HEADERS, "X-Tenant-Id": "bob", "X-Scope-Id": "bob-bank"}
    response = await client.get(f"{PATH}/child", headers=target)
    assert response.status_code == 200
    assert response.json()["task"]["visible_in_my_tasks"] is False


@pytest.mark.asyncio
async def test_corrupt_spec_is_unavailable_not_empty_definition(
    client,
    database,
):
    database.add("bad")
    database.conn.execute("UPDATE swe_cron_jobs_v2 SET spec_json = '[]'")
    assert (
        await client.get(f"{PATH}/bad", headers=HEADERS)
    ).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["manager", "admin"])
async def test_existing_manager_roles_can_read_requested_workspace_only(
    client,
    database,
    role,
):
    database.add("owned-by-other", actor="someone-else")
    database.add("other-source", source="private", actor="someone-else")
    headers = {**HEADERS, "X-User-Role": role}
    response = await client.get(PATH, headers=headers)
    assert [item["id"] for item in response.json()["items"]] == [
        "owned-by-other",
    ]
    assert (
        await client.get(f"{PATH}/owned-by-other", headers=headers)
    ).status_code == 200
    assert (
        await client.get(f"{PATH}/other-source", headers=headers)
    ).status_code == 404


@pytest.mark.asyncio
async def test_missing_spec_is_not_rebuilt_from_partial_columns(
    client,
    database,
):
    database.add("bad")
    database.conn.execute("UPDATE swe_cron_jobs_v2 SET spec_json = NULL")
    assert (
        await client.get(f"{PATH}/bad", headers=HEADERS)
    ).status_code == 503


@pytest.mark.asyncio
async def test_deeply_nested_cursor_is_a_client_error(client):
    raw = "[" * 1100 + "]" * 1100
    token = base64.urlsafe_b64encode(raw.encode()).decode()
    response = await client.get(
        PATH,
        headers=HEADERS,
        params={"cursor": token},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_case_insensitive_database_does_not_expand_header_identity(
    client,
    database,
):
    database.add("job")
    different_actor = {**HEADERS, "X-User-Id": "ALICE"}
    assert (await client.get(PATH, headers=different_actor)).json()[
        "items"
    ] == []
    different_source = {
        **HEADERS,
        "X-Source-Id": "BANK",
        "X-Scope-Id": "alice-BANK",
        "X-User-Role": "manager",
    }
    assert (await client.get(PATH, headers=different_source)).json()[
        "items"
    ] == []
    assert (
        await client.get(f"{PATH}/job", headers=different_source)
    ).status_code == 404


@pytest.mark.asyncio
async def test_cursor_parser_recursion_limit_does_not_escape_as_server_error(
    client,
    monkeypatch,
):
    def parser_limit(_value):
        raise RecursionError("parser depth exceeded")

    monkeypatch.setattr(
        "scheduler.app.services.cron.managed.repository.json.loads",
        parser_limit,
    )
    response = await client.get(
        PATH,
        headers=HEADERS,
        params={"cursor": "e30="},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_children_return_actual_persisted_offset(client, database):
    parent = ("alice", "bank", "default", "parent")
    database.add("parent")
    database.add("child", tenant="bob", parent=parent)
    database.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET offset_minutes=240, enable_offset=1 "
        "WHERE tenant_id='bob'",
    )
    response = await client.get(
        f"{PATH}/parent/broadcast/children",
        headers=HEADERS,
    )
    item = response.json()["items"][0]
    assert item["offset_minutes"] == 240
    assert item["enable_offset"] is True
    assert item["offset_window_hours"] == 4


@pytest.mark.asyncio
async def test_registered_app_keeps_legacy_paths_and_adds_read_only_routes():
    from scheduler.app._app import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/api/scheduler/cron/callback"]
    assert "post" in paths["/api/scheduler/cron/execution"]
    assert "get" in paths[PATH]
    assert "get" in paths[f"{PATH}/{{job_id}}/broadcast/children"]
    assert "post" in paths[PATH]
