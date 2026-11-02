# -*- coding: utf-8 -*-
"""Bounded physical-scope reads; no workspace initialization or source writes."""

from __future__ import annotations

import errno
import heapq
import json
import math
import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from .auth_migration_models import AuthMigrationRequest, safe_scope
from .auth_repository import _scope_metadata
from .auth_state import CronAuthState

MAX_FILE_BYTES = 8 * 1024 * 1024


class AuthMigrationSourceUnavailable(RuntimeError):
    """The source root cannot safely establish file presence or absence."""


@dataclass
class SourceRecord:
    storage_scope_id: str
    status: str
    state: CronAuthState | None = None
    reason: str | None = None


@dataclass
class SourcePage:
    items: list[SourceRecord] = field(default_factory=list)
    scanned: int = 0
    next_after: str | None = None


def matches(scope: str, request: AuthMigrationRequest) -> bool:
    tenant, source = _scope_metadata(scope)
    return all(
        values is None or value in values
        for value, values in (
            (scope, request.storage_scope_ids),
            (tenant, request.tenant_ids),
            (source, request.source_ids),
        )
    )


def check_root(root: Path) -> None:
    """Require a real, readable root; absence never means an empty inventory."""
    try:
        if root.is_symlink():
            raise OSError("unsafe root")
        with os.scandir(root) as entries:
            next(entries, None)
    except OSError:
        raise AuthMigrationSourceUnavailable(
            "auth_migration_source_unavailable",
        ) from None


def _open_directory(path, *, dir_fd=None):
    return os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=dir_fd,
    )


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_json_number")
    return number


def read_record(root: Path, scope: str) -> SourceRecord:
    """A bounded no-follow open protects every path component from symlinks."""
    check_root(root)
    if not safe_scope(scope):
        return SourceRecord(scope, "invalid", reason="unsafe_scope")
    try:
        root_fd = _open_directory(root)
    except OSError:
        raise AuthMigrationSourceUnavailable(
            "auth_migration_source_unavailable",
        ) from None
    try:
        with ExitStack() as stack:
            stack.callback(os.close, root_fd)
            parent_fd = root_fd
            for component in (scope, ".secret"):
                parent_fd = _open_directory(component, dir_fd=parent_fd)
                stack.callback(os.close, parent_fd)
            fd = os.open(
                "cron_auth.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    return SourceRecord(
                        scope,
                        "invalid",
                        reason="unsafe_source_file",
                    )
                raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return SourceRecord(
                scope,
                "invalid",
                reason="source_file_too_large",
            )
        data = json.loads(
            raw.decode("utf-8"),
            parse_float=_finite_number,
            parse_constant=_finite_number,
        )
        if not isinstance(data, dict):
            raise ValueError("object required")
        state = CronAuthState.model_validate(data)
        # Use the actual persistence serializer before this record can reach
        # async database work; deeply nested JSON may exceed its depth limit.
        json.dumps(state.model_dump(mode="json"), allow_nan=False)
        return SourceRecord(scope, "valid", state=state)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            check_root(root)
            return SourceRecord(scope, "absent")
        return SourceRecord(
            scope,
            "invalid",
            reason="source_unreadable_or_unsafe",
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return SourceRecord(scope, "invalid", reason="invalid_source_json")


def read_source_page(
    root: Path,
    request: AuthMigrationRequest,
    after: str | None,
) -> SourcePage:
    check_root(root)
    try:
        with os.scandir(root) as entries:
            names = heapq.nsmallest(
                request.page_size + 1,
                (
                    item.name
                    for item in entries
                    if safe_scope(item.name)
                    and (
                        after is None
                        or item.name.encode("utf-8") > after.encode("utf-8")
                    )
                    and (
                        item.is_dir(follow_symlinks=False) or item.is_symlink()
                    )
                ),
                key=lambda value: value.encode("utf-8"),
            )
    except OSError:
        raise AuthMigrationSourceUnavailable(
            "auth_migration_source_unavailable",
        ) from None
    page = SourcePage(scanned=min(len(names), request.page_size))
    for scope in names[: request.page_size]:
        if matches(scope, request):
            record = read_record(root, scope)
            if record.status != "absent":
                page.items.append(record)
    if len(names) > request.page_size:
        page.next_after = names[request.page_size - 1]
    return page
