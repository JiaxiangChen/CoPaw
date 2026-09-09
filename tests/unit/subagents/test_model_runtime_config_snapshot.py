# -*- coding: utf-8 -*-
"""Model runtime configuration coverage for SubAgent launch snapshots."""

import json
from pathlib import Path

from swe.app.subagents.launch_snapshot import capture_model_launch_snapshot
from swe.app.subagents.models import SubAgentDefinition
from swe.providers.models import ModelSlotConfig


def test_subagent_model_snapshot_includes_model_runtime_config(
    monkeypatch,
    tmp_path,
) -> None:
    slot = ModelSlotConfig(provider_id="openai", model="gpt-5")

    class Provider:
        def has_model(self, model_id: str) -> bool:
            return model_id == "gpt-5"

        def model_dump(self, **_kwargs):
            return {
                "id": "openai",
                "name": "OpenAI",
                "models": [{"id": "gpt-5", "name": "GPT-5"}],
                "model_configs": {
                    "gpt-5": {
                        "temperature": 0.2,
                        "max_output_length": 4096,
                    },
                },
            }

    class Manager:
        def get_active_model(self):
            return slot

        def get_provider(self, provider_id: str):
            return Provider() if provider_id == "openai" else None

    monkeypatch.setattr(
        "swe.app.subagents.launch_snapshot.ProviderManager.get_instance",
        lambda _tenant_id: Manager(),
    )

    path, resolved = capture_model_launch_snapshot(
        tenant_id="tenant-a",
        run_store_dir=tmp_path,
        run_id="run-1",
        definition=SubAgentDefinition(
            name="worker",
            description="Test worker",
            instruction="Test instruction",
        ),
    )

    assert resolved == slot
    assert path is not None
    snapshot = json.loads(Path(path).read_text())
    assert snapshot["selected"]["provider"]["model_configs"] == {
        "gpt-5": {"temperature": 0.2, "max_output_length": 4096},
    }
