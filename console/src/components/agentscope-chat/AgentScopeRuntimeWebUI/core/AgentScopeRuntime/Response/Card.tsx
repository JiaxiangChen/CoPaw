import { type ReactNode, useMemo } from "react";
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
  IAgentScopeRuntimeMessage,
  IAgentScopeRuntimeResponse,
} from "../types";
import AgentScopeRuntimeResponseBuilder from "./Builder";
import Message from "./Message";
import Tool from "./Tool";
import OperationGroup from "./OperationGroup";
import {
  groupOperationMessages,
  isOperationGroupToolMessage,
} from "./operationGrouping";
import type { OperationGroupedItem } from "./operationGrouping";
import Reasoning from "./Reasoning";
import Error from "./Error";
import { Bubble, Markdown } from "@/components/agentscope-chat";
import Actions from "./Actions";
import Suggestions from "./Suggestions";
import RetryStatusMessage from "./RetryStatusMessage";
import { getCompletedReasoningFallbackText } from "./reasoningFallback";
import ProcessDisclosure from "./ProcessDisclosure";
import { resolveToolName } from "./ToolTitle";
import { readStopAppendMetadata } from "@/pages/Chat/stopAppend";
import ConversationCompactionBoundary from "@/pages/Chat/components/ConversationCompactionBoundary";
import type { ChatCompactionBoundary } from "@/api/types/chat";
// import { Avatar, Flex } from "antd";

type RetryMetadata = {
  retry_status?: unknown;
  metadata?: {
    retry_status?: unknown;
  };
};

function getRetryStatus(item: unknown) {
  const metadata = (item as { metadata?: RetryMetadata }).metadata;
  return metadata?.retry_status || metadata?.metadata?.retry_status;
}

function isGeneratingStatus(status: unknown) {
  return (
    status === AgentScopeRuntimeRunStatus.Created ||
    status === AgentScopeRuntimeRunStatus.InProgress
  );
}

function messageHasGeneratingContent(message: IAgentScopeRuntimeMessage) {
  if (isGeneratingStatus(message.status)) return true;
  return Boolean(
    message.content?.some((content) => isGeneratingStatus(content.status)),
  );
}

function responseReadyForProcessDisclosure(
  response: IAgentScopeRuntimeResponse,
  messages: IAgentScopeRuntimeMessage[],
) {
  if (AgentScopeRuntimeResponseBuilder.maybeGenerating(response)) return false;
  if (messages.some(messageHasGeneratingContent)) return false;

  return (
    AgentScopeRuntimeResponseBuilder.maybeDone(response) ||
    String(response.status) === "idle"
  );
}

function hasVisibleAnswerContent(message: IAgentScopeRuntimeMessage) {
  if (message.type !== AgentScopeRuntimeMessageType.MESSAGE) return false;
  if (getRetryStatus(message)) return false;

  return Boolean(
    message.content?.some((content) => {
      switch (content.type) {
        case AgentScopeRuntimeContentType.TEXT:
          return Boolean(content.text?.trim());
        case AgentScopeRuntimeContentType.REFUSAL:
          return Boolean(content.refusal?.trim());
        case AgentScopeRuntimeContentType.IMAGE:
          return Boolean(content.image_url);
        case AgentScopeRuntimeContentType.VIDEO:
          return Boolean(content.video_url);
        case AgentScopeRuntimeContentType.FILE:
          return Boolean(
            content.file_url || content.file_name || content.fileName,
          );
        case AgentScopeRuntimeContentType.AUDIO:
          return Boolean(content.audio_url || content.data);
        case AgentScopeRuntimeContentType.DATA:
          return Boolean(content.data);
        default:
          return false;
      }
    }),
  );
}

function findLastVisibleAnswerMessageIndex(
  messages: IAgentScopeRuntimeMessage[],
) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (hasVisibleAnswerContent(messages[index])) return index;
  }

  return -1;
}

function isToolMessageType(type: AgentScopeRuntimeMessageType) {
  return [
    AgentScopeRuntimeMessageType.PLUGIN_CALL,
    AgentScopeRuntimeMessageType.PLUGIN_CALL_OUTPUT,
    AgentScopeRuntimeMessageType.MCP_CALL,
    AgentScopeRuntimeMessageType.MCP_CALL_OUTPUT,
  ].includes(type);
}

const HIDDEN_PROCESS_TOOL_NAMES = new Set(["update_task_progress"]);

function isHiddenToolMessage(message: IAgentScopeRuntimeMessage) {
  if (!isToolMessageType(message.type)) return false;

  return Boolean(
    message.content?.some((content) => {
      if (content.type !== AgentScopeRuntimeContentType.DATA) return false;
      const data = content.data as Record<string, unknown>;
      const toolName = resolveToolName(data);
      return toolName ? HIDDEN_PROCESS_TOOL_NAMES.has(toolName) : false;
    }),
  );
}

