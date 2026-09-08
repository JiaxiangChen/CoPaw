import { Fragment, useState } from "react";
import type { FeedbackRecord } from "@/api/types/feedback";
import {
  extractDecodedFileNameFromUrl,
  isAutoPreviewHtmlLink,
} from "@/components/agentscope-chat/FilePreviewModal/fileUtils";
import type { IAgentScopeRuntimeWebUIMessage } from "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/types/IMessages";
import {
  findFeedbackForResponse,
  type FeedbackLookupMap,
} from "../../feedbackLookup";
import type {
  ChatApprovalActionCardData,
  ChatRuntimeRequestCardData,
  ChatRuntimeResponseCardData,
  ChatTaskRunGroupCardData,
} from "../../messageMeta";
import { formatMessageTime } from "../../messageMeta";
import ApprovalActionCard from "../ApprovalActionCard";
import RuntimeRequestCard from "../RuntimeRequestCard";
import RuntimeResponseCard, {
  RuntimeResponseFeedbackCard,
} from "../RuntimeResponseCard";
import type { ResponseFeedbackTaskMeta } from "../ResponseFeedbackCard";
import { FilePreviewPresentationProvider } from "@/components/agentscope-chat/FilePreviewPresentationContext";

const MARKDOWN_LINK_PATTERN = /!?\[([^\]]*)\]\(([^)]+)\)/g;
const PLAIN_URL_PATTERN = /https?:\/\/[^\s<>"']+/g;
const TRAILING_URL_PUNCTUATION_PATTERN = /[\]),.。！？!?,，；;：:]+$/;
const UNSAFE_PREVIEW_URL_CHARACTER_PATTERN = /[\s{}<>"'`\\]/;
const EXPLICIT_PREVIEW_URL_PREFIX_PATTERN =
  /^(?:https?:\/\/|\/\/|\/|\.\/|\.\.\/)/i;
const HTML_PREVIEW_PATH_PATTERN = /\.html?(?:[?#].*)?$/i;

type AutoPreviewHtmlMatch = {
  url: string;
  fileName?: string;
};
type RuntimeMessageCard = NonNullable<
  IAgentScopeRuntimeWebUIMessage["cards"]
>[number];

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function isValidAutoPreviewUrlCandidate(value: string): boolean {
  if (!value || UNSAFE_PREVIEW_URL_CHARACTER_PATTERN.test(value)) {
    return false;
  }

  try {
    const parsedUrl = new URL(value, window.location.origin);
    if (parsedUrl.protocol !== "http:" && parsedUrl.protocol !== "https:") {
      return false;
    }
  } catch {
    return false;
  }

  return (
    EXPLICIT_PREVIEW_URL_PREFIX_PATTERN.test(value) ||
    HTML_PREVIEW_PATH_PATTERN.test(value)
  );
}

function toAutoPreviewHtmlMatch(
  value: string,
  fileName?: string,
): AutoPreviewHtmlMatch | null {
  const url = value.replace(TRAILING_URL_PUNCTUATION_PATTERN, "").trim();
  if (
    !isValidAutoPreviewUrlCandidate(url) ||
    !isAutoPreviewHtmlLink(url, fileName)
  ) {
    return null;
  }
  return { url, fileName };
}

function findAutoPreviewHtmlTextMatch(
  value: string,
): AutoPreviewHtmlMatch | null {
  const directMatch = toAutoPreviewHtmlMatch(value);
  if (directMatch) return directMatch;

  let latestMatch: AutoPreviewHtmlMatch | null = null;
  let latestMatchEnd = -1;
  MARKDOWN_LINK_PATTERN.lastIndex = 0;
  let match = MARKDOWN_LINK_PATTERN.exec(value);
  while (match) {
    const [, fileName, url] = match;
    const markdownMatch = toAutoPreviewHtmlMatch(url, fileName);
    if (markdownMatch) {
      latestMatch = markdownMatch;
      latestMatchEnd = MARKDOWN_LINK_PATTERN.lastIndex;
    }
    match = MARKDOWN_LINK_PATTERN.exec(value);
  }

  PLAIN_URL_PATTERN.lastIndex = 0;
  let urlMatch = PLAIN_URL_PATTERN.exec(value);
  while (urlMatch) {
    const plainMatch = toAutoPreviewHtmlMatch(urlMatch[0]);
    if (plainMatch && urlMatch.index >= latestMatchEnd) {
      latestMatch = plainMatch;
      latestMatchEnd = PLAIN_URL_PATTERN.lastIndex;
    }
    urlMatch = PLAIN_URL_PATTERN.exec(value);
  }

  return latestMatch;
}

function findAutoPreviewHtmlValue(
  value: unknown,
  depth = 0,
): AutoPreviewHtmlMatch | null {
  if (depth > 6) {
    return null;
  }

  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try {
        const parsedMatch = findAutoPreviewHtmlValue(
          JSON.parse(trimmed),
          depth + 1,
        );
        if (parsedMatch) return parsedMatch;
      } catch {
        // Fall through to text scanning for malformed or mixed tool output.
      }
    }

    return findAutoPreviewHtmlTextMatch(value);
  }

  if (Array.isArray(value)) {
    for (const item of [...value].reverse()) {
      const match = findAutoPreviewHtmlValue(item, depth + 1);
      if (match) return match;
    }
    return null;
  }

  if (!isRecord(value)) {
    return null;
  }

  const url =
    readString(value.file_url) ||
    readString(value.previewUrl) ||
    readString(value.preview_url) ||
    readString(value.url) ||
    readString(value.href);
  const fileName =
    readString(value.file_name) ||
    readString(value.fileName) ||
    readString(value.filename) ||
    readString(value.name) ||
    readString(value.file_id);
  if (url) {
    const directMatch = toAutoPreviewHtmlMatch(url, fileName);
    if (directMatch) return directMatch;

    const embeddedMatch = findAutoPreviewHtmlTextMatch(url);
    if (embeddedMatch) return embeddedMatch;
  }

  for (const key of ["path", "content", "data", "output", "text", "value"]) {
    const match = findAutoPreviewHtmlValue(value[key], depth + 1);
    if (match) return match;
  }

  return null;
}

function buildAutoPreviewText(match: AutoPreviewHtmlMatch): string {
  const fileName =
    match.fileName || extractDecodedFileNameFromUrl(match.url, "preview.html");
  return `[${fileName}](${match.url})`;
}

function buildAutoPreviewOutputMessage(
  outputMessage: Record<string, unknown>,
  match: AutoPreviewHtmlMatch,
) {
  return {
    ...outputMessage,
    role: "assistant",
    type: "message",
    content: [
      {
        type: "text",
        status: "completed",
        text: buildAutoPreviewText(match),
      },
    ],
  };
}

function shouldReuseAutoPreviewContentItem(contentItem: unknown): boolean {
  return isRecord(contentItem) && contentItem.type === "file";
}

function pickAutoPreviewHtmlResponseData(
  data: ChatRuntimeResponseCardData,
): ChatRuntimeResponseCardData | null {
  const output = Array.isArray(data.output) ? data.output : [];

  for (const outputMessage of [...output].reverse()) {
    if (!isRecord(outputMessage)) {
      continue;
    }

    const content = Array.isArray(outputMessage.content)
      ? outputMessage.content
      : [];
    for (const contentItem of [...content].reverse()) {
      const match = findAutoPreviewHtmlValue(contentItem);
      if (match) {
        const previewOutputMessage = shouldReuseAutoPreviewContentItem(
          contentItem,
        )
          ? {
              ...outputMessage,
              role: "assistant",
              type: "message",
              content: [contentItem],
            }
          : buildAutoPreviewOutputMessage(outputMessage, match);
        return {
          ...data,
          output: [
            previewOutputMessage,
          ] as ChatRuntimeResponseCardData["output"],
        };
      }
    }

    const match = findAutoPreviewHtmlValue(outputMessage);
    if (match) {
      return {
        ...data,
        output: [
          buildAutoPreviewOutputMessage(outputMessage, match),
        ] as ChatRuntimeResponseCardData["output"],
      };
    }
  }

  return null;
}

function findAutoPreviewHtmlMessages(
  messages: IAgentScopeRuntimeWebUIMessage[],
): IAgentScopeRuntimeWebUIMessage[] | null {
  for (const message of [...messages].reverse()) {
    const cards = message.cards || [];
    for (let cardIndex = cards.length - 1; cardIndex >= 0; cardIndex -= 1) {
      const card = cards[cardIndex];
      if (card.code !== "AgentScopeRuntimeResponseCard") {
        continue;
      }

      const autoPreviewData = pickAutoPreviewHtmlResponseData(
        card.data as ChatRuntimeResponseCardData,
      );
      if (autoPreviewData) {
        return [
          {
            ...message,
            id: `${message.id}-auto-preview-${cardIndex}`,
            cards: [
              {
                ...card,
                data: autoPreviewData,
              },
            ],
          },
        ];
      }
    }
  }

  return null;
}

function mergeTaskRunDetailMessages(
  messages: IAgentScopeRuntimeWebUIMessage[],
): IAgentScopeRuntimeWebUIMessage[] {
  const firstMessage = messages[0];
  if (!firstMessage) {
    return [];
  }

  const nonResponseCards: RuntimeMessageCard[] = [];
  let firstResponseCard: RuntimeMessageCard | null = null;
  let firstResponseData: ChatRuntimeResponseCardData | null = null;
  const mergedOutput: ChatRuntimeResponseCardData["output"] = [];

  for (const message of messages) {
    for (const card of message.cards || []) {
      if (card.code !== "AgentScopeRuntimeResponseCard") {
        nonResponseCards.push(card);
        continue;
      }

      const responseData = card.data as ChatRuntimeResponseCardData;
      if (!firstResponseCard) {
        firstResponseCard = card;
        firstResponseData = responseData;
      }

      if (Array.isArray(responseData.output)) {
        mergedOutput.push(...responseData.output);
      }
    }
  }

  if (!firstResponseCard || !firstResponseData) {
    return messages;
  }

  return [
    {
      ...firstMessage,
      id: `${firstMessage.id}-merged-detail`,
      cards: [
        ...nonResponseCards,
        {
          ...firstResponseCard,
          data: {
            ...firstResponseData,
            output: mergedOutput,
          },
        },
      ],
    },
  ];
}

function NestedTaskRunMessages(props: {
  messages: IAgentScopeRuntimeWebUIMessage[];
  showFeedback?: boolean;
  chatId?: string | null;
  sessionId?: string | null;
  task?: ResponseFeedbackTaskMeta | null;
  feedbackLookup?: FeedbackLookupMap;
  loadingFeedback?: boolean;
  onFeedbackSaved?: (
    feedback: FeedbackRecord,
    response: ChatRuntimeResponseCardData,
  ) => void;
  onExternalApprovalResolved?: () => Promise<unknown> | unknown;
}) {
  return (
    <>
      {props.messages.map((message, messageIndex) => (
        <Fragment key={message.id}>
          {(message.cards || []).map((card, cardIndex) => {
            const key = `${message.id}-${card.id || card.code}-${cardIndex}`;
            if (card.code === "AgentScopeRuntimeRequestCard") {
              return (
                <RuntimeRequestCard
                  key={key}
                  data={card.data as ChatRuntimeRequestCardData}
                />
              );
            }
            if (card.code === "AgentScopeRuntimeResponseCard") {
              return (
                <RuntimeResponseCard
                  key={key}
                  data={card.data as ChatRuntimeResponseCardData}
                  isLast={
                    messageIndex === props.messages.length - 1 &&
                    cardIndex === (message.cards || []).length - 1
                  }
                />
              );
            }
            if (card.code === "ResponseFeedback") {
              const response = card.data as ChatRuntimeResponseCardData;
              return (
                <RuntimeResponseFeedbackCard
                  key={key}
                  chatId={props.chatId}
                  data={response}
                  existingFeedback={findFeedbackForResponse(
                    props.feedbackLookup,
                    response,
                  )}
                  loadingFeedback={props.loadingFeedback}
                  onFeedbackSaved={props.onFeedbackSaved}
                  sessionId={props.sessionId}
                  showFeedback={props.showFeedback}
                  task={props.task}
                />
              );
            }
            if (card.code === "ApprovalAction") {
              return (
                <ApprovalActionCard
                  key={key}
                  data={card.data as ChatApprovalActionCardData}
                  onExternalApprovalResolved={props.onExternalApprovalResolved}
                />
              );
            }
            return null;
          })}
        </Fragment>
      ))}
    </>
  );
}

export default function TaskRunGroupCard(props: {
  data: ChatTaskRunGroupCardData;
  chatId?: string | null;
  sessionId?: string | null;
  task?: ResponseFeedbackTaskMeta | null;
  feedbackLookup?: FeedbackLookupMap;
  loadingFeedback?: boolean;
  onFeedbackSaved?: (
    feedback: FeedbackRecord,
    response: ChatRuntimeResponseCardData,
  ) => void;
  onExternalApprovalResolved?: () => Promise<unknown> | unknown;
}) {
  const { data } = props;
  const [resultExpanded, setResultExpanded] = useState(
    !data.collapsedByDefault,
  );
  const [stepsExpanded, setStepsExpanded] = useState(false);
  // Prefer the last final result; only fall back to execution steps.
  const autoPreviewMessages =
    findAutoPreviewHtmlMessages(data.finalMessages) ||
    findAutoPreviewHtmlMessages(data.stepMessages);
  const finalMessages = autoPreviewMessages || data.finalMessages;
  const stepMessages = autoPreviewMessages
    ? mergeTaskRunDetailMessages([...data.stepMessages, ...data.finalMessages])
    : data.stepMessages;
  const hasSteps = stepMessages.length > 0;
  const taskName = data.taskName || `任务 ${data.runIndex + 1}`;
  const headerText = data.headerMeta?.timestamp
    ? `${taskName}，执行时间：${formatMessageTime(
        data.headerMeta.timestamp,
      )}，结果如下`
    : `${taskName}，结果如下`;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
        width: "100%",
        boxSizing: "border-box",
      }}
    >
      <div
        data-testid="task-run-divider"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          width: "100%",
          color: "rgba(0, 0, 0, 0.45)",
          fontSize: 12,
          boxSizing: "border-box",
        }}
      >
        <div style={{ flex: 1, borderTop: "1px solid rgba(0, 0, 0, 0.12)" }} />
        <span
          style={{
            textAlign: "center",
            whiteSpace: "nowrap",
            fontWeight: "bold",
          }}
        >
          {headerText}
        </span>
        {data.collapsedByDefault && (
          <button
            type="button"
            data-testid="task-run-result-toggle"
            onClick={() => setResultExpanded((prev) => !prev)}
            style={{
              border: "none",
              background: "transparent",
              padding: 0,
              color: "#1677ff",
              cursor: "pointer",
              fontSize: 12,
              whiteSpace: "nowrap",
            }}
          >
            {resultExpanded ? "收起历史" : "展开历史"}
          </button>
        )}
        <div style={{ flex: 1, borderTop: "1px solid rgba(0, 0, 0, 0.12)" }} />
      </div>
      {resultExpanded && (
        <FilePreviewPresentationProvider value="modal">
          <NestedTaskRunMessages
            chatId={props.chatId}
            messages={finalMessages}
            sessionId={props.sessionId}
            showFeedback
            task={props.task}
            feedbackLookup={props.feedbackLookup}
            loadingFeedback={props.loadingFeedback}
            onFeedbackSaved={props.onFeedbackSaved}
            onExternalApprovalResolved={props.onExternalApprovalResolved}
          />
        </FilePreviewPresentationProvider>
      )}
      {resultExpanded && hasSteps && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <button
            type="button"
            data-testid="task-run-toggle"
            onClick={() => setStepsExpanded((prev) => !prev)}
            style={{
              alignSelf: "flex-start",
              border: "none",
              background: "transparent",
              padding: 0,
              color: "#1677ff",
              cursor: "pointer",
              fontSize: 12,
              whiteSpace: "nowrap",
            }}
          >
            {stepsExpanded ? "收起步骤" : "查看步骤"}
          </button>
          {stepsExpanded && (
            <div
              data-testid="task-run-steps"
              style={{
                borderLeft: "2px solid rgba(22, 119, 255, 0.18)",
                paddingLeft: 16,
              }}
            >
              <FilePreviewPresentationProvider value="modal">
                <NestedTaskRunMessages
                  messages={stepMessages}
                  showFeedback={false}
                  feedbackLookup={props.feedbackLookup}
                  loadingFeedback={props.loadingFeedback}
                  onFeedbackSaved={props.onFeedbackSaved}
                  onExternalApprovalResolved={
                    props.onExternalApprovalResolved
                  }
                />
              </FilePreviewPresentationProvider>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
