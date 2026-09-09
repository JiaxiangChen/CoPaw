# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from swe.config.context import encode_scope_id
from swe.envs.store import save_envs
from swe.agents.hook_runtime.executor import execute_handler
from swe.agents.hook_runtime.merge import merge_hook_results
from swe.agents.hook_runtime.models import (
    CommandHookHandlerConfig,
    EffectiveHookHandler,
    EffectiveHookPlan,
    FailPolicy,
    HookConfig,
    HookContext,
    HookDecision,
    HookEventName,
    HookHandlerResult,
    HookOutput,
    HttpHookHandlerConfig,
    PromptHookHandlerConfig,
    HookMatcherGroupConfig,
)
from swe.agents.hook_runtime.output import (
    normalize_hook_output,
    normalize_prompt_judgment_output,
)
from swe.config.context import tenant_context


def _context(event: HookEventName = HookEventName.PRE_TOOL_USE) -> HookContext:
    return HookContext(
        session_id="session-1",
        transcript_path="/tmp/transcript.json",
        cwd="/tmp/tenant-a/workspaces/default",
        hook_event_name=event,
        tenant_id="tenant-a",
        effective_tenant_id="tenant-a",
        source_id="source-a",
        user_id="user-1",
        agent_id="agent-1",
        channel="console",
        workspace_dir="/tmp/tenant-a/workspaces/default",
        tool_name="execute_shell_command",
        tool_input={"cmd": "echo old"},
        tool_use_id="tool-1",
    )


def _write_scope_env(
    root: Path,
    tenant_id: str,
    source_id: str,
    envs: dict[str, str],
) -> None:
    scope_id = encode_scope_id(tenant_id, source_id)
    save_envs(envs, root / scope_id / ".secret" / "envs.json")


def _plan(*handlers) -> EffectiveHookPlan:
    return EffectiveHookPlan(
        event_name=HookEventName.PRE_TOOL_USE,
        context=_context(),
        handlers=tuple(
            EffectiveHookHandler(
                handler=h,
                group_id="group",
                order=i,
                dedupe_key=f"tenant-a:PreToolUse:group:{h.id}:{h.type}:{h.target_identity()}",
            )
            for i, h in enumerate(handlers)
        ),
    )


@pytest.mark.asyncio
async def test_command_handler_parses_exit_zero_stdout_json(
    tmp_path: Path,
) -> None:
    script = tmp_path / "hook.py"
    script.write_text(
        "import json, sys\n"
        "ctx=json.load(sys.stdin)\n"
        "print(json.dumps({'hookSpecificOutput': {'additionalContext': 'seen '+ctx['hook_event_name']}}))\n",
        encoding="utf-8",
    )
    handler = CommandHookHandlerConfig(
        id="cmd",
        argv=["python", str(script)],
    )

    with tenant_context(tenant_id="tenant-a", workspace_dir=tmp_path):
        result = await execute_handler(
            handler,
            _context(),
            workspace_dir=tmp_path,
        )

    assert result.failed is False
    assert (
        result.output.hook_specific_output["additionalContext"]
        == "seen PreToolUse"
    )


@pytest.mark.asyncio
async def test_command_exit_two_maps_to_block_without_json_parse(
    tmp_path: Path,
) -> None:
    script = tmp_path / "block.py"
    script.write_text(
        "import sys\n"
        "print('{not-json')\n"
        "print('blocked by script', file=sys.stderr)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    handler = CommandHookHandlerConfig(
        id="blocker",
        argv=["python", str(script)],
    )

    with tenant_context(tenant_id="tenant-a", workspace_dir=tmp_path):
        result = await execute_handler(
            handler,
            _context(),
            workspace_dir=tmp_path,
        )

    assert result.failed is False
    assert result.decision == HookDecision.BLOCK
    assert "blocked by script" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_policy", "decision"),
    [
        (FailPolicy.ALLOW, HookDecision.NONE),
        (FailPolicy.BLOCK, HookDecision.BLOCK),
    ],
)
async def test_command_transformer_exit_two_obeys_fail_policy(
    tmp_path: Path,
    fail_policy: FailPolicy,
    decision: HookDecision,
) -> None:
    script = tmp_path / "block.py"
    script.write_text("raise SystemExit(2)\n", encoding="utf-8")

    result = await execute_handler(
        CommandHookHandlerConfig(
            id="format",
            argv=["python", str(script)],
            outputTransform=True,
            failPolicy=fail_policy,
        ),
        _context(HookEventName.STOP).model_copy(
            update={"assistant_response": "candidate"},
        ),
        workspace_dir=tmp_path,
    )

    assert result.failed is True
    assert result.failure_type == "blocked_response"
    assert result.decision == decision


@pytest.mark.asyncio
async def test_command_cwd_escape_is_rejected(tmp_path: Path) -> None:
    handler = CommandHookHandlerConfig(
        id="escape",
        command="echo no",
        cwd=str(tmp_path.parent),
        fail_policy=FailPolicy.BLOCK,
    )

    with tenant_context(tenant_id="tenant-a", workspace_dir=tmp_path):
        result = await execute_handler(
            handler,
            _context(),
            workspace_dir=tmp_path,
        )

    assert result.failed is True
    assert result.decision == HookDecision.BLOCK
    assert "outside tenant workspace" in result.reason


@pytest.mark.asyncio
async def test_command_argv_executable_escape_is_rejected(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-hook"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    handler = CommandHookHandlerConfig(
        id="escape",
        argv=[str(outside)],
        fail_policy=FailPolicy.BLOCK,
    )

    result = await execute_handler(
        handler,
        _context(),
        workspace_dir=tmp_path,
    )

    assert result.failed is True
    assert result.decision == HookDecision.BLOCK
    assert "outside tenant workspace" in result.reason


@pytest.mark.asyncio
async def test_command_argv_nonexistent_absolute_escape_is_rejected(
    tmp_path: Path,
) -> None:
    handler = CommandHookHandlerConfig(
        id="escape",
        argv=["python", str(tmp_path.parent / "missing.py")],
        fail_policy=FailPolicy.BLOCK,
    )

    result = await execute_handler(
        handler,
        _context(),
        workspace_dir=tmp_path,
    )

    assert result.failed is True
    assert result.decision == HookDecision.BLOCK
    assert "outside tenant workspace" in result.reason


@pytest.mark.asyncio
async def test_command_shell_path_escape_is_rejected(tmp_path: Path) -> None:
    handler = CommandHookHandlerConfig(
        id="escape",
        command=f"cat {tmp_path.parent / 'secret.txt'}",
        fail_policy=FailPolicy.BLOCK,
    )

    result = await execute_handler(
        handler,
        _context(),
        workspace_dir=tmp_path,
    )

    assert result.failed is True
    assert result.decision == HookDecision.BLOCK
    assert "outside the allowed workspace" in result.reason


