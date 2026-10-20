# -*- coding: utf-8 -*-
"""从已受理分发记录建立私有源授权，不通过伪造角色创建跨用户任务。"""

import copy
import json
from dataclasses import dataclass

from .contracts import ManagedCronNotFound
from .distribution import BroadcastStore, ITEMS
from .identity import encode_scope
from .operations import JOURNAL, WHERE_JOB, ManagementConflict
from .repository import IDENTITY_FIELDS, ManagedCronRepository


@dataclass
class ChildGrant:
    task_id: str
    index: int
    owner: str
    task: dict | None = None
    item: dict | None = None

    async def validate(self, tx, key, actor):
        task = await BroadcastStore._fence(tx, self.task_id, self.owner)
        item = await tx.fetch_one(
            f"SELECT * FROM {ITEMS} WHERE task_id=%s AND index_no=%s FOR UPDATE",
            (self.task_id, self.index),
        )
        if (
            item is None
            or tuple(item[name] for name in IDENTITY_FIELDS) != key
            or actor != task["actor_id"]
        ):
            raise ManagedCronNotFound("cron_broadcast_target_not_authorized")
        if item["status"] in {"succeeded", "failed"}:
            raise ManagementConflict("cron_broadcast_target_finished")
        parent = await ManagedCronRepository(tx)._get_row(
            tuple(task[name] for name in IDENTITY_FIELDS),
            include_deleted=True,
        )
        if (
            parent["deleted_at"]
            or parent["definition_version"]
            != task["source_definition_version"]
        ):
            raise ManagementConflict("cron_broadcast_source_changed")
        if not task["actor_is_manager"] and parent["creator_user_id"] != actor:
            raise ManagedCronNotFound("cron_broadcast_source_not_authorized")
        self.task, self.item = task, item

    async def authorize_row(self, tx, caller, job_id):
        await self.validate(tx, caller.job_key(job_id), caller.actor_id)
        row = await ManagedCronRepository(tx)._get_row(
            caller.job_key(job_id),
            include_deleted=True,
        )
        if tuple(
            row.get(f"parent_{name}") for name in IDENTITY_FIELDS
        ) != tuple(self.task[name] for name in IDENTITY_FIELDS):
            raise ManagedCronNotFound("cron_child_not_from_source")
        return row

    async def rewrite_spec(self, tx, spec, current):
        plan = json.loads(self.item["plan_json"])
        if plan["retime_only"]:
            if (
                current is None
                or current["state_management_status"] != "ready"
            ):
                raise ManagementConflict("cron_retime_requires_ready_child")
            result = copy.deepcopy(json.loads(current["spec_json"]))
            result["schedule"] = copy.deepcopy(spec["schedule"])
            result["enabled"] = bool(current["enabled"])
        else:
            result = copy.deepcopy(spec)
        owner = self.item["tenant_id"]
        result["meta"]["creator_user_id"] = owner
        result["meta"]["job_origin"] = "broadcast"
        result["dispatch"]["target"]["user_id"] = owner
        if result.get("request") is not None:
            result["request"]["user_id"] = owner
        profile = {} if plan["retime_only"] else plan["profile"]
        for key, source in (
            ("tenant_name", "tenant_name"),
            ("bbk_id", "bbk_id"),
        ):
            if profile.get(source) is not None:
                result[key] = profile[source]
        source = json.loads(self.task["payload_json"])["source_spec"]
        result["meta"].update(
            {
                "broadcast_source_job_id": self.task["job_id"],
                "broadcast_source_tenant_id": self.task["tenant_id"],
                "broadcast_source_source_id": self.task["source_id"],
                "broadcast_source_agent_id": self.task["agent_id"],
                "broadcast_source_job_name": source["name"],
                "broadcast_original_cron": plan["original_cron_expr"],
                "broadcast_original_timezone": plan["original_timezone"],
                "broadcast_offset_minutes": self.item["offset_minutes"],
                "broadcast_notification_policy": "original_schedule",
            },
        )
        if current is None:
            result["enabled"] = True
        elif not await self._published(tx):
            result["enabled"] = bool(
                json.loads(current["spec_json"])["enabled"],
            )
        return result

    async def _published(self, tx):
        row = await tx.fetch_one(
            f"SELECT operation_id FROM {JOURNAL} WHERE {WHERE_JOB} AND action IN ('create','update') AND status='completed' LIMIT 1",
            tuple(self.item[name] for name in IDENTITY_FIELDS),
        )
        return row is not None

    async def operation_payload(self, tx):
        policy = (
            "retime_existing"
            if json.loads(self.item["plan_json"])["retime_only"]
            else "broadcast"
        )
        return {
            "broadcast_context": {
                "task_id": self.task_id,
                "index_no": self.index,
            },
            "preparation_policy": policy,
            "projection_expect_new": not await self._published(tx),
        }

    def definition_values(self):
        payload = json.loads(self.task["payload_json"])
        plan = json.loads(self.item["plan_json"])
        return {
            **{f"parent_{name}": self.task[name] for name in IDENTITY_FIELDS},
            "job_origin": "broadcast",
            "source_definition_version": self.task[
                "source_definition_version"
            ],
            "enable_offset": payload["enable_offset"],
            "offset_window_hours": payload["offset_window_hours"],
            "offset_minutes": self.item["offset_minutes"],
            "original_cron_expr": plan["original_cron_expr"],
        }


def child_payload(task, item, current_spec=None):
    source = json.loads(task["payload_json"])["source_spec"]
    spec = copy.deepcopy(current_spec if current_spec is not None else source)
    spec.update(
        id=item["job_id"],
        tenant_id=item["tenant_id"],
        source_id=item["source_id"],
        scope_id=encode_scope(item["tenant_id"], item["source_id"]),
    )
    spec["meta"] = {
        key: value
        for key, value in source.get("meta", {}).items()
        if not key.startswith(
            ("task_", "broadcast_", "batch_dispatch_", "_scheduler"),
        )
    }
    spec["schedule"] = {
        "type": "cron",
        "cron": item["cron_expr"],
        "timezone": item["timezone"],
    }
    payload = {
        "request_id": f"{task['task_id']}:{item['index_no']}",
        "spec": spec,
        "dispatch_state": json.loads(task["payload_json"])["dispatch_state"],
    }
    if item["expected_definition_version"] is not None:
        payload["expected_definition_version"] = item[
            "expected_definition_version"
        ]
    return payload
