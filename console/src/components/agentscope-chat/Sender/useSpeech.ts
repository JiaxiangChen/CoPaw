import { useCallback, useEffect, useRef, useState } from "react";
import { useEvent } from "rc-util";

import {
  BrowserPcmCapture,
  isBrowserPcmCaptureSupported,
} from "@/components/GlobalVoiceRecorder/browserPcmCapture";
import { mergePcmChunks } from "@/components/GlobalVoiceRecorder/audio";
import {
  isVoiceTranscriptionConfigured,
  transcribeRecording,
} from "@/components/GlobalVoiceRecorder/transcription";
import { createVoiceRecordingFile } from "@/components/GlobalVoiceRecorder/wav";

type SpeechStatus = "idle" | "requesting" | "listening" | "transcribing";

const TRANSCRIPTION_NOT_CONFIGURED = "语音转写服务尚未配置，请稍后重试。";
const TRANSCRIPTION_FAILED = "语音转写失败，请重试；原有草稿已保留。";

interface DictationSession {
  capture: BrowserPcmCapture;
  chunks: Float32Array[];
  abortController: AbortController | null;
  timer: ReturnType<typeof setTimeout>;
  finalizing: boolean;
}

function captureErrorMessage(error: unknown): string {
  const name =
    error instanceof Error || error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return "无法使用麦克风，请在浏览器设置中允许麦克风权限后重试。";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "未找到可用的麦克风，请检查设备连接。";
  }
  return "无法启动语音输入，请检查麦克风是否被占用后重试。";
}

export default function useSpeech(onSpeech: (transcript: string) => void) {
  const onResult = useEvent(onSpeech);
  const supported = isBrowserPcmCaptureSupported();
  const [status, setStatus] = useState<SpeechStatus>("idle");
  const [error, setError] = useState("");
  const [stream, setStream] = useState<MediaStream | null>(null);
  const sessionRef = useRef<DictationSession | null>(null);
  const statusRef = useRef<SpeechStatus>("idle");

  const setSpeechStatus = useCallback((nextStatus: SpeechStatus) => {
    statusRef.current = nextStatus;
    setStatus(nextStatus);
  }, []);

  const cancel = useCallback(() => {
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) {
      clearTimeout(session.timer);
      session.abortController?.abort();
      void session.capture.stop();
    }
    setStream(null);
    setSpeechStatus("idle");
  }, [setSpeechStatus]);

  useEffect(() => cancel, [cancel]);

  const finish = useCallback(
    async (session: DictationSession) => {
      if (sessionRef.current !== session || session.finalizing) return;
      session.finalizing = true;
      clearTimeout(session.timer);
      setSpeechStatus("transcribing");
      try {
        await session.capture.stop();
        const samples = mergePcmChunks(session.chunks);
        if (!samples.length) {
          throw new Error("empty-audio");
        }
        const file = createVoiceRecordingFile(samples);
        session.abortController = new AbortController();
        const text = await transcribeRecording(
          file,
          undefined,
          fetch,
          session.abortController.signal,
        );
        if (sessionRef.current === session && text) {
          sessionRef.current = null;
          setStream(null);
          setSpeechStatus("idle");
          onResult(text);
        }
      } catch (cause) {
        if (sessionRef.current !== session) return;
        sessionRef.current = null;
        setStream(null);
        setSpeechStatus("idle");
        setError(
          cause instanceof Error && cause.message === "empty-audio"
            ? "未识别到语音，请靠近麦克风后重试。"
            : TRANSCRIPTION_FAILED,
        );
      }
    },
    [onResult, setSpeechStatus],
  );

  const start = useCallback(async () => {
    if (!supported || sessionRef.current) return;
    if (!isVoiceTranscriptionConfigured()) {
      setError(TRANSCRIPTION_NOT_CONFIGURED);
      return;
    }
    setError("");
    setSpeechStatus("requesting");
    const session = {} as DictationSession;
    const capture = new BrowserPcmCapture({
      onSamples: (samples) => {
        if (
          sessionRef.current === session &&
          statusRef.current === "listening"
        ) {
          session.chunks.push(samples);
        }
      },
      onDeviceEnded: () => void finish(session),
      onStreamChange: (nextStream) => {
        if (sessionRef.current === session) setStream(nextStream);
      },
    });
    Object.assign(session, {
      capture,
      chunks: [],
      abortController: null,
      timer: setTimeout(() => {
        if (sessionRef.current !== session) return;
        cancel();
        setError("启动语音输入超时，请检查麦克风权限后重试。");
      }, 20_000),
      finalizing: false,
    });
    sessionRef.current = session;
    try {
      await capture.start();
      if (sessionRef.current !== session) {
        await capture.stop();
        return;
      }
      clearTimeout(session.timer);
      setSpeechStatus("listening");
    } catch (cause) {
      if (sessionRef.current !== session) return;
      sessionRef.current = null;
      clearTimeout(session.timer);
      setStream(null);
      setSpeechStatus("idle");
      setError(captureErrorMessage(cause));
    }
  }, [cancel, finish, setSpeechStatus, supported]);

  const stop = useCallback(() => {
    const session = sessionRef.current;
    if (!session || statusRef.current !== "listening") return;
    void finish(session);
  }, [finish]);

  return {
    supported,
    status,
    preview: "",
    error,
    stream,
    start,
    stop,
    cancel,
  };
}
