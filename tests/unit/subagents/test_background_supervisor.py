# -*- coding: utf-8 -*-
"""Background SubAgent supervisor tests."""

from __future__ import annotations

import signal
import json
import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
from unittest.mock import AsyncMock

import psutil
import pytest

from swe.app.subagents import (
    AgentResult,
    AgentRegistry,
    BackgroundSubAgentScope,
    BackgroundSubAgentStartBlocked,
    BackgroundSubAgentSupervisor,
    DefinitionMatchMetadata,
    DelegationSpec,
    InMemoryDefinitionProvider,
    PerRunSubAgentRunStore,
    PermissionPolicy,
    SkillOwnedDefinitionMetadata,
    SubAgentStartRequest,
    builtin_definition_provider,
    initialize_community_expert_dependency_view,
)
from swe.config.config import AgentProfileConfig, MCPClientConfig, MCPConfig
from swe.app.tenant_context import bind_tenant_context
from swe.app.subagents.models import AgentOwnedDefinitionMetadata
from swe.app.subagents.builtins import _builtin


@pytest.fixture(autouse=True)
def _snapshot_model_for_supervisor_tests(monkeypatch):
    """Keep supervisor tests independent from the local tenant provider state."""
    from swe.app.subagents import supervisor as supervisor_module
    from swe.providers.models import ModelSlotConfig

    monkeypatch.setitem(
        globals(),
        "builtin_definition_provider",
        _definition_provider,
    )
    monkeypatch.setattr(
        supervisor_module,
        "builtin_definition_provider",
        _definition_provider,
    )
    monkeypatch.setattr(
        supervisor_module,
        "capture_model_launch_snapshot",
        lambda **_kwargs: (
            None,
            ModelSlotConfig(provider_id="test", model="test-model"),
        ),
    )


def _definition_provider():
    """Lifecycle tests provide their own expert after builtins were removed."""
    return InMemoryDefinitionProvider(
        [
            _builtin(
                name="plan-researcher",
                description="test",
                instruction="test",
            ),
        ],
    )


class _FakeProcess:
    def __init__(self, pid: int = 99999999):
        self.pid = pid
        self.returncode: int | None = None
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = -signal.SIGTERM
        return self.returncode


class _FakePopenFactory:
    def __init__(self):
        self.processes: list[_FakeProcess] = []
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        process = _FakeProcess(pid=99999999 + len(self.processes))
        self.processes.append(process)
        self.commands.append(list(command))
        return process


def _scope(tmp_path: Path) -> BackgroundSubAgentScope:
    return BackgroundSubAgentScope(
        tenant_id="tenant-1",
        agent_id="agent-1",
        run_store_dir=tmp_path / "subagent_runs",
    )


def _spec() -> DelegationSpec:
    return DelegationSpec(
        task_id="task-1",
        parent_thread_id="session-1",
        name="plan-researcher",
        objective="Inspect supervisor behavior",
    )


@pytest.mark.asyncio
async def test_start_cancellation_cleans_registered_worker(
    tmp_path,
    monkeypatch,
):
    supervisor = BackgroundSubAgentSupervisor()
    scope = _scope(tmp_path)
    entered = asyncio.Event()

    async def start_reserved(**kwargs):
        supervisor._active_for_scope(scope)[kwargs["run_id"]] = object()
        entered.set()
        await asyncio.Future()

    cancel = AsyncMock()
    monkeypatch.setattr(supervisor, "_start_reserved", start_reserved)
    monkeypatch.setattr(supervisor, "cancel", cancel)
    task = asyncio.create_task(
        supervisor.start(
            scope=scope,
            spec=_spec(),
            parent_agent_config=AgentProfileConfig(id="agent-1", name="Main"),
            workspace_dir=tmp_path,
        ),
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancel.assert_awaited_once()
    assert cancel.await_args.args[0] == scope


@pytest.mark.asyncio
async def test_cancel_turn_runs_only_cancels_exact_owner_turn(
    tmp_path,
) -> None:
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=popen_factory,
        registry=AgentRegistry(
            [
                InMemoryDefinitionProvider(
                    [
                        _builtin(
                            name="plan-researcher",
                            description="test",
                            instruction="test",
                        ),
                    ],
                ),
            ],
        ),
    )
    scope = _scope(tmp_path)
    target = await supervisor.start(
        scope=scope,
        spec=DelegationSpec(
            parent_thread_id="session-1",
            parent_chat_id="chat-a",
            parent_msgid="turn-a",
            name="plan-researcher",
            objective="Target turn",
        ),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )
    other = await supervisor.start(
        scope=scope,
        spec=DelegationSpec(
            parent_thread_id="session-1",
            parent_chat_id="chat-a",
            parent_msgid="turn-b",
            name="plan-researcher",
            objective="Other turn",
        ),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )

    cancelled = await supervisor.cancel_turn_runs(
        scope,
        chat_id="chat-a",
        msgid="turn-a",
    )

    assert cancelled == [target.run_id]
    assert (await supervisor.get(scope, target.run_id)).status == "cancelled"
    assert (await supervisor.get(scope, other.run_id)).status == "running"


