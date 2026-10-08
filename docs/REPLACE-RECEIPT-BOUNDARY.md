# REPLACE-03/04 回执边界与原请求保护

> 历史切片：下文的实现、节点版本、未推送状态和验证结果指登记时快照，不是当前入口。最新使用与工程说明见[文档索引](README.md)，当前退役范围见[节点目录](NODE-DIRECTORY-2026-10-09.md)；不按本页重建旧 API。

承接 [固定基线](BASELINE-2026-10-06.md)、[支持与处置矩阵](REPLACE-SUPPORT-MATRIX.md)、[1.0 规划](PLAN-1.0.md)及 [执行配置与分类准备](REPLACE-EXECUTION-SELECTION.md)。HEAD 仍为 `237afb5c0692f78b05f1aad912a53941d67e9aee`，尚未推送；本片与前两片位于其上的未提交工作区，不倒改固定基线、历史模块哈希或前片验证结果。

三个并发子代理分别实现应用身份/纯读取、Vue 当前客户端保护，以及独立数据库/HTTP 保全测试；主代理完成 HTTP 提前分派、静态 GraphChat、合并审读、定向验证和文档。范围是当前应用回执与客户端原请求保护，不是记录冻结、纯档案或正式旧入口退役。

## 1. 原请求身份

management 与 consumer 的 create/start/control 可共用同一原生命令回执，原应用 operation/scope 和完整请求此前没有持久化。原生 digest 与结果存在，不足以凭空认定该请求来自哪个应用作用域；浏览器也不能用稍后拒绝推断原操作未发生。

[应用身份 helper](../backend/src/phase1_agent/graph_application_identity.py)在 [GraphApplication](../backend/src/phase1_agent/graph_application.py)调用期间绑定完整原请求。仅首次匹配的原生回执插入时，由 [GraphRecordStore](../backend/src/phase1_agent/graph_store.py)或 [GlobalResourceStore](../backend/src/phase1_agent/global_resources.py)在原事务内追加一行：

- 使用现有 `idempotency` 表，operation 为 `graph.application.command-identity.v1`；不增加表、schema 版本或迁移。
- key 是原 operation、operation_scope、idempotency_key 的 canonical SHA256；payload 只记录这些身份、完整应用请求摘要、target 及原生回执 table/operation/key/digest 坐标。
- 保持原生请求归一化、digest、result/replay 语义；不复制请求、凭据、资源正文或原操作结果。
- 原生命令和伴随身份同事务；归一化不一致、伴随插入失败或身份冲突必须共同回滚，不留下部分新操作。
- replay 不附加身份，内部变量/对象效果回执不替代外层应用命令证据；ContextVar 在调用结束及异常后复位，不传播到运行线程。

既存回执即使具有原生 request_digest，也不会被追补应用 origin。缺原身份就 unresolved，不从 operation 前缀、结果内容、当前 session 或列表近似匹配构造作用域。SHA256 是本机确定性摘要，不是签名/MAC，不承诺防止有数据库写权限者伪造结构合法的证据。

## 2. 纯读取契约

[纯 reader](../backend/src/phase1_agent/graph_receipts.py)以 raw SQLite URI `mode=ro`、`query_only=ON` 和读事务取快照；不使用可能初始化或迁移的 `SqliteStore`，不调用 mutation callback、包注册、恢复或模型。不存在的库不创建，旧 schema 不补列；不使用会忽略 WAL 更新的 `immutable=1`。

HTTP 在 [server](../backend/src/phase1_agent/server.py)构造图服务或导入普通命令分派前截获：

| 入口 | 原请求 |
| --- | --- |
| POST `/api/graph/receipts/read` | `{operation, parameters}`，management 权限 |
| POST `/api/graph/consumer/receipts/read` | 同一原 envelope，consumer 权限 |
| named query `receipt.read` | parameters 内嵌 `{operation, parameters}`；两种作用域同规则 |

reader 核对完整原请求、伴随身份、原生回执 digest、严格 JSON/wrapper 和原结果身份，不重新套用当前 CAS/head/revision。修改后的当前状态、已完成运行和后来的定义重绑不改变已接受的原回执。

成功返回 `workflow.application-receipt-read`、`outcome=matched`、`reason_code=receipt_matched`、原应用 receipt 和冻结 result。无法证明时仅返回版本/kind/outcome/reason_code；没有成功 receipt/result。缺应用身份、缺原生回执、请求不匹配、损坏/不足证据、旧 schema 或存储不可用均不证明原操作未发生。

