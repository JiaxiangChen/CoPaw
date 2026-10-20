# -*- coding: utf-8 -*-
"""只处理平台实际送达的新版普通/批触发，原子保存执行快照及触发回执。"""

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from .callback_contract import (
    PROTOCOL,
    binding_context,
    trigger_id,
    trigger_times,
)
from .contracts import ManagedCronNotFound
from .definition import canonical
from .dispatch_state_store import CONTROLS
from .distribution import _children
from .executions import ExecutionConflict, ExecutionRequest, ExecutionStore
from .identity import CallerIdentity, identity_value
from .operations import IDENTITY_FIELDS, WHERE_JOB
from .batch_order import ordered_members
from .repository import ManagedCronRepository
from .storage import insert, transaction
from .batch_members import record_batch_members

RECEIPTS = "swe_cron_managed_trigger_receipts"
logger = logging.getLogger(__name__)


def _allows_empty_batch(targets, kind):
    """仍有批模式对象但暂时无人可执行时，保留既有零成员看板回执。"""
    return kind == "batch" and any(
        row["state_dispatch_state"] == "batch_enabled" for row in targets
    )


class TriggerGrant:
    """已验证平台注册绑定产生的内部受理权限，不伪造用户或管理角色。"""

    def __init__(self, source_key, kind):
        self.source_key = source_key
        self.kind = kind

    async def authorize(self, db, caller, job_id):
        key = caller.job_key(job_id)
        row = await ManagedCronRepository(db)._get_row(
            key,
            include_deleted=True,
        )
        if (
            key != self.source_key
            and tuple(row.get(f"parent_{name}") for name in IDENTITY_FIELDS)
            != self.source_key
        ):
            raise ManagedCronNotFound("cron_trigger_job_not_related")
        expected = "ordinary" if self.kind == "ordinary" else "batch_enabled"
        if row["state_dispatch_state"] != expected:
            raise ExecutionConflict("cron_entry_mode_changed")
        return row


