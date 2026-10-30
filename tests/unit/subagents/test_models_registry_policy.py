# -*- coding: utf-8 -*-
"""Focused tests for SubAgent definition, registry, policy, and run records."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from swe.app.subagents import (
    AgentRegistry,
    AgentResult,
    DefinitionValidationError,
    DelegationSpec,
    InMemoryDefinitionProvider,
    InMemorySubAgentRunStore,
    LocalJsonSubAgentRunStore,
    PermissionPolicy,
    SkillOwnedDefinitionMetadata,
    SkillOwnedToolConfig,
    SubAgentDefinition,
    builtin_definition_provider,
    compose_effective_policy,
    build_definition_policy,
    validate_tool_call,
)
from swe.app.subagents.models import (
    AgentError,
    BudgetConfig,
    MutationPolicy,
    PermissionTools,
)


def test_definition_uses_instruction_and_top_level_routing_fields() -> None:
    """Definitions expose canonical instruction/routing vocabulary."""
    definition = SubAgentDefinition.model_validate(
        {
            "name": "customer-aum-analyst",
            "source": "agent_owned",
            "description": "Analyzes 1M AUM customer maintenance strategy.",
            "instruction": "Act as a customer strategy analyst.",
            "trigger_keywords": ["AUM", "客户维护"],
            "priority": 20,
        },
    )

    assert definition.name == "customer-aum-analyst"
    assert definition.instruction == "Act as a customer strategy analyst."
    assert "output_contract" not in definition.model_dump()
    assert definition.trigger_keywords == ["AUM", "客户维护"]
    assert definition.priority == 20
    assert definition.source == "agent_owned"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("trigger_keywords", [""]),
        ("trigger_keywords", ["x" * 65]),
        ("trigger_keywords", [f"keyword-{index}" for index in range(21)]),
    ],
)
def test_definition_rejects_invalid_matching_lists(
    field_name: str,
    value: list[str],
) -> None:
    """Definition matching lists reject empty, oversized, or excessive items."""
    with pytest.raises(ValidationError):
        SubAgentDefinition.model_validate(
            {
                "name": "invalid-match-list",
                "source": "agent_owned",
                "description": "Invalid match list.",
                "instruction": "Act as an analyst.",
                field_name: value,
            },
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "legacy",
            "source": "agent_owned",
            "description": "legacy",
            "system_prompt": "legacy",
        },
        {
            "name": "legacy",
            "source": "agent_owned",
            "description": "legacy",
            "prompt": {"system": "legacy"},
        },
        {
            "agent_name": "legacy",
            "source": "agent_owned",
            "description": "legacy",
            "instruction": "legacy",
        },
    ],
)
def test_definition_rejects_legacy_field_names(payload: dict) -> None:
    """Definition validation rejects old external and internal field names."""
    with pytest.raises(ValidationError):
        SubAgentDefinition.model_validate(payload)


@pytest.mark.parametrize(
    "model, payload",
    [
        (
            SubAgentDefinition,
            {
                "name": "analyst",
                "source": "agent_owned",
                "description": "Analyzes repository evidence.",
                "instruction": "Inspect repository evidence.",
                "output_contract": "Return JSON.",
            },
        ),
        (
            DelegationSpec,
            {
                "name": "analyst",
                "objective": "Inspect repository evidence.",
                "expected_output": {"format": "json"},
            },
        ),
    ],
)
def test_subagent_contract_rejects_retired_structured_fields(
    model,
    payload: dict,
) -> None:
    """New SubAgent inputs reject retired structured-output fields."""
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_agent_result_has_summary_as_its_only_model_content() -> None:
    """Runtime metadata remains while structured content fields are gone."""
    result = AgentResult(
        task_id="task-1",
        agent_run_id="run-1",
        agent_name="analyst",
        status="completed",
        summary="Repository evidence is sufficient.",
    )

    assert result.model_dump() == {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "agent_name": "analyst",
        "status": "completed",
        "summary": "Repository evidence is sufficient.",
        "metrics": {
            "turns_used": 0,
            "tool_calls_used": 0,
            "input_tokens": None,
            "output_tokens": None,
            "elapsed_ms": 0,
        },
        "errors": [],
    }


def test_budget_does_not_accept_max_tokens() -> None:
    """SubAgent budgets no longer expose token limits."""
    with pytest.raises(ValidationError):
        BudgetConfig.model_validate({"max_tokens": 1000})


def test_budget_defaults_allow_fifty_turns_and_ten_minutes() -> None:
    """Default SubAgent work budgets allow longer research runs."""
    budget = BudgetConfig()

    assert budget.max_turns == 50
    assert budget.timeout_ms == 600_000


def test_delegation_spec_uses_name_not_agent_name() -> None:
    """DelegationSpec uses the same SubAgent name field as start requests."""
    spec = DelegationSpec.model_validate(
        {"name": "plan-researcher", "objective": "Inspect repo"},
    )

    assert spec.name == "plan-researcher"
    assert spec.objective == "Inspect repo"

    with pytest.raises(ValidationError):
        DelegationSpec.model_validate(
            {"agent_name": "plan-researcher", "objective": "Inspect repo"},
        )


def test_start_request_allows_omitting_instruction_for_resolved_definition() -> (
    None
):
    """Only a run-scoped fallback requires a caller-supplied instruction."""
    from swe.app.subagents.models import SubAgentStartRequest

    request = SubAgentStartRequest.model_validate(
        {
            "name": "aum-analyst",
            "instruction": "Act as an AUM analyst.",
            "objective": "Analyze customer maintenance.",
            "background": "Private banking context.",
        },
    )

    assert request.name == "aum-analyst"
    assert request.instruction == "Act as an AUM analyst."
    assert request.objective == "Analyze customer maintenance."
    resolved = SubAgentStartRequest.model_validate(
        {
            "name": "quality:reviewer",
            "objective": "Analyze customer maintenance.",
        },
    )
    assert resolved.instruction is None


def test_definition_match_metadata_defaults_to_unmatched() -> None:
    """Run records can persist deterministic match metadata."""
    from swe.app.subagents.models import DefinitionMatchMetadata

    metadata = DefinitionMatchMetadata()

    assert metadata.matched is False
    assert metadata.definition_name is None
    assert metadata.definition_source is None
    assert metadata.score is None


def test_builtin_definitions_are_valid_and_readonly() -> None:
    """No default experts are shipped in the built-in provider."""
    registry = AgentRegistry([builtin_definition_provider()])
    assert registry.list() == []


def test_definition_validation_rejects_unsupported_mvp_capabilities() -> None:
    """Custom definitions use the same validator and cannot widen MVP scope."""
    with pytest.raises(DefinitionValidationError) as exc_info:
        SubAgentDefinition.model_validate(
            {
                "name": "unsafe",
                "version": "1.0.0",
                "description": "Unsafe worker",
                "instruction": "Inspect code",
                "source": "agent_owned",
                "owner_scope": "tenant/source/workspace",
                "tools": {
                    "allow": ["read_file", "write_file", "mcp:server.tool"],
                },
                "isolation": {
                    "context": "fork",
                    "workspace": "worktree",
                    "memory": "project",
                },
                "lifecycle": {"allow_nested_delegation": True},
            },
        )

    message = str(exc_info.value)
    assert "nested delegation" in message
    assert "unsupported context isolation" in message
    assert "unsupported workspace isolation" in message
    assert "persistent memory" in message
    assert "MCP" in message
    assert "mutating tool" in message


def test_definition_validation_rejects_unsafe_permission_overrides() -> None:
    """Stored definitions cannot widen readonly policy via permission config."""
    with pytest.raises(DefinitionValidationError) as exc_info:
        SubAgentDefinition.model_validate(
            {
                "name": "unsafe-permission",
                "version": "1.0.0",
                "description": "Unsafe permission worker",
                "instruction": "Inspect code",
                "source": "agent_owned",
                "owner_scope": "tenant/source/workspace",
                "permission": {
                    "tools": {
                        "allow": [
                            "read_file",
                            "write_file",
                            "mcp:server.tool",
                        ],
                    },
                    "shell": {
                        "allowed_commands": ["pwd", "python"],
                    },
                },
            },
        )

    message = str(exc_info.value)
    assert "permission" in message
    assert "MCP" in message
    assert "mutating tool" in message
    assert "python" in message


def test_definition_validation_rejects_unknown_permission_tools() -> None:
    """Permission tool allowlists must use known built-in tool names."""
    with pytest.raises(DefinitionValidationError) as exc_info:
        SubAgentDefinition.model_validate(
            {
                "name": "unknown-permission-tool",
                "version": "1.0.0",
                "description": "Invalid permission worker",
                "instruction": "Inspect code",
                "source": "agent_owned",
                "owner_scope": "tenant/source/workspace",
                "permission": {
                    "tools": {
                        "allow": ["read_file", "bogus_tool"],
                    },
                },
            },
        )

    assert "unknown built-in tool: bogus_tool" in str(exc_info.value)


@pytest.mark.parametrize("permission_field", ["deny", "ask"])
def test_definition_validation_rejects_unknown_permission_deny_ask_tools(
    permission_field: str,
) -> None:
    """Permission tool deny/ask lists must use known built-in tool names."""
    with pytest.raises(DefinitionValidationError) as exc_info:
        SubAgentDefinition.model_validate(
            {
                "name": f"unknown-permission-{permission_field}-tool",
                "version": "1.0.0",
                "description": "Invalid permission worker",
                "instruction": "Inspect code",
                "source": "agent_owned",
                "owner_scope": "tenant/source/workspace",
                "permission": {
                    "tools": {
                        "allow": ["read_file"],
                        permission_field: ["bogus_tool"],
                    },
                },
            },
        )

    assert "unknown built-in tool: bogus_tool" in str(exc_info.value)


def test_registry_rejects_duplicate_and_builtin_shadowing(
    sample_builtin_definition,
) -> None:
    """A stored provider cannot silently replace a built-in definition."""
    builtin = sample_builtin_definition
    stored_shadow = builtin.model_copy(update={"source": "agent_owned"})

    with pytest.raises(DefinitionValidationError) as exc_info:
        AgentRegistry(
            [
                InMemoryDefinitionProvider([builtin]),
                InMemoryDefinitionProvider([stored_shadow]),
            ],
        )

    assert "duplicate" in str(exc_info.value)
    assert "plan-researcher" in str(exc_info.value)


def test_registry_rejects_stored_definition_shadowing_builtin_name(
    sample_builtin_definition,
) -> None:
    """A stored provider cannot supersede a built-in with another version."""
    builtin = sample_builtin_definition
    stored_shadow = builtin.model_copy(
        update={"source": "agent_owned", "version": "9.0.0"},
    )

    with pytest.raises(DefinitionValidationError) as exc_info:
        AgentRegistry(
            [
                InMemoryDefinitionProvider([builtin]),
                InMemoryDefinitionProvider([stored_shadow]),
            ],
        )

    assert "shadow builtin" in str(exc_info.value)
    assert "plan-researcher" in str(exc_info.value)


def test_registry_rejects_run_scoped_definitions() -> None:
    """Run-scoped definitions are per-run data, not registry entries."""
    run_scoped = SubAgentDefinition.model_validate(
        {
            "name": "ad-hoc",
            "description": "Temporary worker.",
            "instruction": "Handle this run only.",
            "source": "run_scoped",
            "owner_scope": "tenant-a/agent-b",
        },
    )

    with pytest.raises(DefinitionValidationError) as exc_info:
        AgentRegistry([InMemoryDefinitionProvider([run_scoped])])

    assert "run_scoped definition cannot be loaded" in str(exc_info.value)


def test_registry_rejects_run_scoped_before_duplicate_check(
    sample_builtin_definition,
) -> None:
    """Run-scoped definitions are rejected even when the name matches a builtin."""
    builtin = sample_builtin_definition
    run_scoped = builtin.model_copy(update={"source": "run_scoped"})

    with pytest.raises(DefinitionValidationError) as exc_info:
        AgentRegistry(
            [
                InMemoryDefinitionProvider([builtin]),
                InMemoryDefinitionProvider([run_scoped]),
            ],
        )

    assert "run_scoped definition cannot be loaded" in str(exc_info.value)
    assert "duplicate" not in str(exc_info.value)


def test_registry_supports_stored_provider_filtering_and_version_lookup() -> (
    None
):
    """Extension providers can be injected without public CRUD/API support."""
    stored_definition = SubAgentDefinition.model_validate(
        {
            "name": "local-reader",
            "version": "1.0.0",
            "description": "Local readonly worker",
            "instruction": "Read files and summarize evidence.",
            "source": "agent_owned",
            "owner_scope": "tenant-a/source-b/default",
            "tools": {"allow": ["read_file"]},
        },
    )
    registry = AgentRegistry(
        [
            builtin_definition_provider(),
            InMemoryDefinitionProvider([stored_definition]),
        ],
    )

    assert registry.resolve("local-reader").owner_scope == (
        "tenant-a/source-b/default"
    )
    assert registry.get("local-reader", "1.0.0") == stored_definition
    assert registry.list(source="agent_owned") == [stored_definition]
    assert registry.list(owner_scope="tenant-a/source-b/default") == [
        stored_definition,
    ]


def test_registry_resolve_uses_latest_semantic_version_by_default() -> None:
    """Default resolution should pick the highest semantic version."""
    earlier = SubAgentDefinition.model_validate(
        {
            "name": "local-reader",
            "version": "2.0.0",
            "description": "Older readonly worker",
            "instruction": "Read files and summarize evidence.",
            "source": "agent_owned",
            "owner_scope": "tenant-a/source-b/default",
            "tools": {"allow": ["read_file"]},
        },
    )
    later = earlier.model_copy(update={"version": "10.0.0"})
    registry = AgentRegistry(
        [
            builtin_definition_provider(),
            InMemoryDefinitionProvider([earlier, later]),
        ],
    )

    assert registry.resolve("local-reader") == later


def test_effective_policy_uses_intersection_and_deny_precedence() -> None:
    """The effective policy cannot exceed parent/sub/runtime/workspace inputs."""
    parent = PermissionPolicy.readonly(
        allow_tools=["read_file", "grep_search", "execute_shell_command"],
        deny_tools=["grep_search"],
    )
    subagent = PermissionPolicy.readonly(
        allow_tools=["read_file", "execute_shell_command"],
    )
    runtime = PermissionPolicy.readonly(
        allow_tools=["read_file", "glob_search", "execute_shell_command"],
    )
    workspace = PermissionPolicy.readonly(
        allow_tools=["read_file", "grep_search", "execute_shell_command"],
        deny_tools=["execute_shell_command"],
    )

    effective = compose_effective_policy(parent, subagent, runtime, workspace)

    assert effective.tools.allow == ["read_file"]
    assert sorted(effective.tools.deny) == [
        "execute_shell_command",
        "grep_search",
    ]
    assert validate_tool_call(effective, "read_file", {"path": "x"}).allowed
    assert not validate_tool_call(
        effective,
        "execute_shell_command",
        {"command": "git status"},
    ).allowed


def test_effective_policy_preserves_shell_deny_all() -> None:
    """A deny_all input policy must keep effective shell execution denied."""
    parent = PermissionPolicy.readonly()
    subagent = PermissionPolicy.readonly()
    runtime = PermissionPolicy.readonly()
    workspace = PermissionPolicy.readonly()
    workspace.shell.strategy = "deny_all"

    effective = compose_effective_policy(parent, subagent, runtime, workspace)

    assert effective.shell.strategy == "deny_all"
    decision = validate_tool_call(
        effective,
        "execute_shell_command",
        {"command": "pwd"},
    )
    assert not decision.allowed
    assert "deny_all" in decision.reason


def test_skill_owned_definition_can_narrow_parent_to_mutable_tools() -> None:
    parent = PermissionPolicy(
        tools=PermissionTools(
            allow=["read_file", "write_file", "edit_file"],
        ),
        mutation=MutationPolicy(
            allow_file_write=True,
            allow_patch=True,
        ),
    )
    definition = SubAgentDefinition.model_validate(
        {
            "name": "quality:editor",
            "source": "skill_owned",
            "owner_scope": "skill:quality",
            "description": "Edit the requested file.",
            "instruction": "Make only requested edits.",
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="editor",
                tools=SkillOwnedToolConfig(
                    allow=["read_file", "write_file"],
                ),
            ),
        },
    )

    definition_policy = build_definition_policy(definition, parent)
    effective = compose_effective_policy(
        parent,
        definition_policy,
        parent,
        parent,
    )

    assert effective.tools.allow == ["read_file", "write_file"]
    assert validate_tool_call(
        effective,
        "write_file",
        {"path": "notes.txt", "content": "updated"},
    ).allowed
    assert not validate_tool_call(
        effective,
        "edit_file",
        {"path": "notes.txt", "old_str": "a", "new_str": "b"},
    ).allowed


def test_run_scoped_definition_inherits_parent_enabled_tools() -> None:
    parent = PermissionPolicy.bounded(
        allow_tools=["read_file", "write_file"],
        mutation=MutationPolicy(allow_file_write=True),
    )
    definition = SubAgentDefinition.model_construct(
        name="legacy-writer",
        source="skill_owned",
        description="Legacy definition.",
        instruction="Do not trust this policy.",
        permission=PermissionPolicy.bounded(
            allow_tools=["read_file", "write_file"],
            mutation=MutationPolicy(allow_file_write=True),
        ),
    )

    policy = build_definition_policy(definition, parent)

    assert policy.mode == "bounded"
    assert policy.tools.allow == ["read_file", "write_file"]
    assert policy.mutation.allow_file_write is True


def test_skill_owned_definition_without_inheritance_uses_explicit_allow_list() -> (
    None
):
    parent = PermissionPolicy(
        tools=PermissionTools(allow=["read_file", "write_file"]),
        mutation=MutationPolicy(allow_file_write=True),
    )
    definition = SubAgentDefinition.model_validate(
        {
            "name": "quality:no-tools",
            "source": "skill_owned",
            "owner_scope": "skill:quality",
            "description": "Do not use built-in tools.",
            "instruction": "Reason without tools.",
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="no-tools",
                tools=SkillOwnedToolConfig(
                    inherit=False,
                    allow=["read_file"],
                ),
            ),
        },
    )

    assert build_definition_policy(definition, parent).tools.allow == [
        "read_file",
    ]


def test_skill_owned_definition_inherits_parent_enabled_tools_by_default() -> (
    None
):
    """An omitted tools table preserves the parent's supported tool set."""
    parent = PermissionPolicy.bounded(
        allow_tools=[
            "execute_shell_command",
            "read_file",
            "write_file",
            "edit_file",
            "grep_search",
            "glob_search",
            "get_current_time",
            "copy_file_to_static",
            "update_task_progress",
        ],
        mutation=MutationPolicy(
            allow_file_write=True,
            allow_patch=True,
        ),
    )
    definition = SubAgentDefinition.model_validate(
        {
            "name": "quality:editor",
            "source": "skill_owned",
            "owner_scope": "skill:quality",
            "description": "Edit the requested files.",
            "instruction": "Keep the patch minimal.",
            "skill_owned": SkillOwnedDefinitionMetadata(
                skill_name="quality",
                local_name="editor",
            ),
        },
    )

    definition_policy = build_definition_policy(definition, parent)
    effective = compose_effective_policy(
        parent,
        definition_policy,
        parent,
        parent,
    )

    assert set(effective.tools.allow) == set(parent.tools.allow)
    assert validate_tool_call(
        effective,
        "write_file",
        {"path": "notes.txt", "content": "updated"},
    ).allowed
    assert validate_tool_call(
        effective,
        "edit_file",
        {"path": "notes.txt", "old_str": "a", "new_str": "b"},
    ).allowed
    assert not validate_tool_call(
        effective,
        "copy_file_to_static",
        {"path": "notes.txt"},
    ).allowed


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("pwd", True),
        ("ls src/swe", True),
        ("rg SubAgent src tests", True),
        ("git status --short", True),
        ("git show HEAD -- src/swe/app.py", True),
        ("pytest tests/unit", False),
        ("rg --pre 'touch /tmp/subagent-mutates' SubAgent src", False),
        ("git diff --ext-diff", False),
        ("git checkout -- file.py", False),
        ("git status --short && touch /tmp/subagent-mutates", False),
        ("git status --short & touch /tmp/subagent-mutates", False),
        ("git status --short | sh", False),
        ("git diff --output=/tmp/subagent-mutates", False),
        ("git log --output /tmp/subagent-mutates", False),
        ("sed '/warning/p' file.txt", True),
        ("sed '$p' file.txt", True),
        ("sed -i bak s/a/b/ file.txt", False),
        ("sed --in-place=.bak s/a/b/ file.txt", False),
        ("sed 's/a/b/w/tmp/subagent-mutates' file.txt", False),
        ("sed 's/a/date/e' file.txt", False),
        ("sed -n '1,10w /tmp/subagent-mutates' file.txt", False),
        ("sed -e '1w/tmp/subagent-mutates' file.txt", False),
        ("sed '1W /tmp/subagent-mutates' file.txt", False),
        ("sed -e '1W/tmp/subagent-mutates' file.txt", False),
        ("sed '$w /tmp/subagent-mutates' file.txt", False),
        ("sed '/a/w /tmp/subagent-mutates' file.txt", False),
        ("sed '1,$w /tmp/subagent-mutates' file.txt", False),
        ("sed '2!w /tmp/subagent-mutates' file.txt", False),
        ("sed -n '1e touch /tmp/subagent-mutates' file.txt", False),
        ("sed '$e touch /tmp/subagent-mutates' file.txt", False),
        ("sed -f readonly-script.sed file.txt", False),
        ("sed -freadonly-script.sed file.txt", False),
        ("sed --file readonly-script.sed file.txt", False),
        ("sed --file=readonly-script.sed file.txt", False),
        ("sed -n '1,10p' a.py > out.txt", False),
        ("python -m black src", False),
        ("deploy production", False),
    ],
)
def test_readonly_shell_authorization(command: str, allowed: bool) -> None:
    """Readonly shell permits conservative inspection commands only."""
    policy = PermissionPolicy.readonly()

    decision = validate_tool_call(
        policy,
        "execute_shell_command",
        {"command": command},
    )

    assert decision.allowed is allowed


