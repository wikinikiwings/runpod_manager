# Авторетрай запуска пода («заявка на под») — design spec

**Date:** 2026-06-08
**Status:** Draft, awaiting implementation
**Scope:** `runpod_manager.py` (backend + inline HTML/JS) + `DEFAULT_SETTINGS` + новая SQLite-таблица `pod_request`

## Проблема

Видеокарты нужного типа (`NVIDIA RTX PRO 4500 Blackwell`) периодически
недоступны. При нажатии «Create pod» в этот момент RunPod возвращает GraphQL-
ошибку:

> There are no longer any instances available with the requested specifications.
> Please refresh and try again.

Сейчас юзер видит этот пугающий текст и вынужден **вручную долбить кнопку**,
чтобы поймать момент, когда GPU освободится. Нужно автоматизировать: один раз
подтвердить «оставить заявку», а дальше менеджер сам повторяет запуск в фоне,
пока не получится (или не выйдет таймаут).

## Сценарий (happy path)

1. Юзер жмёт «Create pod».
2. GPU недоступна → вместо красной ошибки показываем диалог:
   «Кажется, в данный момент все видеокарты заняты. Оставить заявку на под?»
   [Отмена] [Оставить заявку].
3. По «Оставить заявку» создаётся **заявка** (`pod_request`), в списке подов
   появляется карточка-плейсхолдер: спиннер + подпись «подбираю свободную
   видеокарту, ожидайте» + кнопка «Отменить заявку».
4. Фоновый worker каждые `pod_request_retry_interval_seconds` (базово 15)
   пробует задеплоить под. Как только GPU освобождается — заявка превращается в
   реальный под, карточка-плейсхолдер заменяется обычной карточкой пода.
5. Если за `pod_request_timeout_minutes` (по умолчанию 15) подобрать не удалось
   — заявка переходит в `timed_out`, карточка показывает «Не удалось подобрать
   видеокарту за N мин» + кнопку «Закрыть».

## Решения, принятые при брейншторме

- **Триггер:** только при отказе «GPU недоступна» (не всегда-авто, не отдельная
  кнопка). Обычный «Create pod» работает как раньше — одна попытка.
- **Остановка:** таймаут (настраивается в админке, базовое 15 мин) + ручная
  отмена. Интервал ретрая тоже настраивается в админке (базово 15 сек) — две
  отдельные настройки: общее время ретрая и интервал между попытками.
- **Квота:** pending-заявка **занимает слот квоты сразу** (проверка квоты — один
  раз, при создании заявки).
- **Количество:** сколько угодно заявок в пределах квоты проекта.
- **Рестарт менеджера:** заявки **переживают** рестарт — хранятся в SQLite,
  worker после старта подхватывает `pending` и продолжает (таймаут считается от
  исходного `created_at`).
- **Архитектура:** DB-таблица + один фоновый worker-поток (подход A — в духе
  существующего `scheduler_loop`). Не поток-на-заявку, не frontend-polling.

## Модель данных

### Таблица `pod_request` (новая)

Создаётся в `init_db()` через `CREATE TABLE IF NOT EXISTS` внутри `executescript`.
Новая таблица — миграция не нужна.

```sql
CREATE TABLE IF NOT EXISTS pod_request (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pod_name            TEXT NOT NULL,                  -- зарезервированное имя, напр. cv_pod_3
  assigned_project    TEXT,                           -- проект (или NULL для admin-unassigned)
  counts_toward_quota INTEGER NOT NULL DEFAULT 1,     -- 0/1 boolean
  creation_source     TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'admin'
  requested_by        TEXT NOT NULL,                  -- ник
  status              TEXT NOT NULL DEFAULT 'pending', -- pending|fulfilled|failed|cancelled|timed_out
  created_at          TEXT NOT NULL,                  -- UTC ISO 8601 'Z'; от него считается дедлайн
  last_attempt_at     TEXT,                           -- UTC ISO последней попытки деплоя
  last_error          TEXT,                           -- текст последней ошибки
  pod_id              TEXT,                           -- проставляется при fulfilled
  finished_at         TEXT                            -- UTC ISO перехода в терминальный статус
);
CREATE INDEX IF NOT EXISTS idx_pr_status ON pod_request(status);
```

Статусы:
- `pending` — активна, worker её ретраит. Видна как карточка-плейсхолдер.
- `fulfilled` — под создан (`pod_id` заполнен). Терминальный; карточкой не
  показывается (под уже виден как реальный).
- `timed_out` — превышен таймаут. Терминальный; показывается красной карточкой
  до «Закрыть».
- `failed` — перманентная ошибка деплоя (auth/конфиг) или assignment-сбой.
  Терминальный; красная карточка до «Закрыть».
- `cancelled` — отменена юзером. Терминальный; карточкой не показывается.

«Активная заявка» = `status='pending'`.

## Backend

### 1. Детект «GPU недоступна»

