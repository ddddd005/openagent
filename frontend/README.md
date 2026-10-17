# OpenAgent 工作流前端

Vue 3、TypeScript、Pinia 和 Vue Flow 工作台，负责当前 Graph 的编辑、资源配置、会话操作和结果观察。定义、资源、运行事实与已接纳数据以 Python 后端为权威。

## 当前状态与导航

正常开发使用 `develop`。2026-10-16 的[59号回执](../docs/归档/2026-10-17/中期检查2/59-新架构本地晋升develop与main实施回执-2026-10-16.md)记录本地分支接续，不表示现有稳定服务已部署或存在独立 stable-main 工作区；`start.bat`仍要求唯一、干净的`main`工作区。不要从旧交接记录推断今天的服务或数据库状态，也不要把历史推送、晋升授权当成本轮授权。

旧 GraphChat/Tavern 创建、聊天页面、预设及自动接线入口已退役；Lorebook 和有效的显式前端节点能力仍保留，详见[49号](../docs/归档/2026-10-17/中期检查2/49-阶段E旧入口退役与测试工作流清理实施回执-2026-10-15.md)及[57号](../docs/归档/2026-10-17/中期检查2/57-D-E阶段旧内容退役与收口实施回执-2026-10-15.md)。当前逻辑聊天、候选选择、重roll及楼层分叉由`frontend.node@2`路径承载，不是旧 Tavern 页面，范围见[53号](../docs/归档/2026-10-17/中期检查2/53-工作流独立操作与节点入口补齐实施回执-2026-10-15.md)和[55号](../docs/归档/2026-10-17/中期检查2/55-聊天候选数据层与重roll检查点接续实施回执-2026-10-15.md)。

当前旧债状态和测试边界见[旧债清理入口](../docs/开发新基线-2026-10-16/旧债清理/README.md)与[09号实施总账](../docs/开发新基线-2026-10-16/旧债清理/09-测试旧债实施与验证总账-2026-10-17.md)；不把局部历史通过数解释为当前全量通过。2026-10-16 的[64号回执](../docs/归档/2026-10-17/中期检查2/64-Agent内置final_answer工具声明与两轮真实调用验收回执-2026-10-16.md)记录两轮用户确认的真实模型调用，仅适用于其记载范围，不构成整体验收。[65](../docs/中期检查2/65-工作流架构阶段总规划-2026-10-17.md)、[66](../docs/中期检查2/66-工作流数据管理与定义域会话域契约-2026-10-17.md)、[67](../docs/中期检查2/67-工作流基础支持与配置管理交互契约-2026-10-17.md)、[68](../docs/中期检查2/68-工作流运行与能力调用契约-2026-10-17.md)是下一阶段规划/职责契约，尚非已安装实现。

下列日期段与测试数字保留各批历史状态，不代表本施工树已部署或旧入口仍可使用。

## 历史交付说明

以下2026-10-09至2026-10-11段落是当批状态；当前施工树不继续使用旧节点表或恢复旧聊天入口。

本次 P1-P6 已本地提交并快进合入 `main`，功能提交为 `0c15587`；本批未推送。BAT 未改，正式 `start.bat` 仍指向 `main`，已包含这些功能；真实用户启动与模型调用待验收。范围见 [P1/P2](../docs/归档/2026-10-17/FRONTEND-P1-P2-2026-10-09.md)、[P3/P4](../docs/归档/2026-10-17/FRONTEND-P3-P4-2026-10-09.md)和 [P5/P6 记录](../docs/归档/2026-10-17/FRONTEND-P5-P6-2026-10-09.md)。

当时默认目录有 44 个声明、41 个家族，按 9 个分类添加；该数量不作为当前施工目录的固定断言。现行菜单按准确已安装声明及家族策略添加，不提供旧版本兼容选择器；Gemini思考摘要独立显示，协议签名不作为正文。

2026-10-10 的职责订正目前仅在 `develop`，未合入或推送 `main`；正式 BAT 不变。下面的模型配置/Key 流程与代码入口描述该开发版，验收见 [实施记录](../docs/归档/2026-10-17/MODEL-CONFIG-CORRECTION-2026-10-10.md)。

2026-10-11 首批开发版已接入薄侧栏及后端工作流/会话归档删除，并退出旧浏览器工作台恢复 UI。定义派生、自动后端保存及模型实时配置仍未完整实现，实际验证和清理限制见 [首批实施记录](../docs/归档/2026-10-17/中期检查/11-首批整改实施与离线验证-2026-10-11.md)；以下保留的手动应用/发布路径不代表新规则已完成。

完整启动见 [快速启动](../docs/QUICKSTART.md)，界面操作见 [使用手册](../docs/USER-GUIDE.md)，分层见 [开发说明](../docs/DEVELOPMENT.md)。

