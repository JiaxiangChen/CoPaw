# Skill Hook Revision Sync Design

**Status:** Approved

## Goal

Keep the Skill-owned Hook configuration used by an activated session aligned with
the current `hooks/hooks.json` at the next Hook event boundary.

## Scope

Only Skill-owned Hooks participate. Tenant Hooks and Agent Profile Hooks keep
their existing configuration and refresh behavior.

## Event Semantics

- A Hook event resolves exactly one configuration snapshot before it plans or
  starts handlers.
- A handler already executing when a file changes is allowed to finish.
- The next Hook event detects the change and uses the new configuration.
- Deleting the file, setting `enabled` to false, or making it unreadable or
  invalid withdraws that Skill's Hooks. The runtime never falls back to the
  prior valid configuration.
- Restoring a valid enabled file re-enables the Hooks for a previously
  activated Skill without another selection.

## State and Refresh

An activated Skill has a persisted monitoring record independent of its current
effective Hook source. The record stores the skill root, source path, and a
cheap file version marker derived from `hooks.json` existence and stat data.

At every Hook event, the runtime compares each monitored source's current file
version marker to its cached marker. An unchanged marker reuses the parsed
source without a file read. A changed marker reads and validates the file,
then either replaces the parsed source or withdraws it. This per-session lazy
refresh avoids a bulk rewrite of all active sessions and remains correct after
a process restart because the marker and monitoring record are persisted with
the overlay.

Only `hooks.json` participates in revision detection. Changes below
`scripts/` retain the existing execution-time behavior and do not reload Hook
configuration.

## One-time Handlers and Overlays

When a source is replaced, one-time execution records survive only for handlers
whose normalized definition is identical. Records for changed or removed
handlers are removed. Source-owned overlay entries whose handler no longer
exists are removed together with the withdrawn or replaced source.

## Verification

Focused unit tests cover unchanged-marker fast path, edit, deletion, disabled
configuration, invalid JSON, recovery after restoration, source overlay
cleanup, and per-handler one-time record retention. Runtime tests prove the
refresh occurs before event planning and does not affect tenant or Agent
Profile Hooks.
