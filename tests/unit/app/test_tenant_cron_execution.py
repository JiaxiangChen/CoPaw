# -*- coding: utf-8 -*-
"""Cron executor tenant context regression tests."""

# pylint: disable=protected-access
import asyncio
import importlib
import importlib.util
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from swe.config.config import Config
from swe.config.utils import save_config

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
_MODELS_FILE = SRC_ROOT / "swe" / "app" / "crons" / "models.py"
_EXECUTOR_FILE = SRC_ROOT / "swe" / "app" / "crons" / "executor.py"

_ORIGINAL_MODULES = {
    name: sys.modules.get(name)
    for name in [
        "swe.app",
        "swe.app.crons",
        "swe.app.channels.schema",
        "swe.app.crons.models",
        "swe.app.crons.executor",
    ]
}

if "swe.app" not in sys.modules:
    app_pkg = types.ModuleType("swe.app")
    app_pkg.__path__ = [str(SRC_ROOT / "swe" / "app")]
    sys.modules["swe.app"] = app_pkg

if "swe.app.crons" not in sys.modules:
    crons_pkg = types.ModuleType("swe.app.crons")
    crons_pkg.__path__ = [str(SRC_ROOT / "swe" / "app" / "crons")]
    sys.modules["swe.app.crons"] = crons_pkg

channels_schema = types.ModuleType("swe.app.channels.schema")
channels_schema.ChannelType = str
channels_schema.DEFAULT_CHANNEL = "console"
sys.modules["swe.app.channels.schema"] = channels_schema

context_module = importlib.import_module("swe.config.context")
llm_workload_module = importlib.import_module("swe.config.llm_workload")
importlib.import_module("swe.app.tenant_context")

models_spec = importlib.util.spec_from_file_location(
    "swe.app.crons.models",
    _MODELS_FILE,
)
assert models_spec is not None and models_spec.loader is not None
models_module = importlib.util.module_from_spec(models_spec)
sys.modules["swe.app.crons.models"] = models_module
models_spec.loader.exec_module(models_module)


executor_spec = importlib.util.spec_from_file_location(
    "swe.app.crons.executor",
    _EXECUTOR_FILE,
)
assert executor_spec is not None and executor_spec.loader is not None
executor_module = importlib.util.module_from_spec(executor_spec)
sys.modules["swe.app.crons.executor"] = executor_module
executor_spec.loader.exec_module(executor_module)

for _name, _module in _ORIGINAL_MODULES.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module


auth_state_module = importlib.import_module("swe.app.crons.auth_state")


CronExecutor = executor_module.CronExecutor
CronJobRequest = models_module.CronJobRequest
CronJobSpec = models_module.CronJobSpec
DispatchSpec = models_module.DispatchSpec
DispatchTarget = models_module.DispatchTarget
JobRuntimeSpec = models_module.JobRuntimeSpec
ScheduleSpec = models_module.ScheduleSpec


def _get_current_workspace_dir():
    return context_module.get_current_workspace_dir()


def _get_current_source_id():
    return context_module.get_current_source_id()


def _get_current_effective_tenant_id():
    return context_module.get_current_effective_tenant_id()


def _get_current_llm_workload():
    return llm_workload_module.get_current_llm_workload()


class _Session:
    def __init__(self) -> None:
        self.state: dict[str, object] = {}

    async def mutate_session_state(self, *, mutator, **_kwargs) -> None:
        self.state = mutator(self.state)


class _Runner:
    def __init__(self) -> None:
        self.session = _Session()

    async def stream_query(self, _req):
        for _item in ():
            yield _item


class _ChannelManager:
    async def send_text(self, **_kwargs):
        return None

    async def send_event(self, **_kwargs):
        return None


def _build_text_job(workspace_dir: str) -> object:
    return CronJobSpec(
        id="job-text",
        name="text job",
        tenant_id="tenant-a",
        source_id="source-a",
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="text",
        text="hello",
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={"workspace_dir": workspace_dir},
        ),
        runtime=JobRuntimeSpec(timeout_seconds=1),
        meta={
            "creator_user_id": "user-a",
            "task_session_id": "task-session-a",
        },
    )


def _build_agent_job(workspace_dir: str, timeout_seconds: int = 1) -> object:
    return CronJobSpec(
        id="job-agent",
        name="agent job",
        tenant_id="tenant-a",
        source_id="source-a",
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(input=[{"content": [{"text": "ping"}]}]),
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={"workspace_dir": workspace_dir},
        ),
        runtime=JobRuntimeSpec(timeout_seconds=timeout_seconds),
    )


