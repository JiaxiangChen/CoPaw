# -*- coding: utf-8 -*-
"""复用既有模型池策略和 scope 租约，在领取事务中原子检查新旧占位。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import capacity_claim
from ..scheduling_service import WorkerScope
from .operations import utc_now


class TransactionCursor:
    """适配已有容量查询，保证所有读取仍使用调用方同一个事务连接。"""

    def __init__(self, tx):
        self.tx = tx
        self.rows = []

    async def execute(self, query, params):
        self.rows = await self.tx.fetch_all(query, params)

    async def fetchall(self):
        return self.rows


@dataclass(frozen=True)
class CapacityPermit:
    scope: tuple
    owner: str
    policy: dict


class ManagedCapacityGate:
    def __init__(self, legacy_runtime, *, clock=None):
        # 与同进程旧调度循环共用真实 worker 身份及已有策略，不另建并发配置。
        self.runtime = legacy_runtime
        self.clock = clock or utc_now

    def _now(self):
        return self.clock().replace(tzinfo=timezone.utc)

    def _beijing_now(self):
        return (
            self._now()
            .astimezone(timezone(timedelta(hours=8)))
            .replace(tzinfo=None)
        )

    async def prepare(self, row):
        if not capacity_claim.MANAGED_CAPACITY_ACCOUNTING:
            raise RuntimeError("cron_managed_capacity_not_enabled")
        scope = WorkerScope(
            source_id=row["source_id"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
        )
        strategy = await self.runtime._resolve_worker_strategy(
            scope,
            self._now(),
        )
        if not await self.runtime._acquire_scope_lease(
            scope,
            strategy,
            self._now(),
        ):
            return None
        effective = await self.runtime._effective_workers_for_scope(
            scope,
            strategy,
            self._now(),
        )
        return CapacityPermit(
            (scope.source_id, scope.provider_id, scope.model_id),
            self.runtime._worker_id,
            {
                "strategy_id": strategy.strategy_id,
                "effective_workers": effective,
                "min_workers": strategy.min_workers,
                "max_workers": strategy.max_workers,
            },
        )

    async def lock(self, tx, permit):
        # 必须在定义/执行的非锁定读取之前锁 scope，避免可重复读使用过早快照。
        if permit.owner != self.runtime._worker_id:
            return False
        lease = await tx.fetch_one(
            "SELECT lock_owner, lease_expires_at FROM swe_cron_dispatch_scope_leases "
            "WHERE source_id=%s AND provider_id=%s AND model_id=%s FOR UPDATE",
            permit.scope,
        )
        if lease is None or lease["lock_owner"] != permit.owner:
            return False
        expires = lease["lease_expires_at"]
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        return expires is not None and expires > self._beijing_now()

    async def available(self, tx, permit, row):
        return await capacity_claim.available_capacity(
            TransactionCursor(tx),
            permit.scope,
            permit.owner,
            self._beijing_now(),
            permit.policy,
            exclude_execution_id=row["execution_id"],
        )
