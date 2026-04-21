# Per-project pod quotas — design spec

**Date:** 2026-04-21
**Status:** Draft, awaiting implementation
**Scope:** `runpod_manager.py` + inline HTML/JS + `admin_settings.json` schema + SQLite migration

## Problem

Сейчас `max_pods` — одна глобальная квота на всех юзеров. Все юзеры видят
все non-hidden поды. Проект, указанный при регистрации, используется только
как метка в `pod_actions`.

Нужно: каждый проект (CV, DV, MT, PT, MARK, ADMIN, TV, MW) получает свою
настраиваемую квоту. Юзер видит и создаёт поды **только** в рамках своего
проекта. Админ видит всё, обходит квоты, и может вручную назначать/
переназначать существующие поды проектам (в т.ч. оставить без назначения —
тогда пода видит только админ).

Ограничение: **не ломать существующую статистику** (`pod_actions` продолжает
логироваться как сейчас для всех действий, остаётся полностью доступен в
админ-панели Activity log).

## Модель данных

### 1. Таблица `pod_assignment` (новая, заменяет `pod_hidden`)

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

Семантика:
- `assigned_project` NOT NULL → под видим юзерам проекта + админу, в квоте
  проекта учитывается если `counts_toward_quota=1`.
- `assigned_project IS NULL` → под видит только админ (эквивалент прежнего
  «hidden»).
- `counts_toward_quota` — всегда `1` для user-created подов; может быть `0`
  только когда админ явно поставил чекбокс (см. create/reassign flow).
- `creation_source` — откуда под взялся, влияет на UI-бейджи (не на
  видимость и не на квоту). Устанавливается один раз при **первой** записи
  в `pod_assignment` и более не меняется (`/assign` сохраняет существующее
  значение):
  - `'user'` — под создан обычным юзером через менеджер.
  - `'admin'` — под создан админом через менеджер (флаг `is_admin()==True`
    на момент `POST /api/pods`).
  - `'external'` — под не создавался через менеджер; существует в RunPod
    (например, через web UI), и админ назначает его проекту впервые через
    `/assign`. Определяется при assign-е: если `pod_assignment` ещё нет
    И нет `pod_actions.create` для этого pod_id → `'external'`.

### 2. `admin_settings.json` — новое поле `project_quotas`

```json
{
  "admin_password": "...",
  "max_pods": 3,                                      // DEPRECATED, ignored
  "project_quotas": {
    "CV": 4, "DV": 4, "MT": 4, "PT": 4,
    "MARK": 4, "ADMIN": 4, "TV": 4, "MW": 4
  },
  "auto_delete_enabled": false,
  "auto_delete_time": "21:00",
  "idle_timeout_enabled": true,
  "idle_timeout_minutes": 120,
  "pod_window_enabled": false,
  "pod_window_from": "22:00",
  "pod_window_until": "08:00"
}
```

- `max_pods` остаётся в файле для обратной совместимости, но нигде в коде не
  читается для проверки квоты. Сохранение обеспечивает, что старые админ-
  скрипты/манипуляции с файлом не падают.
- `project_quotas` — единственный источник квот; ключи — проекты из `PROJECTS`.
- `DEFAULT_SETTINGS` бэкфиллит недостающие ключи (см. миграцию ниже).

### 3. `pod_actions` — новый тип `action='assign'`

Без изменения схемы таблицы. Появляется дополнительный тип события:
- `action='assign'` — когда админ назначает/переназначает проект поду.
- `nickname` = `session["user_nickname"]` админа (у админа всегда есть
  user-сессия, т.к. роут стоит за `@require_admin`, который не подразумевает
  user-логин, но на практике админ заходит и как user тоже; если всё-таки
  user-сессии нет — пишем `"ADMIN"`).
- `project` = `session["user_project"]` админа по тем же правилам; иначе
  `"[SYSTEM]"`.
- `pod_name`, `pod_id` — идентификация пода.
- Семантика изменения (что на что поменялось) пишется в серверные логи
  (stdout); в БД хранится только факт события для аудита.

