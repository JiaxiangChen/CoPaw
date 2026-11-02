# -*- coding: utf-8 -*-
"""批成员业务重试共用入队，运输恢复仍复用原执行身份。"""

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone

from .batch_failures import (
    STOP_TYPES,
    failure_preview,
    is_failure,
    member_rows,
    read_batch,
)
from .batch_members import MEMBERS
from .batch_retry_contract import BatchRetryConflict, ManagedBatchRetryRequest
from .definition import canonical, request_hash
from .dispatch_events import EVENTS, append_run_event, failure_type
from .executions import (
    ExecutionConflict,
    ExecutionRequest,
    ExecutionStore,
    JOB_WHERE,
)
from .operations import utc_now
from .repository import IDENTITY_FIELDS, _SELECT
from .storage import insert, transaction, update

logger = logging.getLogger(__name__)


class BatchRetryGrant:
    """授权只来自已锁定批成员，不制造目标用户的管理员身份。"""

    def __init__(self, key, *, require_manager=True):
        self.key = key
        self.require_manager = require_manager

    async def authorize(self, db, caller, job_id):
        if (self.require_manager and not caller.is_manager) or caller.job_key(
            job_id,
        ) != self.key:
            raise PermissionError("cron_batch_retry_forbidden")
        where = " AND ".join(
            f"d.{name}=%s AND d.{name} COLLATE utf8mb4_bin=%s"
            for name in IDENTITY_FIELDS
        )
        row = await db.fetch_one(
            f"{_SELECT} WHERE {where} FOR UPDATE",
            tuple(value for part in self.key for value in (part, part)),
        )
        if row is None or row["management_owner"] != "scheduler":
            raise ExecutionConflict("cron_job_not_ready")
        return row


def _stale(row, candidate):
    pairs = (
        (row["execution_id"], candidate.execution_id),
        (row["member_version"], candidate.member_version),
        (row["version"], candidate.execution_version),
        (row["business_attempt"], candidate.business_attempt),
        (row["current_definition_version"], candidate.definition_version),
    )
    return any(left != right for left, right in pairs)


