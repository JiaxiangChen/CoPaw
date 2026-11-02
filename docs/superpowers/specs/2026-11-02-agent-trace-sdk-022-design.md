# Agent Chat 采集补全与 Trace SDK 0.2.2 升级方案

日期：2026-11-02。状态：方案已实施并完成本地专项回归；真实私有 SDK 验收待部署环境执行。

## 1. 范围与结论

本方案针对并行接入的 AgentTraceSDK（`trace_sdk`），不迁移原有 `swe.tracing` 存储、Monitor 或会话历史。将“记录完整”定义为：主 Agent 的每次 reasoning chat 记录实际传入模型接口的消息、工具定义和选择参数，以及结构化模型返回；保留脱敏和明确的容量限制，任何省略、截断、异常或取消均有可辨识标记。Agent 整轮入口另记录本轮输入和用户可见输出，两种层次不得混淆。

主要结论：

- chat 输入缺失的直接原因是采集位置只有 `self, tool_choice`，实际 prompt 在父类方法内部构造；SDK 默认采集无法取得这些局部变量。
- 输出不完整首先是项目主动投影与 2048 字节限额，不是 SDK 单字段默认 10000000 字符上限导致。
- SDK 0.2.2 要求的准确属性名是 `gen_ai.operation.name`；Span `name` 不能替代它。三个手动 Span 创建点均未提供。
- 测试替身不实现新版本校验和默认输入采集，现有通过结果不能证明真实 SDK 兼容。

## 2. 证据与根因

| 证据位置 | 当前实现 | 结果 |
|---|---|---|
| `docs/trace_design.md:173` | 每个 Span 的 operation 必须为非空字符串；语义装饰器自动设置 | 手动 Span 必须补齐，不能依靠父 Span 继承 operation |
| `src/swe/agents/react_agent.py:2104-2129` | `chat_traced` 装饰 `_run_reasoning_with_internal_context(self, tool_choice)`，只配置输出 factory | 默认输入只有控制参数，不包含真实 messages、system prompt、tools 或工具结果 |
| 本地 AgentScope `agent/_react_agent.py:540-576` | 父类 `_reasoning` 在 formatter 后调用 `self.model(prompt, tools=..., tool_choice=...)`；hint 随后从 memory 删除 | 在装饰器入口重新读 memory 既不精确，也可能漏掉临时 hint |
| `src/swe/agents/agent_trace_output.py:14,42-68` | text 最多 2048 字节；tool_use 仅保留名称 | 长回复、工具调用 ID/参数及非 text 内容未完整记录 |
| `src/swe/app/runner/runner.py:6596-6628` | `agent.run` 仅手动写 `agent.user_message`，无标准输入输出字段 | 根 Span 没有整轮标准 I/O，不能当作 chat I/O |
| `runner.py:6607`、`query_execution/admission.py:30`、`query_attempt.py:506` | 创建 `agent.run`、`agent.admission`、`agent.attempt` 时无 operation | 0.2.2 契约不满足，可能报校验错误或按 SDK 旁路机制降级而丢 Span |
| `pyproject.toml:52` | `LR34.05-AgentTraceSDK>=0.1.9` | 安装允许跨入不兼容版本，没有稳定验收基线 |
| `tests/fakes/trace_sdk/_impl.py:38-135` | 不校验 operation/根字段，装饰器只执行输出 factory | 升级错误与输入缺失均可能漏测 |
| `tests/conftest.py:15-17` | 测试路径优先放置 `tests/fakes` | 普通 pytest 无法作为真实 SDK 验收 |

已有约束不是历史偶然：`docs/agent-trace-sdk.md` 与 2026-08-27 output-redaction 设计明确只记录模型输出摘要、限制高风险工具输出。本方案扩大 chat 内容采集，是对旧契约的明确调整，需要同步更新这些文档；不同时放开 execute_tool 原始输出。

SDK 文档还说明默认递归深度为 5、factory 失败会跳过相应字段、Pydantic 转换有降级路径。这些是需要测试的潜在附加原因，不能在没有真实样本时认定为本次已复现根因。

### 本次实际验证

- 项目 venv 未安装真实 `trace_sdk`；本次没有执行私有 SDK 0.2.2 的兼容性验收。
- 执行三组现有专项测试：`test_agent_trace_output.py`、`test_agent_trace_sdk.py`（agents）、`test_agent_trace_sdk.py`（app），共 **19 passed**。
- 直接调用现有输出投影：6000 字节文本得到 2048 字节，`text_truncated=True`；chat 配置不存在 `input_arguments_factory`。
- 不将测试替身的行为推断为真实 SDK 的异常传播行为。文档 638 行描述创建/进入/退出失败可降级 NoOp；装饰器配置阶段另有静态校验要求，真实包验收必须覆盖两者。

## 3. 可选方案

