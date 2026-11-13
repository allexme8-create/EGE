"""Работа с GSM-модемом по AT-командам (МегаФон, голосовой обзвон).

Модуль умеет:
* находить последовательные порты модема (/dev/ttyUSB*, COM*);
* выполнять AT-команды с учётом несинхронных ответов (RING, NO CARRIER, ...);
* проверять SIM/сеть/баланс (USSD *100#);
* звонить (ATD<номер>;), определять ответ абонента (CONNECT / NO CARRIER / BUSY),
  сбрасывать вызов (ATH) и принимать DTMF (``+DTMF: 1`` в URC-потоке);
* запускать воспроизведение и запись через AT^DDSETEX / AT^DAUDIO / arecord —
  при подключённой к модему USB-звуковой карте (наиболее надёжный способ для
  «голосовых» модемов, которые показывают себя как tty + USB audio).

ВАЖНО: голосовой обзвон возможен только с модемами, у которых открыт голосовой
канал (Huawei E1550/E171/E173/E3372, SIM800/SIM7600, Quectel M26/EC25, Telit).
Свистки «только данные» (например, E3372h-153 в режиме HiLink) голос не
пропускают — для них нужна перепрошивка в режим Stick/TTY или другой модем.
Операторская SIM МегаФона подходит любая голосовая (тариф для звонков).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:  # pyserial — опциональная зависимость (нужна только реальному модему)
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover
    serial = None  # type: ignore
    list_ports = None  # type: ignore


class ModemError(RuntimeError):
    def __init__(self, message: str, response: str = ""):
        super().__init__(message)
        self.response = response


@dataclass
class ModemInfo:
    port: str = ""
    connected: bool = False
    model: str = ""
    imei: str = ""
    sim_ready: bool = False
    sim_number: str = ""
    operator: str = ""
    signal: int = 0
    balance: str = ""
    audio_device: str = ""
    error: str = ""


# --------------------------------------------------------------------------- AT helper
class ATChannel:
    """Последовательный порт с построчным чтением и ожиданием ответов."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        if serial is None:
            raise ModemError("Установите pyserial: pip install pyserial")
        self.port = port
        self.baud = baud
        self.ser = serial.Serial(port, baud, timeout=0.2, write_timeout=2)
        self.timeout = timeout
        self._lock = threading.RLock()
        self._urc: list[str] = []
        self._last_dtmf: list[str] = []
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ------------------------------------------------------------------ I/O
    def _read_loop(self) -> None:
        buffer = b""
        while not self._closed:
            try:
                chunk = self.ser.readline()
            except Exception:
                return
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode("utf-8", errors="replace").strip("\r\n \t")
                if not text:
                    continue
                with self._lock:
                    self._urc.append(text)
                    m = re.search(r"\+DTMF:\s*([0-9A-Da-d*#])", text)
                    if m:
                        self._last_dtmf.append(m.group(1))
                    if len(self._urc) > 500:
                        self._urc = self._urc[-200:]

    def write(self, command: str) -> None:
        with self._lock:
            self._urc.clear()
        self.ser.write((command + "\r").encode("ascii", errors="ignore"))
        try:
            self.ser.flush()
        except Exception:
            pass

    def drain(self) -> list[str]:
        with self._lock:
            lines = list(self._urc)
            self._urc.clear()
        return lines

    def take_dtmf(self) -> list[str]:
        with self._lock:
            digits = list(self._last_dtmf)
            self._last_dtmf.clear()
        return digits

    def command(self, command: str, expect: tuple[str, ...] = ("OK",), timeout: float | None = None,
                allow_error: bool = False) -> tuple[bool, list[str]]:
        """Отправляет AT-команду и ждёт один из финальных ответов."""
        self.write(command)
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        collected: list[str] = []
        while time.time() < deadline:
            lines = self.drain()
            for line in lines:
                if line.startswith(("RING", "+CRING", "+CLIP", "NO CARRIER", "+DTMF")):
                    # несинхронные уведомления — возвращаем в очередь для ожидающего кода
                    with self._lock:
                        self._urc.append(line)
                        if line.startswith("+DTMF"):
                            m = re.search(r"\+DTMF:\s*([0-9A-Da-d*#])", line)
                            if m:
                                self._last_dtmf.append(m.group(1))
                    continue
                collected.append(line)
                if line in expect or line.startswith("+CME ERROR") or line.startswith("+CMS ERROR") or line == "ERROR":
                    ok = line in expect
                    if not ok and not allow_error:
                        raise ModemError(f"Команда {command} → {line}", "\n".join(collected))
                    return ok, collected
            time.sleep(0.05)
        raise ModemError(f"Таймаут команды {command}", "\n".join(collected))

    def wait_for(self, prefixes: tuple[str, ...], timeout: float = 30.0,
                 stop: Callable[[], bool] | None = None) -> str | None:
        """Ждёт строку URC, начинающуюся с одного из prefixes; возвращает её."""
        deadline = time.time() + timeout
        seen: list[str] = []
        while time.time() < deadline:
            lines = self.drain()
            for line in lines:
                seen.append(line)
                for prefix in prefixes:
                    if line.startswith(prefix):
                        return line
            if stop and stop():
                return None
            time.sleep(0.05)
        raise ModemError(f"Не дождались {prefixes} за {timeout} c; получено: {seen[-6:]}")

    def close(self) -> None:
        self._closed = True
        try:
            self.ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- audio
