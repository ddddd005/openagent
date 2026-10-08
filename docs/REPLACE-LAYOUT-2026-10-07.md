# REPLACE-02 普通图工作台布局修复与内容入口复核

> 历史切片：下文的实现、节点版本、未推送状态和验证结果指登记时快照，不是当前入口。最新使用与工程说明见[文档索引](README.md)，当前退役范围见[节点目录](NODE-DIRECTORY-2026-10-09.md)；不按本页重建旧 API。

本地验收日期：2026-10-07，Asia/Shanghai。承接 [供应商侧栏切片](REPLACE-PROVIDER-2026-10-07.md)、[首轮盘点](REPLACE-AUDIT-2026-10-07.md)和 [1.0 规划](PLAN-1.0.md)。用户指出前轮截图的工作流排布问题，本轮先修复该基础交互缺陷，不开展界面重设计或 1.0 后置能力。

起点与当前 HEAD 仍为 `00970f75fb52d741b694499a05f702600529fcdd`；以下证据对应该提交上的未提交工作区，固定基线不改。沿用三个子代理，只读复核布局、测试边界及下一内容切片；主代理负责复现、修改和实际验证。

## 1. 问题与最小修改

`App.vue` 的 `.workbench-canvas-region` 是横向 flex 容器；普通图的 `GraphWorkbench` 根没有 `flex:1` 和 `min-width:0`，默认 `flex:0 1 auto` 按内在内容宽度排布。内部详情面板固定 280px，画布只能取得窄根容器余下的宽度，右侧的大块空白属于未被根容器占用的空间，并非节点坐标或视图平移的预期效果。

本轮默认视口复现：可用区域宽 1235.4px，空普通图工作台仅 405.95px，画布 125.95px。新建无节点的隔离验收会话后，普通图历史页同样只有 336px，未占满父区域。

| 文件 | 修改 |
| --- | --- |
| [GraphWorkbench.vue](../frontend/src/components/GraphWorkbench.vue) | 仅在 scoped 根规则补 `flex:1` 与 `min-width:0` |
| [GraphHistory.vue](../frontend/src/components/GraphHistory.vue) | 对已复现的同类历史页问题补相同两项声明 |
| [GraphViewLayout.test.ts](../frontend/src/components/GraphViewLayout.test.ts) | 用既有 Vue SFC 与 PostCSS AST 解析器保护两个根规则、横向 body、弹性画布和固定详情面板 |

未修改侧栏比例、全局 grid、节点尺寸/坐标、VueFlow 逻辑、表单、运行或历史行为。旧 `PreparationWorkbench` 和 `WorkbenchCanvas` 根已有对应声明，不需要改全局布局来补偿普通图缺口。

## 2. 定向验证

先运行新增声明守卫：**2 failed / 1 passed**，两项失败准确指向两个根容器缺少声明；这是修复前的受控复现，保留该结果。守卫只验证源码声明，不能取代浏览器几何证据。

修复后，在 `frontend/` 执行：

```powershell
npm test -- `
  src/components/GraphViewLayout.test.ts `
  src/components/GraphV2Wiring.test.ts `
  src/components/GraphFrontend.test.ts `
  src/components/WorkbenchCanvas.test.ts `
  src/components/ProviderSidebar.test.ts `
  src/components/ProviderSidebarResources.test.ts `
  src/components/CurrentModelConfiguration.test.ts
