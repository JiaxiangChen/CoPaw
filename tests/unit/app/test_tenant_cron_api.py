# -*- coding: utf-8 -*-
"""Tenant injection regression tests for cron APIs."""

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from swe.config.context import encode_scope_id
from swe.config.context import tenant_context
from swe.providers.provider_manager import ProviderManager

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
sys.path.insert(0, str(SRC_ROOT))

_ORIGINAL_MODULES = {
    name: sys.modules.get(name)
    for name in [
        "swe.app.crons",
        "swe.app.channels.schema",
        "swe.app.crons.models",
        "swe.app.crons.manager",
        "swe.app.crons.api",
    ]
}

if "swe.app.crons" not in sys.modules:
    pkg = types.ModuleType("swe.app.crons")
    pkg.__path__ = [str(SRC_ROOT / "swe" / "app" / "crons")]
    sys.modules["swe.app.crons"] = pkg

channels_schema_module = types.ModuleType("swe.app.channels.schema")
channels_schema_module.ChannelType = str
channels_schema_module.DEFAULT_CHANNEL = "console"
sys.modules["swe.app.channels.schema"] = channels_schema_module

models_spec = importlib.util.spec_from_file_location(
    "swe.app.crons.models",
    SRC_ROOT / "swe" / "app" / "crons" / "models.py",
)
assert models_spec is not None and models_spec.loader is not None
models_module = importlib.util.module_from_spec(models_spec)
sys.modules["swe.app.crons.models"] = models_module
models_spec.loader.exec_module(models_module)

manager_module = types.ModuleType("swe.app.crons.manager")
manager_module.CronManager = object
sys.modules["swe.app.crons.manager"] = manager_module

api_spec = importlib.util.spec_from_file_location(
    "swe.app.crons.api",
    SRC_ROOT / "swe" / "app" / "crons" / "api.py",
)
assert api_spec is not None and api_spec.loader is not None
api_module = importlib.util.module_from_spec(api_spec)
sys.modules["swe.app.crons.api"] = api_module
api_spec.loader.exec_module(api_module)

for _name, _module in _ORIGINAL_MODULES.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module


class _TenantStateMiddleware:
    def __init__(
        self,
        app,
        tenant_id: str,
        source_id: str,
        user_name: str,
        bbk_id: str,
    ):
        self.app = app
        self.tenant_id = tenant_id
        self.source_id = source_id
        self.user_name = user_name
        self.bbk_id = bbk_id

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})
            scope["state"]["tenant_id"] = self.tenant_id
            scope["state"]["source_id"] = self.source_id
            scope["state"]["scope_id"] = encode_scope_id(
                self.tenant_id,
                self.source_id,
            )
            scope["state"]["user_name"] = self.user_name
            scope["state"]["bbk_id"] = self.bbk_id
        await self.app(scope, receive, send)


class _Manager:
    def __init__(self, jobs_by_id: dict[str, object] | None = None):
        self.created = []
        self.jobs_by_id = dict(jobs_by_id or {})
        self.deleted = []
        self.ran = []
        self.run_kwargs = []
        self.batch_enabled = []
        self.batch_disabled = []

    async def create_or_replace_job(self, spec):
        self.created.append(spec)
        self.jobs_by_id[spec.id] = spec

    async def list_jobs(self):
        return list(self.jobs_by_id.values())

    async def get_job(self, job_id):
        return self.jobs_by_id.get(job_id)

    async def delete_job(self, job_id):
        if job_id not in self.jobs_by_id:
            return False
        self.deleted.append(job_id)
        self.jobs_by_id.pop(job_id, None)
        return True

    async def run_job(self, job_id, **kwargs):
        if job_id not in self.jobs_by_id:
            raise KeyError(job_id)
        self.ran.append(job_id)
        self.run_kwargs.append(kwargs)

    async def enable_batch_dispatch_for_parent(
        self,
        job_id,
        *,
        offset_window_hours=4,
    ):
        job = self.jobs_by_id[job_id]
        meta = dict(job.meta or {})
        meta["broadcast_dispatch_intents_enabled"] = True
        meta["batch_dispatch_offset_window_hours"] = offset_window_hours
        updated = job.model_copy(update={"meta": meta})
        self.jobs_by_id[job_id] = updated
        self.batch_enabled.append(
            {
                "job_id": job_id,
                "offset_window_hours": offset_window_hours,
            },
        )
        return updated

    async def disable_batch_dispatch_for_parent(self, job_id):
        job = self.jobs_by_id[job_id]
        meta = dict(job.meta or {})
        meta.pop("broadcast_dispatch_intents_enabled", None)
        updated = job.model_copy(update={"meta": meta})
        self.jobs_by_id[job_id] = updated
        self.batch_disabled.append({"job_id": job_id})
        return updated

    def get_state(self, job_id):
        return types.SimpleNamespace(model_dump=lambda mode=None: {})


class _AsyncTaskDb:
    def __init__(self):
        self.is_connected = True
        self.executed = []
        self.executed_many = []

    async def execute(self, query, params=None):
        self.executed.append((query, params))
        return 1

    async def execute_many(self, query, params_list):
        self.executed_many.append((query, params_list))
        return len(params_list)

    async def fetch_one(self, query, params=None):
        self.executed.append((query, params))
        return None

    async def fetch_all(self, query, params=None):
        self.executed.append((query, params))
        return []


class _Provider:
    def __init__(self, models: list[str]):
        self._models = set(models)

    def has_model(self, model_id: str) -> bool:
        return model_id in self._models


class _ProviderManager:
    def __init__(self, providers: dict[str, _Provider]):
        self._providers = providers

    def get_provider(self, provider_id: str):
        return self._providers.get(provider_id)


class _Workspace:
    def __init__(
        self,
        cron_manager: _Manager,
        workspace_dir: str = "/tmp/workspaces/default",
    ):
        self.cron_manager = cron_manager
        self.workspace_dir = workspace_dir


class _MultiAgentManager:
    def __init__(self, workspaces: dict[str, _Workspace]):
        self._workspaces = workspaces

    async def get_agent(self, _agent_id: str, tenant_id: str):
        return self._workspaces[tenant_id]


class _TenantWorkspacePool:
    def __init__(self):
        self.calls = []

    async def ensure_bootstrap(
        self,
        tenant_id: str,
        source_id: str | None = None,
        tenant_name: str | None = None,
        bbk_id: str | None = None,
    ):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "source_id": source_id,
                "tenant_name": tenant_name,
                "bbk_id": bbk_id,
            },
        )
        return None


CronJobSpec = models_module.CronJobSpec
ScheduleSpec = models_module.ScheduleSpec
DispatchSpec = models_module.DispatchSpec
DispatchTarget = models_module.DispatchTarget
JobRuntimeSpec = models_module.JobRuntimeSpec
CronJobRequest = models_module.CronJobRequest


def _job_spec(
    job_id: str = "",
    *,
    task_type: str = "agent",
    model_slot: dict | None = None,
):
    payload = {
        "id": job_id,
        "name": "tenant cron",
        "enabled": True,
        "tenant_id": None,
        "schedule": ScheduleSpec(cron="* * * * *").model_dump(mode="json"),
        "dispatch": DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={},
        ).model_dump(mode="json"),
        "runtime": JobRuntimeSpec().model_dump(mode="json"),
        "meta": {},
    }
    if task_type == "agent":
        payload.update(
            {
                "task_type": "agent",
                "request": CronJobRequest(
                    input=[{"content": [{"type": "text", "text": "ping"}]}],
                ).model_dump(mode="json"),
            },
        )
    else:
        payload.update(
            {
                "task_type": "text",
                "text": "hello cron",
            },
        )
    if model_slot is not None:
        payload["model_slot"] = model_slot
    return payload


def _install_provider_manager(
    providers: dict[str, _Provider],
    providers_by_tenant: dict[str, dict[str, _Provider]] | None = None,
):
    tenant_providers = dict(providers_by_tenant or {})
    api_module.ProviderManager = types.SimpleNamespace(  # type: ignore[attr-defined]
        ensure_tenant_provider_storage=lambda _tenant_id: None,
        get_instance=lambda tenant_id: _ProviderManager(
            tenant_providers.get(tenant_id, providers),
        ),
    )