@pytest.mark.asyncio
async def test_prepare_agent_execution_marks_trace_for_attach_existing(
    monkeypatch,
) -> None:
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha")

    monkeypatch.setattr(
        executor,
        "_create_trace_for_agent_job",
        lambda *_args, **_kwargs: asyncio.sleep(0, result="trace-cron"),
    )

    _runtime_tenant_id, trace_id, req, _stream_state = (
        await executor._prepare_agent_execution(
            job,
            "user-a",
            "session-a",
            {},
        )
    )

    assert trace_id == "trace-cron"
    assert req["trace_id"] == "trace-cron"
    assert req["trace_attach_existing"] is True


@pytest.mark.asyncio
async def test_prepare_agent_execution_uses_b3_dispatch_meta(monkeypatch):
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha")
    observed = {}

    async def fake_create_trace(*_args, **kwargs):
        observed["trace_id"] = kwargs.get("trace_id")
        observed["b3_trace_id"] = kwargs.get("b3_trace_id")
        return kwargs.get("trace_id") or "trace-cron"

    monkeypatch.setattr(
        executor,
        "_create_trace_for_agent_job",
        fake_create_trace,
    )

    _runtime_tenant_id, trace_id, req, _stream_state = (
        await executor._prepare_agent_execution(
            job,
            "user-a",
            "session-a",
            {
                "passthrough_headers": {
                    "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
                    "X-B3-Spanid": "32befd146889a61a",
                },
                "b3_trace_id": "8267fd70bacf497704fec30eaa353979",
                "scheduled_fire_at": "2026-09-08T06:00:00Z",
            },
        )
    )

    assert observed["trace_id"] == "8267fd70bacf497704fec30eaa353979"
    assert observed["b3_trace_id"] == "8267fd70bacf497704fec30eaa353979"
    assert trace_id == "8267fd70bacf497704fec30eaa353979"
    assert req["trace_id"] == "8267fd70bacf497704fec30eaa353979"
    assert req["trace_attach_existing"] is True
    assert req["b3_trace_id"] == "8267fd70bacf497704fec30eaa353979"
    assert req["passthrough_headers"] == {
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "X-B3-Spanid": "32befd146889a61a",
    }


@pytest.mark.asyncio
async def test_prepare_agent_execution_splits_batch_trace_from_b3(monkeypatch):
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha")
    observed = {}

    async def fake_create_trace(*_args, **kwargs):
        observed.update(kwargs)
        return kwargs.get("trace_id")

    monkeypatch.setattr(
        executor,
        "_create_trace_for_agent_job",
        fake_create_trace,
    )
    passthrough_headers = {
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "X-B3-Spanid": "32befd146889a61a",
    }

    _runtime_tenant_id, trace_id, req, _stream_state = (
        await executor._prepare_agent_execution(
            job,
            "user-a",
            "session-a",
            {
                "source": " dispatch_service ",
                "intent_id": 7,
                "batch_id": " batch-1 ",
                "dispatch_attempt": 2,
                "passthrough_headers": passthrough_headers,
                "b3_trace_id": "8267fd70bacf497704fec30eaa353979",
            },
        )
    )

    assert observed["trace_id"] != "8267fd70bacf497704fec30eaa353979"
    assert str(uuid.UUID(observed["trace_id"])) == observed["trace_id"]
    assert observed["b3_trace_id"] == "8267fd70bacf497704fec30eaa353979"
    assert trace_id == observed["trace_id"]
    assert req["trace_id"] == observed["trace_id"]
    assert req["trace_attach_existing"] is True
    assert req["b3_trace_id"] == "8267fd70bacf497704fec30eaa353979"
    assert req["passthrough_headers"] == passthrough_headers


@pytest.mark.asyncio
async def test_batch_trace_fallback_ids_are_unique_when_precreate_fails(
    monkeypatch,
) -> None:
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha")
    observed = []

    async def fake_create_trace(*_args, **kwargs):
        observed.append(kwargs)
        return None

    monkeypatch.setattr(
        executor,
        "_create_trace_for_agent_job",
        fake_create_trace,
    )
    b3_trace_id = "8267fd70bacf497704fec30eaa353979"
    passthrough_headers = {
        "X-B3-Traceid": b3_trace_id,
        "X-B3-Spanid": "32befd146889a61a",
    }
    dispatch_meta = {
        "source": "dispatch_service",
        "intent_id": 7,
        "batch_id": "batch-1",
        "dispatch_attempt": 2,
        "passthrough_headers": passthrough_headers,
        "b3_trace_id": b3_trace_id,
    }

    executions = [
        await executor._prepare_agent_execution(
            job,
            "user-a",
            "session-a",
            dispatch_meta,
        )
        for _ in range(2)
    ]

    internal_trace_ids = [execution[1] for execution in executions]
    assert all(internal_trace_ids)
    assert len(set(internal_trace_ids)) == 2
    assert b3_trace_id not in internal_trace_ids
    for index, (_runtime_id, trace_id, req, _state) in enumerate(executions):
        assert str(uuid.UUID(trace_id)) == trace_id
        assert observed[index]["trace_id"] == trace_id
        assert observed[index]["b3_trace_id"] == b3_trace_id
        assert req["trace_id"] == trace_id
        assert req["trace_attach_existing"] is True
        assert req["b3_trace_id"] == b3_trace_id
        assert req["passthrough_headers"] == passthrough_headers


