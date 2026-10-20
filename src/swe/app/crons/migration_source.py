# -*- coding: utf-8 -*-
"""Read legacy Cron snapshots without bootstrapping or modifying a workspace."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, TypeGuard

from ...config.context import is_valid_identity_value
from ...config.scope_conversion import (
    decode_canonical_scope_id,
    encode_canonical_scope_id,
)
from .migration_models import (
    MigrationRequest,
    SnapshotCandidate,
    SourceItem,
    SourcePage,
)
from .models import CronJobSpec

PROVENANCE_KEY = "_snapshot_migration"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES_PER_PAGE = 100
MAX_SOURCE_FILES = 20000
SYSTEM_TYPES = {
    "heartbeat",
    "dream",
    "cleanup",
    "task_session_cleanup",
    "archive_maintenance",
    "source_archive_maintenance",
}
SYSTEM_IDS = {
    "_heartbeat",
    "_dream",
    "_source_task_session_cleanup",
    "_source_archive_maintenance",
}
SECRET_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "api-key",
    "apikey",
    "access-token",
    "accesstoken",
    "refresh-token",
    "refreshtoken",
    "client-secret",
    "secret-key",
    "x-internal-token",
    "token",
    "secret",
    "private-key",
    "client-key",
}
META_STATE = {
    "task_chat_id": "task_chat_id",
    "task_session_id": "task_session_id",
    "external_job_id": "ordinary_external_job_id",
    "batch_dispatch_external_job_id": "batch_external_job_id",
    "task_unread_execution_count": "unread_execution_count",
    "task_has_scheduled_result": "has_scheduled_result",
    "task_last_scheduled_preview": "latest_scheduled_preview",
    "pause_reason": "pause_reason",
}


class MigrationSourceError(ValueError):
    """Safe diagnostic for invalid continuation or unavailable source storage."""


@dataclass(frozen=True)
class _SourceFile:
    path: Path
    key: str
    storage_id: str
    agent_id: str
    issue: str | None = None
    config_signature: tuple[int, int] | None = None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _profile_ids(tenant: Path) -> dict[Path, str]:
    """Read profile references only, without loading or repairing configs."""
    config = tenant / "config.json"
    if not config.exists():
        return {}
    if not _inside(config, tenant):
        raise ValueError("unsafe_config")
    with config.open("rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("config_too_large")
    data = json.loads(raw.decode("utf-8-sig"))
    profiles = data.get("agents", {}).get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("invalid_profiles")
    mapped = {}
    for agent_id, profile in profiles.items():
        if (
            not isinstance(profile, dict)
            or profile.get("id", agent_id) != agent_id
        ):
            raise ValueError("invalid_profile_identity")
        workspace = _profile_workspace(profile.get("workspace_dir"), tenant)
        if workspace in mapped and mapped[workspace] != agent_id:
            raise ValueError("ambiguous_profile_identity")
        mapped[workspace] = agent_id
    return mapped


def _profile_workspace(value: Any, tenant: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_workspace")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = tenant / path
    if not path.exists():
        from ...config.utils import _normalize_working_dir_bound_paths

        normalized = _normalize_working_dir_bound_paths(
            {"workspace_dir": value},
        )
        path = Path(normalized["workspace_dir"]).expanduser()
        if not path.is_absolute():
            path = tenant / path
    if not _inside(path, tenant / "workspaces"):
        raise ValueError("unsafe_profile_workspace")
    return path.resolve()


def _workspace_sources(tenant: Path, root: Path) -> list[_SourceFile]:
    workspaces = tenant / "workspaces"
    if not workspaces.is_dir():
        return []
    if not _inside(workspaces, root):
        return [
            _SourceFile(
                workspaces,
                workspaces.relative_to(root).as_posix(),
                tenant.name,
                "default",
            ),
        ]
    sources = []
    config_signature = _config_signature(tenant)
    for agent in sorted(workspaces.iterdir()):
        if agent.name.startswith(".") or not agent.is_dir():
            continue
        path = agent / "jobs.json"
        if path.exists():
            sources.append(
                _SourceFile(
                    path,
                    path.relative_to(root).as_posix(),
                    tenant.name,
                    agent.name,
                    None,
                    config_signature,
                ),
            )
    return sources


def _config_signature(tenant: Path) -> tuple[int, int] | None:
    try:
        stat = (tenant / "config.json").stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _resolved_profile(source: _SourceFile, root: Path) -> _SourceFile:
    if not source.storage_id or source.issue:
        return source
    try:
        mapping = _profile_ids(root / source.storage_id)
        agent_id = mapping.get(source.path.parent.resolve(), source.agent_id)
        return replace(source, agent_id=agent_id)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return replace(source, issue="invalid_agent_config")


def _discover(root: Path) -> list[_SourceFile]:
    if not root.is_dir():
        raise MigrationSourceError("source_storage_unavailable")
    files: list[_SourceFile] = []
    if (root / "jobs.json").exists():
        files.append(
            _SourceFile(root / "jobs.json", "jobs.json", "", "default"),
        )
    for tenant in sorted(root.iterdir()):
        if (
            tenant.name.startswith(".")
            or tenant.name == "default"
            or tenant.name.startswith("default_")
        ):
            continue
        if not tenant.is_dir():
            continue
        # Do not enumerate a directory that resolves outside the service root.
        if tenant.is_symlink() or not _inside(tenant, root):
            files.append(
                _SourceFile(
                    tenant,
                    tenant.name,
                    tenant.name,
                    "default",
                    "invalid_or_unsafe_file",
                ),
            )
            continue
        legacy = tenant / "jobs.json"
        if legacy.exists():
            files.append(
                _SourceFile(
                    legacy,
                    legacy.relative_to(root).as_posix(),
                    tenant.name,
                    "default",
                    None,
                    _config_signature(tenant),
                ),
            )
        files.extend(_workspace_sources(tenant, root))
        if len(files) > MAX_SOURCE_FILES:
            raise MigrationSourceError("source_inventory_too_large")
    return sorted(files, key=lambda f: f.key)


def _manifest(files: list[_SourceFile]) -> str:
    return _hash(
        canonical_json(
            [(f.key, f.agent_id, f.issue, f.config_signature) for f in files],
        ),
    )


def capture_loaded_states(
    manager: Any,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Copy actual state entries on the event loop; never call get/start methods."""
    snapshots = {}
    for key, workspace in list(getattr(manager, "agents", {}).items()):
        cron = getattr(workspace, "cron_manager", None)
        states = getattr(cron, "_states", None)
        if not isinstance(states, dict):
            continue
        snapshots[key] = {
            job_id: copy.deepcopy(
                (
                    state.model_dump(exclude_unset=True)
                    if hasattr(state, "model_dump")
                    else state
                ),
            )
            for job_id, state in list(states.items())
            if job_id not in SYSTEM_IDS
            and (hasattr(state, "model_dump") or isinstance(state, dict))
        }
    return snapshots


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _redact(v) for k, v in value.items() if not _secret_field(k)
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _secret_field(key: str) -> bool:
    key = key.lower().replace("_", "-")
    return key in SECRET_KEYS or key.endswith(
        ("-token", "-password", "-secret", "-api-key", "-authorization"),
    )


