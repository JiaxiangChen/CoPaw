# -*- coding: utf-8 -*-
"""新版输入末尾是年份，平台和本地计算不能误当成秒。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scheduler.app.services.cron.managed.definition import _schedule
from scheduler.app.services.cron.managed.platform import platform_cron
from scheduler.app.services.cron.managed.views import _next_run_times


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("0 9 * * *", "0 0 9 * * ?"),
        ("0 9 * * * 2026", "0 0 9 * * ? 2026"),
        ("0 9 * * * 2026,2027", "0 0 9 * * ? 2026,2027"),
        ("0 9 * * * 2026-2029", "0 0 9 * * ? 2026-2029"),
        ("0 9 * * MON 2026-2029", "0 0 9 ? * 2 2026-2029"),
    ],
)
def test_business_year_is_preserved_in_platform_expression(
    expression,
    expected,
):
    assert platform_cron(expression) == expected
    assert _schedule({"cron": expression})["cron"] == expression


@pytest.mark.parametrize(
    "year",
    ["1969", "2100", "2029-2026", "2026,,2027", "2026/2"],
)
def test_invalid_or_unrequested_year_syntax_is_rejected(year):
    with pytest.raises(ValueError, match="invalid_cron_schedule"):
        _schedule({"cron": f"0 9 * * * {year}"})


def test_expired_year_returns_no_future_runs():
    row = {"cron_expr": "0 9 * * * 2025", "timezone": "Asia/Shanghai"}
    now = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _next_run_times(row, now=now) == []


def test_fewer_than_three_future_runs_is_valid():
    row = {"cron_expr": "0 9 31 12 * 2026", "timezone": "Asia/Shanghai"}
    now = datetime(2026, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _next_run_times(row, now=now) == ["2026-12-31T09:00:00+08:00"]


def test_distant_year_is_not_lost_to_default_search_horizon():
    row = {"cron_expr": "0 9 1 1 * 2099", "timezone": "Asia/Shanghai"}
    now = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _next_run_times(row, now=now) == ["2099-01-01T09:00:00+08:00"]


def test_scatter_preserves_year_when_clock_stays_in_same_day():
    from scheduler.app.services.cron.managed.scatter import (
        shift_cron_expression,
    )

    result = shift_cron_expression(
        "0 9 * * MON 2026-2029",
        "Asia/Shanghai",
        offset_minutes=240,
    )
    assert not result.error
    assert result.cron == "0 5 * * mon 2026-2029"


def test_bounded_year_scatter_does_not_silently_expand_year_at_midnight():
    from scheduler.app.services.cron.managed.scatter import (
        shift_cron_expression,
    )

    result = shift_cron_expression(
        "0 1 * * * 2026",
        "Asia/Shanghai",
        offset_minutes=240,
    )
    assert result.error
    assert not result.cron
