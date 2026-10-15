"""Offline node-private browser client checks, with no browser or HTTP service."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_node_private_client():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node.js is required for the offline frontend-node client tests")
    completed = subprocess.run(
        [executable, "--test", str(Path(__file__).with_name("frontend_node_client.test.cjs"))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
