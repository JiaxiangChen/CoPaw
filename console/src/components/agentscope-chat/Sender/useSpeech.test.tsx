import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import useSpeech, { type SpeechRecognitionInstance } from "./useSpeech";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ i18n: { language: "zh" } }),
}));

let recognition: FakeRecognition;
class FakeRecognition implements SpeechRecognitionInstance {
  continuous = false;
  interimResults = false;
  lang = "";
  onstart: SpeechRecognitionInstance["onstart"] = null;
  onend: SpeechRecognitionInstance["onend"] = null;
  onerror: SpeechRecognitionInstance["onerror"] = null;
  onresult: SpeechRecognitionInstance["onresult"] = null;
  start = vi.fn(() => this.onstart?.());
  stop = vi.fn();
  abort = vi.fn();
  constructor() {
    // The browser constructor exposes this instance to the fake event source.
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    recognition = this;
  }
  result(...texts: string[]) {
    this.onresult?.({
      results: texts.map((transcript, i) => ({
        isFinal: i === 0,
        0: { transcript },
      })),
    });
  }
}
const stopTrack = vi.fn();
const stream = {
  getTracks: () => [{ stop: stopTrack }],
} as unknown as MediaStream;
const getUserMedia = vi.fn();

beforeEach(() => {
  vi.stubGlobal("SpeechRecognition", FakeRecognition);
  vi.stubGlobal("isSecureContext", true);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  getUserMedia.mockResolvedValue(stream);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  vi.useRealTimers();
});

async function begin(onSpeech = vi.fn()) {
  const hook = renderHook(() => useSpeech(onSpeech));
  await act(async () => {
    await hook.result.current.start();
  });
  return { ...hook, onSpeech };
}

describe("chat dictation lifecycle", () => {
  it("previews cumulative results without duplication and commits once on stop", async () => {
    const { result, onSpeech } = await begin();
    expect(recognition.lang).toBe("zh-CN");
    expect(recognition.continuous).toBe(true);
    act(() => {
      recognition.result("你好", "世界");
      recognition.result("你好", "世界！");
    });
    expect(result.current.preview).toBe("你好世界！");
    expect(onSpeech).not.toHaveBeenCalled();
    act(() => result.current.stop());
    expect(stopTrack).toHaveBeenCalled();
    expect(result.current.status).toBe("stopping");
    act(() => recognition.onend?.());
    expect(onSpeech).toHaveBeenCalledExactlyOnceWith("你好世界！");
    expect(result.current.status).toBe("idle");
  });

  it("cancels without committing and ignores late recognition callbacks", async () => {
    const { result, onSpeech } = await begin();
    const lateResult = recognition.onresult;
    const lateEnd = recognition.onend;
    act(() => {
      recognition.result("丢弃");
      result.current.cancel();
      lateResult?.({ results: [{ isFinal: true, 0: { transcript: "迟到" } }] });
      lateEnd?.();
    });
    expect(onSpeech).not.toHaveBeenCalled();
    expect(stopTrack).toHaveBeenCalled();
    expect(recognition.abort).toHaveBeenCalled();
    expect(result.current.preview).toBe("");
  });

  it("releases a microphone permission request resolved after cancellation", async () => {
    let resolve!: (value: MediaStream) => void;
    getUserMedia.mockReturnValueOnce(
      new Promise<MediaStream>((done) => {
        resolve = done;
      }),
    );
    const { result } = renderHook(() => useSpeech(vi.fn()));
    act(() => {
      void result.current.start();
    });
    act(() => result.current.cancel());
    await act(async () => resolve(stream));
    expect(stopTrack).toHaveBeenCalledOnce();
    expect(recognition.start).not.toHaveBeenCalled();
    expect(result.current.status).toBe("idle");
  });

  it("releases resources on unmount and rejects callbacks from the old conversation", async () => {
    const { unmount, onSpeech } = await begin();
    const end = recognition.onend;
    act(() => recognition.result("旧会话"));
    unmount();
    act(() => end?.());
    expect(stopTrack).toHaveBeenCalled();
    expect(onSpeech).not.toHaveBeenCalled();
  });

  it("keeps drafts untouched when permission is denied", async () => {
    getUserMedia.mockRejectedValueOnce(
      new DOMException("denied", "NotAllowedError"),
    );
    const { result, onSpeech } = await begin();
    expect(result.current.error).toContain("允许麦克风权限");
    expect(result.current.status).toBe("idle");
    expect(onSpeech).not.toHaveBeenCalled();
  });

  it("cleans up network errors and allows retry", async () => {
    const { result, onSpeech } = await begin();
    act(() => recognition.onerror?.({ error: "network" }));
    expect(result.current.error).toContain("连接失败");
    expect(stopTrack).toHaveBeenCalled();
    await act(async () => result.current.start());
    expect(result.current.status).toBe("listening");
    expect(result.current.error).toBe("");
    expect(onSpeech).not.toHaveBeenCalled();
  });

  it("recovers if the browser never ends recognition after stop", async () => {
    vi.useFakeTimers();
    const { result, onSpeech } = await begin();
    act(() => recognition.result("保留文字"));
    act(() => result.current.stop());
    act(() => vi.advanceTimersByTime(5000));
    expect(result.current.status).toBe("idle");
    expect(onSpeech).toHaveBeenCalledExactlyOnceWith("保留文字");
  });

  it("does not open the microphone in an unsupported browser", async () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    const { result } = renderHook(() => useSpeech(vi.fn()));
    expect(result.current.supported).toBe(false);
    await act(async () => result.current.start());
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it("retains the latest draft callback when recognition ends", async () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { result, rerender } = renderHook(
      ({ callback }) => useSpeech(callback),
      { initialProps: { callback: first } },
    );
    await act(async () => result.current.start());
    rerender({ callback: latest });
    act(() => {
      recognition.result("追加");
      recognition.onend?.();
    });
    await waitFor(() => expect(latest).toHaveBeenCalledWith("追加"));
    expect(first).not.toHaveBeenCalled();
  });
});
