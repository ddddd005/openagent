# Agent 归档入口的只读接线

承接 [固定基线](BASELINE-2026-10-06.md)、[1.0 规划](PLAN-1.0.md)、[支持与处置矩阵](REPLACE-SUPPORT-MATRIX.md)和 [退役归档与纯投影](REPLACE-ARCHIVE-READ.md)。起点为已提交的 `8bdbe86`；本片只解除现有图 Agent 归档入口的 RW store/协调器依赖，不改历史记录日期或固定归档。

## 1. 本片实现

- [graph_archives](../backend/src/phase1_agent/graph_archives.py)提供 `read_graph_archive`：只打开已有 SQLite 文件，使用 `mode=ro`、`query_only` 和同一个 `BEGIN` 读取快照；检查存储版本 0–13 及必要 records 列，不初始化表、迁移版本、注册包、获取执行租约或恢复活动链。
- 共享 `resolve_graph_archive` 承接原作用域规则：本会话运行和 Head snapshot 保存的历史引用；原生归档必须来自成功 node_run 的原 accepted 包，返回既有 `{turn, root, snapshot}`，不重新组装请求或补写事实。
- 补严 Head/commit/snapshot owner、冻结历史引用的存在与闭合状态、原生 run/chain/执行计划及 baseline owner。严格解析 JSON、校对表内 identity；缺失或损坏的库内证据拒绝，不修复或改写。外部非法身份仍是请求错误，库内非法引用是存储契约错误。
- 已迁移旧 Turn 仍按原 `legacy_archives` 授权；检查引用形状、确切 UUID、列表去重、源节点和相邻 parent 关系，调用上一片的纯闭合 reader。支持原 basic/prepared1/2/3 和变量重派生证据，保留 run_record/node_run owner 后备及未知 Context 拒绝；不执行宏或上下文正则。
- [graph_archive_http](../backend/src/phase1_agent/graph_archive_http.py)接管已有 GET `/api/graph/sessions/{sid}/archives/{turn}` 和管理 POST `/api/graph/queries` 的 `archive.read`。consumer 仍无私有归档权限；不新增授权或远程身份认证。只有匹配并通过请求检查时才选数据库，显式提供的 graph 数据库优先。
- [server](../backend/src/phase1_agent/server.py)在调用 graph service getter 前分派上述读取；旧 context/operation 模块改为对应旧路由内导入。第一次纯归档请求不初始化新旧协调器，也不导入旧执行链或 `SqliteStore`。
- [GraphAgentHost](../backend/src/phase1_agent/graph_agent_host.py)的直接 `get_agent_archive` 同样改走只读连接，原上下文调用者使用共享解析规则；移除重复的 `_allowed_archives`。这不代表 GraphWorkflowService 的构造过程或其余写入入口已只读。

## 2. 验证记录

新增 [存储与作用域回归](../backend/tests/test_graph_archives.py)和 [HTTP 回归](../backend/tests/test_graph_archive_http.py)。只使用新建临时库和 loopback 临时服务，所有测试服务及进程均已结束。

覆盖原生 schema3/4 原样返回、旧五类 frozen 档案及 owner 后备、返回副本隔离、复制后来源新增历史排除、其他会话/错误节点拒绝、Head/链/基线 owner 和结果证据损坏、畸形 legacy refs/parent、缺库/不完整表/未知版本不初始化、consumer 拒绝、显式数据库优先和直接服务查询不调用 `_store`。另有只读写入拒绝及并发 writer 改 Head 时读取保持同一快照的临时库案例；这些不是用户库或真实进程故障验收。

六个 fresh 进程分别通过真实 HTTP 读取原生及五类旧归档，阻断旧 workflow、GraphWorkflowService/GraphAgentHost、私有 Agent Runtime、prepared Context、bindings、Kernel、Runtime、旧资源 store、旧 context view 和 `SqliteStore` 导入。新旧 lazy coordinator 保持为空，保存的 schema/version/全部表行保持不变。

首轮两文件：**74 passed / 7 failed，28.53 秒**。一项是直接服务测试用的最小 records 库被错误标为完整 schema13，缺初始化表；夹具改从 schema0 明确完成服务构造，再禁止初始化。六项揭示 server 顶层旧 operation 导入存储类；改为仅旧路由使用时导入，没有削弱隔离测试。修正后原两文件 **81 passed，27.70 秒**，中间结果不累计。

