import { beforeEach, describe, expect, it, vi } from "vitest";
import { convertArchivedPage, SessionApi } from "./index";

const apiMocks = vi.hoisted(() => ({
  listChats: vi.fn(),
  listChatsPage: vi.fn(),
  getChat: vi.fn(),
  deleteChat: vi.fn(),
}));

const cronJobApiMocks = vi.hoisted(() => ({
  listCronJobs: vi.fn(),
}));

vi.mock("../../../api", () => ({
  __esModule: true,
  default: {
    listChats: apiMocks.listChats,
    listChatsPage: apiMocks.listChatsPage,
    getChat: apiMocks.getChat,
    deleteChat: apiMocks.deleteChat,
  },
}));

vi.mock("../../../api/modules/cronjob", () => ({
  cronJobApi: {
    listCronJobs: cronJobApiMocks.listCronJobs,
  },
}));

vi.mock("../../../utils/identity", () => ({
  getUserId: vi.fn(() => "user-1"),
  getChannel: vi.fn(() => "console"),
  getUserIdWithoutWindow: vi.fn((value?: string) => value || "user-1"),
  getChannelWithoutWindow: vi.fn((value?: string) => value || "console"),
}));

describe("SessionApi identity mapping", () => {
  beforeEach(() => {
    apiMocks.listChats.mockReset();
    apiMocks.listChatsPage.mockReset();
    apiMocks.getChat.mockReset();
    apiMocks.deleteChat.mockReset();
    cronJobApiMocks.listCronJobs.mockReset();
    cronJobApiMocks.listCronJobs.mockResolvedValue([]);
    apiMocks.listChats.mockResolvedValue([]);
    apiMocks.listChatsPage.mockImplementation(async () => {
      const items = await apiMocks.listChats();
      return {
        items,
        total: items.length,
        page: 1,
        page_size: 100,
        has_more: false,
      };
    });
    sessionStorage.clear();
    const runtimeWindow = window as Window & {
      currentSessionId?: string;
      currentUserId?: string;
      currentChannel?: string;
      __env__?: {
        chatSessionPageSize?: number | string;
      };
    };
    runtimeWindow.currentSessionId = undefined;
    runtimeWindow.currentUserId = undefined;
    runtimeWindow.currentChannel = undefined;
    runtimeWindow.__env__ = {};
  });

  it("converts a terminal recovery snapshot into chat cards", () => {
    const sessionApi = new SessionApi();
    const messages = sessionApi.applyChatSnapshot("chat-1", {
      messages: [
        {
          id: "user-1",
          role: "user",
          content: [{ type: "text", text: "hello" }],
        },
      ],
    });

    expect(messages).toHaveLength(1);
    expect(messages?.[0]).toMatchObject({
      id: "user-1",
      role: "user",
      history: true,
      cards: [{ code: "AgentScopeRuntimeRequestCard" }],
    });
  });

  it("keeps attachments when rebuilding a user message from chat history", () => {
    const sessionApi = new SessionApi();
    const messages = sessionApi.applyChatSnapshot("chat-1", {
      messages: [
        {
          id: "user-with-attachment",
          role: "user",
          content: [
            { type: "text", text: "请分析附件" },
            {
              type: "file",
              file_url: "https://files.example.test/report.pdf",
              filename: "报告.pdf",
            },
            {
              type: "image",
              image_url: "https://files.example.test/screenshot.png",
            },
          ],
        },
      ],
    });

    expect(messages?.[0].cards?.[0].data.input[0].content).toMatchObject([
      { type: "text", text: "请分析附件" },
      {
        type: "file",
        file_url: "https://files.example.test/report.pdf",
        file_name: "报告.pdf",
      },
      {
        type: "image",
        image_url: "https://files.example.test/screenshot.png",
      },
    ]);
  });

  it("keeps the logical session id stable after the first reply resolves a real chat id", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "new chat",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    expect(logicalSessionId).toBeTruthy();

    const resolved = vi.fn();
    sessionApi.onSessionIdResolved = resolved;

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "new chat",
        session_id: logicalSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);
    apiMocks.getChat.mockResolvedValue({
      id: "chat-real-1",
      status: "running",
      messages: [],
    });

    await sessionApi.updateSession({
      id: logicalSessionId!,
      name: "new chat",
    });

    expect(resolved).toHaveBeenCalledWith(logicalSessionId, "chat-real-1");
    expect(sessionApi.getLogicalSessionId(logicalSessionId!)).toBe(
      logicalSessionId,
    );
    expect(sessionApi.getChatIdForSession(logicalSessionId!)).toBe(
      "chat-real-1",
    );
    expect(
      (window as Window & { currentSessionId?: string }).currentSessionId,
    ).toBe(logicalSessionId);
  });

  it("shows a pending local session in the history list immediately after creation", async () => {
    const sessionApi = new SessionApi();

    const list = await sessionApi.createSession({
      name: "new chat",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    expect(logicalSessionId).toBeTruthy();
    expect(list).toHaveLength(1);
    expect(list[0]?.id).toBe(logicalSessionId);
    expect(list[0]?.name).toBe("new chat");
  });

  it("loads a newly created local session without refreshing the chat list once", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "new chat",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    expect(logicalSessionId).toBeTruthy();

    const session = await sessionApi.getSession(logicalSessionId!);

    expect(apiMocks.listChats).not.toHaveBeenCalled();
    expect(session.id).toBe(logicalSessionId);
    expect(session.name).toBe("new chat");
  });

  it("keeps multiple pending local sessions when backend persistence has not caught up yet", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "chat A",
      messages: [],
    });
    const firstSessionId = sessionApi.getPendingSessionId();

    await sessionApi.createSession({
      name: "chat B",
      messages: [],
    });
    const secondSessionId = sessionApi.getPendingSessionId();

    expect(firstSessionId).toBeTruthy();
    expect(secondSessionId).toBeTruthy();
    expect(secondSessionId).not.toBe(firstSessionId);

    apiMocks.listChats.mockResolvedValue([]);

    const list = await sessionApi.getSessionList();

    expect(list.map((session) => session.id)).toEqual(
      expect.arrayContaining([firstSessionId, secondSessionId]),
    );
  });

  it("only notifies resolution for the active pending session when multiple local sessions resolve together", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "chat A",
      messages: [],
    });
    const firstSessionId = sessionApi.getPendingSessionId();

    await sessionApi.createSession({
      name: "chat B",
      messages: [],
    });
    const secondSessionId = sessionApi.getPendingSessionId();

    expect(firstSessionId).toBeTruthy();
    expect(secondSessionId).toBeTruthy();
    expect(secondSessionId).not.toBe(firstSessionId);

    const runtimeWindow = window as Window & {
      currentSessionId?: string;
    };
    runtimeWindow.currentSessionId = secondSessionId!;

    const resolved = vi.fn();
    sessionApi.onSessionIdResolved = resolved;

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-a",
        name: "chat A",
        session_id: firstSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-04-22T00:00:00Z",
      },
      {
        id: "chat-real-b",
        name: "chat B",
        session_id: secondSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-04-22T00:00:01Z",
      },
    ]);

    await sessionApi.getSessionList();

    expect(resolved).toHaveBeenCalledTimes(1);
    expect(resolved).toHaveBeenCalledWith(secondSessionId, "chat-real-b");
    expect(sessionApi.getChatIdForSession(firstSessionId!)).toBe("chat-real-a");
    expect(sessionApi.getChatIdForSession(secondSessionId!)).toBe(
      "chat-real-b",
    );
  });

  it("keeps pending session messages accessible before backend persistence catches up", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "new chat",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    const localMessages = [
      {
        id: "user-msg-1",
        role: "user" as const,
        cards: [],
      },
    ];

    apiMocks.listChats.mockResolvedValue([]);

    await sessionApi.updateSession({
      id: logicalSessionId!,
      messages: localMessages,
    });

    const session = await sessionApi.getSession(logicalSessionId!);
    const list = await sessionApi.getSessionList();

    expect(session.messages).toEqual(localMessages);
    expect(list.some((item) => item.id === logicalSessionId)).toBe(true);
  });

  it("preserves local pending messages when the backend chat id resolves before the first frame", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "new chat",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    const localMessages = [
      {
        id: "user-msg-1",
        role: "user" as const,
        cards: [],
      },
    ];

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "new chat",
        session_id: logicalSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "running",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);
    apiMocks.getChat.mockResolvedValue({
      id: "chat-real-1",
      status: "running",
      messages: [],
    });

    await sessionApi.updateSession({
      id: logicalSessionId!,
      messages: localMessages,
      generating: true,
    });

    const session = await sessionApi.getSession(logicalSessionId!);
    const list = await sessionApi.getSessionList();

    expect(session.messages).toEqual(localMessages);
    expect(session.generating).toBe(true);
    expect(list[0]?.id).toBe(logicalSessionId);
    expect(list[0]?.messages).toEqual(localMessages);
    expect(list[0]?.generating).toBe(true);
  });

  it("refreshes backend state before returning an unresolved local timestamp session", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "new chat",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    expect(logicalSessionId).toBeTruthy();

    await sessionApi.getSession(logicalSessionId!);
    apiMocks.listChats.mockClear();

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "new chat",
        session_id: logicalSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "running",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);
    apiMocks.getChat.mockResolvedValue({
      id: "chat-real-1",
      status: "running",
      messages: [],
    });

    const session = await sessionApi.getSession(logicalSessionId!);

    expect(apiMocks.listChats).toHaveBeenCalled();
    expect(apiMocks.getChat).toHaveBeenCalledWith("chat-real-1");
    expect(session.generating).toBe(true);
    expect(sessionApi.getChatIdForSession(logicalSessionId!)).toBe(
      "chat-real-1",
    );
  });

  it("clears stale local generating when the resolved backend chat is idle", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "new chat",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    const localMessages = [
      {
        id: "user-msg-1",
        role: "user" as const,
        cards: [],
      },
    ];

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "new chat",
        session_id: logicalSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);
    apiMocks.getChat.mockResolvedValue({
      id: "chat-real-1",
      status: "idle",
      messages: [],
    });

    await sessionApi.updateSession({
      id: logicalSessionId!,
      messages: localMessages,
      generating: true,
    });

    const session = await sessionApi.getSession(logicalSessionId!);

    expect(session.messages).toEqual(localMessages);
    expect(session.generating).toBe(false);
  });

  it("patches the last user message back into a resolved running session when backend history only has partial assistant output", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "new chat",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    expect(logicalSessionId).toBeTruthy();

    sessionApi.setLastUserMessage(logicalSessionId!, "hello from user");

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "new chat",
        session_id: logicalSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "running",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);
    apiMocks.getChat.mockResolvedValue({
      id: "chat-real-1",
      status: "running",
      messages: [
        {
          id: "assistant-msg-1",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "partial reply" }],
          timestamp: "2026-04-22T00:00:01Z",
          metadata: {},
        },
      ],
    });

    await sessionApi.updateSession({
      id: logicalSessionId!,
      name: "new chat",
      generating: true,
    });

    const session = await sessionApi.getSession(logicalSessionId!);

    expect(session.generating).toBe(true);
    expect(session.messages).toHaveLength(2);
    expect(session.messages[0]).toMatchObject({
      role: "assistant",
    });
    expect(session.messages[1]).toMatchObject({
      role: "user",
      cards: [
        {
          code: "AgentScopeRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                content: [{ type: "text", text: "hello from user" }],
              },
            ],
          },
        },
      ],
    });
  });

  it("collapses historical scheduled text task results in task sessions", async () => {
    const sessionApi = new SessionApi();

    apiMocks.listChats.mockResolvedValue([
      {
        id: "task-chat-1",
        name: "daily text task",
        session_id: "task-session-1",
        user_id: "user-1",
        channel: "console",
        meta: { session_kind: "task" },
        status: "idle",
        created_at: "2026-05-21T00:00:00Z",
      },
    ]);
    apiMocks.getChat.mockResolvedValue({
      id: "task-chat-1",
      status: "idle",
      messages: [
        {
          id: "task-result-old",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "old result" }],
          timestamp: "2026-05-21T08:00:00Z",
          metadata: { cron_task: true },
        },
        {
          id: "task-result-new",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "new result" }],
          timestamp: "2026-05-21T09:00:00Z",
          metadata: { cron_task: true },
        },
      ],
    });

    await sessionApi.getSessionList();
    const session = await sessionApi.getSession("task-chat-1");
    const oldCard = session.messages[0]?.cards?.[0];
    const newCard = session.messages[1]?.cards?.[0];
    const oldData = oldCard?.data as any;
    const newData = newCard?.data as any;

    expect(session.messages).toHaveLength(2);
    expect(oldCard?.code).toBe("TaskRunGroupCard");
    expect(newCard?.code).toBe("TaskRunGroupCard");
    expect(oldData.collapsedByDefault).toBe(true);
    expect(newData.collapsedByDefault).toBe(false);
    expect(JSON.stringify(oldData)).toContain("old result");
    expect(JSON.stringify(newData)).toContain("new result");
  });

  it("collapses historical scheduled agent task runs in task sessions", async () => {
    const sessionApi = new SessionApi();

    apiMocks.listChats.mockResolvedValue([
      {
        id: "task-chat-1",
        name: "daily agent task",
        session_id: "task-session-1",
        user_id: "user-1",
        channel: "console",
        meta: { session_kind: "task" },
        status: "idle",
        created_at: "2026-05-21T00:00:00Z",
      },
    ]);
    apiMocks.getChat.mockResolvedValue({
      id: "task-chat-1",
      status: "idle",
      messages: [
        {
          id: "run-1-step",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "old step" }],
          timestamp: "2026-05-21T08:00:00Z",
          metadata: {
            task_run_id: "run-1",
            task_run_index: 0,
            task_run_section: "step",
          },
        },
        {
          id: "run-1-final",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "old final" }],
          timestamp: "2026-05-21T08:01:00Z",
          metadata: {
            task_run_id: "run-1",
            task_run_index: 0,
            task_run_section: "final",
          },
        },
        {
          id: "run-2-step",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "new step" }],
          timestamp: "2026-05-21T09:00:00Z",
          metadata: {
            task_run_id: "run-2",
            task_run_index: 1,
            task_run_section: "step",
          },
        },
        {
          id: "run-2-final",
          role: "assistant",
          type: "message",
          content: [{ type: "text", text: "new final" }],
          timestamp: "2026-05-21T09:01:00Z",
          metadata: {
            task_run_id: "run-2",
            task_run_index: 1,
            task_run_section: "final",
          },
        },
      ],
    });

    await sessionApi.getSessionList();
    const session = await sessionApi.getSession("task-chat-1");
    const card = session.messages[0]?.cards?.[0];
    const oldData = card?.data as any;
    const newCard = session.messages[1]?.cards?.[0];
    const newData = newCard?.data as any;

    expect(session.messages).toHaveLength(2);
    expect(card?.code).toBe("TaskRunGroupCard");
    expect(newCard?.code).toBe("TaskRunGroupCard");
    expect(oldData.runId).toBe("run-1");
    expect(newData.runId).toBe("run-2");
    expect(oldData.collapsedByDefault).toBe(true);
    expect(newData.collapsedByDefault).toBe(false);
    expect(JSON.stringify(oldData)).toContain("old final");
    expect(JSON.stringify(newData)).toContain("new final");
  });

  it("does not treat a persisted logical session id as a unique backend chat id", async () => {
    const sessionApi = new SessionApi();

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "persisted chat",
        session_id: "channel:user-1",
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "running",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);

    await sessionApi.getSessionList();

    expect(sessionApi.getChatIdForSession("channel:user-1")).toBeNull();
  });

  it("resolves a persisted local timestamp session id to its backend chat id", async () => {
    const sessionApi = new SessionApi();

    apiMocks.listChats.mockResolvedValue([
      {
        id: "3ec62b2e-c427-4778-bbab-f56188c602c4",
        name: "running chat",
        session_id: "1777001065201000",
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "running",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);

    await sessionApi.getSessionList();

    expect(sessionApi.getChatIdForSession("1777001065201000")).toBe(
      "3ec62b2e-c427-4778-bbab-f56188c602c4",
    );
  });

  it("does not resolve a logical session id to the first persisted chat when multiple chats share it", async () => {
    const sessionApi = new SessionApi();

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "older chat",
        session_id: "channel:user-1",
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-04-21T00:00:00Z",
      },
      {
        id: "chat-real-2",
        name: "newer chat",
        session_id: "channel:user-1",
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "running",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);

    await sessionApi.getSessionList();

    expect(sessionApi.getChatIdForSession("channel:user-1")).toBeNull();
  });

  it("clears temp-to-real mappings when deleting the persisted backend chat", async () => {
    const sessionApi = new SessionApi();

    sessionStorage.setItem(
      "copaw_resolved_chat_ids",
      JSON.stringify({
        temp_123: "chat-real-1",
      }),
    );
    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "persisted chat",
        session_id: "channel:user-1",
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-04-22T00:00:00Z",
      },
    ]);
    apiMocks.deleteChat.mockResolvedValue({
      success: true,
      chat_id: "chat-real-1",
    });

    await sessionApi.getSessionList();
    await sessionApi.removeSession({ id: "chat-real-1" });

    expect(apiMocks.deleteChat).toHaveBeenCalledWith("chat-real-1");
    expect(sessionStorage.getItem("copaw_resolved_chat_ids")).toBe("{}");
  });

  it("loads the first bounded chat page in backend order", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage.mockResolvedValue({
      items: [
        {
          id: "chat-newest",
          name: "newest",
          session_id: "session-newest",
          user_id: "user-1",
          channel: "console",
          meta: {},
          status: "idle",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          id: "chat-older",
          name: "older",
          session_id: "session-older",
          user_id: "user-1",
          channel: "console",
          meta: {},
          status: "idle",
          created_at: "2026-06-09T00:00:00Z",
        },
      ],
      total: 3,
      page: 1,
      page_size: 100,
      has_more: true,
    });

    const list = await sessionApi.getSessionList();

    expect(apiMocks.listChatsPage).toHaveBeenCalledWith({
      page_size: 100,
      cursor: null,
    });
    expect(list.map((session) => session.id)).toEqual([
      "chat-newest",
      "chat-older",
    ]);
    expect(sessionApi.hasMoreSessions()).toBe(true);
    expect(sessionApi.getSessionTotal()).toBe(3);
  });

  it("transforms only one page from a large chat history", async () => {
    const sessionApi = new SessionApi();
    const chats = Array.from({ length: 1000 }, (_, index) => ({
      id: `chat-${String(999 - index).padStart(4, "0")}`,
      name: `chat ${index}`,
      session_id: `session-${index}`,
      user_id: "user-1",
      channel: "console",
      meta: {},
      status: "idle",
      created_at: "2026-06-10T00:00:00Z",
    }));
    apiMocks.listChatsPage.mockResolvedValue({
      items: chats.slice(0, 50),
      total: chats.length,
      page: 1,
      page_size: 100,
      has_more: true,
    });

    const list = await sessionApi.getSessionList();

    expect(list).toHaveLength(50);
    expect(list[0].id).toBe("chat-0999");
    expect(list[49].id).toBe("chat-0950");
    expect(apiMocks.listChats).not.toHaveBeenCalled();
    expect(sessionApi.hasMoreSessions()).toBe(true);
  });

  it("appends the next page once and removes duplicate chat ids", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-3",
            name: "three",
            session_id: "session-3",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-10T00:00:00Z",
          },
          {
            id: "chat-2",
            name: "two",
            session_id: "session-2",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-09T00:00:00Z",
          },
        ],
        total: 3,
        page: 1,
        page_size: 100,
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-2",
            name: "two",
            session_id: "session-2",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-09T00:00:00Z",
          },
          {
            id: "chat-1",
            name: "one",
            session_id: "session-1",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-08T00:00:00Z",
          },
        ],
        total: 3,
        page: 2,
        page_size: 100,
        has_more: false,
      });

    await sessionApi.getSessionList();
    const list = await sessionApi.loadMoreSessions();

    expect(apiMocks.listChatsPage).toHaveBeenLastCalledWith({
      page: 2,
      page_size: 100,
    });
    expect(list.map((session) => session.id)).toEqual([
      "chat-3",
      "chat-2",
      "chat-1",
    ]);
    expect(sessionApi.hasMoreSessions()).toBe(false);
  });

  it("shares one in-flight request when loading the next page twice", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage.mockResolvedValueOnce({
      items: [],
      total: 1,
      page: 1,
      page_size: 100,
      has_more: true,
    });
    await sessionApi.getSessionList();

    let resolvePage: (value: unknown) => void = () => undefined;
    const pendingPage = new Promise((resolve) => {
      resolvePage = resolve;
    });
    apiMocks.listChatsPage.mockReturnValueOnce(pendingPage);

    const first = sessionApi.loadMoreSessions();
    const second = sessionApi.loadMoreSessions();
    resolvePage({
      items: [],
      total: 1,
      page: 2,
      page_size: 100,
      has_more: false,
    });
    await Promise.all([first, second]);

    expect(apiMocks.listChatsPage).toHaveBeenCalledTimes(2);
  });

  it("resets pagination state when the active identity changes", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage.mockResolvedValueOnce({
      items: [],
      total: 2,
      page: 1,
      page_size: 100,
      has_more: true,
    });
    await sessionApi.getSessionList();
    expect(sessionApi.hasMoreSessions()).toBe(true);

    sessionApi.resetForIdentityChange();

    expect(sessionApi.hasMoreSessions()).toBe(false);
  });

  it("resolves a pending session when its persisted chat arrives on a later page", async () => {
    const sessionApi = new SessionApi();
    await sessionApi.createSession({ name: "pending", messages: [] });
    const logicalSessionId = sessionApi.getPendingSessionId();
    apiMocks.listChatsPage
      .mockResolvedValueOnce({
        items: [],
        total: 1,
        page: 1,
        page_size: 100,
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-real-later",
            name: "persisted",
            session_id: logicalSessionId,
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-08T00:00:00Z",
          },
        ],
        total: 1,
        page: 2,
        page_size: 100,
        has_more: false,
      });

    await sessionApi.getSessionList();
    const list = await sessionApi.loadMoreSessions();

    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({
      id: logicalSessionId,
      realId: "chat-real-later",
      sessionId: logicalSessionId,
    });
    expect(sessionApi.getChatIdForSession(logicalSessionId!)).toBe(
      "chat-real-later",
    );
  });

  it("recovers logical identity for a deep-linked chat outside loaded pages", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage.mockResolvedValue({
      items: [],
      total: 100,
      page: 1,
      page_size: 100,
      has_more: true,
    });
    apiMocks.listChats.mockResolvedValue([]);
    apiMocks.getChat.mockResolvedValue({
      chat: {
        id: "chat-deep-link",
        name: "older chat",
        session_id: "logical-session-1",
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-01-01T00:00:00Z",
      },
      status: "idle",
      messages: [],
    });

    await sessionApi.getSessionList();
    const session = await sessionApi.getSession("chat-deep-link");

    expect(session).toMatchObject({
      id: "chat-deep-link",
      sessionId: "logical-session-1",
      name: "older chat",
    });
    expect(apiMocks.getChat).toHaveBeenCalledWith("chat-deep-link");
    expect(apiMocks.listChats).not.toHaveBeenCalled();
  });

  it("keeps pagination state when recovering a deep-linked chat outside loaded pages", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-new",
            name: "new",
            session_id: "session-new",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-10T00:00:00Z",
          },
        ],
        total: 3,
        page: 1,
        page_size: 100,
        has_more: true,
        next_cursor: "cursor-1",
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-old",
            name: "old",
            session_id: "session-old",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 3,
        page: 2,
        page_size: 100,
        has_more: false,
        next_cursor: null,
      });
    apiMocks.getChat.mockResolvedValue({
      chat: {
        id: "chat-deep-link",
        name: "deep linked",
        session_id: "session-deep-link",
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-02-01T00:00:00Z",
      },
      status: "idle",
      messages: [],
    });

    await sessionApi.getSessionList();
    await sessionApi.getSession("chat-deep-link");
    const list = await sessionApi.loadMoreSessions();

    expect(apiMocks.listChatsPage).toHaveBeenNthCalledWith(2, {
      page_size: 100,
      cursor: "cursor-1",
    });
    expect(list.map((session) => session.id)).toEqual([
      "chat-deep-link",
      "chat-new",
      "chat-old",
    ]);
  });

  it("continues history pagination with the backend cursor", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage
      .mockResolvedValueOnce({
        items: [],
        total: 100,
        page: 1,
        page_size: 100,
        has_more: true,
        next_cursor: "cursor-1",
      })
      .mockResolvedValueOnce({
        items: [],
        total: 100,
        page: 2,
        page_size: 100,
        has_more: false,
        next_cursor: null,
      });

    await sessionApi.getSessionList();
    await sessionApi.loadMoreSessions();

    expect(apiMocks.listChatsPage).toHaveBeenNthCalledWith(1, {
      page_size: 100,
      cursor: null,
    });
    expect(apiMocks.listChatsPage).toHaveBeenNthCalledWith(2, {
      page_size: 100,
      cursor: "cursor-1",
    });
  });

  it("uses runtime configured chat session page size", async () => {
    const runtimeWindow = window as Window & {
      __env__?: {
        chatSessionPageSize?: number | string;
      };
    };
    runtimeWindow.__env__ = {
      chatSessionPageSize: "42",
    };
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage
      .mockResolvedValueOnce({
        items: [],
        total: 100,
        page: 1,
        page_size: 42,
        has_more: true,
        next_cursor: "cursor-1",
      })
      .mockResolvedValueOnce({
        items: [],
        total: 100,
        page: 2,
        page_size: 42,
        has_more: false,
        next_cursor: null,
      });

    await sessionApi.getSessionList();
    await sessionApi.loadMoreSessions();

    expect(apiMocks.listChatsPage).toHaveBeenNthCalledWith(1, {
      page_size: 42,
      cursor: null,
    });
    expect(apiMocks.listChatsPage).toHaveBeenNthCalledWith(2, {
      page_size: 42,
      cursor: "cursor-1",
    });
  });

  it("falls back to the default page size for invalid runtime config", async () => {
    const runtimeWindow = window as Window & {
      __env__?: {
        chatSessionPageSize?: number | string;
      };
    };
    runtimeWindow.__env__ = {
      chatSessionPageSize: "invalid",
    };
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 100,
      has_more: false,
    });

    await sessionApi.getSessionList();

    expect(apiMocks.listChatsPage).toHaveBeenCalledWith({
      page_size: 100,
      cursor: null,
    });
  });

  it("keeps distinct chats that share a logical session id across pages", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-first",
            name: "first",
            session_id: "shared-logical-session",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-10T00:00:00Z",
          },
        ],
        total: 2,
        page: 1,
        page_size: 100,
        has_more: true,
        next_cursor: "cursor-1",
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-second",
            name: "second",
            session_id: "shared-logical-session",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-09T00:00:00Z",
          },
        ],
        total: 2,
        page: 2,
        page_size: 100,
        has_more: false,
        next_cursor: null,
      });

    await sessionApi.getSessionList();
    const list = await sessionApi.loadMoreSessions();

    expect(list.map((session) => session.id)).toEqual([
      "chat-first",
      "chat-second",
    ]);
  });

  it("keeps loaded older pages when page one is refreshed", async () => {
    const sessionApi = new SessionApi();
    apiMocks.listChatsPage
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-new",
            name: "new",
            session_id: "session-new",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-06-10T00:00:00Z",
          },
        ],
        total: 2,
        page: 1,
        page_size: 100,
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-old",
            name: "old",
            session_id: "session-old",
            user_id: "user-1",
            channel: "console",
            meta: {},
            status: "idle",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 2,
        page: 2,
        page_size: 100,
        has_more: false,
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: "chat-new",
            name: "new title",
            session_id: "session-new",
            user_id: "user-1",
            channel: "console",
            meta: { session_title_generated: true },
            status: "idle",
            created_at: "2026-06-10T00:00:00Z",
          },
        ],
        total: 2,
        page: 1,
        page_size: 100,
        has_more: true,
      });

    await sessionApi.getSessionList();
    await sessionApi.loadMoreSessions();
    const refreshed = await sessionApi.getSessionList();

    expect(refreshed.map((session) => session.id)).toEqual([
      "chat-new",
      "chat-old",
    ]);
    expect(refreshed[0]?.name).toBe("new title");
  });

  it("keeps the generated title after finishing a stream with local message sync", async () => {
    const sessionApi = new SessionApi();

    await sessionApi.createSession({
      name: "original question",
      messages: [],
    });

    const logicalSessionId = sessionApi.getPendingSessionId();
    expect(logicalSessionId).toBeTruthy();

    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-title",
        name: "original question",
        session_id: logicalSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "running",
        created_at: "2026-06-10T00:00:00Z",
      },
    ]);

    await sessionApi.updateSession({
      id: logicalSessionId!,
      messages: [],
      generating: true,
    });

    sessionApi.patchSessionTitle({
      chat_id: "chat-real-title",
      session_id: logicalSessionId,
      session_title: "generated title",
    });

    const synced = await sessionApi.updateSession(
      {
        id: logicalSessionId!,
        messages: [
          {
            id: "message-1",
            role: "assistant",
            msgStatus: "finished",
            cards: [],
          },
        ],
        generating: false,
      },
      { refreshList: false },
    );

    expect(synced[0]?.name).toBe("generated title");
    expect(
      (synced[0] as { meta?: Record<string, unknown> })?.meta,
    ).toMatchObject({
      session_title_generated: true,
    });
  });

  it("filters stale task sessions from later pages", async () => {
    const sessionApi = new SessionApi();
    cronJobApiMocks.listCronJobs.mockResolvedValue([]);
    apiMocks.listChatsPage
      .mockResolvedValueOnce({
        items: [],
        total: 1,
        page: 1,
        page_size: 100,
        has_more: true,
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: "stale-task-chat",
            name: "stale task",
            session_id: "task-session",
            user_id: "user-1",
            channel: "console",
            meta: { session_kind: "task", task_job_id: "missing-job" },
            status: "idle",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total: 1,
        page: 2,
        page_size: 100,
        has_more: false,
      });

    await sessionApi.getSessionList();
    const list = await sessionApi.loadMoreSessions();

    expect(list).toEqual([]);
  });
});

