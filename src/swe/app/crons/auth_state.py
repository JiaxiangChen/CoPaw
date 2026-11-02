# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Coroutine, Mapping, TypeVar

from pydantic import BaseModel, Field
from fastapi import HTTPException
from .auth_mirror import mirror_delete, mirror_upsert
from ...agents.memory.agent_md_manager import AgentMdManager
from ...constant import WORKING_DIR
from ...config.context import decode_scope_id
from ...config.utils import get_tenant_secrets_dir
from ...envs.store import load_envs, save_envs
from ...utils.tools import (
    get_auth_token,
    get_user_info,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

CRON_AUTH_FILE_NAME = "cron_auth.json"
DEFAULT_USER_INFO_TTL = timedelta(days=7)
DEFAULT_AUTH_TOKEN_TTL = timedelta(hours=2)
USER_INFO_REFRESH_MARGIN = timedelta(days=5)
AUTH_TOKEN_REUSE_MIN_REMAINING = timedelta(minutes=30)
ACCESS_TOKEN_COOKIE_NAME = "com.cmb.dw.rtl.sso.token"
IDENTITY_ENV_COOKIE_NAMES = {
    "bbkOrgId": ("com.cmb.dw.rtl.sso.vbbk", "vbbk"),
    "brnOrgId": ("com.cmb.dw.rtl.sso.vorgcode", "vorgcode"),
    "sapId": ("com.cmb.dw.rtl.sso.userid", "userid"),
    "rtlPstId": ("com.cmb.dw.rtl.sso.positionID", "positionID"),
}


class CronAuthState(BaseModel):
    user_info: dict[str, Any] = Field(default_factory=dict)
    user_info_expires_at: datetime | None = None
    user_info_refreshed_at: datetime | None = None
    auth_token: str | None = None
    auth_token_expires_at: datetime | None = None
    cookie_header: str | None = None
    last_prefetch_at: datetime | None = None
    last_error: str | None = None


@dataclass
class CronUserInfoEnsureResult:
    state: CronAuthState
    reused: bool


@dataclass
class ResolvedAuthToken:
    token: str | None
    expires_at: datetime | None
    reused: bool
    cookie_header: str | None = None


@dataclass
class CronAuthSnapshot:
    configured: bool
    user_info_expires_at: datetime | None
    auth_token_expires_at: datetime | None
    has_auth_token: bool


@dataclass
class CronAuthCleanupResult:
    deleted_tenant_ids: list[str]
    deleted_dirs: list[str]
    forced_deleted_tenant_ids: list[str]
    kept_tenant_ids: list[str]
    missing_tenant_ids: list[str]
    dry_run: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _prepare_secret_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path.parent, 0o700)


def _iter_cookie_pairs(cookie_header: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in cookie_header.split(";"):
        item = part.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        if not sep:
            continue
        pairs.append((name.strip(), value.strip()))
    return pairs


def extract_access_token_from_cookie(cookie_header: str) -> str:
    for name, value in _iter_cookie_pairs(cookie_header):
        if name == ACCESS_TOKEN_COOKIE_NAME and value:
            return value
    raise ValueError(
        f"cron auth cookie missing {ACCESS_TOKEN_COOKIE_NAME}",
    )


def sync_identity_envs_from_cookie(
    cookie_header: str,
    *,
    tenant_id: str | None = None,
) -> list[str]:
    """Incrementally persist identity fields from a cron-auth cookie."""
    cookies = dict(_iter_cookie_pairs(cookie_header))
    updates: dict[str, str] = {}
    for env_key, cookie_names in IDENTITY_ENV_COOKIE_NAMES.items():
        for cookie_name in cookie_names:
            value = cookies.get(cookie_name, "").strip()
            if value:
                updates[env_key] = value
                break

    if not updates:
        return []

    envs_path = get_tenant_secrets_dir(tenant_id) / "envs.json"
    envs = load_envs(envs_path)
    envs.update(updates)
    save_envs(envs, envs_path)
    return sorted(updates)


def append_user_profile_from_cookie(
    cookie_header: str,
    workspace_dir: Path,
) -> None:
    """
    Append user profile from cookie to cron auth state.
    """
    try:
        # 解析cookie为字符串
        cookies = {}
        for item in cookie_header.split(";"):
            item = item.strip()
            if "=" in item:
                key, value = item.split("=", 1)
                cookies[key.strip()] = value.strip()
        # 提取所需字段，根据cookie名称判断是否需要更新
        branch_id = cookies.get("com.cmb.dw.rtl.sso.vbbk", "未知")
        vorgcode = cookies.get("com.cmb.dw.rtl.sso.vorgcode", "未知")
        position_id = cookies.get("com.cmb.dw.rtl.sso.positionID", "未知")
        user_id = cookies.get("com.cmb.dw.rtl.sso.userid", "未知")
        if branch_id == "V00":
            return
        # 拼接text
        text = (
            f"\n###用户身份信息\n"
            f"分行号：{branch_id}\n"
            f"网点机构编号：{vorgcode}\n"
            f"岗位编号：{position_id}\n"
            f"客户经理ID：{user_id}\n"
        )

        # 追加到PROFILE.md
        workspace_manager = AgentMdManager(str(workspace_dir))
        workspace_manager.append_working_md("PROFILE.md", text)

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to append user profile from cookie: {str(exc)}",
        ) from exc


