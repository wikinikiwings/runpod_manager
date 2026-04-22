# Login project-default fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать автоматический выбор проекта `CV` на экране регистрации — вместо него показывать disabled-плейсхолдер "— Выберите проект —" и валидировать выбор перед отправкой, чтобы невнимательный юзер не попадал в чужой проект.

**Architecture:** Два текстовых изменения в `runpod_manager.py`: HTML-плейсхолдер в `<select id="loginProj">` и client-side гард в `doUserLogin()`. Серверная валидация (`validate_registration_input`, строка 726) уже отклоняет пустой/некорректный проект — добавим unit-тест, который это фиксирует.

**Tech Stack:** Python 3 (Flask single-file app), vanilla JS, unittest.

**Spec:** `docs/superpowers/specs/2026-04-22-login-project-default-design.md`

---

## File structure

- Modify: `runpod_manager.py:1883` — HTML-разметка селекта логина
- Modify: `runpod_manager.py:2023-2025` — JS-функция `doUserLogin`
- Modify: `tests/test_migration.py` — добавить новый `TestCase` для `validate_registration_input` (или создать `tests/test_user_validation.py`, см. Task 1)

---

## Task 1: Unit-тест для server-side валидации пустого project

Цель: зафиксировать, что сервер отклоняет `project=""` и произвольный `project="FAKE"` — это страховка на случай, если кто-то обойдёт фронт.

**Files:**
- Create: `tests/test_user_validation.py`

- [ ] **Step 1: Создать тест-файл с падающими тестами**

```python
"""Tests for runpod_manager.validate_registration_input.
Run: python -m unittest tests.test_user_validation
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import runpod_manager as rm


class UserValidationTest(unittest.TestCase):
    def test_empty_project_is_rejected(self):
        """Empty string project must raise — defends against client bypass."""
        with self.assertRaises(rm.UserValidationError):
            rm.validate_registration_input("alice", "")

    def test_unknown_project_is_rejected(self):
        """Project not in PROJECTS whitelist must raise."""
        with self.assertRaises(rm.UserValidationError):
            rm.validate_registration_input("alice", "FAKEPROJECT")

    def test_none_project_is_rejected(self):
        """None project must raise (isinstance check)."""
        with self.assertRaises(rm.UserValidationError):
            rm.validate_registration_input("alice", None)

    def test_valid_project_passes(self):
        """A project from PROJECTS must pass and return normalized values."""
        nick, proj = rm.validate_registration_input("alice", "CV")
        self.assertEqual(proj, "CV")
        self.assertTrue(nick)  # nickname non-empty after normalization

    def test_empty_nickname_is_rejected(self):
        """Empty nickname must raise regardless of valid project."""
        with self.assertRaises(rm.UserValidationError):
            rm.validate_registration_input("", "CV")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тест — убедиться, что все 5 тестов ПРОХОДЯТ сразу**

Run: `python -m unittest tests.test_user_validation -v`

Expected: 5 tests, all PASS. Тест проверяет уже существующее поведение `validate_registration_input` (`runpod_manager.py:706-729`), поэтому фейлов быть не должно. Если хоть один тест фейлится — это означает, что серверная защита сломана, и нужно ЧИНИТЬ сервер, а не тест.

Примечание по TDD: обычно сначала пишем красный тест. Здесь мы фиксируем существующее поведение (regression lock) — новой логики на сервере не добавляем, защиту расширяет только фронт. Если в ходе ревью решим добавить явный `if proj == "":` — тест уже будет на месте.

- [ ] **Step 3: Коммит**

```bash
git add tests/test_user_validation.py
git commit -m "test: lock server-side rejection of empty/unknown project on register"
```

---

## Task 2: HTML-плейсхолдер в селекте логина

**Files:**
- Modify: `runpod_manager.py:1883`

- [ ] **Step 1: Заменить пустой `<select>` на вариант с плейсхолдером**

Найти в `runpod_manager.py` строку 1883:

```html
  <div class="fr"><label>Project</label><select id="loginProj"></select></div>
```

Заменить на:

```html
  <div class="fr"><label>Project</label><select id="loginProj"><option value="" disabled selected>— Выберите проект —</option></select></div>
