import type { IAgentScopeRuntimeWebUIMessage } from "@/components/agentscope-chat";
import type { Message } from "@/api/types/chat";
import { readStopAppendMetadata } from "../stopAppend";

const RESPONSE_CARD = "AgentScopeRuntimeResponseCard";
const BOUNDARY_CARD = "ConversationCompactionBoundary";
const FEEDBACK_CARD = "ResponseFeedback";
export const INLINE_COMPACTION_TYPE = "conversation_compaction_boundary";

type HistoryMessage = IAgentScopeRuntimeWebUIMessage & {
  history?: boolean;
  historyAnchorIds?: string[];
  historyResponseCardIds?: string[];
};

function fragmentIdentity(message: Message): string | null {
  if (message.role === "user" || typeof message.id !== "string") return null;
  const metadata = message.metadata as Record<string, unknown> | undefined;
  if (!metadata || metadata.original_id !== message.id) return null;

  const serialized = JSON.stringify([
    message.role,
    message.type,
    message.content,
    metadata.metadata,
  ]);
  let hash = 2166136261;
  for (let index = 0; index < serialized.length; index += 1) {
    hash = Math.imul(hash ^ serialized.charCodeAt(index), 16777619);
  }
  return `${message.id}:fragment:${(hash >>> 0).toString(16)}`;
}

/** Old servers assign every runtime fragment its source Msg ID. */
export function normalizeHistoryMessages(messages: Message[]): Message[] {
  return messages.map((message) => {
    const id = fragmentIdentity(message);
    if (!id) return message;
    return { ...message, id };
  });
}

export function historyMessageMetadata(
  message: Message,
): Record<string, unknown> {
  const metadata =
    message.metadata && typeof message.metadata === "object"
      ? (message.metadata as Record<string, unknown>)
      : {};
  const nested = metadata.metadata;
  return nested && typeof nested === "object"
    ? { ...metadata, ...(nested as Record<string, unknown>) }
    : metadata;
}

function isIndependentOutput(message: Message): boolean {
  if (readStopAppendMetadata(message)) return true;
  return Boolean(historyMessageMetadata(message).cron_task);
}

function responseIdentity(
  messages: Message[],
  key: string,
): string | null | undefined {
  const runIds = new Set(
    messages
      .map((message) => historyMessageMetadata(message)[key])
      .filter((runId): runId is string => typeof runId === "string" && !!runId),
  );
  if (runIds.size > 1) return undefined;
  return runIds.values().next().value ?? null;
}

function canMergeResponses(older: Message[], newer: Message[]): boolean {
  const olderRunId = responseIdentity(older, "task_run_id");
  const newerRunId = responseIdentity(newer, "task_run_id");
  const olderTurnId = responseIdentity(older, "turn_id");
  const newerTurnId = responseIdentity(newer, "turn_id");
  return (
    ((olderRunId === null && newerRunId === null) ||
      (typeof olderRunId === "string" && olderRunId === newerRunId)) &&
    olderTurnId !== undefined &&
    newerTurnId !== undefined &&
    (!olderTurnId || !newerTurnId || olderTurnId === newerTurnId)
  );
}

function responseOutput(message: HistoryMessage): Message[] | null {
  if (
    !message.history ||
    message.role !== "assistant" ||
    message.msgStatus === "generating"
  )
    return null;
  const card = message.cards?.[0];
  if (card?.code !== RESPONSE_CARD || !Array.isArray(card.data?.output))
    return null;
  const output = card.data.output as Message[];
  return output.length && !output.some(isIndependentOutput) ? output : null;
}

function inlineBoundary(message: HistoryMessage): Message {
  return {
    id: message.id,
    role: "system",
    type: INLINE_COMPACTION_TYPE,
    status: "completed",
    content: [
      { type: "data", status: "completed", data: message.cards?.[0].data },
    ],
  };
}

