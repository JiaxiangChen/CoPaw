# Skill Hook Revision Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an activated Skill's current `hooks.json` take effect at its next Hook event without interrupting handlers already running.

**Architecture:** Persist monitored Skill Hook records independently from effective loaded sources. Refresh the overlay at the start of `HookRuntime.emit()` with a cheap `stat` marker; only a changed marker causes file parsing and source replacement or fail-closed withdrawal. The event resolver then uses the refreshed overlay as its fixed event snapshot.

**Tech Stack:** Python 3, Pydantic models, pytest, existing Hook Runtime.

---

### Task 1: Persist monitored Skill Hook sources

**Files:**
- Modify: `src/swe/agents/hook_runtime/models.py`
- Modify: `src/swe/agents/hook_runtime/skill_loader.py`
- Test: `tests/unit/agents/hook_runtime/test_skill_hook_loader.py`

- [ ] **Step 1: Write the failing tests for an activated Skill without effective Hooks.**

```python
def test_skill_activation_keeps_monitoring_record_without_hooks_file(tmp_path):
    result = load_skill_hooks_for_session(...)
    assert result.monitored_skill_sources[0].skill_name == "xlsx"
    assert result.loaded_skill_sources == []
```

- [ ] **Step 2: Run the failing tests.**

Run: `../../venv/bin/python -m pytest tests/unit/agents/hook_runtime/test_skill_hook_loader.py -q`
Expected: FAIL because monitored Skill Hook sources do not exist.

- [ ] **Step 3: Add monitoring models and initial load behavior.**

```python
class MonitoredSkillHookSource(BaseModel):
    skill_name: str
    skill_root: str
    source_path: str
    file_version: SkillHookFileVersion

class HookSessionState(BaseModel):
    monitored_skill_sources: list[MonitoredSkillHookSource] = Field(...)
```

Create one monitored record when a Skill is activated, including when the file
is absent or disabled. Load an effective source only for a valid enabled file.

- [ ] **Step 4: Run the focused tests.**

Run: `../../venv/bin/python -m pytest tests/unit/agents/hook_runtime/test_skill_hook_loader.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/swe/agents/hook_runtime/models.py src/swe/agents/hook_runtime/skill_loader.py tests/unit/agents/hook_runtime/test_skill_hook_loader.py
git commit -m "feat(hooks): track activated skill hook sources"
```

### Task 2: Refresh monitored Skill Hooks at event boundaries

**Files:**
- Modify: `src/swe/agents/hook_runtime/skill_loader.py`
- Modify: `src/swe/agents/hook_runtime/runtime.py`
- Test: `tests/unit/agents/hook_runtime/test_skill_hook_loader.py`
- Test: `tests/unit/agents/hook_runtime/test_runtime.py`

- [ ] **Step 1: Write failing refresh tests.**

```python
def test_refresh_replaces_changed_hook_source(tmp_path):
    state = load_skill_hooks_for_session(...)
    hooks_path.write_text(json.dumps(changed_config))
    refreshed = refresh_skill_hooks_for_event(..., session_state=state)
    assert refreshed.loaded_skill_sources[0].handler_ids() == {"skill:xlsx:new"}

def test_refresh_withdraws_deleted_or_invalid_hook_file(tmp_path):
    state = load_skill_hooks_for_session(...)
    hooks_path.unlink()
    refreshed = refresh_skill_hooks_for_event(..., session_state=state)
    assert refreshed.loaded_skill_sources == []
    assert refreshed.monitored_skill_sources[0].skill_name == "xlsx"
```

- [ ] **Step 2: Run the failing tests.**

Run: `../../venv/bin/python -m pytest tests/unit/agents/hook_runtime/test_skill_hook_loader.py tests/unit/agents/hook_runtime/test_runtime.py -q`
Expected: FAIL because no event-boundary refresh function exists.

- [ ] **Step 3: Implement stat-marker refresh and event snapshot.**

