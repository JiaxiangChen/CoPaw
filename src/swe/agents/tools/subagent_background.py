# -*- coding: utf-8 -*-
"""Background SubAgent management tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...app.subagents import (
    AgentOwnedDefinitionRepository,
    BackgroundSubAgentNotManageable,
    BackgroundSubAgentScope,
    BackgroundSubAgentStartBlocked,
    BackgroundSubAgentSupervisor,
    BackgroundSubAgentWaitSnapshot,
    DelegationSpec,
    DefinitionMatchMetadata,
    PermissionPolicy,
    SubAgentDefinition,
    SubAgentStartRequest,
    builtin_definition_provider,
    build_definition_catalog,
    load_skill_owned_definitions,
)
from ...config.config import AgentProfileConfig
from ...config.utils import get_tenant_working_dir
from ...runtime_workers import run_runtime_state_work
from ...app.subagents.models import TERMINAL_BACKGROUND_RUN_STATUSES
from ...app.subagents.run_access import (
    read_chat_run_record,
    read_chat_run_records,
)
from ...app.subagents.session_dependencies import (
    initialize_community_expert_dependency_view,
)

_RUN_ID_CONTEXT_KEYS = (
    "subagent_run_id",
    "requested_subagent_run_id",
)
_FAILURE_SUMMARY_MAX_CHARS = 1024
_DEFAULT_SUPERVISOR = BackgroundSubAgentSupervisor()
_SELECTED_EXPERT_POLL_SECONDS = 0.25
_SELECTED_EXPERT_RETRY_LIMIT = 3
_SELECTED_EXPERT_CLEANUP_GRACE_SECONDS = 30
_SELECTED_EXPERT_OPERATION_TIMEOUT_SECONDS = 5
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BackgroundSubagentToolContext:
    """Shared state captured by Background SubAgent tool closures."""

    supervisor: BackgroundSubAgentSupervisor
    parent_agent_config: AgentProfileConfig
    workspace_dir: Path
    request_context: dict[str, Any]
    tool_scope: BackgroundSubAgentScope
    definition_catalog: Any
    effective_skill_names: list[str] | None
    skill_snapshot_signatures: dict[str, str] | None
    skill_snapshot_dirs: Mapping[str, Path] | None
    selected_expert_id: str | None


async def _wake_goal_after_subagent(
    *,
    supervisor: BackgroundSubAgentSupervisor,
    scope: BackgroundSubAgentScope,
    goal_id: str,
    run_id: str,
) -> None:
    """Bridge a terminal Background SubAgent result into Goal wake-up."""
    from ...app.goals.registry import get_goal_service

    terminal = await supervisor.wait_for_run(scope, run_id)
    if terminal is None or terminal.status not in {"completed", "partial"}:
        return
    service = get_goal_service()
    if service is not None:
        goal = await service.get(goal_id)
        if getattr(getattr(goal, "state", None), "value", None) != "WAITING":
            return
        await service.wake(goal_id, "Background SubAgent completed")


_START_SUBAGENT_DESCRIPTION = (
    "Start a Background SubAgent Run. Use an exact listed name to reuse a "
    "definition, including its Skill qualifier when present. For a temporary "
    "worker, provide a new name, instruction, and objective; it is not saved "
    "as an expert. A listed definition keeps its configured instruction. "
    "Tool availability does not require delegation; follow the user's request."
)


def get_default_background_subagent_supervisor() -> (
    BackgroundSubAgentSupervisor
):
    """Return the process-local default Background SubAgent supervisor."""
    return _DEFAULT_SUPERVISOR


def has_explicit_subagent_run_id(request_context: dict[str, Any]) -> bool:
    """Return whether this request explicitly carries a Background Run id."""
    return any(
        str(request_context.get(key) or "").strip()
        for key in _RUN_ID_CONTEXT_KEYS
    )


def build_background_subagent_scope(
    *,
    parent_agent_config: AgentProfileConfig,
    request_context: dict[str, Any],
) -> BackgroundSubAgentScope:
    """Build the current tenant-and-agent Background SubAgent scope."""
    tenant_id = str(request_context.get("tenant_id") or "default")
    agent_id = str(
        request_context.get("agent_id") or parent_agent_config.id or "default",
    )
    explicit_run_store_dir = request_context.get("_subagent_run_store_dir")
    if explicit_run_store_dir:
        run_store_dir = Path(explicit_run_store_dir)
    else:
        run_store_dir = (
            get_tenant_working_dir(tenant_id)
            / "workspaces"
            / agent_id
            / "subagent_runs"
        )
    return BackgroundSubAgentScope(
        tenant_id=tenant_id,
        agent_id=agent_id,
        run_store_dir=run_store_dir,
    )


def _parse_subagent_start_request(
    name: str | None,
    instruction: str | None,
    objective: str | None,
    background: str,
    extra: dict[str, Any],
) -> SubAgentStartRequest:
    if extra:
        raise ValueError("unexpected fields: " + ", ".join(sorted(extra)))
    return SubAgentStartRequest.model_validate(
        {
            "name": name,
            "instruction": instruction,
            "objective": objective,
            "background": background,
        },
    )


def _resolve_subagent_start_definition(
    *,
    start_request: SubAgentStartRequest,
    definition_catalog: Any,
    selected_expert_id: str | None,
    tool_scope: BackgroundSubAgentScope,
) -> tuple[
    SubAgentDefinition | None,
    DefinitionMatchMetadata | None,
    ToolResponse | None,
]:
    definition = (
        definition_catalog.resolve_exact(start_request.name)
        if definition_catalog is not None
        else None
    )
    if selected_expert_id and definition is None:
        return (
            None,
            None,
            _json_response(
                {
                    "status": "not_found",
                    "reason": "selected_expert_not_available",
                    "name": start_request.name,
                    "selected_expert_id": selected_expert_id,
                },
            ),
        )
    if selected_expert_id and start_request.name != definition.name:
        return (
            None,
            None,
            _json_response(
                {
                    "status": "failed",
                    "reason": "selected_expert_name_mismatch",
                    "name": start_request.name,
                    "selected_expert_name": definition.name,
                },
            ),
        )
    if definition is not None:
        return (
            definition,
            DefinitionMatchMetadata(
                matched=True,
                definition_name=definition.name,
                definition_source=definition.source,
                score=1.0,
                reason="exact_name",
            ),
            None,
        )
    if start_request.instruction is None:
        return (
            None,
            None,
            _json_response(
                {
                    "status": "not_found",
                    "reason": "subagent_definition_not_found",
                    "name": start_request.name,
                },
            ),
        )
    return (
        SubAgentDefinition(
            name=start_request.name,
            source="run_scoped",
            owner_scope=f"run:{tool_scope.tenant_id}:{tool_scope.agent_id}",
            description="Temporary caller-defined SubAgent.",
            instruction=start_request.instruction,
        ),
        DefinitionMatchMetadata(matched=False, reason="run_scoped"),
        None,
    )


async def _link_goal_subagent_run(
    *,
    goal_id: str,
    result: Any,
    request_context: dict[str, Any],
    supervisor: BackgroundSubAgentSupervisor,
    scope: BackgroundSubAgentScope,
) -> None:
    run_id = getattr(result, "run_id", None)
    if not goal_id or not run_id:
        return
    request_context["goal_subagent_run_ids"].append(run_id)
    from ...app.goals.registry import get_goal_service

    goal_service = get_goal_service()
    if goal_service is not None:
        await goal_service.link_subagent(goal_id, run_id)
    asyncio.create_task(
        _wake_goal_after_subagent(
            supervisor=supervisor,
            scope=scope,
            goal_id=goal_id,
            run_id=run_id,
        ),
    )


def _effective_subagent_skills(context, definition):
    if (
        definition.skill_owned is not None
        or definition.agent_owned is not None
    ):
        return list(context.effective_skill_names or [])
    return []


def _create_start_subagent_tool(
    context: _BackgroundSubagentToolContext,
    directory: str,
) -> Callable[..., Any]:
    """Create the start tool bound to one Background SubAgent scope."""

    async def start_subagent(
        name: str | None = None,
        instruction: str | None = None,
        objective: str | None = None,
        background: str = "",
        **extra: Any,
    ) -> ToolResponse:
        """Start a Background SubAgent Run and return its run identity."""
        try:
            start_request = _parse_subagent_start_request(
                name,
                instruction,
                objective,
                background,
                extra,
            )
        except Exception as exc:
            return _json_response(
                {
                    "status": "failed",
                    "reason": "invalid_request",
                    "message": str(exc),
                },
            )
        try:
            definition, definition_match, failure = (
                _resolve_subagent_start_definition(
                    start_request=start_request,
                    definition_catalog=context.definition_catalog,
                    selected_expert_id=context.selected_expert_id,
                    tool_scope=context.tool_scope,
                )
            )
            if failure is not None:
                return failure
            start_context = dict(context.request_context)
            start_context.pop("_expert_dependency_view_root", None)
            dependency_view = await run_runtime_state_work(
                initialize_community_expert_dependency_view,
                workspace_dir=context.workspace_dir,
                chat_id=str(start_context.get("chat_id") or ""),
                definition=definition,
            )
            if dependency_view is not None:
                start_context["_expert_dependency_view_root"] = str(
                    dependency_view,
                )
            spec = DelegationSpec(
                parent_thread_id=str(
                    context.request_context.get("session_id") or "",
                ),
                parent_chat_id=str(
                    context.request_context.get("chat_id") or "",
                ),
                parent_msgid=str(
                    context.request_context.get("msgid") or "",
                ),
                goal_id=str(context.request_context.get("goal_id") or ""),
                name=start_request.name,
                objective=start_request.objective,
                background=start_request.background,
            )
            goal_id = str(context.request_context.get("goal_id") or "").strip()
            if goal_id:
                context.request_context.setdefault("goal_subagent_run_ids", [])
            result = await context.supervisor.start(
                scope=context.tool_scope,
                spec=spec,
                parent_agent_config=context.parent_agent_config,
                workspace_dir=context.workspace_dir,
                parent_policy=_parent_policy_from_config(
                    context.parent_agent_config,
                ),
                runtime_policy=(
                    PermissionPolicy.readonly()
                    if context.request_context.get("plan_mode_enabled") is True
                    else None
                ),
                request_context=start_context,
                definition=definition,
                start_request=start_request,
                definition_match=definition_match,
                effective_skill_names=_effective_subagent_skills(
                    context,
                    definition,
                ),
                skill_snapshot_signatures=context.skill_snapshot_signatures,
                skill_snapshot_dirs=context.skill_snapshot_dirs,
            )
            if context.request_context.get("selected_expert_execution"):
                run_id = getattr(result, "run_id", None)
                if run_id:
                    context.request_context["selected_expert_run_id"] = run_id
            await _link_goal_subagent_run(
                goal_id=goal_id,
                result=result,
                request_context=context.request_context,
                supervisor=context.supervisor,
                scope=context.tool_scope,
            )
        except Exception as exc:
            return _json_response(
                {
                    "status": "failed",
                    "reason": "invalid_request",
                    "message": str(exc),
                },
            )
        return _json_response(_serialize_start_result(result))

    start_subagent.__doc__ = directory
    return start_subagent


def _create_wait_subagent_tool(
    context: _BackgroundSubagentToolContext,
) -> Callable[..., Any]:
    """Create the wait tool bound to one Background SubAgent scope."""

    async def wait_subagent(timeout_ms: int = 3000) -> ToolResponse:
        """Wait briefly and return current Background SubAgent statuses."""
        snapshot = await _wait_for_chat_runs(context, timeout_ms)
        return _json_response(_serialize_wait_snapshot(snapshot))

    return wait_subagent


def _create_selected_expert_wait_tool(
    context: _BackgroundSubagentToolContext,
) -> Callable[..., Any]:
    async def wait_selected_expert() -> ToolResponse:
        """Wait for the selected expert run belonging to this request."""
        run_id = _selected_expert_wait_run_id(context)
        if not run_id:
            return _json_response({"status": "unavailable"})
        authorized = False
        failures = 0
        request_deadline = context.request_context.get(
            "_selected_expert_request_deadline",
        )
        if isinstance(request_deadline, (int, float)):
            request_deadline = float(request_deadline)
        else:
            request_deadline = None
        deadline = request_deadline
        budget_deadline_set = False
        try:
            while True:
                (
                    record,
                    deadline,
                    budget_deadline_set,
                    category,
                ) = await _poll_selected_expert_record(
                    context,
                    run_id,
                    deadline=deadline,
                    request_deadline=request_deadline,
                    budget_deadline_set=budget_deadline_set,
                )
                authorized = authorized or record is not None
                if record is not None and record.status in (
                    TERMINAL_BACKGROUND_RUN_STATUSES
                ):
                    return _json_response(
                        _parent_facing_record(record, include_result=True),
                    )
                if category == "deadline" or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    return await _selected_expert_deadline_response(
                        context,
                        run_id,
                        category,
                        authorized=authorized,
                    )
                if record is not None:
                    assert deadline is not None
                    category = await _reap_selected_expert_run(
                        context,
                        run_id,
                        deadline,
                    )
                failures = _selected_expert_failure_count(failures, category)
                if failures >= _SELECTED_EXPERT_RETRY_LIMIT:
                    _log_selected_expert_unconfirmed(context, run_id, category)
                    return _json_response({"status": "unavailable"})
                await asyncio.sleep(_selected_expert_poll_delay(deadline))
        except asyncio.CancelledError:
            if authorized:
                await _cancel_selected_expert(context, run_id)
            raise

    return wait_selected_expert


def _selected_expert_wait_run_id(
    context: _BackgroundSubagentToolContext,
) -> str:
    if not context.request_context.pop(
        "_selected_expert_wait_authorized",
        False,
    ):
        return ""
    run_id = str(
        context.request_context.get("selected_expert_run_id") or "",
    ).strip()
    if not run_id or not context.request_context.get(
        "selected_expert_execution",
    ):
        return ""
    return run_id


def _selected_expert_failure_count(failures, category):
    return failures + 1 if category is not None else 0


def _selected_expert_poll_delay(deadline):
    if deadline is None:
        return _SELECTED_EXPERT_POLL_SECONDS
    return min(
        _SELECTED_EXPERT_POLL_SECONDS,
        max(deadline - time.monotonic(), 0),
    )


async def _poll_selected_expert_record(
    context: _BackgroundSubagentToolContext,
    run_id: str,
    *,
    deadline: float | None,
    request_deadline: float | None,
    budget_deadline_set: bool,
) -> tuple[Any, float | None, bool, str | None]:
    """Read one owned run and derive its fixed wait deadline."""
    record = None
    try:
        record = await _read_selected_expert_record(
            context,
            run_id,
            deadline=deadline,
        )
        if record is None:
            return None, deadline, budget_deadline_set, "record_unavailable"
        if record.status in TERMINAL_BACKGROUND_RUN_STATUSES:
            return record, deadline, budget_deadline_set, None
        if not budget_deadline_set:
            remaining = (
                record.effective_budget.timeout_ms / 1000
                + _SELECTED_EXPERT_CLEANUP_GRACE_SECONDS
                - (
                    datetime.now(timezone.utc) - record.created_at
                ).total_seconds()
            )
            budget_deadline = time.monotonic() + max(remaining, 0)
            if request_deadline is not None:
                budget_deadline = min(budget_deadline, request_deadline)
            deadline = budget_deadline
            budget_deadline_set = True
        assert deadline is not None
        if time.monotonic() >= deadline:
            return record, deadline, budget_deadline_set, "deadline"
        return record, deadline, budget_deadline_set, None
    except Exception as exc:
        return record, deadline, budget_deadline_set, type(exc).__name__


async def _reap_selected_expert_run(context, run_id, deadline):
    """Reap after the caller has recorded the run's authorization."""
    try:
        timeout = min(
            _SELECTED_EXPERT_OPERATION_TIMEOUT_SECONDS,
            max(deadline - time.monotonic(), 0),
        )
        await asyncio.wait_for(
            context.supervisor.reap_run(context.tool_scope, run_id),
            timeout=timeout,
        )
        return None
    except Exception as exc:
        return type(exc).__name__


