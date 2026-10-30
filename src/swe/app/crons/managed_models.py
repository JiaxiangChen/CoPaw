# -*- coding: utf-8 -*-
"""仅新版执行链支持末尾年份，不放宽旧管理接口的排程规则。"""

import re
import json

from croniter import croniter
from pydantic import field_validator

from .models import CronJobSpec, ScheduleSpec


class ManagedScheduleSpec(ScheduleSpec):
    """接受五段业务排程或追加年份的六段排程。"""

    @field_validator("cron")
    @classmethod
    def normalize_cron_5_fields(cls, v):
        parts = v.split()
        if len(v) > 255 or len(parts) not in {5, 6}:
            raise ValueError("invalid_cron_schedule")
        base = ScheduleSpec.normalize_cron_5_fields(" ".join(parts[:5]))
        if len(parts) == 6:
            _validate_year(parts[5])
            base += " " + parts[5]
        if not croniter.is_valid("0 " + base, second_at_beginning=True):
            raise ValueError("invalid_cron_schedule")
        return base


def _validate_year(value):
    if value == "*":
        return
    for item in value.split(","):
        if re.fullmatch(r"[0-9]{4}(?:-[0-9]{4})?", item) is None:
            raise ValueError("invalid_cron_schedule")
        bounds = [int(part) for part in item.split("-")]
        if not 1970 <= bounds[0] <= bounds[-1] <= 2099:
            raise ValueError("invalid_cron_schedule")


class ManagedCronJobSpec(CronJobSpec):
    """保留原任务能力，仅增加新链路排程和可选运行日期配置。"""

    schedule: ManagedScheduleSpec
    run_date_range: dict | None = None


def load_managed_spec(value):
    """旧五段仍使用原模型，新增年份或日期的快照才使用扩展模型。"""
    raw = json.loads(value) if isinstance(value, str) else value
    expression = raw.get("schedule", {}).get("cron", "")
    model = (
        ManagedCronJobSpec
        if len(expression.split()) == 6 or raw.get("run_date_range")
        else CronJobSpec
    )
    return model.model_validate(raw)
