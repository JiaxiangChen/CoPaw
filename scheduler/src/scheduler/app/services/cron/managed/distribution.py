# -*- coding: utf-8 -*-
"""持久受理新版异步分发，固定目标和偏移，与旧 SWE 分发表隔离。"""

import json
import logging
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from .contracts import ManagedCronNotFound, ManagedCronUnavailable
from .definition import canonical, request_hash, stable_id
from .identity import identity_value
from .operations import (
    JOURNAL,
    WHERE_JOB,
    LeaseLost,
    ManagementConflict,
    utc_now,
)
from .pagination import decode_position, encode_position
from .repository import IDENTITY_FIELDS, ManagedCronRepository, _identity_terms
from .scatter import (
    ShiftedCron,
    compute_broadcast_offsets,
    shift_cron_expression,
)
from .storage import insert, transaction, update

TASKS = "swe_cron_managed_broadcast_tasks"
ITEMS = "swe_cron_managed_broadcast_items"
logger = logging.getLogger(__name__)


class BroadcastConflict(ManagementConflict):
    """源任务存在并发分发或请求、版本冲突，可附带当前分发标识。"""

    def __init__(self, code, task_id=None):
        super().__init__(code)
        self.task_id = task_id


class BroadcastLeaseLost(LeaseLost):
    """已失去分发租约的后台实例不能提交进度或覆盖新结果。"""


