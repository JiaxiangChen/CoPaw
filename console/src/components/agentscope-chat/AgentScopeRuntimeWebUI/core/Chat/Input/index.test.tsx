import React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { UploadFile } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Input from "./index";
import { RUNTIME_INPUT_SET_CONTENT_EVENT } from "../hooks/followUpSubmit";
import { ChatAnywhereMessagesContext } from "../../Context/ChatAnywhereMessagesContext";
import {
  CHAT_INPUT_APPEND_TEXT_EVENT,
  CHAT_INPUT_REPLACE_TEXT_EVENT,
} from "@/components/agentscope-chat/chatInputDraft";

const attachmentState = {
  currentFileList: [] as UploadFile[],
  getFileList: vi.fn<() => UploadFile[]>(),
  setFileList: vi.fn((next: UploadFile[]) => {
    attachmentState.currentFileList = next;
  }),
  handlePasteFile: vi.fn<(file: File) => void>(),
  uploadQuickMenuItem: null as React.ReactNode,
};

const senderOptions = {
  current: {} as Record<string, unknown>,
};

vi.mock("@/components/GlobalVoiceRecorder/VoiceRecorderQuickMenuItem", () => ({
  default: () => <span data-testid="voice-recorder-trigger">语音录制</span>,
}));

vi.mock("@/components/GlobalVoiceRecorder/context", () => ({
  useVoiceRecorderTrigger: () => ({
    disabled: false,
    label: "开始语音录制",
    loading: false,
    panelOpen: false,
    recording: false,
    unsupported: false,
    trigger: () => undefined,
  }),
}));

function renderActiveInput(onSubmit = vi.fn()) {
  return render(
    <ChatAnywhereMessagesContext.Provider
      value={{
        messages: [{ id: "message-1" } as never],
        setMessages: vi.fn(),
        getMessages: () => [],
      }}
    >
      <Input onCancel={vi.fn()} onSubmit={onSubmit} />
    </ChatAnywhereMessagesContext.Provider>,
  );
}

vi.mock("@/components/agentscope-chat", () => ({
  ChatInput: (props: {
    value?: string;
    onChange?: (value: string) => void;
    onSubmit?: () => void;
    prefix?: React.ReactNode;
  }) => (
    <div>
      {props.prefix}
      <textarea
        data-testid="chat-input"
        value={props.value || ""}
        onChange={(event) => props.onChange?.(event.target.value)}
      />
      <button type="button" onClick={() => props.onSubmit?.()}>
        submit
      </button>
    </div>
  ),
  Disclaimer: () => null,
  useProviderContext: () => ({
    getPrefixCls: (prefix: string) => prefix,
  }),
}));

vi.mock("../../Context/ChatAnywhereOptionsContext", () => ({
  useChatAnywhereOptions: (selector: (value: { sender: object }) => unknown) =>
    selector({ sender: senderOptions.current }),
}));

vi.mock("../../Context/ChatAnywhereInputContext", () => ({
  useChatAnywhereInput: (
    selector: (value: { disabled: boolean; loading: boolean }) => unknown,
  ) => selector({ disabled: false, loading: false }),
}));

vi.mock("./useAttachments", () => ({
  default: () => ({
    fileList: attachmentState.currentFileList,
    getFileList: attachmentState.getFileList,
    setFileList: attachmentState.setFileList,
    handlePasteFile: attachmentState.handlePasteFile,
    uploadQuickMenuItem: attachmentState.uploadQuickMenuItem,
    uploadFileListHeader: null,
  }),
}));

vi.mock("@/components/agentscope-chat/ComposerQuickMenu", () => ({
  default: ({ children }: { children?: React.ReactNode }) => (
    <div data-testid="composer-quick-menu">{children}</div>
  ),
}));

