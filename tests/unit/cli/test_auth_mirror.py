# -*- coding: utf-8 -*-
"""CLI refresh owns one mirror connection across all selected tenants."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner

from swe.cli import auth_cmd
from swe import database
from swe.app.crons import auth_mirror
from swe.app.crons.auth_state import ResolvedAuthToken


@pytest.mark.parametrize(
    "host",
    [None, "", "mysql", "localhost", "127.0.0.1", "::1"],
)
@pytest.mark.parametrize("connect_timeout", [None, "1"])
def test_refresh_command_preserves_output_with_optional_database(
    tmp_path,
    monkeypatch,
    host,
    connect_timeout,
):
    for name in os.environ:
        if name.startswith("SWE_DB_"):
            monkeypatch.delenv(name)
    if host is not None:
        monkeypatch.setenv("SWE_DB_HOST", host)
    if connect_timeout is not None:
        monkeypatch.setenv("SWE_DB_CONNECT_TIMEOUT", connect_timeout)
    connection = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    factory = Mock(return_value=connection)
    monkeypatch.setattr(database, "DatabaseConnection", factory)
    config_loader = Mock(
        return_value=SimpleNamespace(host=host or "localhost"),
    )
    monkeypatch.setattr(database, "get_database_config", config_loader)
    warning = Mock()
    monkeypatch.setattr(auth_mirror.logger, "warning", warning)
    monkeypatch.setattr(
        auth_cmd,
        "_discover_cron_auth_tenant_ids",
        lambda: ["alice", "bob"],
    )
    monkeypatch.setattr(
        auth_cmd,
        "get_current_effective_tenant_id",
        lambda: None,
    )
    monkeypatch.setattr(
        auth_cmd,
        "get_tenant_secrets_dir",
        lambda tenant: tmp_path / tenant / ".secret",
    )
    seen = []

    async def prefetch(*, tenant_id):
        if host:
            assert auth_mirror._repository.db is connection
        else:
            assert auth_mirror._repository is None
        seen.append(tenant_id)
        return ResolvedAuthToken(
            token="token",
            expires_at=None,
            reused=True,
            cookie_header="cookie",
        )

    monkeypatch.setattr(auth_cmd, "prefetch_auth_token", prefetch)
    result = CliRunner().invoke(auth_cmd.auth_group, ["refresh-token"])
    assert result.exit_code == 0, result.output
    assert "Cron auth synced for 2 tenant(s); 0 refreshed." in result.output
    assert seen == ["alice", "bob"]
    if host:
        config_loader.assert_called_once()
        factory.assert_called_once()
        connection.connect.assert_awaited_once()
        connection.close.assert_awaited_once()
    else:
        config_loader.assert_not_called()
        factory.assert_not_called()
        connection.connect.assert_not_awaited()
        connection.close.assert_not_awaited()
    warning.assert_not_called()
    assert auth_mirror._repository is None
    for tenant in seen:
        assert (tmp_path / tenant / ".secret" / "envs.json").is_file()