@pytest.mark.asyncio
async def test_command_shell_field_selects_requested_shell(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, payload):
            del payload
            return b"{}", b""

    async def fake_create_subprocess_shell(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.asyncio.create_subprocess_shell",
        fake_create_subprocess_shell,
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.shutil.which",
        lambda shell: f"/tenant/bin/{shell}",
    )
    handler = CommandHookHandlerConfig(
        id="shell",
        command="echo {}",
        shell="bash",
    )

    result = await execute_handler(
        handler,
        _context(),
        workspace_dir=tmp_path,
    )

    assert result.failed is False
    assert observed["kwargs"]["executable"] == "/tenant/bin/bash"


@pytest.mark.asyncio
async def test_command_handler_receives_tenant_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("swe.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.delenv("HOOK_TOKEN", raising=False)
    _write_scope_env(
        tmp_path,
        "tenant-a",
        "source-a",
        {"HOOK_TOKEN": "tenant-secret"},
    )
    script = tmp_path / "hook_env.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'hookSpecificOutput': {'additionalContext': os.environ.get('HOOK_TOKEN', '')}}))\n",
        encoding="utf-8",
    )
    handler = CommandHookHandlerConfig(id="env", argv=["python", str(script)])

    with tenant_context(
        tenant_id="tenant-a",
        source_id="source-a",
        workspace_dir=tmp_path,
    ):
        result = await execute_handler(
            handler,
            _context(),
            workspace_dir=tmp_path,
        )

    assert result.failed is False
    assert (
        result.output.hook_specific_output["additionalContext"]
        == "tenant-secret"
    )
    assert "HOOK_TOKEN" not in os.environ


@pytest.mark.asyncio
async def test_command_handler_env_overrides_tenant_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("swe.config.utils.WORKING_DIR", tmp_path)
    _write_scope_env(
        tmp_path,
        "tenant-a",
        "source-a",
        {"HOOK_TOKEN": "tenant-secret"},
    )
    observed = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, payload):
            del payload
            return b"{}", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        observed.update(kwargs.get("env") or {})
        return FakeProcess()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    handler = CommandHookHandlerConfig(
        id="env",
        argv=["python", str(tmp_path / "noop.py")],
        env={"HOOK_TOKEN": "handler-secret"},
    )
    (tmp_path / "noop.py").write_text("print('{}')\n", encoding="utf-8")

    with tenant_context(
        tenant_id="tenant-a",
        source_id="source-a",
        workspace_dir=tmp_path,
    ):
        result = await execute_handler(
            handler,
            _context(),
            workspace_dir=tmp_path,
        )

    assert result.failed is False
    assert observed["HOOK_TOKEN"] == "handler-secret"


@pytest.mark.asyncio
async def test_command_handler_env_excludes_system_configuration_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SWE_DB_ACCESS", "backend-secret")
    monkeypatch.setenv("SWE_SECRET_DIR", str(tmp_path / ".secret"))
    observed = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, payload):
            del payload
            return b"{}", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        observed.update(kwargs.get("env") or {})
        return FakeProcess()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    handler = CommandHookHandlerConfig(
        id="env",
        argv=["python", str(tmp_path / "noop.py")],
        env={
            "SWE_ZHAOHU_CLIENT_SECRET_POSEIDON": "handler-secret",
            "HOOK_TOKEN": "handler-token",
        },
    )
    (tmp_path / "noop.py").write_text("print('{}')\n", encoding="utf-8")

    result = await execute_handler(
        handler,
        _context(),
        workspace_dir=tmp_path,
    )

    assert result.failed is False
    assert observed["HOOK_TOKEN"] == "handler-token"
    assert "SWE_DB_ACCESS" not in observed
    assert "SWE_SECRET_DIR" not in observed
    assert "SWE_ZHAOHU_CLIENT_SECRET_POSEIDON" not in observed


@pytest.mark.asyncio
async def test_command_handler_env_injects_runtime_claims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, payload):
            del payload
            return b"{}", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        observed.update(kwargs.get("env") or {})
        return FakeProcess()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    handler = CommandHookHandlerConfig(
        id="env",
        argv=["python", str(tmp_path / "noop.py")],
        env={"SWE_TENANT_ID": "fake-tenant"},
    )
    (tmp_path / "noop.py").write_text("print('{}')\n", encoding="utf-8")
    context = _context().model_copy(
        update={
            "effective_tenant_id": encode_scope_id("tenant-a", "source-a"),
            "chat_id": "chat-uuid-1",
            "trace_id": "trace-1",
        },
    )

    result = await execute_handler(
        handler,
        context,
        workspace_dir=tmp_path,
    )

    assert result.failed is False
    assert observed["SWE_TENANT_ID"] == "tenant-a"
    assert observed["SWE_SOURCE_ID"] == "source-a"
    assert observed["SWE_RUNTIME_SCOPE_ID"] == encode_scope_id(
        "tenant-a",
        "source-a",
    )
    assert observed["SWE_SESSION_ID"] == "session-1"
    assert observed["SWE_CHAT_ID"] == "chat-uuid-1"
    assert observed["SWE_TRACE_ID"] == "trace-1"


@pytest.mark.asyncio
async def test_http_handler_maps_2xx_json_and_409_block(monkeypatch) -> None:
    responses = [
        httpx.Response(
            200,
            json={
                "hookSpecificOutput": {
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "ok",
                },
            },
        ),
        httpx.Response(409, text="blocked remotely"),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.httpx.AsyncClient",
        FakeClient,
    )

    allow = await execute_handler(
        HttpHookHandlerConfig(id="http-allow", url="https://hooks.example/a"),
        _context(),
        workspace_dir=Path("/tmp/tenant-a/workspaces/default"),
    )
    block = await execute_handler(
        HttpHookHandlerConfig(id="http-block", url="https://hooks.example/b"),
        _context(),
        workspace_dir=Path("/tmp/tenant-a/workspaces/default"),
    )

    assert allow.decision == HookDecision.ALLOW
    assert allow.reason == "ok"
    assert block.decision == HookDecision.BLOCK
    assert "blocked remotely" in block.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [409, 422])
async def test_http_transformer_status_failure_obeys_fail_policy(
    monkeypatch,
    status_code: int,
) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return httpx.Response(status_code, text="formatter rejected")

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.httpx.AsyncClient",
        FakeClient,
    )
    result = await execute_handler(
        HttpHookHandlerConfig(
            id="format",
            url="https://hooks.example/format",
            outputTransform=True,
            failPolicy=FailPolicy.ALLOW,
        ),
        _context(HookEventName.STOP).model_copy(
            update={"assistant_response": "candidate"},
        ),
        workspace_dir=Path("/tmp/tenant-a/workspaces/default"),
    )

    assert result.failed is True
    assert result.decision == HookDecision.NONE
    assert result.failure_type == "blocked_response"


