# GraphQL deploy — основной путь создания пода

Это **самый важный документ** в этой папке. Если что-то сломается при запуске
подов, смотрите сюда. Здесь мутация, её переменные, headers, URL — всё что
нужно, чтобы воспроизвести запрос вручную или восстановить логику с нуля.

## Почему GraphQL, а не `runpodctl`

Прямая цитата из кода (`runpod_manager.py:927–944`):

> We discovered through F12 inspection that the RunPod web UI creates pods via
> a GraphQL mutation `DeployOnDemand` on `https://api.runpod.io/graphql`.
> This is a separate code path from `runpodctl pod create`, and importantly
> RunPod's CLI has been observed to fail with `no resources` errors for GPU
> types that the GraphQL endpoint accepts without complaint (notably newer
> Blackwell-class GPUs like RTX PRO 4500). The two paths likely talk to
> different backend services on RunPod's side.

Поэтому **приоритет**: если задан `_api_key`, сначала пробуем GraphQL, и только
если он падает с исключением — падаем через на CLI как safety-net.

Решение принято после ручной диагностики (скрипты `test_graphql.py` и
`test_deploy.py` в корне репо — их можно запускать для sanity-check:
`docker compose exec runpod-manager python3 /app/test_deploy.py`).

## Мутация `DEPLOY_MUTATION` (runpod_manager.py:945–951)

```graphql
mutation DeployOnDemand($input: PodFindAndDeployOnDemandInput) {
  podFindAndDeployOnDemand(input: $input) {
    id
    imageName
    machineId
  }
}
```

## Переменные `input`

Собираются из `PRESET` в `create_pod_via_graphql()` (runpod_manager.py:960–978).
**Имена полей — camelCase**, строго как ждёт GraphQL-схема RunPod:

| Поле GraphQL | Источник в PRESET | Текущее значение |
|--------------|-------------------|-----------------|
| `cloudType` | `cloud_type` | `"SECURE"` |
| `containerDiskInGb` | `container_disk_in_gb` | `20` |
| `dataCenterId` | `data_center_id` | `"EU-RO-1"` |
| `globalNetwork` | `global_network` | `False` |
| `gpuCount` | `gpu_count` | `1` |
| `gpuTypeId` | `gpu_id` | `"NVIDIA RTX PRO 4500 Blackwell"` |
| `minMemoryInGb` | `min_memory_in_gb` | `62` |
| `minVcpuCount` | `min_vcpu_count` | `28` |
| `name` | — (аргумент функции) | `"pod_N"` где N — следующий свободный |
| `networkVolumeId` | `network_volume_id` | `"0czgom7b1j"` |
| `ports` | `ports` | `"8188/http,8888/http,8686/http,8189/http"` |
| `startJupyter` | `start_jupyter` | `True` |
| `startSsh` | `start_ssh` | `True` |
| `templateId` | `template_id` | `"i3j2sm66q8"` |
| `volumeInGb` | `volume_in_gb` | `0` |
| `volumeKey` | — (литерал) | `None` |

**Не забыть**: `templateId` уже содержит `image` и `env` — они НЕ передаются
отдельно в GraphQL, в отличие от CLI-пути.

## Запрос целиком

`URL` (runpod_manager.py:989):
```
POST https://api.runpod.io/graphql?operation=DeployOnDemand
```

Query-параметр `?operation=DeployOnDemand` — зеркалит UI-запрос. Не обязателен
по GraphQL-спеке, но edge-роутер RunPod может его использовать.

`Headers` (runpod_manager.py:990–994):
```
Content-Type: application/json
Authorization: Bearer <RUNPOD_API_KEY>
User-Agent: RunPod-Manager/6.0
```

**Про User-Agent (важно)**: Cloudflare перед API RunPod **блокирует** запросы
с дефолтным Python urllib UA (возвращает `error code: 1010`). Нужен любой
осмысленный UA. Этот же UA используется во всех других GraphQL-вызовах
(`try_gql_bearer`, listing и т.д.).

`Body` (runpod_manager.py:981–985):
```json
{
  "operationName": "DeployOnDemand",
  "query": "<DEPLOY_MUTATION>",
  "variables": {"input": {...см. выше...}}
}
```

`Timeout`: **60 секунд** (runpod_manager.py:1000) — deploy может быть медленным.