def _model_slot(
    provider_id: str = "openai",
    model: str = "gpt-5.4",
) -> dict[str, str]:
    return {
        "provider_id": provider_id,
        "model": model,
    }


def _build_client(
    manager: _Manager,
    *,
    multi_agent_manager: _MultiAgentManager | None = None,
    tenant_workspace_pool: _TenantWorkspacePool | None = None,
) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        _TenantStateMiddleware,
        tenant_id="tenant-a",
        source_id="source-a",
        user_name="Alice",
        bbk_id="1001",
    )
    app.include_router(api_module.router)
    if multi_agent_manager is not None:
        app.state.multi_agent_manager = multi_agent_manager
    if tenant_workspace_pool is not None:
        app.state.tenant_workspace_pool = tenant_workspace_pool

    async def _get_mgr():
        return manager

    app.dependency_overrides[api_module.get_cron_manager] = _get_mgr
    return TestClient(app)


@pytest.mark.parametrize("plan_id", [None, "plan-001"])
def test_create_and_broadcast_preserve_plan_id(plan_id):
    manager = _Manager()
    with _build_client(manager) as client:
        payload = _job_spec(task_type="text")
        if plan_id is not None:
            payload["planId"] = plan_id
        response = client.post("/cron/jobs", json=payload)
    assert response.status_code == 200
    source = manager.created[0]
    assert source.plan_id == plan_id
    assert response.json()["plan_id"] == plan_id
    child = api_module._build_broadcast_job(
        source,
        job_id="child-1",
        target_tenant_id="tenant-b",
        target_tenant_name="Bob",
        target_bbk_id="1002",
        source_id="source-a",
        cron=source.schedule.cron,
        timezone_name=source.schedule.timezone,
        offset_minutes=0,
        model_slot=None,
        model_slot_fallback_reason="",
    )
    assert child.plan_id == plan_id
    existing = child.model_copy(update={"plan_id": "old-plan"})
    refreshed = api_module._merge_existing_child_with_source(existing, child)
    assert refreshed.plan_id == plan_id


def test_create_job_injects_request_tenant_id():
    manager = _Manager()
    client = _build_client(manager)

    response = client.post("/cron/jobs", json=_job_spec())

    assert response.status_code == 200
    assert manager.created[0].tenant_id == "tenant-a"
    assert manager.created[0].source_id == "source-a"
    assert manager.created[0].scope_id == encode_scope_id(
        "tenant-a",
        "source-a",
    )
    assert manager.created[0].tenant_name == "Alice"
    assert manager.created[0].bbk_id == "1001"
    assert manager.created[0].model_slot is None
    assert response.json().get("model_slot") is None


