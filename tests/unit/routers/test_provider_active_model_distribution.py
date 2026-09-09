# -*- coding: utf-8 -*-
"""Active-model distribution router tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from swe.app.routers import providers as providers_router
from swe.config.context import encode_scope_id, tenant_context
from swe.providers.provider_manager import ProviderManager
from swe.providers.models import ModelSlotConfig


class FakeTenantWorkspacePool:
    """Record tenant bootstrap requests from active-model distribution."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def ensure_bootstrap(
        self,
        tenant_id: str,
        *,
        source_id: str | None = None,
    ) -> None:
        self.calls.append((tenant_id, source_id))


def _request(
    tenant_id: str = "tenant-source",
    source_id: str | None = None,
    scope_id: str | None = None,
    headers: dict[str, str] | None = None,
    app: Any | None = None,
) -> SimpleNamespace:
    state = app.state if app is not None else None
    if state is None:
        state = SimpleNamespace()
    if not hasattr(state, "tenant_workspace_pool"):
        state.tenant_workspace_pool = FakeTenantWorkspacePool()
    request = SimpleNamespace(
        headers=headers or {},
        state=SimpleNamespace(
            tenant_id=tenant_id,
            source_id=source_id,
            scope_id=scope_id,
            tenant_workspace_pool=state.tenant_workspace_pool,
        ),
    )
    if app is not None:
        request.app = app
    return request


class FakeAsyncTaskDb:
    """提供异步任务写入器所需的数据库连接状态。"""

    is_connected = True


class DisconnectedAsyncTaskDb(FakeAsyncTaskDb):
    """模拟连接状态标记为断开但仍可执行写入的任务库。"""

    is_connected = False


class LazyAsyncTaskDb(FakeAsyncTaskDb):
    """模拟从配置懒加载出来的任务库连接。"""

    connected = False

    async def connect(self) -> None:
        self.connected = True


def _patch_resolve_identity(
    monkeypatch: pytest.MonkeyPatch,
    names: dict[str, str] | None = None,
) -> None:
    """替换分发目标身份解析，避免单测触发远端查询。"""
    name_map = names or {"tenant-a": "用户A"}

    async def fake_resolve_user_identity(**kwargs):  # noqa: ANN003
        tenant_id = kwargs["tenant_id"]
        return SimpleNamespace(
            user_name=name_map.get(tenant_id),
            bbk_id=None,
        )

    monkeypatch.setattr(
        providers_router,
        "resolve_user_identity",
        fake_resolve_user_identity,
    )


@dataclass
class FakeProvider:
    id: str
    models: list[dict[str, Any]] = field(default_factory=list)
    extra_models: list[dict[str, Any]] = field(default_factory=list)
    is_custom: bool = False
    name: str = ""
    api_key: str = ""
    base_url: str = ""
    chat_model: str = "OpenAIChatModel"
    model_configs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def has_model(self, model_id: str) -> bool:
        return any(
            item["id"] == model_id
            for item in [*self.models, *self.extra_models]
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "chat_model": self.chat_model,
            "models": self.models,
            "extra_models": self.extra_models,
            "is_custom": self.is_custom,
            "model_configs": self.model_configs,
        }


