# -*- coding: utf-8 -*-
"""异步切换源任务及关联对象的三态，失败保留原目标并允许原方式继续。"""

import copy
import json
import logging

from .definition import canonical, request_hash
from .dispatch_state_store import CONTROLS, MODE_ITEMS, DispatchStateStore
from .identity import CallerIdentity
from .operations import (
    JOURNAL,
    WHERE_JOB,
    IDENTITY_FIELDS,
    LeaseLost,
    ManagementConflict,
    ManagementStore,
    RegistrationUnknown,
    utc_now,
)
from .projection import write_projection
from .repository import ManagedCronRepository
from .scatter import shift_cron_expression
from .storage import insert, transaction, update

logger = logging.getLogger(__name__)


class DispatchStateService:
    def __init__(self, db, platform):
        self.db = db
        self.platform = platform
        self.store = DispatchStateStore(db)

    async def get(self, caller, job_id):
        return await self.store.get(caller, job_id)

    async def set(self, caller, job_id, payload):
        operation_id = await self.store.begin(caller, job_id, payload)
        logger.info(
            "三态切换目标已持久受理 operation_id=%s job_id=%s state=%s",
            operation_id,
            job_id,
            payload["state"],
        )
        return await self.get(caller, job_id)

    async def retry(self, caller, job_id, operation_id):
        operation = await ManagementStore(self.db).get(
            caller,
            job_id,
            operation_id,
        )
        if operation["action"] != "dispatch_state":
            raise ManagementConflict("invalid_dispatch_state_operation")
        await self.process(operation_id)
        return await self.get(caller, job_id)

    async def process(self, operation_id):
        original = await self.db.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE operation_id=%s",
            (operation_id,),
        )
        if original is None or original["action"] != "dispatch_state":
            return
        payload = json.loads(original["payload_json"])
        caller = CallerIdentity(
            original["tenant_id"],
            original["source_id"],
            original["agent_id"],
            original["actor_id"],
            bool(payload["actor_is_manager"]),
        )
        operation = await ManagementStore(self.db).claim(
            caller,
            original["job_id"],
            operation_id,
        )
        if operation is None:
            return
        progress = json.loads(operation["progress_json"])
        logger.info(
            "开始处理三态切换 operation_id=%s state=%s",
            operation_id,
            payload["state"],
        )
        try:
            if payload["state"] != "ordinary":
                await self._group_batch(operation, progress, payload["state"])
            items = await self.db.fetch_all(
                f"SELECT * FROM {MODE_ITEMS} WHERE operation_id=%s AND status<>'completed' ORDER BY index_no",
                (operation_id,),
            )
            for item in items:
                await self._one(operation, item, payload["state"])
                await self.store.checkpoint(operation, progress)
            failures = await self.db.fetch_one(
                f"SELECT COUNT(*) AS count FROM {MODE_ITEMS} WHERE operation_id=%s AND status<>'completed'",
                (operation_id,),
            )
            if not failures["count"] and payload["state"] == "ordinary":
                root_state = await ManagementStore(self.db).state(operation)
                if root_state["batch_external_job_id"]:
                    await self.platform.set_enabled(
                        root_state["batch_external_job_id"],
                        False,
                    )
            await self.store.finish(
                operation,
                failed=bool(failures["count"]),
                error=(
                    "cron_mode_item_sync_failed" if failures["count"] else None
                ),
            )
        except LeaseLost:
            raise
        except Exception as exc:
            code = (
                "platform_registration_failed"
                if isinstance(exc, RegistrationUnknown)
                else "cron_mode_group_sync_failed"
            )
            logger.warning(
                "三态同步未完成，保持原方式可触发 operation_id=%s error_code=%s error_type=%s",
                operation_id,
                code,
                type(exc).__name__,
            )
            await self.store.finish(operation, failed=True, error=code)

    async def _group_batch(self, operation, progress, target):
        key = tuple(operation[name] for name in IDENTITY_FIELDS)
        root = await ManagedCronRepository(self.db)._get_row(
            key,
            include_deleted=False,
        )
        spec = copy.deepcopy(json.loads(root["spec_json"]))
        rows = await self.db.fetch_all(
            "SELECT offset_minutes FROM swe_cron_jobs_v2 WHERE parent_tenant_id=%s AND parent_source_id=%s AND parent_agent_id=%s AND parent_job_id=%s AND management_owner='scheduler' AND deleted_at IS NULL",
            key,
        )
        advance = max(
            (int(row["offset_minutes"] or 0) for row in rows),
            default=0,
        )
        if advance:
            shifted = shift_cron_expression(
                root["original_cron_expr"] or root["cron_expr"],
                root["timezone"],
                offset_minutes=advance,
            )
            if shifted.error:
                raise ValueError("batch_timer_cron_not_shiftable")
            spec["schedule"]["cron"] = shifted.cron
        progress["batch_spec"] = spec
        progress["batch_advance_minutes"] = advance
        await self.store.checkpoint(operation, progress)
        external_id = root["state_batch_external_job_id"] or progress.get(
            "external_id",
        )
        if not external_id:
            external_id = await self._register(
                operation,
                progress,
                spec,
                target,
                self.store.checkpoint,
            )
            async with transaction(self.db) as tx:
                await ManagementStore(tx)._fence(tx, operation)
                await tx.execute(
                    f"UPDATE swe_cron_job_states SET batch_external_job_id=%s, management_version=management_version+1 WHERE {WHERE_JOB}",
                    (external_id, *key),
                )
        else:
            await self.platform.update(external_id, operation, spec, target)
        await self.platform.set_enabled(external_id, target == "batch_enabled")

    async def _register(self, operation, progress, spec, target, save):
        if progress.get("registration_started"):
            logger.warning(
                "模式切换未保存平台注册ID，按未注册重新请求，可能遗留重复定时器 operation_id=%s job_id=%s",
                operation["operation_id"],
                operation["job_id"],
            )
        progress["registration_started"] = True
        await save(operation, progress)
        try:
            external_id = await self.platform.register(operation, spec, target)
        except Exception as exc:
            progress["registration_started"] = False
            await save(operation, progress)
            logger.warning(
                "模式切换未取得平台ID，按注册失败处理，原操作重试可能重复注册 operation_id=%s error_type=%s",
                operation["operation_id"],
                type(exc).__name__,
            )
            raise
        progress["external_id"] = external_id
        await save(operation, progress)
        return external_id

    async def _item_operation(self, parent, item):
        key = tuple(item[name] for name in IDENTITY_FIELDS)
        async with transaction(self.db) as tx:
            await ManagementStore(tx)._fence(tx, parent)
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            existing = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id=%s",
                (item["sync_operation_id"],),
            )
            if existing:
                return existing
            current = await ManagedCronRepository(tx)._get_row(
                key,
                include_deleted=True,
            )
            if (
                current["deleted_at"]
                or current["definition_version"]
                != item["expected_definition_version"]
            ):
                raise ManagementConflict("cron_mode_definition_changed")
            root_key = tuple(parent[name] for name in IDENTITY_FIELDS)
            if (
                key != root_key
                and tuple(
                    current.get(f"parent_{name}") for name in IDENTITY_FIELDS
                )
                != root_key
            ):
                raise ManagementConflict("cron_mode_child_not_from_source")
            active_key = None if key == root_key else request_hash(key)
            if active_key and await tx.fetch_one(
                f"SELECT operation_id FROM {JOURNAL} WHERE active_key=%s",
                (active_key,),
            ):
                raise ManagementConflict("cron_management_in_progress")
            now = utc_now()
            operation = {
                "operation_id": item["sync_operation_id"],
                "active_key": active_key,
                **dict(zip(IDENTITY_FIELDS, key)),
                "actor_id": parent["actor_id"],
                "action": "mode_sync",
                "request_id": item["sync_operation_id"],
                "request_hash": request_hash([parent["operation_id"], key]),
                "expected_version": current["definition_version"],
                "payload_json": canonical(
                    {
                        "spec": json.loads(current["spec_json"]),
                        "parent_operation_id": parent["operation_id"],
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
            await insert(tx, JOURNAL, operation)
            return operation

    async def _one(self, parent, item, target):
        try:
            async with transaction(self.db) as tx:
                await ManagementStore(tx)._fence(tx, parent)
            operation = await self._item_operation(parent, item)
            if operation["status"] == "completed":
                await self._item_result(parent, item, "completed", None)
                return
            progress = json.loads(operation["progress_json"])
            key = tuple(item[name] for name in IDENTITY_FIELDS)
            current = await ManagedCronRepository(self.db)._get_row(
                key,
                include_deleted=False,
            )
            state = await self.db.fetch_one(
                f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB}",
                key,
            )
            spec = json.loads(current["spec_json"])
            if target == "ordinary":
                external_id = state[
                    "ordinary_external_job_id"
                ] or progress.get("external_id")
                if not external_id:

                    async def save(child, child_progress):
                        await self._item_checkpoint(
                            parent,
                            child,
                            child_progress,
                        )

                    external_id = await self._register(
                        operation,
                        progress,
                        spec,
                        target,
                        save,
                    )
                else:
                    await self.platform.update(
                        external_id,
                        operation,
                        spec,
                        target,
                    )
                if state["ordinary_external_job_id"] != external_id:
                    # 已确认注册但尚未保存绑定时，重试也必须先补存再启用。
                    await self._save_binding(parent, item, external_id)
                await self.platform.set_enabled(
                    external_id,
                    bool(current["enabled"] and not state["pause_reason"]),
                )
            elif state["ordinary_external_job_id"]:
                await self.platform.set_enabled(
                    state["ordinary_external_job_id"],
                    False,
                )
            await self._publish(parent, item, operation, target)
        except LeaseLost:
            raise
        except Exception as exc:
            code = (
                "platform_registration_failed"
                if isinstance(exc, RegistrationUnknown)
                else "cron_mode_item_sync_failed"
            )
            logger.warning(
                "对象调度方式同步失败，保留原方式 operation_id=%s job_id=%s error_type=%s",
                parent["operation_id"],
                item["job_id"],
                type(exc).__name__,
            )
            async with transaction(self.db) as tx:
                await ManagementStore(tx)._fence(tx, parent)
                await update(
                    tx,
                    MODE_ITEMS,
                    {"status": "failed", "error_code": code},
                    "operation_id=%s AND index_no=%s",
                    (item["operation_id"], item["index_no"]),
                )

    async def _item_checkpoint(self, parent, operation, progress):
        async with transaction(self.db) as tx:
            await ManagementStore(tx)._fence(tx, parent)
            await update(
                tx,
                JOURNAL,
                {
                    "progress_json": canonical(progress),
                    "updated_at": utc_now(),
                },
                "operation_id=%s",
                (operation["operation_id"],),
            )

    async def _save_binding(self, parent, item, external_id):
        key = tuple(item[name] for name in IDENTITY_FIELDS)
        async with transaction(self.db) as tx:
            await ManagementStore(tx)._fence(tx, parent)
            current = await tx.fetch_one(
                f"SELECT definition_version, deleted_at FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            if (
                current is None
                or current["deleted_at"]
                or current["definition_version"]
                != item["expected_definition_version"]
            ):
                raise ManagementConflict("cron_mode_definition_changed")
            await tx.execute(
                f"UPDATE swe_cron_job_states SET ordinary_external_job_id=%s, management_version=management_version+1 WHERE {WHERE_JOB}",
                (external_id, *key),
            )

    async def _publish(self, parent, item, operation, target):
        key = tuple(item[name] for name in IDENTITY_FIELDS)
        async with transaction(self.db) as tx:
            await ManagementStore(tx)._fence(tx, parent)
            current = await tx.fetch_one(
                f"SELECT * FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            if (
                current["deleted_at"]
                or current["definition_version"]
                != operation["expected_version"]
            ):
                raise ManagementConflict("cron_mode_definition_changed")
            await tx.execute(
                f"UPDATE swe_cron_job_states SET dispatch_state=%s, management_version=management_version+1 WHERE {WHERE_JOB}",
                (target, *key),
            )
            state = await tx.fetch_one(
                f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB}",
                key,
            )
            await write_projection(
                tx,
                json.loads(current["spec_json"]),
                state,
                item["agent_id"],
                utc_now(),
            )
            await update(
                tx,
                JOURNAL,
                {
                    "status": "completed",
                    "stage": "completed",
                    "active_key": None,
                    "error_code": None,
                },
                "operation_id=%s",
                (operation["operation_id"],),
            )
            await update(
                tx,
                MODE_ITEMS,
                {"status": "completed", "error_code": None},
                "operation_id=%s AND index_no=%s",
                (item["operation_id"], item["index_no"]),
            )

    async def _item_result(self, parent, item, status, code):
        async with transaction(self.db) as tx:
            await ManagementStore(tx)._fence(tx, parent)
            await update(
                tx,
                MODE_ITEMS,
                {"status": status, "error_code": code},
                "operation_id=%s AND index_no=%s",
                (item["operation_id"], item["index_no"]),
            )