| 方案 | 收益 | 局限 |
|---|---|---|
| A：给现有装饰器加 memory 输入 factory，扩大 text 上限 | 改动少 | 采集不是真实模型请求，无法准确覆盖 formatter、hint、tools；不推荐 |
| B：保留现有 chat 生命周期，通过模型边界补采真实输入，升级结构化输出 | 不复制父类 reasoning，保持现有 Span 数量、异常语义与流式时序 | 新增薄模型包装与当前 chat 采集上下文；推荐 |
| C：将 chat Span 完全迁移到模型包装器 | 请求边界最直接 | 异步生成器关闭、ContextVar 跨 task、取消与未消费流生命周期更复杂，且需重定 chat 语义；暂不采用 |

## 4. 推荐架构：保留 Span，在真实请求处采集

```text
agent.run                         operation=invoke_agent
  agent.admission                 operation=swe.admission
  agent.attempt                   operation=invoke_agent, retry.index=N
    chat <model>                  operation=chat，仍包住完整 reasoning
      formatter -> model wrapper  补写这个 chat 的实际输入，不新建 Span
      consume stream -> Msg       输出完整结构化投影
    execute_tool <name>           operation=execute_tool，沿用原策略
```

### 4.1 采集上下文与模型边界

保留 `_run_reasoning_with_internal_context` 的 `chat_traced`。进入其业务函数后，从 SDK 公开 API `get_current_span()` 取得当前 chat Span，通过专用 ContextVar 绑定到本次 reasoning，`finally` 用 token 恢复。绑定内容只包含 Span 句柄和本次采集状态，不存 Agent 全局字段、不复制会话 memory。

新增薄包装器 `AgentTraceModelWrapper`，在 `_wrap_model_with_tracing` 装配。它不依赖旧 TraceManager 是否启用；只有绑定了本次 chat 采集上下文且 Span 正在 recording 才做投影。摘要、压缩、非主 Agent 调用没有该绑定时直接透传，维持当前采集范围。子任务继承 ContextVar 时还需校验所属 asyncio task，不能把其他 task 的模型输入写到父 chat；本次 reasoning 内的正常 await 不受影响。

包装器在调用下层模型前，从原始 `messages/tools/tool_choice` 参数构造白名单投影，经脱敏后写入该 Span 的 `cmb.input.arguments`。不在 tracing 代码重新运行 formatter、读 memory 或改变模型参数。SDK 默认输入仅有 tool_choice 的值会被实际请求投影覆盖；建议显式默认输入 factory 返回空结构，表示真实输入由模型边界负责，避免误导。

包装器原样返回非流式业务对象；流式路径只用透传迭代器观察快照，保持每个 chunk 对象、顺序和异常，并向下传递关闭。不预消费、不自行拼流、不创建 chat 子 Span。当前 AgentScope 流 chunk 的 content 是累计快照，不能逐帧拼接。成功输出仍使用 reasoning 最终返回的 Msg；取消/错误的部分输出另按 4.4 处理。底层 Provider 内部的 HTTP 重试仍属于同一次 AgentScope 模型调用，本期不新增网络请求级 Span。

### 4.2 chat 输入输出字段

`cmb.input.arguments` 的 JSON 对象：

- `schema_version=2`、`messages`：保留真实消息顺序、role 和受支持 content blocks，覆盖 system/user/assistant/tool 消息及临时 hint。
- `tools`、`tool_choice`：实际模型调用使用的工具 schema 和选择。
- 对 text 保留脱敏后文本；对 tool_use/tool_result 保留 ID、name、结构化 input/result；对多模态保留类型及安全描述，不导出 base64、二进制、带签名 URL。
- 未识别块保留 type 和 `omitted_reason=unsupported_block`，不得用任意对象 `__dict__` 或 repr 兜底导出。

`cmb.output.arguments` 的 JSON 对象：

- `schema_version=2`、`role`、`content`：按返回 Msg 的块顺序保留 text、tool_use ID/name/input，其他块使用相同白名单规则。
- 保留旧 `text`、`text_truncated`、`tool_call_names` 作为过渡读兼容，`text` 从最终投影的 text blocks 派生，不再单独截成 2KB。
- `status=completed|partial|failed|cancelled`、`complete` 以及采集容量信息；空输出也是合法结构，不等于没采集。
- 不导出 Msg.metadata、Provider 原始响应、实例对象或请求认证信息；模型 usage 在确实取得时以独立属性记录，缺失不编造。

`complete` 表示业务输出是否正常结束；容量截断用独立 `truncated` 表示。正常结束但被限额裁剪不能标成内容完整。

### 4.3 内容预算与脱敏

“完整”不能等于无限制保存。建议主 Agent chat I/O 改用独立总预算：每方向默认 **1 MiB UTF-8 序列化 JSON**，通过一个 `SWE_AGENT_TRACE_CHAT_MAX_BYTES` 参数调整。这是方案默认值，不是当前配置或 SDK 限额；上线前需用实际长会话及接收端上限验证。

