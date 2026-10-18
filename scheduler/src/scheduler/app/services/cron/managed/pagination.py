# -*- coding: utf-8 -*-
"""按完整查询上下文约束分页位置，不把游标当成授权凭据。"""

import base64
import json


def encode_position(context, position):
    payload = {"version": 1, "context": context, "after": position}
    return (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode(),
        )
        .decode()
        .rstrip("=")
    )


def decode_position(token, context):
    if token is None:
        return 0
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError("invalid_cursor")
        value = json.loads(
            base64.b64decode(
                token + "=" * (-len(token) % 4),
                altchars=b"-_",
                validate=True,
            ),
        )
        after = value["after"]
        if (
            value["version"] != 1
            or value["context"] != context
            or type(after) is not int
            or after < 0
        ):
            raise ValueError("invalid_cursor")
        return after
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise ValueError("invalid_cron_cursor") from exc
