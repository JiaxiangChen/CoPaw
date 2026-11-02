# -*- coding: utf-8 -*-
"""派发已持久登记的执行请求；HTTP 应答不代表实际执行结果。"""

from datetime import datetime, timedelta
from uuid import uuid4
import logging
import json

import httpx

from .executions import ExecutionStore, RUN_TABLE, TERMINAL_STATUSES
from .contracts import ManagedCronNotFound
from .identity import CallerIdentity, encode_scope, identity_value
from .operations import utc_now
from .repository import IDENTITY_FIELDS, ManagedCronRepository
from .storage import transaction, update
from .run_accounting import occupied_run_sql
from .swe_client import checked_base
from .dispatch_events import append_run_event

logger = logging.getLogger(__name__)
DISPATCH_RETRY_DELAYS_SECONDS = (5, 10, 20, 30, 60)


class ManagedSweExecutionClient:
    def __init__(self, base_url, *, http=None, headers=None):
        self.base_url = checked_base(base_url).removesuffix("/api")
        self.http = http
        self.headers = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower()
            in {
                "authorization",
                "x-header-cookie",
                "b3",
                "x-b3-traceid",
                "x-b3-spanid",
            }
        }

    async def execute(self, row, caller):
        return await self._call("execute", row, caller)

    async def recover(self, row, caller):
        """恢复既有执行的结果提交，不调用执行业务入口。"""
        return await self._call("recover", row, caller)

    async def _call(self, action, row, caller):
        headers = {
            **self.headers,
            "X-User-Id": caller.actor_id,
            "X-Tenant-Id": caller.tenant_id,
            "X-Source-Id": caller.source_id,
            "X-Agent-Id": caller.agent_id,
            "X-Scope-Id": encode_scope(caller.tenant_id, caller.source_id),
        }
        if caller.is_manager:
            headers["X-User-Role"] = "manager"
        body = {"execution_id": row["execution_id"], "job_id": row["job_id"]}
        if action == "execute":
            body["dispatch_token"] = row["dispatch_owner"]
        url = f"{self.base_url}/api/internal/cron/managed/{action}"
        if self.http is not None:
            response = await self.http.post(url, headers=headers, json=body)
        else:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
            ) as client:
                response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        result = response.json()
        if (
            not isinstance(result, dict)
            or result.get("execution_id") != row["execution_id"]
        ):
            raise ValueError("invalid_swe_execution_receipt")
        return result


