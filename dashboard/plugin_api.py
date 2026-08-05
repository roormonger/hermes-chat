"""Dashboard backend API for Hermes Chat.

Mounted by Hermes under ``/api/plugins/hermes-chat/``. All routes here run
inside the Hermes dashboard server process and manipulate the Hermes Chat daemon via
``hermes_chat.daemon``.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

# Make sure the plugin root is importable regardless of how Hermes loads this file.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_ERROR_LOG = _PLUGIN_ROOT / "run" / "dashboard-api-error.log"

from hermes_chat.analytics import get_models_analytics as _get_models_analytics
from hermes_chat.config import auth_secret, load_config, update_config
from hermes_chat.daemon import LOG_FILE, is_running, logs, restart, start, status, stop
from hermes_chat.dependencies import check_dependencies, check_optional_dependencies, install_dependencies

_DEFAULT_HERMES_DASHBOARD_URL = "http://127.0.0.1:9119"


def _hermes_dashboard_url() -> dict:
    """Return the resolved Hermes dashboard URL and its source."""
    cfg = load_config()
    if cfg.hermes_dashboard_url:
        return {"url": cfg.hermes_dashboard_url, "source": "config"}
    env_url = os.environ.get("HERMES_DASHBOARD_URL")
    if env_url:
        return {"url": env_url, "source": "HERMES_DASHBOARD_URL"}
    env_port = os.environ.get("HERMES_DASHBOARD_PORT")
    if env_port:
        return {"url": f"http://127.0.0.1:{env_port}", "source": "HERMES_DASHBOARD_PORT"}
    return {"url": _DEFAULT_HERMES_DASHBOARD_URL, "source": "default"}


def _verify_dashboard_url(url: str) -> dict:
    """Verify by reading the Hermes session DB directly.

    We avoid making an HTTP request to the dashboard because the dashboard
    routes require authentication. Hermes Chat and this plugin run on the same
    host as Hermes, so we can read the underlying SQLite database directly.
    """
    try:
        data = _get_models_analytics(days=30)
        models = data.get("models") or []
        return {"ok": True, "model_count": len(models), "url": url}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}

router = APIRouter()


def _handle_exc(exc: Exception) -> None:
    """Log the full traceback and raise a 500 with the error summary."""
    _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    _ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
    raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


class ConfigUpdate(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    hermes_bin: Optional[str] = None
    session_idle_timeout: Optional[float] = None
    gate_idle_threshold: Optional[float] = None
    log_level: Optional[str] = None
    debug: Optional[bool] = None
    auto_start: Optional[bool] = None
    hermes_dashboard_url: Optional[str] = None
    voice_enabled: Optional[bool] = None
    suggestions_enabled: Optional[bool] = None
    suggestions_interval_minutes: Optional[int] = None
    suggestions_pool_size: Optional[int] = None
    suggestions_show_count: Optional[int] = None
    suggestions_model: Optional[str] = None
    suggestions_provider: Optional[str] = None
    restart: bool = False


class SuggestionsPromptUpdate(BaseModel):
    content: str


class SuggestionsPromptRestore(BaseModel):
    source: str = "github"  # github | bundle


@router.get("/status")
async def get_status() -> dict:
    try:
        return status()
    except Exception as exc:
        _handle_exc(exc)


@router.post("/start")
async def post_start() -> dict:
    try:
        return start()
    except Exception as exc:
        _handle_exc(exc)


@router.post("/stop")
async def post_stop() -> dict:
    try:
        return stop()
    except Exception as exc:
        _handle_exc(exc)


@router.post("/restart")
async def post_restart() -> dict:
    try:
        return restart()
    except Exception as exc:
        _handle_exc(exc)


@router.get("/config")
async def get_config() -> dict:
    try:
        return load_config().to_dict()
    except Exception as exc:
        _handle_exc(exc)


@router.get("/voice-config")
async def get_voice_config() -> dict:
    """Voice availability, sourced from Hermes' own STT/TTS providers."""
    try:
        from hermes_chat.voice import voice_status

        cfg = load_config()
        return {"voice_enabled": cfg.voice_enabled, **voice_status()}
    except Exception as exc:
        _handle_exc(exc)


@router.post("/config")
async def post_config(body: ConfigUpdate) -> dict:
    try:
        updates = {k: v for k, v in body.model_dump().items() if v is not None and k != "restart"}
        cfg = update_config(updates)
        result = {"status": "configured", "config": cfg.to_dict()}
        if body.restart:
            result.update(restart())
        return result
    except Exception as exc:
        _handle_exc(exc)


@router.get("/logs")
async def get_logs(tail: int = 100) -> dict:
    try:
        return {"lines": tail, "log": logs(tail)}
    except Exception as exc:
        _handle_exc(exc)


@router.get("/logs/download")
async def download_logs():
    try:
        if not LOG_FILE.exists():
            return PlainTextResponse("No log file found.", status_code=404)
        return FileResponse(
            path=str(LOG_FILE),
            media_type="text/plain",
            filename="hermes-chat.log",
            headers={"Content-Disposition": 'attachment; filename="hermes-chat.log"'},
        )
    except Exception as exc:
        _handle_exc(exc)