def _utc(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed is None:
        raise ValueError("cron_batch_logical_time_missing")
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


class BatchRetryService:
    def __init__(self, db):
        self.db = db

    async def preview(self, caller, batch_id, types=()):
        return await failure_preview(self.db, caller, batch_id, types)

    async def retry(self, caller, batch_id, command: ManagedBatchRetryRequest):
        receipt = await read_batch(self.db, caller, batch_id)
        key = tuple(receipt[name] for name in IDENTITY_FIELDS)
        digest = request_hash(
            [caller.source_id, caller.actor_id, batch_id, command.request_id],
        )
        fingerprint = request_hash(command.model_dump())
        async with transaction(self.db) as tx:
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
                key,
            )
            receipt = await read_batch(tx, caller, batch_id, lock=True)
            previous = await self._previous(tx, digest, fingerprint)
            if previous is not None:
                return previous
            rows = await member_rows(tx, receipt)
            by_id = {row["member_id"]: row for row in rows}
            if any(item.member_id not in by_id for item in command.candidates):
                raise LookupError("cron_batch_member_not_found")
            await self._lock_candidates(tx, by_id, command.candidates)
            # 等待行锁时执行状态可能变化，必须重读，不用预览快照执行。
            by_id = {
                row["member_id"]: row
                for row in await member_rows(
                    tx,
                    receipt,
                    lock=True,
                    member_ids=tuple(
                        item.member_id for item in command.candidates
                    ),
                )
            }
            result = await self._apply(
                tx,
                caller,
                batch_id,
                command,
                by_id,
                digest,
            )
            await self._audit(tx, receipt, caller, digest, fingerprint, result)
        logger.info(
            "人工批重试已提交 batch_id=%s request_id=%s queued=%s skipped=%s",
            batch_id,
            command.request_id,
            result["queued"],
            result["skipped"],
        )
        return result

    @staticmethod
    async def _previous(tx, digest, fingerprint):
        previous = await tx.fetch_one(
            f"SELECT details_json FROM {EVENTS} WHERE event_key=%s",
            (digest,),
        )
        if previous is None:
            return None
        payload = json.loads(previous["details_json"])
        if payload["request_hash"] != fingerprint:
            raise BatchRetryConflict("cron_retry_request_content_conflict")
        return {**payload["response"], "reused": True}

    @staticmethod
    async def _lock_candidates(tx, rows, candidates):
        selected = sorted(
            (rows[item.member_id] for item in candidates),
            key=lambda row: tuple(row[name] for name in IDENTITY_FIELDS),
        )
        for row in selected:
            key = tuple(row[name] for name in IDENTITY_FIELDS)
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
                key,
            )
            await tx.fetch_one(
                "SELECT execution_id FROM swe_cron_execution_runs WHERE execution_id=%s FOR UPDATE",
                (row["execution_id"],),
            )

    async def _apply(self, tx, caller, batch_id, command, rows, digest):
        results = []
        audit = {
            "batch_id": batch_id,
            "actor_id": caller.actor_id,
            "request_key": digest,
        }
        for candidate in command.candidates:
            row = rows[candidate.member_id]
            reason = self._skip_reason(row, candidate, command.confirm_stopped)
            if reason:
                results.append(
                    {
                        "member_id": row["member_id"],
                        "status": "skipped",
                        "reason": reason,
                    },
                )
                await self._decision(
                    tx,
                    row,
                    audit,
                    "retry_skipped",
                    reason,
                )
                continue
            try:
                new_run = await self._enqueue(
                    tx,
                    caller,
                    row,
                    batch_id,
                    digest,
                )
            except ExecutionConflict as exc:
                reason = str(exc)
                results.append(
                    {
                        "member_id": row["member_id"],
                        "status": "skipped",
                        "reason": reason,
                    },
                )
                await self._decision(
                    tx,
                    row,
                    audit,
                    "retry_skipped",
                    reason,
                )
                continue
            await self._decision(
                tx,
                new_run,
                audit,
                "retry_accepted",
                "cron_retry_queued",
                member_id=row["member_id"],
            )
            results.append(
                {
                    "member_id": row["member_id"],
                    "execution_id": new_run["execution_id"],
                    "status": "queued",
                    "transport_only": new_run["execution_id"]
                    == row["execution_id"],
                },
            )
        queued = sum(item["status"] == "queued" for item in results)
        return {
            "batch_id": batch_id,
            "request_id": command.request_id,
            "queued": queued,
            "skipped": len(results) - queued,
            "results": results,
            "reused": False,
        }

    @staticmethod
    def _skip_reason(row, candidate, confirmed):
        if _stale(row, candidate):
            return "cron_retry_preview_stale"
        if not is_failure(row):
            return "cron_retry_not_failed"
        if row["status"] == "dispatch_failed" and row.get("accepted_at"):
            return "cron_retry_acceptance_inconsistent"
        if failure_type(row) in STOP_TYPES and not confirmed:
            raise ValueError("cron_retry_confirmation_required")
        return None

    @staticmethod
    async def _enqueue(
        tx,
        caller,
        row,
        batch_id,
        digest,
        *,
        grant=None,
        retry_at=None,
    ):
        key = tuple(row[name] for name in IDENTITY_FIELDS)
        target = replace(
            caller,
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
        )
        store = ExecutionStore(tx, grant=grant or BatchRetryGrant(key))
        current = await store.grant.authorize(tx, target, row["job_id"])
        from .executions import _ready

        command = ExecutionRequest(
            "batch",
            request_hash([digest, row["member_id"]]),
            scheduled_at=_utc(row["scheduled_fire_at"]),
            expected_definition_version=row["current_definition_version"],
            not_before_at=retry_at or datetime.now(timezone.utc),
            dispatch_meta={
                "batch_id": batch_id,
                "batch_member_id": row["member_id"],
                "business_attempt": row["business_attempt"] + 1,
                "previous_execution_id": row["execution_id"],
                "dispatch_order": row["member_order"],
            },
        )
        _ready(current, command)
        if row["status"] == "dispatch_failed" and not row.get("accepted_at"):
            values = {
                "status": "queued",
                "dispatch_owner": "",
                "dispatch_lease_until": None,
                "error_code": None,
                "version": row["version"] + 1,
                "updated_at": utc_now(),
            }
            await update(
                tx,
                "swe_cron_execution_runs",
                values,
                "execution_id=%s AND version=%s",
                (row["execution_id"], row["version"]),
            )
            result = {**row, **values}
            await append_run_event(tx, result, "transport_requeued")
            return result
        run, _ = await store.enqueue_in_transaction(
            tx,
            target,
            row["job_id"],
            command,
        )
        await update(
            tx,
            MEMBERS,
            {
                "current_execution_id": run["execution_id"],
                "business_attempt": row["business_attempt"] + 1,
                "version": row["member_version"] + 1,
                "updated_at": utc_now(),
            },
            "batch_id=%s AND member_id=%s AND version=%s",
            (batch_id, row["member_id"], row["member_version"]),
        )
        return run

    @staticmethod
    async def _decision(
        tx,
        run,
        audit,
        kind,
        reason,
        *,
        member_id=None,
    ):
        selected = member_id or run["member_id"]
        key = request_hash([audit["request_key"], selected, kind])
        await insert(
            tx,
            EVENTS,
            {
                "event_id": key,
                "event_key": key,
                **{name: run[name] for name in IDENTITY_FIELDS},
                "execution_id": run["execution_id"],
                "batch_id": audit["batch_id"],
                "member_id": selected,
                "event_type": kind,
                "execution_version": run["version"],
                "actor_id": audit["actor_id"],
                "occurred_at": utc_now(),
                "details_json": canonical({"reason": reason}),
            },
        )

    @staticmethod
    async def _audit(tx, receipt, caller, digest, fingerprint, result):
        await insert(
            tx,
            EVENTS,
            {
                "event_id": digest,
                "event_key": digest,
                **{name: receipt[name] for name in IDENTITY_FIELDS},
                "execution_id": None,
                "batch_id": receipt["batch_id"],
                "member_id": None,
                "event_type": "retry_request",
                "execution_version": None,
                "actor_id": caller.actor_id,
                "occurred_at": utc_now(),
                "details_json": canonical(
                    {"request_hash": fingerprint, "response": result},
                ),
            },
        )
