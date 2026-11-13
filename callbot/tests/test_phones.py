"""Нормализация номеров и импорт контактов."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.phones import format_pretty, normalize_phone, parse_contacts_csv


@pytest.mark.parametrize("raw,expected", [
    ("+7 999 123-45-67", "+79991234567"),
    ("8 (999) 123-45-67", "+79991234567"),
    ("9991234567", "+79991234567"),
    ("79991234567", "+79991234567"),
    ("8-800-555-35-35", "+78005553535"),
    ("+7 913 111 22 33 доб. 105", "+79131112233"),
    ("0079991234567", "+79991234567"),
])
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "123", "+7 999"])
def test_normalize_bad(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_pretty() -> None:
    assert format_pretty("+79991234567") == "+7 999 123-45-67"


def test_csv_with_header_semicolon() -> None:
    data = "Имя;Телефон;ID\nИван Петров;8 999 123-45-67;1\nМария;+79997654321;2\n"
    contacts, warnings = parse_contacts_csv(data)
    assert [c["phone"] for c in contacts] == ["+79991234567", "+79997654321"]
    assert contacts[0]["name"] == "Иван Петров"
    assert contacts[0]["external_id"] == "1"


def test_csv_without_header_comma() -> None:
    data = "Иван,9991234567\nМария,9997654321\n"
    contacts, warnings = parse_contacts_csv(data)
    assert len(contacts) == 2
    assert contacts[0]["phone"] == "+79991234567"


def test_csv_cp1251_and_duplicates() -> None:
    data = "Имя;Телефон\nИван;+79991234567\nИван2;+79991234567\nЛоманая;123\n".encode("cp1251")
    contacts, warnings = parse_contacts_csv(data)
    assert len(contacts) == 1
    assert any("дубликат" in w for w in warnings)
    assert any("Некорректная длина" in w for w in warnings)


def test_empty_csv() -> None:
    contacts, warnings = parse_contacts_csv("")
    assert contacts == []
    assert warnings
