# -*- coding: utf-8 -*-
"""游标只定位已授权查询的边界，不承担权限证明。"""

import base64
import hashlib
import json
from datetime import datetime, timezone

from .tracking_common import parse_time


def fingerprint(identity, query):
    payload = [
        identity.values(),
        query.start_time.astimezone(timezone.utc).isoformat(),
        query.end_time.astimezone(timezone.utc).isoformat(),
        query.category,
        query.entry,
        query.order,
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode(),
    ).hexdigest()


def decode_cursor(value, expected):
    try:
        decoded = base64.b64decode(
            value.encode(),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded)
        boundary = payload["boundary"]
        upper = parse_time(payload["upper"])
        valid = (
            payload["v"] == 1
            and payload["fingerprint"] == expected
            and isinstance(boundary, list)
            and len(boundary) == 4
            and all(type(part) is int for part in boundary[:3])
            and 0 < boundary[0] < 10**18
            and 0 <= boundary[1] <= 100
            and boundary[2] >= 0
            and isinstance(boundary[3], str)
            and len(boundary[3]) <= 256
            and upper is not None
            and upper.tzinfo is not None
        )
        if not valid:
            raise ValueError()
        return tuple(boundary), upper
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("invalid_cron_tracking_cursor") from exc


def encode_cursor(boundary, upper, expected):
    payload = {
        "v": 1,
        "fingerprint": expected,
        "boundary": boundary,
        "upper": upper.isoformat(),
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode(),
    ).decode()


def sql_time(value, zone):
    return value.astimezone(zone).replace(tzinfo=None).isoformat(" ")


def boundary_clause(boundary, source, descending):
    if boundary is None:
        return "", ()
    stamp, rank, sequence, key = boundary
    stamp = sql_time(
        datetime.fromtimestamp(stamp / 1_000_000, timezone.utc),
        source.zone,
    )
    comparison = "<" if descending else ">"
    equality_allowed = source.rank < rank if descending else source.rank > rank
    if source.rank != rank:
        clause = f" AND (_time{comparison}%s" + (
            " OR _time=%s)" if equality_allowed else ")"
        )
        return clause, (stamp, stamp) if equality_allowed else (stamp,)
    return (
        f" AND (_time{comparison}%s OR (_time=%s AND "
        f"(_seq{comparison}%s OR (_seq=%s AND _key COLLATE utf8mb4_bin{comparison}%s))))",
        (stamp, stamp, sequence, sequence, key),
    )
