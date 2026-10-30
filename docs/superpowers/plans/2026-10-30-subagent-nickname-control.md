# SubAgent Nickname Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Main Agent control Background SubAgents by Chat-unique nicknames, never internal run IDs, and wait for a chosen group until all are terminal or its timeout expires.

**Architecture:** `run_id` remains the worker and persisted-record key. The supervisor allocates a unique, immutable nickname for every Chat-owned run; the model-facing tool adapter resolves nicknames to run records before calling the supervisor. The selected-expert compatibility flow becomes catalog filtering only, with ordinary `start_subagent`, `get_subagent`, grouped `wait_subagent`, and `cancel_subagent` as the sole control protocol.

**Tech Stack:** Python 3, Pydantic, asyncio, pytest, AgentScope toolkit.

---

## File map

- Modify: `src/swe/app/subagents/nicknames.py` — derive a base nickname and allocate a collision-free Chat nickname.
- Modify: `src/swe/app/subagents/run_access.py` — resolve one Chat-owned retained record by nickname without exposing its run ID upstream.
- Modify: `src/swe/app/subagents/supervisor.py` — reserve nicknames atomically while concurrent starts are being prepared.
- Modify: `src/swe/agents/tools/subagent_background.py` — expose the expert-name/nickname tool protocol, grouped wait semantics, and sanitized responses.
- Modify: `src/swe/agents/react_agent.py` — retain selected-expert catalog filtering but remove selected-expert forced-control registration and cancellation behavior.
- Modify: `src/swe/agents/tool_guard_mixin.py` — remove synthetic selected-expert start/wait replay and result-summary branches.
- Modify: `src/swe/app/runner/runner.py` — keep selected expert identity in request context without forcing a Background SubAgent run.
- Modify: `tests/unit/subagents/test_background_tools.py` — cover the tool contract and no-ID response projection.
- Modify: `tests/unit/subagents/test_chat_runtime_access.py` — cover nickname authorization and target/group waiting behavior.
- Modify: `tests/unit/subagents/test_background_supervisor.py` — cover concurrent nickname collision reservation.
- Modify: `tests/unit/subagents/test_react_agent_and_guard_integration.py` — remove forced selected-expert replay assertions and prove ordinary Main orchestration.
- Modify: `tests/unit/agents/test_tool_guard_hook_runtime.py` and `tests/unit/agents/test_tool_output_budget_mixin.py` — remove the retired `wait_selected_expert` coverage.

### Task 1: Allocate a durable, Chat-unique nickname

**Files:**
- Modify: `src/swe/app/subagents/nicknames.py`
- Modify: `src/swe/app/subagents/run_access.py`
- Modify: `src/swe/app/subagents/supervisor.py:173-234,236-405`
- Test: `tests/unit/subagents/test_background_supervisor.py`
- Test: `tests/unit/subagents/test_chat_runtime_access.py`

- [ ] **Step 1: Write failing nickname allocation tests**

Add tests that start two runs whose definitions both configure `nickname="研究员"`, and assert their persisted records contain `研究员` and `研究员-2`. Add a concurrent-start test using a launch barrier so both reservations occur before either record is durable; assert the returned nicknames differ. Add a Chat-scope lookup test: the same nickname in another Chat must not resolve.

