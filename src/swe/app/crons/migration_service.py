# -*- coding: utf-8 -*-
"""Request-bounded snapshot orchestration; no background job or durable queue."""

from __future__ import annotations

import socket
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ...runtime_workers import run_runtime_state_work
from .migration_models import (
    MigrationItemResult,
    MigrationRequest,
    MigrationResponse,
)
from .migration_repository import (
    IDENTITY_FIELDS,
    MigrationOutcome,
    MigrationRepository,
    MigrationTargetUnavailable,
)
from .migration_source import (
    audit_snapshot_sources,
    capture_loaded_states,
    database_cursor,
    database_position,
    inventory_fingerprint,
    read_source_page,
)


class MigrationService:
    def __init__(
        self,
        root: Path,
        repository: MigrationRepository,
        *,
        instance_id: str | None = None,
    ):
        self.root = root
        self.repository = repository
        self.instance_id = instance_id or socket.gethostname()

    async def run(
        self,
        operation: Literal["preview", "import", "verify"],
        payload: MigrationRequest,
        manager: Any,
    ) -> MigrationResponse:
        await self.repository.check_schema()
        if operation == "verify" and (payload.cursor or "").startswith("db:"):
            return await self._audit_database(payload)
        observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        loaded = capture_loaded_states(manager)
        page = await run_runtime_state_work(
            read_source_page, self.root, payload, loaded, observed_at
        )
        counts = Counter(memory_missing=0)
        results = []
        for item in page.items:
            if item.candidate is None:
                status = (
                    "excluded"
                    if item.reason == "independent_system_task"
                    else "failed"
                )
                counts[status] += 1
                results.append(
                    MigrationItemResult(
                        source_key=item.source_key,
                        job_id=item.job_id,
                        status=status,
                        reason=item.reason,
                    )
                )
                continue
            candidate = item.candidate
            counts["memory_missing"] += int(not candidate.memory_available)
            try:
                await self.repository.resolve_batch_control(candidate)
                outcome = (
                    await self.repository.import_snapshot(candidate)
                    if operation == "import"
                    else await self.repository.inspect(candidate, operation)
                )
            except Exception as exc:
                code = exc.args[0] if exc.args else None
                if isinstance(
                    exc, (ConnectionError, TimeoutError)
                ) or code in {
                    1040,
                    1045,
                    1049,
                    1054,
                    1146,
                    2002,
                    2003,
                    2006,
                    2013,
                }:
                    raise MigrationTargetUnavailable(
                        "migration_database_unavailable"
                    ) from exc
                # The per-item transaction has rolled back. Never return SQL, credentials,
                # payloads, or the raw DB exception through the operator endpoint.
                outcome = MigrationOutcome(
                    "failed", "snapshot_database_operation_failed"
                )
            counts[outcome.status] += 1
            results.append(
                MigrationItemResult(
                    source_key=item.source_key,
                    job_id=item.job_id,
                    identity=dict(zip(IDENTITY_FIELDS, candidate.identity)),
                    status=outcome.status,
                    reason=outcome.reason,
                    differences=outcome.differences,
                    warnings=candidate.warnings,
                    memory_available=candidate.memory_available,
                )
            )
        next_cursor = page.next_cursor
        if operation == "verify" and next_cursor is None:
            manifest = await run_runtime_state_work(
                inventory_fingerprint, self.root
            )
            next_cursor = database_cursor(manifest, payload, None)
        return MigrationResponse(
            operation=operation,
            instance_id=self.instance_id,
            counts=dict(counts),
            scanned_files=page.scanned_files,
            scanned_jobs=page.scanned_jobs,
            memory_incomplete=bool(counts["memory_missing"]),
            partial_failure=bool(counts["failed"] or counts["conflict"]),
            next_cursor=next_cursor,
            items=results,
        )

    async def _audit_database(
        self, payload: MigrationRequest
    ) -> MigrationResponse:
        after, manifest = await run_runtime_state_work(
            database_position, self.root, payload
        )
        rows, more = await self.repository.snapshot_rows(payload, after)
        reports = await run_runtime_state_work(
            audit_snapshot_sources, self.root, rows, manifest
        )
        results = [
            MigrationItemResult(
                source_key=report["source_key"],
                job_id=report["row"].get("job_id"),
                identity={k: report["row"][k] for k in IDENTITY_FIELDS},
                status=report["reason"],
                reason=report["reason"],
            )
            for report in reports
        ]
        counts = Counter(item.status for item in results)
        next_cursor = (
            database_cursor(
                manifest, payload, tuple(rows[-1][k] for k in IDENTITY_FIELDS)
            )
            if more
            else None
        )
        return MigrationResponse(
            operation="verify",
            instance_id=self.instance_id,
            counts=dict(counts),
            scanned_files=0,
            scanned_jobs=0,
            scanned_database_rows=len(rows),
            memory_incomplete=False,
            partial_failure=bool(
                counts["source_unreadable"] or counts["unowned_snapshot"]
            ),
            next_cursor=next_cursor,
            items=results,
            phase="database",
        )
