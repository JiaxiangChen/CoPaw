# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access,unused-argument,unused-variable
"""Unit tests for shell tenant path boundary enforcement.

Tests cover:
- Allowed tenant-local shell paths
- Denied cross-tenant cwd
- Denied relative traversal
- Denied absolute-path access
"""

from __future__ import annotations

import asyncio
import shlex
import os
import signal
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import call, patch

import pytest

from swe.config.context import encode_scope_id, tenant_context
from swe.config.config import Config
from swe.config.utils import save_config
from swe.envs.store import save_envs
from swe.agents.tool_failure import ToolExecutionError
from swe.agents.tools.shell import (
    execute_shell_command,
    _classify_shell_failure,
    _extract_path_tokens,
    _prepare_subprocess_env,
    prepare_shell_command,
    _scan_python_source_for_outside_path,
    _validate_shell_paths,
    _resolve_cwd,
)
from swe.agents.skill_context_manager import get_skill_context_manager
from swe.security.tenant_path_boundary import (
    TenantPathBoundaryError,
    TenantContextMissingError,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_tenant_dir(tmp_path: Path) -> Path:
    """Create a temporary directory structure mimicking ~/.swe/tenant_id."""
    tenant_dir = tmp_path / "test_tenant"
    tenant_dir.mkdir(parents=True)

    # Create subdirectories
    (tenant_dir / "subdir").mkdir()
    (tenant_dir / "subdir" / "nested").mkdir()

    # Create test files
    (tenant_dir / "file.txt").write_text("test content")
    (tenant_dir / "subdir" / "script.sh").write_text("#!/bin/bash\necho hello")

    return tenant_dir


@pytest.fixture
def other_tenant_dir(tmp_path: Path) -> Path:
    """Create another tenant directory to test isolation."""
    other_dir = tmp_path / "other_tenant"
    other_dir.mkdir(parents=True)
    (other_dir / "secret.txt").write_text("secret content")
    return other_dir


@pytest.fixture
def mock_working_dir(
    temp_tenant_dir: Path,
    other_tenant_dir: Path,
) -> Generator[Path, None, None]:
    """Mock WORKING_DIR to use temporary directory."""
    parent_dir = temp_tenant_dir.parent
    with patch("swe.constant.WORKING_DIR", parent_dir):
        with patch(
            "swe.security.tenant_path_boundary.WORKING_DIR",
            parent_dir,
        ):
            with patch("swe.config.utils.WORKING_DIR", parent_dir):
                yield parent_dir


def _write_process_limit_config(
    base_dir: Path,
    tenant_id: str,
    *,
    enabled: bool,
    shell: bool = True,
    cpu_time_limit_seconds: int | None = None,
    memory_max_mb: int | None = None,
    shell_max_concurrent: int | None = None,
    shell_acquire_timeout_seconds: float | None = None,
) -> None:
    process_limits = {
        "enabled": enabled,
        "shell": shell,
        "mcp_stdio": True,
        "cpu_time_limit_seconds": cpu_time_limit_seconds,
        "memory_max_mb": memory_max_mb,
    }
    if shell_max_concurrent is not None:
        process_limits["shell_max_concurrent"] = shell_max_concurrent
    if shell_acquire_timeout_seconds is not None:
        process_limits["shell_acquire_timeout_seconds"] = (
            shell_acquire_timeout_seconds
        )
    tenant_dir = base_dir / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    save_config(
        Config.model_validate(
            {
                "security": {"process_limits": process_limits},
            },
        ),
        tenant_dir / "config.json",
    )


def _write_scope_env(
    base_dir: Path,
    tenant_id: str,
    source_id: str,
    envs: dict[str, str],
) -> None:
    scope_id = encode_scope_id(tenant_id, source_id)
    save_envs(envs, base_dir / scope_id / ".secret" / "envs.json")


def _assert_tool_error(
    exc_info: pytest.ExceptionInfo[ToolExecutionError],
    *,
    error_type: str,
    detail_contains: str,
) -> None:
    assert exc_info.value.error_type == error_type
    assert detail_contains in exc_info.value.detail


def test_shell_subprocess_env_preserves_backend_storage_roots(
    mock_working_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shell 子进程应继承后端确定的 SWE 存储根路径。"""
    backend_working_dir = mock_working_dir / "backend-working"
    backend_secret_dir = mock_working_dir / "backend-working.secret"
    monkeypatch.setenv("SWE_WORKING_DIR", str(backend_working_dir))
    monkeypatch.setenv("SWE_SECRET_DIR", str(backend_secret_dir))

    _write_scope_env(
        mock_working_dir,
        "test_tenant",
        "source-a",
        {
            "SWE_WORKING_DIR": "/tmp/tenant-overrides-working",
            "SWE_SECRET_DIR": "/tmp/tenant-overrides-secret",
            "PYTHONPATH": "/tmp/tenant-pythonpath",
            "APP_TOKEN": "tenant-secret",
        },
    )

    with tenant_context(tenant_id="test_tenant", source_id="source-a"):
        env = _prepare_subprocess_env()

    assert env["SWE_WORKING_DIR"] == str(backend_working_dir)
    assert env["SWE_SECRET_DIR"] == str(backend_secret_dir)
    assert env["APP_TOKEN"] == "tenant-secret"
    assert "PYTHONPATH" not in env


def test_shell_subprocess_env_defaults_blas_thread_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shell 子进程默认限制常见 BLAS/OpenMP 运行时线程数。"""
    thread_limit_keys = {
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    }
    for key in thread_limit_keys:
        monkeypatch.delenv(key, raising=False)

    env = _prepare_subprocess_env()

    assert {key: env[key] for key in thread_limit_keys} == {
        key: "1" for key in thread_limit_keys
    }


def test_shell_subprocess_env_preserves_explicit_blas_thread_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shell 子进程不覆盖调用方已经指定的线程限制。"""
    explicit_limits = {
        "OPENBLAS_NUM_THREADS": "2",
        "OMP_NUM_THREADS": "3",
        "MKL_NUM_THREADS": "4",
        "NUMEXPR_NUM_THREADS": "5",
        "VECLIB_MAXIMUM_THREADS": "6",
    }
    for key, value in explicit_limits.items():
        monkeypatch.setenv(key, value)

    env = _prepare_subprocess_env()

    assert {key: env[key] for key in explicit_limits} == explicit_limits


# =============================================================================
# Tests for _extract_path_tokens
# =============================================================================


class TestExtractPathTokens:
    """Tests for _extract_path_tokens function."""

    def test_extracts_absolute_paths(self):
        """Should extract absolute paths from commands."""
        cmd = "cat /etc/passwd && ls /var/log"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/passwd" in file_paths
        assert "/var/log" in file_paths
        assert has_code_exec is False

    def test_extracts_relative_paths(self):
        """Should extract relative paths from commands."""
        cmd = "cat ./file.txt && ls ../parent"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "./file.txt" in file_paths
        assert "../parent" in file_paths
        assert has_code_exec is False

    def test_extracts_paths_after_flags(self):
        """Should extract paths following file-related flags."""
        cmd = "cat -f /path/to/file --input ./input.txt -o /output.txt"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/path/to/file" in file_paths
        assert "./input.txt" in file_paths
        assert "/output.txt" in file_paths
        assert has_code_exec is False

    def test_extracts_tilde_paths(self):
        """Should extract paths starting with tilde."""
        cmd = "cat ~/.bashrc && cp ~/file.txt /dest"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "~/.bashrc" in file_paths
        assert "~/file.txt" in file_paths
        assert has_code_exec is False

    def test_no_false_positives(self):
        """Should not extract non-path tokens."""
        cmd = "echo hello world 123"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "hello" not in file_paths
        assert "world" not in file_paths
        assert "123" not in file_paths
        assert has_code_exec is False

    def test_string_arguments_not_treated_as_paths(self):
        """String arguments to echo flags should not be treated as paths."""
        cmd = 'echo -n "/etc/hosts"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        # /etc/hosts is argument to -n flag in echo command (exempt), should not be extracted
        assert "/etc/hosts" not in file_paths
        assert has_code_exec is False

    def test_code_exec_flags_detected(self):
        """Commands with -c/-e flags should be flagged for rejection."""
        cmd = 'bash -c "cat /etc/passwd"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        # Should detect code execution flag
        assert has_code_exec is True
        # Should NOT extract paths from code strings (we reject the whole command)
        assert "/etc/passwd" not in file_paths

    def test_printf_string_not_treated_as_path(self):
        """printf format strings should not be treated as paths."""
        cmd = 'printf -- "/etc/hosts\\n"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/hosts" not in file_paths
        assert has_code_exec is False

    def test_cat_with_double_dash_extracts_path(self):
        """cat -- /etc/hosts should extract /etc/hosts as path."""
        cmd = "cat -- /etc/hosts"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/hosts" in file_paths
        assert has_code_exec is False

    def test_cp_with_double_dash_extracts_paths(self):
        """cp -- /src /dst should extract both paths."""
        cmd = "cp -- /etc/passwd /tmp/copied.txt"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/passwd" in file_paths
        assert "/tmp/copied.txt" in file_paths
        assert has_code_exec is False

    def test_tar_with_absolute_path_extracts_path(self):
        """tar -xf /etc/hosts should extract /etc/hosts as path."""
        cmd = "tar -xf /etc/hosts"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/hosts" in file_paths
        assert has_code_exec is False

    def test_wc_with_dash_c_not_code_exec(self):
        """wc -c file.txt should NOT be treated as code execution."""
        cmd = "wc -c file.txt"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is False

    def test_grep_with_dash_c_not_code_exec(self):
        """grep -c pattern file should NOT be treated as code execution."""
        cmd = "grep -c hello file.txt"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is False

    def test_echo_with_dash_e_not_code_exec(self):
        """echo -e 'a\\nb' should NOT be treated as code execution."""
        cmd = 'echo -e "a\\nb"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is False

    def test_bash_with_combined_lce_flag_detected(self):
        """bash -lc 'cmd' should be detected as code execution."""
        cmd = 'bash -lc "cat /etc/hosts"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is True

    def test_sh_with_combined_ec_flag_detected(self):
        """sh -ec 'cmd' should be detected as code execution."""
        cmd = 'sh -ec "cat /etc/hosts"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is True

    def test_python_with_combined_flag_detected(self):
        """bash -vc 'code' should be detected as code execution."""
        cmd = 'bash -vc "echo hello"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is True

    def test_python3_with_standalone_c_flag_detected(self):
        """bash -c 'code' should be detected as code execution."""
        cmd = 'bash -c "cat /etc/passwd"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is True


# =============================================================================
# Tests for _validate_shell_paths
# =============================================================================


class TestValidateShellPaths:
    """Tests for _validate_shell_paths function."""

    def test_no_paths_returns_none(self, mock_working_dir: Path):
        """Should return None when no paths in command."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "echo hello world",
                base_dir=tenant_dir,
            )
            assert result is None

    def test_validate_shell_paths_uses_workspace_dir_as_base(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_root = tenant_dir / "workspaces"
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace_dir = workspace_root / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        shared_file = workspace_root / "shared.txt"
        shared_file.write_text("shared")

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _validate_shell_paths(
                "cat ../shared.txt",
                base_dir=_resolve_cwd(None),
            )

        assert result is None

    def test_tenant_local_paths_allowed(self, mock_working_dir: Path):
        """Should allow paths within tenant workspace."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths("cat file.txt", base_dir=tenant_dir)
            assert result is None

    def test_direct_active_workspace_skill_write_target_denied(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "skill.json").write_text(
            '{"skills": {"uploaded": {"source": "marketplace:demo"}}}',
            encoding="utf-8",
        )

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            context_manager = get_skill_context_manager()
            context_manager.push_skill("uploaded")
            try:
                result = _validate_shell_paths(
                    "unzip uploaded.zip -d skills/uploaded",
                    base_dir=workspace_dir,
                )
            finally:
                context_manager.clear()

        assert result is not None
        assert "skills/uploaded" in result

    def test_other_workspace_skill_write_target_allowed(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "skill.json").write_text(
            '{"skills": {"uploaded": {"source": "customized"}}}',
            encoding="utf-8",
        )

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            context_manager = get_skill_context_manager()
            context_manager.push_skill("edited-skill")
            try:
                result = _validate_shell_paths(
                    "unzip uploaded.zip -d skills/uploaded",
                    base_dir=workspace_dir,
                )
            finally:
                context_manager.clear()

        assert result is None

    def test_disabled_workspace_skill_write_target_allowed(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "skill.json").write_text(
            '{"skills": {"uploaded": {"source": "customized"}}}',
            encoding="utf-8",
        )

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _validate_shell_paths(
                "unzip uploaded.zip -d .disabled_skills/uploaded",
                base_dir=workspace_dir,
            )

        assert result is None

    def test_workspace_skill_root_removal_denied(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "skill.json").write_text(
            '{"skills": {"uploaded": {"source": "customized"}}}',
            encoding="utf-8",
        )

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _validate_shell_paths(
                "rm -rf skills",
                base_dir=workspace_dir,
            )

        assert result is not None
        assert "skills" in result

    def test_new_workspace_skill_creation_allowed(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            context_manager = get_skill_context_manager()
            context_manager.push_skill("new-skill")
            try:
                result = _validate_shell_paths(
                    "mkdir -p skills/new-skill",
                    base_dir=workspace_dir,
                )
            finally:
                context_manager.clear()

        assert result is None

    def test_created_workspace_skill_file_removal_allowed(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        skill_dir = workspace_dir / "skills" / "uploaded"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("x", encoding="utf-8")
        (workspace_dir / "skill.json").write_text(
            '{"skills": {"uploaded": {"source": "customized"}}}',
            encoding="utf-8",
        )

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _validate_shell_paths(
                "rm -f skills/uploaded/SKILL.md",
                base_dir=workspace_dir,
            )

        assert result is None

    def test_created_workspace_skill_root_removal_allowed(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        skill_dir = workspace_dir / "skills" / "uploaded"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("x", encoding="utf-8")
        (workspace_dir / "skill.json").write_text(
            '{"skills": {"uploaded": {"source": "customized"}}}',
            encoding="utf-8",
        )

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _validate_shell_paths(
                "rm -rf skills/uploaded",
                base_dir=workspace_dir,
            )

        assert result is None

    def test_new_workspace_skill_root_removal_allowed(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            context_manager = get_skill_context_manager()
            context_manager.push_skill("new-skill")
            try:
                result = _validate_shell_paths(
                    "rm -rf skills/new-skill",
                    base_dir=workspace_dir,
                )
            finally:
                context_manager.clear()

        assert result is None

    def test_existing_workspace_skill_root_without_manifest_denied(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        skill_dir = workspace_dir / "skills" / "received"
        skill_dir.mkdir(parents=True, exist_ok=True)

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _validate_shell_paths(
                "rm -rf skills/received",
                base_dir=workspace_dir,
            )

        assert result is not None
        assert "skills/received" in result

    def test_workspace_skill_root_chmod_denied(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        (workspace_dir / "skill.json").write_text(
            '{"skills": {"uploaded": {"source": "customized"}}}',
            encoding="utf-8",
        )

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _validate_shell_paths(
                "chmod 700 skills",
                base_dir=workspace_dir,
            )

        assert result is not None
        assert "skills" in result

    def test_non_created_workspace_skill_move_source_denied(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        skill_dir = workspace_dir / "skills" / "received"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("x", encoding="utf-8")
        (workspace_dir / "skill.json").write_text(
            '{"skills": {"received": {"source": "marketplace:demo"}}}',
            encoding="utf-8",
        )

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _validate_shell_paths(
                "mv skills/received/SKILL.md notes.txt",
                base_dir=workspace_dir,
            )

        assert result is not None
        assert "skills/received/SKILL.md" in result

    def test_unsafe_active_skill_name_does_not_escape_skill_root(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            context_manager = get_skill_context_manager()
            context_manager.push_skill("../outside")
            try:
                result = _validate_shell_paths(
                    "touch skills/other/SKILL.md",
                    base_dir=workspace_dir,
                )
            finally:
                context_manager.clear()

        assert result is not None
        assert "skills/other/SKILL.md" in result

    def test_python_script_content_outside_path_denied(
        self,
        mock_working_dir: Path,
    ):
        """Should reject tenant-local Python scripts that read outside paths."""
        tenant_dir = mock_working_dir / "test_tenant"
        script = tenant_dir / "script.py"
        script.write_text('open("/etc/passwd").read()\n')

        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "python script.py",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "Python script contains path outside" in result
            assert "/etc/passwd" in result

    def test_python_script_pathlib_outside_path_denied(
        self,
        mock_working_dir: Path,
    ):
        """Should reject pathlib literals that point outside the tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        script = tenant_dir / "script.py"
        script.write_text(
            "from pathlib import Path\n" 'Path("/etc/passwd").read_text()\n',
        )

        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "python script.py",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "Python script contains path outside" in result
            assert "/etc/passwd" in result

    def test_python_code_string_outside_path_denied(
        self,
        mock_working_dir: Path,
    ):
        """Should reject static outside paths in python -c code."""
        tenant_dir = mock_working_dir / "test_tenant"

        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                'python -c "open(\\"/etc/passwd\\").read()"',
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "Python code contains path outside" in result
            assert "/etc/passwd" in result

    def test_python_code_string_tenant_absolute_opt_path_allowed(
        self,
        mock_working_dir: Path,
    ):
        """Tenant-local absolute paths under /opt should not be rejected as system paths."""
        tenant_dir = mock_working_dir / "test_tenant"
        code = (
            "path = "
            "'/opt/deployments/app/working/test_tenant/workspaces/default/"
            "tool_result/result.txt'\n"
            "print(open(path).read())"
        )

        with patch(
            "swe.agents.tools.shell.is_path_within_tenant_with_base",
            return_value=True,
        ):
            result = _scan_python_source_for_outside_path(code, tenant_dir)

        assert result is None

    def test_python_directory_content_outside_path_denied(
        self,
        mock_working_dir: Path,
    ):
        """Should scan Python directory execution targets."""
        tenant_dir = mock_working_dir / "test_tenant"
        package_dir = tenant_dir / "package"
        package_dir.mkdir()
        (package_dir / "__main__.py").write_text(
            'open("/etc/passwd").read()\n',
        )

        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "python package",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "Python script contains path outside" in result
            assert "/etc/passwd" in result

    def test_python_script_tenant_local_path_allowed(
        self,
        mock_working_dir: Path,
    ):
        """Should allow Python scripts that use tenant-local paths."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "allowed.txt").write_text("ok")
        script = tenant_dir / "script.py"
        script.write_text('open("allowed.txt").read()\n')

        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "python script.py",
                base_dir=tenant_dir,
            )
            assert result is None

    def test_python_script_system_path_string_denied(
        self,
        mock_working_dir: Path,
    ):
        """ctypes/syscall scripts should not hide system path strings."""
        tenant_dir = mock_working_dir / "test_tenant"
        script = tenant_dir / "copy_opt.py"
        script.write_text(
            "import ctypes\n"
            "ctypes.CDLL(None)\n"
            "source = '/opt/python/bin/jp.py'\n"
            "print(source)\n",
        )

        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "python copy_opt.py",
                base_dir=tenant_dir,
            )

        assert result is not None
        assert "system path string" in result
        assert "/opt/python/bin/jp.py" in result

    def test_python_script_symlink_outside_tenant_denied(
        self,
        mock_working_dir: Path,
    ):
        """Should reject Python script paths resolving outside the tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        outside_script = mock_working_dir / "other_tenant" / "script.py"
        outside_script.write_text('print("outside")\n')
        (tenant_dir / "evil.py").symlink_to(outside_script)

        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "python evil.py",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "Python script path outside" in result

    def test_python_directory_symlinked_source_outside_tenant_denied(
        self,
        mock_working_dir: Path,
    ):
        """Should reject scanned Python files resolving outside the tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        package_dir = tenant_dir / "package"
        package_dir.mkdir()
        outside_script = mock_working_dir / "other_tenant" / "module.py"
        outside_script.write_text('print("outside")\n')
        (package_dir / "__main__.py").write_text("import module\n")
        (package_dir / "module.py").symlink_to(outside_script)

        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "python package",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "Python script path outside" in result

    def test_absolute_path_outside_tenant_denied(self, mock_working_dir: Path):
        """Should reject absolute paths outside tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            other_path = mock_working_dir / "other_tenant/secret.txt"
            result = _validate_shell_paths(
                f"cat {other_path}",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_dev_null_output_sink_allowed(self, mock_working_dir: Path):
        """Common output probes may write to /dev/null without escaping tenant data."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1',
                base_dir=tenant_dir,
            )

        assert result is None

    def test_relative_traversal_denied(self, mock_working_dir: Path):
        """Should reject relative paths that traverse outside tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "cat ../other_tenant/secret.txt",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_tilde_path_denied(self, mock_working_dir: Path):
        """Should reject paths starting with tilde (expands to home)."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "cat ~/.bashrc",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_home_env_path_denied(self, mock_working_dir: Path):
        """Shell path variables should not bypass tenant path checks."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "cat $HOME/.ssh/id_rsa",
                base_dir=tenant_dir,
            )

        assert result is not None
        assert "environment path variable" in result
        assert "$HOME" in result

    def test_braced_home_env_path_denied(self, mock_working_dir: Path):
        """Braced shell path variables should be rejected too."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "ls ${HOME}/.config",
                base_dir=tenant_dir,
            )

        assert result is not None
        assert "environment path variable" in result
        assert "${HOME}" in result

    def test_relative_path_against_cwd_within_tenant_allowed(
        self,
        mock_working_dir: Path,
    ):
        """Should allow relative paths that resolve within tenant when using cwd."""
        tenant_dir = mock_working_dir / "test_tenant"
        subdir = tenant_dir / "subdir" / "nested"
        subdir.mkdir(parents=True, exist_ok=True)

        with tenant_context(tenant_id="test_tenant"):
            # ../ from subdir/nested should resolve to subdir, which is within tenant
            result = _validate_shell_paths("cat ../file.txt", base_dir=subdir)
            assert result is None

    def test_code_exec_flag_rejected(self, mock_working_dir: Path):
        """Should reject commands with -c/-e code execution flags."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                'bash -c "cat /etc/passwd"',
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "code execution flags" in result

    def test_double_dash_paths_validated(self, mock_working_dir: Path):
        """cat -- /etc/hosts should reject /etc/hosts as outside tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "cat -- /etc/hosts",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_tar_with_absolute_path_rejected(self, mock_working_dir: Path):
        """tar -xf /etc/hosts should reject /etc/hosts as outside tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "tar -xf /etc/hosts",
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_wc_with_dash_c_allowed(self, mock_working_dir: Path):
        """wc -c file.txt should be allowed (not code execution)."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "test.txt").write_text("hello")
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "wc -c test.txt",
                base_dir=tenant_dir,
            )
            assert result is None

    def test_grep_with_dash_c_allowed(self, mock_working_dir: Path):
        """grep -c pattern file should be allowed (not code execution)."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "test.txt").write_text("hello")
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "grep -c hello test.txt",
                base_dir=tenant_dir,
            )
            assert result is None

    def test_bash_with_combined_flag_rejected(self, mock_working_dir: Path):
        """bash -lc 'cmd' should be rejected as code execution."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                'bash -lc "cat /etc/hosts"',
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "code execution flags" in result

    def test_sh_with_combined_ec_flag_rejected(self, mock_working_dir: Path):
        """sh -ec 'cmd' should be rejected as code execution."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                'sh -ec "cat /etc/hosts"',
                base_dir=tenant_dir,
            )
            assert result is not None
            assert "code execution flags" in result


