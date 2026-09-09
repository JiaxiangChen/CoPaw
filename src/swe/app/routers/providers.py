# -*- coding: utf-8 -*-
"""API routes for LLM providers and models."""

from __future__ import annotations

import asyncio
import inspect
import logging
import shutil
import time
import uuid
from pathlib import Path as PathlibPath
from typing import Any, List, Literal, Optional, Sequence
from copy import deepcopy
from urllib.parse import unquote

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
)
from pydantic import BaseModel, ConfigDict, Field

from ...config.context import (
    get_current_effective_tenant_id,
    resolve_storage_tenant_id,
)
from ...config.utils import (
    SECRET_DIR,
    migrate_legacy_scope_dir_if_needed,
    get_tenant_storage_working_dir,
    get_tenant_working_dir_strict,
    list_logical_tenant_ids,
)
from ...providers.models import ModelSlotConfig
from ...providers.provider import (
    ModelInfo,
    ModelRuntimeConfig,
    ProviderInfo,
    ReasoningEffort,
)
from ...providers.provider_manager import ActiveModelsInfo, ProviderManager
from ..async_tasks import AsyncTaskStore
from ..async_tasks.db import get_or_create_async_task_db
from ..identity_resolver import resolve_user_identity
from ..workspace.tenant_initializer import TenantInitializer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


_PROVIDER_API_SLOW_LOG_MS = 500

ChatModelName = Literal[
    "OpenAIChatModel",
    "KimiChatModel",
    "AnthropicChatModel",
    "GeminiChatModel",
]

# effective: agent-specific if set, otherwise global
# global: the global model only, ignoring any agent-specific setting
# agent: a specific agent's model only, error if not set
ActiveModelReadScope = Literal["effective", "global", "agent"]
ActiveModelWriteScope = Literal["global", "agent"]


async def get_provider_manager(request: Request) -> ProviderManager:
    """Get the tenant-specific provider manager.

    Ensures tenant provider storage is initialized before returning the manager.
    This lazy-initializes provider storage on first provider API use. Cached
    managers stay on the async hot path so quick model-list requests do not
    queue behind unrelated sync work in AnyIO's threadpool.

    Args:
        request: FastAPI request object

    Returns:
        ProviderManager instance for the current tenant.

    Raises:
        HTTPException: If tenant ID is not available in request context.
    """
    started_at = time.perf_counter()
    resolve_started_at = started_at
    tenant_id = _get_effective_tenant_id(request)
    resolve_ms = int((time.perf_counter() - resolve_started_at) * 1000)

    if tenant_id is None:
        # For exempt routes or backward compatibility, use default tenant
        tenant_id = "default"
        logger.debug("No tenant ID in request, using default tenant")

    provider_tenant_id = ProviderManager._resolve_effective_provider_tenant_id(
        tenant_id,
    )
    cached_instances = (
        ProviderManager._instances
        if isinstance(ProviderManager._instances, dict)
        else {}
    )
    cache_hit_before = provider_tenant_id in cached_instances
    root_path = ProviderManager._get_tenant_root_path(provider_tenant_id)
    request.state.provider_manager_dependency_threadpool_wait_ms = 0
    logger.info(
        "provider_manager_dependency_start path=%s route_tenant_id=%s "
        "provider_tenant_id=%s source_id=%s scope_id=%s cache_hit_before=%s "
        "root_path=%s",
        request.url.path,
        tenant_id,
        provider_tenant_id,
        _request_source_id(request),
        getattr(request.state, "scope_id", None),
        cache_hit_before,
        root_path,
    )

    cached_manager = cached_instances.get(provider_tenant_id)
    if cached_manager is not None:
        await _await_if_needed(cached_manager.refresh_if_due())
        return _record_provider_manager_dependency_done(
            request=request,
            started_at=started_at,
            resolve_ms=resolve_ms,
            ensure_ms=0,
            get_instance_ms=0,
            threadpool_wait_ms=0,
            tenant_id=tenant_id,
            provider_tenant_id=provider_tenant_id,
            manager=cached_manager,
            cache_hit_before=cache_hit_before,
            root_path=root_path,
            storage_ensure_skipped=True,
        )

    manager = await _await_if_needed(
        ProviderManager.get_or_create_instance(tenant_id),
    )
    await _await_if_needed(manager.refresh_if_due())
    return _record_provider_manager_dependency_done(
        request=request,
        started_at=started_at,
        resolve_ms=resolve_ms,
        ensure_ms=0,
        get_instance_ms=int((time.perf_counter() - started_at) * 1000),
        threadpool_wait_ms=0,
        tenant_id=tenant_id,
        provider_tenant_id=provider_tenant_id,
        manager=manager,
        cache_hit_before=cache_hit_before,
        root_path=root_path,
        storage_ensure_skipped=False,
    )


def _record_provider_manager_dependency_done(
    *,
    request: Request,
    started_at: float,
    resolve_ms: int,
    ensure_ms: int,
    get_instance_ms: int,
    threadpool_wait_ms: int,
    tenant_id: str,
    provider_tenant_id: str,
    manager: ProviderManager,
    cache_hit_before: bool,
    root_path: PathlibPath,
    storage_ensure_skipped: bool,
) -> ProviderManager:
    if storage_ensure_skipped:
        logger.info(
            "provider_storage_ensure_done path=%s route_tenant_id=%s "
            "provider_tenant_id=%s duration_ms=0 root_path=%s skipped=True "
            "reason=provider_manager_cache_hit",
            request.url.path,
            tenant_id,
            provider_tenant_id,
            root_path,
        )
        logger.info(
            "provider_manager_get_instance_done path=%s route_tenant_id=%s "
            "provider_tenant_id=%s manager_tenant_id=%s duration_ms=0 "
            "cache_hit_after=%s root_path=%s skipped=True "
            "reason=provider_manager_cache_hit",
            request.url.path,
            tenant_id,
            provider_tenant_id,
            manager.tenant_id,
            manager.tenant_id in ProviderManager._instances,
            root_path,
        )

    total_ms = int((time.perf_counter() - started_at) * 1000)
    request.state.provider_manager_dependency_ms = total_ms
    request.state.provider_manager_dependency_done_at = time.perf_counter()
    request.state.provider_manager_dependency_ensure_ms = ensure_ms
    request.state.provider_manager_dependency_get_instance_ms = get_instance_ms
    request.state.provider_manager_dependency_threadpool_wait_ms = (
        threadpool_wait_ms
    )
    request.state.provider_manager_dependency_cache_hit_before = (
        cache_hit_before
    )

    if total_ms >= _PROVIDER_API_SLOW_LOG_MS:
        logger.info(
            "provider_manager_dependency_slow path=%s total_ms=%d "
            "resolve_ms=%d ensure_ms=%d get_instance_ms=%d "
            "threadpool_wait_ms=%d "
            "route_tenant_id=%s provider_tenant_id=%s manager_tenant_id=%s "
            "source_id=%s scope_id=%s cache_hit_before=%s "
            "cache_hit_after=%s root_path=%s root_exists=%s",
            request.url.path,
            total_ms,
            resolve_ms,
            ensure_ms,
            get_instance_ms,
            threadpool_wait_ms,
            tenant_id,
            provider_tenant_id,
            manager.tenant_id,
            _request_source_id(request),
            getattr(request.state, "scope_id", None),
            cache_hit_before,
            manager.tenant_id in ProviderManager._instances,
            root_path,
            root_path.exists(),
        )

    return manager


