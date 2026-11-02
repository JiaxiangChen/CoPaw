# -*- coding: utf-8 -*-
"""Bounded best-effort mirror for file-authoritative cron credentials."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .auth_repository import CronAuthRepository
    from .auth_state import CronAuthState

logger = logging.getLogger(__name__)
MIRROR_TIMEOUT_SECONDS = 3.0
_repository: CronAuthRepository | None = None


def set_cron_auth_repository(repository: CronAuthRepository | None) -> None:
    global _repository
    _repository = repository


async def mirror_upsert(scope_id: str, state: CronAuthState) -> None:
    if _repository is not None:
        await _attempt("upsert", scope_id, _repository.upsert(scope_id, state))


async def mirror_delete(scope_id: str) -> None:
    if _repository is not None:
        await _attempt("delete", scope_id, _repository.delete(scope_id))


async def _attempt(operation: str, scope_id: str, work) -> None:
    try:
        await asyncio.wait_for(work, timeout=MIRROR_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning(
            "Cron auth mirror failed operation=%s scope=%s error=%s",
            operation,
            scope_id,
            type(exc).__name__,
        )


def configure_cron_auth_mirror(db) -> None:
    """Bind the service's existing connection, or clear it during teardown."""
    from .auth_repository import CronAuthRepository

    set_cron_auth_repository(
        CronAuthRepository(db) if db is not None else None,
    )


@asynccontextmanager
async def cli_cron_auth_mirror():
    """Own one optional database connection for an entire CLI operation."""
    from ...database import DatabaseConnection, get_database_config

    connection = None
    try:
        try:
            # The default localhost host does not imply database configuration.
            if any(
                os.environ.get(name)
                for name in (
                    "SWE_DB_HOST",
                    "SWE_DB_PORT",
                    "SWE_DB_USER",
                    "SWE_DB_ACCESS",
                    "SWE_DB_NAME",
                )
            ):
                config = get_database_config()
                connection = DatabaseConnection(config)
                await asyncio.wait_for(
                    connection.connect(),
                    MIRROR_TIMEOUT_SECONDS,
                )
                configure_cron_auth_mirror(connection)
        except Exception as exc:
            logger.warning(
                "Cron auth mirror failed operation=connect error=%s",
                type(exc).__name__,
            )
        yield
    finally:
        set_cron_auth_repository(None)
        if connection is not None:
            try:
                await connection.close()
            except Exception as exc:
                logger.warning(
                    "Cron auth mirror failed operation=close error=%s",
                    type(exc).__name__,
                )