def _lease_time(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _optional_text(value, maximum):
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("invalid_broadcast_profile")
    return value.strip() or None


def _target(raw, caller):
    if not isinstance(raw, dict) or set(raw) - {
        "tenant_id",
        "tenant_name",
        "bbk_id",
        "agent_id",
    }:
        raise ValueError("invalid_broadcast_target")
    tenant = raw.get("tenant_id")
    agent = raw.get("agent_id") or caller.agent_id
    if not isinstance(tenant, str) or not isinstance(agent, str):
        raise ValueError("invalid_broadcast_target")
    branch = _optional_text(raw.get("bbk_id"), 128)
    if branch is not None:
        identity_value(branch)
    return {
        "tenant_id": identity_value(tenant.strip()),
        "agent_id": identity_value(agent.strip()),
        "tenant_name": _optional_text(raw.get("tenant_name"), 255),
        "bbk_id": branch,
    }


def normalized_request(caller, payload):
    identity_value(payload["request_id"])
    targets = payload.get("targets")
    if not isinstance(targets, list) or len(targets) > 1000:
        raise ValueError("invalid_broadcast_target_count")
    enabled = payload.get("enable_offset", False)
    window = payload.get("offset_window_hours", 0)
    if (
        type(enabled) is not bool
        or type(window) is not int
        or not 0 <= window <= 24
    ):
        raise ValueError("invalid_broadcast_offset")
    result = {}
    for raw in targets:
        target = _target(raw, caller)
        key = target["tenant_id"], target["agent_id"]
        if key in result and result[key] != target:
            raise ValueError("conflicting_duplicate_broadcast_target")
        result[key] = target
    return {
        **payload,
        "targets": list(result.values()),
        "enable_offset": enabled,
        "offset_window_hours": window,
    }


async def _children(db, key, targets):
    names = tuple(f"parent_{name}" for name in IDENTITY_FIELDS)
    clauses, params = _identity_terms(names, key)
    clauses.extend(["d.management_owner='scheduler'", "d.deleted_at IS NULL"])
    if targets:
        tenants = sorted({target["tenant_id"] for target in targets})
        clauses.append(
            "d.tenant_id IN (" + ",".join("%s" for _ in tenants) + ")",
        )
        params.extend(tenants)
    return await db.fetch_all(
        "SELECT d.tenant_id, d.source_id, d.agent_id, d.job_id, d.enabled, "
        "d.definition_version, d.original_cron_expr, d.cron_expr, d.timezone "
        "FROM swe_cron_jobs_v2 d WHERE "
        + " AND ".join(clauses)
        + " ORDER BY d.tenant_id, d.source_id, d.agent_id, d.job_id",
        tuple(params),
    )


def _reserved_items(task, current, targets):
    by_target = {}
    for row in current:
        key = row["tenant_id"], row["agent_id"]
        if key in by_target:
            raise BroadcastConflict("ambiguous_broadcast_child")
        by_target[key] = row
    retime = not targets
    selected = targets or [
        {"tenant_id": row["tenant_id"], "agent_id": row["agent_id"]}
        for row in current
    ]
    payload = json.loads(task["payload_json"])
    window = payload["offset_window_hours"] if payload["enable_offset"] else 0
    offsets = compute_broadcast_offsets(len(selected), window_hours=window)
    return [
        _reserved_item(
            task,
            target,
            by_target.get((target["tenant_id"], target["agent_id"])),
            index,
            offset,
            retime,
        )
        for index, (target, offset) in enumerate(zip(selected, offsets), 1)
    ]


def _reserved_item(task, target, current, index, offset, retime):
    source = json.loads(task["payload_json"])["source_spec"]
    original = (
        current["original_cron_expr"] or current["cron_expr"]
        if retime
        else source["schedule"]["cron"]
    )
    zone = current["timezone"] if retime else source["schedule"]["timezone"]
    shifted = (
        shift_cron_expression(original, zone, offset_minutes=offset)
        if offset
        else ShiftedCron(original, zone, 0)
    )
    job_id = (
        current["job_id"]
        if current
        else str(
            uuid5(
                NAMESPACE_URL,
                canonical(
                    [
                        "managed-child",
                        task["task_id"],
                        target["tenant_id"],
                        target["agent_id"],
                    ],
                ),
            ),
        )
    )
    plan = {
        "profile": target,
        "enabled": bool(current["enabled"]) if current else True,
        "original_cron_expr": original,
        "original_timezone": zone,
        "retime_only": retime,
    }
    return {
        "task_id": task["task_id"],
        "index_no": index,
        "tenant_id": target["tenant_id"],
        "source_id": task["source_id"],
        "agent_id": target["agent_id"],
        "job_id": job_id,
        "expected_definition_version": (
            current["definition_version"] if current else None
        ),
        "operation": (
            "retimed" if retime else "updated" if current else "created"
        ),
        "status": "pending",
        "plan_json": canonical(plan),
        "result_json": None,
        "cron_expr": shifted.cron if not shifted.error else original,
        "timezone": zone,
        "offset_minutes": shifted.offset_minutes if not shifted.error else 0,
        "warning": "cron_offset_not_applied" if shifted.error else None,
        "lease_owner": "",
        "lease_until": None,
        "version": 1,
        "created_at": task["created_at"],
        "updated_at": task["created_at"],
    }


async def _summary(db, task):
    rows = await db.fetch_all(
        f"SELECT status, COUNT(*) AS count FROM {ITEMS} WHERE task_id=%s GROUP BY status",
        (task["task_id"],),
    )
    counts = {row["status"]: row["count"] for row in rows}
    if sum(counts.values()) != task["tenant_count"]:
        raise ManagedCronUnavailable("broadcast_target_records_missing")
    return {
        "task_id": task["task_id"],
        "source_job_id": task["job_id"],
        "source_definition_version": task["source_definition_version"],
        "status": task["status"],
        "tenant_count": task["tenant_count"],
        "success_count": counts.get("succeeded", 0),
        "failed_count": counts.get("failed", 0),
        "pending_count": counts.get("pending", 0) + counts.get("running", 0),
        "sync_status": task["sync_status"],
        "error_code": task["error_code"],
    }


def _item_view(row):
    extra = json.loads(row["result_json"]) if row["result_json"] else {}
    return {
        **{
            name: row[name]
            for name in (
                "tenant_id",
                "source_id",
                "agent_id",
                "job_id",
                "operation",
                "status",
                "offset_minutes",
                "warning",
            )
        },
        "cron": row["cron_expr"],
        "timezone": row["timezone"],
        "error_code": extra.get("error_code"),
    }


class BroadcastStore:
    def __init__(self, db):
        self.db = db

    async def claim(self, task_id, owner):
        async with transaction(self.db) as tx:
            row = await tx.fetch_one(
                f"SELECT * FROM {TASKS} WHERE task_id=%s FOR UPDATE",
                (task_id,),
            )
            if row is None or row["status"] not in {"queued", "running"}:
                return None
            now = utc_now()
            if row["lease_owner"] and _lease_time(row["lease_until"]) > now:
                return None
            values = {
                "lease_owner": owner,
                "lease_until": now + timedelta(seconds=120),
                "status": "running",
                "version": row["version"] + 1,
                "updated_at": now,
            }
            await update(
                tx,
                TASKS,
                values,
                "task_id=%s AND version=%s",
                (task_id, row["version"]),
            )
        logger.info("已领取分发后台任务 task_id=%s owner=%s", task_id, owner)
        return {**row, **values}

    @staticmethod
    async def _fence(tx, task_id, owner):
        row = await tx.fetch_one(
            f"SELECT * FROM {TASKS} WHERE task_id=%s FOR UPDATE",
            (task_id,),
        )
        if row is None or row["status"] not in {"queued", "running"}:
            raise BroadcastLeaseLost("broadcast_lease_lost")
        if (
            row["lease_owner"] != owner
            or _lease_time(row["lease_until"]) <= utc_now()
        ):
            raise BroadcastLeaseLost("broadcast_lease_lost")
        return row

    async def record_item(self, task_id, owner, index, status, result):
        if status not in {"pending", "running", "succeeded", "failed"}:
            raise ValueError("invalid_broadcast_item_status")
        async with transaction(self.db) as tx:
            task = await self._fence(tx, task_id, owner)
            row = await tx.fetch_one(
                f"SELECT * FROM {ITEMS} WHERE task_id=%s AND index_no=%s FOR UPDATE",
                (task_id, index),
            )
            if row is None:
                raise ManagedCronUnavailable(
                    "broadcast_target_records_missing",
                )
            if row["status"] in {"succeeded", "failed"}:
                return row
            public = {
                name: result.get(name)
                for name in ("operation_id", "error_code", "warning")
            }
            warnings = list(
                dict.fromkeys(
                    value
                    for value in (row["warning"], public["warning"])
                    if value
                ),
            )
            now = utc_now()
            values = {
                "status": status,
                "result_json": canonical(public),
                "warning": ";".join(warnings) or None,
                "version": row["version"] + 1,
                "updated_at": now,
            }
            await update(
                tx,
                ITEMS,
                values,
                "task_id=%s AND index_no=%s AND version=%s",
                (task_id, index, row["version"]),
            )
            await update(
                tx,
                TASKS,
                {
                    "lease_until": now + timedelta(seconds=120),
                    "version": task["version"] + 1,
                    "updated_at": now,
                },
                "task_id=%s AND lease_owner=%s",
                (task_id, owner),
            )
        logger.info(
            "分发目标进度已提交 task_id=%s index=%s status=%s error_code=%s",
            task_id,
            index,
            status,
            public["error_code"],
        )
        return {**row, **values}

    async def finish(self, task_id, owner, *, error_code=None):
        async with transaction(self.db) as tx:
            task = await self._fence(tx, task_id, owner)
            summary = await _summary(tx, task)
            if summary["pending_count"]:
                raise BroadcastConflict("broadcast_items_not_finished")
            status = "succeeded"
            if summary["failed_count"]:
                status = (
                    "partial_failed" if summary["success_count"] else "failed"
                )
            elif error_code:
                status = "failed"
            values = {
                "status": status,
                "sync_status": "failed" if error_code else "completed",
                "error_code": error_code,
                "active_key": None,
                "lease_owner": "",
                "lease_until": None,
                "version": task["version"] + 1,
                "updated_at": utc_now(),
            }
            await update(
                tx,
                TASKS,
                values,
                "task_id=%s AND lease_owner=%s",
                (task_id, owner),
            )
        logger.info(
            "异步分发处理结束 task_id=%s status=%s error_code=%s",
            task_id,
            status,
            error_code,
        )
        return {**task, **values}

    async def submit(self, caller, job_id, payload):
        payload = normalized_request(caller, payload)
        key = caller.job_key(job_id)
        task_id = stable_id(
            caller,
            payload["request_id"],
            "broadcast:" + job_id,
        )
        fingerprint = request_hash([key, payload])
        async with transaction(self.db) as tx:
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            root = await ManagedCronRepository(tx)._authorized_row(
                caller,
                job_id,
                True,
            )
            previous = await tx.fetch_one(
                f"SELECT * FROM {TASKS} WHERE task_id=%s",
                (task_id,),
            )
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise BroadcastConflict("cron_request_content_conflict")
                return {**await _summary(tx, previous), "reused": True}
            await self._ready(tx, key, root, payload)
            task = self._new_task(caller, root, payload, task_id, fingerprint)
            current = await _children(tx, key, payload["targets"])
            items = _reserved_items(task, current, payload["targets"])
            task["tenant_count"] = len(items)
            await insert(tx, TASKS, task)
            for item in items:
                await insert(tx, ITEMS, item)
            result = {**await _summary(tx, task), "reused": False}
        logger.info(
            "异步分发已持久受理 task_id=%s source_job_id=%s target_count=%s",
            task_id,
            job_id,
            len(items),
        )
        return result

    @staticmethod
    async def _ready(tx, key, root, payload):
        if (
            root["deleted_at"]
            or root.get("parent_job_id")
            or root.get("state_management_status") != "ready"
        ):
            raise BroadcastConflict("cron_broadcast_source_not_ready")
        if (
            root["definition_version"]
            != payload["expected_definition_version"]
        ):
            raise BroadcastConflict("cron_definition_version_conflict")
        active = await tx.fetch_one(
            f"SELECT task_id FROM {TASKS} WHERE active_key=%s",
            (request_hash(key),),
        )
        if active:
            raise BroadcastConflict(
                "cron_broadcast_in_progress",
                active["task_id"],
            )
        if await tx.fetch_one(
            f"SELECT operation_id FROM {JOURNAL} WHERE active_key=%s",
            (request_hash(key),),
        ):
            raise BroadcastConflict("cron_management_in_progress")

    @staticmethod
    def _new_task(caller, root, payload, task_id, fingerprint):
        now = utc_now()
        return {
            "task_id": task_id,
            "active_key": request_hash(caller.job_key(root["job_id"])),
            **{name: root[name] for name in IDENTITY_FIELDS},
            "actor_id": caller.actor_id,
            "actor_is_manager": int(caller.is_manager),
            "request_id": payload["request_id"],
            "request_hash": fingerprint,
            "source_definition_version": root["definition_version"],
            "payload_json": canonical(
                {
                    "source_spec": json.loads(root["spec_json"]),
                    "dispatch_state": root["state_dispatch_state"],
                    "retime_only": not payload["targets"],
                    "enable_offset": payload["enable_offset"],
                    "offset_window_hours": payload["offset_window_hours"],
                },
            ),
            "tenant_count": 0,
            "status": "queued",
            "sync_status": "pending",
            "error_code": None,
            "lease_owner": "",
            "lease_until": None,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }

    async def get(self, caller, job_id, task_id, *, limit=50, cursor=None):
        await ManagedCronRepository(self.db)._authorized_row(
            caller,
            job_id,
            True,
        )
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_cron_page_limit")
        task = await self.db.fetch_one(
            f"SELECT * FROM {TASKS} WHERE task_id=%s AND {WHERE_JOB}",
            (identity_value(task_id), *caller.job_key(job_id)),
        )
        if task is None or tuple(
            task[name] for name in IDENTITY_FIELDS
        ) != caller.job_key(job_id):
            raise ManagedCronNotFound("cron_broadcast_not_found")
        context = {
            "task_id": task_id,
            "source": list(caller.job_key(job_id)),
            "actor": caller.actor_id,
            "manager": caller.is_manager,
        }
        after = decode_position(cursor, context)
        rows = await self.db.fetch_all(
            f"SELECT * FROM {ITEMS} WHERE task_id=%s AND index_no>%s ORDER BY index_no LIMIT %s",
            (task_id, after, limit + 1),
        )
        visible = rows[:limit]
        return {
            **await _summary(self.db, task),
            "results": [_item_view(row) for row in visible],
            "next_cursor": (
                encode_position(context, visible[-1]["index_no"])
                if len(rows) > limit
                else None
            ),
        }

    async def current(self, caller, job_id, *, limit=50, cursor=None):
        await ManagedCronRepository(self.db)._authorized_row(
            caller,
            job_id,
            True,
        )
        task = await self.db.fetch_one(
            f"SELECT task_id FROM {TASKS} WHERE {WHERE_JOB} ORDER BY created_order DESC LIMIT 1",
            caller.job_key(job_id),
        )
        return {
            "task": (
                await self.get(
                    caller,
                    job_id,
                    task["task_id"],
                    limit=limit,
                    cursor=cursor,
                )
                if task
                else None
            ),
        }
