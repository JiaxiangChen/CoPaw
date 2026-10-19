# -*- coding: utf-8 -*-
"""持久受理批量删除；查询只读，后台按原身份及清单恢复。"""

import json

from .batch_delete import (
    BatchDeleteService,
    _item_result,
    _normalize,
    _summary,
)
from .contracts import ManagedCronNotFound
from .identity import CallerIdentity
from .operations import IDENTITY_FIELDS, JOURNAL, WHERE_JOB, utc_now
from .storage import update


class AsyncBatchDelete:
    def __init__(self, db, management):
        self.db = db
        self.sync = BatchDeleteService(db, management)

    async def submit(self, caller, parent_id, payload):
        operation = await self.sync._begin(
            caller,
            parent_id,
            _normalize(payload),
        )
        if operation["status"] == "failed":
            return await self.retry(
                caller,
                parent_id,
                operation["operation_id"],
            )
        return await self._view(operation)

    async def get(self, caller, parent_id, operation_id):
        operation = await self.sync.operations.get(
            caller,
            parent_id,
            operation_id,
        )
        if operation["action"] != "batch_delete":
            raise ManagedCronNotFound("cron_batch_delete_not_found")
        return await self._view(operation)

    async def current(self, caller, parent_id):
        await self.sync.operations._authorize(self.db, caller, parent_id)
        row = await self.db.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE {WHERE_JOB} AND action='batch_delete' "
            "ORDER BY created_at DESC, operation_id DESC LIMIT 1",
            caller.job_key(parent_id),
        )
        return await self._view(row) if row else None

    async def retry(self, caller, parent_id, operation_id):
        await self.get(caller, parent_id, operation_id)
        await update(
            self.db,
            JOURNAL,
            {"status": "pending", "error_code": None, "updated_at": utc_now()},
            "operation_id=%s AND status='failed' AND lease_owner=''",
            (operation_id,),
        )
        return await self.get(caller, parent_id, operation_id)

    async def process(self, operation_id):
        operation = await self.db.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE operation_id=%s AND action='batch_delete'",
            (operation_id,),
        )
        if not operation or operation["status"] not in {"pending", "running"}:
            return
        stored = json.loads(operation["payload_json"])
        caller = CallerIdentity(
            operation["tenant_id"],
            operation["source_id"],
            operation["agent_id"],
            operation["actor_id"],
            bool(stored.get("actor_is_manager", False)),
        )
        payload = {
            "request_id": operation["request_id"],
            "items": [plan["item"] for plan in stored["plans"]],
        }
        if stored.get("all_children"):
            payload["all_children"] = True
        await self.sync.delete(caller, operation["job_id"], payload)

    async def _view(self, operation):
        stored = json.loads(operation["payload_json"])
        results = json.loads(operation["progress_json"])["results"]
        ids = [
            item["operation_id"]
            for item in results.values()
            if item["operation_id"]
        ]
        current = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            rows = await self.db.fetch_all(
                f"SELECT * FROM {JOURNAL} WHERE action='delete' AND operation_id IN ("
                + ",".join("%s" for _ in chunk)
                + ")",
                tuple(chunk),
            )
            current.update((row["operation_id"], row) for row in rows)
        values = []
        for index, plan in enumerate(stored["plans"]):
            cached = results.get(str(index))
            if cached is None:
                continue
            child = current.get(cached["operation_id"])
            if child:
                child = {**child, "logical_delete_applied": True}
                cached = _item_result(
                    tuple(child[name] for name in IDENTITY_FIELDS),
                    child,
                )
            values.append(cached)
        result = _summary(operation, values)
        result.update(
            operation_id=operation["operation_id"],
            operation_status=operation["status"],
            error_code=operation.get("error_code"),
            all_children=bool(stored.get("all_children")),
            total_count=len(stored["plans"]),
        )
        result["pending_count"] += len(stored["plans"]) - len(values)
        if operation["status"] != "completed":
            result["status"] = operation["status"]
        return result
