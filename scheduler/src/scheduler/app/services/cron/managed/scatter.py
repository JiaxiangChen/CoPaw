# -*- coding: utf-8 -*-
"""定时任务广播时的错峰调度与通知时间工具。"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Optional

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_TO_INDEX = {name: index for index, name in enumerate(DAY_NAMES)}
DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS = 4
MIN_BROADCAST_OFFSET_WINDOW_HOURS = 0
MAX_BROADCAST_OFFSET_WINDOW_HOURS = 24
NUM_TO_DAY = {
    "0": "sun",
    "1": "mon",
    "2": "tue",
    "3": "wed",
    "4": "thu",
    "5": "fri",
    "6": "sat",
    "7": "sun",
}


@dataclass(frozen=True)
class ShiftedCron:
    """广播错峰后的 cron 结果。"""

    cron: str
    timezone: str
    offset_minutes: int
    error: str = ""


def compute_broadcast_offsets(
    count: int,
    *,
    window_hours: int = DEFAULT_BROADCAST_OFFSET_WINDOW_HOURS,
) -> list[int]:
    """在指定小时窗口内为目标租户计算均匀错峰分钟数。"""
    if (
        window_hours < MIN_BROADCAST_OFFSET_WINDOW_HOURS
        or window_hours > MAX_BROADCAST_OFFSET_WINDOW_HOURS
    ):
        raise ValueError(
            "broadcast offset window must be between 0 and 24 hours",
        )
    if count <= 0:
        return []
    if count == 1:
        return [0]
    step = window_hours * 60 / (count - 1)
    return [round(index * step) for index in range(count)]


def shift_cron_expression(
    cron_expression: str,
    timezone_name: str,
    *,
    offset_minutes: int,
) -> ShiftedCron:
    """将 5 字段 cron 按 offset 向前平移。"""
    timezone_value = timezone_name or "UTC"
    parts = cron_expression.strip().split()
    if len(parts) != 5:
        return _error(
            timezone_value,
            offset_minutes,
            "cron must have 5 fields",
        )

    minute, hour, dom, month, dow = parts
    parsed_minute = _parse_int(minute, 0, 59)
    parsed_hour = _parse_int(hour, 0, 23)

    if (
        parsed_minute is not None
        and hour == "*"
        and _is_every_day(dom, month, dow)
    ):
        shifted_minute = (parsed_minute - offset_minutes) % 60
        return ShiftedCron(
            cron=f"{shifted_minute} * * * *",
            timezone=timezone_value,
            offset_minutes=offset_minutes,
        )

    if parsed_minute is None or parsed_hour is None:
        return _error(
            timezone_value,
            offset_minutes,
            "unsupported cron: minute and hour must be fixed numbers",
        )

    shifted_hour, shifted_minute, day_delta = _shift_clock(
        parsed_hour,
        parsed_minute,
        offset_minutes,
    )

    if _is_every_day(dom, month, dow):
        return ShiftedCron(
            cron=f"{shifted_minute} {shifted_hour} * * *",
            timezone=timezone_value,
            offset_minutes=offset_minutes,
        )

    if dom == "*" and month == "*" and dow != "*":
        shifted_dow = _shift_dow(dow, day_delta)
        if shifted_dow is None:
            return _error(
                timezone_value,
                offset_minutes,
                "unsupported cron: day-of-week field is not enumerable",
            )
        return ShiftedCron(
            cron=f"{shifted_minute} {shifted_hour} * * {shifted_dow}",
            timezone=timezone_value,
            offset_minutes=offset_minutes,
        )

    shifted_dom = _shift_dom(dom, day_delta)
    if shifted_dom is None:
        return _error(
            timezone_value,
            offset_minutes,
            "unsupported cron: month boundary cannot be expressed safely",
        )
    if dow != "*":
        return _error(
            timezone_value,
            offset_minutes,
            "unsupported cron: cannot shift both day-of-month and day-of-week",
        )
    return ShiftedCron(
        cron=f"{shifted_minute} {shifted_hour} {shifted_dom} {month} *",
        timezone=timezone_value,
        offset_minutes=offset_minutes,
    )


def _error(
    timezone_name: str,
    offset_minutes: int,
    message: str,
) -> ShiftedCron:
    return ShiftedCron(
        cron="",
        timezone=timezone_name,
        offset_minutes=offset_minutes,
        error=message,
    )


def _parse_int(value: str, minimum: int, maximum: int) -> Optional[int]:
    if not value.isdigit():
        return None
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        return None
    return parsed


def _is_every_day(dom: str, month: str, dow: str) -> bool:
    return dom == "*" and month == "*" and dow == "*"


def _shift_clock(
    hour: int,
    minute: int,
    offset_minutes: int,
) -> tuple[int, int, int]:
    total = hour * 60 + minute - offset_minutes
    day_delta = floor(total / 1440)
    total %= 1440
    return total // 60, total % 60, day_delta


def _shift_dow(dow: str, day_delta: int) -> Optional[str]:
    indexes: list[int] = []
    for token in dow.split(","):
        token = token.strip().lower()
        if not token:
            return None
        expanded = _expand_dow_token(token)
        if expanded is None:
            return None
        for item in expanded:
            shifted = (item + day_delta) % 7
            if shifted not in indexes:
                indexes.append(shifted)
    return ",".join(DAY_NAMES[index] for index in indexes)


def _expand_dow_token(token: str) -> Optional[list[int]]:
    if "/" in token:
        base, step_raw = token.rsplit("/", 1)
        if not step_raw.isdigit():
            return None
        base_values = _expand_dow_token(base)
        if base_values is None:
            return None
        step = int(step_raw)
        return base_values[::step] if step > 0 else None
    if "-" in token:
        start_raw, end_raw = token.split("-", 1)
        start = _dow_index(start_raw)
        end = _dow_index(end_raw)
        if start is None or end is None or start > end:
            return None
        return list(range(start, end + 1))
    index = _dow_index(token)
    return [index] if index is not None else None


def _dow_index(value: str) -> Optional[int]:
    normalized = NUM_TO_DAY.get(value, value)
    return DAY_TO_INDEX.get(normalized)


def _shift_dom(dom: str, day_delta: int) -> Optional[str]:
    if dom == "*":
        return "*"
    values = []
    for token in dom.split(","):
        parsed = _parse_int(token.strip(), 1, 31)
        if parsed is None:
            return None
        shifted = parsed + day_delta
        if shifted < 1 or shifted > 31:
            return None
        values.append(shifted)
    return ",".join(str(value) for value in sorted(set(values)))
