# -*- coding: utf-8 -*-
"""沿用已有执行历史字段，不改原表结构，不通过 HTTP 回传最终结果。"""

import json
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from ..runner.model_call_error_detail import redact_sensitive_fragments
from .managed_failure import public_error_message
from ..source_system_config.runtime import (
    get_current_source_system_config,
    resolve_cron_notification_config,
)


def safe_metadata(value):
    if isinstance(value, dict):
        return {
            key: safe_metadata(item)
            for key, item in value.items()
            if not _credential_key(str(key))
        }
    if isinstance(value, list):
        return [safe_metadata(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_fragments(value)
    return value


def _credential_key(key):
    key = key.lower().replace("_", "-")
    return key in {
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
    } or key.endswith(
        (
            "-token",
            "-password",
            "-secret",
            "-api-key",
            "-authorization",
        ),
    )


def bounded_json(value, limit):
    text = json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    if len(text) <= limit:
        return text
    return json.dumps(
        {"_truncated": True, "preview": text[: limit // 6]},
        ensure_ascii=False,
    )


def _utc(value):
    from datetime import datetime

    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _notification_time(run, spec, now):
    meta = spec.get("meta", {})
    try:
        delay = min(
            max(int(meta.get("notification_delay_minutes", 0)), 0),
            7 * 24 * 60,
        )
    except (ValueError, TypeError):
        delay = 0
    dispatch = json.loads(run["dispatch_meta_json"])
    original = dispatch.get("parent_scheduled_fire_at")
    if original:
        return _utc(original) + timedelta(minutes=delay)
    if meta.get("broadcast_notification_policy") == "original_schedule":
        offset = max(int(meta.get("broadcast_offset_minutes", 0) or 0), 0)
        return _utc(run.get("started_at") or now) + timedelta(
            minutes=delay + offset,
        )
    return _utc(now) + timedelta(minutes=delay)


def notification_fields(run, spec, outcome, now, *, deleted=False):
    zone = spec["schedule"].get("timezone", "UTC")
    needed = (
        outcome.status == "success"
        and spec["task_type"] in {"agent", "workflow"}
        and not deleted
    )
    due = _notification_time(run, spec, now) if needed else None
    config = resolve_cron_notification_config(
        get_current_source_system_config(),
    )
    if (
        needed
        and run["kind"] != "manual"
        and config.skip_weekend_zhaohu_enabled
    ):
        needed = due.astimezone(ZoneInfo(zone)).weekday() < 5
    return {
        "notification_status": "pending" if needed else "not_required",
        "notification_due_at": (
            due.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
            if needed
            else None
        ),
        "notification_timezone": zone if needed else "",
    }


def history_values(run, outcome, now, *, deleted=False):
    spec = json.loads(run["spec_json"])
    started = _utc(run.get("started_at") or run.get("accepted_at") or now)
    end = _utc(now)
    beijing = ZoneInfo("Asia/Shanghai")
    return {
        "job_id": run["job_id"],
        "job_name": spec["name"],
        "tenant_id": run["tenant_id"],
        "scheduled_time": (
            _utc(run["scheduled_fire_at"])
            .astimezone(beijing)
            .replace(tzinfo=None)
            if run.get("scheduled_fire_at")
            else None
        ),
        "actual_time": started.astimezone(beijing).replace(tzinfo=None),
        "end_time": end.astimezone(beijing).replace(tzinfo=None),
        "duration_ms": max(int((end - started).total_seconds() * 1000), 0),
        "status": outcome.status,
        "error_message": public_error_message(
            outcome.error_code,
            outcome.execution_meta,
        )
        or "",
        "instance_id": str(run.get("run_owner") or "")[:64],
        "executor_leader": outcome.executor_leader[:64],
        "is_manual": int(run["kind"] == "manual"),
        "is_final": 1,
        "trace_id": outcome.trace_id[:64],
        "session_id": spec.get("meta", {}).get("task_session_id", "")[:64],
        "input_snapshot": bounded_json(outcome.input_snapshot, 16000),
        "output_preview": outcome.preview[:512],
        "meta": history_metadata(run, outcome),
        **notification_fields(run, spec, outcome, now, deleted=deleted),
        "is_read": 0,
        "read_at": None,
        "created_at": end.astimezone(beijing).replace(tzinfo=None),
    }


def history_metadata(run, outcome):
    execution = json.loads(
        bounded_json(safe_metadata(outcome.execution_meta or {}), 1024),
    )
    return bounded_json(
        {
            "management_owner": "scheduler",
            "managed_execution_id": run["execution_id"],
            "source_id": run["source_id"],
            "agent_id": run["agent_id"],
            "definition_version": run["definition_version"],
            "execution": execution,
        },
        2048,
    )
