# -*- coding: utf-8 -*-
"""Tests for tracing daily trend query service."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from monitor.app.services.tracing.query_service import TracingQueryService


class TestTracingDailyTrend:
    """覆盖日趋势查询的 SQL 分支与结果聚合行为。"""

    @pytest.mark.asyncio
    async def test_get_daily_trend_for_all_sources_merges_related_metrics(
        self,
    ):
        """source_id=all 时应排除默认平台并合并附加指标。"""
        db = AsyncMock()
        service = TracingQueryService(db)
        start = datetime(2026, 6, 1)
        end = datetime(2026, 6, 2)

        db.fetch_all.side_effect = [
            [
                {
                    "date": datetime(2026, 6, 1),
                    "calls": 3,
                    "tokens": 12,
                    "users": 2,
                },
            ],
            [{"date": datetime(2026, 6, 1), "read_tasks": 5}],
            [
                {
                    "date": datetime(2026, 6, 1),
                    "button_type": "plan",
                    "customer_count": 7,
                },
                {
                    "date": datetime(2026, 6, 1),
                    "button_type": "phone",
                    "customer_count": 1,
                },
            ],
        ]

        result = await service.get_daily_trend(
            source_id="all",
            start_date=start,
            end_date=end,
            bbk_ids="100",
        )

        trace_sql, trace_params = db.fetch_all.call_args_list[0].args
        assert "source_id NOT IN" in trace_sql
        assert trace_params == (start, end, "default", "100", "V00")

        assert result == [
            {
                "date": "2026-06-01",
                "calls": 3,
                "tokens": 12,
                "users": 2,
                "read_tasks": 5,
                "plan_customers": 7,
                "insight_customers": 0,
                "phone_customers": 1,
            },
        ]

    @pytest.mark.asyncio
    async def test_get_daily_trend_for_specific_source_uses_source_filter(
        self,
    ):
        """非 RMASSIST 的日趋势不应查询客户点击指标。"""
        db = AsyncMock()
        service = TracingQueryService(db)
        start = datetime(2026, 6, 1)
        end = datetime(2026, 6, 2)

        db.fetch_all.side_effect = [
            [
                {
                    "date": datetime(2026, 6, 2),
                    "calls": 1,
                    "tokens": 8,
                    "users": 1,
                },
            ],
            [],
        ]

        result = await service.get_daily_trend(
            source_id="tenant-a",
            start_date=start,
            end_date=end,
            bbk_ids="201",
        )

        trace_sql, trace_params = db.fetch_all.call_args_list[0].args
        read_sql, read_params = db.fetch_all.call_args_list[1].args

        assert "WHERE source_id = %s" in trace_sql
        assert trace_params == ("tenant-a", start, end, "201")
        assert "j.source_id = %s" in read_sql
        assert "DATE(e.read_at) as date" in read_sql
        assert "e.is_final = 1" in read_sql
        assert "AND e.read_at >= %s AND e.read_at <= %s" in read_sql
        assert "GROUP BY DATE(e.read_at)" in read_sql
        assert read_params == (start, end, "tenant-a", "201")
        assert len(db.fetch_all.call_args_list) == 2
        assert result[0]["read_tasks"] == 0
        assert result[0]["plan_customers"] == 0
        assert result[0]["insight_customers"] == 0
        assert result[0]["phone_customers"] == 0

    @pytest.mark.asyncio
    async def test_get_daily_trend_for_rmassist_queries_click_metrics(
        self,
    ):
        """RMASSIST 的日趋势仍应查询客户点击指标。"""
        db = AsyncMock()
        service = TracingQueryService(db)
        start = datetime(2026, 6, 1)
        end = datetime(2026, 6, 2)

        db.fetch_all.side_effect = [
            [
                {
                    "date": datetime(2026, 6, 2),
                    "calls": 1,
                    "tokens": 8,
                    "users": 1,
                },
            ],
            [],
            [],
        ]

        await service.get_daily_trend(
            source_id="RMASSIST",
            start_date=start,
            end_date=end,
            bbk_ids="201",
        )

        click_sql, click_params = db.fetch_all.call_args_list[2].args
        assert "source_id = %s" in click_sql
        assert click_params == (start, end, "RMASSIST", "201")

    @pytest.mark.asyncio
    async def test_get_hourly_trend_for_specific_source_filters_related_metrics(
        self,
    ):
        """非 RMASSIST 的小时趋势不应查询客户点击指标。"""
        db = AsyncMock()
        service = TracingQueryService(db)
        start = datetime(2026, 6, 1)
        end = datetime(2026, 6, 2)

        db.fetch_all.side_effect = [
            [{"hour_bucket": 9, "calls": 2, "tokens": 6, "users": 1}],
            [],
        ]

        result = await service.get_hourly_trend(
            source_id="tenant-a",
            start_date=start,
            end_date=end,
            bbk_ids="201",
        )

        trace_sql, trace_params = db.fetch_all.call_args_list[0].args
        read_sql, read_params = db.fetch_all.call_args_list[1].args

        assert "WHERE source_id = %s" in trace_sql
        assert trace_params == ("tenant-a", start, end, "201")
        assert "j.source_id = %s" in read_sql
        assert "HOUR(e.read_at) as hour_bucket" in read_sql
        assert "e.is_final = 1" in read_sql
        assert "AND e.read_at >= %s AND e.read_at <= %s" in read_sql
        assert "GROUP BY HOUR(e.read_at)" in read_sql
        assert read_params == (start, end, "tenant-a", "201")
        assert len(db.fetch_all.call_args_list) == 2
        assert result[9]["calls"] == 2
        assert result[9]["read_tasks"] == 0
        assert result[9]["plan_customers"] == 0
        assert result[9]["insight_customers"] == 0
        assert result[9]["phone_customers"] == 0

    @pytest.mark.asyncio
    async def test_get_hourly_trend_for_rmassist_queries_click_metrics(
        self,
    ):
        """RMASSIST 的小时趋势仍应查询客户点击指标。"""
        db = AsyncMock()
        service = TracingQueryService(db)
        start = datetime(2026, 6, 1)
        end = datetime(2026, 6, 2)

        db.fetch_all.side_effect = [
            [{"hour_bucket": 9, "calls": 2, "tokens": 6, "users": 1}],
            [],
            [],
        ]

        await service.get_hourly_trend(
            source_id="RMASSIST",
            start_date=start,
            end_date=end,
            bbk_ids="201",
        )

        click_sql, click_params = db.fetch_all.call_args_list[2].args
        assert "source_id = %s" in click_sql
        assert click_params == (start, end, "RMASSIST", "201")
