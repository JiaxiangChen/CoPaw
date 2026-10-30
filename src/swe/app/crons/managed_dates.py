# -*- coding: utf-8 -*-
"""SWE 只核对共库展开的实际日期，不启动日期维护任务。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def outside_date_range(definition):
    """独立检查实际开始日期，不能只相信 Scheduler 的入队快照。"""
    start, end = (
        definition.get(key) for key in ("run_start_date", "run_end_date")
    )
    if not start and not end:
        return False
    if not start or not end:
        return True
    try:
        start = start if isinstance(start, date) else date.fromisoformat(start)
        end = end if isinstance(end, date) else date.fromisoformat(end)
    except (ValueError, TypeError):
        return True
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return not start <= today <= end