def _identity(
    raw: dict[str, Any],
    source: _SourceFile,
) -> tuple[str, str, str]:
    tenant, origin = _resolve_business_identity(raw, source.storage_id)
    _validate_identity_values((tenant, origin, source.agent_id, raw.get("id")))
    scope = encode_canonical_scope_id(tenant, origin).scope_id
    if source.storage_id not in ("", tenant, scope, f"scope.v1.{scope}"):
        raise ValueError("conflicting_storage_identity")
    if raw.get("scope_id") and raw["scope_id"] not in (
        scope,
        f"scope.v1.{scope}",
    ):
        raise ValueError("conflicting_scope")
    if len(scope) > 512:
        raise ValueError("scope_too_long")
    return tenant, origin, scope


def _resolve_business_identity(
    raw: dict[str, Any],
    storage_id: str,
) -> tuple[str, str]:
    tenant, origin = raw.get("tenant_id"), raw.get("source_id")
    if not tenant or not origin:
        stored_scope = raw.get("scope_id") or storage_id
        if isinstance(stored_scope, str):
            stored_scope = stored_scope.removeprefix("scope.v1.")
        decoded = decode_canonical_scope_id(stored_scope)
        if (tenant and tenant != decoded.tenant_id) or (
            origin and origin != decoded.source_id
        ):
            raise ValueError("conflicting_identity")
        tenant, origin = (
            tenant or decoded.tenant_id,
            origin or decoded.source_id,
        )
    return tenant, origin


