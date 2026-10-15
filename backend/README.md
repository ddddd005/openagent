# OpenAgent Backend

The 2026-10-15 construction checkout has retired unused history-closure restore
authority and cross-workflow session copying. Current checkpoints initialize a
new session through the dedicated checkpoint loader. Frozen receipt verification,
live object ACL/CAS and runtime evidence remain in place. See the
[cleanup receipt](../docs/中期检查2/38-阶段D后旧架构代码整理实施回执-2026-10-15.md).
The mainline/develop history below is not a deployment claim for this checkout.

The scoped D/E retirement is now complete. The current menu creates
`frontend.node@2`; old reference-display business, duplicate commit-candidate
queries and session-data mutations are retired. Final scoped validation passed
778 backend tests; existing services were not updated. See the
[retirement and closeout receipt](../docs/中期检查2/57-D-E阶段旧内容退役与收口实施回执-2026-10-15.md)
for exact evidence and remaining manual/real-model acceptance.

Stage E4 installed node-owned HTTP entry/channel/events addresses and a
display-only frontend page for the then-current `frontend.node@1` instance.
Opening the page does not create a session or start execution. At E4 delivery,
existing product services and legacy GraphChat/Tavern entries were unchanged. See the
[E4 scoped delivery receipt](../docs/中期检查2/47-阶段E节点HTTP入口与页面限定验收实施回执-2026-10-15.md).

Stage E5 retires legacy GraphChat/Tavern page entry, active consumer mutations
and preset workflow creation. Only ordinary workflows are created. Lorebook,
current explicit display nodes, exact frontend-node routes and original request
evidence remain. Four old page addresses return 410; retained old source is
served as non-executable plain text. Two exact legacy test workflows were deleted
from the named construction databases, preserving ordinary workflows and global
resources. Product services were not started or restarted. See the
[E5 scoped delivery receipt](../docs/中期检查2/49-阶段E旧入口退役与测试工作流清理实施回执-2026-10-15.md).

Workflow operations are now independent of frontend nodes:
`WorkflowOperations` owns create/start coordination, pause/resume/close and
checkpoint metadata reads/fork calls over the existing service. Exact-node
adapters verify ownership before calling it; the node page now exposes resume,
end-round and checkpoint controls. Original start/pause request identities remain
readable without redispatch. See the
[operation delivery receipt](../docs/中期检查2/53-工作流独立操作与节点入口补齐实施回执-2026-10-15.md).

Logical chat is now stored in registered `workflow.chat-context@1` items and
admitted to checkpoint schema4. Independent chat operations use the fixed
pre-round checkpoint for reroll, select successful candidates, lock earlier
floors and fork whole floors with target-owned checkpoint references.
`frontend.node@2` consumes these functions through the exact-node channel.
The current retirement batch removes version 1 from executable registration;
its saved original requests retain receipt-only verification, not live business
access or an automatic upgrade. Existing schema3 product databases
were not migrated. Conflicting computation bases across participating chat
items are rejected pending a separate user decision. See the
[scoped chat delivery receipt](../docs/中期检查2/55-聊天候选数据层与重roll检查点接续实施回执-2026-10-15.md).

## Historical Delivery

The dated mainline/develop paragraphs below describe their original batches,
not the current construction registry or an instruction to reopen retired APIs.

Python workflow runtime for the OpenAgent graph workbench and node-owned frontend.
The import package remains `phase1_agent`; it is not the retired fixed
`A -> B -> Output` host.

The current mainline supports graph definitions and workflow sessions, native
Agent context, DeepSeek/Gemini model protocols, tool execution, separate thinking
summaries, SQLite persistence and execution diagnostics. The default registry
has 44 executable declarations across 41 node families. Historical node
implementations and their test graphs are retired rather than silently upgraded.
Application metadata remains `0.2.0`; the current mainline is not a new release
tag or a claim that the entire backend suite is green.

