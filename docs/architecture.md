# Архитектура `runpod_manager.py`

Файл — Flask-монолит, ~2500 строк, порядок секций (сверху вниз) строгий:
сначала чистые хелперы без зависимостей, потом слои поверх них, в конце —
routes, inline frontend и `main`.

## Карта секций

| Секция | Строки | Что делает |
|--------|--------|-----------|
| Imports, globals, `PRESET`, `DEFAULT_SETTINGS` | 1–87 | Константы конфига пода и дефолтных настроек админа |
| Time helpers | 11–45 | `now_utc()`, `now_iso()`, `parse_iso()` — единый UTC ISO 8601 с суффиксом `Z` для ВСЕХ таймстемпов в БД |
| ComfyUI service health (порт 8188) | 89–131 | `check_pod_service()` → `/system_stats`, TTL 15s, параллельно через `ThreadPoolExecutor(8)` |
| Boot status (порт 8189) | 133–185 | `check_pod_boot_status()` → `/status.json` (stage/pct/msg/elapsed), TTL 5s |
| Runtime activity (порт 8189) | 187–245 | `check_pod_runtime_status()` → `/runtime.json` (active/queue/started/completed), TTL 10s |
| SQLite слой | 247–439 | `init_db()`, `log_action()`, `touch_user()`, `get_pod_creators()`, pod_timers (init/touch/delete/get_all), pod_hidden (hide/unhide/get_ids/is_hidden) |
| Settings | 441–456 | `load_settings()` / `save_settings()` под `_settings_lock`, auto-backfill недостающих ключей из `DEFAULT_SETTINGS` |
| Pod creation window | ~458–550 | `check_pod_window()` — логика запретного окна создания подов (strategy A) |
| User validation + session | 560–630 | `validate_user_input()`, `get_session_user()`, декораторы `@require_user`, `@require_admin`, `is_admin()` |
| API key + HTTP/CLI utils | 635–725 | `resolve_api_key()` (CLI → env → `~/.runpod/config.toml`), `http_request()`, `detect_cli()`, `run_cmd()`, `humanize_cli_error()` |
| Pod listing (multi-fallback) | 730–925 | `list_pods()` + `try_gql_bearer()` / `try_gql_qp()` / `try_rest()` / `try_cli()`, обогащение каждого пода (health, boot, runtime, timers, hidden, creator) |
| **GraphQL deploy** | 927–1038 | `DEPLOY_MUTATION` + `create_pod_via_graphql()` — primary path создания пода |
| Pod operations | 1040–1188 | `create_pod()` (GraphQL → CLI fallback), `delete_pod()`, `start_pod()`, `next_name()`, `delete_all_pods()`, `check_idle_timeouts()` |
| Scheduler | 1189–1209 | `scheduler_loop()` — daemon-тред, tick раз в 30s: daily auto-delete + idle timeout |
| API routes | 1212–1454 | `/api/projects`, `/api/user/*`, `/api/pods`, `/api/admin/*` |
| Inline HTML SPA | 1459–2480 | `FRONTEND_HTML` — вся вёрстка, стили, JS в одной строке (~1000 строк) |
| Root + favicon | 2483–2491 | `/` возвращает `FRONTEND_HTML`, `/favicon.ico` → 204 |
| `main` | 2493–2512 | argparse, logging, `detect_cli()`, `init_db()`, старт scheduler-треда, `app.run()` |

## Глобальные константы

### `PRESET` (runpod_manager.py:47–66)

Базовый конфиг пода. Используется и GraphQL-путём, и CLI-fallback-ом.

```python
PRESET = {
    "gpu_id": "NVIDIA RTX PRO 4500 Blackwell", "gpu_count": 1,
    "template_id": "i3j2sm66q8", "image": "wikiniki/comfy_runpod:latest",
    "network_volume_id": "0czgom7b1j", "volume_mount_path": "/workspace",
    "volume_in_gb": 0, "container_disk_in_gb": 20, "cloud_type": "SECURE",
    "env": {"COMFY_API_KEY": "{{ RUNPOD_SECRET_comfyui_api_partners_secret }}"},
    "comfy_port": 8188, "pod_name_prefix": "pod_",
    # GraphQL-only поля:
    "data_center_id": "EU-RO-1",
    "min_memory_in_gb": 62,
    "min_vcpu_count": 28,
    "ports": "8188/http,8888/http,8686/http,8189/http",
    "start_ssh": True, "start_jupyter": True,
    "global_network": False,
}
```