def _validate_identity_values(values: tuple) -> None:
    for value in values:
        if (
            not isinstance(value, str)
            or len(value) > 128
            or value != value.strip()
            or not is_valid_identity_value(value)
        ):
            raise ValueError("invalid_identity")


def _utc(value: Any, field: str, warnings: list[str]) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            raise ValueError("missing_timezone")
        normalized = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        if normalized.year < 1000:
            raise ValueError("outside_mysql_datetime_range")
        return normalized
    except (TypeError, ValueError, OverflowError):
        warnings.append(f"invalid_time:{field}")
        return None


def _meta_state(meta: dict, warnings: list[str]) -> dict:
    patch = {
        target: meta[key]
        for key, target in META_STATE.items()
        if key in meta and meta[key] is not None
    }
    for key, target in (
        ("task_last_scheduled_run_at", "last_scheduled_run_at"),
        ("auto_paused_at", "auto_paused_at"),
    ):
        if key in meta and meta[key] is not None:
            value = _utc(meta[key], target, warnings)
            if value is not None:
                patch[target] = value
    if "unread_execution_count" in patch:
        value = patch["unread_execution_count"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 9223372036854775807
        ):
            warnings.append("invalid_state:unread_execution_count")
            patch.pop("unread_execution_count")
    if "has_scheduled_result" in patch and patch[
        "has_scheduled_result"
    ] not in (True, False, 0, 1):
        warnings.append("invalid_state:has_scheduled_result")
        patch.pop("has_scheduled_result")
    return patch


def _validated_state(patch: dict, warnings: list[str]) -> dict:
    for key, maximum in (
        ("task_chat_id", 128),
        ("task_session_id", 128),
        ("ordinary_external_job_id", 128),
        ("batch_external_job_id", 128),
        ("pause_reason", 64),
        ("last_error", None),
        ("latest_scheduled_preview", None),
    ):
        if key in patch and (
            not isinstance(patch[key], str)
            or (maximum is not None and len(patch[key]) > maximum)
        ):
            if patch[key] is None and key == "last_error":
                continue
            patch.pop(key)
            warnings.append(f"invalid_state:{key}")
    return patch


def _state_patch(meta: dict, memory: dict | None, warnings: list[str]) -> dict:
    patch = _meta_state(meta, warnings)
    if not memory:
        return _validated_state(patch, warnings)
    for key in ("last_status", "last_error"):
        if key in memory:
            patch[key] = memory[key]
    if patch.get("last_status") is None:
        patch.pop("last_status", None)
    elif not isinstance(patch["last_status"], str) or patch[
        "last_status"
    ] not in {
        None,
        "success",
        "error",
        "running",
        "skipped",
        "cancelled",
        "timeout",
    }:
        warnings.append("invalid_state:last_status")
        patch.pop("last_status")
    for key in ("last_run_at", "last_prefetch_at", "next_run_at"):
        if memory.get(key) is not None:
            value = _utc(memory[key], key, warnings)
            if value is not None:
                patch[key] = value
    if "next_run_times" in memory:
        times = _next_times(memory["next_run_times"], warnings)
        if times is not None:
            patch["next_run_times_json"] = times
    ext = memory.get("external_job_id")
    if ext:
        if patch.get("ordinary_external_job_id") not in (None, ext):
            warnings.append("binding_conflict:ordinary_external_job_id")
            patch.pop("ordinary_external_job_id", None)
        else:
            patch["ordinary_external_job_id"] = ext
    return _validated_state(patch, warnings)


