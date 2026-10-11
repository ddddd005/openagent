# OpenAgent 工作流前端

Vue 3、TypeScript、Pinia 和 Vue Flow 工作台，负责当前 Graph 的编辑、资源配置、会话操作和结果观察。定义、资源、运行事实与已接纳数据以 Python 后端为权威；本机编辑草稿写入独立后端仓库，不是运行档案。

本次 P1-P6 已本地提交并快进合入 `main`，功能提交为 `0c15587`；本批未推送。BAT 未改，正式 `start.bat` 仍指向 `main`，已包含这些功能；真实用户启动与模型调用待验收。范围见 [P1/P2](../docs/FRONTEND-P1-P2-2026-10-09.md)、[P3/P4](../docs/FRONTEND-P3-P4-2026-10-09.md)和 [P5/P6 记录](../docs/FRONTEND-P5-P6-2026-10-09.md)。

当前默认目录有 44 个声明、41 个家族，按 9 个分类的二级菜单添加。单一现行路线直接添加；模型来源、模型调用与 Agent 选择 DeepSeek/Gemini 协议，不提供历史版本选择器。Gemini 思考摘要独立显示，协议签名不作为正文。

2026-10-10 的职责订正目前仅在 `develop`，未合入或推送 `main`；正式 BAT 不变。下面的模型配置/Key 流程与代码入口描述该开发版，验收见 [实施记录](../docs/MODEL-CONFIG-CORRECTION-2026-10-10.md)。

2026-10-11 首批开发版已接入薄侧栏及后端工作流/会话归档删除，并退出旧浏览器工作台恢复 UI。定义派生、自动后端保存及模型实时配置仍未完整实现，实际验证和清理限制见 [首批实施记录](../docs/中期检查/11-首批整改实施与离线验证-2026-10-11.md)；以下保留的手动应用/发布路径不代表新规则已完成。

完整启动见 [快速启动](../docs/QUICKSTART.md)，界面操作见 [使用手册](../docs/USER-GUIDE.md)，分层见 [开发说明](../docs/DEVELOPMENT.md)。

## Windows 入口

仓库根目录 `start.bat` 启动干净的本地 `main` 工作区，默认前端 `5178`、后端 `8765`；`start-dev.bat` 要求当前工作区为 `develop`，默认前端 `5179`、后端 `8766`。二者数据库与浏览器 origin 隔离，均使用可见控制台。

启动器读取所选分支当前源码，显式同步 `/api` 代理与聊天链接，不使用旧 `step1` 或固定安装快照；端口占用时拒绝，不结束旧进程、不自动拉取代码。依赖必须安装在实际选中的工作区，不能依赖原开发机的忽略目录。

## 手动开发

推荐 Node.js 24.x，版本要求见快速启动。在本目录安装并运行：

```powershell
npm ci
$env:OPENAGENT_API_PORT = "8766"
$env:VITE_CHAT_UI_URL = "http://127.0.0.1:8766/"
npm run dev -- --port 5179 --strictPort
```

这组配置对应开发后端 `8766`；须另行启动同份源码的后端。未设置环境变量时，Vite 默认端口 `5178`，代理后端 `8765`，可自动尝试其他前端端口；正式验收推荐 BAT 或显式 `--strictPort`。

`OPENAGENT_API_PORT` 只允许 `1024..65535` 的本机 loopback 端口。代理只接受本机 Host 与同源浏览器请求。`VITE_CHAT_UI_URL` 在启动/构建时决定聊天链接，不改变代理目标；密钥不能放进 `VITE_*` 变量。

```powershell
npm test
npm run build
npm run preview
```

`build` 包含 TypeScript 检查。`preview` 仅预览构建产物，不带开发 `/api` 代理；完整同源入口由后端 `--workbench-dist` 提供，构建需 `--base=/workbench/`，见快速启动。

## 聊天与工作流

新安装建立空普通图。示例只生成草稿，不自动运行模型。酒馆支持 Chat/Gemini 预置流，复用当前有效配置或明确保持待配置；串行示例仍须配置供应商、模型名称与容量，再保存并显式运行。

普通图保存后由工作台“打开聊天前端”携带准确的定义及可选会话身份；酒馆由 `tavern.chat.presentation@1` 节点详情“打开酒馆前端”，绑定当前工作流和选中会话。工作流区“创建酒馆工作流”打开置顶的模板面板，创建后选中展示节点；无会话时显式创建精确保存修订，不发布或运行。不带身份的聊天首页不会新建业务会话。入口仅向没有用户信息的 HTTP/HTTPS loopback 地址附带本地身份，外部地址不附带身份。旧 `VITE_LEGACY_UI_URL` 仅为地址别名，不恢复旧流程。

请求结果未知时使用“核实原请求”，保留 body/key，不生成新身份重发。普通编辑保持工作流身份；保存只发布修订，会话采用修订须另行确认。只有明确复制才产生独立工作流，历史恢复不覆盖编辑区。

供应商先编辑草稿，只有一个“保存供应商”提交按钮，保存完整字段及填写的 Key；已有 Key 留空保留。私有 Key 只在输入组件内存中，同一草稿字段变化保留 Key，不以 Key 非空禁用字段；非秘密表单和 intent 先落盘。密码框及表单使用 `autocomplete="off"`，不标记为 `new-password`。凭证只保留页面入口，删除环境 Key 引用选择器；宿主不支持安全保护时明确提示不可用，不建议环境或明文回退。提交中或结果未知才锁定；未知结果只核实原操作、回执及当前完整配置。P01 的失败后单独补绑定入口已移除，部分失败不报配置保存成功、不重发密钥。已接纳表单依据先确认落盘，再释放原请求。无法核实的请求可以先归档、重读确认再解锁，不撤销远端效果；归档失败不清原记录。

