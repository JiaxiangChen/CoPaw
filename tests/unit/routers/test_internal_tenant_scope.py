# -*- coding: utf-8 -*-
"""Internal reload API source-scope regression tests."""

import base64
import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from swe.app.identity_resolver import ResolvedIdentity
from swe.app.crons.manager import CronManager
from swe.app.routers import internal as internal_router
from swe.app.routers.internal import router
from swe.app.workspace.tenant_pool import BootstrapOutcome
from swe.config.context import encode_scope_id


def _build_client(manager) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.multi_agent_manager = manager
    return TestClient(app)


class FakeAsyncTaskDb:
    """记录异步任务 SQL 调用的数据库替身。"""

    is_connected = True

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.executed_many: list[tuple[str, list[tuple]]] = []

    async def execute(self, sql: str, params: Any = None) -> int:
        self.executed.append((sql, params))
        return 1

    async def execute_many(self, sql: str, params_list: list[tuple]) -> int:
        self.executed_many.append((sql, params_list))
        return len(params_list)


def _enable_async_task_submission(
    client: TestClient,
    monkeypatch,
) -> FakeAsyncTaskDb:
    """让批量初始化接口只提交异步任务，不在测试中执行后台协程。"""
    db = FakeAsyncTaskDb()
    client.app.state.db_connection = db
    monkeypatch.setattr(
        internal_router.asyncio,
        "create_task",
        lambda coro: coro.close() or object(),
    )
    return db


