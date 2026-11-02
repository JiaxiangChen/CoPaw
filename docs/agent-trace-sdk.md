# AgentTraceSDK 接入

Swe 仅使用 `docs/trace_design.md` 中定义的 AgentTraceSDK API，为一条用户消息的 Main Agent 执行生成 `agent.run` 根 Span。子 Span 覆盖准入、每次执行尝试、模型调用和实际执行的工具调用；原有 `swe.tracing` 链路保持不变。

部署时从私有制品源安装项目声明的 `LR34.05-AgentTraceSDK` 依赖。默认关闭采集：

```bash
AGENT_TRACE_ENABLED=false
AGENT_TRACE_SERVICE_NAME=swe-agent
AGENT_TRACE_EXPORTER=console
AGENT_TRACE_ATTRIBUTE_REDACTION_ENABLED=true
AGENT_TRACE_ATTRIBUTE_VALUE_REDACTION_ENABLED=true
```

本期验证时显式启用 Console exporter：

```bash
AGENT_TRACE_ENABLED=true
AGENT_TRACE_EXPORTER=console
```

不配置 HTTP exporter、端点或认证信息。

模型输入输出按下述结构化有界契约采集；工具输入保持采集，工具原始输出保持摘要限制。属性脱敏和属性值脱敏均开启。

## 0.2.2 升级与 chat I/O（2026-11-02）

依赖固定为 `LR34.05-AgentTraceSDK==0.2.2`。手动 Span 明确传入
`operation_name`：agent.run/agent.attempt 为 invoke_agent，admission 为
swe.admission；语义装饰器继续自动设置操作名。scheduled 和缺少业务标识的
请求通过统一策略跳过嵌套 SDK 采集。

主 Agent chat 在实际模型调用处采集 messages、tools、tool_choice，成功输出
保留 text/tool_use 结构（ID、名称、参数），异常或取消保留最后有界快照。
流式快照只保留一份，收尾时脱敏与序列化。二进制、多模态载荷和 Msg.metadata
不导出。根 Span 另记录整轮输入与按消息 ID 更新的 assistant 输出，忽略工具
过程和重试状态消息。

每个方向的序列化 JSON 默认上限 1MiB，通过
`SWE_AGENT_TRACE_CHAT_MAX_BYTES` 调整（最低 1024 字节）。内容先脱敏，
超过预算时保持有效 JSON 并标记 capture.truncated/omitted_paths。旧
text/text_truncated/tool_call_names 字段保留；工具输出仍使用原有 2048 字节
摘要和高风险工具预览限制。此节替代此前模型仅记录 2KB 摘要的约束。

真实 SDK 验收须在安装私有包的环境运行，不能以 tests/fakes 替代：

```bash
venv/bin/python scripts/tracing/verify_agent_trace_sdk.py
```

脚本校验精确版本、排除 fake，并解析实际 Console Span。缺包/版本错误会失败，
不能作为可忽略的 skip。本地单元测试通过不代表真实 SDK 已验收。