class FakeManager:
    def __init__(
        self,
        *,
        active_model: ModelSlotConfig | None = None,
        providers: dict[str, FakeProvider] | None = None,
        fail_on_overwrite: str | None = None,
        fail_on_activate: str | None = None,
    ) -> None:
        self._active_model = active_model
        self._providers = providers or {}
        self.fail_on_overwrite = fail_on_overwrite
        self.fail_on_activate = fail_on_activate
        self.overwritten_payloads: list[dict[str, Any]] = []
        self.activated: list[tuple[str, str]] = []

    def get_active_model(self) -> ModelSlotConfig | None:
        return self._active_model

    def get_provider(self, provider_id: str) -> FakeProvider | None:
        return self._providers.get(provider_id)

    def overwrite_provider_payload(self, payload: dict[str, Any]) -> None:
        if self.fail_on_overwrite:
            raise RuntimeError(self.fail_on_overwrite)
        self.overwritten_payloads.append(payload)
        provider = FakeProvider(
            id=str(payload["id"]),
            name=str(payload.get("name") or payload["id"]),
            base_url=str(payload.get("base_url") or ""),
            api_key=str(payload.get("api_key") or ""),
            chat_model=str(payload.get("chat_model") or "OpenAIChatModel"),
            models=list(payload.get("models") or []),
            extra_models=list(payload.get("extra_models") or []),
            is_custom=bool(payload.get("is_custom")),
            model_configs=dict(payload.get("model_configs") or {}),
        )
        self._providers[provider.id] = provider

    async def activate_model(self, provider_id: str, model_id: str) -> None:
        if self.fail_on_activate:
            raise RuntimeError(self.fail_on_activate)
        self.activated.append((provider_id, model_id))
        self._active_model = ModelSlotConfig(
            provider_id=provider_id,
            model=model_id,
        )


def _manager_factory(manager: FakeManager):
    def get_instance(tenant_id=None):  # noqa: ANN001
        del tenant_id
        return manager

    return staticmethod(get_instance)


def _working_dir_factory(tmp_path: Path):
    def get_working_dir(tenant_id=None):  # noqa: ANN001
        return tmp_path / str(tenant_id)

    return get_working_dir


def _storage_recorder(calls: list[str | None]):
    def ensure_storage(tenant_id: str | None) -> None:
        calls.append(tenant_id)

    return staticmethod(ensure_storage)


def test_list_active_model_distribution_tenants_returns_discovered_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list_logical_tenant_ids(
        _source_id=None,
        *,
        source_filter=False,
        include_templates=False,
    ):
        assert include_templates is True
        del source_filter
        return ["default_ruice", "tenant-a", "tenant-b"]

    monkeypatch.setattr(
        providers_router,
        "list_logical_tenant_ids",
        fake_list_logical_tenant_ids,
    )

    result = asyncio.run(
        providers_router.list_active_model_distribution_tenants(_request()),
    )

    assert result.tenant_ids == ["default_ruice", "tenant-a", "tenant-b"]


def test_list_active_model_distribution_tenants_maps_source_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str | None] = []

    async def fake_list_logical_tenant_ids(
        source_id: str | None = None,
        *,
        source_filter: bool = False,
        include_templates: bool = False,
    ) -> list[str]:
        assert include_templates is True
        del source_filter
        observed.append(source_id)
        return ["default_ruice", "tenant-a"]

    monkeypatch.setattr(
        providers_router,
        "list_logical_tenant_ids",
        fake_list_logical_tenant_ids,
    )

    result = asyncio.run(
        providers_router.list_active_model_distribution_tenants(
            _request(source_id="ruice"),
        ),
    )

    assert observed == ["ruice"]
    assert result.tenant_ids == ["default_ruice", "tenant-a"]


