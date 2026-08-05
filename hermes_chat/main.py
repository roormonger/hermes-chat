"""
hermes-chat FastAPI service.

Runs on the same host as the `hermes` CLI. Exposes:

  POST /v1/chat          -> SSE stream of {"type": "text"|"gate_interrupt"|"tool_start"|...
  POST /v1/gate/resolve  -> resolves an approval/clarify/sudo gate
  WS   /api/ws           -> raw tui_gateway JSON-RPC WebSocket passthrough
  GET  /healthz          -> liveness probe

See README.md for the full protocol description.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile, WebSocket, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analytics import get_models_analytics as _get_models_analytics_db
from .chat_history import ChatHistoryStore
from .chat_runs import ChatRunManager
from .config import _plugin_dir, auth_secret, load_config
from .database import ChatSessionStore
from .users import UserStore
from .model_access import (
    filter_analytics_models_for_free_user,
    filter_model_options_for_free_user,
    model_allowed_for_free_user,
)
from .suggestions import (
    SuggestionStore,
    SuggestionWorker,
    ensure_suggestions_prompt,
    sample_suggestions,
)

config = load_config()
_log_level = getattr(logging, config.log_level.upper(), logging.INFO)
if config.debug:
    _log_level = logging.DEBUG
logging.basicConfig(level=_log_level)
logger = logging.getLogger("hermes_chat")

# ---------------------------------------------------------------------------
# Gateway backend selection: prefer tui_gateway (in-process JSON-RPC) over
# the legacy PTY path. Both expose the same interface to the REST layer.
# ---------------------------------------------------------------------------
from .gateway_session import (
    gateway_available,
    gateway_available_error,
    GatewaySessionManager,
    HERMES_CHAT_SOURCE,
    _translate_event,
)

if gateway_available():
    logger.info("tui_gateway available — using in-process JSON-RPC backend")
    _BACKEND = "gateway"
else:
    logger.warning(
        "tui_gateway not available (%s) — using legacy PTY backend",
        gateway_available_error() or "unknown import error",
    )
    from .pty_manager import SessionManager as _PtySessionManager  # type: ignore
    _BACKEND = "pty"

app = FastAPI(title="hermes-chat", version="0.1.0")

store = ChatSessionStore()
history = ChatHistoryStore()
users = UserStore(secret=auth_secret())
suggestion_store = SuggestionStore()
suggestion_worker: Optional[SuggestionWorker] = None

if _BACKEND == "gateway":
    sessions = GatewaySessionManager(session_idle_timeout=config.session_idle_timeout)
    runs: Optional[ChatRunManager] = ChatRunManager(history, store)
    suggestion_worker = SuggestionWorker(
        users=users, history=history, sessions=sessions, store=suggestion_store
    )
else:
    sessions = _PtySessionManager(config)  # type: ignore[assignment]
    runs = None


# --------------------------------------------------------------------------- #
# Static web UI
# --------------------------------------------------------------------------- #

_webui_dir = (_plugin_dir() or Path(__file__).resolve().parent.parent) / "webui" / "dist"
if _webui_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_webui_dir)), name="static")


@app.get("/")
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/chat")


@app.get("/chat")
async def chat_ui() -> FileResponse:
    return _chat_index()


@app.get("/chat/{chat_id}")
async def chat_ui_with_id(chat_id: str) -> FileResponse:
    """SPA entry for a deep-linked chat. The React app reads chat_id from the path."""
    return _chat_index()


def _chat_index() -> FileResponse:
    index_path = _webui_dir / "index.html"
    if not index_path.exists():
        logger.error("hermes-chat web UI not found at %s", index_path)
        raise HTTPException(status_code=404, detail=f"web UI not found at {index_path}")
    return FileResponse(
        str(index_path),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    if suggestion_worker is not None:
        suggestion_worker.stop()
    if runs is not None:
        await runs.shutdown()


@app.on_event("startup")
async def _on_startup() -> None:
    logger.info("hermes-chat listening on %s:%s", config.host, config.port)
    logger.info("hermes-chat web UI directory: %s", _webui_dir)
    logger.info("hermes-chat backend: %s", _BACKEND)
    try:
        ensure_suggestions_prompt()
    except Exception as exc:
        logger.warning("suggestions prompt setup failed: %s", exc)

    if _BACKEND == "gateway":
        loop = asyncio.get_running_loop()
        try:
            gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)
            await loop.run_in_executor(None, gw.model_options)
            await loop.run_in_executor(None, gw.current_model)
            logger.info("hermes-chat model catalog pre-warmed")
        except Exception as exc:
            logger.warning("hermes-chat model catalog pre-warm failed: %s", exc)
        if suggestion_worker is not None:
            suggestion_worker.start(loop)


class ChatRequest(BaseModel):
    chat_id: str
    message: str


class StartChatRunRequest(BaseModel):
    chat_id: str
    message: str
    assistant_message_id: int


class GateResolveRequest(BaseModel):
    chat_id: str
    gate_id: str
    choice: str
    gate_kind: str = "approval"  # approval | clarify | sudo | secret


class CreateChatRequest(BaseModel):
    title: str = "New chat"


class RenameChatRequest(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None


class SaveMessageRequest(BaseModel):
    role: str
    content: str
    images: list[str] = []
    tool_steps: list[dict] | None = None
    reasoning: str | None = None


class UpdateMessageRequest(BaseModel):
    content: str
    tool_steps: list[dict] | None = None
    reasoning: str | None = None


class UsageSaveRequest(BaseModel):
    usage: dict
    context_window: int | None = None


class GateChoiceRequest(BaseModel):
    choice: str


class ImageAttachRequest(BaseModel):
    chat_id: str
    content_base64: str
    filename: str = ""


class PdfAttachRequest(BaseModel):
    chat_id: str
    content_base64: str
    filename: str = "uploaded.pdf"


class FileAttachRequest(BaseModel):
    chat_id: str
    data_url: str
    filename: str = ""
    path: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    user_id: str
    username: str
    token: str
    free_models_only: bool = False


def _user_is_free_only(user: dict) -> bool:
    return bool(user.get("free_models_only"))


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
    user = users.decode_token(token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return user


def get_current_user_query(token: str | None = None, authorization: str | None = Header(default=None)) -> dict:
    """Like get_current_user but also accepts ?token= query param (for <img src=> URLs)."""
    raw = token or _extract_bearer_token(authorization)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    user = users.decode_token(raw)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return user


def _verify_chat_access(chat_id: str, current_user: dict) -> dict:
    """Return the chat if the user owns it (or it is unowned), otherwise raise 404."""
    chat = history.get_chat(chat_id, current_user["user_id"])
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


def _resolve_gate_choice(options: list[str], choice: str) -> str | None:
    """Return the matched option label, or None if the choice is unrecognised.

    Accepts the exact option label or a 1-based number/index.
    """
    choice = choice.strip()
    lowered = choice.lower()
    for i, opt in enumerate(options):
        if opt.strip().lower() == lowered:
            return opt
        numbered = f"{i + 1}"
        if lowered == numbered or lowered.startswith(numbered + ")") or lowered.startswith(numbered + "."):
            return opt
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except ValueError:
        pass
    return None


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/v1/suggestions")
async def get_suggestions(current_user: dict = Depends(get_current_user)) -> dict:
    """Sample starter chips from the user's pre-generated pool (static fallback)."""
    cfg = load_config()
    count = max(1, min(int(cfg.suggestions_show_count), 8))
    pool = suggestion_store.get_pool(current_user["user_id"])
    meta = suggestion_store.get_meta(current_user["user_id"])
    sampled = sample_suggestions(pool, count)
    return {
        "suggestions": sampled,
        "source": "pool" if pool else "static",
        "mode": (meta or {}).get("mode"),
        "updated_at": (meta or {}).get("updated_at"),
    }