async def _selected_expert_deadline_response(
    context,
    run_id,
    category,
    *,
    authorized,
):
    if not authorized:
        _log_selected_expert_unconfirmed(context, run_id, category)
        return _json_response({"status": "unavailable"})
    terminal = await _cancel_selected_expert(context, run_id)
    return _json_response(
        (
            _parent_facing_record(terminal, include_result=True)
            if terminal is not None
            else {"status": "unavailable"}
        ),
    )


async def _read_selected_expert_record(context, run_id, *, deadline=None):
    timeout = _SELECTED_EXPERT_OPERATION_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, max(deadline - time.monotonic(), 0.001))
    return await asyncio.wait_for(
        run_runtime_state_work(
            read_chat_run_record,
            context.tool_scope.run_store_dir,
            context.request_context,
            run_id,
            strict=True,
        ),
        timeout=timeout,
    )


def _log_selected_expert_unconfirmed(context, run_id, category):
    logger.warning(
        "selected_expert_wait_unconfirmed tenant_id=%s agent_id=%s "
        "run_id=%s category=%s",
        context.tool_scope.tenant_id,
        context.tool_scope.agent_id,
        run_id,
        category,
    )


async def _cancel_selected_expert(context, run_id):
    try:
        cancel = getattr(context.supervisor, "cancel_run", None)
        if cancel is None:
            cancel = context.supervisor.cancel
        result = await asyncio.wait_for(
            cancel(context.tool_scope, run_id),
            timeout=_SELECTED_EXPERT_OPERATION_TIMEOUT_SECONDS,
        )
        if (
            getattr(result, "run_id", None) == run_id
            and getattr(result, "status", None)
            in TERMINAL_BACKGROUND_RUN_STATUSES
        ):
            return result
        # Natural completion may win a race with remote/unmanageable cancel.
        record = await _read_selected_expert_record(context, run_id)
        if (
            record is not None
            and record.status in TERMINAL_BACKGROUND_RUN_STATUSES
        ):
            return record
        category = getattr(result, "status", "cancel_unconfirmed")
    except Exception as exc:
        category = type(exc).__name__
    _log_selected_expert_unconfirmed(context, run_id, category)
    return None


