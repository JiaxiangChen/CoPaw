# -*- coding: utf-8 -*-
"""Immutable dependency snapshots for Background SubAgent workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
from collections.abc import Mapping
from typing import Any

from ...agents.skills_manager import (
    _build_signature,
    get_skill_freshness_token,
    resolve_effective_skill_dir,
)
from ...config.config import AgentProfileConfig, MCPClientConfig
from ...providers import ProviderManager
from ...providers.models import ModelSlotConfig
from .models import SubAgentDefinition, SubAgentLaunchDiagnostics


class ModelLaunchSnapshotError(RuntimeError):
    """The worker cannot safely launch without a complete model snapshot."""


def _capture_declared_skill_snapshots(
    *,
    metadata: Any,
    frozen_root: Path | None,
    snapshot_root: Path,
    workspace_dir: Path,
    effective_skill_names: list[str],
    skill_snapshot_signatures: Mapping[str, str] | None,
    skill_snapshot_dirs: Mapping[str, Path] | None,
) -> tuple[list[str], list[str], dict[str, str]]:
    loaded_skills: list[str] = []
    skipped_skills: list[str] = []
    freshness_tokens: dict[str, str] = {}
    if metadata is None:
        return loaded_skills, skipped_skills, freshness_tokens

    available_skills = set(effective_skill_names)
    for skill_name in metadata.declared_skills:
        source = _resolve_launch_skill_source(
            workspace_dir=workspace_dir,
            skill_name=skill_name,
            frozen_root=frozen_root,
            skill_snapshot_dirs=skill_snapshot_dirs,
        )
        if frozen_root is None and skill_name not in available_skills:
            skipped_skills.append(skill_name)
            continue
        target = snapshot_root / skill_name
        if (
            source is None
            or source.is_symlink()
            or not skill_tree_is_regular(source)
        ):
            if frozen_root is not None:
                raise OSError(
                    f"frozen expert dependency is missing: skill {skill_name}",
                )
            skipped_skills.append(skill_name)
            continue
        try:
            expected_signature = (skill_snapshot_signatures or {}).get(
                skill_name,
            )
            if (
                expected_signature is not None
                and _build_signature(source) != expected_signature
            ):
                skipped_skills.append(skill_name)
                continue
            _copy_skill_tree_no_symlinks(source, target)
            if (
                expected_signature is not None
                and _build_signature(target) != expected_signature
            ):
                _remove_snapshot_tree(target)
                skipped_skills.append(skill_name)
                continue
            loaded_skills.append(skill_name)
            freshness_tokens[skill_name] = get_skill_freshness_token(source)
        except OSError:
            skipped_skills.append(skill_name)
    return loaded_skills, skipped_skills, freshness_tokens


def _resolve_launch_skill_source(
    *,
    workspace_dir: Path,
    skill_name: str,
    frozen_root: Path | None,
    skill_snapshot_dirs: Mapping[str, Path] | None,
) -> Path | None:
    if frozen_root is not None:
        return frozen_root / "skills" / skill_name
    if skill_snapshot_dirs is not None:
        return skill_snapshot_dirs.get(skill_name)
    return resolve_effective_skill_dir(workspace_dir, skill_name)


def capture_launch_dependencies(
    *,
    run_store_dir: Path,
    run_id: str,
    workspace_dir: Path,
    parent_agent_config: AgentProfileConfig,
    definition: SubAgentDefinition,
    effective_skill_names: list[str],
    skill_snapshot_signatures: Mapping[str, str] | None = None,
    skill_snapshot_dirs: Mapping[str, Path] | None = None,
) -> tuple[list[str], str | None, SubAgentLaunchDiagnostics]:
    """Snapshot effective Skills and MCP configuration privately."""
    community = getattr(definition.agent_owned, "community", None)
    metadata = (
        definition.agent_owned
        if community is not None
        else definition.skill_owned or definition.agent_owned
    )
    frozen_root = (
        workspace_dir
        / "agents"
        / f"{definition.agent_owned.definition_id}.dependencies"
        if community is not None and definition.agent_owned is not None
        else None
    )
    snapshot_root = run_store_dir / f"{run_id}.skills"
    if (
        frozen_root is not None
        and not frozen_root.is_dir()
        and (
            metadata is not None
            and (metadata.declared_skills or metadata.declared_mcps)
        )
    ):
        raise OSError("frozen expert dependency directory is missing")
    loaded_skills, skipped_skills, freshness_tokens = (
        _capture_declared_skill_snapshots(
            metadata=metadata,
            frozen_root=frozen_root,
            snapshot_root=snapshot_root,
            workspace_dir=workspace_dir,
            effective_skill_names=effective_skill_names,
            skill_snapshot_signatures=skill_snapshot_signatures,
            skill_snapshot_dirs=skill_snapshot_dirs,
        )
    )
    try:
        if frozen_root is not None:
            mcp_payload, snapshotted_mcps, skipped_mcps = (
                _snapshot_frozen_mcps(
                    frozen_root,
                    (
                        (metadata.declared_mcps or [])
                        if metadata is not None
                        else None
                    ),
                )
            )
        else:
            mcp_payload, snapshotted_mcps, skipped_mcps = (
                _snapshot_declared_mcps(
                    parent_agent_config,
                    (
                        (metadata.declared_mcps or [])
                        if metadata is not None
                        else None
                    ),
                )
            )
        private_path = _write_private_mcp_snapshot(
            run_store_dir,
            run_id,
            mcp_payload,
        )
    except OSError:
        _remove_snapshot_tree(snapshot_root)
        raise
    return (
        [str(snapshot_root / name) for name in loaded_skills],
        private_path,
        _diagnostics(
            loaded_skills,
            skipped_skills,
            freshness_tokens,
            snapshotted_mcps,
            skipped_mcps,
        ),
    )


def _snapshot_declared_mcps(
    parent_agent_config: AgentProfileConfig,
    declared_mcps: list[str] | None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    payload: dict[str, Any] = {}
    snapshotted: list[str] = []
    skipped: list[str] = []
    clients = getattr(getattr(parent_agent_config, "mcp", None), "clients", {})
    names = (
        declared_mcps
        if declared_mcps is not None
        else sorted(clients) if isinstance(clients, dict) else []
    )
    for name in names:
        client = clients.get(name) if isinstance(clients, dict) else None
        if client is None or not client.enabled:
            skipped.append(name)
            continue
        payload[name] = client.model_dump(mode="json")
        snapshotted.append(name)
    return payload, snapshotted, skipped


def _snapshot_frozen_mcps(
    dependency_root: Path,
    declared_mcps: list[str] | None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    config_path = dependency_root / "mcp" / "config.json"
    if not config_path.is_file():
        if declared_mcps:
            raise OSError("frozen expert dependency is missing: MCP config")
        return {}, [], []
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError(
            "frozen expert dependency MCP config is unreadable",
        ) from exc
    if not isinstance(payload, dict):
        raise OSError("frozen expert dependency MCP config is invalid")
    names = declared_mcps if declared_mcps is not None else sorted(payload)
    missing = [name for name in names if name not in payload]
    if missing:
        raise OSError(
            "frozen expert dependency is missing: MCP " + ", ".join(missing),
        )
    normalized = {}
    for name in names:
        try:
            normalized[name] = MCPClientConfig.model_validate(
                payload[name],
            ).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise OSError(
                f"frozen expert dependency MCP is invalid: {name}",
            ) from exc
    return normalized, list(names), []


def capture_model_launch_snapshot(
    *,
    tenant_id: str,
    run_store_dir: Path,
    run_id: str,
    definition: SubAgentDefinition,
) -> tuple[str | None, ModelSlotConfig | None]:
    """Freeze the selected and parent provider configurations privately."""
    try:
        manager = ProviderManager.get_instance(tenant_id)
        parent_slot = _valid_model_slot(manager.get_active_model())
        if parent_slot is None:
            raise ModelLaunchSnapshotError("No active model is available")
        selected_slot = resolve_skill_owned_model_slot(tenant_id, definition)
        chosen_slot = selected_slot or parent_slot
        selected_provider = manager.get_provider(chosen_slot.provider_id)
        parent_provider = manager.get_provider(parent_slot.provider_id)
        if selected_provider is None or parent_provider is None:
            raise ModelLaunchSnapshotError(
                "Model snapshot provider is unavailable",
            )
        payload = {
            "selected": _model_snapshot_entry(chosen_slot, selected_provider),
            "parent": _model_snapshot_entry(parent_slot, parent_provider),
        }
        return (
            _write_private_snapshot(
                run_store_dir,
                run_id,
                "model.json",
                payload,
            ),
            chosen_slot,
        )
    except OSError:
        raise
    except ModelLaunchSnapshotError:
        raise
    except Exception as exc:
        raise ModelLaunchSnapshotError(
            "Unable to capture the worker model snapshot",
        ) from exc


def remove_launch_skill_snapshot(run_store_dir: Path, run_id: str) -> None:
    """Remove the copied Skill tree for a launch that never reaches worker."""
    _remove_snapshot_tree(run_store_dir / f"{run_id}.skills")


def read_and_remove_private_model_snapshot(
    path: str | None,
    *,
    run_store_dir: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Consume the one-shot frozen model/provider configuration."""
    return _read_and_remove_private_snapshot(
        path,
        run_store_dir=run_store_dir,
        run_id=run_id,
        suffix="model.json",
    )


