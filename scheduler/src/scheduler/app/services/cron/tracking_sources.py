# -*- coding: utf-8 -*-
"""每个事实按自身时间和完整任务身份过滤，SQL 页有界。"""

from dataclasses import dataclass
from datetime import timezone

from .tracking_common import (
    BEIJING,
    BROADCASTS,
    DEFINITIONS,
    EVENTS,
    HISTORY,
    ITEMS,
    JOURNAL,
    KEYS,
    MEMBERS,
    MODES,
    RECEIPTS,
    RUNS,
    json_field,
    new_protocol,
    where_identity,
)


@dataclass(frozen=True)
class TrackingSource:
    table: str
    rank: int
    fact: str
    sql: str
    params: tuple
    zone: object = timezone.utc


def joins(left, right):
    return " AND ".join(
        f"{left}.{name} COLLATE utf8mb4_bin={right}.{name}" for name in KEYS
    )


FACT_CATEGORIES = {
    "journal": "management",
    "mode": "management",
    "broadcast": "management",
    "broadcast_item": "management",
    "delete": "management",
    "receipt": "scheduling",
    "created_at": "scheduling",
    "accepted_at": "execution",
    "started_at": "execution",
    "finished_at": "execution",
    "settled_at": "execution",
    "history_end": "execution",
    "notification": "notification",
    "read": "notification",
}


def source_matches(source, query):
    categories = (
        {"scheduling", "execution"}
        if source.fact == "event"
        else {FACT_CATEGORIES[source.fact]}
    )
    if query.category != "all" and query.category not in categories:
        return False
    if query.entry == "management":
        return "management" in categories
    return query.entry == "all" or "management" not in categories