npm run build
```

最终结果：**7 个文件，38 passed，2.38 秒，退出码 0**。包含新增三项；与供应商切片的用例存在重叠，不累加为 88 项或全量通过。

类型检查与 Vite 构建通过，退出码 0。仍保留已有单 chunk 大于 500 kB 的提示，不启动 LATER-09 分包专项。没有运行后端或前端全量。

## 3. 浏览器几何与交互证据

沿用前轮独立数据库 `.local/replace-provider-20261007-7d3e9c/verification.sqlite` 及 loopback 预览，未访问原运行库或改变常驻服务。只新建一个空验收定义及 idle 会话以打开历史页，没有启动运行或派发模型。

验收定义：`72b7d38b-32bf-4992-977d-77ba90716030`；idle 会话：`ba98a5f9-b93b-4bf5-914c-43d58c7a1c02`。没有修改已有图的节点位置或模型资源，没有删除记录。

使用内置浏览器 DOM 的 `getBoundingClientRect()`，以小于 1px 的误差检查根容器占满父区域、画布/详情相邻且不重叠、详情贴右、VueFlow 正尺寸并占满画布，以及无页面横向溢出：

| 桌面视口 | 可用区域 / 工作台宽 | 画布宽 | 详情宽 | 历史页宽 | 几何检查 |
| --- | --- | --- | --- | --- | --- |
| 1920 × 1080 | 1398px | 1118px | 280px | 1398px | 通过 |
| 1440 × 900 | 1038px | 758px | 280px | 1038px | 通过 |
| 1024 × 768 | 726px | 446px | 280px | 726px | 通过 |

补充实际操作：

- 三档视口均切换历史/主流程后复测，根容器及高度保持正确；历史验证是空 idle 会话的排布，不冒称完成历史内容联合验收。
- 最小桌面宽度下切到旧固定图，旧准备画布仍占满 726px 父区域。
- 返回已有 18 节点串行示例，打开供应商、选择模型源、定位全部节点及查看长配置；1440 与 1024 下实际截图无详情面板覆盖画布的问题。
- 1440 下详情内容 `clientHeight=760`、`scrollHeight=1128`，聚焦模型名后的 `scrollTop=368`；滚动内容仍限定在详情面板内。
- 恢复默认视口并重新加载，工作台宽 1235.4px、画布宽 955.4px、详情宽 280px，几何检查继续通过；当前资源 `s2` 与原模型配置保留。
- 本次采集的浏览器 error/warn 列表为空。临时 viewport override 已 reset。

截图保留在同一忽略目录：`layout-before.jpg`、`layout-after-1440.jpg`、`layout-after-1024.jpg`、`layout-after-default.jpg`。前轮供应商截图未覆盖，构建产物与本地运行数据不进入公开快照。

只验收当前支持的桌面宽度；手机适配、节点显示构型、长期规模、真实模型及全面故障专项均未开展。

## 4. 下一内容切片的协议边界

并行只读复核发现，“v2 全局内容”不足以描述新架构的资源缺口，需要区分三层：

| 层级 | 接口 / 类型 / 节点 | 当前事实 |
| --- | --- | --- |
| 旧修订库 | `/api/global-content`、`workflow.global-content@1` | 使用旧 revision、name/kind 与 store |
| 兼容 current | 类型 `workflow.global-content`、节点 `workflow.global-content@2` | 由 `workflow.compat` 注册；bare resource_id、固定 workspace，仍有旧 shape |
| 独立提示词 current | 类型 `workflow.prompt-resource`、`prompts.global-reference@1` / `global-resolve@1` | 使用完整 ResourceIdentity 与 `{enabled,members}`，明确拒绝兼容类型，不要求 compat |

关键源码：独立类型/schema/引用校验在 [prompt_package.py](../backend/src/phase1_agent/prompt_package.py)；当前 [GraphNodeConfiguration.vue](../frontend/src/components/GraphNodeConfiguration.vue) 仅按旧 component ID 读取旧目录，未区分兼容节点的 v1/v2；旧 [globalContent.ts](https://github.com/ddddd005/openagent/blob/7ff72afd1e31481e04f7b37b58de961bef7d823f/frontend/src/stores/globalContent.ts) 不适合直接复用为新的 unknown 控制器。

下一代码切片以**独立提示词 current 的管理与完整引用选择**为目标，补可信 panel/node-fields 声明与必要入口；兼容 @2 目录错误和执行支持范围另行处置，不能把修正兼容入口当作完成新架构退出。

实施前仍需明确显示名称、管理 scope、删除范围及旧数据去向；不在 schema1 暗加 name/kind，不自动转换类型、节点 ID 或包锁。旧草稿/pending 的正文、原键和路径必须保留，新控制器使用独立持久化键，并沿用后续拒绝不能结清先前 unknown 的保护。现有 import 只覆盖兼容资源，不冒称可导入独立 prompt 资源。

这些是 REPLACE-01/02 原范围内的继续盘点，不是内容切片已实施、通用转换已立刻启动或新增后置能力。

## 5. 当前状态

布局缺陷已在限定范围修复并验收；REPLACE-01/02 整体仍在进行，REPLACE-03–06 与完整 VERIFY 联合验收尚未完成。未提交或推送，未动 DEMO-05/06、COND、LATER 或暂停项。

本轮代码 SHA256：

```text
34DB2C7CDC765C856F4AAFA46A9F775E454A56279F2B1C20A5801D97F691AC66  frontend/src/components/GraphWorkbench.vue
174D9546711A478E472CA807401A25A5958232002425E5556A0A46B566267BCE  frontend/src/components/GraphHistory.vue
DF6C6D2FFC2544F24BD56227BB83F08CF0C66048DBD70B4A2E7697088258EE0E  frontend/src/components/GraphViewLayout.test.ts
```
