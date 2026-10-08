"""Branch isolation without starting services or touching user databases."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_dev_launcher import WORKSPACE, call_bat, powershell


FAKE_LAUNCHER = r"""
param(
    [string]$PythonPath, [int]$BackendPort, [int]$FrontendPort,
    [string]$DatabasePath, [string]$Mode, [switch]$NoBrowser, [switch]$CheckOnly
)
if ($Mode -eq 'fail-after-entry') { exit 7 }
[ordered]@{
    root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    python = $PythonPath
    backend_port = $BackendPort
    frontend_port = $FrontendPort
    database = $DatabasePath
    mode = $Mode
    no_browser = [bool]$NoBrowser
    check_only = [bool]$CheckOnly
} | ConvertTo-Json -Compress
exit 0
"""


def git(root, *arguments):
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Launcher Test",
         "-c", "user.email=launcher-test@example.invalid", *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture
def branch_repo(tmp_path):
    powershell()
    root = tmp_path / "branch space \u4e2d\u6587"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("start.bat", "start-dev.bat"):
        shutil.copy2(WORKSPACE / name, root / name)
    shutil.copy2(WORKSPACE / "scripts" / "start-branch-services.ps1", scripts)
    (scripts / "start-services.ps1").write_text(FAKE_LAUNCHER, encoding="ascii")
    (root / ".gitignore").write_text(".local/\n", encoding="ascii")
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "stable fixture")
    stable_commit = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-c", "develop")
    (root / "develop-only.txt").write_text("development\n", encoding="ascii")
    git(root, "add", ".")
    git(root, "commit", "-m", "development fixture")
    stable = root / ".local" / "stable-main"
    git(root, "worktree", "add", str(stable), "main")
    return root, stable, stable_commit


def resolved(script, *arguments):
    result = call_bat(script, *arguments, "-ResolveOnly")
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip())


def test_main_selects_stable_even_when_develop_has_new_commit_and_changes(branch_repo):
    root, stable, commit = branch_repo
    (root / "develop-only.txt").write_text("unsaved development\n", encoding="ascii")
    value = resolved(root / "start.bat")
    assert value["branch"] == "main" and value["git_commit"] == commit
    assert Path(value["repository_root"]).resolve() == stable.resolve()
    assert Path(value["database_path"]).resolve() == root / ".local" / "dev" / "workflow.sqlite"
    assert (value["backend_port"], value["frontend_port"]) == (8765, 5178)
    assert git(root, "branch", "--show-current") == "develop"
    assert (root / "develop-only.txt").read_text(encoding="ascii") == "unsaved development\n"
    assert not (root / ".local" / "dev").exists()


def test_develop_has_separate_default_ports_and_database(branch_repo):
    root, _, stable_commit = branch_repo
    value = resolved(root / "start-dev.bat")
    assert value["git_commit"] != stable_commit
    assert Path(value["repository_root"]).resolve() == root.resolve()
    assert (value["backend_port"], value["frontend_port"]) == (8766, 5179)
    assert Path(value["database_path"]).resolve() == root / ".local" / "develop" / "workflow.sqlite"
    assert not (root / ".local" / "develop").exists()


def test_missing_main_does_not_fall_back_or_switch_branch(branch_repo):
    root, stable, _ = branch_repo
    git(stable, "switch", "--detach")
    result = call_bat(root / "start.bat", "-ResolveOnly")
    assert result.returncode != 0
    assert "checked-out main" in result.stdout
    assert git(root, "branch", "--show-current") == "develop"


@pytest.mark.parametrize("tracked", [False, True], ids=["untracked", "tracked"])
def test_dirty_main_is_rejected_without_overwriting_files(branch_repo, tracked):
    root, stable, _ = branch_repo
    changed = stable / ("start.bat" if tracked else "untracked.txt")
    changed.write_text("keep this change\n", encoding="ascii")
    result = call_bat(root / "start.bat", "-ResolveOnly")
    assert result.returncode != 0 and "local changes" in result.stdout
    assert changed.read_text(encoding="ascii") == "keep this change\n"


@pytest.mark.parametrize("arguments", [
    ("-BackendPort", "8765"),
    ("-FrontendPort", "5178"),
    ("-BackendPort", "5178"),
    ("-FrontendPort", "8765"),
    ("-DatabasePath", ".local/dev/workflow.sqlite"),
    ("-BackendPort", "8801", "-FrontendPort", "8801"),
])
def test_develop_rejects_stable_resources_and_same_ports(branch_repo, arguments):
    root, _, _ = branch_repo
    result = call_bat(root / "start-dev.bat", *arguments, "-ResolveOnly")
    assert result.returncode != 0 and "[START FAILED]" in result.stdout
    assert not (root / ".local" / "dev").exists()
    assert not (root / ".local" / "develop").exists()


def test_develop_entry_does_not_run_main(branch_repo):
    _, stable, _ = branch_repo
    result = call_bat(stable / "start-dev.bat", "-ResolveOnly")
    assert result.returncode != 0 and "on develop" in result.stdout
    assert git(stable, "branch", "--show-current") == "main"


def test_stable_forwards_arguments_to_selected_launcher_without_creating_database(branch_repo):
    root, stable, _ = branch_repo
    result = call_bat(
        root / "start.bat", "-PythonPath", ".venv/Scripts/python.exe",
        "-BackendPort", "8801", "-FrontendPort", "5801",
        "-DatabasePath", ".local/custom db/workflow.sqlite",
        "-NoBrowser", "-CheckOnly", "-Mode", "must-reach-target",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout.splitlines()[-1])
    assert Path(value["root"]).resolve() == stable.resolve()
    assert Path(value["python"]).resolve() == stable / ".venv" / "Scripts" / "python.exe"
    assert Path(value["database"]).resolve() == root / ".local" / "custom db" / "workflow.sqlite"
    assert value["backend_port"] == 8801 and value["frontend_port"] == 5801
    assert value["no_browser"] and value["check_only"]
    assert value["mode"] == "must-reach-target"
    assert not (root / ".local" / "custom db").exists()


def test_target_failure_exit_code_is_preserved_by_bat(branch_repo):
    root, _, _ = branch_repo
    result = call_bat(root / "start.bat", "-Mode", "fail-after-entry")
    assert result.returncode == 7, result.stdout + result.stderr
    assert not (root / ".local" / "dev").exists()


def test_wrapper_has_no_hidden_launch_or_automatic_git_mutation():
    source = (WORKSPACE / "scripts" / "start-branch-services.ps1").read_text(encoding="ascii")
    assert "Start-Process" not in source
    assert "WindowStyle" not in source
    assert "Read-Git $entryRoot @('checkout'" not in source
    assert "Read-Git $entryRoot @('switch'" not in source
    assert "Read-Git $entryRoot @('reset'" not in source
