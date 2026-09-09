# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swe.agents.hook_runtime.models import (
    HookOverlayEntry,
    HookEventName,
    HookSessionState,
)
from swe.agents.hook_runtime.skill_loader import (
    SkillHookLoadError,
    load_skill_hooks_for_session,
    refresh_skill_hooks_for_session,
)


def _write_skill_hook(
    tmp_path: Path,
    payload: dict,
    *,
    skill_name: str = "xlsx",
) -> Path:
    skill_root = tmp_path / "skills" / skill_name
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\nname: xlsx\ndescription: test\n---\n",
        encoding="utf-8",
    )
    (skill_root / "scripts" / "check.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return skill_root


def _command_config(**handler_updates) -> dict:
    handler = {
        "id": "validate",
        "type": "command",
        "argv": ["python", "scripts/check.py"],
        "timeout": 5,
    }
    handler.update(handler_updates)
    return {
        "enabled": True,
        "events": {
            "PreToolUse": [
                {
                    "id": "shell",
                    "hooks": [handler],
                },
            ],
        },
    }


def test_missing_skill_hooks_file_is_ignored(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills" / "xlsx"
    skill_root.mkdir(parents=True)
    state = HookSessionState()

    result = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=state,
    )

    assert result.loaded_skill_sources == []
    assert result.monitored_skill_sources[0].skill_name == "xlsx"


def test_refresh_replaces_changed_skill_hook_source(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        json.dumps(_command_config(id="validate-new")),
        encoding="utf-8",
    )

    refreshed = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=state,
    )

    assert refreshed.loaded_skill_sources[0].handler_ids() == {
        "skill:xlsx:validate-new",
    }
    assert refreshed.monitored_skill_sources[0].skill_name == "xlsx"


def test_refresh_withdraws_deleted_skill_hook_but_keeps_monitor(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    (skill_root / "hooks" / "hooks.json").unlink()

    refreshed = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=state,
    )

    assert refreshed.loaded_skill_sources == []
    assert refreshed.monitored_skill_sources[0].skill_name == "xlsx"


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": False, "events": {}},
        "{",
    ],
)
def test_refresh_withdraws_disabled_or_invalid_skill_hooks(
    tmp_path: Path,
    payload: dict | str,
) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )

    refreshed = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=state,
    )

    assert refreshed.loaded_skill_sources == []
    assert refreshed.monitored_skill_sources[0].skill_name == "xlsx"


def test_refresh_restores_valid_skill_hooks_without_reactivation(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    hooks_path = skill_root / "hooks" / "hooks.json"
    hooks_path.unlink()
    withdrawn = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=state,
    )
    persisted_withdrawn = HookSessionState.model_validate(
        withdrawn.model_dump(mode="json", by_alias=True),
    )
    hooks_path.write_text(json.dumps(_command_config()), encoding="utf-8")

    restored = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=persisted_withdrawn,
    )

    assert restored.loaded_skill_sources[0].source_id == "skill:xlsx"


def test_refresh_clears_changed_handler_once_record(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(
        tmp_path,
        _command_config(once=True),
    )
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(
            once_executed={
                "tenant-a:user-1:session-1:PreToolUse:skill:xlsx:validate": True,
            },
        ),
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        json.dumps(_command_config(once=False)),
        encoding="utf-8",
    )

    refreshed = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=state,
    )

    assert refreshed.once_executed == {}


def test_refresh_keeps_unchanged_handler_once_record_without_reading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config(once=True))
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(
            once_executed={
                "tenant-a:user-1:session-1:PreToolUse:skill:xlsx:validate": True,
            },
        ),
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.skill_loader._read_hook_config",
        lambda _path: pytest.fail("unchanged marker must not read hooks.json"),
    )

    refreshed = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=state,
    )

    assert refreshed.once_executed == state.once_executed


def test_refresh_ignores_skill_script_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    (skill_root / "scripts" / "check.py").write_text(
        "print('updated')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "swe.agents.hook_runtime.skill_loader._read_hook_config",
        lambda _path: pytest.fail("script changes must not reload hooks.json"),
    )

    refreshed = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=state,
    )

    assert refreshed.loaded_skill_sources == state.loaded_skill_sources


