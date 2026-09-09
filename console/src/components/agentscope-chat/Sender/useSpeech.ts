import { useCallback, useEffect, useRef, useState } from "react";
import { useEvent } from "rc-util";
import { useTranslation } from "react-i18next";

export interface SpeechRecognitionInstance {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onresult:
    | ((event: {
        results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
      }) => void)
    | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
}

type SpeechWindow = Window & {
  SpeechRecognition?: new () => SpeechRecognitionInstance;
  webkitSpeechRecognition?: new () => SpeechRecognitionInstance;
};
type SpeechStatus = "idle" | "starting" | "listening" | "stopping";

const SPEECH_ERRORS: Record<string, string> = {
  "not-allowed": "无法使用麦克风，请在浏览器设置中允许麦克风权限后重试。",
  "service-not-allowed": "浏览器未允许语音识别服务，请检查浏览器设置。",
  "audio-capture": "未找到可用的麦克风，请检查设备连接。",
  network: "语音识别服务连接失败，请检查网络后重试。",
  "no-speech": "未识别到语音，请靠近麦克风后重试。",
  "language-not-supported": "当前浏览器的语音服务不支持此语言。",
};

export default function useSpeech(onSpeech: (transcript: string) => void) {
  const onResult = useEvent(onSpeech);
  const { i18n } = useTranslation();
  const [status, setStatus] = useState<SpeechStatus>("idle");
  const [preview, setPreview] = useState("");
  const [error, setError] = useState("");
  const [stream, setStream] = useState<MediaStream | null>(null);
  const sessionRef = useRef<{
    recognition: SpeechRecognitionInstance;
    stream: MediaStream | null;
    transcript: string;
    timer?: ReturnType<typeof setTimeout>;
  } | null>(null);
  const speechWindow =
    typeof window === "undefined" ? undefined : (window as SpeechWindow);
  const Recognition =
    speechWindow?.SpeechRecognition || speechWindow?.webkitSpeechRecognition;
  const supported = Boolean(
    Recognition &&
      navigator.mediaDevices?.getUserMedia &&
      window.isSecureContext,
  );

  const release = useCallback(() => {
    const session = sessionRef.current;
    sessionRef.current = null;
    if (session) {
      clearTimeout(session.timer);
      session.stream?.getTracks().forEach((track) => track.stop());
      const recognition = session.recognition;
      recognition.onstart = null;
      recognition.onend = null;
      recognition.onerror = null;
      recognition.onresult = null;
      recognition.abort();
    }
  }, []);

  const cancel = useCallback(() => {
    release();
    setStatus("idle");
    setStream(null);
    setPreview("");
  }, [release]);

  useEffect(() => release, [release]);

  const start = async () => {
    if (!supported || !Recognition || sessionRef.current) return;
    setError("");
    setPreview("");
    setStatus("starting");
    const session = {
      recognition: new Recognition(),
      stream: null as MediaStream | null,
      transcript: "",
      timer: undefined as ReturnType<typeof setTimeout> | undefined,
    };
    sessionRef.current = session;
    const fail = (message: string) => {
      if (sessionRef.current !== session) return;
      cancel();
      setError(message);
    };
    session.timer = setTimeout(
      () => fail("启动语音输入超时，请检查麦克风权限后重试。"),
      20000,
    );
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (sessionRef.current !== session) {
        media.getTracks().forEach((track) => track.stop());
        return;
      }
      session.stream = media;
      setStream(media);
      const recognition = session.recognition;
      recognition.lang = i18n.language?.startsWith("en") ? "en-US" : "zh-CN";
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.onstart = () => {
        if (sessionRef.current !== session) return;
        clearTimeout(session.timer);
        setStatus("listening");
      };
      recognition.onresult = (event) => {
        if (sessionRef.current !== session) return;
        session.transcript = Array.from(event.results)
          .map((result) => result[0].transcript)
          .join("");
        setPreview(session.transcript);
      };
      recognition.onerror = (event) =>
        fail(
          SPEECH_ERRORS[event.error] ||
            "语音识别失败，请重试；原有草稿已保留。",
        );
      recognition.onend = () => {
        if (sessionRef.current !== session) return;
        const text = session.transcript.trim();
        cancel();
        if (text) onResult(text);
        else setError(SPEECH_ERRORS["no-speech"]);
      };
      recognition.start();
    } catch (cause) {
      const name =
        cause instanceof Error || cause instanceof DOMException
          ? cause.name
          : "";
      fail(
        name === "NotAllowedError"
          ? SPEECH_ERRORS["not-allowed"]
          : name === "NotFoundError"
          ? SPEECH_ERRORS["audio-capture"]
          : "无法启动语音输入，请检查麦克风是否被占用后重试。",
      );
    }
  };

  const stop = () => {
    const session = sessionRef.current;
    if (!session || status !== "listening") return;
    setStatus("stopping");
    session.stream?.getTracks().forEach((track) => track.stop());
    setStream(null);
    session.timer = setTimeout(() => {
      if (sessionRef.current !== session) return;
      const text = session.transcript.trim();
      cancel();
      if (text) onResult(text);
      else setError(SPEECH_ERRORS["no-speech"]);
    }, 5000);
    session.recognition.stop();
  };

  return { supported, status, preview, error, stream, start, stop, cancel };
}
