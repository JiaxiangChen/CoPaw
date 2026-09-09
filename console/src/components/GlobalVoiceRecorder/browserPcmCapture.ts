import { StreamingLinearResampler, VOICE_WAV_SAMPLE_RATE } from "./audio";

interface WebkitAudioWindow extends Window {
  webkitAudioContext?: typeof AudioContext;
}

export interface BrowserPcmCaptureHandlers {
  onSamples: (samples: Float32Array) => void;
  onDeviceEnded: () => void;
  onStreamChange?: (stream: MediaStream | null) => void;
}

export function isBrowserPcmCaptureSupported(): boolean {
  if (typeof window === "undefined" || typeof navigator === "undefined") {
    return false;
  }
  const AudioContextConstructor =
    window.AudioContext ?? (window as WebkitAudioWindow).webkitAudioContext;
  return Boolean(
    navigator.mediaDevices?.getUserMedia &&
      AudioContextConstructor &&
      window.AudioWorkletNode,
  );
}

export class BrowserPcmCapture {
  private audioContext: AudioContext | null = null;
  private mediaStream: MediaStream | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private silentGain: GainNode | null = null;
  private stopped = false;
  private intentionalStop = false;
  private resampler: StreamingLinearResampler | null = null;
  private readonly endedTracks = new Set<MediaStreamTrack>();

  constructor(private readonly handlers: BrowserPcmCaptureHandlers) {}

  async start(): Promise<void> {
    if (!isBrowserPcmCaptureSupported()) {
      throw new Error("Browser PCM capture is not supported.");
    }

    const AudioContextConstructor =
      window.AudioContext ?? (window as WebkitAudioWindow).webkitAudioContext;
    if (!AudioContextConstructor) {
      throw new Error("AudioContext is unavailable.");
    }

    try {
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (this.stopped) {
        this.mediaStream.getTracks().forEach((track) => track.stop());
        return;
      }
      this.handlers.onStreamChange?.(this.mediaStream);

      this.audioContext = new AudioContextConstructor({
        latencyHint: "interactive",
      });
      this.resampler = new StreamingLinearResampler(
        this.audioContext.sampleRate,
        VOICE_WAV_SAMPLE_RATE,
      );
      await this.audioContext.audioWorklet.addModule(
        new URL("./pcmCapture.worklet.js", import.meta.url),
      );

      this.sourceNode = this.audioContext.createMediaStreamSource(
        this.mediaStream,
      );
      this.workletNode = new AudioWorkletNode(
        this.audioContext,
        "copaw-pcm-capture",
        {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          outputChannelCount: [1],
          channelCount: 1,
          channelCountMode: "explicit",
        },
      );
      this.silentGain = this.audioContext.createGain();
      this.silentGain.gain.value = 0;
      this.workletNode.port.onmessage = (event: MessageEvent<Float32Array>) => {
        if (this.stopped || !this.resampler) {
          return;
        }
        const samples = this.resampler.process(event.data);
        if (samples.length > 0) {
          this.handlers.onSamples(samples);
        }
      };

      this.mediaStream.getAudioTracks().forEach((track) => {
        const handleEnded = () => {
          if (
            this.intentionalStop ||
            this.stopped ||
            this.endedTracks.has(track)
          ) {
            return;
          }
          this.endedTracks.add(track);
          this.handlers.onDeviceEnded();
        };
        track.addEventListener("ended", handleEnded, { once: true });
      });

      this.sourceNode
        .connect(this.workletNode)
        .connect(this.silentGain)
        .connect(this.audioContext.destination);
      if (this.audioContext.state === "suspended") {
        await this.audioContext.resume();
      }
    } catch (error) {
      await this.stop();
      throw error;
    }
  }

  async stop(): Promise<void> {
    if (this.stopped) {
      return;
    }
    this.stopped = true;
    this.intentionalStop = true;
    if (this.workletNode) {
      this.workletNode.port.onmessage = null;
    }
    this.sourceNode?.disconnect();
    this.workletNode?.disconnect();
    this.silentGain?.disconnect();
    this.mediaStream?.getTracks().forEach((track) => track.stop());
    this.handlers.onStreamChange?.(null);
    if (this.audioContext && this.audioContext.state !== "closed") {
      await this.audioContext.close();
    }
    this.sourceNode = null;
    this.workletNode = null;
    this.silentGain = null;
    this.mediaStream = null;
    this.audioContext = null;
    this.resampler = null;
  }
}
