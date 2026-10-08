# P3：酒馆业务与隔离聊天壳

日期：2026-10-08，Asia/Shanghai。用户授权继续并要求三个并发方向。本批分别实现独立酒馆业务、ST 聊天壳和有限宿主接线；P4 分叉协议不在本批实现。

## 1. 结果与版本

新增精确能力包 `workflow.tavern@1.2.0`，保留 `1.0.0 / 1.1.0`。新建且没有保存包配置的项目默认选择 `1.2.0`；旧保存选择重开不会自动升级。内联及全局 Lorebook 节点描述不变，没有重新定义旧资源 schema 或迁移旧图。

- 酒馆业务复制自宿主原前端业务/契约，放在 [chat_contracts.py](../../backend/src/phase1_agent/tavern/chat_contracts.py) 和 [chat_nodes.py](../../backend/src/phase1_agent/tavern/chat_nodes.py)。通用 `frontend_business.py / frontend_contracts.py` 未改。
- ST 复制内容、修改后的页面、业务适配、样式、头像和本地运行依赖均在 [独立 frontend 目录](../../backend/src/phase1_agent/tavern/frontend/)。
- 工作台酒馆草稿/入口放在 [酒馆插件目录](../../frontend/src/plugins/tavern/)。公共层只补静态装载、明确的展示类型支持和显式新草稿注册。
- `1.2.0` 的工作台声明使用独立扩展 ID；旧 `1.0.0 / 1.1.0` 声明和原通用聊天页面继续保留。

这是源码能力包版本，不是宿主发行版本、标签或对外发布。本批不选择宿主整体许可，不部署、不操作产品库、不提交或推送。

## 2. 业务契约与接线

| 身份 | 用途 |
| --- | --- |
| `workflow.tavern.chat-state@1` | 独立会话展示状态，只存条目和已接纳 view 引用 |
| `TAVERN_CHAT_VIEW@1` | 带所有权、CAS 依据及 read/append 来源证明的不可变产物 |
| `TAVERN_CHAT_COMMIT@1` | 实际对象接纳回执 |
| `TAVERN_CHAT_DISPLAY@1` | 显式公开的展示声明，只导出直接正文引用 |
| `tavern.chat.output/append/presentation@1` | 独立的读取、追加、展示节点 |

展示声明严格为 `schema_version / kind / entries`，kind 为 `workflow.tavern-chat-display`。每个条目只有 `entry_id / role / source_ref`；正文必须通过被公开声明直接导出的 `TEXT@2` 产物读取。

默认展示对象键为 `tavern-chat`，与 Agent 正常上下文对象分开。状态写入继续证明完整链路：酒馆自己的 read/append 节点身份、对象键、实际 config、当前 CAS、会话和调用身份、实际输入绑定、旧条目前缀和新条目身份。原通用前端节点、伪造正文或跨会话引用不能冒充该证明链。

工作台“内容”侧的“酒馆聊天”面板提供显式新建草稿和打开入口。示例为单个 `TEXT` 用户输入、原生 Agent、既有正常上下文装配/写回，以及独立酒馆展示：

```text
正常 context.merge 成功
  -> 读取酒馆展示状态 -> 追加实际用户 TEXT
  -> 重新读取状态 -> 追加 Agent 最终 TEXT
  -> tavern.chat.presentation.display（明确公开）
```

示例只创建新草稿，不保存、不改绑、不启动模型、不替换旧图。模型资源、模型名及容量保持待配置状态，不能隐藏地使用开发机供应商。完成配置并保存后才能打开准确入口。

打开链接使用已保存定义而非脏草稿，要求精确 `1.2.0` 包、单个适配输入、单个显式公开酒馆展示根和匹配的会话身份。缺失或不兼容时不生成可用链接；页面再次按实际 consumer 输入/展示声明验证，不静默丢掉必需输入。

## 3. 独立页面

入口为同源独立顶层 `/tavern/?graph_workflow=<uuid>`，可带 `graph_session=<uuid>`。不会将 ST DOM、全局 CSS、初始化函数或库挂入 Vue 工作台、原 GraphChat 文档。

- 抽取本地 ST 的聊天容器、消息模板、输入结构、必要 CSS 及消息更新/滚动函数后，在包内裁剪和适配。
- 提供消息列表、名称/头像、安全 Markdown、原文复制、发送、新建/切换会话、刷新、实际运行状态及后端允许的控制。
- 不加载原 ST `script.js / lib.js / bookmarks.js` 入口或扩展，不请求 `/api/chats / /api/characters`，不存 ST JSONL。
- 没有编辑、删除、隐藏、swipe、重生成、续写、角色卡、供应商管理或上下文操作。P4 尚未接线，因此本批不显示无效分叉按钮。
- 多输入、非 TEXT 输入、多个酒馆展示根或无展示声明会明确诊断，禁止正常发送。
- 采用本地 Markdown 转换和清洗库；不执行正文脚本、事件属性、任意 HTML/CSS、宏、regex 或 ST 扩展钩子。渲染不改原文。
- 使用共享 `GraphChatClient` 的请求/回执保护，仅通过 consumer 入口。酒馆请求日志和草稿使用独立命名空间，原通用日志不受影响。
- 结果未知时保留原 body/key、锁定新修改；重开不重发，核实只读回执。会话切换、迟到响应和轮询保留既有身份保护。
- 读取累计展示历史后按条目身份去重。正文缓存同时保留真实 TEXT 产物的 producer，不将所有旧消息误归到最近一次展示节点运行，为下一批回合分叉提供正确来源。

