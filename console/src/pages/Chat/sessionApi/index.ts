// ==================== 组件引入方式变更 (Kun He) ====================
import {
  IAgentScopeRuntimeWebUISession,
  IAgentScopeRuntimeWebUISessionAPI,
  IAgentScopeRuntimeWebUIMessage,
  IAgentScopeRuntimeWebUISessionUpdateOptions,
} from "@/components/agentscope-chat";
// ==================== 组件引入方式变更结束 ====================
import api, {
  type ChatSpec,
  type ChatHistory,
  type ChatCompactionBoundary,
  type ChatStatus,
  type Message,
} from "../../../api";
import { listTaskSources, taskPresence } from "@/api/modules/cronTaskSources";
import { currentCronContext, cronAccessKey } from "@/api/cronTaskContext";
import type {
  ChatApprovalActionCardData,
  ChatPlanInteractionCardData,
  ChatPlanReviewCardData,
  ChatRuntimeRequestCardData,
  ChatRuntimeResponseCardData,
  ChatTaskRunGroupCardData,
  PlanReviewDecision,
} from "../messageMeta";
import {
  extractPlanInteractionCard,
  resolveGroupTimestamp,
  resolveMessageTimestamp,
} from "../messageMeta";
import { toDisplayUrl } from "../utils";
import { readStopAppendMetadata, stopAppendBubbleId } from "../stopAppend";
import {
  mergeHistoryResponses,
  normalizeHistoryMessages,
} from "./historyAssembly";
import { applyPreferredSessionSelection } from "./preferredSession";
import { shouldNotifySessionSelected } from "./sessionRaceGuard";
import { createLocalSessionId } from "./localSessionId";
import { filterStaleTaskSessions } from "./taskSessions";
import {
  forgetResolvedChatId,
  forgetResolvedChatIdsForChat,
  getResolvedChatId,
  rememberResolvedChatId,
} from "./resolvedSessionMapping";

// ==================== userId 统一整改 (Kun He) ====================
// 使用统一的 getUserId/getChannel helper
import {
  getUserId,
  getChannel,
  getUserIdWithoutWindow,
  getChannelWithoutWindow,
} from "../../../utils/identity";
// ==================== userId 统一整改结束 ====================

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_SESSION_NAME = "新会话";
const ROLE_TOOL = "tool";
const ROLE_USER = "user";
const ROLE_ASSISTANT = "assistant";
const TYPE_PLUGIN_CALL_OUTPUT = "plugin_call_output";
// const CARD_REQUEST = "AgentScopeRuntimeRequestCard";
const CARD_RESPONSE = "AgentScopeRuntimeResponseCard";
const CARD_RESPONSE_FEEDBACK = "ResponseFeedback";
const CARD_APPROVAL_ACTION = "ApprovalAction";
const CARD_PLAN_INTERACTION = "PlanInteraction";
const CARD_TASK_RUN = "TaskRunGroupCard";
const CARD_COMPACTION_BOUNDARY = "ConversationCompactionBoundary";
const TASK_SESSION_KIND = "task";
const TASK_RUN_SECTION_STEP = "step";
const TASK_RUN_SECTION_FINAL = "final";
const SESSION_TITLE_GENERATED_META_KEY = "session_title_generated";
const DEFAULT_SESSION_PAGE_SIZE = 100;

// ---------------------------------------------------------------------------
// Window globals
// ---------------------------------------------------------------------------

interface CustomWindow extends Window {
  currentSessionId?: string;
  currentUserId?: string;
  currentChannel?: string;
}

declare const window: CustomWindow;

function getSessionPageSize(): number {
  const configured = window.__env__?.chatSessionPageSize;
  const pageSize =
    typeof configured === "number"
      ? configured
      : Number.parseInt(String(configured || ""), 10);

  return Number.isInteger(pageSize) && pageSize > 0
    ? pageSize
    : DEFAULT_SESSION_PAGE_SIZE;
}

// ---------------------------------------------------------------------------
// Local helper types
// ---------------------------------------------------------------------------

/** A single item inside a message's content array. */
interface ContentItem {
  type: string;
  text?: string;
  [key: string]: unknown;
}

/** A backend message after role-normalisation (output of toOutputMessage). */
interface OutputMessage extends Omit<Message, "role"> {
  role: string;
  metadata: unknown;
  sequence_number?: number;
}

interface TaskRunMetadata {
  runId: string;
  runIndex: number;
  section: string;
}

interface SubmittedPlanReview {
  planId: string;
  decision: PlanReviewDecision;
  feedback?: string;
}

function extractApprovalAction(
  message: OutputMessage,
): ChatApprovalActionCardData | null {
  const metadata =
    message.metadata && typeof message.metadata === "object"
      ? (message.metadata as Record<string, unknown>)
      : null;
  if (!metadata) return null;

  const direct = metadata.approval_action;
  if (direct && typeof direct === "object") {
    return direct as ChatApprovalActionCardData;
  }

  const nested = metadata.metadata;
  if (nested && typeof nested === "object") {
    const approvalAction = (nested as Record<string, unknown>).approval_action;
    if (approvalAction && typeof approvalAction === "object") {
      return approvalAction as ChatApprovalActionCardData;
    }
  }

  return null;
}

/**
 * Extended session carrying extra fields that the library type does not define
 * but our backend / window globals require.
 */
interface ExtendedSession extends IAgentScopeRuntimeWebUISession {
  /** Session identifier (channel:user_id format) */
  sessionId: string;
  /** User identifier */
  userId: string;
  /** Channel name */
  channel: string;
  /** Additional metadata */
  meta: Record<string, unknown>;
  /** Real backend UUID, used when id is overridden with a local timestamp. */
  realId?: string;
  /** Conversation status from backend. */
  status?: ChatStatus;
  /** ISO 8601 creation timestamp from backend. */
  createdAt?: string | null;
  /** Whether the backend is still generating a response for this session. */
  generating?: boolean;
  skipInitialResolve?: boolean;
}

interface SessionTitlePatchPayload {
  chat_id?: unknown;
  session_id?: unknown;
  session_title?: unknown;
}

interface NormalizedSessionTitlePatchPayload {
  chat_id?: string;
  session_id?: string;
  session_title: string;
}

function getSessionTitlePatchPayload(
  payload: unknown,
): NormalizedSessionTitlePatchPayload | undefined {
  if (!payload || typeof payload !== "object") {
    return undefined;
  }

  const titlePatch = payload as SessionTitlePatchPayload;
  const sessionTitle =
    typeof titlePatch.session_title === "string"
      ? titlePatch.session_title.trim()
      : "";
  if (!sessionTitle) {
    return undefined;
  }

  const sessionId =
    typeof titlePatch.session_id === "string"
      ? titlePatch.session_id.trim()
      : "";
  const chatId =
    typeof titlePatch.chat_id === "string" ? titlePatch.chat_id.trim() : "";
  if (!sessionId && !chatId) {
    return undefined;
  }

  return {
    chat_id: chatId || undefined,
    session_id: sessionId || undefined,
    session_title: sessionTitle,
  };
}

function sessionMatchesTitlePatch(
  session: ExtendedSession,
  payload: NormalizedSessionTitlePatchPayload,
): boolean {
  const candidateIds = new Set<string>(
    [payload.session_id, payload.chat_id].filter((id): id is string =>
      Boolean(id),
    ),
  );

  return [session.id, session.realId, session.sessionId].some(
    (id) => typeof id === "string" && candidateIds.has(id),
  );
}

// ---------------------------------------------------------------------------
// Message conversion helpers: backend flat messages → card-based UI format
// ---------------------------------------------------------------------------

function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

/** Extract plain text from a message's content array. */
const extractTextFromContent = (content: unknown): string => {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return String(content || "");
  return (content as ContentItem[])
    .filter((c) => c.type === "text")
    .map((c) => c.text || "")
    .filter(Boolean)
    .join("\n");
};

function resolveContentItemUrl(c: ContentItem): ContentItem {
  if (c.type === "image" && c.image_url) {
    return { ...c, image_url: toDisplayUrl(c.image_url as string) };
  }
  if (c.type === "audio" && c.data) {
    return { ...c, data: toDisplayUrl(c.data as string) };
  }
  if (c.type === "video" && c.video_url) {
    return { ...c, video_url: toDisplayUrl(c.video_url as string) };
  }
  if (c.type === "file" && (c.file_url || c.file_id)) {
    return {
      ...c,
      file_url: toDisplayUrl((c.file_url as string) || (c.file_id as string)),
      file_name: (c.filename as string) || (c.file_name as string) || "file",
    };
  }
  return c;
}