随后加入三个数据库延迟选择边界，与既有受影响回归合并：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_archives.py tests/test_graph_archive_http.py tests/test_graph_agent_contracts.py tests/test_legacy_archive_contracts.py tests/test_graph_legacy_migration.py tests/test_graph_context_boundaries.py tests/test_graph_agent_service.py::test_native_graph_repeated_runs_closed_context_copy_and_restore tests/test_current_runtime_isolation.py tests/test_graph_application_receipt_custody.py tests/test_graph_receipt_http.py tests/test_graph_server.py tests/test_server_operations.py::test_http_envelope_submit_replay_and_legacy_body_remain_compatible tests/test_server_context_view.py::test_http_strict_context_and_inline_preview_dispatch_only_read_methods tests/test_server_context_view.py::test_http_invalid_revision_fences_are_redacted_400_without_service_call tests/test_server_context_view.py::test_http_preview_rejects_unbounded_private_or_broken_exact_drafts -q -p no:cacheprovider --basetemp ..\.local\pytest-graph-archive-final-8bdbe86-a
```

在 `backend/` 执行，**13 个文件，325 passed，246.20 秒，退出码 0**；server operations/context view 和 graph Agent service 只选受影响的节点，不宣称这些文件全跑。覆盖共享解析器的旧上下文/迁移调用者、当前 Agent 多轮/复制/重开隔离、既有回执纯读取和普通 graph HTTP；旧迁移仅是仍存调用者的回归，不改变停止新迁移写入的处置目标。

最后复核新增库内非法 Head identity 案例，先复现 **1 failed，1.13 秒**：原先误报 `invalid_request`。只调整内部 records reader 的错误分类后，最终两文件复测：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_graph_archives.py tests/test_graph_archive_http.py -q -p no:cacheprovider --basetemp ..\.local\pytest-graph-archive-final-boundaries-8bdbe86
```

**85 passed，31.62 秒，退出码 0**。该批与前批 84 项重叠，不重复累计；其余文件未在最后这项错误码微调后重跑，不将 325 项写成全部最终代码复测。

## 3. 剩余退出边界

本片是现有私有 Agent `archive.read` 的只读入口，不是完整旧固定会话的 session/history/resource 只读门面，也不是旧 UI 替换或删除完成。

- 保存的 frozen refs 仍是读取授权依据；本片只补结构、相邻 parent 和 owner 校验，没有补完整迁移来源会话谱系、历史包来源/实际导出证据或可信分类授权。不能把当前同 ID/version 的包声明当成历史 owner 证明。
- reader 仍扫描相关种类的记录；无关损坏行可能阻断读取，不承诺混合库损坏隔离、长期容量或性能规模。
- 尚未接线全库冻结写守卫、恢复跳过、同进程活动保护或解除旧 active/缺包的全局阻断；不改旧状态、预算、事实或原回执。
- 正式旧客户端的 schema1–6/静态键 passthrough、完整只读入口及旧专用运行/写入路由、UI/store、host/runtime/适配与专属测试仍须按依赖实际移除。GraphWorkflowService 仍继承旧宿主，旧迁移导入也尚在。

下一步继续这些删除前置，而不是长期维护两套架构；REPLACE-02/03/04/05、阶段 B/C 和 1.0 均未整体完成。DEMO-05/06、COND、LATER 继续后置，节点打组和历史正文编辑/删除继续暂停。

## 4. 保全与操作

固定基线 SHA256 保持 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。退役前标签 `legacy-retirement-2026-10-07` 仍指向 `a94d560dcd2292b4f8f792623a8f75e13262fe1f`；仓库外源码 ZIP/Git bundle 哈希与 [原记录](REPLACE-ARCHIVE-READ.md)完全一致，不移动标签、不回填固定归档。

本轮单代理，无新增子任务；无真实模型调用、用户数据库或浏览器存档访问、常驻服务变更、数据清理、全量回归、旧 prepared 历史超时专项、前端构建或浏览器验收。本片随文档独立提交，不推送；定向通过不关闭历史等待根因，也不外推 1.0 已完成。

最终检查：12 个改动/新增文件的 UTF8、结尾换行和冲突标记通过，diff 空白检查通过；6 份文档的 163 个本地链接通过。产品源码 SHA256：

```text
B724DE8E3D502547262F70C18D9EF8E650E7D3655A702C03FDAC6D0CF5C26F2C  backend/src/phase1_agent/graph_archives.py
F44484FB441B28567E1AD75FF7905EDEE97B8DDFC3A1FB781D1C1B410169596B  backend/src/phase1_agent/graph_archive_http.py
467708D1DE938F4CCBC23CA8EC13E81CF8383A2DFDDD2D27125F1CEF1D9580D3  backend/src/phase1_agent/graph_agent_host.py
793BE563FA2EAB404FB6F87CB961E2575D698E64BD80298CAA681240C3BA2C9E  backend/src/phase1_agent/server.py
```
