# -*- coding: utf-8 -*-
"""Application startup must not perform database schema initialization."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app import _app as app_module
from swe.app.approvals.store import ApprovalAuditStore
from swe.app.chat_sharing import router as chat_sharing_router
from swe.app.chat_sharing.store import ChatShareStore
from swe.app.crons.broadcast_children_store import CronBroadcastChildrenStore
from swe.app.crons.broadcast_task_store import CronBroadcastTaskStore
from swe.app.goals import registry as goal_registry
from swe.app.goals import router as goal_router
from swe.app.goals.store import MySqlGoalStore
from swe.app.skill_readiness.store import SkillReadinessStore
from swe.security import skill_scanner
from swe.security.skill_scanner.history import SkillScanHistoryStore


@pytest.mark.asyncio
async def test_startup_wires_database_stores_without_schema_initialization(
    monkeypatch,
) -> None:
    initializers = [
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    ]
    store_classes = [
        ApprovalAuditStore,
        SkillScanHistoryStore,
        CronBroadcastChildrenStore,
        CronBroadcastTaskStore,
        SkillReadinessStore,
    ]
    for store_class, initializer in zip(store_classes, initializers):
        monkeypatch.setattr(store_class, "initialize", initializer)

    db = SimpleNamespace(is_connected=True, execute=AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace())

    await app_module._initialize_approval_audit_store(app, db)
    await app_module._initialize_skill_scan_history(app, db)
    await app_module._initialize_cron_broadcast_children_store(app, db)
    await app_module._initialize_cron_broadcast_task_store(app, db)
    await app_module._initialize_skill_readiness(
        app,
        db,
        SimpleNamespace(),
    )

    for initializer in initializers:
        initializer.assert_not_awaited()
    db.execute.assert_not_awaited()

    await app.state.skill_scan_history_recorder.stop()
    skill_scanner.install_skill_scan_history_recorder(None)


@pytest.mark.asyncio
async def test_goal_service_startup_does_not_initialize_schema(
    monkeypatch,
) -> None:
    initializer = AsyncMock()
    monkeypatch.setattr(MySqlGoalStore, "initialize", initializer)
    monkeypatch.setattr(goal_registry, "_service", None)

    service = await goal_registry.initialize_goal_service(
        SimpleNamespace(is_connected=True),
    )

    assert service is not None
    initializer.assert_not_awaited()


@pytest.mark.asyncio
async def test_goal_router_fallback_does_not_initialize_schema(
    monkeypatch,
) -> None:
    initializer = AsyncMock()
    monkeypatch.setattr(MySqlGoalStore, "initialize", initializer)
    monkeypatch.setattr(goal_router, "get_goal_service", lambda: None)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db_connection=SimpleNamespace(is_connected=True),
            ),
        ),
    )

    service = await goal_router._service(request)

    assert service is not None
    initializer.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_sharing_startup_does_not_initialize_schema(
    monkeypatch,
) -> None:
    initializer = AsyncMock()
    monkeypatch.setattr(ChatShareStore, "ensure_schema", initializer)
    monkeypatch.setattr(chat_sharing_router, "_service", None)

    await chat_sharing_router.initialize_chat_sharing_module(
        SimpleNamespace(is_connected=True),
    )

    initializer.assert_not_awaited()