@pytest.mark.asyncio
async def test_transformer_debug_log_excludes_candidate_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate = "do-not-log-this-candidate"
    script = tmp_path / "format.py"
    script.write_text(
        "import json\nprint(json.dumps({'decision':'allow','reason':'ok'}))\n",
        encoding="utf-8",
    )

    logged: dict[str, object] = {}
    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.logger.debug",
        lambda _message, *args: logged.update(context=args[-1]),
    )
    await execute_handler(
        CommandHookHandlerConfig(
            id="format",
            argv=["python", str(script)],
            outputTransform=True,
        ),
        _context(HookEventName.STOP).model_copy(
            update={"assistant_response": candidate},
        ),
        workspace_dir=tmp_path,
    )

    logged_context = logged["context"]
    assert isinstance(logged_context, dict)
    assert logged_context["assistant_response"] == {
        "length": len(candidate),
        "sha256": hashlib.sha256(
            candidate.encode("utf-8"),
        ).hexdigest(),
    }


@pytest.mark.asyncio
async def test_http_handler_resolves_header_secret_from_effective_tenant(
    monkeypatch,
) -> None:
    observed = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            observed.update(kwargs.get("headers") or {})
            return httpx.Response(200, json={})

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.httpx.AsyncClient",
        FakeClient,
    )
    tenant_calls = []

    def fake_get_tenant_env(key, tenant_id=None, default=None):
        tenant_calls.append((key, tenant_id))
        return "tenant-secret"

    monkeypatch.setattr(
        "swe.config.utils.get_tenant_env",
        fake_get_tenant_env,
    )

    result = await execute_handler(
        HttpHookHandlerConfig(
            id="http-secret",
            url="https://hooks.example/secret",
            headerSecretRefs={"Authorization": "HOOK_TOKEN"},
        ),
        _context(),
        workspace_dir=Path("/tmp/tenant-a/workspaces/default"),
    )

    assert result.failed is False
    assert observed["Authorization"] == "tenant-secret"
    assert tenant_calls == [("HOOK_TOKEN", "tenant-a")]


@pytest.mark.asyncio
async def test_http_handler_injects_canonical_runtime_claim_headers(
    monkeypatch,
) -> None:
    observed = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            observed.update(kwargs.get("headers") or {})
            return httpx.Response(200, json={})

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.httpx.AsyncClient",
        FakeClient,
    )
    context = _context().model_copy(
        update={
            "effective_tenant_id": encode_scope_id("tenant-a", "source-a"),
            "chat_id": "chat-uuid-1",
            "trace_id": "trace-1",
        },
    )

    result = await execute_handler(
        HttpHookHandlerConfig(
            id="http-claims",
            url="https://hooks.example/claims",
            headers={
                "X-Swe-Tenant-Id": "fake-tenant",
                "tenantid": "fake-tenant",
                "X-Static": "static",
            },
        ),
        context,
        workspace_dir=Path("/tmp/tenant-a/workspaces/default"),
    )

    assert result.failed is False
    assert observed == {
        "X-Static": "static",
        "x-swe-tenant-id": "tenant-a",
        "x-swe-source-id": "source-a",
        "x-swe-runtime-scope-id": encode_scope_id("tenant-a", "source-a"),
        "x-swe-session-id": "session-1",
        "x-swe-chat-id": "chat-uuid-1",
        "x-swe-trace-id": "trace-1",
    }


@pytest.mark.parametrize(
    ("text", "decision"),
    [
        ('{"decision":"allow","reason":"ok"}', HookDecision.ALLOW),
        ('{"decision":"deny","reason":"no"}', HookDecision.DENY),
        ('{"decision":"block","reason":"stop"}', HookDecision.BLOCK),
    ],
)
def test_prompt_judgment_output_maps_valid_decisions(
    text: str,
    decision: HookDecision,
) -> None:
    result = normalize_prompt_judgment_output(
        handler_id="policy",
        order=3,
        text=text,
    )

    assert result.decision == decision
    assert result.reason
    assert result.order == 3


def test_prompt_pre_tool_use_stop_is_terminal() -> None:
    result = normalize_prompt_judgment_output(
        handler_id="policy",
        order=3,
        text='{"decision":"stop","reason":"end this run"}',
        event_name=HookEventName.PRE_TOOL_USE,
    )

    assert result.decision == HookDecision.STOP
    assert result.reason == "end this run"


def test_prompt_judgment_output_repairs_malformed_json() -> None:
    result = normalize_prompt_judgment_output(
        handler_id="policy",
        order=3,
        text="{decision: allow, reason: ok}",
    )

    assert result.decision == HookDecision.ALLOW
    assert result.reason == "ok"


@pytest.mark.parametrize(
    ("text", "decision"),
    [
        ('{"decision":"allow","reason":"ok"}', HookDecision.ALLOW),
        ('{"decision":"block","reason":"继续完成测试"}', HookDecision.BLOCK),
    ],
)
def test_stop_prompt_judgment_accepts_gate_decisions(
    text: str,
    decision: HookDecision,
) -> None:
    result = normalize_prompt_judgment_output(
        handler_id="policy",
        order=0,
        text=text,
        event_name=HookEventName.STOP,
    )

    assert result.decision == decision
    assert result.reason


