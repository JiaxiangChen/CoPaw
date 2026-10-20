# -*- coding: utf-8 -*-
"""新看板按来源只读共库；概览在完整过滤范围聚合，不以当前页代替总量。"""

import json

from . import managed_board_sql as sql
from ...models.managed_board import BoardDetailQuery


class ManagedBoardUnavailable(RuntimeError):
    """关系或表不可用时不能返回假空数据。"""


def batch_item(row):
    if row["status"] == "unavailable":
        raise ManagedBoardUnavailable("cron_batch_relation_incomplete")
    fields = (
        "batch_id",
        "tenant_id",
        "source_id",
        "agent_id",
        "job_id",
        "job_name",
        "member_count",
        "done_count",
        "failed_count",
        "success_count",
        "skipped_count",
        "provider_id",
        "model_id",
        "active_count",
        "waiting_count",
        "status",
    )
    result = {name: row[name] for name in fields}
    result["dispatch_paused"] = bool(row["dispatch_paused"])
    # 新协议没有批次级Owner；dispatch_owner是派发令牌，不能暴露。
    result["lock_owner"] = None
    result.update(
        {
            name: sql.time_text(row[name])
            for name in ("logical_fire_at", "physical_fire_at", "created_at")
        },
    )
    result["completion_rate"] = (
        row["done_count"] / row["member_count"]
        if row["member_count"]
        else None
    )
    return result


def member_item(row):
    spec = json.loads(row["spec_json"])
    meta = json.loads(row["dispatch_meta_json"])
    names = (
        *sql.KEYS,
        "member_id",
        "execution_id",
        "business_attempt",
        "member_role",
        "member_order",
        "status",
        "settled_status",
        "settled_error_code",
        "provider_id",
        "model_id",
        "definition_version",
        "current_definition_version",
        "dispatch_attempt",
        "management_status",
        "dispatch_state",
        "error_code",
        "actor_id",
        "history_execution_id",
    )
    result = {name: row.get(name) for name in names}
    result.update(
        {
            name: sql.time_text(row.get(name))
            for name in (
                "not_before_at",
                "accepted_at",
                "started_at",
                "finished_at",
                "settled_at",
            )
        },
    )
    result.update(
        {
            "job_name": spec.get("name"),
            "bbk_id": spec.get("bbk_id"),
            "dispatch_priority": meta.get("dispatch_priority"),
            "viewer_heat_score": meta.get("viewer_heat_score"),
            "error_message": member_error_message(row),
        },
    )
    return result


def member_error_message(row):
    code = row.get("settled_error_code") or row.get("error_code")
    if code == "cron_async_subtasks_failed":
        code = "子任务执行失败"
    if (
        row.get("status") not in {"error", "dispatch_failed"}
        and row.get(
            "settled_status",
        )
        != "error"
    ):
        return ""
    try:
        meta = json.loads(row.get("result_json") or "{}")
    except (ValueError, TypeError):
        meta = {}
    detail = meta.get("error_detail") if isinstance(meta, dict) else None
    # 只返回现有摘要字段，不展开整个结果或异常链。
    for value in (row.get("history_error_message"), detail, code):
        if isinstance(value, str) and value.strip():
            return value[:2048]
    return ""