/** Map backend message content to request card content (text + image + file). */
function contentToRequestParts(
  content: unknown,
): Array<Record<string, unknown>> {
  if (typeof content === "string") {
    return [{ type: "text", text: content, status: "created" }];
  }
  if (!Array.isArray(content)) {
    return [{ type: "text", text: String(content || ""), status: "created" }];
  }
  const parts = (content as ContentItem[])
    .map(resolveContentItemUrl)
    .map((c) => ({ ...c, status: "created" }));

  if (parts.length === 0) {
    return [{ type: "text", text: "", status: "created" }];
  }

  return parts;
}
function normalizeOutputMessageContent(content: unknown): unknown {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return content;
  return (content as ContentItem[]).map(resolveContentItemUrl);
}

/**
 * Convert a backend message to a response output message.
 * Maps system + plugin_call_output → role "tool" and strips metadata.
 */
const toOutputMessage = (msg: Message): OutputMessage => ({
  ...msg,
  role:
    msg.type === TYPE_PLUGIN_CALL_OUTPUT && msg.role === "system"
      ? ROLE_TOOL
      : msg.role,
  metadata: msg.metadata ?? null,
});

function isStandaloneOutputMessage(message: Message | OutputMessage): boolean {
  return (
    Boolean(readStopAppendMetadata(message)) || isCronTaskOutputMessage(message)
  );
}

function getMessageMetadata(
  message: Message | OutputMessage,
): Record<string, unknown> | null {
  const metadata =
    message.metadata && typeof message.metadata === "object"
      ? (message.metadata as Record<string, unknown>)
      : null;
  if (!metadata) return null;
  const nested = metadata.metadata;
  return nested && typeof nested === "object"
    ? { ...metadata, ...(nested as Record<string, unknown>) }
    : metadata;
}

function getTaskRunId(message: Message | OutputMessage): string | null {
  const value = getMessageMetadata(message)?.task_run_id;
  return typeof value === "string" && value ? value : null;
}

function isCronTaskOutputMessage(message: Message | OutputMessage): boolean {
  return Boolean(getMessageMetadata(message)?.cron_task);
}

function getTaskRunMetadata(message: Message): TaskRunMetadata | null {
  const metadata = getMessageMetadata(message);
  if (!metadata) return null;

  const runId = metadata.task_run_id;
  const runIndex = metadata.task_run_index;
  const section = metadata.task_run_section;
  if (
    typeof runId !== "string" ||
    !runId ||
    typeof runIndex !== "number" ||
    typeof section !== "string"
  ) {
    return null;
  }

  return {
    runId,
    runIndex,
    section,
  };
}

function getMessageOrderScore(message: Message, index: number): number {
  return (
    resolveMessageTimestamp({
      timestamp: message.timestamp,
    }) ?? index
  );
}

function isNewerTaskRunCandidate(
  candidate: {
    latestScore: number;
    runIndex: number;
    latestOrder: number;
  },
  current: {
    latestScore: number;
    runIndex: number;
    latestOrder: number;
  } | null,
): boolean {
  if (!current) return true;
  if (candidate.latestScore !== current.latestScore) {
    return candidate.latestScore > current.latestScore;
  }
  if (candidate.runIndex !== current.runIndex) {
    return candidate.runIndex > current.runIndex;
  }
  return candidate.latestOrder > current.latestOrder;
}

function getLatestTaskSessionResultKey(messages: Message[]): string | null {
  let latestResultKey: string | null = null;
  let latestResult: {
    latestScore: number;
    runIndex: number;
    latestOrder: number;
  } | null = null;
  const taskRuns = new Map<
    string,
    { latestScore: number; runIndex: number; latestOrder: number }
  >();

  messages.forEach((message, index) => {
    const score = getMessageOrderScore(message, index);
    const runMetadata = getTaskRunMetadata(message);
    if (runMetadata) {
      const current = taskRuns.get(runMetadata.runId);
      const candidate = {
        latestScore: Math.max(current?.latestScore ?? score, score),
        runIndex: Math.max(
          current?.runIndex ?? runMetadata.runIndex,
          runMetadata.runIndex,
        ),
        latestOrder: Math.max(current?.latestOrder ?? index, index),
      };
      taskRuns.set(runMetadata.runId, candidate);
      return;
    }

    if (isCronTaskOutputMessage(message)) {
      const candidate = {
        latestScore: score,
        runIndex: index,
        latestOrder: index,
      };
      if (isNewerTaskRunCandidate(candidate, latestResult)) {
        latestResultKey = `cron:${index}`;
        latestResult = candidate;
      }
    }
  });

  taskRuns.forEach((candidate, runId) => {
    if (isNewerTaskRunCandidate(candidate, latestResult)) {
      latestResultKey = `run:${runId}`;
      latestResult = candidate;
    }
  });

  return latestResultKey;
}

function hasTaskSessionResultMessages(messages: Message[]): boolean {
  return messages.some(
    (message) =>
      getTaskRunMetadata(message) !== null || isCronTaskOutputMessage(message),
  );
}

