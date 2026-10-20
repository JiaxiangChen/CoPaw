# -*- coding: utf-8 -*-
"""独立任务控制；保留任务正文及实际执行结果。"""

import asyncio
import json
import logging
from zoneinfo import ZoneInfo
from datetime import timezone

from .contracts import ManagedCronUnavailable
from .definition import canonical, request_hash, stable_id
from .operations import (
    JOURNAL,
    WHERE_JOB,
    ManagementConflict,
    ManagementStore,
    operation_summary,
    utc_now,
)
from .priority import normalize_priority
from .projection import write_projection
from .repository import IDENTITY_FIELDS, ManagedCronRepository
from .storage import insert, transaction, update

logger = logging.getLogger(__name__)


def _journal(caller, current, action, payload):
    now = utc_now()
    needs_platform_sync = action in {"set_enabled", "auto_pause"}
    return {
        "operation_id": stable_id(caller, payload["request_id"], action),
        "active_key": (
            request_hash(caller.job_key(current["job_id"]))
            if needs_platform_sync
            else None
        ),
        **{name: current[name] for name in IDENTITY_FIELDS},
        "actor_id": caller.actor_id,
        "action": action,
        "request_id": payload["request_id"],
        "request_hash": request_hash(
            {
                "action": action,
                "job_id": current["job_id"],
                "payload": payload,
            },
        ),
        "expected_version": current["definition_version"],
        "payload_json": canonical(payload),
        "progress_json": canonical(
            {"database_applied": True, "read_watermark": now.isoformat()},
        ),
        "status": "pending" if needs_platform_sync else "completed",
        "stage": "platform" if needs_platform_sync else "completed",
        "error_code": None,
        "lease_owner": "",
        "lease_until": None,
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }


async def _state(db, key):
    state = await db.fetch_one(
        f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB} FOR UPDATE",
        key,
    )
    if state is None or tuple(state[name] for name in IDENTITY_FIELDS) != key:
        raise ManagedCronUnavailable("cron_state_record_missing")
    return state


async def _mark_histories_read(db, key, now):
    beijing = (
        now.replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .replace(tzinfo=None)
    )
    await db.execute(
        "UPDATE swe_cron_executions SET is_read=1, read_at=%s WHERE is_read=0 "
        "AND id IN (SELECT history_execution_id FROM swe_cron_execution_runs "
        f"WHERE {WHERE_JOB} AND finished_at<=%s AND history_execution_id IS NOT NULL)",
        (beijing, *key, now),
    )


def _validate_control(current, state, action, payload):
    if current["deleted_at"]:
        raise ManagementConflict("cron_job_deleted")
    if action == "mark_read":
        return
    if payload["expected_management_version"] != state["management_version"]:
        raise ManagementConflict("cron_management_version_conflict")
    if state["management_status"] != "ready":
        raise ManagementConflict("cron_job_not_ready")
    if action == "set_enabled" and type(payload["enabled"]) is not bool:
        raise ValueError("invalid_cron_enabled")
    if action == "set_priority" and current.get("parent_job_id"):
        raise ManagementConflict("cron_priority_requires_source")


