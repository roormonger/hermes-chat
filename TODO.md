# Hermes Chat — Improvement Backlog

## North star

Stay a **thin chat UI on top of Hermes `tui_gateway`**, not a parallel agent runtime.

- Prefer Hermes RPC / events / session identity over custom backends.
- Translate gateway events faithfully; avoid inventing parallel chat logic.
- New Hermes features (voice, redirects, slash commands, …) should land by **wiring the gateway**, not by bolting on one-off workarounds (e.g. our Edge TTS / Whisper stack).

Official surface: [programmatic integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration) · local map: `docs/hermes-gateway-protocol.md` · wrapper: `hermes_chat/gateway_session.py`

---

## 🔴 Gateway compliance (do first)

Foundation so native features (including voice) can attach without more forks.

- [x] **Tag sessions `source: "hermes-chat"`** — `session.create` currently defaults to `"tui"`, so our chats are indistinguishable from the real TUI. Required before import/resume and any cross-surface continuity.

- [x] **Audit & close translation gaps** — Walked catalog vs `_translate_event()`. Bridged `status.update`, `tool.generating`, `tool.progress` (UI), `notification.show`/`clear`. Unknown events log at debug; desktop/niche events listed as unsupported in `docs/hermes-gateway-protocol.md`.

- [x] **Prefer Hermes session APIs over local reinvention** — `ensure_session` / submit / image-attach now **`session.resume` before creating** a new empty session. Opening a chat resumes the bound Hermes session; empty local caches seed from Hermes transcript; renames call `session.title`. Usage already uses `session.usage`. Local SQLite remains a display/edit cache with stable ids.

- [x] **Pass-through unknown events safely** — Unrecognized gateway events are logged at debug (not silently forgotten). Full JSON passthrough to the UI deferred until a consumer needs it.

---

## 🔴 Native-parity features (gateway already has these)

Ordered by “feels like CLI/desktop” impact:

1. [x] **Mid-turn redirect (`session.steer`)** — While a turn is running, composer Send / Enter injects a correction via `session.steer` (toast ack + busy hint). Stop remains available beside it.
2. [x] **Slash / power commands** — Composer `/` autocomplete from `commands.catalog`; Send routes through `slash.exec` → `command.dispatch` (`POST /v1/chat/command`). Output / skill / send / prefill handled; `/new` + `/clear` are local.
3. [x] **Attachments parity** — Images via `image.attach_bytes`; PDFs via `pdf.attach` (falls back to `file.attach` if pdftoppm missing); other files via `file.attach` + `@file:` inserted into the prompt.
4. [x] **Import / resume TUI (and other) sessions** — Sidebar “Import session” picker via `session.list` + `session.resume`; excludes `hermes-chat` sources; already-imported opens the existing chat.
5. [x] **Session tools** — Composer Compress / Branch / Undo wire `session.compress`, `session.branch`, and richer `session.undo` (prefill last user text; clear busy errors).
6. [x] **Live status UX** — compacting / busy from `status.update`; richer tool line from `tool.generating` (wired in event-translation pass).

---

## 🟡 Voice — replace the workaround with Hermes

Today: plugin-owned Whisper STT + Edge TTS (`hermes_chat/voice.py`, `/v1/audio/*`). Native Hermes (v2026.8.3+) has streaming TTS, barge-in, wake words, unified STT/TTS on CLI/desktop/gateways.

- [ ] **Research: how native voice attaches to `tui_gateway` / sessions** — Find the RPC, events, or shared APIs desktop/CLI use. Document in `docs/` what a browser client must call (and what stays desktop-only: wake word, full-duplex mic ownership, etc.).
- [ ] **Spike: one turn of Hermes TTS (or STT) through our chat path** — Prove audio can ride the same session as text/tools/gates without Edge/Whisper.
- [ ] **Replace plugin TTS/STT** — Swap auto-speak / Read aloud / mic input to Hermes providers; delete or gate `voice.py` once parity is good enough.
- [ ] **Stretch: conversational voice in the web UI** — Streaming clause TTS + barge-in only if the gateway exposes it for non-desktop clients; don’t fake it in the browser.

Until the research item lands, keep the current voice stack; don’t expand it.

---

## 🟡 Future-proofing

- [ ] **Track Hermes releases against the gateway catalog** — When Hermes ships features, check for new RPC/events first; only build UI after the wire exists.
- [ ] **Refresh `docs/hermes-gateway-protocol.md` after major Hermes upgrades** — Diff `tui_gateway` methods/events so the backlog stays honest.
- [ ] **`@assistant-ui/react-devtools`** — Dev-only inspector for runtime state; strip from production builds.

---

## 🟢 Light UX (keep chat-scoped)

- [ ] Confirm before delete (sidebar)
- [ ] Keyboard shortcut for New Chat (`Cmd/Ctrl+N`)
- [ ] Message timestamps on hover (user messages)

---

## Explicitly out of scope

Do not chase desktop/platform parity here: artifacts / plugin SDK / multi-window, messaging-platform voice adapters, Hermes settings Control Center, cron/skills/plugins admin, multi-profile management, workspace file browser.