@pytest.mark.parametrize(
    "text",
    [
        '{"decision":"deny","reason":"no"}',
        '{"decision":"stop","reason":"end this run"}',
        '{"decision":"ask","reason":"review"}',
        '{"decision":"allow","reason":"ok","continue":false}',
        (
            '{"decision":"allow","reason":"ok",'
            '"hookSpecificOutput":{"permissionDecision":"ask"}}'
        ),
        (
            '{"decision":"allow","reason":"ok",'
            '"hookSpecificOutput":{"updatedInput":{"command":"echo hi"}}}'
        ),
        (
            '{"decision":"allow","reason":"ok",'
            '"hookSpecificOutput":{"sessionTitle":"Done"}}'
        ),
        (
            '{"decision":"allow","reason":"ok",'
            '"hookSpecificOutput":{"additionalContext":"extra"}}'
        ),
    ],
)
def test_stop_prompt_judgment_rejects_unsupported_outputs(
    text: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_prompt_judgment_output(
            handler_id="policy",
            order=0,
            text=text,
            event_name=HookEventName.STOP,
        )


@pytest.mark.parametrize(
    ("raw_output", "decision"),
    [
        ({"decision": "allow", "reason": "ok"}, HookDecision.ALLOW),
        ({"decision": "block", "reason": "run tests"}, HookDecision.BLOCK),
    ],
)
def test_stop_hook_output_accepts_gate_decisions(
    raw_output: dict,
    decision: HookDecision,
) -> None:
    result = normalize_hook_output(
        handler_id="policy",
        order=0,
        raw_output=raw_output,
        event_name=HookEventName.STOP,
    )

    assert result.decision == decision
    assert result.reason


@pytest.mark.parametrize(
    "raw_output",
    [
        {},
        {"decision": "deny", "reason": "no"},
        {"decision": "ask", "reason": "review"},
        {"continue": False, "stopReason": "stop"},
        {"continue": True},
        {"stopReason": "stop"},
        {"systemMessage": "hidden note"},
        {"suppressOutput": True},
        {"hookSpecificOutput": {"permissionDecision": "ask"}},
        {"hookSpecificOutput": {"permissionDecisionReason": "review"}},
        {"hookSpecificOutput": {"updatedInput": {"command": "echo hi"}}},
        {"hookSpecificOutput": {"sessionTitle": "Done"}},
        {"hookSpecificOutput": {"additionalContext": "extra"}},
    ],
)
def test_stop_hook_output_rejects_unsupported_fields(
    raw_output: dict,
) -> None:
    with pytest.raises(ValueError):
        normalize_hook_output(
            handler_id="policy",
            order=0,
            raw_output=raw_output,
            event_name=HookEventName.STOP,
        )


@pytest.mark.parametrize(
    ("raw_output", "replacement"),
    [
        (
            {
                "decision": "allow",
                "reason": "formatted",
                "hookSpecificOutput": {"replacementText": "final text"},
            },
            "final text",
        ),
        ({"decision": "allow", "reason": "passed through"}, None),
    ],
)
def test_stop_transformer_accepts_replacement_or_pass_through(
    raw_output: dict,
    replacement: str | None,
) -> None:
    result = normalize_hook_output(
        handler_id="format",
        order=0,
        raw_output=raw_output,
        event_name=HookEventName.STOP,
        output_transform=True,
    )

    assert result.decision == HookDecision.ALLOW
    assert result.replacement_text == replacement


@pytest.mark.parametrize(
    "raw_output",
    [
        {"decision": "block", "reason": "no"},
        {
            "decision": "allow",
            "hookSpecificOutput": {"replacementText": "  "},
        },
        {
            "decision": "allow",
            "hookSpecificOutput": {"replacementText": 1},
        },
        {
            "decision": "allow",
            "hookSpecificOutput": {"additionalContext": "nope"},
        },
    ],
)
def test_stop_transformer_rejects_invalid_output(raw_output: dict) -> None:
    with pytest.raises(ValueError):
        normalize_hook_output(
            handler_id="format",
            order=0,
            raw_output=raw_output,
            event_name=HookEventName.STOP,
            output_transform=True,
        )


def test_stop_transformer_rejects_unknown_top_level_output() -> None:
    with pytest.raises(ValueError, match="unsupported output fields"):
        normalize_hook_output(
            handler_id="format",
            order=0,
            raw_output={
                "decision": "allow",
                "reason": "formatted",
                "unexpectedEffect": "nope",
            },
            event_name=HookEventName.STOP,
            output_transform=True,
        )


def test_stop_prompt_transformer_accepts_only_its_extended_contract() -> None:
    result = normalize_prompt_judgment_output(
        handler_id="format",
        order=0,
        text=(
            '{"decision":"allow","reason":"formatted",'
            '"hookSpecificOutput":{"replacementText":"final text"}}'
        ),
        event_name=HookEventName.STOP,
        output_transform=True,
    )

    assert result.replacement_text == "final text"

    with pytest.raises(ValueError):
        normalize_prompt_judgment_output(
            handler_id="format",
            order=0,
            text=(
                '{"decision":"block","reason":"no",'
                '"hookSpecificOutput":{"replacementText":"final text"}}'
            ),
            event_name=HookEventName.STOP,
            output_transform=True,
        )


@pytest.mark.parametrize(
    ("raw_output", "reason"),
    [
        ({"decision": "stop", "reason": "end this run"}, "end this run"),
        ({"decision": "stop", "reason": ""}, "Hook requested stop"),
    ],
)
def test_generic_canonical_stop_is_terminal(
    raw_output: dict,
    reason: str,
) -> None:
    result = normalize_hook_output(
        handler_id="policy",
        order=0,
        raw_output=raw_output,
        event_name=HookEventName.PRE_TOOL_USE,
    )

    assert result.decision == HookDecision.STOP
    assert result.reason == reason


@pytest.mark.parametrize(
    "event_name",
    [
        HookEventName.SESSION_START,
        HookEventName.USER_PROMPT_SUBMIT,
        HookEventName.PRE_TOOL_USE,
    ],
)
def test_non_stop_prompt_judgment_still_accepts_deny(
    event_name: HookEventName,
) -> None:
    result = normalize_prompt_judgment_output(
        handler_id="policy",
        order=0,
        text='{"decision":"deny","reason":"policy denied"}',
        event_name=event_name,
    )

    assert result.decision == HookDecision.DENY
    assert result.reason == "policy denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_policy", "expected_decision"),
    [
        (FailPolicy.BLOCK, HookDecision.BLOCK),
        (FailPolicy.ALLOW, HookDecision.NONE),
    ],
)
async def test_stop_prompt_handler_invalid_output_uses_fail_policy(
    monkeypatch,
    tmp_path: Path,
    fail_policy: FailPolicy,
    expected_decision: HookDecision,
) -> None:
    async def fake_model(messages):
        del messages
        return '{"decision":"deny","reason":"not a gate decision"}'

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.create_model_and_formatter",
        lambda agent_id=None, trace_context=None: (
            fake_model,
            object(),
        ),
    )

    result = await execute_handler(
        PromptHookHandlerConfig(
            id="policy",
            prompt="检查是否可以停止。",
            failPolicy=fail_policy,
        ),
        _context(HookEventName.STOP),
        workspace_dir=tmp_path,
    )

    assert result.failed is True
    assert result.failure_type == "execution_error"
    assert result.decision == expected_decision


@pytest.mark.parametrize(
    "text",
    [
        "not-json",
        "[]",
        '{"decision":"allow"}',
        '{"reason":"ok"}',
        '{"decision":"ask","reason":"review"}',
        '{"decision":"allow","reason":1}',
        '{"decision":"allow","reason":"   "}',
        '{"decision":"allow","reason":"ok","extra":true}',
        '{"decision":"allow","reason":"ok","continue":false}',
        '{"decision":"allow","reason":"' + ("x" * 2001) + '"}',
    ],
)
def test_prompt_judgment_output_rejects_invalid_shapes(text: str) -> None:
    with pytest.raises(ValueError):
        normalize_prompt_judgment_output(
            handler_id="policy",
            order=0,
            text=text,
        )


