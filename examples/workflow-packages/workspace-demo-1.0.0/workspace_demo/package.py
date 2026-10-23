"""Controlled long-running workspace tools supplied through a local package.

The package owns a real child process (``worker.py``) with a JSON-lines
protocol.  The host keeps only the opaque handle returned by ``open`` and calls
``finish`` once after a valid model final, then ``close`` once; ``abort`` is the
release-only path used by the executor when a run does not finish normally.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading

from phase1_agent.capability_packages import CapabilityPackage, PackageDependency, PackageManifest
from phase1_agent.content_contracts import (
    default_presentation, object_schema, presentation_schema, validate_presentation,
)
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.prompt_lifecycle import (
    PROMPT_MATERIALS_TYPE, lifecycle_prompt_content, make_lifecycle_prompt_item,
    merge_lifecycle_prompt_materials,
)
from phase1_agent.tool_binding_contract import WorkspaceToolBindingAdapter


PACKAGE_ID = "example.workspace-demo"
VERSION = "1.0.0"
LOADER = "example.workspace-demo-tools"
WORKER = Path(__file__).resolve().with_name("worker.py")
REQUEST_TIMEOUT = 15.0
CLOSE_TIMEOUT = 15.0
TERMINATE_TIMEOUT = 5.0


def normalize(config):
    """Declare the three controlled workspace tools for the bound Agent."""
    return [
        {
            "name": "look",
            "description": "Inspect the workspace's current location and the known destinations.",
            "parameters": object_schema({}),
            "config": {},
        },
        {
            "name": "status",
            "description": "Report the workspace's current location and recorded movement history.",
            "parameters": object_schema({}),
            "config": {},
        },
        {
            "name": "move",
            "description": "Move the workspace to an exact destination and report the new location.",
            "parameters": object_schema({
                "destination": {"type": "string", "description": "Exact destination identifier."},
            }),
            "config": {},
        },
    ]


def _shutdown_streams(handle) -> None:
    process = handle["process"]
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def _request(handle, message: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
    """Write one JSON-line command and read exactly one JSON-object answer."""
    process = handle["process"]
    if process.poll() is not None:
        raise RuntimeError("workspace worker is not running")
    try:
        process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError) as error:
        raise RuntimeError("workspace worker communication failed") from error
    try:
        line = handle["lines"].get(timeout=timeout)
    except queue.Empty:
        raise RuntimeError("workspace worker did not answer within the request timeout")
    if line is None:
        raise RuntimeError("workspace worker closed its output")
    try:
        reply = json.loads(line)
    except json.JSONDecodeError as error:
        raise RuntimeError("workspace worker returned a non-JSON line: " + line.strip()[:300]) from error
    if type(reply) is not dict:
        raise RuntimeError("workspace worker returned a non-object reply")
    return reply


def _terminate(process) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=TERMINATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("workspace worker could not be terminated") from error


def open(binding, invocation):
    """Start the controlled child process and return an opaque handle."""
    config = binding["config"]
    state_path = config.get("state_path")
    if not (type(state_path) is str and state_path):
        raise RuntimeError("workspace demo requires one state file path")
    process = subprocess.Popen(
        [sys.executable, str(WORKER), state_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=str(WORKER.parent), text=True, encoding="utf-8", bufsize=1,
    )
    lines: queue.Queue = queue.Queue()

    def read_lines():
        try:
            for line in process.stdout:
                lines.put(line)
        except (ValueError, OSError):
            pass
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_lines, name="workspace-demo-reader", daemon=True)
    reader.start()
    handle = {"process": process, "lines": lines, "state_path": state_path, "reader": reader}
    try:
        reply = _request(handle, {"op": "status"})
        if reply.get("ok") is not True:
            raise RuntimeError("workspace worker did not report its initial state")
    except BaseException:
        _terminate(process)
        _shutdown_streams(handle)
        raise
    return handle


def invoke(handle, tool, arguments, invocation):
    """Send one model tool call to the same running workspace instance."""
    return deepcopy(_request(handle, {"op": tool["name"], "arguments": deepcopy(arguments)}))


def finish(handle, invocation):
    """Let the worker persist its state atomically and export the composed text."""
    reply = _request(handle, {"op": "finish"})
    text = reply.get("text")
    if reply.get("ok") is not True or type(text) is not str:
        raise RuntimeError("workspace worker did not export text")
    return {"text": text}


def close(handle):
    """Ask the worker to quit and require its clean exit before releasing it."""
    process = handle["process"]
    if process.poll() is None:
        reply = _request(handle, {"op": "quit"})
        if reply.get("ok") is not True:
            raise RuntimeError("workspace worker refused the quit command")
    try:
        returncode = process.wait(timeout=CLOSE_TIMEOUT)
    except subprocess.TimeoutExpired:
        _terminate(process)
        _shutdown_streams(handle)
        raise RuntimeError("workspace worker did not confirm exit within the close timeout")
    _shutdown_streams(handle)
    if returncode != 0:
        raise RuntimeError("workspace worker exited with a nonzero return code: " + str(returncode))


def abort(handle):
    """Release the worker without saving or exporting anything."""
    try:
        _terminate(handle["process"])
    finally:
        _shutdown_streams(handle)


def prompt(config, inputs, context):
    """Emit canonical paired tool-description and tool-schema materials."""
    specs = context.bound_tool_prompt(config["agent_node_id"])
    items = []
    for spec in specs:
        for part, purpose, presentation, text in (
            ("description", "tool-description", config["list_presentation"], spec["description"]),
            ("parameters", "tool-schema", config["parameters_presentation"],
             canonical_bytes(spec["parameters"]).decode("utf-8")),
        ):
            # One binding serves several tools, so the stable material identity
            # must include the tool name in addition to the exact part.
            items.append(make_lifecycle_prompt_item(
                context.node_binding_id,
                spec["binding_id"] + ":" + spec["name"] + ":" + part, text, presentation,
                purpose=purpose,
                source={"kind": "bound_tool", "agent_node_id": config["agent_node_id"],
                        "tool_name": spec["name"], "binding_id": spec["binding_id"], "part": part},
                metadata={"agent_node_id": config["agent_node_id"], "tool_name": spec["name"],
                          "binding_id": spec["binding_id"], "part": part},
            ))
    return {"materials": merge_lifecycle_prompt_materials([lifecycle_prompt_content(items)])}


def validate_loader(config):
    validate_presentation(config["list_presentation"])
    validate_presentation(config["parameters_presentation"])


def register(host):
    host.register_node(NodeDefinition(
        LOADER, "1", "Workspace demo tools", "Examples",
        {"agent_node_id": "", "state_path": "", "list_presentation": default_presentation(),
         "parameters_presentation": {**default_presentation(), "placement": "after"}},
        object_schema({
            "agent_node_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "state_path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "list_presentation": presentation_schema(),
            "parameters_presentation": presentation_schema(),
        }),
        outputs=(NodePort("materials", PROMPT_MATERIALS_TYPE),),
        input_storage="references",
        config_node_references=({
            "config_field": "agent_node_id", "multiple": False,
            "target_types": [{"component_id": "agents.execute", "component_version": "10"}],
        },),
    ), prompt, config_validator=validate_loader, tool_binding=WorkspaceToolBindingAdapter(
        normalize=normalize, open=open, invoke=invoke, finish=finish, close=close, abort=abort,
        prompt_output=("materials", PROMPT_MATERIALS_TYPE, 1)))


def create_package():
    return CapabilityPackage(PackageManifest(
        PACKAGE_ID, VERSION,
        dependencies=(PackageDependency("workflow.content", "1.0.0"),
                      PackageDependency("workflow.agents", "1.0.0")),
        exports={"nodes": [
            {"component_id": LOADER, "component_version": "1"},
        ]},
    ), register)
