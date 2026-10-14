# -*- coding: utf-8 -*-
"""Scheduler cron dispatch intent service tests."""

import inspect
import json
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock

from scheduler.app.services.cron import dispatch_intent_service as service_module
from scheduler.app.routers import cron as scheduler_cron
from scheduler.app.services.cron.dispatch_intent_service import (
    CronDispatchIntentService,
    compute_batch_dispatch_order,
)
from scheduler.app.models.cron import ExecutionSyncRequest
from scheduler.app.services.cron import execution_sync_service as sync_module
from scheduler.app.services.cron.execution_sync_service import ExecutionSyncService
from scheduler.app.database import schema as scheduler_schema


def test_claim_due_intents_uses_skip_locked_and_stable_order() -> None:
    """Claiming due dispatch intents must be atomic and stable."""
    source = "\n".join(
        inspect.getsource(method)
        for method in (
            CronDispatchIntentService._fetch_candidate_batch_ids,
            CronDispatchIntentService._claimable_intent_ids_for_batch,
            CronDispatchIntentService._fetch_claimed_intents,
            CronDispatchIntentService._record_exhausted_dispatched_events,
        )
    )

    assert "FOR UPDATE SKIP LOCKED" in source
    assert "AND due_at <= %s" in source
    assert "ORDER BY next_dispatch_order, next_id" in source
    assert source.count("ORDER BY dispatch_order, id") == 2
    assert "ORDER BY due_at" not in source
    assert "status = 'pending'" in source
    assert "status IN ('claimed', 'acknowledged')" in source
    assert "attempt_count < max_attempts" in source
    assert "child_execution_missing_failed" in source


def test_claim_due_intents_acquires_batch_owner_before_intent_claim() -> None:
    """One scheduler worker must own a dispatch batch before claiming its intents."""
    source = inspect.getsource(
        CronDispatchIntentService._lock_candidate_batch,
    )

    assert "UPDATE swe_cron_dispatch_batches" in source
    assert "lock_owner = %s" in source
    assert "locked_at = %s" in source
    assert "WHERE batch_id = %s" in source


def test_dispatch_batch_schema_tracks_batch_owner() -> None:
    """Batch ownership needs durable columns so scheduler pods can coordinate."""
    source = scheduler_schema.CREATE_CRON_DISPATCH_BATCHES_TABLE

    assert "lock_owner VARCHAR(128)" in source
    assert "locked_at DATETIME" in source
    assert "idx_dispatch_batch_lock" in source


def test_scope_lease_schema_tracks_model_owner() -> None:
    """Model scope ownership needs a durable lease shared by scheduler pods."""
    source = scheduler_schema.CREATE_CRON_DISPATCH_SCOPE_LEASES_TABLE

    assert "swe_cron_dispatch_scope_leases" in source
    assert "PRIMARY KEY (source_id, provider_id, model_id)" in source
    assert "lock_owner VARCHAR(128)" in source
    assert "lease_expires_at DATETIME" in source
    assert "idx_scope_lease_owner" in source


def test_latest_capacity_is_scope_level_not_worker_level() -> None:
    """Capacity is shared per model scope; worker_id is only writer metadata."""
    source = inspect.getsource(
        CronDispatchIntentService.get_latest_worker_capacity,
    )

    assert "worker_id = %s" not in source
    assert "source_id = %s" in source
    assert "provider_id = %s" in source
    assert "model_id = %s" in source


def test_record_execution_uses_insert_cursor_lastrowid() -> None:
    source = inspect.getsource(ExecutionSyncService._insert_execution)

    assert "lastrowid" in source
    assert "LAST_INSERT_ID" not in source


def test_record_execution_persists_dispatch_identity_columns() -> None:
    source = inspect.getsource(ExecutionSyncService._insert_execution)

    assert "dispatch_intent_id" in source
    assert "dispatch_batch_id" in source
    assert "dispatch_attempt" in source


def test_find_execution_by_dispatch_identity_uses_indexed_columns() -> None:
    source = inspect.getsource(
        ExecutionSyncService.find_execution_by_dispatch_identity,
    )

    assert "dispatch_intent_id = %s" in source
    assert "dispatch_batch_id = %s" in source
    assert "dispatch_attempt = %s" in source
    assert "JSON_EXTRACT" not in source