class TrackingSources:
    def __init__(self, reader, identity):
        self.reader = reader
        self.identity = identity
        self.key = identity.values()

    def usable(self, table):
        return self.reader.coverage.get(table) != "unavailable"

    async def build(self, query):
        probes = {
            EVENTS: "execution_id,event_id,event_type,event_order,occurred_at",
            RUNS: "execution_id,dispatch_meta_json,queue_order,created_at,accepted_at,started_at,finished_at,settled_at,settled_status",
            JOURNAL: "operation_id,action,payload_json,created_at",
            MEMBERS: "batch_id,initial_execution_id",
        }
        needed = {
            "all": {EVENTS, RUNS, JOURNAL, MEMBERS},
            "management": {JOURNAL},
            "scheduling": {EVENTS, RUNS, MEMBERS},
            "execution": {EVENTS, RUNS},
            "notification": {RUNS},
        }["management" if query.entry == "management" else query.category]
        for table in needed:
            await self.reader.rows(
                table,
                f"SELECT {probes[table]} FROM {table} LIMIT 0",
            )
        sources = [
            self.own(
                EVENTS,
                10,
                "event",
                "occurred_at",
                "event_id",
                seq="event_order",
            ),
            self.own(JOURNAL, 20, "journal", "created_at", "operation_id"),
            self.own(BROADCASTS, 22, "broadcast", "created_at", "task_id"),
            self.broadcast_items(),
            self.receipts(),
        ]
        if self.usable(JOURNAL):
            sources.append(self.mode_items())
        sources.extend(self.fallbacks())
        if self.usable(RUNS):
            sources.extend(self.histories())
        else:
            self.reader.coverage[HISTORY] = "unavailable"
            self.reader.warn("execution_history_association_unavailable")
        sources.append(self.deletion())
        return [
            source
            for source in sources
            if self.usable(source.table) and source_matches(source, query)
        ]

    def own(self, table, rank, fact, stamp, key, *, seq="0"):
        sequence = seq if seq == "0" else "n." + seq
        return TrackingSource(
            table,
            rank,
            fact,
            f"SELECT n.*,n.{stamp} AS _time,CAST(n.{key} AS CHAR) AS _key,{sequence} AS _seq "
            f"FROM {table} n WHERE {where_identity('n')}",
            self.key,
        )

    def mode_items(self):
        return TrackingSource(
            MODES,
            21,
            "mode",
            "SELECT j.*,mi.status AS item_status,mi.error_code AS item_error_code,"
            "mi.sync_operation_id,j.created_at AS _time,"
            "CONCAT(mi.operation_id,':',mi.index_no) AS _key,mi.index_no AS _seq "
            f"FROM {MODES} mi JOIN {JOURNAL} j ON mi.operation_id=j.operation_id "
            f"WHERE {where_identity('mi')} AND j.source_id COLLATE utf8mb4_bin=%s "
            f"AND j.action='dispatch_state' AND NOT ({joins('mi', 'j')})",
            (*self.key, self.identity.source_id),
        )

    def broadcast_items(self):
        return TrackingSource(
            ITEMS,
            23,
            "broadcast_item",
            "SELECT bi.*,bt.actor_id,bi.created_at AS _time,"
            "CONCAT(bi.task_id,':',bi.index_no) AS _key,bi.index_no AS _seq "
            f"FROM {ITEMS} bi JOIN {BROADCASTS} bt ON bi.task_id=bt.task_id "
            f"WHERE {where_identity('bi')} AND bt.source_id COLLATE utf8mb4_bin=%s",
            (*self.key, self.identity.source_id),
        )

    def receipts(self):
        conditions = [where_identity("t")]
        params = list(self.key)
        if self.usable(RUNS):
            conditions.append(
                f"EXISTS (SELECT 1 FROM {RUNS} r WHERE {where_identity('r')} "
                f"AND {new_protocol('r')} AND {json_field('r.dispatch_meta_json', 'trigger_receipt_id')}=t.receipt_id)",
            )
            params.extend(self.key)
        if self.usable(MEMBERS):
            conditions.append(
                f"EXISTS (SELECT 1 FROM {MEMBERS} b WHERE {where_identity('b')} AND b.batch_id=t.batch_id)",
            )
            params.extend(self.key)
        return TrackingSource(
            RECEIPTS,
            30,
            "receipt",
            "SELECT "
            + ",".join(
                "t." + name
                for name in (
                    *KEYS,
                    "receipt_id",
                    "external_job_id",
                    "operation_id",
                    "kind",
                    "trigger_id",
                    "logical_fire_at",
                    "physical_fire_at",
                    "batch_id",
                    "status",
                    "response_json",
                )
            )
            + ",t.created_at AS _time,t.receipt_id AS _key,0 AS _seq "
            f"FROM {RECEIPTS} t WHERE t.source_id COLLATE utf8mb4_bin=%s AND ("
            + " OR ".join(conditions)
            + ")",
            (self.identity.source_id, *params),
        )

    def absent_events(self, events):
        if not self.usable(EVENTS):
            return ""
        return (
            f" AND NOT EXISTS (SELECT 1 FROM {EVENTS} e WHERE e.execution_id=r.execution_id "
            f"AND {joins('e', 'r')} AND e.occurred_at IS NOT NULL AND e.event_type IN ("
            + ",".join("'" + event + "'" for event in events)
            + "))"
        )

    def fallbacks(self):
        fields = (
            ("created_at", ("queued",), ""),
            ("accepted_at", ("accepted",), ""),
            ("started_at", ("running",), ""),
            (
                "finished_at",
                ("success", "error", "skipped", "cancelled"),
                " AND r.status IN ('success','error','skipped','cancelled')",
            ),
            ("settled_at", ("settled",), " AND r.settled_status IS NOT NULL"),
        )
        return [
            TrackingSource(
                RUNS,
                40 + index,
                field,
                f"SELECT r.execution_id,r.kind,r.status,r.settled_status,r.settled_at,r.{field} AS _time,r.execution_id AS _key,r.queue_order AS _seq "
                f"FROM {RUNS} r WHERE {where_identity('r')} AND {new_protocol('r')} "
                f"AND r.{field} IS NOT NULL"
                + extra
                + self.absent_events(events),
                self.key,
            )
            for index, (field, events, extra) in enumerate(fields)
        ]

    def histories(self):
        common = (
            "SELECT h.id,r.execution_id AS linked_execution_id,"
            "{stamp} AS _time,CAST(h.id AS CHAR) AS _key,h.id AS _seq "
            f"FROM {HISTORY} h JOIN {RUNS} r ON r.history_execution_id=h.id "
            f"WHERE {where_identity('r')} AND {new_protocol('r')} AND {{condition}}"
        )
        facts = (
            (
                "history_end",
                "h.end_time",
                "r.finished_at IS NULL"
                + self.absent_events(
                    ("success", "error", "skipped", "cancelled"),
                ),
            ),
            (
                "notification",
                "h.notification_sent_at",
                "h.notification_status='sent'",
            ),
            ("read", "h.read_at", "h.is_read=1"),
        )
        return [
            TrackingSource(
                HISTORY,
                60 + index,
                fact,
                common.replace("{stamp}", stamp).replace(
                    "{condition}",
                    condition,
                )
                + f" AND {stamp} IS NOT NULL",
                self.key,
                BEIJING,
            )
            for index, (fact, stamp, condition) in enumerate(facts)
        ]

    def deletion(self):
        absent = ""
        if self.usable(JOURNAL):
            absent = f" AND NOT EXISTS (SELECT 1 FROM {JOURNAL} j WHERE {joins('j', 'd')} AND j.action='delete')"
        return TrackingSource(
            DEFINITIONS,
            50,
            "delete",
            "SELECT d.*,d.deleted_at AS _time,d.job_id AS _key,0 AS _seq "
            f"FROM {DEFINITIONS} d WHERE {where_identity('d')} AND d.deleted_at IS NOT NULL"
            + absent,
            self.key,
        )
