# -*- coding: utf-8 -*-
"""只为锁内确认的失败创建新尝试，保留批关系和原通知时间。"""

from dataclasses import dataclass
from datetime import timezone

from . import executions
from .batch_members import MEMBERS, RECEIPTS
from .daily_retry_policy import (
    MARKER,
    PROTOCOL,
    json_object,
    notification_target,
    retry_marker,
    utc_time,
)
from .daily_retry_scan import (
    definition_reason,
    history_reason,
    latest_reason,
    native_retry_reason,
    terminal_reason,
)
from .executions import (
    ExecutionConflict,
    ExecutionRequest,
    ExecutionStore,
    JOB_WHERE,
    RUN_TABLE,
)
from .identity import CallerIdentity
from .repository import IDENTITY_FIELDS, ManagedCronRepository
from .storage import update


@dataclass
class PreparedRetry:
    current: dict | None
    member: dict | None
    receipt: dict | None
    model: dict | None


class DailyRetryGrant:
    """权限固定为已按来源扫描并锁定的任务，不信任 HTTP 身份或正文。"""

    def __init__(self, current):
        self.current = current

    async def authorize(self, _db, caller, job_id):
        if tuple(
            self.current[name] for name in IDENTITY_FIELDS
        ) != caller.job_key(
            job_id,
        ):
            raise ExecutionConflict("cron_daily_retry_identity_conflict")
        return self.current


async def prepare_retry(db, row):
    key = tuple(row[name] for name in IDENTITY_FIELDS)
    current = await ManagedCronRepository(db)._get_row(
        key,
        include_deleted=True,
    )
    member = await db.fetch_one(
        f"SELECT * FROM {MEMBERS} WHERE current_execution_id=%s",
        (row["execution_id"],),
    )
    receipt = None
    if member:
        receipt = await _receipt(db, member)
    model = None
    if (
        not definition_reason(current)
        and json_object(current["spec_json"])["task_type"] == "agent"
    ):
        model = await executions.resolve_agent_model(
            current,
            json_object(current["spec_json"]),
        )
    return PreparedRetry(current, member, receipt, model)


async def _receipt(db, member, *, lock=False):
    order = int(member["dispatch_order"])
    if order < 0:
        return None
    return await db.fetch_one(
        f"SELECT receipt_id,batch_id,tenant_id,source_id,agent_id,job_id,kind,status,logical_fire_at,"
        f"JSON_EXTRACT(members_json,%s) AS manifest_member FROM {RECEIPTS} WHERE batch_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (f"$[{order}]", member["batch_id"]),
    )


async def lock_retry(tx, row, prepared):
    if prepared.receipt:
        await tx.fetch_one(
            f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
            tuple(prepared.receipt[name] for name in IDENTITY_FIELDS),
        )
    receipt = (
        await _receipt(tx, prepared.member, lock=True)
        if prepared.member
        else None
    )
    key = tuple(row[name] for name in IDENTITY_FIELDS)
    await tx.fetch_one(
        f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
        key,
    )
    current = await ManagedCronRepository(tx)._get_row(
        key,
        include_deleted=True,
    )
    locked = await tx.fetch_one(
        f"SELECT * FROM {RUN_TABLE} WHERE execution_id=%s FOR UPDATE",
        (row["execution_id"],),
    )
    member = None
    if prepared.member:
        member = await tx.fetch_one(
            f"SELECT * FROM {MEMBERS} WHERE batch_id=%s AND member_id=%s FOR UPDATE",
            (prepared.member["batch_id"], prepared.member["member_id"]),
        )
    return current, locked, member, receipt


def relation_reason(row, member, receipt):
    meta = json_object(row["dispatch_meta_json"])
    if member is None:
        return (
            "cron_daily_retry_batch_relation_missing"
            if row["kind"] == "batch" or meta.get("batch_id")
            else None
        )
    if (
        receipt is None
        or receipt["kind"] != "batch"
        or receipt["status"] != "accepted"
    ):
        return "cron_daily_retry_batch_relation_missing"
    manifest = json_object(receipt["manifest_member"])
    key = tuple(row[name] for name in IDENTITY_FIELDS)
    valid = (
        tuple(member[name] for name in IDENTITY_FIELDS) == key
        and receipt["source_id"] == row["source_id"]
        and member["current_execution_id"] == row["execution_id"]
        and member["member_id"] == member["initial_execution_id"]
        and manifest.get("execution_id") == member["initial_execution_id"]
        and manifest.get("identity") == list(key)
        and member["member_role"] in {"parent", "child"}
        and utc_time(row["scheduled_fire_at"])
        == utc_time(receipt["logical_fire_at"])
    )
    return None if valid else "cron_daily_retry_batch_not_current"


