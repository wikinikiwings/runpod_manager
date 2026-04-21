# Жизненный цикл пода

Как менеджер понимает, в какой фазе находится каждый под, и что происходит в
каждой фазе — от `create` до `delete`.

## Фазы

```
  create (GraphQL/CLI)
        │
        ▼
  ┌─ RunPod provisions container ──┐
  │                                 │
  │  start.sh в контейнере:         │
  │   1. Запускает HTTP-сервер     │
  │      на порту 8189 (статус)    │
  │   2. Запускает ComfyUI         │
  │      на порту 8188             │
  └─────────────────────────────────┘
        │
        ▼
  BOOT  — есть /status.json на 8189, {stage, pct, msg, elapsed}
        │
        ▼
  READY — есть /system_stats на 8188, ComfyUI отвечает
        │  └─ timer_init_if_missing(pod_id) — запись в pod_timers
        ▼
  RUNNING — обычная работа, periodic polling /runtime.json
        │
        ├─ busy (active > 0 или idleSeconds=0):
        │    timer_touch(pod_id) — обновить last_active
        │
        └─ idle (active == 0):
             last_active не обновляется, счётчик idle растёт
        │
        ▼
  Delete. Условия:
    • ручное — пользователь/админ жмёт ✕
    • idle timeout — scheduler видит idleSeconds >= idle_timeout_minutes*60
    • daily auto-delete — scheduler срабатывает в auto_delete_time UTC
    • admin "Delete all"
```

## Health-check endpoints

Все три — HTTP GET на `https://{pod_id}-{port}.proxy.runpod.net/...`. RunPod
автоматически выдаёт HTTPS-прокси для указанных в `ports` портов контейнера.

### `/system_stats` на порту 8188 (runpod_manager.py:97–125)

Источник: сам ComfyUI. Отвечает JSON-ом вида `{"system": {...}, "devices": [...]}`.
Мы проверяем `resp.status == 200` и наличие ключа `system` ИЛИ `devices` в JSON.
Если да — `serviceReady = True`.

- Timeout: `SERVICE_CHECK_TIMEOUT = 6s`
- TTL кэша: `SERVICE_CHECK_TTL = 15s`
- Кэш: `_service_cache` под `_service_cache_lock`
- Параллельный fetch: `check_pods_services_parallel()` через `ThreadPoolExecutor(8)`

### `/status.json` на порту 8189 (runpod_manager.py:142–185)

Источник: **не** ComfyUI, а отдельный python HTTP-сервер из `start.sh`
(запускается первым и живёт всё время работы контейнера). Отвечает:

```json
{"stage": "downloading-models", "pct": 42, "msg": "SDXL base", "elapsed": 87}
```

Используется **только в фазу BOOT** (до того, как ComfyUI стал ready) — чтобы
в UI был прогресс-бар вместо «чёрного ящика» на 5 минут.

- Timeout: `BOOT_CHECK_TIMEOUT = 4s`
- TTL кэша: `BOOT_CHECK_TTL = 5s` (короткий, чтобы прогресс-бар был живой)

### `/runtime.json` на порту 8189 (runpod_manager.py:197–238)

Источник: тот же HTTP-сервер из `start.sh`, только **watcher** скрипта tail-ит
лог ComfyUI и инкрементит счётчики по событиям:

- `got prompt` → `total_started++`
- `Prompt executed` → `total_completed++`

Пример:
```json
{
  "active": false,
  "queue_depth": 0,
  "total_started": 88,
  "total_completed": 88,
  "last_event": "Prompt executed",
  "last_event_at": "2026-04-11T13:42:02Z"
}
```

- Timeout: `RUNTIME_CHECK_TIMEOUT = 4s`
- TTL кэша: `RUNTIME_CHECK_TTL = 10s`
- Поле `active` — источник истины для `is_busy` (приоритетнее GPU/CPU-
  телеметрии, если runtime.json доступен).

**Известная проблема**: если ComfyUI крашнул промпт без вывода `Prompt executed`,
`total_started > total_completed` застревает → `active` вечно `true` → idle
timer не стартует → деньги текут. Фикс планируется в `start.sh` (не в
менеджере), детали в `TODO.md`.

## `list_pods()` — сборка полной картины (runpod_manager.py:730–925)

Единая функция, которую зовёт UI при каждом refresh (каждые 15 секунд):

1. Получить список подов одним из 4 способов (по очереди, при неудаче следующий):
   - `try_gql_bearer()` — GraphQL + `Authorization: Bearer <key>`
   - `try_gql_qp()` — GraphQL + `?api_key=...`
   - `try_rest()` — REST `/v1/pods`
   - `try_cli()` — `runpodctl pod list`
2. Параллельно для RUNNING-подов:
   - `check_pods_services_parallel(running_ids)` → serviceReady
   - `check_pods_runtime_parallel(ready_ids)` → runtime.json