def read_and_remove_private_mcp_snapshot(
    path: str | None,
    *,
    run_store_dir: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Read the one-shot private MCP snapshot and delete it immediately."""
    return _read_and_remove_private_snapshot(
        path,
        run_store_dir=run_store_dir,
        run_id=run_id,
        suffix="mcp.json",
    )


def resolve_skill_owned_model_slot(
    tenant_id: str,
    definition: SubAgentDefinition,
) -> ModelSlotConfig | None:
    """Resolve a validated model override for a configured Definition."""
    metadata = definition.skill_owned or definition.agent_owned
    reference = metadata.model if metadata is not None else None
    if reference is None:
        return None
    try:
        provider = ProviderManager.get_instance(tenant_id).get_provider(
            reference.provider,
        )
        if provider is None or not provider.has_model(reference.id):
            return None
    except Exception:
        return None
    return ModelSlotConfig(provider_id=reference.provider, model=reference.id)


def _write_private_mcp_snapshot(
    run_store_dir: Path,
    run_id: str,
    payload: dict[str, Any],
) -> str | None:
    if not payload:
        return None
    return _write_private_snapshot(run_store_dir, run_id, "mcp.json", payload)


def _write_private_snapshot(
    run_store_dir: Path,
    run_id: str,
    suffix: str,
    payload: dict[str, Any],
) -> str:
    path = run_store_dir / f".{run_id}.{suffix}"
    _ensure_private_run_store_dir(path.parent)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
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
    return str(path)


def _ensure_private_run_store_dir(path: Path) -> None:
    """Create a private run store without accepting a symlinked root."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("SubAgent run store is not a directory")
    finally:
        os.close(descriptor)
    os.chmod(path, 0o700)


def _read_and_remove_private_snapshot(
    path: str | None,
    *,
    run_store_dir: Path | None,
    run_id: str | None,
    suffix: str,
) -> dict[str, Any]:
    if not path:
        return {}
    snapshot_path = _validated_private_snapshot_path(
        path,
        run_store_dir=run_store_dir,
        run_id=run_id,
        suffix=suffix,
    )
    if snapshot_path is None:
        return {}
    try:
        descriptor = os.open(
            snapshot_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {}
            return json.load(stream)
    except (OSError, json.JSONDecodeError):
        return {}
    finally:
        try:
            snapshot_path.unlink()
        except OSError:
            pass


def _valid_model_slot(value: Any) -> ModelSlotConfig | None:
    try:
        slot = ModelSlotConfig.model_validate(value)
    except Exception:
        return None
    if not slot.provider_id or not slot.model:
        return None
    return slot


def _model_snapshot_entry(
    slot: ModelSlotConfig,
    provider: Any,
) -> dict[str, Any]:
    return {
        "slot": slot.model_dump(mode="json"),
        "provider": provider.model_dump(mode="json"),
    }


def _diagnostics(
    loaded_skills: list[str],
    skipped_skills: list[str],
    skill_freshness_tokens: dict[str, str],
    snapshotted_mcps: list[str],
    skipped_mcps: list[str],
) -> SubAgentLaunchDiagnostics:
    return SubAgentLaunchDiagnostics(
        loaded_skills=loaded_skills,
        skipped_skills=skipped_skills,
        skill_freshness_tokens=skill_freshness_tokens,
        snapshotted_mcps=snapshotted_mcps,
        skipped_mcps=skipped_mcps,
    )


def skill_tree_is_regular(root: Path) -> bool:
    """Reject package trees that contain symlinks before snapshot copying."""
    try:
        return not any(path.is_symlink() for path in root.rglob("*"))
    except OSError:
        return False


def _validated_private_snapshot_path(
    path: str,
    *,
    run_store_dir: Path | None,
    run_id: str | None,
    suffix: str,
) -> Path | None:
    """Accept only this run's direct, non-symlink private snapshot file."""
    snapshot_path = Path(path)
    if run_store_dir is None:
        return snapshot_path
    expected_root = Path(run_store_dir).resolve()
    try:
        parent = snapshot_path.parent.resolve()
    except OSError:
        return None
    if parent != expected_root:
        return None
    if run_id is not None and snapshot_path.name != f".{run_id}.{suffix}":
        return None
    if run_id is None and (
        not snapshot_path.name.startswith(".")
        or not snapshot_path.name.endswith(f".{suffix}")
    ):
        return None
    return snapshot_path


def _copy_skill_tree_no_symlinks(source: Path, target: Path) -> None:
    """Copy one package using descriptor-relative no-follow file opens.

    A preliminary package scan is helpful for diagnostics, but cannot close a
    check-to-copy race.  Every source entry is therefore opened with
    ``O_NOFOLLOW`` immediately before it is copied.
    """
    source_fd = os.open(
        source,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(target)
        target_fd = os.open(
            target,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            _copy_skill_directory(source_fd, target_fd)
        finally:
            os.close(target_fd)
    except BaseException:
        _remove_snapshot_tree(target)
        raise
    finally:
        os.close(source_fd)


def _copy_skill_directory(source_fd: int, target_fd: int) -> None:
    for entry in os.scandir(source_fd):
        name = entry.name
        if entry.is_symlink():
            raise OSError(f"Skill snapshot rejects symlink: {name}")
        if entry.is_dir(follow_symlinks=False):
            child_source_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=source_fd,
            )
            try:
                if not stat.S_ISDIR(os.fstat(child_source_fd).st_mode):
                    raise OSError(
                        f"Skill snapshot rejects non-directory: {name}",
                    )
                os.mkdir(name, dir_fd=target_fd)
                child_target_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=target_fd,
                )
                try:
                    _copy_skill_directory(child_source_fd, child_target_fd)
                finally:
                    os.close(child_target_fd)
            finally:
                os.close(child_source_fd)
            continue
        if not entry.is_file(follow_symlinks=False):
            raise OSError(f"Skill snapshot rejects special file: {name}")
        source_file_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(source_file_fd).st_mode):
                raise OSError(
                    f"Skill snapshot rejects non-regular file: {name}",
                )
            target_file_fd = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=target_fd,
            )
            try:
                while chunk := os.read(source_file_fd, 1024 * 1024):
                    os.write(target_file_fd, chunk)
            finally:
                os.close(target_file_fd)
        finally:
            os.close(source_file_fd)


def _remove_snapshot_tree(path: Path) -> None:
    """Best-effort cleanup that never traverses a substituted symlink root."""
    try:
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    except OSError:
        pass
