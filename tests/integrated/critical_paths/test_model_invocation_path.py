# -*- coding: utf-8 -*-
"""Critical-path checks for provider-to-model invocation."""

from __future__ import annotations

import pytest
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage

from swe.agents import model_factory
from swe.config.llm_workload import LLM_WORKLOAD_CRON, bind_llm_workload
from swe.providers.rate_limiter import reset_rate_limiter
from swe.providers.retry_chat_model import RetryChatModel

from tests.integrated.critical_paths.conftest import (
    DeterministicChatModel,
    FakeProvider,
    FakeProviderManager,
)


@pytest.fixture(autouse=True)
def _reset_rate_limiter_registry():
    reset_rate_limiter()
    yield
    reset_rate_limiter()


@pytest.mark.asyncio
async def test_provider_model_factory_wrappers_and_retry_are_in_invocation_path(
    monkeypatch,
    isolated_agent_config,
) -> None:
    _, write_agent_config = isolated_agent_config
    write_agent_config()
    bottom_model = DeterministicChatModel(
        [
            TimeoutError("first model call timed out"),
            ChatResponse(
                content=[{"type": "text", "text": "retry ok"}],
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
            ),
        ],
    )
    manager = FakeProviderManager(FakeProvider(bottom_model))

    monkeypatch.setattr(
        model_factory.ProviderManager,
        "ensure_tenant_provider_storage",
        lambda _tenant_id: None,
    )
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_instance",
        lambda _tenant_id=None: manager,
    )

    model, formatter = model_factory.create_model_and_formatter(
        agent_id="critical-agent",
    )

    assert isinstance(model, RetryChatModel)
    assert formatter is not None

    with bind_llm_workload(LLM_WORKLOAD_CRON):
        response = await model([])

    assert response.content[0]["text"] == "retry ok"
    assert len(bottom_model.calls) == 2


def test_provider_model_factory_passes_model_generation_config(
    monkeypatch,
) -> None:
    bottom_model = DeterministicChatModel([])
    provider = FakeProvider(
        bottom_model,
        generation_kwargs={"temperature": 0.2, "max_tokens": 4_096},
    )
    manager = FakeProviderManager(provider)

    monkeypatch.setattr(
        model_factory.ProviderManager,
        "ensure_tenant_provider_storage",
        lambda _tenant_id: None,
    )
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_instance",
        lambda _tenant_id=None: manager,
    )

    model_factory.create_model_and_formatter()

    assert provider.request_generation_kwargs == [
        {"temperature": 0.2, "max_tokens": 4_096},
    ]
