# -*- coding: utf-8 -*-
"""Internal Background SubAgent worker entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Sequence

from ...config.config import AgentProfileConfig
from ...providers.models import ModelSlotConfig
from ..tenant_context import bind_tenant_context
from .launch_snapshot import (
    read_and_remove_private_mcp_snapshot,
    read_and_remove_private_model_snapshot,
    skill_tree_is_regular,
)
from .models import SubAgentRunRecord, WorkerLaunchSpec
from .run_store import PerRunSubAgentRunStore
from .runtime import SubAgentRuntime

_GENERIC_FAILURE_MESSAGE = "SubAgent execution failed."


def load_launch_spec(path: Path) -> WorkerLaunchSpec:
    """Load and validate a worker launch spec from disk."""
    return WorkerLaunchSpec.model_validate_json(
        Path(path).read_text(encoding="utf-8"),
    )


async def run_worker(launch_spec_path: Path) -> int:
    """Run one Background SubAgent and persist its terminal record."""
    try:
        launch_spec = load_launch_spec(launch_spec_path)
    except Exception:
        _cleanup_private_snapshots_from_launch_path(launch_spec_path)
        return 1
    store = PerRunSubAgentRunStore(Path(launch_spec.run_store_dir))
    record = await store.get(launch_spec.run_id)
    if record is None:
        _remove_private_snapshot_paths(launch_spec)
        return 1
    await store.mark_running(
        launch_spec.run_id,
        worker_pid=os.getpid(),
        stderr_log_path=launch_spec.stderr_log_path,
    )
    runtime_record = SubAgentRunRecord(
        run_id=record.run_id,
        status="queued",
        spec=launch_spec.delegation_spec,
        definition_name=launch_spec.definition.name,
        definition_version=launch_spec.definition.version,
        definition_source=launch_spec.definition.source,
        owner_scope=launch_spec.definition.owner_scope,
        effective_policy=launch_spec.effective_policy,
        nickname=record.nickname,
        start_request=record.start_request,
        definition_match=record.definition_match,
        created_at=record.created_at,
        started_at=record.started_at,
    )
    try:
        runtime = SubAgentRuntime(store=store)
        context = launch_spec.request_context
        workspace_dir = Path(launch_spec.workspace_dir)
        mcp_clients: list[Any] = []
        selected_slot, selected_provider, parent_slot, parent_provider = (
            _load_snapshotted_model(launch_spec)
        )
        if (
            launch_spec.definition.skill_owned is None
            and launch_spec.definition.agent_owned is None
        ):
            selected_slot = parent_slot
            selected_provider = parent_provider
        elif selected_slot is None or selected_provider is None:
            selected_slot = parent_slot
            selected_provider = parent_provider
        with bind_tenant_context(
            tenant_id=str(context.get("tenant_id") or "default"),
            user_id=context.get("user_id"),
            workspace_dir=workspace_dir,
            source_id=context.get("source_id"),
            scope_id=context.get("scope_id"),
        ):
            mcp_clients = await _connect_snapshotted_mcp_clients(
                launch_spec,
            )
            await store.record_connected_mcps(
                launch_spec.run_id,
                [
                    str(getattr(client, "_swe_subagent_mcp_key", ""))
                    for client in mcp_clients
                    if getattr(client, "_swe_subagent_mcp_key", "")
                ],
            )
            try:
                result = await runtime.run(
                    run=runtime_record,
                    definition=launch_spec.definition,
                    spec=launch_spec.delegation_spec,
                    parent_agent_config=AgentProfileConfig.model_validate(
                        launch_spec.parent_agent_config,
                    ),
                    workspace_dir=workspace_dir,
                    effective_policy=launch_spec.effective_policy,
                    request_context=context,
                    skill_snapshot_dirs=_validated_skill_snapshot_dirs(
                        launch_spec,
                    ),
                    mcp_clients=mcp_clients,
                    model_slot_override=selected_slot,
                    model_provider_override=selected_provider,
                    fallback_model_slot=parent_slot,
                    fallback_model_provider=parent_provider,
                )
            finally:
                await _cleanup_mcp_clients(mcp_clients)
        await store.finish(launch_spec.run_id, result)
        return 0
    except Exception:
        await store.fail(
            launch_spec.run_id,
            _GENERIC_FAILURE_MESSAGE,
            error_code="worker_runtime_error",
        )
        return 1
    finally:
        _remove_private_snapshot_paths(launch_spec)


async def _connect_snapshotted_mcp_clients(
    launch_spec: WorkerLaunchSpec,
) -> list[Any]:
    """Independently connect only the private MCP client snapshot."""
    payload = read_and_remove_private_mcp_snapshot(
        launch_spec.launch_snapshot.private_mcp_snapshot_path,
        run_store_dir=Path(launch_spec.run_store_dir),
        run_id=launch_spec.run_id,
    )
    if not payload:
        return []
    from ...app.runner.runner import _create_mcp_client_with_headers
    from ...config.config import MCPClientConfig

    context = launch_spec.request_context
    clients: list[Any] = []
    for client_key, raw_config in payload.items():
        client: Any | None = None
        try:
            config = MCPClientConfig.model_validate(raw_config)
            if not config.enabled:
                continue
            client = await _create_mcp_client_with_headers(
                config,
                session_id=context.get("session_id"),
                chat_id=context.get("chat_id"),
                trace_id=context.get("trace_id"),
            )
            if client is None:
                continue
            await asyncio.wait_for(client.connect(), timeout=30)
            setattr(client, "_swe_subagent_mcp_key", client_key)
            clients.append(client)
        except asyncio.CancelledError:
            await _close_mcp_client(client)
            raise
        except Exception:
            await _close_mcp_client(client)
            continue
    return clients


def _validated_skill_snapshot_dirs(launch_spec: WorkerLaunchSpec) -> list[str]:
    """Keep only copied Skill roots located under this run's snapshot root."""
    root = Path(launch_spec.run_store_dir) / f"{launch_spec.run_id}.skills"
    try:
        resolved_root = root.resolve()
    except OSError:
        return []
    valid: list[str] = []
    for raw_path in launch_spec.launch_snapshot.skill_snapshot_dirs:
        path = Path(raw_path)
        try:
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        if (
            not path.is_symlink()
            and not root.is_symlink()
            and resolved.parent == resolved_root
            and (resolved / "SKILL.md").is_file()
            and skill_tree_is_regular(resolved)
        ):
            valid.append(str(resolved))
    return valid


