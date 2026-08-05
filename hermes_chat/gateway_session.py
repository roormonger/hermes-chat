"""
In-process TUI gateway session manager.

Instead of spawning `hermes chat -r <id>` in a PTY and scraping ANSI output,
we import `tui_gateway.server` (which is part of the Hermes Python package)
and drive it via its JSON-RPC dispatch function directly in the same process.

Architecture
------------
  REST /v1/chat
       │
       ▼
  GatewaySession.submit(text)          ← JSON-RPC: prompt.submit / session.create
       │
       ▼
  tui_gateway.server.dispatch(req)     ← in-process call, no subprocess/network
       │
       ▼
  events pushed via Transport.write()  ← we inject a QueueTransport
       │
       ▼
  asyncio.Queue  →  SSE stream         ← same shape the UI already consumes

Gate / approval flow
--------------------
  tui_gateway emits  approval.request / clarify.request / sudo.request
  → translated to    {"type": "gate_interrupt", ...}  SSE event
  → resolved via     approval.respond / clarify.respond / sudo.respond  RPC call

Fallback
--------
If `tui_gateway` is not importable (Hermes not installed, wrong Python env,
etc.) we raise ImportError at import time so `main.py` can fall back to the
legacy PTY path and emit a clear log message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger("hermes_chat.gateway")

# Distinct from the built-in TUI default ("tui") so session.list / import can
# separate hermes-chat conversations from CLI/desktop sessions.
HERMES_CHAT_SOURCE = "hermes-chat"

# ---------------------------------------------------------------------------
# Import guard — fail loudly if tui_gateway is not available so the caller
# can decide whether to fall back to the PTY path.
# ---------------------------------------------------------------------------
try:
    from tui_gateway import server as _gw_server
    from tui_gateway.transport import bind_transport, Transport
    _GATEWAY_AVAILABLE = True
    _import_err_msg = ""
    logger.info("tui_gateway imported successfully")
except Exception as _import_err:
    _GATEWAY_AVAILABLE = False
    _import_err_msg = str(_import_err)
    logger.warning(
        "tui_gateway not available (%s: %s) — falling back to PTY backend",
        type(_import_err).__name__,
        _import_err_msg,
    )


def gateway_available() -> bool:
    return _GATEWAY_AVAILABLE


def gateway_available_error() -> str:
    return _import_err_msg


# ---------------------------------------------------------------------------
# QueueTransport — injected into tui_gateway so its events reach our queue
# ---------------------------------------------------------------------------

class QueueTransport:
    """A tui_gateway Transport that pushes JSON-RPC frames onto an asyncio Queue.

    Long-running RPCs (``slash.exec``, ``session.resume``, …) return ``None``
    from ``dispatch()`` and write the JSON-RPC response asynchronously via
    ``write()``. Callers that need that result register a waiter with
    ``begin_wait`` / ``wait_response`` so the matching ``id`` is intercepted
    instead of being dropped by the SSE event translator.
    """

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        self._queue = queue
        self._loop = loop
        self._waiters: dict[Any, threading.Event] = {}
        self._responses: dict[Any, dict] = {}
        self._wait_lock = threading.Lock()

    def begin_wait(self, rid: Any) -> None:
        with self._wait_lock:
            self._waiters[rid] = threading.Event()

    def cancel_wait(self, rid: Any) -> None:
        with self._wait_lock:
            self._waiters.pop(rid, None)
            self._responses.pop(rid, None)

    def wait_response(self, rid: Any, timeout: float = 300.0) -> dict:
        with self._wait_lock:
            event = self._waiters.get(rid)
        if event is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "no waiter"}}
        if not event.wait(timeout):
            self.cancel_wait(rid)
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32000, "message": f"RPC timeout after {timeout:.0f}s"},
            }
        with self._wait_lock:
            resp = self._responses.pop(rid, {})
            self._waiters.pop(rid, None)
        return resp

    # tui_gateway.transport.Transport interface
    def write(self, obj: dict) -> bool:
        rid = obj.get("id") if isinstance(obj, dict) else None
        # JSON-RPC responses have an id and no method; events use method=event.
        if rid is not None and isinstance(obj, dict) and "method" not in obj:
            with self._wait_lock:
                event = self._waiters.get(rid)
                if event is not None:
                    self._responses[rid] = obj
                    event.set()
                    return True
        self._loop.call_soon_threadsafe(self._queue.put_nowait, obj)
        return True

    def is_alive(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Event translation: tui_gateway JSON-RPC → our SSE event shape
# ---------------------------------------------------------------------------

# Desktop / niche surfaces we intentionally do not bridge (see docs).
_UNSUPPORTED_GATEWAY_EVENTS = frozenset({
    "terminal.read.request",
    "agent.terminal.output",
    "agent.terminal.close",
    "moa.reference",
    "moa.aggregating",
    "review.summary",
})


def _translate_event(frame: dict) -> Optional[dict]:
    """
    Convert a tui_gateway JSON-RPC push frame into our SSE event dict.

    tui_gateway pushes events as:
        {"jsonrpc": "2.0", "method": "event", "params": {"type": "...", ...}}

    Returns None for frames we don't need to forward (e.g. RPC ack frames).
    """
    if frame.get("method") != "event":
        return None

    params = frame.get("params", {})
    etype = params.get("type", "")
    nested_payload = params.get("payload")
    payload = nested_payload if isinstance(nested_payload, dict) else params

    if etype == "message.delta":
        text = payload.get("text") or payload.get("delta") or ""
        return {"type": "text", "text": text}

    if etype in ("message.complete", "turn.complete"):
        return {"type": "turn_complete"}

    if etype == "tool.start":
        return {
            "type": "tool_start",
            "tool_id": payload.get("tool_id", ""),
            "name": payload.get("name", ""),
            "context": payload.get("context", ""),
        }

    if etype == "tool.generating":
        return {
            "type": "tool_generating",
            "tool_id": payload.get("tool_id", ""),
            "name": payload.get("name", ""),
        }

    if etype == "tool.progress":
        return {
            "type": "tool_progress",
            "tool_id": payload.get("tool_id", ""),
            "name": payload.get("name", ""),
            "text": payload.get("text", ""),
        }

    if etype == "tool.complete":
        return {
            "type": "tool_complete",
            "tool_id": payload.get("tool_id", ""),
            "name": payload.get("name", ""),
            "summary": payload.get("summary", ""),
            "duration_s": payload.get("duration_s"),
            "result": payload.get("result"),
            "artifact": payload.get("artifact"),
        }

    if etype in ("reasoning.delta", "thinking.delta"):
        text = payload.get("text") or payload.get("delta") or ""
        if not text:
            return None
        return {"type": "reasoning", "text": text}

    if etype == "reasoning.available":
        text = payload.get("text") or ""
        if not text:
            return None
        return {"type": "reasoning", "text": text, "replace": True}

    if etype == "approval.request":
        return {
            "type": "gate_interrupt",
            "gate_kind": "approval",
            "gate_id": payload.get("request_id", ""),
            "prompt": payload.get("prompt", ""),
            "options": payload.get("choices") or ["approve", "deny"],
            "context": payload.get("context", {}),
        }

    if etype == "clarify.request":
        logger.debug("clarify.request payload keys=%s payload=%r", list(payload.keys()), payload)
        choices = payload.get("choices") or payload.get("options") or []
        return {
            "type": "gate_interrupt",
            "gate_kind": "clarify",
            "gate_id": payload.get("request_id", ""),
            "prompt": payload.get("question", ""),
            "options": choices,
        }

    if etype == "sudo.request":
        return {
            "type": "gate_interrupt",
            "gate_kind": "sudo",
            "gate_id": payload.get("request_id", ""),
            "prompt": payload.get("prompt", "Password required"),
            "options": [],
        }

    if etype == "secret.request":
        return {
            "type": "gate_interrupt",
            "gate_kind": "secret",
            "gate_id": payload.get("request_id", ""),
            "prompt": payload.get("prompt", "Secret required"),
            "options": [],
        }

    if etype == "error":
        return {
            "type": "error",
            "message": payload.get("message", "Unknown error"),
        }

    if etype == "session.title":
        return {
            "type": "session_title",
            "title": payload.get("title", ""),
            "session_id": payload.get("session_id", ""),
        }

    if etype == "session.info":
        model = payload.get("model", "")
        provider = payload.get("provider", "")
        # Hermes sometimes reports the model as "<model> --provider <gateway>".
        if isinstance(model, str) and " --provider " in model:
            model, provider = model.split(" --provider ", 1)
            model = model.strip()
            provider = provider.strip()
        return {
            "type": "session_info",
            "model": model,
            "provider": provider,
            "gateway": payload.get("gateway", ""),
            "api_provider": payload.get("api_provider", ""),
            "reasoning_effort": payload.get("reasoning_effort", ""),
            "service_tier": payload.get("service_tier", ""),
            "fast": payload.get("fast", False),
            "yolo": payload.get("yolo", False),
            "context_window": payload.get("context_window") or payload.get("context_window_tokens") or 0,
            "input_tokens": payload.get("input_tokens") or payload.get("prompt_tokens") or 0,
            "output_tokens": payload.get("output_tokens") or payload.get("completion_tokens") or 0,
            "cache_read_tokens": payload.get("cache_read_tokens") or payload.get("cached_tokens") or 0,
            "reasoning_tokens": payload.get("reasoning_tokens") or 0,
            "total_tokens": payload.get("total_tokens") or 0,
        }

    if etype == "status.update":
        kind = payload.get("kind") or payload.get("status") or ""
        text = payload.get("text") or payload.get("message") or ""
        return {
            "type": "status_update",
            "kind": kind,
            "text": text,
        }

    if etype == "notification.show":
        return {
            "type": "notification",
            "id": payload.get("id") or payload.get("key") or "",
            "key": payload.get("key") or "",
            "text": payload.get("text") or payload.get("message") or "",
            "level": payload.get("level") or "info",
        }

    if etype == "notification.clear":
        return {
            "type": "notification_clear",
            "id": payload.get("id") or "",
            "key": payload.get("key") or "",
        }

    # Desktop / niche surfaces we intentionally do not bridge (see docs).
    if etype in _UNSUPPORTED_GATEWAY_EVENTS:
        return None

    # Keep upgrades discoverable: log at debug without dropping the frame silently.
    logger.debug("unhandled gateway event type=%s keys=%s", etype, list(payload.keys()) if isinstance(payload, dict) else type(payload))
    return None


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _rpc(method: str, params: dict, rid: Any = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid if rid is not None else uuid.uuid4().hex[:8],
        "method": method,
        "params": params,
    }


def _dispatch_sync(req: dict, transport: "QueueTransport", *, timeout: float = 300.0) -> dict:
    """Call tui_gateway.server.dispatch and return the JSON-RPC response.

    Fast-path methods return inline. Long handlers (``_LONG_HANDLERS``) schedule
    work on Hermes' pool and write the response through ``transport`` — we wait
    for the matching ``id`` so callers always get a real result.
    """
    from tui_gateway.transport import bind_transport, reset_transport
    rid = req.get("id")
    if rid is not None:
        transport.begin_wait(rid)
    token = bind_transport(transport)
    try:
        result = _gw_server.dispatch(req, transport)
        if result is not None:
            if rid is not None:
                transport.cancel_wait(rid)
            return result
        if rid is None:
            return {}
        return transport.wait_response(rid, timeout=timeout)
    except Exception:
        if rid is not None:
            transport.cancel_wait(rid)
        raise
    finally:
        reset_transport(token)


# ---------------------------------------------------------------------------
class _TimedCache:
    """Simple in-memory TTL cache keyed by string."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        ts, value = self._data.get(key, (0, None))
        if value is not None and time.monotonic() - ts < self.ttl:
            return value
        return None

    def set(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)


