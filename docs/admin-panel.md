# Аутентификация и админка

Две независимых «авторизации» живут в одной Flask-сессии (подписанная cookie):
- `session["user_nickname"]` + `session["user_project"]` — обычный пользователь
- `session["admin"] = True` — флаг админа

Эти поля не связаны: можно быть одновременно зарегистрированным юзером и
админом, можно логиниться админом без регистрации юзером.

## Регистрация пользователя (runpod_manager.py:1217–1230)

```
POST /api/user/register
Body: {"nickname": "vasya", "project": "CV"}
```

Валидация (`validate_user_input`, runpod_manager.py:560–597):
- Никнейм: 1–30 символов, strip, запрет control chars.
- Проект: **должен** быть в `PROJECTS = ["CV","DV","MT","PT","MARK","ADMIN","TV","MW"]`.

При успехе:
1. `touch_user()` — UPSERT в таблице `users` (update `last_seen` или INSERT).
2. `session["user_nickname"] = nick`, `session["user_project"] = proj`,
   `session.permanent = True`.

**Важно**: после регистрации все последующие `/api/pods*` запросы берут
идентификацию **из сессии**, а не из body. Это защита от спуфинга.

Проверка сессии: `GET /api/user/check` → 200 с `{nickname, project}` или 403.
Специально **403, а не 401**, чтобы reverse-proxy (Caddy с basic auth) не
интерпретировал как протухшие креды и не вызывал basic-auth popup
(runpod_manager.py:1232–1241).

Разлогин: `POST /api/user/logout` — чистит только `user_*` ключи из сессии,
`admin` флаг не трогается.

## Админ-логин (runpod_manager.py:1347–1354)

```
POST /api/admin/login
Body: {"password": "admin"}
```

Сравнение cleartext с `admin_password` из `admin_settings.json`:
```python
if data.get("password") == get_settings().get("admin_password", ""):
    session["admin"] = True
```

**Никакого хеширования.** Пароль лежит в файле в открытом виде.
Меняется через UI (POST `/api/admin/settings` с полем `admin_password`).

Проверка: `GET /api/admin/check` → `{"admin": true|false}`. Используется UI
для показа/скрытия админ-панели.

Разлогин: `POST /api/admin/logout` → `session.pop("admin", None)`.

## Декораторы

- `@require_user` (runpod_manager.py:609–621) — если нет `user_nickname`, возврат
  403 с `{"ok": false, "error": "Not registered"}`.
- `@require_admin` (runpod_manager.py:552–558) — если нет `admin=True`, возврат
  401 с `{"ok": false, "error": "Admin only"}`.
- `is_admin()` (runpod_manager.py:456) — `session.get("admin") == True`,
  используется внутри endpoint'ов для conditional логики (например, показывать
  ли hidden-поды в листинге).

## Таблица admin-endpoints

| Route | Метод | Декоратор | Действие |
|-------|-------|-----------|----------|
| `/api/admin/login` | POST | — | Ставит `session["admin"] = True` при совпадении пароля |
| `/api/admin/logout` | POST | — | Снимает `session["admin"]` |
| `/api/admin/check` | GET | — | Возвращает `{admin: bool}` |
| `/api/admin/settings` | GET | `@require_admin` | Возвращает все настройки (включая `admin_password`!) |
| `/api/admin/settings` | POST | `@require_admin` | Обновляет настройки из body; валидация границ (max_pods 1-50, idle_timeout 1-1440 и т.д.) |
| `/api/admin/delete-all` | POST | `@require_admin` | `delete_all_pods(source="manual")` |
| `/api/admin/pods/<pid>/hide` | POST | `@require_admin` | `hide_pod_id(pid, admin_nickname)` |
| `/api/admin/pods/<pid>/unhide` | POST | `@require_admin` | `unhide_pod_id(pid)` |
| `/api/admin/activity` | GET | `@require_admin` | SELECT из `pod_actions` с фильтрами `?from=YYYY-MM-DD&to=YYYY-MM-DD`, LIMIT 500 |

## Семантика настроек

Все настройки живут в **одном JSON-файле** `admin_settings.json` в `DATA_DIR`
(в Docker — `/app/data/admin_settings.json` в volume `runpod-data`).

### `max_pods` (runpod_manager.py:1067–1074)

Лимит одновременно **видимых** RUNNING-подов для обычных пользователей.
Формула:
```
visible_running = sum(1 for p in pods if p.desiredStatus=="RUNNING" and not p.hidden)
if visible_running >= max_pods: raise "Достигнут лимит"
```