@app.post("/v1/suggestions/refresh")
async def refresh_suggestions(request: Request) -> dict:
    """Force-regenerate suggestion pools for all chat users.

    Localhost-only so the Hermes dashboard plugin can trigger a run without a
    chat-user JWT. Generation runs in the background on the suggestion worker.
    """
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Localhost only")
    if suggestion_worker is None:
        raise HTTPException(
            status_code=503,
            detail="Suggestion worker unavailable (gateway backend required)",
        )
    started = suggestion_worker.refresh_all_async()
    if not started:
        return {
            "status": "busy",
            "message": "A suggestion refresh is already running.",
        }
    user_count = len(users.list_users())
    return {
        "status": "started",
        "users": user_count,
        "message": f"Refreshing suggestion pools for {user_count} user(s).",
    }


@app.post("/api/auth/login")
async def login(request: LoginRequest) -> AuthResponse:
    user = users.verify_user(request.username, request.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = users.create_token(user["user_id"])
    return AuthResponse(
        user_id=user["user_id"],
        username=user["username"],
        token=token,
        free_models_only=bool(user.get("free_models_only")),
    )


@app.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)) -> dict:
    return {
        "user_id": current_user["user_id"],
        "username": current_user["username"],
        "free_models_only": bool(current_user.get("free_models_only")),
    }


