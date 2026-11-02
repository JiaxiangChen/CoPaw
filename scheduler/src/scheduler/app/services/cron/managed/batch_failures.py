# -*- coding: utf-8 -*-
"""批失败预览使用稳定成员与当前尝试，不凭入口类型猜测归属。"""

from collections import Counter
import json

from .batch_members import (
    MEMBERS,
    RECEIPTS,
    BatchRelationUnavailable,
    _manifest,
)
from .dispatch_events import failure_type, _safe_detail
from .repository import IDENTITY_FIELDS

LABELS = {
    "auth_expired": "认证失效",
    "subtask_timeout": "子任务超时",
    "subtask_failed": "子任务失败",
    "configuration": "配置错误",
    "model_rate_limit": "模型限流",
    "outcome_unknown": "执行结果未知",
    "execution_timeout": "执行超时",
    "service_error": "服务异常",
    "other": "其他错误",
}
STOP_TYPES = {
    "other",
    "outcome_unknown",
    "execution_timeout",
    "subtask_timeout",
}
RUN_FIELDS = (
    *IDENTITY_FIELDS,
    "execution_id",
    "definition_version",
    "enqueued_state_version",
    "status",
    "settled_status",
    "settled_error_code",
    "error_code",
    "result_json",
    "version",
    "actor_id",
    "kind",
    "scheduled_fire_at",
    "dispatch_meta_json",
    "dispatch_attempt",
    "updated_at",
    "accepted_at",
    "started_at",
    "finished_at",
    "settled_at",
    "history_execution_id",
)


def require_manager(caller):
    if not caller.is_manager:
        raise PermissionError("cron_batch_manager_required")


async def read_batch(db, caller, batch_id, *, lock=False):
    require_manager(caller)
    row = await db.fetch_one(
        f"SELECT * FROM {RECEIPTS} WHERE source_id=%s AND source_id COLLATE utf8mb4_bin=%s "
        "AND batch_id=%s AND kind='batch' AND status='accepted'"
        + (" FOR UPDATE" if lock else ""),
        (caller.source_id, caller.source_id, batch_id),
    )
    if row is None or row["source_id"] != caller.source_id:
        raise LookupError("cron_batch_not_found")
    return row


async def member_rows(db, receipt, *, lock=False, member_ids=None):
    joins = " AND ".join(
        f"m.{name}=r.{name} AND m.{name} COLLATE utf8mb4_bin=r.{name}"
        for name in IDENTITY_FIELDS
    )
    definitions = " AND ".join(
        f"m.{name}=d.{name} AND m.{name} COLLATE utf8mb4_bin=d.{name}"
        for name in IDENTITY_FIELDS
    )
    states = " AND ".join(
        f"m.{name}=s.{name} AND m.{name} COLLATE utf8mb4_bin=s.{name}"
        for name in IDENTITY_FIELDS
    )
    selection = (
        ""
        if member_ids is None
        else " AND m.member_id IN (" + ",".join("%s" for _ in member_ids) + ")"
    )
    rows = await db.fetch_all(
        "SELECT "
        + ",".join(f"r.{name}" for name in RUN_FIELDS)
        + ",m.member_id,m.version AS member_version,m.business_attempt,"
        "m.initial_execution_id,m.current_execution_id,m.member_role,m.dispatch_order AS member_order,"
        "d.definition_version AS current_definition_version,d.management_owner,s.job_id AS current_state_job_id,"
        "s.queue_invalidation_version AS current_queue_invalidation_version "
        f"FROM {MEMBERS} m LEFT JOIN swe_cron_execution_runs r ON m.current_execution_id=r.execution_id AND {joins} "
        f"LEFT JOIN swe_cron_jobs_v2 d ON {definitions} LEFT JOIN swe_cron_job_states s ON {states} WHERE m.batch_id=%s"
        + selection
        + " ORDER BY m.dispatch_order,m.member_id"
        + (" FOR UPDATE" if lock else ""),
        (receipt["batch_id"], *(member_ids or ())),
    )
    manifest = _manifest(receipt)
    if member_ids is not None:
        manifest = [
            item for item in manifest if item["execution_id"] in member_ids
        ]
    expected = {
        item["execution_id"]: tuple(item["identity"]) for item in manifest
    }
    if (
        len(rows) != len(manifest)
        or {row["initial_execution_id"] for row in rows} != expected.keys()
    ):
        raise BatchRelationUnavailable("cron_batch_members_incomplete")
    if any(not _valid_member(row, expected) for row in rows):
        raise BatchRelationUnavailable("cron_batch_execution_invalid")
    return rows


