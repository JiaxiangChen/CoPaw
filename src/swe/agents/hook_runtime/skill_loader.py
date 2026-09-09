# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    CommandHookHandlerConfig,
    HookConfig,
    HookMatcherGroupConfig,
    HookSessionState,
    HttpHookHandlerConfig,
    LoadedSkillHookSource,
    MonitoredSkillHookSource,
    SkillHookFileVersion,
    skill_hook_handler_definition,
)

logger = logging.getLogger(__name__)


ApprovedHttpUrls = Collection[str] | Callable[[str], bool] | None


class SkillHookLoadError(ValueError):
    """Raised when a skill-owned hook file fails validation."""

    session_state: HookSessionState | None = None


def load_skill_hooks_for_session(
    *,
    skill_name: str,
    skill_root: Path,
    workspace_dir: Path,
    session_state: HookSessionState,
    approved_http_urls: ApprovedHttpUrls = None,
) -> HookSessionState:
    """Load one skill's hooks/hooks.json into session hook state."""
    resolved_workspace = _resolve_existing_dir(workspace_dir, "workspace")
    resolved_skill_root = _resolve_existing_dir(skill_root, "skill root")
    _ensure_under(resolved_skill_root, resolved_workspace, "skill root")

    source_id = f"skill:{skill_name}"
    state = _with_legacy_monitors(session_state)
    if any(
        source.source_id == source_id
        for source in state.monitored_skill_sources
    ):
        return state

    hook_file = resolved_skill_root / "hooks" / "hooks.json"
    state = _with_monitored_source(
        state,
        MonitoredSkillHookSource(
            source_id=source_id,
            skill_name=skill_name,
            skill_root=str(resolved_skill_root),
            source_path=str(hook_file),
            file_version=_skill_hook_file_version(hook_file),
        ),
    )
    if not hook_file.is_file():
        return state

    try:
        source = _load_skill_hook_source(
            skill_name=skill_name,
            skill_root=resolved_skill_root,
            workspace_dir=resolved_workspace,
            approved_http_urls=approved_http_urls,
        )
    except SkillHookLoadError as exc:
        exc.session_state = state
        raise
    if source is None:
        return state
    return _replace_skill_source(state, skill_name, source)


def refresh_skill_hooks_for_session(
    *,
    workspace_dir: Path,
    session_state: HookSessionState,
    approved_http_urls: ApprovedHttpUrls = None,
) -> HookSessionState:
    """Refresh activated Skill Hooks when their hooks.json marker changed."""
    state = _with_legacy_monitors(session_state)
    for monitored in state.monitored_skill_sources:
        current_version = _skill_hook_file_version(
            Path(monitored.source_path),
        )
        if monitored.file_version == current_version:
            continue
        state = _refresh_monitored_skill_source(
            state=state,
            monitored=monitored,
            current_version=current_version,
            workspace_dir=workspace_dir,
            approved_http_urls=approved_http_urls,
        )
    return state


def _refresh_monitored_skill_source(
    *,
    state: HookSessionState,
    monitored: MonitoredSkillHookSource,
    current_version: SkillHookFileVersion,
    workspace_dir: Path,
    approved_http_urls: ApprovedHttpUrls,
) -> HookSessionState:
    source: LoadedSkillHookSource | None = None
    if current_version.exists:
        try:
            resolved_workspace = _resolve_existing_dir(
                workspace_dir,
                "workspace",
            )
            resolved_skill_root = _resolve_existing_dir(
                Path(monitored.skill_root),
                "skill root",
            )
            _ensure_under(
                resolved_skill_root,
                resolved_workspace,
                "skill root",
            )
            source = _load_skill_hook_source(
                skill_name=monitored.skill_name,
                skill_root=resolved_skill_root,
                workspace_dir=resolved_workspace,
                approved_http_urls=approved_http_urls,
            )
        except SkillHookLoadError as exc:
            logger.warning(
                "Withdrawing changed hooks for skill '%s': %s",
                monitored.skill_name,
                exc,
            )

    updated_monitor = monitored.model_copy(
        update={"file_version": current_version},
    )
    state = _replace_monitored_source(state, updated_monitor)
    return _replace_skill_source(state, monitored.skill_name, source)


