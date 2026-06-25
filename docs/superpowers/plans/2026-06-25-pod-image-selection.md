# Per-Project Pod Image Selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the admin maintain a catalog of pod images (`{label → template_id}`) and assign one per project, so each project's pods deploy on its chosen RunPod template.

**Architecture:** Three new keys in `admin_settings.json` (`pod_image_catalog`, `default_pod_image`, `project_pod_image`). A pure resolver `resolve_template_id(project)` picks the template (project choice → global default → `PRESET` fallback). The chosen `template_id` is threaded through `create_pod` / `create_pod_via_graphql` and the auto-retry worker into the `DeployOnDemand` mutation's `input.templateId`. Admin GET/POST `/api/admin/settings` is extended with a pure validation helper; the admin UI gets a catalog editor + per-project dropdown grid.

**Tech Stack:** Python 3.12 stdlib + Flask (no new deps), SQLite (unchanged — no DB migration), inline HTML/JS frontend, `unittest` tests.

## Global Constraints

- No new third-party dependencies — stdlib + Flask only.
- All settings persist in `admin_settings.json` via existing `load_settings`/`save_settings`; `load_settings` already shallow-merges `DEFAULT_SETTINGS`, so new keys backfill automatically.
- `template_id` validation pattern: `^[A-Za-z0-9_-]+$`; `label` non-empty after `strip()`, max 60 chars.
- The catalog must never become empty — an empty/invalid submitted catalog leaves the previous catalog intact.
- Seed catalog entry is the current template: `{"label": "Текущий (comfy_runpod)", "template_id": "i3j2sm66q8"}`, and `default_pod_image = "i3j2sm66q8"` — so behavior is unchanged until the admin switches a project.
- Identity of a catalog entry is its `template_id` (unique). `label` is display-only.
- Follow existing code style: compact, typed `except` clauses, helpers defined before consumers.
- New tests live in `tests/test_pod_image_selection.py`, run via `python -m unittest tests.test_pod_image_selection -v` (mirror `tests/test_pod_request.py`).
- Branch: `feat/pod-image-selection` (already created; spec already committed there).

---

### Task 1: Settings keys + `resolve_template_id`

**Files:**
- Modify: `runpod_manager.py` — `DEFAULT_SETTINGS` (around line 80-102)
- Modify: `runpod_manager.py` — Settings section, right after `get_settings()` (line 743)
- Test: `tests/test_pod_image_selection.py`

**Interfaces:**
- Produces: `resolve_template_id(project: str | None) -> str` — returns the `template_id` to deploy for `project`. Priority: project's catalog choice (if still in catalog) → `default_pod_image` (if in catalog) → `PRESET["template_id"]`.
- Produces: settings keys `pod_image_catalog: list[{"label","template_id"}]`, `default_pod_image: str`, `project_pod_image: dict[project→template_id]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pod_image_selection.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_image_selection.ResolveTemplateTest -v`
Expected: FAIL with `AttributeError: module 'runpod_manager' has no attribute 'resolve_template_id'`

- [ ] **Step 3: Add the three settings keys**

In `runpod_manager.py`, in `DEFAULT_SETTINGS`, immediately after the `"project_quotas":{...},` line (line 81), insert:

```python
    # Per-project pod image selection. Catalog of {label, template_id} the admin
    # edits in the panel; default_pod_image is the template_id used for projects
    # with no explicit choice, unassigned pods, and admin pods. project_pod_image
    # maps project -> template_id (missing key = use default). Seeded with the
    # current template so behavior is unchanged until the admin switches a project.
    "pod_image_catalog":[{"label":"Текущий (comfy_runpod)","template_id":"i3j2sm66q8"}],
    "default_pod_image":"i3j2sm66q8",
    "project_pod_image":{},
```

- [ ] **Step 4: Add the resolver**

In `runpod_manager.py`, immediately after `def get_settings(): return load_settings()` (line 743), insert:

```python
def resolve_template_id(project):
    """RunPod template_id to deploy for a pod belonging to `project` (may be None
    for unassigned/admin pods). Priority: the project's catalog choice if it still
    exists, then the global default, then PRESET as a last resort if the catalog
    is empty/broken."""
    s = get_settings()
    catalog = s.get("pod_image_catalog") or []
    valid = {e.get("template_id") for e in catalog if isinstance(e, dict)}
    tid = (s.get("project_pod_image") or {}).get(project)
    if tid in valid:
        return tid
    if s.get("default_pod_image") in valid:
        return s["default_pod_image"]
    return PRESET["template_id"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_image_selection.ResolveTemplateTest -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add runpod_manager.py tests/test_pod_image_selection.py
git commit -m "feat(image-select): settings keys + resolve_template_id"
```

