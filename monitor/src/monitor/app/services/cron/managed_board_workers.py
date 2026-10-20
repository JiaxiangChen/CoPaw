# -*- coding: utf-8 -*-
"""复用共用模型池只读能力，明确旧容量时间的北京时间语义。"""

from .managed_board_sql import time_text
from .query_service import QueryService


async def shared_workers(db, source, start=None, end=None, cursor=None):
    reader = QueryService()
    policies = (
        []
        if cursor
        else await reader._fetch_dispatch_worker_policy_rows(db, source)
    )
    current = (
        []
        if cursor
        else await reader._fetch_current_dispatch_capacity_rows(db, source)
    )
    events, next_cursor = await reader._fetch_dispatch_capacity_event_rows(
        db,
        source_id=source,
        start_time=start,
        end_time=end,
        capacity_cursor=cursor,
    )

    def capacity(row):
        value = reader._map_dispatch_capacity(row).model_dump()
        value["created_at"] = time_text(row.get("created_at"), local=True)
        return value

    mapped_policies = []
    for row in policies:
        if row["source_id"] != source:
            continue
        item = reader._map_dispatch_policy(row).model_dump()
        for field in ("created_at", "updated_at"):
            item[field] = time_text(row.get(field), local=True)
        mapped_policies.append(item)
    return {
        "source_id": source,
        "shared_pool": True,
        "policies": mapped_policies,
        "current_capacity": [
            capacity(row) for row in current if row["source_id"] == source
        ],
        "capacity_events": [
            capacity(row) for row in events if row["source_id"] == source
        ],
        "capacity_events_next_cursor": next_cursor,
    }
