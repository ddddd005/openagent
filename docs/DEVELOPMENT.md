# 开发说明

本版使用 Vue 3、TypeScript、Pinia、Vue Flow 工作台，以及 Python `phase1_agent` 包和 SQLite 持久化。包名沿用开发版本，不等于项目永久限定为第一阶段或固定 A/B。

## 当前开发边界

唯一产品开发主仓为 `openagent`；`step1` 及其旧后端是历史来源，根目录 `smolagents` 等是第三方参考，不作为当前开发分支使用。2026-10-09 的改动归属、限定修补、验收证据与 Git 收口统一见 [工作区梳理与验收](WORKTREE-ACCEPTANCE-2026-10-09.md)。该记录的前六节保留验收时状态，最后一节登记随后提交，不把历史快照当作当前分支状态。

自本次收口起固定以下分支职责：

| 分支 | 职责 |
| --- | --- |
| `main` | 已按明确范围验收的稳定线；不等于后端全量绿色、发布版本或已部署 |
| `develop` | 唯一产品开发线，新能力默认在这里实现、测试和分组提交；明确授权的并行工作按模块划分归属 |
| 临时功能分支 | 仅用于明确授权的并行工作，从 `develop` 派生并合回 `develop`，不直接混入 `main` |

- 开始新能力前先确认范围与验收标准；不再为每次开发另建分支。Gemini 本轮已按用户授权三并发在 `develop` 实现，随后分组提交并经用户授权快进纳入 `main`。
- `develop` 的目标提交通过对应验收后才纳入 `main`；单线无分叉时优先 fast-forward。推送、标签、版本升级和部署分别决策，不因本机验收自动执行。
- 分支只隔离 Git 代码，不隔离运行进程、Vite 页面、端口、SQLite 或浏览器存档。根 `start.bat` 固定启动干净的 `main` 工作区；`start-dev.bat` 是唯一显式开发入口，默认独立端口和数据库。切换代码后须核对实际服务源码和启动记录，不能把旧进程当作新基线。
- `v0.2.0` 继续固定 2026-10-07 的架构收口，不移动旧标签来冒充当前 HEAD。当前版本元数据仍为 `0.2.0`。

Gemini 工具与思考按 [G1-G5 规划](PLAN-GEMINI-THINKING.md) 实施，具体版本、配置、离线验收证据、主干收口及剩余边界见 [本轮实现记录](GEMINI-ACCEPTANCE-2026-10-09.md)。根 `start.bat` 仍固定稳定 `main`，下次启动将使用已合入的 Gemini 实现；Git 合并不自动更新正在运行的服务。

## 代码入口

下表中的 Python 文件均在 `backend/src/phase1_agent/` 下。

| 修改范围 | 入口 |
| --- | --- |
| 服务启动与 HTTP 适配 | `server.py`、`graph_http.py` |
| 统一应用命令与查询 | `graph_application.py`、`graph_application_contracts.py` |
| 应用请求身份与纯回执读取 | `graph_application_identity.py`、`graph_receipts.py`；HTTP 读取在构造服务前截获 |
| 图服务与编译/执行 | `graph_service.py`、`graph_contracts.py`、`graph_execution.py`、`graph_runtime_host.py` |
| 公共资源预检与冻结读取 | `graph_resource_host.py` |
| 资源纯契约与请求门禁 | `resource_contracts.py`、`prepared_request.py`；旧 prepared 请求直接拒绝 |
| 公共 Runtime 与控制 | `runtime.py`、`runtime_hosting.py`、`runtime_control.py`、`runtime_executor_contracts.py` |
| 能力声明与注册 | `host_sdk.py`、`capability_packages.py`、`builtin_packages.py`、`capability_registry.py` |
| 当前精确包选择与依赖解析 | `graph_package_selection.py`、`CapabilityPackageLoader.resolve`；无旧配置运行回退 |
| 普通内容、提示词与模型节点 | `tool_package.py`、`prompt_package.py`、`model_package.py` |
| Agent 执行与失败分类 | `agent_package.py`、`agent_executor.py`、`runtime.py`、`agent_failed_retry.py` |
| 模型调用与受控失败 | `model_service.py`、`model_host_service.py`、`adapter.py`、`contract_errors.py` |
| 上下文读、装配、写回 | `context_package.py`、`context_v3_nodes.py`、`context_v3.py` |
| 前端业务节点 | `frontend_package.py`、`frontend_business.py` |
| 定义、会话、对象与运行存储 | `storage.py`、`graph_store.py`、`graph_records.py`、`session_objects.py`、`runtime_fact_store.py` |
| 旧测试数据的一次性清理 | `storage_retirement.py`；仅版本升级事务使用，不提供旧读写 API |
| 信息源与事实读取 | `graph_information.py`、`runtime_information.py` |
| 独立聊天客户端 | `static/graph-chat.js`、`static/graph-chat-core.js` |

