# OpenAgent Backend

This is the Python backend for the OpenAgent demo. The Python package remains
`phase1_agent`; the directory name is a release-layout change, not a runtime
package rename.

The current demo uses editable graph definitions, workflow sessions and a
serial Agent workflow. It includes model configuration, prompt/context
preparation, SQLite persistence, candidate history and execution diagnostics.
The older fixed `A -> B -> Output` workflow and bundled static page remain
compatibility paths; they are not the main workbench experience.

## Start Here

- [Quickstart](../docs/QUICKSTART.md): installation, local startup and demo.
- [Development](../docs/DEVELOPMENT.md): module boundaries and data flow.
- [Debugging](../docs/DEBUGGING.md): evidence, failure classification and recovery.
- [Testing](../docs/TESTING.md): offline checks and bounded demo verification.
- [Frontend](../frontend/README.md): the separate workbench client.
- [Third-party notices](../THIRD_PARTY_NOTICES.md): source and license details.

From this directory, with Python 3.10 or later:

```powershell
python -m pip install -e .
New-Item -ItemType Directory -Force .local | Out-Null
python -m phase1_agent.server --port 8765 --database .local/workflow.sqlite --mode offline
```

The server binds to loopback only. The main workbench runs separately from
`../frontend`; its development proxy connects to this backend.
The `offline` host mode supplies the legacy deterministic adapter. It does not
prohibit graph workflows from invoking configured online providers. Do not
submit a model-backed graph without checking its provider and credential
configuration.

## Development Dependencies

Core installation does not require smolagents. Native tools use
`register_callable`; the optional `register_tool` adapter supports
`smolagents.Tool` for legacy integrations and tests.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_serial_agent_demo_integration.py tests/test_graph_failed_retry.py -q
```

Run these commands from `backend` so the relative editable paths resolve.
The pinned `smolagents==1.27.0.dev0` compatibility dependency is supplied by
`../vendor/smolagents`, not fetched as a development version from an index.
Some cross-package tests require Node.js and installed frontend TypeScript
dependencies; see the testing document before interpreting skipped tests.

## Code Entry Points

| Entry | Responsibility |
| --- | --- |
| `src/phase1_agent/server.py` | Local HTTP server and request routing |
| `workflow_host.py`, `graph_platform.py` | Host composition and graph platform |
| `graph_service.py`, `graph_application.py` | Graph/session operations |
| `graph_agent_runtime.py`, `agent_executor.py` | Agent execution integration |
| `graph_store.py`, `storage.py`, `runtime_fact_store.py` | Persistent records and execution facts |
| `model_service.py`, `model_configuration.py` | Model/provider configuration |
| `context_v3.py`, `prompt_assembly.py` | Context and prompt preparation |
| `schemas/`, `scripts/export_*_schemas.py` | Checked-in schemas and exports |

The demo is not a production deployment, a general parallel scheduler or
cross-process checkpoint recovery system. Known-failure retry, result
acceptance retry and uncertain external effects are distinct operations.
In-process continuation cannot reconstruct an executable checkpoint after a
restart, and tool effects are not rolled back by changing a selected candidate.
