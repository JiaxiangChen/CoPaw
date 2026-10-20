# -*- coding: utf-8 -*-
"""持久化已经受理的执行工作，不扫描定义补生遗漏的触发。"""

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from .contracts import ManagedCronNotFound
from .definition import canonical, request_hash
from .identity import identity_value
from .operations import utc_now
from .repository import IDENTITY_FIELDS, ManagedCronRepository
from .storage import insert, transaction
from .views import public_value
from .dispatch_events import append_run_event
from ..workflow_binding import resolve_workflow_batch_jobs

RUN_TABLE = "swe_cron_execution_runs"
JOB_WHERE = " AND ".join(f"`{key}`=%s" for key in IDENTITY_FIELDS)
TERMINAL_STATUSES = frozenset({"success", "error", "skipped", "cancelled"})


class ExecutionConflict(ValueError):
    """任务当前状态不允许受理新的执行请求。"""


@dataclass(frozen=True)
class ExecutionRequest:
    kind: str
    trigger_key: str
    scheduled_at: datetime | None = None
    expected_definition_version: int | None = None
    dispatch_meta: dict | None = None
    not_before_at: datetime | None = None


def execution_summary(row):
    return {
        name: row.get(name)
        for name in (
            "execution_id",
            "job_id",
            "kind",
            "status",
            "definition_version",
            "accepted_at",
            "started_at",
            "finished_at",
            "error_code",
            "trace_id",
            "output_preview",
            "history_execution_id",
            "settled_status",
            "settled_at",
            "settled_error_code",
            "version",
        )
    }


def execution_identity(key, command):
    if command.kind not in {"ordinary", "batch", "manual"}:
        raise ValueError("invalid_cron_execution_kind")
    trigger = identity_value(command.trigger_key)
    family = "manual" if command.kind == "manual" else "scheduled"
    digest = request_hash([*key, family, trigger])
    return (
        str(uuid5(NAMESPACE_URL, "managed-cron-execution:" + digest)),
        digest,
    )


def _ready(row, command):
    if row.get("deleted_at"):
        raise ExecutionConflict("cron_job_deleted")
    if row.get("state_management_status") != "ready":
        raise ExecutionConflict("cron_job_not_ready")
    if not row["enabled"] or row.get("state_pause_reason"):
        raise ExecutionConflict("cron_job_disabled")
    expected = command.expected_definition_version
    if expected is not None and expected != row["definition_version"]:
        raise ExecutionConflict("cron_definition_version_conflict")


def _scheduled_time(command):
    value = command.scheduled_at
    if value is None:
        if command.kind != "manual":
            raise ValueError("scheduled_cron_requires_logical_time")
        return None
    if value.tzinfo is None:
        raise ValueError("cron_execution_time_requires_timezone")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _not_before_time(command):
    if command.not_before_at is None:
        return utc_now()
    if command.not_before_at.tzinfo is None:
        raise ValueError("cron_execution_time_requires_timezone")
    return command.not_before_at.astimezone(timezone.utc).replace(tzinfo=None)


async def _snapshot(tx, current, command, execution_id):
    spec = copy.deepcopy(json.loads(current["spec_json"]))
    meta = public_value(copy.deepcopy(command.dispatch_meta or {}))
    slot = spec.get("model_slot") or {}
    meta.update(
        {
            "protocol": "scheduler_managed_v1",
            "source": "managed_scheduler",
            "cron_execution_key": execution_id,
            "cron_is_manual": command.kind == "manual",
            "provider_id": str(slot.get("provider_id") or "").strip()
            or "default",
            "model_id": str(slot.get("model") or "").strip() or "default",
        },
    )
    when = _scheduled_time(command)
    if when is not None:
        meta["scheduled_fire_at"] = when.replace(
            tzinfo=timezone.utc,
        ).isoformat()
    if spec["task_type"] == "workflow":
        frozen = {**spec, "source_id": current["source_id"]}
        await resolve_workflow_batch_jobs([frozen], tx)
        meta["workflow_config_version"] = frozen["workflow_config_version"]
        meta["provider_id"] = frozen["provider_id"]
        meta["model_id"] = frozen["model_id"]
    return spec, meta, when


