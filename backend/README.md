# OpenAgent Backend

Python workflow runtime for the OpenAgent graph workbench, GraphChat and Tavern
client. The import package remains `phase1_agent`; it is not the retired fixed
`A -> B -> Output` host.

The current mainline supports graph definitions and workflow sessions, native
Agent context, DeepSeek/Gemini model protocols, tool execution, separate thinking
summaries, SQLite persistence and execution diagnostics. The default registry
has 44 executable declarations across 41 node families. Historical node
implementations and their test graphs are retired rather than silently upgraded.
Application metadata remains `0.2.0`; the current mainline is not a new release
tag or a claim that the entire backend suite is green.

## Start Here

- [Documentation index](../docs/README.md): current guides and historical records.
- [Quickstart](../docs/QUICKSTART.md): dependencies and explicit BAT startup.
- [User guide](../docs/USER-GUIDE.md): workflow, model and client operations.
- [Development](../docs/DEVELOPMENT.md): module boundaries and data contracts.
- [Debugging](../docs/DEBUGGING.md): evidence and permitted recovery actions.
- [Testing](../docs/TESTING.md): current offline commands and known test debt.
- [Frontend](../frontend/README.md): the separate workbench client.
- [Third-party notices](../THIRD_PARTY_NOTICES.md): provenance and licenses.

On Windows, use the BAT files in the repository root:

| Entry | Code | Backend / Workbench | Default database, relative to the BAT directory |
| --- | --- | --- | --- |
| `start.bat` | The unique clean `main` worktree | `8765` / `5178` | `.local/dev/workflow.sqlite` |
| `start-dev.bat` | This checkout on `develop` | `8766` / `5179` | `.local/develop/workflow.sqlite` |

Both entries open visible backend and frontend consoles. Neither automatically
pulls Git, changes branches, installs dependencies or stops an existing port
owner. The startup record identifies the selected source, database and owned
processes. See Quickstart before setting up a fresh clone or a `main` worktree.
Git push updates the repository, not a running service or a deployment.

For an explicitly isolated backend-only session, run from this directory with
Python 3.10 or later:

```powershell
python -m pip install -e .
New-Item -ItemType Directory -Force .local | Out-Null
python -m phase1_agent.server --port 8877 --database .local/backend-debug.sqlite
```

The server binds to loopback only. This command does not start the Vue
workbench; the backend also serves GraphChat and `/tavern/`. A previously built
workbench can be served with `--workbench-dist ../frontend/dist`; it is loaded as
an asset snapshot at startup. The retired `--mode` argument is not accepted.
Model calls are controlled by workflow configuration, not an implicit offline
launcher mode.

## Development Dependencies

From `backend/`, after setting up the repository-root virtual environment:

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
New-Item -ItemType Directory -Force .local | Out-Null
..\.venv\Scripts\python.exe -m pytest tests/test_current_node_directory.py tests/test_serial_agent_demo_integration.py tests/test_result_acceptance_retry.py --basetemp .local/pytest-current-backend -q
```

These tests use controlled model transports and temporary databases, not real
model credentials. Use a new basetemp directory for each run; pytest manages
and may clear that directory. Current tests do not require smolagents. Native
tools use `register_callable`; vendored source is retained for provenance and
licenses, not as a runtime dependency. Some cross-package tests additionally
require Node.js and installed frontend dependencies.

## Code Entry Points

All files below are under `src/phase1_agent/`.

| Entry | Responsibility |
| --- | --- |
| `server.py`, `graph_http.py` | Loopback HTTP and application routing |
| `graph_application.py`, `graph_application_contracts.py` | Command/query boundary and identity checks |
| `workflow_host.py`, `graph_platform.py`, `graph_service.py` | Host composition, graph/session operations and object adoption |
| `graph_execution.py`, `graph_runtime_host.py` | Compilation and hosted execution |
| `agent_package.py`, `agent_executor.py`, `runtime.py`, `runtime_hosting.py` | Native Agent execution, tool boundaries and same-process continuation |
| `capability_registry.py`, `capability_packages.py`, `builtin_packages.py` | Exact package selection and current declarations |
| `storage.py`, `graph_store.py`, `runtime_fact_store.py`, `storage_retirement.py` | Records, facts and precise storage-v15 retirement |
| `model_service.py`, `model_host_service.py`, `model_package.py` | Frozen provider resources, capacity-aware bindings and model calls |
| `adapter.py`, `gemini_adapter.py`, `gemini_capabilities.py`, `provider_metadata.py` | Protocol conversion, thinking validation and original signed parts |
| `context_v4.py`, `context_v4_nodes.py`, `context_receipt.py` | Native context, verified writeback and CAS receipts |
| `prompt_package.py`, `prompt_lifecycle.py`, `prompt_assembly.py`, `context_prompt_v6.py` | Lifecycle materials, ordinary `PROMPT@2` and native Agent `PROMPT@6` |
| `tavern/`, `static/graph-chat.js`, `static/graph-chat-core.js` | Tavern and GraphChat consumers |

Schemas are checked in under `backend/schemas/`; exporters are under
`backend/scripts/`.

Thinking summaries are display data, not private protocol state. Gemini
signatures remain opaque and preserve original part order and tool bindings;
they must not be forged, removed or exposed as answer text.

The runtime is not a general parallel tool scheduler or an active cross-process
checkpoint recovery system. Current native Agents do not register a failed-node
retry policy. A retained successful result may permit `retry_acceptance`
without repeating model or tool execution. Reloading persistent history does
not recreate an active continuation, and selecting a different candidate does
not undo external tool effects.
