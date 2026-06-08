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

    def test_pod_request_table_exists(self):
        db = sqlite3.connect(self.db_path)
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pod_request'")
        self.assertIsNotNone(cur.fetchone())
        # Index exists too
        cur = db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_pr_status'")
        self.assertIsNotNone(cur.fetchone())
        db.close()


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
