# P1：全局 Lorebook 实施记录

> 2026-10-08 历史快照：版本、入口、待办和验收数字保留登记时事实。当前酒馆操作见[酒馆说明](README.md)与[使用手册](../USER-GUIDE.md)，当前节点与分支状态见[文档索引](../README.md)；不按本页恢复旧节点路线。

日期：2026-10-08，Asia/Shanghai。用户在步骤 1 后授权继续，本批仅实现 P1，不开始 P2 / P3 / P4。

## 1. 结果与版本

全局 Lorebook 的资源、引用/触发节点和必要工作台 UI 已实现，定向验证通过。

- 新包为 `workflow.tavern@1.1.0`，新增代码位于 [后端酒馆子包](../../backend/src/phase1_agent/tavern/) 和 [工作台酒馆目录](../../frontend/src/plugins/tavern/)。
- `workflow.tavern@1.0.0` 继续作为可选择的精确版本提供，不给旧包原地增加全局语义。
- 新的无保存包配置项目默认选择 `1.1.0`；已有保存的包配置重开仍使用原版本，不自动升级。
- `lorebook.item/group@1` 的节点描述、内联配置和执行规则保持一致。共用注册函数只是供两个包版本复用原节点。
- 新包的内联字段扩展使用 `*-fields-v1-1` 身份；旧扩展身份仍保留。宿主禁止重复扩展 ID，因此不在前端注册表中用同一 ID 混装两个版本。
- 这是源码中的能力包版本，不是宿主发行版升级；没有更新常驻安装、打标签、构建独立安装包、提交或推送。

## 2. 资源和执行边界

全局数据类型为 `workflow.tavern.lorebook@1`，资源身份仍为完整的 `envelope_version / scope / type_id / resource_id`，不以名称或单独 UUID 匹配。

资源正文严格包含：

```json
{
  "name": "",
  "enabled": true,
  "entries": []
}
```

`entries` 复用既有 Lorebook 条目结构；一个成员就是单条目，多成员就是条目组，允许空组。成员 UUID 在重命名、修改正文和排序后保留。名称只用于管理，不参与触发。

- 资源中不能保存 `object_keys`、生命周期、概率结果或扫描状态。
- 变量对象授权仍配置在触发节点上，只有当前会话明确授权的 `workflow.variable@1` 对象可读。
- 全局资源保存沿用已有 CAS、幂等回执、纯回执查询和服务端大小限制。资源信封最多 `1,000,000` 字节，条目最多 1024 个。
- 引用和触发节点声明资源依赖，运行前检查存在、类型、schema 和资源启用状态；不符合时不会启动回合或改变会话。
- 执行通过宿主本轮冻结帧读资源，不在运行过程中重新查询变化的 current 头。
- 仍只扫描一个精确接受的 `PROMPT@6` 产物，输出 `PROMPT_MATERIALS@1`，固定 `per_request / never`。
- 每个触发节点独立计算扫描、单次概率和最多三轮递归；共享的是条目正文，不是计算状态。
- 读取证据记录完整资源身份和冻结 `update_sequence`，不记录变量正文。材料和触发诊断沿用既有接受产物记录。

资源修改只影响后续回合；已接受的材料、提示词装配和冻结运行历史不被覆盖。没有新增 Agent 上下文编辑或写入权限。

实现入口：[资源与节点](../../backend/src/phase1_agent/tavern/lorebook_resources.py)、[包注册](../../backend/src/phase1_agent/tavern/package.py)。

## 3. 节点接线与管理 UI

新增两个节点：

| 节点 | 配置 | 输入 | 输出 |
| --- | --- | --- | --- |
| `lorebook.global-reference@1` | 完整资源 `reference` | 无 | `output: GLOBAL_RESOURCE_REF@1` |
| `lorebook.global-activate@1` | 本节点 `object_keys` | `resource: GLOBAL_RESOURCE_REF@1`、`input: PROMPT@6` | `output: PROMPT_MATERIALS@1` |

接线方式：

