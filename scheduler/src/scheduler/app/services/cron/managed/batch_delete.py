# -*- coding: utf-8 -*-
"""后台逐项处理固定子任务清单，整批幂等沿用共库操作日志。"""

import json
import logging
from dataclasses import replace
from datetime import timedelta

from .contracts import ManagedCronNotFound
from .definition import canonical, request_hash, stable_id
from .distribution import TASKS
from .identity import identity_value
from .operations import (
    IDENTITY_FIELDS,
    JOURNAL,
    WHERE_JOB,
    ManagementConflict,
    ManagementStore,
    utc_now,
)
from .repository import ManagedCronRepository
from .storage import insert, transaction, update

logger = logging.getLogger(__name__)


def _normalize(payload):
    request_id = identity_value(payload.get("request_id", ""))
    items = payload.get("items")
    all_children = payload.get("all_children") is True
    if not isinstance(items, list) or not items:
        raise ValueError("invalid_cron_batch_delete_items")
    if len(items) > 1000 and not all_children:
        raise ValueError("invalid_cron_batch_delete_items")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "job_id",
            "tenant_id",
            "agent_id",
            "expected_definition_version",
        }:
            raise ValueError("invalid_cron_batch_delete_item")
        key = tuple(
            identity_value(item[name])
            for name in ("tenant_id", "agent_id", "job_id")
        )
        if key in seen:
            raise ValueError("duplicate_cron_child_identity")
        seen.add(key)
        version = item["expected_definition_version"]
        if type(version) is not int or version < 1:
            raise ValueError("invalid_cron_definition_version")
        result.append({**item})
    normalized = {"request_id": request_id, "items": result}
    if all_children:
        normalized["all_children"] = True
    return normalized


def _item_result(key, operation=None, *, http_status=200, error=None):
    status, stage, operation_id = "failed", None, None
    deleted = False
    if operation is not None:
        status = {
            "completed": "completed",
            "failed": "failed",
            "superseded": "failed",
        }.get(operation["status"], "pending")
        stage, operation_id = operation["stage"], operation["operation_id"]
        error = operation.get("error_code")
        deleted = operation["logical_delete_applied"]
        http_status = {"completed": 200, "pending": 202, "failed": 503}[status]
    return {
        **dict(zip(IDENTITY_FIELDS, key)),
        "status": status,
        "http_status": http_status,
        "logical_delete_applied": deleted,
        "operation_id": operation_id,
        "stage": stage,
        "error_code": error,
    }


def _summary(operation, results):
    completed = sum(row["status"] == "completed" for row in results)
    pending = sum(row["status"] == "pending" for row in results)
    failed = sum(row["status"] == "failed" for row in results)
    status = "completed"
    if failed:
        status = "failed" if failed == len(results) else "partial_failed"
    elif pending:
        status = "pending"
    return {
        "request_id": operation["request_id"],
        "source_job_id": operation["job_id"],
        "status": status,
        "total_count": len(results),
        "deleted_count": sum(row["logical_delete_applied"] for row in results),
        "completed_count": completed,
        "pending_count": pending,
        "failed_count": failed,
        "results": results,
    }


