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
    assert [p.stem for p in WORKFLOWS] == [
        "01-pipeline", "02-payout", "03-errors", "04-dashboard", "05-prep", "99-fake-providers"]


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


STICKY = "n8n-nodes-base.stickyNote"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_node_is_reachable(path: Path) -> None:
    wf = load(path)
    triggers = {n["name"] for n in wf["nodes"] if n["type"] in TRIGGER_TYPES}
    assert triggers, "nothing can start this workflow"
    notes = {n["name"] for n in wf["nodes"] if n["type"] == STICKY}
    orphans = node_names(wf) - targets(wf) - triggers - notes
    assert not orphans, f"unreachable nodes {sorted(orphans)}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_workflow_explains_itself_on_the_canvas(path: Path) -> None:
    """Opening a workflow in n8n should say what it does before anyone reads a node."""
    notes = [n for n in load(path)["nodes"] if n["type"] == STICKY]
    summary = [n for n in notes if n["name"] == "Nota: resumen"]
    assert summary, "no summary sticky note"
    assert len(summary[0]["parameters"]["content"]) > 200, "summary note is too thin to explain anything"
    testing = [n for n in notes if n["name"] == "Nota: probar"]
    assert testing, "no note saying how to test it"
    assert "Cómo probarlo" in testing[0]["parameters"]["content"]


@pytest.mark.parametrize(
    "path", [p for p in WORKFLOWS if p.stem != "99-fake-providers"], ids=lambda p: p.stem)
def test_every_node_is_explained_in_a_note(path: Path) -> None:
    """Every working node is named in bold in some note of its workflow.

    Renaming or adding a node without updating the notes fails here, so the
    explanation on the canvas cannot quietly drift from what the workflow does.
    Triggers are described in words ("cada 6 horas") rather than by name.
    """
    wf = load(path)
    text = "\n".join(n["parameters"]["content"] for n in wf["nodes"] if n["type"] == STICKY)
    mentioned = set(re.findall(r"\*\*(.+?)\*\*", text))
    working = {n["name"] for n in wf["nodes"]
               if n["type"] != STICKY and n["type"] not in TRIGGER_TYPES - {"n8n-nodes-base.webhook"}}
    unexplained = working - mentioned
    assert not unexplained, f"nodes no note explains: {sorted(unexplained)}"


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


@pytest.mark.parametrize("stem", ["01-pipeline", "02-payout", "04-dashboard", "05-prep"])
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


def test_a_repeated_crash_does_not_alert_again() -> None:
    """With no row returned, the Postgres node still emits an item, so the alert needs its own gate.

    Found in production: n8n re-reported an old crashed execution on restart,
    the dead letter was correctly skipped, and the alert went out anyway.
    """
    wf = workflow("03-errors")
    assert wf["connections"]["Dead letter"]["main"][0][0]["node"] == "New failure?"
    assert wf["connections"]["New failure?"]["main"][0][0]["node"] == "Alert"
    assert "$json.id" in json.dumps(node(wf, "New failure?")["parameters"])


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


PAGES = [("04-dashboard", DASHBOARD_DIR / "page.html"), ("05-prep", N8N_DIR / "prep" / "page.html")]


@pytest.mark.parametrize("stem,page_path", PAGES, ids=[p[0] for p in PAGES])
def test_the_workflow_embeds_the_current_page(stem: str, page_path: Path) -> None:
    """Editing a page without rebuilding must fail here, not in production."""
    build = _load_build()
    page = page_path.read_text(encoding="utf-8")
    assert node(workflow(stem), build.NODE)["parameters"]["jsCode"] == build.render_js(page), (
        f"{stem}.json is stale: run python n8n/dashboard/build.py")


@pytest.mark.parametrize("stem,page_path", PAGES, ids=[p[0] for p in PAGES])
def test_the_page_never_writes_html_from_data(stem: str, page_path: Path) -> None:
    """Handles, error messages and model output come from outside; markup from them is stored XSS."""
    page = page_path.read_text(encoding="utf-8")
    script = page[page.index("<script>"):]
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in strip_comments(script), f"{stem} uses {forbidden}"


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


def test_prep_actions_are_behind_auth_and_a_token() -> None:
    wf = workflow("05-prep")
    hooks = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"]
    assert len(hooks) == 2
    for hook in hooks:
        assert hook["parameters"].get("authentication") == "basicAuth", hook["name"]
        assert "httpBasicAuth" in hook.get("credentials", {}), hook["name"]
    assert "dashboard_action_token" in json.dumps(node(wf, "Token matches?"))
    assert wf["connections"]["Prep action"]["main"][0][0]["node"] == "Config"
    assert wf["connections"]["Config"]["main"][0][0]["node"] == "Token matches?"


def test_the_api_key_lives_in_a_credential() -> None:
    """The key is a Header Auth credential; a header typed into the node would be exported with it."""
    ask = node(workflow("05-prep"), "Ask Claude")
    assert ask["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert "httpHeaderAuth" in ask["credentials"]
    headers = [h["name"].lower() for h in ask["parameters"]["headerParameters"]["parameters"]]
    assert "x-api-key" not in headers and "authorization" not in headers


def test_the_model_is_only_called_after_a_repeat_check() -> None:
    """A double click must not pay for the same case, question or grading twice."""
    wf = workflow("05-prep")
    assert wf["connections"]["Plan the call"]["main"][0][0]["node"] == "Needs the model?"
    assert wf["connections"]["Needs the model?"]["main"][0][0]["node"] == "Ask Claude"
    plan = node(wf, "Plan the call")["parameters"]["jsCode"]
    assert "if (attempt) return skip('case')" in plan
    assert "Number(body.turn) !== transcript.length" in plan
    assert "if (attempt.feedback) return skip('answer')" in plan


def test_a_reply_is_validated_before_it_is_saved() -> None:
    wf = workflow("05-prep")
    assert wf["connections"]["Ask Claude"]["main"][0][0]["node"] == "Check the reply"
    assert wf["connections"]["Check the reply"]["main"][0][0]["node"] == "Reply valid?"
    assert wf["connections"]["Reply valid?"]["main"][0][0]["node"] == "Save the attempt"
    assert wf["connections"]["Reply valid?"]["main"][1][0]["node"] == "Report the failure"


def test_saving_an_attempt_never_overwrites_what_is_there() -> None:
    query = node(workflow("05-prep"), "Save the attempt")["parameters"]["query"]
    assert "ON CONFLICT (id) DO UPDATE" in query
    assert "COALESCE(prep_attempts.feedback, EXCLUDED.feedback)" in query
    assert "COALESCE(prep_attempts.answer, EXCLUDED.answer)" in query


def test_deploy_ships_the_prep_workflow_and_table() -> None:
    deploy = (N8N_DIR / "deploy.sh").read_text(encoding="utf-8")
    assert workflow("05-prep")["id"] in deploy
    assert "03-prep.sql" in deploy
    assert "prep_model" in (N8N_DIR / "sql" / "02-config.sql").read_text(encoding="utf-8")


def test_the_dashboard_button_can_start_the_pipeline() -> None:
    """Calling a workflow with no Execute Workflow trigger fails only when someone presses the button."""
    target = node(workflow("04-dashboard"), "Run the pipeline")["parameters"]["workflowId"]["value"]
    pipeline = workflow("01-pipeline")
    assert target == pipeline["id"]
    trigger = next(n for n in pipeline["nodes"] if n["type"] == "n8n-nodes-base.executeWorkflowTrigger")
    assert pipeline["connections"][trigger["name"]]["main"][0][0]["node"] == "Config"