# GatewaySession — one per active chat
# ---------------------------------------------------------------------------

class GatewaySession:
    """
    Tracks the TUI gateway session for a single chat.

    Lifecycle
    ---------
    1. First message: `ensure_session()` calls session.create → gets a Hermes
       session_id back.  Subsequent messages: session already exists.
    2. `submit(text)` calls prompt.submit with the session_id.
    3. Events arrive on `self.queue` as translated SSE dicts.
    4. `respond_gate(kind, request_id, value)` resolves approval/clarify/sudo.
    5. `close()` calls session.close on teardown.
    """

    def __init__(
        self,
        chat_id: str,
        hermes_session_id: Optional[str],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.chat_id = chat_id
        self.hermes_session_id = hermes_session_id  # None → will be created on first submit
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()
        self._transport = QueueTransport(self.queue, loop)
        self._lock = threading.Lock()
        self._pending_gate: Optional[dict] = None  # last gate_interrupt event
        self.last_active = time.monotonic()
        self._model_options_cache = _TimedCache(30.0)
        self._current_model_cache = _TimedCache(5.0)
        # True once we've confirmed this session_id is live in the gateway.
        # Starts False for sessions loaded from DB (may be stale); True for
        # sessions we create ourselves in this process run.
        self._session_verified: bool = hermes_session_id is None

    # ------------------------------------------------------------------
    def _call(
        self,
        method: str,
        params: dict,
        *,
        expected_error_codes: frozenset[int] | None = None,
        timeout: float = 300.0,
    ) -> dict:
        """Dispatch a JSON-RPC call in the Hermes thread pool and return the result.

        ``expected_error_codes`` are logged at debug (e.g. stale session 4001 on
        usage polls). All other RPC errors stay at warning so real bugs remain visible.
        """
        req = _rpc(method, params)
        result = _dispatch_sync(req, self._transport, timeout=timeout)
        if isinstance(result, dict) and "error" in result:
            code = result["error"].get("code", 0)
            msg = result["error"].get("message", "unknown")
            if expected_error_codes and code in expected_error_codes:
                logger.debug("gateway RPC %s expected error %s: %s", method, code, msg)
            else:
                logger.warning("gateway RPC %s error %s: %s", method, code, msg)
        return result

    def _mark_session_missing(self, sid: str) -> None:
        """Drop a Hermes session id that the gateway no longer has (stale/reaped)."""
        with self._lock:
            if self.hermes_session_id == sid:
                logger.debug(
                    "chat_id=%s hermes session %s not found in gateway; "
                    "clearing until next ensure_session",
                    self.chat_id,
                    sid,
                )
                self.hermes_session_id = None
                # None means "known empty" — create on next ensure_session.
                self._session_verified = True

    def _apply_resumed_session(self, requested_sid: str, data: dict) -> str:
        """Update local binding after a successful session.resume (tip may differ)."""
        tip = (
            data.get("session_id")
            or data.get("resumed")
            or requested_sid
        )
        tip = str(tip)
        with self._lock:
            self.hermes_session_id = tip
            self._session_verified = True
        if tip != requested_sid:
            logger.info(
                "chat_id=%s resume tip session_id=%s (requested %s)",
                self.chat_id, tip, requested_sid,
            )
        return tip

    def session_resume(self, session_id: Optional[str] = None) -> dict:
        """Load a saved Hermes session into this gateway process (Hermes SoT).

        Prefer this over session.create when we already have a hermes_session_id
        from our chat↔session map — otherwise a gateway restart would orphan the
        real transcript and invent an empty session.
        """
        self.last_active = time.monotonic()
        sid = session_id or self.hermes_session_id
        if not sid:
            return {}
        result = self._call(
            "session.resume",
            {"session_id": sid},
            expected_error_codes=frozenset({4001, 4007}),
        )
        if isinstance(result, dict) and "error" in result:
            code = result["error"].get("code")
            msg = result["error"].get("message", "session.resume failed")
            logger.warning("chat_id=%s session.resume(%s) error %s: %s", self.chat_id, sid, code, msg)
            raise RuntimeError(msg)
        data = result.get("result") or {}
        self._apply_resumed_session(sid, data)
        return data

    def session_list(self, limit: int = 200) -> list[dict]:
        """Browse saved Hermes transcripts (session.list) — no live session required."""
        self.last_active = time.monotonic()
        capped = max(1, min(int(limit or 200), 500))
        result = self._call("session.list", {"limit": capped}, timeout=60.0)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"].get("message", "session.list failed"))
        payload = result.get("result") or {}
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, list):
            return []
        return [s for s in sessions if isinstance(s, dict)]

    def session_history(self) -> list[dict]:
        """Return normalized transcript messages from the live Hermes session."""
        self.last_active = time.monotonic()
        sid = self.hermes_session_id
        if not sid:
            return []
        result = self._call(
            "session.history",
            {"session_id": sid},
            expected_error_codes=frozenset({4001}),
        )
        if isinstance(result, dict) and result.get("error", {}).get("code") == 4001:
            # Not live — resume from state.db then history is on the resume payload.
            try:
                data = self.session_resume(sid)
            except RuntimeError:
                return []
            return list(data.get("messages") or [])
        if isinstance(result, dict) and "error" in result:
            return []
        return list((result.get("result") or {}).get("messages") or [])

    def set_title(self, title: str) -> dict:
        """Set the Hermes session title (state.db), mirroring TUI /title."""
        self.last_active = time.monotonic()
        sid = self.hermes_session_id
        if not sid:
            return {}
        cleaned = (title or "").strip()
        if not cleaned:
            return {}
        result = self._call(
            "session.title",
            {"session_id": sid, "title": cleaned},
            expected_error_codes=frozenset({4001}),
        )
        if isinstance(result, dict) and "error" in result:
            if result["error"].get("code") == 4001:
                self._mark_session_missing(sid)
            raise RuntimeError(result["error"].get("message", "session.title failed"))
        return result.get("result") or {}

    # ------------------------------------------------------------------
    def ensure_session(self) -> str:
        """Return the Hermes session_id, resuming or creating via the gateway.

        Prefer ``session.resume`` for stored ids so Hermes ``state.db`` remains
        the source of truth across gateway/process restarts. Only create a new
        session when there is no id or resume cannot find one.
        """
        with self._lock:
            if self.hermes_session_id:
                if self._session_verified:
                    return self.hermes_session_id
                stale_sid = self.hermes_session_id
                # DB-loaded session — probe once; if not live, resume from state.db.
                probe = self._call(
                    "session.info",
                    {"session_id": stale_sid},
                    expected_error_codes=frozenset({4001}),
                )
                if not (isinstance(probe, dict) and probe.get("error", {}).get("code") == 4001):
                    self._session_verified = True
                    return stale_sid
                logger.info(
                    "chat_id=%s stored session_id=%s not live in gateway; resuming from Hermes",
                    self.chat_id, stale_sid,
                )
                resumed = self._call(
                    "session.resume",
                    {"session_id": stale_sid},
                    expected_error_codes=frozenset({4001, 4007}),
                )
                if isinstance(resumed, dict) and "error" not in resumed:
                    data = resumed.get("result") or {}
                    tip = data.get("session_id") or data.get("resumed") or stale_sid
                    self.hermes_session_id = str(tip)
                    self._session_verified = True
                    logger.info(
                        "chat_id=%s resumed hermes session_id=%s",
                        self.chat_id, self.hermes_session_id,
                    )
                    return self.hermes_session_id
                logger.warning(
                    "chat_id=%s resume failed for %s; creating a new Hermes session",
                    self.chat_id, stale_sid,
                )
                self.hermes_session_id = None

            result = self._call("session.create", {"source": HERMES_CHAT_SOURCE})
            sid = (result.get("result") or {}).get("session_id") or \
                  (result.get("result") or {}).get("id") or ""
            if not sid:
                logger.error("session.create returned no session_id: %r", result)
                raise RuntimeError("Failed to create Hermes session: no session_id in response")
            self.hermes_session_id = sid
            self._session_verified = True
            logger.info(
                "chat_id=%s created hermes session_id=%s source=%s",
                self.chat_id, sid, HERMES_CHAT_SOURCE,
            )
            return sid

    # ------------------------------------------------------------------
    def submit(self, text: str) -> None:
        """Send a user message to the gateway (non-blocking, events arrive on queue)."""
        self.last_active = time.monotonic()
        sid = self.ensure_session()
        result = self._call("prompt.submit", {"session_id": sid, "text": text})
        # 4001 = session not found in this process — resume from Hermes, then retry.
        if isinstance(result, dict) and result.get("error", {}).get("code") == 4001:
            logger.warning(
                "chat_id=%s session %s not found in gateway, attempting resume",
                self.chat_id, sid,
            )
            try:
                data = self.session_resume(sid)
                sid = data.get("session_id") or self.hermes_session_id or sid
                result = self._call("prompt.submit", {"session_id": sid, "text": text})
            except RuntimeError:
                with self._lock:
                    self.hermes_session_id = None
                    self._session_verified = True
                sid = self.ensure_session()
                result = self._call("prompt.submit", {"session_id": sid, "text": text})
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"].get("message", "prompt.submit failed"))

    # ------------------------------------------------------------------
    def attach_image_bytes(self, content_base64: str, filename: str = "") -> dict:
        """Attach an image to the session from base64 data (before prompt.submit)."""
        return self._attach_rpc(
            "image.attach_bytes",
            {"content_base64": content_base64, "filename": filename or ""},
        )

    def attach_pdf(self, content_base64: str, filename: str = "") -> dict:
        """Render a PDF to vision tiles via pdf.attach (needs pdftoppm)."""
        return self._attach_rpc(
            "pdf.attach",
            {
                "content_base64": content_base64,
                "filename": filename or "uploaded.pdf",
            },
            timeout=180.0,
        )

    def attach_file(self, data_url: str, filename: str = "", path: str = "") -> dict:
        """Stage a non-image file and return an @file: ref for the prompt."""
        params: dict[str, str] = {"data_url": data_url}
        if filename:
            params["name"] = filename
        if path:
            params["path"] = path
        elif filename:
            params["path"] = filename  # naming hint when no client path exists
        return self._attach_rpc("file.attach", params)

    def _attach_rpc(
        self,
        method: str,
        params: dict,
        *,
        timeout: float = 120.0,
    ) -> dict:
        """Call an attach RPC, resuming/recreating the session on stale 4001."""
        self.last_active = time.monotonic()
        sid = self.ensure_session()
        call_params = {**params, "session_id": sid}
        logger.debug("chat_id=%s %s using session_id=%s", self.chat_id, method, sid)
        result = self._call(method, call_params, timeout=timeout)
        if isinstance(result, dict) and result.get("error", {}).get("code") == 4001:
            logger.warning(
                "chat_id=%s session %s not found during %s, attempting resume",
                self.chat_id, sid, method,
            )
            try:
                data = self.session_resume(sid)
                sid = data.get("session_id") or self.hermes_session_id or sid
            except RuntimeError:
                with self._lock:
                    self.hermes_session_id = None
                    self._session_verified = True
                sid = self.ensure_session()
            call_params["session_id"] = sid
            result = self._call(method, call_params, timeout=timeout)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"].get("message", f"{method} failed"))
        return (result.get("result") or {})

    # ------------------------------------------------------------------
    def interrupt(self) -> None:
        """Send session.interrupt to the gateway to stop the current turn."""
        self.last_active = time.monotonic()
        with self._lock:
            sid = self.hermes_session_id
        if not sid:
            return
        result = self._call("session.interrupt", {"session_id": sid})
        if isinstance(result, dict) and "error" in result:
            code = result["error"].get("code", 0)
            msg = result["error"].get("message", "session.interrupt failed")
            logger.warning("chat_id=%s session.interrupt error %s: %s", self.chat_id, code, msg)
        else:
            logger.info("chat_id=%s session.interrupt sent", self.chat_id)

    def steer(self, text: str) -> dict:
        """Inject a mid-turn correction without interrupting (session.steer).

        Lands on the next tool-result batch so the model course-corrects on its
        following iteration — same surface as the TUI/desktop steer affordance.
        """
        self.last_active = time.monotonic()
        cleaned = (text or "").strip()
        if not cleaned:
            raise RuntimeError("steer text is required")
        sid = self.hermes_session_id
        if not sid:
            raise RuntimeError("no active Hermes session to steer")
        result = self._call(
            "session.steer",
            {"session_id": sid, "text": cleaned},
            expected_error_codes=frozenset({4001, 4010}),
        )
        if isinstance(result, dict) and "error" in result:
            code = result["error"].get("code", 0)
            msg = result["error"].get("message", "session.steer failed")
            logger.warning("chat_id=%s session.steer error %s: %s", self.chat_id, code, msg)
            raise RuntimeError(msg)
        data = result.get("result") or {}
        logger.info(
            "chat_id=%s session.steer status=%s",
            self.chat_id, data.get("status"),
        )
        return data

    # ------------------------------------------------------------------
    def commands_catalog(self) -> dict:
        """Return Hermes slash-command catalog (commands.catalog)."""
        self.last_active = time.monotonic()
        result = self._call("commands.catalog", {})
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"].get("message", "commands.catalog failed"))
        return result.get("result") or {}

    def run_slash(self, command: str, *, _depth: int = 0) -> dict:
        """Run a slash command via slash.exec, falling back to command.dispatch.

        Mirrors the TUI/desktop client path. Returns a normalized action dict:

        - ``{"action": "output", "text": "..."}`` — render inline (no agent turn)
        - ``{"action": "send"|"skill", "message": "...", "name"?, "notice"?}`` — submit as prompt
        - ``{"action": "prefill", "message": "...", "notice"?}`` — drop into composer
        """
        self.last_active = time.monotonic()
        if _depth > 8:
            raise RuntimeError("slash alias loop")
        raw = (command or "").strip()
        if not raw:
            raise RuntimeError("empty command")
        if not raw.startswith("/"):
            raw = "/" + raw

        # Ensure we have a live Hermes session — slash.exec needs session_id.
        sid = self.ensure_session()

        body = raw[1:]  # slash.exec wants no leading slash
        parts = body.split(None, 1)
        name = (parts[0] if parts else "").strip()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            raise RuntimeError("empty command")

        def _as_dispatch(payload: Any) -> Optional[dict]:
            if not isinstance(payload, dict):
                return None
            dtype = payload.get("type")
            if dtype in ("exec", "plugin"):
                return {"action": "output", "text": str(payload.get("output") or "(no output)")}
            if dtype == "alias":
                target = str(payload.get("target") or "").lstrip("/")
                if not target:
                    return {"action": "output", "text": "empty alias target"}
                return self.run_slash(f"/{target}" + (f" {arg}" if arg else ""), _depth=_depth + 1)
            if dtype == "skill":
                return {
                    "action": "skill",
                    "name": str(payload.get("name") or name),
                    "message": str(payload.get("message") or ""),
                    "notice": str(payload.get("notice") or "") or None,
                }
            if dtype == "send":
                return {
                    "action": "send",
                    "message": str(payload.get("message") or ""),
                    "notice": str(payload.get("notice") or "") or None,
                }
            if dtype == "prefill":
                return {
                    "action": "prefill",
                    "message": str(payload.get("message") or ""),
                    "notice": str(payload.get("notice") or "") or None,
                }
            return None

        # Prefer slash.exec (worker + side-effect mirror). Fall back to
        # command.dispatch for skills / structured directives.
        exec_result = self._call(
            "slash.exec",
            {"session_id": sid, "command": body},
            expected_error_codes=frozenset({4001, 4018, 5030}),
            timeout=600.0,
        )
        if isinstance(exec_result, dict) and "error" not in exec_result:
            payload = exec_result.get("result") or {}
            dispatch = _as_dispatch(payload)
            if dispatch is not None:
                return dispatch
            if isinstance(payload, dict) and "output" in payload:
                text = str(payload.get("output") or f"/{name}: no output")
                warning = payload.get("warning")
                if warning:
                    text = f"warning: {warning}\n{text}"
                return {"action": "output", "text": text}

        # Fall through to command.dispatch (skills, aliases, quick commands).
        dispatch_result = self._call(
            "command.dispatch",
            {"session_id": sid, "name": name, "arg": arg},
            expected_error_codes=frozenset({4001, 4004, 4011, 4018}),
        )
        if isinstance(dispatch_result, dict) and "error" in dispatch_result:
            # Prefer the original slash.exec error if dispatch also failed.
            if isinstance(exec_result, dict) and "error" in exec_result:
                msg = exec_result["error"].get("message", "slash.exec failed")
            else:
                msg = dispatch_result["error"].get("message", "command.dispatch failed")
            raise RuntimeError(msg)

        payload = dispatch_result.get("result") or {}
        dispatch = _as_dispatch(payload)
        if dispatch is not None:
            return dispatch
        if isinstance(payload, dict) and "output" in payload:
            return {"action": "output", "text": str(payload.get("output") or "(no output)")}
        raise RuntimeError(f"/{name}: unrecognized command response")

    # ------------------------------------------------------------------
    def session_undo(self) -> dict:
        """Undo the last turn in the Hermes session."""
        self.last_active = time.monotonic()
        sid = self.ensure_session()
        result = self._call(
            "session.undo",
            {"session_id": sid},
            expected_error_codes=frozenset({4001, 4009}),
        )
        if isinstance(result, dict) and "error" in result:
            code = result["error"].get("code", 0)
            msg = result["error"].get("message", "session.undo failed")
            logger.warning("chat_id=%s session.undo error %s: %s", self.chat_id, code, msg)
            raise RuntimeError(msg)
        data = result.get("result") or {"status": "ok"}
        logger.info("chat_id=%s session.undo removed=%s", self.chat_id, data.get("removed"))
        return data

    def session_compress(self, focus_topic: str = "") -> dict:
        """Manually compact conversation context (session.compress)."""
        self.last_active = time.monotonic()
        sid = self.ensure_session()
        params: dict = {"session_id": sid}
        topic = (focus_topic or "").strip()
        if topic:
            params["focus_topic"] = topic
        result = self._call(
            "session.compress",
            params,
            expected_error_codes=frozenset({4001, 4009}),
            timeout=600.0,
        )
        if isinstance(result, dict) and "error" in result:
            msg = result["error"].get("message", "session.compress failed")
            logger.warning("chat_id=%s session.compress error: %s", self.chat_id, msg)
            raise RuntimeError(msg)
        data = result.get("result") or {}
        logger.info(
            "chat_id=%s session.compress %s→%s msgs tokens %s→%s",
            self.chat_id,
            data.get("before_messages"),
            data.get("after_messages"),
            data.get("before_tokens"),
            data.get("after_tokens"),
        )
        return data

    def session_branch(self, name: str = "") -> dict:
        """Fork the current session into a new Hermes session (session.branch)."""
        self.last_active = time.monotonic()
        sid = self.ensure_session()
        params: dict = {"session_id": sid}
        cleaned = (name or "").strip()
        if cleaned:
            params["name"] = cleaned
        result = self._call(
            "session.branch",
            params,
            expected_error_codes=frozenset({4001, 4008, 4090}),
            timeout=120.0,
        )
        if isinstance(result, dict) and "error" in result:
            msg = result["error"].get("message", "session.branch failed")
            logger.warning("chat_id=%s session.branch error: %s", self.chat_id, msg)
            raise RuntimeError(msg)
        data = result.get("result") or {}
        logger.info(
            "chat_id=%s session.branch → %s title=%r",
            self.chat_id, data.get("session_id"), data.get("title"),
        )
        return data

    # ------------------------------------------------------------------
    def model_options(self, *, explicit_only: bool = True, include_unauthenticated: bool = False) -> dict:
        """Return the gateway's model/provider picker payload."""
        self.last_active = time.monotonic()
        cache_key = f"model_options:{explicit_only}:{include_unauthenticated}"
        cached = self._model_options_cache.get(cache_key)
        if cached is not None:
            return cached
        params: dict = {
            "picker_hints": True,
            "canonical_order": True,
            "pricing": True,
            "capabilities": True,
            "explicit_only": explicit_only,
            "include_unauthenticated": include_unauthenticated,
        }
        if self.hermes_session_id:
            params["session_id"] = self.hermes_session_id
        result = self._call("model.options", params)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"].get("message", "model.options failed"))
        data = result.get("result") or {}
        self._model_options_cache.set(cache_key, data)
        return data

    # ------------------------------------------------------------------
    def current_model(self) -> dict:
        """Return the currently configured model/provider."""
        self.last_active = time.monotonic()
        cache_key = "current_model"
        cached = self._current_model_cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._call("config.get", {"key": "provider"})
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"].get("message", "config.get provider failed"))
        data = result.get("result") or {}
        # model.options returns the real routing gateway in its top-level provider field,
        # whereas config.get provider often returns the model vendor (e.g. "deepseek").
        try:
            opts = self.model_options(explicit_only=True, include_unauthenticated=False)
            if opts.get("model") and opts.get("provider"):
                data["model"] = opts["model"]
                data["provider"] = opts["provider"]
        except RuntimeError:
            pass
        self._current_model_cache.set(cache_key, data)
        return data

    # ------------------------------------------------------------------
    def set_model(self, value: str, confirm_expensive_model: bool = False) -> dict:
        """Switch the model for this session (and persist as a session override)."""
        self.last_active = time.monotonic()
        sid = self.ensure_session()
        result = self._call(
            "config.set",
            {
                "key": "model",
                "value": value,
                "session_id": sid,
                "confirm_expensive_model": confirm_expensive_model,
            },
        )
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"].get("message", "config.set model failed"))
        self._model_options_cache._data.clear()
        self._current_model_cache._data.clear()
        return result.get("result") or {}

    # ------------------------------------------------------------------
    def session_usage(self) -> dict:
        """Return token usage for the current session, or {} if none exists."""
        sid = self.hermes_session_id
        if not sid:
            return {}
        result = self._call(
            "session.usage",
            {"session_id": sid},
            expected_error_codes=frozenset({4001}),
        )
        if isinstance(result, dict) and "error" in result:
            if result["error"].get("code") == 4001:
                self._mark_session_missing(sid)
            return {}
        return result.get("result") or {}

    def session_context_breakdown(self) -> dict:
        """Return context breakdown for the current session, or {} if none exists."""
        sid = self.hermes_session_id
        if not sid:
            return {}
        result = self._call(
            "session.context_breakdown",
            {"session_id": sid},
            expected_error_codes=frozenset({4001}),
        )
        if isinstance(result, dict) and "error" in result:
            if result["error"].get("code") == 4001:
                self._mark_session_missing(sid)
            return {}
        return result.get("result") or {}

    # ------------------------------------------------------------------
    def get_pending_gate(self) -> Optional[dict]:
        with self._lock:
            return self._pending_gate

    def set_pending_gate(self, gate: Optional[dict]) -> None:
        with self._lock:
            self._pending_gate = gate

    # ------------------------------------------------------------------
    def respond_gate(self, gate_kind: str, gate_id: str, value: str) -> None:
        """Resolve an approval / clarify / sudo / secret gate."""
        self.last_active = time.monotonic()
        sid = self.hermes_session_id or ""
        params: dict = {"session_id": sid, "request_id": gate_id}

        if gate_kind == "approval":
            params["choice"] = value
            self._call("approval.respond", params)
        elif gate_kind == "clarify":
            params["answer"] = value
            self._call("clarify.respond", params)
        elif gate_kind == "sudo":
            params["password"] = value
            self._call("sudo.respond", params)
        elif gate_kind == "secret":
            params["value"] = value
            self._call("secret.respond", params)
        else:
            raise ValueError(f"Unknown gate kind: {gate_kind!r}")

    # ------------------------------------------------------------------
    def close(self) -> None:
        sid = self.hermes_session_id
        if sid:
            try:
                self._call("session.close", {"session_id": sid})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SessionManager — owns all live GatewaySession objects