describe("Chat Input restore flow", () => {
  beforeEach(() => {
    attachmentState.currentFileList = [];
    attachmentState.getFileList.mockImplementation(
      () => attachmentState.currentFileList,
    );
    attachmentState.getFileList.mockClear();
    attachmentState.setFileList.mockClear();
    attachmentState.handlePasteFile.mockClear();
    attachmentState.uploadQuickMenuItem = null;
    senderOptions.current = {};
  });

  afterEach(() => {
    cleanup();
  });

  it("restores attachments and biz_params when follow-up auto-submit fails", async () => {
    const onSubmit = vi.fn();
    const restoredFiles = [
      {
        uid: "restored-file",
        name: "demo.txt",
        response: { url: "/demo.txt" },
      },
    ] as UploadFile[];
    const biz_params = {
      user_prompt_params: {
        source: "follow-up",
      },
    };

    render(<Input onCancel={vi.fn()} onSubmit={onSubmit} />);

    document.dispatchEvent(
      new CustomEvent(RUNTIME_INPUT_SET_CONTENT_EVENT, {
        detail: {
          content: "recover me",
          fileList: restoredFiles,
          biz_params,
        },
      }),
    );

    await waitFor(() => {
      expect((screen.getByTestId("chat-input") as HTMLInputElement).value).toBe(
        "recover me",
      );
    });
    expect(attachmentState.setFileList).toHaveBeenCalledWith(restoredFiles);

    fireEvent.click(
      screen.getByRole("button", { name: "submit", hidden: true }),
    );

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({
        query: "recover me",
        fileList: restoredFiles,
        biz_params,
      });
    });
    expect(attachmentState.setFileList).toHaveBeenLastCalledWith([]);
  });

  it("clears restored biz_params when input content is replaced programmatically", async () => {
    const onSubmit = vi.fn();
    const biz_params = {
      user_prompt_params: {
        source: "follow-up",
      },
    };

    render(<Input onCancel={vi.fn()} onSubmit={onSubmit} />);

    document.dispatchEvent(
      new CustomEvent(RUNTIME_INPUT_SET_CONTENT_EVENT, {
        detail: {
          content: "recover me",
          biz_params,
        },
      }),
    );

    document.dispatchEvent(
      new CustomEvent(RUNTIME_INPUT_SET_CONTENT_EVENT, {
        detail: {
          content: "normal prompt",
        },
      }),
    );

    await waitFor(() => {
      expect((screen.getByTestId("chat-input") as HTMLInputElement).value).toBe(
        "normal prompt",
      );
    });

    fireEvent.click(
      screen.getByRole("button", { name: "submit", hidden: true }),
    );

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({
        query: "normal prompt",
        fileList: [],
        biz_params: undefined,
      });
    });
  });

  it("clears restored biz_params after the user edits the recovered content", async () => {
    const onSubmit = vi.fn();
    const biz_params = {
      user_prompt_params: {
        source: "follow-up",
      },
    };

    render(<Input onCancel={vi.fn()} onSubmit={onSubmit} />);

    document.dispatchEvent(
      new CustomEvent(RUNTIME_INPUT_SET_CONTENT_EVENT, {
        detail: {
          content: "recover me",
          biz_params,
        },
      }),
    );

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "another question" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "submit", hidden: true }),
    );

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({
        query: "another question",
        fileList: [],
        biz_params: undefined,
      });
    });
  });

  it("handles files dispatched by the chat drag-and-drop bridge", async () => {
    const file = new File(["hello"], "demo.txt", { type: "text/plain" });

    render(<Input onCancel={vi.fn()} onSubmit={vi.fn()} />);

    document.dispatchEvent(
      new CustomEvent("pasteFile", {
        detail: { file },
      }),
    );

    expect(attachmentState.handlePasteFile).toHaveBeenCalledWith(file);
  });

  it("combines upload, voice recording, and caller actions in one quick menu", () => {
    attachmentState.uploadQuickMenuItem = <span>上传文件</span>;
    senderOptions.current = {
      quickMenuItems: <span>计划模式</span>,
    };

    renderActiveInput();

    expect(screen.getByTestId("composer-quick-menu")).toHaveTextContent(
      "上传文件",
    );
    expect(screen.getByTestId("composer-quick-menu")).toHaveTextContent(
      "计划模式",
    );
    expect(screen.getByTestId("composer-quick-menu")).toContainElement(
      screen.getByTestId("voice-recorder-trigger"),
    );
  });

  it("appends transcribed text to the active conversation draft without submitting", async () => {
    const onSubmit = vi.fn();
    renderActiveInput(onSubmit);

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "已有草稿" },
    });
    document.dispatchEvent(
      new CustomEvent(CHAT_INPUT_APPEND_TEXT_EVENT, {
        detail: { content: "语音转写" },
      }),
    );

    await waitFor(() => {
      expect(screen.getByTestId("chat-input")).toHaveValue(
        "已有草稿\n语音转写",
      );
    });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("ignores transcribed text while the conversation input is hidden", () => {
    render(<Input onCancel={vi.fn()} onSubmit={vi.fn()} />);

    document.dispatchEvent(
      new CustomEvent(CHAT_INPUT_APPEND_TEXT_EVENT, {
        detail: { content: "欢迎页负责接收" },
      }),
    );

    expect(screen.getByTestId("chat-input")).toHaveValue("");
  });

  it("replaces the active conversation draft without submitting", async () => {
    const onSubmit = vi.fn();
    renderActiveInput(onSubmit);

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "需要清空的原草稿" },
    });
    document.dispatchEvent(
      new CustomEvent(CHAT_INPUT_REPLACE_TEXT_EVENT, {
        detail: { content: "完整 SOP 提示词" },
      }),
    );

    await waitFor(() => {
      expect(screen.getByTestId("chat-input")).toHaveValue("完整 SOP 提示词");
    });
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