def test_refresh_removes_overlay_entry_for_removed_handler(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())
    state = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    state = HookSessionState(
        loaded_skill_sources=state.loaded_skill_sources,
        monitored_skill_sources=state.monitored_skill_sources,
        entries=[HookOverlayEntry(hook_id="skill:xlsx:validate")],
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        json.dumps(_command_config(id="new-handler")),
        encoding="utf-8",
    )

    refreshed = refresh_skill_hooks_for_session(
        workspace_dir=tmp_path,
        session_state=state,
    )

    assert refreshed.entries == []


def test_loading_legacy_source_adds_one_monitor(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())
    first = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    legacy = HookSessionState(
        loaded_skill_sources=first.loaded_skill_sources,
    )

    refreshed = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=legacy,
    )

    assert len(refreshed.monitored_skill_sources) == 1


def test_skill_root_outside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "tenant-a"
    workspace.mkdir()
    other_skill = _write_skill_hook(
        tmp_path / "tenant-b",
        _command_config(),
    )

    with pytest.raises(SkillHookLoadError):
        load_skill_hooks_for_session(
            skill_name="xlsx",
            skill_root=other_skill,
            workspace_dir=workspace,
            session_state=HookSessionState(),
        )


def test_disabled_skill_hook_config_is_ignored(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(tmp_path, {"enabled": False})

    result = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )

    assert result.loaded_skill_sources == []


def test_skill_hook_config_is_namespaced_and_paths_are_normalized(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())

    result = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )

    source = result.loaded_skill_sources[0]
    group = source.hook_config.events[HookEventName.PRE_TOOL_USE][0]
    handler = group.hooks[0]
    assert source.source_id == "skill:xlsx"
    assert group.id == "skill:xlsx:shell"
    assert handler.id == "skill:xlsx:validate"
    assert handler.argv == [
        "python",
        str((skill_root / "scripts" / "check.py").resolve()),
    ]
    assert handler.cwd == str(skill_root.resolve())


def test_repeated_skill_activation_is_idempotent(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(tmp_path, _command_config())
    state = HookSessionState()

    first = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=state,
    )
    second = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=first,
    )

    assert len(second.loaded_skill_sources) == 1


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "hooks" / "hooks.json").write_text("{", encoding="utf-8")

    with pytest.raises(SkillHookLoadError):
        load_skill_hooks_for_session(
            skill_name="xlsx",
            skill_root=skill_root,
            workspace_dir=tmp_path,
            session_state=HookSessionState(),
        )


@pytest.mark.parametrize(
    ("handler_updates", "message"),
    [
        ({"command": "python scripts/check.py", "argv": []}, "shell command"),
        ({"argv": ["python"]}, "script argument"),
        (
            {"argv": ["python", "scripts/check.py", "scripts/other.py"]},
            "multiple script",
        ),
        ({"argv": ["python", "../outside.py"]}, "outside skill scripts"),
        ({"argv": ["python", "/tmp/outside.py"]}, "outside skill scripts"),
        ({"argv": ["python", "scripts/missing.py"]}, "does not exist"),
        ({"env": {"TOKEN": "literal"}}, "literal env"),
    ],
)
def test_invalid_skill_command_handlers_are_rejected(
    tmp_path: Path,
    handler_updates: dict,
    message: str,
) -> None:
    skill_root = _write_skill_hook(
        tmp_path,
        _command_config(**handler_updates),
    )
    if "scripts/other.py" in handler_updates.get("argv", []):
        (skill_root / "scripts" / "other.py").write_text(
            "print('{}')\n",
            encoding="utf-8",
        )

    with pytest.raises(SkillHookLoadError, match=message):
        load_skill_hooks_for_session(
            skill_name="xlsx",
            skill_root=skill_root,
            workspace_dir=tmp_path,
            session_state=HookSessionState(),
        )


