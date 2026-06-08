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