def test_distribute_active_model_to_bootstrapped_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                name="OpenAI",
                api_key="sk-source",
                base_url="https://api.openai.com/v1",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
                extra_models=[{"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"}],
                model_configs={
                    "gpt-5.4": {"temperature": 0.2},
                    "gpt-5.4-mini": {"temperature": 0.8},
                },
            ),
        },
    )
    target_manager = FakeManager()
    ensured: list[str | None] = []

    monkeypatch.setattr(
        providers_router,
        "get_tenant_storage_working_dir",
        _working_dir_factory(tmp_path),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "ensure_tenant_provider_storage",
        _storage_recorder(ensured),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "get_instance",
        _manager_factory(target_manager),
    )

    class FakeInitializer:
        def __init__(
            self,
            base_working_dir: Path,
            tenant_id: str,
            source_id: str | None = None,
        ):
            assert base_working_dir == tmp_path
            self.tenant_id = tenant_id
            self.source_id = source_id
            self.effective_tenant_id = tenant_id

        def has_seeded_bootstrap(self) -> bool:
            return True

        def ensure_seeded_bootstrap(self) -> dict[str, object]:
            raise AssertionError("should not bootstrap an existing tenant")

    monkeypatch.setattr(providers_router, "TenantInitializer", FakeInitializer)

    result = asyncio.run(
        providers_router.distribute_active_model(
            _request(),
            providers_router.ActiveModelDistributionRequest(
                target_tenant_ids=["tenant-existing"],
                overwrite=True,
            ),
            manager=source_manager,
        ),
    )

    assert result.source_active_llm == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5.4",
    )
    assert len(result.results) == 1
    tenant_result = result.results[0]
    assert tenant_result.success is True
    assert tenant_result.bootstrapped is False
    assert tenant_result.provider_updated == "openai"
    assert tenant_result.active_llm_updated == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5.4",
    )
    assert ensured == ["tenant-existing"]
    assert target_manager.overwritten_payloads[0]["api_key"] == "sk-source"
    assert target_manager.overwritten_payloads[0]["model_configs"] == {
        "gpt-5.4": {"temperature": 0.2},
    }
    assert target_manager.activated == [("openai", "gpt-5.4")]


def test_distribute_active_model_bootstraps_missing_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
            ),
        },
    )
    target_manager = FakeManager()

    monkeypatch.setattr(
        providers_router,
        "get_tenant_storage_working_dir",
        _working_dir_factory(tmp_path),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "ensure_tenant_provider_storage",
        staticmethod(lambda tenant_id: None),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "get_instance",
        staticmethod(lambda tenant_id=None: target_manager),
    )

    class FakeInitializer:
        def __init__(
            self,
            _base_working_dir: Path,
            tenant_id: str,
            source_id: str | None = None,
        ):
            self.tenant_id = tenant_id
            self.source_id = source_id
            self.effective_tenant_id = (
                providers_router.resolve_storage_tenant_id(
                    tenant_id,
                    source_id,
                )
            )

        def has_seeded_bootstrap(self) -> bool:
            return False

    monkeypatch.setattr(providers_router, "TenantInitializer", FakeInitializer)

    request = _request(source_id="ruice")
    result = asyncio.run(
        providers_router.distribute_active_model(
            request,
            providers_router.ActiveModelDistributionRequest(
                target_tenant_ids=["tenant-new"],
                overwrite=True,
            ),
            manager=source_manager,
        ),
    )

    assert request.state.tenant_workspace_pool.calls == [
        ("tenant-new", "ruice"),
    ]
    assert result.results[0].success is True
    assert result.results[0].bootstrapped is True


def test_distribute_active_model_uses_request_scope_for_source_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
            ),
        },
    )
    target_manager = FakeManager()
    scope_id = "scope.v1.dGVuYW50LXNvdXJjZQ.cnVpY2U"
    canonical_scope_id = "dGVuYW50LXNvdXJjZQ.cnVpY2U"
    observed: dict[str, str | None] = {}

    def fake_get_tenant_storage_working_dir(
        tenant_id: str | None,
    ) -> Path:
        observed["tenant_id"] = tenant_id
        return tmp_path / str(tenant_id)

    monkeypatch.setattr(
        providers_router,
        "get_tenant_storage_working_dir",
        fake_get_tenant_storage_working_dir,
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "ensure_tenant_provider_storage",
        staticmethod(lambda tenant_id: None),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "get_instance",
        staticmethod(lambda tenant_id=None: target_manager),
    )

    class FakeInitializer:
        def __init__(
            self,
            _base_working_dir: Path,
            tenant_id: str,
            source_id: str | None = None,
        ):
            self.tenant_id = tenant_id
            self.source_id = source_id

        def has_seeded_bootstrap(self) -> bool:
            return True

        def ensure_seeded_bootstrap(self) -> dict[str, object]:
            return {"minimal": True}

    monkeypatch.setattr(providers_router, "TenantInitializer", FakeInitializer)

    result = asyncio.run(
        providers_router.distribute_active_model(
            _request(
                tenant_id="tenant-source",
                source_id="ruice",
                scope_id=scope_id,
            ),
            providers_router.ActiveModelDistributionRequest(
                target_tenant_ids=["tenant-target"],
                overwrite=True,
            ),
            manager=source_manager,
        ),
    )

    assert observed["tenant_id"] == canonical_scope_id
    assert result.source_active_llm == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5.4",
    )