前端入口如下：

| 修改范围 | 入口 |
| --- | --- |
| 应用与工作台 | `frontend/src/App.vue`、`components/GraphWorkbench.vue` |
| 通用图、串行示例 | `domain/workflowGraph.ts`、`domain/serialAgentDemo.ts` |
| 图与会话状态 | `stores/workflowGraph.ts`、`stores/workspace.ts` |
| 浏览器持久化接口与档案 | `adapters/browserStorage.ts`、`adapters/workbenchPersistence.ts`、`stores/workbenchPersistence.ts` |
| 应用边界 | `application/workflowCommands.ts`、`workflowEditing.ts`、`workflowInformation.ts` |
| HTTP 适配 | `adapters/workflowApplicationApi.ts`、`workflowGraphApi.ts`、`workflowResourcesApi.ts` |
| 供应商与模型源编辑 | `components/CurrentProviderPanel.vue`、`ModelSourceFields.vue`、`application/workflowResources.ts` |
| 可信本地前端扩展 | `plugins/workflowFrontendSdk.ts`、`modelFrontendPackage.ts`、`workflowFrontendPackage.ts` |
| 观察与控制 | `components/GraphInformation.vue`、`GraphRunControls.vue`、`GraphHistory.vue` |

前端表中没有前缀的路径继承对应行的 `frontend/src/` 子目录。

## 一次运行的数据流

```text
工作台编辑副本
  -> 保存定义及修订
  -> 创建/选择工作流会话
  -> 应用 run.start 校验身份、状态与冻结依据
  -> 编译激活节点和依赖，准备宿主服务
  -> Runtime 逐节点执行
  -> 校验并接纳输出、事实及声明的对象写入
  -> 激活计划全部成功接纳
  -> 结算完成检查点
  -> 消费者读取显式公开的 presentation.display
```

普通 A -> B 图通过数据边表达输入依赖，通过控制边约束业务写回顺序。B 的模型响应成功后才进入两个上下文 merge 和聊天追加；第二次追加前重新读取对象。这个顺序不是整图事务：前面的对象写入已接纳后，后面的失败不会自动撤销它。

## 数据权威

| 对象 | 权威及职责 |
| --- | --- |
| 工作流定义及修订 | 持久化节点、配置、端口连接、依赖、对象绑定、包版本；不持有某次会话的动态上下文 |
| 工作流会话 | 持有动态对象、历史和活动链关联；同一会话可执行多轮 |
| chain_run / node_run | 持有本次执行与节点尝试的冻结依据、状态、产物和事实；不能用新草稿改写旧运行 |
| 全局当前资源 | 保存可复用供应商配置；运行准备解析资源并冻结实际使用依据 |
| 会话对象与上下文 | 由声明的读写权限及接纳流程管理；上下文读、装配、merge 分开 |
| 界面及消费者缓存 | 编辑副本、请求恢复记录和展示投影；不能替代后端运行事实 |

通用 Runtime 协调节点执行和接纳，不解释 Agent 私有工具语义。模型服务持有真实凭据，节点配置只保存受控的 `env:DEEPSEEK_API_KEY` 或 `env:GEMINI_API_KEY` 引用。信息目录持有声明及绑定坐标，实际正文由登记的信息源或存储读取器提供。

## 修改规则

