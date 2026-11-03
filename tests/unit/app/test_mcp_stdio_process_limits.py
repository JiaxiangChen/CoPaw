# -*- coding: utf-8 -*-
"""Tests for tenant-aware MCP stdio process-limit enforcement."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from swe.config.config import Config, MCPClientConfig
from swe.config.context import encode_scope_id, tenant_context
from swe.config.utils import save_config
from swe.envs.store import save_envs

_REACT_AGENT_TOOL_EXPORTS = (
    "browser_use",
    "desktop_screenshot",
    "edit_file",
    "execute_shell_command",
    "get_current_time",
    "glob_search",
    "grep_search",
    "read_file",
    "send_file_to_user",
    "write_file",
    "create_memory_search_tool",
)


def _write_process_limit_config(
    base_dir: Path,
    tenant_id: str,
    *,
    enabled: bool,
    mcp_stdio: bool = True,
    cpu_time_limit_seconds: int | None = None,
    memory_max_mb: int | None = None,
) -> None:
    save_config(
        Config.model_validate(
            {
                "security": {
                    "process_limits": {
                        "enabled": enabled,
                        "shell": True,
                        "mcp_stdio": mcp_stdio,
                        "cpu_time_limit_seconds": cpu_time_limit_seconds,
                        "memory_max_mb": memory_max_mb,
                    },
                },
            },
        ),
        base_dir / "config.json",
    )


def _write_scope_env(
    base_dir: Path,
    tenant_id: str,
    source_id: str,
    envs: dict[str, str],
) -> None:
    scope_id = encode_scope_id(tenant_id, source_id)
    save_envs(envs, base_dir / scope_id / ".secret" / "envs.json")


@pytest.fixture
def tenant_config_root(tmp_path: Path):
    with (
        patch("swe.constant.WORKING_DIR", tmp_path),
        patch(
            "swe.config.utils.WORKING_DIR",
            tmp_path,
        ),
    ):
        yield tmp_path


@contextmanager
def _stub_react_agent_tool_exports():
    import swe.agents.tools as agent_tools

    with ExitStack() as stack:
        for export_name in _REACT_AGENT_TOOL_EXPORTS:
            stack.enter_context(
                patch.object(agent_tools, export_name, object(), create=True),
            )
        yield


def test_stdio_launcher_main_applies_limits_before_exec() -> None:
    from swe.app.mcp.stdio_launcher import main

    with (
        patch(
            "swe.app.mcp.stdio_launcher.resource.setrlimit",
        ) as mock_setrlimit,
        patch(
            "swe.app.mcp.stdio_launcher.os.execvpe",
        ) as mock_execvpe,
    ):
        main(
            [
                "--cpu-time-limit-seconds",
                "2",
                "--memory-max-bytes",
                str(64 * 1024 * 1024),
                "--",
                "node",
                "server.js",
            ],
        )

    mock_execvpe.assert_called_once()
    assert mock_execvpe.call_args.args[0] == "node"
    assert mock_execvpe.call_args.args[1] == ["node", "server.js"]
    assert mock_setrlimit.call_count == 2


@pytest.mark.asyncio
async def test_runner_wraps_stdio_client_launch_with_tenant_launcher(
    tenant_config_root: Path,
) -> None:
    with _stub_react_agent_tool_exports():
        from swe.app.runner.runner import _create_mcp_client_with_headers

        _write_process_limit_config(
            tenant_config_root,
            encode_scope_id("tenant-a", "source-a"),
            enabled=True,
            cpu_time_limit_seconds=2,
            memory_max_mb=128,
        )
        _write_scope_env(
            tenant_config_root,
            "tenant-a",
            "source-a",
            {"MCP_TOKEN": "tenant-secret", "TENANT_ONLY": "yes"},
        )

        captured = {}

        class _FakeStdIOClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        client_config = MCPClientConfig(
            name="demo",
            transport="stdio",
            command="node",
            args=["server.js"],
            env={"MCP_TOKEN": "client-secret"},
            cwd="/tmp/demo",
        )

        with patch(
            "swe.app.runner.runner.StdIOStatefulClient",
            _FakeStdIOClient,
        ):
            with tenant_context(tenant_id="tenant-a", source_id="source-a"):
                client = await _create_mcp_client_with_headers(client_config)

    assert captured["command"] == sys.executable
    assert captured["args"][:2] == ["-m", "swe.app.mcp.stdio_launcher"]
    assert captured["args"][-3:] == ["--", "node", "server.js"]
    assert captured["env"]["MCP_TOKEN"] == "client-secret"
    assert captured["env"]["TENANT_ONLY"] == "yes"
    rebuild_info = getattr(client, "_swe_rebuild_info")
    assert rebuild_info["command"] == "node"
    assert rebuild_info["args"] == ["server.js"]
    assert rebuild_info["launch_command"] == sys.executable


@pytest.mark.asyncio
async def test_runner_leaves_stdio_launch_unwrapped_when_policy_disabled(
    tenant_config_root: Path,
) -> None:
    with _stub_react_agent_tool_exports():
        from swe.app.runner.runner import _create_mcp_client_with_headers

        _write_process_limit_config(
            tenant_config_root,
            "tenant-a",
            enabled=False,
        )

        captured = {}

        class _FakeStdIOClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        client_config = MCPClientConfig(
            name="demo",
            transport="stdio",
            command="node",
            args=["server.js"],
            env={"DEMO": "1"},
            cwd="/tmp/demo",
        )

        with patch(
            "swe.app.runner.runner.StdIOStatefulClient",
            _FakeStdIOClient,
        ):
            with tenant_context(tenant_id="tenant-a"):
                await _create_mcp_client_with_headers(client_config)

    assert captured["command"] == "node"
    assert captured["args"] == ["server.js"]


def test_rebuild_mcp_client_reapplies_tenant_launcher(
    tenant_config_root: Path,
) -> None:
    with _stub_react_agent_tool_exports():
        from swe.agents.react_agent import SWEAgent

        _write_process_limit_config(
            tenant_config_root,
            encode_scope_id("tenant-a", "source-a"),
            enabled=True,
            cpu_time_limit_seconds=2,
        )
        _write_scope_env(
            tenant_config_root,
            "tenant-a",
            "source-a",
            {"MCP_TOKEN": "tenant-secret", "TENANT_ONLY": "yes"},
        )

        captured = {}

        class _FakeStdIOClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        original_client = SimpleNamespace(
            _swe_rebuild_info={
                "name": "demo",
                "transport": "stdio",
                "command": "node",
                "args": ["server.js"],
                "env": {"MCP_TOKEN": "client-secret"},
                "cwd": "/tmp/demo",
            },
        )

        with patch(
            "swe.agents.react_agent.StdIOStatefulClient",
            _FakeStdIOClient,
        ):
            with tenant_context(tenant_id="tenant-a", source_id="source-a"):
                rebuilt = SWEAgent._rebuild_mcp_client(original_client)

    assert captured["command"] == sys.executable
    assert captured["args"][:2] == ["-m", "swe.app.mcp.stdio_launcher"]
    assert captured["args"][-3:] == ["--", "node", "server.js"]
    assert captured["env"]["MCP_TOKEN"] == "client-secret"
    assert captured["env"]["TENANT_ONLY"] == "yes"
    assert getattr(rebuilt, "_swe_rebuild_info")["command"] == "node"


def test_stdio_launch_config_merges_tenant_env_with_client_override(
    tenant_config_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swe.app.mcp.stdio_launcher import (
        build_tenant_aware_stdio_launch_config,
    )

    monkeypatch.delenv("MCP_TOKEN", raising=False)
    _write_scope_env(
        tenant_config_root,
        "tenant-a",
        "source-a",
        {"MCP_TOKEN": "tenant-secret", "TENANT_ONLY": "yes"},
    )

    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        launch_config = build_tenant_aware_stdio_launch_config(
            "node",
            ["server.js"],
            {"MCP_TOKEN": "client-secret", "CLIENT_ONLY": "yes"},
        )

    assert launch_config.env["MCP_TOKEN"] == "client-secret"
    assert launch_config.env["TENANT_ONLY"] == "yes"
    assert launch_config.env["CLIENT_ONLY"] == "yes"
    assert "MCP_TOKEN" not in os.environ


def test_stdio_launch_config_filters_protected_client_env(
    tenant_config_root: Path,
) -> None:
    from swe.app.mcp.stdio_launcher import (
        build_tenant_aware_stdio_launch_config,
    )

    _write_scope_env(
        tenant_config_root,
        "tenant-a",
        "source-a",
        {"PATH": "/tenant/bin", "MCP_TOKEN": "tenant-secret"},
    )

    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        launch_config = build_tenant_aware_stdio_launch_config(
            "node",
            env={"PATH": "/client/bin", "PYTHONPATH": "/client/python"},
        )

    assert launch_config.env["MCP_TOKEN"] == "tenant-secret"
    assert launch_config.env.get("PATH") != "/client/bin"
    assert launch_config.env.get("PYTHONPATH") != "/client/python"


def test_stdio_launch_config_injects_runtime_claim_env(
    tenant_config_root: Path,
) -> None:
    from swe.app.mcp.stdio_launcher import (
        build_tenant_aware_stdio_launch_config,
    )
    from swe.runtime_invocation_claims import runtime_invocation_claims_context

    with (
        tenant_context(tenant_id="tenant-a", source_id="source-a"),
        runtime_invocation_claims_context(
            session_id="session-1",
            chat_id="chat-uuid-1",
            trace_id="trace-1",
        ),
    ):
        launch_config = build_tenant_aware_stdio_launch_config(
            "node",
            env={"SWE_TENANT_ID": "fake-tenant"},
        )

    assert launch_config.env["SWE_TENANT_ID"] == "tenant-a"
    assert launch_config.env["SWE_SOURCE_ID"] == "source-a"
    assert launch_config.env["SWE_RUNTIME_SCOPE_ID"] == encode_scope_id(
        "tenant-a",
        "source-a",
    )
    assert launch_config.env["SWE_SESSION_ID"] == "session-1"
    assert launch_config.env["SWE_CHAT_ID"] == "chat-uuid-1"
    assert launch_config.env["SWE_TRACE_ID"] == "trace-1"


def test_limited_stdio_launcher_keeps_pythonpath_for_wrapper_startup(
    tenant_config_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from swe.app.mcp.stdio_launcher import (
        build_tenant_aware_stdio_launch_config,
    )

    _write_process_limit_config(
        tenant_config_root,
        encode_scope_id("tenant-a", "source-a"),
        enabled=True,
        cpu_time_limit_seconds=2,
    )
    monkeypatch.setenv("PYTHONPATH", "/repo/src")

    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        launch_config = build_tenant_aware_stdio_launch_config("node")

    assert launch_config.launch_command == sys.executable
    assert launch_config.args == []
    assert launch_config.env["PYTHONPATH"] == "/repo/src"


def test_stdio_launcher_main_removes_wrapper_only_env_before_exec() -> None:
    from swe.app.mcp.stdio_launcher import main

    wrapper_env = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/repo/src",
        "SWE_STDIO_LAUNCHER_DROP_ENV_KEYS": "PYTHONPATH",
    }

    with (
        patch.dict(os.environ, wrapper_env, clear=True),
        patch("swe.app.mcp.stdio_launcher.os.execvpe") as mock_execvpe,
    ):
        main(["--", "node", "server.js"])

    mock_execvpe.assert_called_once()
    exec_env = mock_execvpe.call_args.args[2]
    assert exec_env["PATH"] == "/usr/bin"
    assert "PYTHONPATH" not in exec_env
    assert "SWE_STDIO_LAUNCHER_DROP_ENV_KEYS" not in exec_env


def test_stdio_launch_config_missing_context_uses_process_env_only(
    tenant_config_root: Path,
) -> None:
    from swe.app.mcp.stdio_launcher import (
        build_tenant_aware_stdio_launch_config,
    )

    save_envs(
        {"MCP_TOKEN": "default-secret"},
        tenant_config_root / "default" / ".secret" / "envs.json",
    )

    launch_config = build_tenant_aware_stdio_launch_config("node")

    assert "MCP_TOKEN" not in launch_config.env
