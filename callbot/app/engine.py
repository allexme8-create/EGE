"""Движок обзвона: очередь контактов, диалог с абонентом, сбор и разбор ответов.

Два режима:
* ``modem`` — реальный GSM-модем (AT-команды + аудио через подключённую
  USB-звуковую карту/гарнитуру), см. telephony/modem.py;
* ``demo``  — эмулятор без оборудования: «абоненты» отвечают случайными
  фразами, аудио генерируется локально. Нужен для проверки сценария,
  обучения операторов и демонстрации таблицы результатов.
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .answers import contains_refusal, parse_answer
from .asr import ASRError, ASRService
from .config import RECORDINGS_DIR
from .settings import SettingsStore
from .db import Database, now_iso
from .telephony.modem import AudioIO, AtModem, ModemError, concat_wavs, write_silence
from .tts import TTSService

DEMO_YES = ["да", "да, конечно", "ну да", "ага", "да, интересно", "конечно да"]
DEMO_NO = ["нет", "нет, не интересно", "не надо", "нет, спасибо", "не сейчас"]
DEMO_REFUSE = ["нет, не звоните мне больше", "уберите меня из списка", "не звоните"]
# «Шумные» ответы — эмуляция ошибок распознавания (для проверки ручной правки).
DEMO_UNCLEAR = [
    "м э э наверное да не знаю",
    "что то не разобрать",
    "ну это самое как его",
    "не очень понял вопрос",
]
DEMO_FREE = [
    "хочу узнать про тариф с интернетом",
    "интересует подключение к домашнему интернету",
    "мне нужна консультация по оплате",
    "пока не знаю, перезвоните позже",
    "всё устраивает, спасибо",
]


def local_now() -> datetime:
    return datetime.now()


def within_calling_hours(spec: str, now: datetime | None = None) -> bool:
    """Проверяет интервал «09:00-20:00» (пустая строка = без ограничений)."""
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return True
    now = now or local_now()
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except ValueError:
        return True
    minutes = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # ночной интервал


@dataclass
class CallResult:
    outcome: str = "failed"  # answered|no_answer|busy|rejected|failed|aborted
    recording: str = ""
    transcript: str = ""
    error: str = ""
    refused: bool = False


@dataclass
class ConversationContext:
    db: Database
    campaign: dict[str, Any]
    contact: dict[str, Any]
    call_id: int
    questions: list[dict[str, Any]]
    attempt: int
    log: Callable[[str, str], None]
    stop_flag: Callable[[], bool]
    answers: list[dict[str, Any]] = field(default_factory=list)
    transcript_parts: list[str] = field(default_factory=list)

    def save_answer(self, question: dict[str, Any], parsed: Any, raw_text: str) -> int:
        """Сохраняет ответ в БД и возвращает id записи."""
        answer_id = self.db.execute(
            """INSERT INTO answers(call_id, campaign_id, contact_id, question_id, raw_text, value,
                                   value_display, confidence, method, audio_path, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (self.call_id, self.campaign["id"], self.contact["id"], question["id"], raw_text[:2000],
             parsed.value, parsed.display, float(parsed.confidence), parsed.method,
             getattr(parsed, "audio_path", ""), now_iso()))
        self.answers.append({"id": answer_id, "question_id": question["id"], "value": parsed.value,
                             "display": parsed.display, "confidence": parsed.confidence})
        return answer_id


