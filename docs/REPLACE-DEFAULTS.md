# REPLACE-02/03 默认包与工作区初始化收口

> 历史切片：下文的实现、节点版本、未推送状态和验证结果指登记时快照，不是当前入口。最新使用与工程说明见[文档索引](README.md)，当前退役范围见[节点目录](NODE-DIRECTORY-2026-10-09.md)；不按本页重建旧 API。

承接 [首轮处置表](REPLACE-AUDIT-2026-10-07.md)、[独立提示词切片](REPLACE-PROMPTS-2026-10-07.md)及 [1.0 规划](PLAN-1.0.md)。继续使用三个并发子代理，分别负责后端包选择与加载、前端初始化与受影响夹具、存档边界回归及只读处置审计；主代理复核、补真实 HTTP 离线回归、合并验证和浏览器检查。

HEAD 仍为 `00970f75fb52d741b694499a05f702600529fcdd`，证据对应其上的未提交工作区。固定 [2026-10-06 基线](BASELINE-2026-10-06.md)不修改；REPLACE-01/02 整体仍在进行，REPLACE-03/04 只推进默认加载与新安装初始化子项，未完成旧实现退出或 1.0。

## 1. 本轮裁决与实现

| 边界 | 本轮行为 |
| --- | --- |
| 新后端库的默认包 | `DEFAULT_PACKAGES` 不再选择 `workflow.compat`；独立包及其依赖照常加载 |
| 显式项目选择 | 精确使用传入选择，包括 `{}`；不强加兼容包，不补回默认包 |
| 已保存项目选择 | 原样恢复，包括空选择和原有兼容选择；不因应用默认值变化改写 |
| 内置兼容包 | 保留精确 `workflow.compat@1.0.0` 的懒注册工厂，只在选中时构造原 registry，并保持原 manifest/catalog |
| 新建或显式选择缺包 | 明确拒绝，不保存为成功，不降级为默认或兼容项目 |
| 已保存选择缺包 | 保留原选择和诊断，以空 registry 提供冻结历史读取；不借兼容包执行 |
| 缺包时的新修改 | 拒绝新定义、会话、启动和控制等事务；精确旧回执仍可重放，启动核查仍可记录 `recovery_unavailable` |
| 首次浏览器初始化 | 只有成功读取且 `raw === null` 才创建一个 UUID 空普通图；安装全部保存守卫后首存 |
| 既有浏览器记录 | schema1–6 继续恢复，不自动转换兼容空文档、改 UUID、改包锁或重发 pending |
| 损坏或不可读记录 | 保持 blocked，保留非 null 原文，不将读取错误解释成首次安装 |
| 本机投影 | 根据恢复目录裁剪无关内存项；不再无条件创建 MAIN A/B 准备草稿，App 激活也不再调用 `ensureEmpty` |

后端产品改动集中在 [builtin_packages.py](../backend/src/phase1_agent/builtin_packages.py)、[capability_packages.py](../backend/src/phase1_agent/capability_packages.py)、[graph_nodes.py](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/backend/src/phase1_agent/graph_nodes.py) 的公开构造 helper 和 [graph_service.py](../backend/src/phase1_agent/graph_service.py)。前端改动集中在 [workbenchPersistence.ts](../frontend/src/stores/workbenchPersistence.ts)、[workspace.ts](../frontend/src/stores/workspace.ts)及 [App.vue](../frontend/src/App.vue) 的激活分支。

旧测试需要兼容能力时改用显式选择；新测试不借恢复默认兼容包通过。前端纯存档夹具 [legacyWorkbenchStorage.ts](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/testUtils/legacyWorkbenchStorage.ts) 明确种入 schema5 或已保存混合 schema6，不再把首次安装等同旧 A/B 示例。

旧记录、包锁、SQLite schema、资源权威和 outbox 均未迁移或删除。模型供应商、独立提示词的 pending 键不被本轮初始化读取或覆盖。恢复未确认请求只保留身份、路径、原 key/body，不自动派发核实。

## 2. 只读与剩余能力边界

