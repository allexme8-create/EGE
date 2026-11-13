"""Конфигурация сервиса «Обзвон+».

Все пути и значения по умолчанию можно переопределить переменными окружения
(см. .env.example в корне проекта). Пользовательские настройки, которые
меняются из веб-интерфейса, хранятся в таблице ``settings`` базы данных.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APP_VERSION = "1.0.0"


def _path_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


DATA_DIR = _path_env("CALLBOT_DATA", BASE_DIR / "data")
RECORDINGS_DIR = DATA_DIR / "recordings"
PROMPTS_DIR = DATA_DIR / "prompts"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = _path_env("CALLBOT_DB", DATA_DIR / "callbot.db")

HOST = os.environ.get("CALLBOT_HOST", "0.0.0.0")
PORT = int(os.environ.get("CALLBOT_PORT", "8000"))

# Необязательная Basic-аутентификация (для доступа через интернет/VPN-шлюз).
AUTH_USER = os.environ.get("CALLBOT_USER", "")
AUTH_PASSWORD = os.environ.get("CALLBOT_PASSWORD", "")

# Значения настроек по умолчанию. Ключи могут переопределяться в БД.
DEFAULT_SETTINGS: dict[str, str] = {
    # Режим работы: demo — без оборудования, modem — реальный GSM-модем.
    "default_mode": os.environ.get("CALLBOT_MODE", "demo"),
    # Модем
    "modem_port": os.environ.get("CALLBOT_MODEM_PORT", ""),
    "modem_baud": "115200",
    "operator": "МегаФон",
    "ussd_balance": "*100#",  # проверка баланса МегаФон
    "sim_number": "",  # номер SIM (для отображения)
    # Аудио (USB-звуковая карта, подключённая к модему, или голосовой порт)
    "audio_backend": os.environ.get("CALLBOT_AUDIO_BACKEND", "auto"),  # auto|alsa|sounddevice|null
    "audio_play_device": os.environ.get("CALLBOT_AUDIO_PLAY", ""),
    "audio_record_device": os.environ.get("CALLBOT_AUDIO_REC", ""),
    "audio_rate": "8000",
    # Распознавание речи
    "asr_engine": os.environ.get("CALLBOT_ASR", "auto"),  # auto|vosk|whispercpp|http|stub
    "vosk_model_path": os.environ.get("CALLBOT_VOSK_MODEL", str(DATA_DIR / "models" / "vosk-ru")),
    "whisper_bin": os.environ.get("CALLBOT_WHISPER_BIN", "whisper-cli"),
    "whisper_model_path": os.environ.get("CALLBOT_WHISPER_MODEL", str(DATA_DIR / "models" / "ggml-base.bin")),
    "http_asr_url": os.environ.get("CALLBOT_ASR_HTTP", ""),
    "http_asr_token": "",
    # Озвучивание вопросов
    "tts_engine": os.environ.get("CALLBOT_TTS", "auto"),  # auto|piper|espeak|none
    "piper_bin": os.environ.get("CALLBOT_PIPER_BIN", "piper"),
    "piper_model": os.environ.get("CALLBOT_PIPER_MODEL", str(DATA_DIR / "models" / "ru_RU-irina-medium.onnx")),
    "espeak_voice": "ru",
    # Поведение обзвона по умолчанию
    "max_concurrent_calls": "1",
    "default_retry_limit": "1",
    "default_call_timeout": "40",
    "answer_seconds": "6",
    "min_confidence": "0.45",
    "record_answers": "1",
    "respect_dnc": "1",  # уважать список «не звонить»
    # Ограничение времени звонков (по местному времени сервера)
    "calling_hours": "09:00-20:00",
    # Публичный адрес для ссылок (необязательно)
    "public_url": "",
}


def ensure_dirs() -> None:
    for path in (DATA_DIR, RECORDINGS_DIR, PROMPTS_DIR, UPLOADS_DIR):
        path.mkdir(parents=True, exist_ok=True)
