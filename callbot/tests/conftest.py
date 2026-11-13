"""Общие фикстуры: изолированная БД на каждый тест."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLBOT_DATA", str(tmp_path / "data"))
    # Конфиг читает переменные при импорте, поэтому пересоздаём модули.
    for module in list(sys.modules):
        if module == "run" or module.startswith("app."):
            del sys.modules[module]
    from fastapi.testclient import TestClient
    import run  # noqa: WPS433

    app = run.create_app()
    with TestClient(app) as test_client:
        yield test_client
    app.state.engine.shutdown()


@pytest.fixture()
def demo_campaign(client):
    campaign = client.post("/api/campaigns", json={
        "name": "Тестовая кампания",
        "intro_text": "Здравствуйте!",
        "mode": "demo",
        "answer_seconds": 1,
    }).json()
    cid = campaign["id"]
    client.post(f"/api/campaigns/{cid}/questions",
                json={"text": "Вам интересно предложение?", "type": "yes_no"})
    client.post(f"/api/campaigns/{cid}/questions",
                json={"text": "Какой тариф интересен?", "type": "choice",
                      "options": ["Интернет", "Связь"]})
    client.post(f"/api/campaigns/{cid}/contacts/import",
                files={"file": ("c.csv", "Имя;Телефон\nИван;+79991234567\nМария;89997654321\n".encode(),
                                "text/csv")})
    return cid
