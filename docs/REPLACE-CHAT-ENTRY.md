# REPLACE-02/04 聊天入口分派收口

> 历史切片：下文的实现、节点版本、未推送状态和验证结果指登记时快照，不是当前入口。最新使用与工程说明见[文档索引](README.md)，当前退役范围见[节点目录](NODE-DIRECTORY-2026-10-09.md)；不按本页重建旧 API。

承接 [默认包与工作区初始化](REPLACE-DEFAULTS.md)和 [1.0 规划](PLAN-1.0.md)，继续使用三个并发子代理，分别负责 HTTP 契约与只读复核、工作台链接及 App 回归、入口 bootstrap 与存储边界。主代理实现静态分派、合并验证、浏览器检查和文档记录。

HEAD 仍为 `00970f75fb52d741b694499a05f702600529fcdd`，证据对应其上的未提交工作区；固定 [2026-10-06 基线](BASELINE-2026-10-06.md)不修改。REPLACE-01/02 仍在进行，REPLACE-04 只推进聊天分派子项，未完成旧客户端、宿主或数据实现退役，也未完成 1.0。

## 1. 本轮入口裁决

| 入口 | 本轮行为 |
| --- | --- |
| 无参数 `/`、`/static/index.html` | 保留静态 HTML 200；显示未绑定终态，不加载任一业务客户端，不发业务请求或访问 outbox |
| 普通图 | 必须有确切 `graph_workflow` UUID4，可带 `graph_session` UUID4；只加载当前消费者及其依赖 |
| 显式旧会话 | 必须有确切 `session` UUID4；暂时保留旧客户端隔离路径，不作为普通图或匿名入口的 fallback |
| 旧会话配置交接 | A/B 提示词、模型、公开引用各自以 UUID 和正整数修订成对传递，保持原精确身份，不跟随 latest |
| 无效或混合参数 | 服务端继续 404；客户端分派也在加载脚本之前拒绝，不读取、清空或合并本机记录 |
| 脚本加载失败 | 当前必要脚本失败显示加载失败，不回退旧客户端；可选前端扩展缺失仍由当前通用消费者处理 |
| 工作台普通图链接 | 使用定义文档 UUID，而非工作区别名；缺文档/身份、未保存或有原请求待核实时无 href |
| 工作台旧图链接 | 只读取当前工作流已绑定的 runtime 行；没有会话时无 href，不借用上一个工作流的全局会话 |
| 配置的聊天基地址 | 只接受既有本地页面路径 `/` 或 `/static/index.html`；生成链接时清空旧查询，只添加该架构的合法身份，保留 fragment |

分派和服务端沿用同一接受集：查询原文最多 1024 字符、11 个字段；拒绝空段、缺等号、未知或重复的解码键、非法 UTF-8、混合身份、非小写 UUID4、缺失配置对、前导零或超过 `2^53-1` 的修订。额外检查 UUID 长度和修订精确拼写，避免 JavaScript `$` 接受末尾换行；不为此扩大修改共用 wire parser。

[chat-entry.js](../backend/src/phase1_agent/static/chat-entry.js) 是唯一直接加载的入口脚本；[index.html](../backend/src/phase1_agent/static/index.html) 中全部旧控件默认位于隐藏容器，只有合法显式旧身份才解除隐藏。普通图客户端仍替换主区域，不短暂展示旧 A/B 表单。[style.css](../backend/src/phase1_agent/static/style.css) 仅补隐藏容器守卫，未重做聊天布局。

[legacyUi.ts](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/adapters/legacyUi.ts) 的两个 helper 改为 `string | null`，不再在身份无效时返回匿名基地址；[App.vue](../frontend/src/App.vue) 根据结果禁用链接，并显示对应 tooltip。现有 `VITE_LEGACY_UI_URL` 名称保留，没有新增路由、flag 或跨服务身份协议。

当前图 helper 保留此前的 loopback、HTTP/HTTPS、自定义端口及 IPv6 地址策略；这不保证默认 IPv4 HTTP 后端支持所有这些部署。旧图 helper 仍只向 `http://127.0.0.1:8765/` 或 `http://localhost:8765/` 共享会话身份，不扩大到其他端口。

## 2. 数据与剩余边界

