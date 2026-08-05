"""Voice support backed by Hermes STT/TTS providers.

hermes-chat does not ship a speech stack of its own. Audio rides the same
providers the CLI / TUI / desktop use — ``tools.tts_tool`` and
``tools.transcription_tools`` — configured in ``~/.hermes/config.yaml`` under
``tts.`` and ``stt.``.

The gateway's own ``voice.*`` RPCs are deliberately unused: they capture and
play on the *host* machine, which never reaches a browser client. See
``docs/hermes-voice.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("hermes_chat.voice")

INSTALL_HINT = (
    "Hermes voice tools are unavailable. Install the Hermes voice extras "
    "(pip install 'hermes-agent[voice]') and configure stt./tts. in "
    "~/.hermes/config.yaml."
)


class VoiceUnavailable(RuntimeError):
    """Raised when Hermes speech tools cannot be used."""


def hermes_tts_available() -> bool:
    try:
        from tools.tts_tool import text_to_speech_tool  # noqa: F401
        return True
    except Exception:
        return False


def hermes_stt_available() -> bool:
    try:
        from tools.transcription_tools import transcribe_audio  # noqa: F401
        return True
    except Exception:
        return False


def _configured_tts_provider() -> str:
    """Best-effort read of the configured Hermes TTS provider (display only)."""
    try:
        from tools.tts_tool import _get_provider, _load_tts_config

        return str(_get_provider(_load_tts_config()) or "")
    except Exception:
        return ""


def _configured_stt_provider() -> str:
    """Best-effort read of the configured Hermes STT provider (display only)."""
    try:
        from tools.transcription_tools import _get_provider, _load_stt_config

        return str(_get_provider(_load_stt_config()) or "")
    except Exception:
        return ""


def voice_status() -> dict:
    """Availability + configured providers, shared by the app and dashboard."""
    tts = hermes_tts_available()
    stt = hermes_stt_available()
    return {
        "tts_available": tts,
        "stt_available": stt,
        "tts_backend": "hermes" if tts else "none",
        "stt_backend": "hermes" if stt else "none",
        "tts_provider": _configured_tts_provider() if tts else "",
        "stt_provider": _configured_stt_provider() if stt else "",
        "detail": "" if (tts or stt) else INSTALL_HINT,
    }


_TEMP_PREFIX = "hermes-chat-tts-"


def _synthesize_blocking(text: str) -> tuple[Path, str]:
    from tools.tts_tool import text_to_speech_tool

    # Direct the output at our own temp dir: left to itself the tool files every
    # reply in ~/voice-memos, which is for things the user asked to keep.
    target = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX)) / "speech.mp3"
    raw = text_to_speech_tool(text, output_path=str(target))
    result = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(result, dict) or not result.get("success"):
        err = result.get("error") if isinstance(result, dict) else "unknown error"
        shutil.rmtree(target.parent, ignore_errors=True)
        raise RuntimeError(f"Hermes TTS failed: {err}")

    file_path = result.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        shutil.rmtree(target.parent, ignore_errors=True)
        raise RuntimeError("Hermes TTS returned no audio file")

    return Path(file_path), str(result.get("provider") or "hermes")


def discard_audio(path: Path) -> None:
    """Drop a synthesized file once it has been sent, if we own its directory."""
    parent = Path(path).parent
    if parent.name.startswith(_TEMP_PREFIX):
        shutil.rmtree(parent, ignore_errors=True)


def transcribe(audio_path: Path) -> dict:
    """Transcribe an audio file via Hermes STT.

    Returns ``{"text": str, "provider": str}``.
    """
    if not hermes_stt_available():
        raise VoiceUnavailable(INSTALL_HINT)

    from tools.transcription_tools import transcribe_audio

    result = transcribe_audio(str(audio_path))
    if not isinstance(result, dict) or not result.get("success"):
        err = result.get("error") if isinstance(result, dict) else "unknown error"
        raise RuntimeError(f"Hermes STT failed: {err}")

    text = str(result.get("transcript") or "").strip()
    provider = str(result.get("provider") or "hermes")
    logger.info("Transcribed %d chars via Hermes STT provider=%s", len(text), provider)
    return {"text": text, "provider": provider}


async def synthesize(text: str) -> dict:
    """Synthesize speech via Hermes TTS.

    Voice and provider come from ``~/.hermes/config.yaml`` (``tts.``), the same
    as the CLI. Returns ``{"path": Path, "provider": str}``.
    """
    if not text.strip():
        raise ValueError("Cannot synthesize empty text")
    if not hermes_tts_available():
        raise VoiceUnavailable(INSTALL_HINT)

    # The Hermes tool is synchronous and may hit the network — keep the loop free.
    path, provider = await asyncio.to_thread(_synthesize_blocking, text)
    logger.info("Synthesized %d chars via Hermes TTS provider=%s", len(text), provider)
    return {"path": path, "provider": provider}