class AudioIO:
    """Запись/воспроизведение через ALSA (arecord/aplay) или звуковое устройство.

    Для голосового обзвона аудио связано с модемом физически (USB-гарнитура
    или звуковая карта, подключённая к голосовому порту модема). Настройки
    audio_play_device/audio_record_device задают ALSA-устройство.
    """

    def __init__(self, play_device: str = "", rec_device: str = "", rate: int = 8000,
                 backend: str = "auto"):
        self.play_device = play_device
        self.rec_device = rec_device
        self.rate = rate
        self.backend = backend

    def _arecord_cmd(self, out: str, seconds: float) -> list[str]:
        cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(self.rate), "-c", "1",
               "-d", str(max(1, int(seconds))), out]
        if self.rec_device:
            cmd[1:1] = ["-D", self.rec_device]
        return cmd

    def record(self, out_path: Path, max_seconds: float, stop: Callable[[], bool] | None = None,
               silence_seconds: float = 1.6, vad_rms: int = 320) -> Path:
        """Пишет ответ абонента: до тишины (VAD) или до max_seconds.

        arecord пишет сырой PCM в stdout, мы считаем RMS по последним фреймам и
        останавливаемся после silence_seconds тишины. Это надёжнее, чем читать
        недописанный WAV-файл, и не требует знать длительность заранее.
        """
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not (shutil.which("arecord") and _has_audioop()):
            write_silence(out_path, min(max_seconds, 3.0), self.rate)
            return out_path

        import audioop

        cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(self.rate), "-c", "1", "-t", "raw"]
        if self.rec_device:
            cmd[1:1] = ["-D", self.rec_device]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        frames: list[bytes] = []
        started = time.time()
        last_voice = started
        window = max(1, self.rate // 4) * 2  # 250 мс, 16-бит mono
        try:
            while True:
                chunk = proc.stdout.read(window * 2) if proc.stdout else b""
                if not chunk or len(chunk) < window:
                    if proc.poll() is not None:
                        break
                    continue
                frames.append(chunk)
                rms = audioop.rms(chunk, 2)
                now = time.time()
                if rms >= vad_rms:
                    last_voice = now
                if stop and stop():
                    break
                if now - started >= max_seconds:
                    break
                # Первая секунда: даём абоненту начать говорить.
                if now - started > 1.0 and now - last_voice >= silence_seconds:
                    break
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()

        data = b"".join(frames)
        min_len = self.rate // 2  # не сохраняем «пустышку» короче 0,25 с
        if len(data) < min_len:
            write_silence(out_path, 0.5, self.rate)
            return out_path
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.rate)
            wf.writeframes(data)
        return out_path

    def play(self, path: Path, stop: Callable[[], bool] | None = None) -> None:
        aplay = shutil.which("aplay")
        if not aplay:
            return
        cmd = [aplay, "-q"]
        if self.play_device:
            cmd += ["-D", self.play_device]
        cmd.append(str(path))
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while proc.poll() is None:
                if stop and stop():
                    break
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                proc.terminate()


def wav_tail_rms(path: Path, seconds: float = 0.25) -> int | None:
    """RMS последнего фрагмента WAV (для простого VAD)."""
    try:
        import audioop  # type: ignore

        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.getnframes()
            n = int(rate * seconds)
            if frames <= 0:
                return None
            wf.setpos(max(0, frames - n))
            data = wf.readframes(n)
        if len(data) < 2:
            return None
        return audioop.rms(data, wf.getsampwidth())
    except Exception:
        return None