**Админы обходят лимит** (`bypass_window=True` в `create_pod()`).

Hidden-поды **не учитываются** в лимите для обычных пользователей — они их
не видят, и их же не должно блокировать.

### `auto_delete_*` (runpod_manager.py:1195–1205)

Ежедневное удаление ВСЕХ running-подов в указанное UTC-время.

- `auto_delete_enabled`: bool
- `auto_delete_time`: `"HH:MM"` UTC
- `auto_delete_last_run`: `"YYYY-MM-DD"` — сегодняшняя дата после запуска
  (guard от повторного срабатывания за ту же минуту)
- `auto_delete_last_log`: текст вида `"[2026-04-06T18:00:01Z] Deleted 3/3"` —
  показывается в UI чтобы админ видел, что произошло

Работает поверх всех подов, включая hidden. При срабатывании
логируется в `pod_actions` как `nickname="AUTODELETE"`, `project="[SYSTEM]"`,
`action="autodelete"`.

### `idle_timeout_*` (runpod_manager.py:1164–1187)

Удалять поды, которые стояли в idle дольше порога.

- `idle_timeout_enabled`: bool
- `idle_timeout_minutes`: int (дефолт 120)

Под считается idle по полю `idleSeconds` из `list_pods()` (время с последнего
`timer_touch`). Если pod ещё не стал ready — `idleSeconds=None` и таймер на него
не действует.

Логируется как `nickname="IDLE_TIMEOUT"`, `project="[SYSTEM]"`,
`action="pod usage timeout auto deleting"`.

### `pod_window_*` (runpod_manager.py:458–550, 1048–1051)

Окно запрета создания новых подов (strategy A: только блокирует создание,
существующие поды работают).

- `pod_window_enabled`: bool
- `pod_window_from`: `"HH:MM"` UTC — начало периода запрета
- `pod_window_until`: `"HH:MM"` UTC — конец периода запрета

Семантика:
- **Same-day** (`from < until`): запрещено в `[from, until)` этого дня.
- **Overnight** (`from > until`): запрещено в `[from, 24:00) ∪ [00:00, until)`.
  Например 22:00–08:00: запрещено всю ночь.
- Ровно в момент `until` — запрет снимается.
- Если `from == until` при включённом флаге — окно считается отключённым.

**Админы обходят окно** (тот же `bypass_window`).

## Hidden pods (v6.4 feature)

Концепция (runpod_manager.py:375–385):
- Pod можно пометить как hidden через `/api/admin/pods/<pid>/hide`.
- **Обычные пользователи**: не видят hidden-поды в `/api/pods`, не могут их
  удалить или запустить (403 blocked в `api_del`/`api_start`), не видят их в
  лимите `max_pods`.
- **Админы**: видят с жёлтой рамкой и иконкой глаза, могут всё.

Hidden-флаг **не освобождает** под от:
- daily auto-delete (удаляется по расписанию)
- idle timeout (удаляется по простою)
- admin "Delete all" (удаляется массово)

Это сознательный выбор — hidden это про **visibility**, а не про billing-
protection. Забытый hidden-под всё равно чистится автоматически.

Хранится в таблице `pod_hidden` (pod_id, hidden_at, hidden_by). Подробности
в [database.md](database.md).

## Админ-панель (UI)

Находится в `FRONTEND_HTML` (runpod_manager.py:1459–2480). Компоненты:

- **Login section** — поле пароля → POST `/api/admin/login`
- **Settings form** — sliders/toggles/inputs для всех описанных выше настроек;
  при сохранении POST-ит разом всё в `/api/admin/settings`
- **Time pickers с TZ-конверсией** (`utcTimeToLocal`, `localTimeToUtc`,
  `getTzLabel`) — админ вводит локальное время, в БД уходит UTC
- **"Delete all" кнопка** — confirmation и POST `/api/admin/delete-all`
- **Activity log** — таблица действий с date-фильтрами from/to, GET
  `/api/admin/activity?from=...&to=...`

Hide/Show кнопка (👁) встроена в карточку пода, отображается только если
`is_admin()`.

## Quirk: админ видит `admin_password` в plain text

`GET /api/admin/settings` возвращает ВСЕ поля включая `admin_password`. UI
показывает его в поле «сменить пароль» как placeholder. Это ок пока админ-
сессия защищена cookie + TLS, но имейте в виду: любой XSS в админ-панели =
утечка пароля.
