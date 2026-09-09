# -*- coding: utf-8 -*-
"""Tests for applying model input limits to an Agent runtime copy."""

from types import SimpleNamespace

from swe.agents.react_agent import SWEAgent
from swe.providers.provider import ModelRuntimeConfig


def test_model_input_limit_overrides_only_agent_runtime_copy() -> None:
    original = SimpleNamespace(
        running=SimpleNamespace(max_input_length=128_000),
    )
    agent = object.__new__(SWEAgent)
    agent._agent_config = SimpleNamespace(
        running=SimpleNamespace(max_input_length=128_000),
    )

    agent._apply_model_input_budget(
        ModelRuntimeConfig(max_input_length=32_000),
    )

    assert agent._agent_config.running.max_input_length == 32_000
    assert original.running.max_input_length == 128_000


def test_absent_model_input_limit_keeps_agent_runtime_budget() -> None:
    agent = object.__new__(SWEAgent)
    agent._agent_config = SimpleNamespace(
        running=SimpleNamespace(max_input_length=128_000),
    )

    agent._apply_model_input_budget(ModelRuntimeConfig())

    assert agent._agent_config.running.max_input_length == 128_000
