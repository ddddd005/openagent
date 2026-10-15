# 开发说明

本版使用 Vue 3、TypeScript、Pinia、Vue Flow 工作台，以及 Python `phase1_agent` 包和 SQLite 持久化。包名保留既有导入身份，不代表当前仍使用固定 A/B 宿主。

本文描述2026-10-15唯一施工树的工程边界。入口见[文档索引](README.md)，产品决定见[41号](中期检查/41-当前产品决定与实施边界-2026-10-13.md)，聊天/检查点安装见[55号](中期检查2/55-聊天候选数据层与重roll检查点接续实施回执-2026-10-15.md)，退役前范围见[56号](中期检查2/56-D-E阶段旧内容退役勘察与收口清单-2026-10-15.md)，实际删除及代码收口见[57号](中期检查2/57-D-E阶段旧内容退役与收口实施回执-2026-10-15.md)。当前限定回归和隔离构建已通过，服务未更新；历史记录不替代当前节点目录、部署状态或API。

2026-10-15唯一施工worktree的E5接续见[49号回执](中期检查2/49-阶段E旧入口退役与测试工作流清理实施回执-2026-10-15.md)：普通创建和节点所属前端保留，旧GraphChat/Tavern业务、预设和自动接线退出，Lorebook及原请求证据继续有效。该施工未晋升main、重启产品服务或进行真实模型验收；下文主干历史不作为恢复退役业务的要求。

历史交付：前端用户化 P1-P6 当时已本地提交并快进合入 `main`，功能提交为 `0c15587`；该批未推送。两个 BAT 均未改，正式入口仍选 `main`。合并后重新构建并通过 `-CheckOnly`，未启动产品服务、打开产品库或调用模型。范围见 [P1/P2](FRONTEND-P1-P2-2026-10-09.md)、[P3/P4](FRONTEND-P3-P4-2026-10-09.md)和 [P5/P6 记录](FRONTEND-P5-P6-2026-10-09.md)。

2026-10-11当批的未提交/未安装范围见[首批实施记录](中期检查/11-首批整改实施与离线验证-2026-10-11.md)，不作为当前阻塞。正式配置、确认派生、统一声明、上下文/变量闭环和独立工作流/聊天操作已分批接入；真实模型、完整搭图及多聊天项不同计算起点仍按相应验收关口处理。

## 分支与启动

唯一施工树为`openagent-retirement-2026-10-13`，授权分支为`retirement/architecture-2026-10-13`；原`openagent`只读，不在其中写入、测试或运行产品。旧`step1`及vendored第三方源码只作历史/来源参考。

| 分支 | 职责 |
| --- | --- |
| `main` | 当前产品主干；原测试版已晋升，旧正式源码仅归档参考 |
| `develop` | 基于同一主干继续开发及隔离验收，不维护第二套实现 |
| `retirement/architecture-2026-10-13` | 当前明确授权的独立施工worktree，不冒充develop或稳定main部署 |
| 临时功能分支 | 只在明确需要时用于授权并行工作，合回 `develop` 后再进入 `main` |

2026-10-09 上一次同步已正常推送 `main`；当时本地 `main`、`develop` 与远端 `main` 对齐到 `0c158e7`。这只是该次同步的身份，不是后续 HEAD 的固定名称。版本元数据仍为 `0.2.0`；`v0.2.0` 固定原 2026-10-07 基线，不移动旧标签冒充当前源码。`main-before-node-unification-2026-10-09` 是仅本地保存的旧版归档标签，未随 `main` 自动推送。

根 `start.bat` 通过 `scripts/start-branch-services.ps1` 选择唯一干净的 `main` 工作区；本机为 `.local/stable-main`。`start-dev.bat` 只接受入口工作区的 `develop`，不回退为其他源码。

| 入口 | 后端 / 工作台 | 默认数据库，相对于 BAT 所在目录 |
| --- | --- | --- |
| `start.bat` | `8765` / `5178` | `.local/dev/workflow.sqlite` |
| `start-dev.bat` | `8766` / `5179` | `.local/develop/workflow.sqlite` |

