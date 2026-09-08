import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import TaskRunGroupCard from ".";
import { useFilePreviewPresentation } from "@/components/agentscope-chat/FilePreviewPresentationContext";
import type { ChatTaskRunGroupCardData } from "../../messageMeta";

type TaskRunMessage = ChatTaskRunGroupCardData["finalMessages"][number];
type MockResponseData = {
  id: string;
  output?: Array<{
    id?: string;
    type?: string;
    content?: Array<{
      data?: unknown;
      file_url?: string;
      text?: string;
    }>;
  }>;
};

function getStepResponseIds() {
  return Array.from(
    screen
      .getByTestId("task-run-steps")
      .querySelectorAll("[data-testid^='response-']"),
  ).map((element) => element.getAttribute("data-testid"));
}

vi.mock("../RuntimeRequestCard", () => ({
  default: ({ data }: { data: { id: string } }) => (
    <div data-testid={`request-${data.id}`}>{data.id}</div>
  ),
}));

vi.mock("../RuntimeResponseCard", () => ({
  default: function MockRuntimeResponseCard({
    data,
    showFeedback,
  }: {
    data: MockResponseData;
    showFeedback?: boolean;
  }) {
    const previewPresentation = useFilePreviewPresentation();
    const firstContent = data.output?.[0]?.content?.[0];
    const output = data.output || [];
    return (
      <div
        data-output={firstContent?.text || firstContent?.file_url || ""}
        data-output-count={output.length}
        data-output-ids={output.map((item) => item.id).join(",")}
        data-output-types={output.map((item) => item.type).join(",")}
        data-preview-presentation={previewPresentation}
        data-show-feedback={String(showFeedback)}
        data-testid={`response-${data.id}`}
      >
        {data.id}
      </div>
    );
  },
  RuntimeResponseFeedbackCard: ({ data }: { data: MockResponseData }) => (
    <div data-testid={`feedback-${data.id}`}>{data.id}</div>
  ),
}));

vi.mock("../ApprovalActionCard", () => ({
  default: ({ data }: { data: { requestId: string } }) => (
    <div data-testid={`approval-${data.requestId}`}>{data.requestId}</div>
  ),
}));

function messageWithResponse(
  messageId: string,
  responseId: string,
): TaskRunMessage {
  return {
    id: messageId,
    role: "assistant",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          id: responseId,
          status: "completed",
          created_at: 0,
          output: [
            {
              id: `${responseId}-message`,
              role: "assistant",
              type: "message",
              status: "completed",
              content: [
                {
                  type: "text",
                  text: responseId,
                },
              ],
            },
          ],
        },
      },
    ],
  };
}

function messageWithAutoPreviewResponse(
  messageId: string,
  responseId: string,
): TaskRunMessage {
  return {
    id: messageId,
    role: "assistant",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          id: responseId,
          status: "completed",
          created_at: 0,
          output: [
            {
              id: `${responseId}-message`,
              role: "assistant",
              type: "message",
              status: "completed",
              content: [
                {
                  type: "file",
                  status: "completed",
                  file_url:
                    "https://example.test/static/report[auto-preview].html",
                  file_name: "report[auto-preview].html",
                },
              ],
            },
          ],
        },
      },
    ],
  };
}

function messageWithAutoPreviewTextResponse(
  messageId: string,
  responseId: string,
): TaskRunMessage {
  return {
    id: messageId,
    role: "assistant",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          id: responseId,
          status: "completed",
          created_at: 0,
          output: [
            {
              id: `${responseId}-reasoning`,
              role: "assistant",
              type: "reasoning",
              status: "completed",
              content: [
                {
                  type: "text",
                  text:
                    "URL是：\n" +
                    "https://example.test/static/customer-list-auto-preview-1780020020982.html\n\n" +
                    "这是一个预览页面。",
                },
              ],
            },
          ],
        },
      },
    ],
  };
}

