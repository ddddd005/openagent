"""Real-browser canvas integration acceptance for S2-03 (Q04-1..Q04-4).

The probe runs the real ``GraphWorkbench.vue``, the real Pinia stores and real Vue Flow
1.48.2 in headless Chromium inside an isolated test-scoped Vite server. It never touches
the product API, services, database or models.

S2-01 scope (kept asserted): click-to-connect closed, drag-only, Escape cancels the
in-flight gesture with real pointer ordering, blank/incompatible releases end without
residue, specific rejection hints, save failure vs admission failure separation.

S2-03 scope (asserted): every component selection write goes through the store's
selectNodes/selectEdge/clearSelection/pruneSelection actions; node (multi) and edge
(single) selection are mutually exclusive; node/edge/blank/box timing is checked with
real pointer events; add/locate clear edge selection; Delete/trash share removeSelection
and prune stale identities; switching workflows clears selection, connection state and
hints; the trash entry only appears for an existing selection.

Run (backend directory), with an available Chromium runtime:

    $env:TAVERN_CHROMIUM_EXECUTABLE = "<...>\\chrome.exe"
    & '..\\.venv\\Scripts\\python.exe' -m pytest -q tests/test_graph_interaction_probe.py \
        --basetemp <new isolated directory>
"""

import json
import os
from pathlib import Path
import subprocess

import pytest

from test_tavern_workbench_browser import browser_runtime

EXPECTED_SCENARIOS = {
    # Connection lifecycle (S2-01).
    "click-handle-no-pending",
    "click-handle-then-blank",
    "click-handle-then-valid-target",
    "click-handle-then-illegal-target",
    "drag-valid-target",
    "drag-release-on-blank",
    "drag-over-target-then-release-on-blank",
    "drag-incompatible-target",
    "drag-incompatible-then-valid-drag",
    "drag-esc-preserves-selection",
    "drag-esc-release-on-valid-target",
    "drag-esc-release-on-blank",
    "drag-esc-after-focus-blur",
    "esc-on-idle-canvas",
    "drag-to-source-handle",
    "rejection-message-dismiss",
    "rejection-message-cleared-by-blank-click",
    "admission-failure-on-release-uses-hint",
    "input-occupied-target-message",
    "drag-target-to-source-handle",
    "locked-drag-handle",
    "switch-workflow-clears-connection-state",
    "mid-gesture-switch-creates-no-edge",
    "save-failure-uses-persistent-feedback",
    # Selection/delete integration (S2-03).
    "selection-click-node",
    "selection-click-edge",
    "selection-node-click-clears-edge",
    "selection-edge-click-clears-nodes",
    "selection-ctrl-click-multi-nodes",
    "selection-box-multi-nodes",
    "selection-esc-clears",
    "selection-pane-click-clears",
    "selection-drag-release-keeps-selection",
    "edge-then-add-node-then-delete",
    "edge-then-locate-then-delete",
    "undo-after-add-node",
    "delete-node-clears-selection",
    "delete-edge-clears-selection",
    "delete-in-editor-input-keeps-node",
    "trash-button-deletes-selection",
    "locked-delete",
    "switch-workflow-clears-selection",
    "esc-closes-menu-before-clearing-selection",
    "handle-click-selection-observation",
}

# Q04-1/Q04-2 expectations (kept from S2-01).
EXPECTED_REPRODUCTIONS = {
    "connect_on_click_disabled",
    "click_handle_creates_no_pending_state",
    "click_handle_creates_no_edge",
    "click_handle_then_blank_creates_no_edge",
    "click_handle_then_valid_target_creates_no_edge",
    "click_handle_then_illegal_target_creates_no_edge",
    "drag_valid_target_shows_hint_then_creates_edge",
    "drag_valid_target_clears_hint_after_save",
    "blank_release_creates_no_edge_and_no_hint",
    "hover_then_blank_release_clears_hint",
    "incompatible_target_creates_no_edge_and_shows_reason",
    "rejected_hint_clears_when_next_connection_starts",
    "rejection_hint_dismissible",
    "rejection_hint_cleared_by_blank_click",
    "esc_cancels_in_flight_gesture_and_keeps_selection",
    "esc_then_release_over_valid_target_creates_no_edge",
    "esc_then_next_drag_creates_edge",
    "esc_then_release_on_blank_creates_no_edge_and_no_hint",
    "esc_blank_then_next_drag_creates_edge",
    "esc_cancels_even_without_focused_canvas",
    "idle_esc_clears_selection_without_connection",
    "release_on_source_handle_shows_direction_reason",
    "occupied_input_shows_specific_reason",
    "admission_failure_uses_hint_not_persistent_error",
    "target_handle_start_reverse_drag_creates_edge",
    "locked_drag_starts_no_connection_and_creates_no_edge",
    "workflow_switch_clears_connection_state_and_hint",
    "drag_works_on_switched_workflow",
    "first_workflow_document_unchanged_after_switch_round_trip",
    "mid_gesture_switch_creates_no_edge_on_new_workflow",
    "mid_gesture_switch_leaves_old_document_unchanged",
    "save_failure_uses_persistent_feedback_not_connection_hint",
    "reconnect_after_save_failure_creates_edge",
}

