# -*- coding: utf-8 -*-
# pylint: disable=unused-argument too-many-branches too-many-statements
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Collection
from uuid import uuid4

import httpx
from agentscope.message import Msg, TextBlock
from agentscope.pipeline import stream_printing_messages
from agentscope_runtime.engine.runner import Runner
from agentscope_runtime.engine.schemas.agent_schemas import (
    AgentRequest,
    Event,
    Message,
    RunStatus,
)
from agentscope_runtime.engine.schemas.exception import AgentException
from dotenv import load_dotenv

from ..mcp.http_headers import (
    _filter_passthrough_headers,
    build_mcp_http_headers,
)
from ..mcp.lazy_client import LazyMCPClient, get_mcp_tool_discovery_cache
from ..mcp.stateful_client import HttpStatefulClient, StdIOStatefulClient
from ..mcp.stdio_launcher import build_tenant_aware_stdio_launch_config
from .command_dispatch import (
    _get_last_user_text,
)
from .context_usage import (
    CONTEXT_USAGE_INVALID_STATE_KEY,
    CONTEXT_USAGE_STATE_KEY,
    capture_context_usage,
)
from .assistant_response import (
    project_candidate_assistant_response,
    replace_candidate_assistant_response,
)
from .hidden_context_injection import (
    append_hidden_context_to_user_message,
)
from .model_call_error_detail import (
    MODEL_CALL_FAILED_MESSAGES_STATE_KEY,
    ModelCallFailureDetail,
    ModelCallFailedException,
    extract_model_call_failure_detail,
)
from .query_error_dump import write_query_error_dump
from .query_execution import QueryExecution, QueryInvocation
from .query_execution.admission import stream_admission
from .query_execution.retry import load_retry_settings
from .query_execution.adapters import LegacyQueryExecutionAdapter
from .query_contracts import (
    _QueryPreflight,
    _QueryRuntime,
    _QueryRuntimeInputs,
    _QueryRuntimeResources,
    _RuntimeStartResult,
)
from . import (
    query_attempt,
    query_cleanup,
    query_preflight,
    query_runtime,
    session_lifecycle,
    turn_lifecycle,
)
from .session import SafeJSONSession, SESSION_SKILL_SNAPSHOT_STATE_KEY
from .stream_boundary import normalize_reasoning_boundary_stream
from .task_progress import attach_task_progress
from .utils import build_env_context
from ..identity_resolver import resolve_user_identity
from ..channels.schema import DEFAULT_CHANNEL
from ...__version__ import __version__
from ...agents.react_agent import SWEAgent
from ...agents.skill_invocation_detector import SkillInvocationDetector
from ...agents.tool_guard_mixin import PreToolUseTerminalStop
from ...agents.tool_failure import (
    TOOL_GOVERNANCE_BLOCK_FIELD,
    attach_tool_governance_message_metadata,
)
from ...agents.skills_manager import (
    get_skill_freshness_token,
    get_workspace_skills_dir,
    resolve_effective_skill_dir,
)
from ...agents.hook_runtime import HookRuntime
from ...agents.hook_runtime.runtime import log_stop_skipped_telemetry
from ...agents.hook_runtime.conversation_snapshot import (
    capture_conversation_snapshot,
)
from ...agents.hook_runtime.models import (
    HookConfig,
    HookContext,
    HookDecision,
    HookEventName,
    HookSessionOverlay,
    HookSessionState,
    MergedHookResult,
    StopHookExecutionResult,
)
from ...agents.hook_runtime.skill_loader import (
    SkillHookLoadError,
    load_skill_hooks_for_session,
)
from ...security.tool_guard.models import TOOL_GUARD_DENIED_MARK
from ...config.config import (
    MCPClientConfig,
    MCPConfig,
    load_agent_config,
    SuggestionMode,
)
from ...constant import (
    QUERY_CLEANUP_TIMEOUT,
    QUERY_TIMEOUT_SECONDS,
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
    WORKING_DIR,
)
from ...security.tool_guard.approval import ApprovalDecision
from ...tracing import (
    has_trace_manager,
    get_trace_manager,
)
from ...tracing.agent_trace_sdk import (
    SpanKind,
    TraceFields,
    global_tracer,
    use_b3_trace_context,
)
from ...tracing.models import TraceStatus
from ...config.context import (
    get_current_passthrough_headers,
)
from ...runtime_invocation_claims import runtime_invocation_claims_context
from ..answer_turn.models import TurnIdentity, TurnOutcome, TurnStatus
from ..source_system_config import is_chat_task_progress_enabled
from ..source_system_config.runtime import get_current_source_system_config

if TYPE_CHECKING:
    from ...agents.memory import BaseMemoryManager

logger = logging.getLogger(__name__)
TASK_RUNS_STATE_KEY = "task_runs"
_INTERNAL_FOLLOW_UP_METADATA_KEY = "swe_internal_follow_up"
_PLAN_MODE_META_KEY = "plan_mode_enabled"
_PLAN_REQUEST_MODE_KEY = "mode"
_PLAN_INTERACTION_RESPONSE_KEY = "plan_interaction_response"
_ACCEPTED_PLAN_SOURCE_META_KEY = "accepted_plan_source"
_ACCEPTED_PLAN_SERVER_SOURCE = "server_plan_store"
_PLAN_INTERACTION_CARD_METADATA_KEY = "plan_interaction_card"
_SKILL_FRESHNESS_NOTICE_METADATA_KEY = "swe_skill_freshness_notice"
_EXTERNAL_APPROVAL_MESSAGE_META_KEY = "external_approval_message"
_APPROVAL_REQUEST_ID_META_KEY = "approval_request_id"
_APPROVAL_DECISION_META_KEY = "approval_decision"
_SESSION_TITLE_GENERATED_META_KEY = "session_title_generated"
_DEFER_ANSWER_TURN_SETTLEMENT_META_KEY = "defer_answer_turn_settlement"
_SCENARIO_SNAPSHOT_REQUEST_META_KEYS = frozenset(
    {
        "scenario_preset_snapshot",
        "scenario_preset_snapshot_source",
    },
)
_TASK_SESSION_KIND = "task"
_STOP_FOLLOW_UP_REASON_TEMPLATE = (
    "Stop completion gate blocked stopping: {reason}\n"
    "Continue working until the gate can allow completion."
)
_STOP_INCOMPLETE_MESSAGE_TEMPLATE = (
    "任务未完成：Stop 完成门禁未通过。最新阻断原因：{reason}"
)
_SKILL_FRESHNESS_NOTICE_HEADER = (
    "[Skill freshness notice]\n"
    "The following previously associated skills changed for this turn. "
    "Treat current skill content as superseding earlier assumptions:\n"
)

_APPROVE_EXACT = frozenset(
    {
        "approve",
        "/approve",
        "/daemon approve",
    },
)
_MCP_HTTP_TIMEOUT_SECONDS = 240.0
_MCP_CONNECT_TIMEOUT_SECONDS = _MCP_HTTP_TIMEOUT_SECONDS
_MCP_HTTP_SSE_READ_TIMEOUT_SECONDS = 60.0 * 5

_DENY_EXACT = frozenset(
    {
        "deny",
        "/deny",
        "/daemon deny",
    },
)


def _plan_decision_from_meta(channel_meta: dict[str, Any]) -> str | None:
    """从计划交互响应中提取审核动作。"""
    response = channel_meta.get(_PLAN_INTERACTION_RESPONSE_KEY)
    if not isinstance(response, dict):
        return None
    decision = response.get("decision")
    return decision if isinstance(decision, str) else None


def _requested_plan_mode_update(
    channel_meta: dict[str, Any],
) -> bool | None:
    """解析本次请求是否显式要求更新 Plan Mode 状态。"""
    decision = _plan_decision_from_meta(channel_meta)
    if decision == "revise":
        return True
    if decision in {"execute", "exit_plan"}:
        return False

    mode = channel_meta.get(_PLAN_REQUEST_MODE_KEY)
    if mode == "plan":
        return True
    if mode == "normal":
        return False
    return None


def _resolve_plan_mode_enabled(
    channel_meta: dict[str, Any],
    chat: Any,
) -> bool:
    """优先使用请求显式状态，否则沿用 ChatSpec.meta 中的持久状态。"""
    requested_update = _requested_plan_mode_update(channel_meta)
    if requested_update is not None:
        return requested_update
    if isinstance(channel_meta.get(_PLAN_MODE_META_KEY), bool):
        return channel_meta[_PLAN_MODE_META_KEY]
    chat_meta = getattr(chat, "meta", None)
    if isinstance(chat_meta, dict):
        return bool(chat_meta.get(_PLAN_MODE_META_KEY, False))
    return False


@dataclass
class _TurnPlan:
    """保存本轮 agent 调用需要的输入。"""

    original_user_message: str
    turn_msgs: list[Any]


@dataclass(frozen=True)
class _QueryHandlerContext:
    query: str | None
    session_id: str
    user_id: str
    identity: TurnIdentity | None
    turn_id: str
    trace_fields: TraceFields | None


@dataclass
class _QueryTurnOutcome:
    """记录 agent 输出与完成态。"""

    task_completed: bool = True
    assistant_response: str = ""
    stop_follow_up_turns: int = 0
    max_stop_turns: int = 0
    automatic_follow_up_turns: int = 0
    max_automatic_follow_up_turns: int = 0
    plan_interaction_turn_boundary: bool = False
    stop_hook_active: bool = False
    completion_blocked: bool = False
    completion_block_reason: str = ""
    completion_marked_incomplete: bool = False
    pre_tool_terminal_stop: bool = False
    stop_output_buffer_required: bool = False
    buffered_assistant_messages: list[Msg] = field(default_factory=list)
    assistant_memory_start: int = 0
    goal_finalization_fallback: bool = False


def _match_command_with_optional_id(
    text: str,
    commands: frozenset[str],
) -> tuple[bool, str | None]:
    normalized = " ".join(text.split()).lower()
    for command in sorted(commands, key=len, reverse=True):
        if normalized == command:
            return True, None
        prefix = f"{command} "
        if normalized.startswith(prefix):
            request_id = normalized[len(prefix) :].strip()
            if request_id:
                return True, request_id
    return False, None


def _extract_memory_entry_payload(entry: Any) -> dict[str, Any] | None:
    """提取内存条目里的消息载荷。"""
    if isinstance(entry, list) and entry and isinstance(entry[0], dict):
        return entry[0]
    if isinstance(entry, dict):
        return entry
    return None


def _is_tool_guard_denied_entry(entry: Any) -> bool:
    return (
        isinstance(entry, list)
        and len(entry) >= 2
        and isinstance(entry[1], list)
        and TOOL_GUARD_DENIED_MARK in entry[1]
    )


def _get_agent_memory_content(states: dict[str, Any]) -> list[Any] | None:
    agent_state = states.get("agent", {})
    if not isinstance(agent_state, dict):
        return None

    memory_state = agent_state.get("memory", {})
    if not isinstance(memory_state, dict):
        return None

    content = memory_state.get("content", [])
    if not isinstance(content, list) or not content:
        return None
    return content


@dataclass(frozen=True)
class _PersistedMemorySnapshot:
    content: list[Any]


def _last_tool_guard_denied_index(content: list[Any]) -> int | None:
    for index in range(len(content) - 1, -1, -1):
        if _is_tool_guard_denied_entry(content[index]):
            return index
    return None


def _is_assistant_memory_entry(entry: Any) -> bool:
    return (
        isinstance(entry, list)
        and len(entry) >= 1
        and isinstance(entry[0], dict)
        and entry[0].get("role") == "assistant"
    )


def _remove_following_denial_explanation(
    content: list[Any],
    denied_entry_index: int | None,
) -> bool:
    if denied_entry_index is None:
        return False

    explanation_index = denied_entry_index + 1
    if explanation_index >= len(content):
        return False

    if not _is_assistant_memory_entry(content[explanation_index]):
        return False

    del content[explanation_index]
    return True


def _strip_tool_guard_denied_marks(content: list[Any]) -> int:
    stripped_count = 0
    for entry in content:
        if _is_tool_guard_denied_entry(entry):
            entry[1].remove(TOOL_GUARD_DENIED_MARK)
            stripped_count += 1
    return stripped_count


def _build_denial_response_memory_entry(
    denial_response: Msg,
) -> list[Any]:
    ts = getattr(denial_response, "timestamp", None)
    msg_dict = {
        "id": getattr(denial_response, "id", ""),
        "name": getattr(denial_response, "name", "Friday"),
        "role": getattr(denial_response, "role", "assistant"),
        "content": denial_response.content,
        "metadata": getattr(
            denial_response,
            "metadata",
            None,
        ),
        "timestamp": str(ts) if ts is not None else "",
    }
    return [msg_dict, []]


def _extract_text_from_message_content(content: Any) -> str:
    """从消息内容中提取可展示文本。"""
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = ""
        if block.get("type") == "text":
            text = str(block.get("text", "") or "")
        elif block.get("type") == "thinking":
            text = str(block.get("thinking", "") or "")
        if text.strip():
            texts.append(text.strip())
    return "\n".join(texts).strip()


def _approval_message_key_from_text(text: str) -> tuple[str, str] | None:
    normalized = " ".join(text.split())
    request_id = _approval_request_id(normalized)
    if request_id:
        return ("approve", request_id)

    request_id = _denial_request_id(normalized)
    if request_id:
        return ("deny", request_id)

    return None


def _external_approval_message_key(
    message: dict[str, Any],
) -> tuple[str, str] | None:
    if message.get("role") != "user":
        return None

    text_key = _approval_message_key_from_text(
        _extract_text_from_message_content(message.get("content")),
    )
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return text_key

    meta_decision = metadata.get(_APPROVAL_DECISION_META_KEY)
    if isinstance(meta_decision, str):
        meta_decision = meta_decision.strip().lower()
    if meta_decision not in {"approve", "deny"}:
        meta_decision = text_key[0] if text_key else None

    meta_request_id = metadata.get(_APPROVAL_REQUEST_ID_META_KEY)
    if isinstance(meta_request_id, str) and meta_request_id.strip():
        request_id = meta_request_id.strip().lower()
    else:
        request_id = text_key[1] if text_key else None

    if meta_decision and request_id:
        return (meta_decision, request_id)
    return text_key


def _dedupe_external_approval_messages_from_state(
    agent_state: dict[str, Any],
) -> int:
    memory_state = agent_state.get("memory")
    if not isinstance(memory_state, dict):
        return 0

    content = memory_state.get("content")
    if not isinstance(content, list):
        return 0

    external_keys: set[tuple[str, str]] = set()
    for entry in content:
        message = _extract_memory_entry_payload(entry)
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if not metadata.get(_EXTERNAL_APPROVAL_MESSAGE_META_KEY):
            continue
        key = _external_approval_message_key(message)
        if key is not None:
            external_keys.add(key)

    if not external_keys:
        return 0

    kept_entries: list[Any] = []
    removed = 0
    for entry in content:
        message = _extract_memory_entry_payload(entry)
        if not isinstance(message, dict):
            kept_entries.append(entry)
            continue

        metadata = message.get("metadata")
        is_external_entry = isinstance(metadata, dict) and bool(
            metadata.get(_EXTERNAL_APPROVAL_MESSAGE_META_KEY),
        )
        key = _external_approval_message_key(message)
        if key in external_keys and not is_external_entry:
            removed += 1
            continue
        kept_entries.append(entry)

    if removed:
        memory_state["content"] = kept_entries

    return removed


def _build_task_run_record(
    memory_entries: list[Any],
    *,
    memory_start: int,
    execution_key: str | None = None,
) -> dict[str, Any] | None:
    """根据本次新增消息构建任务运行元数据。"""
    if not memory_entries:
        return None

    started_at, ended_at = _task_run_timestamps(memory_entries)
    preview_text = _task_run_preview(memory_entries)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started_at = started_at or now
    ended_at = ended_at or started_at

    record = {
        "run_id": f"task-run-{uuid4()}",
        "started_at": started_at,
        "ended_at": ended_at,
        "memory_start": memory_start,
        "memory_end": memory_start + len(memory_entries),
        "preview_text": preview_text,
    }
    if execution_key:
        record["execution_key"] = execution_key
    return record


def _task_run_timestamps(
    memory_entries: list[Any],
) -> tuple[str | None, str | None]:
    started_at: str | None = None
    ended_at: str | None = None
    for entry in memory_entries:
        payload = _extract_memory_entry_payload(entry)
        timestamp = payload.get("timestamp") if payload else None
        if not isinstance(timestamp, str) or not timestamp:
            continue
        started_at = started_at or timestamp
        ended_at = timestamp
    return started_at, ended_at


def _task_run_preview(memory_entries: list[Any]) -> str:
    for entry in reversed(memory_entries):
        payload = _extract_memory_entry_payload(entry)
        if not payload or payload.get("role") != "assistant":
            continue
        preview_text = _extract_text_from_message_content(
            payload.get("content"),
        )
        if preview_text:
            return preview_text
    return ""


def _is_approval(text: str) -> bool:
    """Return True when *text* is an approve command.

    The command may optionally include an approval request id.
    """
    matched, _request_id = _match_command_with_optional_id(
        text,
        _APPROVE_EXACT,
    )
    return matched


def _is_denial(text: str) -> bool:
    """Return True only when *text* is an explicit deny command."""
    matched, _request_id = _match_command_with_optional_id(text, _DENY_EXACT)
    return matched


def _approval_request_id(text: str) -> str | None:
    matched, request_id = _match_command_with_optional_id(
        text,
        _APPROVE_EXACT,
    )
    return request_id if matched else None


def _denial_request_id(text: str) -> str | None:
    matched, request_id = _match_command_with_optional_id(text, _DENY_EXACT)
    return request_id if matched else None


def _approval_replay_metadata(record) -> dict[str, Any] | None:
    if not isinstance(record.extra, dict):
        return None
    approval_kind = record.extra.get("approval_kind", "tool_guard")
    if approval_kind != "hook_pre_tool_use":
        return None
    tool_call = record.extra.get("tool_call")
    if not isinstance(tool_call, dict):
        return None
    return {
        "request_id": record.request_id,
        "approval_kind": approval_kind,
        "tool_call_id": tool_call.get("id", ""),
        "tool_name": tool_call.get("name") or record.tool_name,
        "tool_input": tool_call.get("input", {}),
        "hook_ask_handler_ids": list(
            record.extra.get("hook_ask_handler_ids") or [],
        ),
    }


def _approved_tool_call_from_record(record) -> dict[str, Any] | None:
    """从审批记录恢复需要重放的工具调用和队列上下文。"""
    if not isinstance(record.extra, dict):
        return None
    candidate = record.extra.get("tool_call")
    if not isinstance(candidate, dict):
        return None

    approved_tool_call = dict(candidate)
    _copy_list_extra(
        approved_tool_call,
        record.extra,
        source_key="sibling_tool_calls",
        target_key="_sibling_tool_calls",
    )
    _copy_list_extra(
        approved_tool_call,
        record.extra,
        source_key="remaining_queue",
        target_key="_remaining_queue",
    )
    _copy_list_extra(
        approved_tool_call,
        record.extra,
        source_key="thinking_blocks",
        target_key="_thinking_blocks",
    )
    replay_metadata = _approval_replay_metadata(record)
    if replay_metadata is not None:
        approved_tool_call["_approval_replay"] = replay_metadata
    from .operation_group import restore_operation_group_argument

    return restore_operation_group_argument(
        approved_tool_call,
        record.extra.get("operation_group"),
    )


def _build_denial_response_msg(pending: Any, text: str) -> Msg:
    """Build the denial message, optionally marking the pending tool call.

    When the pending record still carries the original tool call, the
    message embeds a structured tool_result with error_type
    "approval_rejected" so the Console can turn the never-executed
    sub-step into "已拒绝" instead of an execution failure.  The text
    block keeps the existing user-visible denial message.
    """
    blocks: list[Any] = []
    governance_tool_call_id = ""
    extra = getattr(pending, "extra", None)
    if isinstance(extra, dict):
        tool_call = extra.get("tool_call")
        if isinstance(tool_call, dict) and tool_call.get("id"):
            governance_tool_call_id = str(tool_call["id"])
            result_block = {
                "type": "tool_result",
                "id": tool_call.get("id", ""),
                "name": tool_call.get("name")
                or getattr(pending, "tool_name", ""),
                TOOL_GOVERNANCE_BLOCK_FIELD: "rejected",
                "output": {
                    "isError": True,
                    "error_type": "approval_rejected",
                    "content": [
                        {
                            "type": "text",
                            "text": "该工具调用已被拒绝，未执行。",
                        },
                    ],
                },
            }
            operation_group = extra.get("operation_group")
            if isinstance(operation_group, dict):
                result_block["operation_group"] = operation_group
            blocks.append(result_block)
    blocks.append(TextBlock(type="text", text=text))
    message = Msg(name="Friday", role="assistant", content=blocks)
    if governance_tool_call_id:
        attach_tool_governance_message_metadata(
            message,
            tool_call_id=governance_tool_call_id,
            governance_status="rejected",
        )
    return message


def _copy_list_extra(
    target: dict[str, Any],
    extra: dict[str, Any],
    *,
    source_key: str,
    target_key: str,
) -> None:
    value = extra.get(source_key)
    if isinstance(value, list):
        target[target_key] = value


async def _select_pending_approval(
    svc,
    *,
    session_id: str,
    request_id: str | None,
):
    if request_id:
        pending = await svc.get_request(request_id)
        if (
            pending is not None
            and pending.session_id == session_id
            and pending.status == "pending"
        ):
            return pending
        return None
    return await svc.get_pending_by_session(session_id)


def _load_tenant_hook_config(tenant_id: str | None) -> HookConfig:
    try:
        from ...config.utils import get_tenant_config_path, load_config

        config_path = get_tenant_config_path(tenant_id) if tenant_id else None
        return load_config(config_path).hooks
    except Exception:
        logger.debug("Failed to load tenant hook config", exc_info=True)
        return HookConfig()


def _hook_config_enabled(
    tenant_hooks: HookConfig | None,
    agent_config: Any,
    session_state: HookSessionState | None = None,
) -> bool:
    agent_hooks = getattr(agent_config, "hooks", None)
    return bool(
        (tenant_hooks is not None and tenant_hooks.enabled)
        or (agent_hooks is not None and agent_hooks.enabled)
        or (
            session_state is not None
            and (
                session_state.has_loaded_skill_sources()
                or session_state.has_monitored_skill_sources()
            )
        ),
    )


