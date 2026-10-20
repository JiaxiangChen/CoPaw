# -*- coding: utf-8 -*-
"""校验共库注册引用；平台未传 ID 或时间时按固定 Body 协议受理。"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from .distribution import TASKS
from .operations import JOURNAL, IDENTITY_FIELDS
from .definition import request_hash
from .dispatch_state_store import CONTROLS

PROTOCOL = "scheduler_managed_v1"


def parse_time(value, zone):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(
            value / 1000 if value > 100000000000 else value,
            timezone.utc,
        )
    if not isinstance(value, str):
        raise ValueError("cron_trigger_time_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(zone))
    return parsed.astimezone(timezone.utc)


async def binding_context(db, row, body, kind):
    field = (
        "ordinary_external_job_id"
        if kind == "ordinary"
        else "batch_external_job_id"
    )
    expected = str(row.get("state_" + field) or "")
    external_id = str(
        body.get("external_job_id") or body.get("jobId") or expected,
    )
    if (
        not expected
        or external_id != expected
        or body.get("callback_protocol") != PROTOCOL
    ):
        raise ValueError("cron_callback_binding_invalid")
    operation_id = body.get("operation_id")
    operation = await db.fetch_one(
        f"SELECT * FROM {JOURNAL} WHERE operation_id=%s",
        (operation_id,),
    )
    if operation and tuple(
        operation[name] for name in IDENTITY_FIELDS
    ) == tuple(row[name] for name in IDENTITY_FIELDS):
        return await _journal_binding(db, operation, kind, external_id, row)
    task = await db.fetch_one(
        f"SELECT * FROM {TASKS} WHERE task_id=%s",
        (operation_id,),
    )
    if (
        task
        and kind == "batch"
        and tuple(task[name] for name in IDENTITY_FIELDS)
        == tuple(row[name] for name in IDENTITY_FIELDS)
    ):
        plan = _broadcast_timer_plan(task)
        if plan:
            return {
                "spec": plan["spec"],
                "advance": plan["advance_minutes"],
                "definition_version": task["source_definition_version"],
                "operation_id": operation_id,
                "external_id": external_id,
                "skip_reason": _observed_management_gate(row),
            }
    raise ValueError("cron_callback_binding_reference_invalid")


def _broadcast_timer_plan(task):
    """已保存计划和已知平台绑定足以核验触发，不将响应丢失当作未生效。"""
    plan = json.loads(task["payload_json"]).get("timer_sync")
    if not plan:
        return None
    if task["sync_status"] in {"completed", "failed"}:
        return plan
    key = tuple(task[name] for name in IDENTITY_FIELDS)
    if (
        task["status"] == "running"
        and task["sync_status"] == "pending"
        and task.get("active_key") == request_hash(key)
    ):
        return plan
    return None


def _observed_management_gate(row):
    return (
        "job_not_ready"
        if row.get("state_management_status") != "ready"
        else None
    )


def _unfinished_crud_binding(operation, row):
    expected = {
        "create": {"creating", "create_failed"},
        "update": {"updating", "update_failed"},
        "delete": {"deleting", "delete_failed"},
    }.get(operation["action"], ())
    return (
        row.get("state_management_status") in expected
        and operation["status"] in {"pending", "running", "failed"}
        and operation.get("active_key")
        == request_hash(tuple(row[name] for name in IDENTITY_FIELDS))
        and operation["expected_version"] == row["definition_version"]
    )


async def _current_mode_control(db, operation, allowed):
    key = tuple(operation[name] for name in IDENTITY_FIELDS)
    if operation.get("active_key") != request_hash(key):
        return False
    control = await db.fetch_one(
        f"SELECT * FROM {CONTROLS} WHERE "
        + " AND ".join(f"{name}=%s" for name in IDENTITY_FIELDS),
        key,
    )
    return bool(
        control
        and control["operation_id"] == operation["operation_id"]
        and control["state"] in allowed
        and control["sync_status"] in {"pending", "failed"},
    )


async def _unfinished_mode_binding(db, operation, row, kind):
    if operation["status"] not in {"pending", "running", "failed"}:
        return False
    if operation["expected_version"] != row["definition_version"]:
        return False
    if operation["action"] == "dispatch_state" and kind == "batch":
        return await _current_mode_control(
            db,
            operation,
            {"batch_enabled", "batch_paused"},
        )
    if operation["action"] != "mode_sync" or kind != "ordinary":
        return False
    payload = json.loads(operation["payload_json"])
    parent = await db.fetch_one(
        f"SELECT * FROM {JOURNAL} WHERE operation_id=%s",
        (payload.get("parent_operation_id"),),
    )
    if parent is None or parent["action"] != "dispatch_state":
        return False
    parent_key = tuple(parent[name] for name in IDENTITY_FIELDS)
    row_key = tuple(row[name] for name in IDENTITY_FIELDS)
    related = (
        row_key == parent_key
        or tuple(row.get(f"parent_{name}") for name in IDENTITY_FIELDS)
        == parent_key
    )
    if not related or parent["status"] not in {"pending", "running", "failed"}:
        return False
    return await _current_mode_control(db, parent, {"ordinary"})


def _reference_kind(operation, payload, kind):
    action = operation["action"]
    if action == "delete":
        return True
    if action == "dispatch_state":
        return kind == "batch"
    if action == "mode_sync":
        return kind == "ordinary"
    if action not in {"create", "update"}:
        return False
    expected = (
        "ordinary" if payload.get("dispatch_state") == "ordinary" else "batch"
    )
    return kind == expected


async def _journal_binding(db, operation, kind, external_id, row):
    payload = json.loads(operation["payload_json"])
    progress = json.loads(operation["progress_json"])
    if not _reference_kind(operation, payload, kind):
        raise ValueError("cron_callback_binding_reference_invalid")
    spec = (
        progress.get("batch_spec")
        if operation["action"] == "dispatch_state"
        else (progress.get("prepared") or {}).get("spec")
        or payload.get("spec")
    )
    trusted = operation["status"] == "completed" or _unfinished_crud_binding(
        operation,
        row,
    )
    if not trusted:
        trusted = await _unfinished_mode_binding(db, operation, row, kind)
    if not spec or not trusted:
        raise ValueError("cron_callback_binding_operation_incomplete")
    version = operation["expected_version"] + (operation["action"] == "update")
    advance = (
        int(progress.get("batch_advance_minutes", 0))
        if kind == "batch"
        else int(spec.get("meta", {}).get("broadcast_offset_minutes", 0) or 0)
    )
    return {
        "spec": spec,
        "advance": advance,
        "definition_version": version,
        "operation_id": operation["operation_id"],
        "external_id": external_id,
        # 首次确认合法绑定时已观察到管理中，之后即使发布完成也不补执行这次触发。
        "skip_reason": _observed_management_gate(row),
    }


def trigger_times(body, binding, now):
    zone = binding["spec"]["schedule"]["timezone"]
    advance = timedelta(minutes=binding["advance"])
    logical = body.get("scheduled_fire_at")
    physical = next(
        (
            body.get(key)
            for key in ("fire_time", "trigger_time", "fireTime", "triggerTime")
            if body.get(key) is not None
        ),
        None,
    )
    if logical is not None:
        logical_time = parse_time(logical, zone).replace(
            second=0,
            microsecond=0,
        )
        physical_time = logical_time - advance
    elif physical is not None:
        physical_time = parse_time(physical, zone).replace(
            second=0,
            microsecond=0,
        )
        logical_time = physical_time + advance
    else:
        # 平台只传注册时的固定 Body，不能推测它的日志或计划期。
        # 接收时间是本次触发事实；批提前量仍用于计算成员等待时间。
        return now + advance, now
    if not croniter.match(
        binding["spec"]["schedule"]["cron"],
        physical_time.astimezone(ZoneInfo(zone)),
    ):
        raise ValueError("cron_trigger_schedule_mismatch")
    return logical_time, physical_time


def trigger_id(body):
    value = next(
        (
            body.get(key)
            for key in (
                "trigger_id",
                "logId",
                "log_id",
                "external_execution_id",
                "execution_id",
            )
            if body.get(key) is not None
        ),
        "",
    )
    value = str(value)
    if len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError("cron_trigger_identity_invalid")
    return value
