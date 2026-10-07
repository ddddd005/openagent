# 旧架构实际移除

以已提交 `7ff72af` 为工作起点，固定 [00970f7 基线](BASELINE-2026-10-06.md)不改写。

## 范围调整

2026-10-07 用户明确旧会话、旧工作流及相关测试内容可一起删除，不要求保留。随后进一步明确：引用旧历史的当前对象、manifest 和共享旧 revision 也不保留，按旧内容关联闭包将所属测试工作流、会话及相关事实、对象、manifest、回执整组删除。正常关联不再作为升级报错或回滚理由，不另建兼容保留路径。原“冻结与完整只读入口”和“入口替换与旧实现移除”合并为实际删除批。

无关联的当前 Graph 数据、独立资源与配置、当前未决请求保护、可信扩展和共用平台保留；未决请求仍不自动重发，不能为已删除内容回填回执证据。缺包或未知节点本身不作为旧内容的识别依据。此前“当前引用旧历史则回滚保全”的安排已被本次关联数据删除授权替代，不倒改历史切片与固定基线。

退役前标签 `legacy-retirement-2026-10-07` 和仓库外 `D:\agent rp\方案1\_archive\openagent-legacy-2026-10-07` 保持不变。没有把旧源码搬回正式源码树，也没有改写历史验证或基线。

## 实施

本批使用三个并发子代理，分别处理 graph 宿主/执行边界、前端固定工作台、HTTP/静态聊天；主代理处理存储、专属测试及文档。第一批实际代码移除及受影响定向验证已完成；第二批/阶段 C 已取得独立安装和限定受控浏览器证据，并窄修前端运行状态同步，常驻及完整联合验收仍未完成，不提前发布 1.0。

- 删除旧宿主继承与 fallback、固定执行器、兼容节点注册工厂、旧图在线迁移/窄导入/私有档案操作。
- 旧 `/api/sessions`、提示词/模型修订、exposure/content 等专用 HTTP 路由及旧静态 `app.js` 退出；当前 GraphChat 与当前回执核实保留。
- 固定工作台/store/适配/模型和内容旧分支退役，普通 Graph 工作台及独立资源管理保留。
- SQLite v14 不再创建旧专用表；先按确切固定记录家族、compat 包身份和 30 个确切节点身份识别旧内容，再沿明确归属/引用及共享对象 revision 形成关联闭包，在同一升级事务中整组删除相关定义/会话、事实、对象、manifest 与回执。承载旧历史的当前记录也在删除范围内；正常关联不再触发回滚保全，无关联当前数据及独立资源/配置不因此删除。
- 无关联当前浏览器数据和 pending 保留；当前草稿、`saved_document` 或 `pending.body.document` 明确含旧固定/compat 内容的工作流行整行删除，活动/选中身份回落至独立当前行。无需保留旧会话的原文读取或运行能力。
- 旧专属测试和探测脚本删除；共用夹具及必要授权、事务、幂等、当前多轮/分叉/重开回归迁入当前路径。
- 最终扫描补齐 Runtime facts 的 payload 引用及 chain/node 归属关联闭包，相关事实不因 session 字段单独无关联而留下旧引用。
- `frozen_model.py` 无调用者的旧 stage factory 和 `legacy_adapter` 透传删除；前端 `archive.read` 死导出、类型、mock 及旧专属测试一起退役，不以兼容空壳保留。

固定基线文件哈希和退役前标签的解引用提交已核对不变；后端 208 个文件的 AST 本地导入扫描为 0 个缺失，并在最终代码上复核。生产 `src` 扫描 `readGraphArchive`、`GraphArchiveDetail`、`archive.read`、`legacy.migrate`、`resource.import`、`create_configured_adapter`、`--mode` 均无命中。静态扫描不替代第二批独立安装和交互验收。

## 验证状态

范围调整前冻结只读批为 283 通过、4 失败，且执行时源文件有变化；它不验证本批删除后的代码，旧保留场景不再修补。

当前删除批已有以下执行结果，不与此前切片或重叠复测累计：

