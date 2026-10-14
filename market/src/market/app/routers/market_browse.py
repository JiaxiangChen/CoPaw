# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import Literal, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...marketplace.browse import (
    ORPHANED_CATEGORY_ID,
    UNCATEGORIZED_CATEGORY_ID,
    count_by_branch,
    count_by_category,
    filter_market_items,
    is_orphaned_item,
)
from ...marketplace.schemas import (
    MarketMCPItem,
    MarketBrowseBranch,
    MarketBrowseCategory,
    MarketBrowseResponse,
    MarketSkillResponse,
)
from ..deps import require_source_id

router = APIRouter()
logger = logging.getLogger(__name__)
_BROWSE_STATS_CHUNK_SIZE = 500

_BROWSE_SKILL_STATS_SQL = """
    SELECT
        skill_name AS item_key,
        COUNT(*) AS call_count,
        COUNT(DISTINCT user_id) AS user_count
    FROM swe_tracing_spans
    WHERE event_type = 'skill_invocation'
      AND source_id = %s
      AND skill_name IN ({placeholders})
    GROUP BY skill_name
"""

_BROWSE_MCP_STATS_SQL = """
    SELECT
        mcp_server AS item_key,
        COUNT(*) AS call_count,
        COUNT(DISTINCT user_id) AS user_count
    FROM swe_tracing_spans
    WHERE source_id = %s
      AND mcp_server IN ({placeholders})
    GROUP BY mcp_server
"""


async def _load_categories(request: Request, source_id: str) -> list[dict]:
    db = request.app.state.marketplace.db
    if not db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return await db.fetch_all(
        "SELECT id, name, sort_order, COALESCE(branch_visible, 1) AS branch_visible "
        "FROM swe_marketplace_categories WHERE source_id = %s ORDER BY sort_order ASC",
        (source_id,),
    )


def _visible_category_ids(
    categories: list[dict],
    is_head_office: bool,
) -> set[int] | None:
    if is_head_office:
        return None
    return {
        int(category["id"])
        for category in categories
        if bool(category.get("branch_visible", True))
    }


def _branch_ids(
    items: list,
    user_bbk_id: str,
    is_head_office: bool,
) -> list[str]:
    ids = {"100"}
    if is_head_office:
        for item in items:
            ids.update(item.bbk_ids)
    elif user_bbk_id != "100":
        ids.add(user_bbk_id)
    return sorted(ids)


def _normalize_resource_filters(
    resource_type: str,
    category_id: int | None,
    uncategorized: bool,
    orphaned: bool,
    visible_ids: set[int] | None,
    known_category_ids: set[int],
) -> tuple[
    int | None,
    bool,
    bool,
    set[int] | None,
    set[int],
]:
    if resource_type != "mcp":
        return (
            category_id,
            uncategorized,
            orphaned,
            visible_ids,
            known_category_ids,
        )
    return None, False, False, None, set()


def _build_category_facets(
    resource_type: str,
    categories: list[dict],
    branch_items: list,
    known_category_ids: set[int],
    is_head_office: bool,
) -> list[MarketBrowseCategory]:
    if resource_type != "skill":
        return []
    visible_categories = [
        category
        for category in categories
        if is_head_office or bool(category.get("branch_visible", True))
    ]
    category_counts = count_by_category(
        branch_items,
        (int(category["id"]) for category in visible_categories),
    )
    result = [
        MarketBrowseCategory(
            id=int(category["id"]),
            name=category["name"],
            count=category_counts[int(category["id"])],
        )
        for category in visible_categories
    ]
    uncategorized_count = sum(
        1 for item in branch_items if item.category_id is None
    )
    orphaned_count = sum(
        1
        for item in branch_items
        if is_orphaned_item(item, known_category_ids)
    )
    if uncategorized_count > 0:
        result.append(
            MarketBrowseCategory(
                id=UNCATEGORIZED_CATEGORY_ID,
                name="未分类",
                count=uncategorized_count,
            ),
        )
    if orphaned_count > 0:
        result.append(
            MarketBrowseCategory(
                id=ORPHANED_CATEGORY_ID,
                name="待整理分类",
                count=orphaned_count,
            ),
        )
    return result


def _decode_creator_name(value: str) -> str:
    if not value:
        return value
    try:
        return unquote(value)
    except Exception:  # pylint: disable=broad-except
        return value