@pytest.mark.parametrize(
    "dispatch_meta",
    [
        {
            "source": "manual",
            "intent_id": 7,
            "batch_id": "batch-1",
            "dispatch_attempt": 2,
        },
        {
            "source": "dispatch_service",
            "intent_id": True,
            "batch_id": "batch-1",
            "dispatch_attempt": 2,
        },
        {
            "source": "dispatch_service",
            "intent_id": 7,
            "batch_id": "   ",
            "dispatch_attempt": 2,
        },
        {
            "source": "dispatch_service",
            "intent_id": 7,
            "batch_id": "batch-1",
            "dispatch_attempt": False,
        },
    ],
)
def test_malformed_dispatch_identity_is_not_a_batch(dispatch_meta) -> None:
    b3_trace_id = "8267fd70bacf497704fec30eaa353979"
    dispatch_meta["b3_trace_id"] = b3_trace_id

    assert executor_module._resolve_dispatch_trace_ids(dispatch_meta) == (
        b3_trace_id,
        b3_trace_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dispatch_meta", "is_batch"),
    [
        (
            {
                "source": "dispatch_service",
                "intent_id": 7,
                "batch_id": "batch-1",
                "dispatch_attempt": 2,
                "b3_trace_id": "8267fd70bacf497704fec30eaa353979",
            },
            True,
        ),
        (
            {
                "b3_trace_id": "8267fd70bacf497704fec30eaa353979",
                "external_execution_id": "trace-1",
            },
            False,
        ),
    ],
)
async def test_execute_text_job_resolves_trace_identifiers(
    monkeypatch,
    dispatch_meta,
    is_batch,
) -> None:
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")
    trace_manager = types.SimpleNamespace(
        enabled=True,
        start_trace=AsyncMock(
            side_effect=lambda **kwargs: kwargs.get("trace_id"),
        ),
        end_trace=AsyncMock(),
    )
    monkeypatch.setattr(executor_module, "has_trace_manager", lambda: True)
    monkeypatch.setattr(
        executor_module,
        "get_trace_manager",
        lambda: trace_manager,
    )
    monkeypatch.setattr(
        executor_module,
        "resolve_user_identity",
        AsyncMock(
            return_value=types.SimpleNamespace(user_name=None, bbk_id=None),
        ),
    )
    monkeypatch.setattr(
        executor_module,
        "_index_model_output_to_monitor",
        AsyncMock(),
    )

    result = await executor._execute_text_job(
        job,
        "user-a",
        "session-a",
        dispatch_meta,
    )

    start_kwargs = trace_manager.start_trace.await_args.kwargs
    if is_batch:
        assert start_kwargs["trace_id"] != dispatch_meta["b3_trace_id"]
        assert (
            str(uuid.UUID(start_kwargs["trace_id"]))
            == start_kwargs["trace_id"]
        )
    else:
        assert start_kwargs["trace_id"] == dispatch_meta["b3_trace_id"]
    assert start_kwargs["b3_trace_id"] == ("8267fd70bacf497704fec30eaa353979")
    assert result["trace_id"] == start_kwargs["trace_id"]


@pytest.mark.asyncio
async def test_execute_text_job_keeps_batch_id_when_precreate_fails(
    monkeypatch,
) -> None:
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")
    b3_trace_id = "8267fd70bacf497704fec30eaa353979"
    trace_manager = types.SimpleNamespace(
        enabled=True,
        start_trace=AsyncMock(side_effect=RuntimeError("store unavailable")),
        end_trace=AsyncMock(),
    )
    monkeypatch.setattr(executor_module, "has_trace_manager", lambda: True)
    monkeypatch.setattr(
        executor_module,
        "get_trace_manager",
        lambda: trace_manager,
    )
    monkeypatch.setattr(
        executor_module,
        "resolve_user_identity",
        AsyncMock(
            return_value=types.SimpleNamespace(user_name=None, bbk_id=None),
        ),
    )

    result = await executor._execute_text_job(
        job,
        "user-a",
        "session-a",
        {
            "source": "dispatch_service",
            "intent_id": 7,
            "batch_id": "batch-1",
            "dispatch_attempt": 2,
            "b3_trace_id": b3_trace_id,
        },
    )

    start_kwargs = trace_manager.start_trace.await_args.kwargs
    assert result["trace_id"] == start_kwargs["trace_id"]
    assert result["trace_id"] != b3_trace_id
    assert str(uuid.UUID(result["trace_id"])) == result["trace_id"]
    assert start_kwargs["b3_trace_id"] == b3_trace_id
    trace_manager.end_trace.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_text_job_preserves_execution_key_in_snapshot() -> None:
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")

    result = await executor._execute_text_job(
        job,
        "user-a",
        "session-a",
        {"cron_execution_key": "execution-123"},
    )

    assert result["input_snapshot"] == {
        "text": "hello",
        "cron_execution_key": "execution-123",
    }