def test_create_job_does_not_start_batch_dispatch_from_meta(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    manager = _Manager()
    client = _build_client(manager)
    scheduled = []

    async def _schedule(request, source_job, *, existing, reason):
        del request
        scheduled.append(
            {
                "job_id": source_job.id,
                "existing": existing,
                "reason": reason,
            },
        )

    monkeypatch.setattr(
        api_module,
        "_schedule_dispatch_broadcast_children_processing_after_save",
        _schedule,
        raising=False,
    )

    response = client.post(
        "/cron/jobs",
        json={
            **_job_spec(),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )

    assert response.status_code == 200
    assert scheduled == []
    assert "broadcast_dispatch_intents_enabled" not in manager.created[0].meta
    assert "broadcast_dispatch_intents_enabled" not in response.json()["meta"]


def test_replace_job_overrides_payload_tenant_with_request_tenant():
    manager = _Manager()
    client = _build_client(manager)

    response = client.put(
        "/cron/jobs/job-1",
        json={**_job_spec("job-1"), "tenant_id": "other-tenant"},
    )

    assert response.status_code == 200
    assert manager.created[0].tenant_id == "tenant-a"
    assert manager.created[0].source_id == "source-a"
    assert manager.created[0].scope_id == encode_scope_id(
        "tenant-a",
        "source-a",
    )


def test_replace_job_does_not_start_batch_dispatch_from_meta(
    monkeypatch,
):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    existing_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-1"),
            "meta": {},
        },
    )
    manager = _Manager({"job-1": existing_job})
    client = _build_client(manager)
    scheduled = []

    async def _schedule(request, source_job, *, existing, reason):
        del request
        scheduled.append(
            {
                "job_id": source_job.id,
                "existing_id": existing.id if existing else None,
                "reason": reason,
            },
        )

    monkeypatch.setattr(
        api_module,
        "_schedule_dispatch_broadcast_children_processing_after_save",
        _schedule,
        raising=False,
    )

    response = client.put(
        "/cron/jobs/job-1",
        json={
            **_job_spec("job-1"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )

    assert response.status_code == 200
    assert scheduled == []
    assert "broadcast_dispatch_intents_enabled" not in manager.created[0].meta
    assert "broadcast_dispatch_intents_enabled" not in response.json()["meta"]


def test_replace_job_does_not_reschedule_already_enabled_batch_dispatch(
    monkeypatch,
):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    existing_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-1"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )
    manager = _Manager({"job-1": existing_job})
    client = _build_client(manager)
    scheduled = []

    monkeypatch.setattr(
        api_module,
        "_schedule_dispatch_broadcast_children_processing",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
        raising=False,
    )

    response = client.put(
        "/cron/jobs/job-1",
        json={
            **_job_spec("job-1"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )

    assert response.status_code == 200
    assert scheduled == []
    assert (
        response.json()["meta"]["broadcast_dispatch_intents_enabled"] is True
    )


def test_replace_job_does_not_rollback_batch_dispatch_from_meta(
    monkeypatch,
):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    existing_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-1"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )
    manager = _Manager({"job-1": existing_job})
    client = _build_client(manager)
    scheduled = []

    def _schedule(_app, source_job, **kwargs):
        scheduled.append(
            {
                "job_id": source_job.id,
                "reason": kwargs.get("reason"),
                "enable": kwargs.get("enable"),
            },
        )
        return True

    monkeypatch.setattr(
        api_module,
        "_schedule_dispatch_broadcast_children_processing",
        _schedule,
        raising=False,
    )

    response = client.put(
        "/cron/jobs/job-1",
        json={
            **_job_spec("job-1"),
            "meta": {},
        },
    )

    assert response.status_code == 200
    assert scheduled == []
    assert (
        response.json()["meta"]["broadcast_dispatch_intents_enabled"] is True
    )


def test_enable_batch_dispatch_endpoint_schedules_children(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    manager = _Manager({"job-source": source_job})
    client = _build_client(
        manager,
        multi_agent_manager=_MultiAgentManager({}),
    )
    scheduled = []

    def _schedule(_request, _store, snapshot, source_job, **kwargs):
        scheduled.append(
            {
                "task_id": snapshot.task_id,
                "job_id": source_job.id,
                "reason": kwargs.get("reason"),
                "enable": kwargs.get("enable"),
            },
        )

    monkeypatch.setattr(
        api_module,
        "_schedule_dispatch_mode_children_sync",
        _schedule,
        raising=False,
    )

    response = client.post(
        "/cron/jobs/job-source/batch-dispatch/enable",
        json={"offset_window_hours": 4},
    )

    assert response.status_code == 200
    assert (
        response.json()["meta"]["broadcast_dispatch_intents_enabled"] is True
    )
    current = client.get(
        "/cron/jobs/job-source/broadcast/tasks/current",
    ).json()["task"]
    blocked_mode_change = client.post(
        "/cron/jobs/job-source/batch-dispatch/disable",
    )
    blocked_broadcast = client.post(
        "/cron/jobs/job-source/broadcast",
        json={"target_tenant_ids": ["tenant-b"]},
    )
    assert manager.batch_enabled == [
        {
            "job_id": "job-source",
            "offset_window_hours": 4,
        },
    ]
    assert scheduled == [
        {
            "task_id": current["task_id"],
            "job_id": "job-source",
            "reason": "enable_batch_dispatch",
            "enable": True,
        },
    ]
    assert current["status"] == "running"
    assert blocked_mode_change.status_code == 409
    assert blocked_broadcast.status_code == 200
    assert blocked_broadcast.json()["reused"] is True
    assert blocked_broadcast.json()["task_id"] == current["task_id"]


def test_disable_batch_dispatch_endpoint_schedules_rollback(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )
    manager = _Manager({"job-source": source_job})
    client = _build_client(manager)
    scheduled = []

    def _schedule(_request, _store, snapshot, source_job, **kwargs):
        scheduled.append(
            {
                "task_id": snapshot.task_id,
                "job_id": source_job.id,
                "reason": kwargs.get("reason"),
                "enable": kwargs.get("enable"),
            },
        )

    monkeypatch.setattr(
        api_module,
        "_schedule_dispatch_mode_children_sync",
        _schedule,
        raising=False,
    )

    response = client.post("/cron/jobs/job-source/batch-dispatch/disable")

    assert response.status_code == 200
    assert "broadcast_dispatch_intents_enabled" not in response.json()["meta"]
    current = client.get(
        "/cron/jobs/job-source/broadcast/tasks/current",
    ).json()["task"]
    assert manager.batch_disabled == [{"job_id": "job-source"}]
    assert scheduled == [
        {
            "task_id": current["task_id"],
            "job_id": "job-source",
            "reason": "disable_batch_dispatch",
            "enable": False,
        },
    ]
    assert current["status"] == "running"


def test_enable_batch_dispatch_rejects_broadcast_child(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    child_job = CronJobSpec.model_validate(
        {
            **_job_spec("child-job"),
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {"broadcast_source_job_id": "job-source"},
        },
    )
    manager = _Manager({"child-job": child_job})
    client = _build_client(manager)

    response = client.post("/cron/jobs/child-job/batch-dispatch/enable")

    assert response.status_code == 400
    assert "broadcast child" in response.json()["detail"]
    assert manager.batch_enabled == []


def test_create_job_persists_model_slot():
    manager = _Manager()
    client = _build_client(manager)
    _install_provider_manager(
        {
            "openai": _Provider(["gpt-5.4"]),
        },
    )

    response = client.post(
        "/cron/jobs",
        json=_job_spec(model_slot=_model_slot()),
    )

    assert response.status_code == 200
    assert manager.created[0].model_slot is not None
    assert manager.created[0].model_slot.provider_id == "openai"
    assert manager.created[0].model_slot.model == "gpt-5.4"
    assert response.json()["model_slot"] == _model_slot()


def test_create_job_rejects_unknown_model_slot_provider():
    manager = _Manager()
    client = _build_client(manager)
    _install_provider_manager(
        {
            "openai": _Provider(["gpt-5.4"]),
        },
    )

    response = client.post(
        "/cron/jobs",
        json=_job_spec(
            model_slot=_model_slot(provider_id="missing-provider"),
        ),
    )

    assert response.status_code == 404
    assert (
        response.json()["detail"] == "Provider 'missing-provider' not found."
    )
    assert manager.created == []


def test_replace_job_rejects_unknown_model_slot_model():
    manager = _Manager()
    client = _build_client(manager)
    _install_provider_manager(
        {
            "openai": _Provider(["gpt-5.4"]),
        },
    )

    response = client.put(
        "/cron/jobs/job-1",
        json=_job_spec(
            "job-1",
            model_slot=_model_slot(model="gpt-4.1"),
        ),
    )

    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "Model 'gpt-4.1' not found in provider 'openai'."
    )
    assert manager.created == []


def test_create_text_job_clears_model_slot():
    manager = _Manager()
    client = _build_client(manager)
    _install_provider_manager(
        {
            "openai": _Provider(["gpt-5.4"]),
        },
    )

    response = client.post(
        "/cron/jobs",
        json=_job_spec(
            task_type="text",
            model_slot=_model_slot(),
        ),
    )

    assert response.status_code == 200
    assert manager.created[0].task_type == "text"
    assert manager.created[0].model_slot is None
    assert response.json().get("model_slot") is None


def test_run_job_forwards_b3_headers():
    job = CronJobSpec.model_validate(_job_spec("job-1"))
    manager = _Manager({"job-1": job})
    client = _build_client(manager)

    response = client.post(
        "/cron/jobs/job-1/run",
        headers={
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "X-B3-Spanid": "32befd146889a61a",
            "X-B3-BusinessId": "LQ1303LMES-WEB",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"started": True}
    assert manager.ran == ["job-1"]
    assert manager.run_kwargs == [
        {
            "dispatch_meta": {
                "passthrough_headers": {
                    "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
                    "X-B3-Spanid": "32befd146889a61a",
                    "X-B3-BusinessId": "LQ1303LMES-WEB",
                },
                "b3_trace_id": "8267fd70bacf497704fec30eaa353979",
            },
        },
    ]


def test_cron_broadcast_concurrency_uses_env_with_default(monkeypatch):
    monkeypatch.delenv(
        api_module.CRON_BROADCAST_CONCURRENCY_ENV,
        raising=False,
    )
    assert api_module._get_cron_broadcast_concurrency() == 4

    monkeypatch.setenv(api_module.CRON_BROADCAST_CONCURRENCY_ENV, "2")
    assert api_module._get_cron_broadcast_concurrency() == 2

    monkeypatch.setenv(api_module.CRON_BROADCAST_CONCURRENCY_ENV, "0")
    assert api_module._get_cron_broadcast_concurrency() == 4


def test_broadcast_to_tenants_limits_concurrency(monkeypatch):
    monkeypatch.setenv(api_module.CRON_BROADCAST_CONCURRENCY_ENV, "2")
    active = 0
    max_active = 0

    async def _fake_broadcast_to_tenant(_context, tenant_id, offset):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=True,
            offset_minutes=offset,
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    context = types.SimpleNamespace(offsets=[0, 1, 2, 3, 4])

    results = asyncio.run(
        api_module._broadcast_to_tenants(
            context,
            ["tenant-a", "tenant-b", "tenant-c", "tenant-d", "tenant-e"],
        ),
    )

    assert max_active == 2
    assert [item.tenant_id for item in results] == [
        "tenant-a",
        "tenant-b",
        "tenant-c",
        "tenant-d",
        "tenant-e",
    ]
    assert [item.offset_minutes for item in results] == [0, 1, 2, 3, 4]


def test_broadcast_job_returns_running_task_and_polling_result(monkeypatch):
    async def _fake_broadcast_to_tenant(_context, tenant_id, offset):
        await asyncio.sleep(0.02)
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=True,
            job_id=f"job-{tenant_id}",
            cron="0 9 * * *",
            timezone="UTC",
            offset_minutes=offset,
            notification_timezone="UTC",
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    with _build_client(
        _Manager({"job-source": source_job}),
        multi_agent_manager=_MultiAgentManager({}),
    ) as client:
        response = client.post(
            "/cron/jobs/job-source/broadcast",
            json={"target_tenant_ids": ["tenant-b", "tenant-c"]},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["task_id"]
        assert payload["status"] == "running"
        assert payload["tenant_count"] == 2
        assert payload["completed_count"] == 0
        assert payload["failed_count"] == 0
        assert payload["results"] == []
        assert payload["reused"] is False

        finished = _wait_for_broadcast_task(
            client,
            "job-source",
            payload["task_id"],
        )
        assert finished["status"] == "completed"
        assert finished["completed_count"] == 2
        assert finished["failed_count"] == 0
        assert [item["tenant_id"] for item in finished["results"]] == [
            "tenant-b",
            "tenant-c",
        ]


def test_broadcast_job_uses_db_store_when_state_store_missing(monkeypatch):
    """广播任务缺少预置 store 时也应使用 app 上的数据库连接落任务表。"""

    async def _fake_broadcast_to_tenant(_context, tenant_id, offset):
        await asyncio.sleep(0.01)
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=True,
            job_id=f"job-{tenant_id}",
            cron="0 9 * * *",
            timezone="UTC",
            offset_minutes=offset,
            notification_timezone="UTC",
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    db = _AsyncTaskDb()
    db.is_connected = False

    async def _fake_get_async_task_db(request):
        request.app.state.db_connection = db
        return db

    monkeypatch.setattr(
        api_module,
        "get_or_create_async_task_db",
        _fake_get_async_task_db,
    )
    with _build_client(
        _Manager({"job-source": source_job}),
        multi_agent_manager=_MultiAgentManager({}),
    ) as client:
        response = client.post(
            "/cron/jobs/job-source/broadcast",
            json={"target_tenant_ids": ["tenant-b"]},
        )

        assert response.status_code == 200
        assert client.app.state.db_connection is db
        assert any(
            "INSERT INTO swe_async_tasks" in query
            for query, _params in db.executed
        )
        assert any(
            "INSERT INTO swe_async_task_items" in query
            for query, _params in db.executed_many
        )


def test_broadcast_applies_batch_dispatch_after_distribution(
    monkeypatch,
):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    observed_parent_flags = []
    observed_context_flags = []

    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return ["tenant-b", "tenant-c", "tenant-unrelated"]

    async def _fake_broadcast_to_tenant(context, tenant_id, offset):
        del offset
        observed_parent_flags.append(
            bool(
                (context.source_job.meta or {}).get(
                    "broadcast_dispatch_intents_enabled",
                ),
            ),
        )
        observed_context_flags.append(bool(context.enable_batch_dispatch))
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=True,
            job_id=f"job-{tenant_id}",
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {},
        },
    )
    manager = _Manager({"job-source": source_job})
    target_manager = _Manager()
    existing_child = CronJobSpec.model_validate(
        {
            **_job_spec("child-job-c"),
            "tenant_id": "tenant-c",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-c", "source-a"),
            "meta": {"broadcast_source_job_id": "job-source"},
        },
    )
    existing_child_manager = _Manager({"child-job-c": existing_child})
    with _build_client(
        manager,
        multi_agent_manager=_MultiAgentManager(
            {
                encode_scope_id("tenant-b", "source-a"): _Workspace(
                    target_manager,
                ),
                encode_scope_id("tenant-c", "source-a"): _Workspace(
                    existing_child_manager,
                ),
            },
        ),
    ) as client:
        response = client.post(
            "/cron/jobs/job-source/broadcast",
            json={
                "target_tenant_ids": ["tenant-b"],
                "enable_batch_dispatch": True,
            },
        )

        assert response.status_code == 200
        finished = _wait_for_broadcast_task(
            client,
            "job-source",
            response.json()["task_id"],
        )

    assert finished["status"] == "completed"
    assert (
        manager.jobs_by_id["job-source"].meta[
            "broadcast_dispatch_intents_enabled"
        ]
        is True
    )
    assert manager.batch_enabled == [
        {
            "job_id": "job-source",
            "offset_window_hours": 4,
        },
    ]
    assert observed_parent_flags == [False]
    assert observed_context_flags == [False]
    assert (
        existing_child_manager.jobs_by_id["child-job-c"].meta[
            "broadcast_dispatch_intents_enabled"
        ]
        is True
    )


def test_broadcast_applies_normal_dispatch_after_distribution(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    observed_parent_flags = []
    observed_context_flags = []

    async def _fake_broadcast_to_tenant(context, tenant_id, offset):
        del offset
        observed_parent_flags.append(
            bool(
                (context.source_job.meta or {}).get(
                    "broadcast_dispatch_intents_enabled",
                ),
            ),
        )
        observed_context_flags.append(bool(context.enable_batch_dispatch))
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=True,
            job_id=f"job-{tenant_id}",
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )
    manager = _Manager({"job-source": source_job})
    target_manager = _Manager()
    with _build_client(
        manager,
        multi_agent_manager=_MultiAgentManager(
            {
                encode_scope_id("tenant-b", "source-a"): _Workspace(
                    target_manager,
                ),
            },
        ),
    ) as client:
        response = client.post(
            "/cron/jobs/job-source/broadcast",
            json={
                "target_tenant_ids": ["tenant-b"],
                "enable_batch_dispatch": False,
            },
        )

        assert response.status_code == 200
        finished = _wait_for_broadcast_task(
            client,
            "job-source",
            response.json()["task_id"],
        )

    assert finished["status"] == "completed"
    assert (
        "broadcast_dispatch_intents_enabled"
        not in manager.jobs_by_id["job-source"].meta
    )
    assert manager.batch_disabled == [{"job_id": "job-source"}]
    assert observed_parent_flags == [True]
    assert observed_context_flags == [True]


@pytest.mark.parametrize("batch_dispatch_enabled", [False, True])
def test_broadcast_does_not_resync_unchanged_dispatch_mode(
    monkeypatch,
    batch_dispatch_enabled,
):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")

    async def _fake_broadcast_to_tenant(_context, tenant_id, _offset):
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=True,
            job_id=f"job-{tenant_id}",
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    source_meta = (
        {"broadcast_dispatch_intents_enabled": True}
        if batch_dispatch_enabled
        else {}
    )
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": source_meta,
        },
    )
    manager = _Manager({"job-source": source_job})
    with _build_client(
        manager,
        multi_agent_manager=_MultiAgentManager({}),
    ) as client:
        response = client.post(
            "/cron/jobs/job-source/broadcast",
            json={
                "target_tenant_ids": ["tenant-b"],
                "enable_batch_dispatch": batch_dispatch_enabled,
            },
        )
        finished = _wait_for_broadcast_task(
            client,
            "job-source",
            response.json()["task_id"],
        )

    assert finished["status"] == "completed"
    assert manager.batch_enabled == []
    assert manager.batch_disabled == []


def test_broadcast_job_polling_reports_failed_targets(monkeypatch):
    async def _fake_broadcast_to_tenant(_context, tenant_id, offset):
        del offset
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=False,
            error="boom",
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    with _build_client(
        _Manager({"job-source": source_job}),
        multi_agent_manager=_MultiAgentManager({}),
    ) as client:
        response = client.post(
            "/cron/jobs/job-source/broadcast",
            json={"target_tenant_ids": ["tenant-b"]},
        )

        assert response.status_code == 200
        finished = _wait_for_broadcast_task(
            client,
            "job-source",
            response.json()["task_id"],
        )
        assert finished["status"] == "failed"
        assert finished["completed_count"] == 1
        assert finished["failed_count"] == 1
        assert finished["results"][0]["error"] == "boom"


def test_broadcast_job_reuses_running_task(monkeypatch):
    async def _fake_broadcast_to_tenant(_context, tenant_id, offset):
        await asyncio.sleep(0.05)
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=True,
            offset_minutes=offset,
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    with _build_client(
        _Manager({"job-source": source_job}),
        multi_agent_manager=_MultiAgentManager({}),
    ) as client:
        first = client.post(
            "/cron/jobs/job-source/broadcast",
            json={"target_tenant_ids": ["tenant-b"]},
        )
        second = client.post(
            "/cron/jobs/job-source/broadcast",
            json={"target_tenant_ids": ["tenant-c"]},
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["task_id"] == first.json()["task_id"]
        assert second.json()["reused"] is True

        finished = _wait_for_broadcast_task(
            client,
            "job-source",
            first.json()["task_id"],
        )
        assert finished["status"] == "completed"


def test_current_broadcast_task_returns_running_task(monkeypatch):
    async def _fake_broadcast_to_tenant(_context, tenant_id, offset):
        await asyncio.sleep(0.05)
        return api_module.CronBroadcastTenantResult(
            tenant_id=tenant_id,
            success=True,
            offset_minutes=offset,
        )

    monkeypatch.setattr(
        api_module,
        "_broadcast_to_tenant",
        _fake_broadcast_to_tenant,
    )
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    with _build_client(
        _Manager({"job-source": source_job}),
        multi_agent_manager=_MultiAgentManager({}),
    ) as client:
        empty = client.get("/cron/jobs/job-source/broadcast/tasks/current")
        response = client.post(
            "/cron/jobs/job-source/broadcast",
            json={"target_tenant_ids": ["tenant-b"]},
        )
        current = client.get("/cron/jobs/job-source/broadcast/tasks/current")

        assert empty.status_code == 200
        assert empty.json() == {"task": None}
        assert response.status_code == 200
        assert current.status_code == 200
        assert current.json()["task"]["task_id"] == response.json()["task_id"]

        _wait_for_broadcast_task(
            client,
            "job-source",
            response.json()["task_id"],
        )


def test_list_broadcast_children_for_tenants_limits_concurrency(monkeypatch):
    monkeypatch.setenv(api_module.CRON_BROADCAST_CONCURRENCY_ENV, "2")
    active = 0
    max_active = 0

    async def _fake_list_children(_context, tenant_id):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return [tenant_id]

    monkeypatch.setattr(
        api_module,
        "_list_broadcast_children_for_tenant",
        _fake_list_children,
    )

    results = asyncio.run(
        api_module._list_broadcast_children_for_tenants(
            types.SimpleNamespace(),
            ["tenant-a", "tenant-b", "tenant-c", "tenant-d", "tenant-e"],
        ),
    )

    assert max_active == 2
    assert results == [
        "tenant-a",
        "tenant-b",
        "tenant-c",
        "tenant-d",
        "tenant-e",
    ]


def test_broadcast_clears_model_slot_and_returns_warning_for_unsupported_tenant():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec(
                "job-source",
                model_slot=_model_slot(),
            ),
            "schedule": ScheduleSpec(
                cron="0 9 * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_supported = _Manager()
    target_missing = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-b", "source-a"): _Workspace(
                target_supported,
            ),
            encode_scope_id("tenant-c", "source-a"): _Workspace(
                target_missing,
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=_TenantWorkspacePool(),
    )
    _install_provider_manager(
        {},
        providers_by_tenant={
            encode_scope_id("tenant-b", "source-a"): {
                "openai": _Provider(["gpt-5.4"]),
            },
            encode_scope_id("tenant-c", "source-a"): {
                "anthropic": _Provider(["claude-3-7-sonnet"]),
            },
        },
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={"target_tenant_ids": ["tenant-b", "tenant-c"]},
    )

    assert response.status_code == 200
    assert target_supported.created[0].model_slot is not None
    assert target_supported.created[0].model_slot.provider_id == "openai"
    assert target_supported.created[0].model_slot.model == "gpt-5.4"
    assert target_missing.created[0].model_slot is None
    assert target_missing.created[0].meta["broadcast_original_model_slot"] == {
        "provider_id": "openai",
        "model": "gpt-5.4",
    }
    assert (
        target_missing.created[0].meta["broadcast_model_slot_fallback_reason"]
        == "provider_not_found"
    )
    payload = _wait_for_broadcast_task(
        client,
        "job-source",
        response.json()["task_id"],
    )
    assert payload["results"] == [
        {
            "tenant_id": "tenant-b",
            "success": True,
            "job_id": target_supported.created[0].id,
            "cron": target_supported.created[0].schedule.cron,
            "timezone": target_supported.created[0].schedule.timezone,
            "offset_minutes": 0,
            "notification_timezone": "UTC",
            "error": "",
            "warning": "",
        },
        {
            "tenant_id": "tenant-c",
            "success": True,
            "job_id": target_missing.created[0].id,
            "cron": target_missing.created[0].schedule.cron,
            "timezone": target_missing.created[0].schedule.timezone,
            "offset_minutes": 240,
            "notification_timezone": "UTC",
            "error": "",
            "warning": (
                "model_slot not copied: provider/model unavailable in "
                "target tenant"
            ),
        },
    ]


def test_broadcast_uses_configured_offset_window_hours():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "schedule": ScheduleSpec(
                cron="0 9 * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_first = _Manager()
    target_second = _Manager()
    target_third = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-b", "source-a"): _Workspace(
                target_first,
            ),
            encode_scope_id("tenant-c", "source-a"): _Workspace(
                target_second,
            ),
            encode_scope_id("tenant-d", "source-a"): _Workspace(
                target_third,
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=_TenantWorkspacePool(),
    )
    _install_provider_manager(
        {},
        providers_by_tenant={
            encode_scope_id("tenant-b", "source-a"): {},
            encode_scope_id("tenant-c", "source-a"): {},
            encode_scope_id("tenant-d", "source-a"): {},
        },
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={
            "target_tenant_ids": ["tenant-b", "tenant-c", "tenant-d"],
            "offset_window_hours": 1,
        },
    )

    assert response.status_code == 200
    payload = _wait_for_broadcast_task(
        client,
        "job-source",
        response.json()["task_id"],
    )
    assert [item["offset_minutes"] for item in payload["results"]] == [
        0,
        30,
        60,
    ]
    assert target_first.created[0].schedule.cron == "0 9 * * *"
    assert target_second.created[0].schedule.cron == "30 8 * * *"
    assert target_third.created[0].schedule.cron == "0 8 * * *"


def test_broadcast_can_disable_offset_shift():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "schedule": ScheduleSpec(
                cron="*/15 * * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_first = _Manager()
    target_second = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-b", "source-a"): _Workspace(
                target_first,
            ),
            encode_scope_id("tenant-c", "source-a"): _Workspace(
                target_second,
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=_TenantWorkspacePool(),
    )
    _install_provider_manager(
        {},
        providers_by_tenant={
            encode_scope_id("tenant-b", "source-a"): {},
            encode_scope_id("tenant-c", "source-a"): {},
        },
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={
            "target_tenant_ids": ["tenant-b", "tenant-c"],
            "enable_offset": False,
            "offset_window_hours": 24,
        },
    )

    assert response.status_code == 200
    payload = _wait_for_broadcast_task(
        client,
        "job-source",
        response.json()["task_id"],
    )
    assert [item["offset_minutes"] for item in payload["results"]] == [
        0,
        0,
    ]
    assert [item["warning"] for item in payload["results"]] == [
        "",
        "",
    ]
    assert target_first.created[0].schedule.cron == "*/15 * * * *"
    assert target_second.created[0].schedule.cron == "*/15 * * * *"
    assert target_first.created[0].meta["broadcast_offset_minutes"] == 0
    assert target_second.created[0].meta["broadcast_offset_minutes"] == 0


@pytest.mark.parametrize("offset_window_hours", [-1, 25])
def test_broadcast_rejects_invalid_offset_window_hours(offset_window_hours):
    source_manager = _Manager(
        {
            "job-source": CronJobSpec.model_validate(
                {
                    **_job_spec("job-source"),
                    "tenant_id": "tenant-a",
                    "source_id": "source-a",
                    "scope_id": encode_scope_id("tenant-a", "source-a"),
                },
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=_MultiAgentManager({}),
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={
            "target_tenant_ids": ["tenant-b"],
            "offset_window_hours": offset_window_hours,
        },
    )

    assert response.status_code == 422


def test_broadcast_persists_model_not_found_reason_for_unsupported_model():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec(
                "job-source",
                model_slot=_model_slot(),
            ),
            "schedule": ScheduleSpec(
                cron="0 9 * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_missing = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-c", "source-a"): _Workspace(
                target_missing,
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=_TenantWorkspacePool(),
    )
    _install_provider_manager(
        {},
        providers_by_tenant={
            encode_scope_id("tenant-c", "source-a"): {
                "openai": _Provider(["gpt-4.1"]),
            },
        },
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={"target_tenant_ids": ["tenant-c"]},
    )

    assert response.status_code == 200
    assert target_missing.created[0].model_slot is None
    assert (
        target_missing.created[0].meta["broadcast_model_slot_fallback_reason"]
        == "model_not_found"
    )


def test_broadcast_uses_original_cron_when_offset_shift_is_unsupported():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "schedule": ScheduleSpec(
                cron="30 1 1 * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_first = _Manager()
    target_fallback = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-b", "source-a"): _Workspace(
                target_first,
            ),
            encode_scope_id("tenant-c", "source-a"): _Workspace(
                target_fallback,
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=_TenantWorkspacePool(),
    )
    _install_provider_manager(
        {},
        providers_by_tenant={
            encode_scope_id("tenant-b", "source-a"): {},
            encode_scope_id("tenant-c", "source-a"): {},
        },
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={"target_tenant_ids": ["tenant-b", "tenant-c"]},
    )

    assert response.status_code == 200
    assert target_first.created[0].schedule.cron == "30 1 1 * *"
    assert target_first.created[0].meta["broadcast_offset_minutes"] == 0
    assert target_fallback.created[0].schedule.cron == "30 1 1 * *"
    assert target_fallback.created[0].meta["broadcast_offset_minutes"] == 0
    payload = _wait_for_broadcast_task(
        client,
        "job-source",
        response.json()["task_id"],
    )
    fallback_result = payload["results"][1]
    assert fallback_result["success"] is True
    assert fallback_result["cron"] == "30 1 1 * *"
    assert fallback_result["offset_minutes"] == 0
    assert fallback_result["warning"] == (
        "cron offset not applied: unsupported cron, using original schedule"
    )


def test_broadcast_child_inherits_notification_delay_minutes():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "schedule": ScheduleSpec(
                cron="0 9 * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {"notification_delay_minutes": 120},
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_manager = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-b", "source-a"): _Workspace(
                target_manager,
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=_TenantWorkspacePool(),
    )
    _install_provider_manager(
        {},
        providers_by_tenant={encode_scope_id("tenant-b", "source-a"): {}},
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={"target_tenant_ids": ["tenant-b"]},
    )

    assert response.status_code == 200
    assert target_manager.created[0].meta["notification_delay_minutes"] == 120


def test_broadcast_to_default_user_uses_source_template_without_scope():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "schedule": ScheduleSpec(
                cron="0 9 * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_manager = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            "default_source-a": _Workspace(target_manager),
        },
    )
    tenant_pool = _TenantWorkspacePool()
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=tenant_pool,
    )
    _install_provider_manager(
        {},
        providers_by_tenant={"default_source-a": {}},
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={"target_tenant_ids": ["default"]},
    )

    assert response.status_code == 200
    assert tenant_pool.calls == [
        {
            "tenant_id": "default",
            "source_id": "source-a",
            "tenant_name": None,
            "bbk_id": None,
        },
    ]
    assert target_manager.created[0].tenant_id == "default"
    assert target_manager.created[0].source_id == "source-a"
    assert target_manager.created[0].scope_id is None


def test_broadcast_clears_stale_fallback_meta_when_source_model_slot_missing():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "schedule": ScheduleSpec(
                cron="0 9 * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {
                "broadcast_original_model_slot": {
                    "provider_id": "openai",
                    "model": "gpt-5.4",
                },
                "broadcast_model_slot_fallback_reason": ("provider_not_found"),
            },
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_supported = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-b", "source-a"): _Workspace(
                target_supported,
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=_TenantWorkspacePool(),
    )
    _install_provider_manager(
        {},
        providers_by_tenant={
            encode_scope_id("tenant-b", "source-a"): {
                "openai": _Provider(["gpt-5.4"]),
            },
        },
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={"target_tenant_ids": ["tenant-b"]},
    )

    assert response.status_code == 200
    assert (
        "broadcast_original_model_slot" not in target_supported.created[0].meta
    )
    assert (
        "broadcast_model_slot_fallback_reason"
        not in target_supported.created[0].meta
    )


def test_broadcast_job_persists_target_identity_from_request():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "schedule": ScheduleSpec(
                cron="0 9 * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "tenant_name": "Alice",
            "bbk_id": "1001",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_manager = _Manager()
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-b", "source-a"): _Workspace(
                target_manager,
            ),
        },
    )

    tenant_workspace_pool = _TenantWorkspacePool()
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=tenant_workspace_pool,
    )
    _install_provider_manager(
        {},
        providers_by_tenant={
            encode_scope_id("tenant-b", "source-a"): {},
        },
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={
            "target_tenant_ids": ["tenant-b"],
            "targets": [
                {
                    "tenant_id": "tenant-b",
                    "tenant_name": "Bob",
                    "bbk_id": "2002",
                },
            ],
        },
    )

    assert response.status_code == 200
    assert target_manager.created[0].tenant_id == "tenant-b"
    assert target_manager.created[0].tenant_name == "Bob"
    assert target_manager.created[0].bbk_id == "2002"
    assert tenant_workspace_pool.calls == [
        {
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "tenant_name": "Bob",
            "bbk_id": "2002",
        },
    ]


