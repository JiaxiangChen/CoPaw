# -*- coding: utf-8 -*-
"""联合验证真实定时任务执行器、数据库执行登记和会话持久化。"""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    execution_setup,
)


class Session:
    def __init__(self):
        self.state = {}

    async def mutate_session_state(self, *, mutator, **_kwargs):
        self.state = mutator(self.state)


class AgentRunner:
    def __init__(self):
        self.session = Session()
        self.requests = []

    async def stream_query(self, request):
        from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

        self.requests.append(request)
        self.session.state["task_messages"] = [
            {"role": "assistant", "content": "Agent report"},
        ]
        yield SimpleNamespace(
            object="message",
            status=RunStatus.Completed,
            role="assistant",
            content=[SimpleNamespace(type="text", text="Agent report")],
        )
        yield SimpleNamespace(object="response", status=RunStatus.Completed)

    def get_query_persistence_result(self, **kwargs):
        from swe.app.runner.query_contracts import QueryPersistenceResult

        return QueryPersistenceResult(
            session_id=kwargs["session_id"],
            user_id=kwargs["user_id"],
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
            persisted_assistant_content=[
                {"type": "text", "text": "Agent report"},
            ],
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt",
    reason="Existing Runner persistence contracts require Linux fcntl",
)
async def test_real_agent_executor_preserves_execution_identity_and_committed_output(
    execution_setup,
    tmp_path,
    monkeypatch,
):
    from swe.app.crons import executor as executor_module
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    monkeypatch.setattr(executor_module, "has_trace_manager", lambda: False)
    monkeypatch.setattr(
        executor_module.CronExecutor,
        "_resolve_execution_model",
        lambda *_: None,
    )
    monkeypatch.setattr(
        executor_module.CronExecutor,
        "_apply_auth_token",
        AsyncMock(),
    )
    db = execution_setup[0]
    row = await queue_kind(
        execution_setup,
        {
            "task_type": "agent",
            "request": {
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "report"}],
                    },
                ],
            },
        },
    )
    runner = AgentRunner()
    executor = executor_module.CronExecutor(
        runner=runner,
        channel_manager=SimpleNamespace(),
    )
    service = ManagedExecutionService(
        ExecutionWriter(db),
        executor,
        workspace_dir=tmp_path,
    )
    await service.execute(
        row["execution_id"],
        run_key(row),
        live_headers={"CRON_JOB_ID": "forged", "X-B3-TraceId": "trace-input"},
    )
    await service.execute(row["execution_id"], run_key(row))
    history = db.conn.execute("SELECT * FROM swe_cron_executions").fetchone()
    assert history["status"] == "success"
    assert history["output_preview"] == "Agent report"
    assert len(runner.requests) == 1
    headers = runner.requests[0]["passthrough_headers"]
    assert headers["cron_job_id"] == row["job_id"]
    assert headers["x-b3-traceid"] == "trace-input"
    assert not list(tmp_path.rglob("jobs.json"))


async def queue_kind(execution_setup, spec):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, management = execution_setup
    created = await management.create(
        caller,
        {
            "request_id": "engine-create",
            "dispatch_state": "batch_paused",
            "spec": {
                "name": "engine task",
                "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
                **spec,
            },
        },
    )
    assert created["status"] == "completed"
    row, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest(
            "batch",
            "logical-period",
            datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
        ),
    )
    return row


def run_key(row):
    return tuple(
        row[name] for name in ("tenant_id", "source_id", "agent_id", "job_id")
    )