`.local/dev` 名称沿用原稳定库位置，并不表示开发 BAT 使用它。两入口使用可见控制台；不隐藏启动、不自动 `pull`、不切分支、不安装依赖、不杀占端口进程。分支不能隔离运行进程或数据；正在运行的旧服务不会因提交、合并或推送而换版。

新能力先明确范围及验收，在当前授权施工树实现。代码、构建、运行验收和部署分别记录；不因本地提交自动合并、推送、切换服务或迁移数据库。稳定main晋升和真实模型调用须另行授权，不能以清理旧测试替代验收。

核对远端不能只看可能过期的 `origin/main`。从仓库根目录执行：

```powershell
git status --short
git worktree list --porcelain
git rev-parse main develop
git ls-remote --heads origin main
```

完成远端同步时，本地 `main` 的完整 SHA 应与 `ls-remote` 返回的 `refs/heads/main` 一致；仅本地合并而未推送时 main 可合理领先，开发有未晋升提交时 `develop` 可合理领先。同步前可执行 `git fetch origin`，再检查 `git log --oneline --left-right main...origin/main`。在实际 `main` 工作区以 `git push origin main:main` 正常推送；拒绝时检查分叉，不用强推绕过。推送后重新核对远端 SHA 和工作区状态。

明确只推 `main`、不连带标签时使用 `git -c push.followTags=false push origin main:main`。归档标签需另行明确推送。Git 同步、发布标签、版本升级、安装/构建和服务部署是不同操作；日志、数据库、依赖、截图、`dist` 及本地归档不随代码推送。

## 代码入口

下表中的 Python 文件均在 `backend/src/phase1_agent/` 下。

| 修改范围 | 入口 |
| --- | --- |
| 服务启动与 HTTP | `server.py`、`graph_http.py` |
| 统一应用命令、查询及身份 | `graph_application.py`、`graph_application_contracts.py`、`graph_application_identity.py`、`graph_receipts.py` |
| 图服务、编译及执行 | `graph_service.py`、`graph_contracts.py`、`graph_execution.py`、`graph_runtime_host.py` |
| 管理输入声明 | `graph_input_declarations.py`；只读 `definition.inputs`，完整编译失败不丢可编辑声明 |
| 正式配置及合规修改 | `workflow_configuration.py`、`configuration_mutations.py`；固定/可调与确认派生，窄修改许可和通用提交分开 |
| 公共资源预检与当前内容读取 | `graph_resource_host.py`、`resource_contracts.py`、`prepared_request.py`；提示词/Lorebook使用时读取，模型每个新请求准备时读取 |
| 公共 Runtime 与控制 | `runtime.py`、`runtime_hosting.py`、`runtime_control.py`、`runtime_executor_contracts.py` |
| 能力声明、包选择与注册 | `host_sdk.py`、`capability_packages.py`、`builtin_packages.py`、`capability_registry.py`、`graph_package_selection.py` |
| 内容、提示词与模型节点 | `tool_package.py`、`prompt_package.py`、`model_package.py` |
| 原生 Agent、工具及精简 | `agent_package.py`、`agent_executor.py`、`context_compaction.py`、`context_compaction_policy.py` |
| 模型调用与受控失败 | `model_service.py`、`model_host_service.py`、`adapter.py`、`contract_errors.py` |
| Gemini 协议、思考及签名 | `gemini_adapter.py`、`gemini_capabilities.py`、`provider_metadata.py` |
| 原生上下文装配与写回 | `context_package.py`、`context_v4_nodes.py`、`context_v4.py`、`context_prompt_v6.py`、`context_receipt.py` |
| 前端节点与逻辑聊天 | `frontend_node_v2.py`、`frontend_node_channel.py`、`frontend_node_operations.py`、`chat_contracts.py` |
| 独立操作 | `workflow_operations.py`、`workflow_chat_operations.py`、`workflow_chat_service.py`；功能不绑定按钮/页面 |
| 检查点 | `checkpoint_collection.py`、`runtime_checkpoints.py`、`checkpoint_repository.py`、`checkpoint_loading.py` |
| Lorebook | `tavern_package.py`、`tavern/lorebook_resources.py`、`lorebook_nodes.py`、`lorebook_engine.py` |
| 定义、会话、对象、事实与目标生命周期 | `storage.py`、`graph_store.py`、`graph_records.py`、`session_objects.py`、`runtime_fact_store.py`、`workflow_lifecycle.py` |
| 独立管理组合与内容 | `workflow_host.py`、`content_configuration.py`；管理不先构造图执行宿主 |
| 旧证据读取 | `graph_drafts.py`、`graph_editing_host.py`；保留原请求及有限偏好，不恢复持久编辑草稿 |
| 私有凭据与模型发现 | `credential_store.py`、`graph_provider_host.py`、`provider_discovery.py`；管理专属、独立保护库、不走公开秘密 outbox |
| 信息源与事实读取 | `graph_information.py`、`runtime_information.py` |
| 节点私有客户端 | `static/frontend-node-client.js`、`static/frontend-node-entry.js`、`static/frontend-node-host.js` |

