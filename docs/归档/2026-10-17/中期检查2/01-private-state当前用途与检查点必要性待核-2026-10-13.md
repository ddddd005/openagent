# private-state 当前用途核查与初版处理决定

日期：2026-10-13，Asia/Shanghai。

**性质：源码核查及初版范围决定；不是产品实现或删除决定。** 完整初版边界见[工作流数据保存规范与检查点初版方案](工作流数据保存规范与检查点初版方案-2026-10-13.md)。

## 1. 问题

当前规划将节点的配置字段与 private-state 字段分别纳入 `NodeDefinition` 保存声明，再由节点表单映射配置字段。需要确认：private-state 是否有当前业务节点实际使用；如果没有，是否仍应作为第一阶段最小检查点契约的一部分。

## 2. 当前代码事实

- `NodeDefinition` 定义了 `private_state_schema` 与 `private_state_default`，通用默认值为空对象 `{}`，并对默认值和读写值做 schema 校验。见 [graph_contracts.py](../../../../backend/src/phase1_agent/graph_contracts.py#L317)。
- 执行上下文提供 `private_read()` / `private_write()`；状态按节点绑定读取和更新。见 [graph_execution.py](../../../../backend/src/phase1_agent/graph_execution.py#L125)。
- 会话服务从 `node_session` 读取、写入节点 private payload。见 [graph_service.py](../../../../backend/src/phase1_agent/graph_service.py#L328)。
- 当前查到的明确非空示例位于测试插件：`content-probe` 节点维护 `count`，每次执行加一，默认值为 `0`。见 [graph_test_plugin.py](../../../../backend/tests/graph_test_plugin.py#L28) 及其 schema/default 声明 [同文件](../../../../backend/tests/graph_test_plugin.py#L58)。这是测试用途，不能单独证明产品节点有此需求。
- 本次静态检索未在 `backend/src` 中找到明确声明非空 private-state 字段的业务节点。此结论限于当前源码检索，不代表运行时数据库或外部插件中不存在相关状态。

## 3. 当前结论

目前能确认的是：**private-state 机制存在，但当前业务用途尚未证实，疑似没有已接入的业务节点依赖它。** 不能仅凭机制字段和测试计数器就把它当成已确认的业务保存需求。

private-state 与提示词正文、用户编辑的节点配置、独立注册的数据类型不同。若业务节点将来确有仅归该节点所有的运行状态，可由其 schema/default 和运行接口表达；这不意味着该状态天然属于检查点，检查点纳入仍须由最终声明契约决定。

## 4. 初版决定与后续判断

1. **第一版不处理 private-state**：不增加检查点声明字段、不收集、不恢复、不随新分叉写回；现有运行时 schema/default、读写和会话持久化行为暂不修改。
2. **是否移除机制留待后续判断**：初版完成后，结合真实业务节点用途再决定保留、改造或删除，不以本次范围直接删除 `NodeDefinition` 字段或运行时 API。
3. 测试插件的 `count` 仅作为机制测试，不作为产品必须保存 private-state 的依据。

本记录不表示任何产品代码已改，也不改变当前运行时 private-state 行为。首版检查点和分叉范围按所链接初版方案执行；本次未移动、删除或修改任何用户数据。
