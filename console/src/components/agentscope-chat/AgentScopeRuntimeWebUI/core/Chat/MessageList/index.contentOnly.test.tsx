import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { forwardRef, useImperativeHandle, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatContentOnlyProvider } from "@/components/agentscope-chat/ChatContentOnlyContext";
import MessageList from ".";
import * as sessionApiModule from "@/pages/Chat/sessionApi";
import { resolveHistoryAnchorId } from "@/pages/Chat/sessionApi/historyAssembly";

const mocks = vi.hoisted(() => ({
  messagesContext: {},
  sessionsContext: {},
  messages: [] as Array<{
    id: string;
    history?: boolean;
    msgStatus?: "generating" | "finished" | "error" | "interrupted";
    role?: "assistant" | "user" | "system";
    cards?: Array<{ code: string; data?: unknown }>;
  }>,
  setMessages: vi.fn(),
  isSessionLoading: false,
  sessionNotFound: false,
  currentSessionId: "chat-1",
  anchorId: "online-message",
  anchorTop: 100,
  scrollHeight: 1000,
  animationFrame: undefined as FrameRequestCallback | undefined,
  frames: new Map<number, FrameRequestCallback>(),
  nextFrameId: 0,
}));

async function flushHistoryFrame() {
  const callbacks = [...mocks.frames.values()];
  mocks.frames.clear();
  await act(async () => callbacks.forEach((callback) => callback(0)));
}

async function flushHistoryLayout() {
  await flushHistoryFrame();
  await flushHistoryFrame();
}

const apiMocks = vi.hoisted(() => ({
  getChatHistory: vi.fn(),
  getChatIdForSession: vi.fn(),
  getSession: vi.fn(),
  scrollToBottom: vi.fn(),
}));

vi.mock("use-context-selector", () => ({
  useContextSelector: (
    context: unknown,
    selector: (value: unknown) => unknown,
  ) => {
    if (context === mocks.messagesContext) {
      return selector({
        messages: mocks.messages,
        setMessages: mocks.setMessages,
      });
    }
    return selector({
      currentSessionId: mocks.currentSessionId,
      isSessionLoading: mocks.isSessionLoading,
      sessionNotFound: mocks.sessionNotFound,
    });
  },
}));

vi.mock("../../Context/ChatAnywhereMessagesContext", () => ({
  ChatAnywhereMessagesContext: mocks.messagesContext,
}));

vi.mock("../../Context/ChatAnywhereSessionsContext", () => ({
  ChatAnywhereSessionsContext: mocks.sessionsContext,
}));

vi.mock("@/components/agentscope-chat", () => ({
  Bubble: {
    List: forwardRef(
      (
        {
          items = [],
          onReachStart,
          onBottomStateChange,
          pagination,
          topContent,
        }: {
          items?: Array<{ id?: string }>;
          onReachStart?: () => void;
          onBottomStateChange?: (isAtBottom: boolean) => void;
          pagination?: boolean;
          topContent?: React.ReactNode;
        },
        ref,
      ) => {
        const scrollRef = useRef<HTMLDivElement | null>(null);
        useImperativeHandle(ref, () => ({
          getScrollElement: () => scrollRef.current,
          scrollToBottom: apiMocks.scrollToBottom,
        }));
        return (
          <div
            data-testid="bubble-list"
            data-item-ids={items.map((item) => item.id).join(",")}
            data-pagination={String(pagination)}
            onPointerMove={onReachStart}
            ref={(element) => {
              scrollRef.current = element;
              if (!element) return;
              if (element.dataset.scrollGeometryReady === "true") return;
              element.dataset.scrollGeometryReady = "true";
              Object.defineProperties(element, {
                clientHeight: { configurable: true, value: 400 },
                scrollHeight: {
                  configurable: true,
                  value: mocks.scrollHeight,
                },
                scrollTop: {
                  configurable: true,
                  value: 0,
                  writable: true,
                },
                getBoundingClientRect: {
                  configurable: true,
                  value: () => ({
                    bottom: 400,
                    height: 400,
                    left: 0,
                    right: 400,
                    toJSON: () => ({}),
                    top: 0,
                    width: 400,
                    x: 0,
                    y: 0,
                  }),
                },
              });
            }}
          >
            {topContent}
            {items.length}
            {items.map((item, index) => (
              <div
                data-role="assistant"
                id={item.id}
                key={item.id || index}
                ref={(element) => {
                  if (!element) return;
                  Object.defineProperty(element, "getBoundingClientRect", {
                    configurable: true,
                    value: () => ({
                      bottom:
                        (item.id === mocks.anchorId ? mocks.anchorTop : -100) +
                        48,
                      height: 48,
                      left: 0,
                      right: 400,
                      toJSON: () => ({}),
                      top: item.id === mocks.anchorId ? mocks.anchorTop : -100,
                      width: 400,
                      x: 0,
                      y: item.id === mocks.anchorId ? mocks.anchorTop : -100,
                    }),
                  });
                }}
              />
            ))}
            <button onClick={() => onBottomStateChange?.(false)} type="button">
              标记为浏览历史
            </button>
          </div>
        );
      },
    ),
  },
  useProviderContext: () => ({
    getPrefixCls: (name: string) => `copaw-${name}`,
  }),
}));

vi.mock("@/api/modules/chat", () => ({
  chatApi: {
    getChatHistory: apiMocks.getChatHistory,
  },
  sessionApi: {},
}));

vi.mock("@/pages/Chat/sessionApi", () => ({
  convertMessages: (messages: Array<{ id: string }>) => messages,
  convertArchivedPage: (
    messages: Array<{ id: string }>,
    boundaries: Array<{ id: string }> = [],
  ) => [
    ...messages,
    ...boundaries.map((boundary) => ({
      id: `conversation-compaction-${boundary.id}`,
    })),
  ],
  default: {
    getChatIdForSession: apiMocks.getChatIdForSession,
    getSession: apiMocks.getSession,
  },
}));

vi.mock("../../Context/ChatAnywhereOptionsContext", () => ({
  useChatAnywhereOptions: (
    selector: (value: { theme: { bubbleList: object } }) => unknown,
  ) => selector({ theme: { bubbleList: {} } }),
}));

vi.mock("../Welcome", () => ({
  default: () => <div data-testid="welcome">welcome</div>,
}));

vi.mock("antd", () => ({
  Result: ({ title, subTitle }: { title: string; subTitle: string }) => (
    <div data-testid="not-found-result">
      <span>{title}</span>
      <span>{subTitle}</span>
    </div>
  ),
  Spin: () => <div data-testid="spin">loading</div>,
}));

