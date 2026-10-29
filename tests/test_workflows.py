"""Static checks on the exported n8n workflows (run in CI on every push)."""
import json, pathlib, re
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = sorted((ROOT / "n8n" / "workflows").glob("*.json"))

# credential ids created by scripts/render_credentials.py
import importlib.util, io, contextlib, sys
_spec = importlib.util.spec_from_file_location("rc", ROOT / "scripts" / "render_credentials.py")


def rendered_credentials(tmp_path):
    env = tmp_path / ".env"
    env.write_text("POSTGRES_PASSWORD=x\nMATTERMOST_URL=https://m.example\n")
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = ["render", str(env)]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return json.loads(buf.getvalue())


FORBIDDEN = [
    (r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+(?:\.\d+)?\b", "private IP address"),
    (r"https?://(?!api\.zotero\.org|export\.arxiv\.org|arxiv\.org|scirate\.com|doi\.org|localhost)[a-z0-9.-]+\.[a-z]{2,}", "hard-coded external host"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}", "e-mail address"),
]


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_workflows_present():
    assert len(WF) == 6


@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_no_private_data(path):
    wf = load(path)
    assert wf.get("pinData") in ({}, None), "pinned data must be stripped"
    assert wf.get("active") is False
    assert "shared" not in wf and "versionMetadata" not in wf
    text = path.read_text(encoding="utf-8")
    for pattern, what in FORBIDDEN:
        hits = re.findall(pattern, text)
        assert not hits, f"{what}: {hits[:3]}"


@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_credentials_resolve(path, tmp_path):
    known = {(c["id"], c["type"]) for c in rendered_credentials(tmp_path)}
    for node in load(path)["nodes"]:
        for ctype, ref in (node.get("credentials") or {}).items():
            assert (ref["id"], ctype) in known, f"{node['name']}: unknown credential {ref}"


@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_connections_and_references(path):
    wf = load(path)
    names = {n["name"] for n in wf["nodes"]}
    assert len(names) == len(wf["nodes"]), "duplicate node names"
    for src, outs in wf["connections"].items():
        assert src in names, f"connection from unknown node {src}"
        for branches in outs.values():
            for br in branches:
                for c in br:
                    assert c["node"] in names, f"connection to unknown node {c['node']}"
    text = json.dumps(wf["nodes"])
    for ref in re.findall(r"\$\('([^']+)'\)", text) + re.findall(r'\$node\[\\?"([^"\\]+)\\?"\]', text):
        assert ref in names, f"expression references missing node {ref!r}"


@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_env_vars_are_provided_by_compose(path):
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
    provided = set(re.findall(r"^\s{6}([A-Z0-9_]+):", compose, re.M))
    used = set(re.findall(r"\$env\.([A-Z0-9_]+)", path.read_text(encoding="utf-8")))
    missing = used - provided
    assert not missing, f"$env vars not set in compose.yml n8n service: {sorted(missing)}"


def test_sub_workflow_targets_exist():
    ids = {load(p)["id"] for p in WF}
    for p in WF:
        for n in load(p)["nodes"]:
            if n["type"].endswith("executeWorkflow"):
                assert n["parameters"]["workflowId"]["value"] in ids
            err = load(p).get("settings", {}).get("errorWorkflow")
            if err:
                assert err in ids


@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_code_nodes_avoid_missing_sandbox_globals(path):
    """n8n's task-runner sandbox has no URL/URLSearchParams globals (verified on 2.27.4)."""
    for node in load(path)["nodes"]:
        code = node.get("parameters", {}).get("jsCode", "")
        assert not re.search(r"\bnew URL\(|\bURLSearchParams\b", code), node["name"]



def test_qkb_webhooks():
    """Webhooks for other apps: present, wired into the existing flows, and the curation input matches the form."""
    wf = load(ROOT / "n8n" / "workflows" / "mattermost-control.json")
    nodes = {n["name"]: n for n in wf["nodes"]}
    paths = {n["parameters"].get("path"): n for n in wf["nodes"] if n["type"].endswith(".webhook")}
    assert {"podcast-file", "curate-paper-direct", "curation-status"} <= set(paths)
    assert len([p for p in paths if p]) == len(set(p for p in paths if p)), "duplicate webhook paths"
    gen = nodes["Generate from QKB File"]["parameters"]
    assert gen["workflowId"]["value"] == nodes["Generate from File"]["parameters"]["workflowId"]["value"]
    assert gen["workflowInputs"]["value"]["data"] == "={{ $binary.File }}" and gen["workflowInputs"]["value"]["arxivID"] == "="
    assert nodes["Notify QKB File Podcast"].get("onError") == "continueRegularOutput"
    out = wf["connections"]["Tier Change Only?"]["main"]
    assert [c["node"] for c in out[1]] == ["Input Mode Selection"]
    labels = {f["fieldLabel"] for f in nodes["Curation Form Node"]["parameters"]["formFields"]["values"]}
    code = nodes["QKB Curation Input"]["parameters"]["jsCode"]
    used = set(re.findall(r"'([A-Z][A-Za-z ]+)': ", code))
    assert used and used <= labels, used - labels
    modes = [r["conditions"]["conditions"][0]["rightValue"] for r in nodes["Input Mode Selection"]["parameters"]["rules"]["values"]]
    assert "'arXiv ID'" in code and "'Manual Entry'" in code and {"arXiv ID", "Manual Entry"} <= set(modes)
    assert nodes["QKB Curation Status"]["parameters"]["responseMode"] == "responseNode"



def test_curation_stores_missing_ids_as_null():
    """n8n renders a missing value as the text 'null' in queryReplacement; it must not reach arxiv_id (the upsert key)."""
    wf = load(ROOT / "n8n" / "workflows" / "mattermost-control.json")
    q = {n["name"]: n for n in wf["nodes"]}["Update Paper DB"]["parameters"]["query"]
    assert "NULLIF(NULLIF($2, ''), 'null')" in q and "NULLIF(NULLIF($3, ''), 'null')" in q