## Обработка ответа

HTTP-ошибки (runpod_manager.py:1002–1012):
- `HTTPError` → читаем body, кидаем `RuntimeError("GraphQL HTTP <code>: <body>")`
- `URLError` (сеть) → `RuntimeError("GraphQL network error: ...")`
- любое другое исключение → `RuntimeError("GraphQL request failed: ...")`

GraphQL-ошибки приходят **внутри тела HTTP 200** (типовой косяк GraphQL).
Проверка (runpod_manager.py:1019–1027):
```python
if isinstance(data, dict) and data.get("errors"):
    msgs = [err.get("message", str(err)) for err in data["errors"]]
    raise RuntimeError("GraphQL: " + "; ".join(msgs)[:300])
```

Успешный ответ (runpod_manager.py:1029–1038):
```python
pod = data["data"]["podFindAndDeployOnDemand"]
# pod = {"id": "...", "imageName": "wikiniki/comfy_runpod:latest", "machineId": "..."}
return {"id": pod["id"], "name": name, "imageName": ..., "machineId": ...}
```

## Fallback на CLI (runpod_manager.py:1081–1118)

Если `create_pod_via_graphql()` кинул любое исключение, `create_pod()` логирует
warning и идёт на CLI:

**Новая CLI (`_cli_is_new = True`)**:
```bash
runpodctl pod create \
  --cloud-type SECURE \
  --gpu-id "NVIDIA RTX PRO 4500 Blackwell" --gpu-count 1 \
  --name pod_N \
  --image wikiniki/comfy_runpod:latest \
  --container-disk-in-gb 20 \
  --volume-mount-path /workspace --volume-in-gb 0 \
  --template-id i3j2sm66q8 \
  --network-volume-id 0czgom7b1j \
  --env '{"COMFY_API_KEY": "{{ RUNPOD_SECRET_comfyui_api_partners_secret }}"}'
```

**Старая CLI (`_cli_is_new = False`)** — другой CLI, другие флаги
(`--secureCloud`, `--gpuType`, `--imageName`, `--networkVolumeId`, per-key env).

CLI-путь **не проверяет** `min_memory_in_gb` / `min_vcpu_count` / `data_center_id`
/ `ports` — потому что эти поля у старого CLI либо отсутствуют, либо
подтягиваются из template-а. Поэтому CLI-fallback ненадёжнее GraphQL-пути для
новых GPU и fulfilment может упасть в `no resources`.

## Diagnostic scripts

Два скрипта в корне репо полезны, когда GraphQL-путь сломан:

### `test_graphql.py` — листинг подов

```bash
docker compose cp test_graphql.py runpod-manager:/tmp/test_graphql.py
docker compose exec runpod-manager python3 /tmp/test_graphql.py
```

Прогоняет три варианта:
- Bearer auth + manager UA
- Bearer auth + browser UA (на случай, если RunPod пропускает только «браузерные»)
- Через `runpod_manager.try_gql_bearer()` напрямую (end-to-end проверка
  собственных функций менеджера).

Выводит HTTP-коды и первые 500 байт тела — достаточно, чтобы понять, где ломается.

### `test_deploy.py` — реальный deploy

```bash
docker compose cp test_deploy.py runpod-manager:/tmp/test_deploy.py
docker compose exec runpod-manager python3 /tmp/test_deploy.py
```

Делает ОДИН реальный `DeployOnDemand` с именем `test_diag_graphql`. Если
проходит — сразу печатает `docker compose exec runpod-manager runpodctl pod delete <id>`,
которую **нужно запустить вручную**, иначе под останется крутиться и списывать
деньги.

## Что делать, если мутация перестала работать

1. **Проверить API key** — зайти в https://www.runpod.io/console/user/settings,
   убедиться что ключ жив, и в `.env` именно он.
2. **Повторить UI-запрос руками** — открыть https://www.runpod.io/console/deploy,
   задеплоить под через UI с нужными параметрами, поймать запрос в F12 →
   Network → Fetch/XHR → найти `operation=DeployOnDemand` → сравнить `variables`
   с нашими. Если у UI появились новые обязательные поля — добавить в PRESET
   и в `create_pod_via_graphql()`.