describe("MessageList content-only composition", () => {
  it("continues toward a user message after the first page moves the viewport away from the top", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory
      .mockResolvedValueOnce({
        messages: [{ id: "fragment", role: "assistant" }],
        boundaries: [],
        has_more: true,
        next_cursor: "next",
      })
      .mockResolvedValueOnce({
        messages: [{ id: "question", role: "user" }],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const list = screen.getByTestId("bubble-list");
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    list.scrollTop = -100;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryLayout();
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2),
    );
    expect(mocks.messages.some((message) => message.id === "question")).toBe(
      true,
    );
  });

  it("continues past a user already present in the current history", async () => {
    mocks.messages = [{ id: "question", role: "user", history: true }];
    apiMocks.getChatHistory
      .mockResolvedValueOnce({
        messages: [{ id: "question", role: "user" }],
        boundaries: [],
        has_more: true,
        next_cursor: "next",
      })
      .mockResolvedValueOnce({
        messages: [{ id: "older", role: "user" }],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryLayout();
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2),
    );
    expect(mocks.messages.some((message) => message.id === "older")).toBe(true);
  });

  it("keeps the first page and retries from its cursor when the next page fails", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory
      .mockResolvedValueOnce({
        messages: [{ id: "fragment", role: "assistant" }],
        boundaries: [],
        has_more: true,
        next_cursor: "next",
      })
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({
        messages: [{ id: "question", role: "user" }],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryLayout();
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("历史消息加载失败"),
    );
    expect(mocks.messages.some((message) => message.id === "fragment")).toBe(
      true,
    );
    fireEvent.click(screen.getByRole("button", { name: "重试加载历史消息" }));
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(3),
    );
    expect(apiMocks.getChatHistory).toHaveBeenLastCalledWith(
      "chat-real-1",
      "next",
      20,
    );
    expect(mocks.messages.some((message) => message.id === "question")).toBe(
      true,
    );
  });
  it("binds the replacement scroll node after loading with the same message count", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [],
      boundaries: [],
      has_more: false,
    });
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const oldNode = screen.getByTestId("bubble-list");
    mocks.isSessionLoading = true;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    mocks.isSessionLoading = false;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    const newNode = screen.getByTestId("bubble-list");
    expect(newNode).not.toBe(oldNode);
    oldNode.scrollTop = -600;
    fireEvent.scroll(oldNode);
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
    newNode.scrollTop = -600;
    fireEvent.scroll(newNode);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
  });

  it("bounds pages with no visible additions and permits explicit continuation", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    let page = 0;
    apiMocks.getChatHistory.mockImplementation(async () => ({
      messages: [],
      boundaries: [],
      has_more: true,
      next_cursor: `cursor-${++page}`,
    }));
    render(<MessageList onSubmit={vi.fn()} />);
    const node = screen.getByTestId("bubble-list");
    node.scrollTop = -600;
    fireEvent.scroll(node);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    await flushHistoryLayout();
    await flushHistoryLayout();
    await flushHistoryLayout();
    await flushHistoryLayout();
    await flushHistoryLayout();
    expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(5);
    fireEvent.click(
      screen.getByRole("button", { name: "本轮内容较多，继续加载更早消息" }),
    );
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(6),
    );
  });

  it("rejects an archive cursor that cannot advance and retries the same page", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [{ id: "older" }],
      boundaries: [],
      has_more: true,
      next_cursor: null,
    });
    render(<MessageList onSubmit={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("历史消息加载失败"),
    );
    expect(mocks.messages).toEqual([{ id: "online-message", history: true }]);
    fireEvent.click(screen.getByRole("button", { name: "重试加载历史消息" }));
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2),
    );
    expect(apiMocks.getChatHistory).toHaveBeenLastCalledWith(
      "chat-real-1",
      null,
      20,
    );
  });

  beforeEach(() => {
    mocks.messages = [];
    mocks.setMessages.mockReset();
    mocks.setMessages.mockImplementation((update) => {
      mocks.messages =
        typeof update === "function" ? update(mocks.messages) : update;
    });
    apiMocks.getChatHistory.mockReset();
    apiMocks.getChatIdForSession.mockReset();
    apiMocks.getSession.mockReset();
    apiMocks.scrollToBottom.mockReset();
    apiMocks.getChatIdForSession.mockReturnValue("chat-real-1");
    mocks.isSessionLoading = false;
    mocks.sessionNotFound = false;
    mocks.currentSessionId = "chat-1";
    mocks.anchorId = "online-message";
    mocks.anchorTop = 100;
    mocks.scrollHeight = 1000;
    mocks.animationFrame = undefined;
    mocks.frames.clear();
    mocks.nextFrameId = 0;
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      mocks.animationFrame = callback;
      mocks.frames.set(++mocks.nextFrameId, callback);
      return mocks.nextFrameId;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) =>
      mocks.frames.delete(id),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    cleanup();
  });

  it("retains same-source fragments and pairs one response across archive pages", async () => {
    const actual = await vi.importActual<
      typeof import("@/pages/Chat/sessionApi")
    >("@/pages/Chat/sessionApi");
    vi.spyOn(sessionApiModule, "convertArchivedPage").mockImplementation(
      actual.convertArchivedPage,
    );
    const runtime = (type: string, callId: string) => ({
      id: "legacy-source",
      role: "assistant",
      type,
      metadata: { original_id: "legacy-source" },
      content: [
        {
          type: "data",
          data: { call_id: callId, arguments: "{}", output: "ok" },
        },
      ],
    });
    mocks.messages = actual
      .convertMessages([
        {
          id: "result-a",
          role: "system",
          type: "plugin_call_output",
          content: [{ type: "data", data: { call_id: "a", output: "ok" } }],
        },
      ])
      .map((message) => ({ ...message, history: true }));
    const responseId = mocks.messages[0].id;
    apiMocks.getChatHistory
      .mockResolvedValueOnce({
        messages: [runtime("plugin_call", "a"), runtime("plugin_call", "b")],
        boundaries: [],
        has_more: true,
        next_cursor: "older",
      })
      .mockResolvedValueOnce({
        messages: [{ id: "question", role: "user", content: "question" }],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
    render(<MessageList onSubmit={vi.fn()} />);
    const list = screen.getByTestId("bubble-list");
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    await waitFor(() => expect(mocks.messages).toHaveLength(1));
    expect(mocks.messages[0].id).toBe(responseId);
    expect(
      (mocks.messages[0].cards?.[0].data as { output: unknown[] }).output,
    ).toHaveLength(3);
    await flushHistoryLayout();
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2),
    );
    await waitFor(() =>
      expect(mocks.messages.map((message) => message.role)).toEqual([
        "user",
        "assistant",
      ]),
    );
    expect(
      (mocks.messages[1].cards?.[0].data as { output: unknown[] }).output,
    ).toHaveLength(3);
  });

  it.each([2, 3])(
    "keeps newly loaded response fragments across %s archive pages",
    async (pageCount) => {
      const actual = await vi.importActual<
        typeof import("@/pages/Chat/sessionApi")
      >("@/pages/Chat/sessionApi");
      vi.spyOn(sessionApiModule, "convertArchivedPage").mockImplementation(
        actual.convertArchivedPage,
      );
      mocks.messages = actual.convertArchivedPage(
        [{ id: "latest-question", role: "user", content: "latest" }],
        [],
      );
      for (let page = 0; page < pageCount; page += 1) {
        const lastPage = page === pageCount - 1;
        apiMocks.getChatHistory.mockResolvedValueOnce({
          messages: [
            ...(lastPage
              ? [{ id: "question", role: "user", content: "question" }]
              : []),
            {
              id: "fragment-" + (pageCount - page),
              role: "assistant",
              content: "part " + page,
            },
          ],
          boundaries: [],
          has_more: !lastPage,
          next_cursor: lastPage ? null : "page-" + page,
        });
      }
      const rendered = render(<MessageList onSubmit={vi.fn()} />);
      const list = screen.getByTestId("bubble-list");
      const responseId = actual.convertArchivedPage(
        [{ id: "fragment-" + pageCount, role: "assistant", content: "part 0" }],
        [],
      )[0].id;
      list.scrollTop = -380;
      fireEvent.scroll(list);
      for (let page = 0; page < pageCount; page += 1) {
        await waitFor(() =>
          expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(page + 1),
        );
        const response = mocks.messages.find(
          (message) => message.role === "assistant",
        )!;
        expect(response.id).toBe(responseId);
        const loadedResponse = actual.convertArchivedPage(
          [
            {
              id: "fragment-" + (pageCount - page),
              role: "assistant",
              content: "part " + page,
            },
          ],
          [],
        )[0];
        expect(
          resolveHistoryAnchorId(
            mocks.messages as Parameters<typeof resolveHistoryAnchorId>[0],
            "process:" + loadedResponse.cards![0].data.id,
          ),
        ).toBe("process:" + (response.cards![0].data as { id: string }).id);
        expect(
          (
            response.cards![0].data as { output: Array<{ id: string }> }
          ).output.map((item) => item.id),
        ).toEqual(
          Array.from(
            { length: page + 1 },
            (_, index) => "fragment-" + (pageCount - page + index),
          ),
        );
        if (page < pageCount - 1) {
          rendered.rerender(<MessageList onSubmit={vi.fn()} />);
          await flushHistoryLayout();
        }
      }
      expect(mocks.messages.map((message) => message.role)).toEqual([
        "user",
        "assistant",
        "user",
      ]);
      expect(apiMocks.getChatHistory).toHaveBeenLastCalledWith(
        "chat-real-1",
        "page-" + (pageCount - 2),
        20,
      );
    },
  );

  it("keeps an online response after its user when an archive page repeats only the response", async () => {
    const actual = await vi.importActual<
      typeof import("@/pages/Chat/sessionApi")
    >("@/pages/Chat/sessionApi");
    vi.spyOn(sessionApiModule, "convertArchivedPage").mockImplementation(
      actual.convertArchivedPage,
    );
    const response = { id: "answer", role: "assistant", content: "answer" };
    mocks.messages = actual.convertArchivedPage(
      [{ id: "question", role: "user", content: "question" }, response],
      [],
    );
    const originalIds = mocks.messages.map((message) => message.id);
    apiMocks.getChatHistory.mockResolvedValueOnce({
      messages: [response],
      boundaries: [],
      has_more: true,
      next_cursor: "question-page",
    });
    apiMocks.getChatHistory.mockResolvedValueOnce({
      messages: [{ id: "question", role: "user", content: "question" }],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const list = screen.getByTestId("bubble-list");
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    expect(mocks.messages.map((message) => message.id)).toEqual(originalIds);
    expect(
      (mocks.messages[1].cards![0].data as { output: unknown[] }).output,
    ).toHaveLength(1);
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryLayout();
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2),
    );
    expect(mocks.messages.map((message) => message.id)).toEqual(originalIds);
    expect(apiMocks.getChatHistory).toHaveBeenLastCalledWith(
      "chat-real-1",
      "question-page",
      20,
    );
  });

  it.each([false, true])(
    "preserves online response precedence before a shared later user (archive prefix: %s)",
    async (withPrefix) => {
      const actual = await vi.importActual<
        typeof import("@/pages/Chat/sessionApi")
      >("@/pages/Chat/sessionApi");
      vi.spyOn(sessionApiModule, "convertArchivedPage").mockImplementation(
        actual.convertArchivedPage,
      );
      const laterUser = { id: "q2", role: "user", content: "next" };
      mocks.messages = actual.convertArchivedPage(
        [
          { id: "q1", role: "user", content: "question" },
          { id: "a1", role: "assistant", content: "first" },
          { id: "a2", role: "assistant", content: "online latest" },
          laterUser,
        ],
        [],
      );
      const online = mocks.messages[1];
      const archive = [
        ...(withPrefix
          ? [{ id: "a0", role: "assistant", content: "archived prefix" }]
          : []),
        { id: "a2", role: "assistant", content: "archive stale" },
        laterUser,
      ];
      const archivedResponse = actual.convertArchivedPage(archive, [])[0];
      apiMocks.getChatHistory.mockResolvedValueOnce({
        messages: archive,
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
      render(<MessageList onSubmit={vi.fn()} />);
      const list = screen.getByTestId("bubble-list");
      list.scrollTop = -380;
      fireEvent.scroll(list);
      await waitFor(() =>
        expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
      );
      const response = mocks.messages[1];
      expect
        .soft(mocks.messages.map((message) => message.id))
        .toEqual(["q1", online.id, "q2"]);
      const data = response.cards![0].data as {
        id: string;
        output: Array<{ id: string; content: unknown }>;
      };
      expect.soft(data.id).toBe((online.cards![0].data as { id: string }).id);
      expect
        .soft(data.output.find((message) => message.id === "a2")?.content)
        .toEqual(
          (online.cards![0].data as typeof data).output.find(
            (message) => message.id === "a2",
          )?.content,
        );
      expect(data.output.map((message) => message.id)).toEqual(
        withPrefix ? ["a1", "a0", "a2"] : ["a1", "a2"],
      );
      expect(
        resolveHistoryAnchorId(
          mocks.messages as Parameters<typeof resolveHistoryAnchorId>[0],
          "process:" + archivedResponse.cards![0].data.id,
        ),
      ).toBe("process:" + data.id);
    },
  );

  it("reconciles archive overlap with the online response after its user", async () => {
    const actual = await vi.importActual<
      typeof import("@/pages/Chat/sessionApi")
    >("@/pages/Chat/sessionApi");
    vi.spyOn(sessionApiModule, "convertArchivedPage").mockImplementation(
      actual.convertArchivedPage,
    );
    const user = { id: "question", role: "user", content: "question" };
    const first = { id: "answer-1", role: "assistant", content: "first" };
    mocks.messages = actual
      .convertMessages([
        user,
        first,
        { id: "answer-2", role: "assistant", content: "second" },
      ])
      .map((message) => ({ ...message, history: true }));
    apiMocks.getChatHistory.mockResolvedValueOnce({
      messages: [user, first],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });
    render(<MessageList onSubmit={vi.fn()} />);
    const list = screen.getByTestId("bubble-list");
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    expect(mocks.messages.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(
      (mocks.messages[1].cards![0].data as { output: unknown[] }).output,
    ).toHaveLength(2);
  });

  it.each(["before-event", "after-event", "in-flight", "after-snapshot"])(
    "preserves same-turn archive fragments and cursor when loaded %s",
    async (timing) => {
      vi.useFakeTimers();
      const actual = await vi.importActual<
        typeof import("@/pages/Chat/sessionApi")
      >("@/pages/Chat/sessionApi");
      vi.spyOn(sessionApiModule, "convertArchivedPage").mockImplementation(
        actual.convertArchivedPage,
      );
      const current = actual
        .convertMessages([
          {
            id: "result",
            role: "system",
            type: "plugin_call_output",
            content: [{ type: "data", data: { call_id: "a", output: "ok" } }],
          },
          { id: "answer", role: "assistant", content: "answer" },
        ])
        .map((message) => ({ ...message, history: true }));
      mocks.messages = current;
      const page = {
        messages: [
          { id: "older-user", role: "user", content: "question" },
          {
            id: "call",
            role: "assistant",
            type: "plugin_call",
            content: [
              { type: "data", data: { call_id: "a", arguments: "{}" } },
            ],
          },
        ],
        boundaries: [],
        has_more: true,
        next_cursor: "after-first-page",
      };
      let resolvePage!: (value: typeof page) => void;
      if (timing === "after-snapshot") {
        apiMocks.getChatHistory.mockReturnValueOnce(
          new Promise((resolve) => {
            resolvePage = resolve;
          }),
        );
      }
      apiMocks.getChatHistory.mockResolvedValue(page);
      const snapshot = { generating: false, messages: current };
      let resolveSnapshot!: (value: typeof snapshot) => void;
      if (timing === "in-flight") {
        apiMocks.getSession.mockReturnValueOnce(
          new Promise((resolve) => {
            resolveSnapshot = resolve;
          }),
        );
      }
      apiMocks.getSession.mockResolvedValue(snapshot);
      const view = () => <MessageList onSubmit={vi.fn()} />;
      const rendered = render(view());
      const loadArchive = async () => {
        const list = screen.getByTestId("bubble-list");
        list.scrollTop = -380;
        fireEvent.scroll(list);
        await act(async () => {
          await Promise.resolve();
        });
        rendered.rerender(view());
      };
      if (timing === "before-event") await loadArchive();
      document.dispatchEvent(
        new CustomEvent("conversation_compacted", {
          detail: { chat_id: "chat-real-1" },
        }),
      );
      if (timing === "in-flight") {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
        });
      }
      if (timing !== "before-event") await loadArchive();
      if (timing === "in-flight") resolveSnapshot(snapshot);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      if (timing === "after-snapshot") {
        await act(async () => {
          resolvePage(page);
          await Promise.resolve();
        });
        rendered.rerender(view());
      }
      expect(apiMocks.getSession).toHaveBeenCalled();
      expect(mocks.messages.map((message) => message.role)).toEqual([
        "user",
        "assistant",
      ]);
      expect(
        (
          mocks.messages[1].cards![0].data as { output: Array<{ id: string }> }
        ).output.map((item) => item.id),
      ).toEqual(["call", "result", "answer"]);
      const calls = apiMocks.getSession.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(apiMocks.getSession).toHaveBeenCalledTimes(calls);
      rendered.rerender(view());
      await flushHistoryLayout();
      const list = screen.getByTestId("bubble-list");
      list.scrollTop = -200;
      fireEvent.scroll(list);
      list.scrollTop = -380;
      fireEvent.scroll(list);
      await act(async () => {
        await Promise.resolve();
      });
      expect(apiMocks.getChatHistory).toHaveBeenLastCalledWith(
        "chat-real-1",
        timing === "before-event" ? null : "after-first-page",
        20,
      );
    },
  );

  it.each([false, true])(
    "automatically confirms the start of a short conversation (content-only: %s)",
    async (contentOnly) => {
      mocks.messages = [{ id: "online-message", history: true }];
      mocks.scrollHeight = 300;
      apiMocks.getChatHistory.mockResolvedValue({
        messages: [],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
      render(
        <ChatContentOnlyProvider enabled={contentOnly}>
          <MessageList onSubmit={vi.fn()} />
        </ChatContentOnlyProvider>,
      );
      await waitFor(() =>
        expect(screen.getByRole("status")).toHaveTextContent(
          "已到达会话开始处",
        ),
      );
      expect(apiMocks.getChatHistory).toHaveBeenCalledExactlyOnceWith(
        "chat-real-1",
        null,
        20,
      );
      expect(
        screen.queryByRole("button", { name: "加载更早消息" }),
      ).not.toBeInTheDocument();
      await flushHistoryLayout();
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1);
    },
  );

  it("waits for the switched session and its list before checking history", async () => {
    mocks.messages = [{ id: "old", history: true }];
    mocks.scrollHeight = 300;
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    apiMocks.getChatHistory.mockClear();

    mocks.currentSessionId = "chat-2";
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
    mocks.isSessionLoading = true;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
    mocks.messages = [{ id: "new", history: true }];
    mocks.isSessionLoading = false;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    expect(screen.getByTestId("bubble-list")).toHaveAttribute(
      "data-item-ids",
      "new",
    );
  });

  it("bounds initial short-list paging even when empty pages keep advancing", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    mocks.scrollHeight = 300;
    let page = 0;
    apiMocks.getChatHistory.mockImplementation(async () => ({
      messages: [],
      boundaries: [],
      has_more: true,
      next_cursor: `cursor-${++page}`,
    }));
    render(<MessageList onSubmit={vi.fn()} />);
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1),
    );
    for (let step = 0; step < 5; step += 1) await flushHistoryLayout();
    expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "加载更早消息" })).toBeEnabled();
  });

  it("shows retry feedback without automatically retrying a failed initial check", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    mocks.scrollHeight = 300;
    apiMocks.getChatHistory.mockRejectedValue(new Error("offline"));
    render(<MessageList onSubmit={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("历史消息加载失败"),
    );
    await flushHistoryLayout();
    expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1);
  });

  it("preserves the normal Welcome surface outside content-only mode", () => {
    render(<MessageList onSubmit={vi.fn()} />);

    expect(screen.getByTestId("welcome")).toBeInTheDocument();
  });

  it("does not mount the input-bearing Welcome surface in content-only mode", () => {
    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.queryByTestId("welcome")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bubble-list")).not.toBeInTheDocument();
    expect(screen.queryByTestId("not-found-result")).not.toBeInTheDocument();
  });

  it("renders an unavailable result for an active content-only 404", () => {
    mocks.sessionNotFound = true;

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.getByTestId("not-found-result")).toHaveTextContent(
      "会话不存在",
    );
    expect(screen.getByTestId("not-found-result")).toHaveTextContent(
      "该会话不存在或已被删除",
    );
    expect(screen.queryByTestId("welcome")).not.toBeInTheDocument();
  });

  it("does not replace normal chat with the content-only 404 result", () => {
    mocks.sessionNotFound = true;

    render(<MessageList onSubmit={vi.fn()} />);

    expect(screen.getByTestId("welcome")).toBeInTheDocument();
    expect(screen.queryByTestId("not-found-result")).not.toBeInTheDocument();
  });

  it("keeps the existing session loading state unchanged", () => {
    mocks.isSessionLoading = true;
    mocks.sessionNotFound = true;

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.getByTestId("spin")).toBeInTheDocument();
    expect(screen.queryByTestId("not-found-result")).not.toBeInTheDocument();
  });

  it("keeps loaded messages on the existing Bubble list", () => {
    mocks.messages = [{ id: "message-1" }];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.getByTestId("bubble-list")).toHaveTextContent("1");
  });

  it("uses cursor history paging instead of BubbleList's local pagination", () => {
    mocks.messages = [{ id: "online-message", history: true }];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.getByTestId("bubble-list")).toHaveAttribute(
      "data-pagination",
      "false",
    );
  });

  it("renders the newest 10 historical cards initially while keeping live cards visible", () => {
    mocks.messages = [
      ...Array.from({ length: 25 }, (_, index) => ({
        id: `history-${index + 1}`,
        history: true,
      })),
      { id: "live-message" },
    ];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    expect(bubbleList).toHaveTextContent("11");
    expect(bubbleList).toHaveAttribute(
      "data-item-ids",
      "live-message,history-25,history-24,history-23,history-22,history-21,history-20,history-19,history-18,history-17,history-16",
    );
  });

  it("uses the same progressive window in normal chat mode", () => {
    mocks.messages = Array.from({ length: 25 }, (_, index) => ({
      id: `history-${index + 1}`,
      history: true,
    }));

    render(<MessageList onSubmit={vi.fn()} />);

    expect(screen.getByTestId("bubble-list")).toHaveTextContent("10");
  });

  it("reveals local history in 10-card batches before requesting the archive", async () => {
    mocks.messages = Array.from({ length: 25 }, (_, index) => ({
      id: `history-${index + 1}`,
      history: true,
    }));

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    expect(bubbleList).toHaveTextContent("10");

    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => expect(bubbleList).toHaveTextContent("20"));
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveAttribute("data-state", "idle");

    // Layout recovery holds the lock; afterwards remaining near the top advances.
    fireEvent.scroll(bubbleList);
    expect(bubbleList).toHaveTextContent("20");

    await flushHistoryLayout();
    await waitFor(() => expect(bubbleList).toHaveTextContent("25"));
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
  });

  it("continues through hidden local cards to a user after the anchor moves", async () => {
    mocks.messages = Array.from({ length: 25 }, (_, index) => ({
      id: `local-${index}`,
      role: index === 3 ? "user" : "assistant",
      history: true,
    }));
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const list = screen.getByTestId("bubble-list");
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await waitFor(() => expect(list).toHaveTextContent("20"));
    list.scrollTop = -100;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryLayout();
    await waitFor(() => expect(list).toHaveTextContent("25"));
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
  });

  it("restores the visible anchor after revealing a local history batch", async () => {
    mocks.messages = [
      ...Array.from({ length: 15 }, (_, index) => ({
        id: `history-${index + 1}`,
        history: true,
      })),
      { id: "online-message" },
    ];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    const bubbleList = screen.getByTestId("bubble-list");

    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => expect(bubbleList).toHaveTextContent("16"));

    expect(mocks.animationFrame).toBeTypeOf("function");
    mocks.anchorTop = 140;
    await flushHistoryFrame();
    expect(bubbleList.scrollTop).toBe(-340);
  });

  it("requests one archive page with limit 20 only after local history is fully visible", async () => {
    mocks.messages = Array.from({ length: 11 }, (_, index) => ({
      id: `history-${index + 1}`,
      history: true,
    }));
    apiMocks.getChatHistory.mockResolvedValue({
      messages: Array.from({ length: 20 }, (_, index) => ({
        id: `archived-${index + 1}`,
      })),
      boundaries: [],
      has_more: true,
      next_cursor: "cursor-2",
    });

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    const bubbleList = screen.getByTestId("bubble-list");

    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => expect(bubbleList).toHaveTextContent("11"));
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();

    await flushHistoryLayout();
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledWith(
        "chat-real-1",
        null,
        20,
      );
    });
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    expect(bubbleList).toHaveTextContent("31");
  });

  it("continues archive paging with the returned cursor", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory
      .mockResolvedValueOnce({
        messages: [{ id: "archived-page-1" }],
        boundaries: [],
        has_more: true,
        next_cursor: "cursor-2",
      })
      .mockResolvedValueOnce({
        messages: [{ id: "archived-page-2" }],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    const bubbleList = screen.getByTestId("bubble-list");

    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenNthCalledWith(
        1,
        "chat-real-1",
        null,
        20,
      );
    });

    await flushHistoryLayout();
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenNthCalledWith(
        2,
        "chat-real-1",
        "cursor-2",
        20,
      );
    });
    expect(screen.getByRole("status")).toHaveTextContent("已到达会话开始处");
  });

  it("does not request archived history until normal scrolling enters the preload range", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -300;
    fireEvent.scroll(bubbleList);

    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
  });

  it("loads archived history with the resolved backend chat ID while normal scrolling nears the top", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    let resolveHistory!: (page: {
      messages: Array<{ id: string }>;
      boundaries: never[];
      has_more: boolean;
      next_cursor: string | null;
    }) => void;
    apiMocks.getChatHistory.mockReturnValue(
      new Promise((resolve) => {
        resolveHistory = resolve;
      }),
    );

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);

    expect(screen.getByRole("status")).toHaveTextContent("正在加载更早的消息…");
    expect(screen.getByRole("status")).toHaveStyle({ flexShrink: "0" });
    expect(screen.getByTestId("bubble-list")).toContainElement(
      screen.getByRole("status"),
    );

    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledWith(
        "chat-real-1",
        null,
        20,
      );
    });
    resolveHistory({
      messages: [{ id: "archived-message" }],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });
    await waitFor(() => {
      expect(mocks.messages).toEqual([
        { id: "archived-message", history: true },
        { id: "online-message", history: true },
      ]);
    });
    expect(screen.getByRole("status")).toHaveTextContent("已到达会话开始处");
    expect(mocks.messages).toEqual([
      { id: "archived-message", history: true },
      { id: "online-message", history: true },
    ]);
  });

  it("preserves the visible content within a response when earlier history is inserted", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [{ id: "archived-message" }],
      boundaries: [],
      has_more: true,
      next_cursor: "next-page",
    });
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const list = screen.getByTestId("bubble-list");
    const content = document.createElement("div");
    content.dataset.historyAnchorId = "visible-runtime-message";
    let contentTop = 120;
    content.getBoundingClientRect = () => ({
      top: contentTop,
      bottom: contentTop + 48,
      height: 48,
      width: 400,
      x: 0,
      y: contentTop,
      left: 0,
      right: 400,
      toJSON: () => ({}),
    });
    document.getElementById("online-message")!.appendChild(content);
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await waitFor(() => expect(mocks.messages).toHaveLength(2));
    mocks.anchorTop = 240;
    contentTop = 160;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryFrame();
    expect(list.scrollTop).toBe(-340);
  });

  it("restores a merged process descendant instead of the response bubble", async () => {
    const actual = await vi.importActual<
      typeof import("@/pages/Chat/sessionApi")
    >("@/pages/Chat/sessionApi");
    vi.spyOn(sessionApiModule, "convertArchivedPage").mockImplementation(
      actual.convertArchivedPage,
    );
    const response = (id: string) =>
      actual.convertArchivedPage(
        [{ id: `runtime-${id}`, role: "assistant", content: id }],
        [],
      )[0];
    const older = response("online-message");
    const newer = response("newer-response");
    mocks.anchorId = older.id;
    mocks.messages = [older, newer];
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [{ id: "archive", role: "assistant", content: "earlier" }],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const list = screen.getByTestId("bubble-list");
    const rect = (top: number) => ({
      top,
      bottom: top + 48,
      height: 48,
      width: 400,
      x: 0,
      y: top,
      left: 0,
      right: 400,
      toJSON: () => ({}),
    });
    const olderElement = document.getElementById(older.id)!;
    const oldProcess = document.createElement("div");
    oldProcess.dataset.historyAnchorId = `process:${older.cards![0].data.id}`;
    oldProcess.getBoundingClientRect = () => rect(120);
    olderElement.appendChild(oldProcess);
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await waitFor(() => expect(mocks.messages).toHaveLength(1));

    mocks.anchorTop = 240;
    const process = document.createElement("div");
    process.dataset.historyAnchorId = `process:${newer.cards![0].data.id}`;
    process.getBoundingClientRect = () => rect(160);
    document.getElementById(newer.id)!.appendChild(process);
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryFrame();
    expect(list.scrollTop).toBe(-340);
  });

  it("restores the visible anchor after a post-commit history layout shift", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    let resolveHistory!: (page: {
      messages: Array<{ id: string }>;
      boundaries: never[];
      has_more: boolean;
      next_cursor: string | null;
    }) => void;
    apiMocks.getChatHistory.mockReturnValue(
      new Promise((resolve) => {
        resolveHistory = resolve;
      }),
    );
    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1);
    });

    resolveHistory({
      messages: [{ id: "archived-message" }],
      boundaries: [],
      has_more: true,
      next_cursor: "next-page",
    });
    await waitFor(() => {
      expect(mocks.messages).toHaveLength(2);
    });
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(mocks.animationFrame).toBeTypeOf("function");
    mocks.anchorTop = 140;
    await flushHistoryFrame();
    expect(bubbleList.scrollTop).toBe(-340);
  });

  it("shows the start-of-conversation state immediately for a terminal page", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("已到达会话开始处");
    });
    expect(mocks.messages).toEqual([{ id: "online-message", history: true }]);
  });

  it("keeps loaded messages and exposes a retry action when history loading fails", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory.mockRejectedValueOnce(new Error("offline"));
    apiMocks.getChatHistory.mockResolvedValueOnce({
      messages: [{ id: "archived-message" }],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("历史消息加载失败");
    });
    expect(mocks.messages).toEqual([{ id: "online-message", history: true }]);

    fireEvent.click(screen.getByRole("button", { name: "重试加载历史消息" }));
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2);
    });
    expect(apiMocks.getChatHistory).toHaveBeenNthCalledWith(
      1,
      "chat-real-1",
      null,
      20,
    );
    expect(apiMocks.getChatHistory).toHaveBeenNthCalledWith(
      2,
      "chat-real-1",
      null,
      20,
    );
    expect(mocks.messages).toEqual([
      { id: "archived-message", history: true },
      { id: "online-message", history: true },
    ]);
  });

  it("does not pull the reader back to the latest message after they enter history", () => {
    mocks.messages = [{ id: "online-message" }];
    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    mocks.messages = [{ id: "online-message" }, { id: "new-message" }];
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(apiMocks.scrollToBottom).not.toHaveBeenCalled();
  });

  it("preserves the reading anchor when a live message changes reverse-list geometry", () => {
    mocks.messages = [{ id: "online-message", history: true }];
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const node = screen.getByTestId("bubble-list");
    node.scrollTop = -380;
    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    mocks.messages = [...mocks.messages, { id: "live-message" }];
    mocks.anchorTop = 60;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    expect(node.scrollTop).toBe(-420);
    expect(apiMocks.scrollToBottom).not.toHaveBeenCalled();
  });

  it("restores the anchor when Markdown finishes layout after the first frame", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const node = screen.getByTestId("bubble-list");
    node.scrollTop = -380;
    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    mocks.messages = [...mocks.messages, { id: "live-message" }];
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryFrame();
    mocks.anchorTop = 76;
    await flushHistoryFrame();
    expect(node.scrollTop).toBe(-404);
  });

  it("preserves the anchor for late content resize until the reader moves", async () => {
    let notifyResize!: () => void;
    const disconnect = vi.fn();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          notifyResize = callback;
        }
        observe = vi.fn();
        disconnect = disconnect;
      },
    );
    mocks.messages = [{ id: "online-message", history: true }];
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const node = screen.getByTestId("bubble-list");
    node.scrollTop = -380;
    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    mocks.messages = [...mocks.messages, { id: "live-message" }];
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    await flushHistoryLayout();
    mocks.anchorTop = 76;
    act(() => notifyResize?.());
    expect(node.scrollTop).toBe(-404);
    node.scrollTop = -300;
    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    mocks.anchorTop = 50;
    act(() => notifyResize());
    expect(node.scrollTop).toBe(-300);
    mocks.isSessionLoading = true;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    expect(disconnect).toHaveBeenCalled();
    rendered.unmount();
  });

  it("ignores a delayed programmatic scroll after anchor recovery finishes", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const node = screen.getByTestId("bubble-list");
    node.scrollTop = -380;
    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    mocks.messages = [...mocks.messages, { id: "live-message" }];
    mocks.anchorTop = 60;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    mocks.anchorTop = 100;
    await flushHistoryLayout();
    fireEvent.scroll(node);
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
    node.scrollTop = -500;
    fireEvent.scroll(node);
    expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1);
  });

  it("does not restore an old anchor after the reader moves between commit and the next frame", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    const rendered = render(<MessageList onSubmit={vi.fn()} />);
    const node = screen.getByTestId("bubble-list");
    node.scrollTop = -380;
    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    mocks.messages = [...mocks.messages, { id: "live-message" }];
    mocks.anchorTop = 60;
    rendered.rerender(<MessageList onSubmit={vi.fn()} />);
    node.scrollTop = -300;
    mocks.anchorTop = -60;
    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    await flushHistoryFrame();
    expect(node.scrollTop).toBe(-300);
  });

  it("holds the progression lock through retry layout recovery", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({
        messages: [{ id: "older" }],
        boundaries: [],
        has_more: true,
        next_cursor: "next",
      });
    render(<MessageList onSubmit={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    await flushHistoryLayout();
    fireEvent.click(screen.getByRole("button", { name: "重试加载历史消息" }));
    await waitFor(() =>
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "加载更早消息" }),
      ).toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
    expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2);
  });

  it("keeps an existing compaction divider when its archive page is loaded", async () => {
    mocks.messages = [
      { id: "conversation-compaction-boundary-1", history: true },
      { id: "online-message", history: true },
    ];
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [{ id: "archived-message" }],
      boundaries: [{ id: "boundary-1" }],
      has_more: false,
      next_cursor: null,
    });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);

    await waitFor(() => {
      expect(mocks.messages).toEqual([
        { id: "archived-message", history: true },
        { id: "conversation-compaction-boundary-1", history: true },
        { id: "online-message", history: true },
      ]);
    });
  });

  it("keeps archive paging available when compaction invalidates an in-flight page", async () => {
    mocks.messages = [{ id: "online-message" }];
    let resolveHistory!: (value: {
      messages: Array<{ id: string }>;
      boundaries: never[];
      has_more: boolean;
      next_cursor: string | null;
    }) => void;
    apiMocks.getChatHistory
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveHistory = resolve;
        }),
      )
      .mockResolvedValueOnce({
        messages: [],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1);
    });

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    expect(apiMocks.getSession).not.toHaveBeenCalled();

    resolveHistory({
      messages: [{ id: "stale-archived-message" }],
      boundaries: [],
      has_more: true,
      next_cursor: "stale-cursor",
    });
    await act(async () => {
      await Promise.resolve();
    });

    bubbleList.scrollTop = -300;
    fireEvent.scroll(bubbleList);
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2);
    });
  });

  it("keeps loaded archive messages when a compaction snapshot is accepted", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "current-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "current-response",
        role: "assistant",
        msgStatus: "generating",
        cards: [{ code: "Response", data: { output: ["answer"] } }],
      },
    ];
    apiMocks.getChatHistory.mockResolvedValueOnce({
      messages: [{ id: "older-user", role: "user", content: "older question" }],
      boundaries: [],
      has_more: true,
      next_cursor: "after-first-page",
    });
    apiMocks.getSession.mockResolvedValue({
      generating: false,
      messages: [
        {
          id: "current-request",
          role: "user",
          cards: [{ code: "Request", data: { input: "current question" } }],
        },
        {
          id: "current-response",
          role: "assistant",
          msgStatus: "finished",
          cards: [{ code: "Response", data: { output: ["answer"] } }],
        },
        {
          id: "conversation-compaction-boundary-1",
          cards: [
            {
              code: "ConversationCompactionBoundary",
              data: { id: "boundary-1" },
            },
          ],
        },
      ],
    });

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: {
          chat_id: "chat-real-1",
          boundary: { id: "boundary-1" },
        },
      }),
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await act(async () => {
      await Promise.resolve();
    });
    expect(mocks.messages.some((message) => message.id === "older-user")).toBe(
      true,
    );
    mocks.messages = mocks.messages.map((message) =>
      message.id === "current-response"
        ? { ...message, msgStatus: "finished" }
        : message,
    );
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(apiMocks.getSession).toHaveBeenCalledTimes(1);
    expect(mocks.messages.some((message) => message.id === "older-user")).toBe(
      true,
    );
  });

  it("keeps previously loaded archives before newly published pages after compaction", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "current-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current" } }],
      },
    ];
    const snapshot = { generating: false, messages: mocks.messages };
    apiMocks.getSession.mockResolvedValue(snapshot);
    apiMocks.getChatHistory
      .mockResolvedValueOnce({
        messages: [{ id: "old-user", role: "user" }],
        boundaries: [],
        has_more: true,
        next_cursor: "old-page",
      })
      .mockResolvedValueOnce({
        messages: [{ id: "newly-archived-user", role: "user" }],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
    const view = () => <MessageList onSubmit={vi.fn()} />;
    const rendered = render(view());
    const list = screen.getByTestId("bubble-list");
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await act(async () => {
      await Promise.resolve();
    });
    rendered.rerender(view());
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });
    await flushHistoryLayout();
    list.scrollTop = -200;
    fireEvent.scroll(list);
    list.scrollTop = -380;
    fireEvent.scroll(list);
    await act(async () => {
      await Promise.resolve();
    });
    rendered.rerender(view());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2);
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "old-user",
      "newly-archived-user",
      "current-request",
    ]);
  });

  it("does not replace a finished local turn when the compaction snapshot omits its tail", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "local-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ];
    apiMocks.getSession.mockResolvedValue({
      generating: false,
      messages: [
        {
          id: "stale-persisted-message",
          role: "assistant",
          cards: [{ code: "Response", data: { output: "older answer" } }],
        },
      ],
    });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );

    expect(apiMocks.getSession).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(apiMocks.getSession).toHaveBeenCalledWith("chat-1");
    expect(mocks.messages).toEqual([
      {
        id: "local-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ]);
    vi.useRealTimers();
  });

  it("requires the ordered local request-response suffix before accepting a compaction snapshot", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "current-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "current-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "same answer" } }],
      },
    ];
    apiMocks.getSession
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "old-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "older question" } }],
          },
          {
            id: "old-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "same answer" } }],
          },
        ],
      })
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "persisted-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "current question" } }],
          },
          {
            id: "persisted-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "same answer" } }],
          },
        ],
      });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "current-request",
      "current-response",
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "persisted-request",
      "persisted-response",
    ]);
  });

  it("requires the compaction boundary in the snapshot when the online tail is unchanged", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "current-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "current-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "same answer" } }],
      },
    ];
    apiMocks.getSession
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "stale-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "current question" } }],
          },
          {
            id: "stale-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "same answer" } }],
          },
        ],
      })
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "persisted-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "current question" } }],
          },
          {
            id: "persisted-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "same answer" } }],
          },
          {
            id: "conversation-compaction-boundary-1",
            role: "assistant",
            cards: [
              {
                code: "ConversationCompactionBoundary",
                data: { id: "boundary-1" },
              },
            ],
          },
        ],
      });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: {
          chat_id: "chat-real-1",
          boundary: { id: "boundary-1" },
        },
      }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "current-request",
      "current-response",
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "persisted-request",
      "persisted-response",
      "conversation-compaction-boundary-1",
    ]);
  });

  it("keeps retrying compaction refresh until a delayed persisted snapshot confirms the local tail", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "current-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
    ];
    apiMocks.getSession
      .mockResolvedValueOnce({ generating: false, messages: [] })
      .mockResolvedValueOnce({ generating: false, messages: [] })
      .mockResolvedValueOnce({ generating: false, messages: [] })
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "persisted-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "current question" } }],
          },
        ],
      });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(apiMocks.getSession).toHaveBeenCalledTimes(4);
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "persisted-request",
    ]);
  });

  it("refreshes a pending stream compaction only after a non-generating snapshot confirms the local tail", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "generating",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ];
    apiMocks.getSession
      .mockResolvedValueOnce({ generating: true, messages: [] })
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "persisted-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "current answer" } }],
          },
        ],
      });

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    expect(apiMocks.getSession).not.toHaveBeenCalled();

    mocks.messages = [
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ];
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
      await Promise.resolve();
    });
    expect(apiMocks.getSession).toHaveBeenCalledTimes(1);
    expect(mocks.messages).toEqual([
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
      await Promise.resolve();
    });
    expect(apiMocks.getSession).toHaveBeenCalledTimes(2);
    expect(mocks.messages).toEqual([
      {
        id: "persisted-response",
        role: "assistant",
        history: true,
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ]);
    vi.useRealTimers();
  });

  it("does not replace a new local request when a compaction snapshot is in flight", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "existing-message",
        role: "user",
        cards: [{ code: "Request", data: { input: "existing" } }],
      },
    ];
    let resolveSession!: (value: {
      generating: boolean;
      messages: unknown[];
    }) => void;
    apiMocks.getSession.mockReturnValue(
      new Promise((resolve) => {
        resolveSession = resolve;
      }),
    );

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(apiMocks.getSession).toHaveBeenCalledWith("chat-1");

    mocks.messages = [
      {
        id: "existing-message",
        role: "user",
        cards: [{ code: "Request", data: { input: "existing" } }],
      },
      {
        id: "new-local-response",
        role: "assistant",
        msgStatus: "generating",
        cards: [{ code: "Response", data: { output: "new" } }],
      },
    ];
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    resolveSession({ generating: false, messages: [] });

    await act(async () => {
      await Promise.resolve();
    });
    expect(mocks.messages).toHaveLength(2);
    expect(mocks.messages[1]?.id).toBe("new-local-response");
    vi.useRealTimers();
  });

  it("keeps the current session visible when a compaction boundary arrives", () => {
    mocks.messages = [{ id: "old-message" }];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );

    expect(apiMocks.getSession).not.toHaveBeenCalled();
    expect(mocks.messages).toEqual([{ id: "old-message" }]);
  });

  it("does not let an earlier compaction refresh overwrite a switched session", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "message-for-chat-1",
        role: "user",
        cards: [{ code: "Request", data: { input: "chat-1" } }],
      },
    ];
    let resolveSession!: (value: {
      generating: boolean;
      messages: unknown[];
    }) => void;
    apiMocks.getSession.mockReturnValue(
      new Promise((resolve) => {
        resolveSession = resolve;
      }),
    );
    apiMocks.getChatIdForSession.mockImplementation((sessionId: string) =>
      sessionId === "chat-1" ? "chat-real-1" : "chat-real-2",
    );

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(apiMocks.getSession).toHaveBeenCalledWith("chat-1");

    mocks.currentSessionId = "chat-2";
    mocks.messages = [{ id: "message-for-chat-2" }];
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    resolveSession({ generating: false, messages: [] });

    await act(async () => {
      await Promise.resolve();
    });
    expect(mocks.messages).toEqual([{ id: "message-for-chat-2" }]);
    vi.useRealTimers();
  });
});