async def _load_session_hook_overlay(
    session: Any | None,
    *,
    session_id: str,
    user_id: str,
    session_execution: Any = None,
) -> HookSessionOverlay:
    if session is None or not session_id:
        return HookSessionOverlay()
    try:
        if session_execution is not None:
            state = await session_execution.read_state()
        else:
            state = await session.get_session_state_dict(
                session_id=session_id,
                user_id=user_id,
                allow_not_exist=True,
            )
    except Exception:
        logger.debug("Failed to load hook overlay from session", exc_info=True)
        return HookSessionOverlay()
    raw_overlay = (
        state.get("hook_overlay") if isinstance(state, dict) else None
    )
    if not isinstance(raw_overlay, dict):
        return HookSessionOverlay()
    try:
        return HookSessionOverlay.model_validate(raw_overlay)
    except Exception:
        logger.warning("Invalid hook_overlay session state", exc_info=True)
        return HookSessionOverlay()


def _load_tenant_approved_skill_hook_http_urls(
    tenant_id: str | None,
) -> set[str]:
    try:
        from ...config.utils import get_tenant_config_path, load_config

        config_path = get_tenant_config_path(tenant_id) if tenant_id else None
        security = load_config(config_path).security
        skill_hook_http = getattr(security, "skill_hook_http", None)
        urls = getattr(skill_hook_http, "approved_urls", None) or []
        return {str(url) for url in urls if str(url).strip()}
    except Exception:
        logger.debug(
            "Failed to load tenant skill hook HTTP approvals",
            exc_info=True,
        )
        return set()


def _create_session_skill_detector(
    *,
    workspace_dir: Path,
    tenant_id: str | None,
    user_id: str,
    session_id: str,
    channel: str,
    source_id: str,
    enabled_skills: list[str],
    skill_runtime_profiles: dict[str, Any] | None = None,
    skill_metadata: dict[str, Any] | None = None,
    skill_dirs: dict[str, Path] | None = None,
    skill_signatures: dict[str, str] | None = None,
    get_hook_state: Callable[[], HookSessionState],
    set_hook_state: Callable[[HookSessionState], None],
    approved_http_urls: Collection[str] | None = None,
    confirmed_skill_callback: Callable[[str], Any] | None = None,
    skill_tool_registry: Any | None = None,
) -> SkillInvocationDetector:
    workspace = Path(workspace_dir)
    approvals = (
        set(approved_http_urls)
        if approved_http_urls is not None
        else _load_tenant_approved_skill_hook_http_urls(tenant_id)
    )

    async def _load_skill_hooks(skill_name: str) -> None:
        skill_root = (skill_dirs or {}).get(skill_name)
        if skill_root is None:
            skill_root = resolve_effective_skill_dir(workspace, skill_name)
        if skill_root is None:
            return
        expected_signature = (skill_signatures or {}).get(skill_name)
        if expected_signature:
            from ...agents.skills_manager import _build_signature

            actual_signature = await asyncio.to_thread(
                _build_signature,
                skill_root,
            )
            if actual_signature != expected_signature:
                logger.warning(
                    "Skipping hooks for changed skill '%s'",
                    skill_name,
                )
                return
        try:
            next_state = await asyncio.to_thread(
                load_skill_hooks_for_session,
                skill_name=skill_name,
                skill_root=skill_root,
                workspace_dir=workspace,
                session_state=get_hook_state(),
                approved_http_urls=approvals,
            )
        except SkillHookLoadError as exc:
            if exc.session_state is not None:
                set_hook_state(exc.session_state)
            logger.warning(
                "Rejected hooks for skill '%s': %s",
                skill_name,
                exc,
            )
            return
        set_hook_state(next_state)

    detector = SkillInvocationDetector(
        registry=skill_tool_registry,
        user_id=user_id,
        session_id=session_id,
        channel=channel,
        source_id=source_id,
        workspace_dir=workspace_dir,
        skill_hook_loader=_load_skill_hooks,
        confirmed_skill_callback=confirmed_skill_callback,
    )
    detector.set_enabled_skills(enabled_skills, skill_metadata)
    if skill_runtime_profiles:
        detector.set_skill_runtime_profiles(skill_runtime_profiles)
    return detector


def _build_runner_hook_context(
    event_name: HookEventName,
    *,
    request: Any,
    runner: "AgentRunner",
    prompt: str | None = None,
    assistant_response: str | None = None,
    source: str | None = None,
    model: str | None = None,
) -> HookContext:
    session_id = str(getattr(request, "session_id", "") or "")
    user_id = str(getattr(request, "user_id", "") or "")
    channel = str(
        getattr(request, "channel", DEFAULT_CHANNEL) or DEFAULT_CHANNEL,
    )
    channel_meta = getattr(request, "channel_meta", {}) or {}
    workspace_dir = Path(runner.workspace_dir or WORKING_DIR)
    transcript_path = ""
    session_obj = getattr(runner, "session", None)
    if session_obj is not None and hasattr(session_obj, "_get_save_path"):
        try:
            transcript_path = str(
                session_obj._get_save_path(session_id, user_id),
            )
        except Exception:
            transcript_path = ""

    effective_tenant_id = runner.tenant_id or "default"
    try:
        from ...config.context import get_current_effective_tenant_id

        effective_tenant_id = (
            get_current_effective_tenant_id() or effective_tenant_id
        )
    except Exception:
        pass

    return HookContext(
        session_id=session_id,
        transcript_path=transcript_path,
        cwd=str(workspace_dir),
        hook_event_name=event_name,
        tenant_id=runner.tenant_id or effective_tenant_id,
        effective_tenant_id=effective_tenant_id,
        user_id=user_id,
        agent_id=runner.agent_id,
        channel=channel,
        source_id=getattr(request, "source_id", None)
        or channel_meta.get("source_id"),
        trace_id=getattr(request, "trace_id", None),
        workspace_dir=str(workspace_dir),
        chat_id=channel_meta.get("chat_id"),
        turn_id=channel_meta.get("turn_id"),
        prompt=prompt,
        assistant_response=assistant_response,
        source=source,
        model=model,
    )


async def _emit_runner_hook(
    event_name: HookEventName,
    *,
    request: Any,
    runner: "AgentRunner",
    tenant_hooks: HookConfig,
    agent_config: Any,
    overlay: HookSessionOverlay,
    prompt: str | None = None,
    assistant_response: str | None = None,
    source: str | None = None,
    model: str | None = None,
    agent: Any | None = None,
    session_execution: Any = None,
) -> MergedHookResult:
    agent_hooks = getattr(agent_config, "hooks", None)
    if not isinstance(agent_hooks, HookConfig):
        agent_hooks = HookConfig()
    runtime = HookRuntime(
        tenant_config=tenant_hooks,
        agent_config=agent_hooks,
        session_overlay=overlay,
    )
    context = _build_runner_hook_context(
        event_name,
        request=request,
        runner=runner,
        prompt=prompt,
        assistant_response=assistant_response,
        source=source,
        model=model,
    )

    async def _conversation_snapshot_provider():
        if agent is not None:
            return await capture_conversation_snapshot(
                getattr(agent, "memory", None),
            )
        return await _capture_persisted_runner_conversation_snapshot(
            request=request,
            runner=runner,
            session_execution=session_execution,
        )

    return await runtime.emit(
        context,
        workspace_dir=Path(runner.workspace_dir or WORKING_DIR),
        conversation_snapshot_provider=_conversation_snapshot_provider,
    )


def _emit_runner_stop_skip_telemetry(
    *,
    request: Any,
    runner: "AgentRunner",
    prompt: str | None,
    assistant_response: str | None,
    skipped_reason: str,
) -> None:
    try:
        log_stop_skipped_telemetry(
            _build_runner_hook_context(
                HookEventName.STOP,
                request=request,
                runner=runner,
                prompt=prompt,
                assistant_response=assistant_response,
            ),
            skipped_reason=skipped_reason,
        )
    except Exception as exc:
        logger.warning("Failed to emit skipped Stop telemetry: %s", exc)


def _build_stop_hook_runtime(
    *,
    tenant_hooks: HookConfig,
    agent_config: Any,
    overlay: HookSessionOverlay,
) -> HookRuntime:
    agent_hooks = getattr(agent_config, "hooks", None)
    if not isinstance(agent_hooks, HookConfig):
        agent_hooks = HookConfig()
    return HookRuntime(
        tenant_config=tenant_hooks,
        agent_config=agent_hooks,
        session_overlay=overlay,
    )


def _requires_stop_output_buffer(
    *,
    request: Any,
    runner: "AgentRunner",
    tenant_hooks: HookConfig,
    agent_config: Any,
    overlay: HookSessionOverlay,
    prompt: str,
) -> bool:
    context = _build_runner_hook_context(
        HookEventName.STOP,
        request=request,
        runner=runner,
        prompt=prompt,
    )
    return _build_stop_hook_runtime(
        tenant_hooks=tenant_hooks,
        agent_config=agent_config,
        overlay=overlay,
    ).requires_stop_output_buffer(
        context,
        workspace_dir=runner.workspace_dir,
    )


async def _emit_runner_stop_finalization(
    *,
    request: Any,
    runner: "AgentRunner",
    tenant_hooks: HookConfig,
    agent_config: Any,
    overlay: HookSessionOverlay,
    prompt: str,
    assistant_response: str,
    agent: Any,
    max_transform_seconds: float,
) -> StopHookExecutionResult:
    context = _build_runner_hook_context(
        HookEventName.STOP,
        request=request,
        runner=runner,
        prompt=prompt,
        assistant_response=assistant_response,
    )

    async def _conversation_snapshot_provider():
        return await capture_conversation_snapshot(
            getattr(agent, "memory", None),
        )

    return await _build_stop_hook_runtime(
        tenant_hooks=tenant_hooks,
        agent_config=agent_config,
        overlay=overlay,
    ).emit_stop_finalization(
        context,
        workspace_dir=Path(runner.workspace_dir or WORKING_DIR),
        max_transform_seconds=max_transform_seconds,
        conversation_snapshot_provider=_conversation_snapshot_provider,
    )


async def _capture_persisted_runner_conversation_snapshot(
    *,
    request: Any,
    runner: "AgentRunner",
    session_execution: Any = None,
) -> dict[str, Any] | None:
    if getattr(request, "skip_history", False):
        return None

    session_id = getattr(request, "session_id", None)
    if not session_id:
        return None

    try:
        if session_execution is not None:
            state = await session_execution.read_state()
        else:
            session = getattr(runner, "session", None)
            get_session_state_dict = getattr(
                session,
                "get_session_state_dict",
                None,
            )
            if not callable(get_session_state_dict):
                return None
            state = await get_session_state_dict(
                session_id=_coerce_session_storage_id(session_id),
                user_id=_coerce_session_storage_user_id(
                    getattr(request, "user_id", None),
                ),
                allow_not_exist=True,
            )
    except Exception:
        logger.debug(
            "Failed to load persisted memory for hook snapshot",
            exc_info=True,
        )
        return None

    if not isinstance(state, dict):
        return None

    content = _get_agent_memory_content(state)
    if content is None:
        return None

    return await capture_conversation_snapshot(
        _PersistedMemorySnapshot(content=content),
    )


def _format_hook_additional_context(result: MergedHookResult) -> str:
    if not result.additional_context:
        return ""
    lines = []
    for item in result.additional_context:
        lines.append(f"[{item.handler_id}] {item.context}")
    return "\n".join(lines)


def _hook_block_message(result: MergedHookResult) -> Msg:
    reason = result.reason or "Hook blocked this request."
    return Msg(name="Friday", role="assistant", content=reason)


def _resolve_active_model_label(tenant_id: str | None) -> str | None:
    try:
        from ..crons.model_slot_context import (
            get_current_model_slot_override,
        )
        from ...providers.provider_manager import ProviderManager

        override = get_current_model_slot_override()
        if override and override.provider_id and override.model:
            return f"{override.provider_id}/{override.model}"
        manager = ProviderManager.get_instance(tenant_id)
        active = manager.get_active_model()
        if active and active.provider_id and active.model:
            return f"{active.provider_id}/{active.model}"
    except Exception:
        logger.debug(
            "Failed to resolve active model for hook context",
            exc_info=True,
        )
    return None


async def _build_and_connect_mcp_clients(
    mcp_config: MCPConfig | None,
    passthrough_headers: dict[str, str] | None = None,
    session_id: str | None = None,
    chat_id: str | None = None,
    trace_id: str | None = None,
) -> list[Any]:
    """Build and connect MCP clients from config for single request use.

    Args:
        mcp_config: MCP configuration from agent_config.mcp
        passthrough_headers: Headers to merge for HTTP transport clients
        session_id: Request-scoped session identifier for reserved headers
        chat_id: Persistent chat UUID for reserved transport claims
        trace_id: Request-scoped trace identifier for reserved headers

    Returns:
        List of connected MCP client instances (all created for this request)
    """
    started_at = time.perf_counter()
    if mcp_config is None or not mcp_config.clients:
        logger.debug(
            "mcp_client_connect_duration_ms=%d client_count=0",
            int((time.perf_counter() - started_at) * 1000),
        )
        return []

    clients = []
    for key, client_config in mcp_config.clients.items():
        if not client_config.enabled:
            continue

        try:
            client = await _create_mcp_client_with_headers(
                client_config,
                passthrough_headers,
                session_id=session_id,
                chat_id=chat_id,
                trace_id=trace_id,
            )
            if client is not None:
                await client.connect(timeout=_MCP_CONNECT_TIMEOUT_SECONDS)
                clients.append(client)
                logger.info(f"MCP client '{key}' created and connected")
        except asyncio.CancelledError:
            # MCP 连接阶段的取消（如远端 502 导致），降级跳过而非取消整个查询
            logger.warning(
                f"MCP client '{key}' connection cancelled, skipping",
            )
        except Exception as e:
            logger.warning(
                f"Failed to create MCP client '{key}': {e}",
                exc_info=True,
            )

    logger.debug(
        "mcp_client_connect_duration_ms=%d client_count=%d",
        int((time.perf_counter() - started_at) * 1000),
        len(clients),
    )
    return clients


