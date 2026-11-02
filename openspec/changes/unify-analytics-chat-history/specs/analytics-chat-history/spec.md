## ADDED Requirements

### Requirement: Target-user history shares Chat assembly

The analytics detail endpoint SHALL resolve the target source, user and Agent workspace and SHALL reuse the same Chat history builder, including v3 turn materialization, failure messages, hidden-context redaction and archive metadata. It MUST NOT impersonate the current requester or call the current-user Chat HTTP endpoint.

#### Scenario: Equivalent persisted snapshot
- **WHEN** authorized Chat and analytics requests read the same fixed target workspace and chat snapshot
- **THEN** their message content, ordering, archive metadata and persisted status SHALL agree

#### Scenario: Running chat
- **WHEN** analytics reads history while an answer is running
- **THEN** it SHALL read the latest committed snapshot without waiting for model completion and SHALL NOT subscribe to the live answer stream

### Requirement: Consistent target access boundary

All analytics mapping, detail and archive endpoints MUST apply the same confirmed operator access policy and MUST restrict reads to the resolved target user/source/Agent. The policy authority SHALL remain the existing external platform/gateway, as confirmed by the user. No new SWE role or branch authorization SHALL be introduced; access to the active-user ranking grants browsing of its corresponding user histories at that external boundary.

#### Scenario: Chat belongs to another target
- **WHEN** the supplied chat_id does not belong to the resolved target context
- **THEN** the endpoint SHALL return 404 without returning its messages

#### Scenario: Operator lacks access
- **WHEN** the confirmed access policy denies the operator access to a target user
- **THEN** mapping, detail and archive access SHALL be rejected at the confirmed enforcement boundary before target data is returned

### Requirement: Dedicated analytics archive paging

The system SHALL expose `GET /api/tracing/chats/{chat_id}/history` with required user_id, optional before and limit between 1 and 50 (default 50). It SHALL reuse Chat archive paging and its ChatArchivePage response, including redacted messages, boundaries, has_more and next_cursor.

#### Scenario: Multiple pages
- **WHEN** authorized requests page through a fixed archive using the returned cursors
- **THEN** the resulting raw messages and boundaries SHALL match Chat archive pages for that same snapshot and parameters

#### Scenario: Invalid cursor or limit
- **WHEN** a cursor is malformed or limit is outside the allowed range
- **THEN** the API SHALL return 422 rather than a successful empty page

### Requirement: Read-only archived history browsing

The analytics viewer SHALL offer an accessible load-earlier action when archives exist, request pages with limit=20 through the analytics API, and reuse Chat message conversion and boundary assembly. It SHALL preserve reading position, deduplicate messages and boundaries, and remain read-only.

#### Scenario: Empty current snapshot with archives
- **WHEN** detail contains no current messages but archive.has_more is true
- **THEN** the viewer SHALL still allow archived history to be loaded

#### Scenario: Prepending a page
- **WHEN** an earlier page is loaded
- **THEN** it SHALL be merged in chronological order without duplicate messages or boundaries and SHALL preserve the visible reading anchor

#### Scenario: Archive exhausted
- **WHEN** a page reports has_more=false
- **THEN** the viewer SHALL show that the beginning was reached and SHALL stop further archive requests

#### Scenario: Read-only message cards
- **WHEN** history contains runtime cards that support actions in the main Chat
- **THEN** the analytics viewer SHALL NOT permit sending, approval, stopping or resuming execution from those cards

### Requirement: Paging state isolation and recoverable failure

The viewer MUST isolate history state by source, target user, Agent and chat identity, discard stale responses, and prevent concurrent page loads. Failed requests SHALL retain messages and cursor and expose a retry action. A non-advancing cursor with has_more=true SHALL be treated as an error.

#### Scenario: Switch during a request
- **WHEN** a detail or archive response arrives after the target identity changed or the viewer closed
- **THEN** it SHALL NOT change the active viewer messages, cursor or feedback

#### Scenario: Retry after network failure
- **WHEN** an archive request fails and the user retries
- **THEN** the viewer SHALL retain existing messages and retry the same cursor

### Requirement: Monitor selection and complete history remain separate

The change SHALL preserve Monitor ranking and session filters. Selecting a matching session SHALL expose its complete available history through paging rather than restrict message timestamps to the ranking date range. Missing mappings and request failures SHALL be distinguishable from successful empty history.

#### Scenario: Session overlaps the ranking window
- **WHEN** an included session has archived messages outside the selected ranking dates
- **THEN** those archived messages SHALL remain readable through the analytics history endpoint

#### Scenario: History unavailable
- **WHEN** a trace session has no ChatSpec mapping or its history request fails
- **THEN** the viewer SHALL show an appropriate unavailable or error state instead of presenting it as a successfully loaded empty conversation
