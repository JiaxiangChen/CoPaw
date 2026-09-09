# -*- coding: utf-8 -*-
"""Provider catalog operations behind the :class:`ProviderManager` facade."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any

from swe.providers.models import ModelSlotConfig
from swe.providers.provider import ModelInfo, ModelRuntimeConfig, ProviderInfo
from swe.providers.provider_runtime_cache import ProviderRuntimeCache
from swe.providers.tenant_provider_repository import TenantProviderRepository

if TYPE_CHECKING:
    from swe.providers.provider_manager import ProviderManager


logger = logging.getLogger(__name__)

_PROVIDER_INFO_SLOW_LOG_MS = 100


class ProviderCatalogService:
    """Own provider catalog mutations while preserving the manager facade.

    The manager supplies the tenant-scoped provider state and persistence
    compatibility hooks.  This service decides catalog behaviour: provider
    info composition, collisions, validation, persistence ordering, and cache
    invalidation.  It deliberately never constructs provider file paths.
    """

    def __init__(
        self,
        manager: "ProviderManager",
        *,
        repository: TenantProviderRepository | None = None,
        runtime_cache: ProviderRuntimeCache | None = None,
    ) -> None:
        if repository is None or runtime_cache is None:
            manager_repository, manager_runtime_cache = (
                manager._catalog_seams()
            )
            repository = repository or manager_repository
            runtime_cache = runtime_cache or manager_runtime_cache
        self._manager = manager
        self._repository = repository
        self._runtime_cache = runtime_cache

    async def list_provider_info(self) -> list[ProviderInfo]:
        manager = self._manager
        started_at = time.perf_counter()
        refresh_started_at = time.perf_counter()
        await manager.refresh_if_due()
        refresh_ms = int((time.perf_counter() - refresh_started_at) * 1000)
        providers = [
            ("builtin", provider)
            for provider in manager.builtin_providers.values()
        ]
        providers.extend(
            ("custom", provider)
            for provider in manager.custom_providers.values()
        )
        gather_started_at = time.perf_counter()
        provider_results = await asyncio.gather(
            *[
                manager._get_provider_info_with_timing(provider, provider_kind)
                for provider_kind, provider in providers
            ],
        )
        gather_ms = int((time.perf_counter() - gather_started_at) * 1000)
        provider_infos = [info for info, _ in provider_results]
        timings = [
            (provider.id, provider_kind, duration_ms)
            for (provider_kind, provider), (_, duration_ms) in zip(
                providers,
                provider_results,
            )
        ]
        max_provider_id = ""
        max_provider_kind = ""
        max_provider_ms = 0
        if timings:
            max_provider_id, max_provider_kind, max_provider_ms = max(
                timings,
                key=lambda item: item[2],
            )
        model_count = sum(len(provider.models) for provider in provider_infos)
        extra_model_count = sum(
            len(provider.extra_models) for provider in provider_infos
        )
        logger.info(
            "provider_list_provider_info_done tenant_id=%s total_ms=%d "
            "refresh_ms=%d gather_ms=%d provider_count=%d builtin_count=%d "
            "custom_count=%d model_count=%d extra_model_count=%d "
            "max_provider_id=%s max_provider_kind=%s max_provider_ms=%d "
            "freshness_token_count=%d root_path=%s",
            manager.tenant_id,
            int((time.perf_counter() - started_at) * 1000),
            refresh_ms,
            gather_ms,
            len(provider_infos),
            len(manager.builtin_providers),
            len(manager.custom_providers),
            model_count,
            extra_model_count,
            max_provider_id,
            max_provider_kind,
            max_provider_ms,
            len(manager._file_freshness_tokens),
            manager.root_path,
        )
        return list(provider_infos)

    async def get_provider_info(
        self,
        provider_id: str,
    ) -> ProviderInfo | None:
        manager = self._manager
        await manager.refresh_if_due()
        provider = manager.get_provider(provider_id)
        return await provider.get_info() if provider else None

    def update_provider(
        self,
        provider_id: str,
        config: dict[str, Any],
    ) -> bool:
        manager = self._manager
        provider = manager.get_provider(provider_id)
        if not provider:
            return False
        provider.update_config(config)
        manager._save_provider(
            provider,
            is_builtin=provider_id in manager.builtin_providers,
        )
        self._invalidate_after_write()
        return True

    def get_model_config(
        self,
        provider_id: str,
        model_id: str,
    ) -> ModelRuntimeConfig:
        """Return a model's config after verifying its catalog membership."""
        provider = self._require_provider_model(provider_id, model_id)
        return provider.get_model_config(model_id)

    def update_model_config(
        self,
        provider_id: str,
        model_id: str,
        updates: dict[str, Any],
    ) -> ModelRuntimeConfig:
        """Persist a partial update for one model without replacing siblings."""
        manager = self._manager
        provider = self._require_provider_model(provider_id, model_id)
        config = provider.update_model_config(model_id, updates)
        manager._save_provider(
            provider,
            is_builtin=provider_id in manager.builtin_providers,
        )
        self._invalidate_after_write()
        return config

    async def fetch_provider_models(self, provider_id: str) -> list[ModelInfo]:
        manager = self._manager
        provider = manager.get_provider(provider_id)
        if not provider:
            return []
        try:
            models = await provider.fetch_models()
            provider.extra_models = models
            await manager._save_provider_async(
                provider,
                is_builtin=provider_id in manager.builtin_providers,
            )
            self._reset_model_caches()
            return models
        except Exception as exc:
            logger.warning(
                "Failed to fetch models for provider '%s': %s",
                provider_id,
                exc,
            )
            return []

    def resolve_custom_provider_id(self, provider_id: str) -> str:
        manager = self._manager
        base_id = provider_id
        if base_id in manager.builtin_providers:
            base_id = f"{base_id}-custom"

        resolved_id = base_id
        while (
            resolved_id in manager.builtin_providers
            or resolved_id in manager.custom_providers
        ):
            resolved_id = f"{resolved_id}-new"
        return resolved_id

    async def add_custom_provider(
        self,
        provider_data: ProviderInfo,
    ) -> ProviderInfo:
        manager = self._manager
        provider_payload = provider_data.model_dump()
        provider_payload["id"] = self.resolve_custom_provider_id(
            provider_data.id,
        )
        provider_payload["is_custom"] = True
        provider = manager._provider_from_data(provider_payload)
        provider.support_connection_check = False
        manager.custom_providers[provider.id] = provider
        await manager._save_provider_async(provider, is_builtin=False)
        self._reset_model_caches()
        return await provider.get_info()

    def remove_custom_provider(self, provider_id: str) -> bool:
        manager = self._manager
        if provider_id not in manager.custom_providers:
            return False
        del manager.custom_providers[provider_id]
        self._delete_provider(
            manager.tenant_id,
            provider_id,
            is_builtin=False,
        )
        self._invalidate_after_write()
        return True

    async def activate_model(self, provider_id: str, model_id: str) -> None:
        manager = self._manager
        provider = manager.get_provider(provider_id)
        if not provider:
            raise ValueError(f"Provider '{provider_id}' not found.")
        if not provider.has_model(model_id):
            raise ValueError(
                f"Model '{model_id}' not found in provider '{provider_id}'.",
            )
        manager.active_model = ModelSlotConfig(
            provider_id=provider_id,
            model=model_id,
        )
        await self._save_active_model(manager.active_model)
        self._reset_model_caches()
        manager.maybe_probe_multimodal(provider_id, model_id)

    def maybe_probe_multimodal(self, provider_id: str, model_id: str) -> None:
        provider = self._manager.get_provider(provider_id)
        for model in provider.models + provider.extra_models:
            if model.id == model_id and model.supports_multimodal is None:
                asyncio.create_task(
                    self._auto_probe_multimodal(provider_id, model_id),
                )
                break

    async def _auto_probe_multimodal(
        self,
        provider_id: str,
        model_id: str,
    ) -> None:
        try:
            result = await self.probe_model_multimodal(provider_id, model_id)
            logger.info(
                "Auto-probe for %s/%s: image=%s, video=%s",
                provider_id,
                model_id,
                result.get("supports_image"),
                result.get("supports_video"),
            )
        except Exception as exc:
            logger.warning("Auto-probe multimodal failed: %s", exc)

    async def add_model_to_provider(
        self,
        provider_id: str,
        model_info: ModelInfo,
    ) -> ProviderInfo:
        manager = self._manager
        provider = manager.get_provider(provider_id)
        if not provider:
            raise ValueError(f"Provider '{provider_id}' not found.")
        await provider.add_model(model_info)
        await manager._save_provider_async(
            provider,
            is_builtin=provider_id in manager.builtin_providers,
        )
        self._reset_model_caches()
        return await provider.get_info()

    async def delete_model_from_provider(
        self,
        provider_id: str,
        model_id: str,
    ) -> ProviderInfo:
        manager = self._manager
        provider = manager.get_provider(provider_id)
        if not provider:
            raise ValueError(f"Provider '{provider_id}' not found.")
        await provider.delete_model(model_id=model_id)
        provider.delete_model_config(model_id)
        await manager._save_provider_async(
            provider,
            is_builtin=provider_id in manager.builtin_providers,
        )
        self._reset_model_caches()
        return await provider.get_info()

    def _require_provider_model(self, provider_id: str, model_id: str):
        provider = self._manager.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found.")
        if not provider.has_model(model_id):
            raise ValueError(
                f"Model '{model_id}' not found in provider '{provider_id}'.",
            )
        return provider

    async def probe_model_multimodal(
        self,
        provider_id: str,
        model_id: str,
    ) -> dict:
        manager = self._manager
        provider = manager.get_provider(provider_id)
        if not provider:
            return {"error": f"Provider '{provider_id}' not found"}

        result = await provider.probe_model_multimodal(model_id)
        for model in provider.models + provider.extra_models:
            if model.id == model_id:
                model.supports_image = result.supports_image
                model.supports_video = result.supports_video
                model.supports_multimodal = result.supports_multimodal
                model.probe_source = "probed"
                break

        from .capability_baseline import (
            ExpectedCapabilityRegistry,
            compare_probe_result,
        )

        expected = ExpectedCapabilityRegistry().get_expected(
            provider_id,
            model_id,
        )
        if expected:
            for discrepancy in compare_probe_result(
                expected,
                result.supports_image,
                result.supports_video,
            ):
                logger.warning(
                    "Probe discrepancy: %s/%s %s expected=%s actual=%s (%s)",
                    discrepancy.provider_id,
                    discrepancy.model_id,
                    discrepancy.field,
                    discrepancy.expected,
                    discrepancy.actual,
                    discrepancy.discrepancy_type,
                )

        await manager._save_provider_async(
            provider,
            is_builtin=provider_id in manager.builtin_providers,
        )
        self._reset_model_caches()
        return {
            "supports_image": result.supports_image,
            "supports_video": result.supports_video,
            "supports_multimodal": result.supports_multimodal,
            "image_message": result.image_message,
            "video_message": result.video_message,
        }

    def overwrite_provider_payload(self, payload: dict[str, Any]):
        manager = self._manager
        provider = manager._provider_from_data(payload)
        is_builtin = not provider.is_custom
        if is_builtin:
            manager.custom_providers.pop(provider.id, None)
            self._delete_provider(
                manager.tenant_id,
                provider.id,
                is_builtin=False,
            )
            manager.builtin_providers[provider.id] = provider
        else:
            manager.builtin_providers.pop(provider.id, None)
            self._delete_provider(
                manager.tenant_id,
                provider.id,
                is_builtin=True,
            )
            manager.custom_providers[provider.id] = provider
        manager._save_provider(provider, is_builtin=is_builtin)
        self._invalidate_after_write()
        return provider

    def _invalidate_after_write(self) -> None:
        self._runtime_cache.invalidate_provider_scope(
            self._manager.tenant_id,
        )

    def _delete_provider(
        self,
        scope: str,
        provider_id: str,
        *,
        is_builtin: bool,
    ) -> None:
        if self._repository is None:
            raise RuntimeError("Provider repository is not configured.")
        self._repository.delete_provider(
            scope,
            provider_id,
            is_builtin=is_builtin,
        )
        self._repository.discard_provider_freshness_token(
            scope,
            provider_id,
            is_builtin=is_builtin,
        )

    async def _save_active_model(
        self,
        active_model: ModelSlotConfig,
    ) -> None:
        save_async = getattr(self._manager, "_save_active_model_async", None)
        if save_async is not None:
            await save_async(active_model)
            return
        result = self._manager.save_active_model(active_model)
        if inspect.isawaitable(result):
            await result

    def _reset_model_caches(self) -> None:
        self._runtime_cache.reset_scope_bound_model_caches(
            self._manager.tenant_id,
        )
