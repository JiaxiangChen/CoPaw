#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pre-deployment backup script for CoPaw user data.

Creates backup zip files and uploads to S3-compatible storage.

Usage:
    python predeploy_backup.py --instance-id prod-01 --date 2026-03-30 --hour 14

Zip structure:
    {user_id}.zip
    ├── config.json
    ├── AGENTS.md
    ├── ... (other files from working/{user_id}/)
    └── .secret/
        └── providers.json
        └── ... (other files from working.secret/{user_id}/)

S3 path: {prefix}/{instance_id}/{date}/{hour:02d}/{user_id}.zip
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

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
    """Compress a directory into a zip file."""
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


def load_backup_config(config_path: Optional[Path] = None) -> dict:
    """Load backup configuration from file."""
    if config_path is None:
        config_path = Path.home() / ".copaw" / "backup.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Backup config not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_active_env_config(config: dict) -> dict:
    """Get the active environment configuration."""
    env = os.environ.get("COPAW_BACKUP_ENV", "dev")
    envs = config.get("environments", {})

    if env not in envs:
        raise ValueError(
            f"Environment '{env}' not found in config. "
            f"Available: {list(envs.keys())}"
        )

    return envs[env]


def list_users(working_dir: Path) -> list[str]:
    """List all user IDs in the working directory."""
    users = []

    if not working_dir.exists():
        logger.warning(f"Working directory does not exist: {working_dir}")
        return users

    # Exclude special directories
    special_dirs = {
        ".rollback", "active_skills", "customized_skills",
        "memory", "custom_channels", "models"
    }

    for item in working_dir.iterdir():
        if item.is_dir() and item.name not in special_dirs and not item.name.startswith("."):
            users.append(item.name)

    return sorted(users)


def create_backup_zip(
    user_id: str,
    working_dir: Path,
    output_path: Path,
    compress_level: int = 1,
) -> bool:
    """Create a backup zip file for a single user.

    Zip structure:
    - Files from working/{user_id}/ at root level
    - Files from working.secret/{user_id}/ under .secret/

    Args:
        user_id: User identifier
        working_dir: Base working directory (~/.copaw)
        output_path: Path to write the zip file
        compress_level: Compression level (0-9)

    Returns:
        True if successful, False otherwise
    """
    user_dir = working_dir / user_id
    secret_dir = Path(f"{working_dir}.secret").expanduser().resolve() / user_id

    skipped_files = []

    try:
        with zipfile.ZipFile(
            output_path,
            "w",
            zipfile.ZIP_DEFLATED,
            compresslevel=compress_level,
        ) as zf:
            _compress_directory(zf, user_dir, user_dir, skipped=skipped_files)
            _compress_directory(zf, secret_dir, secret_dir, prefix=".secret/", skipped=skipped_files)

        # Check if any content was added
        if not output_path.exists() or output_path.stat().st_size == 0:
            logger.warning(f"No content for user {user_id}")
            output_path.unlink(missing_ok=True)
            return False

        if skipped_files:
            logger.warning(f"Skipped {len(skipped_files)} files for user {user_id}")

        return True

    except Exception as e:
        logger.error(f"Failed to create backup for user {user_id}: {e}")
        output_path.unlink(missing_ok=True)
        return False


def upload_to_s3(
    local_path: Path,
    config: dict,
    instance_id: str,
    date: str,
    hour: int,
    user_id: str,
) -> str:
    """Upload a file to S3.

    Returns the S3 key.
    """
    try:
        import boto3
    except ImportError:
        raise ImportError("boto3 is required. Install with: pip install boto3")

    endpoint_url = config.get("endpoint_url") or None

    s3 = boto3.client(
        "s3",
        aws_access_key_id=config["aws_access_key_id"],
        aws_secret_access_key=config["aws_secret_access_key"],
        region_name=config.get("s3_region", "cn-north-1"),
        endpoint_url=endpoint_url,
    )

    bucket = config["s3_bucket"]
    prefix = config.get("s3_prefix", "cmbswe")

    # S3 path: {prefix}/{instance_id}/{date}/{hour:02d}/{user_id}.zip
    s3_key = f"{prefix}/{instance_id}/{date}/{str(hour).zfill(2)}/{user_id}.zip"

    s3.upload_file(str(local_path), bucket, s3_key)

    return s3_key


def main():
    parser = argparse.ArgumentParser(description="Pre-deployment backup script")
    parser.add_argument("--instance-id", required=True, help="Instance ID")
    parser.add_argument("--date", required=True, help="Date (YYYY-MM-DD)")
    parser.add_argument("--hour", type=int, required=True, help="Hour (0-23)")
    parser.add_argument("--user-id", help="Specific user ID (default: all users)")
    parser.add_argument("--config", type=Path, help="Path to backup.json")
    parser.add_argument("--working-dir", type=Path, help="Working directory (default: ~/.copaw)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--skip-upload", action="store_true", help="Create zips but skip upload")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temp files")
    parser.add_argument("--compress-level", type=int, default=6, help="Compression level 0-9")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate date
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        parser.error(f"Invalid date format: {args.date}")

    # Get working directory
    working_dir = args.working_dir or Path.home() / ".copaw"
    working_dir = working_dir.expanduser().resolve()
    logger.info(f"Working directory: {working_dir}")

    # Load config
    try:
        backup_config = load_backup_config(args.config)
        env_config = get_active_env_config(backup_config)
        logger.info(f"Environment: {os.environ.get('COPAW_BACKUP_ENV', 'dev')}")
        logger.info(f"S3 bucket: {env_config['s3_bucket']}")
    except Exception as e:
        if not args.dry_run and not args.skip_upload:
            parser.error(str(e))
        env_config = {}
        logger.warning(f"Config load failed: {e}")

    # Get users
    users = [args.user_id] if args.user_id else list_users(working_dir)
    if not users:
        logger.warning("No users found")
        return 0

    logger.info(f"Users to backup: {len(users)}")

    if args.dry_run:
        for u in users:
            logger.info(f"  Would backup: {u}")
        logger.info("Dry run complete")
        return 0

    # Create temp dir
    temp_dir = Path(tempfile.mkdtemp(prefix="copaw_backup_"))

    success = 0
    failed = 0

    try:
        for user_id in users:
            logger.info(f"Processing: {user_id}")

            zip_path = temp_dir / f"{user_id}.zip"

            if not create_backup_zip(user_id, working_dir, zip_path, args.compress_level):
                failed += 1
                continue

            size_kb = zip_path.stat().st_size / 1024
            logger.info(f"  Zip created: {size_kb:.1f} KB")

            if args.skip_upload:
                dest = Path.cwd() / f"{user_id}.zip"
                shutil.copy(zip_path, dest)
                logger.info(f"  Saved to: {dest}")
                success += 1
                continue

            # Upload
            try:
                s3_key = upload_to_s3(
                    zip_path, env_config, args.instance_id, args.date, args.hour, user_id
                )
                logger.info(f"  Uploaded: s3://{env_config['s3_bucket']}/{s3_key}")
                success += 1
            except Exception as e:
                logger.error(f"  Upload failed: {e}")
                failed += 1

    finally:
        if args.keep_temp:
            logger.info(f"Temp dir kept: {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)

    logger.info(f"Done: {success} succeeded, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())