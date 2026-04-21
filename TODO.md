# RunPod Manager — TODO для следующей сессии

## TODO #1: Stale activity protection ("залип `загружен · N в очереди`")

### Симптом
Карточка пода показывает `загружен · 2 в очереди` бесконечно, при этом:
- GPU 0% / VRAM 0% / CPU 0% (телеметрия пустая)
- `total_started − total_completed > 0` уже долгое время
- `last_event_at` старее нескольких десятков минут
- Реально под простаивает, но в UI висит как занятый
- Idle timer **не запускается** на сброс (потому что `is_busy = True`)
- Это блокирует автоматический idle-cleanup → деньги текут

### Корень проблемы
В `start.sh` (`E:\docker\собираю сам\мой файл версия для ранпод\start.sh`)
runtime watcher tail-парсит лог ComfyUI и инкрементирует:
- `total_started` на каждом `got prompt`
- `total_completed` на каждом `Prompt executed`

Активность определяется как `started > completed`. Если ComfyUI крашнул промпт
(OOM, ошибка ноды, дисконнект клиента, таймаут) — `Prompt executed` не выводится,
и `started − completed` остаётся положительным **навсегда**. Watcher уже имеет
orphan-защиту для самых первых событий (когда `started==completed==0`), но не
для последующих рассинхронов.

Подтверждение из логов inцидента 10.04.2026 (pod_2 от Naina):
```
27 × got prompt
29 × Prompt executed
1 × orphan ignored
```
Реально на UI: `88 done · 2 active`. Накопилось за прошлые сессии — два промпта
были потеряны без `Prompt executed`.

### План фикса — ТОЛЬКО start.sh, подход B (timeout safety net в watcher)

**ВАЖНО:** Решено по итогам обсуждения 11.04.2026 — фиксим **только в одном
месте**, в `start.sh`. Никаких правок в `runpod_manager.py` и в frontend.
Это решение явное и осознанное: один источник истины, никакой размазанной
логики между watcher и сервером.

Подход A (распознавать конкретные строки ошибок ComfyUI типа `Failed to
validate prompt`, `Exception during processing` и т.д.) был **отвергнут** —
нужно угадывать все возможные форматы, новые версии ComfyUI могут добавлять
новые, легко пропустить кейс.

Подход B — универсальный timeout: если `total_started > total_completed` И
прошло больше N секунд с `last_event_at`, то залипшие started считаем
потерянными и подтягиваем `total_completed = total_started`. Это закрывает
**любую** причину рассинхрона навсегда, не только те которые мы можем угадать.

**Файл:** `E:\docker\собираю сам\мой файл версия для ранпод\start.sh`
**Место:** runtime watcher loop, который tail-парсит лог ComfyUI и пишет
`/runtime.json`. Найти где этот файл генерируется (обычно функция типа
`write_runtime_json()` или прямой `cat > runtime.json` после обновления
счётчиков).

### Псевдокод фикса

Каждый раз перед записью `runtime.json` (или периодически в основном цикле
watcher, например каждые 30 секунд независимо от прихода событий):

```bash
STALE_THRESHOLD=1800  # 30 минут в секундах

# Если есть рассинхрон и last_event_at старее threshold — подтянуть completed
if [ "$total_started" -gt "$total_completed" ] && [ -n "$last_event_at" ]; then
    now_epoch=$(date -u +%s)
    # last_event_at в формате ISO 8601 UTC: 2026-04-11T13:42:02Z
    last_epoch=$(date -u -d "$last_event_at" +%s 2>/dev/null || echo 0)
    age=$((now_epoch - last_epoch))
    if [ "$age" -gt "$STALE_THRESHOLD" ]; then
        # Залипший промпт — подтянуть счётчик completed до started
        echo "[RUNTIME] stale active: started=$total_started completed=$total_completed age=${age}s — assuming lost, syncing" >&2
        total_completed=$total_started
        # 'active' пересчитается как (started > completed) → false автоматически
    fi
fi
```

Деталь: проверка должна работать **периодически**, не только при приходе
нового события. Иначе если ComfyUI после краша вообще ничего не логирует,
watcher никогда не пересчитает. Если основной цикл watcher уже tick-based
(условно `while read line` с таймаутом) — добавить проверку в timeout-ветку.
Если watcher event-driven через `tail -f` — нужен отдельный sleep-loop рядом
который раз в минуту перепроверяет и при необходимости перезаписывает
`runtime.json`.

### Что НЕ менять

- `runpod_manager.py` — никаких правок. Менеджер продолжает читать
  `runtime.json` как есть и доверять `active` оттуда
- `FRONTEND_HTML` — никаких новых тэгов, тултипов, иконок. Когда watcher
  починит счётчик, UI автоматически покажет `свободен` без всяких хаков
- Не плодить новые поля в `runtime.json` (никаких `runtimeStale` и т.п.)

### Тесты для проверки

