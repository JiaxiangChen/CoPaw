# -*- coding: utf-8 -*-
"""把真实批回执成员正规化，不按运行入口推断批归属或复制执行状态。"""

import json
from datetime import timezone

from .operations import IDENTITY_FIELDS, utc_now
from .storage import insert

MEMBERS = "swe_cron_managed_batch_members"
RECEIPTS = "swe_cron_managed_trigger_receipts"


class BatchRelationUnavailable(RuntimeError):
    """批次关系缺失或身份不一致，不能伪造完整的监控数据。"""


def _key(row):
    return tuple(row[name] for name in IDENTITY_FIELDS)


def _manifest_identity(item):
    identity = item["identity"]
    identifier = item["execution_id"]
    if not isinstance(identity, list) or len(identity) != 4:
        raise ValueError()
    if not all(
        isinstance(part, str) and 0 < len(part) <= 128 for part in identity
    ):
        raise ValueError()
    if not isinstance(identifier, str) or not 0 < len(identifier) <= 64:
        raise ValueError()
    return tuple(identity)


def _manifest(receipt):
    try:
        items = json.loads(receipt["members_json"])
        if not isinstance(items, list):
            raise ValueError()
        keys = [_manifest_identity(item) for item in items]
        identifiers = [item["execution_id"] for item in items]
        if any(
            len(key) != 4 or key[1] != receipt["source_id"] for key in keys
        ):
            raise ValueError()
        if len(set(keys)) != len(keys) or len(set(identifiers)) != len(
            identifiers,
        ):
            raise ValueError()
        return items
    except (ValueError, TypeError, KeyError) as exc:
        raise BatchRelationUnavailable("cron_batch_manifest_invalid") from exc


async def _runs(db, items):
    identifiers = [item["execution_id"] for item in items]
    joins = " AND ".join(
        f"r.{name}=d.{name} AND r.{name} COLLATE utf8mb4_bin=d.{name}"
        for name in IDENTITY_FIELDS
    )
    result = {}
    for offset in range(0, len(identifiers), 500):
        group = identifiers[offset : offset + 500]
        rows = await db.fetch_all(
            "SELECT r.execution_id,r.tenant_id,r.source_id,r.agent_id,r.job_id,"
            "r.dispatch_order,r.dispatch_meta_json,d.management_owner "
            f"FROM swe_cron_execution_runs r JOIN swe_cron_jobs_v2 d ON {joins} "
            "WHERE r.execution_id IN (" + ",".join("%s" for _ in group) + ")",
            tuple(group),
        )
        result.update({row["execution_id"]: row for row in rows})
    return result


def _validate_run(item, run):
    try:
        valid = (
            run is not None
            and _key(run) == tuple(item["identity"])
            and run["management_owner"] == "scheduler"
            and json.loads(run["dispatch_meta_json"]).get("protocol")
            == "scheduler_managed_v1"
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise BatchRelationUnavailable("cron_batch_execution_invalid") from exc
    if not valid:
        raise BatchRelationUnavailable("cron_batch_execution_invalid")


async def record_batch_members(db, batch_id, *, now=None):
    """由受理事务调用；无批次不写入，已有当前尝试永不被初始化覆盖。"""
    if batch_id is None:
        return []
    receipt = await db.fetch_one(
        f"SELECT * FROM {RECEIPTS} WHERE batch_id=%s AND kind='batch' AND status='accepted'",
        (batch_id,),
    )
    if receipt is None:
        raise BatchRelationUnavailable("cron_batch_receipt_missing")
    items = _manifest(receipt)
    runs = await _runs(db, items)
    existing = await db.fetch_all(
        f"SELECT * FROM {MEMBERS} WHERE batch_id=%s",
        (batch_id,),
    )
    by_id = {row["member_id"]: row for row in existing}
    when = now or utc_now()
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    result = []
    for order, item in enumerate(items):
        run = runs.get(item["execution_id"])
        _validate_run(item, run)
        result.append(await _record_one(db, receipt, run, by_id, when, order))
    return result


async def _record_one(db, receipt, run, existing, now, order):
    execution_id = run["execution_id"]
    previous = existing.get(execution_id)
    if previous is not None:
        if (
            _key(previous) != _key(run)
            or previous["initial_execution_id"] != execution_id
        ):
            raise BatchRelationUnavailable(
                "cron_batch_member_identity_conflict",
            )
        return previous
    row = {
        "batch_id": receipt["batch_id"],
        "member_id": execution_id,
        **{name: run[name] for name in IDENTITY_FIELDS},
        "initial_execution_id": execution_id,
        "current_execution_id": execution_id,
        "business_attempt": 1,
        "member_role": "parent" if _key(run) == _key(receipt) else "child",
        # 回执数组就是本批冻结顺序；复用的普通运行可能保存不同的原顺位。
        "dispatch_order": order,
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }
    await insert(db, MEMBERS, row)
    return row