- `is_gpu_unavailable_error(msg) -> bool` — case-insensitive поиск известных
  фраз: `"no longer any instances available"`,
  `"instances available with the requested"`, `"no resources"`.
- Новый класс `class GpuUnavailableError(RuntimeError)`.
- `create_pod_via_graphql`: если GraphQL вернул `errors` и они матчатся хелпером
  → `raise GpuUnavailableError(msg)` вместо обычного `RuntimeError`.
- `create_pod`: при `GpuUnavailableError` от GraphQL **не падаем в CLI-fallback**
  (CLI тоже падает `no resources` на Blackwell) — пробрасываем исключение.
  Прочие ошибки GraphQL → CLI-fallback как сейчас. Если CLI-ошибка матчит
  `is_gpu_unavailable_error` — тоже оборачиваем в `GpuUnavailableError`.
- `api_pods_post`: ловит `GpuUnavailableError` → отвечает
  `{"ok": False, "gpuUnavailable": True, "error": <msg>}` с **HTTP 200** (чтобы
  фронт обработал диалогом, а не красным тостом). Прочие исключения — как
  сейчас (500 + `error`).

### 2. Общий учёт квоты

Новый хелпер `project_quota_usage(project, pods=None) -> int`:
```
running-поды проекта с counts_toward_quota=1  +  pending-заявки проекта с counts_toward_quota=1
```
Используется в трёх местах:
- `api_pods_get` (бейдж `quotaUsed`/`projectRunning`);
- проверка квоты при создании заявки (`POST /api/pod-requests`);
- проверка квоты в `create_pod` (прямое создание тоже должно видеть заявки).

Инвариант: `pending-заявки + running-поды ≤ квоты`. Держится тем, что слот
резервируется при создании заявки; `fulfilled` лишь конвертирует заявку→под —
счётчик не растёт.

### 3. Резерв имени

`next_name` должен учитывать имена pending-заявок, иначе две заявки (или заявка
и под) получат одинаковое имя. Решение: при подсчёте передавать в `next_name`
объединённый список — реальные поды + синтетические `{'name': r['pod_name']}`
для всех pending-заявок.

### 4. Создание заявки — `POST /api/pod-requests` (`@require_user`)

Логика identity/admin-полей — точно как в `api_pods_post`:
- admin: `assigned_project` (валидируется против `PROJECTS`, может быть `null`),
  `counts_toward_quota` из body, `source='admin'`;
- не-admin: проект = свой, `counts_toward_quota=True`, `source='user'`.

Шаги:
1. Окно запрета (`check_pod_window`) — для не-admin. Закрыто → 400.
2. Квота через `project_quota_usage` (для не-admin). Перебор → 400 с тем же
   текстом, что в `create_pod` («Достигнут лимит …»).
3. Резерв имени через `next_name(pods + pending_names, ap)`.
4. INSERT `pod_request` со `status='pending'`, `created_at=now`.
5. `log_action(nick, proj, "request", name, "")`.
6. Ответ `{"ok": True, "request": {id, name, status, assignedProject}}`.

### 5. Фоновый worker — `pod_request_loop()`

Daemon-поток, запускается рядом со `scheduler_loop` при старте приложения.
Между тиками спит `pod_request_retry_interval_seconds` из настроек (базово 15),
перечитывая значение на каждой итерации (чтобы смена в админке подхватывалась
без рестарта). Защита от мусорных значений: clamp в разумный минимум (напр.
не меньше 5 сек).

Каждый тик:
1. Загрузить все `pod_request` со `status='pending'`.
2. Для каждой (последовательно — масштаб единицы заявок):
   - **Таймаут:** если `now - created_at >= pod_request_timeout_minutes` →
     `status='timed_out'`, `finished_at=now`,
     `log_action("REQUEST_TIMEOUT", "[SYSTEM]", "request timeout", pod_name, "")`.
     Деплой не пробуем.
   - Иначе **попытка деплоя** `create_pod_via_graphql(pod_name)` напрямую
     (квота/окно уже зарезервированы при создании заявки):
     - ✅ Успех → **перечитать статус заявки** (могла быть отменена во время
       деплоя — см. edge case «гонка отмены»). Если уже `cancelled` →
       `delete_pod(pid)` и заявку не трогаем. Иначе:
       `upsert_pod_assignment(pid, assigned_project, counts_toward_quota,
       creation_source, requested_by)`; заявка `status='fulfilled'`,
       `pod_id=pid`, `finished_at=now`;
       `log_action(requested_by, assigned_project, "create", pod_name, pid)`
       (попадёт в Activity как обычное создание).
     - `GpuUnavailableError` → GPU всё ещё нет: `last_attempt_at=now`,
       `last_error=<msg>`, остаётся `pending`.
     - Прочее исключение → перманентная ошибка: `status='failed'`,
       `last_error=<msg>`, `finished_at=now`. **Не ретраим перманент.**
     - Под-случай: успех деплоя, но `upsert_pod_assignment` упал →
       `status='failed'`, `pod_id=pid`, `last_error="под создан (id=<pid>), но
       assignment не записан — admin /assign"`. Зеркалит `api_pods_post`.