def test_provider_manager_keeps_explicit_target_scope_under_request_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_dir = tmp_path / "secret"
    monkeypatch.setattr(
        "swe.providers.provider_manager.SECRET_DIR",
        secret_dir,
    )
    ProviderManager.reset_instance_cache()

    target_scope_id = encode_scope_id("tenant-target", "source-b")
    source_scope_id = encode_scope_id("tenant-source", "source-a")

    with tenant_context(
        tenant_id="tenant-source",
        source_id="source-a",
        scope_id=source_scope_id,
    ):
        ProviderManager.ensure_tenant_provider_storage(target_scope_id)
        manager = ProviderManager.get_instance(target_scope_id)

    assert manager.tenant_id == target_scope_id
    assert (secret_dir / target_scope_id / "providers").exists()
    assert not (secret_dir / source_scope_id / "providers").exists()


def test_distribute_active_model_overwrites_builtin_provider_and_switches_active_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                name="OpenAI",
                api_key="sk-new",
                base_url="https://api.openai.com/v1",
                models=[{"id": "gpt-4.1", "name": "GPT-4.1"}],
                extra_models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
                model_configs={"gpt-5.4": {"temperature": 0.3}},
            ),
        },
    )
    target_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-4.1"),
        providers={
            "openai": FakeProvider(
                id="openai",
                api_key="sk-old",
                base_url="https://old.example/v1",
                models=[{"id": "gpt-4.1", "name": "GPT-4.1"}],
            ),
        },
    )

    monkeypatch.setattr(
        providers_router,
        "get_tenant_storage_working_dir",
        _working_dir_factory(tmp_path),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "ensure_tenant_provider_storage",
        staticmethod(lambda tenant_id: None),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "get_instance",
        staticmethod(lambda tenant_id=None: target_manager),
    )
    monkeypatch.setattr(
        providers_router,
        "TenantInitializer",
        lambda base_working_dir, tenant_id, source_id=None: SimpleNamespace(
            effective_tenant_id=providers_router.resolve_storage_tenant_id(
                tenant_id,
                source_id,
            ),
            has_seeded_bootstrap=lambda: True,
            ensure_seeded_bootstrap=lambda: {"minimal": True},
        ),
    )

    result = asyncio.run(
        providers_router.distribute_active_model(
            _request(),
            providers_router.ActiveModelDistributionRequest(
                target_tenant_ids=["tenant-builtin"],
                overwrite=True,
            ),
            manager=source_manager,
        ),
    )

    overwritten = target_manager.get_provider("openai")
    assert overwritten is not None
    assert overwritten.api_key == "sk-new"
    assert overwritten.base_url == "https://api.openai.com/v1"
    assert overwritten.has_model("gpt-5.4") is True
    assert overwritten.model_configs == {"gpt-5.4": {"temperature": 0.3}}
    assert result.results[0].active_llm_updated == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5.4",
    )


