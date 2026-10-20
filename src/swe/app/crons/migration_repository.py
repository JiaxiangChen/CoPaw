# -*- coding: utf-8 -*-
"""Transactional import into the two manually provisioned Cron snapshot tables."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .migration_models import MigrationRequest, SnapshotCandidate
from .migration_source import PROVENANCE_KEY, canonical_json

IDENTITY_FIELDS = ("tenant_id", "source_id", "agent_id", "job_id")
DEFINITION_TABLE = "swe_cron_jobs_v2"
STATE_TABLE = "swe_cron_job_states"
IDENTITY_WHERE = " AND ".join(f"`{name}` = %s" for name in IDENTITY_FIELDS)
DEFINITION_FIELDS = {
    *IDENTITY_FIELDS,
    "scope_id",
    "name",
    "tenant_name",
    "bbk_id",
    "skill_ids",
    "workflow_binding_id",
    "task_type",
    "enabled",
    "cron_expr",
    "original_cron_expr",
    "timezone",
    "spec_json",
    "management_owner",
    "job_origin",
    "creator_user_id",
    "plan_id",
    "parent_tenant_id",
    "parent_source_id",
    "parent_agent_id",
    "parent_job_id",
    "enable_offset",
    "offset_window_hours",
    "offset_minutes",
    "source_definition_version",
}
STATE_FIELDS = {
    "task_chat_id",
    "task_session_id",
    "ordinary_external_job_id",
    "batch_external_job_id",
    "dispatch_state",
    "pause_reason",
    "auto_paused_at",
    "next_run_at",
    "next_run_times_json",
    "last_status",
    "last_run_at",
    "last_prefetch_at",
    "last_error",
    "has_scheduled_result",
    "latest_scheduled_preview",
    "last_scheduled_run_at",
    "unread_execution_count",
    "state_source",
}


@dataclass
class MigrationOutcome:
    status: str
    reason: str | None = None
    differences: list[str] = field(default_factory=list)


class MigrationTargetUnavailable(RuntimeError):
    """The manual target schema or configured shared database is unavailable."""


def _json_value(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return copy.deepcopy(value)


def _spec_without_provenance(value: Any) -> Any:
    value = _json_value(value)
    if isinstance(value, dict):
        value.pop(PROVENANCE_KEY, None)
    return value


def _time(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return (
        value.astimezone(timezone.utc).replace(tzinfo=None)
        if value.tzinfo
        else value
    )


def _equal(name: str, left: Any, right: Any) -> bool:
    if name == "spec_json":
        return _spec_without_provenance(left) == _spec_without_provenance(
            right,
        )
    if name.endswith("_json"):
        return _json_value(left) == _json_value(right)
    if isinstance(right, datetime):
        return _time(left) == right
    return left == right


def _parameters(values: dict) -> tuple:
    return tuple(
        canonical_json(value) if key.endswith("_json") else value
        for key, value in values.items()
    )


def _error_code(error: Exception) -> int | None:
    return (
        error.args[0]
        if error.args and isinstance(error.args[0], int)
        else None
    )


class MigrationRepository:
    """Only this importer may replace its own legacy-owned snapshot rows."""

    def __init__(self, db: Any):
        self.db = db
        self._controls_available = True

    async def check_schema(self) -> None:
        """Check columns with SELECT, never bootstrap or alter tables."""
        if self.db is None or not getattr(self.db, "is_connected", False):
            raise MigrationTargetUnavailable("migration_database_unavailable")
        required = {
            DEFINITION_TABLE: DEFINITION_FIELDS
            | {
                "definition_version",
                "deleted_at",
                "imported_at",
                "source_observed_at",
                "migration_batch_id",
                "created_at",
                "updated_at",
            },
            STATE_TABLE: STATE_FIELDS
            | set(IDENTITY_FIELDS)
            | {
                "state_version",
                "state_observed_at",
                "management_status",
                "management_version",
                "management_updated_at",
                "runtime_updated_at",
                "created_at",
                "updated_at",
            },
        }
        try:
            for table, columns in required.items():
                selected = ", ".join(
                    f"`{column}`" for column in sorted(columns)
                )
                await self.db.execute(
                    f"SELECT {selected} FROM `{table}` WHERE 1 = 0",
                )
        except Exception as exc:
            raise MigrationTargetUnavailable(
                "migration_target_schema_unavailable",
            ) from exc

    async def _read(
        self,
        candidate: SnapshotCandidate,
        *,
        lock: bool = False,
    ) -> tuple:
        suffix = " FOR UPDATE" if lock else ""
        rows = []
        for table in (DEFINITION_TABLE, STATE_TABLE):
            rows.append(
                await self.db.fetch_one(
                    f"SELECT * FROM `{table}` WHERE {IDENTITY_WHERE}{suffix}",
                    candidate.identity,
                ),
            )
        return tuple(rows)

    def _conflict(
        self,
        candidate: SnapshotCandidate,
        definition: dict | None,
        state: dict | None,
    ) -> str | None:
        if definition is None:
            return "orphan_state" if state is not None else None
        return self._definition_conflict(
            candidate,
            definition,
        ) or self._state_conflict(candidate, state)

    @staticmethod
    def _definition_conflict(
        candidate: SnapshotCandidate,
        definition: dict,
    ) -> str | None:
        if definition.get("management_owner") != "legacy":
            return "scheduler_owned"
        if definition.get("deleted_at") is not None:
            return "target_deleted"
        spec = _json_value(definition.get("spec_json"))
        provenance = (
            spec.get(PROVENANCE_KEY) if isinstance(spec, dict) else None
        )
        if not isinstance(provenance, dict) or not definition.get(
            "imported_at",
        ):
            return "unowned_snapshot"
        if provenance.get("source_key") != candidate.source_key:
            return "different_source"
        old_time = _time(definition.get("source_observed_at"))
        if old_time and candidate.observed_at < old_time:
            return "stale_snapshot"
        if candidate.file_version < int(
            definition.get("source_definition_version") or 0,
        ):
            return "stale_file_version"
        return None

    @staticmethod
    def _state_conflict(
        candidate: SnapshotCandidate,
        state: dict | None,
    ) -> str | None:
        if state is None:
            return None
        if (
            state.get("state_source")
            not in (None, "unknown", "file_meta", "memory_snapshot")
            or state.get("management_status") not in (None, "imported")
            or int(state.get("management_version") or 1) != 1
            or state.get("management_updated_at") is not None
            or state.get("runtime_updated_at") is not None
        ):
            return "target_managed"
        state_time = _time(state.get("state_observed_at"))
        if state_time and candidate.observed_at < state_time:
            return "stale_state_snapshot"
        return None

    def _state_values(
        self,
        candidate: SnapshotCandidate,
        current: dict | None,
    ) -> dict:
        values = dict(candidate.state_patch)
        # A file-only refresh does not relabel previously recovered live-memory fields.
        if (
            current
            and current.get("state_source") == "memory_snapshot"
            and not candidate.memory_available
        ):
            values.pop("state_source", None)
        return values

    def _differences(
        self,
        candidate: SnapshotCandidate,
        definition: dict,
        state: dict | None,
    ) -> list[str]:
        differences = [
            name
            for name, value in candidate.definition.items()
            if name not in IDENTITY_FIELDS
            and not _equal(name, definition.get(name), value)
        ]
        if state is None:
            differences.append("state_record")
        else:
            differences.extend(
                f"state.{name}"
                for name, value in self._state_values(candidate, state).items()
                if not _equal(name, state.get(name), value)
            )
        return sorted(differences)

    async def resolve_batch_control(
        self,
        candidate: SnapshotCandidate,
    ) -> None:
        """Optionally read the existing independent parent gate; never repair it."""
        meta = candidate.definition["spec_json"].get("meta", {})
        if (
            not self._controls_available
            or not meta.get("broadcast_dispatch_intents_enabled")
            or meta.get("broadcast_source_job_id")
        ):
            return
        try:
            row = await self.db.fetch_one(
                "SELECT paused FROM swe_cron_dispatch_controls WHERE source_id = %s AND tenant_id = %s AND parent_job_id = %s",
                (
                    candidate.definition["source_id"],
                    candidate.definition["tenant_id"],
                    candidate.definition["job_id"],
                ),
            )
        except Exception as exc:
            if _error_code(exc) not in {1146, 1054}:
                raise
            self._controls_available = False
            return
        if row is not None and row.get("paused") in (0, 1, False, True):
            candidate.state_patch["dispatch_state"] = (
                "batch_paused" if row["paused"] else "batch_enabled"
            )
            candidate.warnings = [
                w for w in candidate.warnings if w != "batch_control_unknown"
            ]

    async def inspect(
        self,
        candidate: SnapshotCandidate,
        mode: Literal["preview", "verify"],
    ) -> MigrationOutcome:
        definition, state = await self._read(candidate)
        reason = self._conflict(candidate, definition, state)
        if reason:
            return MigrationOutcome("conflict", reason)
        if definition is None:
            return MigrationOutcome(
                "would_insert" if mode == "preview" else "missing",
                differences=["definition_record", "state_record"],
            )
        differences = self._differences(candidate, definition, state)
        return MigrationOutcome(
            (
                ("would_update" if mode == "preview" else "different")
                if differences
                else ("unchanged" if mode == "preview" else "matched")
            ),
            differences=differences,
        )

    async def snapshot_rows(
        self,
        request: MigrationRequest,
        after: tuple | None = None,
    ) -> tuple[list[dict], bool]:
        """Read legacy import rows for bounded, non-destructive orphan detection."""
        clauses = [
            "management_owner = 'legacy'",
            "imported_at IS NOT NULL",
            "deleted_at IS NULL",
        ]
        params = []
        for column, values in (
            ("tenant_id", request.tenant_ids),
            ("source_id", request.source_ids),
            ("agent_id", request.agent_ids),
            ("job_id", request.job_ids),
        ):
            if values:
                clauses.append(
                    f"`{column}` IN ({', '.join('%s' for _ in values)})",
                )
                params.extend(values)
        if after is not None:
            clauses.append(
                "(`tenant_id`, `source_id`, `agent_id`, `job_id`) > (%s, %s, %s, %s)",
            )
            params.extend(after)
        rows = await self.db.fetch_all(
            f"SELECT tenant_id, source_id, agent_id, job_id, spec_json FROM `{DEFINITION_TABLE}` WHERE {' AND '.join(clauses)} ORDER BY tenant_id, source_id, agent_id, job_id LIMIT %s",
            (*params, request.limit + 1),
        )
        return rows[: request.limit], len(rows) > request.limit

    async def import_snapshot(
        self,
        candidate: SnapshotCandidate,
    ) -> MigrationOutcome:
        if not set(candidate.definition).issubset(
            DEFINITION_FIELDS,
        ) or not set(candidate.state_patch).issubset(STATE_FIELDS):
            raise ValueError("unexpected_snapshot_field")
        for attempt in range(3):
            try:
                async with self.db.transaction():
                    return await self._write(candidate)
            except Exception as exc:
                if _error_code(exc) not in {1062, 1205, 1213} or attempt == 2:
                    raise
                await asyncio.sleep(0)
        raise RuntimeError("unreachable_snapshot_retry")

    async def _insert(self, table: str, values: dict) -> None:
        columns = ", ".join(f"`{key}`" for key in values)
        placeholders = ", ".join("%s" for _ in values)
        await self.db.execute(
            f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders})",
            _parameters(values),
        )

    async def _update(
        self,
        table: str,
        values: dict,
        candidate: SnapshotCandidate,
        current: dict,
        *,
        bump: bool = True,
    ) -> None:
        version = (
            "definition_version"
            if table == DEFINITION_TABLE
            else "state_version"
        )
        setters = [f"`{key}` = %s" for key in values]
        if bump:
            setters.append(f"`{version}` = `{version}` + 1")
        query = f"UPDATE `{table}` SET {', '.join(setters)} WHERE {IDENTITY_WHERE} AND `{version}` = %s"
        count = await self.db.execute(
            query,
            (
                *_parameters(values),
                *candidate.identity,
                int(current.get(version) or 1),
            ),
        )
        # Under the row lock, an identical definition-watermark UPDATE may
        # legitimately affect zero rows. State/version-changing writes may not.
        if count != 1 and not (count == 0 and not bump):
            raise RuntimeError("snapshot_concurrent_update")

    async def _write(self, candidate: SnapshotCandidate) -> MigrationOutcome:
        definition, state = await self._read(candidate, lock=True)
        reason = self._conflict(candidate, definition, state)
        if reason:
            return MigrationOutcome("conflict", reason)
        changes = (
            self._differences(candidate, definition, state)
            if definition
            else []
        )
        written_at = datetime.now(timezone.utc).replace(tzinfo=None)
        values = copy.deepcopy(candidate.definition)
        values["spec_json"][PROVENANCE_KEY] = {
            "source_key": candidate.source_key,
            "fingerprint": candidate.fingerprint,
            "observed_at": candidate.observed_at.isoformat() + "Z",
        }
        values["source_observed_at"] = candidate.observed_at
        values["updated_at"] = written_at
        if definition is None:
            values["imported_at"] = written_at
            values["created_at"] = written_at
            values["migration_batch_id"] = candidate.fingerprint
            await self._insert(DEFINITION_TABLE, values)
        else:
            for name in IDENTITY_FIELDS:
                values.pop(name)
            definition_changed = any(
                not name.startswith("state.")
                and name not in {"state_record", "source_definition_version"}
                for name in changes
            )
            await self._update(
                DEFINITION_TABLE,
                values,
                candidate,
                definition,
                bump=definition_changed,
            )
        state_values = self._state_values(candidate, state)
        state_values["state_observed_at"] = candidate.observed_at
        state_values["updated_at"] = written_at
        if state is None:
            state_values["created_at"] = written_at
            state_values.update(zip(IDENTITY_FIELDS, candidate.identity))
            state_values["management_status"] = "imported"
            await self._insert(STATE_TABLE, state_values)
        else:
            await self._update(STATE_TABLE, state_values, candidate, state)
        return MigrationOutcome(
            (
                "inserted"
                if definition is None
                else ("updated" if changes else "unchanged")
            ),
            differences=changes,
        )
