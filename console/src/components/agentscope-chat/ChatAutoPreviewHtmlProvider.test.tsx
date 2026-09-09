import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatAutoPreviewHtmlProvider } from "./ChatAutoPreviewHtmlProvider";
import { ChatAnywhereMessagesContext } from "./AgentScopeRuntimeWebUI/core/Context/ChatAnywhereMessagesContext";
import { ChatAnywhereSessionsContext } from "./AgentScopeRuntimeWebUI/core/Context/ChatAnywhereSessionsContext";
import DownloadFileCard from "./DownloadFileCard";
import BubbleList from "./Bubble/BubbleList";
import type { IAgentScopeRuntimeWebUIMessage as Message } from "./AgentScopeRuntimeWebUI/core/types/IMessages";
import type { IAgentScopeRuntimeWebUISessionsContext } from "./AgentScopeRuntimeWebUI/core/types/ISessions";

vi.mock("@agentscope-ai/icons", () => ({ SparkDownloadLine: () => null }));

vi.mock(
  "./AgentScopeRuntimeWebUI/core/Context/ChatAnywhereOptionsContext",
  () => ({
    useChatAnywhereOptions: () => undefined,
  }),
);

vi.mock(
  "./AgentScopeRuntimeWebUI/core/Context/ChatAnywhereMessagesContext",
  async () => {
    const { createContext } = await import("use-context-selector");
    return { ChatAnywhereMessagesContext: createContext({ messages: [] }) };
  },
);
vi.mock(
  "./AgentScopeRuntimeWebUI/core/Context/ChatAnywhereSessionsContext",
  async () => {
    const { createContext } = await import("use-context-selector");
    return {
      ChatAnywhereSessionsContext: createContext({ isSessionLoading: false }),
    };
  },
);
vi.mock("@/components/agentscope-chat", () => ({
  useProviderContext: () => ({ getPrefixCls: (name: string) => name }),
}));
vi.mock("./Bubble/style/list", () => ({ default: () => null }));
vi.mock("./Bubble/ScrollToBottom", () => ({ default: () => null }));
vi.mock("./Bubble/Bubble", () => ({
  default: ({ content }: { content: string }) => (
    <DownloadFileCard url={content} />
  ),
}));
vi.mock("./FilePreviewModal", () => ({
  default: ({ open, fileName }: { open: boolean; fileName: string }) =>
    open ? <div role="dialog">{fileName}</div> : null,
}));
vi.mock("./FilePreviewDrawer", () => ({ default: () => null }));

