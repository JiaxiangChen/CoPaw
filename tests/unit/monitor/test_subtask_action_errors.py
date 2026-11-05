# -*- coding: utf-8 -*-
"""External action failures are persisted with the subtask status."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from monitor.app.models.subtask import SubtaskModel
from monitor.app.services.subtask.sync_service import SyncService
from monitor.app.services.subtask.query_service import QueryService


@pytest.mark.parametrize("status", ["FAIL", "PART_SUC", "SUC"])
def test_sync_action_failure(status):
    reason = "XXXXX【Prompt】执行错误：模型调用失败"
    response = Mock(status_code=200)
    response.json.return_value = {
        "returnCode": "SUC0000",
        "body": {
            "status": status,
            "actionResults": [
                {"status": "SUC", "result": "ignored"},
                {"status": "FAIL", "result": reason},
                {"status": "FAIL", "result": "【Other】another"},
            ],
        },
    }
    client = AsyncMock()
    client.post.return_value = response
    db = AsyncMock()
    query = QueryService.__new__(QueryService)
    query.db = db
    service = SyncService(query)
    service._is_configured = lambda: True
    subtask = SubtaskModel(
        task_id="task",
        trace_id="trace",
        filename="file",
        created_at=datetime.now(),
    )
    asyncio.run(
        service._process_pending_subtask(client, subtask, datetime.now()),
    )
    sql, params = db.execute.call_args.args
    assert "error_type = %s, error_reason = %s" in sql
    assert params[0] == status
    assert params[3:5] == (
        (None, None) if status == "SUC" else ("Prompt", reason)
    )
    assert params[5:] == ("task", "trace")


@pytest.mark.parametrize(
    "actions,expected",
    [
        (None, (None, None)),
        ({}, (None, None)),
        ([None, {"status": "FAIL", "result": None}], (None, None)),
        ([{"status": "FAIL", "result": "plain error"}], (None, "plain error")),
    ],
)
def test_missing_or_malformed_action_errors(actions, expected):
    assert (
        SyncService._parse_action_error({"actionResults": actions}, "FAIL")
        == expected
    )