```

**Важно:** плейсхолдер должен оставаться нетронутым при динамическом добавлении опций в `refreshSession()` (строка 2019). Там используется `sel.appendChild(o)` — это аппенд в конец, так что существующая disabled-опция не затрётся. НЕ менять на `innerHTML = ...` или `sel.innerHTML = ''`.

- [ ] **Step 2: Проверить, что populate-код не затирает плейсхолдер**

Прочитать `runpod_manager.py` в районе строки 2019:

```js
try{const r=await aok('/api/projects');if(r){const sel=$('loginProj');r.projects.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;sel.appendChild(o)})}}catch(e){}
```

Убедиться: используется `appendChild`, нет `innerHTML=''` или `sel.replaceChildren(...)`. Если есть — это ОК для свежей страницы, но для повторной регистрации в той же вкладке (после logout) селект может накопить дубли. В этом проекте после logout вызывается `location.reload()` (строка 2045), так что проблема не возникает. Коммит остаётся как есть.

- [ ] **Step 3: Коммит**

```bash
git add runpod_manager.py
git commit -m "feat(login): add disabled placeholder so project picker has no default"
```

---

## Task 3: Client-side валидация в `doUserLogin`

**Files:**
- Modify: `runpod_manager.py:2023-2025`

- [ ] **Step 1: Добавить проверку пустого project**

Найти в `runpod_manager.py` (около строки 2023):

```js
async function doUserLogin(){
  const n=$('loginNick').value.trim(),p=$('loginProj').value;
  if(!n){toast('Enter a nickname','er');return}
  try{
```

Заменить на:

```js
async function doUserLogin(){
  const n=$('loginNick').value.trim(),p=$('loginProj').value;
  if(!n){toast('Enter a nickname','er');return}
  if(!p){toast('Выберите проект','er');return}
  try{
```

Одна новая строка между двумя существующими.

- [ ] **Step 2: Коммит**

```bash
git add runpod_manager.py
git commit -m "feat(login): require explicit project selection with toast reminder"
```

---

## Task 4: Ручная проверка

Автотестов на фронт в проекте нет, поэтому — живая проверка.

**Files:** (нет правок, только smoke-test)

- [ ] **Step 1: Запустить приложение локально**

Run:

```bash
python runpod_manager.py
```

Ожидаемо: Flask стартует на своём порту (см. `PORT` в верху файла). Открыть в браузере.

- [ ] **Step 2: Сбросить состояние**

В DevTools браузера:

1. Application → Cookies → удалить cookie сессии.
2. Application → Local Storage → удалить ключ `runpod_manager_user` (точное имя см. по константе `LS` в `runpod_manager.py`).
3. Перезагрузить страницу.

Ожидаемо: открывается `#loginScreen`, в селекте `Project` первая строка — `— Выберите проект —`, она disabled и selected.

- [ ] **Step 3: Тест "юзер забыл выбрать проект"**

1. Ввести имя (например, `testuser`).
2. НЕ трогая селект, нажать Enter.

Ожидаемо: появляется красный тост `Выберите проект`. Регистрация не проходит, loginScreen остаётся.

- [ ] **Step 4: Тест "осознанный выбор"**

1. Выбрать в селекте `DV` (НЕ `CV` — чтобы убедиться, что нет прилипания к дефолту).
2. Нажать Enter.

Ожидаемо: loginScreen скрывается, в правом верхнем углу `userTag` показывает `testuser DV`.

- [ ] **Step 5: Тест "обход через DevTools"**

В DevTools Console на открытой странице (юзер ещё НЕ залогинен — сбросить сессию как в Step 2):

```js
fetch('/api/user/register',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"nickname":"hacker","project":""}'}).then(r=>console.log(r.status))
```

Ожидаемо: статус `403` (серверная валидация отклоняет). Это — подтверждение, что сервер защищён и без фронта.

- [ ] **Step 6: Тест "повторный логин после logout"**

1. Кликнуть `userTag` → `changeUser` → страница перезагружается.
2. На экране логина убедиться, что селект снова показывает `— Выберите проект —`, а не запомненный `DV`.

Ожидаемо: плейсхолдер на месте. localStorage хранит только nickname, так что nickname может подставиться — это ожидаемо.

- [ ] **Step 7: Зафиксировать результат ручной проверки в журнале (опционально)**

Если в репо есть журнал разработки — добавить короткую запись о пройденной ручной проверке. Если нет — пропустить этот шаг.

---

## Self-review checklist (заполняется перед завершением плана)

- Spec coverage:
  - "HTML-плейсхолдер" → Task 2 ✓
  - "Client-side валидация" → Task 3 ✓
  - "Server-side не меняем, но страхуем тестом" → Task 1 ✓
  - "Ручная проверка всех векторов обхода" → Task 4 (steps 3, 5) ✓
- Placeholder scan: нет "TBD"/"TODO"/"similar to Task N" — все шаги содержат конкретный код и команды.
- Type consistency: `validate_registration_input` и `UserValidationError` — имена соответствуют `runpod_manager.py:706, 714` (проверено grep'ом). `doUserLogin`, `$('loginProj')`, `$('loginNick')`, `toast()` — все существуют в текущем файле.
