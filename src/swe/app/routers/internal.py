# -*- coding: utf-8 -*-
"""Internal API for service-to-service communication."""

import asyncio
import base64
import json
from datetime import datetime, timezone
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, Request
from fastapi import UploadFile
from pydantic import BaseModel, Field

from ..b3_headers import build_b3_dispatch_meta
from ..async_tasks.db import get_or_create_async_task_db
from ..async_tasks.store import AsyncTaskStore
from ..identity_resolver import resolve_user_identity
from ...config.context import (
    is_valid_identity_value,
    resolve_runtime_tenant_id,
    resolve_scope_id,
    resolve_storage_tenant_id,
)
from ...config.scope_conversion import (
    decode_canonical_scope_id,
    encode_canonical_scope_id,
)
from ...config.utils import list_all_tenant_ids
from ...constant import WORKING_DIR
from ..crons.manager import (
    broadcast_dispatch_intents_enabled,
    dispatch_intents_runtime_enabled,
    is_batch_dispatch_managed_broadcast_child,
)
from ..workspace.bootstrap_state import SourceTemplateUnavailable
from ..workspace.source_template_provisioner import SourceTemplateProvisioner

router = APIRouter(prefix="/internal", tags=["internal"])
public_router = APIRouter(prefix="/assets", tags=["assets"])
logger = logging.getLogger(__name__)

# 内部服务认证 Token（可选）
_INTERNAL_TOKEN = os.environ.get("SWE_INTERNAL_TOKEN", "")
_ASSET_ROOT_DIRNAME = "asset"
_DEFAULT_FILE_URL_BASE = "http://localhost:8088"
_STATIC_AGENT_ID = "default"
_INVALID_FILE_NAME_DETAIL = "Invalid file_name"
_ASSET_NOT_FOUND_DETAIL = "Asset file not found"
_ASSET_INVALID_UTF8_DETAIL = "Asset file is not valid UTF-8"
_CONTENT_INVALID_UTF8_DETAIL = "Content is not valid UTF-8"
_INVALID_PREVIEW_TARGET_DETAIL = "Invalid preview target"
_DISPATCH_CALLBACK_SOURCE = "dispatch_service"
_CALLBACK_EXECUTION_IDENTITY_FIELDS = (
    "cron_execution_key",
    "execution_key",
    "scheduled_fire_at",
    "fire_time",
    "trigger_time",
    "fireTime",
    "triggerTime",
    "external_execution_id",
    "execution_id",
    "log_id",
    "logId",
)
_PREVIEW_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>文件正在生成中</title>
  <style>
    :root {
      color-scheme: light;
      font-family:
        -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #eef2f7;
      color: #172033;
    }

    * {
      box-sizing: border-box;
    }

    body {
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      padding: 32px;
      background:
        radial-gradient(circle at top left, #dbeafe 0, transparent 34%),
        linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%);
    }

    main {
      width: min(100%, 520px);
      padding: 44px 40px;
      border: 1px solid rgba(148, 163, 184, 0.28);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.92);
      box-shadow: 0 24px 70px rgba(15, 23, 42, 0.12);
      text-align: center;
    }

    .loader {
      width: 52px;
      height: 52px;
      margin: 0 auto 24px;
      border: 4px solid #dbeafe;
      border-top-color: #2563eb;
      border-radius: 999px;
      animation: spin 1s linear infinite;
    }

    h1 {
      margin: 0 0 12px;
      font-size: 28px;
      font-weight: 700;
      line-height: 1.25;
      letter-spacing: 0;
    }

    p {
      margin: 0;
      color: #475569;
      font-size: 16px;
      line-height: 1.8;
    }

    @keyframes spin {
      to {
        transform: rotate(360deg);
      }
    }
  </style>
</head>
<body>
  <main>
    <div class="loader" aria-hidden="true"></div>
    <h1>文件正在生成中</h1>
    <p>内容准备完成后，页面会自动展示最新预览。</p>
  </main>
