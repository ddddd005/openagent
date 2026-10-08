# 文档索引

当前产品主干是 `main`，`develop` 是基于同一主干继续开发与隔离验收的工作区，不是另一套产品实现。2026-10-09 的节点整理与 Gemini 工具、思考已进入主干并正常推送；推送不等于部署服务或发布安装包。应用元数据仍为 `0.2.0`，固定 `v0.2.0` 标签不是最新主干身份。

## 当前手册

| 文档 | 用途 |
| --- | --- |
| [项目介绍](../README.md) | 当前能力、产品边界与开始入口 |
| [快速启动](QUICKSTART.md) | 安装依赖、正式/开发 BAT、端口与同源启动 |
| [使用手册](USER-GUIDE.md) | 工作台、资源、模型与思考、运行控制、聊天和历史 |
| [节点目录](NODE-DIRECTORY-2026-10-09.md) | 当前 44 个声明、41 个家族、9 个分类及旧图清理规则 |
| [开发说明](DEVELOPMENT.md) | 分支规则、代码入口、数据权威与修改约束 |
| [后端说明](../backend/README.md) | Python 包、当前模型/上下文边界及契约入口 |
| [前端说明](../frontend/README.md) | Vue 工作台、开发依赖与当前前端入口 |
| [测试说明](TESTING.md) | 可执行验证命令、分层证据与已知失败 |
| [调试说明](DEBUGGING.md) | 定位身份、事实、诊断及原请求保护 |
| [酒馆说明](tavern/README.md) | 世界书、提示词组、聊天壳与完成回合分叉 |
| [本机存档恢复](tavern/LOCAL-STORAGE-RECOVERY.md) | 不可读存档的诊断、备份与受限恢复 |
| [第三方说明](../THIRD_PARTY_NOTICES.md) | 来源及许可边界 |

旧节点不提供迁移、兼容选择器或自动换版；SQLite v15 / 工作台存档重读按精确退休身份删除旧图及关联测试内容，不猜测删除未知插件或版本。当前数据与请求的保护边界以节点目录和使用手册为准。

## 当前实现记录

| 文档 | 记录范围 |
| --- | --- |
| [主干晋升与旧版归档](MAIN-PROMOTION-2026-10-09.md) | 当前版进入 `main`、旧源码/默认稳定库快照、随后正常推送 |
| [Gemini 实现验收](GEMINI-ACCEPTANCE-2026-10-09.md) | G1-G5 的工具、思考摘要、签名回传和限定验收 |
| [工作区梳理验收](WORKTREE-ACCEPTANCE-2026-10-09.md) | 先前工作区归属、分组提交与分支建立 |

各批结果按实际代码和范围分别解释，不累加重叠案例。当前已登记前端 928 项、主干后端定向 239 项通过；此前后端全量第二轮为 3008 通过、50 项既有失败、9 跳过，不宣称全量绿色。

原始日志、截图、SQLite、依赖、构建产物和源码 ZIP 位于开发机器忽略的 `.local/` 等目录，不随 Git 推送提供。`main-before-node-unification-2026-10-09` 归档标签本轮仍只保存在本地；旧提交历史仍保留在远端。远端用户应依照测试说明重建所需证据，不能把缺少本机产物理解为产品缺失或已完成部署。

## 后续规划

- [后续待办](BACKLOG.md)：实际未完成事项和按需能力，不把已落地主干的功能继续列为待实现。
- [1.0 规划](PLAN-1.0.md)：发布门槛与范围，不将元数据 `0.2.0` 等同于 1.0 发布。
- [Gemini 规划](PLAN-GEMINI-THINKING.md)：保留设计与验收矩阵，实际状态见实现记录和节点目录。
- [上下文精简规划](PLAN-CONTEXT-COMPACTION.md)：设计沿革与当前策略边界，不据旧路线重建摘要节点。
- [酒馆包规划](PLAN-TAVERN-PACKAGE.md)与[专项计划](tavern/PLAN.md)：设计范围和后续边界。

## 历史快照

下列页面保留登记时的状态、提交、节点版本、测试数字和运行产物。文中的“当前”“下一步”“未推送”、旧端口与旧模块指当时的快照，不是最新使用指南；已退役的源码路径应按记录提交从 Git 历史查看，而不是按当前代码执行。固定基线文档及已登记哈希不倒改。

- [2026-10-06 进度基线](BASELINE-2026-10-06.md)与[2026-10-07 的 0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)。
- [首轮退役盘点](REPLACE-AUDIT-2026-10-07.md)与[支持处置矩阵](REPLACE-SUPPORT-MATRIX.md)。
- [供应商入口](REPLACE-PROVIDER-2026-10-07.md)、[工作台布局](REPLACE-LAYOUT-2026-10-07.md)、[提示词资源](REPLACE-PROMPTS-2026-10-07.md)。
- [默认包](REPLACE-DEFAULTS.md)、[聊天入口](REPLACE-CHAT-ENTRY.md)、[历史校验](REPLACE-HISTORY-RETIREMENT.md)。
- [资源边界](REPLACE-RESOURCE-BOUNDARY.md)、[执行配置](REPLACE-EXECUTION-SELECTION.md)、[回执边界](REPLACE-RECEIPT-BOUNDARY.md)、[旧 pending 保全](REPLACE-LEGACY-PENDING.md)。
- [退役前归档](REPLACE-ARCHIVE-READ.md)、[旧归档接线](REPLACE-GRAPH-ARCHIVE-READ.md)、[2026-10-07 实际移除及部署收口](REPLACE-REMOVAL.md)。
- 酒馆历史：[勘察](tavern/SURVEY-2026-10-08.md)、[P1](tavern/P1-GLOBAL-LOREBOOK-2026-10-08.md)、[P2](tavern/P2-GLOBAL-PROMPT-GROUPS-2026-10-08.md)、[P3](tavern/P3-CHAT-SHELL-2026-10-08.md)、[P4](tavern/P4-COMPLETED-ROUND-FORKS-2026-10-08.md)、[P5](tavern/P5-JOINT-ACCEPTANCE-2026-10-08.md)、[当时本机入口](tavern/LOCAL-ACCEPTANCE-ENTRY-2026-10-08.md)。