## Windows 入口

仓库根目录 `start.bat` 启动干净的本地 `main` 工作区，默认前端 `5178`、后端 `8765`；`start-dev.bat` 要求当前工作区为 `develop`，默认前端 `5179`、后端 `8766`。二者数据库与浏览器 origin 隔离，均使用可见控制台。

2026-10-15 的[60号回执](../docs/归档/2026-10-17/中期检查2/60-旧稳定库退役与正常开发环境交接回执-2026-10-15.md)记录当时归档旧默认业务库、重建独立 v20 空库及停止 archived retirement 工作区服务；这些是交接时点事实，不表示当前数据库为空或服务已停止。正常开发使用 `develop`；`start.bat` 仍要求唯一、干净的 `main` 工作区。2026-10-17 只读盘点未发现 `.local/stable-main` 工作区，本轮不创建工作区、不绕过分支门禁，并保留归档 retirement 工作区原路径及锁。

启动器读取所选分支当前源码，显式同步 `/api` 代理，不使用旧 `step1` 或固定安装快照；本施工树不再消费旧全局聊天链接变量。端口占用时拒绝，不结束旧进程、不自动拉取代码。依赖必须安装在实际选中的工作区，不能依赖原开发机的忽略目录。

## 手动开发

推荐 Node.js 24.x，版本要求见快速启动。在本目录安装并运行：

```powershell
npm ci
$env:OPENAGENT_API_PORT = "8766"
npm run dev -- --port 5179 --strictPort
```

这组配置对应开发后端 `8766`；须另行启动同份源码的后端。未设置环境变量时，Vite 默认端口 `5178`，代理后端 `8765`，可自动尝试其他前端端口；正式验收推荐 BAT 或显式 `--strictPort`。

`OPENAGENT_API_PORT` 只允许 `1024..65535` 的本机 loopback 端口。代理只接受本机 Host 与同源浏览器请求。节点页面地址由对应后端节点的entry返回，不再配置全局聊天链接；密钥不能放进 `VITE_*` 变量。

```powershell
npm test
npm run build
npm run preview
```

`build` 包含 TypeScript 检查。`preview` 仅预览构建产物，不带开发 `/api` 代理；完整同源入口由后端 `--workbench-dist` 提供，构建需 `--base=/workbench/`，见快速启动。

## 聊天与工作流

新建只建立空普通工作流，不建立会话或运行模型。用户自行添加现行输入、Agent、上下文、变量、Lorebook及前端节点并编排连线；旧预设builder不自动迁移或替换已有图。

新建菜单使用`frontend.node@2`，节点详情中的打开按钮携带准确工作流、保存修订、节点及可空会话。后端核实后提供其独立页面；开窗不建会话、不启动，首次发送才经对应后端节点调用现有能力。前端只消费后端读取的展示快照，不能直接操作工作流或回传上下文覆盖。旧GraphChat/Tavern四个页面地址返回410。

`GraphChatDisplay.vue`是`workflow.chat-context@1`的只读对象renderer，与节点页使用同一后端数据，不是第二份候选权威。节点私有200ms输入更新时间不扩大到其他字段、节点或表单。

请求结果未知时使用“核实原请求”，保留 body/key，不生成新身份重发。无会话时配置原地保存；有有效会话时固定修改、增删节点须确认后派生无会话新工作流，可调修改原地保存。检查点分叉只初始化新会话，不覆盖原会话。

供应商先编辑草稿，只有一个“保存供应商”提交按钮，保存完整字段及填写的 Key；已有 Key 留空保留。私有 Key 只在输入组件内存中，同一草稿字段变化保留 Key，不以 Key 非空禁用字段；非秘密表单和 intent 先落盘。密码框及表单使用 `autocomplete="off"`，不标记为 `new-password`。凭证只保留页面入口，删除环境 Key 引用选择器；宿主不支持安全保护时明确提示不可用，不建议环境或明文回退。提交中或结果未知才锁定；未知结果只核实原操作、回执及当前完整配置。P01 的失败后单独补绑定入口已移除，部分失败不报配置保存成功、不重发密钥。已接纳表单依据先确认落盘，再释放原请求。无法核实的请求可以先归档、重读确认再解锁，不撤销远端效果；归档失败不清原记录。

2026-10-10 记录中的“真实模型验收待完成”是当时状态；后续 64 号回执记录了其范围内两轮用户确认的真实模型调用。两者都不构成当前服务状态核验或整个产品的全面验收。

