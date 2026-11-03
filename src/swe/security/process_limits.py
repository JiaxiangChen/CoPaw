# -*- coding: utf-8 -*-
"""Process-limit helpers for tenant-scoped subprocesses."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable, Literal
import sys

try:
    import resource
except ImportError:  # pragma: no cover - non-Unix import guard
    resource = None  # type: ignore[assignment]

from swe.config.context import get_current_effective_tenant_id
from swe.config.utils import load_config

logger = logging.getLogger(__name__)

ProcessLimitScope = Literal["shell", "mcp_stdio"]


def _supports_unix_rlimits() -> bool:
    """Return True when the current platform supports Unix rlimits."""
    return (
        sys.platform != "win32"
        and resource is not None
        and hasattr(resource, "setrlimit")
        and hasattr(resource, "RLIMIT_CPU")
    )


def _supports_memory_rlimit(scope: ProcessLimitScope) -> bool:
    """Return True when ``RLIMIT_AS`` is usable for the given scope."""
    return (
        resource is not None
        and hasattr(resource, "RLIMIT_AS")
        and not (scope == "shell" and sys.platform == "darwin")
    )


@dataclass(frozen=True)
class CurrentProcessLimitPolicy:
    """Resolved process-limit policy for the current tenant and scope."""

    tenant_id: str | None
    scope: ProcessLimitScope
    enabled: bool
    cpu_time_limit_seconds: int | None
    memory_max_mb: int | None
    should_enforce: bool
    should_enforce_memory_limit: bool = True
    shell_max_concurrent: int | None = None
    shell_acquire_timeout_seconds: float = 5
    diagnostic: str | None = None

    @property
    def memory_max_bytes(self) -> int | None:
        if self.memory_max_mb is None:
            return None
        return self.memory_max_mb * 1024 * 1024

    @property
    def rlimit_cpu(self) -> int | None:
        return getattr(resource, "RLIMIT_CPU", None)

    @property
    def rlimit_as(self) -> int | None:
        return getattr(resource, "RLIMIT_AS", None)

    def build_preexec_fn(self) -> Callable[[], None] | None:
        """Build a Unix ``preexec_fn`` that applies configured limits."""
        if not self.should_enforce or resource is None:
            return None

        cpu_time_limit_seconds = self.cpu_time_limit_seconds
        memory_max_bytes = self.memory_max_bytes
        rlimit_cpu = self.rlimit_cpu
        rlimit_as = self.rlimit_as

        should_enforce_memory_limit = self.should_enforce_memory_limit

        def _apply_limits() -> None:
            if cpu_time_limit_seconds is not None and rlimit_cpu is not None:
                resource.setrlimit(
                    rlimit_cpu,
                    (cpu_time_limit_seconds, cpu_time_limit_seconds),
                )
            if (
                should_enforce_memory_limit
                and memory_max_bytes is not None
                and rlimit_as is not None
            ):
                resource.setrlimit(
                    rlimit_as,
                    (memory_max_bytes, memory_max_bytes),
                )

        return _apply_limits


def resolve_current_process_limit_policy(
    scope: ProcessLimitScope,
) -> CurrentProcessLimitPolicy:
    """Resolve global process limits while retaining the current tenant identity."""
    tenant_id = get_current_effective_tenant_id()
    config = load_config()
    process_limits = config.security.process_limits
    scope_enabled = (
        process_limits.shell if scope == "shell" else process_limits.mcp_stdio
    )
    enabled = bool(process_limits.enabled and scope_enabled)
    cpu_time_limit_seconds = process_limits.cpu_time_limit_seconds
    memory_max_mb = process_limits.memory_max_mb
    shell_max_concurrent = (
        process_limits.shell_max_concurrent if scope == "shell" else None
    )
    shell_acquire_timeout_seconds = (
        process_limits.shell_acquire_timeout_seconds if scope == "shell" else 5
    )

    if not enabled:
        return CurrentProcessLimitPolicy(
            tenant_id=tenant_id,
            scope=scope,
            enabled=False,
            cpu_time_limit_seconds=cpu_time_limit_seconds,
            memory_max_mb=memory_max_mb,
            should_enforce=False,
            should_enforce_memory_limit=False,
            shell_max_concurrent=None,
            shell_acquire_timeout_seconds=shell_acquire_timeout_seconds,
        )

    if not _supports_unix_rlimits():
        diagnostic = (
            f"Tenant process limits for {scope} are enabled for tenant "
            f"{tenant_id or 'default'}, but not enforced on this platform."
        )
        logger.warning(diagnostic)
        return CurrentProcessLimitPolicy(
            tenant_id=tenant_id,
            scope=scope,
            enabled=True,
            cpu_time_limit_seconds=cpu_time_limit_seconds,
            memory_max_mb=memory_max_mb,
            should_enforce=False,
            should_enforce_memory_limit=False,
            shell_max_concurrent=shell_max_concurrent,
            shell_acquire_timeout_seconds=shell_acquire_timeout_seconds,
            diagnostic=diagnostic,
        )

    should_enforce_memory_limit = bool(
        memory_max_mb is not None and _supports_memory_rlimit(scope),
    )
    should_enforce = bool(
        cpu_time_limit_seconds is not None or should_enforce_memory_limit,
    )
    diagnostic = None
    if memory_max_mb is not None and not should_enforce_memory_limit:
        diagnostic = (
            f"Tenant process limits for {scope} are enabled for tenant "
            f"{tenant_id or 'default'}, but memory limits are not "
            "enforced on this platform."
        )
        logger.warning(diagnostic)

    return CurrentProcessLimitPolicy(
        tenant_id=tenant_id,
        scope=scope,
        enabled=True,
        cpu_time_limit_seconds=cpu_time_limit_seconds,
        memory_max_mb=memory_max_mb,
        should_enforce=should_enforce,
        should_enforce_memory_limit=should_enforce_memory_limit,
        shell_max_concurrent=shell_max_concurrent,
        shell_acquire_timeout_seconds=shell_acquire_timeout_seconds,
        diagnostic=diagnostic,
    )
