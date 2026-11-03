# -*- coding: utf-8 -*-
"""Execute marketplace statistics SQL against small isolated datasets."""

import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from market.app.routers.market_browse import (
    _browse_item_dict,
    _fetch_batch_stats_chunk,
)
from market.marketplace import skill_statistics
from market.marketplace.models import MarketItem
from market.marketplace.service import MarketplaceService


@pytest.fixture
def statistics_service(tmp_path, monkeypatch):
    class DecemberClock(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 12, 15)

    monkeypatch.setattr(skill_statistics, "datetime", DecemberClock)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function("CONCAT", -1, lambda *args: "".join(args))
    connection.executescript("""
        CREATE TABLE swe_marketplace_skills (
            source_id TEXT, skill_name TEXT, include_in_statistics INTEGER
        );
        CREATE TABLE swe_tracing_spans (
            source_id TEXT, skill_name TEXT, skill_id TEXT,
            trace_id TEXT, user_id TEXT, user_name TEXT,
            start_time TEXT, bbk_id TEXT
        );
        INSERT INTO swe_marketplace_skills VALUES ('src_a', 'search', 1);
    """)
    rows = [
        (
            "src_a",
            "search",
            "new",
            "t1",
            "u1",
            "U1",
            "2026-12-10 00:00:00",
            "110",
        ),
        (
            "src_a",
            "search",
            "new",
            "t1",
            "u1",
            "U1",
            "2026-12-10 00:00:00",
            "110",
        ),
        (
            "src_a",
            "search",
            "new",
            "start",
            "u2",
            "U2",
            "2026-12-01 00:00:00",
            "110",
        ),
        (
            "src_a",
            "search",
            "new",
            "other",
            "u3",
            "U3",
            "2026-12-10 00:00:00",
            "120",
        ),
        (
            "src_a",
            "search",
            "new",
            "null",
            "u4",
            "U4",
            "2026-12-10 00:00:00",
            None,
        ),
        (
            "src_a",
            "search",
            "new",
            "next",
            "u5",
            "U5",
            "2027-01-01 00:00:00",
            "110",
        ),
        (
            "src_a",
            "search",
            "new",
            "previous",
            "u6",
            "U6",
            "2026-11-30 23:59:59",
            "110",
        ),
        (
            "src_a",
            "search",
            "new",
            "default",
            "default",
            "",
            "2026-12-10 00:00:00",
            "110",
        ),
        (
            "src_b",
            "search",
            "new",
            "source",
            "u7",
            "U7",
            "2026-12-10 00:00:00",
            "110",
        ),
        (
            "src_a",
            "search",
            "old",
            "old",
            "u8",
            "U8",
            "2026-12-10 00:00:00",
            "110",
        ),
        (
            "src_a",
            "search",
            None,
            "legacy",
            "u9",
            "U9",
            "2026-12-10 00:00:00",
            "110",
        ),
        (
            "src_a",
            "search",
            "",
            "legacy2",
            "u9",
            "U9",
            "2026-12-10 00:00:00",
            "110",
        ),
        (
            "src_a",
            "cron",
            "cron",
            "cron",
            "u1",
            "U1",
            "2026-12-10 00:00:00",
            "110",
        ),
        (
            "src_a",
            "unlisted",
            "unlisted",
            "unlisted",
            "u1",
            "U1",
            "2026-12-10 00:00:00",
            "110",
        ),
    ]
    connection.executemany(
        "INSERT INTO swe_tracing_spans VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    async def fetch_all(sql, params):
        params = tuple(
            p.isoformat(" ") if isinstance(p, datetime) else p for p in params
        )
        return [
            dict(row)
            for row in connection.execute(sql.replace("%s", "?"), params)
        ]

    async def fetch_one(sql, params):
        return (await fetch_all(sql, params))[0]

    db = AsyncMock()
    db.is_connected = True
    db.fetch_all.side_effect = fetch_all
    db.fetch_one.side_effect = fetch_one
    service = MarketplaceService(
        db=db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )
    yield service
    connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch, expected",
    [("110", 2), ("100", 4), ("V00", 4)],
)
async def test_browse_detail_and_users_execute_same_scope(
    statistics_service,
    branch,
    expected,
):
    service = statistics_service
    item = MarketItem(
        item_id="item",
        name="search",
        skill_id="new",
        creator_id="u1",
    )
    service.market_skill_registry.get_market_skill = AsyncMock(
        return_value=item,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(marketplace=service)),
    )
    stats = await _fetch_batch_stats_chunk(
        request,
        "src_a",
        "skill",
        ["search"],
        branch,
    )
    card = _browse_item_dict(item, "skill", stats)
    detail = await service.get_skill_detail("src_a", "item", branch)
    assert card["call_count"] == detail.call_count == expected
    assert card["user_count"] == detail.user_count == expected
    assert len(detail.user_stats) == expected
    assert all(user.call_count == 1 for user in detail.user_stats)
    assert await service._get_stats("search", "src_a", "old", branch) == (1, 1)
    assert await service._get_stats("search", "src_a", "", branch) == (2, 1)
    assert await service._get_stats(
        "unlisted",
        "src_a",
        "unlisted",
        branch,
    ) == (0, 0)
    assert await service._get_stats("cron", "src_a", "cron", branch) == (0, 0)
    _, params = service.db.fetch_one.await_args.args
    assert params[1:3] == (datetime(2026, 12, 1), datetime(2027, 1, 1))
