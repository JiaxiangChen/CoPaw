# -*- coding: utf-8 -*-
"""持久化整组三态目标和逐对象同步进度，不把失败写成任务禁止触发。"""

import json
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from .definition import canonical, request_hash, stable_id
from .distribution import TASKS, _children
from .operations import (
    JOURNAL,
    WHERE_JOB,
    IDENTITY_FIELDS,
    ManagementConflict,
    ManagementStore,
    utc_now,
)
from .repository import ManagedCronRepository
from .storage import insert, transaction, update

CONTROLS = "swe_cron_managed_dispatch_controls"
MODE_ITEMS = "swe_cron_managed_mode_items"
STATES = {"ordinary", "batch_enabled", "batch_paused"}


class DispatchStateStore:
    def __init__(self, db):
        self.db = db

    async def root(self, caller, job_id):
        row = await ManagedCronRepository(self.db)._authorized_row(
            caller,
            job_id,
            True,
        )
        if row["deleted_at"] or row.get("parent_job_id"):
            raise ManagementConflict("cron_dispatch_state_requires_source")
        return row

    async def get(self, caller, job_id):
        root = await self.root(caller, job_id)
        key = caller.job_key(job_id)
        control = await self.db.fetch_one(
            f"SELECT * FROM {CONTROLS} WHERE {WHERE_JOB}",
            key,
        )
        objects = [root, *await _children(self.db, key, [])]
        modes = []
        for item in objects:
            state = await self.db.fetch_one(
                f"SELECT dispatch_state FROM swe_cron_job_states WHERE {WHERE_JOB}",
                tuple(item[name] for name in IDENTITY_FIELDS),
            )
            modes.append(state["dispatch_state"])
        effective = modes[0] if len(set(modes)) == 1 else "mixed"
        counts = {}
        if control and control["operation_id"]:
            rows = await self.db.fetch_all(
                f"SELECT status, COUNT(*) AS count FROM {MODE_ITEMS} WHERE operation_id=%s GROUP BY status",
                (control["operation_id"],),
            )
            counts = {row["status"]: row["count"] for row in rows}
        return {
            "job_id": job_id,
            "operation_id": control["operation_id"] if control else None,
            "state": (
                control["state"] if control else root["state_dispatch_state"]
            ),
            "version": control["version"] if control else 1,
            "effective_state": effective,
            "sync_status": control["sync_status"] if control else "completed",
            "completed_count": counts.get(
                "completed",
                len(objects) if not control else 0,
            ),
            "pending_count": counts.get("pending", 0)
            + counts.get("running", 0),
            "failed_count": counts.get("failed", 0),
            "error_code": control["error_code"] if control else None,
        }

    async def begin(self, caller, job_id, payload):
        if payload["state"] not in STATES:
            raise ValueError("invalid_dispatch_state")
        key = caller.job_key(job_id)
        operation_id = stable_id(
            caller,
            payload["request_id"],
            "dispatch_state:" + job_id,
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
            if (
                root["deleted_at"]
                or root.get("parent_job_id")
                or root["state_management_status"] != "ready"
            ):
                raise ManagementConflict(
                    "cron_dispatch_state_source_not_ready",
                )
            existing = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id=%s",
                (operation_id,),
            )
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise ManagementConflict("cron_request_content_conflict")
                return existing["operation_id"]
            control = await tx.fetch_one(
                f"SELECT * FROM {CONTROLS} WHERE {WHERE_JOB}",
                key,
            )
            if control and control["sync_status"] != "completed":
                if control["state"] == payload["state"]:
                    return control["operation_id"]
                raise ManagementConflict("cron_dispatch_switch_in_progress")
            version = control["version"] if control else 1
            if version != payload["expected_version"]:
                raise ManagementConflict("cron_dispatch_version_conflict")
            await self._exclusive(tx, key)
            operation = self._operation(
                caller,
                root,
                payload,
                operation_id,
                fingerprint,
            )
            await insert(tx, JOURNAL, operation)
            values = {
                "state": payload["state"],
                "version": version + 1,
                "operation_id": operation_id,
                "sync_status": "pending",
                "error_code": None,
            }
            if control:
                await update(tx, CONTROLS, values, WHERE_JOB, key)
            else:
                await insert(
                    tx,
                    CONTROLS,
                    {**dict(zip(IDENTITY_FIELDS, key)), **values},
                )
            children = await _children(tx, key, [])
            for index, item in enumerate([root, *children], 1):
                item_key = tuple(item[name] for name in IDENTITY_FIELDS)
                await insert(
                    tx,
                    MODE_ITEMS,
                    {
                        "operation_id": operation_id,
                        "index_no": index,
                        **dict(zip(IDENTITY_FIELDS, item_key)),
                        "expected_definition_version": item[
                            "definition_version"
                        ],
                        "sync_operation_id": str(
                            uuid5(
                                NAMESPACE_URL,
                                canonical([operation_id, item_key]),
                            ),
                        ),
                        "status": "pending",
                        "error_code": None,
                    },
                )
        return operation_id

    @staticmethod
    async def _exclusive(tx, key):
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

    @staticmethod
    def _operation(caller, root, payload, operation_id, fingerprint):
        now = utc_now()
        return {
            "operation_id": operation_id,
            "active_key": request_hash(caller.job_key(root["job_id"])),
            **{name: root[name] for name in IDENTITY_FIELDS},
            "actor_id": caller.actor_id,
            "action": "dispatch_state",
            "request_id": payload["request_id"],
            "request_hash": fingerprint,
            "expected_version": root["definition_version"],
            "payload_json": canonical(
                {
                    "state": payload["state"],
                    "actor_is_manager": caller.is_manager,
                    "root_spec": json.loads(root["spec_json"]),
                },
            ),
            "progress_json": "{}",
            "status": "pending",
            "stage": "platform",
            "error_code": None,
            "lease_owner": "",
            "lease_until": None,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }

    async def checkpoint(self, operation, progress):
        async with transaction(self.db) as tx:
            await ManagementStore(tx)._fence(tx, operation)
            values = {
                "progress_json": canonical(progress),
                "version": operation["version"] + 1,
                "updated_at": utc_now(),
                "lease_until": utc_now() + timedelta(seconds=120),
            }
            await update(
                tx,
                JOURNAL,
                values,
                "operation_id=%s AND lease_owner=%s",
                (operation["operation_id"], operation["lease_owner"]),
            )
            operation.update(values)

    async def finish(self, operation, *, failed, error=None):
        key = tuple(operation[name] for name in IDENTITY_FIELDS)
        async with transaction(self.db) as tx:
            await ManagementStore(tx)._fence(tx, operation)
            values = {
                "status": "failed" if failed else "completed",
                "stage": "platform" if failed else "completed",
                "active_key": operation["active_key"] if failed else None,
                "lease_owner": "",
                "lease_until": None,
                "error_code": error,
                "version": operation["version"] + 1,
                "updated_at": utc_now(),
            }
            await update(
                tx,
                JOURNAL,
                values,
                "operation_id=%s AND lease_owner=%s",
                (operation["operation_id"], operation["lease_owner"]),
            )
            await update(
                tx,
                CONTROLS,
                {
                    "sync_status": "failed" if failed else "completed",
                    "error_code": error,
                },
                WHERE_JOB,
                key,
            )
