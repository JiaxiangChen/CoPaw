# -*- coding: utf-8 -*-
"""Batch backup service for multi-instance deployment."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .batch_models import (
    BatchTaskItemStatus,
    BatchTaskStatus,
    InstanceConfig,
    load_instances_config,
    save_instances_config,
    BackupInstancesConfig,
)

logger = logging.getLogger(__name__)

# In-memory storage for batch task status
_batch_tasks: dict[str, BatchTaskStatus] = {}


class BatchBackupService:
    """Service for batch backup/restore operations across multiple instances."""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=120.0)

    async def close(self):
        """Close HTTP client."""
        await self._client.aclose()

    def get_instances(self) -> list[InstanceConfig]:
        """Get all configured instances."""
        config = load_instances_config()
        return config.instances

    def get_enabled_instances(self) -> list[InstanceConfig]:
        """Get all enabled instances."""
        config = load_instances_config()
        return config.get_enabled_instances()

    def save_instances(self, instances: list[InstanceConfig]) -> None:
        """Save instance configuration."""
        config = BackupInstancesConfig(instances=instances)
        save_instances_config(config)

    def get_batch_task(self, batch_id: str) -> BatchTaskStatus | None:
        """Get batch task status by ID."""
        return _batch_tasks.get(batch_id)

    def list_batch_tasks(self, limit: int = 20) -> list[BatchTaskStatus]:
        """List recent batch tasks."""
        tasks = sorted(
            _batch_tasks.values(),
            key=lambda t: t.created_at,
            reverse=True,
        )
        return tasks[:limit]

    async def _call_instance_api(
        self,
        instance: InstanceConfig,
        endpoint: str,
        payload: dict,
    ) -> dict:
        """Call API on a single instance."""
        url = f"{instance.url.rstrip('/')}/api/backup/{endpoint.lstrip('/')}"
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
        except httpx.RequestError as e:
            return {"error": f"Connection error: {str(e)}"}
        except Exception as e:
            return {"error": f"Unexpected error: {str(e)}"}

    async def batch_backup(
        self,
        instances: list[InstanceConfig] | None = None,
        backup_date: str | None = None,
        backup_hour: int | None = None,
    ) -> BatchTaskStatus:
        """Start batch backup for all enabled instances.

        Args:
            instances: Specific instances to backup, or None for all enabled
            backup_date: Backup date (YYYY-MM-DD), defaults to today
            backup_hour: Backup hour (0-23), defaults to current hour

        Returns:
            BatchTaskStatus with batch_id for tracking
        """
        if instances is None:
            instances = self.get_enabled_instances()

        if not instances:
            raise ValueError("No instances to backup")

        # Use Beijing time for defaults
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if backup_date is None:
            backup_date = now.strftime("%Y-%m-%d")
        if backup_hour is None:
            backup_hour = now.hour

        batch_id = str(uuid.uuid4())
        batch_task = BatchTaskStatus(
            batch_id=batch_id,
            task_type="backup",
            created_at=now.isoformat(),
            total=len(instances),
            backup_date=backup_date,
            backup_hour=backup_hour,
            items=[
                BatchTaskItemStatus(
                    instance_id=inst.id,
                    instance_name=inst.name,
                )
                for inst in instances
            ],
        )
        _batch_tasks[batch_id] = batch_task

        # Run backup in background
        asyncio.create_task(self._run_batch_backup(batch_task, instances, backup_date, backup_hour))

        return batch_task

    async def _run_batch_backup(
        self,
        batch_task: BatchTaskStatus,
        instances: list[InstanceConfig],
        backup_date: str,
        backup_hour: int,
    ) -> None:
        """Execute batch backup."""
        for item in batch_task.items:
            instance = next((i for i in instances if i.id == item.instance_id), None)
            if not instance:
                item.status = "failed"
                item.error = "Instance not found"
                batch_task.failed += 1
                continue

            item.status = "running"
            batch_task.status = "running"

            payload = {
                "instance_id": instance.id,
                "backup_date": backup_date,
                "backup_hour": backup_hour,
            }

            result = await self._call_instance_api(instance, "/upload", payload)

            if "error" in result:
                item.status = "failed"
                item.error = result["error"]
                batch_task.failed += 1
            else:
                item.status = "success"
                item.task_id = result.get("task_id")
                item.message = result.get("message", "Backup started")
                batch_task.success += 1

            batch_task.completed += 1

        # Update final status
        if batch_task.failed == 0:
            batch_task.status = "completed"
        elif batch_task.success == 0:
            batch_task.status = "failed"
        else:
            batch_task.status = "partial"

    async def batch_restore(
        self,
        instances: list[InstanceConfig] | None = None,
        backup_date: str | None = None,
        backup_hour: int | None = None,
    ) -> BatchTaskStatus:
        """Start batch restore for all enabled instances.

        Args:
            instances: Specific instances to restore, or None for all enabled
            backup_date: Backup date (YYYY-MM-DD), defaults to today
            backup_hour: Backup hour (0-23), defaults to current hour

        Returns:
            BatchTaskStatus with batch_id for tracking
        """
        if instances is None:
            instances = self.get_enabled_instances()

        if not instances:
            raise ValueError("No instances to restore")

        # Use Beijing time for defaults
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if backup_date is None:
            backup_date = now.strftime("%Y-%m-%d")
        if backup_hour is None:
            backup_hour = now.hour

        batch_id = str(uuid.uuid4())
        batch_task = BatchTaskStatus(
            batch_id=batch_id,
            task_type="restore",
            created_at=now.isoformat(),
            total=len(instances),
            backup_date=backup_date,
            backup_hour=backup_hour,
            items=[
                BatchTaskItemStatus(
                    instance_id=inst.id,
                    instance_name=inst.name,
                )
                for inst in instances
            ],
        )
        _batch_tasks[batch_id] = batch_task

        # Run restore in background
        asyncio.create_task(self._run_batch_restore(batch_task, instances, backup_date, backup_hour))

        return batch_task

    async def _run_batch_restore(
        self,
        batch_task: BatchTaskStatus,
        instances: list[InstanceConfig],
        backup_date: str,
        backup_hour: int,
    ) -> None:
        """Execute batch restore."""
        for item in batch_task.items:
            instance = next((i for i in instances if i.id == item.instance_id), None)
            if not instance:
                item.status = "failed"
                item.error = "Instance not found"
                batch_task.failed += 1
                continue

            item.status = "running"
            batch_task.status = "running"

            payload = {
                "instance_id": instance.id,
                "date": backup_date,
                "hour": backup_hour,
            }

            result = await self._call_instance_api(instance, "/download", payload)

            if "error" in result:
                item.status = "failed"
                item.error = result["error"]
                batch_task.failed += 1
            else:
                item.status = "success"
                item.task_id = result.get("task_id")
                item.message = result.get("message", "Restore started")
                batch_task.success += 1

            batch_task.completed += 1

        # Update final status
        if batch_task.failed == 0:
            batch_task.status = "completed"
        elif batch_task.success == 0:
            batch_task.status = "failed"
        else:
            batch_task.status = "partial"

    async def get_latest_backups(self) -> dict[str, dict]:
        """Get latest backup info for each instance from S3."""
        from .s3_client import S3BackupClient
        from .config import load_backup_config

        backup_config = load_backup_config()
        if not backup_config:
            return {}

        env_config = backup_config.get_active_config()
        if not env_config:
            return {}

        s3_client = S3BackupClient(env_config)
        backups = s3_client.list_backups()

        # Find latest backup for each instance
        latest = {}
        for instance_id, dates in backups.get("backups", {}).items():
            if not dates:
                continue

            # Sort dates descending
            sorted_dates = sorted(dates.keys(), reverse=True)
            if not sorted_dates:
                continue

            latest_date = sorted_dates[0]
            hours = dates[latest_date]
            if not hours:
                continue

            # Sort hours descending
            sorted_hours = sorted(hours.keys(), reverse=True)
            if not sorted_hours:
                continue

            latest_hour = sorted_hours[0]
            users = hours[latest_hour]
            user_count = len(users) if isinstance(users, dict) else 0

            latest[instance_id] = {
                "date": latest_date,
                "hour": latest_hour,
                "user_count": user_count,
            }

        return latest


# Singleton service
_batch_service: BatchBackupService | None = None


def get_batch_service() -> BatchBackupService:
    """Get batch backup service instance."""
    global _batch_service
    if _batch_service is None:
        _batch_service = BatchBackupService()
    return _batch_service