function shouldFoldIntoProcessDisclosure(message: IAgentScopeRuntimeMessage) {
  return (
    message.type !== AgentScopeRuntimeMessageType.HEARTBEAT &&
    !isHiddenToolMessage(message)
  );
}

function shouldCountAsProcessStep(message: IAgentScopeRuntimeMessage) {
  return (
    message.type === AgentScopeRuntimeMessageType.REASONING ||
    message.type === AgentScopeRuntimeMessageType.ERROR ||
    isOperationGroupToolMessage(message) ||
    Boolean(getRetryStatus(message))
  );
}

function messageHasFailedProcess(message: IAgentScopeRuntimeMessage) {
  if (message.status === AgentScopeRuntimeRunStatus.Failed) return true;
  return Boolean(
    message.content?.some((content) => {
      if (content.type !== AgentScopeRuntimeContentType.DATA) return false;
      const data = content.data as Record<string, unknown>;
      return (
        data.tool_status === "failed" ||
        Boolean(data.tool_error) ||
        data.isError === true
      );
    }),
  );
}

function formatDurationSeconds(totalSeconds: number) {
  if (totalSeconds < 1) return "<1s";

  const seconds = Math.round(totalSeconds);
  if (seconds < 60) return `${seconds}s`;

  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) {
    return remainingSeconds > 0
      ? `${minutes}m ${remainingSeconds}s`
      : `${minutes}m`;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes > 0 ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function parseTimestampSeconds(value: unknown) {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return undefined;
    return value >= 1_000_000_000_000 ? value / 1000 : value;
  }

  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return undefined;

    const numericValue = Number(trimmed);
    if (Number.isFinite(numericValue)) {
      return numericValue >= 1_000_000_000_000
        ? numericValue / 1000
        : numericValue;
    }

    const dateValue = Date.parse(trimmed);
    if (Number.isNaN(dateValue)) return undefined;
    return dateValue / 1000;
  }

  return undefined;
}

function getMessageTimestampSeconds(message: IAgentScopeRuntimeMessage) {
  return parseTimestampSeconds(
    (message as IAgentScopeRuntimeMessage & { timestamp?: unknown }).timestamp,
  );
}

function getMessagesDurationText(messages: IAgentScopeRuntimeMessage[]) {
  const timestamps = messages
    .map(getMessageTimestampSeconds)
    .filter((timestamp): timestamp is number => timestamp !== undefined);

  if (timestamps.length < 2) return undefined;

  const durationSeconds = timestamps[timestamps.length - 1] - timestamps[0];
  if (!Number.isFinite(durationSeconds) || durationSeconds < 0) {
    return undefined;
  }

  return formatDurationSeconds(durationSeconds);
}

function renderResponseItem(item: IAgentScopeRuntimeMessage) {
  switch (item.type) {
    case AgentScopeRuntimeMessageType.CONVERSATION_COMPACTION_BOUNDARY: {
      const content = item.content[0];
      if (content?.type !== AgentScopeRuntimeContentType.DATA) return null;
      return (
        <ConversationCompactionBoundary
          key={item.id}
          data={content.data as ChatCompactionBoundary}
        />
      );
    }
    case AgentScopeRuntimeMessageType.MESSAGE: {
      // 检测重试状态，使用专用卡片渲染。
      // SSE 流式路径: metadata.retry_status
      // 历史加载路径: metadata.metadata.retry_status（后端嵌套）
      const retryStatus = getRetryStatus(item);
      if (retryStatus) {
        return <RetryStatusMessage key={item.id} data={item} />;
      }
      return <Message key={item.id} data={item} />;
    }
    case AgentScopeRuntimeMessageType.PLUGIN_CALL:
    case AgentScopeRuntimeMessageType.PLUGIN_CALL_OUTPUT:
    case AgentScopeRuntimeMessageType.MCP_CALL:
    case AgentScopeRuntimeMessageType.MCP_CALL_OUTPUT:
      return <Tool key={item.id} data={item} />;
    case AgentScopeRuntimeMessageType.MCP_APPROVAL_REQUEST:
      return <Tool key={item.id} data={item} isApproval={true} />;
    case AgentScopeRuntimeMessageType.REASONING:
      return <Reasoning key={item.id} data={item} />;
    case AgentScopeRuntimeMessageType.ERROR:
      return <Error key={item.id} data={item} />;
    case AgentScopeRuntimeMessageType.HEARTBEAT:
      return null;
    default:
      console.warn(`[WIP] Unknown message type: ${item.type}`);
      return null;
  }
}

function renderGroupedItem(item: OperationGroupedItem) {
  const anchorId =
    item.kind === "group" ? `operation:${item.key}` : item.message.id;
  const fullWidth =
    item.kind === "group" ||
    isOperationGroupToolMessage(item.message) ||
    item.message.type === AgentScopeRuntimeMessageType.MCP_APPROVAL_REQUEST;
  return (
    <div
      key={anchorId}
      data-history-anchor-id={anchorId}
      style={fullWidth ? { width: "100%", minWidth: 0 } : undefined}
    >
      {item.kind === "group" ? (
        <OperationGroup entry={item} />
      ) : (
        renderResponseItem(item.message)
      )}
    </div>
  );
}