def test_cron_provider_manager_respects_explicit_target_scope(
    monkeypatch,
    tmp_path: Path,
):
    api_module.ProviderManager = ProviderManager
    secret_dir = tmp_path / "secret"
    monkeypatch.setattr(
        "swe.providers.provider_manager.SECRET_DIR",
        secret_dir,
    )
    ProviderManager.reset_instance_cache()

    source_scope_id = encode_scope_id("tenant-a", "source-a")
    target_scope_id = encode_scope_id("tenant-b", "source-b")

    with tenant_context(
        tenant_id="tenant-a",
        source_id="source-a",
        scope_id=source_scope_id,
    ):
        manager = api_module._get_provider_manager(target_scope_id)

    assert manager.tenant_id == target_scope_id
    assert (secret_dir / target_scope_id / "providers").exists()
    assert not (secret_dir / source_scope_id / "providers").exists()


def test_broadcast_updates_existing_child_job_definition():
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "schedule": ScheduleSpec(
                cron="30 10 * * *",
            ).model_dump(mode="json"),
            "tenant_id": "tenant-a",
            "tenant_name": "Alice",
            "bbk_id": "1001",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "request": CronJobRequest(
                input=[{"content": [{"type": "text", "text": "new task"}]}],
            ).model_dump(mode="json"),
            "runtime": JobRuntimeSpec(
                timeout_seconds=456,
            ).model_dump(mode="json"),
            "meta": {"notification_delay_minutes": 60},
        },
    )
    existing_child_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-existing-child"),
            "schedule": ScheduleSpec(
                cron="0 9 * * *",
            ).model_dump(mode="json"),
            "enabled": False,
            "tenant_id": "tenant-b",
            "tenant_name": "Bob",
            "bbk_id": "2002",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "request": CronJobRequest(
                input=[{"content": [{"type": "text", "text": "old task"}]}],
                user_id="tenant-b",
                session_id="cron-task:job-existing-child",
            ).model_dump(mode="json"),
            "dispatch": DispatchSpec(
                channel="console",
                target=DispatchTarget(
                    user_id="tenant-b",
                    session_id="cron-task:job-existing-child",
                ),
                meta={"target-owned": True},
            ).model_dump(mode="json"),
            "meta": {
                "broadcast_source_job_id": "job-source",
                "broadcast_offset_minutes": 0,
                "task_chat_id": "chat-child",
                "task_session_id": "session-child",
                "pause_reason": "manual",
            },
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_manager = _Manager({"job-existing-child": existing_child_job})
    multi_agent_manager = _MultiAgentManager(
        {
            encode_scope_id("tenant-b", "source-a"): _Workspace(
                target_manager,
            ),
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=multi_agent_manager,
        tenant_workspace_pool=_TenantWorkspacePool(),
    )
    _install_provider_manager(
        {},
        providers_by_tenant={
            encode_scope_id("tenant-b", "source-a"): {},
        },
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast",
        json={"target_tenant_ids": ["tenant-b"]},
    )

    assert response.status_code == 200
    assert len(target_manager.created) == 1
    updated = target_manager.created[0]
    assert updated.id == "job-existing-child"
    assert updated.enabled is False
    assert updated.tenant_id == "tenant-b"
    assert updated.tenant_name == "Bob"
    assert updated.bbk_id == "2002"
    assert updated.dispatch.target.user_id == "tenant-b"
    assert updated.dispatch.target.session_id == "cron-task:job-existing-child"
    assert updated.request is not None
    assert updated.request.user_id == "tenant-b"
    assert updated.request.session_id == "cron-task:job-existing-child"
    assert updated.request.input == [
        {"content": [{"type": "text", "text": "new task"}]},
    ]
    assert updated.schedule.cron == "30 10 * * *"
    assert updated.runtime.timeout_seconds == 456
    assert updated.meta["notification_delay_minutes"] == 60
    assert updated.meta["broadcast_source_job_id"] == "job-source"
    assert updated.meta["broadcast_source_job_name"] == "tenant cron"
    assert updated.meta["broadcast_source_tenant_id"] == "tenant-a"
    assert updated.meta["broadcast_source_tenant_name"] == "Alice"
    assert updated.meta["broadcast_source_bbk_id"] == "1001"
    assert updated.meta["task_chat_id"] == "chat-child"
    assert updated.meta["task_session_id"] == "session-child"
    assert updated.meta["pause_reason"] == "manual"
    payload = _wait_for_broadcast_task(
        client,
        "job-source",
        response.json()["task_id"],
    )
    assert payload["results"] == [
        {
            "tenant_id": "tenant-b",
            "success": True,
            "job_id": "job-existing-child",
            "cron": "30 10 * * *",
            "timezone": "UTC",
            "offset_minutes": 0,
            "notification_timezone": "UTC",
            "error": "",
            "warning": "",
        },
    ]


def test_list_broadcast_children_returns_empty_for_undistributed_job(
    monkeypatch,
):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return ["tenant-b"]

    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_manager = _Manager()
    client = _build_client(
        source_manager,
        multi_agent_manager=_MultiAgentManager(
            {
                encode_scope_id("tenant-b", "source-a"): _Workspace(
                    target_manager,
                ),
            },
        ),
        tenant_workspace_pool=_TenantWorkspacePool(),
    )

    response = client.get("/cron/jobs/job-source/broadcast/children")

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "status": "idle",
        "tenant_count": 0,
        "failed_tenants": 0,
        "failure_summary": None,
        "updated_at": None,
    }