async def _wait_for_chat_runs(
    context: _BackgroundSubagentToolContext,
    timeout_ms: int,
) -> BackgroundSubAgentWaitSnapshot:
    """Reap processes but project results from retained Chat-owned records."""
    if not str(context.request_context.get("session_id") or "").strip():
        return BackgroundSubAgentWaitSnapshot()
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while True:
        await context.supervisor.wait(context.tool_scope, timeout_ms=0)
        records = await run_runtime_state_work(
            read_chat_run_records,
            context.tool_scope.run_store_dir,
            context.request_context,
        )
        terminal = [
            record
            for record in records
            if record.status in TERMINAL_BACKGROUND_RUN_STATUSES
        ]
        active = [
            record
            for record in records
            if record.status not in TERMINAL_BACKGROUND_RUN_STATUSES
        ]
        if terminal or not active or time.monotonic() >= deadline:
            return BackgroundSubAgentWaitSnapshot(
                active_runs=active,
                terminal_runs=terminal,
                timed_out=bool(active and not terminal),
            )
        await asyncio.sleep(min(0.05, max(deadline - time.monotonic(), 0)))


async def _has_chat_run_access(
    context: _BackgroundSubagentToolContext,
    run_id: str,
) -> bool:
    return (
        await run_runtime_state_work(
            read_chat_run_record,
            context.tool_scope.run_store_dir,
            context.request_context,
            run_id,
        )
        is not None
    )


