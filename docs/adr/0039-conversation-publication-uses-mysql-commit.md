---
status: accepted
---

# Conversation Publication Uses the MySQL Commit Boundary

Following ADR 0038, conversation content is durably published as an immutable
NAS version before a short MySQL transaction publishes its references, ordering,
and associated state. The database commit makes that content visible as
Committed Conversation History. A failed or uncertain commit may leave an
unreferenced NAS object; recovery must resolve the original commit identity
before retrying publication or reclaiming that object. An acknowledged write
must not knowingly reference content that has not been durably stored.

The alternative of exposing metadata before its content is durable creates
visible records with missing bodies. Publishing content first instead accepts
temporary unreferenced storage and requires an explicit recovery and reclamation
protocol. Database and NAS do not become one atomic physical transaction.

The user confirmed on 2026-10-20 that restart recovery guarantees committed
content, not every displayed streaming increment. User-question admission
remains durable before Agent execution. Live increments may precede history
publication; success and terminal-history publication must reflect the
committed result. The intermediate persistence cadence was subsequently resolved
on 2026-10-30 as described below.

One identity-scoped Logical Chat Session executes one Answer Turn at a time;
a concurrent independent submission receives a busy result rather than implicit
queueing. Different Sessions may execute concurrently. Pod failure does not
trigger automatic cross-Pod continuation or replay of model/tool execution.
Storage-commit reconciliation is distinct from Agent execution and may recover
already durable results without invoking tools again.

Execution ownership is recorded in MySQL with a monotonically increasing
generation. Publications must validate the current execution authority; a
writer that loses valid authority stops advancing and cannot publish a late
result after another generation supersedes it. Expiration does not automatically
continue model/tool execution. This protects persisted state but does not undo
external effects of already-started tools. Lease timing and the precise
reconciliation protocol remain to be designed.

The deployment entry point guarantees session affinity, including reconnect,
Stop, and approval requests. The application will not add internal cross-Pod
request proxying for this redesign. Affinity routes requests; it does not
replace database ownership validation. If the owner dies, a subsequent request
may reconcile committed history but cannot attach to the dead process's live
stream or automatically resume its Agent. This explicit deployment assumption
avoids a second internal routing system.

The user selected semantic-boundary publication on 2026-10-30: completed tool
results, approval-state changes, and checkpoint/compaction/reset boundaries are
published, while plain streamed text is coalesced into bounded batches rather
than committed per token. Batch limits are determined by measurement. An uncertain
or failed boundary publication stops further model/tool advancement until the
original operation is reconciled; it never authorizes rerunning a completed tool.
Continuation requires confirmed commit and still-valid execution authority and
state. This improves retained intermediate progress without claiming atomicity
between external tool effects and storage, or automatic Agent continuation after
process failure. Approval business state remains owned by the approval service.