function isTaskSessionResultMessage(message: Message): boolean {
  return (
    getTaskRunMetadata(message) !== null || isCronTaskOutputMessage(message)
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

function isPlanReviewDecision(value: unknown): value is PlanReviewDecision {
  return value === "revise" || value === "execute" || value === "exit_plan";
}

function extractSubmittedPlanReview(
  message: Message,
): SubmittedPlanReview | null {
  const record = asRecord(message);
  const metadata = asRecord(record?.metadata);
  const meta = asRecord(record?.meta);
  const bizParams = asRecord(record?.biz_params);
  const candidates = [
    record?.plan_interaction_response,
    metadata?.plan_interaction_response,
    meta?.plan_interaction_response,
    bizParams?.plan_interaction_response,
  ];

  for (const candidate of candidates) {
    const response = asRecord(candidate);
    if (!response) continue;
    const planId = response.plan_id;
    const decision = response.decision;
    if (
      typeof planId === "string" &&
      planId &&
      isPlanReviewDecision(decision)
    ) {
      const feedback = response.feedback;
      return {
        planId,
        decision,
        feedback: typeof feedback === "string" ? feedback : undefined,
      };
    }
  }

  return null;
}

function collectSubmittedPlanReviews(
  messages: Message[],
): Map<string, SubmittedPlanReview> {
  const submittedPlanReviews = new Map<string, SubmittedPlanReview>();
  messages.forEach((message) => {
    const submitted = extractSubmittedPlanReview(message);
    if (submitted) {
      submittedPlanReviews.set(submitted.planId, submitted);
    }
  });
  return submittedPlanReviews;
}

function markSubmittedPlanReviewCard(
  card: ChatPlanInteractionCardData | null,
  submittedPlanReviews: Map<string, SubmittedPlanReview>,
): ChatPlanInteractionCardData | null {
  const submitted =
    card?.card_type === "plan_review"
      ? submittedPlanReviews.get(card.plan_id)
      : undefined;
  if (!card || card.card_type !== "plan_review" || !submitted) {
    return card;
  }

  return {
    ...card,
    status: "submitted",
    submitted_decision: submitted.decision,
    feedback: submitted.feedback,
  } satisfies ChatPlanReviewCardData;
}

/** Build a user card (AgentScopeRuntimeRequestCard) from a user message. */
function buildUserCard(msg: Message): IAgentScopeRuntimeWebUIMessage {
  const contentParts = contentToRequestParts(msg.content);
  const timestamp = resolveMessageTimestamp({
    timestamp: msg.timestamp,
  });
  return {
    id: (msg.id as string) || generateId(),
    role: "user",
    cards: [
      {
        code: "AgentScopeRuntimeRequestCard",
        data: {
          input: [
            {
              role: "user",
              type: "message",
              content: contentParts,
            },
          ],
          headerMeta: {
            timestamp,
          },
        } as unknown as ChatRuntimeRequestCardData,
      },
    ],
  };
}

/**
 * Build an assistant response card (AgentScopeRuntimeResponseCard)
 * wrapping a group of consecutive non-user output messages.
 */
const buildResponseCard = (
  outputMessages: OutputMessage[],
  submittedPlanReviews: Map<string, SubmittedPlanReview>,
): IAgentScopeRuntimeWebUIMessage => {
  const timestamp = resolveGroupTimestamp(
    outputMessages.map((message) => ({
      timestamp: message.timestamp,
    })),
  );
  const createdAt = timestamp ?? Date.now();
  const maxSeq = outputMessages.reduce(
    (max, m) => Math.max(max, m.sequence_number || 0),
    0,
  );

  const normalizedMessages = outputMessages.map((msg) => ({
    ...msg,
    content: normalizeOutputMessageContent(msg.content),
  }));
  const stableId = stableResponseId(normalizedMessages);
  const appendMarker =
    normalizedMessages.length === 1
      ? readStopAppendMetadata(normalizedMessages[0])
      : null;

  const cardTraceId = normalizedMessages.reduce<string | null>((found, msg) => {
    if (found) return found;
    const metadata = msg.metadata;
    if (!metadata || typeof metadata !== "object") return null;
    const record = metadata as Record<string, unknown>;
    const tid = record.trace_id || record.traceId;
    return typeof tid === "string" && tid.trim() ? tid : null;
  }, null);

  const approvalAction =
    normalizedMessages.reduce<ChatApprovalActionCardData | null>(
      (found, message) => found ?? extractApprovalAction(message),
      null,
    );
  const planInteractionCard = markSubmittedPlanReviewCard(
    normalizedMessages.reduce<ChatPlanInteractionCardData | null>(
      (found, message) => found ?? extractPlanInteractionCard(message),
      null,
    ),
    submittedPlanReviews,
  );

  const cards: NonNullable<IAgentScopeRuntimeWebUIMessage["cards"]> = [
    {
      code: CARD_RESPONSE,
      data: {
        id: `response_${stableId}`,
        output: normalizedMessages,
        object: "response",
        status: "completed",
        created_at: createdAt,
        sequence_number: maxSeq + 1,
        error: null,
        completed_at: createdAt,
        usage: null,
        trace_id: cardTraceId || undefined,
        headerMeta: {
          timestamp,
        },
        planReviewCard:
          planInteractionCard?.card_type === "plan_review"
            ? planInteractionCard
            : undefined,
      } as unknown as ChatRuntimeResponseCardData,
    },
  ];

  if (approvalAction) {
    cards.push({
      code: CARD_APPROVAL_ACTION,
      data: approvalAction,
    });
  }

  if (planInteractionCard) {
    cards.push({
      code: CARD_PLAN_INTERACTION,
      data: planInteractionCard,
    });
  }

  if (!appendMarker) {
    cards.push({
      code: CARD_RESPONSE_FEEDBACK,
      data: cards[0].data,
    });
  }

  return {
    id: appendMarker
      ? stopAppendBubbleId(appendMarker)
      : `history-response-${stableId}`,
    role: ROLE_ASSISTANT,
    cards,
    msgStatus: "finished",
  };
};

function stableResponseId(messages: OutputMessage[]): string {
  const appendMarker =
    messages.length === 1 ? readStopAppendMetadata(messages[0]) : null;
  if (appendMarker) return appendMarker.message_id;
  const persistedIds = messages
    .map((message) => message.id)
    .filter((id): id is string => typeof id === "string" && id.length > 0);
  return persistedIds.length > 0 ? persistedIds.join("_") : generateId();
}

/**
 * Convert flat backend messages into the card-based format expected by
 * the @agentscope-ai/chat component.
 *
 * - User messages → AgentScopeRuntimeRequestCard
 * - Consecutive non-user messages (assistant / system / tool) → grouped
 *   into a single AgentScopeRuntimeResponseCard with all output messages.
 */
export const convertMessages = (
  rawMessages: Message[],
): IAgentScopeRuntimeWebUIMessage[] => {
  const messages = normalizeHistoryMessages(rawMessages);
  const result: IAgentScopeRuntimeWebUIMessage[] = [];
  const submittedPlanReviews = collectSubmittedPlanReviews(messages);
  let i = 0;

  while (i < messages.length) {
    if (messages[i].role === ROLE_USER) {
      result.push(buildUserCard(messages[i++]));
    } else {
      if (isStandaloneOutputMessage(messages[i])) {
        const card = buildResponseCard(
          [toOutputMessage(messages[i++])],
          submittedPlanReviews,
        );
        const existingIndex = result.findIndex(
          (message) => message.id === card.id,
        );
        if (existingIndex >= 0) result[existingIndex] = card;
        else result.push(card);
        continue;
      }

      const outputMsgs: OutputMessage[] = [];
      const firstTaskRunId = getTaskRunId(messages[i]);
      let turnId = getMessageMetadata(messages[i])?.turn_id;
      while (i < messages.length && messages[i].role !== ROLE_USER) {
        if (isStandaloneOutputMessage(messages[i])) {
          break;
        }
        if (
          outputMsgs.length > 0 &&
          getTaskRunId(messages[i]) !== firstTaskRunId
        ) {
          break;
        }
        const nextTurnId = getMessageMetadata(messages[i])?.turn_id;
        if (
          typeof turnId === "string" &&
          turnId &&
          typeof nextTurnId === "string" &&
          nextTurnId &&
          turnId !== nextTurnId
        ) {
          break;
        }
        if (typeof nextTurnId === "string" && nextTurnId) turnId = nextTurnId;
        outputMsgs.push(toOutputMessage(messages[i++]));
      }
      if (outputMsgs.length) {
        result.push(buildResponseCard(outputMsgs, submittedPlanReviews));
      }
    }
  }

  return result;
};

function compactionBoundaryCard(
  boundary: ChatCompactionBoundary,
): IAgentScopeRuntimeWebUIMessage {
  return {
    id: `conversation-compaction-${boundary.id}`,
    role: ROLE_ASSISTANT,
    msgStatus: "finished",
    cards: [{ code: CARD_COMPACTION_BOUNDARY, data: boundary }],
  };
}

function archiveSourceMessageId(
  message: Message | undefined,
): string | undefined {
  if (!message) return undefined;
  const metadata = message.metadata;
  if (metadata && typeof metadata === "object") {
    const originalId = (metadata as Record<string, unknown>).original_id;
    if (typeof originalId === "string" && originalId) {
      return originalId;
    }
  }
  return typeof message.id === "string" ? message.id : undefined;
}

function withArchiveBoundaries(
  messages: IAgentScopeRuntimeWebUIMessage[],
  boundaries: ChatCompactionBoundary[] | undefined,
): IAgentScopeRuntimeWebUIMessage[] {
  if (!boundaries?.length) return messages;
  return [...boundaries.map(compactionBoundaryCard), ...messages];
}

/** Convert one archived page while keeping each divider after its batch. */
export function convertArchivedPage(
  rawMessages: Message[],
  boundaries: ChatCompactionBoundary[],
): IAgentScopeRuntimeWebUIMessage[] {
  const messages = normalizeHistoryMessages(rawMessages);
  const boundariesByLastMessageId = new Map<string, ChatCompactionBoundary[]>();
  boundaries.forEach((boundary) => {
    const current = boundariesByLastMessageId.get(boundary.last_message_id);
    boundariesByLastMessageId.set(boundary.last_message_id, [
      ...(current || []),
      boundary,
    ]);
  });

  const result: IAgentScopeRuntimeWebUIMessage[] = [];
  let segmentStart = 0;
  messages.forEach((message, index) => {
    const sourceMessageId = archiveSourceMessageId(message);
    const nextSourceMessageId = archiveSourceMessageId(messages[index + 1]);
    const batchBoundaries =
      sourceMessageId && sourceMessageId !== nextSourceMessageId
        ? boundariesByLastMessageId.get(sourceMessageId)
        : undefined;
    if (!batchBoundaries?.length) return;
    result.push(...convertMessages(messages.slice(segmentStart, index + 1)));
    result.push(...batchBoundaries.map(compactionBoundaryCard));
    segmentStart = index + 1;
  });
  result.push(...convertMessages(messages.slice(segmentStart)));
  return mergeHistoryResponses(
    result.map((message) => ({ ...message, history: true })),
  );
}

function buildTaskRunCard(
  runId: string,
  runIndex: number,
  runMessages: Message[],
  taskName?: string,
  collapsedByDefault = false,
): IAgentScopeRuntimeWebUIMessage[] {
  const finalMessages = runMessages.filter((message) => {
    const metadata = getTaskRunMetadata(message);
    return metadata?.section === TASK_RUN_SECTION_FINAL;
  });
  const stepMessages = runMessages.filter((message) => {
    const metadata = getTaskRunMetadata(message);
    return metadata?.section === TASK_RUN_SECTION_STEP;
  });

  if (finalMessages.length === 0) {
    return convertMessages(runMessages);
  }

  return [
    {
      id: `task-run-${runId}-${runIndex}`,
      role: ROLE_ASSISTANT,
      msgStatus: "finished",
      cards: [
        {
          code: CARD_TASK_RUN,
          data: {
            runId,
            runIndex,
            taskName,
            collapsedByDefault,
            finalMessages: convertMessages(finalMessages),
            stepMessages: convertMessages(stepMessages),
            headerMeta: {
              timestamp: resolveGroupTimestamp(
                runMessages.map((message) => ({
                  timestamp: message.timestamp,
                })),
              ),
            },
          } as ChatTaskRunGroupCardData,
        },
      ],
    },
  ];
}

function buildStandaloneTaskRunCard(
  message: Message,
  messageIndex: number,
  taskName: string | undefined,
  collapsedByDefault: boolean,
): IAgentScopeRuntimeWebUIMessage[] {
  const runId = `cron-${message.id || messageIndex}`;
  return [
    {
      id: `task-run-${runId}-${messageIndex}`,
      role: ROLE_ASSISTANT,
      msgStatus: "finished",
      cards: [
        {
          code: CARD_TASK_RUN,
          data: {
            runId,
            runIndex: messageIndex,
            taskName,
            collapsedByDefault,
            finalMessages: convertMessages([message]),
            stepMessages: [],
            headerMeta: {
              timestamp: resolveMessageTimestamp({
                timestamp: message.timestamp,
              }),
            },
          } as ChatTaskRunGroupCardData,
        },
      ],
    },
  ];
}

function convertMessagesForSession(
  messages: Message[],
  sessionMeta?: Record<string, unknown>,
  sessionName?: string,
): IAgentScopeRuntimeWebUIMessage[] {
  const shouldFilterTaskResults =
    sessionMeta?.session_kind === TASK_SESSION_KIND ||
    hasTaskSessionResultMessages(messages);

  if (!shouldFilterTaskResults) {
    return convertMessages(messages);
  }

  const latestResultKey = getLatestTaskSessionResultKey(messages);

  if (!latestResultKey) {
    return convertMessages(messages);
  }

  const result: IAgentScopeRuntimeWebUIMessage[] = [];
  let index = 0;

  while (index < messages.length) {
    if (isCronTaskOutputMessage(messages[index])) {
      result.push(
        ...buildStandaloneTaskRunCard(
          messages[index],
          index,
          sessionName,
          `cron:${index}` !== latestResultKey,
        ),
      );
      index += 1;
      continue;
    }

    const metadata = getTaskRunMetadata(messages[index]);
    if (!metadata) {
      const gap: Message[] = [];
      while (
        index < messages.length &&
        !isTaskSessionResultMessage(messages[index])
      ) {
        gap.push(messages[index]);
        index += 1;
      }
      result.push(...convertMessages(gap));
      continue;
    }

    const runMessages: Message[] = [];
    while (index < messages.length) {
      const candidateMetadata = getTaskRunMetadata(messages[index]);
      if (!candidateMetadata || candidateMetadata.runId !== metadata.runId) {
        break;
      }
      runMessages.push(messages[index]);
      index += 1;
    }
    result.push(
      ...buildTaskRunCard(
        metadata.runId,
        metadata.runIndex,
        runMessages,
        sessionName,
        `run:${metadata.runId}` !== latestResultKey,
      ),
    );
  }

  return result;
}

const chatSpecToSession = (chat: ChatSpec): ExtendedSession =>
  ({
    id: chat.id,
    name: chat.name || DEFAULT_SESSION_NAME,
    sessionId: chat.session_id,
    userId: chat.user_id,
    channel: chat.channel,
    messages: [],
    meta: chat.meta || {},
    status: chat.status ?? "idle",
    createdAt: chat.created_at ?? null,
  }) as ExtendedSession;

/** Returns true when id is a pure numeric local timestamp (not a backend UUID). */
const isLocalTimestamp = (id: string): boolean => /^\d+$/.test(id);

/** Detect if backend is still generating content for this chat. */
const isGenerating = (chatHistory: ChatHistory): boolean => {
  if (chatHistory.status === "running") return true;
  if (chatHistory.status === "stopping") return true;
  if (chatHistory.status === "idle") return false;
  const msgs = chatHistory.messages || [];
  if (msgs.length === 0) return false;
  const last = msgs[msgs.length - 1];
  return last.role === ROLE_USER;
};

const mergeGeneratingState = (
  backendStatus?: string,
  backendGenerating?: boolean,
  localGenerating?: boolean,
): boolean => {
  if (backendStatus === "running") return true;
  if (backendStatus === "stopping") return true;
  if (backendStatus === "idle") return false;
  if (typeof backendGenerating === "boolean") return backendGenerating;
  return Boolean(localGenerating);
};

const hasGeneratedSessionTitle = (session?: ExtendedSession): boolean => {
  return Boolean(
    session?.name && session.meta?.[SESSION_TITLE_GENERATED_META_KEY] === true,
  );
};

const mergeSessionMeta = (
  backendSession: ExtendedSession,
  localSession?: ExtendedSession,
): Record<string, unknown> => {
  const backendMeta = backendSession.meta || {};
  const localMeta = localSession?.meta || {};
  const merged = {
    ...backendMeta,
    ...localMeta,
  };

  if (backendMeta[SESSION_TITLE_GENERATED_META_KEY] === true) {
    merged[SESSION_TITLE_GENERATED_META_KEY] = true;
  }

  return merged;
};

const resolveMergedSessionName = (
  backendSession: ExtendedSession,
  localSession?: ExtendedSession,
): string => {
  if (hasGeneratedSessionTitle(backendSession)) {
    return backendSession.name;
  }

  return localSession?.name || backendSession.name || DEFAULT_SESSION_NAME;
};

/**
 * Resolve and persist the real backend UUID for a local timestamp session.
 * Stores the real UUID as realId while keeping the timestamp as id, so the
 * library's internal currentSessionId (timestamp) remains valid.
 * Returns the resolved real UUID, or null if not found.
 */
const mergeResolvedSession = (
  resolvedSession: ExtendedSession,
  localSession?: ExtendedSession,
  tempSessionId?: string,
): ExtendedSession => {
  const realId = resolvedSession.realId || resolvedSession.id;

  return {
    ...resolvedSession,
    id: localSession?.id || tempSessionId || resolvedSession.id,
    realId,
    sessionId: localSession?.sessionId || resolvedSession.sessionId,
    name: resolveMergedSessionName(resolvedSession, localSession),
    userId: localSession?.userId || resolvedSession.userId,
    channel: localSession?.channel || resolvedSession.channel,
    meta: mergeSessionMeta(resolvedSession, localSession),
    createdAt: localSession?.createdAt || resolvedSession.createdAt,
    messages:
      resolvedSession.messages?.length > 0
        ? resolvedSession.messages
        : localSession?.messages || [],
    generating: mergeGeneratingState(
      resolvedSession.status,
      resolvedSession.generating,
      localSession?.generating,
    ),
  } as ExtendedSession;
};

const resolveRealId = (
  sessionList: IAgentScopeRuntimeWebUISession[],
  tempSessionId: string,
): { list: IAgentScopeRuntimeWebUISession[]; realId: string | null } => {
  const localSession = sessionList.find((s) => s.id === tempSessionId) as
    | ExtendedSession
    | undefined;
  const realSession = sessionList.find((s) => {
    const extendedSession = s as ExtendedSession;
    return (
      extendedSession.sessionId === tempSessionId &&
      (extendedSession.id !== tempSessionId || Boolean(extendedSession.realId))
    );
  });
  if (!realSession) return { list: sessionList, realId: null };

  const realUUID = (realSession as ExtendedSession).realId || realSession.id;
  const resolvedSession = mergeResolvedSession(
    realSession as ExtendedSession,
    localSession,
  );
  return {
    list: [
      resolvedSession,
      ...sessionList.filter((s) => s !== realSession && s !== localSession),
    ],
    realId: realUUID,
  };
};

const isPendingLocalSession = (
  session: IAgentScopeRuntimeWebUISession,
): session is ExtendedSession => {
  const extendedSession = session as ExtendedSession;
  return isLocalTimestamp(extendedSession.id) && !extendedSession.realId;
};

const mergePendingSession = (
  sessionList: IAgentScopeRuntimeWebUISession[],
  pendingSession: ExtendedSession,
): IAgentScopeRuntimeWebUISession[] => {
  return [
    pendingSession,
    ...sessionList.filter((session) => session.id !== pendingSession.id),
  ];
};

const mergePendingSessions = (
  sessionList: IAgentScopeRuntimeWebUISession[],
  pendingSessions: ExtendedSession[],
): IAgentScopeRuntimeWebUISession[] =>
  [...pendingSessions]
    .reverse()
    .reduce(
      (list, pendingSession) => mergePendingSession(list, pendingSession),
      sessionList,
    );

const getSessionIdentityKeys = (
  session: IAgentScopeRuntimeWebUISession,
): string[] => {
  const extendedSession = session as ExtendedSession;
  return [extendedSession.id, extendedSession.realId].filter(
    (value): value is string => Boolean(value),
  );
};

const appendUniqueSessions = (
  current: IAgentScopeRuntimeWebUISession[],
  incoming: IAgentScopeRuntimeWebUISession[],
): IAgentScopeRuntimeWebUISession[] => {
  const merged = [...current];
  incoming.forEach((session) => {
    const keys = getSessionIdentityKeys(session);
    const existingIndex = merged.findIndex((existing) =>
      getSessionIdentityKeys(existing).some((key) => keys.includes(key)),
    );
    if (existingIndex === -1) {
      merged.push(session);
      return;
    }

    const existing = merged[existingIndex] as ExtendedSession;
    const next = session as ExtendedSession;
    if (!existing.realId && next.realId) {
      merged[existingIndex] = session;
    }
  });
  return merged;
};

// ---------------------------------------------------------------------------
// Per-session user message persistence (survives page refresh)
// ---------------------------------------------------------------------------

const STORAGE_PREFIX = "copaw_pending_user_msg_";

function savePendingUserMessage(sessionId: string, text: string): void {
  try {
    sessionStorage.setItem(`${STORAGE_PREFIX}${sessionId}`, text);
  } catch {
    /* quota exceeded – ignore */
  }
}

function loadPendingUserMessage(sessionId: string): string {
  try {
    return sessionStorage.getItem(`${STORAGE_PREFIX}${sessionId}`) || "";
  } catch {
    return "";
  }
}

function loadPendingUserMessageFromCandidates(sessionIds: string[]): string {
  for (const sessionId of sessionIds) {
    const text = loadPendingUserMessage(sessionId);
    if (text) {
      return text;
    }
  }

  return "";
}

function clearPendingUserMessage(sessionId: string): void {
  try {
    sessionStorage.removeItem(`${STORAGE_PREFIX}${sessionId}`);
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// SessionApi
// ---------------------------------------------------------------------------

export class SessionApi implements IAgentScopeRuntimeWebUISessionAPI {
  private taskLookupScope: string | null = null;
  private taskLookupGeneration = 0;
  private sessionList: IAgentScopeRuntimeWebUISession[] = [];
  private sessionPage = 0;
  private hasMoreSessionPages = false;
  private nextSessionCursor: string | null | undefined = null;
  private sessionTotal = 0;
  private intendedSessionId: string | null = null;

  /**
   * When set, getSessionList will move the matching session to the front on the first call,
   * so the library's useMount auto-selects it instead of always defaulting to sessions[0].
   * Cleared after first use.
   */
  preferredChatId: string | null = null;

  /**
   * Cache the latest user message for a chat so it can be patched into
   * history during reconnect (the backend only persists it after generation
   * completes). Persisted to sessionStorage so it survives page refresh.
   */
  setLastUserMessage(sessionId: string, text: string): void {
    if (!sessionId || !text) return;
    savePendingUserMessage(sessionId, text);
  }

  /**
   * Deduplicates concurrent getSessionList calls so that two parallel
   * invocations share one network request and write sessionList only once,
   * preserving any realId mappings that were already resolved.
   */
  private sessionListRequest: Promise<IAgentScopeRuntimeWebUISession[]> | null =
    null;
  private sessionPageRequest: Promise<IAgentScopeRuntimeWebUISession[]> | null =
    null;

  /**
   * Deduplicates concurrent getSession calls for the same sessionId.
   * Key: sessionId, Value: in-flight promise for getSession.
   */
  private sessionRequests: Map<
    string,
    Promise<IAgentScopeRuntimeWebUISession>
  > = new Map();

  resetForIdentityChange(): void {
    this.taskLookupScope = null;
    this.taskLookupGeneration += 1;
    this.sessionList = [];
    this.sessionPage = 0;
    this.hasMoreSessionPages = false;
    this.nextSessionCursor = null;
    this.sessionTotal = 0;
    this.sessionListRequest = null;
    this.sessionPageRequest = null;
    this.sessionRequests.clear();
    this.intendedSessionId = null;
    this.preferredChatId = null;
    this.lastSelectedSessionId = null;
  }

  /**
   * Called when a temporary timestamp session id is resolved to a real backend
   * UUID. Consumers (e.g. Chat/index.tsx) can register here to update the URL.
   */
  onSessionIdResolved: ((tempId: string, realId: string) => void) | null = null;

  /**
   * Called after a session is removed. Consumers can register here to clear
   * the session id from the URL.
   */
  onSessionRemoved: ((removedId: string) => void) | null = null;

  /**
   * Called when a session is selected from the session list.
   * Consumers can register here to update the URL when switching sessions.
   */
  onSessionSelected:
    | ((sessionId: string | null | undefined, realId: string | null) => void)
    | null = null;

  /**
   * Called when a new session is created.
   * Consumers can register here to update the URL with the new session id.
   */
  onSessionCreated: ((sessionId: string) => void) | null = null;

  /**
   * When reconnecting to a running conversation, the backend history may not
   * include the latest user message (it's only persisted after generation
   * completes). If generating, look up the cached text from sessionStorage
   * and patch it into the message list.
   *
   * When not generating the conversation is done — clear the cached entry.
   */
  private patchLastUserMessage(
    messages: IAgentScopeRuntimeWebUIMessage[],
    generating: boolean,
    backendSessionId: string,
    fallbackSessionIds: string[] = [],
  ): void {
    const candidateSessionIds = Array.from(
      new Set([backendSessionId, ...fallbackSessionIds].filter(Boolean)),
    );

    if (!generating) {
      candidateSessionIds.forEach(clearPendingUserMessage);
      return;
    }

    const cachedText =
      loadPendingUserMessageFromCandidates(candidateSessionIds);
    if (!cachedText) return;

    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.role === ROLE_USER) {
      const text = extractTextFromContent(
        lastMsg?.cards?.[0]?.data?.input?.[0]?.content,
      );
      if (!text) {
        lastMsg.cards = buildUserCard({
          content: [{ type: "text", text: cachedText }],
          role: ROLE_USER,
        } as Message).cards;
      }
    } else {
      messages.push(
        buildUserCard({
          content: [{ type: "text", text: cachedText }],
          role: ROLE_USER,
        } as Message),
      );
    }
  }

  private createEmptySession(sessionId: string): ExtendedSession {
    window.currentSessionId = sessionId;
    // ==================== userId 统一整改 (Kun He) ====================
    // 使用 getUserId() 获取用户 ID，优先级：iframe > window > default
    window.currentUserId = getUserId();
    window.currentChannel = getChannel();
    // ==================== userId 统一整改结束 ====================
    return {
      id: sessionId,
      name: DEFAULT_SESSION_NAME,
      sessionId,
      userId: getUserId(),
      channel: getChannel(),
      messages: [],
      meta: {},
    } as ExtendedSession;
  }

  private updateWindowVariables(session: ExtendedSession): void {
    window.currentSessionId = session.sessionId || "";
    // ==================== userId 统一整改 (Kun He) ====================
    // 使用 getUserId() 获取用户 ID，传入 session.userId 作为候选值
    window.currentUserId = getUserIdWithoutWindow(session.userId);
    window.currentChannel = getChannelWithoutWindow(session.channel);
    // ==================== userId 统一整改结束 ====================
  }

  private getLocalSession(sessionId: string): IAgentScopeRuntimeWebUISession {
    const local = this.sessionList.find((s) => s.id === sessionId);
    if (local) {
      this.updateWindowVariables(local as ExtendedSession);
      return local;
    }
    return this.createEmptySession(sessionId);
  }

  private findSessionByIdentity(
    sessionId: string,
  ): ExtendedSession | undefined {
    return this.sessionList.find((session) => {
      const extendedSession = session as ExtendedSession;
      return (
        extendedSession.id === sessionId || extendedSession.realId === sessionId
      );
    }) as ExtendedSession | undefined;
  }

  private patchSessionLocally(
    session: Partial<IAgentScopeRuntimeWebUISession>,
  ): void {
    if (!session.id) {
      return;
    }

    const index = this.sessionList.findIndex((item) => {
      const extendedSession = item as ExtendedSession;
      return (
        extendedSession.id === session.id ||
        extendedSession.realId === session.id ||
        extendedSession.sessionId === session.id
      );
    });

    if (index === -1) {
      this.sessionList = mergePendingSession(
        this.sessionList,
        session as ExtendedSession,
      );
      return;
    }

    const current = this.sessionList[index] as ExtendedSession;
    this.sessionList[index] = {
      ...current,
      ...session,
      id: current.id,
      realId: current.realId,
      sessionId: current.sessionId,
    } as ExtendedSession;
  }

  patchSessionTitle(payload: unknown): IAgentScopeRuntimeWebUISession[] {
    const titlePatch = getSessionTitlePatchPayload(payload);
    if (!titlePatch) {
      return [...this.sessionList];
    }

    this.sessionList = this.sessionList.map((session) => {
      const extendedSession = session as ExtendedSession;
      if (!sessionMatchesTitlePatch(extendedSession, titlePatch)) {
        return session;
      }
      if (extendedSession.meta?.session_kind === TASK_SESSION_KIND) {
        return session;
      }

      const generatedMeta =
        extendedSession.meta?.[SESSION_TITLE_GENERATED_META_KEY] === true;
      if (extendedSession.name === titlePatch.session_title && generatedMeta) {
        return session;
      }

      return {
        ...extendedSession,
        name: titlePatch.session_title,
        meta: {
          ...extendedSession.meta,
          [SESSION_TITLE_GENERATED_META_KEY]: true,
        },
      } as ExtendedSession;
    });

    return [...this.sessionList];
  }

  private getPendingSessions(): ExtendedSession[] {
    return this.sessionList.filter(isPendingLocalSession) as ExtendedSession[];
  }

  private getLatestPendingSession(): ExtendedSession | null {
    return this.getPendingSessions()[0] ?? null;
  }

  private isActivePendingSession(
    pendingSessionId: string,
    logicalSessionId: string = pendingSessionId,
  ): boolean {
    const currentSessionId = window.currentSessionId;
    if (!currentSessionId) {
      return this.getLatestPendingSession()?.id === pendingSessionId;
    }

    return (
      currentSessionId === pendingSessionId ||
      currentSessionId === logicalSessionId
    );
  }

  private notifyResolvedSessionIfActive(
    pendingSessionId: string,
    realId: string,
    logicalSessionId: string = pendingSessionId,
  ): void {
    if (this.isActivePendingSession(pendingSessionId, logicalSessionId)) {
      this.onSessionIdResolved?.(pendingSessionId, realId);
    }
  }

  getLogicalSessionId(sessionId: string): string {
    if (!sessionId) {
      return "";
    }

    const session = this.sessionList.find((item) => {
      const extended = item as ExtendedSession;
      return (
        extended.id === sessionId ||
        extended.realId === sessionId ||
        extended.sessionId === sessionId
      );
    }) as ExtendedSession | undefined;
    return session?.sessionId || sessionId;
  }

  /**
   * Returns the real backend UUID for a session identified by id (which may be
   * a local timestamp). Returns null when not yet resolved or not found.
   */
  getRealIdForSession(sessionId: string): string | null {
    return (
      this.findSessionByIdentity(sessionId)?.realId ??
      getResolvedChatId(sessionId)
    );
  }

  getChatIdForSession(sessionId: string): string | null {
    if (!sessionId) {
      return null;
    }

    const session = this.findSessionByIdentity(sessionId);
    if (session?.realId) {
      return session.realId;
    }

    if (session && !isLocalTimestamp(session.id)) {
      return session.id;
    }

    if (isLocalTimestamp(sessionId)) {
      const resolvedChatId = getResolvedChatId(sessionId);
      if (resolvedChatId) {
        return resolvedChatId;
      }

      const matches = this.sessionList.filter(
        (session) => (session as ExtendedSession).sessionId === sessionId,
      ) as ExtendedSession[];
      if (matches.length === 1) {
        const matchedSession = matches[0];
        if (matchedSession.realId) {
          return matchedSession.realId;
        }
        return isLocalTimestamp(matchedSession.id) ? null : matchedSession.id;
      }

      return null;
    }

    const matchesLogicalSessionId = this.sessionList.some(
      (session) => (session as ExtendedSession).sessionId === sessionId,
    );
    if (matchesLogicalSessionId) {
      return null;
    }

    return sessionId;
  }

  /**
   * 获取当前的临时会话ID（用于发送消息时作为 session_id）
   */
  getPendingSessionId(): string | null {
    return this.getLatestPendingSession()?.id ?? null;
  }

  /**
   * 清除临时会话（消息发送完成后调用）
   */
  clearPendingSession(): void {
    return;
  }

  /**
   * 消息发送完成后，创建真实会话记录并更新URL
   * @param realId 后端返回的真实UUID
   * @param name 会话名称（可选，默认使用临时会话的名称）
   */
  async createSessionFromPending(realId: string, name?: string): Promise<void> {
    const pendingSession = this.getLatestPendingSession();
    if (!pendingSession) return;

    const pendingSessionId = pendingSession.id;
    const session: ExtendedSession = {
      id: realId,
      sessionId: pendingSession.sessionId,
      userId: pendingSession.userId,
      channel: pendingSession.channel,
      name: name || pendingSession.name || DEFAULT_SESSION_NAME,
      messages: [],
      meta: pendingSession.meta || {},
      createdAt: pendingSession.createdAt,
    };

    // 添加到历史列表
    this.sessionList = [
      session,
      ...this.sessionList.filter((item) => item.id !== pendingSessionId),
    ];

    // 触发回调更新URL
    rememberResolvedChatId(pendingSession.id, realId);
    this.notifyResolvedSessionIfActive(
      pendingSession.id,
      realId,
      pendingSession.sessionId,
    );

    // 更新 window 变量
    this.updateWindowVariables(session);
  }

  /** 两份任务清单必须与同一次会话请求使用同一身份，旧响应不可写回新工作区。 */
  private taskListScope() {
    const context = currentCronContext();
    const accessKey = cronAccessKey(context);
    if (this.taskLookupScope && this.taskLookupScope !== accessKey)
      this.resetForIdentityChange();
    this.taskLookupScope = accessKey;
    return { context, accessKey, generation: this.taskLookupGeneration };
  }

  private taskListScopeMatches(accessKey: string, generation: number): boolean {
    return (
      this.ownsTaskListRequest(accessKey, generation) &&
      cronAccessKey() === accessKey
    );
  }

  private ownsTaskListRequest(accessKey: string, generation: number): boolean {
    return (
      this.taskLookupScope === accessKey &&
      generation === this.taskLookupGeneration
    );
  }

  private taskListCurrentSnapshot(): IAgentScopeRuntimeWebUISession[] {
    return this.taskLookupScope === cronAccessKey()
      ? [...this.sessionList]
      : [];
  }

  async getSessionList() {
    const { context, accessKey, generation } = this.taskListScope();
    if (this.sessionListRequest) return this.sessionListRequest;

    this.sessionListRequest = (async () => {
      try {
        const allowPreferredSelection = this.sessionList.length === 0;
        const pendingSessions = this.getPendingSessions();

        const [chatPage, jobsResult] = await Promise.all([
          api.listChatsPage({
            page_size: getSessionPageSize(),
            cursor: null,
          }),
          listTaskSources(context),
        ]);
        const chats = chatPage.items;
        if (!this.taskListScopeMatches(accessKey, generation))
          return this.taskListCurrentSnapshot();
        const activeTaskJobIds = taskPresence(jobsResult);
        const newList = chats
          .filter((c) => c.id && c.id !== "undefined" && c.id !== "null")
          .map(chatSpecToSession);
        const filteredList = filterStaleTaskSessions(newList, activeTaskJobIds);

        const resolvedPendingSessionIds = new Set<string>();
        pendingSessions.forEach((pendingSession) => {
          const matchedIndex = filteredList.findIndex(
            (session) =>
              (session as ExtendedSession).sessionId ===
              pendingSession.sessionId,
          );
          if (matchedIndex === -1) {
            return;
          }

          const matchedBackendSession = filteredList[
            matchedIndex
          ] as ExtendedSession;
          const realId = matchedBackendSession.id;
          filteredList[matchedIndex] = mergeResolvedSession(
            matchedBackendSession,
            pendingSession,
            pendingSession.id,
          );
          resolvedPendingSessionIds.add(pendingSession.id);
          rememberResolvedChatId(pendingSession.id, realId);
          this.notifyResolvedSessionIfActive(
            pendingSession.id,
            realId,
            pendingSession.sessionId,
          );

          if (
            window.currentSessionId === pendingSession.id ||
            window.currentSessionId === pendingSession.sessionId
          ) {
            window.currentSessionId =
              matchedBackendSession.sessionId || pendingSession.id;
          }
        });

        const previousResolvedSessions = this.sessionList.filter((session) => {
          const extendedSession = session as ExtendedSession;
          return (
            isLocalTimestamp(extendedSession.id) &&
            Boolean(extendedSession.realId)
          );
        }) as ExtendedSession[];

        previousResolvedSessions.forEach((localResolvedSession) => {
          const matchedIndex = filteredList.findIndex(
            (session) => session.id === localResolvedSession.realId,
          );
          if (matchedIndex > -1) {
            const backendSession = filteredList[
              matchedIndex
            ] as ExtendedSession;
            filteredList[matchedIndex] = {
              ...backendSession,
              id: localResolvedSession.id,
              realId: localResolvedSession.realId,
              sessionId:
                localResolvedSession.sessionId || backendSession.sessionId,
              name: resolveMergedSessionName(
                backendSession,
                localResolvedSession,
              ),
              userId: localResolvedSession.userId || backendSession.userId,
              channel: localResolvedSession.channel || backendSession.channel,
              meta: mergeSessionMeta(backendSession, localResolvedSession),
              createdAt:
                localResolvedSession.createdAt || backendSession.createdAt,
              messages:
                backendSession.messages?.length > 0
                  ? backendSession.messages
                  : localResolvedSession.messages || [],
              generating: mergeGeneratingState(
                backendSession.status,
                backendSession.generating,
                localResolvedSession.generating,
              ),
            } as ExtendedSession;
          }
        });

        const pageIdentityKeys = new Set(
          filteredList.flatMap(getSessionIdentityKeys),
        );
        const preservedOlderSessions = filterStaleTaskSessions(
          this.sessionList,
          activeTaskJobIds,
        ).filter(
          (session) =>
            !isPendingLocalSession(session) &&
            !getSessionIdentityKeys(session).some((key) =>
              pageIdentityKeys.has(key),
            ),
        );

        // 合并第一页、已加载旧页与本地待落库会话。
        this.sessionList = mergePendingSessions(
          appendUniqueSessions(filteredList, preservedOlderSessions),
          pendingSessions.filter(
            (pendingSession) =>
              !resolvedPendingSessionIds.has(pendingSession.id),
          ),
        );

        this.sessionList = applyPreferredSessionSelection({
          sessions: this.sessionList,
          preferredChatId: this.preferredChatId,
          allowReorder: allowPreferredSelection,
        });
        this.preferredChatId = null;
        this.sessionPage = chatPage.page;
        this.hasMoreSessionPages = chatPage.has_more;
        this.nextSessionCursor = chatPage.next_cursor;
        this.sessionTotal = chatPage.total;

        return [...this.sessionList];
      } finally {
        if (this.ownsTaskListRequest(accessKey, generation))
          this.sessionListRequest = null;
      }
    })();

    return this.sessionListRequest;
  }

  hasMoreSessions(): boolean {
    return this.hasMoreSessionPages;
  }

  getSessionTotal(): number {
    return this.sessionTotal;
  }

  loadMoreSessions(): Promise<IAgentScopeRuntimeWebUISession[]> {
    const { context, accessKey, generation } = this.taskListScope();
    if (this.sessionPageRequest) return this.sessionPageRequest;
    if (!this.hasMoreSessionPages) {
      return Promise.resolve([...this.sessionList]);
    }

    const nextPage = this.sessionPage + 1;
    const pageSize = getSessionPageSize();
    const paginationParams = this.nextSessionCursor
      ? { page_size: pageSize, cursor: this.nextSessionCursor }
      : { page: nextPage, page_size: pageSize };
    this.sessionPageRequest = (async () => {
      try {
        const [chatPage, jobsResult] = await Promise.all([
          api.listChatsPage(paginationParams),
          listTaskSources(context),
        ]);
        if (!this.taskListScopeMatches(accessKey, generation))
          return this.taskListCurrentSnapshot();
        const activeTaskJobIds = taskPresence(jobsResult);
        const pendingSessions = this.getPendingSessions();
        const nextSessions = filterStaleTaskSessions(
          chatPage.items
            .filter(
              (chat) =>
                chat.id && chat.id !== "undefined" && chat.id !== "null",
            )
            .map(chatSpecToSession),
          activeTaskJobIds,
        ).map((backendSession) => {
          const pendingSession = pendingSessions.find(
            (session) => session.sessionId === backendSession.sessionId,
          );
          if (pendingSession) {
            const resolved = mergeResolvedSession(
              backendSession as ExtendedSession,
              pendingSession,
              pendingSession.id,
            );
            const realId =
              (backendSession as ExtendedSession).realId || backendSession.id;
            rememberResolvedChatId(pendingSession.id, realId);
            this.notifyResolvedSessionIfActive(
              pendingSession.id,
              realId,
              pendingSession.sessionId,
            );
            return resolved;
          }

          const existingResolved = this.sessionList.find((session) => {
            const extended = session as ExtendedSession;
            return extended.realId === backendSession.id;
          }) as ExtendedSession | undefined;
          return existingResolved
            ? mergeResolvedSession(
                backendSession as ExtendedSession,
                existingResolved,
              )
            : backendSession;
        });

        this.sessionList = appendUniqueSessions(this.sessionList, nextSessions);
        this.sessionPage = chatPage.page;
        this.hasMoreSessionPages = chatPage.has_more;
        this.nextSessionCursor = chatPage.next_cursor;
        this.sessionTotal = chatPage.total;
        return [...this.sessionList];
      } finally {
        if (this.ownsTaskListRequest(accessKey, generation))
          this.sessionPageRequest = null;
      }
    })();

    return this.sessionPageRequest;
  }

  /** Track the last session ID that triggered onSessionSelected to avoid duplicate calls. */
  private lastSelectedSessionId: string | null = null;

  setSelectedSessionIntent(sessionId: string | undefined | null): void {
    this.intendedSessionId = sessionId ?? null;
  }

  async getSession(sessionId: string) {
    const existingRequest = this.sessionRequests.get(sessionId);
    if (existingRequest) return existingRequest;

    const requestPromise = this._doGetSession(sessionId);
    this.sessionRequests.set(sessionId, requestPromise);

    try {
      const session = await requestPromise;
      // Trigger onSessionSelected only when session actually changes
      if (
        shouldNotifySessionSelected({
          requestedSessionId: sessionId,
          intendedSessionId: this.intendedSessionId,
        }) &&
        sessionId !== this.lastSelectedSessionId
      ) {
        this.lastSelectedSessionId = sessionId;
        const extendedSession = session as ExtendedSession;
        const realId = extendedSession.realId || null;
        this.onSessionSelected?.(sessionId, realId);
      }
      return session;
    } finally {
      this.sessionRequests.delete(sessionId);
    }
  }

  applyChatSnapshot(
    sessionId: string,
    history: unknown,
  ): IAgentScopeRuntimeWebUIMessage[] | undefined {
    const snapshot = history as Partial<ChatHistory> | null;
    if (!snapshot || !Array.isArray(snapshot.messages)) {
      return undefined;
    }
    const session = this.findSessionByIdentity(sessionId);
    const messages = withArchiveBoundaries(
      convertMessagesForSession(
        snapshot.messages,
        session?.meta,
        session?.name,
      ),
      snapshot.archive?.boundaries,
    ).map((message) => ({ ...message, history: true }));
    if (session) {
      session.messages = messages;
      session.generating = false;
      session.status = "idle";
      this.patchLastUserMessage(messages, false, session.realId || session.id, [
        session.id,
        session.sessionId,
      ]);
    }
    return messages;
  }

  private async getResolvedLocalTimestampSession(
    sessionId: string,
    fromList: ExtendedSession,
  ): Promise<ExtendedSession> {
    const realId = fromList.realId;
    if (!realId) {
      return this.getLocalSession(sessionId) as ExtendedSession;
    }

    const chatHistory = await api.getChat(realId);
    const backendGenerating = isGenerating(chatHistory);
    const backendMessages = withArchiveBoundaries(
      convertMessagesForSession(
        chatHistory.messages || [],
        fromList?.meta || {},
        fromList?.name,
      ),
      chatHistory.archive?.boundaries,
    );
    const messages =
      backendMessages.length > 0 ? backendMessages : fromList.messages || [];
    const generating = mergeGeneratingState(
      chatHistory.status,
      backendGenerating,
      fromList.generating,
    );
    this.patchLastUserMessage(messages, generating, realId, [
      sessionId,
      fromList.sessionId,
    ]);
    const session: ExtendedSession = {
      id: sessionId,
      name: fromList.name || DEFAULT_SESSION_NAME,
      sessionId: fromList.sessionId || sessionId,
      // ==================== userId 统一整改 (Kun He) ====================
      userId: getUserIdWithoutWindow(fromList.userId),
      channel: getChannelWithoutWindow(fromList.channel),
      // ==================== userId 统一整改结束 ====================
      messages,
      meta: fromList.meta || {},
      realId,
      generating,
    };
    this.updateWindowVariables(session);
    return session;
  }

  private async _doGetSession(
    sessionId: string,
  ): Promise<IAgentScopeRuntimeWebUISession> {
    // --- Local timestamp ID (New Chat before first reply) ---
    if (isLocalTimestamp(sessionId)) {
      let fromList = this.sessionList.find((s) => s.id === sessionId) as
        | ExtendedSession
        | undefined;

      // If realId is already resolved, use it directly to fetch history.
      if (fromList?.realId) {
        return this.getResolvedLocalTimestampSession(sessionId, fromList);
      }

      if (fromList?.skipInitialResolve) {
        fromList.skipInitialResolve = false;
        return this.getLocalSession(sessionId);
      }

      // The stream may already have created a backend chat while this tab still
      // only knows the local timestamp id. Refresh once so switching back to a
      // running local session can resolve its backend status before reconnecting.
      if (!fromList?.realId) {
        await this.getSessionList();
        fromList = this.sessionList.find((s) => s.id === sessionId) as
          | ExtendedSession
          | undefined;

        if (fromList?.realId) {
          return this.getResolvedLocalTimestampSession(sessionId, fromList);
        }

        return this.getLocalSession(sessionId);
      }
    }

    // --- No session selected (e.g. after delete) ---
    if (!sessionId || sessionId === "undefined" || sessionId === "null") {
      return this.createEmptySession(createLocalSessionId());
    }

    // --- Regular backend UUID ---
    let fromList = this.sessionList.find((s) => s.id === sessionId) as
      | ExtendedSession
      | undefined;

    const chatHistory = await api.getChat(sessionId);
    if (!fromList && chatHistory.chat) {
      fromList = chatSpecToSession(chatHistory.chat);
      this.sessionList = appendUniqueSessions([fromList], this.sessionList);
    }
    const generating = isGenerating(chatHistory);
    const messages = withArchiveBoundaries(
      convertMessagesForSession(
        chatHistory.messages || [],
        fromList?.meta || {},
        fromList?.name,
      ),
      chatHistory.archive?.boundaries,
    );
    this.patchLastUserMessage(messages, generating, sessionId, [
      fromList?.sessionId,
      fromList?.realId,
    ]);
    const session: ExtendedSession = {
      id: sessionId,
      name: fromList?.name || sessionId,
      sessionId: fromList?.sessionId || sessionId,
      // ==================== userId 统一整改 (Kun He) ====================
      userId: getUserIdWithoutWindow(fromList?.userId),
      channel: getChannelWithoutWindow(fromList?.channel),
      // ==================== userId 统一整改结束 ====================
      messages,
      meta: fromList?.meta || {},
      generating,
    };

    this.updateWindowVariables(session);
    return session;
  }

  async updateSession(
    session: Partial<IAgentScopeRuntimeWebUISession>,
    options?: IAgentScopeRuntimeWebUISessionUpdateOptions,
  ) {
    const shouldKeepLocalMessages = Boolean(
      session.id &&
        isLocalTimestamp(session.id) &&
        !this.getRealIdForSession(session.id),
    );
    const nextSession = {
      ...session,
      messages:
        options?.refreshList === false
          ? session.messages || []
          : shouldKeepLocalMessages
          ? session.messages || []
          : [],
    };

    if (options?.refreshList === false) {
      this.patchSessionLocally(nextSession);
      return [...this.sessionList];
    }

    const index = this.sessionList.findIndex((s) => s.id === nextSession.id);

    if (index > -1) {
      this.sessionList[index] = { ...this.sessionList[index], ...nextSession };

      const existing = this.sessionList[index] as ExtendedSession;
      if (isLocalTimestamp(existing.id) && !existing.realId) {
        const tempId = existing.id;
        await this.getSessionList().then(() => {
          const { list, realId } = resolveRealId(this.sessionList, tempId);
          if (realId) {
            this.sessionList = list;
            rememberResolvedChatId(tempId, realId);
            this.notifyResolvedSessionIfActive(tempId, realId);
          }
        });
      } else {
        const tempId = nextSession.id!;
        await this.getSessionList().then(() => {
          const { list, realId } = resolveRealId(this.sessionList, tempId);
          this.sessionList = list;
          if (realId) {
            rememberResolvedChatId(tempId, realId);
            this.notifyResolvedSessionIfActive(tempId, realId);
          }
        });
      }
    } else {
      if (shouldKeepLocalMessages) {
        this.sessionList = mergePendingSession(
          this.sessionList,
          nextSession as ExtendedSession,
        );
      }

      const tempId = nextSession.id!;
      await this.getSessionList().then(() => {
        const { list, realId } = resolveRealId(this.sessionList, tempId);
        this.sessionList = list;
        if (realId) {
          rememberResolvedChatId(tempId, realId);
          this.notifyResolvedSessionIfActive(tempId, realId);
        }
      });
    }

    return [...this.sessionList];
  }

  async createSession(session: Partial<IAgentScopeRuntimeWebUISession>) {
    // 生成临时时间戳ID（只在内存中使用，不添加到历史列表）
    session.id = createLocalSessionId();

    // ==================== userId 统一整改 (Kun He) ====================
    // 使用 getUserId() 获取用户 ID
    const extended: ExtendedSession = {
      ...session,
      sessionId: session.id,
      userId: getUserId(),
      channel: getChannel(),
      createdAt: new Date().toISOString(),
      name: session.name || DEFAULT_SESSION_NAME,
      messages: [],
      meta: {},
      skipInitialResolve: true,
    } as ExtendedSession;
    // ==================== userId 统一整改结束 ====================

    // 等消息发送完成后，后端返回真实UUID时才创建历史记录
    this.updateWindowVariables(extended);

    this.sessionList = mergePendingSession(this.sessionList, extended);

    // 触发回调（URL 清空，不导航到临时ID）
    this.onSessionCreated?.(session.id);

    // 返回当前列表（不包含临时会话）
    return [...this.sessionList];
  }

  async removeSession(session: Partial<IAgentScopeRuntimeWebUISession>) {
    if (!session.id) return [...this.sessionList];

    const { id: sessionId } = session;

    const existing = this.sessionList.find((s) => s.id === sessionId) as
      | ExtendedSession
      | undefined;

    const deleteId =
      this.getChatIdForSession(sessionId) ??
      (isLocalTimestamp(sessionId) ? null : sessionId);

    if (deleteId) await api.deleteChat(deleteId);

    this.sessionList = this.sessionList.filter((s) => s.id !== sessionId);

    const resolvedId = existing?.realId ?? sessionId;
    forgetResolvedChatId(sessionId);
    if (deleteId) {
      forgetResolvedChatIdsForChat(deleteId);
    }
    this.onSessionRemoved?.(resolvedId);

    return [...this.sessionList];
  }
}

export default new SessionApi();
