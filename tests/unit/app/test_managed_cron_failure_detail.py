# -*- coding: utf-8 -*-
"""新旧错误说明语义一致，并贯通新版执行历史和状态。"""

import json
import asyncio

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)
from tests.unit.app.test_managed_cron_execution import key


@pytest.mark.asyncio
async def test_workflow_public_detail_reaches_history_and_state(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter
    from swe.app.crons.workflow.engine import WorkflowCallError

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    spec = json.loads(row["spec_json"])
    spec.update(
        task_type="workflow",
        skill_ids="skill-one",
        workflow_binding_id="binding-one",
    )
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET spec_json=?",
        (json.dumps(spec),),
    )

    class Workflow:
        async def execute(self, *_args, **_kwargs):
            raise WorkflowCallError(
                "技能任务执行失败 Authorization: Bearer secret-token",
                status_code=502,
                error_code="http_status",
            )

    await ManagedExecutionService(
        ExecutionWriter(db),
        None,
        Workflow(),
    ).execute(row["execution_id"], key(row))
    history = dict(
        db.conn.execute("SELECT * FROM swe_cron_executions").fetchone(),
    )
    state = dict(
        db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone(),
    )
    run = dict(
        db.conn.execute("SELECT * FROM swe_cron_execution_runs").fetchone(),
    )
    assert run["error_code"] == "cron_execution_failed"
    assert history["error_message"] == (
        "技能任务执行失败 Authorization: Bearer [REDACTED]"
    )
    assert history["error_message"] == state["last_error"]
    assert "secret-token" not in json.dumps([history, state, run])


@pytest.mark.asyncio
async def test_general_failure_keeps_message_but_redacts_token(
    execution_setup,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter
    from tests.unit.app.test_managed_cron_execution import Engine

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    await ManagedExecutionService(
        ExecutionWriter(db),
        Engine(failure=True),
    ).execute(row["execution_id"], key(row))
    history = dict(
        db.conn.execute("SELECT * FROM swe_cron_executions").fetchone(),
    )
    assert history["error_message"] == "private token=[REDACTED]"
    assert "do-not-expose" not in json.dumps(history)


def test_public_detail_is_bounded_and_legacy_metadata_falls_back():
    from swe.app.crons.managed_failure import (
        public_failure_detail,
        public_error_message,
    )
    from swe.app.crons.workflow.engine import WorkflowCallError

    detail = public_failure_detail(
        WorkflowCallError(
            "技能失败 " + "x" * 4000,
            status_code=503,
            error_code="http_status",
        ),
    )
    assert detail == ("技能失败 " + "x" * 4000)[:200]
    assert (
        public_error_message("cron_execution_failed", None)
        == "cron_execution_failed"
    )
    assert (
        public_error_message("cron_execution_failed", "{invalid")
        == "cron_execution_failed"
    )
    assert public_error_message(None, {"error_detail": "旧错误"}) is None


@pytest.mark.parametrize("task_type", ["text", "agent", "workflow"])
@pytest.mark.parametrize(
    "message",
    [
        "子任务执行失败: 报告生成失败",
        "获取子任务状态超时",
        "workflow secret value missing: user_info",
        "cron auth user_info is expired; please refresh cron auth configuration",
        "workflow task session binding missing",
        "channel not found: console",
        "失败详情" * 100,
    ],
)
@pytest.mark.asyncio
async def test_all_task_types_preserve_legacy_failure_message(
    execution_setup,
    task_type,
    message,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    spec = json.loads(row["spec_json"])
    spec.update(task_type=task_type, text="测试任务")
    if task_type == "workflow":
        spec.update(skill_ids="skill-one", workflow_binding_id="binding-one")
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET spec_json=?",
        (json.dumps(spec),),
    )

    class FailingEngine:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError(message)

    engine = FailingEngine()
    await ManagedExecutionService(
        ExecutionWriter(db),
        engine,
        engine,
    ).execute(row["execution_id"], key(row))
    history = db.conn.execute("SELECT * FROM swe_cron_executions").fetchone()
    state = db.conn.execute("SELECT * FROM swe_cron_job_states").fetchone()
    run = db.conn.execute("SELECT * FROM swe_cron_execution_runs").fetchone()
    assert history["error_message"] == message[:200]
    assert state["last_error"] == message[:200]
    assert run["error_code"] == "cron_execution_failed"


@pytest.mark.parametrize(
    "error, code, message",
    [
        (
            asyncio.CancelledError(),
            "cron_execution_cancelled",
            "Job was cancelled",
        ),
        (
            TimeoutError("上游处理超时"),
            "cron_execution_timeout",
            "上游处理超时",
        ),
        (TimeoutError(), "cron_execution_timeout", "cron_execution_timeout"),
        (RuntimeError(), "cron_execution_failed", "cron_execution_failed"),
    ],
)
def test_cancellation_timeout_and_empty_failures(error, code, message):
    from swe.app.crons.managed_execution import _failure_outcome
    from swe.app.crons.managed_failure import public_error_message

    outcome = _failure_outcome(error)
    assert outcome.error_code == code
    assert public_error_message(code, outcome.execution_meta) == message


@pytest.mark.parametrize(
    "credential",
    [
        "token=do-not-expose",
        '"token": "do-not-expose"',
        "Authorization: Bearer do-not-expose",
        "cookie: auth=do-not-expose",
        "api_key=do-not-expose",
    ],
)
def test_general_exception_redacts_credentials_without_expanding_cause(
    credential,
):
    from swe.app.crons.managed_failure import public_failure_detail

    error = RuntimeError("子任务执行失败 " + credential)
    error.__cause__ = ValueError("private upstream payload")
    detail = public_failure_detail(error)
    assert detail.startswith("子任务执行失败")
    assert "do-not-expose" not in detail
    assert "private upstream payload" not in detail
    assert "[REDACTED]" in detail
