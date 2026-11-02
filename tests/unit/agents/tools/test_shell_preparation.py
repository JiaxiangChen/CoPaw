# -*- coding: utf-8 -*-
"""Thread, tenant, and cancellation ownership for Shell preparation."""

import asyncio
from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from swe.agents.tool_failure import ToolExecutionError
from swe.config.context import tenant_context


@pytest.fixture
def shell_roots(monkeypatch, tmp_path):
    from swe import constant
    from swe.config import utils
    from swe.security import tenant_path_boundary

    for module in (constant, utils, tenant_path_boundary):
        monkeypatch.setattr(module, "WORKING_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_installation_denied_before_any_process_starts(
    monkeypatch,
    shell_roots,
    background,
):
    from swe.agents.tools import shell
    from swe.agents.tools import background_process

    tenant_root = shell_roots / "alice"
    tenant_root.mkdir()
    marker = tenant_root / "started"

    async def unexpected_foreground(*args, **kwargs):
        marker.touch()
        return 0, "launched", ""

    def unexpected_background(*args, **kwargs):
        marker.touch()
        raise AssertionError("background process was started")

    monkeypatch.setattr(
        shell,
        "_execute_platform_subprocess",
        unexpected_foreground,
    )
    monkeypatch.setattr(
        background_process.subprocess,
        "Popen",
        unexpected_background,
    )
    with tenant_context(tenant_id="alice"):
        with pytest.raises(
            ToolExecutionError,
            match="平台策略禁止",
        ) as failure:
            execute = (
                background_process.start_background_process
                if background
                else shell.execute_shell_command
            )
            await execute("mkdir started && pip install requests")

    assert failure.value.error_type == "shell_policy_denied"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_real_preparation_and_guard_cleanup_run_off_loop(
    monkeypatch,
    shell_roots,
):
    from swe.agents.tools import shell

    loop_thread = threading.get_ident()
    observed = {}
    guard_paths = []
    for name in (
        "prepare_shell_command",
        "_validate_shell_paths",
        "_scan_python_source_for_outside_path",
        "_prepare_subprocess_env",
        "resolve_current_process_limit_policy",
    ):
        original = getattr(shell, name)

        def record(*args, _name=name, _original=original, **kwargs):
            observed[_name] = threading.get_ident()
            return _original(*args, **kwargs)

        monkeypatch.setattr(shell, name, record)

    original_guard = shell.prepare_python_runtime_path_guard_env

    def prepare_guard(*args, **kwargs):
        observed["guard_prepare"] = threading.get_ident()
        guard = original_guard(*args, **kwargs)
        guard_paths.append(Path(guard.name))

        @contextmanager
        def track_guard():
            observed["guard_enter"] = threading.get_ident()
            try:
                with guard:
                    yield
            finally:
                observed["guard_exit"] = threading.get_ident()

        return track_guard()

    async def subprocess(*_args, **_kwargs):
        assert threading.get_ident() == loop_thread
        assert guard_paths[0].is_dir()
        return 0, "hello", ""

    monkeypatch.setattr(
        shell,
        "prepare_python_runtime_path_guard_env",
        prepare_guard,
    )
    monkeypatch.setattr(shell, "_execute_platform_subprocess", subprocess)
    (shell_roots / "alice").mkdir()
    with tenant_context(tenant_id="alice"):
        result = await shell.execute_shell_command('python -c "print(123)"')
    assert result.content[0]["text"] == "hello"
    assert len(observed) == 8
    assert loop_thread not in observed.values()
    assert not guard_paths[0].exists()


