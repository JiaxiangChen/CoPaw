# -*- coding: utf-8 -*-
"""HTTP-facing orchestration never exposes the captured definition or secrets."""

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from swe.app.crons.migration_models import MigrationRequest
from swe.app.crons.migration_repository import (
    MigrationOutcome,
    MigrationTargetUnavailable,
)
from swe.app.crons.migration_service import MigrationService
from tests.unit.app.test_cron_migration_source import job, write_jobs


class Repository:
    def __init__(self):
        self.actions = []
        self.fail = False

    async def check_schema(self):
        if self.fail:
            raise MigrationTargetUnavailable("unavailable")

    async def resolve_batch_control(self, candidate):
        pass

    async def inspect(self, candidate, mode):
        self.actions.append(mode)
        return MigrationOutcome(
            "would_insert" if mode == "preview" else "missing"
        )

    async def import_snapshot(self, candidate):
        self.actions.append("import")
        if candidate.identity[-1] == "failed":
            raise RuntimeError("password=DO_NOT_EXPOSE")
        return MigrationOutcome("inserted")

    async def snapshot_rows(self, payload, after=None):
        return [], False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["preview", "import", "verify"])
async def test_modes_and_missing_memory_reported_separately(tmp_path, mode):
    write_jobs(tmp_path)
    repo = Repository()
    response = await MigrationService(
        tmp_path, repo, instance_id="instance-a"
    ).run(mode, MigrationRequest(), None)
    assert repo.actions == [mode]
    assert response.instance_id == "instance-a"
    assert response.memory_incomplete
    assert response.counts["memory_missing"] == 1
    assert "hello" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_partial_failure_is_sanitized_and_other_items_continue(tmp_path):
    write_jobs(
        tmp_path,
        [
            job(id="failed"),
            job(id="good"),
            job(id="_heartbeat", task_type="heartbeat"),
        ],
    )
    result = await MigrationService(tmp_path, Repository()).run(
        "import", MigrationRequest(), None
    )
    assert result.counts["failed"] == 1
    assert result.counts["inserted"] == 1
    assert result.counts["excluded"] == 1
    assert result.partial_failure
    assert "DO_NOT_EXPOSE" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_database_unavailable_does_not_scan_files(tmp_path):
    repo = Repository()
    repo.fail = True
    with pytest.raises(MigrationTargetUnavailable):
        await MigrationService(tmp_path / "not-a-root", repo).run(
            "preview", MigrationRequest(), None
        )


@pytest.mark.asyncio
async def test_loaded_state_is_copied_without_initialization(tmp_path):
    path = write_jobs(tmp_path)
    memory = {"job-1": {"last_status": "running"}}
    manager = SimpleNamespace(
        agents={
            f"{path.parents[2].name}:default": SimpleNamespace(
                cron_manager=SimpleNamespace(_states=memory)
            )
        }
    )
    baseline = copy.deepcopy(memory)
    response = await MigrationService(tmp_path, Repository()).run(
        "preview", MigrationRequest(), manager
    )
    assert response.counts["memory_missing"] == 0
    assert memory == baseline


@pytest.mark.asyncio
async def test_verify_reports_database_snapshot_with_missing_source(tmp_path):
    class MissingSourceRepository(Repository):
        async def snapshot_rows(self, payload, after=None):
            return [
                {
                    "tenant_id": "tenant-a",
                    "source_id": "source-a",
                    "agent_id": "default",
                    "job_id": "gone",
                    "spec_json": {
                        "_snapshot_migration": {
                            "source_key": "old/workspaces/default/jobs.json"
                        }
                    },
                }
            ], False

    service = MigrationService(tmp_path, MissingSourceRepository())
    first = await service.run("verify", MigrationRequest(), None)
    assert first.next_cursor
    second = await service.run(
        "verify", MigrationRequest(cursor=first.next_cursor), None
    )
    assert second.counts["source_missing"] == 1
    assert second.phase == "database"
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_snapshot_observation_precedes_memory_capture(
    tmp_path, monkeypatch
):
    write_jobs(tmp_path)
    captured = []
    observed = []

    def capture(manager):
        captured.append(datetime.now(timezone.utc).replace(tzinfo=None))
        return {}

    class RecordingRepository(Repository):
        async def inspect(self, value, mode):
            observed.append(value.observed_at)
            return await super().inspect(value, mode)

    monkeypatch.setattr(
        "swe.app.crons.migration_service.capture_loaded_states", capture
    )
    await MigrationService(tmp_path, RecordingRepository()).run(
        "preview", MigrationRequest(), None
    )
    assert observed[0] <= captured[0]


@pytest.mark.asyncio
async def test_connection_loss_is_unavailable_not_a_successful_partial_page(
    tmp_path,
):
    write_jobs(tmp_path)

    class LostConnectionRepository(Repository):
        async def import_snapshot(self, value):
            raise ConnectionError("DO_NOT_EXPOSE")

    with pytest.raises(
        MigrationTargetUnavailable, match="migration_database_unavailable"
    ):
        await MigrationService(tmp_path, LostConnectionRepository()).run(
            "import", MigrationRequest(), None
        )
