"""Real canvas handles on a local fixture, without product state or model calls."""

import json
import os
from pathlib import Path
import subprocess

from test_tavern_workbench_browser import browser_runtime


def test_graph_port_geometry_after_catalog_and_port_order_changes(tmp_path):
    executable, packages = browser_runtime()
    completed = subprocess.run(
        [executable, str(Path(__file__).with_name("graph_port_geometry_browser.mjs")), str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        env={**os.environ, "NODE_PATH": str(packages)}, check=False,
    )
    (tmp_path / "browser.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (tmp_path / "browser.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((tmp_path / "port-report.json").read_text(encoding="utf-8"))
    assert report["errors"] == [] and report["unexpected_requests"] == []
    assert len(report["scenarios"]) == 4
    for scenario in report["scenarios"]:
        assert scenario["start"]["handleId"] == "receipts"
        assert scenario["start"]["clicked"] == "receipts"
        assert scenario["connection"]["sourceHandle"] == "receipts"
        assert scenario["connection"]["targetHandle"] == "receipts"
