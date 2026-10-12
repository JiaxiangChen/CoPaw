# -*- coding: utf-8 -*-
"""用户信息查询接口路由。

用于用户登录后获取更多用户信息，转发到外部API。
"""

import logging
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...constant import BRANCH_TREE_API_URL, USER_INFO_API_URL
from ...constant import USER_POSITION_API_URL as _USER_POSITION_API_URL
from ...constant import USER_POSITION_HEADER_KEY, USER_POSITION_HEADER_VALUE
from ...utils.bbk import get_bbk_id_by_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user-info", tags=["user-info"])

_USER_POSITION_HEADERS = {
    USER_POSITION_HEADER_KEY: USER_POSITION_HEADER_VALUE,
}


def _user_position_headers(request: Request) -> dict[str, str]:
    """组装请求级凭证和登录态，优先使用前端传入的 cookie。"""
    headers = _USER_POSITION_HEADERS.copy()
    cookie = request.headers.get("x-header-cookie") or request.headers.get(
        "cookie",
    )
    if cookie:
        headers["Cookie"] = cookie
    return headers


class UserPositionQueryRequest(BaseModel):
    """机构岗位人员查询参数，来自登录上下文。"""

    bbkOrgId: str = Field(..., min_length=1)
    mOrgCode: str = Field(..., min_length=1)
    mOrgLvl: int = Field(..., ge=0)


@router.post("/positions/by-branch", summary="查询机构岗位人员")
async def query_user_positions(
    body: UserPositionQueryRequest,
    request: Request,
) -> dict:
    """代理查询分发目标人员，凭证仅在服务端发送。"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                _USER_POSITION_API_URL,
                json=body.model_dump(),
                headers=_user_position_headers(request),
            )
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "User position API timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, "User position API error") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, "User position API unavailable") from exc


class BranchTreeQueryRequest(BaseModel):
    """分行下属机构查询参数。"""

    mOrgCode: str = Field(..., min_length=1)
    mOrgLvl: int = Field(..., ge=0)
    sysId: str = Field(..., min_length=1)


@router.post("/branches/by-branch", summary="查询分行下属支行")
async def query_branch_tree(
    body: BranchTreeQueryRequest,
    request: Request,
) -> list[dict]:
    """代理查询机构列表，复用机构岗位人员接口的凭证配置。"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                BRANCH_TREE_API_URL,
                json=body.model_dump(),
                headers=_user_position_headers(request),
            )
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Branch tree API timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, "Branch tree API error") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, "Branch tree API unavailable") from exc


class UserInfoQueryRequest(BaseModel):
    """用户信息查询请求参数。"""

    keyWord: str  # 用户ID
    compareType: str = "EQ"  # 比较类型，默认EQ


class UserInfoQueryResponse(BaseModel):
    """用户信息查询响应（动态字段）。"""

    # 使用动态字段，实际返回由外部API决定
    data: dict


@router.post(
    "/query",
    response_model=UserInfoQueryResponse,
    summary="查询用户信息",
    description="用户登录后获取更多用户信息，转发到外部API",
)
async def query_user_info(
    request: Request,
    body: UserInfoQueryRequest,
) -> UserInfoQueryResponse:
    """查询用户信息。

    Args:
        request: FastAPI请求对象
        body: 查询参数，包含keyWord（用户ID）和compareType

    Returns:
        用户信息数据

    Raises:
        HTTPException: 如果API地址未配置或请求失败
    """
    # 检查API地址是否配置
    if not USER_INFO_API_URL:
        logger.warning("USER_INFO_API_URL not configured")
        raise HTTPException(
            status_code=503,
            detail="User info API not configured",
        )

    # 构建请求
    url = USER_INFO_API_URL
    headers = {
        "Content-Type": "application/json",
    }

    # 从请求中获取认证信息并传递
    auth_header = request.headers.get("Authorization")
    if auth_header:
        headers["Authorization"] = auth_header

    # 调用外部API
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                json={
                    "keyWord": body.keyWord,
                    "compareType": body.compareType,
                },
                headers=headers,
            )

        if not response.is_success:
            error_detail = response.text
            logger.error(
                f"User info API failed: {response.status_code} - {error_detail}",
            )
            raise HTTPException(
                status_code=response.status_code,
                detail=f"User info API error: {error_detail}",
            )

        data = response.json()
        return UserInfoQueryResponse(data=data)

    except httpx.TimeoutException as exc:
        logger.error(f"User info API timeout: {exc}")
        raise HTTPException(
            status_code=504,
            detail="User info API timeout",
        ) from exc
    except httpx.RequestError as exc:
        logger.error(f"User info API request error: {exc}")
        raise HTTPException(
            status_code=502,
            detail="User info API connection error",
        ) from exc


