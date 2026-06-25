#!/usr/bin/env python3
"""RunPod Manager v6.6 — per-project quotas + pod-launch auto-retry (заявка на под)"""

import subprocess, json, re, os, argparse, logging, shutil, secrets, threading, sqlite3
import time as _time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, jsonify, Response, request, session, g

# ============================================================
# Time helpers — ALL timestamps in this app are UTC ISO 8601 with 'Z' suffix.
# The frontend converts to the user's local timezone via JavaScript Date().
# This is the future-proof approach: works for any user in any timezone.
# ============================================================
def now_utc():
    """Current time as timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)

def now_iso():
    """Current UTC time as ISO 8601 string with 'Z' suffix.
    Example: '2026-04-07T12:34:56Z'
    This is the canonical format for ALL timestamps stored in the DB."""
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_iso(s):
    """Parse an ISO 8601 string back to a timezone-aware UTC datetime.
    Tolerates both 'Z' suffix and naive (no-tz) strings — naive is
    interpreted as UTC for backward compatibility with old DB rows."""
    if not s:
        return None
    s = s.strip()
    try:
        # Handle 'Z' suffix (Python <3.11 doesn't parse 'Z' natively)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # Handle space separator (legacy format)
        if " " in s and "T" not in s:
            s = s.replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None

PRESET = {
    "gpu_id": "NVIDIA RTX PRO 4500 Blackwell", "gpu_count": 1,
    "template_id": "i3j2sm66q8", "image": "wikiniki/comfy_runpod:latest",
    "network_volume_id": "0czgom7b1j", "volume_mount_path": "/workspace",
    "volume_in_gb": 0, "container_disk_in_gb": 20, "cloud_type": "SECURE",
    "env": {"COMFY_API_KEY": "{{ RUNPOD_SECRET_comfyui_api_partners_secret }}"},
    "comfy_port": 8188, "pod_name_prefix": "pod_",
    # GraphQL deploy fields (used by create_pod_via_graphql).
    # These are constraints and metadata the RunPod UI sends in its DeployOnDemand mutation.
    # If you change gpu_id, also update min_memory_in_gb / min_vcpu_count to match the new GPU's spec
    # (look it up in `runpodctl get cloud` or RunPod web UI). data_center_id is locked by the
    # network_volume's location — if you change the volume, change the DC too.
    "data_center_id": "EU-RO-1",
    "min_memory_in_gb": 62,
    "min_vcpu_count": 28,
    "ports": "8188/http,8888/http,8686/http,8189/http",
    "start_ssh": True,
    "start_jupyter": True,
    "global_network": False,
}
PROJECTS = ["CV", "DV", "MT", "PT", "MARK", "ADMIN", "TV", "MW"]
DEFAULT_PROJECT_QUOTA = 4

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
log = logging.getLogger("runpod_manager")
_cli_path = "runpodctl"; _cli_is_new = True; _api_key = ""
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "admin_settings.json"
DB_PATH = DATA_DIR / "runpod_manager.db"
DEFAULT_SETTINGS = {"admin_password":"admin","max_pods":5,
    "project_quotas":{p: DEFAULT_PROJECT_QUOTA for p in PROJECTS},
    # Per-project pod image selection. Catalog of {label, template_id} the admin
    # edits in the panel; default_pod_image is the template_id used for projects
    # with no explicit choice, unassigned pods, and admin pods. project_pod_image
    # maps project -> template_id (missing key = use default). Seeded with the
    # current template so behavior is unchanged until the admin switches a project.
    "pod_image_catalog":[{"label":"Текущий (comfy_runpod)","template_id":"i3j2sm66q8"}],
    "default_pod_image":"i3j2sm66q8",
    "project_pod_image":{},
    "auto_delete_enabled":False,"auto_delete_time":"21:00",
    "auto_delete_last_run":"","auto_delete_last_log":"",
    # Per-project auto-delete offset in MINUTES. For each project, the effective
    # daily auto-delete fires at (auto_delete_time + offset_minutes). 0 = no
    # offset (delete at base time). Value range 0..1440 (up to 24h). Unassigned
    # pods ignore this dict entirely and always fire at base time.
    "project_autodelete_offset_minutes":{p: 0 for p in PROJECTS},
    # Per-project last-fire date guard ({project_name|__unassigned__: "YYYY-MM-DD"}).
    # Prevents double-firing within the same UTC day. Populated at runtime.
    "project_autodelete_last_run":{},
    "idle_timeout_enabled":True,"idle_timeout_minutes":120,
    # Auto-retry "заявка на под": total retry window (minutes) and interval
    # between deploy attempts (seconds). See pod_request_loop.
    "pod_request_timeout_minutes":15,
    "pod_request_retry_interval_seconds":15,
    # Pod creation restriction window (strategy A: only blocks NEW pods).
    # Times are UTC 'HH:MM'. 'from' and 'until' define the period when creation is
    # FORBIDDEN (typically night hours). Supports overnight spans (e.g. 22:00 -> 08:00).
    # At 'until' exactly, the restriction lifts and creation becomes allowed again.
    # If from == until, the window is logically disabled even when enabled flag is True.
    "pod_window_enabled":False,"pod_window_from":"22:00","pod_window_until":"08:00"}

# ============================================================
# ComfyUI service health check
# ============================================================
_service_cache = {}
_service_cache_lock = threading.Lock()
SERVICE_CHECK_TTL = 15
SERVICE_CHECK_TIMEOUT = 6

def check_pod_service(pod_id, port=8188):
    if not pod_id: return False
    now = _time.time()
    with _service_cache_lock:
        c = _service_cache.get(pod_id)
        if c and (now - c["checked_at"] < SERVICE_CHECK_TTL):
            return c["ready"]
    url = f"https://{pod_id}-{port}.proxy.runpod.net/system_stats"
    ready = False
    try:
        req = urllib.request.Request(url, method="GET",
                                      headers={"User-Agent": "RunPod-Manager-Health/1.0",
                                               "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=SERVICE_CHECK_TIMEOUT) as resp:
            if resp.status == 200:
                body = resp.read(8192).decode("utf-8", errors="ignore")
                try:
                    data = json.loads(body)
                    if isinstance(data, dict) and ("system" in data or "devices" in data):
                        ready = True
                except json.JSONDecodeError:
                    pass
    except urllib.error.HTTPError:
        ready = False
    except Exception:
        ready = False
    with _service_cache_lock:
        _service_cache[pod_id] = {"ready": ready, "checked_at": now}
    return ready

def check_pods_services_parallel(pod_ids):
    if not pod_ids: return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check_pod_service, pod_ids))
    return dict(zip(pod_ids, results))

# ============================================================
# Boot status check (port 8189 — served by start.sh's python http.server)
# ============================================================
_boot_cache = {}
_boot_cache_lock = threading.Lock()
BOOT_CHECK_TTL = 5         # short cache so progress feels live
BOOT_CHECK_TIMEOUT = 4     # don't waste time — if it's not there, fall back to indeterminate
BOOT_PORT = 8189

def check_pod_boot_status(pod_id):
    """Fetch /status.json from the pod's boot HTTP server on port 8189.
    Returns dict with stage/pct/msg/elapsed, or None if unavailable.
    Cached for BOOT_CHECK_TTL seconds."""
    if not pod_id: return None
    now = _time.time()
    with _boot_cache_lock:
        c = _boot_cache.get(pod_id)
        if c and (now - c["checked_at"] < BOOT_CHECK_TTL):
            return c["data"]
    url = f"https://{pod_id}-{BOOT_PORT}.proxy.runpod.net/status.json"
    data = None
    try:
        req = urllib.request.Request(url, method="GET",
                                      headers={"User-Agent": "RunPod-Manager-Boot/1.0",
                                               "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=BOOT_CHECK_TIMEOUT) as resp:
            if resp.status == 200:
                body = resp.read(4096).decode("utf-8", errors="ignore")
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict) and "pct" in parsed:
                        data = {
                            "stage": str(parsed.get("stage", "")),
                            "pct": int(parsed.get("pct", 0)),
                            "msg": str(parsed.get("msg", "")),
                            "elapsed": int(parsed.get("elapsed", 0)),
                        }
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass
    with _boot_cache_lock:
        _boot_cache[pod_id] = {"data": data, "checked_at": now}
    return data

def check_pods_boot_parallel(pod_ids):
    """Fetch boot status for multiple pods in parallel."""
    if not pod_ids: return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check_pod_boot_status, pod_ids))
    return dict(zip(pod_ids, results))

# ============================================================
# Runtime activity check (port 8189 — same HTTP server as boot status)
# Reads /runtime.json which is updated by start.sh's runtime tail watcher
# whenever ComfyUI logs 'got prompt' / 'Prompt executed' / 'Processing interrupted'.
# ============================================================
_runtime_cache = {}
_runtime_cache_lock = threading.Lock()
RUNTIME_CHECK_TTL = 10        # seconds; activity changes are event-driven, no need to poll fast
RUNTIME_CHECK_TIMEOUT = 4

def check_pod_runtime_status(pod_id):
    """Fetch /runtime.json from the pod's HTTP server on port 8189.
    Returns dict with active/queue_depth/last_event/etc, or None if unavailable.
    Cached for RUNTIME_CHECK_TTL seconds.
    Only call this for pods that are already serviceReady — boot-stage pods
    won't have meaningful activity data yet."""
    if not pod_id: return None
    now = _time.time()
    with _runtime_cache_lock:
        c = _runtime_cache.get(pod_id)
        if c and (now - c["checked_at"] < RUNTIME_CHECK_TTL):
            return c["data"]
    url = f"https://{pod_id}-{BOOT_PORT}.proxy.runpod.net/runtime.json"
    data = None
    try:
        req = urllib.request.Request(url, method="GET",
                                      headers={"User-Agent": "RunPod-Manager-Runtime/1.0",
                                               "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=RUNTIME_CHECK_TIMEOUT) as resp:
            if resp.status == 200:
                body = resp.read(4096).decode("utf-8", errors="ignore")
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict) and "active" in parsed:
                        data = {
                            "active": bool(parsed.get("active", False)),
                            "queue_depth": int(parsed.get("queue_depth", 0)),
                            "total_started": int(parsed.get("total_started", 0)),
                            "total_completed": int(parsed.get("total_completed", 0)),
                            "last_event": str(parsed.get("last_event", "")),
                            "last_event_at": str(parsed.get("last_event_at", "")),
                            "last_completed_duration": parsed.get("last_completed_duration"),
                        }
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass
    with _runtime_cache_lock:
        _runtime_cache[pod_id] = {"data": data, "checked_at": now}
    return data

def check_pods_runtime_parallel(pod_ids):
    """Fetch runtime status for multiple ready pods in parallel."""
    if not pod_ids: return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check_pod_runtime_status, pod_ids))
    return dict(zip(pod_ids, results))

# ============================================================
# SQLite
# ============================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH)); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(str(DB_PATH))
    # NOTE: All timestamps are stored as UTC ISO 8601 with 'Z' suffix.
    # SQLite's strftime('%Y-%m-%dT%H:%M:%SZ', 'now') returns UTC time in our canonical format.
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
    """)
    db.close()
    migrate_to_pod_assignment()


def migrate_to_pod_assignment():
    """One-shot migration from pod_hidden → pod_assignment.

    Idempotent: if pod_hidden doesn't exist (already migrated), does nothing.
    Also back-fills pod_assignment from pod_actions.create for pods not in pod_hidden.

    Transaction note: this runs on its own connection, separate from the
    executescript() inside init_db(). If the migration crashes partway, the
    per-row existence check on next startup skips already-inserted rows, and
    pod_hidden is only dropped at the very end — so partial failures are
    recoverable by simply re-running init_db().
    """
    db = sqlite3.connect(str(DB_PATH))
    try:
        db.execute("BEGIN")
        now = now_iso()

        cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pod_hidden'")
        has_hidden = cur.fetchone() is not None

        hidden_count = 0
        if has_hidden:
            # Step 1: hidden pods → assigned_project=NULL, counts=0
            rows = db.execute("SELECT pod_id FROM pod_hidden").fetchall()
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
        # Explicit subquery join avoids relying on SQLite's non-standard bare-column
        # GROUP BY semantics for columns not covered by an aggregate.
        creator_count = 0
        rows = db.execute("""
            SELECT pa.pod_id, pa.nickname, pa.project
            FROM pod_actions pa
            JOIN (
                SELECT pod_id, MAX(ts) AS max_ts
                FROM pod_actions
                WHERE action='create'
                GROUP BY pod_id
            ) latest ON latest.pod_id = pa.pod_id AND latest.max_ts = pa.ts
            WHERE pa.action='create'
        """).fetchall()
        for pid, nickname, project in rows:
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
        if hidden_count > 0 or creator_count > 0:
            log.info(f"[MIGRATION] pod_assignment populated: {hidden_count} from pod_hidden, {creator_count} from pod_actions")
    except Exception as e:
        db.rollback()
        log.error(f"migrate_to_pod_assignment failed: {e}")
        raise
    finally:
        db.close()

def log_action(nickname, project, action, pod_name="", pod_id=""):
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.execute("INSERT INTO pod_actions(nickname,project,action,pod_name,pod_id,ts) VALUES(?,?,?,?,?,?)",
                   (nickname, project, action, pod_name, pod_id, now_iso()))
        db.commit(); db.close()
    except Exception as e: log.error(f"log_action: {e}")

def touch_user(nickname, project):
    try:
        db = sqlite3.connect(str(DB_PATH))
        ex = db.execute("SELECT id FROM users WHERE nickname=? AND project=?", (nickname, project)).fetchone()
        if ex: db.execute("UPDATE users SET last_seen=? WHERE id=?", (now_iso(), ex[0]))
        else: db.execute("INSERT INTO users(nickname,project,created_at,last_seen) VALUES(?,?,?,?)", (nickname, project, now_iso(), now_iso()))
        db.commit(); db.close()
    except Exception as e: log.error(f"touch_user: {e}")

def get_pod_creators(pod_ids):
    if not pod_ids: return {}
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(pod_ids))
        rows = db.execute(f"""
            SELECT pod_id, nickname, project, ts FROM pod_actions
            WHERE pod_id IN ({placeholders}) AND action='create'
            ORDER BY ts DESC
        """, pod_ids).fetchall()
        db.close()
        result = {}
        for r in rows:
            pid = r["pod_id"]
            if pid not in result:
                result[pid] = {"nickname": r["nickname"], "project": r["project"], "ts": r["ts"]}
        return result
    except Exception as e:
        log.error(f"get_pod_creators: {e}")
        return {}

# ----- Pod timers (idle tracking) -----

def timer_init_if_missing(pod_id):
    """Create timer entry only if it doesn't exist yet.
    Called when ComfyUI first becomes ready for a pod."""
    try:
        now = now_iso()
        db = sqlite3.connect(str(DB_PATH))
        cur = db.execute("SELECT 1 FROM pod_timers WHERE pod_id=?", (pod_id,))
        if cur.fetchone() is None:
            db.execute("INSERT INTO pod_timers(pod_id, last_active, created_at) VALUES(?,?,?)",
                       (pod_id, now, now))
            db.commit()
            log.info(f"⏱  Idle timer started for {pod_id} (ComfyUI ready)")
        db.close()
    except Exception as e: log.error(f"timer_init_if_missing: {e}")

def timer_touch(pod_id):
    """Update last_active. Only if entry already exists (i.e. ComfyUI was ready at some point)."""
    try:
        now = now_iso()
        db = sqlite3.connect(str(DB_PATH))
        db.execute("UPDATE pod_timers SET last_active=? WHERE pod_id=?", (now, pod_id))
        db.commit(); db.close()
    except Exception as e: log.error(f"timer_touch: {e}")

def timer_delete(pod_id):
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.execute("DELETE FROM pod_timers WHERE pod_id=?", (pod_id,))
        db.commit(); db.close()
    except Exception as e: log.error(f"timer_delete: {e}")

def timer_get_all(pod_ids):
    """Returns {pod_id: {"last_active": str, "created_at": str}}"""
    if not pod_ids: return {}
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(pod_ids))
        rows = db.execute(f"SELECT pod_id, last_active, created_at FROM pod_timers WHERE pod_id IN ({placeholders})",
                          pod_ids).fetchall()
        db.close()
        return {r["pod_id"]: {"last_active": r["last_active"], "created_at": r["created_at"]} for r in rows}
    except Exception as e:
        log.error(f"timer_get_all: {e}")
        return {}

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
    Caller is responsible for computing source correctly on FIRST write.
    Raises on DB write failure — callers MUST handle the exception (typically
    by surfacing it to the user so they know the pod exists but isn't yet
    assigned, and an admin can recover via /assign)."""
    try:
        db = sqlite3.connect(str(DB_PATH))
        try:
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
            db.commit()
        finally:
            db.close()
    except Exception:
        log.error(f"upsert_pod_assignment failed for pod_id={pid}", exc_info=True)
        raise

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
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT * FROM pod_request WHERE status='pending' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()
    except Exception as e:
        log.error(f"list_pending_requests: {e}")
        return []