- 先按显式 block/schema 投影和脱敏，再计算紧凑 JSON 字节数；结果始终是有效 JSON，不截断 JSON 字符串中间。
- 保留 `capture.truncated`、`capture.limit_bytes`、`capture.omitted_count`、`capture.omitted_paths`（路径列表自身也受限）、`capture.redacted`。
- 单个方向所有字段和兼容字段的总和计入预算，不能仅限制每个字符串。超限优先保留结构、最近用户问题和最后输出，明确标记被省略的历史项或工具定义。
- 不复用 sanitizer 默认 `max_items=32` 导致长 messages/tools 静默缺失，也不全局调大旧 sanitizer 默认值。为 chat 显式传递边界或新增专用投影 helper，保留工具输出旧策略。
- 复用现有按 key、文本模式和租户 secret 值脱敏能力。chat tool_result 内嵌内容同样脱敏；多模态描述、URL query 按允许字段投影。
- SDK 仍是第二道脱敏和限额边界。真实导出需验证递归深度与二次截断；SDK/接收端限制应容纳应用投影，不能宣称应用层未截断就证明端到端完整。
- NoOp/关闭采集时不执行深拷贝、序列化和敏感值扫描，避免长会话开销。

### 4.4 异常、取消与整轮 I/O

SDK 输出 factory 仅在正常返回时执行，不能靠它记录中断结果。模型包装器可在透传流时只保留最后一份已脱敏、受预算限制的累计快照；不保存每个 chunk。每个 chunk 只更新有界快照，不执行全量重复 JSON 序列化；序列化在收尾时完成。非流式使用返回结果的内容快照。

在 reasoning 内部 `except/finally` 中，于 chat Span 退出之前记录部分输出和状态；随后原样重抛业务异常或 CancelledError。成功时以最终 Msg 为准；失败时 output factory 不运行，因此不会覆盖部分输出。不可让后来的旧 task 写入新轮次。投影失败记录有界 `capture_error` 标记，不能吞业务异常或重新调用模型。

`agent.run` 在 `_stream_query_handler_frames` 中补 `cmb.input.arguments` 与 `cmb.output.arguments`，复用 chat 每方向总预算及截断标记。输入使用本轮消息投影；输出使用消息 ID 的最终快照收集器，依据现有可见性/role 规则排除工具过程和重试状态消息，保留多个最终 assistant 消息，按出现顺序记录。不能把重复累计快照拼接，也不能假定一个 `last=True` 就一定代表整轮最终答案。异常/取消在 Span 内写入本轮已产生的部分内容。原 `agent.user_message` 作为兼容字段保留。

## 5. SDK 0.2.2 兼容设计

### operation 映射

| Span | operation | 说明 |
|---|---|---|
| `agent.run` | `OperationName.INVOKE_AGENT` | 当前范围是主 Agent 整轮调用，保留 SERVER kind |
| `agent.admission` | 项目常量 `swe.admission` | SDK 无专用准入常量，文档接受自定义非空字符串；不伪装成模型 chat |
| `agent.attempt` | `OperationName.INVOKE_AGENT` | 一次执行尝试，保留 retry.index |
| `chat <model>` | 语义装饰器自动 `chat` | 无需重复传入或手工覆盖 |
| `execute_tool <name>` | 语义装饰器自动 `execute_tool` | 沿用现有装饰器 |

统一从 `swe.tracing.agent_trace_sdk` 导出 OperationName/get_current_span，保持第三方导入边界。三个手动 Span 创建时直接传 `operation_name`，不在进入 Span 后用 set_attribute 补救，不从动态 span.name 猜测，也不全局兜底为 chat。

推荐第一阶段将项目依赖精确锁到 **`LR34.05-AgentTraceSDK==0.2.2`**，建立可复现基线；生产镜像同步重建。后续放宽版本范围需有真实包契约矩阵。常规运行不继续承诺 0.1.x；旧镜像仅用于整体发布回退，不在一个进程中通过 TypeError 重试两套 API。

更新显式开发 fallback：提供同名常量、`get_current_span()->None`、`is_recording=False`；仍只在缺失 SDK 且 `SWE_ALLOW_MISSING_TRACE_SDK=true` 时启用，不掩盖已安装包的内部导入错误。

### 没有合法根上下文的路径

当前 scheduled 或缺少标识时会跳过 agent.run，但 admission/attempt/语义装饰器可能仍执行。新测试必须用实际 adapter 证明这些路径不会生成无 TraceFields 的孤儿根 Span；不能仅使用不经过子层的 `_Adapter`。建议统一检查合法采集上下文：本期范围外路径全链路透传并跳过 SDK 采集，不生成假 task/user/source ID。保留 task_id=session_id、SDK session_id=turn_id 的现有业务映射，不顺带重构。