def test_list_broadcast_children_returns_matching_target_jobs(monkeypatch):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return ["tenant-b", "tenant-c"]

    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    matching_child = CronJobSpec.model_validate(
        {
            **_job_spec("child-b"),
            "enabled": True,
            "tenant_id": "tenant-b",
            "tenant_name": "Bob",
            "bbk_id": "2002",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {
                "broadcast_source_job_id": "job-source",
                "broadcast_offset_minutes": 5,
            },
        },
    )
    other_child = CronJobSpec.model_validate(
        {
            **_job_spec("child-c"),
            "tenant_id": "tenant-c",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-c", "source-a"),
            "meta": {"broadcast_source_job_id": "other-source"},
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_b = _Manager({"child-b": matching_child})
    target_c = _Manager({"child-c": other_child})
    client = _build_client(
        source_manager,
        multi_agent_manager=_MultiAgentManager(
            {
                encode_scope_id("tenant-b", "source-a"): _Workspace(
                    target_b,
                ),
                encode_scope_id("tenant-c", "source-a"): _Workspace(
                    target_c,
                ),
            },
        ),
        tenant_workspace_pool=_TenantWorkspacePool(),
    )

    response = client.get("/cron/jobs/job-source/broadcast/children")

    assert response.status_code == 200
    assert response.json()["status"] == "idle"

    refresh_response = client.post(
        "/cron/jobs/job-source/broadcast/children/refresh",
    )

    assert refresh_response.status_code == 200
    assert refresh_response.json()["status"] == "running"
    assert refresh_response.json()["reused"] is False

    payload = _wait_for_broadcast_children_refresh(client, "job-source")
    assert payload["status"] == "completed"
    assert payload["tenant_count"] == 2
    assert payload["failed_tenants"] == 0
    assert payload["updated_at"]
    assert payload["items"] == [
        {
            "tenant_id": "tenant-b",
            "tenant_name": "Bob",
            "bbk_id": "2002",
            "job_id": "child-b",
            "job_name": "tenant cron",
            "enabled": True,
            "cron": "* * * * *",
            "timezone": "UTC",
            "offset_minutes": 5,
            "last_status": None,
            "last_run_at": None,
            "last_error": None,
        },
    ]


def _wait_for_broadcast_children_refresh(client: TestClient, job_id: str):
    for _ in range(50):
        response = client.get(f"/cron/jobs/{job_id}/broadcast/children")
        payload = response.json()
        if payload["status"] != "running":
            return payload
        time.sleep(0.01)
    return payload


def _wait_for_broadcast_task(
    client: TestClient,
    job_id: str,
    task_id: str,
):
    for _ in range(50):
        response = client.get(
            f"/cron/jobs/{job_id}/broadcast/tasks/{task_id}",
        )
        payload = response.json()
        if payload["status"] != "running":
            return payload
        time.sleep(0.01)
    return payload


def test_schedule_broadcast_children_refresh_reuses_running_task(monkeypatch):
    calls = []

    async def _refresh_snapshot(store, parts, context, tenant_ids):
        del store, context, tenant_ids
        calls.append(parts["job_id"])
        await asyncio.sleep(0.01)

    monkeypatch.setattr(
        api_module,
        "_refresh_broadcast_children_snapshot",
        _refresh_snapshot,
    )
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )

    async def _run():
        request = types.SimpleNamespace(
            app=types.SimpleNamespace(state=types.SimpleNamespace()),
            state=types.SimpleNamespace(
                agent_id="default",
                source_id="source-a",
                tenant_id="tenant-a",
            ),
        )
        context = types.SimpleNamespace(source_job=source_job)
        first = await api_module._schedule_broadcast_children_refresh(
            request,
            source_job,
            context,
            ["tenant-b"],
        )
        second = await api_module._schedule_broadcast_children_refresh(
            request,
            source_job,
            context,
            ["tenant-b"],
        )
        await asyncio.gather(
            *api_module._get_broadcast_children_tasks(request).values(),
        )
        return first, second

    first, second = asyncio.run(_run())

    assert first[1] is False
    assert second[1] is True
    assert calls == ["job-source"]


def test_dispatch_child_processing_records_failed_snapshot_on_target_error(
    monkeypatch,
):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return ["tenant-b"]

    class _Store:
        def __init__(self):
            self.failed = []
            self.completed = []

        async def mark_running(self, **kwargs):
            del kwargs
            return True

        async def record_failed(self, **kwargs):
            self.failed.append(kwargs)

        async def record_completed(self, **kwargs):
            self.completed.append(kwargs)

    class _FailingMultiAgentManager:
        async def get_agent(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("workspace unavailable")

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )
    store = _Store()
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            cron_broadcast_children_store=store,
            multi_agent_manager=_FailingMultiAgentManager(),
            tenant_workspace_pool=None,
        ),
    )

    synchronized = asyncio.run(
        api_module._process_dispatch_broadcast_children(
            app,
            source_job,
            agent_id="default",
            source_id="source-a",
            reason="test",
            tenant_ids=["tenant-b"],
        ),
    )

    assert synchronized is False
    assert store.completed == []
    assert store.failed
    assert store.failed[0]["tenant_count"] == 1