Frontend-userization P1-P6 is locally committed and fast-forwarded into `main`,
with feature commit `0c15587`; this batch has not been pushed. P4 adds
management-only private credentials and bounded model discovery; P5 adds
read-only draft input declarations.
See the [P3/P4 record](../docs/FRONTEND-P3-P4-2026-10-09.md) and
[P5/P6 record](../docs/FRONTEND-P5-P6-2026-10-09.md).
The BAT files remain unchanged and `start.bat` still selects `main`, now with
these features. The main worktree build and BAT `-CheckOnly` preflight passed;
no product service or real model call was started. Live acceptance is pending.

The 2026-10-10 configuration correction is currently on `develop`, not merged
or pushed to `main`; the stable BAT remains unchanged. Development now uses
global `workflow.model-profile@1` resources and reference-only model sources
`@5/@6`. New runs resolve and freeze current profile/provider values; active
runs and historical facts do not change. See the
[correction record](../docs/MODEL-CONFIG-CORRECTION-2026-10-10.md).

The development credential path is now page-entered Key -> protected private
vault -> `local:<UUID>`. Environment credentials and their manual reference
selector are removed; model services require an explicitly injected private
broker, with no plaintext fallback. All responsibility corrections will be
accepted and merged together. Real-model acceptance uses DeepSeek, with the
user starting the page and entering/saving the Key; online acceptance is pending.

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

The authorized construction branch is `retirement/architecture-2026-10-13`,
not a `develop` checkout eligible for the development BAT. Use the explicit
construction startup record with an isolated schema4 database and a workbench
build whose base is `/workbench/`; do not bypass branch checks or automatically
migrate an existing schema3 database.

For an explicitly isolated backend-only session, run from this directory with
Python 3.10 or later:

```powershell
python -m pip install -e .
New-Item -ItemType Directory -Force .local | Out-Null
python -m phase1_agent.server --port 8877 --database .local/backend-debug.sqlite
```

The server binds to loopback only. This command does not start the Vue
workbench; this construction checkout serves exact frontend-node pages, not the
retired GraphChat or `/tavern/` business entries. A previously built
workbench can be served with `--workbench-dist ../frontend/dist`; it is loaded as
an asset snapshot at startup. The retired `--mode` argument is not accepted.
Model calls are controlled by workflow configuration, not an implicit offline
launcher mode.

## Development Dependencies

From `backend/`, after setting up the repository-root virtual environment:

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
New-Item -ItemType Directory -Force .local | Out-Null
..\.venv\Scripts\python.exe -m pytest tests/test_chat_contracts.py tests/test_workflow_chat_operations.py tests/test_variable_registration_contract.py -q
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
| `frontend_node_entry.py`, `frontend_node_http.py`, `frontend_node_channel.py` | Exact-node frontend addresses and private channel, outside generic commands |
| `frontend_node_snapshots.py`, `frontend_node_connections.py`, `static/frontend-node-*` | Real postcommit display reads, node-owned notifications and frontend page |
| `chat_contracts.py`, `workflow_chat_operations.py`, `workflow_chat_service.py`, `workflow_chat_runtime.py` | Registered logical chat, fixed reroll basis, candidate selection and floor fork |
| `checkpoint_collection.py`, `runtime_checkpoints.py`, `checkpoint_repository.py`, `checkpoint_loading.py` | Explicit schema4 admission, full/diff storage and fresh-target loading |
| `graph_application.py`, `graph_application_contracts.py` | Command/query boundary and identity checks |
| `graph_input_declarations.py` | Management-only `definition.inputs`, editable declarations for incomplete graphs and separate active required inputs |
| `workflow_host.py`, `graph_platform.py`, `graph_service.py` | Host composition, graph/session operations and object adoption |
| `graph_execution.py`, `graph_runtime_host.py` | Compilation and hosted execution |
| `agent_package.py`, `agent_executor.py`, `runtime.py`, `runtime_hosting.py` | Native Agent execution, tool boundaries and same-process continuation |
| `capability_registry.py`, `capability_packages.py`, `builtin_packages.py` | Exact package selection and current declarations |
| `storage.py`, `graph_store.py`, `runtime_fact_store.py`, `workflow_lifecycle.py` | Storage version 20 records/facts and exact target archive/deletion |
| `graph_drafts.py`, `graph_editing_host.py`, `workflow_configuration.py` | Original evidence reads, formal configuration/CAS and management scope checks, not persistent editing drafts |
| `credential_contract.py`, `credential_store.py`, `graph_provider_host.py`, `provider_discovery.py` | Neutral credential contracts, independent ProviderConfigurationService, private vault, exact receipts and bounded discovery |
| `content_configuration.py` | Independent trusted global-content management, excluding provider/profile types |
| `model_contract.py`, `model_service.py`, `model_host_service.py`, `model_package.py` | Global named profiles, reference-only source nodes, recursively frozen configuration and model calls |
| `adapter.py`, `gemini_adapter.py`, `gemini_capabilities.py`, `provider_metadata.py` | Protocol conversion, thinking validation and original signed parts |
| `context_v4.py`, `context_v4_nodes.py`, `context_receipt.py` | Native context, verified writeback and CAS receipts |
| `prompt_package.py`, `prompt_lifecycle.py`, `prompt_assembly.py`, `context_prompt_v6.py` | Lifecycle materials, ordinary `PROMPT@2` and native Agent `PROMPT@6` |
| `tavern/`, `lorebook_nodes.py`, `lorebook_engine.py` | Current Lorebook nodes/resources; old chat source retained only as source/attribution |

