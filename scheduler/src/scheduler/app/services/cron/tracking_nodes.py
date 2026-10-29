# -*- coding: utf-8 -*-
"""所有时间轴节点使用相同的证据和时间口径。"""

import hashlib
import json

from .tracking_common import iso_time, parse_time, public_value


def make_node(row, kind, title, category, source, *, basis="event_recorded"):
    key = str(row["_key"])
    node_id = hashlib.sha256(
        json.dumps(
            [source.table, source.rank, key, kind],
            ensure_ascii=False,
        ).encode(),
    ).hexdigest()
    return {
        "event_id": node_id,
        "node_type": kind,
        "category": category,
        "occurred_at": iso_time(row["_time"], source.zone),
        "time_basis": basis,
        "title": title,
        "recorded_outcome": None,
        "current_status": None,
        "entry": "unknown",
        "original_entry": None,
        "reason": None,
        "reason_basis": "missing",
        "execution_id": row.get("execution_id")
        or row.get("linked_execution_id"),
        "operation_id": row.get("operation_id"),
        "history_id": None,
        "receipt_id": row.get("receipt_id"),
        "actor_id": None,
        "actor_kind": "unrecorded",
        "evidence": "recorded_event",
        "source_refs": [{"table": source.table, "key": key}],
        "details": {},
        "gaps": [],
    }


def finish_node(node):
    return public_value(node)


def sort_key(row, source):
    stamp = parse_time(row["_time"], source.zone).timestamp()
    return (
        int(round(stamp * 1_000_000)),
        source.rank,
        int(row["_seq"] or 0),
        str(row["_key"]),
    )


def recorded_reason(node, reason, *, derived=False):
    if isinstance(reason, str) and reason:
        node["reason"] = reason
        node["reason_basis"] = (
            "derived_business_type" if derived else "recorded"
        )
    elif reason is not None:
        node["gaps"].append("reason_value_unavailable")


def manifest_member(receipt, identity, execution_id=None):
    try:
        members = json.loads(receipt.get("members_json") or "[]")
    except (TypeError, ValueError):
        return False
    if not isinstance(members, list):
        return False
    own = [
        item
        for item in members
        if isinstance(item, dict)
        and item.get("identity") == list(identity.values())
    ]
    return (
        len(own) == 1
        and isinstance(own[0].get("execution_id"), str)
        and (execution_id is None or own[0]["execution_id"] == execution_id)
    )
