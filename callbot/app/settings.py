"""Настройки, хранящиеся в БД (переопределяют значения по умолчанию из config)."""
from __future__ import annotations

from typing import Any

from .config import DEFAULT_SETTINGS
from .db import Database, now_iso

SECRET_SETTINGS = {"http_asr_token"}


class SettingsStore:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self._cache = dict(DEFAULT_SETTINGS)
        for row in self.db.query("SELECT key, value FROM settings"):
            self._cache[row["key"]] = row["value"]

    def get(self, key: str, default: str | None = None) -> str:
        return self._cache.get(key, default if default is not None else DEFAULT_SETTINGS.get(key, ""))

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(key)))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, "1" if default else "0").strip().lower()
        return value in {"1", "true", "yes", "on", "да"}

    def set(self, key: str, value: Any) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self._cache[key] = str(value)

    def update_many(self, values: dict[str, Any], allow_unknown: bool = False) -> list[str]:
        applied: list[str] = []
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS and not allow_unknown:
                continue
            if value is None:
                continue
            self.set(key, value)
            applied.append(key)
        return applied

    def public(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, value in self._cache.items():
            if key in SECRET_SETTINGS:
                data[key] = "***" if value else ""
            else:
                data[key] = value
        return data

    def secret(self, key: str) -> str:
        return self.get(key, "")

    def export_all(self) -> dict[str, str]:
        return dict(self._cache)

    def reset(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key=?", (key,))
        self._cache.pop(key, None)
        if key in DEFAULT_SETTINGS:
            self._cache[key] = DEFAULT_SETTINGS[key]
        self.db.execute(
            "INSERT INTO events(campaign_id, ts, level, message) VALUES (NULL, ?, 'info', ?)",
            (now_iso(), f"Настройка «{key}» сброшена к значению по умолчанию"),
        )