const shareState = { active: false };
vi.mock("@/pages/Chat/chatShareContext", () => ({
  useChatShareSelection: () => shareState,
}));

describe("Chat Input sharing mode", () => {
  afterEach(() => {
    shareState.active = false;
    senderOptions.current = {};
    cleanup();
  });

  it("hides the composer, blocks submission, and preserves the draft on exit", async () => {
    const onSubmit = vi.fn();
    const view = () => (
      <ChatAnywhereMessagesContext.Provider
        value={{
          messages: [{ id: "message-1" } as never],
          setMessages: vi.fn(),
          getMessages: () => [],
        }}
      >
        <Input onCancel={vi.fn()} onSubmit={onSubmit} />
      </ChatAnywhereMessagesContext.Provider>
    );
    const { rerender } = render(view());
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "保留这段草稿" },
    });
    shareState.active = true;
    rerender(view());
    expect(screen.getByTestId("chat-input")).not.toBeVisible();
    fireEvent.click(screen.getByText("submit"));
    expect(onSubmit).not.toHaveBeenCalled();
    shareState.active = false;
    rerender(view());
    expect(screen.getByTestId("chat-input")).toBeVisible();
    expect(screen.getByTestId("chat-input")).toHaveValue("保留这段草稿");
    fireEvent.click(screen.getByRole("button", { name: "submit" }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ query: "保留这段草稿" }),
      ),
    );
  });

  it("blocks an awaiting submission when sharing starts before validation finishes", async () => {
    let resolveSubmit!: (value: boolean) => void;
    senderOptions.current = {
      beforeSubmit: () =>
        new Promise<boolean>((resolve) => {
          resolveSubmit = resolve;
        }),
    };
    const onSubmit = vi.fn();
    const { rerender } = render(
      <Input onCancel={vi.fn()} onSubmit={onSubmit} />,
    );
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "待发送草稿" },
    });
    fireEvent.click(screen.getByText("submit"));
    shareState.active = true;
    rerender(<Input onCancel={vi.fn()} onSubmit={onSubmit} />);
    resolveSubmit(true);
    await waitFor(() =>
      expect(screen.getByTestId("chat-input")).toHaveValue("待发送草稿"),
    );
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