@pytest.mark.asyncio
async def test_execute_text_job_passes_stable_delivery_key_to_channel() -> (
    None
):
    class _RecordingChannelManager(_ChannelManager):
        def __init__(self) -> None:
            self.texts: list[dict[str, object]] = []

        async def send_text(self, **kwargs) -> bool:
            self.texts.append(kwargs)
            return True

    channel_manager = _RecordingChannelManager()
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=channel_manager,
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha").model_copy(
        update={
            "dispatch": DispatchSpec(
                channel="zhaohu",
                target=DispatchTarget(
                    user_id="user-a",
                    session_id="session-a",
                ),
            ),
        },
    )

    await executor._execute_text_job(
        job,
        "user-a",
        "session-a",
        {"scheduled_fire_at": "2026-09-08T02:30:00Z"},
    )

    meta = channel_manager.texts[0]["meta"]
    assert isinstance(meta, dict)
    assert meta["cron_delivery_key"] == (
        "cron:job-text:2026-09-08T02:30:00Z:session-a:output"
    )


@pytest.mark.asyncio
async def test_execute_text_job_builds_key_from_scheduled_fire_time() -> None:
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")

    result = await executor._execute_text_job(
        job,
        "user-a",
        "session-a",
        {"scheduled_fire_at": "2026-09-08T02:30:00Z"},
    )

    assert result["input_snapshot"]["cron_execution_key"] == (
        "job-text:2026-09-08T02:30:00Z:session-a"
    )


class _Provider:
    def __init__(self, models: list[str]):
        self._models = set(models)

    def has_model(self, model_id: str) -> bool:
        return model_id in self._models


def test_job_runtime_defaults_allow_longer_cron_runs() -> None:
    runtime = JobRuntimeSpec()

    assert runtime.timeout_seconds == 7200
    assert runtime.misfire_grace_seconds == 300


def test_execute_binds_workspace_dir_during_job_and_resets_afterward(
    monkeypatch,
):
    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        dispatch_meta,
    ):
        observed["workspace_in_job"] = _get_current_workspace_dir()
        observed["meta_workspace"] = dispatch_meta.get("workspace_dir")

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)

    asyncio.run(executor.execute(job))

    assert observed["workspace_in_job"] == Path(
        "/tmp/tenant-a/workspaces/alpha",
    )
    assert _get_current_workspace_dir() is None


def test_execute_binds_source_scope_during_job_and_resets_afterward(
    monkeypatch,
):
    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        dispatch_meta,
    ):
        observed["source_id"] = _get_current_source_id()
        observed["effective_tenant_id"] = _get_current_effective_tenant_id()
        observed["meta_source"] = dispatch_meta.get("source_id")
        observed["meta_scope"] = dispatch_meta.get("scope_id")

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)

    asyncio.run(executor.execute(job))

    assert observed["source_id"] == "source-a"
    assert observed["effective_tenant_id"] is not None
    assert observed["meta_source"] == "source-a"
    assert observed["meta_scope"] == observed["effective_tenant_id"]
    assert _get_current_source_id() is None


def test_prepare_execution_context_resolves_scope_and_workspace_dir() -> None:
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")

    context = executor._prepare_execution_context(job)

    assert context.target_user_id == "user-a"
    assert context.target_session_id == "session-a"
    assert context.workspace_dir == Path("/tmp/tenant-a/workspaces/alpha")
    assert context.tenant_id == "tenant-a"
    assert context.source_id == "source-a"
    assert context.scope_id == context_module.encode_scope_id(
        "tenant-a",
        "source-a",
    )
    assert context.dispatch_meta == {
        "workspace_dir": "/tmp/tenant-a/workspaces/alpha",
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "scope_id": context_module.encode_scope_id("tenant-a", "source-a"),
    }


def test_build_execution_result_truncates_output_preview() -> None:
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )

    result = executor._build_execution_result(
        {
            "trace_id": "trace-1",
            "output_preview": "x" * 120,
            "input_snapshot": {"text": "hello"},
            "executor_leader": "leader-1",
        },
        execution_meta={"fallback_reason": "provider_not_found"},
    )

    assert result == executor_module.ExecutionResult(
        trace_id="trace-1",
        output_preview="x" * 100,
        input_snapshot={"text": "hello"},
        executor_leader="leader-1",
        execution_meta={"fallback_reason": "provider_not_found"},
    )


