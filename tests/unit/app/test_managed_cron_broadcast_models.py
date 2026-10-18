# -*- coding: utf-8 -*-
"""验证分发模型回退只作用于目标环境，不降低普通创建的配置校验。"""

import json
from types import SimpleNamespace

import pytest

from tests.unit.app.test_managed_cron_resources import Chats, spec


def model_manager(*, default_available=True):
    from swe.providers.models import ModelSlotConfig

    provider = SimpleNamespace(
        has_model=lambda model: model == "default-model",
    )
    return SimpleNamespace(
        get_provider=lambda name: (
            provider if name == "receiver-provider" else None
        ),
        get_active_model=lambda: (
            ModelSlotConfig(
                provider_id="receiver-provider",
                model="default-model",
            )
            if default_available
            else None
        ),
    )


@pytest.mark.asyncio
async def test_broadcast_falls_back_to_available_target_default_and_returns_warning(
    monkeypatch,
):
    from swe.app.crons import managed_model_preparation
    from swe.app.crons.managed_resources import ResourcePreparer

    monkeypatch.setattr(
        managed_model_preparation,
        "get_model_manager",
        lambda _: model_manager(),
    )
    value = spec()
    value["model_slot"] = {
        "provider_id": "source-provider",
        "model": "source-model",
    }
    result = await ResourcePreparer(Chats(), agent_id="default").prepare(
        value,
        {"payload_json": json.dumps({"preparation_policy": "broadcast"})},
    )
    assert result["spec"]["model_slot"] is None
    assert result["warning"] == "broadcast_model_fallback"
    assert (
        result["spec"]["meta"]["broadcast_original_model_slot"]
        == value["model_slot"]
    )


@pytest.mark.asyncio
async def test_missing_default_fails_before_chat_creation(monkeypatch):
    from swe.app.crons import managed_model_preparation
    from swe.app.crons.managed_resources import ResourcePreparer

    monkeypatch.setattr(
        managed_model_preparation,
        "get_model_manager",
        lambda _: model_manager(default_available=False),
    )
    chats = Chats()
    with pytest.raises(ValueError, match="default_model"):
        await ResourcePreparer(chats, agent_id="default").prepare(
            spec(),
            {"payload_json": json.dumps({"preparation_policy": "broadcast"})},
        )
    assert chats.created == 0