```python
assert first.nickname == "研究员"
assert second.nickname == "研究员-2"
assert first.nickname != second.nickname
assert read_chat_run_by_nickname(run_dir, other_chat, "研究员") is None
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `venv/bin/python -m pytest tests/unit/subagents/test_background_supervisor.py tests/unit/subagents/test_chat_runtime_access.py -v`

Expected: FAIL because no Chat-unique allocator or nickname lookup exists.

- [ ] **Step 3: Add allocation and lookup primitives**

Keep `assign_subagent_nickname()` as the base-name source. Add a pure allocator that returns the base when unused and otherwise appends the smallest unused positive suffix beginning at `-2`. In `run_access.py`, add `read_chat_run_by_nickname(run_store_dir, request_context, nickname)` that filters `read_chat_run_records()` and returns exactly one matching record, otherwise `None`.

```python
def allocate_subagent_nickname(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"

def read_chat_run_by_nickname(run_store_dir, request_context, nickname):
    matches = [
        record for record in read_chat_run_records(run_store_dir, request_context)
        if record.nickname == nickname
    ]
    return matches[0] if len(matches) == 1 else None
```

In `BackgroundSubAgentSupervisor.start()`, reserve the allocated nickname before its first `await`; include nicknames already persisted for this Chat and nicknames reserved by in-flight starts in the same scope. Pass the reservation into `_start_reserved()`, persist it as `nickname`, and release it in `finally` after the record has been created or the launch fails. Do not change `run_id` allocation or storage filenames.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `venv/bin/python -m pytest tests/unit/subagents/test_background_supervisor.py tests/unit/subagents/test_chat_runtime_access.py -v`

Expected: PASS; names are unique for retained records and concurrent starts.

- [ ] **Step 5: Commit the nickname identity boundary**

```bash
git add src/swe/app/subagents/nicknames.py src/swe/app/subagents/run_access.py src/swe/app/subagents/supervisor.py tests/unit/subagents/test_background_supervisor.py tests/unit/subagents/test_chat_runtime_access.py
git commit -m "feat(subagents): allocate chat-unique nicknames"
```

### Task 2: Replace model-facing run IDs with expert names and nicknames

**Files:**
- Modify: `src/swe/agents/tools/subagent_background.py:147-163,281-411,681-762,903-1025`
- Test: `tests/unit/subagents/test_background_tools.py`
- Test: `tests/unit/subagents/test_chat_runtime_access.py`

- [ ] **Step 1: Write failing protocol tests**

Test `start_subagent(expert_name="plan-researcher", objective="Inspect")` returns `expert_name`, `nickname`, `status`, and `objective`, but contains no string key named `run_id`. Test `get_subagent(nickname="研究员")` and `cancel_subagent(nickname="研究员")` resolve the retained Chat-owned record. Test every model-facing failure path: unknown nickname, concurrency-blocked start, and non-manageable cancellation must also omit `run_id` and `active_run_ids`.

```python
assert payload["expert_name"] == "plan-researcher"
assert payload["nickname"] == "研究员"
assert "run_id" not in json.dumps(payload)
assert "active_run_ids" not in json.dumps(payload)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `venv/bin/python -m pytest tests/unit/subagents/test_background_tools.py tests/unit/subagents/test_chat_runtime_access.py -v`

Expected: FAIL because the current adapter accepts `name` and serializes `run_id`.

- [ ] **Step 3: Implement the sanitized tool adapter**

Expose `expert_name` in the callable signature and map it to the existing internal `SubAgentStartRequest.name`; keep the Pydantic model and definition resolver unchanged. Replace `_parent_facing_record()` with a model-facing projection that includes only `expert_name`, `nickname`, `status`, `objective`, and the compact terminal `result` when requested. Resolve nickname arguments through `read_chat_run_by_nickname()` before calling `supervisor.get()` or `supervisor.cancel()` with the internal `record.run_id`.

For start-blocked responses, look up each internal active record only if it belongs to the current Chat, project their nicknames as `active_nicknames`, and otherwise return the capacity limit without a worker identity. For a non-manageable cancellation, return the persisted projected record with `manageable: false`; never serialize the supervisor's `run_id` field.

```python
async def cancel_subagent(nickname: str) -> ToolResponse:
    record = await _resolve_chat_nickname(context, nickname)
    if record is None:
        return _json_response({"status": "not_found", "nickname": nickname})
    result = await context.supervisor.cancel(context.tool_scope, record.run_id)
    return _json_response(_model_subagent_record(result or record))
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `venv/bin/python -m pytest tests/unit/subagents/test_background_tools.py tests/unit/subagents/test_chat_runtime_access.py -v`

Expected: PASS; model inputs and outputs use only expert names and nicknames.

- [ ] **Step 5: Commit the model-facing identifier migration**

```bash
git add src/swe/agents/tools/subagent_background.py tests/unit/subagents/test_background_tools.py tests/unit/subagents/test_chat_runtime_access.py
git commit -m "feat(subagents): control runs by nickname"
```

### Task 3: Add targeted and grouped wait barriers

**Files:**
- Modify: `src/swe/agents/tools/subagent_background.py:401-411,646-678,916-929`
- Test: `tests/unit/subagents/test_background_tools.py`
- Test: `tests/unit/subagents/test_chat_runtime_access.py`

- [ ] **Step 1: Write failing wait tests**

Add tests for `wait_subagent(nicknames=["研究员", "审查员"], timeout_ms=50)`: when only one becomes terminal, the task must remain waiting until the other terminalizes or the timeout expires. Verify a prior unrelated terminal run does not short-circuit targeted waiting. Verify all-terminal targets return immediately, an unknown nickname returns `not_found` before waiting, and an explicit empty list is `invalid_request`.

```python
result = await tools["wait_subagent"](
    nicknames=["研究员", "审查员"], timeout_ms=50,
)
assert result["timed_out"] is True
assert {item["nickname"] for item in result["subagents"]} == {"研究员", "审查员"}
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `venv/bin/python -m pytest tests/unit/subagents/test_background_tools.py tests/unit/subagents/test_chat_runtime_access.py -v`

Expected: FAIL because `wait_subagent` only accepts `timeout_ms` and exits on any terminal record.

- [ ] **Step 3: Implement a fixed-target wait helper**

Change the public signature to `wait_subagent(nicknames: list[str] | None = None, timeout_ms: int = 3000)`. With `nicknames is None`, preserve the current Chat-level wait timing but project records without run IDs. With a non-empty list, resolve every nickname once, retain their internal run IDs in a local target map, then repeatedly reap and reload only those records. Return early only when every target status is terminal; otherwise sleep in the existing short polling interval until the deadline. At deadline, return `timed_out: true` and the latest projection for every target. Do not call `cancel()` from this helper.

```python
while True:
    records = await _reload_target_records(context, targets)
    if all(record.status in TERMINAL_BACKGROUND_RUN_STATUSES for record in records):
        return _group_wait_response(records, timed_out=False)
    if time.monotonic() >= deadline:
        return _group_wait_response(records, timed_out=True)
    await asyncio.sleep(min(0.05, deadline - time.monotonic()))
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `venv/bin/python -m pytest tests/unit/subagents/test_background_tools.py tests/unit/subagents/test_chat_runtime_access.py -v`

Expected: PASS; group waits ignore unrelated terminals and do not cancel active targets.

- [ ] **Step 5: Commit grouped wait behavior**

```bash
git add src/swe/agents/tools/subagent_background.py tests/unit/subagents/test_background_tools.py tests/unit/subagents/test_chat_runtime_access.py
git commit -m "feat(subagents): wait for nickname groups"
```

### Task 4: Make expert selection a catalog filter, not a forced execution

**Files:**
- Modify: `src/swe/agents/tools/subagent_background.py:414-644,765-823`
- Modify: `src/swe/agents/react_agent.py:884-1045,2671-2705`
- Modify: `src/swe/agents/tool_guard_mixin.py:2520-2795`
- Modify: `src/swe/app/runner/runner.py:4590-4745,7289-7293`
- Test: `tests/unit/subagents/test_react_agent_and_guard_integration.py`
- Test: `tests/unit/agents/test_tool_guard_hook_runtime.py`
- Test: `tests/unit/agents/test_tool_output_budget_mixin.py`

- [ ] **Step 1: Write failing selected-expert behavior tests**

Add an integration test that supplies `selected_expert_id` and verifies the background tool catalog exposes only the selected definition, but does not synthesize `start_subagent` or any wait tool call. Add a test that the Main Agent can later start that expert normally and keep answering while it remains active. Update cancellation tests to prove parent-turn interruption does not automatically cancel the ordinary background run.

```python
assert available_definition_names == ["selected-expert"]
assert forced_calls == []
assert response.get_text_content() == "Main can continue"
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `venv/bin/python -m pytest tests/unit/subagents/test_react_agent_and_guard_integration.py tests/unit/agents/test_tool_guard_hook_runtime.py tests/unit/agents/test_tool_output_budget_mixin.py -v`

Expected: FAIL because selected-expert state currently forces start and `wait_selected_expert` replay.

- [ ] **Step 3: Remove the dedicated selected-expert lifecycle**

Delete `_create_selected_expert_wait_tool()` and its polling, authorization, cancellation, deadline, and serialization helpers. Stop registering `wait_selected_expert` in `create_background_subagent_tools()` and remove it from `SWEAgent._background_subagent_tool_names()`.

In the runner, replace `_apply_selected_expert_context()`'s forced-call behavior with the minimal context assignment below; retain `_build_definition_catalog()`'s existing selected-definition branch so `start_subagent` can launch only the selected definition. Delete selected-expert replay handling, summary evidence insertion, and parent-turn cancellation code from `ToolGuardMixin` and `SWEAgent`.

```python
if selected_expert_id:
    request_context["selected_expert_id"] = selected_expert_id
```

Do not modify `expert_as_main_enabled=true`: its `expert_main_execution` guard must still prevent all Background SubAgent tools.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `venv/bin/python -m pytest tests/unit/subagents/test_react_agent_and_guard_integration.py tests/unit/agents/test_tool_guard_hook_runtime.py tests/unit/agents/test_tool_output_budget_mixin.py -v`

Expected: PASS; selected experts are launch-filtered and no special wait tool remains.

- [ ] **Step 5: Commit the selection semantic change**

```bash
git add src/swe/agents/tools/subagent_background.py src/swe/agents/react_agent.py src/swe/agents/tool_guard_mixin.py src/swe/app/runner/runner.py tests/unit/subagents/test_react_agent_and_guard_integration.py tests/unit/agents/test_tool_guard_hook_runtime.py tests/unit/agents/test_tool_output_budget_mixin.py
git commit -m "refactor(experts): make selected experts delegable"
```

### Task 5: Run compatibility and regression verification

**Files:**
- Modify only if failures expose an assertion coupled to `run_id` or `wait_selected_expert`.
- Test: `tests/unit/subagents/`
- Test: `tests/unit/agents/test_complexity_budget.py`
- Test: `tests/unit/app/test_expert_main_preparation.py`

- [ ] **Step 1: Run the complete focused regression set**

Run: `venv/bin/python -m pytest tests/unit/subagents/ tests/unit/agents/test_complexity_budget.py tests/unit/app/test_expert_main_preparation.py -v`

Expected: PASS; Background SubAgent control works by nickname, expert-main remains isolated, and complexity-budget assertions stay valid.

- [ ] **Step 2: Run static checks for retired protocol names**

Run: `rg -n "wait_selected_expert|selected_expert_execution|selected_expert_run_id|_selected_expert_wait_authorized" src/swe tests`

Expected: no production references; only deliberately retained migration documentation is acceptable, and no test calls the retired tool.

- [ ] **Step 3: Run the project checks for touched Python files**

Run: `venv/bin/python -m pre_commit run --files src/swe/app/subagents/nicknames.py src/swe/app/subagents/run_access.py src/swe/app/subagents/supervisor.py src/swe/agents/tools/subagent_background.py src/swe/agents/react_agent.py src/swe/agents/tool_guard_mixin.py src/swe/app/runner/runner.py`

Expected: all applicable hooks pass.

- [ ] **Step 4: Commit only necessary regression repairs**

```bash
git add src/swe tests
git commit -m "test(subagents): cover nickname control protocol"
```
