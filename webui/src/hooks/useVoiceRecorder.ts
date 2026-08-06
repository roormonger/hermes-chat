import { useCallback, useEffect, useRef, useState } from "react";
import { transcribeAudio } from "../api";

export type VoiceRecorderOptions = {
  /** End the capture automatically once the speaker goes quiet (hands-free mode). */
  autoStopOnSilence?: boolean;
  /** Quiet time that ends a phrase. */
  silenceMs?: number;
  /** Give up when nobody speaks at all. */
  noSpeechTimeoutMs?: number;
  /** Hard cap on a single utterance. */
  maxDurationMs?: number;
  /** Capture ended without producing a transcript (silence, empty, or failure). */
  onIdle?: () => void;
  onError?: (err: unknown) => void;
};

// RMS thresholds with hysteresis: speech has to cross the upper bound before a
// dip below the lower bound counts as the end of a phrase.
const SPEECH_RMS = 0.025;
const SILENCE_RMS = 0.015;
const MONITOR_INTERVAL_MS = 100;

export function useVoiceRecorder(
  onTranscript: (text: string) => void,
  options: VoiceRecorderOptions = {},
) {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const discardRef = useRef(false);
  const startingRef = useRef(false);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const monitorRef = useRef<number | null>(null);

  // Keep callbacks/options in refs so `start` stays referentially stable even
  // when the caller passes inline objects.
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const teardownMonitor = useCallback(() => {
    if (monitorRef.current !== null) {
      window.clearInterval(monitorRef.current);
      monitorRef.current = null;
    }
    const ctx = audioCtxRef.current;
    audioCtxRef.current = null;
    if (ctx && ctx.state !== "closed") ctx.close().catch(() => {});
  }, []);

  const finish = useCallback((discard: boolean) => {
    const recorder = mediaRecorderRef.current;
    if (!recorder || recorder.state === "inactive") return;
    discardRef.current = discard;
    recorder.stop();
    mediaRecorderRef.current = null;
    setRecording(false);
  }, []);

  const startMonitor = useCallback((stream: MediaStream) => {
    const AudioCtx: typeof AudioContext | undefined =
      window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return;

    const {
      silenceMs = 1100,
      noSpeechTimeoutMs = 7000,
      maxDurationMs = 60000,
    } = optionsRef.current;

    const ctx = new AudioCtx();
    audioCtxRef.current = ctx;
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);

    const startedAt = Date.now();
    let heardSpeech = false;
    let quietSince = 0;

    monitorRef.current = window.setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
      const rms = Math.sqrt(sum / samples.length);
      const now = Date.now();

      if (rms >= SPEECH_RMS) {
        heardSpeech = true;
        quietSince = 0;
      } else if (rms < SILENCE_RMS && quietSince === 0) {
        quietSince = now;
      }

      if (heardSpeech && quietSince && now - quietSince >= silenceMs) {
        finish(false);
      } else if (!heardSpeech && now - startedAt >= noSpeechTimeoutMs) {
        finish(true);
      } else if (now - startedAt >= maxDurationMs) {
        finish(!heardSpeech);
      }
    }, MONITOR_INTERVAL_MS);
  }, [finish]);

  const start = useCallback(async () => {
    // getUserMedia is awaited below, so guard against a second call landing
    // before the recorder exists.
    if (mediaRecorderRef.current || startingRef.current) return;
    startingRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      discardRef.current = false;
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = async () => {
        teardownMonitor();
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        const discarded = discardRef.current;
        discardRef.current = false;
        if (discarded || blob.size === 0) {
          optionsRef.current.onIdle?.();
          return;
        }
        setTranscribing(true);
        try {
          const text = await transcribeAudio(blob);
          if (text) onTranscriptRef.current(text);
          else optionsRef.current.onIdle?.();
        } catch (e) {
          console.error("Transcription failed:", e);
          optionsRef.current.onError?.(e);
          optionsRef.current.onIdle?.();
        } finally {
          setTranscribing(false);
        }
      };
      recorder.start();
      mediaRecorderRef.current = recorder;
      setRecording(true);
      if (optionsRef.current.autoStopOnSilence) startMonitor(stream);
    } catch (e) {
      console.error("Failed to start recording:", e);
      optionsRef.current.onError?.(e);
      optionsRef.current.onIdle?.();
    } finally {
      startingRef.current = false;
    }
  }, [startMonitor, teardownMonitor]);

  const stop = useCallback(() => finish(false), [finish]);

  /** Abandon the capture without transcribing it (leaving voice mode). */
  const cancel = useCallback(() => {
    if (mediaRecorderRef.current) {
      finish(true);
      return;
    }
    teardownMonitor();
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, [finish, teardownMonitor]);

  useEffect(() => () => {
    teardownMonitor();
    streamRef.current?.getTracks().forEach((t) => t.stop());
  }, [teardownMonitor]);

  return { recording, transcribing, start, stop, cancel };
}
