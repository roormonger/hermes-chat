/**
 * Browsers block Audio.play() until the document has a user gesture.
 * Voice mode starts on a click, but the reply may arrive many seconds later —
 * after that gesture window is gone — so play() fails with NotAllowedError
 * unless we unlock playback while we still have the click.
 */

let unlocked = false;
let silentEl: HTMLAudioElement | null = null;

/** Tiny silent WAV (one sample) as a data URL — enough to satisfy autoplay policy. */
const SILENT_WAV =
  "data:audio/wav;base64,UklGRigAAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA";

export function unlockAudioPlayback(): void {
  if (unlocked) return;
  try {
    const AudioCtx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (AudioCtx) {
      const ctx = new AudioCtx();
      void ctx.resume();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      gain.gain.value = 0;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(0);
      osc.stop(0.01);
      window.setTimeout(() => {
        void ctx.close();
      }, 50);
    }
  } catch {
    /* fall through to the element path */
  }

  try {
    if (!silentEl) {
      silentEl = new Audio(SILENT_WAV);
      silentEl.preload = "auto";
      silentEl.volume = 0.01;
    }
    void silentEl
      .play()
      .then(() => {
        silentEl?.pause();
        unlocked = true;
      })
      .catch(() => {
        /* still try real replies; user may have gesture elsewhere */
      });
  } catch {
    /* ignore */
  }
}

/** Play a blob URL; rejects on autoplay / decode failure so callers can surface it. */
export async function startAudioPlayback(url: string): Promise<HTMLAudioElement> {
  const audio = new Audio(url);
  try {
    await audio.play();
  } catch (err: unknown) {
    const name = (err as { name?: string })?.name || "";
    if (name === "NotAllowedError") {
      throw new Error(
        "Browser blocked audio playback. Toggle Voice mode off and on, then ask again."
      );
    }
    throw err instanceof Error ? err : new Error(String(err));
  }
  return audio;
}
