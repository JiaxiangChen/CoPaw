# -*- coding: utf-8 -*-
"""只遍历已登记工作，按模型池轮转，不能从任务定义生成新触发。"""

from .executions import RUN_TABLE
from .operations import IDENTITY_FIELDS, utc_now
from .run_accounting import occupied_run_sql

SCOPE_FIELDS = ("source_id", "provider_id", "model_id")
INVALIDATED = (
    "(d.deleted_at IS NOT NULL OR d.enabled=0 OR s.pause_reason IS NOT NULL "
    "OR r.enqueued_state_version<s.queue_invalidation_version)"
)


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
        params.append(now)
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
                else " ORDER BY r.dispatch_order,r.queue_order LIMIT %s"
            ),
            (*params, self.limit),
        )