def list_visible_requests(project=None, viewer_is_admin=False):
    """Requests to render as cards: statuses pending/timed_out/failed.
    Admin sees all; a regular user sees only their own project's requests."""
    try:
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
                # Non-admin callers always have a concrete project (require_user
                # guarantees it); filter to just that project's requests.
                rows = db.execute(
                    """SELECT * FROM pod_request
                       WHERE status IN ('pending','timed_out','failed')
                       AND assigned_project=? ORDER BY created_at""",
                    (project,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()
    except Exception as e:
        log.error(f"list_visible_requests: {e}")
        return []

def get_pod_request(req_id):
    """Single pod_request row as dict, or None."""
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        try:
            r = db.execute("SELECT * FROM pod_request WHERE id=?", (req_id,)).fetchone()
            return dict(r) if r else None
        finally:
            db.close()
    except Exception as e:
        log.error(f"get_pod_request: {e}")
        return None

def update_pod_request(req_id, **fields):
    """Update the given columns on a pod_request row. No-op if fields empty."""
    if not fields:
        return
    # Column names come from caller-controlled **fields keys (internal call
    # sites only, never user input) — safe to interpolate. Values are still
    # passed as bound parameters.
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
    try:
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
    except Exception as e:
        log.error(f"count_pending_quota: {e}")
        return 0

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

# ============================================================
# Settings
# ============================================================
_settings_lock = threading.Lock()
def load_settings():
    with _settings_lock:
        if SETTINGS_FILE.exists():
            try: return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text("utf-8"))}
            except Exception: pass
        _save_nl(DEFAULT_SETTINGS); return dict(DEFAULT_SETTINGS)
def save_settings(s):
    with _settings_lock: _save_nl(s)
def _save_nl(s):
    SETTINGS_FILE.write_text(json.dumps(s,indent=2,ensure_ascii=False),encoding="utf-8")
def get_settings(): return load_settings()
def resolve_template_id(project):
    """RunPod template_id to deploy for a pod belonging to `project` (may be None
    for unassigned/admin pods). Priority: the project's catalog choice if it still
    exists, then the global default, then PRESET as a last resort if the catalog
    is empty/broken."""
    s = get_settings()
    catalog = s.get("pod_image_catalog") or []
    valid = {e["template_id"] for e in catalog
             if isinstance(e, dict) and e.get("template_id")}
    tid = (s.get("project_pod_image") or {}).get(project)
    if tid in valid:
        return tid
    if s.get("default_pod_image") in valid:
        return s["default_pod_image"]
    return PRESET["template_id"]
def compute_image_settings_update(data, s):
    """Pure validation for the pod-image settings POST. Given the request body
    `data` and current settings `s`, return a dict containing only the validated
    keys among {pod_image_catalog, default_pod_image, project_pod_image} that were
    present in `data`. Catalog never becomes empty (invalid/empty submission is
    dropped). Stale leftovers are tolerated by resolve_template_id at deploy time."""
    out = {}
    new_catalog = s.get("pod_image_catalog") or []
    if isinstance(data.get("pod_image_catalog"), list):
        seen = set(); cleaned = []
        for e in data["pod_image_catalog"]:
            if not isinstance(e, dict):
                continue
            label = str(e.get("label", "")).strip()[:60]
            tid = str(e.get("template_id", "")).strip()
            if not label or not tid:
                continue
            if not re.match(r"^[A-Za-z0-9_-]+$", tid):
                continue
            if tid in seen:
                continue
            seen.add(tid); cleaned.append({"label": label, "template_id": tid})
        if cleaned:                              # never let the catalog go empty
            new_catalog = cleaned
            out["pod_image_catalog"] = cleaned
    valid = {e["template_id"] for e in new_catalog}
    if "default_pod_image" in data:
        d = str(data.get("default_pod_image", "")).strip()
        out["default_pod_image"] = d if d in valid else (
            new_catalog[0]["template_id"] if new_catalog else "")
    if isinstance(data.get("project_pod_image"), dict):
        cleaned = {}
        for proj, tid in data["project_pod_image"].items():
            tid = str(tid).strip()
            if proj in PROJECTS and tid in valid:
                cleaned[proj] = tid
        out["project_pod_image"] = cleaned
    return out
def is_admin(): return session.get("admin") is True

# ============================================================
# Pod creation window check
# ============================================================
# SEMANTICS: 'from' and 'until' define a RESTRICTION window — the time period
# when pod creation is FORBIDDEN (typically night hours). Outside of this window,
# creation is allowed. The half-open interval [from, until) is the blocked period,
# so at 'until' exactly the restriction lifts (creation becomes allowed again).
#
# Returns a dict:
#   {"enabled": bool, "is_open": bool, "from": "HH:MM", "until": "HH:MM",
#    "opens_in_sec": int|None, "closes_in_sec": int|None}
#
# is_open = True means creation is currently ALLOWED.
# is_open = False means creation is currently BLOCKED (we're inside the restriction window).
#
# - If pod_window_enabled=False — no restriction, is_open=True always.
# - If from == until — degenerate config, treated as disabled (is_open=True always).
# - If from < until — same-day restriction: blocked when from <= now_utc < until.
#     Example: from=13:00, until=15:00 → blocked 13:00-15:00, allowed rest of day.
# - If from > until — overnight restriction (e.g. 23:00 → 09:00):
#     blocked when now_utc >= from OR now_utc < until.
#     Example: from=23:00, until=09:00 → blocked 23:00-09:00, allowed 09:00-23:00.
#
# opens_in_sec: seconds until restriction LIFTS (only set when currently blocked)
# closes_in_sec: seconds until restriction STARTS (only set when currently allowed)
def check_pod_window():
    s = get_settings()
    enabled = s.get("pod_window_enabled", False)
    from_str = s.get("pod_window_from", "22:00")
    until_str = s.get("pod_window_until", "08:00")
    result = {
        "enabled": enabled,
        "is_open": True,  # default: unrestricted (creation allowed)
        "from": from_str,
        "until": until_str,
        "opens_in_sec": None,
        "closes_in_sec": None,
    }
    if not enabled:
        return result
    # Parse HH:MM
    try:
        fh, fm = map(int, from_str.split(":"))
        uh, um = map(int, until_str.split(":"))
    except (ValueError, AttributeError):
        return result  # bad config — fail open (allow creation)
    from_min = fh * 60 + fm
    until_min = uh * 60 + um
    if from_min == until_min:
        # Degenerate config — treat as disabled
        return result
    now = now_utc()
    now_min = now.hour * 60 + now.minute
    # Determine if we're inside the RESTRICTION window (blocked)
    if from_min < until_min:
        # Same-day restriction: [from, until) is the blocked period
        is_blocked = (from_min <= now_min < until_min)
    else:
        # Overnight restriction: [from, 24:00) ∪ [00:00, until) is the blocked period
        is_blocked = (now_min >= from_min or now_min < until_min)
    # is_open means creation allowed — it's the opposite of is_blocked
    result["is_open"] = not is_blocked
    # Compute countdown to the next boundary.
    # Seconds within the current UTC minute offset the countdown for smoother display.
    sec_into_minute = now.second
    if is_blocked:
        # Inside restriction — compute seconds until it LIFTS (creation becomes allowed)
        # The lift happens at 'until'.
        if from_min < until_min:
            # Same-day: lift at until today
            lift_min = until_min
        else:
            # Overnight: if now >= from, lift is tomorrow at until
            # If now < until, lift is today at until
            if now_min >= from_min:
                lift_min = until_min + 24 * 60  # tomorrow
            else:
                lift_min = until_min  # today
        result["opens_in_sec"] = (lift_min - now_min) * 60 - sec_into_minute
    else:
        # Outside restriction — compute seconds until it STARTS (creation becomes blocked)
        # The block starts at 'from'.
        if from_min < until_min:
            # Same-day restriction: either starts later today or tomorrow
            if now_min < from_min:
                start_min = from_min  # today
            else:
                start_min = from_min + 24 * 60  # tomorrow (we're past until)
        else:
            # Overnight restriction: we're in the daytime gap [until, from)
            start_min = from_min  # today
        result["closes_in_sec"] = (start_min - now_min) * 60 - sec_into_minute
    return result

def require_admin(f):
    @wraps(f)
    def w(*a,**kw):
        if not is_admin(): return (jsonify({"ok":False,"error":"Unauthorized"}),401)
        return f(*a,**kw)
    return w

# ============================================================
# User identity validation and session-based auth
# ============================================================
# All pod operations (create/delete/start) and the pod listing require
# a registered user. The 'identity' is stored in the Flask session cookie
# (signed with app.secret_key, unforgeable without server-side key).
#
# Registration flow:
#   1. Client POSTs to /api/user/register with {nickname, project}
#   2. Server validates, stores in session['user_nickname'] and session['user_project']
#   3. All subsequent /api/pods* calls READ identity from session, NOT from request body
#
# This is the foundation for the future Google OAuth migration: when OAuth lands,
# only /api/user/register changes (identity comes from Google ID token instead of
# user input). All other endpoints continue to read from session unchanged.

class UserValidationError(ValueError):
    """Raised when nickname/project don't pass validation rules."""
    pass

def validate_user_input(nick, proj):
    """Validate and normalize user identity fields.
    Returns (clean_nick, clean_proj) on success, raises UserValidationError otherwise.
    Rules:
      - nickname: required, non-empty after strip(), 1-30 chars, no control chars
      - project: required, must be in PROJECTS whitelist
    """
    if not isinstance(nick, str):
        raise UserValidationError("Nickname is required")
    nick = nick.strip()
    if not nick:
        raise UserValidationError("Nickname is required")
    if len(nick) > 30:
        raise UserValidationError("Nickname is too long (max 30 chars)")
    # Strip control characters that could mess up logs/UI
    if any(ord(c) < 32 for c in nick):
        raise UserValidationError("Nickname contains invalid characters")
    if not isinstance(proj, str) or proj not in PROJECTS:
        raise UserValidationError("Invalid or missing project")
    return nick, proj

def get_session_user():
    """Returns (nickname, project) from session, or (None, None) if not registered."""
    nick = session.get("user_nickname")
    proj = session.get("user_project")
    if nick and proj:
        return nick, proj
    return None, None

def require_user(f):
    """Decorator: requires the request to come from a registered session user.
    Returns 403 if no valid identity in session. The user (nick, proj) is
    placed in flask.g.current_user for the route to use.

    NOTE: we use 403 (Forbidden) instead of 401 (Unauthorized) on purpose.
    When this manager sits behind a reverse proxy with HTTP basic auth (e.g. Caddy),
    a 401 response from our app can be misinterpreted by the browser as a stale
    basic auth credential, causing the browser to show the basic auth popup again
    on every API call until the user logs into this app. 403 avoids that popup
    entirely because browsers never trigger basic auth re-prompts on 403.
    The frontend's global handler in api() treats both 401 and 403 the same way:
    show the in-app login screen."""
    @wraps(f)
    def w(*a, **kw):
        nick, proj = get_session_user()
        if not nick or not proj:
            return (jsonify({"ok": False, "error": "Not registered. Please enter your name and project first."}), 403)
        g.current_user = (nick, proj)
        return f(*a, **kw)
    return w

# ============================================================
# HTTP / CLI
# ============================================================
def resolve_api_key(cli_arg=""):
    if cli_arg: return cli_arg.strip()
    key = os.environ.get("RUNPOD_API_KEY","").strip()
    if key: return key
    for cp in [Path.home()/".runpod"/"config.toml", Path(os.environ.get("USERPROFILE",""))/".runpod"/"config.toml"]:
        try:
            if cp.exists():
                m = re.search(r'api[Kk]ey\s*=\s*["\']?([^\s"\']+)', cp.read_text(encoding="utf-8"))
                if m: return m.group(1).strip()
        except Exception: pass
    return ""
def http_request(url, data=None, headers=None):
    hdrs = {"Content-Type":"application/json","User-Agent":"RunPod-Manager/6.0"}
    if headers: hdrs.update(headers)
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=15) as resp: return json.loads(resp.read().decode("utf-8"))
def detect_cli():
    global _cli_path, _cli_is_new
    which = shutil.which("runpodctl")
    if not which:
        for c in [os.path.expanduser("~/runpodctl.exe"), r"C:\runpodctl\runpodctl.exe"]:
            if os.path.isfile(c): which=c; break
    if not which: _cli_path="runpodctl"; return
    _cli_path = which
    try:
        r = subprocess.run([_cli_path,"pod","list","--all","--output=json"], capture_output=True, text=True, timeout=15)
        if r.returncode==0 and (not r.stdout.strip() or r.stdout.strip()[0] in "[{"): _cli_is_new=True; print(f"  ✓  CLI: {which} (new)"); return
    except Exception: pass
    _cli_is_new=False; print(f"  ✓  CLI: {which} (legacy)")
def run_cmd(args, timeout=45):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout); s = r.stdout.strip()
        if s:
            try: return {"ok":True,"data":json.loads(s)}
            except Exception: return {"ok":True,"data":s}
        if r.returncode!=0: return {"ok":False,"error":r.stderr.strip() or f"exit {r.returncode}"}
        return {"ok":True,"data":None}
    except Exception as e: return {"ok":False,"error":str(e)}

# ============================================================
# CLI error humanizer
# ============================================================
# runpodctl errors come through as multi-line stderr dumps that include the
# full '{"error":"..."}' JSON blob, followed by the command's --help usage text
# (flags, descriptions, etc). Showing that raw to an end user in a toast is
# noisy and unhelpful. This function extracts the meaningful first line and
# maps common error patterns to short, human-readable messages.
#
# For unknown errors: returns the first non-empty line, truncated to 200 chars.
# The raw error is still logged server-side via log.error() for postmortem.
def humanize_cli_error(raw):
    if not raw:
        return "Unknown error"
    # Take only the first non-empty line — strips the Usage: block that runpodctl
    # prints after actual errors, which otherwise dominates the output.
    first_line = ""
    for line in raw.splitlines():
        s = line.strip()
        if s:
            first_line = s
            break
    if not first_line:
        return "Unknown error"
    lower = first_line.lower()
    # Order matters: more specific patterns first, more generic last.
    if "does not have the resources to deploy" in lower or "no available machines" in lower:
        return "GPU временно недоступна на RunPod. Попробуйте через минуту-две"
    if "insufficient" in lower and ("balance" in lower or "credit" in lower or "funds" in lower):
        return "Недостаточно средств на балансе RunPod"
    if "not enough" in lower and ("balance" in lower or "credit" in lower):
        return "Недостаточно средств на балансе RunPod"
    if "rate limit" in lower or "429" in first_line or "too many requests" in lower:
        return "RunPod API rate limit, подождите несколько секунд"
    if "network volume" in lower and "not found" in lower:
        return "Network volume не найден"
    if ("image" in lower and "not found" in lower) or "pull" in lower and "failed" in lower or "manifest unknown" in lower:
        return "Docker образ недоступен для загрузки"
    if "gpu" in lower and ("not found" in lower or "invalid" in lower):
        return "Неизвестный тип GPU в конфигурации"
    if "unauthorized" in lower or "401" in first_line or "invalid api key" in lower:
        return "RunPod API key недействительный"
    if "timeout" in lower or "timed out" in lower:
        return "RunPod API не отвечает, попробуйте ещё раз"
    # Try to extract the 'error' field from a JSON-like blob: {"error":"..."}
    m = re.search(r'"error"\s*:\s*"([^"]+)"', first_line)
    if m:
        extracted = m.group(1).strip()
        return extracted[:200]
    # Unknown error — return first line truncated
    return first_line[:200]

# ============================================================
# Pod listing
# ============================================================
PODS_GQL = 'query{myself{pods{id name desiredStatus imageName gpuCount costPerHr machine{gpuDisplayName}runtime{uptimeInSeconds gpus{id gpuUtilPercent memoryUtilPercent}container{cpuPercent memoryPercent}}}}}'
def try_gql_bearer():
    try:
        resp = http_request("https://api.runpod.io/graphql", data={"query":PODS_GQL}, headers={"Authorization":f"Bearer {_api_key}"})
        if resp.get("errors"): raise Exception("")
        return [enrich_gql(p) for p in resp.get("data",{}).get("myself",{}).get("pods",[])]
    except Exception: return None
def try_gql_qp():
    try:
        resp = http_request(f"https://api.runpod.io/graphql?api_key={_api_key}", data={"query":PODS_GQL})
        if resp.get("errors"): raise Exception("")
        return [enrich_gql(p) for p in resp.get("data",{}).get("myself",{}).get("pods",[])]
    except Exception: return None
def try_rest():
    try:
        data = http_request("https://rest.runpod.io/v1/pods", headers={"Authorization":f"Bearer {_api_key}"})
        return [enrich_rest(p) for p in (data if isinstance(data,list) else [])]
    except Exception: return None
def try_cli():
    if _cli_is_new:
        res = run_cmd([_cli_path,"pod","list","--all","--output=json"])
        if res["ok"] and isinstance(res.get("data"),list): return [enrich_rest(p) for p in res["data"]]
    res = run_cmd([_cli_path,"get","pod","--allfields"] if not _cli_is_new else [_cli_path,"pod","list","--all"])
    if not res["ok"]: raise RuntimeError(res["error"])
    d = res["data"]
    if isinstance(d,str): d = parse_table(d)
    return [enrich_rest(p) for p in d] if isinstance(d,list) else []
def enrich_gql(p):
    rt=p.get("runtime") or {}; gpus=rt.get("gpus") or []; ctr=rt.get("container") or {}; mch=p.get("machine") or {}
    gu=max((g.get("gpuUtilPercent",0) or 0 for g in gpus),default=0)
    gm=max((g.get("memoryUtilPercent",0) or 0 for g in gpus),default=0)
    cu=ctr.get("cpuPercent",0) or 0; ru=ctr.get("memoryPercent",0) or 0
    st=(p.get("desiredStatus","") or "").upper()
    if not st: st="RUNNING" if rt.get("uptimeInSeconds") is not None else "EXITED"
    pid=p.get("id","")
    return {"id":pid,"name":p.get("name",""),"desiredStatus":st,"imageName":p.get("imageName",""),
            "gpuId":mch.get("gpuDisplayName",""),"gpuCount":p.get("gpuCount",0),
            "costPerHr":round(float(p.get("costPerHr",0) or 0),4),
            "comfyUrl":f"https://{pid}-{PRESET['comfy_port']}.proxy.runpod.net" if pid else "",
            "telemetry":{"gpuUtil":round(gu),"gpuMem":round(gm),"cpuUtil":round(cu),"ramUtil":round(ru)}}