Worker оборачивает каждую заявку в try/except, чтобы сбой одной не валил тик.

### 6. Листинг — `api_pods_get`

- В JSON добавить `requests: [...]` — заявки, видимые зрителю (юзер — только
  свой проект; admin — все), в статусах `pending|timed_out|failed`. Поля:
  `{id, name, assignedProject, status, lastError, createdAt}`.
- Отдельный массив (а не подмешивание в `pods`), чтобы не ломать допущения
  `render()` о полях реального пода.
- Бейдж квоты `quotaUsed`/`projectRunning` считается через
  `project_quota_usage` (running-поды + pending-заявки).

### 7. Отмена/закрытие — `DELETE /api/pod-requests/<id>` (`@require_user`)

- Project-scoped доступ как в `api_del`: не-admin видит/трогает только заявки
  своего проекта, иначе 404 (приватность).
- `pending` → `status='cancelled'`, `finished_at=now`,
  `log_action(nick, proj, "request_cancel", pod_name, "")` (сохраняем строку для
  аудита).
- `timed_out`/`failed` (кнопка «Закрыть») → физически удаляем строку
  (`DELETE FROM pod_request WHERE id=?`) — исход уже зафиксирован в логе при
  переходе в терминальный статус, карточку просто убираем.

### 8. Админ-настройка

- В `DEFAULT_SETTINGS` добавить:
  - `"pod_request_timeout_minutes": 15` — общее время ретрая (после него заявка
    `timed_out`);
  - `"pod_request_retry_interval_seconds": 15` — интервал между попытками.
- В админ-панель — два числовых поля: «Таймаут заявки на под (мин)» и «Интервал
  ретрая (сек)», оба проводятся через `sbSave` так же, как `idle_timeout_minutes`.

## Frontend (inline HTML/JS)

### `createPod()`
При ответе с `gpuUnavailable: true` — вместо красного тоста показать диалог
(`showDlg`): «Кажется, в данный момент все видеокарты заняты. Оставить заявку на
под?» [Отмена] [Оставить заявку]. По подтверждению → `POST /api/pod-requests`
(тем же body, что `createPod` собирает для admin) → тост «Заявка создана» →
`refreshPods()`.

### `render()`
Рендерить `r.requests` как отдельные карточки:
- `pending`: спиннер (`.sp`) + имя + «подбираю свободную видеокарту, ожидайте» +
  кнопка «Отменить заявку» → `DELETE /api/pod-requests/<id>`.
- `timed_out`/`failed`: красная пометка («Не удалось подобрать видеокарту за N
  мин» или `lastError`) + кнопка «Закрыть» → тот же `DELETE`.

Авторефреш каждые 15с уже существует (`refreshPods` на `setInterval`) — карточки
заявок оживают сами, отдельного поллинга не нужно.

## Edge cases

- **Две заявки fulfilled в одном тике** — счётчик квоты не растёт (конвертация
  заявка→под), инвариант держится.
- **Рестарт** — worker берёт `pending` из DB; таймаут от `created_at` учитывает
  даунтайм автоматически.
- **Окно запрета** — проверяется только при создании заявки, на ретраях нет
  (заявка создана, когда окно было открыто).
- **Гонка отмены и fulfilled** — worker и отмена пишут `status` в одной SQLite-
  БД; UPDATE-ы атомарны на уровне строки. Worker перед фиксацией `fulfilled`
  перечитывает статус заявки и, если она уже `cancelled`, **удаляет только что
  созданный под** (`delete_pod`), чтобы не оставить осиротевший под. Это
  редкий, но реальный кейс — обрабатываем явно.

## Тестирование (`tests/`, в духе `test_migration.py`)

- `is_gpu_unavailable_error` — матчит известные фразы, отвергает посторонние.
- `project_quota_usage` — учитывает pending-заявки.
- `next_name` — учитывает имена pending-заявок (нет коллизий).
- Worker-тик (мок `create_pod_via_graphql`): `pending`→`fulfilled` при успехе;
  остаётся `pending` при `GpuUnavailableError`; `failed` при прочей ошибке;
  `timed_out` при просрочке (мок текущего времени).
- Создание заявки уважает квоту (отказ при заполнении).
- Отмена `pending`-заявки → `cancelled`; гонка cancel+fulfilled → под удаляется.

Реальный деплой не тестируем (стоит денег). Ручные шаги проверки — отдельным
разделом при имплементации (можно временно подставить заведомо недоступный
`gpu_id`, чтобы спровоцировать `GpuUnavailableError` и увидеть карточку-заявку,
затем вернуть рабочий тип и убедиться, что заявка `fulfilled`).
