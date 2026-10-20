# -*- coding: utf-8 -*-
"""模拟旧 Python 的独立异步超时类型，验证循环、关闭和结果归类。"""

import asyncio
from types import SimpleNamespace

import pytest


class LegacyAsyncTimeoutError(Exception):
    """Python 3.10 的 asyncio.TimeoutError 不等同于内置 TimeoutError。"""


@pytest.mark.asyncio
async def test_recovery_lane_keeps_running_after_legacy_async_timeout(
    monkeypatch,
):
    from scheduler.app.services.cron.managed.recovery import (
        ManagedRecoveryWorker,
    )

    stop = asyncio.Event()
    calls = []

    async def lane():
        calls.append("tick")
        if len(calls) == 2:
            stop.set()

    async def wait_once(awaitable, *, timeout):
        assert timeout == 2
        if len(calls) == 1:
            awaitable.close()
            raise LegacyAsyncTimeoutError()
        return await awaitable

    monkeypatch.setattr(asyncio, "TimeoutError", LegacyAsyncTimeoutError)
    monkeypatch.setattr(asyncio, "wait_for", wait_once)
    worker = SimpleNamespace(_isolated=ManagedRecoveryWorker._isolated)
    await ManagedRecoveryWorker._run_lane(worker, lane, stop, 2)
    assert calls == ["tick", "tick"]


@pytest.mark.asyncio
async def test_shutdown_reaps_worker_after_legacy_async_timeout(monkeypatch):
    from scheduler.app.services.cron.managed.runtime import _stop

    task = asyncio.create_task(asyncio.Event().wait())
    stop = asyncio.Event()

    async def timeout_immediately(awaitable, *, timeout):
        assert timeout == 10
        awaitable.cancel()
        raise LegacyAsyncTimeoutError()

    monkeypatch.setattr(asyncio, "TimeoutError", LegacyAsyncTimeoutError)
    monkeypatch.setattr(asyncio, "wait_for", timeout_immediately)
    try:
        await _stop(task, stop)
        assert stop.is_set()
        assert task.cancelled(), "超时关闭必须收拢子任务，不能提前关闭数据库"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_execution_timeout_is_not_classified_as_generic_failure(monkeypatch):
    from swe.app.crons.managed_execution import _failure_outcome

    monkeypatch.setattr(asyncio, "TimeoutError", LegacyAsyncTimeoutError)
    result = _failure_outcome(LegacyAsyncTimeoutError())
    assert result.status == "error"
    assert result.error_code == "cron_execution_timeout"
