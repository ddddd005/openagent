"""Offline client evidence checks for logical chat actions."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_node_logical_chat_client():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node.js is required for offline frontend-node client tests")
    completed = subprocess.run(
        [executable, "--test", str(Path(__file__).with_name("frontend_node_chat_client.test.cjs"))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
