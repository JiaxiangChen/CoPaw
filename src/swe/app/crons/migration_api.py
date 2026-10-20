# -*- coding: utf-8 -*-
"""Internal operator API for Cron snapshots, not daily task management."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path as ApiPath, Query, Request

from .migration_models import (
    MigrationRequest,
    MigrationResponse,
    MigrationTaskRequest,
    MigrationTaskStatus,
)
from .migration_repository import (
    MigrationRepository,
    MigrationTargetUnavailable,
)
from .migration_service import MigrationService
from .migration_source import MigrationSourceError
from .migration_tasks import (
    MigrationTaskCapacity,
    MigrationTaskNotFound,
    MigrationTaskRunner,
    MigrationTaskStore,
)

router = APIRouter(
    prefix="/internal/cron/migration", tags=["internal-cron-migration"]
)


def working_root() -> Path:
    from ...config import utils

    return Path(utils.WORKING_DIR)


def verify_internal_access(request: Request) -> None:
    """Reuse internal-token checks; retain the global login middleware."""
    from ..routers.internal import _verify_internal_token

    # When login and internal authentication coexist, JWT and internal token
    # are separate credentials. Never let Authorization mask X-Internal-Token.
    _verify_internal_token(
        request.headers.get("X-Internal-Token")
        or request.headers.get("Authorization")
    )


async def _perform(
    operation: Literal["preview", "import", "verify"],
    payload: MigrationRequest,
    request: Request,
) -> MigrationResponse:
    verify_internal_access(request)
    try:
        # Reuse the initialized shared connection. An operator request must
        # not load/log fresh credentials or create another connection pool.
        db = getattr(request.app.state, "db_connection", None)
        service = MigrationService(working_root(), MigrationRepository(db))
        return await service.run(
            operation,
            payload,
            getattr(request.app.state, "multi_agent_manager", None),
        )
    except MigrationSourceError as exc:
        status = 503 if str(exc) == "source_storage_unavailable" else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except MigrationTargetUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="migration_service_unavailable"
        ) from exc


@router.post("/preview", response_model=MigrationResponse)
async def preview_snapshot(
    payload: MigrationRequest, request: Request
) -> MigrationResponse:
    return await _perform("preview", payload, request)


@router.post("/import", response_model=MigrationResponse)
async def import_snapshot(
    payload: MigrationRequest, request: Request
) -> MigrationResponse:
    return await _perform("import", payload, request)


@router.post("/verify", response_model=MigrationResponse)
async def verify_snapshot(
    payload: MigrationRequest, request: Request
) -> MigrationResponse:
    return await _perform("verify", payload, request)


TaskId = Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")]


async def _task_store(request: Request) -> MigrationTaskStore:
    verify_internal_access(request)
    store = MigrationTaskStore(
        getattr(request.app.state, "db_connection", None)
    )
    try:
        await store.check_schema()
    except MigrationTargetUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return store


@router.post("/tasks", response_model=MigrationTaskStatus, status_code=202)
async def start_full_snapshot(
    payload: MigrationTaskRequest, request: Request
) -> MigrationTaskStatus:
    store = await _task_store(request)
    try:
        repository = MigrationRepository(store.db)
        await repository.check_schema()
        service = MigrationService(working_root(), repository)
        runner = getattr(request.app.state, "cron_migration_tasks", None)
        if runner is None:
            runner = MigrationTaskRunner(store)
            request.app.state.cron_migration_tasks = runner
        task_id = await runner.submit(
            payload,
            service,
            getattr(request.app.state, "multi_agent_manager", None),
        )
        return MigrationTaskStatus(
            task_id=task_id,
            status="queued",
            progress={
                "operation": payload.operation,
                "instance_id": service.instance_id,
            },
        )
    except MigrationTaskCapacity as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except MigrationTargetUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="migration_service_unavailable"
        ) from exc


@router.get("/tasks/{task_id}", response_model=MigrationTaskStatus)
async def get_full_snapshot(
    task_id: TaskId,
    request: Request,
    after_page: Annotated[int, Query(ge=0, le=10**12 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> MigrationTaskStatus:
    store = await _task_store(request)
    try:
        return await store.view(task_id, after_page=after_page, limit=limit)
    except MigrationTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="migration_progress_unavailable"
        ) from exc


@router.post("/tasks/{task_id}/stop", response_model=MigrationTaskStatus)
async def stop_full_snapshot(
    task_id: TaskId, request: Request
) -> MigrationTaskStatus:
    store = await _task_store(request)
    try:
        await store.stop(task_id)
        return await store.view(task_id)
    except MigrationTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="migration_progress_unavailable"
        ) from exc
