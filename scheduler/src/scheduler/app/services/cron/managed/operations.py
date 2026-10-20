# -*- coding: utf-8 -*-
"""在共用数据库保存管理步骤，并通过行锁和租约围栏协调并发。"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .contracts import ManagedCronNotFound
from .definition import (
    canonical,
    normalize_spec,
    profile_columns,
    request_hash,
    stable_id,
)
from .projection import write_projection
from .repository import IDENTITY_FIELDS, ManagedCronRepository
from .storage import insert, transaction, update
from .run_accounting import occupied_run_sql

JOURNAL = "swe_cron_registration_journal"
WHERE_JOB = " AND ".join(f"`{key}` = %s" for key in IDENTITY_FIELDS)


class ManagementConflict(ValueError):
    """存在并发管理操作或请求内容冲突。"""


class RegistrationUnknown(RuntimeError):
    """平台未返回可保存的注册ID，调用方按未注册失败处理。"""


class RegistrationRejected(RuntimeError):
    """调度平台明确拒绝了请求，尚未受理注册。"""


class LeaseLost(RuntimeError):
    """只有当前操作租约持有者可以提交步骤进度。"""


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _time(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def operation_summary(row):
    result = {
        key: row.get(key)
        for key in (
            "operation_id",
            "job_id",
            "action",
            "status",
            "stage",
            "error_code",
            "expected_version",
            "version",
        )
    }
    result["logical_delete_applied"] = row["action"] == "delete"
    return result


class ManagementStore:
    def __init__(self, db, *, grant=None):
        self.db = db
        self.grant = grant

    async def has_parent(self, operation):
        """平台入口归属取数据库关联，不信任请求元数据里的来源标记。"""
        row = await self.db.fetch_one(
            f"SELECT parent_job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB}",
            tuple(operation[name] for name in IDENTITY_FIELDS),
        )
        return row is not None and bool(row["parent_job_id"])

    async def has_running_execution(self, operation):
        key = tuple(operation[name] for name in IDENTITY_FIELDS)
        row = await self.db.fetch_one(
            f"SELECT COUNT(*) AS count FROM swe_cron_execution_runs WHERE {WHERE_JOB} AND {occupied_run_sql()}",
            key,
        )
        return bool(row["count"])

    async def begin(self, caller, action, payload, job_id=None):
        for attempt in range(3):
            try:
                return await self._begin(caller, action, payload, job_id)
            except Exception as exc:
                code = exc.args[0] if exc.args else None
                if code not in (1062, 1205, 1213) or attempt == 2:
                    raise
                await asyncio.sleep(0)
        raise RuntimeError("unreachable_cron_transaction_retry")

    async def _begin(self, caller, action, payload, job_id=None):
        op_id = stable_id(caller, payload["request_id"], action)
        job_id = job_id or stable_id(caller, payload["request_id"], "job")
        fingerprint = request_hash(
            {"action": action, "job_id": job_id, "payload": payload},
        )
        async with transaction(self.db) as tx:
            if self.grant is not None:
                await self.grant.validate(
                    tx,
                    caller.job_key(job_id),
                    caller.actor_id,
                )
            previous = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id = %s FOR UPDATE",
                (op_id,),
            )
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise ManagementConflict("cron_request_content_conflict")
                await self._authorize(tx, caller, job_id)
                return previous, True
            current = await self._begin_job(
                tx,
                caller,
                action,
                payload,
                job_id,
            )
            if current is not None and current.get("deleted_at"):
                return (
                    await self._existing_delete(tx, caller.job_key(job_id)),
                    True,
                )
            key = caller.job_key(job_id)
            active_key = request_hash(key)
            active = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE active_key = %s",
                (active_key,),
            )
            superseded = await self._supersede_failed(tx, active, action)
            spec = (
                normalize_spec(
                    payload.get("spec", {}),
                    caller,
                    job_id,
                    current,
                )
                if action != "delete"
                else json.loads(current["spec_json"])
            )
            if self.grant is not None:
                spec = await self.grant.rewrite_spec(tx, spec, current)
            now = utc_now()
            operation = {
                "operation_id": op_id,
                "active_key": active_key,
                **dict(zip(IDENTITY_FIELDS, key)),
                "actor_id": caller.actor_id,
                "action": action,
                "request_id": payload["request_id"],
                "request_hash": fingerprint,
                "expected_version": (
                    current["definition_version"] if current else 1
                ),
                "payload_json": canonical(
                    {
                        "spec": spec,
                        "actor_is_manager": caller.is_manager,
                        "dispatch_state": (
                            payload.get("dispatch_state", "ordinary")
                            if current is None
                            else current.get("state_dispatch_state")
                            or "ordinary"
                        ),
                        **(
                            await self.grant.operation_payload(tx)
                            if self.grant is not None
                            else {}
                        ),
                    },
                ),
                "progress_json": canonical({"prior_operations": superseded}),
                "status": "pending",
                "stage": "stop" if action == "delete" else "prepare",
                "error_code": None,
                "lease_owner": "",
                "lease_until": None,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
            await insert(tx, JOURNAL, operation)
            if current is None:
                await self._insert_job(tx, caller, spec, payload, now)
            elif action == "delete":
                await self._delete_job(tx, caller, current, now)
            await self._state(
                tx,
                key,
                {
                    "management_status": {
                        "create": "creating",
                        "update": "updating",
                        "delete": "deleting",
                    }[action],
                },
                now,
            )
            return operation, False

    async def _supersede_failed(self, tx, active, action):
        if active is None:
            return []
        if (
            (
                action != "delete"
                and not (self.grant is not None and action == "update")
            )
            or active["status"] != "failed"
            or active["action"]
            not in {"create", "update", "set_enabled", "auto_pause"}
        ):
            raise ManagementConflict("cron_management_in_progress")
        await update(
            tx,
            JOURNAL,
            {
                "status": "superseded",
                "active_key": None,
                "lease_owner": "",
                "lease_until": None,
                "error_code": "superseded_by_delete",
                "version": active["version"] + 1,
            },
            "operation_id=%s AND version=%s",
            (active["operation_id"], active["version"]),
        )
        names = (
            "operation_id",
            "tenant_id",
            "source_id",
            "agent_id",
            "job_id",
            "actor_id",
            "payload_json",
            "progress_json",
        )
        return [{name: active[name] for name in names}]

    async def _authorize(self, tx, caller, job_id):
        if self.grant is not None:
            return await self.grant.authorize_row(tx, caller, job_id)
        return await ManagedCronRepository(tx)._authorized_row(
            caller,
            job_id,
            True,
        )

    @staticmethod
    async def _existing_delete(tx, key):
        row = await tx.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE {WHERE_JOB} AND action='delete' "
            "AND status<>'superseded' LIMIT 1",
            key,
        )
        if row is None:
            raise ManagementConflict("cron_delete_operation_missing")
        return row

    async def _begin_job(self, tx, caller, action, payload, job_id):
        row = await tx.fetch_one(
            f"SELECT * FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
            caller.job_key(job_id),
        )
        if action == "create":
            if (
                self.grant is None
                and caller.tenant_id != caller.actor_id
                and not caller.is_manager
            ):
                raise ManagedCronNotFound("cron_workspace_not_owned")
            if row:
                raise ManagementConflict("cron_job_already_exists")
            if await tx.fetch_one(
                "SELECT id FROM swe_cron_jobs WHERE id=%s FOR UPDATE",
                (job_id,),
            ):
                raise ManagementConflict("legacy_job_id_conflict")
            return None
        row = await self._authorize(tx, caller, job_id)
        if row.get("deleted_at") and action != "delete":
            raise ManagementConflict("cron_job_deleted")
        if row.get("deleted_at"):
            return row
        if row["definition_version"] != payload["expected_definition_version"]:
            raise ManagementConflict("cron_definition_version_conflict")
        # 源定义锁与分发受理共用，先检查活动分发再修改内容或写删除标记。
        active_broadcast = await tx.fetch_one(
            "SELECT task_id FROM swe_cron_managed_broadcast_tasks WHERE active_key=%s",
            (request_hash(caller.job_key(job_id)),),
        )
        if active_broadcast:
            raise ManagementConflict("cron_broadcast_in_progress")
        if action == "update":
            children = await tx.fetch_one(
                "SELECT job_id FROM swe_cron_jobs_v2 WHERE parent_tenant_id=%s "
                "AND parent_source_id=%s AND parent_agent_id=%s AND parent_job_id=%s "
                "AND deleted_at IS NULL LIMIT 1",
                caller.job_key(job_id),
            )
            if children:
                raise ManagementConflict("cron_source_has_children")
        return row

    async def _insert_job(self, tx, caller, spec, payload, now):
        key = dict(zip(IDENTITY_FIELDS, caller.job_key(spec["id"])))
        values = self.definition_values(spec)
        await insert(
            tx,
            "swe_cron_jobs_v2",
            {
                **key,
                **values,
                "enabled": False,
                "definition_version": 1,
                "management_owner": "scheduler",
                "job_origin": "manual",
                "creator_user_id": spec["meta"]["creator_user_id"],
                **(
                    self.grant.definition_values()
                    if self.grant is not None
                    else {}
                ),
                "created_at": now,
                "updated_at": now,
            },
        )
        await insert(
            tx,
            "swe_cron_job_states",
            {
                **key,
                "dispatch_state": payload.get("dispatch_state", "ordinary"),
                "management_status": "creating",
                "management_version": 1,
                "state_version": 1,
                "state_source": "scheduler",
                "unread_execution_count": 0,
                "has_scheduled_result": False,
                "created_at": now,
                "updated_at": now,
            },
        )

    async def _delete_job(self, tx, caller, current, now):
        key = caller.job_key(current["job_id"])
        await update(
            tx,
            "swe_cron_jobs_v2",
            {
                "deleted_at": now,
                "enabled": False,
                "definition_version": current["definition_version"] + 1,
                "updated_at": now,
            },
            WHERE_JOB,
            key,
        )
        state = await tx.fetch_one(
            f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB} FOR UPDATE",
            key,
        )
        receipt = await tx.fetch_one(
            f"SELECT operation_id FROM {JOURNAL} WHERE {WHERE_JOB} AND action='create' AND status='completed' LIMIT 1",
            key,
        )
        if receipt is None:
            return
        await write_projection(
            tx,
            json.loads(current["spec_json"]),
            state or {},
            caller.agent_id,
            now,
            deleted=True,
        )

    @staticmethod
    def definition_values(spec):
        return {
            **profile_columns(spec),
            "name": spec["name"],
            "scope_id": spec["scope_id"],
            "task_type": spec["task_type"],
            "enabled": spec["enabled"],
            "cron_expr": spec["schedule"]["cron"],
            "original_cron_expr": spec["schedule"]["cron"],
            "timezone": spec["schedule"]["timezone"],
            "spec_json": canonical(spec),
            "plan_id": spec.get("plan_id"),
        }

    @staticmethod
    async def _state(tx, key, values, now):
        affected = await tx.execute(
            "UPDATE swe_cron_job_states SET "
            + ", ".join(f"`{name}`=%s" for name in values)
            + ", management_version=management_version+1, management_updated_at=%s "
            + f"WHERE {WHERE_JOB}",
            (*values.values(), now, *key),
        )
        if affected != 1:
            raise ManagementConflict("cron_state_record_missing")

    async def get(self, caller, job_id, operation_id):
        await self._authorize(self.db, caller, job_id)
        row = await self.db.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE operation_id=%s AND {WHERE_JOB}",
            (operation_id, *caller.job_key(job_id)),
        )
        if row is None:
            raise ManagedCronNotFound("cron_operation_not_found")
        return row

    async def pending(self, caller, job_id):
        await self._authorize(self.db, caller, job_id)
        return await self.db.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE active_key=%s",
            (request_hash(caller.job_key(job_id)),),
        )

    async def claim(self, caller, job_id, operation_id):
        async with transaction(self.db) as tx:
            await self._authorize(tx, caller, job_id)
            row = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id=%s AND {WHERE_JOB} FOR UPDATE",
                (operation_id, *caller.job_key(job_id)),
            )
            if row is None:
                raise ManagedCronNotFound("cron_operation_not_found")
            if row["status"] in {"completed", "superseded"}:
                return None
            now = utc_now()
            if row.get("lease_owner") and _time(row["lease_until"]) > now:
                return None
            row.update(
                lease_owner=str(uuid4()),
                lease_until=now + timedelta(seconds=120),
                status="running",
                error_code=None,
            )
            await update(
                tx,
                JOURNAL,
                {
                    name: row[name]
                    for name in (
                        "lease_owner",
                        "lease_until",
                        "status",
                        "error_code",
                    )
                },
                "operation_id=%s",
                (operation_id,),
            )
            return row

    async def checkpoint(
        self,
        operation,
        stage,
        progress,
        *,
        status="running",
        error=None,
    ):
        now = utc_now()
        values = {
            "stage": stage,
            "progress_json": canonical(progress),
            "status": status,
            "error_code": error,
            "updated_at": now,
            "version": operation["version"] + 1,
        }
        if status != "running":
            values.update(lease_owner="", lease_until=None)
        else:
            values["lease_until"] = now + timedelta(seconds=120)
        async with transaction(self.db) as tx:
            if self.grant is not None:
                await self.grant.validate(
                    tx,
                    tuple(operation[name] for name in IDENTITY_FIELDS),
                    operation["actor_id"],
                )
            count = await update(
                tx,
                JOURNAL,
                values,
                "operation_id=%s AND lease_owner=%s AND version=%s",
                (
                    operation["operation_id"],
                    operation["lease_owner"],
                    operation["version"],
                ),
            )
            if count != 1:
                raise LeaseLost("cron_operation_lease_lost")
            operation.update(values)
            if status == "failed":
                key = tuple(operation[name] for name in IDENTITY_FIELDS)
                await self._state(
                    tx,
                    key,
                    {"management_status": f"{operation['action']}_failed"},
                    now,
                )

    async def save_preparation(self, operation, prepared):
        async with transaction(self.db) as tx:
            await self._fence(tx, operation)
            key = tuple(operation[name] for name in IDENTITY_FIELDS)
            if operation["action"] == "create":
                await update(
                    tx,
                    "swe_cron_jobs_v2",
                    {
                        **profile_columns(prepared["spec"]),
                        "spec_json": canonical(prepared["spec"]),
                        "updated_at": utc_now(),
                    },
                    WHERE_JOB + " AND deleted_at IS NULL",
                    key,
                )
            await self._state(
                tx,
                key,
                {
                    "task_chat_id": prepared["chat_id"],
                    "task_session_id": prepared["session_id"],
                },
                utc_now(),
            )

    async def save_binding(self, operation, progress, mode):
        async with transaction(self.db) as tx:
            await self._fence(tx, operation)
            column = (
                "ordinary_external_job_id"
                if mode == "ordinary"
                else "batch_external_job_id"
            )
            await self._state(
                tx,
                tuple(operation[key] for key in IDENTITY_FIELDS),
                {column: progress["external_id"]},
                utc_now(),
            )

    async def state(self, operation):
        return await self.db.fetch_one(
            f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB}",
            tuple(operation[key] for key in IDENTITY_FIELDS),
        )

    async def finish(self, operation, progress):
        async with transaction(self.db) as tx:
            await self._fence(tx, operation)
            key = tuple(operation[name] for name in IDENTITY_FIELDS)
            if operation["action"] != "delete":
                spec = progress["prepared"]["spec"]
                version = operation["expected_version"] + (
                    operation["action"] == "update"
                )
                values = {
                    **self.definition_values(spec),
                    **(
                        self.grant.definition_values()
                        if self.grant is not None
                        else {}
                    ),
                    "definition_version": version,
                    "updated_at": utc_now(),
                }
                count = await update(
                    tx,
                    "swe_cron_jobs_v2",
                    values,
                    WHERE_JOB
                    + " AND deleted_at IS NULL AND definition_version=%s",
                    (*key, operation["expected_version"]),
                )
                if count != 1:
                    raise ManagementConflict(
                        "cron_definition_version_conflict",
                    )
                binding = progress["prepared"]
                await self._state(
                    tx,
                    key,
                    {
                        "task_chat_id": binding["chat_id"],
                        "task_session_id": binding["session_id"],
                        "management_status": "ready",
                    },
                    utc_now(),
                )
                state = await tx.fetch_one(
                    f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB}",
                    key,
                )
                await write_projection(
                    tx,
                    spec,
                    state,
                    operation["agent_id"],
                    utc_now(),
                    expect_new=operation["action"] == "create"
                    or json.loads(operation["payload_json"]).get(
                        "projection_expect_new",
                        False,
                    ),
                )
            else:
                await self._state(
                    tx,
                    key,
                    {"management_status": "deleted"},
                    utc_now(),
                )
            count = await update(
                tx,
                JOURNAL,
                {
                    "status": "completed",
                    "stage": "completed",
                    "active_key": None,
                    "progress_json": canonical(progress),
                    "lease_owner": "",
                    "lease_until": None,
                    "error_code": None,
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
            if count != 1:
                raise LeaseLost("cron_operation_lease_lost")

    async def _fence(self, tx, operation):
        if self.grant is not None:
            await self.grant.validate(
                tx,
                tuple(operation[name] for name in IDENTITY_FIELDS),
                operation["actor_id"],
            )
        row = await tx.fetch_one(
            f"SELECT lease_owner, version FROM {JOURNAL} WHERE operation_id=%s FOR UPDATE",
            (operation["operation_id"],),
        )
        if (
            not row
            or row["lease_owner"] != operation["lease_owner"]
            or row["version"] != operation["version"]
        ):
            raise LeaseLost("cron_operation_lease_lost")