class ProviderConfigRequest(BaseModel):
    api_key: Optional[str] = Field(default=None)
    base_url: Optional[str] = Field(default=None)
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Chat model class name for protocol selection",
    )


class ModelSlotRequest(BaseModel):
    provider_id: str = Field(..., description="Provider to use")
    model: str = Field(..., description="Model identifier")
    scope: ActiveModelWriteScope = Field(
        ...,
        description="Whether to update the global model or a specific agent",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="Target agent ID when scope is 'agent'",
    )


class CreateCustomProviderRequest(BaseModel):
    id: str = Field(...)
    name: str = Field(...)
    default_base_url: str = Field(default="")
    api_key_prefix: str = Field(default="")
    chat_model: ChatModelName = Field(default="OpenAIChatModel")
    models: List[ModelInfo] = Field(default_factory=list)


class AddModelRequest(BaseModel):
    id: str = Field(...)
    name: str = Field(...)


class ModelRuntimeConfigUpdate(BaseModel):
    """Partial model runtime configuration update."""

    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0)
    max_input_length: int | None = Field(default=None, gt=0)
    max_output_length: int | None = Field(default=None, gt=0)
    supports_enable_thinking: bool | None = None
    supported_reasoning_efforts: list[ReasoningEffort] | None = None
    enable_thinking: bool | None = None
    reasoning_effort: ReasoningEffort | None = None


def _validate_model_slot(
    manager: ProviderManager,
    provider_id: str,
    model_id: str,
) -> None:
    """Validate that the provider and model exist without mutating state."""
    provider = manager.get_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found.",
        )
    if not provider.has_model(model_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_id}' not found in provider "
                f"'{provider_id}'."
            ),
        )


def _request_tenant_id(request: Request) -> str | None:
    return getattr(request.state, "tenant_id", None)


def _request_tenant_working_dir(request: Request):
    return get_tenant_working_dir_strict(_get_effective_tenant_id(request))


def _request_tenant_storage_working_dir(request: Request):
    """获取当前请求的 storage 语义工作目录。"""
    return get_tenant_storage_working_dir(_get_effective_tenant_id(request))


def _request_source_id(request: Request) -> str | None:
    return getattr(request.state, "source_id", None)


def _request_actor(request: Request) -> tuple[str, str]:
    """从请求头解析操作人信息，缺省保持为空。"""
    actor_id = (request.headers.get("X-User-Id") or "").strip()
    actor_name = unquote(request.headers.get("X-User-Name") or "").strip()
    return actor_id, actor_name


def _get_effective_tenant_id(request: Request) -> str | None:
    """从请求上下文获取 storage 语义的有效租户 ID。"""
    return resolve_storage_tenant_id(
        _request_tenant_id(request),
        _request_source_id(request),
        scope_id=getattr(request.state, "scope_id", None),
    )


async def _request_db_connection(request: Request):
    """读取或懒加载异步任务数据库连接。"""
    return await get_or_create_async_task_db(request)


def _get_tenant_storage_providers_dir(tenant_id: str | None = None):
    """获取 storage 语义下的 providers 目录。"""
    resolved_tenant_id = _get_effective_tenant_id_proxy(tenant_id)
    if not resolved_tenant_id:
        resolved_tenant_id = "default"
    return (
        migrate_legacy_scope_dir_if_needed(
            SECRET_DIR,
            resolved_tenant_id,
        )
        / "providers"
    )


def _get_effective_tenant_id_proxy(tenant_id: str | None) -> str | None:
    """在没有 request 时解析 tenant 目录名。"""
    if tenant_id:
        return tenant_id
    return None


def _get_target_storage_providers_dir(tenant_id: str) -> PathlibPath:
    """获取目标租户的 providers 目录。"""
    return _get_tenant_storage_providers_dir(tenant_id)


async def _make_async_task_store(request: Request) -> AsyncTaskStore:
    """创建异步任务写入器。"""
    db_connection = await _request_db_connection(request)
    if db_connection is None:
        raise HTTPException(
            status_code=503,
            detail="Async task database connection is not available",
        )
    return AsyncTaskStore(db_connection)


def _new_async_task_id() -> str:
    """生成统一异步任务 ID。"""
    return str(uuid.uuid4())


def _distribution_summary(kind: str, name: str, target_count: int) -> str:
    """构造包含分发对象的任务摘要。"""
    object_name = str(name or "").strip() or "-"
    return f"分发{kind}「{object_name}」，目标 {target_count} 个用户"


def _active_model_distribution_name(active_model: ModelSlotConfig) -> str:
    """生成活跃模型分发的对象名称。"""
    return f"{active_model.provider_id}/{active_model.model}"


def _providers_distribution_name(source_providers_dir: PathlibPath) -> str:
    """从源 providers 目录提取本次分发的供应商标识。"""
    provider_ids: list[str] = []
    for subdir_name in ("builtin", "custom"):
        provider_dir = source_providers_dir / subdir_name
        if not provider_dir.exists():
            continue
        provider_ids.extend(
            sorted(
                provider_file.stem
                for provider_file in provider_dir.glob("*.json")
                if provider_file.is_file()
            ),
        )
    return ", ".join(provider_ids) or "全部供应商"