def test_dispatch_child_processing_skips_unavailable_discovery_tenant(
    monkeypatch,
):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return ["tenant-b", "tenant-unrelated"]

    class _Store:
        def __init__(self):
            self.failed = []
            self.completed = []

        async def mark_running(self, **kwargs):
            del kwargs
            return True

        async def record_failed(self, **kwargs):
            self.failed.append(kwargs)

        async def record_completed(self, **kwargs):
            self.completed.append(kwargs)

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )
    child = CronJobSpec.model_validate(
        {
            **_job_spec("child-job"),
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {"broadcast_source_job_id": "job-source"},
        },
    )
    child_manager = _Manager({"child-job": child})
    store = _Store()
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            cron_broadcast_children_store=store,
            multi_agent_manager=_MultiAgentManager(
                {
                    encode_scope_id("tenant-b", "source-a"): _Workspace(
                        child_manager,
                    ),
                },
            ),
            tenant_workspace_pool=None,
        ),
    )

    synchronized = asyncio.run(
        api_module._process_dispatch_broadcast_children(
            app,
            source_job,
            agent_id="default",
            source_id="source-a",
            reason="test",
        ),
    )

    assert synchronized is True
    assert store.failed == []
    assert store.completed[0]["failed_tenants"] == 1
    assert "unavailable" in store.completed[0]["failure_summary"]
    assert (
        child_manager.jobs_by_id["child-job"].meta[
            "broadcast_dispatch_intents_enabled"
        ]
        is True
    )


