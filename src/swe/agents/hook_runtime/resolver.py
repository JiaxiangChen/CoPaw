# -*- coding: utf-8 -*-
"""解析 Hook 配置并生成事件级执行计划。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import (
    EffectiveHookHandler,
    EffectiveHookPlan,
    HookConfig,
    HookContext,
    HookEventName,
    HookHandlerConfig,
    HookMatcherGroupConfig,
    HookSessionOverlay,
    copy_handler_with_overrides,
    skill_hook_handler_definition,
    validate_handler_event,
)


@dataclass(frozen=True)
class _MatchedHandler:
    """保存已通过 group 匹配的 handler 及其来源 group。"""

    group_id: str
    handler: HookHandlerConfig
    source: str
    skill_definition: str | None = None


class HookResolver:
    """按租户、Agent 与会话技能配置解析 Hook 事件计划。"""

    def __init__(
        self,
        *,
        tenant_config: HookConfig | None = None,
        agent_config: HookConfig | None = None,
        session_overlay: HookSessionOverlay | None = None,
        now: datetime | None = None,
    ) -> None:
        self.tenant_config = tenant_config or HookConfig()
        self.agent_config = agent_config or HookConfig()
        self.session_overlay = session_overlay or HookSessionOverlay()
        self.now = now or datetime.now(timezone.utc)

    def resolve_event_plan(self, context: HookContext) -> EffectiveHookPlan:
        """解析当前事件需要执行的 handler，并保持配置来源顺序。"""

        config_sources = self._enabled_config_sources()
        if not config_sources:
            return self._build_plan(context, ())

        overlay_entries = self._active_overlay_entries(
            config for _, config in config_sources
        )
        matched_handlers = self._matching_handlers(
            context,
            config_sources,
            overlay_entries,
        )
        handlers = self._deduplicated_handlers(context, matched_handlers)
        return self._build_plan(context, handlers)

    def requires_stop_output_buffer(self, context: HookContext) -> bool:
        return bool(
            self.resolve_stop_transformer_plan(
                context,
                evaluate_if=False,
            ).handlers,
        )

    def resolve_stop_transformer_plan(
        self,
        context: HookContext,
        *,
        evaluate_if: bool = True,
    ) -> EffectiveHookPlan:
        if context.hook_event_name != HookEventName.STOP:
            return self._build_plan(context, ())
        config_sources = self._enabled_config_sources(
            sort_skill_sources=True,
        )
        overlay_entries = self._active_overlay_entries(
            config for _, config in config_sources
        )
        matched_handlers = self._matching_handlers(
            context,
            config_sources,
            overlay_entries,
            evaluate_if=evaluate_if,
            output_transform=True,
        )
        return self._build_plan(
            context,
            self._deduplicated_handlers(
                context,
                matched_handlers,
                include_source=True,
            ),
        )

    def resolve_stop_validator_plan(
        self,
        context: HookContext,
        *,
        evaluate_if: bool = True,
    ) -> EffectiveHookPlan:
        if context.hook_event_name != HookEventName.STOP:
            return self._build_plan(context, ())
        config_sources = self._enabled_config_sources()
        overlay_entries = self._active_overlay_entries(
            config for _, config in config_sources
        )
        matched_handlers = self._matching_handlers(
            context,
            config_sources,
            overlay_entries,
            evaluate_if=evaluate_if,
            output_transform=False,
        )
        return self._build_plan(
            context,
            self._deduplicated_handlers(context, matched_handlers),
        )

    def _enabled_configs(self) -> tuple[HookConfig, ...]:
        return tuple(config for _, config in self._enabled_config_sources())

    def _enabled_config_sources(
        self,
        *,
        sort_skill_sources: bool = False,
    ) -> tuple[tuple[str, HookConfig], ...]:
        loaded_skill_sources = self.session_overlay.loaded_skill_sources
        if sort_skill_sources:
            loaded_skill_sources = sorted(
                loaded_skill_sources,
                key=lambda source: source.skill_name,
            )
        loaded_skill_configs = tuple(
            (f"skill:{source.skill_name}", source.hook_config)
            for source in loaded_skill_sources
            if source.hook_config.enabled
        )
        return tuple(
            source
            for source in (
                ("tenant", self.tenant_config),
                ("agent", self.agent_config),
                *loaded_skill_configs,
            )
            if source[1].enabled
        )

    def _active_overlay_entries(
        self,
        configs: Iterable[HookConfig],
    ) -> dict[str, Any]:
        available_ids: set[str] = set()
        for config in configs:
            available_ids.update(config.handler_ids())
        return {
            entry.hook_id: entry
            for entry in self.session_overlay.entries
            if not entry.is_expired(self.now)
            and entry.hook_id in available_ids
        }

    def _matching_handlers(
        self,
        context: HookContext,
        config_sources: Iterable[tuple[str, HookConfig]],
        overlay_entries: dict[str, Any],
        *,
        evaluate_if: bool = True,
        output_transform: bool | None = None,
    ) -> list[_MatchedHandler]:
        matched_handlers: list[_MatchedHandler] = []
        event_name = _event_name_value(context.hook_event_name)

        for source, config in config_sources:
            groups = self._groups_for_event(config, context, event_name)
            for group_index, group in enumerate(groups):
                if not group.matcher.matches(context):
                    continue
                matched_handlers.extend(
                    self._matching_group_handlers(
                        group_index,
                        group,
                        context,
                        overlay_entries,
                        source=source,
                        event_name=event_name,
                        evaluate_if=evaluate_if,
                        output_transform=output_transform,
                    ),
                )
        return matched_handlers

    def _groups_for_event(
        self,
        config: HookConfig,
        context: HookContext,
        event_name: str,
    ) -> list[HookMatcherGroupConfig]:
        groups_by_event: dict[Any, list[HookMatcherGroupConfig]] = (
            config.events
        )
        return groups_by_event.get(
            context.hook_event_name,
            [],
        ) or groups_by_event.get(event_name, [])

    def _matching_group_handlers(
        self,
        group_index: int,
        group: HookMatcherGroupConfig,
        context: HookContext,
        overlay_entries: dict[str, Any],
        *,
        source: str,
        event_name: str,
        evaluate_if: bool,
        output_transform: bool | None,
    ) -> list[_MatchedHandler]:
        group_id = group.id or f"group-{group_index}"
        handlers: list[_MatchedHandler] = []
        for raw_handler in group.hooks:
            handler = self._apply_overlay(
                raw_handler,
                overlay_entries,
                context.hook_event_name,
            )
            if handler is None:
                continue
            if (
                output_transform is not None
                and handler.output_transform != output_transform
            ):
                continue
            if evaluate_if and not self._handler_can_run(handler, context):
                continue
            handlers.append(
                _MatchedHandler(
                    group_id,
                    handler,
                    source,
                    (
                        skill_hook_handler_definition(
                            event_name,
                            group,
                            raw_handler,
                        )
                        if source.startswith("skill:")
                        else None
                    ),
                ),
            )
        return handlers

    def _handler_can_run(
        self,
        handler: HookHandlerConfig,
        context: HookContext,
    ) -> bool:
        return self._matches_if(
            handler.if_condition,
            context,
        ) and not self._once_already_executed(handler, context)

    def _deduplicated_handlers(
        self,
        context: HookContext,
        matched_handlers: Iterable[_MatchedHandler],
        *,
        include_source: bool = False,
    ) -> tuple[EffectiveHookHandler, ...]:
        event_name = _event_name_value(context.hook_event_name)
        handlers: list[EffectiveHookHandler] = []
        seen: set[str] = set()

        for matched in matched_handlers:
            dedupe_key = self._dedupe_key(
                context.effective_tenant_id,
                event_name,
                matched.group_id,
                matched.handler,
            )
            if include_source:
                dedupe_key = f"{matched.source}:{dedupe_key}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            handlers.append(
                EffectiveHookHandler(
                    handler=matched.handler,
                    group_id=matched.group_id,
                    order=len(handlers),
                    dedupe_key=dedupe_key,
                    source=matched.source,
                    skill_definition=matched.skill_definition,
                ),
            )
        return tuple(handlers)

    @staticmethod
    def _build_plan(
        context: HookContext,
        handlers: Iterable[EffectiveHookHandler],
    ) -> EffectiveHookPlan:
        return EffectiveHookPlan(
            event_name=context.hook_event_name,
            context=context,
            handlers=tuple(handlers),
        )

    def _apply_overlay(
        self,
        handler: HookHandlerConfig,
        entries: dict[str, Any],
        event_name: HookEventName,
    ) -> HookHandlerConfig | None:
        entry = entries.get(handler.id)
        if entry is None:
            return handler
        if entry.enabled is False:
            return None
        if entry.overrides:
            updated_handler = copy_handler_with_overrides(
                handler,
                entry.overrides,
            )
            validate_handler_event(updated_handler, event_name)
            return updated_handler
        return handler

    def _once_already_executed(
        self,
        handler: HookHandlerConfig,
        context: HookContext,
    ) -> bool:
        if not handler.once:
            return False
        return bool(
            self.session_overlay.once_executed.get(
                once_key(
                    context.effective_tenant_id,
                    context.user_id,
                    context.session_id,
                    _event_name_value(context.hook_event_name),
                    handler.id,
                ),
            ),
        )

    @staticmethod
    def _dedupe_key(
        tenant_id: str,
        event_name: str,
        group_id: str,
        handler: HookHandlerConfig,
    ) -> str:
        return (
            f"{tenant_id}:{event_name}:{group_id}:"
            f"{handler.id}:{handler.type}:{handler.target_identity()}"
        )

    @staticmethod
    def _matches_if(expression: str, context: HookContext) -> bool:
        if not expression:
            return True
        values = context.to_handler_payload()
        try:
            parsed = ast.parse(expression, mode="eval")
            return bool(_eval_if_node(parsed.body, values))
        except Exception:
            return False


def once_key(
    effective_tenant_id: str,
    user_id: str,
    session_id: str,
    event_name: str,
    handler_id: str,
) -> str:
    return (
        f"{effective_tenant_id}:{user_id}:{session_id}:"
        f"{event_name}:{handler_id}"
    )


def _event_name_value(event_name: Any) -> str:
    return str(getattr(event_name, "value", event_name))


def _eval_if_node(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_if_node(node.operand, values)
    if isinstance(node, ast.BoolOp):
        return _eval_if_bool_op(node, values)
    if isinstance(node, ast.Compare):
        return _eval_if_compare(node, values)
    if isinstance(node, ast.Attribute):
        return _eval_if_attribute(node, values)
    if isinstance(node, ast.Subscript):
        return _eval_if_subscript(node, values)
    if isinstance(node, ast.List):
        return [_eval_if_node(item, values) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_if_node(item, values) for item in node.elts)
    raise ValueError("unsupported hook if expression")


def _eval_if_bool_op(node: ast.BoolOp, values: dict[str, Any]) -> bool:
    items = [_eval_if_node(value, values) for value in node.values]
    if isinstance(node.op, ast.And):
        return all(items)
    if isinstance(node.op, ast.Or):
        return any(items)
    raise ValueError("unsupported hook if expression")


def _eval_if_compare(node: ast.Compare, values: dict[str, Any]) -> bool:
    if len(node.ops) != 1 or len(node.comparators) != 1:
        raise ValueError("unsupported hook if expression")

    left = _eval_if_node(node.left, values)
    right = _eval_if_node(node.comparators[0], values)
    return _eval_if_compare_op(node.ops[0], left, right)


def _eval_if_compare_op(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right
    raise ValueError("unsupported hook if expression")


def _eval_if_attribute(node: ast.Attribute, values: dict[str, Any]) -> Any:
    base = _eval_if_node(node.value, values)
    if isinstance(base, dict):
        return base.get(node.attr)
    raise ValueError("unsupported hook if expression")


def _eval_if_subscript(node: ast.Subscript, values: dict[str, Any]) -> Any:
    base = _eval_if_node(node.value, values)
    key = _eval_if_node(node.slice, values)
    if isinstance(base, dict):
        return base.get(key)
    raise ValueError("unsupported hook if expression")