async def _resolve_distribution_target_names(
    request: Request,
    target_tenant_ids: list[str],
) -> dict[str, str | None]:
    """解析分发目标的展示名称，失败时回退到目标 ID。"""
    source_id = _request_source_id(request)
    headers = dict(request.headers)

    async def resolve_one(tenant_id: str) -> tuple[str, str | None] | None:
        target_id = str(tenant_id or "").strip()
        if not target_id:
            return None
        try:
            resolved_identity = await resolve_user_identity(
                tenant_id=target_id,
                source_id=source_id,
                user_name=None,
                bbk_id=None,
                headers=headers,
                allow_remote_lookup=True,
            )
            return target_id, resolved_identity.user_name or target_id
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Failed to resolve provider distribution target name: tenant_id=%s",
                target_id,
                exc_info=True,
            )
            return target_id, target_id

    resolved_items = await asyncio.gather(
        *(resolve_one(tenant_id) for tenant_id in target_tenant_ids),
    )
    return {
        target_id: target_name
        for item in resolved_items
        if item is not None
        for target_id, target_name in (item,)
    }


async def _distribute_providers_to_tenant(
    *,
    source_providers_dir: PathlibPath,
    target_tenant_id: str,
    source_working_dir: PathlibPath,
    source_id: str | None,
    tenant_workspace_pool: Any,
) -> ProvidersDistributionTenantResult:
    """分发 providers 目录到单个目标租户。

    Args:
        source_providers_dir: 源租户的 providers 目录路径。
        target_tenant_id: 目标租户 ID。
        source_working_dir: 源租户的工作目录父路径。
        source_id: 租户初始化使用的 source 标识。

    Returns:
        分发结果。
    """
    # 安全校验
    target_tenant_id = _validate_target_tenant_id(target_tenant_id)

    initializer = TenantInitializer(
        source_working_dir.parent,
        target_tenant_id,
        source_id=source_id,
    )
    was_bootstrapped = initializer.has_seeded_bootstrap()
    if not was_bootstrapped:
        await tenant_workspace_pool.ensure_bootstrap(
            target_tenant_id,
            source_id=source_id,
        )

    target_providers_dir = _get_target_storage_providers_dir(
        initializer.effective_tenant_id,
    )

    # Remove existing target directory if exists
    if target_providers_dir.exists():
        shutil.rmtree(target_providers_dir)

    # Copy entire providers directory
    shutil.copytree(source_providers_dir, target_providers_dir)

    return ProvidersDistributionTenantResult(
        tenant_id=target_tenant_id,
        success=True,
        bootstrapped=not was_bootstrapped,
    )


def _validate_target_tenant_id(tenant_id: str) -> str:
    tenant_id = str(tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if len(tenant_id) > 256:
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    if ".." in tenant_id or "/" in tenant_id or "\\" in tenant_id:
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    if any(ord(c) < 32 for c in tenant_id):
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    return tenant_id


def _resolve_distribution_source(
    manager: ProviderManager,
) -> tuple[ModelSlotConfig, dict]:
    active_model = manager.get_active_model()
    if (
        active_model is None
        or not active_model.provider_id
        or not active_model.model
    ):
        raise HTTPException(
            status_code=400,
            detail="No active model configured for the current tenant",
        )

    provider = manager.get_provider(active_model.provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{active_model.provider_id}' not found.",
        )
    if not provider.has_model(active_model.model):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{active_model.model}' not found in provider "
                f"'{active_model.provider_id}'."
            ),
        )

    provider_payload = provider.model_dump()
    model_configs = provider_payload.get("model_configs") or {}
    provider_payload["model_configs"] = (
        {active_model.model: model_configs[active_model.model]}
        if active_model.model in model_configs
        else {}
    )
    return active_model, provider_payload


async def _distribute_active_model_to_tenant(
    *,
    source_working_dir,
    target_tenant_id: str,
    provider_payload: dict,
    source_active_model: ModelSlotConfig,
    source_id: str | None,
    tenant_workspace_pool: Any,
) -> ActiveModelDistributionTenantResult:
    initializer = TenantInitializer(
        source_working_dir.parent,
        target_tenant_id,
        source_id=source_id,
    )
    was_bootstrapped = initializer.has_seeded_bootstrap()
    if not was_bootstrapped:
        await tenant_workspace_pool.ensure_bootstrap(
            target_tenant_id,
            source_id=source_id,
        )

    ProviderManager.ensure_tenant_provider_storage(
        initializer.effective_tenant_id,
    )
    target_manager = ProviderManager.get_instance(
        initializer.effective_tenant_id,
    )
    target_manager.overwrite_provider_payload(provider_payload)
    await target_manager.activate_model(
        source_active_model.provider_id,
        source_active_model.model,
    )
    return ActiveModelDistributionTenantResult(
        tenant_id=target_tenant_id,
        success=True,
        bootstrapped=not was_bootstrapped,
        provider_updated=source_active_model.provider_id,
        active_llm_updated=ModelSlotConfig(
            provider_id=source_active_model.provider_id,
            model=source_active_model.model,
        ),
    )


def _active_model_result_payload(
    result: ActiveModelDistributionTenantResult,
    target_name: str | None = None,
) -> dict:
    """将活跃模型分发结果转为可落库的 JSON 结构。"""
    active_llm = (
        result.active_llm_updated.model_dump()
        if result.active_llm_updated is not None
        else None
    )
    return {
        "tenant_id": result.tenant_id,
        "tenant_name": target_name,
        "bootstrapped": result.bootstrapped,
        "provider_updated": result.provider_updated,
        "active_llm_updated": active_llm,
    }


def _providers_result_payload(
    result: ProvidersDistributionTenantResult,
    target_name: str | None = None,
) -> dict:
    """将 providers 分发结果转为可落库的 JSON 结构。"""
    return {
        "tenant_id": result.tenant_id,
        "tenant_name": target_name,
        "bootstrapped": result.bootstrapped,
    }


