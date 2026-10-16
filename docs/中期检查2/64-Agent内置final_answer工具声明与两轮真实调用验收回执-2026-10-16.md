# Agent内置final_answer工具声明与两轮真实调用验收回执

日期：2026-10-16，Asia/Shanghai。

**状态：Agent内部工具声明修复完成，当前链路9个测试文件136项离线回归通过；开发服务重启后，用户明确确认两轮真实模型调用成功。用户据此授权提交、并入main并推送。本文记录实施与验收事实，不提前声称Git提交、合并或远端推送已成功。**

## 1. 原因与批准范围

原失败发生在`agents.execute@10`：模型先返回正常中文正文，随后两次调用`final_answer`时均使用`answer.answer`，不符合Agent实际要求的`answer.text`，最终触发`invalid_final`。

当时模型收到的工具参数仅声明`answer`为对象，没有携带Agent的完整输出Schema。内核已存在工具注册、参数校验和最终结束处理，本轮不另建执行机制。

用户批准将工具用途说明和工具参数说明作为两类信息，在Agent节点内部先补齐内置工具声明。不增加工作流工具端口、工具配置页面、插件发现或动态加载接口。

## 2. 实际改动

- [tools.py](../../backend/src/phase1_agent/tools.py)：`final_answer_tool`允许接收输出Schema，用同一份不可变参数声明提供模型侧说明和工具参数校验；未绑定Schema的既有调用保持原声明。
- [agent_executor.py](../../backend/src/phase1_agent/agent_executor.py)：Agent内部装配内置`final_answer`，绑定现有`AGENT_OUTPUT_SCHEMA`；即使传入的内部工具列表没有最终工具，也补入该内置工具。其他工具实现保持不变，重复工具声明仍交给既有内核校验。
- 工具用途通过原生声明的`description`提供，参数说明通过`parameters`提供，随实际请求和快照留存，不向用户提示词或历史上下文插入额外说明。
- 内核仍负责严格校验、调用事实和本轮结束。未放宽输出校验，未增加纠错或模型重试次数，未修改工作流定义格式、数据库结构和模型通信协议。

当前有效最终工具参数：

```json
{
  "answer": {
    "text": "面向用户的完整正文"
  }
}
```

## 3. 离线验证

新增[test_agent_builtin_tools.py](../../backend/tests/test_agent_builtin_tools.py)，共14项，覆盖声明与校验一致、不可变Schema、内置工具自动装配、其他工具保留、错误字段拒绝、实际离线请求、两轮上下文、检查点和分叉、既有纠错及持续错误后的停止。

最终当前链路联合回归：**136 passed，0 failed，0 errors，0 skipped**。准确选择如下，重叠测试结果不累加：

```text
tests/test_agent_builtin_tools.py
tests/test_stage4_model_request_installation.py
tests/test_model_execution_contracts.py
tests/test_model_execution_requests.py
tests/test_context_registration_fork_integration.py
tests/test_workflow_chat_integration.py
tests/test_frontend_node_operations.py
tests/test_current_node_directory.py
tests/test_prompt_package.py
```

本机证据：`.local/test-runs/agent-builtins-current-20261016-8ec46300/`，包含`stdout.txt`、`junit.xml`及隔离测试目录。模型返回均来自测试实现，新增测试禁止网络连接，默认产品库和真实凭据未用于离线测试。

扩展回归另遇到历史测试不兼容。本轮将两份修改前源码从基准提交`4c67bbb0e416092ce7ebd1aef1e8a42c8940bfa7`装载到独立测试进程内存核对，未回写产品源码：

- `test_runtime_model_requests.py`：5项setup错误，仍向已退役入口传入`release_run`。
- `test_prompt_lifecycle.py`：14项失败，仍使用旧版上下文格式调用当前装配入口。
- `test_tool_independence.py`：1项失败，仍传入已退役的`max_model_requests`。

上述20项在修改前基线复现，未在本轮顺带修复。基线核对组结果为37 passed、15 failed、5 errors，证据位于`.local/test-runs/agent-builtins-baseline-20261016-4a81cc3a/`。不宣称历史全量测试绿色。

## 4. 服务与真实调用验收

用户授权重启后，开发服务使用正常develop源码和独立业务库`.local/develop/workflow.sqlite`重新启动，前端5179、后端8766。已核对旧启动进程全部退出、新服务身份、页面HTTP 200、后端及前端代理health正常。新启动记录为`.local/service-starts/20261016-110843-3db31b0f/services.json`。

停服过程遇到旧PowerShell读取启动记录的编码问题，以及进程退出期间的身份校验竞态；未绕过身份检查、未终止不匹配进程，最终核对旧进程均已退出后完成新服务检查。启动和停止脚本未修改。

随后用户明确报告：“真实调用完成，成功两轮模型真实调用。”本轮据此记录为**用户确认的两轮真实调用成功**；代理未额外发送模型请求，未将该反馈扩展为所有真实调用事实、检查点及浏览器操作的独立审计结论。

## 5. 提交与晋升边界

本批提交范围为上述两个源码文件、新增测试、本文，以及用户随后追加授权的[使用手册](../USER-GUIDE.md)和README文档入口更新。之前的61至63号架构梳理文档保留本地，不混入本批提交。

用户批准在develop提交、将main快进到同一提交，再普通推送远端main。不强制推送、不推develop、不附带标签、不更新归档工作区，也不启动main服务、不迁移业务库、不修改凭据或重放失败请求。实际Git提交、快进与远端结果以执行后的核对和本轮报告为准。

## 6. 使用手册补充

用户随后明确要求使用子代理制作使用手册，重点包括变量注册、会话绑定，以及展开模型预设配置真实上下文和输出上限。三名子代理分别负责正式手册编写、变量与会话机制核对、模型表单与保存校验核对；主代理复核并更新既有文档入口，不另设第二份正式手册。

手册将“会话绑定”的数据项授权、工作流会话选择和前端输入绑定分开说明；模型配置准确区分“真实模型容量”折叠区与“上下文长度方式”下拉选择。填写容量是配置校验和请求预算的前置条件，不等同于模型返回、Agent终答、上下文写回或成功检查点均已保存。手册只描述已实现行为，未据此修改产品、调用模型或新增工作流接口。
