import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BubbleProps } from "@/components/agentscope-chat/Bubble/interface";
import Builder from "./Builder";
import RequestCard from "./Card";

vi.mock("./style", () => ({ default: () => null }));
vi.mock("@/components/agentscope-chat", () => ({
  Bubble: ({ cards, className, role }: BubbleProps) => (
    <div data-testid="message" className={className} data-role={role}>
      {cards?.map((card, index) => (
        <div key={`${card.code}-${index}`} data-testid={card.code}>
          {JSON.stringify(card.data)}
        </div>
      ))}
    </div>
  ),
}));

const files = [
  {
    uid: "1",
    name: "报告.pdf",
    type: "application/pdf",
    response: { url: "/report.pdf" },
  },
  {
    uid: "2",
    name: "截图.png",
    type: "image/png",
    response: { url: "/first.png" },
  },
  {
    uid: "3",
    name: "截图2.png",
    type: "image/png",
    response: { url: "/second.png" },
  },
];

describe("user messages with attachments", () => {
  afterEach(cleanup);

  it("groups attachments above text in one bubble before and after reload", () => {
    const request = new Builder().handle({
      query: "请分析这些附件",
      fileList: files,
    });
    const { rerender } = render(<RequestCard data={request} />);

    expect(screen.getAllByTestId("message")).toHaveLength(1);
    expect(screen.getByTestId("message")).toHaveClass("swe-request-grouped");
    expect(
      Array.from(screen.getByTestId("message").children).map((element) =>
        element.getAttribute("data-testid"),
      ),
    ).toEqual(["Files", "Images", "Text"]);
    expect(screen.getByTestId("Images")).toHaveTextContent("/first.png");
    expect(screen.getByTestId("Images")).toHaveTextContent("/second.png");

    rerender(<RequestCard data={JSON.parse(JSON.stringify(request))} />);
    expect(screen.getAllByTestId("message")).toHaveLength(1);
    expect(screen.getByTestId("message")).toHaveClass("swe-request-grouped");
  });

  it("does not render blank text for attachment-only messages", () => {
    render(
      <RequestCard
        data={new Builder().handle({ query: "  ", fileList: files })}
      />,
    );

    expect(screen.queryByTestId("Text")).not.toBeInTheDocument();
    expect(screen.getByTestId("Files")).toBeInTheDocument();
    expect(screen.getAllByTestId("Images")).toHaveLength(1);
  });
});
