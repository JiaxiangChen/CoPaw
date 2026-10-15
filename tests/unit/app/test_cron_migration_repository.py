# -*- coding: utf-8 -*-
"""Transaction and ownership contracts for the two-table snapshot writer."""

from __future__ import annotations

import asyncio
import copy
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

from swe.app.crons.migration_models import SnapshotCandidate
from swe.app.crons.migration_repository import MigrationRepository


class FakeDatabase:
    """Small transactional SQL double; real locking is covered by opt-in MySQL tests."""

    is_connected = True

    def __init__(self):
        self.tables = {"swe_cron_jobs_v2": {}, "swe_cron_job_states": {}}
        self.sql = []
        self.fail_state = False
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            before = copy.deepcopy(self.tables)
            try:
                yield
            except BaseException:
                self.tables = before
                raise

    async def fetch_one(self, query, params=None):
        self.sql.append((query, params))
        if "swe_cron_dispatch_controls" in query:
            return None
        table = re.search(r"FROM `?(\w+)", query, re.I).group(1)
        return copy.deepcopy(self.tables[table].get(tuple(params)))

    async def fetch_all(self, query, params=None):
        self.sql.append((query, params))
        rows = [
            copy.deepcopy(r)
            for r in self.tables["swe_cron_jobs_v2"].values()
            if r.get("management_owner") == "legacy"
            and r.get("imported_at")
            and not r.get("deleted_at")
        ]
        position = 0
        for column, slots in re.findall(r"`(\w+)` IN \(([^)]+)\)", query):
            count = slots.count("%s")
            values = params[position : position + count]
            rows = [r for r in rows if r.get(column) in values]
            position += count
        identity = lambda row: tuple(
            row[k] for k in ("tenant_id", "source_id", "agent_id", "job_id")
        )
        if ") > (" in query:
            after = tuple(params[position : position + 4])
            rows = [r for r in rows if identity(r) > after]
        return sorted(rows, key=identity)[: params[-1]]

    async def execute(self, query, params=None):
        self.sql.append((query, params))
        if query.lstrip().startswith("SELECT"):
            return 0
        table = re.search(r"(?:INTO|UPDATE) `(\w+)`", query).group(1)
        if self.fail_state and table == "swe_cron_job_states":
            raise RuntimeError("injected state write failure")
        if query.startswith("INSERT"):
            names = re.search(r"\((.*?)\) VALUES", query).group(1)
            names = [v.strip().strip("`") for v in names.split(",")]
            row = dict(zip(names, params))
            row.setdefault("definition_version", 1)
            row.setdefault("state_version", 1)
            row.setdefault("management_status", "imported")
            row.setdefault("management_version", 1)
            row.setdefault("management_owner", "legacy")
            key = tuple(
                row[k]
                for k in ("tenant_id", "source_id", "agent_id", "job_id")
            )
            if key in self.tables[table]:
                raise ValueError(1062, "duplicate")
            self.tables[table][key] = row
            return 1
        assignments = (
            query.split(" SET ", 1)[1].split(" WHERE ", 1)[0].split(", ")
        )
        values_count = sum("%s" in a for a in assignments)
        key = tuple(params[values_count : values_count + 4])
        row = self.tables[table].get(key)
        if row is None:
            return 0
        if len(params) > values_count + 4:
            version_field = (
                "definition_version"
                if table == "swe_cron_jobs_v2"
                else "state_version"
            )
            if row.get(version_field) != params[-1]:
                return 0
        position = 0
        for assignment in assignments:
            name = assignment.split("=", 1)[0].strip().strip("`")
            if "%s" in assignment:
                row[name] = params[position]
                position += 1
            else:
                row[name] = row.get(name, 0) + 1
        return 1


def candidate(
    *,
    observed=None,
    name="任务",
    state=None,
    source="tenant/workspaces/default/jobs.json",
    version=0,
):
    return SnapshotCandidate(
        definition={
            "tenant_id": "tenant",
            "source_id": "source",
            "agent_id": "default",
            "job_id": "job",
            "scope_id": "scope",
            "name": name,
            "task_type": "text",
            "enabled": True,
            "cron_expr": "0 9 * * *",
            "original_cron_expr": "0 9 * * *",
            "timezone": "UTC",
            "spec_json": {"id": "job", "name": name},
            "management_owner": "legacy",
            "source_definition_version": version,
        },
        state_patch=(
            state
            if state is not None
            else {
                "last_status": "success",
                "unread_execution_count": 2,
                "state_source": "memory_snapshot",
            }
        ),
        observed_at=observed or datetime(2026, 10, 15, 9),
        source_key=source,
        fingerprint="a" * 64,
        file_version=version,
        memory_available=state is None,
    )


