# -*- coding: utf-8 -*-
"""SWE Agent - Main agent implementation.

This module provides the main SWEAgent class built on ReActAgent,
with integrated tools, skills, and memory management.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
import logging
import os
from pathlib import Path
import time
from typing import Any, List, Literal, Mapping, Optional, Type, TYPE_CHECKING
from uuid import uuid4

from agentscope.agent import ReActAgent
from agentscope.agent._react_agent import _MemoryMark
from agentscope.message import Msg, ToolResultBlock, ToolUseBlock
from agentscope.tool import Toolkit
from pydantic import BaseModel

from . import mcp_tool_registrar
from .agent_runtime_builder import AgentRuntimeBuilder
from .command_handler import CommandHandler
from .mcp_tool_registrar import McpToolRegistrar
from .hooks import BootstrapHook, MemoryCompactionHook
from .prompt import (
    PromptConfig,
    build_multimodal_hint,
    build_system_prompt_from_working_dir,
    get_active_model_supports_multimodal,
)
from .skills_manager import (
    apply_skill_config_env_overrides,
    ensure_skills_initialized,
    get_workspace_skills_dir,
    resolve_effective_skills,
)
from .skill_tool_registry import SkillToolRegistry
from .tool_failure import normalize_tool_function_errors
from .tool_guard_mixin import (
    PLAN_INTERACTION_SUMMARIZING_SHORT_CIRCUIT_METADATA_KEY,
    ToolGuardMixin,
)
from .tool_output_budget_mixin import ToolOutputBudgetMixin
from .agent_trace_output import build_chat_output_arguments
from .tools import (
    edit_file,
    execute_shell_command,
    # get_process_output,
    get_current_time,
    glob_search,
    grep_search,
    # list_background_processes,
    read_file,
    # start_background_process,
    # stop_background_process,
    write_file,
    create_memory_search_tool,
    create_recover_evidence_tool,
    copy_file_to_static,
    update_task_progress,
    emit_wplus_sop_event,
    ask_plan_clarification,
    create_submit_proposed_plan_tool,
    build_background_subagent_scope,
    create_background_subagent_tools,
    get_default_background_subagent_supervisor,
)
from .utils import process_file_and_media_blocks_in_message
from ..utils.fs_text import sanitize_text_for_json
from ..tracing.agent_trace_sdk import chat_traced
from ..constant import (
    AGENT_INTERRUPT_TIMEOUT,
    AGENT_WATCHDOG_TIMEOUT,
    WORKING_DIR,
)
from ..agents.memory.base_memory_manager import BaseMemoryManager
from ..app.mcp import HttpStatefulClient, StdIOStatefulClient

if TYPE_CHECKING:
    from ..config.config import AgentProfileConfig

logger = logging.getLogger(__name__)
_ACCEPTED_PLAN_TEXT_LIMIT = 1200
_ACCEPTED_PLAN_LIST_LIMIT = 20
_ACCEPTED_PLAN_SOURCE_META_KEY = "accepted_plan_source"
_ACCEPTED_PLAN_SERVER_SOURCE = "server_plan_store"
_INTERNAL_ACCEPTED_PLAN_TOOL_NAME = "accepted_plan_context"
_INTERNAL_ACCEPTED_PLAN_TOOL_ID_KEY = "_accepted_plan_tool_call_id"
_PLAN_MODE_CLARIFICATION_INSTRUCTION = """[Plan Mode]
You are now in Plan Mode. Your job is to turn the user's request into a concrete, reviewable plan **without executing it**.

## Core Rules

- Do not implement the plan, modify files, create resources, or take any action with execution side effects.
- Model the work as a decision tree. Each decision may introduce dependencies, constraints, risks, acceptance criteria, and implementation details.
- Never silently assume an unresolved decision. Material assumptions must be confirmed by the user or verified with available tools.
- The user makes product and trade-off decisions. You investigate verifiable facts, identify risks, and organize the plan.

## Clarify in Rounds

Work in rounds rather than guessing everything at once:

1. Identify all unresolved decisions.
2. Find the current frontier: decisions whose prerequisites are already settled and can be answered now.
3. You MUST use ask_plan_clarification to ask the complete frontier.
   Ask a question series when several related decisions can be collected
   together; prefer a form for that series.
4. Wait for the user's answer.
5. Update the decision tree, mark resolved decisions, and calculate the next frontier.

Do not ask questions that depend on other unresolved questions. If a fact can be checked in repositories, documentation, the environment, or available tools, investigate it instead of asking the user to provide it.

## What Must Be Resolved

Use `ask_plan_clarification` for every material unresolved item unless the user has already specified it or it has been verified:

- Scope and non-goals
- Priorities, trade-offs, and acceptable risks
- Technical approach and implementation constraints
- Dependencies, ownership, and collaboration boundaries
- Acceptance criteria, testing, and verification
- Deployment, migration, compatibility, and rollback requirements

Make the decision being requested explicit. Provide concrete options when useful.
- single_choice and multi_choice clarifications must not include recommended answers.
- Choice controls include a system-owned custom-answer path. Provide only concrete
  business options and do not generate an "other" or custom-answer option.
- text clarifications may include a recommended answer only when it helps the user evaluate a concrete default.
- After the user answers one question series, review remaining dependencies and continue with the next question series when needed.
- Continue until all decision-tree branches relevant to the requested plan have been clarified well enough to produce a concrete, reviewable plan.

## Submit the Plan

Do not call `submit_proposed_plan` until every relevant branch has an empty frontier.

Before calling `submit_proposed_plan`, each material decision must be one of
the following. In particular, before calling submit_proposed_plan, confirm
every relevant decision tree branch is resolved:

- Explicitly decided by the user
- Explicitly accepted by the user as an assumption
- Verified through available tools or reliable sources
- Explicitly recorded as out of scope

`submit_proposed_plan` must include:

- Goals and scope
- Explicit non-goals
- Implementation steps and dependencies
- Risks and mitigations
- Verification and acceptance criteria
- Confirmed assumptions and boundaries

Keep using `ask_plan_clarification` until all material open questions are resolved. Never call `submit_proposed_plan` merely to finish quickly.
    """
_PLAN_MODE_ALLOWED_TOOLS = frozenset(
    {
        "execute_shell_command",
        "read_file",
        "grep_search",
        "glob_search",
        "get_current_time",
        "memory_search",
        "ask_plan_clarification",
        "submit_proposed_plan",
    },
)
_COMPLETION_JUDGE_ALLOWED_TOOLS = frozenset(
    {
        "read_file",
        "grep_search",
        "glob_search",
        "get_current_time",
    },
)
_OPERATION_GROUP_DECLARATION_INSTRUCTION = """[Operation Group Declaration]
When several tool calls belong to one user-visible task phase (for example:
inspect an image, then recognize its text, then verify the result), attach a
consistent display-only metadata object to EACH of those tool calls' arguments:

{"__swe_operation_group": {"id": "<stable-phase-id>", "name": "<short user-facing phase name>"}}

Rules:
- Reuse the exact same id and name for every tool call in the same phase.
  Use a NEW id whenever the phase changes.
- The name is plain Chinese or English text, at most 40 characters. It MUST
  NOT contain paths, commands, quotes, credentials, environment variables,
  or any sensitive values.
