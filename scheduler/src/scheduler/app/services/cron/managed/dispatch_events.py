# -*- coding: utf-8 -*-
"""新执行事件的安全契约，不携带认证信息、派发令牌或原始异常。"""

import hashlib
import json
import re

EVENTS = "swe_cron_managed_dispatch_events"
KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
EVENT_TYPES = frozenset(
    {
        "queued",
        "dispatching",
        "accepted",
        "running",
        "success",
        "error",
        "skipped",
        "cancelled",
        "settled",
        "transport_redelivery",
        "dispatch_unconfirmed",
        "transport_requeued",
        "retry_request",
        "retry_accepted",
        "retry_skipped",
        "auto_retry_scheduled",
    },
)
STATUSES = frozenset(
    {
        "queued",
        "dispatching",
        "dispatch_failed",
        "accepted",
        "running",
        "success",
        "error",
        "skipped",
        "cancelled",
    },
)
FAILURE_TYPES = frozenset(
    {
        "auth_expired",
        "subtask_timeout",
        "subtask_failed",
        "configuration",
        "model_rate_limit",
        "outcome_unknown",
        "execution_timeout",
        "service_error",
        "other",
    },
)
DETAIL_FIELDS = frozenset(
    {
        "status",
        "error_code",
        "failure_type",
        "dispatch_attempt",
        "definition_version",
        "settled_status",
        "settled_error_code",
        "business_attempt",
        "previous_execution_id",
        "reason",
    },
)


def failure_type(run):
    if run.get("settled_error_code") == "cron_async_subtasks_timeout":
        return "subtask_timeout"
    if run.get("settled_error_code") == "cron_async_subtasks_failed":
        return "subtask_failed"
    try:
        meta = json.loads(run.get("result_json") or "{}")
    except (ValueError, TypeError):
        meta = {}
    selected = meta.get("failure_type") if isinstance(meta, dict) else None
    if isinstance(selected, str) and selected in FAILURE_TYPES:
        return selected
    return {
        "cron_execution_timeout": "execution_timeout",
        "cron_execution_lease_expired": "outcome_unknown",
        "cron_dispatch_unconfirmed": "outcome_unknown",
    }.get(run.get("error_code"), "other")


def _safe_detail(name, value):
    if value is None:
        return None
    if name in {"dispatch_attempt", "definition_version", "business_attempt"}:
        return value if type(value) is int and value >= 0 else None
    if name in {"status", "settled_status"}:
        return (
            value
            if isinstance(value, str) and value in STATUSES
            else "unknown"
        )
    if name == "failure_type":
        return (
            value
            if isinstance(value, str) and value in FAILURE_TYPES
            else "other"
        )
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return "cron_detail_redacted"


def event_values(run, kind, details=None):
    if kind not in EVENT_TYPES:
        raise ValueError("invalid_cron_event_type")
    identity = [run[name] for name in KEYS]
    key = hashlib.sha256(
        json.dumps(
            [*identity, run["execution_id"], kind, int(run["version"])],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode(),
    ).hexdigest()
    meta = json.loads(run["dispatch_meta_json"])
    selected = {
        name: run.get(name)
        for name in (
            "status",
            "error_code",
            "dispatch_attempt",
            "definition_version",
            "settled_status",
            "settled_error_code",
        )
    }
    selected.update(
        {
            name: value
            for name, value in (details or {}).items()
            if name in DETAIL_FIELDS
        },
    )
    selected = {
        name: _safe_detail(name, value) for name, value in selected.items()
    }
    if run.get("status") == "error" or run.get("settled_status") == "error":
        selected["failure_type"] = failure_type(run)
    return {
        "event_id": key,
        "event_key": key,
        **dict(zip(KEYS, identity)),
        "execution_id": run["execution_id"],
        "batch_id": meta.get("batch_id"),
        "member_id": (
            (meta.get("batch_member_id") or run["execution_id"])
            if meta.get("batch_id")
            else None
        ),
        "event_type": kind,
        "execution_version": run["version"],
        "actor_id": run["actor_id"],
        "occurred_at": run["updated_at"],
        "details_json": json.dumps(
            selected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


async def append_run_event(db, run, kind, details=None):
    """在调用方事实事务内写入，版本不变的重复事实不追加事件。"""
    if (
        json.loads(run["dispatch_meta_json"]).get("protocol")
        != "scheduler_managed_v1"
    ):
        return False
    values = event_values(run, kind, details)
    existing = await db.fetch_one(
        f"SELECT event_id FROM {EVENTS} WHERE event_key=%s",
        (values["event_key"],),
    )
    if existing:
        return False
    columns = ",".join(values)
    await db.execute(
        f"INSERT INTO {EVENTS} ({columns}) VALUES ("
        + ",".join("%s" for _ in values)
        + ")",
        tuple(values.values()),
    )
    return True
