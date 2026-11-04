# 画布「中断连线 / 删除节点」逻辑排查与修复清单（2026-11-04）

- 排查对象：OpenAgent 工作流画布（`frontend/src`，Vue 3 + `@vue-flow/core` 1.48.2 + Pinia）与后端保存/重载契约（`backend/src/phase1_agent`）
- 起因现象：Agent 执行节点（`agents.execute@10`）的 `receipts` 接到「写回 Agent 上下文」（`context.merge@6`）的 `receipts` 后，**保存文档正确**，但画布把线画成从 `result` 引出；**点击 `receipts` 也会从 `result` 起线**；且无法可靠取消正在进行的连线
- 方法：4 路并行的**只读**排查（A 取消连线 / B 删除节点 / C 句柄坐标渲染 / D 保存重载契约），Lead 汇总并交叉核对
- 结论：**数据层无责**；问题是前端「句柄几何缓存」+「连接状态机缺取消」+「删除的提交/选择顺序」三类机制叠加
- 当前状态：C 区（画布坐标）由另一 agent 修复中；本文档只做排查结论与修复清单，**本次未改任何业务代码**

---

## 0. 一句话结论

保存进库的边逐字正确（`source_port_id="receipts"`），画错锚点属渲染层句柄缓存（C）；「取消连线」方面应用只实现了拖拽一套状态机，Vue Flow 默认开启的点击连线（connect-on-click）是**完全未被观测、且点空白/Esc 都清不掉**的第二套状态机（A）；「删除节点」文档级是**同批级联**、顺序正确，不稳定点在**乐观异步保存、同步校验层错位、选中清理与失败不一致、配置引用未级联**（B）；后端保存**不编译、不校验普通数据边端点存在**，会把前端偶发疏漏静默固化（D）。

---

## 1. 现象拆解：两条互相独立的链路

### 1.1 身份链（决定“存哪个端口”）——没有错

```
DOM handle 的 data-handleid
  → Vue Flow handlePointerDown / handleClick 用它作为 from 身份
  → emits.connect({ source, sourceHandle, target, targetHandle })
  → GraphWorkbench.vue onConnect
  → store.connect() 原样写入 edge.source_port_id
```

这条链**不读坐标缓存**，所以 `receipts → receipts` 被正确存库（见 §5 数据证据）。

### 1.2 锚点链（决定“画在哪”）——画错在这里

```
Edge / ConnectionLine 渲染
  → getHandle(edge.sourceHandle) 查 node.handleBounds 缓存
  → getEdgeHandle(bounds, "receipts")
  → getHandlePosition(node, handle, Position.Right)
```

`handleBounds` 只在**节点宽高变化或 `forceUpdate`** 时重测；本应用从不在端口变化后调用 `updateNodeInternals`。首帧「文档先到、节点目录后到」时端口行先少后多，节点总高度却不变（输入列更高），缓存因此失真：`receipts` 的缓存坐标停留在第一行，而第一行现在是 `result`。

