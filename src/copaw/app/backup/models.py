# -*- coding: utf-8 -*-
"""Backup feature data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class BackupTaskStatus(str, Enum):
    """Backup task status states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class BackupTaskType(str, Enum):
    """Backup task types."""

    BACKUP = "backup"
    RESTORE = "restore"


class BackupTask(BaseModel):
    """Backup task model."""

    task_id: str
    task_type: BackupTaskType
    user_id: Optional[str] = None
    status: BackupTaskStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Input parameters
    target_user_id: Optional[str] = None
    target_user_ids: Optional[list[str]] = None
    backup_date: Optional[str] = None  # YYYY-MM-DD
    backup_hour: Optional[int] = None  # 0-23, defaults to current hour
    instance_id: Optional[str] = None  # Instance identifier for multi-instance deployment

    # Progress info
    current_step: str = ""
    progress_percent: int = Field(default=0, ge=0, le=100)
    processed_users: int = Field(default=0, ge=0)
    total_users: int = Field(default=0, ge=0)

    # Results
    s3_keys: list[str] = Field(default_factory=list)
    local_zip_paths: list[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    rollback_data_paths: list[str] = Field(default_factory=list)
    restored_users: list[str] = Field(default_factory=list)  # All users restored (download task)

    def is_terminal(self) -> bool:
        """Check if task has reached terminal state."""
        return self.status in {
            BackupTaskStatus.COMPLETED,
            BackupTaskStatus.FAILED,
            BackupTaskStatus.ROLLED_BACK,
        }

    def can_delete(self) -> bool:
        """Check if task can be deleted."""
        return self.status != BackupTaskStatus.RUNNING
