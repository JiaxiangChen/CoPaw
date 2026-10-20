# -*- coding: utf-8 -*-
"""隔离 MySQL 验证批关系、并发人工重试、Monitor SQL 与人工回填材料。"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from monitor.app.models.managed_board import BoardDetailQuery

from tests.integrated.scheduler.test_managed_cron_mysql import (
    SOURCE,
    mysql_pair,
)
from tests.unit.scheduler.test_managed_cron_management import (
    PlatformClient,
    ResourceClient,
    create_payload,
)
from tests.unit.scheduler.test_managed_batch_retry import retry_body


async def failed_mysql_batch(pair):
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter

    db, caller = pair.scheduler, pair.caller
    created = await ManagementService(
        db,
        ResourceClient(),
        PlatformClient(),
    ).create(
        caller,
        {**create_payload("board-mysql"), "dispatch_state": "batch_enabled"},
    )
    binding = await db.fetch_one(
        "SELECT batch_external_job_id FROM swe_cron_job_states WHERE tenant_id=%s AND job_id=%s",
        (caller.tenant_id, created["job_id"]),
    )
    command = {
        "job_id": created["job_id"],
        "external_job_id": binding["batch_external_job_id"],
        "operation_id": created["operation_id"],
        "callback_protocol": "scheduler_managed_v1",
        "trigger_id": "board-period",
        "scheduled_fire_at": "2026-10-16T09:00:00+08:00",
    }
    response = await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    ).accept(caller, command, "batch")
    run = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE tenant_id=%s",
        (caller.tenant_id,),
    )
    claimed = await ExecutionDispatcher(db, None)._claim(run, True)
    writer = ExecutionWriter(pair.swe)
    key = caller.job_key(created["job_id"])
    await writer.accept(
        run["execution_id"],
        key,
        dispatch_token=claimed["dispatch_owner"],
    )
    await writer.start(run["execution_id"], key, "board-owner")
    await writer.complete(
        run["execution_id"],
        key,
        "board-owner",
        ExecutionOutcome(
            "error",
            error_code="cron_execution_failed",
            execution_meta={"failure_type": "model_rate_limit"},
        ),
    )
    return replace(caller, is_manager=True), response["batch_id"]


@pytest.mark.asyncio
async def test_mysql_board_query_and_concurrent_retry_one_current_attempt(
    mysql_pair,
):
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    caller, batch = await failed_mysql_batch(mysql_pair)
    service = BatchRetryService(mysql_pair.scheduler)
    preview = await service.preview(caller, batch)
    first, second = await asyncio.gather(
        service.retry(caller, batch, retry_body(preview, "mysql-manager-1")),
        service.retry(
            replace(caller, actor_id="real-second-admin"),
            batch,
            retry_body(preview, "mysql-manager-2"),
        ),
    )
    assert first["queued"] + second["queued"] == 1
    assert first["skipped"] + second["skipped"] == 1
    query = ManagedBoardQuery(mysql_pair.swe)
    listed = await query.batches(SOURCE)
    assert any(item["batch_id"] == batch for item in listed["items"])
    detail = await query.detail(SOURCE, batch, BoardDetailQuery(event_limit=1))
    assert detail["batch"]["member_count"] == 1
    assert detail["members"][0]["business_attempt"] == 2
    assert detail["batch"]["status"] == "pending"
    assert detail["event_total"] >= 8


@pytest.mark.asyncio
async def test_mysql_manual_backfill_restores_only_missing_real_members(
    mysql_pair,
):
    from scheduler.app.services.cron.managed.storage import (
        TransactionConnection,
    )

    caller, batch = await failed_mysql_batch(mysql_pair)
    db = mysql_pair.scheduler
    await db.execute(
        "DELETE FROM swe_cron_managed_batch_members WHERE batch_id=%s",
        (batch,),
    )
    path = (
        Path(__file__).resolve().parents[3]
        / "docs/deployment/cron_batch_board_manual_backfill.sql"
    )
    source = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    source = source.replace(
        "SET @cron_board_source = '请替换为真实source_id'",
        f"SET @cron_board_source = '{SOURCE}'",
    )
    async with db.acquire() as connection:
        tx = TransactionConnection(connection)
        for statement in source.split(";"):
            if statement.strip():
                await tx.execute(statement)
        await connection.commit()
    member = await db.fetch_one(
        "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s",
        (batch,),
    )
    assert member["tenant_id"] == caller.tenant_id
    assert member["initial_execution_id"] == member["current_execution_id"]
    assert member["member_role"] == "parent"
    assert member["business_attempt"] == 1