def _build_lazy_mcp_clients(
    mcp_config: MCPConfig | None,
    *,
    tenant_id: str | None,
    user_id: str | None,
    passthrough_headers: dict[str, str] | None = None,
    session_id: str | None = None,
    chat_id: str | None = None,
    trace_id: str | None = None,
    frozen_tools_by_key: dict[str, list[dict[str, Any]]] | None = None,
) -> list[LazyMCPClient]:
    """Build request-lazy MCP clients without opening transport sessions."""
    if mcp_config is None or not mcp_config.clients:
        return []

    clients: list[LazyMCPClient] = []
    for key, client_config in mcp_config.clients.items():
        if not client_config.enabled:
            continue

        effective_passthrough_headers = passthrough_headers

        config_payload = client_config.model_dump(mode="json")
        config_fingerprint = hashlib.sha256(
            json.dumps(
                config_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ).hexdigest()
        request_scope_headers = {
            header_name.casefold(): header_value
            for header_name, header_value in (
                _filter_passthrough_headers(
                    effective_passthrough_headers,
                    url=getattr(client_config, "url", None),
                )
                or {}
            ).items()
        }
        request_scope_fingerprint = hashlib.sha256(
            json.dumps(
                request_scope_headers,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ).hexdigest()
        discovery_key = ":".join(
            [
                tenant_id or "default",
                user_id or "default",
                key,
                config_fingerprint,
                request_scope_fingerprint,
            ],
        )

        async def create_client(
            config: MCPClientConfig = client_config,
            headers: dict[str, str] | None = effective_passthrough_headers,
            request_session_id: str | None = session_id,
            request_chat_id: str | None = chat_id,
            request_trace_id: str | None = trace_id,
        ) -> Any:
            return await _create_mcp_client_with_headers(
                config,
                headers,
                session_id=request_session_id,
                chat_id=request_chat_id,
                trace_id=request_trace_id,
            )

        clients.append(
            LazyMCPClient(
                name=client_config.name,
                discovery_key=discovery_key,
                create_client=create_client,
                discovery_cache=get_mcp_tool_discovery_cache(),
                connect_timeout=_MCP_CONNECT_TIMEOUT_SECONDS,
                frozen_tools=(frozen_tools_by_key or {}).get(
                    getattr(client_config, "market_client_key", "") or key,
                ),
            ),
        )
    return clients


async def _create_mcp_client_with_headers(
    client_config: MCPClientConfig,
    passthrough_headers: dict[str, str] | None = None,
    session_id: str | None = None,
    chat_id: str | None = None,
    trace_id: str | None = None,
) -> Any:
    """Create a single MCP client with optional header passthrough.

    For HTTP transport, merges static config headers with passthrough headers.
    For StdIO transport, uses static config directly.

    Args:
        client_config: Single MCP client configuration
        passthrough_headers: Headers to merge for HTTP transport
        session_id: Request-scoped session identifier for reserved headers
        chat_id: Persistent chat UUID for reserved transport claims
        trace_id: Request-scoped trace identifier for reserved headers

    Returns:
        MCP client instance (not yet connected)
    """
    rebuild_info = {
        "name": client_config.name,
        "transport": client_config.transport,
        "url": client_config.url,
        "headers": client_config.headers or None,
        "passthrough_headers": dict(passthrough_headers or {}) or None,
        "session_id": session_id,
        "chat_id": chat_id,
        "trace_id": trace_id,
        "timeout": _MCP_HTTP_TIMEOUT_SECONDS,
        "sse_read_timeout": _MCP_HTTP_SSE_READ_TIMEOUT_SECONDS,
        "command": client_config.command,
        "args": list(client_config.args),
        "env": dict(client_config.env),
        "cwd": client_config.cwd or None,
    }

    if client_config.transport == "stdio":
        launch_config = build_tenant_aware_stdio_launch_config(
            client_config.command,
            client_config.args,
            client_config.env,
            client_config.cwd or None,
            chat_id=chat_id,
        )
        client = StdIOStatefulClient(
            name=client_config.name,
            command=launch_config.launch_command,
            args=launch_config.launch_args,
            env=launch_config.env,
            cwd=launch_config.cwd,
        )
        setattr(
            client,
            "_swe_rebuild_info",
            {
                **rebuild_info,
                "launch_command": launch_config.launch_command,
                "launch_args": launch_config.launch_args,
                "launch_diagnostic": launch_config.diagnostic,
            },
        )
        setattr(client, "_swe_temp_client", True)
        return client

    # HTTP transport (streamable_http or sse)
    merged_headers = build_mcp_http_headers(
        client_config.headers,
        passthrough_headers=passthrough_headers,
        url=client_config.url,
        session_id=session_id,
        chat_id=chat_id,
        trace_id=trace_id,
    )

    client = HttpStatefulClient(
        name=client_config.name,
        transport=client_config.transport,
        url=client_config.url,
        headers=merged_headers,
        timeout=_MCP_HTTP_TIMEOUT_SECONDS,
        sse_read_timeout=_MCP_HTTP_SSE_READ_TIMEOUT_SECONDS,
    )

    setattr(
        client,
        "_swe_rebuild_info",
        {
            **rebuild_info,
            "_temp_client": True,
        },
    )
    setattr(client, "_swe_temp_client", True)

    return client


def _consume_background_task_exception(task: asyncio.Task[Any]) -> None:
    """取回后台任务异常，避免未消费异常泄露到事件循环。"""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


async def _cleanup_mcp_clients(clients: list[Any]) -> None:
    """Compatibility Adapter for request-scoped MCP cleanup."""
    await query_cleanup.cleanup_mcp_clients(clients)


def _assistant_response_candidate(
    index: int,
    entry: Any,
) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(entry, (tuple, list)) or not entry:
        return None, {"index": index, "reason": "invalid_memory_entry"}

    msg = entry[0]
    role = getattr(msg, "role", None)
    content = getattr(msg, "content", None)
    metadata = getattr(msg, "metadata", None)
    summary: dict[str, Any] = {
        "index": index,
        "role": role,
        "content_type": type(content).__name__,
        "metadata_fields": [
            key
            for key in ("event_type", "message_type", "kind", "type")
            if isinstance(metadata, dict) and key in metadata
        ],
    }
    if isinstance(content, list):
        summary["block_types"] = [
            (
                block.get("type")
                if isinstance(block, dict)
                else getattr(block, "type", None)
            )
            for block in content
        ]
    if (
        role != "assistant"
        or not hasattr(msg, "content")
        or _is_live_assistant_event(msg)
    ):
        summary["reason"] = (
            "role_or_missing_content"
            if role != "assistant" or not hasattr(msg, "content")
            else "live_assistant_event"
        )
        return None, summary

    response = project_candidate_assistant_response(msg)
    if response is not None:
        summary["text_len"] = len(response)
        summary["reason"] = "accepted"
        return response, summary
    summary["reason"] = "unsupported_content"
    return None, summary


def _extract_assistant_response(
    agent: SWEAgent,
    *,
    memory_start: int = 0,
) -> str:
    """从 agent memory 的当前 turn 中提取最后的助手响应文本."""
    if not agent or not hasattr(agent, "memory"):
        logger.warning(
            "[STOP-DEBUG] extract reason=missing_agent_memory "
            "memory_start=%d",
            memory_start,
        )
        return ""

    try:
        memory = agent.memory.content
        start = max(memory_start, 0)
        memory_total = len(memory) if isinstance(memory, list) else None
        candidates: list[dict[str, Any]] = []
        entries = (
            list(enumerate(memory[start:], start))
            if isinstance(memory, list)
            else []
        )
        for index, entry in reversed(entries):
            response, summary = _assistant_response_candidate(index, entry)
            if response is not None:
                logger.warning(
                    "[STOP-DEBUG] extract memory_total=%s memory_start=%d "
                    "selected=%s candidates=%s",
                    memory_total,
                    start,
                    summary,
                    candidates,
                )
                return response
            candidates.append(summary)
        logger.warning(
            "[STOP-DEBUG] extract memory_total=%s memory_start=%d "
            "selected=None candidates=%s",
            memory_total,
            start,
            candidates,
        )
    except Exception as e:
        logger.warning(
            "[STOP-DEBUG] extract reason=exception error_type=%s",
            type(e).__name__,
        )

    return ""


def _replace_assistant_response(
    agent: SWEAgent,
    response: str,
    *,
    memory_start: int = 0,
) -> bool:
    if not agent or not hasattr(agent, "memory"):
        return False
    try:
        memory = agent.memory.content
        for msg, _marks in reversed(memory[max(memory_start, 0) :]):
            if msg.role != "assistant" or _is_live_assistant_event(msg):
                continue
            if replace_candidate_assistant_response(msg, response):
                return True
    except Exception as exc:
        logger.debug("Failed to replace assistant response: %s", exc)
    return False


def _is_live_assistant_event(msg: Any) -> bool:
    metadata = getattr(msg, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    values = " ".join(
        str(metadata.get(key, ""))
        for key in ("event_type", "message_type", "kind", "type")
    ).lower()
    return any(token in values for token in ("progress", "tool", "approval"))


def _build_internal_follow_up_msg(follow_up_prompt: str) -> Msg:
    """Build a hidden continuation turn for the same agent."""
    return Msg(
        name="system-follow-up",
        role="user",
        content=(
            "[内部续跑指令]\n"
            "继续当前用户任务。不要把本段当作用户的新需求，"
            "不要向用户复述本指令。\n"
            f"{follow_up_prompt.strip()}"
        ),
        metadata={
            _INTERNAL_FOLLOW_UP_METADATA_KEY: True,
        },
    )


def _build_stop_follow_up_msg(reason: str) -> Msg:
    """构造 Stop 阻断后的内部续跑指令。"""
    return _build_internal_follow_up_msg(
        _STOP_FOLLOW_UP_REASON_TEMPLATE.format(
            reason=(reason or "Stop blocked completion").strip(),
        ),
    )


def _build_stop_incomplete_msg(reason: str) -> Msg:
    """构造自动续跑预算耗尽后的显式未完成消息。"""
    return Msg(
        name="Friday",
        role="assistant",
        content=_STOP_INCOMPLETE_MESSAGE_TEMPLATE.format(
            reason=(reason or "Stop blocked completion").strip(),
        ),
    )


def _build_goal_follow_up_msg(
    next_focus: str | None,
    steering: list[str] | None = None,
    contract_context: str | None = None,
) -> Msg:
    """Continue an active Goal without creating a visible user request."""
    steering_text = "\n".join(f"- {item}" for item in steering or [])
    return _build_internal_follow_up_msg(
        "Continue the confirmed Goal Contract. "
        + (f"\n{contract_context}" if contract_context else "")
        + "\n"
        f"Next focus: {(next_focus or 'advance remaining criteria').strip()}"
        + (f"\nNew user steering:\n{steering_text}" if steering_text else ""),
    )


def _build_goal_contract_context(goal: Any) -> str:
    """Keep internal continuations anchored to the durable Contract revision."""
    remaining = [
        "\n".join(
            [
                f"- {item.criterion_id}",
                f"  Requirement: {item.criterion.requirement}",
                f"  Observable assertion: {item.criterion.observable_assertion}",
                f"  Verification method: {item.criterion.verification_method}",
                f"  Expected outcome: {item.criterion.expected_outcome}",
            ],
        )
        for item in goal.criteria
        if not item.verified
    ]
    verified = [
        f"- {item.criterion_id}: verified"
        for item in goal.criteria
        if item.verified
    ]
    failures = [
        f"- {item.criterion_id}: {item.consecutive_failures} consecutive failure(s)"
        for item in goal.criteria
        if item.consecutive_failures
    ]
    constraints = goal.contract.constraints
    must_preserve = getattr(constraints, "must_preserve", [])
    must_not_do = getattr(constraints, "must_not_do", [])
    preserve_text = ", ".join(must_preserve) if must_preserve else "none"
    must_not_text = ", ".join(must_not_do) if must_not_do else "none"
    return (
        f"Contract revision: {goal.revision}\n"
        f"Objective: {goal.contract.objective}\n"
        "Constraints:\n"
        f"- must_preserve: {preserve_text}\n"
        f"- must_not_do: {must_not_text}\n"
        f"Autonomy boundary: {goal.contract.autonomy_boundary}\n"
        "Verified completion criteria:\n"
        + ("\n".join(verified) if verified else "- none")
        + "\n"
        "Unverified completion criteria:\n"
        + ("\n".join(remaining) if remaining else "- none")
        + "\nVerification failures:\n"
        + ("\n".join(failures) if failures else "- none")
    )


def _build_goal_finalization_msg(state: str, reason: str | None) -> Msg:
    """Emit the only terminal chat event for a Goal request."""
    messages = {
        "COMPLETE": (
            "Goal Completion Judge accepted all confirmed criteria. "
            "The Goal is complete."
        ),
        "PAUSED": "Goal is paused. You can resume it when ready.",
        "BLOCKED": "Goal is blocked and needs your direction.",
        "LIMITED": "Goal paused after reaching its Main Agent turn budget. Resume to start a new budget cycle.",
        "CANCELLED": "Goal was cancelled.",
        "WAITING": "Goal is waiting for its declared wake condition.",
        "INTERRUPTED": "Goal execution was interrupted. Resume to continue.",
    }
    content = messages.get(state, "Goal execution ended.")
    if reason:
        content = f"{content}\n\nReason: {reason}"
    return Msg(name="Friday", role="assistant", content=content)


_GOAL_FINALIZATION_SYSTEM_PROMPT = """[Goal Finalization]
Produce only the final concise user-facing response for the authoritative Goal
state supplied in the user message. This is a read-only finalization turn: do
not use tools, do not propose more work, do not modify the Goal, and do not
claim evidence beyond that supplied state. For COMPLETE, provide the formal
delivery. For other states, state the reason and the appropriate next step."""

_GOAL_COMPLETION_JUDGE_SYSTEM_PROMPT = """[Goal Completion Judge]
Perform an independent, evidence-based completion review for the authoritative
Goal context supplied in the user message. Use only the available read-only
tools when needed. Do not modify files or the Goal, create plans, delegate,
or provide a user-facing delivery.

Return exactly one JSON object with no prose or markdown:
{"reviews":[{"criterion_id":...,"decision":"accept"|"reject","reason":...,"evidence_refs":[...]}]}
Provide one entry for every supplied criterion. When evidence is insufficient,
reject the criterion and state the missing evidence in its reason."""


def _build_goal_finalization_input(
    goal: Any,
    state: str,
    reason: str | None,
    stop_rejection_reason: str | None = None,
) -> Msg:
    """Build bounded internal input for a tool-free Goal Finalization Turn."""
    stop_feedback = (
        ""
        if not stop_rejection_reason
        else (
            "\nStop rejected the previous delivery. Revise the final response "
            f"to address this feedback: {stop_rejection_reason}\n"
        )
    )
    return _build_internal_follow_up_msg(
        "Authoritative Goal finalization context:\n"
        f"Goal state: {state}\n"
        f"State reason: {reason or 'No additional reason was recorded.'}\n"
        + stop_feedback
        + _build_goal_contract_context(goal),
    )


def _request_goal_id(request: AgentRequest) -> str | None:
    meta = getattr(request, "channel_meta", None) or {}
    value = meta.get("goal_id") if isinstance(meta, dict) else None
    return value if isinstance(value, str) and value else None


def _append_goal_tool_observations(
    observations: list[dict[str, str]],
    msg: Msg,
) -> None:
    """Copy bounded current-turn tool results into a Judge-only package."""
    if len(observations) >= 20 or not isinstance(msg.content, list):
        return
    for block in msg.content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        if block.get("name") in {
            "start_subagent",
            "wait_subagent",
            "get_subagent",
            "cancel_subagent",
        }:
            continue
        output = block.get("output", "")
        try:
            output_text = json.dumps(output, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            output_text = str(output)
        observations.append(
            {
                "tool_call_id": str(block.get("id") or ""),
                "tool_name": str(block.get("name") or ""),
                "output": output_text[:4000],
            },
        )
        if len(observations) >= 20:
            return


def _goal_matches_runtime_scope(
    goal: Any,
    runtime: _QueryRuntime,
    *,
    tenant_id: str | None,
    agent_id: str | None,
) -> bool:
    """Reject a Goal id injected from a different Chat or frozen scope."""
    chat_id = str(getattr(getattr(runtime, "chat", None), "id", "") or "")
    request_context = getattr(runtime.agent, "_request_context", {}) or {}
    source_id = str(request_context.get("source_id") or "default")
    resolved_model = str(
        (getattr(runtime.agent, "_resolved_model_slot", {}) or {}).get("model")
        or "",
    )
    resolved_provider_id = str(
        (getattr(runtime.agent, "_resolved_model_slot", {}) or {}).get(
            "provider_id",
        )
        or "",
    )
    frozen_provider_id = str(
        getattr(goal.scope, "effective_model_provider_id", "") or "",
    )
    return (
        bool(chat_id)
        and goal.scope.chat_id == chat_id
        and goal.scope.tenant_id == str(tenant_id or "default")
        and goal.scope.agent_profile_id == str(agent_id or "default")
        and goal.scope.source_id == source_id
        and (
            not resolved_model
            or goal.scope.effective_model in {"default", resolved_model}
        )
        and (
            not frozen_provider_id
            or not resolved_provider_id
            or frozen_provider_id == resolved_provider_id
        )
    )


def _normalize_session_skill_snapshot(
    snapshot: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for skill_name, entry in snapshot.items():
        if isinstance(skill_name, str) and isinstance(entry, dict):
            normalized[skill_name] = dict(entry)
    return normalized


def _coerce_session_storage_id(session_id: str | None | Any) -> str:
    return "" if session_id is None else str(session_id)


def _coerce_session_storage_user_id(user_id: str | None) -> str:
    return user_id or ""


def _build_session_skill_snapshot_entry(
    *,
    skill_name: str,
    resolved_skill_dir: Path,
    freshness_token: Any,
    confirmed_at: Any | None = None,
) -> dict[str, Any]:
    entry = {
        "skill_name": skill_name,
        "resolved_skill_dir": str(resolved_skill_dir),
        "freshness_token": freshness_token,
    }
    if confirmed_at is not None:
        entry["confirmed_at"] = confirmed_at
    return entry


def _upsert_session_skill_snapshot_entry(
    snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    resolved_skill_dir: Path,
    freshness_token: Any,
    confirmed_at: Any | None = None,
) -> None:
    snapshot[skill_name] = _build_session_skill_snapshot_entry(
        skill_name=skill_name,
        resolved_skill_dir=resolved_skill_dir,
        freshness_token=freshness_token,
        confirmed_at=confirmed_at,
    )


def _remove_session_skill_snapshot_entry(
    snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
) -> None:
    snapshot.pop(skill_name, None)


def _skill_freshness_notice_text(
    changes: list[str],
) -> str:
    return _SKILL_FRESHNESS_NOTICE_HEADER + "\n".join(
        f"- {item}" for item in changes
    )


def _build_skill_freshness_notice_msg(text: str) -> Msg:
    return Msg(
        name="system",
        role="system",
        content=[TextBlock(type="text", text=text)],
        metadata={
            _SKILL_FRESHNESS_NOTICE_METADATA_KEY: True,
        },
    )


@dataclass(frozen=True)
class _SkillFreshnessRefreshResult:
    notice_text: str | None = None
    stored_snapshot: dict[str, dict[str, Any]] | None = None
    refreshed_snapshot: dict[str, dict[str, Any]] | None = None


def _select_restorable_session_skill(
    snapshot: dict[str, dict[str, Any]] | None,
    *,
    enabled_skills: Collection[str],
) -> str | None:
    """从已持久化 snapshot 中选出可恢复的最近确认 skill。"""
    normalized = _normalize_session_skill_snapshot(snapshot)
    candidates = [
        entry
        for skill_name, entry in normalized.items()
        if skill_name in enabled_skills
    ]
    if not candidates:
        return None

    with_confirmed_at = [
        entry for entry in candidates if entry.get("confirmed_at") is not None
    ]
    if with_confirmed_at:
        chosen = max(
            with_confirmed_at,
            key=lambda item: float(item.get("confirmed_at") or 0.0),
        )
        return str(chosen.get("skill_name") or "") or None

    if len(candidates) == 1:
        return str(candidates[0].get("skill_name") or "") or None

    return None


def _can_restore_confirmed_session_skill_context(
    *,
    session_id: str | None,
    session_skill_detector: Any,
    session: Any,
) -> bool:
    """Return whether persisted skill context has the required capabilities."""
    return all(
        (
            session_id,
            session_skill_detector is not None,
            hasattr(session_skill_detector, "restore_confirmed_skill"),
            hasattr(session, "get_session_skill_snapshot"),
        ),
    )


def _supports_session_skill_freshness_refresh(
    *,
    session: Any,
    runtime: "_QueryRuntime",
) -> bool:
    if runtime.skip_history or session is None or not runtime.session_id:
        return False
    if not hasattr(runtime.agent, "get_effective_skills"):
        return False
    return all(
        hasattr(session, attr)
        for attr in (
            "get_session_skill_snapshot",
            "save_session_skill_snapshot",
        )
    )


def _refresh_switched_session_skill_snapshot_entry(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    stored_dir: Path,
    current_dir: Path | None,
    confirmed_at: Any | None = None,
) -> str | None:
    if (
        current_dir is None
        or not current_dir.exists()
        or current_dir == stored_dir
    ):
        return None

    current_token = get_skill_freshness_token(current_dir)
    _upsert_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        resolved_skill_dir=current_dir,
        freshness_token=current_token,
        confirmed_at=confirmed_at,
    )
    return (
        f"{skill_name}: detected skill-directory switch "
        f"{stored_dir} -> {current_dir}. Treat current skill "
        "content as superseding earlier assumptions. You MUST "
        f"re-read {current_dir / 'SKILL.md'} before relying on this skill."
    )


def _refresh_withdrawn_session_skill_snapshot_entry(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    current_dir: Path | None,
) -> str | None:
    if current_dir is not None and current_dir.exists():
        return None

    _remove_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
    )
    return (
        f"{skill_name}: no longer effective for this turn. "
        "Stop relying on earlier assumptions from this skill."
    )


def _refresh_changed_session_skill_snapshot_entry(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    entry: dict[str, Any],
    current_dir: Path,
) -> str | None:
    current_token = get_skill_freshness_token(current_dir)
    if current_token == entry.get("freshness_token"):
        return None

    _upsert_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        resolved_skill_dir=current_dir,
        freshness_token=current_token,
        confirmed_at=entry.get("confirmed_at"),
    )
    return (
        f"{skill_name}: detected skill-directory change at "
        f"{current_dir}. Treat current skill content as "
        "superseding earlier assumptions. You MUST "
        f"re-read {current_dir / 'SKILL.md'} before relying on this skill."
    )


def _refresh_session_skill_snapshot_entry(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    entry: dict[str, Any],
    effective_skill_dirs: dict[str, Path],
) -> str | None:
    stored_dir = Path(str(entry.get("resolved_skill_dir", "")))
    current_dir = effective_skill_dirs.get(skill_name)

    switch_notice = _refresh_switched_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        stored_dir=stored_dir,
        current_dir=current_dir,
        confirmed_at=entry.get("confirmed_at"),
    )
    if switch_notice is not None:
        return switch_notice

    if not stored_dir.exists():
        _remove_session_skill_snapshot_entry(
            next_snapshot,
            skill_name=skill_name,
        )
        return None

    withdrawal_notice = _refresh_withdrawn_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        current_dir=current_dir,
    )
    if withdrawal_notice is not None:
        return withdrawal_notice

    assert current_dir is not None
    return _refresh_changed_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        entry=entry,
        current_dir=current_dir,
    )


def _refresh_session_skill_snapshot_entries(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    stored_snapshot: dict[str, dict[str, Any]],
    effective_skill_dirs: dict[str, Path],
) -> list[str]:
    changes: list[str] = []
    for skill_name, entry in stored_snapshot.items():
        notice = _refresh_session_skill_snapshot_entry(
            next_snapshot,
            skill_name=skill_name,
            entry=entry,
            effective_skill_dirs=effective_skill_dirs,
        )
        if notice is not None:
            changes.append(notice)
    return changes


def _resolve_max_stop_turns(agent_config: Any) -> int:
    """解析 Stop 自动续跑上限，未配置时使用保守默认值。"""
    running_config = getattr(agent_config, "running", None)
    hook_runtime_config = getattr(running_config, "hook_runtime", None)
    configured_turns = getattr(
        hook_runtime_config,
        "max_stop_turns",
        None,
    )
    if configured_turns is None:
        configured_turns = getattr(
            running_config,
            "max_stop_turns",
            2,
        )
    stop_turns = 2 if configured_turns is None else configured_turns
    try:
        return max(int(stop_turns), 0)
    except (TypeError, ValueError):
        return 2


def _resolve_max_stop_transform_seconds(agent_config: Any) -> float:
    running_config = getattr(agent_config, "running", None)
    hook_runtime_config = getattr(running_config, "hook_runtime", None)
    configured_seconds = getattr(
        hook_runtime_config,
        "max_stop_transform_seconds",
        30.0,
    )
    try:
        return max(float(configured_seconds), 0.001)
    except (TypeError, ValueError):
        return 30.0


def _resolve_max_automatic_follow_up_turns(
    agent_config: Any,
    default_limit: int,
) -> int:
    """解析请求级自动续跑总上限，确保多套续跑机制共享同一预算。"""
    running_config = getattr(agent_config, "running", None)
    hook_runtime_config = getattr(running_config, "hook_runtime", None)
    configured_turns = getattr(
        hook_runtime_config,
        "max_automatic_follow_up_turns",
        None,
    )
    if configured_turns is None:
        configured_turns = getattr(
            running_config,
            "max_automatic_follow_up_turns",
            default_limit,
        )
    aggregate_turns = (
        default_limit if configured_turns is None else configured_turns
    )
    try:
        return max(int(aggregate_turns), 0)
    except (TypeError, ValueError):
        return default_limit


def _strip_internal_follow_up_messages_from_state(
    agent_state: dict[str, Any],
) -> int:
    """Remove ephemeral system prompts before persisting session state."""
    memory_state = agent_state.get("memory")
    if not isinstance(memory_state, dict):
        return 0

    content = memory_state.get("content")
    if not isinstance(content, list):
        return 0

    kept_entries = []
    removed = 0
    for entry in content:
        msg_payload = entry[0] if isinstance(entry, list) and entry else None
        metadata = (
            msg_payload.get("metadata")
            if isinstance(msg_payload, dict)
            else None
        )
        if isinstance(metadata, dict) and (
            metadata.get(_INTERNAL_FOLLOW_UP_METADATA_KEY)
            or metadata.get(_SKILL_FRESHNESS_NOTICE_METADATA_KEY)
        ):
            removed += 1
            continue
        kept_entries.append(entry)

    if removed:
        memory_state["content"] = kept_entries

    return removed


async def _index_model_output_to_monitor(
    trace_id: str,
    model_output: str,
) -> None:
    """通过 Monitor API 写入 model_output 到 ES.

    Args:
        trace_id: 追踪 ID
        model_output: 模型输出文本
    """
    monitor_url = os.environ.get(
        "SWE_MONITOR_API_URL",
        "http://127.0.0.1:9090",
    )
    url = f"{monitor_url}/monitor/tracing/model-output"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                url,
                json={
                    "trace_id": trace_id,
                    "model_output": model_output,
                },
            )
            logger.debug(
                "Monitor API response: status=%s, body=%s",
                response.status_code,
                response.text[:200] if response.text else "",
            )
            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "success":
                    logger.info(
                        "Model output indexed via Monitor API: trace_id=%s",
                        trace_id,
                    )
                else:
                    logger.info(
                        "Model output write skipped: trace_id=%s, reason=%s",
                        trace_id,
                        result.get("reason", "unknown"),
                    )
            else:
                logger.warning(
                    "Monitor API returned %s: trace_id=%s",
                    response.status_code,
                    trace_id,
                )
    except httpx.TimeoutException:
        logger.warning("Monitor API timeout: trace_id=%s", trace_id)
    except Exception as e:
        logger.warning(
            "Failed to call Monitor API for model_output: %s",
            e,
        )


def _with_hook_context(
    env_context: str,
    hook_context: str,
) -> str:
    """追加 hook 上下文，避免主流程重复拼接同一段格式。"""
    if not hook_context:
        return env_context
    return f"{env_context}\n\n[Hook additional context]\n{hook_context}"


def _request_system_prompt_injections(request: AgentRequest) -> list[str]:
    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "system_prompt_injections", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("system_prompt_injections")
    return _normalize_system_prompt_injections(value)


def _request_selected_skill_names(request: AgentRequest) -> list[object]:
    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "selected_skill_names", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("selected_skill_names")
    return list(value) if isinstance(value, list) else []


def _request_selected_expert_id(request: AgentRequest) -> str | None:
    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "selected_expert_id", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("selected_expert_id")
    if isinstance(value, str):
        value = value.strip()
    return value or None


def _selected_expert_start_tool_call(
    *,
    workspace_dir: Path,
    tenant_id: str | None,
    agent_id: str,
    selected_expert_id: str,
    objective: str,
) -> dict[str, Any] | None:
    """Build the exact forced start call for one enabled local expert.

    The Definition ID is a management identity, whereas the Background
    SubAgent tool deliberately accepts only its runtime name.  Resolve that
    mapping server-side so a submitted composer selection cannot be silently
    skipped or changed by the Main Agent.
    """
    from ..subagents import (
        AgentOwnedDefinitionRepository,
        builtin_definition_provider,
    )

    try:
        builtin_names = {
            item.name
            for item in builtin_definition_provider().list_definitions()
        }
        package = AgentOwnedDefinitionRepository(
            workspace_dir / "agents",
            owner_scope=f"{tenant_id or 'default'}/{agent_id}",
            builtin_names=builtin_names,
        ).get(selected_expert_id)
    except ValueError:
        return None
    definition = package.definition if package is not None else None
    if (
        definition is None
        or not definition.enabled
        or definition.name in builtin_names
    ):
        return None
    normalized_objective = objective.strip()
    if not normalized_objective:
        return None
    return {
        "id": f"selected-expert-{uuid4().hex}",
        "name": "start_subagent",
        "input": {
            "name": definition.name,
            "objective": normalized_objective,
        },
    }


def _is_selected_expert_start_approval(
    approved_tool_call: dict[str, Any] | None,
    selected_expert_call: dict[str, Any],
) -> bool:
    """Keep a matching approved start call's identity and hook replay."""
    if not isinstance(approved_tool_call, dict):
        return False
    if approved_tool_call.get("name") != "start_subagent":
        return False
    approved_input = approved_tool_call.get("input")
    selected_input = selected_expert_call.get("input")
    return (
        isinstance(approved_input, dict)
        and isinstance(selected_input, dict)
        and approved_input.get("name") == selected_input.get("name")
    )


def _initialize_selected_expert_dependency_view(
    *,
    workspace_dir: Path,
    tenant_id: str | None,
    agent_id: str,
    selected_expert_id: str,
    chat_id: str,
) -> Path | None:
    """Bind a received expert's frozen dependencies to this Chat once."""
    from ..subagents import (
        AgentOwnedDefinitionRepository,
        initialize_community_expert_dependency_view,
    )

    try:
        package = AgentOwnedDefinitionRepository(
            workspace_dir / "agents",
            owner_scope=f"{tenant_id or 'default'}/{agent_id}",
        ).get(selected_expert_id)
    except ValueError:
        return None
    definition = package.definition if package is not None else None
    if definition is None or not definition.enabled:
        return None
    return initialize_community_expert_dependency_view(
        workspace_dir=workspace_dir,
        chat_id=chat_id,
        definition=definition,
    )