下表中的前端路径均在 `frontend/src/` 下。

| 修改范围 | 入口 |
| --- | --- |
| 应用与工作台 | `App.vue`、`components/WorkbenchSidebar.vue`、`components/WorkflowSidebar.vue`、`components/GraphWorkbench.vue` |
| 图契约及数据项登记 | `domain/workflowGraph.ts`、`stores/workflowGraph.ts`；旧预设builder已经退出 |
| 二级节点目录与协议选择 | `domain/nodeCatalog.ts`、`domain/nodeSelection.ts`、`components/NodeProfileDialog.vue` |
| 连线检查与反馈 | `domain/graphConnections.ts`；画布与 store 共用动态端口、schema、基数和组合环检查 |
| 图、会话与工作区状态 | `stores/workflowGraph.ts`、`stores/workspace.ts` |
| 内存表单、旧本机证据及原请求 | `composables/useVolatileFormState.ts`、`adapters/requestEvidenceApi.ts`；`adapters/localDraftsApi.ts`和`stores/workbenchPersistence.ts`保留混合用途证据读取。旧表单持久化壳已删除，见[38号回执](中期检查2/38-阶段D后旧架构代码整理实施回执-2026-10-15.md) |
| 应用命令、编辑及信息读取 | `application/workflowCommands.ts`、`application/workflowEditing.ts`、`application/workflowInformation.ts` |
| HTTP 适配 | `adapters/workflowApplicationApi.ts`、`adapters/workflowGraphApi.ts`、`adapters/workflowResourcesApi.ts` |
| 供应商与全局模型配置编辑 | `components/CurrentProviderPanel.vue`、`components/CurrentModelProfilesPanel.vue`、`application/modelProfiles.ts`、`adapters/workflowModelProfilesApi.ts` |
| 工作流模型引用 | `components/ModelSourceFields.vue`；只选择全局配置，不编辑参数、容量或发现型号 |
| 私有 Key 编辑和模型列表 | `components/ProviderCredentialFields.vue`、`application/providerCredentialEditing.ts`、`adapters/providerConfigurationApi.ts` |
| 提示词成员和运行输入 | `components/PromptMembersEditor.vue`、`components/PromptGroupFields.vue`、`adapters/graphInputDeclarationsApi.ts` |
| 正文、冻结历史与本机标签 | `components/GraphResult.vue`、`domain/resultPresentation.ts`、`components/GraphHistory.vue`、`stores/managementLabels.ts` |
| 可信本地扩展 | `plugins/workflowFrontendSdk.ts`、`plugins/modelFrontendPackage.ts`、`plugins/workflowFrontendPackage.ts` |
| 准确节点入口及聊天展示 | `application/frontendNodeEntry.ts`、`components/FrontendNodeFields.vue`、`components/GraphChatDisplay.vue`、`domain/chatContext.ts` |
| 观察与控制 | `components/GraphInformation.vue`、`components/GraphRunControls.vue`、`components/GraphHistory.vue` |