@pytest.mark.asyncio
async def test_record_execution_writes_dispatch_identity_values(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _Cursor:
        lastrowid = 42

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

    class _Connection:
        def cursor(self):
            return _Cursor()

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return False

    class _Db:
        def acquire(self):
            return _Acquire()

    monkeypatch.setattr(
        sync_module,
        "get_db_connection",
        lambda: _Db(),
    )

    request = ExecutionSyncRequest(
        job_id="child-1",
        job_name="Child",
        tenant_id="tenant-a",
        actual_time=datetime(2026, 7, 1, 10, 0),
        status="success",
        meta=json.dumps(
            {
                "cron_dispatch": {
                    "intent_id": 7,
                    "batch_id": "batch-1",
                    "dispatch_attempt": 2,
                },
            },
        ),
    )

    execution_id = await ExecutionSyncService().record_execution(request)

    assert execution_id == 42
    assert "dispatch_intent_id" in str(captured["sql"])
    assert "dispatch_batch_id" in str(captured["sql"])
    assert "dispatch_attempt" in str(captured["sql"])
    assert captured["params"][17:20] == (7, "batch-1", 2)


@pytest.mark.asyncio
async def test_find_execution_by_dispatch_identity_queries_indexed_columns(
    monkeypatch,
) -> None:
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value={"id": "42"})
    monkeypatch.setattr(
        sync_module,
        "get_db_connection",
        lambda: db,
    )

    execution_id = await ExecutionSyncService().find_execution_by_dispatch_identity(
        intent_id=7,
        batch_id="batch-1",
        dispatch_attempt=2,
    )

    sql, params = db.fetch_one.await_args.args
    assert execution_id == 42
    assert "JSON_EXTRACT" not in sql
    assert "dispatch_intent_id = %s" in sql
    assert params == (7, "batch-1", 2)


def test_enqueue_child_intents_reads_viewer_heat_from_execution_reads() -> None:
    """Child priority should be derived from existing read execution signals."""
    source = "\n".join(
        inspect.getsource(fn)
        for fn in (
            service_module._fetch_viewer_heat_scores,
            service_module._viewer_heat_parent_filter,
        )
    )
    fast_read_clause = service_module._viewer_fast_read_select_clause()

    assert "swe_cron_executions" in source
    assert "is_read = TRUE" in source
    assert "read_at" in source
    assert "TIMESTAMPDIFF(" in fast_read_clause
    assert "SECOND" in fast_read_clause
    assert "fast_read_2_hour_count" in fast_read_clause
    assert "fast_read_3_hour_count" in fast_read_clause
    assert "fast_read_4_hour_count" in fast_read_clause
    assert "fast_read_5_hour_count" in fast_read_clause
    assert "broadcast_source_job_id" in source


def test_viewer_heat_score_includes_fast_read_bonus() -> None:
    """Fast reads should increase dispatch priority without adding columns."""
    score = service_module._viewer_heat_score_from_row(
        {
            "read_count": 3,
            "fast_read_2_hour_count": 2,
            "fast_read_3_hour_count": 3,
            "fast_read_4_hour_count": 4,
            "fast_read_5_hour_count": 5,
        },
    )

    assert score == Decimal("17")


def test_batch_dispatch_order_has_no_waiting_aging() -> None:
    """Waiting longer must not reshuffle an already ordered batch."""
    rows = [
        {
            "job_id": "job-c",
            "tenant_id": "tenant-c",
            "viewer_heat_score": 1.0,
        },
        {
            "job_id": "job-a",
            "tenant_id": "tenant-a",
            "viewer_heat_score": 4.0,
        },
        {
            "job_id": "job-b",
            "tenant_id": "tenant-b",
            "viewer_heat_score": 4.0,
        },
    ]

    ordered = compute_batch_dispatch_order(rows)

    assert [item["job_id"] for item in ordered] == [
        "job-a",
        "job-b",
        "job-c",
    ]
    assert [item["dispatch_order"] for item in ordered] == [0, 1, 2]


def test_scheduler_router_exposes_dispatch_execution_endpoint() -> None:
    """Dispatch intent feedback APIs belong to Scheduler."""
    source = inspect.getsource(scheduler_cron)

    assert '"/execution"' in source
    assert "record_dispatch_execution" in source


