import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  let handlers: {
    onSamples: (samples: Float32Array) => void;
    onDeviceEnded: () => void;
    onStreamChange?: (stream: MediaStream | null) => void;
  } | null = null;
  const capture = {
    start: vi.fn(async () => undefined),
    stop: vi.fn(async () => handlers?.onStreamChange?.(null)),
  };
  return {
    capture,
    getHandlers: () => handlers,
    reset: () => {
      handlers = null;
      capture.start.mockClear();
      capture.stop.mockClear();
      capture.start.mockResolvedValue(undefined);
      capture.stop.mockResolvedValue(undefined);
    },
    setHandlers: (next: typeof handlers) => {
      handlers = next;
    },
    transcribe: vi.fn(),
    configured: true,
  };
});

vi.mock("@/components/GlobalVoiceRecorder/browserPcmCapture", () => ({
  BrowserPcmCapture: class {
    constructor(handlers: Parameters<typeof mocks.setHandlers>[0]) {
      mocks.setHandlers(handlers);
      return mocks.capture;
    }
  },
  isBrowserPcmCaptureSupported: () => true,
}));

vi.mock("@/components/GlobalVoiceRecorder/transcription", () => ({
  isVoiceTranscriptionConfigured: () => mocks.configured,
  transcribeRecording: (...args: unknown[]) => mocks.transcribe(...args),
}));

import useSpeech from "./useSpeech";

beforeEach(() => {
  mocks.reset();
  mocks.configured = true;
  mocks.transcribe.mockResolvedValue("转写结果");
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function begin(onSpeech = vi.fn()) {
  const hook = renderHook(() => useSpeech(onSpeech));
  await act(async () => {
    await hook.result.current.start();
  });
  return { ...hook, onSpeech };
}

describe("chat dictation lifecycle", () => {
  it("captures WAV audio, sends it to the transcription adapter, and commits its text", async () => {
    const { result, onSpeech } = await begin();
    expect(result.current.status).toBe("listening");
    act(() => mocks.getHandlers()?.onSamples(new Float32Array([0.1, -0.1])));
    act(() => result.current.stop());

    await waitFor(() => expect(onSpeech).toHaveBeenCalledWith("转写结果"));
    expect(mocks.transcribe).toHaveBeenCalledOnce();
    const [file] = mocks.transcribe.mock.calls[0];
    expect(file).toBeInstanceOf(File);
    expect((file as File).type).toBe("audio/wav");
    expect(result.current.status).toBe("idle");
    expect(result.current.stream).toBeNull();
  });

  it("cancels an active transcription without appending late text", async () => {
    let resolve!: (value: string) => void;
    mocks.transcribe.mockReturnValueOnce(
      new Promise<string>((done) => {
        resolve = done;
      }),
    );
    const { result, onSpeech } = await begin();
    act(() => mocks.getHandlers()?.onSamples(new Float32Array([0.1])));
    act(() => result.current.stop());
    await waitFor(() => expect(result.current.status).toBe("transcribing"));

    act(() => result.current.cancel());
    await act(async () => resolve("迟到的文字"));

    expect(onSpeech).not.toHaveBeenCalled();
    expect(result.current.status).toBe("idle");
  });

  it("reports no-speech when the capture has no samples", async () => {
    const { result, onSpeech } = await begin();
    act(() => result.current.stop());

    await waitFor(() => expect(result.current.error).toContain("未识别到语音"));
    expect(onSpeech).not.toHaveBeenCalled();
    expect(mocks.transcribe).not.toHaveBeenCalled();
  });

  it("does not start recording before the transcription adapter is configured", async () => {
    mocks.configured = false;
    const { result } = renderHook(() => useSpeech(vi.fn()));
    await act(async () => {
      await result.current.start();
    });

    expect(result.current.error).toContain("尚未配置");
    expect(mocks.capture.start).not.toHaveBeenCalled();
  });

  it("preserves the latest input callback until transcription completes", async () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { result, rerender } = renderHook(
      ({ callback }) => useSpeech(callback),
      { initialProps: { callback: first } },
    );
    await act(async () => {
      await result.current.start();
    });
    act(() => mocks.getHandlers()?.onSamples(new Float32Array([0.1])));
    rerender({ callback: latest });
    act(() => result.current.stop());

    await waitFor(() => expect(latest).toHaveBeenCalledWith("转写结果"));
    expect(first).not.toHaveBeenCalled();
  });

  it("cleans up microphone capture when the component unmounts", async () => {
    const { unmount } = await begin();
    unmount();
    expect(mocks.capture.stop).toHaveBeenCalled();
  });
});
