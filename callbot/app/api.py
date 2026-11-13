"""HTTP API сервиса «Обзвон+» (FastAPI)."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from .answers import answer_is_low_confidence, parse_answer
from .config import APP_VERSION, PROMPTS_DIR
from .settings import SettingsStore
from .db import Database, now_iso, row_to_dict
from .engine import CallEngine, within_calling_hours
from .exporting import (campaign_questions, export_csv, export_json, export_transcripts,
                        export_xlsx, safe_filename)
from .phones import normalize_phone, parse_contacts_csv
from .telephony.modem import audio_devices, decode_ussd, probe_ports
from .tts import TTSService, save_uploaded_audio

QUESTION_TYPES = {"yes_no", "choice", "rating", "number", "free"}

router = APIRouter()


# --------------------------------------------------------------------------- accessors
def db_of(request: Request) -> Database:
    return request.app.state.db


def settings_of(request: Request) -> SettingsStore:
    return request.app.state.settings


def engine_of(request: Request) -> CallEngine:
    return request.app.state.engine


def tts_of(request: Request) -> TTSService:
    return request.app.state.tts


# --------------------------------------------------------------------------- schemas
class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    greeting: str = ""
    intro_text: str = ""
    outro_text: str = ""
    retry_limit: int = Field(default=1, ge=0, le=10)
    call_timeout: int = Field(default=40, ge=10, le=300)
    answer_seconds: float = Field(default=6, ge=1, le=60)
    dtmf_enabled: bool = True
    record_answers: bool = True
    mode: Literal["demo", "modem"] = "demo"
    notes: str = ""


class CampaignPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    greeting: str | None = None
    intro_text: str | None = None
    outro_text: str | None = None
    retry_limit: int | None = Field(default=None, ge=0, le=10)
    call_timeout: int | None = Field(default=None, ge=10, le=300)
    answer_seconds: float | None = Field(default=None, ge=1, le=60)
    dtmf_enabled: bool | None = None
    record_answers: bool | None = None
    mode: Literal["demo", "modem"] | None = None
    notes: str | None = None


class QuestionIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    type: Literal["yes_no", "choice", "rating", "number", "free"] = "yes_no"
    options: list[str] = []
    max_seconds: float | None = Field(default=None, ge=1, le=60)
    required: bool = True
    audio_path: str | None = None

    @field_validator("options")
    @classmethod
    def clean_options(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v and v.strip()][:10]


class ContactIn(BaseModel):
    name: str = ""
    phone: str
    external_id: str = ""

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str) -> str:
        try:
            return normalize_phone(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class SettingsIn(BaseModel):
    values: dict[str, Any]


class AnswerPatch(BaseModel):
    value: str | None = None
    value_display: str | None = None
    reviewed: bool | None = None


class RecalcIn(BaseModel):
    call_id: int | None = None


# --------------------------------------------------------------------------- helpers
def require_campaign(db: Database, campaign_id: int) -> dict[str, Any]:
    row = row_to_dict(db.query_one("SELECT * FROM campaigns WHERE id=?", (campaign_id,)))
    if not row:
        raise HTTPException(404, "Кампания не найдена")
    return row


def campaign_dict(db: Database, campaign_id: int) -> dict[str, Any]:
    campaign = require_campaign(db, campaign_id)
    campaign["questions"] = [
        row_to_dict(r) for r in db.query(
            "SELECT * FROM questions WHERE campaign_id=? ORDER BY position, id", (campaign_id,))
    ]
    return campaign


# --------------------------------------------------------------------------- meta
@router.get("/meta")
def meta(request: Request) -> dict[str, Any]:
    db, settings = db_of(request), settings_of(request)
    return {
        "version": APP_VERSION,
        "demo_mode": settings.get("default_mode") == "demo",
        "calling_hours": settings.get("calling_hours"),
        "within_calling_hours": within_calling_hours(settings.get("calling_hours")),
        "asr": request.app.state.asr.status(),
        "tts": tts_of(request).status(),
        "modem_mode": settings.get("default_mode"),
        "question_types": sorted(QUESTION_TYPES),
        "campaigns": db.scalar("SELECT COUNT(*) FROM campaigns", (), 0),
        "contacts": db.scalar("SELECT COUNT(*) FROM contacts", (), 0),
    }


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    db = db_of(request)
    campaigns = [row_to_dict(r) for r in db.query(
        "SELECT * FROM campaigns ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, "
        "id DESC LIMIT 50")]
    for campaign in campaigns:
        campaign["stats"] = engine_of(request).stats(campaign["id"])
    totals = {
        "campaigns": len(campaigns),
        "contacts": db.scalar("SELECT COUNT(*) FROM contacts", (), 0),
        "calls": db.scalar("SELECT COUNT(*) FROM calls", (), 0),
        "answers": db.scalar("SELECT COUNT(*) FROM answers", (), 0),
        "talk_seconds": round(float(db.scalar("SELECT COALESCE(SUM(duration_sec),0) FROM calls", (), 0)), 1),
        "answered": db.scalar("SELECT COUNT(*) FROM calls WHERE outcome='answered'", (), 0),
        "avg_confidence": round(float(db.scalar("SELECT COALESCE(AVG(confidence),0) FROM answers", (), 0)), 3),
        "dnc": db.scalar("SELECT COUNT(*) FROM do_not_call", (), 0),
    }
    last_calls = [row_to_dict(r) for r in db.query(
        """SELECT c.id, c.started_at, c.outcome, c.duration_sec, c.engine, ct.name, ct.phone,
                  c.campaign_id, cp.name AS campaign_name
           FROM calls c JOIN contacts ct ON ct.id=c.contact_id JOIN campaigns cp ON cp.id=c.campaign_id
           ORDER BY c.id DESC LIMIT 12""")]
    return {"totals": totals, "campaigns": campaigns, "last_calls": last_calls,
            "today": _today_stats(db)}


def _today_stats(db: Database) -> dict[str, Any]:
    today = now_iso()[:10]
    return {
        "calls": db.scalar("SELECT COUNT(*) FROM calls WHERE substr(started_at,1,10)=?", (today,), 0),
        "answered": db.scalar("SELECT COUNT(*) FROM calls WHERE outcome='answered' AND substr(started_at,1,10)=?",
                              (today,), 0),
        "answers": db.scalar("SELECT COUNT(*) FROM answers WHERE substr(created_at,1,10)=?", (today,), 0),
    }


# --------------------------------------------------------------------------- campaigns
@router.get("/campaigns")
def list_campaigns(request: Request) -> list[dict[str, Any]]:
    db, engine = db_of(request), engine_of(request)
    rows = [row_to_dict(r) for r in db.query("SELECT * FROM campaigns ORDER BY id DESC")]
    for row in rows:
        row["stats"] = engine.stats(row["id"])
        row["questions_count"] = db.scalar("SELECT COUNT(*) FROM questions WHERE campaign_id=?", (row["id"],), 0)
    return rows


@router.post("/campaigns", status_code=201)
def create_campaign(request: Request, payload: CampaignIn) -> dict[str, Any]:
    db = db_of(request)
    campaign_id = db.execute(
        """INSERT INTO campaigns(name, status, mode, greeting, intro_text, outro_text, retry_limit,
                                 call_timeout, answer_seconds, dtmf_enabled, record_answers, notes, created_at)
           VALUES (?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (payload.name.strip(), payload.mode, payload.greeting, payload.intro_text, payload.outro_text,
         payload.retry_limit, payload.call_timeout, payload.answer_seconds, int(payload.dtmf_enabled),
         int(payload.record_answers), payload.notes, now_iso()))
    engine_of(request).log(campaign_id, "info", f"Создана кампания «{payload.name}»")
    return campaign_dict(db, campaign_id)


