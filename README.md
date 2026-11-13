Практика подготовки к ЕГЭ

---

## Сервис обзвона клиентов (`callbot/`)

В репозитории также лежит веб-сервис автоматического обзвона клиентов через
GSM-модем с SIM-картой МегаФона: задания вопросов, запись и распознавание ответов,
таблица результатов и экспорт в Excel.

```bash
cd callbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py            # http://localhost:8000
```

Подробности: [callbot/README.md](callbot/README.md).