function messagesForGroupedItem(
  item: OperationGroupedItem,
): IAgentScopeRuntimeMessage[] {
  return item.kind === "group" ? item.steps : [item.message];
}

export default function AgentScopeRuntimeResponseCard(props: {
  data: IAgentScopeRuntimeResponse;
  isLast?: boolean;
  beforeActions?: ReactNode;
}) {
  // const avatar = useChatAnywhereOptions((v) => v.welcome.avatar);
  // const nick = useChatAnywhereOptions((v) => v.welcome.nick);
  const messages = useMemo(() => {
    return AgentScopeRuntimeResponseBuilder.mergeToolMessages(
      props.data.output,
    );
  }, [props.data.output]);
  const reasoningFallbackText = useMemo(() => {
    return getCompletedReasoningFallbackText(props.data, messages);
  }, [messages, props.data]);
  const groupedMessages = useMemo(() => {
    const canCollapseProcess = responseReadyForProcessDisclosure(
      props.data,
      messages,
    );
    const finalAnswerIndex = reasoningFallbackText
      ? -1
      : findLastVisibleAnswerMessageIndex(messages);
    const hasAnswer = Boolean(reasoningFallbackText || finalAnswerIndex >= 0);

    // Operation groups are explicit (agent-declared) and always render as
    // their own default-collapsed entries; they never fold into the
    // process disclosure and never merge with messages outside the group.
    const { items } = groupOperationMessages(messages);

    if (!canCollapseProcess || !hasAnswer) {
      return {
        process: [] as OperationGroupedItem[],
        direct: items,
        failedProcessCount: 0,
        processStepCount: 0,
        toolCallCount: 0,
      };
    }

    const process: OperationGroupedItem[] = [];
    const direct: OperationGroupedItem[] = [];

    items.forEach((item) => {
      if (item.kind === "group") {
        process.push(item);
        return;
      }
      const message = item.message;
      if (finalAnswerIndex >= 0 && message === messages[finalAnswerIndex]) {
        direct.push({ kind: "message", message });
        return;
      }

      if (shouldFoldIntoProcessDisclosure(message)) {
        process.push(item);
      } else {
        direct.push({ kind: "message", message });
      }
    });

    const processMessages = process.flatMap(messagesForGroupedItem);
    return {
      process,
      direct,
      failedProcessCount: processMessages.filter(messageHasFailedProcess)
        .length,
      processStepCount: processMessages.filter(shouldCountAsProcessStep).length,
      toolCallCount: processMessages.filter(isOperationGroupToolMessage).length,
    };
  }, [messages, props.data, reasoningFallbackText]);
  const durationText = useMemo(() => {
    return getMessagesDurationText(messages);
  }, [messages]);
  const hasModelCallFailed =
    props.data.error?.code === "model_call_failed" ||
    messages.some(
      (message) =>
        message.type === AgentScopeRuntimeMessageType.ERROR &&
        message.code === "model_call_failed",
    );
  if (
    !messages?.length &&
    AgentScopeRuntimeResponseBuilder.maybeGenerating(props.data)
  )
    return <Bubble.Spin />;

  return (
    <>
      {/* {avatar && (
        <Flex align="center" gap={8} style={{ marginBottom: 8 }}>
          <Avatar src={avatar} />
          {nick && <span>{nick as string}</span>}
        </Flex>
      )} */}
      {groupedMessages.process.length > 0 && (
        <div
          data-history-anchor-id={`process:${props.data.id}`}
          style={{ width: "100%", minWidth: 0 }}
        >
          <ProcessDisclosure
            durationText={durationText}
            failedCount={groupedMessages.failedProcessCount}
            processCount={groupedMessages.processStepCount}
            toolCallCount={groupedMessages.toolCallCount}
            status={
              props.data.status === AgentScopeRuntimeRunStatus.Canceled
                ? "canceled"
                : "completed"
            }
          >
            {groupedMessages.process.map(renderGroupedItem)}
          </ProcessDisclosure>
        </div>
      )}
      {groupedMessages.direct.map(renderGroupedItem)}
      {reasoningFallbackText && <Markdown content={reasoningFallbackText} />}
      {props.data.error && <Error data={props.data.error} />}
      {props.beforeActions}
      <Actions
        {...props}
        hideReplace={
          hasModelCallFailed ||
          messages.some((message) => Boolean(readStopAppendMetadata(message)))
        }
      />
      {props.data.suggestions?.length > 0 && (
        <Suggestions
          suggestions={props.data.suggestions}
          status={props.data.status}
        />
      )}
    </>
  );
}
