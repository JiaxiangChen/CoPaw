# -*- coding: utf-8 -*-
# flake8: noqa: E402
# pylint: disable=wrong-import-position
"""Tests for model_factory tenant integration."""

import sys
import logging
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from swe.agents.model_factory import (
    _get_formatter_for_chat_model,
    _create_file_block_support_formatter,
)
from swe.agents.hook_runtime.messages import (
    build_hook_additional_context_msg,
)
from swe.agents.react_agent import _build_accepted_plan_tool_exchange


class TestFormatterMapping:
    """Tests for chat model to formatter mapping."""

    def test_openai_model_returns_openai_formatter(self):
        """OpenAIChatModel returns OpenAIChatFormatter."""
        from agentscope.formatter import OpenAIChatFormatter
        from agentscope.model import OpenAIChatModel

        formatter_class = _get_formatter_for_chat_model(OpenAIChatModel)
        assert formatter_class == OpenAIChatFormatter

    def test_unknown_model_defaults_to_openai_formatter(self):
        """Unknown model class defaults to OpenAIChatFormatter."""
        from agentscope.formatter import OpenAIChatFormatter
        from agentscope.model import OpenAIChatModel

        class UnknownModel(OpenAIChatModel):
            pass

        formatter_class = _get_formatter_for_chat_model(UnknownModel)
        assert formatter_class == OpenAIChatFormatter