def test_build_agent_request_includes_runtime_scope():
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/beta")
    job = job.model_copy(
        update={
            "scope_id": context_module.encode_scope_id("tenant-a", "source-a"),
        },
    )

    req = executor._build_agent_request(job, "user-a", "session-a")

    assert req["execution_origin"] == "scheduled"
    assert req["source_id"] == "source-a"
    assert req["scope_id"] == context_module.encode_scope_id(
        "tenant-a",
        "source-a",
    )


def test_build_agent_request_skips_scope_for_default_source_template():
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job(
        "/tmp/default_source-a/workspaces/default",
    ).model_copy(
        update={
            "tenant_id": "default",
            "source_id": "source-a",
            "scope_id": None,
        },
    )

    req = executor._build_agent_request(job, "default", "session-a")

    assert req["source_id"] == "source-a"
    assert "scope_id" not in req


def test_build_agent_request_skips_scope_for_empty_default_source_id():
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job(
        "/tmp/default/workspaces/default",
    ).model_copy(
        update={
            "tenant_id": "default",
            "source_id": "",
            "scope_id": None,
        },
    )

    req = executor._build_agent_request(job, "default", "session-a")

    assert "source_id" not in req
    assert "scope_id" not in req


def test_build_agent_request_removes_stale_trace_id():
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/beta").model_copy(
        update={
            "request": CronJobRequest.model_validate(
                {
                    "input": [{"content": [{"text": "ping"}]}],
                    "trace_id": "stale-trace-id",
                },
            ),
        },
    )

    req = executor._build_agent_request(job, "user-a", "session-a")

    assert "trace_id" not in req


@pytest.mark.asyncio
async def test_apply_auth_token_uses_runtime_scope(monkeypatch):
    """Cron auth 读取必须使用 scope_id，不能回退到逻辑 tenant。"""
    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/beta").model_copy(
        update={
            "scope_id": context_module.encode_scope_id(
                "tenant-a",
                "source-a",
            ),
        },
    )

    async def fake_resolve_auth_token_for_execution(
        *,
        tenant_id,
        workspace_dir,
    ):
        observed["tenant_id"] = tenant_id
        observed["workspace_dir"] = workspace_dir
        return types.SimpleNamespace(token=None, cookie_header=None)

    monkeypatch.setattr(
        executor_module,
        "resolve_auth_token_for_execution",
        fake_resolve_auth_token_for_execution,
    )

    await executor._apply_auth_token(
        job,
        {"workspace_dir": "/tmp/tenant-a/workspaces/beta"},
        {},
    )

    assert observed == {
        "tenant_id": context_module.encode_scope_id("tenant-a", "source-a"),
        "workspace_dir": "/tmp/tenant-a/workspaces/beta",
    }


def test_execute_binds_cron_workload_during_job_and_resets_afterward(
    monkeypatch,
):
    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        observed["workload"] = _get_current_llm_workload()

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)

    with llm_workload_module.bind_llm_workload(
        llm_workload_module.LLM_WORKLOAD_CHAT,
    ):
        asyncio.run(executor.execute(job))
        assert (
            _get_current_llm_workload()
            == llm_workload_module.LLM_WORKLOAD_CHAT
        )

    assert observed["workload"] == llm_workload_module.LLM_WORKLOAD_CRON
    assert _get_current_llm_workload() == llm_workload_module.LLM_WORKLOAD_CHAT


def test_execute_resets_workspace_dir_after_timeout(monkeypatch):
    observed = {}

    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/beta")

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        observed["workspace_in_job"] = _get_current_workspace_dir()
        raise asyncio.TimeoutError

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(executor.execute(job))

    assert observed["workspace_in_job"] == Path(
        "/tmp/tenant-a/workspaces/beta",
    )
    assert _get_current_workspace_dir() is None


def test_execute_aborts_agent_job_when_user_info_expired(monkeypatch):
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/beta")

    async def fake_resolve_auth_token_for_execution(
        *,
        tenant_id=None,
        workspace_dir=None,
    ):
        del tenant_id, workspace_dir
        raise ValueError("cron auth user_info is expired")

    monkeypatch.setattr(
        executor_module,
        "resolve_auth_token_for_execution",
        fake_resolve_auth_token_for_execution,
    )

    with pytest.raises(
        RuntimeError,
        match="please refresh cron auth configuration",
    ):
        asyncio.run(
            executor._execute_job(
                job,
                "user-a",
                "session-a",
                {
                    "workspace_dir": "/tmp/tenant-a/workspaces/beta",
                    "cron_is_manual": True,
                },
            ),
        )


