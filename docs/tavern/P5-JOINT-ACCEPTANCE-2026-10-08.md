# P5：联合验收与交付核对

日期：2026-10-08，Asia/Shanghai。用户授权 P4 完成后提交，然后按三个并发方向进入 P5。本批本机离线联合验收已完成，三路最终结果均通过；不据此宣称全量测试、真实供应商调用或对外发布已经完成。

## 1. 提交与验收身份

- P0-P4 累积专项已提交为 `17617a1a460748c779d64177eb50d8ec5f94e7e8`，提交说明为 `feat: add global tavern resources and isolated chat forks`，共 78 个源码、测试、文档和必要告知文件。
- 未推送、部署或创建发布标签；截图、数据库、wheel、隔离安装目录和早期 `.codex-tavern-p1-*` 临时目录不在提交中。
- 酒馆节点仍使用精确包 `workflow.tavern@1.2.0`，保留 `1.0.0 / 1.1.0` 和原通用前端；没有上下文编辑、候选选择、消息编辑/删除或重生成。
- P5 只补验收测试与记录，不因测试便利改变产品接口。P4 的实现与定向结果见 [P4 记录](P4-COMPLETED-ROUND-FORKS-2026-10-08.md)。

## 2. 三个并发方向

| 方向 | 检查内容 | 当前证据 |
| --- | --- | --- |
| 真实酒馆聊天 | 实际页面和 HTTP、离线 native Agent、多轮、完成检查点分叉、子续聊、父不变、页面重开、丢响应后只读核实 | 1 项真实联合场景通过，1440px / 390px 截图已复核 |
| 真实工作台 | 实际资源 API、Lorebook 创建/编辑、提示词组复制/排序/引用、被动酒馆草稿及精确保存入口 | 1440px / 1024px 两项通过，实际入口及配置保存已复核 |
| 冷提交与安装包 | 精确 Git archive、无索引 wheel 构建及独立 target 安装、资源 hash、有限资产入口、安装来源与专项回归 | 48 项安装态专项通过，来源及原始资产字节核对通过 |

浏览器通过前台 pytest 夹具启动临时端口 HTTP 服务，并在 `finally` 中关闭线程和服务。没有启动常驻后台预览服务，不绕过此前的后台启动/清理拦截。所有运行使用独立临时数据库和离线模型替身，不接产品数据库或收费模型。

## 3. 已完成的集中回归

资源与通用客户端回归 8 个文件、95 项通过，无跳过：

```text
test_tavern_global_lorebook.py
test_tavern_prompt_presets.py
test_tavern_chat_package.py
test_tavern_chat_business.py
test_tavern_chat_integration.py
test_tavern_package.py
test_plan13a_client_http.py
test_plan13b_client_http.py
```

使用工作树 `backend/src` 和独立 `.local/tavern-p5-resources-2e3d0073f37e457dbdcff7c815127de5/`。该结果不与 P4 的重复复核累加成独立测试目标数。

`test_tavern_fork_http.py` 的 Node 核心定位从测试目录推算源码路径，改为 `importlib.resources.files("phase1_agent")`。这只改测试资源加载，确保隔离安装时 Node 读取实际安装包里的 `graph-chat-core.js`。冷产品 archive 仍为上述精确提交；冷 QA 副本使用相同定位调整，不能声称其测试文件与 P4 提交逐字相同。

资源定位调整后的源树真实 HTTP 单文件另复核为 3 项通过。前端在产品源码与 P4 提交无差异的情况下重新执行 `npm.cmd run build`，`vue-tsc --noEmit` 与 Vite 均通过。

工作台实际挂载另严格使用既有 [生产构建约定](../QUICKSTART.md)：`--base=/workbench/` 和构建时的 `VITE_CHAT_UI_URL` 必须匹配测试后端。本批选取未占用 loopback 端口 `64032`，设置 `VITE_CHAT_UI_URL=http://127.0.0.1:64032/`，执行 `npm.cmd run build -- --base=/workbench/`，类型检查与构建再次通过。资产为 `index-B48R_1zc.js / index-B_UZjMYx.css`，不依赖此前残留 `dist`，不使用常驻 `8765`。

