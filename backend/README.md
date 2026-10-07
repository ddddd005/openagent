# OpenAgent Backend

This is the Python backend for the OpenAgent demo. The Python package remains
`phase1_agent`; the directory name is a release-layout change, not a runtime
package rename.

The current demo uses editable graph definitions, workflow sessions and a
serial Agent workflow. It includes model configuration, prompt/context
preparation, SQLite persistence, candidate history and execution diagnostics.
The fixed `A -> B -> Output` host, compatibility nodes and old static client
have been removed. The current graph workbench and GraphChat are the supported
entry points. Removal and targeted checks are complete; independent wheel and
limited mock-browser checks have passed. Resident deployment and the remaining
usage checks are pending. This checkpoint is not a 1.0 release.

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
python -m phase1_agent.server --port 8765 --database .local/workflow.sqlite
```

The server binds to loopback only. The main workbench runs separately from
`../frontend`; its development proxy connects to this backend.
The retired `--mode` argument is no longer accepted. Do not
submit a model-backed graph without checking its provider and credential
configuration.

## Development Dependencies

Installation and current tests do not require smolagents. Native tools use
`register_callable`; the legacy `smolagents.Tool` adapter has been removed.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_serial_agent_demo_integration.py tests/test_graph_failed_retry.py -q
```

Run these commands from `backend` so the relative editable paths resolve.
The vendored source remains only for provenance and license preservation.
Some cross-package tests require Node.js and installed frontend TypeScript
dependencies; see the testing document before interpreting skipped tests.

## Code Entry Points

| Entry | Responsibility |
| --- | --- |
| `src/phase1_agent/server.py` | Local HTTP server and request routing |
| `workflow_host.py`, `graph_platform.py` | Host composition and graph platform |
| `graph_service.py`, `graph_application.py` | Graph/session operations |
| `agent_executor.py`, `runtime.py`, `runtime_hosting.py` | Public Agent execution integration |
| `capability_registry.py`, `builtin_packages.py` | Current capability registration |
| `graph_store.py`, `storage.py`, `runtime_fact_store.py` | Persistent records and execution facts |
| `model_service.py`, `model_package.py` | Model/provider configuration |
| `context_v3.py`, `prompt_assembly.py` | Context and prompt preparation |
| `schemas/`, `scripts/export_*_schemas.py` | Checked-in schemas and exports |

The demo is not a production deployment, a general parallel scheduler or
cross-process checkpoint recovery system. Known-failure retry, result
acceptance retry and uncertain external effects are distinct operations.
In-process continuation cannot reconstruct an executable checkpoint after a
restart, and tool effects are not rolled back by changing a selected candidate.
