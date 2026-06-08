# SQLite БД

Путь: `runpod_manager.db` в `DATA_DIR` (в Docker — `/app/data/runpod_manager.db`
в именованном volume `runpod-data`, выживает рестарты контейнера).

Создаётся при старте в `init_db()` через `CREATE TABLE IF NOT EXISTS`. Схема
наращивается только добавлением новых таблиц (`CREATE TABLE IF NOT EXISTS`
идемпотентен) — `ALTER TABLE` для существующих таблиц нигде не используется.
Единственная настоящая миграция данных — one-shot `pod_hidden → pod_assignment`
внутри `init_db()` (см. раздел ниже); она идемпотентна и больше не срабатывает
после первого прогона.

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

CREATE TABLE IF NOT EXISTS pod_assignment (
    pod_id TEXT PRIMARY KEY,
    assigned_project TEXT,
    counts_toward_quota INTEGER NOT NULL DEFAULT 1,
    creation_source TEXT NOT NULL DEFAULT 'user',
    assigned_at TEXT NOT NULL,
    assigned_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pod_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_name            TEXT NOT NULL,
    assigned_project    TEXT,
    counts_toward_quota INTEGER NOT NULL DEFAULT 1,
    creation_source     TEXT NOT NULL DEFAULT 'user',
    requested_by        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL,
    last_attempt_at     TEXT,
    last_error          TEXT,
    pod_id              TEXT,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pr_status ON pod_request(status);
```

## Таблицы по порядку

### `users` — реестр зарегистрированных пользователей

- `nickname` + `project` — пара идентификатор (уникальности **нет**,
  теоретически могут быть дубли, но UPSERT в `touch_user()` делает это
  безопасным через SELECT+UPDATE).
- `created_at` — когда первый раз зашёл.
- `last_seen` — обновляется при каждом `POST /api/user/register` (что бывает и
  при повторном логине после `/logout`, не только при первой регистрации).

**Пишется**: `touch_user()` из
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

**Пишется**: `log_action()` из:
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

**Пишется**: `timer_init_if_missing()`, `timer_touch()`, `timer_delete()`.

**Читается**: `timer_get_all()` — batch запрос
из `list_pods()`.

### `pod_assignment` — project ownership + admin-only visibility

Replaces the old `pod_hidden` table (see Migration below).

DDL:

```sql
CREATE TABLE IF NOT EXISTS pod_assignment (
    pod_id TEXT PRIMARY KEY,
    assigned_project TEXT,                           -- NULL = unassigned (admin-only)
    counts_toward_quota INTEGER NOT NULL DEFAULT 1,  -- 0/1 boolean
    creation_source TEXT NOT NULL DEFAULT 'user',    -- 'user' | 'admin' | 'external'
    assigned_at TEXT NOT NULL,
    assigned_by TEXT NOT NULL
);
```

Semantics:
- `assigned_project` — project this pod belongs to. NULL means "unassigned" —
  only admin sees the pod; users never see it.
- `counts_toward_quota` — whether this pod occupies a slot in the project's
  quota. User-created pods always set 1. Admin-created may set 0 (the admin
  explicitly chose "не считать в квоту").
- `creation_source` — driver for UI badges. Set ONCE at first INSERT and
  preserved on every UPDATE (source immutability):
  - `'user'` — created by a regular user through the manager.
  - `'admin'` — created by admin through the manager.
  - `'external'` — not created through this manager (e.g., via RunPod's web
    UI). Assigned to `'external'` on first `/assign` call for a pod that has
    no prior `pod_assignment` row and no `pod_actions.create` entry.

**Пишется:**
- `upsert_pod_assignment()` — called from `api_pods_post` after a successful
  pod create (user or admin), and from `api_admin_pod_assign` for reassigns.
- `migrate_to_pod_assignment()` — one-shot at startup if `pod_hidden` exists.

**Читается:**
- `get_pod_assignment(pid)` — single-pod lookup, used in `api_del`/`api_start`
  for visibility check and in `api_admin_pod_assign` to preserve source.
- `get_assignments_batch(pod_ids)` — bulk fetch used in `list_pods()` to
  annotate each pod with `assignedProject`, `countsTowardQuota`,
  `creationSource` fields.

**Удаляется:**
- `delete_pod_assignment(pid)` — called from `delete_pod()` to clean up on
  pod removal.

### Migration from `pod_hidden` → `pod_assignment`

One-shot, runs inside `init_db()` at process startup. Idempotent: if
`pod_hidden` no longer exists (already migrated), the migration does nothing.

Steps:
1. If `pod_hidden` table exists: for every row, INSERT into pod_assignment
   with `assigned_project=NULL, counts_toward_quota=0, creation_source='user',
   assigned_by='migration'`.
2. For every most-recent `pod_actions.create` (grouped by `pod_id`), if not
   already in pod_assignment AND `project in PROJECTS`: INSERT with
   `assigned_project=<project>, counts_toward_quota=1, creation_source='user',
   assigned_by=<nickname>`.
3. If we touched `pod_hidden`: DROP TABLE pod_hidden.

Covered by `tests/test_migration.py` (stdlib unittest; runs via
`python -m unittest tests.test_migration`).

### `pod_request` — заявки на под (авторетрай)

DB-backed-очередь для фичи авторетрая: если `create` падает из-за нехватки
видеокарт, заявка кладётся сюда и переживает рестарт. Фоновый поток
`pod_request_loop` → `process_pending_requests()` повторяет деплой, пока не успех
или таймаут. Полное описание потока — `docs/graphql-deploy.md` (раздел
«Авторетрай»), состояния — `docs/pod-lifecycle.md`.

Колонки:
- `id` — autoincrement PK (в отличие от остальных таблиц — заявка не привязана к
  `pod_id`, пока под не создан).
- `pod_name` — зарезервированное имя будущего пода (`next_name()` учитывает
  pending-заявки, чтобы не было коллизий).
- `assigned_project` / `counts_toward_quota` / `creation_source` — переносятся в
  `pod_assignment` при успехе (та же семантика, что у прямого create).
- `requested_by` — никнейм заказчика.
- `status` — `pending | fulfilled | timed_out | failed | cancelled`. Только
  `pending` обрабатывается воркером; `pending/timed_out/failed` рендерятся
  карточками (`fulfilled/cancelled` — нет).
- `created_at` — отсчёт таймаута (`pod_request_timeout_minutes`).
- `last_attempt_at` / `last_error` — обновляются на каждой ретрай-попытке;
  `last_error` показывается в карточке `failed`.
- `pod_id` — заполняется id созданного пода при `fulfilled`.
- `finished_at` — момент перехода в любое терминальное состояние.

Индекс `idx_pr_status` — воркер фильтрует по `status='pending'` каждую итерацию.

**Пишется/читается**: CRUD-хелперы `create_pod_request`, `list_pending_requests`,
`list_visible_requests`, `get_pod_request`, `update_pod_request`,
`delete_pod_request`, `pending_request_names`, `count_pending_quota`. Покрыто
`tests/test_pod_request.py`.

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
- `pod_assignment` — все assign-данные потеряются. Все поды станут
  unassigned (видны только админу). Unassigned-поды не считаются в квоты.
  Нужно будет заново назначить проекты через `/assign` для каждого пода.
- `pod_request` — потеряются незавершённые заявки на под. Авторетрай для них
  остановится; пользователю надо будет оставить заявку заново. Уже созданные
  (fulfilled) поды не затрагиваются.

Ни одна из этих потерь не ломает работу с подами. Поэтому БД считается
«мягким» state-ом: важна для UX и аудита, но не для управления подами.