@pytest.mark.asyncio
async def test_prompt_handler_binds_context_and_redacts_model_input(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed = {}

    async def fake_model(messages):
        observed["messages"] = messages
        return '{"decision":"deny","reason":"secret request"}'

    def fake_create_model_and_formatter(agent_id=None, trace_context=None):
        observed["agent_id"] = agent_id
        observed["trace_context"] = trace_context
        from swe.config.context import (
            get_current_source_id,
            get_current_tenant_id,
            get_current_user_id,
            get_current_workspace_dir,
        )

        observed["tenant_id"] = get_current_tenant_id()
        observed["user_id"] = get_current_user_id()
        observed["source_id"] = get_current_source_id()
        observed["workspace_dir"] = get_current_workspace_dir()
        return fake_model, object()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    context = _context()
    context.source_id = "web"
    context.tool_input = {"api_key": "sk-secret", "cmd": "echo ok"}
    handler = PromptHookHandlerConfig(
        id="policy",
        prompt="Reject leaked secrets.",
    )

    result = await execute_handler(
        handler,
        context,
        workspace_dir=tmp_path,
    )

    assert result.decision == HookDecision.DENY
    assert observed["agent_id"] == "agent-1"
    assert observed["tenant_id"] == "tenant-a"
    assert observed["user_id"] == "user-1"
    assert observed["source_id"] == "web"
    assert observed["workspace_dir"] == Path(context.workspace_dir)
    assert observed["trace_context"]["trace_id"] == context.trace_id
    assert observed["trace_context"]["session_id"] == context.session_id
    prompt_text = observed["messages"][0]["content"]
    assert "Reject leaked secrets." in prompt_text
    assert "HookContext JSON" in prompt_text
    assert "sk-secret" not in prompt_text
    assert "[REDACTED]" in prompt_text


@pytest.mark.asyncio
async def test_prompt_handler_extracts_streaming_delta_and_cumulative_chunks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    responses = [
        [
            {"content": [{"type": "text", "text": '{"decision":"all'}]},
            {"content": [{"type": "text", "text": 'ow","reason":"ok"}'}]},
        ],
        [
            {"content": [{"type": "text", "text": '{"decision":"allow"'}]},
            {
                "content": [
                    {
                        "type": "text",
                        "text": '{"decision":"allow","reason":"ok"}',
                    },
                ],
            },
        ],
    ]

    async def fake_model(_messages):
        items = responses.pop(0)

        async def stream():
            for item in items:
                yield item

        return stream()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.create_model_and_formatter",
        lambda agent_id=None, trace_context=None: (
            fake_model,
            object(),
        ),
    )
    handler = PromptHookHandlerConfig(id="policy", prompt="Allow safe work.")

    first = await execute_handler(handler, _context(), workspace_dir=tmp_path)
    second = await execute_handler(handler, _context(), workspace_dir=tmp_path)

    assert first.decision == HookDecision.ALLOW
    assert second.decision == HookDecision.ALLOW


@pytest.mark.asyncio
async def test_prompt_handler_timeout_closes_stream(
    monkeypatch,
    tmp_path: Path,
) -> None:
    closed = {"value": False}

    class FakeStream:
        def __init__(self):
            self._index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index == 0:
                self._index += 1
                return {
                    "content": [
                        {"type": "text", "text": '{"decision":"allow"'},
                    ],
                }
            await asyncio.sleep(1)
            return {"content": [{"type": "text", "text": ',"reason":"ok"}'}]}

        async def aclose(self):
            closed["value"] = True

    async def fake_model(_messages):
        return FakeStream()

    monkeypatch.setattr(
        "swe.agents.hook_runtime.executor.create_model_and_formatter",
        lambda agent_id=None, trace_context=None: (
            fake_model,
            object(),
        ),
    )
    handler = PromptHookHandlerConfig(
        id="policy",
        prompt="Allow safe work.",
        timeout=0.01,
    )

    result = await execute_handler(handler, _context(), workspace_dir=tmp_path)

    assert result.failed is True
    assert result.decision == HookDecision.BLOCK
    assert result.failure_type == "timeout"
    assert closed["value"] is True


@pytest.mark.asyncio
async def test_runtime_emits_prompt_command_and_http_handlers_concurrently(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    events = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        events.append(("start", handler.id))
        await asyncio.sleep(0.01)
        events.append(("end", handler.id))
        return HookHandlerResult(
            handler_id=handler.id,
            order=0,
            decision=HookDecision.ALLOW,
            reason=handler.id,
        )

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )

    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.PRE_TOOL_USE: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(id="cmd", command="echo"),
                            HttpHookHandlerConfig(
                                id="http",
                                url="https://hooks.example/http",
                            ),
                            PromptHookHandlerConfig(
                                id="prompt",
                                prompt="Reject unsafe actions.",
                            ),
                        ],
                    ),
                ],
            },
        ),
    )

    await runtime.emit(_context(), workspace_dir=Path("/tmp"))

    assert events[:3] == [
        ("start", "cmd"),
        ("start", "http"),
        ("start", "prompt"),
    ]
    assert events[-3:] == [("end", "cmd"), ("end", "http"), ("end", "prompt")]


@pytest.mark.asyncio
async def test_runtime_refreshes_changed_skill_hooks_before_event_plan(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from swe.agents.hook_runtime.models import (
        HookSessionOverlay,
        HookSessionState,
    )
    from swe.agents.hook_runtime.runtime import HookRuntime
    from swe.agents.hook_runtime.skill_loader import (
        load_skill_hooks_for_session,
    )

    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "check.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    hooks_path = skill_root / "hooks" / "hooks.json"

    def write_config(handler_id: str) -> None:
        hooks_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "events": {
                        "PreToolUse": [
                            {
                                "hooks": [
                                    {
                                        "id": handler_id,
                                        "type": "command",
                                        "argv": ["python", "scripts/check.py"],
                                    },
                                ],
                            },
                        ],
                    },
                },
            ),
            encoding="utf-8",
        )

    write_config("old")
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    write_config("new")
    executed_handler_ids: list[str] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del context, workspace_dir
        executed_handler_ids.append(handler.id)
        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )
    runtime = HookRuntime(
        session_overlay=HookSessionOverlay.model_validate(
            state.model_dump(mode="json", by_alias=True),
        ),
    )

    await runtime.emit(_context(), workspace_dir=tmp_path)

    assert executed_handler_ids == ["skill:xlsx:new"]