class ExecutionStore:
    def __init__(self, db, *, grant=None):
        self.db = db
        self.grant = grant

    async def enqueue(self, caller, job_id, command):
        async with transaction(self.db) as tx:
            return await self.enqueue_in_transaction(
                tx,
                caller,
                job_id,
                command,
            )

    async def enqueue_in_transaction(self, tx, caller, job_id, command):
        """在已有事务中固定本次执行，供批回调原子登记整组快照。"""
        key = caller.job_key(job_id)
        execution_id, digest = execution_identity(key, command)
        await tx.fetch_one(
            f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
            key,
        )
        if self.grant is None:
            current = await ManagedCronRepository(tx)._authorized_row(
                caller,
                job_id,
                True,
            )
        else:
            current = await self.grant.authorize(tx, caller, job_id)
        previous = await tx.fetch_one(
            f"SELECT * FROM {RUN_TABLE} WHERE execution_id=%s FOR UPDATE",
            (execution_id,),
        )
        if previous is not None:
            if (
                command.kind == "manual"
                and command.expected_definition_version
                not in (None, previous["definition_version"])
            ):
                raise ExecutionConflict("cron_request_content_conflict")
            return previous, True
        _ready(current, command)
        existing = await self._manual_pending(tx, key, command)
        if existing is not None:
            return existing, True
        spec, meta, when = await _snapshot(
            tx,
            current,
            command,
            execution_id,
        )
        row = self._new_row(caller, current, command, execution_id, digest)
        row.update(
            {
                "spec_json": canonical(spec),
                "dispatch_meta_json": canonical(meta),
                "scheduled_fire_at": when,
                "provider_id": meta["provider_id"],
                "model_id": meta["model_id"],
                "dispatch_order": int(meta.get("dispatch_order") or 0),
            },
        )
        await insert(tx, RUN_TABLE, row)
        await append_run_event(tx, row, "queued")
        return row, False

    @staticmethod
    async def _manual_pending(tx, key, command):
        if command.kind != "manual":
            return None
        active = await tx.fetch_all(
            f"SELECT * FROM {RUN_TABLE} WHERE {JOB_WHERE} "
            "AND status IN ('queued','dispatching','accepted','running') "
            "ORDER BY created_at, execution_id",
            key,
        )
        if any(row["status"] == "running" for row in active):
            raise ExecutionConflict("cron_job_running")
        return next((row for row in active if row["kind"] == "manual"), None)

    @staticmethod
    def _new_row(caller, current, command, execution_id, digest):
        now = utc_now()
        return {
            "execution_id": execution_id,
            "execution_key": digest,
            **{name: current[name] for name in IDENTITY_FIELDS},
            "scope_id": current["scope_id"],
            "actor_id": caller.actor_id,
            "definition_version": current["definition_version"],
            "enqueued_state_version": current["state_state_version"],
            "started_order": None,
            "kind": command.kind,
            "trigger_key": command.trigger_key,
            "not_before_at": _not_before_time(command),
            "status": "queued",
            "dispatch_attempt": 0,
            "dispatch_owner": "",
            "dispatch_lease_until": None,
            "run_owner": "",
            "run_lease_until": None,
            "accepted_at": None,
            "started_at": None,
            "finished_at": None,
            "history_execution_id": None,
            "output_preview": "",
            "trace_id": "",
            "error_code": None,
            "result_json": None,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }

    async def get(self, caller, job_id, execution_id):
        await ManagedCronRepository(self.db)._authorized_row(
            caller,
            job_id,
            True,
        )
        row = await self.db.fetch_one(
            f"SELECT * FROM {RUN_TABLE} WHERE execution_id=%s AND {JOB_WHERE}",
            (identity_value(execution_id), *caller.job_key(job_id)),
        )
        if row is None or tuple(
            row[name] for name in IDENTITY_FIELDS
        ) != caller.job_key(job_id):
            raise ManagedCronNotFound("cron_execution_not_found")
        return row
