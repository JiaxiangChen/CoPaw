# -*- coding: utf-8 -*-
"""Tests for tenant-scoped process limit policy resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import call, patch

import pytest
from pydantic import ValidationError

from swe.config.config import Config
from swe.config.context import tenant_context
from swe.config.utils import save_config


def test_process_limit_policy_uses_root_config_for_all_tenants(
    tmp_path: Path,
) -> None:
    from swe.security.process_limits import (
        resolve_current_process_limit_policy,
    )

    save_config(
        Config.model_validate(
            {
                "security": {
                    "process_limits": {
                        "cpu_time_limit_seconds": 11,
                        "memory_max_mb": 512,
                        "mcp_stdio": True,
                        "shell_max_concurrent": 7,
                        "shell_acquire_timeout_seconds": 1.5,
                    },
                },
            },
        ),
        tmp_path / "config.json",
    )
    _write_tenant_config(tmp_path, "tenant-a", enabled=False)
    _write_tenant_config(
        tmp_path,
        "tenant-b",
        enabled=True,
        cpu_time_limit_seconds=2,
    )
    with patch("swe.config.utils.WORKING_DIR", tmp_path):
        for tenant in ("tenant-a", "tenant-b"):
            with tenant_context(tenant_id=tenant):
                for scope in ("shell", "mcp_stdio"):
                    policy = resolve_current_process_limit_policy(scope)
                    assert policy.tenant_id == tenant
                    assert policy.enabled is True
                    assert policy.cpu_time_limit_seconds == 11
                    assert policy.memory_max_mb == 512
                    if scope == "shell":
                        assert policy.shell_max_concurrent == 7
                        assert policy.shell_acquire_timeout_seconds == 1.5


def _write_tenant_config(
    base_dir: Path,
    tenant_id: str,
    *,
    enabled: bool,
    shell: bool = True,
    mcp_stdio: bool = True,
    cpu_time_limit_seconds: int | None = None,
    memory_max_mb: int | None = None,
    shell_max_concurrent: int | None = None,
    shell_acquire_timeout_seconds: float | None = None,
) -> None:
    process_limits = {
        "enabled": enabled,
        "shell": shell,
        "mcp_stdio": mcp_stdio,
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
            {"security": {"process_limits": process_limits}},
        ),
        tenant_dir / "config.json",
    )


def test_process_limits_config_uses_current_default_limits() -> None:
    config = Config.model_validate(
        {
            "security": {
                "process_limits": {
                    "enabled": True,
                    "shell": True,
                    "mcp_stdio": True,
                },
            },
        },
    )

    assert config.security.process_limits.cpu_time_limit_seconds == 90
    assert config.security.process_limits.memory_max_mb == 34000


def test_process_limits_config_rejects_enabled_policy_without_scope() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "security": {
                    "process_limits": {
                        "enabled": True,
                        "shell": False,
                        "mcp_stdio": False,
                        "cpu_time_limit_seconds": 2,
                    },
                },
            },
        )


def test_process_limits_config_defaults_to_disabled() -> None:
    config = Config()

    assert config.security.process_limits.enabled is True
    assert config.security.process_limits.shell is True
    assert config.security.process_limits.mcp_stdio is False
    assert config.security.process_limits.cpu_time_limit_seconds == 90
    assert config.security.process_limits.memory_max_mb == 34000
    assert config.security.process_limits.shell_max_concurrent == 5
    assert config.security.process_limits.shell_acquire_timeout_seconds == 5


def test_process_limits_config_rejects_shell_concurrency_without_shell_scope() -> (
    None
):
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "security": {
                    "process_limits": {
                        "enabled": True,
                        "shell": False,
                        "mcp_stdio": True,
                        "cpu_time_limit_seconds": None,
                        "memory_max_mb": None,
                        "shell_max_concurrent": 5,
                    },
                },
            },
        )


def test_process_limits_config_rejects_zero_shell_acquire_timeout() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "security": {
                    "process_limits": {
                        "enabled": True,
                        "shell": True,
                        "shell_acquire_timeout_seconds": 0,
                    },
                },
            },
        )


def test_resolved_policy_builds_unix_preexec_fn(tmp_path: Path) -> None:
    from swe.security.process_limits import (
        resolve_current_process_limit_policy,
    )

    _write_tenant_config(
        tmp_path,
        ".",
        enabled=True,
        cpu_time_limit_seconds=3,
        memory_max_mb=64,
    )

    with (
        patch("swe.constant.WORKING_DIR", tmp_path),
        patch(
            "swe.config.utils.WORKING_DIR",
            tmp_path,
        ),
        patch("swe.security.process_limits.sys.platform", "linux"),
    ):
        with tenant_context(tenant_id="tenant-a"):
            policy = resolve_current_process_limit_policy("shell")

        with patch(
            "swe.security.process_limits.resource.setrlimit",
        ) as mock_setrlimit:
            preexec_fn = policy.build_preexec_fn()
            assert preexec_fn is not None
            preexec_fn()

    assert mock_setrlimit.call_args_list == [
        call(policy.rlimit_cpu, (3, 3)),
        call(policy.rlimit_as, (64 * 1024 * 1024, 64 * 1024 * 1024)),
    ]


def test_shell_policy_skips_memory_rlimit_on_macos(tmp_path: Path) -> None:
    from swe.security.process_limits import (
        resolve_current_process_limit_policy,
    )

    _write_tenant_config(
        tmp_path,
        ".",
        enabled=True,
        cpu_time_limit_seconds=3,
        memory_max_mb=64,
    )

    with (
        patch("swe.constant.WORKING_DIR", tmp_path),
        patch(
            "swe.config.utils.WORKING_DIR",
            tmp_path,
        ),
        patch("swe.security.process_limits.sys.platform", "darwin"),
    ):
        with tenant_context(tenant_id="tenant-a"):
            policy = resolve_current_process_limit_policy("shell")

        with patch(
            "swe.security.process_limits.resource.setrlimit",
        ) as mock_setrlimit:
            preexec_fn = policy.build_preexec_fn()
            assert preexec_fn is not None
            preexec_fn()

    assert mock_setrlimit.call_args_list == [
        call(policy.rlimit_cpu, (3, 3)),
    ]
    assert "memory" in (policy.diagnostic or "").lower()


def test_resolved_policy_reports_unsupported_platform(tmp_path: Path) -> None:
    from swe.security.process_limits import (
        resolve_current_process_limit_policy,
    )

    _write_tenant_config(
        tmp_path,
        ".",
        enabled=True,
        cpu_time_limit_seconds=2,
        memory_max_mb=32,
    )

    with (
        patch("swe.constant.WORKING_DIR", tmp_path),
        patch(
            "swe.config.utils.WORKING_DIR",
            tmp_path,
        ),
        patch("swe.security.process_limits.sys.platform", "win32"),
    ):
        with tenant_context(tenant_id="tenant-a"):
            policy = resolve_current_process_limit_policy("shell")

    assert policy.enabled is True
    assert policy.should_enforce is False
    assert policy.build_preexec_fn() is None
    assert "not enforced on this platform" in (policy.diagnostic or "")
