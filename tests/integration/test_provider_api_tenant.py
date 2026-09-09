# -*- coding: utf-8 -*-
# flake8: noqa: E402
# pylint: disable=wrong-import-position,redefined-outer-name,unused-variable
"""Integration tests for provider API tenant routing."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

import anyio
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI, Request

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from swe.app.routers import providers as providers_module
from swe.app.routers.providers import router as providers_router
from swe.app.routers.providers import tenant_providers_router
from swe.providers.provider import ModelRuntimeConfig, ProviderInfo


@pytest.fixture
def client():
    """Create a test client with providers router."""
    app = FastAPI()

    # Add middleware to set tenant_id in request state
    @app.middleware("http")
    async def add_tenant_id(request: Request, call_next):
        request.state.tenant_id = request.headers.get("X-Tenant-Id", "default")
        response = await call_next(request)
        return response

    # Don't add prefix - the router already has /models prefix
    app.include_router(providers_router)
    # Include deprecated /providers router
    app.include_router(tenant_providers_router)
    return TestClient(app)


class TestProviderAPIGetProviders:
    """Tests for GET /models endpoint."""

    @pytest.mark.asyncio
    async def test_get_provider_manager_cache_hit_avoids_threadpool(
        self,
        monkeypatch,
    ):
        """Cached provider managers return on the async hot path."""
        manager = SimpleNamespace(
            tenant_id="scope-a",
            refresh_if_due=AsyncMock(),
        )
        request = SimpleNamespace(
            url=SimpleNamespace(path="/api/models"),
            state=SimpleNamespace(source_id="source-a", scope_id="scope-a"),
        )

        provider_manager = MagicMock()
        provider_manager._instances = {"scope-a": manager}
        provider_manager._resolve_effective_provider_tenant_id.return_value = (
            "scope-a"
        )
        provider_manager._get_tenant_root_path.return_value = Path(
            "/tmp/scope-a/providers",
        )
        monkeypatch.setattr(
            providers_module,
            "ProviderManager",
            provider_manager,
        )
        monkeypatch.setattr(
            providers_module,
            "_get_effective_tenant_id",
            lambda _request: "scope-a",
        )

        async def fail_run_sync(*args, **kwargs):
            raise AssertionError("cache hit should not enter threadpool")

        monkeypatch.setattr(anyio.to_thread, "run_sync", fail_run_sync)

        result = await providers_module.get_provider_manager(request)

        assert result is manager
        provider_manager.ensure_tenant_provider_storage.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_provider_manager_cache_miss_records_threadpool_wait(
        self,
        monkeypatch,
    ):
        """Cold provider managers run in threadpool and expose wait timing."""
        logged_messages = []
        manager = SimpleNamespace(
            tenant_id="scope-cold",
            refresh_if_due=AsyncMock(),
        )
        request = SimpleNamespace(
            url=SimpleNamespace(path="/api/models"),
            state=SimpleNamespace(
                source_id="source-cold",
                scope_id="scope-cold",
            ),
        )

        provider_manager = MagicMock()
        provider_manager._instances = {}
        provider_manager._resolve_effective_provider_tenant_id.return_value = (
            "scope-cold"
        )
        provider_manager._get_tenant_root_path.return_value = Path(
            "/tmp/scope-cold/providers",
        )
        provider_manager.get_or_create_instance = AsyncMock(
            return_value=manager,
        )
        monkeypatch.setattr(
            providers_module,
            "ProviderManager",
            provider_manager,
        )
        monkeypatch.setattr(
            providers_module,
            "_get_effective_tenant_id",
            lambda _request: "scope-cold",
        )
        monkeypatch.setattr(
            providers_module.logger,
            "info",
            lambda message, *args, **kwargs: logged_messages.append(message),
        )

        async def immediate_run_sync(func, *args, **kwargs):
            return func(*args)

        monkeypatch.setattr(anyio.to_thread, "run_sync", immediate_run_sync)

        result = await providers_module.get_provider_manager(request)

        assert result is manager
        assert (
            request.state.provider_manager_dependency_threadpool_wait_ms >= 0
        )
        assert not any(
            "provider_manager_dependency_threadpool_enter" in message
            for message in logged_messages
        )

    def test_get_providers_uses_tenant_from_header(self, client):
        """GET /models uses tenant ID from header."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.list_provider_info = AsyncMock(
                return_value=[
                    ProviderInfo(
                        id="openai",
                        name="OpenAI",
                        base_url="https://api.openai.com/v1",
                        is_custom=False,
                    ),
                ],
            )
            mock_manager.get_active_model.return_value = None
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            response = client.get(
                "/models/",
                headers={"X-Tenant-Id": "tenant-a"},
            )

            assert response.status_code == 200
            # Verify get_instance was called with tenant-a
            mock_pm_class.get_or_create_instance.assert_called_with("tenant-a")

    def test_get_providers_uses_default_without_header(self, client):
        """GET /models uses default tenant without header."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.list_provider_info = AsyncMock(return_value=[])
            mock_manager.get_active_model.return_value = None
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            response = client.get("/models/")

            assert response.status_code == 200
            # Verify get_instance was called with default
            mock_pm_class.get_or_create_instance.assert_called_with("default")


class TestProviderAPICreateProvider:
    """Tests for POST /models/custom-providers endpoint."""

    def test_create_provider_uses_tenant_from_header(self, client):
        """POST /models/custom-providers uses tenant ID from header."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.add_custom_provider = AsyncMock(
                return_value=ProviderInfo(
                    id="custom",
                    name="Custom",
                    base_url="https://custom.example/v1",
                    is_custom=True,
                ),
            )
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            client.post(
                "/models/custom-providers",
                headers={"X-Tenant-Id": "tenant-b"},
                json={
                    "id": "custom",
                    "name": "Custom",
                    "base_url": "https://custom.example/v1",
                    "api_key_prefix": "",
                    "chat_model": "OpenAIChatModel",
                    "models": [],
                },
            )

            # Verify get_instance was called with tenant-b
            mock_pm_class.get_or_create_instance.assert_called_with("tenant-b")


