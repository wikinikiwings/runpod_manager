# Per-project pod quotas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single global `max_pods` quota with per-project configurable quotas. Each project gets its own limit. Users see only pods assigned to their project; admin sees all. Admin can assign (or reassign) any pod — including pods created in RunPod's web UI — to any project, or leave unassigned (admin-only visibility). Admin bypasses quotas; admin-created pods are tagged in the UI.

**Architecture:** Single Flask monolith `runpod_manager.py` (~2500 lines) + embedded HTML/JS. Persistence in SQLite. Introduces a new table `pod_assignment(pod_id, assigned_project, counts_toward_quota, creation_source, assigned_at, assigned_by)` which replaces `pod_hidden`. Migration runs once at startup: back-fills pod_assignment from the existing pod_actions audit log. All frontend changes are embedded in the same .py file.

**Tech Stack:** Python 3.12, Flask, SQLite (stdlib `sqlite3`), Docker/compose, inline HTML/CSS/vanilla JS. No test framework yet — the plan introduces `tests/` using stdlib `unittest`.

**Spec:** [`docs/superpowers/specs/2026-04-21-per-project-quotas-design.md`](../specs/2026-04-21-per-project-quotas-design.md)

---

## Codebase orientation (zero-context intro)

`runpod_manager.py` is organized in strict top-down order. Sections the engineer will touch:

| Section | Lines | What's here |
|---------|-------|-------------|
| Globals, `PRESET`, `DEFAULT_SETTINGS`, `PROJECTS` | 1–87 | Constants. `PROJECTS = ["CV","DV","MT","PT","MARK","ADMIN","TV","MW"]` |
| SQLite layer | 247–439 | `init_db()`, `log_action()`, `touch_user()`, `get_pod_creators()`, pod_timers (init/touch/delete/get_all), pod_hidden (hide/unhide/get_ids/is_hidden) — **pod_hidden helpers to be replaced** |
| Settings | 441–456 | `load_settings()`, `save_settings()` |
| `list_pods()` | 797–925 | Returns enriched pod list — **add assignment fields here** |
| `create_pod()` / `delete_pod()` / `start_pod()` | 1043–1142 | **Quota check in `create_pod`** |
| Routes | 1212–1454 | `/api/pods*`, `/api/admin/*` — **modify several** |
| Embedded frontend | 1459–2480 | `FRONTEND_HTML` — big string with HTML + CSS + JS. Functions: `loadAdminPanel()` (settings form), `createPod()` (called by "+ New pod" button), `render()` (renders pod cards), `formatLocalFull()` (date format in activity log), `togglePodVisibility()` (hide/unhide). |
| Main | 2493–2512 | argparse, `init_db()`, scheduler thread, `app.run()` |

**Run / test loop:**
- On Windows host: `docker compose up -d --build` (rebuild on every code change to `runpod_manager.py`; 10–30s for incremental build).
- Logs: `docker compose logs -f runpod-manager`.
- Web UI: `http://localhost:5001`.
- Direct container shell: `docker compose exec runpod-manager bash`.
- Inspect DB: `docker compose exec runpod-manager sqlite3 /app/data/runpod_manager.db "SELECT * FROM pod_assignment;"`.
- Reset DB (wipe data!): `docker compose down -v && docker compose up -d --build`.
- **Do NOT wipe prod data.** For migration testing use the `tests/test_migration.py` script against a temp DB (Task 1).

**Commit convention:** small, purposeful commits with short subjects (`feat:`, `fix:`, `refactor:`). Look at recent `git log` for style.

---

## File structure changes

| File | Action | Purpose |
|------|--------|---------|
| `runpod_manager.py` | Modify (the main workhorse) | All backend + frontend changes |
| `tests/__init__.py` | Create (empty) | Make tests a package |
| `tests/test_migration.py` | Create | Standalone unittest script for migration — runs without Docker, uses temp sqlite DB |
| `docs/database.md` | Modify | Replace pod_hidden docs with pod_assignment |
| `docs/admin-panel.md` | Modify | Update endpoint list + settings semantics |
| `docs/architecture.md` | Modify | Update section map (migration + new helpers) |

We keep the monolithic single-file approach because the codebase already does. The migration test lives outside to make it runnable in isolation.

---

## Task 1: Database — create pod_assignment table + migration + standalone test

**Goal:** add the new table, add a one-shot migration function that back-fills from `pod_hidden` + `pod_actions`, drop `pod_hidden`. Write a unittest that exercises it on a temp DB before touching prod.

**Files:**
- Create: `E:/my_stable/runpod_manager/tests/__init__.py` (empty)
- Create: `E:/my_stable/runpod_manager/tests/test_migration.py`
- Modify: `E:/my_stable/runpod_manager/runpod_manager.py:260–286` (`init_db()` — add new table + call migration)
- Modify: `E:/my_stable/runpod_manager/runpod_manager.py:287` (insert new function `migrate_to_pod_assignment()` right after `init_db()`)

- [ ] **Step 1: Create empty `tests/__init__.py`**

```python
# (empty file)
```

- [ ] **Step 2: Write the migration test (TDD — test first)**

Create `tests/test_migration.py`:

