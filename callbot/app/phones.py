"""Нормализация телефонных номеров (Россия/МегаФон) и CSV-импорт."""
from __future__ import annotations

import csv
import io
import re
from typing import Iterable

_DIGITS = re.compile(r"\d")
_EXT = re.compile(r"(?i)\s*(доб\.?|ext\.?|#)\s*\d+\s*$")


def digits_only(value: str) -> str:
    return "".join(_DIGITS.findall(value or ""))


def normalize_phone(raw: str, default_country: str = "7") -> str:
    """Приводит номер к формату +7XXXXXXXXXX (или +<код><номер>).

    Поддерживает: +7 (999) 123-45-67, 8 999 123 45 67, 9991234567,
    0079991234567, 8-800-... и т.п. Для 10-значных номеров подставляется
    код страны (по умолчанию 7 — Россия).
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Пустой номер телефона")
    text = _EXT.sub("", text)
    digits = digits_only(text)
    if not digits:
        raise ValueError(f"В номере нет цифр: {raw!r}")
    if len(digits) == 10:
        digits = default_country + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = default_country + digits[1:]
    elif len(digits) == 11 and digits.startswith("7"):
        pass
    elif len(digits) > 11 and digits.startswith("00"):
        digits = digits[2:]
    if len(digits) < 10 or len(digits) > 15:
        raise ValueError(f"Некорректная длина номера: {raw!r}")
    return "+" + digits


def phone_for_modem(phone: str) -> str:
    """МегаФон/GSM: номер передаётся в международном формате (ATD+7...)."""
    return phone if phone.startswith("+") else "+" + digits_only(phone)


def format_pretty(phone: str) -> str:
    """+79991234567 -> +7 999 123-45-67 (для отображения)."""
    digits = digits_only(phone)
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return phone


def parse_contacts_csv(data: str | bytes, default_country: str = "7") -> tuple[list[dict], list[str]]:
    """Разбирает CSV/TXT со списком контактов.

    Поддерживаемые колонки (заголовок не обязателен):
    name, phone, external_id и любые дополнительные (сохраняются в meta).
    Если заголовка нет, первая колонка с телефоном определяется по содержимому.
    Возвращает (контакты, список предупреждений).
    """
    if isinstance(data, bytes):
        for enc in ("utf-8-sig", "cp1251", "utf-16"):
            try:
                data = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            data = data.decode("utf-8", errors="replace")

    text = data.strip()
    if not text:
        return [], ["Файл пуст"]

    sample = text[:4096]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        for cand in (";", "\t", "|", ","):
            if cand in sample:
                delimiter = cand
                break

    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return [], ["Файл пуст"]

    warnings: list[str] = []
    header_map: dict[str, int] = {}
    aliases = {
        "name": {"name", "имя", "фио", "клиент", "название", "contact"},
        "phone": {"phone", "телефон", "номер", "тел", "mobile", "msisdn", "phone_number"},
        "external_id": {"id", "external_id", "внешний_id", "код", "client_id"},
    }
    first = [c.strip().lower() for c in rows[0]]
    if any(cell in {"phone", "телефон", "номер", "tel", "name", "имя"} for cell in first):
        for idx, cell in enumerate(first):
            for key, names in aliases.items():
                if cell in names and key not in header_map:
                    header_map[key] = idx
        rows = rows[1:]

    result: list[dict] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows, start=2 if header_map else 1):
        if not row:
            continue
        phone = ""
        name = ""
        ext_id = ""
        extra: dict[str, str] = {}

        if header_map.get("phone") is not None and header_map["phone"] < len(row):
            phone = row[header_map["phone"]]
        if header_map.get("name") is not None and header_map["name"] < len(row):
            name = row[header_map["name"]]
        if header_map.get("external_id") is not None and header_map["external_id"] < len(row):
            ext_id = row[header_map["external_id"]]

        if not phone:
            # Ищем первое поле, похожее на телефон.
            for cell in row:
                if len(digits_only(cell)) >= 10:
                    phone = cell
                    break
        if not phone:
            warnings.append(f"Строка {line_no}: телефон не найден, пропущена")
            continue
        try:
            normalized = normalize_phone(phone, default_country)
        except ValueError as exc:
            warnings.append(f"Строка {line_no}: {exc}")
            continue
        if normalized in seen:
            warnings.append(f"Строка {line_no}: дубликат {normalized} пропущен")
            continue
        seen.add(normalized)

        if not name:
            for cell in row:
                cleaned = (cell or "").strip()
                if cleaned and cleaned != phone and len(digits_only(cleaned)) < 6:
                    name = cleaned
                    break
        if not ext_id:
            for cell in row:
                cleaned = (cell or "").strip()
                if cleaned and cleaned not in (phone, name) and len(digits_only(cleaned)) >= 1:
                    ext_id = cleaned
                    break
        for idx, cell in enumerate(row):
            cleaned = (cell or "").strip()
            if cleaned and cleaned not in (phone, name, ext_id):
                extra[f"col{idx + 1}"] = cleaned

        result.append(
            {
                "name": name.strip()[:200],
                "phone": normalized,
                "external_id": str(ext_id).strip()[:100],
                "meta": extra,
            }
        )
    return result, warnings


def phone_sample_rows(count: int = 3) -> Iterable[dict]:
    names = ["Иван Петров", "Мария Смирнова", "Алексей Кузнецов"]
    for i in range(count):
        yield {"name": names[i % len(names)], "phone": f"+7913000000{i}", "external_id": str(1000 + i)}
