# -*- coding: utf-8 -*-
"""北京时间运行日期约束，与任务自身启用和暂停原因独立。"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
INVALID_RANGE = "invalid_cron_run_date_range"


def normalize_range(value):
    """空配置不限日期，非空配置必须是首尾齐全的 ISO 日期。"""
    if value is None or value == {}:
        return None
    if not isinstance(value, dict) or set(value) != {"start_date", "end_date"}:
        raise ValueError(INVALID_RANGE)
    if value["start_date"] is None and value["end_date"] is None:
        return None
    try:
        start, end = (
            date.fromisoformat(value[key])
            for key in ("start_date", "end_date")
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(INVALID_RANGE) from exc
    if (
        start > end
        or start.isoformat() != value["start_date"]
        or end.isoformat() != value["end_date"]
    ):
        raise ValueError(INVALID_RANGE)
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def bounds(row):
    """同时供定义快照和展开后的共库行使用。"""
    period = row.get("run_date_range") or {}
    start = row.get("run_start_date") or period.get("start_date")
    end = row.get("run_end_date") or period.get("end_date")
    return tuple(_day(value) if value else None for value in (start, end))


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(value)


def reason(row, now=None):
    """返回实际当天的日期暂停原因，不修改人工意图。"""
    start, end = bounds(row)
    if start is None and end is None:
        return None
    if start is None or end is None:
        return "date_configuration_invalid"
    instant = now or datetime.now(BEIJING)
    instant = (
        instant.replace(tzinfo=BEIJING)
        if instant.tzinfo is None
        else instant.astimezone(BEIJING)
    )
    if instant.date() < start:
        return "date_before_start"
    return "date_expired" if instant.date() > end else None


def platform_window_open(row, now=None):
    """入口可提前一天打开，但实际执行许可仍仅由 reason 判断当天。"""
    now = now or datetime.now(BEIJING)
    return (
        reason(row, now) is None
        or reason(row, now + timedelta(days=1)) is None
    )


def columns(spec):
    """返回日期的展开副本，写入者必须与定义在同一事务保存。"""
    period = spec.get("run_date_range") or {}
    return {
        "run_start_date": period.get("start_date"),
        "run_end_date": period.get("end_date"),
    }


def search_window(row, base):
    """将未来周期搜索直接移至开始日，避免逐分钟跳过整个日期空档。"""
    start, end = bounds(row)
    if start and start != date.min:
        earliest = datetime.combine(start, time.min, BEIJING) - timedelta(
            microseconds=1,
        )
        base = max(base, earliest.astimezone(base.tzinfo))
    until = (
        datetime.combine(end + timedelta(days=1), time.min, BEIJING)
        if end and end != date.max
        else None
    )
    return base, until
