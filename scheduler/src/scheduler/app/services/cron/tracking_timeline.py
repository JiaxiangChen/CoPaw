# -*- coding: utf-8 -*-
"""多来源有界 keyset 归并；不写业务表、不调用执行或标读接口。"""

from builtins import anext
from datetime import datetime, timezone

from .tracking_common import (
    MEMBERS,
    RUNS,
    TrackingReader,
    json_field,
    json_object,
    new_protocol,
    same_identity,
    select_fields,
    iso_time,
    where_identity,
)
from .tracking_cursor import (
    boundary_clause,
    decode_cursor,
    encode_cursor,
    fingerprint,
    sql_time,
)
from .tracking_execution import EVENT_MAP, TrackingExecution
from .tracking_lookup import TrackingLookup
from .tracking_management import auxiliary_node, journal_node
from .tracking_nodes import (
    finish_node,
    make_node,
    manifest_member,
    recorded_reason,
    sort_key,
)
from .tracking_notifications import history_node
from .tracking_sources import TrackingSources


class TrackingTimeline:
    def __init__(self, db, source, *, clock=None):
        self.db = db
        self.source = source
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def timeline(self, job_id, tenant_id, agent_id, query):
        reader = TrackingReader(self.db)
        lookup = TrackingLookup(self.db, self.source, reader=reader)
        identity, _ = await lookup.resolve(job_id, tenant_id, agent_id)
        expected = fingerprint(identity, query)
        boundary, upper = None, min(query.end_time, self.clock())
        if query.cursor:
            boundary, upper = decode_cursor(query.cursor, expected)
            if upper > query.end_time or upper > self.clock():
                raise ValueError("invalid_cron_tracking_cursor")
        execution = TrackingExecution(reader, identity)
        sources = await TrackingSources(reader, identity).build(query)
        heads = await self._heads(sources, query, upper, boundary, execution)
        selected = await self._merge(heads, query)
        has_more = len(selected) > query.limit
        selected = selected[: query.limit]
        cursor = (
            encode_cursor(selected[-1][0], upper, expected)
            if has_more
            else None
        )
        return {
            "identity": identity.public(),
            "items": [node for _, node in selected],
            "has_more": has_more,
            "next_cursor": cursor,
            "loaded_count": len(selected),
            "query_upper": iso_time(upper),
            "consistency": "best_effort_current_records",
            **reader.metadata(),
        }

    async def _heads(self, sources, query, upper, boundary, execution):
        heads = {}
        for source in sources:
            stream = self._stream(source, query, upper, boundary, execution)
            item = await anext(stream, None)
            if item is not None:
                heads[source.rank] = (item, stream)
        return heads

    @staticmethod
    async def _merge(heads, query):
        selected = []
        choose = max if query.order == "desc" else min
        while heads and len(selected) <= query.limit:
            rank = choose(heads, key=lambda name: heads[name][0][0])
            item, stream = heads[rank]
            selected.append(item)
            following = await anext(stream, None)
            if following is None:
                del heads[rank]
            else:
                heads[rank] = following, stream
        return selected

    async def _stream(self, source, query, upper, boundary, execution):
        if upper <= query.start_time:
            return
        page_size = 100
        while True:
            rows = await self._page(
                source,
                query,
                upper,
                boundary,
                execution.reader,
                page_size,
            )
            for row in rows:
                boundary = sort_key(row, source)
                node = await self._map(row, source, execution, query)
                if self._matches(node, query):
                    yield boundary, finish_node(node)
            if len(rows) < page_size:
                return

    @staticmethod
    async def _page(source, query, upper, boundary, reader, size):
        descending = query.order == "desc"
        after, after_params = boundary_clause(boundary, source, descending)
        event_filter, event_params = event_category_filter(source, query)
        direction = "DESC" if descending else "ASC"
        sql = (
            f"SELECT * FROM ({source.sql}) tracking WHERE _time>=%s AND _time<%s"
            + event_filter
            + after
            + f" ORDER BY _time {direction},_seq {direction},"
            f"_key COLLATE utf8mb4_bin {direction} LIMIT %s"
        )
        params = (
            *source.params,
            sql_time(query.start_time, source.zone),
            sql_time(upper, source.zone),
            *event_params,
            *after_params,
            size,
        )
        return await reader.rows(source.table, sql, params)

    @staticmethod
    def _matches(node, query):
        return (
            node is not None
            and query.category in ("all", node["category"])
            and query.entry in ("all", node["entry"])
        )

    async def _map(self, row, source, execution, query):
        if source.fact == "event":
            return await execution.event_node(
                row,
                source,
                entry_filter=query.entry,
            )
        if source.fact in {
            "created_at",
            "accepted_at",
            "started_at",
            "finished_at",
            "settled_at",
        }:
            return await execution.fallback_node(
                row,
                source,
                entry_filter=query.entry,
            )
        if source.fact == "journal":
            return journal_node(row, source)
        if source.fact == "receipt":
            if query.entry not in {"all", row.get("kind")}:
                return None
            return await self._receipt(row, source, execution)
        if source.fact in {"notification", "read", "history_end"}:
            return await history_node(row, source, execution)
        return auxiliary_node(row, source)

    async def _receipt(self, row, source, execution):
        direct = same_identity(row, execution.identity)
        if not direct and not await self._participated(row, execution):
            return None
        skipped = row.get("status") == "skipped"
        batch = row.get("kind") == "batch"
        kind = "T03" if skipped else "T02" if batch else "T01"
        title = (
            "触发被跳过"
            if skipped
            else "批调度受理涉及本任务" if batch else "收到普通调度请求并受理"
        )
        node = make_node(
            row,
            kind,
            title,
            "scheduling",
            source,
            basis="receipt_received",
        )
        node["entry"] = "batch" if batch else "ordinary"
        node["recorded_outcome"] = row.get("status")
        response = json_object(row.get("response_json"))
        node["details"] = {
            **select_fields(
                row,
                "external_job_id operation_id trigger_id batch_id kind",
            ),
            "logical_fire_at": iso_time(row.get("logical_fire_at")),
            "physical_fire_at": iso_time(row.get("physical_fire_at")),
            "recorded_time_origin": "unrecorded",
            "association": (
                "direct_callback" if direct else "frozen_batch_membership"
            ),
        }
        recorded_reason(node, response.get("reason"))
        return node

    @staticmethod
    async def _participated(receipt, execution):
        if receipt.get("kind") != "batch":
            return False
        receipt = await execution.receipt(receipt["receipt_id"])
        if receipt is None:
            return False
        identity = execution.identity
        rows = await execution.reader.rows(
            RUNS,
            f"SELECT * FROM {RUNS} WHERE {where_identity()} AND {new_protocol()} "
            f"AND {json_field('dispatch_meta_json', 'trigger_receipt_id')}=%s LIMIT 2",
            (*identity.values(), receipt["receipt_id"]),
        )
        if any(
            manifest_member(receipt, identity, row["execution_id"])
            for row in rows
        ):
            return True
        members = await execution.reader.rows(
            MEMBERS,
            f"SELECT initial_execution_id FROM {MEMBERS} WHERE {where_identity()} AND batch_id=%s LIMIT 2",
            (*identity.values(), receipt.get("batch_id")),
        )
        for member in members:
            initial = member["initial_execution_id"]
            if manifest_member(
                receipt,
                identity,
                initial,
            ) and await execution.run(initial):
                return True
        return False


def event_category_filter(source, query):
    if source.fact != "event" or query.category == "all":
        return "", ()
    scheduled = tuple(
        name
        for name, (_, _, category) in EVENT_MAP.items()
        if category == "scheduling"
    )
    names = (
        scheduled
        if query.category == "scheduling"
        else (*scheduled, "retry_request")
    )
    placeholders = ",".join("%s" for _ in names)
    if query.category == "scheduling":
        return f" AND event_type IN ({placeholders})", names
    return (
        f" AND (event_type IS NULL OR event_type NOT IN ({placeholders}))",
        names,
    )