function mergeOrderedMessages<T extends { id?: unknown }>(
  older: T[],
  newer: T[],
): T[] {
  const newerIds = new Set(newer.map((message) => message.id));
  const before = new Map<unknown, T[]>();
  let pending: T[] = [];
  let lastSharedId: unknown;
  for (const message of older) {
    if (typeof message.id === "string" && newerIds.has(message.id)) {
      before.set(message.id, pending);
      pending = [];
      lastSharedId = message.id;
    } else {
      pending.push(message);
    }
  }
  const result = lastSharedId === undefined ? [...pending] : [];
  for (const message of newer) {
    result.push(...(before.get(message.id) || []), message);
    if (message.id === lastSharedId) result.push(...pending);
  }
  const seen = new Set<string>();
  return result.filter((message) => {
    if (typeof message.id !== "string") return true;
    if (seen.has(message.id)) return false;
    seen.add(message.id);
    return true;
  });
}

function mergeResponsePair(
  older: HistoryMessage,
  separators: HistoryMessage[],
  newer: HistoryMessage,
): HistoryMessage {
  const olderCard = older.cards![0];
  const newerCard = newer.cards![0];
  const data = {
    ...newerCard.data,
    output: mergeOrderedMessages(
      [...olderCard.data.output, ...separators.map(inlineBoundary)],
      newerCard.data.output,
    ),
    headerMeta: olderCard.data.headerMeta || newerCard.data.headerMeta,
    planReviewCard:
      newerCard.data.planReviewCard || olderCard.data.planReviewCard,
  };
  const extraCards = new Map<
    string,
    NonNullable<HistoryMessage["cards"]>[number]
  >();
  for (const card of [...(older.cards || []), ...(newer.cards || [])]) {
    if (card.code !== RESPONSE_CARD && card.code !== FEEDBACK_CARD) {
      extraCards.set(`${card.code}:${JSON.stringify(card.data)}`, card);
    }
  }
  const feedback = [...(newer.cards || []), ...(older.cards || [])].find(
    (card) => card.code === FEEDBACK_CARD,
  );
  return {
    ...newer,
    historyAnchorIds: Array.from(
      new Set([
        older.id,
        ...(older.historyAnchorIds || []),
        ...(newer.historyAnchorIds || []),
      ]),
    ).filter((id) => id !== newer.id),
    historyResponseCardIds: Array.from(
      new Set([
        olderCard.data.id,
        ...(older.historyResponseCardIds || []),
        ...(newer.historyResponseCardIds || []),
      ]),
    ).filter((id) => typeof id === "string" && id !== newerCard.data.id),
    cards: [
      { ...newerCard, data },
      ...extraCards.values(),
      ...(feedback ? [{ ...feedback, data }] : []),
    ],
  };
}

function reconcileOverlappingResponses(
  messages: HistoryMessage[],
): HistoryMessage[] {
  const result = [...messages];
  const owners = new Map<string, number>();
  const removed = new Set<number>();
  const messageIds = new Set<string>();
  for (let index = result.length - 1; index >= 0; index -= 1) {
    const message = result[index];
    const output = responseOutput(message);
    if (!output) {
      if (messageIds.has(message.id)) removed.add(index);
      messageIds.add(message.id);
      continue;
    }
    const overlaps = new Set(
      output
        .map((item) =>
          typeof item.id === "string" ? owners.get(item.id) : undefined,
        )
        .filter((owner): owner is number => owner !== undefined),
    );
    const owner =
      overlaps.size === 1 ? overlaps.values().next().value : undefined;
    if (
      owner !== undefined &&
      canMergeResponses(output, responseOutput(result[owner])!)
    ) {
      const separators: HistoryMessage[] = [];
      for (
        let separator = index + 1;
        separator < owner &&
        result[separator].cards?.[0]?.code === BOUNDARY_CARD;
        separator += 1
      ) {
        separators.push(result[separator]);
        removed.add(separator);
      }
      result[owner] = mergeResponsePair(message, separators, result[owner]);
      removed.add(index);
      output.forEach((item) => {
        if (typeof item.id === "string") owners.set(item.id, owner);
      });
    } else {
      output.forEach((item) => {
        if (typeof item.id === "string") owners.set(item.id, index);
      });
    }
  }
  return result.filter((_, index) => !removed.has(index));
}