async def _safe_record_provider_task_item(
    store: AsyncTaskStore,
    *,
    task_id: str,
    target_id: str,
    success: bool,
    result: dict | None = None,
    error_message: str | None = None,
) -> None:
    """尽力记录分发明细，避免后台任务异常泄漏到事件循环。"""
    try:
        await store.record_item_result(
            task_id=task_id,
            target_id=target_id,
            success=success,
            result=result,
            error_message=error_message,
        )
    except Exception:
        logger.warning(
            "Failed to record provider distribution item: task_id=%s target_id=%s",
            task_id,
            target_id,
            exc_info=True,
        )


async def _safe_finish_provider_task(
    store: AsyncTaskStore,
    **kwargs,
) -> None:
    """尽力汇总任务，防止 create_task 出现未取异常。"""
    try:
        await store.finish_task(**kwargs)
    except Exception:
        logger.warning(
            "Failed to finish provider distribution task: task_id=%s",
            kwargs.get("task_id"),
            exc_info=True,
        )


async def _fail_provider_task_before_running(
    *,
    store: AsyncTaskStore,
    task_id: str,
    target_ids: list[str],
    error_message: str,
) -> None:
    """任务进入运行态前失败时，尽力将所有目标置为失败。"""
    for target_id in target_ids:
        await _safe_record_provider_task_item(
            store,
            task_id=task_id,
            target_id=target_id,
            success=False,
            error_message=error_message,
        )
    await _safe_finish_provider_task(
        store,
        task_id=task_id,
        status="failed",
        done_count=0,
        failed_count=len(target_ids),
        error_message=error_message,
        result={"done": 0, "failed": len(target_ids)},
    )


async def _run_active_model_distribution_task(
    *,
    task_id: str,
    store: AsyncTaskStore,
    source_working_dir: PathlibPath,
    target_tenant_ids: list[str],
    provider_payload: dict,
    source_active_model: ModelSlotConfig,
    source_id: str | None,
    tenant_workspace_pool: Any,
    target_names: dict[str, str | None] | None = None,
) -> None:
    """后台执行活跃模型分发并回写统一任务表。"""
    try:
        await store.mark_running(task_id)
    except Exception as exc:  # pylint: disable=broad-except
        error_message = str(exc)
        logger.warning(
            "Failed to mark active model distribution task running: task_id=%s",
            task_id,
            exc_info=True,
        )
        await _fail_provider_task_before_running(
            store=store,
            task_id=task_id,
            target_ids=target_tenant_ids,
            error_message=error_message,
        )
        return
    done_count = 0
    failed_count = 0
    errors: list[str] = []
    for tenant_id in target_tenant_ids:
        try:
            validated_tenant_id = _validate_target_tenant_id(tenant_id)
            result = await _distribute_active_model_to_tenant(
                source_working_dir=source_working_dir,
                target_tenant_id=validated_tenant_id,
                provider_payload=provider_payload,
                source_active_model=source_active_model,
                source_id=source_id,
                tenant_workspace_pool=tenant_workspace_pool,
            )
            done_count += 1
            await _safe_record_provider_task_item(
                store,
                task_id=task_id,
                target_id=tenant_id,
                success=True,
                result=_active_model_result_payload(
                    result,
                    target_names.get(tenant_id) if target_names else None,
                ),
            )
        except Exception as exc:  # pylint: disable=broad-except
            failed_count += 1
            error_message = str(exc)
            errors.append(f"{tenant_id}: {error_message}")
            await _safe_record_provider_task_item(
                store,
                task_id=task_id,
                target_id=tenant_id,
                success=False,
                error_message=error_message,
            )

    if failed_count == 0:
        status = "succeeded"
        error_message = None
    elif done_count == 0:
        status = "failed"
        error_message = "; ".join(errors)
    else:
        status = "partial_failed"
        error_message = "; ".join(errors)
    await _safe_finish_provider_task(
        store,
        task_id=task_id,
        status=status,
        done_count=done_count,
        failed_count=failed_count,
        error_message=error_message,
        result={"done": done_count, "failed": failed_count},
    )


async def _run_providers_distribution_task(
    *,
    task_id: str,
    store: AsyncTaskStore,
    source_providers_dir: PathlibPath,
    source_working_dir: PathlibPath,
    target_tenant_ids: list[str],
    source_id: str | None,
    tenant_workspace_pool: Any,
    target_names: dict[str, str | None] | None = None,
) -> None:
    """后台执行 providers 全量分发并回写统一任务表。"""
    try:
        await store.mark_running(task_id)
    except Exception as exc:  # pylint: disable=broad-except
        error_message = str(exc)
        logger.warning(
            "Failed to mark providers distribution task running: task_id=%s",
            task_id,
            exc_info=True,
        )
        await _fail_provider_task_before_running(
            store=store,
            task_id=task_id,
            target_ids=target_tenant_ids,
            error_message=error_message,
        )
        return
    done_count = 0
    failed_count = 0
    errors: list[str] = []
    for tenant_id in target_tenant_ids:
        try:
            result = await _distribute_providers_to_tenant(
                source_providers_dir=source_providers_dir,
                target_tenant_id=tenant_id,
                source_working_dir=source_working_dir,
                source_id=source_id,
                tenant_workspace_pool=tenant_workspace_pool,
            )
            done_count += 1
            await _safe_record_provider_task_item(
                store,
                task_id=task_id,
                target_id=tenant_id,
                success=True,
                result=_providers_result_payload(
                    result,
                    target_names.get(tenant_id) if target_names else None,
                ),
            )
        except Exception as exc:  # pylint: disable=broad-except
            failed_count += 1
            error_message = str(exc)
            errors.append(f"{tenant_id}: {error_message}")
            await _safe_record_provider_task_item(
                store,
                task_id=task_id,
                target_id=tenant_id,
                success=False,
                error_message=error_message,
            )

    if failed_count == 0:
        status = "succeeded"
        error_message = None
    elif done_count == 0:
        status = "failed"
        error_message = "; ".join(errors)
    else:
        status = "partial_failed"
        error_message = "; ".join(errors)
    await _safe_finish_provider_task(
        store,
        task_id=task_id,
        status=status,
        done_count=done_count,
        failed_count=failed_count,
        error_message=error_message,
        result={"done": done_count, "failed": failed_count},
    )