- The field is stripped before the tool runs and never reaches the tool.
- When tool calls are unrelated to a shared phase, do not attach
  __swe_operation_group at all."""
_GOAL_TURN_INSTRUCTION = """[Goal Mode]
You are advancing a confirmed Goal Contract. Perform one focused Main Agent turn,
then you MUST call `submit_goal_turn_resolution` exactly once. Use `continue`
when more work can proceed now, `wait` only with explicit wake conditions,
`propose_completion` only when every completion condition is ready for
independent verification, and `blocked` only when no effective action remains.
Do not claim final completion in prose; the Goal Runtime verifies it."""
_GOAL_PROPOSAL_INSTRUCTION = """[Goal Mode — Contract Draft]
Turn the user's overall objective into a complete Goal Contract Draft. Ask for
clarification when the objective, deterministic completion criteria, constraints,
or autonomy boundary are materially unclear. When the Contract Draft is ready,
call `submit_proposed_plan` with objective, completion_criteria, constraints,
and autonomy_boundary. Each completion criterion must contain exactly four
non-empty string fields: requirement, observable_assertion,
verification_method, and expected_outcome. constraints must contain exactly
must_preserve and must_not_do, both string arrays. Do not use criterion,
verification, verification_command, or arrays for a criterion field. Do not
execute the Goal before the user confirms it."""

# Valid namesake strategies for tool registration
NamesakeStrategy = Literal["override", "skip", "raise", "rename"]


def _plan_interaction_tools_enabled(plan_mode_enabled: bool) -> bool:
    if plan_mode_enabled:
        return True

    from ..app.source_system_config.registry import (
        is_normal_mode_plan_interaction_tools_enabled,
    )
    from ..app.source_system_config.runtime import (
        get_current_source_system_config,
    )

    return is_normal_mode_plan_interaction_tools_enabled(
        get_current_source_system_config(),
    )


def _add_main_agent_tools(
    tool_functions: dict[str, Any],
    *,
    request_context: dict[str, Any],
    workspace_dir: Path | None,
    plan_mode_enabled: bool,
) -> None:
    if request_context.get("goal_id"):
        from ..app.goals.turn_tool import (
            create_submit_goal_turn_resolution_tool,
        )

        tool_functions["submit_goal_turn_resolution"] = (
            create_submit_goal_turn_resolution_tool(request_context)
        )
    if request_context.get("execution_origin") == "scheduled":
        return
    goal_mode_enabled = bool(request_context.get("goal_mode_enabled"))
    if not goal_mode_enabled and not _plan_interaction_tools_enabled(
        plan_mode_enabled,
    ):
        return
    tool_functions.update(
        {
            "ask_plan_clarification": ask_plan_clarification,
            "submit_proposed_plan": create_submit_proposed_plan_tool(
                request_context=request_context,
                workspace_dir=workspace_dir,
            ),
        },
    )


def _stringify_accepted_plan_value(value: Any) -> str:
    """限制计划字段长度，避免异常持久化内容撑爆系统提示词。"""
    text = str(value).strip()
    if len(text) <= _ACCEPTED_PLAN_TEXT_LIMIT:
        return text
    return text[: _ACCEPTED_PLAN_TEXT_LIMIT - 3] + "..."


def _format_accepted_plan_items(value: Any) -> list[str]:
    """把计划列表字段转成稳定文本，忽略非预期结构。"""
    if not isinstance(value, list):
        return []
    return [
        _stringify_accepted_plan_value(item)
        for item in value[:_ACCEPTED_PLAN_LIST_LIMIT]
        if str(item).strip()
    ]


def _get_server_accepted_plan(
    request_context: dict[str, Any],
) -> dict[str, Any] | None:
    """仅接受来自后端计划存储的 accepted plan。"""
    accepted_plan = request_context.get("accepted_plan")
    if not isinstance(accepted_plan, dict):
        return None
    if (
        request_context.get(_ACCEPTED_PLAN_SOURCE_META_KEY)
        != _ACCEPTED_PLAN_SERVER_SOURCE
    ):
        return None
    if request_context.get("plan_mode_enabled"):
        return None
    return accepted_plan


def _build_accepted_plan_tool_result_text(
    accepted_plan: dict[str, Any],
) -> str:
    """构造 accepted plan 的内部 tool result 文本。"""

    lines = [
        "[Accepted Plan Execution Context]",
        "The backend persisted this plan after the user selected Execute.",
        "Treat it as the source of truth for this execution turn.",
        "Do not regenerate the plan or rely on the front-end query text.",
        "If the plan conflicts with the current repository state, report the "
        "conflict before changing files.",
    ]
    for field in ("plan_id", "title", "summary"):
        value = accepted_plan.get(field)
        if value is not None:
            lines.append(f"- {field}: {_stringify_accepted_plan_value(value)}")

    for field in ("steps", "risks", "verification"):
        items = _format_accepted_plan_items(accepted_plan.get(field))
        if not items:
            continue
        lines.append(f"- {field}:")
        lines.extend(
            f"  {index}. {item}" for index, item in enumerate(items, 1)
        )

    return "\n".join(lines)


def _get_internal_accepted_plan_tool_call_id(
    request_context: dict[str, Any],
) -> str:
    """为当前执行轮次返回稳定的内部 tool call id。"""
    existing = request_context.get(_INTERNAL_ACCEPTED_PLAN_TOOL_ID_KEY)
    if isinstance(existing, str) and existing:
        return existing

    turn_id = str(request_context.get("turn_id") or "").strip()
    suffix = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in turn_id
    ).strip("-_")
    if not suffix:
        suffix = uuid4().hex

    call_id = f"accepted-plan-{suffix}"
    request_context[_INTERNAL_ACCEPTED_PLAN_TOOL_ID_KEY] = call_id
    return call_id


def _build_accepted_plan_tool_exchange(
    request_context: dict[str, Any],
) -> list[Msg]:
    """构造当前执行轮次使用的内部 accepted plan tool exchange。"""
    accepted_plan = _get_server_accepted_plan(request_context)
    if accepted_plan is None:
        return []

    call_id = _get_internal_accepted_plan_tool_call_id(request_context)
    tool_name = _INTERNAL_ACCEPTED_PLAN_TOOL_NAME
    result_text = _build_accepted_plan_tool_result_text(accepted_plan)
    return [
        Msg(
            "assistant",
            [
                ToolUseBlock(
                    type="tool_use",
                    id=call_id,
                    name=tool_name,
                    input={},
                ),
            ],
            "assistant",
        ),
        Msg(
            "system",
            [
                ToolResultBlock(
                    type="tool_result",
                    id=call_id,
                    name=tool_name,
                    output=[{"type": "text", "text": result_text}],
                ),
            ],
            "system",
        ),
    ]


class AgentPhase(str, Enum):
    """Execution phases used by the Agent watchdog policy."""

    REASONING = "reasoning"
    ACTING = "acting"
    TOOL_EXECUTION = "tool_execution"
    TOOL_GUARD = "tool_guard"
    APPROVAL_REPLAY = "approval_replay"
    SUMMARIZING = "summarizing"
    IDLE = "idle"
    UNKNOWN = "unknown"


@dataclass
class AgentPhaseState:
    """Current Agent phase and activity metadata."""

    phase: AgentPhase
    started_at: float
    last_activity_at: float
    tool_name: str | None = None
    tool_call_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ResearchPhaseResult:
    """Outcome of a bounded SubAgent research ReAct loop."""

    status: Literal["completed", "turn_limit_reached"]
    reply: Msg | None
    turns_used: int
    messages: tuple[Msg, ...] = ()


class SWEAgent(ToolGuardMixin, ToolOutputBudgetMixin, ReActAgent):
    """SWE Agent with integrated tools, skills, and memory management.

    This agent extends ReActAgent with:
    - Built-in tools (shell, file operations, browser, etc.)
    - Dynamic skill loading from working directory
    - Memory management with auto-compaction
    - Bootstrap guidance for first-time setup
    - System command handling (/compact, /new, etc.)
    - Tool-guard security interception (via ToolGuardMixin)

    MRO note
    ~~~~~~~~
    ``ToolGuardMixin`` overrides ``_acting`` and ``_reasoning`` via
    Python's MRO: SWEAgent → ToolGuardMixin → ToolOutputBudgetMixin →
    ReActAgent.  If you
    add a ``_acting`` or ``_reasoning`` override in this class, you
    **must** call ``super()._acting(...)`` / ``super()._reasoning(...)``
    so the guard interception remains active.
    """

    _reply_task: asyncio.Task[Any] | None
    _resolved_model_provider: Any | None

    def _apply_model_input_budget(self, model_config: Any) -> None:
        """Apply a selected model's context capacity to this run only."""
        max_input_length = getattr(model_config, "max_input_length", None)
        if max_input_length is not None:
            self._agent_config.running.max_input_length = max_input_length

    def _capture_model_provider_snapshot(self, provider: Any) -> None:
        """Retain the Provider state selected when this Agent was created."""
        self._resolved_model_provider = provider

    @staticmethod
    def _rebuild_mcp_client(client: Any) -> Any | None:
        """Proxy the legacy recovery seam to the MCP tool registrar."""
        original_http_client = mcp_tool_registrar.HttpStatefulClient
        original_stdio_client = mcp_tool_registrar.StdIOStatefulClient
        mcp_tool_registrar.HttpStatefulClient = HttpStatefulClient
        mcp_tool_registrar.StdIOStatefulClient = StdIOStatefulClient
        try:
            return McpToolRegistrar._rebuild_mcp_client(client)
        finally:
            mcp_tool_registrar.HttpStatefulClient = original_http_client
            mcp_tool_registrar.StdIOStatefulClient = original_stdio_client

    def __init__(
        self,
        agent_config: "AgentProfileConfig",
        env_context: Optional[str] = None,
        enable_memory_manager: bool = True,
        mcp_clients: Optional[List[Any]] = None,
        memory_manager: "BaseMemoryManager | None" = None,
        request_context: Optional[dict[str, str]] = None,
        namesake_strategy: NamesakeStrategy = "skip",
        workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
        enable_workspace_skills: bool = True,
        workspace_skill_dirs: dict[str, Path] | None = None,
        workspace_skill_snapshot: Any | None = None,
        model_slot_override: Any | None = None,
        model_provider_override: Any | None = None,
        fallback_model_slot: Any | None = None,
        fallback_model_provider: Any | None = None,
        system_prompt_override: str | None = None,
        source_tool_versions: tuple[Any, ...] = (),
    ):
        """Initialize SWEAgent.

        Args:
            agent_config: Agent profile configuration containing all settings
                including running config (max_iters, max_input_length,
                memory_compact_threshold, etc.) and language setting.
            env_context: Optional environment context to prepend to
                system prompt
            enable_memory_manager: Whether to enable memory manager
            mcp_clients: Optional list of MCP clients for tool
                integration
            memory_manager: Optional memory manager instance
            request_context: Optional request context with session_id,
                user_id, channel, agent_id
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
            workspace_dir: Workspace directory for reading prompt files
                (if None, uses global WORKING_DIR)
        """
        self._agent_config = agent_config.model_copy(deep=True)
        agent_config = self._agent_config
        self._env_context = env_context
        self._request_context = dict(request_context or {})
        self._mcp_clients = mcp_clients or []
        self._namesake_strategy = namesake_strategy
        self._workspace_dir = workspace_dir
        self._task_tracker = task_tracker
        self._enable_workspace_skills = enable_workspace_skills
        self._workspace_skill_dirs = dict(workspace_skill_dirs or {})
        self._workspace_skill_snapshot = workspace_skill_snapshot
        self._model_slot_override = model_slot_override
        self._model_provider_override = model_provider_override
        self._fallback_model_slot = fallback_model_slot
        self._fallback_model_provider = fallback_model_provider
        self._resolved_model_slot: dict[str, str] = {}
        self._resolved_model_provider = None
        self._system_prompt_override = system_prompt_override
        self._source_tool_versions = tuple(source_tool_versions)
        self._skill_tool_registry = SkillToolRegistry()
        self._init_agent_phase_state()
        goal_finalization = bool(
            self._request_context.get("goal_finalization"),
        )
        completion_judge = (
            self._request_context.get("agent_role") == "completion_judge"
        )

        # Extract configuration from agent_config
        running_config = agent_config.running
        self._language = agent_config.language

        runtime = AgentRuntimeBuilder.build_for_swe_agent(
            self,
            goal_finalization=goal_finalization,
            completion_judge=completion_judge,
            namesake_strategy=namesake_strategy,
        )

        # Get model info from ProviderManager (single source of truth)
        try:
            from swe.config.context import get_current_effective_tenant_id
            from swe.providers.provider_manager import ProviderManager

            tenant_id = get_current_effective_tenant_id()
            manager = ProviderManager.get_instance(tenant_id)
            active = manager.get_active_model()
            if active:
                model_info = f"{active.provider_id}/{active.model}"
            else:
                model_info = "not-configured"
        except Exception:
            model_info = "unknown"
        logger.info(
            f"Agent '{agent_config.id}' initialized with model: "
            f"{model_info} (class: {runtime.model.__class__.__name__})",
        )
        # Initialize parent ReActAgent
        super().__init__(
            name="Friday",
            model=runtime.model,
            sys_prompt=runtime.system_prompt,
            toolkit=runtime.toolkit,
            memory=runtime.memory,
            formatter=runtime.formatter,
            max_iters=running_config.max_iters,
        )
        self._sys_prompt_freshness_token = (
            self._current_system_prompt_freshness_token()
        )

        # Setup memory manager
        self._setup_memory_manager(
            enable_memory_manager
            and not goal_finalization
            and not completion_judge,
            None if goal_finalization or completion_judge else memory_manager,
            namesake_strategy,
        )

        # Setup command handler
        self.command_handler = CommandHandler(
            agent_name=self.name,
            memory=self.memory,
            memory_manager=self.memory_manager,
            enable_memory_manager=self._enable_memory_manager,
            request_context=self._request_context,
        )

        # Register hooks
        self._register_hooks()

    def _create_toolkit(
        self,
        namesake_strategy: NamesakeStrategy = "skip",
    ) -> Toolkit:
        """Create and populate toolkit with built-in tools.

        Args:
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")

        Returns:
            Configured toolkit instance
        """
        request_context = getattr(self, "_request_context", {}) or {}
        plan_mode_enabled = bool(request_context.get("plan_mode_enabled"))
        enabled_tools, async_execution_tools = self._tool_settings(
            request_context,
            plan_mode_enabled,
        )
        tool_functions = self._tool_functions(
            request_context,
            plan_mode_enabled,
        )
        if request_context.get("agent_role") == "completion_judge":
            enabled_tools = {
                name: enabled_tools.get(name, False)
                and name in _COMPLETION_JUDGE_ALLOWED_TOOLS
                for name in tool_functions
            }
        toolkit = Toolkit()
        self._register_enabled_tools(
            toolkit,
            tool_functions,
            enabled_tools,
            async_execution_tools,
            plan_mode_enabled,
            namesake_strategy,
        )
        if request_context.get("agent_role") != "completion_judge":
            self._register_background_task_tools(
                toolkit,
                tool_functions,
                enabled_tools,
                async_execution_tools,
                namesake_strategy,
            )

            self._register_background_subagent_tools(
                toolkit,
                namesake_strategy,
                request_context,
            )

        return toolkit

    def _tool_settings(
        self,
        request_context: dict[str, Any],
        plan_mode_enabled: bool,
    ) -> tuple[dict[str, bool], dict[str, bool]]:
        from ..config.config import _default_builtin_tools

        builtin_tool_defaults = _default_builtin_tools()
        enabled_tools = {
            name: tool.enabled for name, tool in builtin_tool_defaults.items()
        }
        async_execution_tools = {
            "execute_shell_command": builtin_tool_defaults[
                "execute_shell_command"
            ].async_execution,
        }
        try:
            builtin_tools = getattr(
                getattr(self._agent_config, "tools", None),
                "builtin_tools",
                None,
            )
            if builtin_tools is not None:
                enabled_tools.update(
                    {
                        name: tool.enabled
                        for name, tool in builtin_tools.items()
                    },
                )
                if "execute_shell_command" in builtin_tools:
                    async_execution_tools["execute_shell_command"] = (
                        builtin_tools["execute_shell_command"].async_execution
                    )
        except Exception as exc:
            logger.warning(
                f"Failed to load agent tools config: {exc}, "
                "canonical tool defaults will be used",
            )
        if request_context.get("agent_role") == "completion_judge":
            return (
                {
                    name: enabled and name in _COMPLETION_JUDGE_ALLOWED_TOOLS
                    for name, enabled in enabled_tools.items()
                },
                async_execution_tools,
            )
        if request_context.get("agent_role") == "subagent":
            return (
                self._subagent_tool_settings(request_context),
                async_execution_tools,
            )
        if plan_mode_enabled:
            enabled_tools = {
                name: enabled and name in _PLAN_MODE_ALLOWED_TOOLS
                for name, enabled in enabled_tools.items()
            }
        return enabled_tools, async_execution_tools

    @staticmethod
    def _subagent_tool_settings(
        request_context: dict[str, Any],
    ) -> dict[str, bool]:
        policy = request_context.get("subagent_policy") or {}
        tools_policy = policy.get("tools") if isinstance(policy, dict) else {}
        allowed = (
            set(tools_policy.get("allow") or [])
            if isinstance(tools_policy, dict)
            else set()
        )
        return {
            name: name in allowed
            for name in (
                "execute_shell_command",
                "start_background_process",
                "list_background_processes",
                "get_process_output",
                "stop_background_process",
                "read_file",
                "write_file",
                "edit_file",
                "grep_search",
                "glob_search",
                "get_current_time",
                "set_user_timezone",
                "get_token_usage",
                "copy_file_to_static",
                "update_task_progress",
            )
        }

    def _tool_functions(
        self,
        request_context: dict[str, Any],
        plan_mode_enabled: bool,
    ) -> dict[str, Any]:
        tool_functions = {
            "execute_shell_command": execute_shell_command,
            "read_file": read_file,
            "write_file": write_file,
            "edit_file": edit_file,
            "grep_search": grep_search,
            "glob_search": glob_search,
            "get_current_time": get_current_time,
            "copy_file_to_static": copy_file_to_static,
            "update_task_progress": update_task_progress,
            "emit_wplus_sop_event": emit_wplus_sop_event,
        }
        if request_context.get("agent_role", "main") not in {
            "subagent",
            "completion_judge",
        }:
            _add_main_agent_tools(
                tool_functions,
                request_context=request_context,
                workspace_dir=getattr(self, "_workspace_dir", None),
                plan_mode_enabled=plan_mode_enabled,
            )
        return tool_functions

    def _register_enabled_tools(
        self,
        toolkit: Toolkit,
        tool_functions: dict[str, Any],
        enabled_tools: dict[str, bool],
        async_execution_tools: dict[str, bool],
        plan_mode_enabled: bool,
        namesake_strategy: NamesakeStrategy,
    ) -> None:
        for tool_name, tool_func in tool_functions.items():
            if plan_mode_enabled and tool_name not in _PLAN_MODE_ALLOWED_TOOLS:
                logger.debug("Skipped Plan Mode forbidden tool: %s", tool_name)
                continue
            if not enabled_tools.get(tool_name, True):
                logger.debug("Skipped disabled tool: %s", tool_name)
                continue
            async_exec = async_execution_tools.get(tool_name, False)
            toolkit.register_tool_function(
                tool_func,
                namesake_strategy=namesake_strategy,
                async_execution=async_exec,
            )
            logger.debug(
                "Registered tool: %s (async_execution=%s)",
                tool_name,
                async_exec,
            )
            self._normalize_registered_tool_functions(toolkit, [tool_name])

    @staticmethod
    def _register_background_task_tools(
        toolkit: Toolkit,
        tool_functions: dict[str, Any],
        enabled_tools: dict[str, bool],
        async_execution_tools: dict[str, bool],
        namesake_strategy: NamesakeStrategy,
    ) -> None:
        has_async_tools = any(
            async_execution_tools.get(name, False)
            for name in tool_functions
            if enabled_tools.get(name, True)
        )
        if not has_async_tools:
            return
        try:
            for task_tool in (
                toolkit.view_task,
                toolkit.wait_task,
                toolkit.cancel_task,
            ):
                toolkit.register_tool_function(
                    task_tool,
                    namesake_strategy=namesake_strategy,
                )
            logger.debug(
                "Registered background task management tools "
                "(view_task, wait_task, cancel_task)",
            )
        except Exception as exc:
            logger.warning(f"Failed to register task management tools: {exc}")

    def _register_background_subagent_tools(
        self,
        toolkit: Toolkit,
        namesake_strategy: NamesakeStrategy,
        request_context: dict[str, Any],
    ) -> None:
        if self._background_subagent_registration_blocked(request_context):
            return
        supervisor = (
            request_context.get(
                "_subagent_supervisor",
            )
            or get_default_background_subagent_supervisor()
        )
        workspace_dir = self._workspace_dir or Path(
            self._agent_config.workspace_dir or ".",
        )
        workspace_snapshot = getattr(self, "_workspace_skill_snapshot", None)
        if workspace_snapshot is not None:
            channel = request_context.get("channel", "console")
            effective_skill_names = [
                name
                for name, skill in workspace_snapshot.skills.items()
                if "all" in skill.channels or channel in skill.channels
            ]
        else:
            effective_skill_names = resolve_effective_skills(
                workspace_dir,
                request_context.get("channel", "console"),
            )
        skill_snapshot_signatures = (
            {
                name: skill.content_signature
                for name, skill in workspace_snapshot.skills.items()
            }
            if workspace_snapshot is not None
            else None
        )
        skill_snapshot_dirs = (
            {
                name: skill.directory
                for name, skill in workspace_snapshot.skills.items()
            }
            if workspace_snapshot is not None
            else None
        )
        tools = create_background_subagent_tools(
            supervisor=supervisor,
            parent_agent_config=self._agent_config,
            workspace_dir=workspace_dir,
            request_context=request_context,
            effective_skill_names=effective_skill_names,
            skill_snapshot_signatures=skill_snapshot_signatures,
            skill_snapshot_dirs=skill_snapshot_dirs,
            selected_expert_id=str(
                request_context.get("selected_expert_id") or "",
            ).strip()
            or None,
        )
        names = self._background_subagent_tool_names(True)
        for name in names:
            toolkit.register_tool_function(
                tools[name],
                namesake_strategy=namesake_strategy,
            )
        self._normalize_registered_tool_functions(toolkit, names)

    def _background_subagent_registration_blocked(
        self,
        request_context: dict[str, Any],
    ) -> bool:
        selected_expert_id = str(
            request_context.get("selected_expert_id") or "",
        ).strip()
        return (
            request_context.get("agent_role", "main")
            in {"subagent", "completion_judge"}
            or not (
                request_context.get("agent_id")
                or getattr(self._agent_config, "id", None)
            )
            or not selected_expert_id
        )

    @staticmethod
    def _background_subagent_tool_names(
        intent: bool,
    ) -> list[str]:
        names = []
        if intent:
            names.append("start_subagent")
        if intent:
            names.append("wait_subagent")
        if intent:
            names.extend(["get_subagent", "cancel_subagent"])
        return names

    def _register_source_tools(self, toolkit: Toolkit) -> None:
        """Register the Agent-start source-tool snapshot after skills are known."""
        if not self._source_tool_versions:
            return
        from ..config.config import _default_builtin_tools

        builtin_names = set(_default_builtin_tools())
        runtime = self._source_tool_runtime()
        configured_tools = self._configured_builtin_tools()
        enabled_tools = {
            name: tool.enabled for name, tool in configured_tools.items()
        }
        for version in self._source_tool_versions:
            self._register_source_tool_version(
                toolkit,
                version,
                runtime,
                builtin_names,
                configured_tools,
                enabled_tools,
            )

    def _source_tool_runtime(self):
        """Build the execution context shared by source-tool versions."""
        from .source_tools import SourceToolRuntime

        return SourceToolRuntime(
            tenant_id=self._request_context.get("tenant_id") or None,
            source_id=self._request_context.get("source_id") or None,
            workspace_dir=Path(self._workspace_dir or WORKING_DIR),
            agent_id=self._request_context.get("agent_id") or None,
        )

    def _configured_builtin_tools(self):
        """Return the current agent's configured builtin tools."""
        return getattr(self._agent_config.tools, "builtin_tools", {})

    def _register_source_tool_version(
        self,
        toolkit: Toolkit,
        version,
        runtime,
        builtin_names: set[str],
        configured_tools,
        enabled_tools: dict[str, bool],
    ) -> None:
        """Validate and register one source-tool version."""
        if not enabled_tools.get(version.name, True):
            logger.debug("Skipped disabled source tool: %s", version.name)
            return

        is_builtin_override = version.name in builtin_names
        self._validate_source_tool_registration(
            toolkit,
            version,
            is_builtin_override,
        )
        from .source_tools import build_source_tool_function

        tool_func = build_source_tool_function(version, runtime)
        toolkit.register_tool_function(
            tool_func,
            func_name=version.name,
            func_description=version.description,
            json_schema={
                "type": "function",
                "function": {
                    "name": version.name,
                    "description": version.description,
                    "parameters": version.json_schema,
                },
            },
            namesake_strategy="override" if is_builtin_override else "raise",
            async_execution=self._source_tool_async_execution(
                version.name,
                configured_tools,
                enabled_tools,
            ),
        )
        self._normalize_registered_tool_functions(toolkit, [version.name])

    @staticmethod
    def _validate_source_tool_registration(
        toolkit: Toolkit,
        version,
        is_builtin_override: bool,
    ) -> None:
        """Reject source tools that cannot safely replace a registered tool."""
        if not is_builtin_override and version.name in toolkit.tools:
            raise RuntimeError(
                "source tool collides with a skill or managed tool: "
                f"{version.name}",
            )
        if not is_builtin_override:
            return

        registered = toolkit.tools.get(version.name)
        registered_schema = getattr(registered, "json_schema", None)
        registered_parameters = (
            registered_schema.get("function", {}).get("parameters")
            if isinstance(registered_schema, dict)
            else None
        )
        from ..app.runner.operation_group import (
            schema_parameters_without_operation_group,
        )

        if schema_parameters_without_operation_group(
            registered_parameters,
        ) != schema_parameters_without_operation_group(version.json_schema):
            raise RuntimeError(
                "source override schema must match the code-defined builtin: "
                f"{version.name}",
            )

    @staticmethod
    def _source_tool_async_execution(
        tool_name: str,
        configured_tools,
        enabled_tools: dict[str, bool],
    ) -> bool:
        """Return whether a source tool uses background execution."""
        shell_tool = configured_tools.get("execute_shell_command")
        return bool(
            tool_name == "execute_shell_command"
            and enabled_tools.get("execute_shell_command", False)
            and shell_tool is not None
            and shell_tool.async_execution,
        )

    @staticmethod
    def _normalize_registered_tool_functions(
        toolkit: Toolkit,
        tool_names: list[str],
    ) -> None:
        """Wrap registered tools so failures use Swe's structured contract.

        Also declares the optional display-only operation_group argument
        in each tool schema so the agent may group tool calls of one
        user-visible task phase without breaking strict providers.
        """
        from ..app.runner.operation_group import inject_operation_group_schema

        for tool_name in tool_names:
            tool_entry = toolkit.tools.get(tool_name)
            if tool_entry is None:
                continue
            tool_entry.original_func = normalize_tool_function_errors(
                tool_entry.original_func,
            )
            inject_operation_group_schema(
                getattr(tool_entry, "json_schema", None),
            )

    def _register_skills(self, toolkit: Toolkit) -> None:
        """Load and register skills from workspace directory.

        Uses the registry-backed skill resolver to determine effective
        skills for the current channel.

        Args:
            toolkit: Toolkit to register skills to
        """
        if not getattr(self, "_enable_workspace_skills", True):
            self._effective_skills = []
            return

        workspace_dir = self._workspace_dir or WORKING_DIR
        request_context = getattr(self, "_request_context", {})
        channel_name = request_context.get("channel", "console")

        snapshot_skill_dirs = getattr(self, "_workspace_skill_dirs", {})
        if snapshot_skill_dirs:
            self._register_explicit_workspace_skills(
                toolkit,
                snapshot_skill_dirs,
            )
            return

        workspace_snapshot = getattr(self, "_workspace_skill_snapshot", None)
        if workspace_snapshot is not None:
            effective_skills = [
                name
                for name, runtime_skill in workspace_snapshot.skills.items()
                if "all" in runtime_skill.channels
                or channel_name in runtime_skill.channels
            ]
            registered_skills: list[str] = []
            for skill_name in effective_skills:
                runtime_skill = workspace_snapshot.skills[skill_name]
                try:
                    # AgentScope's public helper reparses SKILL.md.  The
                    # query snapshot already validated and captured its
                    # metadata, so register the equivalent adapter directly
                    # and avoid synchronous frontmatter I/O on the loop.
                    toolkit.skills[skill_name] = {
                        "name": skill_name,
                        "description": str(
                            runtime_skill.metadata.get("description") or "",
                        ),
                        "dir": str(runtime_skill.directory),
                    }
                    registered_skills.append(skill_name)
                    logger.debug("Registered skill: %s", skill_name)
                except Exception as exc:
                    logger.error(
                        "Failed to register skill '%s': %s",
                        skill_name,
                        exc,
                    )
            self._sanitize_registered_skill_dirs(toolkit)
            skill_runtime_profiles = {
                name: workspace_snapshot.skills[name].runtime_profile
                for name in registered_skills
            }
            self._build_skill_tool_registry(skill_runtime_profiles)
            self._runtime_skills = registered_skills
            self._effective_skills = registered_skills
            self._skill_runtime_profiles = skill_runtime_profiles
            return

        ensure_skills_initialized(workspace_dir)

        effective_skills = resolve_effective_skills(
            workspace_dir,
            channel_name,
        )
        from .skill_runtime_profile import build_skill_runtime_profiles

        skill_runtime_profiles = build_skill_runtime_profiles(
            Path(workspace_dir),
            effective_skills,
        )

        working_skills_dir = get_workspace_skills_dir(Path(workspace_dir))

        for skill_name in effective_skills:
            skill_dir = working_skills_dir / skill_name
            if skill_dir.exists():
                try:
                    toolkit.register_agent_skill(str(skill_dir))
                    logger.debug("Registered skill: %s", skill_name)
                except Exception as e:
                    logger.error(
                        "Failed to register skill '%s': %s",
                        skill_name,
                        e,
                    )

        self._sanitize_registered_skill_dirs(toolkit)

        # Build skill-tool registry for multi-skill attribution
        self._build_skill_tool_registry(skill_runtime_profiles)

        # Store effective skills for later detector setup
        self._runtime_skills = effective_skills
        self._effective_skills = effective_skills
        self._skill_runtime_profiles = skill_runtime_profiles

    def _register_explicit_workspace_skills(
        self,
        toolkit: Toolkit,
        skill_dirs: dict[str, Path],
    ) -> None:
        """Register only immutable Skill roots supplied by a worker launch."""
        from .skill_runtime_profile import build_skill_runtime_profile

        effective_skills: list[str] = []
        profiles: dict[str, object] = {}
        for skill_name, skill_dir in skill_dirs.items():
            path = Path(skill_dir)
            if not path.is_dir() or not (path / "SKILL.md").is_file():
                continue
            try:
                toolkit.register_agent_skill(str(path))
                effective_skills.append(skill_name)
                profiles[skill_name] = build_skill_runtime_profile(
                    path,
                    skill_name,
                )
            except Exception as exc:
                logger.error(
                    "Failed to register explicit skill '%s': %s",
                    skill_name,
                    exc,
                )
        self._sanitize_registered_skill_dirs(toolkit)
        self._skill_tool_registry = self._build_explicit_skill_tool_registry(
            profiles,
        )
        self._runtime_skills = effective_skills
        self._effective_skills = effective_skills
        self._skill_runtime_profiles = profiles

    @staticmethod
    def _build_explicit_skill_tool_registry(
        profiles: dict[str, object],
    ) -> SkillToolRegistry:
        """Register copied Skill declarations without consulting a workspace."""
        registry = SkillToolRegistry()
        for skill_name, profile in profiles.items():
            declared_tools = getattr(profile, "declared_tools", [])
            if declared_tools:
                registry.register_skill_tools(skill_name, declared_tools)
        return registry

    def get_effective_skills(self) -> list[str]:
        """Get the list of effective skills for this agent.

        Returns:
            List of enabled skill names
        """
        return self._effective_skills

    def get_runtime_skills(self) -> list[str]:
        """Get the list of runtime-enabled skills for this agent."""
        return getattr(self, "_runtime_skills", self._effective_skills)

    def get_skill_runtime_profiles(self) -> dict[str, object]:
        """Get cached skill runtime profiles for this agent."""
        return getattr(self, "_skill_runtime_profiles", {})

    def get_skill_tool_registry(self) -> SkillToolRegistry:
        """Get this Agent's request-local skill-tool registry."""
        registry = getattr(self, "_skill_tool_registry", None)
        if registry is None:
            registry = SkillToolRegistry()
            self._skill_tool_registry = registry
        return registry

    def _build_skill_tool_registry(
        self,
        profiles: Mapping[str, object],
    ) -> None:
        """Build skill-tool registry for tool attribution.

        Args:
            profiles: Runtime profiles already parsed from enabled skills.
        """
        from .skill_tool_registry import (
            build_skill_tool_registry_from_profiles,
        )

        try:
            self._skill_tool_registry = (
                build_skill_tool_registry_from_profiles(
                    profiles,
                )
            )
        except Exception as e:
            logger.warning("Failed to build skill-tool registry: %s", e)

    async def setup_skill_detector(self, trace_id: str) -> None:
        """Setup skill invocation detector for a trace.

        This should be called after start_trace() to enable skill
        detection during the trace.

        Args:
            trace_id: The trace ID to setup detector for
        """
        if getattr(self, "_workspace_skill_dirs", {}):
            # The detector resolves manifests and asset paths relative to a
            # workspace. Snapshot-backed agents must never consult the mutable
            # parent workspace after launch.
            return

        try:
            from ..tracing.manager import (
                get_trace_manager,
                has_trace_manager,
                get_current_trace,
            )

            if not has_trace_manager():
                return

            trace_mgr = get_trace_manager()
            if not trace_mgr.enabled:
                return

            # Check if detector already setup
            ctx = get_current_trace()
            if ctx and ctx.skill_detector:
                return

            # Setup detector with effective skills
            workspace_dir = Path(self._workspace_dir or WORKING_DIR)
            await trace_mgr.setup_skill_detector(
                trace_id=trace_id,
                enabled_skills=self.get_runtime_skills(),
                skill_runtime_profiles=self.get_skill_runtime_profiles(),
                workspace_dir=workspace_dir,
                skill_tool_registry=self.get_skill_tool_registry(),
                skill_metadata=(
                    {
                        name: dict(skill.metadata)
                        for name, skill in getattr(
                            self._workspace_skill_snapshot,
                            "skills",
                            {},
                        ).items()
                    }
                    if getattr(self, "_workspace_skill_snapshot", None)
                    is not None
                    else None
                ),
                skill_dirs=(
                    {
                        name: skill.directory
                        for name, skill in getattr(
                            self._workspace_skill_snapshot,
                            "skills",
                            {},
                        ).items()
                    }
                    if getattr(self, "_workspace_skill_snapshot", None)
                    is not None
                    else None
                ),
                skill_signatures=(
                    {
                        name: skill.content_signature
                        for name, skill in getattr(
                            self,
                            "_workspace_skill_snapshot",
                            None,
                        ).skills.items()
                    }
                    if getattr(self, "_workspace_skill_snapshot", None)
                    is not None
                    else None
                ),
            )
        except Exception as e:
            logger.debug("Failed to setup skill detector: %s", e)

    @staticmethod
    def _sanitize_registered_skill_dirs(toolkit: Toolkit) -> None:
        """Sanitize skill dir paths for prompt/runtime display only."""
        for skill in getattr(toolkit, "skills", {}).values():
            skill_dir = skill.get("dir")
            if not isinstance(skill_dir, str):
                continue
            sanitized = sanitize_text_for_json(skill_dir)
            skill["dir"] = sanitized.value

    def _heartbeat_enabled_for_prompt(self) -> bool:
        heartbeat = getattr(
            getattr(self, "_agent_config", None),
            "heartbeat",
            None,
        )
        return bool(getattr(heartbeat, "enabled", False))

    def _system_prompt_enabled_files(self) -> tuple[str, ...]:
        files = getattr(
            getattr(self, "_agent_config", None),
            "system_prompt_files",
            None,
        )
        if files is None:
            files = PromptConfig.DEFAULT_FILES
        return tuple(str(filename) for filename in files)

    def _system_prompt_file_snapshot(self) -> tuple[tuple[Any, ...], ...]:
        workspace_dir = Path(
            getattr(self, "_workspace_dir", None) or WORKING_DIR,
        )
        snapshots: list[tuple[Any, ...]] = []
        for filename in self._system_prompt_enabled_files():
            file_path = workspace_dir / filename
            try:
                stat_result = file_path.stat()
            except FileNotFoundError:
                snapshots.append((filename, "missing"))
            except OSError as exc:
                snapshots.append((filename, "error", type(exc).__name__))
            else:
                snapshots.append(
                    (
                        filename,
                        "present",
                        stat_result.st_mtime_ns,
                        stat_result.st_size,
                    ),
                )
        return tuple(snapshots)

    @staticmethod
    def _source_system_config_prompt_token() -> tuple[Any, ...] | None:
        from ..app.source_system_config import (
            is_chat_task_progress_enabled,
        )
        from ..app.source_system_config.runtime import (
            get_current_source_system_config,
        )

        source_config = get_current_source_system_config()
        if source_config is None:
            return None
        return (
            getattr(source_config, "source_id", None),
            getattr(source_config, "version", None),
            is_chat_task_progress_enabled(source_config),
        )

    @staticmethod
    def _active_model_prompt_token() -> tuple[Any, ...] | None:
        try:
            from ..config.context import get_current_effective_tenant_id
            from ..providers.provider_manager import ProviderManager

            tenant_id = get_current_effective_tenant_id()
            manager = ProviderManager.get_instance(tenant_id)
            active = manager.get_active_model()
            if active is None:
                return None
            provider = manager.get_provider(active.provider_id)
            model_info = None
            if provider is not None:
                for model in provider.models + provider.extra_models:
                    if model.id == active.model:
                        model_info = model
                        break
            return (
                active.provider_id,
                active.model,
                getattr(model_info, "supports_image", None),
                getattr(model_info, "supports_video", None),
                getattr(model_info, "supports_multimodal", None),
            )
        except Exception as exc:
            return ("error", type(exc).__name__)

    def _current_system_prompt_freshness_token(self) -> tuple[Any, ...]:
        system_prompt_override = getattr(self, "_system_prompt_override", None)
        if system_prompt_override is not None:
            return ("override", system_prompt_override)

        request_context = getattr(self, "_request_context", {}) or {}
        resolved_model_slot = getattr(self, "_resolved_model_slot", {}) or {}
        return (
            str(Path(getattr(self, "_workspace_dir", None) or WORKING_DIR)),
            self._system_prompt_enabled_files(),
            self._system_prompt_file_snapshot(),
            request_context.get("agent_id"),
            bool(request_context.get("plan_mode_enabled")),
            request_context.get("goal_id"),
            self._heartbeat_enabled_for_prompt(),
            getattr(self, "_env_context", None),
            tuple(sorted(resolved_model_slot.items())),
            self._active_model_prompt_token(),
            self._source_system_config_prompt_token(),
        )

    def _build_sys_prompt(self) -> str:
        """Build system prompt from working dir files and env context.

        Returns:
            Complete system prompt string
        """
        system_prompt_override = getattr(self, "_system_prompt_override", None)
        if system_prompt_override is not None:
            return system_prompt_override

        # Get agent_id from request_context
        agent_id = (
            self._request_context.get("agent_id")
            if self._request_context
            else None
        )

        # Check if heartbeat is enabled in agent config
        sys_prompt = build_system_prompt_from_working_dir(
            working_dir=self._workspace_dir,
            enabled_files=list(self._system_prompt_enabled_files()),
            agent_id=agent_id,
            heartbeat_enabled=self._heartbeat_enabled_for_prompt(),
        )
        logger.debug("System prompt:\n%s...", sys_prompt[:100])

        # Inject multimodal capability awareness
        multimodal_hint = build_multimodal_hint()
        if multimodal_hint:
            sys_prompt = sys_prompt + "\n\n" + multimodal_hint

        if self._env_context is not None:
            sys_prompt = sys_prompt + "\n\n" + self._env_context

        from ..app.source_system_config import (
            is_chat_task_progress_enabled,
        )
        from ..app.source_system_config.runtime import (
            get_current_source_system_config,
        )

        plan_mode_enabled = bool(
            (getattr(self, "_request_context", {}) or {}).get(
                "plan_mode_enabled",
            ),
        )
        if plan_mode_enabled:
            sys_prompt = (
                sys_prompt + "\n\n" + _PLAN_MODE_CLARIFICATION_INSTRUCTION
            )
        elif self._request_context.get("goal_id"):
            goal_context = self._request_context.get("goal_contract_context")
            sys_prompt = sys_prompt + "\n\n" + _GOAL_TURN_INSTRUCTION
            if isinstance(goal_context, str) and goal_context.strip():
                sys_prompt = sys_prompt + "\n\n" + goal_context
        elif self._request_context.get("goal_mode_enabled"):
            sys_prompt = sys_prompt + "\n\n" + _GOAL_PROPOSAL_INSTRUCTION
        if not plan_mode_enabled and is_chat_task_progress_enabled(
            get_current_source_system_config(),
        ):
            # 这里按 source 开关注入要求，避免关闭后仍提示模型强制调用。
            sys_prompt += (
                "\n\n[Task Progress Requirement]\n"
                "You MUST call the update_task_progress tool for every non-trivial "
                "user request. This is mandatory, not optional.\n\n"
                "Each item in the items array has these fields:\n"
                "- label: short Chinese step title (required)\n"
                '- status: "todo" | "running" | "done" (required)\n'
                "- id: unique step identifier (optional, auto-generated)\n\n"
                "CRITICAL RULES:\n"
                "- Call update_task_progress BEFORE your first tool call or substantive "
                "action, with 3-6 short Chinese step titles.\n"
                "- After finishing each step, call update_task_progress again to "
                "mark it done and advance the next step to running.\n"
                "- Always keep EXACTLY ONE step in 'running' status.\n"
                '- When fully done, call with phase_status="completed" and all steps '
                'marked "done".\n\n'
                'SKIP ONLY for: pure chitchat ("hello"), simple knowledge questions '
                '("what is Python"), or single-command requests ("run npm install").\n'
                "For analysis, coding, debugging, refactoring, optimization, or any "
                "multi-step request — ALWAYS use the tool. When in doubt, use it."
            )

        sys_prompt += _OPERATION_GROUP_DECLARATION_INSTRUCTION

        return sys_prompt

    def _setup_memory_manager(
        self,
        enable_memory_manager: bool,
        memory_manager: BaseMemoryManager | None,
        namesake_strategy: NamesakeStrategy,
    ) -> None:
        """Setup memory manager and register memory search tool if enabled.

        Args:
            enable_memory_manager: Whether to enable memory manager
            memory_manager: Optional memory manager instance
            namesake_strategy: Strategy to handle namesake tool functions
        """
        # Check env var: if ENABLE_MEMORY_MANAGER=false, disable memory manager
        env_enable_mm = os.getenv("ENABLE_MEMORY_MANAGER", "")
        if env_enable_mm.lower() == "false":
            enable_memory_manager = False

        self._enable_memory_manager: bool = enable_memory_manager
        self.memory_manager = memory_manager

        # Register memory_search tool if enabled and available
        if self._enable_memory_manager and self.memory_manager is not None:
            # update memory manager
            chat_id = self._request_context.get("chat_id") or None
            create_request_memory = getattr(
                self.memory_manager,
                "create_request_memory",
                None,
            )
            if chat_id and callable(create_request_memory):
                self.memory = create_request_memory(chat_id)
            else:
                self.memory = self.memory_manager.get_in_memory_memory(
                    chat_id=chat_id,
                )

            # Register memory_search as a tool function
            self.toolkit.register_tool_function(
                create_memory_search_tool(self.memory_manager),
                namesake_strategy=namesake_strategy,
            )
            logger.debug("Registered memory_search tool")
            chat_id = self._request_context.get("chat_id") or None
            checkpoint_store = getattr(
                self.memory,
                "chat_checkpoint_store",
                None,
            )
            if chat_id and checkpoint_store is not None:
                try:
                    state = checkpoint_store._read_checkpoint_state(chat_id)
                except (TypeError, ValueError):
                    logger.warning(
                        "Skip recover_evidence: request Chat ID is invalid",
                    )
                else:
                    self.toolkit.register_tool_function(
                        create_recover_evidence_tool(
                            self.memory_manager,
                            chat_id=chat_id,
                            epoch=state.current_epoch,
                        ),
                        namesake_strategy=namesake_strategy,
                    )
                    logger.debug(
                        "Registered request-bound recover_evidence tool",
                    )

    def _register_hooks(self) -> None:
        """Register pre-reasoning and pre-acting hooks."""
        if self._request_context.get("agent_role") == "completion_judge":
            return
        # Bootstrap hook - checks BOOTSTRAP.md on first interaction
        # Use workspace_dir if available, else fallback to WORKING_DIR
        working_dir = (
            self._workspace_dir if self._workspace_dir else WORKING_DIR
        )
        bootstrap_hook = BootstrapHook(
            working_dir=working_dir,
            language=self._language,
        )
        self.register_instance_hook(
            hook_type="pre_reasoning",
            hook_name="bootstrap_hook",
            hook=bootstrap_hook.__call__,
        )
        logger.debug("Registered bootstrap hook")

        # Memory compaction hook - auto-compact when context is full
        if self._enable_memory_manager and self.memory_manager is not None:
            memory_compact_hook = MemoryCompactionHook(
                memory_manager=self.memory_manager,
            )
            self.register_instance_hook(
                hook_type="pre_reasoning",
                hook_name="memory_compact_hook",
                hook=memory_compact_hook.__call__,
            )
            logger.debug("Registered memory compaction hook")

    def rebuild_sys_prompt(self) -> None:
        """Rebuild and replace the system prompt.

        Useful after load_session_state to ensure the prompt reflects
        the latest AGENTS.md / SOUL.md / PROFILE.md on disk.

        Updates both self._sys_prompt and the first system-role
        message stored in self.memory.content (if one exists).
        """
        current_token = self._current_system_prompt_freshness_token()
        if (
            getattr(self, "_sys_prompt", None) is None
            or getattr(self, "_sys_prompt_freshness_token", None)
            != current_token
        ):
            self._sys_prompt = self._build_sys_prompt()
            current_token = self._current_system_prompt_freshness_token()
            self._sys_prompt_freshness_token = current_token

        if self.memory is None:
            logger.warning(
                "rebuild_sys_prompt: self.memory is None, "
                "skipping in-memory system prompt update.",
            )
            return

        for msg, _marks in self.memory.content:
            if msg.role == "system":
                msg.content = (
                    self.sys_prompt
                    if hasattr(self, "toolkit")
                    else self._sys_prompt
                )
            break

    async def register_mcp_clients(
        self,
        namesake_strategy: NamesakeStrategy = "skip",
    ) -> None:
        """Register MCP clients on this agent's toolkit after construction.

        Args:
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
        """
        await McpToolRegistrar.from_agent(self).register_clients(
            namesake_strategy=namesake_strategy,
        )

    # ------------------------------------------------------------------
    # Watchdog: detect and recover from agent stalls.
    # ------------------------------------------------------------------

    _watchdog_task: asyncio.Task | None = None
    _WATCHDOG_IDLE_SENSITIVE_PHASES = {
        AgentPhase.REASONING,
        AgentPhase.SUMMARIZING,
        AgentPhase.IDLE,
        AgentPhase.UNKNOWN,
    }

    @staticmethod
    def _phase_clock() -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return time.monotonic()

    def _init_agent_phase_state(self) -> None:
        now = self._phase_clock()
        self._agent_phase_state = AgentPhaseState(
            phase=AgentPhase.IDLE,
            started_at=now,
            last_activity_at=now,
        )

    def _ensure_agent_phase_state(self) -> AgentPhaseState:
        state = getattr(self, "_agent_phase_state", None)
        if state is None:
            self._init_agent_phase_state()
            state = self._agent_phase_state
        return state

    def _record_agent_activity(self, reason: str | None = None) -> None:
        state = self._ensure_agent_phase_state()
        state.last_activity_at = self._phase_clock()
        if reason:
            state.reason = reason

    @contextmanager
    def agent_phase(
        self,
        phase: AgentPhase | str,
        *,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        reason: str | None = None,
    ):
        previous = replace(self._ensure_agent_phase_state())
        now = self._phase_clock()
        self._agent_phase_state = AgentPhaseState(
            phase=AgentPhase(phase),
            started_at=now,
            last_activity_at=now,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            reason=reason,
        )
        try:
            yield self._agent_phase_state
        finally:
            self._agent_phase_state = replace(
                previous,
                last_activity_at=self._phase_clock(),
            )

    def _watchdog_diagnostic_fields(
        self,
        state: AgentPhaseState,
        now: float,
        threshold: float,
    ) -> dict[str, str | float]:
        request_context = getattr(self, "_request_context", {}) or {}
        return {
            "phase": state.phase.value,
            "phase_duration": max(now - state.started_at, 0.0),
            "silence_duration": max(now - state.last_activity_at, 0.0),
            "threshold": threshold,
            "session_id": str(request_context.get("session_id") or ""),
            "user_id": str(request_context.get("user_id") or ""),
            "agent_id": str(request_context.get("agent_id") or ""),
            "tool_name": state.tool_name or "",
            "tool_call_id": state.tool_call_id or "",
            "reason": state.reason or "",
        }

    def _log_watchdog_diagnostic(
        self,
        *,
        event: str,
        policy: str,
        state: AgentPhaseState,
        now: float,
        threshold: float,
    ) -> None:
        fields = self._watchdog_diagnostic_fields(state, now, threshold)
        logger.warning(
            "Agent watchdog %s: policy=%s phase=%s "
            "phase_duration=%.3fs silence_duration=%.3fs threshold=%.3fs "
            "session_id=%s user_id=%s agent_id=%s tool_name=%s "
            "tool_call_id=%s reason=%s",
            event,
            policy,
            fields["phase"],
            fields["phase_duration"],
            fields["silence_duration"],
            fields["threshold"],
            fields["session_id"],
            fields["user_id"],
            fields["agent_id"],
            fields["tool_name"],
            fields["tool_call_id"],
            fields["reason"],
        )

    def _start_watchdog(
        self,
        timeout: float = AGENT_WATCHDOG_TIMEOUT,
        *,
        check_interval: float | None = None,
    ) -> None:
        """Start a phase-aware watchdog for the active reply task.

        Args:
            timeout: Maximum seconds of idle-sensitive silence before interrupt
                (default: ``AGENT_WATCHDOG_TIMEOUT``).
            check_interval: Optional polling interval for tests and tuning.
        """
        self._stop_watchdog()
        self._ensure_agent_phase_state()
        interval = (
            float(check_interval)
            if check_interval is not None
            else min(max(timeout / 4.0, 0.5), 5.0)
        )
        interval = max(interval, 0.001)

        async def _watchdog() -> None:
            last_non_interrupt_log_at = 0.0
            try:
                while True:
                    await asyncio.sleep(interval)
                    state = replace(self._ensure_agent_phase_state())
                    now = self._phase_clock()
                    silence_duration = now - state.last_activity_at
                    if silence_duration < timeout:
                        continue

                    if state.phase in self._WATCHDOG_IDLE_SENSITIVE_PHASES:
                        self._log_watchdog_diagnostic(
                            event="interrupt",
                            policy="idle_sensitive",
                            state=state,
                            now=now,
                            threshold=timeout,
                        )
                        if self._reply_task and not self._reply_task.done():
                            self._reply_task.cancel(
                                asyncio.CancelledError(
                                    "Agent watchdog: "
                                    f"phase={state.phase.value} "
                                    f"silent for {silence_duration:.0f}s",
                                ),
                            )
                        return

                    if now - last_non_interrupt_log_at >= timeout:
                        last_non_interrupt_log_at = now
                        self._log_watchdog_diagnostic(
                            event="silent_phase",
                            policy="tool_or_async_phase_non_interrupting",
                            state=state,
                            now=now,
                            threshold=timeout,
                        )
            except asyncio.CancelledError:
                return

        self._watchdog_task = asyncio.create_task(_watchdog())

    def _reset_watchdog(self, timeout: float = AGENT_WATCHDOG_TIMEOUT) -> None:
        """Record Agent activity without changing the active phase.

        Args:
            timeout: Kept for compatibility with older call sites.
        """
        del timeout
        self._record_agent_activity(reason="output")

    def _stop_watchdog(self) -> None:
        """Stop the watchdog without interrupting the agent."""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    # ------------------------------------------------------------------
    # Media-block fallback: strip unsupported media blocks (image, audio,
    # video) from memory and retry when the model rejects them.
    # ------------------------------------------------------------------

    _MEDIA_BLOCK_TYPES = {"image", "audio", "video"}

    def _proactive_strip_media_blocks(self) -> int:
        """Proactively strip media blocks from memory before model call.

        Only called when the active model does not support multimodal.
        Returns the number of blocks stripped.
        """
        return self._strip_media_blocks_from_memory()

    @chat_traced(
        request_model_factory=lambda self, *args, **kwargs: (
            self._resolved_model_slot.get("model")
        ),
        provider_name_factory=lambda self, *args, **kwargs: (
            self._resolved_model_slot.get("provider_id")
        ),
        output_arguments_factory=build_chat_output_arguments,
    )
    async def _run_reasoning_with_internal_context(
        self,
        tool_choice: Literal["auto", "none", "required"] | None = None,
    ) -> Msg:
        """把 accepted plan 内部上下文只注入当前推理轮次。"""
        request_context = getattr(self, "_request_context", {}) or {}
        internal_msgs = _build_accepted_plan_tool_exchange(request_context)
        if not internal_msgs:
            return await super()._reasoning(tool_choice=tool_choice)

        # 复用 AgentScope 的 HINT 标记，把内部上下文仅注入当前推理轮次，
        # 避免进入会话持久化、工具执行和前端工具卡片路径。
        for internal_msg in internal_msgs:
            await self.memory.add(internal_msg, marks=_MemoryMark.HINT)
        try:
            return await super()._reasoning(tool_choice=tool_choice)
        finally:
            await self.memory.delete_by_mark(mark=_MemoryMark.HINT)

    async def _reasoning(
        self,
        tool_choice: Literal["auto", "none", "required"] | None = None,
    ) -> Msg:
        """Override reasoning with proactive media filtering.

        1. Proactive layer: if the model does not support
           multimodal, strip media blocks *before* calling.
        2. Passive layer: if the model call still fails with a
           bad-request / media error, strip remaining blocks and retry.
        3. If the model IS marked as multimodal but still errors on
           media, log a warning about possibly inaccurate capability flag.

        Calls ``super()._reasoning`` to keep the ToolGuardMixin
        interception active.
        """
        with self.agent_phase(AgentPhase.REASONING, reason="reasoning"):
            # --- Proactive filtering layer ---
            if not get_active_model_supports_multimodal():
                n = self._proactive_strip_media_blocks()
                if n > 0:
                    logger.warning(
                        "Proactively stripped %d media block(s) - "
                        "model does not support multimodal.",
                        n,
                    )

            # --- Passive fallback layer (existing logic) ---
            try:
                return await self._run_reasoning_with_internal_context(
                    tool_choice=tool_choice,
                )
            except Exception as e:
                if not self._is_bad_request_or_media_error(e):
                    raise

                n_stripped = self._strip_media_blocks_from_memory()
                if n_stripped == 0:
                    raise

                # If the model is marked as multimodal but still
                # errored, the capability flag may be wrong.
                if get_active_model_supports_multimodal():
                    logger.warning(
                        "Model marked multimodal but "
                        "rejected media. "
                        "Capability flag may be wrong.",
                    )

                logger.warning(
                    "_reasoning failed (%s). "
                    "Stripped %d media block(s) from memory, retrying.",
                    e,
                    n_stripped,
                )
                return await self._run_reasoning_with_internal_context(
                    tool_choice=tool_choice,
                )

    async def _summarizing(self) -> Msg:
        """Override summarizing with proactive media filtering,
        passive fallback, and tool_use block filtering.

        1. Proactive layer: if the model does not support multimodal,
           strip media blocks *before* calling the model.
        2. Passive layer: if the model call still fails with a
           bad-request / media error, strip remaining blocks and retry.
        3. If the model IS marked as multimodal but still errors on
           media, log a warning about possibly inaccurate capability flag.

        Some models (e.g. kimi-k2.5) generate tool_use blocks even when
        no tools are provided.  We set ``_in_summarizing`` so that
        ``print`` can strip tool_use blocks from streaming chunks.
        """
        with self.agent_phase(AgentPhase.SUMMARIZING, reason="summarizing"):
            # --- Proactive filtering layer ---
            if not get_active_model_supports_multimodal():
                n = self._proactive_strip_media_blocks()
                if n > 0:
                    logger.warning(
                        "Proactively stripped %d media block(s) - "
                        "model does not support multimodal.",
                        n,
                    )

            # --- Passive fallback layer ---
            self._in_summarizing = True
            try:
                try:
                    msg = await super()._summarizing()
                except Exception as e:
                    if not self._is_bad_request_or_media_error(e):
                        raise

                    n_stripped = self._strip_media_blocks_from_memory()
                    if n_stripped == 0:
                        raise

                    if get_active_model_supports_multimodal():
                        logger.warning(
                            "Model marked multimodal but "
                            "rejected media. "
                            "Capability flag may be wrong.",
                        )

                    logger.warning(
                        "_summarizing failed (%s). "
                        "Stripped %d media block(s) from memory, retrying.",
                        e,
                        n_stripped,
                    )
                    msg = await super()._summarizing()
            finally:
                self._in_summarizing = False

            metadata = getattr(msg, "metadata", None)
            if isinstance(metadata, dict) and metadata.pop(
                PLAN_INTERACTION_SUMMARIZING_SHORT_CIRCUIT_METADATA_KEY,
                False,
            ):
                return msg
            return self._strip_tool_use_from_msg(msg)

    async def print(
        self,
        msg: Msg,
        last: bool = True,
        speech: Any = None,
    ) -> None:
        """Filter tool_use blocks during _summarizing before they hit the
        message queue, preventing the frontend from briefly rendering
        phantom tool calls that will never be executed.

        On the *final* streaming event (``last=True``), append the
        round-end notice so users see it immediately instead of only
        after a page refresh.  Intermediate events that become empty
        after filtering are silently skipped to avoid blank UI flashes.

        Also records Agent activity on each output event without erasing
        the active phase metadata.
        """
        self._record_agent_activity(reason="output")

        if not getattr(self, "_in_summarizing", False):
            return await super().print(msg, last, speech=speech)

        original = msg.content
        modified = False

        if isinstance(original, list):
            filtered = [
                b
                for b in original
                if not (isinstance(b, dict) and b.get("type") == "tool_use")
            ]
            if not filtered and not last:
                return
            if len(filtered) != len(original) or last:
                msg.content = filtered
                if last:
                    msg.content.append(
                        {"type": "text", "text": self._ROUND_END_NOTICE},
                    )
                modified = True
        elif isinstance(original, str) and last:
            msg.content = original + self._ROUND_END_NOTICE
            modified = True
        if modified:
            try:
                return await super().print(msg, last, speech=speech)
            finally:
                msg.content = original
        return await super().print(msg, last, speech=speech)

    _ROUND_END_NOTICE = (
        "\n\n---\n"
        "本轮调用已达最大次数，回复已终止，请继续输入。\n"
        "Maximum iterations reached for this round. "
        "Please send a new message to continue."
    )

    @staticmethod
    def _strip_tool_use_from_msg(msg: Msg) -> Msg:
        """Remove tool_use blocks from a message and append a user notice.

        When _summarizing is called without tools, some models still
        return tool_use blocks.  Those blocks can never be executed, so
        strip them and append a bilingual notice telling the user this
        round of calls has ended.
        """
        if isinstance(msg.content, str):
            msg.content += SWEAgent._ROUND_END_NOTICE
            return msg

        filtered = [
            block
            for block in msg.content
            if not (
                isinstance(block, dict) and block.get("type") == "tool_use"
            )
        ]

        n_removed = len(msg.content) - len(filtered)
        if n_removed:
            logger.debug(
                "Stripped %d tool_use block(s) from _summarizing response",
                n_removed,
            )

        filtered.append({"type": "text", "text": SWEAgent._ROUND_END_NOTICE})
        msg.content = filtered
        return msg

    @staticmethod
    def _is_bad_request_or_media_error(exc: Exception) -> bool:
        """Return True for 400-class or media-related model errors.

        Targets bad-request (400) errors because unsupported media
        content typically causes request validation failures.  Keyword
        matching provides an extra safety net for providers that use
        non-standard status codes.
        """
        status = getattr(exc, "status_code", None)
        if status == 400:
            return True

        error_str = str(exc).lower()
        keywords = [
            "image",
            "audio",
            "video",
            "vision",
            "multimodal",
            "image_url",
        ]
        return any(kw in error_str for kw in keywords)

    _MEDIA_PLACEHOLDER = (
        "[Media content removed - model does not support this media type]"
    )

    def _strip_media_blocks_from_memory(self) -> int:
        """Remove media blocks (image/audio/video) from all messages.

        Also strips media blocks nested inside ToolResultBlock outputs.
        Inserts placeholder text when stripping leaves content empty to
        avoid malformed API requests.

        Returns:
            Total number of media blocks removed.
        """
        media_types = self._MEDIA_BLOCK_TYPES
        total_stripped = 0

        for msg, _marks in self.memory.content:
            if not isinstance(msg.content, list):
                continue

            new_content = []
            for block in msg.content:
                if (
                    isinstance(block, dict)
                    and block.get("type") in media_types
                ):
                    total_stripped += 1
                    continue

                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and isinstance(block.get("output"), list)
                ):
                    original_len = len(block["output"])
                    block["output"] = [
                        item
                        for item in block["output"]
                        if not (
                            isinstance(item, dict)
                            and item.get("type") in media_types
                        )
                    ]
                    stripped_count = original_len - len(block["output"])
                    total_stripped += stripped_count
                    if stripped_count > 0 and not block["output"]:
                        block["output"] = self._MEDIA_PLACEHOLDER

                new_content.append(block)

            if not new_content and total_stripped > 0:
                new_content.append(
                    {"type": "text", "text": self._MEDIA_PLACEHOLDER},
                )

            msg.content = new_content

        return total_stripped

    async def run_research_phase(
        self,
        msg: Msg | list[Msg] | None,
    ) -> ResearchPhaseResult:
        """Run a bounded tool-enabled ReAct loop for a SubAgent research phase.

        Unlike :meth:`reply`, reaching ``max_iters`` is reported explicitly
        instead of invoking AgentScope's free-form summarization fallback.
        """
        from ..config.context import (
            set_current_task_progress_chat_id,
            set_current_task_progress_tracker,
            set_current_task_progress_turn_id,
            set_current_workspace_dir,
            set_current_recent_max_bytes,
        )
        from ..app.source_system_config import (
            resolve_tool_result_compact_config,
        )

        set_current_workspace_dir(self._workspace_dir)
        tool_result_compact = resolve_tool_result_compact_config(
            self._agent_config.running.tool_result_compact,
        )
        set_current_recent_max_bytes(tool_result_compact.recent_max_bytes)
        set_current_task_progress_tracker(self._task_tracker)
        set_current_task_progress_chat_id(
            self._request_context.get("chat_id"),
        )
        set_current_task_progress_turn_id(
            self._request_context.get("turn_id"),
        )
        if msg is not None:
            await process_file_and_media_blocks_in_message(msg)
        await self.memory.add(msg)
        await self._retrieve_from_long_term_memory(msg)
        await self._retrieve_from_knowledge(msg)

        last_reply: Msg | None = None
        previous_reply_task = self._reply_task
        self._reply_task = asyncio.current_task()
        self._required_structured_model = None
        self.toolkit.remove_tool_function(self.finish_function_name)
        self._start_watchdog()
        turn_callback = getattr(self, "_subagent_turn_callback", None)
        try:
            with self.agent_phase(AgentPhase.REASONING, reason="research"):
                for turn in range(1, self.max_iters + 1):
                    await self._compress_memory_if_needed()
                    reply = await self._reasoning()
                    last_reply = reply
                    if turn_callback is not None:
                        await turn_callback(turn)
                    tool_calls = reply.get_content_blocks("tool_use")
                    if not tool_calls:
                        return ResearchPhaseResult(
                            status="completed",
                            reply=reply,
                            turns_used=turn,
                            messages=await self._research_messages(),
                        )

                    futures = [
                        self._acting(tool_call) for tool_call in tool_calls
                    ]
                    if self.parallel_tool_calls:
                        await asyncio.gather(*futures)
                    else:
                        for future in futures:
                            await future

            return ResearchPhaseResult(
                status="turn_limit_reached",
                reply=last_reply,
                turns_used=self.max_iters,
                messages=await self._research_messages(),
            )
        finally:
            self._stop_watchdog()
            self._reply_task = previous_reply_task

    async def _research_messages(self) -> tuple[Msg, ...]:
        """Snapshot research memory for a bounded terminal handoff."""
        return tuple(await self.memory.get_memory())

    # pylint: disable=protected-access
    async def reply(
        self,
        msg: Msg | list[Msg] | None = None,
        structured_model: Type[BaseModel] | None = None,
    ) -> Msg:
        """Override reply to process file blocks and handle commands.

        Args:
            msg: Input message(s) from user
            structured_model: Optional pydantic model for structured output

        Returns:
            Response message
        """
        # Set workspace_dir and recent_max_bytes in context for tool functions
        from ..config.context import (
            set_current_task_progress_chat_id,
            set_current_task_progress_tracker,
            set_current_task_progress_turn_id,
            set_current_workspace_dir,
            set_current_recent_max_bytes,
        )
        from ..app.source_system_config import (
            resolve_tool_result_compact_config,
        )

        set_current_workspace_dir(self._workspace_dir)
        tool_result_compact = resolve_tool_result_compact_config(
            self._agent_config.running.tool_result_compact,
        )
        set_current_recent_max_bytes(tool_result_compact.recent_max_bytes)
        set_current_task_progress_tracker(self._task_tracker)
        set_current_task_progress_chat_id(
            self._request_context.get("chat_id"),
        )
        set_current_task_progress_turn_id(
            self._request_context.get("turn_id"),
        )

        # Process file and media blocks in messages
        if msg is not None:
            await process_file_and_media_blocks_in_message(msg)

        # Check if message is a system command
        last_msg = msg[-1] if isinstance(msg, list) else msg
        query = (
            last_msg.get_text_content() if isinstance(last_msg, Msg) else None
        )

        if self.command_handler.is_command(query):
            logger.info(f"Received command: {query}")
            msg = await self.command_handler.handle_command(query)
            await self.print(msg)
            return msg

        # Normal message processing
        logger.info("SWEAgent.reply: max_iters=%s", self.max_iters)

        if hasattr(self.memory, "_long_term_memory"):
            running = self._agent_config.running
            ms = running.memory_summary
            if (
                ms.force_memory_search
                and self.memory_manager is not None
                and query
            ):
                try:
                    result = await asyncio.wait_for(
                        self.memory_manager.memory_search(
                            query=query[:100],
                            max_results=ms.force_max_results,
                            min_score=ms.force_min_score,
                        ),
                        timeout=1,
                    )
                    self.memory._long_term_memory = "\n".join(
                        block["text"]
                        for block in (result.content or [])
                        if isinstance(block, dict) and block.get("text")
                    )
                except Exception as e:
                    logger.warning(
                        "force_memory_search failed or timed out,"
                        f" skipping e={e}",
                    )
                    self.memory._long_term_memory = ""
            else:
                self.memory._long_term_memory = ""

        request_context = getattr(self, "_request_context", {}) or {}
        channel_name = request_context.get("channel", "console")
        workspace_dir = Path(self._workspace_dir or WORKING_DIR)
        with apply_skill_config_env_overrides(
            workspace_dir,
            channel_name,
            snapshot=getattr(self, "_workspace_skill_snapshot", None),
        ):
            try:
                self._start_watchdog()
                with self.agent_phase(AgentPhase.REASONING, reason="reply"):
                    return await super().reply(
                        msg=msg,
                        structured_model=structured_model,
                    )
            finally:
                self._stop_watchdog()

    async def interrupt(self, msg: Msg | list[Msg] | None = None) -> None:
        """Interrupt the current reply process and wait for cleanup.

        If the reply task does not finish within
        ``AGENT_INTERRUPT_TIMEOUT`` seconds, the wait is abandoned.
        """
        await self._cancel_selected_expert_run()
        self._stop_watchdog()
        if self._reply_task and not self._reply_task.done():
            task = self._reply_task
            task.cancel(msg)
            try:
                await asyncio.wait_for(task, timeout=AGENT_INTERRUPT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "Agent interrupt timed out (%.0fs) for agent '%s', "
                    "abandoning wait",
                    AGENT_INTERRUPT_TIMEOUT,
                    self.name,
                )
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
            except Exception:
                logger.warning(
                    "Exception occurred during interrupt cleanup",
                    exc_info=True,
                )

    async def _cancel_selected_expert_run(self) -> None:
        """Best-effort cancellation for the expert synchronously awaiting this turn."""
        request_context = getattr(self, "_request_context", {}) or {}
        if not request_context.get("selected_expert_execution"):
            return
        run_id = str(
            request_context.get("selected_expert_run_id") or "",
        ).strip()
        if not run_id:
            return
        agent_config = getattr(self, "_agent_config", None)
        if agent_config is None:
            return
        supervisor = (
            request_context.get("_subagent_supervisor")
            or get_default_background_subagent_supervisor()
        )
        try:
            scope = build_background_subagent_scope(
                parent_agent_config=agent_config,
                request_context=request_context,
            )
            await supervisor.cancel(scope, run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Failed to cancel selected expert run %s during interrupt",
                run_id,
                exc_info=True,
            )