- 本轮不迁移定义、会话、旧资源、数据库 schema 或任何浏览器存档，不生成运行或换 key 重发。App 链接计算不创建会话，不改变原锁和 outbox。
- 零网络/存储访问结论针对无身份/非法身份的分派，以及入口脚本自身。合法客户端加载后仍执行原有读取、观察和必要的显式会话选择交接；不能把静态页面 GET 的零宿主结论扩大到整个合法客户端生命周期。
- 旧 `app.js`、旧控制与 store、旧 HTTP 路由和宿主仍存在。显式旧入口是过渡隔离，不代表最终 1.0 支持范围已裁决，更不代表 REPLACE-04/05 完成。
- provider/prompt 的原请求保护不被改动。generic/旧 runtime 部分核实拒绝后清 pending 的既存行为，仍属 VERIFY-04/06 待核查；本轮入口检查不能替代那条生命周期的安全验收。
- 缺包活动链限制、兼容 v2、通用对象节点声明的归属及已有历史去向继续按处置表推进；不自动转换旧图、不删除测试或用户数据。

## 3. 定向验证

后端在 `backend/` 执行：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_server.py tests/test_server_user_ui_handoff.py -q --basetemp ..\.local\pytest-chat-entry-http-802c634a
```

**2 个文件，47 passed，29.45 秒，退出码 0**。覆盖严格查询矩阵、合法编码字段、静态允许列表和页面/API 查询边界；禁止构造两个业务宿主时，匿名/current/显式 legacy 页面、所有静态资源和 health 仍可读，数据库及 graph lock 不创建。首次运行无失败。[server.py](../backend/src/phase1_agent/server.py) 本轮未改，保留既有严格路由；这些测试不执行页面脚本。

主代理前端最终合并范围在 `frontend/` 执行：

```powershell
npm test -- src/adapters/chatEntryBootstrap.test.ts src/adapters/legacyUi.test.ts src/stores/exposurePublication.test.ts src/components/ChatInterfaceHandoff.test.ts src/adapters/workflowFrontendConsumer.test.ts src/adapters/graphChatConsumer.test.ts src/adapters/graphChatEvents.test.ts src/components/ContentSidebar.test.ts src/components/ModelConfiguration.test.ts
npm run build
```

**9 个文件，306 passed，4.56 秒，退出码 0**；全 src 类型检查及 Vite 构建通过，无测试 unhandled error。既有大于 500 kB 的单 chunk 提示保留，不启动 LATER-09。

新增 [chatEntryBootstrap.test.ts](../frontend/src/adapters/chatEntryBootstrap.test.ts) 的 86 项用真实入口源码和 VM spies 覆盖零副作用、严格解码、原存储保留、脚本顺序和失败隔离，并以 HTML AST 检查全部旧控件默认隐藏。它模拟脚本加载，不冒称执行了两个客户端的全流程。

新增 [ChatInterfaceHandoff.test.ts](../frontend/src/components/ChatInterfaceHandoff.test.ts) 的 12 项包含真实 App SSR、精确引用、当前工作流绑定、未保存及 pending 门槛；缺 generic 文档时不回落 legacy 的分支另用真实 SFC AST 守卫。后者是代码结构检查，不冒称损坏存档的浏览器联合验收。

保留本轮验证过程：

- 入口 VM 首跑的 86 项通过，但同步定向 TypeScript 检查因 `it.each` 数组展开报两处类型错误，且可选脚本“双失败”实际只测到第一条路径。改为对象参数表后重新得到有效 86 项及类型通过，不累计重复次数。
- App/helper 的测试先通过；首次全 src 类型检查发现模板跨 computed 变量不能收窄 nullable href。显式把 null 转为 undefined 后，相关 5 文件 97 项及类型通过。最终以以上 9 文件 306 项为准，子代理及前阶段的重叠结果不累加。

旧客户端仅选运行与交接相关的既有回归，在仓库根目录执行：

```powershell
node --test --test-name-pattern="handoff|unverified submission survives session switching|observed model selection" backend/tests/frontend_controls.test.cjs
```

**9 passed，276.4521ms，退出码 0**。覆盖精确交接及原请求/会话隔离，不是全部旧客户端测试；直接执行 `app.js` 的非法交接用例不表示服务器或新分派允许该非法 URL。

## 4. 本地浏览器证据

复用现有独立预览 `http://127.0.0.1:43931/` 和后端 `43932`，没有重启原预览或改常驻服务。静态文件由服务器逐请求读取，本轮没有 Python 产品改动需要重启。