class BatchDeleteService:
    def __init__(self, db, management):
        self.db = db
        self.management = management
        self.operations = ManagementStore(db)

    async def delete(self, caller, parent_id, payload):
        payload = _normalize(payload)
        operation = await self._begin(caller, parent_id, payload)
        if operation["status"] == "completed":
            return await self._current(caller, operation)
        claimed = await self.operations.claim(
            caller,
            parent_id,
            operation["operation_id"],
        )
        if claimed is None:
            current = await self.operations.get(
                caller,
                parent_id,
                operation["operation_id"],
            )
            if current["status"] == "completed":
                return await self._current(caller, current)
            raise ManagementConflict("cron_batch_delete_in_progress")
        progress = json.loads(claimed["progress_json"])
        plans = json.loads(claimed["payload_json"])["plans"]
        logger.info(
            "开始处理子任务删除清单 operation_id=%s source_job_id=%s count=%s",
            claimed["operation_id"],
            parent_id,
            len(plans),
        )
        try:
            for index, plan in enumerate(plans):
                # 在每项外部调用前续租；实例中断重提时沿用原清单和子操作身份。
                await self._save(claimed, progress)
                cached = progress["results"].get(str(index))
                progress["results"][str(index)] = await self._one(
                    caller,
                    parent_id,
                    plan,
                    cached,
                )
                await self._save(claimed, progress)
            await self._save(claimed, progress, completed=True)
        except Exception:
            await self._save(claimed, progress, failed=True)
            raise
        result = _summary(
            claimed,
            [progress["results"][str(index)] for index in range(len(plans))],
        )
        logger.info(
            "批量删除处理结束 operation_id=%s status=%s deleted=%s pending=%s failed=%s",
            claimed["operation_id"],
            result["status"],
            result["deleted_count"],
            result["pending_count"],
            result["failed_count"],
        )
        return result

    async def _begin(self, caller, parent_id, payload):
        key = caller.job_key(parent_id)
        operation_id = stable_id(
            caller,
            payload["request_id"],
            f"batch_delete:{parent_id}",
        )
        fingerprint = request_hash([key, payload])
        async with transaction(self.db) as tx:
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            root = await ManagedCronRepository(tx)._authorized_row(
                caller,
                parent_id,
                True,
            )
            previous = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id=%s FOR UPDATE",
                (operation_id,),
            )
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise ManagementConflict("cron_request_content_conflict")
                return previous
            active = request_hash(key)
            if await tx.fetch_one(
                f"SELECT task_id FROM {TASKS} WHERE active_key=%s",
                (active,),
            ):
                raise ManagementConflict("cron_broadcast_in_progress")
            if await tx.fetch_one(
                f"SELECT operation_id FROM {JOURNAL} WHERE active_key=%s",
                (active,),
            ):
                raise ManagementConflict("cron_management_in_progress")
            plans = self._plans(caller, operation_id, payload["items"])
            now = utc_now()
            operation = {
                "operation_id": operation_id,
                "active_key": active,
                **dict(zip(IDENTITY_FIELDS, key)),
                "actor_id": caller.actor_id,
                "action": "batch_delete",
                "request_id": payload["request_id"],
                "request_hash": fingerprint,
                "expected_version": root["definition_version"],
                "payload_json": canonical(
                    {
                        "plans": plans,
                        "actor_is_manager": caller.is_manager,
                        "all_children": payload.get("all_children", False),
                    },
                ),
                "progress_json": canonical({"results": {}}),
                "status": "pending",
                "stage": "processing",
                "error_code": None,
                "lease_owner": "",
                "lease_until": None,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
            await insert(tx, JOURNAL, operation)
            return operation

    @staticmethod
    def _plans(caller, operation_id, items):
        result = []
        for item in items:
            child = replace(
                caller,
                tenant_id=item["tenant_id"],
                agent_id=item["agent_id"],
            )
            request_id = request_hash(
                [operation_id, child.job_key(item["job_id"])],
            )
            result.append(
                {
                    "item": item,
                    "request_id": request_id,
                    "operation_id": stable_id(child, request_id, "delete"),
                },
            )
        return result

    async def _save(
        self,
        operation,
        progress,
        *,
        completed=False,
        failed=False,
    ):
        now = utc_now()
        values = {
            "progress_json": canonical(progress),
            "status": (
                "completed" if completed else "failed" if failed else "running"
            ),
            "stage": "completed" if completed else "processing",
            "active_key": None if completed else operation["active_key"],
            "lease_owner": (
                "" if completed or failed else operation["lease_owner"]
            ),
            "lease_until": (
                None if completed or failed else now + timedelta(seconds=120)
            ),
            "error_code": (
                "cron_batch_delete_processing_failed" if failed else None
            ),
            "version": operation["version"] + 1,
            "updated_at": now,
        }
        async with transaction(self.db) as tx:
            await self.operations._fence(tx, operation)
            await update(
                tx,
                JOURNAL,
                values,
                "operation_id=%s AND version=%s",
                (operation["operation_id"], operation["version"]),
            )
        operation.update(values)

    async def _one(self, caller, parent_id, plan, cached):
        item = plan["item"]
        child = replace(
            caller,
            tenant_id=item["tenant_id"],
            agent_id=item["agent_id"],
        )
        key = child.job_key(item["job_id"])
        if cached is not None and cached["operation_id"] is None:
            return cached
        try:
            row = await ManagedCronRepository(self.db)._authorized_row(
                child,
                item["job_id"],
                True,
            )
            if tuple(
                row.get(f"parent_{name}") for name in IDENTITY_FIELDS
            ) != caller.job_key(parent_id):
                raise ManagedCronNotFound("cron_child_not_found")
            if cached is not None:
                operation = await self.operations.get(
                    child,
                    item["job_id"],
                    cached["operation_id"],
                )
                operation = {
                    **operation,
                    "logical_delete_applied": operation["action"] == "delete",
                }
            else:
                operation = await self.management.delete(
                    child,
                    item["job_id"],
                    {
                        "request_id": plan["request_id"],
                        "expected_definition_version": item[
                            "expected_definition_version"
                        ],
                    },
                )
            return _item_result(key, operation)
        except ManagedCronNotFound:
            return _item_result(
                key,
                http_status=404,
                error="cron_child_not_found",
            )
        except ManagementConflict as exc:
            return _item_result(key, http_status=409, error=str(exc))
        except ValueError:
            return _item_result(
                key,
                http_status=400,
                error="invalid_cron_batch_delete_item",
            )

    async def _current(self, caller, operation):
        plans = json.loads(operation["payload_json"])["plans"]
        progress = json.loads(operation["progress_json"])["results"]
        results = [
            await self._one(
                caller,
                operation["job_id"],
                plan,
                progress[str(index)],
            )
            for index, plan in enumerate(plans)
        ]
        return _summary(operation, results)