def _load_skill_hook_source(
    *,
    skill_name: str,
    skill_root: Path,
    workspace_dir: Path,
    approved_http_urls: ApprovedHttpUrls,
) -> LoadedSkillHookSource | None:
    hook_file = skill_root / "hooks" / "hooks.json"
    hook_config = _read_hook_config(hook_file)
    if not hook_config.enabled:
        return None

    namespaced = _namespace_and_validate_config(
        hook_config,
        skill_name=skill_name,
        skill_root=skill_root,
        workspace_dir=workspace_dir,
        approved_http_urls=approved_http_urls,
    )
    return LoadedSkillHookSource(
        source_id=f"skill:{skill_name}",
        skill_name=skill_name,
        skill_root=str(skill_root),
        source_path=str(hook_file),
        hook_config=namespaced,
        loaded_at=datetime.now(timezone.utc),
        metadata={"format": "hooks.json"},
    )


def _with_legacy_monitors(state: HookSessionState) -> HookSessionState:
    monitored_names = {
        source.skill_name for source in state.monitored_skill_sources
    }
    legacy_monitors = [
        MonitoredSkillHookSource(
            source_id=source.source_id,
            skill_name=source.skill_name,
            skill_root=source.skill_root,
            source_path=source.source_path,
        )
        for source in state.loaded_skill_sources
        if source.skill_name not in monitored_names
    ]
    if not legacy_monitors:
        return state
    return _build_state(
        state,
        monitored_skill_sources=[
            *state.monitored_skill_sources,
            *legacy_monitors,
        ],
    )


def _with_monitored_source(
    state: HookSessionState,
    monitored: MonitoredSkillHookSource,
) -> HookSessionState:
    return _build_state(
        state,
        monitored_skill_sources=[
            *state.monitored_skill_sources,
            monitored,
        ],
    )


def _replace_monitored_source(
    state: HookSessionState,
    monitored: MonitoredSkillHookSource,
) -> HookSessionState:
    return _build_state(
        state,
        monitored_skill_sources=[
            monitored if source.skill_name == monitored.skill_name else source
            for source in state.monitored_skill_sources
        ],
    )


def _replace_skill_source(
    state: HookSessionState,
    skill_name: str,
    replacement: LoadedSkillHookSource | None,
) -> HookSessionState:
    previous = next(
        (
            source
            for source in state.loaded_skill_sources
            if source.skill_name == skill_name
        ),
        None,
    )
    loaded_sources = [
        source
        for source in state.loaded_skill_sources
        if source.skill_name != skill_name
    ]
    if replacement is not None:
        loaded_sources.append(replacement)

    valid_handler_ids = (
        replacement.handler_ids() if replacement is not None else set()
    )
    prefix = f"skill:{skill_name}:"
    entries = [
        entry
        for entry in state.entries
        if not entry.hook_id.startswith(prefix)
        or entry.hook_id in valid_handler_ids
    ]
    changed_handler_ids = _changed_handler_ids(previous, replacement)
    once_executed = {
        key: value
        for key, value in state.once_executed.items()
        if not any(
            key.endswith(f":{handler_id}")
            for handler_id in changed_handler_ids
        )
    }
    return _build_state(
        state,
        loaded_skill_sources=loaded_sources,
        entries=entries,
        once_executed=once_executed,
    )


def _changed_handler_ids(
    previous: LoadedSkillHookSource | None,
    replacement: LoadedSkillHookSource | None,
) -> set[str]:
    old_definitions = _handler_definitions(previous)
    new_definitions = _handler_definitions(replacement)
    return {
        handler_id
        for handler_id, definition in old_definitions.items()
        if new_definitions.get(handler_id) != definition
    }


