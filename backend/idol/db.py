import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .config import DEFAULT_SETTINGS


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    """Short-lived SQLite connections; never share a connection between threads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, created_at);
            """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params=()) -> int:
        with self.transaction() as conn:
            return conn.execute(sql, params).lastrowid or 0

    def query(self, sql: str, params=()) -> list[dict]:
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def settings(self) -> dict:
        values = dict(DEFAULT_SETTINGS)
        for row in self.query("SELECT key,value FROM settings"):
            if row["key"] in values:
                values[row["key"]] = json.loads(row["value"])
        return values

    def update_settings(self, values: dict) -> dict:
        with self.transaction() as conn:
            for key, value in values.items():
                if key in DEFAULT_SETTINGS:
                    conn.execute(
                        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
        return self.settings()
