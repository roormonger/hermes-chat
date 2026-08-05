---
name: hermes-home-reference
description: >-
  Map of the local gitignored Hermes home copy at hermes install/ and the
  tui_gateway / plugin APIs hermes-chat depends on. Use when integrating with
  gateway RPC, gates, sessions, plugins, or debugging against a real install.
  Never commit hermes install/ — it is gitignored and may contain secrets.
---

# Local Hermes home reference

Path: `hermes install/` (gitignored copy of `~/.hermes` from another machine).  
**Do not commit this folder** — secrets and large runtime state. Use `-LiteralPath` / quoted paths — the directory name has a **space**.

Official programmatic docs: https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration  
Project catalog (already mined): `docs/hermes-gateway-protocol.md`  
Our wrapper: `hermes_chat/gateway_session.py`

## Top-level layout

| Path | Role |
|---|---|
| `hermes-agent/` | Full agent source (includes `tui_gateway/`, `ui-tui/`, `hermes_cli/`, docs) |
| `config.yaml` | User config (model, compression, display, plugins, approvals) |
| `state.db` | **Canonical** session/message store (SQLite) |
| `sessions/` | Mostly failed-API request dumps — **not** primary transcripts |
| `plugins/` | User plugins (`hermes-bridge`, achievements state) |
| `auth.json`, `.env` | Secrets — do not commit or copy into the UI |
| `memories/`, `skills/`, `workspace/` | Agent-side; not chat-admin product surfaces |
| `logs/`, `cache/`, `cron/`, … | Runtime infra |

## Integration choice (ours)

Three protocols exist: ACP, **TUI gateway**, OpenAI-compatible API server.  
hermes-chat uses **TUI gateway** — same surface as the Ink TUI — via **in-process** `tui_gateway.server.dispatch` + `QueueTransport` (not a CLI spawn, not hermes_client’s subprocess).

## `hermes-agent/tui_gateway/`

| File | Role |
|---|---|
| `server.py` (~550KB) | JSON-RPC methods, sessions, events, gates |
| `entry.py` | stdio main; optional WS tee |
| `ws.py` / `transport.py` | WebSocket + Transport binding |
| `event_publisher.py`, `slash_worker.py` | Sidecars |

### Chat-critical RPC

- `session.create` — pass `source` (prefer `"hermes-chat"` so sessions aren’t anonymous `"tui"`)
- `session.resume` — reopen stored sessions after restart / on chat load (prefer over inventing a new id)
- `session.info` / `session.list` / `session.history` / `session.title` / `session.usage`
  (web import: `GET /v1/hermes-sessions`, `POST /v1/hermes-sessions/import`)
- `prompt.submit` — `{session_id, text}`
- `session.interrupt`, `session.steer` (mid-turn correction), optional `session.undo` / `session.redirect` / `compress` / `branch`
- Slash: `commands.catalog`, `slash.exec`, `command.dispatch` (web: `GET /v1/chat/commands`, `POST /v1/chat/command`)
- Attachments: `image.attach` / `image.attach_bytes` / `pdf.attach` / `file.attach`
  (web: `POST /v1/image/attach`, `/v1/pdf/attach`, `/v1/file/attach` — file refs go into the prompt)
- Gates: `approval.respond`, `clarify.respond`, `sudo.respond`, `secret.respond`
- Voice: `voice.toggle` / `voice.record` / `voice.tts` + `voice.status` / `voice.transcript`
  are **host mic/speakers** — not for remote browsers. Web path = Desktop-style
  `/api/audio/*` → `tools.tts_tool` / `transcription_tools` (see `docs/hermes-voice.md`).
- Skip for web: `terminal.read.respond` (desktop PTY); host `voice.record` / `voice.tts` playback

### Events we care about

`message.delta` / `complete`, `tool.start` / `generating` / `complete`, `reasoning.delta` / `thinking.delta` / `reasoning.available`, `approval.request`, clarify/sudo/secret request types, `status.update`, `session.info`, `error`, `turn.complete`.

Translation lives in `GatewaySession._translate_event()`. Known gaps vs TUI: `reasoning.delta`, richer `tool.generating`, some status/compacting events.

### Gates

- clarify / sudo / secret → `_block` wait → respond RPC
- approvals → `approval.request` → `approval.respond` (separate path)
- **Always surface gates to the user** (unlike hermes_client auto-approve)

## Built-in TUI (`hermes-agent/ui-tui/`)

Ink React client. Same RPC/event model over stdio/WS child process. Useful mirrors:

- `src/gatewayClient.ts`, `src/app/createGatewayEventHandler.ts`
- `useSessionLifecycle.ts`, `submissionCore.ts`, gate handling in `useMainApp.ts`

Pattern to copy: event switch + gate UI. Transport differs (they’re out-of-process).

## Plugin system

Discovery (`hermes_cli/plugins.py`): bundled `hermes-agent/plugins/` → user `plugins/` → optional project plugins → pip entry points.  
Needs `plugin.yaml` + `register(ctx)` (often via `plugin.py`).

Dashboard tabs: `plugins/*/dashboard/manifest.json` → routes under `/api/plugins/<name>/`. Our `dashboard/` follows this.

This install’s `plugins/hermes-bridge/` is an older/sibling packaging of a chat bridge (PTY-era). Prefer our current in-process gateway path; use bridge only for packaging/daemon lifecycle reference.

## Sessions / resume

- Ground truth: `state.db` (`sessions` + `messages` tables), `source` field (`tui`, `api_server`, …)
- Resume API: `session.resume` (follows compression tip) — prefer over raw SQL
- Chat UI should map `chat_id` ↔ `hermes_session_id` (our SQLite) and resume via gateway

## Config a chat UI may read (not manage)

Prefer gateway `model.options` / `config.get` / `session.info` events over editing YAML in-product:

- `model.*`, `agent.max_turns`, `agent.reasoning_effort`
- `compression.*`, `display.show_reasoning` / streaming / tool_progress
- `approvals.mode`, `session_reset`, `plugins.enabled`

Do **not** build a settings Control Center around `config.yaml`.

## When working in this folder

1. Quote paths / use `-LiteralPath` (`hermes install/...`).
2. Prefer `docs/hermes-gateway-protocol.md` before re-grepping `server.py`.
3. Don’t commit secrets from `.env` / `auth.json`.
4. Chat features only — leave workspace/skills/cron admin to Hermes itself.