## 6. 改动边界与实施顺序

1. **先修契约**：锁 SDK；导出常量并更新 fallback；修三个 operation；强化 fake 校验，并运行真实 0.2.2 Console smoke。验证：Span operation、TraceFields、B3 父子关系和 scope 清理。
2. **补 chat I/O**：新增薄模型包装与 scoped collector，扩展块投影和总预算；保留当前 chat Span 生命周期。验证：formatter 后实际输入、长输出、工具结果、hint、流式/非流式与关闭采集。
3. **补整轮及终态**：root I/O 收集与部分输出、并发/取消隔离。验证：SSE 内容与顺序不变，重试不重复拼接，错误不被吞掉。
4. **发布验收**：更新 `docs/agent-trace-sdk.md` 与旧 output-redaction 设计的适用范围；在测试环境启用 Console 对照 Span JSON，再按现有发布流程推广。Exporter 迁移和 Monitor 改版不纳入本期。

预计涉及：`pyproject.toml`、SDK boundary、三个 Span 调用点、`agents/model_factory.py`、新 `tracing/agent_trace_model_wrapper.py`、`agents/react_agent.py`、`agents/agent_trace_output.py`、必要的 chat 投影 helper、对应 tests/fakes 和专项测试。旧 tracing manager/store/schema 不改。

## 7. 验收矩阵

| 场景 | 必须验证 |
|---|---|
| SDK 必填校验 | 每个实际导出的 Span operation 非空；漏传/空值/冲突值有明确负例；NoOp 不等于成功采集 |
| 请求内容 | system + 多轮 memory + hint + tools + tool_choice 与实际模型入参一致；超过 32 条仍不静默丢失 |
| 模型输出 | 6KB 中文和英文输出不再 2KB 截断；多 text、tool_use ID/参数、空响应、混合块均可解释 |
| 限额 | 深层 tool schema、超过 1MiB、序列化后总大小、中文 UTF-8、多模态省略均有稳定标记与有效 JSON |
| 终态 | 流开始前失败、流中失败、Stop/CancelledError、主动关闭迭代器、媒体回退重试均保持原业务行为 |
| 并发 | 两个租户、两个 task、连续轮次、继承 ContextVar 的后台任务不串写；退出后 collector/current span 恢复 |
| scope | 压缩/摘要/非主 Agent 不新增 chat；scheduled/缺字段使用真实执行路径不生成孤儿 Span |
| 旧链路 | 开关独立：旧 tracing 关闭但 SDK 开启仍采集；两者都开时原指标与工具行为不变 |
| fake | 实现 operation、根字段继承、输入/输出 factory、factory 失败及异常不吞业务语义；不追求复制 SDK |
| 真实 SDK | 独立子进程排除 tests/fakes，断言 distribution==0.2.2；Console 导出可解析且包含预期完整属性 |

真实 SDK 测试缺包时本地可显式 skip，但发布门禁必须失败，不能把 fake 通过当成升级成功。发布前至少取得一条含模型与工具的正常链路和一条取消链路的导出证据。

## 8. 发布与回退

先部署包含 0.2.2 契约修复与新采集逻辑的测试镜像，用现有 AGENT_TRACE_ENABLED 开关控制；生产保持既有开关配置，由既有上线流程启用。观察缺字段/NoOp、capture_error、限额截断、队列丢弃日志及新增 CPU/内存成本。仅有 exporter=none 不代表采集开销关闭，应使用 AGENT_TRACE_ENABLED=false。

紧急回退先关闭 SDK 采集；需要版本回退时整体回退匹配旧依赖的镜像，不单独替换 SDK wheel。历史记录无法由本次修复补回，不承诺追溯恢复已丢失内容。

## 实施验证（2026-11-02）

代码已补齐三个 operation、任务级采集策略、真实模型输入包装、结构化 chat 输出与总预算、整轮快照去重和异常/取消输出。新 SDK 依赖精确固定 0.2.2。工具输出摘要策略保留。

相关测试 358 passed，包含 tracing 子系统、模型装配、查询重试与 SDK fallback。新增模块通过 flake8，差异通过空白检查。实际 SDK 制品不在当前包源，独立验收脚本 scripts/tracing/verify_agent_trace_sdk.py 因缺少私有包而阻塞；不将 fake 测试结果作为真实 SDK 兼容成功。发布环境仍须运行该脚本。

实施细节：快照在流式过程中仅保存一份有界、分离的内容，异常收尾时脱敏和序列化。omitted_paths 最多记录 8 个路径；深度/条目上限使用汇总路径标记。超限时优先缩短大文本或移除较早列表项，不保证所有兼容字段在截断后仍能完全互相还原，消费者必须检查 capture.truncated。
