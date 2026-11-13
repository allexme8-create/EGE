"""Тесты вспомогательной логики обзвона: VAD-запись, склейка WAV, часы, восстановление."""
from __future__ import annotations

import sys
import wave
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.engine import CallEngine, within_calling_hours
from app.telephony.modem import AudioIO, concat_wavs, wav_params, write_silence


@pytest.mark.parametrize("spec,now,expected", [
    ("09:00-20:00", datetime(2026, 5, 5, 10, 30), True),
    ("09:00-20:00", datetime(2026, 5, 5, 8, 59), False),
    ("09:00-20:00", datetime(2026, 5, 5, 20, 0), False),
    ("22:00-06:00", datetime(2026, 5, 5, 23, 15), True),
    ("22:00-06:00", datetime(2026, 5, 5, 12, 0), False),
    ("", datetime(2026, 5, 5, 3, 0), True),
    ("неверный формат", datetime(2026, 5, 5, 3, 0), True),
])
def test_calling_hours(spec: str, now: datetime, expected: bool) -> None:
    assert within_calling_hours(spec, now) is expected


def test_concat_wavs(tmp_path: Path) -> None:
    a = write_silence(tmp_path / "a.wav", 0.4, 8000)
    b = write_silence(tmp_path / "b.wav", 0.2, 8000)
    out = concat_wavs([a, b], tmp_path / "call.wav")
    assert out and out.exists()
    assert wav_params(out) == (1, 2, 8000)
    with wave.open(str(out), "rb") as wf:
        duration = wf.getnframes() / wf.getframerate()
    assert duration > 0.5  # 0.4 + 0.2 + пауза


def test_concat_wavs_empty(tmp_path: Path) -> None:
    assert concat_wavs([], tmp_path / "x.wav") is None
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav")
    assert concat_wavs([broken], tmp_path / "y.wav") is None


def test_record_without_arecord_produces_valid_wav(tmp_path: Path, monkeypatch) -> None:
    import app.telephony.modem as modem_module

    monkeypatch.setattr(modem_module.shutil, "which", lambda name: None)
    audio = AudioIO(rate=8000)
    out = audio.record(tmp_path / "answer.wav", max_seconds=2)
    assert out.exists()
    assert wav_params(out) == (1, 2, 8000)


def test_recover_stale(tmp_path) -> None:
    from app.asr import ASRService
    from app.db import Database, now_iso
    from app.settings import SettingsStore
    from app.tts import TTSService

    db = Database(tmp_path / "db.sqlite")
    db.init_schema()
    settings = SettingsStore(db)
    engine = CallEngine(db, settings, ASRService(settings), TTSService(settings, tmp_path))
    cid = db.execute(
        "INSERT INTO campaigns(name, status, mode, created_at) VALUES ('X', 'running', 'demo', ?)", (now_iso(),))
    contact_id = db.execute(
        "INSERT INTO contacts(campaign_id, phone, status, created_at) VALUES (?, '+79990000000', 'calling', ?)",
        (cid, now_iso()))
    call_id = db.execute(
        "INSERT INTO calls(campaign_id, contact_id, started_at, outcome) VALUES (?,?,?, 'running')",
        (cid, contact_id, now_iso()))
    engine.recover_stale()
    assert db.query_one("SELECT status FROM contacts WHERE id=?", (contact_id,))["status"] == "pending"
    assert db.query_one("SELECT outcome, ended_at FROM calls WHERE id=?", (call_id,))["outcome"] == "aborted"
    assert db.query_one("SELECT ended_at FROM calls WHERE id=?", (call_id,))["ended_at"]