Прочие существующие action (`create`, `delete`, `start`, `hide`, `show`,
`autodelete`, `pod usage timeout auto deleting`) остаются. `hide`/`show`
больше не пишутся, т.к. endpoints удаляются, но исторические записи в
таблице не трогаем (не делаем UPDATE на них, не удаляем).

### 4. Таблица `pod_hidden` — DROPPED после миграции

Удаляется только после успешной миграции данных в `pod_assignment` (см.
раздел Migration).

## Алгоритмы

### Проверка квоты при создании пода

```
DEFAULT_PROJECT_QUOTA = 4       # Python-уровень константа, fallback

create_pod(name, bypass_window=is_admin()):
    if not bypass_window:
        check_pod_window()                        # как было
        project = session["user_project"]
        quotas = settings.get("project_quotas", {})
        quota = quotas.get(project, DEFAULT_PROJECT_QUOTA)
        count = SELECT COUNT(*) FROM running_pods
                JOIN pod_assignment USING (pod_id)
                WHERE pod_assignment.assigned_project = project
                  AND pod_assignment.counts_toward_quota = 1
        if count >= quota:
            raise RuntimeError(f"Достигнут лимит {project}: {count}/{quota}")

    ...проксируем create через GraphQL или CLI как сейчас...

    after successful create:
        if is_admin():
            # admin передаёт assigned_project и counts_toward_quota из формы
            ap = request.json.get("assigned_project")   # string или None
            cf = 1 if request.json.get("counts_toward_quota") else 0
            src = 'admin'
            # валидация: ap либо None, либо в PROJECTS; иначе 400
        else:
            ap = session["user_project"]
            cf = 1                                # всегда 1 для юзеров
            src = 'user'
        INSERT INTO pod_assignment(pod_id, assigned_project, counts_toward_quota,
                                    creation_source, assigned_at, assigned_by)
                VALUES (pod_id, ap, cf, src, now_iso(), session["user_nickname"])
        log_action("create", ...)
```

`DEFAULT_PROJECT_QUOTA = 4` применяется, если какой-то проект отсутствует в
`project_quotas` (не должно случаться после миграции, но защитный дефолт).

### Фильтрация в `list_pods()`

```
list_pods():
    ...собираем все поды от RunPod как сейчас...

    assignments = SELECT pod_id, assigned_project, counts_toward_quota
                  FROM pod_assignment

    for pod in pods:
        a = assignments.get(pod.id)
        pod.assignedProject = a.assigned_project if a else None
        pod.countsTowardQuota = bool(a.counts_toward_quota) if a else True

    if is_admin():
        return pods   # админу видно всё, включая unassigned
    else:
        user_project = session["user_project"]
        return [p for p in pods if p.assignedProject == user_project]
```

Внешние поды (созданные вне менеджера) и поды ещё без записи в
`pod_assignment` — для юзера НЕ видны (нет match по `assigned_project`),
для админа видны как «unassigned» (маркер в UI).

### Видимость операций

```
api_del(pid):                  # DELETE /api/pods/<pid>
    if not is_admin():
        a = pod_assignment WHERE pod_id = pid
        if a is None or a.assigned_project != session.user_project:
            return 403
    delete_pod(pid)
    log_action("delete", ...)
```

Так же для `/api/pods/<pid>/start`.

### Reassign pod (admin) — работает и для external-подов

```
POST /api/admin/pods/<pid>/assign
Body: {"project": "CV" | null, "counts_toward_quota": true | false}

@require_admin:
    # Валидация pid: либо есть запись в pod_assignment, либо pod существует
    # в текущем list_pods(). Иначе 404.
    existing = SELECT * FROM pod_assignment WHERE pod_id = pid
    if existing:
        src = existing.creation_source       # сохраняем историю
    else:
        # первый раз назначаем этот под
        has_create = EXISTS(SELECT 1 FROM pod_actions WHERE pod_id=pid AND action='create')
        src = 'external' if not has_create else 'user'
        # ('user' для подов, созданных до деплоя этой фичи, но ещё не
        # получивших pod_assignment — fallback после migration)

    UPSERT pod_assignment(pod_id, project, counts_toward_quota, src,
                           now_iso(), admin_nick)
    log_action("assign", admin_nick, admin_project, pod_name, pod_id)
    return 200
```

