---
name: hermes-gateway-protocol
description: "Reference for the Hermes tui_gateway JSON-RPC protocol as consumed by hermes-layer/hermes_chat. Use when adding new gateway features (reasoning display, session usage, file/PDF attachments, TUI session import, multi-agent/subagent windows) or debugging Hermes Chat<->gateway RPC issues."
version: 1.0.0
---

# Hermes TUI Gateway Protocol — Reference for Hermes Chat

This document catalogs the `tui_gateway/server.py` JSON-RPC surface (from the
installed Hermes agent at `hermes install/hermes-agent/tui_gateway/`) that is
relevant to building out `hermes-layer`'s web chat UI. It exists so future
work doesn't have to re-derive this by reading a 14k-line file again.

Official docs: https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration

## Three integration protocols (context)

| Protocol | Transport | Defined by |
|---|---|---|
| ACP | JSON-RPC over stdio | `acp_adapter/` |
| **TUI gateway** (what we use) | JSON-RPC over stdio or WebSocket | `tui_gateway/server.py` |
| API server | HTTP + SSE, OpenAI-compatible | `gateway/platforms/api_server.py` |

We use the TUI gateway because it's the only protocol that exposes slash
commands, approval/clarify/sudo/secret gates, session branching, and
multi-agent — i.e. "every Hermes feature". Our own `hermes_chat/gateway_session.py`
(`GatewaySession` class) wraps this: one Python subprocess-free RPC channel
per chat, JSON-RPC requests via `_call()`, events drained onto an
`asyncio.Queue` and translated to our SSE shape in `_translate_event()`.

## RPC methods currently used by Hermes Chat

- `session.create` — first message in a chat; returns `session_id` (with `source: "hermes-chat"`).
- `session.resume` — reopen a stored Hermes session after restart / on chat load.
- `session.history` / `session.title` / `session.usage` / `session.context_breakdown`
- `session.info` — cheap liveness probe (used to detect stale DB session ids after a Hermes Chat restart — see `ensure_session()`).
- `prompt.submit` — send a user turn.
- `image.attach_bytes` — attach base64 image before submitting a prompt.
- `pdf.attach` / `file.attach` — PDF vision tiles + `@file:` staging (`POST /v1/pdf/attach`, `POST /v1/file/attach`).
- `session.interrupt` — stop an in-flight turn from the stop button.
- `session.steer` — mid-turn correction without interrupting (`POST /v1/chat/steer`).
- `session.undo` — pop last turn; returns composer `prefill` (`POST /v1/chat/undo`).
- `session.compress` — manual context compact (`POST /v1/chat/compress`).
- `session.branch` — fork into a new chat (`POST /v1/chat/branch`).
- `commands.catalog` / `slash.exec` / `command.dispatch` — slash commands (`GET /v1/chat/commands`, `POST /v1/chat/command`).
- `approval.respond` / `clarify.respond` / `sudo.respond` / `secret.respond` — resolve gate interrupts.

## Full method catalog (selected, from official docs + server.py grep)

```
session.create        session.list           session.most_recent
session.resume         session.active_list     session.activate
session.close          session.interrupt       session.history
session.compress       session.branch          session.title
session.usage          session.context_breakdown  session.status
session.undo           session.steer           session.cwd.set
session.save

prompt.submit          prompt.background       clarify.respond
sudo.respond            secret.respond          approval.respond

image.attach            image.attach_bytes      image.detach
pdf.attach              file.attach

config.set / config.get   model.options          model.save_key
model.disconnect          commands.catalog       command.resolve
command.dispatch         cli.exec

delegation.status       subagent.interrupt      spawn_tree.save/list/load
terminal.resize          clipboard.paste         input.detect_drop

reload.mcp              reload.env              process.stop / process.list
```

**Important distinction** (from official docs): `session.active_list`,
`session.activate`, `session.close` are *process-local live-session*
controls (sessions currently open in this gateway process). `session.list`
+ `session.resume` are the *saved transcript* browser/loader — these work
across gateway restarts and are what a "resume picker" or session importer
needs.

## Event catalog (streamed back via `{"method": "event", "params": {"type": ..., "session_id": ..., "payload": {...}}}`)

Events we already translate in `gateway_session.py::_translate_event()`:

- `message.delta` → `{"type": "text", "text": ...}`
- `message.complete` / `turn.complete` → `{"type": "turn_complete"}`
- `tool.start` / `tool.generating` / `tool.progress` / `tool.complete` → `tool_*`
- `reasoning.delta` / `thinking.delta` / `reasoning.available` → `reasoning`
- `approval.request` / `clarify.request` / `sudo.request` / `secret.request` → `gate_interrupt`
- `status.update` → `status_update` (compacting / busy line in the UI)
- `notification.show` / `notification.clear` → toast stack in the web UI
- `session.title` / `session.info` → sidebar title + header usage/model
- `error` → `error`