@pytest.mark.asyncio
async def test_runtime_does_not_restore_once_record_from_replaced_handler(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from swe.agents.hook_runtime.models import (
        HookSessionOverlay,
        HookSessionState,
    )
    from swe.agents.hook_runtime.runtime import HookRuntime
    from swe.agents.hook_runtime.skill_loader import (
        load_skill_hooks_for_session,
    )

    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "check.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    hooks_path = skill_root / "hooks" / "hooks.json"

    def write_config(event: str) -> None:
        hooks_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "events": {
                        event: [
                            {
                                "hooks": [
                                    {
                                        "id": "once",
                                        "type": "command",
                                        "argv": ["python", "scripts/check.py"],
                                        "once": True,
                                    },
                                ],
                            },
                        ],
                    },
                },
            ),
            encoding="utf-8",
        )

    write_config("PreToolUse")
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del handler, context, workspace_dir
        started.set()
        await release.wait()
        return HookHandlerResult(handler_id="skill:xlsx:once", order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )
    runtime = HookRuntime(
        session_overlay=HookSessionOverlay.model_validate(
            state.model_dump(mode="json", by_alias=True),
        ),
    )

    old_event = asyncio.create_task(
        runtime.emit(_context(), workspace_dir=tmp_path),
    )
    await started.wait()
    write_config("Stop")
    await runtime.emit(_context(), workspace_dir=tmp_path)
    release.set()
    await old_event

    assert runtime.session_overlay.once_executed == {}


def test_runtime_refreshes_skill_hooks_before_stop_buffer_decision(
    tmp_path: Path,
) -> None:
    from swe.agents.hook_runtime.models import (
        HookSessionOverlay,
        HookSessionState,
    )
    from swe.agents.hook_runtime.runtime import HookRuntime
    from swe.agents.hook_runtime.skill_loader import (
        load_skill_hooks_for_session,
    )

    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "check.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    hooks_path = skill_root / "hooks" / "hooks.json"

    def write_config(output_transform: bool) -> None:
        hooks_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "events": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "id": "finalize",
                                        "type": "command",
                                        "argv": ["python", "scripts/check.py"],
                                        "outputTransform": output_transform,
                                    },
                                ],
                            },
                        ],
                    },
                },
            ),
            encoding="utf-8",
        )

    write_config(False)
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    runtime = HookRuntime(
        session_overlay=HookSessionOverlay.model_validate(
            state.model_dump(mode="json", by_alias=True),
        ),
    )
    stop_context = _context(HookEventName.STOP)

    assert (
        runtime.requires_stop_output_buffer(
            stop_context,
            workspace_dir=tmp_path,
        )
        is False
    )

    write_config(True)

    assert (
        runtime.requires_stop_output_buffer(
            stop_context,
            workspace_dir=tmp_path,
        )
        is True
    )


@pytest.mark.asyncio
async def test_runtime_stop_executes_handlers_and_returns_gate_result(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    executed_handler_ids: list[str] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del context, workspace_dir
        executed_handler_ids.append(handler.id)
        return HookHandlerResult(
            handler_id=handler.id,
            order=0,
            output=HookOutput(decision="block", reason="completion blocked"),
            decision=HookDecision.BLOCK,
            reason="completion blocked",
        )

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )
    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.STOP: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(
                                id="stop-observer",
                                command="echo",
                            ),
                        ],
                    ),
                ],
            },
        ),
    )

    result = await runtime.emit(
        _context(HookEventName.STOP),
        workspace_dir=Path("/tmp"),
    )

    assert executed_handler_ids == ["stop-observer"]
    assert result.decision == HookDecision.BLOCK
    assert result.reason == "completion blocked"
    assert result.additional_context == []
    assert result.hook_specific_outputs == {}
    assert result.permission_decisions == []
    assert result.updated_input is None
    assert result.session_title is None
    assert result.suppress_output is False
    assert result.system_messages == []


@pytest.mark.asyncio
async def test_runtime_stop_finalization_transforms_serially_then_validates(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    seen: list[tuple[str, str | None]] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del workspace_dir
        seen.append((handler.id, context.assistant_response))
        replacements = {
            "tenant-format": "tenant text",
            "agent-format": "agent text",
        }
        return HookHandlerResult(
            handler_id=handler.id,
            order=0,
            decision=(
                HookDecision.BLOCK
                if handler.id == "validator"
                else HookDecision.ALLOW
            ),
            reason=(
                "final validation" if handler.id == "validator" else "format"
            ),
            replacement_text=replacements.get(handler.id),
        )

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )
    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.STOP: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(
                                id="tenant-format",
                                command="echo",
                                outputTransform=True,
                            ),
                        ],
                    ),
                ],
            },
        ),
        agent_config=HookConfig(
            enabled=True,
            events={
                HookEventName.STOP: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(
                                id="agent-format",
                                command="echo",
                                outputTransform=True,
                            ),
                            CommandHookHandlerConfig(
                                id="validator",
                                command="echo",
                            ),
                        ],
                    ),
                ],
            },
        ),
    )

    result = await runtime.emit_stop_finalization(
        _context(HookEventName.STOP).model_copy(
            update={"assistant_response": "candidate"},
        ),
        workspace_dir=Path("/tmp"),
        max_transform_seconds=30,
    )

    assert seen == [
        ("tenant-format", "candidate"),
        ("agent-format", "tenant text"),
        ("validator", "agent text"),
    ]
    assert result.final_response == "agent text"
    assert result.validation_result.decision == HookDecision.BLOCK
    assert result.transformation_failed is False


@pytest.mark.asyncio
async def test_runtime_stop_finalization_stops_on_blocking_transform_failure(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    executed: list[str] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del context, workspace_dir
        executed.append(handler.id)
        return HookHandlerResult(
            handler_id=handler.id,
            order=0,
            decision=HookDecision.BLOCK,
            reason="formatter unavailable",
            failed=True,
            failure_type="timeout",
        )

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )
    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.STOP: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(
                                id="blocking-format",
                                command="echo",
                                outputTransform=True,
                                failPolicy=FailPolicy.BLOCK,
                            ),
                            CommandHookHandlerConfig(
                                id="never-runs",
                                command="echo",
                                outputTransform=True,
                            ),
                            CommandHookHandlerConfig(
                                id="never-validates",
                                command="echo",
                            ),
                        ],
                    ),
                ],
            },
        ),
    )

    result = await runtime.emit_stop_finalization(
        _context(HookEventName.STOP).model_copy(
            update={"assistant_response": "candidate"},
        ),
        workspace_dir=Path("/tmp"),
        max_transform_seconds=30,
    )

    assert executed == ["blocking-format"]
    assert result.final_response == "candidate"
    assert result.transformation_failed is True
    assert result.transformation_failure_reason == "formatter unavailable"