def test_dispatch_child_processing_fails_for_unavailable_known_child(
    monkeypatch,
):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return []

    class _Store:
        def __init__(self):
            self.failed = []
            self.completed = []

        async def get_snapshot(self, **kwargs):
            del kwargs
            return types.SimpleNamespace(items=[{"tenant_id": "tenant-b"}])

        async def mark_running(self, **kwargs):
            del kwargs
            return True

        async def record_failed(self, **kwargs):
            self.failed.append(kwargs)

        async def record_completed(self, **kwargs):
            self.completed.append(kwargs)

    class _FailingMultiAgentManager:
        async def get_agent(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("workspace unavailable")

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )
    store = _Store()
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            cron_broadcast_children_store=store,
            multi_agent_manager=_FailingMultiAgentManager(),
            tenant_workspace_pool=None,
        ),
    )

    synchronized = asyncio.run(
        api_module._process_dispatch_broadcast_children(
            app,
            source_job,
            agent_id="default",
            source_id="source-a",
            reason="test",
        ),
    )

    assert synchronized is False
    assert store.completed == []
    assert store.failed[0]["tenant_count"] == 1


def test_dispatch_child_processing_records_failed_snapshot_on_tenant_list_error(
    monkeypatch,
):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        raise RuntimeError("tenant list unavailable")

    class _Store:
        def __init__(self):
            self.running = []
            self.failed = []
            self.completed = []

        async def mark_running(self, **kwargs):
            self.running.append(kwargs)
            return True

        async def record_failed(self, **kwargs):
            self.failed.append(kwargs)

        async def record_completed(self, **kwargs):
            self.completed.append(kwargs)

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {"broadcast_dispatch_intents_enabled": True},
        },
    )
    store = _Store()
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            cron_broadcast_children_store=store,
            multi_agent_manager=object(),
            tenant_workspace_pool=None,
        ),
    )

    asyncio.run(
        api_module._process_dispatch_broadcast_children(
            app,
            source_job,
            agent_id="default",
            source_id="source-a",
            reason="test",
        ),
    )

    assert store.running == []
    assert store.completed == []
    assert store.failed
    assert store.failed[0]["tenant_count"] == 0
    assert "tenant list unavailable" in store.failed[0]["failure_summary"]


