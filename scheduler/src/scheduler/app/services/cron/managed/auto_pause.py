# -*- coding: utf-8 -*-
"""将 SWE 已提交的自动暂停事实收敛为管理效果，不冒充创建人或管理员。"""

import json
import logging

from .controls import ControlService
from .definition import request_hash
from .identity import CallerIdentity
from .operations import (
    IDENTITY_FIELDS,
    JOURNAL,
    WHERE_JOB,
    ManagementConflict,
    utc_now,
)
from .repository import ManagedCronRepository

logger = logging.getLogger(__name__)


class AutoPauseGrant:
    """只授予某次已完成执行产生的同一暂停水位，不是通用跨用户权限。"""

    def __init__(self, key, payload):
        self.key, self.payload = key, payload

    async def validate(self, db, key, actor_id):
        if tuple(key) != self.key:
            raise ManagementConflict("cron_auto_pause_scope_conflict")
        await db.fetch_one(
            f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
            key,
        )
        row = await ManagedCronRepository(db)._get_row(
            key,
            include_deleted=True,
        )
        state = await db.fetch_one(
            f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB} FOR UPDATE",
            key,
        )
        cause = await db.fetch_one(
            f"SELECT actor_id FROM swe_cron_execution_runs WHERE execution_id=%s AND {WHERE_JOB} "
            "AND status='success' AND kind<>'manual' AND finished_at=%s",
            (
                self.payload["source_execution_id"],
                *key,
                self.payload["auto_paused_at"],
            ),
        )
        if row["deleted_at"] or state is None or cause is None:
            raise ManagementConflict("cron_auto_pause_obsolete")
        if (
            state["pause_reason"] != "auto_unread_threshold"
            or state["queue_invalidation_version"]
            != self.payload["auto_pause_epoch"]
            or str(state["auto_paused_at"]) != self.payload["auto_paused_at"]
        ):
            raise ManagementConflict("cron_auto_pause_obsolete")
        if cause["actor_id"] != actor_id:
            raise ManagementConflict("cron_auto_pause_obsolete")

    async def authorize_row(self, db, caller, job_id):
        await self.validate(db, caller.job_key(job_id), caller.actor_id)
        return await ManagedCronRepository(db)._get_row(
            self.key,
            include_deleted=True,
        )


class AutoPauseController:
    def __init__(self, db, platform, *, limit=32):
        self.db, self.platform, self.limit = db, platform, limit

    async def apply_fact(self, key, epoch, paused_at):
        cause = await self.db.fetch_one(
            f"SELECT execution_id,actor_id FROM swe_cron_execution_runs WHERE {WHERE_JOB} "
            "AND status='success' AND kind<>'manual' AND finished_at=%s ORDER BY execution_id LIMIT 1",
            (*key, paused_at),
        )
        if cause is None:
            return None
        row = await ManagedCronRepository(self.db)._get_row(
            tuple(key),
            include_deleted=True,
        )
        payload = {
            "request_id": request_hash([list(key), "auto_pause", epoch]),
            "enabled": False,
            "expected_management_version": row["state_management_version"],
            "auto_pause_epoch": epoch,
            "auto_paused_at": str(paused_at),
            "source_execution_id": cause["execution_id"],
        }
        caller = CallerIdentity(*key[:3], cause["actor_id"])
        service = ControlService(
            self.db,
            self.platform,
            grant=AutoPauseGrant(tuple(key), payload),
        )
        try:
            return await service.apply_auto_pause(caller, key[3], payload)
        except ManagementConflict:
            return None

    async def _pending(self):
        rows = await self.db.fetch_all(
            f"SELECT * FROM {JOURNAL} WHERE action='auto_pause' AND status IN ('pending','running') "
            "AND (lease_until IS NULL OR lease_until<=%s) ORDER BY updated_at,operation_id LIMIT %s",
            (utc_now().isoformat(" "), self.limit),
        )
        for row in rows:
            key = tuple(row[name] for name in IDENTITY_FIELDS)
            payload = json.loads(row["payload_json"])
            caller = CallerIdentity(*key[:3], row["actor_id"])
            try:
                await ControlService(
                    self.db,
                    self.platform,
                    grant=AutoPauseGrant(key, payload),
                ).retry(caller, key[3], row["operation_id"])
            except ManagementConflict:
                logger.info(
                    "旧自动暂停无需继续 job_id=%s operation_id=%s",
                    key[3],
                    row["operation_id"],
                )
        return len(rows)

    async def tick(self):
        checked = await self._pending()
        join = " AND ".join(
            f"d.{name}=s.{name} AND d.{name} COLLATE utf8mb4_bin=s.{name}"
            for name in IDENTITY_FIELDS
        )
        active = " AND ".join(
            f"j.{name}=d.{name} AND j.{name} COLLATE utf8mb4_bin=d.{name}"
            for name in IDENTITY_FIELDS
        )
        rows = await self.db.fetch_all(
            f"SELECT d.tenant_id,d.source_id,d.agent_id,d.job_id,s.queue_invalidation_version,s.auto_paused_at "
            f"FROM swe_cron_jobs_v2 d JOIN swe_cron_job_states s ON {join} "
            "WHERE d.management_owner='scheduler' AND d.deleted_at IS NULL AND d.enabled=1 "
            "AND s.management_status='ready' AND s.pause_reason='auto_unread_threshold' "
            f"AND NOT EXISTS (SELECT 1 FROM {JOURNAL} j WHERE {active} AND j.active_key IS NOT NULL) "
            "ORDER BY s.auto_paused_at,d.job_id LIMIT %s",
            (self.limit,),
        )
        for row in rows:
            await self.apply_fact(
                tuple(row[name] for name in IDENTITY_FIELDS),
                row["queue_invalidation_version"],
                row["auto_paused_at"],
            )
        return checked + len(rows)