**Важно при смене GPU:** `min_memory_in_gb` и `min_vcpu_count` должны соответствовать
характеристикам нового типа (смотреть в `runpodctl get cloud` или на RunPod UI).
`data_center_id` **заблокирован** локацией `network_volume_id` — если меняете
volume, меняйте и DC.

### `PROJECTS` (runpod_manager.py:67)

Whitelist проектов для регистрации пользователей:
```python
PROJECTS = ["CV", "DV", "MT", "PT", "MARK", "ADMIN", "TV", "MW"]
```

При регистрации `validate_user_input()` отбрасывает всё, чего нет в этом списке.

### `DEFAULT_SETTINGS` (runpod_manager.py:78–87)

Значения, которыми бэкфиллится `admin_settings.json` если каких-то ключей нет:

| Ключ | Дефолт | Назначение |
|------|--------|-----------|
| `admin_password` | `"admin"` | Cleartext (!), сравнивается напрямую в `/api/admin/login` |
| `max_pods` | `5` | Лимит одновременно видимых RUNNING-подов для не-админа |
| `auto_delete_enabled` | `False` | Ежедневное авто-удаление всех подов в указанное UTC-время |
| `auto_delete_time` | `"21:00"` | UTC `HH:MM` |
| `auto_delete_last_run` / `auto_delete_last_log` | `""` | Guard против двойного срабатывания в одни сутки + текст результата для UI |
| `idle_timeout_enabled` | `True` | Удалять поды простоявшие > N минут |
| `idle_timeout_minutes` | `120` | Порог простоя в минутах |
| `pod_window_enabled` | `False` | Окно запрета создания подов |
| `pod_window_from` / `pod_window_until` | `"22:00"` / `"08:00"` | UTC `HH:MM`, период запрета (overnight поддерживается) |

## Кэши и их TTL

Все three TTL-кэша живут в процессе памяти (при рестарте пересоздаются).
Каждый защищён своим `threading.Lock()`.

| Кэш | TTL | Источник | Очищается в |
|-----|-----|----------|-------------|
| `_service_cache` | 15s | `https://{pod_id}-8188.proxy.runpod.net/system_stats` | `delete_pod()` |
| `_boot_cache` | 5s | `https://{pod_id}-8189.proxy.runpod.net/status.json` | `delete_pod()`, `start_pod()` |
| `_runtime_cache` | 10s | `https://{pod_id}-8189.proxy.runpod.net/runtime.json` | `delete_pod()`, `start_pod()` |

Параллелизм — `ThreadPoolExecutor(max_workers=8)` в `check_pods_*_parallel`,
вызывается из `list_pods()`.

## Модули стандартной библиотеки, никаких зависимостей

Кроме Flask — только стандартные модули Python: `urllib.request`, `sqlite3`,
`threading`, `subprocess` (для `runpodctl`), `re`, `json`, `logging`.
**Нет** `requests`, `httpx`, `SQLAlchemy`, `gevent` и т.п.

Установка в Dockerfile: `pip install --no-cache-dir flask`.

## Главная точка входа

`runpod_manager.py:2493–2512`:

1. `argparse` парсит `--host`, `--port`, `--api-key`, `--debug`.
2. Настраивает логгер.
3. `detect_cli()` — проверяет, что `runpodctl` есть в PATH, записывает
   `_cli_path` и `_cli_is_new` (новая версия имеет JSON-вывод).
4. `resolve_api_key()` — CLI arg → env `RUNPOD_API_KEY` → `~/.runpod/config.toml`.
   Пишет в глобальную `_api_key`.
5. `init_db()` — `CREATE TABLE IF NOT EXISTS` для всех 4 таблиц.
6. Стартует scheduler-тред: `threading.Thread(target=scheduler_loop, daemon=True).start()`
   (`daemon=True` → умрёт вместе с Flask без явного shutdown).
7. `app.run(host=..., port=..., debug=...)`.

Если на любом шаге ошибка — логируется и продолжаем (например, без API key
можно зайти в UI, но операции с подами не будут работать).
