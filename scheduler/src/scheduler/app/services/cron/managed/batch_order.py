# -*- coding: utf-8 -*-
"""在本次受理事务中固定既有优先级/热度算法，不重排已入队工作。"""

import json
from datetime import timedelta, timezone

from ..dispatch_intent_service import (
    VIEWER_FAST_READ_BUCKET_SECONDS,
    VIEWER_HEAT_LOOKBACK_DAYS,
    _viewer_fast_read_select_clause,
    _viewer_heat_score_from_row,
    compute_batch_dispatch_order,
)
from .priority import priority_snapshot


async def _heat_scores(db, selected, now):
    if not selected:
        return {}
    # 候选已按源子关系鉴权，再以任务/租户对限定原历史，避免裸 ID 扩大范围。
    pairs = [
        part for row in selected for part in (row["job_id"], row["tenant_id"])
    ]
    marks = ",".join("(%s,%s)" for _ in selected)
    since = now.astimezone(timezone(timedelta(hours=8))).replace(
        tzinfo=None,
    ) - timedelta(days=VIEWER_HEAT_LOOKBACK_DAYS)
    rows = await db.fetch_all(
        f"SELECT e.job_id, COUNT(*) AS read_count, {_viewer_fast_read_select_clause()} "
        f"FROM swe_cron_executions e WHERE (e.job_id,e.tenant_id) IN ({marks}) "
        "AND e.status='success' AND e.async_status='success' AND e.is_read=TRUE "
        "AND e.read_at IS NOT NULL AND e.read_at>=%s GROUP BY e.job_id",
        (*VIEWER_FAST_READ_BUCKET_SECONDS, *pairs, since.isoformat(" ")),
    )
    return {
        row["job_id"]: float(_viewer_heat_score_from_row(row)) for row in rows
    }


async def ordered_members(db, root, selected, logical, now, kind):
    policy = (
        json.loads(root["spec_json"])
        .get("meta", {})
        .get("batch_dispatch_priority", {})
    )
    heat = await _heat_scores(db, selected, now) if kind == "batch" else {}
    entries = []
    for row in selected:
        profile = json.loads(row["spec_json"])
        entries.append(
            {
                "job_id": row["job_id"],
                "tenant_id": row["tenant_id"],
                "definition": row,
                "payload": {
                    "dispatch_priority": priority_snapshot(
                        policy,
                        row["tenant_id"],
                        profile.get("bbk_id") or "",
                    ),
                },
                "viewer_heat_score": heat.get(row["job_id"], 0),
                # 批入口已经提前触发；整批立即就绪，不按成员散列二次等待。
                "due_at": (
                    now
                    if kind == "batch"
                    else logical
                    - timedelta(minutes=int(row["offset_minutes"] or 0))
                ),
            },
        )
    return compute_batch_dispatch_order(entries)
