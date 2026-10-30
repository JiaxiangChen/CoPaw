export type ChatStatus = "idle" | "running" | "stopping";

export interface ExpertBindingView {
  revision: number;
  expert_id: string | null;
  enabled: boolean;
  available: boolean;
  expert_name: string | null;
  reason: string | null;
}

export interface ExpertBindingUpdate {
  expected_revision: number;
  expert_id: string | null;
}

export interface ExpertBindingConflict {
  chat_id?: string;
  code:
    | "expert_binding_changed"
    | "expert_main_disabled"
    | "expert_unavailable";
  expert_binding: ExpertBindingView;
}

export interface ChatSpec {
  id: string; // Chat UUID identifier
  session_id: string; // Session identifier (channel:user_id format)
  user_id: string; // User identifier
  channel: string; // Channel name, default: "default"
  name?: string; // Chat display name
  created_at: string | null; // Chat creation timestamp (ISO 8601)
  updated_at: string | null; // Chat last update timestamp (ISO 8601)
  meta?: Record<string, unknown>; // Additional metadata
  status?: ChatStatus; // Conversation status: idle, running, or stopping
}

export interface ChatCreateRequest extends Partial<ChatSpec> {
  initial_expert_id?: string;
}

export interface ChatCreateResponse extends ChatSpec {
  expert_binding: ExpertBindingView | null;
}

export interface Message {
  role: string;
  content: unknown;
  timestamp?: string | null;
  [key: string]: unknown;
}

export interface ChatHistory {
  chat?: ChatSpec | null;
  messages: Message[];
  status?: ChatStatus; // Conversation status: idle, running, or stopping
  archive?: ChatArchiveMetadata;
}

export interface ChatCompactionBoundary {
  id: string;
  archived_message_count: number;
  first_message_id: string;
  last_message_id: string;
  created_at: string;
  first_timestamp?: string | null;
  last_timestamp?: string | null;
}

export interface ChatArchiveMetadata {
  has_more: boolean;
  boundaries: ChatCompactionBoundary[];
}

export interface ChatArchivePage extends ChatArchiveMetadata {
  messages: Message[];
  next_cursor?: string | null;
}

export interface ChatPage {
  items: ChatSpec[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  next_cursor?: string | null;
}

export interface ChatDeleteResponse {
  success: boolean;
  chat_id: string;
}

export interface ChatShareCreateResponse {
  token: string;
  share_path: string;
}

export interface ChatShareSnapshot {
  chat_name: string;
  messages: Message[];
}

export interface ChatShareOptions {
  chat_name: string;
  messages: Message[];
  turn_statuses: Record<string, string>;
}

export type SubAgentRunStatus =
  | "pending"
  | "running"
  | "paused"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "expired";

export interface SubAgentBudgetConsumption {
  elapsed_ms: number;
  timeout_ms: number;
  turns_used: number;
  max_turns: number;
  ratio: number;
}

export interface SubAgentRunSnapshotItem {
  run_id: string;
  agent_name: string;
  nickname?: string | null;
  objective: string;
  status: SubAgentRunStatus;
  stoppable: boolean;
  definition_match?: {
    matched: boolean;
    definition_name?: string | null;
    definition_source?: string | null;
    score?: number | null;
    reason?: string | null;
  };
  budget_consumption: SubAgentBudgetConsumption;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  summary_preview?: string | null;
  error_preview?: string | null;
}

export interface SubAgentRunSnapshot {
  chat_id: string;
  session_id: string;
  runs: SubAgentRunSnapshotItem[];
}

export interface SubAgentRunCancelResponse {
  run: SubAgentRunSnapshotItem;
}

export type GoalState =
  | "ACTIVE"
  | "WAITING"
  | "PAUSED"
  | "BLOCKED"
  | "LIMITED"
  | "INTERRUPTED"
  | "COMPLETE"
  | "CANCELLED";

export interface GoalCriterionSnapshot {
  criterion_id: string;
  verified: boolean;
  consecutive_failures: number;
  evidence_refs: string[];
  criterion: {
    requirement: string;
    observable_assertion: string;
    verification_method: string;
    expected_outcome: string;
  };
}

export interface GoalSnapshot {
  goal_id: string;
  subagent_delegation_enabled?: boolean;
  state: GoalState;
  revision: number;
  turn_budget: number;
  budget_cycle: number;
  turns_used: number;
  next_focus?: string | null;
  state_reason?: string | null;
  contract: {
    objective: string;
    completion_criteria: GoalCriterionSnapshot["criterion"][];
    constraints: { must_preserve: string[]; must_not_do: string[] };
    autonomy_boundary: string;
  };
  criteria: GoalCriterionSnapshot[];
}

// Legacy Session type alias for backward compatibility
export type Session = ChatSpec;
