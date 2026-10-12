# -*- coding: utf-8 -*-
"""机构岗位人员代理的外部请求契约。"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from swe.app.routers import user_info


@pytest.fixture
def client(monkeypatch):
    client = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        user_info.httpx,
        "AsyncClient",
        lambda **kwargs: context,
    )
    return client


@pytest.mark.asyncio
async def test_query_positions_forwards_body_and_credential(client):
    payload = {
        "code": "success",
        "errMsg": None,
        "data": [{"sapID": "00000000"}],
    }
    client.post.return_value = httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", user_info._USER_POSITION_API_URL),
    )
    body = user_info.UserPositionQueryRequest(
        bbkOrgId="755",
        mOrgCode="755480",
        mOrgLvl=70,
    )
    assert await user_info.query_user_positions(body) == payload
    client.post.assert_awaited_once_with(
        user_info._USER_POSITION_API_URL,
        json={"bbkOrgId": "755", "mOrgCode": "755480", "mOrgLvl": 70},
        headers={"xxx": "yyyy"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 500])
async def test_query_positions_upstream_failure(client, status):
    client.post.return_value = httpx.Response(
        status,
        text="private upstream details",
        request=httpx.Request("POST", user_info._USER_POSITION_API_URL),
    )
    with pytest.raises(HTTPException) as error:
        await user_info.query_user_positions(
            user_info.UserPositionQueryRequest(
                bbkOrgId="755",
                mOrgCode="755480",
                mOrgLvl=70,
            ),
        )
    assert error.value.status_code == 502
    assert "private" not in error.value.detail


@pytest.mark.asyncio
async def test_query_positions_timeout(client):
    client.post.side_effect = httpx.ReadTimeout("timeout")
    with pytest.raises(HTTPException) as error:
        await user_info.query_user_positions(
            user_info.UserPositionQueryRequest(
                bbkOrgId="755",
                mOrgCode="755480",
                mOrgLvl=70,
            ),
        )
    assert error.value.status_code == 504


@pytest.mark.asyncio
async def test_query_branch_tree_forwards_branch_and_credential(client):
    payload = [
        {
            "sysId": "retailbanking",
            "mOrgCode": "755480",
            "mOrgName": "某支行",
            "mOrgLvl": 70,
            "leaf": True,
        },
    ]
    client.post.return_value = httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", user_info.BRANCH_TREE_API_URL),
    )
    body = user_info.BranchTreeQueryRequest(
        mOrgCode="755",
        mOrgLvl=30,
        sysId="cookie-system",
    )
    assert await user_info.query_branch_tree(body) == payload
    client.post.assert_awaited_once_with(
        user_info.BRANCH_TREE_API_URL,
        json={"mOrgCode": "755", "mOrgLvl": 30, "sysId": "cookie-system"},
        headers={"xxx": "yyyy"},
    )
