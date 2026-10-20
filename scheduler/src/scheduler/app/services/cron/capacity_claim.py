# -*- coding: utf-8 -*-
"""Capacity reservation on the existing model-scope lease row."""

from typing import Mapping
from datetime import datetime
from .managed.run_accounting import occupied_run_sql

# 仅由新版后台在完整表结构校验后启用，不是新增运维配置。
# 旧部署没有新表时沿用原统计，不探测或自动创建业务表。
MANAGED_CAPACITY_ACCOUNTING = False


def enable_managed_capacity_accounting():
    """新运行时已就绪后，将其持久在途占位纳入共用模型池。"""
    global MANAGED_CAPACITY_ACCOUNTING
    MANAGED_CAPACITY_ACCOUNTING = True


async def _managed_occupants(cur, scope, exclude_execution_id=None):
    await cur.execute(
        "SELECT COUNT(*) AS count FROM swe_cron_execution_runs "
        "WHERE source_id=%s AND provider_id=%s AND model_id=%s "
        f"AND {occupied_run_sql()} "
        "AND execution_id<>%s "
        "AND JSON_UNQUOTE(JSON_EXTRACT(dispatch_meta_json,'$.protocol'))="
        "'scheduler_managed_v1'",
        (*scope, exclude_execution_id or ""),
    )
    rows = await cur.fetchall()
    return int(row_values(rows[0], ("count",))[0])


def row_values(row, names):
    return [row[name] for name in names] if isinstance(row, Mapping) else row


async def available_capacity(
    cur,
    scope,
    owner,
    now,
    policy,
    *,
    exclude_execution_id=None,
):
    await cur.execute(
        "SELECT lock_owner, lease_expires_at "
        "FROM swe_cron_dispatch_scope_leases "
        "WHERE source_id=%s AND provider_id=%s AND model_id=%s FOR UPDATE",
        scope,
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    lock_owner, expires = row_values(
        rows[0],
        ("lock_owner", "lease_expires_at"),
    )
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if lock_owner != owner or not expires or expires < now:
        return 0
    await cur.execute(
        "SELECT effective_workers FROM swe_cron_dispatch_worker_capacity "
        "WHERE source_id=%s AND provider_id=%s AND model_id=%s "
        "AND strategy_id=%s "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (*scope, policy["strategy_id"]),
    )
    rows = await cur.fetchall()
    capacity = (
        int(row_values(rows[0], ("effective_workers",))[0])
        if rows
        else policy["effective_workers"]
    )
    capacity = max(policy["min_workers"], min(policy["max_workers"], capacity))
    # First non-locking reads occur after the scope lock. Competing claimers
    # cannot add occupants until this transaction commits. Do not lock intents
    # before batches: manual retry holds batch -> intent in that order.
    await cur.execute(
        "SELECT id FROM swe_cron_dispatch_intents "
        "WHERE COALESCE(source_id,'')=%s "
        "AND COALESCE(NULLIF(provider_id,''),'default')=%s "
        "AND COALESCE(NULLIF(model_id,''),'default')=%s "
        "AND status IN ('claimed','acknowledged','dispatched')",
        scope,
    )
    legacy = len(await cur.fetchall())
    # 与旧领取事务共用同一 scope 行锁；统计失败必须阻止领取，不能当作零占用。
    managed = (
        await _managed_occupants(cur, scope, exclude_execution_id)
        if MANAGED_CAPACITY_ACCOUNTING
        else 0
    )
    return max(0, capacity - legacy - managed)


async def write_capacity(db, sql, params):
    """Publish a capacity decision under the same lock used by claimers."""
    owner, source, provider, model = params[:4]
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT lock_owner, lease_expires_at "
                    "FROM swe_cron_dispatch_scope_leases "
                    "WHERE source_id=%s AND provider_id=%s AND model_id=%s "
                    "FOR UPDATE",
                    (source, provider, model),
                )
                rows = await cur.fetchall()
                lease = (
                    row_values(rows[0], ("lock_owner", "lease_expires_at"))
                    if rows
                    else (None, None)
                )
                if lease[0] != owner or not lease[1] or lease[1] < params[-1]:
                    raise RuntimeError("Capacity scope lease lost")
                await cur.execute(sql, params)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