def _create_get_subagent_tool(
    context: _BackgroundSubagentToolContext,
) -> Callable[..., Any]:
    """Create the get tool bound to one Background SubAgent scope."""

    async def get_subagent(
        run_id: str,
        include_details: bool = False,
    ) -> ToolResponse:
        """Fetch one Background SubAgent Run in the current scope."""
        if not await _has_chat_run_access(context, run_id):
            return _json_response({"status": "not_found", "run_id": run_id})
        try:
            record = await context.supervisor.get(
                context.tool_scope,
                run_id,
            )
        except ValueError:
            return _json_response({"status": "not_found", "run_id": run_id})
        if record is None:
            return _json_response({"status": "not_found", "run_id": run_id})
        return _json_response(
            _compact_record(
                record,
                include_details,
                manageable=_is_manageable(
                    context.supervisor,
                    context.tool_scope,
                    run_id,
                ),
                run_store_dir=context.tool_scope.run_store_dir,
            ),
        )

    return get_subagent


def _create_cancel_subagent_tool(
    context: _BackgroundSubagentToolContext,
) -> Callable[..., Any]:
    """Create the cancel tool bound to one Background SubAgent scope."""

    async def cancel_subagent(run_id: str) -> ToolResponse:
        """Cancel one active Background SubAgent Run in the current scope."""
        if not await _has_chat_run_access(context, run_id):
            return _json_response({"status": "not_found", "run_id": run_id})
        try:
            result = await context.supervisor.cancel(
                context.tool_scope,
                run_id,
            )
        except ValueError:
            return _json_response({"status": "not_found", "run_id": run_id})
        if result is None:
            return _json_response({"status": "not_found", "run_id": run_id})
        if isinstance(result, BackgroundSubAgentNotManageable):
            return _json_response(result.model_dump(mode="json"))
        return _json_response(
            _compact_record(
                result,
                include_details=False,
                manageable=False,
                run_store_dir=context.tool_scope.run_store_dir,
            ),
        )

    return cancel_subagent