def write_silence(path: Path, seconds: float, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


# --------------------------------------------------------------------------- modem
def list_serial_ports() -> list[dict]:
    if list_ports is None:
        return []
    ports = []
    for p in list_ports.comports():
        desc = " ".join(x for x in (p.description, p.manufacturer, p.product) if x and x != "n/a")
        ports.append({
            "device": p.device,
            "description": desc or "Последовательный порт",
            "hwid": p.hwid or "",
            "likely_modem": any(k in (desc + p.hwid).lower() for k in
                                ("modem", "huawei", "quectel", "simcom", "sim800", "sim7600",
                                 "telit", "sierra", "wavecom", "gsm")),
        })
    return ports


class AtModem:
    """Высокоуровневый драйвер модема для обзвона."""

    def __init__(self, port: str, baud: int = 115200, ussd_balance: str = "*100#",
                 audio: AudioIO | None = None, logger: Callable[[str, str], None] | None = None):
        self.port = port
        self.baud = baud
        self.ussd_balance = ussd_balance
        self.audio = audio or AudioIO()
        self.log = logger or (lambda level, msg: None)
        self.channel: ATChannel | None = None
        self.info = ModemInfo(port=port)

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> ModemInfo:
        self.channel = ATChannel(self.port, self.baud)
        self.info.connected = True
        self._initialize()
        return self.info

    def close(self) -> None:
        if self.channel:
            try:
                self.channel.command("ATH", allow_error=True, timeout=2)
            except Exception:
                pass
            self.channel.close()
            self.channel = None

    def _initialize(self) -> None:
        assert self.channel
        ch = self.channel
        ch.command("ATZ", allow_error=True)
        ch.command("ATE0")
        ch.command("AT+CMEE=1", allow_error=True)
        # Голосовой канал и DTMF (наборы команд различаются по производителю).
        for cmd in ("AT^CURC=1", "AT+CRC=1", "AT+CLIP=1", "AT+DDET=1,1", "AT+VTD=1"):
            try:
                ch.command(cmd, allow_error=True, timeout=2)
            except ModemError:
                pass

        self.info.model = self._try_best(("AT+CGMM", "AT+CGMI", "ATI"))
        self.info.imei = self._try_best(("AT+CGSN", "AT+GSN"))
        simstat = self._try_best(("AT+CPIN?",))
        self.info.sim_ready = "READY" in simstat.upper()
        cnum = self._try_best(("AT+CNUM",))
        m = re.search(r'\+CNUM:\s*"[^"]*",\s*"([^"]+)"', cnum)
        if m and re.search(r"\d{5,}", m.group(1)):
            self.info.sim_number = m.group(1)
        cops = self._try_best(("AT+COPS?",))
        m = re.search(r'\+COPS:\s*\d,\d,"([^"]+)"', cops)
        if m:
            self.info.operator = m.group(1)
        csq = self._try_best(("AT+CSQ",))
        m = re.search(r"\+CSQ:\s*(\d+)", csq)
        if m and m.group(1) != "99":
            self.info.signal = int(m.group(1))
        self.log("info", f"Модем: {self.info.model or 'неизвестен'}, SIM: "
                         f"{'готово' if self.info.sim_ready else 'нет'}, оператор: {self.info.operator or '—'}, "
                         f"сигнал: {self.info.signal}/31")

    def _try_best(self, commands: tuple[str, ...]) -> str:
        assert self.channel
        for cmd in commands:
            ok, lines = self.channel.command(cmd, allow_error=True, timeout=3)
            payload = [ln for ln in lines if ln != "OK"]
            if ok and payload:
                return payload[0]
            if cmd.startswith("AT+COPS") and not payload:
                continue
        return ""

    # ------------------------------------------------------------------ extras
    def refresh(self) -> ModemInfo:
        self.info.error = ""
        try:
            self._initialize()
        except Exception as exc:  # noqa: BLE001
            self.info.connected = False
            self.info.error = str(exc)
        return self.info

    def balance(self) -> str:
        """Проверка баланса USSD (*100# для МегаФона)."""
        if not self.channel:
            raise ModemError("Модем не открыт")
        ch = self.channel
        ch.command("AT+CUSD=1", allow_error=True, timeout=2)  # отмена предыдущего запроса
        ch.write(f'AT+CUSD=1,"{self.ussd_balance}",15')
        deadline = time.time() + 20
        while time.time() < deadline:
            lines = ch.drain()
            for line in lines:
                if line.startswith("+CUSD:"):
                    m = re.search(r'\+CUSD:\s*\d,\s*"([^"]+)"', line)
                    if m:
                        text = decode_ussd(m.group(1))
                        self.info.balance = text
                        return text
            time.sleep(0.1)
        return ""

    def check_number(self) -> str:
        return self._try_best(("AT+CNUM",))

    # ------------------------------------------------------------------ call
    def dial(self, phone: str, timeout: float = 45.0,
             on_ring: Callable[[], None] | None = None) -> str:
        """Набирает номер и возвращает исход: answered|busy|no_answer|rejected|error."""
        if not self.channel:
            raise ModemError("Модем не открыт")
        ch = self.channel
        ch.drain()
        ch.write(f"ATD{phone};")
        deadline = time.time() + timeout
        rings = 0
        while time.time() < deadline:
            lines = ch.drain()
            for line in lines:
                upper = line.upper()
                if line.startswith(("CONNECT",)) or "VOICE CALL: BEGIN" in upper:
                    return "answered"
                if "BUSY" in upper:
                    return "busy"
                if "NO ANSWER" in upper or "NO CARRIER" in upper:
                    return "no_answer"
                if line == "ERROR" or line.startswith(("+CME ERROR", "+CMS ERROR")):
                    return "failed"
                if line.startswith(("RING", "+CRING", "ALERT")):
                    rings += 1
                    if on_ring:
                        on_ring()
            time.sleep(0.1)
        try:
            ch.command("ATH", allow_error=True, timeout=3)
        except ModemError:
            pass
        return "no_answer"

    def hangup(self) -> None:
        if not self.channel:
            return
        try:
            self.channel.command("ATH", allow_error=True, timeout=3)
        except ModemError:
            pass

    def send_dtmf(self, digits: str) -> None:
        if self.channel and digits:
            self.channel.command(f"AT+VTS={digits}", allow_error=True, timeout=2)

    def hw_audio_mode(self, enable: bool = True) -> None:
        """Включает прямой USB-аудиоканал модема (Huawei/Quectel/SIMCom)."""
        if not self.channel:
            return
        for cmd in ("AT^DDSETEX=2", "AT+QPCMV=1,2", "AT+QAUDMOD=3"):
            self.channel.command(cmd, allow_error=True, timeout=2)

    def take_dtmf(self) -> list[str]:
        return self.channel.take_dtmf() if self.channel else []


# --------------------------------------------------------------------------- helpers
def decode_ussd(raw: str) -> str:
    """USSD приходит в GSM 7-bit/hex; декодируем наиболее частые случаи."""
    if re.fullmatch(r"[0-9A-Fa-f]+", raw or "") and len(raw) % 2 == 0:
        try:
            return bytes.fromhex(raw).decode("utf-16-be", errors="replace").strip()
        except Exception:  # noqa: BLE001
            pass
    return raw


def probe_ports() -> list[dict]:
    """Возвращает список портов с пометкой «похож на модем»."""
    ports = list_serial_ports()
    if platform.system() == "Linux" and not ports:
        for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"):
            import glob

            for dev in sorted(glob.glob(pattern)):
                ports.append({"device": dev, "description": "Последовательный порт", "hwid": "",
                              "likely_modem": True})
    return ports


def temp_wav(prefix: str = "call_") -> Path:
    fd, name = tempfile.mkstemp(suffix=".wav", prefix=prefix)
    os.close(fd)
    return Path(name)


def arecord_available() -> bool:
    return shutil.which("arecord") is not None


def aplay_available() -> bool:
    return shutil.which("aplay") is not None


def audio_devices() -> list[dict]:
    """Список ALSA-устройств записи/воспроизведения (если есть arecord/aplay)."""
    devices: list[dict] = []
    for kind, cmd in (("record", ["arecord", "-l"]), ("play", ["aplay", "-l"])):
        if not shutil.which(cmd[0]):
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except Exception:  # noqa: BLE001
            continue
        for line in proc.stdout.splitlines():
            m = re.match(r"card (\d+): (.+?) \[(.+?)\], device (\d+): (.+?) \[(.+?)\]", line)
            if m:
                card, _, _, dev, _, _ = m.groups()
                hw = f"plughw:{card},{dev}"
                devices.append({"kind": kind, "hw": hw, "label": line.strip()})
    return devices


def _has_audioop() -> bool:
    try:
        import audioop  # noqa: F401
    except ImportError:
        return False
    return True


def wav_params(path: str | Path) -> tuple[int, int, int] | None:
    """(каналы, ширина сэмпла, частота) или None, если файл не читается."""
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
    except Exception:  # noqa: BLE001
        return None


def concat_wavs(paths: list[Path], out_path: Path) -> Path | None:
    """Склеивает WAV-записи ответов в один файл записи звонка.

    Работает без ffmpeg: все записи пишутся одним форматом (8 кГц mono 16-бит),
    между ответами вставляется короткая пауза 0,3 с.
    """
    paths = [Path(p) for p in paths if Path(p).exists() and Path(p).stat().st_size > 44]
    if not paths:
        return None
    params = wav_params(paths[0])
    if params is None:
        return None
    chunks: list[bytes] = []
    for path in paths:
        if wav_params(path) != params:
            continue  # пропускаем файлы с другим форматом
        with wave.open(str(path), "rb") as wf:
            chunks.append(wf.readframes(wf.getnframes()))
    if not chunks:
        return None
    channels, width, rate = params
    silence = b"\x00" * int(rate * 0.3) * channels * width
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        for chunk in chunks:
            wf.writeframes(chunk)
            wf.writeframes(silence)
    return out_path
