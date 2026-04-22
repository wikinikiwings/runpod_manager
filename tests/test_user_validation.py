"""Tests for runpod_manager.validate_user_input.
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
            rm.validate_user_input("alice", "")

    def test_unknown_project_is_rejected(self):
        """Project not in PROJECTS whitelist must raise."""
        with self.assertRaises(rm.UserValidationError):
            rm.validate_user_input("alice", "FAKEPROJECT")

    def test_none_project_is_rejected(self):
        """None project must raise (isinstance check)."""
        with self.assertRaises(rm.UserValidationError):
            rm.validate_user_input("alice", None)

    def test_valid_project_passes(self):
        """A project from PROJECTS must pass and return normalized values."""
        nick, proj = rm.validate_user_input("alice", "CV")
        self.assertEqual(proj, "CV")
        self.assertEqual(nick, "alice")

    def test_empty_nickname_is_rejected(self):
        """Empty nickname must raise regardless of valid project."""
        with self.assertRaises(rm.UserValidationError):
            rm.validate_user_input("", "CV")


if __name__ == "__main__":
    unittest.main()
