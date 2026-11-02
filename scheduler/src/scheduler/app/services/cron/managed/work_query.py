# -*- coding: utf-8 -*-
"""只遍历已登记工作，按模型池轮转，不能从任务定义生成新触发。"""

from .executions import RUN_TABLE
from .operations import IDENTITY_FIELDS, utc_now
from .run_accounting import occupied_run_sql
from .date_policy import BEIJING
from datetime import datetime

SCOPE_FIELDS = ("source_id", "provider_id", "model_id")
INVALIDATED = (
    "(d.deleted_at IS NOT NULL OR d.enabled=0 OR s.pause_reason IS NOT NULL "
    "OR r.enqueued_state_version<s.queue_invalidation_version "
    "OR ((d.run_start_date IS NOT NULL OR d.run_end_date IS NOT NULL) AND "
    "(d.run_start_date IS NULL OR d.run_end_date IS NULL OR d.run_start_date>%s OR d.run_end_date<%s)))"
)


def _batch_order():
    """新工作使用冻结元数据，存量工作仍由真实回执和成员关系确定顺序。"""
    received = "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.batch_created_at')),(SELECT MIN(t.created_at) FROM swe_cron_managed_batch_members m JOIN swe_cron_managed_trigger_receipts t ON t.batch_id=m.batch_id AND t.source_id=m.source_id WHERE m.current_execution_id=r.execution_id AND m.source_id=r.source_id AND t.kind='batch' AND t.status='accepted'),'1000-01-01')"
    first = "COALESCE(CAST(JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.batch_queue_order')) AS UNSIGNED),(SELECT MIN(i.queue_order) FROM swe_cron_managed_batch_members m JOIN swe_cron_managed_batch_members b ON b.batch_id=m.batch_id JOIN swe_cron_execution_runs i ON i.execution_id=b.initial_execution_id WHERE m.current_execution_id=r.execution_id AND m.source_id=r.source_id),r.queue_order)"
    return f" ORDER BY CASE WHEN r.dispatch_lane='urgent' THEN 0 ELSE 1 END,{received},{first},r.dispatch_order,r.queue_order LIMIT %s"


def _identity_match(left, right):
    return " AND ".join(
        f"{left}.{key}={right}.{key} AND {left}.{key} COLLATE utf8mb4_bin={right}.{key}"
        for key in IDENTITY_FIELDS
    )


def _base(running):
    status = (
        "('running')"
        if running
        else "('queued','dispatching','accepted','dispatch_failed')"
    )
    sql = (
        f" FROM {RUN_TABLE} r JOIN swe_cron_jobs_v2 d ON {_identity_match('r', 'd')} "
        f"JOIN swe_cron_job_states s ON {_identity_match('r', 's')} "
        f"WHERE d.management_owner='scheduler' AND r.status IN {status} "
        "AND JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.protocol'))='scheduler_managed_v1' "
        "AND (r.dispatch_lease_until IS NULL OR r.dispatch_lease_until<=%s)"
    )
    now = utc_now().isoformat(" ")
    params = [now]
    if not running:
        sql += (
            f" AND (r.not_before_at<=%s OR r.not_before_at IS NULL OR {INVALIDATED}) "
            f"AND (r.status='accepted' OR {INVALIDATED} OR "
            f"(SELECT COUNT(*) FROM {RUN_TABLE} a WHERE {_identity_match('a', 'r')} "
            f"AND a.execution_id<>r.execution_id AND {occupied_run_sql('a')}) "
            "< CAST(JSON_UNQUOTE(JSON_EXTRACT(r.spec_json,'$.runtime.max_concurrency')) AS UNSIGNED))"
        )
        today = datetime.now(BEIJING).date().isoformat()
        params.extend((now, today, today, today, today))
    return sql, params


class ManagedWorkQuery:
    def __init__(self, db, limit):
        self.db = db
        self.limit = limit
        # 仅是本进程轮转位置，不存任务状态；重启从头仍能从共库找回全部工作。
        self.scope_after = None

    async def scopes(self):
        rows = await self._scope_page(self.scope_after)
        if not rows and self.scope_after is not None:
            rows = await self._scope_page(None)
        self.scope_after = (
            tuple(rows[-1][name] for name in SCOPE_FIELDS) if rows else None
        )
        return rows

    async def _scope_page(self, after):
        sql, params = _base(False)
        fields = ",".join(f"r.{name}" for name in SCOPE_FIELDS)
        if after is not None:
            sql += f" AND ({fields})>(%s,%s,%s)"
            params.extend(after)
        return await self.db.fetch_all(
            f"SELECT DISTINCT {fields}{sql} ORDER BY {fields} LIMIT %s",
            (*params, self.limit),
        )

    async def rows(self, *, running, scope=None):
        sql, params = _base(running)
        if scope is not None:
            sql += " AND " + " AND ".join(
                f"r.{name}=%s" for name in SCOPE_FIELDS
            )
            params.extend(scope[name] for name in SCOPE_FIELDS)
        return await self.db.fetch_all(
            "SELECT r.execution_id"
            + sql
            + (
                " ORDER BY r.updated_at,r.queue_order LIMIT %s"
                if running
                else _batch_order()
            ),
            (*params, self.limit),
        )