function messageWithWrappedToolAutoPreviewResponse(
  messageId: string,
  responseId: string,
): TaskRunMessage {
  const fileName = "经营客户清单-2026-08-03auto-preview-1785740419902.html";
  const previewUrl = `https://example.test/static/${fileName}`;
  const wrappedOutput = `[${fileName}](${JSON.stringify({
    type: "text",
    text:
      "**returnCode**: (类型: string)\\n" +
      JSON.stringify({
        returnCode: "SUC000",
        body: {
          output: {
            previewUrl: `[${fileName}](${previewUrl})`,
            todayNeedDealSize: 0,
            overDealSize: 2,
          },
          sessionId: "2084172496587239664",
        },
      }),
  })})`;

  return {
    id: messageId,
    role: "assistant",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          id: responseId,
          status: "completed",
          created_at: 0,
          output: [
            {
              id: `${responseId}-tool-output`,
              role: "tool",
              type: "plugin_call_output",
              status: "completed",
              content: [
                {
                  type: "data",
                  status: "completed",
                  data: {
                    call_id: "call-preview",
                    name: "query_business_opportunity",
                    arguments: {
                      customerType: "fund-redemption",
                    },
                    output: wrappedOutput,
                  },
                },
              ],
            },
          ],
        },
      },
    ],
  };
}

function messageWithToolFileAutoPreviewResponse(
  messageId: string,
  responseId: string,
): TaskRunMessage {
  return {
    id: messageId,
    role: "assistant",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          id: responseId,
          status: "completed",
          created_at: 0,
          output: [
            {
              id: `${responseId}-tool-output`,
              role: "tool",
              type: "plugin_call_output",
              status: "completed",
              content: [
                {
                  type: "file",
                  status: "completed",
                  file_url:
                    "https://example.test/static/tool-report[auto-preview].html",
                  file_name: "tool-report[auto-preview].html",
                },
              ],
            },
          ],
        },
      },
    ],
  };
}

function messageWithMisleadingAutoPreviewLabel(
  messageId: string,
  responseId: string,
): TaskRunMessage {
  const fileName = "not-a-link[auto-preview].html";
  return {
    id: messageId,
    role: "assistant",
    cards: [
      {
        code: "AgentScopeRuntimeResponseCard",
        data: {
          id: responseId,
          status: "completed",
          created_at: 0,
          output: [
            {
              id: `${responseId}-tool-output`,
              role: "tool",
              type: "plugin_call_output",
              status: "completed",
              content: [
                {
                  type: "data",
                  status: "completed",
                  data: {
                    call_id: "call-no-preview",
                    name: "query_business_opportunity",
                    output: `[${fileName}]({"type":"text","text":"schema only"})`,
                  },
                },
              ],
            },
          ],
        },
      },
    ],
  };
}

function taskRunData(
  overrides: Partial<ChatTaskRunGroupCardData> = {},
): ChatTaskRunGroupCardData {
  return {
    runId: "run-1",
    runIndex: 0,
    taskName: "daily-check",
    finalMessages: [messageWithResponse("final-message", "final-response")],
    stepMessages: [messageWithResponse("step-message", "step-response")],
    ...overrides,
  } as ChatTaskRunGroupCardData;
}