def _next_times(raw: Any, warnings: list[str]) -> list[str] | None:
    if not isinstance(raw, list):
        warnings.append("invalid_state:next_run_times")
        return None
    values = [_utc(value, "next_run_times", warnings) for value in raw]
    if any(value is None for value in values):
        return None
    return [value.isoformat() + "Z" for value in values]


def _duplicate_ids(jobs: list) -> set[str]:
    counts = Counter(
        raw["id"]
        for raw in jobs
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    )
    return {key for key, count in counts.items() if count > 1}


def _candidate(
    raw: dict,
    source: _SourceFile,
    snapshot: dict,
    loaded: dict,
    fingerprint: str,
    observed: datetime,
) -> SnapshotCandidate:
    if PROVENANCE_KEY in raw:
        raise ValueError("reserved_snapshot_field")
    tenant, origin, scope = _identity(raw, source)
    clean = _redact(raw)
    clean.update(tenant_id=tenant, source_id=origin, scope_id=scope)
    model = CronJobSpec.model_validate(clean)
    spec = {**clean, **model.model_dump(mode="json")}
    meta = model.meta
    warnings: list[str] = []
    memory = loaded.get(f"{source.storage_id}:{source.agent_id}", {}).get(
        model.id,
    )
    if memory is None:
        memory = loaded.get(f"{scope}:{source.agent_id}", {}).get(model.id)
    available = memory is not None
    if not available:
        warnings.append("memory_state_missing")
    state = _state_patch(meta, memory, warnings)
    version = _file_version(snapshot)
    definition = _snapshot_definition(model, source, spec, version, warnings)
    if not meta.get("broadcast_dispatch_intents_enabled"):
        state["dispatch_state"] = "ordinary"
    elif not meta.get("broadcast_source_job_id"):
        warnings.append("batch_control_unknown")
    state["state_source"] = (
        "memory_snapshot"
        if available
        else ("file_meta" if state else "unknown")
    )
    _validate_definition(definition)
    return SnapshotCandidate(
        definition,
        state,
        observed,
        source.key,
        fingerprint,
        version,
        available,
        warnings,
    )


def _file_version(snapshot: dict) -> int:
    version = snapshot.get("definition_version", 0)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 0 <= version <= 9223372036854775807
    ):
        raise ValueError("invalid_file_version")
    return version


def _snapshot_definition(
    model: CronJobSpec,
    source: _SourceFile,
    spec: dict,
    version: int,
    warnings: list[str],
) -> dict:
    meta = model.meta
    original = meta.get("broadcast_original_cron") or meta.get(
        "batch_dispatch_parent_cron",
    )
    if not original and meta.get("broadcast_offset_minutes"):
        warnings.append("original_cron_unknown")
    definition = {
        "tenant_id": spec["tenant_id"],
        "source_id": spec["source_id"],
        "agent_id": source.agent_id,
        "job_id": model.id,
        "scope_id": spec["scope_id"],
        "name": model.name,
        "tenant_name": model.tenant_name or "",
        "bbk_id": model.bbk_id or "",
        "skill_ids": model.skill_ids,
        "workflow_binding_id": model.workflow_binding_id,
        "task_type": model.task_type,
        "enabled": model.enabled,
        "cron_expr": model.schedule.cron,
        "original_cron_expr": original or model.schedule.cron,
        "timezone": model.schedule.timezone,
        "spec_json": spec,
        "management_owner": "legacy",
        "job_origin": str(meta.get("job_origin") or "manual"),
        "creator_user_id": str(meta.get("creator_user_id") or ""),
        "plan_id": model.plan_id,
        "source_definition_version": version,
    }
    _relations_and_offsets(definition, meta, warnings)
    return definition


