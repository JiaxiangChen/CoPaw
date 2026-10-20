# -*- coding: utf-8 -*-
"""保留旧版执行失败说明，并保存重试分类及脱敏摘要。"""

import asyncio
import json
import re

from ..runner.model_call_error_detail import redact_sensitive_fragments

ERROR_DETAIL_LIMIT = 2048
LEGACY_ERROR_MESSAGE_LIMIT = 200
_TOKEN_ASSIGNMENT = re.compile(
    r"""(?i)(["']?\btoken["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)""",
)


def public_failure_detail(error):
    """与旧版 str(error)[:200] 一致，但先脱敏、不展开异常链。"""
    detail = (
        "Job was cancelled"
        if isinstance(error, asyncio.CancelledError)
        else str(error)
    )
    detail = _TOKEN_ASSIGNMENT.sub(r"\1[REDACTED]", detail)
    return redact_sensitive_fragments(detail)[:LEGACY_ERROR_MESSAGE_LIMIT]


def public_error_message(error_code, metadata):
    """新记录用脱敏异常说明，旧记录或空异常仍回退稳定错误码。"""
    if not error_code:
        return None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    detail = (
        metadata.get("error_detail") if isinstance(metadata, dict) else None
    )
    if isinstance(detail, str) and detail.strip():
        return redact_sensitive_fragments(detail)[:ERROR_DETAIL_LIMIT]
    return error_code


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
