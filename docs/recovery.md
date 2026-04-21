# Playbook восстановления и починки

Пошаговые процедуры для типовых аварий. Перед любым действием: `docker compose logs -f runpod-manager` — почти всегда там уже написано что сломалось.

## Быстрая диагностика

```bash
docker compose ps                                           # Контейнер жив?
docker compose logs --tail=100 runpod-manager               # Что в логах?
curl http://localhost:5001/api/projects                     # Отвечает Flask?
docker compose exec runpod-manager runpodctl pod list       # API key валидный?
```

Если первые две команды говорят что контейнер упал — **[Сценарий 1](#сценарий-1--контейнер-не-стартует)**.
Если Flask отвечает, но поды не создаются — **[Сценарий 4](#сценарий-4--не-создаются-поды)**.
Если поды исчезают сами — **[Сценарий 6](#сценарий-6--поды-удаляются-сами)**.

---

## Сценарий 1 — контейнер не стартует

**Симптомы**: `docker compose up` завершается или контейнер в статусе
`Restarting` / `Exited`.

### Проверить логи последнего запуска
```bash
docker compose logs runpod-manager | tail -50
```

Типовые ошибки:

| Сообщение | Причина | Фикс |
|-----------|---------|------|
| `ModuleNotFoundError: No module named 'flask'` | Слом билда | `docker compose build --no-cache` |
| `sqlite3.OperationalError: unable to open database file` | `/app/data` недоступен | См. ниже — volume |
| `Address already in use` | 5001 занят другим процессом | `netstat -ano \| findstr :5001` и убить; или сменить порт в `docker-compose.yml` |
| `RUNPOD_API_KEY=` в env пустой | `.env` не найден или пустой | Восстановить `.env` с ключом (см. [сценарий 2](#сценарий-2--потерян-api-key)) |

### Проверить volume
```bash
docker volume ls | grep runpod-data
docker volume inspect runpod_manager_runpod-data
```

Если volume внезапно исчез (например, `docker compose down -v` по ошибке) —
БД и настройки потеряны, см. [сценарий 5](#сценарий-5--потерян-volume-с-бд-и-настройками).
Контейнер при этом **будет стартовать** — создаст новые таблицы с нуля.

### Полный rebuild
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

---

## Сценарий 2 — потерян API key

**Симптомы**: в логах `GraphQL deploy unavailable: no API key configured`
или 401 при вызовах RunPod.

### Где взять новый
1. Зайти на https://www.runpod.io/console/user/settings
2. Создать новый API key (или использовать существующий).
3. Прописать в `.env`:
   ```
   RUNPOD_API_KEY=rpa_XXXXXXXXXXXXXXXX
   ```
4. Рестарт: `docker compose up -d` (подхватит новое значение).

### Проверка валидности ключа вручную
```bash
docker compose exec runpod-manager python3 /app/test_graphql.py
```
(Если `test_graphql.py` не лежит в образе — `docker compose cp test_graphql.py runpod-manager:/app/test_graphql.py` сначала.)

Должен вывести `pods: list of N pods` в Test C1. Если ошибка 401/403 — ключ
действительно невалидный.

### Альтернатива — взять ключ из локального runpodctl

На хосте (если когда-то логинились через CLI):
```
type "C:\Users\admin_korneev\.runpod\config.toml"
```
Там поле `api_key = "..."`. См. локальную заметку `где хранится ключ.txt`.

---

## Сценарий 3 — забыт админ-пароль

**Симптомы**: `/api/admin/login` возвращает 401 на любой введённый пароль.

### Вариант A — сбросить через файл

```bash
docker compose exec runpod-manager cat /app/data/admin_settings.json
```

Видите `"admin_password": "..."` в открытом виде. Чтобы сменить:

```bash
# На хосте редактируем локальный файл
# На Windows подойдёт любой редактор
docker compose cp runpod-manager:/app/data/admin_settings.json .
# Отредактировать admin_password в JSON
docker compose cp admin_settings.json runpod-manager:/app/data/admin_settings.json
docker compose restart runpod-manager
```

### Вариант B — прямо внутри контейнера
```bash
docker compose exec runpod-manager python3 -c "
import json
with open('/app/data/admin_settings.json') as f: s = json.load(f)
s['admin_password'] = 'newpassword'
with open('/app/data/admin_settings.json','w') as f: json.dump(s, f, indent=2)
print('done')"
docker compose restart runpod-manager
```

---

## Сценарий 4 — не создаются поды

**Симптомы**: кнопка «создать pod» в UI ничего не делает или в toast
ошибка.

### Ошибка говорит «лимит подов»
Обычный пользователь упёрся в `max_pods`. Залогиниться админом → увеличить
в настройках или удалить чужие поды.

### Ошибка говорит «Запуск подов ограничен»
Сработало `pod_window`. Проверить текущее UTC-время и настройки окна. Админ
обходит окно, так что можно залогиниться админом и создать.

### Ошибка говорит «no resources» (особенно для RTX PRO 4500 Blackwell)
GraphQL отдал `no resources available`. Варианты:
1. **У RunPod реально нет свободных GPU этого типа в `EU-RO-1`** — подождать,
   или поменять `gpu_id` / `data_center_id` в `PRESET` (`runpod_manager.py:47-66`).
   Помнить: `data_center_id` связан с `network_volume_id` — нельзя менять
   одно без другого.
2. **CLI-fallback тоже падает** — это нормальный симптом, GraphQL и CLI
   ходят в разные backend-ы. Если GraphQL говорит «no resources», CLI
   скорее всего скажет то же. Решение выше — сменить GPU/DC или ждать.

### Ошибка «GraphQL HTTP 400» или «empty response»
RunPod мог изменить GraphQL-схему. Выполнить диагностику:
```bash
docker compose cp test_deploy.py runpod-manager:/tmp/test_deploy.py
docker compose exec runpod-manager python3 /tmp/test_deploy.py
```

Если скрипт тоже падает с той же ошибкой — значит проблема в запросе, не в
менеджере. Дальше — по [graphql-deploy.md, раздел «Что делать, если мутация перестала работать»](graphql-deploy.md#что-делать-если-мутация-перестала-работать).

### Ошибка «Cloudflare error code 1010»
Слетел User-Agent. Убедиться что в `create_pod_via_graphql()` остался
`"User-Agent": "RunPod-Manager/6.0"` (runpod_manager.py:993).

---

## Сценарий 5 — потерян volume с БД и настройками

**Симптомы**: после `docker compose down -v` или миграции на другую машину
БД и `admin_settings.json` пустые.

### Что продолжает работать без БД
- Создание / удаление подов через UI (идентификация через API key, а не БД).
- Health-check'и (данные in-memory).
- Админ-панель (но с паролем `admin` по умолчанию).

### Что потеряно
- Аудит-лог (`pod_actions`).
- Idle-timers — пересоздадутся при следующем `list_pods()` когда поды будут ready.
- Hidden-отметки — все поды стали видимы всем.
- Настройки админки (лимиты, расписания, пароль) — вернулись к `DEFAULT_SETTINGS`.

### Восстановление из бэкапа (если был)
```bash
docker compose cp backup.db runpod-manager:/app/data/runpod_manager.db
docker compose cp backup-settings.json runpod-manager:/app/data/admin_settings.json
docker compose restart runpod-manager
```

### Если бэкапа нет
Просто зайти в админ-панель (пароль `admin`) и настроить заново: max_pods,
auto_delete, idle_timeout, pod_window, сменить пароль. Аудит-лог начнёт
писаться с нуля.

---

## Сценарий 6 — поды удаляются сами

**Симптомы**: поды исчезают без действий пользователя.

### Проверка 1 — планировщик auto_delete
```bash
docker compose exec runpod-manager cat /app/data/admin_settings.json | grep auto_delete
```

Если `"auto_delete_enabled": true` и время совпадает с моментом исчезновения —
это штатное поведение. В `admin_settings.json` есть `auto_delete_last_log`
с фактом выполнения.

### Проверка 2 — idle timeout
В UI открыть карточку пода с «i» → вкладка tech: есть `Idle for / Auto-delete in`.
Если `idle_timeout_minutes` слишком низкий (например, 5 минут) — под с
запущенным ComfyUI, но без активных промптов, удалится.

### Проверка 3 — конкурентные инстансы менеджера

**Это частая причина**, см. TODO.md инцидент 09.04.2026. Если где-то ещё
запущен второй RunPod Manager с тем же `RUNPOD_API_KEY`, он **видит те же
поды** и удаляет их по своему расписанию. Симптомы:
- В нашем `pod_actions` нет записи об удалении.
- При этом поды точно удалились с аккаунта.

**Лечение**:
1. Открыть https://www.runpod.io/console/user/audit-logs — там видно, с какого
   IP пришёл delete-запрос.
2. Найти этот инстанс, выключить (`docker compose down`), а лучше удалить
   либо сменить API key.

### Проверка 4 — залипший `active` (stale prompt)
Под показывает `загружен · N в очереди` → idle-timer не тикает → НЕ удаляется.
Это **обратная** проблема (деньги текут, но поды не удаляются). Подробности
и план фикса — в `TODO.md` (TODO #1).

---

## Сценарий 7 — висит «зависший» под с `active > 0` навсегда

**Симптом**: `загружен · 2 в очереди` в UI, при этом `GPU 0% / VRAM 0% / CPU 0%`,
`last_event_at` старее получаса. Idle-timer не работает, деньги текут.

**Причина**: расхождение счётчиков `total_started` и `total_completed` в
`runtime.json` — ComfyUI крашнул промпт без `Prompt executed` в логе (OOM,
ошибка ноды, WebSocket disconnect во время промпта).

**Временный workaround**: ручное удаление пода через кнопку ✕ в UI.

**Нормальный фикс**: требует правки `start.sh` в Docker-образе ComfyUI
(`wikiniki/comfy_runpod:latest`), не в этом репо. Детальный план — в
`TODO.md` (TODO #1). Подход B: в watcher добавить timeout 30 минут без
событий → `total_completed = total_started`.

Менеджер **ничего не делает** с этой ситуацией; он просто читает `runtime.json`
как источник истины. Правка должна быть в месте, где этот файл пишется.

---

## Сценарий 8 — поменяли RunPod-аккаунт или GPU-тип

Нужно синхронизированно обновить `PRESET` (`runpod_manager.py:47-66`):

1. `gpu_id` — точное имя GPU-типа (как показывает `runpodctl get cloud` или
   RunPod UI при выборе GPU).
2. `min_memory_in_gb` + `min_vcpu_count` — характеристики выбранного GPU
   (обычно написаны на странице GPU в RunPod UI).
3. `network_volume_id` — ID volume нового аккаунта.
4. `data_center_id` — **обязательно** тот же DC что у volume.
5. `template_id` + `image` — template нового аккаунта, обычно свой
   приватный.

После правки — `docker compose up -d --build` (нужен rebuild, `.py` вшит в образ).

Проверить что GraphQL принимает новый набор полей:
```bash
docker compose cp test_deploy.py runpod-manager:/tmp/test_deploy.py
# В test_deploy.py заменить VARIABLES на новый конфиг
docker compose exec runpod-manager python3 /tmp/test_deploy.py
```

Если деплой прошёл — **удалить** тест-под командой, которую выведет скрипт.

---

## Сценарий 9 — нужно массово удалить все поды прямо сейчас

**Через UI**: админ-панель → кнопка «Удалить все поды сейчас».

**Через CLI (если UI недоступен)**:
```bash
docker compose exec runpod-manager runpodctl pod list
# скопировать все pod IDs
docker compose exec runpod-manager runpodctl pod delete <id1> <id2> ...
```

**Через RunPod UI**: https://www.runpod.io/console/pods → выделить → Terminate.
Это работает всегда, даже если наш менеджер сломан полностью.

---

## Диагностические команды на шпаргалку

```bash
# Статус
docker compose ps
docker compose logs -f runpod-manager

# Зайти внутрь
docker compose exec runpod-manager bash

# Состояние БД
docker compose exec runpod-manager sqlite3 /app/data/runpod_manager.db \
  "SELECT * FROM pod_actions ORDER BY ts DESC LIMIT 20;"

# Текущие настройки
docker compose exec runpod-manager cat /app/data/admin_settings.json

# Проверка API key
docker compose exec runpod-manager bash -c 'echo $RUNPOD_API_KEY | head -c 10'
docker compose exec runpod-manager runpodctl pod list

# Проверка GraphQL deploy
docker compose cp test_deploy.py runpod-manager:/tmp/test_deploy.py
docker compose exec runpod-manager python3 /tmp/test_deploy.py

# Volume-inspect
docker volume inspect runpod_manager_runpod-data

# Жёсткий reset (УНИЧТОЖИТ ВОЛЮМ!)
docker compose down -v
docker compose up -d --build
```

## Контакты и ссылки

- RunPod console: https://www.runpod.io/console/pods
- Audit log (важнейший для диагностики мистических удалений):
  https://www.runpod.io/console/user/audit-logs
- API settings: https://www.runpod.io/console/user/settings
- `runpodctl` releases: https://github.com/runpod/runpodctl/releases
