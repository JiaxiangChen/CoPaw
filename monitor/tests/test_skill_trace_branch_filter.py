# -*- coding: utf-8 -*-
"""Branch scope regression coverage for skill detail queries."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from monitor.app.routers.tracing import router
from monitor.app.services.tracing.query_service import TracingQueryService


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["all", "RMASSIST"])
@pytest.mark.parametrize(
    ("bbk_ids", "expected_ids"),
    [(None, []), ("200,201", ["200", "201"]), ("100", ["100", "V00"])],
)
async def test_skill_traces_filter_count_and_page(
    source_id,
    bbk_ids,
    expected_ids,
):
    db = AsyncMock()
    db.fetch_one.return_value = {"total": 0}
    db.fetch_all.return_value = []
    service = TracingQueryService(db=db)
    await service.get_skill_traces(
        "weather",
        source_id,
        page=2,
        page_size=10,
        start_date=datetime(2026, 11, 1),
        end_date=datetime(2026, 11, 4),
        bbk_ids=bbk_ids,
    )
    count_sql, count_params = db.fetch_one.call_args.args
    data_sql, data_params = db.fetch_all.call_args.args
    assert data_params == (*count_params, 10, 10)
    for sql in (count_sql, data_sql):
        assert ("s.bbk_id IN" in sql) == bool(expected_ids)
        assert sql.count("%s") == len(count_params) + (
            2 if sql == data_sql else 0
        )
    if expected_ids:
        assert list(count_params[-len(expected_ids) :]) == expected_ids


def test_skill_trace_route_forwards_branch_scope():
    service = AsyncMock()
    service.get_skill_traces.return_value = ([], 0)
    app = FastAPI()
    app.include_router(router)
    with patch.object(
        TracingQueryService,
        "get_instance",
        return_value=service,
    ):
        with TestClient(app) as client:
            response = client.get(
                "/monitor/tracing/skills/weather/traces",
                params={
                    "bbk_ids": "200,201",
                    "start_date": "2026-11-01",
                    "end_date": "2026-11-03",
                },
                headers={"X-Source-Id": "RMASSIST"},
            )
    assert response.status_code == 200
    service.get_skill_traces.assert_awaited_once_with(
        "weather",
        "RMASSIST",
        1,
        20,
        datetime(2026, 11, 1),
        datetime(2026, 11, 4),
        "200,201",
    )
