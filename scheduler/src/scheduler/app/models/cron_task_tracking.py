# -*- coding: utf-8 -*-
"""只读任务追踪的输入约束。"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..services.cron.tracking_common import BEIJING


def day_start():
    return datetime.now(BEIJING).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


class TimelineQuery(BaseModel):
    start_time: datetime = Field(default_factory=day_start)
    end_time: datetime = Field(
        default_factory=lambda: day_start() + timedelta(days=1),
    )
    category: Literal[
        "all",
        "management",
        "scheduling",
        "execution",
        "notification",
    ] = "all"
    entry: Literal[
        "all",
        "ordinary",
        "batch",
        "manual",
        "manual_retry",
        "daily_failure_retry",
        "unknown",
        "management",
    ] = "all"
    order: Literal["asc", "desc"] = "asc"
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def check_window(self):
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("cron_tracking_timezone_required")
        duration = self.end_time - self.start_time
        if not timedelta(0) < duration <= timedelta(days=31):
            raise ValueError("invalid_cron_tracking_time_range")
        return self