def _agent_config(tmp_path: Path) -> AgentProfileConfig:
    return AgentProfileConfig(
        id="agent-1",
        name="Agent",
        workspace_dir=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_start_blocks_when_concurrency_limit_reached(tmp_path):
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=1,
        popen_factory=popen_factory,
        registry=AgentRegistry([builtin_definition_provider()]),
    )
    scope = _scope(tmp_path)

    first = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )
    second = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )

    run_files = [
        path
        for path in scope.run_store_dir.glob("subagent-*.json")
        if not path.name.endswith(".launch.json")
    ]
    assert first.status == "running"
    assert isinstance(second, BackgroundSubAgentStartBlocked)
    assert second.status == "blocked"
    assert second.reason == "background_subagent_concurrency_limit"
    assert second.limit == 1
    assert second.active_run_ids == [first.run_id]
    assert len(run_files) == 1


@pytest.mark.asyncio
async def test_start_uses_the_initialized_community_expert_session_view(
    tmp_path: Path,
) -> None:
    """Later runs in a Chat keep using the first selected dependency view."""
    definition = (
        AgentRegistry([builtin_definition_provider()])
        .resolve(
            "plan-researcher",
        )
        .model_copy(
            update={
                "name": "received-reviewer",
                "agent_owned": AgentOwnedDefinitionMetadata(
                    definition_id="00000000-0000-0000-0000-000000000030",
                    declared_skills=["quality"],
                    declared_mcps=["github"],
                    community={
                        "item_id": "expert-1",
                        "version": "1.0.0",
                        "content_fingerprint": "fingerprint",
                    },
                ),
            },
        )
    )
    source_root = (
        tmp_path
        / "agents"
        / "00000000-0000-0000-0000-000000000030.dependencies"
    )
    (source_root / "skills" / "quality").mkdir(parents=True)
    (source_root / "skills" / "quality" / "SKILL.md").write_text(
        "# session original",
        encoding="utf-8",
    )
    (source_root / "mcp").mkdir()
    (source_root / "mcp" / "config.json").write_text(
        '{"github":{"name":"github","command":"session-github"}}',
        encoding="utf-8",
    )
    chat_id = "00000000-0000-0000-0000-000000000031"
    view_root = initialize_community_expert_dependency_view(
        workspace_dir=tmp_path,
        chat_id=chat_id,
        definition=definition,
    )
    assert view_root is not None
    (source_root / "skills" / "quality" / "SKILL.md").write_text(
        "# later profile update",
        encoding="utf-8",
    )

    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(popen_factory=popen_factory)
    scope = _scope(tmp_path)
    started = await supervisor.start(
        scope=scope,
        spec=_spec().model_copy(update={"name": definition.name}),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        definition=definition,
        request_context={
            "chat_id": chat_id,
            "_expert_dependency_view_root": str(view_root),
        },
    )

    assert started.status == "running"
    launch = json.loads(
        (scope.run_store_dir / f"{started.run_id}.launch.json").read_text(
            encoding="utf-8",
        ),
    )
    skill_dir = Path(launch["launch_snapshot"]["skill_snapshot_dirs"][0])
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == (
        "# session original"
    )
    mcp_path = Path(launch["launch_snapshot"]["private_mcp_snapshot_path"])
    assert (
        json.loads(mcp_path.read_text(encoding="utf-8"))["github"]["command"]
        == "session-github"
    )


