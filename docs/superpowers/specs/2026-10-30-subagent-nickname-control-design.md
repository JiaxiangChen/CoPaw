# SubAgent Nickname Control Design

## Goal

When `expert_as_main_enabled` is disabled, let the Main Agent start, inspect,
wait for, and cancel Background SubAgents without ever seeing an internal
`run_id`. The model uses meaningful expert names to choose a definition and a
Chat-unique nickname to refer to one concrete execution.

This applies equally to a user-selected expert and to a SubAgent launched by
the Main Agent. Selection narrows which definitions `start_subagent` may
launch; it does not force the selected expert to own or complete the turn.

## Terminology and identity boundary

| Identifier | Audience | Meaning |
| --- | --- | --- |
| `expert_name` | Main Agent | The definition to launch. |
| `nickname` | Main Agent | A Chat-unique, persistent handle for one SubAgent run. |
| `run_id` | Backend only | The opaque primary key used by storage, workers, and the supervisor. |

The model-facing protocol must not expose `run_id`, including in successful
responses, concurrency-limit responses, not-manageable responses, or errors.
The console and backend may retain it for their existing operational APIs.

## Nickname allocation

The Background SubAgent Supervisor allocates the nickname before it persists a
run record.

1. Use the definition's configured nickname when present; otherwise use the
   existing generated nickname as the base.
2. Compare the base against all retained runs belonging to the current Chat.
3. Allocate the base when unused; otherwise append the lowest available numeric
   suffix: `安全审查员`, `安全审查员-2`, `安全审查员-3`.
4. Persist the allocated nickname in the run record and never change or reuse
   it while that Chat retains the record.

This makes a nickname an unambiguous model-side reference even when the same
expert is launched concurrently or a historical run is still visible.

## Tool contract

The four Background SubAgent tools use names and nicknames only.

```text
start_subagent(expert_name, objective, background="")
get_subagent(nickname, include_details=false)
wait_subagent(nickname=null, timeout_ms=3000)
cancel_subagent(nickname)
```

`start_subagent` validates `expert_name` against the current definition
catalog. When an expert is selected, the catalog contains only that selected
expert. The Main Agent therefore cannot launch another expert through this
tool, but remains free to decide whether and when to launch the selected one.

A successful start returns the allocated nickname, expert name, status, and
objective. It does not return a run ID.

`get_subagent`, targeted `wait_subagent`, and `cancel_subagent` first resolve
the supplied nickname to one Chat-owned record, then perform the existing
operation using its internal run ID. An unknown nickname, or one outside the
current Chat, produces `not_found`.

Without a nickname, `wait_subagent` retains its Chat-level monitoring role,
but reports `active_subagents` and `terminal_subagents` using model-facing
fields only. With a nickname, it waits only for that run, returning when it
becomes terminal or when `timeout_ms` elapses. A timeout does not cancel the
SubAgent.

`cancel_subagent` is idempotent: a terminal SubAgent returns its current
terminal state rather than an operation failure.

## Removal of selected-expert control flow

Remove the dedicated `wait_selected_expert` tool and its special state:

- no forced `start_subagent -> wait_selected_expert` replay;
- no one-time selected-expert wait authorization;
- no automatic cancellation tied to the parent turn ending;
- no selected-expert-only result summarization path.

The normal Tool Guard continues to govern `start_subagent` and every later
operation. A Main Agent may keep responding while SubAgents run, wait for one
or many later, or cancel one explicitly.

The `expert_as_main_enabled=true` path is intentionally unchanged: it is a
separate, bounded expert-main execution and does not expose Background
SubAgent orchestration tools.

## Error handling

- `expert_name` not in the permitted catalog: `not_found` or
  `expert_not_available`, without leaking internal definition identifiers.
- unresolved nickname: `not_found`, without revealing whether an unrelated
  Chat owns a matching run.
- concurrency exhausted: return the configured capacity and model-facing
  nicknames of Chat-visible active SubAgents, never `active_run_ids`.
- worker no longer manageable: return the persisted model-facing status. The
  Main Agent can still inspect a retained record; cancellation reports the
  terminal or non-manageable state without an internal ID.

## Verification

Tests must cover:

1. configured and generated nickname allocation, collision suffixing, and no
   reuse among retained Chat records;
2. no `run_id` in each model-facing tool response, including every error path;
3. selected-expert catalog filtering still blocks launches of other experts;
4. targeted wait ignores other terminal runs and times out without cancelling
   its target;
5. Chat-level wait remains backwards-compatible in timing and status behavior,
   except for replacing internal IDs with nicknames;
6. get, wait, and cancel reject nicknames outside the current Chat;
7. Main can manage a previously created Chat-owned SubAgent by nickname;
8. removal of forced selected-expert start/wait and interruption cancellation;
9. `expert_as_main_enabled=true` remains isolated from these tools.