## 当前数据契约

二级菜单按准确已安装声明的家族策略添加，数量不固定为旧节点目录的44/41/9。协议是模型配置的一部分，不是旧节点版本选择器；不静默迁移已有图。

| 路线 | DeepSeek | Gemini |
| --- | --- | --- |
| 全局配置引用来源 | `models.source@7` / `MODEL_BINDING@5` | 同一准确声明，协议由全局配置决定 |
| 普通模型调用 | `models.chat@5` / `MODEL_RESULT@3` | 同一准确声明 |
| 原生 Agent | `agents.execute@10` / `PROMPT@6` | 同一准确声明 |

普通提示词材料及 `prompts.assembly` 使用节点版本 `@2`，装配输出已就绪 `PROMPT@2`。格式转换的普通提示词通过独立 `raw_prompt` 端口接入，保留真实产物引用。普通 Chat 消费该契约，不把普通提示词当作持久 Agent 上下文。

全局模型配置保存名称、协议、供应商引用、四参数、手填容量及启用状态。来源config仅`{reference}`，不内联Key或模型覆盖值。模型服务在每个新请求准备时读取当前已保存设置；已准备或已发送的请求保持原证据，不追改，不冻结整轮后续请求。工作流二次限额和摘要额外1024上限已经退出。历史模型/配置类型的读取校验不授予旧节点可执行资格。

`ProviderConfigurationService` 独立于图 mixin，可只初始化资源与私有凭据管理，不读取工作流历史。应用组合层显式委派配置操作，内容 CRUD 属于 `ContentConfigurationService`；供应商通信适配器仍仅承担协议收发。供应商先编辑草稿，仅通过“保存供应商”提交完整配置；输入 Key 时不锁定其他字段，同一归属和版本的草稿变化保留内存 Key。填写新 Key 时，`provider.credential.save` 在同一用户动作保存完整字段、Key 与生效引用；Key 留空通过资源保存保留既有密钥。P01 的 `provider.credential.bind` mutation/UI 已移除，仅保留旧请求只读回执，不自动修补部分失败。原身份包含操作名与完整非秘密参数；只读核实不重写 Key、供应商或请求。资源和 Key 收到回执后，先写入已确认表单状态，再释放原请求；失败保留原请求，归档确认后才允许解锁。实际范围见首批实施记录。

当前`context.output@5 / context.assembly@5 / context.merge@6`提供显式读取、装配和写回；`context_receipt.py`核对真实写入回执。消费者选择具体数据项，不绑定Agent内核；Agent只消费计算输入、产生结果和真实执行证据。`agents.compaction-policy@2`仍是可选能力，不恢复旧精简预设。

Gemini 普通响应及 `agent_message@5` 分离回答正文、可见 `thinking_summary` 和不透明 `provider_metadata`。原 Content/parts、签名、供应商调用 ID 与显式内核调用绑定经响应、运行事实、上下文、持久化重读进入下一次请求；不能按工具名合并调用、改写原签名 part 或重新生成签名。摘要不是完整内部思维链，签名不能作为正文展示。

工具定义转换、`functionCall/functionResponse` 继续复用公共工具执行及 `final_answer`，没有另建无工具路径或并行工具执行器。型号思考能力由 `gemini_capabilities.py` 的明确规则校验，未知能力派发前拒绝；不为绕过错误默改模式、预算、签名或工具 schema。

## 运行与权威

```text
正式工作流配置
  -> 保存配置及固定修订，必要时确认派生无会话新工作流
  -> 首次启动按需建立会话，既有会话保持固定修订
  -> run.start 校验身份、状态与冻结依据
  -> 编译激活节点和依赖，准备宿主服务
  -> Runtime 逐节点执行
  -> 校验并接纳输出、事实及声明的对象写入
  -> 全部激活节点成功接纳
  -> 成功结算规定白名单检查点
  -> 后端读取已提交业务数据并提供临时展示快照
```