### Intentionally unsupported (desktop / niche)

| Event | Why skipped |
|---|---|
| `terminal.read.request` | Desktop PTY buffer gate — not a web chat concern |
| `agent.terminal.output` / `agent.terminal.close` | Subagent PTY passthrough |
| `moa.reference` / `moa.aggregating` | Mixture-of-Agents UI not exposed |
| `review.summary` | Background code-review surface |

Unknown event types are logged at debug (`unhandled gateway event type=…`) and dropped so Hermes upgrades stay discoverable without breaking the stream.

Adding a new bridged event: add a branch to `_translate_event()`, extend `SseEvent` in `webui/src/api.ts`, handle it in `handleStreamEvent` in `webui/src/App.tsx`.

## Gates we handle vs. don't

Handled (`respond_gate()` in `gateway_session.py`): `approval`, `clarify`,
`sudo`, `secret`.

Not handled: `terminal.read.request` (desktop-GUI PTY read gate — skip,
not relevant to a web chat client).

## Attachment methods

- **`image.attach_bytes`** — `POST /v1/image/attach` — base64 image → vision tile queue.
- **`pdf.attach`** — `POST /v1/pdf/attach` — PDF → `pdftoppm` page PNGs queued as vision tiles.
  If `pdftoppm` is missing, hermes-chat falls back to `file.attach` and returns `ref_text`.
- **`file.attach`** — `POST /v1/file/attach` — `data_url` upload staged under
  `.hermes/desktop-attachments/`; returns `ref_text` (`@file:…`) that the UI
  **inserts into the prompt** before `prompt.submit`.

## TUI session import — the key finding

This is directly useful for the "import TUI chat sessions" goal.

**`session.list`** — params: `{limit?}` (default 200). Returns saved
transcripts across every surface (CLI, TUI, ACP, gateway platforms), not just
currently-open ones:

```json
{"sessions": [
  {"id": "...", "title": "...", "preview": "...",
   "started_at": 172..., "message_count": 28, "source": "tui"}
]}
```

Internally filters out `source == "tool"` (sub-agent runs) but keeps
everything else — `tui`, `api_server`, and any custom source. Our local
`state.db` inspection (see below) confirms `source` values seen in the wild
are `"tui"` and `"api_server"`.

**`session.resume`** — params: `{session_id, cols?, profile?, lazy?}`. This
is the one-call answer to "load a saved TUI session for use in the web UI":
it loads the session (following the compression-continuation chain to the
live tip if the session was auto-compressed), makes it live in the gateway
process, and returns:

```json
{
  "info": {...},
  "message_count": N,
  "messages": [{"role": "user"|"assistant"|"tool", "text": "...", ...}],
  "running": false,
  "session_id": "...",
  "session_key": "...",
  "started_at": ...,
  "status": "idle"
}
```

`messages` is already normalized by `_history_to_messages()` — tool-call
pairs are collapsed to `{"role": "tool", "name": ..., "context": ...}`
(no raw OpenAI `tool_calls` schema to parse), assistant reasoning is
preserved under `reasoning`/`reasoning_content` keys when present. This is
directly usable to hydrate our `ChatMessage[]` state and backend `messages`
table.

**`session.history`** — same normalized shape, but for a session that's
*already* referenced by `session_id` (must already be resolvable via
`_sess_nowait`, i.e. either live or has a `session_key` in the DB). Less
useful for cold import than `session.resume`, which works even if the
session was never opened in this gateway process.

### Session `source` tagging

**Done:** `GatewaySession.ensure_session()` creates sessions with
`{"source": "hermes-chat"}` (`HERMES_CHAT_SOURCE` in `gateway_session.py`).
Without that, the gateway defaults to `"tui"` and our chats are
indistinguishable from the real TUI in `session.list` / `state.db`.

Import pickers should filter to `source == "tui"` (and other native surfaces)
and exclude `"hermes-chat"` so we never re-import a session that already
backs a hermes-chat conversation.

### Hermes as session SoT (done)

- **`session.resume` first** — `ensure_session`, `prompt.submit` (4001 retry),
  and `image.attach_bytes` resume from Hermes `state.db` before inventing a
  new empty session after a gateway/process restart.
- **Open chat** — `GET /api/chats/{id}/messages` resumes the mapped Hermes
  session, updates the stored tip id if compression forked it, syncs title
  when our cache still says `"New chat"`, and seeds the local message cache
  from Hermes when that cache is empty.
