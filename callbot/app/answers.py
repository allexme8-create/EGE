"""Разбор устных/клавиатурных ответов в структурированные значения.

Модуль изолирован от внешних сервисов: на вход — распознанный текст и тип
вопроса, на выходе — значение для таблицы, «человеческое» отображение и
уверенность. Используется и для live-обработки звонков, и для повторного
разбора в интерфейсе.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NEGATIVE = {
    "нет", "не", "неа", "не хочу", "не надо", "не нужно", "не интересно",
    "не интересно мне", "отказ", "отказываюсь", "ни за что", "не согласен",
    "не согласна", "против", "никогда", "no", "nope",
}
POSITIVE = {
    "да", "ага", "конечно", "да конечно", "разумеется", "согласен", "согласна",
    "согласны", "хорошо", "давайте", "ок", "окей", "верно", "точно", "конечно да",
    "yes", "yeah", "yep", "ok", "okay", "sure",
}
REFUSAL = {
    "не звоните", "не звоните больше", "уберите меня", "уберите из списка",
    "не беспокойте", "не звоните мне", "хватит звонить", "отпишите",
    "не надо мне звонить", "прекратите звонить",
}
CORRECTION = {"нет", "неправильно", "неверно", "не то"}

DIGIT_WORDS = {
    "ноль": "0", "нуль": "0", "один": "1", "одна": "1", "два": "2", "две": "2",
    "три": "3", "четыре": "4", "пять": "5", "шесть": "6", "семь": "7",
    "восемь": "8", "девять": "9",
}
ORDINALS = {
    "первый": 1, "первое": 1, "первого": 1, "первая": 1,
    "второй": 2, "второе": 2, "второго": 2, "вторая": 2,
    "третий": 3, "третье": 3, "третьего": 3,
    "четвертый": 4, "четвёртый": 4, "четвертое": 4, "четвёртое": 4,
    "пятый": 5, "пятое": 5, "шестой": 6, "седьмой": 7,
    "восьмой": 8, "девятый": 9, "десятый": 10,
}

# Названия цифр для клавиатуры/голосом (ДТМФ «1»..«9»).
PUNCT_RE = re.compile(r"[^\w\s+.,-]", re.UNICODE)
MULTISPACE = re.compile(r"\s+")


@dataclass
class ParsedAnswer:
    value: str = ""
    display: str = ""
    confidence: float = 0.0
    method: str = "asr"
    matched: bool = False
    hints: dict[str, str] = field(default_factory=dict)


def normalize_text(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = PUNCT_RE.sub(" ", text)
    return MULTISPACE.sub(" ", text).strip()


def contains_refusal(text: str) -> bool:
    norm = normalize_text(text)
    return any(phrase in norm for phrase in REFUSAL)


def _words(text: str) -> list[str]:
    words = []
    for raw in normalize_text(text).split(" "):
        cleaned = raw.strip(".,-+")
        if cleaned:
            words.append(cleaned)
    return words


def _match_phrase(norm: str, phrase: str) -> bool:
    phrase = normalize_text(phrase)
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", norm) is not None


def parse_yes_no(text: str, min_confidence: float = 0.45) -> ParsedAnswer:
    norm = normalize_text(text)
    words = _words(text)
    if not norm:
        return ParsedAnswer(value="", display="нет ответа", confidence=0.0, matched=False)
    if len(words) > 10:
        # Очень длинная «реплика»: решение по одному слову ненадёжно.
        return ParsedAnswer(value="", display=text.strip()[:200], confidence=0.3, matched=False)

    # Приоритет: явный отказ от дальнейших звонков.
    for phrase in REFUSAL:
        if _match_phrase(norm, phrase):
            return ParsedAnswer("refused", "отказ от звонков", 0.9, matched=True)

    # Явные отрицания, которые не являются отказом от звонков.
    for phrase in ("не интересует", "не интересно", "не нужно", "не надо", "не сейчас",
                   "не хочу", "не буду", "пока нет", "вряд ли", "нет желания"):
        if _match_phrase(norm, phrase):
            return ParsedAnswer("no", "нет", 0.85, matched=True)
    # «да, нет» — берём первое значимое слово.
    for word in words:
        if word in {"нет", "неа", "no", "nope", "отказ", "против"}:
            return ParsedAnswer("no", "нет", 0.92 if len(words) <= 3 else 0.8, matched=True)
        if word in {"да", "yes", "ага", "конечно", "разумеется", "согласен", "согласна", "ок", "окей", "хорошо", "давайте"}:
            return ParsedAnswer("yes", "да", 0.92 if len(words) <= 3 else 0.8, matched=True)
    return ParsedAnswer(value="", display=text.strip()[:200], confidence=0.2, matched=False)


def parse_choice(text: str, options: list[str], dtmf_enabled: bool = True) -> ParsedAnswer:
    norm = normalize_text(text)
    if not norm:
        return ParsedAnswer(display="нет ответа")
    options_norm = [normalize_text(o) for o in options]

    # 1) Точное/фразовое совпадение с вариантом.
    for idx, opt in enumerate(options_norm, start=1):
        if opt and _match_phrase(norm, opt):
            return ParsedAnswer(f"{idx}", options[idx - 1], 0.95, matched=True)

    # 2) Номер варианта: «вариант два», «второй», «2».
    for word in _words(text):
        if word.isdigit() and 1 <= int(word) <= len(options):
            return ParsedAnswer(word, options[int(word) - 1], 0.85, matched=True)
        if word in DIGIT_WORDS:
            num = int(DIGIT_WORDS[word])
            if 1 <= num <= len(options):
                return ParsedAnswer(str(num), options[num - 1], 0.85, matched=True)
        if word in ORDINALS and 1 <= ORDINALS[word] <= len(options):
            num = ORDINALS[word]
            return ParsedAnswer(str(num), options[num - 1], 0.8, matched=True)

    # 3) Подстрока варианта (например, ASR услышал часть фразы).
    for idx, opt in enumerate(options_norm, start=1):
        if not opt:
            continue
        for word in _words(text):
            if len(word) >= 4 and word in opt.split(" "):
                return ParsedAnswer(f"{idx}", options[idx - 1], 0.6, matched=True)
    return ParsedAnswer(value="", display=text.strip()[:200], confidence=0.2, matched=False)


def parse_dtmf(digit: str, options: list[str], question_type: str) -> ParsedAnswer:
    digit = (digit or "").strip()
    if question_type == "yes_no":
        if digit == "1":
            return ParsedAnswer("yes", "да", 1.0, method="dtmf", matched=True)
        if digit == "2":
            return ParsedAnswer("no", "нет", 1.0, method="dtmf", matched=True)
        return ParsedAnswer(display=f"клавиша {digit}", method="dtmf")
    if question_type == "choice":
        if digit.isdigit() and 1 <= int(digit) <= len(options):
            return ParsedAnswer(digit, options[int(digit) - 1], 1.0, method="dtmf", matched=True)
    return ParsedAnswer(display=f"клавиша {digit}", method="dtmf")


def parse_rating(text: str, min_value: int = 1, max_value: int = 5) -> ParsedAnswer:
    norm = normalize_text(text)
    if not norm:
        return ParsedAnswer(display="нет ответа")
    # «пять», «на пять», «5», «пятерка»
    for word in _words(text):
        if word.isdigit() and min_value <= int(word) <= max_value:
            return ParsedAnswer(word, word, 0.9, matched=True)
        if word in DIGIT_WORDS and min_value <= int(DIGIT_WORDS[word]) <= max_value:
            val = DIGIT_WORDS[word]
            return ParsedAnswer(val, val, 0.9, matched=True)
        if word.startswith("пятер") and max_value >= 5:
            return ParsedAnswer("5", "5", 0.85, matched=True)
        if word.startswith("четвер") and max_value >= 4:
            return ParsedAnswer("4", "4", 0.85, matched=True)
        if word.startswith("трой") and max_value >= 3:
            return ParsedAnswer("3", "3", 0.85, matched=True)
        if word.startswith("двой") and min_value <= 2:
            return ParsedAnswer("2", "2", 0.85, matched=True)
        if word.startswith("единиц") and min_value <= 1:
            return ParsedAnswer("1", "1", 0.85, matched=True)
    return ParsedAnswer(value="", display=text.strip()[:200], confidence=0.2, matched=False)


def parse_number(text: str) -> ParsedAnswer:
    """Извлекает число из фразы («мне сорок два года», «42»)."""
    norm = normalize_text(text)
    if not norm:
        return ParsedAnswer(display="нет ответа")
    m = re.search(r"(?<!\d)(\d{1,7})(?!\d)", norm)
    if m:
        return ParsedAnswer(m.group(1), m.group(1), 0.9, matched=True)
    # Простые числительные до 1000.
    values = {"ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
              "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
              "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
              "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
              "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
              "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
              "девяносто": 90, "сто": 100, "тысяча": 1000}
    total = 0
    found = False
    for word in _words(text):
        if word in values:
            total += values[word]
            found = True
    if found:
        return ParsedAnswer(str(total), str(total), 0.75, matched=True)
    return ParsedAnswer(value="", display=text.strip()[:200], confidence=0.2, matched=False)


def parse_answer(question: dict, text: str, *, from_dtmf: bool = False, digit: str = "",
                 min_confidence: float = 0.45) -> ParsedAnswer:
    """Главная точка входа: разбирает ответ для вопроса любого типа."""
    qtype = question.get("type", "yes_no")
    options = question.get("options") or []
    if isinstance(options, str):
        import json

        try:
            options = json.loads(options)
        except ValueError:
            options = []
    if from_dtmf:
        parsed = parse_dtmf(digit, options, qtype)
    elif qtype == "yes_no":
        parsed = parse_yes_no(text, min_confidence)
    elif qtype == "choice":
        parsed = parse_choice(text, options)
    elif qtype == "rating":
        parsed = parse_rating(text)
    elif qtype == "number":
        parsed = parse_number(text)
    else:
        parsed = ParsedAnswer(value=(text or "").strip()[:500],
                              display=(text or "").strip()[:500],
                              confidence=0.7 if text.strip() else 0.0,
                              matched=bool(text.strip()), method="asr")
    if parsed.matched and parsed.confidence >= min_confidence and not parsed.display:
        parsed.display = str(parsed.value)
    if parsed.matched and is_non_answer(text):
        # «Не понял вопрос» — это не ответ «да», снижаем уверенность.
        parsed.value = ""
        parsed.display = (text or "").strip()[:200]
        parsed.confidence = 0.25
        parsed.matched = False
    return parsed


NON_ANSWER_PHRASES = (
    "не понял", "не поняла", "повторите", "какой вопрос", "что вы сказали",
    "не расслышал", "не расслышала", "алло", "это кто", "кто звонит",
)


def is_non_answer(text: str) -> bool:
    """Абонент не ответил по существу (переспросил, не расслышал)."""
    norm = normalize_text(text)
    return any(_match_phrase(norm, phrase) for phrase in NON_ANSWER_PHRASES)


def has_noise_markers(text: str) -> bool:
    """Признаки неразборчивой речи/шума в распознанном тексте."""
    norm = normalize_text(text)
    if not norm:
        return False
    words = norm.split(" ")
    markers = {"э", "ээ", "эээ", "мм", "ммм", "мгм", "ага", "ну", "это", "самое", "как", "его"}
    short_noise = sum(1 for w in words if len(w) <= 2)
    if len(words) >= 3 and short_noise / len(words) > 0.5:
        return True
    return len(words) >= 3 and sum(w in markers for w in words) >= 3


def answer_is_low_confidence(answer: dict, min_confidence: float = 0.45) -> bool:
    if answer.get("method") != "asr":
        return False
    return (float(answer.get("confidence") or 0) < min_confidence
            or has_noise_markers(answer.get("raw_text") or ""))


def summarize_table(questions: list[dict], mode: str = "wide") -> list[dict]:
    """Заготовка описания колонок таблицы ответов (для экспорта/фронта)."""
    columns = []
    for q in questions:
        columns.append({
            "id": q["id"],
            "title": f"В{q.get('position', 0) + 1}. {q['text']}",
            "type": q["type"],
        })
    return columns
