# -*- coding: utf-8 -*-
"""验证新链路中文注释及关键调用日志，禁止泄漏任务正文和认证信息。"""

import ast
import io
import logging
import re
import tokenize
from pathlib import Path

import pytest
import httpx

from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)
from tests.unit.scheduler.test_managed_cron_management import create_payload


def test_new_backend_comments_and_docstrings_use_chinese():
    root = Path(__file__).resolve().parents[3]
    files = [
        *root.joinpath(
            "scheduler/src/scheduler/app/services/cron/managed",
        ).glob("*.py"),
        *root.joinpath("scheduler/src/scheduler/app/routers").glob(
            "managed_cron*.py",
        ),
        *root.joinpath("src/swe/app/crons").glob("managed*.py"),
    ]
    missing = []
    for path in files:
        source = path.read_text(encoding="utf-8-sig")
        for node in ast.walk(ast.parse(source)):
            if isinstance(
                node,
                (
                    ast.Module,
                    ast.ClassDef,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                ),
            ):
                text = ast.get_docstring(node)
                if text and not re.search(r"[\u4e00-\u9fff]", text):
                    missing.append(f"{path.name}:{getattr(node, 'lineno', 1)}")
        comments = [
            token
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        ]
        for comment in comments:
            if not comment.string.startswith(
                ("# -*-", "# noqa", "# pylint", "# type:"),
            ) and not re.search(r"[\u4e00-\u9fff]", comment.string):
                missing.append(f"{path.name}:{comment.start[0]}")
    assert not missing, missing


@pytest.mark.asyncio
async def test_management_key_logs_include_identity_not_payload_or_secrets(
    execution_setup,
    caplog,
):
    db, caller, management = execution_setup
    caplog.set_level(
        logging.INFO,
        logger="scheduler.app.services.cron.managed.management",
    )
    created = await management.create(
        caller,
        create_payload(request={"input": "private-task-body"}),
    )
    assert created["operation_id"] in caplog.text
    assert created["job_id"] in caplog.text
    assert "受理" in caplog.text
    assert "private-task-body" not in caplog.text
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_jobs_v2").fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_execution_logs_include_run_identity_but_not_private_error(
    execution_setup,
    caplog,
    monkeypatch,
):
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter
    from tests.unit.app.test_managed_cron_execution import Engine, key

    db = execution_setup[0]
    row, _ = await enqueue(execution_setup)
    caplog.set_level(logging.INFO, logger="swe.app.crons.managed_execution")
    logger = logging.getLogger("swe.app.crons.managed_execution")
    monkeypatch.setattr(logger, "handlers", [*logger.handlers, caplog.handler])
    await ManagedExecutionService(
        ExecutionWriter(db),
        Engine(failure=True),
    ).execute(row["execution_id"], key(row))
    assert row["execution_id"] in caplog.text
    assert "开始" in caplog.text
    assert "提交" in caplog.text
    assert "do-not-expose" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http_status", "timeout"])
async def test_prepare_failure_logs_http_diagnostics_without_secrets(
    execution_setup,
    caplog,
    failure,
):
    from scheduler.app.services.cron.managed.swe_client import ManagedSweClient

    db, caller, management = execution_setup

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private-task-body", request=request)
        return httpx.Response(
            422,
            json={
                "detail": "cron_resource_preparation_failed",
                "input": "private-task-body",
                "token": "private-response-token",
            },
        )

    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as client:
        management.resources = ManagedSweClient(
            "http://swe",
            http=client,
            headers={"Authorization": "Bearer private-request-token"},
        )
        result = await management.create(
            caller,
            create_payload(request={"input": "private-task-body"}),
        )
    assert result["error_code"] == "swe_prepare_failed"
    assert result["status"] == "failed"
    assert result["operation_id"] in caplog.text
    assert result["job_id"] in caplog.text
    assert "stage=prepare" in caplog.text
    assert "swe_client.py" in caplog.text
    if failure == "http_status":
        assert "HTTPStatusError" in caplog.text
        assert "http_status=422" in caplog.text
        assert "cron_resource_preparation_failed" in caplog.text
    else:
        assert "ReadTimeout" in caplog.text
    for secret in (
        "private-task-body",
        "private-request-token",
        "private-response-token",
    ):
        assert secret not in caplog.text
        assert secret not in str(result)
    assert management.platform.calls == []
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_jobs_v2").fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_prepare_failure_logs_safe_exception_chain_not_private_messages(
    execution_setup,
    caplog,
    monkeypatch,
):
    _, caller, management = execution_setup

    async def fail_prepare(*_args):
        try:
            raise RuntimeError("private-task-body password=private-password")
        except RuntimeError as exc:
            raise ValueError("cron_chat_manager_unavailable") from exc

    monkeypatch.setattr(management.resources, "prepare", fail_prepare)
    caplog.set_level(logging.INFO)
    result = await management.create(caller, create_payload())
    assert result["error_code"] == "swe_prepare_failed"
    assert "ValueError" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "fail_prepare" in caplog.text
    assert "cron_chat_manager_unavailable" in caplog.text
    assert "private-task-body" not in caplog.text
    assert "private-password" not in caplog.text


@pytest.mark.parametrize(
    "response, expected",
    [
        (
            httpx.Response(
                422,
                json={
                    "detail": [
                        {
                            "type": "missing",
                            "loc": ["body", "operation_id"],
                            "input": "private-input",
                            "ctx": {"token": "private-token"},
                        },
                    ],
                },
            ),
            "missing at body.operation_id",
        ),
        (httpx.Response(500, text="private-input"), "unavailable"),
        (
            httpx.Response(500, text="private-input" * 1000),
            "omitted_large_body",
        ),
        (
            httpx.Response(500, json={"detail": "private-input"}),
            "omitted_non_code_body",
        ),
    ],
)
def test_http_error_details_never_include_body_input_or_context(
    response,
    expected,
):
    from scheduler.app.services.cron.managed.diagnostics import (
        format_management_error,
    )

    error = httpx.HTTPStatusError(
        "private-input",
        request=httpx.Request(
            "POST",
            "http://swe/prepare?token=private-token",
        ),
        response=response,
    )
    detail = format_management_error(error)
    assert expected in detail
    assert "private-input" not in detail
    assert "private-token" not in detail


@pytest.mark.parametrize(
    "number, meaning",
    [(1205, "lock_wait_timeout"), (1213, "deadlock")],
)
def test_database_error_keeps_number_not_raw_statement(number, meaning):
    from pymysql.err import OperationalError
    from scheduler.app.services.cron.managed.diagnostics import (
        format_management_error,
    )

    detail = format_management_error(OperationalError(number, "private-sql"))
    assert f"error_number={number}" in detail
    assert meaning in detail
    assert "private-sql" not in detail