def test_dispatch_child_processing_records_failed_when_completion_store_errors(
    monkeypatch,
):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return []

    class _Store:
        def __init__(self):
            self.failed = []

        async def mark_running(self, **kwargs):
            del kwargs
            return True

        async def record_failed(self, **kwargs):
            self.failed.append(kwargs)

        async def record_completed(self, **kwargs):
            del kwargs
            raise RuntimeError("completion store unavailable")

    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    store = _Store()
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            cron_broadcast_children_store=store,
            multi_agent_manager=_MultiAgentManager({}),
            tenant_workspace_pool=None,
        ),
    )

    synchronized = asyncio.run(
        api_module._process_dispatch_broadcast_children(
            app,
            source_job,
            agent_id="default",
            source_id="source-a",
            reason="test",
        ),
    )

    assert synchronized is False
    assert store.failed[0]["tenant_count"] == 0
    assert "completion store unavailable" in store.failed[0]["failure_summary"]


def test_dispatch_child_processing_can_rollback_batch_flag(monkeypatch):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return ["tenant-b"]

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
            "meta": {},
        },
    )
    child = CronJobSpec.model_validate(
        {
            **_job_spec("child-job"),
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {
                "broadcast_source_job_id": "job-source",
                "broadcast_dispatch_intents_enabled": True,
            },
        },
    )
    child_manager = _Manager({"child-job": child})
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            cron_broadcast_children_store=None,
            multi_agent_manager=_MultiAgentManager(
                {
                    encode_scope_id("tenant-b", "source-a"): _Workspace(
                        child_manager,
                    ),
                },
            ),
            tenant_workspace_pool=None,
        ),
    )

    asyncio.run(
        api_module._process_dispatch_broadcast_children(
            app,
            source_job,
            agent_id="default",
            source_id="source-a",
            reason="rollback",
            enable=False,
        ),
    )

    saved = child_manager.created[-1]
    assert saved.meta["broadcast_source_job_id"] == "job-source"
    assert "broadcast_dispatch_intents_enabled" not in saved.meta


def test_batch_delete_broadcast_children_validates_source(monkeypatch):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return ["tenant-b"]

    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    matching_child = CronJobSpec.model_validate(
        {
            **_job_spec("child-b"),
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {"broadcast_source_job_id": "job-source"},
        },
    )
    unrelated_child = CronJobSpec.model_validate(
        {
            **_job_spec("unrelated-child"),
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {"broadcast_source_job_id": "other-source"},
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_manager = _Manager(
        {
            "child-b": matching_child,
            "unrelated-child": unrelated_child,
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=_MultiAgentManager(
            {
                encode_scope_id("tenant-b", "source-a"): _Workspace(
                    target_manager,
                ),
            },
        ),
        tenant_workspace_pool=_TenantWorkspacePool(),
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast/children/delete",
        json={
            "items": [
                {"tenant_id": "tenant-b", "job_id": "child-b"},
                {"tenant_id": "tenant-b", "job_id": "unrelated-child"},
            ],
        },
    )

    assert response.status_code == 200
    assert target_manager.deleted == ["child-b"]
    assert response.json()["results"] == [
        {
            "tenant_id": "tenant-b",
            "job_id": "child-b",
            "success": True,
            "status": "deleted",
            "message": "",
        },
        {
            "tenant_id": "tenant-b",
            "job_id": "unrelated-child",
            "success": False,
            "status": "failed",
            "message": "child job does not belong to source job",
        },
    ]


def test_batch_run_broadcast_children_skips_disabled_jobs(monkeypatch):
    async def _list_tenants(_source_id, source_filter=True):
        del source_filter
        return ["tenant-b"]

    monkeypatch.setattr(api_module, "list_logical_tenant_ids", _list_tenants)
    source_job = CronJobSpec.model_validate(
        {
            **_job_spec("job-source"),
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    )
    enabled_child = CronJobSpec.model_validate(
        {
            **_job_spec("child-enabled"),
            "enabled": True,
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {"broadcast_source_job_id": "job-source"},
        },
    )
    disabled_child = CronJobSpec.model_validate(
        {
            **_job_spec("child-disabled"),
            "enabled": False,
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {"broadcast_source_job_id": "job-source"},
        },
    )
    source_manager = _Manager({"job-source": source_job})
    target_manager = _Manager(
        {
            "child-enabled": enabled_child,
            "child-disabled": disabled_child,
        },
    )
    client = _build_client(
        source_manager,
        multi_agent_manager=_MultiAgentManager(
            {
                encode_scope_id("tenant-b", "source-a"): _Workspace(
                    target_manager,
                ),
            },
        ),
        tenant_workspace_pool=_TenantWorkspacePool(),
    )

    response = client.post(
        "/cron/jobs/job-source/broadcast/children/run",
        json={
            "items": [
                {"tenant_id": "tenant-b", "job_id": "child-enabled"},
                {"tenant_id": "tenant-b", "job_id": "child-disabled"},
            ],
        },
    )

    assert response.status_code == 200
    assert target_manager.ran == ["child-enabled"]
    assert response.json()["results"] == [
        {
            "tenant_id": "tenant-b",
            "job_id": "child-enabled",
            "success": True,
            "status": "started",
            "message": "",
        },
        {
            "tenant_id": "tenant-b",
            "job_id": "child-disabled",
            "success": True,
            "status": "skipped",
            "message": "paused, not executed",
        },
    ]