def merge_auth_token_into_cookie(
    cookie_header: str | None,
    auth_token: str,
) -> str:
    if not cookie_header:
        return f"{ACCESS_TOKEN_COOKIE_NAME}={auth_token}"

    merged_parts: list[str] = []
    replaced = False
    for name, value in _iter_cookie_pairs(cookie_header):
        if name == ACCESS_TOKEN_COOKIE_NAME:
            merged_parts.append(f"{ACCESS_TOKEN_COOKIE_NAME}={auth_token}")
            replaced = True
            continue
        merged_parts.append(f"{name}={value}")

    if not replaced:
        merged_parts.append(f"{ACCESS_TOKEN_COOKIE_NAME}={auth_token}")
    return "; ".join(merged_parts)


def get_cron_auth_file_path(
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> Path:
    _ = workspace_dir
    return get_tenant_secrets_dir(tenant_id) / CRON_AUTH_FILE_NAME


def _scope_from_tenant_dir_name(
    tenant_dir_name: str,
) -> tuple[str | None, str | None]:
    try:
        tenant_id, source_id = decode_scope_id(tenant_dir_name)
        return tenant_id, source_id
    except ValueError:
        pass

    legacy_prefix = "default_"
    if tenant_dir_name.startswith(legacy_prefix):
        return None, tenant_dir_name[len(legacy_prefix) :] or None
    return None, None


def _source_id_from_tenant_dir_name(tenant_dir_name: str) -> str | None:
    _tenant_id, source_id = _scope_from_tenant_dir_name(tenant_dir_name)
    return source_id


def _matches_force_delete_tenant(
    tenant_dir_name: str,
    force_delete_tenant_ids: set[str],
) -> bool:
    if not force_delete_tenant_ids:
        return False
    if tenant_dir_name in force_delete_tenant_ids:
        return True

    tenant_id, _source_id = _scope_from_tenant_dir_name(tenant_dir_name)
    return tenant_id in force_delete_tenant_ids if tenant_id else False


async def cleanup_cron_auth_except_source(
    *,
    keep_source_id: str = "RMASSIST",
    force_delete_tenant_ids: list[str] | set[str] | None = None,
    working_dir: str | Path | None = None,
    dry_run: bool = False,
) -> CronAuthCleanupResult:
    """删除非指定来源租户目录下的 cron 授权状态文件。"""
    keep_source_id = keep_source_id.strip()
    if not keep_source_id:
        raise ValueError("keep_source_id is required")
    force_delete_tenants = {
        tenant_id.strip()
        for tenant_id in (force_delete_tenant_ids or [])
        if tenant_id.strip()
    }

    base_dir = Path(working_dir) if working_dir is not None else WORKING_DIR
    candidates = await asyncio.to_thread(_scan_cron_auth_cleanup, base_dir)

    deleted_tenant_ids: list[str] = []
    deleted_dirs: list[str] = []
    forced_deleted_tenant_ids: list[str] = []
    kept_tenant_ids: list[str] = []
    missing_tenant_ids: list[str] = []

    for tenant_dir, auth_path, has_auth_file in candidates:
        if not has_auth_file:
            missing_tenant_ids.append(tenant_dir.name)
            continue

        force_delete = _matches_force_delete_tenant(
            tenant_dir.name,
            force_delete_tenants,
        )
        if (
            _source_id_from_tenant_dir_name(tenant_dir.name) == keep_source_id
            and not force_delete
        ):
            kept_tenant_ids.append(tenant_dir.name)
            continue

        if not dry_run:
            await _await_cron_auth_mutation(_delete_and_mirror(auth_path))
        deleted_tenant_ids.append(tenant_dir.name)
        deleted_dirs.append(str(tenant_dir))
        if force_delete:
            forced_deleted_tenant_ids.append(tenant_dir.name)

    return CronAuthCleanupResult(
        deleted_tenant_ids=deleted_tenant_ids,
        deleted_dirs=deleted_dirs,
        forced_deleted_tenant_ids=forced_deleted_tenant_ids,
        kept_tenant_ids=kept_tenant_ids,
        missing_tenant_ids=missing_tenant_ids,
        dry_run=dry_run,
    )


def _scan_cron_auth_cleanup(
    base_dir: Path,
) -> list[tuple[Path, Path, bool]]:
    if not base_dir.exists():
        return []
    candidates = []
    for tenant_dir in sorted(base_dir.iterdir(), key=lambda item: item.name):
        if not tenant_dir.is_dir() or tenant_dir.name.startswith("."):
            continue
        auth_path = tenant_dir / ".secret" / CRON_AUTH_FILE_NAME
        candidates.append((tenant_dir, auth_path, auth_path.is_file()))
    return candidates


def load_cron_auth_state(
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> CronAuthState:
    path = get_cron_auth_file_path(
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    logger.info("load_cron_auth_state workspace_dir")
    print(path)
    if not path.is_file():
        return CronAuthState()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return CronAuthState.model_validate(data)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "Failed to load cron auth state from %s: %s",
            path,
            exc,
        )
        return CronAuthState(last_error=f"load_failed: {exc}")


def _save_cron_auth_file(
    state: CronAuthState,
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> Path:
    path = get_cron_auth_file_path(
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    _prepare_secret_parent(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            state.model_dump(mode="json", exclude_none=True),
            fh,
            ensure_ascii=False,
            indent=2,
        )
    _chmod_best_effort(path, 0o600)
    return path


async def save_cron_auth_state(
    state: CronAuthState,
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> Path:
    return await _await_cron_auth_mutation(
        _save_and_mirror(
            state.model_copy(deep=True),
            tenant_id=tenant_id,
            workspace_dir=workspace_dir,
        ),
    )


async def _save_and_mirror(
    snapshot: CronAuthState,
    *,
    tenant_id: str | None,
    workspace_dir: str | Path | None,
) -> Path:
    path = await asyncio.to_thread(
        _save_cron_auth_file,
        snapshot,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    await mirror_upsert(path.parent.parent.name, snapshot)
    return path


async def _delete_and_mirror(path: Path) -> None:
    await asyncio.to_thread(path.unlink)
    await mirror_delete(path.parent.parent.name)


async def _await_cron_auth_mutation(work: Coroutine[Any, Any, _T]) -> _T:
    """Own a started file mutation and its mirror through caller cancellation."""
    task = asyncio.create_task(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


def _parse_expire_time(raw: Any, default_ttl: timedelta) -> datetime:
    now = utc_now()
    if isinstance(raw, datetime):
        return ensure_utc(raw) or (now + default_ttl)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            return ensure_utc(datetime.fromisoformat(raw)) or (
                now + default_ttl
            )
        except ValueError:
            pass
    return now + default_ttl


def _normalize_user_info_payload(
    payload: Any,
) -> tuple[dict[str, Any], datetime]:
    if isinstance(payload, Mapping):
        data = dict(payload)
        exp = _parse_expire_time(data.get("exp"), DEFAULT_USER_INFO_TTL)
        user_info = data.get("userInfo", data)
        if isinstance(user_info, Mapping):
            return dict(user_info), exp
        return {"value": user_info}, exp
    return {"value": payload}, utc_now() + DEFAULT_USER_INFO_TTL


def _normalize_auth_token_payload(auth_token: str) -> str:
    try:
        payload = json.loads(auth_token)
    except (TypeError, ValueError):
        return auth_token
    if (
        isinstance(payload, Mapping)
        and set(payload.keys()) == {"value"}
        and isinstance(payload["value"], str)
    ):
        return payload["value"]
    return auth_token


def _raise_if_user_info_expired(state: CronAuthState) -> None:
    if not state.user_info:
        return
    expires_at = ensure_utc(state.user_info_expires_at)
    print("user_info expires_at", expires_at)
    now = utc_now()
    if expires_at is not None and expires_at <= now:
        raise ValueError("cron auth user_info is expired")


async def save_user_info_from_access_token(
    access_token: str,
    *,
    cookie_header: str | None = None,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> CronAuthState:
    payload = await asyncio.to_thread(get_user_info, access_token)
    user_info, expires_at = _normalize_user_info_payload(payload)
    state = await asyncio.to_thread(
        load_cron_auth_state,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    state.user_info = user_info
    state.user_info_expires_at = expires_at
    state.user_info_refreshed_at = utc_now()
    state.auth_token = None
    state.auth_token_expires_at = None
    if cookie_header is not None:
        state.cookie_header = cookie_header
    state.last_error = None
    await save_cron_auth_state(
        state,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    return state


async def ensure_user_info_from_access_token(
    access_token: str,
    *,
    cookie_header: str | None = None,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
    min_remaining: timedelta = USER_INFO_REFRESH_MARGIN,
) -> CronUserInfoEnsureResult:
    state = await asyncio.to_thread(
        load_cron_auth_state,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    expires_at = ensure_utc(state.user_info_expires_at)
    now = utc_now()
    if (
        state.user_info
        and expires_at is not None
        and expires_at - now > min_remaining
    ):
        if cookie_header is not None:
            state.cookie_header = cookie_header
            await save_cron_auth_state(
                state,
                tenant_id=tenant_id,
                workspace_dir=workspace_dir,
            )
        return CronUserInfoEnsureResult(
            state=state,
            reused=True,
        )
    return CronUserInfoEnsureResult(
        state=await save_user_info_from_access_token(
            access_token,
            cookie_header=cookie_header,
            tenant_id=tenant_id,
            workspace_dir=workspace_dir,
        ),
        reused=False,
    )


def require_valid_user_info(
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> CronAuthState:
    state = load_cron_auth_state(
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    if not state.user_info:
        raise ValueError("cron auth user_info is not configured")

    _raise_if_user_info_expired(state)
    return state


def refresh_user_info_if_needed(
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
    min_remaining: timedelta = USER_INFO_REFRESH_MARGIN,
) -> CronAuthState:
    return require_valid_user_info(
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )


def _is_auth_token_reusable(
    state: CronAuthState,
    *,
    min_remaining: timedelta = AUTH_TOKEN_REUSE_MIN_REMAINING,
) -> bool:
    expires_at = ensure_utc(state.auth_token_expires_at)
    if not state.auth_token or expires_at is None:
        return False
    return expires_at - utc_now() > min_remaining


async def issue_auth_token(
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> ResolvedAuthToken:
    state = await asyncio.to_thread(
        require_valid_user_info,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    if not state.user_info:
        raise ValueError("cron auth user_info is not configured")

    auth_token = _normalize_auth_token_payload(
        await asyncio.to_thread(get_auth_token, state.user_info),
    )
    expires_at = utc_now() + DEFAULT_AUTH_TOKEN_TTL
    cookie_header = merge_auth_token_into_cookie(
        state.cookie_header,
        auth_token,
    )
    state.auth_token = auth_token
    state.auth_token_expires_at = expires_at
    state.cookie_header = cookie_header
    state.last_error = None
    await save_cron_auth_state(
        state,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    return ResolvedAuthToken(
        token=auth_token,
        expires_at=expires_at,
        reused=False,
        cookie_header=cookie_header,
    )


async def prefetch_auth_token(
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> ResolvedAuthToken:
    state = await asyncio.to_thread(
        load_cron_auth_state,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    if _is_auth_token_reusable(state, min_remaining=timedelta(0)):
        state.last_prefetch_at = utc_now()
        state.last_error = None
        await save_cron_auth_state(
            state,
            tenant_id=tenant_id,
            workspace_dir=workspace_dir,
        )
        return ResolvedAuthToken(
            token=state.auth_token,
            expires_at=(
                ensure_utc(state.auth_token_expires_at)
                or (utc_now() + DEFAULT_AUTH_TOKEN_TTL)
            ),
            reused=True,
            cookie_header=merge_auth_token_into_cookie(
                state.cookie_header,
                state.auth_token or "",
            ),
        )

    resolved = await issue_auth_token(
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    state = await asyncio.to_thread(
        load_cron_auth_state,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    state.last_prefetch_at = utc_now()
    state.last_error = None
    await save_cron_auth_state(
        state,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    return resolved


async def resolve_auth_token_for_execution(
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> ResolvedAuthToken:
    state = await asyncio.to_thread(
        load_cron_auth_state,
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    _raise_if_user_info_expired(state)

    if not state.user_info:
        return ResolvedAuthToken(
            token=None,
            expires_at=None,
            reused=False,
            cookie_header=None,
        )

    if _is_auth_token_reusable(state):
        return ResolvedAuthToken(
            token=state.auth_token,
            expires_at=(
                ensure_utc(state.auth_token_expires_at)
                or (utc_now() + DEFAULT_AUTH_TOKEN_TTL)
            ),
            reused=True,
            cookie_header=merge_auth_token_into_cookie(
                state.cookie_header,
                state.auth_token or "",
            ),
        )
    return await issue_auth_token(
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )


def get_auth_snapshot(
    *,
    tenant_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> CronAuthSnapshot:
    state = load_cron_auth_state(
        tenant_id=tenant_id,
        workspace_dir=workspace_dir,
    )
    return CronAuthSnapshot(
        configured=bool(state.user_info),
        user_info_expires_at=ensure_utc(state.user_info_expires_at),
        auth_token_expires_at=ensure_utc(
            state.auth_token_expires_at,
        ),
        has_auth_token=bool(state.auth_token),
    )
