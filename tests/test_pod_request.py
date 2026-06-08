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
