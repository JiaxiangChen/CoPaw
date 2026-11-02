# -*- coding: utf-8 -*-
"""Shell 命令拦截器的租户与来源注入回归测试。"""

import shlex
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.agents.tool_failure import ToolExecutionError
from swe.agents.tools.shell_interceptor import intercept_command
from swe.config.context import resolve_scope_id, tenant_context


def _force_quoted_shell_argument(value: str) -> str:
    if sys.platform == "win32":
        quoted_with_marker = subprocess.list2cmdline([f" {value}"])
        return f'"{quoted_with_marker[2:]}'
    return "'" + value.replace("'", "'\"'\"'") + "'"


@pytest.mark.asyncio
async def test_intercept_swe_cron_injects_logical_tenant_and_source():
    with tenant_context(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id="source-a",
    ):
        command, intercepted = await intercept_command("swe cron list")

    assert intercepted is True
    assert "--tenant-id tenant-a" in command
    assert "--source-id source-a" in command
    assert "dGVuYW50" not in command


@pytest.mark.asyncio
async def test_intercept_swe_cron_create_injects_source_and_user_fields():
    with tenant_context(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id="source-a",
    ):
        command, intercepted = await intercept_command(
            "swe cron create --type agent --name demo --cron '* * * * *'",
        )

    assert intercepted is True
    assert "--tenant-id tenant-a" in command
    assert "--source-id source-a" in command
    assert "--target-user user-a" in command
    assert "--creator-user user-a" in command


@pytest.mark.asyncio
async def test_intercept_swe_cron_list_does_not_inject_create_only_user_fields():
    with tenant_context(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id="source-a",
    ):
        command, intercepted = await intercept_command("swe cron list")

    assert intercepted is True
    assert "--tenant-id tenant-a" in command
    assert "--source-id source-a" in command
    assert "--target-user" not in command
    assert "--creator-user" not in command


@pytest.mark.asyncio
async def test_intercept_swe_cron_keeps_explicit_source_id():
    with tenant_context(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id="source-a",
    ):
        command, intercepted = await intercept_command(
            "swe cron list --source-id explicit-source",
        )

    assert intercepted is True
    assert command.count("--source-id") == 1
    assert "--source-id explicit-source" in command


@pytest.mark.asyncio
async def test_intercept_swe_cron_after_shell_and_operator():
    with tenant_context(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id="source-a",
    ):
        command, intercepted = await intercept_command(
            "echo ready && swe cron list",
        )

    assert intercepted is True
    assert command == (
        "echo ready && swe cron list "
        "--tenant-id tenant-a --source-id source-a"
    )


@pytest.mark.asyncio
async def test_intercept_swe_cron_before_shell_and_operator():
    with tenant_context(
        tenant_id="tenant-a",
        user_id="user-a",
        source_id="source-a",
    ):
        command, intercepted = await intercept_command(
            "swe cron list && echo done",
        )

    assert intercepted is True
    assert command == (
        "swe cron list --tenant-id tenant-a --source-id source-a && echo done"
    )


@pytest.mark.asyncio
async def test_intercept_opencli_injects_resolved_credentials_for_effective_tenant(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    async def fake_resolve_auth_token_for_execution(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            token="resolved-authorization",
            cookie_header="resolved-cookie",
        )

    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        fake_resolve_auth_token_for_execution,
    )

    with tenant_context(
        tenant_id="default",
        user_id="user-a",
        source_id="source-a",
        workspace_dir=tmp_path,
    ):
        command, intercepted = await intercept_command("opencli apps list")

    assert intercepted is True
    assert shlex.split(command) == [
        "opencli",
        "apps",
        "list",
        "--authorization",
        "Bearer resolved-authorization",
        "--cookie",
        "resolved-cookie",
    ]
    assert calls == [
        {
            "tenant_id": resolve_scope_id("default", "source-a"),
            "workspace_dir": tmp_path,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        (
            "opencli --authorization explicit-authorization "
            "--cookie explicit-cookie apps list"
        ),
        (
            "opencli --authorization=explicit-authorization "
            "--cookie=explicit-cookie apps list"
        ),
    ],
)
async def test_intercept_opencli_keeps_both_explicit_credentials(
    monkeypatch,
    command: str,
):
    async def unexpected_resolve(**_kwargs):
        raise AssertionError("explicit credentials must not be replaced")

    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        unexpected_resolve,
    )

    with tenant_context(tenant_id="tenant-a", user_id="user-a"):
        modified, intercepted = await intercept_command(command)

    assert intercepted is False
    assert modified == command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "resolved_authorization", "resolved_cookie", "expected"),
    [
        (
            "opencli --authorization explicit-authorization apps list",
            None,
            "resolved-cookie",
            (
                "opencli --authorization explicit-authorization apps list "
                '--cookie "resolved-cookie"'
            ),
        ),
        (
            "opencli --cookie explicit-cookie apps list",
            "resolved-authorization",
            None,
            (
                "opencli --cookie explicit-cookie apps list "
                '--authorization "Bearer resolved-authorization"'
            ),
        ),
    ],
)
async def test_intercept_opencli_only_injects_missing_credential(
    monkeypatch,
    command: str,
    resolved_authorization: str | None,
    resolved_cookie: str | None,
    expected: str,
):
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                token=resolved_authorization,
                cookie_header=resolved_cookie,
            ),
        ),
    )

    with tenant_context(tenant_id="tenant-a", user_id="user-a"):
        modified, intercepted = await intercept_command(command)

    assert intercepted is True
    assert shlex.split(modified) == shlex.split(expected)


