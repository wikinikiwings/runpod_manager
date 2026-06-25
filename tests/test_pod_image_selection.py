"""Tests for per-project pod image selection (resolver + settings validation
+ deploy threading). Runs against temp SQLite/settings, no Docker required.
Run: python -m unittest tests.test_pod_image_selection -v
"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import runpod_manager as rm


CATALOG = [
    {"label": "Default", "template_id": "def0000001"},
    {"label": "CV image", "template_id": "cvtpl00001"},
]


def _settings(**over):
    base = {
        "pod_image_catalog": CATALOG,
        "default_pod_image": "def0000001",
        "project_pod_image": {},
    }
    base.update(over)
    return base


class ResolveTemplateTest(unittest.TestCase):
    def test_project_with_choice(self):
        s = _settings(project_pod_image={"CV": "cvtpl00001"})
        with mock.patch.object(rm, "get_settings", return_value=s):
            self.assertEqual(rm.resolve_template_id("CV"), "cvtpl00001")

    def test_project_without_choice_uses_default(self):
        with mock.patch.object(rm, "get_settings", return_value=_settings()):
            self.assertEqual(rm.resolve_template_id("CV"), "def0000001")

    def test_choice_no_longer_in_catalog_falls_back_to_default(self):
        s = _settings(project_pod_image={"CV": "deleted999"})
        with mock.patch.object(rm, "get_settings", return_value=s):
            self.assertEqual(rm.resolve_template_id("CV"), "def0000001")

    def test_none_project_uses_default(self):
        with mock.patch.object(rm, "get_settings", return_value=_settings()):
            self.assertEqual(rm.resolve_template_id(None), "def0000001")

    def test_empty_catalog_falls_back_to_preset(self):
        s = _settings(pod_image_catalog=[], default_pod_image="")
        with mock.patch.object(rm, "get_settings", return_value=s):
            self.assertEqual(rm.resolve_template_id("CV"), rm.PRESET["template_id"])

    def test_catalog_entry_missing_template_id_falls_back_to_preset(self):
        # A malformed catalog entry (no template_id) must not let a project
        # with no choice (tid=None) match a None in `valid` and return None.
        s = _settings(pod_image_catalog=[{"label": "broken"}], default_pod_image="")
        with mock.patch.object(rm, "get_settings", return_value=s):
            self.assertEqual(rm.resolve_template_id("CV"), rm.PRESET["template_id"])
