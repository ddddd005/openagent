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
- Normal development uses `develop`. On 2026-10-16 the user approved local fast-forward promotion of the accepted retirement baseline into `develop` and `main`, superseding the temporary sole-construction-site and original-worktree read-only restrictions for that transition. Keep this retirement checkout and its local acceptance evidence; subsequent product work uses `develop` under its own task authorization.
- The normal `start.bat` still selects a clean `main` checkout. Do not change that arrangement, bypass launcher branch checks, start product services or call real models without explicit authorization. Offline tests and builds for the approved construction scope are allowed here.

## Current Status and Documents

The user approved the complete snapshot and temporary retirement worktree on 2026-10-13, then local promotion after scoped D/E closeout and two-round offline acceptance on 2026-10-16. Clean `develop` and `main` worktrees were fast-forwarded to the same accepted baseline. The local promotion does not authorize pushing, proxy changes, deployment, database migration, real model calls or new product work; the user will adjust the network proxy before any later push.

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
- [Stage D core HTTP/browser scoped closeout, current-contract test evidence and remaining manual/real-model limits](docs/中期检查2/33-阶段D核心路径HTTP与页面限定验收收尾回执-2026-10-15.md).
- [Historical variable manual-entry suggestion, superseded by documents 35-36; E input/Tavern review and unresolved retirement scope remain separate](docs/中期检查2/34-阶段D变量主动入口与E输入及Tavern接续审阅规划-2026-10-15.md).
- [Unified session-variable registration, explicit read/write, approved variable checkpoint admission and subsequent Lorebook variable-read work plan; supersedes document 34's migration deferral, installation recorded in document 36](docs/中期检查2/35-变量统一登记与检查点准入及Lorebook读取接续执行规划-2026-10-15.md).
- [Variable registration, explicit interaction/read/write, schema 3 checkpoint/fork and Lorebook delivery; scoped offline acceptance, precise historical exclusions and unmodified product databases](docs/中期检查2/36-变量统一登记与检查点及Lorebook接续实施回执-2026-10-15.md).
- [Stage D remaining differences: runtime environment and real-model/browser evidence; E, extensions and historical cleanup kept separate, documentation only](docs/中期检查2/37-阶段D剩余差异与接续边界整理-2026-10-15.md).
- [Post-D legacy code cleanup: unused history authority, copy/restore/rebind and persistent form shells removed; receipt evidence preserved; E remains subsequent development](docs/中期检查2/38-阶段D后旧架构代码整理实施回执-2026-10-15.md).
- [Variable registration menu and typed-form correction; inline validation, scoped test results and browser evidence limited to invalid-field feedback](docs/中期检查2/39-变量注册菜单与类型表单修正回执-2026-10-15.md).
- [Stage E editable-input and frontend/backend node responsibility brief; frontend intents route through their bound backend node, implementation contracts remain subsequent work](docs/中期检查2/40-阶段E输入修改能力与前端后端节点职责简述-2026-10-15.md).
- [Stage E source survey and execution plan: compliant configuration/input, bound backend-node capabilities, internal communication/display, node-owned frontend entry/binding and scoped legacy replacement; recommendations and undecided contracts separated, documentation only](docs/中期检查2/41-阶段E输入修改与前端节点接入执行规划-2026-10-15.md).
- [Stage E first code batch: trusted narrow configuration mutations, editable input, generic policy controls, frozen-run/checkpoint verification and scoped offline results; whole frontend node and E2-E5 remain subsequent work](docs/中期检查2/42-阶段E首批合规配置修改与可编辑输入实施回执-2026-10-15.md).
- [Stage E backend frontend-node installation: new-message append proof, real postcommit display reads, exact-node operations and scoped results; browser communication, frontend entry and legacy replacement remain subsequent work](docs/中期检查2/43-阶段E整体节点消息追加与后端窄调用实施回执-2026-10-15.md).
- [Stage E node-private input and communication work scope: 200ms belongs only to this frontend node; exact state, request custody and injected client port, with network entry deferred](docs/中期检查2/44-阶段E节点内部通信与输入交互执行说明-2026-10-15.md).
- [Stage E internal channel and private client delivery: real offline client-to-node checks, original receipt custody and scoped verification; network port, page and open button remain subsequent work](docs/中期检查2/45-阶段E节点私有输入与内部通信实施回执-2026-10-15.md).
- [Stage E node-owned HTTP entry, exact binding, local notifications and page/workbench installation scope; approved existing HTTP host, no additional numeric port](docs/中期检查2/46-阶段E节点HTTP入口与页面接入执行说明-2026-10-15.md).
- [Stage E HTTP/page installation and scoped acceptance: exact node routes, real fetch and browser evidence, precise historical exclusions; existing services unmodified and E5 legacy entry replacement remains subsequent work](docs/中期检查2/47-阶段E节点HTTP入口与页面限定验收实施回执-2026-10-15.md).
- [Stage E5 approved retirement plan: ordinary creation only, legacy consumer and presets retired, Lorebook and original evidence preserved, exact old test workflows eligible for cleanup](docs/中期检查2/48-阶段E旧入口与预设设施退役执行规划-2026-10-15.md).
- [Stage E5 retirement and exact test-workflow cleanup receipt; scoped verification, historical gaps and remaining independent frontend capabilities](docs/中期检查2/49-阶段E旧入口退役与测试工作流清理实施回执-2026-10-15.md).
- [Independent workflow operations and exact-node entry work scope](docs/中期检查2/50-工作流独立操作模块与节点入口补齐执行说明-2026-10-15.md).
- [Reroll/candidate semantics corrected by the user: same round, all candidates persisted and checkpointed, selected continuation; implementation prerequisites remain](docs/中期检查2/51-重roll与候选独立功能勘察及待裁决-2026-10-15.md).
- [Chat checkpoint reference survey and recommended logical-value type; candidate data and pre-round baseline contracts not installed](docs/中期检查2/52-聊天数据检查点准入与分叉引用边界勘察-2026-10-15.md).
- [Independent operations and resume/close/checkpoint node-page delivery; scoped offline checks and remaining candidate/chat boundaries](docs/中期检查2/53-工作流独立操作与节点入口补齐实施回执-2026-10-15.md).
- [Approved logical-chat direction, actual candidate checkpoint links, fixed pre-round reroll and full-candidate fork rules](docs/中期检查2/54-重roll楼层候选与检查点接续执行规划-2026-10-15.md).
- [Logical chat data-layer persistence, checkpoint schema4, independent reroll/select/floor-fork and exact-node consumers; scoped offline delivery, historical exclusions and unresolved multi-item computation bases](docs/中期检查2/55-聊天候选数据层与重roll检查点接续实施回执-2026-10-15.md).
- [D/E legacy retirement survey and staged closeout inventory; current-menu version gap, receipt-only prerequisites, protected shared functionality and pending acceptance, investigation only](docs/中期检查2/56-D-E阶段旧内容退役勘察与收口清单-2026-10-15.md).
- [D/E scoped legacy retirement and code closeout; current menu, receipt-only verification, mixed-test cleanup and final offline evidence, with deployment and real-model acceptance remaining](docs/中期检查2/57-D-E阶段旧内容退役与收口实施回执-2026-10-15.md).
- [User-built minimal workflow two-round offline acceptance, committed D/E baseline and recommended same-commit develop/main fast-forward sequence; no merge or push performed](docs/中期检查2/58-用户极简工作流两轮离线验收与主干接续建议-2026-10-16.md).
- [Authorized local develop/main fast-forward promotion, clean-worktree checks and documentation handover; no push, deployment or database changes](docs/中期检查2/59-新架构本地晋升develop与main实施回执-2026-10-16.md).

Moving a rule out of this file does not revoke it. Historical instructions are not current installation requirements; unresolved architecture questions go to the user.