class CallEngine:
    """Управляет кампаниями обзвона и активными «раннерами»."""

    def __init__(self, db: Database, settings: SettingsStore, asr: ASRService, tts: TTSService):
        self.db = db
        self.settings = settings
        self.asr = asr
        self.tts = tts
        self._runners: dict[int, CampaignRunner] = {}
        self._lock = threading.RLock()
        self.modem: AtModem | None = None

    def resume_running_campaigns(self) -> int:
        """После перезапуска сервиса возобновляет кампании, бывшие в работе."""
        resumed = 0
        for row in self.db.query("SELECT id, mode FROM campaigns WHERE status='running'"):
            try:
                mode = row["mode"] if row["mode"] in ("demo", "modem") else self.settings.get("default_mode")
                self.start_campaign(int(row["id"]), mode)
                resumed += 1
            except Exception as exc:  # noqa: BLE001
                self.log(int(row["id"]), "error", f"Не удалось возобновить обзвон: {exc}")
        if resumed:
            self.log(None, "info", f"Возобновлено кампаний после перезапуска: {resumed}")
        return resumed

    # ------------------------------------------------------------------ log/events
    def log(self, campaign_id: int | None, level: str, message: str) -> None:
        try:
            self.db.execute(
                "INSERT INTO events(campaign_id, ts, level, message) VALUES (?,?,?,?)",
                (campaign_id, now_iso(), level, message[:2000]),
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ modem helpers
    def audio_io(self) -> AudioIO:
        return AudioIO(
            play_device=self.settings.get("audio_play_device"),
            rec_device=self.settings.get("audio_record_device"),
            rate=self.settings.get_int("audio_rate", 8000),
            backend=self.settings.get("audio_backend", "auto"),
        )

    def get_modem(self) -> AtModem:
        port = self.settings.get("modem_port").strip()
        if not port:
            raise ModemError("Не выбран порт модема (Настройки → Модем)")
        if self.modem and self.modem.port == port and self.modem.channel:
            return self.modem
        self.close_modem()
        modem = AtModem(port, self.settings.get_int("modem_baud", 115200),
                        self.settings.get("ussd_balance", "*100#"), self.audio_io(),
                        logger=lambda level, msg: self.log(None, level, msg))
        modem.open()
        self.modem = modem
        return modem

    def close_modem(self) -> None:
        if self.modem:
            try:
                self.modem.close()
            except Exception:  # noqa: BLE001
                pass
            self.modem = None

    def modem_status(self, probe: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "port": self.settings.get("modem_port"),
            "operator": self.settings.get("operator"),
            "default_mode": self.settings.get("default_mode"),
            "connected": False,
            "error": "",
        }
        if self.settings.get("default_mode") == "demo":
            data.update({"connected": True, "demo": True, "model": "Демо-эмулятор (без модема)",
                         "sim_ready": True, "signal": 25,
                         "operator": self.settings.get("operator") or "МегаФон"})
            return data
        if not probe and not self.modem:
            data["error"] = "Модем не открыт"
            return data
        try:
            modem = self.get_modem()
            info = modem.info
            data.update({
                "connected": info.connected, "model": info.model, "imei": info.imei,
                "sim_ready": info.sim_ready, "sim_number": info.sim_number,
                "operator": info.operator or self.settings.get("operator"),
                "signal": info.signal, "balance": info.balance, "error": info.error,
            })
        except Exception as exc:  # noqa: BLE001
            data["error"] = str(exc)
        return data

    # ------------------------------------------------------------------ campaigns
    def campaign(self, campaign_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
        if not row:
            return None
        data = dict(row)
        data["questions"] = [
            dict(q) | {"options": json.loads(q["options"] or "[]")}
            for q in self.db.query("SELECT * FROM questions WHERE campaign_id=? ORDER BY position, id",
                                   (campaign_id,))
        ]
        return data

    def stats(self, campaign_id: int) -> dict[str, Any]:
        counts = {row["status"]: row["n"] for row in self.db.query(
            "SELECT status, COUNT(*) AS n FROM contacts WHERE campaign_id=? GROUP BY status",
            (campaign_id,))}
        total = sum(counts.values())
        calls = self.db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration_sec),0) AS dur FROM calls WHERE campaign_id=?",
            (campaign_id,)) or {"n": 0, "dur": 0}
        answered = self.db.scalar(
            "SELECT COUNT(*) FROM calls WHERE campaign_id=? AND outcome='answered'", (campaign_id,), 0)
        answers = self.db.query_one(
            "SELECT COUNT(*) AS n, COALESCE(AVG(confidence),0) AS conf FROM answers WHERE campaign_id=?",
            (campaign_id,)) or {"n": 0, "conf": 0}
        runner = self._runners.get(campaign_id)
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "calling": counts.get("calling", 0),
            "done": counts.get("done", 0),
            "refused": counts.get("refused", 0),
            "no_answer": counts.get("no_answer", 0),
            "failed": counts.get("failed", 0),
            "dnc": counts.get("dnc", 0),
            "calls": calls["n"],
            "talk_seconds": round(float(calls["dur"] or 0), 1),
            "answered": answered,
            "answers": answers["n"],
            "avg_confidence": round(float(answers["conf"] or 0), 3),
            "active": runner is not None and runner.alive,
            "progress": round(100 * (total - counts.get("pending", 0) - counts.get("calling", 0)) / total, 1)
            if total else 0.0,
        }

    def start_campaign(self, campaign_id: int, mode: str | None = None) -> dict[str, Any]:
        campaign = self.campaign(campaign_id)
        if not campaign:
            raise ValueError("Кампания не найдена")
        with self._lock:
            runner = self._runners.get(campaign_id)
            if runner and runner.alive:
                return {"ok": True, "already_running": True}
            mode = mode or campaign["mode"] or self.settings.get("default_mode", "demo")
            if mode not in ("modem", "demo"):
                raise ValueError("Неизвестный режим: " + mode)
            runner = CampaignRunner(self, campaign, mode)
            self._runners[campaign_id] = runner
            self.db.execute(
                "UPDATE campaigns SET status='running', started_at=COALESCE(started_at, ?), finished_at=NULL "
                "WHERE id=?", (now_iso(), campaign_id))
            runner.start()
        self.log(campaign_id, "success", f"Обзвон запущен в режиме {'ДЕМО' if mode == 'demo' else 'МОДЕМ'}")
        return {"ok": True, "mode": mode}

    def pause_campaign(self, campaign_id: int) -> dict[str, Any]:
        runner = self._runners.get(campaign_id)
        if runner:
            runner.pause()
            self.db.execute("UPDATE campaigns SET status='paused' WHERE id=?", (campaign_id,))
            self.log(campaign_id, "warning", "Обзвон поставлен на паузу")
            return {"ok": True, "status": "paused"}
        return {"ok": False, "error": "Кампания не запущена"}

    def resume_campaign(self, campaign_id: int) -> dict[str, Any]:
        runner = self._runners.get(campaign_id)
        if runner:
            runner.resume()
            self.db.execute("UPDATE campaigns SET status='running' WHERE id=?", (campaign_id,))
            self.log(campaign_id, "info", "Обзвон продолжен")
            return {"ok": True, "status": "running"}
        return {"ok": False, "error": "Кампания не запущена"}

    def stop_campaign(self, campaign_id: int) -> dict[str, Any]:
        runner = self._runners.get(campaign_id)
        if runner:
            runner.stop()
        self.db.execute("UPDATE campaigns SET status='stopped', finished_at=? WHERE id=?",
                        (now_iso(), campaign_id))
        self.log(campaign_id, "warning", "Обзвон остановлен оператором")
        return {"ok": True, "status": "stopped"}

    def recover_stale(self) -> None:
        """Чинит состояние после аварийного завершения процесса."""
        self.db.execute("UPDATE contacts SET status='pending' WHERE status='calling'")
        self.db.execute(
            "UPDATE calls SET outcome='aborted', ended_at=? WHERE outcome='running'", (now_iso(),))
        self.db.execute(
            "UPDATE campaigns SET status='paused' WHERE status='running' AND id NOT IN (SELECT campaign_id "
            "FROM contacts WHERE status='pending')")

    def reset_failed(self, campaign_id: int) -> int:
        """Возвращает в очередь контакты с неудачными попытками."""
        cur = self.db.execute(
            "UPDATE contacts SET status='pending', attempts=0 WHERE campaign_id=? AND status IN ('failed','no_answer')",
            (campaign_id,))
        self.log(campaign_id, "info", f"Возвращено в очередь контактов: {max(0, cur)}")
        return max(0, cur)

    def shutdown(self) -> None:
        for campaign_id, runner in list(self._runners.items()):
            runner.stop()
        self.close_modem()


