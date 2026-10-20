# -*- coding: utf-8 -*-
"""平台无查回接口时，无持久ID按注册失败处理，重试不重复已知绑定。"""

import json

import httpx
import pytest

from tests.unit.scheduler.test_managed_cron_management import (
    setup,
    create_payload,
)
from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
)
from tests.unit.scheduler.test_managed_cron_mode_recovery import batch_group


@pytest.mark.asyncio
async def test_interrupted_unconfirmed_registration_retries_without_lookup(
    setup,
):
    db, resources, platform, caller, management = setup
    platform.lose_register_response = True
    failed = await management.create(caller, create_payload())
    stored = db.conn.execute(
        "SELECT progress_json FROM swe_cron_registration_journal WHERE operation_id=?",
        (failed["operation_id"],),
    ).fetchone()
    progress = json.loads(stored[0])
    progress["registration_started"] = True
    db.conn.execute(
        "UPDATE swe_cron_registration_journal SET progress_json=? WHERE operation_id=?",
        (json.dumps(progress), failed["operation_id"]),
    )
    platform.lose_register_response = False
    done = await management.retry(
        caller,
        failed["job_id"],
        failed["operation_id"],
    )
    assert done["status"] == "completed"
    assert platform.calls.count("register") == 2
    assert "lookup" not in platform.calls
    assert len(resources.prepared) == 1


@pytest.mark.asyncio
async def test_deleting_unconfirmed_registration_does_not_require_platform_query(
    setup,
):
    _, resources, platform, caller, management = setup
    platform.lose_register_response = True
    failed = await management.create(caller, create_payload())
    deleted = await management.delete(
        caller,
        failed["job_id"],
        {"request_id": "delete-unconfirmed", "expected_definition_version": 1},
    )
    assert deleted["status"] == "completed"
    assert "lookup" not in platform.calls
    assert "stop" not in platform.calls
    assert len(resources.cleaned) == 1


@pytest.mark.asyncio
async def test_failed_batch_timer_registration_retries_original_mode_operation(
    callback_setup,
):
    from scheduler.app.services.cron.managed.dispatch_state import (
        DispatchStateService,
    )

    db, caller, management = callback_setup
    created = await create_source(callback_setup)
    modes = DispatchStateService(db, management.platform)
    accepted = await modes.set(
        caller,
        created["job_id"],
        {
            "request_id": "batch-with-lost-response",
            "state": "batch_enabled",
            "expected_version": 1,
        },
    )
    management.platform.lose_register_response = True
    await modes.process(accepted["operation_id"])
    failed = await modes.get(caller, created["job_id"])
    assert failed["sync_status"] == "failed"
    assert failed["error_code"] == "platform_registration_failed"
    management.platform.lose_register_response = False
    before = management.platform.calls.count("register")
    await modes.retry(caller, created["job_id"], accepted["operation_id"])
    assert (await modes.get(caller, created["job_id"]))[
        "sync_status"
    ] == "completed"
    assert management.platform.calls.count("register") == before + 1
    assert "lookup" not in management.platform.calls
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_real_platform_timeout_can_retry_add_without_any_query_endpoint(
    setup,
):
    from scheduler.app.services.cron.managed.management import (
        ManagementService,
    )
    from scheduler.app.services.cron.managed.platform import (
        ManagedPlatformClient,
    )

    db, resources, _, caller, _ = setup
    adds, requests = [], []

    async def response(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/add-job"):
            adds.append(True)
            if len(adds) == 1:
                raise httpx.ReadTimeout("lost registration response")
            return httpx.Response(200, json={"code": 200, "content": "222"})
        assert request.url.path.endswith("/update-job-run-states")
        assert json.loads(request.content)["id"] == 222
        return httpx.Response(200, json={"code": 200})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(response),
    ) as http:
        client = ManagedPlatformClient(
            {"base_url": "http://platform", "callback_base": "http://gateway"},
            http=http,
        )
        management = ManagementService(db, resources, client)
        failed = await management.create(caller, create_payload())
        assert failed["error_code"] == "platform_registration_failed"
        done = await management.retry(
            caller,
            failed["job_id"],
            failed["operation_id"],
        )
    assert done["status"] == "completed"
    assert len(adds) == 2
    assert set(requests) == {
        "/job-admin/v2/add-job",
        "/job-admin/v2/update-job-run-states",
    }
    assert (
        db.conn.execute(
            "SELECT ordinary_external_job_id FROM swe_cron_job_states",
        ).fetchone()[0]
        == "222"
    )
    assert len(resources.prepared) == 1
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_failed_ordinary_mode_registration_does_not_repeat_successful_objects(
    callback_setup,
):
    from scheduler.app.services.cron.managed.operations import (
        RegistrationUnknown,
    )

    _, caller, management = callback_setup
    modes, root, operation = await batch_group(callback_setup)
    original = management.platform.register
    root_attempts = []
    lost = True

    async def register(item, spec, mode):
        result = await original(item, spec, mode)
        if item["job_id"] == root["job_id"]:
            root_attempts.append(True)
            if lost:
                raise RegistrationUnknown()
        return result

    management.platform.register = register
    await modes.process(operation["operation_id"])
    failed = await modes.get(caller, root["job_id"])
    assert failed["sync_status"] == "failed"
    assert failed["failed_count"] == 1
    before = management.platform.calls.count("register")
    lost = False
    await modes.retry(caller, root["job_id"], operation["operation_id"])
    assert (await modes.get(caller, root["job_id"]))[
        "sync_status"
    ] == "completed"
    assert management.platform.calls.count("register") == before + 1
    assert len(root_attempts) == 2
    assert "lookup" not in management.platform.calls
