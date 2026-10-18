# -*- coding: utf-8 -*-
"""验证新链路中文注释及关键调用日志，禁止泄漏任务正文和认证信息。"""

import ast
import io
import logging
import re
import tokenize
from pathlib import Path

import pytest

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