```python
def refresh_skill_hooks_for_event(..., session_state: HookSessionState) -> HookSessionState:
    for monitored in session_state.monitored_skill_sources:
        if current_file_version(monitored.source_path) == monitored.file_version:
            continue
        state = reconcile_monitored_skill_source(...)
    return state

async def HookRuntime.emit(...):
    self.session_overlay = await asyncio.to_thread(refresh_skill_hooks_for_event, ...)
    plan = HookResolver(session_overlay=self.session_overlay).resolve_event_plan(context)
```

The reconcile path fails closed for absent, disabled, unreadable, and invalid
files; preserves identical handlers' once keys; and removes obsolete Skill
overlay entries.

- [ ] **Step 4: Run the focused tests.**

Run: `../../venv/bin/python -m pytest tests/unit/agents/hook_runtime/test_skill_hook_loader.py tests/unit/agents/hook_runtime/test_runtime.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/swe/agents/hook_runtime/skill_loader.py src/swe/agents/hook_runtime/runtime.py tests/unit/agents/hook_runtime/test_skill_hook_loader.py tests/unit/agents/hook_runtime/test_runtime.py
git commit -m "feat(hooks): refresh skill hooks before event dispatch"
```

### Task 3: Preserve refreshed state through runner integration

**Files:**
- Modify: `src/swe/app/runner/runner.py`
- Test: `tests/unit/app/test_runner_hook_runtime.py`

- [ ] **Step 1: Write a failing runner test.**

```python
async def test_runner_persists_skill_hook_refresh_from_event(tmp_path):
    # Persist an activated source, remove hooks.json, emit a hook event,
    # then assert the saved overlay retains monitoring but no effective source.
```

- [ ] **Step 2: Run the failing test.**

Run: `../../venv/bin/python -m pytest tests/unit/app/test_runner_hook_runtime.py -q`
Expected: FAIL because the refreshed runtime overlay is not propagated to the
runner-owned session state.

- [ ] **Step 3: Propagate the event-refreshed overlay.**

```python
result = await runtime.emit(...)
overlay.copy_from(runtime.session_overlay)
return result
```

Keep tenant and Agent Profile hook inputs unchanged.

- [ ] **Step 4: Run runner and Hook suites.**

Run: `../../venv/bin/python -m pytest tests/unit/app/test_runner_hook_runtime.py tests/unit/agents/hook_runtime/ -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/swe/app/runner/runner.py tests/unit/app/test_runner_hook_runtime.py
git commit -m "fix(hooks): persist refreshed skill hook state"
```

### Task 4: Verify the full behavior and documentation

**Files:**
- Modify: `CONTEXT.md`
- Modify: `docs/superpowers/specs/2026-09-08-skill-hook-revision-sync-design.md`
- Modify: `docs/superpowers/plans/2026-09-08-skill-hook-revision-sync.md`

- [ ] **Step 1: Run all relevant tests.**

Run: `../../venv/bin/python -m pytest tests/unit/agents/hook_runtime/ tests/unit/app/test_runner_hook_runtime.py -q`
Expected: PASS.

- [ ] **Step 2: Run style checks for changed Python files.**

Run: `../../venv/bin/python -m ruff check src/swe/agents/hook_runtime src/swe/app/runner/runner.py tests/unit/agents/hook_runtime tests/unit/app/test_runner_hook_runtime.py`
Expected: PASS.

- [ ] **Step 3: Inspect the final diff and graph impact.**

Run: `git diff v1.0.0...HEAD --check && node ../.gitnexus/run.cjs detect-changes --scope all --repo .`
Expected: no whitespace errors and a non-truncated graph result reviewed for
Hook Runtime, Tool Guard, and session persistence effects.

- [ ] **Step 4: Commit documentation updates.**

```bash
git add CONTEXT.md docs/superpowers/specs/2026-09-08-skill-hook-revision-sync-design.md docs/superpowers/plans/2026-09-08-skill-hook-revision-sync.md
git commit -m "docs: define skill hook revision boundary"
```
