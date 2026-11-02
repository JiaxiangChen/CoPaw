# -*- coding: utf-8 -*-
"""Workflow Cron integration keeps Agent execution separate."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.crons.models import CronJobSpec
from swe.app.crons.api import _build_broadcast_job
from swe.app.crons.manager import CronManager
from swe.app.crons.monitor_sync_client import MonitorSyncClient
from swe.app.crons.task_view import build_cron_task_view
from swe.app.crons.models import CronJobState
from swe.app.crons.workflow.cron_adapter import WorkflowCronExecutor
from swe.app.crons.workflow.cron_adapter import _default_secret_loader
from swe.app.crons.workflow.engine import WorkflowOutcome
from swe.app.crons.workflow.models import WorkflowValueMapping


@pytest.fixture(autouse=True)
def mock_trace_output_index(monkeypatch):
    from swe.app.crons.workflow import cron_adapter

    indexer = AsyncMock()
    monkeypatch.setattr(cron_adapter, "index_trace_output", indexer)
    return indexer


def _job() -> CronJobSpec:
    return CronJobSpec.model_validate(
        {
            "id": "job-1",
            "name": "skill task",
            "tenant_id": "user-1",
            "source_id": "RMASSIST",
            "schedule": {"cron": "0 9 * * *"},
            "task_type": "workflow",
            "skill_ids": "skill-a",
            "workflow_binding_id": "binding-1",
            "dispatch": {
                "channel": "console",
                "target": {
                    "user_id": "user-1",
                    "session_id": "cron-task:job-1",
                },
            },
            "meta": {
                "creator_user_id": "user-1",
                "task_session_id": "cron-task:job-1",
            },
        },
    )


class _Session:
    def __init__(self):
        self.state = {}

    async def mutate_session_state(
        self,
        session_id,
        mutator,
        user_id,
        create_if_not_exist,
    ):
        assert (session_id, user_id, create_if_not_exist) == (
            "cron-task:job-1",
            "user-1",
            True,
        )
        self.state = mutator(self.state)

    async def get_session_state_dict(self, _session_id, _user_id):
        return self.state


class _Trace:
    enabled = True

    def __init__(self):
        self.ended = []

    async def start_trace(self, **_kwargs):
        return "trace-1"

    async def emit_skill_invocation(self, **_kwargs):
        return "span-1"

    async def end_skill_invocation(self, **_kwargs):
        return None

    async def end_trace(self, trace_id, status, error=None):
        self.ended.append((trace_id, status, error))


@pytest.mark.asyncio
async def test_workflow_adapter_persists_result_once_and_returns_cron_result(
    mock_trace_output_index,
):
    config = SimpleNamespace(
        binding_id="binding-1",
        version=2,
        skill_id="skill-a",
        provider_id="provider-a",
        model_id="model-a",
        headers={
            "Authorization": WorkflowValueMapping(
                source="secret",
                key="auth_token",
            ),
        },
    )

    class _Store:
        async def get_current(self, binding_id, *, source_id=None):
            assert binding_id == "binding-1"
            assert source_id == "RMASSIST"
            return config

    class _Engine:
        calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            assert _kwargs["secrets"] == {"auth_token": "target-token"}
            assert _kwargs["runtime"]["trace_id"] == "trace-1"
            assert _kwargs["runtime"]["cron_job_id"] == "job-1"
            assert _kwargs["runtime"]["job_id"] == "job-1"
            return WorkflowOutcome(
                display_text="报告已生成",
                selected_result={"result": "报告已生成"},
                input_snapshot={"workflow_binding_id": "binding-1"},
                http_status=200,
            )

    session = _Session()
    trace = _Trace()
    engine = _Engine()
    executor = WorkflowCronExecutor(
        config_store=_Store(),
        session=session,
        channel_manager=SimpleNamespace(),
        engine=engine,
        env_loader=lambda _job: {"sapId": "user-1"},
        secret_loader=lambda _job, _keys, _env: {
            "auth_token": "target-token",
        },
        trace_manager=lambda: trace,
    )
    dispatch_meta = {
        "scheduled_fire_at": "2026-09-24T09:00:00+08:00",
        "trace_id": "untrusted-trace",
        "cron_job_id": "untrusted-job",
    }

    first = await executor.execute(_job(), dispatch_meta)
    second = await executor.execute(_job(), dispatch_meta)

    assert first.trace_id == "trace-1"
    assert first.output_preview == "报告已生成"
    assert first.execution_meta["workflow"]["version"] == 2
    assert len(session.state["task_messages"]) == 1
    assert engine.calls == 2
    assert len(trace.ended) == 2
    assert mock_trace_output_index.await_count == 2
    assert second.input_snapshot["cron_execution_key"] == (
        first.input_snapshot["cron_execution_key"]
    )


@pytest.mark.asyncio
async def test_workflow_configuration_failure_still_ends_trace():
    class _Store:
        async def get_current(self, _binding_id, *, source_id=None):
            assert source_id == "RMASSIST"
            raise ValueError("workflow binding disabled")

    trace = _Trace()
    executor = WorkflowCronExecutor(
        config_store=_Store(),
        session=_Session(),
        channel_manager=SimpleNamespace(),
        env_loader=lambda _job: {"sapId": "user-1"},
        trace_manager=lambda: trace,
    )

    with pytest.raises(ValueError, match="disabled") as error:
        await executor.execute(_job())

    assert error.value.cron_trace_id == "trace-1"
    assert len(trace.ended) == 1


@pytest.mark.asyncio
async def test_batch_workflow_without_frozen_version_fails_closed():
    class _Store:
        async def get_current(self, *_args, **_kwargs):
            raise AssertionError("batch run must not read latest config")

    executor = WorkflowCronExecutor(
        config_store=_Store(),
        session=_Session(),
        channel_manager=SimpleNamespace(),
        env_loader=lambda _job: {"sapId": "user-1"},
        trace_manager=lambda: _Trace(),
    )

    with pytest.raises(ValueError, match="version"):
        await executor.execute(
            _job(),
            {
                "source": "dispatch_service",
                "intent_id": 7,
                "batch_id": "batch-1",
                "dispatch_attempt": 1,
            },
        )


@pytest.mark.asyncio
async def test_trace_cleanup_failure_keeps_workflow_success():
    class _Store:
        async def get_current(self, _binding_id, *, source_id=None):
            return SimpleNamespace(
                binding_id="binding-1",
                version=2,
                skill_id="skill-a",
                provider_id="p",
                model_id="m",
            )

    class _Engine:
        async def execute(self, *_args, **_kwargs):
            return WorkflowOutcome(
                display_text="已完成",
                selected_result={"result": "已完成"},
                input_snapshot={},
                http_status=200,
            )

    class _FailingTrace(_Trace):
        async def end_trace(self, *_args, **_kwargs):
            raise RuntimeError("trace store unavailable")

    executor = WorkflowCronExecutor(
        config_store=_Store(),
        session=_Session(),
        channel_manager=SimpleNamespace(),
        engine=_Engine(),
        env_loader=lambda _job: {"sapId": "user-1"},
        trace_manager=lambda: _FailingTrace(),
    )

    result = await executor.execute(_job())

    assert result.status == "success"
    assert result.trace_id == "trace-1"


@pytest.mark.asyncio
async def test_cancelled_trace_cleanup_preserves_completed_workflow():
    class _Store:
        async def get_current(self, _binding_id, *, source_id=None):
            return SimpleNamespace(
                binding_id="binding-1",
                version=2,
                skill_id="skill-a",
                provider_id="p",
                model_id="m",
            )

    class _Engine:
        async def execute(self, *_args, **_kwargs):
            return WorkflowOutcome(
                display_text="已完成",
                selected_result={"result": "已完成"},
                input_snapshot={},
                http_status=200,
            )

    class _CancelledTrace(_Trace):
        async def end_trace(self, *_args, **_kwargs):
            raise asyncio.CancelledError()

    session = _Session()
    executor = WorkflowCronExecutor(
        config_store=_Store(),
        session=session,
        channel_manager=SimpleNamespace(),
        engine=_Engine(),
        env_loader=lambda _job: {"sapId": "user-1"},
        trace_manager=lambda: _CancelledTrace(),
    )

    result = await executor.execute(_job())

    assert result.status == "success"
    assert len(session.state["task_messages"]) == 1


@pytest.mark.asyncio
async def test_cron_manager_routes_workflow_without_agent_executor():
    class _AgentExecutor:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("workflow entered Agent executor")

    class _WorkflowExecutor:
        async def execute(self, job, dispatch_meta):
            assert job.task_type == "workflow"
            assert dispatch_meta == {"cron_is_manual": True}
            return SimpleNamespace(status="success")

    manager = object.__new__(CronManager)
    manager._executor = _AgentExecutor()
    manager._workflow_executor = _WorkflowExecutor()

    result = await manager._execute_job_by_type(
        _job(),
        {"cron_is_manual": True},
    )

    assert result.status == "success"


def test_workflow_task_is_visible_in_my_tasks():
    task = build_cron_task_view(_job(), CronJobState(), "user-1")

    assert task.visible_in_my_tasks is True


def test_user_task_schedule_includes_workflow_jobs():
    manager = object.__new__(CronManager)
    workflow = _job()

    assert manager._filter_jobs_by_user([workflow], "user-1") == [workflow]


def test_workflow_success_qualifies_for_completion_notification():
    client = MonitorSyncClient("")

    assert client._execution_needs_notification(_job(), "success") is True
    assert client._execution_needs_notification(_job(), "error") is False


@pytest.mark.asyncio
async def test_workflow_target_secret_loader_uses_cron_auth_and_tenant_env(
    monkeypatch,
):
    from swe.app.crons.workflow import cron_adapter

    async def resolve_auth(**kwargs):
        assert kwargs["tenant_id"]
        return SimpleNamespace(token="token-1", cookie_header="cookie-1")

    monkeypatch.setattr(
        cron_adapter,
        "resolve_auth_token_for_execution",
        resolve_auth,
    )

    secrets = await _default_secret_loader(
        _job(),
        {"auth_token", "cookie", "CUSTOM_KEY"},
        {"sapId": "user-1", "CUSTOM_KEY": "secret-2"},
    )

    assert secrets == {
        "auth_token": "token-1",
        "cookie": "cookie-1",
        "CUSTOM_KEY": "secret-2",
    }


@pytest.mark.asyncio
async def test_workflow_expired_cron_auth_uses_existing_terminal_error(
    monkeypatch,
):
    from swe.app.crons.workflow import cron_adapter

    async def expired(**_kwargs):
        raise ValueError("cron auth user_info is expired")

    monkeypatch.setattr(
        cron_adapter,
        "resolve_auth_token_for_execution",
        expired,
    )

    with pytest.raises(
        RuntimeError,
        match="cron auth user_info is expired; please refresh",
    ):
        await _default_secret_loader(
            _job(),
            {"auth_token"},
            {"sapId": "user-1"},
        )


def test_broadcast_child_keeps_shared_workflow_binding():
    child = _build_broadcast_job(
        _job(),
        job_id="child-1",
        target_tenant_id="user-2",
        target_tenant_name="Second User",
        target_bbk_id="200",
        source_id="RMASSIST",
        cron="5 9 * * *",
        timezone_name="UTC",
        offset_minutes=5,
        model_slot=None,
        model_slot_fallback_reason="",
    )

    assert child.task_type == "workflow"
    assert child.workflow_binding_id == "binding-1"
    assert child.dispatch.target.user_id == "user-2"


@pytest.mark.asyncio
async def test_workflow_success_updates_task_and_monitor_execution():
    class _Repo:
        def __init__(self, job):
            self.job = job

        async def get_job(self, _job_id):
            return self.job

        async def load(self):
            from swe.app.crons.models import JobsFile

            return JobsFile(jobs=[self.job])

        async def save(self, jobs_file):
            self.job = jobs_file.jobs[0]

    class _Store:
        async def get_current(self, _binding_id, *, source_id=None):
            assert source_id == "RMASSIST"
            return SimpleNamespace(
                binding_id="binding-1",
                version=2,
                skill_id="skill-a",
                provider_id="p",
                model_id="m",
            )

    class _Engine:
        async def execute(self, *_args, **_kwargs):
            return WorkflowOutcome(
                display_text="报告已生成",
                selected_result={"result": "报告已生成"},
                input_snapshot={"workflow_binding_id": "binding-1"},
                http_status=200,
            )

    class _Monitor:
        records = []

        async def record_execution(self, **kwargs):
            self.records.append(kwargs)

    job = _job()
    repo = _Repo(job)
    session = _Session()
    runner = SimpleNamespace(session=session)
    manager = CronManager(
        repo=repo,
        runner=runner,
        channel_manager=SimpleNamespace(),
        workflow_config_store=_Store(),
    )
    manager._workflow_executor = WorkflowCronExecutor(
        config_store=_Store(),
        session=session,
        channel_manager=SimpleNamespace(),
        engine=_Engine(),
        env_loader=lambda _job: {"sapId": "user-1"},
        trace_manager=lambda: _Trace(),
    )
    monitor = _Monitor()
    manager._monitor_sync_client = monitor

    await manager._execute_once(
        job,
        is_manual=False,
        dispatch_meta={"scheduled_fire_at": "2026-09-24T09:00:00Z"},
    )

    assert repo.job.meta["task_unread_execution_count"] == 1
    assert session.state["task_messages"][0]["content"][0]["text"] == (
        "报告已生成"
    )
    assert len(monitor.records) == 1
    assert monitor.records[0]["status"] == "success"
    assert monitor.records[0]["trace_id"] == "trace-1"
    assert monitor.records[0]["session_id"] == "cron-task:job-1"
    assert monitor.records[0]["meta"]["session_state_committed"] is True
