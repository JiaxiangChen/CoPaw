# -*- coding: utf-8 -*-
"""资源事务诊断只保留错误类别和栈位置，不输出 SQL 或业务输入。"""

import traceback

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


def _storage_description(error):
    number = error.args[0] if error.args else None
    if isinstance(number, int):
        category = _DATABASE_ERRORS.get(number, "see_exception_type")
        return f"error_number={number} detail={category}"
    if isinstance(error, AttributeError) and error.name in _DATABASE_METHODS:
        return f"missing_attribute={error.name}"
    if isinstance(error, OSError):
        return f"errno={error.errno}"
    return "detail=omitted_non_code_message"


def format_storage_error(error):
    """记录有限异常链及位置，不包含原始消息、源码行或局部变量。"""
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
