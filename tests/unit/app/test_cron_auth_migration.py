# -*- coding: utf-8 -*-
"""Credential migration contracts; source files remain authoritative."""

import asyncio
import importlib
import json

import pytest
from pydantic import ValidationError

from swe.app.crons.auth_repository import CronAuthRepository
from swe.app.crons.auth_state import CronAuthState
from swe.config.context import encode_scope_id
from tests.unit.app.test_cron_auth_repository import FakeDatabase


def modules():
    found = {}
    for name in ("models", "source", "service"):
        spec = importlib.util.find_spec("swe.app.crons.auth_migration_" + name)
        assert spec is not None, "auth migration " + name + " is missing"
        found[name] = importlib.import_module(spec.name)
    return found


def write(root, scope, value):
    path = root / scope / ".secret" / "cron_auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


async def collect(service, request):
    pages = []
    phase, after = (
        "database" if request.operation == "prune" else "files"
    ), None
    while True:
        page = await service.page(request, phase=phase, after=after)
        pages.append(page)
        if page.scan_complete:
            return pages
        phase, after = page.next_phase, page.next_after


@pytest.mark.parametrize(
    "value",
    [[], ["A", "A"], ["../bad"], ["/bad"], ["a\\b"], ["."], ["a\x00b"]],
)
def test_strict_scope_filters(value):
    cls = modules()["models"].AuthMigrationRequest
    with pytest.raises(ValidationError):
        cls(operation="preview", storage_scope_ids=value)


def test_prune_requires_explicit_range_and_unknown_fields_rejected():
    cls = modules()["models"].AuthMigrationRequest
    with pytest.raises(ValidationError):
        cls(operation="prune")
    with pytest.raises(ValidationError):
        cls(operation="preview", cursor="x")
    assert (
        cls(operation="prune", storage_scope_ids=["scope.v1.a.b"]).operation
        == "prune"
    )


@pytest.mark.asyncio
async def test_four_operations_complete_snapshot_repeat_import_and_db_only(
    tmp_path,
):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    service = m["service"].AuthMigrationService(tmp_path, repo)
    request = m["models"].AuthMigrationRequest
    write(
        tmp_path,
        "Alice",
        {"auth_token": "TOKEN_SECRET", "user_info": {"name": "USER_SECRET"}},
    )
    write(tmp_path, "Bob", {})
    await repo.upsert("db-only", CronAuthState(auth_token="OLD_SECRET"))
    pages = await collect(service, request(operation="preview", page_size=1))
    items = [i for p in pages for i in p.items]
    assert [i.status for i in items].count("would_insert") == 2
    assert [i.status for i in items].count("database_only") == 1
    assert await repo.get("Alice") is None
    await collect(service, request(operation="import", page_size=1))
    await collect(service, request(operation="import", page_size=1))
    assert (await repo.get("Alice"))["auth_token"] == "TOKEN_SECRET"
    assert await repo.get("db-only") is not None
    write(tmp_path, "Alice", {})
    pages = await collect(service, request(operation="verify", page_size=1))
    item = next(
        i for p in pages for i in p.items if i.storage_scope_id == "Alice"
    )
    assert item.status == "different"
    assert item.differences == ["user_info", "auth_token"]
    assert not any(
        s in json.dumps([p.model_dump() for p in pages])
        for s in ["TOKEN_SECRET", "USER_SECRET", "OLD_SECRET"]
    )
    await collect(service, request(operation="import"))
    assert repo.state_from_row(await repo.get("Alice")) == CronAuthState()
    await collect(
        service,
        request(operation="prune", storage_scope_ids=["db-only", "Alice"]),
    )
    assert await repo.get("db-only") is None
    assert await repo.get("Alice") is not None


