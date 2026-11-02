# -*- coding: utf-8 -*-
"""资源处理诊断保留安全错误码、校验字段和栈位置，不输出 SQL 或输入。"""

import re
import traceback

from pydantic import ValidationError

_DATABASE_ERRORS = {
    1045: "authentication_failed",
    1049: "database_missing",
    1054: "unknown_column",
    1142: "permission_denied",
    1146: "table_missing",
    1205: "lock_wait_timeout",
    1213: "deadlock",
    2006: "server_gone_away",
    2013: "connection_lost",
}
_DATABASE_METHODS = (
    "transaction",
    "begin",
    "commit",
    "rollback",
    "fetch_one",
    "cursor",
    "execute",
)
_VALIDATION_FIELDS = frozenset(
    "id name enabled task_type tenant_id source_id scope_id skill_ids "
    "workflow_binding_id plan_id bbk_id tenant_name schedule type cron timezone "
    "run_date_range start_date end_date request dispatch runtime model_slot "
    "provider_id model session_id user_id input channel mode target meta "
    "max_concurrency timeout_seconds misfire_grace_seconds".split(),
)


def _validation_description(error):
    """只记录校验类别和已知字段路径，排除原始输入、消息及上下文。"""
    details = []
    for item in error.errors(
        include_input=False,
        include_context=False,
        include_url=False,
    )[:8]:
        path = ".".join(
            (
                str(part)
                if isinstance(part, int) or part in _VALIDATION_FIELDS
                else "<field>"
            )
            for part in item["loc"][:8]
        )
        kind = item["type"]
        if re.fullmatch(r"[a-z_]{1,64}", kind) is None:
            kind = "validation_error"
        details.append(f"{kind} at {path}")
    return f"validation_errors={error.error_count()} " + "; ".join(details)


def _storage_description(error):
    number = error.args[0] if error.args else None
    if isinstance(number, int):
        category = _DATABASE_ERRORS.get(number, "see_exception_type")
        return f"error_number={number} detail={category}"
    if isinstance(error, ValidationError):
        return _validation_description(error)
    code = str(error)
    if re.fullmatch(
        r"(?:cron|swe|workflow|invalid|conflicting|agent)_[a-z0-9_]{1,120}",
        code,
    ):
        return f"detail={code}"
    if isinstance(error, AttributeError) and error.name in _DATABASE_METHODS:
        return f"missing_attribute={error.name}"
    if isinstance(error, OSError):
        return f"errno={error.errno}"
    return "detail=omitted_non_code_message"


def format_storage_error(error):
    """记录安全错误码、校验字段和有限异常链，不含输入、源码行或局部变量。"""
    lines = []
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 8:
        seen.add(id(error))
        lines.append(f"{type(error).__name__}: {_storage_description(error)}")
        for frame in traceback.extract_tb(error.__traceback__, limit=-12):
            lines.append(
                f"  {frame.filename[:300]}:{frame.lineno} in {frame.name[:100]}",
            )
        error = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
    return "\n".join(lines)
