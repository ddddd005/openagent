# 开发新基线

记录日期：2026-10-16，Asia/Shanghai。

性质：本目录记录**本机开发环境重建后的当前基线**。它是一次环境与限定验证的事实登记，不是产品功能验收、发布说明或全量测试通过声明。

后续维护入口（2026-10-17）：[旧债清理](旧债清理/README.md)。旧债的口径订正、处置总账和后续推进文档统一在该子目录维护；本页及原始失败清单、勘察报告保留历史测量事实，不随清理进展倒改。

## 1. 基线身份

| 项目 | 值 |
| --- | --- |
| 工作区 | `D:\openagent` |
| 分支 | `develop`，跟踪`origin/develop` |
| 提交 | `8c432dbe41f311a84e39c9c1000b08982adc072f`（`fix: bind Agent final-answer schema and document workflow setup`） |
| `main` | 与本提交相同；远端`origin/main`、`origin/develop`均为同一提交 |
| 开发主体 | `develop`；`main`保留为同一基准的稳定分支，不在本目录重复维护 |
| 版本元数据 | `0.2.0`（`backend/pyproject.toml`、`frontend/package.json`） |
| 工作树状态 | 仅三份未跟踪架构文档（61-63号）；无已跟踪文件改动 |

本次环境重建**没有**产生提交、分支变更、标签或推送。三份架构梳理文档按用户决定暂不提交：

- [项目整体架构总设想](../归档/2026-10-17/中期检查2/61-项目整体架构总设想-2026-10-16.md)
- [项目整体架构当前代码已涉及部分](../归档/2026-10-17/中期检查2/62-项目整体架构当前代码已涉及部分-2026-10-16.md)
- [项目整体架构当前代码完全未涉及部分](../归档/2026-10-17/中期检查2/63-项目整体架构当前代码完全未涉及部分-2026-10-16.md)

## 2. 本机开发依赖

| 组件 | 实际版本或位置 | 说明 |
| --- | --- | --- |
| 虚拟环境 | `D:\openagent\.venv` | 由`py -3.12 -m venv .venv`创建 |
| Python | 3.12.10 | 文档推荐3.12，满足`requires-python >=3.10` |
| 后端包 | `phase1-agent 0.2.0`（editable） | `pip install -e ./backend` |
| 后端依赖 | jsonschema 4.25.1、openai 2.51.0、httpx 0.28.1 | 按`pyproject.toml`固定版本安装 |
| 测试依赖 | pytest 8.4.2 | 由`pip install -e "./backend[test]"`安装；首次只装主包，后补齐 |
| Node / npm | v22.15.0 / 10.9.2 | 满足锁定Vite要求，低于文档推荐的24.x |
| 前端依赖 | `frontend/node_modules`（94包，0漏洞） | `npm ci` |
| 前端工具链 | vite 8.3.1、vitest 5.0.3、typescript 5.9.3 | 由`package-lock.json`锁定 |
| 数据目录 | `D:\openagent\.local` | 本次仅新建空目录，业务库留给首次启动创建 |

`.venv/`、`frontend/node_modules/`、`.local/`、`frontend/dist/`均在Git忽略范围内，不进入版本库。

## 3. 实际验证结果

| 验证项 | 命令 | 实际结果 |
| --- | --- | --- |
| 开发启动预检 | `.\start-dev.bat -CheckOnly -NoBrowser` | 通过；选择`develop (8c432db)`、`.venv\Scripts\python.exe`、`.local\develop\workflow.sqlite`、后端8766、工作台5179，并列出9个能力包及tavern节点目录 |
| 后端测试收集 | `.venv\Scripts\python.exe -m pytest --collect-only -q` | **4208 tests collected，0 导入错误** |
| 后端全量 | `.venv\Scripts\python.exe -m pytest -q` | **3733 passed / 418 failed / 6 skipped / 51 errors**，14分59秒 |
| 后端选定子集 | 64号当前链路6个文件 | **109 passed，0 failed** |
| 前端全量 | `npx vitest run` | **112文件：98通过 / 14失败；1462项：1372通过 / 90失败；3个未处理错误** |
| 前端类型与构建 | `npm run build`（`vue-tsc --noEmit && vite build`） | 通过；产物`dist/index.html`、`dist/assets/index-*.css`、`dist/assets/index-*.js` |

前后端的失败项不在本次环境重建的预期内。其按文件、按错误签名的分布，两类已核到的真实集成问题（开发库重置helper缺新聊天表、浏览器依赖缺失）以及归因边界，记录于[现有测试失败清单](现有测试失败清单-2026-10-16.md)。构建期间的619kB大chunk提示属文档已登记的历史提示，不计为失败。

本次证据存放在Git忽略的`.local/test-runs/baseline-20261016/`：`frontend-full-verbose.txt`、`failures.tsv`、`backend-full.txt`、`backend-full-junit.xml`、`backend-failures.json`。

## 4. 本次未做与未验证

- **本次测量未启动产品服务**：只执行预检。用户随后自行启动验收服务，登记时后端8766与工作台5179均在监听，`/api/health`返回`{"status": "ok"}`；启动器已创建`.local/develop/workflow.sqlite`。
- **本次测量未改动业务库**：测试运行前检查显示不生成该库；测试后已存在，来源是用户启动服务而非测试写入。为不影响在线写入，未对该库打开连接或执行写入。
- **未调用真实模型**：未读取真实Key、未向上游发起请求。
- **未重新验收产品功能**：本目录不宣称用户工作流、模型对话或页面操作已验收；58号两轮离线验收与64号用户确认的两轮真实调用仍按其原始范围解释。
- **未执行提交、合并或推送**（用户已决定61-63号文档暂不提交）。

## 5. 当前开发入口约定

| 入口 | 分支要求 | 端口 | 默认业务库 |
| --- | --- | --- | --- |
| `start-dev.bat` | 当前工作区必须位于`develop` | 后端8766 / 工作台5179 | `.local/develop/workflow.sqlite` |
| `start.bat` | 需要唯一检出的`main`工作区 | 后端8765 / 工作台5178 | `.local/dev/workflow.sqlite` |

按用户决定，本机**暂不建立`.local/stable-main`**，因此`start.bat`当前不可用，启动器会明确报"需要唯一已检出的main工作区"并拒绝回退到develop。该限制不影响以`develop`为主体的开发。

## 6. 复现方式

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./backend[test]"
Set-Location frontend; npm ci; Set-Location ..
New-Item -ItemType Directory -Force .local | Out-Null
.\start-dev.bat -CheckOnly -NoBrowser
```

## 7. 后续待办

当前未决事项按优先级：

1. 测试失败项的处理方式（后端469项 + 前端90项）：逐项核实后退役陈旧断言、迁移到当前契约，或修复真实缺口；清单见本目录第二份文档。
2. 已核到的产品侧缺口：开发库重置helper的表集合未收录`workflow_chat_invocations`与`workflow_chat_round_inputs`。
3. 完整真实模型对话、页面操作与稳定服务的剩余验收证据。
4. `main`稳定入口的`.local/stable-main`工作区及其依赖安装（当前暂不建立）。
5. 61-63号架构文档的提交方式（当前暂不提交）。

## 8. 边界

本目录只登记环境重建轮次的身份、依赖、验证结果与限制，不把未执行的项目写成已完成，也不据此授权后续施工、数据迁移、服务启动或模型调用。后续源码变化后保留本基线事实，通过新的记录说明差异。
