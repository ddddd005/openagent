"""Independent verifier acceptance for step 2 (V2-01..V2-04) on a real browser.

Runs the verifier-owned ``graph_interaction_acceptance.mjs`` harness (real
GraphWorkbench.vue, real Pinia stores, real Vue Flow) inside an isolated test-scoped
Vite server and headless Chromium. The product API, services, database and models are
never contacted; the only HTTP fixture answers workflow request-evidence reads.

V2-05 (C01 coordinate regression) is the pre-existing
``test_graph_port_geometry_browser.py`` and is run as a separate explicit command; it is
not duplicated here.

Run (backend directory):

    $env:TAVERN_CHROMIUM_EXECUTABLE = "<...>\\chrome.exe"
    & '..\\.venv\\Scripts\\python.exe' -m pytest -q tests/test_graph_interaction_acceptance.py \
        --basetemp <new isolated directory>
"""

import json
import os
from pathlib import Path
import subprocess

import pytest

from test_tavern_workbench_browser import browser_runtime

SCENARIOS = {
    "click-handle-closed",
    "drag-valid-edge",
    "drag-esc-release-valid",
    "drag-esc-release-blank",
    "drag-release-blank",
    "drag-incompatible-target",
    "hint-lifecycle",
    "switch-workflow-mid-gesture",
    "save-failure-persistent",
    "admission-failure-hint",
    "selection-mutual-exclusive",
    "ctrl-multi-selection",
    "edge-add-node-then-delete",
    "edge-locate-then-delete",
    "trash-and-keyboard-same-target",
    "stale-selection-no-change",
    "undo-prunes-selection",
    "switch-workflow-clears-selection",
    "locked-delete-keeps-selection",
    "esc-priority-menu-then-selection",
}