@pytest.mark.asyncio
async def test_wait_lazy_reaps_worker_without_result(tmp_path):
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=1,
        popen_factory=popen_factory,
    )
    scope = _scope(tmp_path)
    started = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )
    popen_factory.processes[0].returncode = 1

    snapshot = await supervisor.wait(scope, timeout_ms=1)
    record = await PerRunSubAgentRunStore(scope.run_store_dir).get(
        started.run_id,
    )

    assert snapshot.terminal_runs
    assert snapshot.terminal_runs[0].status == "failed"
    assert record is not None
    assert record.status == "failed"
    assert record.worker is not None
    assert record.worker.exit_code == 1
    assert record.errors[-1].code == "worker_exited_without_result"


@pytest.mark.asyncio
async def test_reap_run_does_not_reap_other_workers(tmp_path):
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=2,
        popen_factory=popen_factory,
    )
    scope = _scope(tmp_path)
    first = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )
    second = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )
    popen_factory.processes[0].returncode = 0
    popen_factory.processes[1].returncode = None

    result = await supervisor.reap_run(scope, first.run_id)

    assert result is not None and result.status == "failed"
    assert first.run_id not in supervisor._active_for_scope(scope)
    assert second.run_id in supervisor._active_for_scope(scope)


@pytest.mark.asyncio
async def test_wait_for_run_waits_for_one_worker_without_a_poll_timeout(
    tmp_path,
):
    popen_factory = _FakePopenFactory()
    definition = _builtin(
        name="plan-researcher",
        description="test definition",
        instruction="test instruction",
    )
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=popen_factory,
        registry=AgentRegistry([InMemoryDefinitionProvider([definition])]),
    )
    scope = _scope(tmp_path)
    started = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )

    record = await supervisor.wait_for_run(scope, started.run_id)

    assert record is not None
    assert record.run_id == started.run_id
    assert record.status == "failed"


@pytest.mark.asyncio
async def test_wait_preserves_partial_worker_result(tmp_path):
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=1,
        popen_factory=popen_factory,
    )
    scope = _scope(tmp_path)
    started = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )
    store = PerRunSubAgentRunStore(scope.run_store_dir)
    partial = AgentResult(
        task_id="task-1",
        agent_run_id=started.run_id,
        agent_name="plan-researcher",
        status="partial",
        summary="research retained",
    )
    await store.finish(started.run_id, partial)
    popen_factory.processes[0].returncode = 0

    snapshot = await supervisor.wait(scope, timeout_ms=1)
    record = await store.get(started.run_id)

    assert snapshot.terminal_runs[0].status == "partial"
    assert snapshot.terminal_runs[0].result == partial
    assert record is not None
    assert record.status == "partial"
    assert record.result == partial


@pytest.mark.asyncio
async def test_start_persists_start_request_match_and_runtime_nickname(
    tmp_path,
):
    popen_factory = _FakePopenFactory()
    registry = AgentRegistry([builtin_definition_provider()])
    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=1,
        popen_factory=popen_factory,
        registry=registry,
    )
    scope = _scope(tmp_path)
    definition = registry.resolve("plan-researcher")
    start_request = SubAgentStartRequest.model_validate(
        {
            "name": "plan-researcher",
            "instruction": "Research a plan.",
            "objective": "Find evidence.",
        },
    )
    definition_match = DefinitionMatchMetadata(
        matched=True,
        definition_name="plan-researcher",
        definition_source="builtin",
        score=1.0,
    )

    started = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
        definition=definition,
        start_request=start_request,
        definition_match=definition_match,
    )
    record = await PerRunSubAgentRunStore(scope.run_store_dir).get(
        started.run_id,
    )

    assert record is not None
    assert record.nickname
    assert record.start_request is not None
    assert record.start_request.name == "plan-researcher"
    assert record.definition_match.matched is True
    assert record.definition_match.definition_name == "plan-researcher"