Валидация: `project` либо `null`, либо в `PROJECTS` whitelist (иначе 400).
`counts_toward_quota` — bool.

**External-поды в админ-листинге**: любой под, который RunPod возвращает
через `list_pods()` и которого нет в `pod_assignment`, виден админу
(фильтр применяется только для не-админа). В UI админ получает на него
кнопку «Назначить», которая делает UPSERT с `creation_source='external'`.

### Удаление endpoints hide/unhide

`/api/admin/pods/<pid>/hide` и `/unhide` **удаляются**. Их заменяет
`/assign` — для аналога прежнего «hide» админ вызывает `{"project": null,
"counts_toward_quota": false}`.

## UI

### Admin settings form

Добавляется секция «Квоты по проектам». 8 полей ввода (по одному на проект)
с числами. Сохранение — тот же `POST /api/admin/settings`, сервер принимает
целый JSON-объект `project_quotas` и валидирует (каждое значение 0-50).

### Create pod form

Переделывается:
- **Юзер (не-админ)**: как сейчас, без дропдауна. Проект берётся из сессии.
- **Админ**: появляется дропдаун «Назначить проекту» со списком из 9
  вариантов (8 проектов + «Не назначать»). Рядом — чекбокс «Считать в
  квоту» (по умолчанию выключен). Выключен «Не назначать» → чекбокс
  игнорируется.

Backend принимает `assigned_project` и `counts_toward_quota` только если
запрос от админа (иначе эти поля в body игнорируются). Это защищает от
спуфинга: обычный юзер не может создать под с чужим проектом.

### Pod card

Для каждой карточки добавляется индикация. **Бейджи** (крупные видимые
метки рядом с именем пода):

| Бейдж | Когда | Видно кому |
|-------|-------|-----------|
| `CV` / `DV` / ... (тег проекта) | `assigned_project NOT NULL` | Всем (юзер проекта видит свой тег, админ видит любой) |
| 🛡 `admin created` | `creation_source = 'admin'` | Всем (юзер видит «этот под создан админом», админ тоже) |
| 🌐 `external` | `creation_source = 'external'` | Только админу (у юзера такой под в принципе не виден, если только админ не назначил его проекту юзера — тогда юзер всё равно видит бейдж) |
| 👁 `unassigned` | `assigned_project IS NULL` | Только админу (юзеры такие поды не видят) |
| ∞ `не считается в квоту` | `counts_toward_quota = 0` | Только админу (мини-бейдж, юзеру этот факт не важен) |

**Комбинации допустимы**: один под может иметь сразу `CV` + `admin created`
+ `не считается в квоту` — если админ создал для проекта CV под «сверху
квоты».

Кнопка **«Назначить проекту»** (в карточке, только админу) — открывает
модальное окно с dropdown (8 проектов + «Не назначать») + чекбоксом
«Считать в квоту», POST-ит в `/assign`. Доступна для ЛЮБОГО пода в
админской листе, включая external (без существующей записи в
`pod_assignment`).

Существующая кнопка «Hide / Show» (👁) удаляется. Её функцию заменяет
пункт «Не назначать» в ассайн-диалоге.

### Activity log (admin)

Текущее поведение исправляется:

1. **Сортировка** — нужно гарантировать, что рендеринг в UI идёт в порядке
   `ts DESC` (свежее сверху). В SQL уже `ORDER BY ts DESC` через
   `idx_pa_ts`, но нужно проверить JS и не допустить обратной сортировки.
2. **Формат даты** — `DD.MM.YYYY HH:MM`, локальная таймзона пользователя.
   Сейчас показывается, вероятно, ISO или «today HH:MM»; заменяется на
   `Date(tsUTC).toLocaleString('ru-RU', {day:'2-digit', month:'2-digit',
   year:'numeric', hour:'2-digit', minute:'2-digit'})`.

