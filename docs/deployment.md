# Deployment

## Запуск

```bash
cd E:\my_stable\runpod_manager
docker compose up -d --build
# UI: http://localhost:5001
```

Первый билд скачает Python base image, `runpodctl` бинарь и поставит Flask —
2-3 минуты. Последующие пересборки — секунды, если не трогали Dockerfile.

Остановка:
```bash
docker compose down              # оставит volume с БД и настройками
docker compose down -v           # УДАЛИТ volume (потеряете аудит-лог и настройки!)
```

Логи:
```bash
docker compose logs -f runpod-manager
```

## Dockerfile (разбор по строкам)

```dockerfile
FROM python:3.12-slim                            # Минимальный образ ~45 MB

# Скачиваем последний runpodctl бинарь (не из pip!)
ADD https://github.com/runpod/runpodctl/releases/latest/download/runpodctl-linux-amd64 /usr/local/bin/runpodctl
RUN chmod +x /usr/local/bin/runpodctl

RUN pip install --no-cache-dir flask             # Единственная pip-зависимость

WORKDIR /app
COPY runpod_manager.py .                         # Только сам файл приложения

VOLUME /app/data                                  # Том для БД и settings
ENV DATA_DIR=/app/data                           # Переменная указывает на volume

EXPOSE 5001
CMD ["python", "runpod_manager.py", "--host", "0.0.0.0", "--port", "5001"]
```

**Что не включено** в образ (из `.dockerignore`):
- `__pycache__`, `*.pyc`
- `*.db` (БД должна браться из volume, не из контекста билда)
- `admin_settings.json` (тоже из volume)
- `.env` (RUNPOD_API_KEY приходит через env var)
- `runpod_manager_header.tmp`, локальные заметки

## docker-compose.yml

```yaml
services:
  runpod-manager:
    build: .
    container_name: runpod-manager
    restart: unless-stopped           # Автоматический рестарт при падении
    ports:
      - "5001:5001"                   # Публикуем UI наружу
    environment:
      - RUNPOD_API_KEY=${RUNPOD_API_KEY}   # Из .env
      - DATA_DIR=/app/data            # Совпадает с ENV в Dockerfile
    volumes:
      - runpod-data:/app/data         # Именованный volume, выживает rebuild

volumes:
  runpod-data:                        # Docker сам управляет
```

**Почему именованный volume, а не bind-mount**: на Windows-хосте с bind-mount
SQLite иногда упирается в проблемы с locking; named volume хранится внутри
Docker Desktop VM и работает стабильно. Backup делается через
`docker compose cp` (см. [database.md](database.md)).

## Переменные окружения

| Переменная | Где задаётся | Назначение |
|------------|--------------|-----------|
| `RUNPOD_API_KEY` | `.env` → `docker-compose.yml` → контейнер | Bearer token для GraphQL-мутаций и листинга |
| `DATA_DIR` | `docker-compose.yml` + Dockerfile ENV | Папка для БД и `admin_settings.json`; по умолчанию = `BASE_DIR` (рядом с `.py`-файлом) |

При локальном запуске (не через Docker):
```bash
set RUNPOD_API_KEY=rpa_...                        # Windows cmd
$env:RUNPOD_API_KEY="rpa_..."                     # PowerShell
export RUNPOD_API_KEY=rpa_...                     # bash
python runpod_manager.py --port 5001
```

## Откуда читается API key (`resolve_api_key`, runpod_manager.py:635–645)

Порядок приоритета (первое найденное побеждает):

1. **CLI-аргумент** `--api-key <key>` — оверрайд для дебага.
2. **ENV variable** `RUNPOD_API_KEY` — продовый путь в Docker.
3. **`~/.runpod/config.toml`** — fallback, читается только на хосте с
   залогиненым `runpodctl` (на Windows это `C:\Users\<user>\.runpod\config.toml`).
   В контейнере этого пути обычно нет.

Если все три пусты — `_api_key = ""`, UI запускается, но любая операция с
подами отвалится с ошибкой `no API key configured` или уйдёт только на CLI-путь.

## Порты

- `5001` — Flask UI (через `ports: 5001:5001` в compose).
- Внутри каждого **пода** (не менеджера):
  - `8188` — ComfyUI
  - `8189` — start.sh's HTTP-сервер со `/status.json` и `/runtime.json`
  - `8888` — Jupyter (если `startJupyter=True`)
  - `8686` — резерв под ComfyUI-Manager/SSH-прокси

  Эти порты публикуются через `ports: "8188/http,8888/http,8686/http,8189/http"`
  в GraphQL `DeployOnDemand` input. RunPod автоматически выдаёт HTTPS-прокси
  вида `https://{pod_id}-{port}.proxy.runpod.net`.

## Здоровье контейнера

Healthcheck в compose **не настроен** — `restart: unless-stopped` покрывает
падение процесса Python, но если Flask висит не отвечая, Docker не заметит.

Можно добавить:
```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5001/api/projects', timeout=5)"]
  interval: 30s
  timeout: 10s
  retries: 3
```

## Reverse proxy (Caddy/Nginx)

Комментарий в коде (runpod_manager.py:1232–1241) упоминает, что deployment
может быть за reverse proxy с basic auth. Поэтому используется **403, а не 401**
для unauthenticated пользователя, чтобы proxy не перехватывал редирект и не
показывал свой login popup.

Пример Caddyfile (если нужен внешний доступ с basic auth):
```
runpod.example.com {
    basicauth {
        admin $2a$14$...hashed...
    }
    reverse_proxy localhost:5001
}
```

## Типичный путь данных при старте Docker-контейнера

```
docker compose up
  └─ docker читает .env → проставляет RUNPOD_API_KEY
  └─ запускает python runpod_manager.py --host 0.0.0.0 --port 5001
      └─ argparse → --host, --port
      └─ logging.basicConfig(INFO)
      └─ detect_cli() → /usr/local/bin/runpodctl, _cli_is_new=True
      └─ resolve_api_key() → читает env RUNPOD_API_KEY → _api_key = "rpa_..."
      └─ init_db() → /app/data/runpod_manager.db (создаёт таблицы при первом старте)
      └─ load_settings() → /app/data/admin_settings.json (создаёт если нет)
      └─ threading.Thread(scheduler_loop, daemon=True).start()
      └─ app.run(host=0.0.0.0, port=5001)
          ✓ Готов принимать HTTP-запросы
```

## Multi-arch / архитектура

Dockerfile скачивает `runpodctl-linux-amd64` напрямую, без multi-arch варианта.
На ARM-хостах (Apple Silicon с Docker Desktop) работает через QEMU-эмуляцию —
медленнее, но рабочий. Для нативного ARM нужно было бы скачивать другой бинарь
и ставить `platforms: linux/arm64` в compose.