@pytest.mark.asyncio
async def test_run_stores_record_status_result_and_errors(
    tmp_path: Path,
    sample_builtin_definition,
) -> None:
    """Run stores keep lifecycle state in app workspace state."""
    spec = DelegationSpec(
        task_id="task-1",
        parent_thread_id="thread-1",
        name="plan-researcher",
        objective="Inspect the repo",
    )
    definition = sample_builtin_definition
    policy = PermissionPolicy.readonly()
    result = AgentResult(
        task_id="task-1",
        agent_run_id="placeholder",
        agent_name="plan-researcher",
        status="completed",
        summary="done",
    )

    memory_store = InMemorySubAgentRunStore()
    record = await memory_store.create(spec, definition, policy)
    await memory_store.mark_running(record.run_id)
    await memory_store.finish(record.run_id, result)

    saved = await memory_store.get(record.run_id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.result is not None
    assert saved.definition_source == "builtin"

    local_store = LocalJsonSubAgentRunStore(tmp_path / "app-state")
    failed = await local_store.create(spec, definition, policy)
    await local_store.mark_running(failed.run_id)
    await local_store.fail(failed.run_id, "boom")

    assert (tmp_path / "app-state" / "subagent_runs.json").exists()
    saved_failed = await local_store.get(failed.run_id)
    assert saved_failed is not None
    assert saved_failed.status == "failed"
    assert saved_failed.errors[0].message == "SubAgent execution failed."

    timeout = await local_store.create(spec, definition, policy)
    await local_store.mark_running(timeout.run_id)
    timeout_result = AgentResult(
        task_id="task-1",
        agent_run_id=timeout.run_id,
        agent_name="plan-researcher",
        status="failed",
        summary="timed out",
        errors=[
            AgentError(
                code="timeout",
                message="SubAgent execution exceeded its timeout budget.",
                recoverable=False,
            ),
        ],
    )
    await local_store.fail(
        timeout.run_id,
        timeout_result.summary,
        result=timeout_result,
    )

    saved_timeout = await local_store.get(timeout.run_id)
    assert saved_timeout is not None
    assert saved_timeout.result is not None
    assert saved_timeout.result.errors[0].code == "timeout"
    assert saved_timeout.errors[0].code == "timeout"