# Q04-3/Q04-4 expectations (S2-03 integration).
EXPECTED_SELECTION_EXPECTATIONS = {
    "node_click_selects_only_that_node",
    "edge_click_selects_only_that_edge",
    "node_click_after_edge_clears_edge_selection",
    "edge_click_after_node_clears_node_selection",
    "ctrl_click_adds_node_to_selection",
    "ctrl_click_selected_node_removes_it",
    "box_selection_collects_multiple_nodes_and_clears_edge",
    "esc_clears_selection",
    "pane_click_clears_selection",
    "connection_drag_release_keeps_existing_selection",
    "add_node_clears_edge_selection",
    "delete_after_add_removes_only_the_new_node",
    "locate_clears_edge_selection",
    "delete_after_locate_removes_only_the_located_node",
    "undo_prunes_selection_of_removed_node",
    "delete_after_undo_is_a_no_change",
    "delete_node_clears_selection",
    "delete_edge_clears_selection",
    "delete_in_editor_input_does_not_delete_node",
    "trash_button_deletes_selected_node_and_clears_selection",
    "locked_delete_keeps_selection_and_persistent_failure",
    "switch_workflow_clears_selection_and_connection_state",
    "esc_closes_menu_before_clearing_selection",
    "handle_click_keeps_no_edge_selection_or_connection_state",
    "selection_is_always_mutually_exclusive",
}


def _edges(scenario):
    return scenario["steps"][-1]["state"]["document"]["edges"]


def _nodes(scenario):
    return scenario["steps"][-1]["state"]["document"]["nodes"]


def _last(scenario):
    return scenario["steps"][-1]["state"]


def _message(state):
    return state["dom"]["connectionMessage"]


