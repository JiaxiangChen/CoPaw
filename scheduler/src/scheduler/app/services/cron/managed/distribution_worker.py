# -*- coding: utf-8 -*-
"""领取已受理的分发记录逐项处理，只恢复原轮，不自动重做已失败目标。"""

import copy
import asyncio
import json
import logging
from uuid import uuid4

from .child_management import ChildGrant, child_payload
from .contracts import ManagedCronNotFound
from .definition import stable_id
from .distribution import BroadcastStore, ITEMS, TASKS, _children, _summary
from .identity import CallerIdentity
from .management import ManagementService
from .operations import (
    IDENTITY_FIELDS,
    JOURNAL,
    WHERE_JOB,
    LeaseLost,
    ManagementConflict,
    utc_now,
)
from .retime_resources import ExistingBindings
from .repository import ManagedCronRepository
from .scatter import shift_cron_expression
from .storage import transaction, update

logger = logging.getLogger(__name__)


class BroadcastProcessor:
    def __init__(self, db, resources, platform, *, concurrency=4):
        if type(concurrency) is not int or not 1 <= concurrency <= 16:
            raise ValueError("invalid_broadcast_concurrency")
        self.db = db
        self.resources = resources
        self.platform = platform
        self.store = BroadcastStore(db)
        self.concurrency = concurrency

    async def process(self, task_id):
        owner = str(uuid4())
        task = await self.store.claim(task_id, owner)
        if task is None:
            return
        try:
            items = await self.db.fetch_all(
                f"SELECT * FROM {ITEMS} WHERE task_id=%s AND status IN ('pending','running') ORDER BY index_no",
                (task_id,),
            )
            semaphore = asyncio.Semaphore(self.concurrency)

            async def bounded(item):
                async with semaphore:
                    return await self._one(task, item, owner)

            results = await asyncio.gather(
                *(bounded(item) for item in items),
                return_exceptions=True,
            )
            errors = [
                result
                for result in results
                if isinstance(result, BaseException)
            ]
            if errors:
                raise errors[0]
            summary = await _summary(self.db, task)
            if summary["pending_count"]:
                await self._release(task_id, owner)
                return
            error = await self._sync_parent(task, owner)
            await self.store.finish(task_id, owner, error_code=error)
        except LeaseLost:
            logger.info("分发租约已转移，当前实例停止提交 task_id=%s", task_id)
        except Exception as exc:
            logger.warning(
                "分发技术步骤暂未完成，保留原轮恢复 task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
            )
            await self._release(task_id, owner)

    async def _one(self, task, item, owner):
        grant = ChildGrant(task["task_id"], item["index_no"], owner)
        caller = CallerIdentity(
            item["tenant_id"],
            item["source_id"],
            item["agent_id"],
            task["actor_id"],
            bool(task["actor_is_manager"]),
        )
        resources = (
            ExistingBindings(self.db)
            if item["operation"] == "retimed"
            else self.resources
        )
        service = ManagementService(
            self.db,
            resources,
            self.platform,
            grant=grant,
        )
        action = "create" if item["operation"] == "created" else "update"
        try:
            operation = await self._begin_item(
                service,
                caller,
                task,
                item,
                action,
            )
            await self.store.record_item(
                task["task_id"],
                owner,
                item["index_no"],
                "running",
                {"operation_id": operation["operation_id"]},
            )
            if operation["status"] in {"pending", "running"}:
                result = await service.retry(
                    caller,
                    item["job_id"],
                    operation["operation_id"],
                )
            else:
                result = operation
        except (ManagementConflict, ManagedCronNotFound, ValueError) as exc:
            code = (
                str(exc)
                if isinstance(exc, ManagementConflict)
                else "target_definition_conflict"
            )
            await self.store.record_item(
                task["task_id"],
                owner,
                item["index_no"],
                "failed",
                {"error_code": code},
            )
            return
        status = {
            "completed": "succeeded",
            "failed": "failed",
            "superseded": "failed",
        }.get(result["status"], "running")
        warning = None
        if status == "succeeded":
            completed = await service.store.get(
                caller,
                item["job_id"],
                result["operation_id"],
            )
            warning = (
                json.loads(completed["progress_json"])
                .get("prepared", {})
                .get("warning")
            )
        await self.store.record_item(
            task["task_id"],
            owner,
            item["index_no"],
            status,
            {
                "operation_id": result["operation_id"],
                "error_code": result["error_code"],
                "warning": warning,
            },
        )

    async def _begin_item(self, service, caller, task, item, action):
        request_id = f"{task['task_id']}:{item['index_no']}"
        operation_id = stable_id(caller, request_id, action)
        existing = await self.db.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE operation_id=%s AND {WHERE_JOB}",
            (operation_id, *caller.job_key(item["job_id"])),
        )
        if existing:
            context = json.loads(existing["payload_json"]).get(
                "broadcast_context",
            )
            if context != {
                "task_id": task["task_id"],
                "index_no": item["index_no"],
            }:
                raise ManagementConflict(
                    "cron_broadcast_operation_context_conflict",
                )
            return await service.store.get(
                caller,
                item["job_id"],
                operation_id,
            )
        current_spec = None
        if item["operation"] == "retimed":
            current = await ManagedCronRepository(self.db)._get_row(
                caller.job_key(item["job_id"]),
                include_deleted=True,
            )
            current_spec = json.loads(current["spec_json"])
        operation, _ = await service.store.begin(
            caller,
            action,
            child_payload(task, item, current_spec),
            item["job_id"],
        )
        return operation

    async def _release(self, task_id, owner):
        async with transaction(self.db) as tx:
            await update(
                tx,
                TASKS,
                {
                    "lease_owner": "",
                    "lease_until": None,
                    "updated_at": utc_now(),
                },
                "task_id=%s AND lease_owner=%s",
                (task_id, owner),
            )

    async def _sync_parent(self, task, owner):
        key = tuple(task[name] for name in IDENTITY_FIELDS)
        current = await ManagedCronRepository(self.db)._get_row(
            key,
            include_deleted=True,
        )
        mode = current["state_dispatch_state"]
        if mode == "ordinary":
            return None
        children = await _children(self.db, key, [])
        rows = await self.db.fetch_all(
            "SELECT offset_minutes FROM swe_cron_jobs_v2 WHERE parent_tenant_id=%s AND parent_source_id=%s AND parent_agent_id=%s AND parent_job_id=%s AND management_owner='scheduler' AND deleted_at IS NULL",
            key,
        )
        advance = max(
            (int(row["offset_minutes"] or 0) for row in rows),
            default=0,
        )
        spec = copy.deepcopy(json.loads(current["spec_json"]))
        if advance:
            shifted = shift_cron_expression(
                current["original_cron_expr"] or current["cron_expr"],
                current["timezone"],
                offset_minutes=advance,
            )
            if shifted.error:
                return "batch_timer_cron_not_shiftable"
            spec["schedule"]["cron"] = shifted.cron
        external_id = current.get("state_batch_external_job_id")
        if not external_id:
            return "batch_timer_binding_missing"
        async with transaction(self.db) as tx:
            latest = await self.store._fence(tx, task["task_id"], owner)
            payload = json.loads(latest["payload_json"])
            payload["timer_sync"] = {
                "advance_minutes": advance,
                "spec": spec,
                "dispatch_state": mode,
            }
            await update(
                tx,
                TASKS,
                {
                    "payload_json": json.dumps(payload, ensure_ascii=False),
                    "version": latest["version"] + 1,
                },
                "task_id=%s AND lease_owner=%s",
                (task["task_id"], owner),
            )
        operation = {
            **task,
            "operation_id": task["task_id"],
            "payload_json": json.dumps({"spec": spec, "dispatch_state": mode}),
        }
        try:
            await self.platform.update(external_id, operation, spec, mode)
            await self.platform.set_enabled(
                external_id,
                mode == "batch_enabled",
            )
        except Exception as exc:
            logger.warning(
                "分发批入口时间同步失败 task_id=%s error_type=%s",
                task["task_id"],
                type(exc).__name__,
            )
            return "batch_timer_sync_failed"
        logger.info(
            "分发批入口时间已同步 task_id=%s advance_minutes=%s child_count=%s",
            task["task_id"],
            advance,
            len(children),
        )
        return None