class ManagedSchedulingService:
    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def accept(self, caller, body, kind):
        if kind not in {"ordinary", "batch"}:
            raise ValueError("invalid_cron_callback_kind")
        job_id = identity_value(
            str(body.get("job_id") or body.get("parent_job_id") or ""),
        )
        key = caller.job_key(job_id)
        root = await ManagedCronRepository(self.db)._get_row(
            key,
            include_deleted=True,
        )
        binding = await binding_context(self.db, root, body, kind)
        # 平台没有每次触发的标识，且约定每次请求都是新触发。
        receipt_id = str(uuid4())
        identifier = trigger_id(body) or receipt_id
        now = self.clock().astimezone(timezone.utc)
        logical, physical = trigger_times(body, binding, now)
        async with transaction(self.db) as tx:
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            root = await ManagedCronRepository(tx)._get_row(
                key,
                include_deleted=True,
            )
            response, members = await self._accept_locked(
                tx,
                caller,
                root,
                binding,
                kind,
                logical,
                physical,
                receipt_id,
                now,
            )
            await insert(
                tx,
                RECEIPTS,
                {
                    "receipt_id": receipt_id,
                    "callback_key": None,
                    **dict(zip(IDENTITY_FIELDS, key)),
                    "external_job_id": binding["external_id"],
                    "operation_id": binding["operation_id"],
                    "kind": kind,
                    "trigger_id": identifier,
                    "logical_fire_at": logical.replace(tzinfo=None),
                    "physical_fire_at": physical.replace(tzinfo=None),
                    "batch_id": response.get("batch_id"),
                    "status": (
                        "accepted" if response["accepted"] else "skipped"
                    ),
                    "response_json": canonical(response),
                    "members_json": canonical(members),
                    "created_at": now.replace(tzinfo=None),
                },
            )
            await record_batch_members(tx, response.get("batch_id"), now=now)
        logger.info(
            "平台触发已持久处理 receipt_id=%s job_id=%s kind=%s accepted=%s",
            receipt_id,
            job_id,
            kind,
            response["accepted"],
        )
        return response

    async def _accept_locked(
        self,
        tx,
        caller,
        root,
        binding,
        kind,
        logical,
        physical,
        receipt_id,
        now,
    ):
        reason = self._gate(root, binding, physical, now)
        if kind == "batch":
            control = await tx.fetch_one(
                f"SELECT state FROM {CONTROLS} WHERE {WHERE_JOB}",
                caller.job_key(root["job_id"]),
            )
            if control and control["state"] == "batch_paused":
                reason = "batch_paused"
            if (
                control is None
                and root["state_dispatch_state"] == "batch_paused"
            ):
                reason = "batch_paused"
        if reason:
            return {
                "job_id": root["job_id"],
                "accepted": False,
                "reused": False,
                "status": "skipped",
                "reason": reason,
            }, []
        targets = [root]
        if kind == "batch":
            children = await _children(tx, caller.job_key(root["job_id"]), [])
            targets += [
                await ManagedCronRepository(tx)._get_row(
                    tuple(row[name] for name in IDENTITY_FIELDS),
                    include_deleted=True,
                )
                for row in children
            ]
        selected = [row for row in targets if self._eligible(row, kind)]
        if not selected and not _allows_empty_batch(targets, kind):
            return {
                "job_id": root["job_id"],
                "accepted": False,
                "reused": False,
                "status": "skipped",
                "reason": "job_not_eligible",
            }, []
        batch_id = (
            str(uuid5(NAMESPACE_URL, "managed-batch:" + receipt_id))
            if kind == "batch"
            else None
        )
        members = await self._enqueue_members(
            tx,
            caller,
            root,
            selected,
            logical,
            receipt_id,
            batch_id,
            kind,
        )
        if kind == "ordinary":
            return {
                "job_id": root["job_id"],
                "execution_id": members[0]["execution_id"],
                "definition_version": members[0]["definition_version"],
                "accepted": True,
                "reused": members[0]["reused"],
                "status": members[0]["status"],
            }, members
        return {
            "batch_id": batch_id,
            "parent_job_id": root["job_id"],
            "accepted": True,
            "reused": False,
            "enqueued_intents": len(members),
            "status": "queued",
        }, members

    @staticmethod
    def _gate(root, binding, physical, now):
        if root["deleted_at"]:
            return "job_deleted"
        if binding.get("skip_reason"):
            return binding["skip_reason"]
        if root["definition_version"] != binding["definition_version"]:
            return "binding_definition_changed"
        if root["state_management_status"] != "ready":
            return "job_not_ready"
        grace = json.loads(root["spec_json"])["runtime"][
            "misfire_grace_seconds"
        ]
        if physical > now + timedelta(seconds=5) or now - physical > timedelta(
            seconds=grace,
        ):
            return "trigger_outside_window"
        return None

    @staticmethod
    def _eligible(row, kind):
        wanted = "ordinary" if kind == "ordinary" else "batch_enabled"
        return (
            not row["deleted_at"]
            and row["enabled"]
            and not row["state_pause_reason"]
            and row["state_management_status"] == "ready"
            and row["state_dispatch_state"] == wanted
        )

    async def _enqueue_members(
        self,
        tx,
        caller,
        root,
        selected,
        logical,
        receipt_id,
        batch_id,
        kind,
    ):
        key = tuple(root[name] for name in IDENTITY_FIELDS)
        ranked = await ordered_members(
            tx,
            root,
            selected,
            logical,
            self.clock(),
            kind,
        )
        store = ExecutionStore(tx, grant=TriggerGrant(key, kind))
        result = []
        for entry in ranked:
            row = entry["definition"]
            rank = entry["payload"]["dispatch_priority"]
            actor = CallerIdentity(
                row["tenant_id"],
                row["source_id"],
                row["agent_id"],
                caller.actor_id,
            )
            due = entry["due_at"]
            metadata = {
                "protocol": PROTOCOL,
                "trigger_receipt_id": receipt_id,
                "batch_id": batch_id,
                "parent_scheduled_fire_at": logical.isoformat(),
                "dispatch_priority": rank,
                "dispatch_order": entry["dispatch_order"],
                "viewer_heat_score": entry["viewer_heat_score"],
            }
            command = ExecutionRequest(
                kind,
                receipt_id,
                logical,
                dispatch_meta=metadata,
                not_before_at=due,
            )
            run, reused = await store.enqueue_in_transaction(
                tx,
                actor,
                row["job_id"],
                command,
            )
            result.append(
                {
                    "execution_id": run["execution_id"],
                    "definition_version": run["definition_version"],
                    "status": run["status"],
                    "reused": reused,
                    "identity": list(actor.job_key(row["job_id"])),
                    "priority": rank,
                },
            )
        return result