| 范围 | 已有结果 | 当前边界 |
| --- | --- | --- |
| 后端初轮定向 | 55 个目标、789 个案例：780 通过 / 9 失败 | 9 项失败已修复并在下面 8 文件范围通过；初轮结果原样保留，不改称全量通过 |
| 前端初轮定向 | 16 个文件：331 通过 / 10 失败 | 修复后直接受影响 3 个文件、102 项通过，不与初轮相加或宣称 16 文件全部最终代码重跑 |
| 浏览器持久化早期定向 | 11 项通过；补强后 15 项通过 | 15 项包含在下面最终 26 项中，不累计；不是浏览器联合交互验收 |
| 后端失败修复与关联清理复测 | 8 文件、55 项通过，47.31 秒 | 9 项原失败均通过，覆盖 checkpoint/execution/platform/只读动作、两个客户端 HTTP、存储及架构退役 |
| 最终扫描补测 | 后端 4 文件、64 项通过，24.46 秒 | 存储清理 18、model package 39、models service integration 6、current runtime isolation 1；与前批有重复，不按 55 + 64 累计 |
| 删除前端死 archive 入口后复测 | 2 文件、26 项通过 | 持久化 15 项及 workflowGraphApi 11 项；与先前 15 项重叠不累计 |
| 最终前端类型/构建 | 通过；JS 430.45 kB，gzip 137.23 kB | 期间曾因第 97 行索引类型收窄触发 TS2345，改为绑定 `entryRow` 后通过；不替代独立安装、常驻及使用验收 |

后端 8 文件为 `test_graph_checkpoint_retry.py`、`test_graph_execution.py`、`test_graph_platform.py`、`test_graph_readonly_actions.py`、`test_plan12b_client_http.py`、`test_plan14_client_http.py`、`test_storage_retirement.py`、`test_current_architecture_retirement.py`。最终后端补测为 `test_storage_retirement.py`、`test_model_package.py`、`test_models_service_integration.py`、`test_current_runtime_isolation.py`。前端最终为 `workbenchPersistence.test.ts`、`workflowGraphApi.test.ts`，均为离线定向范围，不冒称全量回归。

日志/JUnit 在本地 `.local/retirement-validation/`：`backend-final.xml` / `backend-final.stdout.log`、`backend-supplement.xml` / `backend-supplement.stdout.log`、`frontend-supplement.xml` / `frontend-supplement.stdout.log` / `frontend-supplement.build.log`；三个最终 JUnit 分别为 55、64、26 项，均 0 失败、0 错误、0 跳过。此前初轮和持久化 15 项的日志仍保留。工具中断且没有可用最终结果的启动轮次不计为通过或失败证据，第一批验证进程已全部退出。

截至第一批收口，实际移除及定向验证完成，第二批/阶段 C 及 1.0 未完成。第一批未提交或推送，没有真实模型调用、产品数据库操作、常驻服务切换或外部源码归档改动。第二批当前结果见下方，不将初轮或历史范围改写为最终代码全量通过。

## 第二批：独立安装与受控浏览器验收

2026-10-07，Asia/Shanghai。目标为 `7ff72af` 之上的第一批最终未提交源码；后端从当前源码构建 wheel 后，在独立 venv 以非 editable 方式安装，不使用 test extras、system-site-packages 或 `PYTHONPATH`。包版本仍为 `0.1.0`，不是已发布的 1.0。

后端 wheel SHA256 为 `d7bbf7ee1c279770c17b5e64c0ceb39888fd7a30b147f5ab97e510c0361eed5e`，后续前端窄修未改后端，未重复构建 wheel 或重跑后端定向测试。浏览器工作台使用当前前端源码及现有 `node_modules` 的 Vite 开发服务；类型/生产构建有离线结果，但本轮不是全新 `npm ci` 或生产前端部署联合验收。

### 独立安装

最终检查 **33/33 通过**：104 个包文件与当前后端源码一致，38 个退役模块/旧静态文件不在 wheel；未安装或依赖 smolagents，`pip check` 正常。标准 CLI 可独立临时启动，当前目录有 51 个节点，默认包不含 compat，匿名首页走当前 GraphChat；旧 HTTP 路由、旧会话 query、旧静态 `app.js` 和 `--mode` 参数均被拒绝。临时安装检查服务已停止、监听端口已关闭。