class ExecutionDispatcher:
    def __init__(self, db, client, *, clock=None, capacity=None):
        self.db = db
        self.client = client
        self.clock = clock or utc_now
        self.capacity = capacity

    async def dispatch(self, caller, job_id, execution_id, *, retry=False):
        row = await ExecutionStore(self.db).get(caller, job_id, execution_id)
        return await self._dispatch(row, caller, retry=retry)

    async def _registered(self, execution_id):
        """内部后台入口只加载已有新协议记录，不根据定义生成执行。"""
        row = await self.db.fetch_one(
            f"SELECT * FROM {RUN_TABLE} WHERE execution_id=%s",
            (identity_value(execution_id),),
        )
        if (
            row is None
            or json.loads(row["dispatch_meta_json"]).get("protocol")
            != "scheduler_managed_v1"
        ):
            return None
        key = tuple(row[name] for name in IDENTITY_FIELDS)
        try:
            await ManagedCronRepository(self.db)._get_row(
                key,
                include_deleted=True,
            )
        except ManagedCronNotFound:
            return None
        # 使用受理时保存的真实操作者；不为后台伪造创建者或 manager 角色。
        caller = CallerIdentity(*key[:3], identity_value(row["actor_id"]))
        return row, caller

    async def dispatch_registered(self, execution_id, *, capacity_permit=None):
        registered = await self._registered(execution_id)
        if registered is None:
            return None
        row, caller = registered
        return await self._dispatch(
            row,
            caller,
            retry=True,
            capacity_permit=capacity_permit,
        )

    async def recover_registered(self, execution_id):
        registered = await self._registered(execution_id)
        if registered is None:
            return None
        row, caller = registered
        claimed = await self._claim(row, False, recovery=True)
        if claimed is None:
            return row
        try:
            await self.client.recover(claimed, caller)
        except Exception as exc:
            logger.warning(
                "执行结果恢复响应未确认 execution_id=%s error_type=%s",
                execution_id,
                type(exc).__name__,
            )
        # HTTP 内容不能替代共库事实，更不能把恢复失败转成另一次业务执行。
        return await self._settle(claimed, recovery=True)

    async def _dispatch(self, row, caller, *, retry, capacity_permit=None):
        execution_id, job_id = row["execution_id"], row["job_id"]
        claimed = await self._claim(
            row,
            retry,
            capacity_permit=capacity_permit,
        )
        if claimed is None:
            logger.debug(
                "本次执行暂不重复派发 execution_id=%s status=%s",
                execution_id,
                row["status"],
            )
            return row
        logger.info(
            "开始向 SWE 派发执行 execution_id=%s job_id=%s attempt=%s",
            execution_id,
            job_id,
            claimed["dispatch_attempt"],
        )
        try:
            await self.client.execute(claimed, caller)
        except Exception as exc:
            # 请求可能已被 SWE 受理，必须核对共库记录。
            # 不能因响应丢失新建执行，也不能只凭 HTTP 结果判定受理。
            logger.warning(
                "SWE 派发响应未确认，转为核对共库 execution_id=%s error_type=%s",
                execution_id,
                type(exc).__name__,
            )
        result = await self._settle(claimed)
        logger.info(
            "执行派发已核对共库 execution_id=%s status=%s",
            execution_id,
            result["status"],
        )
        return result

    async def _claim(
        self,
        row,
        retry,
        *,
        recovery=False,
        capacity_permit=None,
    ):
        permit = await self._capacity_permit(row, recovery, capacity_permit)
        if self.capacity is not None and not recovery and permit is None:
            return None
        return await self._claim_transaction(row, retry, recovery, permit)

    async def _capacity_permit(self, row, recovery, capacity_permit):
        permit = capacity_permit
        if permit is not None and (recovery or self.capacity is None):
            raise ValueError("invalid_cron_capacity_permit")
        if recovery or self.capacity is None:
            return None
        if permit is None:
            permit = await self.capacity.prepare(row)
        if permit is not None and permit.scope != tuple(
            row[name] for name in ("source_id", "provider_id", "model_id")
        ):
            raise ValueError("cron_capacity_permit_scope_conflict")
        return permit

    async def _claim_transaction(self, row, retry, recovery, permit):
        async with transaction(self.db) as tx:
            if permit is not None and not await self.capacity.lock(tx, permit):
                return None
            # 与 SWE 启动及单任务启停遵守同样的定义→执行锁顺序。
            key = tuple(row[name] for name in IDENTITY_FIELDS)
            where = " AND ".join(f"{name}=%s" for name in IDENTITY_FIELDS)
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {where} FOR UPDATE",
                key,
            )
            definition = await ManagedCronRepository(tx)._get_row(
                key,
                include_deleted=True,
            )
            current = await tx.fetch_one(
                f"SELECT * FROM {RUN_TABLE} WHERE execution_id=%s FOR UPDATE",
                (row["execution_id"],),
            )
            if not self._claimable(current, retry, recovery):
                return None
            if not recovery and not self._due(current, definition):
                return None
            if permit is not None and not await self._can_reserve(
                tx,
                permit,
                current,
                definition,
            ):
                return None
            values = {
                "status": (
                    current["status"]
                    if current["status"] in {"accepted", "running"}
                    else "dispatching"
                ),
                "dispatch_owner": str(uuid4()),
                "dispatch_lease_until": self.clock() + timedelta(seconds=30),
                "dispatch_attempt": current["dispatch_attempt"]
                + (not recovery),
                "version": current["version"] + 1,
                "updated_at": self.clock(),
            }
            await update(
                tx,
                RUN_TABLE,
                values,
                "execution_id=%s AND version=%s",
                (current["execution_id"], current["version"]),
            )
            result = {**current, **values}
            if not recovery:
                kind = (
                    "dispatching"
                    if values["status"] == "dispatching"
                    else "transport_redelivery"
                )
                await append_run_event(tx, result, kind)
            return result

    def _claimable(self, current, retry, recovery=False):
        if current is None or current["status"] in TERMINAL_STATUSES:
            return False
        if (current["status"] == "running") != recovery:
            return False
        if current["status"] == "dispatch_failed" and not retry:
            return False
        lease = current["dispatch_lease_until"]
        if isinstance(lease, str):
            lease = datetime.fromisoformat(lease)
        return lease is None or lease <= self.clock()

    @staticmethod
    def _invalidated(row, definition):
        # 停用、删除及曾经停用的旧队列应尽快由 SWE 原子标记 skipped，
        # 不必等散列到期；恢复后也不能让旧队列重新具备执行资格。
        return bool(
            definition["deleted_at"]
            or not definition["enabled"]
            or definition.get("state_pause_reason")
            or row["enqueued_state_version"]
            < (definition.get("state_queue_invalidation_version") or 0),
        )

    def _due(self, row, definition):
        due = row.get("not_before_at")
        if isinstance(due, str):
            due = datetime.fromisoformat(due)
        return (
            self._invalidated(row, definition)
            or due is None
            or due <= self.clock()
        )

    async def _can_reserve(self, tx, permit, current, definition):
        if current["status"] == "accepted" or self._invalidated(
            current,
            definition,
        ):
            return True
        if await self.capacity.available(tx, permit, current) <= 0:
            return False
        # 尚未开始的同任务周期不提前占满模型池，让其他任务仍有机会运行。
        key = tuple(current[name] for name in IDENTITY_FIELDS)
        where = " AND ".join(f"{name}=%s" for name in IDENTITY_FIELDS)
        active = await tx.fetch_one(
            f"SELECT COUNT(*) AS count FROM {RUN_TABLE} WHERE {where} "
            f"AND execution_id<>%s AND {occupied_run_sql()}",
            (*key, current["execution_id"]),
        )
        maximum = json.loads(current["spec_json"])["runtime"][
            "max_concurrency"
        ]
        return active["count"] < maximum

    async def _settle(self, claimed, *, recovery=False):
        async with transaction(self.db) as tx:
            row = await tx.fetch_one(
                f"SELECT * FROM {RUN_TABLE} WHERE execution_id=%s FOR UPDATE",
                (claimed["execution_id"],),
            )
            if row["status"] in TERMINAL_STATUSES or row["status"] in {
                "accepted",
                "running",
            }:
                if row["dispatch_owner"] == claimed["dispatch_owner"]:
                    values = {
                        "dispatch_owner": "",
                        "dispatch_lease_until": (
                            self.clock()
                            + timedelta(seconds=30 if recovery else 5)
                            if row["status"] in {"accepted", "running"}
                            else None
                        ),
                        "version": row["version"] + 1,
                        "updated_at": self.clock(),
                    }
                    await update(
                        tx,
                        RUN_TABLE,
                        values,
                        "execution_id=%s AND version=%s",
                        (row["execution_id"], row["version"]),
                    )
                    return {**row, **values}
                return row
            if row["dispatch_owner"] != claimed["dispatch_owner"]:
                return row
            values = {
                "status": (
                    "dispatch_failed"
                    if row["dispatch_attempt"] >= 3
                    else "queued"
                ),
                "error_code": "cron_dispatch_unconfirmed",
                "dispatch_owner": "",
                "dispatch_lease_until": self.clock()
                + timedelta(
                    seconds=DISPATCH_RETRY_DELAYS_SECONDS[
                        min(row["dispatch_attempt"] - 1, 4)
                    ],
                ),
                "version": row["version"] + 1,
                "updated_at": self.clock(),
            }
            await update(
                tx,
                RUN_TABLE,
                values,
                "execution_id=%s AND version=%s",
                (row["execution_id"], row["version"]),
            )
            result = {**row, **values}
            await append_run_event(tx, result, "dispatch_unconfirmed")
            return result