class TestFileBlockSupportFormatter:
    """Tests for file block support formatter wrapper."""

    def test_formatter_creation(self):
        """File block support formatter can be created."""
        from agentscope.formatter import OpenAIChatFormatter

        formatter_class = _create_file_block_support_formatter(
            OpenAIChatFormatter,
        )
        assert formatter_class is not None
        assert "FileBlockSupport" in formatter_class.__name__

    @pytest.mark.asyncio
    async def test_openai_formatter_demotes_later_generic_system_role(self):
        """OpenAI 兼容后端应降级普通中段 system 消息。"""
        from agentscope.formatter import OpenAIChatFormatter
        from agentscope.message import Msg

        formatter_class = _create_file_block_support_formatter(
            OpenAIChatFormatter,
        )
        formatter = formatter_class()

        messages = await formatter._format(
            [
                Msg(name="system", role="system", content="base prompt"),
                Msg(name="user", role="user", content="hello"),
                Msg(
                    name="system",
                    role="system",
                    content="intermediate system context",
                ),
                Msg(name="user", role="user", content="next turn"),
            ],
        )

        assert [message["role"] for message in messages] == [
            "system",
            "user",
            "user",
            "user",
        ]
        assert messages[2]["content"][0]["text"] == (
            "intermediate system context"
        )

    @pytest.mark.asyncio
    async def test_anthropic_formatter_demotes_later_generic_system_role(
        self,
    ):
        """Anthropic 后端应降级普通中段 system 消息。"""
        from agentscope.formatter import AnthropicChatFormatter
        from agentscope.message import Msg

        formatter_class = _create_file_block_support_formatter(
            AnthropicChatFormatter,
        )
        formatter = formatter_class()

        messages = await formatter._format(
            [
                Msg(name="system", role="system", content="base prompt"),
                Msg(name="user", role="user", content="hello"),
                Msg(
                    name="system",
                    role="system",
                    content="intermediate system context",
                ),
                Msg(name="user", role="user", content="next turn"),
            ],
        )

        assert [message["role"] for message in messages] == [
            "system",
            "user",
            "user",
            "user",
        ]
        assert messages[2]["content"][0]["text"] == (
            "intermediate system context"
        )

    @pytest.mark.asyncio
    async def test_openai_formatter_preserves_later_hook_system_role(self):
        """OpenAI 兼容后端应保留持久化 hook system 消息。"""
        from agentscope.formatter import OpenAIChatFormatter
        from agentscope.message import Msg

        formatter_class = _create_file_block_support_formatter(
            OpenAIChatFormatter,
        )
        formatter = formatter_class()

        messages = await formatter._format(
            [
                Msg(name="system", role="system", content="base prompt"),
                Msg(name="user", role="user", content="hello"),
                build_hook_additional_context_msg(
                    "[Hook additional context]\nremember",
                ),
                Msg(name="user", role="user", content="next turn"),
            ],
        )

        assert [message["role"] for message in messages] == [
            "system",
            "user",
            "system",
            "user",
        ]
        assert messages[2]["content"][0]["text"] == (
            "[Hook additional context]\nremember"
        )

    @pytest.mark.asyncio
    async def test_anthropic_formatter_merges_later_hook_system_role(
        self,
    ):
        """Anthropic 后端应把持久化 hook system 合并到首条 system。"""
        from agentscope.formatter import AnthropicChatFormatter
        from agentscope.message import Msg

        formatter_class = _create_file_block_support_formatter(
            AnthropicChatFormatter,
        )
        formatter = formatter_class()

        messages = await formatter._format(
            [
                Msg(name="system", role="system", content="base prompt"),
                Msg(name="user", role="user", content="hello"),
                build_hook_additional_context_msg(
                    "[Hook additional context]\nremember",
                ),
                Msg(name="user", role="user", content="next turn"),
            ],
        )

        assert [message["role"] for message in messages] == [
            "system",
            "user",
            "user",
        ]
        assert messages[0]["content"][-1]["text"] == (
            "[Hook additional context]\nremember"
        )
        assert all(message["role"] != "system" for message in messages[1:])

    @pytest.mark.asyncio
    async def test_openai_formatter_preserves_internal_accepted_plan_exchange(
        self,
    ):
        """accepted plan 内部 tool exchange 应保持 OpenAI 协议配对。"""
        from agentscope.formatter import OpenAIChatFormatter
        from agentscope.message import Msg

        formatter_class = _create_file_block_support_formatter(
            OpenAIChatFormatter,
        )
        formatter = formatter_class()
        accepted_plan_msgs = _build_accepted_plan_tool_exchange(
            {
                "turn_id": "turn-1",
                "plan_mode_enabled": False,
                "accepted_plan_source": "server_plan_store",
                "accepted_plan": {"plan_id": "plan-123"},
            },
        )

        messages = await formatter._format(
            [
                Msg(name="system", role="system", content="base prompt"),
                *accepted_plan_msgs,
                Msg(name="user", role="user", content="next turn"),
            ],
        )

        assert [message["role"] for message in messages] == [
            "system",
            "assistant",
            "tool",
            "user",
        ]
        tool_call = messages[1]["tool_calls"][0]
        assert tool_call["function"]["name"] == "accepted_plan_context"
        assert messages[2]["tool_call_id"] == tool_call["id"]
        assert "Accepted Plan Execution Context" in messages[2]["content"]
        assert "developer" not in {message["role"] for message in messages}

    @pytest.mark.asyncio
    async def test_anthropic_formatter_preserves_internal_accepted_plan_exchange(
        self,
    ):
        """accepted plan 内部 tool exchange 应保持 Anthropic 协议配对。"""
        from agentscope.formatter import AnthropicChatFormatter
        from agentscope.message import Msg

        formatter_class = _create_file_block_support_formatter(
            AnthropicChatFormatter,
        )
        formatter = formatter_class()
        accepted_plan_msgs = _build_accepted_plan_tool_exchange(
            {
                "turn_id": "turn-1",
                "plan_mode_enabled": False,
                "accepted_plan_source": "server_plan_store",
                "accepted_plan": {"plan_id": "plan-123"},
            },
        )

        messages = await formatter._format(
            [
                Msg(name="system", role="system", content="base prompt"),
                *accepted_plan_msgs,
                Msg(name="user", role="user", content="next turn"),
            ],
        )

        assert [message["role"] for message in messages] == [
            "system",
            "assistant",
            "user",
            "user",
        ]
        assert messages[1]["content"][0]["name"] == "accepted_plan_context"
        assert (
            messages[2]["content"][0]["tool_use_id"]
            == messages[1]["content"][0]["id"]
        )
        assert "Accepted Plan Execution Context" in (
            messages[2]["content"][0]["content"][0]["text"]
        )
        assert "developer" not in {message["role"] for message in messages}

    def test_formatter_supports_structured_failed_tool_result(self):
        """Structured failed tool outputs remain readable to the model."""
        from agentscope.formatter import OpenAIChatFormatter

        formatter_class = _create_file_block_support_formatter(
            OpenAIChatFormatter,
        )

        text, multimodal = formatter_class.convert_tool_result_to_string(
            {
                "isError": True,
                "error_type": "permission_denied",
                "content": [
                    {
                        "type": "text",
                        "text": "permission denied",
                    },
                ],
            },
        )

        assert text == "permission denied"
        assert multimodal == []

    def test_formatter_supports_file_blocks(self):
        """File blocks are converted into readable text and metadata."""
        from agentscope.formatter import OpenAIChatFormatter

        formatter_class = _create_file_block_support_formatter(
            OpenAIChatFormatter,
        )

        text, multimodal = formatter_class.convert_tool_result_to_string(
            [
                {
                    "type": "text",
                    "text": "artifact generated",
                },
                {
                    "type": "file",
                    "path": "/tmp/report.txt",
                    "name": "report.txt",
                },
            ],
        )

        assert text == (
            "- artifact generated\n"
            "- The returned file 'report.txt' can be found at: /tmp/report.txt"
        )
        assert multimodal == [
            (
                "/tmp/report.txt",
                {
                    "type": "file",
                    "path": "/tmp/report.txt",
                    "name": "report.txt",
                },
            ),
        ]