服务器采用有限资产表，拒绝目录、路径穿越、未知文件和多余查询字段。页面仅允许同源脚本/样式/连接/图片/字体，不放开内联脚本或内联样式；原通用页面 CSP 不变。静态读取不会启动运行内核或创建产品数据库。

## 4. 来源与包装

本地 ST 提交仍为 `06bde939fb1e9c4c8d8641d810f0a916b5bce127`，克隆未修改、未更新。

复制范围、源文件/片段和 hash、目标、改动、依赖版本及许可记录放在包内 `vendor/sillytavern/PROVENANCE.md` 和 `COPY-MANIFEST.json`，保留上游许可证与修改告知。页面有独立来源/许可入口。

本地运行依赖为 Showdown `2.1.0`、DOMPurify `3.4.16`、Lucide `0.577.0`；不升级主工作台依赖，不从 CDN 或开发机 node_modules 在运行时加载。嵌套 package-data 声明包含页面、脚本、样式、头像、许可和来源清单。

目录/页面隔离不是不可信脚本沙箱或许可隔离结论。对外分发或开放网络访问前仍需单独确认宿主许可、完整对应源码和告知安排。

## 5. 验证证据

| 验证 | 结果 |
| --- | --- |
| 包版本定向回归 | 5 文件，76 项通过 |
| 酒馆业务单元与集成 | 2 文件，33 项通过 |
| 后端最终集中回归 | 11 文件，132 项通过、1 项可选浏览器跳过 |
| 前端最终集中回归 | 10 文件，194 项通过 |
| 类型和生产构建 | `vue-tsc --noEmit`、Vite build 通过 |
| 最终客户端/静态入口复核 | 2 文件，29 项通过，含离线浏览器，无跳过/警告 |
| 离线浏览器检查 | 桌面 1280x900 / 手机 390x844 通过；路由拦截夹具，不是后端联合验收 |
| 独立安装产物 | 最终 wheel 构建及隔离安装通过；19 个静态入口资产、13 个复制 hash 和三个精确包版本通过 |
| 工作树空白和文档链接 | `git diff --check` 和专项本地链接核对通过 |

包版本回归包含新节点仅在 `1.2.0` 可用、旧节点描述不变、旧保存选择重开、通用前端继续可用、精确扩展注册和包依赖边界。

业务测试包含顺序追加、只存引用、来源证明、跨会话/未接纳/未导出引用拒绝、对象结算回滚后复用已接受产物、冷重开及实际离线 Agent 正常上下文写回后的聊天展示。所有模型传输为离线替身。

前端集中范围为酒馆插件目录、`LorebookFields.test.ts`、`workflowFrontendPackage.test.ts`、`workflowDemoActions.test.ts`、`graphChatConsumer.test.ts` 和 `CurrentPromptResources.test.ts`。覆盖新草稿不替换旧图、精确已保存入口、旧资源面板/字段在 `1.2.0` 的 SDK 依据、展示类型严格验证和原客户端行为。

集中后端回归初次缺少浏览器运行环境，跳过 1 项可选浏览器检查，并遇到子进程 GBK 解码警告。客户端子进程已明确使用 UTF-8，随后配置已安装的 Playwright/Chromium，用最终源码重跑客户端和静态入口 29 项，全部通过且无警告。

浏览器验证覆盖消息去重、冷重开、新建/切换、断连后保留原请求、核实只读回执、安全 Markdown、本地图标/头像以及手机无重叠。检查期间捕获并修复 `requestAnimationFrame`/`cancelAnimationFrame` 裸函数调用引发的 `Illegal invocation`，修改后的文件 hash 已同步来源清单。

本批测试数据库、截图和 wheel/隔离安装目标位于 `.local/tavern-p3-*`；前端生产构建产物位于 `frontend/dist`。显式使用本仓源码或专项安装目标，不改常驻 Python 安装。

最终宿主 wheel 为 `.local/tavern-p3-final-wheel-4578e88802be412396e9198a4611110e/phase1_agent-0.2.0-py3-none-any.whl`，SHA-256 为 `68dddb66b161e8fe05c41048d5a625d5592218de629cb2accd16309ca8769652`。这是本地验收产物，不是新增的宿主发行版本。

通过 `--no-index --no-deps --target` 安装到 `.local/tavern-p3-final-installed-9953e1de72c4449ebf0914546f11bf20`，隔离导入确认包来自该目标而非源码或常驻安装。核对全部有限资产、复制文件 hash，以及旧版本不能注册新聊天节点、`1.2.0` 能注册的边界。最终浏览器截图位于 `.local/tavern-p3-browser-final/tavern-desktop.png` 和 `tavern-mobile.png`。

## 6. 剩余范围

- P4：消费者完成回合候选投影、fork 命令、回执、pending、回合映射和子会话切换，仍未实现。不能拿本批来源字段作为已完成分叉的证据。
- P5：真实后端与工作台/酒馆页面的集中浏览器联合验收、完整分叉场景和最后交付核对。
- P1 后台预览启动和临时目录清理曾被策略拦截，本批不换机制绕过；离线浏览器夹具不能代替受限的后台服务验收。
- 未运行全量回归，未重开暂停的消息正文编辑、上下文操作或其他 ST 子系统，没有真实模型调用、部署、产品数据改动、Git 提交或推送。
