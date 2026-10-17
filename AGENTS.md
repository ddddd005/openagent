# Project Guidance

## Engineering Defaults

- Prioritize development and debugging, a simple core path, and visible diagnostic errors.
- Avoid speculative fallback, recovery and retry mechanisms. Before adding one, ask whether it could hide a deeper error and whether normal operation actually requires it.
- Keep edits scoped to the request and existing code patterns. Preserve unrelated changes and the dirty worktree; never revert them without authorization.
- Report actual write and test results. Historical tests, planning approval and uninstalled helpers are not evidence that a new design is implemented or accepted.

## Collaboration

- Ask the user directly before deciding a moderately complex or unconfirmed architecture, responsibility boundary, contract, node, scheduler or user flow. Do not fill gaps by inventing a complete design.
- Where the user has delegated plan review or agent coordination for a task, the lead may review plans and delegate work within that task's approved requirements. New architecture, responsibility boundaries or mechanisms still require a report of the reason, affected scope and intended result before the user decides.
- Follow the latest explicit user decisions. Do not reopen settled behavior. If documents conflict and the user has not already resolved the conflict, explain the concrete issue and ask before proceeding.
- Stop and report substantive conflicts, missing prerequisites or scope violations before implementation; do not work around the authorization boundary.
- Treat source code as implementation evidence, not as an architectural mandate. Old proposals, precise version tables and previously built foundations do not authorize installing them.
- Read only the current documents and historical evidence needed for the task. Keep product decisions and delivery history in documents, not in this file.

## Current Status: Legacy Debt Cleanup Closed

- The approved legacy-debt cleanup is closed. The seven P legacy checks and the original 49 pending identities have been disposed of, not left pending. Do not resume retired test matrices or start the next architecture stage without a new user request. Maintain cleanup follow-up documents in `docs/开发新基线-2026-10-16/旧债清理/`, with test results in the implementation ledger.
- Retire low-value old tests rather than recreating legacy architecture or one-for-one replacement matrices. Keep useful small current checks and record dropped coverage. Future error propagation, lifecycle and tool-call changes belong to the architecture stage, not this cleanup; retiring a test is not fixing or accepting the current product behavior.
- Keep the two E operational checks separate. Test cleanup does not authorize existing service startup/shutdown, browser installation or real model calls.
- Consolidate tests only where verification intent and boundaries genuinely overlap; do not force merges. Identify invalid content precisely before removing it, and preserve current-behavior checks or record their replacement coverage.
- Separate original failures, later probe findings, existing local fixes and newly verified results. Do not treat error-signature matching as a final retirement decision or subtract historical fixes to invent a current failure count.
- Documents 65-68 describe the subsequent architecture stage, not an installed implementation or authorization to mix new architecture into debt cleanup.

## Authorization and Safety

- Work only within the current authorization. Completed cleanup approvals are scoped to their documented stages, not standing authorization for more migration, removal or architecture work. Do not restore retired production APIs or install next-stage architecture to satisfy old tests. New product behavior, unresolved contracts, business-data cleanup, existing service startup/shutdown, real model calls, dependency installation, commits, merges and pushes require separate explicit authorization.
- Obtain approval for removal or migration outside the agreed scope. Explain the affected content and dependencies, and verify absolute data paths. Existing cleanup approvals remain subject to their documented stage and replacement prerequisites.
- Protect credentials, original requests and historical evidence. Do not log secrets, rewrite confirmed execution facts or automatically replay unknown requests. Keep provenance and original hashes intact.
- Normal development uses `develop`. `start.bat` requires a clean `main` checkout; do not bypass branch checks, change launcher selection or provision a stable checkout without authorization.
- Preserve the archived retirement checkout at its original path, its Git worktree lock and path-bound test vaults. Do not use it for new development or move credential vaults.
- Past approvals for promotion, handover, proxy adjustments or pushes are scoped historical approvals, not continuing authorization. Do not infer current service or database state from old receipts.

## Reading Entry Points

- [Current cleanup scope and rules](docs/开发新基线-2026-10-16/旧债清理/README.md).
- [Debt ledger and statistical corrections](docs/开发新基线-2026-10-16/旧债清理/01-旧债清理总账与统计订正-2026-10-17.md).
- [Historical guidance and evidence navigation](docs/开发新基线-2026-10-16/旧债清理/02-历史指引精简与阅读索引-2026-10-17.md).

Moving a rule out of this file does not revoke it. Historical instructions are not current installation requirements; unresolved architecture questions go to the user.
