# AgentTraceSDK 输入输出缺失与版本升级排查

适用范围：并行的 `trace_sdk` 导出；先与原有 `swe.tracing` / Monitor 数据区分。

## 已确认的定位入口

- SDK 契约：`docs/trace_design.md`，当前文档标注 0.2.2。
- 导入和开发 fallback：`src/swe/tracing/agent_trace_sdk.py`。
- 手动 Span：runner 的 `agent.run`、query_execution/admission 的 `agent.admission`、query_attempt 的 `agent.attempt`。
- chat 装饰器：`SWEAgent._run_reasoning_with_internal_context`；其参数没有真实 prompt，真实输入要在 formatter 后的 model 调用处核对。
- 输出投影：`src/swe/agents/agent_trace_output.py`，当前 text 默认限额 2048 字节，tool_use 仅保留名称。

## 最小排查顺序

1. 确认实际 distribution 版本、采集开关和 exporter；检查完成后的原始 Span JSON，不仅查看 UI。
2. 每个 Span 检查 `gen_ai.operation.name`，不要把 Span `name` 当成 operation。0.2.2 的 NoOp 降级可能使业务成功但无采集。
3. 分开检查输入来源错误、项目主动投影/截断、SDK 深度限制/factory 失败、SDK 二次截断与 exporter 队列丢弃。
4. 取消/异常时 SDK 不调用正常输出 factory；部分输出需要在 Span 退出前主动记录。
5. 使用真实 SDK 独立子进程验收：`tests/conftest.py` 会优先加载 tests/fakes；普通专项 pytest 通过不证明真实 SDK 兼容。

2026-11-02 基线：三组现有专项共 19 passed；6000 字节文本投影为 2048 字节，truncated=true。当前环境没有安装真实 SDK，0.2.2 运行行为尚未实测。

完整设计与验收矩阵：[Agent Chat 采集补全与 SDK 升级方案](../../docs/superpowers/specs/2026-11-02-agent-trace-sdk-022-design.md)。本文记录现状与拟议方案入口，不表示代码已修复。

2026-11-02 实施更新：三个 operation 与结构化 chat/整轮 I/O 已修复，相关专项 358 passed。chat 改用默认每方向 1MiB 总预算；工具输出仍为 2KB 摘要。真实私有 SDK 未安装，发布前必须运行 scripts/tracing/verify_agent_trace_sdk.py。

## 2026-11-02 代码检视修复

- 工具结果的 output 与 content 使用同一个递归语义白名单，嵌套图像/音频只保留省略标记，避免导出 base64 和签名 URL。
- 流式快照仅在需要截断字符串时先对完整字符串脱敏；未截断字符串在最终投影时脱敏。不得先截断再匹配注册密钥、PEM 等完整模式。
- TurnCapture 流式阶段只保存有预算限制的独立快照，按消息 ID 替换并增量维护容量；结束时统一投影和序列化，避免每帧重复扫描整个累计响应。
- retry 内捕获取消时先标记当前任务的整轮采集状态；即使异常被处理或转换，根输出仍为 cancelled / complete=false。ContextVar 作用域退出复位，子任务不能覆盖父任务状态。
- 回归入口：tests/unit/tracing/test_agent_trace_capture.py 与 tests/unit/app/test_agent_trace_sdk.py，覆盖嵌套媒体、密钥截断边界、延迟投影、容量、可变消息快照和真实 retry 处理链路。
- 本地 100 帧累计至 500KB 的观察阶段约 1ms，结束投影约 99ms（仅作本机对比，不作为稳定性能阈值）。真实 SDK 验收要求不变。

## 模型输入最新消息

chat CLIENT Span 的 cmb.input.arguments.messages 仅包含实际模型调用 messages 序列的最后一条；空序列记录空数组。最新消息可能是工具结果或系统提示，不按角色过滤。tools 和 tool_choice 继续记录，消息仍经过脱敏、媒体过滤和总预算限制；业务模型仍接收完整输入。agent.run 的输入按整轮消息记录。