class TestCreateModelAndFormatterTenantIntegration:
    """Tests for tenant-aware model creation."""

    def test_raises_when_no_active_model(self):
        """Factory raises when ProviderManager has no active model."""
        from swe.agents.model_factory import create_model_and_formatter

        # Patch ProviderManager to return no active model
        with patch(
            "swe.agents.model_factory.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.get_active_model.return_value = None
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            with pytest.raises(
                ValueError,
                match="No tenant model configuration found",
            ):
                create_model_and_formatter()

    def test_uses_provider_manager_as_primary_source(self):
        """Factory uses ProviderManager.get_active_model() as primary source."""
        from swe.agents.model_factory import create_model_and_formatter

        # Patch ProviderManager with active model
        with patch(
            "swe.agents.model_factory.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            from swe.providers.models import ModelSlotConfig

            mock_manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-4",
            )
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            # Mock provider to return a model
            mock_provider = MagicMock()
            mock_model = MagicMock()
            mock_model.__class__.__name__ = "OpenAIChatModel"
            mock_provider.get_chat_model_instance.return_value = mock_model
            mock_manager.get_provider.return_value = mock_provider

            # Patch formatter creation and wrappers
            with patch(
                "swe.agents.model_factory._create_formatter_instance",
            ):
                with patch(
                    "swe.agents.model_factory.TokenRecordingModelWrapper",
                ):
                    with patch("swe.agents.model_factory.RetryChatModel"):
                        model, _ = create_model_and_formatter()

            # Verify ProviderManager.get_active_model was called (not TenantModelContext)
            mock_manager.get_active_model.assert_called_once()
            mock_manager.get_provider.assert_called_once_with("openai")

    def test_tenant_provider_manager_isolation(self):
        """Different tenants get different ProviderManager instances."""
        from swe.agents.model_factory import create_model_and_formatter

        # Patch ProviderManager to track tenant IDs
        with patch(
            "swe.agents.model_factory.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            from swe.providers.models import ModelSlotConfig

            mock_manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-4",
            )
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            mock_provider = MagicMock()
            mock_model = MagicMock()
            mock_model.__class__.__name__ = "OpenAIChatModel"
            mock_provider.get_chat_model_instance.return_value = mock_model
            mock_manager.get_provider.return_value = mock_provider

            # Patch formatter creation
            with patch(
                "swe.agents.model_factory._create_formatter_instance",
            ):
                with patch(
                    "swe.agents.model_factory.TokenRecordingModelWrapper",
                ):
                    with patch(
                        "swe.agents.model_factory.RetryChatModel",
                    ):
                        # First call with tenant-a
                        with patch(
                            "swe.config.context.get_current_tenant_id",
                            return_value="tenant-a",
                        ):
                            try:
                                create_model_and_formatter()
                            except Exception:
                                pass

            # Verify get_instance was called with tenant-a
            calls = [
                str(call) for call in mock_pm_class.get_instance.call_args_list
            ]
            assert any("tenant-a" in call for call in calls)

    def test_logs_model_factory_duration(self):
        """Factory emits timing diagnostics for model creation."""
        import swe.agents.model_factory as model_factory_module
        from swe.agents.model_factory import create_model_and_formatter

        with patch(
            "swe.agents.model_factory.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            from swe.providers.models import ModelSlotConfig

            mock_manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-4",
            )
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            mock_provider = MagicMock()
            mock_model = MagicMock()
            mock_model.__class__.__name__ = "OpenAIChatModel"
            mock_provider.get_chat_model_instance.return_value = mock_model
            mock_manager.get_provider.return_value = mock_provider

            with patch.object(
                model_factory_module.logger,
                "debug",
            ) as mock_debug:
                with patch(
                    "swe.agents.model_factory._create_formatter_instance",
                ):
                    with patch(
                        "swe.agents.model_factory.TokenRecordingModelWrapper",
                        side_effect=lambda _provider_id, model: model,
                    ):
                        with patch(
                            "swe.agents.model_factory.RetryChatModel",
                            side_effect=lambda model, **_kwargs: model,
                        ):
                            create_model_and_formatter()

        assert any(
            call.args
            and "create_model_and_formatter_duration_ms=" in call.args[0]
            for call in mock_debug.call_args_list
        )

    def test_passes_effective_tenant_and_agent_scope_to_retry_model(self):
        """Factory propagates limiter scope and config to RetryChatModel."""
        from swe.agents.model_factory import create_model_and_formatter
        from swe.providers.models import ModelSlotConfig

        with (
            patch(
                "swe.config.context.get_current_effective_tenant_id",
                return_value="tenant-a",
            ),
            patch(
                "swe.app.agent_context.get_current_agent_id",
                return_value="agent-x",
            ),
            patch(
                "swe.config.config.load_agent_config",
            ) as mock_load_agent_config,
            patch(
                "swe.agents.model_factory.ProviderManager",
            ) as mock_pm_class,
            patch(
                "swe.agents.model_factory._create_formatter_instance",
            ),
            patch(
                "swe.agents.model_factory.TokenRecordingModelWrapper",
                side_effect=lambda _provider_id, model: model,
            ),
            patch(
                "swe.agents.model_factory.RetryChatModel",
                side_effect=lambda model, **_kwargs: model,
            ) as mock_retry_model,
        ):
            mock_agent_config = MagicMock()
            mock_agent_config.running.llm_retry_enabled = True
            mock_agent_config.running.llm_max_retries = 3
            mock_agent_config.running.llm_backoff_base = 1.0
            mock_agent_config.running.llm_backoff_cap = 10.0
            mock_agent_config.running.llm_max_concurrent = 7
            mock_agent_config.running.llm_max_qpm = 70
            mock_agent_config.running.llm_rate_limit_pause = 4.0
            mock_agent_config.running.llm_rate_limit_jitter = 0.5
            mock_agent_config.running.llm_acquire_timeout = 30.0
            mock_agent_config.running.llm_chat_max_concurrent = None
            mock_agent_config.running.llm_cron_max_concurrent = None
            mock_agent_config.running.llm_chat_acquire_timeout = None
            mock_agent_config.running.llm_cron_acquire_timeout = None
            mock_load_agent_config.return_value = mock_agent_config

            mock_manager = MagicMock()
            mock_manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-4",
            )
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            mock_provider = MagicMock()
            mock_model = MagicMock()
            mock_model.model_name = "gpt-4"
            mock_model.stream = False
            mock_provider.get_chat_model_instance.return_value = mock_model
            mock_manager.get_provider.return_value = mock_provider

            create_model_and_formatter()

        mock_pm_class.get_instance.assert_called_once_with("tenant-a")
        assert mock_retry_model.call_args.kwargs["tenant_id"] == "tenant-a"
        assert mock_retry_model.call_args.kwargs["agent_id"] == "agent-x"
        rate_limit_config = mock_retry_model.call_args.kwargs[
            "rate_limit_config"
        ]
        assert rate_limit_config.max_concurrent == 7
        assert rate_limit_config.max_qpm == 70
        assert rate_limit_config.max_concurrent_for("chat") == 2
        assert rate_limit_config.max_concurrent_for("cron") == 3
        assert rate_limit_config.acquire_timeout_for("chat") == 30.0
        assert rate_limit_config.acquire_timeout_for("cron") == 30.0
        mock_load_agent_config.assert_any_call(
            "agent-x",
            tenant_id="tenant-a",
        )

    def test_workload_specific_rate_limit_config_overrides_fallbacks(self):
        """Factory keeps default fallback while applying workload overrides."""
        from swe.agents.model_factory import create_model_and_formatter
        from swe.providers.models import ModelSlotConfig

        with (
            patch(
                "swe.config.context.get_current_effective_tenant_id",
                return_value="tenant-a",
            ),
            patch(
                "swe.app.agent_context.get_current_agent_id",
                return_value="agent-x",
            ),
            patch(
                "swe.config.config.load_agent_config",
            ) as mock_load_agent_config,
            patch(
                "swe.agents.model_factory.ProviderManager",
            ) as mock_pm_class,
            patch(
                "swe.agents.model_factory._create_formatter_instance",
            ),
            patch(
                "swe.agents.model_factory.TokenRecordingModelWrapper",
                side_effect=lambda _provider_id, model: model,
            ),
            patch(
                "swe.agents.model_factory.RetryChatModel",
                side_effect=lambda model, **_kwargs: model,
            ) as mock_retry_model,
        ):
            mock_agent_config = MagicMock()
            mock_agent_config.running.llm_retry_enabled = True
            mock_agent_config.running.llm_max_retries = 3
            mock_agent_config.running.llm_backoff_base = 1.0
            mock_agent_config.running.llm_backoff_cap = 10.0
            mock_agent_config.running.llm_max_concurrent = 5
            mock_agent_config.running.llm_chat_max_concurrent = None
            mock_agent_config.running.llm_cron_max_concurrent = 2
            mock_agent_config.running.llm_max_qpm = 70
            mock_agent_config.running.llm_rate_limit_pause = 4.0
            mock_agent_config.running.llm_rate_limit_jitter = 0.5
            mock_agent_config.running.llm_acquire_timeout = 30.0
            mock_agent_config.running.llm_chat_acquire_timeout = 15.0
            mock_agent_config.running.llm_cron_acquire_timeout = None
            mock_load_agent_config.return_value = mock_agent_config

            mock_manager = MagicMock()
            mock_manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-4",
            )
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            mock_provider = MagicMock()
            mock_model = MagicMock()
            mock_model.model_name = "gpt-4"
            mock_model.stream = False
            mock_provider.get_chat_model_instance.return_value = mock_model
            mock_manager.get_provider.return_value = mock_provider

            create_model_and_formatter()

        rate_limit_config = mock_retry_model.call_args.kwargs[
            "rate_limit_config"
        ]
        assert rate_limit_config.max_concurrent_for("chat") == 2
        assert rate_limit_config.max_concurrent_for("cron") == 2
        assert rate_limit_config.acquire_timeout_for("chat") == 15.0
        assert rate_limit_config.acquire_timeout_for("cron") == 30.0

    def test_source_rate_limit_override_applies_to_retry_model(self):
        """Factory applies current source LLM limiter overrides."""
        from swe.agents.model_factory import create_model_and_formatter
        from swe.app.source_system_config.models import (
            EffectiveSourceSystemConfig,
            SourceSystemConfig,
        )
        from swe.app.source_system_config.runtime import (
            bind_source_system_config,
        )
        from swe.providers.models import ModelSlotConfig

        with (
            patch(
                "swe.config.context.get_current_effective_tenant_id",
                return_value="tenant-a",
            ),
            patch(
                "swe.app.agent_context.get_current_agent_id",
                return_value="agent-x",
            ),
            patch(
                "swe.config.config.load_agent_config",
            ) as mock_load_agent_config,
            patch(
                "swe.agents.model_factory.ProviderManager",
            ) as mock_pm_class,
            patch(
                "swe.agents.model_factory._create_formatter_instance",
            ),
            patch(
                "swe.agents.model_factory.TokenRecordingModelWrapper",
                side_effect=lambda _provider_id, model: model,
            ),
            patch(
                "swe.agents.model_factory.RetryChatModel",
                side_effect=lambda model, **_kwargs: model,
            ) as mock_retry_model,
        ):
            mock_agent_config = MagicMock()
            mock_agent_config.running.llm_retry_enabled = True
            mock_agent_config.running.llm_max_retries = 3
            mock_agent_config.running.llm_backoff_base = 1.0
            mock_agent_config.running.llm_backoff_cap = 10.0
            mock_agent_config.running.llm_max_concurrent = 7
            mock_agent_config.running.llm_chat_max_concurrent = 4
            mock_agent_config.running.llm_cron_max_concurrent = 6
            mock_agent_config.running.llm_max_qpm = 70
            mock_agent_config.running.llm_rate_limit_pause = 4.0
            mock_agent_config.running.llm_rate_limit_jitter = 0.5
            mock_agent_config.running.llm_acquire_timeout = 30.0
            mock_agent_config.running.llm_chat_acquire_timeout = None
            mock_agent_config.running.llm_cron_acquire_timeout = 45.0
            mock_load_agent_config.return_value = mock_agent_config

            mock_manager = MagicMock()
            mock_manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-4",
            )
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            mock_provider = MagicMock()
            mock_model = MagicMock()
            mock_model.model_name = "gpt-4"
            mock_model.stream = False
            mock_provider.get_chat_model_instance.return_value = mock_model
            mock_manager.get_provider.return_value = mock_provider

            effective = EffectiveSourceSystemConfig(
                source_id="portal",
                config=SourceSystemConfig.model_validate({}),
                raw_config=SourceSystemConfig.model_validate(
                    {
                        "llm_rate_limiter": {
                            "llm_chat_max_concurrent": 1,
                            "llm_max_qpm": 12,
                        },
                    },
                ),
                version=3,
            )
            with bind_source_system_config(effective):
                create_model_and_formatter()

        rate_limit_config = mock_retry_model.call_args.kwargs[
            "rate_limit_config"
        ]
        assert rate_limit_config.max_concurrent == 7
        assert rate_limit_config.max_concurrent_for("chat") == 1
        assert rate_limit_config.max_concurrent_for("cron") == 6
        assert rate_limit_config.max_qpm == 12
        assert rate_limit_config.acquire_timeout_for("cron") == 45.0