def test_distribute_active_model_overwrites_custom_provider_and_switches_active_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_manager = FakeManager(
        active_model=ModelSlotConfig(
            provider_id="corp-gateway",
            model="claude-enterprise",
        ),
        providers={
            "corp-gateway": FakeProvider(
                id="corp-gateway",
                name="Corp Gateway",
                is_custom=True,
                api_key="secret-token",
                base_url="https://corp.example/v1",
                models=[
                    {"id": "claude-enterprise", "name": "Claude Enterprise"},
                ],
                model_configs={"claude-enterprise": {"top_p": 0.9}},
            ),
        },
    )
    target_manager = FakeManager()

    monkeypatch.setattr(
        providers_router,
        "get_tenant_storage_working_dir",
        _working_dir_factory(tmp_path),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "ensure_tenant_provider_storage",
        staticmethod(lambda tenant_id: None),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "get_instance",
        staticmethod(lambda tenant_id=None: target_manager),
    )
    monkeypatch.setattr(
        providers_router,
        "TenantInitializer",
        lambda base_working_dir, tenant_id, source_id=None: SimpleNamespace(
            effective_tenant_id=providers_router.resolve_storage_tenant_id(
                tenant_id,
                source_id,
            ),
            has_seeded_bootstrap=lambda: True,
            ensure_seeded_bootstrap=lambda: {"minimal": True},
        ),
    )

    result = asyncio.run(
        providers_router.distribute_active_model(
            _request(),
            providers_router.ActiveModelDistributionRequest(
                target_tenant_ids=["tenant-custom"],
                overwrite=True,
            ),
            manager=source_manager,
        ),
    )

    overwritten = target_manager.get_provider("corp-gateway")
    assert overwritten is not None
    assert overwritten.is_custom is True
    assert overwritten.api_key == "secret-token"
    assert overwritten.base_url == "https://corp.example/v1"
    assert overwritten.model_configs == {"claude-enterprise": {"top_p": 0.9}}
    assert result.results[0].provider_updated == "corp-gateway"
    assert result.results[0].active_llm_updated == ModelSlotConfig(
        provider_id="corp-gateway",
        model="claude-enterprise",
    )


def test_distribute_active_model_reports_partial_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
            ),
        },
    )
    managers = {
        "tenant-ok": FakeManager(),
        "tenant-fail": FakeManager(fail_on_overwrite="write failed"),
    }

    monkeypatch.setattr(
        providers_router,
        "get_tenant_storage_working_dir",
        _working_dir_factory(tmp_path),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "ensure_tenant_provider_storage",
        staticmethod(lambda tenant_id: None),
    )
    monkeypatch.setattr(
        providers_router.ProviderManager,
        "get_instance",
        staticmethod(lambda tenant_id=None: managers[str(tenant_id)]),
    )
    monkeypatch.setattr(
        providers_router,
        "TenantInitializer",
        lambda base_working_dir, tenant_id, source_id=None: SimpleNamespace(
            effective_tenant_id=providers_router.resolve_storage_tenant_id(
                tenant_id,
                source_id,
            ),
            has_seeded_bootstrap=lambda: True,
            ensure_seeded_bootstrap=lambda: {"minimal": True},
        ),
    )

    result = asyncio.run(
        providers_router.distribute_active_model(
            _request(),
            providers_router.ActiveModelDistributionRequest(
                target_tenant_ids=["tenant-ok", "tenant-fail"],
                overwrite=True,
            ),
            manager=source_manager,
        ),
    )

    assert [item.tenant_id for item in result.results] == [
        "tenant-ok",
        "tenant-fail",
    ]
    assert result.results[0].success is True
    assert result.results[1].success is False
    assert "write failed" in str(result.results[1].error)
    assert managers["tenant-ok"].activated == [("openai", "gpt-5.4")]


def test_distribute_active_model_rejects_missing_overwrite() -> None:
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
            ),
        },
    )

    with pytest.raises(providers_router.HTTPException) as exc_info:
        asyncio.run(
            providers_router.distribute_active_model(
                _request(),
                providers_router.ActiveModelDistributionRequest(
                    target_tenant_ids=["tenant-a"],
                    overwrite=False,
                ),
                manager=source_manager,
            ),
        )

    assert exc_info.value.status_code == 400
    assert "overwrite=true" in str(exc_info.value.detail)


