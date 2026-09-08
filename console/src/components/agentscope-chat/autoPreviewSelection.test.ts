import { describe, expect, it } from "vitest";
import { findLatestAutoPreviewUrl } from "./autoPreviewSelection";
import type { IAgentScopeRuntimeWebUIMessage as Message } from "./AgentScopeRuntimeWebUI/core/types/IMessages";

const oldUrl = "https://example.test/A-auto-preview.html";
const newUrl = "https://example.test/B-auto-preview.html";

function response(id: string, url: string): Message {
  return {
    id,
    role: "assistant",
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

function textResponse(id: string, text: string): Message {
  return {
    id,
    role: "assistant",
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

describe("latest auto-preview report", () => {
  it("uses message order rather than filename order", () => {
    const messages = [response("1", newUrl), response("2", oldUrl)];
    expect(findLatestAutoPreviewUrl(messages)).toBe(oldUrl);
    expect(messages.map((m) => m.id)).toEqual(["1", "2"]);
  });

  it("ignores trailing messages without reports and user attachments", () => {
    expect(
      findLatestAutoPreviewUrl([
        response("1", oldUrl),
        response("2", newUrl),
        response("3", "https://example.test/plain.html"),
        { ...response("4", oldUrl), role: "user" },
      ]),
    ).toBe(newUrl);
  });

  it("keeps dynamic file cards in the original auto-preview selection", () => {
    const dynamic =
      "https://example.test/report.html?resultId=new&templateId=1";
    expect(
      findLatestAutoPreviewUrl([
        response("old", oldUrl),
        response("new", dynamic),
      ]),
    ).toBe(dynamic);
  });

  it("does not treat dynamic text as a file-card auto-preview candidate", () => {
    const dynamic =
      "https://example.test/report.html?resultId=new&templateId=1";
    expect(
      findLatestAutoPreviewUrl([
        response("old", oldUrl),
        textResponse("new", dynamic),
      ]),
    ).toBe(oldUrl);
  });

  it("does not select a dynamic file that cannot be previewed", () => {
    const dynamic = "https://example.test/render?resultId=new&templateId=1";
    expect(
      findLatestAutoPreviewUrl([
        response("old", oldUrl),
        response("new", dynamic),
      ]),
    ).toBe(oldUrl);
  });

  it("does not select an unsupported dynamic file card", () => {
    const dynamic = "https://example.test/render?resultId=new&templateId=1";
    expect(
      findLatestAutoPreviewUrl([
        response("old", oldUrl),
        response("new", dynamic),
      ]),
    ).toBe(oldUrl);
  });

  it("uses the expanded task result and does not fall back to folded old executions", () => {
    const task = (id: string, collapsed: boolean, url: string): Message => ({
      id,
      role: "assistant",
      cards: [
        {
          code: "TaskRunGroupCard",
          data: {
            collapsedByDefault: collapsed,
            finalMessages: [response(id, url)],
            stepMessages: [],
          },
        },
      ],
    });
    expect(
      findLatestAutoPreviewUrl([
        task("old", true, oldUrl),
        task("new", false, newUrl),
      ]),
    ).toBe(newUrl);
    expect(
      findLatestAutoPreviewUrl([
        task("old", true, oldUrl),
        task("new", false, "https://example.test/plain.html"),
      ]),
    ).toBeNull();
  });
});