@pytest.mark.asyncio
async def test_invalid_symlink_and_oversize_files_never_import_or_prune(
    tmp_path,
    monkeypatch,
):
    m = modules()
    monkeypatch.setattr(m["source"], "MAX_FILE_BYTES", 64)
    repo = CronAuthRepository(FakeDatabase())
    for key in ["invalid", "array", "large", "link"]:
        await repo.upsert(key, CronAuthState(auth_token="KEEP"))
    write(tmp_path, "invalid", {}).write_text("{BAD_SECRET")
    write(tmp_path, "array", []).write_text("[]")
    write(tmp_path, "large", {"auth_token": "x" * 100})
    path = write(tmp_path, "link", {})
    path.unlink()
    path.symlink_to(tmp_path / "array" / ".secret" / "cron_auth.json")
    service = m["service"].AuthMigrationService(tmp_path, repo)
    cls = m["models"].AuthMigrationRequest
    pages = await collect(service, cls(operation="import"))
    assert {
        i.storage_scope_id
        for p in pages
        for i in p.items
        if i.status == "invalid_source"
    } == {"invalid", "array", "large", "link"}
    await collect(
        service,
        cls(
            operation="prune",
            storage_scope_ids=["invalid", "array", "large", "link"],
        ),
    )
    for key in ["invalid", "array", "large", "link"]:
        assert (await repo.get(key))["auth_token"] == "KEEP"
    assert "BAD_SECRET" not in json.dumps([p.model_dump() for p in pages])


@pytest.mark.asyncio
async def test_filters_are_intersection_preserve_legacy_default_and_case(
    tmp_path,
):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    canonical = encode_scope_id("Alice", "RMASSIST")
    legacy = "scope.v1." + canonical
    for scope in [
        canonical,
        legacy,
        "default_RMASSIST",
        "default",
        "Alice",
        "alice",
    ]:
        write(tmp_path, scope, {})
    service = m["service"].AuthMigrationService(tmp_path, repo)
    cls = m["models"].AuthMigrationRequest
    await collect(
        service,
        cls(
            operation="import",
            tenant_ids=["Alice"],
            source_ids=["RMASSIST"],
            storage_scope_ids=[legacy, "default_RMASSIST"],
        ),
    )
    assert [r["storage_scope_id"] for r in await repo.list_rows(None, 20)] == [
        legacy,
    ]
    pages = await collect(
        service,
        cls(
            operation="preview",
            storage_scope_ids=["Alice", "default", "default_RMASSIST"],
        ),
    )
    items = [i for p in pages for i in p.items]
    assert {i.storage_scope_id for i in items} == {
        "Alice",
        "default",
        "default_RMASSIST",
    }
    assert any("unknown_metadata" in i.warnings for i in items)


@pytest.mark.asyncio
async def test_source_root_missing_or_unreadable_cannot_authorize_prune(
    tmp_path,
    monkeypatch,
):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    await repo.upsert("gone", CronAuthState())
    service = m["service"].AuthMigrationService(tmp_path / "missing", repo)
    cls = m["models"].AuthMigrationRequest
    with pytest.raises(m["source"].AuthMigrationSourceUnavailable):
        await collect(
            service,
            cls(operation="prune", storage_scope_ids=["gone"]),
        )
    assert await repo.get("gone") is not None
    service.root = tmp_path

    def denied(*args, **kwargs):
        raise PermissionError("RAW_SECRET")

    monkeypatch.setattr(m["source"].os, "scandir", denied)
    with pytest.raises(m["source"].AuthMigrationSourceUnavailable) as exc:
        await collect(
            service,
            cls(operation="prune", storage_scope_ids=["gone"]),
        )
    assert "RAW_SECRET" not in str(exc.value)
    assert await repo.get("gone") is not None


@pytest.mark.asyncio
async def test_source_scans_are_bounded_byte_order_and_off_event_loop(
    tmp_path,
    monkeypatch,
):
    m = modules()
    for scope in ["z", "A", "a", "中", "é", "default"]:
        write(tmp_path, scope, {})
    observed = []
    original = m["source"].read_source_page

    def inspect(*args, **kwargs):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        result = original(*args, **kwargs)
        observed.extend(i.storage_scope_id for i in result.items)
        assert len(result.items) <= 2
        return result

    monkeypatch.setattr(m["service"], "read_source_page", inspect)
    service = m["service"].AuthMigrationService(
        tmp_path,
        CronAuthRepository(FakeDatabase()),
    )
    pages = await collect(
        service,
        m["models"].AuthMigrationRequest(operation="preview", page_size=2),
    )
    assert observed == sorted(observed, key=lambda v: v.encode("utf-8"))
    assert all(
        p.scanned_files <= 2 and p.scanned_database_rows <= 2 for p in pages
    )