</body>
</html>
"""


def _list_runtime_tenant_ids() -> list[str]:
    """返回参与运行态维护的租户列表，显式排除模板目录。"""
    tenant_ids = list_all_tenant_ids()
    return [
        tenant_id
        for tenant_id in tenant_ids
        if tenant_id == "default" or not tenant_id.startswith("default_")
    ]


async def _get_cron_job_for_dispatch(mgr: Any, job_id: str) -> Any | None:
    getter = getattr(mgr, "get_job", None)
    if getter is None:
        return None
    return await getter(job_id)


def _job_dispatch_intents_enabled(job: Any | None) -> bool:
    if not dispatch_intents_runtime_enabled():
        return False
    if is_batch_dispatch_managed_broadcast_child(job):
        return False
    return broadcast_dispatch_intents_enabled(job)


def _is_dispatch_service_callback(params: dict[str, Any]) -> bool:
    return str(params.get("callback_source") or "").strip() == (
        _DISPATCH_CALLBACK_SOURCE
    )


def _build_dispatch_callback_meta(
    params: dict[str, Any],
) -> dict[str, Any] | None:
    context = _dispatch_callback_context(params)
    if _is_dispatch_service_callback(params):
        intent_id, batch_id, dispatch_attempt = _require_dispatch_callback_ids(
            params,
        )
        metadata = {
            "source": _DISPATCH_CALLBACK_SOURCE,
            "intent_id": intent_id,
            "batch_id": batch_id,
            "dispatch_attempt": dispatch_attempt,
            **context,
            **(_callback_execution_meta(params) or {}),
        }
        workflow_version = _safe_int(params.get("workflow_config_version"))
        if workflow_version > 0:
            metadata["workflow_config_version"] = workflow_version
        return metadata
    return _callback_execution_meta(params)


def _require_dispatch_callback_ids(
    params: dict[str, Any],
) -> tuple[int, str, int]:
    intent_id = _safe_int(
        params.get("dispatch_intent_id") or params.get("intent_id"),
    )
    batch_id = str(
        params.get("dispatch_batch_id") or params.get("batch_id") or "",
    )
    dispatch_attempt = _safe_int(params.get("dispatch_attempt", 1))
    _validate_dispatch_callback_ids(intent_id, batch_id, dispatch_attempt)
    return intent_id, batch_id, dispatch_attempt


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _validate_dispatch_callback_ids(
    intent_id: int,
    batch_id: str,
    dispatch_attempt: int,
) -> None:
    if not intent_id or not batch_id.strip():
        raise HTTPException(
            status_code=400,
            detail="dispatch_service callback requires dispatch_intent_id and dispatch_batch_id",
        )
    if dispatch_attempt <= 0:
        raise HTTPException(
            status_code=400,
            detail="dispatch_service callback requires positive dispatch_attempt",
        )


def _dispatch_callback_value(
    params: dict[str, Any],
    *keys: str,
    default: str = "",
) -> str:
    for key in keys:
        value = params.get(key)
        if value:
            return str(value)
    return default


def _dispatch_callback_context(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": _dispatch_callback_value(params, "tenant_id"),
        "source_id": _dispatch_callback_value(params, "source_id"),
        "scope_id": _dispatch_callback_value(params, "scope_id", "scopeId"),
        "from_id": _dispatch_callback_value(params, "from_id", "fromId"),
        "agent_id": _dispatch_callback_value(
            params,
            "agent_id",
            default=_STATIC_AGENT_ID,
        ),
        "job_id": _dispatch_callback_value(params, "job_id"),
        "parent_scheduled_fire_at": _dispatch_callback_value(
            params,
            "parent_scheduled_fire_at",
        ),
        "provider_id": _dispatch_callback_value(
            params,
            "provider_id",
            default="default",
        ),
        "model_id": _dispatch_callback_value(
            params,
            "model_id",
            default="default",
        ),
    }


def _callback_execution_meta(params: dict[str, Any]) -> dict[str, Any] | None:
    metadata = {
        "cron_execution_key": _dispatch_callback_value(
            params,
            "cron_execution_key",
            "execution_key",
        ),
        "scheduled_fire_at": _dispatch_callback_value(
            params,
            "scheduled_fire_at",
            "fire_time",
            "trigger_time",
            "fireTime",
            "triggerTime",
        ),
        "external_execution_id": _dispatch_callback_value(
            params,
            "external_execution_id",
            "execution_id",
            "log_id",
            "logId",
        ),
    }
    return {key: value for key, value in metadata.items() if value} or None


def _has_callback_execution_identity(dispatch_meta: dict[str, Any]) -> bool:
    """Match the CronManager scheduled execution identity contract."""
    if dispatch_meta.get("cron_execution_key") or dispatch_meta.get(
        "execution_key",
    ):
        return True
    if dispatch_meta.get("intent_id") and dispatch_meta.get("batch_id"):
        return True
    return bool(
        dispatch_meta.get("scheduled_fire_at")
        or dispatch_meta.get("parent_scheduled_fire_at")
        or dispatch_meta.get("external_execution_id"),
    )


def _decode_cron_callback_params(body: Dict[str, Any]) -> dict[str, Any]:
    job_param = body.get("jobParam") or body.get("job_param") or ""
    if not job_param:
        return body
    try:
        params = json.loads(base64.urlsafe_b64decode(job_param))
        if not isinstance(params, dict):
            raise ValueError("jobParam payload must be an object")
        for key in _CALLBACK_EXECUTION_IDENTITY_FIELDS:
            if body.get(key):
                params[key] = body[key]
        return params
    except Exception as exc:
        logger.warning("Failed to decode jobParam: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid jobParam: {exc}",
        )


def _require_cron_callback_params(
    params: dict[str, Any],
) -> tuple[str, Any, str, str, str]:
    try:
        return (
            params["tenant_id"],
            params.get("source_id"),
            params["agent_id"],
            params["task_type"],
            params.get("job_id", ""),
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required param in callback body: {exc}",
        )


def _require_source_scheduler(
    request: Request,
    task_type: str,
    source_id: Any,
) -> Any:
    if not source_id:
        raise HTTPException(
            status_code=400,
            detail=f"source_id required for task_type={task_type}",
        )
    source_scheduler = getattr(
        request.app.state,
        "source_system_task_scheduler",
        None,
    )
    if source_scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="Source system task scheduler not available",
        )
    return source_scheduler


async def _run_source_callback(
    request: Request,
    task_type: str,
    source_id: Any,
) -> bool:
    if task_type not in {"cleanup", "archive_maintenance"}:
        return False
    source_scheduler = _require_source_scheduler(request, task_type, source_id)
    if task_type == "cleanup":
        result = await source_scheduler.run_task_session_cleanup(
            source_id=source_id,
        )
        logger.info("Source task session cleanup result: %s", result)
    else:
        result = await source_scheduler.run_archive_maintenance(
            source_id=source_id,
        )
        logger.info("Source archive maintenance result: %s", result)
    return True


async def _require_callback_cron_manager(
    request: Request,
    tenant_id: str,
    source_id: Any,
    agent_id: str,
) -> Any:
    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        logger.warning("MultiAgentManager not initialized")
        raise HTTPException(status_code=503, detail="Manager not available")
    runtime_tenant_id = (
        resolve_runtime_tenant_id(tenant_id, source_id) or tenant_id
    )
    mgr = await _get_cron_manager(manager, runtime_tenant_id, agent_id)
    if mgr is None:
        raise HTTPException(status_code=404, detail="CronManager not found")
    return mgr


def _batch_callback_skip_response(
    job: Any | None,
    params: dict[str, Any],
    tenant_id: str,
    agent_id: str,
    task_type: str,
    job_id: str,
) -> dict[str, str] | None:
    if _is_dispatch_service_callback(params):
        return None
    if is_batch_dispatch_managed_broadcast_child(job):
        logger.info(
            "Callback skipped for batch-managed broadcast child: "
            "tenant=%s agent=%s job=%s",
            tenant_id,
            agent_id,
            job_id,
        )
        return {
            "status": "ok",
            "task_type": task_type,
            "skipped": "batch_managed_child",
        }
    if _job_dispatch_intents_enabled(job):
        logger.info(
            "Callback skipped for batch-managed broadcast parent: "
            "tenant=%s agent=%s job=%s",
            tenant_id,
            agent_id,
            job_id,
        )
        return {
            "status": "ok",
            "task_type": task_type,
            "skipped": "batch_managed_external_callback",
        }
    return None


async def _run_job_callback(
    request: Request,
    mgr: Any,
    params: dict[str, Any],
    tenant_id: str,
    source_id: Any,
    agent_id: str,
    task_type: str,
    job_id: str,
) -> dict[str, str] | None:
    if not job_id:
        raise HTTPException(
            status_code=400,
            detail="job_id required for task_type=job",
        )
    job = await _get_cron_job_for_dispatch(mgr, job_id)
    skip_response = _batch_callback_skip_response(
        job,
        params,
        tenant_id,
        agent_id,
        task_type,
        job_id,
    )
    if skip_response is not None:
        return skip_response

    dispatch_meta = _build_dispatch_callback_meta(params) or {}
    if getattr(job, "task_type", None) in {
        "agent",
        "text",
        "workflow",
    } and not _has_callback_execution_identity(dispatch_meta):
        dispatch_meta["cron_execution_key"] = f"legacy:{uuid.uuid4()}"
    dispatch_meta.update(
        build_b3_dispatch_meta(getattr(request, "headers", {})),
    )
    run_kwargs = {"is_manual": False, "source_id": source_id}
    if dispatch_meta:
        run_kwargs["dispatch_meta"] = dispatch_meta
    try:
        result = await mgr.run_job(job_id, **run_kwargs)
    except KeyError as exc:
        if exc.args != (f"Job not found: {job_id}",):
            raise
        logger.info(
            "Callback skipped for missing job: "
            "tenant=%s source=%s agent=%s job=%s",
            tenant_id,
            source_id,
            agent_id,
            job_id,
        )
        return {"status": "ok", "skipped": "job_not_found", "job_id": job_id}
    if result is False:
        return {"status": "ok", "skipped": "job_disabled", "job_id": job_id}
    return None


async def _run_agent_callback(
    request: Request,
    params: dict[str, Any],
    tenant_id: str,
    source_id: Any,
    agent_id: str,
    task_type: str,
    job_id: str,
) -> dict[str, str] | None:
    mgr = await _require_callback_cron_manager(
        request,
        tenant_id,
        source_id,
        agent_id,
    )
    if task_type == "heartbeat":
        await mgr.run_heartbeat()
        return None
    if task_type == "dream":
        await mgr.run_dream()
        return None
    return await _run_job_callback(
        request,
        mgr,
        params,
        tenant_id,
        source_id,
        agent_id,
        task_type,
        job_id,
    )


class InternalErrorResponse(BaseModel):
    detail: str = Field(..., description="Error detail message.")


class InternalTextAssetReadResponse(BaseModel):
    success: bool = Field(default=True)
    file_name: str
    content: str


class InternalTextAssetWriteRequest(BaseModel):
    user_id: str
    source_id: str
    content: str
    preview_url: Optional[str] = None


class InternalTextAssetWriteResponse(BaseModel):
    success: bool = Field(default=True)
    file_name: str
    scope_id: str
    public_url: str


class InternalAssetUploadResponse(BaseModel):
    """内部 asset 上传响应。"""

    success: bool = Field(default=True)
    file_name: str
    asset_path: str
    size: int


class InternalTextAssetPreviewPathRequest(BaseModel):
    user_id: str
    source_id: str
    file_name: Optional[str] = None


class InternalTextAssetPreviewPathResponse(BaseModel):
    success: bool = Field(default=True)
    file_name: str
    scope_id: str
    public_url: str
    static_path: str


class InternalScopeEncodeItem(BaseModel):
    tenant_id: str
    source_id: str
    scope_id: str


class InternalScopeEncodeResponse(BaseModel):
    success: bool = Field(default=True)
    item: Optional[InternalScopeEncodeItem] = None
    items: Optional[list[InternalScopeEncodeItem]] = None


class InternalScopeDecodeItem(BaseModel):
    scope_id: str
    tenant_id: str
    source_id: str


class InternalScopeDecodeResponse(BaseModel):
    success: bool = Field(default=True)
    item: Optional[InternalScopeDecodeItem] = None
    items: Optional[list[InternalScopeDecodeItem]] = None


class InternalBatchInitializeTenantsRequest(BaseModel):
    """批量初始化租户请求。"""

    tenant_ids: str = Field(..., description="逗号分隔的租户 ID 字符串")
    source_id: str = Field(..., min_length=1, description="来源标识")
    enable_bootstrap_chat: bool = Field(
        default=True,
        description="是否为新租户保留 BOOTSTRAP.md 初始化聊天",
    )
    fail_fast: bool = Field(
        default=False,
        description="单个租户失败时是否立即终止后续处理",
    )


class InternalBatchInitializeTenantResult(BaseModel):
    """单个租户初始化结果。"""

    tenant_id: str
    tenant_name: Optional[str] = None
    bbk_id: Optional[str] = None
    status: str
    message: str


class InternalBatchInitializeTenantsResponse(BaseModel):
    """批量初始化租户响应。"""

    success: bool
    task_id: Optional[str] = None
    status: Optional[str] = None
    total: int
    success_count: int
    fail_count: int
    results: list[InternalBatchInitializeTenantResult] = Field(
        default_factory=list,
    )


class InternalEnsureSourceTemplateRequest(BaseModel):
    """Request for explicitly provisioning a source template."""

    source_id: str = Field(..., min_length=1)


class InternalEnsureSourceTemplateResponse(BaseModel):
    """Safe source-template provisioning result."""

    source_id: str
    template_name: str
    status: str


def _verify_internal_token(token: Optional[str]) -> None:
    """验证内部服务 Token（如果配置了的话）."""
    if _INTERNAL_TOKEN:
        if not token or token != f"Bearer {_INTERNAL_TOKEN}":
            raise HTTPException(status_code=401, detail="Unauthorized")


def _http_400(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _parse_batch_tenant_ids(raw_tenant_ids: str) -> list[str]:
    """解析批量租户字符串并去重。"""
    tenant_ids: list[str] = []
    seen: set[str] = set()
    for raw_tenant_id in raw_tenant_ids.split(","):
        tenant_id = raw_tenant_id.strip()
        if not tenant_id:
            continue
        if not is_valid_identity_value(tenant_id):
            raise _http_400(f"Invalid tenant_id: {tenant_id}")
        if tenant_id in seen:
            continue
        seen.add(tenant_id)
        tenant_ids.append(tenant_id)
    if not tenant_ids:
        raise _http_400("tenant_ids must not be empty")
    return tenant_ids


async def _request_db_connection(request: Request):
    """读取或懒加载当前请求绑定的数据库连接。"""
    return await get_or_create_async_task_db(request)


def _request_actor(request: Request) -> tuple[str, str]:
    """从请求头解析操作人信息，缺省保持为空。"""
    actor_id = (request.headers.get("X-User-Id") or "").strip()
    actor_name = unquote(request.headers.get("X-User-Name") or "").strip()
    return actor_id, actor_name


async def _make_async_task_store(request: Request) -> AsyncTaskStore:
    """创建统一异步任务写入器。"""
    db_connection = await _request_db_connection(request)
    if db_connection is None:
        raise HTTPException(
            status_code=503,
            detail="Async task database connection is not available",
        )
    return AsyncTaskStore(db_connection)


def _require_internal_token(
    authorization: Optional[str],
    x_internal_token: Optional[str],
) -> None:
    _verify_internal_token(authorization or x_internal_token)


@router.post(
    "/source-templates/ensure",
    response_model=InternalEnsureSourceTemplateResponse,
)
async def ensure_source_template(
    payload: InternalEnsureSourceTemplateRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_internal_token: Optional[str] = Header(None),
) -> InternalEnsureSourceTemplateResponse:
    """Create or repair one source template outside tenant request traffic."""
    _require_internal_token(authorization, x_internal_token)
    if not is_valid_identity_value(payload.source_id):
        raise _http_400("Invalid source_id")
    try:
        result = await SourceTemplateProvisioner(WORKING_DIR).ensure(
            payload.source_id,
        )
    except SourceTemplateUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Source template unavailable",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    pool = getattr(request.app.state, "tenant_workspace_pool", None)
    if pool is not None and result.status in {"created", "repaired"}:
        scope = resolve_storage_tenant_id("default", payload.source_id) or (
            f"default_{payload.source_id}"
        )
        await pool.invalidate_bootstrap(scope, reason="source_template_reload")
    return InternalEnsureSourceTemplateResponse(
        source_id=result.source_id,
        template_name=result.template_name,
        status=result.status,
    )


def _encode_scope_items_from_body(
    body: Dict[str, Any],
) -> tuple[tuple[Any, ...], bool]:
    tenant_id = body.get("tenant_id")
    source_id = body.get("source_id")
    items = body.get("items")
    has_single_fields = tenant_id is not None or source_id is not None
    has_batch_items = items is not None

    if has_single_fields == has_batch_items:
        raise _http_400("Expected either tenant_id/source_id or items")

    try:
        if has_batch_items:
            if not isinstance(items, list) or not items:
                raise _http_400("items must not be empty")
            encoded_items = []
            for item in items:
                if not isinstance(item, dict):
                    raise _http_400("Each item must be an object")
                encoded_items.append(
                    encode_canonical_scope_id(
                        str(item.get("tenant_id", "")),
                        str(item.get("source_id", "")),
                    ),
                )
            return tuple(encoded_items), False

        if tenant_id is None or source_id is None:
            raise _http_400(
                "tenant_id and source_id must be provided together",
            )
        return (
            (encode_canonical_scope_id(str(tenant_id), str(source_id)),),
            True,
        )
    except ValueError as exc:
        raise _http_400(str(exc)) from exc


def _decode_scope_items_from_body(
    body: Dict[str, Any],
) -> tuple[tuple[Any, ...], bool]:
    scope_id = body.get("scope_id")
    scope_ids = body.get("scope_ids")
    has_single_scope = scope_id is not None
    has_batch_scopes = scope_ids is not None

    if has_single_scope == has_batch_scopes:
        raise _http_400("Expected either scope_id or scope_ids")

    try:
        if has_batch_scopes:
            if not isinstance(scope_ids, list) or not scope_ids:
                raise _http_400("scope_ids must not be empty")
            return (
                tuple(
                    decode_canonical_scope_id(str(raw_scope_id))
                    for raw_scope_id in scope_ids
                ),
                False,
            )

        return ((decode_canonical_scope_id(str(scope_id)),), True)
    except ValueError as exc:
        raise _http_400(str(exc)) from exc


async def _run_internal_batch_initialize_task(
    *,
    task_id: str,
    store: AsyncTaskStore,
    pool: Any,
    payload: InternalBatchInitializeTenantsRequest,
    tenant_ids: list[str],
    headers: dict[str, str],
) -> None:
    """后台执行批量租户初始化，并同步统一异步任务表。"""
    try:
        await store.mark_running(task_id)
    except Exception as exc:  # pylint: disable=broad-except
        error_message = str(exc)
        logger.warning(
            "Failed to mark tenant bootstrap task running: task_id=%s",
            task_id,
            exc_info=True,
        )
        for tenant_id in tenant_ids:
            try:
                await store.record_item_result(
                    task_id=task_id,
                    target_id=tenant_id,
                    success=False,
                    item_status="failed",
                    error_message=error_message,
                )
            except Exception:
                logger.warning(
                    "Failed to record tenant bootstrap item failure: task_id=%s tenant_id=%s",
                    task_id,
                    tenant_id,
                    exc_info=True,
                )
        try:
            await store.finish_task(
                task_id=task_id,
                status="failed",
                done_count=0,
                failed_count=len(tenant_ids),
                error_message=error_message,
            )
        except Exception:
            logger.warning(
                "Failed to finish tenant bootstrap task: task_id=%s",
                task_id,
                exc_info=True,
            )
        return
    success_count = 0
    fail_count = 0
    for tenant_id in tenant_ids:
        try:
            resolved_identity = await resolve_user_identity(
                tenant_id=tenant_id,
                source_id=payload.source_id,
                user_name=None,
                bbk_id=None,
                headers=headers,
                allow_remote_lookup=True,
            )
            if not resolved_identity.user_name or not resolved_identity.bbk_id:
                fail_count += 1
                await store.record_item_result(
                    task_id=task_id,
                    target_id=tenant_id,
                    success=False,
                    item_status="failed",
                    result={
                        "tenant_id": tenant_id,
                        "tenant_name": resolved_identity.user_name,
                        "bbk_id": resolved_identity.bbk_id,
                        "status": "failed",
                        "message": "user identity not resolved",
                    },
                    error_message="user identity not resolved",
                )
                if payload.fail_fast:
                    break
                continue

            outcome = await pool.ensure_bootstrap(
                tenant_id,
                source_id=payload.source_id,
                tenant_name=resolved_identity.user_name,
                bbk_id=resolved_identity.bbk_id,
                enable_bootstrap_chat=payload.enable_bootstrap_chat,
            )
            item_status = (
                "skipped" if outcome.status == "already_ready" else "created"
            )
            success_count += 1
            await store.record_item_result(
                task_id=task_id,
                target_id=tenant_id,
                success=True,
                item_status=item_status,
                result={
                    "tenant_id": tenant_id,
                    "tenant_name": resolved_identity.user_name,
                    "bbk_id": resolved_identity.bbk_id,
                    "status": item_status,
                    "message": item_status,
                },
            )
        except Exception as exc:  # pylint: disable=broad-except
            fail_count += 1
            await store.record_item_result(
                task_id=task_id,
                target_id=tenant_id,
                success=False,
                item_status="failed",
                result={
                    "tenant_id": tenant_id,
                    "status": "failed",
                    "message": str(exc),
                },
                error_message=str(exc),
            )
            if payload.fail_fast:
                break

    status = "succeeded"
    if fail_count > 0 and success_count > 0:
        status = "partial_failed"
    elif fail_count > 0:
        status = "failed"
    await store.finish_task(
        task_id=task_id,
        status=status,
        done_count=success_count + fail_count,
        failed_count=fail_count,
        result=None,
    )


def _validate_asset_file_name(file_name: str) -> str:
    path = Path(file_name)
    has_reserved_name = not file_name or file_name in {".", ".."}
    has_path_component = path.name != file_name or path.is_absolute()
    has_forbidden_character = any(
        char in file_name for char in ("/", "\\", "\x00")
    )
    if has_reserved_name or has_path_component or has_forbidden_character:
        raise HTTPException(
            status_code=400,
            detail=_INVALID_FILE_NAME_DETAIL,
        )
    return file_name


def _decode_utf8_content(raw_content: bytes) -> str:
    try:
        return raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=_ASSET_INVALID_UTF8_DETAIL,
        ) from exc


def _validate_utf8_text(content: str) -> str:
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=_CONTENT_INVALID_UTF8_DETAIL,
        ) from exc
    return content


def _generate_text_asset_file_name(user_id: str) -> str:
    timestamp = datetime.now(tz=timezone.utc).strftime(
        "%Y%m%d%H%M%S%f",
    )[:-3]
    return f"{user_id}-{timestamp}.html"


def _build_static_path(scope_id: str, file_name: str) -> str:
    return f"/static/{scope_id}/{_STATIC_AGENT_ID}/{file_name}"


def _build_public_url(scope_id: str, file_name: str) -> str:
    base_url = os.getenv("FILE_URL", _DEFAULT_FILE_URL_BASE).rstrip("/")
    return f"{base_url}{_build_static_path(scope_id, file_name)}"


def _get_asset_file_path(file_name: str) -> Path:
    return WORKING_DIR / _ASSET_ROOT_DIRNAME / file_name


def _build_asset_path(file_name: str) -> str:
    return f"{_ASSET_ROOT_DIRNAME}/{file_name}"


def _get_scope_static_dir(scope_id: str) -> Path:
    return WORKING_DIR / scope_id / "workspaces" / _STATIC_AGENT_ID / "static"


def _validate_text_asset_identity(user_id: str, source_id: str) -> str:
    if not is_valid_identity_value(user_id):
        raise HTTPException(status_code=400, detail="Invalid user_id")
    if not is_valid_identity_value(source_id):
        raise HTTPException(status_code=400, detail="Invalid source_id")

    scope_id = resolve_scope_id(user_id, source_id)
    if scope_id is None:
        raise HTTPException(status_code=400, detail="Failed to resolve scope")
    return scope_id


def _validate_preview_file_name(file_name: str) -> str:
    safe_file_name = _validate_asset_file_name(file_name)
    if not safe_file_name.endswith(".html"):
        raise HTTPException(
            status_code=400,
            detail=_INVALID_PREVIEW_TARGET_DETAIL,
        )
    return safe_file_name


def _resolve_static_target(
    static_dir: Path,
    file_name: str,
) -> Path:
    try:
        resolved_static_dir = static_dir.resolve()
        target = (resolved_static_dir / file_name).resolve()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=_INVALID_PREVIEW_TARGET_DETAIL,
        ) from exc

    if target.parent != resolved_static_dir:
        raise HTTPException(
            status_code=400,
            detail=_INVALID_PREVIEW_TARGET_DETAIL,
        )
    return target


def _parse_preview_target(preview_url: str, expected_scope_id: str) -> str:
    parsed = urlsplit(preview_url.strip())
    raw_path = parsed.path if parsed.scheme or parsed.netloc else preview_url
    path = unquote(raw_path)
    parts = path.split("/")
    if (
        len(parts) != 5
        or parts[0] != ""
        or parts[1] != "static"
        or parts[2] != expected_scope_id
        or parts[3] != _STATIC_AGENT_ID
    ):
        raise HTTPException(
            status_code=400,
            detail=_INVALID_PREVIEW_TARGET_DETAIL,
        )

    file_name = parts[4]
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise HTTPException(
            status_code=400,
            detail=_INVALID_PREVIEW_TARGET_DETAIL,
        )
    try:
        return _validate_preview_file_name(file_name)
    except HTTPException as exc:
        raise HTTPException(
            status_code=400,
            detail=_INVALID_PREVIEW_TARGET_DETAIL,
        ) from exc


def _normalize_preview_file_name(
    file_name: Optional[str],
    user_id: str,
) -> str:
    if file_name is None:
        return _generate_text_asset_file_name(user_id)

    safe_file_name = _validate_asset_file_name(file_name)
    path = Path(file_name)
    if path.suffix:
        return safe_file_name
    return f"{safe_file_name}.html"


def _read_text_asset(file_name: str) -> InternalTextAssetReadResponse:
    safe_file_name = _validate_asset_file_name(file_name)
    asset_file = _get_asset_file_path(safe_file_name)
    if not asset_file.exists() or not asset_file.is_file():
        raise HTTPException(status_code=404, detail=_ASSET_NOT_FOUND_DETAIL)
    content = _decode_utf8_content(asset_file.read_bytes())
    return InternalTextAssetReadResponse(
        file_name=safe_file_name,
        content=content,
    )


async def _save_uploaded_asset_file(
    file: UploadFile,
    *,
    template_flag: Optional[str] = None,
) -> InternalAssetUploadResponse:
    safe_file_name = _validate_asset_file_name(file.filename or "")
    content = await file.read()
    asset_dir = WORKING_DIR / _ASSET_ROOT_DIRNAME
    asset_dir.mkdir(parents=True, exist_ok=True)
    asset_file = _get_asset_file_path(safe_file_name)
    asset_file.write_bytes(content)

    asset_path = _build_asset_path(safe_file_name)
    file_size = len(content)

    try:
        from ..asset_upload_record.router import get_service

        service = get_service()
        await service.create_record(
            file_name=safe_file_name,
            file_size=file_size,
            asset_path=asset_path,
            template_flag=template_flag,
        )
    except Exception:
        logger.warning(
            "Failed to persist upload record for file: %s",
            safe_file_name,
            exc_info=True,
        )

    return InternalAssetUploadResponse(
        file_name=safe_file_name,
        asset_path=asset_path,
        size=file_size,
    )


def _write_text_asset(
    payload: InternalTextAssetWriteRequest,
) -> InternalTextAssetWriteResponse:
    scope_id = _validate_text_asset_identity(
        payload.user_id,
        payload.source_id,
    )
    file_name = (
        _parse_preview_target(payload.preview_url, scope_id)
        if payload.preview_url is not None
        else _generate_text_asset_file_name(payload.user_id)
    )
    content = _validate_utf8_text(payload.content)
    static_dir = _get_scope_static_dir(scope_id)
    static_dir.mkdir(parents=True, exist_ok=True)
    target = _resolve_static_target(static_dir, file_name)
    target.write_text(content, encoding="utf-8")

    return InternalTextAssetWriteResponse(
        file_name=file_name,
        scope_id=scope_id,
        public_url=_build_public_url(scope_id, file_name),
    )


def _create_preview_path(
    payload: InternalTextAssetPreviewPathRequest,
) -> InternalTextAssetPreviewPathResponse:
    scope_id = _validate_text_asset_identity(
        payload.user_id,
        payload.source_id,
    )
    file_name = _validate_preview_file_name(
        _normalize_preview_file_name(payload.file_name, payload.user_id),
    )
    static_dir = _get_scope_static_dir(scope_id)
    static_dir.mkdir(parents=True, exist_ok=True)
    target = _resolve_static_target(static_dir, file_name)
    if target.exists():
        target.unlink()
    target.write_text(_PREVIEW_PLACEHOLDER_HTML, encoding="utf-8")

    return InternalTextAssetPreviewPathResponse(
        file_name=file_name,
        scope_id=scope_id,
        public_url=_build_public_url(scope_id, file_name),
        static_path=_build_static_path(scope_id, file_name),
    )


@router.post(
    "/assets/text/preview-path",
    response_model=InternalTextAssetPreviewPathResponse,
    responses={
        400: {"model": InternalErrorResponse},
        401: {"model": InternalErrorResponse},
    },
)
async def internal_create_text_asset_preview_path(
    payload: InternalTextAssetPreviewPathRequest,
    x_internal_token: Optional[str] = Header(
        default=None,
        alias="X-Internal-Token",
    ),
) -> InternalTextAssetPreviewPathResponse:
    _verify_internal_token(x_internal_token)
    return _create_preview_path(payload)


@router.post(
    "/scope/encode",
    response_model=InternalScopeEncodeResponse,
    response_model_exclude_none=True,
    responses={
        400: {"model": InternalErrorResponse},
        401: {"model": InternalErrorResponse},
    },
)
async def internal_scope_encode(
    body: Dict[str, Any] = Body(...),
) -> InternalScopeEncodeResponse:
    encoded_items, is_single = _encode_scope_items_from_body(body)
    response_items = [
        InternalScopeEncodeItem(
            tenant_id=item.tenant_id,
            source_id=item.source_id,
            scope_id=item.scope_id,
        )
        for item in encoded_items
    ]
    if is_single:
        return InternalScopeEncodeResponse(item=response_items[0])
    return InternalScopeEncodeResponse(items=response_items)


@router.post(
    "/scope/decode",
    response_model=InternalScopeDecodeResponse,
    response_model_exclude_none=True,
    responses={
        400: {"model": InternalErrorResponse},
        401: {"model": InternalErrorResponse},
    },
)
async def internal_scope_decode(
    body: Dict[str, Any] = Body(...),
) -> InternalScopeDecodeResponse:
    decoded_items, is_single = _decode_scope_items_from_body(body)
    response_items = [
        InternalScopeDecodeItem(
            scope_id=item.scope_id,
            tenant_id=item.tenant_id,
            source_id=item.source_id,
        )
        for item in decoded_items
    ]
    if is_single:
        return InternalScopeDecodeResponse(item=response_items[0])
    return InternalScopeDecodeResponse(items=response_items)


@router.post(
    "/tenants/batch-initialize",
    response_model=InternalBatchInitializeTenantsResponse,
    responses={
        400: {"model": InternalErrorResponse},
        503: {"model": InternalErrorResponse},
    },
)
async def internal_batch_initialize_tenants(
    payload: InternalBatchInitializeTenantsRequest,
    request: Request,
) -> InternalBatchInitializeTenantsResponse:
    """公开批量补齐身份并初始化租户，不校验内部服务 Token。"""

    if not is_valid_identity_value(payload.source_id):
        raise _http_400("Invalid source_id")

    tenant_ids = _parse_batch_tenant_ids(payload.tenant_ids)

    pool = getattr(request.app.state, "tenant_workspace_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="Tenant pool not available",
        )

    task_id = str(uuid.uuid4())
    store = await _make_async_task_store(request)
    actor_user_id, actor_user_name = _request_actor(request)
    await store.start_task(
        task_id=task_id,
        service="swe",
        task_type="tenant.bootstrap",
        source_id=payload.source_id,
        actor_user_id=actor_user_id,
        actor_user_name=actor_user_name,
        target_ids=tenant_ids,
    )
    asyncio.create_task(
        _run_internal_batch_initialize_task(
            task_id=task_id,
            store=store,
            pool=pool,
            payload=payload,
            tenant_ids=tenant_ids,
            headers={
                key: value
                for key, value in {
                    "Content-Type": "application/json",
                    "Authorization": request.headers.get("Authorization"),
                }.items()
                if value
            },
        ),
    )
    return InternalBatchInitializeTenantsResponse(
        success=True,
        task_id=task_id,
        status="queued",
        total=len(tenant_ids),
        success_count=0,
        fail_count=0,
        results=[],
    )


@public_router.post(
    "/upload",
    response_model=InternalAssetUploadResponse,
    responses={
        400: {"model": InternalErrorResponse},
    },
)
async def upload_asset(
    file: UploadFile = File(...),
    template_flag: Optional[str] = Form(None),
) -> InternalAssetUploadResponse:
    """公开上传 asset 文件，不校验内部服务 Token。"""
    return await _save_uploaded_asset_file(file, template_flag=template_flag)


@public_router.get(
    "/text/read",
    response_model=InternalTextAssetReadResponse,
    responses={
        400: {"model": InternalErrorResponse},
        404: {"model": InternalErrorResponse},
    },
)
async def read_text_asset(
    file_name: str,
) -> InternalTextAssetReadResponse:
    return _read_text_asset(file_name)


@public_router.post(
    "/text/write",
    response_model=InternalTextAssetWriteResponse,
    responses={
        400: {"model": InternalErrorResponse},
    },
)
async def write_text_asset(
    payload: InternalTextAssetWriteRequest,
) -> InternalTextAssetWriteResponse:
    return _write_text_asset(payload)


@router.post("/agents/{agent_id}/reload")
async def internal_reload_agent(
    agent_id: str,
    request: Request,
    tenant_id: str = "default",
    source_id: Optional[str] = None,
    x_internal_token: Optional[str] = Header(
        default=None,
        alias="X-Internal-Token",
    ),
):
    """内部服务调用：重载指定 Agent.

    用于 market 服务修改技能配置后通知主服务重载 Agent。
    """
    _verify_internal_token(x_internal_token)

    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        logger.warning("MultiAgentManager not initialized")
        raise HTTPException(status_code=503, detail="Manager not available")

    if not source_id:
        raise HTTPException(status_code=400, detail="source_id is required")
    if not is_valid_identity_value(tenant_id):
        raise HTTPException(status_code=400, detail="Invalid tenant_id")
    if not is_valid_identity_value(source_id):
        raise HTTPException(status_code=400, detail="Invalid source_id")

    scope_id = resolve_scope_id(tenant_id, source_id)
    if scope_id is None:
        raise HTTPException(status_code=400, detail="Failed to resolve scope")

    try:
        await manager.reload_agent(agent_id, tenant_id=scope_id)
        logger.info(
            f"Agent '{agent_id}' (scope={scope_id}) reloaded via internal API",
        )
        return {
            "success": True,
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "source_id": source_id,
            "scope_id": scope_id,
        }
    except Exception as e:
        logger.error(f"Failed to reload agent '{agent_id}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _get_cron_manager(manager, tenant_id: str, agent_id: str):
    """获取指定 tenant/agent 的 CronManager 实例。"""
    try:
        ws = await manager.get_agent(agent_id, tenant_id=tenant_id)
    except ValueError:
        return None
    return ws.cron_manager


def _get_configured_agent_ids(tenant_id: str) -> list[str]:
    """读取指定租户配置中的所有 Agent ID。"""
    from ...config.utils import get_tenant_storage_config_path, load_config

    config = load_config(get_tenant_storage_config_path(tenant_id))
    return sorted(config.agents.profiles.keys())


@router.post("/cron/register-missing-jobs")
async def register_missing_cron_jobs(request: Request):
    """手动补注册所有租户、所有 Agent 中未注册到外部平台的定时任务。"""
    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        logger.warning("MultiAgentManager not initialized")
        raise HTTPException(status_code=503, detail="Manager not available")

    tenant_ids = _list_runtime_tenant_ids()
    summary: dict[str, Any] = {
        "tenant_count": len(tenant_ids),
        "agent_count": 0,
        "total": 0,
        "registered": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "results": [],
        "errors": [],
    }

    for tenant_id in tenant_ids:
        try:
            agent_ids = _get_configured_agent_ids(tenant_id)
        except Exception as exc:  # pylint: disable=broad-except
            summary["failed"] += 1
            summary["errors"].append(
                {
                    "tenant_id": tenant_id,
                    "agent_id": "",
                    "error": str(exc),
                },
            )
            continue

        for agent_id in agent_ids:
            summary["agent_count"] += 1
            mgr = await _get_cron_manager(manager, tenant_id, agent_id)
            if mgr is None:
                summary["failed"] += 1
                summary["errors"].append(
                    {
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "error": "CronManager not found",
                    },
                )
                continue

            try:
                result = await mgr.register_missing_external_jobs()
            except Exception as exc:  # pylint: disable=broad-except
                summary["failed"] += 1
                summary["errors"].append(
                    {
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "error": str(exc),
                    },
                )
                continue

            summary["total"] += int(result.get("total", 0))
            summary["registered"] += int(result.get("registered", 0))
            summary["updated"] += int(result.get("updated", 0))
            summary["skipped"] += int(result.get("skipped", 0))
            summary["failed"] += int(result.get("failed", 0))
            summary["errors"].extend(result.get("errors", []))
            summary["results"].append(result)

    return summary


# ── External cron maintenance endpoints ──


@router.post("/cron/refresh-external-jobs")
async def refresh_external_cron_jobs(request: Request):
    """按当前代码规则刷新所有外部调度平台任务。"""
    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        logger.warning("MultiAgentManager not initialized")
        raise HTTPException(status_code=503, detail="Manager not available")

    tenant_ids = _list_runtime_tenant_ids()
    summary: dict[str, Any] = {
        "tenant_count": len(tenant_ids),
        "agent_count": 0,
        "total": 0,
        "registered": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "results": [],
        "errors": [],
    }

    for tenant_id in tenant_ids:
        try:
            agent_ids = _get_configured_agent_ids(tenant_id)
        except Exception as exc:  # pylint: disable=broad-except
            summary["failed"] += 1
            summary["errors"].append(
                {"tenant_id": tenant_id, "agent_id": "", "error": str(exc)},
            )
            continue

        for agent_id in agent_ids:
            summary["agent_count"] += 1
            mgr = await _get_cron_manager(manager, tenant_id, agent_id)
            if mgr is None:
                summary["failed"] += 1
                summary["errors"].append(
                    {
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "error": "CronManager not found",
                    },
                )
                continue

            try:
                result = await mgr.refresh_external_jobs()
            except Exception as exc:  # pylint: disable=broad-except
                summary["failed"] += 1
                summary["errors"].append(
                    {
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "error": str(exc),
                    },
                )
                continue

            summary["total"] += int(result.get("total", 0))
            summary["registered"] += int(result.get("registered", 0))
            summary["updated"] += int(result.get("updated", 0))
            summary["skipped"] += int(result.get("skipped", 0))
            summary["failed"] += int(result.get("failed", 0))
            summary["errors"].extend(result.get("errors", []))
            summary["results"].append(result)

    return summary


# ── Unified callback endpoint (jobParam-based) ──


@router.post("/cron/callback")
# Existing endpoint handles legacy and jobParam callback shapes in one handler.
# pylint: disable=too-many-statements
async def internal_cron_callback(
    request: Request,
    body: Dict[str, Any] = Body(...),
):
    """外部调度平台统一回调端点。

    支持两种参数传入方式：
    1. jobParam（base64 JSON 包裹）→ 解码后提取参数
    2. body 顶层直接携带 tenant_id / agent_id / task_type / job_id

    根据 task_type 分发到对应的 CronManager 方法。
    不校验内部 token；部署必须将此执行入口隔离在可信内网。
    """
    params = _decode_cron_callback_params(body)
    tenant_id, source_id, agent_id, task_type, job_id = (
        _require_cron_callback_params(params)
    )

    try:
        source_callback_handled = await _run_source_callback(
            request,
            task_type,
            source_id,
        )
        if not source_callback_handled:
            skip_response = await _run_agent_callback(
                request,
                params,
                tenant_id,
                source_id,
                agent_id,
                task_type,
                job_id,
            )
            if skip_response is not None:
                return skip_response
        logger.info(
            "Callback dispatched: type=%s tenant=%s agent=%s job=%s",
            task_type,
            tenant_id,
            agent_id,
            job_id,
        )
        return {"status": "ok", "task_type": task_type}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to run callback (type=%s, tenant=%s, agent=%s, job=%s): %s",
            task_type,
            tenant_id,
            agent_id,
            job_id,
            e,
        )
        raise HTTPException(status_code=500, detail=str(e))