1. 匿名后端首页显示“入口未绑定”；DOM 只有 `/static/chat-entry.js` 一个脚本，全部旧控件隐藏，可见控件数为零，默认桌面宽度无横向溢出。未把浏览器只读环境中不可用的 Performance API 当作网络日志证据，零业务请求由 VM 与 HTTP 范围分别验证。
2. 重载已保存串行图后，工作台仍生成 `graph_workflow=e94fad25-429b-4431-aa91-cadf36b911fb` 的完整入口。当前聊天页仅加载当前四个依赖，没有旧 composer；显示对应图名、空会话选择，启动禁用，没有创建普通图会话或点击运行。
3. 原 `5178` 存档仍有五个工作流。打开未绑定旧默认图时，聊天链接无 href、`aria-disabled=true`，tooltip 为“选择已有会话后打开聊天前端”；没有借用其他图会话或打开匿名旧页面。随后恢复原提示词普通图和内容导航，独立提示词 s4 仍保留。
4. 另开 `.local/chat-entry-preview-4ea7788c/verification.sqlite` 的 `43933` 临时后端，由测试 API 显式建立一个空旧会话夹具，不在原库或普通图预览库造数据。其 `session=449ffd29-2c75-4b42-9a5e-b731398c6c27` 页面只加载 `app.js`，准确选中该会话，图 composer 不存在。只读复核仍为 revision 1、零消息、零 chain；没有提交输入或运行模型。
5. 临时旧入口标签关闭，精确核对进程与测试库后仅停止 `43933` 服务，保留其库、日志和截图；原 `5178/8765` 及独立 `43931/43932` 服务仍保留。当前聊天页和两个工作台的最终 warn/error 采集为空，没有改变 viewport override。

截图分别保留在 `.local/replace-defaults-fresh-72ac11/` 的 `chat-entry-anonymous.jpg`、`chat-entry-current.jpg`、`chat-entry-unbound-legacy.jpg`，以及临时旧入口目录的 `chat-entry-legacy.jpg`。浏览器操作中曾把工作流单击选择误作打开、把 AX checkbox 当作 DOM role 定位；查明现有双击打开及真实 button role 后恢复原页面，不将等待/定位失败计为产品故障。

本轮浏览器只验证入口、保存后的重载和空会话显示，不是多轮聊天、完整历史、真实故障或 VERIFY-01–04 联合验收。未运行模型、全量测试、提交或推送，未删除旧实现或数据。

## 5. 下一步

继续 REPLACE-01/02 的旧版本、必要通用声明和能力处置裁决，核实旧宿主、图内兼容执行器及剩余客户端/数据调用者，再按依赖推进 REPLACE-03/04/05。入口不再匿名回落只是局部收口；仍需最终受支持入口都走新架构，才能开展 REPLACE-06 与基础使用联合验收。

DEMO-05/06、COND、LATER 保持后置，节点打组及历史展示正文编辑/删除继续暂停。固定基线 SHA256 仍为 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。

`git diff --check` 通过，仅有换行格式提示；本轮五份文档的 57 个本地行内 Markdown 链接、结尾换行及下方五个源码校验值检查通过。

本轮主要产品源码 SHA256：

```text
F114B092E09E470C4EA1F814E20288105512B7E140BF73D892DB0F116E7985CD  backend/src/phase1_agent/static/chat-entry.js
E9AE289E5B474F98337ED95ED367901C2B21373AFF19D83447C04C5B86E76689  backend/src/phase1_agent/static/index.html
41940B2EF9FEF8EB530547DE908855021A6A79613EFBA045EA6B9C239BAF792D  backend/src/phase1_agent/static/style.css
FDBA6FECA740ECA2ADC3AADDD6CC84846CE22FE9BF6A19F42FB9033D613B6D1B  frontend/src/App.vue
54E06270498B556AA174CF3F4E0557A269263F90E341DE47EDD9C5F402B16441  frontend/src/adapters/legacyUi.ts
```