def test_distribute_active_model_returns_async_task_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """提交活跃模型分发后应返回受理中的任务信息。"""
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
            ),
        },
    )
    submitted: dict[str, Any] = {}
    task_ids: list[str] = []
    _patch_resolve_identity(monkeypatch)

    class FakeStore:
        def __init__(self, db) -> None:  # noqa: ANN001
            submitted["db"] = db

        async def start_task(self, **kwargs) -> None:  # noqa: ANN003
            submitted["start_task"] = kwargs

    async def fake_task_runner(*args, **kwargs):  # noqa: ANN001, ANN003
        submitted["runner"] = (args, kwargs)

    def fake_create_task(coro):  # noqa: ANN001
        task_ids.append("scheduled")
        submitted["coroutine"] = coro
        coro.close()
        return object()

    monkeypatch.setattr(
        providers_router,
        "AsyncTaskStore",
        FakeStore,
        raising=False,
    )
    monkeypatch.setattr(
        providers_router.asyncio,
        "create_task",
        fake_create_task,
    )
    monkeypatch.setattr(
        providers_router,
        "_run_active_model_distribution_task",
        fake_task_runner,
        raising=False,
    )

    result = asyncio.run(
        providers_router.distribute_active_model(
            _request(
                headers={
                    "X-User-Id": "operator-1",
                    "X-User-Name": "%E5%BC%A0%E4%B8%89",
                },
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        db_connection=DisconnectedAsyncTaskDb(),
                    ),
                ),
            ),
            providers_router.ActiveModelDistributionRequest(
                target_tenant_ids=["tenant-a"],
                overwrite=True,
            ),
            manager=source_manager,
        ),
    )

    assert result.status == "queued"
    assert result.reused is False
    assert result.task_id
    assert task_ids == ["scheduled"]
    assert isinstance(submitted["db"], DisconnectedAsyncTaskDb)
    assert submitted["start_task"]["task_id"] == result.task_id
    assert (
        submitted["start_task"]["task_type"]
        == "provider.active_model.distribute"
    )
    assert (
        submitted["start_task"]["summary"]
        == "分发模型「openai/gpt-5.4」，目标 1 个用户"
    )
    assert submitted["start_task"]["actor_user_id"] == "operator-1"
    assert submitted["start_task"]["actor_user_name"] == "张三"
    assert submitted["start_task"]["target_names"] == {
        "tenant-a": "用户A",
    }