def _handler_definitions(
    source: LoadedSkillHookSource | None,
) -> dict[str, str]:
    if source is None:
        return {}
    definitions: dict[str, str] = {}
    for event_name, groups in source.hook_config.events.items():
        for group in groups:
            for handler in group.hooks:
                definitions[handler.id] = skill_hook_handler_definition(
                    event_name,
                    group,
                    handler,
                )
    return definitions


def _build_state(
    state: HookSessionState,
    *,
    loaded_skill_sources: list[LoadedSkillHookSource] | None = None,
    monitored_skill_sources: list[MonitoredSkillHookSource] | None = None,
    entries: list[Any] | None = None,
    once_executed: dict[str, bool] | None = None,
) -> HookSessionState:
    return HookSessionState(
        loaded_skill_sources=(
            state.loaded_skill_sources
            if loaded_skill_sources is None
            else loaded_skill_sources
        ),
        monitored_skill_sources=(
            state.monitored_skill_sources
            if monitored_skill_sources is None
            else monitored_skill_sources
        ),
        entries=state.entries if entries is None else entries,
        once_executed=(
            state.once_executed if once_executed is None else once_executed
        ),
    )


def _skill_hook_file_version(hook_file: Path) -> SkillHookFileVersion:
    try:
        stat = hook_file.stat()
    except OSError:
        return SkillHookFileVersion(exists=False)
    if not hook_file.is_file():
        return SkillHookFileVersion(exists=False)
    return SkillHookFileVersion(
        exists=True,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        inode=stat.st_ino,
    )