@pytest.mark.asyncio
async def test_start_launch_spec_carries_current_scope_id(tmp_path):
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=1,
        popen_factory=popen_factory,
    )
    scope = _scope(tmp_path)

    with bind_tenant_context(
        tenant_id="tenant-1",
        source_id="source-1",
        scope_id="dGVuYW50LTE.c291cmNlLTE",
        workspace_dir=tmp_path,
    ):
        started = await supervisor.start(
            scope=scope,
            spec=_spec(),
            parent_agent_config=_agent_config(tmp_path),
            workspace_dir=tmp_path,
            request_context={
                "tenant_id": "tenant-1",
                "source_id": "source-1",
            },
        )

    launch_path = scope.run_store_dir / f"{started.run_id}.launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8"))

    assert launch["request_context"]["tenant_id"] == "tenant-1"
    assert launch["request_context"]["source_id"] == "source-1"
    assert launch["request_context"]["scope_id"] == "dGVuYW50LTE.c291cmNlLTE"


@pytest.mark.asyncio
async def test_cancel_terminates_process_group(
    monkeypatch,
    tmp_path,
):
    from swe.app.subagents import supervisor as supervisor_module

    signals: list[int] = []
    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=1,
        popen_factory=popen_factory,
    )
    scope = _scope(tmp_path)
    started = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )

    monkeypatch.setattr(supervisor_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        lambda _pgid, sig: signals.append(sig),
    )

    response = await supervisor.cancel(scope, started.run_id)

    assert response.status == "cancelled"
    assert signals == [signal.SIGTERM]


@pytest.mark.asyncio
async def test_cancel_removes_unconsumed_private_dependency_snapshots(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A killed worker cannot leave one-shot credentials behind."""
    from swe.app.subagents import supervisor as supervisor_module

    popen_factory = _FakePopenFactory()
    supervisor = BackgroundSubAgentSupervisor(popen_factory=popen_factory)
    scope = _scope(tmp_path)
    started = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )
    mcp_snapshot = scope.run_store_dir / f".{started.run_id}.mcp.json"
    model_snapshot = scope.run_store_dir / f".{started.run_id}.model.json"
    mcp_snapshot.write_text("{}", encoding="utf-8")
    model_snapshot.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(supervisor_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(supervisor_module.os, "killpg", lambda *_args: None)

    await supervisor.cancel(scope, started.run_id)

    assert not mcp_snapshot.exists()
    assert not model_snapshot.exists()


@pytest.mark.asyncio
async def test_start_failure_uses_structured_worker_start_error(tmp_path):
    def _raising_popen(command, **kwargs):
        raise OSError("no worker")

    supervisor = BackgroundSubAgentSupervisor(
        max_running_per_scope=1,
        popen_factory=_raising_popen,
    )
    scope = _scope(tmp_path)

    response = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )

    assert response.status == "failed"
    assert response.errors[-1].code == "worker_start_failed"


@pytest.mark.asyncio
async def test_start_failure_removes_private_mcp_snapshot(tmp_path):
    def _raising_popen(command, **kwargs):
        raise OSError("no worker")

    config = _agent_config(tmp_path).model_copy(
        update={
            "mcp": MCPConfig(
                clients={
                    "github": MCPClientConfig(
                        name="github",
                        command="github-mcp",
                    ),
                },
            ),
        },
    )
    definition = (
        AgentRegistry([builtin_definition_provider()])
        .resolve(
            "plan-researcher",
        )
        .model_copy(
            update={
                "name": "quality:reviewer",
                "skill_owned": SkillOwnedDefinitionMetadata(
                    skill_name="quality",
                    local_name="reviewer",
                    declared_mcps=["github"],
                ),
            },
        )
    )
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=_raising_popen,
    )
    scope = _scope(tmp_path)

    response = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=config,
        workspace_dir=tmp_path,
        definition=definition,
    )

    assert response.status == "failed"
    assert not list(scope.run_store_dir.glob(".*.mcp.json"))


@pytest.mark.asyncio
async def test_start_stops_when_model_snapshot_serialization_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A worker cannot launch without an immutable model/provider snapshot."""
    from swe.app.subagents import launch_snapshot as snapshot_module
    from swe.providers.models import ModelSlotConfig

    class UnserializableProvider:
        def model_dump(self, **_kwargs):
            raise ValueError("provider serialization failed")

    class Manager:
        def get_active_model(self):
            return ModelSlotConfig(provider_id="openai", model="gpt-test")

        def get_provider(self, _provider_id):
            return UnserializableProvider()

    monkeypatch.setattr(
        snapshot_module.ProviderManager,
        "get_instance",
        lambda _tenant_id: Manager(),
    )
    from swe.app.subagents import supervisor as supervisor_module

    monkeypatch.setattr(
        supervisor_module,
        "capture_model_launch_snapshot",
        snapshot_module.capture_model_launch_snapshot,
    )
    popen_factory = _FakePopenFactory()
    config = _agent_config(tmp_path).model_copy(
        update={
            "mcp": MCPConfig(
                clients={
                    "github": MCPClientConfig(
                        name="github",
                        command="github-mcp",
                    ),
                },
            ),
        },
    )
    definition = (
        AgentRegistry([builtin_definition_provider()])
        .resolve("plan-researcher")
        .model_copy(
            update={
                "name": "quality:reviewer",
                "skill_owned": SkillOwnedDefinitionMetadata(
                    skill_name="quality",
                    local_name="reviewer",
                    declared_skills=["quality"],
                    declared_mcps=["github"],
                ),
            },
        )
    )
    supervisor = BackgroundSubAgentSupervisor(popen_factory=popen_factory)
    scope = _scope(tmp_path)
    skill_dir = tmp_path / "skills" / "quality"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Quality", encoding="utf-8")

    response = await supervisor.start(
        scope=scope,
        spec=_spec(),
        parent_agent_config=config,
        workspace_dir=tmp_path,
        definition=definition,
        effective_skill_names=["quality"],
    )

    assert response.status == "failed"
    assert response.errors[-1].code == "worker_snapshot_failed"
    assert popen_factory.processes == []
    assert not list(scope.run_store_dir.glob(".*.mcp.json"))
    assert not list(scope.run_store_dir.glob(".*.model.json"))
    assert not list(scope.run_store_dir.glob("*.skills"))


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        (set(), "研究员"),
        ({"研究员"}, "研究员-2"),
        ({"研究员", "研究员-3"}, "研究员-2"),
        ({"研究员", "研究员-2", "研究员-3"}, "研究员-4"),
    ],
)
def test_allocate_subagent_nickname_uses_smallest_unused_suffix(
    used,
    expected,
):
    from swe.app.subagents.nicknames import allocate_subagent_nickname

    assert allocate_subagent_nickname("研究员", used) == expected


