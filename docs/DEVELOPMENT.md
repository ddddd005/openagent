# 开发说明

本版使用 Vue 3、TypeScript、Pinia、Vue Flow 工作台，以及 Python `phase1_agent` 包和 SQLite 持久化。包名沿用开发版本，不等于项目永久限定为第一阶段或固定 A/B。

## 代码入口

下表中的 Python 文件均在 `backend/src/phase1_agent/` 下。

| 修改范围 | 入口 |
| --- | --- |
| 服务启动与 HTTP 适配 | `server.py`、`graph_http.py` |
| 统一应用命令与查询 | `graph_application.py`、`graph_application_contracts.py` |
| 图服务与编译/执行 | `graph_service.py`、`graph_contracts.py`、`graph_execution.py`、`graph_runtime_host.py` |
| 公共 Runtime 与控制 | `runtime.py`、`runtime_hosting.py`、`runtime_control.py`、`runtime_executor_contracts.py` |
| 能力声明与注册 | `host_sdk.py`、`capability_packages.py`、`builtin_packages.py` |
| 普通内容、提示词与模型节点 | `tool_package.py`、`prompt_package.py`、`model_package.py` |
| Agent 执行与失败分类 | `agent_package.py`、`agent_executor.py`、`kernel.py`、`agent_failed_retry.py` |
| 模型调用 | `model_service.py`、`model_host_service.py`、`adapter.py` |
| 上下文读、装配、写回 | `context_package.py`、`context_v3_nodes.py`、`context_v3.py` |
| 前端业务节点 | `frontend_package.py`、`frontend_business.py` |
| 定义、会话、对象与运行存储 | `storage.py`、`graph_store.py`、`graph_records.py`、`session_objects.py`、`runtime_fact_store.py` |
| 冻结旧 Agent 证据的纯校验 | `graph_agent_contracts.py`；旧执行器保留同名导出，不参与历史读取 |
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

通用 Runtime 协调节点执行和接纳，不解释 Agent 私有工具语义。模型服务持有真实凭据，节点配置只保存 `env:DEEPSEEK_API_KEY` 引用。信息目录持有声明及绑定坐标，实际正文由登记的信息源或存储读取器提供。

## 修改规则

- 先确认操作针对定义、副本、会话还是原运行。复制和检查点分叉应重映射实际身份，不复用私有节点归属。
- 命令沿统一应用边界提交，保持 request body、idempotency key、修订/CAS 依据和 owner 校验一致。请求结果未知时只核实原 body/key；后续拒绝不自动证明原请求未发生。
- 节点声明类型、版本、配置 schema、端口和执行器；端口数据必须满足内容契约，不以普通字符串代替带版本的结构。
- 新节点的专有逻辑留在能力包。Agent 的确定失败判断在 `agent_failed_retry.py`，公共 `graph_failed_retry.py` 只协调合法的新尝试。
- 单节点成功、HTTP 200、可读输出、正式交付和整图完成分别处理。不要让模型结果直接绕过接纳与检查点。
- 未知外部效果不能凭空判成失败并重试；成功结果接纳失败不能重新调用模型。
- 活动运行继续依赖同进程现场及冻结服务。持久化历史可读不授予跨进程 `resume` 或失败重试资格。
- 前端专用配置通过确切包、扩展、版本与槽位声明加载；通用编辑器不按模型业务字段硬编码。

## 添加普通处理节点

先阅读 `tool_package.py` 的纯文本处理实现和 `tests/test_tool_package.py`。一个最小节点需要配置 schema、输入/输出端口、类型/版本和执行函数，再通过包注册进入目录。纯处理执行器应只生成合法输出；需要对象读写时通过执行上下文及显式绑定声明，不直接操作 SQLite 或其他节点私有状态。

验证至少覆盖正常输出、错误类型/配置、目录与编译、实例间隔离。需要界面专用编辑器时再接可信本地扩展，不先修改通用调度器。本版不提供完整公共插件 SDK、远程不可信插件隔离或插件市场。

## 新旧路径

`workflow.py`、`workflow_control.py` 等保留旧固定流程兼容；新版普通图以 `graph_*`、公共 Runtime 与能力包为主。新增工作流能力应先选择新版路径，不因已有旧 UI 或 `--mode deepseek` 就接回固定 A/B。不要在本次 demo 发布中顺带删除全部兼容层。

无正式入口调用者的早期 mock 原型已退役，旧 Agent 历史读取改用纯契约校验，见 [历史校验与原型退役](REPLACE-HISTORY-RETIREMENT.md)。正式兼容客户端、旧宿主及默认共享 prepared-context 导入链仍待退出；不能从局部退役推断所有兼容实现已删除，也不能改写原型旧存储键或既存档案。

下一步支持边界见 [联合处置矩阵](REPLACE-SUPPORT-MATRIX.md)：当前独立包及精确可信扩展保留，旧固定流程/compat 图以整份冻结只读为目标；判定包含完整包锁，不按前缀或版本数字直接删节点。冻结、历史/当前执行配置、纯 archive/receipt-only 和前端 passthrough 尚待实现，当前 start 仍调用旧 GraphAgentHost 资源预检。先迁出公共依赖与纯读取，再退役正式旧入口，不清旧 active/status/facts，也不靠重 POST 核实退役操作。

安装、测试和调试分别见 [快速启动](QUICKSTART.md)、[测试说明](TESTING.md)、[调试说明](DEBUGGING.md)。来源边界见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