- **Rename** — `PATCH /api/chats/{id}` also calls `session.title` when bound.
- **Usage** — already via `session.usage` / `session.context_breakdown`.
- Local SQLite stays a **cache** (stable numeric message ids for edit/reload);
  Hermes remains authoritative for the agent turn.

### Recommended import flow

**Done** in hermes-chat:

1. `GET /v1/hermes-sessions` → `session.list`, excludes `source == "hermes-chat"`,
   marks already-bound ids as `imported` + `chat_id`.
2. `POST /v1/hermes-sessions/import` → `session.resume`, creates a chat, seeds
   local cache, binds tip `hermes_session_id`. Re-import of an already-bound
   session opens the existing chat.
3. Sidebar **Import session** picker.

## Model picker — feasible and well-supported

A model picker is very reasonable. The TUI gateway already has first-class
model switching; the web UI just needs to wrap three JSON-RPC methods.

### Required gateway methods

- **`model.options`** — returns the full picker payload: providers, their
  `authenticated`/`auth_type`/`key_env` status, available models, pricing, and
  capabilities. Call with no `session_id` to get the disk-configured catalog,
  or pass the current chat's `session_id` to layer the live session's model on
  top. The payload shape is the same one used by the TUI and dashboard
  `/api/model/options`.
- **`config.get`** with `key: "provider"` — returns the current
  `{model, provider, providers}` cheaply (reads `config.yaml` / env vars; no
  live session required for the model part).
- **`config.set`** with `key: "model"` — performs the switch. The `value` can be
  just `"deepseek/deepseek-v4"` or `"deepseek/deepseek-v4 --provider openrouter"`.
  It calls `agent.switch_model()` in place, emits a fresh `session.info` event,
  and sets a per-session `model_override` so the choice survives resume/`/new`.

### Suggested implementation path in `hermes-layer`

1. **Backend endpoints** in `hermes_chat/main.py`:
   - `GET /v1/models` → call `GatewaySession._call("model.options", {})` (or use
     the existing `/api/ws` passthrough). Use `explicit_only=True` if you only
     want providers the user has already configured, or
     `include_unauthenticated=False` for a cleaner list.
   - `GET /v1/model` → call `config.get {"key": "provider"}` for the current
     model header.
   - `POST /v1/model` → accept `{model, provider?}` and call
     `config.set {"key": "model", "value": "<model> --provider <provider>", "session_id": ...}`
     on the active chat's `GatewaySession`.

2. **Frontend** in `webui/src/App.tsx` / `thread.tsx`:
   - Show the current model from the `session.info` event (already emitted by
     the gateway) or from `GET /v1/model`.
   - Render a dropdown / combobox from `GET /v1/models`, grouped by provider.
   - On selection, call `POST /v1/model` and wait for the updated `session.info`
     event to confirm.

### Caveats

- **Blocked during in-flight turns**: `config.set` returns error `4009`
  (`"session busy — /interrupt the current turn before switching models"`) if
  the agent is running. Disable the picker while `isRunning`, or offer to cancel
  first.
- **Expensive model confirmation**: `config.set` may return
  `confirm_required: true` with a `confirm_message` (e.g. for high-cost models).
  The UI should show a confirmation modal and re-call with
  `confirm_expensive_model: true` only after the user agrees.
- **Persistence semantics**: an in-session switch sets a per-session
  `model_override` (so it survives resume and `/new` in that chat) and does
  **not** write global `config.yaml` by default. The TUI-style flags
  `--global` / `--session` can change this; the exact default is computed by
  `hermes_cli.model_switch.resolve_persist_behavior`.
- **No session yet**: if the picker is opened before the first message, the
  Hermes Chat `GatewaySession` may not have a Hermes `session_id`. Either call
  `GatewaySession.ensure_session()` first, or call `model.options` with no
  `session_id` and only apply the switch once the session exists.

This is low-hanging fruit for TUI feature parity: the TUI already has the same
picker, and the gateway exposes everything needed.

## `state.db` schema (ground truth for message/session persistence)

Confirmed by direct inspection of the installed `hermes install/state.db`
(SQLite). Relevant tables:

**`sessions`**: `id, source, user_id, session_key, chat_id, chat_type,
thread_id, model, model_config, system_prompt, parent_session_id,
started_at, ended_at, end_reason, message_count, tool_call_count,
input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
reasoning_tokens, cwd, git_branch, git_repo_root, title, archived,
display_name, ...` (plus billing/cost columns, handoff state, rewind count).

**`messages`**: `id, session_id, role, content, tool_call_id, tool_calls,
tool_name, timestamp, token_count, finish_reason, reasoning,
reasoning_content, reasoning_details, codex_reasoning_items,
codex_message_items, platform_message_id, observed, active, compacted`.