@pytest.mark.asyncio
async def test_configured_nickname_remains_unique_after_terminal_run(tmp_path):
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=_FakePopenFactory(),
    )
    definition = (
        _definition_provider()
        .list_definitions()[0]
        .model_copy(
            update={"nickname": "研究员"},
        )
    )
    scope = _scope(tmp_path)
    kwargs = {
        "scope": scope,
        "spec": _spec(),
        "definition": definition,
        "parent_agent_config": _agent_config(tmp_path),
        "workspace_dir": tmp_path,
    }
    first = await supervisor.start(**kwargs)
    await supervisor.cancel(scope, first.run_id)
    second = await supervisor.start(**kwargs)

    store = PerRunSubAgentRunStore(scope.run_store_dir)
    assert (await store.get(first.run_id)).nickname == "研究员"
    assert (await store.get(second.run_id)).nickname == "研究员-2"
    assert first.run_id != second.run_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_chat", "second_session", "second_chat", "expected_nicknames"),
    [
        ("", "session-1", "", {"研究员", "研究员-2"}),
        ("", "session-1", "other-chat", {"研究员", "研究员-2"}),
        ("chat-1", "session-1", "", {"研究员", "研究员-2"}),
        ("chat-1", "session-1", "other-chat", {"研究员"}),
        ("", "other-session", "", {"研究员"}),
    ],
)
async def test_concurrent_starts_reserve_nicknames_before_launch(
    tmp_path,
    monkeypatch,
    first_chat,
    second_session,
    second_chat,
    expected_nicknames,
):
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=_FakePopenFactory(),
    )
    definition = (
        _definition_provider()
        .list_definitions()[0]
        .model_copy(
            update={"nickname": "研究员"},
        )
    )
    scope = _scope(tmp_path)
    entered = 0
    launch_barrier = asyncio.Event()
    original_start_reserved = supervisor._start_reserved

    async def start_reserved(
        *,
        scope,
        run_id,
        nickname,
        spec,
        parent_agent_config,
        workspace_dir,
        **kwargs,
    ):
        nonlocal entered
        entered += 1
        assert not list(scope.run_store_dir.glob("subagent-*.json"))
        if entered == 2:
            launch_barrier.set()
        await launch_barrier.wait()
        return await original_start_reserved(
            scope=scope,
            run_id=run_id,
            nickname=nickname,
            spec=spec,
            parent_agent_config=parent_agent_config,
            workspace_dir=workspace_dir,
            **kwargs,
        )

    monkeypatch.setattr(supervisor, "_start_reserved", start_reserved)
    kwargs = {
        "scope": scope,
        "spec": _spec().model_copy(update={"parent_chat_id": first_chat}),
        "definition": definition,
        "parent_agent_config": _agent_config(tmp_path),
        "workspace_dir": tmp_path,
    }
    records = await asyncio.wait_for(
        asyncio.gather(
            supervisor.start(**kwargs),
            supervisor.start(
                **{
                    **kwargs,
                    "spec": _spec().model_copy(
                        update={
                            "parent_thread_id": second_session,
                            "parent_chat_id": second_chat,
                        },
                    ),
                },
            ),
        ),
        timeout=5,
    )
    assert {record.nickname for record in records} == expected_nicknames