普通 root-base 构建虽能通过类型与打包检查，不能直接挂到 `/workbench/`；第一次夹具检查发现这一配置差异后已关闭临时服务，并按现有约定重建。没有改默认 Vite 配置、静态安全边界或发行 JavaScript 字符串来掩盖问题。

## 4. 真实聊天浏览器

新增 `backend/tests/test_tavern_joint_browser.py`，最终 1 项通过，耗时 27.48 秒。使用真实 `GraphWorkflowService / SQLite / consumer_server` 和发行页面，普通资产与 API 不伪造；仅离线 native 模型使用既有 `NativeTransport` 夹具，网络路由拦截用于禁止外部访问和模拟一次已接纳分叉的响应丢失。

- 父会话实际完成两轮；旧消息依据各自 `TEXT` producer 关联第一轮，不误关联累计展示的最新运行。
- 点击第一轮分叉，子仅继承第一轮完整检查点；继续子聊不包含父第二轮，父会话的 revision、对象与历史全量不变。
- 父子页面重开不重复消息，数据库关闭后重新打开仍保留相同事实；分叉和冷重开不调用模型。
- 第二次分叉先通过真实 HTTP 得到 `201` 接纳，再丢弃响应。未知请求保存原 body/key 和父身份，禁用新修改；带不同会话 URL 重开也不自动重发或核实。
- 显式核实只调用一次 `/api/graph/consumer/receipts/read`，请求与原信封完全一致，接纳唯一真实子会话。整个场景恰有 2 次 fork POST、1 次 receipt POST、3 次模型运行，未产生额外分叉。
- Markdown 正常显示，字面 `{{keep_literal}}` 不展开；脚本、事件属性、危险链接及外部图片不能执行或加载。
- CSP、头像、Lucide 图标、桌面和手机布局通过；外部请求、缺失资源和页面异常均为零，不触发 ST 聊天/角色 API。

证据目录为 `.local/tavern-p5-joint-browser/`：`joint-evidence.json`、`tavern-joint-desktop.png`（1440x1000）和 `tavern-joint-mobile.png`（390x844）。主代理已打开两张最终截图复核，无横向溢出或聊天/输入区重叠。测试数据库位于 `.local/tavern-p5-joint-tests-02/`，不进入版本库。

## 5. 真实工作台浏览器

新增 `backend/tests/test_tavern_workbench_browser.py` 和 `backend/tests/tavern_workbench_browser.cjs`。最终 1440x1000 / 1024x1000 两项通过，耗时 27.00 秒；使用当前生产构建、真实 UI、资源/工作流 HTTP 与独立 SQLite，普通请求不伪造。

- Lorebook 新建、保存、编辑再保存，条目 UUID 保持，后端实际 `update_sequence=2`；两个节点通过可见配置表单显式绑定同一全局资源。
- 提示词组新建后独立复制，资源与成员 UUID 全新；上移后正文、role、`context_once` 和精简许可保持，原组及原引用不因复制而改变。
- 在真实图上定位并选择节点，应用引用后显式保存工作流副本；后端持久定义中两个 Lorebook 引用和提示词组引用与界面一致，原会话全量不变。
- 实际点击 `target=_blank` 酒馆入口，新页同源且携带精确已保存 workflow/session，展示原两条事实，不发送 command 或开始运行。
- 新建酒馆示例只生成独立未保存草稿，保留原缓存，无会话、pending 或自动模型配置；酒馆打开入口禁用，不发送 command 或模型请求。
- 每个宽度有 35 个实际 API 请求、7 个明确用户操作 command；打开入口及新建草稿分别增加 0 个 command。离线 native 模型仅在浏览器前的种子准备中调用一次。
- 页面异常、资产错误、console 错误、横向溢出和侧栏控件重叠均为零。节点选择使用现有“定位全部节点”交互，不强制点击视口外节点或直接改 store。

最终证据根目录为 `.local/tavern-p5-workbench-1d2b2d69274c4f20b58ca2f06b6abf01/`，两个子目录分别为 `test_real_workbench_resource_m0/workbench-1440/` 和 `test_real_workbench_resource_m1/workbench-1024/`。每处保留 `result.json`、runner stdout/stderr 和五张截图：

```text
exact-tavern-launch-{width}.png
lorebook-create-{width}.png
prompt-copy-reorder-{width}.png
saved-explicit-references-{width}.png
new-unconfigured-tavern-draft-{width}.png
```