# Agent-level model configuration is deprecated
# Models are now managed at tenant level via TenantModelConfig
# _load_agent_model function removed as agent-specific models are no longer supported


@router.get(
    "",
    response_model=List[ProviderInfo],
    summary="List all providers",
)
async def list_all_providers(
    request: Request,
    manager: ProviderManager = Depends(get_provider_manager),
) -> List[ProviderInfo]:
    started_at = time.perf_counter()
    logger.info(
        "provider_models_handler_start path=%s tenant_id=%s "
        "manager_tenant_id=%s source_id=%s scope_id=%s builtin_count=%d "
        "custom_count=%d root_path=%s",
        request.url.path,
        getattr(request.state, "tenant_id", None),
        manager.tenant_id,
        _request_source_id(request),
        getattr(request.state, "scope_id", None),
        len(manager.builtin_providers),
        len(manager.custom_providers),
        manager.root_path,
    )
    providers = await manager.list_provider_info()
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    request.state.provider_models_handler_ms = duration_ms
    request.state.provider_models_handler_done_at = time.perf_counter()
    model_count = sum(len(provider.models) for provider in providers)
    extra_model_count = sum(
        len(provider.extra_models) for provider in providers
    )
    logger.info(
        "provider_models_handler_done path=%s tenant_id=%s "
        "manager_tenant_id=%s duration_ms=%d provider_count=%d "
        "builtin_count=%d custom_count=%d model_count=%d "
        "extra_model_count=%d root_path=%s",
        request.url.path,
        getattr(request.state, "tenant_id", None),
        manager.tenant_id,
        duration_ms,
        len(providers),
        len(manager.builtin_providers),
        len(manager.custom_providers),
        model_count,
        extra_model_count,
        manager.root_path,
    )
    if duration_ms >= _PROVIDER_API_SLOW_LOG_MS:
        logger.info(
            "provider_list_info_slow tenant_id=%s duration_ms=%d "
            "provider_count=%d custom_count=%d model_count=%d "
            "extra_model_count=%d root_path=%s",
            manager.tenant_id,
            duration_ms,
            len(providers),
            len(manager.custom_providers),
            model_count,
            extra_model_count,
            manager.root_path,
        )
    return providers


@router.put(
    "/{provider_id}/config",
    response_model=ProviderInfo,
    summary="Configure a provider",
)
async def configure_provider(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: ProviderConfigRequest = Body(...),
) -> ProviderInfo:
    ok = manager.update_provider(
        provider_id,
        {
            "api_key": body.api_key,
            "base_url": body.base_url,
            "chat_model": body.chat_model,
        },
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found",
        )

    provider_info = await manager.get_provider_info(provider_id)
    if provider_info is None:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider_id}' not found after update",
        )
    return provider_info