management 返回校验后的原生结果；consumer 仅返回 `result.receipt` 的八字段公开操作回执：

`workflow_definition_id`、`definition_revision`、`workflow_session_id`、`session_revision`、`chain_run_id`、`status`、`idempotency_key`、`operation`。

consumer 不返回私有 `graph_result`、正文或 `consumer` 当前观察，不通过构造运行协调器制造 live 投影。管理入口调用 consumer operation 仍沿原 `spec.consumer` 规则记录 consumer scope，不改变既有应用权限。

## 3. 当前客户端保护

[Vue command adapter](../frontend/src/adapters/workflowApplicationApi.ts)提供独立 `readGraphReceipt`；[command ports](../frontend/src/application/workflowCommands.ts)要求 `readReceipt`，没有回落 send 的可选实现。[普通图 store](../frontend/src/stores/workflowGraph.ts)核实 pending 或 migration_request 时只读取完整原请求，不先覆写 outbox。

- 初次提交仍先保存原请求，再发送 mutation；只有当次确定拒绝且身份仍当前时，才按原规则清除。
- 核实的 404/410/409/503、网络失败、unresolved、坏契约或晚到拒绝不能清除原 pending；`idempotency_conflict` 不作为未发生证明。
- 成功/失败均以完整 path/body/key/action 及 entry/generation 身份设迟到响应边界，不只比较相同 key。
- 已取得回执而本机写入失败时保留原请求；候选精确定义读取失败也保持待核实，不以普通回执覆盖较新的观察。
- 没有原请求的 copyPending 保留明确的新复制尝试，不将其冒称原请求核实；migration_request 有原身份时只读。

[独立 Provider](../frontend/src/application/workflowResources.ts)和 [Prompt](../frontend/src/application/promptResources.ts)分别使用只读回执端口；不重复保存资源正文，读取失败和本机清除失败保留完整 outbox。

[静态 GraphChat](../backend/src/phase1_agent/static/graph-chat-core.js)核实时使用 consumer 回执入口；不要求原 mutation 仍在发现目录中，不接受附带 live consumer 的查询结果。只持久化成功确认和 session 身份；不回退较新的观察。create 确认需要切换会话时清除旧会话观察，仍须另行刷新；存储失败则恢复原 session/pending。

客户端检验 receipt 身份、摘要格式和结果对应关系；服务端执行原 json-v1 完整请求摘要比较。json-v1 使用 Python 数字拼写，浏览器未自行生成或宣称互通的幂等摘要，仍保留 [原契约](../backend/src/phase1_agent/contract_json.py)边界。

## 4. 定向验证

最终合并：**后端 13 个文件、201 passed；前端 18 个文件、345 passed；类型与构建通过。**子代理首跑、修正和主代理重跑不重复累计，不代表全量或正式交互验收。

新增独立验证入口：

- [身份映射与绑定](../backend/tests/test_graph_application_identity.py)：原生归一化、应用 scope/完整请求区分、同步事务 hook 与异常/线程复位。
- [真实应用往返](../backend/tests/test_graph_receipt_roundtrip.py)：目前声明的 20 个有幂等回执的操作名称、原生 request 归一化、上下文接纳及六种损坏反例。包含尚未禁用的旧迁移/窄导入回归，不代表保留它们作为 1.0 新写入入口。
- [数据库保全](../backend/tests/test_graph_application_receipt_custody.py)：真实当前操作、原 CAS/已完成运行/重绑、缺身份、不追补 replay、原生及伴随证据损坏、消费者隐私、共同回滚和旧 schema；原生只读比较全部 schema/user_version/原行。
- [HTTP 保全](../backend/tests/test_graph_receipt_http.py)：两作用域直接及 named query，查询前后双宿主均未初始化，失败/旧 schema/缺库无写入。
- [静态聊天](../frontend/src/adapters/graphChatReceipts.test.ts)、[消费回执](../frontend/src/adapters/graphChatConsumer.test.ts)、[事件](../frontend/src/adapters/graphChatEvents.test.ts)：只读核实、原 body/key、读取失败/坏证据、迟到身份边界、保存失败及新观察不回退。

后端最终在 `backend/` 执行，并显式使用本工作树源码：

