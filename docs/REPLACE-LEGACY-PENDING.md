# REPLACE-04 旧客户端原请求保全

> 历史切片：下文的实现、节点版本、未推送状态和验证结果指登记时快照，不是当前入口。最新使用与工程说明见[文档索引](README.md)，当前退役范围见[节点目录](NODE-DIRECTORY-2026-10-09.md)；不按本页重建旧 API。

承接 [固定基线](BASELINE-2026-10-06.md)、[1.0 规划](PLAN-1.0.md)、[支持与处置矩阵](REPLACE-SUPPORT-MATRIX.md)和 [当前应用回执边界](REPLACE-RECEIPT-BOUNDARY.md)。HEAD 仍为 `237afb5c0692f78b05f1aad912a53941d67e9aee`，尚未推送；本片与前三片均在其上的未提交工作区，不倒改历史记录、模块哈希或基线。

用户再次确认阶段目标是移除旧架构。**本片是删除旧入口前的请求保全，不是长期维护双架构，也不是旧架构已经退出。**三个并发子代理分别实施旧 runtime、旧资源 stores 和独立保全测试；主代理处理持久化副本入口、静态聊天、合并验证及文档。

## 1. 原请求不重发

- [旧 runtime](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/stores/workbenchRuntime.ts) 的 `replayUnknown` 仅本地报告 unresolved，不先读健康/刷新会话，不 POST、不调用保存或清 pending。覆盖会话创建、选择、输入、提示词发布、复制、变量、分叉、候选、重抽及运行控制。
- [模型/供应商](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/stores/modelConfiguration.ts)、[公开声明](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/stores/exposures.ts) 的 `reconcile` 和 [旧内容](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/stores/globalContent.ts) 的 `save(true)` 同样只保留原请求。旧 native 请求没有可证明的应用 origin，不将它包装成 graph receipt 查询。
- 首次提交仍先保存；完整 pending 指纹、会话/目标对象和恢复/销毁代际约束迟到成功及拒绝。清除结果的本机保存返回 false 或抛异常时恢复原 pending；首次 `idempotency_conflict` 也不作为未发生证明。明确未发送或首次确定拒绝与此前结果未知分别处理。
- [副本持久化](../frontend/src/stores/workbenchPersistence.ts) 的旧 `reconcileCopy` 不再从缺失请求坐标重建复制，也不激活/刷新/保存。copyPending、目标原请求及源复制原请求不能借放弃副本删除；runtime 的公开删除/绑定也检查未选中的 session pending。确定未发送的临时副本仍可清理。

这些改动不赋予旧 UUID/opaque key 新意义，不用列表、当前状态、后来 CAS/404/410 或原生摘要猜原操作结果。确定成功的原生结果与当前观察仍分开处理。

## 2. 静态旧聊天

[app.js](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/backend/src/phase1_agent/static/app.js) 对既存 schema1/2 `pending_submission` 保留原存储字节、正文与 opaque key；点击核实不网络、不改写。当前页面的旧控制/分叉/选择/预算/创建 pending 阻止重发和覆盖，列表刷新不清掉它们。

首次原生 2xx 回执须核对确切字段及 session/run/chain/source 身份、适用的修订增量和状态，然后才能清本页面 pending；失形或外来 JSON 保留待核实。检查依据是原 `workflow.py` 返回，不是历史 application origin 证明。`retry_archive` 原 `accepted + completion=unconfirmed` 与 `succeeded` 均保留其原语义。

首次输入确认还有原 record 对象、完整 body 和存储原文围栏；其他页面更新或本机保存失败不会覆盖另一份请求。保存失败保存旧 raw，重置也须核实该 raw 未被其他页面替换；已知 pending 不允许借重置删除。

**静态控制类仍是 volatile，仅本页面保全；本片未实现跨重载 journal，也无法补回历史上未持久化的请求。**旧页仍可能首次发送原生写入，不是正式只读替换；schema1–6 frozen passthrough 也未完成。

## 3. 验证

最终前端在 `frontend/` 执行：

```powershell
npm test -- src/stores/workbenchRuntime.test.ts src/stores/workbenchModelRuntime.test.ts src/stores/modelConfiguration.test.ts src/stores/exposures.test.ts src/stores/exposurePublication.test.ts src/stores/globalContent.test.ts src/stores/workbenchPersistence.test.ts src/stores/workbenchIntegration.test.ts src/stores/workbenchVariables.test.ts src/stores/workbenchContext.test.ts src/stores/legacyPendingCustody.test.ts src/adapters/workbenchPersistence.test.ts src/adapters/unifiedWorkbenchDocument.test.ts src/adapters/legacyChatPending.test.ts src/adapters/chatEntryBootstrap.test.ts src/components/ModelConfiguration.test.ts src/components/ProviderSidebar.test.ts src/components/ContentSidebar.test.ts src/components/ChatInterfaceHandoff.test.ts
npm run build
```

结果：**19 文件、364 passed，5.40 秒，退出码 0**；`vue-tsc --noEmit` 和 Vite 构建通过，Vite 1.14 秒。保留已有大 chunk 警告（JS 708.25 kB / gzip 215.44 kB），不引入拆包或规模优化。

[独立保全测试](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/stores/legacyPendingCustody.test.ts) 17 项覆盖 runtime 两版快照中的 13 类请求、选择、模型/供应商、公开声明、旧内容及 schema1–6 初始化；[静态聊天](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/adapters/legacyChatPending.test.ts) 最终 41 项覆盖原文、opaque key、网络丢失、首次成功/拒绝、本机失败、跨页面冲突、重置、控制失形和重复操作。不把内部循环当作额外案例累计，也不将初始化原文不变外推为后续保存 frozen passthrough。

