# -*- coding: utf-8 -*-
"""Construct the dependencies installed while creating an ``SWEAgent``."""

from collections.abc import Callable
from typing import Any

from agentscope.memory import InMemoryMemory
from agentscope.tool import Toolkit

from .agent_runtime_components import (
    AgentRequestContext,
    AgentRuntimeComponents,
)
from .model_factory import create_model_and_formatter
from .mcp_tool_registrar import McpToolRegistrar


class AgentRuntimeBuilder:
    """Build runtime collaborators in their required installation order."""

    def __init__(
        self,
        *,
        toolkit_factory: Callable[[], Any],
        system_prompt_factory: Callable[[], str],
        model_and_formatter_factory: Callable[[], tuple[Any, Any]],
        memory_factory: Callable[[], Any],
    ) -> None:
        self._toolkit_factory = toolkit_factory
        self._system_prompt_factory = system_prompt_factory
        self._model_and_formatter_factory = model_and_formatter_factory
        self._memory_factory = memory_factory

    def build(self) -> AgentRuntimeComponents:
        """Build each component once, without calling the chat model."""
        toolkit = self._toolkit_factory()
        system_prompt = self._system_prompt_factory()
        model, formatter = self._model_and_formatter_factory()
        memory = self._memory_factory()
        return AgentRuntimeComponents(
            toolkit=toolkit,
            system_prompt=system_prompt,
            model=model,
            formatter=formatter,
            memory=memory,
        )

    @classmethod
    def build_for_swe_agent(
        cls,
        agent: Any,
        *,
        goal_finalization: bool,
        completion_judge: bool,
        namesake_strategy: str,
    ) -> AgentRuntimeComponents:
        """Construct the production collaborators from existing agent state.

        ``SWEAgent`` owns its policy helpers; this boundary owns their ordered
        assembly so no model request occurs while the agent's ReAct loop is
        installed.
        """

        def build_toolkit() -> Any:
            toolkit = (
                Toolkit()
                if goal_finalization
                else agent._create_toolkit(namesake_strategy=namesake_strategy)
            )
            if not goal_finalization and not completion_judge:
                agent._register_skills(toolkit)
                agent._register_source_tools(toolkit)
            return toolkit

        def build_model_and_formatter() -> tuple[Any, Any]:
            request_context = agent._request_context
            return create_model_and_formatter(
                agent_id=agent._agent_config.id,
                model_slot_override=agent._model_slot_override,
                model_provider_override=agent._model_provider_override,
                fallback_model_slot=agent._fallback_model_slot,
                fallback_model_provider=agent._fallback_model_provider,
                resolved_model_info=agent._resolved_model_slot,
                on_model_config_resolved=agent._apply_model_input_budget,
                on_model_provider_resolved=agent._capture_model_provider_snapshot,
                trace_context={
                    "trace_id": request_context.get("trace_id"),
                    "user_id": request_context.get("user_id"),
                    "session_id": request_context.get("session_id"),
                    "channel": request_context.get("channel"),
                    "source_id": request_context.get("source_id"),
                    "user_name": request_context.get("user_name"),
                    "bbk_id": request_context.get("bbk_id"),
                },
            )

        return cls(
            toolkit_factory=build_toolkit,
            system_prompt_factory=agent._build_sys_prompt,
            model_and_formatter_factory=build_model_and_formatter,
            memory_factory=InMemoryMemory,
        ).build()


__all__ = [
    "AgentRequestContext",
    "AgentRuntimeBuilder",
    "AgentRuntimeComponents",
    "McpToolRegistrar",
]