@router.post(
    "/custom-providers",
    response_model=ProviderInfo,
    summary="Create a custom provider",
    status_code=201,
)
async def create_custom_provider_endpoint(
    manager: ProviderManager = Depends(get_provider_manager),
    body: CreateCustomProviderRequest = Body(...),
) -> ProviderInfo:
    try:
        provider_info = await manager.add_custom_provider(
            ProviderInfo(
                id=body.id,
                name=body.name,
                base_url=body.default_base_url,
                api_key_prefix=body.api_key_prefix,
                chat_model=body.chat_model,
                extra_models=body.models,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return provider_info


class TestConnectionResponse(BaseModel):
    success: bool = Field(..., description="Whether the test passed")
    message: str = Field(..., description="Human-readable result message")


class TestProviderRequest(BaseModel):
    api_key: Optional[str] = Field(
        default=None,
        description="Optional API key to test",
    )
    base_url: Optional[str] = Field(
        default=None,
        description="Optional Base URL to test",
    )
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Optional chat model class to test protocol behavior",
    )


class TestModelRequest(BaseModel):
    model_id: str = Field(..., description="Model ID to test")


class DiscoverModelsRequest(BaseModel):
    api_key: Optional[str] = Field(
        default=None,
        description="Optional API key to use for discovery",
    )
    base_url: Optional[str] = Field(
        default=None,
        description="Optional Base URL to use for discovery",
    )
    chat_model: Optional[ChatModelName] = Field(
        default=None,
        description="Optional chat model class to use for discovery",
    )


class DiscoverModelsResponse(BaseModel):
    success: bool = Field(..., description="Whether discovery succeeded")
    models: List[ModelInfo] = Field(
        default_factory=list,
        description="Discovered models",
    )
    message: str = Field(
        default="",
        description="Human-readable result message",
    )
    added_count: int = Field(
        default=0,
        description="How many new models were added into provider config",
    )


class DistributionTenantListResponse(BaseModel):
    tenant_ids: List[str] = Field(default_factory=list)


class ActiveModelDistributionRequest(BaseModel):
    target_tenant_ids: List[str] = Field(default_factory=list)
    overwrite: bool = Field(...)


class ActiveModelDistributionTenantResult(BaseModel):
    tenant_id: str = Field(...)
    success: bool = Field(...)
    bootstrapped: bool = Field(default=False)
    provider_updated: Optional[str] = Field(default=None)
    active_llm_updated: ModelSlotConfig | None = Field(default=None)
    error: Optional[str] = Field(default=None)


class ActiveModelDistributionResponse(BaseModel):
    source_active_llm: ModelSlotConfig
    results: List[ActiveModelDistributionTenantResult] = Field(
        default_factory=list,
    )


class ProvidersDistributionRequest(BaseModel):
    """Request body for distributing entire providers directory."""

    target_tenant_ids: List[str] = Field(
        default_factory=list,
        description="Target tenant IDs to distribute providers to",
    )
    overwrite: bool = Field(
        ...,
        description="Must be true for providers distribution",
    )


class ProvidersDistributionTenantResult(BaseModel):
    """Per-tenant providers distribution result."""

    tenant_id: str = Field(..., description="Target tenant ID")
    success: bool = Field(..., description="Whether distribution succeeded")
    bootstrapped: bool = Field(
        default=False,
        description="Whether the target tenant was bootstrapped during distribution",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if failed",
    )


class ProvidersDistributionResponse(BaseModel):
    """Response payload for providers distribution requests."""

    source_tenant_id: str = Field(..., description="Source tenant ID")
    results: List[ProvidersDistributionTenantResult] = Field(
        default_factory=list,
        description="Per-tenant distribution results",
    )


class AsyncTaskSubmitResponse(BaseModel):
    """异步任务提交响应。"""

    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(..., description="任务ID")
    task_id_alias: str | None = Field(
        default=None,
        alias="taskId",
        description="任务ID兼容字段",
    )
    status: str = Field(default="queued", description="任务状态")
    reused: bool = Field(default=False, description="是否复用已有任务")
    source_active_llm: ModelSlotConfig | None = Field(
        default=None,
        description="同步回退时返回的源活跃模型",
    )
    source_tenant_id: str | None = Field(
        default=None,
        description="同步回退时返回的源租户ID",
    )
    results: list[object] = Field(
        default_factory=list,
        description="同步回退时返回的分发结果",
    )


def _async_task_submit_response(
    *,
    task_id: str,
    status: str = "queued",
    reused: bool = False,
    source_active_llm: ModelSlotConfig | None = None,
    source_tenant_id: str | None = None,
    results: Sequence[Any] | None = None,
) -> AsyncTaskSubmitResponse:
    """构造同时包含 snake_case 与 camelCase 任务 ID 的提交响应。"""
    return AsyncTaskSubmitResponse(
        task_id=task_id,
        taskId=task_id,
        status=status,
        reused=reused,
        source_active_llm=source_active_llm,
        source_tenant_id=source_tenant_id,
        results=list(results) if results is not None else [],
    )


@router.post(
    "/{provider_id}/test",
    response_model=TestConnectionResponse,
    summary="Test provider connection",
)
async def test_provider(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: Optional[TestProviderRequest] = Body(default=None),
) -> TestConnectionResponse:
    """Test if a provider's URL and API key are valid."""
    try:
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found")
        # Ensure we don't accidentally modify provider config during test
        tmp_provider = deepcopy(provider)
        if body and body.api_key:
            tmp_provider.api_key = body.api_key
        if body and body.base_url:
            tmp_provider.base_url = body.base_url
        ok, msg = await tmp_provider.check_connection()
        return TestConnectionResponse(
            success=ok,
            message=(
                "Connection successful" if ok else f"Connection failed: {msg}"
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{provider_id}/discover",
    response_model=DiscoverModelsResponse,
    summary="Discover available models from provider",
)
async def discover_models(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: Optional[DiscoverModelsRequest] = Body(default=None),
) -> DiscoverModelsResponse:
    try:
        ok = manager.update_provider(
            provider_id,
            {
                "api_key": body.api_key if body else None,
                "base_url": body.base_url if body else None,
            },
        )
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"Provider '{provider_id}' not found",
            )
        try:
            result = await manager.fetch_provider_models(
                provider_id,
            )
            success = True
        except Exception:
            result = []
            success = False
        return DiscoverModelsResponse(success=success, models=result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{provider_id}/models/test",
    response_model=TestConnectionResponse,
    summary="Test a specific model",
)
async def test_model(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: TestModelRequest = Body(...),
) -> TestConnectionResponse:
    """Test if a specific model works with the configured provider."""
    try:
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found")
        ok, msg = await provider.check_model_connection(model_id=body.model_id)
        return TestConnectionResponse(
            success=ok,
            message=(
                "Model connection successful"
                if ok
                else f"Model connection failed: {msg}"
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete(
    "/custom-providers/{provider_id}",
    response_model=List[ProviderInfo],
    summary="Delete a custom provider",
)
async def delete_custom_provider_endpoint(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
) -> List[ProviderInfo]:
    try:
        ok = manager.remove_custom_provider(provider_id)
        if not ok:
            raise ValueError(f"Custom Provider '{provider_id}' not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await manager.list_provider_info()


@router.post(
    "/{provider_id}/models",
    response_model=ProviderInfo,
    summary="Add a model to a provider",
    status_code=201,
)
async def add_model_endpoint(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    body: AddModelRequest = Body(...),
) -> ProviderInfo:
    try:
        provider = await manager.add_model_to_provider(
            provider_id=provider_id,
            model_info=ModelInfo(id=body.id, name=body.name),
        )  # Validate provider exists and add model
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return provider


@router.get(
    "/{provider_id}/models/{model_id:path}/config",
    response_model=ModelRuntimeConfig,
    summary="Get a model runtime configuration",
)
async def get_model_runtime_config(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ModelRuntimeConfig:
    try:
        return manager.get_model_config(provider_id, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put(
    "/{provider_id}/models/{model_id:path}/config",
    response_model=ModelRuntimeConfig,
    summary="Update a model runtime configuration",
)
async def update_model_runtime_config(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
    body: ModelRuntimeConfigUpdate = Body(...),
) -> ModelRuntimeConfig:
    try:
        return manager.update_model_config(
            provider_id,
            model_id,
            body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ProbeMultimodalResponse(BaseModel):
    supports_image: bool = Field(
        default=False,
        description="Whether the model supports image input",
    )
    supports_video: bool = Field(
        default=False,
        description="Whether the model supports video input",
    )
    supports_multimodal: bool = Field(
        default=False,
        description="Whether the model supports any multimodal input",
    )
    image_message: str = Field(
        default="",
        description="Probe result message for image support",
    )
    video_message: str = Field(
        default="",
        description="Probe result message for video support",
    )


@router.post(
    "/{provider_id}/models/{model_id:path}/probe-multimodal",
    response_model=ProbeMultimodalResponse,
    summary="Probe model multimodal capability",
)
async def probe_model_multimodal(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ProbeMultimodalResponse:
    """Probe image and video support by sending lightweight test requests."""
    result = await manager.probe_model_multimodal(provider_id, model_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return ProbeMultimodalResponse(**result)


@router.delete(
    "/{provider_id}/models/{model_id:path}",
    response_model=ProviderInfo,
    summary="Remove a model from a provider",
)
async def remove_model_endpoint(
    manager: ProviderManager = Depends(get_provider_manager),
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ProviderInfo:
    try:
        provider = await manager.delete_model_from_provider(
            provider_id=provider_id,
            model_id=model_id,
        )  # Validate provider and model exist and delete
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return provider


@router.get(
    "/active",
    response_model=ActiveModelsInfo,
    summary="Get effective active LLM",
)
async def get_active_models(
    request: Request,
    scope: ActiveModelReadScope = Query(default="effective"),
    _agent_id: Optional[str] = Query(default=None),  # Deprecated
) -> ActiveModelsInfo:
    """Get active model by scope.

    DEPRECATED: Agent-level model configuration is no longer supported.
    Models are now managed at tenant level.

    - effective: Returns tenant-level active model (agent-specific fallback removed)
    - global: ProviderManager global model (tenant-level model)
    - agent: DEPRECATED - treated as 'global' for backward compatibility
    """
    # Short-term compatibility: normalize legacy 'agent' scope to 'global'
    started_at = time.perf_counter()
    if scope == "agent":
        logger.warning(
            "Received deprecated scope='agent' for get_active_models. "
            "Treating as 'global'. Client should be updated to use scope='global'.",
        )

    # For 'effective' and 'global', return the tenant-level active model
    # Agent-level model fallback is removed as models are now tenant-scoped
    tenant_id = _get_effective_tenant_id(request) or "default"
    ProviderManager.ensure_tenant_provider_storage(tenant_id)
    provider_tenant_id = ProviderManager._resolve_effective_provider_tenant_id(
        tenant_id,
    )
    root_path = ProviderManager._get_tenant_root_path(provider_tenant_id)
    global_model = ProviderManager._read_active_model_from_root(
        root_path,
    )
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    if duration_ms >= _PROVIDER_API_SLOW_LOG_MS:
        logger.info(
            "provider_active_model_read_slow tenant_id=%s duration_ms=%d "
            "scope=%s root_path=%s",
            provider_tenant_id,
            duration_ms,
            scope,
            root_path,
        )
    return ActiveModelsInfo(active_llm=global_model)


@router.put(
    "/active",
    response_model=ActiveModelsInfo,
    summary="Set active LLM",
)
async def set_active_model(
    _request: Request,  # Kept for future tenant context usage
    manager: ProviderManager = Depends(get_provider_manager),
    body: ModelSlotRequest = Body(...),
) -> ActiveModelsInfo:
    """Set active model by scope.

    Note: 'agent' scope is deprecated and will be treated as 'global'.
    Models are now managed at tenant level only.
    """
    # Short-term compatibility: normalize legacy 'agent' scope to 'global'
    effective_scope = body.scope
    if body.scope == "agent":
        logger.warning(
            "Received deprecated scope='agent' for set_active_model. "
            "Treating as 'global'. Client should be updated to use scope='global'.",
        )
        effective_scope = "global"

    if effective_scope == "global":
        try:
            await manager.activate_model(body.provider_id, body.model)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            message = str(exc)
            lower_msg = message.lower()
            if "provider" in lower_msg and "not found" in lower_msg:
                raise HTTPException(status_code=404, detail=message) from exc
            raise HTTPException(status_code=400, detail=message) from exc
        return ActiveModelsInfo(active_llm=manager.get_active_model())

    # Any other scope is not supported
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported scope: {body.scope}. Use 'global' for tenant-level model.",
    )


@router.get(
    "/distribution/tenants",
    response_model=DistributionTenantListResponse,
    summary="List discovered tenants for model distribution",
)
async def list_active_model_distribution_tenants(
    request: Request,
) -> DistributionTenantListResponse:
    return DistributionTenantListResponse(
        tenant_ids=await list_logical_tenant_ids(
            _request_source_id(request),
            source_filter=True,
            include_templates=True,
        ),
    )


@router.post(
    "/distribution/active-llm",
    response_model=AsyncTaskSubmitResponse,
    summary="Distribute current tenant active model to target tenants",
)
async def distribute_active_model(
    request: Request,
    body: ActiveModelDistributionRequest = Body(...),
    manager: ProviderManager = Depends(get_provider_manager),
) -> AsyncTaskSubmitResponse:
    if not body.overwrite:
        raise HTTPException(
            status_code=400,
            detail="overwrite=true is required for active-model distribution",
        )
    if not body.target_tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="No target tenant IDs provided",
        )
    tenant_workspace_pool = getattr(
        getattr(getattr(request, "app", None), "state", request.state),
        "tenant_workspace_pool",
        None,
    )
    if tenant_workspace_pool is None:
        raise HTTPException(
            status_code=503,
            detail="Tenant pool not available",
        )

    source_active_model, provider_payload = _resolve_distribution_source(
        manager,
    )
    task_id = _new_async_task_id()
    use_async_dispatch = getattr(
        request,
        "app",
        None,
    ) is not None or not AsyncTaskStore.__module__.endswith(
        "swe.app.async_tasks.store",
    )
    if use_async_dispatch:
        store = await _make_async_task_store(request)
        actor_user_id, actor_user_name = _request_actor(request)
        target_names = await _resolve_distribution_target_names(
            request,
            body.target_tenant_ids,
        )
        await store.start_task(
            task_id=task_id,
            service="swe",
            task_type="provider.active_model.distribute",
            source_id=_request_source_id(request),
            actor_user_id=actor_user_id,
            actor_user_name=actor_user_name,
            target_ids=body.target_tenant_ids,
            target_names=target_names,
            summary=_distribution_summary(
                "模型",
                _active_model_distribution_name(source_active_model),
                len(body.target_tenant_ids),
            ),
        )
        asyncio.create_task(
            _run_active_model_distribution_task(
                task_id=task_id,
                store=store,
                source_working_dir=_request_tenant_storage_working_dir(
                    request,
                ),
                target_tenant_ids=body.target_tenant_ids,
                provider_payload=provider_payload,
                source_active_model=source_active_model,
                source_id=_request_source_id(request),
                tenant_workspace_pool=tenant_workspace_pool,
                target_names=target_names,
            ),
        )
        return _async_task_submit_response(task_id=task_id)

    source_working_dir = _request_tenant_storage_working_dir(request)
    source_id = _request_source_id(request)
    results: list[ActiveModelDistributionTenantResult] = []
    for tenant_id in body.target_tenant_ids:
        try:
            validated_tenant_id = _validate_target_tenant_id(tenant_id)
            result = await _distribute_active_model_to_tenant(
                source_working_dir=source_working_dir,
                target_tenant_id=validated_tenant_id,
                provider_payload=provider_payload,
                source_active_model=source_active_model,
                source_id=source_id,
                tenant_workspace_pool=tenant_workspace_pool,
            )
            results.append(result)
        except Exception as exc:
            results.append(
                ActiveModelDistributionTenantResult(
                    tenant_id=str(tenant_id),
                    success=False,
                    error=str(exc),
                ),
            )

    return _async_task_submit_response(
        task_id=task_id,
        status="succeeded",
        source_active_llm=source_active_model,
        results=results,
    )


@router.post(
    "/distribution/providers",
    response_model=AsyncTaskSubmitResponse,
    summary="Distribute entire providers directory to target tenants",
)
async def distribute_providers(
    request: Request,
    body: ProvidersDistributionRequest = Body(...),
) -> AsyncTaskSubmitResponse:
    """从当前租户全量分发 providers 目录到目标租户。

    该端点执行完全覆盖，包括 builtin/、custom/ 和 active_model.json。

    Args:
        request: FastAPI 请求对象。
        body: 分发请求，包含目标租户 ID 列表。

    Returns:
        每个目标租户的分发结果。

    Raises:
        HTTPException: 400 如果 overwrite 为 False、无目标租户、
            或源 providers 目录不存在。
    """
    if not body.overwrite:
        raise HTTPException(
            status_code=400,
            detail="overwrite=true is required for providers distribution",
        )
    if not body.target_tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="No target tenant IDs provided",
        )
    tenant_workspace_pool = getattr(
        getattr(getattr(request, "app", None), "state", request.state),
        "tenant_workspace_pool",
        None,
    )
    if tenant_workspace_pool is None:
        raise HTTPException(
            status_code=503,
            detail="Tenant pool not available",
        )

    # 获取源租户的有效租户 ID
    effective_tenant_id = _get_effective_tenant_id(request)
    if effective_tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail="No tenant ID in request context",
        )

    # 获取源 providers 目录
    source_providers_dir = _get_tenant_storage_providers_dir(
        effective_tenant_id,
    )
    if not source_providers_dir.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Source providers directory not found for tenant '{effective_tenant_id}'",
        )

    task_id = _new_async_task_id()
    use_async_dispatch = getattr(
        request,
        "app",
        None,
    ) is not None or not AsyncTaskStore.__module__.endswith(
        "swe.app.async_tasks.store",
    )
    if use_async_dispatch:
        store = await _make_async_task_store(request)
        actor_user_id, actor_user_name = _request_actor(request)
        target_names = await _resolve_distribution_target_names(
            request,
            body.target_tenant_ids,
        )
        await store.start_task(
            task_id=task_id,
            service="swe",
            task_type="provider.providers.distribute",
            source_id=_request_source_id(request),
            actor_user_id=actor_user_id,
            actor_user_name=actor_user_name,
            target_ids=body.target_tenant_ids,
            target_names=target_names,
            summary=_distribution_summary(
                "供应商配置",
                _providers_distribution_name(source_providers_dir),
                len(body.target_tenant_ids),
            ),
        )
        asyncio.create_task(
            _run_providers_distribution_task(
                task_id=task_id,
                store=store,
                source_providers_dir=source_providers_dir,
                source_working_dir=_request_tenant_working_dir(request),
                target_tenant_ids=body.target_tenant_ids,
                source_id=_request_source_id(request),
                tenant_workspace_pool=tenant_workspace_pool,
                target_names=target_names,
            ),
        )
        return _async_task_submit_response(task_id=task_id)

    source_working_dir = _request_tenant_working_dir(request)
    source_id = _request_source_id(request)
    results: list[ProvidersDistributionTenantResult] = []
    for tenant_id in body.target_tenant_ids:
        try:
            result = await _distribute_providers_to_tenant(
                source_providers_dir=source_providers_dir,
                target_tenant_id=tenant_id,
                source_working_dir=source_working_dir,
                source_id=source_id,
                tenant_workspace_pool=tenant_workspace_pool,
            )
            results.append(result)
        except Exception as exc:
            results.append(
                ProvidersDistributionTenantResult(
                    tenant_id=str(tenant_id),
                    success=False,
                    error=str(exc),
                ),
            )

    return _async_task_submit_response(
        task_id=task_id,
        status="succeeded",
        source_tenant_id=effective_tenant_id,
        results=results,
    )


# ============================================================================
# Deprecated: Tenant Model Configuration Endpoints
# ============================================================================
# These endpoints are deprecated and will be removed in a future release.
# The /models endpoints should be used instead for all provider/model operations.

tenant_providers_router = APIRouter(
    prefix="/providers",
    tags=["tenant-providers (deprecated)"],
)


@tenant_providers_router.get(
    "",
    summary="Get tenant model configuration (DEPRECATED)",
    deprecated=True,
)
async def get_tenant_providers():
    """Get the current tenant's model configuration (DEPRECATED).

    This endpoint is deprecated. Use /models and /models/active instead.
    Returns the tenant-specific provider configuration from ProviderManager.

    Returns:
        JSON object containing:
        - tenant_id: Current tenant ID
        - providers: List of provider configurations
        - active_model: Currently active model slot

    Raises:
        HTTPException: 400 if tenant ID not set in context
    """
    # Get tenant ID from context
    tenant_id = get_current_effective_tenant_id()
    if tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail="Tenant ID not set in context. Ensure request includes tenant identity.",
        )

    # Get a fresh tenant snapshot before reading the active model.
    manager = await _await_if_needed(
        ProviderManager.get_or_create_instance(tenant_id),
    )
    await _await_if_needed(manager.refresh_if_due())

    # Get active model from ProviderManager
    active_model = manager.get_active_model()

    # Get provider info list
    provider_infos = await manager.list_provider_info()

    return {
        "tenant_id": tenant_id,
        "providers": [p.model_dump() for p in provider_infos],
        "active_model": active_model.model_dump() if active_model else None,
        "deprecated": True,
        "migration_note": "Use /models and /models/active endpoints instead.",
    }
