# -*- coding: utf-8 -*-
"""在异常仍可见时只生成公开分类，不持久化原始异常文本。"""

import asyncio

RULES = (
    ("auth_expired", ("cron auth user_info is expired",)),
    ("subtask_timeout", ("获取子任务状态超时",)),
    ("subtask_failed", ("子任务执行失败",)),
    (
        "configuration",
        (
            "validation error",
            "channel not found",
            "illegal argument",
            "configuration",
            "workflow version",
            "workflow binding",
        ),
    ),
    (
        "model_rate_limit",
        (
            "rate limit",
            "rate-limited",
            "lailgw0429",
            "lailgw0433",
            "too many requests",
            "过多的访问请求",
            "每分钟上限",
        ),
    ),
    (
        "outcome_unknown",
        (
            "dispatch outcome unknown",
            "no execution record",
            "callback outcome unknown",
        ),
    ),
    ("execution_timeout", ("timeout", "timed out", "超时")),
    (
        "service_error",
        ("connection", "connecterror", "network", "502", "503", "504"),
    ),
)


def classify_error(error):
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "execution_timeout"
    text = (type(error).__name__ + " " + str(error)).lower()
    return next(
        (
            kind
            for kind, patterns in RULES
            if any(pattern in text for pattern in patterns)
        ),
        "other",
    )


def error_metadata(error):
    original = getattr(error, "cron_execution_meta", None)
    return {
        **(original if isinstance(original, dict) else {}),
        "failure_type": classify_error(error),
    }