def _load_snapshotted_model(
    launch_spec: WorkerLaunchSpec,
) -> tuple[
    ModelSlotConfig | None,
    Any | None,
    ModelSlotConfig | None,
    Any | None,
]:
    """Build private providers without consulting mutable ProviderManager."""
    payload = read_and_remove_private_model_snapshot(
        launch_spec.launch_snapshot.private_model_snapshot_path,
        run_store_dir=Path(launch_spec.run_store_dir),
        run_id=launch_spec.run_id,
    )
    try:
        selected = payload["selected"]
        parent = payload["parent"]
        from ...providers.provider_manager import ProviderManager

        selected_slot = ModelSlotConfig.model_validate(selected["slot"])
        parent_slot = ModelSlotConfig.model_validate(parent["slot"])
        selected_provider = ProviderManager._provider_from_data(
            object.__new__(ProviderManager),
            selected["provider"],
        )
        parent_provider = ProviderManager._provider_from_data(
            object.__new__(ProviderManager),
            parent["provider"],
        )
        return selected_slot, selected_provider, parent_slot, parent_provider
    except Exception:
        return None, None, None, None


def _remove_private_snapshot_paths(launch_spec: WorkerLaunchSpec) -> None:
    for path, suffix in (
        (launch_spec.launch_snapshot.private_mcp_snapshot_path, "mcp.json"),
        (
            launch_spec.launch_snapshot.private_model_snapshot_path,
            "model.json",
        ),
    ):
        _remove_private_snapshot_path(
            path,
            Path(launch_spec.run_store_dir),
            launch_spec.run_id,
            suffix,
        )


def _remove_private_snapshot_path(
    path: str | None,
    run_store_dir: Path,
    run_id: str,
    suffix: str,
) -> None:
    if not path:
        return
    candidate = Path(path)
    try:
        if candidate.parent.resolve() != run_store_dir.resolve():
            return
    except OSError:
        return
    if candidate.name != f".{run_id}.{suffix}":
        return
    try:
        candidate.unlink()
    except OSError:
        pass


def _cleanup_private_snapshots_from_launch_path(launch_path: Path) -> None:
    """Best-effort cleanup for malformed launch JSON before validation."""
    try:
        raw = json.loads(launch_path.read_text(encoding="utf-8"))
        run_store_dir = Path(raw["run_store_dir"])
        run_id = str(raw["run_id"])
        snapshot = raw.get("launch_snapshot") or {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError, KeyError):
        return
    for field, suffix in (
        ("private_mcp_snapshot_path", "mcp.json"),
        ("private_model_snapshot_path", "model.json"),
    ):
        _remove_private_snapshot_path(
            snapshot.get(field),
            run_store_dir,
            run_id,
            suffix,
        )


async def _cleanup_mcp_clients(clients: list[Any]) -> None:
    """Close workers' private MCP sessions after the delegated run ends."""
    for client in clients:
        await _close_mcp_client(client)


async def _close_mcp_client(client: Any | None) -> None:
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `python -m swe.app.subagents.worker`."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-spec", required=True)
    args = parser.parse_args(argv)
    return asyncio.run(run_worker(Path(args.launch_spec)))


if __name__ == "__main__":
    raise SystemExit(main())