首轮在 Windows 端口回收瞬间断言失败，仅为证据脚本增加关闭等待后取得最终结果，没有修改产品；`result.initial.json`、`http-observations.initial.json` 和 `verification.initial.stdout.log` 保留。最终证据在 `.local/release-acceptance-20261007/install/` 的 `result.json`、`http-observations.json`、`verification.stdout.log` 及 build/install 日志，33 项不与第一批定向用例累计。

### 浏览器限定场景

浏览器只操作新工作区和临时验收库 `.local/release-acceptance-20261007/rig/acceptance.sqlite`。模型端点为 loopback 受控 mock，使用 `final_answer` 工具协议完成当前 Agent，不包含收费或外部真实模型调用。

| 场景 | 当前结果 | 范围与边界 |
| --- | --- | --- |
| 独立资源管理 | 供应商“本地验收供应商”保存并重开为 s2，名称/URL 保留；提示词资源 `c187b7cb-a7a1-4b1b-96e0-cf26fea9323e` 重开为 s1，正文/system role/启用保留 | 提示词本轮未接入串行执行，不宣称已验证运行引用 |
| 当前图保存重载 | 18 节点串行 Agent 示例配置 `acceptance-echo`、`max_tokens=128`、`temperature=0`，保存重载保留 | 当前供应商完整身份选择，不通过旧修订资源 |
| 同会话多轮 | `ROUND-1`、`ROUND-2` 在会话 `1657d1b4-c6ab-4201-99cd-58e42b32d0e7` 完成 | 只读核对确认第二轮 A/B 请求均包含首轮上下文 |
| 首轮候选分叉 | 从首轮候选新建会话 `668911a7-929c-47fc-8bf5-742899d41890`，`BRANCH-1` 完成 | seed 及 A/B 请求均继承首轮、排除 `ROUND-2`，新会话身份与候选来源一致 |
| 暂停与继续 | `PAUSE-2` 两次暂停/继续成功：一次模型发出前，一次 A 在途结果接纳后，B 尚未发出；A9/B10 各请求一次 | `PAUSE-1` 初试收到 `stale_revision`，不计作暂停通过，保留原结果；暂停不取消已在途请求 |
| 确定失败重试 | `RETRY-1` 为 A11=200、B12=503、显式重试后 B13=200 | 只让 B 增加新尝试，不重跑已接受的 A；原 B 失败不释放输出或效果，服务事实保留 request/attempt/outcome 与准确 503 |
| 聊天入口后续聊 | 精确定义/会话链接进入独立安装的静态当前聊天页，`CHAT-1` A14/B15 完成，末条正文读取 12 条引用 | 返回工作台刷新/重开后，Chat 运行存在于同一分叉会话历史 |
| 完成历史重开 | 原会话重开显示 `0b7986a9`、`40e33ca2` 两轮；分叉重开显示 7 个本地运行加继承首轮，共 8 条历史，含 Chat 和终态补验 | 当前完成库重开可读，不外推为活动现场跨进程恢复 |
| 窄修后终态补验 | `TERMINAL-SYNC` A16/B17 完成；`TERMINAL-RETRY` A18=200/B19=503，失败菜单无需手动刷新即出现，显式重试 B20=200 | 完成状态和安全重试菜单已自动同步，仍不重跑 A 或自动重发 unknown |

最终只读合并核对 **74/74 通过**，包含 20 个 mock 请求和 20 个响应：18 次成功、2 次受控 B=503。临时库 9 条 chain 全部最终 succeeded，其中 6 个核心场景、2 个窄修补验可用于对应验收；额外 `PAUSE-1` 仅完成运行，不计为暂停验收通过。两会话 `active_chain_run_id` 均为空，162 个成功节点尝试及 2 个原 B 失败尝试保留，216 个输出均属于成功尝试。17 个表、11 个记录家族均为当前范围，结构化 payload 未发现 compat 或退役身份；这些只证明本次临时库，不代表产品库已经检查或清理。