```text
全局 Lorebook 引用.output -> 全局 Lorebook 触发.resource
扫描用 context.assembly@4.output -> 全局 Lorebook 触发.input
全局 Lorebook 触发.output -> 提示词汇总或最终装配.materials
```

扫描装配和最终装配使用不同节点，保持无环图；不将触发结果接回其自身扫描源。

工作台“内容”侧栏在精确 `1.1.0` 扩展可用时显示全局 Lorebook 面板，原提示词面板继续保留。提供新建、编辑、启停资源，以及条目新增/删除/排序、关键词、扫描、概率和呈现字段编辑。资源身份只读，节点引用通过专用选择器显式应用，触发节点独立配置只读变量绑定。

缺失引用不会自动改选首个资源或同 UUID 的其他 scope。节点配置沿用 SDK 的原配置和生命周期依据，锁定/过期时不能修改。无匹配包时不启用专用面板或字段接管。

保存原请求先持久化到独立 `workflow.tavern.lorebook.pending.v1` 命名空间。结果未知时禁止新的资源修改，重开保留原 body/key；核实仅查询原回执，不再次执行保存。核实成功后关闭旧表单，避免用旧 CAS 依据重复提交。

没有自动转换已有内联图、重绑旧冻结历史或导入 ST 世界书数据库。现有内联条目仍可继续使用；全局资源及新节点由显式创建、选择和接线形成。

## 4. 验证证据

| 验证 | 最终结果 |
| --- | --- |
| 后端定向测试 | 5 文件，75 项通过 |
| 前端定向测试 | 8 文件，89 项通过 |
| 类型和生产构建 | `vue-tsc --noEmit` 与 Vite build 通过 |
| 工作树空白检查 | `git diff --check` 通过 |

后端范围：`test_tavern_global_lorebook.py`、`test_tavern_package.py`、`test_tavern_integration.py`、`test_graph_package_selection.py`、`test_capability_packages.py`。新增全局专项共 15 项，包含保存/重放/CAS/重开、同 UUID 不同 scope、多图共享、局部变量授权、精确产物、类型预检、运行期间更新、旧选择重开、概率/递归隔离和离线实际工作流装配。

前端范围：酒馆目录 4 个专项文件，加 `LorebookFields.test.ts`、`lorebook.test.ts`、`workflowFrontendPackage.test.ts`、`CurrentPromptResources.test.ts`。覆盖实际挂载表单事件、双面板 SSR、旧版/新版扩展解析、节点 SDK 依据、资源 CAS 和 unknown/reopen/receipt-only/迟到响应/持久化失败保护。

原生 Agent 装配测试使用离线替身传输：两个全局触发节点的同文材料在模型输入中只装配一次，材料没有进入正常上下文写回的历史正文。没有真实模型请求。

首次测试因 Python 默认导入常驻旧安装、系统 pytest 临时目录权限失败，随后显式使用本仓 `backend/src` 和专项临时目录复核；未修改常驻 Python 安装。调试期间的失败夹具已修正，以上为最终定向结果，不累计中间重跑。

## 5. 未执行项与后续

- 独立数据库预览服务的后台启动被本机执行策略拦截，没有服务可用链接；浏览器联合验收未执行，不能以 SSR/挂载测试或生产构建替代。
- 仅针对本批已知临时目录的清理命令同样被策略拦截，未绕过限制。早期 `.codex-tavern-p1-*` 测试目录仍在仓库下，最终复核数据位于 `.local/tavern-p1-tests-*`；这些是测试产物，不属于产品代码或交付文件。
- 未做独立 wheel 安装/静态资源分发验证、部署、常驻服务切换、产品数据库操作、收费调用、提交或推送。
- 没有复制 ST 代码、修改 ST 克隆、引入 ST 依赖或更改宿主许可。聊天外壳、业务状态及分叉仍未开始。
- 没有执行全量后端/前端回归，也没有清理既有未关联测试债务。

下一批为 P2：全局提示词组。复用已有 schema 2 提示词组的资源、引用/解析和生命周期，仅补实际管理操作缺口。P3 / P4 后再进行独立聊天壳、静态资源和完成回合分叉的集中浏览器验收；本批浏览器限制也须在最终交付前补齐。