1. **Реальный залипший pod**: подождать когда снова появится залипшее
   `загружен · N в очереди` (или симулировать через `kill -9` ComfyUI
   во время промпта). Через 30 минут после последнего события watcher
   должен сам подтянуть `completed = started` и тэг переключится на
   `свободен`. После этого idle timer пойдёт нормально и через
   `idle_timeout_minutes` под удалится автоматически
2. **Нормальный длинный промпт** (видели в логах `Prompt executed in 00:17:22`):
   17 минут < 30 минут threshold → не должен сработать. Pod должен
   остаться `загружен` всё время выполнения
3. **Edge case на границе**: промпт длительностью ~28 минут — должен
   успешно завершиться без ложного срабатывания. Если бывают регулярно
   воркфлоу длиннее 25 минут, threshold нужно поднять до 60 минут
   (отредактировать `STALE_THRESHOLD` в начале фикса)

### Действия пользователя после фикса

1. Прочитать обновлённый start.sh, убедиться что фикс на месте
2. Пересобрать ComfyUI Docker образ (`wikiniki/comfy_runpod:latest`):
   ```
   cd "E:\docker\собираю сам\мой файл версия для ранпод"
   docker build -t wikiniki/comfy_runpod:latest .
   docker push wikiniki/comfy_runpod:latest
   ```
3. Удалить и пересоздать существующие поды чтобы они подтянули новый образ
   (или дождаться когда RunPod сам обновит при следующем старте)
4. Менеджер пересобирать **не нужно** — он не менялся

### Контекст инцидента (10.04.2026)
- Пользователь: pod_2 от Naina (TV проект), GPU RTX PRO 4500
- Состояние на момент обнаружения: 88 done · 2 active, "загружен · 2 в очереди",
  GPU/VRAM/CPU = 0%, RAM 94%, idle 0s/480m, last event "today 21:32"
- Логи: `/mnt/user-data/uploads/logs__7_.txt` (если ещё доступны)
- Воркараунд: пересоздать pod (× → + New Pod). Это не решает корень.

### Связанный симптом: `Cannot write to closing transport` (обнаружено 11.04.2026)

В логах ComfyUI регулярно появляется ошибка `send error: Cannot write to
closing transport`. Это НЕ отдельный баг, а **один из триггеров** рассинхрона
счётчиков started/completed, который чинит TODO #1.

Механизм: ComfyUI использует WebSocket для отправки прогресса клиенту. Когда
пользователь закрывает вкладку / обновляет страницу во время выполнения
промпта, транспорт закрывается, но ComfyUI ещё пытается писать прогресс-
события. Каждая такая попытка → `Cannot write to closing transport`. В
некоторых случаях это приводит к тому что ComfyUI не выводит `Prompt
executed` в лог (broken pipe в середине), и наш watcher видит `got prompt`
без парного `Prompt executed` → залипший счётчик.

Также наблюдается побочный эффект: pod-ссылка "Open" может показывать
100% прогресса прошлого промпта и блокировать UI ComfyUI — это
упомянутое состояние решается F5 внутри самого ComfyUI или перезапуском
пода.

**Важно:** фикс TODO #1 (timeout safety в watcher) автоматически покрывает
этот симптом — через 30 минут после последнего события watcher подтянет
счётчик независимо от причины рассинхрона. Отдельных правок для
`Cannot write to closing transport` делать НЕ нужно — это upstream-поведение
ComfyUI которое мы не чиним, только отлавливаем последствия.

---

## Журнал предыдущих TODO/решений

### ✅ DONE: Hidden pods feature (v6.4)
Реализовано полностью с over-quota бейджем и тестами. См. саммари сессии.

### ✅ DONE: Сценарий B (admin bypass лимита)
Админ полностью обходит max_pods и pod_window restrictions.

### ✅ DONE: Quota semantics (4 итерации)
Финальная формула: `quotaUsed = min(visible_running, max_pods)`,
`overQuota = max(0, visible_running - max_pods)`. Учёт по visible (с учётом
hidden filtering для не-админа). Никакого `createdBy` в логике квоты.

### ✅ DONE: sbLogout/sbLogin race condition
`await refreshPods()` после изменения admin-сессии — без 15с стейл-окна.

### ✅ INVESTIGATED & CLOSED: Массовое удаление подов 09.04.2026
**Не баг.** Причина — забытый второй контейнер на старом ПК с
`auto_delete_time = 21:00 МСК`. Подтверждено через RunPod audit log
(скриншот в саммари сессии). Логика удаления в коде корректна,
46 функциональных тестов пройдены.

**Класс багов "конкурентные инстансы менеджера"** — при работе нескольких
RunPod Manager на разных ПК с одним `RUNPOD_API_KEY`, каждый видит и
управляет всеми подами на аккаунте. При диагностике странных удалений
**первым делом** проверять RunPod audit log
(https://www.runpod.io/console/user/audit-logs).
</content>