# ---------------------------------------------------------------------------

class GatewaySessionManager:
    """Owns all live GatewaySession objects, keyed by chat_id."""

    def __init__(self, session_idle_timeout: float = 600.0) -> None:
        self._sessions: dict[str, GatewaySession] = {}
        self._lock = threading.Lock()
        self.session_idle_timeout = session_idle_timeout
        self._reaper_started = False

    def get_or_create(
        self,
        chat_id: str,
        hermes_session_id: Optional[str],
        loop: asyncio.AbstractEventLoop,
    ) -> GatewaySession:
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                session = GatewaySession(chat_id, hermes_session_id, loop)
                self._sessions[chat_id] = session
                logger.debug("chat_id=%s new GatewaySession", chat_id)
            self._ensure_reaper()
            return session

    def get(self, chat_id: str) -> Optional[GatewaySession]:
        with self._lock:
            return self._sessions.get(chat_id)

    def remove(self, chat_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(chat_id, None)
        if session:
            session.close()

    def _ensure_reaper(self) -> None:
        if self._reaper_started:
            return
        self._reaper_started = True
        threading.Thread(target=self._reap_loop, daemon=True, name="gw-session-reaper").start()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(60)
            now = time.monotonic()
            with self._lock:
                stale = [
                    (cid, s)
                    for cid, s in self._sessions.items()
                    if now - s.last_active > self.session_idle_timeout
                ]
                for cid, _ in stale:
                    self._sessions.pop(cid, None)
            for _, s in stale:
                try:
                    s.close()
                except Exception:
                    pass