- 缺包只读不等于可以继续执行。完成历史、冻结输出、对象和原回执可读；恢复正确包后才可提交新的业务操作。
- **缺包且原会话仍有活动链时**，启动核查保留活动链并标记 `recovery_unavailable`。本轮只读门禁不允许 close，现有包配置也拒绝活动链；须补回原精确安装后重开、显式关闭，才能重配包。恢复安装后仍不可 resume，不新增活动内核恢复。
- 已保存 `{}` 没有缺包诊断，是合法空项目；其旧定义重新执行仍须通过节点目录、编译和原包锁检查。空选择不是兼容 fallback。
- `workflow.*@1/2` 与 `tools.*`、`prompts.*`、`models.source`、`agents.execute@3` 的 schema、端口、身份和历史来源不同，不自动改名或转换。
- `workflow.global-content@2` 仍是兼容类型和隐含 workspace 引用，不能因为它读取 current 就替换成 `workflow.prompt-resource` 的完整身份。现有入口保持隔离，必要执行支持仍待裁决。
- `workflow.object-read/write/delete@1` 已使用共用平台对象与 CAS，不属于旧变量/共享数据私有权威。其必要声明是否迁出单独裁决，不随旧宿主或变量代码删除；本轮没有开发新的通用对象节点。
- 既存 generic/旧 runtime 在某些核实拒绝后清 pending 的行为，尚未完成与 provider/prompt 同等级的保护核查。登记为 VERIFY-04/06 安全候选，本轮只验证初始化不重发、不丢原请求，不声称整个交互生命周期已闭环。
- 旧宿主、普通图兼容执行器、旧客户端/store、匿名聊天默认分派及混合库旧表初始化仍在。取消默认兼容包不等于完成 REPLACE-03–06。

## 3. 定向回归

主代理最终后端合并范围在 `backend/` 执行，使用本轮唯一隔离目录：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_package_selection.py tests/test_capability_packages.py tests/test_plan13b_frontend_packages.py tests/test_serial_agent_demo_integration.py tests/test_graph_session_objects.py tests/test_graph_platform.py tests/test_graph_application.py tests/test_frontend_execution_packages.py tests/test_graph_server.py tests/test_graph_service.py tests/test_graph_agent_service.py tests/test_graph_prompt.py tests/test_plan13a_frontend_public.py tests/test_models_service_integration.py tests/test_graph_failed_retry.py tests/test_graph_fresh_http.py -q --basetemp ..\.local\pytest-defaults-final-89f07a
```

**16 个文件，179 passed，213.12 秒，退出码 0**。覆盖精确默认/空/显式/已存选择、懒兼容工厂及 helper、缺包历史与旧回执、缺包活动链限制、串行多轮/复制/分叉/重开、对象及前端包锁、旧版本显式兼容支持和真实本地 HTTP 的纯文本图。

[test_graph_fresh_http.py](../backend/tests/test_graph_fresh_http.py) 不使用旧图夹具，以 schema2 和当前 execution package_lock 保存、运行、重开纯文本图，并禁止构造旧 registry。早期新增测试先后因 monkeypatch 函数名错误和历史输出字段误认各失败 1 项；修正测试契约后 schema1 首片 1 项通过，最终再升级为上述当前 schema2 范围。保留这两次测试编写错误，不冒称产品故障或从未失败。

子代理先前两批 105 / 72 项和最后包边界 15 项复测与最终合并范围重叠，不累加；前阶段 118 项也不重复累计。

前端主代理最终定向在 `frontend/` 执行：

```powershell
npm test -- src/stores/workspace.test.ts src/stores/workbenchPersistence.test.ts src/stores/modelConfiguration.test.ts src/stores/workbenchIntegration.test.ts src/stores/unifiedWorkbench.test.ts src/stores/workflowGraphRegressions.test.ts src/stores/workflowGraphMigration.test.ts src/stores/workflowApplication.test.ts src/stores/workflowGraphReviewRegressions.test.ts src/stores/workbenchInitialization.test.ts src/adapters/workbenchPersistence.test.ts src/adapters/unifiedWorkbenchDocument.test.ts src/components/ModelConfiguration.test.ts src/components/ContentSidebar.test.ts src/domain/legacyGraphMigration.test.ts src/components/GraphViewLayout.test.ts src/components/ProviderSidebar.test.ts src/components/CurrentPromptResources.test.ts src/components/GraphFrontend.test.ts
npm run build
```

**19 个文件，156 passed，5.52 秒，退出码 0**，无 warn/unhandled error。新 [workbenchInitialization.test.ts](../frontend/src/stores/workbenchInitialization.test.ts) 的 19 项包含实际首次保存后重载、schema1–6、兼容空文档、空锁、缺节点/包锁及新旧未知请求、读取/写入失败和不可读原文阻断。子代理 121 项及其中独立 19 项均为最终范围子集，不另加数量。

全 src 类型检查及 Vite 构建通过；已有大于 500 kB 的 chunk 提示保留，不启动 LATER-09。未运行前后端全量、真实模型调用、全面故障专项或常驻部署变更。

## 4. 本地浏览器证据

新库 `.local/replace-defaults-fresh-72ac11/verification.sqlite`；独立前端 `http://127.0.0.1:43931/`、后端 `43932`。临时代理和聊天基地址仅在忽略目录的独立 Vite 配置中调整，仓库默认启动配置未改。预览保留供查看，不能将 `offline` 标签视为网络禁用开关。