def _read_hook_config(hook_file: Path) -> HookConfig:
    try:
        data = json.loads(hook_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillHookLoadError(f"invalid skill hooks JSON: {exc}") from exc
    except OSError as exc:
        raise SkillHookLoadError(
            f"failed to read skill hooks file: {exc}",
        ) from exc
    try:
        return HookConfig.model_validate(data)
    except ValidationError as exc:
        raise SkillHookLoadError(f"invalid skill hook config: {exc}") from exc


def _namespace_and_validate_config(
    hook_config: HookConfig,
    *,
    skill_name: str,
    skill_root: Path,
    workspace_dir: Path,
    approved_http_urls: ApprovedHttpUrls,
) -> HookConfig:
    namespace = f"skill:{skill_name}:"
    events: dict[Any, list[HookMatcherGroupConfig]] = {}
    for event_name, groups in hook_config.events.items():
        namespaced_groups: list[HookMatcherGroupConfig] = []
        for group_index, group in enumerate(groups):
            group_data = group.model_dump(mode="json", by_alias=True)
            original_group_id = group.id or f"group-{group_index}"
            group_data["id"] = _namespace_id(namespace, original_group_id)
            group_data["hooks"] = [
                _normalize_handler(
                    handler,
                    namespace=namespace,
                    skill_root=skill_root,
                    workspace_dir=workspace_dir,
                    approved_http_urls=approved_http_urls,
                )
                for handler in group.hooks
            ]
            namespaced_groups.append(
                HookMatcherGroupConfig.model_validate(group_data),
            )
        events[event_name] = namespaced_groups
    return HookConfig(enabled=True, events=events)


def _normalize_handler(
    handler: Any,
    *,
    namespace: str,
    skill_root: Path,
    workspace_dir: Path,
    approved_http_urls: ApprovedHttpUrls,
) -> dict[str, Any]:
    data = copy.deepcopy(handler.model_dump(mode="json", by_alias=True))
    data["id"] = _namespace_id(namespace, handler.id)
    if isinstance(handler, CommandHookHandlerConfig):
        _normalize_command_handler(data, skill_root, workspace_dir)
    elif isinstance(handler, HttpHookHandlerConfig):
        _validate_http_handler(data, approved_http_urls)
    return data


def _normalize_command_handler(
    data: dict[str, Any],
    skill_root: Path,
    workspace_dir: Path,
) -> None:
    if data.get("command"):
        raise SkillHookLoadError(
            "skill hook command handlers must not use shell command strings",
        )
    argv = data.get("argv") or []
    if not isinstance(argv, list) or not argv:
        raise SkillHookLoadError(
            "skill hook command handler requires an argv script argument",
        )
    if data.get("env"):
        raise SkillHookLoadError(
            "skill hook command handlers must not define literal env values",
        )

    script_index = _find_single_script_arg(argv, skill_root)
    script_path = _resolve_script_path(argv[script_index], skill_root)
    _ensure_under(script_path, skill_root / "scripts", "script path")
    _ensure_under(script_path, workspace_dir, "script path")
    if not script_path.exists():
        raise SkillHookLoadError("skill hook script path does not exist")
    if not script_path.is_file():
        raise SkillHookLoadError(
            "skill hook script path must be a regular file",
        )

    normalized_argv = list(argv)
    normalized_argv[script_index] = str(script_path)
    data["argv"] = normalized_argv

    cwd = data.get("cwd") or ""
    if cwd:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_absolute():
            cwd_path = skill_root / cwd_path
        try:
            resolved_cwd = cwd_path.resolve(strict=True)
        except OSError as exc:
            raise SkillHookLoadError("skill hook cwd does not exist") from exc
        if not resolved_cwd.is_dir():
            raise SkillHookLoadError("skill hook cwd must be a directory")
        _ensure_under(resolved_cwd, skill_root, "cwd")
        _ensure_under(resolved_cwd, workspace_dir, "cwd")
        data["cwd"] = str(resolved_cwd)
    else:
        data["cwd"] = str(skill_root)


def _find_single_script_arg(argv: list[str], skill_root: Path) -> int:
    scripts_root = skill_root / "scripts"
    candidates: list[int] = []
    for index, item in enumerate(argv):
        if not isinstance(item, str) or not _looks_like_path(item):
            continue
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = skill_root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(scripts_root)
        except ValueError as exc:
            raise SkillHookLoadError(
                "skill hook script path is outside skill scripts",
            ) from exc
        candidates.append(index)
    if not candidates:
        raise SkillHookLoadError(
            "skill hook command handler requires exactly one script argument",
        )
    if len(candidates) > 1:
        raise SkillHookLoadError(
            "skill hook command handler has multiple script arguments",
        )
    return candidates[0]


def _resolve_script_path(raw_path: str, skill_root: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = skill_root / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError:
        return candidate.resolve(strict=False)


def _validate_http_handler(
    data: dict[str, Any],
    approved_http_urls: ApprovedHttpUrls,
) -> None:
    if data.get("headers"):
        raise SkillHookLoadError(
            "skill hook HTTP handlers must not define literal headers",
        )
    if data.get("allowedEnvVars"):
        raise SkillHookLoadError(
            "skill hook HTTP handlers must not define allowedEnvVars",
        )
    url = str(data.get("url") or "")
    if not _is_http_url_approved(url, approved_http_urls):
        raise SkillHookLoadError(
            f"skill hook HTTP endpoint is not approved: {url}",
        )


def _is_http_url_approved(
    url: str,
    approved_http_urls: ApprovedHttpUrls,
) -> bool:
    if callable(approved_http_urls):
        return bool(approved_http_urls(url))
    # Skill-owned HTTP hooks are now allowed by default. Keep the callable
    # branch for custom validators used by internal callers and tests.
    return True


def _namespace_id(namespace: str, raw_id: str) -> str:
    if raw_id.startswith(namespace):
        return raw_id
    return f"{namespace}{raw_id}"


def _looks_like_path(value: str) -> bool:
    return (
        "/" in value
        or "\\" in value
        or value.startswith(".")
        or Path(value).expanduser().is_absolute()
    )


def _resolve_existing_dir(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SkillHookLoadError(f"{label} does not exist") from exc
    if not resolved.is_dir():
        raise SkillHookLoadError(f"{label} must be a directory")
    return resolved


def _ensure_under(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SkillHookLoadError(f"{label} is outside skill scripts") from exc