def _validate_definition(definition: dict) -> None:
    for key, maximum in (
        ("name", 255),
        ("tenant_name", 255),
        ("bbk_id", 128),
        ("skill_ids", 200),
        ("workflow_binding_id", 128),
        ("cron_expr", 128),
        ("original_cron_expr", 128),
        ("timezone", 64),
        ("job_origin", 32),
        ("creator_user_id", 128),
        ("plan_id", 255),
        ("parent_job_id", 128),
        ("parent_tenant_id", 128),
        ("parent_source_id", 128),
        ("parent_agent_id", 128),
    ):
        if definition.get(key) is not None and (
            not isinstance(definition[key], str)
            or len(definition[key]) > maximum
        ):
            raise ValueError("definition_field_out_of_range")


def _relations_and_offsets(
    definition: dict,
    meta: dict,
    warnings: list[str],
) -> None:
    if meta.get("broadcast_source_job_id"):
        definition["parent_job_id"] = meta["broadcast_source_job_id"]
        for key in ("tenant_id", "source_id", "agent_id"):
            value = meta.get(f"broadcast_source_{key}")
            if value:
                definition[f"parent_{key}"] = value
        if not all(
            f"parent_{k}" in definition
            for k in ("tenant_id", "source_id", "agent_id")
        ):
            warnings.append("parent_identity_incomplete")
    for source_key, target in (
        ("broadcast_offset_minutes", "offset_minutes"),
        ("batch_dispatch_offset_window_hours", "offset_window_hours"),
    ):
        if source_key in meta:
            value = meta[source_key]
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 2147483647
            ):
                definition[target] = value
            else:
                warnings.append(f"invalid_offset:{source_key}")
    if definition.get("offset_minutes"):
        definition["enable_offset"] = True


def _matches(candidate: SnapshotCandidate, request: MigrationRequest) -> bool:
    return all(
        values is None or candidate.definition[key] in values
        for key, values in (
            ("tenant_id", request.tenant_ids),
            ("source_id", request.source_ids),
            ("agent_id", request.agent_ids),
            ("job_id", request.job_ids),
        )
    )


def _cursor(payload: dict) -> str:
    data = canonical_json(payload)
    envelope = {"data": payload, "checksum": _hash(data)}
    return base64.urlsafe_b64encode(canonical_json(envelope).encode()).decode()


def _position(
    request: MigrationRequest,
    files: list[_SourceFile],
) -> tuple[int, int, str | None]:
    if not request.cursor:
        return 0, 0, None
    try:
        envelope = json.loads(
            base64.b64decode(request.cursor, altchars=b"-_", validate=True),
        )
        data = envelope["data"]
        if envelope["checksum"] != _hash(canonical_json(data)):
            raise ValueError("checksum")
        filters = request.model_dump(exclude={"cursor", "limit"})
        if data["version"] != 1 or data["filters"] != _hash(
            canonical_json(filters),
        ):
            raise ValueError("filters")
        if data["manifest"] != _manifest(files):
            raise ValueError("manifest")
        index = next(i for i, f in enumerate(files) if f.key == data["file"])
        if type(data["offset"]) is not int or data["offset"] < 0:
            raise ValueError("offset")
        return index, data["offset"], data["fingerprint"]
    except (
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        UnicodeDecodeError,
    ) as exc:
        raise MigrationSourceError("invalid_or_changed_cursor") from exc


