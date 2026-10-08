"""Current-checkout BAT acceptance on owned ports and disposable databases."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryFile
from time import monotonic, sleep
from urllib.request import urlopen
from uuid import uuid4

import pytest


WORKSPACE = Path(__file__).resolve().parents[2]
START = WORKSPACE / "scripts" / "start-services.ps1"
STOP = WORKSPACE / "scripts" / "stop-services.ps1"
EXPECTED_TAVERN_NODES = {
    "lorebook.global-reference",
    "lorebook.global-activate",
    "tavern.chat.output",
    "tavern.chat.append",
    "tavern.chat.presentation",
}


def powershell():
    executable = shutil.which("powershell.exe")
    if os.name != "nt" or executable is None:
        pytest.skip("The Windows BAT launcher requires Windows PowerShell")
    return executable


def run_captured(arguments, environment=None, timeout=90):
    # Detached Windows children can inherit pipe handles after PowerShell exits.
    with TemporaryFile() as output, TemporaryFile() as errors:
        result = subprocess.run(
            arguments, cwd=WORKSPACE, stdout=output, stderr=errors,
            timeout=timeout, check=False, env=environment,
        )
        output.seek(0)
        errors.seek(0)
        return subprocess.CompletedProcess(
            result.args, result.returncode, output.read().decode("utf-8", "replace"),
            errors.read().decode("utf-8", "replace"),
        )


def call_script(script, *arguments, environment=None, timeout=90):
    return run_captured(
        [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         *map(str, arguments)], environment=environment, timeout=timeout)


def call_bat(script, *arguments):
    powershell()
    command = subprocess.list2cmdline([str(script), *map(str, arguments)])
    interpreter = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")])
    return run_captured(
        interpreter + ' /d /s /c "' + command + '"',
        environment={**os.environ, "NO_PAUSE": "1", "NO_BROWSER": "1"})


def machine_result(result):
    assert result.returncode == 0, result.stdout + result.stderr
    for line in reversed(result.stdout.splitlines()):
        if line.strip().startswith("{"):
            value = json.loads(line)
            if value.get("schema") == "openagent.dev-launcher@1":
                return value
    pytest.fail("Launcher did not print its machine-readable identity: " + result.stdout)


@contextmanager
def listening_socket():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert listener.getsockname()[1] not in (8765, 5178)
        yield listener


def free_ports():
    with listening_socket() as first, listening_socket() as second:
        return first.getsockname()[1], second.getsockname()[1]


def parameters(database, backend_port, frontend_port):
    return [
        "-PythonPath", sys.executable, "-BackendPort", backend_port,
        "-FrontendPort", frontend_port, "-DatabasePath", database, "-NoBrowser",
    ]


def fetch(base, path):
    with urlopen(base + path, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def catalog(base):
    status, content = fetch(base, "/api/graph/node-types/v2")
    assert status == 200
    return json.loads(content)


def port_open(port):
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def assert_source_identity(result, workspace=WORKSPACE):
    assert Path(result["repository_root"]).resolve() == workspace
    assert Path(result["backend_source"]).resolve() == workspace / "backend" / "src"
    assert Path(result["server_module"]).resolve() == workspace / "backend" / "src" / "phase1_agent" / "server.py"
    assert Path(result["frontend_root"]).resolve() == workspace / "frontend"
    assert {"package_id": "workflow.tavern", "version": "1.2.0"} in result["package_lock"]
    assert EXPECTED_TAVERN_NODES <= {row["component_id"] for row in result["tavern_nodes"]}


def test_launcher_keeps_services_in_visible_consoles():
    source = START.read_text(encoding="utf-8")
    assert source.count("-WindowStyle Normal -PassThru") == 2
    assert "-WindowStyle Hidden" not in source
    assert "-RedirectStandardOutput" not in source
    assert "-RedirectStandardError" not in source


def test_check_only_prefers_current_source_without_creating_database(tmp_path):
    backend_port, frontend_port = free_ports()
    database = tmp_path / "must-not-exist" / "workflow.sqlite"
    environment = {**os.environ, "PYTHONPATH": str(WORKSPACE.parent / "step1" / "smolagents" / "phase1_agent" / "src")}
    result = machine_result(call_script(
        START, *parameters(database, backend_port, frontend_port), "-CheckOnly",
        environment=environment,
    ))
    assert result["check_only"] is True
    assert_source_identity(result)
    assert not database.parent.exists()
    assert not port_open(backend_port) and not port_open(frontend_port)


@pytest.mark.parametrize("entry", ["stable", "develop", "legacy"])
def test_bat_entry_forwards_selected_checkout_and_arguments(tmp_path, entry):
    backend_port, frontend_port = free_ports()
    database = tmp_path / "not-created" / "workflow.sqlite"
    script = (WORKSPACE.parent / "step1" / "docs" / "\u542f\u52a8\u670d\u52a1.bat" if entry == "legacy"
              else WORKSPACE / ("start-dev.bat" if entry == "develop" else "start.bat"))
    if entry == "legacy" and not script.is_file():
        pytest.skip("The legacy sibling BAT is only available in the full local workspace")
    assert script.is_file(), "The selected BAT must be present"
    target = json.loads(call_bat(script, "-ResolveOnly").stdout.strip())
    checked = machine_result(call_bat(
        script, *parameters(database, backend_port, frontend_port), "-CheckOnly"))
    assert_source_identity(checked, Path(target["repository_root"]).resolve())
    assert target["branch"] == ("develop" if entry == "develop" else "main")
    assert checked["check_only"] is True
    assert checked["backend_port"] == backend_port and checked["frontend_port"] == frontend_port
    assert Path(checked["database_path"]).resolve() == database
    assert not database.parent.exists()


@pytest.mark.parametrize("busy", ["backend", "frontend"])
def test_busy_port_is_rejected_without_touching_owner_or_database(tmp_path, busy):
    backend_port, frontend_port = free_ports()
    database = tmp_path / "untouched" / "workflow.sqlite"
    with listening_socket() as owner:
        occupied = owner.getsockname()[1]
        if busy == "backend":
            backend_port = occupied
        else:
            frontend_port = occupied
        result = call_script(START, *parameters(database, backend_port, frontend_port), "-CheckOnly")
        assert result.returncode != 0, result.stdout
        assert str(occupied) in result.stdout + result.stderr
        assert port_open(occupied), "A pre-existing listener must never be stopped"
        assert not database.parent.exists()


def test_missing_interpreter_is_reported_before_database_or_startup(tmp_path):
    backend_port, frontend_port = free_ports()
    database = tmp_path / "untouched" / "workflow.sqlite"
    arguments = parameters(database, backend_port, frontend_port)
    arguments[1] = tmp_path / "missing-python.exe"
    result = call_script(START, *arguments, "-CheckOnly")
    assert result.returncode != 0
    assert "missing-python.exe" in result.stdout + result.stderr
    assert not database.parent.exists()
    assert not port_open(backend_port) and not port_open(frontend_port)


def test_retired_mode_is_rejected_instead_of_claiming_offline_model_calls(tmp_path):
    backend_port, frontend_port = free_ports()
    database = tmp_path / "untouched" / "workflow.sqlite"
    result = call_script(
        START, *parameters(database, backend_port, frontend_port), "-CheckOnly", "-Mode", "offline")
    assert result.returncode != 0
    assert "retired" in result.stdout + result.stderr
    assert not database.parent.exists()


def test_backend_start_failure_cleans_owned_processes_and_preserves_database(tmp_path):
    backend_port, frontend_port = free_ports()
    database = tmp_path / "invalid.sqlite"
    original = b"This is not a SQLite database."
    database.write_bytes(original)
    checked = machine_result(call_script(
        START, *parameters(database, backend_port, frontend_port), "-CheckOnly"))
    assert checked["check_only"] is True
    assert database.read_bytes() == original
    failed = call_script(START, *parameters(database, backend_port, frontend_port))
    assert failed.returncode != 0
    assert database.read_bytes() == original
    assert not port_open(backend_port) and not port_open(frontend_port)


@pytest.fixture(scope="module")
def dev_services(tmp_path_factory):
    evidence = tmp_path_factory.mktemp("dev-launcher")
    database = evidence / "disposable.sqlite"
    backend_port, frontend_port = free_ports()
    metadata = None
    try:
        started = machine_result(call_script(START, *parameters(database, backend_port, frontend_port)))
        metadata = Path(started["metadata_path"])
        assert metadata.is_file()
        assert_source_identity(started)
        assert started["check_only"] is False
        assert database.is_file()
        (evidence / "launcher-result.json").write_text(json.dumps(started, indent=2), encoding="utf-8")
        yield {**started, "evidence": evidence, "database": database, "metadata": metadata}
    finally:
        if metadata is None:
            for candidate in (WORKSPACE / ".local" / "service-starts").glob("*/services.json"):
                saved = json.loads(candidate.read_text(encoding="utf-8-sig"))
                if Path(saved["database_path"]).resolve() == database:
                    metadata = candidate
                    break
        if metadata is not None:
            stopped = call_script(STOP, "-MetadataPath", metadata)
            assert stopped.returncode == 0, stopped.stdout + stopped.stderr
            (evidence / "stop.stdout.txt").write_text(stopped.stdout, encoding="utf-8")
        deadline = monotonic() + 5
        while monotonic() < deadline and (port_open(backend_port) or port_open(frontend_port)):
            sleep(0.1)
        assert not port_open(backend_port) and not port_open(frontend_port)
        if metadata is not None:
            assert database.is_file(), "Stopping owns processes, not database content"


def test_real_launcher_serves_current_tavern_and_vite_proxy(dev_services):
    backend = dev_services["backend_url"].rstrip("/")
    frontend = dev_services["frontend_url"].rstrip("/")
    direct, proxied = catalog(backend), catalog(frontend)
    assert direct == proxied
    assert EXPECTED_TAVERN_NODES <= {row["component_id"] for row in direct["node_types"]}
    assert {"package_id": "workflow.tavern", "version": "1.2.0"} in direct["package_lock"]
    status, page = fetch(frontend, "/")
    assert status == 200 and "/src/main.ts" in page
    status, current_graph = fetch(frontend, "/src/domain/workflowGraph.ts")
    assert status == 200 and "schema_version: 2" in current_graph
    status, current_store = fetch(frontend, "/src/stores/workflowGraph.ts")
    assert status == 200 and "registerWorkflowDraft" in current_store
    assert fetch(backend, "/tavern/")[0] == 200


def test_invalid_stop_metadata_cannot_stop_owned_services(dev_services, tmp_path):
    invalid = tmp_path / "invalid-services.json"
    invalid.write_text(json.dumps({"schema": "not-a-launcher", "backend_pid": os.getpid()}), encoding="utf-8")
    result = call_script(STOP, "-MetadataPath", invalid)
    assert result.returncode != 0
    assert fetch(dev_services["backend_url"].rstrip("/"), "/api/health")[0] == 200
    assert fetch(dev_services["frontend_url"].rstrip("/"), "/")[0] == 200


def test_reused_pid_identity_is_rejected_before_stopping_any_owned_service(dev_services):
    directory = WORKSPACE / ".local" / "service-starts" / ("qa-stale-identity-" + uuid4().hex)
    directory.mkdir()
    copied = directory / "services.json"
    metadata = json.loads(dev_services["metadata"].read_text(encoding="utf-8-sig"))
    metadata["processes"][0]["start_time_ticks"] = str(int(metadata["processes"][0]["start_time_ticks"]) + 1)
    copied.write_text(json.dumps(metadata), encoding="utf-8")
    result = call_script(STOP, "-MetadataPath", copied)
    assert result.returncode != 0
    assert "no longer matches" in result.stdout + result.stderr
    assert fetch(dev_services["backend_url"].rstrip("/"), "/api/health")[0] == 200
    assert fetch(dev_services["frontend_url"].rstrip("/"), "/")[0] == 200


@pytest.mark.parametrize("width", [1440, 1024], ids=["desktop", "narrow-desktop"])
def test_current_workbench_empty_populated_and_tavern_share_canvas(dev_services, width):
    from test_tavern_workbench_browser import browser_runtime

    node, packages = browser_runtime()
    evidence = dev_services["evidence"] / f"workflow-ui-{width}"
    evidence.mkdir()
    result = subprocess.run(
        [node, str(Path(__file__).with_name("dev_launcher_workflow_browser.cjs")),
         dev_services["frontend_url"].rstrip("/"), dev_services["backend_url"].rstrip("/"),
         str(width), str(evidence)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "NODE_PATH": str(packages)}, timeout=100, check=False,
    )
    (evidence / "runner.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (evidence / "runner.stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    assert observed["page_errors"] == [] and observed["asset_errors"] == []
    assert observed["console_errors"] == []
    assert observed["mutation_requests"] == []


@pytest.mark.parametrize("width", [1440, 1024], ids=["desktop", "narrow-desktop"])
def test_current_workbench_recovers_unreadable_local_record(dev_services, width):
    from test_tavern_workbench_browser import browser_runtime

    node, packages = browser_runtime()
    evidence = dev_services["evidence"] / f"storage-recovery-{width}"
    evidence.mkdir()
    result = subprocess.run(
        [node, str(Path(__file__).with_name("workbench_recovery_browser.cjs")),
         dev_services["frontend_url"].rstrip("/"), dev_services["backend_url"].rstrip("/"),
         str(width), str(evidence)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "NODE_PATH": str(packages)}, timeout=100, check=False,
    )
    (evidence / "runner.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (evidence / "runner.stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
    assert len(observed["scenarios"]) == 3
    for field in ("page_errors", "asset_errors", "console_errors", "mutation_requests"):
        assert observed[field] == []