Schemas are checked in under `backend/schemas/`; exporters are under
`backend/scripts/`.

Thinking summaries are display data, not private protocol state. Gemini
signatures remain opaque and preserve original part order and tool bindings;
they must not be forged, removed or exposed as answer text.

Draft input discovery reads each node declaration independently and returns
compile diagnostics instead of hiding all fields when a graph is incomplete.
`required_for_run` is `null` when compilation fails; editable declarations are
not automatically required by the current execution roots. Consumer permission
is not expanded. Prompt group fields are an exact frontend extension, not a new
node or data schema. Display names and directory archive metadata remain
management editing records, separate from runtime facts and resource bodies.

The P5/P6 targeted backend group passed 286 tests in 14 files using isolated
databases and mock transports, not a full backend rerun or live BAT acceptance.

Private credentials use a separate `<database>.credentials.sqlite` protected
with Windows current-user DPAPI and path-bound entropy. No plaintext fallback
is available. Public provider schema 3 stores only a `local:<UUID>` reference
or `null`; environment references and provider schema 1/2 are retired. Credential
commands bypass public request-body persistence and return secret-free
receipts. Unknown requests are never replayed. Key rotation or deletion after
run preparation prevents later dispatch; it does not cancel an already sent call.
Vault migration to another account, host or path is not guaranteed.

The 2026-10-11 restructuring remains uncommitted on `develop`; `main` and all
launchers are unchanged. Independent provider/content management, target
workflow/session archive and deletion, and live prompt/lorebook reads are in
place. Definition derivation/autosave and per-request model settings are still
unfinished. See the [implementation record](../docs/中期检查/11-首批整改实施与离线验证-2026-10-11.md).
Storage v17 no longer runs legacy retirement scans during initialization;
explicit development reset owns scoped cleanup and separate request evidence.

The provider form has one "Save provider" action, for both creation and updates.
Editing remains a draft until submission. With a newly entered Key,
`provider.credential.save` persists the complete explicit provider configuration
and its private Key; without one, ordinary configuration saving preserves the
existing Key. The dedicated `provider.credential.bind` mutation/UI is removed;
old binding receipts remain read-only evidence. Partial failure does not mean
the provider configuration was saved. Original operation and non-secret
parameters are part of the durable identity. Both authoritative receipts and
complete configuration must verify before reporting success; missing evidence stays
unresolved. Public `credential.save`, old `MODEL_RESOURCE`, `model_configuration.py`
and `model_selection.py` are removed, not restored as compatibility routes.

The runtime is not a general parallel tool scheduler or an active cross-process
checkpoint recovery system. Current native Agents do not register a failed-node
retry policy. A retained successful result may permit `retry_acceptance`
without repeating model or tool execution. Reloading persistent history does
not recreate an active continuation, and selecting a different candidate does
not undo external tool effects.