class ControlService:
    def __init__(self, db, platform=None, *, grant=None):
        self.db = db
        self.platform = platform
        self.operations = ManagementStore(db, grant=grant)

    async def set_enabled(self, caller, job_id, payload):
        operation, reused = await self._begin(
            caller,
            job_id,
            "set_enabled",
            payload,
        )
        if not reused:
            return await self.retry(caller, job_id, operation["operation_id"])
        return operation_summary(operation)

    async def mark_read(self, caller, job_id, payload):
        operation, _ = await self._begin(caller, job_id, "mark_read", payload)
        return operation_summary(operation)

    async def apply_auto_pause(self, caller, job_id, payload):
        if self.operations.grant is None:
            raise ManagementConflict("cron_auto_pause_requires_internal_fact")
        operation, reused = await self._begin(
            caller,
            job_id,
            "auto_pause",
            payload,
        )
        if not reused:
            return await self.retry(caller, job_id, operation["operation_id"])
        return operation_summary(operation)

    async def set_priority(self, caller, job_id, payload):
        normalize_priority(payload)
        operation, _ = await self._begin(
            caller,
            job_id,
            "set_priority",
            payload,
        )
        return operation_summary(operation)

    async def _begin(self, caller, job_id, action, payload):
        for attempt in range(3):
            try:
                operation, reused = await self._begin_once(
                    caller,
                    job_id,
                    action,
                    payload,
                )
                logger.info(
                    "任务控制请求已持久受理 action=%s operation_id=%s job_id=%s reused=%s",
                    action,
                    operation["operation_id"],
                    job_id,
                    reused,
                )
                return operation, reused
            except Exception as exc:
                code = exc.args[0] if exc.args else None
                if code not in (1062, 1205, 1213) or attempt == 2:
                    raise
                await asyncio.sleep(0)
        raise RuntimeError("unreachable_control_transaction_retry")

    async def _begin_once(self, caller, job_id, action, payload):
        key = caller.job_key(job_id)
        async with transaction(self.db) as tx:
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            current = await self.operations._authorize(tx, caller, job_id)
            operation = _journal(caller, current, action, payload)
            previous = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id=%s FOR UPDATE",
                (operation["operation_id"],),
            )
            if previous:
                if previous["request_hash"] != operation["request_hash"]:
                    raise ManagementConflict("cron_request_content_conflict")
                return previous, True
            state = await _state(tx, key)
            _validate_control(current, state, action, payload)
            if action != "mark_read":
                await self._control_slot(tx, key, action, payload)
            await insert(tx, JOURNAL, operation)
            await self._apply(tx, key, current, state, operation)
            return operation, False

    @staticmethod
    async def _control_slot(tx, key, action, payload):
        active = await tx.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE active_key=%s FOR UPDATE",
            (request_hash(key),),
        )
        if active is None:
            return
        if not (
            action == "set_enabled"
            and payload["enabled"]
            and active["action"] == "auto_pause"
            and active["status"] == "failed"
        ):
            raise ManagementConflict("cron_management_in_progress")
        # 显式恢复可替代已经失败且没有在途步骤的旧自动暂停，不能接管运行中操作。
        await update(
            tx,
            JOURNAL,
            {
                "status": "superseded",
                "active_key": None,
                "lease_owner": "",
                "lease_until": None,
                "error_code": "auto_pause_replaced_by_resume",
                "version": active["version"] + 1,
                "updated_at": utc_now(),
            },
            "operation_id=%s AND version=%s",
            (active["operation_id"], active["version"]),
        )

    @staticmethod
    async def _apply(tx, key, current, state, operation):
        spec = json.loads(current["spec_json"])
        payload = json.loads(operation["payload_json"])
        action, now = operation["action"], operation["created_at"]
        changes = {}
        if action == "set_enabled":
            spec["enabled"] = payload["enabled"]
            changes.update(
                {
                    "pause_reason": None if spec["enabled"] else "manual",
                    "auto_paused_at": None,
                },
            )
            if not spec["enabled"]:
                changes["queue_invalidation_version"] = (
                    state["state_version"] + 1
                )
        if action == "auto_pause":
            # SWE 已记录原因、首次时间及失效水位，管理层不能重新生成或覆盖它们。
            spec["enabled"] = False
        if action == "mark_read" or (
            action == "set_enabled" and spec["enabled"]
        ):
            await _mark_histories_read(tx, key, now)
            changes["unread_execution_count"] = 0
        if action == "set_priority":
            spec["meta"]["batch_dispatch_priority"] = normalize_priority(
                payload,
            )
        if action != "mark_read":
            await update(
                tx,
                "swe_cron_jobs_v2",
                {
                    "spec_json": canonical(spec),
                    "enabled": spec["enabled"],
                    "updated_at": now,
                },
                WHERE_JOB,
                key,
            )
        changes.update(
            {
                "management_version": state["management_version"] + 1,
                "state_version": state["state_version"] + 1,
                "management_updated_at": now,
                "updated_at": now,
            },
        )
        await update(tx, "swe_cron_job_states", changes, WHERE_JOB, key)
        await write_projection(tx, spec, {**state, **changes}, key[2], now)

    async def retry(self, caller, job_id, operation_id):
        operation = await self.operations.claim(caller, job_id, operation_id)
        if operation is None:
            return operation_summary(
                await self.operations.get(caller, job_id, operation_id),
            )
        if operation["action"] not in {"set_enabled", "auto_pause"}:
            raise ManagementConflict("unsupported_cron_control_retry")
        logger.info(
            "开始同步任务启停 operation_id=%s job_id=%s",
            operation_id,
            job_id,
        )
        try:
            current = await self._sync_enabled(operation)
        except Exception:
            await self._finish(operation, failed=True)
        else:
            await self._finish(
                operation,
                failed=False,
                obsolete=current is False,
            )
        result = operation_summary(
            await self.operations.get(caller, job_id, operation_id),
        )
        logger.info(
            "任务启停同步结束 operation_id=%s job_id=%s status=%s error_code=%s",
            operation_id,
            job_id,
            result["status"],
            result["error_code"],
        )
        return result

    async def _sync_enabled(self, operation):
        key = tuple(operation[name] for name in IDENTITY_FIELDS)
        state = await self.operations.state(operation)
        if operation["action"] == "auto_pause":
            payload = json.loads(operation["payload_json"])
            if (
                state["pause_reason"] != "auto_unread_threshold"
                or state["queue_invalidation_version"]
                != payload["auto_pause_epoch"]
            ):
                return False
        if state["dispatch_state"] != "ordinary":
            return
        current = await self.db.fetch_one(
            f"SELECT enabled FROM swe_cron_jobs_v2 WHERE {WHERE_JOB}",
            key,
        )
        external_id = state["ordinary_external_job_id"]
        if self.platform is None or not external_id:
            raise RuntimeError("cron_external_binding_missing")
        await self.platform.set_enabled(
            external_id,
            bool(current["enabled"] and not state["pause_reason"]),
        )

    async def _finish(self, operation, *, failed, obsolete=False):
        async with transaction(self.db) as tx:
            await self.operations._fence(tx, operation)
            await update(
                tx,
                JOURNAL,
                {
                    "status": (
                        "superseded"
                        if obsolete
                        else "failed" if failed else "completed"
                    ),
                    "stage": "platform" if failed else "completed",
                    "error_code": (
                        "cron_auto_pause_obsolete"
                        if obsolete
                        else "cron_control_sync_failed" if failed else None
                    ),
                    "active_key": operation["active_key"] if failed else None,
                    "lease_owner": "",
                    "lease_until": None,
                    "version": operation["version"] + 1,
                    "updated_at": utc_now(),
                },
                "operation_id=%s AND lease_owner=%s AND version=%s",
                (
                    operation["operation_id"],
                    operation["lease_owner"],
                    operation["version"],
                ),
            )
