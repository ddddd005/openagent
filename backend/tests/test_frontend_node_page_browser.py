"""Rendered node page with every network request intercepted by a test fixture."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_node_page_offline_browser(tmp_path):
    executable = shutil.which("node")
    bundle = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node"
    packages = Path(os.environ.get("TAVERN_BROWSER_NODE_MODULES", str(bundle / "node_modules")))
    if executable is None or not (packages / "playwright").is_dir():
        pytest.skip("An installed Playwright runtime is required for offline node-page rendering")
    environment = {**os.environ, "NODE_PATH": str(packages)}
    completed = subprocess.run(
        [executable, str(Path(__file__).with_name("frontend_node_page_browser.cjs")), str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        env=environment, check=False,
    )
    (tmp_path / "browser.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (tmp_path / "browser.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((tmp_path / "page-report.json").read_text(encoding="utf-8"))
    assert report["errors"] == [] and report["unexpected"] == []
    assert len(report["scenarios"]) == 14 and len(report["layouts"]) == 10
