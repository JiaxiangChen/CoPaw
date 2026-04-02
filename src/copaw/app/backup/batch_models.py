# -*- coding: utf-8 -*-
"""Multi-instance backup configuration and models."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class InstanceConfig(BaseModel):
    """Single instance configuration."""

    id: str = Field(..., description="Instance identifier (e.g., instance-01)")
    name: str = Field(..., description="Display name (e.g., 容器1-生产)")
    url: str = Field(..., description="Instance URL (e.g., https://app1.example.com)")
    enabled: bool = Field(default=True, description="Whether this instance is enabled")


class BackupInstancesConfig(BaseModel):
    """Configuration for all backup instances."""

    instances: list[InstanceConfig] = Field(default_factory=list)

    def get_instance(self, instance_id: str) -> Optional[InstanceConfig]:
        """Get instance by ID."""
        for inst in self.instances:
            if inst.id == instance_id:
                return inst
        return None

    def get_enabled_instances(self) -> list[InstanceConfig]:
        """Get all enabled instances."""
        return [inst for inst in self.instances if inst.enabled]


# Config file path
def get_instances_config_path() -> Path:
    """Get the path to instances configuration file."""
    return Path.home() / ".copaw" / "backup_instances.json"


def load_instances_config() -> BackupInstancesConfig:
    """Load instances configuration from file."""
    config_path = get_instances_config_path()

    if not config_path.exists():
        # Return empty config with a note
        return BackupInstancesConfig(instances=[])

    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return BackupInstancesConfig(**data)
    except Exception:
        return BackupInstancesConfig(instances=[])


def save_instances_config(config: BackupInstancesConfig) -> None:
    """Save instances configuration to file."""
    import json

    config_path = get_instances_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config.model_dump(mode="json"), f, indent=2, ensure_ascii=False)


# Batch task status models
class BatchTaskItemStatus(BaseModel):
    """Status of a single instance in a batch task."""

    instance_id: str
    instance_name: str
    status: str = "pending"  # pending, running, success, failed
    task_id: Optional[str] = None
    message: str = ""
    error: Optional[str] = None


class BatchTaskStatus(BaseModel):
    """Status of a batch backup/restore task."""

    batch_id: str
    task_type: str  # "backup" or "restore"
    created_at: str
    total: int = 0
    completed: int = 0
    success: int = 0
    failed: int = 0
    status: str = "running"  # pending, running, completed, partial
    items: list[BatchTaskItemStatus] = Field(default_factory=list)
    backup_date: Optional[str] = None
    backup_hour: Optional[int] = None