@router.get("/deps")
async def get_deps() -> dict:
    try:
        missing = check_dependencies()
        missing_optional = check_optional_dependencies()
        return {"missing": missing, "ok": not missing, "missing_optional": missing_optional}
    except Exception as exc:
        _handle_exc(exc)


@router.get("/dashboard-url")
async def get_dashboard_url() -> dict:
    """Return the auto-discovered Hermes dashboard URL and a verification result."""
    try:
        info = _hermes_dashboard_url()
        info["verify"] = _verify_dashboard_url(info["url"])
        return info
    except Exception as exc:
        _handle_exc(exc)


@router.post("/dashboard-url/verify")
async def post_verify_dashboard_url(body: dict) -> dict:
    """Verify an arbitrary Hermes dashboard URL."""
    try:
        url = body.get("url", "")
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        return _verify_dashboard_url(url)
    except HTTPException:
        raise
    except Exception as exc:
        _handle_exc(exc)


@router.post("/install-deps")
async def post_install_deps() -> dict:
    try:
        return install_dependencies(auto=True)
    except Exception as exc:
        _handle_exc(exc)


class CreateUserRequest(BaseModel):
    username: str
    password: str
    free_models_only: bool = False


class UpdateUserRequest(BaseModel):
    free_models_only: bool


def _user_store() -> UserStore:
    """Lazy-load UserStore so missing bcrypt doesn't break dashboard import."""
    from hermes_chat.users import UserStore

    return UserStore(secret=auth_secret())


@router.get("/users")
async def get_users() -> dict:
    try:
        store = _user_store()
        return {"users": store.list_users()}
    except Exception as exc:
        _handle_exc(exc)


@router.post("/users")
async def post_user(body: CreateUserRequest) -> dict:
    try:
        store = _user_store()
        user = store.create_user(
            body.username,
            body.password,
            free_models_only=body.free_models_only,
        )
        return {"status": "created", "user": user}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _handle_exc(exc)


@router.patch("/users/{user_id}")
async def patch_user(user_id: str, body: UpdateUserRequest) -> dict:
    try:
        store = _user_store()
        user = store.set_free_models_only(user_id, body.free_models_only)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return {"status": "updated", "user": user}
    except HTTPException:
        raise
    except Exception as exc:
        _handle_exc(exc)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str) -> dict:
    try:
        store = _user_store()
        deleted = store.delete_user(user_id)
        return {"status": "deleted" if deleted else "not_found", "user_id": user_id}
    except Exception as exc:
        _handle_exc(exc)


def _flatten_catalog_models(catalog: dict) -> list[dict]:
    from hermes_chat.model_access import is_catalog_model_free

    options: list[dict] = []
    for provider in catalog.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        slug = str(provider.get("slug") or provider.get("id") or "")
        provider_name = str(provider.get("name") or slug or "Unknown")
        for model in provider.get("models") or []:
            if isinstance(model, dict):
                mid = str(model.get("id") or model.get("model") or "")
                name = str(model.get("name") or mid)
            else:
                mid = str(model)
                name = mid
            if not mid:
                continue
            options.append(
                {
                    "id": mid,
                    "name": name,
                    "provider": slug,
                    "provider_name": provider_name,
                    "is_profile": False,
                    "free": is_catalog_model_free(provider, mid),
                }
            )
    return options


def _flatten_profile_models(analytics: dict) -> list[dict]:
    options: list[dict] = []
    for model in analytics.get("models") or []:
        if not isinstance(model, dict):
            continue
        mid = str(model.get("model") or "")
        if not mid:
            continue
        provider = str(model.get("provider") or "")
        options.append(
            {
                "id": mid,
                "name": mid.split("/")[-1] or mid,
                "provider": provider,
                "provider_name": "Hermes Profiles",
                "is_profile": True,
                "free": mid.lower().endswith(":free"),
            }
        )
    return options


async def _load_gateway_catalog() -> dict:
    """Load model.options via in-process tui_gateway (Hermes dashboard process)."""
    import asyncio

    from hermes_chat.gateway_session import GatewaySessionManager, gateway_available, gateway_available_error

    if not gateway_available():
        raise RuntimeError(gateway_available_error() or "tui_gateway not available")
    loop = asyncio.get_running_loop()
    mgr = GatewaySessionManager(session_idle_timeout=120.0)
    chat_id = "__dashboard_suggestion_models__"
    gw = mgr.get_or_create(chat_id, None, loop)
    try:
        return await loop.run_in_executor(None, gw.model_options)
    finally:
        try:
            mgr.remove(chat_id)
        except Exception:
            pass


def _load_daemon_catalog() -> dict:
    """Fall back to the running hermes-chat daemon /v1/models with a short-lived JWT."""
    cfg = load_config()
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    store = _user_store()
    users = store.list_users()
    if not users:
        raise RuntimeError("Chat daemon proxy needs at least one chat user to mint a token")
    token = store.create_token(users[0]["user_id"])
    url = f"http://{host}:{cfg.port}/v1/models"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


