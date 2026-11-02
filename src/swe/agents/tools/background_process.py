# -*- coding: utf-8 -*-
"""管理可跨工具调用存活的后台 Shell 进程。"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ..tool_failure import ToolExecutionError
from .shell import (
    _kill_process_tree_win32,
    _sanitize_win_cmd,
    prepare_shell_command,
    smart_decode,
)
from ...app.agent_context import get_current_agent_id
from ...config.context import (
    get_current_scope_id,
    get_current_source_id,
    get_current_task_progress_chat_id,
    get_current_tenant_id,
    get_current_user_id,
    get_current_workspace_dir,
    resolve_scope_id,
)

logger = logging.getLogger(__name__)

PROCESS_ID_PREFIX = "bgp_"
DEFAULT_SOURCE_ID = "__default_source__"
DEFAULT_SCOPE_ID = "__default_scope__"
DEFAULT_TENANT_ID = "__default_tenant__"
DEFAULT_USER_ID = "__default_user__"
DEFAULT_CHAT_ID = "__default_chat__"
DEFAULT_AGENT_ID = "default"
DEFAULT_WORKSPACE_DIR = "__default_workspace__"

MAX_RUNNING_PROCESSES_PER_OWNER = 5
MAX_RUNNING_PROCESSES_GLOBAL = 100
DEFAULT_OUTPUT_MAX_BYTES = 64 * 1024
OUTPUT_MAX_BYTES_HARD_LIMIT = 1024 * 1024
TERMINAL_RECORD_RETENTION = timedelta(hours=24)
STOP_GRACE_SECONDS = 5


@dataclass(frozen=True)
class BackgroundProcessOwnerKey:
    """后台进程的不可变归属范围。"""

    source_id: str
    scope_id: str
    tenant_id: str
    user_id: str
    chat_id: str
    agent_id: str
    workspace_dir: str


@dataclass
class ManagedBackgroundProcess:
    """后台进程管理器保存的进程记录。"""

    process_id: str
    owner_key: BackgroundProcessOwnerKey
    command: str
    cwd: str
    name: str | None
    pid: int
    popen: subprocess.Popen
    stdout_path: Path
    stderr_path: Path
    python_runtime_guard: object
    python_runtime_guard_entered: bool
    python_runtime_guard_cleaned: bool
    status: str
    returncode: int | None
    started_at: datetime
    stopped_at: datetime | None = None


def _build_owner_key() -> BackgroundProcessOwnerKey:
    source_id = get_current_source_id() or DEFAULT_SOURCE_ID
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    scope_id = (
        get_current_scope_id()
        or resolve_scope_id(tenant_id, source_id)
        or DEFAULT_SCOPE_ID
    )
    user_id = get_current_user_id() or DEFAULT_USER_ID
    chat_id = get_current_task_progress_chat_id() or DEFAULT_CHAT_ID
    workspace_dir = get_current_workspace_dir()
    workspace = (
        str(Path(workspace_dir).expanduser().resolve())
        if workspace_dir is not None
        else DEFAULT_WORKSPACE_DIR
    )
    try:
        agent_id = get_current_agent_id(tenant_id) or DEFAULT_AGENT_ID
    except Exception:
        agent_id = DEFAULT_AGENT_ID

    return BackgroundProcessOwnerKey(
        source_id=source_id,
        scope_id=scope_id,
        tenant_id=tenant_id,
        user_id=user_id,
        chat_id=chat_id,
        agent_id=agent_id,
        workspace_dir=workspace,
    )


def _new_process_id() -> str:
    return f"{PROCESS_ID_PREFIX}{uuid.uuid4().hex[:12]}"


def _command_preview(command: str, max_length: int = 160) -> str:
    normalized = " ".join(command.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3] + "..."


def _enter_runtime_guard(runtime_guard: object) -> bool:
    enter = getattr(runtime_guard, "__enter__", None)
    if not callable(enter):
        return False
    enter()
    return True


def _cleanup_runtime_guard(
    runtime_guard: object,
    *,
    entered: bool,
) -> None:
    if entered:
        exit_context = getattr(runtime_guard, "__exit__", None)
        if callable(exit_context):
            try:
                exit_context(None, None, None)
                return
            except Exception:
                logger.debug(
                    "Failed to exit Python runtime guard",
                    exc_info=True,
                )

    cleanup = getattr(runtime_guard, "cleanup", None)
    if callable(cleanup):
        try:
            cleanup()
        except Exception:
            logger.debug("Failed to clean Python runtime guard", exc_info=True)


def _delete_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug(
            "Failed to delete background process output file: %s",
            path,
        )


def _make_tool_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _raise_tool_error(error_type: str, detail: str) -> None:
    raise ToolExecutionError(error_type=error_type, detail=detail)


class ManagedBackgroundProcessManager:
    """以内存注册表管理后台 Shell 进程。"""

    def __init__(self) -> None:
        self._processes: dict[str, ManagedBackgroundProcess] = {}
        self._running_reservations_by_owner: dict[
            BackgroundProcessOwnerKey,
            int,
        ] = {}
        self._global_running_reservations = 0
        self._lock = threading.RLock()

    def _cleanup_record_runtime_guard(
        self,
        record: ManagedBackgroundProcess,
    ) -> None:
        if record.python_runtime_guard_cleaned:
            return
        _cleanup_runtime_guard(
            record.python_runtime_guard,
            entered=record.python_runtime_guard_entered,
        )
        record.python_runtime_guard_entered = False
        record.python_runtime_guard_cleaned = True

    def _refresh_record(
        self,
        record: ManagedBackgroundProcess,
        *,
        now: datetime | None = None,
    ) -> ManagedBackgroundProcess:
        returncode = record.popen.poll()
        if returncode is not None and record.status == "running":
            record.returncode = returncode
            record.status = "exited" if returncode == 0 else "failed"
            record.stopped_at = now or datetime.now(timezone.utc)
            self._cleanup_record_runtime_guard(record)
        return record

    def _records_for_owner(
        self,
        owner_key: BackgroundProcessOwnerKey,
    ) -> list[ManagedBackgroundProcess]:
        now = datetime.now(timezone.utc)
        with self._lock:
            records = list(self._processes.values())
            for record in records:
                self._refresh_record(record, now=now)
            return [
                record for record in records if record.owner_key == owner_key
            ]

    def _find_for_owner(
        self,
        process_id: str,
        owner_key: BackgroundProcessOwnerKey,
    ) -> ManagedBackgroundProcess | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            record = self._processes.get(process_id)
            if record is None:
                return None
            self._refresh_record(record, now=now)
            if record.owner_key != owner_key:
                return None
            return record

    def _count_running_locked(
        self,
        owner_key: BackgroundProcessOwnerKey | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        count = 0
        for record in self._processes.values():
            self._refresh_record(record, now=now)
            if record.status != "running":
                continue
            if owner_key is not None and record.owner_key != owner_key:
                continue
            count += 1
        return count

    def _reserve_running_slot_locked(
        self,
        owner_key: BackgroundProcessOwnerKey,
    ) -> None:
        owner_running = self._count_running_locked(owner_key)
        owner_running += self._running_reservations_by_owner.get(owner_key, 0)
        if owner_running >= MAX_RUNNING_PROCESSES_PER_OWNER:
            _raise_tool_error(
                "resource_limit_exceeded",
                "Background process per owner limit exceeded "
                f"(limit={MAX_RUNNING_PROCESSES_PER_OWNER}).",
            )

        global_running = self._count_running_locked()
        global_running += self._global_running_reservations
        if global_running >= MAX_RUNNING_PROCESSES_GLOBAL:
            _raise_tool_error(
                "resource_limit_exceeded",
                "Background process global limit exceeded "
                f"(limit={MAX_RUNNING_PROCESSES_GLOBAL}).",
            )

        self._running_reservations_by_owner[owner_key] = (
            self._running_reservations_by_owner.get(owner_key, 0) + 1
        )
        self._global_running_reservations += 1

    def _release_running_slot_locked(
        self,
        owner_key: BackgroundProcessOwnerKey,
    ) -> None:
        owner_reservations = self._running_reservations_by_owner.get(
            owner_key,
            0,
        )
        if owner_reservations <= 1:
            self._running_reservations_by_owner.pop(owner_key, None)
        else:
            self._running_reservations_by_owner[owner_key] = (
                owner_reservations - 1
            )
        self._global_running_reservations = max(
            0,
            self._global_running_reservations - 1,
        )

    def prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - TERMINAL_RECORD_RETENTION
        with self._lock:
            for process_id, record in list(self._processes.items()):
                self._refresh_record(record)
                if record.status == "running":
                    continue
                if (
                    record.stopped_at is not None
                    and record.stopped_at < cutoff
                ):
                    self._delete_record_locked(process_id)

    def _delete_record_locked(self, process_id: str) -> None:
        record = self._processes.pop(process_id, None)
        if record is None:
            return
        self._cleanup_record_runtime_guard(record)
        _delete_file(record.stdout_path)
        _delete_file(record.stderr_path)

    def start(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        name: str | None = None,
        owner_key: BackgroundProcessOwnerKey | None = None,
    ) -> ManagedBackgroundProcess:
        self.prune()
        owner = owner_key or _build_owner_key()

        with self._lock:
            self._reserve_running_slot_locked(owner)

        prepared = None
        stdout_file = None
        stderr_file = None
        guard_entered = False
        reserved = True

        try:
            prepared = prepare_shell_command(command, cwd)
            stdout_fd, stdout_path = tempfile.mkstemp(prefix="swe_bgp_out_")
            stderr_fd, stderr_path = tempfile.mkstemp(prefix="swe_bgp_err_")
            stdout_file = os.fdopen(stdout_fd, "wb")
            stderr_file = os.fdopen(stderr_fd, "wb")
            process_id = _new_process_id()
            if sys.platform == "win32":
                shell_command = _sanitize_win_cmd(prepared.command)
                argv: str | list[str] = f'cmd /D /S /C "{shell_command}"'
                popen_kwargs = {
                    "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
                }
            else:
                argv = ["/bin/sh", "-c", prepared.command]
                popen_kwargs = {"start_new_session": True}

            guard_entered = _enter_runtime_guard(
                prepared.python_runtime_guard,
            )
            popen = subprocess.Popen(  # pylint: disable=consider-using-with
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=False,
                cwd=str(prepared.working_dir),
                env=prepared.env,
                **popen_kwargs,
            )
        except Exception as exc:
            if stdout_file is not None:
                stdout_file.close()
            if stderr_file is not None:
                stderr_file.close()
            if "stdout_path" in locals():
                _delete_file(Path(stdout_path))
            if "stderr_path" in locals():
                _delete_file(Path(stderr_path))
            if prepared is not None:
                _cleanup_runtime_guard(
                    prepared.python_runtime_guard,
                    entered=guard_entered,
                )
            if reserved:
                with self._lock:
                    self._release_running_slot_locked(owner)
            if isinstance(exc, ToolExecutionError):
                raise
            _raise_tool_error(
                "unexpected_tool_error",
                f"Failed to start background process: {exc}",
            )

        stdout_file.close()
        stderr_file.close()

        record = ManagedBackgroundProcess(
            process_id=process_id,
            owner_key=owner,
            command=prepared.command,
            cwd=str(prepared.working_dir),
            name=name,
            pid=popen.pid,
            popen=popen,
            stdout_path=Path(stdout_path),
            stderr_path=Path(stderr_path),
            python_runtime_guard=prepared.python_runtime_guard,
            python_runtime_guard_entered=guard_entered,
            python_runtime_guard_cleaned=False,
            status="running",
            returncode=None,
            started_at=datetime.now(timezone.utc),
        )

        with self._lock:
            self._processes[process_id] = record
            self._release_running_slot_locked(owner)
            reserved = False

        logger.info(
            "Started managed background process process_id=%s pid=%s "
            "status=running",
            process_id,
            popen.pid,
        )
        return record

    def list_for_owner(
        self,
        owner_key: BackgroundProcessOwnerKey | None = None,
    ) -> list[ManagedBackgroundProcess]:
        self.prune()
        return self._records_for_owner(owner_key or _build_owner_key())

    def get_for_owner(
        self,
        process_id: str,
        owner_key: BackgroundProcessOwnerKey | None = None,
    ) -> ManagedBackgroundProcess | None:
        return self._find_for_owner(
            process_id,
            owner_key or _build_owner_key(),
        )

    def stop_for_owner(
        self,
        process_id: str,
        owner_key: BackgroundProcessOwnerKey | None = None,
    ) -> ManagedBackgroundProcess | None:
        record = self._find_for_owner(
            process_id,
            owner_key or _build_owner_key(),
        )
        if record is None:
            return None

        if record.status != "running":
            return record

        try:
            if sys.platform == "win32":
                _kill_process_tree_win32(record.pid)
                try:
                    record.popen.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    record.popen.kill()
                    try:
                        record.popen.wait(timeout=STOP_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            "Timed out waiting for bg process stop: %s",
                            process_id,
                        )
            else:
                os.killpg(os.getpgid(record.pid), signal.SIGTERM)
                try:
                    record.popen.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(record.pid), signal.SIGKILL)
                    record.popen.wait(timeout=STOP_GRACE_SECONDS)
        except (ProcessLookupError, OSError):
            logger.debug(
                "Background process already exited during stop: %s",
                process_id,
                exc_info=True,
            )

        with self._lock:
            record.returncode = record.popen.poll()
            record.status = "stopped"
            record.stopped_at = datetime.now(timezone.utc)
            self._cleanup_record_runtime_guard(record)
        return record

    def stop_all(self) -> None:
        with self._lock:
            process_ids = list(self._processes.keys())

        for process_id in process_ids:
            with self._lock:
                record = self._processes.get(process_id)
            if record is None:
                continue
            if record.status == "running":
                self.stop_for_owner(process_id, record.owner_key)

        with self._lock:
            for process_id in list(self._processes.keys()):
                self._delete_record_locked(process_id)


managed_background_process_manager = ManagedBackgroundProcessManager()
atexit.register(managed_background_process_manager.stop_all)


def _format_started(record: ManagedBackgroundProcess) -> str:
    return "\n".join(
        [
            "Background process started",
            f"process_id: {record.process_id}",
            f"pid: {record.pid}",
            f"status: {record.status}",
            f"cwd: {record.cwd}",
            f"name: {record.name or ''}",
        ],
    )


async def start_background_process(
    command: str,
    cwd: str | None = None,
    name: str | None = None,
) -> ToolResponse:
    """启动一个受管理的后台 Shell 进程。"""
    cmd = (command or "").strip()
    if not cmd:
        _raise_tool_error("invalid_arguments", "No command provided.")

    record = await _start_in_thread(cmd, cwd=cwd, name=name)
    return _make_tool_response(_format_started(record))


async def _start_in_thread(
    command: str,
    *,
    cwd: Optional[Path | str],
    name: str | None,
) -> ManagedBackgroundProcess:
    import asyncio

    from .shell_interceptor import intercept_command

    command, _was_intercepted = await intercept_command(command)
    return await asyncio.to_thread(
        managed_background_process_manager.start,
        command,
        cwd=cwd,
        name=name,
    )


def _format_list(records: list[ManagedBackgroundProcess]) -> str:
    if not records:
        return "No background processes in current scope."

    lines = ["Background processes:"]
    for record in records:
        stopped_at = record.stopped_at.isoformat() if record.stopped_at else ""
        lines.extend(
            [
                f"process_id: {record.process_id}",
                f"name: {record.name or ''}",
                f"pid: {record.pid}",
                f"status: {record.status}",
                f"returncode: {record.returncode}",
                f"started_at: {record.started_at.isoformat()}",
                f"stopped_at: {stopped_at}",
                f"cwd: {record.cwd}",
                f"command: {_command_preview(record.command)}",
                "",
            ],
        )
    return "\n".join(lines).rstrip()


async def list_background_processes() -> ToolResponse:
    """列出当前归属范围内的后台进程。"""
    records = managed_background_process_manager.list_for_owner()
    return _make_tool_response(_format_list(records))


def _clamp_output_bytes(max_bytes: int) -> int:
    try:
        requested = int(max_bytes)
    except (TypeError, ValueError):
        requested = DEFAULT_OUTPUT_MAX_BYTES
    return max(1, min(requested, OUTPUT_MAX_BYTES_HARD_LIMIT))


def _read_tail(path: Path, max_bytes: int) -> tuple[str, bool]:
    try:
        size = path.stat().st_size
        with path.open("rb") as output_file:
            if size > max_bytes:
                output_file.seek(size - max_bytes)
                return smart_decode(output_file.read()), True
            return smart_decode(output_file.read()), False
    except OSError:
        return "", False


def _format_output(record: ManagedBackgroundProcess, max_bytes: int) -> str:
    stdout, stdout_truncated = _read_tail(record.stdout_path, max_bytes)
    stderr, stderr_truncated = _read_tail(record.stderr_path, max_bytes)
    truncated = stdout_truncated or stderr_truncated

    parts = [
        f"process_id: {record.process_id}",
        f"status: {record.status}",
        f"returncode: {record.returncode}",
        f"truncated: {str(truncated).lower()}",
        "[stdout]",
        stdout or "(no output)",
    ]
    if stderr:
        parts.extend(["[stderr]", stderr])
    return "\n".join(parts)


async def get_process_output(
    process_id: str,
    max_bytes: int = DEFAULT_OUTPUT_MAX_BYTES,
) -> ToolResponse:
    """读取受管理进程已捕获的 stdout/stderr 尾部内容。"""
    record = managed_background_process_manager.get_for_owner(process_id)
    if record is None:
        return _make_tool_response(
            f"Process not found in current scope: {process_id}",
        )
    max_bytes = _clamp_output_bytes(max_bytes)
    return _make_tool_response(_format_output(record, max_bytes))


async def stop_background_process(process_id: str) -> ToolResponse:
    """停止当前归属范围内的受管理进程。"""
    record = managed_background_process_manager.stop_for_owner(process_id)
    if record is None:
        return _make_tool_response(
            f"Process not found in current scope: {process_id}",
        )
    return _make_tool_response(
        "\n".join(
            [
                f"process_id: {record.process_id}",
                f"status: {record.status}",
                f"returncode: {record.returncode}",
            ],
        ),
    )


__all__ = [
    "BackgroundProcessOwnerKey",
    "ManagedBackgroundProcess",
    "ManagedBackgroundProcessManager",
    "managed_background_process_manager",
    "start_background_process",
    "list_background_processes",
    "get_process_output",
    "stop_background_process",
]