本轮全部职责订正完成后统一验收合并，真实验收用 DeepSeek，由用户启动页面、填写并保存 Key；当前开发版尚未进入正式 BAT，也未调用真实模型。

供应商与全局命名模型配置在同一入口，模型发现只在这里按已保存供应商/协议/序号查询，不从浏览器带 Key 直调上游，也不自动填写容量。工作流仅保存全局配置引用，新运行自动采用生效值。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| `src/App.vue`, `src/components/WorkbenchSidebar.vue` | 应用壳、侧栏布局/导航和业务组件承载 |
| `src/components/WorkflowSidebar.vue` | 工作流目录及目标归档/删除操作 |
| `src/components/GraphWorkbench.vue` | 画布、节点详情、绑定、结果与信息目录 |
| `src/domain/nodeCatalog.ts` | 现行家族、分类、协议显示策略 |
| `src/domain/nodeSelection.ts` | 节点选择与配置依据保护 |
| `src/domain/graphConnections.ts` | 画布/store 共用连接拒绝检查，不覆盖旧线 |
| `src/components/NodeProfileDialog.vue` | 协议选择、契约预览与重置确认 |
| `src/components/CurrentModelProfilesPanel.vue`, `src/application/modelProfiles.ts` | 全局命名模型、温度/思考、手填容量及原请求恢复 |
| `src/components/ModelSourceFields.vue` | 仅选择全局模型配置引用 |
| `src/application/providerCredentialEditing.ts`, `src/adapters/providerConfigurationApi.ts` | 私有 Key 生命周期、非秘密 intent、严校回执与模型列表 |
| `src/domain/serialAgentDemo.ts` | 原生上下文串行/精简示例 |
| `src/stores/workflowGraph.ts` | 图、会话、运行动作与原请求核实 |
| `src/application/` | UI 动作和应用边界 |
| `src/adapters/` | HTTP、异步本机草稿、原请求保管与聊天交接；不再导入旧浏览器工作台 |
| `src/stores/workbenchLegacyIsolation.test.ts` | 核对旧浏览器字节不读取、不导入、不覆盖，现行后端工作台可重读 |
| `src/stores/formDrafts.ts`, `src/composables/useBoundFormDraft.ts` | 未应用表单、原文、依据冲突及切换前落盘 |
| `src/components/PromptMembersEditor.vue`, `src/components/PromptGroupFields.vue` | 全局/图内提示词共享条目编辑，保留身份和原始数字 |
| `src/adapters/graphInputDeclarationsApi.ts`, `src/components/GraphRunControls.vue` | 管理只读输入声明，TEXT 表单和高级 JSON |
| `src/domain/resultPresentation.ts`, `src/components/GraphResult.vue`, `src/components/GraphHistory.vue` | 严格 kind/schema 正文展示及冻结定义历史 |
| `src/stores/managementLabels.ts`, `src/components/ManagementLabelFields.vue` | 独立本机名称和目录状态，不改变资源或运行事实 |
| `src/plugins/` | 可信前端扩展，酒馆入口与全局资源 UI |
| `vite.config.ts` | loopback 开发代理和 Vitest 配置 |

## 数据与验收边界

图、表单和聊天输入使用本机草稿 API，200ms 合并普通编辑；未知草稿写入只核实原回执，CAS 冲突不覆盖其他页面，尚在内存的内容不报已保存。开发版不再读取、自动归档或恢复旧浏览器工作台，也没有“旧版存档”入口。旧资源请求只读核实回执，不恢复表单、不导入 intent、不重放请求；原浏览器证据键未清除，实际清理仍待执行。聊天消费端旧请求导入尚未退出，不将 R12 整体记为完成。草稿依据按 JSON 内容比较，字段排序不再产生假冲突，数组顺序和真实配置变化仍严格检查。

本次 P1/P2 开发源码为 80 文件、1023 项前端测试及生产构建通过；真实 dist 的离线草稿与菜单浏览器场景通过，详见开发记录。稳定主干此前的 67 文件、928 项记录不累加为本次结果。窄屏菜单/弹窗检查不等于整个工作台支持移动端，工作台保留 1024px 最小宽度；完整移动端、无限长会话或全面人工验收未承诺。跨前后端契约测试依赖同仓库 `../backend`。

P3/P4 的当期结果见独立历史记录，不把先前批次数字相加；其旧浏览器夹具随本轮配置路线退出。当前供应商/模型配置浏览器入口为 `../backend/tests/test_model_configuration_browser.py` 及 `model_configuration_browser.cjs`，拦截全部请求和资产，不启动产品服务，不调用真实模型。实际覆盖与结果见本轮实施记录，不把新脚本当成旧批次全部场景的复验。

P5/P6 已通过前端全量 87 文件/1075 项、类型和生产构建。`../backend/tests/frontend_p56_browser.cjs` 覆盖 12 个离线浏览器场景，包含真实 Handle 拖动、已有数据/控制线绘制、半输入数字恢复、冻结标题、签名隐藏和可逆归档；不监听端口或访问产品库。正文和常用配置优先，运行设置/技术详情折叠，本机名称及归档使用原管理草稿 CAS。具体证据和未执行项见 P5/P6 记录。

来源和第三方许可见根目录 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
