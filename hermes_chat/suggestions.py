"""Background starter-suggestion pools for chat UI users."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .config import _plugin_dir, data_dir, load_config
from .gateway_session import _translate_event
from .model_access import is_catalog_model_free

logger = logging.getLogger("hermes_chat.suggestions")

SUGGESTIONS_GITHUB_RAW = (
    "https://raw.githubusercontent.com/roormonger/hermes-chat/main/suggestions.md.default"
)

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")

STATIC_SUGGESTIONS: list[dict[str, str]] = [
    {
        "title": "List files",
        "label": "in the current directory",
        "prompt": "What files are in the current directory?",
    },
    {
        "title": "Running processes",
        "label": "show what's active",
        "prompt": "Show me running processes",
    },
    {
        "title": "What can Hermes do?",
        "label": "quick overview",
        "prompt": "Summarize what Hermes can do",
    },
    {
        "title": "System info",
        "label": "OS, CPU, memory",
        "prompt": "Give me a brief summary of this machine (OS, CPU, memory, disk).",
    },
]


def plugin_root() -> Path:
    plugin = _plugin_dir()
    if plugin is not None:
        return plugin
    return Path(__file__).resolve().parent.parent


def suggestions_prompt_path() -> Path:
    return plugin_root() / "suggestions.md"


def suggestions_default_path() -> Path:
    return plugin_root() / "suggestions.md.default"


def ensure_suggestions_prompt() -> Path:
    """Copy bundled default prompt into the plugin dir if missing."""
    dest = suggestions_prompt_path()
    if dest.exists():
        return dest
    src = suggestions_default_path()
    if src.exists():
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("Created suggestions prompt at %s", dest)
    return dest


def read_suggestions_prompt() -> str:
    ensure_suggestions_prompt()
    path = suggestions_prompt_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    default = suggestions_default_path()
    if default.exists():
        return default.read_text(encoding="utf-8")
    raise FileNotFoundError("suggestions.md not found")


def write_suggestions_prompt(content: str) -> None:
    path = suggestions_prompt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def restore_suggestions_prompt_from_github() -> str:
    """Download the default prompt from GitHub and write it as suggestions.md."""
    req = urllib.request.Request(SUGGESTIONS_GITHUB_RAW, headers={"User-Agent": "hermes-chat"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        content = resp.read().decode("utf-8")
    if not content.strip():
        raise RuntimeError("GitHub returned an empty suggestions prompt")
    write_suggestions_prompt(content)
    try:
        suggestions_default_path().write_text(content, encoding="utf-8")
    except OSError:
        pass
    return content


def restore_suggestions_prompt_from_bundle() -> str:
    """Restore from the shipped suggestions.md.default (offline fallback)."""
    src = suggestions_default_path()
    if not src.exists():
        raise FileNotFoundError("suggestions.md.default not found in plugin")
    content = src.read_text(encoding="utf-8")
    write_suggestions_prompt(content)
    return content


_RETRY_BASE_S = 300.0
_RETRY_MAX_S = 6 * 3600.0


def retry_delay_for(failures: int) -> float:
    """Back off after failed refreshes.

    A refresh spends a real model request, so a persistent failure (exhausted
    daily quota, missing provider key, model that won't emit JSON) must not be
    retried on the worker's normal tick — that burns the user's rate limit on
    attempts that cannot succeed.
    """
    if failures <= 0:
        return 0.0
    return min(_RETRY_MAX_S, _RETRY_BASE_S * (2 ** (failures - 1)))


class SuggestionStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or (data_dir() / "suggestions.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS suggestion_pools (
                user_id TEXT PRIMARY KEY,
                pool_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                mode TEXT NOT NULL DEFAULT 'generic'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS suggestion_failures (
                user_id TEXT PRIMARY KEY,
                failures INTEGER NOT NULL,
                last_attempt_at REAL NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()

    def get_pool(self, user_id: str) -> list[dict[str, str]]:
        row = self._conn().execute(
            "SELECT pool_json FROM suggestion_pools WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return []
        try:
            data = json.loads(row["pool_json"])
            return [x for x in data if isinstance(x, dict) and x.get("prompt")]
        except Exception:
            return []

    def get_meta(self, user_id: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT updated_at, mode FROM suggestion_pools WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def set_pool(self, user_id: str, pool: list[dict[str, str]], mode: str) -> None:
        self._conn().execute(
            """
            INSERT INTO suggestion_pools (user_id, pool_json, updated_at, mode)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                pool_json = excluded.pool_json,
                updated_at = excluded.updated_at,
                mode = excluded.mode
            """,
            (user_id, json.dumps(pool), time.time(), mode),
        )
        self._conn().commit()

    def get_failure(self, user_id: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT failures, last_attempt_at, error FROM suggestion_failures WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def record_failure(self, user_id: str, error: str) -> int:
        """Count a failed attempt and return the new consecutive-failure count."""
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO suggestion_failures (user_id, failures, last_attempt_at, error)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                failures = suggestion_failures.failures + 1,
                last_attempt_at = excluded.last_attempt_at,
                error = excluded.error
            """,
            (user_id, time.time(), error[:500]),
        )
        conn.commit()
        row = conn.execute(
            "SELECT failures FROM suggestion_failures WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["failures"]) if row else 1

    def clear_failure(self, user_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM suggestion_failures WHERE user_id = ?", (user_id,))
        conn.commit()


def normalize_suggestion(item: Any) -> Optional[dict[str, str]]:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    label = str(item.get("label") or item.get("description") or "").strip()
    prompt = str(item.get("prompt") or item.get("message") or "").strip()
    if not prompt:
        return None
    if not title:
        title = " ".join(prompt.split()[:4])
    return {"title": title[:48], "label": label[:80], "prompt": prompt[:500]}


def parse_suggestions_json(text: str) -> list[dict[str, str]]:
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_ARRAY_RE.search(text)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        norm = normalize_suggestion(item)
        if norm:
            out.append(norm)
    return out


def sample_suggestions(pool: list[dict[str, str]], count: int) -> list[dict[str, str]]:
    if count <= 0:
        return []
    if not pool:
        return list(STATIC_SUGGESTIONS[:count])
    if len(pool) <= count:
        return list(pool)
    return random.sample(pool, count)


def build_history_digest(
    history, user_id: str, *, max_chats: int = 5, max_msgs_per_chat: int = 4
) -> str:
    chats = history.list_chats(user_id, limit=max_chats)
    if not chats:
        return ""
    lines: list[str] = []
    for chat in chats:
        title = chat.get("title") or "Untitled"
        lines.append(f"- Chat: {title}")
        messages = history.get_messages(chat["chat_id"], user_id)
        for msg in messages[-max_msgs_per_chat:]:
            role = msg.get("role") or "?"
            content = (msg.get("content") or "").replace("\n", " ").strip()
            if len(content) > 180:
                content = content[:177] + "..."
            if content:
                lines.append(f"  {role}: {content}")
    return "\n".join(lines)


def render_prompt(template: str, *, pool_size: int, mode: str, history: str) -> str:
    if mode == "history":
        mode_instructions = (
            "The user has prior chat history. Prefer actionable Hermes-agent prompts that "
            "continue or relate to their recent topics (files, tools, debugging, system tasks). "
            "Avoid repeating the exact same wording as recent messages."
        )
        history_block = history.strip() or "(no digest available)"
    else:
        mode_instructions = (
            "The user has little or no chat history yet. Generate friendly generic openers: "
            "weather, light current events, fun facts, plus a couple Hermes-capable starters "
            "(files, processes, what Hermes can do, system info)."
        )
        history_block = "(no prior chats)"
    return (
        template.replace("{{POOL_SIZE}}", str(pool_size))
        .replace("{{MODE}}", mode)
        .replace("{{MODE_INSTRUCTIONS}}", mode_instructions)
        .replace("{{HISTORY}}", history_block)
    )


def _pick_free_model(catalog: dict) -> Optional[tuple[str, str]]:
    for provider in catalog.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        slug = str(provider.get("slug") or provider.get("id") or "")
        for model in provider.get("models") or []:
            if isinstance(model, dict):
                mid = str(model.get("id") or model.get("model") or model)
            else:
                mid = str(model)
            if is_catalog_model_free(provider, mid):
                return mid, slug
    return None


def _queue_get_sync(gw, timeout: float):
    loop = gw.loop
    future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(gw.queue.get(), timeout), loop)
    try:
        return future.result(timeout=timeout + 1.0)
    except Exception:
        return None


def _drain_queue_sync(gw) -> None:
    """Drop pending gateway frames; must run drain on the session's event loop."""

    async def _drain() -> None:
        while True:
            try:
                gw.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    try:
        asyncio.run_coroutine_threadsafe(_drain(), gw.loop).result(timeout=2.0)
    except Exception:
        pass


def generate_pool_for_user(
    *,
    user: dict,
    history,
    sessions,
    loop,
) -> tuple[list[dict[str, str]], str]:
    """Run a scratch Hermes turn and return (pool, mode)."""
    cfg = load_config()
    pool_size = max(1, min(int(cfg.suggestions_pool_size), 32))
    digest = build_history_digest(history, user["user_id"])
    mode = "history" if digest.strip() else "generic"
    template = read_suggestions_prompt()
    prompt = render_prompt(template, pool_size=pool_size, mode=mode, history=digest)

    chat_id = f"__suggestions__{user['user_id']}"
    gw = sessions.get_or_create(chat_id, None, loop)

    try:
        if user.get("free_models_only"):
            catalog = gw.model_options()
            picked = _pick_free_model(catalog)
            if picked:
                mid, provider = picked
                value = f"{mid} --provider {provider}" if provider else mid
                gw.ensure_session()
                gw.set_model(value, confirm_expensive_model=True)
        elif cfg.suggestions_model.strip():
            value = cfg.suggestions_model.strip()
            if cfg.suggestions_provider.strip():
                value = f"{value} --provider {cfg.suggestions_provider.strip()}"
            gw.ensure_session()
            gw.set_model(value, confirm_expensive_model=True)
    except Exception as exc:
        logger.warning("suggestions model switch failed for %s: %s", user.get("username"), exc)

    _drain_queue_sync(gw)
    gw.submit(prompt)

    chunks: list[str] = []
    last_note = ""
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        frame = _queue_get_sync(gw, remaining)
        if frame is None:
            continue
        event = _translate_event(frame)
        if event is None:
            if isinstance(frame, dict) and "error" in frame:
                raise RuntimeError(frame["error"].get("message", "suggestion RPC error"))
            continue
        etype = event.get("type")
        if etype == "text":
            chunks.append(event.get("text") or "")
        elif etype == "gate_interrupt":
            try:
                gw.interrupt()
            except Exception:
                pass
            raise RuntimeError("suggestion generation hit a gate; aborted")
        elif etype in ("tool_start", "tool_progress", "tool_generating"):
            try:
                gw.interrupt()
            except Exception:
                pass
            raise RuntimeError("suggestion generation started a tool; aborted")
        elif etype == "turn_complete":
            break
        elif etype in ("error", "process_exit"):
            raise RuntimeError(
                event.get("message") or last_note or "suggestion generation error"
            )
        elif etype == "notification":
            # Provider failures (rate limits, bad keys) arrive here, not as text.
            note = (event.get("text") or "").strip()
            if note:
                last_note = note

    text = "".join(chunks)
    parsed = parse_suggestions_json(text)
    if not parsed:
        if not text.strip():
            raise RuntimeError(
                "model returned no text — "
                + (last_note or "provider error or rate limit?")
            )
        raise RuntimeError(f"could not parse suggestions JSON (got {len(text)} chars)")
    return parsed[:pool_size], mode


class SuggestionWorker:
    """Daemon thread that refreshes per-user suggestion pools on an interval."""

    def __init__(self, *, users, history, sessions, store: SuggestionStore) -> None:
        self.users = users
        self.history = history
        self.sessions = sessions
        self.store = store
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hermes-chat-suggestions", daemon=True
        )
        self._thread.start()
        logger.info("suggestion worker started")

    def stop(self) -> None:
        self._stop.set()

    def refresh_due_users(self, *, force: bool = False) -> dict:
        """Refresh suggestion pools. With force=True, ignore interval and enabled flag."""
        cfg = load_config()
        if not force and not cfg.suggestions_enabled:
            return {"refreshed": [], "failed": [], "skipped": "disabled"}
        if self._loop is None:
            return {"refreshed": [], "failed": [], "skipped": "no_event_loop"}
        interval = max(5, int(cfg.suggestions_interval_minutes)) * 60
        refreshed: list[dict] = []
        failed: list[dict] = []
        for user in self.users.list_users():
            if self._stop.is_set():
                break
            meta = self.store.get_meta(user["user_id"])
            updated = float(meta["updated_at"]) if meta else 0.0
            if (
                not force
                and time.time() - updated < interval
                and self.store.get_pool(user["user_id"])
            ):
                continue
            failure = self.store.get_failure(user["user_id"])
            if not force and failure:
                delay = retry_delay_for(int(failure["failures"]))
                if time.time() - float(failure["last_attempt_at"]) < delay:
                    continue
            try:
                pool, mode = generate_pool_for_user(
                    user=user,
                    history=self.history,
                    sessions=self.sessions,
                    loop=self._loop,
                )
                self.store.set_pool(user["user_id"], pool, mode)
                self.store.clear_failure(user["user_id"])
                logger.info(
                    "refreshed suggestion pool for %s (%s items, mode=%s)",
                    user.get("username"),
                    len(pool),
                    mode,
                )
                refreshed.append(
                    {
                        "user_id": user["user_id"],
                        "username": user.get("username"),
                        "count": len(pool),
                        "mode": mode,
                    }
                )
            except Exception as exc:
                count = self.store.record_failure(user["user_id"], str(exc))
                retry_in = retry_delay_for(count)
                logger.warning(
                    "suggestion refresh failed for %s (%d in a row, next try in ~%d min): %s",
                    user.get("username"),
                    count,
                    int(retry_in // 60),
                    exc,
                )
                failed.append(
                    {
                        "user_id": user["user_id"],
                        "username": user.get("username"),
                        "error": str(exc),
                        "failures": count,
                        "retry_in_s": retry_in,
                    }
                )
        return {"refreshed": refreshed, "failed": failed}

    def refresh_all_async(self) -> bool:
        """Kick off a forced refresh on a background thread. Returns False if busy/unavailable."""
        if self._loop is None:
            return False
        if getattr(self, "_manual_refresh_lock", None) is None:
            self._manual_refresh_lock = threading.Lock()
        if not self._manual_refresh_lock.acquire(blocking=False):
            return False

        def _run() -> None:
            try:
                self.refresh_due_users(force=True)
            finally:
                self._manual_refresh_lock.release()

        threading.Thread(
            target=_run, name="hermes-chat-suggestions-manual", daemon=True
        ).start()
        return True

    def _run(self) -> None:
        # Small initial delay so startup/pre-warm finishes first.
        self._stop.wait(15.0)
        while not self._stop.is_set():
            try:
                self.refresh_due_users(force=False)
            except Exception as exc:
                logger.warning("suggestion worker loop error: %s", exc)
            cfg = load_config()
            # Wake often enough to notice enable/interval changes without hammering.
            wait_s = 60.0 if cfg.suggestions_enabled else 120.0
            self._stop.wait(wait_s)