def test_execute_allows_agent_job_when_user_info_missing(monkeypatch):
    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/beta")

    async def fake_resolve_auth_token_for_execution(
        *,
        tenant_id=None,
        workspace_dir=None,
    ):
        del tenant_id, workspace_dir
        return auth_state_module.ResolvedAuthToken(
            token=None,
            expires_at=None,
            reused=False,
            cookie_header=None,
        )

    async def fake_stream_query(req):
        observed["req"] = req
        for _item in ():
            yield _item

    monkeypatch.setattr(
        executor_module,
        "resolve_auth_token_for_execution",
        fake_resolve_auth_token_for_execution,
    )
    monkeypatch.setattr(executor._runner, "stream_query", fake_stream_query)

    with pytest.raises(RuntimeError, match="did not complete"):
        asyncio.run(
            executor._execute_job(
                job,
                "user-a",
                "session-a",
                {
                    "workspace_dir": "/tmp/tenant-a/workspaces/beta",
                    "cron_is_manual": True,
                },
            ),
        )

    assert observed["req"]["user_id"] == "user-a"
    assert observed["req"]["session_id"] == "session-a"
    assert "auth_token" not in observed["req"]
    assert "cookie" not in observed["req"]


def test_execute_injects_auth_token_and_cookie_into_agent_request(monkeypatch):
    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/beta")

    async def fake_resolve_auth_token_for_execution(
        *,
        tenant_id=None,
        workspace_dir=None,
    ):
        del tenant_id, workspace_dir
        return auth_state_module.ResolvedAuthToken(
            token="auth-123",
            expires_at=None,
            reused=True,
            cookie_header="foo=bar; com.cmb.dw.rtl.sso.token=auth-123",
        )

    async def fake_stream_query(req):
        observed["req"] = req
        for _item in ():
            yield _item

    monkeypatch.setattr(
        executor_module,
        "resolve_auth_token_for_execution",
        fake_resolve_auth_token_for_execution,
    )
    monkeypatch.setattr(executor._runner, "stream_query", fake_stream_query)

    with pytest.raises(RuntimeError, match="did not complete"):
        asyncio.run(
            executor._execute_job(
                job,
                "user-a",
                "session-a",
                {
                    "workspace_dir": "/tmp/tenant-a/workspaces/beta",
                    "cron_is_manual": True,
                },
            ),
        )

    assert observed["req"]["auth_token"] == "auth-123"
    assert observed["req"]["cookie"] == (
        "foo=bar; com.cmb.dw.rtl.sso.token=auth-123"
    )


def test_execute_exposes_tenant_process_limit_policy_inside_cron_context(
    monkeypatch,
    tmp_path: Path,
):
    from swe.config.context import encode_scope_id
    from swe.security.process_limits import (
        resolve_current_process_limit_policy,
    )

    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_text_job("/tmp/tenant-a/workspaces/alpha")

    scope_id = encode_scope_id("tenant-a", "source-a")
    tenant_dir = tmp_path / scope_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    save_config(
        Config.model_validate(
            {
                "security": {
                    "process_limits": {
                        "enabled": True,
                        "shell": True,
                        "mcp_stdio": True,
                        "cpu_time_limit_seconds": 3,
                    },
                },
            },
        ),
        tenant_dir / "config.json",
    )

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        policy = resolve_current_process_limit_policy("shell")
        observed["tenant_id"] = policy.tenant_id
        observed["enabled"] = policy.enabled
        observed["cpu"] = policy.cpu_time_limit_seconds

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)
    monkeypatch.setattr("swe.constant.WORKING_DIR", tmp_path)
    monkeypatch.setattr("swe.config.utils.WORKING_DIR", tmp_path)

    asyncio.run(executor.execute(job))

    assert observed == {
        "tenant_id": scope_id,
        "enabled": True,
        "cpu": 3,
    }


def test_execute_binds_explicit_model_slot_and_resets_afterward(monkeypatch):
    from swe.providers.models import ModelSlotConfig
    from swe.app.crons.model_slot_context import (
        get_current_model_slot_override,
    )

    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha").model_copy(
        update={
            "model_slot": ModelSlotConfig(
                provider_id="openai",
                model="gpt-5.4",
            ),
        },
    )

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        current = get_current_model_slot_override()
        observed["provider_id"] = (
            current.provider_id if current is not None else None
        )
        observed["model"] = current.model if current is not None else None
        return {}

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)
    monkeypatch.setattr(
        executor_module,
        "ProviderManager",
        types.SimpleNamespace(
            ensure_tenant_provider_storage=lambda _tenant_id: None,
            get_instance=lambda _tenant_id: types.SimpleNamespace(
                get_provider=lambda provider_id: (
                    _Provider(["gpt-5.4"]) if provider_id == "openai" else None
                ),
                get_active_model=lambda: ModelSlotConfig(
                    provider_id="anthropic",
                    model="claude-3-7-sonnet",
                ),
            ),
        ),
    )

    result = asyncio.run(executor.execute(job))

    assert observed == {
        "provider_id": "openai",
        "model": "gpt-5.4",
    }
    assert result.execution_meta == {
        "original_model_slot": {
            "provider_id": "openai",
            "model": "gpt-5.4",
        },
        "effective_model_slot": {
            "provider_id": "openai",
            "model": "gpt-5.4",
        },
        "fallback_reason": "",
    }
    assert get_current_model_slot_override() is None