def enrich_rest(p):
    pid=p.get("id","")
    return {"id":pid,"name":p.get("name",""),"desiredStatus":(p.get("desiredStatus","") or "").upper() or "RUNNING",
            "imageName":p.get("imageName",""),
            "gpuId":p.get("gpuId","") or p.get("gpuTypeId","") or (p.get("machine") or {}).get("gpuDisplayName",""),
            "gpuCount":p.get("gpuCount",0),"costPerHr":round(float(p.get("costPerHr",0) or 0),4),
            "comfyUrl":f"https://{pid}-{PRESET['comfy_port']}.proxy.runpod.net" if pid else "",
            "telemetry":{"gpuUtil":0,"gpuMem":0,"cpuUtil":0,"ramUtil":0}}
def parse_table(text):
    lines=text.strip().splitlines(); pods=[]; headers=[]
    for line in lines:
        s=line.strip()
        if s.startswith("+") or s.startswith("-") or not s or "|" not in s: continue
        cells=[c.strip() for c in s.split("|")]
        if cells and cells[0]=="": cells=cells[1:]
        if cells and cells[-1]=="": cells=cells[:-1]
        if not cells: continue
        if not headers: headers=[h.upper() for h in cells]; continue
        row={headers[i]:cells[i] for i in range(min(len(headers),len(cells)))}
        pid=row.get("ID","").strip()
        if not pid: continue
        gc,gi=0,row.get("GPU","")
        m=re.match(r"^(\d+)\s+(.+)$",gi.strip())
        if m: gc,gi=int(m.group(1)),m.group(2).strip()
        pods.append({"id":pid,"name":row.get("NAME",""),"desiredStatus":row.get("STATUS","RUNNING").upper(),"gpuId":gi,"gpuCount":gc,"costPerHr":0})
    return pods

def list_pods():
    if _api_key:
        for fn in [try_gql_bearer, try_gql_qp, try_rest]:
            r = fn()
            if r is not None:
                pods = r
                break
        else:
            pods = try_cli()
    else:
        pods = try_cli()
    # Augment with service health
    running_ids = [p["id"] for p in pods if p.get("desiredStatus")=="RUNNING" and p.get("id")]
    health = check_pods_services_parallel(running_ids)
    # Fetch boot status only for running pods that are NOT yet ready (saves requests)
    not_ready_ids = [pid for pid in running_ids if not health.get(pid, False)]
    boot_statuses = check_pods_boot_parallel(not_ready_ids)
    # Fetch runtime activity ONLY for ready pods (boot-stage pods have no meaningful activity)
    ready_ids = [pid for pid in running_ids if health.get(pid, False)]
    runtime_statuses = check_pods_runtime_parallel(ready_ids)
    # Augment with creator info
    all_ids = [p["id"] for p in pods if p.get("id")]
    creators = get_pod_creators(all_ids)
    # Get timer data
    timers = timer_get_all(all_ids)
    # Batch-fetch per-pod assignments (pod_id -> {assigned_project,
    # counts_toward_quota, creation_source}). Pods without a row in
    # pod_assignment have no assignment — surfaced to the caller as nulls,
    # equivalent to 'unassigned' / admin-only visibility.
    assignments = get_assignments_batch(all_ids)
    s = get_settings()
    idle_timeout_min = s.get("idle_timeout_minutes", 120)
    for p in pods:
        pid = p.get("id","")
        is_running = p.get("desiredStatus")=="RUNNING"
        svc_ready = health.get(pid, False) if is_running else False
        p["serviceReady"] = svc_ready
        # Boot status — only relevant for running-but-not-ready pods
        if is_running and not svc_ready:
            bs = boot_statuses.get(pid)
            if bs:
                p["bootStage"] = bs["stage"]
                p["bootPct"] = bs["pct"]
                p["bootMsg"] = bs["msg"]
                p["bootElapsed"] = bs["elapsed"]
            else:
                p["bootStage"] = None
                p["bootPct"] = None
                p["bootMsg"] = None
                p["bootElapsed"] = None
        else:
            p["bootStage"] = None
            p["bootPct"] = None
            p["bootMsg"] = None
            p["bootElapsed"] = None
        # Runtime activity — only meaningful when ComfyUI is ready
        rt = runtime_statuses.get(pid) if (is_running and svc_ready) else None
        if rt:
            p["runtimeActive"] = rt["active"]
            p["runtimeQueueDepth"] = rt["queue_depth"]
            p["runtimeLastEvent"] = rt["last_event"]
            p["runtimeLastEventAt"] = rt["last_event_at"]
            p["runtimeLastDuration"] = rt["last_completed_duration"]
            p["runtimeTotalCompleted"] = rt["total_completed"]
            p["runtimeTotalStarted"] = rt["total_started"]
        else:
            p["runtimeActive"] = None
            p["runtimeQueueDepth"] = None
            p["runtimeLastEvent"] = None
            p["runtimeLastEventAt"] = None
            p["runtimeLastDuration"] = None
            p["runtimeTotalCompleted"] = None
            p["runtimeTotalStarted"] = None
        c = creators.get(pid)
        if c:
            p["createdBy"] = c["nickname"]
            p["createdProject"] = c["project"]
            p["createdAt"] = c["ts"]
        else:
            p["createdBy"] = ""
            p["createdProject"] = ""
            p["createdAt"] = ""

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

        # ===== TIMER LOGIC =====
        # Timer ONLY exists/runs if ComfyUI has become ready at least once.
        # 1. If service is ready and no timer yet → initialize (this is "the moment ComfyUI started")
        # 2. If service is ready and pod is busy → touch timer (reset countdown)
        # 3. If service is ready and pod is idle → just let timer keep ticking
        # 4. If service NOT ready → do nothing (timer stays absent or frozen)
        #
        # "Busy" preference order:
        #   a) runtimeActive from /runtime.json (most accurate — event-based)
        #   b) telemetry gpuUtil/cpuUtil (fallback if runtime watcher unavailable)
        if is_running and svc_ready:
            if pid not in timers:
                timer_init_if_missing(pid)
                _ts = now_iso()
                timers[pid] = {"last_active": _ts, "created_at": _ts}
            if rt is not None:
                # Authoritative source: ComfyUI log via runtime watcher
                is_busy = bool(rt["active"])
            else:
                # Fallback: telemetry from RunPod GraphQL
                t = p.get("telemetry") or {}
                is_busy = (t.get("gpuUtil",0) > 0) or (t.get("cpuUtil",0) > 0)
            if is_busy:
                timer_touch(pid)
                timers[pid] = {"last_active": now_iso(),
                               "created_at": timers[pid]["created_at"]}

        # Compute idle info to send to frontend
        tinfo = timers.get(pid)
        if tinfo and is_running and svc_ready:
            last = parse_iso(tinfo["last_active"])
            if last is not None:
                idle_sec = int((now_utc() - last).total_seconds())
                p["idleSeconds"] = max(0, idle_sec)
                p["idleTimeoutMinutes"] = idle_timeout_min
                p["lastActiveAt"] = tinfo["last_active"]
            else:
                p["idleSeconds"] = None
        else:
            # Either pod not running, or ComfyUI not ready yet → no timer to show
            p["idleSeconds"] = None
    return pods

# ============================================================
# GraphQL pod creation (primary path — same endpoint the RunPod UI uses)
# ============================================================
# We discovered through F12 inspection that the RunPod web UI creates pods via
# a GraphQL mutation 'DeployOnDemand' on https://api.runpod.io/graphql.
# This is a separate code path from runpodctl 'pod create', and importantly
# RunPod's CLI has been observed to fail with 'no resources' errors for GPU
# types that the GraphQL endpoint accepts without complaint (notably newer
# Blackwell-class GPUs like RTX PRO 4500). The two paths likely talk to
# different backend services on RunPod's side.
#
# By using the same mutation as the UI we get the same reliability as the UI.
# CLI is kept as a fallback in create_pod() in case GraphQL ever changes.
#
# Note: Cloudflare in front of the RunPod API blocks requests with default
# Python urllib User-Agent (returns 'error code: 1010'). We must send a
# meaningful UA — 'RunPod-Manager/6.0' is what we already use successfully
# in try_gql_bearer for listing pods.
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

DEPLOY_MUTATION = """mutation DeployOnDemand($input: PodFindAndDeployOnDemandInput) {
  podFindAndDeployOnDemand(input: $input) {
    id
    imageName
  }
}"""

def create_pod_via_graphql(name, template_id=None):
    """Create a pod via the same GraphQL mutation that the RunPod UI uses.
    Returns {id, name, imageName} on success, raises RuntimeError on any failure.
    Caller (create_pod) is responsible for falling back to CLI if this fails."""
    if not _api_key:
        raise RuntimeError("GraphQL deploy unavailable: no API key configured")

    variables = {
        "input": {
            "cloudType": PRESET["cloud_type"],
            "containerDiskInGb": PRESET["container_disk_in_gb"],
            "dataCenterId": PRESET["data_center_id"],
            "globalNetwork": PRESET["global_network"],
            "gpuCount": PRESET["gpu_count"],
            "gpuTypeId": PRESET["gpu_id"],
            "minMemoryInGb": PRESET["min_memory_in_gb"],
            "minVcpuCount": PRESET["min_vcpu_count"],
            "name": name,
            "networkVolumeId": PRESET["network_volume_id"],
            "ports": PRESET["ports"],
            "startJupyter": PRESET["start_jupyter"],
            "startSsh": PRESET["start_ssh"],
            "templateId": template_id or PRESET["template_id"],
            "volumeInGb": PRESET["volume_in_gb"],
            "volumeKey": None,
        }
    }

    payload = {
        "operationName": "DeployOnDemand",
        "query": DEPLOY_MUTATION,
        "variables": variables,
    }

    # The ?operation= URL param mirrors the UI request exactly. Not strictly required
    # by the GraphQL spec but RunPod's edge router may use it for routing/metrics.
    url = "https://api.runpod.io/graphql?operation=DeployOnDemand"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_api_key}",
        "User-Agent": "RunPod-Manager/6.0",
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"GraphQL HTTP {e.code}: {body_text[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"GraphQL network error: {e}")
    except Exception as e:
        raise RuntimeError(f"GraphQL request failed: {type(e).__name__}: {e}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GraphQL returned invalid JSON: {text[:200]}")

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

    pod = (data.get("data") or {}).get("podFindAndDeployOnDemand")
    if not pod or not pod.get("id"):
        raise RuntimeError(f"GraphQL: empty response (got {text[:200]})")

    return {
        "id": pod["id"],
        "name": name,
        "imageName": pod.get("imageName", ""),
    }

# ============================================================
# Pod operations
# ============================================================
def create_pod(name, bypass_window=False, template_id=None):
    s = get_settings()
    # Check pod creation restriction window unless caller explicitly bypasses (admin).
    # Strategy A: restriction only blocks NEW pods, existing pods keep running regardless.
    # 'from/until' define when creation is FORBIDDEN; outside that window it's allowed.
    if not bypass_window:
        w = check_pod_window()
        if not w["is_open"]:
            raise RuntimeError(f"Запуск подов ограничен. Снова будет доступен в {w['until']} UTC.")
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

    # ===== PRIMARY PATH: GraphQL DeployOnDemand mutation =====
    # This uses the same endpoint as the RunPod web UI and is much more reliable
    # than 'runpodctl pod create' which often fails with 'no resources' for newer GPUs.
    # If GraphQL fails for any reason, we fall through to the CLI path below as a
    # safety net (it has its own bugs but at least it's an independent code path).
    if _api_key:
        try:
            log.info(f"Creating pod {name!r} via GraphQL DeployOnDemand mutation")
            return create_pod_via_graphql(name, template_id=template_id)
        except GpuUnavailableError:
            # No GPU available right now. The CLI path also fails on scarcity,
            # so don't bother — surface the retryable error to the caller.
            log.info(f"GraphQL deploy: no GPU available for {name!r}")
            raise
        except Exception as e:
            log.warning(f"GraphQL deploy failed for {name!r}: {e}. Falling back to CLI.")
            # fall through to CLI path below

    # ===== FALLBACK PATH: runpodctl pod create =====
    log.info(f"Creating pod {name!r} via runpodctl CLI (fallback)")
    if _cli_is_new:
        cmd=[_cli_path,"pod","create","--cloud-type",PRESET["cloud_type"],"--gpu-id",PRESET["gpu_id"],
             "--gpu-count",str(PRESET["gpu_count"]),"--name",name,"--image",PRESET["image"],
             "--container-disk-in-gb",str(PRESET["container_disk_in_gb"]),
             "--volume-mount-path",PRESET["volume_mount_path"],"--volume-in-gb",str(PRESET["volume_in_gb"])]
        tid = template_id or PRESET.get("template_id")
        if tid: cmd+=["--template-id",tid]
        if PRESET.get("network_volume_id"): cmd+=["--network-volume-id",PRESET["network_volume_id"]]
        if PRESET.get("env"): cmd+=["--env",json.dumps(PRESET["env"])]
    else:
        cmd=[_cli_path,"create","pod","--secureCloud" if PRESET["cloud_type"]=="SECURE" else "--communityCloud",
             "--gpuType",PRESET["gpu_id"],"--gpuCount",str(PRESET["gpu_count"]),"--name",name,
             "--imageName",PRESET["image"],"--containerDiskSize",str(PRESET["container_disk_in_gb"]),
             "--volumePath",PRESET["volume_mount_path"],"--volumeSize",str(PRESET["volume_in_gb"])]
        tid = template_id or PRESET.get("template_id")
        if tid: cmd+=["--templateId",tid]
        if PRESET.get("network_volume_id"): cmd+=["--networkVolumeId",PRESET["network_volume_id"]]
        if PRESET.get("env"):
            for k,v in PRESET["env"].items(): cmd+=["--env",f"{k}={v}"]
    res=run_cmd(cmd,60)
    if not res["ok"]:
        # Log raw error for postmortem, show humanized version to user
        log.error(f"create_pod CLI fallback also failed, raw error: {res['error']}")
        if is_gpu_unavailable_error(res["error"]):
            raise GpuUnavailableError(humanize_cli_error(res["error"]))
        raise RuntimeError(humanize_cli_error(res["error"]))
    d=res["data"]
    if isinstance(d,dict): return d
    if isinstance(d,str):
        m=re.search(r'pod\s+"([^"]+)"\s+created',d)
        if m: return {"id":m.group(1),"name":name}
    return {"name":name}
def delete_pod(pid):
    r=run_cmd([_cli_path,"pod","delete",pid] if _cli_is_new else [_cli_path,"remove","pod",pid])
    if not r["ok"]: raise RuntimeError(r["error"])
    with _service_cache_lock:
        _service_cache.pop(pid, None)
    with _boot_cache_lock:
        _boot_cache.pop(pid, None)
    with _runtime_cache_lock:
        _runtime_cache.pop(pid, None)
    timer_delete(pid)
    # Also clean up pod_assignment, otherwise a freshly-created pod could inherit
    # the assignment from a deleted one if pod IDs were ever recycled. Cheap
    # insurance — delete_pod_assignment is a no-op if the row doesn't exist.
    delete_pod_assignment(pid)
def start_pod(pid):
    r=run_cmd([_cli_path,"pod","start",pid] if _cli_is_new else [_cli_path,"start","pod",pid])
    if not r["ok"]: raise RuntimeError(r["error"])
    # Reset timer on restart — but only if it already existed (i.e. ComfyUI was ready before)
    # Actually safer: delete it, so it gets re-initialized when ComfyUI becomes ready again
    with _boot_cache_lock:
        _boot_cache.pop(pid, None)
    with _runtime_cache_lock:
        _runtime_cache.pop(pid, None)
    timer_delete(pid)
def pod_name_prefix(project):
    """Return the pod-name prefix for a given project (or None for unassigned).
    CV -> 'cv_pod_', DV -> 'dv_pod_', ADMIN -> 'admin_pod_', ..., None -> 'pod_'.
    Nummeration is per-prefix so names don't collide across projects."""
    return f"{project.lower()}_pod_" if project else PRESET['pod_name_prefix']

def next_name(pods, project=None):
    """Next free name within the given project's namespace. If project is None,
    uses the legacy global 'pod_' prefix (unassigned pods)."""
    prefix = pod_name_prefix(project)
    pat=re.compile(rf"^{re.escape(prefix)}(\d+)$")
    mx=max((int(pat.match(p.get('name','')).group(1)) for p in pods if pat.match(p.get('name',''))),default=0)
    return f"{prefix}{mx+1}"

def delete_all_pods(source="manual"):
    try:
        pods = list_pods(); running = [p for p in pods if p.get("desiredStatus")=="RUNNING"]
        if not running: return 0, "No running pods"
        ok = 0
        nick = "AUTODELETE" if source=="auto" else "ADMIN"
        label = "autodelete" if source=="auto" else "delete"
        for p in running:
            try:
                delete_pod(p["id"]); ok+=1
                log_action(nick, "[SYSTEM]", label, p.get("name",""), p["id"])
            except Exception as e:
                log.error(f"Failed to delete {p.get('name','')}: {e}")
        return ok, f"Deleted {ok}/{len(running)}"
    except Exception as e: return 0, str(e)

def delete_project_pods(project):
    """Delete RUNNING pods scoped to a single project (or None = unassigned
    bucket, i.e. pods with assigned_project IS NULL). Used by the scheduler's
    per-project daily auto-delete. Returns (count_deleted, message)."""
    try:
        pods = list_pods()
        # Match by assignedProject — includes pods with counts_toward_quota=0
        # (the flag is about quota accounting, not cleanup scope).
        target = [p for p in pods
                  if p.get("desiredStatus") == "RUNNING"
                  and p.get("assignedProject") == project]
        if not target:
            return 0, f"No running pods in {project or 'unassigned'}"
        ok = 0
        for p in target:
            try:
                delete_pod(p["id"]); ok += 1
                log_action("AUTODELETE", "[SYSTEM]", "autodelete",
                           p.get("name",""), p["id"])
            except Exception as e:
                log.error(f"Failed to delete {p.get('name','')}: {e}")
        return ok, f"{project or 'unassigned'}: {ok}/{len(target)}"
    except Exception as e:
        return 0, str(e)

