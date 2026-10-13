# Project Guidance

## Engineering Defaults

- Prioritize development and debugging, a simple core path, and visible diagnostic errors.
- Avoid speculative fallback, recovery and retry mechanisms. Before adding one, ask whether it could hide a deeper error and whether normal operation actually requires it.
- Keep edits scoped to the request and existing code patterns. Preserve unrelated changes and the dirty worktree; never revert them without authorization.
- Report actual write and test results. Historical tests, planning approval and uninstalled helpers are not evidence that a new design is implemented or accepted.

## Collaboration

- Ask the user directly before deciding a moderately complex or unconfirmed architecture, responsibility boundary, contract, node, scheduler or user flow. Do not fill gaps by inventing a complete design.
- Follow the latest explicit user decisions. Do not reopen settled behavior. If documents conflict and the user has not already resolved the conflict, explain the concrete issue and ask before proceeding.
- Stop and report substantive conflicts, missing prerequisites or scope violations before implementation; do not work around the authorization boundary.
- Treat source code as implementation evidence, not as an architectural mandate. Old proposals, precise version tables and previously built foundations do not authorize installing them.
- Read only the current documents and historical evidence needed for the task. Keep product decisions and delivery history in documents, not in this file.

## Authorization and Safety

- Work only within the current authorization. Permission to investigate or edit documents is not permission to change product code, run product tests, clean data, start services, call models, commit, merge or push.
- Obtain approval for removal or migration outside the agreed scope. Explain the affected content and dependencies, and verify absolute data paths. Existing cleanup approvals remain subject to their documented stage and replacement prerequisites.
- Protect credentials and original request evidence. Do not log secrets, rewrite confirmed execution facts or automatically replay unknown requests.
- Develop on `develop`; the normal `start.bat` selects a clean `main` checkout. Do not change that arrangement or start hidden services without authorization.

## Current Status and Documents

Product implementation remains paused. The current authorization is documentation organization only; it does not resume implementation or cleanup.

- [Current product decisions and operation boundaries](docs/中期检查/41-当前产品决定与实施边界-2026-10-13.md).
- [Work order and remaining review gates](docs/中期检查/40-统一工作规划与实施顺序-2026-10-12.md).
- [Historical authorization and delivery index](docs/中期检查/42-历史授权与交付索引-2026-10-13.md).

Moving a rule out of this file does not revoke it. Historical instructions are not current installation requirements; unresolved architecture questions go to the user.