def test_enqueue_intents_preserves_handed_off_rows() -> None:
    """Duplicate enqueue must not reset callback-dispatched rows to pending."""
    parent_source = inspect.getsource(
        CronDispatchIntentService.enqueue_parent_intent,
    )
    child_source = inspect.getsource(
        CronDispatchIntentService.enqueue_child_intents,
    )

    expected = "status IN ('claimed', 'acknowledged', 'dispatched', 'completed')"
    assert expected in parent_source
    assert expected in child_source


def test_batch_enqueue_preserves_terminal_intents() -> None:
    """Duplicate parent callbacks must not reopen terminal intents."""
    source = inspect.getsource(
        CronDispatchIntentService.enqueue_batch_execution_intents,
    )

    duplicate_update = source.split("ON DUPLICATE KEY UPDATE", 1)[1].split(
        '"""', 1
    )[0]
    assert duplicate_update.strip() == "id = LAST_INSERT_ID(id)"
    claim_source = inspect.getsource(
        CronDispatchIntentService._claimable_intent_ids_for_batch,
    )
    assert "(status = 'pending' AND attempt_count < max_attempts)" in (
        claim_source
    )


@pytest.mark.asyncio
async def test_unknown_callback_outcome_keeps_intent_dispatched() -> None:
    service = CronDispatchIntentService()
    service._transition_intent = AsyncMock(return_value=True)
    observed_at = datetime(2026, 7, 1, 10, 0)
    details = {
        "callback_outcome": "unknown",
        "error_type": "ReadTimeout",
    }

    updated = await service.mark_intent_dispatch_unknown(
        intent_id=7,
        worker_id="scheduler-1",
        observed_at=observed_at,
        details=details,
    )

    assert updated is True
    service._transition_intent.assert_awaited_once_with(
        intent_id=7,
        worker_id="scheduler-1",
        status="dispatched",
        timestamp_column="updated_at",
        timestamp=observed_at,
        event_type="callback_outcome_unknown",
        details=details,
    )


