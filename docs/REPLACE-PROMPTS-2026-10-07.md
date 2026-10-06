# REPLACE-02 独立提示词 current 管理与引用选择

本地验收日期：2026-10-07，Asia/Shanghai。承接 [内容入口复核](REPLACE-LAYOUT-2026-10-07.md)、[供应商切片](REPLACE-PROVIDER-2026-10-07.md)和 [1.0 规划](PLAN-1.0.md)。按用户要求使用三个并发子代理，分别实施后端声明/回归、前端资源协议/控制器和前端可信面板/字段；主代理负责实际入口集成、复核、合并回归及浏览器验证。

起点与 HEAD 仍为 `00970f75fb52d741b694499a05f702600529fcdd`；本轮证据对应其上的未提交工作区，不改变固定基线，不代表 REPLACE-01/02 或 1.0 已完成。

## 1. 范围裁决

| 项目 | 本轮范围 |
| --- | --- |
| 资源权威 | 独立包 `workflow.prompts@1.0.0` 的 `workflow.prompt-resource` schema1；不使用旧修订库或兼容 `workflow.global-content` |
| 资源结构 | 保持 `{enabled,members}`；成员保留 UUID、正文、完整 presentation 和 metadata，不新增顶层 name/kind |
| 显示标签 | 从第一个非空成员正文首行派生；空组显示资源 UUID。列表单行省略并保留 title，另显示 UUID 前缀以区分同正文资源；标签不是新增持久名称 |
| 管理范围 | 新增固定 `workspace`；列出该类型当前资源，编辑已有其他 scope 时保留完整身份。本轮浏览器只验收 workspace，其他 scope 用受控测试覆盖 |
| 必要操作 | 新增、编辑成员正文/呈现字段、启停、保存、刷新和显式核实原请求；允许空组，最多 1024 成员 |
| 删除与导入 | 可移除未提交表单中的成员；不提供持久资源删除、独立资源 import 或自动转换，不改旧资源和定义 |
| 引用 | `prompts.global-reference@1` 使用完整 ResourceIdentity；缺失引用保留并诊断，不自动选择第一项、不替换类型或改包锁 |
| 旧入口 | 旧图继续使用原内容侧栏/编辑器；兼容 v2 选择目录错位仍待范围处置，不能以本轮独立入口的通过销项 |

metadata 在编辑中原样保留，不提供新元数据编辑功能。保存的是 current 资源的新 update_sequence，资源引用不绑定序号或正文副本。middle 深度可按既有 schema 保存；本轮不增加历史锚点或长上下文能力，普通装配对缺少锚点的诊断边界不变。

## 2. 接线与请求安全

- [prompt_package.py](../backend/src/phase1_agent/prompt_package.py) 登记精确 panel/node-fields 声明，并在现有 manifest exports 中列出类型、九个节点和扩展。资源 schema、manifest schema1、包依赖及节点运行语义不变。
- [promptFrontendManifest.ts](../frontend/src/plugins/promptFrontendManifest.ts) / [promptFrontendPackage.ts](../frontend/src/plugins/promptFrontendPackage.ts) 经已有可信宿主登记实现，字段只接管 `reference`；不绕过宿主直接挂载节点编辑器。
- [ContentSidebar.vue](../frontend/src/components/ContentSidebar.vue) 按完整声明身份选取可信面板；缺包、错误版本、伪造或重复声明显式不可用，普通图不回退旧内容库。
- [CurrentPromptPanel.vue](../frontend/src/components/CurrentPromptPanel.vue) 与 [PromptReferenceFields.vue](../frontend/src/components/PromptReferenceFields.vue) 共用 [promptResources.ts](../frontend/src/application/promptResources.ts) 单例；资源提交锁与工作流 SDK 配置锁分别生效。
- [App.vue](../frontend/src/App.vue) 仅在旧图内容模式显示旧主编辑器。普通图内容侧栏打开时仍保留顶部操作、画布和节点详情，沿用前轮根布局修复。
- [workflowPromptResources.ts](../frontend/src/domain/workflowPromptResources.ts) / [promptResourcesApi.ts](../frontend/src/adapters/promptResourcesApi.ts) 检查完整 scope/type/UUID、schema、序号、成员唯一性、JSON 和呈现字段；回执必须匹配原身份、序号及 `deleted=false`。