@pytest.mark.asyncio
async def test_intercept_opencli_only_modifies_matching_and_segment(
    monkeypatch,
):
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                token="resolved-authorization",
                cookie_header="resolved-cookie",
            ),
        ),
    )

    with tenant_context(tenant_id="tenant-a", user_id="user-a"):
        command, intercepted = await intercept_command(
            "echo ready && opencli apps list && echo done",
        )

    assert intercepted is True
    expected = (
        "echo ready && opencli apps list "
        '--authorization "Bearer resolved-authorization" '
        '--cookie "resolved-cookie" && echo done'
    )
    assert shlex.split(command) == shlex.split(expected)


@pytest.mark.asyncio
async def test_intercept_opencli_quotes_resolved_credentials_for_platform(
    monkeypatch,
):
    authorization = '{"name": "Alice Example"}'
    cookie = "session=Alice Example; mode=full"
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                token=authorization,
                cookie_header=cookie,
            ),
        ),
    )

    with tenant_context(tenant_id="tenant-a", user_id="user-a"):
        command, intercepted = await intercept_command("opencli apps list")

    quoted_authorization = _force_quoted_shell_argument(
        f"Bearer {authorization}",
    )
    quoted_cookie = _force_quoted_shell_argument(cookie)
    assert intercepted is True
    assert command == (
        f"opencli apps list --authorization {quoted_authorization} "
        f"--cookie {quoted_cookie}"
    )


@pytest.mark.asyncio
async def test_intercept_opencli_quotes_trailing_backslashes(monkeypatch):
    authorization = "resolved-authorization\\"
    cookie = "resolved-cookie\\"
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                token=authorization,
                cookie_header=cookie,
            ),
        ),
    )

    with tenant_context(tenant_id="tenant-a", user_id="user-a"):
        command, intercepted = await intercept_command("opencli apps list")

    assert intercepted is True
    assert command == (
        "opencli apps list "
        f"--authorization {_force_quoted_shell_argument(f'Bearer {authorization}')} "
        f"--cookie {_force_quoted_shell_argument(cookie)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    ["Bearer existing-token", "bearer existing-token"],
)
async def test_intercept_opencli_does_not_duplicate_bearer_prefix(
    monkeypatch,
    authorization: str,
):
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                token=authorization,
                cookie_header="resolved-cookie",
            ),
        ),
    )

    with tenant_context(tenant_id="tenant-a", user_id="user-a"):
        command, intercepted = await intercept_command("opencli apps list")

    assert intercepted is True
    assert shlex.split(command) == [
        "opencli",
        "apps",
        "list",
        "--authorization",
        authorization,
        "--cookie",
        "resolved-cookie",
    ]


@pytest.mark.asyncio
async def test_intercept_opencli_fails_when_authorization_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                token=None,
                cookie_header="resolved-cookie",
            ),
        ),
    )

    with (
        tenant_context(tenant_id="tenant-a", user_id="user-a"),
        pytest.raises(ToolExecutionError) as exc_info,
    ):
        await intercept_command("opencli apps list")

    assert exc_info.value.error_type == "permission_denied"
    assert "OpenCLI authorization is not configured" in exc_info.value.detail


@pytest.mark.asyncio
async def test_intercept_opencli_fails_when_cookie_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                token="resolved-authorization",
                cookie_header=None,
            ),
        ),
    )

    with (
        tenant_context(tenant_id="tenant-a", user_id="user-a"),
        pytest.raises(ToolExecutionError) as exc_info,
    ):
        await intercept_command("opencli apps list")

    assert exc_info.value.error_type == "permission_denied"
    assert "OpenCLI cookie is not configured" in exc_info.value.detail


@pytest.mark.asyncio
async def test_intercept_opencli_maps_expired_auth_state_to_tool_failure(
    monkeypatch,
):
    async def expired_auth_state(**_kwargs):
        raise ValueError("cron auth user_info is expired")

    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        expired_auth_state,
    )

    with (
        tenant_context(tenant_id="tenant-a", user_id="user-a"),
        pytest.raises(ToolExecutionError) as exc_info,
    ):
        await intercept_command("opencli apps list")

    assert exc_info.value.error_type == "permission_denied"
    assert "OpenCLI authentication has expired" in exc_info.value.detail


@pytest.mark.asyncio
async def test_intercept_opencli_does_not_log_credentials(
    monkeypatch,
):
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor."
        "resolve_auth_token_for_execution",
        AsyncMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                token="secret-authorization",
                cookie_header="secret-cookie",
            ),
        ),
    )

    messages = []
    monkeypatch.setattr(
        "swe.agents.tools.shell_interceptor.logger.info",
        lambda *args, **_kwargs: messages.append(args),
    )

    with tenant_context(tenant_id="tenant-a", user_id="user-a"):
        await intercept_command("opencli apps list")

    assert messages == [("Shell command intercepted",)]