@pytest.mark.asyncio
async def test_stale_dispatch_requeues_retryable_and_fails_exhausted(
    monkeypatch,
) -> None:
    executed: list[tuple[str, tuple]] = []
    stale_rows = [
        {
            "id": 7,
            "batch_id": "batch-1",
            "job_id": "child-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "attempt_count": 1,
            "max_attempts": 3,
        },
        {
            "id": 8,
            "batch_id": "batch-1",
            "job_id": "child-2",
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "attempt_count": 3,
            "max_attempts": 3,
        },
    ]

    class _Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql, params):
            executed.append((" ".join(str(sql).split()), tuple(params)))

        async def fetchall(self):
            return (
                []
                if executed[-1][0].startswith("SELECT id,batch_id")
                else stale_rows
            )

    class _Connection:
        async def begin(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

        def cursor(self):
            return _Cursor()

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return False

    class _Db:
        def acquire(self):
            return _Acquire()

    monkeypatch.setattr(
        service_module,
        "get_db_connection",
        lambda: _Db(),
    )
    service = CronDispatchIntentService()
    service._record_event_best_effort = AsyncMock()
    service._refresh_batch_counts_for_rows = AsyncMock()

    recovered = await service.recover_stale_dispatched_intents(
        now_utc=datetime(2026, 7, 1, 10, 0),
    )

    assert recovered == 2
    update_sql = " ".join(sql for sql, _params in executed[1:])
    assert "SET status = 'pending'" in update_sql
    assert "SET status = 'failed'" in update_sql
    event_types = [
        call.kwargs["event_type"]
        for call in service._record_event_best_effort.await_args_list
    ]
    assert event_types == [
        "stale_dispatch_requeued",
        "child_execution_missing_failed",
    ]


@pytest.mark.asyncio
async def test_upsert_dispatch_batch_writes_provider_model(monkeypatch) -> None:
    captured: list[tuple[str, tuple]] = []

    class _Db:
        async def execute(self, sql, params):
            captured.append((str(sql), tuple(params)))

    monkeypatch.setattr(
        service_module,
        "get_db_connection",
        lambda: _Db(),
    )

    await CronDispatchIntentService().upsert_dispatch_batch(
        batch_id="batch-1",
        parent_job_id="parent-1",
        parent_external_job_id="external-1",
        tenant_id="tenant-a",
        source_id="source-a",
        agent_id="default",
        provider_id="dashscope",
        model_id="qwen-max",
        scheduled_fire_at=datetime(2026, 7, 1, 10, 0),
        callback_received_at=datetime(2026, 7, 1, 10, 1),
        callback_metadata={"job_id": "parent-1"},
    )

    sql, params = captured[0]
    assert "provider_id" in sql
    assert "model_id" in sql
    assert "provider_id = VALUES(provider_id)" in sql
    assert "model_id = VALUES(model_id)" in sql
    assert params[5:7] == ("dashscope", "qwen-max")


@pytest.mark.asyncio
async def test_enqueue_child_intents_uses_json_safe_default_payload(
    monkeypatch,
) -> None:
    """Default child payload must not include datetime or Decimal values."""
    db = MagicMock()
    db.fetch_all = AsyncMock(
        return_value=[{"job_id": "child-a", "read_count": 3}],
    )
    monkeypatch.setattr(
        service_module,
        "get_db_connection",
        lambda: db,
    )
    captured_payloads: list[dict] = []

    async def _fake_execute_write_return_last_id(_sql, params):
        captured_payloads.append(json.loads(params[-1]))
        return len(captured_payloads)

    monkeypatch.setattr(
        service_module,
        "_execute_write_return_last_id",
        _fake_execute_write_return_last_id,
    )
    service = CronDispatchIntentService()
    service._record_event = AsyncMock()

    intent_ids = await service.enqueue_child_intents(
        parent_intent_id=1,
        batch_id="batch-1",
        parent_job_id="parent-job",
        source_id="source-a",
        child_jobs=[
            {
                "tenant_id": "tenant-a",
                "job_id": "child-a",
                "due_at": datetime(2026, 6, 30, 10, 0),
            },
        ],
    )

    assert intent_ids == [1]
    assert captured_payloads == [
        {
            "tenant_id": "tenant-a",
            "job_id": "child-a",
            "source_id": "",
            "agent_id": "default",
        },
    ]


@pytest.mark.asyncio
async def test_enqueue_child_intents_prefers_child_source_id(monkeypatch) -> None:
    """Child intents must keep their own source id for runtime routing."""
    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(
        service_module,
        "get_db_connection",
        lambda: db,
    )
    captured_source_ids: list[str] = []

    async def _fake_execute_write_return_last_id(_sql, params):
        captured_source_ids.append(params[1])
        return len(captured_source_ids)

    monkeypatch.setattr(
        service_module,
        "_execute_write_return_last_id",
        _fake_execute_write_return_last_id,
    )
    service = CronDispatchIntentService()
    service._record_event = AsyncMock()

    await service.enqueue_child_intents(
        parent_intent_id=1,
        batch_id="batch-1",
        parent_job_id="parent-job",
        source_id="parent-source",
        child_jobs=[
            {
                "tenant_id": "tenant-a",
                "job_id": "child-a",
                "source_id": "child-source",
            },
        ],
    )

    assert captured_source_ids == ["child-source"]


@pytest.mark.asyncio
async def test_transition_conflict_does_not_record_event(monkeypatch) -> None:
    """A stale worker must not emit success events after losing the lock."""
    db = MagicMock()
    db.fetch_one = AsyncMock(
        return_value={
            "batch_id": "batch-1",
            "job_id": "job-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
        },
    )
    db.execute = AsyncMock(return_value=MagicMock(rowcount=0))
    monkeypatch.setattr(
        service_module,
        "get_db_connection",
        lambda: db,
    )
    service = CronDispatchIntentService()
    service._record_event = AsyncMock()

    await service.complete_intent(
        intent_id=1,
        worker_id="stale-worker",
        completed_at=datetime(2026, 6, 30, 10, 0),
    )

    assert service._record_event.await_count == 0
    sql, params = db.execute.await_args.args
    assert "lock_owner = %s" in sql
    assert "stale-worker" in params


@pytest.mark.asyncio
async def test_complete_from_execution_updates_matching_intent(monkeypatch) -> None:
    """Execution feedback must complete only the matching dispatch intent."""
    db = MagicMock()
    db.fetch_one = AsyncMock(
        return_value={
            "batch_id": "batch-1",
            "job_id": "child-1",
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "attempt_count": 1,
            "max_attempts": 3,
            "current_status": "dispatched",
        },
    )
    db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    monkeypatch.setattr(
        service_module,
        "get_db_connection",
        lambda: db,
    )
    service = CronDispatchIntentService()
    service._record_event = AsyncMock()

    updated = await service.complete_from_execution(
        intent_id=7,
        execution_id=42,
        status="success",
        completed_at=datetime(2026, 7, 1, 10, 0),
        expected_batch_id="batch-1",
        expected_job_id="child-1",
        expected_tenant_id="tenant-b",
        expected_source_id="source-a",
        expected_attempt_count=1,
    )

    assert updated is True
    db.execute.assert_awaited_once()
    assert "AND attempt_count = %s" in db.execute.await_args.args[0]
    assert db.execute.await_args.args[1][-1] == 1
    service._record_event.assert_awaited_once()
    assert service._record_event.await_args.kwargs["event_type"] == (
        "child_execution_completed"
    )


@pytest.mark.asyncio
async def test_complete_from_execution_rejects_mismatched_intent(
    monkeypatch,
) -> None:
    """A stale or wrong intent id must not update another job's row."""
    db = MagicMock()
    db.fetch_one = AsyncMock(
        return_value={
            "batch_id": "batch-1",
            "job_id": "child-1",
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "attempt_count": 1,
            "max_attempts": 3,
            "current_status": "dispatched",
        },
    )
    db.execute = AsyncMock()
    monkeypatch.setattr(
        service_module,
        "get_db_connection",
        lambda: db,
    )
    service = CronDispatchIntentService()
    service._record_event = AsyncMock()

    updated = await service.complete_from_execution(
        intent_id=7,
        execution_id=42,
        status="success",
        completed_at=datetime(2026, 7, 1, 10, 0),
        expected_batch_id="batch-1",
        expected_job_id="other-child",
        expected_tenant_id="tenant-b",
        expected_source_id="source-a",
    )

    assert updated is False
    db.execute.assert_not_awaited()
    service._record_event.assert_awaited_once()
    assert service._record_event.await_args.kwargs["event_type"] == (
        "execution_intent_mismatch"
    )


@pytest.mark.asyncio
async def test_complete_from_execution_rejects_stale_attempt_feedback(
    monkeypatch,
) -> None:
    """Late execution feedback from a stale attempt must not finish a retry."""
    db = MagicMock()
    db.fetch_one = AsyncMock(
        return_value={
            "batch_id": "batch-1",
            "job_id": "child-1",
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "attempt_count": 2,
            "max_attempts": 3,
            "current_status": "dispatched",
        },
    )
    db.execute = AsyncMock()
    monkeypatch.setattr(
        service_module,
        "get_db_connection",
        lambda: db,
    )
    service = CronDispatchIntentService()
    service._record_event = AsyncMock()

    updated = await service.complete_from_execution(
        intent_id=7,
        execution_id=42,
        status="success",
        completed_at=datetime(2026, 7, 1, 10, 0),
        expected_batch_id="batch-1",
        expected_job_id="child-1",
        expected_tenant_id="tenant-b",
        expected_source_id="source-a",
        expected_attempt_count=1,
    )

    assert updated is False
    db.execute.assert_not_awaited()
    service._record_event.assert_awaited_once()
    assert service._record_event.await_args.kwargs["event_type"] == (
        "execution_attempt_mismatch"
    )


@pytest.mark.asyncio
async def test_sync_execution_with_dispatch_meta_is_durable() -> None:
    """Scheduler dispatch execution sync should await DB write and intent update."""
    sync_service = MagicMock()
    sync_service.find_execution_by_dispatch_identity = AsyncMock(return_value=None)
    sync_service.record_dispatch_execution = AsyncMock(return_value=42)
    scheduling_service = MagicMock()
    scheduling_service.handle_execution_recorded = AsyncMock(return_value=True)
    request = ExecutionSyncRequest(
        job_id="child-1",
        job_name="Child",
        tenant_id="tenant-b",
        source_id="source-a",
        actual_time=datetime(2026, 7, 1, 10, 0),
        status="success",
        meta=json.dumps(
            {
                "cron_dispatch": {
                    "intent_id": 7,
                    "batch_id": "batch-1",
                    "dispatch_attempt": 1,
                },
            },
        ),
    )

    assert request.source_id == "source-a"

    response = await scheduler_cron.record_dispatch_execution(
        request,
        sync_service=sync_service,
        scheduling_service=scheduling_service,
    )

    assert response.recorded is True
    assert response.execution_id == 42
    sync_service.find_execution_by_dispatch_identity.assert_not_awaited()
    sync_service.record_dispatch_execution.assert_awaited_once_with(request)
    scheduling_service.handle_execution_recorded.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_execution_without_dispatch_meta_is_ignored_not_400() -> None:
    """Scheduler execution endpoint is open to non-dispatch calls but ignores them."""
    sync_service = MagicMock()
    sync_service.find_execution_by_dispatch_identity = AsyncMock()
    sync_service.record_dispatch_execution = AsyncMock()
    scheduling_service = MagicMock()
    scheduling_service.handle_execution_recorded = AsyncMock()
    request = ExecutionSyncRequest(
        job_id="child-1",
        job_name="Child",
        tenant_id="tenant-b",
        source_id="source-a",
        actual_time=datetime(2026, 7, 1, 10, 0),
        status="success",
        meta="",
    )

    response = await scheduler_cron.record_dispatch_execution(
        request,
        sync_service=sync_service,
        scheduling_service=scheduling_service,
    )

    assert response.recorded is False
    assert response.execution_id is None
    sync_service.find_execution_by_dispatch_identity.assert_not_awaited()
    sync_service.record_dispatch_execution.assert_not_awaited()
    scheduling_service.handle_execution_recorded.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_execution_with_dispatch_meta_reuses_existing_execution(
) -> None:
    """Scheduler feedback retry must not duplicate execution rows."""
    sync_service = MagicMock()
    sync_service.find_execution_by_dispatch_identity = AsyncMock(return_value=42)
    sync_service.record_dispatch_execution = AsyncMock(return_value=42)
    scheduling_service = MagicMock()
    scheduling_service.handle_execution_recorded = AsyncMock(return_value=True)
    request = ExecutionSyncRequest(
        job_id="child-1",
        job_name="Child",
        tenant_id="tenant-b",
        source_id="source-a",
        actual_time=datetime(2026, 7, 1, 10, 0),
        status="success",
        meta=json.dumps(
            {
                "cron_dispatch": {
                    "intent_id": 7,
                    "batch_id": "batch-1",
                    "dispatch_attempt": 1,
                },
            },
        ),
    )

    response = await scheduler_cron.record_dispatch_execution(
        request,
        sync_service=sync_service,
        scheduling_service=scheduling_service,
    )

    assert response.recorded is True
    assert response.execution_id == 42
    sync_service.record_dispatch_execution.assert_awaited_once_with(request)
    sync_service.find_execution_by_dispatch_identity.assert_not_awaited()
    scheduling_service.handle_execution_recorded.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_execution_sync_fails_when_intent_update_fails(
) -> None:
    """Dispatch-meta execution sync success requires the intent update too."""
    sync_service = MagicMock()
    sync_service.find_execution_by_dispatch_identity = AsyncMock(return_value=None)
    sync_service.record_dispatch_execution = AsyncMock(return_value=42)
    scheduling_service = MagicMock()
    scheduling_service.handle_execution_recorded = AsyncMock(return_value=False)
    request = ExecutionSyncRequest(
        job_id="child-1",
        job_name="Child",
        tenant_id="tenant-b",
        source_id="source-a",
        actual_time=datetime(2026, 7, 1, 10, 0),
        status="success",
        meta=json.dumps(
            {
                "cron_dispatch": {
                    "intent_id": 7,
                    "batch_id": "batch-1",
                },
            },
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await scheduler_cron.record_dispatch_execution(
            request,
            sync_service=sync_service,
            scheduling_service=scheduling_service,
        )
    assert exc_info.value.status_code == 500
    assert "dispatch intent was not updated" in str(exc_info.value.detail)
