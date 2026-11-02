# -*- coding: utf-8 -*-
"""Strict snapshot persistence for the manually provisioned cron-auth table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from ...config.context import decode_scope_id
from .auth_state import CronAuthState, ensure_utc

TABLE_NAME = "swe_cron_auth_states"
TIME_FIELDS = (
    "user_info_expires_at",
    "user_info_refreshed_at",
    "auth_token_expires_at",
    "last_prefetch_at",
)
STATE_FIELDS = (
    "user_info",
    "user_info_expires_at",
    "user_info_refreshed_at",
    "auth_token",
    "auth_token_expires_at",
    "cookie_header",
    "last_prefetch_at",
    "last_error",
)
COLUMNS = (
    "storage_scope_id",
    "tenant_id",
    "source_id",
    *STATE_FIELDS,
    "created_at",
    "updated_at",
)
SELECT_COLUMNS = ", ".join(f"`{name}`" for name in COLUMNS)


class CronAuthRepositoryUnavailable(RuntimeError):
    """The database operation or manually provisioned schema is unavailable."""


def _scope_metadata(storage_scope_id: str) -> tuple[str | None, str | None]:
    """Decode metadata without canonicalizing the actual storage directory key."""
    try:
        return decode_scope_id(storage_scope_id)
    except ValueError:
        if storage_scope_id.startswith("default_"):
            return None, storage_scope_id[len("default_") :] or None
        return None, None


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    key = result["storage_scope_id"]
    if isinstance(key, (bytes, bytearray)):
        result["storage_scope_id"] = key.decode("utf-8")
    return result


class CronAuthRepository:
    """Store one full snapshot per exact scope-directory identity.

    MySQL DATETIME values are written as naive UTC and restored to aware UTC
    by state_from_row. No method creates tables or suppresses database errors.
    """

    def __init__(self, db: Any):
        self.db = db

    async def _query(
        self,
        method: str,
        query: str,
        params: tuple | None = None,
        *,
        schema: bool = False,
    ) -> Any:
        if self.db is None or not getattr(self.db, "is_connected", False):
            raise CronAuthRepositoryUnavailable(
                "cron_auth_database_unavailable",
            )
        try:
            return await getattr(self.db, method)(query, params)
        except Exception:
            # Driver errors may include SQL parameters containing credentials.
            reason = "schema" if schema else "database"
            raise CronAuthRepositoryUnavailable(
                f"cron_auth_{reason}_unavailable",
            ) from None

    async def check_schema(self) -> None:
        """Verify all required columns with SELECT; DDL is operator-managed."""
        await self._query(
            "execute",
            f"SELECT {SELECT_COLUMNS} FROM `{TABLE_NAME}` WHERE 1 = 0",
            schema=True,
        )

    async def upsert(
        self,
        storage_scope_id: str,
        state: CronAuthState,
    ) -> None:
        tenant_id, source_id = _scope_metadata(storage_scope_id)
        values = state.model_dump()
        values["user_info"] = json.dumps(
            state.model_dump(mode="json")["user_info"],
            ensure_ascii=False,
        )
        for name in TIME_FIELDS:
            value = ensure_utc(values[name])
            values[name] = value.replace(tzinfo=None) if value else None
        written_at = datetime.now(timezone.utc).replace(tzinfo=None)
        params = (
            storage_scope_id.encode("utf-8"),
            tenant_id,
            source_id,
            *(values[name] for name in STATE_FIELDS),
            written_at,
            written_at,
        )
        placeholders = ", ".join("%s" for _ in COLUMNS)
        updates = ", ".join(
            f"`{name}` = VALUES(`{name}`)"
            for name in COLUMNS
            if name not in {"storage_scope_id", "created_at"}
        )
        await self._query(
            "execute",
            f"INSERT INTO `{TABLE_NAME}` ({SELECT_COLUMNS}) "
            f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}",
            params,
        )

    async def delete(self, storage_scope_id: str) -> None:
        await self._query(
            "execute",
            f"DELETE FROM `{TABLE_NAME}` WHERE `storage_scope_id` = %s",
            (storage_scope_id.encode("utf-8"),),
        )

    async def get(self, storage_scope_id: str) -> dict[str, Any] | None:
        row = await self._query(
            "fetch_one",
            f"SELECT {SELECT_COLUMNS} FROM `{TABLE_NAME}` "
            "WHERE `storage_scope_id` = %s",
            (storage_scope_id.encode("utf-8"),),
        )
        return _public_row(row) if row is not None else None

    async def list_rows(
        self,
        after_scope_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        where = ""
        params: tuple = (limit,)
        if after_scope_id is not None:
            where = " WHERE `storage_scope_id` > %s"
            params = (after_scope_id.encode("utf-8"), limit)
        rows = await self._query(
            "fetch_all",
            f"SELECT {SELECT_COLUMNS} FROM `{TABLE_NAME}`{where} "
            "ORDER BY BINARY `storage_scope_id` LIMIT %s",
            params,
        )
        return [_public_row(row) for row in rows]

    @staticmethod
    def state_from_row(row: Mapping[str, Any]) -> CronAuthState:
        values = {name: row[name] for name in STATE_FIELDS}
        values["user_info"] = json.loads(values["user_info"])
        state = CronAuthState.model_validate(values)
        return state.model_copy(
            update={
                name: ensure_utc(getattr(state, name)) for name in TIME_FIELDS
            },
        )