新请求队列使用独立键 `workflow.prompts.resource.pending.v1`。旧 `workflow-workbench:content-drafts:v1`、供应商队列及已有正文不被搬移、覆盖或清除。提交前必须先保存原 body/key；持久化失败不派发请求。未知结果或回执不匹配保留原请求，只允许显式同 key/body 核实，不自动重发。

首次明确拒绝且非 idempotency_conflict 可清 pending，保留编辑表单供刷新后重编；未知结果后的核实即使后来被拒绝，也不能据此结清原 unknown。读取/清理本地队列失败继续阻止不安全修改，不把页面重载当成故障恢复证明。

## 3. 定向验证

后端在 `backend/` 执行：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_prompt_package.py tests/test_prompt_current_resources.py tests/test_model_package.py tests/test_capability_packages.py tests/test_plan13b_frontend_packages.py -q --basetemp ..\.local\pytest-prompt-current-2bbac8e3
```

**5 个文件，118 passed，13.72 秒，退出码 0**。其中新加 11 项，包含不加载 compat 的提示词声明、named current 保存/读取/重放/重开、跨 scope/type 同 UUID 隔离、实际 reference -> resolve -> sink 图运行，以及 missing/disabled 在 start 前拒绝。默认目录新增两个 prompt 扩展；相关默认、禁用和重开测试改为精确 ID 集合，消费端仍只返回其公开绑定。

前端合并定向在 `frontend/` 执行：

```powershell
npm test -- src/domain/workflowPromptResources.test.ts src/adapters/promptResourcesApi.test.ts src/application/promptResources.test.ts src/components/ContentSidebar.test.ts src/components/CurrentPromptResources.test.ts src/application/workflowFrontendPackage.test.ts src/application/workflowNodeConfiguration.test.ts src/components/ProviderSidebar.test.ts src/components/ProviderSidebarResources.test.ts src/components/CurrentModelConfiguration.test.ts src/components/GraphViewLayout.test.ts src/components/GraphV2Wiring.test.ts src/components/GraphFrontend.test.ts src/components/WorkbenchCanvas.test.ts
npm run build
```

首跑 **166 passed / 2 failed，另有 2 个 unhandled errors，退出码 1**。新增轻量测试渲染器在 Vue v-model 更新时缺少 Document，导致继发断言及卸载错误；补齐 Document/ShadowRoot/getRootNode 并使用真实 SFC setup/hooks 后保留原断言复测。随后增加一项成员上限交互测试。

最终 **14 个文件，169 passed，3.73 秒，退出码 0**，无 warn 或 unhandled error。覆盖实际表单提交、空组、1024 上限、所有呈现字段及负顺序、metadata 保留、缺失引用、同 UUID 跨 scope 选择、SDK 生命周期、可信声明拒绝、锁隔离、原请求核实和 App 真实路由条件。

此前子代理的资源协议 93 项及 UI 29 项属于该 169 项的子集，不累加。该批还与前轮供应商/布局回归重叠，不称为新增加 169 项或前端全量。类型检查及 Vite 构建通过；仍保留大于 500 kB 的已有单 chunk 提示，不启动 LATER-09。未运行前后端全量或真实模型调用。

`git diff --check`、新增源码/文档空白及文件结尾检查通过；本轮更新的五份文档中 49 个本地行内 Markdown 链接检查通过。

## 4. 本地浏览器证据

复用忽略目录 `.local/replace-provider-20261007-7d3e9c/verification.sqlite` 和 loopback 前端 `http://127.0.0.1:5178/`。先核对原验收后端进程确实绑定该库，再重启该独立预览以加载新声明；没有操作原运行库或常驻服务。预览保留供后续检查。

新建仅用于配置验收的普通图定义 `b5517af9-ebb2-4783-9d67-56c59517a52b`，添加一个 `prompts.global-reference@1` 节点 `1b77b6e7-71e7-485d-bd51-8439e184b59c`；未建立运行会话、未点击启动或派发模型。

提示词资源：`workspace / workflow.prompt-resource / 37a1e302-b5f2-4f15-a7b1-b00757596fec`。

