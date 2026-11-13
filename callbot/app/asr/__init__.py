"""Распознавание речи из аудиофайлов (WAV) с переключением движков.

Поддерживаются:
* vosk — локальная модель Vosk (офлайн, хорошо для узкой лексики обзвона);
* whispercpp — whisper.cpp CLI (ggml-модель, офлайн, точнее на свободной речи);
* http — внешний ASR по HTTP (например, собственный сервис на базе Whisper);
* stub — демонстрационный режим без аудио (ответы имитируются в DemoEngine).

Движок выбирается параметром ``engine`` или автоматически по настройкам
(asr_engine) и доступности модели/бинарника.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from ..settings import SettingsStore


@dataclass
class Transcription:
    text: str
    engine: str
    confidence: float = 0.0
    error: str = ""


class ASRError(RuntimeError):
    pass


# --------------------------------------------------------------------------- utils
def wav_info(path: str | Path) -> tuple[int, int, int, float]:
    with wave.open(str(path), "rb") as wf:
        return wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes() / max(wf.getframerate(), 1)


def resample_wav(path: str | Path, rate: int = 16000, channels: int = 1) -> Path:
    """Приводит WAV к mono/16 кГц 16-бит (нужно Vosk и whisper.cpp)."""
    path = Path(path)
    with wave.open(str(path), "rb") as src:
        if src.getframerate() == rate and src.getnchannels() == channels and src.getsampwidth() == 2:
            return path
        params = src.getparams()
        frames = src.readframes(params.nframes)
    try:
        import audioop  # type: ignore
    except ImportError:  # Python 3.13+
        audioop = None
    data = frames
    if audioop is not None:
        if params.sampwidth != 2:
            data = audioop.lin2lin(data, params.sampwidth, 2)
        if params.nchannels != channels:
            data = audioop.tomono(data, 2, 1.0, 1.0) if params.nchannels == 2 else audioop.tostereo(data, 2, 1.0, 1.0)
        if params.framerate != rate:
            data, _ = audioop.ratecv(data, 2, channels, params.framerate, rate, None)
    out = Path(tempfile.mkstemp(suffix=".wav", prefix="asr_")[1])
    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(data)
    return out


def audio_has_speech(path: str | Path, min_rms: int = 250) -> bool:
    """Грубая проверка, что в записи есть голос (отсекает тишину/шум)."""
    try:
        import audioop  # type: ignore
    except ImportError:
        return True
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.readframes(min(wf.getnframes(), wf.getframerate() * 60))
        rms = audioop.rms(frames, 2)
        peak = audioop.max(frames, 2)
        return rms >= min_rms or peak >= 3000
    except Exception:
        return True


# --------------------------------------------------------------------------- engines
class BaseEngine:
    name = "base"

    def available(self) -> bool:  # pragma: no cover - интерфейс
        return False

    def transcribe(self, wav_path: str) -> Transcription:  # pragma: no cover
        raise NotImplementedError


class VoskEngine(BaseEngine):
    name = "vosk"

    def __init__(self, model_path: str):
        self.model_path = model_path
        self._model: Any = None

    def available(self) -> bool:
        try:
            import vosk  # noqa: F401
        except ImportError:
            return False
        return bool(self.model_path) and Path(self.model_path).is_dir()

    def _load(self):
        if self._model is None:
            import vosk

            vosk.SetLogLevel(-1)
            self._model = vosk.Model(self.model_path)
        return self._model

    def transcribe(self, wav_path: str) -> Transcription:
        import vosk

        model = self._load()
        with wave.open(wav_path, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                path = resample_wav(wav_path)
                return self.transcribe(str(path))
            rec = vosk.KaldiRecognizer(model, wf.getframerate())
            rec.SetWords(True)
            parts: list[str] = []
            confidences: list[float] = []
            while True:
                data = wf.readframes(8000)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    parts.append(result.get("text", ""))
                    confidences += [w.get("conf", 0.0) for w in result.get("result", [])]
            final = json.loads(rec.FinalResult())
            parts.append(final.get("text", ""))
            confidences += [w.get("conf", 0.0) for w in final.get("result", [])]
        text = " ".join(p for p in parts if p).strip()
        conf = sum(confidences) / len(confidences) if confidences else 0.0
        return Transcription(text=text, engine=self.name, confidence=round(conf, 3))


class WhisperCppEngine(BaseEngine):
    name = "whispercpp"

    def __init__(self, bin_path: str, model_path: str):
        self.bin_path = bin_path
        self.model_path = model_path

    def available(self) -> bool:
        exe = shutil.which(self.bin_path) or (self.bin_path if Path(self.bin_path).exists() else None)
        return bool(exe and Path(self.model_path).is_file())

    def transcribe(self, wav_path: str) -> Transcription:
        exe = shutil.which(self.bin_path) or self.bin_path
        cmd = [exe, "-m", self.model_path, "-f", wav_path, "-l", "ru", "-nt", "-np"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise ASRError(proc.stderr.strip()[-500:] or "whisper.cpp завершился с ошибкой")
        text = " ".join(line.strip() for line in proc.stdout.splitlines() if line.strip())
        return Transcription(text=text, engine=self.name, confidence=0.7 if text else 0.0)


class HttpAsrEngine(BaseEngine):
    name = "http"

    def __init__(self, url: str, token: str = ""):
        self.url = url.rstrip("/")
        self.token = token

    def available(self) -> bool:
        return bool(self.url)

    def transcribe(self, wav_path: str) -> Transcription:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with open(wav_path, "rb") as fh:
            resp = httpx.post(f"{self.url}/transcribe", files={"file": (Path(wav_path).name, fh, "audio/wav")},
                              data={"language": "ru"}, headers=headers, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        return Transcription(text=(data.get("text") or "").strip(), engine=self.name,
                             confidence=float(data.get("confidence") or 0.0))


class StubEngine(BaseEngine):
    name = "stub"

    def available(self) -> bool:
        return True

    def transcribe(self, wav_path: str) -> Transcription:
        return Transcription(text="", engine=self.name, confidence=0.0,
                             error="ASR не настроен: демо-режим или отсутствует модель")


class ASRService:
    """Фабрика движков + кэш последних результатов."""

    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self._engines: dict[str, BaseEngine] = {}

    def _build(self, preferred: str | None = None) -> BaseEngine:
        engine = (preferred or self.settings.get("asr_engine") or "auto").lower()
        vosk = VoskEngine(self.settings.get("vosk_model_path"))
        whisper = WhisperCppEngine(self.settings.get("whisper_bin"), self.settings.get("whisper_model_path"))
        http = HttpAsrEngine(self.settings.get("http_asr_url"), self.settings.get("http_asr_token") or "")
        candidates: list[BaseEngine]
        if engine == "auto":
            candidates = [vosk, whisper, http, StubEngine()]
        else:
            mapping = {"vosk": vosk, "whispercpp": whisper, "whisper": whisper, "http": http, "stub": StubEngine()}
            chosen = mapping.get(engine, vosk)
            candidates = [chosen]
        for cand in candidates:
            if cand.available():
                return cand
        return StubEngine()

    def engine_name(self, preferred: str | None = None) -> str:
        key = preferred or ""
        if key in self._engines:
            return self._engines[key].name
        engine = self._build(preferred)
        self._engines[key] = engine
        return engine.name

    def status(self) -> dict[str, Any]:
        vosk = VoskEngine(self.settings.get("vosk_model_path"))
        whisper = WhisperCppEngine(self.settings.get("whisper_bin"), self.settings.get("whisper_model_path"))
        http = HttpAsrEngine(self.settings.get("http_asr_url"), self.settings.get("http_asr_token") or "")
        return {
            "selected": self.settings.get("asr_engine"),
            "active": self.engine_name(),
            "engines": {
                "vosk": {"available": vosk.available(), "model": self.settings.get("vosk_model_path")},
                "whispercpp": {"available": whisper.available(), "model": self.settings.get("whisper_model_path"),
                               "bin": self.settings.get("whisper_bin")},
                "http": {"available": http.available(), "url": self.settings.get("http_asr_url")},
                "stub": {"available": True},
            },
        }

    def transcribe(self, wav_path: str, preferred: str | None = None,
                   progress: Callable[[float], None] | None = None) -> Transcription:
        engine = self._build(preferred)
        if engine.name == "stub":
            return Transcription(text="", engine="stub", confidence=0.0,
                                 error="Движок распознавания не настроен (см. «Настройки»)")
        try:
            path = str(resample_wav(wav_path, 16000, 1))
            if progress:
                progress(0.1)
            result = engine.transcribe(path)
            if progress:
                progress(1.0)
            return result
        except ASRError:
            raise
        except Exception as exc:  # noqa: BLE001 - важно не уронить обзвон
            raise ASRError(f"Ошибка распознавания ({engine.name}): {exc}") from exc
