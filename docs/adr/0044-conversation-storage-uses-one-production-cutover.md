---
status: accepted
---

# Conversation Storage Uses One Production Cutover

The NAS-to-MySQL conversation migration is rehearsed with representative workspace
scopes in isolation, then switches all production conversation scopes together.
The user selected this boundary on 2026-10-30. It replaces the draft's production
scope-by-scope rollout and avoids temporary backend-selection rules, mixed
production storage authorities, and scope-specific maintenance routing. Rehearsal
must still cover tenant/source/agent isolation and every supported legacy format.

All legacy conversation writers must exit before the new writers start. The user
subsequently selected one continuous outage for backup, migration, validation,
and installation, with no migration-window loss allowance; see ADR 0045, which
supersedes ADR 0040. Rollback preserves new committed writes. Execution-time
persistence cadence remains recorded separately in ADR 0039.