def _valid_member(row, expected):
    try:
        return (
            row["execution_id"] is not None
            and row["management_owner"] == "scheduler"
            and row["current_state_job_id"] is not None
            and tuple(row[name] for name in IDENTITY_FIELDS)
            == expected[row["initial_execution_id"]]
            and row["member_id"] == row["initial_execution_id"]
            and row["kind"] in {"ordinary", "batch"}
            and row["scheduled_fire_at"] is not None
            and json.loads(row["dispatch_meta_json"]).get("protocol")
            == "scheduler_managed_v1"
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def is_failure(row):
    return row["status"] in {"error", "dispatch_failed"} or (
        row["status"] == "success" and row.get("settled_status") == "error"
    )


def preview_item(row):
    return {
        **{name: row[name] for name in IDENTITY_FIELDS},
        "member_id": row["member_id"],
        "execution_id": row["execution_id"],
        "member_version": row["member_version"],
        "execution_version": row["version"],
        "definition_version": row["current_definition_version"],
        "execution_definition_version": row["definition_version"],
        "business_attempt": row["business_attempt"],
        "status": row["status"],
        "settled_status": row.get("settled_status"),
        "failure_type": failure_type(row),
        "error_code": _safe_detail("error_code", row.get("error_code")),
        "settled_error_code": _safe_detail(
            "settled_error_code",
            row.get("settled_error_code"),
        ),
        "error_message": row.get("display_error_message", ""),
        "transport_only": row["status"] == "dispatch_failed"
        and not row.get("accepted_at"),
    }


async def display_error_messages(db, rows):
    ids = [
        row["history_execution_id"]
        for row in rows
        if row.get("history_execution_id")
    ]
    histories = (
        await db.fetch_all(
            "SELECT id,tenant_id,job_id,error_message FROM swe_cron_executions WHERE id IN ("
            + ",".join("%s" for _ in ids)
            + ")",
            tuple(ids),
        )
        if ids
        else []
    )
    messages = {
        (item["id"], item["tenant_id"], item["job_id"]): item["error_message"]
        for item in histories
    }
    for row in rows:
        history = messages.get(
            (row.get("history_execution_id"), row["tenant_id"], row["job_id"]),
        )
        try:
            meta = json.loads(row.get("result_json") or "{}")
        except (ValueError, TypeError):
            meta = {}
        detail = meta.get("error_detail") if isinstance(meta, dict) else None
        code = row.get("settled_error_code") or row.get("error_code")
        if code == "cron_async_subtasks_failed":
            code = "子任务执行失败"
        row["display_error_message"] = next(
            (
                value[:2048]
                for value in (history, detail, code)
                if isinstance(value, str) and value.strip()
            ),
            "",
        )


async def failure_preview(db, caller, batch_id, types=()):
    invalid = set(types) - LABELS.keys()
    if invalid:
        raise ValueError("invalid_cron_failure_type")
    receipt = await read_batch(db, caller, batch_id)
    rows = [row for row in await member_rows(db, receipt) if is_failure(row)]
    counts = Counter(failure_type(row) for row in rows)
    selected = [row for row in rows if not types or failure_type(row) in types]
    await display_error_messages(db, selected[:200])
    return {
        "batch_id": batch_id,
        "counts": [
            {"failure_type": kind, "label": label, "count": counts[kind]}
            for kind, label in LABELS.items()
            if counts[kind]
        ],
        "items": [preview_item(row) for row in selected[:200]],
        "matched_count": len(selected),
        "has_more": len(selected) > 200,
    }