def test_distribute_active_model_http_response_includes_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """活跃模型分发的 HTTP 响应必须显式返回任务 ID。"""
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
            ),
        },
    )
    _patch_resolve_identity(monkeypatch)

    class FakeStore:
        def __init__(self, _db) -> None:  # noqa: ANN001
            pass

        async def start_task(self, **_kwargs) -> None:  # noqa: ANN003
            return None

    async def fake_manager():
        return source_manager

    async def fake_task_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    app = FastAPI()
    app.state.db_connection = DisconnectedAsyncTaskDb()
    app.state.tenant_workspace_pool = FakeTenantWorkspacePool()
    app.include_router(providers_router.router)
    app.dependency_overrides[providers_router.get_provider_manager] = (
        fake_manager
    )
    monkeypatch.setattr(
        providers_router,
        "AsyncTaskStore",
        FakeStore,
        raising=False,
    )
    monkeypatch.setattr(
        providers_router,
        "_run_active_model_distribution_task",
        fake_task_runner,
        raising=False,
    )

    response = TestClient(app).post(
        "/models/distribution/active-llm",
        json={"target_tenant_ids": ["tenant-a"], "overwrite": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"]
    assert payload["taskId"] == payload["task_id"]


def test_active_model_distribution_lazy_loads_missing_app_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app state 缺少数据库对象时应按配置懒加载任务库。"""
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
            ),
        },
    )
    submitted: dict[str, Any] = {}
    _patch_resolve_identity(monkeypatch)

    class FakeStore:
        def __init__(self, db) -> None:  # noqa: ANN001
            submitted["db"] = db

        async def start_task(self, **kwargs) -> None:  # noqa: ANN003
            submitted["start_task"] = kwargs

    async def fake_task_runner(*args, **kwargs):  # noqa: ANN001, ANN003
        submitted["runner"] = (args, kwargs)

    def fake_create_task(coro):  # noqa: ANN001
        submitted["coroutine"] = coro
        coro.close()
        return object()

    async def fake_get_db(request):  # noqa: ANN001
        db = LazyAsyncTaskDb()
        await db.connect()
        request.app.state.db_connection = db
        return db

    monkeypatch.setattr(
        providers_router,
        "get_or_create_async_task_db",
        fake_get_db,
    )
    monkeypatch.setattr(
        providers_router,
        "AsyncTaskStore",
        FakeStore,
        raising=False,
    )
    monkeypatch.setattr(
        providers_router.asyncio,
        "create_task",
        fake_create_task,
    )
    monkeypatch.setattr(
        providers_router,
        "_run_active_model_distribution_task",
        fake_task_runner,
        raising=False,
    )

    app = SimpleNamespace(state=SimpleNamespace())
    result = asyncio.run(
        providers_router.distribute_active_model(
            _request(app=app),
            providers_router.ActiveModelDistributionRequest(
                target_tenant_ids=["tenant-a"],
                overwrite=True,
            ),
            manager=source_manager,
        ),
    )

    assert result.status == "queued"
    assert result.task_id
    assert submitted["db"].connected is True
    assert app.state.db_connection is submitted["db"]
    assert submitted["start_task"]["task_id"] == result.task_id
    assert submitted["start_task"]["target_names"] == {
        "tenant-a": "用户A",
    }


def test_active_model_distribution_requires_async_task_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型分发必须提交异步任务，缺少任务库时返回明确错误。"""
    source_manager = FakeManager(
        active_model=ModelSlotConfig(provider_id="openai", model="gpt-5.4"),
        providers={
            "openai": FakeProvider(
                id="openai",
                models=[{"id": "gpt-5.4", "name": "GPT-5.4"}],
            ),
        },
    )

    async def no_db(_request):  # noqa: ANN001
        return None

    monkeypatch.setattr(providers_router, "get_or_create_async_task_db", no_db)

    with pytest.raises(providers_router.HTTPException) as exc_info:
        asyncio.run(
            providers_router.distribute_active_model(
                _request(app=SimpleNamespace(state=SimpleNamespace())),
                providers_router.ActiveModelDistributionRequest(
                    target_tenant_ids=["tenant-a"],
                    overwrite=True,
                ),
                manager=source_manager,
            ),
        )

    assert exc_info.value.status_code == 503
    assert (
        exc_info.value.detail
        == "Async task database connection is not available"
    )


def test_active_model_distribution_marks_failed_when_mark_running_fails() -> (
    None
):
    """后台任务启动阶段异常不应泄漏到事件循环外，并应尽力落失败状态。"""

    class FailingStore:
        def __init__(self) -> None:
            self.item_results: list[dict] = []
            self.finished: dict | None = None

        async def mark_running(self, task_id: str) -> None:
            raise RuntimeError("db down")

        async def record_item_result(self, **kwargs) -> None:  # noqa: ANN003
            self.item_results.append(kwargs)

        async def finish_task(self, **kwargs) -> None:  # noqa: ANN003
            self.finished = kwargs

    store = FailingStore()

    asyncio.run(
        providers_router._run_active_model_distribution_task(  # noqa: SLF001
            task_id="task-1",
            store=store,
            source_working_dir=Path("/unused"),
            target_tenant_ids=["tenant-a", "tenant-b"],
            provider_payload={},
            source_active_model=ModelSlotConfig(
                provider_id="openai",
                model="gpt-5.4",
            ),
            source_id="src1",
            tenant_workspace_pool=FakeTenantWorkspacePool(),
        ),
    )

    assert len(store.item_results) == 2
    assert store.finished is not None
    assert store.finished["status"] == "failed"
    assert store.finished["failed_count"] == 2