@pytest.mark.asyncio
async def test_two_supervisors_allocate_unique_nicknames_in_same_chat(
    tmp_path,
):
    """Separate Supervisor instances share durable nickname reservations."""
    definition = (
        _definition_provider()
        .list_definitions()[0]
        .model_copy(update={"nickname": "研究员"})
    )
    scope = _scope(tmp_path)
    kwargs = {
        "scope": scope,
        "spec": _spec().model_copy(update={"parent_chat_id": "chat-1"}),
        "definition": definition,
        "parent_agent_config": _agent_config(tmp_path),
        "workspace_dir": tmp_path,
    }

    records = await asyncio.gather(
        BackgroundSubAgentSupervisor(popen_factory=_FakePopenFactory()).start(
            **kwargs,
        ),
        BackgroundSubAgentSupervisor(popen_factory=_FakePopenFactory()).start(
            **kwargs,
        ),
    )

    assert {record.nickname for record in records} == {"研究员", "研究员-2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_chat,second_chat",
    [("chat-1", ""), ("", "chat-1")],
)
async def test_persisted_nickname_records_treat_empty_chat_as_session_wildcard(
    tmp_path,
    first_chat,
    second_chat,
):
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=_FakePopenFactory(),
    )
    definition = (
        _definition_provider()
        .list_definitions()[0]
        .model_copy(update={"nickname": "研究员"})
    )
    scope = _scope(tmp_path)
    kwargs = {
        "scope": scope,
        "definition": definition,
        "parent_agent_config": _agent_config(tmp_path),
        "workspace_dir": tmp_path,
    }

    first = await supervisor.start(
        **kwargs,
        spec=_spec().model_copy(update={"parent_chat_id": first_chat}),
    )
    second = await supervisor.start(
        **kwargs,
        spec=_spec().model_copy(update={"parent_chat_id": second_chat}),
    )

    assert first.nickname == "研究员"
    assert second.nickname == "研究员-2"


@pytest.mark.asyncio
async def test_nickname_allocation_reclaims_stale_orphan_reservation(tmp_path):
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=_FakePopenFactory(),
    )
    definition = (
        _definition_provider()
        .list_definitions()[0]
        .model_copy(update={"nickname": "研究员"})
    )
    scope = _scope(tmp_path)
    scope.run_store_dir.mkdir()
    session_digest = hashlib.sha256(b"session-1").hexdigest()[:16]
    stale = (
        scope.run_store_dir
        / f".nickname-reservation-{session_digest}-orphan.json"
    )
    stale.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "chat_id": "chat-1",
                "nickname": "研究员",
                "owner_pid": 99999999,
                "owner_started_at": 0,
                "created_at": time.time() - 3600,
            },
        ),
        encoding="utf-8",
    )

    record = await supervisor.start(
        scope=scope,
        spec=_spec().model_copy(update={"parent_chat_id": "chat-1"}),
        definition=definition,
        parent_agent_config=_agent_config(tmp_path),
        workspace_dir=tmp_path,
    )

    assert record.nickname == "研究员"
    assert not stale.exists()


