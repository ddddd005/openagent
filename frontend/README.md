# OpenAgent 工作流前端

基于 Vue 3、TypeScript、Pinia 和 Vue Flow 的工作流工作台。本次 demo 以普通节点工作流和串行 Agent A -> B 为主要入口；模型资源、工作流定义、会话、运行和归档由后端负责保存与执行。

完整启动步骤见 [快速启动](../docs/QUICKSTART.md)，代码分层与数据流见 [开发说明](../docs/DEVELOPMENT.md)。

## 本地开发

推荐 Node.js 24.x；具体构建和测试工具的版本要求见快速启动。在本目录运行：

```bash
npm ci
npm run dev
```

开发服务默认监听 `http://127.0.0.1:5178/`；端口被占用时以 Vite 输出为准。同源 `/api` 请求代理到 `http://127.0.0.1:8765`，因此需要另行启动 `../backend` 中的后端。代理只接受本机 loopback Host 与同源浏览器请求。

创建串行 Agent 示例后，先选择已保存的模型资源和模型名，保存工作流，再创建会话并打开聊天前端。示例不会自行配置凭据或自动调用模型。

```bash
npm run build
npm test
npm run preview
```

`build` 包含 TypeScript 检查。`preview` 只预览构建产物，不包含开发服务器的 `/api` 代理；部署构建产物时需自行提供同源后端代理。

## 聊天入口

默认聊天入口为 `http://127.0.0.1:8765/`。如需改变入口，可参考 `.env.example`，在本目录 `.env.local` 设置：

```dotenv
VITE_CHAT_UI_URL=http://127.0.0.1:8765/
```

修改后重启开发服务或重新构建。该设置不改变 `/api` 代理目标。Graph 入口只向不含用户信息的 HTTP/HTTPS loopback 地址附带工作流或会话身份，支持配置端口和 IPv6；外部地址不附带本地身份。旧环境变量 `VITE_LEGACY_UI_URL` 仅作为入口地址别名继续识别，不启用旧流程。模型密钥由后端凭据配置管理，不应放进 `VITE_*` 环境变量或浏览器草稿。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| `src/App.vue` | 工作台导航、工作流列表与入口 |
| `src/components/GraphWorkbench.vue` | 普通图编辑、配置、会话与运行观察 |
| `src/domain/serialAgentDemo.ts` | 串行示例图定义 |
| `src/domain/` | 图、端口、模型资源及界面领域类型 |
| `src/stores/workflowGraph.ts` | 普通图状态、会话动作、运行观察及未知请求核实 |
| `src/application/` | 界面动作与应用边界 |
| `src/adapters/` | HTTP、本机草稿与聊天消费契约 |
| `src/plugins/` | 前端扩展声明和包入口 |
| `vite.config.ts` | 开发代理和 Vitest 配置 |

浏览器草稿和观察缓存不是后端权威档案；打开页面或恢复草稿不代表执行工作流。请求结果未知时须核实原请求，不应重新生成身份并重复提交。

## Demo 边界

工作台只保留当前 Graph 架构。旧固定 A/B 的界面、stores、客户端、提示词编译器与迁移入口已删除；浏览器保存遇到明确的旧兼容行时会移除这些测试数据，当前 Graph 文档、会话归属与待核实请求保持不变。当前版本不是生产部署方案，也不宣称完整浏览器人工验收、长会话稳定性或移动端适配已经完成。测试中的模拟数据仅用于离线验证；跨前后端契约测试依赖同仓库的 `../backend`。

第三方依赖和参考来源见根目录 [来源说明](../THIRD_PARTY_NOTICES.md)；本项目许可说明见根目录文件。