# =============================================================================
# 租户来源信息查询接口（运维模式使用）
# =============================================================================


class TenantSourceInfo(BaseModel):
    """租户来源信息。"""

    tenant_id: str = Field(..., description="租户ID（用户ID或模板目录名）")
    tenant_name: Optional[str] = Field(default=None, description="租户名称")
    bbk_id: Optional[str] = Field(default=None, description="BBK标识")
    init_source: Optional[str] = Field(
        default=None,
        description="初始化来源模板（模板条目为default，用户条目为default_{source_id}）",
    )


class TenantSourceInfoListResponse(BaseModel):
    """租户来源信息列表响应。"""

    items: List[TenantSourceInfo] = Field(default_factory=list)


@router.get(
    "/tenants/by-source",
    response_model=TenantSourceInfoListResponse,
    summary="按来源查询租户列表",
    description="运维模式下查询指定source_id下的所有租户信息",
)
async def list_tenants_by_source(
    request: Request,
    source_id: Optional[str] = None,
) -> TenantSourceInfoListResponse:
    """按来源查询租户列表。

    Args:
        request: FastAPI请求对象
        source_id: 来源标识，如果不传则使用请求头中的X-Source-Id

    Returns:
        租户来源信息列表

    Raises:
        HTTPException: 如果数据库不可用或source_id为空
    """
    from ..workspace.tenant_init_source_store import (
        get_tenant_init_source_store,
    )

    # 如果未传source_id，从请求头获取
    if source_id is None:
        source_id = getattr(request.state, "source_id", None)

    if not source_id:
        raise HTTPException(
            status_code=400,
            detail="source_id is required",
        )

    store = get_tenant_init_source_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        )

    rows = await store.get_by_source(
        source_id,
        include_templates=True,
    )
    items = [
        TenantSourceInfo(
            tenant_id=row["tenant_id"],
            tenant_name=row.get("tenant_name"),
            bbk_id=row.get("bbk_id"),
            init_source=row.get("init_source"),
        )
        for row in rows
    ]

    return TenantSourceInfoListResponse(items=items)


# =============================================================================
# 按来源查询机构列表
# =============================================================================


class BbkInfo(BaseModel):
    """机构信息。"""

    bbk_id: str = Field(..., description="BBK标识")
    bbk_name: Optional[str] = Field(default=None, description="机构名称")


class BbkListResponse(BaseModel):
    """机构列表响应。"""

    items: List[BbkInfo] = Field(default_factory=list)


@router.get(
    "/bbk/by-source",
    response_model=BbkListResponse,
    summary="按来源查询机构列表",
    description="查询指定source_id下所有不重复的bbk_id",
)
async def list_bbk_by_source(
    request: Request,
    source_id: Optional[str] = None,
) -> BbkListResponse:
    """按来源查询机构列表。

    Args:
        request: FastAPI请求对象
        source_id: 来源标识，如果不传则使用请求头中的X-Source-Id

    Returns:
        机构信息列表

    Raises:
        HTTPException: 如果数据库不可用或source_id为空
    """
    from ..workspace.tenant_init_source_store import (
        get_tenant_init_source_store,
    )
    from ...utils.bbk import BBK_ID_TO_NAME_MAP

    # 如果未传source_id，从请求头获取
    if source_id is None:
        source_id = getattr(request.state, "source_id", None)

    if not source_id:
        raise HTTPException(
            status_code=400,
            detail="source_id is required",
        )

    store = get_tenant_init_source_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        )

    # 查询同一来源下所有不重复的 bbk_id
    rows = await store.get_bbk_by_source(source_id)
    items = [
        BbkInfo(
            bbk_id=row["bbk_id"],
            bbk_name=BBK_ID_TO_NAME_MAP.get(row["bbk_id"], row["bbk_id"]),
        )
        for row in rows
        if row.get("bbk_id")
    ]

    return BbkListResponse(items=items)


# =============================================================================
# 批量更新历史数据接口（运维模式使用）
# =============================================================================


class BatchUpdateTenantInfoResponse(BaseModel):
    """批量更新租户信息响应。"""

    total: int = Field(..., description="待处理总数")
    updated: int = Field(..., description="成功更新数")
    failed: int = Field(..., description="失败数")
    details: List[dict] = Field(default_factory=list, description="处理详情")


def _extract_bbk_id_from_path_name(path_name: str | None) -> str | None:
    """从 pathName 中提取 BBK ID。

    pathName 格式如: "某企业/总行/生产部/某组"
    提取第一个和第二个"/"之间的内容，映射为 BBK ID。

    Args:
        path_name: 路径名称

    Returns:
        BBK ID 或 None
    """
    if not path_name:
        return None

    parts = path_name.split("/")
    # parts[0] = "某企业", parts[1] = "总行", parts[2] = "生产部"
    if len(parts) >= 2 and parts[1]:
        bbk_name = parts[1]
        return get_bbk_id_by_name(bbk_name)

    return None