@pytest.mark.asyncio
async def test_nickname_allocation_keeps_live_other_process_reservation(
    tmp_path,
):
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=_FakePopenFactory(),
    )
    definition = (
        _definition_provider()
        .list_definitions()[0]
        .model_copy(update={"nickname": "研究员"})
    )
    scope = _scope(tmp_path)
    scope.run_store_dir.mkdir()
    owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    session_digest = hashlib.sha256(b"session-1").hexdigest()[:16]
    reservation = (
        scope.run_store_dir
        / f".nickname-reservation-{session_digest}-live.json"
    )
    try:
        reservation.write_text(
            json.dumps(
                {
                    "session_id": "session-1",
                    "chat_id": "chat-1",
                    "nickname": "研究员",
                    "owner_pid": owner.pid,
                    "owner_started_at": psutil.Process(
                        owner.pid,
                    ).create_time(),
                    "created_at": time.time() - 3600,
                },
            ),
            encoding="utf-8",
        )

        record = await supervisor.start(
            scope=scope,
            spec=_spec().model_copy(update={"parent_chat_id": "chat-1"}),
            definition=definition,
            parent_agent_config=_agent_config(tmp_path),
            workspace_dir=tmp_path,
        )

        assert record.nickname == "研究员-2"
        assert reservation.exists()
    finally:
        owner.terminate()
        owner.wait(timeout=5)
        reservation.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_nickname_reservation_rejects_symlinked_run_store(tmp_path):
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=_FakePopenFactory(),
    )
    scope = _scope(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    scope.run_store_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        await supervisor.start(
            scope=scope,
            spec=_spec(),
            parent_agent_config=_agent_config(tmp_path),
            workspace_dir=tmp_path,
        )

    assert not list(outside.iterdir())


@pytest.mark.asyncio
async def test_other_session_keeps_reservation_while_it_is_being_written(
    tmp_path,
    monkeypatch,
):
    supervisor = BackgroundSubAgentSupervisor()
    definition = (
        _definition_provider()
        .list_definitions()[0]
        .model_copy(update={"nickname": "研究员"})
    )
    scope = _scope(tmp_path)
    write_started = threading.Event()
    continue_write = threading.Event()
    original_dump = json.dump

    def paused_dump(payload, stream, **kwargs):
        if payload.get("session_id") == "session-1":
            write_started.set()
            assert continue_write.wait(timeout=5)
        return original_dump(payload, stream, **kwargs)

    monkeypatch.setattr(json, "dump", paused_dump)

    def reserve_first():
        return asyncio.run(
            supervisor._reserve_nickname(scope, _spec(), definition),
        )

    first_task = asyncio.create_task(asyncio.to_thread(reserve_first))
    try:
        assert await asyncio.to_thread(write_started.wait, 5)
        first_path = next(
            scope.run_store_dir.glob(".nickname-reservation-*.json"),
        )
        second = await supervisor._reserve_nickname(
            scope,
            _spec().model_copy(update={"parent_thread_id": "session-2"}),
            definition,
        )
        first_exists_during_write = first_path.exists()
    finally:
        continue_write.set()
        first = (await asyncio.gather(first_task, return_exceptions=True))[0]

    assert first_exists_during_write
    assert first == ("研究员", first_path)
    assert second[0] == "研究员"
    assert first_path.exists()
    assert second[1].exists()


@pytest.mark.asyncio
async def test_nickname_allocation_keeps_old_reservation_when_owner_access_denied(
    tmp_path,
    monkeypatch,
):
    supervisor = BackgroundSubAgentSupervisor()
    definition = (
        _definition_provider()
        .list_definitions()[0]
        .model_copy(update={"nickname": "研究员"})
    )
    scope = _scope(tmp_path)
    _, reservation = await supervisor._reserve_nickname(
        scope,
        _spec(),
        definition,
    )
    payload = json.loads(reservation.read_text(encoding="utf-8"))
    payload["created_at"] = time.time() - 3600
    reservation.write_text(json.dumps(payload), encoding="utf-8")
    original_process = psutil.Process

    def denied_owner_process(pid=None):
        if pid == os.getpid():
            raise psutil.AccessDenied(pid)
        return original_process(pid)

    monkeypatch.setattr(psutil, "Process", denied_owner_process)
    nickname, _ = await supervisor._reserve_nickname(
        scope,
        _spec(),
        definition,
    )

    assert nickname == "研究员-2"
    assert reservation.exists()