def create_background_subagent_tools(
    *,
    supervisor: BackgroundSubAgentSupervisor,
    parent_agent_config: AgentProfileConfig,
    workspace_dir: Path,
    request_context: dict[str, Any],
    effective_skill_names: list[str] | None = None,
    skill_snapshot_signatures: dict[str, str] | None = None,
    skill_snapshot_dirs: Mapping[str, Path] | None = None,
    selected_expert_id: str | None = None,
    expert_discovery_enabled: bool = False,
    start_enabled: bool = True,
) -> dict[str, Callable[..., Any]]:
    """Create start/wait/get/cancel Background SubAgent tool callables."""
    tool_scope = build_background_subagent_scope(
        parent_agent_config=parent_agent_config,
        request_context=request_context,
    )
    definition_catalog = (
        _build_definition_catalog(
            tool_scope=tool_scope,
            workspace_dir=workspace_dir,
            effective_skill_names=effective_skill_names,
            skill_snapshot_dirs=skill_snapshot_dirs,
            selected_expert_id=selected_expert_id,
            expert_discovery_enabled=expert_discovery_enabled,
        )
        if start_enabled
        else None
    )
    directory = _format_skill_definition_directory(definition_catalog)
    context = _BackgroundSubagentToolContext(
        supervisor=supervisor,
        parent_agent_config=parent_agent_config,
        workspace_dir=workspace_dir,
        request_context=request_context,
        tool_scope=tool_scope,
        definition_catalog=definition_catalog,
        effective_skill_names=effective_skill_names,
        skill_snapshot_signatures=skill_snapshot_signatures,
        skill_snapshot_dirs=skill_snapshot_dirs,
        selected_expert_id=selected_expert_id,
    )

    tools: dict[str, Callable[..., Any]] = {
        "wait_subagent": _create_wait_subagent_tool(context),
        "get_subagent": _create_get_subagent_tool(context),
        "cancel_subagent": _create_cancel_subagent_tool(context),
    }
    if start_enabled:
        tools["start_subagent"] = _create_start_subagent_tool(
            context,
            directory,
        )
    if selected_expert_id:
        tools["wait_selected_expert"] = _create_selected_expert_wait_tool(
            context,
        )
    return tools