## Migration (one-time, при первом старте нового кода)

Выполняется в `init_db()` после `CREATE TABLE IF NOT EXISTS pod_assignment`.

Шаги:

1. `SELECT EXISTS(...) FROM sqlite_master WHERE name='pod_hidden'` —
   проверяем наличие старой таблицы (если миграция уже прошла — скипаем).

2. `SELECT pod_id FROM pod_hidden` → для каждого INSERT в
   `pod_assignment(pod_id, NULL, 0, 'user', now_iso(), 'migration')`.
   Сохраняем прежнее поведение «только админ видит» через
   `assigned_project IS NULL`. `counts_toward_quota=0` т.к. админ-hidden
   поды и раньше не занимали user-квоту. `creation_source='user'` как
   безопасный дефолт — нельзя из старых данных надёжно отличить, создавал
   ли hidden-под сам админ через менеджер или изначально юзер; админ
   может вручную поправить через `/assign` (но source при UPSERT
   сохраняется, т.к. row уже существует — см. ниже).

3. `SELECT DISTINCT pod_id FROM pod_actions WHERE action='create'` → для
   каждого, которого нет в `pod_assignment` (т.е. он не был hidden):
   - Взять самый свежий `pod_actions` с `action='create'` для этого
     pod_id (ORDER BY ts DESC LIMIT 1).
   - Если `project` в `PROJECTS` → INSERT в `pod_assignment(pod_id,
     project, 1, 'user', now_iso(), nickname)`. `creation_source='user'`
     — в старых данных невозможно отличить admin от user (оба логируются
     одинаково), безопасный дефолт.
   - Если `project` не в `PROJECTS` (например, старые данные или опечатка)
     → пропустить (под получит `assigned_project=NULL` при чтении, т.е.
     станет admin-only; админ назначит вручную через `/assign`, при этом
     source будет вычислен как `'user'` т.к. `pod_actions.create`
     существует).

4. `DROP TABLE pod_hidden` — только после успешной обработки всех записей.
   Оборачиваем всё в транзакцию: если на шаге 2 или 3 упало — откат, без
   DROP.

5. Миграция settings: `load_settings()` на следующей загрузке видит, что в
   JSON нет `project_quotas` → бэкфиллит из нового `DEFAULT_SETTINGS`
   (`{"CV":4,"DV":4,...}`). Сохраняет через `save_settings()`.

6. Лог в stdout: `[MIGRATION] Migrated N pods from pod_hidden, M pods from
   pod_actions, initialized project_quotas with default=4`.

Edge case: пользователь добавил новый проект в `PROJECTS` после миграции
(код-изменение). Его квоту админ задаёт через UI; до этого — fallback на
`DEFAULT_PROJECT_QUOTA = 4` в `create_pod` и в UI «not set».

## API changes summary

| Endpoint | Старое | Новое |
|----------|--------|-------|
| `POST /api/pods` | body пустой | body `{assigned_project, counts_toward_quota}` (только от админа; у юзеров игнорируется) |
| `DELETE /api/pods/<pid>` | 403 если `is_pod_hidden(pid)` && не админ | 403 если `pod_assignment.assigned_project != session.project` && не админ |
| `POST /api/pods/<pid>/start` | аналогично | аналогично |
| `GET /api/pods` | filter hidden | filter by assigned_project для юзера, без filter для админа |
| `POST /api/admin/pods/<pid>/hide` | **REMOVED** | Использовать `/assign` с `{project: null, counts_toward_quota: false}` |
| `POST /api/admin/pods/<pid>/unhide` | **REMOVED** | Использовать `/assign` с нужным проектом |
| `POST /api/admin/pods/<pid>/assign` | **NEW** | Body `{project: string\|null, counts_toward_quota: bool}`. Работает для любого существующего пода, включая external (созданного в RunPod UI вне менеджера). При первом assign-е для unknown-пода `creation_source` вычисляется как `'external'` если нет `pod_actions.create`, иначе `'user'`. |
| `GET /api/admin/settings` | + `max_pods` | + `project_quotas` dict |
| `POST /api/admin/settings` | validates `max_pods` 1–50 | validates `project_quotas[*]` 0–50, игнорирует `max_pods` |

