# -*- coding: utf-8 -*-
"""串联真实新路由、跨服务HTTP客户端和共库SQL，不连接外部业务服务。"""

import asyncio
import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tests.unit.app.test_managed_cron_execution import Engine
from tests.unit.app.test_managed_cron_resources import Chats
from tests.unit.scheduler.test_managed_cron_callbacks import CallbackDatabase

ROOT = "/api/scheduler/cron/jobs"


class PlatformBoundary:
    """仅替代外部平台传输，保留真实平台客户端的协议转换。"""

    def __init__(self):
        self.jobs = {}
        self.calls = []

    def respond(self, request):
        body = json.loads(request.content)
        action = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((action, body))
        if action == "add-job":
            identifier = str(len(self.jobs) + 1)
            self.jobs[identifier] = {**body, "enabled": False}
            return httpx.Response(
                200,
                json={"code": 200, "content": identifier},
            )
        identifier = str(body["id"])
        assert identifier in self.jobs
        if action == "update-job":
            self.jobs[identifier].update(body)
        else:
            assert action == "update-job-run-states"
            self.jobs[identifier]["enabled"] = bool(body["runFlag"])
        return httpx.Response(200, json={"code": 200})


@pytest.fixture
async def chain(monkeypatch):
    from scheduler.app.routers import (
        api_router,
        managed_cron,
        managed_cron_management,
        managed_cron_dispatch_state,
    )
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from scheduler.app.services.cron.managed.platform import (
        ManagedPlatformClient,
    )
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )
    from scheduler.app.services.cron.managed.swe_client import ManagedSweClient
    from swe.app.crons import managed_api, managed_execution_api
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_resources import ResourcePreparer
    from swe.app.crons.managed_results import ExecutionWriter

    db, chats, engine, boundary = (
        CallbackDatabase(),
        Chats(),
        Engine(),
        PlatformBoundary(),
    )
    swe = FastAPI()
    swe.state.db_connection = db
    swe.include_router(managed_api.router, prefix="/api")
    swe.include_router(managed_execution_api.router, prefix="/api")
    sent = []

    async def resources(request):
        return ResourcePreparer(
            chats,
            agent_id=request.headers["X-Agent-Id"],
            workflow_store=getattr(swe.state, "workflow_config_store", None),
        )

    async def execution(_request):
        return ManagedExecutionService(ExecutionWriter(db), engine)

    async def observe(request):
        sent.append(
            (
                request.url.path,
                dict(request.headers),
                json.loads(request.content) if request.content else {},
            ),
        )

    monkeypatch.setattr(managed_api, "get_resource_preparer", resources)
    monkeypatch.setattr(
        managed_execution_api,
        "get_execution_service",
        execution,
    )
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=swe),
            base_url="http://swe",
            event_hooks={"request": [observe]},
        ) as swe_http,
        httpx.AsyncClient(
            transport=httpx.MockTransport(boundary.respond),
        ) as platform_http,
    ):
        client = ManagedSweClient("http://swe", http=swe_http)
        platform = ManagedPlatformClient(
            {"base_url": "http://platform", "callback_base": "http://gateway"},
            http=platform_http,
        )
        service = ManagementService(db, client, platform)
        scheduler = FastAPI()
        scheduler.state.managed_cron_runtime_prepared = True
        scheduler.state.managed_cron_runtime_ready = True
        scheduler.include_router(api_router, prefix="/api")
        scheduler.dependency_overrides[
            managed_cron_management.management_database
        ] = lambda: db
        scheduler.dependency_overrides[
            managed_cron_management.get_management_service
        ] = lambda: service
        scheduler.dependency_overrides[managed_cron.get_managed_repository] = (
            lambda: ManagedCronRepository(db)
        )
        scheduler.dependency_overrides[
            managed_cron_dispatch_state.dispatch_service
        ] = lambda: DispatchStateService(db, platform)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=scheduler),
                base_url="http://scheduler",
            ) as http:
                yield SimpleNamespace(
                    db=db,
                    http=http,
                    swe=swe,
                    swe_http=swe_http,
                    client=client,
                    platform=platform,
                    boundary=boundary,
                    chats=chats,
                    engine=engine,
                    sent=sent,
                )
        finally:
            await asyncio.gather(
                *getattr(swe.state, "managed_cron_tasks", set()),
            )
            db.conn.close()


