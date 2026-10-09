# OpenAgent 工作流前端

Vue 3、TypeScript、Pinia 和 Vue Flow 工作台，负责当前 Graph 的编辑、资源配置、会话操作和结果观察。定义、资源、运行事实与已接纳数据以 Python 后端为权威；本机编辑草稿写入独立后端仓库，不是运行档案。

本次 P1-P6 已本地提交并快进合入 `main`，功能提交为 `0c15587`；本批未推送。BAT 未改，正式 `start.bat` 仍指向 `main`，已包含这些功能；真实用户启动与模型调用待验收。范围见 [P1/P2](../docs/FRONTEND-P1-P2-2026-10-09.md)、[P3/P4](../docs/FRONTEND-P3-P4-2026-10-09.md)和 [P5/P6 记录](../docs/FRONTEND-P5-P6-2026-10-09.md)。

当前默认目录有 44 个声明、41 个家族，按 9 个分类的二级菜单添加。单一现行路线直接添加；模型来源、模型调用与 Agent 选择 DeepSeek/Gemini 协议，不提供历史版本选择器。Gemini 思考摘要独立显示，协议签名不作为正文。

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

私有 Key 只在密码组件内存中，非秘密 intent 先落盘；未知结果只查询原私有回执并重读当前代次。Key 保存和供应商绑定分开，不用普通资源 outbox 保管秘密。模型发现经已保存供应商、协议和序号查询，不直接从浏览器带 Key 调上游。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| `src/App.vue` | 导航、工作流列表、保存与聊天入口 |
| `src/components/GraphWorkbench.vue` | 画布、节点详情、绑定、结果与信息目录 |
| `src/domain/nodeCatalog.ts` | 现行家族、分类、协议显示策略 |
| `src/domain/nodeSelection.ts` | 节点选择与配置依据保护 |
| `src/domain/graphConnections.ts` | 画布/store 共用连接拒绝检查，不覆盖旧线 |
| `src/components/NodeProfileDialog.vue` | 协议选择、契约预览与重置确认 |
| `src/components/ModelSourceFields.vue` | 供应商引用、模型、容量与 Gemini 思考配置 |
| `src/application/providerCredentialEditing.ts`, `src/adapters/providerConfigurationApi.ts` | 私有 Key 生命周期、非秘密 intent、严校回执与模型列表 |
| `src/domain/serialAgentDemo.ts` | 原生上下文串行/精简示例 |
| `src/stores/workflowGraph.ts` | 图、会话、运行动作与原请求核实 |
| `src/application/` | UI 动作和应用边界 |
| `src/adapters/` | HTTP、异步本机草稿、原请求保管、浏览器旧工作台原文归档与聊天交接 |
| `src/adapters/browserWorkbenchArchives.ts`, `src/components/BrowserWorkbenchArchives.vue` | 内容寻址旧存档、确认后清理旧命名空间及校验导出 |
| `src/stores/formDrafts.ts`, `src/composables/useBoundFormDraft.ts` | 未应用表单、原文、依据冲突及切换前落盘 |
| `src/components/PromptMembersEditor.vue`, `src/components/PromptGroupFields.vue` | 全局/图内提示词共享条目编辑，保留身份和原始数字 |
| `src/adapters/graphInputDeclarationsApi.ts`, `src/components/GraphRunControls.vue` | 管理只读输入声明，TEXT 表单和高级 JSON |
| `src/domain/resultPresentation.ts`, `src/components/GraphResult.vue`, `src/components/GraphHistory.vue` | 严格 kind/schema 正文展示及冻结定义历史 |
| `src/stores/managementLabels.ts`, `src/components/ManagementLabelFields.vue` | 独立本机名称和目录状态，不改变资源或运行事实 |
| `src/plugins/` | 可信前端扩展，酒馆入口与全局资源 UI |
| `vite.config.ts` | loopback 开发代理和 Vitest 配置 |

## 数据与验收边界

图、表单和聊天输入使用本机草稿 API，200ms 合并普通编辑，业务提交和应用内切换前等待确认。未知草稿写入只核实原回执，CAS 冲突不覆盖其他页面；尚在内存的内容不报已保存。两套旧浏览器工作台先按原文与 SHA-256 存入管理端独立归档记录，确认后才清理旧命名空间；未知格式不自动导入，损坏原文也可从“旧版存档”导出。归档失败不禁用当前工作台。草稿依据按 JSON 内容比较，字段排序不再产生假冲突，数组顺序和真实配置变化仍严格检查。

本次 P1/P2 开发源码为 80 文件、1023 项前端测试及生产构建通过；真实 dist 的离线草稿与菜单浏览器场景通过，详见开发记录。稳定主干此前的 67 文件、928 项记录不累加为本次结果。窄屏菜单/弹窗检查不等于整个工作台支持移动端，工作台保留 1024px 最小宽度；完整移动端、无限长会话或全面人工验收未承诺。跨前后端契约测试依赖同仓库 `../backend`。

P3/P4 的最新定向结果见单独开发记录，不把先前批次数字相加。离线浏览器脚本 `../backend/tests/frontend_p34_browser.cjs` 拦截全部请求和资产，不启动产品服务，不调用真实模型。

P5/P6 已通过前端全量 87 文件/1075 项、类型和生产构建。`../backend/tests/frontend_p56_browser.cjs` 覆盖 12 个离线浏览器场景，包含真实 Handle 拖动、已有数据/控制线绘制、半输入数字恢复、冻结标题、签名隐藏和可逆归档；不监听端口或访问产品库。正文和常用配置优先，运行设置/技术详情折叠，本机名称及归档使用原管理草稿 CAS。具体证据和未执行项见 P5/P6 记录。

来源和第三方许可见根目录 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
