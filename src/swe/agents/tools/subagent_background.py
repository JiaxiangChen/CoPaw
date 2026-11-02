# -*- coding: utf-8 -*-
"""Background SubAgent management tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...app.subagents import (
    AgentOwnedDefinitionRepository,
    BackgroundSubAgentNotManageable,
    BackgroundSubAgentRunRecord,
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
    read_chat_run_by_nickname,
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
_DEFAULT_SUPERVISOR = BackgroundSubAgentSupervisor()


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
    "Start a listed Background SubAgent expert with its exact expert_name, "
    "including a Skill qualifier when present. The returned nickname is the "
    "Chat-local handle for later get, wait, or cancel operations."
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
    expert_name: str,
    objective: str,
    background: str,
) -> SubAgentStartRequest:
    return SubAgentStartRequest.model_validate(
        {
            "name": expert_name,
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
    definition = definition_catalog.resolve_exact(start_request.name)
    if selected_expert_id and definition is None:
        return (
            None,
            None,
            _json_response(
                {
                    "status": "not_found",
                    "reason": "selected_expert_not_available",
                    "expert_name": start_request.name,
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
                    "expert_name": start_request.name,
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
    return (
        None,
        None,
        _json_response(
            {
                "status": "not_found",
                "reason": "subagent_definition_not_found",
                "expert_name": start_request.name,
            },
        ),
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
        expert_name: str,
        objective: str,
        background: str = "",
    ) -> ToolResponse:
        """Start one listed expert and return its Chat nickname."""
        try:
            start_request = _parse_subagent_start_request(
                expert_name,
                objective,
                background,
            )
        except Exception as exc:
            return _json_response(
                {
                    "status": "failed",
                    "reason": "invalid_request",
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
                },
            )
        return _json_response(await _serialize_start_result(context, result))

    start_subagent.__doc__ = directory
    return start_subagent


def _create_wait_subagent_tool(
    context: _BackgroundSubagentToolContext,
) -> Callable[..., Any]:
    """Create the wait tool bound to one Background SubAgent scope."""

    async def wait_subagent(
        nicknames: list[str] | None = None,
        timeout_ms: int = 3000,
    ) -> ToolResponse:
        """Wait for all named SubAgents, or monitor the Chat when omitted."""
        try:
            if nicknames is None:
                snapshot = await _wait_for_chat_runs(context, timeout_ms)
                return _json_response(_serialize_wait_snapshot(snapshot))
            if not nicknames:
                return _json_response(
                    {
                        "status": "invalid_request",
                        "reason": "nicknames_must_not_be_empty",
                    },
                )
            targets = {}
            for nickname in nicknames:
                record = await _resolve_chat_nickname(context, nickname)
                if record is None:
                    return _json_response(
                        {"status": "not_found", "nickname": nickname},
                    )
                targets[record.run_id] = record
            return _json_response(
                await _wait_for_target_runs(context, targets, timeout_ms),
            )
        except Exception:
            return _json_response(
                {
                    "status": "failed",
                    "reason": "subagent_operation_failed",
                },
            )

    return wait_subagent


async def _reload_target_records(
    context: _BackgroundSubagentToolContext,
    targets: dict[str, BackgroundSubAgentRunRecord],
) -> list[BackgroundSubAgentRunRecord]:
    """Refresh fixed, previously authorized run identities only."""
    for run_id in targets:
        await context.supervisor.reap_run(context.tool_scope, run_id)
        record = await run_runtime_state_work(
            read_chat_run_record,
            context.tool_scope.run_store_dir,
            context.request_context,
            run_id,
        )
        if record is not None:
            targets[run_id] = record
    return list(targets.values())


async def _wait_for_target_runs(
    context: _BackgroundSubagentToolContext,
    targets: dict[str, BackgroundSubAgentRunRecord],
    timeout_ms: int,
) -> dict[str, Any]:
    """Wait until every fixed target is terminal or the deadline expires."""
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while True:
        records = await _reload_target_records(context, targets)
        complete = all(
            record.status in TERMINAL_BACKGROUND_RUN_STATUSES
            for record in records
        )
        remaining = deadline - time.monotonic()
        if complete or remaining <= 0:
            return {
                "timed_out": not complete,
                "subagents": [
                    _model_subagent_record(
                        record,
                        include_result=(
                            record.status in TERMINAL_BACKGROUND_RUN_STATUSES
                        ),
                    )
                    for record in records
                ],
            }
        await asyncio.sleep(min(0.05, remaining))


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


async def _resolve_chat_nickname(
    context: _BackgroundSubagentToolContext,
    nickname: str,
) -> Any | None:
    if not isinstance(nickname, str) or not nickname.strip():
        return None
    return await run_runtime_state_work(
        read_chat_run_by_nickname,
        context.tool_scope.run_store_dir,
        context.request_context,
        nickname,
    )


def _create_get_subagent_tool(
    context: _BackgroundSubagentToolContext,
) -> Callable[..., Any]:
    """Create the get tool bound to one Background SubAgent scope."""

    async def get_subagent(
        nickname: str,
        include_details: bool = False,
    ) -> ToolResponse:
        """Fetch one Background SubAgent Run in the current scope."""
        try:
            record = await _resolve_chat_nickname(context, nickname)
            if record is None:
                return _json_response(
                    {"status": "not_found", "nickname": nickname},
                )
            current = await context.supervisor.get(
                context.tool_scope,
                record.run_id,
            )
        except ValueError:
            return _json_response(
                {"status": "not_found", "nickname": nickname},
            )
        except Exception:
            return _json_response(_operation_failed_response(nickname))
        if current is None:
            return _json_response(
                {"status": "not_found", "nickname": nickname},
            )
        return _json_response(
            _model_subagent_record(current, include_result=True),
        )

    return get_subagent


def _create_cancel_subagent_tool(
    context: _BackgroundSubagentToolContext,
) -> Callable[..., Any]:
    """Create the cancel tool bound to one Background SubAgent scope."""

    async def cancel_subagent(nickname: str) -> ToolResponse:
        """Cancel one active Background SubAgent Run in the current scope."""
        try:
            record = await _resolve_chat_nickname(context, nickname)
            if record is None:
                return _json_response(
                    {"status": "not_found", "nickname": nickname},
                )
            result = await context.supervisor.cancel(
                context.tool_scope,
                record.run_id,
            )
        except ValueError:
            return _json_response(
                {"status": "not_found", "nickname": nickname},
            )
        except Exception:
            return _json_response(_operation_failed_response(nickname))
        if result is None:
            return _json_response(
                {"status": "not_found", "nickname": nickname},
            )
        if isinstance(result, BackgroundSubAgentNotManageable):
            return _json_response(
                {
                    **_model_subagent_record(record, include_result=True),
                    "manageable": False,
                },
            )
        return _json_response(
            _model_subagent_record(result, include_result=True),
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


def _operation_failed_response(nickname: str) -> dict[str, str]:
    return {
        "status": "failed",
        "reason": "subagent_operation_failed",
        "nickname": nickname,
    }


async def _serialize_start_result(
    context: _BackgroundSubagentToolContext,
    result: Any,
) -> dict[str, Any]:
    if isinstance(result, BackgroundSubAgentStartBlocked):
        active_nicknames = []
        for run_id in result.active_run_ids:
            record = await run_runtime_state_work(
                read_chat_run_record,
                context.tool_scope.run_store_dir,
                context.request_context,
                run_id,
            )
            if record is not None and record.nickname:
                active_nicknames.append(record.nickname)
        return {
            "status": result.status,
            "reason": result.reason,
            "limit": result.limit,
            "active_nicknames": active_nicknames,
            "accepted": False,
        }
    return {
        **_model_subagent_record(result, include_result=False),
        "accepted": True,
    }


def _serialize_wait_snapshot(
    snapshot: BackgroundSubAgentWaitSnapshot,
) -> dict[str, Any]:
    return {
        "timed_out": snapshot.timed_out,
        "active_subagents": [
            _model_subagent_record(record, include_result=False)
            for record in snapshot.active_runs
        ],
        "terminal_subagents": [
            _model_subagent_record(record, include_result=True)
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


def _model_subagent_record(
    record: Any,
    *,
    include_result: bool,
) -> dict[str, Any]:
    payload = {
        "expert_name": record.spec.name,
        "nickname": getattr(record, "nickname", None),
        "status": record.status,
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
    return {
        "status": "failed",
        "summary": "SubAgent execution failed.",
    }


def _compact_agent_result(result: Any) -> dict[str, Any]:
    if getattr(result, "status", None) == "failed":
        return {"summary": "SubAgent execution failed."}
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
