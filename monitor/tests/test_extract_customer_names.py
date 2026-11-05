# -*- coding: utf-8 -*-
"""Tests for customer name extraction queries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime as RealDateTime

import pytest

from monitor.app.models.tracing import ExtractCustomerNamesRequest
from monitor.app.routers import tracing as tracing_router
from monitor.app.services.tracing.extract_service import (
    ExtractCustomerNamesService,
)


@pytest.mark.asyncio
async def test_query_traces_by_skill_does_not_filter_event_type():
    """客户姓名提取查询不应限制 span 的 event_type。"""
    db = SimpleNamespace(
        fetch_all=AsyncMock(return_value=[]),
    )
    service = ExtractCustomerNamesService()

    await service._query_traces_by_skill(
        db=db,
        skill_names=["skill-a"],
        user_ids=None,
        bbk_id=None,
        start_date=None,
        end_date=None,
    )

    query, params = db.fetch_all.await_args.args

    assert "event_type" not in query
    assert "s.skill_name IN (%s)" in query
    assert "e.trace_id = s.trace_id" in query
    assert params == ("skill-a",)


@pytest.mark.asyncio
async def test_extract_customer_names_defaults_to_local_today(monkeypatch):
    """Missing date bounds should select the full current local day."""
    frozen_now = RealDateTime(2026, 11, 4, 10, 30)

    class FrozenDateTime(RealDateTime):
        @classmethod
        def now(cls):
            return frozen_now

    service = SimpleNamespace(extract_names=AsyncMock(return_value={}))
    monkeypatch.setattr(tracing_router, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        tracing_router.ExtractCustomerNamesService,
        "get_instance",
        lambda: service,
    )

    await tracing_router.extract_customer_names(
        ExtractCustomerNamesRequest(skill_names=["skill-a"]),
    )

    kwargs = service.extract_names.await_args.kwargs
    assert kwargs["start_date"] == RealDateTime(2026, 11, 4)
    assert kwargs["end_date"] == RealDateTime(2026, 11, 4, 23, 59, 59)


@pytest.mark.asyncio
async def test_extract_customer_names_keeps_single_date_bound(monkeypatch):
    """A supplied start date should not cause a default end date."""
    service = SimpleNamespace(extract_names=AsyncMock(return_value={}))
    monkeypatch.setattr(
        tracing_router.ExtractCustomerNamesService,
        "get_instance",
        lambda: service,
    )

    await tracing_router.extract_customer_names(
        ExtractCustomerNamesRequest(
            skill_names=["skill-a"],
            start_date="2026-11-01",
        ),
    )

    kwargs = service.extract_names.await_args.kwargs
    assert kwargs["start_date"] == RealDateTime(2026, 11, 1)
    assert kwargs["end_date"] is None