@pytest.mark.asyncio
async def test_runtime_stop_finalization_stops_when_last_transformer_exceeds_budget(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del handler, context, workspace_dir
        return HookHandlerResult(
            handler_id="format",
            order=0,
            decision=HookDecision.ALLOW,
        )

    monotonic_calls = 0

    def fake_monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 0.0 if monotonic_calls <= 2 else 2.0

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.time.monotonic",
        fake_monotonic,
    )
    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.STOP: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(
                                id="format",
                                command="echo",
                                outputTransform=True,
                            ),
                            CommandHookHandlerConfig(
                                id="validator",
                                command="echo",
                            ),
                        ],
                    ),
                ],
            },
        ),
    )

    result = await runtime.emit_stop_finalization(
        _context(HookEventName.STOP).model_copy(
            update={"assistant_response": "candidate"},
        ),
        workspace_dir=Path("/tmp"),
        max_transform_seconds=1,
    )

    assert result.transformation_failed is True
    assert result.validation_result.decision == HookDecision.NONE


@pytest.mark.asyncio
async def test_runtime_stop_preserves_fail_policy_block_effect(
    tmp_path: Path,
) -> None:
    """Stop handler 的阻断失败必须交给 runner 结束当前请求。"""
    from swe.agents.hook_runtime.runtime import HookRuntime

    script = tmp_path / "fail_stop_observer.py"
    script.write_text("raise SystemExit(1)\n", encoding="utf-8")
    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.STOP: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(
                                id="failing-stop-observer",
                                argv=["python", str(script)],
                                fail_policy=FailPolicy.BLOCK,
                            ),
                        ],
                    ),
                ],
            },
        ),
    )

    result = await runtime.emit(
        _context(HookEventName.STOP),
        workspace_dir=tmp_path,
    )

    assert result.decision == HookDecision.BLOCK
    assert result.reason


@pytest.mark.asyncio
async def test_runtime_injects_conversation_snapshot_per_handler(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    seen_payloads: dict[str, dict] = {}

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del workspace_dir
        seen_payloads[handler.id] = context.to_handler_payload()
        return HookHandlerResult(handler_id=handler.id, order=0)

    async def snapshot_provider():
        return {
            "messages": [
                {
                    "role": "user",
                    "content": "first",
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "visible"},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "read_file",
                            "input": {"path": "README.md"},
                        },
                    ],
                },
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "tool_result",
                            "id": "tool-1",
                            "name": "read_file",
                            "output": "ok",
                        },
                    ],
                },
            ],
            "meta": {
                "reasoning_omitted": True,
                "media_content_omitted": False,
            },
        }

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )

    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.PRE_TOOL_USE: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(
                                id="with-snapshot",
                                command="echo",
                                includeConversationSnapshot=True,
                                conversationSnapshotLimit=2,
                            ),
                            CommandHookHandlerConfig(
                                id="without-snapshot",
                                command="echo",
                            ),
                        ],
                    ),
                ],
            },
        ),
    )

    await runtime.emit(
        _context(),
        workspace_dir=Path("/tmp"),
        conversation_snapshot_provider=snapshot_provider,
    )

    assert "conversation_snapshot" not in seen_payloads["without-snapshot"]
    snapshot_payload = seen_payloads["with-snapshot"]
    assert [
        item["role"] for item in snapshot_payload["conversation_snapshot"]
    ] == [
        "assistant",
        "system",
    ]
    assert snapshot_payload["conversation_snapshot"][0]["content"] == [
        {"type": "text", "text": "visible"},
        {
            "type": "tool_use",
            "id": "tool-1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    ]
    assert snapshot_payload["conversation_snapshot_meta"] == {
        "included_messages": 2,
        "omitted_messages": 1,
        "limit": 2,
        "reasoning_omitted": True,
        "media_content_omitted": False,
    }


@pytest.mark.asyncio
async def test_runtime_marks_conversation_snapshot_unavailable(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    seen_payloads: list[dict] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        del handler, workspace_dir
        seen_payloads.append(context.to_handler_payload())
        return HookHandlerResult(handler_id="cmd", order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )

    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.PRE_TOOL_USE: [
                    HookMatcherGroupConfig(
                        hooks=[
                            CommandHookHandlerConfig(
                                id="cmd",
                                command="echo",
                                includeConversationSnapshot=True,
                            ),
                        ],
                    ),
                ],
            },
        ),
    )

    await runtime.emit(_context(), workspace_dir=Path("/tmp"))

    assert seen_payloads[0]["conversation_snapshot"] == []
    assert seen_payloads[0]["conversation_snapshot_meta"] == {
        "included_messages": 0,
        "omitted_messages": 0,
        "limit": 50,
        "unavailable": True,
        "unavailable_reason": "agent_memory_unavailable",
    }


@pytest.mark.asyncio
async def test_runtime_logs_hook_telemetry_for_executed_handlers(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    log_messages: list[str] = []

    async def fake_execute_handler(handler, context, *, workspace_dir):
        if handler.id == "policy":
            return HookHandlerResult(
                handler_id=handler.id,
                order=0,
                decision=HookDecision.ASK,
                reason="approval required for token abc123",
                output=HookOutput(
                    system_message="raw system should not log",
                    hookSpecificOutput={
                        "permissionDecision": "ask",
                        "permissionDecisionReason": "approval required",
                        "additionalContext": "raw context should not log",
                        "updatedInput": {"cmd": "echo changed"},
                    },
                ),
            )
        return HookHandlerResult(
            handler_id=handler.id,
            order=0,
            failed=True,
            failure_type="timeout",
            reason="handler timed out",
        )

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler,
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.logger.info",
        lambda message, *args: log_messages.append(message % args),
    )

    runtime = HookRuntime(
        tenant_config=HookConfig(
            enabled=True,
            events={
                HookEventName.PRE_TOOL_USE: [
                    HookMatcherGroupConfig(
                        id="guards",
                        hooks=[
                            CommandHookHandlerConfig(
                                id="policy",
                                command="echo",
                            ),
                            HttpHookHandlerConfig(
                                id="notify",
                                url="https://hooks.example/notify",
                            ),
                        ],
                    ),
                ],
            },
        ),
    )
    context_data = _context().model_dump(mode="json")
    context_data.update(
        trace_id="trace-1",
        prompt="raw prompt should not log",
    )
    context = HookContext(**context_data)

    await runtime.emit(context, workspace_dir=Path("/tmp"))

    messages = [
        message
        for message in log_messages
        if message.startswith("HOOK_TELEMETRY ")
    ]
    assert len(messages) == 1
    payload = json.loads(messages[0].removeprefix("HOOK_TELEMETRY "))

    assert payload["schema"] == "hook_telemetry.v1"
    assert payload["hook_event_name"] == "PreToolUse"
    assert payload["trace_id"] == "trace-1"
    assert payload["source_id"] == "source-a"
    assert payload["execution_state"] == "executed"
    assert payload["handler_count"] == 2
    assert payload["decision"] == "ask"
    assert payload["blocked"] is False
    assert payload["has_updated_input"] is True
    assert payload["updated_input_handler_ids"] == ["policy"]
    assert payload["has_additional_context"] is True
    assert payload["additional_context_handler_ids"] == ["policy"]
    assert payload["has_system_messages"] is True
    assert payload["system_message_handler_ids"] == ["policy"]
    assert payload["permission_decisions"] == [
        {
            "handler_id": "policy",
            "decision": "ask",
            "reason_preview": "approval required",
        },
    ]
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0
    assert [
        (item["handler_id"], item["group_id"], item["type"])
        for item in payload["handlers"]
    ] == [
        ("policy", "guards", "command"),
        ("notify", "guards", "http"),
    ]
    assert all(
        isinstance(item["duration_ms"], int) for item in payload["handlers"]
    )
    assert payload["handlers"][1]["failed"] is True
    assert payload["handlers"][1]["failure_type"] == "timeout"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "raw prompt should not log" not in serialized
    assert "raw context should not log" not in serialized
    assert "raw system should not log" not in serialized
    assert "echo changed" not in serialized
    assert "https://hooks.example/notify" not in serialized