def _build_definition_catalog(
    *,
    tool_scope: BackgroundSubAgentScope,
    workspace_dir: Path,
    effective_skill_names: list[str] | None,
    skill_snapshot_dirs: Mapping[str, Path] | None = None,
    selected_expert_id: str | None = None,
    expert_discovery_enabled: bool = False,
):
    """Build the catalog only for an explicit delegation-intent turn."""
    builtin_definitions = builtin_definition_provider().list_definitions()
    agent_packages = AgentOwnedDefinitionRepository(
        workspace_dir / "agents",
        owner_scope=f"{tool_scope.tenant_id}/{tool_scope.agent_id}",
        builtin_names={definition.name for definition in builtin_definitions},
    ).list()
    if selected_expert_id:
        selected_package = next(
            (
                package
                for package in agent_packages
                if package.definition_id == selected_expert_id
                and package.definition is not None
                and package.definition.enabled
            ),
            None,
        )
        return build_definition_catalog(
            skill_definitions=[],
            builtin_definitions=[],
            agent_owned_definitions=(
                [selected_package.definition]
                if selected_package is not None
                else []
            ),
        )
    return build_definition_catalog(
        skill_definitions=load_skill_owned_definitions(
            workspace_dir=workspace_dir,
            effective_skill_names=effective_skill_names or [],
            skill_snapshot_dirs=skill_snapshot_dirs,
        ).definitions,
        builtin_definitions=builtin_definitions,
        agent_owned_definitions=[
            package.definition
            for package in agent_packages
            if expert_discovery_enabled and package.definition is not None
        ],
    )


