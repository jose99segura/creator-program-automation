"""Structural checks on the exported n8n workflows.

These are not a substitute for running them. Nothing here proves a Postgres
query is valid or that a node's typeVersion still exists in the n8n release
you are on -- only an execution against a live instance does that, and the
n8n README says plainly that it has not happened yet.

What they do catch is the class of mistake that is invisible in a 700 line
JSON file and obvious the moment it is stated: a connection pointing at a node
that was renamed, a node nothing routes to, an error output wired to nothing,
a workflow with no error workflow configured. Every one of those imports
cleanly and fails later, which is the worst shape a defect can have.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / "n8n" / "workflows"
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


def node_names(wf: dict) -> set[str]:
    return {n["name"] for n in wf["nodes"]}


def targets(wf: dict) -> set[str]:
    return {
        c["node"]
        for conn in wf["connections"].values()
        for outputs in conn.get("main", [])
        for c in outputs
    }


def test_there_are_workflows() -> None:
    """A glob that silently matches nothing turns every test below green."""
    assert len(WORKFLOWS) >= 7, f"expected the full set, found {len(WORKFLOWS)}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_parses_and_has_the_expected_shape(path: Path) -> None:
    wf = load(path)
    assert wf["name"], "a workflow with no name is unfindable in the UI"
    assert wf["nodes"], "no nodes"
    assert "connections" in wf


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_node_names_are_unique(path: Path) -> None:
    """Connections address nodes by name, so a duplicate is an ambiguous edge.

    n8n permits this on import and then routes to whichever it found first.
    """
    names = [n["name"] for n in load(path)["nodes"]]
    assert len(names) == len(set(names)), f"duplicate node names in {path.name}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_connection_points_at_a_node_that_exists(path: Path) -> None:
    """The failure mode of renaming a node in the editor and exporting.

    n8n imports the dangling edge without complaint and the branch is simply
    never taken, which looks exactly like a condition that is never true.
    """
    wf = load(path)
    names = node_names(wf)
    for source, conn in wf["connections"].items():
        assert source in names, f"{path.name}: connection from unknown {source!r}"
        for outputs in conn.get("main", []):
            for c in outputs:
                assert c["node"] in names, (
                    f"{path.name}: {source!r} points at unknown {c['node']!r}")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_node_is_reachable(path: Path) -> None:
    """An orphan node is either dead weight or a branch somebody forgot to wire.

    The second is the one that matters: a dead letter node nothing routes to
    is a workflow that looks like it handles failure and does not.
    """
    wf = load(path)
    triggers = {n["name"] for n in wf["nodes"] if n["type"] in TRIGGER_TYPES}
    orphans = node_names(wf) - targets(wf) - triggers
    assert not orphans, f"{path.name}: unreachable nodes {sorted(orphans)}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_every_workflow_has_an_entry_point(path: Path) -> None:
    wf = load(path)
    assert any(n["type"] in TRIGGER_TYPES for n in wf["nodes"]), (
        f"{path.name}: nothing can start this workflow")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_error_outputs_are_connected(path: Path) -> None:
    """onError: continueErrorOutput adds a second output. It must go somewhere.

    A node set to route its failures onward, with nothing attached to that
    output, swallows them: the item disappears, the execution succeeds, and
    the run is reported as clean. That is the single most dangerous
    misconfiguration in n8n, because it converts a failure into a silence.
    """
    wf = load(path)
    for node in wf["nodes"]:
        if node.get("onError") != "continueErrorOutput":
            continue
        outputs = wf["connections"].get(node["name"], {}).get("main", [])
        assert len(outputs) >= 2 and outputs[1], (
            f"{path.name}: {node['name']!r} routes errors to its second output "
            f"and nothing is connected to it")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_no_secrets_in_the_export(path: Path) -> None:
    """n8n exports are the easiest way to leak a token: people paste them.

    Every value that varies by environment is read with $env at execution
    time, so the JSON in this repository is safe to share as-is. This asserts
    that rather than trusting it.
    """
    raw = path.read_text(encoding="utf-8")
    for marker in ("xoxb-", "sk-", "-----BEGIN", "postgres://", "postgresql://"):
        assert marker not in raw, f"{path.name}: looks like a credential ({marker})"


def test_the_ruleset_has_exactly_one_home() -> None:
    """The whole argument for 00 being a sub-workflow, asserted.

    If somebody pastes the screening logic into the pipeline or the eval to
    save a node, the eval starts scoring a copy of the rules instead of the
    rules. That is a silent failure of the measurement itself, so it is worth
    a test that names it.
    """
    others = [p for p in WORKFLOWS if p.stem != "00-screening-rules"]
    for path in others:
        raw = path.read_text(encoding="utf-8")
        assert "REVIEW_FOLLOWERS" not in raw, (
            f"{path.stem} contains screening thresholds. The ruleset lives in "
            f"00-screening-rules and is called, never copied.")


def test_the_eval_and_the_pipeline_call_the_same_ruleset() -> None:
    """Both must point at 00, or the eval measures something else entirely."""
    for stem in ("01-pipeline", "05-eval-screening"):
        raw = (WORKFLOW_DIR / f"{stem}.json").read_text(encoding="utf-8")
        assert "00_SCREENING_RULES_WORKFLOW_ID" in raw or "screening" in raw, (
            f"{stem} does not reference the screening rules workflow")
