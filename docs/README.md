# RunPod Manager — документация (as-is)

Снимок проекта на момент `2026-06-08`, версия `v6.6`.
Цель этой папки — дать достаточно контекста, чтобы восстановить и починить
проект с нуля, даже если потерян код, БД или настройки.

## Что это такое

Веб-админка для управления GPU-подами на [RunPod.io](https://www.runpod.io):
- Одна Flask-страница (SPA), раздаётся на порту `5001`.
- Пользователи регистрируются (ник + проект из whitelist), запускают и удаляют
  свои ComfyUI-поды.
- Админ управляет всеми подами, настройками авто-удаления, окнами запуска и
  назначает поды проектам (юзер видит только поды своего проекта).
- Запуск подов идёт через **GraphQL-мутацию `DeployOnDemand`** — ту же, что
  использует веб-UI RunPod (потому что `runpodctl` ломается на новых GPU вроде
  Blackwell RTX PRO 4500).
- Если все видеокарты заняты — пользователь оставляет **заявку на под**, и
  фоновый воркер сам повторяет запуск, пока карта не освободится (авторетрай).
- Один файл — `runpod_manager.py` (~3290 строк), одна БД — `runpod_manager.db`
  (SQLite), одни настройки — `admin_settings.json`.

## Структура репозитория

```
runpod_manager/
├─ runpod_manager.py      # весь backend + inline HTML SPA frontend
├─ Dockerfile             # python:3.12-slim + runpodctl + flask
├─ docker-compose.yml     # сервис, volume /app/data, port 5001
├─ .env                   # RUNPOD_API_KEY=... (gitignored)
├─ admin_settings.json    # runtime state: пароль, лимиты, расписания (gitignored)
├─ runpod_manager.db      # SQLite, 5 таблиц (gitignored)
├─ test_graphql.py        # диагностика: проверка GraphQL-листинга подов
├─ test_deploy.py         # диагностика: ручной вызов DeployOnDemand
├─ TODO.md                # текущие задачи между сессиями
└─ docs/                  # эта папка
```

## Документы

| Файл | О чём |
|------|-------|
| [architecture.md](architecture.md) | Разбивка `runpod_manager.py` по секциям, глобальные константы, `PRESET` |
| [graphql-deploy.md](graphql-deploy.md) | **Самый важный.** Полная схема `DeployOnDemand`-мутации, переменные, headers, fallback на `runpodctl` |
| [pod-lifecycle.md](pod-lifecycle.md) | Create → boot (порт 8189) → ready (порт 8188) → idle → delete, все health-check endpoints |
| [pod-images.md](pod-images.md) | Два варианта образа для подов (**baked** ~73с vs **s3-loading** ~13мин), их компромиссы, контракт портов с менеджером, расхождения имён образов и незакрытый TODO #1 в baked |
| [admin-panel.md](admin-panel.md) | Аутентификация (user + admin), все `/api/admin/*`, настройки, per-project quotas, pod window, scheduler |
| [database.md](database.md) | DDL всех 5 таблиц SQLite, где каждая пишется/читается |
| [deployment.md](deployment.md) | Запуск через `docker compose`, env vars, volumes, откуда читается API key |
| [recovery.md](recovery.md) | Плейбук на случай поломки: потерял БД / ключ / настройки / не стартует / списывает деньги |
| [ui-conventions.md](ui-conventions.md) | Соглашения по inline-фронтенду (`FRONTEND_HTML`): темизация полей ввода (всегда указывать `type`), структура `.fr`/`label`/`.toggle`, экранирование ввода |

## Быстрый старт (если уже есть `.env` с ключом)

```bash
cd E:\my_stable\runpod_manager
docker compose up -d --build
# UI: http://localhost:5001
```

Дефолтный админ-пароль — `admin` (поле `admin_password` в `admin_settings.json`,
cleartext; меняется через UI в admin-панели).

## Критичные внешние зависимости

- **RunPod аккаунт** — привязан к `RUNPOD_API_KEY` в `.env`. Запасной источник
  ключа — `~/.runpod/config.toml` в домашней папке пользователя на хосте (если
  логинились через `runpodctl`).
- **Network volume** `0czgom7b1j` в дата-центре `EU-RO-1` — смонтирован в
  `/workspace` каждого пода. Если удалить — поды не смогут стартовать.
- **RunPod template** `i3j2sm66q8` с образом `wikiniki/comfy_runpod:latest`.
  Образ содержит `start.sh`, который запускает ComfyUI на 8188 и HTTP-сервер
  статуса на 8189 (этот сервер источник `/status.json` и `/runtime.json`,
  от которых зависит половина функционала менеджера).
  > ⚠️ Имя `wikiniki/comfy_runpod:latest` **не совпадает** с реально собираемыми
  > образами (`comfy_gpu_baked` / `comfy_gpu`) — фактический образ задаётся
  > шаблоном `i3j2sm66q8` на стороне RunPod. Подробнее и про выбор baked vs
  > s3-loading — [pod-images.md](pod-images.md).
- **`runpodctl` CLI** — скачивается в Dockerfile при билде, используется только
  для `pod delete` / `pod start` и как fallback для `pod create`.

Все эти значения зафиксированы в `PRESET` в `runpod_manager.py` (секция globals). При
смене RunPod-аккаунта нужно править `PRESET` или подтянуть значения нового
аккаунта.