- 先确认操作针对定义、副本、会话还是原运行。复制和检查点分叉应重映射实际身份，不复用私有节点归属。
- 命令沿统一应用边界提交，保持 request body、idempotency key、修订/CAS 依据和 owner 校验一致。当前应用结果未知时经 `/api/graph/receipts/read` 或 consumer 同作用域入口核实完整原 operation/parameters，不再重发 mutation；后续拒绝不自动证明原请求未发生。
- 工作流编辑复制的核实仅读原回执；缺原坐标的旧副本继续保留，不生成新 key/body。仅初次明确拒绝复制后保存精确 `rejected_copy`，由独立用户重试命令在证据匹配、新读修订和并发门禁下提交，不能把核实与重试合并。
- 节点声明类型、版本、配置 schema、端口和执行器；端口数据必须满足内容契约，不以普通字符串代替带版本的结构。
- 新节点的专有逻辑留在能力包。Agent 的确定失败判断在 `agent_failed_retry.py`，公共 `graph_failed_retry.py` 只协调合法的新尝试。
- 单节点成功、HTTP 200、可读输出、正式交付和整图完成分别处理。不要让模型结果直接绕过接纳与检查点。
- 未知外部效果不能凭空判成失败并重试；成功结果接纳失败不能重新调用模型。
- 模型服务的受控失败经 `ModelRequestError` 保留 `model_provider_error`、`model_dispatch_unknown`、`model_not_dispatched`、`model_response_invalid`，不进入 kernel 的自动 transport retry。安全重试许可仍由原 service facts 和失败重试策略裁决，不能只凭表层错误码授权。
- 活动运行继续依赖同进程现场及冻结服务。持久化历史可读不授予跨进程 `resume` 或失败重试资格。
- 前端专用配置通过确切包、扩展、版本与槽位声明加载；通用编辑器不按模型业务字段硬编码。

## 添加普通处理节点

先阅读 `tool_package.py` 的纯文本处理实现和 `tests/test_tool_package.py`。一个最小节点需要配置 schema、输入/输出端口、类型/版本和执行函数，再通过包注册进入目录。纯处理执行器应只生成合法输出；需要对象读写时通过执行上下文及显式绑定声明，不直接操作 SQLite 或其他节点私有状态。

验证至少覆盖正常输出、错误类型/配置、目录与编译、实例间隔离。需要界面专用编辑器时再接可信本地扩展，不先修改通用调度器。本版不提供完整公共插件 SDK、远程不可信插件隔离或插件市场。

## 退役边界

当前入口只使用普通 Graph、公共 Runtime 与独立能力包。旧固定宿主、私有 Agent 执行器、兼容节点工厂、旧客户端/store、旧 HTTP 路由、在线迁移和私有档案入口已从源码删除；不保留旧运行 fallback 或旧会话只读门面。当前包选择只读 `current-execution`。

SQLite v14 在单笔升级事务中识别确切固定/compat 内容，沿明确归属/引用和共享对象 revision 形成关联闭包，包含 Runtime facts 的 payload 引用及 chain/node 归属；整组删除相关定义/会话、事实、对象、manifest、回执及旧专用表。引用旧历史的当前对象/manifest 和共享旧 revision 不再单独保留，正常关联不再作为升级回滚保全的理由；无关联当前记录、独立资源与配置保留，不因缺包或未知节点而猜旧。有效旧 `project` 选择仅在没有当前行时转入当前配置，移除兼容包，然后删除旧行。保留下来的当前 `node_run@4` 空 `agent` 字段迁为版本 5。升级不是旧接口，不能用来重新启动旧工作流。

当前回执仍通过 raw SQLite `mode=ro` 查询，缺原应用身份或证据不匹配返回 unresolved，不重发 mutation；当前 Vue/GraphChat 保留完整 pending，不为已删除的关联测试内容重建兼容证据。无关联当前数据、可信扩展及共用类型/事实/对象设施保留。旧源码留在 Git 标签及仓库外归档，不放回正式源码树。

最终扫描已删除 `frozen_model.py` 无调用者的旧 stage factory/`legacy_adapter` 透传及前端 `archive.read` 死导出、类型、mock、旧专属测试。本批后端初轮 55 个目标、789 个案例为 780 通过 / 9 失败；修复后 8 文件 55 项通过（47.31 秒），9 项原失败均通过；最终扫描对应后端补测 4 文件 64 项通过（24.46 秒）。前端初轮 16 文件 331 通过 / 10 失败、相关修复后 3 文件 102 项通过，最终持久化/API 两文件 26 项及类型/构建通过；期间 TS2345 索引类型收窄错误已通过绑定 `entryRow` 修复。最终 JS 430.45 kB / gzip 137.23 kB。工具中断无可用最终结果不计，后端两批及前端持久化 15 项等重叠范围不累加。

