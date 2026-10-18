# -*- coding: utf-8 -*-
"""批成员按真实回执归属，普通复用、重放与错误关联均执行真实SQL。"""

from datetime import datetime, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
    body,
)


@pytest.mark.asyncio
async def test_real_batch_callback_persists_members_in_its_acceptance_transaction(
    callback_setup,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    command = body(db, created, kind="batch")
    first = await service.accept(caller, command, "batch")
    members = await db.fetch_all(
        "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s",
        (first["batch_id"],),
    )
    assert len(members) == 1
    assert (
        members[0]["member_id"]
        == members[0]["initial_execution_id"]
        == members[0]["current_execution_id"]
    )
    assert members[0]["business_attempt"] == 1
    assert members[0]["member_role"] == "parent"
    assert members[0]["source_id"] == caller.source_id
    assert (await service.accept(caller, command, "batch"))["reused"]
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_managed_batch_members"))
        == 1
    )


@pytest.mark.asyncio
async def test_batch_members_include_an_execution_first_accepted_as_ordinary(
    callback_setup,
):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup)
    scheduling = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    original = await scheduling.accept(caller, body(db, created), "ordinary")
    modes = DispatchStateService(db, management.platform)
    change = await modes.set(
        caller,
        created["job_id"],
        {
            "request_id": "batch-mode",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    await modes.process(change["operation_id"])
    command = {
        **body(db, created, kind="batch"),
        "operation_id": change["operation_id"],
    }
    result = await scheduling.accept(caller, command, "batch")
    row = await db.fetch_one(
        "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s",
        (result["batch_id"],),
    )
    assert row is not None
    assert row["current_execution_id"] == original["execution_id"]
    assert (await db.fetch_one("SELECT kind FROM swe_cron_execution_runs"))[
        "kind"
    ] == "ordinary"


@pytest.mark.asyncio
async def test_member_failure_rolls_back_receipt_and_enqueued_work(
    callback_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    original = db.execute

    async def fail_member_write(query, params=None):
        if query.startswith("INSERT INTO `swe_cron_managed_batch_members`"):
            raise RuntimeError("injected_relation_failure")
        return await original(query, params)

    monkeypatch.setattr(db, "execute", fail_member_write)
    service = ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(RuntimeError, match="injected_relation_failure"):
        await service.accept(caller, body(db, created, kind="batch"), "batch")
    assert not await db.fetch_all(
        "SELECT * FROM swe_cron_managed_trigger_receipts",
    )
    assert not await db.fetch_all("SELECT * FROM swe_cron_execution_runs")
