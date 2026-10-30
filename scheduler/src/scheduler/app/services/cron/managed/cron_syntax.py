# -*- coding: utf-8 -*-
"""新版业务 Cron 的五段及末尾年份适配，不修改第三方库。"""

import re
from datetime import datetime

from croniter import CroniterBadDateError, croniter

INVALID_SCHEDULE = "invalid_cron_schedule"


def business_parts(expression):
    """校验业务表达式，六段的最后一段仅能表示年份。"""
    if not isinstance(expression, str) or len(expression) > 255:
        raise ValueError(INVALID_SCHEDULE)
    parts = expression.split()
    if len(parts) not in {5, 6}:
        raise ValueError(INVALID_SCHEDULE)
    if len(parts) == 6:
        _validate_year(parts[5])
    if not croniter.is_valid("0 " + " ".join(parts), second_at_beginning=True):
        raise ValueError(INVALID_SCHEDULE)
    return parts


def _validate_year(value):
    if value == "*":
        return
    for item in value.split(","):
        if re.fullmatch(r"[0-9]{4}(?:-[0-9]{4})?", item) is None:
            raise ValueError(INVALID_SCHEDULE)
        bounds = [int(part) for part in item.split("-")]
        if not 1970 <= bounds[0] <= bounds[-1] <= 2099:
            raise ValueError(INVALID_SCHEDULE)


def iterator(expression, base):
    """以秒在最前的固定零秒解析，允许合法范围内的远期年份。"""
    parts = business_parts(expression)
    return croniter(
        "0 " + " ".join(parts),
        base,
        second_at_beginning=True,
        max_years_between_matches=130,
    )


def next_times(expression, base, count=3, *, until=None):
    """返回真实剩余周期，有限年份耗尽不是查询错误。"""
    schedule = iterator(expression, base)
    result = []
    for _ in range(count):
        try:
            value = schedule.get_next(datetime)
        except CroniterBadDateError:
            break
        if until is not None and value >= until:
            break
        result.append(value)
    return result


def matches(expression, when):
    """核对平台显式提供的触发期，不能把年份当秒。"""
    parts = business_parts(expression)
    return croniter.match(
        "0 " + " ".join(parts),
        when,
        second_at_beginning=True,
    )