后端在 `backend/` 使用本工作区源码和全新临时库执行：

```powershell
$env:PYTHONPATH = (Resolve-Path -LiteralPath src).Path
$basetemp = Join-Path (Resolve-Path -LiteralPath '..\.local').Path ('pytest-legacy-client-' + [guid]::NewGuid().ToString('N'))
& ..\.venv\Scripts\python.exe -m pytest tests/test_server_user_ui_handoff.py -q -p no:cacheprovider --basetemp $basetemp
```

结果：**1 文件、4 passed，7.67 秒，退出码 0**。这是静态资产/旧交接的临时 HTTP 回归，不是浏览器执行、模型或完整历史验收；服务和命令均已收尾。

保留修正过程，不重复累计：

- 主代理第一批 19 文件为 **318 passed / 1 failed，4.19 秒**；变量测试仍要求核实时第二次写入。仅改测试为不网络、原 key/body/snapshot 不变；中间 **319 passed，6.32 秒** 与最终范围重叠。
- 独立保全首跑 17 项有 13 项失败，是新夹具遗漏完整 A/B 提示词引用及使用不支持的凭据引用；仅修夹具后 17 项通过。类型检查另发现新负例直接给合法 path union 赋非法值，改为 unknown 输入，不放宽产品契约。
- 静态首跑 18 项通过；后续控制 41 项首跑 **39 passed / 2 failed，0.413 秒**，新预算夹具未更新初始化时被禁用的按钮，尚未发请求；只补夹具的现有控制更新调用。独立审查还发现 reset 可删除另一页面新增 pending，已加 raw 检查及回归，不省略此产品修正。
- 独立复核发现旧内容在同步持久化回调中销毁/替换 pending 后仍可继续初次 POST 或结算。补命令冻结时点、persist 前后及清 pending 后围栏；新增 24 项同步回调测试，该文件最终 37 项通过。此前主代理 **340 passed，6.15 秒** 的结果保留为中间批次，不冒称已包含这项修正。
- runtime 54 项、资源 64 项、内容追加后的 37 项、独立 17 项及静态/四文件重跑均与最终 364 项重叠，不再相加。子代理发现的模型负例 union 类型问题也仅修测试。

## 4. 删除目标与剩余边界

阶段 B 的退出产物必须是：旧专用写入/运行路由关闭，正式旧 UI/store 换成必要只读入口并删除，旧宿主/私有执行器及适配、无必要调用者的专属测试退役。只保留已裁决的公共设施和历史纯数据读取，不能保留旧执行 fallback 或长期双写。

后续按以下依赖完成实际退役，而不是继续扩展旧功能：

1. 将可信精确包来源/实际接纳证据与记录分类接入写授权、启动恢复和配置活动保护；核对完整源/目标/产物身份，同库 current 新图可用且旧 active/facts 原样保全。持久锁、类型 digest 和当前内存 manifests 不能冒充缺失历史导出/来源。
2. 迁出完整旧档案纯读取，保存冻结作用域和原事实；实现 schema1–6、compatibility 文档及旧静态键只读保留，替换正式旧入口。现 `save`/卸载仍可能把旧保存重编码，旧档案也仍会进入 RW store/旧 prepared 投影。
3. 在读取及保全到位后停止旧在线迁移/窄导入等新写入，再删除对应 host/runtime/路由/UI/store 和专属测试；共用 Agent 内核、类型/存储/算法按依赖保留。
4. 对最终版本核实没有旧执行导入、服务初始化或 fallback，接阶段 C 的独立安装、部署与基础使用验收。

本片未启用 frozen/blocked 守卫、恢复跳过或 mixed-store 校验隔离；全库 JSON/图契约耦合仍待处理。没有旧架构已移除、阶段 B/C 已完成或 1.0 可发布的结论。

无真实模型调用、用户库访问、常驻服务变更、全量回归、旧 prepared 历史超时重跑、新浏览器截图、数据清理或提交/推送。原历史根因不凭新增通过销项。DEMO-05/06、COND、LATER 继续后置，节点打组与历史正文编辑/删除继续暂停。

## 5. 工作区证据

固定基线 SHA256 保持 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`；下列值只记录本片，不改前片历史：

```text
38DB35A6D702762384C533DA99D186C6EEE5046F91E7F3D92CB2C645E062EE6F  backend/src/phase1_agent/static/app.js
239AA8D82372A5FC10DEFD60F0F93D1701E0AFCD0E28AB08309D7858FF5E975F  frontend/src/stores/workbenchRuntime.ts
C9648D492C00720B269341261EEA952A72A242C61DC1FFDBB03ABB661D00AAE5  frontend/src/stores/workbenchPersistence.ts
718CF07A85BDD470E3C5FDAB9C3AA2140E25C1D3B59C9F23CDF87178A0122C56  frontend/src/stores/modelConfiguration.ts
E9E9633FBF255C9C3C6BE07D0B130E537131014C188615515465A2A5DF3FEB89  frontend/src/stores/exposures.ts
90360B72C1EC87118BF14C1A44FFEFD928DC325AEC43995C8EB6E477BDE1CCFB  frontend/src/stores/globalContent.ts
```

最终检查：九份相关文档 203 个本地链接、全部 91 个改动/未跟踪文件的空白、结尾换行及冲突标记通过；本片六个产品模块哈希、前片回执记录的十五个模块哈希及固定基线均核实。既有四份 store 测试的 CRLF 转 LF 提示保留，不额外重写格式。三个子代理和本轮命令均已收尾；HEAD 不变，本片未提交或推送。