def test_execute_falls_back_to_tenant_default_when_model_slot_is_missing(
    monkeypatch,
):
    from swe.providers.models import ModelSlotConfig
    from swe.app.crons.model_slot_context import (
        get_current_model_slot_override,
    )

    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha").model_copy(
        update={
            "model_slot": ModelSlotConfig(
                provider_id="openai",
                model="gpt-5.4",
            ),
        },
    )

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        current = get_current_model_slot_override()
        observed["override"] = None if current is None else current.model
        return {}

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)
    monkeypatch.setattr(
        executor_module,
        "ProviderManager",
        types.SimpleNamespace(
            ensure_tenant_provider_storage=lambda _tenant_id: None,
            get_instance=lambda _tenant_id: types.SimpleNamespace(
                get_provider=lambda _provider_id: None,
                get_active_model=lambda: ModelSlotConfig(
                    provider_id="anthropic",
                    model="claude-3-7-sonnet",
                ),
            ),
        ),
    )

    result = asyncio.run(executor.execute(job))

    assert observed["override"] is None
    assert result.execution_meta == {
        "original_model_slot": {
            "provider_id": "openai",
            "model": "gpt-5.4",
        },
        "effective_model_slot": {
            "provider_id": "anthropic",
            "model": "claude-3-7-sonnet",
        },
        "fallback_reason": "provider_not_found",
    }
    assert get_current_model_slot_override() is None


def test_execute_falls_back_to_tenant_default_when_model_is_missing(
    monkeypatch,
):
    from swe.providers.models import ModelSlotConfig
    from swe.app.crons.model_slot_context import (
        get_current_model_slot_override,
    )

    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha").model_copy(
        update={
            "model_slot": ModelSlotConfig(
                provider_id="openai",
                model="gpt-5.4",
            ),
        },
    )

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        current = get_current_model_slot_override()
        observed["override"] = None if current is None else current.model
        return {}

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)
    monkeypatch.setattr(
        executor_module,
        "ProviderManager",
        types.SimpleNamespace(
            ensure_tenant_provider_storage=lambda _tenant_id: None,
            get_instance=lambda _tenant_id: types.SimpleNamespace(
                get_provider=lambda provider_id: (
                    _Provider(["gpt-4.1"]) if provider_id == "openai" else None
                ),
                get_active_model=lambda: ModelSlotConfig(
                    provider_id="anthropic",
                    model="claude-3-7-sonnet",
                ),
            ),
        ),
    )

    result = asyncio.run(executor.execute(job))

    assert observed["override"] is None
    assert result.execution_meta == {
        "original_model_slot": {
            "provider_id": "openai",
            "model": "gpt-5.4",
        },
        "effective_model_slot": {
            "provider_id": "anthropic",
            "model": "claude-3-7-sonnet",
        },
        "fallback_reason": "model_not_found",
    }
    assert get_current_model_slot_override() is None


def test_execute_uses_broadcast_fallback_meta_when_model_slot_cleared(
    monkeypatch,
):
    from swe.providers.models import ModelSlotConfig
    from swe.app.crons.model_slot_context import (
        get_current_model_slot_override,
    )

    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha").model_copy(
        update={
            "model_slot": None,
            "meta": {
                "broadcast_original_model_slot": {
                    "provider_id": "openai",
                    "model": "gpt-5.4",
                },
                "broadcast_model_slot_fallback_reason": ("provider_not_found"),
            },
        },
    )

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        current = get_current_model_slot_override()
        observed["override"] = None if current is None else current.model
        return {}

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)
    monkeypatch.setattr(
        executor_module,
        "ProviderManager",
        types.SimpleNamespace(
            ensure_tenant_provider_storage=lambda _tenant_id: None,
            get_instance=lambda _tenant_id: types.SimpleNamespace(
                get_provider=lambda _provider_id: None,
                get_active_model=lambda: ModelSlotConfig(
                    provider_id="anthropic",
                    model="claude-3-7-sonnet",
                ),
            ),
        ),
    )

    result = asyncio.run(executor.execute(job))

    assert observed["override"] is None
    assert result.execution_meta == {
        "original_model_slot": {
            "provider_id": "openai",
            "model": "gpt-5.4",
        },
        "effective_model_slot": {
            "provider_id": "anthropic",
            "model": "claude-3-7-sonnet",
        },
        "fallback_reason": "provider_not_found",
    }
    assert get_current_model_slot_override() is None


