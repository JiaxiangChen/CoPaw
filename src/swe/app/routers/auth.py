# -*- coding: utf-8 -*-
"""Authentication API endpoints."""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..agent_context import get_agent_for_request
from ..crons.auth_state import (
    append_user_profile_from_cookie,
    cleanup_cron_auth_except_source,
    extract_access_token_from_cookie,
    get_auth_snapshot,
    resolve_auth_token_for_execution,
    save_user_info_from_access_token,
    sync_identity_envs_from_cookie,
)
from ..auth import (
    authenticate,
    has_registered_users,
    is_auth_enabled,
    register_user,
    update_credentials,
    verify_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str


class RegisterRequest(BaseModel):
    username: str
    password: str


class AuthStatusResponse(BaseModel):
    enabled: bool
    has_users: bool


@router.post("/login")
async def login(req: LoginRequest):
    """Authenticate with username and password."""
    if not is_auth_enabled():
        return LoginResponse(token="", username="")

    token = authenticate(req.username, req.password)
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return LoginResponse(token=token, username=req.username)


@router.post("/register")
async def register(req: RegisterRequest):
    """Register the single user account (only allowed once)."""
    env_flag = os.environ.get("SWE_AUTH_ENABLED", "").strip().lower()
    if env_flag not in ("true", "1", "yes"):
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    if has_registered_users():
        raise HTTPException(
            status_code=403,
            detail="User already registered",
        )

    if not req.username.strip() or not req.password.strip():
        raise HTTPException(
            status_code=400,
            detail="Username and password are required",
        )

    token = register_user(req.username.strip(), req.password)
    if token is None:
        raise HTTPException(
            status_code=409,
            detail="Registration failed",
        )

    return LoginResponse(token=token, username=req.username.strip())


@router.get("/status")
async def auth_status():
    """Check if authentication is enabled and whether a user exists."""
    return AuthStatusResponse(
        enabled=is_auth_enabled(),
        has_users=has_registered_users(),
    )


@router.get("/verify")
async def verify(request: Request):
    """Verify that the caller's Bearer token is still valid."""
    if not is_auth_enabled():
        return {"valid": True, "username": ""}

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="No token provided")

    username = verify_token(token)
    if username is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        )

    return {"valid": True, "username": username}


class UpdateProfileRequest(BaseModel):
    current_password: str
    new_username: str | None = None
    new_password: str | None = None


class CronAuthConfigureRequest(BaseModel):
    cookie: str


class CronAuthTokenResponse(BaseModel):
    token: str
    cookie: str
    expires_at: datetime
    reused: bool


class CronAuthCleanupRequest(BaseModel):
    keep_source_id: str = "RMASSIST"
    force_delete_tenant_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False


@router.post("/cron-auth")
async def configure_cron_auth(
    req: CronAuthConfigureRequest,
    request: Request,
):
    """Configure cron auth state for the current workspace."""
    cookie_header = req.cookie.strip()
    if not cookie_header:
        raise HTTPException(status_code=400, detail="cookie is required")

    try:
        access_token = extract_access_token_from_cookie(cookie_header)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    workspace = await get_agent_for_request(request)
    await save_user_info_from_access_token(
        access_token,
        cookie_header=cookie_header,
        tenant_id=workspace.tenant_id,
        workspace_dir=workspace.workspace_dir,
    )

    # 追加用户身份信息到PROFILE.md
    append_user_profile_from_cookie(cookie_header, workspace.workspace_dir)

    try:
        env_synced_keys = sync_identity_envs_from_cookie(
            cookie_header,
            tenant_id=workspace.tenant_id,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to sync identity envs: {exc}",
        ) from exc
    snapshot = get_auth_snapshot(
        tenant_id=workspace.tenant_id,
        workspace_dir=workspace.workspace_dir,
    )

    return {
        "configured": snapshot.configured,
        "user_info_status": "refreshed",
        "user_info_expires_at": snapshot.user_info_expires_at,
        "auth_token_expires_at": snapshot.auth_token_expires_at,
        "has_auth_token": snapshot.has_auth_token,
        "env_synced_keys": env_synced_keys,
    }


@router.post(
    "/cron-auth/refresh-token",
    response_model=CronAuthTokenResponse,
)
async def refresh_cron_auth_token(
    request: Request,
    response: Response,
) -> CronAuthTokenResponse:
    """Return a reusable or freshly issued cron auth token and cookie."""
    workspace = await get_agent_for_request(request)
    try:
        resolved = await resolve_auth_token_for_execution(
            tenant_id=workspace.tenant_id,
            workspace_dir=workspace.workspace_dir,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if (
        not resolved.token
        or not resolved.cookie_header
        or not resolved.expires_at
    ):
        raise HTTPException(
            status_code=409,
            detail="cron auth is not configured",
        )

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return CronAuthTokenResponse(
        token=resolved.token,
        cookie=resolved.cookie_header,
        expires_at=resolved.expires_at,
        reused=resolved.reused,
    )


@router.post("/cron-auth/cleanup")
async def cleanup_cron_auth(req: CronAuthCleanupRequest):
    """手动清理非指定来源租户的 cron 授权状态文件。"""
    keep_source_id = req.keep_source_id.strip()
    try:
        result = await cleanup_cron_auth_except_source(
            keep_source_id=keep_source_id,
            force_delete_tenant_ids=req.force_delete_tenant_ids,
            dry_run=req.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "dry_run": result.dry_run,
        "keep_source_id": keep_source_id,
        "deleted_count": len(result.deleted_tenant_ids),
        "kept_count": len(result.kept_tenant_ids),
        "missing_count": len(result.missing_tenant_ids),
        "deleted_tenant_ids": result.deleted_tenant_ids,
        "deleted_dirs": result.deleted_dirs,
        "forced_deleted_tenant_ids": result.forced_deleted_tenant_ids,
        "force_delete_tenant_ids": req.force_delete_tenant_ids,
        "kept_tenant_ids": result.kept_tenant_ids,
        "missing_tenant_ids": result.missing_tenant_ids,
    }


@router.post("/update-profile")
async def update_profile(req: UpdateProfileRequest, request: Request):
    """Update username and/or password for the authenticated user."""
    if not is_auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Authentication is not enabled",
        )

    if not has_registered_users():
        raise HTTPException(
            status_code=403,
            detail="No user registered",
        )

    # Verify caller is authenticated
    auth_header = request.headers.get("Authorization", "")
    caller_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not caller_token or verify_token(caller_token) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not req.new_username and not req.new_password:
        raise HTTPException(
            status_code=400,
            detail="Nothing to update",
        )

    if req.new_username is not None and not req.new_username.strip():
        raise HTTPException(
            status_code=400,
            detail="Username cannot be empty",
        )

    if req.new_password is not None and not req.new_password.strip():
        raise HTTPException(
            status_code=400,
            detail="Password cannot be empty",
        )

    token = update_credentials(
        current_password=req.current_password,
        new_username=req.new_username,
        new_password=req.new_password,
    )
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Current password is incorrect",
        )

    username = req.new_username.strip() if req.new_username else ""
    return LoginResponse(token=token, username=username)
