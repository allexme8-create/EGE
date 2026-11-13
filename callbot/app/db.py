"""Работа с SQLite: подключение, схема, транзакции и вспомогательные функции."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft',   -- draft|running|paused|completed|stopped
    mode           TEXT NOT NULL DEFAULT 'modem',   -- modem|demo
    greeting       TEXT DEFAULT '',
    intro_text     TEXT DEFAULT '',
    outro_text     TEXT DEFAULT '',
    intro_audio    TEXT,
    outro_audio    TEXT,
    retry_limit    INTEGER NOT NULL DEFAULT 1,
    call_timeout   INTEGER NOT NULL DEFAULT 40,
    answer_seconds REAL NOT NULL DEFAULT 6,
    dtmf_enabled   INTEGER NOT NULL DEFAULT 1,
    record_answers INTEGER NOT NULL DEFAULT 1,
    notes          TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'yes_no',  -- yes_no|choice|rating|number|free
    options     TEXT DEFAULT '[]',               -- JSON-массив вариантов
    audio_path  TEXT,
    max_seconds REAL,
    required    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_questions_campaign ON questions(campaign_id, position);

CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name            TEXT DEFAULT '',
    phone           TEXT NOT NULL,
    external_id     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    -- pending|calling|done|refused|no_answer|failed|dnc
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    meta            TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_campaign ON contacts(campaign_id, status);

CREATE TABLE IF NOT EXISTS calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id    INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id     INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    attempt        INTEGER NOT NULL DEFAULT 1,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    outcome        TEXT NOT NULL DEFAULT 'running',
    -- running|answered|no_answer|busy|rejected|failed|aborted
    duration_sec   REAL DEFAULT 0,
    recording_path TEXT,
    transcript     TEXT DEFAULT '',
    error          TEXT DEFAULT '',
    engine         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calls_campaign ON calls(campaign_id, started_at);

CREATE TABLE IF NOT EXISTS answers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id       INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    campaign_id   INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id    INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    question_id   INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    raw_text      TEXT DEFAULT '',
    value         TEXT DEFAULT '',
    value_display TEXT DEFAULT '',
    confidence    REAL DEFAULT 0,
    method        TEXT DEFAULT 'asr',   -- asr|dtmf|manual|empty
    reviewed      INTEGER NOT NULL DEFAULT 0,
    audio_path    TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answers_campaign ON answers(campaign_id, contact_id);
CREATE INDEX IF NOT EXISTS idx_answers_call ON answers(call_id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'info',   -- info|warning|error|success
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id, id);

CREATE TABLE IF NOT EXISTS do_not_call (
    phone      TEXT PRIMARY KEY,
    reason     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    """Текущее время в ISO-8601 (UTC, с суффиксом Z)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_duration(seconds: float | None) -> str:
    seconds = int(round(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Database:
    """Небольшая обёртка над sqlite3 с отдельным соединением на поток."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # ------------------------------------------------------------------ connect
    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ queries
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connect().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._write_lock:
            cur = self.connect().execute(sql, tuple(params))
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._write_lock:
            self.connect().executemany(sql, rows)

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        return row[0] if row[0] is not None else default

    # ------------------------------------------------------------------ init
    def init_schema(self) -> None:
        self.connect().executescript(SCHEMA)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key, value in list(data.items()):
        if key in ("options", "meta") and isinstance(value, str):
            try:
                data[key] = json.loads(value)
            except (TypeError, ValueError):
                data[key] = [] if key == "options" else {}
    for key in ("dtmf_enabled", "record_answers", "required", "reviewed"):
        if key in data and data[key] is not None:
            data[key] = bool(data[key])
    return data


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]
