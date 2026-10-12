# -*- coding: utf-8 -*-
"""定时任务广播错峰工具测试。"""

import pytest

from swe.app.crons.broadcast import (
    compute_broadcast_offsets,
    shift_cron_expression,
)


def test_compute_broadcast_offsets_spreads_inside_four_hour_window():
    assert compute_broadcast_offsets(1) == [0]
    assert compute_broadcast_offsets(3) == [0, 120, 240]
    assert compute_broadcast_offsets(5) == [0, 60, 120, 180, 240]


def test_compute_broadcast_offsets_uses_configured_hour_window():
    assert compute_broadcast_offsets(3, window_hours=1) == [0, 30, 60]
    assert compute_broadcast_offsets(4, window_hours=24) == [0, 480, 960, 1440]


@pytest.mark.parametrize("window_hours", [-1, 25])
def test_compute_broadcast_offsets_rejects_invalid_hour_window(window_hours):
    with pytest.raises(ValueError, match="between 0 and 24"):
        compute_broadcast_offsets(3, window_hours=window_hours)


def test_compute_broadcast_offsets_zero_window_preserves_original_schedule():
    assert compute_broadcast_offsets(3, window_hours=0) == [0, 0, 0]


def test_shift_daily_cron_uses_job_timezone_and_crosses_day_boundary():
    shifted = shift_cron_expression(
        "30 1 * * *",
        "Asia/Shanghai",
        offset_minutes=120,
    )

    assert shifted.cron == "30 23 * * *"
    assert shifted.timezone == "Asia/Shanghai"
    assert shifted.offset_minutes == 120


def test_shift_weekly_cron_adjusts_weekday_when_crossing_day_boundary():
    shifted = shift_cron_expression(
        "30 1 * * mon,wed",
        "Asia/Shanghai",
        offset_minutes=120,
    )

    assert shifted.cron == "30 23 * * sun,tue"


def test_shift_monthly_first_day_crossing_previous_month_is_rejected():
    shifted = shift_cron_expression(
        "30 1 1 * *",
        "Asia/Shanghai",
        offset_minutes=120,
    )

    assert shifted.error
    assert "month" in shifted.error.lower()
