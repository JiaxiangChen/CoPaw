# -*- coding: utf-8 -*-
"""Complexity budgets for high-risk agent control paths."""

from __future__ import annotations

import ast
import inspect
import textwrap

from swe.agents.hook_runtime.output import (
    _stop_append_text,
    normalize_stop_append_output,
)
from swe.agents.hook_runtime.runtime import HookRuntime
from swe.agents.skills_manager import (
    SkillPoolService,
    _require_workspace_layout_v2,
)
from swe.agents.tool_guard_mixin import ToolGuardMixin
from swe.agents.tools.subagent_background import (
    _create_selected_expert_wait_tool,
    _selected_expert_wait_run_id,
)
from swe.app.channels.base import BaseChannel
from swe.app.channels.zhaohu.channel import ZhaohuChannel
from swe.app.crons.executor import CronExecutor
from swe.app.crons.manager import CronManager
from swe.app.runner.expert_main_admission import (
    _validate_binding_revision,
    _validate_prepared_binding,
    admit_expert_main,
)
from swe.app.runner.runner import AgentRunner, _extract_assistant_response
from swe.app.routers.console import (
    _apply_expert_payload_metadata,
    _extract_session_and_payload,
)


def _cyclomatic_complexity(func) -> int:
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    decision_nodes = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.ExceptHandler,
        ast.BoolOp,
        ast.IfExp,
        ast.comprehension,
    )
    return 1 + sum(isinstance(node, decision_nodes) for node in ast.walk(tree))


def test_targeted_agent_paths_stay_within_complexity_budget() -> None:
    targets = (
        _require_workspace_layout_v2,
        SkillPoolService.download_to_workspace,
        ToolGuardMixin._acting_impl,
        ToolGuardMixin._selected_expert_follow_up,
        _extract_assistant_response,
        AgentRunner.query_handler,
        BaseChannel._has_sendable_content,
        ZhaohuChannel.send,
        CronExecutor._build_agent_request,
        CronExecutor._run_agent_stream,
        CronExecutor._deliver_persisted_agent_output,
        CronManager.run_job,
        CronManager._record_task_execution_success,
        CronManager._execute_once,
        ToolGuardMixin._acting_policy_denial,
        ToolGuardMixin._build_approval_extra,
        _extract_session_and_payload,
        AgentRunner._resolve_pending_approval,
        AgentRunner._prepare_query_agent_context,
        admit_expert_main,
        normalize_stop_append_output,
        HookRuntime.emit_stop_append,
        ToolGuardMixin._subagent_policy_denial,
        ToolGuardMixin._consume_subagent_tool_budget,
        ToolGuardMixin._approval_thinking_blocks,
        _apply_expert_payload_metadata,
        AgentRunner._external_approval_conflict_message,
        AgentRunner._discard_pending_expert_main_approval,
        AgentRunner._apply_expert_main_query_context,
        _validate_binding_revision,
        _validate_prepared_binding,
        _stop_append_text,
        HookRuntime._filter_completed_append_tasks,
        _create_selected_expert_wait_tool,
        _selected_expert_wait_run_id,
    )

    complexities = {
        target.__qualname__: _cyclomatic_complexity(target)
        for target in targets
    }
    assert max(complexities.values()) <= 15, complexities
