"""Синтез речи для озвучивания вопросов и загрузка готовых аудиофайлов."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .settings import SettingsStore

SUPPORTED_AUDIO_EXT = {".wav", ".mp3", ".ogg", ".opus", ".amr", ".gsm", ".m4a", ".aac"}


class TTSService:
    """Озвучивание текста. Приоритет: piper (естественный голос) → espeak-ng.

    Внешний TTS можно подключить, положив готовые WAV-файлы к вопросам
    (поле audio) — это рекомендуемый путь для боевого обзвона: файл
    записывается один раз и не зависит от движка на сервере.
    """

    def __init__(self, settings: SettingsStore, prompts_dir: Path):
        self.settings = settings
        self.prompts_dir = prompts_dir
        self.prompts_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- engines
    def _piper(self) -> str | None:
        exe = shutil.which(self.settings.get("piper_bin") or "piper")
        model = self.settings.get("piper_model") or ""
        if exe and model and Path(model).is_file():
            return exe
        return None

    def _espeak(self) -> str | None:
        for name in ("espeak-ng", "espeak"):
            exe = shutil.which(name)
            if exe:
                return exe
        return None

    def engine(self) -> str:
        chosen = (self.settings.get("tts_engine") or "auto").lower()
        if chosen == "none":
            return "none"
        if chosen in ("piper", "auto") and self._piper():
            return "piper"
        if chosen in ("espeak", "auto") and self._espeak():
            return "espeak"
        return "none"

    def status(self) -> dict:
        return {
            "selected": self.settings.get("tts_engine"),
            "active": self.engine(),
            "piper": bool(self._piper()),
            "espeak": bool(self._espeak()),
            "piper_model": self.settings.get("piper_model"),
        }

    # ---------------------------------------------------------------- synthesis
    def synthesize(self, text: str, voice: str | None = None) -> Path:
        """Возвращает путь к WAV (8 кГц mono 16-бит — совместимо с модемом)."""
        text = (text or "").strip()
        if not text:
            raise ValueError("Пустой текст для озвучивания")
        key = hashlib.sha1(f"{self.engine()}|{voice}|{text}".encode("utf-8")).hexdigest()[:16]
        out = self.prompts_dir / f"tts_{key}.wav"
        if out.exists() and out.stat().st_size > 44:
            return out

        tmp = self.prompts_dir / f"tts_{key}_raw.wav"
        engine = self.engine()
        if engine == "piper":
            cmd = [self._piper(), "--model", self.settings.get("piper_model"), "--output_file", str(tmp)]
            proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"piper: {proc.stderr.strip()[-300:]}")
        elif engine == "espeak":
            cmd = [self._espeak(), "-v", voice or self.settings.get("espeak_voice") or "ru",
                   "-s", "140", "-w", str(tmp), text]
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        else:
            raise RuntimeError(
                "Синтез речи недоступен: установите piper/espeak-ng или загрузите готовый WAV-файл к вопросу"
            )
        convert_to_telephony_wav(tmp, out)
        tmp.unlink(missing_ok=True)
        return out


def convert_to_telephony_wav(src: str | Path, dst: str | Path, rate: int = 8000) -> None:
    """Конвертация в 8 кГц mono PCM (или копирование, если уже в нужном формате)."""
    src, dst = Path(src), Path(dst)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(rate),
               "-sample_fmt", "s16", str(dst)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return
    # Без ffmpeg: только если источник уже WAV.
    try:
        import wave

        with wave.open(str(src), "rb") as r:
            params = r.getparams()
        if params.nchannels == 1 and params.sampwidth == 2 and params.framerate == rate:
            shutil.copyfile(src, dst)
            return
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError("Для конвертации аудио установите ffmpeg (или загрузите WAV 8 кГц mono)")


def save_uploaded_audio(filename: str, data: bytes, target_dir: Path) -> Path:
    ext = Path(filename or "").suffix.lower()
    if ext not in SUPPORTED_AUDIO_EXT:
        raise ValueError(f"Неподдерживаемый формат аудио: {ext or 'без расширения'}")
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:16]
    raw = target_dir / f"upload_{digest}{ext}"
    raw.write_bytes(data)
    if ext == ".wav":
        try:
            out = target_dir / f"upload_{digest}_8k.wav"
            convert_to_telephony_wav(raw, out)
            return out
        except RuntimeError:
            return raw
    try:
        out = target_dir / f"upload_{digest}_8k.wav"
        convert_to_telephony_wav(raw, out)
        return out
    except Exception:  # noqa: BLE001 - модем умеет и mp3/amr, оставляем как есть
        return raw