- `role` ∈ `{user, assistant, tool, system}`.
- `tool_calls` (assistant rows only) is a JSON array, OpenAI function-call
  shape: `[{"id", "call_id", "type": "function", "function": {"name",
  "arguments"}}]`.
- `tool` rows have `tool_call_id` linking back to the originating
  `tool_calls[].id`, and `content` holding the JSON-stringified tool result.
- `active`/`compacted` flags: rows can be soft-retired by compression without
  deletion — filter `active=1` (or trust `session.resume`/`session.history`
  which already do this via the agent's live `history`, not a raw table
  scan).
- There's also `messages_fts` / `messages_fts_trigram` (SQLite FTS5) for
  full-text + fuzzy search across all messages — could power a global search
  feature across imported + native chats later.

We do **not** need to touch `state.db` directly for the import feature —
`session.resume` already does the equivalent read + compression-chain
resolution correctly. This section is here so a future "raw export" or
"cross-session search" feature doesn't have to redo this discovery.

## `hermes_client` (lotsoftick/hermes_client) — architectural comparison

https://github.com/lotsoftick/hermes_client — a different, CLI-subprocess
based approach worth knowing about even though we deliberately don't follow
it:

- **No gateway at all.** Every turn spawns `hermes -p <profile> chat -Q -q
  "<message>"` as a fresh subprocess and streams its stdout over SSE. No
  persistent process, no JSON-RPC.
- **Multi-agent = Hermes profiles.** Each UI "agent" is a separate Hermes
  `profile` (own home dir/config/sessions), managed via `hermes profile ...`
  CLI commands.
- **Cross-app session sync** — because it shells out to the same `hermes`
  CLI the terminal uses, a session started in a plain terminal REPL shows up
  in their sidebar automatically (same `state.db`), and can be continued from
  either side via `hermes -p <profile> chat -r <sessionKey>`.
- **File uploads** — saved to `~/.hermes_client/uploads/<conversationId>/`,
  then passed to Hermes via `--image <path>` (images) or referenced inline in
  the prompt text (everything else) — i.e. no `file.attach`/`data_url` RPC,
  just CLI flags/paths.
- **Auth**: JWT, default admin/admin bootstrap — same shape as our own
  Hermes Chat auth, nothing new there.

**Why our gateway-RPC approach is strictly better for feature parity with
the TUI**: no per-turn subprocess spawn latency, native streaming events
(tool steps, reasoning, gates) instead of parsing CLI stdout formatting,
and access to gates (approval/clarify/sudo/secret) which the `-Q -q` quiet
CLI mode can't surface interactively at all — hermes_client's CLI mode would
just hang or fail on any turn that needs a gate. The one thing they get "for
free" that we don't — automatic visibility of sessions from *any* Hermes
surface without an explicit import step — is exactly what our `session.list`
+ `session.resume` import flow above would replicate deliberately instead.

## Slash / power commands

Wired in hermes-chat:

- **`commands.catalog`** → `GET /v1/chat/commands` — autocomplete pairs / categories / canon.
- **`slash.exec`** then **`command.dispatch`** → `POST /v1/chat/command` — same fallthrough as the TUI.
- Normalized actions: `output` (inline), `send` / `skill` (prompt.submit), `prefill` (composer draft).
- Long-handler RPC wait: `QueueTransport` intercepts JSON-RPC responses for pool methods (`slash.exec`, `session.resume`, …) so `_call` always returns a real result.

Local-only: `/new`, `/clear` start a new chat in the web UI.

## Voice (see `docs/hermes-voice.md`)

Gateway RPCs exist but are **host-audio** (not for remote browsers):

- `voice.toggle` / `voice.record` / `voice.tts`
- Events: `voice.status`, `voice.transcript` → TUI auto-`prompt.submit`

Browser-correct path (Desktop): Hermes `hermes_cli/web_server.py`
`POST /api/audio/transcribe` + `POST /api/audio/speak` →
`tools.transcription_tools` / `tools.tts_tool` → bytes/`data_url`.
hermes-chat `/v1/audio/*` prefers Hermes `tools.tts_tool` /
`tools.transcription_tools` (same as Desktop); plugin Edge/Whisper is fallback.
`GET .../voice-config` reports `tts_backend` / `stt_backend`.

## Open items / not yet investigated

- Session tools: ~~`session.branch` / `session.compress` / richer undo~~ done
  (`POST /v1/chat/compress`, `POST /v1/chat/branch`; undo returns `prefill`).
- TUI session import: ~~`session.list` + `session.resume` picker~~ done
  (`GET /v1/hermes-sessions`, `POST /v1/hermes-sessions/import`).
- Voice research: ~~done~~ → spike Hermes TTS/STT on `/v1/audio/*`
  (`docs/hermes-voice.md`) ~~done~~ (Hermes-first + plugin fallback).
