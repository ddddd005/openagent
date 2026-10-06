"""Run the dependency-free browser script tests with the regular Python suite."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_frontend_run_controls():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    script = Path(__file__).with_name("frontend_controls.test.cjs")
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
