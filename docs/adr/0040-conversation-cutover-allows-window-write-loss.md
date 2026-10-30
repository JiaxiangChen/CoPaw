---
status: superseded by ADR-0045
---

# Conversation Cutover Allows Limited Window Write Loss

Historical decision only. On 2026-10-30 the user selected one continuous outage
with preservation of the final drained legacy state. [ADR 0045](0045-conversation-migration-uses-one-continuous-outage.md)
supersedes the window-loss allowance and two-window procedure below, and carries
forward the fixed-image rollback and complete-backup requirements.

For the MySQL/NAS conversation redesign, the user explicitly accepts losing
the very small volume of legacy writes that may occur after the selected
migration source snapshot and before cutover completes. The migration will not
build a dedicated write-rejection gate, change-data capture, dual writes, or
incremental catch-up solely to preserve those writes. This choice limits
cutover complexity at the cost of acknowledged migration-window loss.

The migration source snapshot must still be internally consistent and fully
validated. The exception does not permit silently skipping damaged snapshot
records, mixing half-written files, discarding unrelated data, or weakening
normal publication guarantees after cutover. Rollback exports must preserve
the new system's subsequently committed changes.

Routine backups have a different boundary: short scoped write quiescence is
allowed to establish a matching database snapshot and NAS content retention
set. Copying can continue after writes resume while referenced objects remain
protected. Backup success requires complete metadata and content for the
declared recovery point; migration-window loss tolerance does not apply.

Confirmed on 2026-10-20. This is design authorization for the trade-off,
not a record of a production migration or actual data loss.

On 2026-10-30 the user chose two short write quiescence windows: first drain
conversation writers and capture a stable, complete source snapshot, then resume
the legacy service during offline import and validation; finally stop all legacy
writers again and switch production as one unit (ADR 0044). Actual outage lengths
must be measured; without a validated storage snapshot, stable copying is included
in the first window. This uses deployment maintenance controls rather than adding
a dedicated application write-rejection protocol. The interval and volume of
discarded legacy-window writing must remain explicit, not silently expand to an
unbounded allowance.

The rollback compatibility target is the last production image digest before
cutover, fixed in the migration manifest. The exporter must preserve all new
committed state in formats that this image actually supports; compatibility with
additional older images is outside scope. If the target cannot represent new
state, export compatibility must be fixed before rollout. Restoring the original
snapshot or merely reverting the image is not a valid rollback after new commits.

The user explicitly clarified the window-loss scope on 2026-10-30: all legacy
conversation changes after the source snapshot may be discarded, including Chat
deletion, newly created permanent shares, titles, expert bindings, messages, and
share access counters. A Chat deleted during this window can therefore reappear
after cutover, and a newly issued share token can become unavailable. The runbook
must state these concrete consequences. This exception applies only to the
migration window; source-snapshot deletion markers and shares remain mandatory,
and normal post-cutover deletion/publication guarantees are unchanged. It does
not authorize reverting unrelated business tables to the conversation snapshot.
