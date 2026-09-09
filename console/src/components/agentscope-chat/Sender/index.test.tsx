import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Sender from "./index";

vi.mock("@/components/agentscope-chat", () => ({
  useProviderContext: () => ({
    direction: "ltr",
    getPrefixCls: (prefix: string) => prefix,
  }),
}));

vi.mock("./SenderHeader", () => ({
  default: () => null,
  SendHeaderContext: {
    Provider: ({ children }: { children: React.ReactNode }) => children,
  },
}));

vi.mock("./ModeSelect", () => ({ default: () => null }));
vi.mock("./BeforeUIContainer", () => ({ default: () => null }));
vi.mock("./components/ActionButton", () => ({
  ActionButtonContext: {
    Provider: ({ children }: { children: React.ReactNode }) => children,
  },
}));
vi.mock("./components/ClearButton", () => ({ default: () => null }));
vi.mock("./components/LoadingButton", () => ({ default: () => null }));
vi.mock("./components/SendButton", () => ({
  default: () => <button aria-label="发送消息" type="button" />,
}));

const skills = [
  {
    id: "skill:browser",
    type: "skill" as const,
    label: "browser",
    name: "browser",
    description: "Use a browser",
  },
  {
    id: "skill:Build",
    type: "skill" as const,
    label: "Build",
    name: "Build",
    description: "Build an app",
  },
];

function renderSender() {
  const onOpen = vi.fn();
  const onChange = vi.fn();

  render(
    <Sender
      skillMentions={{
        items: skills,
        selected: [],
        onOpen,
        onChange,
      }}
    />,
  );

  return { input: screen.getByRole("textbox"), onChange, onOpen };
}

function setTokenEditorValue(input: HTMLElement, value: string) {
  input.textContent = value;
  fireEvent.input(input);
}

describe("Sender skill mentions", () => {
  afterEach(cleanup);

  it("does not select a mention when Enter commits an IME composition", () => {
    const { input, onChange } = renderSender();

    setTokenEditorValue(input, "@br");
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });

    expect(onChange).not.toHaveBeenCalled();
    expect(input).toHaveTextContent("@br");
  });

  it("keeps dictation immediately before send and hides the character counter", () => {
    render(
      <Sender
        allowSpeech
        maxLength={10000}
        actions={(defaultActions) => (
          <div>
            <button aria-label="上下文占用" type="button" />
            {defaultActions}
          </div>
        )}
      />,
    );

    const actionGroup = document.querySelector(".sender-actions-list");
    const microphone = screen.getByRole("button", { name: "语音输入" });
    const send = screen.getByRole("button", { name: "发送消息" });

    expect(actionGroup).toContainElement(microphone);
    expect(actionGroup).toContainElement(send);
    expect(actionGroup).not.toHaveTextContent("0/10000");
    expect(
      microphone.compareDocumentPosition(send) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("uses the shared accessible menu and shows its no-match state", () => {
    const { input, onOpen } = renderSender();

    setTokenEditorValue(input, "@missing");

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("listbox", { name: "可用上下文引用" }),
    ).toBeInTheDocument();
    expect(screen.getByText("未找到匹配的上下文引用")).toBeInTheDocument();
  });

  it("anchors the context-reference menu above the complete sender card", () => {
    const { input } = renderSender();

    setTokenEditorValue(input, "@");
    const menu = document.getElementById("context-reference-menu");

    expect(menu?.parentElement).toBe(input.closest(".sender"));
    expect(menu).toHaveStyle({ bottom: "calc(100% + 8px)" });
    expect(menu).not.toHaveStyle({ top: "calc(100% + 8px)" });
  });

  it("submits an unmatched mention when Enter cannot select a skill", () => {
    const onSubmit = vi.fn();
    render(
      <Sender
        onSubmit={onSubmit}
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange: vi.fn(),
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "@missing");
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("@missing");
    expect(input).toHaveTextContent("@missing");
  });

  it("selects a matching skill instead of submitting on Enter", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn();
    render(
      <Sender
        onSubmit={onSubmit}
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange,
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "@br");
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith([skills[0]]);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(input.textContent).toBe("@browser ");
  });

  it("does not submit while a loading skill menu has no matches", () => {
    const onSubmit = vi.fn();
    render(
      <Sender
        onSubmit={onSubmit}
        skillMentions={{
          items: [],
          selected: [],
          loading: true,
          onOpen: vi.fn(),
          onChange: vi.fn(),
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "@missing");
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onSubmit).not.toHaveBeenCalled();
    expect(input).toHaveTextContent("@missing");
  });

  it("selects matching mentions by click and Enter", () => {
    const clickSender = renderSender();

    setTokenEditorValue(clickSender.input, "请用 @br");
    fireEvent.click(screen.getByRole("option", { name: /browser/ }));

    expect(clickSender.onOpen).toHaveBeenCalledTimes(1);
    expect(clickSender.onChange).toHaveBeenCalledWith([skills[0]]);
    expect(clickSender.input.textContent).toBe("请用 @browser ");

    cleanup();
    const enterSender = renderSender();
    setTokenEditorValue(enterSender.input, "@BU");
    fireEvent.keyDown(enterSender.input, { key: "Enter" });

    expect(enterSender.onOpen).toHaveBeenCalledTimes(1);
    expect(enterSender.onChange).toHaveBeenCalledWith([skills[1]]);
    expect(enterSender.input.textContent).toBe("@Build ");
  });

  it("does not activate slash-command suggestions while skill tags are enabled", () => {
    render(
      <Sender
        suggestions={[{ label: "Help", value: "help" }]}
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange: vi.fn(),
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "/help @");

    expect(screen.queryByText("Help")).toBeNull();
    expect(
      screen.getByRole("listbox", { name: "可用上下文引用" }),
    ).toBeInTheDocument();
  });

  it("retains read-only, max-length, and shift-enter sender semantics", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn();
    render(
      <Sender
        maxLength={3}
        onChange={onChange}
        onSubmit={onSubmit}
        submitType="shiftEnter"
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange: vi.fn(),
        }}
      />,
    );

    const input = screen.getByRole("textbox");
    setTokenEditorValue(input, "hello");
    expect(onChange).toHaveBeenCalledWith("hel", undefined);

    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(onSubmit).toHaveBeenCalledWith("hel");

    cleanup();
    render(
      <Sender
        readOnly
        skillMentions={{
          items: skills,
          selected: [],
          onOpen: vi.fn(),
          onChange: vi.fn(),
        }}
      />,
    );
    expect(screen.getByRole("textbox")).toHaveAttribute(
      "contenteditable",
      "false",
    );
  });
});
