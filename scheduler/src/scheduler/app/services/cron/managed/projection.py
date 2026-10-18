# -*- coding: utf-8 -*-
"""只维护 Scheduler 新任务的旧表投影，不调用或修改旧写入逻辑。"""

import json

from .definition import BROADCAST_RELATION_META_FIELDS, canonical
from .storage import insert, update


def _request_preview(value):
    encoded = canonical(value)
    if len(encoded) <= 4096:
        return encoded
    return canonical({"_truncated": True, "preview": encoded[:1024]})


def _meta_preview(meta):
    encoded = canonical(meta)
    if len(encoded) <= 4096:
        return encoded
    core = {
        key: value
        for key, value in meta.items()
        if key.startswith("task_")
        or key
        in {
            *BROADCAST_RELATION_META_FIELDS,
            "job_origin",
            "agent_id",
            "scope_id",
            "management_owner",
            "creator_user_id",
            "pause_reason",
            "model_slot",
        }
    }
    core["compat_meta_truncated"] = True
    return canonical(core)


async def write_projection(
    db,
    spec,
    state,
    agent_id,
    now,
    *,
    deleted=False,
    expect_new=False,
):
    existing = await db.fetch_one(
        "SELECT * FROM swe_cron_jobs WHERE id = %s FOR UPDATE",
        (spec["id"],),
    )
    if existing and expect_new:
        raise ValueError("legacy_projection_already_exists")
    if existing and not _owned(existing, spec, agent_id):
        raise ValueError("legacy_projection_identity_conflict")
    if deleted and existing is None:
        return
    values = _values(spec, state, agent_id, now, deleted)
    if existing:
        await update(db, "swe_cron_jobs", values, "id = %s", (spec["id"],))
    else:
        await insert(
            db,
            "swe_cron_jobs",
            {"id": spec["id"], "created_at": now, **values},
        )


def _owned(row, spec, agent_id):
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (ValueError, TypeError):
        return False
    return (row["tenant_id"], row["source_id"]) == (
        spec["tenant_id"],
        spec["source_id"],
    ) and (
        isinstance(meta, dict)
        and meta.get("management_owner") == "scheduler"
        and meta.get("agent_id") == agent_id
    )


def _values(spec, state, agent_id, now, deleted):
    meta = dict(spec.get("meta", {}))
    meta.update(
        agent_id=agent_id,
        management_owner="scheduler",
        model_slot=spec.get("model_slot"),
        scope_id=spec["scope_id"],
        job_origin=spec.get("meta", {}).get("job_origin", "manual"),
    )
    for key, column in {
        "task_chat_id": "task_chat_id",
        "task_session_id": "task_session_id",
        "task_unread_execution_count": "unread_execution_count",
        "task_has_scheduled_result": "has_scheduled_result",
        "task_last_scheduled_preview": "latest_scheduled_preview",
        "pause_reason": "pause_reason",
    }.items():
        meta[key] = state.get(column)
    runtime = spec["runtime"]
    paused = state.get("pause_reason") or (
        "manual" if not spec["enabled"] else ""
    )
    return {
        "name": spec["name"],
        "tenant_id": spec["tenant_id"],
        "source_id": spec["source_id"],
        "tenant_name": spec.get("tenant_name") or "",
        "bbk_id": spec.get("bbk_id") or "",
        "enabled": False if deleted else spec["enabled"],
        "task_type": spec["task_type"],
        "cron_expr": spec["schedule"]["cron"],
        "timezone": spec["schedule"]["timezone"],
        "channel": spec["dispatch"].get("channel", "console"),
        "target_user_id": spec["dispatch"]["target"]["user_id"],
        "target_session_id": spec["dispatch"]["target"]["session_id"],
        **runtime,
        "text_content": (spec.get("text") or "")[:4096],
        "request_input": _request_preview(spec.get("request") or {}),
        "creator_user_id": spec["meta"]["creator_user_id"],
        "task_chat_id": state.get("task_chat_id") or "",
        "task_session_id": state.get("task_session_id") or "",
        "plan_id": spec.get("plan_id"),
        "skill_ids": spec.get("skill_ids") or "",
        "workflow_binding_id": spec.get("workflow_binding_id"),
        "job_origin": spec.get("meta", {}).get("job_origin", "manual"),
        "meta": _meta_preview(meta),
        "status": "deleted" if deleted else "paused" if paused else "active",
        "pause_reason": paused,
        "deleted_at": now if deleted else None,
        "updated_at": now,
    }
