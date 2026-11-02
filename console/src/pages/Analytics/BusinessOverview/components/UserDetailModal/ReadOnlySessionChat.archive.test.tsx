import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import ReadOnlySessionChat from "./ReadOnlySessionChat";
import { convertMessagesForSession } from "../../../../Chat/sessionApi";

const api = vi.hoisted(() => ({
  getUserChat: vi.fn(),
  getUserChatHistory: vi.fn(),
}));
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
vi.mock("@/components/agentscope-chat", () => ({
  AgentScopeRuntimeWebUIComposedProvider: ({
    children,
  }: {
    children: ReactNode;
  }) => children,
  Bubble: {
    List: ({
      items,
      topContent,
    }: {
      items: unknown[];
      topContent: ReactNode;
    }) => (
      <div>
        {topContent}
        <output data-testid="messages">{JSON.stringify(items)}</output>
      </div>
    ),
  },
}));
afterEach(cleanup);

it("joins an archived tool call and retained result using the unchanged Chat converters", async () => {
  const result = {
    id: "result",
    role: "system",
    type: "plugin_call_output",
    content: [{ type: "data", data: { call_id: "tool-a", output: "ok" } }],
  };
  const boundary = {
    id: "boundary",
    archived_message_count: 1,
    first_message_id: "call",
    last_message_id: "call",
    created_at: "2026-10-30T00:00:00Z",
  };
  api.getUserChat.mockResolvedValue({
    messages: [result],
    archive: { has_more: true, boundaries: [boundary] },
  });
  api.getUserChatHistory.mockResolvedValue({
    messages: [
      {
        id: "call",
        role: "assistant",
        type: "plugin_call",
        content: [
          { type: "data", data: { call_id: "tool-a", arguments: "{}" } },
        ],
      },
    ],
    boundaries: [boundary],
    has_more: false,
  });
  // The public converter continues to leave history marking to its callers.
  expect(convertMessagesForSession([result])[0]).not.toHaveProperty("history");
  render(
    <ReadOnlySessionChat
      selectedSessionId="s"
      chatIdBySessionId={{ s: "chat" }}
      targetUserId="target"
    />,
  );
  fireEvent.click(await screen.findByText("加载更早历史"));
  await screen.findByText("已到达会话开始处");
  await waitFor(() => {
    const messages = JSON.parse(screen.getByTestId("messages").textContent!);
    const responses = messages.flatMap(
      (message: {
        cards: { code: string; data: { output: { id: string }[] } }[];
      }) =>
        message.cards.filter(
          (card) => card.code === "AgentScopeRuntimeResponseCard",
        ),
    );
    expect(responses).toHaveLength(1);
    expect(
      responses[0].data.output.map((message: { id: string }) => message.id),
    ).toEqual(["call", "conversation-compaction-boundary", "result"]);
  });
});