1. 新建空组保存为 s1，显示 UUID 和零条目。
2. 编辑增加正文，保存 assistant/after/order=-2 为 s2；重新打开时字段保持。
3. 从画布菜单添加独立引用节点。其默认缺失引用保留，应用按钮不可用，没有自动改选已有资源。
4. 显式选取该资源并应用、保存图，DOM 的选择值包含 `[1,"workspace","workflow.prompt-resource",UUID]`。
5. 资源停用保存为 s3，已配置节点立即显示停用诊断，身份未改变；重新启用并更新长首行正文为 s4，节点显示当前 s4。
6. 切到旧图，确认原内容侧栏和主编辑器存在、新面板不存在；返回新图、重载并重新打开内容/选择节点，s4 与完整引用保留。

| 桌面视口 | 主区域 / 工作台宽 | 画布宽 | 详情宽 | 侧栏正文宽 / scrollWidth | 页面横向溢出 |
| --- | --- | --- | --- | --- | --- |
| 1440 × 900 | 1038px | 758px | 280px | 315 / 315px | 无 |
| 1024 × 768 | 726px | 446px | 280px | 196 / 196px | 无 |

1024 下内容侧栏 `clientHeight=734`、`scrollHeight=743`，表单滚动留在侧栏内；画布与详情相邻且占满主区域。已恢复默认 viewport override，再次重载检查工作台与父区域同宽 918px，仍无页面横向溢出。本轮浏览器 error/warn 采集为空。

截图留在同一忽略目录：`prompt-after-1440.jpg`、`prompt-after-1024.jpg`、`prompt-after-default.jpg`。先前截图和数据未删除。浏览器使用默认已启用目录，仍能看到兼容节点；无 compat 的独立性是上述受控后端/前端包测试证据，不冒称已完成默认目录退役。unknown/CAS 是受控自动测试，没有主动制造真实存储或网络故障。

## 5. 剩余事项与代码标识

独立提示词 current 管理/选择的限定切片完成；REPLACE-01/02 整体仍在进行。下一步继续裁决兼容 v2 内容、旧图/存档/缺包加载、默认及持久化初始化支持范围，再按依赖推进 REPLACE-03 的基础节点迁出和运行/加载退出。旧专用实现、store、数据和测试尚未退役，不以本轮新面板代替全架构独立验收。

未提交或推送；未开展 DEMO-05/06、COND、LATER 或暂停项。固定基线 SHA256 仍为 `0690253BEC23A2E377245E6F570B2814C3320CEA2C384A4DE927534678C6FCC4`。

本轮关键代码 SHA256：

```text
0F177B489D3B5655F59CD5B0C35A1594C555A3A7BC3EADED5390DE93F32094FC  backend/src/phase1_agent/prompt_package.py
95EEF609AB5E75844B7A1DC480349CD12DBB712502AAE7BB23C2F9458755D85B  frontend/src/App.vue
9CFC6365246697EF9820A22D73A9C4A142FDECDEEDBC6E7736A91287DB866DB4  frontend/src/application/workflowFrontendPackage.ts
349F11B4208789FEA276022A36D8503B1C3823AA768F8AA3DF64829A4276B780  frontend/src/domain/workflowPromptResources.ts
3580A18E72A29CFC02D85A6F6E890C97E522E56433F82743C96C7A49B9131EC3  frontend/src/adapters/promptResourcesApi.ts
2CB1EB545B7A77BD7AC7D1CB4952D80A46D87D3334B9370E2525B3017FF5208B  frontend/src/application/promptResources.ts
05CA99661B6E65818F883F9EAFDF5B7B41BA15B07AAA83CA142649A0E4EAA119  frontend/src/components/CurrentPromptPanel.vue
8AADB15E7AD5224F72FF0851AF937665BA3049528E13A453E6C1DE49D1BD6B00  frontend/src/components/PromptReferenceFields.vue
13F6BC42685796611B64FA5108E970F65564FC7967DD7379777257C84983ABDB  frontend/src/components/ContentSidebar.vue
34EE2A5D81420F618C814996A40448DC57C5CF2AD897F0B79CB69A71CE57B294  frontend/src/plugins/promptFrontendManifest.ts
F572D9D42F173AA9A95E56AAA2BC9191CE811518F2CC3EE2E9AF6357408CEF11  frontend/src/plugins/promptFrontendPackage.ts
```
