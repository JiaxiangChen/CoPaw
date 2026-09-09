# -*- coding: utf-8 -*-
"""Contract tests for Provider persistence, cache, and catalog boundaries."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from swe.config.context import tenant_context
from swe.providers.provider import ModelInfo
from swe.providers.models import ModelSlotConfig
from swe.providers.openai_provider import OpenAIProvider
from swe.providers.provider_runtime_cache import ProviderRuntimeCache
from swe.providers.tenant_provider_repository import TenantProviderRepository


def test_provider_services_exports_the_catalog_service() -> None:
    from swe.providers.provider_catalog_service import ProviderCatalogService
    from swe.providers.provider_services import (
        ProviderCatalogService as Legacy,
    )

    assert Legacy is ProviderCatalogService


@pytest.mark.parametrize(
    "submodule_name",
    ["provider_manager", "retry_chat_model"],
)
def test_provider_package_resolves_submodules_after_attribute_cleanup(
    submodule_name: str,
) -> None:
    providers = importlib.import_module("swe.providers")
    qualified_name = f"swe.providers.{submodule_name}"
    expected = importlib.import_module(qualified_name)
    providers.__dict__.pop(submodule_name, None)

    resolved = getattr(providers, submodule_name)

    assert resolved is expected
    assert getattr(providers, submodule_name) is expected


def test_catalog_constructor_resolves_optional_manager_seams() -> None:
    from swe.providers.provider_catalog_service import ProviderCatalogService

    class FakeManager:
        def __init__(self) -> None:
            self.repository = object()
            self.runtime_cache = object()

        def _catalog_seams(self) -> tuple[object, object]:
            return self.repository, self.runtime_cache

    manager = FakeManager()

    catalog = ProviderCatalogService(manager)

    assert catalog._repository is manager.repository
    assert catalog._runtime_cache is manager.runtime_cache


def test_runtime_cache_uses_injected_model_cache_reset() -> None:
    cache = ProviderRuntimeCache()
    invalidated_scopes: list[str] = []
    cache.set_model_cache_reset(invalidated_scopes.append)

    cache.invalidate_provider_scope("tenant-a")

    assert invalidated_scopes == ["tenant-a"]


def test_updating_model_config_invalidates_tenant_model_caches() -> None:
    from swe.providers.provider_catalog_service import ProviderCatalogService

    provider = OpenAIProvider(
        id="openai",
        name="OpenAI",
        models=[ModelInfo(id="gpt-5", name="GPT-5")],
    )

    class FakeManager:
        tenant_id = "tenant-a"
        builtin_providers = {"openai": provider}
        custom_providers: dict[str, OpenAIProvider] = {}

        def __init__(self) -> None:
            self.saved: list[OpenAIProvider] = []

        def get_provider(self, provider_id: str) -> OpenAIProvider | None:
            return provider if provider_id == "openai" else None

        def _save_provider(
            self,
            saved_provider: OpenAIProvider,
            *,
            is_builtin: bool,
        ) -> None:
            assert is_builtin is True
            self.saved.append(saved_provider)

    class FakeRuntimeCache:
        def __init__(self) -> None:
            self.invalidated_scopes: list[str] = []

        def invalidate_provider_scope(self, scope: str) -> None:
            self.invalidated_scopes.append(scope)

    manager = FakeManager()
    runtime_cache = FakeRuntimeCache()
    catalog = ProviderCatalogService(
        manager,
        repository=object(),
        runtime_cache=runtime_cache,
    )

    config = catalog.update_model_config(
        "openai",
        "gpt-5",
        {"temperature": 0.2},
    )

    assert config.temperature == 0.2
    assert manager.saved == [provider]
    assert runtime_cache.invalidated_scopes == ["tenant-a"]


def test_catalog_deletes_custom_provider_through_repository_seam() -> None:
    from swe.providers.provider_catalog_service import ProviderCatalogService

    class FakeManager:
        tenant_id = "tenant-a"
        custom_providers = {"custom-provider": object()}

    class FakeRepository:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str, bool]] = []
            self.discarded_tokens: list[tuple[str, str, bool]] = []

        def delete_provider(
            self,
            scope: str,
            provider_id: str,
            *,
            is_builtin: bool,
        ) -> None:
            self.deleted.append((scope, provider_id, is_builtin))

        def discard_provider_freshness_token(
            self,
            scope: str,
            provider_id: str,
            *,
            is_builtin: bool,
        ) -> None:
            self.discarded_tokens.append((scope, provider_id, is_builtin))

    class FakeRuntimeCache:
        def __init__(self) -> None:
            self.invalidated_scopes: list[str] = []

        def invalidate_provider_scope(self, scope: str) -> None:
            self.invalidated_scopes.append(scope)

    manager = FakeManager()
    repository = FakeRepository()
    runtime_cache = FakeRuntimeCache()

    catalog = ProviderCatalogService(
        manager,
        repository=repository,
        runtime_cache=runtime_cache,
    )

    assert catalog.remove_custom_provider("custom-provider") is True
    assert manager.custom_providers == {}
    assert repository.deleted == [("tenant-a", "custom-provider", False)]
    assert repository.discarded_tokens == [
        ("tenant-a", "custom-provider", False),
    ]
    assert runtime_cache.invalidated_scopes == ["tenant-a"]


def test_repository_deletion_discards_collected_freshness_token(
    repository: TenantProviderRepository,
    provider: OpenAIProvider,
) -> None:
    provider_path = repository.write_provider(
        "tenant-a",
        provider.model_dump(),
        is_builtin=False,
    )
    freshness_tokens = repository.collect_freshness_tokens("tenant-a", [])

    repository.delete_provider(
        "tenant-a",
        provider.id,
        is_builtin=False,
    )
    repository.discard_provider_freshness_token(
        "tenant-a",
        provider.id,
        is_builtin=False,
    )

    assert not provider_path.exists()
    assert str(provider_path) not in freshness_tokens


@pytest.fixture
def provider() -> OpenAIProvider:
    return OpenAIProvider(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key="sk-test-provider-boundary",
        models=[ModelInfo(id="gpt-5", name="GPT-5")],
        freeze_url=True,
    )


@pytest.fixture
def repository(tmp_path) -> TenantProviderRepository:
    return TenantProviderRepository(tmp_path / ".swe.secret")


def test_repository_uses_tenant_scoped_builtin_and_custom_paths(
    repository: TenantProviderRepository,
) -> None:
    paths = repository.prepare_scope("tenant-a")

    assert paths.root == repository.secret_dir / "tenant-a" / "providers"
    assert paths.builtin == paths.root / "builtin"
    assert paths.custom == paths.root / "custom"
    assert paths.builtin.is_dir()
    assert paths.custom.is_dir()


def test_repository_preserves_current_provider_json_bytes_and_tracks_writes(
    repository: TenantProviderRepository,
    provider: OpenAIProvider,
) -> None:
    before = repository.freshness_token("tenant-a")

    repository.write_provider(
        "tenant-a",
        provider.model_dump(),
        is_builtin=True,
    )

    provider_path = repository.builtin_path("tenant-a") / "openai.json"
    assert provider_path.read_bytes() == json.dumps(
        provider.model_dump(),
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    assert repository.read_provider("tenant-a", "openai", is_builtin=True) == (
        provider.model_dump()
    )
    assert repository.freshness_token("tenant-a") != before


def test_repository_reads_and_writes_active_model_and_ignores_invalid_json(
    repository: TenantProviderRepository,
) -> None:
    active_model = ModelSlotConfig(provider_id="openai", model="gpt-5")

    repository.write_active_model("tenant-a", active_model)

    active_path = repository.root_path("tenant-a") / "active_model.json"
    assert active_path.read_bytes() == json.dumps(
        active_model.model_dump(),
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    assert repository.read_active_model("tenant-a") == active_model

    active_path.write_text("{invalid-json", encoding="utf-8")
    assert repository.read_active_model("tenant-a") is None


def test_repository_seeds_a_new_scope_from_default_without_overwriting_itself(
    repository: TenantProviderRepository,
    provider: OpenAIProvider,
) -> None:
    repository.write_provider(
        "default",
        provider.model_dump(),
        is_builtin=True,
    )
    repository.write_active_model(
        "default",
        ModelSlotConfig(provider_id="openai", model="gpt-5"),
    )

    repository.prepare_scope("tenant-a")

    assert (
        repository.read_provider(
            "tenant-a",
            "openai",
            is_builtin=True,
        )
        == provider.model_dump()
    )
    assert repository.read_active_model("tenant-a") == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5",
    )
    assert repository.root_path("tenant-a") != repository.root_path("default")


@pytest.mark.asyncio
async def test_runtime_cache_single_flights_same_scope() -> None:
    cache = ProviderRuntimeCache()
    started = asyncio.Event()
    release = asyncio.Event()
    builds = 0

    async def build() -> str:
        nonlocal builds
        builds += 1
        started.set()
        await release.wait()
        return "scope-a-state"

    first = asyncio.create_task(cache.get_or_create("scope-a", build))
    await started.wait()
    second = asyncio.create_task(cache.get_or_create("scope-a", build))
    await asyncio.sleep(0)
    assert builds == 1

    release.set()
    assert await asyncio.gather(first, second) == [
        "scope-a-state",
        "scope-a-state",
    ]


@pytest.mark.asyncio
async def test_runtime_cache_builds_different_scopes_concurrently() -> None:
    cache = ProviderRuntimeCache()
    started_a = asyncio.Event()
    started_b = asyncio.Event()
    release = asyncio.Event()

    async def build_a() -> str:
        started_a.set()
        await release.wait()
        return "scope-a-state"

    async def build_b() -> str:
        started_b.set()
        await release.wait()
        return "scope-b-state"

    task_a = asyncio.create_task(cache.get_or_create("scope-a", build_a))
    task_b = asyncio.create_task(cache.get_or_create("scope-b", build_b))
    await asyncio.wait_for(
        asyncio.gather(started_a.wait(), started_b.wait()),
        timeout=0.2,
    )

    release.set()
    assert await asyncio.gather(task_a, task_b) == [
        "scope-a-state",
        "scope-b-state",
    ]


@pytest.mark.asyncio
async def test_runtime_cache_clears_a_failed_single_flight_for_retry() -> None:
    cache = ProviderRuntimeCache()
    attempts = 0

    async def build() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient provider initialization failure")
        return "recovered-state"

    with pytest.raises(RuntimeError, match="transient provider"):
        await cache.get_or_create("scope-a", build)

    assert await cache.get_or_create("scope-a", build) == "recovered-state"
    assert attempts == 2


@pytest.mark.asyncio
async def test_runtime_cache_refreshes_due_scope_once() -> None:
    cache = ProviderRuntimeCache(freshness_ttl_seconds=60)
    refreshes = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        started.set()
        await release.wait()

    cache.mark_freshness_due("scope-a")
    first = asyncio.create_task(cache.refresh_if_due("scope-a", refresh))
    await started.wait()
    second = asyncio.create_task(cache.refresh_if_due("scope-a", refresh))
    await asyncio.sleep(0)
    assert refreshes == 1
    release.set()
    await asyncio.gather(first, second)
    await cache.refresh_if_due("scope-a", refresh)
    assert refreshes == 1


@pytest.mark.asyncio
async def test_runtime_cache_keeps_refresh_due_when_invalidated_inflight() -> (
    None
):
    cache = ProviderRuntimeCache(freshness_ttl_seconds=60)
    started = asyncio.Event()
    release = asyncio.Event()
    refreshes = 0

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        started.set()
        await release.wait()

    cache.mark_freshness_due("scope-a")
    first = asyncio.create_task(cache.refresh_if_due("scope-a", refresh))
    await started.wait()
    cache.invalidate("scope-a")
    release.set()
    await first

    await cache.refresh_if_due("scope-a", refresh)

    assert refreshes == 2


@pytest.mark.asyncio
async def test_runtime_cache_invalidation_evicts_only_the_written_scope() -> (
    None
):
    cache = ProviderRuntimeCache()
    builds: list[str] = []

    async def build(scope: str) -> str:
        builds.append(scope)
        return f"{scope}-{len(builds)}"

    first_a = await cache.get_or_create("scope-a", lambda: build("scope-a"))
    first_b = await cache.get_or_create("scope-b", lambda: build("scope-b"))
    cache.invalidate("scope-a")

    second_a = await cache.get_or_create("scope-a", lambda: build("scope-a"))
    second_b = await cache.get_or_create("scope-b", lambda: build("scope-b"))

    assert (first_a, first_b, second_a, second_b) == (
        "scope-a-1",
        "scope-b-2",
        "scope-a-3",
        "scope-b-2",
    )


def test_repository_concurrent_scope_seed_produces_complete_template_copy(
    repository: TenantProviderRepository,
    provider: OpenAIProvider,
) -> None:
    repository.write_provider(
        "default",
        provider.model_dump(),
        is_builtin=True,
    )
    repository.write_active_model(
        "default",
        ModelSlotConfig(provider_id="openai", model="gpt-5"),
    )
    barrier = threading.Barrier(2)

    def prepare() -> None:
        barrier.wait()
        repository.prepare_scope("tenant-a")

    first = threading.Thread(target=prepare)
    second = threading.Thread(target=prepare)
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert repository.read_provider("tenant-a", "openai", is_builtin=True) == (
        provider.model_dump()
    )
    assert repository.read_active_model("tenant-a") == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5",
    )


def test_repository_waits_for_an_incomplete_template_copy_before_returning(
    repository: TenantProviderRepository,
    provider: OpenAIProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository.write_provider(
        "default",
        provider.model_dump(),
        is_builtin=True,
    )
    repository.write_active_model(
        "default",
        ModelSlotConfig(provider_id="openai", model="gpt-5"),
    )
    template = repository.root_path("default")
    started = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    original_copytree = shutil.copytree

    def copytree_with_mid_copy_pause(
        source,
        destination,
        *args,
        **kwargs,
    ):  # noqa: ANN001
        if Path(source) != template:
            return original_copytree(source, destination, *args, **kwargs)
        destination_path = Path(destination)
        destination_path.mkdir()
        (destination_path / "builtin").mkdir()
        shutil.copy2(
            template / "builtin" / "openai.json",
            destination_path / "builtin" / "openai.json",
        )
        started.set()
        assert release.wait(timeout=2)
        (destination_path / "custom").mkdir()
        shutil.copy2(
            template / "active_model.json",
            destination_path / "active_model.json",
        )
        return destination_path

    monkeypatch.setattr(
        sys.modules[TenantProviderRepository.__module__].shutil,
        "copytree",
        copytree_with_mid_copy_pause,
    )

    first = threading.Thread(
        target=lambda: repository.prepare_scope("tenant-a"),
    )

    def prepare_second_scope() -> None:
        repository.prepare_scope("tenant-a")
        second_done.set()

    second = threading.Thread(target=prepare_second_scope)
    first.start()
    assert started.wait(timeout=2)
    second.start()
    assert not second_done.wait(timeout=0.1)

    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert repository.read_provider("tenant-a", "openai", is_builtin=True) == (
        provider.model_dump()
    )
    assert repository.read_active_model("tenant-a") == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5",
    )


def test_repository_waits_for_source_template_publication_before_seeding_scopes(
    repository: TenantProviderRepository,
    provider: OpenAIProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository.write_provider(
        "default",
        provider.model_dump(),
        is_builtin=True,
    )
    repository.write_active_model(
        "default",
        ModelSlotConfig(provider_id="openai", model="gpt-5"),
    )
    default_template = repository.root_path("default")
    started = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    original_copytree = shutil.copytree

    def copytree_with_mid_copy_pause(
        source,
        destination,
        *args,
        **kwargs,
    ):  # noqa: ANN001
        if Path(source) != default_template:
            return original_copytree(source, destination, *args, **kwargs)
        destination_path = Path(destination)
        destination_path.mkdir()
        (destination_path / "builtin").mkdir()
        shutil.copy2(
            default_template / "builtin" / "openai.json",
            destination_path / "builtin" / "openai.json",
        )
        started.set()
        assert release.wait(timeout=2)
        (destination_path / "custom").mkdir()
        shutil.copy2(
            default_template / "active_model.json",
            destination_path / "active_model.json",
        )
        return destination_path

    monkeypatch.setattr(
        sys.modules[TenantProviderRepository.__module__].shutil,
        "copytree",
        copytree_with_mid_copy_pause,
    )

    def prepare(scope: str, done: threading.Event | None = None) -> None:
        with tenant_context(source_id="source-a"):
            repository.prepare_scope(scope)
        if done is not None:
            done.set()

    first = threading.Thread(target=lambda: prepare("tenant-a"))
    second = threading.Thread(
        target=lambda: prepare("tenant-b", second_done),
    )
    first.start()
    assert started.wait(timeout=2)
    second.start()
    second_returned_before_template_published = second_done.wait(timeout=0.1)

    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not second_returned_before_template_published
    assert repository.read_provider("tenant-a", "openai", is_builtin=True) == (
        provider.model_dump()
    )
    assert repository.read_active_model("tenant-a") == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5",
    )
    assert repository.read_provider("tenant-b", "openai", is_builtin=True) == (
        provider.model_dump()
    )
    assert repository.read_active_model("tenant-b") == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5",
    )


def test_runtime_cache_reset_does_not_repopulate_after_running_build_completes() -> (
    None
):
    cache = ProviderRuntimeCache()
    started = threading.Event()
    release = threading.Event()

    def build(scope: str) -> str:
        started.set()
        assert release.wait(timeout=2)
        return f"{scope}-state"

    future = cache.get_or_start_instance("scope-a", build)
    assert started.wait(timeout=2)
    cache.reset_instances()
    release.set()

    assert future.result(timeout=2) == "scope-a-state"
    assert cache.instances == {}
    assert cache.instance_inflight == {}


def test_repository_replaces_provider_json_from_a_same_directory_temp_file(
    repository: TenantProviderRepository,
    provider: OpenAIProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacements: list[tuple[os.PathLike[str], os.PathLike[str]]] = []
    original_replace = os.replace

    def record_replace(source, destination):  # noqa: ANN001
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", record_replace)

    provider_path = repository.write_provider(
        "tenant-a",
        provider.model_dump(),
        is_builtin=True,
    )

    provider_replacements = [
        replacement
        for replacement in replacements
        if replacement[1] == provider_path
    ]
    assert len(provider_replacements) == 1
    temporary_source, replacement_destination = provider_replacements[0]
    assert replacement_destination == provider_path
    temporary_path = os.fspath(temporary_source)
    assert os.path.dirname(temporary_path) == os.fspath(provider_path.parent)
    assert not os.path.exists(temporary_path)
    assert (
        json.loads(provider_path.read_text(encoding="utf-8"))
        == provider.model_dump()
    )