def test_internal_reload_requires_source_id() -> None:
    manager = SimpleNamespace(reload_agent=AsyncMock(return_value=True))
    client = _build_client(manager)

    response = client.post(
        "/internal/agents/default/reload?tenant_id=tenant-a",
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "source_id is required"
    manager.reload_agent.assert_not_called()


def test_internal_reload_resolves_scope_id() -> None:
    manager = SimpleNamespace(reload_agent=AsyncMock(return_value=True))
    client = _build_client(manager)

    response = client.post(
        "/internal/agents/default/reload"
        "?tenant_id=tenant-a&source_id=source-a",
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"
    assert response.json()["scope_id"] == encode_scope_id(
        "tenant-a",
        "source-a",
    )
    manager.reload_agent.assert_awaited_once_with(
        "default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
    )


def test_internal_reload_rejects_invalid_source_id() -> None:
    manager = SimpleNamespace(reload_agent=AsyncMock(return_value=True))
    client = _build_client(manager)

    response = client.post(
        "/internal/agents/default/reload"
        "?tenant_id=tenant-a&source_id=../bad",
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid source_id"
    manager.reload_agent.assert_not_called()


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("dispatch_service", [False, True])
@pytest.mark.parametrize("deleted_after_lookup", [False, True])
def test_internal_cron_callback_skips_missing_job(
    encoded: bool,
    dispatch_service: bool,
    deleted_after_lookup: bool,
) -> None:
    job = SimpleNamespace(meta={}, task_type="agent")
    repo = SimpleNamespace(
        get_job=AsyncMock(
            side_effect=[job if deleted_after_lookup else None, None],
        ),
    )
    cron_manager = CronManager(
        repo=repo,
        runner=object(),
        channel_manager=object(),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    payload = {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "default",
        "task_type": "job",
        "job_id": "job-1",
    }
    if dispatch_service:
        payload.update(
            callback_source="dispatch_service",
            dispatch_intent_id=7,
            dispatch_batch_id="batch-1",
            dispatch_attempt=1,
        )
    if encoded:
        payload = {
            "jobParam": base64.urlsafe_b64encode(
                json.dumps(payload).encode(),
            ).decode(),
        }

    response = _build_client(manager).post(
        "/internal/cron/callback",
        json=payload,
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "skipped": "job_not_found",
        "job_id": "job-1",
    }


@pytest.mark.parametrize(
    "error",
    [
        KeyError("provider_id"),
        KeyError("Job not found: different-job"),
        RuntimeError("database unavailable"),
    ],
)
def test_internal_cron_callback_preserves_execution_errors(error) -> None:
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(side_effect=error),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )

    response = _build_client(manager).post(
        "/internal/cron/callback",
        json={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "task_type": "job",
            "job_id": "job-1",
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == str(error)


def test_internal_cron_callback_dispatches_job_param_tenant() -> None:
    cron_manager = SimpleNamespace(run_job=AsyncMock())
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    client = _build_client(manager)
    payload = {
        "tenant_id": "runtime-scope",
        "agent_id": "default",
        "task_type": "job",
        "job_id": "job-1",
    }
    job_param = base64.urlsafe_b64encode(
        json.dumps(payload).encode(),
    ).decode()

    response = client.post(
        "/internal/cron/callback",
        json={"jobParam": job_param},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "task_type": "job"}
    manager.get_agent.assert_awaited_once_with(
        "default",
        tenant_id="runtime-scope",
    )
    cron_manager.run_job.assert_awaited_once_with(
        "job-1",
        is_manual=False,
        source_id=None,
    )


@pytest.mark.parametrize("task_type", ["agent", "text", "workflow"])
def test_internal_cron_callback_forwards_scheduler_execution_identity(
    task_type: str,
) -> None:
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=SimpleNamespace(task_type=task_type)),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    client = _build_client(manager)
    job_param = base64.urlsafe_b64encode(
        json.dumps(
            {
                "tenant_id": "runtime-scope",
                "agent_id": "default",
                "task_type": "job",
                "job_id": "job-1",
            },
        ).encode(),
    ).decode()

    response = client.post(
        "/internal/cron/callback",
        json={
            "jobParam": job_param,
            "logId": "scheduler-log-1",
            "triggerTime": "2026-09-08T02:30:00Z",
        },
    )

    assert response.status_code == 200
    cron_manager.run_job.assert_awaited_once()
    args, kwargs = cron_manager.run_job.await_args
    assert args == ("job-1",)
    assert kwargs["is_manual"] is False
    assert kwargs["source_id"] is None
    metadata = kwargs["dispatch_meta"]
    assert metadata["scheduled_fire_at"] == "2026-09-08T02:30:00Z"
    assert metadata["external_execution_id"] == "scheduler-log-1"
    assert "cron_execution_key" not in metadata


@pytest.mark.parametrize("task_type", ["agent", "text", "workflow"])
def test_internal_cron_callback_generates_identity_for_legacy_scheduler(
    task_type: str,
) -> None:
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=SimpleNamespace(task_type=task_type)),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    client = _build_client(manager)

    response = client.post(
        "/internal/cron/callback",
        json={
            "tenant_id": "80074361",
            "source_id": "RMASSIST",
            "scopeId": "80074361-RMASSIST",
            "agent_id": "default",
            "task_type": "job",
            "job_id": "8819af9e-0bdf-4545-aa6c-7c3045a14ac2",
            "fromId": "80074361",
        },
    )

    assert response.status_code == 200
    cron_manager.run_job.assert_awaited_once()
    _, kwargs = cron_manager.run_job.await_args
    assert kwargs["is_manual"] is False
    assert kwargs["source_id"] == "RMASSIST"
    assert kwargs["dispatch_meta"]["cron_execution_key"].startswith(
        "legacy:",
    )


def test_internal_cron_callback_forwards_b3_headers_to_run_job() -> None:
    cron_manager = SimpleNamespace(run_job=AsyncMock())
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    client = _build_client(manager)

    response = client.post(
        "/internal/cron/callback",
        json={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "task_type": "job",
            "job_id": "job-1",
        },
        headers={
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "X-B3-Spanid": "32befd146889a61a",
            "X-B3-Parentspanid": "5be42cd2b570b6da",
            "X-B3-Sampled": "1",
            "X-B3-Debug": "0",
            "X-B3-BusinessId": "LQ1303LMES-WEB",
            "X-B3-Timestamp": "1782962021603",
        },
    )

    assert response.status_code == 200
    cron_manager.run_job.assert_awaited_once_with(
        "job-1",
        is_manual=False,
        source_id="source-a",
        dispatch_meta={
            "passthrough_headers": {
                "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
                "X-B3-Spanid": "32befd146889a61a",
                "X-B3-Parentspanid": "5be42cd2b570b6da",
                "X-B3-Sampled": "1",
                "X-B3-Debug": "0",
                "X-B3-BusinessId": "LQ1303LMES-WEB",
                "X-B3-Timestamp": "1782962021603",
            },
            "b3_trace_id": "8267fd70bacf497704fec30eaa353979",
        },
    )


def test_internal_cron_callback_skips_batch_parent_external_callback(
    monkeypatch,
) -> None:
    source_job = SimpleNamespace(
        meta={"broadcast_dispatch_intents_enabled": True},
    )
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=source_job),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    client = _build_client(manager)
    payload = {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "default",
        "task_type": "job",
        "job_id": "job-1",
    }
    job_param = base64.urlsafe_b64encode(
        json.dumps(payload).encode(),
    ).decode()

    response = client.post(
        "/internal/cron/callback",
        json={"jobParam": job_param},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "task_type": "job",
        "skipped": "batch_managed_external_callback",
    }
    cron_manager.run_job.assert_not_awaited()


def test_internal_cron_callback_skips_batch_parent_direct_external_body(
    monkeypatch,
) -> None:
    source_job = SimpleNamespace(
        meta={"broadcast_dispatch_intents_enabled": True},
        id="job-1",
        schedule=SimpleNamespace(cron="0 9 * * *", timezone="UTC"),
    )
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=source_job),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    client = _build_client(manager)
    body = {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "default",
        "task_type": "job",
        "job_id": "job-1",
        "logId": "scheduler-log-1",
    }

    response = client.post("/internal/cron/callback", json=body)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "task_type": "job",
        "skipped": "batch_managed_external_callback",
    }
    cron_manager.run_job.assert_not_awaited()