受控 503 在 executor 表层仍显示 `adapter_contract_error`，service 事实的 `provider_error` / `status_code=503` 准确，安全重试边界成立；不将其称为 mock 响应非法或新的后端修复。真实供应商诊断归类继续归 VERIFY-03 后续核对。本轮工作台功能场景和截图使用 1440×900 桌面视口；默认约 620 px 窄侧栏仍观察到横向滚动，未修改响应式布局，不宣称窄视口或移动适配通过。

### 验收发现与窄修

本轮发现前端 observe 结果未同步会话/view，完成或失败后菜单和历史可能停留旧状态。窄修集中在当前 graph store 与对应回归：同步观察结果、终态补一次刷新；明确暂停遇到 `stale_revision` 显示刷新后重试，不自动重 POST。没有扩大失败继续范围或改变未知请求保护。

修复后的 `workflowGraphReviewRegressions.test.ts` **1 文件、13 项通过**，0 失败；类型检查及构建通过，JS 431.16 kB、gzip 137.41 kB。日志/JUnit 为 `.local/release-acceptance-20261007/frontend-state.stdout.log`、`frontend-state.xml`、`frontend-state.build.log`。仅做直接受影响集中验证，不重复全量或将其与第一批前端用例累计；修复后浏览器终态同步及 B=503 菜单自动可见取得上表补验与只读证据。

最终证据在 `.local/release-acceptance-20261007/browser-evidence-result.json`，对应 mock 原请求、响应和控制在 `rig/requests.jsonl`、`responses.jsonl`、`controls.jsonl`；保留 `workbench-history.jpg`、`automatic-retry-menu.jpg`、`chat-continuation.jpg`。只读核对与 UI 观察分别记录，不将数据库 active 为空冒称为全部浏览器原请求安全验收。

主代理按精确 PID、可执行文件和命令行核对后停止 mock 142284、后端 128352、前端 121108；停止前 in-flight=0，8867/8997/5260 三端口关闭，结果在 `cleanup-result.json`。本轮创建的浏览器 tab 已关闭，临时视口已 reset；测试库、原始日志和截图保留，未操作产品数据库或常驻服务。

### 当前边界

第二批/阶段 C **部分通过，尚未完成**。本轮独立安装、限定受控交互、只读核对和临时进程收尾已完成；它们不替代常驻后端/前端及启动配置收口、目标代码的收费真实模型调用、完整 PC 操作、重复点击/冲突/断网与原请求核实、真实供应商诊断归类、窄视口布局或全面进程/存储故障验收。完成历史可读不等于活动内核跨进程恢复。

固定基线 SHA256 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4` 及退役前标签解引用提交 `a94d560dcd2292b4f8f792623a8f75e13262fe1f` 已复核不变，仓库外归档未改。第二批验收结束时本批代码及文档尚未提交或推送；没有收费真实模型调用、产品数据库操作或常驻服务切换，未发布 1.0、未另立收口基线。DEMO-05/06、COND、LATER 继续后置，节点打组及历史展示正文编辑/删除继续暂停。

## 阶段性整合

2026-10-07，Asia/Shanghai。用户授权先本地整理，再提交并推送远端。本次阶段性提交纳入第一批实际移除、第二批独立安装/限定 Mock 验收记录及前端观察/终态同步窄修，保留 `237afb5`、`a94d560`、`8bdbe86`、`7ff72af` 等已有提交历史；不压缩或改写退役前提交，不将旧实现放回正式源码树。

README、开发说明、测试说明、规划和待办同步当前状态。阶段 A 与第一批已完成，阶段 C 部分通过；常驻部署、剩余 PC 操作/原请求核实、诊断归类及目标代码限定真实调用仍待继续。包版本保持 `0.1.0`，不建立 1.0 标签或新完成基线。各轮原始失败与复测结果保留，不重复累计，也不因文档整理重跑全量验证。

提交仅包含源码、测试和公开文档；`.local/` 下的验收原始日志、测试库、截图、wheel/venv 及仓库外归档不加入远端源码。此前未提交/未推送说明保留为当轮历史记录，实际提交与远端同步结果以 Git 记录为准。
