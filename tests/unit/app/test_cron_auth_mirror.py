# -*- coding: utf-8 -*-
"""File-authoritative cron auth mirror integration."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from swe.app.crons import auth_state


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    from swe.app.crons import auth_mirror

    path = tmp_path / "resolved-scope" / ".secret" / "cron_auth.json"
    monkeypatch.setattr(
        auth_state,
        "get_cron_auth_file_path",
        lambda **_: path,
    )
    repo = SimpleNamespace(upsert=AsyncMock(), delete=AsyncMock())
    auth_mirror.set_cron_auth_repository(repo)
    yield auth_mirror, repo, path
    auth_mirror.set_cron_auth_repository(None)


@pytest.mark.asyncio
async def test_save_mirrors_resolved_scope_and_field_clear_after_file_success(
    mirror,
):
    _, repo, path = mirror
    state = auth_state.CronAuthState(
        user_info={"nested": {"id": 1}},
        auth_token="old",
    )

    async def verify_file(scope, snapshot):
        assert scope == "resolved-scope"
        assert json.loads(path.read_text())["user_info"] == snapshot.user_info

    repo.upsert.side_effect = verify_file
    await auth_state.save_cron_auth_state(state, tenant_id="logical-tenant")
    state.auth_token = None
    await auth_state.save_cron_auth_state(state, tenant_id="logical-tenant")
    assert repo.upsert.await_count == 2
    assert repo.upsert.await_args.args[1].auth_token is None


@pytest.mark.asyncio
async def test_db_failure_keeps_file_success_and_sanitizes_log(
    mirror,
    monkeypatch,
):
    module, repo, path = mirror
    warning = Mock()
    monkeypatch.setattr(module.logger, "warning", warning)
    repo.upsert.side_effect = RuntimeError("credential-secret")
    result = await auth_state.save_cron_auth_state(
        auth_state.CronAuthState(auth_token="sensitive"),
    )
    assert result == path
    assert path.is_file()
    logged = str(warning.call_args)
    assert "credential-secret" not in logged
    assert "sensitive" not in logged
    assert "RuntimeError" in logged


@pytest.mark.asyncio
async def test_timeout_bounds_db_wait(mirror, monkeypatch):
    module, repo, path = mirror
    monkeypatch.setattr(module, "MIRROR_TIMEOUT_SECONDS", 0.01)

    async def slow(*_):
        await asyncio.sleep(10)

    repo.upsert.side_effect = slow
    await asyncio.wait_for(
        auth_state.save_cron_auth_state(auth_state.CronAuthState()),
        0.5,
    )
    assert path.is_file()


@pytest.mark.asyncio
async def test_cancellation_is_propagated_after_file_success(mirror):
    _, repo, path = mirror
    repo.upsert.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await auth_state.save_cron_auth_state(auth_state.CronAuthState())
    assert path.is_file()


@pytest.mark.asyncio
async def test_failed_file_write_never_mirrors(mirror, monkeypatch):
    _, repo, _ = mirror

    def fail(*_):
        raise OSError("disk failed")

    monkeypatch.setattr(auth_state, "_prepare_secret_parent", fail)
    with pytest.raises(OSError):
        await auth_state.save_cron_auth_state(auth_state.CronAuthState())
    repo.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_dry_run_and_successful_unlink(mirror):
    _, repo, path = mirror
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    await auth_state.cleanup_cron_auth_except_source(
        working_dir=path.parents[2],
        dry_run=True,
    )
    repo.delete.assert_not_awaited()
    assert path.is_file()
    await auth_state.cleanup_cron_auth_except_source(
        working_dir=path.parents[2],
    )
    repo.delete.assert_awaited_once_with("resolved-scope")
    assert not path.exists()


@pytest.mark.asyncio
async def test_cleanup_failed_unlink_never_deletes_mirror(mirror, monkeypatch):
    _, repo, path = mirror
    path.parent.mkdir(parents=True)
    path.write_text("{}")

    def fail(*_):
        raise OSError("unlink failed")

    monkeypatch.setattr(type(path), "unlink", fail)
    with pytest.raises(OSError):
        await auth_state.cleanup_cron_auth_except_source(
            working_dir=path.parents[2],
        )
    repo.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_registration_reuses_connection_and_clears(monkeypatch):
    from swe.app.crons import auth_mirror
    from swe.app.crons.auth_repository import CronAuthRepository

    db = object()
    auth_mirror.configure_cron_auth_mirror(db)
    assert isinstance(auth_mirror._repository, CronAuthRepository)
    assert auth_mirror._repository.db is db
    auth_mirror.configure_cron_auth_mirror(None)
    assert auth_mirror._repository is None


@pytest.mark.asyncio
async def test_cli_lifecycle_opens_once_and_closes_on_error(monkeypatch):
    from swe.app.crons import auth_mirror
    from swe import database

    monkeypatch.setenv("SWE_DB_HOST", "mysql")
    connection = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    factory = Mock(return_value=connection)
    monkeypatch.setattr(database, "DatabaseConnection", factory)
    monkeypatch.setattr(
        database,
        "get_database_config",
        lambda: SimpleNamespace(host="mysql"),
    )
    with pytest.raises(ValueError):
        async with auth_mirror.cli_cron_auth_mirror():
            assert auth_mirror._repository is not None
            raise ValueError("command failed")
    factory.assert_called_once()
    connection.connect.assert_awaited_once()
    connection.close.assert_awaited_once()
    assert auth_mirror._repository is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refused", "timeout"])
async def test_cli_local_connection_failure_keeps_file_success(
    tmp_path,
    monkeypatch,
    failure,
):
    from swe import database
    from swe.app.crons import auth_mirror

    monkeypatch.setenv("SWE_DB_HOST", "localhost")

    async def connect():
        if failure == "refused":
            raise ConnectionRefusedError("database-password-secret")
        await asyncio.sleep(10)

    connection = SimpleNamespace(
        connect=AsyncMock(side_effect=connect),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        database,
        "DatabaseConnection",
        Mock(return_value=connection),
    )
    monkeypatch.setattr(
        database,
        "get_database_config",
        lambda: SimpleNamespace(host="localhost"),
    )
    monkeypatch.setattr(auth_mirror, "MIRROR_TIMEOUT_SECONDS", 0.01)
    warning = Mock()
    monkeypatch.setattr(auth_mirror.logger, "warning", warning)
    path = tmp_path / "cron_auth.json"
    monkeypatch.setattr(
        auth_state,
        "get_cron_auth_file_path",
        lambda **_: path,
    )

    async def save():
        async with auth_mirror.cli_cron_auth_mirror():
            assert auth_mirror._repository is None
            await auth_state.save_cron_auth_state(
                auth_state.CronAuthState(auth_token="refreshed"),
            )

    await asyncio.wait_for(save(), timeout=0.5)
    assert json.loads(path.read_text())["auth_token"] == "refreshed"
    connection.connect.assert_awaited_once()
    connection.close.assert_awaited_once()
    warning.assert_called_once()
    assert "database-password-secret" not in str(warning.call_args)
    assert auth_mirror._repository is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["save", "delete"])
async def test_cancelled_file_mutation_finishes_its_mirror_attempt(
    mirror,
    monkeypatch,
    operation,
):
    import threading

    _, repo, path = mirror
    started = threading.Event()
    release = threading.Event()
    if operation == "save":
        original = auth_state._save_cron_auth_file

        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            started.set()
            release.wait(timeout=5)
            return result

        monkeypatch.setattr(auth_state, "_save_cron_auth_file", mutate)
        work = auth_state.save_cron_auth_state(auth_state.CronAuthState())
        expected = repo.upsert
    else:
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        original = type(path).unlink

        def delete(file_path, *args, **kwargs):
            result = original(file_path, *args, **kwargs)
            started.set()
            release.wait(timeout=5)
            return result

        monkeypatch.setattr(type(path), "unlink", delete)
        work = auth_state.cleanup_cron_auth_except_source(
            working_dir=path.parents[2],
        )
        expected = repo.delete
    task = asyncio.create_task(work)
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    expected.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SWE_DB_PORT", "3306"),
        ("SWE_DB_USER", "root"),
        ("SWE_DB_ACCESS", "BEE_local-secret"),
        ("SWE_DB_NAME", "swe"),
    ],
)
async def test_cli_local_database_can_be_configured_without_host(
    monkeypatch,
    name,
    value,
):
    from swe import database
    from swe.app.crons import auth_mirror

    for key in os.environ:
        if key.startswith("SWE_DB_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv(name, value)
    connection = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    factory = Mock(return_value=connection)
    monkeypatch.setattr(database, "DatabaseConnection", factory)

    async with auth_mirror.cli_cron_auth_mirror():
        assert auth_mirror._repository.db is connection
    assert factory.call_args.args[0].host == "localhost"
    connection.connect.assert_awaited_once()
    connection.close.assert_awaited_once()
    assert auth_mirror._repository is None


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["mysql", "localhost"])
async def test_cli_config_does_not_log_database_credentials(monkeypatch, host):
    import logging
    from swe import database
    from swe.app.crons import auth_mirror

    raw = "BEE_cli-password-secret"
    monkeypatch.setenv("SWE_DB_ACCESS", raw)
    monkeypatch.setenv("SWE_DB_HOST", host)
    info = Mock()
    monkeypatch.setattr(logging.getLogger("swe.database.config"), "info", info)
    connection = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(
        database,
        "DatabaseConnection",
        Mock(return_value=connection),
    )
    async with auth_mirror.cli_cron_auth_mirror():
        pass
    connection.connect.assert_awaited_once()
    assert "cli-password-secret" not in str(info.call_args_list)
    assert raw not in str(info.call_args_list)


@pytest.mark.asyncio
async def test_service_teardown_clears_registration_when_cleanup_raises():
    from swe.app import _app
    from swe.app.crons import auth_mirror

    auth_mirror.configure_cron_auth_mirror(object())
    app = SimpleNamespace(
        state=SimpleNamespace(
            transient_memory_cleanup=SimpleNamespace(
                stop=AsyncMock(side_effect=ValueError("stop failed")),
            ),
        ),
    )
    try:
        with pytest.raises(ValueError, match="stop failed"):
            await _app._shutdown_lifespan_resources(app, None)
        assert auth_mirror._repository is None
    finally:
        auth_mirror.set_cron_auth_repository(None)


@pytest.mark.asyncio
async def test_startup_failure_before_yield_clears_mirror(monkeypatch):
    from swe.app import _app
    from swe.app import auth
    from swe.app.crons import auth_mirror

    monkeypatch.setattr(_app, "_configure_async_thread_pools", Mock())
    monkeypatch.setattr(auth, "auto_register_from_env", Mock())
    monkeypatch.setattr(_app, "ensure_default_agent_exists", Mock())
    monkeypatch.setattr(
        _app,
        "_reset_scope_sensitive_runtime_state",
        AsyncMock(),
    )
    monkeypatch.setattr(
        _app,
        "_initialize_runtime_managers",
        AsyncMock(return_value=(object(), object())),
    )
    connection = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        _app,
        "_initialize_database_connection",
        AsyncMock(return_value=connection),
    )
    monkeypatch.setattr(
        _app,
        "_initialize_tracing_manager",
        AsyncMock(side_effect=ValueError("startup failed")),
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(_app, "_shutdown_lifespan_resources_impl", cleanup)
    app = SimpleNamespace(state=SimpleNamespace())
    try:
        with pytest.raises(ValueError, match="startup failed"):
            async with _app.lifespan(app):
                pytest.fail("Startup must fail before yielding")
        assert auth_mirror._repository is None
        cleanup.assert_awaited_once_with(app, connection)
    finally:
        auth_mirror.set_cron_auth_repository(None)


@pytest.mark.asyncio
async def test_cleanup_directory_scan_runs_in_worker(mirror, monkeypatch):
    import threading

    _, _, path = mirror
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    loop_thread = threading.get_ident()
    original = type(path).iterdir
    scanned = []

    def scan(directory):
        scanned.append(threading.get_ident())
        return original(directory)

    monkeypatch.setattr(type(path), "iterdir", scan)
    await auth_state.cleanup_cron_auth_except_source(
        working_dir=path.parents[2],
        dry_run=True,
    )
    assert scanned
    assert loop_thread not in scanned