---

### Task 2: Thread `template_id` through deploy functions

**Files:**
- Modify: `runpod_manager.py` — `create_pod_via_graphql` (def line 1271; `templateId` line 1293)
- Modify: `runpod_manager.py` — `create_pod` (def line 1363; GraphQL call line 1392; CLI fallback lines 1404-1418)
- Test: `tests/test_pod_image_selection.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `create_pod_via_graphql(name: str, template_id: str | None = None) -> dict` — puts `template_id or PRESET["template_id"]` into `variables.input.templateId`.
- Produces: `create_pod(name: str, bypass_window: bool = False, template_id: str | None = None) -> dict` — forwards `template_id` to GraphQL and to the CLI `--template-id` / `--templateId`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pod_image_selection.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_image_selection.DeployThreadingTest -v`
Expected: FAIL — `test_passed_template_id_used` errors with `TypeError: create_pod_via_graphql() got an unexpected keyword argument 'template_id'`

- [ ] **Step 3: Update `create_pod_via_graphql`**

Change the signature (line 1271):

```python
def create_pod_via_graphql(name, template_id=None):
```

Change the `templateId` line (line 1293) from:

```python
            "templateId": PRESET["template_id"],
```

to:

```python
            "templateId": template_id or PRESET["template_id"],
```

- [ ] **Step 4: Update `create_pod`**

Change the signature (line 1363):

```python
def create_pod(name, bypass_window=False, template_id=None):
```

Change the GraphQL call (line 1392) from:

```python
            return create_pod_via_graphql(name)
```

to:

```python
            return create_pod_via_graphql(name, template_id=template_id)
```

In the new-CLI fallback branch, change the template line (line 1409) from:

```python
        if PRESET.get("template_id"): cmd+=["--template-id",PRESET["template_id"]]
```

to:

```python
        tid = template_id or PRESET.get("template_id")
        if tid: cmd+=["--template-id",tid]
```

In the old-CLI fallback branch, change the template line (line 1417) from:

```python
        if PRESET.get("template_id"): cmd+=["--templateId",PRESET["template_id"]]
```

to:

```python
        tid = template_id or PRESET.get("template_id")
        if tid: cmd+=["--templateId",tid]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_image_selection.DeployThreadingTest -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add runpod_manager.py tests/test_pod_image_selection.py
git commit -m "feat(image-select): thread template_id through create_pod + GraphQL deploy"
```

---

### Task 3: Wire resolver into call sites

**Files:**
- Modify: `runpod_manager.py` — `api_pods_post` (line 1797)
- Modify: `runpod_manager.py` — `process_pending_requests` (line 1557)
- Test: `tests/test_pod_image_selection.py`

**Interfaces:**
- Consumes: `resolve_template_id` (Task 1), `create_pod(..., template_id=)` and `create_pod_via_graphql(..., template_id=)` (Task 2).
- Produces: deploys triggered by HTTP create and by the auto-retry worker use the project's resolved template.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pod_image_selection.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_image_selection.AutoRetryTemplateTest -v`
Expected: FAIL — `captured["tid"]` is `None` (worker still calls `create_pod_via_graphql(req["pod_name"])` without a template).

- [ ] **Step 3: Wire the auto-retry worker**

In `process_pending_requests`, change the deploy call (line 1557) from:

```python
                result = create_pod_via_graphql(req["pod_name"])
```

to:

```python
                tid = resolve_template_id(req["assigned_project"])
                result = create_pod_via_graphql(req["pod_name"], template_id=tid)
```

- [ ] **Step 4: Wire the HTTP create route**

In `api_pods_post`, change the create call (line 1797) from:

```python
        result = create_pod(name, bypass_window=admin)
```

to:

```python
        result = create_pod(name, bypass_window=admin,
                            template_id=resolve_template_id(ap))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_image_selection.AutoRetryTemplateTest -v`
Expected: PASS (1 test)

- [ ] **Step 6: Run the full suite to check no regressions**

Run: `python -m unittest tests.test_pod_image_selection tests.test_pod_request -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add runpod_manager.py tests/test_pod_image_selection.py
git commit -m "feat(image-select): resolve project template in create route + auto-retry"
```

---

### Task 4: Admin API — GET response + POST validation