```powershell
$env:PYTHONPATH = (Resolve-Path -LiteralPath src).Path
$basetemp = Join-Path (Resolve-Path -LiteralPath '..\.local').Path ('pytest-receipt-final-237afb5-' + [guid]::NewGuid().ToString('N'))
& ..\.venv\Scripts\python.exe -m pytest tests/test_graph_application_identity.py tests/test_graph_application_receipt_custody.py tests/test_graph_receipt_http.py tests/test_graph_receipt_roundtrip.py tests/test_graph_application.py tests/test_graph_fresh_http.py tests/test_graph_platform.py tests/test_graph_session_objects.py tests/test_graph_candidates.py tests/test_graph_events.py tests/test_global_resources.py tests/test_graph_package_configuration.py tests/test_type_contract_store.py -q -p no:cacheprovider --basetemp $basetemp
```

结果：**201 passed，119.71 秒，退出码 0**。包含身份/数据库/HTTP/真实应用往返四份新增测试，以及 application、fresh HTTP、platform、对象、候选、事件、全局资源、包配置与类型契约的受影响回归。临时 HTTP 使用 loopback 动态测试端口，全部测试服务与命令已收尾，不改常驻进程。

前端最终在 `frontend/` 执行：

```powershell
npm test -- src/adapters/workflowReceiptApi.test.ts src/adapters/workflowResourcesApi.test.ts src/adapters/promptResourcesApi.test.ts src/application/workflowClientBoundary.test.ts src/application/workflowResources.test.ts src/application/promptResources.test.ts src/components/ProviderSidebarResources.test.ts src/components/ProviderSidebar.test.ts src/components/CurrentPromptResources.test.ts src/components/ContentSidebar.test.ts src/components/CurrentModelConfiguration.test.ts src/stores/workflowGraph.test.ts src/stores/workflowGraphCandidates.test.ts src/stores/workflowGraphMigration.test.ts src/stores/workflowGraphReviewRegressions.test.ts src/adapters/graphChatConsumer.test.ts src/adapters/graphChatEvents.test.ts src/adapters/graphChatReceipts.test.ts
npm run build
```

结果：**18 个文件，345 passed，5.51 秒，退出码 0**；`vue-tsc --noEmit` 与 Vite 构建通过，Vite 构建 1.29 秒。保留既有大 chunk 警告（JS 703.49 kB / gzip 214.00 kB），不在本片引入拆包或规模优化。范围为 Vue 当前命令/资源/迁移/候选与侧栏、两份既有资源 adapter 和静态 GraphChat，不是全部前端回归或新浏览器验收。

保留首跑与修正事实，不追加累计：

- 后端初次主代理合并 **13 个文件，179 passed / 22 failed，115.54 秒，退出码 1**；全部失败来自事件夹具引用 `workflow.text/output`，却仅显式选择自定义 `test.events`。HEAD 已使用精确显式包选择，夹具仍依赖旧隐含 compat 行为；对应 run/event 编译在本轮原生回执及身份 hook 前拒绝。只补 [事件夹具](../backend/tests/test_graph_events.py)的显式 `workflow.compat@1.0.0`，图、断言和产品加载规则不变；单文件复测 **27 passed，15.74 秒**。不称该批原本通过，不归因于回执功能，也不外推固定基线或旧历史超时。
- 后端 identity/application 最初遇到系统 TEMP 权限及一次 basetemp 父目录不存在的夹具 setup 失败，未触测试业务；改用已存在 ignored `.local` 下的全新 GUID basetemp，不改系统权限。子代理的纯映射、真实应用往返及独立存储/HTTP 成功批次与最终合并重叠，不累计。
- Vue 首跑 **14 个文件，147 passed / 1 failed，6.34 秒**，唯一失败是 Prompt 组件夹具仍要求第二次 save；改为断言只读回执。中间 **15 个文件，210 passed，6.33 秒**，类型检查另发现新增测试 fixture 的 GraphSession 索引签名与 PromptIdentity envelope 字段问题，均只修测试类型/字段；随后 **211 passed，6.42 秒** 及类型/构建通过。
- 主代理静态首跑 **3 个文件，134 passed，0.712 秒**。上列 Vue 和静态结果均包含在最终 345 项中，不能再次相加。

独立复核补强 `context.adopt` 的纯回执校验：标准四字段对象回执、0/1 数量、UUID/正整数/删除标记，以及冻结 session.objects 中的原对象 revision/revision_id、writer 和原 view_ref 关联；重复采纳的零写结果也须证明已指向原 view。真实往返及六种损坏反例通过，不从当前可变对象读取猜原请求结果。

## 5. 尚未完成

