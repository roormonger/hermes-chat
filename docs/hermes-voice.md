# Hermes voice for hermes-chat (research)

How native Hermes STT/TTS relates to `tui_gateway`, and what a **browser**
client should call. Researched against local install `hermes install/hermes-agent`
**0.18.2** (2026-07). Re-diff after upgrading Hermes.

Official feature guide: [Voice Mode](https://hermes-agent.nousresearch.com/docs/user-guide/features/voice-mode)

---

## Verdict

| Path | Role for hermes-chat |
|------|----------------------|
| **Desktop HTTP** (`/api/audio/transcribe`, `/api/audio/speak`) | **Correct model** — client owns mic/speakers; Hermes providers return bytes/transcript |
| **TUI gateway** (`voice.toggle` / `voice.record` / `voice.tts`) | **Host audio only** — server mic + speakers; not usable for remote browser users |
| **Current plugin** (`hermes_chat/voice.py` → Edge + Whisper) | Parallel stack; same *shape* as Desktop HTTP, wrong *providers* |

Do **not** drive browser UX with `voice.record` / `voice.tts`. Those play and
capture on the **machine running the gateway**, not in the user’s browser.

---

## Architecture (native Hermes)

```
CLI / Ink TUI                    Desktop / browser-shaped clients
─────────────                    ────────────────────────────────
voice.toggle / .record / .tts    POST /api/audio/transcribe|speak
        │                                  │
        ▼                                  ▼
 hermes_cli.voice                  tools.transcription_tools
        │                          tools.tts_tool
        ▼                                  │
 tools.voice_mode  ──mic/speakers──┘       │
 tools.transcription_tools                 ▼
 tools.tts_tool                    JSON + data_url / file bytes
        │                                  │
        ▼                                  ▼
 voice.status / voice.transcript     <audio> / composer text
 → prompt.submit                     → prompt.submit (same session)
```

Shared brains: `tools/transcription_tools.py`, `tools/tts_tool.py`,
`~/.hermes/config.yaml` blocks `stt:` / `tts:` / `voice:`.

---

## `tui_gateway` surface

### RPCs

| Method | Params | Effect |
|--------|--------|--------|
| `voice.toggle` | `{ action: "status"\|"on"\|"off"\|"tts" }` | Runtime flags (`HERMES_VOICE` / `HERMES_VOICE_TTS`); status probes deps |
| `voice.record` | `{ action: "start"\|"stop", session_id? }` | VAD PTT on **host mic**; emits transcript events |
| `voice.tts` | `{ text }` | Synthesize and **play on host speakers** (daemon thread); returns `{ status: "speaking" }` — **no audio bytes** |

Source: `tui_gateway/server.py` (~12810–13056 in 0.18.2).

### Events

| Type | Payload | Notes |
|------|---------|-------|
| `voice.status` | `{ state: "listening"\|"transcribing"\|"idle" }` | Process-global (one mic) |
| `voice.transcript` | `{ text }` or `{ no_speech_limit: true }` | TUI auto-`prompt.submit`s text |

Auto-speak: when TTS flag is on, after a completed turn the gateway batches the
reply through `speak_text` on host speakers — **not** streaming TTS over RPC.
Streaming ElevenLabs (`stream_tts_to_speaker`) is **CLI-local** only.

### Not on the gateway

- No “return audio bytes” TTS method
- No barge-in / wake-word RPCs (CLI barge = local `stop_playback`; “wake words”
  on messaging gateways are **mention regexes**, not acoustic detection)
- Official programmatic-integration catalog omits `voice.*` (docs lag)

---

## Desktop HTTP (the web-shaped API)

In `hermes_cli/web_server.py`:

| Endpoint | Body | Response |
|----------|------|----------|
| `POST /api/audio/transcribe` | `{ data_url, mime_type? }` | `{ transcript, provider, … }` via `transcription_tools` |
| `POST /api/audio/speak` | `{ text }` | `{ data_url, mime_type, provider }` via `text_to_speech_tool` |

Desktop client records in the browser, posts audio, plays returned speech, and
submits transcript text on the normal chat session. Conversation chunking /
barge-in are **frontend** (`HTMLAudioElement`), not gateway RPCs.

hermes-chat already mirrors the **route shape** (`POST /v1/audio/transcribe`,
`POST /v1/audio/speak`) but implements STT/TTS in `hermes_chat/voice.py`
(plugin Whisper + Edge) instead of Hermes `tools.*`.

---

## Config browsers should respect (not edit in-product)

```yaml
stt:
  provider: local   # local | groq | openai | mistral | elevenlabs | …
  local: { model: base, language: "" }

tts:
  provider: edge    # edge | elevenlabs | openai | gemini | piper | …
  edge: { voice: en-US-AriaNeural }

voice:
  record_key: ctrl+b          # CLI/TUI only
  auto_tts: false
  silence_threshold: 200
  silence_duration: 3.0
```

Extras: `hermes-agent[voice]` (faster-whisper, sounddevice); `[tts-premium]`
(ElevenLabs). Env: `ELEVENLABS_API_KEY`, `GROQ_API_KEY`, `VOICE_TOOLS_OPENAI_KEY`, …

---

## Browser feasibility matrix

| Capability | Via gateway `voice.*` | Via Hermes providers (Desktop pattern) |
|------------|----------------------|----------------------------------------|
| Mic capture for remote user | No (host mic) | Yes — browser MediaRecorder → upload |
| Hear reply in browser | No (host speakers) | Yes — TTS → `data_url` / file → `<audio>` |
| Same Hermes session as text/tools/gates | Transcript → `prompt.submit` | Submit transcribed text on bound `session_id` |
| Use user’s `stt.` / `tts.` config | Indirect (host path) | Yes — call `tools.*` |
| Streaming clause TTS | Not on gateway | Client can chunk; gateway doesn’t stream |
| Acoustic wake word | N/A | N/A |
| Barge-in | CLI local stop | Stop `<audio>` in the browser |

---

## hermes-chat today

The plugin has **no speech stack of its own** — Edge TTS and faster-whisper are gone,
along with the dashboard voice picker and its "Install Voice" button.

| | Implementation |
|--|----------------|
| STT | `tools.transcription_tools.transcribe_audio` only; 503 + install hint when unavailable |
| TTS | `tools.tts_tool.text_to_speech_tool` only; voice/provider come from `tts.` config |
| Wire | `POST /v1/audio/transcribe` → `{text, provider}`; `POST /v1/audio/speak` → audio FileResponse |
| Config | `voice-config` returns `tts_available` / `stt_available`, `*_backend`, `*_provider`, `detail` |
| Session | Mic → STT → normal chat submit; reply → TTS. Nothing bypasses the gateway turn |

`/v1/audio/speak` takes only `{text}`: per-user voice selection was removed because
the voice belongs to Hermes' `tts.` config, the same as the CLI.

### Hands-free voice mode

The web UI mirrors Hermes' own voice mode instead of stopping at push-to-talk:

1. `useVoiceRecorder({ autoStopOnSilence: true })` watches mic RMS through an
   `AnalyserNode` and ends the capture ~1.5s after speech stops (with no-speech
   and max-duration cutoffs).
2. The transcript is appended to the thread automatically — no Send press.
3. On `turn_complete` the reply is spoken via Hermes TTS.
4. When playback ends the mic re-arms, so a conversation continues untouched.

The loop is state-driven in `App.tsx` (`voiceMode` / `voicePhase`): it listens only
while the thread is idle, so Hermes' own speech is never recorded back. Decision
gates deliberately pause the loop — approvals stay manual.

**Still out of scope:** `voice.record` from the browser, host auto-TTS, Discord VC,
wake words, gateway streaming TTS.

---

## Doc / catalog follow-ups

- Add `voice.*` + Desktop `/api/audio/*` notes to `docs/hermes-gateway-protocol.md`.
- Re-grep `tui_gateway/server.py` after Hermes upgrades (TODO “v2026.8.3+” may
  differ from this 0.18.2 tree).
- Voice now requires `hermes-agent[voice]` (or a cloud `stt.`/`tts.` provider) in the
  environment that runs the plugin; there is no in-plugin fallback to install.
