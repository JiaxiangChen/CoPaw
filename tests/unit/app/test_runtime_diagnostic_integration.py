# -*- coding: utf-8 -*-
"""Tests for Runtime Diagnostic application integration."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from swe.app.middleware.sse_diagnostic import SSEDiagnosticMiddleware
from swe.app.middleware.liveness_probe import LivenessProbeMiddleware
from swe.app.runtime_diagnostic import RuntimeDiagnosticManager


def test_app_exposes_runtime_diagnostic_manager() -> None:
    from swe.app._app import app, runtime_diagnostic_manager

    assert isinstance(runtime_diagnostic_manager, RuntimeDiagnosticManager)
    assert app.state.runtime_diagnostic_manager is runtime_diagnostic_manager


def test_app_installs_sse_diagnostic_middleware_with_shared_manager() -> None:
    from swe.app._app import app, runtime_diagnostic_manager

    middleware = next(
        item
        for item in app.user_middleware
        if item.cls is SSEDiagnosticMiddleware
    )

    assert middleware.kwargs["manager"] is runtime_diagnostic_manager


def test_app_installs_liveness_probe_as_outermost_middleware() -> None:
    from swe.app._app import app

    assert app.user_middleware[0].cls is LivenessProbeMiddleware


def test_lifespan_starts_and_stops_runtime_diagnostic_manager() -> None:
    from swe.app._app import (
        _shutdown_lifespan_resources_impl,
        _start_lifespan_background_services,
    )

    start_source = inspect.getsource(_start_lifespan_background_services)
    stop_source = inspect.getsource(_shutdown_lifespan_resources_impl)

    assert "await runtime_diagnostic_manager.start()" in start_source
    assert "await runtime_diagnostic_manager.stop()" in stop_source


def test_lifespan_does_not_schedule_batch_dispatch_startup_scan() -> None:
    from swe.app._app import _start_lifespan_background_services

    start_source = inspect.getsource(_start_lifespan_background_services)

    assert (
        "schedule_startup_dispatch_broadcast_children_processing"
        not in start_source
    )


def test_shutdown_stops_managed_background_processes() -> None:
    from swe.app._app import _shutdown_lifespan_resources_impl

    source = inspect.getsource(_shutdown_lifespan_resources_impl)

    assert "managed_background_process_manager.stop_all()" in source


@pytest.mark.asyncio
async def test_shutdown_releases_workspace_metrics_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swe.agents.tools import background_process
    from swe.app import _app as app_module

    class FakeRuntimeDiagnosticManager:
        def __init__(self) -> None:
            self.stop_calls = 0
            self.workspace_metric_callbacks: list[object] = []

        async def stop(self) -> None:
            self.stop_calls += 1

        def set_workspace_metrics(self, callback) -> None:
            self.workspace_metric_callbacks.append(callback)

    fake_runtime_diagnostic_manager = FakeRuntimeDiagnosticManager()
    monkeypatch.setattr(
        app_module,
        "runtime_diagnostic_manager",
        fake_runtime_diagnostic_manager,
    )
    monkeypatch.setattr(
        background_process.managed_background_process_manager,
        "stop_all",
        lambda: None,
    )
    monkeypatch.setattr(
        app_module,
        "close_trace_manager",
        _async_noop,
    )
    monkeypatch.setattr(
        app_module,
        "stop_service_heartbeat",
        _async_noop,
    )
    monkeypatch.setattr(
        app_module,
        "_stop_multi_agent_manager",
        lambda _app: _async_noop(),
    )
    monkeypatch.setattr(
        app_module,
        "_stop_tenant_workspace_pool",
        lambda _app: _async_noop(),
    )
    monkeypatch.setattr(app_module, "shutdown_logger", lambda: None)

    await app_module._shutdown_lifespan_resources(
        SimpleNamespace(state=SimpleNamespace(cron_notification_worker=None)),
        db_connection=None,
    )

    assert fake_runtime_diagnostic_manager.stop_calls == 1
    assert fake_runtime_diagnostic_manager.workspace_metric_callbacks == [
        None,
    ]


@pytest.mark.asyncio
async def test_shutdown_releases_workspace_metrics_when_diagnostic_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swe.agents.tools import background_process
    from swe.app import _app as app_module

    class BrokenRuntimeDiagnosticManager:
        def __init__(self) -> None:
            self.workspace_metric_callbacks: list[object] = []

        async def stop(self) -> None:
            raise RuntimeError("diagnostic stop failed")

        def set_workspace_metrics(self, callback) -> None:
            self.workspace_metric_callbacks.append(callback)

    broken_runtime_diagnostic_manager = BrokenRuntimeDiagnosticManager()
    monkeypatch.setattr(
        app_module,
        "runtime_diagnostic_manager",
        broken_runtime_diagnostic_manager,
    )
    monkeypatch.setattr(
        background_process.managed_background_process_manager,
        "stop_all",
        lambda: None,
    )
    monkeypatch.setattr(
        app_module,
        "close_trace_manager",
        _async_noop,
    )
    monkeypatch.setattr(
        app_module,
        "stop_service_heartbeat",
        _async_noop,
    )
    monkeypatch.setattr(
        app_module,
        "_stop_multi_agent_manager",
        lambda _app: _async_noop(),
    )
    monkeypatch.setattr(
        app_module,
        "_stop_tenant_workspace_pool",
        lambda _app: _async_noop(),
    )
    monkeypatch.setattr(app_module, "shutdown_logger", lambda: None)

    await app_module._shutdown_lifespan_resources(
        SimpleNamespace(state=SimpleNamespace(cron_notification_worker=None)),
        db_connection=None,
    )

    assert broken_runtime_diagnostic_manager.workspace_metric_callbacks == [
        None,
    ]


async def _async_noop() -> None:
    return None
