# -*- coding: utf-8 -*-
"""Skill scan history application lifecycle tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe.app import _app as app_module
from swe.security import skill_scanner


@pytest.mark.asyncio
async def test_initialize_history_store_and_recorder_on_connected_database():
    db = SimpleNamespace(
        is_connected=True,
        execute=AsyncMock(return_value=1),
    )
    app = SimpleNamespace(state=SimpleNamespace())

    await app_module._initialize_skill_scan_history(app, db)

    assert app.state.skill_scan_history_store.is_available is True
    assert app.state.skill_scan_history_recorder is not None
    db.execute.assert_not_awaited()

    await app.state.skill_scan_history_recorder.stop()
    skill_scanner.install_skill_scan_history_recorder(None)


@pytest.mark.asyncio
async def test_initialize_history_without_database_stays_unavailable():
    app = SimpleNamespace(state=SimpleNamespace())

    await app_module._initialize_skill_scan_history(app, None)

    assert app.state.skill_scan_history_store.is_available is False
    assert app.state.skill_scan_history_recorder is None


def test_shutdown_drains_history_before_closing_database():
    import inspect

    source = inspect.getsource(app_module._shutdown_lifespan_resources_impl)

    recorder_stop = source.index("history_recorder.stop()")
    assert source.index("await _stop_multi_agent_manager(app)") < recorder_stop
    assert (
        source.index("await _stop_tenant_workspace_pool(app)") < recorder_stop
    )
    assert recorder_stop < source.index(
        "await db_connection.close()",
    )


@pytest.mark.asyncio
async def test_shutdown_still_drains_history_when_heartbeat_stop_fails(
    monkeypatch,
):
    events: list[str] = []

    async def fail_heartbeat() -> None:
        events.append("heartbeat")
        raise RuntimeError("heartbeat stop failed")

    async def stop_manager(_app) -> None:
        events.append("manager")

    async def stop_pool(_app) -> None:
        events.append("pool")

    recorder = SimpleNamespace(
        stop=AsyncMock(side_effect=lambda: events.append("recorder")),
    )
    database = SimpleNamespace(
        close=AsyncMock(side_effect=lambda: events.append("database")),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(skill_scan_history_recorder=recorder),
    )

    monkeypatch.setattr(app_module, "stop_service_heartbeat", fail_heartbeat)
    monkeypatch.setattr(app_module, "_stop_multi_agent_manager", stop_manager)
    monkeypatch.setattr(app_module, "_stop_tenant_workspace_pool", stop_pool)
    monkeypatch.setattr(app_module, "close_trace_manager", AsyncMock())
    monkeypatch.setattr(
        app_module.runtime_diagnostic_manager,
        "stop",
        AsyncMock(),
    )
    monkeypatch.setattr(
        app_module.runtime_diagnostic_manager,
        "set_workspace_metrics",
        MagicMock(),
    )
    monkeypatch.setattr(app_module, "shutdown_logger", MagicMock())

    await app_module._shutdown_lifespan_resources(app, database)

    assert events == [
        "heartbeat",
        "manager",
        "pool",
        "recorder",
        "database",
    ]


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_block_database_close(monkeypatch):
    stop_started = False

    async def stalled_stop() -> None:
        nonlocal stop_started
        stop_started = True
        await asyncio.Event().wait()

    recorder = SimpleNamespace(stop=stalled_stop)
    database = SimpleNamespace(close=AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(skill_scan_history_recorder=recorder),
    )

    monkeypatch.setattr(
        app_module,
        "_SKILL_SCAN_HISTORY_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(app_module, "stop_service_heartbeat", AsyncMock())
    monkeypatch.setattr(
        app_module,
        "_stop_multi_agent_manager",
        AsyncMock(),
    )
    monkeypatch.setattr(
        app_module,
        "_stop_tenant_workspace_pool",
        AsyncMock(),
    )
    monkeypatch.setattr(app_module, "close_trace_manager", AsyncMock())
    monkeypatch.setattr(
        app_module.runtime_diagnostic_manager,
        "stop",
        AsyncMock(),
    )
    monkeypatch.setattr(
        app_module.runtime_diagnostic_manager,
        "set_workspace_metrics",
        MagicMock(),
    )
    monkeypatch.setattr(app_module, "shutdown_logger", MagicMock())

    await app_module._shutdown_lifespan_resources(app, database)

    assert stop_started is True
    database.close.assert_awaited_once()
