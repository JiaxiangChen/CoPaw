# -*- coding: utf-8 -*-
"""Internal asynchronous operator API for credential snapshots."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path as ApiPath, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from .auth_migration_models import (
    AuthMigrationRequest,
    AuthMigrationTaskStatus,
)
from .auth_migration_progress import (
    AuthMigrationTaskNotFound,
    AuthMigrationTaskStore,
)
from .auth_migration_service import AuthMigrationService
from .auth_migration_tasks import (
    AuthMigrationTaskCapacity,
    AuthMigrationTaskRunner,
)
from .auth_repository import CronAuthRepository, CronAuthRepositoryUnavailable
from .migration_api import verify_internal_access, working_root


class SafeValidationRoute(APIRoute):
    """Pydantic input values may be secrets; return only a fixed safe code."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422,
                    "invalid_auth_migration_request",
                ) from None

        return safe_handler


router = APIRouter(
    prefix="/internal/cron/auth-migration",
    tags=["internal-cron-auth-migration"],
    route_class=SafeValidationRoute,
)
TaskId = Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")]


async def _store(request: Request):
    verify_internal_access(request)
    store = AuthMigrationTaskStore(
        getattr(request.app.state, "db_connection", None),
    )
    try:
        await store.check_schema()
    except CronAuthRepositoryUnavailable:
        raise HTTPException(
            503,
            "auth_migration_progress_unavailable",
        ) from None
    return store


@router.post("/tasks", response_model=AuthMigrationTaskStatus, status_code=202)
async def submit(payload: AuthMigrationRequest, request: Request):
    store = await _store(request)
    try:
        repository = CronAuthRepository(store.db)
        await repository.check_schema()
        service = AuthMigrationService(working_root(), repository)
        runner = getattr(request.app.state, "cron_auth_migration_tasks", None)
        if runner is None:
            runner = AuthMigrationTaskRunner(store)
            request.app.state.cron_auth_migration_tasks = runner
        task_id = await runner.submit(payload, service)
        return AuthMigrationTaskStatus(
            task_id=task_id,
            status="queued",
            progress={
                "operation": payload.operation,
                "instance_id": service.instance_id,
            },
        )
    except AuthMigrationTaskCapacity:
        raise HTTPException(
            429,
            "auth_migration_task_capacity_reached",
        ) from None
    except Exception:
        raise HTTPException(
            503,
            "auth_migration_service_unavailable",
        ) from None


@router.get("/tasks/{task_id}", response_model=AuthMigrationTaskStatus)
async def query(
    task_id: TaskId,
    request: Request,
    after_page: Annotated[int, Query(ge=0, le=10**12 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
):
    store = await _store(request)
    try:
        return await store.view(task_id, after_page=after_page, limit=limit)
    except AuthMigrationTaskNotFound:
        raise HTTPException(404, "auth_migration_task_not_found") from None
    except Exception:
        raise HTTPException(
            503,
            "auth_migration_progress_unavailable",
        ) from None