def _request_context_references(request: AgentRequest) -> list[object]:
    """Read the Console's typed, one-turn context references."""
    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "context_references", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("context_references")
    return list(value) if isinstance(value, list) else []


def _request_scenario_preset_snapshot(
    request: AgentRequest,
) -> dict[str, Any] | None:
    """Read only the server-populated scenario snapshot from request metadata."""
    channel_meta = getattr(request, "channel_meta", None) or {}
    if channel_meta.get("scenario_preset_snapshot_source") != "chat_meta":
        return None
    value = channel_meta.get("scenario_preset_snapshot")
    return dict(value) if isinstance(value, dict) else None


def _without_request_scenario_snapshot(
    channel_meta: dict[str, Any],
) -> dict[str, Any]:
    """Discard client-supplied scenario state before restoring Chat state."""
    return {
        key: value
        for key, value in channel_meta.items()
        if key not in _SCENARIO_SNAPSHOT_REQUEST_META_KEYS
    }


def _agent_config_with_scenario_mcp(
    agent_config: Any,
    snapshot: dict[str, Any] | None,
    *,
    workspace_dir: Path,
    chat_id: str,
) -> Any:
    """Overlay trusted temporary scenario MCPs without persisting config."""
    if snapshot is None:
        return agent_config
    from ..scenario_preset.resources import (
        resolve_temporary_mcp_config,
        sanitize_mcp_config,
    )
    from ..scenario_preset.runtime import scenario_snapshot_mcp_configs

    entries = scenario_snapshot_mcp_configs(
        snapshot,
        workspace_dir=workspace_dir,
        chat_id=chat_id,
    )
    if not entries:
        return agent_config
    try:
        effective = agent_config.model_copy(deep=True)
        mcp_config = getattr(effective, "mcp", None)
        if mcp_config is None:
            mcp_config = MCPConfig(clients={})
            effective.mcp = mcp_config
        clients = getattr(mcp_config, "clients", None)
        if not isinstance(clients, dict):
            return agent_config
        for entry in entries:
            key = entry["client_key"]
            if key in clients:
                existing_source = str(
                    getattr(clients[key], "source", "") or "",
                )
                if existing_source == f"marketplace:{entry['resource_id']}":
                    continue
                logger.warning(
                    "Temporary scenario MCP key collision; skipping resource_id=%s",
                    entry["resource_id"],
                )
                continue
            config = resolve_temporary_mcp_config(
                sanitize_mcp_config(entry["config"]),
            )
            if config is None:
                logger.warning(
                    "Temporary scenario MCP credentials unavailable; skipping",
                )
                continue
            config.update(
                {
                    "name": f"scenario:{entry['resource_id']}",
                    "enabled": True,
                    "source": f"marketplace:{entry['resource_id']}",
                    "market_client_key": key,
                },
            )
            clients[key] = MCPClientConfig.model_validate(config)
        return effective
    except (AttributeError, TypeError, ValueError):
        logger.warning("Invalid temporary scenario MCP snapshot; skipping")
        return agent_config


def _scenario_snapshot_frozen_mcp_tools(
    snapshot: dict[str, Any] | None,
    agent_config: Any,
) -> dict[str, list[dict[str, Any]]]:
    clients = getattr(getattr(agent_config, "mcp", None), "clients", None)
    if not isinstance(clients, dict):
        return {}
    frozen: dict[str, list[dict[str, Any]]] = {}
    for resource in (snapshot or {}).get("resources", []):
        if (
            not isinstance(resource, dict)
            or resource.get("type") != "mcp_service"
            or resource.get("status") not in {"temporary", "persistent"}
            or not isinstance(resource.get("tools"), list)
        ):
            continue
        key = str(resource.get("mcp_client_key") or resource.get("id") or "")
        source = f"marketplace:{resource.get('id') or ''}"
        if not key or not any(
            str(getattr(client, "source", "") or "") == source
            and str(getattr(client, "market_client_key", "") or key) == key
            for client in clients.values()
        ):
            continue
        frozen[key] = resource["tools"]
    return frozen


def _request_file_url_network(request: AgentRequest) -> str:
    """从请求属性和 channel_meta 中读取静态文件访问网络。"""
    from ...config.context import normalize_file_url_network

    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "file_url_network", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("file_url_network")
    return normalize_file_url_network(value)


def _normalize_system_prompt_injections(value: Any) -> list[str]:
    from ..source_system_config.registry import (
        normalize_system_prompt_injections,
    )

    try:
        return normalize_system_prompt_injections(value)
    except ValueError:
        logger.warning(
            "Ignored invalid system_prompt_injections payload",
            exc_info=True,
        )
        return []