def _format_skill_definition_directory(catalog) -> str:
    """Build the bounded Definition directory included in tool metadata."""
    definitions = catalog.list_definitions() if catalog is not None else []
    if not definitions:
        return _START_SUBAGENT_DESCRIPTION
    lines = [_START_SUBAGENT_DESCRIPTION, "Available definitions:"]
    for definition in definitions:
        keywords = ", ".join(definition.trigger_keywords) or "(none)"
        lines.append(
            f"- {definition.name}: {definition.description} "
            f"[keywords: {keywords}]",
        )
    return "\n".join(lines)


def _json_response(payload: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            ),
        ],
    )


def _serialize_start_result(
    result: Any,
) -> dict[str, Any]:
    if isinstance(result, BackgroundSubAgentStartBlocked):
        payload = result.model_dump(mode="json")
        payload["accepted"] = False
        return payload
    return {
        **_parent_facing_record(result, include_result=False),
        "accepted": True,
    }


def _serialize_wait_snapshot(
    snapshot: BackgroundSubAgentWaitSnapshot,
) -> dict[str, Any]:
    return {
        "timed_out": snapshot.timed_out,
        "active_runs": [
            _parent_facing_record(record, include_result=False)
            for record in snapshot.active_runs
        ],
        "terminal_runs": [
            _parent_facing_record(record, include_result=True)
            for record in snapshot.terminal_runs
        ],
    }


def _parent_facing_record(
    record: Any,
    *,
    include_result: bool,
) -> dict[str, Any]:
    payload = {
        "run_id": record.run_id,
        "status": record.status,
        "agent_name": record.spec.name,
        "nickname": getattr(record, "nickname", None),
        "objective": record.spec.objective,
    }
    result = getattr(record, "result", None)
    if include_result and result is not None:
        payload["result"] = _compact_agent_result(result)
    elif include_result:
        failure_result = _compact_failure_without_result(record)
        if failure_result is not None:
            payload["result"] = failure_result
    return payload


def _compact_failure_without_result(record: Any) -> dict[str, str] | None:
    if getattr(record, "status", None) != "failed":
        return None
    errors = getattr(record, "errors", []) or []
    if not errors:
        return None
    error = errors[-1]
    code = str(getattr(error, "code", "") or "").strip()
    message = str(getattr(error, "message", "") or "").strip()
    if code and message.startswith(f"{code}:"):
        summary = message
    else:
        summary = ": ".join(part for part in (code, message) if part)
    if not summary:
        return None
    return {
        "status": "failed",
        "summary": summary[:_FAILURE_SUMMARY_MAX_CHARS],
    }


def _compact_agent_result(result: Any) -> dict[str, Any]:
    payload = {"summary": result.summary}
    errors = getattr(result, "errors", []) or []
    if any(
        getattr(error, "code", "") == "text_finalization_failed"
        for error in errors
    ):
        payload["error_code"] = "text_finalization_failed"
    return payload


