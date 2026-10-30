---
status: accepted
---

# Conversation Migration Uses One Continuous Outage

On 2026-10-30 the user replaced the two-window migration with one continuous
production outage: prepare the new release, drain and stop every legacy
conversation writer, capture the final consistent source, import and fully
validate it, then start the new service and reopen traffic. All production
conversation scopes switch together as in ADR 0044. Preparing a release does
not authorize starting its writers before migration succeeds.

This supersedes ADR 0040's allowance for migration-window write loss and the
subsequent two-window procedure. Every legacy conversation change committed
before the drained source boundary must be preserved, including deletion,
permanent shares, access counters, titles, bindings, and message/recovery state.
The legacy service remains stopped during backup, import, verification, and
table installation. Migration does not add dual writes, CDC, incremental catch-up,
or a dedicated application write gate; existing deployment maintenance controls
keep writers and triggers stopped. The cost is an outage covering all those steps,
whose duration must be measured before scheduling production maintenance.

Before any new-version write occurs, migration failure may return to the fixed
legacy image with its untouched final NAS tree and original share table. Once
the new version has committed any write, including smoke-test writes or share
access counters, rollback must reconcile unknown publications and export the
complete new committed state into formats supported by that fixed legacy image.
An image-only rollback or restoring the pre-migration snapshot would lose commits.
No unrelated business tables are restored as part of conversation rollback.

Routine backups continue to require a matching metadata snapshot and protected
NAS reference/dependency closure, with complete restore validation. This cutover
decision does not weaken normal publication, deletion, or backup guarantees.
