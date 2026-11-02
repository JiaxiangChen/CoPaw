# -*- coding: utf-8 -*-
"""仅对已登记的新版批成员续跑失败尝试，不扫描定义补生触发。"""

import json
import logging
from datetime import datetime, timedelta, timezone

from .batch_failures import display_error_messages, member_rows
from .batch_members import MEMBERS, RECEIPTS
from .batch_retry import BatchRetryGrant, BatchRetryService, _utc
from .definition import request_hash
from .dispatch_events import append_run_event, failure_type
from .executions import ExecutionConflict, JOB_WHERE, RUN_TABLE
from .identity import CallerIdentity
from .operations import IDENTITY_FIELDS, utc_now
from .settlement import PROTOCOL, _history_matches
from .storage import transaction, update

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 300
SUBTASK_TIMEOUT_SECONDS = 7800
STOP_ERRORS = (
    "cron auth user_info is expired",
    "workflow sapid does not match target user",
    "技能任务结果不可用",
)


def _naive(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return _utc(parsed).replace(tzinfo=None)


class AutoBatchRetry:
    def __init__(self, db, *, limit=32, clock=None):
        self.db = db
        self.limit = limit
        self.clock = clock or utc_now
        self.after = 0

    async def tick(self):
        rows = await self._candidates(self.after)
        if not rows and self.after:
            rows = await self._candidates(0)
        if rows:
            self.after = rows[-1]["queue_order"]
        count = 0
        for row in rows:
            try:
                count += await self._retry(row)
            except ExecutionConflict:
                # 停用、删除、管理变更或其他执行条件不满足，不复活任务。
                continue
            except Exception as exc:
                logger.warning(
                    "批成员自动重试本轮未完成 batch_id=%s error_type=%s",
                    row["batch_id"],
                    type(exc).__name__,
                )
        return count

    async def _candidates(self, after):
        return await self.db.fetch_all(
            f"SELECT m.*,r.queue_order FROM {MEMBERS} m "
            f"JOIN {RUN_TABLE} r ON r.execution_id=m.current_execution_id "
            f"WHERE r.queue_order>%s AND {PROTOCOL} "
            "AND (m.business_attempt<%s OR (r.status='success' AND r.settled_status IS NULL)) "
            "AND (r.status='error' OR r.settled_status='error' OR "
            "(r.status='success' AND r.settled_status IS NULL AND "
            "COALESCE(r.started_at,r.accepted_at,r.created_at)<=%s)) "
            "ORDER BY r.queue_order LIMIT %s",
            (
                after,
                MAX_ATTEMPTS,
                self.clock() - timedelta(seconds=SUBTASK_TIMEOUT_SECONDS),
                self.limit,
            ),
        )

    async def _receipt(self, db, batch_id, *, lock=False):
        return await db.fetch_one(
            f"SELECT * FROM {RECEIPTS} WHERE batch_id=%s AND kind='batch' "
            "AND status='accepted'" + (" FOR UPDATE" if lock else ""),
            (batch_id,),
        )

    async def _retry(self, candidate):
        receipt = await self._receipt(self.db, candidate["batch_id"])
        if receipt is None:
            return False
        async with transaction(self.db) as tx:
            # 与人工重试保持父定义→回执→目标定义→执行→成员的锁顺序。
            await self._lock_definition(tx, receipt)
            receipt = await self._receipt(tx, receipt["batch_id"], lock=True)
            if receipt is None:
                return False
            await self._lock_definition(tx, candidate)
            await tx.fetch_one(
                f"SELECT execution_id FROM {RUN_TABLE} WHERE execution_id=%s FOR UPDATE",
                (candidate["current_execution_id"],),
            )
            rows = await member_rows(
                tx,
                receipt,
                lock=True,
                member_ids=(candidate["member_id"],),
            )
            row = rows[0]
            if not self._current(row, candidate):
                return False
            timed_out = await self._expire_subtasks(tx, row)
            if not await self._eligible(tx, row):
                return False
            return await self._enqueue(tx, receipt, row, timed_out)

    @staticmethod
    async def _lock_definition(tx, row):
        await tx.fetch_one(
            f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
            tuple(row[name] for name in IDENTITY_FIELDS),
        )

    @staticmethod
    def _current(row, candidate):
        return (
            row["execution_id"] == candidate["current_execution_id"]
            and row["definition_version"] == row["current_definition_version"]
            and row["enqueued_state_version"]
            >= (row.get("current_queue_invalidation_version") or 0)
        )

    async def _expire_subtasks(self, tx, row):
        if row["status"] != "success" or row["settled_status"] is not None:
            return False
        history = await tx.fetch_one(
            "SELECT job_id,tenant_id,status,async_status,meta FROM swe_cron_executions WHERE id=%s FOR UPDATE",
            (row["history_execution_id"],),
        )
        if not _history_matches(row, history) or history["async_status"] in {
            "success",
            "error",
        }:
            return False
        started = row["started_at"] or row["accepted_at"]
        if started is None or self.clock() < _naive(started) + timedelta(
            seconds=SUBTASK_TIMEOUT_SECONDS,
        ):
            return False
        values = {
            "settled_status": "error",
            "settled_error_code": "cron_async_subtasks_timeout",
            "settled_at": self.clock(),
            "updated_at": self.clock(),
            "version": row["version"] + 1,
        }
        await update(
            tx,
            RUN_TABLE,
            values,
            "execution_id=%s AND version=%s AND settled_status IS NULL",
            (row["execution_id"], row["version"]),
        )
        row.update(values)
        await append_run_event(tx, row, "settled")
        return True

    @staticmethod
    async def _eligible(tx, row):
        failed = row["status"] == "error" or (
            row["status"] == "success" and row["settled_status"] == "error"
        )
        if (
            not failed
            or row["business_attempt"] >= MAX_ATTEMPTS
            or failure_type(row) == "auth_expired"
        ):
            return False
        await display_error_messages(tx, [row])
        try:
            metadata = json.loads(row.get("result_json") or "{}")
        except (ValueError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        details = [
            row.get("display_error_message", ""),
            metadata.get("error_detail", ""),
        ]
        text = " ".join(str(value) for value in details).casefold()
        return not any(error in text for error in STOP_ERRORS)

    async def _enqueue(self, tx, receipt, row, timed_out):
        key = tuple(row[name] for name in IDENTITY_FIELDS)
        caller = CallerIdentity(*key[:3], row["actor_id"])
        digest = request_hash(
            [
                "auto_retry",
                receipt["batch_id"],
                row["member_id"],
                row["execution_id"],
            ],
        )
        finished = row["settled_at"] or row["finished_at"] or self.clock()
        due = (
            self.clock()
            if timed_out
            or row.get("settled_error_code") == "cron_async_subtasks_timeout"
            else _naive(finished) + timedelta(seconds=RETRY_DELAY_SECONDS)
        )
        run = await BatchRetryService._enqueue(
            tx,
            caller,
            row,
            receipt["batch_id"],
            digest,
            grant=BatchRetryGrant(key, require_manager=False),
            retry_at=due.replace(tzinfo=timezone.utc),
        )
        await append_run_event(
            tx,
            run,
            "auto_retry_scheduled",
            {
                "business_attempt": row["business_attempt"] + 1,
                "previous_execution_id": row["execution_id"],
            },
        )
        return True