读取、计算和更新顺序由用户通过实际数据边及控制边编排，不按共享项自动补先序。runtime等待全部纳入计划的节点完成，输出节点不直接宣告回合成功。节点接纳不是整图事务：已提交前缀可在下游失败后保留；聊天pending候选不会因此伪绑成功点。成功点绑定与该次最终提交使用同一事务。

| 对象 | 权威 |
| --- | --- |
| 统一定义模块 | 节点/类型声明、工作分类和检查点准入，不维护业务当前值 |
| 工作流配置及修订 | 节点实例、配置策略、端口、包版本与对象绑定 |
| 工作流会话 | 动态对象、历史及活动链关联，可执行多轮 |
| chain_run / node_run | 原运行冻结依据、尝试、状态、产物和事实；新草稿不能改写旧运行 |
| 全局当前资源 | 各业务独立管理；预检核对身份，提示词/世界书使用时读取，模型按实际新请求准备读取 |
| 会话对象与上下文 | 显式读写权限、接纳及 CAS 管理 |
| 逻辑聊天及变量项 | 数据层中的准确独立项及会话当前值，检查点按白名单保存 |
| 前端内存与证据 | 临时编辑/展示副本和完整原请求保管，不替代后端数据或提供直接工作流权限 |

本批 `develop` 只保留页面输入 Key、私有保护库和 `local:<UUID>` 引用路线，删除环境 broker、环境状态查询分支及手工引用选择器。模型服务必须显式注入私有 broker，不默认读取环境变量；每次运行冻结凭据代次，同 Key 重存也会使原 lease 失效。当前 provider 仅 schema 3，不保留旧格式或环境引用兼容。Key 只进独立当前用户保护库，普通草稿、资源、事实及公开身份存档不持有正文；查询原凭据回执不需要 Key。宿主不支持保护时明确拒绝，不降级明文。通用 Runtime 不解释 Agent 私有工具语义，也不直接暴露供应商凭据。

正式配置提交不要求图可执行，不保留独立持久编辑草稿。有效会话下固定修改须确认派生，无会话时原地保存；历史分叉按保存状态初始化新目标，不重绑原会话。模型列表按已保存供应商身份和序号查询，派发前后检查资源和凭据，未知Gemini不自动授予能力。

P5/P6 显示层按精确 kind/schema 读取正文，签名仍经既有过滤器隐藏而不修改 facts。历史节点标题通过该次 run 的 `definition.read(identity, revision)` 获得，不回退到当前编辑图。本机名称仍是管理表单 metadata；开发版归档改由后端停止所属运行并保留只读数据，不恢复执行。删除只清目标所属数据，保留全局资源、Key 及原请求证据。

输入声明将全部可编辑字段与当前运行必填集合分开，节点逐个读取后再尝试编译；失败返回诊断与`required_for_run: null`。节点私有页面仅向对应节点交输入和意图；UI折叠不改变输出/对象权限，不稳定会话限制仍由后端校验。

## 修改规则