class TestProviderAPIUpdateProvider:
    """Tests for PUT /models/{provider_id}/config endpoint."""

    def test_update_provider_uses_tenant_from_header(self, client):
        """PUT /models/{id}/config uses tenant ID from header."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.update_provider.return_value = True
            mock_manager.get_provider_info = AsyncMock(
                return_value=ProviderInfo(
                    id="openai",
                    name="OpenAI",
                    base_url="https://api.openai.com/v1",
                    is_custom=False,
                ),
            )
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            client.put(
                "/models/openai/config",
                headers={"X-Tenant-Id": "tenant-c"},
                json={"api_key": "sk-test"},
            )

            # Verify get_instance was called with tenant-c
            mock_pm_class.get_or_create_instance.assert_called_with("tenant-c")


class TestProviderAPIModelConfig:
    """Tests for the tenant-scoped model runtime config endpoint."""

    def test_update_model_config_uses_tenant_from_header(self, client):
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.update_model_config.return_value = ModelRuntimeConfig(
                temperature=0.2,
            )
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            response = client.put(
                "/models/openai/models/gpt-5/config",
                headers={"X-Tenant-Id": "tenant-c"},
                json={"temperature": 0.2},
            )

            assert response.status_code == 200
            mock_pm_class.get_or_create_instance.assert_called_with("tenant-c")
            mock_manager.update_model_config.assert_called_once_with(
                "openai",
                "gpt-5",
                {"temperature": 0.2},
            )


class TestProviderAPIDeleteProvider:
    """Tests for DELETE /models/custom-providers/{provider_id} endpoint."""

    def test_delete_provider_uses_tenant_from_header(self, client):
        """DELETE /models/custom-providers/{id} uses tenant ID from header."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.remove_custom_provider.return_value = True
            mock_manager.list_provider_info = AsyncMock(return_value=[])
            mock_manager.builtin_providers = {"openai"}
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            client.delete(
                "/models/custom-providers/custom",
                headers={"X-Tenant-Id": "tenant-d"},
            )

            # Verify get_instance was called with tenant-d
            mock_pm_class.get_or_create_instance.assert_called_with("tenant-d")