@router.get("/campaigns/{campaign_id}")
def get_campaign(request: Request, campaign_id: int) -> dict[str, Any]:
    db, engine = db_of(request), engine_of(request)
    campaign = campaign_dict(db, campaign_id)
    campaign["stats"] = engine.stats(campaign_id)
    return campaign


@router.patch("/campaigns/{campaign_id}")
def patch_campaign(request: Request, campaign_id: int, payload: CampaignPatch) -> dict[str, Any]:
    db = db_of(request)
    require_campaign(db, campaign_id)
    fields = payload.model_dump(exclude_none=True)
    bool_fields = {"dtmf_enabled", "record_answers"}
    if fields:
        sets, values = [], []
        for key, value in fields.items():
            sets.append(f"{key}=?")
            values.append(int(value) if key in bool_fields else value)
        values.append(campaign_id)
        db.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id=?", values)
    return campaign_dict(db, campaign_id)


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(request: Request, campaign_id: int) -> dict[str, Any]:
    db = db_of(request)
    require_campaign(db, campaign_id)
    engine_of(request).stop_campaign(campaign_id)
    db.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/start")
def start_campaign(request: Request, campaign_id: int, mode: str | None = Query(default=None)) -> dict[str, Any]:
    engine = engine_of(request)
    require_campaign(db_of(request), campaign_id)
    if not db_of(request).scalar("SELECT COUNT(*) FROM questions WHERE campaign_id=?", (campaign_id,), 0):
        raise HTTPException(400, "Добавьте хотя бы один вопрос в сценарий")
    q_count = db_of(request).scalar("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND status='pending'",
                                    (campaign_id,), 0)
    if not q_count:
        raise HTTPException(400, "Нет контактов в очереди на обзвон")
    try:
        result = engine.start_campaign(campaign_id, mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(request: Request, campaign_id: int) -> dict[str, Any]:
    return engine_of(request).pause_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/resume")
def resume_campaign(request: Request, campaign_id: int) -> dict[str, Any]:
    return engine_of(request).resume_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/stop")
def stop_campaign(request: Request, campaign_id: int) -> dict[str, Any]:
    return engine_of(request).stop_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/reset-failed")
def reset_failed(request: Request, campaign_id: int) -> dict[str, Any]:
    require_campaign(db_of(request), campaign_id)
    count = engine_of(request).reset_failed(campaign_id)
    return {"ok": True, "updated": count}


@router.get("/campaigns/{campaign_id}/stats")
def campaign_stats(request: Request, campaign_id: int) -> dict[str, Any]:
    require_campaign(db_of(request), campaign_id)
    return engine_of(request).stats(campaign_id)


# --------------------------------------------------------------------------- questions
@router.get("/campaigns/{campaign_id}/questions")
def list_questions(request: Request, campaign_id: int) -> list[dict[str, Any]]:
    require_campaign(db_of(request), campaign_id)
    return [row_to_dict(r) for r in db_of(request).query(
        "SELECT * FROM questions WHERE campaign_id=? ORDER BY position, id", (campaign_id,))]


@router.post("/campaigns/{campaign_id}/questions", status_code=201)
def add_question(request: Request, campaign_id: int, payload: QuestionIn) -> dict[str, Any]:
    db = db_of(request)
    require_campaign(db, campaign_id)
    if payload.type == "choice" and len(payload.options) < 2:
        raise HTTPException(400, "Для вопроса с вариантами нужно минимум 2 варианта")
    position = db.scalar("SELECT COALESCE(MAX(position), -1) + 1 FROM questions WHERE campaign_id=?",
                         (campaign_id,), 0)
    question_id = db.execute(
        """INSERT INTO questions(campaign_id, position, text, type, options, max_seconds, required, audio_path)
           VALUES (?,?,?,?,?,?,?,?)""",
        (campaign_id, position, payload.text.strip(), payload.type,
         json.dumps(payload.options, ensure_ascii=False), payload.max_seconds,
         int(payload.required), payload.audio_path))
    return row_to_dict(db.query_one("SELECT * FROM questions WHERE id=?", (question_id,)))


@router.patch("/questions/{question_id}")
def patch_question(request: Request, question_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    db = db_of(request)
    question = db.query_one("SELECT * FROM questions WHERE id=?", (question_id,))
    if not question:
        raise HTTPException(404, "Вопрос не найден")
    allowed = {"text", "type", "options", "max_seconds", "required", "audio_path", "position"}
    updates: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key == "type" and value not in QUESTION_TYPES:
            raise HTTPException(400, "Неизвестный тип вопроса")
        if key == "options":
            value = json.dumps([str(v) for v in (value or [])], ensure_ascii=False)
        if key == "required":
            value = int(bool(value))
        updates[key] = value
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE questions SET {sets} WHERE id=?", [*updates.values(), question_id])
    return row_to_dict(db.query_one("SELECT * FROM questions WHERE id=?", (question_id,)))


@router.delete("/questions/{question_id}")
def delete_question(request: Request, question_id: int) -> dict[str, Any]:
    db = db_of(request)
    row = db.query_one("SELECT campaign_id FROM questions WHERE id=?", (question_id,))
    if not row:
        raise HTTPException(404, "Вопрос не найден")
    db.execute("DELETE FROM questions WHERE id=?", (question_id,))
    _renumber(db, row["campaign_id"])
    return {"ok": True}


def _renumber(db: Database, campaign_id: int) -> None:
    ids = [r["id"] for r in db.query("SELECT id FROM questions WHERE campaign_id=? ORDER BY position, id",
                                     (campaign_id,))]
    for position, question_id in enumerate(ids):
        db.execute("UPDATE questions SET position=? WHERE id=?", (position, question_id))


@router.post("/questions/reorder")
def reorder_questions(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ids = payload.get("ids") or []
    db = db_of(request)
    for position, question_id in enumerate(ids):
        db.execute("UPDATE questions SET position=? WHERE id=?", (position, int(question_id)))
    return {"ok": True}


@router.post("/questions/{question_id}/audio")
async def upload_question_audio(request: Request, question_id: int, file: UploadFile = File(...)) -> dict[str, Any]:
    db = db_of(request)
    question = db.query_one("SELECT * FROM questions WHERE id=?", (question_id,))
    if not question:
        raise HTTPException(404, "Вопрос не найден")
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 30 МБ")
    try:
        path = save_uploaded_audio(file.filename or "audio.wav", data, PROMPTS_DIR)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.execute("UPDATE questions SET audio_path=? WHERE id=?", (str(path), question_id))
    return row_to_dict(db.query_one("SELECT * FROM questions WHERE id=?", (question_id,)))


@router.get("/questions/{question_id}/audio-file")
def question_audio(request: Request, question_id: int) -> FileResponse:
    row = db_of(request).query_one("SELECT audio_path FROM questions WHERE id=?", (question_id,))
    if not row or not row["audio_path"] or not Path(row["audio_path"]).exists():
        raise HTTPException(404, "У вопроса нет аудиофайла")
    return FileResponse(row["audio_path"], media_type="audio/wav")


@router.delete("/questions/{question_id}/audio")
def delete_question_audio(request: Request, question_id: int) -> dict[str, Any]:
    db = db_of(request)
    db.execute("UPDATE questions SET audio_path=NULL WHERE id=?", (question_id,))
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/audio")
async def upload_campaign_audio(request: Request, campaign_id: int, slot: str = Form("intro"),
                                file: UploadFile = File(...)) -> dict[str, Any]:
    if slot not in ("intro", "outro"):
        raise HTTPException(400, "slot должен быть intro или outro")
    db = db_of(request)
    require_campaign(db, campaign_id)
    data = await file.read()
    try:
        path = save_uploaded_audio(file.filename or "audio.wav", data, PROMPTS_DIR)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    column = f"{slot}_audio"
    db.execute(f"UPDATE campaigns SET {column}=? WHERE id=?", (str(path), campaign_id))
    return {"ok": True, "path": str(path)}


@router.post("/tts/preview")
def tts_preview(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Пустой текст")
    try:
        path = tts_of(request).synthesize(text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Синтез речи недоступен: {exc}") from exc
    return FileResponse(path, media_type="audio/wav", filename=Path(path).name)


@router.get("/tts/status")
def tts_status(request: Request) -> dict[str, Any]:
    return tts_of(request).status()


# --------------------------------------------------------------------------- contacts
@router.get("/campaigns/{campaign_id}/contacts")
def list_contacts(request: Request, campaign_id: int, status: str | None = None,
                  limit: int = Query(default=500, le=5000), offset: int = 0) -> dict[str, Any]:
    db = db_of(request)
    require_campaign(db, campaign_id)
    where = "campaign_id=?"
    params: list[Any] = [campaign_id]
    if status:
        where += " AND status=?"
        params.append(status)
    total = db.scalar(f"SELECT COUNT(*) FROM contacts WHERE {where}", params, 0)
    rows = [row_to_dict(r) for r in db.query(
        f"SELECT * FROM contacts WHERE {where} ORDER BY id LIMIT ? OFFSET ?", [*params, limit, offset])]
    return {"rows": rows, "total": total}


@router.post("/campaigns/{campaign_id}/contacts", status_code=201)
def add_contact(request: Request, campaign_id: int, payload: ContactIn) -> dict[str, Any]:
    db = db_of(request)
    require_campaign(db, campaign_id)
    contact_id = db.execute(
        "INSERT INTO contacts(campaign_id, name, phone, external_id, status, created_at) VALUES (?,?,?,?, 'pending', ?)",
        (campaign_id, payload.name.strip(), payload.phone, payload.external_id, now_iso()))
    return row_to_dict(db.query_one("SELECT * FROM contacts WHERE id=?", (contact_id,)))


@router.delete("/contacts/{contact_id}")
def delete_contact(request: Request, contact_id: int) -> dict[str, Any]:
    db = db_of(request)
    if not db.query_one("SELECT id FROM contacts WHERE id=?", (contact_id,)):
        raise HTTPException(404, "Контакт не найден")
    db.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/contacts/import")
async def import_contacts(request: Request, campaign_id: int, file: UploadFile = File(...),
                          reset: bool = Form(False)) -> dict[str, Any]:
    db = db_of(request)
    require_campaign(db, campaign_id)
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 20 МБ")
    contacts, warnings = parse_contacts_csv(raw)
    if not contacts:
        return JSONResponse({"imported": 0, "skipped": len(warnings), "warnings": warnings[:50]},
                            status_code=400)
    if reset:
        db.execute("DELETE FROM contacts WHERE campaign_id=?", (campaign_id,))
    existing = {r["phone"] for r in db.query("SELECT phone FROM contacts WHERE campaign_id=?", (campaign_id,))}
    new_rows = []
    for contact in contacts:
        if contact["phone"] in existing:
            warnings.append(f"{contact['phone']}: уже есть в кампании, пропущен")
            continue
        new_rows.append((campaign_id, contact["name"], contact["phone"], contact["external_id"],
                         "pending", json.dumps(contact["meta"], ensure_ascii=False), now_iso()))
    if new_rows:
        db.executemany(
            "INSERT INTO contacts(campaign_id, name, phone, external_id, status, meta, created_at) "
            "VALUES (?,?,?,?,?,?,?)", new_rows)
    engine_of(request).log(campaign_id, "info", f"Импорт контактов: добавлено {len(new_rows)}")
    return {"imported": len(new_rows), "skipped": len(contacts) - len(new_rows), "warnings": warnings[:50]}


@router.get("/campaigns/{campaign_id}/contacts/sample")
def contacts_sample(campaign_id: int) -> Response:
    text = "name;phone;external_id\n"
    names = ["Иван Петров", "Мария Смирнова", "Алексей Кузнецов", "Ольга Иванова", "Дмитрий Соколов",
             "Анна Морозова", "Сергей Волков", "Елена Лебедева"]
    for i, name in enumerate(names, start=1):
        text += f"{name};+79{i}1234{1000 + i};{1000 + i}\n"
    return Response(text.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="contacts_sample.csv"'})


# --------------------------------------------------------------------------- do not call
@router.get("/do-not-call")
def list_dnc(request: Request) -> list[dict[str, Any]]:
    return [row_to_dict(r) for r in db_of(request).query(
        "SELECT * FROM do_not_call ORDER BY created_at DESC")]


@router.post("/do-not-call", status_code=201)
def add_dnc(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        phone = normalize_phone(payload.get("phone", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db_of(request).execute(
        "INSERT OR REPLACE INTO do_not_call(phone, reason, created_at) VALUES (?,?,?)",
        (phone, (payload.get("reason") or "")[:300], now_iso()))
    return {"ok": True, "phone": phone}


@router.delete("/do-not-call/{phone:path}")
def remove_dnc(request: Request, phone: str) -> dict[str, Any]:
    try:
        normalized = normalize_phone(phone)
    except ValueError:
        normalized = phone
    db_of(request).execute("DELETE FROM do_not_call WHERE phone=?", (normalized,))
    return {"ok": True}


# --------------------------------------------------------------------------- results
@router.get("/campaigns/{campaign_id}/results")
def campaign_results(request: Request, campaign_id: int) -> dict[str, Any]:
    db = db_of(request)
    campaign = require_campaign(db, campaign_id)
    questions = campaign_questions(db, campaign_id)
    contacts = [row_to_dict(r) for r in db.query(
        "SELECT * FROM contacts WHERE campaign_id=? ORDER BY id", (campaign_id,))]
    answer_rows = [row_to_dict(r) for r in db.query(
        """SELECT a.*, q.position AS q_position, c.outcome AS call_outcome, c.started_at AS call_started_at
           FROM answers a
           JOIN questions q ON q.id=a.question_id
           JOIN calls c ON c.id=a.call_id
           WHERE a.campaign_id=? ORDER BY a.id""", (campaign_id,))]
    by_contact: dict[int, dict[int, dict]] = {}
    call_info: dict[int, dict] = {}
    for row in answer_rows:
        by_contact.setdefault(row["contact_id"], {})[row["question_id"]] = row
        call_info[row["contact_id"]] = {"call_id": row["call_id"], "outcome": row["call_outcome"],
                                        "started_at": row["call_started_at"]}
    min_conf = settings_of(request).get_float("min_confidence", 0.45)
    columns = [{"id": q["id"], "title": f"В{q['position'] + 1}", "text": q["text"], "type": q["type"],
                "options": q["options"]} for q in questions]
    rows = []
    for contact in contacts:
        cells = []
        for q in questions:
            answer = by_contact.get(contact["id"], {}).get(q["id"])
            if answer:
                low = answer_is_low_confidence(answer, min_conf)
                cells.append({
                    "answer_id": answer["id"], "value": answer["value"],
                    "display": answer["value_display"] or answer["value"], "raw_text": answer["raw_text"],
                    "confidence": round(float(answer["confidence"] or 0), 3), "method": answer["method"],
                    "low_confidence": low, "reviewed": bool(answer["reviewed"]),
                    "audio_url": f"/api/answers/{answer['id']}/audio" if answer["audio_path"] else None,
                })
            else:
                cells.append(None)
        rows.append({
            "contact": contact,
            "call": call_info.get(contact["id"]),
            "cells": cells,
        })
    low_total = sum(1 for r in rows for c in r["cells"] if c and c["low_confidence"] and not c["reviewed"])
    return {"campaign": campaign, "questions": columns, "rows": rows, "min_confidence": min_conf,
            "low_confidence_total": low_total, "stats": engine_of(request).stats(campaign_id)}


@router.patch("/answers/{answer_id}")
def patch_answer(request: Request, answer_id: int, payload: AnswerPatch) -> dict[str, Any]:
    db = db_of(request)
    answer = db.query_one("SELECT * FROM answers WHERE id=?", (answer_id,))
    if not answer:
        raise HTTPException(404, "Ответ не найден")
    fields = payload.model_dump(exclude_none=True)
    updates = {}
    if "value" in fields:
        updates["value"] = fields["value"]
        updates["method"] = "manual"
        updates["confidence"] = 1.0
    if "value_display" in fields:
        updates["value_display"] = fields["value_display"]
    if "reviewed" in fields:
        updates["reviewed"] = int(bool(fields["reviewed"]))
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        db.execute(f"UPDATE answers SET {sets} WHERE id=?", [*updates.values(), answer_id])
    return row_to_dict(db.query_one("SELECT * FROM answers WHERE id=?", (answer_id,)))


@router.get("/answers/{answer_id}/audio")
def answer_audio(request: Request, answer_id: int) -> FileResponse:
    row = db_of(request).query_one("SELECT audio_path FROM answers WHERE id=?", (answer_id,))
    if not row or not row["audio_path"] or not Path(row["audio_path"]).exists():
        raise HTTPException(404, "Запись ответа недоступна")
    return FileResponse(row["audio_path"], media_type="audio/wav")


@router.post("/answers/recalc")
def recalc_answers(request: Request, payload: RecalcIn = Body(default=RecalcIn())) -> dict[str, Any]:
    """Повторно разбирает сохранённые расшифровки (после правки правил/словаря)."""
    db = db_of(request)
    where, params = "1=1", []
    if payload.call_id:
        where, params = "a.call_id=?", [payload.call_id]
    rows = db.query(
        f"""SELECT a.id, a.raw_text, a.method, q.* FROM answers a JOIN questions q ON q.id=a.question_id
            WHERE {where}""", params)
    updated = 0
    for row in rows:
        if row["method"] == "dtmf":
            continue
        question = {"type": row["type"], "options": json.loads(row["options"] or "[]")}
        parsed = parse_answer(question, row["raw_text"] or "")
        db.execute("UPDATE answers SET value=?, value_display=?, confidence=? WHERE id=?",
                   (parsed.value, parsed.display, float(parsed.confidence), row["id"]))
        updated += 1
    return {"ok": True, "updated": updated}


@router.get("/calls")
def list_calls(request: Request, campaign_id: int | None = None, outcome: str | None = None,
               limit: int = Query(default=100, le=1000)) -> list[dict[str, Any]]:
    db = db_of(request)
    where, params = ["1=1"], []
    if campaign_id:
        where.append("c.campaign_id=?")
        params.append(campaign_id)
    if outcome:
        where.append("c.outcome=?")
        params.append(outcome)
    rows = db.query(
        f"""SELECT c.*, ct.name, ct.phone FROM calls c JOIN contacts ct ON ct.id=c.contact_id
            WHERE {' AND '.join(where)} ORDER BY c.id DESC LIMIT ?""", [*params, limit])
    return [row_to_dict(r) for r in rows]


@router.get("/calls/{call_id}")
def call_detail(request: Request, call_id: int) -> dict[str, Any]:
    db = db_of(request)
    call = row_to_dict(db.query_one(
        """SELECT c.*, ct.name, ct.phone FROM calls c JOIN contacts ct ON ct.id=c.contact_id
           WHERE c.id=?""", (call_id,)))
    if not call:
        raise HTTPException(404, "Звонок не найден")
    call["answers"] = [row_to_dict(r) for r in db.query(
        """SELECT a.*, q.text AS question, q.position FROM answers a
           JOIN questions q ON q.id=a.question_id WHERE a.call_id=? ORDER BY q.position""", (call_id,))]
    call["recording_url"] = f"/api/calls/{call_id}/recording" if call.get("recording_path") else None
    return call


@router.get("/calls/{call_id}/recording")
def call_recording(request: Request, call_id: int) -> FileResponse:
    row = db_of(request).query_one("SELECT recording_path FROM calls WHERE id=?", (call_id,))
    if not row or not row["recording_path"] or not Path(row["recording_path"]).exists():
        raise HTTPException(404, "Запись недоступна")
    return FileResponse(row["recording_path"], media_type="audio/wav")


# --------------------------------------------------------------------------- export
@router.get("/campaigns/{campaign_id}/export")
def export_results(request: Request, campaign_id: int, fmt: str = Query(default="xlsx", pattern="^(csv|xlsx|json)$")) -> Response:
    db = db_of(request)
    campaign = require_campaign(db, campaign_id)
    name = safe_filename(campaign["name"])
    if fmt == "csv":
        content, media, ext = export_csv(db, campaign_id), "text/csv; charset=utf-8", "csv"
    elif fmt == "json":
        content, media, ext = export_json(db, campaign_id), "application/json", "json"
    else:
        content, media, ext = export_xlsx(db, campaign_id, campaign["name"]), (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), "xlsx"
    return Response(content, media_type=media, headers={
        "Content-Disposition": content_disposition(f"{name}.{ext}"),
        "Access-Control-Expose-Headers": "Content-Disposition",
    })


@router.get("/campaigns/{campaign_id}/export/transcripts")
def export_call_transcripts(request: Request, campaign_id: int) -> Response:
    db = db_of(request)
    campaign = require_campaign(db, campaign_id)
    filename = f"{safe_filename(campaign['name'])}_transcripts.csv"
    return Response(export_transcripts(db, campaign_id), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": content_disposition(filename)})


@router.get("/campaigns/{campaign_id}/preview-table")
def preview_table(request: Request, campaign_id: int) -> dict[str, Any]:
    """Лёгкий предпросмотр таблицы (первые 50 строк) для интерфейса."""
    db = db_of(request)
    require_campaign(db, campaign_id)
    questions = campaign_questions(db, campaign_id)
    rows = db.query(
        """SELECT ct.id, ct.name, ct.phone, ct.status, COUNT(a.id) AS answers_count,
                  ROUND(AVG(a.confidence), 3) AS avg_conf
           FROM contacts ct LEFT JOIN answers a ON a.contact_id=ct.id
           WHERE ct.campaign_id=? GROUP BY ct.id ORDER BY ct.id LIMIT 50""", (campaign_id,))
    return {"questions": [{"id": q["id"], "text": q["text"]} for q in questions],
            "rows": [row_to_dict(r) for r in rows]}


# --------------------------------------------------------------------------- events
@router.get("/events")
def list_events(request: Request, campaign_id: int | None = None, after_id: int = 0,
                level: str | None = None, limit: int = Query(default=200, le=1000)) -> list[dict[str, Any]]:
    db = db_of(request)
    where, params = ["id > ?"], [after_id]
    if campaign_id:
        where.append("campaign_id=?")
        params.append(campaign_id)
    if level:
        where.append("level=?")
        params.append(level)
    rows = db.query(f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                    [*params, limit])
    return [row_to_dict(r) for r in reversed(rows)]


# --------------------------------------------------------------------------- settings & equipment
@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    settings = settings_of(request)
    return {"values": settings.public(), "secrets": {"http_asr_token": bool(settings.secret("http_asr_token"))}}


@router.put("/settings")
def put_settings(request: Request, payload: SettingsIn) -> dict[str, Any]:
    settings, engine = settings_of(request), engine_of(request)
    applied = settings.update_many(payload.values)
    engine.asr._engines.clear()  # пересоздать движки при изменении путей
    engine.log(None, "info", f"Изменены настройки: {', '.join(applied) or 'нет'}")
    return {"ok": True, "applied": applied, "values": settings.public()}


@router.post("/settings/reset")
def reset_setting(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    settings = settings_of(request)
    settings.reset(payload.get("key", ""))
    return {"ok": True, "values": settings.public()}


@router.get("/equipment")
def equipment(request: Request, probe: bool = False) -> dict[str, Any]:
    engine, settings = engine_of(request), settings_of(request)
    return {
        "modem": engine.modem_status(probe=probe),
        "ports": probe_ports(),
        "audio_devices": audio_devices(),
        "audio": {"arecord": _which("arecord"), "aplay": _which("aplay"), "ffmpeg": _which("ffmpeg"),
                  "play_device": settings.get("audio_play_device"),
                  "record_device": settings.get("audio_record_device")},
        "tts": tts_of(request).status(),
        "asr": request.app.state.asr.status(),
        "calling_hours": settings.get("calling_hours"),
        "within_calling_hours": within_calling_hours(settings.get("calling_hours")),
    }


def content_disposition(filename: str) -> str:
    """RFC 6266: ASCII-fallback + UTF-8 имя файла для кириллицы."""
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip() or "export"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _which(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None


@router.post("/equipment/modem/check")
def modem_check(request: Request) -> dict[str, Any]:
    engine = engine_of(request)
    try:
        modem = engine.get_modem()
        modem.refresh()
        return {"ok": True, **engine.modem_status()}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/equipment/modem/balance")
def modem_balance(request: Request) -> dict[str, Any]:
    engine = engine_of(request)
    try:
        modem = engine.get_modem()
        text = modem.balance()
        return {"ok": bool(text), "balance": text}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/equipment/modem/test-call")
def modem_test_call(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Пробный звонок без записи: проверка звука/озвучки на реальном модеме."""
    engine = engine_of(request)
    try:
        phone = normalize_phone(payload.get("phone", ""))
        modem = engine.get_modem()
        outcome = modem.dial(phone, timeout=float(payload.get("timeout") or 40))
        if outcome == "answered" and payload.get("speak"):
            text = str(payload["speak"])[:500]
            path = engine.tts.synthesize(text)
            engine.audio_io().play(path)
        modem.hangup()
        return {"ok": outcome == "answered", "outcome": outcome}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/equipment/modem/ussd")
def modem_ussd(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    engine = engine_of(request)
    code = (payload.get("code") or "*100#").strip()[:40]
    try:
        modem = engine.get_modem()
        modem.ussd_balance = code
        modem.channel.command("AT+CUSD=1", allow_error=True, timeout=2)
        modem.channel.write(f'AT+CUSD=1,"{code}",15')
        import time

        deadline = time.time() + 20
        while time.time() < deadline:
            for line in modem.channel.drain():
                if line.startswith("+CUSD:"):
                    m = re.search(r'\+CUSD:\s*\d,\s*"([^"]+)"', line)
                    if m:
                        return {"ok": True, "response": decode_ussd(m.group(1))}
            time.sleep(0.15)
        return {"ok": False, "error": "Оператор не ответил на USSD-запрос"}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


# --------------------------------------------------------------------------- ASR
@router.post("/asr/transcribe")
async def asr_transcribe(request: Request, file: UploadFile = File(...), engine: str | None = Form(None)) -> dict[str, Any]:
    """Распознавание загруженного аудио (для проверки качества и тестов)."""
    data = await file.read()
    if len(data) > 40 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 40 МБ")
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        result = request.app.state.asr.transcribe(str(tmp_path), preferred=engine)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"text": result.text, "engine": result.engine, "confidence": result.confidence,
            "error": result.error}


@router.get("/asr/status")
def asr_status(request: Request) -> dict[str, Any]:
    return request.app.state.asr.status()


@router.post("/text/parse")
def parse_text(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Проверка распознавания ответа по тексту (без звонка)."""
    question = payload.get("question") or {}
    text = payload.get("text") or ""
    parsed = parse_answer(question, text)
    return {"value": parsed.value, "display": parsed.display, "confidence": parsed.confidence,
            "matched": parsed.matched, "method": parsed.method}


# --------------------------------------------------------------------------- health
@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "ts": now_iso(), "version": APP_VERSION}
