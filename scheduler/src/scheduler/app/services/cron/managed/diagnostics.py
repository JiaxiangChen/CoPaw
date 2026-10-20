# -*- coding: utf-8 -*-
"""输出可定位的异常链，不输出请求、任务正文或异常内携带的私密输入。"""

import re
import traceback

import httpx


def _safe_code(value):
    if isinstance(value, str) and re.fullmatch(
        r"(?:cron|swe|workflow|invalid|conflicting|agent)_[a-z0-9_]{1,120}",
        value,
    ):
        return value
    return None


def _validation_item(item):
    if not isinstance(item, dict):
        return "validation_error"
    kind = item.get("type")
    kinds = (
        "missing",
        "string_type",
        "string_too_short",
        "string_too_long",
        "extra_forbidden",
        "int_parsing",
        "int_type",
        "value_error",
        "dict_type",
        "list_type",
        "model_attributes_type",
    )
    kind = kind if kind in kinds else "validation_error"
    location = item.get("loc")
    if not isinstance(location, (list, tuple)):
        return kind
    fields = (
        "body",
        "query",
        "path",
        "header",
        "operation_id",
        "job_id",
        "tenant_id",
        "source_id",
        "agent_id",
    )
    parts = [part if part in fields else "<field>" for part in location[:8]]
    return f"{kind} at {'.'.join(parts) or '<field>'}"


def _http_detail(response):
    try:
        if len(response.content) > 8192:
            return "response_detail=omitted_large_body"
        body = response.json()
    except (ValueError, RuntimeError):
        return "response_detail=unavailable"
    detail = body.get("detail") if isinstance(body, dict) else None
    code = _safe_code(detail)
    if code:
        return f"response_detail={code}"
    if isinstance(detail, list):
        items = "; ".join(_validation_item(item) for item in detail[:8])
        return f"response_detail=validation_error count={len(detail)} {items}"
    return "response_detail=omitted_non_code_body"


def _description(error):
    if isinstance(error, httpx.HTTPStatusError):
        return (
            f"http_status={error.response.status_code} "
            f"{_http_detail(error.response)}"
        )
    code = _safe_code(str(error))
    if code:
        return f"detail={code}"
    number = error.args[0] if error.args else None
    if isinstance(number, int):
        meaning = {
            1205: "lock_wait_timeout",
            1213: "deadlock",
        }.get(number, "see_exception_type")
        return f"error_number={number} detail={meaning}"
    if isinstance(error, OSError):
        return f"errno={error.errno}"
    return "detail=omitted_non_code_message"


def format_management_error(error):
    """限制异常链和栈深度，只记录位置，避免栈源码行夹带原始输入。"""
    lines = []
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 8:
        seen.add(id(error))
        lines.append(f"{type(error).__name__}: {_description(error)}")
        for frame in traceback.extract_tb(error.__traceback__, limit=-12):
            lines.append(
                f"  {frame.filename[:300]}:{frame.lineno} in {frame.name[:100]}",
            )
        error = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
    return "\n".join(lines)
