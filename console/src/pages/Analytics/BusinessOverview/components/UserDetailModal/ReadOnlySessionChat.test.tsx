import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { useAgentStore } from "@/stores/agentStore";
import ReadOnlySessionChat from "./ReadOnlySessionChat";

const capturedCards = vi.hoisted(() => ({
  current: {} as Record<
    string,
    (props: { data: Record<string, unknown> }) => ReactElement
  >,
}));

const tracingApiMock = vi.hoisted(() => ({
  getUserChat: vi.fn(),
  getUserChatHistory: vi.fn(),
}));
const htmlPreviewTrackingValues = vi.hoisted(
  () => [] as Array<{ disableEventRecording?: boolean }>,
);

vi.mock("../../../../../api/modules/tracing", () => ({
  tracingApi: tracingApiMock,
}));

vi.mock("../../../../Chat/sessionApi", () => ({
  convertMessages: (messages: unknown[]) => messages,
  convertMessagesForSession: (messages: unknown[]) => messages,
  withArchiveBoundaries: (messages: unknown[]) => messages,
  convertArchivedPage: (messages: unknown[]) => messages,
}));

vi.mock("../../../../Chat/components/RuntimeRequestCard", () => ({
  default: () => <div data-testid="runtime-request-card" />,
}));

vi.mock("../../../../Chat/components/RuntimeResponseCard", () => ({
  default: ({ data }: { data: { planReviewCard?: unknown } }) => (
    <div data-testid="runtime-response-card">
      {data.planReviewCard ? <button>确认计划</button> : "只读回答"}
    </div>
  ),
}));

vi.mock("../../../../../components/ConversationQuickNav", () => ({
  default: ({ messages }: { messages?: unknown[] }) => (
    <div data-testid="conversation-quick-nav">{messages?.length ?? 0}</div>
  ),
}));

vi.mock("@/components/agentscope-chat/HtmlPreviewTrackingContext", () => ({
  HtmlPreviewTrackingProvider: ({
    value,
    children,
  }: {
    value: { disableEventRecording?: boolean };
    children: ReactNode;
  }) => {
    htmlPreviewTrackingValues.push(value);
    return <div data-testid="html-preview-tracking-provider">{children}</div>;
  },
}));

vi.mock("@/components/agentscope-chat", () => ({
  AgentScopeRuntimeWebUIComposedProvider: ({
    children,
    cards,
  }: {
    children: ReactNode;
    cards: typeof capturedCards.current;
  }) => {
    capturedCards.current = cards;
    return <div data-testid="chat-provider">{children}</div>;
  },
  Bubble: {
    List: ({
      items,
      topContent,
    }: {
      items: unknown[];
      topContent?: ReactNode;
    }) => (
      <div>
        {topContent}
        <div data-testid="bubble-list">{items.length}</div>
      </div>
    ),
  },
}));

beforeEach(() => {
  vi.resetAllMocks();
  htmlPreviewTrackingValues.length = 0;
});
afterEach(cleanup);

describe("ReadOnlySessionChat", () => {
  it("loads history with mapped chat id", async () => {
    tracingApiMock.getUserChat.mockResolvedValue({
      messages: [{ id: "message-1", role: "assistant", content: [] }],
    });

    render(
      <ReadOnlySessionChat
        selectedSessionId="cron-task:job-1"
        chatIdBySessionId={{ "cron-task:job-1": "chat-uuid-1" }}
        targetUserId="user-001"
      />,
    );

    await waitFor(() => {
      expect(tracingApiMock.getUserChat).toHaveBeenCalledWith(
        "user-001",
        "chat-uuid-1",
      );
    });
    expect(
      await screen.findByTestId("conversation-quick-nav"),
    ).toHaveTextContent("1");
    expect(
      screen.getByTestId("html-preview-tracking-provider"),
    ).toBeInTheDocument();
    expect(htmlPreviewTrackingValues).toContainEqual({
      disableEventRecording: true,
    });
  });

  it("does not call chat detail when mapping is missing", async () => {
    render(
      <ReadOnlySessionChat
        selectedSessionId="cron-task:missing-job"
        chatIdBySessionId={{}}
      />,
    );

    await screen.findByText("未找到该会话的聊天映射或目标用户");
    expect(tracingApiMock.getUserChat).not.toHaveBeenCalled();
  });
});

const props = {
  selectedSessionId: "s",
  targetUserId: "target",
  chatIdBySessionId: { s: "chat" },
};
const archived = (id: string) => ({ id, role: "user", cards: [] });

it("loads archive-only history and advances cursor; retries without losing messages", async () => {
  tracingApiMock.getUserChat.mockResolvedValue({
    messages: [],
    archive: { has_more: true },
  });
  tracingApiMock.getUserChatHistory
    .mockResolvedValueOnce({
      messages: [archived("old")],
      next_cursor: "c1",
      has_more: true,
    })
    .mockRejectedValueOnce(new Error("network"))
    .mockResolvedValueOnce({
      messages: [archived("old"), archived("older")],
      has_more: false,
    });
  render(<ReadOnlySessionChat {...props} />);
  fireEvent.click(await screen.findByText("加载更早历史"));
  await waitFor(() =>
    expect(screen.getByTestId("bubble-list")).toHaveTextContent("1"),
  );
  expect(tracingApiMock.getUserChatHistory).toHaveBeenLastCalledWith(
    "target",
    "chat",
    null,
    20,
  );
  fireEvent.click(screen.getByText("加载更早历史"));
  fireEvent.click(await screen.findByText("重试加载历史"));
  await screen.findByText("已到达会话开始处");
  expect(tracingApiMock.getUserChatHistory).toHaveBeenLastCalledWith(
    "target",
    "chat",
    "c1",
    20,
  );
  expect(screen.getByTestId("bubble-list")).toHaveTextContent("2");
});

