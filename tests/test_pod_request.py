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
from unittest import mock

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

    def test_pod_request_table_exists(self):
        db = sqlite3.connect(self.db_path)
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pod_request'")
        self.assertIsNotNone(cur.fetchone())
        # Index exists too
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_pr_status'")
        self.assertIsNotNone(cur.fetchone())
        db.close()

    def test_next_name_skips_pending_request_names(self):
        # One real pod named cv_pod_1, plus a pending request for cv_pod_2.
        real_pods = [{"name": "cv_pod_1"}]
        rm.create_pod_request("cv_pod_2", "CV", True, "user", "alice")
        combined = real_pods + [{"name": n} for n in rm.pending_request_names()]
        self.assertEqual(rm.next_name(combined, "CV"), "cv_pod_3")


class GpuUnavailableDetectTest(unittest.TestCase):
    def test_matches_runpod_phrases(self):
        self.assertTrue(rm.is_gpu_unavailable_error(
            "There are no longer any instances available with the requested "
            "specifications. Please refresh and try again."))
        self.assertTrue(rm.is_gpu_unavailable_error("no resources"))
        self.assertTrue(rm.is_gpu_unavailable_error("NO RESOURCES currently"))
        # Phrase 2 in isolation (would otherwise be masked by phrase 1 overlap).
        self.assertTrue(rm.is_gpu_unavailable_error(
            "instances available with the requested gpu type"))

    def test_rejects_other_errors(self):
        self.assertFalse(rm.is_gpu_unavailable_error("invalid api key"))
        self.assertFalse(rm.is_gpu_unavailable_error("template not found"))
        self.assertFalse(rm.is_gpu_unavailable_error(""))
        self.assertFalse(rm.is_gpu_unavailable_error(None))

    def test_error_is_runtimeerror_subclass(self):
        self.assertTrue(issubclass(rm.GpuUnavailableError, RuntimeError))


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

        def deploy_then_cancel(name, **kwargs):
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

    def test_settings_defaults_present(self):
        self.assertEqual(rm.DEFAULT_SETTINGS["pod_request_timeout_minutes"], 15)
        self.assertEqual(rm.DEFAULT_SETTINGS["pod_request_retry_interval_seconds"], 15)

    def test_pod_request_loop_callable(self):
        self.assertTrue(callable(rm.pod_request_loop))


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
