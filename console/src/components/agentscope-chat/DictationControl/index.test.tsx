import { useState } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DictationControl from "./index";
import { appendChatInputText } from "../chatInputDraft";

vi.mock("../Sender/useSpeech", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    default: function useSpeech(onSpeech: (text: string) => void) {
      const [status, setStatus] = React.useState<"idle" | "listening">("idle");
      return {
        supported: true,
        status,
        preview: "",
        error: "",
        stream: null,
        start: () => setStatus("listening"),
        stop: () => {
          setStatus("idle");
          onSpeech("听写结果");
        },
        cancel: () => setStatus("idle"),
      };
    },
  };
});
function Composer({ disabled = false }: { disabled?: boolean }) {
  const [draft, setDraft] = useState("原有草稿");
  const [active, setActive] = useState(false);
  return (
    <>
      <textarea
        aria-label="消息"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
      <DictationControl
        disabled={disabled}
        onActiveChange={setActive}
        onTranscript={(text) =>
          setDraft((current) => appendChatInputText(current, text))
        }
      />
      <button disabled={active}>发送</button>
    </>
  );
}
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
const begin = async () => {
  fireEvent.click(screen.getByRole("button", { name: "语音输入" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "停止语音输入" })).toBeEnabled(),
  );
};
describe("DictationControl", () => {
  it("blocks send and appends the converted text to the latest editable draft", async () => {
    render(<Composer />);
    await begin();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "修改后的草稿" },
    });
    fireEvent.click(screen.getByRole("button", { name: "停止语音输入" }));
    expect(screen.getByRole("textbox")).toHaveValue("修改后的草稿\n听写结果");
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
  });
  it("supports Escape cancellation with focus restored and draft preserved", async () => {
    render(<Composer />);
    await begin();
    fireEvent.keyDown(screen.getByRole("button", { name: "取消语音输入" }), {
      key: "Escape",
    });
    expect(screen.getByRole("textbox")).toHaveValue("原有草稿");
    expect(screen.getByRole("button", { name: "语音输入" })).toHaveFocus();
  });
  it("keeps the action row compact while listening and after no speech", async () => {
    render(<Composer />);
    await begin();
    expect(
      screen.queryByText("请说话，停止后填入输入框"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("正在启动麦克风…")).not.toBeInTheDocument();
    expect(screen.queryByText("正在整理文字…")).not.toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole("button", { name: "取消语音输入" }), {
      key: "Escape",
    });
    expect(screen.getByRole("button", { name: "语音输入" })).toBeVisible();
  });
  it("cancels active capture when the composer is disabled", async () => {
    const { rerender } = render(<Composer />);
    await begin();
    rerender(<Composer disabled />);
    expect(
      screen.queryByRole("button", { name: "停止语音输入" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "语音输入" })).toBeDisabled();
  });
});
