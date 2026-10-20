# -*- coding: utf-8 -*-
"""按历史归属核对新版完成通知的冻结执行快照，不读取任务文件。"""

import json

from .models import CronJobSpec

_IDENTITY_KEYS = ("tenant_id", "source_id", "agent_id", "job_id")


def notification_metadata(row):
    """读取历史元数据；损坏的归属信息不能静默当作旧任务。"""
    raw = row.get("meta") or "{}"
    metadata = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(metadata, dict):
        raise ValueError("cron_notification_metadata_invalid")
    return metadata


async def load_notification_spec(db, row, metadata):
    """只读核对当前归属及历史绑定，返回该次执行的任务快照。"""
    execution_id = metadata.get("managed_execution_id")
    key = (
        row.get("tenant_id"),
        metadata.get("source_id"),
        metadata.get("agent_id"),
        row.get("job_id"),
    )
    if any(
        not isinstance(value, str) or not value
        for value in (execution_id, *key)
    ):
        raise ValueError("cron_notification_identity_missing")
    if row.get("source_id") and row["source_id"] != key[1]:
        raise ValueError("cron_notification_identity_conflict")
    if db is None:
        raise RuntimeError("cron_notification_database_unavailable")
    joins = " AND ".join(f"r.{name}=d.{name}" for name in _IDENTITY_KEYS)
    clauses = " AND ".join(f"r.{name}=%s" for name in _IDENTITY_KEYS)
    definition_keys = ", ".join(
        f"d.{name} AS definition_{name}" for name in _IDENTITY_KEYS
    )
    run = await db.fetch_one(
        "SELECT r.spec_json, r.tenant_id, r.source_id, r.agent_id, r.job_id, "
        f"{definition_keys} "
        f"FROM swe_cron_execution_runs r JOIN swe_cron_jobs_v2 d ON {joins} "
        f"WHERE r.execution_id=%s AND r.history_execution_id=%s AND {clauses} "
        "AND r.status='success' AND d.management_owner='scheduler' "
        "AND d.deleted_at IS NULL",
        (execution_id, int(row["id"]), *key),
    )
    if run is None or tuple(run[name] for name in _IDENTITY_KEYS) != key:
        raise ValueError("managed_cron_notification_not_found")
    if tuple(run[f"definition_{name}"] for name in _IDENTITY_KEYS) != key:
        raise ValueError("cron_notification_identity_conflict")
    spec = CronJobSpec.model_validate_json(run["spec_json"])
    if (spec.tenant_id, spec.source_id, spec.id) != (key[0], key[1], key[3]):
        raise ValueError("cron_notification_snapshot_conflict")
    return spec
