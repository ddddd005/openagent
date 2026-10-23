# Workspace Demo Local Package

Place this entire directory under the workflow package root:
`<main SQLite directory>/packages/workflow/workspace-demo-1.0.0/`.
An explicit `--workflow-package-root` overrides that parent directory.
The package is discovered but is not enabled automatically.

Use the existing package selection command to add
`"example.workspace-demo": "1.0.0"` while retaining the project's other exact
selections. No additional Python dependencies are required.

In a workflow with an Agent and native context assembly, add the
`example.workspace-demo-tools@1` node, set its `agent_node_id` to that Agent,
set `state_path` to an absolute file path owned by this demo, and connect
`materials` to the context assembly's `materials` input. The Agent receives
`look()`, `status()` and `move(destination)` plus their paired descriptions.

This is the controlled sample for the workspace tool adapter, not a product
data layer. Its state lives in the demo `state_path` file only:

- `open` starts one long-running `worker.py` child process, loading the saved
  state file when it exists (initial location is `city_gate`).
- Every model tool call is sent to that same process as one JSON line, so
  consecutive calls share the loaded workspace state.
- After a valid model final, `finish` asks the worker to persist its state
  atomically and returns the exported text (`current location` and
  `movement history`); the program composes the model final text plus a blank
  line plus that export into the node `result`.
- `close` sends `quit` and requires the child process to exit with code 0;
  a timeout or non-zero exit is reported explicitly, and the already saved
  state is not rolled back.
- `abort` only terminates the process. Model errors and user termination use
  this path: nothing is saved and nothing is exported.

A second Agent invocation starts a new process and reloads the state that the
previous normal finish saved. Each Agent invocation has at most one workspace
instance; `look/status/move` share it. The demo exposes no generic data write
port and the `state_path` file is not protected by workflow checkpoints.

This is trusted local Python code. The adapter interface is a contract only;
the host does not sandbox Python imports or arbitrary filesystem/network calls.