3. Параллельно для не-ready:
   - `check_pods_boot_parallel(not_ready_ids)` → bootStage/bootPct/elapsed
4. `timer_get_all(all_ids)` — одним запросом к SQLite получить все таймеры
5. Для каждого пода:
   - Если он ready и в `pod_timers` нет записи — `timer_init_if_missing()`
   - Вычислить `is_busy`: `runtime.active` если есть, иначе
     `(gpuUtil > 0 or cpuUtil > 0)`
   - Если busy — `timer_touch()` (обновить last_active)
   - `idleSeconds = (now - last_active).total_seconds()` если timer есть, иначе
     `None`
6. `get_pod_creators(all_ids)` — одним запросом SELECT из pod_actions
   WHERE action='create' → nickname/project/timestamp создателя (для UI)
7. `get_hidden_ids()` — set подов, помеченных как hidden
8. Возвращаем список pod-dict'ов с полями:
   - базовые: `id`, `name`, `desiredStatus`, `imageName`, `gpuId`, `costPerHr`
   - health: `serviceReady`
   - boot: `bootStage`, `bootPct`, `bootMsg`, `bootElapsed`
   - runtime: `runtime` (dict или null), `isBusy`
   - timer: `idleSeconds`, `createdAt` (когда ComfyUI первый раз стал ready)
   - metadata: `createdBy` ({nickname, project, ts} или null)
   - visibility: `hidden` (bool)

## Удаление пода (`delete_pod()`, runpod_manager.py:1119–1132)

1. `runpodctl pod delete <id>` (для новой CLI; `remove pod <id>` для старой).
   **Да, удаление всегда идёт через CLI** — GraphQL-удаления в коде нет.
2. Очистить все три кэша: `_service_cache`, `_boot_cache`, `_runtime_cache`.
3. `timer_delete(pid)` — удалить из `pod_timers`.
4. `unhide_pod_id(pid)` — удалить из `pod_hidden` (insurance от повторного
   использования pod ID).

Запись в `pod_actions` делает вызывающий код (endpoints `/api/pods/<id>` DELETE,
`check_idle_timeouts`, `delete_all_pods`).

## Перезапуск (`start_pod()`, runpod_manager.py:1133–1142)

Только для подов в статусе `EXITED`. Зовёт `runpodctl pod start <id>`, чистит
`_boot_cache` и `_runtime_cache` (не `_service_cache` — он сам истечёт через
15s), **удаляет таймер** чтобы idle-счёт начался с нуля когда ComfyUI снова
станет ready.

## Авто-удаление по расписанию и idle-timeout

Оба крутятся в `scheduler_loop()` (runpod_manager.py:1192–1209), который
запускается как daemon-тред при старте. Tick раз в 30 секунд.

### Daily auto-delete

```python
if s.get("auto_delete_enabled") and s.get("auto_delete_time"):
    now = now_utc()
    today = now.strftime("%Y-%m-%d")
    h, m = map(int, s["auto_delete_time"].split(":"))
    if now.hour == h and now.minute == m and s.get("auto_delete_last_run", "") != today:
        cnt, msg = delete_all_pods(source="auto")
        # save auto_delete_last_run = today, auto_delete_last_log = msg
```

- Время в **UTC**. UI админки конвертирует в локальное при показе и обратно
  при сохранении.
- Guard `auto_delete_last_run != today` предотвращает повторный запуск в те же
  сутки, даже если tick попадёт несколько раз в минуту HH:MM.
- Работает ВСЕГДА поверх всех подов, включая hidden.

### Idle timeout (`check_idle_timeouts()`, runpod_manager.py:1164–1187)

Вызывается каждый tick (если включено):
```python
threshold = idle_timeout_minutes * 60
for p in running_pods:
    if p.idleSeconds is not None and p.idleSeconds >= threshold:
        delete_pod(p.id)
        log_action("IDLE_TIMEOUT", "[SYSTEM]", "pod usage timeout auto deleting", ...)
```

`p.idleSeconds is None` если ComfyUI ещё не стал ready (нет записи в
`pod_timers`) → такие поды **не** удаляются по idle.

## Конкурентные инстансы менеджера (важная ловушка)

Если на разных ПК запущены два+ RunPod Manager с одним и тем же
`RUNPOD_API_KEY`, **каждый видит и может удалять все поды в аккаунте**.
Симптом: поды удаляются «сами», а в `pod_actions` локальной БД нет записи.

Лечение: при странных удалениях **первым делом** смотреть
[RunPod audit log](https://www.runpod.io/console/user/audit-logs). Он
показывает, из какого IP пришёл delete-запрос — так и палится забытый второй
инстанс.

Эта категория багов **не решается кодом** менеджера (мы знаем только свою БД,
но не чужую). Единственная защита — организационная: не запускать несколько
инстансов под один ключ одновременно.