def rows(db):
    return next(iter(db.tables["swe_cron_jobs_v2"].values())), next(
        iter(db.tables["swe_cron_job_states"].values()),
    )


@pytest.mark.asyncio
async def test_first_import_and_repeat_only_write_two_new_tables():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    assert (await repo.import_snapshot(candidate())).status == "inserted"
    assert (await repo.import_snapshot(candidate())).status == "unchanged"
    assert len(db.tables["swe_cron_jobs_v2"]) == 1
    assert rows(db)[1]["unread_execution_count"] == 2
    assert all(
        not re.search(r"^\s*(CREATE|ALTER|DELETE|TRUNCATE)\b", q, re.I)
        for q, _ in db.sql
    )
    assert all("swe_cron_executions" not in q for q, _ in db.sql)


@pytest.mark.asyncio
async def test_two_rows_rollback_together():
    db = FakeDatabase()
    db.fail_state = True
    with pytest.raises(RuntimeError):
        await MigrationRepository(db).import_snapshot(candidate())
    assert not any(db.tables.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition_patch,state_patch,expected",
    [
        (
            {"management_owner": "scheduler", "deleted_at": "deleted"},
            {},
            "scheduler_owned",
        ),
        ({"deleted_at": "deleted", "spec_json": "{}"}, {}, "target_deleted"),
        (
            {"spec_json": "{}", "source_definition_version": 9},
            {},
            "unowned_snapshot",
        ),
        (
            {
                "source_observed_at": datetime(2099, 1, 1),
                "source_definition_version": 9,
            },
            {},
            "stale_snapshot",
        ),
        (
            {"source_definition_version": 9},
            {"state_source": "execution"},
            "stale_file_version",
        ),
        (
            {},
            {
                "state_source": "execution",
                "state_observed_at": datetime(2099, 1, 1),
            },
            "target_managed",
        ),
    ],
)
async def test_conflict_priority_survives_split(
    definition_patch,
    state_patch,
    expected,
):
    db = FakeDatabase()
    repo = MigrationRepository(db)
    value = candidate()
    await repo.import_snapshot(value)
    definition, state = rows(db)
    definition.update(definition_patch)
    state.update(state_patch)
    before = copy.deepcopy(db.tables)
    assert (await repo.inspect(value, "preview")).reason == expected
    assert db.tables == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["owner", "deleted", "live_state", "managed_state", "unowned_source"],
)
async def test_protected_targets_are_not_overwritten(mutation):
    db = FakeDatabase()
    repo = MigrationRepository(db)
    await repo.import_snapshot(candidate())
    definition, state = rows(db)
    if mutation == "owner":
        definition["management_owner"] = "scheduler"
    elif mutation == "deleted":
        definition["deleted_at"] = datetime(2026, 10, 15)
    elif mutation == "live_state":
        state["state_source"] = "execution"
    elif mutation == "managed_state":
        state["management_status"] = "ready"
    else:
        definition["spec_json"] = "{}"
    before = copy.deepcopy(db.tables)
    result = await repo.import_snapshot(
        candidate(name="changed", observed=datetime(2026, 10, 15, 10)),
    )
    assert result.status == "conflict"
    assert db.tables == before


@pytest.mark.asyncio
async def test_file_only_rerun_preserves_memory_but_accepts_explicit_zero():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    await repo.import_snapshot(candidate())
    result = await repo.import_snapshot(
        candidate(
            observed=datetime(2026, 10, 15, 10),
            state={"unread_execution_count": 0, "state_source": "file_meta"},
        ),
    )
    assert result.status == "updated"
    assert rows(db)[1]["last_status"] == "success"
    assert rows(db)[1]["unread_execution_count"] == 0


@pytest.mark.asyncio
async def test_old_snapshot_or_lower_file_version_is_rejected():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    value = candidate(version=3)
    await repo.import_snapshot(value)
    assert (
        await repo.import_snapshot(
            candidate(
                observed=value.observed_at - timedelta(seconds=1),
                name="old",
                version=3,
            ),
        )
    ).status == "conflict"
    assert (
        await repo.import_snapshot(
            candidate(
                observed=value.observed_at + timedelta(seconds=1),
                version=2,
            ),
        )
    ).status == "conflict"


