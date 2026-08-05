import { useEffect, useState } from "react";
import { getVoiceConfig } from "../api";

export interface VoiceCapabilities {
  ttsAvailable: boolean;
  sttAvailable: boolean;
  /** Configured Hermes provider names, for display only. */
  ttsProvider?: string;
  sttProvider?: string;
  /** Set when Hermes speech tools are missing, explaining how to install them. */
  detail?: string;
}

export function useVoiceCapabilities(): VoiceCapabilities {
  const [caps, setCaps] = useState<VoiceCapabilities>({ ttsAvailable: true, sttAvailable: true });

  useEffect(() => {
    getVoiceConfig()
      .then((data: any) => {
        const enabled: boolean = data?.voice_enabled !== false;
        setCaps({
          ttsAvailable: enabled && data?.tts_available !== false,
          sttAvailable: enabled && data?.stt_available !== false,
          ttsProvider: data?.tts_provider || "",
          sttProvider: data?.stt_provider || "",
          detail: data?.detail || "",
        });
      })
      .catch(() => {
        // On error leave optimistic values as-is so buttons stay visible
        setCaps((prev) => prev);
      });
  }, []);

  return caps;
}