def test_internal_cron_callback_preserves_task_type_when_skipping_batch_parent(
    monkeypatch,
) -> None:
    source_job = SimpleNamespace(
        meta={"broadcast_dispatch_intents_enabled": True},
    )
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=source_job),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    client = _build_client(manager)
    response = client.post(
        "/internal/cron/callback",
        json={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "task_type": "legacy_job",
            "job_id": "job-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "task_type": "legacy_job",
        "skipped": "batch_managed_external_callback",
    }
    cron_manager.run_job.assert_not_awaited()


def test_internal_cron_callback_runs_flagged_parent_when_runtime_flag_off(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", raising=False)
    source_job = SimpleNamespace(
        meta={"broadcast_dispatch_intents_enabled": True},
    )
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=source_job),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    client = _build_client(manager)

    response = client.post(
        "/internal/cron/callback",
        json={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "task_type": "job",
            "job_id": "job-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "task_type": "job"}
    cron_manager.run_job.assert_awaited_once_with(
        "job-1",
        is_manual=False,
        source_id="source-a",
    )


def test_internal_cron_callback_skips_batch_managed_child_callback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    child_job = SimpleNamespace(
        meta={
            "broadcast_source_job_id": "parent-job",
            "broadcast_dispatch_intents_enabled": True,
        },
    )
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=child_job),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    client = _build_client(manager)
    payload = {
        "tenant_id": "tenant-b",
        "source_id": "source-a",
        "agent_id": "default",
        "task_type": "job",
        "job_id": "child-1",
    }
    job_param = base64.urlsafe_b64encode(
        json.dumps(payload).encode(),
    ).decode()

    response = client.post(
        "/internal/cron/callback",
        json={"jobParam": job_param},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "task_type": "job",
        "skipped": "batch_managed_child",
    }
    cron_manager.run_job.assert_not_awaited()


def test_dispatch_service_callback_runs_batch_managed_child(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    child_job = SimpleNamespace(
        meta={
            "broadcast_source_job_id": "parent-job",
            "broadcast_dispatch_intents_enabled": True,
        },
    )
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=child_job),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    client = _build_client(manager)

    response = client.post(
        "/internal/cron/callback",
        json={
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "agent_id": "default",
            "task_type": "job",
            "job_id": "child-1",
            "scopeId": "tenant-b-source-a",
            "fromId": "tenant-b",
            "callback_source": "dispatch_service",
            "dispatch_intent_id": 7,
            "dispatch_batch_id": "batch-1",
            "dispatch_attempt": 2,
            "execution_key": "child-1:batch-1:1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "task_type": "job"}
    cron_manager.run_job.assert_awaited_once_with(
        "child-1",
        is_manual=False,
        source_id="source-a",
        dispatch_meta={
            "source": "dispatch_service",
            "intent_id": 7,
            "batch_id": "batch-1",
            "dispatch_attempt": 2,
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": "tenant-b-source-a",
            "from_id": "tenant-b",
            "agent_id": "default",
            "job_id": "child-1",
            "parent_scheduled_fire_at": "",
            "provider_id": "default",
            "model_id": "default",
            "cron_execution_key": "child-1:batch-1:1",
        },
    )


def test_dispatch_service_callback_rejects_missing_dispatch_identity(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    child_job = SimpleNamespace(
        meta={
            "broadcast_source_job_id": "parent-job",
            "broadcast_dispatch_intents_enabled": True,
        },
    )
    cron_manager = SimpleNamespace(
        run_job=AsyncMock(),
        get_job=AsyncMock(return_value=child_job),
    )
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(cron_manager=cron_manager),
        ),
    )
    client = _build_client(manager)

    response = client.post(
        "/internal/cron/callback",
        json={
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "agent_id": "default",
            "task_type": "job",
            "job_id": "child-1",
            "callback_source": "dispatch_service",
        },
    )

    assert response.status_code == 400
    assert "dispatch_service callback requires" in response.json()["detail"]
    cron_manager.run_job.assert_not_awaited()


def test_internal_scope_encode_single_item() -> None:
    client = _build_client(SimpleNamespace())

    response = client.post(
        "/internal/scope/encode",
        json={"tenant_id": "tenant-a", "source_id": "source-a"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "item": {
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-a", "source-a"),
        },
    }


def test_internal_runtime_tenant_ids_exclude_template_dirs(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        internal_router,
        "list_all_tenant_ids",
        lambda: [
            "default",
            "default_ruice",
            encode_scope_id("tenant-a", "source-a"),
            "tenant-b",
        ],
    )

    assert internal_router._list_runtime_tenant_ids() == [
        "default",
        encode_scope_id("tenant-a", "source-a"),
        "tenant-b",
    ]


def test_internal_scope_encode_skips_internal_token_auth(
    monkeypatch,
) -> None:
    client = _build_client(SimpleNamespace())
    monkeypatch.setattr(internal_router, "_INTERNAL_TOKEN", "secret-token")

    response = client.post(
        "/internal/scope/encode",
        json={"tenant_id": "tenant-a", "source_id": "source-a"},
    )

    assert response.status_code == 200


def test_internal_scope_encode_batch_items() -> None:
    client = _build_client(SimpleNamespace())

    response = client.post(
        "/internal/scope/encode",
        json={
            "items": [
                {"tenant_id": "tenant-a", "source_id": "source-a"},
                {"tenant_id": "tenant-b", "source_id": "source-b"},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "items": [
            {
                "tenant_id": "tenant-a",
                "source_id": "source-a",
                "scope_id": encode_scope_id("tenant-a", "source-a"),
            },
            {
                "tenant_id": "tenant-b",
                "source_id": "source-b",
                "scope_id": encode_scope_id("tenant-b", "source-b"),
            },
        ],
    }


def test_internal_scope_encode_rejects_mixed_single_and_batch_input() -> None:
    client = _build_client(SimpleNamespace())

    response = client.post(
        "/internal/scope/encode",
        json={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "items": [
                {"tenant_id": "tenant-b", "source_id": "source-b"},
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Expected either tenant_id/source_id or items"
    )


def test_internal_scope_encode_rejects_empty_fields() -> None:
    client = _build_client(SimpleNamespace())

    response = client.post(
        "/internal/scope/encode",
        json={"tenant_id": "", "source_id": "source-a"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid tenant_id"


def test_internal_scope_encode_rejects_empty_batch() -> None:
    client = _build_client(SimpleNamespace())

    response = client.post(
        "/internal/scope/encode",
        json={"items": []},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "items must not be empty"


def test_internal_scope_decode_single_item() -> None:
    client = _build_client(SimpleNamespace())
    scope_id = encode_scope_id("tenant-a", "source-a")

    response = client.post(
        "/internal/scope/decode",
        json={"scope_id": scope_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "item": {
            "scope_id": scope_id,
            "tenant_id": "tenant-a",
            "source_id": "source-a",
        },
    }


def test_internal_scope_decode_skips_internal_token_auth(
    monkeypatch,
) -> None:
    client = _build_client(SimpleNamespace())
    scope_id = encode_scope_id("tenant-a", "source-a")
    monkeypatch.setattr(internal_router, "_INTERNAL_TOKEN", "secret-token")

    response = client.post(
        "/internal/scope/decode",
        json={"scope_id": scope_id},
    )

    assert response.status_code == 200


def test_internal_scope_decode_batch_items() -> None:
    client = _build_client(SimpleNamespace())
    scope_a = encode_scope_id("tenant-a", "source-a")
    scope_b = encode_scope_id("tenant-b", "source-b")

    response = client.post(
        "/internal/scope/decode",
        json={"scope_ids": [scope_a, scope_b]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "items": [
            {
                "scope_id": scope_a,
                "tenant_id": "tenant-a",
                "source_id": "source-a",
            },
            {
                "scope_id": scope_b,
                "tenant_id": "tenant-b",
                "source_id": "source-b",
            },
        ],
    }


def test_internal_scope_decode_rejects_legacy_scope() -> None:
    client = _build_client(SimpleNamespace())

    response = client.post(
        "/internal/scope/decode",
        json={"scope_id": "scope.v1.dGVuYW50LWE.c291cmNlLWE"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Legacy scope IDs are not supported"


def test_internal_scope_decode_rejects_malformed_scope() -> None:
    client = _build_client(SimpleNamespace())

    response = client.post(
        "/internal/scope/decode",
        json={"scope_id": "bad.scope.payload"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid scope_id payload"


def test_internal_batch_initialize_tenants(monkeypatch) -> None:
    pool = SimpleNamespace(ensure_bootstrap=AsyncMock())
    client = _build_client(SimpleNamespace())
    client.app.state.tenant_workspace_pool = pool
    db = _enable_async_task_submission(client, monkeypatch)

    async def fake_resolve_user_identity(**kwargs):
        tenant_id = kwargs["tenant_id"]
        return ResolvedIdentity(
            user_name=f"name-{tenant_id}",
            bbk_id=f"bbk-{tenant_id}",
        )

    monkeypatch.setattr(
        internal_router,
        "resolve_user_identity",
        fake_resolve_user_identity,
    )

    response = client.post(
        "/internal/tenants/batch-initialize",
        json={
            "tenant_ids": "111, 222,111",
            "source_id": "RMASSIST",
            "fail_fast": False,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "queued"
    assert data["task_id"]
    assert data["total"] == 2
    assert data["success_count"] == 0
    assert data["fail_count"] == 0
    assert "INSERT INTO swe_async_tasks" in db.executed[0][0]
    assert "INSERT INTO swe_async_task_items" in db.executed_many[0][0]
    assert [row[1] for row in db.executed_many[0][1]] == ["111", "222"]
    pool.ensure_bootstrap.assert_not_awaited()


def test_internal_batch_initialize_returns_async_task_when_db_available(
    monkeypatch,
) -> None:
    pool = SimpleNamespace(ensure_bootstrap=AsyncMock())
    client = _build_client(SimpleNamespace())
    client.app.state.tenant_workspace_pool = pool

    class FakeDb:
        is_connected = True

        async def execute(self, _sql, _params=None):
            return 1

        async def execute_many(self, _sql, params_list):
            return len(params_list)

    client.app.state.db_connection = FakeDb()
    monkeypatch.setattr(
        internal_router.asyncio,
        "create_task",
        lambda coro: coro.close() or object(),
    )

    response = client.post(
        "/internal/tenants/batch-initialize",
        json={
            "tenant_ids": "111,222",
            "source_id": "RMASSIST",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "queued"
    assert data["task_id"]
    assert data["total"] == 2
    assert data["success_count"] == 0
    assert data["fail_count"] == 0
    pool.ensure_bootstrap.assert_not_awaited()


def test_internal_batch_initialize_requires_async_task_db() -> None:
    """批量初始化必须提交异步任务，缺少任务库时返回明确错误。"""
    pool = SimpleNamespace(ensure_bootstrap=AsyncMock())
    client = _build_client(SimpleNamespace())
    client.app.state.tenant_workspace_pool = pool

    response = client.post(
        "/internal/tenants/batch-initialize",
        json={
            "tenant_ids": "111",
            "source_id": "RMASSIST",
        },
    )

    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "Async task database connection is not available"
    )
    pool.ensure_bootstrap.assert_not_awaited()


def test_internal_batch_initialize_lazy_loads_missing_app_db(monkeypatch):
    """app state 缺少数据库对象时应懒加载异步任务库。"""
    pool = SimpleNamespace(ensure_bootstrap=AsyncMock())
    client = _build_client(SimpleNamespace())
    client.app.state.tenant_workspace_pool = pool
    db = FakeAsyncTaskDb()

    async def fake_get_db(request):  # noqa: ANN001
        request.app.state.db_connection = db
        return db

    monkeypatch.setattr(
        internal_router,
        "get_or_create_async_task_db",
        fake_get_db,
    )
    monkeypatch.setattr(
        internal_router.asyncio,
        "create_task",
        lambda coro: coro.close() or object(),
    )

    response = client.post(
        "/internal/tenants/batch-initialize",
        json={
            "tenant_ids": "111",
            "source_id": "RMASSIST",
        },
    )

    assert response.status_code == 200
    assert response.json()["task_id"]
    assert client.app.state.db_connection is db
    assert "INSERT INTO swe_async_tasks" in db.executed[0][0]
    pool.ensure_bootstrap.assert_not_awaited()


def test_internal_batch_initialize_uses_async_task_db_without_connection_check(
    monkeypatch,
) -> None:
    """批量初始化提交任务时不预校验数据库连接状态。"""
    pool = SimpleNamespace(ensure_bootstrap=AsyncMock())
    client = _build_client(SimpleNamespace())
    client.app.state.tenant_workspace_pool = pool

    class FakeDb:
        is_connected = False

        async def execute(self, _sql, _params=None):
            return 1

        async def execute_many(self, _sql, params_list):
            return len(params_list)

    client.app.state.db_connection = FakeDb()
    monkeypatch.setattr(
        internal_router.asyncio,
        "create_task",
        lambda coro: coro.close() or object(),
    )

    response = client.post(
        "/internal/tenants/batch-initialize",
        json={
            "tenant_ids": "111",
            "source_id": "RMASSIST",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    pool.ensure_bootstrap.assert_not_awaited()


def test_internal_batch_initialize_can_disable_bootstrap_chat(
    monkeypatch,
) -> None:
    pool = SimpleNamespace(ensure_bootstrap=AsyncMock())
    client = _build_client(SimpleNamespace())
    client.app.state.tenant_workspace_pool = pool
    _enable_async_task_submission(client, monkeypatch)

    async def fake_resolve_user_identity(**kwargs):
        tenant_id = kwargs["tenant_id"]
        return ResolvedIdentity(
            user_name=f"name-{tenant_id}",
            bbk_id=f"bbk-{tenant_id}",
        )

    monkeypatch.setattr(
        internal_router,
        "resolve_user_identity",
        fake_resolve_user_identity,
    )

    response = client.post(
        "/internal/tenants/batch-initialize",
        json={
            "tenant_ids": "111",
            "source_id": "RMASSIST",
            "enable_bootstrap_chat": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    pool.ensure_bootstrap.assert_not_awaited()


def test_internal_batch_initialize_requires_identity_resolution(
    monkeypatch,
) -> None:
    pool = SimpleNamespace(ensure_bootstrap=AsyncMock())
    client = _build_client(SimpleNamespace())
    client.app.state.tenant_workspace_pool = pool
    _enable_async_task_submission(client, monkeypatch)

    async def fake_resolve_user_identity(**kwargs):
        del kwargs
        return ResolvedIdentity(user_name=None, bbk_id=None)

    monkeypatch.setattr(
        internal_router,
        "resolve_user_identity",
        fake_resolve_user_identity,
    )

    response = client.post(
        "/internal/tenants/batch-initialize",
        json={
            "tenant_ids": "111",
            "source_id": "RMASSIST",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    pool.ensure_bootstrap.assert_not_awaited()


@pytest.mark.parametrize(
    ("outcome_status", "expected_status"),
    [
        ("already_ready", "skipped"),
        ("bootstrapped", "created"),
    ],
)
def test_internal_batch_initialize_task_uses_bootstrap_outcome(
    monkeypatch,
    outcome_status,
    expected_status,
) -> None:
    """Batch item status must come from the pool bootstrap outcome."""

    class FakeStore:
        def __init__(self) -> None:
            self.item_results: list[dict[str, Any]] = []
            self.finished: dict[str, Any] | None = None

        async def mark_running(self, _task_id: str) -> None:
            return None

        async def record_item_result(self, **kwargs) -> None:
            self.item_results.append(kwargs)

        async def finish_task(self, **kwargs) -> None:
            self.finished = kwargs

    async def fake_resolve_user_identity(**kwargs):
        tenant_id = kwargs["tenant_id"]
        return ResolvedIdentity(
            user_name=f"name-{tenant_id}",
            bbk_id=f"bbk-{tenant_id}",
        )

    monkeypatch.setattr(
        internal_router,
        "resolve_user_identity",
        fake_resolve_user_identity,
    )

    store = FakeStore()
    pool = SimpleNamespace(
        ensure_bootstrap=AsyncMock(
            return_value=BootstrapOutcome(
                tenant_id="111",
                status=outcome_status,
                duration_ms=1,
            ),
        ),
    )
    payload = internal_router.InternalBatchInitializeTenantsRequest(
        tenant_ids="111",
        source_id="RMASSIST",
    )

    asyncio.run(
        internal_router._run_internal_batch_initialize_task(  # noqa: SLF001
            task_id="task-1",
            store=store,
            pool=pool,
            payload=payload,
            tenant_ids=["111"],
            headers={},
        ),
    )

    assert [item["item_status"] for item in store.item_results] == [
        expected_status,
    ]
    assert [item["result"]["status"] for item in store.item_results] == [
        expected_status,
    ]
    pool.ensure_bootstrap.assert_awaited_once()
    assert store.finished is not None
    assert store.finished["status"] == "succeeded"
    assert store.finished["done_count"] == 1


def test_internal_batch_initialize_rejects_empty_tenant_ids() -> None:
    client = _build_client(SimpleNamespace())

    response = client.post(
        "/internal/tenants/batch-initialize",
        json={
            "tenant_ids": " , ",
            "source_id": "RMASSIST",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "tenant_ids must not be empty"