3. **Проверить GraphQL-схему** — в F12 видно `introspection` запросы на
   старте. Если поле переименовали (было `dataCenterId` → стало `dcId` и т.п.),
   править в обеих местах: мутация использует имена схемы, и variables — тоже.
4. **Прогнать `test_deploy.py`** — изолированно, без бэкенда, чтобы исключить
   баги в enrich/listing.


## Авторетрай: заявка на под («заявка»)

Когда `DeployOnDemand` падает именно из-за нехватки свободных видеокарт, мы не
показываем красную ошибку, а предлагаем оставить **заявку** — фоновый воркер сам
повторяет запуск, пока карта не освободится или не выйдет таймаут.

**`GpuUnavailableError` (подкласс `RuntimeError`).** Детектор
`is_gpu_unavailable_error(msg)` ловит фразы RunPod про отсутствие инстансов
(`no resources`, `no longer any instances available`, …). `create_pod_via_graphql`
поднимает именно этот тип, когда ошибка в теле GraphQL подходит под детектор;
`create_pod` на нём **не делает CLI-fallback** (CLI тоже падает на дефиците) и
пробрасывает выше. `api_pods_post` ловит его и отвечает HTTP 200 с
`{ok:false, gpuUnavailable:true}` — фронт показывает диалог «Оставить заявку?».

**Таблица `pod_request`** (DB-backed, переживает рестарт). Колонки: `pod_name`,
`assigned_project`, `counts_toward_quota`, `creation_source`, `requested_by`,
`status`, `created_at`, `last_attempt_at`, `last_error`, `pod_id`, `finished_at`.
Индекс `idx_pr_status`. CRUD-хелперы: `create_pod_request`,
`list_pending_requests`, `list_visible_requests`, `get_pod_request`,
`update_pod_request`, `delete_pod_request`, `pending_request_names`,
`count_pending_quota`.

**Квота и имена.** Заявка резервирует слот квоты в момент создания:
`project_quota_usage(project)` = RUNNING-поды (counts_toward_quota) + pending-заявки
(counts_toward_quota). `next_name()` кормят `pending_request_names()`, чтобы прямой
create и заявка не столкнулись на имени.

**Воркер.** `process_pending_requests()` — один тик: для каждой pending-заявки
проверяет таймаут → пробует `create_pod_via_graphql` → на `GpuUnavailableError`
оставляет pending (пишет `last_error`), на прочей ошибке → `failed`, на успехе →
`upsert_pod_assignment` + `fulfilled`. Между деплоем и записью перечитывает статус:
если юзер отменил заявку в полёте — удаляет осиротевший под. Гоняет
`pod_request_loop` (daemon-поток рядом со `scheduler_loop`), интервал берётся из
настроек каждую итерацию (можно менять без рестарта).

**Endpoints:** `POST /api/pod-requests` (создать, проверяет окно+квоту для не-админа),
`DELETE /api/pod-requests/<id>` (pending → `cancelled`; терминальная → удалить
карточку), `GET /api/pods` отдаёт `requests[]` и считает pending в `projectRunning`.

**Настройки (admin):** `pod_request_timeout_minutes` (1..1440, дефолт 15) и
`pod_request_retry_interval_seconds` (5..600, дефолт 15). Правятся в админ-панели
(секция «🔁 Авторетрай заявки на под»).

### Ручной smoke-тест (реальный деплой стоит денег — выполнять осознанно)

1. Временно подменить `PRESET["gpu_id"]` на заведомо недоступный тип, чтобы
   спровоцировать `GpuUnavailableError`.
2. **+ New Pod** → должен появиться диалог «Все видеокарты заняты … Оставить
   заявку?» (не красный тост).
3. **Оставить заявку** → карточка-плейсхолдер со спиннером и «подбираю свободную
   видеокарту, ожидайте…», бейдж квоты увеличивается.
4. `docker compose logs -f runpod-manager` — воркер логирует попытку раз в
   ~интервал секунд.
5. Вернуть рабочий `PRESET["gpu_id"]` → в течение одного интервала карточка
   превращается в реальный под.
6. **Отменить заявку** на свежей pending → карточка исчезает, квота освобождается.
7. Поставить таймаут 1 минуту, оставить заявку без GPU, подождать → карточка
   показывает «Не удалось…» с кнопкой **Закрыть**.
