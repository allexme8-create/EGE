"""Разбор ответов: самый важный модуль — от него зависит таблица."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.answers import contains_refusal, parse_answer, parse_dtmf, parse_rating, parse_yes_no


@pytest.mark.parametrize("text,expected", [
    ("да", "yes"),
    ("Да, конечно", "yes"),
    ("ага", "yes"),
    ("ну да, интересно", "yes"),
    ("нет", "no"),
    ("Нет, спасибо", "no"),
    ("нет, не интересно", "no"),
    ("не интересует", "no"),
    ("пока нет", "no"),
    ("не нужно", "no"),
])
def test_yes_no(text: str, expected: str) -> None:
    parsed = parse_yes_no(text)
    assert parsed.value == expected, text
    assert parsed.confidence >= 0.8


def test_refusal_priority() -> None:
    parsed = parse_yes_no("да, но не звоните мне больше")
    assert parsed.value == "refused"
    assert contains_refusal("Уберите меня из списка")


def test_empty() -> None:
    parsed = parse_yes_no("")
    assert parsed.value == ""
    assert parsed.confidence == 0.0


@pytest.mark.parametrize("text,value,display", [
    ("первый", "1", "Интернет"),
    ("вариант два", "2", "Связь"),
    ("мне нужна связь", "2", "Связь"),
    ("Интернет", "1", "Интернет"),
    ("3", "3", "Телевидение"),
    ("третье", "3", "Телевидение"),
])
def test_choice(text: str, value: str, display: str) -> None:
    parsed = parse_answer({"type": "choice", "options": ["Интернет", "Связь", "Телевидение"]}, text)
    assert parsed.value == value
    assert parsed.display == display
    assert parsed.matched


@pytest.mark.parametrize("text,expected", [
    ("пять", "5"),
    ("на пятерку", "5"),
    ("4", "4"),
    ("оценка три", "3"),
    ("четверка", "4"),
])
def test_rating(text: str, expected: str) -> None:
    assert parse_rating(text).value == expected


@pytest.mark.parametrize("text,expected", [
    ("сорок два", "42"),
    ("35", "35"),
    ("двадцать пять минут", "25"),
])
def test_number(text: str, expected: str) -> None:
    assert parse_answer({"type": "number"}, text).value == expected


def test_free_text() -> None:
    parsed = parse_answer({"type": "free"}, "хочу узнать про тариф")
    assert parsed.value == "хочу узнать про тариф"
    assert parsed.matched


def test_dtmf() -> None:
    yes = parse_dtmf("1", [], "yes_no")
    assert yes.value == "yes" and yes.method == "dtmf" and yes.confidence == 1.0
    choice = parse_dtmf("2", ["Интернет", "Связь"], "choice")
    assert choice.value == "2" and choice.display == "Связь"


def test_low_confidence_flag() -> None:
    parsed = parse_answer({"type": "yes_no"}, "ммм не разобрать")
    assert not parsed.matched
    assert parsed.confidence < 0.45