2026-10-07 上一轮整合时，阶段 A 的盘点裁决与阶段 B 的实际代码移除、第一批定向验证已完成；208 文件 AST 本地导入扫描 0 缺失已复核，固定基线哈希/退役前标签解引用提交不变。第二批独立非 editable wheel 安装最终 33/33、限定 Mock 浏览器只读联合核对 74/74 通过；前端观察/终态同步窄修后 1 文件 13 项及类型/构建通过，当时 JS 431.16 kB / gzip 137.41 kB。上述代码及文档已纳入阶段性整合提交 `8eb7e93` 并推送至 `origin/main`，退役前标签 `legacy-retirement-2026-10-07` 同步推送；整合后的文档整理未重复已有验证。本轮新增进展见下方，包版本仍为 `0.1.0`，未发布 1.0、未另立完成基线。集中结果、具体文件与本地日志/JUnit 见 [实际移除记录](REPLACE-REMOVAL.md)。

最新阶段 C 本地收口完成：三个并发组补齐必要后端行为/分类、PC 复制安全及长表单约束，并新增 `--workbench-dist`。`server.py` 在启动时读取 index 和允许资产为 byte snapshot，以同一 loopback origin 提供工作台、GraphChat 和 API，不提供任意文件或 SPA fallback；工作台单独允许 VueFlow 所需样式属性，原聊天/API 安全策略不变，运行中重建资产须重启才换版。

本轮后端必要行为 176 独立目标、部署入口 65 独立目标、前端 7 文件 116 项及类型/生产构建通过；当前非 editable 安装/同源资产 18/18、浏览器后只读核对 23/23 通过，生产 JS 432.86 kB / gzip 137.88 kB。旧 wheel 与旧通过数不冒充匹配这些新修改，范围不累加。本轮当前 wheel SHA256 为 `b5b82a9c0c4696c51e70fa754e0a99c9e4e176d194e30634648c446e0b2414b3`，复用已有独立 venv 重装，不称全新环境；详见 [阶段 C 本地收口](REPLACE-REMOVAL.md#阶段-c-本地收口)。

上述为先前本地收口时的状态。随后已完成限定真实供应商及同版常驻门槛，更新版本元数据为 `1.0.0`，标准隔离构建 wheel 并在新的 `.local/resident/runtime/` 非 editable 安装，最终核对 21/21；104 后端包文件与目标源码匹配，且与收费验收时的 0.1.0 wheel 逐字节相同。8765 同源服务使用稳定 dist 和新库，旧启动快捷方式已改指当前入口；没有 Windows 服务注册、自启动或旧 runtime fallback。原 8873 构建及各轮统计不改写。

阶段 C 本期范围已完成。用户随后指定本次提交为 `0.2.0` 基线，见 [最终合并记录](REPLACE-REMOVAL.md#阶段-c-最终收口)与 [0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)。版本元数据、隔离构建 wheel 和常驻已统一到 0.2.0，安装核对 21/21、`pip check` 通过；0.1.0、此前本地 1.0.0 和 0.2.0 的 104 个包文件逐字节相同，前端依赖与稳定 bundle 未变，不重复业务回归或收费。本次 Git 身份为 v0.2.0，不是 1.0 正式版。全面故障、移动/完整窄屏及活动跨进程恢复继续后置；系统 pytest Temp 权限及其他历史根因不关闭。后续仍按 [1.0 规划](PLAN-1.0.md) 与 [待办](BACKLOG.md) 执行。

此前 `REPLACE-*` 切片记录的是当时方案与证据，旧数据保留/引用回滚条款已被最新关联数据整组删除授权替代，不应当作当前 API 文档；固定基线不倒改。本期 0.2.0 收口不外推为全面交互、外部生产或 1.0 正式发布完成。

安装、测试和调试分别见 [快速启动](QUICKSTART.md)、[测试说明](TESTING.md)、[调试说明](DEBUGGING.md)。来源边界见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
