# -*- coding: utf-8 -*-
"""按权限只读查询共用的任务定义表和状态表。"""

from __future__ import annotations

import base64
import json
from typing import Any

from .contracts import (
    ManagedCronNotFound,
    ManagedCronUnavailable,
    QueryOptions,
)
from .identity import CallerIdentity, identity_value
from .views import STATE_COLUMNS, child_item, job_view, list_item

IDENTITY_FIELDS = ("tenant_id", "source_id", "agent_id", "job_id")
_JOIN = " AND ".join(
    f"d.`{key}` = s.`{key}` AND d.`{key}` COLLATE utf8mb4_bin = s.`{key}`"
    for key in IDENTITY_FIELDS
)
_STATE_SELECT = ", ".join(
    f"s.`{key}` AS `state_{key}`" for key in ("job_id", *STATE_COLUMNS)
)
_SELECT = (
    f"SELECT d.*, {_STATE_SELECT} FROM swe_cron_jobs_v2 d "
    f"LEFT JOIN swe_cron_job_states s ON {_JOIN}"
)
_ORDER = ", ".join(f"d.`{key}`" for key in IDENTITY_FIELDS)


def _key(row: dict) -> tuple:
    return tuple(row[name] for name in IDENTITY_FIELDS)


def _parent_key(row: dict) -> tuple | None:
    values = tuple(row.get(f"parent_{name}") for name in IDENTITY_FIELDS)
    return values if all(values) else None


def _identity_terms(names, values):
    # 保留可命中索引的等值条件，同时显式区分大小写。
    # 避免 MySQL 默认不区分大小写的排序规则扩大标准 scope 的身份范围。
    clauses = [
        f"d.`{name}`=%s AND d.`{name}` COLLATE utf8mb4_bin=%s"
        for name in names
    ]
    params = [part for value in values for part in (value, value)]
    return clauses, params


def _cursor_context(
    caller: CallerIdentity,
    deleted: bool,
    parent: tuple | None,
    targets: tuple[str, ...],
) -> dict:
    return {
        "identity": [
            caller.tenant_id,
            caller.source_id,
            caller.agent_id,
            caller.actor_id,
        ],
        "include_deleted": deleted,
        "parent": list(parent) if parent else None,
        "tenant_ids": list(targets),
        "manager": caller.is_manager,
    }


def _decode_cursor(token: str | None, context: dict) -> tuple | None:
    if token is None:
        return None
    try:
        raw = base64.b64decode(
            token + "=" * (-len(token) % 4),
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(raw)
        if value.get("context") != context or value.get("version") != 1:
            raise ValueError("cursor_context")
        after = value["after"]
        if not isinstance(after, list) or len(after) != 4:
            raise ValueError("cursor_position")
        return tuple(identity_value(item) for item in after)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise ValueError("invalid_cron_cursor") from exc


def _encode_cursor(row: dict, context: dict) -> str:
    value = {"version": 1, "context": context, "after": _key(row)}
    return (
        base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode(),
        )
        .decode()
        .rstrip("=")
    )


class ManagedCronRepository:
    def __init__(self, db: Any):
        self.db = db

    async def _rows(
        self,
        clauses: list[str],
        params: tuple,
        *,
        limit: int,
    ) -> list[dict]:
        try:
            if not getattr(self.db, "is_connected", False):
                raise ManagedCronUnavailable("cron_database_unavailable")
            return await self.db.fetch_all(
                f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY {_ORDER} LIMIT %s",
                (*params, limit),
            )
        except ManagedCronUnavailable:
            raise
        except Exception as exc:
            raise ManagedCronUnavailable("cron_database_unavailable") from exc

    async def _get_row(self, key: tuple, include_deleted: bool) -> dict:
        clauses, params = _identity_terms(IDENTITY_FIELDS, key)
        clauses.append("d.management_owner = 'scheduler'")
        if not include_deleted:
            clauses.append("d.deleted_at IS NULL")
        rows = await self._rows(clauses, tuple(params), limit=1)
        if not rows:
            raise ManagedCronNotFound("cron_job_not_found")
        return rows[0]

    async def _authorized_row(
        self,
        caller: CallerIdentity,
        job_id: str,
        deleted: bool,
    ) -> dict:
        row = await self._get_row(caller.job_key(job_id), deleted)
        if caller.is_manager or row.get("creator_user_id") == caller.actor_id:
            return row
        parent_key = _parent_key(row)
        if parent_key is not None and parent_key[1] == caller.source_id:
            parent = await self._get_row(parent_key, include_deleted=True)
            if parent.get("creator_user_id") == caller.actor_id:
                return row
        raise ManagedCronNotFound("cron_job_not_found")

    async def detail(
        self,
        caller: CallerIdentity,
        job_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict:
        row = await self._authorized_row(caller, job_id, include_deleted)
        return job_view(row, caller.actor_id)

    async def page(
        self,
        caller: CallerIdentity,
        options: QueryOptions,
        *,
        parent_id: str | None = None,
    ) -> dict:
        limit = options.limit
        if not 1 <= limit <= 200:
            raise ValueError("invalid_cron_page_limit")
        parent = None
        if parent_id is not None:
            source = await self._authorized_row(caller, parent_id, True)
            # 接收方只能查看自己的子任务，不能据此查看同源的其他接收方。
            if (
                not caller.is_manager
                and source.get("creator_user_id") != caller.actor_id
            ):
                raise ManagedCronNotFound("cron_job_not_found")
            parent = _key(source)
        targets = self._target_filter(options.tenant_ids)
        context = _cursor_context(
            caller,
            options.include_deleted,
            parent,
            targets,
        )
        after = _decode_cursor(options.cursor, context)
        clauses, params = self._page_filter(
            caller,
            options.include_deleted,
            parent,
        )
        if targets:
            clauses.append(
                f"d.tenant_id IN ({', '.join('%s' for _ in targets)})",
            )
            params.extend(targets)
        if after:
            clauses.append(f"({_ORDER}) > (%s, %s, %s, %s)")
            params.extend(after)
        rows = await self._rows(clauses, tuple(params), limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        mapper = child_item if parent else list_item
        result = {
            "items": [mapper(row, caller.actor_id) for row in rows],
            "next_cursor": _encode_cursor(rows[-1], context) if more else None,
        }
        if parent:
            result["source_job_id"] = parent_id
        return result

    @staticmethod
    def _target_filter(values: list[str] | None) -> tuple[str, ...]:
        if values is None:
            return ()
        if not values or len(values) > 1000:
            raise ValueError("invalid_cron_target_filter")
        return tuple(sorted({identity_value(value) for value in values}))

    @staticmethod
    def _page_filter(
        caller: CallerIdentity,
        deleted: bool,
        parent: tuple | None,
    ) -> tuple:
        clauses = ["d.management_owner = 'scheduler'"]
        if not deleted:
            clauses.append("d.deleted_at IS NULL")
        if parent:
            terms, params = _identity_terms(
                [f"parent_{key}" for key in IDENTITY_FIELDS],
                parent,
            )
            return [*clauses, *terms], params
        names = list(IDENTITY_FIELDS[:3])
        params = [caller.tenant_id, caller.source_id, caller.agent_id]
        if not caller.is_manager:
            names.append("creator_user_id")
            params.append(caller.actor_id)
        terms, bound = _identity_terms(names, params)
        return [*clauses, *terms], bound