@pytest.mark.asyncio
async def test_new_snapshot_updates_definition_and_only_present_state():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    await repo.import_snapshot(candidate())
    assert (
        await repo.import_snapshot(
            candidate(
                observed=datetime(2026, 10, 15, 10),
                name="new",
                state={"state_source": "unknown"},
            ),
        )
    ).status == "updated"
    definition, state = rows(db)
    assert definition["name"] == "new"
    assert definition["definition_version"] == 2
    assert state["unread_execution_count"] == 2


@pytest.mark.asyncio
async def test_different_file_origin_is_a_conflict():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    await repo.import_snapshot(candidate())
    assert (
        await repo.import_snapshot(candidate(source="other/jobs.json"))
    ).reason == "different_source"


@pytest.mark.asyncio
async def test_read_modes_do_not_write_and_verify_reports_field_names_only():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    assert (
        await repo.inspect(candidate(), "preview")
    ).status == "would_insert"
    assert not any(q.startswith(("INSERT", "UPDATE")) for q, _ in db.sql)
    await repo.import_snapshot(candidate())
    before = copy.deepcopy(db.tables)
    result = await repo.inspect(candidate(name="secret payload"), "verify")
    assert result.status == "different"
    assert "name" in result.differences
    assert "secret payload" not in repr(result)
    assert db.tables == before


@pytest.mark.asyncio
async def test_missing_state_can_be_repaired_but_orphan_state_cannot_be_adopted():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    await repo.import_snapshot(candidate())
    db.tables["swe_cron_job_states"].clear()
    assert (await repo.import_snapshot(candidate())).status == "updated"
    db.tables["swe_cron_jobs_v2"].clear()
    assert (await repo.import_snapshot(candidate())).reason == "orphan_state"


@pytest.mark.asyncio
async def test_concurrent_import_has_one_identity():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    results = await asyncio.gather(
        repo.import_snapshot(candidate()),
        repo.import_snapshot(candidate()),
    )
    assert sorted(r.status for r in results) == ["inserted", "unchanged"]


@pytest.mark.asyncio
async def test_mysql_zero_affected_rows_for_identical_watermark_is_safe():
    class UnchangedDatabase(FakeDatabase):
        async def execute(self, query, params=None):
            result = await super().execute(query, params)
            if (
                query.startswith("UPDATE `swe_cron_jobs_v2`")
                and "`definition_version` = `definition_version` + 1"
                not in query
            ):
                return 0
            return result

    repo = MigrationRepository(UnchangedDatabase())
    await repo.import_snapshot(candidate())
    assert (await repo.import_snapshot(candidate())).status == "unchanged"


@pytest.mark.asyncio
async def test_new_table_timestamps_do_not_depend_on_mysql_session_timezone():
    db = FakeDatabase()
    await MigrationRepository(db).import_snapshot(candidate())
    for row in rows(db):
        assert isinstance(row.get("created_at"), datetime)
        assert isinstance(row.get("updated_at"), datetime)
        assert row["created_at"].tzinfo is None


@pytest.mark.asyncio
async def test_file_version_advance_alone_does_not_change_job_definition_version():
    db = FakeDatabase()
    repo = MigrationRepository(db)
    await repo.import_snapshot(candidate(version=1))
    await repo.import_snapshot(
        candidate(version=2, observed=datetime(2026, 10, 15, 10)),
    )
    assert rows(db)[0]["definition_version"] == 1


@pytest.mark.asyncio
async def test_database_audit_query_is_filtered_and_keyset_paginated():
    from swe.app.crons.migration_models import MigrationRequest

    db = FakeDatabase()
    repo = MigrationRepository(db)
    await repo.import_snapshot(candidate())
    second = candidate()
    second.definition["job_id"] = "job-2"
    await repo.import_snapshot(second)
    values, more = await repo.snapshot_rows(
        MigrationRequest(limit=1, tenant_ids=["tenant"]),
    )
    assert len(values) == 1 and more
    after = tuple(
        values[0][k] for k in ("tenant_id", "source_id", "agent_id", "job_id")
    )
    values, more = await repo.snapshot_rows(MigrationRequest(limit=1), after)
    assert len(values) == 1 and not more and values[0]["job_id"] == "job-2"
    assert (await repo.snapshot_rows(MigrationRequest(tenant_ids=["other"])))[
        0
    ] == []
