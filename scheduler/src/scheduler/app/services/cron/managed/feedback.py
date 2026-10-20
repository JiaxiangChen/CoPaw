# -*- coding: utf-8 -*-
"""向唯一容量调整器提供新协议的已确认运行反馈，不重跑业务。"""

from datetime import timezone

from .executions import RUN_TABLE
from .operations import IDENTITY_FIELDS
from .settlement import ExecutionSettlement, PROTOCOL
from .run_accounting import occupied_run_sql

METRICS = (
    "pending_count",
    "claimed_count",
    "running_count",
    "success_count",
    "failure_count",
)


def _utc_text(value):
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(" ")


def _scope_filter(scope):
    if scope is None:
        return "", ()
    names = ("source_id", "provider_id", "model_id")
    return (
        " AND " + " AND ".join(f"r.{name}=%s" for name in names),
        tuple(scope[name] for name in names),
    )


class ManagedCapacityFeedback:
    def __init__(self, db):
        self.db = db
        self.settlement = ExecutionSettlement(db)

    async def tick(self):
        return await self.settlement.tick()

    async def scopes(self, *, now_utc, source_ids=None):
        joins = " AND ".join(
            f"r.{name}=d.{name} COLLATE utf8mb4_bin"
            for name in IDENTITY_FIELDS
        )
        source_sql, params = "", [_utc_text(now_utc)]
        if source_ids:
            source_sql = (
                " AND r.source_id IN ("
                + ",".join("%s" for _ in source_ids)
                + ")"
            )
            params.extend(source_ids)
        return await self.db.fetch_all(
            "SELECT DISTINCT r.source_id,r.provider_id,r.model_id "
            f"FROM {RUN_TABLE} r JOIN swe_cron_jobs_v2 d ON {joins} "
            f"WHERE d.management_owner='scheduler' AND {PROTOCOL} "
            "AND ((r.status IN ('queued','dispatch_failed') "
            "AND (r.not_before_at IS NULL OR r.not_before_at<=%s)) "
            f"OR {occupied_run_sql('r')}) "
            + source_sql
            + " ORDER BY r.source_id,r.provider_id,r.model_id",
            params,
        )

    async def summarize(self, *, since, now_utc, scope=None):
        clause, params = _scope_filter(scope)
        start, now = _utc_text(since), _utc_text(now_utc)
        row = await self.db.fetch_one(
            "SELECT SUM(CASE WHEN r.status IN ('queued','dispatch_failed') "
            "AND (r.not_before_at IS NULL OR r.not_before_at<=%s) THEN 1 ELSE 0 END) AS pending_count, "
            "SUM(CASE WHEN r.status='dispatching' THEN 1 ELSE 0 END) AS claimed_count, "
            "SUM(CASE WHEN r.status IN ('accepted','running') OR "
            "(r.status='success' AND r.settled_status IS NULL) THEN 1 ELSE 0 END) AS running_count, "
            "SUM(CASE WHEN r.settled_status='success' AND r.settled_at>=%s "
            "AND r.settled_at<=%s THEN 1 ELSE 0 END) AS success_count, "
            "SUM(CASE WHEN r.settled_status IN ('error','cancelled') AND r.settled_at>=%s "
            "AND r.settled_at<=%s THEN 1 ELSE 0 END) AS failure_count "
            f"FROM {RUN_TABLE} r WHERE {PROTOCOL} "
            f"AND (r.settled_status IS NULL OR r.settled_at>=%s){clause}",
            (now, start, now, start, now, start, *params),
        )
        # 跳过的未开始工作、未知 HTTP 回执不伪装成模型执行失败。
        return {
            **{name: int((row or {}).get(name) or 0) for name in METRICS},
            "latency_p95_ms": 0,
        }