## Что не трогаем (выходит за scope)

- `auto_delete_enabled/time` — глобально, удаляет все running поды.
- `idle_timeout_enabled/minutes` — глобально.
- `pod_window_enabled/from/until` — глобально.
- Аутентификация: остаются раздельные user + admin сессии, cleartext пароль
  в `admin_settings.json`.
- `PRESET` с конфигом пода (GPU, image, ports) — не меняется.
- GraphQL deploy flow — без изменений.
- Bypass-логика для админа (window + quotas) — остаётся через
  `bypass_window=is_admin()`.

## Тестирование (компонентно)

1. **Миграция**: создать dev-БД с записями `pod_hidden` + `pod_actions`,
   запустить `init_db()`, проверить что `pod_assignment` заполнен
   правильно, `pod_hidden` удалена.
2. **Фильтрация**: залогиниться юзером CV → GET /api/pods не возвращает
   поды DV. Залогиниться админом → видит всё.
3. **Quota**: создать 4 пода в CV (квота 4) как юзер, попробовать 5-й →
   ошибка «Достигнут лимит CV: 4/4». Админ в той же сессии создаёт 5-й →
   успех.
4. **Assign**: админ POST `/assign {project:"CV", counts:true}` — юзер CV
   теперь видит этот под; POST `/assign {project:null}` — юзер CV не видит.
5. **counts_toward_quota=false**: админ создаёт 2 пода в CV с `counts=0`,
   юзер CV создаёт 4 → total 6 подов в CV, но квота 4/4 (только юзерские
   считаются). Админ-созданные имеют бейдж `admin created`.
6. **External pod**: через RunPod UI создать под вручную, в менеджере
   он появится ТОЛЬКО у админа. POST `/assign {project:"CV",
   counts:false}` → юзер CV видит его с бейджами `CV` + `external` (но
   не `admin created`). `creation_source` в БД = `'external'`.
7. **Source immutability**: после первого `/assign` сделать повторный
   `/assign` на том же поде — `creation_source` не меняется.
8. **Activity log**: проверить что сортировка DESC и формат
   `DD.MM.YYYY HH:MM` везде.
9. **Action='assign'** пишется при каждом `/assign`.

## Риски и ограничения

- **Concurrent manager instances**: если второй менеджер с тем же API key
  создаёт под в RunPod, у нас для него не появится записи в
  `pod_assignment` — под будет виден только админу (fallback: NULL).
  Это безопасный дефолт, но админу нужно будет вручную назначить проект.
  При assign-е такой под получит `creation_source='external'` если
  `pod_actions.create` в нашей БД не найдётся.
- **Race между creation и assignment INSERT**: между `create_pod_via_graphql`
  возвращающим pod_id и нашим INSERT в `pod_assignment` — окно 0.1-1 сек,
  в которое `list_pods()` вызванный параллельным запросом UI увидит под
  без записи в pod_assignment (→ admin-only). Через 1-2 рефреша
  консистентность восстанавливается. Пробуем минимизировать: INSERT
  делается сразу после успешного create_pod в той же транзакции, до
  возврата HTTP 200 клиенту.
- **Данные для юзера пропадают при переназначении**: админ переназначает
  под CV → DV, юзер CV теряет видимость «своего» пода. Это штатное
  поведение, но нужно чтобы action='assign' писался (юзер хотя бы из
  админ-activity поймёт что случилось, если дёрнет админа).
- **Существующая запись `admin_settings.json` имеет `max_pods=3`, а не
  4**: миграция настроек бэкфиллит `project_quotas` со всеми 4, игнорируя
  старое значение `max_pods=3`. Это сознательный выбор (user сказал
  «начальная квота 4»); старое значение остаётся в файле как мусор.
  При желании админ может руками прописать квоты = 3 под каждого после
  миграции через UI.