@pytest.mark.asyncio
async def test_concurrent_preparation_keeps_tenant_environment_and_policy(
    monkeypatch,
    shell_roots,
):
    from swe.agents.tools import shell
    from swe.config.config import Config
    from swe.config.context import (
        encode_scope_id,
        get_current_effective_tenant_id,
    )
    from swe.config.utils import save_config
    from swe.envs.store import save_envs

    seen = {}
    for tenant in ("alice", "bob"):
        root = shell_roots / encode_scope_id(tenant, "source-a")
        root.mkdir()
        save_config(Config(), root / "config.json")
        save_envs(
            {"SHELL_TEST_TENANT": tenant},
            root / ".secret" / "envs.json",
        )

    async def subprocess(_cmd, cwd, _timeout, env, **_kwargs):
        tenant = get_current_effective_tenant_id()
        await asyncio.sleep(0)
        seen[tenant] = (cwd, env["SHELL_TEST_TENANT"])
        return 0, tenant, ""

    original_policy = shell.resolve_current_process_limit_policy
    policies = []

    def policy(scope):
        value = original_policy(scope)
        policies.append(value.tenant_id)
        return value

    monkeypatch.setattr(shell, "_execute_platform_subprocess", subprocess)
    monkeypatch.setattr(shell, "resolve_current_process_limit_policy", policy)

    async def execute(tenant):
        scope = encode_scope_id(tenant, "source-a")
        with tenant_context(tenant_id=tenant, source_id="source-a"):
            result = await shell.execute_shell_command("echo hello")
            assert result.content[0]["text"] == scope

    await asyncio.gather(execute("alice"), execute("bob"))
    assert seen == {
        encode_scope_id(tenant, "source-a"): (
            shell_roots / encode_scope_id(tenant, "source-a"),
            tenant,
        )
        for tenant in ("alice", "bob")
    }
    assert sorted(policies) == sorted(
        encode_scope_id(tenant, "source-a") for tenant in ("alice", "bob")
    )


def _fake_preparation(monkeypatch, tmp_path, guard):
    from swe.agents.tools import shell

    prepared = SimpleNamespace(
        command="ignored",
        working_dir=tmp_path,
        env=os.environ.copy(),
        python_runtime_guard=guard,
    )
    monkeypatch.setattr(shell, "prepare_shell_command", lambda *_: prepared)
    monkeypatch.setattr(
        shell,
        "resolve_current_process_limit_policy",
        lambda _: SimpleNamespace(
            build_preexec_fn=lambda: None,
            enabled=False,
            shell_max_concurrent=None,
            diagnostic=None,
        ),
    )
    return shell, prepared


@pytest.mark.asyncio
async def test_cancelled_preparation_waits_and_cleans_without_starting(
    monkeypatch,
    tmp_path,
):
    loop_thread = threading.get_ident()
    started = threading.Event()
    release = threading.Event()
    cleaned = []
    launched = []

    @contextmanager
    def guard():
        try:
            yield
        finally:
            cleaned.append(threading.get_ident())

    shell, prepared = _fake_preparation(monkeypatch, tmp_path, guard())

    def prepare(*_args):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(5)
        return prepared

    async def subprocess(*_args, **_kwargs):
        launched.append(True)
        return 0, "", ""

    monkeypatch.setattr(shell, "prepare_shell_command", prepare)
    monkeypatch.setattr(shell, "_execute_platform_subprocess", subprocess)
    task = asyncio.create_task(shell.execute_shell_command("ignored"))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not cleaned
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert not launched
    assert cleaned and loop_thread not in cleaned


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["policy", "startup"])
async def test_preparation_guard_cleans_on_failure(
    monkeypatch,
    tmp_path,
    stage,
):
    cleaned = []

    @contextmanager
    def guard():
        try:
            yield
        finally:
            cleaned.append(threading.get_ident())

    shell, _ = _fake_preparation(monkeypatch, tmp_path, guard())

    def fail(*_args, **_kwargs):
        raise ValueError("test failure")

    if stage == "policy":
        monkeypatch.setattr(
            shell,
            "resolve_current_process_limit_policy",
            fail,
        )
    else:
        monkeypatch.setattr(shell, "_execute_platform_subprocess", fail)
    expected = ValueError if stage == "policy" else ToolExecutionError
    with pytest.raises(expected, match="test failure"):
        await shell.execute_shell_command("ignored")
    assert cleaned and threading.get_ident() not in cleaned