def test_canvas_connection_and_selection_integration_on_real_browser(tmp_path):
    executable, packages = browser_runtime()
    evidence = tmp_path / "graph-interaction"
    completed = subprocess.run(
        [executable, str(Path(__file__).with_name("graph_interaction_probe.mjs")), str(evidence)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=420,
        env={**os.environ, "NODE_PATH": str(packages)}, check=False,
    )
    (tmp_path / "browser.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (tmp_path / "browser.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    report_path = evidence / "graph-interaction-report.json"
    assert report_path.is_file(), completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert completed.returncode == 0, (
        completed.stdout + completed.stderr + json.dumps(report.get("scenario_errors", []), ensure_ascii=False))

    # Isolation: no product endpoint, service or unexpected origin was used.
    assert report.get("fatal") is None
    assert report["errors"] == []
    assert report["unexpected_requests"] == []
    assert report["fixture_errors"] == []
    assert report["unexpected_api_requests"] == []
    assert report["baseline"]["head"]
    assert all(report["baseline"]["files"].values())
    assert report["ran_categories"] == ["connection", "selection"]

    scenarios = report["scenarios"]
    assert {row["name"] for row in scenarios} == EXPECTED_SCENARIOS
    connection_rows = [row for row in scenarios if row["category"] == "connection"]
    selection_rows = [row for row in scenarios if row["category"] == "selection"]
    assert len(connection_rows) == 24
    assert len(selection_rows) == 20
    assert [row["name"] for row in scenarios if row["error"]] == []

    reproductions = report["reproductions"]
    assert set(reproductions) == EXPECTED_REPRODUCTIONS
    misread = {name: value for name, value in reproductions.items() if value is not True}
    assert misread == {}, f"Q04-1/Q04-2 expectations failed: {misread}"
    selection = report["selection_expectations"]
    assert set(selection) == EXPECTED_SELECTION_EXPECTATIONS
    selection_misread = {name: value for name, value in selection.items() if value is not True}
    assert selection_misread == {}, f"Q04-3/Q04-4 expectations failed: {selection_misread}"

    by_name = {row["name"]: row for row in scenarios}

    # S2-01 guarded regressions: click-to-connect closed and Esc terminates the gesture.
    click = by_name["click-handle-no-pending"]
    click_state = click["steps"][0]["state"]
    assert click_state["flow"]["connectionClickStartHandle"] is None
    assert click_state["flow"]["connectionStartHandle"] is None
    assert click_state["connecting"] is None
    assert click_state["dom"]["connectingHandles"] == []
    assert _message(click_state) is None
    assert len(_edges(click)) == len(click["before"]["document"]["edges"])
    esc = by_name["drag-esc-release-on-valid-target"]
    assert esc["steps"][0]["state"]["flow"]["connectionStartHandle"] is not None
    assert esc["steps"][1]["state"]["connecting"] is None
    assert esc["steps"][1]["state"]["flow"]["connectionStartHandle"] is None
    assert len(esc["steps"][3]["state"]["document"]["edges"]) == len(esc["before"]["document"]["edges"])
    assert len(_edges(esc)) == len(esc["before"]["document"]["edges"]) + 1  # next drag works

    # S2-03 selection integration details.
    node_click = by_name["selection-click-node"]["steps"][0]["state"]
    assert len(node_click["selectedNodeIds"]) == 1 and node_click["selectedEdgeId"] is None
    edge_click = by_name["selection-click-edge"]["steps"][0]["state"]
    assert edge_click["selectedEdgeId"] is not None and edge_click["selectedNodeIds"] == []

    multi = by_name["selection-ctrl-click-multi-nodes"]
    assert len(multi["steps"][0]["state"]["selectedNodeIds"]) == 1
    assert len(multi["steps"][1]["state"]["selectedNodeIds"]) == 2
    assert len(multi["steps"][2]["state"]["selectedNodeIds"]) == 1

    box = by_name["selection-box-multi-nodes"]
    assert len(box["steps"][0]["state"]["selectedNodeIds"]) == 0
    assert box["steps"][0]["state"]["selectedEdgeId"] is not None
    assert len(box["steps"][2]["state"]["selectedNodeIds"]) == 2
    assert box["steps"][2]["state"]["selectedEdgeId"] is None

    # A connection drag keeps the pre-existing selection and does not fire a trailing
    # pane/node click that would rewrite it.
    drag_selection = by_name["selection-drag-release-keeps-selection"]
    assert drag_selection["steps"][0]["state"]["selectedNodeIds"] == drag_selection["steps"][1]["state"]["selectedNodeIds"]
    assert len(drag_selection["steps"][0]["state"]["selectedNodeIds"]) == 1

    add_delete = by_name["edge-then-add-node-then-delete"]
    assert add_delete["steps"][1]["state"]["selectedEdgeId"] is None
    assert len(add_delete["steps"][1]["state"]["selectedNodeIds"]) == 1
    assert len(_edges(add_delete)) == len(add_delete["before"]["document"]["edges"])

    locate_delete = by_name["edge-then-locate-then-delete"]
    assert locate_delete["steps"][1]["state"]["selectedEdgeId"] is None
    assert len(locate_delete["steps"][1]["state"]["selectedNodeIds"]) == 1
    assert len(_edges(locate_delete)) == len(locate_delete["before"]["document"]["edges"])
    assert len(_nodes(locate_delete)) == len(locate_delete["before"]["document"]["nodes"]) - 1

    undo = by_name["undo-after-add-node"]
    undone = undo["steps"][1]["state"]
    existing = {node["id"] for node in undone["document"]["nodes"]}
    assert all(node_id in existing for node_id in undone["selectedNodeIds"])
    assert undone["selectedNodeIds"] == []
    assert undo["steps"][2]["state"]["document"] == undone["document"]

    delete_node = by_name["delete-node-clears-selection"]
    assert len(_nodes(delete_node)) == len(delete_node["before"]["document"]["nodes"]) - 1
    assert _last(delete_node)["selectedNodeIds"] == []
    delete_edge = by_name["delete-edge-clears-selection"]
    assert len(_edges(delete_edge)) == len(delete_edge["before"]["document"]["edges"]) - 1
    assert _last(delete_edge)["selectedEdgeId"] is None
    input_delete = by_name["delete-in-editor-input-keeps-node"]
    assert len(_nodes(input_delete)) == len(input_delete["before"]["document"]["nodes"])
    assert _last(input_delete)["selectedNodeIds"] == input_delete["steps"][0]["state"]["selectedNodeIds"]
    trash = by_name["trash-button-deletes-selection"]
    assert len(_nodes(trash)) == len(trash["before"]["document"]["nodes"]) - 1
    assert _last(trash)["selectedNodeIds"] == [] and _last(trash)["selectedEdgeId"] is None

    switch = by_name["switch-workflow-clears-selection"]
    switched = switch["steps"][1]["state"]
    assert switched["selectedNodeIds"] == [] and switched["selectedEdgeId"] is None
    assert switched["flow"]["connectionStartHandle"] is None
    assert _message(switched) is None
    assert switch["steps"][2]["state"]["selectedNodeIds"] == []

    # Esc priority: the first Escape closes the menu and keeps the selection; a second
    # Escape clears the selection (one action per key press).
    menu_esc = by_name["esc-closes-menu-before-clearing-selection"]
    assert menu_esc["steps"][1]["menuOpen"] is True
    assert menu_esc["steps"][2]["menuOpen"] is False
    assert menu_esc["steps"][2]["state"]["selectedNodeIds"] == menu_esc["steps"][0]["state"]["selectedNodeIds"]
    assert menu_esc["steps"][3]["state"]["selectedNodeIds"] == []

    # S2-01 failure separation still holds after integration.
    save_failure = by_name["save-failure-uses-persistent-feedback"]
    assert "fixture save failure" in save_failure["steps"][2]["state"]["editFailure"]
    assert _message(save_failure["steps"][2]["state"]) is None
    assert len(save_failure["steps"][2]["state"]["document"]["edges"]) == len(
        save_failure["before"]["document"]["edges"])

    assert (evidence / "00-ready.png").is_file()
    assert (evidence / "08-esc-release-on-valid-target.png").is_file()
    assert (evidence / "16-ctrl-click-multi-nodes.png").is_file()
    assert (evidence / "17-box-multi-nodes.png").is_file()
