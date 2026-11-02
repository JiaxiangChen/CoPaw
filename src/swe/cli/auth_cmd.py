# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

import click

from ..app.crons.auth_state import CRON_AUTH_FILE_NAME, prefetch_auth_token
from ..app.auth import (
    _hash_password,
    _load_auth_data,
    _save_auth_data,
    is_auth_enabled,
)
from ..config.context import (
    get_current_effective_tenant_id,
    resolve_request_effective_tenant_id,
)
from ..config.utils import get_tenant_secrets_dir
from ..constant import WORKING_DIR
from ..envs.store import load_envs, save_envs

CRON_AUTH_TOKEN_ENV_KEY = "token"
CRON_AUTH_COOKIE_ENV_KEY = "cookie"


@click.group("auth", help="Manage web authentication.")
def auth_group() -> None:
    """Manage web authentication."""


def _has_configured_cron_user_info(auth_path: Path) -> bool:
    try:
        with open(auth_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get("user_info"))


def _discover_cron_auth_tenant_ids() -> list[str]:
    tenant_ids: list[str] = []
    if not WORKING_DIR.is_dir():
        return tenant_ids
    for tenant_dir in sorted(
        WORKING_DIR.iterdir(),
        key=lambda item: item.name,
    ):
        if not tenant_dir.is_dir() or tenant_dir.name.startswith("."):
            continue
        auth_path = tenant_dir / ".secret" / CRON_AUTH_FILE_NAME
        if auth_path.is_file() and _has_configured_cron_user_info(auth_path):
            tenant_ids.append(tenant_dir.name)
    return tenant_ids


async def _sync_cron_auth_to_envs(tenant_id: str | None) -> tuple[str, str]:
    try:
        resolved = await prefetch_auth_token(tenant_id=tenant_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if not resolved.token:
        raise click.ClickException(
            "cron auth token is not configured; configure cron auth first.",
        )
    if not resolved.cookie_header:
        raise click.ClickException(
            "cron auth cookie is not configured; configure cron auth first.",
        )

    envs_path = get_tenant_secrets_dir(tenant_id) / "envs.json"
    envs = await asyncio.to_thread(load_envs, envs_path)
    envs[CRON_AUTH_TOKEN_ENV_KEY] = resolved.token
    envs[CRON_AUTH_COOKIE_ENV_KEY] = resolved.cookie_header
    await asyncio.to_thread(save_envs, envs, envs_path)

    status = "reused" if resolved.reused else "refreshed"
    expires_at = (
        resolved.expires_at.isoformat() if resolved.expires_at else "unknown"
    )
    return status, (
        f"{tenant_id or 'default'}: {status}; "
        f"synced {CRON_AUTH_TOKEN_ENV_KEY} and "
        f"{CRON_AUTH_COOKIE_ENV_KEY} to {envs_path} "
        f"(expires_at={expires_at})."
    )


@auth_group.command("refresh-token")
@click.option("--tenant-id", default=None, hidden=True)
@click.option("--source-id", default=None, hidden=True)
def refresh_token_cmd(
    tenant_id: str | None,
    source_id: str | None,
) -> None:
    """Refresh cron auth token and sync it to tenant envs.json."""
    effective_tenant_id = (
        resolve_request_effective_tenant_id(tenant_id, source_id)
        if tenant_id is not None
        else get_current_effective_tenant_id()
    )
    tenant_ids = (
        [effective_tenant_id]
        if effective_tenant_id is not None
        else (_discover_cron_auth_tenant_ids())
    )
    if not tenant_ids:
        raise click.ClickException(
            "cron auth user_info is not configured; "
            "configure cron auth first.",
        )

    asyncio.run(_refresh_tokens(tenant_ids))


async def _refresh_tokens(tenant_ids: list[str | None]) -> None:
    from ..app.crons.auth_mirror import cli_cron_auth_mirror

    async with cli_cron_auth_mirror():
        await _sync_tenant_tokens(tenant_ids)


async def _sync_tenant_tokens(tenant_ids: list[str | None]) -> None:
    messages: list[str] = []
    refreshed_count = 0
    for target_tenant_id in tenant_ids:
        status, message = await _sync_cron_auth_to_envs(target_tenant_id)
        if status == "refreshed":
            refreshed_count += 1
        messages.append(message)

    click.echo(
        f"✓ Cron auth synced for {len(messages)} tenant(s); "
        f"{refreshed_count} refreshed.",
    )
    for message in messages:
        click.echo(f"  - {message}")


@auth_group.command("reset-password")
def reset_password_cmd() -> None:
    """Reset the password for the registered web user."""
    if not is_auth_enabled():
        click.echo(
            "Authentication is not enabled.\n"
            "Set SWE_AUTH_ENABLED=true to enable it first.",
        )
        return

    data = _load_auth_data()

    if data.get("_auth_load_error"):
        raise click.ClickException(
            "Failed to read auth data. Check auth.json for corruption.",
        )

    user = data.get("user")
    if not user:
        click.echo("No registered user found. Nothing to reset.")
        return

    username = user.get("username", "<unknown>")
    click.echo(f"Resetting password for user: {username}")

    new_password = click.prompt(
        "New password",
        hide_input=True,
        confirmation_prompt=True,
    )

    if not new_password or not new_password.strip():
        raise click.ClickException("Password cannot be empty.")

    pw_hash, salt = _hash_password(new_password)
    data["user"]["password_hash"] = pw_hash
    data["user"]["password_salt"] = salt

    # Invalidate existing tokens by rotating jwt_secret
    data["jwt_secret"] = secrets.token_hex(32)

    _save_auth_data(data)
    click.echo(
        "✓ Password reset successfully. "
        "All existing sessions have been invalidated.",
    )
