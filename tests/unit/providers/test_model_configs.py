# -*- coding: utf-8 -*-
"""Tests for per-model Provider runtime configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swe.providers.anthropic_provider import AnthropicProvider
from swe.providers.gemini_provider import GeminiProvider
from swe.providers.ollama_provider import OllamaProvider
from swe.providers.openai_provider import OpenAIProvider
from swe.providers.provider import ModelRuntimeConfig


def _provider() -> OpenAIProvider:
    return OpenAIProvider(
        id="openai",
        name="OpenAI",
        base_url="https://api.example.test/v1",
        api_key="sk-test",
        models=[],
    )


def test_model_runtime_config_validates_and_deduplicates_efforts() -> None:
    config = ModelRuntimeConfig(
        temperature=0.7,
        top_p=0.9,
        top_k=20,
        max_input_length=128_000,
        max_output_length=8_192,
        supports_enable_thinking=True,
        supported_reasoning_efforts=["high", "low", "high"],
        enable_thinking=True,
        reasoning_effort="high",
    )

    assert config.supported_reasoning_efforts == ["high", "low"]
    assert config.reasoning_effort == "high"


@pytest.mark.parametrize(
    "payload",
    [
        {"temperature": -0.1},
        {"top_p": 1.1},
        {"top_k": -1},
        {"max_input_length": 0},
        {"max_output_length": 0},
        {"supported_reasoning_efforts": ["medium"]},
        {
            "supported_reasoning_efforts": ["low"],
            "reasoning_effort": "high",
        },
    ],
)
def test_model_runtime_config_rejects_invalid_values(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ModelRuntimeConfig(**payload)


def test_provider_updates_one_model_config_without_legacy_generate_kwargs() -> (
    None
):
    provider = _provider()

    updated = provider.update_model_config(
        "gpt-5",
        {"temperature": 0.2, "max_output_length": 4_096},
    )

    assert updated.temperature == 0.2
    assert updated.max_output_length == 4_096
    assert provider.model_configs["gpt-5"] == updated
    assert "generate_kwargs" not in provider.model_dump()


def test_provider_deleting_model_config_removes_only_target() -> None:
    provider = _provider()
    provider.update_model_config("gpt-5", {"temperature": 0.2})
    provider.update_model_config("gpt-5-mini", {"temperature": 0.7})

    provider.delete_model_config("gpt-5")

    assert "gpt-5" not in provider.model_configs
    assert provider.model_configs["gpt-5-mini"].temperature == 0.7


def test_chat_model_instance_uses_only_explicit_generation_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    created: list[dict] = []

    class FakeChatModel:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

    monkeypatch.setattr(
        "swe.providers.openai_chat_model_compat.OpenAIChatModelCompat",
        FakeChatModel,
    )

    provider.get_chat_model_instance(
        "gpt-5",
        generation_kwargs={"temperature": 0.2, "max_tokens": 4_096},
    )

    assert created[0]["generate_kwargs"] == {
        "temperature": 0.2,
        "max_tokens": 4_096,
    }


@pytest.mark.parametrize(
    ("provider_type", "model_target", "output_length_key"),
    [
        (
            OpenAIProvider,
            "swe.providers.openai_chat_model_compat.OpenAIChatModelCompat",
            "max_tokens",
        ),
        (
            AnthropicProvider,
            "agentscope.model.AnthropicChatModel",
            "max_tokens",
        ),
        (
            GeminiProvider,
            "agentscope.model.GeminiChatModel",
            "max_output_tokens",
        ),
        (
            OllamaProvider,
            "swe.providers.openai_chat_model_compat.OpenAIChatModelCompat",
            "max_tokens",
        ),
    ],
)
def test_provider_adapters_forward_resolved_model_configuration(
    monkeypatch: pytest.MonkeyPatch,
    provider_type: type,
    model_target: str,
    output_length_key: str,
) -> None:
    created: list[dict] = []

    class FakeChatModel:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

    monkeypatch.setattr(model_target, FakeChatModel)
    provider = provider_type(
        id="provider",
        name="Provider",
        base_url="https://api.example.test/v1",
        api_key="test-key",
    )
    config = ModelRuntimeConfig(
        temperature=0.2,
        top_p=0.8,
        top_k=20,
        max_output_length=4_096,
        supports_enable_thinking=True,
        supported_reasoning_efforts=["high"],
        enable_thinking=True,
        reasoning_effort="high",
    )

    provider.get_chat_model_instance(
        "configured-model",
        generation_kwargs=provider.build_generation_kwargs(config),
    )

    assert created[0]["generate_kwargs"] == {
        "temperature": 0.2,
        "top_p": 0.8,
        "top_k": 20,
        output_length_key: 4_096,
        "enable_thinking": True,
        "reasoning_effort": "high",
    }


def test_provider_maps_model_config_to_request_arguments() -> None:
    provider = _provider()
    config = ModelRuntimeConfig(
        temperature=0.2,
        top_p=0.8,
        top_k=20,
        max_input_length=128_000,
        max_output_length=4_096,
        supports_enable_thinking=True,
        supported_reasoning_efforts=["high"],
        enable_thinking=True,
        reasoning_effort="high",
    )

    assert provider.build_generation_kwargs(config) == {
        "temperature": 0.2,
        "top_p": 0.8,
        "top_k": 20,
        "max_tokens": 4_096,
        "enable_thinking": True,
        "reasoning_effort": "high",
    }


def test_reasoning_effort_is_independent_of_thinking_switch_capability() -> (
    None
):
    provider = _provider()
    config = ModelRuntimeConfig(
        supported_reasoning_efforts=["low"],
        reasoning_effort="low",
    )

    assert (
        provider.build_generation_kwargs(config)["reasoning_effort"] == "low"
    )