最终两宽截图已目检；主代理另打开最终已保存引用、1024px 提示词复制表单/新草稿及实际酒馆入口复核。使用既有 Chromium 1228，不下载浏览器；临时端口 `64032` 的两次服务均已关闭。

复跑真实浏览器测试需已有 Playwright/Chromium。聊天测试使用 `NODE_PATH`，工作台测试可使用 `TAVERN_BROWSER_NODE_MODULES`；可用 `TAVERN_CHROMIUM_EXECUTABLE` 指定已安装浏览器。工作台另要求显式 `TAVERN_WORKBENCH_PORT` 与生产构建中 `VITE_CHAT_UI_URL` 一致，禁止使用常驻 `8765`，不自动下载或启动常驻服务。缺少可选浏览器运行条件时测试会明确跳过；本批最终运行没有跳过。

## 6. 安装产物

- 验收目录：`.local/tavern-p5-install-1d10a424ac864a25aa211643e44898ce/`，包含 `source/ / wheelhouse/ / installed/`。
- wheel：`phase1_agent-0.2.0-py3-none-any.whl`。
- SHA-256：`1242beb1c1fcf298e6a8420a52d96a1beffb767c37f7ab8ea897d9fac29ec8ae`。
- 由精确 P4 提交 archive 构建，不使用后续工作树源码替代；构建/安装均无索引、无依赖解析、无缓存，不修改全局 Python 安装。
- 冷 archive、wheel 成员、安装目录中完整 13 个复制目标的原始 hash 与 `COPY-MANIFEST.json` 匹配；19 条有限酒馆路由对应 18 个唯一文件，三处原始字节一致。主代理另复核源树 13 个目标和 wheel hash，均匹配。
- 测试前 75 个、结束后 103 个 `phase1_agent` 已加载模块的 `__file__` 全部位于独立 `installed/`，不从工作树或冷源码偷读产品实现；实际 Node 也加载安装包核心。
- 精确酒馆版本 `1.0.0 / 1.1.0 / 1.2.0`、原 `frontend.presentation`、酒馆展示，以及受限消费者 candidate list/fork 目录检查通过；没有管理候选选择或对象写权限。
- 安装态 `test_tavern_consumer_forks.py` 14 项、`test_tavern_chat_host.py` 24 项、`test_tavern_fork_integration.py` 7 项和 `test_tavern_fork_http.py` 3 项，共 48 项通过，无跳过，耗时 73.65 秒。

详细证据位于该目录的 `INSTALL-ACCEPTANCE.md`，包含构建/安装命令、导入来源、有限路由与 QA 定位差异。所有构建、安装和测试进程已退出，服务均随夹具关闭。

这是本机宿主 wheel 的隔离验收，复用已有运行依赖，不是依赖完全空白的虚拟环境认证、独立酒馆插件发布或新的宿主发布版本。

## 7. 已知边界

- 扩展历史测试的七项旧节点身份失败已在原起点提交 `79c145720ec3c9b5dfe5b13628a13fd863485c02` 复现；详情和快照位置见 P4。不修无关历史测试，不宣称全量绿色。
- 工作台已有最小宽度为 1024px，本批验收桌面及 1024px 工作台，不把它改造成手机工作台；酒馆独立页另验桌面和 390px。
- 本批不做实时 token 流、角色卡、Persona、完整 ST 世界书导入、模型供应商设置或扩展兼容。
- ST 的 AGPL 和依赖告知仍按包内来源记录保留。宿主整体许可未确定；本机验收不代表对外传播、网络开放或许可兼容性已获批准。

## 8. 收口

P5 范围内验收已完成，没有发现需要修改酒馆产品实现的缺陷。主代理核对产品目录与 P4 提交没有差异，复制目标 hash、wheel hash、文档链接和空白检查通过。所有临时测试进程与服务已退出；未动常驻服务、产品库、上游 ST 克隆或早期临时目录。

P4 提交保持为 `17617a1`。P5 工作树仅新增上述三个验收文件、测试资源定位调整与本目录记录，尚未另行提交；运行产物保持忽略，不纳入 Git。后续真实供应商调用、常驻切换、许可与对外交付均须独立决策，不以本次离线通过自动执行。
