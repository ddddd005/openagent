# 旧架构实际移除

以已提交 `7ff72af` 为工作起点，固定 [00970f7 基线](BASELINE-2026-10-06.md)不改写。

最新状态：阶段 A/B/C 本期范围已完成，按用户最新指定建立 `0.2.0` 基线，8765 常驻已统一到该版本，限定真实证据通过同字节核对承接。详见 [最终收口](#阶段-c-最终收口)、[0.2.0 发布整理](#020-基线发布整理)与 [0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)。本次 Git 身份为 `v0.2.0`，不是 1.0 正式版；以下各批次保留当时事实，不用最终通过倒改原失败或扩大验收范围。

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

本次整合已形成提交 `8eb7e9395e32496962cf941af3519475e708a06d`（`8eb7e93`），`origin/main` 与退役前标签 `legacy-retirement-2026-10-07` 已推送并核对远端一致；整合完成时工作树干净。四个此前待推送提交历史保留。本节之后的进度文档更新不属于该提交，不据此宣称新改动已提交或推送。

上一轮提交前本地整理检查已完成：208 个后端 Python 文件的 AST 本地导入扫描为 0 个缺失；wheel 的 104 个包文件仍完全匹配源码，SHA256 不变；10 份文档的 146 处本地链接、4 处锚点为 0 个失效；四个待推送历史提交及暂存区敏感信息扫描无候选，未纳入运行数据；`git diff --cached --check` 通过。这些是整合提交前检查，不是本轮新增运行验收，也不替代尚未完成的阶段 C。

提交仅包含源码、测试和公开文档；`.local/` 下的验收原始日志、测试库、截图、wheel/venv 及仓库外归档不加入远端源码。此前未提交/未推送说明保留为当轮历史记录。固定基线 `00970f7`、基线文件哈希及退役前标签解引用提交保持不变，包版本仍为 `0.1.0`，未发布 1.0。

### 整合后剩余范围

这份整合后的待办已在本轮推进，最新结果见下节。PC、必要行为/诊断、请求安全及临时生产同源验收已补齐；整体阶段 C 仍待当次授权的同版常驻切换和目标代码真实供应商验收。

全面进程/锁/写入/磁盘故障专项、移动适配及活动内核跨进程恢复不是新增的 1.0 前置；现有 1440×900 桌面结果不外推为窄视口通过。DEMO-05/06 延后至 1.0 后，COND、LATER 为后续选做；节点打组及历史展示正文编辑/删除继续暂停。

## 阶段 C 本地收口

本节记录两个限定门槛执行前的历史状态；最终状态见下方“阶段 C 最终收口”。

2026-10-07，Asia/Shanghai。按用户要求使用三个并发组，以已推送 `8eb7e93` 及本轮未提交源码为目标；固定 `00970f7` 基线不改写。**必要实现、受影响验证与临时生产同源验收已收口，整体阶段 C 和 1.0 尚未完成。**仍待同版常驻切换和当前代码真实供应商验收两个当次授权门槛；本轮无收费调用、产品库操作或常驻变更，不先改版本、建发布标签或另立完成基线。

### 三组实施与验证

| 组别 | 本轮实现 | 受影响结果 |
| --- | --- | --- |
| 后端 | 新增受控 `ModelRequestError`，保留 provider/unknown/not-dispatched/invalid-response 分类，避免误报 `adapter_contract_error`；不进入 kernel 自动重试，安全许可仍由原 service facts 裁决 | 176 个不重复目标通过；初轮 136 正文通过、40 系统 pytest Temp setup `WinError5`，只复测这 40 在新本地 basetemp 全部通过，环境根因未关闭 |
| 前端 | 复制核实只读原回执，缺原坐标保留副本；仅初次明确拒绝记录精确 `rejected_copy`，单独显式重试带证据/并发门禁；长供应商名、模型标签及 pending 横向收缩 | 7 文件 116 项、类型及一次生产构建通过；JS 432.86 kB / gzip 137.88 kB |
| 部署 | `--workbench-dist` 提供同源 `/workbench/`、GraphChat 与 API；启动时快照 index/允许资产，拒绝目录、source map、未知路径；保留 Host/Origin 防线，工作台单独允许画布样式属性 | HTTP/静态 64 项、新 CLI 1 项通过，共 65 个独立目标；快速启动补生产及非 editable 安装路径 |

后端本轮还覆盖凭据变化/缺失在派发前或失败重试前拒绝、关闭接纳一次在途结果、重开不重发；多输出身份、共享上游一次执行、显式状态链、失败不释放写入/输出、当前串行历史/分叉/重开等必要案例通过。它们是离线受控证据，不冒称真实供应商或所有故障通过。前端明确拒绝/unknown/CAS 和读取竞态通过定向，浏览器未制造全部复制失败分支。

各组命令、目标清单和结果保留 `.local/phase-c-closeout-20261007/backend/result.json`、`frontend/result.json` 及相邻日志/JUnit；部署结果保留 `deployment/server-tests.log`、`deployment/cli-test.log`。初轮和失败复测分开保存，范围不与旧批次或彼此累计，不重跑全量。

### 当前安装与生产同源

当前 wheel SHA256：`b5b82a9c0c4696c51e70fa754e0a99c9e4e176d194e30634648c446e0b2414b3`，包仍为 `0.1.0`。初试 `--no-build-isolation` 因开发环境缺 setuptools 失败；标准隔离构建成功，保留两份日志，并纠正快速启动命令。

在原独立验收 venv 以非 editable 方式重装当前 wheel，不使用系统 site-packages 或 `PYTHONPATH`；不是新建全新 venv。前端复用当前依赖，以 `VITE_CHAT_UI_URL=http://127.0.0.1:8873/` 和 `--base=/workbench/` 构建，使用生产 dist，不使用 Vite 服务。临时安装包从独立 site-packages 启动一个 Python 服务，在 `8873` 提供三类同源入口；模型只连 `8998/v1` 的 loopback Mock，不转发外部，使用假凭据。

当前安装/HTTP/资产只读核对 **18/18 通过**：104 包文件在源码、wheel 和实际安装间完全一致，退役路径仍不入包，依赖一致；生产 index/JS/CSS 与 dist 字节一致，51 当前节点、GraphChat、旧入口/未知资产拒绝、Host 和外站 Origin 防线通过。证据为 `target-install-result.json`，与旧 wheel 的 33 项不同，不重复累计。

生产资产 SHA256：

| 文件 | SHA256 |
| --- | --- |
| `frontend/dist/index.html` | `343fdf06876a0c64acd383a1de2a1656af174a1fab95e17b454047c05b01e120` |
| `index-DwQp9mcz.js` | `b4ceaf7602e697eabd643e1daf0cf24e16f174f83fb3bc2d05be04a3f2e907b3` |
| `index-BjGGYY_u.css` | `3ba28313444d7df374b4cde991d6bfe6d95e6f9b36db62b883d934f875e88b32` |

### 浏览器与安全底线

仅操作本轮新工作区/临时验收库 `rig/acceptance.sqlite`，实际 viewport 为 **1382×956**。1024×768 override 未生效，DOM 和截图仍为前者，故不计窄视口通过；此前约 620 px 窄侧栏问题仍后置。没有为了此次收口扩展完整移动/无障碍功能。

| 场景 | 本轮观察及只读核对 |
| --- | --- |
| 节点、连线、撤销重做 | 空图创建文本/输出节点，编辑标题/正文并拖动；端口拖连一次，重复拖连仍一条边；复制 2→3 节点、撤销→2、重做→3、再撤销→2 |
| 保存、切换及完整编辑副本 | 公开端口变更保存为独立定义；完成会话后修改源标题创建完整副本，新定义/会话身份独立，原图 `PC Source` 不变，副本为 `PC Source Copy`；保存并切回原图可读 |
| 重复启动/终态同步 | 双击纯文本图只生成一条无模型成功链；串行双击 `C-DUPLICATE` 仅 A1=200、B2=200，终态自动同步 |
| 503 归类及显式重试 | `C-DIAGNOSTIC` 为 A3=200、B4=503，浏览器 B 诊断为 `model_provider_error`，原服务事实仍准确 503；双击重试仅新 B5=200，不重跑 A |
| 重复保存及 stale 冲突 | 两页面持有供应商 s1；首页面双击保存长名称仅升至 s2；另一页旧修订保存明确 409/`stale_revision`，不覆盖 s2；长名称及表单无本轮桌面横向溢出 |
| 精确同源 Chat | 从生产工作台精确定义/会话链接进入同源 GraphChat，`C-SAMEORIGIN` A6/B7 成功，公开展示累计 6 条引用 |
| 临时断连及核实 | 全部工作结束、active/in-flight=0 后停止自己的临时后端。`C-OFFLINE-MUST-NOT-RESEND` 保留原 key `22253e19-a87c-43d0-85d0-7b30b0d4d1fd` 并锁输入；离线核实失败仍保留 |
| 服务重开与页面重载 | 同临时库重开后，核实返回 `application_identity_missing`，不猜测未发生、不重发；页面重载仍保留同一 key、原文和输入锁。工作台三轮完成历史仍可读，Mock 总数保持 7 |

最后只读存储/Mock/浏览器观察联合核对 **23/23 通过**，不是 23 个浏览器测试：7 请求全部完成，6 成功、1 次受控 503；三轮各一条 chain、同一会话，无 active；仅失败节点有两次尝试，原失败不释放输出/效果；后来请求带前轮材料、供应商保持 s2、编辑副本身份独立；断连 marker 没有 chain 或模型请求，原 pending 重载保留。结果为 `browser-evidence-result.json`，直接 UI 读数为 `browser-observations.json`，原请求/响应/控制为 `rig/*.jsonl`。

截图保留 `diagnostic-provider-error.png`、`resource-conflict.png`、`desktop-1382.png`、`offline-pending.png`、`reloaded-pending.png`、`reopened-history.png`。关闭前 in-flight=0，精确核对后端/mock 启动器及其实际监听子进程后停止，8873/8998 端口关闭，三个验收页已关闭；`cleanup-result.json` 保留进程身份，日志/截图/测试库不删除。

上述断连只验证无在途时请求未知后的保护，不是“派发后丢回执”故障注入，更不是活动执行跨进程恢复。关闭时在途结果接纳由后端定向覆盖，不能把两类证据混写；全面进程/锁/磁盘/写入故障继续后置。

### 证据映射与剩余门槛

| 编号 | 本轮支持范围和证据 | 未完成内容 |
| --- | --- | --- |
| REPLACE-06 / VERIFY-01 | 当前 wheel、生产构建、同源入口、目录/拒绝防线及临时独立安装匹配 | 当次授权后切换实际常驻，核对同版路径、启动配置及环境/数据库 |
| VERIFY-02/03 | 当前串行/历史、必要多输出/共享上游/状态链、凭据/关闭行为、准确错误归类和 B-only 重试 | 当次预算内的目标供应商真实交互/诊断；业务修正输入后的真实输出质量不拿 Mock 回显替代 |
| VERIFY-04/06 | 必要 PC 编辑、安全控制、stale 冲突、原请求不重发、完成历史重开及页面重载保护 | 全面故障、窄屏/移动和活动跨进程恢复明确后置，不设新增 1.0 门槛 |
| VERIFY-05 | 本轮 176、65、116 各自范围及类型/生产构建；原失败/复测分开 | 无本期实现待修；系统 Temp 权限、其他历史根因仍按 BACKLOG 保留 |
| VERIFY-07 | 当前版本/资产/支持行为、离线与浏览器证据、空缺集中于本节 | 两个授权门槛通过后更新最终版本、发布记录及新完成基线 |

本轮代码、测试和文档尚未提交/推送；公开仓库不纳入 `.local/` wheel/venv、运行库、原日志、截图或凭据。固定基线和退役前标签保持原身份，仓库外归档不变；包版本 `0.1.0`，没有 1.0 标签。下一步只推进上述两个授权验收，全部通过后再执行发布收口，不重开旧架构或后置能力。DEMO-05/06、COND、LATER 继续在 1.0 后，节点打组及历史正文编辑/删除继续暂停。

## 阶段 C 最终收口

2026-10-07，Asia/Shanghai。用户继续授权后，以三个并发组完成同版常驻、限定真实验收及最终版本证据。**阶段 C 本期范围已完成，当轮版本 `1.0.0` 的本地构建、独立安装、常驻切换和本地记录完成；当时 Git 发布未执行。**当轮最后已推送提交为 `8eb7e9395e32496962cf941af3519475e708a06d`，源码、测试、版本和文档在其上未提交。以下保留当轮产物身份；随后用户指定的 0.2.0 发布整理另记下节，没有 1.0 标签或正式版。

### 常驻及最终产物

新建 `.local/resident/runtime/` 独立 Python 3.12.8 venv，先非 editable 安装上述已验证 0.1.0 wheel；生产前端只集中重建一次为 8765、`/workbench/` base，再复制为稳定 dist。标准 `-I -m phase1_agent.server` 在隐藏后台进程提供工作台、GraphChat 与 API，使用新稳定库 `.local/resident/data/workflow.sqlite`，未打开或复用旧测试数据库。

初次常驻核对 **21/21** 通过。限定真实验收通过后，仅更新后端项目和前端根/锁文件版本为 `1.0.0`，标准隔离构建 wheel；确认常驻没有 active/in-flight 或未决业务，精确停止自己的进程、非 editable 重装并启动最终版。最终同版部署核对 **21/21** 通过：104 源码/wheel/安装文件、监听身份/参数/指纹、生产资产、51 节点目录、SQLite v14 和空闲状态匹配，不与初次结果相加。

| 最终产物 | SHA256 |
| --- | --- |
| `phase1_agent-1.0.0-py3-none-any.whl` | `a7ace03e275e88982e7ede4ed3227daa1ebc1fd8a7884590f76eed8b31b9552f` |
| 工作台 `index.html` | `c87a60da24bce31cf5241b654c42a3f739e04fd5a7a1f83d0cc2ad844e106177` |
| `index-CqJ_BbyF.js` | `e40ef9da0467f03ca3e9a2b35cfb206160cf65b388584f9fb8d67bfd79b577eb` |
| `index-BjGGYY_u.css` | `3ba28313444d7df374b4cde991d6bfe6d95e6f9b36db62b883d934f875e88b32` |

JS 432.86 kB / gzip 137.87 kB。前端未将包版本注入 bundle，改版本元数据后不重复构建或回归；`package-equivalence.json` 只读证明收费验收 0.1.0 wheel 与最终 1.0.0 的全部 104 个包文件逐字节相同，无新增/移除/重复/差异，以此承接对应业务证据，不另付费。

最终监听 PID `47980`、启动 PID `43512` 仅为本次快照；运行状态与真实身份以 `run/state.json` 和当次检查为准。旧 `step1/启动服务.lnk` 已改指 `.local/resident/launch.ps1`，不再启动旧 bat/runtime/Vite；原快捷方式备份仅留 ignored 本地目录。常驻为本机显式管理的后台进程，没有 Windows 服务或开机自启动，不称外部生产交付。凭据从既有私有文件读入子进程环境，未复制秘密、未写入源码/状态/日志；启动器清除 `PYTHONPATH`/`PYTHONHOME`，未知或不同版占口拒绝接管。

### 限定真实供应商

使用新独立 runtime 中的 0.1.0 安装包、标准 Agent 包/内核/工具和 `public_model_factory` 下的标准 Chat transport；独立验收库与常驻库分离，不修改正式会话。请求模型 `deepseek-chat`，响应模型报告 `deepseek-flash`，两者分别记录；`temperature=0`、thinking disabled、非流式，A/B 各最多一个 HTTP、各 `max_tokens=128`。

发送前以独占不可变文件消耗额度，禁 SDK/HTTP 重试、重定向、自动代理和替代端点；连接或协议错误不返还额度、不补发。离线守卫 **13/13** 通过后，准备模式零请求、未读凭据，真实模式只执行一次。该守卫是验收限制，不是新增产品默认预算能力；输出两次最多 256 token 不限制输入或金额。

原需求是至少 15 项、不能是 5 项、仅在 `flag=true` 时适用。A 明确被要求生成错误草稿 `At least 5 items; apply always.`；B 的真实请求同时包含原输入及精确 A 草稿，返回 `At least 15 items, never 5; apply only when flag=true.`，数量/条件等七项质量检查通过。

| Agent | HTTP | 请求耗时 | 输入 / 输出 / 合计 tokens |
| --- | --- | --- | --- |
| A | 200，1 次 | 0.719 秒 | 444 / 49 / 493 |
| B | 200，1 次 | 0.875 秒 | 470 / 56 / 526 |

总用量 914 输入、105 输出、1019 token，缓存命中 0；整轮 6.078 秒，不是规模测量或费用报价。链 `e581c9e8-145a-43bb-b32a-31434c38aa6d` 成功：18 个节点、24 个唯一输出、22 条事实（含两组三元组共 6 条模型事实）、3 个对象及 completed execution checkpoint。A/B 上下文各 revision 2、接受一个 delta，frontend revision 3、含 user/assistant 两条记录。

服务关闭后 SQLite `mode=ro`、`query_only` **10/10** 一致性检查通过，包括节点、完整正文、事实、对象、起止 manifest 和完成检查点。最终 1.0 安装版临时读取服务再重开该完成库，浏览器只读取原输入与 B 正文，之后同样 10 项仍一致、请求总数仍为 2；它不是浏览器触发常驻模型的收费端到端，也不是活动现场恢复。

临时读取服务初次因本地 PowerShell 参数拼装带空格未启动，修正启动参数后成功，原日志保留，没有改产品或补发模型。其监听/启动身份核对并停止，8874 关闭；真实历史页关闭，8765 最终常驻保留。运行事实无 active。真实供应商多轮/分叉/暂停/凭据轮换/故障没有新增验收，仍按既有离线和受控证据分别记录。

### 完成映射与边界

REPLACE-06 / VERIFY-01 的最终独立安装、资产和常驻；VERIFY-02/03 的限定真实交互、业务修正、正文/对象/完成历史；VERIFY-04/06 的已有 PC 与必要安全底线；VERIFY-05 的此前受影响回归；VERIFY-07 的最终产物/证据映射均按本期范围完成。当前发布版本身份及固定边界见 [0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)，不重开旧架构或追加全量。

证据统一留 `.local/phase-c-closeout-20261007/final-release/`：`resident-initial-result.json`、`resident-final-result.json`、`wheel-build.log`、`wheel-install.log`、`package-equivalence.json`、`shortcut-switch.json`、`real-acceptance/`、`real-history-browser.json`、`real-history-1.0.png`、`readback-post/readonly-reopen.json` 和 `readback-cleanup.json`。源码、公开文档与私有运行材料分开，原日志/库/截图/wheel/venv/秘密不入 Git。

固定基线哈希、退役标签和仓库外归档不变。HIST-01/03 旧等待超时、HIST-02 系统 Temp 权限、HIST-04 诊断异常和过程47日志保存错误不关闭；全面故障、窄屏/移动及活动跨进程恢复继续后置。DEMO-05/06 在 1.0 后维护，COND/LATER 按需选做，节点打组及历史正文编辑/删除继续暂停。当轮待授权的发布动作及后续版本调整见下节。

## 0.2.0 基线发布整理

2026-10-07，用户明确要求“提交，将其建立为 0.2.0 基线，然后整理推送”。本期完成范围不变，项目元数据从未发布的本地 1.0.0 调整为 0.2.0；能力包、节点和执行协议版本不随项目编号修改。代码、测试、文档及版本一并纳入本次源码基线，Git 身份使用 `v0.2.0` 标签指向的提交，不在自指基线中写自身提交哈希；远端分支/标签以 Git 核对为准，不宣称 1.0 正式发布。

标准隔离构建 `phase1_agent-0.2.0-py3-none-any.whl`，SHA256 `b1761c7a5fdf01701e90531f76c4fff15f0299a25ccf85f22a8792f218ae94c2`。0.1.0、此前本地 1.0.0 及 0.2.0 的 104 个包文件逐字节相同，与当前源码一致；依赖未改，工作台 index/JS/CSS 沿用上表同一稳定产物。保留全部原验收证据，不重复业务、前端或全量回归，不增加模型请求。

切换前重新核对旧常驻身份与 21/21 安装证据，并只读确认常驻库没有业务会话、链/节点运行或在途依据；精确停止自己的服务、非 editable 安装 0.2.0 并重新启动。新版本安装/源码/wheel、监听身份、同源资产、51 当前节点及 SQLite v14 核对 **21/21**，`pip check` 通过。监听 PID `41184`、启动 PID `50772` 仅为当轮快照，运行身份仍以当次检查与 `run/state.json` 为准。

新证据单独留 ignored `.local/baseline-0.2.0-20261007/`：`package-equivalence-0.2.0.json`、`resident-before-switch.json`、`resident-result-0.2.0.json`、`wheel-build.log`、`wheel-install.log` 及文档检查。原 final-release 日志、产物和收费证据未覆盖，运行库、凭据、截图、wheel/venv 不入 Git。固定基线、旧标签和仓库外归档不变。

本次按用户指定收口为 [0.2.0 基线](BASELINE-0.2.0-2026-10-07.md)；1.0 正式版仍需后续版本决策，原 DEMO/COND/LATER 后置和暂停边界不变，不自动进入能力拓展。