1. 整份 current/frozen/blocked 写守卫、恢复跳过、混合库旧 active/facts 保全和同进程现场保护尚未接线；原 `_change`/`_recover` 与缺包/活动阻断不放宽。
2. 固定流程及 migrated archives 的完整纯档案投影尚未迁出；本片仅证明回执查询不构造运行协调器，不证明所有历史读取均纯只读。
3. 正式旧工作台 runtime/model/exposure 等 stores、旧资源入口和静态 `app.js` 仍保留原行为；schema1–6 frozen passthrough、旧静态键与旧 opaque pending 的全生命周期隔离待做。
4. 旧 native/应用请求缺伴随身份时继续 unresolved；这是证据不足，不是自动退役操作后可清除的失败。不得以当前 reader 给旧请求伪造 origin。
5. 旧迁移/窄导入的新写入尚未禁用，旧 host/runtime/路由/UI/store 未退役；阶段 B/C、REPLACE-02–06 和 1.0 未完成。

本片不包含真实模型调用、用户库访问、常驻服务变更、新浏览器交互/截图、全量回归、旧 prepared 高耗时重跑、真实故障专项、数据清理或新增提交/推送。测试只使用新建临时库和 ignored `.local` basetemp；原历史超时、系统临时目录权限及验收日志问题均不凭新增通过销项。

DEMO-05/06、COND、LATER 继续在 1.0 后按需安排；节点打组和历史展示正文编辑/删除继续暂停。固定基线 SHA256 保持 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。

## 6. 工作区证据

本片产品模块 SHA256 如下；前片记录的哈希对应当时切片，不因本片追加修改而回写为新值：

```text
00D6D4EEF98F57A55B495C649525AC16C5345FAD49BA6032F8F91E4D46C6BFA7  backend/src/phase1_agent/graph_application_identity.py
1AE091FF83B3BE9686137F4261D66B2A904337BE5E38F00F004954FDA5F60B8E  backend/src/phase1_agent/graph_receipts.py
D9DB3A74DE8598AEB686081DBC2FB229121CFFB4F506057377944407E484D1FA  backend/src/phase1_agent/graph_application.py
0473D8C470DE9F8E21882A7B7A36E684045D968EC4AE701E7AF212B5FFF24ADA  backend/src/phase1_agent/graph_application_contracts.py
50A138A39C4C2CA9240828457E63DB717F3F68017AD94B60A2D1853F3138AEB9  backend/src/phase1_agent/graph_store.py
8F4B9DED807E473C87D587A4B073A21294B26BA813182F61578B078850E8E3FD  backend/src/phase1_agent/global_resources.py
515B348590E86AC835BF164CB34E8E9BDF739BE71AAA643B3986E9F59C88B2FB  backend/src/phase1_agent/server.py
177A5B2A918444B54F010060E30BCC442B5C54D04A94F8DED6F058F8CE03C854  backend/src/phase1_agent/static/graph-chat-core.js
6AF06365D6792D8835A859F07BE129F1C59D777898C3C36562DED17A9167C8F6  frontend/src/adapters/workflowApplicationApi.ts
F49512CAF3F7AC0D676B91E6041FD2EF0F3FE14D9C6542E356FB1FC5F018A9AC  frontend/src/adapters/workflowResourcesApi.ts
D300BDEE67BE8636287F5451036540CC25D2B25F55817A7D9EE649F049D042FD  frontend/src/adapters/promptResourcesApi.ts
5A0BE23D7C0B6388C60DA0200D05D2418C4C588FE93CB5EBB2D831814DAF387F  frontend/src/application/workflowCommands.ts
7FD82D4E39FC57F20B9F555095B066415ACB7FFF54359D516E4C490CE489C7F9  frontend/src/application/workflowResources.ts
C3E24763F97DA477818E797C1A4E0F9B9E8F7AECE3896102438ABD699BEBEB1F  frontend/src/application/promptResources.ts
80557A2D50A881DEDFAB57F7E8C23EF01E85D498A8E26CA78CB8EA4F63421E75  frontend/src/stores/workflowGraph.ts
```

最终检查：八份相关文档的 185 个本地行内链接、全部 74 个改动/未跟踪文件的空白/结尾换行/冲突标记，以及上述 15 个产品哈希和固定基线通过。`git diff --check` 通过，四份原 store 测试的 CRLF 转 LF 提示保留，不作额外格式重写。三个子代理和本片测试命令均已结束；HEAD 仍为 `237afb5`、领先 origin/main 一次提交，本片与前两片均未提交或推送。
