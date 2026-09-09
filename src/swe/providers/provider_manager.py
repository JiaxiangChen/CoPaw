# -*- coding: utf-8 -*-
"""A Manager class to handle all providers, including built-in and custom ones.
It provides a unified interface to manage providers, such as listing available
providers, adding/removing custom providers, and fetching provider details."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import shutil
import threading
import time
from typing import TYPE_CHECKING, Dict, List

try:
    import fcntl
except ImportError:  # pragma: no cover (Windows)
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover (Unix)
    msvcrt = None

from pathlib import Path

from pydantic import BaseModel

from swe.providers.provider import (
    ModelInfo,
    ModelRuntimeConfig,
    Provider,
    ProviderInfo,
)
from swe.providers.models import ModelSlotConfig
from swe.providers.provider_catalog_service import ProviderCatalogService
from swe.providers.provider_runtime_cache import ProviderRuntimeCache
from swe.providers.tenant_provider_repository import TenantProviderRepository
from swe.constant import SECRET_DIR
from swe.runtime_cache import reset_scope_bound_model_caches
from swe.runtime_workers import run_runtime_state_work

if TYPE_CHECKING:
    from agentscope.model import ChatModelBase

logger = logging.getLogger(__name__)

_PROVIDER_MANAGER_SLOW_LOG_MS = 500
_PROVIDER_INFO_SLOW_LOG_MS = 100
_PROVIDER_FRESHNESS_TTL_SECONDS = 300.0

# -------------------------------------------------------
# Built-in provider definitions and their default models.
# -------------------------------------------------------


class ActiveModelsInfo(BaseModel):
    active_llm: ModelSlotConfig | None


class ProviderManager:
    """A manager class to handle all providers,
    including built-in and custom ones."""

    _instance = None
    _runtime_cache = ProviderRuntimeCache(_PROVIDER_FRESHNESS_TTL_SECONDS)
    _instances = _runtime_cache.instances
    _instances_lock = _runtime_cache.instances_lock
    _init_executor = _runtime_cache.init_executor
    _inflight = _runtime_cache.instance_inflight
    _instance_tasks = _inflight

    @classmethod
    def reset_instance_cache(cls) -> None:
        """清空进程内 ProviderManager 单例缓存。

        source-scoped cutover 期间必须确保旧的 tenant-only 单例不会在同一
        进程生命周期里继续复用，因此这里提供显式清理入口供启动/测试调用。
        """
        cls._runtime_cache.reset_instances()
        cls._instance = None
        reset_scope_bound_model_caches()

    def __init__(self, tenant_id: str = "default") -> None:
        """Initialize provider manager for a specific tenant.

        Args:
            tenant_id: The tenant ID for isolated storage. Defaults to "default".
        """
        # Initialize provider manager, load providers from registry and store
        # any necessary state (e.g., cached models).
        self.tenant_id = tenant_id
        self._repository = TenantProviderRepository(SECRET_DIR)
        self.builtin_providers: Dict[str, Provider] = {}
        self._builtin_provider_defaults: Dict[str, Provider] = {}
        self.custom_providers: Dict[str, Provider] = {}
        self.active_model: ModelSlotConfig | None = None
        self._catalog = ProviderCatalogService(self)
        self._file_freshness_tokens: dict[str, tuple[int, int]] = {}
        self._next_freshness_check_at = (
            time.monotonic() + _PROVIDER_FRESHNESS_TTL_SECONDS
        )
        self._freshness_lock = threading.RLock()
        self.root_path = self._get_tenant_root_path(tenant_id)
        self.builtin_path = self.root_path / "builtin"
        self.custom_path = self.root_path / "custom"
        init_started_at = time.perf_counter()
        logger.info(
            "provider_manager_init_start tenant_id=%s root_path=%s "
            "thread_id=%s",
            tenant_id,
            self.root_path,
            threading.get_ident(),
        )

        step_started_at = time.perf_counter()
        logger.info(
            "provider_manager_init_step_start tenant_id=%s "
            "step=prepare_disk_storage root_path=%s",
            tenant_id,
            self.root_path,
        )
        self._prepare_disk_storage()
        logger.info(
            "provider_manager_init_step_done tenant_id=%s "
            "step=prepare_disk_storage duration_ms=%d root_path=%s",
            tenant_id,
            int((time.perf_counter() - step_started_at) * 1000),
            self.root_path,
        )

        step_started_at = time.perf_counter()
        logger.info(
            "provider_manager_init_step_start tenant_id=%s step=init_builtins",
            tenant_id,
        )
        self._init_builtins()
        logger.info(
            "provider_manager_init_step_done tenant_id=%s "
            "step=init_builtins duration_ms=%d builtin_count=%d",
            tenant_id,
            int((time.perf_counter() - step_started_at) * 1000),
            len(self.builtin_providers),
        )

        step_started_at = time.perf_counter()
        logger.info(
            "provider_manager_init_step_start tenant_id=%s "
            "step=copy_builtin_defaults builtin_count=%d",
            tenant_id,
            len(self.builtin_providers),
        )
        self._builtin_provider_defaults = {
            provider_id: provider.model_copy(deep=True)
            for provider_id, provider in self.builtin_providers.items()
        }
        logger.info(
            "provider_manager_init_step_done tenant_id=%s "
            "step=copy_builtin_defaults duration_ms=%d",
            tenant_id,
            int((time.perf_counter() - step_started_at) * 1000),
        )

        step_started_at = time.perf_counter()
        logger.info(
            "provider_manager_init_step_start tenant_id=%s "
            "step=init_from_storage root_path=%s",
            tenant_id,
            self.root_path,
        )
        self._init_from_storage()
        logger.info(
            "provider_manager_init_step_done tenant_id=%s "
            "step=init_from_storage duration_ms=%d builtin_count=%d "
            "custom_count=%d active_model_set=%s",
            tenant_id,
            int((time.perf_counter() - step_started_at) * 1000),
            len(self.builtin_providers),
            len(self.custom_providers),
            self.active_model is not None,
        )

        step_started_at = time.perf_counter()
        logger.info(
            "provider_manager_init_step_start tenant_id=%s "
            "step=apply_default_annotations",
            tenant_id,
        )
        self._apply_default_annotations()
        logger.info(
            "provider_manager_init_step_done tenant_id=%s "
            "step=apply_default_annotations duration_ms=%d",
            tenant_id,
            int((time.perf_counter() - step_started_at) * 1000),
        )

        step_started_at = time.perf_counter()
        logger.info(
            "provider_manager_init_step_start tenant_id=%s "
            "step=record_mtimes root_path=%s",
            tenant_id,
            self.root_path,
        )
        self._record_mtimes()
        logger.info(
            "provider_manager_init_step_done tenant_id=%s "
            "step=record_mtimes duration_ms=%d freshness_token_count=%d",
            tenant_id,
            int((time.perf_counter() - step_started_at) * 1000),
            len(self._file_freshness_tokens),
        )
        logger.info(
            "provider_manager_init_done tenant_id=%s duration_ms=%d "
            "builtin_count=%d custom_count=%d root_path=%s",
            tenant_id,
            int((time.perf_counter() - init_started_at) * 1000),
            len(self.builtin_providers),
            len(self.custom_providers),
            self.root_path,
        )

    @staticmethod
    def _get_tenant_root_path(tenant_id: str) -> Path:
        """Get the root path for a tenant's provider configuration.

        Args:
            tenant_id: The tenant ID.

        Returns:
            Path to the tenant's provider configuration directory.
        """
        return TenantProviderRepository(SECRET_DIR).root_path(tenant_id)

    @staticmethod
    def _do_initialize_provider_storage(
        tenant_id: str,
        tenant_providers_dir: Path,
    ) -> None:
        """Initialize provider storage for a tenant.

        Copies from the appropriate default_{source} template if available.
        If the source-specific template doesn't exist, automatically creates
        it from the default tenant, then copies to the tenant directory.

        When tenant_id is "default" and source_id is set, the dynamic
        template creation may create the target directory directly (since
        template dir == target dir), so no additional copy is needed.

        Args:
            tenant_id: The effective tenant ID.
            tenant_providers_dir: Target directory for provider storage.
        """
        repository = TenantProviderRepository(SECRET_DIR)
        if os.environ.get("SWE_ENABLE_LEGACY_PROVIDER_STORAGE") != "1":
            repository._seed_scope(
                tenant_id,
                tenant_providers_dir,
            )
            for path in (
                tenant_providers_dir,
                tenant_providers_dir / "builtin",
                tenant_providers_dir / "custom",
            ):
                path.mkdir(parents=True, exist_ok=True)
                repository._restrict_directory_permissions(path)
            return

        from ..config.context import get_current_source_id

        started_at = time.perf_counter()
        source_id = get_current_source_id()
        source_dir = None
        template_name = "default"
        logger.info(
            "provider_storage_init_prepare_start tenant_id=%s source_id=%s "
            "target_dir=%s",
            tenant_id,
            source_id,
            tenant_providers_dir,
        )

        # Try source-specific template first
        if source_id:
            candidate = SECRET_DIR / f"default_{source_id}" / "providers"
            if candidate.exists() and any(candidate.iterdir()):
                source_dir = candidate
                template_name = f"default_{source_id}"
            else:
                # Dynamic creation: create source template from default
                template_started_at = time.perf_counter()
                ProviderManager._ensure_source_template_providers(
                    SECRET_DIR,
                    source_id,
                )
                logger.info(
                    "provider_storage_source_template_ensure_done "
                    "tenant_id=%s source_id=%s duration_ms=%d "
                    "candidate=%s exists_after=%s",
                    tenant_id,
                    source_id,
                    int((time.perf_counter() - template_started_at) * 1000),
                    candidate,
                    candidate.exists(),
                )
                # Re-check after creation
                if candidate.exists() and any(candidate.iterdir()):
                    source_dir = candidate
                    template_name = f"default_{source_id}"

        # After dynamic creation, target might already exist
        # (when effective_tenant_id matches template_name, e.g., default + ruice)
        if tenant_providers_dir.exists():
            logger.info(
                "provider_storage_init_skip_existing tenant_id=%s "
                "duration_ms=%d target_dir=%s",
                tenant_id,
                int((time.perf_counter() - started_at) * 1000),
                tenant_providers_dir,
            )
            return

        # Fall back to generic default
        if source_dir is None:
            default_dir = SECRET_DIR / "default" / "providers"
            if default_dir.exists() and any(default_dir.iterdir()):
                source_dir = default_dir

        if source_dir is not None:
            logger.info(
                "provider_storage_copy_start tenant_id=%s template=%s "
                "source_dir=%s target_dir=%s",
                tenant_id,
                template_name,
                source_dir,
                tenant_providers_dir,
            )
            copy_started_at = time.perf_counter()
            shutil.copytree(source_dir, tenant_providers_dir)
            logger.info(
                "provider_storage_copy_done tenant_id=%s template=%s "
                "duration_ms=%d source_dir=%s target_dir=%s",
                tenant_id,
                template_name,
                int((time.perf_counter() - copy_started_at) * 1000),
                source_dir,
                tenant_providers_dir,
            )
        else:
            logger.info(
                "provider_storage_empty_create_start tenant_id=%s "
                "target_dir=%s",
                tenant_id,
                tenant_providers_dir,
            )
            mkdir_started_at = time.perf_counter()
            tenant_providers_dir.mkdir(parents=True, exist_ok=True)
            (tenant_providers_dir / "builtin").mkdir(exist_ok=True)
            (tenant_providers_dir / "custom").mkdir(exist_ok=True)
            logger.info(
                "provider_storage_empty_create_done tenant_id=%s "
                "duration_ms=%d target_dir=%s",
                tenant_id,
                int((time.perf_counter() - mkdir_started_at) * 1000),
                tenant_providers_dir,
            )

        logger.info(
            "provider_storage_init_prepare_done tenant_id=%s source_id=%s "
            "template=%s source_dir=%s duration_ms=%d target_dir=%s",
            tenant_id,
            source_id,
            template_name,
            source_dir,
            int((time.perf_counter() - started_at) * 1000),
            tenant_providers_dir,
        )

    @staticmethod
    def _ensure_source_template_providers(
        secret_dir: Path,
        source_id: str,
    ) -> None:
        """Ensure source-specific providers template exists.

        Creates default_{source_id}/providers from default/providers
        if the source template doesn't exist.

        Args:
            secret_dir: Base secret directory (e.g., ~/.swe.secret).
            source_id: Source identifier (e.g., "ruice").
        """
        started_at = time.perf_counter()
        default_providers = secret_dir / "default" / "providers"
        target_providers = secret_dir / f"default_{source_id}" / "providers"

        if not default_providers.exists():
            logger.info(
                "provider_storage_source_template_skip_no_default "
                "source_id=%s duration_ms=%d default_providers=%s",
                source_id,
                int((time.perf_counter() - started_at) * 1000),
                default_providers,
            )
            return

        target_parent = target_providers.parent
        try:
            if not target_parent.exists():
                # Copy entire default directory to create default_{source_id}
                shutil.copytree(
                    secret_dir / "default",
                    target_parent,
                )
                logger.info(
                    "Created source template providers directory: %s",
                    target_parent,
                )
            elif not target_providers.exists():
                shutil.copytree(default_providers, target_providers)
                logger.info(
                    "Created source template providers: %s",
                    target_providers,
                )
            logger.info(
                "provider_storage_source_template_done source_id=%s "
                "duration_ms=%d target_providers=%s target_exists=%s",
                source_id,
                int((time.perf_counter() - started_at) * 1000),
                target_providers,
                target_providers.exists(),
            )
        except OSError:
            # Handle race condition - created by concurrent request
            if not target_providers.exists():
                raise
            logger.debug(
                "Source template providers %s created by concurrent request",
                target_providers,
            )
            logger.info(
                "provider_storage_source_template_race_done source_id=%s "
                "duration_ms=%d target_providers=%s",
                source_id,
                int((time.perf_counter() - started_at) * 1000),
                target_providers,
            )

    @staticmethod
    def _resolve_effective_provider_tenant_id(
        tenant_id: str | None,
    ) -> str:
        """解析 provider 存储使用的 storage 租户标识。"""
        from ..config.context import (
            canonicalize_scope_id,
            get_current_scope_id,
            get_current_source_id,
            get_current_tenant_id,
            resolve_storage_tenant_id,
        )

        requested_tenant_id = tenant_id or get_current_tenant_id() or "default"
        if tenant_id is not None:
            try:
                return canonicalize_scope_id(requested_tenant_id)
            except ValueError:
                # 显式传入的是逻辑 tenant/default 模板名时，仍需结合当前
                # source 做 storage 解析；但不能继续套用当前请求 scope，
                # 否则会把目标租户错误重定向回源租户目录。
                resolved_tenant_id = resolve_storage_tenant_id(
                    requested_tenant_id,
                    get_current_source_id(),
                )
                return resolved_tenant_id or requested_tenant_id

        resolved_tenant_id = resolve_storage_tenant_id(
            requested_tenant_id,
            get_current_source_id(),
            scope_id=get_current_scope_id(),
        )
        return resolved_tenant_id or requested_tenant_id

    @staticmethod
    def ensure_tenant_provider_storage(tenant_id: str | None) -> None:
        """Ensure tenant provider storage exists, initializing if needed.

        This method is idempotent and concurrency-safe. It initializes tenant
        provider storage by copying from the default tenant's configuration
        when it doesn't exist. If the default tenant has no configuration,
        an empty directory structure is created.

        当显式传入 tenant_id 且当前上下文带有 source/scope 时，会写入
        目标租户在当前 source 下的 storage 目录；未传入 tenant_id 时
        继续沿用当前请求对应的 storage 语义。

        Args:
            tenant_id: The tenant ID to ensure storage for. If None, uses "default".

        Raises:
            TimeoutError: If unable to acquire initialization lock within timeout.
            OSError: If initialization fails due to filesystem issues.

        Note:
            This method is called automatically at provider feature boundaries
            (provider APIs, local model APIs, runtime model creation). It is safe
            to call multiple times - subsequent calls are no-ops if storage exists.
        """
        effective_tenant_id = (
            ProviderManager._resolve_effective_provider_tenant_id(
                tenant_id,
            )
        )
        if os.environ.get("SWE_ENABLE_LEGACY_PROVIDER_STORAGE") != "1":
            TenantProviderRepository(SECRET_DIR).prepare_scope(
                effective_tenant_id,
            )
            return
        tenant_providers_dir = ProviderManager._get_tenant_root_path(
            effective_tenant_id,
        )

        # Fast path: already exists
        if tenant_providers_dir.exists():
            return

        lock_file = tenant_providers_dir.parent / ".provider_init.lock"
        started_at = time.perf_counter()
        logger.info(
            "provider_storage_ensure_slow_path_start route_tenant_id=%s "
            "provider_tenant_id=%s target_dir=%s lock_file=%s",
            tenant_id,
            effective_tenant_id,
            tenant_providers_dir,
            lock_file,
        )
        try:
            tenant_providers_dir.parent.mkdir(parents=True, exist_ok=True)
            ProviderManager._initialize_with_lock(
                lock_file,
                effective_tenant_id,
                tenant_providers_dir,
            )
            logger.info(
                "provider_storage_ensure_slow_path_done "
                "route_tenant_id=%s provider_tenant_id=%s duration_ms=%d "
                "target_dir=%s exists_after=%s",
                tenant_id,
                effective_tenant_id,
                int((time.perf_counter() - started_at) * 1000),
                tenant_providers_dir,
                tenant_providers_dir.exists(),
            )
        except Exception as e:
            logger.error(
                "Failed to initialize provider config for tenant %s: %s",
                effective_tenant_id,
                e,
            )
            raise

    @staticmethod
    def _initialize_with_lock(
        lock_file: Path,
        tenant_id: str,
        tenant_providers_dir: Path,
    ) -> None:
        """Initialize provider storage with file locking.

        Args:
            lock_file: Path to lock file.
            tenant_id: The tenant ID.
            tenant_providers_dir: Target directory for provider storage.
        """
        max_wait_seconds = 30.0
        deadline = time.monotonic() + max_wait_seconds
        started_at = time.perf_counter()
        logger.info(
            "provider_storage_init_lock_start tenant_id=%s lock_file=%s "
            "target_dir=%s",
            tenant_id,
            lock_file,
            tenant_providers_dir,
        )

        with open(lock_file, "w", encoding="utf-8") as f:
            # Acquire lock
            wait_started_at = time.perf_counter()
            ProviderManager._wait_for_lock(
                f,
                deadline,
                tenant_id,
                tenant_providers_dir,
            )
            wait_ms = int((time.perf_counter() - wait_started_at) * 1000)
            logger.info(
                "provider_storage_init_lock_ready tenant_id=%s wait_ms=%d "
                "target_dir=%s exists_after_wait=%s",
                tenant_id,
                wait_ms,
                tenant_providers_dir,
                tenant_providers_dir.exists(),
            )

            # Double-check after acquiring lock
            if tenant_providers_dir.exists():
                logger.info(
                    "provider_storage_init_skip_after_lock tenant_id=%s "
                    "duration_ms=%d wait_ms=%d target_dir=%s",
                    tenant_id,
                    int((time.perf_counter() - started_at) * 1000),
                    wait_ms,
                    tenant_providers_dir,
                )
                return

            # Initialize storage
            init_started_at = time.perf_counter()
            ProviderManager._do_initialize_provider_storage(
                tenant_id,
                tenant_providers_dir,
            )
            init_ms = int((time.perf_counter() - init_started_at) * 1000)

            # Release lock
            ProviderManager._release_lock(f)
            logger.info(
                "provider_storage_init_lock_done tenant_id=%s "
                "duration_ms=%d wait_ms=%d init_ms=%d target_dir=%s",
                tenant_id,
                int((time.perf_counter() - started_at) * 1000),
                wait_ms,
                init_ms,
                tenant_providers_dir,
            )

    @staticmethod
    def _wait_for_lock(
        f,
        deadline: float,
        tenant_id: str,
        tenant_providers_dir: Path,
    ) -> None:
        """Wait for file lock with timeout.

        Args:
            f: File handle.
            deadline: Timeout deadline (monotonic time).
            tenant_id: Tenant ID for logging.
            tenant_providers_dir: Provider directory to check during wait.
        """
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover (Windows)
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (IOError, OSError) as exc:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Timeout waiting for provider initialization lock for tenant {tenant_id}",
                    ) from exc
                logger.debug(
                    "Waiting for concurrent provider initialization for tenant %s",
                    tenant_id,
                )
                time.sleep(0.05)
                if tenant_providers_dir.exists():
                    return

    @staticmethod
    def _release_lock(f) -> None:
        """Release file lock.

        Args:
            f: File handle.
        """
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover (Windows)
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def get_instance(tenant_id: str | None = None) -> "ProviderManager":
        """Get a ProviderManager instance for a specific tenant.

        This method implements a multi-instance singleton pattern where
        each tenant has its own isolated ProviderManager instance.

        当显式传入 tenant_id 且当前上下文带有 source/scope 时，单例 key
        会解析为目标租户在当前 source 下的 storage 目录；未传入
        tenant_id 时继续沿用当前请求对应的 storage 语义。

        Args:
            tenant_id: The tenant ID. If None, uses "default" tenant.

        Returns:
            ProviderManager instance for the specified tenant.
        """
        effective_tenant_id = (
            ProviderManager._resolve_effective_provider_tenant_id(
                tenant_id,
            )
        )

        cached = ProviderManager._instances.get(effective_tenant_id)
        if cached is not None:
            return cached
        logger.info(
            "provider_manager_instance_cache_miss route_tenant_id=%s "
            "provider_tenant_id=%s cached_instances=%d thread_id=%s",
            tenant_id,
            effective_tenant_id,
            len(ProviderManager._instances),
            threading.get_ident(),
        )
        future = ProviderManager._runtime_cache.get_or_start_instance(
            effective_tenant_id,
            ProviderManager._build_instance_sync,
        )
        create_started_at = time.perf_counter()
        existing = future.result()
        logger.info(
            "provider_manager_instance_create_done route_tenant_id=%s "
            "provider_tenant_id=%s duration_ms=%d cached_instances=%d",
            tenant_id,
            effective_tenant_id,
            int((time.perf_counter() - create_started_at) * 1000),
            len(ProviderManager._instances),
        )
        return existing

    @classmethod
    async def get_or_create_instance(
        cls,
        tenant_id: str | None = None,
    ) -> "ProviderManager":
        """Get a manager asynchronously with scope-keyed single-flight startup."""
        effective = cls._resolve_effective_provider_tenant_id(tenant_id)
        cached = cls._instances.get(effective)
        if cached is not None:
            return cached
        future = cls._runtime_cache.get_or_start_instance(
            effective,
            cls._build_instance_sync,
        )

        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        finally:
            cls._runtime_cache.discard_completed_instance_startup(
                effective,
                future,
            )

    @classmethod
    def _get_or_start_instance_future(
        cls,
        effective: str,
    ):
        """Compatibility facade for the cache-owned startup registry."""
        return cls._runtime_cache.get_or_start_instance(
            effective,
            cls._build_instance_sync,
        )

    @staticmethod
    def _build_instance_sync(effective: str) -> "ProviderManager":
        ProviderManager.ensure_tenant_provider_storage(effective)
        return ProviderManager(effective)

    @staticmethod
    def get_active_chat_model() -> ChatModelBase:
        """Get the currently active provider/model configuration.

        .. deprecated::
            This method is deprecated in multi-tenant environments.
            Use TenantModelContext.get_config() for tenant-isolated model selection.
        """
        import warnings

        warnings.warn(
            "get_active_chat_model() accesses global active model which is not "
            "isolated per tenant. In multi-tenant environments, use "
            "TenantModelContext.get_config() for proper tenant isolation.",
            DeprecationWarning,
            stacklevel=2,
        )
        manager = ProviderManager.get_instance()
        model = manager.get_active_model()
        if model is None or model.provider_id == "" or model.model == "":
            raise ValueError("No active model configured.")
        provider = manager.get_provider(model.provider_id)
        if provider is None:
            raise ValueError(
                f"Active provider '{model.provider_id}' not found.",
            )
        model_config = provider.get_model_config(model.model)
        return provider.get_chat_model_instance(
            model.model,
            generation_kwargs=provider.build_generation_kwargs(model_config),
        )

    def _prepare_disk_storage(self):
        """Prepare directory structure"""
        paths = self._repository.prepare_scope(self.tenant_id)
        self.root_path = paths.root
        self.builtin_path = paths.builtin
        self.custom_path = paths.custom

    def _init_builtins(self):
        # Deep copy builtin providers to ensure per-tenant isolation
        pass

    def _add_builtin(self, provider: Provider):
        self.builtin_providers[provider.id] = provider

    def _record_mtimes(self):
        """Snapshot modification times of all provider config files."""
        self._file_freshness_tokens = self._repository.freshness_snapshot(
            self.tenant_id,
            list(self.builtin_providers),
        )

    def _file_token(self, path: Path) -> tuple[int, int]:
        repository = getattr(self, "_repository", None)
        if repository is not None:
            return repository.file_token(path)
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size

    def _update_mtime(self, path: Path):
        """Update cached mtime for a single file after writing."""
        if path.exists():
            self._file_freshness_tokens[str(path)] = self._file_token(path)
        else:
            self._file_freshness_tokens.pop(str(path), None)

    def _mark_freshness_due(self) -> None:
        self._next_freshness_check_at = 0.0
        self._get_runtime_cache().mark_freshness_due(self.tenant_id)

    def _get_runtime_cache(self) -> ProviderRuntimeCache:
        return ProviderManager._runtime_cache

    def _catalog_seams(
        self,
    ) -> tuple[TenantProviderRepository | None, ProviderRuntimeCache]:
        """Supply catalog dependencies without exposing storage internals."""
        runtime_cache = self._get_runtime_cache()
        runtime_cache.set_model_cache_reset(
            reset_scope_bound_model_caches,
        )
        return getattr(self, "_repository", None), runtime_cache

    def _catalog_service(self) -> ProviderCatalogService:
        """Return the catalog service, including for legacy test fixtures."""
        catalog = getattr(self, "_catalog", None)
        if catalog is None:
            catalog = ProviderCatalogService(self)
            self._catalog = catalog
        return catalog

    def _refresh_if_stale(self):
        """Reload providers whose files changed on disk since last snapshot."""
        started_at = time.perf_counter()
        detect_builtin_started_at = time.perf_counter()
        changed_builtin = self._detect_changed_builtins()
        detect_builtin_ms = int(
            (time.perf_counter() - detect_builtin_started_at) * 1000,
        )
        detect_custom_started_at = time.perf_counter()
        (
            changed_custom,
            new_custom,
            removed_custom,
        ) = self._detect_custom_changes()
        detect_custom_ms = int(
            (time.perf_counter() - detect_custom_started_at) * 1000,
        )
        detect_active_started_at = time.perf_counter()
        active_changed = self._detect_active_model_change()
        detect_active_ms = int(
            (time.perf_counter() - detect_active_started_at) * 1000,
        )

        if not any(
            [
                changed_builtin,
                changed_custom,
                new_custom,
                removed_custom,
                active_changed,
            ],
        ):
            total_ms = int((time.perf_counter() - started_at) * 1000)
            if total_ms >= _PROVIDER_MANAGER_SLOW_LOG_MS:
                logger.info(
                    "provider_refresh_noop_slow tenant_id=%s "
                    "duration_ms=%d detect_builtin_ms=%d "
                    "detect_custom_ms=%d detect_active_ms=%d "
                    "builtin_count=%d custom_count=%d freshness_token_count=%d",
                    self.tenant_id,
                    total_ms,
                    detect_builtin_ms,
                    detect_custom_ms,
                    detect_active_ms,
                    len(self.builtin_providers),
                    len(self.custom_providers),
                    len(self._file_freshness_tokens),
                )
            return

        logger.info(
            "provider_refresh_start tenant_id=%s changed_builtin=%d "
            "changed_custom=%d new_custom=%d removed_custom=%d "
            "active_changed=%s detect_builtin_ms=%d detect_custom_ms=%d "
            "detect_active_ms=%d root_path=%s",
            self.tenant_id,
            len(changed_builtin),
            len(changed_custom),
            len(new_custom),
            len(removed_custom),
            active_changed,
            detect_builtin_ms,
            detect_custom_ms,
            detect_active_ms,
            self.root_path,
        )
        apply_builtin_started_at = time.perf_counter()
        self._apply_builtin_refresh(changed_builtin)
        apply_builtin_ms = int(
            (time.perf_counter() - apply_builtin_started_at) * 1000,
        )
        apply_custom_started_at = time.perf_counter()
        self._apply_custom_refresh(changed_custom, new_custom, removed_custom)
        apply_custom_ms = int(
            (time.perf_counter() - apply_custom_started_at) * 1000,
        )
        apply_active_ms = 0
        if active_changed:
            apply_active_started_at = time.perf_counter()
            self._apply_active_model_refresh()
            apply_active_ms = int(
                (time.perf_counter() - apply_active_started_at) * 1000,
            )
        reset_cache_started_at = time.perf_counter()
        reset_scope_bound_model_caches(self.tenant_id)
        reset_cache_ms = int(
            (time.perf_counter() - reset_cache_started_at) * 1000,
        )
        record_started_at = time.perf_counter()
        self._record_mtimes()
        record_ms = int((time.perf_counter() - record_started_at) * 1000)
        logger.info(
            "provider_refresh_done tenant_id=%s duration_ms=%d "
            "apply_builtin_ms=%d apply_custom_ms=%d apply_active_ms=%d "
            "reset_cache_ms=%d record_mtimes_ms=%d builtin_count=%d "
            "custom_count=%d freshness_token_count=%d root_path=%s",
            self.tenant_id,
            int((time.perf_counter() - started_at) * 1000),
            apply_builtin_ms,
            apply_custom_ms,
            apply_active_ms,
            reset_cache_ms,
            record_ms,
            len(self.builtin_providers),
            len(self.custom_providers),
            len(self._file_freshness_tokens),
            self.root_path,
        )

    def _detect_changed_builtins(self) -> list[str]:
        """Detect builtin providers whose files have changed."""
        changed: list[str] = []
        for provider_id in self.builtin_providers:
            path = self.builtin_path / f"{provider_id}.json"
            if self._file_has_changed(path):
                changed.append(provider_id)
        return changed

    def _detect_custom_changes(
        self,
    ) -> tuple[list[Path], list[Path], list[str]]:
        """Detect custom provider changes, additions, and removals."""
        changed: list[Path] = []
        new: list[Path] = []
        current: set[str] = set()

        for path in self._repository.custom_provider_paths(self.tenant_id):
            path_str = str(path)
            current.add(path_str)
            try:
                token = self._file_token(path)
                if path_str not in self._file_freshness_tokens:
                    new.append(path)
                elif self._file_freshness_tokens[path_str] != token:
                    changed.append(path)
            except OSError:
                pass

        removed = self._detect_removed_custom(current)
        return changed, new, removed

    def _detect_removed_custom(self, current_paths: set[str]) -> list[str]:
        """Detect custom provider files that were removed."""
        removed: list[str] = []
        custom_prefix = str(self.custom_path)
        for path_str in list(self._file_freshness_tokens):
            if (
                path_str.startswith(custom_prefix)
                and path_str not in current_paths
            ):
                removed.append(path_str)
        return removed

    def _detect_active_model_change(self) -> bool:
        """Check if active model file has changed."""
        active_path = self.root_path / "active_model.json"
        return self._file_has_changed(active_path)

    def _file_has_changed(self, path: Path) -> bool:
        """Check if a file has changed since last snapshot."""
        return self._repository.file_has_changed(
            path,
            self._file_freshness_tokens,
        )

    def _apply_builtin_refresh(self, provider_ids: list[str]) -> None:
        """Apply changes for modified builtin providers."""
        for provider_id in provider_ids:
            provider = self.load_provider(provider_id, is_builtin=True)
            if provider:
                builtin = self.builtin_providers[provider_id]
                if not builtin.freeze_url:
                    builtin.base_url = provider.base_url
                builtin.api_key = provider.api_key
                builtin.extra_models = provider.extra_models
                builtin.model_configs = provider.model_configs
            else:
                self._reset_builtin_provider(provider_id)

    def _reset_builtin_provider(self, provider_id: str) -> None:
        default_provider = self._builtin_provider_defaults.get(provider_id)
        if default_provider is None:
            self.builtin_providers.pop(provider_id, None)
            return
        self.builtin_providers[provider_id] = default_provider.model_copy(
            deep=True,
        )

    def _apply_custom_refresh(
        self,
        changed: list[Path],
        new: list[Path],
        removed: list[str],
    ) -> None:
        """Apply changes for custom providers."""
        for path in changed + new:
            provider = self.load_provider(path.stem, is_builtin=False)
            if provider:
                self.custom_providers[provider.id] = provider
            else:
                self.custom_providers.pop(path.stem, None)

        for path_str in removed:
            provider_id = Path(path_str).stem
            self.custom_providers.pop(provider_id, None)

    def _apply_active_model_refresh(self) -> None:
        """Apply changes for active model."""
        self.active_model = self.load_active_model()

    async def refresh_if_due(self) -> None:
        """Refresh provider files only after the freshness TTL elapses."""
        cache = self._get_runtime_cache()
        if time.monotonic() < getattr(
            self,
            "_next_freshness_check_at",
            0.0,
        ) and not cache.freshness_check_is_due(self.tenant_id):
            return
        cache.ensure_freshness_due(self.tenant_id)

        async def refresh() -> None:
            future = cache.submit(
                self._refresh_and_mark_fresh,
            )
            await asyncio.shield(asyncio.wrap_future(future))

        await cache.refresh_if_due(self.tenant_id, refresh)
        self._next_freshness_check_at = cache.next_freshness_check_at(
            self.tenant_id,
        )

    def _refresh_and_mark_fresh(self) -> None:
        self._refresh_if_stale()

    async def list_provider_info(self) -> List[ProviderInfo]:
        return await self._catalog_service().list_provider_info()

    async def _get_provider_info_with_timing(
        self,
        provider: Provider,
        provider_kind: str,
    ) -> tuple[ProviderInfo, int]:
        """记录单个 provider 生成 ProviderInfo 的耗时。"""
        started_at = time.perf_counter()
        try:
            provider_info = await provider.get_info()
        except Exception:
            logger.exception(
                "provider_get_info_error tenant_id=%s provider_id=%s "
                "provider_kind=%s duration_ms=%d",
                self.tenant_id,
                provider.id,
                provider_kind,
                int((time.perf_counter() - started_at) * 1000),
            )
            raise

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        if duration_ms >= _PROVIDER_INFO_SLOW_LOG_MS:
            logger.info(
                "provider_get_info_slow tenant_id=%s provider_id=%s "
                "provider_kind=%s duration_ms=%d model_count=%d "
                "extra_model_count=%d is_custom=%s is_local=%s",
                self.tenant_id,
                provider.id,
                provider_kind,
                duration_ms,
                len(provider_info.models),
                len(provider_info.extra_models),
                provider_info.is_custom,
                provider_info.is_local,
            )
        return provider_info, duration_ms

    def get_provider(self, provider_id: str) -> Provider | None:
        # Return a provider instance by its ID. This will be used to create
        # chat model instances for the agent.
        if provider_id in self.builtin_providers:
            return self.builtin_providers[provider_id]
        if provider_id in self.custom_providers:
            return self.custom_providers[provider_id]
        return None

    async def get_provider_info(self, provider_id: str) -> ProviderInfo | None:
        return await self._catalog_service().get_provider_info(provider_id)

    def get_active_model(self) -> ModelSlotConfig | None:
        """Return the cached active model.

        Async request boundaries refresh this snapshot through
        :meth:`refresh_if_due`.  Synchronous CLI callers retain their
        historical refresh behavior when no event loop is running.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if time.monotonic() >= getattr(
                self,
                "_next_freshness_check_at",
                0.0,
            ):
                self._refresh_if_stale()
                self._next_freshness_check_at = (
                    time.monotonic() + _PROVIDER_FRESHNESS_TTL_SECONDS
                )
        return self.active_model

    def update_provider(self, provider_id: str, config: Dict) -> bool:
        return self._catalog_service().update_provider(provider_id, config)

    def get_model_config(
        self,
        provider_id: str,
        model_id: str,
    ) -> ModelRuntimeConfig:
        return self._catalog_service().get_model_config(provider_id, model_id)

    def update_model_config(
        self,
        provider_id: str,
        model_id: str,
        updates: Dict,
    ) -> ModelRuntimeConfig:
        return self._catalog_service().update_model_config(
            provider_id,
            model_id,
            updates,
        )

    async def fetch_provider_models(
        self,
        provider_id: str,
    ) -> List[ModelInfo]:
        return await self._catalog_service().fetch_provider_models(provider_id)

    def _resolve_custom_provider_id(self, provider_id: str) -> str:
        return self._catalog_service().resolve_custom_provider_id(provider_id)

    async def add_custom_provider(self, provider_data: ProviderInfo):
        return await self._catalog_service().add_custom_provider(provider_data)

    def remove_custom_provider(self, provider_id: str) -> bool:
        return self._catalog_service().remove_custom_provider(provider_id)

    async def activate_model(self, provider_id: str, model_id: str):
        return await self._catalog_service().activate_model(
            provider_id,
            model_id,
        )

    def maybe_probe_multimodal(self, provider_id: str, model_id: str) -> None:
        self._catalog_service().maybe_probe_multimodal(provider_id, model_id)

    async def _auto_probe_multimodal(
        self,
        provider_id: str,
        model_id: str,
    ) -> None:
        await self._catalog_service()._auto_probe_multimodal(
            provider_id,
            model_id,
        )

    async def add_model_to_provider(
        self,
        provider_id: str,
        model_info: ModelInfo,
    ) -> ProviderInfo:
        return await self._catalog_service().add_model_to_provider(
            provider_id,
            model_info,
        )

    async def delete_model_from_provider(
        self,
        provider_id: str,
        model_id: str,
    ) -> ProviderInfo:
        return await self._catalog_service().delete_model_from_provider(
            provider_id,
            model_id,
        )

    async def probe_model_multimodal(
        self,
        provider_id: str,
        model_id: str,
    ) -> dict:
        return await self._catalog_service().probe_model_multimodal(
            provider_id,
            model_id,
        )

    def _save_provider(
        self,
        provider: Provider,
        is_builtin: bool = False,
        skip_if_exists: bool = False,
    ):
        """Save a provider configuration to disk."""
        provider_dir = self.builtin_path if is_builtin else self.custom_path
        provider_path = provider_dir / f"{provider.id}.json"
        if skip_if_exists and provider_path.exists():
            return
        repository = getattr(self, "_repository", None)
        if (
            repository is not None
            and repository.root_path(self.tenant_id) == self.root_path
        ):
            provider_path = repository.write_provider(
                self.tenant_id,
                provider.model_dump(),
                is_builtin=is_builtin,
                skip_if_exists=skip_if_exists,
            )
            self._update_mtime(provider_path)
            return
        with open(provider_path, "w", encoding="utf-8") as f:
            json.dump(provider.model_dump(), f, ensure_ascii=False, indent=2)
        try:
            os.chmod(provider_path, 0o600)
        except OSError:
            pass
        self._update_mtime(provider_path)

    async def _save_provider_async(
        self,
        provider: Provider,
        is_builtin: bool = False,
        skip_if_exists: bool = False,
    ) -> None:
        await run_runtime_state_work(
            self._save_provider,
            provider,
            is_builtin=is_builtin,
            skip_if_exists=skip_if_exists,
        )
        self._mark_freshness_due()

    def overwrite_provider_payload(self, payload: Dict) -> Provider:
        return self._catalog_service().overwrite_provider_payload(payload)

    def load_provider(
        self,
        provider_id: str,
        is_builtin: bool = False,
    ) -> Provider | None:
        """Load a provider configuration from disk."""
        provider_dir = self.builtin_path if is_builtin else self.custom_path
        provider_path = provider_dir / f"{provider_id}.json"
        if not provider_path.exists():
            return None
        try:
            repository = getattr(self, "_repository", None)
            if (
                repository is not None
                and repository.root_path(self.tenant_id) == self.root_path
            ):
                data = repository.read_provider(
                    self.tenant_id,
                    provider_id,
                    is_builtin=is_builtin,
                )
                return self._provider_from_data(data) if data else None
            with open(provider_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._provider_from_data(data)
        except Exception as e:
            logger.warning(
                "Failed to load provider '%s' from %s: %s",
                provider_id,
                provider_path,
                e,
            )
            return None

    def _provider_from_data(self, data: Dict) -> Provider:
        """Deserialize provider data to a concrete provider type."""
        from swe.providers.anthropic_provider import AnthropicProvider
        from swe.providers.ollama_provider import OllamaProvider
        from swe.providers.openai_provider import OpenAIProvider

        provider_id = str(data.get("id", ""))
        chat_model = str(data.get("chat_model", ""))

        if provider_id == "anthropic" or chat_model == "AnthropicChatModel":
            return AnthropicProvider.model_validate(data)
        # if provider_id == "gemini" or chat_model == "GeminiChatModel":
        #     return GeminiProvider.model_validate(data)
        if provider_id == "ollama":
            return OllamaProvider.model_validate(data)
        return OpenAIProvider.model_validate(data)

    def save_active_model(self, active_model: ModelSlotConfig):
        """Save the active provider/model configuration to disk."""
        repository = getattr(self, "_repository", None)
        if (
            repository is not None
            and repository.root_path(self.tenant_id) == self.root_path
        ):
            active_path = repository.write_active_model(
                self.tenant_id,
                active_model,
            )
            self._update_mtime(active_path)
            return
        self._save_active_model_to_root(self.root_path, active_model)
        self._update_mtime(self.root_path / "active_model.json")

    async def _save_active_model_async(
        self,
        active_model: ModelSlotConfig,
    ) -> None:
        await run_runtime_state_work(self.save_active_model, active_model)
        self._mark_freshness_due()

    @staticmethod
    def _save_active_model_to_root(
        root_path: Path,
        active_model: ModelSlotConfig,
    ) -> None:
        """Save the active provider/model configuration under a provider root."""
        active_path = root_path / "active_model.json"
        with open(active_path, "w", encoding="utf-8") as f:
            json.dump(
                active_model.model_dump(),
                f,
                ensure_ascii=False,
                indent=2,
            )
        try:
            os.chmod(active_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _read_active_model_from_root(
        root_path: Path,
    ) -> ModelSlotConfig | None:
        """Read active provider/model configuration from active_model.json."""
        active_path = root_path / "active_model.json"

        if active_path.exists():
            try:
                with open(active_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return ModelSlotConfig.model_validate(data)
            except Exception:
                return None

        return None

    def load_active_model(self) -> ModelSlotConfig | None:
        """Load the active provider/model configuration from disk."""
        repository = getattr(self, "_repository", None)
        if (
            repository is not None
            and repository.root_path(self.tenant_id) == self.root_path
        ):
            return repository.read_active_model(self.tenant_id)
        return self._read_active_model_from_root(self.root_path)

    def _init_from_storage(self):
        """Initialize all providers and active model from disk storage."""
        # Load built-in providers
        for builtin in self.builtin_providers.values():
            provider = self.load_provider(builtin.id, is_builtin=True)
            if provider:
                # inherit user-configured base_url only when freeze_url=False
                if not builtin.freeze_url:
                    builtin.base_url = provider.base_url
                builtin.api_key = provider.api_key
                builtin.extra_models = provider.extra_models
                builtin.model_configs = provider.model_configs
        # Load custom providers
        for provider_file in self._repository.custom_provider_paths(
            self.tenant_id,
        ):
            provider = self.load_provider(provider_file.stem, is_builtin=False)
            if provider:
                self.custom_providers[provider.id] = provider
        # Load active model config
        active_model = self.load_active_model()
        if active_model:
            self.active_model = active_model

    def _apply_default_annotations(self):
        """Apply doc-based default annotations for unprobed models.

        Models that already carry static annotations (supports_image /
        supports_video set at definition time) only need the derived
        supports_multimodal flag computed.  Models with no annotations
        at all fall back to the ExpectedCapabilityRegistry.
        """
        from .capability_baseline import ExpectedCapabilityRegistry

        registry = ExpectedCapabilityRegistry()
        for provider in self.builtin_providers.values():
            for model in provider.models:
                # Already fully annotated (e.g. by a prior probe) → skip
                if model.supports_multimodal is not None:
                    continue

                # Static annotations present → compute derived flag only
                if (
                    model.supports_image is not None
                    or model.supports_video is not None
                ):
                    model.supports_multimodal = bool(
                        model.supports_image or model.supports_video,
                    )
                    continue

                # No annotations at all → fall back to registry
                expected = registry.get_expected(provider.id, model.id)
                if expected:
                    model.supports_image = expected.expected_image
                    model.supports_video = expected.expected_video
                    model.supports_multimodal = bool(
                        expected.expected_image or expected.expected_video,
                    )
                    model.probe_source = "documentation"
