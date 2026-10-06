# Sources and Third-Party Notices

## Demo Snapshot

This repository is a consolidated demo snapshot, prepared on 2026-10-06.
The development repositories and their histories remain separate. The
snapshot does not merge or publish their complete histories.

| Component | Source revision |
| --- | --- |
| Frontend | `3b6ac8e44a0014d2f89b3bfa8a3f1e64f614a06b` |
| Backend | `117a748526c8f0fbe3d0fb22f156f2e05fff2cb2` |
| Documentation evidence | `b4814d0c3408576fb09fc939ebee33a254382b1e` |

Release changes are limited to packaging paths, concise documentation and
repository hygiene. The Python import name remains `phase1_agent`.
Historical implementation reports, runtime databases, credentials and
temporary acceptance outputs are not part of this snapshot.

## smolagents

Upstream: <https://github.com/huggingface/smolagents>

The project's original investigation baseline was
`227ef5e49ddd82339295939072f0223249aa8d38`.
The vendored package is selected from the backend checkout recorded above;
it is not a claim that every vendored file equals that upstream baseline.
Its original license and source headers are retained under
`vendor/smolagents/`.

The backend's native runtime does not use the old smolagents agent loop,
memory or model cleaner. The optional `smolagents.Tool` compatibility
entry and some legacy tests use the vendored package. Keeping that package
does not make all OpenAgent code Apache-2.0 licensed.

## Frontend Dependencies

Dependencies are installed from `frontend/package-lock.json`; their
license files and copyright notices remain in the installed packages.
The primary runtime packages are Vue, Pinia, Vue Flow and Lucide.
Vite, Vitest, TypeScript and vue-tsc supply development tooling.
The lockfile, rather than this summary, fixes their exact versions.

ComfyUI frontend was used as a design reference for workflow organization,
typed ports and separation of editing from execution. The source
documentation records no copying of its application, litegraph,
components, styles, assets or plugin code into this frontend.
ComfyUI, SillyTavern, dsh, LangChain and LangGraph reference checkouts
are not distributed with this demo.

## Backend Dependencies

The core backend declares jsonschema, the OpenAI Python SDK and HTTPX in
`backend/pyproject.toml`. The SDK is a protocol client, not a requirement to
use an OpenAI-hosted model. Package notices and licenses remain applicable
to each installed dependency.

## Project License

The OpenAgent-wide license decision remains pending; see
[LICENSE.md](LICENSE.md). This document records sources and boundaries,
not a complete legal audit or a per-line originality determination.
