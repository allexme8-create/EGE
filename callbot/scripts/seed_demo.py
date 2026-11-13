#!/usr/bin/env python3
"""Наполняет базу демонстрационными данными: две кампании и готовые результаты.

Запуск:  python scripts/seed_demo.py [--no-run]
Без --no-run сразу прогоняет демо-обзвон, чтобы таблица ответов была заполнена.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import create_app  # noqa: E402

CAMPAIGNS = [
    {
        "campaign": {
            "name": "Опрос по тарифу «Всё за 500»",
            "intro_text": "Здравствуйте! Это МегаФон. Мы проводим короткий опрос о тарифах, "
                          "займёт около минуты.",
            "outro_text": "Спасибо за ответы! Всего доброго.",
            "mode": "demo",
            "retry_limit": 1,
        },
        "questions": [
            {"text": "Вам интересно предложение подключить домашний интернет?", "type": "yes_no"},
            {"text": "Какой тариф вам интересен: интернет, связь или телевидение?", "type": "choice",
             "options": ["Интернет", "Связь", "Телевидение"]},
            {"text": "Оцените по шкале от 1 до 5, насколько удобен ваш текущий тариф?", "type": "rating"},
            {"text": "Сколько минут в месяц вы обычно тратите на звонки?", "type": "number"},
            {"text": "Что бы вы хотели уточнить у оператора?", "type": "free"},
        ],
        "contacts": [
            ("Иван Петров", "+79991234567"), ("Мария Смирнова", "89997654321"),
            ("Алексей Кузнецов", "9995554433"), ("Ольга Иванова", "+7 913 111-22-33"),
            ("Дмитрий Соколов", "89134445566"), ("Анна Морозова", "+79135556677"),
            ("Сергей Волков", "9998887766"), ("Елена Лебедева", "+79997776655"),
        ],
    },
    {
        "campaign": {
            "name": "Контроль качества обслуживания",
            "intro_text": "Добрый день! Оцените, пожалуйста, качество обслуживания в салоне связи.",
            "mode": "demo",
        },
        "questions": [
            {"text": "Вы были в салоне связи за последний месяц?", "type": "yes_no"},
            {"text": "Оцените обслуживание от 1 до 5", "type": "rating"},
        ],
        "contacts": [("Андрей Белов", "+79130001122"), ("Наталья Орлова", "89130003344")],
    },
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-run", action="store_true", help="не запускать обзвон")
    args = parser.parse_args()

    app = create_app()
    # TestClient позволяет обращаться к API без запуска сервера.
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        existing = client.get("/api/campaigns").json()
        if existing:
            print(f"В базе уже есть {len(existing)} кампаний — демо-данные не добавляю.")
            return
        for item in CAMPAIGNS:
            cid = client.post("/api/campaigns", json=item["campaign"]).json()["id"]
            for question in item["questions"]:
                client.post(f"/api/campaigns/{cid}/questions", json=question)
            csv = "Имя;Телефон\n" + "\n".join(f"{n};{p}" for n, p in item["contacts"])
            client.post(f"/api/campaigns/{cid}/contacts/import",
                        files={"file": ("contacts.csv", csv.encode("utf-8"), "text/csv")})
            print(f"Создана кампания #{cid}: {item['campaign']['name']}")
            if not args.no_run:
                client.post(f"/api/campaigns/{cid}/start?mode=demo")
                deadline = time.time() + 120
                while time.time() < deadline:
                    stats = client.get(f"/api/campaigns/{cid}/stats").json()
                    if not stats["active"] and not stats["pending"]:
                        break
                    time.sleep(0.5)
                stats = client.get(f"/api/campaigns/{cid}/stats").json()
                print(f"  обзвон завершён: опрошено {stats['done']}, ответов {stats['answers']}")
    print("Готово. Запустите сервис: python run.py")


if __name__ == "__main__":
    main()
