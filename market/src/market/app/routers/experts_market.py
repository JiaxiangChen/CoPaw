# -*- coding: utf-8 -*-
"""Expert Community admin routes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Request, status

from ...marketplace.schemas import (
    ExpertDistributionRequest,
    ExpertDistributionResponse,
    ExpertInstallRequest,
    DistributionRecord,
    ExpertRecallRequest,
    ExpertRecallResponse,
    MarketExpertDetail,
    MarketExpertResponse,
    PublishExpertRequest,
)
from ...marketplace.service import (
    ExpertDependencyError,
    ExpertNameConflictError,
)
from ...security import SkillScanError
from ..deps import decode_user_name, require_source_id

router = APIRouter()


def _require_manager(x_manager: Optional[str]) -> None:
    if x_manager != "true":
        raise HTTPException(status_code=403, detail="Manager access required")


@router.post(
    "/market/experts",
    response_model=MarketExpertResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_expert(
    req: PublishExpertRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
):
    """Publish a community expert."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )
    svc = request.app.state.marketplace
    try:
        item, version_unchanged = await svc.publish_expert_from_profile(
            source_id,
            x_user_id,
            req.agent_id,
            req.definition_id,
            category_id=req.category_id,
            bbk_ids=req.bbk_ids,
            creator_name=decode_user_name(x_user_name) or "",
            overwrite=req.overwrite,
        )
    except ExpertNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "existing_item_id": exc.existing_item_id,
                "existing_name": exc.existing_name,
                "existing_creator_id": exc.existing_creator_id,
                "existing_creator_name": exc.existing_creator_name,
                "existing_version": exc.existing_version,
            },
        ) from exc
    except ExpertDependencyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SkillScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MarketExpertResponse(
        item_id=item.item_id,
        name=item.name,
        description=item.description,
        version=item.version,
        creator_id=item.creator_id,
        creator_name=item.creator_name,
        category_id=item.category_id,
        bbk_ids=item.bbk_ids,
        status=item.status,
        created_at=item.created_at,
        updated_at=item.updated_at,
        version_unchanged=version_unchanged,
    )


@router.post(
    "/market/experts/{item_id}/versions/{version_id}/restore",
    response_model=MarketExpertDetail,
)
async def restore_expert_version(
    item_id: str,
    version_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """Restore a historical expert version."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    try:
        item = await svc.restore_expert_version(
            source_id,
            item_id,
            version_id,
            operator_id="manager",
            operator_name="Manager",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    detail = await svc.get_expert_detail(source_id, item_id, "100")
    if detail is None:
        raise HTTPException(status_code=404, detail="Expert not found")
    return MarketExpertDetail.model_validate(
        detail,
        from_attributes=True,
    ).model_copy(update={"version": item.version})


@router.delete("/market/experts/{item_id}")
async def unpublish_expert(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """Unpublish a community expert."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    success = await svc.unpublish_expert(
        source_id,
        item_id,
        operator_id="manager",
        operator_name="Manager",
    )
    if not success:
        raise HTTPException(status_code=404, detail="Expert not found")
    return {"success": True}


@router.post("/market/experts/{item_id}/install")
async def install_expert(
    item_id: str,
    req: ExpertInstallRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """用户接收一个专家到当前 Agent Profile。"""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )
    try:
        return await request.app.state.marketplace.install_expert(
            source_id,
            item_id,
            x_user_id,
            req.agent_id,
            x_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/market/experts/{item_id}/distribute",
    response_model=ExpertDistributionResponse,
)
async def distribute_expert(
    item_id: str,
    req: ExpertDistributionRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """管理员静默分发并覆盖已接收专家。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    try:
        return await request.app.state.marketplace.distribute_expert(
            source_id,
            item_id,
            x_user_id or "manager",
            req,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/market/experts/{item_id}/distributions",
    response_model=list[DistributionRecord],
)
async def get_expert_distributions(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """查询当前实际持有专家副本的用户（管理员）。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    try:
        return await request.app.state.marketplace.get_expert_distributions(
            source_id,
            item_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/market/experts/{item_id}/recall",
    response_model=ExpertRecallResponse,
)
async def recall_expert(
    item_id: str,
    req: ExpertRecallRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """管理员按社区 item_id 撤回已接收副本。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    try:
        return await request.app.state.marketplace.recall_expert(
            source_id,
            item_id,
            x_user_id or "manager",
            req.target_user_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