async def _fetch_user_info_for_tenant(
    tenant_id: str,
    headers: dict,
) -> tuple[str | None, str | None]:
    """调用外部 API 获取用户信息。

    Args:
        tenant_id: 用户 ID
        headers: 请求头

    Returns:
        (userName, bbk_id) 元组
    """
    if not USER_INFO_API_URL:
        return None, None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                USER_INFO_API_URL,
                json={
                    "keyWord": tenant_id,
                    "compareType": "EQ",
                },
                headers=headers,
            )

        if not response.is_success:
            logger.warning(
                f"User info API failed for tenant {tenant_id}: "
                f"{response.status_code}",
            )
            return None, None

        data = response.json()

        # 响应结构可能是 {"data": {...}} 或 {"data": [...]}
        outer_data = data.get("data")
        if outer_data is None:
            return None, None

        # 检查 outer_data 是 dict 还是 list
        if isinstance(outer_data, list):
            result_data = outer_data
        elif isinstance(outer_data, dict):
            result_data = outer_data.get("data", [])
            if not isinstance(result_data, list):
                result_data = []
        else:
            result_data = []

        if not result_data or len(result_data) == 0:
            return None, None

        user_info = result_data[0]
        if not isinstance(user_info, dict):
            return None, None

        user_name = user_info.get("userName")
        path_name = user_info.get("pathName")
        bbk_id = _extract_bbk_id_from_path_name(path_name)

        return user_name, bbk_id

    except Exception as e:
        logger.error(f"Error fetching user info for tenant {tenant_id}: {e}")
        return None, None


@router.post(
    "/tenants/batch-update-info",
    response_model=BatchUpdateTenantInfoResponse,
    summary="批量更新租户信息",
    description="查询以80或0开头的租户，批量更新tenant_name和bbk_id字段",
)
async def batch_update_tenant_info(
    request: Request,
) -> BatchUpdateTenantInfoResponse:
    """批量更新租户信息。

    查询 swe_tenant_init_source 表中 tenant_id 以 80 或 0 开头的数据，
    逐个调用 user-info 接口获取 userName 和 pathName，
    更新 tenant_name 和 bbk_id 字段。

    Args:
        request: FastAPI请求对象

    Returns:
        处理结果统计

    Raises:
        HTTPException: 如果数据库不可用
    """
    from ..workspace.tenant_init_source_store import (
        get_tenant_init_source_store,
    )

    store = get_tenant_init_source_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        )

    # 查询以 80 或 0 开头的租户
    rows = await store.get_by_tenant_prefix(["80", "0", "IT"])
    total = len(rows)

    if total == 0:
        return BatchUpdateTenantInfoResponse(
            total=0,
            updated=0,
            failed=0,
            details=[],
        )

    # 构建请求头
    headers = {"Content-Type": "application/json"}
    auth_header = request.headers.get("Authorization")
    if auth_header:
        headers["Authorization"] = auth_header

    updated = 0
    failed = 0
    details: list[dict] = []

    for row in rows:
        tenant_id = row["tenant_id"]
        source_id = row["source_id"]

        # 调用外部 API 获取用户信息
        user_name, bbk_id = await _fetch_user_info_for_tenant(
            tenant_id,
            headers,
        )

        # 检查是否有需要更新的内容
        current_name = row.get("tenant_name")
        current_bbk = row.get("bbk_id")

        name_changed = user_name and user_name != current_name
        bbk_changed = bbk_id and bbk_id != current_bbk

        if not name_changed and not bbk_changed:
            details.append(
                {
                    "tenant_id": tenant_id,
                    "status": "skipped",
                    "reason": "no change needed",
                },
            )
            continue

        # 执行更新
        success = await store.update_tenant_info(
            tenant_id=tenant_id,
            source_id=source_id,
            tenant_name=user_name if name_changed else current_name,
            bbk_id=bbk_id if bbk_changed else current_bbk,
        )

        if success:
            updated += 1
            details.append(
                {
                    "tenant_id": tenant_id,
                    "status": "updated",
                    "tenant_name": user_name,
                    "bbk_id": bbk_id,
                },
            )
        else:
            failed += 1
            details.append(
                {
                    "tenant_id": tenant_id,
                    "status": "failed",
                    "reason": "database update failed",
                },
            )

    logger.info(
        f"Batch update completed: total={total}, "
        f"updated={updated}, failed={failed}",
    )

    return BatchUpdateTenantInfoResponse(
        total=total,
        updated=updated,
        failed=failed,
        details=details,
    )


__all__ = ["router"]