@router.get("/suggestions/models")
async def get_suggestion_models() -> dict:
    """Dynamic Hermes model list for the suggestion-engine dropdown."""
    try:
        options: list[dict] = []
        source = "none"
        errors: list[str] = []

        try:
            analytics = _get_models_analytics(days=30)
            options.extend(_flatten_profile_models(analytics))
        except Exception as exc:
            errors.append(f"profiles: {exc}")

        catalog = None
        try:
            catalog = await _load_gateway_catalog()
            source = "gateway"
        except Exception as gw_exc:
            errors.append(f"gateway: {gw_exc}")
            try:
                catalog = _load_daemon_catalog()
                source = "daemon"
            except Exception as daemon_exc:
                errors.append(f"daemon: {daemon_exc}")

        if catalog:
            options.extend(_flatten_catalog_models(catalog))

        # Stable de-dupe: prefer first occurrence (profiles listed first).
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for opt in options:
            key = (opt.get("id") or "", opt.get("provider") or "")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(opt)

        return {
            "options": deduped,
            "source": source,
            "ok": bool(deduped),
            "errors": errors,
        }
    except Exception as exc:
        _handle_exc(exc)


@router.get("/suggestions/prompt")
async def get_suggestions_prompt() -> dict:
    try:
        from hermes_chat.suggestions import (
            SUGGESTIONS_GITHUB_RAW,
            ensure_suggestions_prompt,
            read_suggestions_prompt,
            suggestions_prompt_path,
        )

        ensure_suggestions_prompt()
        return {
            "content": read_suggestions_prompt(),
            "path": str(suggestions_prompt_path()),
            "github_url": SUGGESTIONS_GITHUB_RAW,
        }
    except Exception as exc:
        _handle_exc(exc)


@router.put("/suggestions/prompt")
async def put_suggestions_prompt(body: SuggestionsPromptUpdate) -> dict:
    try:
        from hermes_chat.suggestions import write_suggestions_prompt

        if not body.content.strip():
            raise HTTPException(status_code=400, detail="Prompt content cannot be empty")
        write_suggestions_prompt(body.content)
        return {"status": "saved"}
    except HTTPException:
        raise
    except Exception as exc:
        _handle_exc(exc)


@router.post("/suggestions/prompt/restore")
async def post_restore_suggestions_prompt(body: SuggestionsPromptRestore) -> dict:
    try:
        from hermes_chat.suggestions import (
            restore_suggestions_prompt_from_bundle,
            restore_suggestions_prompt_from_github,
        )

        source = (body.source or "github").strip().lower()
        if source == "bundle":
            content = restore_suggestions_prompt_from_bundle()
        elif source == "github":
            try:
                content = restore_suggestions_prompt_from_github()
            except Exception as github_exc:
                # Offline / GitHub unreachable → bundled default.
                content = restore_suggestions_prompt_from_bundle()
                return {
                    "status": "restored",
                    "source": "bundle",
                    "content": content,
                    "warning": f"GitHub restore failed ({github_exc}); used bundled default.",
                }
        else:
            raise HTTPException(status_code=400, detail="source must be 'github' or 'bundle'")
        return {"status": "restored", "source": source, "content": content}
    except HTTPException:
        raise
    except Exception as exc:
        _handle_exc(exc)


@router.get("/suggestions/status")
async def get_suggestions_status() -> dict:
    try:
        from hermes_chat.suggestions import SuggestionStore

        cfg = load_config()
        store = SuggestionStore()
        users = _user_store().list_users()
        pools = []
        for user in users:
            meta = store.get_meta(user["user_id"])
            pool = store.get_pool(user["user_id"])
            pools.append(
                {
                    "user_id": user["user_id"],
                    "username": user.get("username"),
                    "count": len(pool),
                    "mode": (meta or {}).get("mode"),
                    "updated_at": (meta or {}).get("updated_at"),
                }
            )
        return {
            "enabled": cfg.suggestions_enabled,
            "interval_minutes": cfg.suggestions_interval_minutes,
            "pool_size": cfg.suggestions_pool_size,
            "show_count": cfg.suggestions_show_count,
            "model": cfg.suggestions_model,
            "provider": cfg.suggestions_provider,
            "pools": pools,
        }
    except Exception as exc:
        _handle_exc(exc)


def _daemon_base_url() -> str:
    cfg = load_config()
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    return f"http://{host}:{cfg.port}"


@router.post("/suggestions/refresh")
async def post_suggestions_refresh() -> dict:
    """Ask the hermes-chat daemon to force-regenerate all suggestion pools."""
    try:
        if not is_running():
            raise HTTPException(
                status_code=400,
                detail="Hermes Chat daemon is not running. Start it, then try again.",
            )
        url = _daemon_base_url().rstrip("/") + "/v1/suggestions/refresh"
        req = urllib.request.Request(
            url,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as http_exc:
            detail = http_exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("detail") or detail
            except Exception:
                pass
            raise HTTPException(status_code=http_exc.code, detail=str(detail)) from http_exc
    except HTTPException:
        raise
    except Exception as exc:
        _handle_exc(exc)
