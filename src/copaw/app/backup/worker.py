# -*- coding: utf-8 -*-
"""Async worker for executing backup/restore tasks."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ...constant import DEFAULT_WORKING_DIR, get_secret_dir
from .config import BackupEnvironmentConfig
from .models import BackupTask, BackupTaskStatus
from .s3_client import S3BackupClient
from .task_store import TaskStore

logger = logging.getLogger(__name__)

# Beijing timezone for consistent time handling
BJ_TZ = ZoneInfo("Asia/Shanghai")

# Encodings to try when UTF-8 fails (common for Chinese Windows systems)
FALLBACK_ENCODINGS = ['gb18030', 'gbk', 'big5', 'cp936', 'latin-1']


def safe_archive_name(file_path: Path, base_dir: Path) -> str | None:
    """Safely convert file path to archive name with multi-encoding support."""
    try:
        relative = file_path.relative_to(base_dir)
        name = str(relative)

        try:
            name.encode('utf-8')
            return name
        except UnicodeEncodeError:
            pass

        for encoding in FALLBACK_ENCODINGS:
            try:
                encoded = name.encode(encoding, errors='strict')
                decoded = encoded.decode(encoding)
                return decoded.encode('utf-8', errors='replace').decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue

        try:
            return name.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
        except Exception:
            logger.warning(f"Cannot encode file name: {file_path}")
            return None
    except Exception as e:
        logger.warning(f"Error processing path {file_path}: {e}")
        return None


def _compress_directory(
    zf: zipfile.ZipFile,
    source_dir: Path,
    base_dir: Path,
    prefix: str = "",
    skipped: list | None = None,
) -> None:
    """Compress a directory into a zip file.

    Args:
        zf: ZipFile object to write to.
        source_dir: Directory to compress.
        base_dir: Base directory for relative paths.
        prefix: Optional prefix for archive names (e.g., ".secret/").
        skipped: Optional list to collect skipped file paths.
    """
    if not source_dir.exists():
        return

    for file in source_dir.rglob("*"):
        try:
            arcname = safe_archive_name(file, base_dir)
            if not arcname:
                if skipped is not None:
                    skipped.append(str(file))
                continue

            if prefix:
                arcname = f"{prefix}{arcname}"

            if file.is_file():
                zf.write(file, arcname)
            elif file.is_dir() and not any(file.iterdir()):
                zf.writestr(f"{arcname}/", "")
        except (PermissionError, OSError) as e:
            logger.warning(f"Error accessing {file}: {e}")
            if skipped is not None:
                skipped.append(str(file))


class BackupWorker:
    """Async worker for backup and restore operations."""

    def __init__(self, task_store: TaskStore, config: BackupEnvironmentConfig):
        self.task_store = task_store
        self.config = config
        self.s3_client = S3BackupClient(config)

    # pylint: disable=too-many-statements
    async def run_backup_task(
        self,
        task: BackupTask,
    ) -> None:
        """Execute a backup task."""
        task.status = BackupTaskStatus.RUNNING
        task.started_at = datetime.now(BJ_TZ)
        self.task_store.save(task)

        try:
            if task.target_user_id:
                user_ids = [task.target_user_id]
            else:
                user_ids = self._get_all_user_ids()

            # Check for empty user list to avoid division by zero
            if not user_ids:
                task.status = BackupTaskStatus.COMPLETED
                task.current_step = "completed"
                task.progress_percent = 100
                task.completed_at = datetime.now(BJ_TZ)
                self.task_store.save(task)
                return

            task.total_users = len(user_ids)
            task.current_step = "compressing"
            self.task_store.save(task)

            s3_keys = []
            local_paths = []

            # Use task's backup_date and backup_hour
            date_str = task.backup_date or datetime.now(BJ_TZ).strftime("%Y-%m-%d")
            hour = task.backup_hour if task.backup_hour is not None else datetime.now(BJ_TZ).hour
            instance_id = task.instance_id or "default"

            for i, user_id in enumerate(user_ids):
                # Update progress (1-indexed)
                task.processed_users = i + 1
                task.progress_percent = int(((i + 1) / len(user_ids)) * 50)
                self.task_store.save(task)

                # Compress user directory
                user_dir = DEFAULT_WORKING_DIR / user_id
                if not user_dir.exists():
                    continue

                zip_path = (
                    Path(tempfile.gettempdir()) / f"backup_{user_id}.zip"
                )
                await self._compress_user(user_id, user_dir, zip_path)
                local_paths.append(str(zip_path))

            # Upload to S3
            task.current_step = "uploading"
            self.task_store.save(task)

            # Check for empty local_paths to avoid division by zero
            if not local_paths:
                task.status = BackupTaskStatus.COMPLETED
                task.current_step = "completed"
                task.progress_percent = 100
                task.completed_at = datetime.now(BJ_TZ)
                self.task_store.save(task)
                return

            for i, zip_path_str in enumerate(local_paths):
                zip_path = Path(zip_path_str)
                user_id = zip_path.stem.replace("backup_", "")
                s3_key = await asyncio.to_thread(
                    self.s3_client.upload,
                    zip_path,
                    instance_id,
                    date_str,
                    hour,
                    user_id,
                )
                s3_keys.append(s3_key)

                # Update progress (1-indexed)
                task.processed_users = i + 1
                task.progress_percent = 50 + int(
                    ((i + 1) / len(local_paths)) * 50,
                )
                self.task_store.save(task)

            task.s3_keys = s3_keys
            task.local_zip_paths = local_paths
            task.status = BackupTaskStatus.COMPLETED
            task.current_step = "completed"
            task.progress_percent = 100

        except Exception as e:
            task.status = BackupTaskStatus.FAILED
            task.error_message = str(e)
            task.current_step = "failed"
        finally:
            task.completed_at = datetime.now(BJ_TZ)
            self.task_store.save(task)
            # Cleanup temp files
            for path in task.local_zip_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp file {path}: {e}")

    # pylint: disable=too-many-statements
    async def run_restore_task(
        self,
        task: BackupTask,
    ) -> None:
        """Execute a restore task."""
        task.status = BackupTaskStatus.RUNNING
        task.started_at = datetime.now(BJ_TZ)
        self.task_store.save(task)

        rollback_paths = []

        try:
            instance_id = task.instance_id or "default"
            backup_date = task.backup_date
            backup_hour = task.backup_hour

            if not backup_date:
                raise ValueError("backup_date is required for restore task")

            # Get target users
            if task.target_user_ids:
                user_ids = task.target_user_ids
            else:
                # List all backups for the date/hour/instance
                backups = self.s3_client.list_backups(
                    instance_id=instance_id,
                    date=backup_date,
                    hour=backup_hour,
                )
                user_ids = list(
                    backups["backups"]
                    .get(instance_id, {})
                    .get(backup_date, {})
                    .get(backup_hour if backup_hour is not None else 0, {})
                    .keys(),
                )

            # Check for empty user list to avoid division by zero
            if not user_ids:
                task.status = BackupTaskStatus.COMPLETED
                task.current_step = "completed"
                task.progress_percent = 100
                task.completed_at = datetime.now(BJ_TZ)
                self.task_store.save(task)
                return

            task.total_users = len(user_ids)
            task.current_step = "backing_up_current"
            self.task_store.save(task)

            # Backup current data for rollback
            for i, user_id in enumerate(user_ids):
                user_dir = DEFAULT_WORKING_DIR / user_id
                if user_dir.exists():
                    rollback_path = await self._create_rollback_backup(
                        task.task_id,
                        user_id,
                        user_dir,
                    )
                    rollback_paths.append(rollback_path)

            task.rollback_data_paths = rollback_paths
            task.current_step = "downloading"
            self.task_store.save(task)

            # Download and restore
            restored_users = []
            for i, user_id in enumerate(user_ids):
                task.processed_users = i + 1
                task.progress_percent = int(((i + 1) / len(user_ids)) * 50)
                self.task_store.save(task)

                # Get the backup hour (use task's hour or find latest available)
                hour_to_restore = backup_hour
                if hour_to_restore is None:
                    # Find the latest hour with backup for this user
                    backups = self.s3_client.list_backups(
                        instance_id=instance_id,
                        date=backup_date,
                    )
                    hours = list(
                        backups["backups"]
                        .get(instance_id, {})
                        .get(backup_date, {})
                        .keys()
                    )
                    if hours:
                        hour_to_restore = max(hours)
                    else:
                        logger.warning(
                            f"No backup found for {instance_id}/{backup_date}/{user_id}"
                        )
                        continue

                s3_key = self.s3_client.get_backup_key(
                    instance_id,
                    backup_date,
                    hour_to_restore,
                    user_id,
                )
                zip_path = (
                    Path(tempfile.gettempdir()) / f"restore_{user_id}.zip"
                )

                await asyncio.to_thread(
                    self.s3_client.download,
                    s3_key,
                    zip_path,
                )

                user_dir = DEFAULT_WORKING_DIR / user_id
                await self._extract_zip(zip_path, user_dir, user_id)
                restored_users.append(user_id)

            task.restored_users = restored_users

            # Clean up rollback data after successful restore
            rollback_dir = DEFAULT_WORKING_DIR / ".rollback" / task.task_id
            if rollback_dir.exists():
                try:
                    shutil.rmtree(rollback_dir)
                except Exception as e:
                    logger.warning(f"Failed to cleanup rollback dir: {e}")

            task.current_step = "completed"
            task.status = BackupTaskStatus.COMPLETED
            task.progress_percent = 100

        except Exception as e:
            task.error_message = str(e)
            task.current_step = "rolling_back"
            task.status = BackupTaskStatus.ROLLING_BACK
            self.task_store.save(task)

            # Rollback all users
            await self._rollback_all(rollback_paths)

            task.status = BackupTaskStatus.ROLLED_BACK
        finally:
            task.completed_at = datetime.now(BJ_TZ)
            self.task_store.save(task)

    def _get_all_user_ids(self) -> list[str]:
        """Get all user IDs from working directory."""
        from ...constant import list_all_user_ids

        return list_all_user_ids()

    async def _compress_user(
        self,
        user_id: str,
        user_dir: Path,
        zip_path: Path,
    ) -> str:
        """Compress user directory to zip."""

        def _do_compress():
            skipped_files = []
            with zipfile.ZipFile(
                zip_path,
                "w",
                zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as zf:
                _compress_directory(zf, user_dir, user_dir, skipped=skipped_files)
                _compress_directory(
                    zf,
                    get_secret_dir(user_id),
                    get_secret_dir(user_id),
                    prefix=".secret/",
                    skipped=skipped_files,
                )

            if skipped_files:
                logger.warning(
                    f"Skipped {len(skipped_files)} files due to encoding/access issues for user {user_id}"
                )

            return str(zip_path)

        return await asyncio.to_thread(_do_compress)

    async def _create_rollback_backup(
        self,
        task_id: str,
        user_id: str,
        user_dir: Path,
    ) -> str:
        """Create a backup of current data before restore."""
        rollback_dir = DEFAULT_WORKING_DIR / ".rollback" / task_id
        rollback_dir.mkdir(parents=True, exist_ok=True)
        zip_path = rollback_dir / f"{user_id}.zip"

        def _do_compress():
            with zipfile.ZipFile(
                zip_path,
                "w",
                zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as zf:
                _compress_directory(zf, user_dir, user_dir)
                _compress_directory(
                    zf,
                    get_secret_dir(user_id),
                    get_secret_dir(user_id),
                    prefix=".secret/",
                )
            return str(zip_path)

        await asyncio.to_thread(_do_compress)
        return str(zip_path)

    async def _extract_zip(
        self,
        zip_path: Path,
        target_dir: Path,
        user_id: str,
    ) -> None:
        """Extract zip to target, routing .secret/ to secret directory.

        Args:
            zip_path: Path to the zip file to extract.
            target_dir: Target directory for non-secret files.
            user_id: User ID for determining secret directory.

        Raises:
            ValueError: If path traversal is detected in zip entries.
        """

        def _do_extract():
            target_dir.mkdir(parents=True, exist_ok=True)
            secret_dir = get_secret_dir(user_id)
            secret_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    # Determine target directory based on prefix
                    if member.filename.startswith(".secret/"):
                        # Route to secret directory
                        relative_path = member.filename[
                            8:
                        ]  # Remove .secret/ prefix
                        if (
                            not relative_path
                        ):  # Skip if it's just .secret/ directory
                            continue
                        extract_dir = secret_dir
                        dest_path = secret_dir / relative_path
                    else:
                        # Route to target directory
                        relative_path = member.filename
                        extract_dir = target_dir
                        dest_path = target_dir / relative_path

                    # Validate path traversal
                    resolved_dest = dest_path.resolve()
                    resolved_extract = extract_dir.resolve()
                    if not str(resolved_dest).startswith(
                        str(resolved_extract),
                    ):
                        raise ValueError(
                            "Path traversal detected in zip entry: "
                            f"{member.filename}",
                        )

                    # Extract the file/directory
                    if member.is_dir():
                        dest_path.mkdir(parents=True, exist_ok=True)
                    else:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as source, open(
                            dest_path,
                            "wb",
                        ) as target:
                            target.write(source.read())

        await asyncio.to_thread(_do_extract)

    async def _rollback_all(self, rollback_paths: list[str]) -> None:
        """Rollback all users to pre-restore state."""
        for rollback_path in rollback_paths:
            try:
                path = Path(rollback_path)
                if not path.exists():
                    continue
                user_id = path.stem
                user_dir = DEFAULT_WORKING_DIR / user_id

                # Remove current data
                if user_dir.exists():
                    shutil.rmtree(user_dir)

                # Restore from rollback
                await self._extract_zip(path, user_dir, user_id)
            except Exception as e:
                # Continue rollback for other users
                logger.error(f"Failed to rollback {rollback_path}: {e}")
