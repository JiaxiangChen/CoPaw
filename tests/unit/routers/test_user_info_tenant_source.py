# -*- coding: utf-8 -*-
"""Tests for tenant source listing in user-info router."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.routers.user_info import list_tenants_by_source


@pytest.mark.asyncio
async def test_list_tenants_by_source_includes_templates(monkeypatch):
    """运维租户来源列表应包含 template 条目。"""
    store = SimpleNamespace(
        get_by_source=AsyncMock(
            return_value=[
                {
                    "tenant_id": "default_ruice",
                    "tenant_name": "模板",
                    "bbk_id": None,
                    "init_source": "default",
                    "tenant_type": "template",
                },
                {
                    "tenant_id": "tenant-a",
                    "tenant_name": "用户A",
                    "bbk_id": "bbk-a",
                    "init_source": "default_ruice",
                    "tenant_type": "tenant",
                },
            ],
        ),
    )
    monkeypatch.setattr(
        "swe.app.workspace.tenant_init_source_store.get_tenant_init_source_store",
        lambda: store,
    )

    request = SimpleNamespace(state=SimpleNamespace(source_id="ruice"))
    response = await list_tenants_by_source(request, source_id=None)

    store.get_by_source.assert_awaited_once_with(
        "ruice",
        include_templates=True,
        bbk_id=None,
    )
    assert [item.tenant_id for item in response.items] == [
        "default_ruice",
        "tenant-a",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bbk_id", "expected_ids"),
    [
        (None, ["template", "tenant-a", "tenant-b"]),
        ("bbk-a", ["tenant-a"]),
        ("missing", []),
        ("", []),
    ],
)
async def test_list_tenants_by_source_filters_bbk_id(
    monkeypatch,
    bbk_id,
    expected_ids,
):
    store = SimpleNamespace(
        get_by_source=AsyncMock(
            return_value=[
                {"tenant_id": tenant_id, "bbk_id": bbk_id}
                for tenant_id in expected_ids
            ],
        ),
    )
    monkeypatch.setattr(
        "swe.app.workspace.tenant_init_source_store.get_tenant_init_source_store",
        lambda: store,
    )
    request = SimpleNamespace(state=SimpleNamespace(source_id="header-source"))

    response = await list_tenants_by_source(
        request,
        source_id="ruice",
        bbk_id=bbk_id,
    )

    store.get_by_source.assert_awaited_once_with(
        "ruice",
        include_templates=True,
        bbk_id=bbk_id,
    )
    assert [item.tenant_id for item in response.items] == expected_ids