class ManagedBoardQuery:
    def __init__(self, db):
        self.db = db

    async def _ready(self):
        await self.db.fetch_all(
            f"SELECT event_id,event_key,event_order,details_json FROM {sql.EVENTS} LIMIT 0",
        )
        await self.db.fetch_all(
            f"SELECT member_id,current_execution_id,business_attempt FROM {sql.MEMBERS} LIMIT 0",
        )

    async def batches(
        self,
        source,
        *,
        start=None,
        end=None,
        status=None,
        query=None,
        page=1,
        page_size=20,
    ):
        await self._ready()
        where, params = sql.batch_where(source, start, end, query)
        grouped = sql.aggregate_query(where)
        state_filter = "WHERE status=%s" if status else ""
        stats_filter = (
            "WHERE status=%s OR status='unavailable'" if status else ""
        )
        bound = (*params, status) if status else params
        stats = await self.db.fetch_one(
            "SELECT COUNT(*) AS total_batches, SUM(member_count) AS total_members, SUM(done_count) AS done_members, SUM(failed_count) AS failed_members, "
            "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_batches, SUM(CASE WHEN status='unavailable' THEN 1 ELSE 0 END) AS unavailable_batches "
            f"FROM ({grouped}) b {stats_filter}",
            bound,
        )
        if stats["unavailable_batches"]:
            raise ManagedBoardUnavailable("cron_batch_relation_incomplete")
        rows = await self.db.fetch_all(
            f"SELECT * FROM ({grouped}) b {state_filter} ORDER BY logical_fire_at DESC,batch_id DESC LIMIT %s OFFSET %s",
            (*bound, page_size, (page - 1) * page_size),
        )
        return {
            "items": [batch_item(row) for row in rows],
            "total": int(stats["total_batches"]),
            "stats": {
                name: int(value or 0)
                for name, value in stats.items()
                if name != "unavailable_batches"
            },
            "page": page,
            "page_size": page_size,
        }

    async def detail(
        self,
        source,
        batch_id,
        options: BoardDetailQuery | None = None,
    ):
        query = options or BoardDetailQuery()
        await self._ready()
        where, params = sql.batch_where(source, batch_id=batch_id)
        row = await self.db.fetch_one(sql.aggregate_query(where), params)
        if row is None:
            raise LookupError("cron_batch_not_found")
        batch = batch_item(row)
        members, total = await self._members(
            batch_id,
            query.member_page,
            query.member_limit,
            query.member_status,
            query.member_role,
            query.member_query,
        )
        events, event_total = await self._events(
            source,
            batch_id,
            query.event_page,
            query.event_limit,
            query.event_type,
        )
        return {
            "batch": batch,
            "members": members,
            "member_total": total,
            "member_page": query.member_page,
            "member_page_size": query.member_limit,
            "events": events,
            "event_total": event_total,
            "event_page": query.event_page,
            "event_page_size": query.event_limit,
        }

    async def _members(self, batch_id, page, limit, status, role, query):
        clauses, params = ["m.batch_id=%s"], [batch_id]
        for name, value in (("r.status", status), ("m.member_role", role)):
            if value:
                clauses.append(f"{name}=%s")
                params.append(value)
        if query:
            clauses.append(
                "(m.tenant_id LIKE %s ESCAPE '!' OR m.job_id LIKE %s ESCAPE '!' OR JSON_UNQUOTE(JSON_EXTRACT(r.spec_json,'$.name')) LIKE %s ESCAPE '!')",
            )
            params.extend([sql.like_text(query)] * 3)
        where = " AND ".join(clauses)
        count = await self.db.fetch_one(
            f"SELECT COUNT(*) AS total {sql.MEMBER_FROM} WHERE {where}",
            tuple(params),
        )
        rows = await self.db.fetch_all(
            "SELECT r.*,h.error_message AS history_error_message,m.member_id,m.business_attempt,m.member_role,m.dispatch_order AS member_order,d.definition_version AS current_definition_version,s.management_status,s.dispatch_state "
            + sql.MEMBER_FROM
            + "LEFT JOIN swe_cron_executions h ON h.id=r.history_execution_id AND h.tenant_id=r.tenant_id AND h.tenant_id COLLATE utf8mb4_bin=r.tenant_id AND h.job_id=r.job_id AND h.job_id COLLATE utf8mb4_bin=r.job_id "
            + f" WHERE {where} ORDER BY m.dispatch_order,m.member_id LIMIT %s OFFSET %s",
            (*params, limit, (page - 1) * limit),
        )
        return [member_item(row) for row in rows], int(count["total"])

    async def _events(self, source, batch_id, page, limit, event_type):
        # 普通入口先受理的首轮运行也属于批成员，不靠运行 kind 或元数据漏掉事实。
        selection, params = sql.event_selection(source, batch_id)
        joined = f"FROM ({selection}) selected JOIN {sql.EVENTS} e ON e.event_id=selected.event_id"
        where = "1=1"
        if event_type:
            where += " AND e.event_type=%s"
            params += (event_type,)
        count = await self.db.fetch_one(
            f"SELECT COUNT(*) AS total {joined} WHERE {where}",
            params,
        )
        rows = await self.db.fetch_all(
            f"SELECT e.* {joined} WHERE {where} ORDER BY e.event_order DESC LIMIT %s OFFSET %s",
            (*params, limit, (page - 1) * limit),
        )
        items = []
        for row in rows:
            # 请求审计内保存的完整幂等响应不需要暴露到看板。
            detail = json.loads(row["details_json"])
            if row["event_type"] == "retry_request":
                response = detail.get("response", {})
                detail = {
                    name: response.get(name) for name in ("queued", "skipped")
                }
            items.append(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "execution_id": row["execution_id"],
                    "member_id": row["member_id"],
                    "actor_id": row["actor_id"],
                    "occurred_at": sql.time_text(row["occurred_at"]),
                    "details": detail,
                },
            )
        return items, int(count["total"])
