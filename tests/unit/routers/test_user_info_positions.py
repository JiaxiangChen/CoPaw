# -*- coding: utf-8 -*-
"""机构岗位人员代理的外部请求契约。"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException, Request

from swe.app.routers import user_info


@pytest.fixture
def incoming_request():
    return Request({"type": "http", "headers": []})


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
async def test_query_positions_forwards_body_and_credential(
    client,
    incoming_request,
):
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
    assert (
        await user_info.query_user_positions(body, incoming_request) == payload
    )
    client.post.assert_awaited_once_with(
        user_info._USER_POSITION_API_URL,
        json={"bbkOrgId": "755", "mOrgCode": "755480", "mOrgLvl": 70},
        headers={"xxx": "yyyy"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 500])
async def test_query_positions_upstream_failure(
    client,
    status,
    incoming_request,
):
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
            incoming_request,
        )
    assert error.value.status_code == 502
    assert "private" not in error.value.detail


@pytest.mark.asyncio
async def test_query_positions_timeout(client, incoming_request):
    client.post.side_effect = httpx.ReadTimeout("timeout")
    with pytest.raises(HTTPException) as error:
        await user_info.query_user_positions(
            user_info.UserPositionQueryRequest(
                bbkOrgId="755",
                mOrgCode="755480",
                mOrgLvl=70,
            ),
            incoming_request,
        )
    assert error.value.status_code == 504


@pytest.mark.asyncio
async def test_query_branch_tree_forwards_branch_and_credential(
    client,
    incoming_request,
):
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
    assert await user_info.query_branch_tree(body, incoming_request) == payload
    client.post.assert_awaited_once_with(
        user_info.BRANCH_TREE_API_URL,
        json={"mOrgCode": "755", "mOrgLvl": 30, "sysId": "cookie-system"},
        headers={"xxx": "yyyy"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["positions", "branches"])
@pytest.mark.parametrize(
    "headers, expected",
    [
        (
            [(b"x-header-cookie", b"sysid=retail; vorglvl=30")],
            "sysid=retail; vorglvl=30",
        ),
        ([(b"cookie", b"sysid=fallback")], "sysid=fallback"),
        (
            [
                (b"cookie", b"userid=old"),
                (b"x-header-cookie", b"userid=current"),
            ],
            "userid=current",
        ),
    ],
)
async def test_external_queries_forward_request_cookie(
    client,
    endpoint,
    headers,
    expected,
):
    incoming = Request({"type": "http", "headers": headers})
    if endpoint == "positions":
        handler = user_info.query_user_positions
        body = user_info.UserPositionQueryRequest(
            bbkOrgId="755",
            mOrgCode="755480",
            mOrgLvl=70,
        )
        payload = {"code": "success", "data": []}
    else:
        handler = user_info.query_branch_tree
        body = user_info.BranchTreeQueryRequest(
            mOrgCode="755",
            mOrgLvl=30,
            sysId="retail",
        )
        payload = []
    client.post.return_value = httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "http://upstream.test"),
    )
    await handler(body, incoming)
    assert client.post.call_args.kwargs["headers"] == {
        "xxx": "yyyy",
        "Cookie": expected,
    }
    # 后续无 cookie 请求不应继承上一个用户的登录态。
    await handler(body, Request({"type": "http", "headers": []}))
    assert client.post.call_args.kwargs["headers"] == {"xxx": "yyyy"}
