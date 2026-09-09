import { useEffect, useRef, useState } from "react";
import {
  AudioOutlined,
  CloseOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import useSpeech from "../Sender/useSpeech";
import styles from "./index.module.less";

function Waveform({ stream }: { stream: MediaStream | null }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!stream || !window.AudioContext) return;
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    void context.resume().catch(() => undefined);
    const data = new Uint8Array(analyser.fftSize);
    const history = Array<number>(96).fill(0);
    let frame = 0;
    let last = 0;
    const draw = (time: number) => {
      if (time - last > 65) {
        last = time;
        analyser.getByteTimeDomainData(data);
        const rms = Math.sqrt(
          data.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) /
            data.length,
        );
        history.shift();
        history.push(Math.min(1, rms * 5));
        const reducedMotion = window.matchMedia(
          "(prefers-reduced-motion: reduce)",
        ).matches;
        ref.current?.childNodes.forEach((node, index) => {
          const bar = node as HTMLElement;
          bar.style.transform = `scaleY(${
            reducedMotion ? 1 : 1 + history[index] * 7
          })`;
          bar.style.opacity = history[index] > 0.025 ? "0.8" : "0.3";
        });
      }
      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(frame);
      source.disconnect();
      void context.close().catch(() => undefined);
    };
  }, [stream]);
  return (
    <div className={styles.waveform} ref={ref} aria-hidden="true">
      {Array.from({ length: 96 }, (_, i) => (
        <span key={i} />
      ))}
    </div>
  );
}

interface DictationControlProps {
  disabled?: boolean;
  onTranscript: (text: string) => void;
  onActiveChange: (active: boolean) => void;
}

export default function DictationControl({
  disabled,
  onTranscript,
  onActiveChange,
}: DictationControlProps) {
  const speech = useSpeech(onTranscript);
  const [showUnsupported, setShowUnsupported] = useState(false);
  const active = speech.status !== "idle";
  const { cancel } = speech;
  const startRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const wasActive = useRef(false);
  useEffect(() => {
    onActiveChange(active);
    if (active && !wasActive.current) cancelRef.current?.focus();
    if (!active && wasActive.current) startRef.current?.focus();
    wasActive.current = active;
  }, [active, onActiveChange]);
  useEffect(() => {
    if (disabled) cancel();
  }, [disabled, cancel]);
  useEffect(() => () => onActiveChange(false), [onActiveChange]);
  const hint = speech.supported
    ? "语音输入"
    : "当前环境不支持语音输入，请使用支持语音识别的浏览器并通过 HTTPS 访问。";
  return (
    <>
      {!active ? (
        <button
          ref={startRef}
          type="button"
          className={styles.button}
          aria-label="语音输入"
          title={hint}
          aria-disabled={disabled || !speech.supported}
          disabled={disabled}
          onClick={() => {
            if (speech.supported) void speech.start();
            else setShowUnsupported(true);
          }}
        >
          <AudioOutlined />
        </button>
      ) : (
        <div
          className={styles.session}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              speech.cancel();
            }
          }}
        >
          <div className={styles.preview} role="status">
            {speech.preview ||
              (speech.status === "starting"
                ? "正在启动麦克风…"
                : speech.status === "stopping"
                ? "正在整理文字…"
                : "请说话，停止后填入输入框")}
          </div>
          <div className={styles.row}>
            <button
              ref={cancelRef}
              className={styles.button}
              type="button"
              aria-label="取消语音输入"
              title="取消语音输入（Esc）"
              onClick={speech.cancel}
            >
              <CloseOutlined />
            </button>
            <Waveform stream={speech.stream} />
            <button
              className={styles.button}
              type="button"
              aria-label="停止语音输入"
              title="停止并填入文字"
              disabled={speech.status !== "listening"}
              onClick={speech.stop}
            >
              {speech.status === "listening" ? (
                <span className={styles.stop} />
              ) : (
                <LoadingOutlined spin />
              )}
            </button>
          </div>
        </div>
      )}
      {!active && (speech.error || showUnsupported) && (
        <div role="alert" className={styles.error}>
          {speech.error || hint}
        </div>
      )}
    </>
  );
}