def headers(agent="default"):
    return {
        "X-User-Id": "alice",
        "X-Tenant-Id": "alice",
        "X-Source-Id": "bank",
        "X-Agent-Id": agent,
    }


def create_body(mode="ordinary"):
    return {
        "request_id": "http-chain-create",
        "dispatch_state": mode,
        "spec": {
            "name": "测试任务",
            "task_type": "text",
            "text": "无业务副作用的测试内容",
            "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["default", "research"])
async def test_crud_execution_and_cleanup_through_real_service_http(
    chain,
    agent,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
        ManagedSweExecutionClient,
    )
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    context = headers(agent)
    created = await chain.http.post(ROOT, headers=context, json=create_body())
    assert created.status_code == 201, created.text
    operation = created.json()["operation"]
    path = f"{ROOT}/{operation['job_id']}"
    first = (await chain.http.get(path, headers=context)).json()
    assert first["management_status"] == "ready"
    assert first["agent_id"] == agent
    assert len(first["state"]["next_run_times"]) == 3
    repeated = await chain.http.post(ROOT, headers=context, json=create_body())
    assert repeated.json()["operation"]["job_id"] == operation["job_id"]
    update = await chain.http.put(
        path,
        headers=context,
        json={
            "request_id": "http-chain-update",
            "expected_definition_version": 1,
            "spec": {**create_body()["spec"], "name": "更新名称"},
        },
    )
    assert update.status_code == 200, update.text
    current = (await chain.http.get(path, headers=context)).json()
    assert current["definition_version"] == 2
    assert current["task"]["chat_id"] == first["task"]["chat_id"]
    assert chain.chats.created == 1

    submitted = await chain.http.post(
        f"{path}/run",
        headers=context,
        json={
            "request_id": "http-chain-run",
            "expected_definition_version": 2,
        },
    )
    assert submitted.status_code == 202, submitted.text
    execution_id = submitted.json()["execution"]["execution_id"]
    dispatcher = ExecutionDispatcher(
        chain.db,
        ManagedSweExecutionClient("http://swe", http=chain.swe_http),
    )
    await dispatcher.dispatch_registered(execution_id)
    await asyncio.gather(*chain.swe.state.managed_cron_tasks)
    result = (
        await chain.http.get(
            f"{path}/executions/{execution_id}",
            headers=context,
        )
    ).json()
    assert result["status"] == "success"
    assert result["settled_status"] is None
    assert len(chain.engine.calls) == 1
    await dispatcher.dispatch_registered(execution_id)
    assert len(chain.engine.calls) == 1
    # Monitor 在此为边界替身：只注入异步已结束事实，实际收敛逻辑仍读共库执行。
    chain.db.conn.execute(
        "UPDATE swe_cron_executions SET async_status='success' WHERE id=?",
        (result["history_execution_id"],),
    )
    assert await ExecutionSettlement(chain.db).tick() == 1
    deleted = await chain.http.request(
        "DELETE",
        path,
        headers=context,
        json={
            "request_id": "http-chain-delete",
            "expected_definition_version": 2,
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert (await chain.http.get(path, headers=context)).status_code == 404
    assert (
        await chain.http.get(
            path,
            headers=context,
            params={"include_deleted": True},
        )
    ).json()["deleted"]
    assert not chain.chats.rows
    assert len(chain.boundary.jobs) == 1
    assert not next(iter(chain.boundary.jobs.values()))["enabled"]
    assert all(
        body.keys()
        <= {"operation_id", "execution_id", "job_id", "dispatch_token"}
        for _, _, body in chain.sent
    )
    assert all(
        sent_headers["x-user-id"] == "alice"
        and sent_headers["x-agent-id"] == agent
        for _, sent_headers, _ in chain.sent
    )


@pytest.mark.asyncio
async def test_distribution_and_modes_keep_real_actor_across_service_headers(
    chain,
):
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.identity import CallerIdentity

    context = headers("research")
    created = await chain.http.post(
        ROOT,
        headers=context,
        json=create_body("batch_paused"),
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["operation"]["job_id"]
    path = f"{ROOT}/{job_id}"
    submitted = await chain.http.post(
        f"{path}/broadcast",
        headers=context,
        json={
            "request_id": "http-chain-broadcast",
            "expected_definition_version": 1,
            "targets": [{"tenant_id": "bob"}, {"tenant_id": "carol"}],
            "enable_offset": False,
            "offset_window_hours": 0,
        },
    )
    assert submitted.status_code == 202, submitted.text
    task_id = submitted.json()["task_id"]
    await BroadcastProcessor(chain.db, chain.client, chain.platform).process(
        task_id,
    )
    current = await chain.http.get(
        f"{path}/broadcast/tasks/current",
        headers=context,
    )
    assert current.status_code == 200, current.text
    assert current.json()["task"]["task_id"] == task_id
    progress = (
        await chain.http.get(
            f"{path}/broadcast/tasks/{task_id}",
            headers=context,
        )
    ).json()
    assert progress["status"] == "succeeded"
    assert progress["success_count"] == 2
    children = (
        await chain.http.get(f"{path}/broadcast/children", headers=context)
    ).json()["items"]
    assert {item["tenant_id"] for item in children} == {"bob", "carol"}
    assert all(item["agent_id"] == "research" for item in children)
    assert chain.chats.created == 3
    assert len(chain.boundary.jobs) == 1
    prepare_headers = [
        item[1] for item in chain.sent if item[0].endswith("/prepare")
    ]
    assert {item["x-tenant-id"] for item in prepare_headers} == {
        "alice",
        "bob",
        "carol",
    }
    assert all(
        item["x-user-id"] == "alice" and "x-user-role" not in item
        for item in prepare_headers
    )
    modes = DispatchStateService(chain.db, chain.platform)
    caller = CallerIdentity("alice", "bank", "research", "alice")
    for mode in ("batch_enabled", "ordinary", "batch_paused"):
        state = (
            await chain.http.get(f"{path}/dispatch-state", headers=context)
        ).json()
        accepted = await chain.http.put(
            f"{path}/dispatch-state",
            headers=context,
            json={
                "request_id": f"http-chain-{mode}",
                "state": mode,
                "expected_version": state["version"],
            },
        )
        assert accepted.status_code == 202, accepted.text
        await modes.process(accepted.json()["operation_id"])
        final = await modes.get(caller, job_id)
        assert final["sync_status"] == "completed"
        assert final["effective_state"] == mode
    assert (
        chain.db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_existing_skill_lookup_can_supply_new_scheduler_workflow_creation(
    chain,
):
    from swe.app.crons import api as legacy
    from swe.app.crons.workflow.config_store import WorkflowConfigStore

    # 仅建立测试库的既有 Workflow 表，不调用真实应用的建表逻辑。
    chain.db.conn.executescript("""
        CREATE TABLE swe_workflow_bindings (
            binding_id TEXT PRIMARY KEY, source_id TEXT, skill_id TEXT,
            current_version INTEGER, enabled INTEGER
        );
        CREATE TABLE swe_workflow_binding_versions (
            binding_id TEXT, version INTEGER, skill_id TEXT, config_json TEXT
        );
        INSERT INTO swe_workflow_bindings VALUES ('http-binding','bank','skill-report',3,1);
    """)
    config = {
        "url": "https://workflow.test/run",
        "provider_id": "provider",
        "model_id": "model",
        "result_fields": {"result": ["data", "result"]},
    }
    chain.db.conn.execute(
        "INSERT INTO swe_workflow_binding_versions VALUES ('http-binding',3,'skill-report',?)",
        (json.dumps(config),),
    )
    chain.swe.state.workflow_config_store = WorkflowConfigStore(chain.db)
    chain.swe.include_router(legacy.router, prefix="/api")

    @chain.swe.middleware("http")
    async def source_context(request, call_next):
        # 替代已有租户中间件的来源注入，不替代原接口角色和绑定权限检查。
        request.state.source_id = request.headers.get("X-Source-Id")
        return await call_next(request)

    lookup = "/api/cron/workflow-bindings/skill-report"
    assert (
        await chain.swe_http.get(lookup, headers=headers())
    ).status_code == 403
    manager = {**headers(), "X-User-Role": "manager"}
    found = await chain.swe_http.get(lookup, headers=manager)
    assert found.status_code == 200, found.text
    assert found.json()["binding_id"] == "http-binding"
    assert found.json()["version"] == 3
    assert "workflow_id" not in found.json()
    assert (
        await chain.swe_http.get(
            lookup,
            headers={**manager, "X-Source-Id": "another-bank"},
        )
    ).status_code == 404
    body = create_body("batch_paused")
    body["spec"].pop("text")
    body["spec"].update(
        task_type="workflow",
        skill_ids="skill-report",
        workflow_binding_id=found.json()["binding_id"],
    )
    created = await chain.http.post(ROOT, headers=manager, json=body)
    assert created.status_code == 201, created.text
    detail = (
        await chain.http.get(
            f"{ROOT}/{created.json()['operation']['job_id']}",
            headers=manager,
        )
    ).json()
    assert detail["spec"]["workflow_binding_id"] == "http-binding"
    assert detail["spec"]["task_type"] == "workflow"
    assert detail["management_status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected_runs", [("ordinary", 1), ("batch", 3)])
@pytest.mark.parametrize("envelope", ["flat", "encoded"])
async def test_each_platform_arrival_reaches_swe_through_new_callback(
    chain,
    kind,
    expected_runs,
    envelope,
):
    from scheduler.app.services.cron.managed.distribution_worker import (
        BroadcastProcessor,
    )
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
        ManagedSweExecutionClient,
    )

    context = headers("research")
    fire = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    command = create_body(
        "ordinary" if kind == "ordinary" else "batch_enabled",
    )
    command["spec"]["schedule"] = {
        "cron": f"{fire.minute} {fire.hour} * * *",
        "timezone": "UTC",
    }
    created = await chain.http.post(ROOT, headers=context, json=command)
    assert created.status_code == 201, created.text
    job_id = created.json()["operation"]["job_id"]
    if kind == "batch":
        submitted = await chain.http.post(
            f"{ROOT}/{job_id}/broadcast",
            headers=context,
            json={
                "request_id": "callback-broadcast",
                "expected_definition_version": 1,
                "targets": [{"tenant_id": "bob"}, {"tenant_id": "carol"}],
                "enable_offset": False,
                "offset_window_hours": 0,
            },
        )
        assert submitted.status_code == 202, submitted.text
        await BroadcastProcessor(
            chain.db,
            chain.client,
            chain.platform,
        ).process(submitted.json()["task_id"])
    identifier, timer = next(iter(chain.boundary.jobs.items()))
    assert timer["enabled"]
    assert (
        timer["jobAddress"]
        == f"http://gateway/api/scheduler/cron/{kind}/callback"
    )
    callback = {
        "jobId": int(identifier),
        "jobParam": timer["jobParam"],
        "logId": 10001,
        "fireTime": fire.isoformat(),
    }
    if envelope == "flat":
        callback = json.loads(base64.urlsafe_b64decode(timer["jobParam"]))
    # 平台封装中的完整身份来自真实客户端，不另造 Header 或操作引用。
    first = await chain.http.post(
        f"/api/scheduler/cron/{kind}/callback",
        json=callback,
    )
    assert first.status_code == 202, first.text
    assert first.json()["accepted"]
    original = first.json()
    dispatcher = ExecutionDispatcher(
        chain.db,
        ManagedSweExecutionClient("http://swe", http=chain.swe_http),
    )
    rows = await chain.db.fetch_all(
        "SELECT execution_id FROM swe_cron_execution_runs ORDER BY queue_order",
    )
    assert len(rows) == expected_runs
    for row in rows:
        await dispatcher.dispatch_registered(row["execution_id"])
        await asyncio.gather(*chain.swe.state.managed_cron_tasks)
        # 平台新请求不去重，不等于允许同一执行 ID 的内部重试再跑业务。
        await dispatcher.dispatch_registered(row["execution_id"])
    assert len(chain.engine.calls) == expected_runs
    repeated = await chain.http.post(
        f"/api/scheduler/cron/{kind}/callback",
        json=callback,
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["accepted"] and not repeated.json()["reused"]
    reference = "execution_id" if kind == "ordinary" else "batch_id"
    assert repeated.json()[reference] != original[reference]
    pending = await chain.db.fetch_all(
        "SELECT execution_id FROM swe_cron_execution_runs WHERE status='queued' ORDER BY queue_order",
    )
    assert len(pending) == expected_runs
    for row in pending:
        await dispatcher.dispatch_registered(row["execution_id"])
        await asyncio.gather(*chain.swe.state.managed_cron_tasks)
    assert len(chain.engine.calls) == expected_runs * 2
    assert (
        chain.db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs WHERE status='success'",
        ).fetchone()[0]
        == expected_runs * 2
    )
    assert (
        chain.db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_executions",
        ).fetchone()[0]
        == expected_runs * 2
    )
    assert all(item[1]["x-user-id"] == "alice" for item in chain.sent)