def check_idle_timeouts():
    """Run by scheduler. Delete pods that have been idle longer than threshold."""
    try:
        s = get_settings()
        if not s.get("idle_timeout_enabled"): return
        timeout_min = s.get("idle_timeout_minutes", 120)
        if timeout_min < 1: return
        pods = list_pods()
        running = [p for p in pods if p.get("desiredStatus")=="RUNNING"]
        if not running: return
        threshold = timeout_min * 60
        for p in running:
            idle = p.get("idleSeconds")
            if idle is None: continue  # ComfyUI not ready yet — skip
            if idle >= threshold:
                pid = p["id"]; pname = p.get("name","")
                log.info(f"⏱  Idle timeout: {pname} idle for {idle}s (limit {threshold}s) → deleting")
                try:
                    delete_pod(pid)
                    log_action("IDLE_TIMEOUT", "[SYSTEM]", "pod usage timeout auto deleting", pname, pid)
                except Exception as e:
                    log.error(f"Idle delete failed for {pname}: {e}")
    except Exception as e:
        log.error(f"check_idle_timeouts: {e}")

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
                tid = resolve_template_id(req["assigned_project"])
                result = create_pod_via_graphql(req["pod_name"], template_id=tid)
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


# ============================================================
# Scheduler
# ============================================================
def scheduler_loop():
    while True:
        try:
            s = get_settings()
            if s.get("auto_delete_enabled") and s.get("auto_delete_time"):
                # auto_delete_time is interpreted as UTC — admin UI labels it as such.
                # Per-project offsets (in minutes) shift each project's fire time.
                # Unassigned pods always fire at the base time (offset=0).
                now = now_utc(); today = now.strftime("%Y-%m-%d")
                h,m = map(int, s["auto_delete_time"].split(":"))
                base_total = h * 60 + m
                offsets = s.get("project_autodelete_offset_minutes") or {}
                last_run = dict(s.get("project_autodelete_last_run") or {})
                fires = []  # (project_or_None, count_msg) collected this tick

                def _effective(off_min):
                    total = (base_total + int(off_min or 0)) % 1440
                    return total // 60, total % 60

                # Per-project fires
                for proj in PROJECTS:
                    try:
                        off = int(offsets.get(proj, 0) or 0)
                    except (TypeError, ValueError):
                        off = 0
                    eff_h, eff_m = _effective(off)
                    if now.hour == eff_h and now.minute == eff_m and last_run.get(proj) != today:
                        log.info(f"⏰ Auto-delete {proj} at {eff_h:02d}:{eff_m:02d} UTC (offset {off}m)")
                        _cnt, msg = delete_project_pods(proj)
                        last_run[proj] = today
                        fires.append(msg)

                # Unassigned bucket — always at base time, offset=0.
                if now.hour == h and now.minute == m and last_run.get("__unassigned__") != today:
                    log.info(f"⏰ Auto-delete unassigned at {h:02d}:{m:02d} UTC")
                    _cnt, msg = delete_project_pods(None)
                    last_run["__unassigned__"] = today
                    fires.append(msg)

                if fires:
                    s2 = get_settings()
                    s2["project_autodelete_last_run"] = last_run
                    # Keep the legacy fields updated for admin UI "Last: ..." display.
                    s2["auto_delete_last_run"] = today
                    s2["auto_delete_last_log"] = f"[{now_iso()}] " + "; ".join(fires)
                    save_settings(s2)
            check_idle_timeouts()
        except Exception as e:
            log.error(f"scheduler_loop: {e}")
        _time.sleep(30)

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

# ============================================================
# Routes
# ============================================================
@app.route("/api/projects")
def api_projects(): return jsonify({"ok":True,"projects":PROJECTS})

@app.route("/api/user/register", methods=["POST"])
def api_user_register():
    d=request.get_json() or {}
    try:
        nick, proj = validate_user_input(d.get("nickname",""), d.get("project",""))
    except UserValidationError as e:
        return jsonify({"ok":False,"error":str(e)}),400
    touch_user(nick, proj)
    # Bind identity to the Flask session — all subsequent /api/pods* calls
    # will read nickname/project from the session, not from request body.
    session["user_nickname"] = nick
    session["user_project"] = proj
    session.permanent = True
    return jsonify({"ok":True,"nickname":nick,"project":proj})

@app.route("/api/user/check")
def api_user_check():
    """Check if the current session has a registered user. Used by the frontend
    on page load to decide whether to show the login screen or proceed.
    Returns 403 (not 401) to avoid triggering basic-auth popups when behind a
    reverse proxy — see require_user docstring for the full rationale."""
    nick, proj = get_session_user()
    if not nick or not proj:
        return jsonify({"ok":False,"error":"Not registered"}),403
    return jsonify({"ok":True,"nickname":nick,"project":proj})