class TestProviderAPISetActiveModel:
    """Tests for PUT /models/active endpoint."""

    def test_set_active_model_uses_tenant_from_header(self, client):
        """PUT /models/active uses tenant ID from header."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.activate_model = AsyncMock(return_value=None)
            mock_manager.get_active_model.return_value = None
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            client.put(
                "/models/active",
                headers={"X-Tenant-Id": "tenant-e"},
                json={
                    "provider_id": "openai",
                    "model": "gpt-4",
                    "scope": "global",
                },
            )

            # Verify get_instance was called with tenant-e
            mock_pm_class.get_or_create_instance.assert_called_with("tenant-e")

    def test_set_active_model_scope_agent_is_compatible(self, client):
        """PUT /models/active with scope=agent is treated as global (backward compat)."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.activate_model = AsyncMock(return_value=None)
            mock_manager.get_active_model.return_value = None
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            # Use deprecated scope=agent
            response = client.put(
                "/models/active",
                headers={"X-Tenant-Id": "tenant-compat"},
                json={
                    "provider_id": "openai",
                    "model": "gpt-4",
                    "scope": "agent",
                },
            )

            # Should succeed (not return 400 error)
            assert response.status_code == 200
            # Should call activate_model (normalized to global scope)
            mock_manager.activate_model.assert_called_once_with(
                "openai",
                "gpt-4",
            )

    def test_set_active_model_invalid_scope_rejected(self, client):
        """PUT /models/active with invalid scope fails validation."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            response = client.put(
                "/models/active",
                headers={"X-Tenant-Id": "tenant-invalid"},
                json={
                    "provider_id": "openai",
                    "model": "gpt-4",
                    "scope": "invalid_scope",
                },
            )

            # Pydantic validation rejects invalid enum values (422)
            assert response.status_code == 422


class TestProviderAPITenantIsolation:
    """Tests for tenant isolation in provider API."""

    def test_different_tenants_see_different_providers(self, client):
        """Different tenants see different provider configurations."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            # Create separate managers for each tenant
            manager_a = MagicMock()
            manager_a.list_provider_info = AsyncMock(
                return_value=[
                    ProviderInfo(
                        id="openai",
                        name="OpenAI",
                        base_url="https://api.openai.com/v1",
                        is_custom=False,
                    ),
                ],
            )
            manager_a.get_active_model.return_value = None

            manager_b = MagicMock()
            manager_b.list_provider_info = AsyncMock(
                return_value=[
                    ProviderInfo(
                        id="anthropic",
                        name="Anthropic",
                        base_url="https://api.anthropic.com/v1",
                        is_custom=False,
                    ),
                ],
            )
            manager_b.get_active_model.return_value = None

            def get_instance(tenant_id):
                return manager_a if tenant_id == "tenant-a" else manager_b

            mock_pm_class.get_or_create_instance = AsyncMock(
                side_effect=get_instance,
            )

            # Get providers for tenant-a
            response_a = client.get(
                "/models/",
                headers={"X-Tenant-Id": "tenant-a"},
            )
            data_a = response_a.json()

            # Get providers for tenant-b
            response_b = client.get(
                "/models/",
                headers={"X-Tenant-Id": "tenant-b"},
            )
            data_b = response_b.json()

            # Verify different providers
            assert data_a[0]["id"] == "openai"
            assert data_b[0]["id"] == "anthropic"

    def test_provider_config_isolated_by_tenant(self, client):
        """Provider configurations are isolated by tenant."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.update_provider.return_value = True
            mock_manager.get_provider_info = AsyncMock(
                return_value=ProviderInfo(
                    id="openai",
                    name="OpenAI",
                    base_url="https://api.openai.com/v1",
                    is_custom=False,
                ),
            )
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            # Update for tenant-x
            client.put(
                "/models/openai/config",
                headers={"X-Tenant-Id": "tenant-x"},
                json={"api_key": "sk-tenant-x"},
            )

            # Update for tenant-y
            client.put(
                "/models/openai/config",
                headers={"X-Tenant-Id": "tenant-y"},
                json={"api_key": "sk-tenant-y"},
            )

            # Verify different tenants were used
            assert mock_pm_class.get_or_create_instance.call_count == 2
            calls = [
                call.args[0]
                for call in mock_pm_class.get_or_create_instance.call_args_list
            ]
            assert "tenant-x" in calls
            assert "tenant-y" in calls


class TestDeprecatedProvidersEndpoint:
    """Tests for deprecated /providers endpoint (provider-backed view)."""

    def test_deprecated_providers_endpoint_returns_provider_data(self, client):
        """GET /providers returns provider-backed data with deprecation flag."""
        with patch(
            "swe.app.routers.providers.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.list_provider_info = AsyncMock(
                return_value=[
                    ProviderInfo(
                        id="openai",
                        name="OpenAI",
                        base_url="https://api.openai.com/v1",
                        is_custom=False,
                    ),
                ],
            )
            from swe.providers.models import ModelSlotConfig

            mock_manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-4",
            )
            mock_pm_class.get_or_create_instance = AsyncMock(
                return_value=mock_manager,
            )

            # endpoint 现在读取 effective tenant，兼容 source-scoped 存储。
            with patch(
                "swe.app.routers.providers.get_current_effective_tenant_id",
                return_value="tenant-deprecated",
            ):
                response = client.get("/providers")

            assert response.status_code == 200
            data = response.json()

            # Verify provider-backed structure
            assert data["tenant_id"] == "tenant-deprecated"
            assert "providers" in data
            assert "active_model" in data
            assert data["active_model"]["provider_id"] == "openai"
            assert data["active_model"]["model"] == "gpt-4"

            # Verify deprecation flag
            assert data["deprecated"] is True
            assert "migration_note" in data
            assert "/models" in data["migration_note"]

    def test_deprecated_providers_endpoint_requires_tenant(self, client):
        """GET /providers requires tenant ID."""
        with patch(
            "swe.app.routers.providers.get_current_effective_tenant_id",
            return_value=None,
        ):
            response = client.get("/providers")

            assert response.status_code == 400
            assert "Tenant ID not set" in response.json()["detail"]