@pytest.mark.asyncio
async def test_database_failure_fatal_and_sanitized(tmp_path):
    m = modules()
    write(tmp_path, "Alice", {})
    db = FakeDatabase()
    db.failure = RuntimeError("SQL TOKEN_SECRET")
    from swe.app.crons.auth_repository import CronAuthRepositoryUnavailable

    with pytest.raises(CronAuthRepositoryUnavailable) as exc:
        await collect(
            m["service"].AuthMigrationService(
                tmp_path,
                CronAuthRepository(db),
            ),
            m["models"].AuthMigrationRequest(operation="import"),
        )
    assert "TOKEN_SECRET" not in str(exc.value)


@pytest.mark.asyncio
async def test_import_repairs_invalid_database_snapshot(tmp_path):
    m = modules()
    db = FakeDatabase()
    repo = CronAuthRepository(db)
    await repo.upsert("A", CronAuthState())
    db.rows[b"A"]["user_info"] = "INVALID_SECRET"
    write(tmp_path, "A", {})
    service = m["service"].AuthMigrationService(tmp_path, repo)
    cls = m["models"].AuthMigrationRequest
    pages = await collect(service, cls(operation="verify"))
    assert any(i.status == "invalid_database" for p in pages for i in p.items)
    await collect(service, cls(operation="import"))
    assert repo.state_from_row(await repo.get("A")) == CronAuthState()


def test_root_disappearing_after_check_is_fatal(tmp_path, monkeypatch):
    m = modules()
    original = m["source"]._open_directory

    def gone(path, **kwargs):
        if path == tmp_path:
            raise FileNotFoundError(2, "SECRET")
        return original(path, **kwargs)

    monkeypatch.setattr(m["source"], "_open_directory", gone)
    with pytest.raises(m["source"].AuthMigrationSourceUnavailable):
        m["source"].read_record(tmp_path, "gone")


@pytest.mark.asyncio
async def test_prune_rechecks_file_and_keeps_newly_appearing_source(
    tmp_path,
    monkeypatch,
):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    await repo.upsert("A", CronAuthState(auth_token="KEEP"))
    real = m["service"].read_record
    calls = []

    def race(root, scope):
        calls.append(scope)
        if len(calls) == 2:
            write(root, scope, {})
        return real(root, scope)

    monkeypatch.setattr(m["service"], "read_record", race)
    await collect(
        m["service"].AuthMigrationService(tmp_path, repo),
        m["models"].AuthMigrationRequest(
            operation="prune",
            storage_scope_ids=["A"],
        ),
    )
    assert (await repo.get("A"))["auth_token"] == "KEEP"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["scope_link", "secret_link", "permission"])
async def test_prune_protects_unreadable_and_symlink_parents(
    tmp_path,
    monkeypatch,
    kind,
):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    await repo.upsert("A", CronAuthState(auth_token="KEEP"))
    (tmp_path / "outside").mkdir()
    if kind == "scope_link":
        (tmp_path / "A").symlink_to(
            tmp_path / "outside",
            target_is_directory=True,
        )
    elif kind == "secret_link":
        (tmp_path / "A").mkdir()
        (tmp_path / "A" / ".secret").symlink_to(
            tmp_path / "outside",
            target_is_directory=True,
        )
    else:
        original = m["source"]._open_directory

        def denied(path, **kwargs):
            if path == "A":
                raise PermissionError(13, "SECRET")
            return original(path, **kwargs)

        monkeypatch.setattr(m["source"], "_open_directory", denied)
    pages = await collect(
        m["service"].AuthMigrationService(tmp_path, repo),
        m["models"].AuthMigrationRequest(
            operation="prune",
            storage_scope_ids=["A"],
        ),
    )
    assert (await repo.get("A"))["auth_token"] == "KEEP"
    assert any(i.status == "invalid_source" for p in pages for i in p.items)


