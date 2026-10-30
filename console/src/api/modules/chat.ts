import { request } from "../request";
import { clearAuthToken, getApiUrl, getApiToken } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import {
  clearExternalToken,
  ensureValidToken,
  isExternalTokenEnabled,
} from "../externalToken";
import type {
  ExpertBindingView,
  ExpertBindingUpdate,
  ChatPage,
  ChatSpec,
  ChatCreateRequest,
  ChatCreateResponse,
  ChatHistory,
  ChatArchivePage,
  ChatDeleteResponse,
  ChatShareCreateResponse,
  ChatShareSnapshot,
  ChatShareOptions,
  Session,
  SubAgentRunCancelResponse,
  SubAgentRunSnapshot,
  GoalSnapshot,
} from "../types";
import type { ContextUsageSnapshot } from "../types/contextUsage";

/** Response from POST /console/upload. url = filename only; agent_id from header. */
export interface ChatUploadResponse {
  url: string;
  file_name: string;
  stored_name?: string;
}

export interface GeneratedFileItem {
  name: string;
  display_name: string;
  relative_path: string;
  file_url: string;
  size: number;
  modified_at: string;
  mime_type?: string | null;
  preview_type:
    | "image"
    | "video"
    | "audio"
    | "office"
    | "pdf"
    | "markdown"
    | "text"
    | "html"
    | "other";
  source: "generated" | "uploaded";
}

export interface GeneratedFilesResponse {
  files: GeneratedFileItem[];
}

export type FileManagerRoot =
  | "working"
  | "source_scope"
  | "upload"
  | "download"
  | "conversation"
  | "recycle";

export type FileManagerItemKind = "directory" | "file" | "symlink" | "special";

export interface FileManagerCapabilities {
  browse: boolean;
  read: boolean;
  upload: boolean;
  edit: boolean;
  download: boolean;
  archive: boolean;
}

export interface FileManagerItem {
  name: string;
  path: string;
  kind: FileManagerItemKind;
  size_bytes?: number | null;
  modified_at?: string | null;
  capabilities: FileManagerCapabilities;
  archive_item_id?: string | null;
  original_path?: string | null;
  archived_at?: string | null;
}

export interface FileManagerDirectoryListing {
  root: FileManagerRoot;
  path: string;
  items: FileManagerItem[];
  next_cursor: string | null;
  has_child_directory: boolean;
  first_child_directory: string | null;
  capabilities: FileManagerCapabilities;
}

export interface FileManagerTextPreview {
  path: string;
  size_bytes: number;
  is_text: boolean;
  content: string | null;
  is_truncated: boolean;
  editable: boolean;
  revision: string;
}

export interface FileManagerPathParams {
  root: FileManagerRoot;
  path: string;
}

export interface FileManagerListDirectoryParams extends FileManagerPathParams {
  cursor?: string | null;
  query?: string;
}

export interface FileManagerSaveTextParams extends FileManagerPathParams {
  content: string;
  revision: string;
}

export interface FileManagerRecycleMutation {
  archive_item_id: string;
  original_path: string;
}

export interface FileManagerDownload {
  blob: Blob;
  filename: string;
}

const FILES_PREVIEW = "/files/preview";

function fileManagerQuery(params: object) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (typeof value === "string") query.set(key, value);
  }
  return query.toString();
}

function downloadFilename(contentDisposition: string | null): string {
  const utf8Filename = contentDisposition?.match(
    /filename\*=UTF-8''([^;]+)/i,
  )?.[1];
  if (utf8Filename) {
    try {
      return decodeURIComponent(utf8Filename);
    } catch {
      // Fall through to a safe generic name when a malformed header arrives.
    }
  }
  const quotedFilename = contentDisposition?.match(/filename="([^"]+)"/i)?.[1];
  return quotedFilename || "download";
}

async function fileManagerDownload(
  params: FileManagerPathParams,
): Promise<FileManagerDownload> {
  const url = getApiUrl(
    `/console/file-manager/files/download?${fileManagerQuery(params)}`,
  );
  const fetchDownload = () =>
    fetch(url, { method: "GET", headers: buildAuthHeaders() });

  let response = await fetchDownload();
  if (response.status === 401 && isExternalTokenEnabled()) {
    try {
      await ensureValidToken(true);
    } catch {
      clearExternalToken();
      throw new Error("登录状态已失效，请刷新页面或重新进入系统后再试");
    }
    response = await fetchDownload();
    if (response.status === 401) {
      clearExternalToken();
      throw new Error("登录状态已失效，请刷新页面或重新进入系统后再试");
    }
  } else if (response.status === 401) {
    clearAuthToken();
    throw new Error("认证已失效，请刷新页面或重新进入系统后再试");
  }

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(
      `File download failed: ${response.status} ${response.statusText}${
        text ? ` - ${text}` : ""
      }`,
    );
  }

  return {
    blob: await response.blob(),
    filename: downloadFilename(response.headers.get("Content-Disposition")),
  };
}

