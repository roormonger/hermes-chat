import { useCallback, useState } from "react";

const STORAGE_KEY = "hermes_auto_speak";

function readStored(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

export function useAutoSpeak() {
  const [enabled, setEnabled] = useState<boolean>(readStored);

  const persist = useCallback((next: boolean) => {
    try {
      localStorage.setItem(STORAGE_KEY, String(next));
    } catch {
      /* ignore */
    }
    setEnabled(next);
  }, []);

  const toggle = useCallback(() => {
    setEnabled((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(STORAGE_KEY, String(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  return {
    autoSpeak: enabled,
    toggleAutoSpeak: toggle,
    /** Force on — used when entering voice mode so replies always speak. */
    enableAutoSpeak: () => persist(true),
  };
}