class TestBackwardCompatibility:
    """Tests for backward compatibility with non-tenant mode."""

    def test_raises_when_provider_manager_has_no_active_model(self):
        """Factory raises when ProviderManager has no active model."""
        from swe.agents.model_factory import create_model_and_formatter

        # Patch ProviderManager to return no active model
        with patch(
            "swe.agents.model_factory.ProviderManager",
        ) as mock_pm_class:
            mock_manager = MagicMock()
            mock_manager.get_active_model.return_value = None
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            with pytest.raises(
                ValueError,
                match="No tenant model configuration",
            ):
                create_model_and_formatter()

    def test_agent_id_parameter_uses_retry_config(self):
        """agent_id loads retry config from agent config."""
        from swe.agents.model_factory import create_model_and_formatter

        with patch(
            "swe.app.agent_context.get_current_agent_id",
        ) as mock_get_agent:
            mock_get_agent.return_value = "context-agent"

            with patch("swe.config.config.load_agent_config") as mock_load:
                mock_config = MagicMock()
                mock_config.running.llm_retry_enabled = True
                mock_config.running.llm_max_retries = 3
                mock_config.running.llm_backoff_base = 1.0
                mock_config.running.llm_backoff_cap = 60.0
                mock_config.running.llm_max_concurrent = 10
                mock_config.running.llm_max_qpm = 100
                mock_config.running.llm_rate_limit_pause = 1.0
                mock_config.running.llm_rate_limit_jitter = 0.1
                mock_config.running.llm_acquire_timeout = 30.0
                mock_config.running.llm_chat_max_concurrent = None
                mock_config.running.llm_cron_max_concurrent = None
                mock_config.running.llm_chat_acquire_timeout = None
                mock_config.running.llm_cron_acquire_timeout = None
                mock_load.return_value = mock_config

                # Also need to mock ProviderManager since it's the primary source
                with patch(
                    "swe.agents.model_factory.ProviderManager",
                ) as mock_pm_class:
                    mock_manager = MagicMock()
                    mock_manager.get_active_model.return_value = None
                    mock_pm_class.get_instance.return_value = mock_manager
                    mock_pm_class.ensure_tenant_provider_storage = MagicMock()

                    with pytest.raises(
                        ValueError,
                        match="No tenant model configuration",
                    ):
                        create_model_and_formatter(agent_id="param-agent")

                    # load_agent_config should be called with param-agent
                    mock_load.assert_called_once_with("param-agent")


