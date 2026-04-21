# SQLite БД

Путь: `runpod_manager.db` в `DATA_DIR` (в Docker — `/app/data/runpod_manager.db`
в именованном volume `runpod-data`, выживает рестарты контейнера).

Создаётся при старте в `init_db()` (runpod_manager.py:260–286) через
`CREATE TABLE IF NOT EXISTS`. Миграций нет, схема не менялась — если понадобится
добавить колонку, придётся писать ALTER TABLE руками.

Все таймстемпы — **UTC ISO 8601 с суффиксом `Z`**, например
`2026-04-07T12:34:56Z`. Пишутся либо через `now_iso()` из Python, либо
через `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` в SQLite DEFAULT.

## Полная DDL

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT NOT NULL,
    project TEXT NOT NULL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_seen TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS pod_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT,
    project TEXT,
    action TEXT NOT NULL,
    pod_name TEXT,
    pod_id TEXT,
    ts TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_pa_ts ON pod_actions(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pa_pod ON pod_actions(pod_id, action);

CREATE TABLE IF NOT EXISTS pod_timers (
    pod_id TEXT PRIMARY KEY,
    last_active TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pod_hidden (
    pod_id TEXT PRIMARY KEY,
    hidden_at TEXT NOT NULL,
    hidden_by TEXT NOT NULL
);
```

## Таблицы по порядку

### `users` — реестр зарегистрированных пользователей

- `nickname` + `project` — пара идентификатор (уникальности **нет**,
  теоретически могут быть дубли, но UPSERT в `touch_user()` делает это
  безопасным через SELECT+UPDATE).
- `created_at` — когда первый раз зашёл.
- `last_seen` — обновляется при каждом `POST /api/user/register` (что бывает и
  при повторном логине после `/logout`, не только при первой регистрации).

**Пишется**: `touch_user()` (runpod_manager.py:296–303) из
`/api/user/register`.

**Читается**: нигде в текущей версии кода кроме как для admin-обзора (можно
было бы выводить список активных юзеров, но UI этого не делает).

### `pod_actions` — журнал всех действий

Ключевая таблица для аудита.

- `nickname` — кто делал; спецзначения: `"AUTODELETE"`, `"IDLE_TIMEOUT"`,
  `"ADMIN"` для системных действий.
- `project` — проект этого юзера; для системных действий `"[SYSTEM]"`.
- `action` — текстовая метка: `"create"`, `"delete"`, `"start"`, `"hide"`,
  `"show"`, `"autodelete"`, `"pod usage timeout auto deleting"`.
- `pod_name`, `pod_id` — идентификация пода (name вводится пользователем,
  id приходит от RunPod).
- `ts` — таймстемп действия.

**Индексы**:
- `idx_pa_ts` по `ts DESC` — для быстрой выборки последних N действий в
  activity log.
- `idx_pa_pod` по `(pod_id, action)` — для `get_pod_creators()` (найти
  запись с `action='create'` для списка pod IDs одним запросом).

**Пишется**: `log_action()` (runpod_manager.py:288–294) из:
- `/api/pods` POST (create)
- `/api/pods/<id>` DELETE (delete)
- `/api/pods/<id>/start` POST (start)
- `/api/admin/pods/<id>/hide|unhide` POST (hide/show)
- `check_idle_timeouts()` (idle-удаление)
- `delete_all_pods()` (auto/manual всем)

**Читается**:
- `get_pod_creators()` — batch поиск `action='create'` для обогащения
  `list_pods()`.
- `/api/admin/activity` — админ-панель, SELECT с date-фильтрами, LIMIT 500.

### `pod_timers` — idle-tracking

- `pod_id` — primary key, уникален пока под жив.
- `last_active` — когда под последний раз был busy (`runtime.active=true`
  или `gpuUtil>0 or cpuUtil>0`). Обновляется в `list_pods()`.
- `created_at` — когда ComfyUI первый раз стал ready (т.е. когда этот timer
  был создан).

**Инварианты**:
- Запись **появляется только** после того, как `check_pod_service()` вернул
  `True` хотя бы раз для этого pod_id.
- Запись **удаляется** в `delete_pod()` и `start_pod()` (оба сбрасывают
  таймер, чтобы idle-счёт начинался с нуля после restart).
- Если пользователь никогда не запускал нагрузку, `last_active` остаётся
  равным `created_at` → `idleSeconds` растёт с момента ready.

**Пишется**: `timer_init_if_missing()`, `timer_touch()`, `timer_delete()`
(runpod_manager.py:329–358).

**Читается**: `timer_get_all()` (runpod_manager.py:360–373) — batch запрос
из `list_pods()`.

### `pod_hidden` — скрытые поды

- `pod_id` — primary key.
- `hidden_at` — когда скрыт.
- `hidden_by` — ник админа, который скрыл (админ-сессия даёт
  `session["user_nickname"]` если он был ещё и юзером).

**Пишется**: `hide_pod_id()` (runpod_manager.py:387–403) — INSERT idempotent
через SELECT WHERE EXISTS.

**Удаляется**:
- `unhide_pod_id()` (runpod_manager.py:405–413) — idempotent DELETE.
- Внутри `delete_pod()` (runpod_manager.py:1132) — insurance на случай
  повторного использования pod ID для нового пода.

**Читается**:
- `get_hidden_ids()` — все hidden-id как set, для обогащения в `list_pods()`.
- `is_pod_hidden(pid)` — single pod check в `api_del`/`api_start` для 403-
  блокировки не-админа.

## Бэкап

БД и настройки выживают рестарт контейнера благодаря volume `runpod-data:/app/data`
в `docker-compose.yml`. Чтобы вытащить локальную копию:

```bash
docker compose cp runpod-manager:/app/data/runpod_manager.db ./backup.db
docker compose cp runpod-manager:/app/data/admin_settings.json ./backup-settings.json
```

Чтобы восстановить:

```bash
docker compose cp ./backup.db runpod-manager:/app/data/runpod_manager.db
docker compose cp ./backup-settings.json runpod-manager:/app/data/admin_settings.json
docker compose restart runpod-manager
```

## Что будет, если БД потерять

- `users` — минус список зарегистрированных; все логинятся заново, последние
  активности забыты.
- `pod_actions` — потеряется весь аудит-лог. В activity панели админки
  пусто. **Поды всё равно работают**: идентификация через RunPod API, а не
  через нашу БД.
- `pod_timers` — idle-tracking сбрасывается, таймеры пересоздадутся при
  следующем `list_pods()` когда ComfyUI снова станет ready. **Будет окно в
  пару минут, когда idle-timeout не работает** (пока все running-поды не
  получат новые таймеры).
- `pod_hidden` — все hidden-поды станут видимыми для всех. Не страшно, но
  надо будет руками проскрыть нужные.

Ни одна из этих потерь не ломает работу с подами. Поэтому БД считается
«мягким» state-ом: важна для UX и аудита, но не для управления подами.