it("rejects a non-advancing cursor", async () => {
  tracingApiMock.getUserChat.mockResolvedValue({
    messages: [],
    archive: { has_more: true },
  });
  tracingApiMock.getUserChatHistory.mockResolvedValue({
    messages: [archived("old")],
    has_more: true,
    next_cursor: null,
  });
  render(<ReadOnlySessionChat {...props} />);
  fireEvent.click(await screen.findByText("加载更早历史"));
  await screen.findByRole("alert");
  expect(screen.getByTestId("bubble-list")).toHaveTextContent("0");
});

it("ignores old archive responses after switching target and prevents concurrent loads", async () => {
  tracingApiMock.getUserChat.mockResolvedValue({
    messages: [archived("current")],
    archive: { has_more: true },
  });
  let resolve!: (value: unknown) => void;
  tracingApiMock.getUserChatHistory.mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const { rerender } = render(<ReadOnlySessionChat {...props} />);
  const load = await screen.findByText("加载更早历史");
  const initialCalls = tracingApiMock.getUserChatHistory.mock.calls.length;
  fireEvent.click(load);
  fireEvent.click(load);
  expect(tracingApiMock.getUserChatHistory).toHaveBeenCalledTimes(
    initialCalls + 1,
  );
  rerender(<ReadOnlySessionChat {...props} targetUserId="other" />);
  await waitFor(() =>
    expect(tracingApiMock.getUserChat).toHaveBeenLastCalledWith(
      "other",
      "chat",
    ),
  );
  await act(async () =>
    resolve({ messages: [archived("stale")], has_more: false }),
  );
  expect(screen.getByTestId("bubble-list")).toHaveTextContent("1");
  expect(screen.queryByText("已到达会话开始处")).not.toBeInTheDocument();
});

it("shows a detail error with retry instead of empty history", async () => {
  tracingApiMock.getUserChat
    .mockRejectedValueOnce(new Error("network"))
    .mockResolvedValueOnce({ messages: [archived("restored")] });
  render(<ReadOnlySessionChat {...props} />);
  fireEvent.click(await screen.findByRole("button", { name: "重 试" }));
  await waitFor(() =>
    expect(screen.getByTestId("bubble-list")).toHaveTextContent("1"),
  );
});

it.each(["pending", "submitted"])(
  "preserves a single read-only plan snapshot for %s reviews",
  async (status) => {
    tracingApiMock.getUserChat.mockResolvedValue({
      messages: [archived("answer")],
    });
    render(<ReadOnlySessionChat {...props} />);
    await screen.findByTestId("chat-provider");
    const Response = capturedCards.current.AgentScopeRuntimeResponseCard;
    const Approval = capturedCards.current.ApprovalAction;
    const Interaction = capturedCards.current.PlanInteraction;
    const plan = {
      card_type: "plan_review",
      plan_id: "plan-1",
      title: "修复登录异常",
      summary: "检查认证流程",
      steps: ["定位认证失败原因"],
      risks: ["已有会话可能受影响"],
      verification: ["执行登录回归测试"],
      status,
      ...(status === "submitted"
        ? { submitted_decision: "revise", feedback: "补充异常场景" }
        : {}),
    };
    render(
      <>
        <Response data={{ planReviewCard: plan }} />
        <Interaction data={plan} />
        <Approval data={{ status: "pending" }} />
      </>,
    );
    expect(
      screen.queryByRole("button", { name: "确认计划" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("只读回答")).toBeInTheDocument();
    expect(screen.getAllByRole("region", { name: plan.title })).toHaveLength(1);
    for (const text of [
      plan.summary,
      ...plan.steps,
      ...plan.risks,
      ...plan.verification,
    ]) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
    expect(
      screen.getByText(status === "submitted" ? "已要求修改" : "计划待确认"),
    ).toBeInTheDocument();
    if (status === "submitted")
      expect(screen.getByText("补充异常场景")).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByText("工具审批状态：pending")).toBeInTheDocument();
  },
);

it("resets the viewer when the selected Agent changes", async () => {
  tracingApiMock.getUserChat.mockResolvedValue({
    messages: [archived("current")],
  });
  render(<ReadOnlySessionChat {...props} />);
  await screen.findByTestId("bubble-list");
  const count = tracingApiMock.getUserChat.mock.calls.length;
  act(() => useAgentStore.getState().setSelectedAgent("another"));
  await waitFor(() =>
    expect(tracingApiMock.getUserChat).toHaveBeenCalledTimes(count + 1),
  );
  act(() => useAgentStore.getState().setSelectedAgent("default"));
});