@pytest.mark.asyncio
async def test_runtime_does_not_log_hook_telemetry_without_handlers(
    monkeypatch,
) -> None:
    from swe.agents.hook_runtime.runtime import HookRuntime

    log_messages: list[str] = []
    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.logger.info",
        lambda message, *args: log_messages.append(message % args),
    )
    runtime = HookRuntime(tenant_config=HookConfig(enabled=True))

    await runtime.emit(_context(), workspace_dir=Path("/tmp"))

    assert not [
        message
        for message in log_messages
        if message.startswith("HOOK_TELEMETRY ")
    ]


def test_merge_priority_additional_context_and_updated_input_conflict() -> (
    None
):
    first = CommandHookHandlerConfig(id="first", command="echo")
    second = CommandHookHandlerConfig(id="second", command="echo")
    third = CommandHookHandlerConfig(id="third", command="echo")
    plan = _plan(first, second, third)
    results = [
        plan.handlers[2].success(
            {
                "hookSpecificOutput": {
                    "additionalContext": "third",
                    "permissionDecision": "allow",
                },
            },
        ),
        plan.handlers[0].success(
            {
                "hookSpecificOutput": {
                    "additionalContext": "first",
                    "updatedInput": {"cmd": "echo one"},
                },
            },
        ),
        plan.handlers[1].success(
            {
                "hookSpecificOutput": {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "no",
                    "updatedInput": {"cmd": "echo two"},
                },
            },
        ),
    ]

    merged = merge_hook_results(plan, results)

    assert merged.decision == HookDecision.BLOCK
    assert "updatedInput" in merged.reason
    assert merged.updated_input is None
    assert [item.context for item in merged.additional_context] == [
        "first",
        "third",
    ]
    assert list(merged.hook_specific_outputs) == ["first", "second", "third"]
    assert [
        (item.handler_id, item.decision, item.reason)
        for item in merged.permission_decisions
    ] == [
        ("second", HookDecision.DENY, "no"),
        ("third", HookDecision.ALLOW, ""),
    ]


def test_merge_continue_false_overrides_other_decisions() -> None:
    stopper = CommandHookHandlerConfig(id="stopper", command="echo")
    asker = CommandHookHandlerConfig(id="asker", command="echo")
    plan = _plan(stopper, asker)
    merged = merge_hook_results(
        plan,
        [
            plan.handlers[1].success(
                {
                    "hookSpecificOutput": {
                        "permissionDecision": "ask",
                        "permissionDecisionReason": "review",
                    },
                },
            ),
            plan.handlers[0].success(
                {"continue": False, "stopReason": "stop now"},
            ),
        ],
    )

    assert merged.decision == HookDecision.STOP
    assert merged.reason == "stop now"


def test_merge_blocking_failure_preserves_its_reason_over_prior_block() -> (
    None
):
    policy = CommandHookHandlerConfig(id="policy", command="echo")
    audit = CommandHookHandlerConfig(
        id="audit",
        command="echo",
        failPolicy=FailPolicy.BLOCK,
    )
    plan = _plan(policy, audit)

    merged = merge_hook_results(
        plan,
        [
            plan.handlers[0].success(
                {"decision": "block", "reason": "policy requires review"},
            ),
            plan.handlers[1].failure("audit service timed out", "timeout"),
        ],
    )

    assert merged.decision == HookDecision.BLOCK
    assert merged.reason == "policy requires review"
    assert merged.has_blocking_failure is True
    assert merged.blocking_failure_reason == "audit service timed out"


def test_merge_stop_wins_over_multiple_updated_inputs() -> None:
    stopper = CommandHookHandlerConfig(id="stopper", command="echo")
    first_updater = CommandHookHandlerConfig(
        id="first-updater",
        command="echo",
    )
    second_updater = CommandHookHandlerConfig(
        id="second-updater",
        command="echo",
    )
    plan = _plan(stopper, first_updater, second_updater)

    merged = merge_hook_results(
        plan,
        [
            plan.handlers[2].success(
                {"hookSpecificOutput": {"updatedInput": {"cmd": "echo two"}}},
            ),
            plan.handlers[0].success(
                {"decision": "stop", "reason": "first stop reason"},
            ),
            plan.handlers[1].success(
                {"hookSpecificOutput": {"updatedInput": {"cmd": "echo one"}}},
            ),
        ],
    )

    assert merged.decision == HookDecision.STOP
    assert merged.reason == "first stop reason"
    assert merged.updated_input is None


@pytest.mark.parametrize("stopper_first", [True, False])
def test_merge_stop_discards_single_updated_input_regardless_of_handler_order(
    stopper_first: bool,
) -> None:
    stopper = CommandHookHandlerConfig(id="stopper", command="echo")
    updater = CommandHookHandlerConfig(id="updater", command="echo")
    plan = (
        _plan(stopper, updater) if stopper_first else _plan(updater, stopper)
    )
    handlers = {item.handler.id: item for item in plan.handlers}

    merged = merge_hook_results(
        plan,
        [
            handlers["updater"].success(
                {
                    "hookSpecificOutput": {
                        "updatedInput": {"cmd": "echo changed"},
                    },
                },
            ),
            handlers["stopper"].success(
                {"decision": "stop", "reason": "first stop reason"},
            ),
        ],
    )

    assert merged.decision == HookDecision.STOP
    assert merged.reason == "first stop reason"
    assert merged.updated_input is None
