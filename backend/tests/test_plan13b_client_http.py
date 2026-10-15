"""Current workbench manifest agrees with its registered frontend package."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from phase1_agent.builtin_packages import builtin_capability_packages
from phase1_agent.capability_packages import CapabilityPackageLoader


def test_actual_workbench_manifest_matches_registered_package():
    node_binary = shutil.which("node")
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    typescript = frontend / "node_modules" / "typescript"
    if node_binary is None or not typescript.is_dir():
        pytest.skip("Node.js and the installed TypeScript dependency are required")
    loaded = CapabilityPackageLoader(builtin_capability_packages()).load({"workflow.frontend": "1.0.0"})
    script = r"""
const ts = require(process.argv[1]), fs = require("node:fs"), assert = require("node:assert/strict");
const exports = {};
const source = ts.transpileModule(fs.readFileSync(process.argv[2], "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
new Function("require", "exports", source)(require, exports);
const sort = items => items.sort((a, b) => a.extension_id.localeCompare(b.extension_id));
assert.deepEqual(sort(exports.workflowFrontendExtensions), sort(JSON.parse(process.argv[3])));
assert.deepEqual(new Set(exports.workflowFrontendExtensions.map(row => row.extension_id)), new Set([
  "workflow.frontend.chat-session-object", "workflow.frontend.node-entry-fields-v2",
]));
assert.ok(!exports.workflowFrontendExtensions.some(row => row.extension_id === "workflow.frontend.public-output"));
"""
    result = subprocess.run(
        [node_binary, "-e", script, str(typescript),
         str(frontend / "src" / "plugins" / "workflowFrontendManifest.ts"),
         json.dumps(list(loaded.frontend_extensions))],
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
