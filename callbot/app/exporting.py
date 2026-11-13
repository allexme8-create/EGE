"""Экспорт таблицы ответов: CSV (Excel-совместимый), XLSX, JSON."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

from .answers import answer_is_low_confidence
from .db import Database

STATUS_LABELS = {
    "pending": "в очереди",
    "calling": "звоним",
    "done": "опрос завершён",
    "no_answer": "не ответил",
    "refused": "отказ от звонков",
    "failed": "ошибка",
    "dnc": "не звонить",
}


# --------------------------------------------------------------------------- данные
def campaign_questions(db: Database, campaign_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM questions WHERE campaign_id=? ORDER BY position, id", (campaign_id,))]


def answers_matrix(db: Database, campaign_id: int) -> tuple[list[str], list[dict]]:
    """Строка таблицы = контакт; колонки = вопросы + служебные поля."""
    questions = campaign_questions(db, campaign_id)
    contacts = [dict(r) for r in db.query(
        "SELECT * FROM contacts WHERE campaign_id=? ORDER BY id", (campaign_id,))]
    answers = db.query(
        """SELECT a.*, c.started_at AS call_started_at, c.outcome AS call_outcome
           FROM answers a JOIN calls c ON c.id = a.call_id
           WHERE a.campaign_id=? ORDER BY a.id""", (campaign_id,))
    by_contact: dict[int, dict[int, dict]] = {}
    call_info: dict[int, dict] = {}
    for row in answers:
        by_contact.setdefault(row["contact_id"], {})[row["question_id"]] = dict(row)
        call_info[row["contact_id"]] = {"call_id": row["call_id"], "outcome": row["call_outcome"],
                                        "started_at": row["call_started_at"]}

    min_conf = float(db.scalar("SELECT value FROM settings WHERE key='min_confidence'", (), 0.45) or 0.45)
    headers = ["ID", "Имя", "Телефон", "Статус", "Попыток", "Последний звонок"]
    headers += [f"В{i + 1}. {_short(q['text'], 60)}" for i, q in enumerate(questions)]
    headers += ["Низкая уверенность", "Комментарий"]
    rows: list[dict] = []
    for contact in contacts:
        answers_map = by_contact.get(contact["id"], {})
        info = call_info.get(contact["id"], {})
        cells = []
        low = []
        for q in questions:
            answer = answers_map.get(q["id"])
            if not answer:
                cells.append("")
                continue
            value = answer.get("value_display") or answer.get("value") or ""
            cells.append(value)
            if answer_is_low_confidence(answer, min_conf):
                low.append(f"В{q['position'] + 1}")
        data = {
            "ID": contact["id"],
            "Имя": contact.get("name") or "",
            "Телефон": contact["phone"],
            "Статус": STATUS_LABELS.get(contact["status"], contact["status"]),
            "Попыток": contact["attempts"],
            "Последний звонок": info.get("started_at") or contact.get("last_attempt_at") or "",
            "Низкая уверенность": ", ".join(low),
            "Комментарий": "",
            "external_id": contact.get("external_id") or "",
            "call_id": info.get("call_id"),
            "cells": cells,
        }
        rows.append(data)
    return headers, rows


def _short(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def export_csv(db: Database, campaign_id: int) -> bytes:
    headers, rows = answers_matrix(db, campaign_id)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row[h] for h in headers[:6]] + row["cells"]
                        + [row["Низкая уверенность"], row["Комментарий"]])
    return out.getvalue().encode("utf-8-sig")  # BOM — Excel откроет кириллицу корректно


# --------------------------------------------------------------------------- XLSX
def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _xlsx_cell(ref: str, value: Any, style: int = 0) -> str:
    style_attr = f' s="{style}"' if style else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    text = escape(str(value if value is not None else ""))
    preserve = ' xml:space="preserve"' if text != text.strip() else ""
    return f'<c r="{ref}" t="inlineStr"{style_attr}><is><t{preserve}>{text}</t></is></c>'


def export_xlsx(db: Database, campaign_id: int, campaign_name: str = "") -> bytes:
    headers, rows = answers_matrix(db, campaign_id)
    questions = campaign_questions(db, campaign_id)
    q_headers = headers[6:6 + len(questions)]
    sheet_rows: list[list[Any]] = [headers]
    for row in rows:
        line = [row[h] for h in headers[:6]] + row["cells"]
        line += [row["Низкая уверенность"], row["Комментарий"]]
        sheet_rows.append(line)

    rows_xml: list[str] = []
    for r_idx, line in enumerate(sheet_rows, start=1):
        cells = []
        for c_idx, value in enumerate(line):
            ref = f"{_col_letter(c_idx)}{r_idx}"
            cells.append(_xlsx_cell(ref, value, style=1 if r_idx == 1 else 0))
        height = ' ht="30" customHeight="1"' if r_idx == 1 else ""
        rows_xml.append(f'<row r="{r_idx}"{height}>{"".join(cells)}</row>')

    cols = []
    widths = [6, 24, 18, 12, 8, 20] + [26] * len(q_headers) + [18, 30]
    for i, width in enumerate(widths, start=1):
        cols.append(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
        'state="frozen"/></sheetView></sheetViews>'
        f'<cols>{"".join(cols)}</cols><sheetData>{"".join(rows_xml)}</sheetData></worksheet>'
    )
    title = escape((campaign_name or "Ответы")[:31] or "Ответы").replace("&amp;", "&")
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{title}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1F6FEB"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1">'
        '<alignment vertical="center" wrapText="1"/></xf></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/></Relationships>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def export_json(db: Database, campaign_id: int) -> bytes:
    headers, rows = answers_matrix(db, campaign_id)
    questions = campaign_questions(db, campaign_id)
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "campaign_id": campaign_id,
        "questions": [{"id": q["id"], "position": q["position"], "text": q["text"], "type": q["type"],
                       "options": json.loads(q["options"] or "[]")} for q in questions],
        "rows": rows,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def export_transcripts(db: Database, campaign_id: int) -> bytes:
    """Выгрузка расшифровок звонков (для контроля качества)."""
    rows = db.query(
        """SELECT c.id, c.started_at, c.outcome, c.duration_sec, c.transcript,
                  ct.name, ct.phone
           FROM calls c JOIN contacts ct ON ct.id=c.contact_id
           WHERE c.campaign_id=? ORDER BY c.id""", (campaign_id,))
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Звонок", "Время", "Абонент", "Телефон", "Исход", "Длительность, с", "Расшифровка"])
    for row in rows:
        writer.writerow([row["id"], row["started_at"], row["name"] or "", row["phone"], row["outcome"],
                         row["duration_sec"], row["transcript"] or ""])
    return out.getvalue().encode("utf-8-sig")


TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def safe_filename(name: str) -> str:
    cleaned = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in " -_()")) else "_" for ch in name)
    cleaned = " ".join(cleaned.split()).strip("_ ")
    if not cleaned:
        translit = "".join(TRANSLIT.get(ch.lower(), ch if ch.isascii() else "") for ch in name)
        cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in translit).strip("_")
    return cleaned or "campaign"
