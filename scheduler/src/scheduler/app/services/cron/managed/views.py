# -*- coding: utf-8 -*-
"""只读展示字段映射，保持原任务界面的字段语义。"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from .contracts import ManagedCronUnavailable

STATE_COLUMNS = (
    "task_chat_id",
    "task_session_id",
    "ordinary_external_job_id",
    "batch_external_job_id",
    "dispatch_state",
    "pause_reason",
    "auto_paused_at",
    "next_run_at",
    "next_run_times_json",
    "last_status",
    "last_run_at",
    "last_prefetch_at",
    "last_error",
    "has_scheduled_result",
    "latest_scheduled_preview",
    "last_scheduled_run_at",
    "unread_execution_count",
    "management_status",
    "management_version",
    "state_version",
    "queue_invalidation_version",
)
_SECRET_NAMES = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api-key",
    "access-key",
    "credential",
    "credentials",
    "x-header-cookie",
}


def _secret_name(name: str) -> bool:
    key = name.lower().replace("_", "-")
    return key in _SECRET_NAMES or key.endswith(
        ("-token", "-password", "-secret", "-api-key", "-authorization"),
    )


def public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: public_value(item)
            for key, item in value.items()
            if not _secret_name(str(key))
            and not str(key).startswith("_scheduler")
        }
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def _json(value: Any, kind: type, default: Any) -> Any:
    if value is None:
        return copy.deepcopy(default)
    try:
        parsed = (
            json.loads(value) if isinstance(value, (str, bytes)) else value
        )
    except (ValueError, TypeError) as exc:
        raise ManagedCronUnavailable("cron_data_unavailable") from exc
    if not isinstance(parsed, kind):
        raise ManagedCronUnavailable("cron_data_unavailable")
    return copy.deepcopy(parsed)


def task_view(row: dict, actor_id: str) -> dict:
    paused = row.get("state_pause_reason")
    if not paused and not row["enabled"]:
        paused = "manual"
    return {
        "visible_in_my_tasks": row.get("creator_user_id") == actor_id,
        "chat_id": row.get("state_task_chat_id"),
        "session_id": row.get("state_task_session_id"),
        "is_running": row.get("state_last_status") == "running",
        "is_paused": bool(paused),
        "pause_reason": paused,
        "auto_paused_at": row.get("state_auto_paused_at"),
        "has_scheduled_result": bool(row.get("state_has_scheduled_result")),
        "latest_scheduled_preview": row.get("state_latest_scheduled_preview")
        or "",
        "last_scheduled_run_at": row.get("state_last_scheduled_run_at"),
        "unread_execution_count": int(
            row.get("state_unread_execution_count") or 0,
        ),
    }


def public_spec(value):
    spec = copy.deepcopy(value)
    for key in ("meta", "model_slot"):
        if key in spec:
            spec[key] = public_value(spec[key])
    dispatch = spec.get("dispatch")
    if isinstance(dispatch, dict) and "meta" in dispatch:
        dispatch["meta"] = public_value(dispatch["meta"])
    return {
        key: item
        for key, item in spec.items()
        if not key.startswith("_scheduler")
    }


def _next_run_times(row, *, now=None):
    """按当前定义实时计算展示时间，不落库、不产生或补发执行意图。"""
    try:
        zone = ZoneInfo(row["timezone"])
        base = now or datetime.now(zone)
        base = (
            base.replace(tzinfo=zone)
            if base.tzinfo is None
            else base.astimezone(zone)
        )
        schedule = croniter(row["cron_expr"], base)
        return [schedule.get_next(datetime).isoformat() for _ in range(3)]
    except (ValueError, KeyError, OverflowError) as exc:
        raise ManagedCronUnavailable("cron_data_unavailable") from exc


def job_view(row: dict, actor_id: str) -> dict:
    if row.get("spec_json") is None:
        raise ManagedCronUnavailable("cron_data_unavailable")
    spec = public_spec(_json(row.get("spec_json"), dict, {}))
    spec.update(
        {
            "id": row["job_id"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "task_type": row["task_type"],
            "tenant_id": row["tenant_id"],
            "source_id": row["source_id"],
            "scope_id": row["scope_id"],
            "schedule": {
                "type": "cron",
                "cron": row["cron_expr"],
                "timezone": row["timezone"],
            },
        },
    )
    state = {
        key: row.get(f"state_{key}")
        for key in (
            "last_run_at",
            "last_prefetch_at",
            "last_status",
            "last_error",
        )
    }
    next_times = (
        _next_run_times(row) if row.get("state_job_id") is not None else []
    )
    state["next_run_times"] = next_times
    state["next_run_at"] = next_times[0] if next_times else None
    return {
        "spec": spec,
        "state": state,
        "task": task_view(row, actor_id),
        "management_owner": "scheduler",
        "agent_id": row["agent_id"],
        "definition_version": row["definition_version"],
        "deleted": row.get("deleted_at") is not None,
        "dispatch_state": row.get("state_dispatch_state"),
        "management_status": (
            row.get("state_management_status") or "unknown"
            if row.get("state_job_id") is not None
            else "state_missing"
        ),
        "management_version": row.get("state_management_version"),
        "state_version": row.get("state_state_version"),
        "offset_minutes": row.get("offset_minutes"),
        "offset_window_hours": row.get("offset_window_hours"),
        "enable_offset": bool(row.get("enable_offset")),
    }


def list_item(row: dict, actor_id: str) -> dict:
    view = job_view(row, actor_id)
    return {**view.pop("spec"), **view}


def child_item(row: dict, actor_id: str) -> dict:
    item = list_item(row, actor_id)
    # 源管理者可查看关联元数据，但轻量列表不返回其他用户的完整任务正文。
    return {
        key: item[key]
        for key in (
            "id",
            "name",
            "tenant_id",
            "source_id",
            "scope_id",
            "agent_id",
            "enabled",
            "task_type",
            "schedule",
            "definition_version",
            "deleted",
            "management_status",
            "management_version",
            "management_owner",
            "dispatch_state",
            "state",
            "task",
            "offset_minutes",
            "offset_window_hours",
            "enable_offset",
        )
    } | {
        "job_id": item["id"],
        "tenant_name": item.get("tenant_name"),
        "bbk_id": item.get("bbk_id"),
    }