const oldUrl = "https://example.test/A-auto-preview.html";
const newUrl = "https://example.test/B-auto-preview.html";
function report(id: string, url: string): Message {
  return {
    id,
    role: "assistant",
    msgStatus: "finished",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          output: [
            {
              role: "assistant",
              type: "message",
              content: [{ type: "file", file_url: url }],
            },
          ],
        },
      },
    ],
  };
}
function textReport(id: string, text: string): Message {
  return {
    id,
    role: "assistant",
    msgStatus: "finished",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          output: [
            {
              role: "assistant",
              type: "message",
              content: [{ type: "text", text }],
            },
          ],
        },
      },
    ],
  };
}
function Fixture({
  loading = false,
  messages = [report("old", oldUrl), report("new", newUrl)],
  urls = [newUrl, oldUrl],
  onConsumed = () => {},
  triggerKey = 1,
}: {
  loading?: boolean;
  messages?: Message[];
  urls?: string[];
  onConsumed?: () => void;
  triggerKey?: number;
}) {
  return (
    <ChatAnywhereMessagesContext.Provider
      value={{ messages, getMessages: () => messages, setMessages: () => {} }}
    >
      <ChatAnywhereSessionsContext.Provider
        value={
          {
            isSessionLoading: loading,
          } as IAgentScopeRuntimeWebUISessionsContext
        }
      >
        <ChatAutoPreviewHtmlProvider
          triggerKey={triggerKey}
          onConsumed={onConsumed}
        >
          <BubbleList
            order="desc"
            pagination={false}
            items={urls.map((url) => ({ id: url, content: url }))}
          />
        </ChatAutoPreviewHtmlProvider>
      </ChatAnywhereSessionsContext.Provider>
    </ChatAnywhereMessagesContext.Provider>
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
describe("task report auto-preview through a reverse-ordered BubbleList", () => {
  it("does not scan messages without an auto-preview trigger", () => {
    const messages = new Proxy([] as Message[], {
      get(target, property, receiver) {
        if (property === Symbol.iterator) {
          throw new Error("messages should not be scanned");
        }
        return Reflect.get(target, property, receiver);
      },
    });

    expect(() =>
      render(<Fixture messages={messages} triggerKey={0} urls={[]} />),
    ).not.toThrow();
  });

  it.each([
    [newUrl, oldUrl],
    [oldUrl, newUrl],
  ])("opens B for mount order %s, %s", (first, second) => {
    vi.useFakeTimers();
    render(<Fixture urls={[first, second]} />);
    act(() => vi.advanceTimersByTime(150));
    expect(screen.getByRole("dialog")).toHaveTextContent("B-auto-preview.html");
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });

  it("opens a newer dynamic file card supported by the original behavior", () => {
    vi.useFakeTimers();
    const dynamic =
      "https://example.test/latest.html?resultId=new&templateId=1";
    render(
      <Fixture
        messages={[report("old", oldUrl), report("new", dynamic)]}
        urls={[dynamic, oldUrl]}
      />,
    );
    act(() => vi.advanceTimersByTime(150));
    expect(screen.getByRole("dialog")).toHaveTextContent("latest.html");
  });

  it("ignores dynamic text that does not render an auto-preview file card", () => {
    vi.useFakeTimers();
    const dynamic =
      "https://example.test/latest.html?resultId=new&templateId=1";
    render(
      <Fixture
        messages={[report("old", oldUrl), textReport("new", dynamic)]}
        urls={[oldUrl]}
      />,
    );
    act(() => vi.advanceTimersByTime(150));
    expect(screen.getByRole("dialog")).toHaveTextContent("A-auto-preview.html");
  });

  it("matches a URL encoded by Markdown to the selected report", () => {
    vi.useFakeTimers();
    const raw = "https://example.test/最新-auto-preview.html";
    render(
      <Fixture
        messages={[report("old", oldUrl), report("new", raw)]}
        urls={[encodeURI(raw), oldUrl]}
      />,
    );
    act(() => vi.advanceTimersByTime(150));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "最新-auto-preview.html",
    );
  });

  it("waits for B to mount without allowing an already mounted A to consume the preview", () => {
    vi.useFakeTimers();
    const view = render(<Fixture urls={[oldUrl]} />);
    act(() => vi.advanceTimersByTime(400));
    expect(screen.queryByRole("dialog")).toBeNull();
    view.rerender(<Fixture />);
    act(() => vi.advanceTimersByTime(150));
    expect(screen.getByRole("dialog")).toHaveTextContent("B-auto-preview.html");
  });

  it("waits beyond the old five-second timeout for session loading to finish", () => {
    vi.useFakeTimers();
    const view = render(
      <Fixture loading messages={[report("old", oldUrl)]} urls={[oldUrl]} />,
    );
    act(() => vi.advanceTimersByTime(6000));
    expect(screen.queryByRole("dialog")).toBeNull();
    view.rerender(<Fixture />);
    act(() => vi.advanceTimersByTime(150));
    expect(screen.getByRole("dialog")).toHaveTextContent("B-auto-preview.html");
  });

  it("does not auto-open A after a parent rerender changes the consumed callback", () => {
    vi.useFakeTimers();
    const consumed = vi.fn();
    const view = render(<Fixture onConsumed={consumed} />);
    act(() => vi.advanceTimersByTime(150));
    view.rerender(
      <Fixture
        messages={[report("old", oldUrl)]}
        urls={[oldUrl]}
        onConsumed={() => consumed()}
      />,
    );
    act(() => vi.advanceTimersByTime(150));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(consumed).toHaveBeenCalledTimes(1);
  });

  it("opens the latest available report while another message is generating", () => {
    vi.useFakeTimers();
    render(
      <Fixture
        messages={[
          report("old", oldUrl),
          { ...report("new", newUrl), msgStatus: "generating" },
        ]}
      />,
    );
    act(() => vi.advanceTimersByTime(150));
    expect(screen.getByRole("dialog")).toHaveTextContent("B-auto-preview.html");
  });
});
