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


class DeployThreadingTest(unittest.TestCase):
    class _FakeResp:
        def __init__(self, payload):
            self._b = json.dumps(payload).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._b

    def _run_deploy(self, **kwargs):
        captured = {}
        ok = {"data": {"podFindAndDeployOnDemand": {"id": "p1", "imageName": "img"}}}

        def fake_urlopen(req, timeout=0):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return self._FakeResp(ok)

        with mock.patch.object(rm, "_api_key", "k"), \
             mock.patch.object(rm.urllib.request, "urlopen", fake_urlopen):
            rm.create_pod_via_graphql("cv_pod_1", **kwargs)
        return captured["body"]["variables"]["input"]["templateId"]

    def test_passed_template_id_used(self):
        self.assertEqual(self._run_deploy(template_id="tpl_X00001"), "tpl_X00001")

    def test_defaults_to_preset_template(self):
        self.assertEqual(self._run_deploy(), rm.PRESET["template_id"])


class AutoRetryTemplateTest(unittest.TestCase):
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

    def test_worker_deploys_with_project_template(self):
        rm.create_pod_request("cv_pod_1", "CV", True, "user", "alice")
        s = _settings(project_pod_image={"CV": "cvtpl00001"},
                      pod_request_timeout_minutes=15)
        captured = {}

        def fake_deploy(name, template_id=None):
            captured["tid"] = template_id
            return {"id": "p1"}

        with mock.patch.object(rm, "get_settings", return_value=s), \
             mock.patch.object(rm, "create_pod_via_graphql", side_effect=fake_deploy), \
             mock.patch.object(rm, "upsert_pod_assignment"), \
             mock.patch.object(rm, "log_action"):
            rm.process_pending_requests()

        self.assertEqual(captured["tid"], "cvtpl00001")


class ImageSettingsValidationTest(unittest.TestCase):
    def _cur(self):
        return {
            "pod_image_catalog": [{"label": "Default", "template_id": "def0000001"}],
            "default_pod_image": "def0000001",
            "project_pod_image": {},
        }

    def test_dedupes_template_ids(self):
        data = {"pod_image_catalog": [
            {"label": "A", "template_id": "dup1"},
            {"label": "B", "template_id": "dup1"},
        ]}
        out = rm.compute_image_settings_update(data, self._cur())
        self.assertEqual(out["pod_image_catalog"], [{"label": "A", "template_id": "dup1"}])

    def test_rejects_bad_template_id_and_empty_label(self):
        data = {"pod_image_catalog": [
            {"label": "ok", "template_id": "good1"},
            {"label": "", "template_id": "noLabel"},
            {"label": "bad chars", "template_id": "has space"},
        ]}
        out = rm.compute_image_settings_update(data, self._cur())
        self.assertEqual(out["pod_image_catalog"], [{"label": "ok", "template_id": "good1"}])

    def test_empty_catalog_not_applied(self):
        data = {"pod_image_catalog": []}
        out = rm.compute_image_settings_update(data, self._cur())
        self.assertNotIn("pod_image_catalog", out)

    def test_default_outside_catalog_falls_to_first(self):
        data = {"pod_image_catalog": [
                    {"label": "A", "template_id": "aaa"},
                    {"label": "B", "template_id": "bbb"}],
                "default_pod_image": "zzz"}
        out = rm.compute_image_settings_update(data, self._cur())
        self.assertEqual(out["default_pod_image"], "aaa")

    def test_project_map_filters_unknown_project_and_template(self):
        data = {"pod_image_catalog": [{"label": "A", "template_id": "aaa"}],
                "default_pod_image": "aaa",
                "project_pod_image": {"CV": "aaa",
                                      "NOPE": "aaa", "DV": "missing"}}
        out = rm.compute_image_settings_update(data, self._cur())
        self.assertEqual(out["project_pod_image"], {"CV": "aaa"})