@pytest.mark.asyncio
async def test_comparison_normalizes_json_objects_and_utc_times(tmp_path):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    from datetime import datetime, timezone

    await repo.upsert(
        "A",
        CronAuthState(
            user_info={"b": [1, {"k": True}], "a": None},
            auth_token_expires_at=datetime(
                2026,
                10,
                29,
                10,
                tzinfo=timezone.utc,
            ),
        ),
    )
    write(
        tmp_path,
        "A",
        {
            "user_info": {"a": None, "b": [1, {"k": True}]},
            "auth_token_expires_at": "2026-10-29T18:00:00+08:00",
        },
    )
    pages = await collect(
        m["service"].AuthMigrationService(tmp_path, repo),
        m["models"].AuthMigrationRequest(operation="verify"),
    )
    assert [i.status for p in pages for i in p.items] == ["unchanged"]


def test_safe_exact_scope_preserves_compatible_colon_name():
    cls = modules()["models"].AuthMigrationRequest
    assert cls(
        operation="preview",
        storage_scope_ids=["Alice:Legacy"],
    ).storage_scope_ids == ["Alice:Legacy"]


@pytest.mark.asyncio
async def test_comparison_distinguishes_nested_json_booleans_from_numbers(
    tmp_path,
):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    await repo.upsert(
        "A",
        CronAuthState(user_info={"nested": [{"flag": True}]}),
    )
    write(tmp_path, "A", {"user_info": {"nested": [{"flag": 1}]}})
    page = (
        await m["service"]
        .AuthMigrationService(tmp_path, repo)
        .page(
            m["models"].AuthMigrationRequest(operation="verify"),
            phase="files",
            after=None,
        )
    )
    assert page.items[0].status == "different"
    assert page.items[0].differences == ["user_info"]


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
async def test_nonfinite_json_is_invalid_and_never_imported_or_pruned(
    tmp_path,
    number,
):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    await repo.upsert("A", CronAuthState(auth_token="KEEP"))
    write(tmp_path, "A", {}).write_text(
        '{"user_info":{"nested":[' + number + "]}}",
    )
    service = m["service"].AuthMigrationService(tmp_path, repo)
    cls = m["models"].AuthMigrationRequest
    for operation in ["import", "prune"]:
        pages = await collect(
            service,
            cls(operation=operation, storage_scope_ids=["A"]),
        )
        assert any(
            i.status == "invalid_source" for p in pages for i in p.items
        )
    assert (await repo.get("A"))["auth_token"] == "KEEP"


@pytest.mark.asyncio
async def test_comparison_preserves_json_numeric_equivalence(tmp_path):
    m = modules()
    repo = CronAuthRepository(FakeDatabase())
    await repo.upsert("A", CronAuthState(user_info={"nested": [1, 0]}))
    write(tmp_path, "A", {"user_info": {"nested": [1.0, -0.0]}})
    page = (
        await m["service"]
        .AuthMigrationService(tmp_path, repo)
        .page(
            m["models"].AuthMigrationRequest(operation="verify"),
            phase="files",
            after=None,
        )
    )
    assert page.items[0].status == "unchanged"


@pytest.mark.asyncio
async def test_unserializable_deep_source_is_local_issue_and_later_record_continues(
    tmp_path,
):
    m = modules()
    nested = {}
    for _ in range(300):
        nested = {"child": nested}
    write(tmp_path, "A", {"user_info": nested})
    write(tmp_path, "B", {"auth_token": "valid"})
    repo = CronAuthRepository(FakeDatabase())
    service = m["service"].AuthMigrationService(tmp_path, repo)
    cls = m["models"].AuthMigrationRequest
    for operation in ["preview", "import"]:
        pages = await collect(service, cls(operation=operation))
        item = next(
            i for p in pages for i in p.items if i.storage_scope_id == "A"
        )
        assert item.status == "invalid_source"
    assert await repo.get("A") is None
    assert (await repo.get("B"))["auth_token"] == "valid"