/** HTTP pages and compaction dividers are not user-turn boundaries. */
export function mergeHistoryResponses(
  messages: IAgentScopeRuntimeWebUIMessage[],
): IAgentScopeRuntimeWebUIMessage[] {
  const result: HistoryMessage[] = [];
  for (const message of reconcileOverlappingResponses(
    messages as HistoryMessage[],
  )) {
    if (responseOutput(message)) {
      let previousIndex = result.length - 1;
      while (
        previousIndex >= 0 &&
        result[previousIndex].cards?.[0]?.code === BOUNDARY_CARD
      ) {
        previousIndex -= 1;
      }
      const previous = result[previousIndex];
      const previousOutput = previous && responseOutput(previous);
      const currentOutput = responseOutput(message);
      if (
        previousOutput &&
        currentOutput &&
        canMergeResponses(previousOutput, currentOutput)
      ) {
        result.splice(
          previousIndex,
          result.length - previousIndex,
          mergeResponsePair(previous, result.slice(previousIndex + 1), message),
        );
        continue;
      }
    }
    result.push(message);
  }
  return result;
}

/** Resolve source precedence before shared user IDs determine timeline positions. */
export function reconcileHistoryLayers(
  retained: IAgentScopeRuntimeWebUIMessage[],
  newer: IAgentScopeRuntimeWebUIMessage[],
): IAgentScopeRuntimeWebUIMessage[] {
  const refreshed = [...newer];
  const newerBoundaryIds = new Set(
    refreshed
      .filter((message) => message.cards?.[0]?.code === BOUNDARY_CARD)
      .map((message) => message.id),
  );
  const owners = new Map<string, number>();
  refreshed.forEach((message, index) => {
    responseOutput(message)?.forEach((item) => {
      if (typeof item.id === "string") owners.set(item.id, index);
    });
  });
  const removed = new Set<number>();
  const matched = new Map<number, number>();
  for (let index = retained.length - 1; index >= 0; index -= 1) {
    const message = retained[index];
    const output = responseOutput(message);
    if (!output) continue;
    const overlaps = new Set(
      output.flatMap((item) => {
        const owner =
          typeof item.id === "string" ? owners.get(item.id) : undefined;
        return owner === undefined ? [] : [owner];
      }),
    );
    const owner =
      overlaps.size === 1 ? overlaps.values().next().value : undefined;
    if (
      owner === undefined ||
      !canMergeResponses(output, responseOutput(refreshed[owner])!)
    )
      continue;
    const separators: HistoryMessage[] = [];
    for (
      let separator = index + 1;
      separator < retained.length &&
      retained[separator].cards?.[0]?.code === BOUNDARY_CARD;
      separator += 1
    ) {
      if (!newerBoundaryIds.has(retained[separator].id)) {
        separators.push(retained[separator]);
      }
      removed.add(separator);
    }
    refreshed[owner] = mergeResponsePair(message, separators, refreshed[owner]);
    matched.set(index, owner);
    output.forEach((item) => {
      if (typeof item.id === "string") owners.set(item.id, owner);
    });
  }
  return mergeHistoryResponses(
    mergeOrderedMessages(
      retained.flatMap((message, index) => {
        if (removed.has(index)) return [];
        const owner = matched.get(index);
        return [owner === undefined ? message : refreshed[owner]];
      }),
      refreshed,
    ),
  );
}

export function resolveHistoryAnchorId(
  messages: IAgentScopeRuntimeWebUIMessage[],
  anchorId: string,
): string {
  // Process anchors use response-card IDs; bubble aliases are a separate namespace.
  const prefix = anchorId.startsWith("process:") ? "process:" : "";
  const responseId = anchorId.slice(prefix.length);
  const owner = (messages as HistoryMessage[]).find((message) => {
    if (prefix) {
      return (
        message.cards?.[0]?.code === RESPONSE_CARD &&
        (message.cards[0].data?.id === responseId ||
          message.historyResponseCardIds?.includes(responseId))
      );
    }
    return (
      message.id === responseId ||
      message.historyAnchorIds?.includes(responseId)
    );
  });
  if (!owner) return anchorId;
  return prefix ? `${prefix}${owner.cards![0].data.id}` : owner.id;
}