@pytest.mark.asyncio
async def test_real_text_executor_persists_one_message_without_jobs_file(
    execution_setup,
    tmp_path,
    monkeypatch,
):
    from swe.app.crons import executor as executor_module
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    monkeypatch.setattr(executor_module, "has_trace_manager", lambda: False)
    db = execution_setup[0]
    row = await queue_kind(
        execution_setup,
        {"task_type": "text", "text": "真实执行器文本"},
    )
    session = Session()
    engine = executor_module.CronExecutor(
        runner=SimpleNamespace(session=session),
        channel_manager=SimpleNamespace(),
    )
    service = ManagedExecutionService(
        ExecutionWriter(db),
        engine,
        workspace_dir=tmp_path,
    )
    await service.execute(row["execution_id"], run_key(row))
    await service.execute(row["execution_id"], run_key(row))
    history = db.conn.execute("SELECT * FROM swe_cron_executions").fetchone()
    assert history["status"] == "success"
    assert history["output_preview"] == "真实执行器文本"
    assert len(session.state["task_messages"]) == 1
    assert not list(tmp_path.rglob("jobs.json"))


def workflow_tables(db):
    db.conn.executescript("""
        CREATE TABLE swe_workflow_bindings (
            binding_id TEXT PRIMARY KEY, source_id TEXT,
            current_version INTEGER, enabled INTEGER
        );
        CREATE TABLE swe_workflow_binding_versions (
            binding_id TEXT, version INTEGER, config_json TEXT
        );
    """)
    db.conn.execute(
        "INSERT INTO swe_workflow_bindings VALUES ('binding-one','bank',1,1)",
    )
    db.conn.execute(
        "INSERT INTO swe_workflow_binding_versions VALUES ('binding-one',1,?)",
        (json.dumps({"provider_id": "provider", "model_id": "model"}),),
    )


class WorkflowStore:
    def __init__(self, *, missing=False):
        self.versions = []
        self.missing = missing

    async def get_version(self, binding_id, version, *, source_id):
        assert (binding_id, source_id) == ("binding-one", "bank")
        self.versions.append(version)
        if self.missing:
            raise ValueError("frozen workflow version unavailable")
        return SimpleNamespace(
            binding_id=binding_id,
            version=version,
            skill_id="skill-one",
            provider_id="provider",
            model_id="model",
        )

    async def get_current(self, *_args, **_kwargs):
        raise AssertionError(
            "New execution must not resolve the current version",
        )


class WorkflowEngine:
    def __init__(self):
        self.calls = 0

    async def execute(self, config, **_kwargs):
        from swe.app.crons.workflow.engine import WorkflowOutcome

        self.calls += 1
        assert config.version == 1
        return WorkflowOutcome(
            display_text="冻结配置的报告",
            selected_result={},
            input_snapshot={},
            http_status=200,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_real_workflow_adapter_uses_frozen_version_not_current(
    execution_setup,
    tmp_path,
    monkeypatch,
    missing,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter
    from swe.app.crons.workflow import cron_adapter

    db = execution_setup[0]
    workflow_tables(db)
    row = await queue_kind(
        execution_setup,
        {
            "task_type": "workflow",
            "skill_ids": "skill-one",
            "workflow_binding_id": "binding-one",
        },
    )
    db.conn.execute("UPDATE swe_workflow_bindings SET current_version=2")
    session, store, engine = (
        Session(),
        WorkflowStore(missing=missing),
        WorkflowEngine(),
    )
    adapter = cron_adapter.WorkflowCronExecutor(
        config_store=store,
        session=session,
        channel_manager=SimpleNamespace(),
        engine=engine,
        env_loader=lambda _: {},
        secret_loader=lambda *_: {},
        trace_manager=lambda: None,
    )
    service = ManagedExecutionService(
        ExecutionWriter(db),
        None,
        adapter,
        workspace_dir=tmp_path,
    )
    await service.execute(row["execution_id"], run_key(row))
    history = db.conn.execute("SELECT * FROM swe_cron_executions").fetchone()
    assert store.versions == [1]
    assert history["status"] == ("error" if missing else "success")
    assert history["error_message"] == (
        "frozen workflow version unavailable" if missing else ""
    )
    assert engine.calls == (0 if missing else 1)
    assert not list(tmp_path.rglob("jobs.json"))
