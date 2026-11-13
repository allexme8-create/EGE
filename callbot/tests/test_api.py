"""Интеграционные тесты API: кампания → обзвон (демо) → таблица → экспорт."""
from __future__ import annotations

import time


def wait_campaign(client, campaign_id: int, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = client.get(f"/api/campaigns/{campaign_id}/stats").json()
        if not stats["active"] and not stats["pending"] and not stats["calling"]:
            return stats
        time.sleep(0.2)
    raise AssertionError("кампания не завершилась за отведённое время")


def test_meta(client) -> None:
    data = client.get("/api/meta").json()
    assert data["version"]
    assert "asr" in data and data["asr"]["active"] in {"vosk", "whispercpp", "http", "stub"}
    assert data["within_calling_hours"] in (True, False)


def test_campaign_crud(client) -> None:
    created = client.post("/api/campaigns", json={"name": "Кампания 1", "mode": "demo"})
    assert created.status_code == 201
    cid = created.json()["id"]

    patched = client.patch(f"/api/campaigns/{cid}", json={"name": "Кампания 2", "retry_limit": 3})
    assert patched.json()["name"] == "Кампания 2"
    assert patched.json()["retry_limit"] == 3

    question = client.post(f"/api/campaigns/{cid}/questions",
                           json={"text": "Вопрос?", "type": "yes_no"})
    assert question.status_code == 201
    qid = question.json()["id"]
    assert client.patch(f"/api/questions/{qid}", json={"text": "Новый вопрос?"}).json()["text"] == "Новый вопрос?"
    assert client.delete(f"/api/questions/{qid}").json()["ok"]

    assert client.delete(f"/api/campaigns/{cid}").json()["ok"]
    assert client.get(f"/api/campaigns/{cid}").status_code == 404


def test_choice_requires_options(client) -> None:
    cid = client.post("/api/campaigns", json={"name": "Ошибки", "mode": "demo"}).json()["id"]
    resp = client.post(f"/api/campaigns/{cid}/questions", json={"text": "Какой?", "type": "choice", "options": []})
    assert resp.status_code == 400


def test_contact_validation(client) -> None:
    cid = client.post("/api/campaigns", json={"name": "Контакты", "mode": "demo"}).json()["id"]
    ok = client.post(f"/api/campaigns/{cid}/contacts", json={"name": "Иван", "phone": "8 999 123 45 67"})
    assert ok.status_code == 201
    assert ok.json()["phone"] == "+79991234567"
    bad = client.post(f"/api/campaigns/{cid}/contacts", json={"name": "X", "phone": "abc"})
    assert bad.status_code == 422


def test_start_requires_questions_and_contacts(client) -> None:
    cid = client.post("/api/campaigns", json={"name": "Пустая", "mode": "demo"}).json()["id"]
    assert client.post(f"/api/campaigns/{cid}/start?mode=demo").status_code == 400
    client.post(f"/api/campaigns/{cid}/questions", json={"text": "Да?", "type": "yes_no"})
    resp = client.post(f"/api/campaigns/{cid}/start?mode=demo")
    assert resp.status_code == 400 and "контакт" in resp.json()["detail"].lower()


def test_demo_flow_and_results(client, demo_campaign) -> None:
    cid = demo_campaign
    assert client.post(f"/api/campaigns/{cid}/start?mode=demo").json()["ok"]
    stats = wait_campaign(client, cid)
    assert stats["done"] >= 1
    assert stats["answers"] > 0

    results = client.get(f"/api/campaigns/{cid}/results").json()
    assert len(results["questions"]) == 2
    assert len(results["rows"]) == 2
    phones = {row["contact"]["phone"] for row in results["rows"]}
    assert phones == {"+79991234567", "+79997654321"}
    answered_rows = [row for row in results["rows"] if any(row["cells"])]
    # В демо-эмуляторе часть абонентов может не взять трубку — проверяем, что
    # хотя бы у одного контакта есть ответы и связанный звонок.
    assert answered_rows, results
    assert answered_rows[0]["call"]["call_id"]

    campaign = client.get(f"/api/campaigns/{cid}").json()
    assert campaign["status"] == "completed"


def test_exports(client, demo_campaign) -> None:
    cid = demo_campaign
    client.post(f"/api/campaigns/{cid}/start?mode=demo")
    wait_campaign(client, cid)

    xlsx = client.get(f"/api/campaigns/{cid}/export?fmt=xlsx")
    assert xlsx.status_code == 200
    assert xlsx.content[:2] == b"PK"
    assert "spreadsheetml" in xlsx.headers["content-type"]

    csv_resp = client.get(f"/api/campaigns/{cid}/export?fmt=csv")
    text = csv_resp.content.decode("utf-8-sig")
    assert "Телефон" in text and "+79991234567" in text

    json_resp = client.get(f"/api/campaigns/{cid}/export?fmt=json").json()
    assert json_resp["campaign_id"] == cid
    assert len(json_resp["rows"]) == 2

    transcripts = client.get(f"/api/campaigns/{cid}/export/transcripts")
    assert transcripts.status_code == 200


def test_answer_manual_fix(client, demo_campaign) -> None:
    cid = demo_campaign
    client.post(f"/api/campaigns/{cid}/start?mode=demo")
    wait_campaign(client, cid)
    results = client.get(f"/api/campaigns/{cid}/results").json()
    answer = next(c for r in results["rows"] for c in r["cells"] if c)
    patched = client.patch(f"/api/answers/{answer['answer_id']}",
                           json={"value": "yes", "value_display": "да", "reviewed": True}).json()
    assert patched["value"] == "yes"
    assert patched["method"] == "manual"
    assert patched["reviewed"] == 1


def test_do_not_call_blocking(client) -> None:
    cid = client.post("/api/campaigns", json={"name": "Стоп-лист", "mode": "demo"}).json()["id"]
    client.post(f"/api/campaigns/{cid}/questions", json={"text": "Интересно?", "type": "yes_no"})
    client.post(f"/api/campaigns/{cid}/contacts", json={"name": "A", "phone": "+79990000001"})
    client.post(f"/api/campaigns/{cid}/contacts", json={"name": "B", "phone": "+79990000002"})
    assert client.post("/api/do-not-call", json={"phone": "8 999 000-00-01"}).status_code == 201
    client.post(f"/api/campaigns/{cid}/start?mode=demo")
    wait_campaign(client, cid)
    contacts = {r["phone"]: r for r in client.get(f"/api/campaigns/{cid}/contacts").json()["rows"]}
    assert contacts["+79990000001"]["status"] == "dnc"


def test_settings_roundtrip(client) -> None:
    assert client.put("/api/settings", json={"values": {"calling_hours": "10:00-19:00"}}).status_code == 200
    assert client.get("/api/settings").json()["values"]["calling_hours"] == "10:00-19:00"
    client.post("/api/settings/reset", json={"key": "calling_hours"})
    assert client.get("/api/settings").json()["values"]["calling_hours"] == "09:00-20:00"


def test_parse_endpoint_and_calls(client, demo_campaign) -> None:
    cid = demo_campaign
    client.post(f"/api/campaigns/{cid}/start?mode=demo")
    wait_campaign(client, cid)
    parsed = client.post("/api/text/parse", json={
        "question": {"type": "yes_no"}, "text": "нет, не звоните больше"}).json()
    assert parsed["value"] == "refused"

    calls = client.get(f"/api/calls?campaign_id={cid}").json()
    assert calls
    detail = client.get(f"/api/calls/{calls[0]['id']}").json()
    assert "answers" in detail and detail["phone"]


def test_index_and_static(client) -> None:
    assert client.get("/").status_code == 200
    assert "Обзвон" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_equipment_and_tts_status(client) -> None:
    data = client.get("/api/equipment").json()
    assert "modem" in data and "asr" in data and "tts" in data
    assert client.get("/api/asr/status").json()["active"] in {"vosk", "whispercpp", "http", "stub"}
    assert client.get("/api/tts/status").json()["active"] in {"piper", "espeak", "none"}
    health = client.get("/api/health").json()
    assert health["ok"] is True


def test_audio_upload_for_question(client) -> None:
    import io
    import wave

    cid = client.post("/api/campaigns", json={"name": "Аудио", "mode": "demo"}).json()["id"]
    qid = client.post(f"/api/campaigns/{cid}/questions",
                      json={"text": "Вопрос с аудио", "type": "yes_no"}).json()["id"]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * 8000)
    resp = client.post(f"/api/questions/{qid}/audio",
                       files={"file": ("q.wav", buffer.getvalue(), "audio/wav")})
    assert resp.status_code == 200
    assert resp.json()["audio_path"]
    assert client.get(f"/api/questions/{qid}/audio-file").status_code == 200
    assert client.delete(f"/api/questions/{qid}/audio").json()["ok"]
    assert client.get(f"/api/questions/{qid}/audio-file").status_code == 404
    bad = client.post(f"/api/questions/{qid}/audio", files={"file": ("q.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 400


def test_reorder_questions(client) -> None:
    cid = client.post("/api/campaigns", json={"name": "Порядок", "mode": "demo"}).json()["id"]
    ids = [client.post(f"/api/campaigns/{cid}/questions",
                       json={"text": f"Вопрос {i}", "type": "free"}).json()["id"] for i in range(3)]
    client.post("/api/questions/reorder", json={"ids": list(reversed(ids))})
    questions = client.get(f"/api/campaigns/{cid}/questions").json()
    assert [q["id"] for q in questions] == list(reversed(ids))
    assert [q["position"] for q in questions] == [0, 1, 2]
    client.delete(f"/api/questions/{ids[0]}")
    assert [q["position"] for q in client.get(f"/api/campaigns/{cid}/questions").json()] == [0, 1]


def test_calls_recording_available_after_demo(client, demo_campaign) -> None:
    cid = demo_campaign
    client.post(f"/api/campaigns/{cid}/start?mode=demo")
    wait_campaign(client, cid)
    calls = client.get(f"/api/calls?campaign_id={cid}").json()
    answered = [c for c in calls if c["outcome"] == "answered"]
    assert answered and answered[0]["recording_path"]
    call_id = answered[0]["id"]
    recording = client.get(f"/api/calls/{call_id}/recording")
    assert recording.status_code == 200
    assert recording.content[:4] == b"RIFF"
    detail = client.get(f"/api/calls/{call_id}").json()
    assert detail["recording_url"] == f"/api/calls/{call_id}/recording"
    answers = detail["answers"]
    assert answers and answers[0]["audio_path"]
    audio = client.get(f"/api/answers/{answers[0]['id']}/audio")
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"


def test_recalc_answers(client, demo_campaign) -> None:
    cid = demo_campaign
    client.post(f"/api/campaigns/{cid}/start?mode=demo")
    wait_campaign(client, cid)
    results = client.get(f"/api/campaigns/{cid}/results").json()
    call_id = results["rows"][0]["call"]["call_id"]
    updated = client.post("/api/answers/recalc", json={"call_id": call_id}).json()
    assert updated["updated"] >= 1