- 先辨明操作目标是定义、副本、会话还是原运行；复制和分叉必须重映射实际身份及归属。
- 命令统一经过应用边界，保持 body、idempotency key、修订/CAS 和 owner 一致。结果未知时按原 operation/parameters 与作用域读取回执，不自动重发 mutation。
- 回执读取由 `server.py` 在构造应用服务前截获，使用 raw SQLite 只读入口。management 与 consumer 作用域不能混用；缺原身份或证据不匹配时保持 unresolved。
- 定义节点时提供确切类型/版本、schema、端口和执行器，不用普通字符串替代带版本结构；共用类型在原版本下不可悄悄重定义。
- 专有行为放入能力包。公共`graph_failed_retry.py`只协调已注册的失败策略；当前`agents.execute@10`未注册失败节点重试策略，不能套用旧摘要路线的重试结论。
- 原结果成功但接纳失败时，仅在原现场与 `available_actions` 允许下 `retry_acceptance`，不重新调用模型、执行工具或重放外部效果。
- 受控 `ModelRequestError` 区分 `model_provider_error`、`model_dispatch_unknown`、`model_not_dispatched`、`model_response_invalid`；表层 HTTP 429/503 或未派发诊断本身不授予重试许可，也不进入自动 transport retry。
- 暂停/恢复依赖同进程现场及冻结服务。历史可冷重读不代表活动内核可跨进程恢复；进程重启后不得凭持久消息自动重建执行或工具队列。
- 前端专用编辑器经确切包、版本、扩展与槽位声明加载，不在通用调度器中硬编码模型业务。

添加普通处理节点先参考 `tool_package.py` 与 `backend/tests/test_tool_package.py`。至少覆盖正常输出、非法类型/配置、目录/编译、实例隔离及显式对象权限。本版不承诺完整公共插件 SDK、远程不可信插件隔离或插件市场。

## 数据退役

当前`STORAGE_VERSION = 20`；图文档schema3、检查点schema4、节点版本和包版本分别校验，不混为一个软件版本。正常初始化不扫描或清理旧产品内容。旧`storage_retirement.py`只保留不可执行[源码参考](中期检查/reference/storage_retirement.py.txt)。显式开发reset或正式目标删除都须核对授权路径/身份，保留原请求证据及实际清理回执；不自动升级旧schema3检查点库。

旧浏览器工作台读取/导入/恢复UI及直接consumer业务已退出。存量原请求只核实，不自动导入新节点或重新派发，不清除证据键。纯文本旧聊天源码和许可仍保留，不等于保留可执行入口；退出路径不等于清空浏览器namespace。

已授权的旧开发产品数据及代理临时数据按确切路径/身份清理；临时删除被平台拒绝须登记失败，不绕过拒绝，不清理稳定库、私有 Key、未知请求证据或用户新架构验收内容。日常全局编辑不触发删图。表单依据按规范化 JSON 内容比较，对象字段顺序不是配置变化；数组顺序、数据类型与真实字段变化仍影响依据。

旧固定宿主、兼容工厂、旧上下文/Agent 路线和专用测试已删除。当前包选择为 `current-execution`，没有旧运行 fallback 或旧存储 API 门面。存储版本、数据 schema、节点版本、能力包版本和应用 `0.2.0` 是不同契约，不应混称“软件版本”。

## 验收与历史

当前主干晋升复核为前端 67 文件 / 928 项、类型/构建、后端定向 13 文件 / 239 项通过。此前节点整理后端全量为 3008 通过 / 50 既有失败 / 9 跳过；剩余失败有旧稳定基线对照，不声明全量绿色。全量后最后修改另有 191 项补测，批次范围不累加。

工程检查、运行验收和部署状态分别记录。当前代码仍有 >500 kB bundle 提示、通用后端测试债务及活动跨进程恢复边界；历史 wheel、旧常驻、收费模型或截图不自动验证当前主干。

- [主干晋升](MAIN-PROMOTION-2026-10-09.md)：产品提交、归档、BAT 预检及本机复核。
- [节点目录](NODE-DIRECTORY-2026-10-09.md)：现行声明、整图清理、全量失败对照和补测。
- [Gemini 实现](GEMINI-ACCEPTANCE-2026-10-09.md)：协议/签名专项，节点版本表保留当时实现阶段。
- [工作区梳理](WORKTREE-ACCEPTANCE-2026-10-09.md)：先前状态与 Git 收口。
- [0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)、[旧架构移除](REPLACE-REMOVAL.md)：固定历史身份与当时部署证据，不倒改原通过数或哈希。

安装、测试和调试分别见 [快速启动](QUICKSTART.md)、[测试说明](TESTING.md)、[调试说明](DEBUGGING.md)。来源边界见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
