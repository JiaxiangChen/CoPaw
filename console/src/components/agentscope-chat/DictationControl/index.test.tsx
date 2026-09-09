import { useState } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DictationControl from "./index";
import { appendChatInputText } from "../chatInputDraft";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ i18n: { language: "zh" } }),
}));
const microphoneStop = vi.fn();
const abort = vi.fn();
const recognition = {
  onstart: null as null | (() => void),
  onend: null as null | (() => void),
  onresult: null as
    | null
    | ((event: {
        results: { isFinal: boolean; 0: { transcript: string } }[];
      }) => void),
  start() {
    this.onstart?.();
  },
  stop() {
    this.onend?.();
  },
  abort,
};
const getUserMedia = vi.fn(async () => ({
  getTracks: () => [{ stop: microphoneStop }],
}));
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
beforeEach(() => {
  vi.stubGlobal(
    "SpeechRecognition",
    class {
      constructor() {
        return recognition;
      }
    },
  );
  vi.stubGlobal("isSecureContext", true);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});
const begin = async () => {
  fireEvent.click(screen.getByRole("button", { name: "语音输入" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "停止语音输入" })).toBeEnabled(),
  );
};
describe("DictationControl", () => {
  it("previews speech, blocks send and appends to the latest editable draft on stop", async () => {
    render(<Composer />);
    await begin();
    act(
      () =>
        recognition.onresult?.({
          results: [{ isFinal: false, 0: { transcript: "听写结果" } }],
        }),
    );
    expect(screen.getByRole("status")).toHaveTextContent("听写结果");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "修改后的草稿" },
    });
    fireEvent.click(screen.getByRole("button", { name: "停止语音输入" }));
    expect(screen.getByRole("textbox")).toHaveValue("修改后的草稿\n听写结果");
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
    expect(microphoneStop).toHaveBeenCalled();
  });
  it("supports Escape cancellation with focus restored and draft preserved", async () => {
    render(<Composer />);
    await begin();
    fireEvent.keyDown(screen.getByRole("button", { name: "取消语音输入" }), {
      key: "Escape",
    });
    expect(screen.getByRole("textbox")).toHaveValue("原有草稿");
    expect(screen.getByRole("button", { name: "语音输入" })).toHaveFocus();
    expect(abort).toHaveBeenCalled();
  });
  it("cancels active capture when the composer is disabled", async () => {
    const { rerender } = render(<Composer />);
    await begin();
    rerender(<Composer disabled />);
    expect(
      screen.queryByRole("button", { name: "停止语音输入" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "语音输入" })).toBeDisabled();
    expect(microphoneStop).toHaveBeenCalled();
  });
  it("explains unsupported browsers without requesting the microphone", () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "语音输入" }));
    expect(screen.getByRole("alert")).toHaveTextContent("不支持语音输入");
    expect(getUserMedia).not.toHaveBeenCalled();
  });
});