def _merge_system_prompt_injections(*sources: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for item in _normalize_system_prompt_injections(source):
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def _with_system_prompt_injections(
    env_context: str,
    injections: list[str],
) -> str:
    if not injections:
        return env_context
    body = "\n\n".join(injections)
    return f"{env_context}\n\n[System prompt injections]\n{body}"


def _chat_name_from_messages(msgs: list[Any]) -> str:
    """从首条消息派生会话名，保持原有文本和媒体消息规则。"""
    if not msgs:
        return "New Chat"

    content = msgs[0].get_text_content()
    if content:
        return content[:10]
    return "Media Message"


def _should_generate_session_title(
    chat: Any,
    *,
    fallback_name: str,
) -> bool:
    """判断当前 chat 是否仍可由自动标题覆盖。

    只允许覆盖尚未生成过标题且仍保持默认/历史自动名称的会话，避免后续
    轮次或人工改名被后台任务再次覆盖。
    """
    if chat is None:
        return False

    meta = getattr(chat, "meta", None) or {}
    if meta.get("session_kind") == _TASK_SESSION_KIND:
        return False

    if meta.get(_SESSION_TITLE_GENERATED_META_KEY):
        return False

    current_name = (getattr(chat, "name", "") or "").strip()
    auto_names = {"New Chat", "新会话", fallback_name}
    return current_name in auto_names


def _clear_session_title_meta(request: AgentRequest) -> None:
    """清理通道标题元数据，避免跳过生成后仍向前端推送标题更新。"""
    channel_meta = getattr(request, "channel_meta", None)
    if not isinstance(channel_meta, dict):
        return
    if "session_title" not in channel_meta:
        return
    channel_meta.pop("session_title", None)
    request.channel_meta = channel_meta


def _request_source_id(request: AgentRequest) -> str:
    """从请求属性和 channel_meta 中解析追踪或 hook 的来源标识。"""
    channel_meta = getattr(request, "channel_meta", None) or {}
    return getattr(request, "source_id", None) or channel_meta.get(
        "source_id",
        "default",
    )


def _request_approval_source_channel(
    request: Any | None,
) -> str | None:
    channel_meta = getattr(request, "channel_meta", None) or {}
    if not isinstance(channel_meta, dict):
        return None
    source_channel = channel_meta.get("approval_source_channel")
    if isinstance(source_channel, str) and source_channel.strip():
        return source_channel.strip()
    return None


def _external_approval_submission(record: Any) -> dict[str, Any] | None:
    extra = getattr(record, "extra", None)
    if not isinstance(extra, dict):
        return None
    submission = extra.get("external_submission")
    if not isinstance(submission, dict):
        return None
    return submission


def _request_matches_external_approval_submission(
    request: Any | None,
    submission: dict[str, Any],
) -> bool:
    submitted_channel = submission.get("source_channel")
    if not isinstance(submitted_channel, str) or not submitted_channel.strip():
        return False
    request_channel = _request_approval_source_channel(request)
    return request_channel == submitted_channel.strip()


def _request_user_name(request: AgentRequest) -> str | None:
    """按兼容顺序读取通道注入的用户名称。"""
    channel_meta = getattr(request, "channel_meta", None) or {}
    return (
        getattr(request, "user_name", None)
        or getattr(getattr(request, "state", None), "user_name", None)
        or channel_meta.get("user_name")
    )


def _request_bbk_id(request: AgentRequest) -> str | None:
    """按兼容顺序读取通道注入的 BBK 标识。"""
    channel_meta = getattr(request, "channel_meta", None) or {}
    return (
        getattr(request, "bbk_id", None)
        or getattr(getattr(request, "state", None), "bbk_id", None)
        or channel_meta.get("bbk_id")
    )


def _request_b3_trace_id(request: AgentRequest) -> str | None:
    channel_meta = getattr(request, "channel_meta", None) or {}
    trace_id = getattr(request, "b3_trace_id", None) or channel_meta.get(
        "b3_trace_id",
    )
    if not isinstance(trace_id, str):
        return None
    trace_id = trace_id.strip()
    return trace_id or None


def _request_passthrough_headers(request: AgentRequest) -> dict[str, str]:
    channel_meta = getattr(request, "channel_meta", None) or {}
    headers = getattr(request, "passthrough_headers", None)
    if headers is None and isinstance(channel_meta, dict):
        headers = channel_meta.get("passthrough_headers")
    if not isinstance(headers, dict):
        return {}

    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if value is None:
            continue
        header_name = str(name).strip()
        header_value = str(value).strip()
        if header_name and header_value:
            normalized[header_name] = header_value
    return normalized


def _session_name_from_messages(msgs: list[Any]) -> str | None:
    """从第一条消息提取 trace 中展示的短会话名。"""
    if not msgs:
        return None

    content = msgs[0].get_text_content()
    if not content:
        return None
    return content[:10]


def _has_automatic_follow_up_budget(outcome: _QueryTurnOutcome) -> bool:
    """判断本请求级自动续跑总预算是否仍可消耗。"""
    return outcome.automatic_follow_up_turns < (
        outcome.max_automatic_follow_up_turns
    )


def _should_stop_follow_up(outcome: _QueryTurnOutcome) -> bool:
    """判断 Stop 阻断后是否允许再自动续跑一次。"""
    return bool(
        outcome.stop_follow_up_turns < outcome.max_stop_turns
        and _has_automatic_follow_up_budget(outcome),
    )


def _build_cron_append_state(
    existing_state: dict[str, Any],
    current_agent_state: dict[str, Any],
    hook_overlay: HookSessionOverlay | None,
    execution_key: str | None = None,
) -> tuple[dict[str, Any], list[Any], list[Any], int, bool]:
    """构建只追加本次 request memory delta 的 cron session state。"""
    existing_memory = existing_state.get("agent", {}).get("memory", {}) or {}
    existing_content = list(existing_memory.get("content", []) or [])
    stripped_count = _strip_internal_follow_up_messages_from_state(
        current_agent_state,
    )
    current_memory = current_agent_state.get("memory", {}) or {}
    current_content = list(current_memory.get("content", []) or [])
    execution_key = (
        str(execution_key).strip() if isinstance(execution_key, str) else ""
    )

    task_runs = list(existing_state.get(TASK_RUNS_STATE_KEY, []) or [])
    if execution_key and any(
        isinstance(run, dict) and run.get("execution_key") == execution_key
        for run in task_runs
    ):
        return (
            existing_state,
            existing_content,
            current_content,
            stripped_count,
            False,
        )

    merged_state = dict(existing_state)
    existing_agent = existing_state.get("agent")
    if isinstance(existing_agent, dict) and existing_memory:
        merged_agent = dict(existing_agent)
        merged_memory = dict(existing_memory)
        merged_memory["content"] = existing_content + current_content
        merged_agent["memory"] = merged_memory
        merged_state["agent"] = merged_agent
    else:
        merged_agent = dict(current_agent_state)
        merged_memory = dict(current_memory)
        merged_memory["content"] = existing_content + current_content
        merged_agent["memory"] = merged_memory
        merged_state["agent"] = merged_agent
    if hook_overlay is not None:
        merged_state["hook_overlay"] = hook_overlay.model_dump(
            mode="json",
            by_alias=True,
        )
    else:
        merged_state.pop("hook_overlay", None)

    task_run = _build_task_run_record(
        current_content,
        memory_start=len(existing_content),
        execution_key=execution_key or None,
    )
    if task_run is not None:
        task_runs.append(task_run)
        merged_state[TASK_RUNS_STATE_KEY] = task_runs

    return (
        merged_state,
        existing_content,
        current_content,
        stripped_count,
        True,
    )


@dataclass
class _RetryState:
    """查询级重试过程中需要跨重试轮次保持的状态。"""

    agent: Any = None
    prev_agent: Any = None
    session_state_loaded: bool = False
    prev_session_state_loaded: bool = False
    task_completed: bool = False
    agent_state_snapshot: dict[str, Any] | None = None


@dataclass
class _QueryAttemptState:
    """记录当前 query 尝试的运行时对象与退出原因。"""

    runtime: _QueryRuntime | None = None
    runtime_start: _RuntimeStartResult | None = None
    session_state_loaded: bool = False
    should_return: bool = False
    succeeded: bool = False
    session_title_task_started: bool = False


@dataclass(frozen=True)
class _QueryAttemptInput:
    """封装单次 query 尝试不随执行过程变化的输入。"""

    request: AgentRequest
    msgs: list[Any]
    query: str | None
    preflight: _QueryPreflight
    trace_id: str | None
    session_execution: Any | None = None


class AgentRunner(Runner):
    def __init__(
        self,
        agent_id: str = "default",
        workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
        tenant_id: str | None = None,
        answer_turn_coordinator: Any | None = None,
    ) -> None:
        from ...config.context import resolve_runtime_tenant_id

        super().__init__()
        self.framework_type = "agentscope"
        self.agent_id = agent_id  # Store agent_id for config loading
        self.workspace_dir = (
            workspace_dir  # Store workspace_dir for prompt building
        )
        self.tenant_id = (
            resolve_runtime_tenant_id(tenant_id, None)
            if tenant_id is not None
            else None
        )  # Store tenant_id for config loading
        self._chat_manager = None  # Store chat_manager reference
        self._workspace: Any = None  # Workspace instance for control commands
        self.memory_manager: BaseMemoryManager | None = None
        self._task_tracker = task_tracker  # Task tracker for background tasks
        self._answer_turn_coordinator = answer_turn_coordinator
        self._answer_turn_tasks: dict[TurnIdentity, asyncio.Task[Any]] = {}
        self._answer_turn_runtimes: dict[
            TurnIdentity,
            tuple[_QueryRuntime | None, Any],
        ] = {}
        self._answer_turn_locations: dict[TurnIdentity, tuple[str, str]] = {}
        self._query_background_tasks: set[asyncio.Task[None]] = set()
        self.session: Any | None = None
        self._query_execution = QueryExecution(
            LegacyQueryExecutionAdapter(self),
        )

    def set_answer_turn_coordinator(self, coordinator: Any) -> None:
        """Attach the workspace-owned answer-turn coordinator."""
        self._answer_turn_coordinator = coordinator

    @staticmethod
    def _answer_turn_identity(request: Any) -> TurnIdentity | None:
        channel_meta = getattr(request, "channel_meta", None) or {}
        identity = channel_meta.get("answer_turn_identity")
        return identity if isinstance(identity, TurnIdentity) else None

    async def request_cooperative_stop(self, identity: TurnIdentity) -> None:
        """Ask the active agent to stop without terminating its task."""
        runtime, _ = self._answer_turn_runtimes.get(identity, (None, None))
        if runtime is not None:
            await runtime.agent.interrupt()

    async def hard_cancel(self, identity: TurnIdentity) -> None:
        """Cancel the execution task that still owns *identity*."""
        task = self._answer_turn_tasks.get(identity)
        if task is not None and not task.done():
            task.cancel()

    async def persist_outcome(self, outcome: TurnOutcome) -> None:
        """Persist a terminal answer-turn outcome in the owning session."""
        runtime, session_execution = self._answer_turn_runtimes.get(
            outcome.identity,
            (None, None),
        )
        locations = getattr(self, "_answer_turn_locations", {})
        location = locations.get(outcome.identity)
        if runtime is None and session_execution is None and location is None:
            raise RuntimeError(
                "answer turn persistence context is unavailable",
            )
        from .session_lifecycle import (
            mark_stopped_agent_memory,
            mark_terminal_turn_state,
        )

        terminal_status = (
            "stopped"
            if outcome.status == TurnStatus.CANCELLED
            else outcome.status.value
        )
        if outcome.status == TurnStatus.CANCELLED and runtime is not None:
            mark_stopped_agent_memory(
                runtime.agent,
                outcome.identity.msgid,
            )

        if session_execution is not None and getattr(
            session_execution,
            "is_active",
            True,
        ):
            mark_terminal_turn_state(
                session_execution.state,
                outcome.identity.msgid,
                terminal_status,
            )
            await session_execution.commit_state(session_execution.state)
            return

        session_id = str(
            getattr(runtime, "session_id", "") or (location or ("", ""))[0],
        )
        user_id = str(
            getattr(runtime, "user_id", "") or (location or ("", ""))[1],
        )
        if not session_id or self.session is None:
            raise RuntimeError("answer turn persistence target is unavailable")

        def mark_outcome(state: dict[str, Any]) -> dict[str, Any]:
            mark_terminal_turn_state(
                state,
                outcome.identity.msgid,
                terminal_status,
            )
            return state

        await self.session.mutate_session_state(
            session_id,
            mark_outcome,
            user_id=user_id,
        )

    async def release_outcome(self, identity: TurnIdentity) -> None:
        """Release the execution context after durable settlement succeeds."""
        self._answer_turn_runtimes.pop(identity, None)
        getattr(self, "_answer_turn_locations", {}).pop(identity, None)

    async def _report_answer_turn_outcome(
        self,
        identity: TurnIdentity | None,
        outcome: TurnOutcome,
    ) -> None:
        if identity is None or self._answer_turn_coordinator is None:
            return
        await self._answer_turn_coordinator.settle(outcome)

    def set_chat_manager(self, chat_manager):
        """Set chat manager for auto-registration.

        Args:
            chat_manager: ChatManager instance
        """
        self._chat_manager = chat_manager

    def set_workspace(self, workspace):
        """Set workspace for control command handlers.

        Args:
            workspace: Workspace instance
        """
        self._workspace = workspace

    _APPROVAL_TIMEOUT_SECONDS = TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS

    async def _resolve_pending_approval(
        self,
        session_id: str,
        query: str | None,
        request: Any | None = None,
    ) -> tuple[Msg | None, bool, dict[str, Any] | None]:
        """Check for a pending tool-guard approval for *session_id*.

        Returns ``(response_msg, was_consumed, approved_tool_call)``:

        - ``(None, False, None)`` — no pending approval, continue normally.
        - ``(Msg, True, None)``   — denied; yield the Msg and stop.
        - ``(None, True, dict)``  — approved with stored tool call.

        Approvals are resolved FIFO per session (oldest pending first).
        """
        if not session_id:
            return None, False, None

        from ..approvals import get_approval_service

        svc = get_approval_service()
        normalized = (query or "").strip().lower()
        request_id = _approval_request_id(normalized) or _denial_request_id(
            normalized,
        )
        pending = await _select_pending_approval(
            svc,
            session_id=session_id,
            request_id=request_id,
        )
        if pending is None:
            return None, False, None

        elapsed = time.time() - pending.created_at
        if elapsed > self._APPROVAL_TIMEOUT_SECONDS:
            await svc.resolve_request(
                pending.request_id,
                ApprovalDecision.TIMEOUT,
            )
            return (
                _build_denial_response_msg(
                    pending,
                    f"⏰ Tool `{pending.tool_name}` approval "
                    f"timed out ({int(elapsed)}s) — denied.\n"
                    f"工具 `{pending.tool_name}` 审批超时"
                    f"（{int(elapsed)}s），已拒绝执行。",
                ),
                True,
                None,
            )

        external_submission = _external_approval_submission(pending)
        if external_submission is not None and (
            not _request_matches_external_approval_submission(
                request,
                external_submission,
            )
        ):
            source_channel = external_submission.get("source_channel")
            if not isinstance(source_channel, str) or not source_channel:
                source_channel = "external"
            return (
                Msg(
                    name="Friday",
                    role="assistant",
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                f"Approval request `{pending.request_id}` "
                                f"has already been submitted from "
                                f"`{source_channel}`. Refresh the session "
                                "to see the latest approval state."
                            ),
                        ),
                    ],
                ),
                True,
                None,
            )

        if _is_approval(normalized):
            resolved = await svc.resolve_request(
                pending.request_id,
                ApprovalDecision.APPROVED,
            )
            record = resolved or pending
            approved_tool_call = _approved_tool_call_from_record(record)
            await self._notify_console_approval_result(
                record,
                ApprovalDecision.APPROVED,
                request,
            )
            return None, True, approved_tool_call

        explicit_deny = _is_denial(normalized)
        denial_decision = (
            ApprovalDecision.DENIED
            if explicit_deny
            else ApprovalDecision.DENIED
        )
        resolved = await svc.resolve_request(
            pending.request_id,
            denial_decision,
        )
        await self._notify_console_approval_result(
            resolved or pending,
            denial_decision,
            request,
        )
        return (
            _build_denial_response_msg(
                pending,
                f"❌ Tool `{pending.tool_name}` denied.\n"
                f"工具 `{pending.tool_name}` 已拒绝执行。",
            ),
            True,
            None,
        )

    async def _notify_console_approval_result(
        self,
        pending: Any,
        decision: ApprovalDecision,
        request: Any | None,
    ) -> None:
        from ..approvals.external import (
            CONSOLE_CHANNEL,
            ExternalApprovalDecision,
            notify_cron_approval_result,
        )

        source_channel = _request_approval_source_channel(request)
        if source_channel and source_channel != CONSOLE_CHANNEL:
            return

        workspace = getattr(self, "_workspace", None)
        if workspace is None:
            return

        external_decision = (
            ExternalApprovalDecision.APPROVE
            if decision == ApprovalDecision.APPROVED
            else ExternalApprovalDecision.DENY
        )
        try:
            await notify_cron_approval_result(
                workspace,
                pending,
                decision=external_decision,
                source_channel=source_channel or CONSOLE_CHANNEL,
            )
        except Exception:
            logger.exception(
                "zhaohu console approval result notification failed: "
                "request_id=%s",
                getattr(pending, "request_id", None),
            )

    async def _prepare_query_preflight(
        self,
        *,
        session_id: str,
        user_id: str,
        query: str | None,
        request: AgentRequest,
        session_execution: Any = None,
    ) -> _QueryPreflight:
        """处理审批与用户 prompt hook，返回主流程需要的前置状态。"""
        return await query_preflight.prepare_query_preflight(
            self,
            session_id=session_id,
            user_id=user_id,
            query=query,
            request=request,
            session_execution=session_execution,
        )

    def _load_query_preflight_config(self) -> tuple[Any, HookConfig]:
        """Load the configuration pair used by query preflight."""
        return (
            load_agent_config(self.agent_id, tenant_id=self.tenant_id),
            _load_tenant_hook_config(self.tenant_id),
        )

    async def _load_query_preflight_overlay(
        self,
        *,
        session_id: str,
        user_id: str,
        session_execution: Any = None,
    ) -> HookSessionOverlay:
        """Load the request's persisted hook overlay."""
        return await _load_session_hook_overlay(
            getattr(self, "session", None),
            session_id=session_id,
            user_id=user_id,
            session_execution=session_execution,
        )

    @staticmethod
    def _query_preflight_hooks_enabled(
        tenant_hooks: HookConfig,
        agent_config: Any,
        overlay: HookSessionOverlay,
    ) -> bool:
        """Preserve preflight hook enablement rules for the collaborator."""
        return _hook_config_enabled(tenant_hooks, agent_config, overlay)

    async def _emit_query_user_prompt_submit_hook(
        self,
        *,
        request: AgentRequest,
        tenant_hooks: HookConfig,
        agent_config: Any,
        overlay: HookSessionOverlay,
        prompt: str,
        session_execution: Any = None,
    ) -> MergedHookResult:
        """Emit the user-prompt hook through the runner's shared helper."""
        hook_args = {
            "request": request,
            "runner": self,
            "tenant_hooks": tenant_hooks,
            "agent_config": agent_config,
            "overlay": overlay,
            "prompt": prompt,
        }
        if session_execution is not None:
            hook_args["session_execution"] = session_execution
        return await _emit_runner_hook(
            HookEventName.USER_PROMPT_SUBMIT,
            **hook_args,
        )

    @staticmethod
    def _query_preflight_hook_block_message(result: MergedHookResult) -> Msg:
        """Render a preflight hook rejection with existing runner semantics."""
        return _hook_block_message(result)

    @staticmethod
    def _query_preflight_additional_context(
        result: MergedHookResult,
    ) -> str:
        """Format prompt-hook context with existing runner semantics."""
        return _format_hook_additional_context(result)

    async def _start_query_trace(
        self,
        request: AgentRequest,
        msgs: list[Any],
    ) -> str | None:
        """启动 query 追踪；追踪不可用时只记录日志并继续主流程。

        默认情况下，query 请求总是创建新的 trace。
        只有显式声明要续接外部 trace 时，才会使用 attach_existing 模式。
        """
        if not has_trace_manager():
            return None

        try:
            trace_mgr = get_trace_manager()
            if not trace_mgr.enabled:
                return None
            existing_trace_id = getattr(request, "trace_id", None)
            attach_existing = self._should_attach_existing_trace(request)
            b3_trace_id = _request_b3_trace_id(request)
            resolved_identity = await resolve_user_identity(
                tenant_id=getattr(request, "user_id", None),
                source_id=_request_source_id(request),
                user_name=_request_user_name(request),
                bbk_id=_request_bbk_id(request),
                allow_remote_lookup=False,
            )
            trace_id = await trace_mgr.start_trace(
                user_id=getattr(request, "user_id", "") or "",
                session_id=getattr(request, "session_id", "") or "",
                channel=getattr(request, "channel", DEFAULT_CHANNEL),
                source_id=_request_source_id(request),
                user_message=_get_last_user_text(msgs),
                user_name=resolved_identity.user_name,
                bbk_id=resolved_identity.bbk_id,
                session_name=_session_name_from_messages(msgs),
                trace_id=(
                    existing_trace_id if attach_existing else b3_trace_id
                ),
                attach_existing=attach_existing,
                b3_trace_id=b3_trace_id,
            )
            if trace_id:
                # 通道层负责把事件发给前端，这里写回 request 让 SSE 能透传 trace_id。
                setattr(request, "trace_id", trace_id)

            return trace_id
        except Exception as e:
            logger.warning("Failed to start trace: %s", e)
            return None

    @staticmethod
    def _should_attach_existing_trace(request: AgentRequest) -> bool:
        """判断当前请求是否显式要求续接已有 trace。"""
        trace_id = getattr(request, "trace_id", None)
        if not trace_id:
            return False

        if bool(getattr(request, "trace_attach_existing", False)):
            return True

        channel_meta = getattr(request, "channel_meta", None) or {}
        return bool(channel_meta.get("trace_attach_existing"))

    async def _generate_session_title_before_stream(
        self,
        *,
        request: AgentRequest,
        chat: Any,
        msgs: list[Any],
        trace_id: str | None,
    ) -> None:
        """在 Agent 主回答前生成标题，确保前端先收到标题刷新事件。"""
        if not trace_id:
            return

        chat_meta = getattr(chat, "meta", None) or {}
        if chat_meta.get("session_kind") == _TASK_SESSION_KIND:
            _clear_session_title_meta(request)
            return

        channel_meta = getattr(request, "channel_meta", None) or {}
        existing_title = channel_meta.get("session_title")
        if existing_title:
            await self._persist_session_title(
                request=request,
                title=str(existing_title),
                trace_id=trace_id,
                chat_id=getattr(chat, "id", None),
            )
            return

        user_question = _get_last_user_text(msgs)
        if not user_question or not user_question.strip():
            return

        fallback_name = _chat_name_from_messages(msgs)
        if not _should_generate_session_title(
            chat,
            fallback_name=fallback_name,
        ):
            return

        await self._generate_and_update_title(
            request=request,
            user_question=user_question,
            trace_id=trace_id,
            chat_id=getattr(chat, "id", None),
        )

    async def _persist_session_title(
        self,
        *,
        request: AgentRequest,
        title: str,
        trace_id: str,
        chat_id: str | None = None,
    ) -> None:
        """把已确定的标题写回 chat、trace 和 SSE 元数据。"""
        channel_meta = getattr(request, "channel_meta", None) or {}
        resolved_chat_id = chat_id or channel_meta.get("chat_id")
        if resolved_chat_id:
            channel_meta["chat_id"] = resolved_chat_id

        persisted = False
        if not resolved_chat_id:
            logger.warning("跳过会话标题刷新：缺少 chat_id")
        elif self._chat_manager is None:
            logger.warning(
                "跳过会话标题刷新：ChatManager 不可用 chat_id=%s",
                resolved_chat_id,
            )
        else:
            try:
                persisted = await self._chat_manager.update_chat_name(
                    resolved_chat_id,
                    title,
                    meta={
                        _SESSION_TITLE_GENERATED_META_KEY: True,
                    },
                )
            except Exception:
                logger.warning(
                    "更新 chats.json 标题失败 chat_id=%s",
                    resolved_chat_id,
                    exc_info=True,
                )
            if not persisted:
                logger.warning(
                    "更新 chats.json 标题未命中 chat_id=%s",
                    resolved_chat_id,
                )

        if not persisted:
            channel_meta.pop("session_title", None)
            request.channel_meta = channel_meta
            return

        if has_trace_manager():
            try:
                trace_mgr = get_trace_manager()
                await trace_mgr.update_session_name(trace_id, title)
            except Exception:
                logger.warning(
                    "更新 tracing 会话标题失败 trace_id=%s",
                    trace_id,
                    exc_info=True,
                )

        channel_meta["session_title"] = title
        request.channel_meta = channel_meta

    async def _generate_and_update_title(
        self,
        request: AgentRequest,
        user_question: str | None,
        trace_id: str,
        chat_id: str | None = None,
    ) -> None:
        """生成会话标题并更新存储。

        调用外部标题 API，成功后更新 chats.json、MySQL 和 channel_meta；
        失败只记日志，不修改任何数据。
        """
        if not user_question or not user_question.strip():
            return

        try:
            from ..title_generator import generate_title

            title = await generate_title(user_question)
            if not title:
                return

            await self._persist_session_title(
                request=request,
                title=title,
                trace_id=trace_id,
                chat_id=chat_id,
            )

            logger.info(
                "会话标题已更新: trace_id=%s title=%s",
                trace_id,
                title,
            )
        except Exception:
            logger.warning(
                "异步标题生成失败 trace_id=%s",
                trace_id,
                exc_info=True,
            )

    def _schedule_session_title_task(
        self,
        *,
        request: AgentRequest,
        chat: Any,
        msgs: list[Any],
        trace_id: str | None,
    ) -> None:
        """在首个模型事件后异步补写会话标题。"""
        task = asyncio.create_task(
            self._generate_session_title_before_stream(
                request=request,
                chat=chat,
                msgs=msgs,
                trace_id=trace_id,
            ),
        )
        self._query_background_tasks.add(task)
        setattr(request, "_session_title_task", task)
        task.add_done_callback(self._query_background_tasks.discard)
        task.add_done_callback(_consume_background_task_exception)

    @staticmethod
    def _attach_trace_id_to_event(event: Any, trace_id: str | None) -> Any:
        """把当前轮次 trace_id 附加到消息元数据，便于前端关联反馈。"""
        if not trace_id or not isinstance(event, Message):
            return event

        metadata = event.metadata
        if isinstance(metadata, dict):
            if metadata.get("trace_id"):
                return event
            event.metadata = {**metadata, "trace_id": trace_id}
        else:
            event.metadata = {"trace_id": trace_id}
        return event

    @staticmethod
    def _attach_trace_id_to_msg(msg: Any, trace_id: str | None) -> Any:
        """在适配 Runtime Message 前，把 trace_id 写入 AgentScope 消息。"""
        if not trace_id or not isinstance(msg, Msg):
            return msg

        metadata = getattr(msg, "metadata", None)
        if isinstance(metadata, dict):
            if metadata.get("trace_id"):
                return msg
            msg.metadata = {**metadata, "trace_id": trace_id}
        else:
            msg.metadata = {"trace_id": trace_id}
        return msg

    async def _get_or_create_chat(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        name: str,
        request: AgentRequest,
        turn_id: str,
    ) -> Any:
        """按原有规则注册或复用 chat，并把 chat_id 写回请求元数据。"""
        logger.debug(
            f"DEBUG chat_manager status: "
            f"_chat_manager={self._chat_manager}, "
            f"is_none={self._chat_manager is None}, "
            f"agent_id={self.agent_id}",
        )
        if self._chat_manager is None:
            logger.warning(
                f"ChatManager is None! Cannot auto-register chat for "
                f"session_id={session_id}",
            )
            return None

        channel_meta = _without_request_scenario_snapshot(
            getattr(request, "channel_meta", None) or {},
        )
        chat = None
        requested_chat_id = channel_meta.get("chat_id")
        if isinstance(requested_chat_id, str) and requested_chat_id:
            candidate = await self._chat_manager.get_chat(requested_chat_id)
            if (
                candidate is not None
                and candidate.session_id == session_id
                and candidate.user_id == user_id
                and candidate.channel == channel
            ):
                chat = candidate
                merged_meta = {
                    **(candidate.meta or {}),
                    "agent_id": self.agent_id,
                }
                if merged_meta != (candidate.meta or {}):
                    chat.meta = merged_meta
                    chat.updated_at = datetime.now(timezone.utc)
                    await self._chat_manager.update_chat(chat)
        if chat is None:
            logger.debug(
                f"Runner: Calling get_or_create_chat for "
                f"session_id={session_id}, user_id={user_id}, "
                f"channel={channel}, name={name}",
            )
            chat = await self._chat_manager.get_or_create_chat(
                session_id,
                user_id,
                channel,
                name=name,
                meta={"agent_id": self.agent_id},
            )
        logger.debug(f"Runner: Got chat: {chat.id}")
        scheduled_request = (
            getattr(request, "execution_origin", None) == "scheduled"
        )
        plan_mode_enabled = (
            False
            if scheduled_request
            else _resolve_plan_mode_enabled(channel_meta, chat)
        )
        requested_plan_mode = (
            None
            if scheduled_request
            else _requested_plan_mode_update(channel_meta)
        )
        if requested_plan_mode is not None:
            chat.meta = {
                **(getattr(chat, "meta", None) or {}),
                _PLAN_MODE_META_KEY: requested_plan_mode,
            }
            await self._chat_manager.update_chat(chat)
        request.channel_meta = {
            **channel_meta,
            "chat_id": chat.id,
            "turn_id": turn_id,
            _PLAN_MODE_META_KEY: plan_mode_enabled,
        }
        from ..scenario_preset.runtime import get_scenario_snapshot

        scenario_snapshot = get_scenario_snapshot(getattr(chat, "meta", None))
        if scenario_snapshot is not None:
            request.channel_meta["scenario_preset_snapshot"] = (
                scenario_snapshot
            )
            request.channel_meta["scenario_preset_snapshot_source"] = (
                "chat_meta"
            )
        return chat

    async def _emit_session_start_hook(
        self,
        *,
        request: AgentRequest,
        tenant_hooks: HookConfig,
        agent_config: Any,
        hook_overlay: HookSessionOverlay,
        skip_history: bool,
        env_context: str,
        session_execution: Any = None,
    ) -> tuple[str, Msg | None]:
        """执行 SESSION_START hook，并返回可能追加的上下文或阻断消息。"""
        if not _hook_config_enabled(tenant_hooks, agent_config, hook_overlay):
            return env_context, None

        hook_args = {
            "request": request,
            "runner": self,
            "tenant_hooks": tenant_hooks,
            "agent_config": agent_config,
            "overlay": hook_overlay,
            "source": "resume" if not skip_history else "startup",
            "model": _resolve_active_model_label(self.tenant_id),
        }
        if session_execution is not None:
            hook_args["session_execution"] = session_execution
        session_start_result = await _emit_runner_hook(
            HookEventName.SESSION_START,
            **hook_args,
        )
        if session_start_result.decision in {
            HookDecision.BLOCK,
            HookDecision.DENY,
            HookDecision.STOP,
        }:
            return env_context, _hook_block_message(session_start_result)

        session_start_context = _format_hook_additional_context(
            session_start_result,
        )
        return _with_hook_context(env_context, session_start_context), None

    def _create_agent_for_query(
        self,
        *,
        agent_config: Any,
        env_context: str,
        mcp_clients: list[Any],
        request: AgentRequest,
        session_id: str,
        user_id: str,
        channel: str,
        chat: Any,
        turn_id: str,
        hook_overlay: HookSessionOverlay,
        auth_token: str | None,
        approved_tool_call: dict[str, Any] | None,
        current_user_text: str = "",
        workspace_skill_snapshot: Any | None = None,
    ) -> SWEAgent:
        """创建 SWEAgent，并注入本轮请求上下文。"""
        request_enable_subagents = getattr(request, "enable_subagents", False)
        if isinstance(request_enable_subagents, str):
            request_enable_subagents = (
                request_enable_subagents.strip().lower()
                in {
                    "true",
                    "1",
                    "yes",
                }
            )
        request_context = {
            "session_id": session_id,
            "user_id": user_id,
            "channel": channel,
            "chat_id": chat.id if chat is not None else "",
            "turn_id": turn_id,
            "msgid": (getattr(request, "channel_meta", None) or {}).get(
                "msgid",
            ),
            "agent_id": self.agent_id,
            "tenant_id": self.tenant_id or "",
            "agent_role": "main",
            "enable_subagents": bool(request_enable_subagents),
            "source_id": _request_source_id(request),
            "user_name": _request_user_name(request),
            "bbk_id": _request_bbk_id(request),
            "trace_id": getattr(request, "trace_id", None),
            "execution_origin": getattr(request, "execution_origin", None),
            "_task_tracker": self._task_tracker,
            "cron_execution_key": getattr(
                request,
                "cron_execution_key",
                None,
            ),
            "current_user_text": current_user_text,
            "channel_manager": getattr(
                getattr(self, "_workspace", None),
                "channel_manager",
                None,
            ),
            "transcript_path": (
                self.session._get_save_path(session_id, user_id)
                if hasattr(self.session, "_get_save_path")
                else ""
            ),
            "hook_overlay": hook_overlay.model_dump(
                mode="json",
                by_alias=True,
            ),
            "_hook_overlay_model": hook_overlay,
        }
        channel_meta = getattr(request, "channel_meta", None) or {}
        goal_id = channel_meta.get("goal_id")
        goal_mode_enabled = bool(channel_meta.get("goal_mode_enabled", False))
        goal_request = bool(goal_id) or goal_mode_enabled
        if isinstance(goal_id, str) and goal_id:
            request_context["goal_id"] = goal_id
        request_context["goal_mode_enabled"] = goal_mode_enabled
        plan_mode_enabled = (
            False
            if (
                goal_request
                or request_context.get("execution_origin") == "scheduled"
            )
            else bool(channel_meta.get(_PLAN_MODE_META_KEY, False))
        )
        request_context[_PLAN_MODE_META_KEY] = plan_mode_enabled
        request_context[_PLAN_REQUEST_MODE_KEY] = (
            "plan" if plan_mode_enabled else "normal"
        )
        plan_response = channel_meta.get(_PLAN_INTERACTION_RESPONSE_KEY)
        if isinstance(plan_response, dict):
            request_context[_PLAN_INTERACTION_RESPONSE_KEY] = plan_response
        accepted_plan = channel_meta.get("accepted_plan")
        if (
            isinstance(accepted_plan, dict)
            and channel_meta.get(_ACCEPTED_PLAN_SOURCE_META_KEY)
            == _ACCEPTED_PLAN_SERVER_SOURCE
        ):
            request_context["accepted_plan"] = accepted_plan
            request_context[_ACCEPTED_PLAN_SOURCE_META_KEY] = (
                _ACCEPTED_PLAN_SERVER_SOURCE
            )
        selected_expert_id = (
            None if goal_request else _request_selected_expert_id(request)
        )
        if plan_mode_enabled:
            selected_expert_id = None
        if selected_expert_id is not None:
            request_context["selected_expert_id"] = selected_expert_id
            try:
                dependency_view_root = (
                    _initialize_selected_expert_dependency_view(
                        workspace_dir=Path(self.workspace_dir or WORKING_DIR),
                        tenant_id=self.tenant_id,
                        agent_id=self.agent_id,
                        selected_expert_id=selected_expert_id,
                        chat_id=chat.id if chat is not None else "",
                    )
                )
            except OSError as exc:
                dependency_view_root = None
                request_context["selected_expert_execution_error"] = str(exc)
            if dependency_view_root is not None:
                request_context["_expert_dependency_view_root"] = str(
                    dependency_view_root,
                )
            selected_expert_call = _selected_expert_start_tool_call(
                workspace_dir=Path(self.workspace_dir or WORKING_DIR),
                tenant_id=self.tenant_id,
                agent_id=self.agent_id,
                selected_expert_id=selected_expert_id,
                objective=current_user_text,
            )
            if selected_expert_call is not None:
                request_context["selected_expert_execution"] = True
                approved_selected_start = (
                    approved_tool_call
                    if _is_selected_expert_start_approval(
                        approved_tool_call,
                        selected_expert_call,
                    )
                    else None
                )
                request_context["forced_tool_call_json"] = json.dumps(
                    approved_selected_start or selected_expert_call,
                    ensure_ascii=False,
                )
            elif "selected_expert_execution_error" not in request_context:
                request_context["selected_expert_execution_error"] = (
                    "The selected expert is unavailable or disabled. "
                    "Choose an enabled expert and try again."
                )
        if auth_token:
            request_context["auth_token"] = auth_token
        if (
            approved_tool_call
            and "forced_tool_call_json" not in request_context
        ):
            request_context["forced_tool_call_json"] = json.dumps(
                approved_tool_call,
                ensure_ascii=False,
            )
        from ..source_tools.service import get_source_tool_service

        source_tool_versions = ()
        source_tool_service = get_source_tool_service()
        if source_tool_service is not None and request_context["source_id"]:
            try:
                source_tool_versions = source_tool_service.get_active_catalog(
                    request_context["source_id"],
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Source tool catalogue unavailable; source tools fail closed",
                )
        return SWEAgent(
            agent_config=agent_config,
            env_context=env_context,
            mcp_clients=mcp_clients,
            memory_manager=self.memory_manager,
            request_context=request_context,
            workspace_dir=self.workspace_dir,
            workspace_skill_snapshot=workspace_skill_snapshot,
            task_tracker=self._task_tracker,
            source_tool_versions=source_tool_versions,
        )

    def _create_goal_finalization_agent(
        self,
        *,
        runtime: _QueryRuntime,
        goal: Any,
    ) -> SWEAgent:
        """Create an isolated, tool-free Agent for one Goal finalization."""
        source_context = getattr(runtime.agent, "_request_context", {}) or {}
        request_context: dict[str, str] = {
            key: str(source_context[key])
            for key in (
                "session_id",
                "user_id",
                "channel",
                "chat_id",
                "turn_id",
                "agent_id",
                "tenant_id",
                "source_id",
                "user_name",
                "bbk_id",
                "trace_id",
            )
            if source_context.get(key) is not None
        }
        request_context["agent_role"] = "main"
        request_context["goal_finalization"] = True
        resolved_slot = (
            getattr(runtime.agent, "_resolved_model_slot", {}) or {}
        )
        model_slot_override = None
        model_provider_override = None
        provider_id = str(resolved_slot.get("provider_id") or "")
        model_name = str(resolved_slot.get("model") or "")
        if provider_id and model_name:
            from ...providers.models import ModelSlotConfig
            from ...providers.provider_manager import ProviderManager

            model_slot_override = ModelSlotConfig(
                provider_id=provider_id,
                model=model_name,
            )
            snapshot_provider = getattr(
                runtime.agent,
                "_resolved_model_provider",
                None,
            )
            if getattr(snapshot_provider, "id", None) != provider_id:
                snapshot_provider = None
            model_provider_override = (
                snapshot_provider
                or ProviderManager.get_instance(
                    self.tenant_id,
                ).get_provider(provider_id)
            )
            if model_provider_override is None:
                raise RuntimeError("Goal finalization provider is unavailable")
        return SWEAgent(
            agent_config=runtime.agent_config,
            env_context=None,
            enable_memory_manager=False,
            mcp_clients=[],
            memory_manager=None,
            request_context=request_context,
            workspace_dir=self.workspace_dir,
            task_tracker=None,
            enable_workspace_skills=False,
            model_slot_override=model_slot_override,
            model_provider_override=model_provider_override,
            system_prompt_override=_GOAL_FINALIZATION_SYSTEM_PROMPT,
            source_tool_versions=(),
        )

    def _create_goal_completion_judge_agent(
        self,
        *,
        runtime: _QueryRuntime,
        goal: Any,
        approved_tool_call: dict[str, Any] | None = None,
    ) -> SWEAgent:
        """Create a restricted, frozen-model Agent for Goal completion review."""
        source_context = getattr(runtime.agent, "_request_context", {}) or {}
        request_context: dict[str, str] = {
            key: str(source_context[key])
            for key in (
                "session_id",
                "user_id",
                "channel",
                "chat_id",
                "turn_id",
                "agent_id",
                "tenant_id",
                "source_id",
                "trace_id",
                "goal_id",
            )
            if source_context.get(key) is not None
        }
        request_context["agent_role"] = "completion_judge"
        if approved_tool_call is not None:
            request_context["forced_tool_call_json"] = json.dumps(
                approved_tool_call,
            )
        resolved_slot = (
            getattr(runtime.agent, "_resolved_model_slot", {}) or {}
        )
        frozen_scope = getattr(goal, "scope", None)
        model_slot_override = None
        model_provider_override = None
        provider_id = str(
            getattr(frozen_scope, "effective_model_provider_id", "") or "",
        ) or str(resolved_slot.get("provider_id") or "")
        model_name = str(
            getattr(frozen_scope, "effective_model", "") or "",
        )
        if model_name == "default":
            model_name = ""
        model_name = model_name or str(resolved_slot.get("model") or "")
        if not provider_id or not model_name:
            raise RuntimeError(
                "Goal completion judge frozen model is unavailable",
            )
        from ...providers.models import ModelSlotConfig
        from ...providers.provider_manager import ProviderManager

        model_slot_override = ModelSlotConfig(
            provider_id=provider_id,
            model=model_name,
        )
        snapshot_provider = getattr(
            runtime.agent,
            "_resolved_model_provider",
            None,
        )
        if getattr(snapshot_provider, "id", None) != provider_id:
            snapshot_provider = None
        model_provider_override = (
            snapshot_provider
            or ProviderManager.get_instance(
                self.tenant_id,
            ).get_provider(provider_id)
        )
        if model_provider_override is None:
            raise RuntimeError("Goal completion judge provider is unavailable")
        return SWEAgent(
            agent_config=runtime.agent_config,
            env_context=None,
            enable_memory_manager=False,
            mcp_clients=[],
            memory_manager=None,
            request_context=request_context,
            workspace_dir=self.workspace_dir,
            task_tracker=None,
            enable_workspace_skills=False,
            model_slot_override=model_slot_override,
            model_provider_override=model_provider_override,
            system_prompt_override=_GOAL_COMPLETION_JUDGE_SYSTEM_PROMPT,
            source_tool_versions=(),
        )

    def _create_goal_completion_reviewer(
        self,
        *,
        runtime: _QueryRuntime,
        resolution: Any,
    ):
        """Bind one Main-Agent turn's bounded evidence to a Judge callback."""
        request_context = getattr(runtime.agent, "_request_context", {}) or {}
        tool_observations = request_context.get(
            "_goal_turn_tool_observations",
            (),
        )

        async def reviewer(review_goal: Any):
            return await self._run_goal_completion_review(
                runtime=runtime,
                review_goal=review_goal,
                completion_proposal=resolution.completion_proposal,
                evidence_refs=resolution.evidence_refs,
                tool_observations=tool_observations,
            )

        return reviewer

    async def _run_goal_completion_review(
        self,
        *,
        runtime: _QueryRuntime,
        review_goal: Any,
        completion_proposal: str | None,
        evidence_refs: list[str],
        tool_observations: object,
    ) -> dict[str, Any]:
        """Run a hidden Judge invocation and fail closed on unusable output."""
        from ..goals.review import (
            build_completion_review_input,
            parse_completion_review,
        )
        from ..goals.runtime import CompletionReviewPending

        criterion_ids = {item.criterion_id for item in review_goal.criteria}
        observations = (
            tool_observations if isinstance(tool_observations, list) else []
        )
        has_pending_review_request = any(
            item.verification_request_id for item in review_goal.criteria
        )
        try:
            approval_result = await self._completion_review_approval_result(
                review_goal,
            )
            if approval_result is not None:
                return approval_result
            approved_tool_call = (
                await self._completion_review_approved_tool_call(
                    review_goal,
                )
            )
            if has_pending_review_request and approved_tool_call is None:
                return {
                    criterion_id: (
                        False,
                        "Completion Judge approved tool replay is unavailable",
                    )
                    for criterion_id in criterion_ids
                }
            agent = self._create_goal_completion_judge_agent(
                runtime=runtime,
                goal=review_goal,
                approved_tool_call=approved_tool_call,
            )
            final_content = ""
            async for msg, _ in self._enforce_query_timeout(
                stream_printing_messages(
                    agents=[agent],
                    coroutine_task=agent(
                        [
                            build_completion_review_input(
                                review_goal,
                                completion_proposal=completion_proposal,
                                evidence_refs=evidence_refs,
                                tool_observations=observations,
                            ),
                        ],
                    ),
                ),
                session_id=runtime.session_id,
                agent=agent,
                run_key=(
                    runtime.chat.id if runtime.chat is not None else None
                ),
            ):
                if getattr(msg, "role", None) == "assistant":
                    final_content = str(getattr(msg, "content", "") or "")
            pending = getattr(agent, "_tool_guard_pending_info", None)
            request_id = (
                pending.get("request_id")
                if isinstance(pending, dict)
                else None
            )
            if request_id:
                return {
                    criterion_id: CompletionReviewPending(
                        request_id=str(request_id),
                        reason="Completion Judge tool approval required",
                    )
                    for criterion_id in criterion_ids
                }
            return parse_completion_review(final_content, criterion_ids)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Goal Completion Judge failed goal_id=%s",
                review_goal.goal_id,
            )
            return parse_completion_review("", criterion_ids)

    async def _completion_review_approval_result(
        self,
        review_goal: Any,
    ) -> dict[str, Any] | None:
        """Return pending or denied review results before replaying a Judge."""
        from ..approvals import get_approval_service
        from ..goals.runtime import CompletionReviewPending

        request_ids = {
            item.verification_request_id
            for item in review_goal.criteria
            if item.verification_request_id
        }
        if not request_ids:
            return None
        criterion_ids = {item.criterion_id for item in review_goal.criteria}
        if len(request_ids) != 1:
            return {
                criterion_id: (
                    False,
                    "completion review approval is inconsistent",
                )
                for criterion_id in criterion_ids
            }
        request_id = next(iter(request_ids))
        status = await get_approval_service().get_request_status(request_id)
        decision = str((status or {}).get("status") or "pending")
        if decision == "approved":
            return None
        if decision in {"pending", "submitted"}:
            return {
                criterion_id: CompletionReviewPending(
                    request_id=request_id,
                    reason="Completion Judge tool approval required",
                )
                for criterion_id in criterion_ids
            }
        return {
            criterion_id: (False, f"Completion Judge approval {decision}")
            for criterion_id in criterion_ids
        }

    async def _completion_review_approved_tool_call(
        self,
        review_goal: Any,
    ) -> dict[str, Any] | None:
        """Load the normal Tool Guard replay payload after approval."""
        from ..approvals import get_approval_service

        request_ids = {
            item.verification_request_id
            for item in review_goal.criteria
            if item.verification_request_id
        }
        if not request_ids:
            return None
        request = await get_approval_service().get_request(
            next(iter(request_ids)),
        )
        return _approved_tool_call_from_record(request) if request else None

    async def _stream_goal_finalization_turn(
        self,
        *,
        runtime: _QueryRuntime,
        goal: Any,
        stop_rejection_reason: str | None = None,
    ):
        """Stream a no-budget Goal Finalization Turn or its fixed fallback."""
        state = goal.state.value
        previous_msg: Msg | None = None
        try:
            agent = self._create_goal_finalization_agent(
                runtime=runtime,
                goal=goal,
            )
            async for msg, _ in self._enforce_query_timeout(
                stream_printing_messages(
                    agents=[agent],
                    coroutine_task=agent(
                        [
                            _build_goal_finalization_input(
                                goal,
                                state,
                                goal.state_reason,
                                stop_rejection_reason,
                            ),
                        ],
                    ),
                ),
                session_id=runtime.session_id,
                agent=agent,
                run_key=(
                    runtime.chat.id if runtime.chat is not None else None
                ),
            ):
                if getattr(msg, "role", None) != "assistant":
                    continue
                if not str(getattr(msg, "content", "") or "").strip():
                    continue
                if previous_msg is not None:
                    yield previous_msg, False
                previous_msg = msg
        except Exception:  # noqa: BLE001
            logger.exception(
                "Goal Finalization Turn failed; emitting fallback goal_id=%s",
                getattr(goal, "goal_id", ""),
            )
        finalization_fallback = previous_msg is None
        final_msg = previous_msg or _build_goal_finalization_msg(
            state,
            goal.state_reason,
        )
        if finalization_fallback:
            final_msg.metadata = {
                **(final_msg.metadata or {}),
                "goal_finalization_fallback": True,
            }
        memory = getattr(runtime.agent, "memory", None)
        add_to_memory = getattr(memory, "add", None)
        if callable(add_to_memory):
            await add_to_memory(final_msg)
        yield final_msg, True

    def _attach_session_skill_detector(
        self,
        *,
        runtime: _QueryRuntime,
        request: AgentRequest,
    ) -> None:
        """挂载会话级技能探测器，并同步 hook overlay 的后续变更。"""

        def _get_session_hook_state() -> HookSessionState:
            return HookSessionState.model_validate(
                runtime.hook_overlay.model_dump(mode="json", by_alias=True),
            )

        def _set_session_hook_state(
            next_state: HookSessionState,
        ) -> None:
            runtime.hook_overlay = HookSessionOverlay.model_validate(
                next_state.model_dump(mode="json", by_alias=True),
            )
            dumped = runtime.hook_overlay.model_dump(
                mode="json",
                by_alias=True,
            )
            runtime.agent._request_context["_hook_overlay_model"] = (
                runtime.hook_overlay
            )
            runtime.agent._request_context["hook_overlay"] = dumped

        async def _queue_confirmed_skill_snapshot_update(
            skill_name: str,
        ) -> None:
            if (
                self.session is None
                or not runtime.session_id
                or not hasattr(self.session, "get_session_skill_snapshot")
                or not hasattr(self.session, "save_session_skill_snapshot")
            ):
                return

            skill_dir = resolve_effective_skill_dir(
                Path(self.workspace_dir or WORKING_DIR),
                skill_name,
            )
            if skill_dir is None or not skill_dir.exists():
                return

            runtime.pending_confirmed_skill_snapshots[skill_name] = (
                _build_session_skill_snapshot_entry(
                    skill_name=skill_name,
                    resolved_skill_dir=skill_dir,
                    freshness_token=get_skill_freshness_token(skill_dir),
                    confirmed_at=time.time(),
                )
            )

        source_id_for_hooks = _request_source_id(request)
        workspace_skill_snapshot = getattr(
            runtime.agent,
            "_workspace_skill_snapshot",
            None,
        )
        snapshot_skills = getattr(workspace_skill_snapshot, "skills", {})
        runtime.session_skill_detector = _create_session_skill_detector(
            workspace_dir=Path(self.workspace_dir or WORKING_DIR),
            tenant_id=self.tenant_id,
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            channel=runtime.channel,
            source_id=source_id_for_hooks,
            enabled_skills=(
                runtime.agent.get_runtime_skills()
                if hasattr(runtime.agent, "get_runtime_skills")
                else []
            ),
            skill_runtime_profiles=(
                runtime.agent.get_skill_runtime_profiles()
                if hasattr(runtime.agent, "get_skill_runtime_profiles")
                else {}
            ),
            skill_metadata={
                name: dict(skill.metadata)
                for name, skill in snapshot_skills.items()
            },
            skill_dirs={
                name: skill.directory
                for name, skill in snapshot_skills.items()
            },
            skill_signatures={
                name: skill.content_signature
                for name, skill in snapshot_skills.items()
            },
            get_hook_state=_get_session_hook_state,
            set_hook_state=_set_session_hook_state,
            confirmed_skill_callback=(_queue_confirmed_skill_snapshot_update),
            skill_tool_registry=(
                runtime.agent.get_skill_tool_registry()
                if hasattr(runtime.agent, "get_skill_tool_registry")
                else None
            ),
        )
        if not hasattr(runtime.agent, "_request_context"):
            runtime.agent._request_context = {}
        runtime.agent._request_context["_skill_invocation_detector"] = (
            runtime.session_skill_detector
        )
        trace_id = getattr(request, "trace_id", None)
        if trace_id and has_trace_manager():
            try:
                trace_mgr = get_trace_manager()
                runtime.session_skill_detector.set_tracing_context(
                    trace_mgr,
                    trace_id,
                    runtime.user_id,
                    runtime.session_id,
                    runtime.channel,
                    source_id_for_hooks,
                )
                from ...tracing import get_current_trace

                trace_ctx = get_current_trace()
                if trace_ctx and trace_ctx.trace_id == trace_id:
                    trace_ctx.set_skill_detector(
                        runtime.session_skill_detector,
                        (
                            runtime.agent.get_runtime_skills()
                            if hasattr(runtime.agent, "get_runtime_skills")
                            else (
                                runtime.agent.get_effective_skills()
                                if hasattr(
                                    runtime.agent,
                                    "get_effective_skills",
                                )
                                else []
                            )
                        ),
                    )
            except Exception:
                logger.warning(
                    "Failed to attach tracing context to session skill detector",
                    exc_info=True,
                )

    def _rebind_trace_skill_detector_if_needed(
        self,
        *,
        runtime: _QueryRuntime,
        trace_id: str | None,
    ) -> None:
        """确保 trace context 与会话级 detector 收敛到同一实例。"""
        if trace_id is None or runtime.session_skill_detector is None:
            return

        from ...tracing import get_current_trace

        trace_ctx = get_current_trace()
        if trace_ctx is None or trace_ctx.trace_id != trace_id:
            return
        if trace_ctx.skill_detector is runtime.session_skill_detector:
            return

        previous_detector = trace_ctx.skill_detector
        enabled_skills = (
            runtime.agent.get_runtime_skills()
            if hasattr(runtime.agent, "get_runtime_skills")
            else (
                runtime.agent.get_effective_skills()
                if hasattr(runtime.agent, "get_effective_skills")
                else []
            )
        )
        trace_ctx.set_skill_detector(
            runtime.session_skill_detector,
            enabled_skills,
        )

    async def _refresh_session_skill_freshness(
        self,
        *,
        runtime: _QueryRuntime,
    ) -> _SkillFreshnessRefreshResult:
        return await session_lifecycle.refresh_session_skill_freshness(
            self,
            runtime=runtime,
            refresh_result_type=_SkillFreshnessRefreshResult,
        )

    async def _build_skill_snapshot_to_persist(
        self,
        *,
        runtime: _QueryRuntime,
        refresh_result: _SkillFreshnessRefreshResult,
    ) -> dict[str, dict[str, Any]] | None:
        return await session_lifecycle.build_skill_snapshot_to_persist(
            self,
            runtime=runtime,
            refresh_result=refresh_result,
        )

    async def _restore_confirmed_session_skill_context(
        self,
        *,
        runtime: _QueryRuntime,
    ) -> None:
        """从持久化的 session snapshot 恢复一次性 skill 续接候选。"""
        await session_lifecycle.restore_confirmed_session_skill_context(
            self,
            runtime=runtime,
        )

    @staticmethod
    def _normalize_session_skill_snapshot(
        value: Any,
    ) -> dict[str, dict[str, Any]]:
        return _normalize_session_skill_snapshot(value)

    def _supports_session_skill_freshness_refresh(
        self,
        *,
        runtime: _QueryRuntime,
    ) -> bool:
        return _supports_session_skill_freshness_refresh(
            session=self.session,
            runtime=runtime,
        )

    @staticmethod
    def _refresh_session_skill_snapshot_entries(
        snapshot: dict[str, dict[str, Any]],
        *,
        stored_snapshot: dict[str, dict[str, Any]],
        effective_skill_dirs: dict[str, Path],
    ) -> list[Any]:
        return _refresh_session_skill_snapshot_entries(
            snapshot,
            stored_snapshot=stored_snapshot,
            effective_skill_dirs=effective_skill_dirs,
        )

    @staticmethod
    def _skill_freshness_notice_text(changes: list[Any]) -> str:
        return _skill_freshness_notice_text(changes)

    def _can_restore_confirmed_session_skill_context(
        self,
        *,
        runtime: _QueryRuntime,
        detector: Any,
    ) -> bool:
        return _can_restore_confirmed_session_skill_context(
            session_id=runtime.session_id,
            session_skill_detector=detector,
            session=self.session,
        )

    @staticmethod
    def _select_restorable_session_skill(
        snapshot: dict[str, dict[str, Any]],
        *,
        enabled_skills: list[str],
    ) -> str | None:
        return _select_restorable_session_skill(
            snapshot,
            enabled_skills=enabled_skills,
        )

    async def _prepare_query_runtime(
        self,
        *,
        request: AgentRequest,
        msgs: list[Any],
        query: str | None,
        preflight: _QueryPreflight,
        session_execution: Any = None,
    ) -> _RuntimeStartResult:
        """装配 agent、chat、MCP 客户端以及会话级 hook 运行状态。"""
        return await query_runtime.prepare_query_runtime(
            self,
            request=request,
            msgs=msgs,
            query=query,
            preflight=preflight,
            session_execution=session_execution,
        )

    @staticmethod
    async def _cleanup_query_runtime_mcp_clients(
        mcp_clients: list[Any],
    ) -> None:
        """Close runtime clients when assembly fails before handoff."""
        await _cleanup_mcp_clients(mcp_clients)

    async def _build_query_runtime_inputs(
        self,
        *,
        request: AgentRequest,
        msgs: list[Any],
        preflight: _QueryPreflight,
        session_execution: Any = None,
    ) -> _QueryRuntimeInputs:
        """Resolve request values needed before connecting runtime resources."""
        return await query_runtime.build_query_runtime_inputs(
            self,
            request=request,
            msgs=msgs,
            preflight=preflight,
            build_environment_context=build_env_context,
            request_source_id=_request_source_id,
            request_user_name=_request_user_name,
            request_passthrough_headers=_request_passthrough_headers,
            with_hook_context=_with_hook_context,
            merge_system_prompt_injections=_merge_system_prompt_injections,
            with_system_prompt_injections=_with_system_prompt_injections,
            request_system_prompt_injections=(
                _request_system_prompt_injections
            ),
            load_tenant_hooks=_load_tenant_hook_config,
            load_agent_configuration=load_agent_config,
            current_passthrough_headers=get_current_passthrough_headers,
            session_execution=session_execution,
        )

    async def _start_query_runtime_resources(
        self,
        *,
        request: AgentRequest,
        msgs: list[Any],
        inputs: _QueryRuntimeInputs,
        mcp_clients: list[Any],
    ) -> tuple[_QueryRuntimeResources, _RuntimeStartResult | None]:
        """Connect request resources and run the session-start hook."""
        channel_meta = getattr(request, "channel_meta", None) or {}
        turn_id = str(channel_meta.get("turn_id") or "")
        chat = await self._get_or_create_chat(
            session_id=inputs.session_id,
            user_id=inputs.user_id,
            channel=inputs.channel,
            name=_chat_name_from_messages(msgs),
            request=request,
            turn_id=turn_id,
        )
        with runtime_invocation_claims_context(
            chat_id=chat.id if chat is not None else None,
        ):
            scenario_snapshot = (
                await query_runtime.select_runtime_context_directives(
                    inputs,
                    request,
                    workspace_dir=Path(self.workspace_dir or WORKING_DIR),
                    chat=chat,
                    request_scenario_snapshot=(
                        _request_scenario_preset_snapshot
                    ),
                    with_scenario_mcp=_agent_config_with_scenario_mcp,
                    request_context_references=_request_context_references,
                    request_selected_skill_names=(
                        _request_selected_skill_names
                    ),
                )
            )
        query_runtime.build_runtime_mcp_clients(
            mcp_clients,
            agent_config=inputs.agent_config,
            tenant_id=self.tenant_id,
            user_id=inputs.user_id,
            passthrough_headers=inputs.passthrough_headers,
            session_id=inputs.session_id,
            chat_id=chat.id if chat is not None else None,
            trace_id=getattr(request, "trace_id", None),
            frozen_tools_by_key=_scenario_snapshot_frozen_mcp_tools(
                scenario_snapshot,
                inputs.agent_config,
            ),
            build_lazy_clients=_build_lazy_mcp_clients,
        )
        return await query_runtime.complete_runtime_activation(
            request=request,
            inputs=inputs,
            chat=chat,
            turn_id=turn_id,
            mcp_clients=mcp_clients,
            emit_session_start=self._emit_session_start_hook,
            load_selected_hooks=self._load_selected_skill_hooks,
        )

    async def _load_selected_skill_hooks(
        self,
        *,
        inputs: _QueryRuntimeInputs,
    ) -> HookSessionOverlay:
        """Load validated selected skill hooks after startup hooks complete."""
        return await query_runtime.load_selected_skill_hooks(
            inputs=inputs,
            workspace_dir=Path(self.workspace_dir or WORKING_DIR),
            tenant_id=self.tenant_id,
            approved_http_urls=(
                _load_tenant_approved_skill_hook_http_urls(self.tenant_id)
            ),
        )

    async def _finalize_query_runtime(
        self,
        *,
        request: AgentRequest,
        query: str | None,
        msgs: list[Any],
        preflight: _QueryPreflight,
        inputs: _QueryRuntimeInputs,
        resources: _QueryRuntimeResources,
        mcp_clients: list[Any],
    ) -> _QueryRuntime:
        """Create the agent and initialize session-skill state for one turn."""
        return await query_runtime.finalize_query_runtime(
            self,
            request=request,
            query=query,
            msgs=msgs,
            preflight=preflight,
            inputs=inputs,
            resources=resources,
            mcp_clients=mcp_clients,
            get_last_user_text=_get_last_user_text,
            debug_log=logger.debug,
        )

    async def _build_turn_plan(
        self,
        *,
        runtime: _QueryRuntime,
        request: AgentRequest,
        msgs: list[Any],
        query: str | None,
    ) -> _TurnPlan:
        """根据普通请求构建本轮输入。"""
        del request
        original_user_message = query or _get_last_user_text(msgs) or ""
        turn_msgs = list(msgs)
        if (
            turn_msgs
            and runtime.selected_context_directives
            and getattr(turn_msgs[-1], "role", None) == "user"
        ):
            turn_msgs[-1] = append_hidden_context_to_user_message(
                turn_msgs[-1],
                runtime.selected_context_directives,
            )
        return _TurnPlan(
            original_user_message=original_user_message,
            turn_msgs=turn_msgs,
        )

    async def _stream_agent_turns(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ):
        """流式执行当前 agent turn。"""
        async for item in turn_lifecycle.stream_agent_turns(
            self,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
            plan_interaction_card_metadata_key=(
                _PLAN_INTERACTION_CARD_METADATA_KEY
            ),
        ):
            yield item

    @staticmethod
    def _stream_printing_messages(**kwargs: Any) -> Any:
        return stream_printing_messages(**kwargs)

    @staticmethod
    def _resolve_max_stop_turns(agent_config: Any) -> int:
        return _resolve_max_stop_turns(agent_config)

    @staticmethod
    def _resolve_max_automatic_follow_up_turns(
        agent_config: Any,
        stop_turns: int,
    ) -> int:
        return _resolve_max_automatic_follow_up_turns(
            agent_config,
            stop_turns,
        )

    @staticmethod
    def _resolve_max_stop_transform_seconds(agent_config: Any) -> float:
        return _resolve_max_stop_transform_seconds(agent_config)

    @staticmethod
    def _extract_assistant_response(
        agent: SWEAgent,
        *,
        memory_start: int = 0,
    ) -> str:
        return _extract_assistant_response(agent, memory_start=memory_start)

    @staticmethod
    def _replace_assistant_response(
        agent: SWEAgent,
        response: str,
        *,
        memory_start: int = 0,
    ) -> bool:
        return _replace_assistant_response(
            agent,
            response,
            memory_start=memory_start,
        )

    def _requires_stop_output_buffer(
        self,
        *,
        request: AgentRequest,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
    ) -> bool:
        tenant_hooks = getattr(runtime, "tenant_hooks", HookConfig())
        agent_config = getattr(runtime, "agent_config", None)
        hook_overlay = getattr(runtime, "hook_overlay", HookSessionOverlay())
        if not _hook_config_enabled(
            tenant_hooks,
            agent_config,
            hook_overlay,
        ):
            return False
        return _requires_stop_output_buffer(
            request=request,
            runner=self,
            tenant_hooks=tenant_hooks,
            agent_config=agent_config,
            overlay=hook_overlay,
            prompt=plan.original_user_message,
        )

    @staticmethod
    def _request_goal_id(request: AgentRequest) -> str | None:
        return _request_goal_id(request)

    def _goal_matches_runtime_scope(
        self,
        goal: Any,
        runtime: _QueryRuntime,
    ) -> bool:
        return _goal_matches_runtime_scope(
            goal,
            runtime,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
        )

    @staticmethod
    def _build_goal_contract_context(goal: Any) -> str:
        return _build_goal_contract_context(goal)

    @staticmethod
    def _append_goal_tool_observations(
        observations: list[dict[str, str]],
        msg: Msg,
    ) -> None:
        _append_goal_tool_observations(observations, msg)

    @staticmethod
    def _build_goal_follow_up_msg(
        next_focus: str | None,
        steering: list[str] | None = None,
        contract_context: str | None = None,
    ) -> Msg:
        return _build_goal_follow_up_msg(
            next_focus,
            steering,
            contract_context,
        )

    @staticmethod
    def _should_stop_follow_up(outcome: _QueryTurnOutcome) -> bool:
        return _should_stop_follow_up(outcome)

    @staticmethod
    def _build_stop_follow_up_msg(reason: str) -> Msg:
        return _build_stop_follow_up_msg(reason)

    @staticmethod
    def _build_stop_incomplete_msg(reason: str) -> Msg:
        return _build_stop_incomplete_msg(reason)

    async def _emit_stop_hook_if_needed(
        self,
        *,
        request: AgentRequest,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ) -> MergedHookResult | None:
        """执行 Stop completion gate，active guard 已设置时跳过递归触发。"""
        tenant_hooks = getattr(runtime, "tenant_hooks", HookConfig())
        agent_config = getattr(runtime, "agent_config", None)
        hook_overlay = getattr(runtime, "hook_overlay", HookSessionOverlay())
        logger.warning(
            "[STOP-DEBUG] stop_entry trace_id=%s turn_id=%s response_len=%d "
            "active=%s plan_boundary=%s tenant_enabled=%s agent_enabled=%s "
            "overlay_ids=%s",
            getattr(request, "trace_id", None),
            (getattr(request, "channel_meta", None) or {}).get("turn_id"),
            len(outcome.assistant_response or ""),
            outcome.stop_hook_active,
            outcome.plan_interaction_turn_boundary,
            getattr(tenant_hooks, "enabled", None),
            getattr(
                getattr(agent_config, "hooks", None),
                "enabled",
                None,
            ),
            [entry.hook_id for entry in hook_overlay.entries],
        )
        if outcome.stop_hook_active:
            logger.warning("[STOP-DEBUG] skipped reason=stop_hook_active")
            _emit_runner_stop_skip_telemetry(
                request=request,
                runner=self,
                prompt=plan.original_user_message,
                assistant_response=outcome.assistant_response,
                skipped_reason="stop_hook_active",
            )
            return None
        if outcome.goal_finalization_fallback:
            _emit_runner_stop_skip_telemetry(
                request=request,
                runner=self,
                prompt=plan.original_user_message,
                assistant_response=outcome.assistant_response,
                skipped_reason="finalization_fallback",
            )
            return None
        if outcome.plan_interaction_turn_boundary:
            logger.warning(
                "[STOP-DEBUG] skipped reason=plan_interaction_turn_boundary",
            )
            _emit_runner_stop_skip_telemetry(
                request=request,
                runner=self,
                prompt=plan.original_user_message,
                assistant_response=outcome.assistant_response,
                skipped_reason="plan_interaction_turn_boundary",
            )
            return None
        if not outcome.assistant_response:
            logger.warning(
                "[STOP-DEBUG] skipped reason=empty_assistant_response",
            )
            _emit_runner_stop_skip_telemetry(
                request=request,
                runner=self,
                prompt=plan.original_user_message,
                assistant_response=outcome.assistant_response,
                skipped_reason="empty_assistant_response",
            )
            return None
        if not _hook_config_enabled(
            tenant_hooks,
            agent_config,
            hook_overlay,
        ):
            logger.warning("[STOP-DEBUG] skipped reason=hooks_disabled")
            _emit_runner_stop_skip_telemetry(
                request=request,
                runner=self,
                prompt=plan.original_user_message,
                assistant_response=outcome.assistant_response,
                skipped_reason="hooks_disabled",
            )
            return None

        outcome.stop_hook_active = True
        if outcome.stop_output_buffer_required:
            finalization = await _emit_runner_stop_finalization(
                request=request,
                runner=self,
                tenant_hooks=tenant_hooks,
                agent_config=agent_config,
                overlay=hook_overlay,
                prompt=plan.original_user_message,
                assistant_response=outcome.assistant_response,
                agent=runtime.agent,
                max_transform_seconds=(
                    self._resolve_max_stop_transform_seconds(
                        agent_config,
                    )
                ),
            )
            memory_replaced = self._replace_assistant_response(
                runtime.agent,
                finalization.final_response,
                memory_start=outcome.assistant_memory_start,
            )
            if not memory_replaced:
                return MergedHookResult(
                    decision=HookDecision.BLOCK,
                    reason="Stop output transformation could not finalize response",
                    has_blocking_failure=True,
                    blocking_failure_reason=(
                        "Stop output transformation could not finalize response"
                    ),
                )
            outcome.assistant_response = finalization.final_response
            if finalization.transformation_failed:
                return MergedHookResult(
                    decision=HookDecision.BLOCK,
                    reason="Stop output transformation failed",
                    has_blocking_failure=True,
                    blocking_failure_reason="Stop output transformation failed",
                )
            return finalization.validation_result
        return await _emit_runner_hook(
            HookEventName.STOP,
            request=request,
            runner=self,
            tenant_hooks=tenant_hooks,
            agent_config=agent_config,
            overlay=hook_overlay,
            prompt=plan.original_user_message,
            assistant_response=outcome.assistant_response,
            agent=runtime.agent,
        )

    async def _stream_completion_lifecycle(
        self,
        *,
        request: AgentRequest,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ):
        """Coordinate the extracted Goal and Stop turn lifecycle."""
        identity = self._answer_turn_identity(request)
        if identity is not None:
            self._answer_turn_runtimes[identity] = (
                runtime,
                runtime.session_execution,
            )
        async for item in turn_lifecycle.stream_completion_lifecycle(
            self,
            request=request,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        ):
            yield item

    async def _generate_backend_suggestions_if_needed(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ) -> None:
        """保留旧调用点，但不再由 runner 重复生成 suggestions。"""
        del runtime, plan, outcome
        logger.debug(
            "Suggestions generation handled by frontend external API; "
            "backend does not schedule duplicate generation.",
        )

    async def _index_model_output_if_needed(
        self,
        *,
        trace_id: str | None,
        agent: SWEAgent | None,
    ) -> None:
        """有 trace 时把最终模型输出写入 Monitor。"""
        if not trace_id or agent is None:
            return

        logger.debug(
            "Preparing to index model output: trace_id=%s, agent=%s",
            trace_id,
            type(agent).__name__,
        )
        assistant_response = _extract_assistant_response(agent)
        logger.debug(
            "Extracted assistant response: trace_id=%s, response_len=%d",
            trace_id,
            len(assistant_response) if assistant_response else 0,
        )
        if assistant_response:
            await _index_model_output_to_monitor(trace_id, assistant_response)
            return

        logger.warning("No assistant response to index: trace_id=%s", trace_id)

    async def _end_trace_if_needed(
        self,
        trace_id: str | None,
        status: TraceStatus,
        error: str | None = None,
    ) -> None:
        """结束 trace，失败时只记录日志避免影响主链路。

        如果 trace 是 attach 到外部已存在的 trace（attached=True），则跳过结束，
        让外部创建者负责结束。
        """
        if not trace_id or not has_trace_manager():
            return

        # 检查是否是 attach 的 trace，如果是则跳过结束
        from ...tracing import get_current_trace

        ctx = get_current_trace()
        if ctx and ctx.attached and ctx.trace_id == trace_id:
            logger.debug(
                "Skip ending attached trace (owned by external): trace_id=%s",
                trace_id[:20] if trace_id else "(empty)",
            )
            from ...tracing import set_current_trace

            # 清除 context，让后续请求不会继承外部 trace。
            set_current_trace(None)
            return

        try:
            trace_mgr = get_trace_manager()
            if error is None:
                await trace_mgr.end_trace(trace_id, status=status)
            else:
                await trace_mgr.end_trace(
                    trace_id,
                    status=status,
                    error=error,
                )
        except Exception as trace_err:
            logger.warning("Failed to end trace: %s", trace_err)

    async def _handle_query_cancelled(
        self,
        *,
        trace_id: str | None,
        session_id: str,
        agent: SWEAgent | None,
        exc: asyncio.CancelledError,
    ) -> None:
        """处理 query 被取消时的 trace 和 agent 中断。"""
        logger.info(f"query_handler: {session_id} cancelled!")
        await self._end_trace_if_needed(trace_id, TraceStatus.CANCELLED)
        if agent is not None:
            await agent.interrupt()
        raise AgentException("Task has been cancelled!") from exc

    async def _settle_stopped_turn(
        self,
        *,
        runtime: _QueryRuntime | None,
        request: AgentRequest,
        session_execution: Any,
    ) -> None:
        """Report a stopped execution for coordinator-owned settlement."""
        identity = self._answer_turn_identity(request)
        coordinator = self._answer_turn_coordinator
        if identity is None or coordinator is None:
            return
        if await coordinator.status(identity) != TurnStatus.STOPPING:
            return
        self._answer_turn_runtimes[identity] = (runtime, session_execution)
        await coordinator.settle(TurnOutcome.cancelled(identity))

    async def _handle_query_error(
        self,
        *,
        request: AgentRequest,
        exc: Exception,
        trace_id: str | None,
        locals_snapshot: dict[str, Any],
    ) -> None:
        """记录 query 异常 dump，并把 dump 路径附加到异常信息。"""
        debug_dump_path = write_query_error_dump(
            request=request,
            exc=exc,
            locals_=locals_snapshot,
        )
        path_hint = (
            f"\n(Details:  {debug_dump_path})" if debug_dump_path else ""
        )
        logger.exception(f"Error in query handler: {exc}{path_hint}")
        await self._end_trace_if_needed(
            trace_id,
            TraceStatus.ERROR,
            error=str(exc),
        )
        if not debug_dump_path:
            return

        setattr(exc, "debug_dump_path", debug_dump_path)
        if hasattr(exc, "add_note"):
            exc.add_note(f"(Details:  {debug_dump_path})")
        suffix = f"\n(Details:  {debug_dump_path})"
        exc.args = (
            (f"{exc.args[0]}{suffix}" if exc.args else suffix.strip()),
        ) + exc.args[1:]

    async def _persist_model_call_failed_detail_safely(
        self,
        *,
        request: AgentRequest,
        detail: ModelCallFailureDetail,
        session_execution: Any = None,
    ) -> None:
        """保存模型调用失败详情，持久化异常不应覆盖原始失败信息。"""
        try:
            await self._persist_model_call_failed_detail(
                request=request,
                detail=detail,
                session_execution=session_execution,
            )
        except Exception:
            logger.warning(
                "Failed to persist model-call failure detail for history",
                exc_info=True,
            )

    async def _raise_console_model_call_failed_if_needed(
        self,
        *,
        request: AgentRequest,
        exc: Exception,
        trace_id: str | None,
        session_execution: Any = None,
    ) -> None:
        """在 Console 模型调用失败时抛出用户可见的结构化异常。"""
        if getattr(request, "channel", DEFAULT_CHANNEL) != DEFAULT_CHANNEL:
            return

        detail = extract_model_call_failure_detail(exc)
        if detail is None:
            return

        logger.warning(
            "Console model call failed after final attempt: "
            "kind=%s status=%s truncated=%s",
            detail.kind.value,
            detail.provider_status,
            detail.truncated,
        )
        await self._end_trace_if_needed(
            trace_id,
            TraceStatus.ERROR,
            error=detail.message,
        )
        await self._persist_model_call_failed_detail_safely(
            request=request,
            detail=detail,
            session_execution=session_execution,
        )
        raise ModelCallFailedException(detail) from exc

    async def _persist_model_call_failed_detail(
        self,
        *,
        request: AgentRequest,
        detail: ModelCallFailureDetail,
        session_execution: Any = None,
    ) -> None:
        """Persist user-visible model-call failure detail outside memory."""
        if self.session is None or not hasattr(
            self.session,
            "mutate_session_state",
        ):
            return

        session_id = _coerce_session_storage_id(
            getattr(request, "session_id", None),
        )
        user_id = _coerce_session_storage_user_id(
            getattr(request, "user_id", None),
        )
        if not session_id:
            return

        record = {
            "id": f"model-call-error-{uuid4().hex}",
            "type": "error",
            "role": "assistant",
            "status": "failed",
            "code": detail.code,
            "message": detail.message,
            "content": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "model_call_failed": True,
                "kind": detail.kind.value,
                "provider_status": detail.provider_status,
                "truncated": detail.truncated,
            },
        }

        def _append_record(state: dict[str, Any]) -> dict[str, Any]:
            next_state = state if isinstance(state, dict) else {}
            existing = next_state.get(MODEL_CALL_FAILED_MESSAGES_STATE_KEY)
            records = list(existing) if isinstance(existing, list) else []
            records.append(record)
            next_state[MODEL_CALL_FAILED_MESSAGES_STATE_KEY] = records
            return next_state

        if session_execution is not None:
            _append_record(session_execution.state)
            session_execution.mark_state_dirty()
            return

        await self.session.mutate_session_state(
            session_id=session_id,
            mutator=_append_record,
            user_id=user_id,
            create_if_not_exist=True,
        )

    async def _save_state_during_cleanup(
        self,
        *,
        runtime: _QueryRuntime | None,
        session_state_loaded: bool,
        fallback_agent: SWEAgent | None = None,
        fallback_session_id: str = "",
        fallback_user_id: str = "",
        fallback_skip_history: bool = False,
        fallback_session_execution: Any = None,
    ) -> None:
        await query_cleanup.save_state_during_cleanup(
            self,
            runtime=runtime,
            session_state_loaded=session_state_loaded,
            cleanup_timeout=QUERY_CLEANUP_TIMEOUT,
            hook_config_enabled=_hook_config_enabled,
            fallback_agent=fallback_agent,
            fallback_session_id=fallback_session_id,
            fallback_user_id=fallback_user_id,
            fallback_skip_history=fallback_skip_history,
            fallback_session_execution=fallback_session_execution,
        )

    async def _update_chat_during_cleanup(
        self,
        runtime: _QueryRuntime | None,
    ) -> None:
        await query_cleanup.update_chat_during_cleanup(
            self,
            runtime,
            cleanup_timeout=QUERY_CLEANUP_TIMEOUT,
        )

    async def _cleanup_mcp_during_cleanup(
        self,
        runtime: _QueryRuntime | None,
    ) -> None:
        await query_cleanup.cleanup_runtime_mcp(
            runtime,
            cleanup_timeout=QUERY_CLEANUP_TIMEOUT,
            cleanup_mcp=_cleanup_mcp_clients,
        )

    async def _end_skill_detector_during_cleanup(
        self,
        runtime: _QueryRuntime | None,
    ) -> None:
        await query_cleanup.end_skill_detector_during_cleanup(
            runtime,
            cleanup_timeout=QUERY_CLEANUP_TIMEOUT,
        )

    async def _cleanup_query_resources(
        self,
        *,
        runtime: _QueryRuntime | None,
        session_state_loaded: bool,
        session_id: str,
    ) -> None:
        """集中执行 query finally 阶段的资源清理。"""
        logger.info(
            "Runner finally block executing for session %s",
            session_id,
        )
        await query_cleanup.cleanup_query_resources(
            self,
            runtime=runtime,
            session_state_loaded=session_state_loaded,
            session_id=session_id,
        )

    async def _cleanup_blocked_runtime_start(
        self,
        runtime_start: _RuntimeStartResult | None,
    ) -> None:
        await query_cleanup.cleanup_blocked_runtime_start(
            self,
            runtime_start,
            cleanup_timeout=QUERY_CLEANUP_TIMEOUT,
            cleanup_mcp=_cleanup_mcp_clients,
        )

    async def _store_qa_content_if_needed(
        self,
        *,
        runtime: _QueryRuntime | None,
        query: str | None,
        outcome: _QueryTurnOutcome,
    ) -> None:
        """按 QA_EXTRACTION_ONLY 模式保存本轮问答内容。"""
        if (
            runtime is None
            or runtime.chat is None
            or not outcome.task_completed
        ):
            return

        suggestions_config = runtime.agent_config.running.suggestions
        if (
            not suggestions_config.enabled
            or suggestions_config.mode != SuggestionMode.QA_EXTRACTION_ONLY
        ):
            return

        assistant_response = _extract_assistant_response(runtime.agent)
        user_message = query
        if not assistant_response or not user_message:
            logger.debug(
                "No Q&A content to extract for suggestions: "
                "assistant_response=%s, user_message=%s",
                bool(assistant_response),
                bool(user_message),
            )
            return

        from ..suggestions.service import extract_key_content
        from ..suggestions.store import store_qa_content

        extracted_user = user_message[
            : suggestions_config.user_message_max_length
        ]
        extracted_assistant = extract_key_content(
            assistant_response,
            max_length=min(
                suggestions_config.qa_content_total_max_length
                - len(extracted_user),
                suggestions_config.assistant_response_max_length,
            ),
        )
        await store_qa_content(
            chat_id=runtime.chat.id,
            user_message=extracted_user,
            assistant_response=extracted_assistant,
            tenant_id=self.tenant_id,
        )
        logger.info(
            "Stored Q&A content for suggestions: chat_id=%s, "
            "user_len=%d, assistant_len=%d",
            runtime.chat.id,
            len(extracted_user),
            len(extracted_assistant),
        )

    # ── Query 级别重试辅助方法 ──

    @staticmethod
    def _summarize_retry_error(exc: BaseException) -> str:
        return query_attempt.summarize_retry_error(exc)

    @staticmethod
    def _extract_retry_config(
        agent_config,
    ) -> tuple[bool, int, float, float]:
        return query_attempt.extract_retry_config(agent_config)

    @staticmethod
    def _compute_retry_backoff(
        retry_attempt: int,
        backoff_cap: float,
        backoff_base: float,
    ) -> float:
        return query_attempt.compute_retry_backoff(
            retry_attempt,
            backoff_cap,
            backoff_base,
        )

    @staticmethod
    def _should_retry(
        retry_attempt: int,
        max_retry_attempts: int,
        exc: BaseException,
    ) -> bool:
        return query_attempt.should_retry(
            retry_attempt,
            max_retry_attempts,
            exc,
        )

    async def _save_state_before_retry(
        self,
        agent: Any,
        session_state_loaded: bool,
        session_id: str,
        skip_history: bool,
        user_id: str,
    ) -> dict[str, Any] | None:
        del session_state_loaded, session_id, skip_history, user_id
        return query_attempt.snapshot_state_before_retry(agent)

    def _load_query_retry_settings(
        self,
        agent_config: Any | None = None,
    ) -> tuple[int, int, float, float]:
        return load_retry_settings(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            agent_config=agent_config,
            load_agent_config_fn=load_agent_config,
        )

    async def _add_retry_notice_to_memory(
        self,
        agent: Any,
        retry_msg: Msg,
    ) -> None:
        await query_attempt.add_retry_notice_to_memory(agent, retry_msg)

    @staticmethod
    def _build_retry_status_msg(text: str) -> Msg:
        return query_attempt.build_retry_status_msg(text)

    async def _stream_retry_backoff_notice(
        self,
        *,
        retry_attempt: int,
        max_retries: int,
        backoff_base: float,
        backoff_cap: float,
        session_id: str,
        retry_state: _RetryState,
    ):
        async for item in query_attempt.stream_retry_backoff_notice(
            self,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            backoff_base=backoff_base,
            backoff_cap=backoff_cap,
            session_id=session_id,
            retry_state=retry_state,
        ):
            yield item

    async def _stream_retryable_query_error(
        self,
        *,
        exc: BaseException,
        retry_attempt: int,
        max_retry_attempts: int,
        max_retries: int,
        retry_state: _RetryState,
        runtime: _QueryRuntime | None,
        session_id: str,
    ):
        async for item in query_attempt.stream_retryable_query_error(
            self,
            exc=exc,
            retry_attempt=retry_attempt,
            max_retry_attempts=max_retry_attempts,
            max_retries=max_retries,
            retry_state=retry_state,
            runtime=runtime,
            session_id=session_id,
        ):
            yield item

    async def _complete_successful_query_attempt(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
        trace_id: str | None,
        skill_snapshot_to_persist: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """执行 agent 输出后的持久化、suggestion 与 trace 收尾。"""
        await self._generate_backend_suggestions_if_needed(
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        )
        await self._index_model_output_if_needed(
            trace_id=trace_id,
            agent=runtime.agent,
        )
        await self._end_trace_if_needed(
            trace_id,
            TraceStatus.COMPLETED,
        )
        if skill_snapshot_to_persist is not None:
            if runtime.session_execution is not None:
                runtime.session_execution.state[
                    SESSION_SKILL_SNAPSHOT_STATE_KEY
                ] = skill_snapshot_to_persist
            else:
                await self.session.save_session_skill_snapshot(
                    session_id=runtime.session_id,
                    user_id=runtime.user_id,
                    snapshot=skill_snapshot_to_persist,
                )

    async def _finish_blocked_query_attempt(
        self,
        *,
        runtime: _QueryRuntime,
        outcome: _QueryTurnOutcome,
        trace_id: str | None,
        skill_snapshot_to_persist: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Stop 耗尽预算时仍需写入最终输出并结束 trace。"""
        if outcome.pre_tool_terminal_stop:
            await self._end_trace_if_needed(
                trace_id,
                TraceStatus.COMPLETED,
            )
        elif outcome.completion_marked_incomplete:
            await self._index_model_output_if_needed(
                trace_id=trace_id,
                agent=runtime.agent,
            )
            await self._end_trace_if_needed(
                trace_id,
                TraceStatus.COMPLETED,
            )
        if skill_snapshot_to_persist is not None:
            if runtime.session_execution is not None:
                runtime.session_execution.state[
                    SESSION_SKILL_SNAPSHOT_STATE_KEY
                ] = skill_snapshot_to_persist
            else:
                await self.session.save_session_skill_snapshot(
                    session_id=runtime.session_id,
                    user_id=runtime.user_id,
                    snapshot=skill_snapshot_to_persist,
                )

    async def _stream_single_query_attempt(
        self,
        *,
        attempt_input: _QueryAttemptInput,
        outcome: _QueryTurnOutcome,
        retry_state: _RetryState,
        attempt_state: _QueryAttemptState,
    ):
        """Coordinate one extracted query attempt."""
        async for item in query_attempt.stream_single_query_attempt(
            self,
            attempt_input=attempt_input,
            outcome=outcome,
            retry_state=retry_state,
            attempt_state=attempt_state,
        ):
            yield item

    def _new_query_turn_outcome(self) -> _QueryTurnOutcome:
        return _QueryTurnOutcome()

    def _new_retry_state(self) -> _RetryState:
        return _RetryState()

    def _new_query_attempt_input(self, **kwargs: Any) -> _QueryAttemptInput:
        return _QueryAttemptInput(**kwargs)

    def _new_query_attempt_state(self) -> _QueryAttemptState:
        return _QueryAttemptState()

    @staticmethod
    def _request_file_url_network(request: AgentRequest) -> Any:
        return _request_file_url_network(request)

    @staticmethod
    def _build_skill_freshness_notice_msg(text: str) -> Msg:
        return _build_skill_freshness_notice_msg(text)

    async def _stream_query_after_preflight(
        self,
        msgs,
        *,
        request: AgentRequest,
        query: str | None,
        session_id: str,
        preflight: _QueryPreflight,
        session_execution: Any = None,
    ):
        """Coordinate retry and attempt execution after preflight."""
        async for item in query_attempt.stream_query_after_preflight(
            self,
            msgs=msgs,
            request=request,
            query=query,
            session_id=session_id,
            preflight=preflight,
            session_execution=session_execution,
        ):
            yield item

    async def _stream_query_entry(
        self,
        msgs,
        *,
        request: AgentRequest,
        query: str | None,
        session_id: str,
        user_id: str,
    ):
        """Compatibility facade for admission execution."""
        async for msg, last in stream_admission(
            self,
            msgs,
            request=request,
            query=query,
            session_id=session_id,
            user_id=user_id,
        ):
            yield msg, last

    async def _stream_query_frames(
        self,
        msgs,
        *,
        request: AgentRequest,
        query: str | None,
        session_id: str,
        user_id: str,
    ):
        """Yield query frames from the configured execution adapter."""
        query_execution = getattr(self, "_query_execution", None)
        if query_execution is not None:
            async for frame in query_execution.stream(
                QueryInvocation(request=request, msgs=tuple(msgs)),
            ):
                yield self._attach_trace_id_to_msg(
                    frame.message,
                    getattr(request, "trace_id", None),
                ), frame.last
            return
        async for msg, last in self._stream_query_entry(
            msgs,
            request=request,
            query=query,
            session_id=session_id,
            user_id=user_id,
        ):
            yield self._attach_trace_id_to_msg(
                msg,
                getattr(request, "trace_id", None),
            ), last

    def _build_query_trace_fields(
        self,
        request: AgentRequest | None,
        *,
        session_id: str,
        user_id: str,
        turn_id: str,
    ) -> TraceFields | None:
        if (
            getattr(request, "execution_origin", None) == "scheduled"
            or not user_id
            or not session_id
            or not turn_id
            or not self.agent_id
        ):
            return None
        return TraceFields(
            task_id=session_id,
            user_id=user_id,
            session_id=turn_id,
            agent_id=self.agent_id,
            agent_version=__version__,
            source_id=_request_source_id(request),
        )

    def _prepare_query_handler_context(
        self,
        msgs: Any,
        request: AgentRequest | None,
    ) -> _QueryHandlerContext:
        query = _get_last_user_text(msgs)
        session_id = getattr(request, "session_id", "") or ""
        user_id = getattr(request, "user_id", "") or ""
        channel_meta = getattr(request, "channel_meta", None) or {}
        identity = self._answer_turn_identity(request)
        turn_id = identity.turn_id if identity is not None else ""
        if identity is not None:
            self._answer_turn_locations[identity] = (
                str(session_id),
                str(user_id),
            )
            request.channel_meta = {
                **channel_meta,
                "turn_id": identity.turn_id,
                "msgid": identity.msgid,
            }
        return _QueryHandlerContext(
            query=query,
            session_id=session_id,
            user_id=user_id,
            identity=identity,
            turn_id=turn_id,
            trace_fields=self._build_query_trace_fields(
                request,
                session_id=session_id,
                user_id=user_id,
                turn_id=turn_id,
            ),
        )

    async def _stream_query_handler_frames(
        self,
        msgs: Any,
        request: AgentRequest | None,
        context: _QueryHandlerContext,
    ):
        with use_b3_trace_context(
            getattr(request, "b3_context", None),
            context.trace_fields,
        ):
            trace_scope = (
                global_tracer.start_as_current_span(
                    "agent.run",
                    kind=SpanKind.SERVER,
                    trace_fields=context.trace_fields,
                )
                if context.trace_fields is not None
                else nullcontext(None)
            )
            async with trace_scope as span:
                if span is not None:
                    span.set_attribute(
                        "agent.user_message",
                        context.query or "",
                    )
                async for msg, last in self._stream_query_frames(
                    msgs,
                    request=request,
                    query=context.query,
                    session_id=context.session_id,
                    user_id=context.user_id,
                ):
                    yield msg, last

    async def _settle_query_handler_outcome(
        self,
        identity: TurnIdentity | None,
        status: str,
        error: Exception | None = None,
    ) -> None:
        if identity is None:
            return
        if status == "cancelled":
            outcome = TurnOutcome.cancelled(identity)
        elif status == "failed":
            outcome = TurnOutcome.failed(identity, error)
        else:
            outcome = TurnOutcome.completed(identity)
        await self._report_answer_turn_outcome(identity, outcome)

    async def query_handler(
        self,
        msgs,
        request: AgentRequest = None,
        **kwargs,
    ):
        """处理 Agent query，并保持 Runner 期望的流式输出格式。"""
        logger.debug(
            f"AgentRunner.query_handler called: agent_id={self.agent_id}, "
            f"msgs={msgs}, request={request}",
        )
        context = self._prepare_query_handler_context(msgs, request)
        identity = context.identity
        task = asyncio.current_task()
        if identity is not None and task is not None:
            self._answer_turn_tasks[identity] = task
        channel_meta = getattr(request, "channel_meta", None) or {}
        defer_settlement = bool(
            channel_meta.get(_DEFER_ANSWER_TURN_SETTLEMENT_META_KEY),
        )
        try:
            async for msg, last in self._stream_query_handler_frames(
                msgs,
                request,
                context,
            ):
                yield msg, last
        except asyncio.CancelledError:
            if not defer_settlement:
                await self._settle_query_handler_outcome(identity, "cancelled")
            raise
        except Exception as exc:
            if not defer_settlement:
                await self._settle_query_handler_outcome(
                    identity,
                    "failed",
                    exc,
                )
            raise
        else:
            if not defer_settlement:
                await self._settle_query_handler_outcome(identity, "completed")
        finally:
            if identity is not None:
                self._answer_turn_tasks.pop(identity, None)

    async def get_state_loaded(
        self,
        agent: SWEAgent,
        session_id: str | None,
        session_state_loaded: bool,
        skip_history: bool | Any,
        user_id: str | None,
        *,
        session_execution: Any = None,
        retry_state_snapshot: dict[str, Any] | None = None,
    ) -> bool:
        return await session_lifecycle.get_state_loaded(
            self,
            agent,
            session_id,
            session_state_loaded,
            skip_history,
            user_id,
            coerce_session_id=_coerce_session_storage_id,
            coerce_user_id=_coerce_session_storage_user_id,
            session_execution=session_execution,
            retry_state_snapshot=retry_state_snapshot,
        )

    async def _save_cron_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
        session_execution: Any = None,
    ) -> None:
        """保存 cron 任务状态，保留旧历史并追加本轮新增消息。"""
        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        current_agent_state = agent.state_dict()
        request_context = getattr(agent, "_request_context", {}) or {}
        execution_key = request_context.get("cron_execution_key")
        merge_stats: dict[str, Any] = {}

        def _merge(existing_state: dict[str, Any]) -> dict[str, Any]:
            (
                merged_state,
                existing_content,
                current_content,
                stripped_count,
                should_commit,
            ) = _build_cron_append_state(
                existing_state,
                current_agent_state,
                hook_overlay,
                execution_key=execution_key,
            )
            merge_stats["existing_content"] = existing_content
            merge_stats["current_content"] = current_content
            merge_stats["stripped_count"] = stripped_count
            merge_stats["should_commit"] = should_commit
            return merged_state

        if session_execution is not None:
            merged_state = _merge(session_execution.state)
            if merge_stats.get("should_commit", True):
                await session_execution.commit_state(merged_state)
        else:
            existing_state = await self.session.get_session_state_dict(
                session_id=storage_session_id,
                user_id=storage_user_id,
                allow_not_exist=True,
            )
            merged_state = _merge(existing_state)
            if merge_stats.get("should_commit", True):
                await self.session.save_merged_state(
                    session_id=storage_session_id,
                    user_id=storage_user_id,
                    state=merged_state,
                )

        logger.info(
            "Cron task: saved merged session state "
            "(session_id=%s, existing_memory_content=%s, new_content=%s, "
            "stripped_internal_follow_ups=%s)",
            session_id,
            len(merge_stats.get("existing_content", [])),
            len(merge_stats.get("current_content", [])),
            merge_stats.get("stripped_count", 0),
        )

    async def _save_legacy_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
        session_execution: Any = None,
    ) -> None:
        """兼容不支持 state_dict 的 agent 落盘路径。"""
        if session_execution is not None:
            raise TypeError("session transaction requires agent.state_dict")
        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        await self.session.save_session_state(
            session_id=storage_session_id,
            user_id=storage_user_id,
            agent=agent,
        )
        if hook_overlay is None:
            await self.session.mutate_session_state(
                session_id=storage_session_id,
                mutator=lambda state: {
                    key: value
                    for key, value in state.items()
                    if key != "hook_overlay"
                },
                user_id=storage_user_id,
                create_if_not_exist=True,
            )
            return

        await self.session.update_session_state(
            storage_session_id,
            "hook_overlay",
            hook_overlay.model_dump(mode="json", by_alias=True),
            user_id=storage_user_id,
        )

    async def _save_regular_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
        session_execution: Any = None,
    ) -> None:
        """保存普通请求状态，并在落盘前剔除内部续跑提示。"""
        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        if not hasattr(agent, "state_dict"):
            await self._save_legacy_session_state(
                agent,
                session_id,
                user_id,
                hook_overlay,
                session_execution,
            )
            return

        current_agent_state = agent.state_dict()
        stripped_count = _strip_internal_follow_up_messages_from_state(
            current_agent_state,
        )
        deduped_external_approvals = (
            _dedupe_external_approval_messages_from_state(
                current_agent_state,
            )
        )
        context_usage_snapshot: dict[str, Any] | None = None
        context_usage_capture_failed = False
        try:
            context_usage_snapshot = (
                await capture_context_usage(agent, current_agent_state)
            ).model_dump(mode="json")
        except Exception:  # noqa: BLE001 - session persistence must continue
            context_usage_capture_failed = True
            logger.warning(
                "Failed to capture context usage; preserving prior snapshot "
                "(session_id=%s)",
                session_id,
                exc_info=True,
            )

        def _merge(existing_state: dict[str, Any]) -> dict[str, Any]:
            state_modules: dict[str, Any] = (
                dict(existing_state)
                if isinstance(existing_state, dict)
                else {}
            )
            if SESSION_SKILL_SNAPSHOT_STATE_KEY in state_modules:
                state_modules[SESSION_SKILL_SNAPSHOT_STATE_KEY] = (
                    _normalize_session_skill_snapshot(
                        state_modules.get(
                            SESSION_SKILL_SNAPSHOT_STATE_KEY,
                        ),
                    )
                )
            state_modules["agent"] = current_agent_state
            if context_usage_snapshot is not None:
                state_modules[CONTEXT_USAGE_STATE_KEY] = context_usage_snapshot
                state_modules.pop(CONTEXT_USAGE_INVALID_STATE_KEY, None)
            elif context_usage_capture_failed:
                if isinstance(
                    state_modules.get(CONTEXT_USAGE_STATE_KEY),
                    dict,
                ):
                    state_modules[CONTEXT_USAGE_INVALID_STATE_KEY] = True
                else:
                    state_modules.pop(CONTEXT_USAGE_INVALID_STATE_KEY, None)
            if hook_overlay is not None:
                state_modules["hook_overlay"] = hook_overlay.model_dump(
                    mode="json",
                    by_alias=True,
                )
            else:
                state_modules.pop("hook_overlay", None)
            return state_modules

        if session_execution is not None:
            await session_execution.commit_state(
                _merge(session_execution.state),
            )
        else:
            await self.session.mutate_session_state(
                session_id=storage_session_id,
                mutator=_merge,
                user_id=storage_user_id,
                create_if_not_exist=True,
            )
        logger.info(
            "Saved session state with stripped_internal_follow_ups=%s "
            "deduped_external_approvals=%s "
            "(session_id=%s)",
            stripped_count,
            deduped_external_approvals,
            session_id,
        )

    async def save_job_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        skip_history: bool | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
        *,
        session_execution: Any = None,
    ):
        """按请求类型保存 session state。"""
        if session_execution is None:
            await session_lifecycle.save_job_session_state(
                self,
                agent,
                session_id,
                skip_history,
                user_id,
                hook_overlay,
            )
            return
        await session_lifecycle.save_job_session_state(
            self,
            agent,
            session_id,
            skip_history,
            user_id,
            hook_overlay,
            session_execution=session_execution,
        )

    async def _cleanup_denied_session_memory(
        self,
        session_id: str,
        user_id: str,
        denial_response: "Msg | None" = None,
        session_execution: Any = None,
    ) -> None:
        """Clean up session memory after a tool-guard denial.

        In the deny path (no agent is created), this method:

        1. Removes the LLM denial explanation (the assistant message
           immediately following the last marked entry).
        2. Strips ``TOOL_GUARD_DENIED_MARK`` from all marks lists so
           the kept tool-call info becomes normal memory entries.
        3. Appends *denial_response* (e.g. "❌ Tool denied") to the
           persisted session memory.
        """
        if not hasattr(self, "session") or self.session is None:
            return

        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        path = self.session._get_save_path(  # pylint: disable=protected-access
            storage_session_id,
            storage_user_id,
        )
        if session_execution is None and not Path(path).exists():
            return

        try:
            modified = False

            def _cleanup_state(
                states: dict[str, Any],
            ) -> dict[str, Any] | None:
                nonlocal modified
                content = _get_agent_memory_content(states)
                if content is None:
                    return None

                last_marked_idx = _last_tool_guard_denied_index(content)
                if _remove_following_denial_explanation(
                    content,
                    last_marked_idx,
                ):
                    modified = True

                if _strip_tool_guard_denied_marks(content):
                    modified = True

                if denial_response is not None:
                    content.append(
                        _build_denial_response_memory_entry(denial_response),
                    )
                    modified = True

                return states if modified else None

            if session_execution is not None:
                updated = _cleanup_state(session_execution.state)
                if updated is not None:
                    await session_execution.commit_state(updated)
            else:
                await self.session.mutate_session_state(
                    session_id=storage_session_id,
                    mutator=_cleanup_state,
                    user_id=storage_user_id,
                    create_if_not_exist=False,
                )

            if not modified:
                return
            logger.info(
                "Tool guard: cleaned up denied session memory in %s",
                path,
            )
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Failed to clean up denied messages from session %s",
                session_id,
                exc_info=True,
            )

    async def _enforce_query_timeout(
        self,
        msg_stream,
        session_id: str,
        agent=None,
        timeout_seconds: float = QUERY_TIMEOUT_SECONDS,
        run_key: str | None = None,
    ):
        """Wrap an async message stream with global wall-clock timeout.

        Iterates over *msg_stream* and yields each ``(msg, last)`` pair.
        If the total elapsed time since the first call exceeds
        *timeout_seconds*, a timeout notification message is yielded,
        the stream is terminated, and the agent is interrupted.

        Args:
            msg_stream: Async iterable of ``(msg, last)`` tuples.
            session_id: Session identifier for logging.
            agent: Agent instance to interrupt on timeout.
            timeout_seconds: Maximum wall-clock seconds for the entire
                query (default: ``QUERY_TIMEOUT_SECONDS``).

        Yields:
            ``(msg, last)`` tuples, with a final timeout notification if
            the limit is exceeded.
        """
        start = time.monotonic()
        async for msg, last in msg_stream:
            elapsed = time.monotonic() - start
            if elapsed > timeout_seconds:
                logger.warning(
                    "Query timeout (%.0fs > %.0fs) for session %s",
                    elapsed,
                    timeout_seconds,
                    session_id,
                )
                if run_key and self._task_tracker is not None:
                    mark_stopping = getattr(
                        self._task_tracker,
                        "mark_stopping",
                        None,
                    )
                    if mark_stopping is not None:
                        try:
                            await mark_stopping(run_key)
                        except Exception as status_err:
                            logger.warning(
                                "Failed to mark run stopping after query "
                                "timeout: %s",
                                status_err,
                            )

                # Interrupt the agent to stop it from continuing
                if agent is not None:
                    try:
                        await agent.interrupt()
                        logger.info(
                            "Agent interrupted after query timeout for "
                            "session %s",
                            session_id,
                        )
                    except Exception as interrupt_err:
                        logger.warning(
                            "Failed to interrupt agent on query timeout: "
                            "%s",
                            interrupt_err,
                        )
                yield (
                    Msg(
                        name="Friday",
                        role="assistant",
                        content=[
                            TextBlock(
                                type="text",
                                text=(
                                    f"⏰ 任务执行超时"
                                    f"（{int(elapsed)}s > "
                                    f"{int(timeout_seconds)}s），"
                                    f"已自动终止。"
                                ),
                            ),
                        ],
                    ),
                    True,
                )
                return
            yield msg, last

    async def stream_query(
        self,
        request,
        **kwargs,
    ) -> AsyncGenerator[Event, None]:
        """Wrap base streaming to normalize reasoning end boundaries."""
        task_progress_enabled = is_chat_task_progress_enabled(
            get_current_source_system_config(),
        )
        identity = self._answer_turn_identity(request)
        terminal_status: str | None = None
        channel_meta = getattr(request, "channel_meta", None)
        marker_was_present = (
            isinstance(channel_meta, dict)
            and _DEFER_ANSWER_TURN_SETTLEMENT_META_KEY in channel_meta
        )
        previous_defer_marker = (
            channel_meta.get(_DEFER_ANSWER_TURN_SETTLEMENT_META_KEY)
            if isinstance(channel_meta, dict)
            else None
        )
        if isinstance(channel_meta, dict):
            channel_meta[_DEFER_ANSWER_TURN_SETTLEMENT_META_KEY] = True
        try:
            async for event in normalize_reasoning_boundary_stream(
                super().stream_query(request, **kwargs),
            ):
                if getattr(event, "object", None) == "response":
                    status = getattr(event, "status", None)
                    if status == RunStatus.Completed:
                        terminal_status = "completed"
                    elif status == RunStatus.Failed:
                        terminal_status = "failed"
                    elif status == RunStatus.Canceled:
                        terminal_status = "cancelled"

                trace_id = getattr(request, "trace_id", None)
                event = self._attach_trace_id_to_event(event, trace_id)
                progress = None
                if task_progress_enabled:
                    channel_meta = getattr(request, "channel_meta", None) or {}
                    chat_id = channel_meta.get("chat_id")
                    if not chat_id and self._chat_manager is not None:
                        chat_id = (
                            await self._chat_manager.get_chat_id_by_session(
                                getattr(request, "session_id", "") or "",
                                getattr(request, "channel", DEFAULT_CHANNEL),
                            )
                        )
                    if chat_id and self._task_tracker is not None:
                        progress = await self._task_tracker.get_task_progress(
                            chat_id,
                        )
                yield attach_task_progress(
                    event,
                    progress,
                    enabled=task_progress_enabled,
                )
        except asyncio.CancelledError:
            await self._settle_query_handler_outcome(identity, "cancelled")
            raise
        except Exception as exc:
            await self._settle_query_handler_outcome(identity, "failed", exc)
            raise
        finally:
            if isinstance(channel_meta, dict):
                if marker_was_present:
                    channel_meta[_DEFER_ANSWER_TURN_SETTLEMENT_META_KEY] = (
                        previous_defer_marker
                    )
                else:
                    channel_meta.pop(
                        _DEFER_ANSWER_TURN_SETTLEMENT_META_KEY,
                        None,
                    )
        if terminal_status is not None:
            await self._settle_query_handler_outcome(
                identity,
                terminal_status,
            )

    async def init_handler(self, *args, **kwargs):
        """
        Init handler.
        """
        # Load environment variables from .env file
        # env_path = Path(__file__).resolve().parents[4] / ".env"
        env_path = Path("./") / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.debug(f"Loaded environment variables from {env_path}")
        else:
            logger.debug(
                f".env file not found at {env_path}, "
                "using existing environment variables",
            )

        session_dir = str(
            (self.workspace_dir if self.workspace_dir else WORKING_DIR)
            / "sessions",
        )
        self.session = SafeJSONSession(save_dir=session_dir)

    async def shutdown_handler(self, *args, **kwargs):
        """
        Shutdown handler.
        """