export const chatApi = {
  /** Upload a file for chat attachment. Returns URL path for content. */
  uploadFile: async (file: File): Promise<ChatUploadResponse> => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(getApiUrl("/console/upload"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(
        `Upload failed: ${response.status} ${response.statusText}${
          text ? ` - ${text}` : ""
        }`,
      );
    }
    return response.json();
  },

  filePreviewUrl: (filename: string): string => {
    if (!filename) return "";
    if (filename.startsWith("http://") || filename.startsWith("https://"))
      return filename;
    const path = `${FILES_PREVIEW}/${filename.replace(/^\/+/, "")}`;
    const url = getApiUrl(path);

    const token = getApiToken();
    if (token) {
      return `${url}?token=${encodeURIComponent(token)}`;
    }

    return url;
  },
  listChats: (params?: { user_id?: string; channel?: string }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    const query = searchParams.toString();
    return request<ChatSpec[]>(`/chats${query ? `?${query}` : ""}`);
  },

  listChatsPage: (params: {
    page_size: number;
    page?: number;
    cursor?: string | null;
    user_id?: string;
    channel?: string;
  }) => {
    const searchParams = new URLSearchParams({
      page_size: String(params.page_size),
    });
    if (params.page !== undefined) {
      searchParams.append("page", String(params.page));
    }
    if (params.cursor !== undefined) {
      searchParams.append("cursor", params.cursor || "");
    }
    if (params.user_id) searchParams.append("user_id", params.user_id);
    if (params.channel) searchParams.append("channel", params.channel);
    return request<ChatPage>(`/chats?${searchParams.toString()}`);
  },

  createChat: (chat: ChatCreateRequest) =>
    request<ChatCreateResponse>("/chats", {
      method: "POST",
      body: JSON.stringify(chat),
    }),

  getChat: (chatId: string) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(chatId)}`),

  getExpertBinding: (chatId: string) =>
    request<ExpertBindingView>(
      `/chats/${encodeURIComponent(chatId)}/expert-binding`,
    ),

  updateExpertBinding: (chatId: string, update: ExpertBindingUpdate) =>
    request<ExpertBindingView>(
      `/chats/${encodeURIComponent(chatId)}/expert-binding`,
      {
        method: "PUT",
        body: JSON.stringify(update),
      },
    ),

  createChatShare: (chatId: string, turnIds: string[]) =>
    request<ChatShareCreateResponse>(
      `/chats/${encodeURIComponent(chatId)}/share`,
      { method: "POST", body: JSON.stringify({ turn_ids: turnIds }) },
    ),

  getChatShareOptions: (chatId: string) =>
    request<ChatShareOptions>(
      `/chats/${encodeURIComponent(chatId)}/share-options`,
    ),

  getChatShare: (token: string) =>
    request<ChatShareSnapshot>(`/chat-shares/${encodeURIComponent(token)}`),

  getContextUsage: (chatId: string) =>
    request<ContextUsageSnapshot>(
      `/chats/${encodeURIComponent(chatId)}/context-usage`,
    ),

  getChatHistory: (chatId: string, before?: string | null, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (before) params.set("before", before);
    return request<ChatArchivePage>(
      `/chats/${encodeURIComponent(chatId)}/history?${params.toString()}`,
    );
  },

  updateChat: (chatId: string, chat: Partial<ChatSpec>) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}`, {
      method: "PUT",
      body: JSON.stringify(chat),
    }),

  deleteChat: (chatId: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(chatId)}`, {
      method: "DELETE",
    }),

  batchDeleteChats: (chatIds: string[]) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(chatIds),
      },
    ),

  listGeneratedFiles: (
    sort: "asc" | "desc" = "desc",
    source: "all" | "generated" | "uploaded" = "all",
  ) =>
    request<GeneratedFilesResponse>(
      `/console/generated-files?sort=${encodeURIComponent(
        sort,
      )}&source=${encodeURIComponent(source)}`,
    ),

  fileManager: {
    listDirectory: (params: FileManagerListDirectoryParams) =>
      request<FileManagerDirectoryListing>(
        `/console/file-manager/directories?${fileManagerQuery({
          root: params.root,
          path: params.path,
          cursor: params.cursor,
          q: params.query,
        })}`,
      ),

    readFile: (params: FileManagerPathParams) =>
      request<FileManagerTextPreview>(
        `/console/file-manager/files/read?${fileManagerQuery(params)}`,
      ),

    saveText: (params: FileManagerSaveTextParams) =>
      request<FileManagerTextPreview>("/console/file-manager/files/text", {
        method: "PUT",
        body: JSON.stringify(params),
      }),

    upload: (params: FileManagerPathParams, file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      return request<FileManagerItem>(
        `/console/file-manager/files/upload?${fileManagerQuery(params)}`,
        { method: "POST", body: formData },
      );
    },

    downloadFile: fileManagerDownload,

    archive: (params: FileManagerPathParams) =>
      request<FileManagerRecycleMutation>(
        `/console/file-manager/files?${fileManagerQuery(params)}`,
        { method: "DELETE" },
      ),

    deleteDirectory: (params: FileManagerPathParams) =>
      request<void>(
        `/console/file-manager/directories?${fileManagerQuery(params)}`,
        { method: "DELETE" },
      ),

    restore: (archiveItemId: string) =>
      request<FileManagerRecycleMutation>(
        `/console/file-manager/recycle/${encodeURIComponent(
          archiveItemId,
        )}/restore`,
        { method: "POST" },
      ),

    purge: (archiveItemId: string) =>
      request<FileManagerRecycleMutation>(
        `/console/file-manager/recycle/${encodeURIComponent(archiveItemId)}`,
        { method: "DELETE" },
      ),
  },

  stopChat: (
    chatId: string,
    msgid?: string | null,
    sessionId?: string | null,
  ) => {
    const params = new URLSearchParams({ chat_id: chatId });
    if (msgid) params.set("msgid", msgid);
    if (sessionId) params.set("session_id", sessionId);
    return request<void>(`/console/chat/stop?${params.toString()}`, {
      method: "POST",
    });
  },

  getSubAgentRuns: (chatId: string, signal?: AbortSignal) =>
    request<SubAgentRunSnapshot>(
      `/subagents/runs?chat_id=${encodeURIComponent(chatId)}`,
      signal ? { signal } : undefined,
    ),

  cancelSubAgentRun: (chatId: string, runId: string) =>
    request<SubAgentRunCancelResponse>(
      `/subagents/runs/${encodeURIComponent(runId)}/cancel`,
      {
        method: "POST",
        body: JSON.stringify({ chat_id: chatId }),
      },
    ),

  getRecentGoal: (chatId: string) =>
    request<GoalSnapshot | null>(
      `/goals/recent?chat_id=${encodeURIComponent(chatId)}`,
    ),

  createGoal: (
    chatId: string,
    contract: GoalSnapshot["contract"],
    enableSubagents = false,
  ) =>
    request<GoalSnapshot>("/goals", {
      method: "POST",
      body: JSON.stringify({
        chat_id: chatId,
        contract,
        enable_subagents: enableSubagents,
      }),
    }),

  pauseGoal: (goalId: string, chatId: string) =>
    request<GoalSnapshot>(
      `/goals/${encodeURIComponent(goalId)}/pause?chat_id=${encodeURIComponent(
        chatId,
      )}`,
      {
        method: "POST",
      },
    ),

  resumeGoal: (goalId: string, chatId: string) =>
    request<GoalSnapshot>(
      `/goals/${encodeURIComponent(goalId)}/resume?chat_id=${encodeURIComponent(
        chatId,
      )}`,
      {
        method: "POST",
      },
    ),

  cancelGoal: (goalId: string, chatId: string) =>
    request<GoalSnapshot>(
      `/goals/${encodeURIComponent(goalId)}/cancel?chat_id=${encodeURIComponent(
        chatId,
      )}`,
      {
        method: "POST",
      },
    ),

  editGoal: (
    goalId: string,
    chatId: string,
    contract: GoalSnapshot["contract"],
  ) =>
    request<GoalSnapshot>(
      `/goals/${encodeURIComponent(goalId)}/edit?chat_id=${encodeURIComponent(
        chatId,
      )}`,
      {
        method: "POST",
        body: JSON.stringify({ contract }),
      },
    ),

  enqueueGoalSteering: (
    goalId: string,
    chatId: string,
    content: string,
    enableSubagents = false,
  ) =>
    request<GoalSnapshot>(
      `/goals/${encodeURIComponent(
        goalId,
      )}/steering?chat_id=${encodeURIComponent(chatId)}`,
      {
        method: "POST",
        body: JSON.stringify({ content, enable_subagents: enableSubagents }),
      },
    ),
};

export const sessionApi = {
  listSessions: (params?: { user_id?: string; channel?: string }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    const query = searchParams.toString();
    return request<Session[]>(`/chats${query ? `?${query}` : ""}`);
  },

  getSession: (sessionId: string) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(sessionId)}`),

  deleteSession: (sessionId: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),

  createSession: (session: Partial<Session>) =>
    request<Session>("/chats", {
      method: "POST",
      body: JSON.stringify(session),
    }),

  updateSession: (sessionId: string, session: Partial<Session>) =>
    request<Session>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "PUT",
      body: JSON.stringify(session),
    }),

  batchDeleteSessions: (sessionIds: string[]) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(sessionIds),
      },
    ),
};