class TestRetryConfigPropagation:
    """Tests for retry configuration propagation."""

    def test_retry_config_from_agent_config(self):
        """Retry configuration is extracted from agent config."""
        from swe.providers.retry_chat_model import RetryConfig

        # Create a RetryConfig to verify structure
        retry_config = RetryConfig(
            enabled=True,
            max_retries=5,
            backoff_base=2.0,
            backoff_cap=120.0,
        )

        assert retry_config.enabled is True
        assert retry_config.max_retries == 5
        assert retry_config.backoff_base == 2.0
        assert retry_config.backoff_cap == 120.0


class TestScopedModelSlotOverride:
    def test_model_configuration_is_snapshotted_per_factory_call(self):
        """An in-flight model keeps its arguments after a config save."""
        from swe.agents.model_factory import create_model_and_formatter
        from swe.providers.models import ModelSlotConfig
        from swe.providers.provider import ModelRuntimeConfig

        first_model = MagicMock()
        second_model = MagicMock()
        current_config = ModelRuntimeConfig(temperature=0.2)

        with (
            patch("swe.agents.model_factory.ProviderManager") as manager_cls,
            patch("swe.agents.model_factory._create_formatter_instance"),
            patch(
                "swe.agents.model_factory.TokenRecordingModelWrapper",
                side_effect=lambda _provider_id, model: model,
            ),
            patch(
                "swe.agents.model_factory.RetryChatModel",
                side_effect=lambda model, **_kwargs: model,
            ),
        ):
            manager = MagicMock()
            manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-5",
            )
            manager_cls.get_instance.return_value = manager
            manager_cls.ensure_tenant_provider_storage = MagicMock()
            provider = MagicMock()
            provider.get_model_config.side_effect = (
                lambda _model_id: current_config
            )
            provider.build_generation_kwargs.side_effect = (
                lambda config: config.generation_kwargs("max_tokens")
            )
            provider.get_chat_model_instance.side_effect = [
                first_model,
                second_model,
            ]
            manager.get_provider.return_value = provider

            first, _ = create_model_and_formatter()
            current_config = ModelRuntimeConfig(temperature=0.9)
            second, _ = create_model_and_formatter()

        assert first is first_model
        assert second is second_model
        assert provider.get_chat_model_instance.call_args_list[0].kwargs == {
            "generation_kwargs": {"temperature": 0.2},
        }
        assert provider.get_chat_model_instance.call_args_list[1].kwargs == {
            "generation_kwargs": {"temperature": 0.9},
        }

    def test_scoped_override_takes_priority_over_tenant_default(self):
        from swe.agents.model_factory import create_model_and_formatter
        from swe.app.crons.model_slot_context import (
            bind_model_slot_override,
        )
        from swe.providers.models import ModelSlotConfig

        with (
            patch(
                "swe.agents.model_factory.ProviderManager",
            ) as mock_pm_class,
            patch(
                "swe.agents.model_factory._create_formatter_instance",
            ),
            patch(
                "swe.agents.model_factory.TokenRecordingModelWrapper",
                side_effect=lambda _provider_id, model: model,
            ),
            patch(
                "swe.agents.model_factory.RetryChatModel",
                side_effect=lambda model, **_kwargs: model,
            ),
        ):
            mock_manager = MagicMock()
            mock_manager.get_active_model.return_value = ModelSlotConfig(
                provider_id="openai",
                model="gpt-4o",
            )
            mock_pm_class.get_instance.return_value = mock_manager
            mock_pm_class.ensure_tenant_provider_storage = MagicMock()

            default_provider = MagicMock()
            default_provider.get_chat_model_instance.return_value = MagicMock()
            override_provider = MagicMock()
            override_provider.get_chat_model_instance.return_value = (
                MagicMock()
            )
            mock_manager.get_provider.side_effect = lambda provider_id: {
                "openai": default_provider,
                "anthropic": override_provider,
            }[provider_id]

            with bind_model_slot_override(
                ModelSlotConfig(
                    provider_id="anthropic",
                    model="claude-3-7-sonnet",
                ),
            ):
                create_model_and_formatter()

        mock_manager.get_provider.assert_called_once_with("anthropic")
        override_provider.get_chat_model_instance.assert_called_once_with(
            "claude-3-7-sonnet",
            generation_kwargs=ANY,
        )

    def test_private_provider_override_does_not_read_current_provider(
        self,
    ):
        """A frozen worker provider wins over mutable ProviderManager data."""
        from swe.agents.model_factory import create_model_and_formatter
        from swe.providers.models import ModelSlotConfig

        with (
            patch("swe.agents.model_factory.ProviderManager") as manager_cls,
            patch("swe.agents.model_factory._create_formatter_instance"),
            patch(
                "swe.agents.model_factory.TokenRecordingModelWrapper",
                side_effect=lambda _provider_id, model: model,
            ),
            patch(
                "swe.agents.model_factory.RetryChatModel",
                side_effect=lambda model, **_kwargs: model,
            ),
        ):
            manager = MagicMock()
            manager_cls.get_instance.return_value = manager
            manager_cls.ensure_tenant_provider_storage = MagicMock()
            frozen_provider = MagicMock()
            frozen_provider.get_chat_model_instance.return_value = MagicMock()

            create_model_and_formatter(
                model_slot_override=ModelSlotConfig(
                    provider_id="frozen",
                    model="frozen-model",
                ),
                model_provider_override=frozen_provider,
            )

        manager.get_provider.assert_not_called()
        manager_cls.get_instance.assert_not_called()
        manager_cls.ensure_tenant_provider_storage.assert_not_called()
        frozen_provider.get_chat_model_instance.assert_called_once_with(
            "frozen-model",
            generation_kwargs=ANY,
        )

    def test_private_selected_model_falls_back_to_frozen_parent(self):
        """A selected model failure does not consult changed tenant config."""
        from swe.agents.model_factory import create_model_and_formatter
        from swe.providers.models import ModelSlotConfig

        with (
            patch("swe.agents.model_factory.ProviderManager") as manager_cls,
            patch("swe.agents.model_factory._create_formatter_instance"),
            patch(
                "swe.agents.model_factory.TokenRecordingModelWrapper",
                side_effect=lambda _provider_id, model: model,
            ),
            patch(
                "swe.agents.model_factory.RetryChatModel",
                side_effect=lambda model, **_kwargs: model,
            ),
        ):
            manager = MagicMock()
            manager_cls.get_instance.return_value = manager
            manager_cls.ensure_tenant_provider_storage = MagicMock()
            selected_provider = MagicMock()
            selected_provider.get_chat_model_instance.side_effect = ValueError(
                "selected unavailable",
            )
            parent_provider = MagicMock()
            parent_provider.get_chat_model_instance.return_value = MagicMock()

            create_model_and_formatter(
                model_slot_override=ModelSlotConfig(
                    provider_id="selected",
                    model="selected-model",
                ),
                model_provider_override=selected_provider,
                fallback_model_slot=ModelSlotConfig(
                    provider_id="parent",
                    model="parent-model",
                ),
                fallback_model_provider=parent_provider,
            )

        manager.get_provider.assert_not_called()
        manager_cls.get_instance.assert_not_called()
        manager_cls.ensure_tenant_provider_storage.assert_not_called()
        parent_provider.get_chat_model_instance.assert_called_once_with(
            "parent-model",
            generation_kwargs=ANY,
        )
