# OpenAgent 工作流前端

Vue 3、TypeScript、Pinia 和 Vue Flow 工作台，负责当前 Graph 的编辑、资源配置、会话操作和结果观察。定义、资源、运行事实与已接纳数据以 Python 后端为权威；浏览器草稿和观察缓存不是后端档案。

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

新安装建立空普通图。串行、精简或酒馆示例均只生成草稿，不配置密钥、不自动运行模型。先配置供应商、模型名称与容量，再保存并显式运行。

普通图保存后由工作台“打开聊天前端”携带准确的定义及可选会话身份；酒馆由“内容”中的专属入口打开。不带身份的聊天首页不会新建业务会话。入口仅向没有用户信息的 HTTP/HTTPS loopback 地址附带本地身份，外部地址不附带身份。旧 `VITE_LEGACY_UI_URL` 仅为地址别名，不恢复旧流程。

请求结果未知时使用“核实原请求”，保留 body/key，不生成新身份重发。编辑已有运行依据时可能产生独立副本，应确认当前图和会话，不把保存草稿等同于完成执行。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| `src/App.vue` | 导航、工作流列表、保存与聊天入口 |
| `src/components/GraphWorkbench.vue` | 画布、节点详情、绑定、结果与信息目录 |
| `src/domain/nodeCatalog.ts` | 现行家族、分类、协议显示策略 |
| `src/domain/nodeSelection.ts` | 节点选择与配置依据保护 |
| `src/components/NodeProfileDialog.vue` | 协议选择、契约预览与重置确认 |
| `src/components/ModelSourceFields.vue` | 供应商引用、模型、容量与 Gemini 思考配置 |
| `src/domain/serialAgentDemo.ts` | 原生上下文串行/精简示例 |
| `src/stores/workflowGraph.ts` | 图、会话、运行动作与原请求核实 |
| `src/application/` | UI 动作和应用边界 |
| `src/adapters/` | HTTP、浏览器持久化与聊天交接契约 |
| `src/plugins/` | 可信前端扩展，酒馆入口与全局资源 UI |
| `vite.config.ts` | loopback 开发代理和 Vitest 配置 |

## 数据与验收边界

明确含退役路线的浏览器图条目整组移除，不静默改版本、不重发旧请求；无关联现行图保留。未知格式或损坏记录仍阻止覆盖，提供“导出原记录”和“另建本机工作区”，见 [存档恢复](../docs/tavern/LOCAL-STORAGE-RECOVERY.md)。

最近记录为 67 文件、928 项前端测试通过及生产构建通过；真实 dist 的离线菜单浏览器场景通过。窄屏菜单/弹窗检查不等于整个工作台支持移动端，工作台保留 1024px 最小宽度；完整移动端、无限长会话或全面人工验收未承诺。跨前后端契约测试依赖同仓库 `../backend`。

来源和第三方许可见根目录 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