# =============================================================================
# Tests for _resolve_cwd
# =============================================================================


class TestResolveCwd:
    """Tests for _resolve_cwd function."""

    def test_returns_tenant_root_when_cwd_none(self, mock_working_dir: Path):
        """Should return tenant root when cwd is None."""
        with tenant_context(tenant_id="test_tenant"):
            result = _resolve_cwd(None)
            expected = mock_working_dir / "test_tenant"
            assert result == expected

    def test_resolve_cwd_defaults_to_workspace_dir_when_present(
        self,
        mock_working_dir: Path,
    ):
        tenant_dir = mock_working_dir / "test_tenant"
        workspace_dir = tenant_dir / "workspaces" / "agent_a"
        workspace_dir.mkdir(parents=True)

        with tenant_context(
            tenant_id="test_tenant",
            workspace_dir=workspace_dir,
        ):
            result = _resolve_cwd(None)

        assert result == workspace_dir.resolve()

    def test_resolve_cwd_allows_source_scoped_workspace_for_default_tenant(
        self,
        mock_working_dir: Path,
    ):
        """default + source should accept cwd under default_{source}."""
        workspace_dir = (
            mock_working_dir / "default_RMASSIST" / "workspaces" / "default"
        )
        workspace_dir.mkdir(parents=True)

        with tenant_context(
            tenant_id="default",
            source_id="RMASSIST",
        ):
            result = _resolve_cwd(workspace_dir)

        assert result == workspace_dir.resolve()

    def test_returns_resolved_cwd_when_within_tenant(
        self,
        mock_working_dir: Path,
    ):
        """Should return resolved cwd when within tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        subdir = tenant_dir / "subdir"

        with tenant_context(tenant_id="test_tenant"):
            result = _resolve_cwd(subdir)
            assert result == subdir.resolve()

    def test_raises_when_cwd_outside_tenant(self, mock_working_dir: Path):
        """Should raise TenantPathBoundaryError when cwd outside tenant."""
        other_dir = mock_working_dir / "other_tenant"

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(TenantPathBoundaryError) as exc_info:
                _resolve_cwd(other_dir)

            assert "outside the tenant workspace" in str(exc_info.value)

    def test_raises_when_tenant_context_missing(self):
        """Should raise TenantContextMissingError when no tenant context."""
        with pytest.raises(TenantContextMissingError):
            _resolve_cwd(None)

    def test_rejects_traversal_cwd(self, mock_working_dir: Path):
        """Should reject cwd with path traversal."""
        tenant_dir = mock_working_dir / "test_tenant"
        traversal_path = tenant_dir / "../other_tenant"

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(TenantPathBoundaryError):
                _resolve_cwd(traversal_path)


# =============================================================================
# Integration tests for execute_shell_command
# =============================================================================


class TestExecuteShellCommand:
    """Integration tests for execute_shell_command with tenant boundary."""

    def test_prepare_shell_command_reuses_shell_boundary_context(
        self,
        mock_working_dir: Path,
    ):
        """共享 Shell 准备逻辑应统一解析 cwd 和租户环境。"""
        from swe.agents.tools.shell import prepare_shell_command

        tenant_dir = mock_working_dir / "test_tenant"

        with tenant_context(
            tenant_id="test_tenant",
            user_id="user_a",
            workspace_dir=tenant_dir,
        ):
            prepared = prepare_shell_command(
                "echo ok",
                cwd=str(tenant_dir),
            )

        assert prepared.command == "echo ok"
        assert prepared.working_dir == tenant_dir.resolve()
        assert "PATH" in prepared.env
        assert prepared.python_runtime_guard is not None

    @pytest.mark.asyncio
    async def test_shell_execution_preparation_injects_opencli_credentials(
        self,
        mock_working_dir: Path,
    ):
        """Shared shell preparation should apply OpenCLI auth interception."""
        from swe.agents.tools.shell import _prepared_shell_execution

        tenant_dir = mock_working_dir / "test_tenant"

        with (
            patch(
                "swe.agents.tools.shell_interceptor."
                "resolve_auth_token_for_execution",
            ) as resolve_token,
            tenant_context(
                tenant_id="test_tenant",
                user_id="user_a",
                workspace_dir=tenant_dir,
            ),
        ):
            resolve_token.return_value.token = "resolved-authorization"
            resolve_token.return_value.cookie_header = "resolved-cookie"
            async with _prepared_shell_execution(
                "opencli apps list",
                cwd=str(tenant_dir),
            ) as (prepared, _policy):
                command = prepared.command

        assert shlex.split(command) == [
            "opencli",
            "apps",
            "list",
            "--authorization",
            "Bearer resolved-authorization",
            "--cookie",
            "resolved-cookie",
        ]
        resolve_token.assert_awaited_once_with(
            tenant_id="test_tenant",
            workspace_dir=tenant_dir,
        )

    def test_prepare_shell_command_preserves_unix_multiline_python(
        self,
        mock_working_dir: Path,
    ):
        """Unix shell commands must keep newlines for python -c and heredocs."""
        tenant_dir = mock_working_dir / "test_tenant"
        command = (
            'python3 -c "\n'
            "import json\n"
            "print(json.dumps({'ok': True}))\n"
            '"'
        )

        with (
            patch("swe.agents.tools.shell.sys.platform", "linux"),
            tenant_context(tenant_id="test_tenant", workspace_dir=tenant_dir),
        ):
            prepared = prepare_shell_command(command, cwd=str(tenant_dir))

        assert "\nimport json\n" in prepared.command
        assert "import json print" not in prepared.command

    def test_prepare_shell_command_preserves_unix_heredoc(
        self,
        mock_working_dir: Path,
    ):
        """Here-doc bodies are executable shell syntax and must not be flattened."""
        tenant_dir = mock_working_dir / "test_tenant"
        command = "python3 << 'PYEOF'\nprint('ok')\nPYEOF"

        with (
            patch("swe.agents.tools.shell.sys.platform", "linux"),
            tenant_context(tenant_id="test_tenant", workspace_dir=tenant_dir),
        ):
            prepared = prepare_shell_command(command, cwd=str(tenant_dir))

        assert prepared.command == command

    def test_prepare_shell_command_collapses_windows_newlines(
        self,
        mock_working_dir: Path,
    ):
        """Windows cmd still receives single-line commands to avoid truncation."""
        tenant_dir = mock_working_dir / "test_tenant"

        with (
            patch("swe.agents.tools.shell.sys.platform", "win32"),
            tenant_context(tenant_id="test_tenant", workspace_dir=tenant_dir),
        ):
            prepared = prepare_shell_command(
                "echo hello\nworld",
                cwd=str(tenant_dir),
            )

        assert prepared.command == "echo hello world"

    @pytest.mark.asyncio
    async def test_accepts_string_cwd_within_tenant(
        self,
        mock_working_dir: Path,
    ):
        """String cwd values from the tool layer should still execute."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "test.txt").write_text("hello from string cwd")

        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command(
                "cat test.txt",
                cwd=str(tenant_dir),
            )

            assert result.content[0]["text"] == "hello from string cwd"

    @pytest.mark.asyncio
    async def test_executes_within_tenant(self, mock_working_dir: Path):
        """Should execute command within tenant workspace."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "test.txt").write_text("hello world")

        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command("cat test.txt")

            assert result.content[0]["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_rejects_cross_tenant_cwd(self, mock_working_dir: Path):
        """Should reject command with cwd outside tenant."""
        from swe.agents.tools.shell import execute_shell_command

        other_dir = mock_working_dir / "other_tenant"

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(ToolExecutionError) as exc_info:
                await execute_shell_command("ls", cwd=other_dir)

        _assert_tool_error(
            exc_info,
            error_type="permission_denied",
            detail_contains="outside the tenant workspace",
        )

    @pytest.mark.asyncio
    async def test_rejects_cross_tenant_path_in_command(
        self,
        mock_working_dir: Path,
    ):
        """Should reject command referencing paths outside tenant."""
        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            other_path = mock_working_dir / "other_tenant/secret.txt"
            with pytest.raises(ToolExecutionError) as exc_info:
                await execute_shell_command(f"cat {other_path}")

        _assert_tool_error(
            exc_info,
            error_type="permission_denied",
            detail_contains="outside the allowed workspace",
        )

    @pytest.mark.asyncio
    async def test_allows_valid_relative_paths(self, mock_working_dir: Path):
        """Should allow commands with valid relative paths."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "subdir" / "nested_file.txt").write_text(
            "nested content",
        )

        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command("cat subdir/nested_file.txt")

            assert "nested content" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_rejects_traversal_in_command(self, mock_working_dir: Path):
        """Should reject commands with path traversal."""
        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(ToolExecutionError) as exc_info:
                await execute_shell_command(
                    "cat ../other_tenant/secret.txt",
                )

        _assert_tool_error(
            exc_info,
            error_type="permission_denied",
            detail_contains="outside the allowed workspace",
        )

    @pytest.mark.asyncio
    async def test_rejects_code_exec_in_command(self, mock_working_dir: Path):
        """Should reject commands with code execution flags."""
        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(ToolExecutionError) as exc_info:
                await execute_shell_command('bash -c "echo hello"')

        _assert_tool_error(
            exc_info,
            error_type="invalid_arguments",
            detail_contains="code execution flags",
        )

    @pytest.mark.asyncio
    async def test_python_runtime_guard_rejects_dynamic_open_outside_tenant(
        self,
        mock_working_dir: Path,
    ):
        """Runtime guard should catch dynamic paths static scanning misses."""
        from swe.agents.tools.shell import execute_shell_command

        code = (
            "import os; "
            "path = os.path.join('..', 'other_tenant', 'secret.txt'); "
            "print(open(path).read())"
        )

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(ToolExecutionError) as exc_info:
                await execute_shell_command(
                    f"python -c {shlex.quote(code)}",
                )

        _assert_tool_error(
            exc_info,
            error_type="permission_denied",
            detail_contains="outside the allowed workspace",
        )

    @pytest.mark.asyncio
    async def test_python_runtime_guard_allows_dynamic_open_inside_tenant(
        self,
        mock_working_dir: Path,
    ):
        """Runtime guard should allow dynamic paths that stay in the tenant."""
        from swe.agents.tools.shell import execute_shell_command

        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "dynamic.txt").write_text("dynamic ok")
        code = (
            "import os; "
            "path = os.path.join('subdir', '..', 'dynamic.txt'); "
            "print(open(path).read())"
        )

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command(
                f"python -c {shlex.quote(code)}",
            )

        assert result.content[0]["text"] == "dynamic ok"

    @pytest.mark.asyncio
    async def test_python_runtime_guard_rejects_subprocess_path_escape(
        self,
        mock_working_dir: Path,
    ):
        """Runtime guard should block Python subprocess calls with outside paths."""
        from swe.agents.tools.shell import execute_shell_command

        code = (
            "import subprocess; "
            "subprocess.run(['cat', '../other_tenant/secret.txt'], check=True)"
        )

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(ToolExecutionError) as exc_info:
                await execute_shell_command(
                    f"python -c {shlex.quote(code)}",
                )

        _assert_tool_error(
            exc_info,
            error_type="permission_denied",
            detail_contains="outside the allowed workspace",
        )

    @pytest.mark.asyncio
    async def test_disabled_process_limit_policy_does_not_inject_preexec(
        self,
        mock_working_dir: Path,
    ):
        """Disabled tenant process limits preserve current shell launch behavior."""
        from swe.agents.tools.shell import execute_shell_command

        _write_process_limit_config(
            mock_working_dir,
            "test_tenant",
            enabled=False,
        )

        captured = {}

        class _FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"ok\n", b""

        async def _fake_create_subprocess_shell(*args, **kwargs):
            captured["preexec_fn"] = kwargs.get("preexec_fn")
            return _FakeProcess()

        with patch(
            "swe.agents.tools.shell.asyncio.create_subprocess_shell",
            side_effect=_fake_create_subprocess_shell,
        ):
            with tenant_context(tenant_id="test_tenant"):
                result = await execute_shell_command("echo ok")

        assert result.content[0]["text"] == "ok"
        assert captured["preexec_fn"] is None

    @pytest.mark.asyncio
    async def test_shell_process_limit_lookup_is_tenant_scoped(
        self,
        mock_working_dir: Path,
    ):
        """Shell launch injects the current tenant's process-limit preexec_fn."""
        from swe.agents.tools.shell import execute_shell_command

        _write_process_limit_config(
            mock_working_dir,
            "tenant-a",
            enabled=True,
            shell=True,
            cpu_time_limit_seconds=2,
        )
        _write_process_limit_config(
            mock_working_dir,
            "tenant-b",
            enabled=True,
            shell=False,
            cpu_time_limit_seconds=2,
        )

        captured = {}

        class _FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"tenant\n", b""

        async def _fake_create_subprocess_shell(*args, **kwargs):
            captured["preexec_fn"] = kwargs.get("preexec_fn")
            return _FakeProcess()

        with patch(
            "swe.agents.tools.shell.asyncio.create_subprocess_shell",
            side_effect=_fake_create_subprocess_shell,
        ):
            with tenant_context(tenant_id="tenant-a"):
                await execute_shell_command("echo tenant")
            assert captured["preexec_fn"] is not None

            with tenant_context(tenant_id="tenant-b"):
                await execute_shell_command("echo tenant")

        assert captured["preexec_fn"] is None

    @pytest.mark.asyncio
    async def test_shell_macos_policy_skips_memory_rlimit_in_preexec(
        self,
        mock_working_dir: Path,
    ):
        """macOS shell launch still injects a CPU-only preexec_fn."""
        from swe.agents.tools.shell import execute_shell_command

        _write_process_limit_config(
            mock_working_dir,
            "test_tenant",
            enabled=True,
            shell=True,
            cpu_time_limit_seconds=2,
            memory_max_mb=64,
        )

        captured = {}

        class _FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"ok\n", b""

        async def _fake_create_subprocess_shell(*args, **kwargs):
            captured["preexec_fn"] = kwargs.get("preexec_fn")
            return _FakeProcess()

        with (
            patch("swe.agents.tools.shell.sys.platform", "darwin"),
            patch(
                "swe.security.process_limits.sys.platform",
                "darwin",
            ),
            patch(
                "swe.agents.tools.shell.asyncio.create_subprocess_shell",
                side_effect=_fake_create_subprocess_shell,
            ),
        ):
            with tenant_context(tenant_id="test_tenant"):
                result = await execute_shell_command("echo ok")

        assert result.content[0]["text"].startswith("ok")
        assert captured["preexec_fn"] is not None

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Unix-only rlimit test",
    )
    async def test_reports_cpu_limit_exceeded_for_shell_command(
        self,
        mock_working_dir: Path,
    ):
        """CPU rlimit termination is reported as process-limit failure."""
        from swe.agents.tools.shell import execute_shell_command

        _write_process_limit_config(
            mock_working_dir,
            "test_tenant",
            enabled=True,
            shell=True,
            cpu_time_limit_seconds=1,
        )

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(ToolExecutionError) as exc_info:
                await execute_shell_command(
                    'python -c "while True: pass"',
                    timeout=10,
                )

        _assert_tool_error(
            exc_info,
            error_type="process_limit_exceeded",
            detail_contains="exit code",
        )

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Unix-only rlimit test",
    )
    async def test_reports_memory_limit_exceeded_for_shell_command(
        self,
        mock_working_dir: Path,
    ):
        """Memory-limit style failures are classified separately."""
        from swe.agents.tools.shell import execute_shell_command

        _write_process_limit_config(
            mock_working_dir,
            "test_tenant",
            enabled=True,
            shell=True,
            memory_max_mb=64,
        )

        class _FakeProcess:
            returncode = 1

            async def communicate(self):
                return b"", b"MemoryError"

        async def _fake_create_subprocess_shell(*args, **kwargs):
            return _FakeProcess()

        with (
            patch(
                "swe.agents.tools.shell.asyncio.create_subprocess_shell",
                side_effect=_fake_create_subprocess_shell,
            ),
            patch("swe.security.process_limits.sys.platform", "linux"),
        ):
            with tenant_context(tenant_id="test_tenant"):
                with pytest.raises(ToolExecutionError) as exc_info:
                    await execute_shell_command("echo hello", timeout=10)

        _assert_tool_error(
            exc_info,
            error_type="process_limit_exceeded",
            detail_contains="MemoryError",
        )

    @pytest.mark.asyncio
    async def test_shell_unsupported_platform_returns_diagnostic(
        self,
        mock_working_dir: Path,
    ):
        """Unsupported platform diagnostics are visible in shell output."""
        from swe.agents.tools.shell import execute_shell_command

        _write_process_limit_config(
            mock_working_dir,
            "test_tenant",
            enabled=True,
            shell=True,
            cpu_time_limit_seconds=2,
        )

        with patch(
            "swe.security.process_limits._supports_unix_rlimits",
            return_value=False,
        ):
            with tenant_context(tenant_id="test_tenant"):
                result = await execute_shell_command("echo hello")

        assert "hello" in result.content[0]["text"]
        assert "not enforced on this platform" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_shell_concurrency_limit_fails_when_slot_is_unavailable(
        self,
        mock_working_dir: Path,
    ):
        """A tenant cannot exceed its configured shell execution slots."""
        from swe.agents.tools.shell import execute_shell_command

        _write_process_limit_config(
            mock_working_dir,
            "test_tenant",
            enabled=True,
            shell=True,
            shell_max_concurrent=1,
            shell_acquire_timeout_seconds=0.01,
        )

        async def _blocking_execute_platform_subprocess(*args, **kwargs):
            await asyncio.sleep(0.2)
            return 0, "held", ""

        with patch(
            "swe.agents.tools.shell._execute_platform_subprocess",
            side_effect=_blocking_execute_platform_subprocess,
        ):
            with tenant_context(tenant_id="test_tenant"):
                first = asyncio.create_task(execute_shell_command("echo one"))
                await asyncio.sleep(0.02)
                with pytest.raises(ToolExecutionError) as exc_info:
                    await execute_shell_command("echo two")
                await first

        _assert_tool_error(
            exc_info,
            error_type="shell_concurrency_limit_exceeded",
            detail_contains="shell execution slots",
        )

    @pytest.mark.asyncio
    async def test_shell_command_receives_source_scoped_tenant_env(
        self,
        mock_working_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """shell 子进程应接收当前 source scope 的持久化 env。"""
        monkeypatch.delenv("API_TOKEN", raising=False)
        _write_scope_env(
            mock_working_dir,
            "test_tenant",
            "source-a",
            {"API_TOKEN": "tenant-secret"},
        )

        with tenant_context(tenant_id="test_tenant", source_id="source-a"):
            result = await execute_shell_command(
                "python -c \"import os; print(os.environ.get('API_TOKEN', ''))\"",
            )

        assert "tenant-secret" in result.content[0]["text"]
        assert "API_TOKEN" not in os.environ

    @pytest.mark.asyncio
    async def test_shell_command_receives_runtime_claim_env(
        self,
        mock_working_dir: Path,
    ):
        """shell 子进程应接收运行时调用 claims env。"""
        from swe.runtime_invocation_claims import (
            runtime_invocation_claims_context,
        )

        command = (
            'python -c "import json, os; '
            "print(json.dumps({"
            "'tenant': os.environ.get('SWE_TENANT_ID'), "
            "'source': os.environ.get('SWE_SOURCE_ID'), "
            "'scope': os.environ.get('SWE_RUNTIME_SCOPE_ID'), "
            "'session': os.environ.get('SWE_SESSION_ID'), "
            "'chat': os.environ.get('SWE_CHAT_ID'), "
            "'trace': os.environ.get('SWE_TRACE_ID')}))\""
        )
        (mock_working_dir / encode_scope_id("test_tenant", "source-a")).mkdir(
            parents=True,
            exist_ok=True,
        )

        with (
            tenant_context(tenant_id="test_tenant", source_id="source-a"),
            runtime_invocation_claims_context(
                session_id="session-1",
                chat_id="chat-uuid-1",
                trace_id="trace-1",
            ),
        ):
            result = await execute_shell_command(command)

        text = result.content[0]["text"]
        assert '"tenant": "test_tenant"' in text
        assert '"source": "source-a"' in text
        assert (
            '"scope": "' + encode_scope_id("test_tenant", "source-a") in text
        )
        assert '"session": "session-1"' in text
        assert '"chat": "chat-uuid-1"' in text
        assert '"trace": "trace-1"' in text

    @pytest.mark.asyncio
    async def test_shell_rejects_boundary_escape_before_runtime_env_build(
        self,
        mock_working_dir: Path,
    ):
        """路径边界拒绝时，不应提前加载或注入 tenant env。"""
        with patch(
            "swe.agents.tools.shell.build_runtime_env",
            side_effect=AssertionError("runtime env should not be built"),
        ):
            with tenant_context(tenant_id="test_tenant", source_id="source-a"):
                with pytest.raises(ToolExecutionError) as exc_info:
                    await execute_shell_command("cat /etc/passwd")

        _assert_tool_error(
            exc_info,
            error_type="permission_denied",
            detail_contains="outside the allowed workspace",
        )


def test_shell_sigkill_failure_is_not_process_limit_without_enforcement():
    assert (
        _classify_shell_failure(
            -signal.SIGKILL,
            "",
            process_limits_enforced=False,
        )
        == "shell_command_failed"
    )


def test_shell_sigkill_failure_is_process_limit_when_enforced():
    assert (
        _classify_shell_failure(
            -signal.SIGKILL,
            "",
            process_limits_enforced=True,
            memory_limit_enforced=False,
        )
        == "process_limit_exceeded"
    )


def test_shell_memory_error_is_not_process_limit_without_memory_enforcement():
    assert (
        _classify_shell_failure(
            1,
            "MemoryError",
            process_limits_enforced=True,
            memory_limit_enforced=False,
        )
        == "shell_command_failed"
    )


def test_shell_memory_error_is_process_limit_with_memory_enforcement():
    assert (
        _classify_shell_failure(
            1,
            "MemoryError",
            process_limits_enforced=True,
            memory_limit_enforced=True,
        )
        == "process_limit_exceeded"
    )


def test_shell_curl_exit_28_is_tool_timeout():
    assert _classify_shell_failure(28, "") == "tool_timeout"


def test_shell_network_timeout_traceback_is_tool_timeout():
    stderr = (
        "Traceback (most recent call last):\n"
        "urllib3.exceptions.ConnectTimeoutError: Connection timed out"
    )

    assert _classify_shell_failure(1, stderr) == "tool_timeout"
