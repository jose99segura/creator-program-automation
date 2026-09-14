"""Structural checks on the exported n8n workflows.

They do not prove a query is valid -- only running against n8n does that. They
catch the mistakes that import cleanly and fail later: a connection to a
renamed node, a node nothing routes to, an error output wired to nothing.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

N8N_DIR = Path(__file__).resolve().parent.parent / "n8n"
WORKFLOW_DIR = N8N_DIR / "workflows"
DASHBOARD_DIR = N8N_DIR / "dashboard"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.json"))

TRIGGER_TYPES = {
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.errorTrigger",
    "n8n-nodes-base.executeWorkflowTrigger",
    "n8n-nodes-base.webhook",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def workflow(stem: str) -> dict:
    return load(WORKFLOW_DIR / f"{stem}.json")


def node(wf: dict, name: str) -> dict:
    return next(n for n in wf["nodes"] if n["name"] == name)


def node_names(wf: dict) -> set[str]:
    return {n["name"] for n in wf["nodes"]}


def targets(wf: dict) -> set[str]:
    return {
        c["node"]
        for conn in wf["connections"].values()
        for outputs in conn.get("main", [])
        for c in outputs
    }


def strip_comments(code: str) -> str:
    return "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("//"))


def test_the_expected_workflows_exist() -> None:
    """A glob that silently matches nothing would turn every test below green."""
    assert [p.stem for p in WORKFLOWS] == ["01-pipeline", "02-payout", "03-errors", "04-dashboard"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_node_names_are_unique(path: Path) -> None:
    """Connections address nodes by name, so a duplicate is an ambiguous edge."""
    names = [n["name"] for n in load(path)["nodes"]]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_connection_points_at_a_node_that_exists(path: Path) -> None:
    wf = load(path)
    names = node_names(wf)
    for source, conn in wf["connections"].items():
        assert source in names, f"connection from unknown {source!r}"
        for outputs in conn.get("main", []):
            for c in outputs:
                assert c["node"] in names, f"{source!r} points at unknown {c['node']!r}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_node_is_reachable(path: Path) -> None:
    wf = load(path)
    triggers = {n["name"] for n in wf["nodes"] if n["type"] in TRIGGER_TYPES}
    assert triggers, "nothing can start this workflow"
    orphans = node_names(wf) - targets(wf) - triggers
    assert not orphans, f"unreachable nodes {sorted(orphans)}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_error_outputs_are_connected(path: Path) -> None:
    """A node routing errors to an unconnected output swallows them silently."""
    wf = load(path)
    for n in wf["nodes"]:
        if n.get("onError") != "continueErrorOutput":
            continue
        outputs = wf["connections"].get(n["name"], {}).get("main", [])
        assert len(outputs) >= 2 and outputs[1], f"{n['name']!r} error output goes nowhere"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_no_secrets_in_the_export(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    for marker in ("xoxb-", "sk-", "-----BEGIN", "postgres://", "postgresql://"):
        assert marker not in raw, f"looks like a credential ({marker})"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_nothing_reads_the_environment(path: Path) -> None:
    """$env is blocked on the instance and resolves to undefined without an error."""
    uses = re.findall(r"\$env\.[A-Z][A-Z0-9_]+", path.read_text(encoding="utf-8"))
    assert not uses, f"reads {sorted(set(uses))}; use the config table instead"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_config_is_loaded_before_it_is_used(path: Path) -> None:
    wf = load(path)
    if "$('Config')" in path.read_text(encoding="utf-8"):
        assert "Config" in node_names(wf)


@pytest.mark.parametrize("stem", ["01-pipeline", "02-payout", "04-dashboard"])
def test_failures_go_to_the_error_workflow(stem: str) -> None:
    assert workflow(stem)["settings"]["errorWorkflow"] == workflow("03-errors")["id"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_webhook_has_a_stable_id(path: Path) -> None:
    """Without webhookId n8n registers the path under a generated prefix, and callers get 404."""
    for n in load(path)["nodes"]:
        if n["type"] == "n8n-nodes-base.webhook":
            assert n.get("webhookId"), f"{n['name']!r} has no webhookId"


def test_an_invalid_applicant_is_dead_lettered_once() -> None:
    """The index and the ON CONFLICT clause only work as a pair."""
    schema = (N8N_DIR / "sql" / "01-schema.sql").read_text(encoding="utf-8")
    assert "idx_dead_letters_one_open_invalid" in schema
    query = node(workflow("01-pipeline"), "Dead letter")["parameters"]["query"]
    assert "ON CONFLICT" in query and "DO NOTHING" in query


def test_a_crashed_execution_is_dead_lettered_once() -> None:
    schema = (N8N_DIR / "sql" / "01-schema.sql").read_text(encoding="utf-8")
    assert "idx_dead_letters_one_per_crash" in schema
    query = node(workflow("03-errors"), "Dead letter")["parameters"]["query"]
    assert "ON CONFLICT (workflow_id, execution_id)" in query and "DO NOTHING" in query


def test_payout_amounts_stay_integer() -> None:
    """sum() over bigint returns numeric; without the cast every payout was a cent high."""
    query = node(workflow("02-payout"), "Recompute payouts")["parameters"]["query"]
    assert "::bigint * c.rate_cents_per_1k + 500) / 1000" in query


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------

def _load_build():
    spec = importlib.util.spec_from_file_location("dashboard_build", DASHBOARD_DIR / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dashboard_workflow_embeds_the_current_page() -> None:
    """Editing page.html without rebuilding must fail here, not in production."""
    build = _load_build()
    page = (DASHBOARD_DIR / "page.html").read_text(encoding="utf-8")
    assert node(workflow("04-dashboard"), build.NODE)["parameters"]["jsCode"] == build.render_js(page), (
        "04-dashboard.json is stale: run python n8n/dashboard/build.py")


def test_the_dashboard_never_writes_html_from_data() -> None:
    """Handles and error messages come from outside; markup from them is stored XSS."""
    page = (DASHBOARD_DIR / "page.html").read_text(encoding="utf-8")
    script = page[page.index("<script>"):]
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in strip_comments(script), f"the dashboard uses {forbidden}"


def test_the_dashboard_escapes_state_for_a_script_element() -> None:
    """The one line between an applicant's handle and script execution."""
    bs = chr(92)
    js = _load_build().render_js("<p>/*__STATE__*/null</p>")
    assert "replace(/</g, '" + bs * 2 + "u003c')" in js
    assert "/" + bs + "u2028/g" in js and "/" + bs + "u2029/g" in js
    # A literal U+2028/U+2029, or a plain space, inside the regex silently corrupts the data.
    assert chr(0x2028) not in js and chr(0x2029) not in js
    assert "/ /g" not in js
    assert "() => json" in js, "replacement must be a function, not a string"


def test_dashboard_actions_are_behind_auth_and_a_token() -> None:
    wf = workflow("04-dashboard")
    hooks = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"]
    assert len(hooks) == 2
    for hook in hooks:
        assert hook["parameters"].get("authentication") == "basicAuth", hook["name"]
        assert "httpBasicAuth" in hook.get("credentials", {}), hook["name"]
    assert "dashboard_action_token" in json.dumps(node(wf, "Token matches?"))
    assert "dashboard_action_token" in (N8N_DIR / "sql" / "02-config.sql").read_text(encoding="utf-8")


def test_the_dashboard_button_can_start_the_pipeline() -> None:
    """Calling a workflow with no Execute Workflow trigger fails only when someone presses the button."""
    target = node(workflow("04-dashboard"), "Run the pipeline")["parameters"]["workflowId"]["value"]
    pipeline = workflow("01-pipeline")
    assert target == pipeline["id"]
    trigger = next(n for n in pipeline["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflowTrigger")
    assert pipeline["connections"][trigger["name"]]["main"][0][0]["node"] == "Config"
