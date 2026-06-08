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
| `/api/admin/pods/<pid>/assign` | POST | `@require_admin` | Assign or reassign a pod to a project. Body: `{project: string|null, counts_toward_quota: bool}`. `project=null` means unassigned (admin-only visibility). Works on any pod in RunPod's listing, including externally-created ones. |
| `/api/admin/activity` | GET | `@require_admin` | SELECT из `pod_actions` с фильтрами `?from=YYYY-MM-DD&to=YYYY-MM-DD`, LIMIT 500 |

## Семантика настроек

Все настройки живут в **одном JSON-файле** `admin_settings.json` в `DATA_DIR`
(в Docker — `/app/data/admin_settings.json` в volume `runpod-data`).

### `project_quotas` (runpod_manager.py)

Dict mapping project name → integer max-pods quota for that project.

```json
{
  "CV": 4,
  "DV": 4,
  "MT": 4,
  "PT": 4,
  "MARK": 4,
  "ADMIN": 50,
  "TV": 4,
  "MW": 4
}
```

When a user creates a pod, the pod is assigned to their project and
`counts_toward_quota=1`. If a pod already exists in RunPod's listing
but was not created through this manager (external), admin can assign it
and set `counts_toward_quota` independently (e.g., set to 0 if the pod
should not consume a slot). Unassigned pods (`assigned_project IS NULL`)
do not count toward any quota.

**Админы обходят лимит**: при создании админ может выбрать любой проект
(или "не назначен") и явно установить флаг "считать в квоту".

### `max_pods` (deprecated)

Kept in `DEFAULT_SETTINGS` for backward compat but **not read**. Replaced
by per-project quotas in `project_quotas`.

### `auto_delete_*` + per-project offset

Ежедневное удаление running-подов, с возможностью **сдвига** по каждому
проекту (bypass-на-N-минут).

**Базовые поля:**
- `auto_delete_enabled`: bool — глобальный включатель планировщика.
- `auto_delete_time`: `"HH:MM"` UTC — базовое время удаления.
- `auto_delete_last_run`: `"YYYY-MM-DD"` — legacy guard, всё ещё пишется
  для отображения в UI «Last: ...».
- `auto_delete_last_log`: текст `"[ts] CV: 2/2; DV: 1/1"` — сводная запись
  что именно сработало в последнем цикле (добавляется в UI).

**Per-project offset** (добавлено 2026-04-21):
- `project_autodelete_offset_minutes`: dict `{"CV":0, "DV":60, ...}` — сдвиг
  в минутах относительно базового времени. 0 = удалять в базовое время.
  Range 0-1440 (до 24ч). Валидируется в `api_admin_settings_post`.
- `project_autodelete_last_run`: dict `{"CV":"2026-04-21", "__unassigned__":"2026-04-21", ...}`
  — per-project guard от двойного срабатывания в те же UTC-сутки.
  Заполняется `scheduler_loop` автоматически.

**Как работает планировщик**:
- `scheduler_loop` тикает каждые 30с. Если `auto_delete_enabled==True`:
  - Для каждого из 8 PROJECTS: вычисляет эффективное время
    `(base_total_minutes + offset[proj]) % 1440`. Если `now.hour == eff_h` и
    `now.minute == eff_m` и `last_run[proj] != today`, вызывает
    `delete_project_pods(proj)` и обновляет `last_run[proj] = today`.
  - Для unassigned-бакета (поды с `assigned_project IS NULL`): эффективное
    время всегда базовое (offset не применяется). Ключ guard-а —
    `"__unassigned__"`.
- Кнопка **«Delete all now»** в UI по-прежнему дёргает `delete_all_pods`
  который бьёт всех running подов одним заходом (не учитывает offset).

**Примеры**:
- Base `21:00` UTC, CV offset `60`, DV offset `0` → CV удаляется в 22:00,
  DV в 21:00, unassigned в 21:00.
- Base `22:00`, MW offset `200` → MW удаляется в 01:20 следующих суток.

Каждое фактическое удаление пода логируется в `pod_actions` как
`nickname="AUTODELETE"`, `project="[SYSTEM]"`, `action="autodelete"`.

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

### `pod_request_*` (авторетрай заявки на под)

Тюнинг фоновой ретрай-очереди (`pod_request_loop` → `process_pending_requests`).
Полный механизм — `docs/graphql-deploy.md` (раздел «Авторетрай»).

- `pod_request_timeout_minutes`: int (дефолт 15, clamp 1..1440) — сколько всего
  держим заявку и ретраим деплой, прежде чем перевести её в `timed_out`. Отсчёт
  от `created_at`.
- `pod_request_retry_interval_seconds`: int (дефолт 15, clamp 5..600) — пауза
  между попытками деплоя. Читается воркером каждую итерацию, поэтому меняется
  **без рестарта**.

POST-валидация в `api_admin_settings_post` (тот же паттерн `max(min,min(max,int))`,
что у idle_timeout). В UI — секция «🔁 Авторетрай заявки на под» (`loadAdminPanel`),
поля `sReqTimeout` / `sReqInterval`, отправляются в `sbSave`.

## Pod Assignment (per-project quotas feature)

Концепция:
- **Pod creation**: admin picks a project (default = admin's session project)
  or "unassigned" (NULL). Optional checkbox "counts_toward_quota" (default true
  for user-created, admin can disable for externally-created pods).
- **Pod visibility**: regular users only see pods where `assigned_project`
  matches their own project. Unassigned pods (`assigned_project IS NULL`)
  are admin-only.
- **Reassignment**: admin can click "Назначить" button on any pod card to open
  a modal: project dropdown + checkbox. Works on any pod that exists in RunPod's
  listing, even if it was created outside this manager (external source).
- **Quota enforcement**: when creating a new pod, count the number of RUNNING
  pods in the user's project where `counts_toward_quota=1`. If >= quota, reject.
  Admin-created pods bypass this check.
- **Pod naming** (added 2026-04-21): каждый проект имеет свой namespace для
  имён. User/admin создаёт под в CV → `cv_pod_1`, `cv_pod_2`, ... Unassigned
  админ-поды получают legacy-префикс `pod_N`. Счётчик per-prefix: в каждом
  проекте нумерация начинается с 1 и не конфликтует с другими проектами.
  Реализовано в `pod_name_prefix(project)` + `next_name(pods, project)` в
  `runpod_manager.py`. Старые поды с глобальным `pod_N` именем остаются как
  есть (переименования не делается).

Хранится в таблице `pod_assignment` (pod_id, assigned_project, counts_toward_quota,
creation_source, assigned_at, assigned_by). Подробности в [database.md](database.md).

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