class CampaignRunner(threading.Thread):
    """Поток обзвона одной кампании."""

    def __init__(self, engine: CallEngine, campaign: dict[str, Any], mode: str):
        super().__init__(daemon=True, name=f"campaign-{campaign['id']}")
        self.engine = engine
        self.db = engine.db
        self.settings = engine.settings
        self.campaign = campaign
        self.mode = mode
        self.campaign_id = int(campaign["id"])
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._pause.set()
        self.alive = True
        # Режим демо по умолчанию ускорен (demo_speed), чтобы результаты
        # появлялись в таблице почти сразу. Для реального модема demo_speed=1.
        self._demo_speed_factor = 0.2 if mode == "demo" else 1.0

    # ------------------------------------------------------------------ control
    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    def stop(self) -> None:
        self._stop.set()
        self._pause.set()

    def _wait_if_paused(self) -> None:
        while not self._pause.is_set():
            if self._stop.is_set():
                return
            time.sleep(0.2)

    def _log(self, level: str, message: str) -> None:
        self.engine.log(self.campaign_id, level, message)

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:  # noqa: C901 - линейный сценарий обзвона
        campaign = self.engine.campaign(self.campaign_id) or self.campaign
        questions = campaign.get("questions") or []
        record_answers = bool(campaign.get("record_answers", 1))
        retry_limit = int(campaign.get("retry_limit") or 0)
        self._log("info", f"Кампания «{campaign['name']}»: {len(questions)} вопрос(ов), "
                          f"контактов в очереди: {self._pending_count()}")

        modem: AtModem | None = None
        if self.mode == "modem":
            try:
                modem = self.engine.get_modem()
                if not modem.info.sim_ready:
                    self._log("error", "SIM не готова (PIN-код или нет карты). Обзвон остановлен.")
                    self.db.execute("UPDATE campaigns SET status='paused' WHERE id=?", (self.campaign_id,))
                    self.alive = False
                    return
            except Exception as exc:  # noqa: BLE001
                self._log("error", f"Не удалось открыть модем: {exc}")
                self.db.execute("UPDATE campaigns SET status='paused' WHERE id=?", (self.campaign_id,))
                self.alive = False
                return
            if self.mode == "modem":
                from .telephony.modem import aplay_available, arecord_available

                if not arecord_available():
                    self._log("error", "Не найдена утилита arecord (пакет alsa-utils): невозможно записывать "
                                       "ответы абонентов. Установите alsa-utils и укажите аудиоустройства.")
                if not aplay_available():
                    self._log("warning", "Не найдена утилита aplay (alsa-utils): вопросы не будут озвучены.")

        max_concurrent = 1  # один модем = один одновременный звонок
        self._log("info", f"Режим: {'МОДЕМ ' + modem.port if modem else 'ДЕМО'}; одновременно звонков: {max_concurrent}")

        try:
            while not self._stop.is_set():
                self._wait_if_paused()
                if self._stop.is_set():
                    break
                if not within_calling_hours(self.settings.get("calling_hours")):
                    self._log("warning", "Вне разрешённого времени звонков — ожидание 60 с")
                    if self._sleep(60):
                        break
                    continue
                contact = self._next_contact()
                if not contact:
                    self._log("success", "Очередь пуста — кампания завершена")
                    self.db.execute(
                        "UPDATE campaigns SET status='completed', finished_at=? WHERE id=?",
                        (now_iso(), self.campaign_id))
                    self.alive = False
                    return
                self._process_contact(contact, campaign, questions, modem, record_answers, retry_limit)
        finally:
            self.alive = False
            if modem:
                try:
                    modem.hangup()
                except Exception:  # noqa: BLE001
                    pass

    def _sleep(self, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set():
                return True
            time.sleep(0.1)
        return False

    def _demo_speed(self) -> float:
        return self._demo_speed_factor

    def _pending_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND status='pending'",
                                  (self.campaign_id,), 0))

    def _next_contact(self) -> dict[str, Any] | None:
        respect_dnc = self.settings.get_bool("respect_dnc", True)
        if respect_dnc:
            row = self.db.query_one(
                """SELECT c.* FROM contacts c
                   WHERE c.campaign_id=? AND c.status='pending'
                     AND c.phone NOT IN (SELECT phone FROM do_not_call)
                   ORDER BY c.id LIMIT 1""", (self.campaign_id,))
            if not row:
                # Помечаем оставшиеся как «не звонить», чтобы не зациклиться.
                self.db.execute(
                    """UPDATE contacts SET status='dnc'
                       WHERE campaign_id=? AND status='pending'
                         AND phone IN (SELECT phone FROM do_not_call)""", (self.campaign_id,))
        else:
            row = self.db.query_one(
                "SELECT * FROM contacts WHERE campaign_id=? AND status='pending' ORDER BY id LIMIT 1",
                (self.campaign_id,))
        return dict(row) if row else None

    # ------------------------------------------------------------------ contact
    def _process_contact(self, contact: dict[str, Any], campaign: dict[str, Any],
                         questions: list[dict[str, Any]], modem: AtModem | None,
                         record_answers: bool, retry_limit: int) -> None:
        if self._stop.is_set():
            return
        attempt = int(contact.get("attempts") or 0) + 1
        self.db.execute(
            "UPDATE contacts SET status='calling', attempts=?, last_attempt_at=? WHERE id=?",
            (attempt, now_iso(), contact["id"]))
        call_id = self.db.execute(
            "INSERT INTO calls(campaign_id, contact_id, attempt, started_at, outcome, engine) "
            "VALUES (?,?,?,?, 'running', ?)",
            (self.campaign_id, contact["id"], attempt, now_iso(), self.mode))
        self._log("info", f"Звонок #{call_id}: {contact.get('name') or 'без имени'} → {contact['phone']} "
                          f"(попытка {attempt})")
        started = time.time()

        # ---------------------------------------------------------------- conversation
        ctx = ConversationContext(
            db=self.db, campaign=campaign, contact=contact, call_id=call_id, questions=questions,
            attempt=attempt, log=self._log, stop_flag=lambda: self._stop.is_set(),
        )
        result: CallResult
        try:
            if self.mode == "demo":
                result = self._demo_call(ctx, record_answers)
            else:
                assert modem is not None
                result = self._modem_call(ctx, modem, record_answers)
        except Exception as exc:  # noqa: BLE001 - один сбой не должен останавливать обзвон
            result = CallResult(outcome="failed", error=str(exc))
            self._log("error", f"Звонок #{call_id} прерван: {exc}")

        duration = round(time.time() - started, 1)
        if self._stop.is_set() and result.outcome == "answered":
            result.outcome = "aborted"

        ended = now_iso()
        self.db.execute(
            "UPDATE calls SET ended_at=?, outcome=?, duration_sec=?, recording_path=?, transcript=?, error=? "
            "WHERE id=?",
            (ended, result.outcome, duration, result.recording, "\n".join(ctx.transcript_parts)[:20000],
             result.error[:1000], call_id))

        # ---------------------------------------------------------------- contact status
        if result.refused:
            status = "refused"
            self.db.execute("INSERT OR REPLACE INTO do_not_call(phone, reason, created_at) VALUES (?,?,?)",
                            (contact["phone"], f"Отказ при обзвоне #{call_id}", now_iso()))
            self._log("warning", f"{contact['phone']}: абонент попросил больше не звонить — добавлен в список «не звонить»")
        elif result.outcome == "answered":
            status = "done"
        elif result.outcome in ("no_answer", "busy") and attempt <= retry_limit:
            status = "pending"
            self._log("info", f"{contact['phone']}: {result.outcome}, повтор через несколько секунд")
        elif result.outcome in ("no_answer", "busy"):
            status = "no_answer"
        elif result.outcome == "aborted":
            status = "pending"
        else:
            status = "failed"
        self.db.execute("UPDATE contacts SET status=? WHERE id=?", (status, contact["id"]))
        self._log("success" if status == "done" else "info",
                  f"Звонок #{call_id} завершён: {result.outcome}, {duration} c, ответов: {len(ctx.answers)}")
        if status == "pending":
            self._sleep(1.0)

    # ------------------------------------------------------------------ demo mode
    def _demo_call(self, ctx: ConversationContext, record_answers: bool) -> CallResult:  # noqa: C901
        speed = self._demo_speed()
        rnd = random.Random()

        def step(seconds: float) -> None:
            if speed > 0:
                self._sleep(max(0.05, seconds * speed))

        self._log("info", f"[ДЕМО] дозвон {ctx.contact['phone']}...")
        step(3.0)
        if rnd.random() < 0.16:  # часть абонентов не берёт трубку
            ctx.transcript_parts.append("[автоответчик] Абонент не отвечает")
            self._log("warning", f"[ДЕМО] {ctx.contact['phone']}: не отвечает")
            return CallResult(outcome="no_answer", transcript="Абонент не отвечает")
        if rnd.random() < 0.05:
            self._log("warning", f"[ДЕМО] {ctx.contact['phone']}: занято")
            return CallResult(outcome="busy")

        self._log("success", f"[ДЕМО] {ctx.contact['name'] or ctx.contact['phone']} взял(а) трубку")
        ctx.transcript_parts.append("[соединение установлено]")
        recording_files: list[Path] = []
        if ctx.campaign.get("intro_text"):
            ctx.transcript_parts.append(f"РОБОТ: {ctx.campaign['intro_text']}")
            step(4.0)

        min_conf = self.settings.get_float("min_confidence", 0.45)
        dtmf_enabled = bool(ctx.campaign.get("dtmf_enabled", 1))
        refused = False
        for question in ctx.questions:
            if self._stop.is_set():
                return CallResult(outcome="aborted", refused=refused)
            if refused and question.get("type") != "free":
                continue
            ctx.transcript_parts.append(f"РОБОТ: {question['text']}")
            step(2.5)
            demo = self._demo_reply(question, rnd)
            if demo is None:  # абонент отказался от дальнейших звонков
                ctx.transcript_parts.append("АБОНЕНТ: не звоните мне больше")
                refused = True
                continue
            if dtmf_enabled and question.get("type") in ("yes_no", "choice") and rnd.random() < 0.25:
                digit = "1" if demo["parsed_hint"] == "yes" else "2"
                raw = f"[клавиша {digit}]"
                parsed = parse_answer(question, "", from_dtmf=True, digit=digit,
                                      min_confidence=min_conf)
                ctx.transcript_parts.append(f"АБОНЕНТ: [нажата клавиша {digit}]")
            else:
                raw = demo["text"]
                parsed = parse_answer(question, raw, min_confidence=min_conf)
                ctx.transcript_parts.append(f"АБОНЕНТ: {raw}")

            if record_answers:
                audio = RECORDINGS_DIR / f"call{ctx.call_id}_q{question['id']}.wav"
                if audio.exists():
                    try:
                        audio.unlink()
                    except OSError:
                        pass
                write_silence(audio, rnd.uniform(1.5, 3.0))
                setattr(parsed, "audio_path", str(audio))
                recording_files.append(audio)
            ctx.save_answer(question, parsed, raw)
            if contains_refusal(raw):
                self._log("warning", f"[ДЕМО] отказ от дальнейших звонков: {raw}")
                break
            step(1.5)

        if ctx.campaign.get("outro_text"):
            ctx.transcript_parts.append(f"РОБОТ: {ctx.campaign['outro_text']}")
            step(3.0)

        recording = ""
        if recording_files and record_answers:
            target = RECORDINGS_DIR / f"call{ctx.call_id}.wav"
            if concat_wavs(recording_files, target):
                recording = str(target)
        if refused:
            return CallResult(outcome="answered", refused=True, recording=recording,
                              transcript="\n".join(ctx.transcript_parts))
        self._log("success", f"[ДЕМО] {ctx.contact['phone']}: опрос завершён")
        return CallResult(outcome="answered", recording=recording,
                          transcript="\n".join(ctx.transcript_parts))

    def _demo_reply(self, question: dict[str, Any], rnd: random.Random) -> dict[str, str] | None:
        qtype = question.get("type")
        options = question.get("options") or []
        if rnd.random() < 0.015:
            return None  # «не звоните больше»
        if rnd.random() < 0.09:
            # Шум/неразборчивая речь: ответ не распознается уверенно.
            return {"text": rnd.choice(DEMO_UNCLEAR), "parsed_hint": "unclear"}
        if qtype == "yes_no":
            if rnd.random() < 0.35:
                return {"text": rnd.choice(DEMO_NO), "parsed_hint": "no"}
            return {"text": rnd.choice(DEMO_YES), "parsed_hint": "yes"}
        if qtype == "choice":
            options = options or ["Первый вариант", "Второй вариант", "Третий вариант"]
            idx = rnd.randrange(len(options))
            phrases = [f"{idx + 1}", f"вариант {idx + 1}", options[idx]]
            return {"text": rnd.choice(phrases), "parsed_hint": str(idx + 1)}
        if qtype == "rating":
            return {"text": rnd.choice(["пять", "четыре", "5", "4", "3", "на пятерку"]), "parsed_hint": "5"}
        if qtype == "number":
            return {"text": rnd.choice(["сорок два", "35", "двадцать пять", "56"]), "parsed_hint": "42"}
        return {"text": rnd.choice(DEMO_FREE), "parsed_hint": "free"}

    # ------------------------------------------------------------------ modem mode
    def _modem_call(self, ctx: ConversationContext, modem: AtModem, record_answers: bool) -> CallResult:
        audio = self.engine.audio_io()
        answer_seconds = float(ctx.campaign.get("answer_seconds") or 6)
        timeout = float(ctx.campaign.get("call_timeout") or 40)
        min_conf = self.settings.get_float("min_confidence", 0.45)
        recording_path = ""
        answer_files: list[Path] = []

        self._log("info", f"Набор {ctx.contact['phone']}...")
        outcome = modem.dial(ctx.contact["phone"], timeout=timeout)
        if outcome != "answered":
            reason = {"busy": "занято", "no_answer": "нет ответа", "failed": "ошибка набора"}.get(outcome, outcome)
            self._log("warning", f"Звонок на {ctx.contact['phone']}: {reason}")
            return CallResult(outcome=outcome, error=reason)
        self._log("success", f"Абонент {ctx.contact['phone']} ответил")
        ctx.transcript_parts.append("[соединение установлено]")

        try:
            modem.hw_audio_mode(True)
            # Приветствие
            intro = ctx.campaign.get("intro_text") or ""
            intro_audio = ctx.campaign.get("intro_audio")
            self._speak(modem, audio, intro, intro_audio)
            if intro:
                ctx.transcript_parts.append(f"РОБОТ: {intro}")

            for question in ctx.questions:
                if self._stop.is_set():
                    return CallResult(outcome="aborted", refused=False)
                ctx.transcript_parts.append(f"РОБОТ: {question['text']}")
                self._speak(modem, audio, question["text"], question.get("audio_path"))
                max_sec = float(question.get("max_seconds") or answer_seconds)
                answer_wav = RECORDINGS_DIR / f"call{ctx.call_id}_q{question['id']}.wav" if record_answers else None
                raw = ""
                parsed = None
                if answer_wav is not None:
                    try:
                        audio.record(answer_wav, max_sec, stop=lambda: self._stop.is_set())
                        if not answer_wav.exists() or answer_wav.stat().st_size <= 44:
                            answer_wav = None
                    except Exception as exc:  # noqa: BLE001
                        self._log("warning", f"Не удалось записать ответ: {exc}")
                        answer_wav = None
                dtmf = modem.take_dtmf()
                if dtmf and ctx.campaign.get("dtmf_enabled", 1):
                    parsed = parse_answer(question, "", from_dtmf=True, digit=dtmf[0], min_confidence=min_conf)
                    raw = f"[клавиша {dtmf[0]}]"
                    ctx.transcript_parts.append(f"АБОНЕНТ: [клавиша {dtmf[0]}]")
                elif answer_wav is not None:
                    try:
                        transcription = self.engine.asr.transcribe(str(answer_wav),
                                                                 preferred=self.settings.get("asr_engine"))
                        raw = transcription.text.strip()
                        self._log("info", f"Распознано: «{raw or '—'}» ({transcription.engine}, "
                                           f"{transcription.confidence:.0%})")
                    except ASRError as exc:
                        self._log("error", f"Распознавание не удалось: {exc}")
                        raw = ""
                    ctx.transcript_parts.append(f"АБОНЕНТ: {raw or '[не распознано]'}")
                    parsed = parse_answer(question, raw, min_confidence=min_conf)
                    if not parsed.matched and raw:
                        parsed.confidence = min(parsed.confidence, 0.3)
                else:
                    parsed = parse_answer(question, "", min_confidence=min_conf)

                if parsed is None:
                    parsed = parse_answer(question, "", min_confidence=min_conf)
                if answer_wav is not None:
                    setattr(parsed, "audio_path", str(answer_wav))
                    answer_files.append(answer_wav)
                ctx.save_answer(question, parsed, raw)
                if parsed.value == "refused" or contains_refusal(raw):
                    ctx.transcript_parts.append("АБОНЕНТ: просит больше не звонить")
                    if answer_files:
                        target = RECORDINGS_DIR / f"call{ctx.call_id}.wav"
                        if concat_wavs(answer_files, target):
                            recording_path = str(target)
                    return CallResult(outcome="answered", refused=True, recording=recording_path,
                                      transcript="\n".join(ctx.transcript_parts))
                # Пауза, чтобы абонент не «перебивал» следующий вопрос
                self._sleep(0.4)

            outro = ctx.campaign.get("outro_text") or ""
            if outro:
                ctx.transcript_parts.append(f"РОБОТ: {outro}")
                self._speak(modem, audio, outro, ctx.campaign.get("outro_audio"))
            if answer_files:
                target = RECORDINGS_DIR / f"call{ctx.call_id}.wav"
                if concat_wavs(answer_files, target):
                    recording_path = str(target)
            self._log("success", f"Опрос {ctx.contact['phone']} завершён")
            return CallResult(outcome="answered", recording=recording_path,
                              transcript="\n".join(ctx.transcript_parts))
        finally:
            try:
                modem.hangup()
            except Exception:  # noqa: BLE001
                pass

    def _speak(self, modem: AtModem, audio: AudioIO, text: str, audio_path: str | None) -> None:
        """Проигрывает приветствие/вопрос: загруженный файл или синтез речи."""
        path: Path | None = None
        if audio_path and Path(audio_path).exists():
            path = Path(audio_path)
        elif text.strip():
            try:
                path = self.engine.tts.synthesize(text)
            except Exception as exc:  # noqa: BLE001
                self._log("error", f"Озвучивание недоступно: {exc}")
                return
        if path is not None:
            try:
                audio.play(path, stop=lambda: self._stop.is_set())
            except Exception as exc:  # noqa: BLE001
                self._log("error", f"Не удалось воспроизвести: {exc}")
