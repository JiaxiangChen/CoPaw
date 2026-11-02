---
status: accepted
---

# Conversation Metadata Uses MySQL and Content Remains on NAS

The conversation storage redesign makes MySQL authoritative for Conversation
Metadata: Chat and Logical Session identities and ownership, Turn state, message
ordering and locators, archive boundaries, checkpoint epochs/revisions/watermarks
and references, and share metadata. Conversation Content remains on NAS,
including structured semantic checkpoint payloads, summaries, tool arguments and
results, and Agent recovery snapshots. Chat titles and content read lengths belong
to metadata; body previews and summaries are not duplicated into the database
by default. SubAgent and Goal retain ownership of their independent business
state; this redesign covers their conversation associations.

This replaces the previous architectural direction in which NAS session and
journal roots remained authoritative and a database could serve only as a
derived index. The split centralizes queryable identity and lifecycle state
while keeping potentially large payloads on shared storage. It introduces a
cross-store publication and recovery problem, whose protocol still requires
separate design.

MySQL is a required runtime dependency for this architecture. Database failure
pauses new conversation writes; there is no automatic fallback to file-only
writes or a separately maintained database-free mode. This decision was
confirmed on 2026-10-20 and records the target architecture, not an implemented
migration. Publication ordering and execution boundaries were subsequently
confirmed in ADR 0039, including subsequent ownership and deployment-affinity
decisions. Existing independent Chat, Logical Session, and permanent-share
retention semantics are preserved: deletion first removes the relevant Chat's
visibility, and physical reclamation respects live references and recovery or
backup retention. No age-based deletion of valid history is introduced.
History paging and direct Turn reads are included in the redesign while
established API semantics remain compatible. Backup and cutover trade-offs
were initially confirmed in ADR 0040 and updated to a continuous-outage migration
in ADR 0045; their detailed protocols are proposed in the
conversation storage design.

The subsequent simplification decision removes computed content checksums,
identity/request digests, and source fingerprints from the new storage design.
Publication still requires durable immutable NAS content before the MySQL commit;
it does not promise detection of arbitrary same-length silent storage corruption.
History paging uses stable order keys and a captured upper bound, not persistent
Message/Turn version intervals or cross-request historical state replay. Current
deletions and authorization take effect immediately. Internal entities use database
integer IDs; established external Chat UUIDs and unpredictable share tokens remain.
