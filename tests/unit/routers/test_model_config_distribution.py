# -*- coding: utf-8 -*-
"""Regression tests for model configuration distribution payloads."""

from swe.app.routers.providers import _resolve_distribution_source
from swe.providers.models import ModelSlotConfig
from swe.providers.openai_provider import OpenAIProvider


class _Manager:
    def __init__(self, provider: OpenAIProvider) -> None:
        self.provider = provider

    def get_active_model(self) -> ModelSlotConfig:
        return ModelSlotConfig(provider_id="openai", model="gpt-5")

    def get_provider(self, provider_id: str):
        return self.provider if provider_id == "openai" else None


def test_active_model_distribution_copies_only_active_model_config() -> None:
    provider = OpenAIProvider(
        id="openai",
        name="OpenAI",
        models=[
            {"id": "gpt-5", "name": "GPT-5"},
            {"id": "gpt-5-mini", "name": "GPT-5 mini"},
        ],
        model_configs={
            "gpt-5": {"temperature": 0.2},
            "gpt-5-mini": {"temperature": 0.8},
        },
    )

    _, payload = _resolve_distribution_source(_Manager(provider))

    assert payload["model_configs"] == {
        "gpt-5": {
            "temperature": 0.2,
            "top_p": None,
            "top_k": None,
            "max_input_length": None,
            "max_output_length": None,
            "supports_enable_thinking": False,
            "supported_reasoning_efforts": [],
            "enable_thinking": False,
            "reasoning_effort": None,
        },
    }