@app.route("/api/user/logout", methods=["POST"])
def api_user_logout():
    """Clear the user identity from session. Called by the 'change user' UI action.
    Note: this does NOT clear admin status — admin sessions are independent."""
    session.pop("user_nickname", None)
    session.pop("user_project", None)
    return jsonify({"ok":True})

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
        # Quota usage = running pods + pending requests, both counting toward quota.
        project_running = project_quota_usage(viewer_project, pods=all_pods)
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
        req_rows = list_visible_requests(viewer_project, viewer_is_admin)
        requests_payload = [{
            "id": r["id"],
            "name": r["pod_name"],
            "assignedProject": r["assigned_project"],
            "status": r["status"],
            "lastError": r["last_error"],
            "createdAt": r["created_at"],
        } for r in req_rows]
        return jsonify({"ok": True, "pods": pods,
                        "requests": requests_payload,
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
        # Reserve a name against BOTH real pods and pending requests so a direct
        # create and a queued "заявка" never collide on a name.
        reserved = pods + [{"name": n} for n in pending_request_names()]
        name = next_name(reserved, ap)
        # Admins bypass window + per-project quota; regular users are checked inside create_pod
        result = create_pod(name, bypass_window=admin,
                            template_id=resolve_template_id(ap))
        pid = result.get("id", "") if isinstance(result, dict) else ""
        # Write the assignment row BEFORE logging the create action. If the upsert
        # raises, the pod exists on RunPod but has no assignment — surface the
        # error with the pid so admin can recover via /assign. We still want the
        # audit trail for the create itself, so log unconditionally after.
        if pid:
            try:
                upsert_pod_assignment(pid, ap, cf, src, nick)
            except Exception as e:
                log_action(nick, proj, "create", name, pid)
                return jsonify({"ok":False,
                                "error":f"Pod created (id={pid}) but assignment failed: {e}. Admin must /assign."}), 500
        log_action(nick, proj, "create", name, pid)
        return jsonify({"ok":True,"name":name,
                        "comfyUrl":f"https://{pid}-{PRESET['comfy_port']}.proxy.runpod.net" if pid else None})
    except GpuUnavailableError as e:
        # Not a hard error — the GPU may free up. Signal the frontend to offer
        # the user a "leave a request?" dialog instead of a scary red toast.
        return jsonify({"ok": False, "gpuUnavailable": True, "error": str(e)}), 200
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),500

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

        # Fetch the pod list once and reuse it for both the quota check and the
        # name reservation below — list_pods() is a network call to RunPod.
        pods = list_pods()

        # Window + quota are enforced once, at request-creation time (admins bypass).
        if not admin:
            w = check_pod_window()
            if not w["is_open"]:
                return jsonify({"ok": False,
                                "error": f"Запуск подов ограничен. Снова будет доступен в {w['until']} UTC."}), 400
            quotas = get_settings().get("project_quotas") or {}
            quota = quotas.get(ap, DEFAULT_PROJECT_QUOTA)
            used = project_quota_usage(ap, pods=pods)
            if used >= quota:
                return jsonify({"ok": False,
                                "error": f"Достигнут лимит {ap}: {used}/{quota}"}), 400

        reserved = pods + [{"name": n} for n in pending_request_names()]
        name = next_name(reserved, ap)
        rid = create_pod_request(name, ap, cf, src, nick)
        log_action(nick, proj, "request", name, "")
        return jsonify({"ok": True, "request": {
            "id": rid, "name": name, "status": "pending", "assignedProject": ap}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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

@app.route("/api/pods/<pid>", methods=["DELETE"])
@require_user
def api_del(pid):
    try:
        # Identity comes from session, not from body. Prevents anonymous deletes.
        nick, proj = g.current_user
        # Non-admin can only act on pods assigned to their own project.
        # Pods with no assignment or assigned to another project are invisible
        # (404) to keep existence private — even showing a different error
        # leaks info about admin-only pods.
        if not is_admin():
            a = get_pod_assignment(pid)
            if a is None or a["assigned_project"] != proj:
                return jsonify({"ok":False,"error":"Pod not found"}),404
        pods=list_pods(); pname=next((p["name"] for p in pods if p["id"]==pid), pid)
        delete_pod(pid)
        log_action(nick, proj, "delete", pname, pid)
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/pods/<pid>/start", methods=["POST"])
@require_user
def api_start(pid):
    try:
        nick, proj = g.current_user
        # Non-admin can only act on pods assigned to their own project.
        # Pods with no assignment or assigned to another project are invisible
        # (404) to keep existence private — even showing a different error
        # leaks info about admin-only pods.
        if not is_admin():
            a = get_pod_assignment(pid)
            if a is None or a["assigned_project"] != proj:
                return jsonify({"ok":False,"error":"Pod not found"}),404
        # Resolve pod name for the activity log before starting
        pods=list_pods(); pname=next((p["name"] for p in pods if p["id"]==pid), pid)
        start_pod(pid)
        log_action(nick, proj, "start", pname, pid)
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    pw=(request.get_json() or {}).get("password","")
    if pw==get_settings().get("admin_password",""): session["admin"]=True; session.permanent=True; return jsonify({"ok":True})
    return jsonify({"ok":False,"error":"Wrong password"}),403

@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout(): session.pop("admin",None); return jsonify({"ok":True})

@app.route("/api/admin/check")
def api_admin_check(): return jsonify({"ok":True,"admin":is_admin()})

@app.route("/api/admin/settings", methods=["GET"])
@require_admin
def api_admin_settings_get():
    s=get_settings()
    # Ensure project_quotas has entries for every current PROJECT (handles
    # post-migration sessions where a new project was added in code)
    quotas = dict(s.get("project_quotas") or {})
    for p in PROJECTS:
        if p not in quotas:
            quotas[p] = DEFAULT_PROJECT_QUOTA
    # Same backfill for per-project auto-delete offsets (minutes).
    offsets = dict(s.get("project_autodelete_offset_minutes") or {})
    for p in PROJECTS:
        if p not in offsets:
            offsets[p] = 0
    catalog = s.get("pod_image_catalog") or list(DEFAULT_SETTINGS["pod_image_catalog"])
    return jsonify({"ok":True,"settings":{
        "project_quotas": quotas,
        "project_autodelete_offset_minutes": offsets,
        "pod_image_catalog": catalog,
        "default_pod_image": s.get("default_pod_image") or DEFAULT_SETTINGS["default_pod_image"],
        "project_pod_image": s.get("project_pod_image") or {},
        **{k:s.get(k) for k in ["auto_delete_enabled","auto_delete_time","auto_delete_last_log","idle_timeout_enabled","idle_timeout_minutes","pod_window_enabled","pod_window_from","pod_window_until","pod_request_timeout_minutes","pod_request_retry_interval_seconds"]}
    }})

@app.route("/api/admin/settings", methods=["POST"])
@require_admin
def api_admin_settings_post():
    data=request.get_json() or {}; s=get_settings()
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
    # Per-project auto-delete offset in minutes. 0-1440. Unknown project keys ignored.
    if isinstance(data.get("project_autodelete_offset_minutes"), dict):
        offsets = dict(s.get("project_autodelete_offset_minutes") or {})
        for proj, val in data["project_autodelete_offset_minutes"].items():
            if proj not in PROJECTS:
                continue
            try:
                offsets[proj] = max(0, min(1440, int(val)))
            except (TypeError, ValueError):
                pass
        s["project_autodelete_offset_minutes"] = offsets
    # Per-project pod image selection (catalog + default + project map).
    s.update(compute_image_settings_update(data, s))
    if "new_password" in data and data["new_password"].strip(): s["admin_password"]=data["new_password"].strip()
    if "auto_delete_enabled" in data: s["auto_delete_enabled"]=bool(data["auto_delete_enabled"])
    if "auto_delete_time" in data:
        t=data["auto_delete_time"].strip()
        if re.match(r"^\d{1,2}:\d{2}$",t): s["auto_delete_time"]=t
    if "idle_timeout_enabled" in data: s["idle_timeout_enabled"]=bool(data["idle_timeout_enabled"])
    if "idle_timeout_minutes" in data:
        try: s["idle_timeout_minutes"]=max(1,min(1440,int(data["idle_timeout_minutes"])))
        except (TypeError, ValueError): pass
    # Pod creation window settings
    if "pod_window_enabled" in data: s["pod_window_enabled"]=bool(data["pod_window_enabled"])
    if "pod_window_from" in data:
        t=data["pod_window_from"].strip()
        if re.match(r"^\d{1,2}:\d{2}$",t): s["pod_window_from"]=t
    if "pod_window_until" in data:
        t=data["pod_window_until"].strip()
        if re.match(r"^\d{1,2}:\d{2}$",t): s["pod_window_until"]=t
    # Auto-retry "заявка на под" tuning
    if "pod_request_timeout_minutes" in data:
        try: s["pod_request_timeout_minutes"]=max(1,min(1440,int(data["pod_request_timeout_minutes"])))
        except (TypeError, ValueError): pass
    if "pod_request_retry_interval_seconds" in data:
        try: s["pod_request_retry_interval_seconds"]=max(5,min(600,int(data["pod_request_retry_interval_seconds"])))
        except (TypeError, ValueError): pass
    save_settings(s); return jsonify({"ok":True})

@app.route("/api/admin/delete-all", methods=["POST"])
@require_admin
def api_admin_delete_all():
    cnt,msg = delete_all_pods(source="manual")
    s=get_settings(); s["auto_delete_last_log"]=f"[{now_iso()}] Manual: {msg}"; save_settings(s)
    return jsonify({"ok":True,"deleted":cnt,"message":msg})

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

@app.route("/api/admin/activity")
@require_admin
def api_admin_activity():
    try:
        db=get_db(); q="SELECT id,nickname,project,action,pod_name,pod_id,ts FROM pod_actions"
        params=[]; conditions=[]
        f=request.args.get("from",""); t=request.args.get("to","")
        if f: conditions.append("date(ts)>=?"); params.append(f)
        if t: conditions.append("date(ts)<=?"); params.append(t)
        if conditions: q+=" WHERE "+" AND ".join(conditions)
        q+=" ORDER BY ts DESC LIMIT 500"
        rows=db.execute(q,params).fetchall()
        return jsonify({"ok":True,"actions":[dict(r) for r in rows]})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500

# ============================================================
# Frontend
# ============================================================
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RunPod Manager</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0f;--c1:#111118;--c2:#181822;--bd:#1c1c2a;--bd2:#2a2a3e;--t:#e2e2ef;--t2:#7a7a95;--t3:#50506a;--ac:#6ee7b7;--ac2:#5fd6a6;--dg:#f87171;--wr:#fbbf24;--in:#60a5fa;--mono:'JetBrains Mono',monospace;--sans:'DM Sans',sans-serif;--r:10px;--rs:6px}
body{font-family:var(--sans);background:var(--bg);color:var(--t);min-height:100vh}
.sh{max-width:980px;margin:0 auto;padding:36px 24px 100px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:32px;padding-bottom:20px;border-bottom:1px solid var(--bd);flex-wrap:wrap;gap:16px}
.logo{display:flex;align-items:center;gap:12px}
.li{width:34px;height:34px;background:linear-gradient(135deg,var(--ac),#34d399);border-radius:8px;display:grid;place-items:center;font-size:17px;color:#0a0a0f;font-weight:700;font-family:var(--mono)}
.logo h1{font-family:var(--mono);font-size:18px;font-weight:600}.logo h1 span{color:var(--t2);font-weight:400}
.btn{font-family:var(--mono);font-size:12px;font-weight:500;border:1px solid var(--bd);background:0;border-radius:var(--rs);padding:8px 16px;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px;color:var(--t2);white-space:nowrap}
.btn:hover:not(:disabled){border-color:var(--bd2);color:var(--t)}.btn:disabled{opacity:.35;cursor:not-allowed}
.bp{background:var(--ac);color:#0a0a0f;border-color:var(--ac)}.bp:hover:not(:disabled){background:var(--ac2);transform:translateY(-1px)}
.bs{font-size:11px;padding:5px 10px}
.bd{background:rgba(248,113,113,.08);color:var(--dg);border-color:rgba(248,113,113,.18)}
.bg{background:rgba(110,231,183,.08);color:var(--ac);border-color:rgba(110,231,183,.18)}
.badge{font-family:var(--mono);font-size:11px;color:var(--t2);background:var(--c1);border:1px solid var(--bd);border-radius:var(--rs);padding:5px 12px}
.ld{width:6px;height:6px;border-radius:50%;background:var(--ac);display:inline-block;margin-right:6px;animation:pu 2s infinite}@keyframes pu{50%{opacity:.3}}
.pl{display:flex;flex-direction:column;gap:6px}
.pc{background:var(--c1);border:1px solid var(--bd);border-radius:var(--r);padding:14px 18px;transition:all .15s}
.pc:hover{background:var(--c2);border-color:var(--bd2)}
/* Hidden pod (admin view only — regular users don't see these at all).
   Yellow border matches other warning-state cues in the app (busy tag, etc).
   Opacity dims the whole card so the admin immediately spots which pods are
   off-limits to regular users, without reading any text. */
.pc.pc-unassigned{border-color:var(--wr);opacity:0.6}
.pc.pc-unassigned:hover{opacity:0.85;border-color:var(--wr)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;display:flex;align-items:center;justify-content:center}
.modal-body{background:var(--bg);padding:20px;border-radius:8px;min-width:320px;max-width:400px}
.modal-body h3{margin:0 0 12px 0}
.pbadges{display:flex;gap:4px;flex-wrap:wrap;margin:4px 0}
.pbadge{display:inline-block;padding:2px 6px;border-radius:10px;font-size:10px;font-weight:600;line-height:1.2}
.pb-proj{background:#2a4a7a;color:#fff}
.pb-admin{background:#4a3a7a;color:#fff}
.pb-ext{background:#7a4a2a;color:#fff}
.pb-unassigned{background:#7a7a2a;color:#fff}
.pb-nocount{background:#555;color:#fff;padding:2px 5px}
.pc-admin-created{border-left:3px solid #7a4a7a}
.pc-external{border-left:3px solid #7a5a2a}
.pc-main{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center}
.pc-info{display:flex;flex-direction:column;gap:6px;min-width:0}
.pn{font-family:var(--mono);font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sd{width:7px;height:7px;border-radius:50%;flex-shrink:0}.sd.r{background:var(--ac);box-shadow:0 0 8px rgba(110,231,183,.5)}.sd.e{background:var(--t3)}.sd.x{background:var(--dg)}
.info-btn{width:18px;height:18px;border-radius:50%;border:1px solid var(--bd);background:transparent;color:var(--t3);font-family:var(--mono);font-size:11px;font-weight:600;cursor:pointer;display:inline-grid;place-items:center;transition:all .15s;padding:0;font-style:italic}
.info-btn:hover{border-color:var(--ac);color:var(--ac)}
.info-btn.open{background:var(--ac);color:#0a0a0f;border-color:var(--ac)}
.creator-line{font-family:var(--sans);font-size:12px;color:var(--t2);display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.creator-line .who{color:var(--in);font-weight:500;font-family:var(--mono)}
.creator-line .pj{color:var(--ac);font-family:var(--mono);font-size:11px;padding:1px 6px;border:1px solid rgba(110,231,183,.2);border-radius:4px}
.creator-line .when{color:var(--t3);font-family:var(--mono);font-size:11px}
.creator-line .nobody{color:var(--t3);font-style:italic}
.svc-tag{font-family:var(--mono);font-size:10px;font-weight:500;padding:2px 8px;border-radius:4px;display:inline-flex;align-items:center;gap:5px}
.svc-tag.ready{background:rgba(110,231,183,.08);color:var(--ac);border:1px solid rgba(110,231,183,.18)}
.svc-tag.init{background:rgba(96,165,250,.08);color:var(--in);border:1px solid rgba(96,165,250,.18)}
.svc-tag .svc-dot{width:6px;height:6px;border-radius:50%}
.svc-tag.ready .svc-dot{background:var(--ac);box-shadow:0 0 6px rgba(110,231,183,.6)}
.svc-tag.init .svc-dot{background:var(--in);animation:pu 1.5s infinite}
/* ===== Boot progress bar ===== */
.boot-prog{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(96,165,250,.06);border:1px solid rgba(96,165,250,.18);min-width:180px}
.boot-prog .bp-bar{position:relative;flex:1;height:6px;background:rgba(96,165,250,.12);border-radius:3px;overflow:hidden;min-width:80px}
.boot-prog .bp-fill{position:absolute;top:0;left:0;height:100%;background:linear-gradient(90deg,var(--in),#7dd3fc);border-radius:3px;transition:width .6s ease}
.boot-prog .bp-pct{color:var(--in);font-weight:600;min-width:30px;text-align:right}
.boot-prog .bp-elapsed{color:var(--t3);font-size:9px}
/* Indeterminate variant: animated stripe when no pct yet */
.boot-prog.indet .bp-fill{width:35% !important;animation:bpSlide 1.6s ease-in-out infinite;background:linear-gradient(90deg,transparent,var(--in),transparent)}
@keyframes bpSlide{0%{left:-35%}100%{left:100%}}
.bt{font-family:var(--mono);font-size:10px;font-weight:500;padding:2px 8px;border-radius:4px}
.bt.f{background:rgba(110,231,183,.08);color:var(--ac);border:1px solid rgba(110,231,183,.18)}
.bt.b{background:rgba(251,191,36,.08);color:var(--wr);border:1px solid rgba(251,191,36,.18)}
.pa{display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.lb{font-family:var(--mono);font-size:11px;color:var(--in);text-decoration:none;padding:5px 10px;border:1px solid rgba(96,165,250,.18);border-radius:var(--rs);transition:all .15s}.lb:hover{background:rgba(96,165,250,.08)}
.lb.disabled{opacity:.35;pointer-events:none}
/* Copy URL button — same size as .lb but neutral color, swaps to green check on success */
.cp{font-family:var(--mono);font-size:13px;line-height:1;color:var(--t2);background:transparent;padding:5px 10px;border:1px solid var(--bd);border-radius:var(--rs);cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;justify-content:center;min-width:30px}
.cp:hover:not(:disabled){border-color:var(--bd2);color:var(--t);background:rgba(255,255,255,.02)}
.cp:disabled{opacity:.35;cursor:not-allowed}
.cp.copied{color:var(--ac);border-color:rgba(110,231,183,.4);background:rgba(110,231,183,.08)}
.tech-panel{display:none;margin-top:12px;padding-top:12px;border-top:1px dashed var(--bd2);animation:slideDown .2s}
.tech-panel.open{display:block}
@keyframes slideDown{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.tech-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px 18px;font-family:var(--mono);font-size:11px;margin-bottom:10px}
.tech-item{display:flex;flex-direction:column;gap:2px}
.tech-item .tk{color:var(--t3);font-size:10px;text-transform:uppercase;letter-spacing:0.5px}
.tech-item .tv{color:var(--t);word-break:break-all}
.tech-item .tv.warn{color:var(--wr)}
.tech-item .tv.danger{color:var(--dg)}
.tech-item .tv.ok{color:var(--ac)}
.tech-item .tv.muted{color:var(--t3);font-style:italic}
.tech-metrics{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding-top:8px;border-top:1px dashed var(--bd2);margin-top:4px}
.ug{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--t3)}
.ub{width:60px;height:5px;background:var(--bd);border-radius:3px;overflow:hidden}
.uf{height:100%;border-radius:3px;transition:width .5s}.uf.lo{background:var(--ac)}.uf.mi{background:var(--wr)}.uf.hi{background:var(--dg)}
.empty{text-align:center;padding:48px 20px;color:var(--t2)}
.tw{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:6px;z-index:1000;pointer-events:none}
.to{font-family:var(--mono);font-size:12px;padding:10px 16px;border-radius:var(--rs);background:var(--c1);border:1px solid var(--bd);color:var(--t);animation:si .2s;max-width:380px;pointer-events:auto}
.to.er{color:var(--dg)}.to.ok{color:var(--ac)}
@keyframes si{from{transform:translateX(80px);opacity:0}to{transform:translateX(0);opacity:1}}
.sp{width:14px;height:14px;border:2px solid var(--bd);border-top-color:var(--ac);border-radius:50%;animation:spn .5s linear infinite;display:inline-block}@keyframes spn{to{transform:rotate(360deg)}}
.ov{position:fixed;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(4px);display:none;place-items:center;z-index:999}
.dl{background:var(--c1);border:1px solid var(--bd);border-radius:12px;padding:24px;max-width:440px;width:90%}
.dl h3{font-family:var(--mono);font-size:14px;margin-bottom:12px}
.da{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
input[type=text],input[type=password],input[type=number],input[type=time],input[type=date],select{font-family:var(--mono);font-size:13px;background:var(--bg);color:var(--t);border:1px solid var(--bd);border-radius:var(--rs);padding:8px 12px;width:100%;outline:none}
input:focus,select:focus{border-color:var(--ac)}
label{font-family:var(--mono);font-size:11px;color:var(--t3);display:block;margin-bottom:4px}
.fr{margin-bottom:14px}
.toggle{display:flex;align-items:center;gap:10px;cursor:pointer;font-family:var(--mono);font-size:12px;color:var(--t2)}
.toggle input{display:none}
.toggle .sw{width:36px;height:20px;background:var(--bd);border-radius:10px;position:relative;transition:background .2s}
.toggle .sw::after{content:'';position:absolute;top:2px;left:2px;width:16px;height:16px;background:var(--t3);border-radius:50%;transition:transform .2s}
.toggle input:checked+.sw{background:var(--ac)}.toggle input:checked+.sw::after{transform:translateX(16px);background:#0a0a0f}
.sidebar{position:fixed;top:0;left:0;width:750px;max-width:95vw;height:100vh;background:var(--c1);border-right:1px solid var(--bd);z-index:1001;transition:transform .25s ease;overflow-y:auto;padding:24px;transform:translateX(-100%)}
.sidebar.open{transform:translateX(0)}
.sidebar h2{font-family:var(--mono);font-size:16px;margin-bottom:20px;display:flex;justify-content:space-between;align-items:center}
.sidebar h2 .xb{cursor:pointer;font-size:20px;color:var(--t3)}.sidebar h2 .xb:hover{color:var(--t)}
.sb-cols{display:grid;grid-template-columns:280px 1fr;gap:24px}
.sb-left{border-right:1px solid var(--bd);padding-right:24px}
.sb-section{margin-bottom:20px}
.sb-section h3{font-family:var(--mono);font-size:13px;color:var(--t2);margin-bottom:10px}
.quota-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px}
.quota-grid .fr{margin:0}
.sb-dim{font-family:var(--mono);font-size:10px;color:var(--t3);margin-top:4px;font-style:italic}
.act-row{font-family:var(--mono);font-size:10px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04);display:grid;grid-template-columns:140px 1fr auto auto;gap:6px;align-items:center}
.act-row .at{color:var(--t3)}.act-row .au{color:var(--in)}.act-row .aa{color:var(--wr)}.act-row .ap{color:var(--t)}
.act-row .aa.autodelete,.act-row .aa.idle{color:var(--dg)}
/* Brief flash for newly-arrived activity rows after an auto-refresh tick */
.act-row.act-new{animation:actFlash 1s ease-out}
@keyframes actFlash{0%{background:rgba(110,231,183,.18)}100%{background:transparent}}
.filter-bar{display:flex;gap:8px;align-items:end;margin-bottom:12px;flex-wrap:wrap}
.filter-bar .fr{margin:0;flex:1;min-width:120px}
.sb-overlay{position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:1000;display:none}.sb-overlay.open{display:block}
.hamburger{font-size:20px;cursor:pointer;color:var(--t3);transition:color .15s;user-select:none}.hamburger:hover{color:var(--t)}
.user-tag{font-family:var(--mono);font-size:11px;color:var(--t2);display:flex;align-items:center;gap:6px;cursor:pointer}
.user-tag .proj{color:var(--ac);font-weight:600}
/* ===== Tooltip (CSS-only, hover on desktop, tap-toggle via .tt-open on mobile) ===== */
.has-tt{position:relative;cursor:help}
.has-tt .tt{position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%) translateY(4px);background:var(--c2);border:1px solid var(--bd2);border-radius:var(--rs);padding:8px 12px;font-family:var(--sans);font-size:11px;line-height:1.45;color:var(--t);white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,.4);opacity:0;pointer-events:none;transition:opacity .15s ease,transform .15s ease;z-index:500}
.has-tt .tt::after{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:5px solid transparent;border-top-color:var(--bd2)}
.has-tt:hover .tt,.has-tt.tt-open .tt{opacity:1;transform:translateX(-50%) translateY(0);pointer-events:auto}
@media(max-width:800px){.sb-cols{grid-template-columns:1fr}.sb-left{border:0;padding:0;border-bottom:1px solid var(--bd);padding-bottom:20px;margin-bottom:20px}}
@media(max-width:640px){.pc-main{grid-template-columns:1fr}.pa{justify-content:flex-start}}
</style></head><body>

<div id="loginScreen" style="position:fixed;inset:0;background:var(--bg);z-index:2000;display:flex;align-items:center;justify-content:center">
<div class="dl" style="max-width:360px">
  <h3>RunPod Manager</h3>
  <p style="color:var(--t2);font-size:13px">Enter your name and select your project</p>
  <div class="fr"><label>Nickname</label><input type="text" id="loginNick" maxlength="30" placeholder="Your name" onkeydown="if(event.key==='Enter')doUserLogin()"></div>
  <div class="fr"><label>Project</label><select id="loginProj"><option value="" disabled selected>— Выберите проект —</option></select></div>
  <div class="da"><button class="btn bp" onclick="doUserLogin()">Enter</button></div>
</div></div>

<div class="sb-overlay" id="sbOv" onclick="closeSidebar()"></div>
<div class="sidebar" id="sidebar">
  <h2>⚙ Admin Panel <span class="xb" onclick="closeSidebar()">✕</span></h2>
  <div id="sbContent">
    <p style="color:var(--t3);font-size:13px">Login required</p>
    <div class="fr"><label>Password</label><input type="password" id="sbPw" onkeydown="if(event.key==='Enter')sbLogin()"></div>
    <button class="btn bs bp" onclick="sbLogin()">Login</button>
  </div>
</div>

<div class="sh">
<header>
  <div style="display:flex;align-items:center;gap:16px">
    <span class="hamburger" onclick="openSidebar()">☰</span>
    <div class="logo"><div class="li">R</div><h1>RunPod <span>Manager</span></h1></div>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <span class="user-tag" id="userTag" onclick="changeUser()" title="Click to change"></span>
    <button class="btn" onclick="refreshPods()" id="rb">↻ Refresh</button>
    <button class="btn bp" onclick="createPod()" id="cb">+ New Pod</button>
    <span id="adminCreateControls" style="display:none;align-items:center;gap:6px">
      <select id="adminAssignProject" style="margin-left:8px;font-size:12px;padding:2px 4px">
        <option value="">Мой проект (ADMIN)</option>
        <option value="__null__">Не назначать</option>
      </select>
      <label style="margin-left:4px;font-size:12px;white-space:nowrap"><input type="checkbox" id="adminCountsFlag"> считать в квоту</label>
    </span>
  </div>
</header>
<div style="display:flex;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:12px">
  <span class="badge" id="cnt"><span class="ld"></span>loading…</span>
  <span class="badge" id="lim" style="color:var(--t3)"></span>
  <span class="badge" id="overBadge" style="color:var(--wr);border-color:rgba(251,191,36,.3);display:none"></span>
  <span class="badge" id="schedBadge" style="color:var(--t3);display:none"></span>
  <span class="badge" id="idleBadge" style="color:var(--t3);display:none"></span>
  <span class="badge" id="windowBadge" style="color:var(--wr);border-color:rgba(251,191,36,.3);display:none"></span>
  <span class="badge" id="upd" style="color:var(--t3)"></span>
</div>
<div id="pl" class="pl"><div class="empty"><p>⏳ Loading…</p></div></div>
</div>
<div class="tw" id="tw"></div><div class="ov" id="ov"></div>

<script>
let pods=[],requests=[],busy=new Set(),maxPods=99,isAdmin=false,user=null;
let podWindowState=null;  // {enabled, is_open, from, until, opens_in_sec, closes_in_sec} from /api/pods
// Activity log auto-refresh state. The interval is started when sidebar opens AND admin
// is logged in, and cleared when either condition becomes false. lastActivityIds tracks
// which row IDs we already saw so we can highlight only the truly-new ones on each tick.
let activityRefreshTimer=null;
let lastActivityIds=new Set();
const ACTIVITY_REFRESH_INTERVAL=15000;  // synchronized with refreshPods cadence
const PROJECTS=['CV','DV','MT','PT','MARK','ADMIN','TV','MW'];
const expandedTech=new Set();
const $=id=>document.getElementById(id);
const LS='runpod_user';
function toast(m,t){const e=document.createElement('div');e.className='to '+(t||'');e.textContent=m;$('tw').appendChild(e);setTimeout(()=>{e.style.opacity='0';setTimeout(()=>e.remove(),300)},4500)}
function showDlg(h){return new Promise(r=>{const o=$('ov');o.innerHTML='<div class="dl">'+h+'</div>';o.style.display='grid';o._r=r})}
function closeDlg(v){$('ov').style.display='none';if($('ov')._r){$('ov')._r(v);$('ov')._r=null}}
async function api(u,m,b){
  const o={method:m||'GET',headers:{'Content-Type':'application/json'}};
  if(b)o.body=JSON.stringify(b);
  const resp=await fetch(u,o);
  // Global auth-required handler: both 401 and 403 from our API mean 'session not
  // registered, show login screen'. We use 403 by default (see require_user in the
  // Python side) to avoid triggering browser basic-auth popups when behind a reverse
  // proxy, but we also handle 401 for backwards compatibility and defense in depth.
  if(resp.status===401||resp.status===403){
    // Skip auto-redirect for the auth-check endpoint itself — initLogin handles it.
    // Also skip admin endpoints — those have their own login flow in the sidebar.
    if(!u.startsWith('/api/user/check')&&!u.startsWith('/api/admin/')){
      user=null;
      $('loginScreen').style.display='flex';
      // Re-populate the projects dropdown if it was cleared
      const sel=$('loginProj');
      if(sel&&!sel.options.length){
        try{const pr=await(await fetch('/api/projects')).json();pr.projects.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;sel.appendChild(o)})}catch(e){}
      }
      $('loginNick').focus();
      // Return a sentinel so aok() can recognize this as 'auth redirected, not a
      // real error' and silently return null instead of throwing. This prevents
      // the red error toast from appearing on top of the login screen — the
      // screen itself already tells the user what to do.
      return {ok:false, _authRedirect:true};
    }
  }
  return await resp.json();
}
// aok: wraps api() to throw on {ok:false}. Special case: if the response is
// the _authRedirect sentinel (auth required, login screen shown), return null
// silently — the caller must be ready to handle null and should not show an
// error toast, because showing 'Not registered' on top of the login form is
// redundant and confusing.
async function aok(u,m,b){
  const j=await api(u,m,b);
  if(j&&j._authRedirect)return null;
  if(!j.ok)throw new Error(j.error||'Error');
  return j;
}

async function initLogin(){
  // Returns true if a valid session was found and the user is logged in,
  // false if the login screen was shown. The caller uses this to decide whether
  // to immediately kick off refreshPods() or wait for doUserLogin() to trigger it.
  // Without this signal, refreshPods() would fire unconditionally and hit 403,
  // producing a red error toast on top of the login screen.
  //
  // First ask the server if we already have a valid session.
  // localStorage is no longer trusted as identity — it's just a cache for
  // pre-filling the login form. The server is the source of truth.
  try{
    const r=await fetch('/api/user/check');
    if(r.ok){
      const j=await r.json();
      if(j.ok&&j.nickname&&j.project){
        user={nickname:j.nickname,project:j.project};
        // Refresh the localStorage cache for next time the page loads
        localStorage.setItem(LS,JSON.stringify(user));
        $('loginScreen').style.display='none';
        showUser();
        return true;
      }
    }
  }catch(e){/* fall through to login screen */}
  // No valid server session — show login screen.
  // Pre-fill from localStorage cache if available, just for convenience.
  const cached=localStorage.getItem(LS);
  if(cached){
    try{
      const c=JSON.parse(cached);
      if(c.nickname)$('loginNick').value=c.nickname;
    }catch(e){}
  }
  try{const r=await aok('/api/projects');if(r){const sel=$('loginProj');r.projects.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;sel.appendChild(o)})}}catch(e){}
  $('loginNick').focus();
  return false;
}
async function doUserLogin(){
  const n=$('loginNick').value.trim(),p=$('loginProj').value;
  if(!n){toast('Enter a nickname','er');return}
  if(!p){toast('Выберите проект','er');return}
  try{
    // Server validates and binds identity to session. On success it returns
    // the canonical (clean) nickname and project, which we use locally.
    const r=await aok('/api/user/register','POST',{nickname:n,project:p});
    user={nickname:r.nickname||n,project:r.project||p};
    localStorage.setItem(LS,JSON.stringify(user));
    $('loginScreen').style.display='none';
    showUser();
    refreshPods();
  }catch(e){
    toast(e.message||'Registration failed','er');
  }
}
function showUser(){$('userTag').innerHTML='<span>'+user.nickname+'</span> <span class="proj">'+user.project+'</span>'}
async function changeUser(){
  // Clear server-side identity binding first, then local cache.
  try{await fetch('/api/user/logout',{method:'POST'})}catch(e){}
  localStorage.removeItem(LS);
  user=null;
  location.reload();
}

function openSidebar(){
  $('sidebar').classList.add('open');$('sbOv').classList.add('open');
  setTimeout(()=>{const el=$('sbPw');if(el)el.focus()},200);
  // If admin is already logged in (e.g. closed and reopened sidebar), restart auto-refresh.
  // The actual snapshot of activity is repopulated by loadAdminPanel which fires on initial
  // login; here we just (re)attach the timer.
  if(isAdmin)startActivityAutoRefresh();
}
function closeSidebar(){
  $('sidebar').classList.remove('open');$('sbOv').classList.remove('open');
  // Always stop the timer when sidebar closes — no point polling if user can't see the log
  stopActivityAutoRefresh();
}
async function sbLogin(){
  try{const r=await api('/api/admin/login','POST',{password:$('sbPw').value});if(!r.ok)throw new Error(r.error);
    isAdmin=true;toast('Admin OK','ok');await loadAdminPanel();
    // Now that admin is logged in and the panel is rendered, kick off auto-refresh
    startActivityAutoRefresh();
    // Also immediately refresh the pod list so hidden pods become visible with
    // their yellow border and eye icons right away — otherwise the admin has to
    // wait up to 15s for the next setInterval tick to see their admin view.
    await refreshPods();
  }catch(e){toast('Wrong password','er')}
}

async function loadActivity(from,to){
  let url='/api/admin/activity';const params=[];
  if(from)params.push('from='+from);if(to)params.push('to='+to);
  if(params.length)url+='?'+params.join('&');
  try{const r=await aok(url);return r.actions||[]}catch(e){return[]}
}
function renderActivity(acts){
  if(!acts.length)return'<div style="color:var(--t3);font-size:11px;padding:12px 0">No actions found</div>';
  return acts.map(a=>{
    const isAuto=a.action==='autodelete';
    const isIdle=a.action==='pod usage timeout auto deleting';
    const cls=isAuto?'autodelete':(isIdle?'idle':'');
    return'<div class="act-row"><span class="at">'+formatLocalFull(a.ts)+'</span><span class="au">'+a.nickname+' ['+a.project+']</span><span class="aa '+cls+'">'+a.action+'</span><span class="ap">'+a.pod_name+'</span></div>'
  }).join('');
}
async function loadAdminPanel(){
  let s;try{s=(await aok('/api/admin/settings')).settings}catch(e){return}
  // Default to today's activity — matches the From/To inputs which are pre-filled
  // with today's date. Previously loaded all records which was confusing.
  const today=new Date().toISOString().slice(0,10);
  const acts=await loadActivity(today,today);
  // Snapshot which row IDs we already saw, so the auto-refresh can highlight only new ones
  lastActivityIds=new Set(acts.map(a=>a.id));
  $('sbContent').innerHTML='<div class="sb-cols">'+
    '<div class="sb-left">'+
      '<div class="sb-section"><h3>Per-project quotas</h3>'+
        '<div class="quota-grid">'+
          Object.keys(s.project_quotas).map(p=>
            '<div class="fr"><label>'+p+'</label><input type="number" class="qInput" data-proj="'+p+'" min="0" max="50" value="'+s.project_quotas[p]+'"></div>'
          ).join('')+
        '</div>'+
        '<div class="sb-dim">Лимит одновременно запущенных подов на каждый проект. Админ обходит лимит.</div>'+
      '</div>'+
      '<div class="sb-section"><h3>🖼 Образы подов</h3>'+
        '<div id="imgCatalog"></div>'+
        '<button class="btn" type="button" onclick="imgAddRow()">+ Добавить образ</button>'+
        '<div class="sb-dim">Каталог образов: понятное название + RunPod template_id. ⭐ — образ по умолчанию (его нельзя удалить).</div>'+
        '<div style="margin-top:8px;font-size:12px;color:var(--t2)">Образ на проект:</div>'+
        '<div class="quota-grid" id="imgProjGrid" style="margin-top:4px"></div>'+
      '</div>'+
      '<div class="sb-section"><h3>⏱ Idle timeout</h3>'+
        '<div class="fr"><label class="toggle"><input type="checkbox" id="sIdleOn" '+(s.idle_timeout_enabled?'checked':'')+
        '><span class="sw"></span> Auto-delete idle pods</label></div>'+
        '<div class="fr"><label>Timeout (minutes)</label><input type="number" id="sIdleMin" min="1" max="1440" value="'+(s.idle_timeout_minutes||120)+'"></div>'+
        '<div class="sb-dim">Timer starts when ComfyUI becomes ready. Activity is detected from ComfyUI prompt log.</div>'+
      '</div>'+
      '<div class="sb-section"><h3>🔁 Авторетрай заявки на под</h3>'+
        '<div class="fr"><label>Таймаут заявки (мин)</label><input type="number" id="sReqTimeout" min="1" max="1440" value="'+(s.pod_request_timeout_minutes||15)+'"></div>'+
        '<div class="fr"><label>Интервал ретрая (сек)</label><input type="number" id="sReqInterval" min="5" max="600" value="'+(s.pod_request_retry_interval_seconds||15)+'"></div>'+
        '<div class="sb-dim">Когда видеокарта занята, пользователь может оставить заявку — менеджер повторяет запуск каждые «интервал» секунд, пока не получится или не выйдет «таймаут».</div>'+
      '</div>'+
      '<div class="sb-section"><h3>⏰ Auto-delete (daily)</h3>'+
        '<div class="fr"><label class="toggle"><input type="checkbox" id="sSchedOn" '+(s.auto_delete_enabled?'checked':'')+
        '><span class="sw"></span> Daily auto-delete</label></div>'+
        '<div class="fr"><label>Time</label><input type="time" id="sSchedTime" value="'+utcTimeToLocal(s.auto_delete_time)+'" oninput="updateSchedHint();updateOffsetHints()"></div>'+
        '<div class="sb-dim" id="sSchedHint"></div>'+
        '<div style="margin-top:8px;font-size:12px;color:var(--t2)">Per-project offset (minutes — bypass delay):</div>'+
        '<div class="quota-grid" style="margin-top:4px">'+
          Object.keys(s.project_autodelete_offset_minutes||{}).map(p=>
            '<div class="fr"><label>'+p+' <span class="offEff" data-proj="'+p+'" style="color:var(--t3);font-size:10px;margin-left:4px"></span></label>'+
            '<input type="number" class="offInput" data-proj="'+p+'" min="0" max="1440" value="'+(s.project_autodelete_offset_minutes[p]||0)+'" oninput="updateOffsetHints()"></div>'
          ).join('')+
        '</div>'+
        '<div class="sb-dim" style="margin-top:4px">0 = удалять в базовое время. Unassigned-поды всегда удаляются в базовое время.</div>'+
        (s.auto_delete_last_log?'<div class="sb-dim">Last: '+formatScheduleLog(s.auto_delete_last_log)+'</div>':'')+
        '<div style="margin-top:10px"><button class="btn bs bd" onclick="deleteAllNow()">Delete all now</button></div></div>'+
      '<div class="sb-section"><h3>🔒 Pod creation restriction (daily)</h3>'+
        '<div class="fr"><label class="toggle"><input type="checkbox" id="sWinOn" '+(s.pod_window_enabled?'checked':'')+
        ' onchange="updateWindowHint()"><span class="sw"></span> Block pod creation during time window</label></div>'+
        '<div class="fr"><label>From (block starts)</label><input type="time" id="sWinFrom" value="'+utcTimeToLocal(s.pod_window_from||'22:00')+'" oninput="updateWindowHint()"></div>'+
        '<div class="fr"><label>Until (block ends)</label><input type="time" id="sWinUntil" value="'+utcTimeToLocal(s.pod_window_until||'08:00')+'" oninput="updateWindowHint()"></div>'+
        '<div class="sb-dim" id="sWinHint"></div>'+
        '<div class="sb-dim">During this window regular users cannot create new pods (typically night hours). Existing pods keep running. Admins bypass this restriction.</div>'+
      '</div>'+
      '<div class="sb-section"><h3>Password</h3><div class="fr"><label>New password</label><input type="password" id="sNewPw" placeholder="leave empty"></div></div>'+
      '<div class="da" style="margin-top:8px"><button class="btn" onclick="sbLogout()">Logout</button><button class="btn bs bp" onclick="sbSave()">Save</button></div>'+
    '</div>'+
    '<div class="sb-right">'+
      '<div class="sb-section"><h3>Activity Log</h3>'+
        '<div class="filter-bar">'+
          '<div class="fr"><label>From</label><input type="date" id="fFrom" value="'+today+'"></div>'+
          '<div class="fr"><label>To</label><input type="date" id="fTo" value="'+today+'"></div>'+
          '<button class="btn bs" onclick="filterLog()">Filter</button>'+
          '<button class="btn bs" onclick="filterLogAll()">All</button>'+
        '</div>'+
        '<div id="actLog" style="max-height:calc(100vh - 250px);overflow-y:auto">'+renderActivity(acts)+'</div>'+
      '</div>'+
    '</div>'+
  '</div>';
  // Initialize the schedule hint with current values (server UTC + computed local representation)
  imgInit(s);
  updateSchedHint();
  updateWindowHint();
  updateOffsetHints();
}
async function filterLog(){const f=$('fFrom').value,t=$('fTo').value;const acts=await loadActivity(f,t);lastActivityIds=new Set(acts.map(a=>a.id));$('actLog').innerHTML=renderActivity(acts)}
async function filterLogAll(){$('fFrom').value='';$('fTo').value='';const acts=await loadActivity('','');lastActivityIds=new Set(acts.map(a=>a.id));$('actLog').innerHTML=renderActivity(acts)}

// Returns true if both From and To inputs are set to today's date in the user's local TZ.
// Auto-refresh only runs in this case — if the admin is browsing the archive (e.g. last
// week), polling would constantly snap them back to today, which is annoying.
function isActivityFilterToday(){
  const f=$('fFrom'),t=$('fTo');
  if(!f||!t)return false;
  const today=new Date().toISOString().slice(0,10);
  return f.value===today&&t.value===today;
}

// Re-fetch the activity log and merge with current state. New rows (id not in
// lastActivityIds) get the .act-new class which triggers a 1-second flash animation.
// Called both by the auto-refresh interval and by C-mode triggers (after createPod etc).
async function refreshActivityLog(){
  if(!isAdmin)return;
  const logEl=$('actLog');
  if(!logEl)return;  // sidebar not rendered as admin yet
  // Only refresh if the filter is set to 'today' — don't disturb archive browsing
  if(!isActivityFilterToday())return;
  const today=new Date().toISOString().slice(0,10);
  const acts=await loadActivity(today,today);
  // Render with .act-new class on rows whose id wasn't in our previous snapshot
  const newIds=new Set();
  const html=acts.map(a=>{
    const isAuto=a.action==='autodelete';
    const isIdle=a.action==='pod usage timeout auto deleting';
    const cls=isAuto?'autodelete':(isIdle?'idle':'');
    const isNew=!lastActivityIds.has(a.id);
    if(isNew)newIds.add(a.id);
    const rowCls='act-row'+(isNew&&lastActivityIds.size>0?' act-new':'');
    return'<div class="'+rowCls+'"><span class="at">'+formatLocalFull(a.ts)+'</span><span class="au">'+a.nickname+' ['+a.project+']</span><span class="aa '+cls+'">'+a.action+'</span><span class="ap">'+a.pod_name+'</span></div>';
  }).join('');
  logEl.innerHTML=html||'<div style="color:var(--t3);font-size:11px;padding:12px 0">No actions found</div>';
  // Update snapshot for the next tick
  lastActivityIds=new Set(acts.map(a=>a.id));
}

// Start the activity log auto-refresh interval. Idempotent — multiple calls don't stack.
// Should be called only when sidebar is open AND admin is logged in.
function startActivityAutoRefresh(){
  if(activityRefreshTimer!==null)return;  // already running
  activityRefreshTimer=setInterval(refreshActivityLog,ACTIVITY_REFRESH_INTERVAL);
}

// Stop the auto-refresh. Called when sidebar closes or admin logs out.
function stopActivityAutoRefresh(){
  if(activityRefreshTimer!==null){
    clearInterval(activityRefreshTimer);
    activityRefreshTimer=null;
  }
}

// Live-update the hint under the schedule time input. Reads the current local-time value
// from the input and shows the equivalent UTC value that will be sent to the server.
// Called on initial render and on every keystroke (oninput).
function updateSchedHint(){
  const inp=$('sSchedTime');const hint=$('sSchedHint');
  if(!inp||!hint)return;
  const localVal=inp.value;
  if(!localVal){hint.textContent='';return}
  const utcVal=localTimeToUtc(localVal);
  hint.textContent='Your local time ('+getTzLabel()+'). Server stores as '+utcVal+' UTC.';
}

// Live-update the hint under the pod_window from/until inputs. Shows both the UTC values
// that will be stored and whether pod creation is currently allowed or blocked.
// NOTE: 'from' and 'until' define the RESTRICTION window (when creation is BLOCKED).
function updateWindowHint(){
  const fromInp=$('sWinFrom');const untilInp=$('sWinUntil');const hint=$('sWinHint');
  const onInp=$('sWinOn');
  if(!fromInp||!untilInp||!hint)return;
  const fromLocal=fromInp.value, untilLocal=untilInp.value;
  if(!fromLocal||!untilLocal){hint.textContent='';return}
  const fromUtc=localTimeToUtc(fromLocal);
  const untilUtc=localTimeToUtc(untilLocal);
  const enabled=onInp&&onInp.checked;
  let line='Your local time ('+getTzLabel()+'). Server stores block period as '+fromUtc+' → '+untilUtc+' UTC.';
  if(fromLocal===untilLocal){
    line+=' ⚠ From == Until — restriction is logically disabled.';
  } else if(enabled){
    // isInLocalWindow returns true if we're INSIDE [from, until).
    // In the new semantics that means creation is currently BLOCKED.
    const blocked=isInLocalWindow(fromLocal,untilLocal);
    line+=blocked?' 🔒 Создание подов сейчас заблокировано.':' ✅ Создание подов сейчас разрешено.';
  }
  hint.textContent=line;
}

// Check if the current local time is inside the [from, until) window.
// Supports overnight spans (from > until). All times are browser-local 'HH:MM'.
function isInLocalWindow(fromStr,untilStr){
  const [fh,fm]=fromStr.split(':').map(Number);
  const [uh,um]=untilStr.split(':').map(Number);
  const fromMin=fh*60+fm, untilMin=uh*60+um;
  if(fromMin===untilMin)return true;  // degenerate: disabled = always allowed
  const now=new Date();
  const nowMin=now.getHours()*60+now.getMinutes();
  if(fromMin<untilMin)return fromMin<=nowMin&&nowMin<untilMin;
  return nowMin>=fromMin||nowMin<untilMin;
}

// Format the auto_delete_last_log line. The server writes it as '[<ISO UTC>] Deleted N/M'
// or 'Manual: ...'. We extract the ISO timestamp, convert to local, and rebuild the line.
function formatScheduleLog(raw){
  if(!raw)return'';
  // Match leading [<iso>] prefix; everything after is the message
  const m=raw.match(/^\[([^\]]+)\]\s*(.*)$/);
  if(!m)return raw;
  const localTs=formatLocalFull(m[1]);
  return localTs+' — '+m[2];
}

// ---- Pod image catalog editor ----
let _imgCatalog = [];        // [{label, template_id}]
let _imgDefault = '';        // template_id of the default entry
let _imgProjSel = {};        // {project: template_id}
let _imgProjects = [];       // project names, from project_quotas keys

function imgInit(s){
  _imgCatalog = (s.pod_image_catalog||[]).map(e=>({label:e.label,template_id:e.template_id}));
  _imgDefault = s.default_pod_image||(_imgCatalog[0]&&_imgCatalog[0].template_id)||'';
  _imgProjSel = Object.assign({}, s.project_pod_image||{});
  _imgProjects = Object.keys(s.project_quotas||{});
  imgRenderCatalog(); imgRenderGrid();
}
function imgReadCatalogFromDom(){
  // Pull current input values back into _imgCatalog before re-render so edits survive.
  const rows=document.querySelectorAll('#imgCatalog .imgRow');
  _imgCatalog=Array.from(rows).map(r=>({
    label:r.querySelector('.imgLabel').value,
    template_id:r.querySelector('.imgTid').value.trim()
  }));
}
function imgAddRow(){ imgReadCatalogFromDom(); _imgCatalog.push({label:'',template_id:''}); imgRenderCatalog(); imgRenderGrid(); }
function imgDelRow(i){
  imgReadCatalogFromDom();
  const tid=_imgCatalog[i].template_id;
  if(tid && tid===_imgDefault){ toast('Нельзя удалить образ по умолчанию','er'); return; }
  _imgCatalog.splice(i,1); imgRenderCatalog(); imgRenderGrid();
}
function imgSetDefault(i){
  imgReadCatalogFromDom();
  const tid=(_imgCatalog[i].template_id||'').trim();
  if(!tid){ toast('Введите template_id перед выбором по умолчанию','er'); return; }
  _imgDefault=tid; imgRenderCatalog(); imgRenderGrid();
}
function imgRenderCatalog(){
  $('imgCatalog').innerHTML=_imgCatalog.map((e,i)=>{
    const isDef=e.template_id && e.template_id===_imgDefault;
    return '<div class="fr imgRow" data-i="'+i+'">'+
      '<input type="text" class="imgLabel" placeholder="название" value="'+imgEsc(e.label)+'" style="flex:1">'+
      '<input type="text" class="imgTid" placeholder="template_id" value="'+imgEsc(e.template_id)+'" oninput="imgRenderGrid()" style="flex:1">'+
      '<button class="btn" type="button" title="Сделать образом по умолчанию" onclick="imgSetDefault('+i+')">'+(isDef?'⭐':'☆')+'</button>'+
      '<button class="btn" type="button" title="Удалить" onclick="imgDelRow('+i+')"'+(isDef?' disabled':'')+'>🗑</button>'+
    '</div>';
  }).join('');
}
function imgEsc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function imgRenderGrid(){
  imgReadCatalogFromDom();
  // Preserve any in-progress per-project selections before we overwrite the grid.
  document.querySelectorAll('.imgProj').forEach(el=>{ _imgProjSel[el.dataset.proj]=el.value; });
  const defLabel=(_imgCatalog.find(e=>e.template_id===_imgDefault)||{}).label||'дефолт';
  $('imgProjGrid').innerHTML=_imgProjects.map(p=>{
    const sel=_imgProjSel[p]||'';
    const opts=['<option value="">По умолчанию ('+imgEsc(defLabel)+')</option>']
      .concat(_imgCatalog.filter(e=>e.template_id).map(e=>
        '<option value="'+imgEsc(e.template_id)+'"'+(e.template_id===sel?' selected':'')+'>'+imgEsc(e.label||e.template_id)+'</option>'));
    return '<div class="fr"><label>'+imgEsc(p)+'</label><select class="imgProj" data-proj="'+imgEsc(p)+'">'+opts.join('')+'</select></div>';
  }).join('');
}

async function sbSave(){
  const quotas = {};
  document.querySelectorAll('.qInput').forEach(el => {
    quotas[el.dataset.proj] = parseInt(el.value,10) || 0;
  });
  const offsets = {};
  document.querySelectorAll('.offInput').forEach(el => {
    offsets[el.dataset.proj] = parseInt(el.value,10) || 0;
  });
  imgReadCatalogFromDom();
  const podImageCatalog = _imgCatalog
    .map(e=>({label:(e.label||'').trim(), template_id:(e.template_id||'').trim()}))
    .filter(e=>e.label && e.template_id);
  const projImg = {};
  document.querySelectorAll('.imgProj').forEach(el=>{ if(el.value) projImg[el.dataset.proj]=el.value; });
  try{await aok('/api/admin/settings','POST',{project_quotas:quotas,
    project_autodelete_offset_minutes:offsets,
    auto_delete_enabled:$('sSchedOn').checked,auto_delete_time:localTimeToUtc($('sSchedTime').value),
    idle_timeout_enabled:$('sIdleOn').checked,idle_timeout_minutes:parseInt($('sIdleMin').value)||120,
    pod_request_timeout_minutes:parseInt($('sReqTimeout').value)||15,
    pod_request_retry_interval_seconds:parseInt($('sReqInterval').value)||15,
    pod_window_enabled:$('sWinOn').checked,
    pod_window_from:localTimeToUtc($('sWinFrom').value),
    pod_window_until:localTimeToUtc($('sWinUntil').value),
    pod_image_catalog:podImageCatalog,
    default_pod_image:_imgDefault,
    project_pod_image:projImg,
    new_password:$('sNewPw').value||undefined});toast('Saved','ok');await refreshPods()}catch(e){toast(e.message,'er')}
}

// Paint effective-time hints next to each per-project offset input.
// Called on offset change and on base-time change.
function updateOffsetHints(){
  const baseLocal = $('sSchedTime') && $('sSchedTime').value;  // HH:MM local
  if(!baseLocal) return;
  const [bh, bm] = baseLocal.split(':').map(n=>parseInt(n,10)||0);
  document.querySelectorAll('.offInput').forEach(el=>{
    const off = parseInt(el.value,10) || 0;
    const total = (bh*60 + bm + off) % 1440;
    const eh = String(Math.floor(total/60)).padStart(2,'0');
    const em = String(total%60).padStart(2,'0');
    const hint = document.querySelector('.offEff[data-proj="'+el.dataset.proj+'"]');
    if(hint) hint.textContent = '→ '+eh+':'+em;
  });
}
async function deleteAllNow(){
  if(!confirm('Delete ALL running pods now?'))return;
  try{const r=await aok('/api/admin/delete-all','POST');toast(r.message,'ok');await refreshPods();loadAdminPanel()}catch(e){toast(e.message,'er')}
}
async function sbLogout(){
  try{await api('/api/admin/logout','POST')}catch(e){}
  isAdmin=false;
  stopActivityAutoRefresh();
  lastActivityIds=new Set();
  $('sbContent').innerHTML='<p style="color:var(--t3);font-size:13px">Login required</p><div class="fr"><label>Password</label><input type="password" id="sbPw" onkeydown="if(event.key===\'Enter\')sbLogin()"></div><button class="btn bs bp" onclick="sbLogin()">Login</button>';
  toast('Logged out');
  // Immediately refresh the pod list so hidden pods disappear from view right
  // away — otherwise the stale admin-populated data (with hidden pods still in
  // the local array and yellow borders still in the DOM) stays visible until
  // the next setInterval tick up to 15s later. The server's api_pods_get will
  // now see is_admin=false in the session and return the filtered list.
  await refreshPods();
}

async function refreshPods(){
  // Guard: don't fire API calls when there's no user session yet. This prevents
  // the setInterval tick (which runs every 15s regardless of login state) from
  // hitting /api/pods with an empty session and producing a red error toast.
  // doUserLogin() will call refreshPods() explicitly once registration succeeds.
  if(!user)return;
  const b=$('rb');b.disabled=true;b.innerHTML='<span class="sp"></span>';
  try{const r=await aok('/api/pods');
    // aok returns null when the global auth handler kicked in (session expired
    // mid-session, for example). In that case the login screen is already shown
    // and we just silently stop — nothing more to do here.
    if(!r)return;
    pods=r.pods||[];requests=r.requests||[];maxPods=r.maxPods||99;
    // Server is the authoritative source for admin status. Reading it from
    // /api/pods response means the eye icons render correctly even immediately
    // after page load, before the user ever opens the admin sidebar. If the
    // admin logs out via the sidebar, sbLogout() resets isAdmin=false and the
    // next refresh picks up the change from the server.
    if(typeof r.isAdmin==='boolean')isAdmin=r.isAdmin;
    render();$('upd').textContent='updated '+new Date().toLocaleTimeString();
    // lim badge shows quota usage, not raw running count. quotaUsed = pods
    // that were created through this manager (have createdBy) and aren't
    // hidden. External/hidden pods don't count against the quota — they show
    // up in the overBadge instead.
    $('lim').textContent=(r.quotaUsed||0)+'/'+maxPods;
    // overBadge: running pods visible to this viewer that are NOT in the quota.
    // For regular users this is only external pods. For admins this is external
    // pods plus hidden pods. Only shown when there's at least one, to avoid
    // cluttering the UI when everything is inside the quota.
    const ob=$('overBadge');
    const oq=r.overQuota||0;
    if(oq>0){
      ob.style.display='';
      ob.classList.add('has-tt');
      ob.setAttribute('onclick','toggleTt(event,this)');
      ob.innerHTML='+'+oq+' · создано админом<span class="tt">Не учитывается в лимите. Это поды созданные вне менеджера или скрытые админом.</span>';
    } else {
      ob.style.display='none';
    }
    if(r.schedule){
      const sb=$('schedBadge');
      sb.style.display='';
      const info=formatScheduleBadge(r.schedule.time);
      sb.classList.add('has-tt');
      sb.setAttribute('onclick','toggleTt(event,this)');
      sb.innerHTML=info.badgeText+'<span class="tt">'+info.tooltipText+'</span>';
    }else{$('schedBadge').style.display='none'}
    if(r.idleTimeoutEnabled){
      const ib=$('idleBadge');
      ib.style.display='';
      ib.classList.add('has-tt');
      ib.setAttribute('onclick','toggleTt(event,this)');
      ib.innerHTML='⏱ idle '+r.idleTimeoutMinutes+'m<span class="tt">'+formatIdleTooltip(r.idleTimeoutMinutes)+'</span>';
    }else{$('idleBadge').style.display='none'}
    // Pod creation restriction badge — shown ONLY when:
    //   1. Restriction is enabled in settings
    //   2. We're currently INSIDE the restriction window (creation blocked right now)
    //   3. Viewer is NOT an admin (admins bypass the restriction, so the badge would
    //      be misleading — 'blocked' while they can still create pods)
    //
    // NOTE: 'from' = when block STARTS, 'until' = when block ENDS.
    // When blocked, we want to tell users WHEN they can create again → show 'until'.
    podWindowState=r.podWindow||null;
    const wb=$('windowBadge');
    if(podWindowState&&podWindowState.enabled&&!podWindowState.is_open&&!isAdmin){
      // 'until' is the time when the restriction lifts (when creation becomes allowed)
      const liftTimeLocal=utcTimeToLocal(podWindowState.until);
      wb.style.display='';
      wb.classList.add('has-tt');
      wb.setAttribute('onclick','toggleTt(event,this)');
      // Format countdown from opens_in_sec (seconds until restriction lifts)
      let countdown='';
      if(podWindowState.opens_in_sec!=null){
        const mins=Math.max(0,Math.floor(podWindowState.opens_in_sec/60));
        if(mins<60)countdown=' · откроется через '+mins+'м';
        else{const h=Math.floor(mins/60),rm=mins%60;countdown=' · откроется через '+h+'ч '+rm+'м';}
      }
      wb.innerHTML='🔒 запуск подов ограничен до '+liftTimeLocal+countdown+
        '<span class="tt">Запуск запрещён с '+utcTimeToLocal(podWindowState.from)+' до '+utcTimeToLocal(podWindowState.until)+' ('+getTzLabel()+')</span>';
    }else{wb.style.display='none'}
  }catch(e){toast('Failed: '+e.message,'er')}finally{b.disabled=false;b.innerHTML='↻ Refresh'}}

async function createPod(){if(!user)return;const b=$('cb');b.disabled=true;b.innerHTML='<span class="sp"></span>';
  // Identity is bound to the session on the server side. Admin may override via body.
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

async function delPod(id,n){
  showDlg('<h3>Delete?</h3><p style="color:var(--t2);font-size:13px;margin-bottom:18px">Terminate <b style="color:var(--t)">'+n+'</b>?</p><div class="da"><button class="btn" onclick="closeDlg(false)">Cancel</button><button class="btn bs bd" onclick="closeDlg(true)">Delete</button></div>').then(async ok=>{
    if(!ok||!user)return;busy.add(id);render();
    // Identity from session, body is empty.
    try{const j=await api('/api/pods/'+id,'DELETE',{});if(!j.ok)throw new Error(j.error);toast(n+' deleted','ok');await refreshPods();refreshActivityLog()}catch(e){toast(e.message,'er')}finally{busy.delete(id)}})}

async function cancelRequest(id){
  if(!user)return;
  try{const j=await api('/api/pod-requests/'+id,'DELETE',{});if(!j.ok)throw new Error(j.error);await refreshPods();refreshActivityLog()}
  catch(e){toast(e.message,'er')}
}

async function startPod(id,n){busy.add(id);render();try{await aok('/api/pods/'+id+'/start','POST',{});toast(n+' started','ok');await refreshPods();refreshActivityLog()}catch(e){toast(e.message,'er')}finally{busy.delete(id)}}


function toggleTech(id){
  if(expandedTech.has(id))expandedTech.delete(id);
  else expandedTech.add(id);
  render();
}

function sc(s){return s==='RUNNING'?'r':s==='EXITED'||s==='STOPPED'?'e':'x'}
function esc(s){return(s||'').replace(/'/g,"\\'").replace(/"/g,'&quot;')}
function ul(v){return v>50?'hi':v>15?'mi':'lo'}
function bar(l,v){return'<div class="ug"><span>'+l+'</span><div class="ub"><div class="uf '+ul(v)+'" style="width:'+Math.min(v,100)+'%"></div></div><span>'+v+'%</span></div>'}

function formatWhen(ts){
  if(!ts)return'';
  // Parse ISO 8601 (with Z suffix preferred). Tolerant fallback for legacy 'YYYY-MM-DD HH:MM:SS' rows:
  // those are interpreted as UTC by appending 'Z'. The browser then renders in the user's local TZ.
  let raw=ts.trim();
  if(!raw.includes('T')&&raw.includes(' '))raw=raw.replace(' ','T');
  if(!/[Zz]|[+-]\d{2}:?\d{2}$/.test(raw))raw+='Z';
  const d=new Date(raw);
  if(isNaN(d))return ts;
  const now=new Date();
  const sameDay=d.toDateString()===now.toDateString();
  const yesterday=new Date(now);yesterday.setDate(now.getDate()-1);
  const isYesterday=d.toDateString()===yesterday.toDateString();
  const hh=String(d.getHours()).padStart(2,'0');
  const mm=String(d.getMinutes()).padStart(2,'0');
  if(sameDay)return'today '+hh+':'+mm;
  if(isYesterday)return'yesterday '+hh+':'+mm;
  const diffDays=Math.floor((now-d)/(1000*60*60*24));
  if(diffDays<7)return diffDays+'d ago';
  return d.toLocaleDateString()+' '+hh+':'+mm;
}

// ============================================================
// Timezone conversion helpers for the admin panel scheduler.
// Server stores auto_delete_time as UTC ('21:00' = 21:00 UTC).
// The admin UI shows it in the browser's local timezone for editing,
// then converts back to UTC on save. This way the server contract
// stays unambiguous (always UTC) while the admin sees familiar times.
// ============================================================

// Convert 'HH:MM' UTC string to 'HH:MM' in browser's local timezone.
// Example: in Moscow (UTC+3), utcTimeToLocal('21:00') -> '00:00' (next day, but we drop the date).
function utcTimeToLocal(utcStr){
  if(!utcStr||!/^\d{1,2}:\d{2}$/.test(utcStr))return utcStr;
  const [h,m]=utcStr.split(':').map(Number);
  // Build a Date with today's UTC date and the given UTC time, then read its local hours.
  const d=new Date();
  d.setUTCHours(h,m,0,0);
  const lh=String(d.getHours()).padStart(2,'0');
  const lm=String(d.getMinutes()).padStart(2,'0');
  return lh+':'+lm;
}

// Convert 'HH:MM' local time string to 'HH:MM' in UTC.
// Inverse of utcTimeToLocal. Example: in Moscow (UTC+3), localTimeToUtc('00:00') -> '21:00'.
function localTimeToUtc(localStr){
  if(!localStr||!/^\d{1,2}:\d{2}$/.test(localStr))return localStr;
  const [h,m]=localStr.split(':').map(Number);
  const d=new Date();
  d.setHours(h,m,0,0);
  const uh=String(d.getUTCHours()).padStart(2,'0');
  const um=String(d.getUTCMinutes()).padStart(2,'0');
  return uh+':'+um;
}

// Returns a short human-readable TZ label like 'UTC+3' or 'UTC-5:30'.
// getTimezoneOffset() returns minutes WEST of UTC (so Moscow returns -180).
// We negate it to get the conventional sign (Moscow = +3).
function getTzLabel(){
  const off=-new Date().getTimezoneOffset();
  const sign=off>=0?'+':'-';
  const abs=Math.abs(off);
  const h=Math.floor(abs/60);
  const m=abs%60;
  return 'UTC'+sign+h+(m?':'+String(m).padStart(2,'0'):'');
}

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

function formatDuration(sec){
  if(sec==null||sec<0)return'—';
  if(sec<60)return sec+'s';
  const m=Math.floor(sec/60);
  if(m<60)return m+'m';
  const h=Math.floor(m/60);
  const rm=m%60;
  return h+'h '+rm+'m';
}

// Copy URL to clipboard. Tries the modern Clipboard API first (works in secure contexts:
// https:// or localhost). Falls back to the legacy execCommand trick for plain http://
// over LAN, where Clipboard API is blocked by browsers. Returns a Promise that resolves
// to true on success, false on failure.
async function copyToClipboard(text){
  // Try modern API
  if(navigator.clipboard&&window.isSecureContext){
    try{await navigator.clipboard.writeText(text);return true}catch(e){/* fall through */}
  }
  // Legacy fallback: temp textarea + execCommand
  try{
    const ta=document.createElement('textarea');
    ta.value=text;
    ta.style.position='fixed';
    ta.style.left='-9999px';
    ta.style.top='0';
    document.body.appendChild(ta);
    ta.focus();ta.select();
    const ok=document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  }catch(e){return false}
}

// Click handler for the copy button. Visually swaps the icon to a checkmark on success,
// or shows an error toast on failure. Argument btn is the actual <button> element.
async function copyPodUrl(btn,url){
  const ok=await copyToClipboard(url);
  if(ok){
    const orig=btn.innerHTML;
    btn.innerHTML='✓';
    btn.classList.add('copied');
    setTimeout(()=>{btn.innerHTML=orig;btn.classList.remove('copied')},1500);
  } else {
    toast('Copy failed — copy URL manually','er');
  }
}

// ============================================================
// Schedule badge: parses 'HH:MM' UTC time and computes:
//   - next occurrence in browser-local time
//   - countdown until then in russian (через 5ч 47м)
// Returns {badgeText, tooltipText}.
// ============================================================
function formatScheduleBadge(timeStr){
  // timeStr is 'HH:MM' interpreted as UTC (admin panel labels it as such)
  const m=String(timeStr||'').match(/^(\d{1,2}):(\d{2})$/);
  if(!m)return{badgeText:'⏰ '+timeStr,tooltipText:'Daily auto-delete schedule'};
  const hUtc=parseInt(m[1],10), mUtc=parseInt(m[2],10);
  // Build a Date for today at HH:MM UTC
  const now=new Date();
  const target=new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate(),hUtc,mUtc,0,0));
  // If that moment is already in the past, the next run is tomorrow at the same time
  if(target.getTime()<=now.getTime()){
    target.setUTCDate(target.getUTCDate()+1);
  }
  // Local representation — getHours/getMinutes return browser-local automatically
  const localH=String(target.getHours()).padStart(2,'0');
  const localM=String(target.getMinutes()).padStart(2,'0');
  // Countdown
  const diffMs=target.getTime()-now.getTime();
  const diffMin=Math.floor(diffMs/60000);
  let countdown;
  if(diffMin<1){
    countdown='менее минуты';
  } else if(diffMin<60){
    countdown='через '+diffMin+'м';
  } else {
    const h=Math.floor(diffMin/60);
    const rm=diffMin%60;
    countdown='через '+h+'ч '+rm+'м';
  }
  return{
    badgeText:'⏰ '+localH+':'+localM+' ('+countdown+')',
    tooltipText:'Все работающие поды будут автоматически удалены в это время',
  };
}

// Tooltip text for the idle-timeout badge — short explanation, no countdown
// (idle is per-pod, can't show one global value).
function formatIdleTooltip(minutes){
  return'Неактивные поды (без выполняющихся задач) будут автоматически удалены через '+minutes+' минут';
}

// Mobile tap-to-toggle for tooltips. Toggles .tt-open on the .has-tt element.
function toggleTt(ev,el){
  ev.stopPropagation();
  const wasOpen=el.classList.contains('tt-open');
  // Close any other open tooltips first
  document.querySelectorAll('.has-tt.tt-open').forEach(o=>o.classList.remove('tt-open'));
  if(!wasOpen)el.classList.add('tt-open');
}
// Click anywhere outside an open tooltip closes it
document.addEventListener('click',function(){
  document.querySelectorAll('.has-tt.tt-open').forEach(o=>o.classList.remove('tt-open'));
});

function render(){
  const run=pods.filter(p=>p.desiredStatus==='RUNNING').length;
  // Local quota calculation — matches the server formula exactly.
  // quota = min(run, maxPods) so we display '4/4' rather than '5/4' when over.
  // The excess (if any) is shown separately in the overBadge.
  // 'run' here is already viewer-filtered because the pods array was populated
  // from api_pods_get which filtered hidden pods for non-admin users.
  const quotaUsed=Math.min(run,maxPods);
  $('cnt').innerHTML='<span class="ld"></span>'+pods.length+' pod'+(pods.length!==1?'s':'')+' · '+run+' running';
  // Disable '+ New Pod' button if (a) visible running >= max_pods AND not
  // admin, OR (b) window is closed AND not admin. Note: we check `run` (raw
  // visible count) not `quotaUsed` (capped) — otherwise we'd only detect the
  // boundary case run===max but not run>max scenarios. Admins bypass both.
  const atLimit=run>=maxPods;
  const windowBlocks=podWindowState&&podWindowState.enabled&&!podWindowState.is_open;
  $('cb').disabled=(!isAdmin)&&(atLimit||windowBlocks);
  $('cb').title=isAdmin?'':(windowBlocks?'Запуск подов в данный момент ограничен':(atLimit?'Достигнут лимит подов':''));
  // Show/hide admin create controls and populate project options once
  const acc=$('adminCreateControls');
  if(acc){
    acc.style.display=isAdmin?'inline-flex':'none';
    const apEl=$('adminAssignProject');
    if(isAdmin&&apEl&&apEl.options.length<=2){
      PROJECTS.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;apEl.appendChild(o);});
    }
  }
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
    const st=p.desiredStatus||'UNKNOWN',isR=st==='RUNNING',isS=st==='EXITED'||st==='STOPPED',ib=busy.has(p.id),sn=esc(p.name),t=p.telemetry||{};
    const svcReady=p.serviceReady===true;
    const techOpen=expandedTech.has(p.id);

    // When ComfyUI is ready — show the green ready tag.
    // While booting — show a progress bar (determinate if we have bootPct, indeterminate otherwise).
    let svcTag='';
    if(isR){
      if(svcReady){
        svcTag='<span class="svc-tag ready"><span class="svc-dot"></span>ComfyUI ready</span>';
      } else if(p.bootPct!=null){
        const pct=Math.max(0,Math.min(100,p.bootPct));
        const elapsed=p.bootElapsed!=null?formatDuration(p.bootElapsed):'';
        svcTag='<span class="boot-prog" title="Booting ComfyUI…">'+
                 '<div class="bp-bar"><div class="bp-fill" style="width:'+pct+'%"></div></div>'+
                 '<span class="bp-pct">'+pct+'%</span>'+
                 (elapsed?'<span class="bp-elapsed">'+elapsed+'</span>':'')+
               '</span>';
      } else {
        // No data yet — indeterminate animated bar (first ~10s after pod start, or port 8189 not exposed)
        svcTag='<span class="boot-prog indet" title="Waiting for boot status…">'+
                 '<div class="bp-bar"><div class="bp-fill"></div></div>'+
                 '<span class="bp-pct">starting</span>'+
               '</span>';
      }
    }

    // Show busy/free tag ONLY when ComfyUI is ready — otherwise we don't really know.
    // Source preference: runtimeActive (event-based, accurate) > telemetry (sampling, lossy).
    let busyTag='';
    if(isR&&svcReady){
      let isBusy;
      let queueExtra='';
      if(p.runtimeActive!==null&&p.runtimeActive!==undefined){
        // Authoritative source: runtime watcher tail-parsing ComfyUI log
        isBusy=p.runtimeActive===true;
        // If there are multiple queued prompts, show the count next to the tag
        if(isBusy&&p.runtimeQueueDepth>1){
          queueExtra=' · '+p.runtimeQueueDepth+' в очереди';
        }
      } else {
        // Fallback: telemetry from RunPod GraphQL
        isBusy=(t.gpuUtil||0)>0||(t.cpuUtil||0)>0;
      }
      busyTag='<span class="bt '+(isBusy?'b':'f')+'">'+(isBusy?'загружен':'свободен')+queueExtra+'</span>';
    }

    let creatorLine;
    if(p.createdBy){
      creatorLine='<div class="creator-line">'+
        '<span>Started by</span>'+
        '<span class="who">'+p.createdBy+'</span>'+
        '<span class="pj">'+p.createdProject+'</span>'+
        '<span class="when">· '+formatWhen(p.createdAt)+'</span>'+
      '</div>';
    } else {
      creatorLine='<div class="creator-line"><span class="nobody">Started outside this app</span></div>';
    }

    let actions='';
    if(ib){
      actions='<span class="sp"></span>';
    } else {
      if(isR&&p.comfyUrl){
        // Copy URL button — always active when URL exists, even before ComfyUI is ready.
        // Rationale: the URL is valid the moment the pod has an ID. A user can copy it,
        // paste into Slack/email, and by the time the recipient opens it ComfyUI will be up.
        actions+='<button class="cp" title="Copy URL to clipboard" onclick="copyPodUrl(this,\''+p.comfyUrl+'\')">⧉</button>';
        actions+='<a class="lb'+(svcReady?'':' disabled')+'" href="'+p.comfyUrl+'" target="_blank" title="'+(svcReady?'Open ComfyUI':'ComfyUI not ready yet')+'">Open ↗</a>';
      }
      if(isS)actions+='<button class="btn bs bg" onclick="startPod(\''+p.id+"','"+sn+"')\">▶ Start</button>";
      if(isAdmin)actions+='<button class="btn bs" title="Назначить проекту" onclick="openAssignModal(\''+p.id+'\',\''+(p.assignedProject||'')+'\','+(p.countsTowardQuota?'true':'false')+')">Назначить</button>';
      actions+='<button class="btn bs bd" onclick="delPod(\''+p.id+"','"+sn+"')\">✕</button>";
    }

    // Idle countdown — only meaningful when ComfyUI is ready
    let idleRow='';
    if(isR){
      if(p.idleSeconds!=null&&p.idleTimeoutMinutes){
        const limit=p.idleTimeoutMinutes*60;
        const remaining=Math.max(0,limit-p.idleSeconds);
        const pct=Math.min(100,(p.idleSeconds/limit)*100);
        let cls='ok';
        if(pct>=80)cls='danger';
        else if(pct>=50)cls='warn';
        idleRow='<div class="tech-item"><span class="tk">Idle for</span><span class="tv '+cls+'">'+formatDuration(p.idleSeconds)+' / '+p.idleTimeoutMinutes+'m</span></div>'+
                '<div class="tech-item"><span class="tk">Auto-delete in</span><span class="tv '+cls+'">'+formatDuration(remaining)+'</span></div>';
      } else {
        // Pod running but ComfyUI not ready yet — timer not started
        idleRow='<div class="tech-item"><span class="tk">Idle timer</span><span class="tv muted">waiting for ComfyUI</span></div>';
      }
    }

    // Runtime activity stats — only when ready and runtime watcher is responding
    let runtimeRows='';
    if(isR&&svcReady&&p.runtimeActive!==null&&p.runtimeActive!==undefined){
      const completed=p.runtimeTotalCompleted||0;
      const started=p.runtimeTotalStarted||0;
      runtimeRows+='<div class="tech-item"><span class="tk">Prompts</span><span class="tv">'+completed+' done'+(started>completed?' · '+(started-completed)+' active':'')+'</span></div>';
      if(p.runtimeLastDuration!=null){
        runtimeRows+='<div class="tech-item"><span class="tk">Last prompt</span><span class="tv">'+formatDuration(Math.round(p.runtimeLastDuration))+'</span></div>';
      }
      if(p.runtimeLastEventAt){
        runtimeRows+='<div class="tech-item"><span class="tk">Last event</span><span class="tv">'+formatWhen(p.runtimeLastEventAt)+'</span></div>';
      }
    }

    const techPanel='<div class="tech-panel'+(techOpen?' open':'')+'">'+
      '<div class="tech-grid">'+
        '<div class="tech-item"><span class="tk">Pod ID</span><span class="tv">'+p.id+'</span></div>'+
        '<div class="tech-item"><span class="tk">Status</span><span class="tv">'+st+'</span></div>'+
        '<div class="tech-item"><span class="tk">GPU</span><span class="tv">'+(p.gpuId||'—')+'</span></div>'+
        '<div class="tech-item"><span class="tk">Cost</span><span class="tv">'+(p.costPerHr?'$'+p.costPerHr.toFixed(3)+'/hr':'—')+'</span></div>'+
        idleRow+
        runtimeRows+
      '</div>'+
      (isR?'<div class="tech-metrics">'+bar('GPU',t.gpuUtil||0)+bar('VRAM',t.gpuMem||0)+bar('CPU',t.cpuUtil||0)+bar('RAM',t.ramUtil||0)+'</div>':'')+
    '</div>';

    // Card class: unassigned pods get pc-unassigned (yellow border, admin-visible only).
    // "Unassigned" means the pod is not assigned to any project — only admins can see
    // and interact with it. Additional border accents for admin-created and external pods.
    const isUnassigned = p.assignedProject === null;
    const cardCls = 'pc'
      + (isUnassigned ? ' pc-unassigned' : '')
      + (p.creationSource === 'admin' ? ' pc-admin-created' : '')
      + (p.creationSource === 'external' ? ' pc-external' : '');

    // Badge strip: project tag + creation source + admin-only status badges.
    const badges = [];
    if(p.assignedProject) badges.push('<span class="pbadge pb-proj">'+p.assignedProject+'</span>');
    if(p.creationSource === 'admin') badges.push('<span class="pbadge pb-admin">🛡 admin created</span>');
    if(p.creationSource === 'external') badges.push('<span class="pbadge pb-ext">🌐 external</span>');
    if(isAdmin && isUnassigned) badges.push('<span class="pbadge pb-unassigned">👁 unassigned</span>');
    if(isAdmin && p.countsTowardQuota === false && p.assignedProject) badges.push('<span class="pbadge pb-nocount" title="не учитывается в квоте">∞</span>');
    const badgeHtml = badges.length ? '<div class="pbadges">'+badges.join('')+'</div>' : '';

    return'<div class="'+cardCls+'">'+
      '<div class="pc-main">'+
        '<div class="pc-info">'+
          '<div class="pn">'+
            '<span class="sd '+sc(st)+'"></span>'+
            (p.name||p.id)+
            (svcTag?svcTag:'')+
            (busyTag?busyTag:'')+
            '<button class="info-btn'+(techOpen?' open':'')+'" onclick="toggleTech(\''+p.id+'\')" title="Technical details">i</button>'+
          '</div>'+
          badgeHtml+
          creatorLine+
        '</div>'+
        '<div class="pa">'+actions+'</div>'+
      '</div>'+
      techPanel+
    '</div>'
  }).join('')}

initLogin().then(loggedIn=>{if(loggedIn)refreshPods()});setInterval(refreshPods,15000);

let _assignPid = null;

function openAssignModal(pid, currentProject, currentCounts){
  _assignPid = pid;
  const sel = $('assignProj');
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
    await aok('/api/admin/pods/'+_assignPid+'/assign','POST',{project, counts_toward_quota});
    closeAssignModal();
    refreshPods();
    if(typeof refreshActivityLog === 'function') refreshActivityLog();
  }catch(e){
    alert('Failed: '+(e && e.message || e));
  }
}
</script>
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
</body></html>
"""

@app.route("/")
def index(): return Response(FRONTEND_HTML, mimetype="text/html")

# Empty favicon — prevents the browser's automatic /favicon.ico request from
# hitting a 401 from an upstream reverse proxy (e.g. Caddy basic auth), which
# in turn prevents spurious basic-auth popups. 204 No Content is the cleanest
# way to say 'I acknowledge the request but there's nothing to return'.
@app.route("/favicon.ico")
def favicon(): return Response("", status=204)

if __name__=="__main__":
    pa=argparse.ArgumentParser(); pa.add_argument("--host",default="0.0.0.0"); pa.add_argument("--port",type=int,default=5001); pa.add_argument("--api-key",default=""); pa.add_argument("--debug",action="store_true")
    a=pa.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,format="%(asctime)s [%(levelname)s] %(message)s",datefmt="%H:%M:%S")
    print("\n  RunPod Manager v6.6\n")
    detect_cli()
    _api_key=resolve_api_key(a.api_key)
    if _api_key: print(f"  ✓  Key: {_api_key[:8]}...")
    else: print("  ⚠  No API key")
    init_db()
    s=load_settings()
    print(f"  ⚙  Admin: {'[default: admin]' if s['admin_password']=='admin' else '[custom]'}")
    quotas = s.get('project_quotas') or {}
    print(f"  ⚙  Per-project quotas: " + ", ".join(f"{k}={v}" for k,v in quotas.items()))
    if s.get("auto_delete_enabled"): print(f"  ⏰  Daily auto-delete: {s['auto_delete_time']}")
    else: print("  ⏰  Daily auto-delete: off")
    if s.get("idle_timeout_enabled"): print(f"  ⏱  Idle timeout: {s['idle_timeout_minutes']} min")
    else: print("  ⏱  Idle timeout: off")
    print(f"\n  🌍 http://localhost:{a.port}\n")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=pod_request_loop, daemon=True).start()
    app.run(host=a.host,port=a.port,debug=a.debug)
