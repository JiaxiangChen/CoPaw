# -*- coding: utf-8 -*-
"""Snapshot migration reads legacy files and loaded state without side effects."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from swe.app.crons.migration_models import MigrationRequest
from swe.app.crons.migration_source import (
    MigrationSourceError,
    capture_loaded_states,
    read_source_page,
)
from swe.config.context import encode_scope_id


def write_jobs(root, jobs=None, *, storage=None, agent="default", version=0):
    storage = storage or encode_scope_id("tenant-a", "source-a")
    path = root / storage / "workspaces" / agent / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"definition_version": version, "jobs": jobs or [job()]}),
        encoding="utf-8",
    )
    return path


def job(**changes):
    value = {
        "id": "job-1",
        "name": "检查",
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "enabled": True,
        "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai"},
        "task_type": "text",
        "text": "hello",
        "dispatch": {
            "channel": "console",
            "target": {"user_id": "u", "session_id": "s"},
        },
        "meta": {},
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("with_profile", [False, True])
def test_snapshot_preserves_profile_columns_and_complete_spec(
    tmp_path,
    with_profile,
):
    fields = {
        "tenant_name": "张三",
        "bbk_id": "100",
        "skill_ids": "skill-a",
        "workflow_binding_id": "bind-a",
    }
    value = job(task_type="workflow", **fields) if with_profile else job()
    write_jobs(tmp_path, [value])
    candidate = candidates(read_source_page(tmp_path, MigrationRequest(), {}))[
        0
    ]
    for field, expected in fields.items():
        expected = (
            expected
            if with_profile
            else (None if field == "workflow_binding_id" else "")
        )
        assert candidate.definition[field] == expected
        stored = candidate.definition["spec_json"].get(field)
        assert (
            stored or (None if field == "workflow_binding_id" else "")
        ) == expected


@pytest.mark.asyncio
async def test_import_and_repeat_preserve_profile_columns_from_source(
    tmp_path,
):
    from swe.app.crons.migration_repository import MigrationRepository
    from tests.unit.app.test_cron_migration_repository import FakeDatabase

    fields = {
        "tenant_name": "张三",
        "bbk_id": "100",
        "skill_ids": "skill-a",
        "workflow_binding_id": "bind-a",
    }
    write_jobs(tmp_path, [job(task_type="workflow", **fields)])
    candidate = candidates(read_source_page(tmp_path, MigrationRequest(), {}))[
        0
    ]
    db = FakeDatabase()
    repo = MigrationRepository(db)
    assert (await repo.import_snapshot(candidate)).status == "inserted"
    assert (await repo.import_snapshot(candidate)).status == "unchanged"
    stored = db.tables["swe_cron_jobs_v2"][candidate.identity]
    spec = json.loads(stored["spec_json"])
    for field, value in fields.items():
        assert stored[field] == spec[field] == value


def candidates(page):
    return [
        item.candidate for item in page.items if item.candidate is not None
    ]


def test_reads_all_sources_and_preserves_files(tmp_path):
    first = write_jobs(tmp_path)
    second = write_jobs(
        tmp_path,
        [job(id="job-2", tenant_id="tenant-b", source_id="source-b")],
        storage=encode_scope_id("tenant-b", "source-b"),
        agent="research",
    )
    baseline = [
        (p.read_bytes(), p.stat().st_mtime_ns) for p in (first, second)
    ]
    page = read_source_page(tmp_path, MigrationRequest(), {})
    assert len(candidates(page)) == 2
    assert {c.definition["source_id"] for c in candidates(page)} == {
        "source-a",
        "source-b",
    }
    assert [
        (p.read_bytes(), p.stat().st_mtime_ns) for p in (first, second)
    ] == baseline
    assert all(not c.memory_available for c in candidates(page))


def test_complete_identity_does_not_decode_scope(tmp_path, monkeypatch):
    write_jobs(tmp_path, storage="tenant-a")
    monkeypatch.setattr(
        "swe.app.crons.migration_source.decode_canonical_scope_id",
        lambda _: pytest.fail("must not decode"),
    )
    candidate = candidates(read_source_page(tmp_path, MigrationRequest(), {}))[
        0
    ]
    assert candidate.identity == ("tenant-a", "source-a", "default", "job-1")


def test_missing_identity_uses_canonical_storage(tmp_path):
    value = job()
    value.pop("tenant_id")
    value.pop("source_id")
    write_jobs(tmp_path, [value])
    assert candidates(read_source_page(tmp_path, MigrationRequest(), {}))[
        0
    ].identity[:2] == ("tenant-a", "source-a")


def test_loaded_state_presence_and_zero_values(tmp_path):
    path = write_jobs(
        tmp_path,
        [
            job(
                meta={
                    "task_unread_execution_count": 0,
                    "task_chat_id": "chat",
                    "external_job_id": "ordinary",
                    "batch_dispatch_external_job_id": "batch",
                },
            ),
        ],
    )
    states = {
        f"{path.parents[2].name}:default": {
            "job-1": {
                "last_status": "success",
                "last_run_at": datetime(2026, 10, 15, tzinfo=timezone.utc),
            },
        },
    }
    candidate = candidates(
        read_source_page(tmp_path, MigrationRequest(), states),
    )[0]
    assert candidate.memory_available
    assert candidate.state_patch["last_status"] == "success"
    assert candidate.state_patch["last_run_at"] == datetime(2026, 10, 15)
    assert candidate.state_patch["unread_execution_count"] == 0
    assert candidate.state_patch["ordinary_external_job_id"] == "ordinary"
    assert candidate.state_patch["batch_external_job_id"] == "batch"
    assert "running_count" not in candidate.state_patch


def test_missing_state_is_not_an_empty_overwrite(tmp_path):
    write_jobs(tmp_path)
    candidate = candidates(read_source_page(tmp_path, MigrationRequest(), {}))[
        0
    ]
    assert "last_status" not in candidate.state_patch
    assert "unread_execution_count" not in candidate.state_patch
    assert "memory_state_missing" in candidate.warnings


def test_system_tasks_excluded_but_system_origin_business_included(tmp_path):
    path = write_jobs(
        tmp_path,
        [
            job(id="_heartbeat", task_type="heartbeat"),
            job(id="_dream", task_type="dream"),
            job(id="business", meta={"job_origin": "system"}),
        ],
    )
    (path.parent / "system_jobs.json").write_text(
        '{"_heartbeat":"123"}',
        encoding="utf-8",
    )
    page = read_source_page(tmp_path, MigrationRequest(), {})
    assert [c.identity[-1] for c in candidates(page)] == ["business"]
    assert sum(i.reason == "independent_system_task" for i in page.items) == 2


def test_invalid_record_does_not_hide_valid_record(tmp_path):
    write_jobs(tmp_path, [{"id": "bad"}, job()])
    page = read_source_page(tmp_path, MigrationRequest(), {})
    assert len(candidates(page)) == 1
    assert any(i.reason == "invalid_job" for i in page.items)


def test_cursor_binds_filters_manifest_and_file(tmp_path):
    path = write_jobs(tmp_path, [job(id="a"), job(id="b")])
    page = read_source_page(tmp_path, MigrationRequest(limit=1), {})
    assert page.next_cursor
    second = read_source_page(
        tmp_path,
        MigrationRequest(limit=1, cursor=page.next_cursor),
        {},
    )
    assert [c.identity[-1] for c in candidates(second)] == ["b"]
    with pytest.raises(MigrationSourceError):
        read_source_page(
            tmp_path,
            MigrationRequest(
                limit=1,
                cursor=page.next_cursor,
                source_ids=["source-b"],
            ),
            {},
        )
    path.write_text(
        json.dumps({"jobs": [job(id="changed")]}),
        encoding="utf-8",
    )
    with pytest.raises(MigrationSourceError):
        read_source_page(
            tmp_path,
            MigrationRequest(limit=1, cursor=page.next_cursor),
            {},
        )


def test_cursor_rejects_manifest_additions(tmp_path):
    write_jobs(tmp_path, [job(id="a"), job(id="b")])
    page = read_source_page(tmp_path, MigrationRequest(limit=1), {})
    write_jobs(tmp_path, [job(id="c")], agent="another")
    with pytest.raises(MigrationSourceError):
        read_source_page(
            tmp_path,
            MigrationRequest(cursor=page.next_cursor),
            {},
        )


def test_credentials_redacted_without_dropping_model_options(tmp_path):
    value = job(
        meta={
            "authorization": "secret",
            "token": "secret",
            "provider_api_key": "secret",
            "max_tokens": 200,
            "passthrough_headers": {
                "Cookie": "secret",
                "X-Access-Token": "secret",
                "X-User-Id": "u",
            },
        },
    )
    write_jobs(tmp_path, [value])
    candidate = candidates(read_source_page(tmp_path, MigrationRequest(), {}))[
        0
    ]
    serialized = json.dumps(
        candidate.definition["spec_json"],
        ensure_ascii=False,
    )
    assert "secret" not in serialized
    assert candidate.definition["spec_json"]["meta"]["max_tokens"] == 200
    assert value["meta"]["authorization"] == "secret"


def test_naive_time_is_reported_not_guessed(tmp_path):
    write_jobs(
        tmp_path,
        [job(meta={"task_last_scheduled_run_at": "2026-10-15T09:00:00"})],
    )
    candidate = candidates(read_source_page(tmp_path, MigrationRequest(), {}))[
        0
    ]
    assert "last_scheduled_run_at" not in candidate.state_patch
    assert any(w.startswith("invalid_time:") for w in candidate.warnings)


def test_capture_only_existing_states_and_does_not_start_manager():
    state = {"last_status": "running"}
    manager = SimpleNamespace(
        agents={
            "scope:default": SimpleNamespace(
                cron_manager=SimpleNamespace(_states={"job": state}),
            ),
        },
    )
    before = copy.deepcopy(state)
    snapshot = capture_loaded_states(manager)
    state["last_status"] = "success"
    assert snapshot["scope:default"]["job"] == before


def test_filters_and_empty_filters(tmp_path):
    write_jobs(tmp_path)
    assert not candidates(
        read_source_page(tmp_path, MigrationRequest(source_ids=["other"]), {}),
    )
    with pytest.raises(ValueError):
        MigrationRequest(source_ids=[])


def test_complete_identity_must_match_its_known_storage_scope(tmp_path):
    write_jobs(tmp_path, storage=encode_scope_id("someone-else", "source-b"))
    page = read_source_page(tmp_path, MigrationRequest(), {})
    assert not candidates(page)
    assert page.items[0].reason == "invalid_job"


def test_oversized_binding_is_reported_without_hiding_definition(tmp_path):
    write_jobs(tmp_path, [job(meta={"task_chat_id": "x" * 129})])
    value = candidates(read_source_page(tmp_path, MigrationRequest(), {}))[0]
    assert "task_chat_id" not in value.state_patch
    assert "invalid_state:task_chat_id" in value.warnings


def test_agent_and_workflow_definitions_preserve_execution_configuration(
    tmp_path,
):
    values = [
        job(
            id="agent",
            task_type="agent",
            request={"input": "payload", "max_tokens": 100},
            model_slot={"provider_id": "p", "model": "m"},
        ),
        job(
            id="workflow",
            task_type="workflow",
            skill_ids="skill",
            workflow_binding_id="binding",
        ),
    ]
    write_jobs(tmp_path, values)
    page = read_source_page(tmp_path, MigrationRequest(), {})
    assert {c.definition["task_type"] for c in candidates(page)} == {
        "agent",
        "workflow",
    }
    agent = next(
        c for c in candidates(page) if c.definition["task_type"] == "agent"
    )
    assert agent.definition["spec_json"]["request"]["max_tokens"] == 100


def test_symlink_outside_root_is_not_read(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    path = write_jobs(outside)
    try:
        (root / "unsafe").symlink_to(path.parents[2], target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    page = read_source_page(root, MigrationRequest(), {})
    assert not candidates(page)
    assert page.items[0].reason == "invalid_or_unsafe_file"


def test_profile_agent_id_is_not_assumed_to_equal_directory_name(tmp_path):
    path = write_jobs(tmp_path, agent="custom-directory")
    config = path.parents[2] / "config.json"
    config.write_text(
        json.dumps(
            {
                "agents": {
                    "profiles": {
                        "research": {
                            "id": "research",
                            "workspace_dir": str(path.parent),
                            "enabled": False,
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    before = config.read_bytes(), config.stat().st_mtime_ns
    page = read_source_page(tmp_path, MigrationRequest(), {})
    assert candidates(page)[0].identity[2] == "research"
    assert (config.read_bytes(), config.stat().st_mtime_ns) == before


def test_legacy_prefixed_scope_is_read_without_directory_migration(tmp_path):
    scope = encode_scope_id("tenant-a", "source-a")
    raw = job(scope_id="scope.v1." + scope)
    raw.pop("tenant_id")
    raw.pop("source_id")
    path = write_jobs(tmp_path, [raw], storage="scope.v1." + scope)
    page = read_source_page(tmp_path, MigrationRequest(), {})
    assert candidates(page)[0].identity[:2] == ("tenant-a", "source-a")
    assert path.exists()


def test_invalid_loaded_time_list_does_not_clear_existing_times(tmp_path):
    path = write_jobs(tmp_path)
    loaded = {
        f"{path.parents[2].name}:default": {
            "job-1": {"next_run_times": 5, "last_status": None},
        },
    }
    value = candidates(read_source_page(tmp_path, MigrationRequest(), loaded))[
        0
    ]
    assert "next_run_times_json" not in value.state_patch
    assert "last_status" not in value.state_patch


def test_duplicate_ids_in_one_file_are_reported_without_picking_a_winner(
    tmp_path,
):
    write_jobs(
        tmp_path,
        [
            job(id="duplicate", name="first"),
            job(id="duplicate", name="second"),
            job(id="valid"),
        ],
    )
    page = read_source_page(tmp_path, MigrationRequest(), {})
    assert [c.identity[-1] for c in candidates(page)] == ["valid"]
    assert sum(i.reason == "duplicate_job_identity" for i in page.items) == 2