@pytest.fixture(scope="module")
def acceptance(tmp_path_factory):
    """Run the browser harness once and return its parsed report."""
    executable, packages = browser_runtime()
    evidence = tmp_path_factory.mktemp("graph-interaction-acceptance")
    completed = subprocess.run(
        [executable, str(Path(__file__).with_name("graph_interaction_acceptance.mjs")), str(evidence)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=420,
        env={**os.environ, "NODE_PATH": str(packages)}, check=False,
    )
    (evidence / "browser.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (evidence / "browser.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    report_path = evidence / "acceptance-report.json"
    assert report_path.is_file(), completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert completed.returncode == 0, (
        completed.stdout + completed.stderr
        + json.dumps(report.get("fatal"), ensure_ascii=False))
    # Isolation boundary: no product endpoint, service or model was reached.
    assert report.get("fatal") is None
    assert report["errors"] == []
    assert report["unexpected_requests"] == []
    assert report["unexpected_api_requests"] == []
    assert report["fixture_errors"] == []
    assert report["baseline"]["head"]
    assert all(report["baseline"]["files"].values())
    assert {row["name"] for row in report["scenarios"]} == SCENARIOS
    assert [row["name"] for row in report["scenarios"] if row.get("error")] == []
    report["evidence_dir"] = str(evidence)
    return report


def _by_name(report, name):
    return next(row for row in report["scenarios"] if row["name"] == name)


def _state(row, step):
    return next(entry["state"] for entry in row["steps"] if entry["step"] == step)


def _edges(state):
    return len(state["document"]["edges"])


def _nodes(state):
    return len(state["document"]["nodes"])


def _message(state):
    return state["dom"]["connectionMessage"]


def _assert_no_connection(state):
    assert state["flow"]["connectionStartHandle"] is None
    assert state["flow"]["connectionClickStartHandle"] is None
    assert state["connecting"] is None
    assert state["dom"]["connectingHandles"] == []
    assert state["dom"]["connectionPath"] is False


# ---------------------------------------------------------------------------
# V2-01: click-to-connect is closed; drag-to-connect still works.
# ---------------------------------------------------------------------------
def test_v2_01_click_to_connect_closed_and_drag_connect_works(acceptance):
    click = _by_name(acceptance, "click-handle-closed")
    before_edges = _edges(click["before"])
    assert click["before"]["connectOnClick"] is False
    after_source = _state(click, "after-click-source")
    _assert_no_connection(after_source)
    assert _message(after_source) is None
    assert _edges(after_source) == before_edges
    after_target = _state(click, "after-click-target")
    _assert_no_connection(after_target)
    assert _edges(after_target) == before_edges  # a second handle click never creates an edge
    after_blank = _state(click, "after-blank")
    _assert_no_connection(after_blank)
    assert _edges(after_blank) == before_edges

    drag = _by_name(acceptance, "drag-valid-edge")
    released = _state(drag, "after-release")
    assert _edges(released) == _edges(drag["before"]) + 1
    assert _message(released) is None
    _assert_no_connection(released)


# ---------------------------------------------------------------------------
# V2-02: real pointer/keyboard order: Escape mid-drag, then release on a valid
# target creates no edge, and the next drag works immediately.
# ---------------------------------------------------------------------------
def test_v2_02_escape_mid_drag_terminates_the_gesture(acceptance):
    row = _by_name(acceptance, "drag-esc-release-valid")
    before_edges = _edges(row["before"])
    during = _state(row, "before-esc")
    assert during["connecting"] is not None
    assert during["flow"]["connectionStartHandle"] is not None
    after_esc = _state(row, "after-esc")
    _assert_no_connection(after_esc)
    released = _state(row, "after-release-on-valid")
    assert _edges(released) == before_edges  # moving back over a valid target and releasing creates nothing
    assert _message(released) is None
    _assert_no_connection(released)
    next_drag = _state(row, "after-next-drag")
    assert _edges(next_drag) == before_edges + 1  # the very next drag succeeds
    assert _message(next_drag) is None

    blank = _by_name(acceptance, "drag-esc-release-blank")
    assert _edges(_state(blank, "after-release-blank")) == _edges(blank["before"])
    assert _message(_state(blank, "after-release-blank")) is None


# ---------------------------------------------------------------------------
# V2-03: blank / incompatible releases leave no edge or stale hint; hint
# lifecycle is bounded; switching workflows clears connection state.
# ---------------------------------------------------------------------------
def test_v2_03_release_hints_have_no_residue(acceptance):
    blank = _by_name(acceptance, "drag-release-blank")
    after_release = _state(blank, "after-release")
    assert _edges(after_release) == _edges(blank["before"])
    assert _message(after_release) is None
    _assert_no_connection(after_release)
    after_click = _state(blank, "after-blank-click")
    assert _message(after_click) is None

    incompatible = _by_name(acceptance, "drag-incompatible-target")
    rejected = _state(incompatible, "after-incompatible-release")
    assert _edges(rejected) == _edges(incompatible["before"])
    assert _message(rejected)
    assert rejected["flow"]["connectionStartHandle"] is None
    during_next = _state(incompatible, "during-next-drag")
    assert _message(during_next) is None  # a new gesture clears the previous reason
    assert _edges(_state(incompatible, "after-next-blank-release")) == _edges(incompatible["before"])

    hint = _by_name(acceptance, "hint-lifecycle")
    assert _message(_state(hint, "after-incompatible-release"))
    assert _message(_state(hint, "after-dismiss")) is None
    assert _message(_state(hint, "after-second-incompatible"))
    assert _message(_state(hint, "after-blank")) is None

    switch = _by_name(acceptance, "switch-workflow-mid-gesture")
    switched = _state(switch, "after-switch")
    _assert_no_connection(switched)
    assert _message(switched) is None
    assert switched["document"]["workflow_definition_id"] == acceptance["fixture"]["second"]
    after_release = _state(switch, "after-release")
    assert _message(after_release) is None
    assert _edges(after_release) == _edges(_state(switch, "after-switch"))
    drag_second = _state(switch, "after-drag-on-second")
    assert _edges(drag_second) == _edges(after_release) + 1
    back = _state(switch, "after-switch-back")
    assert back["document"]["workflow_definition_id"] == acceptance["fixture"]["first"]
    assert _edges(back) == _edges(switch["before"])  # the first document is untouched


def test_v2_03_save_failure_and_admission_failure_are_separate(acceptance):
    save = _by_name(acceptance, "save-failure-persistent")
    failed = _state(save, "after-failed-save")
    assert _edges(failed) == _edges(save["before"])
    assert _message(failed) is None
    assert "fixture save failure" in failed["editFailure"]
    succeeded = _state(save, "after-successful-save")
    assert _edges(succeeded) == _edges(save["before"]) + 1
    assert not succeeded["editFailure"]

    admission = _by_name(acceptance, "admission-failure-hint")
    injected = _state(admission, "after-injected-edge")
    rejected = _state(admission, "after-admission-release")
    assert _message(rejected)  # a real hint, not the persistent save feedback
    assert not rejected["editFailure"]
    assert _edges(rejected) == _edges(injected)  # nothing was submitted
    assert _message(_state(admission, "after-blank")) is None


# ---------------------------------------------------------------------------
# V2-04: selection/deletion integration.
# ---------------------------------------------------------------------------
def test_v2_04_selection_is_mutually_exclusive(acceptance):
    row = _by_name(acceptance, "selection-mutual-exclusive")
    node = _state(row, "after-node-click")
    assert len(node["selectedNodeIds"]) == 1 and node["selectedEdgeId"] is None
    assert node["dom"]["selectedEdgesDom"] == []
    edge = _state(row, "after-edge-click")
    assert edge["selectedNodeIds"] == []
    assert edge["selectedEdgeId"] == acceptance["fixture"]["edgeSel"]
    assert edge["dom"]["selectedNodesDom"] == []
    node2 = _state(row, "after-node-click-2")
    assert len(node2["selectedNodeIds"]) == 1 and node2["selectedEdgeId"] is None


def test_v2_04_ctrl_multi_selection_stays_exclusive(acceptance):
    row = _by_name(acceptance, "ctrl-multi-selection")
    first = _state(row, "after-first-node")
    assert len(first["selectedNodeIds"]) == 1 and first["selectedEdgeId"] is None
    multi = _state(row, "after-ctrl-second")
    assert len(multi["selectedNodeIds"]) == 2 and multi["selectedEdgeId"] is None
    assert set(multi["selectedNodeIds"]) == {
        acceptance["fixture"]["nodes"]["A"], acceptance["fixture"]["nodes"]["C"]}
    assert multi["dom"]["selectedEdgesDom"] == []
    edge = _state(row, "after-edge-click")
    assert edge["selectedNodeIds"] == []
    assert edge["selectedEdgeId"] == acceptance["fixture"]["edgeSel"]


def test_v2_04_add_and_locate_clear_edge_selection_and_delete_targets_are_safe(acceptance):
    add = _by_name(acceptance, "edge-add-node-then-delete")
    before_nodes, before_edges = _nodes(add["before"]), _edges(add["before"])
    assert _state(add, "after-edge-click")["selectedEdgeId"] == acceptance["fixture"]["edgeSel"]
    added = _state(add, "after-add-node")
    assert added["selectedEdgeId"] is None
    assert len(added["selectedNodeIds"]) == 1
    assert _nodes(added) == before_nodes + 1
    deleted = _state(add, "after-delete")
    assert _nodes(deleted) == before_nodes
    assert _edges(deleted) == before_edges  # Delete removed the new node only, never the old edge
    assert deleted["selectedNodeIds"] == [] and deleted["selectedEdgeId"] is None

    locate = _by_name(acceptance, "edge-locate-then-delete")
    assert _state(locate, "after-edge-click")["selectedEdgeId"] == acceptance["fixture"]["edgeSel"]
    located = _state(locate, "after-locate")
    assert located["selectedEdgeId"] is None
    assert located["selectedNodeIds"] == [acceptance["fixture"]["nodes"]["D"]]
    located_deleted = _state(locate, "after-delete")
    assert _nodes(located_deleted) == _nodes(locate["before"]) - 1
    assert _edges(located_deleted) == _edges(locate["before"])  # unrelated edge kept
    assert located_deleted["selectedNodeIds"] == []


def test_v2_04_delete_and_trash_share_the_selection_target(acceptance):
    row = _by_name(acceptance, "trash-and-keyboard-same-target")
    before_nodes, before_edges = _nodes(row["before"]), _edges(row["before"])
    assert _state(row, "after-node-click")["dom"]["trashButtons"] >= 1
    trashed = _state(row, "after-trash")
    assert _nodes(trashed) == before_nodes - 1
    assert trashed["selectedNodeIds"] == [] and trashed["selectedEdgeId"] is None
    keyboard = _state(row, "after-keyboard-delete")
    assert _nodes(keyboard) == before_nodes - 2  # the trashed node is already gone; Delete removed only C
    assert _edges(keyboard) == before_edges - 1  # deleting C removed its related edge

    stale = _by_name(acceptance, "stale-selection-no-change")
    assert stale["before"]["document"] == _state(stale, "after-delete")["document"]
    cleared = _state(stale, "after-delete")
    assert cleared["selectedNodeIds"] == [] and cleared["selectedEdgeId"] is None
    assert not cleared["editFailure"]

    undo = _by_name(acceptance, "undo-prunes-selection")
    assert _nodes(_state(undo, "after-add-node")) == _nodes(undo["before"]) + 1
    undone = _state(undo, "after-undo")
    assert _nodes(undone) == _nodes(undo["before"])
    assert undone["selectedNodeIds"] == []  # historical selection is not restored
    after_delete = _state(undo, "after-delete")
    assert after_delete["document"] == undone["document"]  # stale Delete is a no-change


def test_v2_04_switch_and_locked_editing_keep_selection_guards(acceptance):
    switch = _by_name(acceptance, "switch-workflow-clears-selection")
    switched = _state(switch, "after-switch")
    assert switched["document"]["workflow_definition_id"] == acceptance["fixture"]["second"]
    assert switched["selectedNodeIds"] == [] and switched["selectedEdgeId"] is None
    second_selected = _state(switch, "after-second-node-click")
    assert len(second_selected["selectedNodeIds"]) == 1
    back = _state(switch, "after-switch-back")
    assert back["document"]["workflow_definition_id"] == acceptance["fixture"]["first"]
    assert back["selectedNodeIds"] == [] and back["selectedEdgeId"] is None

    locked = _by_name(acceptance, "locked-delete-keeps-selection")
    before = _state(locked, "after-lock")
    assert before["editorLocked"] is True
    selected_before = before["selectedNodeIds"]
    attempted = _state(locked, "after-delete-attempt")
    assert attempted["document"] == before["document"]  # nothing was deleted
    assert attempted["selectedNodeIds"] == selected_before  # a failed delete keeps the valid selection
    assert attempted["editFailure"]
    unlocked = _state(locked, "after-unlock")
    assert unlocked["document"] == before["document"]

    esc = _by_name(acceptance, "esc-priority-menu-then-selection")
    assert _state(esc, "after-menu-open")["dom"]["addMenuOpen"] is True
    first_esc = _state(esc, "after-first-esc")
    assert first_esc["dom"]["addMenuOpen"] is False
    assert first_esc["selectedNodeIds"] == _state(esc, "after-node-click")["selectedNodeIds"]
    assert _state(esc, "after-second-esc")["selectedNodeIds"] == []


# ---------------------------------------------------------------------------
# Evidence: screenshots and the report are bound to the reviewed sources.
# ---------------------------------------------------------------------------
def test_acceptance_evidence_is_complete(acceptance):
    evidence = Path(acceptance["evidence_dir"])
    assert (evidence / "acceptance-report.json").is_file()
    for name in ("v2-01-drag-valid", "v2-02-esc-then-valid", "v2-04-edge-add-delete"):
        assert (evidence / f"{name}.png").is_file(), name
    files = acceptance["baseline"]["files"]
    assert set(files) == {
        "frontend/src/components/GraphWorkbench.vue",
        "frontend/src/stores/workflowGraph.ts",
        "frontend/src/domain/graphConnections.ts",
    }