def test_execute_uses_broadcast_model_not_found_meta_when_model_slot_cleared(
    monkeypatch,
):
    from swe.providers.models import ModelSlotConfig
    from swe.app.crons.model_slot_context import (
        get_current_model_slot_override,
    )

    observed = {}
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha").model_copy(
        update={
            "model_slot": None,
            "meta": {
                "broadcast_original_model_slot": {
                    "provider_id": "openai",
                    "model": "gpt-5.4",
                },
                "broadcast_model_slot_fallback_reason": "model_not_found",
            },
        },
    )

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        current = get_current_model_slot_override()
        observed["override"] = None if current is None else current.model
        return {}

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)
    monkeypatch.setattr(
        executor_module,
        "ProviderManager",
        types.SimpleNamespace(
            ensure_tenant_provider_storage=lambda _tenant_id: None,
            get_instance=lambda _tenant_id: types.SimpleNamespace(
                get_provider=lambda _provider_id: None,
                get_active_model=lambda: ModelSlotConfig(
                    provider_id="anthropic",
                    model="claude-3-7-sonnet",
                ),
            ),
        ),
    )

    result = asyncio.run(executor.execute(job))

    assert observed["override"] is None
    assert result.execution_meta == {
        "original_model_slot": {
            "provider_id": "openai",
            "model": "gpt-5.4",
        },
        "effective_model_slot": {
            "provider_id": "anthropic",
            "model": "claude-3-7-sonnet",
        },
        "fallback_reason": "model_not_found",
    }
    assert get_current_model_slot_override() is None


def test_execute_does_not_leak_model_slot_override_between_concurrent_runs(
    monkeypatch,
):
    from swe.providers.models import ModelSlotConfig
    from swe.app.crons.model_slot_context import (
        get_current_model_slot_override,
    )

    observed = {}
    release = asyncio.Event()
    entered = asyncio.Event()
    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    override_job = _build_agent_job(
        "/tmp/tenant-a/workspaces/alpha",
    ).model_copy(
        update={
            "id": "job-override",
            "model_slot": ModelSlotConfig(
                provider_id="openai",
                model="gpt-5.4",
            ),
        },
    )
    default_job = _build_agent_job(
        "/tmp/tenant-a/workspaces/beta",
    ).model_copy(update={"id": "job-default"})

    async def fake_execute_job(
        _self,
        job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        current = get_current_model_slot_override()
        observed[job.id] = None if current is None else current.model
        entered.set()
        await release.wait()
        return {}

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)
    monkeypatch.setattr(
        executor_module,
        "ProviderManager",
        types.SimpleNamespace(
            ensure_tenant_provider_storage=lambda _tenant_id: None,
            get_instance=lambda _tenant_id: types.SimpleNamespace(
                get_provider=lambda provider_id: (
                    _Provider(["gpt-5.4"]) if provider_id == "openai" else None
                ),
                get_active_model=lambda: ModelSlotConfig(
                    provider_id="anthropic",
                    model="claude-3-7-sonnet",
                ),
            ),
        ),
    )

    async def run_both():
        first = asyncio.create_task(executor.execute(override_job))
        await entered.wait()
        second = asyncio.create_task(executor.execute(default_job))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    asyncio.run(run_both())

    assert observed == {
        "job-override": "gpt-5.4",
        "job-default": None,
    }


def test_execute_does_not_mutate_tenant_active_model(monkeypatch):
    from swe.providers.models import ModelSlotConfig

    executor = CronExecutor(
        runner=_Runner(),
        channel_manager=_ChannelManager(),
    )
    job = _build_agent_job("/tmp/tenant-a/workspaces/alpha").model_copy(
        update={
            "model_slot": ModelSlotConfig(
                provider_id="openai",
                model="gpt-5.4",
            ),
        },
    )
    manager = types.SimpleNamespace(
        get_provider=lambda provider_id: (
            _Provider(["gpt-5.4"]) if provider_id == "openai" else None
        ),
        get_active_model=lambda: ModelSlotConfig(
            provider_id="anthropic",
            model="claude-3-7-sonnet",
        ),
    )

    async def fake_execute_job(
        _self,
        _job,
        _target_user_id,
        _target_session_id,
        _dispatch_meta,
    ):
        return {}

    monkeypatch.setattr(CronExecutor, "_execute_job", fake_execute_job)
    monkeypatch.setattr(
        executor_module,
        "ProviderManager",
        types.SimpleNamespace(
            ensure_tenant_provider_storage=lambda _tenant_id: None,
            get_instance=lambda _tenant_id: manager,
        ),
    )

    before = manager.get_active_model()
    asyncio.run(executor.execute(job))
    after = manager.get_active_model()

    assert after == before