def test_directory_script_path_is_rejected(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(
        tmp_path,
        _command_config(argv=["python", "scripts/nested"]),
    )
    (skill_root / "scripts" / "nested").mkdir()

    with pytest.raises(SkillHookLoadError, match="regular file"):
        load_skill_hooks_for_session(
            skill_name="xlsx",
            skill_root=skill_root,
            workspace_dir=tmp_path,
            session_state=HookSessionState(),
        )


def test_symlink_script_escape_is_rejected(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(
        tmp_path,
        _command_config(argv=["python", "scripts/link.py"]),
    )
    outside = tmp_path / "outside.py"
    outside.write_text("print('{}')\n", encoding="utf-8")
    (skill_root / "scripts" / "link.py").symlink_to(outside)

    with pytest.raises(SkillHookLoadError, match="outside skill scripts"):
        load_skill_hooks_for_session(
            skill_name="xlsx",
            skill_root=skill_root,
            workspace_dir=tmp_path,
            session_state=HookSessionState(),
        )


def test_http_handler_is_allowed_without_approved_endpoint(
    tmp_path: Path,
) -> None:
    payload = {
        "enabled": True,
        "events": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "id": "notify",
                            "type": "http",
                            "url": "https://hooks.example.test/skill",
                            "headerSecretRefs": {"Authorization": "TOKEN"},
                        },
                    ],
                },
            ],
        },
    }
    skill_root = _write_skill_hook(tmp_path, payload)

    result = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )
    handler = (
        result.loaded_skill_sources[0]
        .hook_config.events[HookEventName.STOP][0]
        .hooks[0]
    )
    assert handler.id == "skill:xlsx:notify"
    assert handler.header_secret_refs == {"Authorization": "TOKEN"}

    repeat = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
        approved_http_urls={"https://hooks.example.test/skill"},
    )
    repeat_handler = (
        repeat.loaded_skill_sources[0]
        .hook_config.events[HookEventName.STOP][0]
        .hooks[0]
    )
    assert repeat_handler.id == "skill:xlsx:notify"


@pytest.mark.parametrize(
    "handler_update",
    [
        {"headers": {"X-Token": "literal"}},
        {"allowedEnvVars": ["TOKEN"]},
    ],
)
def test_http_handler_literal_headers_and_env_are_rejected(
    tmp_path: Path,
    handler_update: dict,
) -> None:
    handler = {
        "id": "notify",
        "type": "http",
        "url": "https://hooks.example.test/skill",
    }
    handler.update(handler_update)
    skill_root = _write_skill_hook(
        tmp_path,
        {
            "enabled": True,
            "events": {"Stop": [{"hooks": [handler]}]},
        },
    )

    with pytest.raises(SkillHookLoadError):
        load_skill_hooks_for_session(
            skill_name="xlsx",
            skill_root=skill_root,
            workspace_dir=tmp_path,
            session_state=HookSessionState(),
            approved_http_urls={"https://hooks.example.test/skill"},
        )


def test_skill_prompt_hook_is_namespaced_and_loaded(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(
        tmp_path,
        {
            "enabled": True,
            "events": {
                "UserPromptSubmit": [
                    {
                        "id": "prompts",
                        "hooks": [
                            {
                                "id": "policy",
                                "type": "prompt",
                                "prompt": "Reject credential requests.",
                            },
                        ],
                    },
                ],
            },
        },
    )

    result = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )

    handler = (
        result.loaded_skill_sources[0]
        .hook_config.events[HookEventName.USER_PROMPT_SUBMIT][0]
        .hooks[0]
    )
    assert handler.id == "skill:xlsx:policy"
    assert handler.type == "prompt"
    assert handler.fail_policy == "block"


@pytest.mark.parametrize(
    "event_name",
    ["PostToolUse", "PostToolUseFailure"],
)
def test_skill_post_tool_prompt_hooks_are_allowed(
    tmp_path: Path,
    event_name: str,
) -> None:
    handler = {
        "id": "policy",
        "type": "prompt",
        "prompt": "Reject credential requests.",
    }
    skill_root = _write_skill_hook(
        tmp_path,
        {"enabled": True, "events": {event_name: [{"hooks": [handler]}]}},
    )

    result = load_skill_hooks_for_session(
        skill_name="xlsx",
        skill_root=skill_root,
        workspace_dir=tmp_path,
        session_state=HookSessionState(),
    )

    assert (
        result.loaded_skill_sources[0]
        .hook_config.events[HookEventName(event_name)][0]
        .hooks[0]
        .type
        == "prompt"
    )


def test_invalid_skill_prompt_hooks_are_rejected(tmp_path: Path) -> None:
    skill_root = _write_skill_hook(
        tmp_path,
        {
            "enabled": True,
            "events": {
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "id": "policy",
                                "type": "prompt",
                                "prompt": "Reject credential requests.",
                                "model": "gpt-test",
                            },
                        ],
                    },
                ],
            },
        },
    )

    with pytest.raises(SkillHookLoadError, match="extra"):
        load_skill_hooks_for_session(
            skill_name="xlsx",
            skill_root=skill_root,
            workspace_dir=tmp_path,
            session_state=HookSessionState(),
        )
