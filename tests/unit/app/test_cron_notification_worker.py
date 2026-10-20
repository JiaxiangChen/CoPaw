# -*- coding: utf-8 -*-
"""定时任务通知 worker 测试。"""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from swe.app.crons.notification_worker import CronNotificationWorker


class _MonitorClient:
    def __init__(self) -> None:
        self.claim_payload: dict[str, Any] = {}

    async def claim_due_notifications(self, **kwargs):
        self.claim_payload = kwargs
        return []


@pytest.mark.asyncio
async def test_scan_once_passes_configured_source_ids(monkeypatch):
    """领取通知时必须携带当前实例允许处理的 source 范围。"""
    monkeypatch.setenv(
        "SWE_CRON_NOTIFICATION_SOURCE_IDS",
        "source-a, source-b\nsource-a",
    )
    monitor_client = _MonitorClient()
    worker = CronNotificationWorker(
        multi_agent_manager=object(),
        monitor_client=monitor_client,
        batch_size=5,
    )
    worker._now_utc = lambda: datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)

    await worker.scan_once()

    assert monitor_client.claim_payload["source_ids"] == [
        "source-a",
        "source-b",
    ]


def _notification_worker(send, count: int):
    rows = [
        {"id": i, "job_id": str(i), "tenant_id": "alice", "source_id": ""}
        for i in range(1, count + 1)
    ]
    monitor_client = SimpleNamespace(
        claim_due_notifications=AsyncMock(return_value=rows),
        mark_notification_sent=AsyncMock(),
        mark_notification_failed=AsyncMock(),
    )
    workspace = SimpleNamespace(
        cron_manager=SimpleNamespace(send_task_success_notification=send),
    )
    worker = CronNotificationWorker(
        multi_agent_manager=SimpleNamespace(
            get_agent=AsyncMock(return_value=workspace),
        ),
        monitor_client=monitor_client,
        batch_size=count,
        app_timezone="UTC",
    )
    return worker, monitor_client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "limit"),
    [(None, 5), ("2", 2), ("1", 1), ("invalid", 5), ("0", 1), ("999", 20)],
)
async def test_scan_sends_concurrently_with_bounded_slots(
    monkeypatch,
    configured,
    limit,
):
    """Concurrency is bounded, and a freed slot advances past slow peers."""
    if configured is None:
        monkeypatch.delenv("SWE_CRON_NOTIFICATION_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("SWE_CRON_NOTIFICATION_CONCURRENCY", configured)
    count = limit + 2
    releases = {str(i): asyncio.Event() for i in range(1, count + 1)}
    started = []
    initial_slots_full = asyncio.Event()
    replacement_started = asyncio.Event()
    active = 0
    peak = 0

    async def send(job_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(job_id)
        if len(started) == limit:
            initial_slots_full.set()
        if len(started) == limit + 1:
            replacement_started.set()
        try:
            await releases[job_id].wait()
        finally:
            active -= 1

    worker, client = _notification_worker(send, count)
    scan = asyncio.create_task(worker.scan_once())
    try:
        await asyncio.wait_for(initial_slots_full.wait(), timeout=1)
        await asyncio.sleep(0)
        assert len(started) == limit
        releases[started[-1]].set()
        await asyncio.wait_for(replacement_started.wait(), timeout=1)
        assert not scan.done()
        assert peak == limit
        for release in releases.values():
            release.set()
        await asyncio.wait_for(scan, timeout=1)
        assert peak == limit
        assert active == 0
        assert sorted(started, key=int) == [
            str(i) for i in range(1, count + 1)
        ]
        assert sorted(
            call.kwargs["execution_id"]
            for call in client.mark_notification_sent.await_args_list
        ) == list(range(1, count + 1))
        client.mark_notification_failed.assert_not_awaited()
    finally:
        scan.cancel()
        await asyncio.gather(scan, return_exceptions=True)


@pytest.mark.asyncio
async def test_status_writeback_holds_a_concurrency_slot(monkeypatch):
    """A slow Monitor acknowledgement must not escape the concurrency cap."""
    monkeypatch.setenv("SWE_CRON_NOTIFICATION_CONCURRENCY", "1")
    acknowledgement_started = asyncio.Event()
    release = asyncio.Event()
    send = AsyncMock()
    worker, client = _notification_worker(send, 2)

    async def mark_sent(**kwargs):
        if kwargs["execution_id"] == 1:
            acknowledgement_started.set()
            await release.wait()

    client.mark_notification_sent.side_effect = mark_sent
    scan = asyncio.create_task(worker.scan_once())
    try:
        await asyncio.wait_for(acknowledgement_started.wait(), timeout=1)
        await asyncio.sleep(0)
        send.assert_awaited_once_with("1")
        release.set()
        await asyncio.wait_for(scan, timeout=1)
        assert send.await_count == 2
    finally:
        scan.cancel()
        await asyncio.gather(scan, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_writeback_does_not_abort_other_notifications(
    monkeypatch,
    caplog,
):
    """失败回写异常不影响其余通知，并保留可捕获的错误日志。"""
    monkeypatch.setenv("SWE_CRON_NOTIFICATION_CONCURRENCY", "1")
    # 生产命名空间关闭传播，测试需显式接入 pytest 的根日志捕获器。
    monkeypatch.setattr(logging.getLogger("swe"), "propagate", True)

    async def send(job_id):
        if job_id == "1":
            raise RuntimeError("delivery failed")

    worker, client = _notification_worker(send, 3)
    client.mark_notification_failed.side_effect = RuntimeError(
        "Monitor unavailable",
    )
    await worker.scan_once()

    assert [
        call.kwargs["execution_id"]
        for call in client.mark_notification_sent.await_args_list
    ] == [2, 3]
    failed = client.mark_notification_failed.await_args.kwargs
    assert failed["execution_id"] == 1
    assert "delivery failed" in failed["error"]
    assert "execution_id=1" in caplog.text
    assert "Monitor unavailable" in caplog.text


@pytest.mark.asyncio
async def test_stop_settles_active_and_queued_notifications(monkeypatch):
    """Shutdown cancels in-flight work and never sends the waiting rows."""
    monkeypatch.setenv("SWE_CRON_NOTIFICATION_CONCURRENCY", "2")
    started = []
    cancelled = []
    slots_full = asyncio.Event()
    release = asyncio.Event()

    async def send(job_id):
        started.append(job_id)
        if len(started) == 2:
            slots_full.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            cancelled.append(job_id)
            raise

    worker, client = _notification_worker(send, 4)
    worker.start()
    try:
        await asyncio.wait_for(slots_full.wait(), timeout=1)
        await asyncio.wait_for(worker.stop(), timeout=1)
        assert started == ["1", "2"]
        assert sorted(cancelled) == ["1", "2"]
        client.mark_notification_sent.assert_not_awaited()
        client.mark_notification_failed.assert_not_awaited()
    finally:
        await worker.stop()


def _observe_idle_wait(worker, monkeypatch):
    waiting = asyncio.Event()
    wait_for_stop = worker._stopped.wait

    async def wait():
        waiting.set()
        await wait_for_stop()

    monkeypatch.setattr(worker._stopped, "wait", wait)
    return waiting


@pytest.mark.asyncio
async def test_run_loop_drains_batches_before_idle_wait(monkeypatch):
    """Refill after all acknowledgements, including after a partial batch."""
    worker, client = _notification_worker(AsyncMock(), 2)
    first_batch = client.claim_due_notifications.return_value
    client.claim_due_notifications.side_effect = [
        first_batch,
        [{"id": 3, "job_id": "3", "tenant_id": "alice", "source_id": ""}],
        [],
    ]
    acknowledgement_started = asyncio.Event()
    release = asyncio.Event()

    async def mark_sent(**kwargs):
        if kwargs["execution_id"] == 2:
            acknowledgement_started.set()
            await release.wait()

    client.mark_notification_sent.side_effect = mark_sent
    idle_wait = _observe_idle_wait(worker, monkeypatch)
    worker.start()
    try:
        await asyncio.wait_for(acknowledgement_started.wait(), timeout=1)
        assert client.claim_due_notifications.await_count == 1
        assert not idle_wait.is_set()
        release.set()
        await asyncio.wait_for(idle_wait.wait(), timeout=1)
        assert client.claim_due_notifications.await_count == 3
        assert sorted(
            call.kwargs["execution_id"]
            for call in client.mark_notification_sent.await_args_list
        ) == [1, 2, 3]
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_error", [False, True])
async def test_run_loop_waits_on_empty_scan_or_claim_error(
    monkeypatch,
    claim_error,
):
    """Empty or failed claims use the stop-aware interval without spinning."""
    send = AsyncMock()
    worker, client = _notification_worker(send, 1)
    if claim_error:
        client.claim_due_notifications.side_effect = RuntimeError(
            "Monitor unavailable",
        )
    else:
        client.claim_due_notifications.return_value = []
    idle_wait = _observe_idle_wait(worker, monkeypatch)
    worker.start()
    try:
        await asyncio.wait_for(idle_wait.wait(), timeout=1)
        assert client.claim_due_notifications.await_count == 1
        send.assert_not_awaited()
        assert not worker._task.done()
    finally:
        await asyncio.wait_for(worker.stop(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_writeback_error", [False, True])
async def test_run_loop_waits_after_notification_failure(
    monkeypatch,
    failed_writeback_error,
):
    """One failed notification preserves retry delay while peers finish."""

    async def send(job_id):
        if job_id == "1":
            raise RuntimeError("delivery unavailable")

    worker, client = _notification_worker(send, 2)
    if failed_writeback_error:
        client.mark_notification_failed.side_effect = RuntimeError(
            "Monitor unavailable",
        )
    idle_wait = _observe_idle_wait(worker, monkeypatch)
    worker.start()
    try:
        await asyncio.wait_for(idle_wait.wait(), timeout=1)
        assert client.claim_due_notifications.await_count == 1
        client.mark_notification_failed.assert_awaited_once()
        assert [
            call.kwargs["execution_id"]
            for call in client.mark_notification_sent.await_args_list
        ] == [2]
    finally:
        await worker.stop()
