# -*- coding: utf-8 -*-
"""SWE 保留公共错误语义，同时输出可定位且不含私密输入的诊断。"""

import logging
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pymysql.err import OperationalError

from tests.unit.app.test_managed_cron_resources import Chats, valid_model
from tests.unit.scheduler.test_managed_cron_management import (
    ManagementDatabase,
    create_payload,
)


@pytest.fixture
async def resource_api(monkeypatch, caplog):
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.operations import ManagementStore
    from swe.app.crons import managed_api
    from swe.app.crons.managed_resources import ResourcePreparer

    # SWE 不向根 logger 传播，在实际模块 logger 上捕获输出。
    monkeypatch.setattr(
        managed_api.logger,
        "handlers",
        [*managed_api.logger.handlers, caplog.handler],
    )
    db = ManagementDatabase()
    caller = CallerIdentity("alice", "bank", "default", "alice")
    operation, _ = await ManagementStore(db).begin(
        caller,
        "create",
        create_payload(),
    )
    chats = Chats()
    resources = ResourcePreparer(
        chats,
        agent_id="default",
        validate_model=valid_model,
    )

    async def factory(_request):
        return resources

    monkeypatch.setattr(managed_api, "get_resource_preparer", factory)
    app = FastAPI()
    app.state.db_connection = db
    app.include_router(managed_api.router, prefix="/api")
    try:
        yield app, db, operation, chats
    finally:
        db.conn.close()


async def _prepare(app, operation):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/api/internal/cron/managed/prepare",
            headers={
                "X-User-Id": "alice",
                "X-Source-Id": "bank",
                "Authorization": "Bearer private-token",
            },
            json={
                "operation_id": operation["operation_id"],
                "job_id": operation["job_id"],
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    ["begin_transaction", "lock_job", "finish_transaction"],
)
async def test_storage_failure_logs_stage_and_database_cause(
    resource_api,
    monkeypatch,
    caplog,
    stage,
):
    app, db, operation, chats = resource_api
    transaction = db.transaction
    fetch_one = db.fetch_one

    def fail_database():
        try:
            raise RuntimeError("private-input password=private-token")
        except RuntimeError as exc:
            raise OperationalError(1205, "private-input") from exc

    @asynccontextmanager
    async def failing_transaction():
        if stage == "begin_transaction":
            fail_database()
        async with transaction():
            yield
            if stage == "finish_transaction":
                fail_database()

    async def failing_fetch(query, params=None):
        if stage == "lock_job" and "FOR UPDATE" in query:
            fail_database()
        return await fetch_one(query, params)

    monkeypatch.setattr(db, "transaction", failing_transaction)
    monkeypatch.setattr(db, "fetch_one", failing_fetch)
    caplog.set_level(logging.ERROR, logger="swe.app.crons.managed_api")
    response = await _prepare(app, operation)
    assert response.status_code == 503
    assert response.json() == {"detail": "cron_resource_storage_unavailable"}
    assert operation["operation_id"] in caplog.text
    assert operation["job_id"] in caplog.text
    assert f"stage={stage}" in caplog.text
    assert "OperationalError" in caplog.text
    assert "error_number=1205" in caplog.text
    assert "lock_wait_timeout" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "fail_database" in caplog.text
    assert "private-input" not in caplog.text
    assert "private-token" not in caplog.text
    assert chats.created == (1 if stage == "finish_transaction" else 0)


@pytest.mark.asyncio
async def test_missing_transaction_logs_public_attribute_not_private_message(
    resource_api,
    monkeypatch,
    caplog,
):
    app, db, operation, _ = resource_api

    def unavailable_transaction():
        raise AttributeError("private-input", name="transaction")

    monkeypatch.setattr(db, "transaction", unavailable_transaction)
    caplog.set_level(logging.ERROR, logger="swe.app.crons.managed_api")
    response = await _prepare(app, operation)
    assert response.status_code == 503
    assert "AttributeError" in caplog.text
    assert "missing_attribute=transaction" in caplog.text
    assert "private-input" not in caplog.text


@pytest.mark.asyncio
async def test_public_http_error_is_not_reclassified_as_storage_failure(
    resource_api,
    monkeypatch,
    caplog,
):
    app, db, operation, _ = resource_api
    transaction = db.transaction

    @asynccontextmanager
    async def conflict():
        async with transaction():
            yield
            raise HTTPException(409, "cron_operation_superseded")

    monkeypatch.setattr(db, "transaction", conflict)
    caplog.set_level(logging.ERROR, logger="swe.app.crons.managed_api")
    response = await _prepare(app, operation)
    assert response.status_code == 409
    assert response.json() == {"detail": "cron_operation_superseded"}
    assert "cron_resource_storage_unavailable" not in caplog.text


@pytest.mark.parametrize(
    "number, category",
    [(1213, "deadlock"), (1054, "unknown_column"), (1146, "table_missing")],
)
def test_storage_diagnostic_keeps_error_number_without_sql(number, category):
    from swe.app.crons.managed_diagnostics import format_storage_error

    detail = format_storage_error(OperationalError(number, "private-sql"))
    assert f"error_number={number}" in detail
    assert category in detail
    assert "private-sql" not in detail