@pytest.mark.asyncio
async def test_preparation_rejection_preserves_error_and_does_not_launch(
    monkeypatch,
    tmp_path,
):
    shell, _ = _fake_preparation(monkeypatch, tmp_path, nullcontext())
    error = ToolExecutionError(error_type="permission_denied", detail="denied")

    def reject(*_args):
        raise error

    async def forbidden(*_args, **_kwargs):
        pytest.fail("rejected command must not start")

    monkeypatch.setattr(shell, "prepare_shell_command", reject)
    monkeypatch.setattr(shell, "_execute_platform_subprocess", forbidden)
    with pytest.raises(ToolExecutionError) as caught:
        await shell.execute_shell_command("ignored")
    assert caught.value is error


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_guard_cleanup_worker(
    monkeypatch,
    tmp_path,
):
    cleanup_started = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    @contextmanager
    def guard():
        try:
            yield
        finally:
            cleanup_started.set()
            assert release.wait(5)
            cleaned.set()

    shell, _ = _fake_preparation(monkeypatch, tmp_path, guard())

    async def subprocess(*_args, **_kwargs):
        return 0, "hello", ""

    monkeypatch.setattr(shell, "_execute_platform_subprocess", subprocess)
    task = asyncio.create_task(shell.execute_shell_command("ignored"))
    try:
        async with asyncio.timeout(5):
            while not cleanup_started.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not cleaned.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert cleaned.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_opencli_refresh_mirrors_before_foreground_or_background_launch(
    monkeypatch,
    shell_roots,
    background,
):
    import shlex
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from swe.agents.tools import shell, background_process
    from swe.app.crons import auth_state, auth_mirror

    tenant_dir = shell_roots / "alice"
    tenant_dir.mkdir()
    secret_dir = tenant_dir / ".secret"
    monkeypatch.setattr(
        auth_state,
        "get_tenant_secrets_dir",
        lambda _: secret_dir,
    )
    monkeypatch.setattr(auth_state, "get_auth_token", lambda _: "new-token")
    await auth_state.save_cron_auth_state(
        auth_state.CronAuthState(
            user_info={"id": "alice"},
            user_info_expires_at=auth_state.utc_now() + timedelta(days=1),
            cookie_header="identity=alice",
        ),
        tenant_id="alice",
    )
    repository = SimpleNamespace(upsert=AsyncMock(), delete=AsyncMock())
    auth_mirror.set_cron_auth_repository(repository)
    commands = []
    loop_thread = threading.get_ident()

    async def record_mirror(scope, state):
        assert threading.get_ident() == loop_thread
        assert scope == "alice"
        assert state.auth_token == "new-token"

    repository.upsert.side_effect = record_mirror

    def launched(command):
        repository.upsert.assert_awaited_once()
        commands.append(shlex.split(command))

    async def foreground(command, *_args, **_kwargs):
        launched(command)
        return 0, "ok", ""

    def background_launch(command, **_kwargs):
        launched(command)
        return SimpleNamespace(
            process_id="p1",
            pid=1,
            status="running",
            cwd=tenant_dir,
            name=None,
        )

    monkeypatch.setattr(shell, "_execute_platform_subprocess", foreground)
    monkeypatch.setattr(
        background_process.managed_background_process_manager,
        "start",
        background_launch,
    )
    try:
        with tenant_context(
            tenant_id="alice",
            user_id="alice",
            workspace_dir=tenant_dir,
        ):
            execute = (
                background_process.start_background_process
                if background
                else shell.execute_shell_command
            )
            await execute("opencli apps list", cwd=str(tenant_dir))
    finally:
        auth_mirror.set_cron_auth_repository(None)
    assert commands == [
        [
            "opencli",
            "apps",
            "list",
            "--authorization",
            "Bearer new-token",
            "--cookie",
            "identity=alice; com.cmb.dw.rtl.sso.token=new-token",
        ],
    ]
