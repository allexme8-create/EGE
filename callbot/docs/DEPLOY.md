# Развёртывание «Обзвон+» на сервере с модемом

## Вариант 1. systemd (рекомендуется для постоянной работы)

```bash
# 1. Код и окружение
sudo mkdir -p /opt/callbot && sudo chown $USER /opt/callbot
git clone <репозиторий> /opt/callbot-src && cp -r /opt/callbot-src/callbot/* /opt/callbot/
cd /opt/callbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Системные пакеты
sudo apt install alsa-utils ffmpeg espeak-ng

# 3. Модель Vosk (офлайн ASR)
mkdir -p data/models
wget -O /tmp/vosk.zip https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip
unzip /tmp/vosk.zip -d data/models
```

`/etc/systemd/system/callbot.service`:

```ini
[Unit]
Description=Callbot — обзвон клиентов
After=network-online.target

[Service]
Type=simple
User=callbot
WorkingDirectory=/opt/callbot
EnvironmentFile=/opt/callbot/.env
ExecStart=/opt/callbot/.venv/bin/python run.py --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
# доступ к последовательному порту и звуку
SupplementaryGroups=dialout audio
ProtectSystem=full
ReadWritePaths=/opt/callbot/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now callbot
sudo journalctl -u callbot -f
```

## Вариант 2. Docker

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    alsa-utils ffmpeg espeak-ng && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
```

Запуск с доступом к модему и звуку:

```bash
docker build -t callbot .
docker run -d --name callbot --restart unless-stopped \
  -p 8000:8000 \
  --device /dev/ttyUSB0 \
  --device /dev/snd \
  -v /opt/callbot/data:/app/data \
  --env-file .env \
  callbot
```

> В контейнере нужны права на `/dev/ttyUSB0` и `/dev/snd`; иногда помогают
> `--group-add dialout --group-add audio`.

## Обратный прокси и HTTPS

```nginx
server {
    listen 443 ssl;
    server_name callbot.example.ru;

    ssl_certificate     /etc/letsencrypt/live/callbot.example.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/callbot.example.ru/privkey.pem;

    client_max_body_size 40m;   # импорт контактов и аудио

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;
        proxy_buffering off;     # живой журнал/скачивание
    }
}
```

Лучше закрыть доступ Basic-аутентификацией сервиса (`CALLBOT_USER`/`CALLBOT_PASSWORD`)
или ограничить IP/VPN.

## Резервные копии

```bash
# данные: БД + записи + синтезированные вопросы
rsync -a /opt/callbot/data/ backup@nas:/backups/callbot/$(date +%F)/
```

SQLite в режиме WAL: копию лучше делать через `sqlite3 data/callbot.db ".backup ..."` или
останавливая сервис на секунды.

## Мониторинг

* `GET /api/health` — проверка живости.
* «Обзор» в интерфейсе: кампании, последние звонки, журнал.
* `journalctl -u callbot` — ошибки модема/ASR.
* Свободное место: записи WAV ~ 16 КБ/с на ответ (≈1 МБ в минуту при 8 кГц mono),
  старые файлы можно чистить, оставляя БД.
