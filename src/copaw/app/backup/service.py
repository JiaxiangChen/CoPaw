# -*- coding: utf-8 -*-
"""Backup service layer."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .config import BackupConfig, load_backup_config
from .models import BackupTask, BackupTaskStatus, BackupTaskType
from .task_store import TaskStore
from .worker import BackupWorker


class BackupService:
    """Backup business logic service."""

    def __init__(self):
        self.task_store = TaskStore()
        self._worker: BackupWorker | None = None
        self._lock = asyncio.Lock()

    def _get_backup_config(self) -> BackupConfig:
        """Get backup config from file."""
        config = load_backup_config()
        if config is None:
            raise HTTPException(
                status_code=400,
                detail="Backup not configured",
            )
        return config

    def _get_worker(self) -> BackupWorker:
        """Get or create worker instance."""
        if self._worker is None:
            backup_config = self._get_backup_config()
            env_config = backup_config.get_active_config()
            if env_config is None:
                raise HTTPException(
                    status_code=400,
                    detail="Backup environment not configured",
                )
            self._worker = BackupWorker(self.task_store, env_config)
        return self._worker

    async def create_backup_task(
        self,
        user_ids: list[str] | None = None,
        instance_id: str | None = None,
        backup_date: str | None = None,
        backup_hour: int | None = None,
    ) -> BackupTask:
        """Create a new backup task.

        Args:
            user_ids: Specific users to backup, or None for all users
            instance_id: Instance identifier for multi-instance deployment
            backup_date: Backup date (YYYY-MM-DD), defaults to today
            backup_hour: Hour of day (0-23), defaults to current hour
        """
        async with self._lock:
            if self.task_store.has_running_task():
                raise HTTPException(
                    status_code=409,
                    detail="Another backup task is already running",
                )

        # Set backup date and hour (Beijing time)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if backup_date is None:
            backup_date = now.strftime("%Y-%m-%d")
        if backup_hour is None:
            backup_hour = now.hour

        task = BackupTask(
            task_id=str(uuid.uuid4()),
            task_type=BackupTaskType.BACKUP,
            status=BackupTaskStatus.PENDING,
            created_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            target_user_ids=user_ids,
            instance_id=instance_id,
            backup_date=backup_date,
            backup_hour=backup_hour,
        )
        self.task_store.save(task)

        # Start async execution
        asyncio.create_task(self._get_worker().run_backup_task(task))

        return task

    async def create_restore_task(
        self,
        date: str,
        hour: int | None = None,
        instance_id: str | None = None,
        user_ids: list[str] | None = None,
    ) -> BackupTask:
        """Create a new restore task.

        Args:
            date: Backup date (YYYY-MM-DD)
            hour: Backup hour (0-23)
            instance_id: Instance identifier
            user_ids: Specific users to restore, or None for all
        """
        async with self._lock:
            if self.task_store.has_running_task():
                raise HTTPException(
                    status_code=409,
                    detail="Another backup task is already running",
                )

        task = BackupTask(
            task_id=str(uuid.uuid4()),
            task_type=BackupTaskType.RESTORE,
            status=BackupTaskStatus.PENDING,
            created_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            backup_date=date,
            backup_hour=hour,
            instance_id=instance_id,
            target_user_ids=user_ids,
        )
        self.task_store.save(task)

        # Start async execution
        asyncio.create_task(self._get_worker().run_restore_task(task))

        return task

    def get_task(self, task_id: str) -> BackupTask | None:
        """Get task by ID."""
        return self.task_store.get(task_id)

    def list_tasks(
        self,
        status: BackupTaskStatus | None = None,
        task_type: str | None = None,
        limit: int = 50,
    ) -> list[BackupTask]:
        """List tasks with filters."""
        return self.task_store.get_all(
            status=status,
            task_type=task_type,
            limit=limit,
        )

    def delete_task(self, task_id: str) -> bool:
        """Delete a task."""
        return self.task_store.delete(task_id)

    def list_available_backups(
        self,
        instance_id: str | None = None,
        date: str | None = None,
        hour: int | None = None,
        user_id: str | None = None,
    ) -> dict:
        """List available backups from S3.

        Args:
            instance_id: Filter by instance ID
            date: Filter by date (YYYY-MM-DD)
            hour: Filter by hour (0-23)
            user_id: Filter by user ID
        """
        backup_config = self._get_backup_config()
        env_config = backup_config.get_active_config()
        if env_config is None:
            raise HTTPException(
                status_code=400,
                detail="Backup environment not configured",
            )

        from .s3_client import S3BackupClient

        s3_client = S3BackupClient(env_config)
        return s3_client.list_backups(
            instance_id=instance_id,
            date=date,
            hour=hour,
            user_id=user_id,
        )