```python
"""Migration test: pod_hidden + pod_actions → pod_assignment.
Runs against a temp SQLite DB, no Docker required.
Run: python -m unittest tests.test_migration
"""
import os
import sys
import tempfile
import sqlite3
import unittest
from pathlib import Path

# Make runpod_manager importable when running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

import runpod_manager as rm


class MigrationTest(unittest.TestCase):
    def setUp(self):
        # Temp DB per-test
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        # Redirect the module to use our temp DB
        self._orig_db_path = rm.DB_PATH
        rm.DB_PATH = Path(self.db_path)
        # Create OLD schema (pre-migration state): pod_hidden + pod_actions
        db = sqlite3.connect(self.db_path)
        db.executescript("""
            CREATE TABLE pod_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT, project TEXT, action TEXT NOT NULL,
                pod_name TEXT, pod_id TEXT,
                ts TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
            CREATE TABLE pod_hidden (
                pod_id TEXT PRIMARY KEY,
                hidden_at TEXT NOT NULL,
                hidden_by TEXT NOT NULL);
        """)
        # Seed data: 3 creates (one hidden, one CV-user, one unknown-project)
        db.execute("INSERT INTO pod_actions(nickname,project,action,pod_name,pod_id,ts) VALUES('alice','CV','create','pod_1','p1_id','2026-04-20T12:00:00Z')")
        db.execute("INSERT INTO pod_actions(nickname,project,action,pod_name,pod_id,ts) VALUES('bob','DV','create','pod_2','p2_id','2026-04-20T13:00:00Z')")
        db.execute("INSERT INTO pod_actions(nickname,project,action,pod_name,pod_id,ts) VALUES('garbage','NOTAPROJECT','create','pod_3','p3_id','2026-04-20T14:00:00Z')")
        # Hidden pod (maps to NULL project post-migration)
        db.execute("INSERT INTO pod_hidden(pod_id,hidden_at,hidden_by) VALUES('p_hidden','2026-04-20T11:00:00Z','admin_joe')")
        # Also put a create for the hidden pod — migration should prefer pod_hidden mapping over pod_actions
        db.execute("INSERT INTO pod_actions(nickname,project,action,pod_name,pod_id,ts) VALUES('admin_joe','ADMIN','create','pod_secret','p_hidden','2026-04-20T10:00:00Z')")
        db.commit()
        db.close()

    def tearDown(self):
        rm.DB_PATH = self._orig_db_path
        os.unlink(self.db_path)

    def test_migration_creates_pod_assignment_and_drops_pod_hidden(self):
        rm.init_db()  # This triggers migration
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        # pod_assignment should exist
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pod_assignment'")
        self.assertIsNotNone(cur.fetchone())
        # pod_hidden should be dropped
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pod_hidden'")
        self.assertIsNone(cur.fetchone())
        # p1_id → assigned to CV, user source, counts=1
        r = db.execute("SELECT * FROM pod_assignment WHERE pod_id='p1_id'").fetchone()
        self.assertEqual(r["assigned_project"], "CV")
        self.assertEqual(r["counts_toward_quota"], 1)
        self.assertEqual(r["creation_source"], "user")
        self.assertEqual(r["assigned_by"], "alice")
        # p2_id → DV, same
        r = db.execute("SELECT * FROM pod_assignment WHERE pod_id='p2_id'").fetchone()
        self.assertEqual(r["assigned_project"], "DV")
        # p3_id → skipped (bad project), so no row
        r = db.execute("SELECT * FROM pod_assignment WHERE pod_id='p3_id'").fetchone()
        self.assertIsNone(r)
        # p_hidden → NULL project, counts=0, source='user' (safe default)
        r = db.execute("SELECT * FROM pod_assignment WHERE pod_id='p_hidden'").fetchone()
        self.assertIsNone(r["assigned_project"])
        self.assertEqual(r["counts_toward_quota"], 0)
        self.assertEqual(r["creation_source"], "user")
        self.assertEqual(r["assigned_by"], "migration")
        db.close()

    def test_migration_is_idempotent(self):
        """Running init_db twice must not duplicate rows or fail."""
        rm.init_db()
        rm.init_db()  # Second run: pod_hidden already dropped
        db = sqlite3.connect(self.db_path)
        count = db.execute("SELECT COUNT(*) FROM pod_assignment").fetchone()[0]
        self.assertEqual(count, 3)  # p1, p2, p_hidden — not duplicated
        db.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails (function not defined yet)**

Run in Windows shell (bash via Git Bash or WSL, from repo root):

```bash
cd E:/my_stable/runpod_manager
python -m unittest tests.test_migration
```

Expected: `AttributeError: module 'runpod_manager' has no attribute 'migrate_to_pod_assignment'` OR the test fails because `pod_assignment` is never created. Either way, NOT ok. That's the point — test must fail first.

- [ ] **Step 4: Add pod_assignment table + migration to `runpod_manager.py`**

Modify `init_db()` at `runpod_manager.py:260–286`:

```python
def init_db():
    db = sqlite3.connect(str(DB_PATH))
    # NOTE: All timestamps are stored as UTC ISO 8601 with 'Z' suffix.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL, project TEXT NOT NULL,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            last_seen TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
        CREATE TABLE IF NOT EXISTS pod_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT, project TEXT, action TEXT NOT NULL,
            pod_name TEXT, pod_id TEXT,
            ts TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
        CREATE INDEX IF NOT EXISTS idx_pa_ts ON pod_actions(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_pa_pod ON pod_actions(pod_id, action);
        CREATE TABLE IF NOT EXISTS pod_timers (
            pod_id TEXT PRIMARY KEY,
            last_active TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pod_assignment (
            pod_id TEXT PRIMARY KEY,
            assigned_project TEXT,
            counts_toward_quota INTEGER NOT NULL DEFAULT 1,
            creation_source TEXT NOT NULL DEFAULT 'user',
            assigned_at TEXT NOT NULL,
            assigned_by TEXT NOT NULL);
    """)
    db.close()
    migrate_to_pod_assignment()
```

Add `migrate_to_pod_assignment()` immediately after `init_db()` (around line 287):

```python
def migrate_to_pod_assignment():
    """One-shot migration from pod_hidden → pod_assignment.
    Idempotent: if pod_hidden doesn't exist (already migrated), does nothing.
    Also back-fills pod_assignment from pod_actions.create for pods not in pod_hidden.
    """
    db = sqlite3.connect(str(DB_PATH))
    try:
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pod_hidden'")
        has_hidden = cur.fetchone() is not None

        hidden_count = 0
        if has_hidden:
            # Step 1: hidden pods → assigned_project=NULL, counts=0
            rows = db.execute("SELECT pod_id FROM pod_hidden").fetchall()
            now = now_iso()
            for (pid,) in rows:
                existing = db.execute("SELECT 1 FROM pod_assignment WHERE pod_id=?", (pid,)).fetchone()
                if existing:
                    continue
                db.execute("""INSERT INTO pod_assignment
                    (pod_id, assigned_project, counts_toward_quota, creation_source, assigned_at, assigned_by)
                    VALUES (?, NULL, 0, 'user', ?, 'migration')""", (pid, now))
                hidden_count += 1

        # Step 2: back-fill from most-recent pod_actions.create per pod_id
        # (skip pods already in pod_assignment — includes ones just added from pod_hidden)
        creator_count = 0
        now = now_iso()
        rows = db.execute("""
            SELECT pod_id, nickname, project, MAX(ts) AS max_ts
            FROM pod_actions
            WHERE action='create'
            GROUP BY pod_id
        """).fetchall()
        for pid, nickname, project, _max_ts in rows:
            existing = db.execute("SELECT 1 FROM pod_assignment WHERE pod_id=?", (pid,)).fetchone()
            if existing:
                continue
            if project not in PROJECTS:
                continue  # bad data, admin can assign manually
            db.execute("""INSERT INTO pod_assignment
                (pod_id, assigned_project, counts_toward_quota, creation_source, assigned_at, assigned_by)
                VALUES (?, ?, 1, 'user', ?, ?)""", (pid, project, now, nickname or 'migration'))
            creator_count += 1

        # Step 3: drop pod_hidden (only after successful processing)
        if has_hidden:
            db.execute("DROP TABLE pod_hidden")

        db.commit()
        if has_hidden or creator_count > 0:
            log.info(f"[MIGRATION] pod_assignment populated: {hidden_count} from pod_hidden, {creator_count} from pod_actions")
    except Exception as e:
        db.rollback()
        log.error(f"migrate_to_pod_assignment failed: {e}")
        raise
    finally:
        db.close()
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python -m unittest tests.test_migration -v
```

Expected output:

```
test_migration_creates_pod_assignment_and_drops_pod_hidden ... ok
test_migration_is_idempotent ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.xxxs

OK
```

If any test fails, read the assertion error and fix the migration logic. Do NOT proceed until both tests pass.

- [ ] **Step 6: Commit**

```bash
cd E:/my_stable/runpod_manager
git add tests/ runpod_manager.py
git commit -m "feat: add pod_assignment table + pod_hidden migration

New table replaces pod_hidden with a per-project assignment model.
Migration runs inside init_db() and is idempotent. Covered by
tests/test_migration.py (stdlib unittest, no Docker required)."
```

---

## Task 2: Settings — add project_quotas to DEFAULT_SETTINGS

**Goal:** introduce `project_quotas` dict into defaults with 4 per project. `max_pods` stays in settings schema but is unused.

**Files:**
- Modify: `runpod_manager.py:78–87` (`DEFAULT_SETTINGS`)
- Modify: `runpod_manager.py:1359–1363` (`api_admin_settings_get`)
- Modify: `runpod_manager.py:1365–1387` (`api_admin_settings_post`)

- [ ] **Step 1: Update DEFAULT_SETTINGS and add DEFAULT_PROJECT_QUOTA constant**

Add near `PROJECTS` at `runpod_manager.py:67`:

```python
PROJECTS = ["CV", "DV", "MT", "PT", "MARK", "ADMIN", "TV", "MW"]
DEFAULT_PROJECT_QUOTA = 4
```

Modify `DEFAULT_SETTINGS`:

```python
DEFAULT_SETTINGS = {"admin_password":"admin","max_pods":5,
    "project_quotas":{p: DEFAULT_PROJECT_QUOTA for p in PROJECTS},
    "auto_delete_enabled":False,"auto_delete_time":"21:00",
    "auto_delete_last_run":"","auto_delete_last_log":"",
    "idle_timeout_enabled":True,"idle_timeout_minutes":120,
    "pod_window_enabled":False,"pod_window_from":"22:00","pod_window_until":"08:00"}
```

- [ ] **Step 2: Update settings GET to include project_quotas**

At `runpod_manager.py:1361–1363` replace the whole function body:

```python
def api_admin_settings_get():
    s=get_settings()
    # Ensure project_quotas has entries for every current PROJECT (handles
    # post-migration sessions where a new project was added in code)
    quotas = dict(s.get("project_quotas") or {})
    for p in PROJECTS:
        if p not in quotas:
            quotas[p] = DEFAULT_PROJECT_QUOTA
    return jsonify({"ok":True,"settings":{
        "project_quotas": quotas,
        **{k:s.get(k) for k in ["auto_delete_enabled","auto_delete_time","auto_delete_last_log","idle_timeout_enabled","idle_timeout_minutes","pod_window_enabled","pod_window_from","pod_window_until"]}
    }})
```

Note: `max_pods` is intentionally dropped from the response — the admin UI no longer needs it.

- [ ] **Step 3: Update settings POST to validate project_quotas**

At `runpod_manager.py:1365–1387`, find the section that currently handles `max_pods`:

```python
    if "max_pods" in data: s["max_pods"]=max(1,min(50,int(data["max_pods"])))
```

Replace with:

```python
    # Per-project quotas. Each value 0-50. Unknown project keys ignored.
    if isinstance(data.get("project_quotas"), dict):
        quotas = dict(s.get("project_quotas") or {})
        for proj, val in data["project_quotas"].items():
            if proj not in PROJECTS:
                continue
            try:
                quotas[proj] = max(0, min(50, int(val)))
            except (TypeError, ValueError):
                pass
        s["project_quotas"] = quotas
```

- [ ] **Step 4: Rebuild and smoke-test**

```bash
cd E:/my_stable/runpod_manager
docker compose up -d --build
```

Wait for build to complete (~30s). Check logs:

```bash
docker compose logs --tail=30 runpod-manager
```

Expected: container starts, no tracebacks. You may see `[MIGRATION]` log line if prod DB had pod_hidden rows.

Test the settings endpoint:

```bash
# Login as admin (password=admin by default)
curl -c /tmp/cj.txt -X POST http://localhost:5001/api/admin/login \
  -H "Content-Type: application/json" -d '{"password":"admin"}'

# Fetch settings
curl -b /tmp/cj.txt http://localhost:5001/api/admin/settings | python -m json.tool
```

Expected: JSON includes `"project_quotas": {"CV":4,"DV":4,"MT":4,"PT":4,"MARK":4,"ADMIN":4,"TV":4,"MW":4}`.

- [ ] **Step 5: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: add project_quotas to admin settings

Per-project quota dict with default 4 per project. max_pods kept in
storage for backward compat but no longer read or exposed in the
settings API response."
```

---

## Task 3: DB helpers — replace pod_hidden helpers with pod_assignment helpers

**Goal:** remove `hide_pod_id`, `unhide_pod_id`, `get_hidden_ids`, `is_pod_hidden`. Add new helpers that operate on `pod_assignment`. Determine_creation_source() for the `/assign` flow.

**Files:**
- Modify: `runpod_manager.py:375–439` (replace the entire hidden-pods section)

- [ ] **Step 1: Replace the pod_hidden helpers block**

Delete the existing block from `runpod_manager.py:375–439` (everything from the comment `# ----- Hidden pods (admin-only visibility control) -----` through `is_pod_hidden` function).

Replace with:

```python
# ----- Pod assignment (project ownership + admin-only visibility) -----
#
# Each pod gets a row in pod_assignment mapping it to a project (or NULL =
# admin-only). The row also carries counts_toward_quota (admin-created pods
# may be exempt) and creation_source (user/admin/external — drives UI badges).
# Regular users only see pods whose assigned_project == their session project.
# Admin sees all, including pods without any assignment (e.g. created in
# RunPod's web UI — labeled 'external' when admin first assigns them).

def upsert_pod_assignment(pid, assigned_project, counts_toward_quota,
                          creation_source, assigned_by):
    """INSERT-or-UPDATE pod_assignment. creation_source is preserved from
    an existing row if present (source is immutable after first write).
    Caller is responsible for computing source correctly on FIRST write."""
    try:
        db = sqlite3.connect(str(DB_PATH))
        existing = db.execute("SELECT creation_source FROM pod_assignment WHERE pod_id=?",
                              (pid,)).fetchone()
        if existing:
            # Update the mutable fields, keep original creation_source
            db.execute("""UPDATE pod_assignment
                SET assigned_project=?, counts_toward_quota=?, assigned_at=?, assigned_by=?
                WHERE pod_id=?""",
                (assigned_project, 1 if counts_toward_quota else 0,
                 now_iso(), assigned_by, pid))
        else:
            db.execute("""INSERT INTO pod_assignment
                (pod_id, assigned_project, counts_toward_quota, creation_source,
                 assigned_at, assigned_by)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (pid, assigned_project, 1 if counts_toward_quota else 0,
                 creation_source, now_iso(), assigned_by))
        db.commit(); db.close()
    except Exception as e: log.error(f"upsert_pod_assignment: {e}")

def get_pod_assignment(pid):
    """Returns dict or None. Keys: assigned_project (may be None),
    counts_toward_quota (bool), creation_source (str)."""
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT assigned_project, counts_toward_quota, creation_source
                            FROM pod_assignment WHERE pod_id=?""", (pid,)).fetchone()
        db.close()
        if row is None:
            return None
        return {
            "assigned_project": row["assigned_project"],
            "counts_toward_quota": bool(row["counts_toward_quota"]),
            "creation_source": row["creation_source"],
        }
    except Exception as e:
        log.error(f"get_pod_assignment: {e}")
        return None

def get_assignments_batch(pod_ids):
    """Returns {pod_id: {assigned_project, counts_toward_quota, creation_source}}
    for the listed pod_ids (missing rows are simply absent from the dict)."""
    if not pod_ids: return {}
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(pod_ids))
        rows = db.execute(f"""SELECT pod_id, assigned_project, counts_toward_quota, creation_source
            FROM pod_assignment WHERE pod_id IN ({placeholders})""", pod_ids).fetchall()
        db.close()
        return {r["pod_id"]: {
            "assigned_project": r["assigned_project"],
            "counts_toward_quota": bool(r["counts_toward_quota"]),
            "creation_source": r["creation_source"],
        } for r in rows}
    except Exception as e:
        log.error(f"get_assignments_batch: {e}")
        return {}

def delete_pod_assignment(pid):
    """Remove the assignment row on pod deletion. Idempotent."""
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.execute("DELETE FROM pod_assignment WHERE pod_id=?", (pid,))
        db.commit(); db.close()
    except Exception as e:
        log.error(f"delete_pod_assignment: {e}")

def determine_creation_source_for_unknown(pid):
    """For the first /assign on a pod that has no pod_assignment yet:
    return 'external' if there is no pod_actions.create for it,
    else 'user' (legacy pods from before this feature shipped)."""
    try:
        db = sqlite3.connect(str(DB_PATH))
        has = db.execute("SELECT 1 FROM pod_actions WHERE pod_id=? AND action='create' LIMIT 1",
                         (pid,)).fetchone()
        db.close()
        return 'user' if has else 'external'
    except Exception as e:
        log.error(f"determine_creation_source_for_unknown: {e}")
        return 'external'  # safer fallback
```

- [ ] **Step 2: Update `delete_pod` to call `delete_pod_assignment` instead of `unhide_pod_id`**

Find `delete_pod` at `runpod_manager.py:1119–1132`. Replace the line:

```python
    unhide_pod_id(pid)
```

with:

```python
    delete_pod_assignment(pid)
```

(This is the only caller of `unhide_pod_id` outside the hide/unhide endpoints, which themselves are being removed in Task 7.)

- [ ] **Step 3: Rebuild and verify boot**

```bash
docker compose up -d --build
docker compose logs --tail=40 runpod-manager
```

Expected: no tracebacks. The frontend will be broken (pod cards reference `p.hidden`) but the backend boots.

Verify new helpers via a one-shot Python exec:

```bash
docker compose exec runpod-manager python3 -c "
import runpod_manager as rm
rm.upsert_pod_assignment('test_pid_1','CV',True,'user','smoke_test')
print(rm.get_pod_assignment('test_pid_1'))
print(rm.get_assignments_batch(['test_pid_1','missing']))
rm.delete_pod_assignment('test_pid_1')
print('after delete:', rm.get_pod_assignment('test_pid_1'))
print('unknown pod source:', rm.determine_creation_source_for_unknown('no_such_pod'))
"
```

Expected output:

```
{'assigned_project': 'CV', 'counts_toward_quota': True, 'creation_source': 'user'}
{'test_pid_1': {'assigned_project': 'CV', 'counts_toward_quota': True, 'creation_source': 'user'}}
after delete: None
unknown pod source: external
```

- [ ] **Step 4: Commit**

```bash
git add runpod_manager.py
git commit -m "refactor: replace pod_hidden helpers with pod_assignment

Adds upsert / get / get_batch / delete / determine_creation_source
functions. delete_pod now cleans pod_assignment instead of pod_hidden.
hide_pod_id / unhide_pod_id / get_hidden_ids / is_pod_hidden removed;
their callers in routes will be updated in later tasks."
```

Note: you may see warnings or errors from the still-existing `/hide` and `/unhide` routes referring to removed functions. That's expected; they're removed in Task 7.

---

## Task 4: list_pods — integrate assignment data + filter in api_pods_get

**Goal:** attach `assignedProject`, `countsTowardQuota`, `creationSource` to every pod in `list_pods()`. Replace `p["hidden"]` with `assignedProject == None`. In `api_pods_get`, filter by user's project for non-admin and swap quota math to per-project.

**Files:**
- Modify: `runpod_manager.py:820–882` (inside `list_pods`, where `hidden_ids` is used)
- Modify: `runpod_manager.py:1251–1295` (`api_pods_get`)

- [ ] **Step 1: In `list_pods`, replace `get_hidden_ids()` with batch assignment fetch**

Find `runpod_manager.py:822–825` (the hidden_ids block with its preceding comment) and replace with:

```python
    # Batch-fetch per-pod assignments (pod_id -> {assigned_project,
    # counts_toward_quota, creation_source}). Pods without a row in
    # pod_assignment have no assignment — surfaced to the caller as nulls,
    # equivalent to 'unassigned' / admin-only visibility.
    assignments = get_assignments_batch(all_ids)
```

- [ ] **Step 2: In the per-pod loop, replace the `p["hidden"]` annotation**

Find `runpod_manager.py:878–882` (the "Annotate hidden status" block) and replace:

```python
        # ===== Assignment annotation =====
        # Expose the assignment fields to the frontend. Unassigned (no row or
        # assigned_project=NULL) means admin-only — the user-facing filter in
        # api_pods_get drops these for non-admins.
        a = assignments.get(pid)
        if a:
            p["assignedProject"] = a["assigned_project"]
            p["countsTowardQuota"] = a["counts_toward_quota"]
            p["creationSource"] = a["creation_source"]
        else:
            p["assignedProject"] = None
            p["countsTowardQuota"] = False
            p["creationSource"] = "external"
```

- [ ] **Step 3: Rewrite `api_pods_get` to filter by project + compute per-project quota**

Replace the entire `api_pods_get` function at `runpod_manager.py:1251–1295`:

```python
@app.route("/api/pods", methods=["GET"])
@require_user
def api_pods_get():
    try:
        all_pods = list_pods()
        s = get_settings()
        viewer_is_admin = is_admin()
        nick, viewer_project = g.current_user
        if viewer_is_admin:
            # Admin sees everything, including unassigned pods and pods from
            # every project. The frontend renders assignedProject / creationSource
            # badges so the admin can tell them apart.
            pods = all_pods
        else:
            # Regular users only see pods assigned to their own project.
            pods = [p for p in all_pods if p.get("assignedProject") == viewer_project]

        # Per-project quota. Regular users see only their project's slot count.
        # Admin sees their own project's count too (same session.user_project)
        # — admin-specific bypass logic is inside create_pod, not here.
        quotas = s.get("project_quotas") or {}
        project_quota = quotas.get(viewer_project, DEFAULT_PROJECT_QUOTA)
        # Count running pods IN THIS PROJECT with counts_toward_quota=True.
        project_running = sum(1 for p in all_pods
                              if p.get("desiredStatus") == "RUNNING"
                              and p.get("assignedProject") == viewer_project
                              and p.get("countsTowardQuota"))
        quota_used = min(project_running, project_quota)
        over_quota = max(0, project_running - project_quota)

        # For admin convenience, also return full project_quotas dict + each
        # project's running count so the admin UI can show a per-project matrix.
        project_counts = {}
        for proj in PROJECTS:
            project_counts[proj] = sum(1 for p in all_pods
                                        if p.get("desiredStatus") == "RUNNING"
                                        and p.get("assignedProject") == proj
                                        and p.get("countsTowardQuota"))

        sched = {"time": s["auto_delete_time"],
                 "lastLog": s.get("auto_delete_last_log", "")} if s.get("auto_delete_enabled") else None
        window = check_pod_window()
        return jsonify({"ok": True, "pods": pods,
                        "viewerProject": viewer_project,
                        "projectQuota": project_quota,
                        "projectRunning": project_running,
                        "quotaUsed": quota_used,
                        "overQuota": over_quota,
                        "projectQuotas": quotas,
                        "projectCounts": project_counts,
                        "schedule": sched,
                        "idleTimeoutEnabled": s.get("idle_timeout_enabled", True),
                        "idleTimeoutMinutes": s.get("idle_timeout_minutes", 120),
                        "podWindow": window,
                        "isAdmin": viewer_is_admin})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
```

Note: removed the old `maxPods` / `runningCount` fields. The frontend will be updated in Task 9 to read the new shape. Until then the UI will show NaN in a few places — acceptable during the implementation window; tasks 8–12 bring the UI to parity.

- [ ] **Step 4: Rebuild and smoke-test the listing**

```bash
docker compose up -d --build
```

Hit the listing as a regular user (register first if needed):

```bash
curl -c /tmp/uc.txt -X POST http://localhost:5001/api/user/register \
  -H "Content-Type: application/json" -d '{"nickname":"smoke","project":"CV"}'
curl -b /tmp/uc.txt http://localhost:5001/api/pods | python -m json.tool | head -40
```

Expected:
- `"isAdmin": false`
- `"viewerProject": "CV"`
- `"projectQuota": 4`
- `"projectRunning": N` (N = pods assigned to CV right now)
- Each pod in `"pods"` array has `"assignedProject"`, `"countsTowardQuota"`, `"creationSource"` fields (frontend can be broken, that's fine).

Hit as admin:

```bash
curl -c /tmp/ac.txt -X POST http://localhost:5001/api/admin/login \
  -H "Content-Type: application/json" -d '{"password":"admin"}'
# register admin as user too so api_pods_get has session.user_project
curl -b /tmp/ac.txt -c /tmp/ac.txt -X POST http://localhost:5001/api/user/register \
  -H "Content-Type: application/json" -d '{"nickname":"admin","project":"ADMIN"}'
curl -b /tmp/ac.txt http://localhost:5001/api/pods | python -m json.tool | head -40
```

Expected: `"isAdmin": true`. `pods` array contains ALL pods (including ones that are unassigned or assigned to projects other than ADMIN).

- [ ] **Step 5: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: list_pods returns per-project assignment + api_pods_get filters by project

list_pods now attaches assignedProject / countsTowardQuota / creationSource
to each pod (batch-fetched from pod_assignment). api_pods_get filters for
non-admin viewers to show only their project's pods, and returns per-project
quota and running counts in the response."
```

---

## Task 5: create flow — per-project quota check + admin body params + pod_assignment INSERT

**Goal:** `create_pod()` enforces the caller's project quota (bypass for admin). `api_pods_post` reads admin-only body params (`assigned_project`, `counts_toward_quota`). Writes the pod_assignment row after successful RunPod create.

**Files:**
- Modify: `runpod_manager.py:1043–1118` (`create_pod`)
- Modify: `runpod_manager.py:1297–1312` (`api_pods_post`)

- [ ] **Step 1: Rewrite the quota check inside `create_pod`**

Find the old block at `runpod_manager.py:1052–1074`:

```python
    # Limit check: admins bypass the limit entirely ...
    if not bypass_window:
        current = list_pods()
        max_pods = s.get("max_pods", 99)
        visible_running = sum(1 for p in current
                              if p.get("desiredStatus") == "RUNNING"
                              and not p.get("hidden"))
        if visible_running >= max_pods:
            raise RuntimeError(f"Достигнут лимит подов: {visible_running}/{max_pods}")
```

Replace with:

```python
    # Per-project quota. Admins bypass (bypass_window doubles as the admin-is-caller signal
    # — the caller wires it up as bypass_window=is_admin() at the route level).
    if not bypass_window:
        nick, proj = get_session_user()  # user is already authenticated via @require_user
        quotas = s.get("project_quotas") or {}
        quota = quotas.get(proj, DEFAULT_PROJECT_QUOTA)
        current = list_pods()
        project_running = sum(1 for p in current
                              if p.get("desiredStatus") == "RUNNING"
                              and p.get("assignedProject") == proj
                              and p.get("countsTowardQuota"))
        if project_running >= quota:
            raise RuntimeError(f"Достигнут лимит {proj}: {project_running}/{quota}")
```

Note: `get_session_user()` returns `(nick, project)` from the session. It's fine to call at the function level because `api_pods_post` runs inside the Flask request context (`@require_user` already ran).

- [ ] **Step 2: Rewrite `api_pods_post` to accept admin params and INSERT pod_assignment**

Replace `api_pods_post` at `runpod_manager.py:1297–1312`:

```python
@app.route("/api/pods", methods=["POST"])
@require_user
def api_pods_post():
    try:
        # Identity comes from session (via @require_user), NOT from request body.
        nick, proj = g.current_user
        admin = is_admin()
        data = request.get_json(silent=True) or {}

        # Admin-only fields. Non-admin bodies are ignored (security: prevent spoofing
        # a pod into a different project).
        if admin:
            ap = data.get("assigned_project")
            if ap is not None and ap not in PROJECTS:
                return jsonify({"ok":False,"error":"Unknown project"}), 400
            cf = bool(data.get("counts_toward_quota", False))
            src = 'admin'
        else:
            ap = proj
            cf = True
            src = 'user'

        pods = list_pods()
        name = next_name(pods)
        # Admins bypass window + per-project quota; regular users are checked inside create_pod
        result = create_pod(name, bypass_window=admin)
        pid = result.get("id", "") if isinstance(result, dict) else ""
        log_action(nick, proj, "create", name, pid)
        # INSERT pod_assignment so subsequent list_pods() sees this pod in the
        # right project (and quota/visibility works from the first refresh).
        if pid:
            upsert_pod_assignment(pid, ap, cf, src, nick)
        return jsonify({"ok":True,"name":name,
                        "comfyUrl":f"https://{pid}-{PRESET['comfy_port']}.proxy.runpod.net" if pid else None})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500
```

- [ ] **Step 3: Rebuild and smoke-test**

```bash
docker compose up -d --build
```

Test quota enforcement as a regular user with a low quota. First set CV quota to 1:

```bash
curl -c /tmp/ac.txt -X POST http://localhost:5001/api/admin/login \
  -H "Content-Type: application/json" -d '{"password":"admin"}'
curl -b /tmp/ac.txt -X POST http://localhost:5001/api/admin/settings \
  -H "Content-Type: application/json" \
  -d '{"project_quotas":{"CV":1,"DV":4,"MT":4,"PT":4,"MARK":4,"ADMIN":4,"TV":4,"MW":4}}'
```

Then register as a CV user and try creating 2 pods:

```bash
curl -c /tmp/uc.txt -X POST http://localhost:5001/api/user/register \
  -H "Content-Type: application/json" -d '{"nickname":"cvuser","project":"CV"}'
curl -b /tmp/uc.txt -X POST http://localhost:5001/api/pods
# First call: may succeed or 500 with "no resources" depending on RunPod availability;
# either way the quota check passed.
# If the first actually created a pod, wait ~30s for it to be RUNNING then:
curl -b /tmp/uc.txt -X POST http://localhost:5001/api/pods
# Second call expected: {"ok":false,"error":"Достигнут лимит CV: 1/1"}
```

**Warning:** this test creates real pods that cost money. Delete any successfully-created test pod via `curl -X DELETE http://localhost:5001/api/pods/<id>` or from the RunPod console. **Restore CV quota to 4 when done.**

If RunPod has no available GPU and `create_pod` fails before the quota check, you can smoke-test the quota math without creating real pods by manually inserting a fake pod_assignment row and seeing the quota respond:

```bash
docker compose exec runpod-manager python3 -c "
import runpod_manager as rm
rm.upsert_pod_assignment('fake_cv_1','CV',True,'user','test')
# Now simulate a listing with that fake pod as RUNNING — we can't easily fake
# RunPod's listing without stubbing, so skip this — the integration test above
# is the real check."
# Clean up:
docker compose exec runpod-manager python3 -c "
import runpod_manager as rm
rm.delete_pod_assignment('fake_cv_1')"
```

- [ ] **Step 4: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: per-project quota enforcement in create_pod + admin params in api_pods_post

create_pod now blocks regular users at their project's quota. api_pods_post
accepts admin-only body params (assigned_project, counts_toward_quota) and
writes the assignment row immediately after RunPod returns the new pod ID
so downstream list_pods() sees it in the right project on the next tick."
```

---

## Task 6: delete + start — visibility check via assignment

**Goal:** `api_del` and `api_start` block non-admin access to pods not in their project (was: block hidden-only).

**Files:**
- Modify: `runpod_manager.py:1314–1329` (`api_del`)
- Modify: `runpod_manager.py:1331–1345` (`api_start`)

- [ ] **Step 1: Update `api_del`**

Replace the `is_pod_hidden` check in `api_del`:

```python
        if is_pod_hidden(pid) and not is_admin():
            return jsonify({"ok":False,"error":"Pod not found"}),404
```

with:

```python
        # Non-admin can only act on pods assigned to their own project.
        # Pods with no assignment or assigned to another project are invisible
        # (404) to keep existence private — even showing a different error
        # leaks info about admin-only pods.
        if not is_admin():
            a = get_pod_assignment(pid)
            if a is None or a["assigned_project"] != proj:
                return jsonify({"ok":False,"error":"Pod not found"}),404
```

- [ ] **Step 2: Update `api_start` the same way**

Replace the same `is_pod_hidden` check in `api_start` at `runpod_manager.py:1338` with the identical block (including the `get_pod_assignment` call).

- [ ] **Step 3: Rebuild and verify**

```bash
docker compose up -d --build
```

Test: admin creates a pod, hides it (assigns project=null), regular user can't see or delete it.

Since creating real pods costs money, exercise via in-container SQL and direct calls:

```bash
# Insert a fake pod assignment (no real pod backing this ID)
docker compose exec runpod-manager python3 -c "
import runpod_manager as rm
rm.upsert_pod_assignment('fake_dv_1','DV',True,'user','test')"

# Login as CV user
curl -c /tmp/uc.txt -X POST http://localhost:5001/api/user/register \
  -H "Content-Type: application/json" -d '{"nickname":"cvguy","project":"CV"}'

# Try deleting DV's pod as CV — should 404
curl -b /tmp/uc.txt -X DELETE http://localhost:5001/api/pods/fake_dv_1
# Expected: HTTP 404 {"ok":false,"error":"Pod not found"}

# Cleanup
docker compose exec runpod-manager python3 -c "
import runpod_manager as rm
rm.delete_pod_assignment('fake_dv_1')"
```

- [ ] **Step 4: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: api_del and api_start enforce project-scoped visibility

Non-admin users get 404 for any pod not assigned to their own project.
Replaces the previous hidden-pod-only guard."
```

---

## Task 7: /api/admin/pods/<pid>/assign + remove /hide and /unhide

**Goal:** introduce the single-endpoint assignment API. Delete the two old hide/unhide routes.

**Files:**
- Modify: `runpod_manager.py:1396–1439` (remove `api_admin_pod_hide` + `api_admin_pod_unhide`, add `api_admin_pod_assign`)

- [ ] **Step 1: Delete the two old routes**

Delete these two functions entirely (the block from `@app.route("/api/admin/pods/<pid>/hide"...)` down through the end of `api_admin_pod_unhide`).

- [ ] **Step 2: Add the new `/assign` route in the same spot**

Paste in:

```python
@app.route("/api/admin/pods/<pid>/assign", methods=["POST"])
@require_admin
def api_admin_pod_assign(pid):
    """Assign or reassign a pod to a project, or leave unassigned (admin-only
    visibility). Body: {"project": "CV"|"DV"|...|null, "counts_toward_quota": bool}.
    Works on any pod that exists in RunPod's listing — including ones created
    outside this manager (creation_source='external' on first assign)."""
    try:
        data = request.get_json(silent=True) or {}
        ap = data.get("project")
        if ap is not None and ap not in PROJECTS:
            return jsonify({"ok":False,"error":"Unknown project"}), 400
        cf = bool(data.get("counts_toward_quota", False))

        # Resolve the caller's nickname/project for the audit log.
        nick, proj = get_session_user()
        if not nick:
            nick, proj = "ADMIN", "[SYSTEM]"

        # Resolve pod name for the log entry.
        pods = list_pods()
        pname = next((p["name"] for p in pods if p["id"] == pid), pid)

        # creation_source: if the pod has no prior pod_assignment, we compute it.
        # If it does, upsert_pod_assignment preserves whatever was there.
        existing = get_pod_assignment(pid)
        if existing:
            src = existing["creation_source"]  # preserved
        else:
            src = determine_creation_source_for_unknown(pid)

        upsert_pod_assignment(pid, ap, cf, src, nick)
        log_action(nick, proj, "assign", pname, pid)
        return jsonify({"ok": True, "assignedProject": ap,
                        "countsTowardQuota": cf, "creationSource": src})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
```

- [ ] **Step 3: Rebuild and smoke-test the endpoint**

```bash
docker compose up -d --build
```

```bash
# Login as admin
curl -c /tmp/ac.txt -X POST http://localhost:5001/api/admin/login \
  -H "Content-Type: application/json" -d '{"password":"admin"}'

# Register admin as user (needed for session.user_project used in log_action)
curl -b /tmp/ac.txt -c /tmp/ac.txt -X POST http://localhost:5001/api/user/register \
  -H "Content-Type: application/json" -d '{"nickname":"admin","project":"ADMIN"}'

# Assign a fake pod ID to CV
curl -b /tmp/ac.txt -X POST http://localhost:5001/api/admin/pods/test_external_1/assign \
  -H "Content-Type: application/json" \
  -d '{"project":"CV","counts_toward_quota":false}'
# Expected: {"ok":true,"assignedProject":"CV","countsTowardQuota":false,"creationSource":"external"}
# (external because there's no pod_actions.create for this ID)

# Re-assign to DV — source should stay 'external'
curl -b /tmp/ac.txt -X POST http://localhost:5001/api/admin/pods/test_external_1/assign \
  -H "Content-Type: application/json" \
  -d '{"project":"DV","counts_toward_quota":true}'
# Expected: creationSource still "external"

# Unassign (admin-only visibility)
curl -b /tmp/ac.txt -X POST http://localhost:5001/api/admin/pods/test_external_1/assign \
  -H "Content-Type: application/json" \
  -d '{"project":null,"counts_toward_quota":false}'
# Expected: assignedProject: null

# Verify activity log has 3 'assign' rows
curl -b /tmp/ac.txt "http://localhost:5001/api/admin/activity" | python -m json.tool | grep -c '"action": "assign"'
# Expected: 3

# Cleanup
docker compose exec runpod-manager python3 -c "
import runpod_manager as rm
rm.delete_pod_assignment('test_external_1')"
```

- [ ] **Step 4: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: unified /api/admin/pods/<pid>/assign replaces /hide and /unhide

POST with {project, counts_toward_quota}. Works for any pod including
external ones (creation_source='external' computed on first assign).
Subsequent assigns preserve creation_source. Each call logs action='assign'."
```

---

## Task 8: Frontend — admin settings UI with per-project quota grid

**Goal:** replace the "Max pods" single input with a grid of 8 number inputs (one per project). The save function now POSTs `project_quotas` dict instead of `max_pods`.

**Files:**
- Modify: `runpod_manager.py:1809` (HTML for max_pods input) — inside `loadAdminPanel()` JS
- Modify: the corresponding save handler (search for `sMax` usage)

- [ ] **Step 1: Find the `sbSave` function to see how settings are POSTed**

Search: `grep -n "sbSave\|function.*sbSave\|sMax" runpod_manager.py`

You'll find a function like `async function sbSave()` that reads inputs and POSTs to `/api/admin/settings`.

- [ ] **Step 2: Replace the "Limits" section in `loadAdminPanel`**

Find at `runpod_manager.py:1809`:

```javascript
      '<div class="sb-section"><h3>Limits</h3><div class="fr"><label>Max pods</label><input type="number" id="sMax" min="1" max="50" value="'+s.max_pods+'"></div></div>'+
```

Replace with a grid generation helper. Add a helper function near the top of the frontend JS block, and update the Limits section:

```javascript
      '<div class="sb-section"><h3>Per-project quotas</h3>'+
        '<div class="quota-grid">'+
          Object.keys(s.project_quotas).map(p=>
            '<div class="fr"><label>'+p+'</label><input type="number" class="qInput" data-proj="'+p+'" min="0" max="50" value="'+s.project_quotas[p]+'"></div>'
          ).join('')+
        '</div>'+
        '<div class="sb-dim">Лимит одновременно запущенных подов на каждый проект. Админ обходит лимит.</div>'+
      '</div>'+
```

Add supporting CSS near the other `.sb-section` CSS rules (search for `.sb-section{` to find the block — typically around line 1580):

```css
.quota-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px}
.quota-grid .fr{margin:0}
```

- [ ] **Step 3: Update `sbSave` to send project_quotas**

Find the `sbSave` function. It currently reads `$('sMax').value`. Replace that read with:

```javascript
  const quotas = {};
  document.querySelectorAll('.qInput').forEach(el=>{
    quotas[el.dataset.proj] = parseInt(el.value,10) || 0;
  });
```

And in the POST body, replace `max_pods: parseInt($('sMax').value,10)` with:

```javascript
  project_quotas: quotas,
```

(Keep all other fields like `auto_delete_enabled`, `idle_timeout_minutes`, etc. intact.)

- [ ] **Step 4: Rebuild and test in browser**

```bash
docker compose up -d --build
```

Open `http://localhost:5001`, register as any user, then open admin panel (password `admin`). Expected:
- "Per-project quotas" section shows 8 inputs (CV, DV, MT, PT, MARK, ADMIN, TV, MW) each at 4.
- Change some values, click Save. Refresh the panel — new values appear.
- Inspect settings: `curl -b /tmp/ac.txt http://localhost:5001/api/admin/settings` shows the saved dict.

- [ ] **Step 5: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: per-project quota grid in admin settings UI

Replaces the single Max-pods input with 8 per-project number inputs.
sbSave POSTs project_quotas dict. Matches the backend accepted schema."
```

---

## Task 9: Frontend — create pod form with admin dropdown + checkbox

**Goal:** regular users get the existing single "Create" button (unchanged). Admin sees a small dropdown (9 options: 8 projects + "Не назначать") + a checkbox "Считать в квоту" right next to the button. On click, the admin-only fields go in the POST body.

**Files:**
- Modify: the HTML rendering of the "New pod" button (search for the string that creates a pod — likely `createPod()` or a form)
- Modify: the `createPod()` JS function

- [ ] **Step 1: Find the create-pod UI**

```bash
grep -n "createPod\|New pod\|Новый под\|onclick.*create" runpod_manager.py | head -20
```

Look for the button HTML and the JS handler that POSTs to `/api/pods`.

- [ ] **Step 2: Extend `createPod()` to read admin inputs**

Modify the `createPod()` JS function. Current form (approx):

```javascript
async function createPod(){
  const r=await aok('/api/pods',{method:'POST'});
  ...
}
```

Change to:

```javascript
async function createPod(){
  let body = {};
  if(isAdmin){
    const ap = $('adminAssignProject') ? $('adminAssignProject').value : '';
    const cf = $('adminCountsFlag') ? $('adminCountsFlag').checked : false;
    if(ap === '__null__') body.assigned_project = null;
    else if(ap) body.assigned_project = ap;
    body.counts_toward_quota = cf;
  }
  const r=await aok('/api/pods',{method:'POST',body:JSON.stringify(body),headers:{'Content-Type':'application/json'}});
  ...  // rest of the function unchanged
}
```

- [ ] **Step 3: Render the admin inputs**

Locate where the "+ New pod" button is rendered (part of the pod list header area, likely in the main `render()` function). Add admin-only inputs next to the button:

```javascript
const adminCreateControls = isAdmin ? (
  '<select id="adminAssignProject" style="margin-left:8px">'+
    '<option value="">Мой проект (ADMIN)</option>'+
    '<option value="__null__">Не назначать (только админ)</option>'+
    PROJECTS.map(p=>'<option value="'+p+'">'+p+'</option>').join('')+
  '</select>'+
  '<label style="margin-left:8px;font-size:12px"><input type="checkbox" id="adminCountsFlag"> считать в квоту</label>'
) : '';
```

Inject `adminCreateControls` into the HTML wherever the "+ New pod" button lives. Note: `PROJECTS` must be available on the JS side. Add it as a global at the top of the JS section (search for `let isAdmin=false`):

```javascript
const PROJECTS = ['CV','DV','MT','PT','MARK','ADMIN','TV','MW'];
```

- [ ] **Step 4: Rebuild and test**

```bash
docker compose up -d --build
```

Open browser as admin. The "+ New pod" button should now have a dropdown + checkbox. Select a project and click create — open the Network tab in F12 and verify the POST body contains `{"assigned_project":"CV","counts_toward_quota":false}` (or similar).

- [ ] **Step 5: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: admin-only dropdown + counts flag next to Create pod

Regular users unchanged. Admins pick the target project (or 'unassigned')
and the counts-toward-quota flag before creating. Values are POSTed
to /api/pods as assigned_project + counts_toward_quota."
```

---

## Task 10: Frontend — pod card badges

**Goal:** each pod card shows bad visible for the viewer: project tag + admin_created badge + external badge + unassigned badge + not-counting mini-badge (admin-only for the last two).

**Files:**
- Modify: `runpod_manager.py:~2400–2470` — the pod card rendering in the `render()` function

- [ ] **Step 1: Find the card rendering**

```bash
grep -n "cardCls\|hidden-pod\|p\.id.*p\.name" runpod_manager.py | head -20
```

The relevant rendering is around lines 2400–2470 (depending on prior edits). Look for the loop over pods that builds `<div class="pc">...</div>` cards.

- [ ] **Step 2: Remove the old hidden-pod visual treatment and add new badges**

Find the line that sets `cardCls` (currently refers to `p.hidden`):

```javascript
const cardCls='pc'+(p.hidden===true?' hidden-pod':'');
```

Replace with:

```javascript
const isUnassigned = p.assignedProject === null;
const cardCls = 'pc'
  + (isUnassigned ? ' hidden-pod' : '')  // reuse existing yellow-border style
  + (p.creationSource === 'admin' ? ' pc-admin-created' : '')
  + (p.creationSource === 'external' ? ' pc-external' : '');
```

Find the header/tags area of the pod card (usually just below the pod name). Add a small badge group:

```javascript
// Badge strip — always shown. User sees project tag + admin/external markers.
// Admin sees everything including 'unassigned' and 'not counting' mini-badges.
const badges = [];
if(p.assignedProject) badges.push('<span class="pbadge pb-proj">'+p.assignedProject+'</span>');
if(p.creationSource === 'admin') badges.push('<span class="pbadge pb-admin">🛡 admin created</span>');
if(p.creationSource === 'external') badges.push('<span class="pbadge pb-ext">🌐 external</span>');
if(isAdmin && isUnassigned) badges.push('<span class="pbadge pb-unassigned">👁 unassigned</span>');
if(isAdmin && p.countsTowardQuota === false && p.assignedProject) badges.push('<span class="pbadge pb-nocount" title="не учитывается в квоте">∞</span>');
const badgeHtml = badges.length ? '<div class="pbadges">'+badges.join('')+'</div>' : '';
```

Inject `badgeHtml` into the card template right after the pod name. Exact placement depends on the existing structure — put it where `hidden`-marker used to appear (search for the existing `isH` / eye-icon logic and replace it).

- [ ] **Step 3: Add badge CSS**

Near the other `.pc` CSS rules (search for `.pc{` to find the block):

```css
.pbadges{display:flex;gap:4px;flex-wrap:wrap;margin:4px 0}
.pbadge{display:inline-block;padding:2px 6px;border-radius:10px;font-size:10px;font-weight:600;line-height:1.2}
.pb-proj{background:#2a4a7a;color:#fff}
.pb-admin{background:#4a3a7a;color:#fff}
.pb-ext{background:#7a4a2a;color:#fff}
.pb-unassigned{background:#7a7a2a;color:#fff}
.pb-nocount{background:#555;color:#fff;padding:2px 5px}
.pc-admin-created{border-left:3px solid #7a4a7a}
.pc-external{border-left:3px solid #7a5a2a}
```

(Keep existing `.hidden-pod` CSS — still used for unassigned visual treatment.)

- [ ] **Step 4: Rebuild and test**

```bash
docker compose up -d --build
```

Open as admin. Insert test rows for visible mockup:

```bash
docker compose exec runpod-manager python3 -c "
import runpod_manager as rm
# Simulate creation histories so list_pods attachment works visually in tests
# (You need actual running RunPod pods to see them; this is for doc/demo purposes)
print('Use real pods with different creation_source values to verify visually')"
```

Best approach: create one real admin-assigned pod (counts=false), verify its card shows `CV` + `admin created` + `∞` badges. Test assign flow (next task) to see `external` badge when assigning a pod without `pod_actions.create`.

- [ ] **Step 5: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: pod card badges for assigned_project / creation_source / counts_flag

Replaces the hidden-pod eye icon with a badge strip visible to the
appropriate audience (CV/DV/... visible to all; admin_created / external
visible to all; unassigned / not-counting visible only to admin)."
```

---

## Task 11: Frontend — assign modal (replaces hide/unhide buttons)

**Goal:** remove the 👁 hide/show toggle button from pod cards. Replace with an "Назначить" button that opens a small modal (dropdown + checkbox). Submit → POST `/api/admin/pods/<pid>/assign`.

**Files:**
- Modify: `runpod_manager.py` — pod card action buttons + `togglePodVisibility` JS function

- [ ] **Step 1: Find and remove the old hide/show button**

```bash
grep -n "togglePodVisibility\|👁" runpod_manager.py
```

Find the button markup inside the card and its handler `togglePodVisibility(pid, hide)`. Delete both (the button HTML and the whole function).

- [ ] **Step 2: Add the new "Назначить" button (admin only)**

In the card action bar (next to ✕ Delete), add:

```javascript
const adminAssignBtn = isAdmin
  ? '<button class="btn bs" title="Назначить проекту" onclick="openAssignModal(\''+p.id+'\',\''+(p.assignedProject||'')+'\','+(p.countsTowardQuota?'true':'false')+')">Назначить</button>'
  : '';
```

Splice `adminAssignBtn` into the card button row.

- [ ] **Step 3: Add the modal HTML (once, at the bottom of `FRONTEND_HTML`)**

Find the closing `</body>` or wherever the main markup ends in `FRONTEND_HTML`. Before that, insert:

```html
<div id="assignModal" class="modal" style="display:none">
  <div class="modal-body">
    <h3>Назначить под проекту</h3>
    <div class="fr"><label>Проект</label>
      <select id="assignProj">
        <option value="__null__">Не назначать (только админ видит)</option>
      </select>
    </div>
    <div class="fr"><label><input type="checkbox" id="assignCounts"> Считать в квоту проекта</label></div>
    <div class="da" style="margin-top:12px">
      <button class="btn" onclick="closeAssignModal()">Отмена</button>
      <button class="btn bs bp" onclick="submitAssign()">Сохранить</button>
    </div>
  </div>
</div>
```

Add CSS:

```css
.modal{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;display:flex;align-items:center;justify-content:center}
.modal-body{background:var(--bg);padding:20px;border-radius:8px;min-width:320px;max-width:400px}
.modal-body h3{margin:0 0 12px 0}
```

- [ ] **Step 4: Add modal JS**

```javascript
let _assignPid = null;
function openAssignModal(pid, currentProject, currentCounts){
  _assignPid = pid;
  const sel = $('assignProj');
  // Populate options (first item '__null__' is hardcoded in HTML)
  sel.innerHTML = '<option value="__null__">Не назначать (только админ видит)</option>'+
    PROJECTS.map(p=>'<option value="'+p+'">'+p+'</option>').join('');
  sel.value = currentProject ? currentProject : '__null__';
  $('assignCounts').checked = !!currentCounts;
  $('assignModal').style.display = 'flex';
}
function closeAssignModal(){
  $('assignModal').style.display = 'none';
  _assignPid = null;
}
async function submitAssign(){
  if(!_assignPid) return;
  const pv = $('assignProj').value;
  const project = pv === '__null__' ? null : pv;
  const counts_toward_quota = $('assignCounts').checked;
  try{
    await aok('/api/admin/pods/'+_assignPid+'/assign', {
      method:'POST',
      body: JSON.stringify({project, counts_toward_quota}),
      headers:{'Content-Type':'application/json'}
    });
    closeAssignModal();
    refreshPods();
    refreshActivityLog();
  }catch(e){
    alert('Failed: '+e.message);
  }
}
```

- [ ] **Step 5: Rebuild and test**

```bash
docker compose up -d --build
```

Open as admin. For any pod:
- Click "Назначить" → modal appears with current project and counts flag preselected.
- Change project to DV, tick "считать в квоту", click Save.
- Pod card re-renders with new badge; activity log shows new "assign" row.

- [ ] **Step 6: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: assign modal replaces hide/unhide buttons

Admin clicks 'Назначить' on any pod to open a small modal with a
project dropdown and counts-toward-quota checkbox. Submits to
/api/admin/pods/<pid>/assign and refreshes the list + activity log."
```

---

## Task 12: Frontend — activity log date format DD.MM.YYYY HH:MM

**Goal:** `formatLocalFull()` currently returns `YYYY-MM-DD HH:MM:SS`. Change to `DD.MM.YYYY HH:MM`. Sort is already DESC in SQL (no change needed).

**Files:**
- Modify: `runpod_manager.py:2188–2202` (`formatLocalFull` function)

- [ ] **Step 1: Replace the function body**

Find `formatLocalFull` at `runpod_manager.py:2188`. Replace with:

```javascript
// Format an ISO timestamp as DD.MM.YYYY HH:MM in the viewer's local timezone.
// Used by the activity log and the pod tech panel for "last event" / creation stamps.
function formatLocalFull(ts){
  if(!ts)return'';
  let raw=ts.trim();
  if(!raw.includes('T')&&raw.includes(' '))raw=raw.replace(' ','T');
  if(!/[Zz]|[+-]\d{2}:?\d{2}$/.test(raw))raw+='Z';
  const d=new Date(raw);
  if(isNaN(d))return ts;
  const yyyy=d.getFullYear();
  const mo=String(d.getMonth()+1).padStart(2,'0');
  const dd=String(d.getDate()).padStart(2,'0');
  const hh=String(d.getHours()).padStart(2,'0');
  const mm=String(d.getMinutes()).padStart(2,'0');
  return dd+'.'+mo+'.'+yyyy+' '+hh+':'+mm;
}
```

- [ ] **Step 2: Rebuild and visually check**

```bash
docker compose up -d --build
```

Open as admin. Open admin panel → Activity log should now show rows like `21.04.2026 14:32`. Sort: newest on top (already correct from SQL ORDER BY).

- [ ] **Step 3: Commit**

```bash
git add runpod_manager.py
git commit -m "feat: activity log date format DD.MM.YYYY HH:MM (local tz)

formatLocalFull now emits the Russian-convention format requested.
Sort is already DESC in the SQL side, no JS sort changes needed."
```

---

## Task 13: Update documentation

**Goal:** reflect the new schema and endpoints in `docs/`.

**Files:**
- Modify: `docs/database.md` — replace `pod_hidden` section with `pod_assignment`
- Modify: `docs/admin-panel.md` — update endpoint table + settings semantics
- Modify: `docs/architecture.md` — update section map (add migration + helper renames)

- [ ] **Step 1: Update `docs/database.md`**

Find the `pod_hidden` section and replace its DDL + description with pod_assignment's. Add description of `creation_source` values. Mention that `pod_hidden` is dropped after migration (with the migration source pointer).

- [ ] **Step 2: Update `docs/admin-panel.md`**

In the endpoints table:
- Remove `/api/admin/pods/<pid>/hide` and `/unhide` rows.
- Add `/api/admin/pods/<pid>/assign` row with body shape.

In the "Настройки админки" section, remove the `max_pods` row and add `project_quotas` (dict, default 4 per project).

In the "Hidden pods" section, replace with an "Assignment" section describing the new model (assigned_project, creation_source, counts_toward_quota).

- [ ] **Step 3: Update `docs/architecture.md`**

In the "Карта секций" table, update:
- `SQLite слой` row: "pod_timers (init/touch/delete/get_all), pod_assignment (upsert/get/get_batch/delete/determine_source), migrate_to_pod_assignment()"
- Add the migration step to the "Главная точка входа" section: "init_db() also runs migrate_to_pod_assignment() once at startup (idempotent; drops pod_hidden after copying its rows)".

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "docs: update database / admin-panel / architecture for per-project quotas

- database.md: pod_assignment DDL and semantics replace pod_hidden.
- admin-panel.md: /assign replaces /hide+/unhide; project_quotas replaces max_pods.
- architecture.md: updated section map and init_db startup order."
```

---

## Final verification

- [ ] **Step 1: Run unit test once more**

```bash
python -m unittest tests.test_migration -v
```

Expected: 2 tests pass.

- [ ] **Step 2: End-to-end flow (manual)**

```bash
docker compose down  # wipe manager state if desired
docker compose up -d --build
```

In browser:
1. Register two users in different projects (`alice` in `CV`, `bob` in `DV`). Each sees empty pod list.
2. As admin (password `admin`), set `CV=2`, `DV=1`. Save.
3. As `alice`, create 2 pods in CV. Third attempt blocked with `"Достигнут лимит CV: 2/2"`.
4. As `bob`, create 1 pod in DV. Second blocked.
5. As admin (dropdown: DV, counts unchecked), create a pod. Verify: bob sees 2 DV pods total (his + admin's), but quota still reads 1/1 (admin's doesn't count). `admin created` badge visible.
6. As admin, open an existing CV pod, click "Назначить", change to DV + tick "считать в квоту". Save. Verify: alice no longer sees that pod; bob does; DV now over-quota (2/1).
7. Activity log shows 3 assigns, 3 creates, all in DD.MM.YYYY HH:MM format, newest first.

- [ ] **Step 3: Restore admin test state**

If you changed CV/DV quotas to small numbers for testing, restore to 4 each before finishing.

- [ ] **Step 4: Final commit (if any stragglers)**

```bash
git status
git log --oneline -20
```

Expected: ~13 commits total for this feature, clean working tree.

---

## Out-of-scope reminders

Per spec section **Что не трогаем**:
- `auto_delete_*` stays global.
- `idle_timeout_*` stays global.
- `pod_window_*` stays global.
- Admin auth + cleartext password in JSON unchanged.
- `PRESET` unchanged.
- GraphQL deploy flow unchanged.

If any task invites scope creep (e.g., "while I'm touching settings, might as well hash the password"), resist — file a TODO and stay on spec.
