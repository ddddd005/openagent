# Project Guidance

## Engineering Defaults

- Prioritize development and debugging, a simple core path, and visible diagnostic errors.
- Avoid speculative fallback, recovery and retry mechanisms. Before adding one, ask whether it could hide a deeper error and whether normal operation actually requires it.
- Keep edits scoped to the request and existing code patterns. Preserve unrelated changes and the dirty worktree; never revert them without authorization.
- Report actual write and test results. Historical tests, planning approval and uninstalled helpers are not evidence that a new design is implemented or accepted.

## Collaboration

- Ask the user directly before deciding a moderately complex or unconfirmed architecture, responsibility boundary, contract, node, scheduler or user flow. Do not fill gaps by inventing a complete design.
- For the current first-version work, the user delegated review of implementation plans that fulfill already-decided requirements to the lead agent. The lead may approve these plans and dispatch subagents. Additional architecture or mechanisms discovered during implementation still require a report of the reason, affected scope and intended result before the user decides; do not prebuild handling for hypothetical errors.
- Follow the latest explicit user decisions. Do not reopen settled behavior. If documents conflict and the user has not already resolved the conflict, explain the concrete issue and ask before proceeding.
- Stop and report substantive conflicts, missing prerequisites or scope violations before implementation; do not work around the authorization boundary.
- Treat source code as implementation evidence, not as an architectural mandate. Old proposals, precise version tables and previously built foundations do not authorize installing them.
- Read only the current documents and historical evidence needed for the task. Keep product decisions and delivery history in documents, not in this file.

## Authorization and Safety

- Work only within the current authorization. Permission to investigate or edit documents is not permission to change product code, run product tests, clean data, start services, call models, commit, merge or push.
- Obtain approval for removal or migration outside the agreed scope. Explain the affected content and dependencies, and verify absolute data paths. Existing cleanup approvals remain subject to their documented stage and replacement prerequisites.
- Protect credentials and original request evidence. Do not log secrets, rewrite confirmed execution facts or automatically replay unknown requests.
- Normal development uses `develop`; the user approved the temporary `retirement/architecture-2026-10-13` branch in this worktree as the sole construction site. The original `openagent` directory is a read-only baseline. Do not write, test or run product code there.
- The normal `start.bat` still selects a clean `main` checkout. Do not change that arrangement, bypass launcher branch checks, start product services or call real models without explicit authorization. Offline tests and builds for the approved construction scope are allowed here.

## Current Status and Documents

The user explicitly approved the complete snapshot, worktree transition and resumption of product construction on 2026-10-13. This worktree is the only construction site. Work through already-decided retirement responsibilities in reviewed batches; unresolved new contracts still go to the user. Resumption does not authorize new architecture, real model calls, data imports, final merge or push.

- [Snapshot, worktree and first retirement batch](docs/中期检查2/05-完整快照与退役施工首批授权回执-2026-10-13.md).
- [Current product decisions and operation boundaries](docs/中期检查/41-当前产品决定与实施边界-2026-10-13.md).
- [Work order and remaining review gates](docs/中期检查/40-统一工作规划与实施顺序-2026-10-12.md).
- [Historical authorization and delivery index](docs/中期检查/42-历史授权与交付索引-2026-10-13.md).
- [Step 1 consolidated review and delegated implementation approval](docs/中期检查2/09-第1步极简契约合订与统一审阅稿-2026-10-13.md).
- [Step 2.1 registration and Agent read/write delivery](docs/中期检查2/10-2.1登记与Agent读写基础实施回执-2026-10-13.md).
- [Steps 2.2-2.3 collection, checkpoint storage and success-save delivery](docs/中期检查2/11-2.2收集仓储与2.3成功点实施回执-2026-10-13.md).
- [Steps 2.4-2.5 target loading and atomic fork batch](docs/中期检查2/12-2.4目标装载与2.5完整分叉实施回执-2026-10-13.md).
- [Step 2.6 checkpoint acceptance and minimal correction](docs/中期检查2/13-2.6检查点整体验收与最小修复回执-2026-10-14.md).
- [Step 3 legacy entry retirement and code audit receipt](docs/中期检查2/14-第3步旧入口退役与代码勘察回执-2026-10-14.md).
- [Step 3 execution-agent work document and remaining gates](docs/中期检查2/15-第3步新架构接入执行文档-2026-10-14.md).
- [Step 3 installation, backend-only checkpoint interface and scoped acceptance](docs/中期检查2/16-第3阶段正式接入与后端检查点接口实施回执-2026-10-14.md).
- [Definition-layer declaration module work document and scoped legacy authority retirement](docs/中期检查2/21-定义层节点声明管理模块执行文档-2026-10-14.md).
- [Legacy definition authority retirement results and temporarily unavailable operations](docs/中期检查2/22-定义层旧声明权威退役回执-2026-10-14.md).
- [Unified definition installation, ordinary execution filtering and scoped acceptance](docs/中期检查2/23-定义层安装与普通执行筛选实施回执-2026-10-14.md).
- [Context data-item decoupling and per-output chat isolation execution plan; variable registration unchanged](docs/中期检查2/24-上下文数据项解耦与聊天上下文隔离执行规划-2026-10-14.md).
- [Scoped context-write and frontend-order retirement, value-snapshot checks and remaining output/item decisions](docs/中期检查2/25-上下文闭环旧职责退役与接续关口回执-2026-10-14.md).
- [Test-output/runtime boundaries and schema 3 editing prerequisites; historical item-scope question superseded by document 27](docs/中期检查2/26-测试输出与schema3编辑前置实施回执-2026-10-14.md).
- [Context item registration and reuse of existing session storage/checkpoints; no additional session-level registry, variables and checkpoint admission unchanged in this batch](docs/中期检查2/27-上下文登记与既有会话存储复用执行文档-2026-10-14.md).
- [Minimal context wiring audit; its Agent-side next_view proposal is superseded by document 29](docs/中期检查2/28-上下文登记最小接线与实施前裁定-2026-10-14.md).
- [Agent computation versus context projection, exact item selection, test-period display format and saved-state initialization correction; documentation only](docs/中期检查2/29-Agent计算与上下文更新解耦及分叉初始化订正规划-2026-10-14.md).
- [Independent context item registration, computation/update wiring and saved-state fork delivery; scoped offline acceptance and remaining boundaries](docs/中期检查2/30-独立上下文登记与保存状态分叉实施回执-2026-10-14.md).
- [Stage D four-parameter and secondary-limit work plan; user approved implementation and canceled the extra summary ceiling](docs/中期检查2/31-阶段4模型四参数与二次限额退出执行规划-2026-10-14.md).
- [Stage D four-parameter/request-service installation and secondary-limit retirement; scoped offline acceptance, exclusions and default-build side effect](docs/中期检查2/32-阶段4模型四参数与二次限额退出实施回执-2026-10-15.md).

Moving a rule out of this file does not revoke it. Historical instructions are not current installation requirements; unresolved architecture questions go to the user.