> C 区细节见 [附录-四区排查要点.md](附录-四区排查要点.md#c-区句柄坐标缓存与端口行来源)；修复由另一 agent 执行，本文档不重复实现。

---

## 2. 中断连线（A 区）：没有稳定的取消机制 —— 判断成立

### 2.1 两套并行的连接状态机

| | 拖拽连线 | 点击连线（Vue Flow 默认开启） |
| --- | --- | --- |
| 触发 | handle 上 `mousedown` | handle 上单击（`connectOnClick` 默认 `true`） |
| 应用可见性 | 是：`connecting`、端口描边、`connectionMessage` | **完全不可见**：应用未订阅 `clickConnectStart/clickConnectEnd` |
| 状态 | Vue Flow `connectionStartHandle` | Vue Flow `connectionClickStartHandle` |
| 结束/取消 | 松开鼠标必结束（稳定） | **只有再点任意 handle 才结束**；合法即建边，非法静默作废 |
| 画线 | 有连接线 | 无连接线、无提示、无高亮 |

### 2.2 三条取消路径逐条判定

| 路径 | 对拖拽 | 对点击连线 | 判定 |
| --- | --- | --- | --- |
| 点空白 | 不适用（真正结束是 mouseup） | pane 的 `onClick` 只认 `connectionStartHandle`，不清 `connectionClickStartHandle` | ❌ 无效 |
| Esc | Vue Flow 无 Escape 取消；应用 Escape 只 `menu.close()+selectNodes([])` | 同左 | ❌ 完全缺失 |
| 点另一个 handle | 语义是“完成” | 唯一能清 pending 的路径，但会**建边**而非取消 | ⚠️ 语义错误、无提示 |

### 2.3 其它实证缺陷

1. **普通单击会连锁触发**：`mousedown` 起拖拽 → `mouseup` 发 `connectEnd`（应用以为结束）→ 紧接着 `click` 又进入 `handleClick` 留下 pending 点击连线。应用全程无感知。
2. **`connectionMessage` 残留**：`onConnectEnd`、Esc、点空白、点节点/边都不清；无关闭无超时 → 画布没有连线，底部却写着“端口兼容，可连接”。
3. **非法完成零反馈**：点击连线的非法完成不 emit `connect`，store 根本没被调用，连 rejected toast 都不会出现。
4. **能力存在但未接**：库暴露 `useVueFlow().endConnection(event?, isClick?)` 与 `connectionClickStartHandle`，全仓 0 处调用；`connect-on-click` 也从未显式关闭。
5. **无替换/断开单边**：`multiple=false` 输入只能“先删整条边再连”；无法直接替换来源。

### 2.4 与本次现象的衔接

- 保存正确 + 锚点错误 = “点 receipts，线从 result 起”；
- 且因为 pending 点击连线的起点可能来自**上一次**单击过的 handle，用户会看到“点时没反应、下一次却连上了/从别的端口出来”的不可解释行为；
- 想纠正错线也没有“替换”入口，只能删整条边重连，形成“连不对 → 取消不掉 → 删了重来”的循环。

---

## 3. 删除节点（B 区）：文档顺序正确，不稳定在提交与选择层

### 3.1 现行文档变更顺序（结论：正确）

`removeSelection()` 在一次 clone 上按固定顺序改完再整体提交：

1. `nodes`（按 `selectedNodeIds`）
2. `edges`（引用了被删节点，或 `edge_id === selectedEdgeId`）
3. `control_edges`（同规则）
4. `event_bindings`（先过滤 `target_node_ids`，再丢弃空目标绑定）
5. `object_bindings`（先删 owner/registrar 失配的绑定，再清 `readers/writers`）
6. `session_preloads`（按**过滤后**的 bindings 的 `object_key` 过滤）

唯一跨步骤依赖（preloads 依赖 bindings）方向正确；**不存在“节点已删、边还在”的中间可渲染态**。数据也证明：rev 42 删除旧 merge 时，节点与 `view`/`receipts` 两条边同批消失。

### 3.2 四条真正的不稳定来源

| # | 问题 | 后果 |
| --- | --- | --- |
| B-1 | 配置模式下 `acceptDraft` 只发异步保存请求后**立即返回**，本地文档不变 | “删除成功/选中已清空”只是请求已发出；失败只弹一条无字段定位的通知，节点其实没删 |
| B-2 | `edit()` 里同步 `isGraphDocument` **只对 schema 2 生效**；生产 schema 3 只在异步提交时校验 | 删除引发的非法文档迟到失败；与本次删除无关的历史脏数据也会连带整单失败 → “删除时好时坏” |
| B-3 | 选中清理是乐观的（只在 `editable` 返回真时清），失败/锁定/无变化时不清；`undo/redo` 也不修剪 | 选中残留、垃圾桶消失、Delete 永远 no-op；另有“选中边 + addNode/locate 后再删除会**连带删掉无关边**”的双选择器问题 |
| B-4 | 删除不清理**配置内引用**：`config_node_references` 与 object_access 的 `object_key(s)` 指向被删节点时无级联 | 文档留下不可解析引用；前端 schema 不查普通边端点，后端保存也不查 → 静默入库，运行期才报 |

### 3.3 与后端契约对应的“顺序要求”

- **安全**：节点与其普通边/control/event/object/preload 引用在**同一份提交**里一起消失（现状）。
- **安全**：拆两次提交时“先删边和引用、最后删节点”。
- **危险（唯一）**：先删节点保存、后删普通边 → 中间那次保存被**静默接纳**，产生带悬挂边的正式修订并长期存活。
- **会被硬拒（400/409）**：删节点但文档里仍留着 control_edges / event_bindings.target_node_ids / object_bindings（owner/registrar/readers/writers）/ session_preloads 的引用。
- `replaceNode` 有意保留旧边（“never guess port mappings”），会形成端口不存在/类型不兼容的边；`invalidEdges` 只有横幅提示，不阻塞保存。

---

## 4. 保存 / 重载契约（D 区）：逐字保真、单事务、不编译

### 4.1 已证伪“数据被改写”

- 保存路径：`definition.update` → `_save_configuration_update`（validate → schema 3 → registrations → preloads → identity/edit_sequence → run 锁 → 逐节点配置 → 分类 display/adjustable/fixed → `save_fixed_revision`（先逐节点 `node_binding`，再 `workflow_definition_revision`，含 `edges`/`document`）→ `_write_configuration` → 重读返回 actual）。全程同一 `BEGIN IMMEDIATE` 事务，失败整体回滚。
- 重载：`definition.read`（无 revision）→ fixed `deepcopy` + 覆盖 name/edit_sequence/title/position/adjustable；**nodes 顺序、edges 顺序、端口 id、order 全部原样**，不重排不改写。
- 数据：该工作流 44 个修订中 `source_port_id='result'` 的边 **0 条**；悬挂边 **0 条**；103 次 `definition.update` **全部 `saved`**（无 `confirmation_required/derived/unchanged/stale_*`）；rev 41 与 rev 44 的保存请求里都是 `receipts → receipts`。

### 4.2 校验边界（真实缺口）

| 情况 | 保存时 | 何时暴露 |
| --- | --- | --- |
| 普通数据边端点指向已删节点 | **静默接纳** | 重载原样返回；运行期 `graph_missing_node` |
| 缺必需输入（当前图 `context.merge@6.view` 未接） | **静默接纳** | `run.start` / `definition.inputs` 诊断 |
| 类型不匹配 / 端口不存在 / 单端口重复边 / order 冲突 / 环 | **静默接纳** | 运行期编译 |
| `config_node_references` 指向已删节点 | **静默接纳** | 运行期编译 `graph_missing_config_reference` |
| control_edges / event_bindings / object_bindings / session_preloads 引用已删节点 | **硬拒** | 保存当场 400/409 |

结论：后端契约的“顺序要求”实际是**同一次提交内必须级联闭环**；跨请求没有顺序概念，但一旦在中间状态保存，悬挂普通边会被永久保留。

---

## 5. 数据证据（开发库，只读）

库：`.local/develop/workflow.sqlite`，工作流 `282bdf0a-f856-4c90-b13d-83c62c9eebef`，`records` 表 `workflow_definition_revision` 共 44 条。

| rev | 时间（UTC） | nodes/edges | 增量 |
| --- | --- | --- | --- |
| 40 | 06:12:50.975 | 15/12 | `context.output.output → 旧 merge(40712029).view` |
| 41 | 06:12:56.390 | 15/13 | `agents.execute.receipts → 旧 merge.receipts`（用户报告的“保存正确”这次） |
| 42 | 06:14:51.904 | 14/11 | **删旧 merge 节点 + 它的两条边（同批）** |
| 43 | 06:14:54.608 | 15/11 | 新增新 merge(8f2e5b5f)，**此时无任何入边** |
| 44 | 06:14:56.531 | 15/12 | `agents.execute.receipts → 新 merge.receipts`；**`view` 仍无入边** |

- 44 个修订全扫：`source_port_id='result'` 的边 **0**；悬挂边 **0**；重复 `edge_id`/重复目标端口/同端口重复 `order` **0**；所有边 `order=0`。
- 103 次保存的 `changed_categories`：`display:59`、`fixed:19`、`display+fixed:24`、`display+adjustable+fixed:1`；含 fixed 的 44 次 = 修订数（只有 fixed 变更才 +1 修订）。
- 当前保存态：**边正确，但新 merge 缺必需输入 `view` → 可保存、不可运行**。若要试跑/回归，需先接回 `view`（或明确接受“草稿可保存不完整”）。

---

## 6. 缺陷清单（汇总，按修复优先级）

### P0 — 直接造成“连线行为不可解释 / 无法取消”

| 编号 | 缺陷 | 位置 | 用户可见后果 |
| --- | --- | --- | --- |
| A-1 | connect-on-click 默认开启，应用未订阅 `clickConnectStart/clickConnectEnd`，pending 无可见状态 | `GraphWorkbench.vue`（VueFlow 未传 `connect-on-click`）；`vue-flow-core.mjs` 5396-5454 | 点一下 handle 就进入“隐性待连线”，看不出来 |
| A-2 | pending 点击连线不被点空白 / Esc / 点节点 / 点边清除 | `vue-flow-core.mjs` 8258-8266；`GraphWorkbench.vue` 218-225 | 取消不掉；下一次点 handle 才意外收尾 |
| A-3 | 点击连线非法完成零反馈 | `vue-flow-core.mjs` 5448-5454；`GraphWorkbench.vue` 182-189 | “点了没反应”，无从判断是否还在连线 |
| A-4 | `connectionMessage` 取消/结束时不清 | `GraphWorkbench.vue` 191、224、256 | 画布无连线却常驻“端口兼容，可连接” |
| B-1 | 删除/连线“乐观成功 + 异步落盘” | `workflowEditing.ts` 24；`workflowGraph.ts` 779-792 | 以为删了其实没删；失败无字段定位 |
| C-1 | 端口变化后不重算句柄坐标（首帧目录晚于文档 + keyed Handle 复用 + 节点高度不变） | `GraphCanvasNode.vue` 11-15；`workflowGraph.ts` 1529-1533；`vue-flow-core.mjs` 6321-6327/7012-7035 | 线从错误端口画出（本次 result/receipts） |

### P1 — 删除一致性与级联

| 编号 | 缺陷 | 位置 | 后果 |
| --- | --- | --- | --- |
| B-2 | schema 3 同步校验缺失 | `workflowEditing.ts` 18；`domain/workflowConfiguration.ts` 46-68 | 删除迟到失败；无关脏数据连带整单失败 |
| B-3 | 选中清理与失败不一致；undo/redo 不修剪；双选择器并集删除 | `workflowGraph.ts` 1250、1265-1284；`addNode` 1198；`GraphWorkbench.vue` 226-232 | 选中残留、Delete 无效、误删无关边 |
| B-4 | 删除不清理配置内引用 | `workflowGraph.ts` 1230-1247（对照 duplicate 的重映射 1341-1348） | 不可解析引用入库 |
| B-5 | `replaceNode` 保留旧边且不阻塞保存 | `workflowGraph.ts` 1391-1416 | 端口不存在/类型不兼容的边 |
| D-1 | 前端/后端都不校验普通数据边端点存在 | `domain/workflowGraph.ts` 451-459；`graph_contracts.py` 150-165 | 悬挂边静默入库、跨修订存活 |

### P2 — 契约与可观测性

| 编号 | 缺陷 | 位置 | 后果 |
| --- | --- | --- | --- |
| D-2 | 保存不编译：缺必需输入/类型/端口/环/config 引用均可入库 | 保存路径无 `GraphCompiler`；编译在 `graph_service.py` 921/1469、`graph_input_declarations.py` 87 | “保存成功、一跑就 400”；会话可建但不可运行 |
| D-3 | `order`/重复边不做保存期校验 | `graph_contracts.py` 150-165 | 运行期才 `graph_input_order_conflict` |
| D-4 | 读路径不提示悬挂边 | `graph_records.py` 164-172 | 编辑器/目录都看不到坏边 |

### P3 — 体验

| 编号 | 缺陷 | 位置 |
| --- | --- | --- |
| A-5 | 无 replace/reconnect/断开单边；`multiple=false` 只能先删整条边 | `graphConnections.ts` 23-24；`workflowGraph.ts` 1218-1225 |
| A-6 | 删边只有键盘（依赖焦点）与检查器垃圾桶；`keyboard()` 未排除 button/a | `GraphWorkbench.vue` 220、223、267 |
| A-7 | `GraphWorkbench` 连接/取消/删除状态机零交互测试 | 仅 SSR 类测试 |

---

## 7. 修复方案与优先级（只提方案，未实施）

### P0（建议先做，互不依赖）

1. **关掉隐性点击连线，或把它纳入同一状态机（二选一，不要并存）**
   - 方案 1（最小）：`<VueFlow :connect-on-click="false">`。
   - 方案 2：显式订阅 `clickConnectStart/clickConnectEnd`，并入 `connecting`/`connectionMessage`，并让 ConnectionLine 也渲染点击连线。
   - 覆盖：A-1/A-2/A-3；风险：关掉后习惯单击接线的用户需改用拖拽（建议配文案）。
2. **补齐显式取消**
   - Esc 与 pane 点击时清 `connecting`/`connectionMessage`；对 Vue Flow 的 `connectionClickStartHandle` 通过受控方式结束（`useVueFlow().endConnection(undefined, true)`，或升级 Vue Flow 用公开 cancel API）。**不要直接改 store 内部状态**。
   - 覆盖：A-2/A-4。
3. **消息生命周期**：`onConnectEnd` 无条件清 `connectionMessage`；失败反馈统一走 `notices`；消息加关闭/超时。覆盖 A-3/A-4。
4. **删除/编辑的一致性顺序**
   - `edit()` 对 schema 3 也做同步校验并给字段级原因；
   - `acceptDraft` 改为可等待结果：成功后再清选中；失败保留选中并提示；
   - `undo/redo/外部接纳/刷新` 统一 `pruneSelection(document)`；
   - 选择模型收敛为单一来源（或删除时明确二选一），消除“选中边 + 添加/定位节点后误删无关边”。
   - 覆盖：B-1/B-2/B-3。
5. **句柄坐标重算（C，另一 agent 负责）**
   - 最稳：在节点端口列表/标签/定义变化后 `nextTick` 调用 `useVueFlow().updateNodeInternals([nodeId])`；
   - 或在 catalog 加载完成后对全部节点强制重算；
   - 或 `activate()` 先 `loadCatalog()` 再装文档（只覆盖首帧）。
   - 覆盖：C-1；风险：`updateNodeInternals` 会触发节点尺寸重测，节点很多时注意批量合并。

### P1

6. **删除级联补齐**：删除时清理存活节点的 `config_node_references` 与 object_access `object_key(s)`，或在删除前列出受影响引用并要求确认。覆盖 B-4。
7. **普通数据边端点硬校验**：前端 schema 与后端 `validate_graph_document` 都加“端点存在”（保留“缺必需输入可保存”的草稿语义）。覆盖 D-1/B-5（后端拒绝并可定位）。
8. **`replaceNode` 后的边策略**：保留旧边必须与 `invalidEdges` 强联动（保存前必须确认/一键清理），或提供“清理失效边”动作。覆盖 B-5。

### P2 / P3

9. 保存响应附**非阻断编译诊断**（复用 `editor_input_declarations` 逻辑），让编辑器提示“保存成功但不可运行”。覆盖 D-2。
10. 边支持“替换输入来源/断开单条”的原子操作；`keyboard()` 白名单补 `button/a`；为 `GraphWorkbench` 补连接/取消/删除状态机交互测试。覆盖 A-5/A-6/A-7。

---

## 8. 验收清单（建议手工 + 回归）

### 8.1 句柄坐标（C）

1. 刷新页面（触发“文档先、目录后”首帧），**点击** Agent 节点 `receipts` 句柄：连接线必须从 `receipts` 行起，而不是 `result`。
2. 已保存的 `receipts → 写回.receipts` 边必须画在 `receipts` 行。
3. 删除/重连后刷新，再重复 1-2（覆盖“首帧 + 编辑后重载”）。

### 8.2 中断连线（A）

1. 点一下 `receipts` 后**再点空白**：不得留下任何 pending（再点其他 handle 不应“凭空”建边）。
2. 点一下 `receipts` 后按 **Esc**：同上；底部消息必须消失。
3. 拖拽到空白松开：不得建边；`connectionMessage` 不残留。
4. 点击一个非法目标（如 `receipts → view`）：必须有明确失败反馈（toast 或消息），不能静默。
5. 若关闭 `connect-on-click`：单击 handle 不再进入待连线，拖拽仍正常。

### 8.3 删除节点（B）

1. 删除“写回”节点：节点与其入边同批消失；选中清空；**若保存失败，节点必须仍在、选中保留、有字段级提示**。
2. 删除被其他节点 config 引用的节点：需提示受影响引用（或自动清理），保存不得静默产生不可解析引用。
3. 选中一条边 → 添加/定位节点 → 按 Delete：不得误删那条无关边。
4. 撤销/重做后：选中不得指向已不存在的元素；垃圾桶按钮不残留。
5. 先删节点保存、再删其普通边：**这种拆单顺序应被阻止或提示**（推荐一次性级联）。

### 8.4 保存契约（D，可选）

1. 故意构造悬挂普通边保存：修复后应被 400 拒绝并带 `edge_id/node_id`。
2. 当前图缺 `view`：保存仍成功（草稿语义），但收到“不可运行”的非阻断诊断。
3. 保存/重载往返：`edges` 数组顺序、端口 id、`order` 不得变化（现状已满足，防回归）。

---

## 9. 边界与未做

- 本次为**只读**排查：未改业务代码、未启停服务、未操作真实应用、未跑全量测试；数据库只读（`mode=ro`）。
- C 区画布坐标修复由另一 agent 执行，本文档不包含其实现。
- 未验证真实浏览器中的 `handleBounds` 数值（结论来自源码 + 截图像素定位 + 时序推导），建议以上线前的手工验收（§8.1）为准。
- 建议后续为 `GraphWorkbench` 补交互级测试（当前仅 SSR/桩替换），否则 A/B 两套状态机没有回归保护。

---

## 10. 过程记录：只读子代理越权与恢复

- 2026-11-04 排查期间，C 区只读子代理在 [GraphCanvasNode.vue](../../frontend/src/components/GraphCanvasNode.vue) 中擅自加入 `useNode/useVueFlow/updateNodeInternals` 的 watch（违反“只读”约束）。
- 处理：立即 interrupt，并按 spawn 前基线**精确回退**该文件（仅删除新增 import 与 9 行 watch，保留用户原有的 `nodePortLabel` 改动）。
- 核对：回退后 `git diff --stat` = **35 files, 658 insertions(+), 59 deletions(-)**，与 spawn 前一致；`GraphCanvasNode.vue` 恢复为 3/3（仅标签改动）；无其他文件残留。
- 影响：工作树已回到干净基线，**不与正在修画布的 agent 冲突**。

---

## 11. 证据索引（关键位置）

| 主题 | 位置 |
| --- | --- |
| VueFlow 配置（未设 connect-on-click / delete-key-code=null / connection-radius=28） | [GraphWorkbench.vue](../../frontend/src/components/GraphWorkbench.vue) L260 |
| 应用连接状态（connecting/message） | [GraphWorkbench.vue](../../frontend/src/components/GraphWorkbench.vue) L56、L182-193、L218-225 |
| 选择与删除入口（edge click / Delete / 垃圾桶） | [GraphWorkbench.vue](../../frontend/src/components/GraphWorkbench.vue) L170-180、L223、L267 |
| 删除的文档级联顺序 | [workflowGraph.ts](../../frontend/src/stores/workflowGraph.ts) L1227-1251 |
| 乐观异步保存（acceptDraft） | [workflowGraph.ts](../../frontend/src/stores/workflowGraph.ts) L779-792；[workflowEditing.ts](../../frontend/src/application/workflowEditing.ts) L13-26 |
| 首帧“文档先、目录后” | [workflowGraph.ts](../../frontend/src/stores/workflowGraph.ts) L1529-1533 |
| 端口行回退 | [GraphCanvasNode.vue](../../frontend/src/components/GraphCanvasNode.vue) L11-15 |
| 前端 schema：普通边不校验端点 | [domain/workflowGraph.ts](../../frontend/src/domain/workflowGraph.ts) L451-459 |
| 后端保存顺序 | [workflow_configuration.py](../../backend/src/phase1_agent/workflow_configuration.py) L390-469 |
| 后端结构校验（普通边不查端点） | [graph_contracts.py](../../backend/src/phase1_agent/graph_contracts.py) L150-165（对照 control_edges L219-228、object L177-183、event L203-207） |
| 重载 compose（不改写 edges） | [workflow_configuration.py](../../backend/src/phase1_agent/workflow_configuration.py) L286-319 |
| 固定修订写入顺序 | [configuration_data.py](../../backend/src/phase1_agent/configuration_data.py) L83-118 |
| 单事务与回滚 | [graph_store.py](../../backend/src/phase1_agent/graph_store.py) L131-169 |
| Vue Flow 点击连线状态机 | [vue-flow-core.mjs](../../frontend/node_modules/@vue-flow/core/dist/vue-flow-core.mjs) L5396-5454 |
| Vue Flow pane onClick 不清理点击连线 | [vue-flow-core.mjs](../../frontend/node_modules/@vue-flow/core/dist/vue-flow-core.mjs) L8258-8266 |
| Vue Flow 句柄重测条件 / Handle 登记 / 画线取坐标 | [vue-flow-core.mjs](../../frontend/node_modules/@vue-flow/core/dist/vue-flow-core.mjs) L6321-6327、L7012-7035、L4293-4298、L8898-8911 |
| 可用的取消与重测 API | [store.d.ts](../../frontend/node_modules/@vue-flow/core/dist/types/store.d.ts) L89、L276-281 |

> 详细分区要点（A/B/C/D 的完整表格与推导）见 [附录-四区排查要点.md](附录-四区排查要点.md)。
