# -*- coding: utf-8 -*-
"""持久受理整组暂停，先使旧队列失效，再按冻结清单关闭平台入口。"""

import json
import logging
from uuid import NAMESPACE_URL, uuid5

from .controls import _journal
from .date_ranges import _lock_definition
from .definition import canonical, request_hash, stable_id
from .diagnostics import format_management_error
from .dispatch_state_store import CONTROLS, MODE_ITEMS, DispatchStateStore
from .distribution import _children
from .identity import CallerIdentity
from .operations import (
    IDENTITY_FIELDS,
    JOURNAL,
    WHERE_JOB,
    ManagementConflict,
    ManagementStore,
    operation_summary,
    utc_now,
)
from .projection import write_projection
from .repository import ManagedCronRepository
from .storage import insert, transaction, update

logger = logging.getLogger(__name__)


class PauseGroupService:
    """只暂停选定任务树，不更换身份、不注册新定时器。"""

    def __init__(self, db, platform):
        self.db, self.platform = db, platform
        self.operations = ManagementStore(db)

    async def submit(self, caller, job_id, payload):
        key = caller.job_key(job_id)
        async with transaction(self.db) as tx:
            await _lock_definition(tx, key)
            current = await ManagementStore(tx)._authorize(tx, caller, job_id)
            operation = _journal(caller, current, "pause_group", payload)
            operation["operation_id"] = stable_id(
                caller,
                payload["request_id"],
                "pause_group:" + job_id,
            )
            previous = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id=%s",
                (operation["operation_id"],),
            )
            if previous:
                if previous["request_hash"] != operation["request_hash"]:
                    raise ManagementConflict("cron_request_content_conflict")
                return operation_summary(previous)
            if (
                payload["expected_definition_version"]
                != current["definition_version"]
            ):
                raise ManagementConflict("cron_definition_version_conflict")
            members = await self._members(tx, current)
            batch_group = not current.get("parent_job_id") and current[
                "state_dispatch_state"
            ] in {"batch_enabled", "batch_paused"}
            operation.update(
                active_key=request_hash(key),
                status="pending",
                stage="platform",
                payload_json=canonical(
                    {
                        **payload,
                        "actor_is_manager": caller.is_manager,
                        "batch_group": batch_group,
                    },
                ),
                progress_json=canonical({"affected_count": len(members)}),
            )
            await insert(tx, JOURNAL, operation)
            for index, (row, state) in enumerate(members, 1):
                await self._pause_member(
                    tx,
                    operation,
                    row,
                    state,
                    index,
                    batch_group,
                )
            if batch_group:
                await self._batch_target(tx, key, operation)
        return operation_summary(operation)

    async def _members(self, tx, current):
        key = tuple(current[name] for name in IDENTITY_FIELDS)
        result = []
        for ref in [current, *await _children(tx, key, [])]:
            item_key = tuple(ref[name] for name in IDENTITY_FIELDS)
            await _lock_definition(tx, item_key)
            await DispatchStateStore._exclusive(tx, item_key)
            row = await ManagedCronRepository(tx)._get_row(item_key, True)
            state = await tx.fetch_one(
                f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB} FOR UPDATE",
                item_key,
            )
            if state is None or state["management_status"] != "ready":
                raise ManagementConflict("cron_job_not_ready")
            result.append((row, state))
        return result

    @staticmethod
    async def _pause_member(tx, operation, row, state, index, batch_group):
        key = tuple(row[name] for name in IDENTITY_FIELDS)
        spec = json.loads(row["spec_json"])
        spec["enabled"] = False
        await update(
            tx,
            "swe_cron_jobs_v2",
            {
                "enabled": False,
                "spec_json": canonical(spec),
                "updated_at": utc_now(),
            },
            WHERE_JOB,
            key,
        )
        values = {
            "pause_reason": "manual",
            "auto_paused_at": None,
            "queue_invalidation_version": state["state_version"] + 1,
            "state_version": state["state_version"] + 1,
            "management_version": state["management_version"] + 1,
            "updated_at": utc_now(),
        }
        if batch_group:
            values["dispatch_state"] = "batch_paused"
        await update(tx, "swe_cron_job_states", values, WHERE_JOB, key)
        await write_projection(
            tx,
            spec,
            {**state, **values},
            key[2],
            utc_now(),
        )
        await insert(
            tx,
            MODE_ITEMS,
            {
                "operation_id": operation["operation_id"],
                "index_no": index,
                **dict(zip(IDENTITY_FIELDS, key)),
                "expected_definition_version": row["definition_version"],
                "sync_operation_id": str(
                    uuid5(
                        NAMESPACE_URL,
                        canonical([operation["operation_id"], key]),
                    ),
                ),
                "status": "pending",
                "error_code": None,
            },
        )

    @staticmethod
    async def _batch_target(tx, key, operation):
        control = await tx.fetch_one(
            f"SELECT * FROM {CONTROLS} WHERE {WHERE_JOB}",
            key,
        )
        values = {
            "state": "batch_paused",
            "version": control["version"] + 1 if control else 2,
            "operation_id": operation["operation_id"],
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

    async def get(self, caller, job_id, operation_id):
        operation = await self.operations.get(caller, job_id, operation_id)
        if operation["action"] != "pause_group":
            raise ManagementConflict("invalid_cron_pause_operation")
        return operation_summary(operation)

    async def retry(self, caller, job_id, operation_id):
        await self.get(caller, job_id, operation_id)
        await update(
            self.db,
            JOURNAL,
            {"status": "pending", "error_code": None, "updated_at": utc_now()},
            "operation_id=%s AND status='failed' AND lease_owner=''",
            (operation_id,),
        )
        return await self.get(caller, job_id, operation_id)

    async def process(self, operation_id):
        row = await self.db.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE operation_id=%s AND action='pause_group'",
            (operation_id,),
        )
        if row is None or row["status"] not in {"pending", "running"}:
            return
        body = json.loads(row["payload_json"])
        caller = CallerIdentity(
            *(row[name] for name in IDENTITY_FIELDS[:3]),
            row["actor_id"],
            body["actor_is_manager"],
        )
        operation = await self.operations.claim(
            caller,
            row["job_id"],
            operation_id,
        )
        if operation is None:
            return
        items = await self.db.fetch_all(
            f"SELECT * FROM {MODE_ITEMS} WHERE operation_id=%s AND status<>'completed' ORDER BY index_no",
            (operation_id,),
        )
        failed = False
        for item in items:
            error = await self._sync_item(item)
            failed = failed or error is not None
            async with transaction(self.db) as tx:
                await self.operations._fence(tx, operation)
                await update(
                    tx,
                    MODE_ITEMS,
                    {
                        "status": "failed" if error else "completed",
                        "error_code": error,
                    },
                    "operation_id=%s AND index_no=%s",
                    (operation_id, item["index_no"]),
                )
            await self.operations.checkpoint(
                operation,
                "platform",
                json.loads(operation["progress_json"]),
            )
        await self._finish(operation, failed, body["batch_group"])

    async def _sync_item(self, item):
        try:
            row = await ManagedCronRepository(self.db)._get_row(
                tuple(item[name] for name in IDENTITY_FIELDS),
                True,
            )
            if row["deleted_at"]:
                return None
            if (
                row["definition_version"]
                != item["expected_definition_version"]
            ):
                raise ManagementConflict("cron_definition_version_conflict")
            external_ids = [row.get("state_ordinary_external_job_id")]
            if not row.get("parent_job_id"):
                external_ids.append(row.get("state_batch_external_job_id"))
            for external in dict.fromkeys(external_ids):
                if external:
                    await self.platform.set_enabled(external, False)
            return None
        except Exception as error:
            logger.error(
                "整组暂停平台同步失败 job_id=%s\n%s",
                item["job_id"],
                format_management_error(error),
            )
            return "cron_group_pause_sync_failed"

    async def _finish(self, operation, failed, batch_group):
        async with transaction(self.db) as tx:
            await self.operations._fence(tx, operation)
            error = "cron_group_pause_sync_failed" if failed else None
            await update(
                tx,
                JOURNAL,
                {
                    "status": "failed" if failed else "completed",
                    "stage": "platform" if failed else "completed",
                    "error_code": error,
                    "active_key": operation["active_key"] if failed else None,
                    "lease_owner": "",
                    "lease_until": None,
                    "version": operation["version"] + 1,
                    "updated_at": utc_now(),
                },
                "operation_id=%s AND lease_owner=%s",
                (operation["operation_id"], operation["lease_owner"]),
            )
            if batch_group:
                key = tuple(operation[name] for name in IDENTITY_FIELDS)
                await update(
                    tx,
                    CONTROLS,
                    {
                        "sync_status": "failed" if failed else "completed",
                        "error_code": error,
                    },
                    WHERE_JOB + " AND operation_id=%s",
                    (*key, operation["operation_id"]),
                )
