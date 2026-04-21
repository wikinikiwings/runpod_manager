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
