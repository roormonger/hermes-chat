"""Voice support: Hermes STT/TTS providers with plugin Edge/Whisper fallback.

Preferred path (Desktop parity): ``tools.tts_tool.text_to_speech_tool`` and
``tools.transcription_tools.transcribe_audio``, using ``~/.hermes`` ``tts.`` /
``stt.`` config. Falls back to plugin-owned Edge TTS + faster-whisper when
Hermes tools are not importable or fail.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes_chat.voice")

# --- Lazy singletons (plugin fallback only) ----------------------------------

_whisper_model = None

# --- Edge TTS voice mapping (ISO 639-1 → Edge Neural voice name) -------------

EDGE_VOICE_MAP: dict[str, str] = {
    "en": "en-US-AriaNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "nl": "nl-NL-ColetteNeural",
    "pl": "pl-PL-ZofiaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "tr": "tr-TR-EmelNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ar": "ar-SA-ZariyahNeural",
    "cs": "cs-CZ-VlastaNeural",
    "el": "el-GR-AthinaNeural",
    "fi": "fi-FI-SelmaNeural",
    "hu": "hu-HU-NoemiNeural",
    "no": "nb-NO-PernilleNeural",
    "ro": "ro-RO-AlinaNeural",
    "sv": "sv-SE-SofieNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "hi": "hi-IN-SwaraNeural",
}


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


def plugin_tts_available() -> bool:
    return importlib_find("edge_tts")


def plugin_stt_available() -> bool:
    return importlib_find("faster_whisper") and importlib_find("imageio_ffmpeg")


def importlib_find(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def _get_whisper():
    """Lazy-load the Whisper model (downloads on first call)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        model_size = os.environ.get("WHISPER_MODEL", "base")
        device = os.environ.get("WHISPER_DEVICE", "cpu")
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
        logger.info("Loading Whisper model '%s' (device=%s, compute=%s)", model_size, device, compute_type)
        _whisper_model = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _whisper_model


def _get_ffmpeg() -> str:
    """Return path to ffmpeg — system install if available, else bundled."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    from imageio_ffmpeg import get_ffmpeg_exe

    return get_ffmpeg_exe()


def _detect_language(text: str) -> str:
    """Detect the language of text, return ISO 639-1 code. Falls back to 'en'."""
    try:
        from langdetect import detect

        lang = detect(text)
        return lang if lang in EDGE_VOICE_MAP else "en"
    except Exception:
        return "en"


def _get_edge_voice(lang: str) -> str:
    """Return the Edge TTS voice name for a language code."""
    return EDGE_VOICE_MAP.get(lang, EDGE_VOICE_MAP["en"])


def _copy_to_temp(src: Path) -> Path:
    """Copy *src* into a new tempfile so callers can delete Hermes/plugin outputs safely."""
    suffix = src.suffix or ".mp3"
    dest = Path(tempfile.mktemp(suffix=suffix))
    shutil.copy2(src, dest)
    return dest


def _synthesize_hermes(text: str) -> tuple[Path, str]:
    """Synthesize via Hermes ``text_to_speech_tool``. Returns (path, provider)."""
    from tools.tts_tool import text_to_speech_tool

    result_json = text_to_speech_tool(text)
    result = json.loads(result_json) if isinstance(result_json, str) else result_json
    if not isinstance(result, dict) or not result.get("success"):
        err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
        raise RuntimeError(f"Hermes TTS failed: {err}")
    file_path = result.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        raise RuntimeError("Hermes TTS returned no audio file")
    src = Path(file_path)
    out = _copy_to_temp(src)
    try:
        src.unlink(missing_ok=True)
    except OSError:
        pass
    provider = str(result.get("provider") or "hermes")
    logger.info("Synthesized %d chars via Hermes TTS provider=%s", len(text), provider)
    return out, provider


async def _synthesize_plugin(text: str, lang: Optional[str], voice: Optional[str]) -> Path:
    """Synthesize via plugin Edge TTS. Returns path to mp3."""
    if voice is None:
        if lang is None:
            lang = _detect_language(text)
        voice = _get_edge_voice(lang)
    out_path = Path(tempfile.mktemp(suffix=".mp3"))
    try:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out_path))
        logger.info("Synthesized %d chars via plugin Edge TTS voice '%s'", len(text), voice)
        return out_path
    except Exception:
        out_path.unlink(missing_ok=True)
        raise


def _transcribe_hermes(audio_path: Path) -> tuple[str, str]:
    """Transcribe via Hermes ``transcribe_audio``. Returns (text, provider)."""
    from tools.transcription_tools import transcribe_audio

    result = transcribe_audio(str(audio_path))
    if not isinstance(result, dict) or not result.get("success"):
        err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
        raise RuntimeError(f"Hermes STT failed: {err}")
    text = str(result.get("transcript") or "").strip()
    provider = str(result.get("provider") or "hermes")
    logger.info("Transcribed %d chars via Hermes STT provider=%s", len(text), provider)
    return text, provider


def _transcribe_plugin(audio_path: Path) -> str:
    """Transcribe via plugin faster-whisper (converts to 16kHz wav first)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)

    try:
        ffmpeg = _get_ffmpeg()
        subprocess.run(
            [ffmpeg, "-i", str(audio_path), "-ar", "16000", "-ac", "1", "-y", str(wav_path)],
            check=True,
            capture_output=True,
        )
        model = _get_whisper()
        segments, _info = model.transcribe(str(wav_path), beam_size=5)
        text = " ".join(segment.text for segment in segments).strip()
        logger.info("Transcribed %d chars via plugin Whisper", len(text))
        return text
    finally:
        wav_path.unlink(missing_ok=True)


def transcribe(audio_path: Path) -> dict:
    """Transcribe an audio file. Prefer Hermes STT; fall back to plugin Whisper.

    Returns ``{"text": str, "provider": str, "source": "hermes"|"plugin"}``.
    """
    if hermes_stt_available():
        try:
            text, provider = _transcribe_hermes(audio_path)
            return {"text": text, "provider": provider, "source": "hermes"}
        except Exception as exc:
            logger.warning("Hermes STT failed, falling back to plugin: %s", exc)

    text = _transcribe_plugin(audio_path)
    return {"text": text, "provider": "faster-whisper", "source": "plugin"}


async def synthesize(
    text: str,
    lang: Optional[str] = None,
    voice: Optional[str] = None,
) -> dict:
    """Synthesize speech. Prefer Hermes TTS; fall back to plugin Edge TTS.

    Returns ``{"path": Path, "provider": str, "source": "hermes"|"plugin"}``.
    Hermes uses ``tts.`` from ``~/.hermes/config.yaml`` (ignores *voice*/*lang*).
    """
    if not text.strip():
        raise ValueError("Cannot synthesize empty text")

    if hermes_tts_available():
        try:
            # Hermes tool is sync and may hit network/disk — keep event loop free.
            import asyncio

            path, provider = await asyncio.to_thread(_synthesize_hermes, text)
            return {"path": path, "provider": provider, "source": "hermes"}
        except Exception as exc:
            logger.warning("Hermes TTS failed, falling back to plugin: %s", exc)

    path = await _synthesize_plugin(text, lang, voice)
    return {"path": path, "provider": voice or "edge", "source": "plugin"}
