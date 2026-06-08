# Pod-launch auto-retry («заявка на под») Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When pod creation fails because no GPU instances are available, let the user leave a "заявка" that a background worker retries every N seconds until a GPU frees up or an admin-configured timeout elapses.

**Architecture:** New SQLite table `pod_request` (DB-backed, survives restart) + a single daemon worker thread (`pod_request_loop`) alongside the existing `scheduler_loop`. A typed `GpuUnavailableError` distinguishes the retryable "no instances" condition. Requests reserve a quota slot at creation and show as placeholder cards in the existing pod list.

**Tech Stack:** Python 3 / Flask monolith (`runpod_manager.py`), SQLite (stdlib `sqlite3`), inline HTML/JS SPA, `unittest` (tests in `tests/`).

**Spec:** `docs/superpowers/specs/2026-06-08-pod-launch-autoretry-design.md`

**Conventions to follow (from existing code):**
- Timestamps: `now_iso()` (UTC ISO 8601 'Z'); parse with `parse_iso()`.
- `time` is imported as `_time` — use `_time.sleep(...)`.
- DB helpers open their own `sqlite3.connect(str(DB_PATH))` and close in `finally`.
- Tests redirect `rm.DB_PATH` to a temp file (see `tests/test_migration.py`).
- Run a single test: `python -m unittest tests.test_pod_request -v`

---

## Task 1: `pod_request` table in `init_db()`

**Files:**
- Modify: `runpod_manager.py` (the `executescript` block inside `init_db()`, ~line 274-298)
- Test: `tests/test_pod_request.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_pod_request.py`:

```python
"""Tests for pod-launch auto-retry (pod_request table + worker + helpers).
Runs against a temp SQLite DB, no Docker required.
Run: python -m unittest tests.test_pod_request -v
"""
import os
import sys
import tempfile
import sqlite3
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import runpod_manager as rm


class PodRequestDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self._orig_db_path = rm.DB_PATH
        rm.DB_PATH = Path(self.db_path)
        rm.init_db()

    def tearDown(self):
        rm.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_pod_request_table_exists(self):
        db = sqlite3.connect(self.db_path)
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pod_request'")
        self.assertIsNotNone(cur.fetchone())
        # Index exists too
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_pr_status'")
        self.assertIsNotNone(cur.fetchone())
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.PodRequestDBTest.test_pod_request_table_exists -v`
Expected: FAIL — no table `pod_request`.

- [ ] **Step 3: Add the table to `init_db()`**

In `runpod_manager.py`, inside the `db.executescript("""...""")` call in `init_db()`, append after the `pod_assignment` table definition (before the closing `"""`):

```sql
        CREATE TABLE IF NOT EXISTS pod_request (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pod_name            TEXT NOT NULL,
            assigned_project    TEXT,
            counts_toward_quota INTEGER NOT NULL DEFAULT 1,
            creation_source     TEXT NOT NULL DEFAULT 'user',
            requested_by        TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            created_at          TEXT NOT NULL,
            last_attempt_at     TEXT,
            last_error          TEXT,
            pod_id              TEXT,
            finished_at         TEXT);
        CREATE INDEX IF NOT EXISTS idx_pr_status ON pod_request(status);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_request.PodRequestDBTest.test_pod_request_table_exists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): add pod_request table"
```

---

## Task 2: `pod_request` CRUD helpers

