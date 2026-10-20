# -*- coding: utf-8 -*-
"""校验 Scheduler 任务定义，不依赖 SWE 运行时包。"""

import copy
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from .identity import encode_scope, identity_value
from .views import public_value

ALLOWED_SPEC_KEYS = {
    "id",
    "name",
    "enabled",
    "plan_id",
    "tenant_id",
    "bbk_id",
    "source_id",
    "tenant_name",
    "scope_id",
    "schedule",
    "task_type",
    "text",
    "request",
    "model_slot",
    "skill_ids",
    "workflow_binding_id",
    "dispatch",
    "runtime",
    "meta",
}

# 关联由服务端分发写入，公开编辑只能保留既有值，不能从请求伪造。
BROADCAST_RELATION_META_FIELDS = (
    "broadcast_source_job_id",
    "broadcast_source_tenant_id",
    "broadcast_source_source_id",
    "broadcast_source_agent_id",
    "broadcast_source_job_name",
    "broadcast_original_cron",
    "broadcast_original_timezone",
    "broadcast_offset_minutes",
    "broadcast_notification_policy",
)


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def profile_columns(spec):
    """从同一份定义提取查询副本，完整定义仍保留在 JSON 中。"""
    skills = spec.get("skill_ids") or ""
    if isinstance(skills, list):
        skills = ",".join(str(item) for item in skills)
    return {
        "tenant_name": spec.get("tenant_name") or "",
        "bbk_id": spec.get("bbk_id") or "",
        "skill_ids": skills,
        "workflow_binding_id": spec.get("workflow_binding_id") or None,
    }


def request_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stable_id(caller, request_id, action):
    identity_value(request_id)
    parts = [
        caller.tenant_id,
        caller.source_id,
        caller.agent_id,
        caller.actor_id,
        request_id,
        action,
    ]
    return str(uuid5(NAMESPACE_URL, "scheduler-cron:" + canonical(parts)))


def _schedule(raw):
    if not isinstance(raw, dict) or raw.get("type", "cron") != "cron":
        raise ValueError("invalid_cron_schedule")
    expression = raw.get("cron", "")
    if not isinstance(expression, str) or len(expression.split()) != 5:
        raise ValueError("invalid_cron_schedule")
    if not croniter.is_valid(expression):
        raise ValueError("invalid_cron_schedule")
    zone = raw.get("timezone", "UTC")
    if not isinstance(zone, str):
        raise ValueError("invalid_cron_timezone")
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid_cron_timezone") from exc
    return {"type": "cron", "cron": expression, "timezone": zone}


def _runtime(raw):
    if not isinstance(raw, dict):
        raise ValueError("invalid_cron_runtime")
    values = {
        "max_concurrency": 1,
        "timeout_seconds": 7200,
        "misfire_grace_seconds": 300,
    }
    for key, default in values.items():
        value = raw.get(key, default)
        minimum = 0 if key == "misfire_grace_seconds" else 1
        if type(value) is not int or value < minimum:
            raise ValueError("invalid_cron_runtime")
        values[key] = value
    return values


def _content(spec):
    kind = spec.get("task_type", "agent")
    if kind not in {"agent", "text", "workflow"}:
        raise ValueError("invalid_cron_task_type")
    if kind == "agent" and not isinstance(spec.get("request"), dict):
        raise ValueError("cron_agent_request_required")
    if kind == "text" and not (
        isinstance(spec.get("text"), str) and spec["text"].strip()
    ):
        raise ValueError("cron_text_required")
    if kind == "workflow" and not (
        spec.get("skill_ids") or spec.get("workflow_binding_id")
    ):
        raise ValueError("cron_workflow_binding_required")
    return kind


def normalize_spec(raw, caller, job_id, current=None):
    if not isinstance(raw, dict) or set(raw) - ALLOWED_SPEC_KEYS:
        raise ValueError("invalid_cron_spec")
    if len(canonical(raw).encode()) > 524288:
        raise ValueError("cron_spec_too_large")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise ValueError("invalid_cron_name")
    expected = {
        "id": job_id,
        "tenant_id": caller.tenant_id,
        "source_id": caller.source_id,
    }
    if any(
        raw.get(key) not in (None, "", value)
        for key, value in expected.items()
    ):
        raise ValueError("conflicting_cron_identity")
    spec = copy.deepcopy(raw)
    _model_slot(spec.get("model_slot"))
    spec.update(expected)
    spec["scope_id"] = encode_scope(caller.tenant_id, caller.source_id)
    spec["schedule"] = _schedule(raw.get("schedule"))
    spec["runtime"] = _runtime(raw.get("runtime", {}))
    spec["task_type"] = _content(spec)
    spec["enabled"] = _enabled(raw, current)
    _profile(spec, caller, current)
    spec["meta"] = _metadata(spec.get("meta", {}), caller, current)
    owner = spec["meta"]["creator_user_id"]
    session = f"cron-task:{job_id}"
    dispatch = spec.setdefault(
        "dispatch",
        {"type": "channel", "channel": "console", "mode": "final"},
    )
    if (
        not isinstance(dispatch, dict)
        or dispatch.get("type", "channel") != "channel"
    ):
        raise ValueError("invalid_cron_dispatch")
    dispatch["target"] = {"user_id": owner, "session_id": session}
    if "meta" in dispatch:
        dispatch["meta"] = public_value(dispatch["meta"])
    if spec.get("request") is not None:
        spec["request"].update(user_id=owner, session_id=session)
    return spec


def _profile(spec, caller, current):
    previous = json.loads(current["spec_json"]) if current else {}
    preserve = (
        current is not None and current["creator_user_id"] != caller.actor_id
    )
    for field, incoming in (
        ("bbk_id", caller.bbk_id),
        ("tenant_name", caller.user_name),
    ):
        spec[field] = (
            previous.get(field) if preserve or incoming is None else incoming
        )


def _model_slot(slot):
    if slot is None:
        return
    if not isinstance(slot, dict) or set(slot) - {"provider_id", "model"}:
        raise ValueError("invalid_cron_model_slot")


def _enabled(raw, current):
    if current is not None:
        return bool(current["enabled"])
    value = raw.get("enabled", True)
    if type(value) is not bool:
        raise ValueError("invalid_cron_enabled")
    return value


def _metadata(raw, caller, current):
    if not isinstance(raw, dict):
        raise ValueError("invalid_cron_metadata")
    raw = public_value(raw)
    reserved = (
        "task_",
        "broadcast_",
        "batch_dispatch_",
        "_scheduler",
        "_cron_migration",
    )
    result = {
        key: value
        for key, value in raw.items()
        if not key.startswith(reserved)
        and key
        not in {
            "external_job_id",
            "creator_user_id",
            "pause_reason",
            "auto_paused_at",
        }
    }
    result["creator_user_id"] = (
        current["creator_user_id"] if current else caller.actor_id
    )
    result["job_origin"] = (
        current.get("job_origin", "manual") if current else "manual"
    )
    if current:
        previous = json.loads(current["spec_json"]).get("meta", {})
        for key in BROADCAST_RELATION_META_FIELDS:
            if key in previous:
                result[key] = copy.deepcopy(previous[key])
        if "batch_dispatch_priority" in previous:
            result["batch_dispatch_priority"] = copy.deepcopy(
                previous["batch_dispatch_priority"],
            )
    return result
