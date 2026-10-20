# -*- coding: utf-8 -*-
"""验证 HTTP 边界契约：新资源准备及先注册、后启用的平台顺序。"""

import base64
import json

import httpx
import pytest


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 0), ("", 0), ("  ", 0), ("2", 2)],
)
def test_platform_group_matches_existing_swe_empty_default(
    monkeypatch,
    raw,
    expected,
):
    from scheduler.app.services.cron.managed.platform import platform_config

    key = "SWE_CRON_SCHEDULER_JOB_GROUP"
    if raw is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, raw)
    assert platform_config()["job_group"] == expected


def test_platform_group_does_not_hide_invalid_non_numeric_configuration(
    monkeypatch,
):
    from scheduler.app.services.cron.managed.platform import platform_config

    monkeypatch.setenv("SWE_CRON_SCHEDULER_JOB_GROUP", "not-a-number")
    with pytest.raises(ValueError):
        platform_config()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403])
async def test_explicit_platform_auth_rejection_is_not_unknown_registration(
    status,
):
    from scheduler.app.services.cron.managed.platform import (
        ManagedPlatformClient,
    )
    from scheduler.app.services.cron.managed.operations import (
        RegistrationRejected,
    )

    async def rejected(_request):
        return httpx.Response(status, json={"code": 403})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(rejected),
    ) as http:
        client = ManagedPlatformClient(
            {
                "base_url": "http://platform",
                "callback_base": "https://gateway",
            },
            http=http,
        )
        operation = {
            "operation_id": "op",
            "job_id": "job",
            "tenant_id": "alice",
            "source_id": "bank",
            "agent_id": "default",
            "actor_id": "alice",
        }
        with pytest.raises(RegistrationRejected):
            await client.register(
                operation,
                {
                    "id": "job",
                    "name": "report",
                    "scope_id": "scope",
                    "schedule": {"cron": "0 9 * * *"},
                },
                "ordinary",
            )


@pytest.mark.asyncio
async def test_platform_register_does_not_enable_and_uses_new_callback():
    from scheduler.app.services.cron.managed.platform import (
        ManagedPlatformClient,
    )

    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "content": "123"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as http:
        platform = ManagedPlatformClient(
            {
                "base_url": "http://platform",
                "callback_base": "https://gateway",
                "client_no": "client",
                "client_key": "private",
                "job_group": 1,
            },
            http=http,
        )
        operation = {
            "operation_id": "op",
            "job_id": "job",
            "tenant_id": "alice",
            "source_id": "bank",
            "agent_id": "default",
            "actor_id": "alice",
        }
        spec = {
            "id": "job",
            "name": "report",
            "scope_id": "YWxpY2U.YmFuaw",
            "schedule": {"cron": "0 9 * * mon-fri"},
        }
        assert await platform.register(operation, spec, "ordinary") == "123"
        assert len(requests) == 1
        payload = json.loads(requests[0].content)
        assert (
            payload["jobAddress"]
            == "https://gateway/api/scheduler/cron/ordinary/callback"
        )
        assert payload["jobCron"] == "0 0 9 ? * 2,3,4,5,6"
        params = json.loads(base64.urlsafe_b64decode(payload["jobParam"]))
        assert params["callback_protocol"] == "scheduler_managed_v1"
        await platform.set_enabled("123", True)
        assert requests[-1].url.path == "/job-admin/v2/update-job-run-states"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,prefix",
    [
        ("ordinary", "[Scheduler]"),
        ("batch_enabled", "[Scheduler][批调度]"),
        ("batch_paused", "[Scheduler][批调度]"),
    ],
)
@pytest.mark.parametrize(
    "name",
    ["report", "长" * 200],
    ids=["short", "max-length"],
)
async def test_platform_registration_and_update_mark_batch_names_only(
    mode,
    prefix,
    name,
):
    from scheduler.app.services.cron.managed.platform import (
        ManagedPlatformClient,
    )

    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "content": "123"})

    operation = {
        "operation_id": "op",
        "job_id": "job",
        "tenant_id": "alice",
        "source_id": "bank",
        "agent_id": "default",
        "actor_id": "alice",
    }
    spec = {
        "id": "job",
        "name": name,
        "scope_id": "YWxpY2U.YmFuaw",
        "schedule": {"cron": "0 9 * * *"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as http:
        client = ManagedPlatformClient(
            {
                "base_url": "http://platform",
                "callback_base": "http://scheduler",
            },
            http=http,
        )
        await client.register(operation, spec, mode)
        await client.update("123", operation, spec, mode)
        await client.update("123", operation, spec, mode)
    payloads = [json.loads(request.content) for request in requests]
    assert all(
        payload["jobDesc"] == f"{prefix} alice/bank - {name}"[:200]
        for payload in payloads
    )
    assert all(len(payload["jobDesc"]) <= 200 for payload in payloads)
    entry = "ordinary" if mode == "ordinary" else "batch"
    assert all(
        payload["jobAddress"].endswith(f"/{entry}/callback")
        for payload in payloads
    )
    assert spec["name"] == name


@pytest.mark.asyncio
async def test_platform_register_timeout_is_reported_without_querying():
    from scheduler.app.services.cron.managed.platform import (
        ManagedPlatformClient,
    )
    from scheduler.app.services.cron.managed.management import (
        RegistrationUnknown,
    )

    requests = []

    async def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("private diagnostic")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout),
    ) as http:
        client = ManagedPlatformClient(
            {
                "base_url": "http://platform",
                "callback_base": "https://gateway",
            },
            http=http,
        )
        with pytest.raises(RegistrationUnknown):
            await client.register(
                {
                    "operation_id": "op",
                    "tenant_id": "alice",
                    "source_id": "bank",
                    "agent_id": "default",
                    "job_id": "job",
                    "actor_id": "alice",
                },
                {
                    "id": "job",
                    "name": "report",
                    "scope_id": "scope",
                    "schedule": {"cron": "0 9 * * *"},
                },
                "ordinary",
            )
    assert len(requests) == 1
    assert requests[0].url.path == "/job-admin/v2/add-job"


@pytest.mark.asyncio
async def test_swe_client_sends_operation_id_and_headers_not_authoritative_spec():
    from scheduler.app.services.cron.managed.swe_client import ManagedSweClient
    from scheduler.app.services.cron.managed.identity import CallerIdentity

    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "chat_id": "chat",
                "session_id": "session",
                "spec": {"id": "job"},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ) as http:
        client = ManagedSweClient("http://swe/api", http=http)
        await client.prepare(
            {"operation_id": "op", "job_id": "job"},
            CallerIdentity("alice", "bank", "default", "alice"),
        )
        assert requests[0].url.path == "/api/internal/cron/managed/prepare"
        assert json.loads(requests[0].content) == {
            "operation_id": "op",
            "job_id": "job",
        }
        assert requests[0].headers["X-Source-Id"] == "bank"
        assert requests[0].headers["X-Tenant-Id"] == "alice"
        assert "X-Internal-Token" not in requests[0].headers