describe("archived conversation card conversion", () => {
  it("keeps the compaction boundary after a local timestamp session resolves", async () => {
    const sessionApi = new SessionApi();
    await sessionApi.createSession({ name: "new chat", messages: [] });
    const localSessionId = sessionApi.getPendingSessionId();
    expect(localSessionId).toBeTruthy();
    apiMocks.listChats.mockResolvedValue([
      {
        id: "chat-real-1",
        name: "new chat",
        session_id: localSessionId,
        user_id: "user-1",
        channel: "console",
        meta: {},
        status: "idle",
        created_at: "2026-08-01T00:00:00Z",
      },
    ]);
    apiMocks.getChat.mockResolvedValue({
      id: "chat-real-1",
      messages: [{ id: "online-1", role: "user", content: "current" }],
      archive: {
        has_more: true,
        boundaries: [
          {
            id: "boundary-1",
            archived_message_count: 3,
            first_message_id: "archived-1",
            last_message_id: "archived-3",
            created_at: "2026-08-01T12:00:00+00:00",
          },
        ],
      },
    });

    await sessionApi.updateSession({ id: localSessionId!, name: "new chat" });
    const session = await sessionApi.getSession(localSessionId!);

    expect(session.messages?.[0]?.cards?.[0]?.code).toBe(
      "ConversationCompactionBoundary",
    );
  });

  it("shows the latest archive boundary before online messages", async () => {
    apiMocks.getChat.mockResolvedValue({
      id: "chat-real-1",
      messages: [{ id: "online-1", role: "user", content: "current" }],
      archive: {
        has_more: true,
        boundaries: [
          {
            id: "boundary-1",
            archived_message_count: 3,
            first_message_id: "archived-1",
            last_message_id: "archived-3",
            created_at: "2026-08-01T12:00:00+00:00",
          },
        ],
      },
    });
    const sessionApi = new SessionApi();

    const session = await sessionApi.getSession("chat-real-1");

    expect(session.messages?.map((message) => message.cards?.[0]?.code)).toEqual(
      [
        "ConversationCompactionBoundary",
        "AgentScopeRuntimeRequestCard",
      ],
    );
  });

  it("places a compaction boundary after the batch it terminates", () => {
    const cards = convertArchivedPage(
      [
        { id: "user-1", role: "user", content: "first" },
        {
          id: "assistant-1",
          role: "assistant",
          content: "answer",
          metadata: {
            plan_interaction_card: {
              card_type: "plan_review",
              plan_id: "plan-1",
              title: "Implementation plan",
              summary: "Review before execution",
              steps: [],
              risks: [],
              verification: [],
            },
          },
        },
        { id: "user-2", role: "user", content: "second" },
      ],
      [
        {
          id: "boundary-1",
          archived_message_count: 2,
          first_message_id: "user-1",
          last_message_id: "assistant-1",
          created_at: "2026-08-01T12:00:00+00:00",
        },
      ],
    );

    expect(cards.map((card) => card.cards?.[0]?.code)).toEqual([
      "AgentScopeRuntimeRequestCard",
      "AgentScopeRuntimeResponseCard",
      "ConversationCompactionBoundary",
      "AgentScopeRuntimeRequestCard",
    ]);
    expect(cards[1]?.cards?.map((card) => card.code)).toEqual([
      "AgentScopeRuntimeResponseCard",
      "PlanInteraction",
      "ResponseFeedback",
    ]);
    expect(cards[1]?.cards?.[0]?.data).toMatchObject({
      planReviewCard: {
        card_type: "plan_review",
        plan_id: "plan-1",
      },
    });
  });

  it("places a compaction boundary after the final runtime fragment of its source message", () => {
    const cards = convertArchivedPage(
      [
        {
          id: "runtime-fragment-1",
          role: "assistant",
          content: "thinking",
          metadata: { original_id: "source-message-1" },
        },
        {
          id: "runtime-fragment-2",
          role: "assistant",
          content: "answer",
          metadata: { original_id: "source-message-1" },
        },
        {
          id: "runtime-message-3",
          role: "user",
          content: "follow-up",
          metadata: { original_id: "source-message-2" },
        },
      ],
      [
        {
          id: "boundary-1",
          archived_message_count: 1,
          first_message_id: "source-message-1",
          last_message_id: "source-message-1",
          created_at: "2026-08-01T12:00:00+00:00",
        },
      ],
    );

    expect(cards.map((card) => card.cards?.[0]?.code)).toEqual([
      "AgentScopeRuntimeResponseCard",
      "ConversationCompactionBoundary",
      "AgentScopeRuntimeRequestCard",
    ]);
    expect(
      (cards[0]?.cards?.[0]?.data as { output?: unknown[] }).output,
    ).toHaveLength(2);
  });
});