describe("TaskRunGroupCard", () => {
  afterEach(() => {
    cleanup();
  });

  it("collapses step messages behind a step toggle", () => {
    render(<TaskRunGroupCard data={taskRunData()} />);

    expect(screen.getByTestId("response-final-response")).toBeInTheDocument();
    expect(screen.getByTestId("task-run-toggle")).toBeInTheDocument();
    expect(screen.queryByTestId("task-run-steps")).toBeNull();
    expect(screen.queryByTestId("response-step-response")).toBeNull();

    fireEvent.click(screen.getByTestId("task-run-toggle"));

    expect(screen.getByTestId("task-run-steps")).toBeInTheDocument();
    expect(screen.getByTestId("response-step-response")).toBeInTheDocument();
  });

  it("renders feedback after the final task response", () => {
    const finalMessage = messageWithResponse("final-message", "final-response");
    finalMessage.cards?.push({
      code: "ResponseFeedback",
      data: finalMessage.cards[0]?.data,
    });

    render(
      <TaskRunGroupCard
        data={taskRunData({ finalMessages: [finalMessage] })}
      />,
    );

    expect(screen.getByTestId("response-final-response")).toBeInTheDocument();
    expect(screen.getByTestId("feedback-final-response")).toBeInTheDocument();
  });

  it("keeps historical step messages behind the step toggle after expanding history", () => {
    render(
      <TaskRunGroupCard data={taskRunData({ collapsedByDefault: true })} />,
    );

    fireEvent.click(screen.getByTestId("task-run-result-toggle"));

    expect(screen.getByTestId("response-final-response")).toBeInTheDocument();
    expect(screen.getByTestId("task-run-toggle")).toBeInTheDocument();
    expect(screen.queryByTestId("task-run-steps")).toBeNull();

    fireEvent.click(screen.getByTestId("task-run-toggle"));

    expect(screen.getByTestId("task-run-steps")).toBeInTheDocument();
    expect(screen.getByTestId("response-step-response")).toBeInTheDocument();
  });

  it.each(["finalMessages", "stepMessages"] as const)(
    "selects the last matching message in %s",
    (section) => {
      render(
        <TaskRunGroupCard
          data={taskRunData({
            [section]: [
              messageWithAutoPreviewResponse("old", "old-response"),
              messageWithAutoPreviewResponse("new", "new-response"),
              messageWithResponse("trailing", "plain-response"),
            ],
          })}
        />,
      );
      expect(screen.getByTestId("response-new-response")).toBeInTheDocument();
      expect(screen.queryByTestId("response-old-response")).toBeNull();
    },
  );

  it("prefers a final preview over a matching execution step", () => {
    render(
      <TaskRunGroupCard
        data={taskRunData({
          finalMessages: [messageWithAutoPreviewResponse("final", "final")],
          stepMessages: [messageWithAutoPreviewResponse("step", "step")],
        })}
      />,
    );
    expect(screen.getByTestId("response-final")).toBeInTheDocument();
    expect(screen.queryByTestId("response-step")).toBeNull();
  });

  it.each([
    "cards",
    "output",
    "content",
    "nested",
    "markdown",
    "plain",
    "mixed",
  ])("selects B from multiple preview links in %s", (level) => {
    const urlA = "https://example.test/A-auto-preview.html";
    const urlB = "https://example.test/B-auto-preview.html";
    const fileA = { type: "file", file_url: urlA };
    const fileB = { type: "file", file_url: urlB };
    const text =
      level === "markdown"
        ? `[A](${urlA}) [B](${urlB})`
        : level === "mixed"
        ? `[A](${urlA}) ${urlB}`
        : `${urlA} ${urlB}`;
    const content =
      level === "nested"
        ? [{ type: "text", text: JSON.stringify({ data: [fileA, fileB] }) }]
        : ["markdown", "plain", "mixed"].includes(level)
        ? [{ type: "text", text }]
        : [fileA, fileB, { type: "text", text: "done" }];
    const outputA = { role: "assistant", type: "message", content: [fileA] };
    const outputB = { role: "assistant", type: "message", content: [fileB] };
    const card = {
      code: "AgentScopeRuntimeResponseCard",
      data: {
        id: "preview",
        output:
          level === "output" ? [outputA, outputB] : [{ ...outputA, content }],
      },
    };
    const message = {
      id: "preview-message",
      role: "assistant",
      cards:
        level === "cards"
          ? [
              { ...card, data: { id: "old-preview", output: [outputA] } },
              { ...card, data: { id: "preview", output: [outputB] } },
            ]
          : [card],
    } as TaskRunMessage;
    const original = JSON.stringify(message);
    render(
      <TaskRunGroupCard data={taskRunData({ finalMessages: [message] })} />,
    );
    const selected = screen
      .getByTestId("response-preview")
      .getAttribute("data-output");
    expect(selected).toContain(urlB);
    expect(selected).not.toContain(urlA);
    expect(JSON.stringify(message)).toBe(original);
  });

  it("shows only the auto-preview HTML card outside and moves all run messages into steps", () => {
    render(
      <TaskRunGroupCard
        data={taskRunData({
          finalMessages: [
            messageWithResponse("final-message", "final-response"),
          ],
          stepMessages: [
            messageWithResponse("step-message", "step-response"),
            messageWithAutoPreviewResponse(
              "preview-message",
              "preview-response",
            ),
          ],
        })}
      />,
    );

    expect(screen.getByTestId("response-preview-response")).toBeInTheDocument();
    expect(screen.getByTestId("response-preview-response")).toHaveAttribute(
      "data-preview-presentation",
      "modal",
    );
    expect(screen.queryByTestId("response-final-response")).toBeNull();
    expect(screen.queryByTestId("response-step-response")).toBeNull();
    expect(screen.queryByTestId("task-run-steps")).toBeNull();

    fireEvent.click(screen.getByTestId("task-run-toggle"));

    expect(screen.getByTestId("task-run-steps")).toBeInTheDocument();
    expect(screen.getByTestId("response-step-response")).toBeInTheDocument();
    expect(screen.getAllByTestId("response-preview-response")).toHaveLength(1);
    expect(getStepResponseIds()).toEqual(["response-step-response"]);
    expect(screen.getByTestId("response-step-response")).toHaveAttribute(
      "data-output-ids",
      "step-response-message,preview-response-message,final-response-message",
    );
  });

  it("detects an auto-preview HTML URL embedded in step reasoning text", () => {
    render(
      <TaskRunGroupCard
        data={taskRunData({
          finalMessages: [
            messageWithResponse("final-message", "final-response"),
          ],
          stepMessages: [
            messageWithAutoPreviewTextResponse(
              "reasoning-message",
              "reasoning-response",
            ),
          ],
        })}
      />,
    );

    expect(screen.getByTestId("response-reasoning-response")).toHaveAttribute(
      "data-output",
      "[customer-list-auto-preview-1780020020982.html](https://example.test/static/customer-list-auto-preview-1780020020982.html)",
    );
    expect(screen.queryByTestId("response-final-response")).toBeNull();

    fireEvent.click(screen.getByTestId("task-run-toggle"));

    expect(screen.getAllByTestId("response-reasoning-response")).toHaveLength(
      2,
    );
    expect(getStepResponseIds()).toEqual(["response-reasoning-response"]);
    expect(
      screen.getAllByTestId("response-reasoning-response")[1],
    ).toHaveAttribute(
      "data-output-ids",
      "reasoning-response-reasoning,final-response-message",
    );
    expect(
      screen.getAllByTestId("response-reasoning-response")[1],
    ).toHaveAttribute("data-output-types", "reasoning,message");
  });

  it("isolates an auto-preview HTML URL from a wrapped tool output", () => {
    render(
      <TaskRunGroupCard
        data={taskRunData({
          finalMessages: [
            messageWithResponse("final-message", "final-response"),
          ],
          stepMessages: [
            messageWithWrappedToolAutoPreviewResponse(
              "tool-message",
              "tool-response",
            ),
          ],
        })}
      />,
    );

    expect(screen.getByTestId("response-tool-response")).toHaveAttribute(
      "data-output",
      "[经营客户清单-2026-08-03auto-preview-1785740419902.html](https://example.test/static/经营客户清单-2026-08-03auto-preview-1785740419902.html)",
    );
    expect(screen.queryByTestId("response-final-response")).toBeNull();
    expect(screen.queryByTestId("task-run-steps")).toBeNull();

    fireEvent.click(screen.getByTestId("task-run-toggle"));

    expect(screen.getByTestId("task-run-steps")).toBeInTheDocument();
    expect(screen.getAllByTestId("response-tool-response")).toHaveLength(2);
    expect(screen.getAllByTestId("response-tool-response")[1]).toHaveAttribute(
      "data-output-types",
      "plugin_call_output,message",
    );
  });

  it("converts a tool file preview into a plain assistant result", () => {
    render(
      <TaskRunGroupCard
        data={taskRunData({
          stepMessages: [
            messageWithToolFileAutoPreviewResponse(
              "tool-file-message",
              "tool-file-response",
            ),
          ],
        })}
      />,
    );

    expect(screen.getByTestId("response-tool-file-response")).toHaveAttribute(
      "data-output",
      "https://example.test/static/tool-report[auto-preview].html",
    );
    expect(screen.getByTestId("response-tool-file-response")).toHaveAttribute(
      "data-output-types",
      "message",
    );
  });

  it("does not promote an auto-preview label without a valid URL", () => {
    render(
      <TaskRunGroupCard
        data={taskRunData({
          stepMessages: [
            messageWithMisleadingAutoPreviewLabel(
              "misleading-message",
              "misleading-response",
            ),
          ],
        })}
      />,
    );

    expect(screen.getByTestId("response-final-response")).toBeInTheDocument();
    expect(screen.queryByTestId("response-misleading-response")).toBeNull();

    fireEvent.click(screen.getByTestId("task-run-toggle"));

    expect(
      screen.getByTestId("response-misleading-response"),
    ).toBeInTheDocument();
  });
});