def _compact_record(
    record: Any,
    include_details: bool,
    manageable: bool = False,
    run_store_dir: Path | None = None,
) -> dict[str, Any]:
    payload = _parent_facing_record(record, include_result=True)
    stderr_tail = _stderr_tail(record, run_store_dir)
    if include_details:
        payload.update(
            {
                "definition_match": _dump_json_value(
                    getattr(record, "definition_match", None),
                ),
                "created_at": _dump_json_value(
                    getattr(record, "created_at", None),
                ),
                "started_at": _dump_json_value(
                    getattr(record, "started_at", None),
                ),
                "finished_at": _dump_json_value(
                    getattr(record, "finished_at", None),
                ),
                "errors": _dump_json_value(getattr(record, "errors", [])),
                "worker": _compact_worker(
                    getattr(record, "worker", None),
                ),
                "launch_diagnostics": _safe_launch_diagnostics(
                    getattr(record, "launch_diagnostics", {}),
                ),
                "manageable": manageable,
            },
        )
        if stderr_tail is not None:
            payload["stderr_tail"] = stderr_tail
        payload["delegation_spec"] = record.spec.model_dump(mode="json")
        payload["effective_policy"] = record.effective_policy.model_dump(
            mode="json",
        )
    return payload


def _compact_worker(worker: Any) -> dict[str, Any] | None:
    if worker is None:
        return None
    return {
        "pid": getattr(worker, "pid", None),
        "started_at": _dump_json_value(getattr(worker, "started_at", None)),
        "exit_code": getattr(worker, "exit_code", None),
        "exited_at": _dump_json_value(getattr(worker, "exited_at", None)),
    }


def _safe_launch_diagnostics(value: Any) -> dict[str, Any]:
    """Return the allowlisted launch diagnostics for detailed inspection."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for field in (
        "loaded_skills",
        "skipped_skills",
        "snapshotted_mcps",
        "connected_mcps",
        "skipped_mcps",
        "resolved_model",
    ):
        if field in value and value[field] not in (None, [], {}):
            result[field] = _dump_json_value(value[field])
    return result


def _parent_policy_from_config(parent_agent_config: Any) -> PermissionPolicy:
    """Build a bounded parent policy from enabled parent built-in tools."""
    from ...app.subagents.models import KNOWN_BUILTIN_TOOLS, MutationPolicy
    from ...config.config import _default_builtin_tools

    builtin_defaults = _default_builtin_tools()
    tools_config = getattr(parent_agent_config, "tools", None)
    builtin_tools = getattr(tools_config, "builtin_tools", None)
    if not isinstance(builtin_tools, dict):
        builtin_tools = builtin_defaults
    enabled = {
        name
        for name, config in builtin_tools.items()
        if name in KNOWN_BUILTIN_TOOLS and getattr(config, "enabled", True)
    }
    return PermissionPolicy.bounded(
        allow_tools=sorted(enabled),
        deny_tools=sorted(KNOWN_BUILTIN_TOOLS - enabled),
        mutation=MutationPolicy(
            allow_file_write="write_file" in enabled,
            allow_patch="edit_file" in enabled,
        ),
    )


def _is_manageable(
    supervisor: BackgroundSubAgentSupervisor,
    scope: BackgroundSubAgentScope,
    run_id: str,
) -> bool:
    checker = getattr(supervisor, "is_manageable", None)
    if checker is None:
        return False
    return bool(checker(scope, run_id))


def _stderr_tail(record: Any, run_store_dir: Path | None) -> str | None:
    if getattr(record, "status", "") not in {"failed", "cancelled"}:
        return None
    worker = getattr(record, "worker", None)
    stderr_log_path = getattr(worker, "stderr_log_path", None)
    if not stderr_log_path or run_store_dir is None:
        return None
    path = Path(stderr_log_path)
    try:
        path.resolve().relative_to(run_store_dir.resolve())
    except (OSError, ValueError):
        return None
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")[-4096:]


def _dump_json_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_dump_json_value(item) for item in value]
    return value