供应商与全局命名模型配置在同一入口，模型发现只在这里按已保存供应商/协议/序号查询，不从浏览器带 Key 直调上游，也不自动填写容量。工作流仅保存全局配置引用，新运行自动采用生效值。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| `src/App.vue`, `src/components/WorkbenchSidebar.vue` | 应用壳、侧栏布局/导航和业务组件承载 |
| `src/components/WorkflowSidebar.vue` | 工作流目录及目标归档/删除操作 |
| `src/components/GraphWorkbench.vue` | 画布、节点详情、绑定、结果与信息目录 |
| `src/components/FrontendNodeFields.vue`, `src/application/frontendNodeEntry.ts` | 整体节点准确输入绑定、只读入口验证与打开；不创建或启动会话 |
| `src/components/GraphChatDisplay.vue`, `src/domain/chatContext.ts` | 按准确逻辑聊天类型读取楼层、候选及成功点，不回写聊天权威 |
| `src/domain/nodeCatalog.ts` | 现行家族、分类、协议显示策略 |
| `src/domain/nodeSelection.ts` | 节点选择与配置依据保护 |
| `src/domain/graphConnections.ts` | 画布/store 共用连接拒绝检查，不覆盖旧线 |
| `src/components/NodeProfileDialog.vue` | 协议选择、契约预览与重置确认 |
| `src/components/CurrentModelProfilesPanel.vue`, `src/application/modelProfiles.ts` | 全局命名模型、温度/思考、手填容量及原请求恢复 |
| `src/components/ModelSourceFields.vue` | 仅选择全局模型配置引用 |
| `src/application/providerCredentialEditing.ts`, `src/adapters/providerConfigurationApi.ts` | 私有 Key 生命周期、非秘密 intent、严校回执与模型列表 |
| `src/stores/workflowGraph.ts` | 图、会话、运行动作与原请求核实 |
| `src/application/` | UI 动作和应用边界 |
| `src/adapters/` | HTTP、原请求保管及有效本机元数据；不再导入旧浏览器工作台或打开旧聊天入口 |
| `src/stores/workbenchLegacyIsolation.test.ts` | 核对旧浏览器字节不读取、不导入、不覆盖，现行后端工作台可重读 |
| `src/composables/useVolatileFormState.ts` | 普通表单内存编辑、当前依据及明确提交；不持久化编辑草稿 |
| `src/components/PromptMembersEditor.vue`, `src/components/PromptGroupFields.vue` | 全局/图内提示词共享条目编辑，保留身份和原始数字 |
| `src/adapters/graphInputDeclarationsApi.ts`, `src/components/GraphRunControls.vue` | 管理只读输入声明，TEXT 表单和高级 JSON |
| `src/domain/resultPresentation.ts`, `src/components/GraphResult.vue`, `src/components/GraphHistory.vue` | 严格 kind/schema 正文展示及冻结定义历史 |
| `src/stores/managementLabels.ts`, `src/components/ManagementLabelFields.vue` | 独立本机名称和目录状态，不改变资源或运行事实 |
| `src/plugins/` | 可信前端扩展、节点字段与Lorebook等全局资源 UI |
| `vite.config.ts` | loopback 开发代理和 Vitest 配置 |

## 数据与验收边界

当前施工树的图配置通过正式配置应用保存，普通资源表单仅在内存编辑；管理操作和资源提交各自保管原请求证据，unknown 只读核实原回执。旧工作台记录仅投影待核实证据及选择偏好，不恢复编辑图或表单，不重放请求。`localDraftsApi`与`workbenchPersistence`仍有原请求和元数据，不能随旧业务整包删除；旧 consumer 草稿仅作存量只读。E5 回执记录仅删除两份准确指定的旧测试工作流，不代表浏览器旧 namespace 或新普通图已清理；边界见 49 号。

P1/P2 的 80 文件、1023 项前端测试与构建，以及旧稳定主干 67 文件、928 项，均为历史批次记录，不是当前套件结果，也不累加为总数。窄屏菜单/弹窗检查不等于整个工作台支持移动端；工作台保留 1024px 最小宽度，完整移动端、无限长会话或全面人工验收未承诺。跨前后端契约测试依赖同仓库 `../backend`；当前旧债状态见 09 号总账。

P3/P4 与供应商/模型配置浏览器场景的结果属于各自历史批次，数字不相加。`../backend/tests/test_model_configuration_browser.py` 和 `model_configuration_browser.cjs` 描述隔离浏览器测试入口，不是服务或真实模型验收授权；脚本只覆盖所记载场景，不替代旧批次整体复验。

P5/P6 的前端 87 文件/1075 项、类型检查和生产构建为历史验收结果，不是当前全量结果。`../backend/tests/frontend_p56_browser.cjs` 当时覆盖 12 个离线浏览器场景；具体证据与限制仍见 P5/P6 记录，不据此声称当前全面验收。

来源和第三方许可见根目录 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
