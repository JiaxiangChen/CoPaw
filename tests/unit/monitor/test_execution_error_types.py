# -*- coding: utf-8 -*-
"""Failure settlement aggregates types and directly batch updates executions."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from monitor.app.services.subtask.query_service import QueryService


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], []),
        (
            [
                (42, "Prompt", 0),
                (42, " API ", 0),
                (42, "Prompt", 0),
                (42, "", 0),
                (43, None, 1),
                (43, "  ", 1),
            ],
            [("API,Prompt", 0, 42), (None, 1, 43)],
        ),
    ],
)
def test_failure_settlement_error_types(rows, expected):
    db = AsyncMock()
    db.fetch_all.return_value = [
        {
            "execution_id": execution_id,
            "error_type": error_type,
            "need_notification": notification,
        }
        for execution_id, error_type, notification in rows
    ]
    db.execute.return_value = 0
    db.execute_many.return_value = len(expected)
    service = QueryService(db=db)
    service._get_completed_execution_candidates = AsyncMock(
        return_value=[
            {"execution_id": 42},
            {"execution_id": 43},
        ],
    )
    service._index_success_execution_results = AsyncMock(return_value=(0, []))

    result = asyncio.run(service.batch_update_execution_async_status())

    assert result == (0, len(expected), 0, [])
    select_sql, select_params = db.fetch_all.call_args.args
    assert "s.status IN ('FAIL', 'PART_SUC', 'TIMEOUT')" in select_sql
    assert "AND NOT EXISTS" in select_sql
    assert "pending.status IS NULL OR pending.status = ''" in select_sql
    assert select_params == (42, 43)
    sql, params = db.execute_many.call_args.args
    assert params == expected
    assert "async_status = 'error'" in sql
    assert "WHERE id = %s" in sql
    assert "async_status IS NULL OR async_status = ''" in sql
    assert "swe_cron_subtasks" not in sql
    db.execute.assert_awaited_once()