async def _load_batch_stats(
    request: Request,
    source_id: str,
    resource_type: str,
    items: list,
) -> dict[str, tuple[int, int]]:
    keys = _stats_keys(resource_type, items)
    if not keys:
        return {}
    try:
        return await _fetch_batch_stats(
            request,
            source_id,
            resource_type,
            keys,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to load browse stats: %s", exc)
        return {}


async def _fetch_batch_stats(
    request: Request,
    source_id: str,
    resource_type: str,
    keys: list[str],
) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for start in range(0, len(keys), _BROWSE_STATS_CHUNK_SIZE):
        chunk = keys[start : start + _BROWSE_STATS_CHUNK_SIZE]
        stats.update(
            await _fetch_batch_stats_chunk(
                request,
                source_id,
                resource_type,
                chunk,
            ),
        )
    return stats


async def _fetch_batch_stats_chunk(
    request: Request,
    source_id: str,
    resource_type: str,
    keys: list[str],
) -> dict[str, tuple[int, int]]:
    placeholders = ", ".join(["%s"] * len(keys))
    sql_template = (
        _BROWSE_SKILL_STATS_SQL
        if resource_type == "skill"
        else _BROWSE_MCP_STATS_SQL
    )
    rows = await request.app.state.marketplace.db.fetch_all(
        sql_template.format(placeholders=placeholders),
        (source_id, *keys),
    )
    return {
        str(row.get("item_key") or ""): (
            int(row.get("call_count", 0)),
            int(row.get("user_count", 0)),
        )
        for row in rows
        if row.get("item_key")
    }


def _stats_keys(resource_type: str, items: list) -> list[str]:
    key_attr = "name" if resource_type == "skill" else "client_key"
    return list(
        dict.fromkeys(
            value
            for item in items
            if (value := str(getattr(item, key_attr, "") or ""))
        ),
    )


def _browse_item_dict(
    item,
    resource_type: str,
    stats: dict[str, tuple[int, int]],
) -> dict:
    if resource_type == "skill":
        call_count, user_count = stats.get(item.name, (0, 0))
        return MarketSkillResponse(
            item_id=item.item_id,
            name=item.name,
            skill_id=item.skill_id,
            chinese_name=item.chinese_name,
            description=item.description,
            version=item.version,
            creator_id=item.creator_id,
            creator_name=_decode_creator_name(item.creator_name),
            category_id=item.category_id,
            bbk_ids=item.bbk_ids,
            status=item.status,
            created_at=item.created_at,
            updated_at=item.updated_at,
            call_count=call_count,
            user_count=user_count,
            include_in_statistics=item.include_in_statistics,
        ).model_dump()
    call_count, user_count = stats.get(item.client_key, (0, 0))
    return MarketMCPItem(
        item_id=item.item_id,
        client_key=item.client_key,
        name=item.name,
        chinese_name=item.chinese_name,
        description=item.description,
        guidance=item.guidance,
        version=item.version,
        creator_id=item.creator_id,
        creator_name=_decode_creator_name(item.creator_name),
        category_id=item.category_id,
        bbk_ids=item.bbk_ids,
        created_at=item.created_at,
        updated_at=item.updated_at,
        call_count=call_count,
        user_count=user_count,
    ).model_dump()


def _browse_item_dicts(
    items: list,
    resource_type: str,
    stats: dict[str, tuple[int, int]],
) -> list[dict]:
    return [_browse_item_dict(item, resource_type, stats) for item in items]


@router.get("/market/browse", response_model=MarketBrowseResponse)
async def browse_market(
    request: Request,
    resource_type: Literal["skill", "mcp"] = Query(...),
    category_id: Optional[int] = None,
    bbk_id: Optional[str] = None,
    uncategorized: bool = False,
    orphaned: bool = False,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
) -> MarketBrowseResponse:
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"
    is_head_office = user_bbk_id == "100"
    categories = await _load_categories(request, source_id)
    known_category_ids = {int(category["id"]) for category in categories}
    visible_ids = _visible_category_ids(categories, is_head_office)
    (
        category_id,
        uncategorized,
        orphaned,
        visible_ids,
        known_category_ids,
    ) = _normalize_resource_filters(
        resource_type,
        category_id,
        uncategorized,
        orphaned,
        visible_ids,
        known_category_ids,
    )
    raw_items = await request.app.state.marketplace.list_market_metadata(
        source_id,
        resource_type,
    )
    base_items = filter_market_items(
        raw_items,
        resource_type,
        user_bbk_id,
        None,
        None,
        is_head_office,
        visible_ids,
        False,
        False,
        known_category_ids,
    )
    branch_items = filter_market_items(
        base_items,
        resource_type,
        user_bbk_id,
        bbk_id,
        None,
        is_head_office,
        visible_ids,
        False,
        False,
        known_category_ids,
    )
    category_items = filter_market_items(
        base_items,
        resource_type,
        user_bbk_id,
        None,
        category_id,
        is_head_office,
        visible_ids,
        uncategorized,
        orphaned,
        known_category_ids,
    )
    branch_ids = _branch_ids(base_items, user_bbk_id, is_head_office)
    branch_counts = count_by_branch(category_items, branch_ids)
    browse_categories = _build_category_facets(
        resource_type,
        categories,
        branch_items,
        known_category_ids,
        is_head_office,
    )
    result_items = filter_market_items(
        base_items,
        resource_type,
        user_bbk_id,
        bbk_id,
        category_id,
        is_head_office,
        visible_ids,
        uncategorized,
        orphaned,
        known_category_ids,
    )
    stats = await _load_batch_stats(
        request,
        source_id,
        resource_type,
        result_items,
    )
    return MarketBrowseResponse(
        resource_type=resource_type,
        items=_browse_item_dicts(result_items, resource_type, stats),
        total=len(result_items),
        category_total=len(branch_items),
        branch_total=len(category_items),
        categories=browse_categories,
        branches=[
            MarketBrowseBranch(
                bbk_id=branch_id,
                count=branch_counts[branch_id],
            )
            for branch_id in branch_ids
        ],
    )