async def eligible_reason(tx, call, row, current, member, receipt, prepared):
    if (
        row["source_id"] != call["source_id"]
        or row["kind"] not in {"ordinary", "batch"}
        or json_object(row["dispatch_meta_json"]).get("protocol") != PROTOCOL
    ):
        return "cron_daily_retry_candidate_identity_invalid"
    reason = definition_reason(current) or terminal_reason(row, call)
    if reason:
        return reason
    if (
        prepared.current is None
        or current["definition_version"]
        != prepared.current["definition_version"]
    ):
        return "cron_daily_retry_definition_changed"
    reason = await latest_reason(tx, row, lock=True)
    if reason:
        return reason
    return (
        relation_reason(row, member, receipt)
        or await history_reason(tx, row)
        or native_retry_reason(row, current, member)
    )


def retry_command(call, row, current, member, now):
    original = json_object(row["dispatch_meta_json"])
    prior = retry_marker(row)
    due, zone = notification_target(row)
    metadata = {
        name: original[name]
        for name in (
            "dispatch_priority",
            "viewer_heat_score",
            "parent_scheduled_fire_at",
            "dispatch_order",
            "batch_id",
            "batch_member_id",
            "batch_created_at",
            "batch_queue_order",
            "dispatch_lane",
        )
        if name in original
    }
    metadata["previous_execution_id"] = row["execution_id"]
    metadata[MARKER] = {
        "call_id": call["call_id"],
        "previous_execution_id": row["execution_id"],
        "root_execution_id": prior.get("root_execution_id")
        or (member["initial_execution_id"] if member else row["execution_id"]),
        "retry_count": int(prior.get("retry_count") or 0) + 1,
        "notification_at": due,
        "notification_timezone": zone,
    }
    if member:
        metadata.update(
            {
                "batch_id": member["batch_id"],
                "batch_member_id": member["member_id"],
                "business_attempt": member["business_attempt"] + 1,
                "dispatch_order": member["dispatch_order"],
            },
        )
    return ExecutionRequest(
        "batch" if member else "ordinary",
        "daily_failure_retry:" + row["execution_id"],
        scheduled_at=utc_time(row["scheduled_fire_at"]).replace(
            tzinfo=timezone.utc,
        ),
        expected_definition_version=current["definition_version"],
        dispatch_meta=metadata,
        not_before_at=now.replace(tzinfo=timezone.utc),
    )


async def enqueue_retry(tx, call, row, current, member, prepared, now):
    key = tuple(row[name] for name in IDENTITY_FIELDS)
    caller = CallerIdentity(*key[:3], row["actor_id"] or row["tenant_id"])
    command = retry_command(call, row, current, member, now)
    if member:
        from .retry_priority import lane_for_batch

        command.dispatch_meta["dispatch_lane"] = await lane_for_batch(
            tx,
            member["batch_id"],
        )
    run, reused = await ExecutionStore(
        tx,
        grant=DailyRetryGrant(current),
    ).enqueue_in_transaction(
        tx,
        caller,
        row["job_id"],
        command,
        resolved_model=prepared.model,
    )
    if reused:
        return run, "skipped", "cron_daily_retry_already_accepted"
    if member:
        await update(
            tx,
            MEMBERS,
            {
                "current_execution_id": run["execution_id"],
                "business_attempt": member["business_attempt"] + 1,
                "version": member["version"] + 1,
                "updated_at": now,
            },
            "batch_id=%s AND member_id=%s AND version=%s",
            (member["batch_id"], member["member_id"], member["version"]),
        )
    return run, "queued", "cron_daily_retry_queued"
