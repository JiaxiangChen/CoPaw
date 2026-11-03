import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { BubbleListRef } from "@/components/agentscope-chat/Bubble/BubbleList";
import type { IAgentScopeRuntimeWebUIMessage } from "@/components/agentscope-chat";
import ReadOnlySessionChat from "./ReadOnlySessionChat";

const api = vi.hoisted(() => ({
  getUserChat: vi.fn(),
  getUserChatHistory: vi.fn(),
}));
const metrics = vi.hoisted(() => ({ height: 1000, viewport: 400 }));
vi.mock("../../../../../api/modules/tracing", () => ({ tracingApi: api }));
vi.mock("../../../../Chat/components/RuntimeRequestCard", () => ({
  default: () => null,
}));
vi.mock("../../../../Chat/components/RuntimeResponseCard", () => ({
  default: () => null,
}));
vi.mock("../../../../../components/ConversationQuickNav", () => ({
  default: () => null,
}));
vi.mock("@/components/agentscope-chat/HtmlPreviewTrackingContext", () => ({
  HtmlPreviewTrackingProvider: ({ children }: { children: ReactNode }) =>
    children,
}));
vi.mock("@/components/agentscope-chat", async () => {
  const { forwardRef, useImperativeHandle, useRef } = await import("react");
  const List = forwardRef<
    BubbleListRef,
    { items: IAgentScopeRuntimeWebUIMessage[]; topContent: ReactNode }
  >(({ items, topContent }, ref) => {
    const element = useRef<HTMLDivElement | null>(null);
    useImperativeHandle(
      ref,
      () => ({
        getScrollElement: () => element.current,
        scrollToBottom: () => {},
      }),
      [],
    );
    return (
      <div
        data-testid="scroll"
        ref={(node) => {
          element.current = node;
          if (node)
            Object.defineProperties(node, {
              clientHeight: { configurable: true, get: () => metrics.viewport },
              scrollHeight: { configurable: true, get: () => metrics.height },
            });
        }}
      >
        {topContent}
        <output data-testid="count">{items.length}</output>
      </div>
    );
  });
  return {
    AgentScopeRuntimeWebUIComposedProvider: ({
      children,
    }: {
      children: ReactNode;
    }) => children,
    Bubble: { List },
  };
});

const props = {
  selectedSessionId: "s",
  chatIdBySessionId: { s: "chat" },
  targetUserId: "target",
};
const message = (id: string) => ({ id, role: "user", content: id });
beforeEach(() => {
  vi.resetAllMocks();
  metrics.height = 1000;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
    window.setTimeout(() => callback(performance.now()), 0),
  );
  vi.stubGlobal("cancelAnimationFrame", (id: number) =>
    window.clearTimeout(id),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("binds upward scrolling to the shared three-step budget and exhausts locals before archives", async () => {
  api.getUserChat.mockResolvedValue({
    messages: Array.from({ length: 55 }, (_, i) => message(`local-${i}`)),
    archive: { has_more: true },
  });
  api.getUserChatHistory.mockResolvedValue({
    messages: [message("old")],
    has_more: false,
  });
  render(<ReadOnlySessionChat {...props} />);
  expect(await screen.findByTestId("count")).toHaveTextContent("10");
  expect(api.getUserChatHistory).not.toHaveBeenCalled();
  const scroll = screen.getByTestId("scroll");
  act(() => {
    scroll.scrollTop = -590;
    fireEvent.scroll(scroll);
  });
  await waitFor(() =>
    expect(screen.getByTestId("count")).toHaveTextContent("40"),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "加载更早消息" }),
    ).not.toBeDisabled(),
  );
  expect(api.getUserChatHistory).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
  await screen.findByText("已到达会话开始处");
  expect(screen.getByTestId("count")).toHaveTextContent("56");
  expect(api.getUserChatHistory).toHaveBeenCalledExactlyOnceWith(
    "target",
    "chat",
    null,
    20,
  );
});

it("automatically fills a short viewport with at most three archive pages", async () => {
  metrics.height = 300;
  api.getUserChat.mockResolvedValue({
    messages: [message("current")],
    archive: { has_more: true },
  });
  let page = 0;
  api.getUserChatHistory.mockImplementation(async () => ({
    messages: [message(`old-${++page}`)],
    next_cursor: `cursor-${page}`,
    has_more: true,
  }));
  render(<ReadOnlySessionChat {...props} />);
  await waitFor(() =>
    expect(screen.getByTestId("count")).toHaveTextContent("4"),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "加载更早消息" }),
    ).not.toBeDisabled(),
  );
  expect(api.getUserChatHistory).toHaveBeenCalledTimes(3);
  expect(api.getUserChatHistory).toHaveBeenNthCalledWith(
    3,
    "target",
    "chat",
    "cursor-2",
    20,
  );
});

it("stops automatic progression on error and retries the same cursor", async () => {
  metrics.height = 300;
  api.getUserChat.mockResolvedValue({
    messages: [],
    archive: { has_more: true },
  });
  api.getUserChatHistory
    .mockRejectedValueOnce(new Error("network"))
    .mockResolvedValueOnce({ messages: [message("old")], has_more: false });
  render(<ReadOnlySessionChat {...props} />);
  await screen.findByText("历史消息加载失败");
  expect(api.getUserChatHistory).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "重试加载历史消息" }));
  await screen.findByText("已到达会话开始处");
  expect(api.getUserChatHistory).toHaveBeenCalledTimes(2);
  expect(api.getUserChatHistory).toHaveBeenNthCalledWith(
    2,
    "target",
    "chat",
    null,
    20,
  );
});

it("discards a pending automatic page after switching target users", async () => {
  metrics.height = 300;
  api.getUserChat.mockImplementation(async (target: string) => ({
    messages: [message(target)],
    archive: { has_more: target === "target" },
  }));
  let resolve!: (value: unknown) => void;
  api.getUserChatHistory.mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const { rerender } = render(<ReadOnlySessionChat {...props} />);
  await waitFor(() => expect(api.getUserChatHistory).toHaveBeenCalledTimes(1));
  rerender(<ReadOnlySessionChat {...props} targetUserId="other" />);
  await screen.findByText("已到达会话开始处");
  await act(async () =>
    resolve({
      messages: [message("stale")],
      next_cursor: "stale-cursor",
      has_more: true,
    }),
  );
  expect(screen.getByTestId("count")).toHaveTextContent("1");
  expect(api.getUserChatHistory).toHaveBeenCalledTimes(1);
});
