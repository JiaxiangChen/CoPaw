import {
  extractDecodedFileNameFromUrl,
  getFileType,
  isAutoPreviewHtmlLink,
  isDynamicRenderHtmlLink,
} from "./FilePreviewModal/fileUtils";
import type { IAgentScopeRuntimeWebUIMessage } from "./AgentScopeRuntimeWebUI/core/types/IMessages";
import type {
  ChatRuntimeResponseCardData,
  ChatTaskRunGroupCardData,
} from "@/pages/Chat/messageMeta";

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
  allowDynamicRender = false,
): AutoPreviewHtmlMatch | null {
  const url = value.replace(TRAILING_URL_PUNCTUATION_PATTERN, "").trim();
  const resolvedFileName = fileName || extractDecodedFileNameFromUrl(url, "");
  const isPreviewableDynamicRender =
    allowDynamicRender &&
    isDynamicRenderHtmlLink(url) &&
    getFileType(resolvedFileName) === "previewable";
  if (
    !isValidAutoPreviewUrlCandidate(url) ||
    !(isAutoPreviewHtmlLink(url, fileName) || isPreviewableDynamicRender)
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
  allowDynamicRender = false,
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
          allowDynamicRender,
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
      const match = findAutoPreviewHtmlValue(
        item,
        depth + 1,
        allowDynamicRender,
      );
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
    const directMatch = toAutoPreviewHtmlMatch(
      url,
      fileName,
      allowDynamicRender,
    );
    if (directMatch) return directMatch;

    const embeddedMatch = findAutoPreviewHtmlTextMatch(url);
    if (embeddedMatch) return embeddedMatch;
  }

  for (const key of ["path", "content", "data", "output", "text", "value"]) {
    const match = findAutoPreviewHtmlValue(
      value[key],
      depth + 1,
      allowDynamicRender,
    );
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

type AutoPreviewHtmlResponseSelection = {
  contentItem?: unknown;
  match: AutoPreviewHtmlMatch;
  outputMessage: Record<string, unknown>;
};

function findAutoPreviewHtmlResponseSelection(
  data: ChatRuntimeResponseCardData,
): AutoPreviewHtmlResponseSelection | null {
  const output = Array.isArray(data.output) ? data.output : [];

  for (const outputMessage of [...output].reverse()) {
    if (!isRecord(outputMessage)) {
      continue;
    }

    const content = Array.isArray(outputMessage.content)
      ? outputMessage.content
      : [];
    for (const contentItem of [...content].reverse()) {
      const match = findAutoPreviewHtmlValue(
        contentItem,
        0,
        shouldReuseAutoPreviewContentItem(contentItem),
      );
      if (match) {
        return { contentItem, match, outputMessage };
      }
    }

    const match = findAutoPreviewHtmlValue(outputMessage);
    if (match) {
      return { match, outputMessage };
    }
  }

  return null;
}

function buildAutoPreviewHtmlResponseData(
  data: ChatRuntimeResponseCardData,
  selection: AutoPreviewHtmlResponseSelection,
): ChatRuntimeResponseCardData {
  const { contentItem, match, outputMessage } = selection;
  const previewOutputMessage = shouldReuseAutoPreviewContentItem(contentItem)
    ? {
        ...outputMessage,
        role: "assistant",
        type: "message",
        content: [contentItem],
      }
    : buildAutoPreviewOutputMessage(outputMessage, match);
  return {
    ...data,
    output: [previewOutputMessage] as ChatRuntimeResponseCardData["output"],
  };
}

type AutoPreviewHtmlMessageSelection = {
  cardIndex: number;
  message: IAgentScopeRuntimeWebUIMessage;
  responseData: ChatRuntimeResponseCardData;
  responseSelection: AutoPreviewHtmlResponseSelection;
};

function findAutoPreviewHtmlMessageSelection(
  messages: IAgentScopeRuntimeWebUIMessage[],
): AutoPreviewHtmlMessageSelection | null {
  for (const message of [...messages].reverse()) {
    const cards = message.cards || [];
    for (let cardIndex = cards.length - 1; cardIndex >= 0; cardIndex -= 1) {
      const card = cards[cardIndex];
      if (card.code !== "AgentScopeRuntimeResponseCard") continue;
      const responseData = card.data as ChatRuntimeResponseCardData;
      const responseSelection =
        findAutoPreviewHtmlResponseSelection(responseData);
      if (responseSelection) {
        return { cardIndex, message, responseData, responseSelection };
      }
    }
  }
  return null;
}

export function findAutoPreviewHtmlMessages(
  messages: IAgentScopeRuntimeWebUIMessage[],
): IAgentScopeRuntimeWebUIMessage[] | null {
  const selection = findAutoPreviewHtmlMessageSelection(messages);
  if (!selection) return null;
  const { cardIndex, message, responseData, responseSelection } = selection;
  const card = message.cards?.[cardIndex];
  if (!card) return null;
  return [
    {
      ...message,
      id: `${message.id}-auto-preview-${cardIndex}`,
      cards: [
        {
          ...card,
          data: buildAutoPreviewHtmlResponseData(
            responseData,
            responseSelection,
          ),
        },
      ],
    },
  ];
}

/** Messages arrive oldest first; presentation and effect order are irrelevant. */
export function findLatestAutoPreviewUrl(
  messages: IAgentScopeRuntimeWebUIMessage[],
): string | null {
  for (const message of [...messages].reverse()) {
    if (message.role === "user") continue;
    for (const card of [...(message.cards || [])].reverse()) {
      if (card.code === "TaskRunGroupCard") {
        const data = card.data as ChatTaskRunGroupCardData;
        if (data.collapsedByDefault) continue;
        const selected =
          findAutoPreviewHtmlMessageSelection(data.finalMessages) ||
          findAutoPreviewHtmlMessageSelection(data.stepMessages);
        return selected?.responseSelection.match.url || null;
      }
      if (card.code !== "AgentScopeRuntimeResponseCard") continue;
      const selected = findAutoPreviewHtmlResponseSelection(
        card.data as ChatRuntimeResponseCardData,
      );
      if (selected) return selected.match.url;
    }
  }
  return null;
}