首次尝试 `5179` 发现已有旧浏览器记录，因此保留该记录并停止本轮为其新启动的两个服务，改用新端口及新库；没有清空浏览器存储，尝试库与日志也保留。

1. 新入口只显示一个零节点普通图，没有旧默认 A/B。其 UUID 为 `37075b91-3645-4f38-95c0-e1c3b12fcd26`；保存并重载后仍是同一图。
2. 从当前目录显式创建串行示例并保存，定义为 `e94fad25-429b-4431-aa91-cadf36b911fb`。实际后端只读查询确认 schema2、18 节点、两个 `agents.execute@3`、当前 execution lock 不含 compat，平台目录和诊断正常。
3. 普通图内容和供应商导航分别显示独立 current 面板，主画布保留；两类资源目录为空，未复制旧资源或自动填入模型。
4. 示例重载后仍保留原定义身份。没有建立运行会话、点击启动或提交聊天；浏览器证据只覆盖初始化、配置入口、保存、重载和桌面布局。
5. 原 `5178` 工作区重载后仍保留五个工作流及原独立提示词 s4；旧默认图切换后保持旧内容入口，没有新面板。该处复用原预览，没有重启原后端，不冒称新后端对全部旧档案已完成联合验收。

| 桌面视口 | 主区域 / 工作台宽 | 画布宽 | 详情宽 | 内容面板 clientWidth / scrollWidth | 页面横向溢出 |
| --- | --- | --- | --- | --- | --- |
| 1440 × 900 | 1038px | 758px | 280px | 315 / 315px | 无 |
| 1024 × 768 | 726px | 446px | 280px | 211 / 211px | 无 |

临时 viewport 已恢复默认。截图保留在新忽略目录：`fresh-empty.jpg`、`fresh-serial-1440.jpg`、`fresh-serial-1024.jpg`、`fresh-serial-default.jpg`。最后选择模型源显示必要配置字段，但没有填写或应用；最终新/原标签的 browser warn/error 采集均为空。原标签已恢复原普通图与内容导航，新预览保留供查看。浏览器不是完整 VERIFY 联合验收；未知请求/损坏存储边界是受控测试，不冒称真实断网或存储故障。

## 5. 后续顺序

继续按原编号裁决旧普通图版本、通用对象声明、资源与历史去向；下一切片收口受支持客户端/聊天分派和旧加载调用者，再决定专用实现及测试退役。缺包活动链和原请求核实的限制必须纳入后续安全检查，不能因本轮通过被掩盖。

DEMO-05/06、COND、LATER 继续后置，节点打组及历史展示正文编辑/删除继续暂停。未提交或推送，不修改固定基线。

`git diff --check` 通过，仅有已有 CRLF 转 LF 提示；本轮五份文档的 55 个本地行内 Markdown 链接及空白/结尾检查通过。固定基线 SHA256 仍为 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。

本轮主要产品源码 SHA256：

```text
C1851DF2A46B9A08E30921589719945427CF4EA448D112F760930316E29305A6  backend/src/phase1_agent/builtin_packages.py
453C0A16EF4DDD3D325736F854FC02AB2622A302144202DFEB15BC4074ED51E6  backend/src/phase1_agent/capability_packages.py
CD7DEC6C2EB1C134EACFD116BC8F95F9C77D3797A85E117C39353190604EA024  backend/src/phase1_agent/graph_nodes.py
16B0559A362899A2BA1276E0617EF94BBBFC6417DE874F3E169DA79FC9A9E0E3  backend/src/phase1_agent/graph_service.py
D3CC1AA828E4EFDF299F99C218242DEFA1A8E699B9BD4B4CCD4B1FE8A0C78D25  frontend/src/stores/workbenchPersistence.ts
10F107BA3D8925DA950BB4D9EC9BCCBA8866AD5CEA85484B4BF1ABE5710897E0  frontend/src/stores/workspace.ts
BEC810FEBACFDEB3A2F160C4D6113890AF78F4254310B6A471C953ED1EFB5BC7  frontend/src/App.vue
```
