"""Rendered provider/profile workflow with isolated ports and no network transport."""

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess

from phase1_agent.graph_application import GraphApplication
from phase1_agent.graph_service import GraphWorkflowService
from test_tavern_workbench_browser import browser_runtime


def test_provider_profile_browser_recovery_without_upstream(tmp_path):
    node, packages = browser_runtime()
    root = Path(__file__).resolve().parents[2]
    assert (root / "frontend" / "dist" / "index.html").is_file(), "Build current frontend first"
    with closing(GraphWorkflowService(tmp_path / "fixture.sqlite")) as service:
        fixture = GraphApplication(service).query("catalog.node-types", {"protocol_version": 2})
    seed = tmp_path / "catalog.json"
    seed.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("model_configuration_browser.cjs")), str(seed), str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=160,
        env={**os.environ, "NODE_PATH": str(packages)}, check=False,
    )
    (tmp_path / "browser.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "browser.stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads((tmp_path / "browser-report.json").read_text(encoding="utf-8"))
    assert report["page_errors"] == [] and report["unexpected"] == []
    assert report["key_saves"] == 4 and report["profile_saves"] == 2
    assert report["provider_config_saves"] == 1
    assert report["provider_receipt_reads"] == 2
    assert report["scenarios"] == [
        "single-private-key-route", "draft-only-until-save", "key-does-not-lock-or-reset-fields",
        "no-new-password-hint", "provider-draft-cold-reload-key-memory-only",
        "single-action-whole-provider", "existing-config-and-key-together",
        "only-inflight-fields-locked", "blank-key-preserves-saved-key", "enter-saves-whole-provider",
        "provider-cold-reconcile-no-replay", "partial-save-truthful-failure",
        "partial-cold-reconcile-original-no-mutation", "partial-original-evidence-archive-no-mutation",
        "manual-capacity", "profile-roundtrip",
        "receipt-only-recovery", "archive-failure-retains", "archive-unlocks",
        "reload-form-archive-failure", "reload-form-explicit", "reference-only-node",
        "desktop-and-narrow-layout",
    ]
