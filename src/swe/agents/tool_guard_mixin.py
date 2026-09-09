# -*- coding: utf-8 -*-
"""Tool-guard mixin for SWEAgent.

Provides ``_acting`` and ``_reasoning`` overrides that intercept
sensitive tool calls before execution, implementing the deny /
guard / approve flow.

Separated from ``react_agent.py`` to keep the main agent class
focused on lifecycle management.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json as _json
import logging
import os
from pathlib import Path
import shlex
import time
import uuid as _uuid
from typing import Any, Literal, NoReturn

from agentscope.message import Msg, ToolResultBlock

from ..constant import AGENT_WATCHDOG_TIMEOUT, QUERY_TIMEOUT_SECONDS
from ..tracing.agent_trace_sdk import execute_tool_traced
from ..app.runner.tool_output_frames import tool_output_invocation
from .agent_trace_output import ToolTraceOutcome, build_tool_output_arguments
from .hook_runtime import HookRuntime
from .hook_runtime.conversation_snapshot import capture_conversation_snapshot
from .hook_runtime.models import (
    HookConfig,
    HookContext,
    HookDecision,
    HookEventName,
    HookSessionState,
    MergedHookResult,
)
from .tool_failure import (
    attach_tool_governance_message_metadata,
    build_failed_tool_result_block,
)
from ..security.tool_guard.models import TOOL_GUARD_DENIED_MARK
from ..tracing import has_trace_manager, get_trace_manager, get_current_trace

logger = logging.getLogger(__name__)


def _current_task_label() -> str:
    """返回当前 asyncio task 的诊断标识。"""
    task = asyncio.current_task()
    if task is None:
        return "no-task"
    return f"task-{id(task)}"


def _goal_tool_may_write_environment(
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """Conservatively track real Goal-turn writes for contract rechecks."""
    if tool_name in _GOAL_ENVIRONMENT_WRITE_TOOLS:
        return True
    if tool_name != "execute_shell_command":
        return False
    command = f" {str(tool_input.get('command') or '').lower()} "
    return any(token in command for token in _GOAL_MUTATING_SHELL_TOKENS)


def _trace_field(trace_ctx: Any, field: str, default: Any = "") -> Any:
    """安全读取 trace 上下文字段，避免诊断日志反向打断 tracing。"""
    if trace_ctx is None:
        return default
    return getattr(trace_ctx, field, default)


_DEFAULT_LOCAL_TOOL_EXECUTION_HARD_TIMEOUT = min(
    QUERY_TIMEOUT_SECONDS,
    max(AGENT_WATCHDOG_TIMEOUT * 2.0, AGENT_WATCHDOG_TIMEOUT + 60.0),
)
try:
    LOCAL_TOOL_EXECUTION_HARD_TIMEOUT = max(
        float(
            os.environ.get(
                "SWE_LOCAL_TOOL_EXECUTION_HARD_TIMEOUT",
                str(_DEFAULT_LOCAL_TOOL_EXECUTION_HARD_TIMEOUT),
            ),
        ),
        1.0,
    )
except (TypeError, ValueError):
    LOCAL_TOOL_EXECUTION_HARD_TIMEOUT = (
        _DEFAULT_LOCAL_TOOL_EXECUTION_HARD_TIMEOUT
    )

_TOOLS_WITH_SPECIFIC_TIMEOUTS = {
    "execute_shell_command",
    "grep_search",
    "glob_search",
}
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
        "start_subagent",
        "wait_subagent",
        "get_subagent",
        "cancel_subagent",
    },
)
_PLAN_INTERACTION_TOOL_NAMES = frozenset(
    {
        "ask_plan_clarification",
        "submit_proposed_plan",
        "start_subagent",
        "wait_subagent",
        "get_subagent",
        "cancel_subagent",
    },
)
_PLAN_INTERACTION_CARD_METADATA_KEY = "plan_interaction_card"
PLAN_INTERACTION_SUMMARIZING_SHORT_CIRCUIT_METADATA_KEY = (
    "_plan_interaction_summarizing_short_circuit"
)
_PLAN_INTERACTION_TURN_BOUNDARY_ATTR = (
    "_plan_interaction_turn_boundary_reached"
)
_PLAN_MODE_SHELL_META_CHARS = frozenset((";", "|", ">", "<", "&", "`", "$"))
_PLAN_MODE_GIT_READONLY_SUBCOMMANDS = frozenset(
    ("status", "diff", "grep", "log", "show"),
)
_PLAN_MODE_DENIED_GIT_OPTIONS = frozenset(
    ("--output", "--ext-diff", "--textconv", "-O", "--open-files-in-pager"),
)
_PLAN_MODE_DENIED_READONLY_OPTIONS = frozenset(("--pre",))
_APPROVAL_KIND_TOOL_GUARD = "tool_guard"
_APPROVAL_KIND_HOOK_PRE_TOOL_USE = "hook_pre_tool_use"
_PENDING_TOOL_SKILL_ATTRIBUTIONS_KEY = "_pending_tool_skill_attributions"
_SELECTED_EXPERT_EXECUTION_KEY = "selected_expert_execution"
_SELECTED_EXPERT_RUN_ID_KEY = "selected_expert_run_id"
_SELECTED_EXPERT_WAIT_TIMEOUT_MS = 3000
_GOAL_ENVIRONMENT_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_GOAL_MUTATING_SHELL_TOKENS = frozenset(
    {
        "rm ",
        "mv ",
        "cp ",
        "mkdir",
        "touch",
        "tee ",
        ">",
        ">>",
        "sed -i",
        "git commit",
        "git reset",
        "git checkout",
        "git clean",
        "pip install",
        "npm install",
        "pnpm install",
    },
)


def _subagent_mcp_server_key(
    mcp_server: str | None,
    request_context: dict[str, Any],
) -> str | None:
    """Translate a registered MCP client name back to its snapshot key."""
    if mcp_server is None:
        return None
    mapping = request_context.get("subagent_mcp_server_keys")
    if not isinstance(mapping, dict):
        return mcp_server
    return str(mapping.get(mcp_server, mcp_server))


def _has_plan_mode_shell_structure(command: str) -> bool:
    return any(char in command for char in _PLAN_MODE_SHELL_META_CHARS)


def _looks_like_env_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return bool(separator and name and name.replace("_", "").isalnum())


def _has_denied_option(
    args: list[str],
    denied_options: frozenset[str],
) -> bool:
    return any(
        arg in denied_options
        or any(arg.startswith(f"{option}=") for option in denied_options)
        for arg in args
    )


def _validate_plan_mode_git(tokens: list[str]) -> str | None:
    if len(tokens) < 2:
        return "git command requires a readonly subcommand"
    subcommand = tokens[1]
    if subcommand.startswith("-"):
        return "git global options are unavailable in Plan Mode"
    if subcommand not in _PLAN_MODE_GIT_READONLY_SUBCOMMANDS:
        return "git subcommand is unavailable in Plan Mode"
    if _has_denied_option(tokens[2:], _PLAN_MODE_DENIED_GIT_OPTIONS):
        return "git command option may write files or execute external helpers"
    return None


def _validate_plan_mode_readonly_shell(command: str) -> str | None:
    if not command:
        return "empty shell command"
    if _has_plan_mode_shell_structure(command):
        return "shell compound syntax is unavailable in Plan Mode"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "shell command could not be parsed safely"
    if not tokens:
        return "empty shell command"
    if _looks_like_env_assignment(tokens[0]):
        return "environment assignments are unavailable in Plan Mode"
    if tokens[0] in {"pwd", "ls"}:
        return None
    if tokens[0] in {"rg", "grep"}:
        if _has_denied_option(tokens[1:], _PLAN_MODE_DENIED_READONLY_OPTIONS):
            return "search command option may execute external helpers"
        return None
    if tokens[0] == "git":
        return _validate_plan_mode_git(tokens)
    return "shell command is not in the Plan Mode readonly allowlist"


class PreToolUseTerminalStop(Exception):
    """Signal that a PreToolUse hook terminated the current agent turn."""

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason or "Hook requested stop"
        super().__init__(self.reason)


class _GuardAction:
    """Lightweight container for a guard decision made under lock."""

    __slots__ = ("kind", "tool_name", "tool_input", "guard_result")

    def __init__(
        self,
        kind: str,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        guard_result: Any = None,
    ) -> None:
        self.kind = kind
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.guard_result = guard_result


class ToolGuardMixin:
    """Mixin that adds tool-guard interception to a ReActAgent.

    At runtime this class is always combined with
    ``agentscope.agent.ReActAgent`` via MRO, so ``super()._acting``
    and ``super()._reasoning`` resolve to the concrete agent methods.
    """

    # ------------------------------------------------------------------
    # Lazy initialisation
    # ------------------------------------------------------------------

    def _init_tool_guard(self) -> None:
        """Lazy-init tool-guard components (called once)."""
        from swe.security.tool_guard.engine import get_guard_engine
        from swe.app.approvals import get_approval_service

        self._tool_guard_engine = get_guard_engine()
        self._tool_guard_approval_service = get_approval_service()
        self._tool_guard_pending_info: dict | None = None
        self._tool_guard_lock = asyncio.Lock()
        self._pre_tool_terminal_stop_reason: str | None = None
        self._active_tool_guard_acting_tasks: set[asyncio.Task[Any]] = set()

    def _ensure_tool_guard(self) -> None:
        if not hasattr(self, "_tool_guard_engine"):
            self._init_tool_guard()

    def _ensure_pre_tool_terminal_stop_tracking(self) -> None:
        if not hasattr(self, "_pre_tool_terminal_stop_reason"):
            self._pre_tool_terminal_stop_reason = None
        if not hasattr(self, "_active_tool_guard_acting_tasks"):
            self._active_tool_guard_acting_tasks = set()

    def consume_pre_tool_terminal_stop(self) -> str | None:
        """Return and clear the terminal stop state for the completed turn."""
        self._ensure_pre_tool_terminal_stop_tracking()
        reason = self._pre_tool_terminal_stop_reason
        self._pre_tool_terminal_stop_reason = None
        return reason

    def reset_pre_tool_terminal_stop(self) -> None:
        """Clear terminal stop state before beginning a new agent turn."""
        self.consume_pre_tool_terminal_stop()

    def _raise_if_pre_tool_terminal_stop_requested(self) -> None:
        self._ensure_pre_tool_terminal_stop_tracking()
        if self._pre_tool_terminal_stop_reason is not None:
            raise PreToolUseTerminalStop(self._pre_tool_terminal_stop_reason)

    def _request_pre_tool_terminal_stop(self, reason: str) -> None:
        self._ensure_pre_tool_terminal_stop_tracking()
        self._pre_tool_terminal_stop_reason = reason
        stopping_task = asyncio.current_task()
        for task in tuple(self._active_tool_guard_acting_tasks):
            if task is not stopping_task and not task.done():
                task.cancel()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _agent_phase_context(
        self,
        phase: str,
        *,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        reason: str | None = None,
    ):
        enter_phase = getattr(self, "agent_phase", None)
        if enter_phase is None:
            return nullcontext()
        return enter_phase(
            phase,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            reason=reason,
        )

    def _tool_has_specific_timeout(self, tool_name: str) -> bool:
        if self._resolve_mcp_server(tool_name):
            return True
        return tool_name in _TOOLS_WITH_SPECIFIC_TIMEOUTS

    async def _run_plan_interaction_tool_call(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
    ) -> dict | None:
        """Execute a plan interaction and preserve its card metadata."""
        tool_res_msg = Msg(
            "system",
            [
                ToolResultBlock(
                    type="tool_result",
                    id=tool_call["id"],
                    name=tool_name,
                    output=[],
                ),
            ],
            "system",
        )
        try:
            tool_res = await self.toolkit.call_tool_function(tool_call)
            async for chunk in tool_res:
                tool_res_msg.content[0]["output"] = chunk.content
                if chunk.metadata:
                    tool_res_msg.metadata.update(chunk.metadata)
                    if isinstance(
                        chunk.metadata.get(
                            _PLAN_INTERACTION_CARD_METADATA_KEY,
                        ),
                        dict,
                    ):
                        setattr(
                            self,
                            _PLAN_INTERACTION_TURN_BOUNDARY_ATTR,
                            True,
                        )
                await self.print(tool_res_msg, chunk.is_last)
                if chunk.is_interrupted:
                    raise asyncio.CancelledError()
            return None
        finally:
            await self.memory.add(tool_res_msg)

    async def _run_tool_call_with_hard_timeout_impl(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict | None:
        """Run a local tool under the generic hard timeout when applicable."""
        tool_call_id = str(tool_call.get("id") or "")
        with self._agent_phase_context(
            "tool_execution",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            reason="tool_execution",
        ):
            with tool_output_invocation(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            ):
                if self._tool_has_specific_timeout(tool_name):
                    return await super()._acting(tool_call)  # type: ignore[misc]

                started_at = time.monotonic()
                try:
                    if tool_name in _PLAN_INTERACTION_TOOL_NAMES and hasattr(
                        self,
                        "toolkit",
                    ):
                        return await asyncio.wait_for(
                            self._run_plan_interaction_tool_call(
                                tool_call,
                                tool_name,
                            ),
                            timeout=LOCAL_TOOL_EXECUTION_HARD_TIMEOUT,
                        )
                    return await asyncio.wait_for(
                        super()._acting(tool_call),  # type: ignore[misc]
                        timeout=LOCAL_TOOL_EXECUTION_HARD_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - started_at
                    timeout_text = (
                        f"Error: Tool {tool_name} timed out after "
                        f"{LOCAL_TOOL_EXECUTION_HARD_TIMEOUT:.2f}s "
                        f"(elapsed {elapsed:.2f}s)."
                    )
                    logger.warning(
                        "Local tool hard timeout: tool_name=%s tool_call_id=%s "
                        "elapsed=%.3fs timeout=%.3fs",
                        tool_name,
                        tool_call_id,
                        elapsed,
                        LOCAL_TOOL_EXECUTION_HARD_TIMEOUT,
                    )
                    await self._persist_local_tool_timeout_result(
                        tool_call_id,
                        tool_name,
                        timeout_text,
                    )
                    return None

    @execute_tool_traced(
        tool_name_factory=lambda self, _call, tool_name, _input: tool_name,
        input_arguments_factory=lambda self, _call, _tool_name, tool_input: tool_input,
        output_arguments_factory=build_tool_output_arguments,
    )
    async def _run_tool_call_with_hard_timeout_traced(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolTraceOutcome:
        result = await self._run_tool_call_with_hard_timeout_impl(
            tool_call,
            tool_name,
            tool_input,
        )
        terminal_output = result
        if terminal_output is None:
            terminal_output = self._extract_current_tool_response(
                str(tool_call.get("id") or ""),
                include_structured_failure=True,
            )
        return ToolTraceOutcome(
            business_result=result,
            terminal_output=terminal_output,
            tool_name=tool_name,
            mcp_server=self._resolve_mcp_server(tool_name),
        )

    async def _run_tool_call_with_hard_timeout(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict | None:
        outcome = await self._run_tool_call_with_hard_timeout_traced(
            tool_call,
            tool_name,
            tool_input,
        )
        return outcome.business_result

    async def _persist_local_tool_timeout_result(
        self,
        tool_call_id: str,
        tool_name: str,
        timeout_text: str,
    ) -> None:
        """Print and persist the timeout result seen by the next LLM turn."""
        timeout_msg = Msg(
            "system",
            [
                ToolResultBlock(
                    **build_failed_tool_result_block(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        error_type="tool_timeout",
                        detail=timeout_text,
                    ),
                ),
            ],
            "system",
        )

        memory_content = getattr(getattr(self, "memory", None), "content", [])
        if memory_content:
            last_msg, marks = memory_content[-1]
            if (
                TOOL_GUARD_DENIED_MARK not in marks
                and last_msg.role == "system"
                and last_msg.get_content_blocks("tool_result")
                and last_msg.get_content_blocks("tool_result")[0].get("id")
                == tool_call_id
            ):
                last_msg.content = timeout_msg.content
                timeout_msg = last_msg
                await self.print(timeout_msg, True)
                return

        await self.print(timeout_msg, True)
        await self.memory.add(timeout_msg)

    def _should_require_approval(self) -> bool:
        """``True`` when a ``session_id`` is available for approval."""
        return bool(self._request_context.get("session_id"))

    def _last_tool_response_is_denied(self) -> bool:
        """Check if the last message is a guard-denied tool result."""
        if not self.memory.content:
            return False
        msg, marks = self.memory.content[-1]
        return (
            bool(marks)
            and TOOL_GUARD_DENIED_MARK in marks
            and msg.role == "system"
        )

    def _extract_sibling_tool_calls(self) -> list[dict[str, Any]]:
        """Extract all tool_use blocks from the last assistant message."""
        for msg, _ in reversed(self.memory.content):
            if msg.role == "assistant":
                return [
                    {
                        "id": b.get("id", ""),
                        "name": b.get("name", ""),
                        "input": b.get("input", {}),
                    }
                    for b in msg.get_content_blocks("tool_use")
                ]
        return []

    def _tool_result_exists_in_memory(self, tool_use_id: str) -> bool:
        """``True`` when a non-denied tool_result for *tool_use_id* exists."""
        for msg, marks in self.memory.content:
            if msg.role != "system" or TOOL_GUARD_DENIED_MARK in marks:
                continue
            for block in msg.get_content_blocks("tool_result"):
                if block.get("id") == tool_use_id:
                    return True
        return False

    def _extract_current_tool_response(
        self,
        tool_use_id: str,
        *,
        include_structured_failure: bool = False,
    ) -> Any | None:
        """Return the terminal output for the current tool result."""
        if not tool_use_id:
            return None

        content = getattr(getattr(self, "memory", None), "content", None)
        if not isinstance(content, list):
            return None
        memory_entries: list[Any] = content

        for entry in memory_entries[::-1]:
            message = (
                entry[0]
                if isinstance(entry, (tuple, list)) and entry
                else entry
            )
            blocks = getattr(message, "content", None)
            if not isinstance(blocks, list):
                continue
            for block in reversed(blocks):
                block_data = self._tool_result_block_to_dict(block)
                if not block_data:
                    continue
                if block_data.get("type") != "tool_result":
                    continue
                if block_data.get("id") != tool_use_id:
                    continue
                output = block_data.get("output")
                if self._is_structured_failure_output(output):
                    if include_structured_failure:
                        return output
                    return None
                return output
        return None

    @staticmethod
    def _is_structured_failure_output(output: Any) -> bool:
        return isinstance(output, dict) and output.get("isError") is True

    @staticmethod
    def _tool_result_block_to_dict(block: Any) -> dict[str, Any] | None:
        if isinstance(block, dict):
            return block

        model_dump = getattr(block, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(mode="json", exclude_none=True)
            return dumped if isinstance(dumped, dict) else None

        to_dict = getattr(block, "to_dict", None)
        if callable(to_dict):
            dumped = to_dict()
            return dumped if isinstance(dumped, dict) else None

        return None

    def _set_forced_tool_replay_approval(
        self,
        replay_approval: Any,
    ) -> None:
        if isinstance(replay_approval, dict):
            self._tool_guard_replay_approval = dict(replay_approval)
        elif hasattr(self, "_tool_guard_replay_approval"):
            self._tool_guard_replay_approval = None

    def _pop_forced_tool_call(  # pylint: disable=too-many-branches
        self,
    ) -> dict[str, Any] | None:
        """Pop and validate a forced tool call injected by the runner."""
        raw = self._request_context.pop("forced_tool_call_json", "")
        if not raw:
            return None

        try:
            tool_call = _json.loads(str(raw))
        except Exception:
            logger.warning(
                "Tool guard: invalid forced tool call payload",
                exc_info=True,
            )
            return None

        if not isinstance(tool_call, dict):
            logger.warning(
                "Tool guard: forced tool call payload is not a dict",
            )
            return None

        tool_name = tool_call.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            logger.warning(
                "Tool guard: forced tool call missing valid name",
            )
            return None

        tool_input = tool_call.get("input", {})
        if not isinstance(tool_input, dict):
            logger.warning(
                "Tool guard: forced tool call input is not a dict",
            )
            return None

        tool_id = tool_call.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            tool_id = f"approved-{_uuid.uuid4().hex[:12]}"

        siblings = tool_call.pop("_sibling_tool_calls", None)
        remaining = tool_call.pop("_remaining_queue", None)
        thinking_blocks = tool_call.pop("_thinking_blocks", None)
        replay_approval = tool_call.pop("_approval_replay", None)
        self._set_forced_tool_replay_approval(replay_approval)

        if remaining is not None and isinstance(remaining, list):
            self._tool_guard_replay_queue = remaining
        elif siblings is not None and isinstance(siblings, list):
            found = False
            queue: list[dict[str, Any]] = []
            for s in siblings:
                if not found and s.get("id") == tool_id:
                    found = True
                    continue
                if found:
                    queue.append(s)
            self._tool_guard_replay_queue = queue
        else:
            self._tool_guard_replay_queue = []

        result = {
            "id": tool_id,
            "name": tool_name,
            "input": tool_input,
        }

        # Preserve thinking blocks for models that require reasoning_content
        if thinking_blocks is not None and isinstance(thinking_blocks, list):
            result["_thinking_blocks"] = thinking_blocks

        return result

    async def _get_pending_info_for_display(self) -> dict[str, Any]:
        """Return pending tool info for the current waiting card."""
        fallback = getattr(self, "_tool_guard_pending_info", None) or {}
        if fallback.get("request_id"):
            return fallback

        session_id = str(self._request_context.get("session_id") or "")
        if not session_id:
            return fallback

        try:
            pending = (
                await self._tool_guard_approval_service.get_pending_by_session(
                    session_id,
                )
            )
        except Exception:
            logger.warning(
                "Tool guard: failed to read pending queue head",
                exc_info=True,
            )
            return fallback

        if pending is None:
            return fallback

        tool_input: dict[str, Any] = {}
        extra = pending.extra if isinstance(pending.extra, dict) else {}
        tool_call = extra.get("tool_call") if isinstance(extra, dict) else {}
        if isinstance(tool_call, dict) and isinstance(
            tool_call.get("input"),
            dict,
        ):
            tool_input = tool_call["input"]

        return {
            "request_id": pending.request_id or fallback.get("request_id", ""),
            "tool_name": pending.tool_name
            or fallback.get("tool_name", "unknown"),
            "tool_input": tool_input or fallback.get("tool_input", {}),
            "guardians": fallback.get("guardians", []),
        }

    async def _cleanup_tool_guard_denied_messages(
        self,
        include_denial_response: bool = True,
    ) -> None:
        """Remove tool-guard denied messages from memory.

        Finds messages marked with ``TOOL_GUARD_DENIED_MARK`` and
        removes them.  When *include_denial_response* is ``True``,
        also removes the assistant message immediately following the
        last marked message (the LLM's denial explanation).

        When *include_denial_response* is ``False`` (approval granted),
        keeps the waiting-for-approval message but clears its
        ``approval_action`` metadata so the approval card won't render
        on reload, preserving the text content for conversation history.
        """
        ids_to_delete: list[str] = []
        last_marked_idx = -1

        for i, (msg, marks) in enumerate(self.memory.content):
            if TOOL_GUARD_DENIED_MARK in marks:
                ids_to_delete.append(msg.id)
                last_marked_idx = i

        if (
            include_denial_response
            and last_marked_idx >= 0
            and last_marked_idx + 1 < len(self.memory.content)
        ):
            next_msg, _ = self.memory.content[last_marked_idx + 1]
            if next_msg.role == "assistant":
                ids_to_delete.append(next_msg.id)

                # When approval is granted (include_denial_response=False),
        # clear approval_action metadata from the waiting message
        # instead of deleting it, preserving text content.
        if (
            not include_denial_response
            and last_marked_idx >= 0
            and last_marked_idx + 1 < len(self.memory.content)
        ):
            next_msg, marks = self.memory.content[last_marked_idx + 1]
            if next_msg.role == "assistant":
                metadata = getattr(next_msg, "metadata", None)
                if metadata and isinstance(metadata, dict):
                    # Clear approval_action so frontend won't render approval card
                    if "approval_action" in metadata:
                        del metadata["approval_action"]
                        logger.info(
                            "Tool guard: cleared approval_action metadata "
                            "from waiting message (approval granted)",
                        )

        if ids_to_delete:
            removed = await self.memory.delete(ids_to_delete)
            logger.info(
                "Tool guard: cleaned up %d denied message(s)",
                removed,
            )

    # ------------------------------------------------------------------
    # _acting override
    # ------------------------------------------------------------------

    def _resolve_mcp_server(self, tool_name: str) -> str | None:
        """Resolve MCP server name from toolkit registration.

        The tool_call dict from agentscope does not include mcp_server,
        so we look it up from the registered tool function.

        Args:
            tool_name: Name of the tool

        Returns:
            MCP server name if the tool is an MCP tool, None otherwise
        """
        try:
            toolkit = getattr(self, "toolkit", None)
            if toolkit is None:
                return None
            tool_func = toolkit.tools.get(tool_name)
            if tool_func is not None:
                return getattr(tool_func, "mcp_name", None)
        except Exception:
            pass
        return None

    async def _emit_tool_trace_start(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        mcp_server: str | None,
        tool_call_id: str | None = None,
    ) -> str:
        """Emit tool call start trace event.

        Returns span_id or empty string.
        """
        if not has_trace_manager():
            self._discard_precomputed_tool_skill_attribution(tool_call_id)
            return ""
        try:
            trace_ctx = self._resolve_trace_context_for_tracing()
            if trace_ctx:
                logger.debug(
                    "Tool trace start: trace_id=%s user_id=%s session_id=%s "
                    "source_id=%s tool=%s mcp_server=%s task=%s",
                    _trace_field(trace_ctx, "trace_id", None),
                    _trace_field(trace_ctx, "user_id", ""),
                    _trace_field(trace_ctx, "session_id", ""),
                    _trace_field(trace_ctx, "source_id", ""),
                    tool_name,
                    mcp_server,
                    _current_task_label(),
                )
                trace_mgr = get_trace_manager()
                (
                    has_precomputed_attribution,
                    precomputed_attribution,
                ) = self._consume_precomputed_tool_skill_attribution(
                    tool_call_id,
                )
                return await trace_mgr.emit_tool_call_start(
                    trace_id=_trace_field(trace_ctx, "trace_id", ""),
                    tool_name=tool_name,
                    tool_input=tool_input,
                    source_id=_trace_field(trace_ctx, "source_id", ""),
                    user_id=_trace_field(trace_ctx, "user_id", ""),
                    session_id=_trace_field(trace_ctx, "session_id", ""),
                    channel=_trace_field(trace_ctx, "channel", ""),
                    mcp_server=mcp_server,
                    user_name=_trace_field(trace_ctx, "user_name", None),
                    bbk_id=_trace_field(trace_ctx, "bbk_id", None),
                    use_precomputed_attribution=(has_precomputed_attribution),
                    precomputed_attribution=precomputed_attribution,
                )
            self._discard_precomputed_tool_skill_attribution(tool_call_id)
        except Exception as e:
            logger.debug("Failed to emit tool start event: %s", e)
        return ""

    async def _emit_tool_trace_end(
        self,
        span_id: str,
        tool_output: dict | str | None,
        error: str | None = None,
    ) -> None:
        """Emit tool call end trace event.

        处理MCP工具返回的isError字段，确保错误信息正确记录到tracing。
        """
        if not span_id or not has_trace_manager():
            return
        try:
            trace_ctx = self._resolve_trace_context_for_tracing()
            if not trace_ctx:
                return

            logger.debug(
                "Tool trace end: trace_id=%s session_id=%s source_id=%s "
                "span_id=%s task=%s",
                _trace_field(trace_ctx, "trace_id", None),
                _trace_field(trace_ctx, "session_id", ""),
                _trace_field(trace_ctx, "source_id", ""),
                span_id,
                _current_task_label(),
            )
            trace_mgr = get_trace_manager()
            output_str, mcp_error = self._resolve_tool_output_and_error(
                tool_output,
                error,
            )

            await trace_mgr.emit_tool_call_end(
                trace_id=_trace_field(trace_ctx, "trace_id", ""),
                span_id=span_id,
                tool_output=output_str,
                error=mcp_error,
            )
        except Exception as e:
            logger.debug("Failed to emit tool end event: %s", e)

    def _resolve_trace_context_for_tracing(self) -> Any | None:
        """优先使用 request_context 绑定的 trace，上下文缺失时回退。"""
        request_context = getattr(self, "_request_context", {}) or {}
        bound_trace_id = str(request_context.get("trace_id") or "")
        if bound_trace_id:
            current_trace = get_current_trace()
            if current_trace is not None and getattr(
                current_trace,
                "trace_id",
                None,
            ) not in {None, bound_trace_id}:
                logger.warning(
                    "Tool tracing detected mismatched current trace; "
                    "using request-bound trace instead. current=%s bound=%s "
                    "task=%s",
                    getattr(current_trace, "trace_id", None),
                    bound_trace_id,
                    _current_task_label(),
                )
            return type(
                "_RequestTraceContext",
                (),
                {
                    "trace_id": bound_trace_id,
                    "user_id": str(request_context.get("user_id") or ""),
                    "session_id": str(
                        request_context.get("session_id") or "",
                    ),
                    "channel": str(request_context.get("channel") or ""),
                    "source_id": str(request_context.get("source_id") or ""),
                    "user_name": request_context.get("user_name"),
                    "bbk_id": request_context.get("bbk_id"),
                },
            )()
        return get_current_trace()

    def _resolve_tool_output_and_error(
        self,
        tool_output: dict | str | None,
        error: str | None,
    ) -> tuple[str | None, str | None]:
        """解析工具输出，处理MCP isError字段.

        Args:
            tool_output: 工具返回结果
            error: 已有的错误信息

        Returns:
            (output_str, resolved_error) 元组
        """
        if error is not None:
            return None, error

        if tool_output is None:
            return None, None

        # 处理MCP CallToolResult类型
        try:
            from mcp.types import CallToolResult

            if isinstance(tool_output, CallToolResult):
                if tool_output.isError:
                    return None, self._extract_mcp_error_content(tool_output)
                return self._extract_mcp_success_content(tool_output), None
        except ImportError:
            pass

        # 处理dict形式
        if isinstance(tool_output, dict):
            if tool_output.get("isError"):
                return None, self._extract_dict_error_content(tool_output)
            return tool_output.get("content") or str(tool_output), None

        return str(tool_output), None

    def _extract_mcp_error_content(self, result) -> str:
        """从MCP CallToolResult中提取错误信息.

        Args:
            result: CallToolResult对象，isError=True

        Returns:
            错误信息字符串
        """
        content = getattr(result, "content", [])
        error_parts = []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                error_parts.append(text)
        return "\n".join(error_parts) if error_parts else "MCP tool error"

    def _extract_mcp_success_content(self, result) -> str:
        """从MCP CallToolResult中提取成功返回内容.

        Args:
            result: CallToolResult对象，isError=False

        Returns:
            内容字符串
        """
        content = getattr(result, "content", [])
        content_parts = []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                content_parts.append(text)
        return "\n".join(content_parts) if content_parts else ""

    def _extract_dict_error_content(self, result: dict) -> str:
        """从dict形式的结果中提取错误信息.

        Args:
            result: 包含isError=True的dict

        Returns:
            错误信息字符串
        """
        content = result.get("content", [])
        if isinstance(content, list):
            error_parts = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                else:
                    text = str(block)
                if text:
                    error_parts.append(text)
            return "\n".join(error_parts) if error_parts else "Tool error"
        return str(content) if content else "Tool error"

    def _load_tenant_hook_config(self) -> HookConfig:
        try:
            from swe.config.utils import get_tenant_config_path, load_config

            tenant_id = self._request_context.get("tenant_id")
            config_path = (
                get_tenant_config_path(tenant_id) if tenant_id else None
            )
            return load_config(config_path).hooks
        except Exception:
            logger.debug(
                "Tool hook: failed to load tenant config",
                exc_info=True,
            )
            return HookConfig()

    def _tool_hooks_enabled(self, tenant_hooks: HookConfig) -> bool:
        agent_hooks = getattr(self._agent_config, "hooks", None)
        session_state = self._get_hook_session_state()
        return bool(
            tenant_hooks.enabled
            or (agent_hooks is not None and agent_hooks.enabled)
            or session_state.has_loaded_skill_sources()
            or session_state.has_monitored_skill_sources(),
        )

    def _get_hook_session_state(self) -> HookSessionState:
        overlay_ref = self._request_context.get("_hook_overlay_model")
        if isinstance(overlay_ref, HookSessionState):
            return overlay_ref
        try:
            return HookSessionState.model_validate(
                self._request_context.get("hook_overlay") or {},
            )
        except Exception:
            return HookSessionState()

    def _build_tool_hook_context(
        self,
        event_name: HookEventName,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str | None = None,
        tool_response: Any = None,
        error: str | None = None,
    ) -> HookContext:
        from pathlib import Path

        from swe.config.context import get_current_effective_tenant_id
        from swe.constant import WORKING_DIR

        request_context = getattr(self, "_request_context", {}) or {}
        workspace_dir = Path(
            getattr(self, "_workspace_dir", None) or WORKING_DIR,
        )
        effective_tenant_id = (
            get_current_effective_tenant_id()
            or request_context.get("tenant_id")
            or "default"
        )
        return HookContext(
            session_id=str(request_context.get("session_id") or ""),
            transcript_path=str(request_context.get("transcript_path") or ""),
            cwd=str(workspace_dir),
            hook_event_name=event_name,
            tenant_id=str(
                request_context.get("tenant_id") or effective_tenant_id,
            ),
            effective_tenant_id=str(effective_tenant_id),
            user_id=str(request_context.get("user_id") or ""),
            agent_id=str(request_context.get("agent_id") or ""),
            channel=str(request_context.get("channel") or ""),
            source_id=request_context.get("source_id"),
            trace_id=request_context.get("trace_id"),
            workspace_dir=str(workspace_dir),
            chat_id=request_context.get("chat_id"),
            turn_id=request_context.get("turn_id"),
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            tool_response=tool_response,
            error=error,
        )

    async def _emit_tool_hook(
        self,
        event_name: HookEventName,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str | None = None,
        tool_response: Any = None,
        error: str | None = None,
    ) -> MergedHookResult:
        tenant_hooks = self._load_tenant_hook_config()
        overlay = self._get_hook_session_state()
        if not self._tool_hooks_enabled(tenant_hooks):
            return MergedHookResult()
        agent_hooks = getattr(self._agent_config, "hooks", None)
        if not isinstance(agent_hooks, HookConfig):
            agent_hooks = HookConfig()
        runtime = HookRuntime(
            tenant_config=tenant_hooks,
            agent_config=agent_hooks,
            session_overlay=overlay,
        )
        context = self._build_tool_hook_context(
            event_name,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            tool_response=tool_response,
            error=error,
        )

        async def _conversation_snapshot_provider():
            return await capture_conversation_snapshot(
                getattr(self, "memory", None),
            )

        result = await runtime.emit(
            context,
            workspace_dir=Path(getattr(self, "_workspace_dir", None) or "."),
            conversation_snapshot_provider=_conversation_snapshot_provider,
        )
        self._request_context["hook_overlay"] = overlay.model_dump(
            mode="json",
            by_alias=True,
        )
        return result

    async def _notify_skill_detector_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        mcp_server: str | None,
        tool_call_id: str | None = None,
    ) -> None:
        detector = self._request_context.get("_skill_invocation_detector")
        if detector is None or not hasattr(detector, "on_tool_call"):
            return
        try:
            validate_snapshot = getattr(
                detector,
                "validate_tool_call_snapshot",
                None,
            )
            if validate_snapshot is not None and not await validate_snapshot(
                tool_name,
                tool_input,
            ):
                return
            primary_skill, _ = await detector.on_tool_call(
                tool_name=tool_name,
                tool_input=tool_input,
                mcp_server=mcp_server,
            )
            self._store_precomputed_tool_skill_attribution(
                tool_call_id,
                {"primary_skill": primary_skill},
            )
        except Exception as exc:
            logger.debug("Skill detector tool notification failed: %s", exc)

    def _store_precomputed_tool_skill_attribution(
        self,
        tool_call_id: str | None,
        attribution: dict[str, Any],
    ) -> None:
        """缓存单次 tool call 的 skill attribution，供 tracing 复用。"""
        if not tool_call_id:
            return
        request_context = getattr(self, "_request_context", {}) or {}
        pending = request_context.get(_PENDING_TOOL_SKILL_ATTRIBUTIONS_KEY)
        if not isinstance(pending, dict):
            pending = {}
            request_context[_PENDING_TOOL_SKILL_ATTRIBUTIONS_KEY] = pending
        pending[tool_call_id] = dict(attribution)

    def _consume_precomputed_tool_skill_attribution(
        self,
        tool_call_id: str | None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """取出并消费预先计算的 tool attribution。"""
        if not tool_call_id:
            return False, None
        request_context = getattr(self, "_request_context", {}) or {}
        pending = request_context.get(_PENDING_TOOL_SKILL_ATTRIBUTIONS_KEY)
        if not isinstance(pending, dict):
            return False, None
        if tool_call_id not in pending:
            return False, None
        return True, pending.pop(tool_call_id)

    def _discard_precomputed_tool_skill_attribution(
        self,
        tool_call_id: str | None,
    ) -> None:
        """丢弃未被 tracing 消费的单次 tool attribution。"""
        self._consume_precomputed_tool_skill_attribution(tool_call_id)

    @staticmethod
    def _hook_ask_handler_ids(result: MergedHookResult) -> list[str]:
        return [
            item.handler_id
            for item in result.permission_decisions
            if item.decision == HookDecision.ASK
        ]

    def _approved_hook_ask_replay_matches(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
        result: MergedHookResult,
    ) -> bool:
        replay = getattr(self, "_tool_guard_replay_approval", None)
        if not isinstance(replay, dict):
            return False
        if replay.get("approval_kind") != _APPROVAL_KIND_HOOK_PRE_TOOL_USE:
            return False
        current_ask_ids = set(self._hook_ask_handler_ids(result))
        approved_ask_ids = set(replay.get("hook_ask_handler_ids") or [])
        if not current_ask_ids or not current_ask_ids.issubset(
            approved_ask_ids,
        ):
            return False
        if replay.get("tool_call_id") != tool_call.get("id"):
            return False
        if replay.get("tool_name") != tool_name:
            return False
        if replay.get("tool_input") != tool_input:
            return False
        self._tool_guard_replay_approval = None
        logger.info(
            "Tool hook approval: replaying approved ask for tool %s "
            "(request %s)",
            tool_name,
            str(replay.get("request_id") or "")[:8],
        )
        return True

    async def _acting_hook_denied(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        reason: str,
    ) -> dict | None:
        from agentscope.message import ToolResultBlock

        denied_text = (
            f"Tool `{tool_name}` blocked by hook runtime.\n"
            f"{reason or 'Hook denied tool execution.'}"
        )
        tool_res_msg = Msg(
            "system",
            [
                ToolResultBlock(
                    **build_failed_tool_result_block(
                        tool_call_id=tool_call["id"],
                        tool_name=tool_name,
                        error_type="hook_denied",
                        detail=denied_text,
                    ),
                ),
            ],
            "system",
        )
        await self.print(tool_res_msg, True)
        await self.memory.add(tool_res_msg)
        return None

    async def _acting_hook_stopped(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        reason: str,
    ) -> None:
        stopped_text = (
            f"Tool `{tool_name}` stopped by hook runtime.\n" f"{reason}"
        )
        tool_res_msg = Msg(
            "system",
            [
                ToolResultBlock(
                    **build_failed_tool_result_block(
                        tool_call_id=tool_call["id"],
                        tool_name=tool_name,
                        error_type="hook_stopped",
                        detail=stopped_text,
                    ),
                ),
            ],
            "system",
        )
        await self.print(tool_res_msg, True)
        await self.memory.add(tool_res_msg)

    async def _stop_pre_tool_hook(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        reason: str,
    ) -> NoReturn:
        """Record a terminal pre-tool stop and cancel peer tool calls."""
        await self._acting_hook_stopped(tool_call, tool_name, reason)
        self._request_pre_tool_terminal_stop(reason)
        raise PreToolUseTerminalStop(reason)

    async def _stop_post_tool_hook(self, reason: str) -> NoReturn:
        """End the turn after a post-tool hook without altering its result."""
        reason = reason or "Hook requested stop"
        self._discard_forced_tool_replay()
        self._request_pre_tool_terminal_stop(reason)
        raise PreToolUseTerminalStop(reason)

    async def _handle_post_tool_use(
        self,
        *,
        span_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str,
        tool_response: dict | str | None,
        trace_tool_output: dict | str | None,
    ) -> None:
        """Record the post-tool hook, trace its outcome, then stop if asked."""
        post_hook_result = await self._emit_tool_hook(
            HookEventName.POST_TOOL_USE,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            tool_response=tool_response,
        )
        await self._record_tool_hook_result(
            post_hook_result,
            event_name=HookEventName.POST_TOOL_USE,
        )
        await self._emit_tool_trace_end(span_id, trace_tool_output)
        if post_hook_result.decision == HookDecision.STOP:
            await self._stop_post_tool_hook(post_hook_result.reason)

    async def _record_tool_hook_result(
        self,
        result: MergedHookResult,
        *,
        event_name: HookEventName,
    ) -> None:
        lines = [
            f"[{item.handler_id}] {item.context}"
            for item in result.additional_context
        ]
        if result.blocked and result.reason:
            lines.append(f"[{event_name.value}] {result.reason}")
        if not lines:
            return
        msg = Msg(
            "system",
            "[Hook additional context]\n" + "\n".join(lines),
            "system",
        )
        await self.memory.add(msg)

    @staticmethod
    def _hook_guard_result(
        tool_name: str,
        tool_input: dict[str, Any],
        reason: str,
    ):
        from swe.security.tool_guard.models import (
            GuardFinding,
            GuardSeverity,
            GuardThreatCategory,
            ToolGuardResult,
        )

        finding = GuardFinding(
            id=f"hook-{_uuid.uuid4().hex[:12]}",
            rule_id="unified_hook_runtime",
            category=GuardThreatCategory.CODE_EXECUTION,
            severity=GuardSeverity.HIGH,
            title="Hook approval requested",
            description=reason or "Hook requested approval before tool use.",
            tool_name=tool_name,
            guardian="unified_hook_runtime",
        )
        return ToolGuardResult(
            tool_name=tool_name,
            params=tool_input,
            findings=[finding],
            guardians_used=["unified_hook_runtime"],
        )

    async def _acting(self, tool_call) -> dict | None:
        """Track an active tool call and stop peers after a terminal hook."""
        self._ensure_pre_tool_terminal_stop_tracking()
        self._raise_if_pre_tool_terminal_stop_requested()
        task = asyncio.current_task()
        if task is None:
            return await self._acting_impl(tool_call)
        self._active_tool_guard_acting_tasks.add(task)
        try:
            return await self._acting_impl(tool_call)
        finally:
            self._active_tool_guard_acting_tasks.discard(task)

    async def _acting_policy_denial(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        include_budget: bool,
    ) -> bool:
        """Enforce Plan Mode and SubAgent policy before executing a tool."""
        request_context = getattr(self, "_request_context", {}) or {}
        agent_role = request_context.get("agent_role", "main")
        reason: str | None = None
        if agent_role != "subagent":
            reason = self._plan_mode_policy_denial(tool_name, tool_input)
        elif agent_role == "subagent":
            policy_payload = request_context.get("subagent_policy")
            if not isinstance(policy_payload, dict):
                reason = "missing SubAgent effective policy"
            else:
                try:
                    from swe.app.subagents import (
                        PermissionPolicy,
                        validate_tool_call,
                    )

                    decision = validate_tool_call(
                        PermissionPolicy.model_validate(policy_payload),
                        tool_name,
                        tool_input,
                        mcp_server=_subagent_mcp_server_key(
                            self._resolve_mcp_server(tool_name),
                            request_context,
                        ),
                        allowed_mcp_servers=set(
                            request_context.get(
                                "subagent_allowed_mcp_servers",
                            )
                            or [],
                        ),
                    )
                    if not decision.allowed:
                        reason = decision.reason
                except Exception as exc:
                    reason = f"invalid SubAgent effective policy: {exc}"
            if reason is None and include_budget:
                budget = request_context.get("subagent_budget")
                if isinstance(budget, dict):
                    try:
                        maximum = int(budget.get("max_tool_calls") or 0)
                        used = int(
                            request_context.get("_subagent_tool_calls_used")
                            or 0,
                        )
                    except (TypeError, ValueError):
                        reason = "invalid SubAgent tool-call budget"
                    else:
                        if used >= maximum > 0:
                            reason = (
                                "SubAgent tool-call budget exceeded "
                                f"({used}/{maximum})."
                            )
                        elif maximum > 0:
                            request_context["_subagent_tool_calls_used"] = (
                                used + 1
                            )
        if reason is None:
            return False
        denied_text = (
            f"Tool `{tool_name}` blocked by "
            f"{'SubAgent' if agent_role == 'subagent' else 'Plan Mode'} policy.\n"
            f"{reason}"
        )
        tool_res_msg = Msg(
            "system",
            [
                ToolResultBlock(
                    type="tool_result",
                    id=tool_call["id"],
                    name=tool_name,
                    output=[{"type": "text", "text": denied_text}],
                ),
            ],
            "system",
        )
        await self.print(tool_res_msg, True)
        await self.memory.add(tool_res_msg)
        return True

    def _plan_mode_policy_denial(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> str | None:
        request_context = getattr(self, "_request_context", {}) or {}
        if not request_context.get("plan_mode_enabled"):
            return None
        if tool_name not in _PLAN_MODE_ALLOWED_TOOLS:
            return "tool is unavailable while Plan Mode is active"
        if tool_name != "execute_shell_command":
            return None
        return _validate_plan_mode_readonly_shell(
            str(tool_input.get("command") or "").strip(),
        )

    async def _apply_pre_tool_hook(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool, dict | None]:
        """Run the pre-tool hook and return an early response if it decides."""
        pre_hook_result = await self._emit_tool_hook(
            HookEventName.PRE_TOOL_USE,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=str(tool_call.get("id") or ""),
        )
        if pre_hook_result.updated_input is not None:
            tool_call = dict(tool_call)
            tool_call["input"] = pre_hook_result.updated_input
            tool_input = pre_hook_result.updated_input
        if pre_hook_result.decision == HookDecision.STOP:
            await self._stop_pre_tool_hook(
                tool_call,
                tool_name,
                pre_hook_result.reason or "Hook requested stop",
            )
        if pre_hook_result.decision in {
            HookDecision.BLOCK,
            HookDecision.DENY,
        }:
            result = await self._acting_hook_denied(
                tool_call,
                tool_name,
                pre_hook_result.reason,
            )
            return tool_call, tool_input, True, result
        if (
            pre_hook_result.decision == HookDecision.ASK
            and not self._approved_hook_ask_replay_matches(
                tool_call,
                tool_name,
                tool_input,
                pre_hook_result,
            )
        ):
            if self._request_context.get("agent_role") == "subagent":
                result = await self._acting_hook_denied(
                    tool_call,
                    tool_name,
                    "Background SubAgent calls cannot await interactive approval.",
                )
                return tool_call, tool_input, True, result
            result = await self._acting_with_approval(
                tool_call,
                tool_name,
                self._hook_guard_result(
                    tool_name,
                    tool_input,
                    pre_hook_result.reason,
                ),
                approval_kind=_APPROVAL_KIND_HOOK_PRE_TOOL_USE,
                hook_ask_handler_ids=self._hook_ask_handler_ids(
                    pre_hook_result,
                ),
            )
            return tool_call, tool_input, True, result
        if pre_hook_result.decision == HookDecision.ASK:
            await self._record_tool_hook_result(
                pre_hook_result,
                event_name=HookEventName.PRE_TOOL_USE,
            )
        return tool_call, tool_input, False, None

    @staticmethod
    def _strip_operation_group_arguments(
        tool_call: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove the display-only operation_group key before execution."""
        from ..app.runner.operation_group import (
            clean_tool_call_operation_group,
        )

        return clean_tool_call_operation_group(tool_call)

    async def _acting_impl(self, tool_call) -> dict | None:
        """Intercept sensitive tool calls before execution.

        1. If tool is in *denied_tools*, auto-deny unconditionally.
        2. If tool is in the guarded scope, check for a one-shot
           pre-approval, then run all guardians.
        3. For non-guarded tools, run only ``always_run`` guardians
           (e.g. sensitive file path checks).
        4. If findings exist, enter the approval flow.
        5. Otherwise, delegate to ``super()._acting``.

        The guard *decision* block is serialised via ``_tool_guard_lock``
        so that ``parallel_tool_calls=True`` does not cause state races
        on shared mixin attributes.  Actual tool execution (both
        pre-approved and non-guarded) runs **outside** the lock for
        true parallelism.
        """
        self._ensure_tool_guard()

        # The operation_group argument is display-only metadata; it must
        # never reach a tool function, the guard engine or the hooks.
        tool_call = self._strip_operation_group_arguments(tool_call)

        tool_name = str(tool_call.get("name", ""))
        tool_input = tool_call.get("input", {})

        # Resolve mcp_server from toolkit registration since tool_call dict
        # (agentscope ToolUseBlock) does not carry mcp_server.
        mcp_server = self._resolve_mcp_server(tool_name)

        if await self._acting_policy_denial(
            tool_call,
            tool_name,
            tool_input,
            include_budget=True,
        ):
            return None

        (
            tool_call,
            tool_input,
            hook_handled,
            hook_result,
        ) = await self._apply_pre_tool_hook(tool_call, tool_name, tool_input)
        if hook_handled:
            return hook_result
        if await self._acting_policy_denial(
            tool_call,
            tool_name,
            tool_input,
            include_budget=False,
        ):
            return None

        await self._notify_skill_detector_tool_call(
            tool_name,
            tool_input,
            mcp_server,
            str(tool_call.get("id") or ""),
        )

        span_id = await self._emit_tool_trace_start(
            tool_name,
            tool_input,
            mcp_server,
            str(tool_call.get("id") or ""),
        )

        action: _GuardAction | None = None
        guard_check_failed = False
        with self._agent_phase_context(
            "tool_guard",
            tool_name=tool_name,
            tool_call_id=str(tool_call.get("id") or ""),
            reason="guard_decision",
        ):
            async with self._tool_guard_lock:
                try:
                    action = await self._decide_guard_action(tool_call)
                except Exception as exc:
                    logger.warning(
                        "Tool guard check error; denying tool execution: %s",
                        exc,
                        exc_info=True,
                    )
                    guard_check_failed = True

        if guard_check_failed:
            result = await self._acting_hook_denied(
                tool_call,
                tool_name,
                "Tool guard check failed; execution was denied.",
            )
            await self._emit_tool_trace_end(span_id, result)
            return result

        if action is not None and action.kind != "preapproved":
            result = await self._execute_guard_action(action, tool_call)
            await self._emit_tool_trace_end(span_id, result)
            return result

        return await self._run_guarded_tool_call(
            action,
            tool_call,
            tool_name,
            tool_input,
            span_id,
        )

    async def _run_guarded_tool_call(
        self,
        action: "_GuardAction | None",
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
        span_id: str,
    ) -> dict | None:
        """Execute a permitted tool call and emit its terminal hook events."""
        try:
            self._raise_if_pre_tool_terminal_stop_requested()
            result = await self._execute_guard_action_or_tool_call(
                action,
                tool_call,
                tool_name,
                tool_input,
            )
            tool_use_id = str(tool_call.get("id") or "")
            tool_response = self._extract_current_tool_response(tool_use_id)
            # post hook 不应把结构化失败当作正常结果继续消费，
            # 但 tracing 仍需要读取原始失败 payload 来提取 error。
            trace_tool_output = (
                result
                if result is not None
                else self._extract_current_tool_response(
                    tool_use_id,
                    include_structured_failure=True,
                )
            )
            await self._handle_post_tool_use(
                span_id=span_id,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                tool_response=tool_response,
                trace_tool_output=trace_tool_output,
            )

            self._complete_forced_tool_replay(
                tool_call,
                tool_name,
                tool_input,
            )
            request_context = getattr(self, "_request_context", {}) or {}
            if request_context.get(
                "goal_id",
            ) and _goal_tool_may_write_environment(tool_name, tool_input):
                request_context["_goal_turn_environment_changed"] = True
            return result

        except PreToolUseTerminalStop:
            raise
        except Exception as e:
            failure_hook_result = await self._emit_tool_hook(
                HookEventName.POST_TOOL_USE_FAILURE,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=str(tool_call.get("id") or ""),
                error=str(e),
            )
            await self._record_tool_hook_result(
                failure_hook_result,
                event_name=HookEventName.POST_TOOL_USE_FAILURE,
            )
            await self._emit_tool_trace_end(span_id, None, error=str(e))
            if failure_hook_result.decision == HookDecision.STOP:
                await self._stop_post_tool_hook(failure_hook_result.reason)
            raise

    async def _decide_guard_action(
        self,
        tool_call: dict[str, Any],
    ) -> "_GuardAction | None":
        """Decide what guard action to take (runs under lock).

        Returns a ``_GuardAction`` describing what to do, or ``None``
        to fall through to the default ``super()._acting`` path.
        No actual tool execution happens here.
        """
        engine = self._tool_guard_engine
        tool_name = str(tool_call.get("name", ""))
        tool_input = tool_call.get("input", {})
        if not tool_name or not engine.enabled:
            return None

        if engine.is_denied(tool_name):
            logger.warning(
                "Tool guard: tool '%s' is in the denied set, auto-denying",
                tool_name,
            )
            denied_result = engine.guard(tool_name, tool_input)
            return _GuardAction(
                "auto_denied",
                tool_name,
                tool_input,
                guard_result=denied_result,
            )

        guarded = engine.is_guarded(tool_name)

        if guarded and await self._consume_preapproval(tool_name, tool_input):
            self._tool_guard_pending_info = None
            await self._cleanup_tool_guard_denied_messages(
                include_denial_response=False,
            )
            return _GuardAction("preapproved", tool_name, tool_input)

        guard_result = engine.guard(
            tool_name,
            tool_input,
            only_always_run=not guarded,
        )
        if guard_result is not None and guard_result.findings:
            from swe.security.tool_guard.utils import log_findings

            log_findings(tool_name, guard_result)
            if self._should_require_approval():
                return _GuardAction(
                    "needs_approval",
                    tool_name,
                    tool_input,
                    guard_result=guard_result,
                )
            if not getattr(guard_result, "is_safe", True):
                return _GuardAction(
                    "auto_denied",
                    tool_name,
                    tool_input,
                    guard_result=guard_result,
                )
        return None

    async def _execute_guard_action(
        self,
        action: "_GuardAction",
        tool_call: dict[str, Any],
    ) -> dict | None:
        """Execute the guard action decided under lock (runs outside lock)."""
        if action.kind == "auto_denied":
            return await self._acting_auto_denied(
                tool_call,
                action.tool_name,
                action.guard_result,
            )
        if action.kind == "preapproved":
            return await self._run_approved_tool_call(
                tool_call,
                action.tool_name,
                action.tool_input,
            )
        if action.kind == "needs_approval":
            if self._request_context.get("agent_role") == "subagent":
                return await self._acting_auto_denied(
                    tool_call,
                    action.tool_name,
                    action.guard_result,
                )
            return await self._acting_with_approval(
                tool_call,
                action.tool_name,
                action.guard_result,
            )
        return None

    async def _execute_guard_action_or_tool_call(
        self,
        action: "_GuardAction | None",
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict | None:
        """Execute a preapproved action or the ordinary tool-call path."""
        from ..app.runner.operation_group import OPERATION_GROUP_INTERNAL_FIELD

        executable_tool_call = dict(tool_call)
        executable_tool_call.pop(OPERATION_GROUP_INTERNAL_FIELD, None)
        if action is not None:
            return await self._execute_guard_action(
                action, executable_tool_call
            )
        return await self._run_tool_call_with_hard_timeout(
            executable_tool_call,
            tool_name,
            tool_input,
        )

    async def _consume_preapproval(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """Consume one matching approval token if present."""
        session_id = str(self._request_context.get("session_id") or "")
        if not session_id:
            return False

        svc = self._tool_guard_approval_service
        consumed = await svc.consume_approval(
            session_id,
            tool_name,
            tool_params=tool_input,
            approval_kind=_APPROVAL_KIND_TOOL_GUARD,
        )
        if consumed:
            logger.info(
                "Tool guard: pre-approved '%s' (session %s), skipping",
                tool_name,
                session_id[:8],
            )
        return bool(consumed)

    async def _run_approved_tool_call(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict | None:
        """Execute an approved call without advancing a replay queue."""
        return await self._run_tool_call_with_hard_timeout(
            tool_call,
            tool_name,
            tool_input,
        )

    def _complete_forced_tool_replay(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        """Record a replay result only after its post-tool hooks finish."""
        if not getattr(self, "_tool_guard_forced_replay_active", False):
            return
        self._tool_guard_forced_replay_active = False
        self._tool_guard_replay_done = {
            "tool_name": tool_name,
            "tool_call_id": str(tool_call.get("id") or ""),
            "tool_input": tool_input,
            "remaining_queue": getattr(self, "_tool_guard_replay_queue", []),
        }

    def _discard_forced_tool_replay(self) -> None:
        """Prevent a terminal hook decision from resuming queued replays."""
        self._tool_guard_forced_replay_active = False
        self._tool_guard_replay_done = None
        self._tool_guard_replay_queue = []

    # ------------------------------------------------------------------
    # Denied / Approval responses
    # ------------------------------------------------------------------

    async def _acting_auto_denied(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        guard_result=None,
    ) -> dict | None:
        """Auto-deny a tool call without offering approval."""
        from agentscope.message import ToolResultBlock
        from swe.security.tool_guard.approval import (
            format_findings_summary,
        )

        if guard_result is not None and guard_result.findings:
            findings_text = format_findings_summary(guard_result)
            severity = guard_result.max_severity.value
            count = str(guard_result.findings_count)
        else:
            findings_text = "- Tool is in the denied list / 工具在禁止列表中"
            severity = "DENIED"
            count = "N/A"

        denied_text = (
            f"⛔ **Tool Blocked / 工具已拦截**\n\n"
            f"- Tool / 工具: `{tool_name}`\n"
            f"- Severity / 严重性: `{severity}`\n"
            f"- Findings / 发现: `{count}`\n\n"
            f"{findings_text}\n\n"
            f"This tool is blocked and cannot be approved.\n"
            f"该工具已被禁止，无法批准执行。"
        )

        tool_res_msg = Msg(
            "system",
            [
                ToolResultBlock(
                    **build_failed_tool_result_block(
                        tool_call_id=tool_call["id"],
                        tool_name=tool_name,
                        error_type="tool_guard_denied",
                        detail=denied_text,
                        governance_status="blocked",
                    ),
                ),
            ],
            "system",
        )
        attach_tool_governance_message_metadata(
            tool_res_msg,
            tool_call_id=tool_call["id"],
            governance_status="blocked",
        )

        await self.print(tool_res_msg, True)
        await self.memory.add(tool_res_msg)
        return None

    def _mark_approval_source_message(self) -> Any:
        original_msg = None
        for msg, marks in reversed(self.memory.content):
            if msg.role == "assistant":
                if TOOL_GUARD_DENIED_MARK not in marks:
                    marks.append(TOOL_GUARD_DENIED_MARK)
                original_msg = msg
                break
        return original_msg

    def _build_approval_extra(
        self,
        tool_call: dict[str, Any],
        approval_kind: str,
        hook_ask_handler_ids: list[str] | None,
        original_msg: Any,
    ) -> dict[str, Any]:
        from ..app.runner.operation_group import OPERATION_GROUP_INTERNAL_FIELD

        stored_tool_call = dict(tool_call)
        operation_group = stored_tool_call.pop(
            OPERATION_GROUP_INTERNAL_FIELD,
            None,
        )
        extra: dict[str, Any] = {
            "approval_kind": approval_kind,
            "tool_call": stored_tool_call,
        }
        if operation_group is not None:
            extra["operation_group"] = operation_group
        extra["agent_id"] = self._request_context.get("agent_id")
        extra["tenant_id"] = self._request_context.get("tenant_id")
        extra["source_id"] = self._request_context.get("source_id")
        goal_id = str(self._request_context.get("goal_id") or "").strip()
        if goal_id:
            extra["goal_id"] = goal_id
        chat_id = str(self._request_context.get("chat_id") or "").strip()
        msgid = str(self._request_context.get("msgid") or "").strip()
        if chat_id and msgid:
            extra["chat_id"] = chat_id
            extra["msgid"] = msgid
        if hook_ask_handler_ids:
            extra["hook_ask_handler_ids"] = list(hook_ask_handler_ids)
        if original_msg is not None:
            thinking_blocks = [
                block
                for block in original_msg.get_content_blocks()
                if isinstance(block, dict) and block.get("type") == "thinking"
            ]
            if thinking_blocks:
                extra["thinking_blocks"] = thinking_blocks
        replay_queue = getattr(self, "_tool_guard_replay_queue", None)
        if replay_queue is not None:
            extra["remaining_queue"] = list(replay_queue)
            self._tool_guard_replay_queue = None
        else:
            siblings = self._extract_sibling_tool_calls()
            if siblings:
                extra["sibling_tool_calls"] = siblings
        return extra

    async def _cancel_stale_approval_requests(
        self,
        session_id: str,
        tool_call: dict[str, Any],
        extra: dict[str, Any],
    ) -> None:
        if not session_id:
            return
        svc = self._tool_guard_approval_service
        tool_call_id = tool_call.get("id", "")
        ids = [tool_call_id] + [
            queued.get("id", "") for queued in extra.get("remaining_queue", [])
        ]
        for call_id in (value for value in ids if value):
            await svc.cancel_stale_pending_for_tool_call(session_id, call_id)

    async def _notify_approval_pending(self, pending_request: Any) -> None:
        try:
            from swe.app.approvals import notify_cron_approval_pending

            await notify_cron_approval_pending(
                pending_request,
                channel_manager=self._request_context.get("channel_manager"),
            )
        except Exception:
            logger.exception(
                "Tool guard: failed to notify cron approval request",
            )

    async def _emit_approval_required(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        guard_result: Any,
    ) -> None:
        from agentscope.message import ToolResultBlock
        from swe.security.tool_guard.approval import format_findings_summary

        denied_text = (
            f"⚠️ **Risk Detected / 检测到风险**\n\n"
            f"- Tool / 工具: `{tool_name}`\n"
            f"- Severity / 严重性: "
            f"`{guard_result.max_severity.value}`\n"
            f"- Findings / 发现: "
            f"`{guard_result.findings_count}`\n\n"
            f"{format_findings_summary(guard_result)}\n\n"
            f"Type `/approve` to approve, "
            f"`/deny` to deny, or send any message to deny.\n"
            f"输入 `/approve` 批准执行，或发送任意消息拒绝。"
        )
        tool_res_msg = Msg(
            "system",
            [
                ToolResultBlock(
                    **build_failed_tool_result_block(
                        tool_call_id=tool_call["id"],
                        tool_name=tool_name,
                        error_type="approval_required",
                        detail=denied_text,
                        governance_status="pending",
                    ),
                ),
            ],
            "system",
        )
        attach_tool_governance_message_metadata(
            tool_res_msg,
            tool_call_id=tool_call["id"],
            governance_status="pending",
        )
        await self.print(tool_res_msg, True)
        await self.memory.add(tool_res_msg, marks=TOOL_GUARD_DENIED_MARK)

    async def _acting_with_approval(
        self,
        tool_call: dict[str, Any],
        tool_name: str,
        guard_result,
        *,
        approval_kind: str = _APPROVAL_KIND_TOOL_GUARD,
        hook_ask_handler_ids: list[str] | None = None,
    ) -> dict | None:
        """Deny the tool call and record a pending approval."""
        channel = str(self._request_context.get("channel") or "")
        original_msg = self._mark_approval_source_message()
        extra = self._build_approval_extra(
            tool_call,
            approval_kind,
            hook_ask_handler_ids,
            original_msg,
        )
        session_id = str(
            self._request_context.get("session_id") or "",
        )
        svc = self._tool_guard_approval_service
        await self._cancel_stale_approval_requests(
            session_id,
            tool_call,
            extra,
        )
        pending_request = await svc.create_pending(
            session_id=session_id,
            user_id=str(
                self._request_context.get("user_id") or "",
            ),
            channel=channel,
            tool_name=tool_name,
            result=guard_result,
            extra=extra,
        )
        await self._notify_approval_pending(pending_request)

        guardians = list(
            {f.guardian for f in guard_result.findings if f.guardian},
        )
        self._tool_guard_pending_info = {
            "request_id": pending_request.request_id,
            "tool_name": tool_name,
            "tool_input": tool_call.get("input", {}),
            "guardians": guardians,
        }

        await self._emit_approval_required(tool_call, tool_name, guard_result)
        return None

    # ------------------------------------------------------------------
    # _reasoning override (guard-aware)
    # ------------------------------------------------------------------

    async def _reasoning(
        self,
        tool_choice: Literal["auto", "none", "required"] | None = None,
    ) -> Msg:
        """Short-circuit reasoning when awaiting guard approval.

        After a forced approved replay completes its ``_acting`` cycle,
        this method either continues with the next queued sibling tool
        call (returning a ``tool_use`` message) or returns a text-only
        completion message so the ``ReActAgent.reply`` loop exits
        naturally.
        """
        if self._consume_plan_interaction_turn_boundary():
            return Msg(self.name, [], "assistant")

        selected_expert_error = str(
            self._request_context.pop(
                "selected_expert_execution_error",
                "",
            )
            or "",
        ).strip()
        if selected_expert_error:
            return await self._emit_assistant_msg(selected_expert_error)

        with self._agent_phase_context(
            "approval_replay",
            reason="approval_replay_done",
        ):
            replay_msg = await self._reason_about_replay_done()
        if replay_msg is not None:
            return replay_msg

        forced_tool_call = self._pop_forced_tool_call()
        if forced_tool_call is not None:
            with self._agent_phase_context(
                "approval_replay",
                tool_name=str(forced_tool_call.get("name") or ""),
                tool_call_id=str(forced_tool_call.get("id") or ""),
                reason="forced_tool_replay",
            ):
                replay_msg = await self._emit_forced_tool_use(
                    forced_tool_call,
                )
            if replay_msg is not None:
                return replay_msg

        if self._last_tool_response_is_denied():
            with self._agent_phase_context(
                "approval_replay",
                reason="waiting_for_approval",
            ):
                return await self._emit_waiting_for_approval()

        return await super()._reasoning(  # type: ignore[misc]
            tool_choice=tool_choice,
        )

    def _consume_plan_interaction_turn_boundary(self) -> bool:
        """Consume the plan-card turn boundary marker once."""
        if not getattr(self, _PLAN_INTERACTION_TURN_BOUNDARY_ATTR, False):
            return False
        setattr(self, _PLAN_INTERACTION_TURN_BOUNDARY_ATTR, False)
        return True

    async def _summarizing(self) -> Msg:
        """Avoid a synthetic summary turn immediately after a plan card."""
        if self._consume_plan_interaction_turn_boundary():
            return Msg(
                self.name,
                [],
                "assistant",
                metadata={
                    PLAN_INTERACTION_SUMMARIZING_SHORT_CIRCUIT_METADATA_KEY: True,
                },
            )
        return await super()._summarizing()  # type: ignore[misc]

    async def _reason_about_replay_done(self) -> Msg | None:
        """Emit replay continuation or completion message.

        When the replay queue is exhausted, all synthetic replay
        messages are cleaned from memory and ``None`` is returned so
        that ``_reasoning`` falls through to ``super()._reasoning()``.
        This lets the LLM respond naturally based on the actual tool
        results without leaving any approval-process artifacts in the
        conversation.
        """
        replay_info = getattr(self, "_tool_guard_replay_done", None)
        if not replay_info:
            return None

        self._tool_guard_replay_done = None
        selected_expert_follow_up = await self._selected_expert_follow_up(
            replay_info,
        )
        if selected_expert_follow_up is not None:
            return await self._emit_forced_tool_use(selected_expert_follow_up)
        selected_expert_error = str(
            self._request_context.pop(
                "selected_expert_execution_error",
                "",
            )
            or "",
        ).strip()
        if selected_expert_error:
            return await self._emit_assistant_msg(selected_expert_error)
        remaining_queue = self._filter_pending_replay_queue(
            replay_info.get("remaining_queue") or [],
        )
        if not remaining_queue:
            return None
        return await self._emit_next_replay_tool_call(remaining_queue)

    def _selected_expert_start_follow_up(
        self,
        context: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Process the forced ``start_subagent`` replay result."""
        run_id = str(response.get("run_id") or "").strip()
        if not response.get("accepted") or not run_id:
            context[_SELECTED_EXPERT_EXECUTION_KEY] = False
            context["selected_expert_execution_error"] = (
                "The selected expert could not be started. "
                "It was not replaced by the Main Agent."
            )
            return None
        context[_SELECTED_EXPERT_RUN_ID_KEY] = run_id
        return self._selected_expert_wait_tool_call()

    def _selected_expert_get_follow_up(
        self,
        context: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Process a forced ``get_subagent`` replay result."""
        if self._selected_expert_record_is_terminal(response):
            context[_SELECTED_EXPERT_EXECUTION_KEY] = False
            return None
        return self._selected_expert_wait_tool_call()

    def _selected_expert_wait_follow_up(
        self,
        context: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Process a forced ``wait_subagent`` replay result."""
        run_id = str(context.get(_SELECTED_EXPERT_RUN_ID_KEY) or "").strip()
        if not run_id or self._selected_expert_is_terminal(response, run_id):
            context[_SELECTED_EXPERT_EXECUTION_KEY] = False
            return None
        if not self._selected_expert_is_active(response, run_id):
            return self._selected_expert_get_tool_call(run_id)
        return self._selected_expert_wait_tool_call()

    async def _selected_expert_follow_up(
        self,
        replay_info: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Keep a selected-expert turn on its one worker until terminal.

        The forced initial ``start_subagent`` call still crosses the normal
        Tool Guard.  Once accepted, this helper emits ordinary guarded
        ``wait_subagent`` calls until that exact run reports a terminal
        result, then lets the Main Agent summarize the recorded result.
        """
        context = self._request_context
        if not context.get(_SELECTED_EXPERT_EXECUTION_KEY):
            return None
        if await self._selected_expert_turn_is_stopping(context):
            context[_SELECTED_EXPERT_EXECUTION_KEY] = False
            return None
        tool_name = str(replay_info.get("tool_name") or "")
        response = self._selected_expert_tool_response(replay_info)
        if tool_name == "start_subagent":
            return self._selected_expert_start_follow_up(context, response)
        if tool_name == "get_subagent":
            return self._selected_expert_get_follow_up(context, response)
        if tool_name != "wait_subagent":
            return None
        return self._selected_expert_wait_follow_up(context, response)

    @staticmethod
    async def _selected_expert_turn_is_stopping(
        context: dict[str, Any],
    ) -> bool:
        tracker = context.get("_task_tracker")
        is_turn_stopping = getattr(tracker, "is_turn_stopping", None)
        if not callable(is_turn_stopping):
            return False
        return await is_turn_stopping(
            str(context.get("chat_id") or ""),
            str(context.get("msgid") or ""),
        )

    def _selected_expert_tool_response(
        self,
        replay_info: dict[str, Any],
    ) -> dict[str, Any]:
        """Read the current forced-call result without trusting model text."""
        response = self._extract_current_tool_response(
            str(replay_info.get("tool_call_id") or ""),
            include_structured_failure=True,
        )
        if isinstance(response, str):
            try:
                response = _json.loads(response)
            except (TypeError, ValueError):
                return {}
        return response if isinstance(response, dict) else {}

    @staticmethod
    def _selected_expert_is_terminal(
        response: dict[str, Any],
        run_id: str,
    ) -> bool:
        """Return whether the selected run is present in a terminal wait."""
        for item in response.get("terminal_runs", []):
            if isinstance(item, dict) and item.get("run_id") == run_id:
                return True
        return False

    @staticmethod
    def _selected_expert_is_active(
        response: dict[str, Any],
        run_id: str,
    ) -> bool:
        return any(
            isinstance(item, dict) and item.get("run_id") == run_id
            for item in response.get("active_runs", [])
        )

    @staticmethod
    def _selected_expert_record_is_terminal(
        response: dict[str, Any],
    ) -> bool:
        return str(response.get("status") or "") in {
            "completed",
            "partial",
            "failed",
            "cancelled",
            "expired",
        }

    @staticmethod
    def _selected_expert_wait_tool_call() -> dict[str, Any]:
        return {
            "id": f"selected-expert-wait-{_uuid.uuid4().hex[:12]}",
            "name": "wait_subagent",
            "input": {"timeout_ms": _SELECTED_EXPERT_WAIT_TIMEOUT_MS},
        }

    @staticmethod
    def _selected_expert_get_tool_call(run_id: str) -> dict[str, Any]:
        return {
            "id": f"selected-expert-get-{_uuid.uuid4().hex[:12]}",
            "name": "get_subagent",
            "input": {"run_id": run_id},
        }

    def _filter_pending_replay_queue(
        self,
        queue: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop replayed tool calls that already have tool results."""
        filtered: list[dict[str, Any]] = []
        for tool_call in list(queue):
            tc_id = tool_call.get("id", "")
            if self._tool_result_exists_in_memory(tc_id):
                continue
            filtered.append(tool_call)
        return filtered

    async def _emit_next_replay_tool_call(
        self,
        remaining_queue: list[dict[str, Any]],
    ) -> Msg:
        """Emit assistant message that chains to the next replayed call.

        Only the ``ToolUseBlock`` is included — no approval-process
        text is added so that the conversation history stays clean
        after the full replay sequence completes.
        """
        from agentscope.message import ToolUseBlock

        next_tc = remaining_queue[0]
        self._tool_guard_replay_queue = remaining_queue[1:]
        next_id = next_tc.get("id") or f"queued-{_uuid.uuid4().hex[:12]}"
        self._tool_guard_forced_replay_active = True
        msg = Msg(
            self.name,
            [
                ToolUseBlock(
                    type="tool_use",
                    id=next_id,
                    name=next_tc.get("name", "unknown"),
                    input=next_tc.get("input", {}),
                ),
            ],
            "assistant",
        )
        await self.print(msg, True)
        await self.memory.add(msg)
        return msg

    async def _emit_assistant_msg(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Msg:
        """Print and persist a plain assistant text message."""
        effective_metadata = metadata
        if effective_metadata is None:
            effective_metadata = getattr(
                self,
                "_tool_guard_pending_message_metadata",
                None,
            )
            if hasattr(self, "_tool_guard_pending_message_metadata"):
                self._tool_guard_pending_message_metadata = None
        msg = Msg(
            self.name,
            content,
            "assistant",
            metadata=effective_metadata,
        )
        await self.print(msg, True)
        await self.memory.add(msg)
        return msg

    async def _emit_forced_tool_use(
        self,
        forced_tool_call: dict[str, Any],
    ) -> Msg | None:
        """Emit a forced tool_use replay block, or ``None`` on failure."""
        try:
            from agentscope.message import ToolUseBlock

            self._tool_guard_forced_replay_active = True

            # Extract thinking blocks if present
            thinking_blocks = forced_tool_call.pop("_thinking_blocks", None)

            # Build content blocks
            content_blocks = []

            # Add thinking blocks first (if present)
            if thinking_blocks is not None and isinstance(
                thinking_blocks,
                list,
            ):
                content_blocks.extend(thinking_blocks)

            # Add tool use block
            content_blocks.append(
                ToolUseBlock(
                    type="tool_use",
                    id=forced_tool_call["id"],
                    name=forced_tool_call["name"],
                    input=forced_tool_call["input"],
                ),
            )

            msg = Msg(
                self.name,
                content_blocks,
                "assistant",
            )
            await self.print(msg, True)
            await self.memory.add(msg)
            return msg
        except Exception as exc:
            self._tool_guard_forced_replay_active = False
            logger.warning(
                "Tool guard: forced tool replay failed, "
                "falling back to normal reasoning: %s",
                exc,
                exc_info=True,
            )
            return None

    @staticmethod
    def _guardian_trigger_hint(guardians: list[str]) -> tuple[str, str]:
        """Return (trigger_label, settings_hint) for the guardian(s)."""
        has_file = "file_path_tool_guardian" in guardians
        has_tool = "rule_based_tool_guardian" in guardians
        if has_file and has_tool:
            label = "Tool Guard & File Guard / 工具护栏 & 文件护栏"
            hint_en = (
                "Triggered by tool guardrails "
                "(configurable in Security → Tool Guard / File Guard settings)"
            )
            hint_zh = "触发工具护栏 & 文件护栏（在安全-工具护栏 / 文件护栏页面可以更改设置）"
        elif has_file:
            label = "File Guard / 文件护栏"
            hint_en = (
                "Triggered by file guardrails "
                "(configurable in Security → File Guard settings)"
            )
            hint_zh = "触发文件护栏（在安全-文件护栏页面可以更改设置）"
        else:
            label = "Tool Guard / 工具护栏"
            hint_en = (
                "Triggered by tool guardrails "
                "(configurable in Security → Tool Guard settings)"
            )
            hint_zh = "触发工具护栏（在安全-工具护栏页面可以更改设置）"
        return label, f"💡 {hint_en}\n💡 {hint_zh}"

    async def _emit_waiting_for_approval(self) -> Msg:
        """Emit waiting-for-approval guidance when call is blocked."""
        pending = await self._get_pending_info_for_display()
        request_id = str(pending.get("request_id", "") or "")
        tool_name = pending.get("tool_name", "unknown")
        tool_input = pending.get("tool_input", {})
        guardians: list[str] = pending.get("guardians", [])
        params_text = _json.dumps(
            tool_input,
            ensure_ascii=False,
            indent=2,
        )
        trigger_label, _ = self._guardian_trigger_hint(guardians)
        metadata = {
            "approval_action": {
                "requestId": request_id,
                "toolName": tool_name,
                "toolInput": tool_input,
                "triggerLabel": trigger_label,
                "approveCommand": "/approve",
                "denyCommand": "/deny",
            },
        }
        if request_id:
            metadata["approval_action"][
                "approveCommand"
            ] = f"/approve {request_id}"
            metadata["approval_action"]["denyCommand"] = f"/deny {request_id}"
        self._tool_guard_pending_message_metadata = metadata
        return await self._emit_assistant_msg(
            f"⏳ `{tool_name}`调用需要审批\n" f"```json\n{params_text}\n```\n",
        )
