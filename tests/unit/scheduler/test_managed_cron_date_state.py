# -*- coding: utf-8 -*-
"""日期限制独立于人工暂停，并在开始新执行前实时生效。"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from scheduler.app.services.cron.managed.definition import normalize_spec
from scheduler.app.services.cron.managed.executions import (
    ExecutionConflict,
    ExecutionRequest,
    _ready,
)
from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.managed.views import task_view
from scheduler.app.services.cron.managed.views import _next_run_times
from tests.unit.scheduler.test_managed_cron_management import create_payload


def definition(**values):
    return {
        "deleted_at": None,
        "enabled": True,
        "definition_version": 1,
        "state_management_status": "ready",
        "state_pause_reason": None,
        "run_start_date": None,
        "run_end_date": None,
        **values,
    }


def test_creation_accepts_optional_date_range():
    period = {"start_date": "2026-11-01", "end_date": "2026-11-30"}
    raw = create_payload(run_date_range=period)["spec"]
    caller = CallerIdentity("alice", "bank", "default", "alice")
    result = normalize_spec(raw, caller, "job-1")
    assert result["run_date_range"] == period


@pytest.mark.parametrize("period", [None, {}])
def test_empty_date_range_does_not_pause_creation(period):
    raw = create_payload(run_date_range=period)["spec"]
    caller = CallerIdentity("alice", "bank", "default", "alice")
    assert normalize_spec(raw, caller, "job-1")["run_date_range"] is None


@pytest.mark.parametrize(
    "period",
    [
        {"start_date": "2026-11-01"},
        {"start_date": "2026-11-30", "end_date": "2026-11-01"},
        {"start_date": "2026-02-30", "end_date": "2026-11-01"},
    ],
)
def test_invalid_date_range_is_not_silently_unlimited(period):
    caller = CallerIdentity("alice", "bank", "default", "alice")
    with pytest.raises(ValueError, match="invalid_cron_run_date_range"):
        normalize_spec(
            create_payload(run_date_range=period)["spec"],
            caller,
            "job-1",
        )


def test_expired_date_range_blocks_manual_execution():
    row = definition(run_start_date="2000-01-01", run_end_date="2000-01-02")
    with pytest.raises(ExecutionConflict, match="cron_job_outside_date_range"):
        _ready(row, ExecutionRequest("manual", "request-1"))


def test_expired_date_range_is_visible_without_changing_enabled():
    row = definition(run_start_date="2000-01-01", run_end_date="2000-01-02")
    view = task_view(row, "alice")
    assert view["is_paused"] is True
    assert view["pause_reason"] == "date_expired"
    assert row["enabled"] is True


def test_manual_pause_reason_survives_date_projection():
    row = definition(
        enabled=False,
        run_start_date="2099-01-01",
        run_end_date="2099-01-02",
    )
    assert task_view(row, "alice")["pause_reason"] == "manual"


def test_next_runs_start_inside_date_range_and_stop_after_last_day():
    row = definition(
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        run_start_date="2026-11-01",
        run_end_date="2026-11-02",
    )
    now = datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _next_run_times(row, now=now) == [
        "2026-11-01T09:00:00+08:00",
        "2026-11-02T09:00:00+08:00",
    ]


def test_date_extremes_do_not_overflow_future_time_search():
    row = definition(
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        run_start_date="0001-01-01",
        run_end_date="9999-12-31",
    )
    now = datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert len(_next_run_times(row, now=now)) == 3