**Files:**
- Modify: `runpod_manager.py` — Settings section, after `resolve_template_id` (Task 1)
- Modify: `runpod_manager.py` — `api_admin_settings_get` (lines 1944-1963)
- Modify: `runpod_manager.py` — `api_admin_settings_post` (after the `project_quotas` block, ~line 1979)
- Test: `tests/test_pod_image_selection.py`

**Interfaces:**
- Consumes: `PROJECTS`, `re` (both module globals).
- Produces: `compute_image_settings_update(data: dict, s: dict) -> dict` — pure; returns only the validated subset of `{pod_image_catalog, default_pod_image, project_pod_image}` keys present in `data`. Never returns an empty catalog.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pod_image_selection.py`:

```python
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
                "project_pod_image": {"CV": "aaa", "CV": "aaa",
                                      "NOPE": "aaa", "DV": "missing"}}
        out = rm.compute_image_settings_update(data, self._cur())
        self.assertEqual(out["project_pod_image"], {"CV": "aaa"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_pod_image_selection.ImageSettingsValidationTest -v`
Expected: FAIL with `AttributeError: module 'runpod_manager' has no attribute 'compute_image_settings_update'`

- [ ] **Step 3: Add the validation helper**

In `runpod_manager.py`, immediately after `resolve_template_id` (Task 1), insert:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_pod_image_selection.ImageSettingsValidationTest -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Extend the GET response**

In `api_admin_settings_get`, just before the `return jsonify(...)` (line 1959), add catalog seeding:

```python
    catalog = s.get("pod_image_catalog") or list(DEFAULT_SETTINGS["pod_image_catalog"])
```

Then add these three keys to the returned `"settings"` dict (alongside `project_quotas` / `project_autodelete_offset_minutes`, line 1960):

```python
        "pod_image_catalog": catalog,
        "default_pod_image": s.get("default_pod_image") or DEFAULT_SETTINGS["default_pod_image"],
        "project_pod_image": s.get("project_pod_image") or {},
```

- [ ] **Step 6: Extend the POST handler**

In `api_admin_settings_post`, immediately after the `project_quotas` block (after `s["project_quotas"] = quotas`, line 1979), insert:

```python
    # Per-project pod image selection (catalog + default + project map).
    s.update(compute_image_settings_update(data, s))
```

- [ ] **Step 7: Run the full backend suite**

Run: `python -m unittest tests.test_pod_image_selection tests.test_pod_request -v`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add runpod_manager.py tests/test_pod_image_selection.py
git commit -m "feat(image-select): admin settings GET/POST + validation helper"
```

---

### Task 5: Admin UI — catalog editor + per-project grid

**Files:**
- Modify: `runpod_manager.py` — `FRONTEND_HTML`, `loadAdminPanel()` settings render (after the Per-project quotas section, ~line 2454)
- Modify: `runpod_manager.py` — `FRONTEND_HTML`, helper JS near `sbSave` (~line 2624)
- Modify: `runpod_manager.py` — `FRONTEND_HTML`, `sbSave()` body assembly (lines 2624-2642)

**Interfaces:**
- Consumes: GET `/api/admin/settings` keys from Task 4 (`pod_image_catalog`, `default_pod_image`, `project_pod_image`); POST accepts the same keys.
- Produces: admin can add/remove catalog entries, mark a default, and pick a per-project image; Save posts all three keys.

This task is UI-only; the repo has no frontend test harness, so it ends with manual verification instead of an automated test.

- [ ] **Step 1: Render the catalog editor + per-project grid**

In `loadAdminPanel()`, immediately after the closing of the "Per-project quotas" `sb-section` div (the `'</div>'+` that follows the quota `sb-dim`, line 2454), insert a new section:

```javascript
      '<div class="sb-section"><h3>🖼 Образы подов</h3>'+
        '<div id="imgCatalog"></div>'+
        '<button class="btn" type="button" onclick="imgAddRow()">+ Добавить образ</button>'+
        '<div class="sb-dim">Каталог образов: понятное название + RunPod template_id. ⭐ — образ по умолчанию (его нельзя удалить).</div>'+
        '<div style="margin-top:8px;font-size:12px;color:var(--t2)">Образ на проект:</div>'+
        '<div class="quota-grid" id="imgProjGrid" style="margin-top:4px"></div>'+
      '</div>'+
```

- [ ] **Step 2: Add the catalog/grid render + add/delete JS**

Just above `async function sbSave(){` (line 2624), insert:

```javascript
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
function imgSetDefault(i){ imgReadCatalogFromDom(); _imgDefault=_imgCatalog[i].template_id; imgRenderCatalog(); imgRenderGrid(); }
function imgRenderCatalog(){
  $('imgCatalog').innerHTML=_imgCatalog.map((e,i)=>{
    const isDef=e.template_id && e.template_id===_imgDefault;
    return '<div class="fr imgRow" data-i="'+i+'">'+
      '<input class="imgLabel" placeholder="название" value="'+(e.label||'').replace(/"/g,'&quot;')+'" style="flex:1">'+
      '<input class="imgTid" placeholder="template_id" value="'+(e.template_id||'').replace(/"/g,'&quot;')+'" oninput="imgRenderGrid()" style="flex:1">'+
      '<button class="btn" type="button" title="Сделать образом по умолчанию" onclick="imgSetDefault('+i+')">'+(isDef?'⭐':'☆')+'</button>'+
      '<button class="btn" type="button" title="Удалить" onclick="imgDelRow('+i+')"'+(isDef?' disabled':'')+'>🗑</button>'+
    '</div>';
  }).join('');
}
function imgRenderGrid(){
  imgReadCatalogFromDom();
  const defLabel=(_imgCatalog.find(e=>e.template_id===_imgDefault)||{}).label||'дефолт';
  $('imgProjGrid').innerHTML=_imgProjects.map(p=>{
    const sel=_imgProjSel[p]||'';
    const opts=['<option value="">По умолчанию ('+defLabel+')</option>']
      .concat(_imgCatalog.filter(e=>e.template_id).map(e=>
        '<option value="'+e.template_id+'"'+(e.template_id===sel?' selected':'')+'>'+(e.label||e.template_id)+'</option>'));
    return '<div class="fr"><label>'+p+'</label><select class="imgProj" data-proj="'+p+'">'+opts.join('')+'</select></div>';
  }).join('');
}
```

- [ ] **Step 3: Call `imgInit` after the panel renders**

In `loadAdminPanel()`, after the `$('sbContent').innerHTML=...` assignment completes and before the function's other post-render calls (search for `updateOffsetHints()` / `updateSchedHint()` calls near the end of `loadAdminPanel`), add:

```javascript
  imgInit(s);
```

- [ ] **Step 4: Collect image settings in `sbSave` and POST them**

In `sbSave()` (line 2624), after the `offsets` collection block (line 2632) add:

```javascript
  imgReadCatalogFromDom();
  const podImageCatalog = _imgCatalog
    .map(e=>({label:(e.label||'').trim(), template_id:(e.template_id||'').trim()}))
    .filter(e=>e.label && e.template_id);
  const projImg = {};
  document.querySelectorAll('.imgProj').forEach(el=>{ if(el.value) projImg[el.dataset.proj]=el.value; });
```

Then add these three keys to the POST body object (inside the `aok('/api/admin/settings','POST',{...})` call, alongside `project_quotas`, line 2633):

```javascript
    pod_image_catalog:podImageCatalog,
    default_pod_image:_imgDefault,
    project_pod_image:projImg,
```

- [ ] **Step 5: Manual verification**

Run the app locally (or in Docker) and open the admin panel → Settings:

```bash
python runpod_manager.py --port 5001
# open http://localhost:5001 , log in as admin (default password "admin")
```

Verify:
1. "🖼 Образы подов" section shows one row: "Текущий (comfy_runpod)" / `i3j2sm66q8` with ⭐ and a disabled 🗑.
2. "+ Добавить образ" adds an empty row; typing a `template_id` makes it appear in every project `<select>`.
3. Clicking ☆ on the new row moves the default ⭐ to it; the old default's 🗑 un-disables, the new default's 🗑 disables.
4. Set a project (e.g. CV) to the new image, click Save → toast "Saved".
5. Reload the panel → selections persisted (GET round-trips). Confirm in `admin_settings.json` that `pod_image_catalog` / `default_pod_image` / `project_pod_image` are written.
6. Deleting the default is blocked with toast "Нельзя удалить образ по умолчанию".

- [ ] **Step 6: Commit**

```bash
git add runpod_manager.py
git commit -m "feat(image-select): admin UI — image catalog editor + per-project grid"
```

---

### Task 6: Documentation

**Files:**
- Modify: `docs/architecture.md` — `DEFAULT_SETTINGS` table
- Modify: `docs/admin-panel.md` — new settings section
- Modify: `docs/graphql-deploy.md` — `templateId` is now per-project
- Modify: `docs/pod-images.md` — resolve Flag #1 framing (selection now exists in UI)
- Modify: `TODO.md` — journal entry
- No test (docs only).

- [ ] **Step 1: Update `docs/architecture.md`**

In the `DEFAULT_SETTINGS` table (under "Глобальные константы"), add three rows:

```markdown
| `pod_image_catalog` | `[{label:"Текущий…",template_id:"i3j2sm66q8"}]` | Каталог образов подов (label + RunPod template_id), редактируется в админке |
| `default_pod_image` | `"i3j2sm66q8"` | template_id образа по умолчанию (проекты без выбора, unassigned, admin-поды) |
| `project_pod_image` | `{}` | Per-project выбор образа `{project: template_id}`; нет ключа = дефолт |
```

Also add to the "GraphQL deploy" / "Pod operations" section note that `resolve_template_id(project)` (Settings section) feeds `template_id` into `create_pod` / `create_pod_via_graphql`.

- [ ] **Step 2: Update `docs/admin-panel.md`**

Add a section describing the "🖼 Образы подов" settings block: catalog editor (add/remove/default), per-project dropdown, and that selection maps project → template_id used in `DeployOnDemand`. Note validation rules (non-empty label/template_id, `^[A-Za-z0-9_-]+$`, catalog never empty, default must be in catalog).

- [ ] **Step 3: Update `docs/graphql-deploy.md`**

In the variables table, change the `templateId` row source from "PRESET" to: "resolve_template_id(project) — выбранный для проекта образ из каталога настроек; fallback PRESET['template_id']". Add a short paragraph under the mutation explaining per-project image selection links to the catalog in admin settings.

- [ ] **Step 4: Update `docs/pod-images.md`**

Update "⚠️ Флаг #1" to note that per-project image selection now exists in the admin panel (catalog of template_ids), so the deployed template is chosen at runtime per project — the `PRESET["image"]` string remains only a CLI-fallback/display label and should still be reconciled with the real template `i3j2sm66q8`.

- [ ] **Step 5: Update `TODO.md`**

Add a journal entry under "Журнал предыдущих TODO/решений":

```markdown
### ✅ DONE: Per-project pod image selection (2026-06-25)
Админ ведёт каталог образов `{label → template_id}` и назначает образ каждому
проекту (по аналогии с project_quotas). Деплой резолвит template_id из проекта
(`resolve_template_id`) и кладёт в `DeployOnDemand`/CLI `--template-id`; авторетрай
перерезолвит на каждой попытке. Один глобальный дефолт, засев текущим i3j2sm66q8
(поведение неизменно до переключения). Три ключа настроек + валидация
(`compute_image_settings_update`), новый блок «🖼 Образы подов» в админке.
Спека/план: `docs/superpowers/{specs,plans}/2026-06-25-pod-image-selection*.md`.
Тесты: `tests/test_pod_image_selection.py`.
```

- [ ] **Step 6: Commit**

```bash
git add docs/ TODO.md
git commit -m "docs(image-select): document per-project image selection across docs + TODO journal"
```

---

## Self-Review

**Spec coverage** (against `2026-06-25-pod-image-selection-design.md`):
- Model data (3 keys) → Task 1 ✓
- Resolver → Task 1 ✓
- Deploy flow (`create_pod_via_graphql`/`create_pod` signatures, CLI fallback) → Task 2 ✓
- Wiring `api_pods_post` + auto-retry per-attempt re-resolve → Task 3 ✓
- Admin GET seed + POST validation (dedupe, empty catalog, default fallback, project filtering) → Task 4 ✓
- Admin UI (catalog editor, per-project grid, default lock) → Task 5 ✓
- Tests (resolver, validation, GET backfill, deploy mock) → Tasks 1/2/4 ✓ (GET backfill verified manually in Task 5 step 5 + covered by seed logic; resolver/validation/deploy automated)
- Edge cases (delete in-use → fallback; bad template_id → mutation error path) → handled by resolver tolerance (Task 1) + validation (Task 4) ✓
- Docs → Task 6 ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases" — all steps carry concrete code/commands. ✓

**Type consistency:** `resolve_template_id(project)`, `create_pod_via_graphql(name, template_id=None)`, `create_pod(name, bypass_window=False, template_id=None)`, `compute_image_settings_update(data, s)` — names/signatures consistent across Tasks 1-4 and the tests. ✓

> Note: the GET-backfill spec test (spec §Тестирование item 3) is satisfied by the seed logic in Task 4 Step 5 and verified in Task 5 manual step; it is not a separate automated test because asserting it cleanly requires temp-`SETTINGS_FILE` plumbing not present in the existing harness. If desired, add a small test that sets `rm.SETTINGS_FILE` to a temp path and asserts `compute_image_settings_update` + load round-trip — optional, low value.
