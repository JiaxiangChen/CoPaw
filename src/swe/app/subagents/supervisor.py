# -*- coding: utf-8 -*-
"""Main-process supervisor for Background SubAgent subprocess runs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Literal, Mapping
from uuid import uuid4

import psutil
from pydantic import BaseModel, Field

from ...config.config import AgentProfileConfig
from ...config.context import get_current_scope_id
from .builtins import builtin_definition_provider
from .models import (
    BackgroundSubAgentRunRecord,
    BudgetConfig,
    DelegationSpec,
    DefinitionMatchMetadata,
    PermissionPolicy,
    SubAgentDefinition,
    SubAgentLaunchDiagnostics,
    SubAgentLaunchSnapshot,
    SubAgentStartRequest,
    TERMINAL_BACKGROUND_RUN_STATUSES,
    WorkerLaunchSpec,
)
from .launch_snapshot import (
    ModelLaunchSnapshotError,
    _ensure_private_run_store_dir,
    capture_launch_dependencies,
    capture_model_launch_snapshot,
    remove_launch_skill_snapshot,
)
from .session_dependencies import (
    capture_community_expert_session_dependencies,
    resolve_community_expert_dependency_view,
)
from .nicknames import allocate_subagent_nickname, assign_subagent_nickname
from .run_access import read_chat_run_records
from .permissions import build_definition_policy, compose_effective_policy
from .registry import AgentRegistry
from .run_store import PerRunSubAgentRunStore

_CONCURRENCY_LIMIT_REASON = "background_subagent_concurrency_limit"
_WORKER_EXITED_WITHOUT_RESULT = "worker_exited_without_result"
_GENERIC_FAILURE_MESSAGE = "SubAgent execution failed."
_NICKNAME_RESERVATION_STALE_SECONDS = 300
_PROCESS_START_TIME_TOLERANCE_SECONDS = 1
logger = logging.getLogger(__name__)


def _effective_budget(
    definition_budget: BudgetConfig,
    spec_budget: BudgetConfig,
) -> BudgetConfig:
    return BudgetConfig(
        max_turns=min(definition_budget.max_turns, spec_budget.max_turns),
        max_tool_calls=min(
            definition_budget.max_tool_calls,
            spec_budget.max_tool_calls,
        ),
        timeout_ms=min(definition_budget.timeout_ms, spec_budget.timeout_ms),
    )


class BackgroundSubAgentScope(BaseModel):
    """Current tenant-and-agent scope for Background SubAgent operations."""

    tenant_id: str
    agent_id: str
    run_store_dir: Path


class BackgroundSubAgentStartBlocked(BaseModel):
    """Response returned when per-scope concurrency is exhausted."""

    status: Literal["blocked"] = "blocked"
    reason: str = _CONCURRENCY_LIMIT_REASON
    limit: int
    active_run_ids: list[str] = Field(default_factory=list)


class BackgroundSubAgentNotManageable(BaseModel):
    """Response for a known run without an active supervisor handle."""

    status: Literal["not_manageable"] = "not_manageable"
    run_id: str
    reason: str = "background_subagent_run_not_manageable"


class BackgroundSubAgentWaitSnapshot(BaseModel):
    """Bounded wait result for currently manageable background runs."""

    active_runs: list[BackgroundSubAgentRunRecord] = Field(
        default_factory=list,
    )
    terminal_runs: list[BackgroundSubAgentRunRecord] = Field(
        default_factory=list,
    )
    timed_out: bool = False


@dataclass
class _ActiveRun:
    scope: BackgroundSubAgentScope
    process: Any
    stderr_log_path: Path
    parent_chat_id: str = ""
    parent_msgid: str = ""
    cancel_task: asyncio.Task[BackgroundSubAgentRunRecord] | None = None


def _worker_parent_agent_config(
    parent_agent_config: AgentProfileConfig,
) -> dict[str, Any]:
    """Serialize worker config without any parent MCP credentials/config."""
    payload = parent_agent_config.model_dump(mode="json")
    payload.pop("mcp", None)
    return payload


def _remove_private_launch_snapshot(
    path: str | None,
    run_store_dir: Path,
    run_id: str,
) -> None:
    """Remove an undelivered private dependency snapshot for this run."""
    if not path:
        return
    candidate = Path(path)
    try:
        if candidate.parent.resolve() != run_store_dir.resolve():
            return
    except OSError:
        return
    if candidate.name not in {
        f".{run_id}.mcp.json",
        f".{run_id}.model.json",
    }:
        return
    try:
        candidate.unlink()
    except OSError:
        pass


class BackgroundSubAgentSupervisor:
    """Supervise active Background SubAgent worker subprocesses."""

    def __init__(
        self,
        *,
        max_running_per_scope: int = 2,
        registry: AgentRegistry | None = None,
        popen_factory: Any | None = None,
        cancel_grace_seconds: float = 1.0,
    ):
        self._max_running_per_scope = max_running_per_scope
        self._registry = registry or AgentRegistry(
            [builtin_definition_provider()],
        )
        self._popen_factory = popen_factory or subprocess.Popen
        self._cancel_grace_seconds = cancel_grace_seconds
        self._active: dict[tuple[str, str, str], dict[str, _ActiveRun]] = {}
        self._starting: dict[tuple[str, str, str], set[str]] = {}

    async def start(
        self,
        *,
        scope: BackgroundSubAgentScope,
        spec: DelegationSpec,
        parent_agent_config: AgentProfileConfig,
        workspace_dir: Path,
        parent_policy: PermissionPolicy | None = None,
        workspace_policy: PermissionPolicy | None = None,
        runtime_policy: PermissionPolicy | None = None,
        request_context: dict[str, Any] | None = None,
        effective_skill_names: list[str] | None = None,
        skill_snapshot_signatures: dict[str, str] | None = None,
        skill_snapshot_dirs: Mapping[str, Path] | None = None,
        definition: SubAgentDefinition | None = None,
        start_request: SubAgentStartRequest | None = None,
        definition_match: DefinitionMatchMetadata | None = None,
    ) -> BackgroundSubAgentRunRecord | BackgroundSubAgentStartBlocked:
        """Reserve capacity before any asynchronous launch preparation."""
        definition = definition or self._registry.resolve(spec.name)
        nickname, reservation_path = await self._reserve_nickname(
            scope,
            spec,
            definition,
        )
        try:
            await self._reap_scope(scope)
            active = self._active_for_scope(scope)
            starting = self._starting.setdefault(self._scope_key(scope), set())
            if len(set(active) | starting) >= self._max_running_per_scope:
                return BackgroundSubAgentStartBlocked(
                    limit=self._max_running_per_scope,
                    active_run_ids=sorted(set(active) | starting),
                )
            # No await between the capacity check and reservation: admission is
            # atomic for concurrent requests on this supervisor's event loop.
            run_id = f"subagent-{uuid4().hex[:12]}"
            starting.add(run_id)
            try:
                return await self._start_reserved(
                    scope=scope,
                    run_id=run_id,
                    nickname=nickname,
                    spec=spec,
                    parent_agent_config=parent_agent_config,
                    workspace_dir=workspace_dir,
                    parent_policy=parent_policy,
                    workspace_policy=workspace_policy,
                    runtime_policy=runtime_policy,
                    request_context=request_context,
                    effective_skill_names=effective_skill_names,
                    skill_snapshot_signatures=skill_snapshot_signatures,
                    skill_snapshot_dirs=skill_snapshot_dirs,
                    definition=definition,
                    start_request=start_request,
                    definition_match=definition_match,
                )
            except asyncio.CancelledError:
                if run_id in active:
                    cleanup = asyncio.create_task(self.cancel(scope, run_id))
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(cleanup),
                            timeout=5,
                        )
                    except (Exception, asyncio.CancelledError):
                        logger.warning(
                            "background_subagent_start_cancel_unconfirmed run_id=%s",
                            run_id,
                        )
                raise
            finally:
                starting.discard(run_id)
        finally:
            try:
                reservation_path.unlink()
            except FileNotFoundError:
                pass

    @asynccontextmanager
    async def _nickname_allocation_lock(
        self,
        scope: BackgroundSubAgentScope,
        parent_thread_id: str,
    ):
        """Serialize nickname allocation until its run record is durable."""
        digest = hashlib.sha256(
            parent_thread_id.encode("utf-8"),
        ).hexdigest()[:16]
        _ensure_private_run_store_dir(scope.run_store_dir)
        lock_path = scope.run_store_dir / f".nickname-{digest}.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
            try:
                while True:
                    try:
                        fcntl.flock(
                            stream.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.01)
                yield digest
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    async def _reserve_nickname(
        self,
        scope: BackgroundSubAgentScope,
        spec: DelegationSpec,
        definition: SubAgentDefinition,
    ) -> tuple[str, Path]:
        """Persist a Chat-visible reservation before launch preparation awaits."""
        async with self._nickname_allocation_lock(
            scope,
            spec.parent_thread_id,
        ) as session_digest:
            chat_context = {
                "session_id": spec.parent_thread_id,
                "chat_id": spec.parent_chat_id,
            }
            used = {
                record.nickname
                for record in read_chat_run_records(
                    scope.run_store_dir,
                    chat_context,
                )
                if record.nickname
            }
            for reservation in self._nickname_reservations(
                scope,
                session_digest,
            ):
                if reservation["session_id"] != spec.parent_thread_id:
                    continue
                reserved_chat_id = reservation["chat_id"]
                if (
                    not reserved_chat_id
                    or not spec.parent_chat_id
                    or reserved_chat_id == spec.parent_chat_id
                ):
                    used.add(reservation["nickname"])
            nickname = allocate_subagent_nickname(
                assign_subagent_nickname(definition.nickname),
                used,
            )
            reservation_path = (
                scope.run_store_dir
                / f".nickname-reservation-{session_digest}-{uuid4().hex}.json"
            )
            self._write_nickname_reservation(
                reservation_path,
                {
                    "session_id": spec.parent_thread_id,
                    "chat_id": spec.parent_chat_id,
                    "nickname": nickname,
                    "owner_pid": os.getpid(),
                    "owner_started_at": psutil.Process().create_time(),
                    "created_at": time.time(),
                },
            )
            return nickname, reservation_path

    @classmethod
    def _nickname_reservations(
        cls,
        scope: BackgroundSubAgentScope,
        session_digest: str,
    ) -> list[dict[str, str]]:
        reservations = []
        for path in scope.run_store_dir.glob(
            f".nickname-reservation-{session_digest}-*.json",
        ):
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                session_id = str(payload["session_id"])
                chat_id = str(payload["chat_id"])
                nickname = str(payload["nickname"])
            except (OSError, ValueError, KeyError, TypeError):
                cls._remove_stale_nickname_reservation(path)
                continue
            if cls._reservation_is_stale(payload):
                cls._remove_stale_nickname_reservation(path)
                continue
            if session_id and nickname:
                reservations.append(
                    {
                        "session_id": session_id,
                        "chat_id": chat_id,
                        "nickname": nickname,
                    },
                )
        return reservations

    @staticmethod
    def _write_nickname_reservation(
        path: Path,
        payload: dict[str, str | int | float],
    ) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        os.chmod(path, 0o600)

    @staticmethod
    def _reservation_is_stale(payload: Mapping[str, Any]) -> bool:
        try:
            created_at = float(payload["created_at"])
            owner_pid = int(payload["owner_pid"])
            owner_started_at = float(payload["owner_started_at"])
        except (KeyError, TypeError, ValueError):
            return True
        if time.time() - created_at < _NICKNAME_RESERVATION_STALE_SECONDS:
            return False
        try:
            return (
                abs(
                    psutil.Process(owner_pid).create_time() - owner_started_at,
                )
                > _PROCESS_START_TIME_TOLERANCE_SECONDS
            )
        except psutil.NoSuchProcess:
            return True
        except (psutil.Error, ValueError):
            return False

    @staticmethod
    def _remove_stale_nickname_reservation(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    async def _start_reserved(
        self,
        *,
        scope: BackgroundSubAgentScope,
        run_id: str,
        nickname: str,
        spec: DelegationSpec,
        parent_agent_config: AgentProfileConfig,
        workspace_dir: Path,
        parent_policy: PermissionPolicy | None = None,
        workspace_policy: PermissionPolicy | None = None,
        runtime_policy: PermissionPolicy | None = None,
        request_context: dict[str, Any] | None = None,
        effective_skill_names: list[str] | None = None,
        skill_snapshot_signatures: dict[str, str] | None = None,
        skill_snapshot_dirs: Mapping[str, Path] | None = None,
        definition: SubAgentDefinition | None = None,
        start_request: SubAgentStartRequest | None = None,
        definition_match: DefinitionMatchMetadata | None = None,
    ) -> BackgroundSubAgentRunRecord | BackgroundSubAgentStartBlocked:
        """Prepare and launch a run whose capacity is already reserved."""
        active = self._active_for_scope(scope)
        definition = definition or self._registry.resolve(spec.name)
        parent_policy = parent_policy or PermissionPolicy.readonly()
        definition_policy = build_definition_policy(definition, parent_policy)
        effective_policy = compose_effective_policy(
            parent_policy,
            definition_policy,
            runtime_policy or parent_policy,
            workspace_policy or parent_policy,
        )
        store = PerRunSubAgentRunStore(scope.run_store_dir)
        effective_budget = _effective_budget(definition.budget, spec.budget)
        parent_skill_snapshot_dirs = skill_snapshot_dirs
        try:
            session_dependency_view = resolve_community_expert_dependency_view(
                workspace_dir=workspace_dir,
                chat_id=str((request_context or {}).get("chat_id") or ""),
                definition=definition,
                view_root=(request_context or {}).get(
                    "_expert_dependency_view_root",
                ),
            )
            if session_dependency_view is not None:
                (
                    snapshotted_skill_paths,
                    private_mcp_snapshot_path,
                    diagnostics,
                ) = await asyncio.to_thread(
                    capture_community_expert_session_dependencies,
                    run_store_dir=scope.run_store_dir,
                    run_id=run_id,
                    dependency_view_root=session_dependency_view,
                    definition=definition,
                    parent_agent_config=parent_agent_config,
                )
            else:
                (
                    snapshotted_skill_paths,
                    private_mcp_snapshot_path,
                    diagnostics,
                ) = await asyncio.to_thread(
                    capture_launch_dependencies,
                    run_store_dir=scope.run_store_dir,
                    run_id=run_id,
                    workspace_dir=workspace_dir,
                    parent_agent_config=parent_agent_config,
                    definition=definition,
                    effective_skill_names=effective_skill_names or [],
                    skill_snapshot_signatures=skill_snapshot_signatures,
                    skill_snapshot_dirs=parent_skill_snapshot_dirs,
                )
        except OSError:
            record = await store.create(
                spec,
                definition,
                effective_policy,
                effective_budget=effective_budget,
                start_request=start_request,
                definition_match=definition_match,
                nickname=nickname,
                run_id=run_id,
            )
            return await store.fail(
                record.run_id,
                _GENERIC_FAILURE_MESSAGE,
                error_code="worker_snapshot_failed",
            )
        try:
            private_model_snapshot_path, resolved_model = (
                capture_model_launch_snapshot(
                    tenant_id=scope.tenant_id,
                    run_store_dir=scope.run_store_dir,
                    run_id=run_id,
                    definition=definition,
                )
            )
        except (OSError, ModelLaunchSnapshotError):
            _remove_private_launch_snapshot(
                private_mcp_snapshot_path,
                scope.run_store_dir,
                run_id,
            )
            remove_launch_skill_snapshot(scope.run_store_dir, run_id)
            record = await store.create(
                spec,
                definition,
                effective_policy,
                effective_budget=effective_budget,
                start_request=start_request,
                definition_match=definition_match,
                nickname=nickname,
                run_id=run_id,
            )
            return await store.fail(
                record.run_id,
                _GENERIC_FAILURE_MESSAGE,
                error_code="worker_snapshot_failed",
            )
        diagnostics = diagnostics.model_copy(
            update={
                "resolved_model": (
                    resolved_model.model_dump(mode="json")
                    if resolved_model is not None
                    else None
                ),
            },
        )
        record = await store.create(
            spec,
            definition,
            effective_policy,
            effective_budget=effective_budget,
            start_request=start_request,
            definition_match=definition_match,
            nickname=nickname,
            launch_diagnostics=diagnostics,
            run_id=run_id,
        )
        launch_path = scope.run_store_dir / f"{record.run_id}.launch.json"
        stderr_log_path = scope.run_store_dir / f"{record.run_id}.stderr.log"
        worker_context = {
            **(request_context or {}),
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
        }
        scope_id = get_current_scope_id()
        if scope_id is not None:
            worker_context["scope_id"] = scope_id
        launch_spec = WorkerLaunchSpec(
            run_id=record.run_id,
            run_store_dir=str(scope.run_store_dir),
            workspace_dir=str(workspace_dir),
            parent_agent_config=_worker_parent_agent_config(
                parent_agent_config,
            ),
            definition=definition,
            delegation_spec=spec,
            effective_policy=effective_policy,
            start_request=record.start_request,
            definition_match=record.definition_match,
            nickname=record.nickname,
            request_context=worker_context,
            stderr_log_path=str(stderr_log_path),
            launch_snapshot=SubAgentLaunchSnapshot(
                skill_snapshot_dirs=snapshotted_skill_paths,
                private_mcp_snapshot_path=private_mcp_snapshot_path,
                private_model_snapshot_path=private_model_snapshot_path,
            ),
            launch_diagnostics=diagnostics,
        )
        logger.info(
            "background_subagent_start run_id=%s tenant_id=%s agent_id=%s "
            "requested_name=%s definition_name=%s definition_source=%s "
            "definition_matched=%s definition_match_reason=%s",
            record.run_id,
            scope.tenant_id,
            scope.agent_id,
            spec.name,
            definition.name,
            definition.source,
            record.definition_match.matched,
            record.definition_match.reason,
        )
        try:
            self._write_launch_spec(launch_path, launch_spec)
        except Exception:
            _remove_private_launch_snapshot(
                private_mcp_snapshot_path,
                scope.run_store_dir,
                record.run_id,
            )
            _remove_private_launch_snapshot(
                private_model_snapshot_path,
                scope.run_store_dir,
                record.run_id,
            )
            return await store.fail(
                record.run_id,
                _GENERIC_FAILURE_MESSAGE,
                error_code="worker_launch_spec_failed",
            )
        command = [
            sys.executable,
            "-m",
            "swe.app.subagents.worker",
            "--launch-spec",
            str(launch_path),
        ]
        try:
            process = self._start_process(command, stderr_log_path)
        except Exception:
            _remove_private_launch_snapshot(
                private_mcp_snapshot_path,
                scope.run_store_dir,
                record.run_id,
            )
            _remove_private_launch_snapshot(
                private_model_snapshot_path,
                scope.run_store_dir,
                record.run_id,
            )
            return await store.fail(
                record.run_id,
                _GENERIC_FAILURE_MESSAGE,
                error_code="worker_start_failed",
            )
        active[record.run_id] = _ActiveRun(
            scope=scope,
            process=process,
            stderr_log_path=stderr_log_path,
            parent_chat_id=spec.parent_chat_id,
            parent_msgid=spec.parent_msgid,
        )
        return await store.mark_running(
            record.run_id,
            worker_pid=process.pid,
            stderr_log_path=str(stderr_log_path),
        )

    async def cancel_turn_runs(
        self,
        scope: BackgroundSubAgentScope,
        *,
        chat_id: str,
        msgid: str,
    ) -> list[str]:
        """Best-effort cancel active runs owned by one chat answer turn."""
        if not chat_id or not msgid:
            return []
        await self._reap_scope(scope)
        active = self._active_for_scope(scope)
        run_ids = [
            run_id
            for run_id, handle in active.items()
            if handle.parent_chat_id == chat_id
            and handle.parent_msgid == msgid
        ]
        cancelled: list[str] = []
        for run_id in run_ids:
            result = await self.cancel(scope, run_id)
            if result is not None and not isinstance(
                result,
                BackgroundSubAgentNotManageable,
            ):
                cancelled.append(run_id)
        return cancelled

    async def wait(
        self,
        scope: BackgroundSubAgentScope,
        *,
        timeout_ms: int = 3000,
    ) -> BackgroundSubAgentWaitSnapshot:
        """Bounded wait over current active handles in one scope."""
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        terminal_runs: list[BackgroundSubAgentRunRecord] = []
        while True:
            terminal_runs.extend(await self._reap_scope(scope))
            if terminal_runs or time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(0.05, max(deadline - time.monotonic(), 0)))
        active_runs = await self._active_records(scope)
        return BackgroundSubAgentWaitSnapshot(
            active_runs=active_runs,
            terminal_runs=terminal_runs,
            timed_out=bool(active_runs and not terminal_runs),
        )

    async def reap_run(
        self,
        scope: BackgroundSubAgentScope,
        run_id: str,
    ) -> BackgroundSubAgentRunRecord | None:
        """Reap only one worker handle after its caller verifies ownership."""
        handle = self._active_for_scope(scope).get(run_id)
        if handle is None:
            return await PerRunSubAgentRunStore(scope.run_store_dir).get(
                run_id,
            )
        return await self._reap_one(scope, run_id, handle)

    async def wait_for_run(
        self,
        scope: BackgroundSubAgentScope,
        run_id: str,
    ) -> BackgroundSubAgentRunRecord | None:
        """Wait for one managed worker to reach a terminal persisted record."""
        active = self._active_for_scope(scope)
        handle = active.get(run_id)
        if handle is None:
            return await PerRunSubAgentRunStore(scope.run_store_dir).get(
                run_id,
            )
        if handle.cancel_task is not None:
            return await self._wait_for_cancellation(handle, run_id)
        await asyncio.to_thread(handle.process.wait)
        if handle.cancel_task is not None:
            return await self._wait_for_cancellation(handle, run_id)
        terminal_runs = await self._reap_scope(scope)
        return next(
            (item for item in terminal_runs if item.run_id == run_id),
            await PerRunSubAgentRunStore(scope.run_store_dir).get(run_id),
        )

    async def _wait_for_cancellation(
        self,
        handle: _ActiveRun,
        run_id: str,
    ) -> BackgroundSubAgentRunRecord | None:
        assert handle.cancel_task is not None
        try:
            return await asyncio.shield(handle.cancel_task)
        except Exception:
            # The cancellation observer logs cleanup failures. Waiters must
            # still see persisted state; retain ownership for cancel retries.
            return await PerRunSubAgentRunStore(
                handle.scope.run_store_dir,
            ).get(run_id)

    async def get(
        self,
        scope: BackgroundSubAgentScope,
        run_id: str,
    ) -> BackgroundSubAgentRunRecord | None:
        """Read a run record in scope and lazy-reap active workers first."""
        await self._reap_scope(scope)
        return await PerRunSubAgentRunStore(scope.run_store_dir).get(run_id)

    async def cancel(
        self,
        scope: BackgroundSubAgentScope,
        run_id: str,
    ) -> BackgroundSubAgentRunRecord | BackgroundSubAgentNotManageable | None:
        """Cancel an active worker process group for a run in scope."""
        await self._reap_scope(scope)
        return await self.cancel_run(scope, run_id)

    async def cancel_run(
        self,
        scope: BackgroundSubAgentScope,
        run_id: str,
    ) -> BackgroundSubAgentRunRecord | BackgroundSubAgentNotManageable | None:
        """Cancel one run without reaping unrelated workers in the scope."""
        store = PerRunSubAgentRunStore(scope.run_store_dir)
        record = await store.get(run_id)
        if record is None:
            return None
        active = self._active_for_scope(scope)
        handle = active.get(run_id)
        if handle is not None and handle.cancel_task is not None:
            if not handle.cancel_task.done():
                return await asyncio.shield(handle.cancel_task)
        if record.status in TERMINAL_BACKGROUND_RUN_STATUSES:
            return record
        if handle is None:
            return BackgroundSubAgentNotManageable(run_id=run_id)
        handle.cancel_task = asyncio.create_task(
            self._cancel_run(handle, run_id),
        )
        handle.cancel_task.add_done_callback(self._observe_cancel_result)
        return await asyncio.shield(handle.cancel_task)

    async def _cancel_run(
        self,
        handle: _ActiveRun,
        run_id: str,
    ) -> BackgroundSubAgentRunRecord:
        """Keep ownership while blocking process cleanup runs off the loop."""
        await asyncio.to_thread(self._terminate_process_group, handle.process)
        scope = handle.scope
        _remove_private_launch_snapshot(
            str(scope.run_store_dir / f".{run_id}.mcp.json"),
            scope.run_store_dir,
            run_id,
        )
        _remove_private_launch_snapshot(
            str(scope.run_store_dir / f".{run_id}.model.json"),
            scope.run_store_dir,
            run_id,
        )
        record = await PerRunSubAgentRunStore(scope.run_store_dir).cancel(
            run_id,
        )
        self._active_for_scope(scope).pop(run_id, None)
        return record

    @staticmethod
    def _observe_cancel_result(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "background_subagent_cancel_cleanup_failed",
                exc_info=task.exception(),
            )

    def has_active_runs(self, scope: BackgroundSubAgentScope) -> bool:
        """Return whether this process currently manages runs in scope."""
        return bool(self._active_for_scope(scope))

    def is_manageable(
        self,
        scope: BackgroundSubAgentScope,
        run_id: str,
    ) -> bool:
        """Return whether this process has an active handle for a run."""
        return run_id in self._active_for_scope(scope)

    async def _active_records(
        self,
        scope: BackgroundSubAgentScope,
    ) -> list[BackgroundSubAgentRunRecord]:
        store = PerRunSubAgentRunStore(scope.run_store_dir)
        records: list[BackgroundSubAgentRunRecord] = []
        for run_id in sorted(self._active_for_scope(scope)):
            record = await store.get(run_id)
            if (
                record is not None
                and record.status not in TERMINAL_BACKGROUND_RUN_STATUSES
            ):
                records.append(record)
        return records

    async def _reap_scope(
        self,
        scope: BackgroundSubAgentScope,
    ) -> list[BackgroundSubAgentRunRecord]:
        active = self._active_for_scope(scope)
        terminal_runs: list[BackgroundSubAgentRunRecord] = []
        for run_id, handle in list(active.items()):
            record = await self._reap_one(scope, run_id, handle)
            if (
                record is not None
                and record.status in TERMINAL_BACKGROUND_RUN_STATUSES
            ):
                terminal_runs.append(record)
        return terminal_runs

    async def _reap_one(
        self,
        scope: BackgroundSubAgentScope,
        run_id: str,
        handle: _ActiveRun,
    ) -> BackgroundSubAgentRunRecord | None:
        if handle.cancel_task is not None or handle.process.poll() is None:
            return None
        _remove_private_launch_snapshot(
            str(scope.run_store_dir / f".{run_id}.mcp.json"),
            scope.run_store_dir,
            run_id,
        )
        _remove_private_launch_snapshot(
            str(scope.run_store_dir / f".{run_id}.model.json"),
            scope.run_store_dir,
            run_id,
        )
        store = PerRunSubAgentRunStore(scope.run_store_dir)
        try:
            record = await store.mark_worker_exited(
                run_id,
                exit_code=handle.process.returncode,
            )
        except KeyError:
            self._active_for_scope(scope).pop(run_id, None)
            return None
        if record.status not in TERMINAL_BACKGROUND_RUN_STATUSES:
            message = (
                f"{_WORKER_EXITED_WITHOUT_RESULT}: "
                f"exit_code={handle.process.returncode}"
            )
            record = await store.fail(
                run_id,
                message,
                error_code=_WORKER_EXITED_WITHOUT_RESULT,
            )
        self._active_for_scope(scope).pop(run_id, None)
        return record

    @staticmethod
    def _scope_key(scope: BackgroundSubAgentScope) -> tuple[str, str, str]:
        """Use the same physical scope boundary as persisted run records."""
        return (
            scope.tenant_id,
            scope.agent_id,
            str(scope.run_store_dir.resolve()),
        )

    def _active_for_scope(
        self,
        scope: BackgroundSubAgentScope,
    ) -> dict[str, _ActiveRun]:
        return self._active.setdefault(
            self._scope_key(scope),
            {},
        )

    def _start_process(self, command: list[str], stderr_log_path: Path) -> Any:
        stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_handle = stderr_log_path.open("ab")
        try:
            process = self._popen_factory(
                command,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            stderr_handle.close()
        return process

    def _terminate_process_group(self, process: Any) -> None:
        # Capture descendants before terminating the worker: independent Shell
        # sessions stop being discoverable through it once they are orphaned.
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        except (OSError, psutil.Error) as exc:
            logger.warning(
                "background_subagent_descendant_discovery_failed "
                "worker_pid=%s descendant_cleanup=incomplete error=%s",
                process.pid,
                exc,
            )
            descendants = []
        try:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                if hasattr(process, "terminate"):
                    process.terminate()
            try:
                process.wait(timeout=self._cancel_grace_seconds)
                return
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                if hasattr(process, "kill"):
                    process.kill()
            process.wait(timeout=self._cancel_grace_seconds)
        finally:
            self._terminate_descendants(descendants)

    def _terminate_descendants(
        self,
        descendants: list[psutil.Process],
    ) -> None:
        """Reap independent tool processes even when the worker exited first."""
        for child in reversed(descendants):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(
            descendants,
            timeout=self._cancel_grace_seconds,
        )
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(
            alive,
            timeout=self._cancel_grace_seconds,
        )
        if any(self._descendant_is_alive(child) for child in alive):
            raise subprocess.TimeoutExpired(
                "SubAgent descendant cleanup",
                self._cancel_grace_seconds,
            )

    @staticmethod
    def _descendant_is_alive(process: psutil.Process) -> bool:
        try:
            return (
                process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except psutil.NoSuchProcess:
            return False

    def _write_launch_spec(
        self,
        launch_path: Path,
        launch_spec: WorkerLaunchSpec,
    ) -> None:
        launch_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = (
            launch_path.parent / f".{launch_path.name}.{uuid4().hex}.tmp"
        )
        tmp_path.write_text(
            json.dumps(
                launch_spec.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp_path.replace(launch_path)