@app.post("/v1/chat/runs")
async def start_chat_run(
    request: StartChatRunRequest, current_user: dict = Depends(get_current_user)
) -> dict:
    if _BACKEND != "gateway" or runs is None:
        raise HTTPException(status_code=501, detail="Durable chat runs require the gateway backend")
    if not request.chat_id.strip() or not request.message.strip():
        raise HTTPException(status_code=400, detail="chat_id and message are required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    session = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        run = await runs.start(
            chat_id=request.chat_id,
            user_id=current_user["user_id"],
            assistant_message_id=request.assistant_message_id,
            message=request.message,
            session=session,
            submit=session.submit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.snapshot()


@app.get("/v1/chat/runs/active")
async def active_chat_run(
    chat_id: str, current_user: dict = Depends(get_current_user)
) -> dict:
    _verify_chat_access(chat_id, current_user)
    if runs is None:
        return {"active": False, "protocol_version": 2}
    run = runs.get_active(chat_id, current_user["user_id"])
    return {
        "active": run is not None,
        "protocol_version": 2,
        "run": run.snapshot() if run is not None else None,
    }


@app.get("/v1/chat/runs/{run_id}")
async def chat_run_status(
    run_id: str, current_user: dict = Depends(get_current_user)
) -> dict:
    if runs is None:
        raise HTTPException(status_code=404, detail="Chat run not found")
    run = runs.get(run_id, current_user["user_id"])
    if run is None:
        raise HTTPException(status_code=404, detail="Chat run not found")
    return run.snapshot()


@app.get("/v1/chat/runs/{run_id}/events")
async def chat_run_events(
    run_id: str,
    after: int = 0,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    if runs is None or runs.get(run_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat run not found")
    return StreamingResponse(
        runs.subscribe(run_id, current_user["user_id"], after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/chat")
async def chat(request: ChatRequest, current_user: dict = Depends(get_current_user)) -> StreamingResponse:
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    if _BACKEND == "gateway":
        return await _chat_gateway(request, loop)
    return await _chat_pty(request, loop)


@app.get("/v1/file/download")
async def file_download(
    path: str,
    current_user: dict = Depends(get_current_user),
) -> FileResponse:
    """Stream an agent-produced file as a download.

    ``path`` must be an absolute path that lives under HERMES_HOME
    (``~/.hermes``) or under the current working directory / workspace.
    Symlinks are resolved before the check so traversal tricks don't work.
    """
    import mimetypes
    import os

    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Must be under HERMES_HOME or cwd
    allowed_roots = [hermes_home, Path.cwd().resolve()]
    if not any(target == r or r in target.parents for r in allowed_roots):
        raise HTTPException(status_code=403, detail="Path is outside allowed directories")

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    # Block credential files
    blocked_names = {".env", ".envrc", "auth.json", "auth.lock", ".auth_secret"}
    if target.name.lower() in blocked_names:
        raise HTTPException(status_code=403, detail="Access to this file is not allowed")

    size = target.stat().st_size
    if size > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large to download")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})


@app.get("/v1/file/preview")
async def file_preview(
    path: str,
    current_user: dict = Depends(get_current_user_query),
) -> FileResponse:
    """Serve an image file for inline display (no Content-Disposition attachment).

    Accepts auth token via ``?token=`` query param so ``<img src=...>`` tags
    can load it without custom headers.  Restricted to image extensions only.
    """
    import mimetypes
    import os

    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")

    allowed_roots = [hermes_home, Path.cwd().resolve()]
    if not any(target == r or r in target.parents for r in allowed_roots):
        raise HTTPException(status_code=403, detail="Path is outside allowed directories")

    if target.suffix.lower() not in _IMAGE_EXTENSIONS:
        raise HTTPException(status_code=403, detail="Only image files can be previewed")

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    size = target.stat().st_size
    if size > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large to preview")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(path=str(target), media_type=mime_type)


@app.post("/v1/image/attach")
async def image_attach(request: ImageAttachRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Image attachment requires gateway backend")
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not request.content_base64.strip():
        raise HTTPException(status_code=400, detail="content_base64 is required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(
            None, gw.attach_image_bytes, request.content_base64, request.filename
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if gw.hermes_session_id and gw.hermes_session_id != hermes_sid:
        store.set_hermes_session_id(request.chat_id, gw.hermes_session_id)
    return result


@app.post("/v1/pdf/attach")
async def pdf_attach(request: PdfAttachRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Render a PDF to vision tiles (pdf.attach). Falls back to file.attach if pdftoppm is missing."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="PDF attachment requires gateway backend")
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not request.content_base64.strip():
        raise HTTPException(status_code=400, detail="content_base64 is required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    filename = request.filename or "uploaded.pdf"
    try:
        result = await loop.run_in_executor(
            None, gw.attach_pdf, request.content_base64, filename
        )
    except Exception as exc:
        msg = str(exc)
        # No poppler → stage as a regular @file: so the agent can still read it.
        if "pdftoppm" in msg.lower():
            data_url = request.content_base64
            if not data_url.startswith("data:"):
                data_url = f"data:application/pdf;base64,{data_url}"
            try:
                result = await loop.run_in_executor(
                    None, gw.attach_file, data_url, filename, filename
                )
                result = {**result, "fallback": "file.attach", "pdf_render_error": msg}
            except Exception as fallback_exc:
                raise HTTPException(status_code=500, detail=str(fallback_exc)) from fallback_exc
        else:
            raise HTTPException(status_code=500, detail=msg) from exc
    if gw.hermes_session_id and gw.hermes_session_id != hermes_sid:
        store.set_hermes_session_id(request.chat_id, gw.hermes_session_id)
    return result


@app.post("/v1/file/attach")
async def file_attach(request: FileAttachRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Stage a non-image file into the session workspace (file.attach → @file: ref)."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="File attachment requires gateway backend")
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not request.data_url.strip():
        raise HTTPException(status_code=400, detail="data_url is required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(
            None,
            gw.attach_file,
            request.data_url,
            request.filename,
            request.path or request.filename,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if gw.hermes_session_id and gw.hermes_session_id != hermes_sid:
        store.set_hermes_session_id(request.chat_id, gw.hermes_session_id)
    return result


class CancelRequest(BaseModel):
    chat_id: str


class SteerRequest(BaseModel):
    chat_id: str
    text: str


@app.post("/v1/chat/cancel")
async def cancel_chat(request: CancelRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Interrupt the currently running agent turn for a chat."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Cancel requires gateway backend")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    if runs is not None:
        run = await runs.cancel(request.chat_id, current_user["user_id"])
        if run is not None:
            return {"status": "interrupted", "run_id": run.run_id}
    gw = sessions.get(request.chat_id)  # type: ignore[attr-defined]
    if gw is None:
        return {"status": "no_session"}
    await loop.run_in_executor(None, gw.interrupt)
    return {"status": "interrupted"}


@app.post("/v1/chat/steer")
async def steer_chat(request: SteerRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Inject a mid-turn correction into the active Hermes turn (session.steer)."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Steer requires gateway backend")
    _verify_chat_access(request.chat_id, current_user)
    text = (request.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    loop = asyncio.get_running_loop()
    gw = sessions.get(request.chat_id)  # type: ignore[attr-defined]
    if gw is None:
        hermes_sid = store.get_hermes_session_id(request.chat_id)
        if not hermes_sid:
            raise HTTPException(status_code=404, detail="No active session to steer")
        gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(None, gw.steer, text)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": result.get("status", "queued"), "text": result.get("text", text)}


class SlashCommandRequest(BaseModel):
    chat_id: str
    command: str


@app.get("/v1/chat/commands")
async def list_commands(
    chat_id: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return Hermes slash-command catalog (commands.catalog)."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Commands require gateway backend")
    loop = asyncio.get_running_loop()
    if chat_id:
        _verify_chat_access(chat_id, current_user)
        hermes_sid = store.get_hermes_session_id(chat_id)
        gw = sessions.get_or_create(chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    else:
        gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)  # type: ignore[attr-defined]
    try:
        catalog = await loop.run_in_executor(None, gw.commands_catalog)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return catalog


@app.post("/v1/chat/command")
async def run_command(request: SlashCommandRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Dispatch a slash command (slash.exec → command.dispatch)."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Commands require gateway backend")
    _verify_chat_access(request.chat_id, current_user)
    command = (request.command or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is required")
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(None, gw.run_slash, command)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Persist hermes_session_id if ensure_session created one during dispatch.
    if gw.hermes_session_id and gw.hermes_session_id != hermes_sid:
        store.set_hermes_session_id(request.chat_id, gw.hermes_session_id)
    return result


class CompressRequest(BaseModel):
    chat_id: str
    focus_topic: str = ""


class BranchRequest(BaseModel):
    chat_id: str
    name: str = ""


def _last_user_prefill(chat_id: str, user_id: Optional[str]) -> str:
    """Return the last user message text (for composer prefill after undo)."""
    msgs = history.get_messages(chat_id, user_id)
    for msg in reversed(msgs):
        if msg.get("role") == "user":
            return str(msg.get("content") or "").strip()
    return ""


def _resync_local_from_hermes(chat_id: str, user_id: Optional[str], gw) -> int:
    """Replace the local message cache from the live Hermes transcript."""
    history.delete_messages(chat_id, user_id)
    hermes_messages = gw.session_history()
    return _seed_local_messages_from_hermes(chat_id, user_id, hermes_messages)


@app.post("/v1/chat/undo")
async def undo_chat(request: CancelRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Undo the last user/assistant turn (session.undo) and return prefill text."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Undo requires gateway backend")
    user_id = current_user["user_id"]
    _verify_chat_access(request.chat_id, current_user)
    prefill = _last_user_prefill(request.chat_id, user_id)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        undo_result = await loop.run_in_executor(None, gw.session_undo)
    except RuntimeError as exc:
        detail = str(exc)
        status = 409 if "interrupt" in detail.lower() or "running" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    deleted = history.delete_last_turn(request.chat_id, user_id)
    return {
        "status": "ok",
        "deleted": deleted,
        "removed": (undo_result or {}).get("removed"),
        "prefill": prefill,
    }


@app.post("/v1/chat/compress")
async def compress_chat(request: CompressRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Manually compact conversation context (session.compress)."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Compress requires gateway backend")
    user_id = current_user["user_id"]
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(
            None, gw.session_compress, (request.focus_topic or "").strip()
        )
    except RuntimeError as exc:
        detail = str(exc)
        status = 409 if "interrupt" in detail.lower() or "running" in detail.lower() or "busy" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail) from exc

    if gw.hermes_session_id and gw.hermes_session_id != hermes_sid:
        store.set_hermes_session_id(request.chat_id, gw.hermes_session_id)

    seeded = await loop.run_in_executor(
        None, _resync_local_from_hermes, request.chat_id, user_id, gw
    )
    return {
        "status": result.get("status", "ok"),
        "before_messages": result.get("before_messages"),
        "after_messages": result.get("after_messages"),
        "before_tokens": result.get("before_tokens"),
        "after_tokens": result.get("after_tokens"),
        "summary": result.get("summary") or result.get("info") or "",
        "message_count": seeded,
    }


@app.post("/v1/chat/branch")
async def branch_chat(request: BranchRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Fork the current session into a new chat (session.branch)."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Branch requires gateway backend")
    user_id = current_user["user_id"]
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    parent_gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        branched = await loop.run_in_executor(
            None, parent_gw.session_branch, (request.name or "").strip()
        )
    except RuntimeError as exc:
        detail = str(exc)
        status = 409 if "busy" in detail.lower() or "running" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail) from exc

    new_sid = str(branched.get("session_id") or "").strip()
    if not new_sid:
        raise HTTPException(status_code=500, detail="session.branch returned no session_id")

    parent_chat = history.get_chat(request.chat_id, user_id) or {}
    parent_title = str(parent_chat.get("title") or "Chat").strip() or "Chat"
    branch_title = str(branched.get("title") or "").strip()
    if not branch_title:
        name = (request.name or "").strip()
        branch_title = name or f"{parent_title} (branch)"

    new_chat_id = str(uuid.uuid4())
    history.create_chat(new_chat_id, user_id, branch_title)
    child_gw = sessions.get_or_create(new_chat_id, new_sid, loop)  # type: ignore[attr-defined]
    try:
        resumed = await loop.run_in_executor(None, child_gw.session_resume, new_sid)
    except RuntimeError as exc:
        history.delete_chat(new_chat_id, user_id)
        store.delete(new_chat_id)
        sessions.remove(new_chat_id)  # type: ignore[attr-defined]
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    tip = str(resumed.get("session_id") or child_gw.hermes_session_id or new_sid)
    store.set_hermes_session_id(new_chat_id, tip)
    hermes_messages = resumed.get("messages") or []
    seeded = _seed_local_messages_from_hermes(new_chat_id, user_id, hermes_messages)
    logger.info(
        "branched chat %s → %s hermes %s→%s seeded=%d",
        request.chat_id, new_chat_id, hermes_sid, tip, seeded,
    )
    return {
        "status": "ok",
        "chat_id": new_chat_id,
        "title": branch_title,
        "hermes_session_id": tip,
        "parent": branched.get("parent") or hermes_sid,
        "message_count": seeded,
    }


@app.get("/v1/usage")
async def get_usage(
    chat_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return the current session's token usage and context breakdown from Hermes."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Usage requires gateway backend")
    _verify_chat_access(chat_id, current_user)
    loop = asyncio.get_running_loop()
    hermes_sid = store.get_hermes_session_id(chat_id)
    gw = sessions.get_or_create(chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    usage, breakdown = await asyncio.gather(
        loop.run_in_executor(None, gw.session_usage),
        loop.run_in_executor(None, gw.session_context_breakdown),
    )
    # Stale DB mapping: gateway no longer has this session (restart/reap).
    # Clear so the 3s usage poll stops retrying the dead id.
    if hermes_sid and gw.hermes_session_id is None:
        store.delete(chat_id)
    return {"usage": usage, "context_breakdown": breakdown}


class ModelSwitchRequest(BaseModel):
    chat_id: str
    model: str
    provider: str = ""
    confirm_expensive_model: bool = False


# Shared scratch session key for catalog RPCs that don't belong to a specific chat.
_MODEL_CATALOG_CHAT_ID = "__hermes_chat_models_catalog__"
_DEFAULT_HERMES_DASHBOARD_URL = "http://127.0.0.1:9119"


def _hermes_dashboard_url() -> str:
    """Return the Hermes dashboard URL, in order of precedence:

    1. Explicit config value (hermes_dashboard_url)
    2. HERMES_DASHBOARD_URL environment variable
    3. HERMES_DASHBOARD_PORT environment variable (host 127.0.0.1)
    4. Default localhost port used by Hermes dashboard
    """
    if config.hermes_dashboard_url:
        return config.hermes_dashboard_url
    env_url = os.environ.get("HERMES_DASHBOARD_URL")
    if env_url:
        return env_url
    env_port = os.environ.get("HERMES_DASHBOARD_PORT")
    if env_port:
        return f"http://127.0.0.1:{env_port}"
    return _DEFAULT_HERMES_DASHBOARD_URL


@app.get("/v1/models")
async def list_models(current_user: dict = Depends(get_current_user)) -> dict:
    """List all available models/providers configured in Hermes."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Model picker requires gateway backend")
    loop = asyncio.get_running_loop()
    gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(None, gw.model_options)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if _user_is_free_only(current_user):
        return filter_model_options_for_free_user(result)
    return result


@app.get("/v1/analytics/models")
async def list_analytics_models(current_user: dict = Depends(get_current_user)) -> dict:
    """Return Hermes dashboard analytics models (recently-used / saved profiles).

    Tries to read directly from the Hermes session database so the request
    works even when the dashboard requires authentication. Falls back to proxying
    the dashboard HTTP endpoint only when hermes_state is not available.
    """
    try:
        data = _get_models_analytics_db(days=30)
    except RuntimeError as exc:
        # hermes_state unavailable; fall back to dashboard HTTP proxy.
        logging.getLogger(__name__).warning("Direct analytics DB read failed: %s", exc)
        url = _hermes_dashboard_url().rstrip("/") + "/api/analytics/models"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as http_exc:
            raise HTTPException(
                status_code=http_exc.code,
                detail=http_exc.read().decode("utf-8", errors="ignore"),
            ) from http_exc
        except Exception as proxy_exc:
            raise HTTPException(status_code=500, detail=str(proxy_exc)) from proxy_exc
    if _user_is_free_only(current_user):
        return filter_analytics_models_for_free_user(data)
    return data


@app.get("/v1/model")
async def get_model(
    chat_id: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Get the currently selected model/provider. Optionally for a live chat session."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Model info requires gateway backend")
    loop = asyncio.get_running_loop()
    if chat_id:
        _verify_chat_access(chat_id, current_user)
        hermes_sid = store.get_hermes_session_id(chat_id)
        gw = sessions.get_or_create(chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    else:
        gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)  # type: ignore[attr-defined]
    try:
        result = await loop.run_in_executor(None, gw.current_model)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/v1/model")
async def set_model(request: ModelSwitchRequest, current_user: dict = Depends(get_current_user)) -> dict:
    """Switch the model for a chat session."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Model switch requires gateway backend")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    if _user_is_free_only(current_user):
        catalog_gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)  # type: ignore[attr-defined]
        try:
            catalog = await loop.run_in_executor(None, catalog_gw.model_options)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not model_allowed_for_free_user(request.model, request.provider or "", catalog):
            raise HTTPException(
                status_code=403,
                detail=(
                    "This account is limited to free models "
                    "(and profiles whose names end with :free)."
                ),
            )
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    value = request.model
    if request.provider:
        value = f"{value} --provider {request.provider}"
    try:
        result = await loop.run_in_executor(None, gw.set_model, value, request.confirm_expensive_model)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# Gateway backend
# ---------------------------------------------------------------------------

async def _chat_gateway(request: ChatRequest, loop: asyncio.AbstractEventLoop) -> StreamingResponse:
    hermes_sid = store.get_hermes_session_id(request.chat_id)
    gw = sessions.get_or_create(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]

    pending = gw.get_pending_gate()
    if pending is not None:
        gate_kind = pending.get("gate_kind", "approval")
        gate_id = pending.get("gate_id", "")
        options = pending.get("options", [])
        choice = request.message.strip()
        if options:
            matched = _resolve_gate_choice(options, choice)
            if matched is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Chat {request.chat_id} has an unresolved {gate_kind} gate "
                        f"({gate_id}). Reply with one of {options} or call /v1/gate/resolve."
                    ),
                )
            choice = matched
        gw.set_pending_gate(None)
        try:
            await loop.run_in_executor(None, gw.respond_gate, gate_kind, gate_id, choice)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return StreamingResponse(_sse_stream_gateway(gw), media_type="text/event-stream")

    try:
        await loop.run_in_executor(None, gw.submit, request.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(_sse_stream_gateway(gw), media_type="text/event-stream")


async def _sse_stream_gateway(gw) -> AsyncGenerator[bytes, None]:
    """Translate tui_gateway JSON-RPC frames into SSE events for the web UI."""
    while True:
        frame = await gw.queue.get()
        event = _translate_event(frame)

        if event is None:
            if "error" in frame:
                event = {"type": "error", "message": frame["error"].get("message", "RPC error")}
            else:
                continue

        yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

        etype = event.get("type", "")

        if etype == "gate_interrupt":
            gw.set_pending_gate(event)
            if gw.hermes_session_id:
                store.set_hermes_session_id(gw.chat_id, gw.hermes_session_id)
            return

        if etype == "turn_complete":
            if gw.hermes_session_id:
                store.set_hermes_session_id(gw.chat_id, gw.hermes_session_id)
            return

        if etype == "error":
            return


# ---------------------------------------------------------------------------
# Legacy PTY backend (fallback)
# ---------------------------------------------------------------------------

async def _chat_pty(request: ChatRequest, loop: asyncio.AbstractEventLoop) -> StreamingResponse:
    hermes_sid, _created = store.get_or_create_hermes_session_id(request.chat_id)
    session = sessions.get_or_start(request.chat_id, hermes_sid, loop)  # type: ignore[attr-defined]

    pending_gate = session.get_pending_gate()
    if pending_gate is not None:
        matched = _resolve_gate_choice(pending_gate.options, request.message.strip())
        if matched is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Chat {request.chat_id} has an unresolved gate "
                    f"({pending_gate.gate_id}). Reply with one of "
                    f"{pending_gate.options} or call /v1/gate/resolve."
                ),
            )
        await loop.run_in_executor(None, session.resolve_gate, pending_gate.gate_id, matched)
        return StreamingResponse(_sse_stream_pty(session), media_type="text/event-stream")

    try:
        await loop.run_in_executor(None, session.send_text, request.message)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(_sse_stream_pty(session), media_type="text/event-stream")


async def _sse_stream_pty(session) -> AsyncGenerator[bytes, None]:
    while True:
        event = await session.queue.get()
        yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
        if event.get("type") in ("gate_interrupt", "process_exit"):
            return


# ---------------------------------------------------------------------------
# Gate resolve (both backends)
# ---------------------------------------------------------------------------

@app.post("/v1/gate/resolve")
async def resolve_gate(request: GateResolveRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not request.gate_id.strip():
        raise HTTPException(status_code=400, detail="gate_id is required")
    if not request.choice.strip():
        raise HTTPException(status_code=400, detail="choice is required")
    _verify_chat_access(request.chat_id, current_user)
    loop = asyncio.get_running_loop()
    session = sessions.get(request.chat_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No active session for chat {request.chat_id}")

    try:
        if _BACKEND == "gateway" and runs is not None:
            run = runs.get_active(request.chat_id, current_user["user_id"])
            if run is not None:
                options = (run.pending_gate or {}).get("options", [])
                choice = _resolve_gate_choice(options, request.choice) if options else request.choice
                if choice is None:
                    raise ValueError(f"Reply with one of {options}")
                await runs.resolve_gate(
                    chat_id=request.chat_id,
                    user_id=current_user["user_id"],
                    gate_id=request.gate_id,
                    gate_kind=request.gate_kind,
                    choice=choice,
                )
                return {"status": "resolved", "gate_id": request.gate_id, "run_id": run.run_id}
            session.set_pending_gate(None)  # type: ignore[attr-defined]
            await loop.run_in_executor(
                None, session.respond_gate, request.gate_kind, request.gate_id, request.choice  # type: ignore[attr-defined]
            )
        elif _BACKEND == "gateway":
            session.set_pending_gate(None)  # type: ignore[attr-defined]
            await loop.run_in_executor(
                None, session.respond_gate, request.gate_kind, request.gate_id, request.choice  # type: ignore[attr-defined]
            )
        else:
            await loop.run_in_executor(None, session.resolve_gate, request.gate_id, request.choice)  # type: ignore[attr-defined]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "resolved", "gate_id": request.gate_id}


@app.post("/v1/chat/drain")
async def drain(request: ChatRequest, current_user: dict = Depends(get_current_user)) -> StreamingResponse:
    """Resume streaming output after a gate was resolved out-of-band."""
    if not request.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    _verify_chat_access(request.chat_id, current_user)
    session = sessions.get(request.chat_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No active session for chat {request.chat_id}")
    if _BACKEND == "gateway":
        return StreamingResponse(_sse_stream_gateway(session), media_type="text/event-stream")  # type: ignore[arg-type]
    return StreamingResponse(_sse_stream_pty(session), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# WebSocket passthrough — raw tui_gateway JSON-RPC
# ---------------------------------------------------------------------------

@app.websocket("/api/ws")
async def ws_gateway(ws: WebSocket) -> None:
    """
    Raw tui_gateway JSON-RPC WebSocket passthrough.

    Lets the web UI speak the full tui_gateway protocol directly —
    session management, slash commands, model switching, approvals, etc.
    Only available when the gateway backend is active.
    """
    if _BACKEND != "gateway":
        await ws.accept()
        await ws.close(code=1011, reason="tui_gateway not available — PTY backend active")
        return
    try:
        from tui_gateway.ws import handle_ws
        await handle_ws(ws)
    except Exception as exc:
        logger.exception("ws_gateway error: %s", exc)


# --------------------------------------------------------------------------- #
# Chat history endpoints for the standalone web UI
# --------------------------------------------------------------------------- #


def _seed_local_messages_from_hermes(
    chat_id: str,
    user_id: Optional[str],
    hermes_messages: list[dict],
) -> int:
    """Populate an empty local cache from a Hermes session.resume transcript.

    Hermes is the agent SoT; our SQLite is a display/edit cache that needs
    stable numeric ids. Only user/assistant turns are seeded.
    """
    seeded = 0
    for msg in hermes_messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = msg.get("text") or msg.get("content") or ""
        if not str(text).strip() and role == "assistant":
            continue
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or None
        history.add_message(
            chat_id,
            user_id,
            role,
            str(text),
            reasoning=str(reasoning) if reasoning else None,
        )
        seeded += 1
    return seeded


@app.get("/api/chats")
async def list_chats(current_user: dict = Depends(get_current_user)) -> list[dict]:
    return history.list_chats(current_user["user_id"])


@app.post("/api/chats")
async def create_chat(request: CreateChatRequest, current_user: dict = Depends(get_current_user)) -> dict:
    chat_id = str(uuid.uuid4())
    history.create_chat(chat_id, current_user["user_id"], request.title)
    return {"chat_id": chat_id, "title": request.title}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str, current_user: dict = Depends(get_current_user)) -> dict:
    chat = history.get_chat(chat_id, current_user["user_id"])
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@app.patch("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, request: RenameChatRequest, current_user: dict = Depends(get_current_user)) -> dict:
    chat = history.get_chat(chat_id, current_user["user_id"])
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    if request.title is not None:
        history.rename_chat(chat_id, current_user["user_id"], request.title)
        # Keep Hermes state.db title in sync when this chat is bound to a session.
        if _BACKEND == "gateway":
            hermes_sid = store.get_hermes_session_id(chat_id)
            if hermes_sid:
                loop = asyncio.get_running_loop()
                gw = sessions.get_or_create(chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
                try:
                    await loop.run_in_executor(None, gw.set_title, request.title)
                except Exception as exc:
                    logger.warning("chat_id=%s session.title failed: %s", chat_id, exc)
    if request.pinned is not None:
        history.pin_chat(chat_id, current_user["user_id"], request.pinned)
    updated = history.get_chat(chat_id, current_user["user_id"])
    return updated or chat


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.delete_chat(chat_id, current_user["user_id"])
    store.delete(chat_id)
    if sessions is not None:
        sessions.remove(chat_id)
    return {"deleted": chat_id}


@app.get("/v1/hermes-sessions")
async def list_hermes_sessions(
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """List Hermes sessions available to import (TUI / CLI / desktop / …).

    Excludes ``hermes-chat`` sessions we created ourselves. Already-imported
    sessions are included with ``imported: true`` + ``chat_id`` so the picker
    can grey them out or jump to the existing chat.
    """
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Import requires gateway backend")
    loop = asyncio.get_running_loop()
    gw = sessions.get_or_create(_MODEL_CATALOG_CHAT_ID, None, loop)  # type: ignore[attr-defined]
    try:
        raw = await loop.run_in_executor(None, gw.session_list, limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bound = store.list_bound_hermes_session_ids()
    user_id = current_user["user_id"]
    out: list[dict] = []
    for s in raw:
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        source = str(s.get("source") or "").strip().lower()
        if source == HERMES_CHAT_SOURCE.lower():
            continue
        chat_id = bound.get(sid)
        imported = False
        if chat_id:
            chat = history.get_chat(chat_id, user_id)
            if chat is None:
                # Orphan binding (deleted chat / other user) — allow re-import.
                store.delete(chat_id)
                chat_id = None
            else:
                imported = True
        out.append({
            "id": sid,
            "title": str(s.get("title") or "").strip() or "Untitled session",
            "preview": str(s.get("preview") or "").strip(),
            "started_at": s.get("started_at") or 0,
            "message_count": int(s.get("message_count") or 0),
            "source": source or "unknown",
            "imported": imported,
            "chat_id": chat_id if imported else None,
        })
    return {"sessions": out}


class ImportHermesSessionRequest(BaseModel):
    session_id: str


@app.post("/v1/hermes-sessions/import")
async def import_hermes_session(
    request: ImportHermesSessionRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Resume a Hermes session and bind it to a new (or existing) hermes-chat."""
    if _BACKEND != "gateway":
        raise HTTPException(status_code=400, detail="Import requires gateway backend")
    session_id = (request.session_id or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    user_id = current_user["user_id"]

    def _existing_for(sid: str) -> Optional[dict]:
        bound_id = store.find_chat_id_by_hermes_session_id(sid)
        if not bound_id:
            return None
        chat = history.get_chat(bound_id, user_id)
        if chat is None:
            store.delete(bound_id)
            return None
        return {
            "chat_id": bound_id,
            "title": chat.get("title") or "Imported chat",
            "already_imported": True,
            "hermes_session_id": sid,
            "message_count": len(history.get_messages(bound_id, user_id)),
        }

    already = _existing_for(session_id)
    if already:
        return already

    loop = asyncio.get_running_loop()
    chat_id = str(uuid.uuid4())
    history.create_chat(chat_id, user_id, "Importing…")
    gw = sessions.get_or_create(chat_id, session_id, loop)  # type: ignore[attr-defined]
    try:
        resumed = await loop.run_in_executor(None, gw.session_resume, session_id)
    except RuntimeError as exc:
        history.delete_chat(chat_id, user_id)
        store.delete(chat_id)
        sessions.remove(chat_id)  # type: ignore[attr-defined]
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    tip = str(resumed.get("session_id") or gw.hermes_session_id or session_id)
    tip_existing = _existing_for(tip) if tip != session_id else None
    if tip_existing:
        history.delete_chat(chat_id, user_id)
        store.delete(chat_id)
        sessions.remove(chat_id)  # type: ignore[attr-defined]
        return tip_existing

    info = resumed.get("info") if isinstance(resumed.get("info"), dict) else {}
    title = (
        (info or {}).get("title")
        or (info or {}).get("display_name")
        or resumed.get("title")
        or ""
    )
    title = str(title).strip() or "Imported session"
    source = str((info or {}).get("source") or resumed.get("source") or "").strip()
    if source:
        title_prefix = {
            "tui": "TUI",
            "api_server": "API",
            "cli": "CLI",
            "desktop": "Desktop",
            "acp": "ACP",
        }.get(source.lower(), source)
        if not title.lower().startswith(f"{title_prefix.lower()}:"):
            title = f"{title_prefix}: {title}"

    history.rename_chat(chat_id, user_id, title)
    store.set_hermes_session_id(chat_id, tip)

    hermes_messages = resumed.get("messages") or []
    seeded = _seed_local_messages_from_hermes(chat_id, user_id, hermes_messages)
    logger.info(
        "imported hermes session %s → chat_id=%s tip=%s seeded=%d",
        session_id, chat_id, tip, seeded,
    )
    return {
        "chat_id": chat_id,
        "title": title,
        "already_imported": False,
        "hermes_session_id": tip,
        "message_count": seeded,
        "source": source or None,
    }


@app.get("/api/chats/{chat_id}/messages")
async def get_messages(chat_id: str, current_user: dict = Depends(get_current_user)) -> list[dict]:
    user_id = current_user["user_id"]
    if history.get_chat(chat_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")

    local = history.get_messages(chat_id, user_id)
    if _BACKEND != "gateway":
        return local

    hermes_sid = store.get_hermes_session_id(chat_id)
    if not hermes_sid:
        return local

    # Resume into this gateway process so Hermes remains SoT across restarts.
    loop = asyncio.get_running_loop()
    gw = sessions.get_or_create(chat_id, hermes_sid, loop)  # type: ignore[attr-defined]
    try:
        resumed = await loop.run_in_executor(None, gw.session_resume, hermes_sid)
    except Exception as exc:
        logger.warning("chat_id=%s session.resume on load failed: %s", chat_id, exc)
        return local

    tip = resumed.get("session_id") or gw.hermes_session_id
    if tip and tip != hermes_sid:
        store.set_hermes_session_id(chat_id, str(tip))

    info = resumed.get("info") if isinstance(resumed.get("info"), dict) else {}
    hermes_title = (
        (info or {}).get("title")
        or (info or {}).get("display_name")
        or resumed.get("title")
        or ""
    )
    if isinstance(hermes_title, str) and hermes_title.strip():
        chat = history.get_chat(chat_id, user_id)
        if chat and chat.get("title") in (None, "", "New chat"):
            history.rename_chat(chat_id, user_id, hermes_title.strip())

    hermes_messages = resumed.get("messages") or []
    if not local and hermes_messages:
        seeded = _seed_local_messages_from_hermes(chat_id, user_id, hermes_messages)
        if seeded:
            logger.info(
                "chat_id=%s seeded %d local messages from Hermes session %s",
                chat_id, seeded, tip or hermes_sid,
            )
            local = history.get_messages(chat_id, user_id)

    return local


@app.post("/api/chats/{chat_id}/messages")
async def save_message(chat_id: str, request: SaveMessageRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    message_id = history.add_message(
        chat_id,
        current_user["user_id"],
        request.role,
        request.content,
        images=request.images or None,
        tool_steps=request.tool_steps or None,
        reasoning=request.reasoning or None,
    )
    return {"id": message_id, "role": request.role, "content": request.content}


@app.put("/api/chats/{chat_id}/messages/{message_id}")
async def update_message(chat_id: str, message_id: int, request: UpdateMessageRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.update_message(
        message_id,
        current_user["user_id"],
        request.content,
        tool_steps=request.tool_steps or None,
        # Pass through only when the client included the field (incl. "").
        reasoning=request.reasoning if "reasoning" in request.model_fields_set else None,
    )
    return {"id": message_id, "content": request.content}


@app.get("/api/chats/{chat_id}/usage")
async def get_chat_usage(chat_id: str, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    payload = history.get_chat_usage(chat_id, current_user["user_id"]) or {}
    context_window = payload.pop("_context_window", None)
    return {"usage": payload, "context_window": context_window}


@app.put("/api/chats/{chat_id}/usage")
async def save_chat_usage(chat_id: str, request: UsageSaveRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    payload = {**request.usage}
    if request.context_window:
        payload["_context_window"] = request.context_window
    history.set_chat_usage(chat_id, current_user["user_id"], payload)
    return {"status": "ok"}


@app.put("/api/chats/{chat_id}/messages/{message_id}/usage")
async def save_message_usage(chat_id: str, message_id: int, request: UsageSaveRequest, current_user: dict = Depends(get_current_user)) -> dict:
    if history.get_chat(chat_id, current_user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    history.update_message_usage(message_id, current_user["user_id"], request.usage)
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Voice support: speech-to-text and text-to-speech
# --------------------------------------------------------------------------- #

class SpeakRequest(BaseModel):
    text: str
    lang: str | None = None
    voice: str | None = None


@app.get("/api/plugins/hermes-chat/voice-config")
async def get_voice_config(current_user: dict = Depends(get_current_user)) -> dict:
    cfg = load_config()
    return {
        "voice_enabled": cfg.voice_enabled,
        "default_tts_voice": cfg.default_tts_voice,
        "tts_available": importlib.util.find_spec("edge_tts") is not None,
        "stt_available": (
            importlib.util.find_spec("faster_whisper") is not None
            and importlib.util.find_spec("imageio_ffmpeg") is not None
        ),
    }


@app.post("/v1/audio/transcribe")
async def transcribe_audio(file: UploadFile, current_user: dict = Depends(get_current_user)) -> dict:
    """Transcribe uploaded audio (webm/opus from browser) to text via Whisper."""
    if not load_config().voice_enabled:
        raise HTTPException(status_code=503, detail="Voice is disabled in settings.")
    import shutil
    import tempfile

    suffix = "." + (file.filename or "audio.webm").rsplit(".", 1)[-1] if "." in (file.filename or "") else ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        from .voice import transcribe

        text = transcribe(tmp_path)
        return {"text": text}
    except ImportError as e:
        logger.error("Voice dependencies not installed: %s", e)
        raise HTTPException(status_code=503, detail="Voice support not installed. Install dependencies via the dashboard → Install Voice button.")
    except Exception as e:
        logger.error("Transcription failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/v1/audio/speak")
async def speak_text(request: SpeakRequest, current_user: dict = Depends(get_current_user)) -> FileResponse:
    """Synthesize speech from text via Piper TTS. Returns a wav audio file."""
    if not load_config().voice_enabled:
        raise HTTPException(status_code=503, detail="Voice is disabled in settings.")
    if request.voice is None:
        request.voice = load_config().default_tts_voice
    try:
        from .voice import synthesize

        wav_path = await synthesize(request.text, lang=request.lang, voice=request.voice)
        media_type = "audio/mpeg" if str(wav_path).endswith(".mp3") else "audio/wav"
        filename = "speech.mp3" if str(wav_path).endswith(".mp3") else "speech.wav"
        return FileResponse(
            str(wav_path),
            media_type=media_type,
            filename=filename,
            background=None,
        )
    except ImportError as e:
        logger.error("Voice dependency import failed: %s", e)
        raise HTTPException(status_code=503, detail=f"Voice dependency not available: {e}. Try reinstalling via the dashboard → Install Voice button.")
    except Exception as e:
        logger.error("TTS failed: %s", e)
        raise HTTPException(status_code=500, detail=f"TTS failed: {e}")