**Files:**
- Modify: `runpod_manager.py` — add a new helper block after `get_assignments_batch` (~line 560, before `check_pod_window`)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pod_request.py` (inside `PodRequestDBTest`):

```python
    def test_crud_helpers(self):
        rid = rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        self.assertIsInstance(rid, int)
        # list_pending_requests returns it
        pend = rm.list_pending_requests()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["pod_name"], "cv_pod_1")
        self.assertEqual(pend[0]["status"], "pending")
        # get_pod_request
        row = rm.get_pod_request(rid)
        self.assertEqual(row["requested_by"], "alice")
        # update_pod_request
        rm.update_pod_request(rid, status="fulfilled", pod_id="p_abc")
        self.assertEqual(rm.get_pod_request(rid)["status"], "fulfilled")
        # fulfilled no longer pending
        self.assertEqual(rm.list_pending_requests(), [])
        # pending_request_names reflects only pending
        rm.create_pod_request("cv_pod_2", "CV", True, "user", "bob")
        self.assertEqual(rm.pending_request_names(), ["cv_pod_2"])
        # delete
        last = rm.list_pending_requests()[0]["id"]
        rm.delete_pod_request(last)
        self.assertEqual(rm.list_pending_requests(), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.PodRequestDBTest.test_crud_helpers -v`
Expected: FAIL — `AttributeError: module 'runpod_manager' has no attribute 'create_pod_request'`.

- [ ] **Step 3: Add the helpers**

Insert into `runpod_manager.py` after `get_assignments_batch` (just before `def check_pod_window`):

```python
# ----- Pod requests (auto-retry "заявка на под") -----

def create_pod_request(pod_name, assigned_project, counts_toward_quota,
                       creation_source, requested_by):
    """Insert a new pending pod_request. Returns the new row id."""
    db = sqlite3.connect(str(DB_PATH))
    try:
        cur = db.execute("""INSERT INTO pod_request
            (pod_name, assigned_project, counts_toward_quota, creation_source,
             requested_by, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (pod_name, assigned_project, 1 if counts_toward_quota else 0,
             creation_source, requested_by, now_iso()))
        db.commit()
        return cur.lastrowid
    finally:
        db.close()

def list_pending_requests():
    """All pod_request rows with status='pending', oldest first, as dicts."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT * FROM pod_request WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

def list_visible_requests(project=None, viewer_is_admin=False):
    """Requests to render as cards: statuses pending/timed_out/failed.
    Admin sees all; a regular user sees only their own project's requests."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        if viewer_is_admin:
            rows = db.execute(
                """SELECT * FROM pod_request
                   WHERE status IN ('pending','timed_out','failed')
                   ORDER BY created_at"""
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT * FROM pod_request
                   WHERE status IN ('pending','timed_out','failed')
                   AND assigned_project=? ORDER BY created_at""",
                (project,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

def get_pod_request(req_id):
    """Single pod_request row as dict, or None."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        r = db.execute("SELECT * FROM pod_request WHERE id=?", (req_id,)).fetchone()
        return dict(r) if r else None
    finally:
        db.close()

def update_pod_request(req_id, **fields):
    """Update the given columns on a pod_request row. No-op if fields empty."""
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [req_id]
    db = sqlite3.connect(str(DB_PATH))
    try:
        db.execute(f"UPDATE pod_request SET {cols} WHERE id=?", vals)
        db.commit()
    finally:
        db.close()

def delete_pod_request(req_id):
    """Physically remove a pod_request row (used by 'Закрыть' on terminal cards)."""
    db = sqlite3.connect(str(DB_PATH))
    try:
        db.execute("DELETE FROM pod_request WHERE id=?", (req_id,))
        db.commit()
    finally:
        db.close()

def pending_request_names():
    """Names reserved by pending requests — feed into next_name() so a request
    and a real pod (or two requests) never collide on a name."""
    return [r["pod_name"] for r in list_pending_requests()]

def count_pending_quota(project):
    """Number of pending requests for `project` that count toward its quota."""
    db = sqlite3.connect(str(DB_PATH))
    try:
        if project is None:
            row = db.execute(
                """SELECT COUNT(*) FROM pod_request
                   WHERE status='pending' AND counts_toward_quota=1
                   AND assigned_project IS NULL""").fetchone()
        else:
            row = db.execute(
                """SELECT COUNT(*) FROM pod_request
                   WHERE status='pending' AND counts_toward_quota=1
                   AND assigned_project=?""", (project,)).fetchone()
        return row[0]
    finally:
        db.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_request.PodRequestDBTest.test_crud_helpers -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): pod_request CRUD helpers"
```

---

## Task 3: `GpuUnavailableError` + `is_gpu_unavailable_error`

**Files:**
- Modify: `runpod_manager.py` — add near the top of the GraphQL deploy section, just before `DEPLOY_MUTATION` (~line 1083)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_pod_request.py` (no DB needed):

```python
class GpuUnavailableDetectTest(unittest.TestCase):
    def test_matches_runpod_phrases(self):
        self.assertTrue(rm.is_gpu_unavailable_error(
            "There are no longer any instances available with the requested "
            "specifications. Please refresh and try again."))
        self.assertTrue(rm.is_gpu_unavailable_error("no resources"))
        self.assertTrue(rm.is_gpu_unavailable_error("NO RESOURCES currently"))

    def test_rejects_other_errors(self):
        self.assertFalse(rm.is_gpu_unavailable_error("invalid api key"))
        self.assertFalse(rm.is_gpu_unavailable_error("template not found"))
        self.assertFalse(rm.is_gpu_unavailable_error(""))
        self.assertFalse(rm.is_gpu_unavailable_error(None))

    def test_error_is_runtimeerror_subclass(self):
        self.assertTrue(issubclass(rm.GpuUnavailableError, RuntimeError))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.GpuUnavailableDetectTest -v`
Expected: FAIL — `AttributeError: ... 'is_gpu_unavailable_error'`.

- [ ] **Step 3: Add the helper and exception class**

In `runpod_manager.py`, immediately before `DEPLOY_MUTATION = """..."""`:

```python
# A GPU "no instances available" deploy failure is RETRYABLE (the GPU may free
# up later) and is what the auto-retry "заявка" feature waits on. We give it a
# dedicated exception type so create_pod() can skip the pointless CLI fallback
# (the CLI also fails on scarcity) and so api_pods_post can offer the user a
# friendly "leave a request?" dialog instead of a scary raw error.
_GPU_UNAVAILABLE_PHRASES = (
    "no longer any instances available",
    "instances available with the requested",
    "no resources",
)

def is_gpu_unavailable_error(msg):
    """True if `msg` looks like RunPod's 'no GPU instances available' error."""
    if not msg:
        return False
    m = str(msg).lower()
    return any(p in m for p in _GPU_UNAVAILABLE_PHRASES)

class GpuUnavailableError(RuntimeError):
    """Deploy failed specifically because no GPU instances are currently
    available — a retryable condition, not a permanent error."""
    pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_request.GpuUnavailableDetectTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): GpuUnavailableError + detection helper"
```

---

## Task 4: Raise `GpuUnavailableError` from the deploy paths

**Files:**
- Modify: `runpod_manager.py` — `create_pod_via_graphql` GraphQL-errors branch (~line 1158-1166), and `create_pod` GraphQL try/except + CLI error branch (~line 1210-1241)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_pod_request.py`:

```python
from unittest import mock


class CreatePodErrorRoutingTest(unittest.TestCase):
    def setUp(self):
        self._orig_key = rm._api_key
        rm._api_key = "test-key"

    def tearDown(self):
        rm._api_key = self._orig_key

    def test_graphql_gpu_unavailable_does_not_fall_back_to_cli(self):
        # GraphQL raises GpuUnavailableError → create_pod must re-raise it,
        # NOT call the CLI fallback.
        with mock.patch.object(rm, "create_pod_via_graphql",
                               side_effect=rm.GpuUnavailableError("GraphQL: no resources")), \
             mock.patch.object(rm, "run_cmd") as run_cmd:
            with self.assertRaises(rm.GpuUnavailableError):
                rm.create_pod("cv_pod_1", bypass_window=True)
            run_cmd.assert_not_called()

    def test_graphql_other_error_falls_back_to_cli(self):
        with mock.patch.object(rm, "create_pod_via_graphql",
                               side_effect=RuntimeError("GraphQL: bad template")), \
             mock.patch.object(rm, "run_cmd",
                               return_value={"ok": True, "data": {"id": "p1", "name": "cv_pod_1"}}) as run_cmd:
            result = rm.create_pod("cv_pod_1", bypass_window=True)
            run_cmd.assert_called_once()
            self.assertEqual(result.get("id"), "p1")
```

Note: `bypass_window=True` skips the window + quota checks so the test stays focused on error routing.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.CreatePodErrorRoutingTest -v`
Expected: FAIL — currently `GpuUnavailableError` isn't raised (plain RuntimeError) and create_pod falls back to CLI on every GraphQL failure.

- [ ] **Step 3: Wire the exception into both paths**

In `create_pod_via_graphql`, replace the GraphQL-errors block:

```python
    # GraphQL-level errors come back inside the response body even on HTTP 200
    if isinstance(data, dict) and data.get("errors"):
        msgs = []
        for err in data["errors"]:
            if isinstance(err, dict):
                msgs.append(err.get("message", str(err)))
            else:
                msgs.append(str(err))
        joined = "; ".join(msgs)[:300]
        if is_gpu_unavailable_error(joined):
            raise GpuUnavailableError("GraphQL: " + joined)
        raise RuntimeError("GraphQL: " + joined)
```

In `create_pod`, change the GraphQL primary-path try/except:

```python
    if _api_key:
        try:
            log.info(f"Creating pod {name!r} via GraphQL DeployOnDemand mutation")
            return create_pod_via_graphql(name)
        except GpuUnavailableError:
            # No GPU available right now. The CLI path also fails on scarcity,
            # so don't bother — surface the retryable error to the caller.
            log.info(f"GraphQL deploy: no GPU available for {name!r}")
            raise
        except Exception as e:
            log.warning(f"GraphQL deploy failed for {name!r}: {e}. Falling back to CLI.")
            # fall through to CLI path below
```

In `create_pod`, change the CLI failure branch:

```python
    if not res["ok"]:
        # Log raw error for postmortem, show humanized version to user
        log.error(f"create_pod CLI fallback also failed, raw error: {res['error']}")
        if is_gpu_unavailable_error(res["error"]):
            raise GpuUnavailableError(humanize_cli_error(res["error"]))
        raise RuntimeError(humanize_cli_error(res["error"]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_request.CreatePodErrorRoutingTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): route GPU-unavailable to GpuUnavailableError, skip CLI fallback"
```

---

## Task 5: `project_quota_usage` helper (running + pending)

**Files:**
- Modify: `runpod_manager.py` — add helper after `count_pending_quota` (from Task 2); update the quota check inside `create_pod` (~line 1193-1203)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pod_request.py` (inside `PodRequestDBTest`):

```python
    def test_project_quota_usage_counts_running_and_pending(self):
        fake_pods = [
            {"desiredStatus": "RUNNING", "assignedProject": "CV", "countsTowardQuota": True},
            {"desiredStatus": "RUNNING", "assignedProject": "CV", "countsTowardQuota": False},
            {"desiredStatus": "EXITED",  "assignedProject": "CV", "countsTowardQuota": True},
            {"desiredStatus": "RUNNING", "assignedProject": "DV", "countsTowardQuota": True},
        ]
        # 1 running CV pod counts (the False and EXITED ones don't)
        self.assertEqual(rm.project_quota_usage("CV", pods=fake_pods), 1)
        # Add two pending CV requests, one not counting
        rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        rm.create_pod_request("cv_pod_2", "CV", False, "admin", "admin_joe")
        self.assertEqual(rm.project_quota_usage("CV", pods=fake_pods), 2)  # 1 running + 1 pending-counting
        self.assertEqual(rm.project_quota_usage("DV", pods=fake_pods), 1)  # 1 running, 0 pending
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.PodRequestDBTest.test_project_quota_usage_counts_running_and_pending -v`
Expected: FAIL — `AttributeError: ... 'project_quota_usage'`.

- [ ] **Step 3: Add the helper and use it in `create_pod`**

Add after `count_pending_quota` in `runpod_manager.py`:

```python
def project_quota_usage(project, pods=None):
    """Quota slots in use for `project`: RUNNING pods that count toward quota,
    plus pending requests that count toward quota. `pods` may be passed in to
    avoid a redundant list_pods() call."""
    if pods is None:
        pods = list_pods()
    running = sum(1 for p in pods
                  if p.get("desiredStatus") == "RUNNING"
                  and p.get("assignedProject") == project
                  and p.get("countsTowardQuota"))
    return running + count_pending_quota(project)
```

Replace the quota check inside `create_pod` (the `if not bypass_window:` quota block):

```python
    # Per-project quota. Admins bypass (bypass_window doubles as the admin-is-caller signal
    # — the caller wires it up as bypass_window=is_admin() at the route level).
    if not bypass_window:
        nick, proj = get_session_user()  # user is already authenticated via @require_user
        quotas = s.get("project_quotas") or {}
        quota = quotas.get(proj, DEFAULT_PROJECT_QUOTA)
        # Usage includes pending requests so a direct create can't exceed quota
        # while requests are queued.
        used = project_quota_usage(proj)
        if used >= quota:
            raise RuntimeError(f"Достигнут лимит {proj}: {used}/{quota}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_request.PodRequestDBTest.test_project_quota_usage_counts_running_and_pending -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): project_quota_usage counts pending requests"
```

---

## Task 6: `next_name` accounts for pending requests

**Files:**
- Modify: `runpod_manager.py` — `api_pods_post` name reservation (~line 1530)
- Test: `tests/test_pod_request.py`

`next_name(pods, project)` already scans `pods` for `{'name': ...}`. We feed it pending-request names as synthetic entries so a direct create and a queued request don't collide.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pod_request.py` (inside `PodRequestDBTest`):

```python
    def test_next_name_skips_pending_request_names(self):
        # One real pod named cv_pod_1, plus a pending request for cv_pod_2.
        real_pods = [{"name": "cv_pod_1"}]
        rm.create_pod_request("cv_pod_2", "CV", True, "user", "alice")
        combined = real_pods + [{"name": n} for n in rm.pending_request_names()]
        self.assertEqual(rm.next_name(combined, "CV"), "cv_pod_3")
```

- [ ] **Step 2: Run test to verify it fails... or passes**

Run: `python -m unittest tests.test_pod_request.PodRequestDBTest.test_next_name_skips_pending_request_names -v`
Expected: PASS already (this test documents that `next_name` works on a combined list — `next_name` itself is unchanged). The real change is in the endpoint (Task 7) where we MUST build that combined list. This test locks the contract the endpoint relies on.

- [ ] **Step 3: Modify `api_pods_post` to reserve names against pending requests too**

In `api_pods_post`, replace:

```python
        pods = list_pods()
        # Per-project pod naming: ap is either a project key or None (unassigned).
        # next_name scans only the pods with the matching prefix so each project
        # gets its own 1,2,3,... counter.
        name = next_name(pods, ap)
```

with:

```python
        pods = list_pods()
        # Reserve a name against BOTH real pods and pending requests so a direct
        # create and a queued "заявка" never collide on a name.
        reserved = pods + [{"name": n} for n in pending_request_names()]
        name = next_name(reserved, ap)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_request.PodRequestDBTest.test_next_name_skips_pending_request_names -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): reserve pod names against pending requests"
```

---

## Task 7: `api_pods_post` returns the `gpuUnavailable` signal

**Files:**
- Modify: `runpod_manager.py` — `api_pods_post` exception handling (~line 1548-1549)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add a Flask-client test class to `tests/test_pod_request.py`:

```python
class ApiPodsPostSignalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._orig_db_path = rm.DB_PATH
        rm.DB_PATH = Path(self.tmp.name)
        rm.init_db()
        rm.app.config["TESTING"] = True
        self.client = rm.app.test_client()

    def tearDown(self):
        rm.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["user_nickname"] = "alice"
            sess["user_project"] = "CV"

    def test_gpu_unavailable_returns_signal_not_500(self):
        self._login()
        with mock.patch.object(rm, "create_pod",
                               side_effect=rm.GpuUnavailableError("GraphQL: no resources")), \
             mock.patch.object(rm, "list_pods", return_value=[]):
            r = self.client.post("/api/pods", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["gpuUnavailable"])
```

(Confirmed: `get_session_user` reads `session["user_nickname"]` / `session["user_project"]`; `require_user` returns 403 if absent. The `_login()` helper above sets exactly those keys.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.ApiPodsPostSignalTest -v`
Expected: FAIL — currently returns HTTP 500 with no `gpuUnavailable` key.

- [ ] **Step 3: Add the specific exception handler**

In `api_pods_post`, change the trailing exception handling from:

```python
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500
```

to:

```python
    except GpuUnavailableError as e:
        # Not a hard error — the GPU may free up. Signal the frontend to offer
        # the user a "leave a request?" dialog instead of a scary red toast.
        return jsonify({"ok": False, "gpuUnavailable": True, "error": str(e)}), 200
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_request.ApiPodsPostSignalTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): api_pods_post signals gpuUnavailable instead of 500"
```

---

## Task 8: `POST /api/pod-requests` — create a заявка

**Files:**
- Modify: `runpod_manager.py` — add route after `api_pods_post` (~line 1549)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pod_request.py` (inside `ApiPodsPostSignalTest`):

```python
    def test_create_request_inserts_pending_row(self):
        self._login()
        with mock.patch.object(rm, "list_pods", return_value=[]):
            r = self.client.post("/api/pod-requests", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["request"]["name"], "cv_pod_1")
        pend = rm.list_pending_requests()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["assigned_project"], "CV")
        self.assertEqual(pend[0]["requested_by"], "alice")

    def test_create_request_rejected_when_quota_full(self):
        self._login()
        running = [{"desiredStatus": "RUNNING", "assignedProject": "CV", "countsTowardQuota": True}] * 4
        with mock.patch.object(rm, "list_pods", return_value=running):
            # default CV quota is 4 → already full
            r = self.client.post("/api/pod-requests", json={})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(rm.list_pending_requests(), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.ApiPodsPostSignalTest.test_create_request_inserts_pending_row -v`
Expected: FAIL — 404 (route doesn't exist).

- [ ] **Step 3: Add the route**

Insert after the `api_pods_post` function in `runpod_manager.py`:

```python
@app.route("/api/pod-requests", methods=["POST"])
@require_user
def api_pod_requests_post():
    """Create an auto-retry pod request ('заявка'). Reserves a quota slot now;
    a background worker (pod_request_loop) retries the deploy until success or
    the admin-configured timeout."""
    try:
        nick, proj = g.current_user
        admin = is_admin()
        data = request.get_json(silent=True) or {}

        # Admin-only fields, mirroring api_pods_post.
        if admin:
            ap = data.get("assigned_project")
            if ap is not None and ap not in PROJECTS:
                return jsonify({"ok": False, "error": "Unknown project"}), 400
            cf = bool(data.get("counts_toward_quota", False))
            src = 'admin'
        else:
            ap = proj
            cf = True
            src = 'user'

        # Window + quota are enforced once, at request-creation time (admins bypass).
        if not admin:
            w = check_pod_window()
            if not w["is_open"]:
                return jsonify({"ok": False,
                                "error": f"Запуск подов ограничен. Снова будет доступен в {w['until']} UTC."}), 400
            quotas = get_settings().get("project_quotas") or {}
            quota = quotas.get(ap, DEFAULT_PROJECT_QUOTA)
            used = project_quota_usage(ap)
            if used >= quota:
                return jsonify({"ok": False,
                                "error": f"Достигнут лимит {ap}: {used}/{quota}"}), 400

        pods = list_pods()
        reserved = pods + [{"name": n} for n in pending_request_names()]
        name = next_name(reserved, ap)
        rid = create_pod_request(name, ap, cf, src, nick)
        log_action(nick, proj, "request", name, "")
        return jsonify({"ok": True, "request": {
            "id": rid, "name": name, "status": "pending", "assignedProject": ap}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_pod_request.ApiPodsPostSignalTest -v`
Expected: PASS (both new tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): POST /api/pod-requests creates a заявка"
```

---

## Task 9: `DELETE /api/pod-requests/<id>` — cancel / close

**Files:**
- Modify: `runpod_manager.py` — add route after `api_pod_requests_post`
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pod_request.py` (inside `ApiPodsPostSignalTest`):

```python
    def test_cancel_pending_sets_cancelled(self):
        self._login()
        rid = rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        r = self.client.delete(f"/api/pod-requests/{rid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(rm.get_pod_request(rid)["status"], "cancelled")

    def test_close_terminal_deletes_row(self):
        self._login()
        rid = rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        rm.update_pod_request(rid, status="timed_out")
        r = self.client.delete(f"/api/pod-requests/{rid}")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(rm.get_pod_request(rid))

    def test_cannot_cancel_other_projects_request(self):
        self._login()  # alice / CV
        rid = rm.create_pod_request("dv_pod_1", "DV", True, "user", "bob")
        r = self.client.delete(f"/api/pod-requests/{rid}")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(rm.get_pod_request(rid)["status"], "pending")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.ApiPodsPostSignalTest.test_cancel_pending_sets_cancelled -v`
Expected: FAIL — 404 (route doesn't exist).

- [ ] **Step 3: Add the route**

Insert after `api_pod_requests_post`:

```python
@app.route("/api/pod-requests/<int:req_id>", methods=["DELETE"])
@require_user
def api_pod_requests_delete(req_id):
    """Cancel a pending request, or close (dismiss) a terminal one.
    Project-scoped: a non-admin can only touch their own project's requests;
    others return 404 to keep existence private (mirrors api_del)."""
    try:
        nick, proj = g.current_user
        req = get_pod_request(req_id)
        if req is None:
            return jsonify({"ok": False, "error": "Request not found"}), 404
        if not is_admin() and req["assigned_project"] != proj:
            return jsonify({"ok": False, "error": "Request not found"}), 404
        if req["status"] == "pending":
            update_pod_request(req_id, status="cancelled", finished_at=now_iso())
            log_action(nick, proj, "request_cancel", req["pod_name"], "")
        else:
            # timed_out / failed → 'Закрыть' just removes the card.
            delete_pod_request(req_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_pod_request.ApiPodsPostSignalTest -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): DELETE /api/pod-requests cancels/closes a заявка"
```

---

## Task 10: `api_pods_get` returns `requests[]` and counts pending in quota

**Files:**
- Modify: `runpod_manager.py` — `api_pods_get` (~line 1469-1500)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pod_request.py` (inside `ApiPodsPostSignalTest`):

```python
    def test_pods_get_includes_requests_and_quota(self):
        self._login()
        rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        with mock.patch.object(rm, "list_pods", return_value=[]):
            r = self.client.get("/api/pods")
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["requests"]), 1)
        self.assertEqual(body["requests"][0]["name"], "cv_pod_1")
        self.assertEqual(body["requests"][0]["status"], "pending")
        # pending request occupies a quota slot
        self.assertEqual(body["projectRunning"], 1)

    def test_pods_get_hides_other_projects_requests(self):
        self._login()  # CV
        rm.create_pod_request("dv_pod_1", "DV", True, "user", "bob")
        with mock.patch.object(rm, "list_pods", return_value=[]):
            r = self.client.get("/api/pods")
        self.assertEqual(r.get_json()["requests"], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.ApiPodsPostSignalTest.test_pods_get_includes_requests_and_quota -v`
Expected: FAIL — no `requests` key; `projectRunning` is 0 (pending not counted).

- [ ] **Step 3: Update `api_pods_get`**

In `api_pods_get`, replace the `project_running` computation:

```python
        # Count running pods IN THIS PROJECT with counts_toward_quota=True.
        project_running = sum(1 for p in all_pods
                              if p.get("desiredStatus") == "RUNNING"
                              and p.get("assignedProject") == viewer_project
                              and p.get("countsTowardQuota"))
```

with (now includes pending requests via the shared helper):

```python
        # Quota usage = running pods + pending requests, both counting toward quota.
        project_running = project_quota_usage(viewer_project, pods=all_pods)
```

Then build the requests payload. Insert just before the `return jsonify(...)`:

```python
        req_rows = list_visible_requests(viewer_project, viewer_is_admin)
        requests_payload = [{
            "id": r["id"],
            "name": r["pod_name"],
            "assignedProject": r["assigned_project"],
            "status": r["status"],
            "lastError": r["last_error"],
            "createdAt": r["created_at"],
        } for r in req_rows]
```

And add `"requests": requests_payload,` to the returned `jsonify({...})` dict (e.g. right after `"pods": pods,`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_pod_request.ApiPodsPostSignalTest -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): api_pods_get returns requests[] and counts them in quota"
```

---

## Task 11: Worker tick `process_pending_requests()`

**Files:**
- Modify: `runpod_manager.py` — add after `check_idle_timeouts` (~line 1350), before the Scheduler section
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_pod_request.py`:

```python
class WorkerTickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._orig_db_path = rm.DB_PATH
        rm.DB_PATH = Path(self.tmp.name)
        rm.init_db()

    def tearDown(self):
        rm.DB_PATH = self._orig_db_path
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_success_marks_fulfilled_and_writes_assignment(self):
        rid = rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        with mock.patch.object(rm, "create_pod_via_graphql",
                               return_value={"id": "p_new", "name": "cv_pod_1"}), \
             mock.patch.object(rm, "upsert_pod_assignment") as upsert:
            rm.process_pending_requests()
            upsert.assert_called_once_with("p_new", "CV", 1, "user", "alice")
        row = rm.get_pod_request(rid)
        self.assertEqual(row["status"], "fulfilled")
        self.assertEqual(row["pod_id"], "p_new")

    def test_gpu_unavailable_stays_pending(self):
        rid = rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        with mock.patch.object(rm, "create_pod_via_graphql",
                               side_effect=rm.GpuUnavailableError("no resources")):
            rm.process_pending_requests()
        row = rm.get_pod_request(rid)
        self.assertEqual(row["status"], "pending")
        self.assertIn("no resources", row["last_error"])

    def test_permanent_error_marks_failed(self):
        rid = rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        with mock.patch.object(rm, "create_pod_via_graphql",
                               side_effect=RuntimeError("invalid api key")):
            rm.process_pending_requests()
        self.assertEqual(rm.get_pod_request(rid)["status"], "failed")

    def test_timeout_marks_timed_out_without_deploy(self):
        rid = rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        # Backdate created_at well past the 15-minute default timeout.
        rm.update_pod_request(rid, created_at="2000-01-01T00:00:00Z")
        with mock.patch.object(rm, "create_pod_via_graphql") as deploy:
            rm.process_pending_requests()
            deploy.assert_not_called()
        self.assertEqual(rm.get_pod_request(rid)["status"], "timed_out")

    def test_cancelled_mid_deploy_deletes_pod(self):
        rid = rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")

        def deploy_then_cancel(name):
            # Simulate the user cancelling while the deploy is in flight.
            rm.update_pod_request(rid, status="cancelled")
            return {"id": "p_orphan", "name": name}

        with mock.patch.object(rm, "create_pod_via_graphql", side_effect=deploy_then_cancel), \
             mock.patch.object(rm, "upsert_pod_assignment") as upsert, \
             mock.patch.object(rm, "delete_pod") as delete_pod:
            rm.process_pending_requests()
            upsert.assert_not_called()
            delete_pod.assert_called_once_with("p_orphan")
        self.assertEqual(rm.get_pod_request(rid)["status"], "cancelled")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.WorkerTickTest -v`
Expected: FAIL — `AttributeError: ... 'process_pending_requests'`.

- [ ] **Step 3: Add the worker tick**

Insert into `runpod_manager.py` after `check_idle_timeouts`:

```python
def process_pending_requests():
    """One auto-retry tick: for each pending pod_request, time it out, retry its
    deploy, or fulfil it. Each request is isolated in try/except so one failure
    can't abort the tick. Driven by pod_request_loop()."""
    s = get_settings()
    timeout_min = s.get("pod_request_timeout_minutes", 15)
    now = now_utc()
    for req in list_pending_requests():
        rid = req["id"]
        try:
            # 1) Timeout check — give up without attempting a deploy.
            created = parse_iso(req["created_at"])
            if created and (now - created).total_seconds() >= timeout_min * 60:
                update_pod_request(rid, status="timed_out", finished_at=now_iso())
                log_action("REQUEST_TIMEOUT", "[SYSTEM]", "request timeout", req["pod_name"], "")
                continue

            # 2) Attempt the deploy (quota/window were reserved at creation).
            try:
                result = create_pod_via_graphql(req["pod_name"])
            except GpuUnavailableError as e:
                update_pod_request(rid, last_attempt_at=now_iso(), last_error=str(e))
                continue
            except Exception as e:
                update_pod_request(rid, status="failed", last_error=str(e), finished_at=now_iso())
                log.error(f"pod_request {rid}: permanent deploy failure: {e}")
                continue

            pid = result.get("id", "") if isinstance(result, dict) else ""

            # 3) Re-read status — the user may have cancelled during the deploy.
            fresh = get_pod_request(rid)
            if fresh and fresh["status"] == "cancelled":
                if pid:
                    try:
                        delete_pod(pid)
                    except Exception as e:
                        log.error(f"pod_request {rid}: failed to clean up orphan pod {pid}: {e}")
                continue

            # 4) Success → assignment + fulfil + log as a normal create.
            try:
                upsert_pod_assignment(pid, req["assigned_project"],
                                      req["counts_toward_quota"], req["creation_source"],
                                      req["requested_by"])
            except Exception as e:
                update_pod_request(rid, status="failed", pod_id=pid, finished_at=now_iso(),
                                   last_error=f"под создан (id={pid}), но assignment не записан — admin /assign: {e}")
                log_action(req["requested_by"], req["assigned_project"], "create", req["pod_name"], pid)
                continue

            update_pod_request(rid, status="fulfilled", pod_id=pid, finished_at=now_iso())
            log_action(req["requested_by"], req["assigned_project"], "create", req["pod_name"], pid)
        except Exception as e:
            log.error(f"process_pending_requests: request {rid}: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_pod_request.WorkerTickTest -v`
Expected: PASS (all five)

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): process_pending_requests worker tick"
```

---

## Task 12: `pod_request_loop` thread + startup wiring + settings defaults

**Files:**
- Modify: `runpod_manager.py` — add `pod_request_loop` after `scheduler_loop` (~line 1407); add `DEFAULT_SETTINGS` keys (~line 91); start the thread in `__main__` (~line 2892)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pod_request.py` `WorkerTickTest`:

```python
    def test_settings_defaults_present(self):
        self.assertEqual(rm.DEFAULT_SETTINGS["pod_request_timeout_minutes"], 15)
        self.assertEqual(rm.DEFAULT_SETTINGS["pod_request_retry_interval_seconds"], 15)

    def test_pod_request_loop_callable(self):
        self.assertTrue(callable(rm.pod_request_loop))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.WorkerTickTest.test_settings_defaults_present -v`
Expected: FAIL — keys missing.

- [ ] **Step 3: Add defaults, loop, and startup wiring**

In `DEFAULT_SETTINGS`, change the idle-timeout line to also include the new keys (append before the closing of the dict — e.g. right after the `idle_timeout` entry):

```python
    "idle_timeout_enabled":True,"idle_timeout_minutes":120,
    # Auto-retry "заявка на под": total retry window (minutes) and interval
    # between deploy attempts (seconds). See pod_request_loop.
    "pod_request_timeout_minutes":15,
    "pod_request_retry_interval_seconds":15,
```

Add `pod_request_loop` after `scheduler_loop`:

```python
def pod_request_loop():
    """Daemon loop driving auto-retry of pending pod requests. Sleeps the
    admin-configured interval (re-read each iteration so changes apply without a
    restart), clamped to a sane minimum."""
    while True:
        try:
            process_pending_requests()
        except Exception as e:
            log.error(f"pod_request_loop: {e}")
        try:
            interval = max(5, int(get_settings().get("pod_request_retry_interval_seconds", 15)))
        except (TypeError, ValueError):
            interval = 15
        _time.sleep(interval)
```

In `__main__`, next to the existing scheduler thread:

```python
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=pod_request_loop, daemon=True).start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_pod_request.WorkerTickTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): start pod_request_loop thread + settings defaults"
```

---

## Task 13: Admin settings — validate + persist the two new fields

**Files:**
- Modify: `runpod_manager.py` — admin settings GET payload (~line 1621), settings POST handler (~line 1655-1658)
- Test: `tests/test_pod_request.py`

- [ ] **Step 1: Write the failing test**

Add a test class to `tests/test_pod_request.py`:

```python
class AdminSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._orig_db_path = rm.DB_PATH
        rm.DB_PATH = Path(self.tmp.name)
        # Isolate settings file too
        self.stmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.stmp.close()
        self._orig_settings = rm.SETTINGS_FILE
        rm.SETTINGS_FILE = Path(self.stmp.name)
        os.unlink(self.stmp.name)  # let load_settings recreate from defaults
        rm.init_db()
        rm.app.config["TESTING"] = True
        self.client = rm.app.test_client()

    def tearDown(self):
        rm.DB_PATH = self._orig_db_path
        rm.SETTINGS_FILE = self._orig_settings
        for p in (self.tmp.name, self.stmp.name):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _admin(self):
        with self.client.session_transaction() as sess:
            sess["admin"] = True

    def test_settings_post_persists_request_fields(self):
        self._admin()
        r = self.client.post("/api/admin/settings", json={
            "pod_request_timeout_minutes": 30,
            "pod_request_retry_interval_seconds": 20,
        })
        self.assertEqual(r.status_code, 200)
        s = rm.get_settings()
        self.assertEqual(s["pod_request_timeout_minutes"], 30)
        self.assertEqual(s["pod_request_retry_interval_seconds"], 20)

    def test_settings_post_clamps_bad_values(self):
        self._admin()
        self.client.post("/api/admin/settings", json={
            "pod_request_timeout_minutes": 0,
            "pod_request_retry_interval_seconds": 1,
        })
        s = rm.get_settings()
        self.assertEqual(s["pod_request_timeout_minutes"], 1)   # min 1
        self.assertEqual(s["pod_request_retry_interval_seconds"], 5)  # min 5
```

(Confirmed: the constant is `SETTINGS_FILE` (line 77), and `@require_admin` reads `session["admin"]` per `api_admin_login`. The helpers above are correct.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_request.AdminSettingsTest -v`
Expected: FAIL — fields not persisted/clamped.

- [ ] **Step 3: Add GET exposure + POST validation**

In the admin settings GET payload (the dict comprehension listing settings keys, ~line 1621), add the two keys to the list:

```python
        **{k:s.get(k) for k in ["auto_delete_enabled","auto_delete_time","auto_delete_last_log","idle_timeout_enabled","idle_timeout_minutes","pod_window_enabled","pod_window_from","pod_window_until","pod_request_timeout_minutes","pod_request_retry_interval_seconds"]}
```

In the settings POST handler, after the `idle_timeout_minutes` block, add:

```python
    if "pod_request_timeout_minutes" in data:
        try: s["pod_request_timeout_minutes"]=max(1,min(1440,int(data["pod_request_timeout_minutes"])))
        except: pass
    if "pod_request_retry_interval_seconds" in data:
        try: s["pod_request_retry_interval_seconds"]=max(5,min(600,int(data["pod_request_retry_interval_seconds"])))
        except: pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_pod_request.AdminSettingsTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_request.py runpod_manager.py
git commit -m "feat(autoretry): admin settings persist/clamp request timeout + interval"
```

---

## Task 14: Admin panel UI — two new inputs

**Files:**
- Modify: `runpod_manager.py` — admin panel HTML in `loadAdminPanel` (the `⏱ Idle timeout` section, ~line 2107-2112) and `sbSave` (~line 2283)

This is inline JS/HTML — no unit test. Verified manually in Task 16.

- [ ] **Step 1: Add the inputs to the settings panel**

In `loadAdminPanel`, after the Idle timeout `sb-section` closes (after line 2112 `'</div>'+`), insert a new section:

```javascript
      '<div class="sb-section"><h3>🔁 Авторетрай заявки на под</h3>'+
        '<div class="fr"><label>Таймаут заявки (мин)</label><input type="number" id="sReqTimeout" min="1" max="1440" value="'+(s.pod_request_timeout_minutes||15)+'"></div>'+
        '<div class="fr"><label>Интервал ретрая (сек)</label><input type="number" id="sReqInterval" min="5" max="600" value="'+(s.pod_request_retry_interval_seconds||15)+'"></div>'+
        '<div class="sb-dim">Когда видеокарта занята, пользователь может оставить заявку — менеджер повторяет запуск каждые «интервал» секунд, пока не получится или не выйдет «таймаут».</div>'+
      '</div>'+
```

- [ ] **Step 2: Send the values in `sbSave`**

In `sbSave`, add to the POST body object (after the `idle_timeout_minutes:` line):

```javascript
    pod_request_timeout_minutes:parseInt($('sReqTimeout').value)||15,
    pod_request_retry_interval_seconds:parseInt($('sReqInterval').value)||15,
```

- [ ] **Step 3: Sanity-check Python still imports**

Run: `python -c "import runpod_manager"`
Expected: no error (the HTML/JS is just a string literal).

- [ ] **Step 4: Commit**

```bash
git add runpod_manager.py
git commit -m "feat(autoretry): admin panel inputs for request timeout + interval"
```

---

## Task 15: Frontend — gpuUnavailable dialog, placeholder cards, cancel

**Files:**
- Modify: `runpod_manager.py` — `createPod()` (~line 2406-2414), `render()` (the `$('pl').innerHTML = ...` block, ~line 2653-2654), and a new `cancelRequest()` function near `delPod` (~line 2416)

Inline JS — no unit test. Verified manually in Task 16.

- [ ] **Step 1: Update `createPod()` to handle the signal**

Replace the body of `createPod()` (keep the signature) so it inspects the response for `gpuUnavailable`. The endpoint returns HTTP 200 with `ok:false,gpuUnavailable:true`, so call the raw `api()` (which returns the parsed JSON without throwing on `ok:false`) rather than `aok()`:

```javascript
async function createPod(){if(!user)return;const b=$('cb');b.disabled=true;b.innerHTML='<span class="sp"></span>';
  let body={};
  if(isAdmin){
    const apEl=$('adminAssignProject');const cfEl=$('adminCountsFlag');
    if(apEl){const v=apEl.value;if(v==='__null__')body.assigned_project=null;else if(v&&v!=='')body.assigned_project=v;}
    if(cfEl)body.counts_toward_quota=cfEl.checked;
  }
  try{
    const r=await api('/api/pods','POST',body);
    if(r.ok){toast(r.name+' created!','ok');await refreshPods();refreshActivityLog();return;}
    if(r.gpuUnavailable){
      b.disabled=false;b.innerHTML='+ New Pod';
      const ok=await showDlg('<h3>Все видеокарты заняты</h3><p style="color:var(--t2);font-size:13px;margin-bottom:18px">Кажется, в данный момент все видеокарты заняты. Оставить заявку на под? Менеджер сам поймает свободную карту.</p><div class="da"><button class="btn" onclick="closeDlg(false)">Отмена</button><button class="btn bs bp" onclick="closeDlg(true)">Оставить заявку</button></div>');
      if(!ok)return;
      const rr=await api('/api/pod-requests','POST',body);
      if(rr.ok){toast('Заявка создана — подбираю видеокарту','ok');await refreshPods();refreshActivityLog();}
      else{toast(rr.error||'Не удалось создать заявку','er');}
      return;
    }
    toast(r.error||'Failed','er');
  }catch(e){toast(e.message,'er')}finally{b.disabled=false;b.innerHTML='+ New Pod'}}
```

(Confirmed: `api(u,m,b)` (line 1945) returns `await resp.json()` for a 200 response and does NOT throw on `ok:false` — only `aok()` throws. So `r.gpuUnavailable` is readable here. The 403/401 auth path returns `{ok:false,_authRedirect:true}`, which falls through to the final `toast(r.error...)` harmlessly since the login screen is already shown.)

- [ ] **Step 2: Add `cancelRequest()` near `delPod`**

Insert after `delPod`:

```javascript
async function cancelRequest(id){
  if(!user)return;
  try{const j=await api('/api/pod-requests/'+id,'DELETE',{});if(!j.ok)throw new Error(j.error);await refreshPods();refreshActivityLog()}
  catch(e){toast(e.message,'er')}
}
```

- [ ] **Step 3: Render request placeholder cards in `render()`**

In `render()`, the early-return for an empty list must also account for requests. Replace:

```javascript
  if(!pods.length){$('pl').innerHTML='<div class="empty"><p>No pods. Click <b>+ New Pod</b>.</p></div>';return}
  $('pl').innerHTML=[...pods].sort((a,b)=>{const d=(a.desiredStatus==='RUNNING'?0:1)-(b.desiredStatus==='RUNNING'?0:1);return d||((a.name||'').localeCompare(b.name||''))}).map(p=>{
```

with:

```javascript
  const reqCards=(requests||[]).map(r=>{
    const nm=esc(r.name);
    if(r.status==='pending'){
      return '<div class="pc"><div style="display:flex;align-items:center;gap:10px;padding:14px 16px">'+
        '<span class="sp"></span>'+
        '<div style="flex:1"><div style="font-weight:600">'+nm+'</div>'+
        '<div style="color:var(--t2);font-size:12px">подбираю свободную видеокарту, ожидайте…</div></div>'+
        '<button class="btn bs" onclick="cancelRequest('+r.id+')">Отменить заявку</button></div></div>';
    }
    const msg=r.status==='timed_out'
      ? 'Не удалось подобрать видеокарту за отведённое время'
      : (esc(r.lastError||'Не удалось создать под'));
    return '<div class="pc"><div style="display:flex;align-items:center;gap:10px;padding:14px 16px">'+
      '<div style="flex:1"><div style="font-weight:600">'+nm+'</div>'+
      '<div style="color:var(--er,#e55);font-size:12px">'+msg+'</div></div>'+
      '<button class="btn bs" onclick="cancelRequest('+r.id+')">Закрыть</button></div></div>';
  }).join('');
  if(!pods.length&&!reqCards){$('pl').innerHTML='<div class="empty"><p>No pods. Click <b>+ New Pod</b>.</p></div>';return}
  $('pl').innerHTML=reqCards+[...pods].sort((a,b)=>{const d=(a.desiredStatus==='RUNNING'?0:1)-(b.desiredStatus==='RUNNING'?0:1);return d||((a.name||'').localeCompare(b.name||''))}).map(p=>{
```

- [ ] **Step 4: Store `requests` from the refresh payload**

In `refreshPods()`, where it sets `pods=r.pods||[];`, add right after it:

```javascript
    requests=r.requests||[];
```

And declare the `requests` global next to the `pods` global. The declaration is (confirmed at ~line 1930):

```javascript
let pods=[],busy=new Set(),maxPods=99,isAdmin=false,user=null;
```

Change it to add `requests`:

```javascript
let pods=[],requests=[],busy=new Set(),maxPods=99,isAdmin=false,user=null;
```

- [ ] **Step 5: Sanity-check import + commit**

Run: `python -c "import runpod_manager"`
Expected: no error.

```bash
git add runpod_manager.py
git commit -m "feat(autoretry): gpuUnavailable dialog + request placeholder cards"
```

---

## Task 16: Full test run, manual verification, docs + memory

**Files:**
- Modify: `docs/graphql-deploy.md` (add an auto-retry note), `docs/pod-lifecycle.md` (mention заявка states), and memory (`pod-creation-flow.md`)
- Verify: full suite + manual smoke test

- [ ] **Step 1: Run the entire test suite**

Run: `python -m unittest discover -s tests -v`
Expected: ALL PASS (existing `test_migration`, `test_user_validation`, and new `test_pod_request`).

- [ ] **Step 2: Manual smoke test (documented, not automated — real deploy costs money)**

Document and perform these steps (in `docs/graphql-deploy.md` under a new "Авторетрай" subsection):

1. Temporarily set `PRESET["gpu_id"]` to a deliberately unavailable type (or use a throwaway invalid spec) to force `GpuUnavailableError`.
2. Click **+ New Pod** → confirm the dialog «Все видеокарты заняты … Оставить заявку?» appears (not a red toast).
3. Click **Оставить заявку** → a placeholder card with spinner + «подбираю свободную видеокарту, ожидайте…» appears, and the quota badge increments.
4. Confirm the worker logs a retry every ~interval seconds (`docker compose logs -f runpod-manager`).
5. Restore `PRESET["gpu_id"]` to the working type → within one interval the card flips to a real running pod.
6. Test **Отменить заявку** on a fresh pending request → card disappears, quota frees.
7. Set the admin timeout to 1 minute, leave a request with no GPU, wait → card shows «Не удалось…» with a **Закрыть** button.

- [ ] **Step 3: Update docs**

Add to `docs/graphql-deploy.md` a section describing `GpuUnavailableError`, the заявка flow, the `pod_request` table, and the two admin settings (timeout / interval). Add to `docs/pod-lifecycle.md` the request states (`pending → fulfilled | timed_out | failed | cancelled`).

- [ ] **Step 4: Update memory**

Edit `C:\Users\admin_korneev\.claude\projects\E--my-stable-runpod-manager\memory\pod-creation-flow.md` to record: `GpuUnavailableError` now carries the "no instances" condition; `pod_request` table + `pod_request_loop` worker implement auto-retry; requests reserve quota and survive restart. Update the «Ретрая нет» line — it now exists.

- [ ] **Step 5: Commit**

```bash
git add docs/graphql-deploy.md docs/pod-lifecycle.md
git commit -m "docs(autoretry): document заявка flow, states, and admin settings"
```

---

## Self-review notes (for the implementer)

All previously-uncertain integration points were verified against the code while writing this plan and are now baked into the tasks:

- **Session keys** — `session["user_nickname"]` / `session["user_project"]` (`get_session_user`, line 730); admin = `session["admin"]` (`api_admin_login`, line 1594). Test helpers set exactly these.
- **`api()` JS semantics** — returns parsed JSON on 200, throws only via `aok()` (lines 1945-1984). Task 15 relies on this.
- **`SETTINGS_FILE`** — module constant at line 77. Task 13 redirects it for isolation.
- **`requests` global** — added to the single `let pods=[],...` declaration at line 1930; no identifier clash (`requests` is otherwise unused in the script).
- **Helper placement** — `project_quota_usage` / `process_pending_requests` call `list_pods` (defined later at line 926). Python resolves these at call time, not import time, so forward references are fine.

**Spec coverage check:** every spec section maps to a task — table (T1), CRUD (T2), detection (T3), deploy routing (T4), quota (T5), naming (T6), gpuUnavailable signal (T7), create endpoint (T8), cancel/close (T9), listing (T10), worker tick incl. cancel-race (T11), thread+defaults (T12), settings persist (T13), admin UI (T14), frontend dialog+cards (T15), tests+docs+memory (T16). No gaps.
