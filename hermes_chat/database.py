"""
SQLite-backed mapping between Open WebUI chat sessions and native Hermes
session identifiers.

The schema is intentionally tiny: a single table keyed on the Open WebUI
`chat_id`. Nothing about message content is persisted here -- Hermes itself
is the source of truth for conversation history (via `hermes chat -r
<session_id>`); this DB only remembers *which* Hermes session a given
Open WebUI chat is bound to.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import data_dir

def _default_db_path() -> Path:
    target = data_dir() / "hermes_chat.db"
    legacy = data_dir() / "hermes_bridge.db"
    if not target.exists() and legacy.exists():
        try:
            legacy.replace(target)
        except OSError:
            return legacy
    return target


DEFAULT_DB_PATH = _default_db_path()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_id             TEXT PRIMARY KEY,
    hermes_session_id   TEXT NOT NULL,
    created_at          REAL NOT NULL,
    last_used_at        REAL NOT NULL
);
"""


class ChatSessionStore:
    """Thread-safe wrapper around a small SQLite mapping table.

    SQLite connections are not shared across threads by default, so we keep
    a lock and open short-lived connections per operation. Given the low
    write volume (one row touched per chat turn) this is more than fast
    enough and avoids any need for a connection pool.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            yield conn
        finally:
            conn.close()

    def get_hermes_session_id(self, chat_id: str) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT hermes_session_id FROM chat_sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return row[0] if row else None

    def get_or_create_hermes_session_id(self, chat_id: str) -> tuple[str, bool]:
        """Return (hermes_session_id, created) for the given chat_id.

        `created` is True when a brand new Hermes session id was minted.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT hermes_session_id FROM chat_sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            now = time.time()
            if row:
                conn.execute(
                    "UPDATE chat_sessions SET last_used_at = ? WHERE chat_id = ?",
                    (now, chat_id),
                )
                conn.commit()
                return row[0], False

            hermes_session_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO chat_sessions (chat_id, hermes_session_id, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, hermes_session_id, now, now),
            )
            conn.commit()
            return hermes_session_id, True

    def set_hermes_session_id(self, chat_id: str, hermes_session_id: str) -> None:
        """Upsert the hermes_session_id for a chat_id (used by the gateway backend)."""
        with self._lock, self._connect() as conn:
            now = time.time()
            conn.execute(
                """
                INSERT INTO chat_sessions (chat_id, hermes_session_id, created_at, last_used_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    hermes_session_id = excluded.hermes_session_id,
                    last_used_at = excluded.last_used_at
                """,
                (chat_id, hermes_session_id, now, now),
            )
            conn.commit()

    def find_chat_id_by_hermes_session_id(self, hermes_session_id: str) -> Optional[str]:
        """Return the chat_id bound to this Hermes session, if any."""
        if not hermes_session_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id FROM chat_sessions WHERE hermes_session_id = ?",
                (hermes_session_id,),
            ).fetchone()
            return row[0] if row else None

    def list_bound_hermes_session_ids(self) -> dict[str, str]:
        """Return ``{hermes_session_id: chat_id}`` for every binding we know."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT hermes_session_id, chat_id FROM chat_sessions"
            ).fetchall()
            return {row[0]: row[1] for row in rows if row[0] and row[1]}

    def delete(self, chat_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM chat_sessions WHERE chat_id = ?", (chat_id,))
            conn.commit()

    def touch(self, chat_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE chat_sessions SET last_used_at = ? WHERE chat_id = ?",
                (time.time(), chat_id),
            )
            conn.commit()