def _read_file(source: _SourceFile, root: Path) -> tuple[dict, str]:
    if source.issue:
        raise ValueError(source.issue)
    current = root
    for component in source.path.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("unsafe_source_path")
    if not _inside(source.path, root) or not source.path.is_file():
        raise ValueError("unsafe_source_path")
    with source.path.open("rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("source_file_too_large")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("invalid_jobs_file")
    return data, hashlib.sha256(raw).hexdigest()


def _source_item(
    raw: Any,
    source: _SourceFile,
    data: dict,
    loaded: dict,
    fingerprint: str,
    observed: datetime,
    request: MigrationRequest,
) -> SourceItem | None:
    if not isinstance(raw, dict):
        return SourceItem(source.key, reason="invalid_job")
    value = raw.get("id")
    job_id = (
        value
        if isinstance(value, str)
        and len(value) <= 128
        and is_valid_identity_value(value)
        else None
    )
    if raw.get("task_type") in SYSTEM_TYPES or job_id in SYSTEM_IDS:
        return SourceItem(source.key, job_id, reason="independent_system_task")
    try:
        candidate = _candidate(
            raw,
            source,
            data,
            loaded,
            fingerprint,
            observed,
        )
        return (
            SourceItem(source.key, job_id, candidate)
            if _matches(candidate, request)
            else None
        )
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return SourceItem(source.key, job_id, reason="invalid_job")


def _is_duplicate_job(raw: Any, duplicates: set[str]) -> bool:
    return (
        isinstance(raw, dict)
        and isinstance(raw.get("id"), str)
        and raw["id"] in duplicates
    )


def read_source_page(
    root: Path,
    request: MigrationRequest,
    loaded_states: dict,
    observed_at: datetime | None = None,
) -> SourcePage:
    """Inspect a bounded page. Cursor checks detect changes, not authorize access."""
    root = root.expanduser().resolve()
    observed_at = observed_at or datetime.now(timezone.utc).replace(
        tzinfo=None,
    )
    try:
        files = _discover(root)
    except OSError as exc:
        raise MigrationSourceError("source_storage_unavailable") from exc
    file_index, offset, expected_fingerprint = _position(request, files)
    page = SourcePage()
    inspected = 0
    while (
        file_index < len(files)
        and inspected < request.limit
        and page.scanned_files < MAX_FILES_PER_PAGE
    ):
        source = _resolved_profile(files[file_index], root)
        page.scanned_files += 1
        try:
            data, fingerprint = _read_file(source, root)
        except (OSError, ValueError, UnicodeError, RecursionError):
            page.items.append(
                SourceItem(
                    source.key,
                    reason=source.issue or "invalid_or_unsafe_file",
                ),
            )
            file_index, offset, expected_fingerprint = file_index + 1, 0, None
            inspected += 1
            continue
        if expected_fingerprint and fingerprint != expected_fingerprint:
            raise MigrationSourceError("source_changed_restart_required")
        jobs = data["jobs"]
        duplicates = _duplicate_ids(jobs)
        if offset > len(jobs):
            raise MigrationSourceError("invalid_or_changed_cursor")
        while offset < len(jobs) and inspected < request.limit:
            raw = jobs[offset]
            offset += 1
            inspected += 1
            page.scanned_jobs += 1
            if _is_duplicate_job(raw, duplicates):
                page.items.append(
                    SourceItem(source.key, reason="duplicate_job_identity"),
                )
                continue
            item = _source_item(
                raw,
                source,
                data,
                loaded_states,
                fingerprint,
                observed_at,
                request,
            )
            if item is not None:
                page.items.append(item)
        if offset == len(jobs):
            file_index, offset, expected_fingerprint = file_index + 1, 0, None
        else:
            expected_fingerprint = fingerprint
    if file_index < len(files):
        page.next_cursor = _cursor(
            {
                "version": 1,
                "filters": _hash(
                    canonical_json(
                        request.model_dump(exclude={"cursor", "limit"}),
                    ),
                ),
                "manifest": _manifest(files),
                "file": files[file_index].key,
                "offset": offset,
                "fingerprint": expected_fingerprint,
            },
        )
    return page


def inventory_fingerprint(root: Path) -> str:
    return _manifest(_discover(root.expanduser().resolve()))


def database_cursor(
    manifest: str,
    request: MigrationRequest,
    after: tuple | None,
) -> str:
    """Separate continuation for read-only checks of database-only snapshots."""
    return "db:" + _cursor(
        {
            "version": 1,
            "kind": "database",
            "manifest": manifest,
            "filters": _hash(
                canonical_json(
                    request.model_dump(exclude={"cursor", "limit"}),
                ),
            ),
            "after": after,
        },
    )


def database_position(
    root: Path,
    request: MigrationRequest,
) -> tuple[tuple | None, str]:
    manifest = inventory_fingerprint(root)
    token = request.cursor
    if token is None or not token.startswith("db:"):
        raise MigrationSourceError("invalid_or_changed_cursor")
    try:
        envelope = json.loads(
            base64.b64decode(token[3:], altchars=b"-_", validate=True),
        )
        data = envelope["data"]
        if (
            envelope["checksum"] != _hash(canonical_json(data))
            or data["version"] != 1
            or data["kind"] != "database"
        ):
            raise ValueError("cursor")
        if data["manifest"] != manifest or data["filters"] != _hash(
            canonical_json(request.model_dump(exclude={"cursor", "limit"})),
        ):
            raise ValueError("changed")
        after = data["after"]
        if after is not None and (
            not isinstance(after, list)
            or len(after) != 4
            or any(
                not isinstance(v, str)
                or not is_valid_identity_value(v)
                or len(v) > 128
                for v in after
            )
        ):
            raise ValueError("identity")
        return tuple(after) if after else None, manifest
    except (
        ValueError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        RecursionError,
    ) as exc:
        raise MigrationSourceError("invalid_or_changed_cursor") from exc


def _file_identities(source: _SourceFile, root: Path) -> set | None:
    source = _resolved_profile(source, root)
    try:
        data, _ = _read_file(source, root)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    identities = set()
    for raw in data["jobs"]:
        try:
            tenant, origin, _ = _identity(raw, source)
            identities.add((tenant, origin, source.agent_id, raw["id"]))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return identities


def _safe_source_key(key: Any) -> TypeGuard[str]:
    return isinstance(key, str) and not (
        key.startswith(("/", "\\"))
        or ".." in Path(key).parts
        or PureWindowsPath(key).drive
        or "\\" in key
    )


def audit_snapshot_sources(
    root: Path,
    rows: list[dict],
    manifest: str,
) -> list[dict]:
    """Only resolve provenance against the actual allowlisted source inventory."""
    files = _discover(root.expanduser().resolve())
    if _manifest(files) != manifest:
        raise MigrationSourceError("source_changed_restart_required")
    allowed = {f.key: f for f in files}
    cache = {}
    reports = []
    for row in rows:
        spec = row.get("spec_json")
        if isinstance(spec, (str, bytes)):
            try:
                spec = json.loads(spec)
            except (ValueError, TypeError):
                spec = None
        metadata = spec.get(PROVENANCE_KEY) if isinstance(spec, dict) else None
        key = (
            metadata.get("source_key") if isinstance(metadata, dict) else None
        )
        identity = tuple(
            row.get(k)
            for k in ("tenant_id", "source_id", "agent_id", "job_id")
        )
        if not _safe_source_key(key):
            reports.append(
                {"row": row, "source_key": "", "reason": "unowned_snapshot"},
            )
            continue
        if key not in allowed:
            reports.append(
                {"row": row, "source_key": key, "reason": "source_missing"},
            )
            continue
        if key not in cache:
            cache[key] = _file_identities(allowed[key], root)
        known_identities = cache[key]
        if known_identities is None:
            reports.append(
                {"row": row, "source_key": key, "reason": "source_unreadable"},
            )
        elif identity not in known_identities:
            reports.append(
                {"row": row, "source_key": key, "reason": "source_missing"},
            )
    return reports
