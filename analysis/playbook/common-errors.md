# 常见报错

本文档只收录仓库中已经出现过、且有明确入口可追的高频报错。

## 后端启动报 ModuleNotFoundError: No module named 'trace_sdk'

### 症状

- 开发环境启动 `swe app` 或导入 `swe.app._app` 时，在 AgentTraceSDK
  导入阶段报 `ModuleNotFoundError: No module named 'trace_sdk'`
- 环境无法安装私有分发包 `LR34.05-AgentTraceSDK`

### 第一落点

- [src/swe/tracing/agent_trace_sdk.py](../../src/swe/tracing/agent_trace_sdk.py)
- 私有依赖仍由 [pyproject.toml](../../pyproject.toml) 声明，生产环境默认
  保持缺包即失败

### 开发环境处理

- 仅在本地无法安装私有包时，显式启动：
  `SWE_ALLOW_MISSING_TRACE_SDK=true swe app`
- 该开关只把 AgentTraceSDK 替换为 no-op 实现，不会关闭 Swe 自身的
  tracing manager
- 不要将该变量加入部署环境。变量未设置或不是 `true` 时，会重新抛出
  原始导入错误，防止生产环境静默失去 Agent Trace

## MCP 报 mcp_transport_error

### 症状

- Agent 工具调用返回 `mcp_transport_error`
- 高并发时间段更容易出现连接中断、服务端关闭连接或连接建立超时

### 第一落点

- [src/swe/app/mcp/lazy_client.py](../../src/swe/app/mcp/lazy_client.py)
- [src/swe/app/runner/runner.py](../../src/swe/app/runner/runner.py)
- [src/swe/app/mcp/stateful_client.py](../../src/swe/app/mcp/stateful_client.py)

### 排查与处理

- 区分 schema discovery 和真实工具调用：schema cache 只缓存工具定义，不能作为活跃 session 的证据
- 核对同一时间段的 MCP 服务端连接上限、反向代理超时、连接池耗尽和实例重启
- 核对调用期的 `session_id`、`chat_id`、`trace_id` 与认证 header 是否符合上游要求
- 首期按需连接会减少未被调用 MCP 的握手和空闲连接；它不替代 endpoint 限流或后续 session pool

## submit_proposed_plan 报 items must not be empty

### 症状

- 模型调用 `submit_proposed_plan` 时工具执行失败
- 常见报错为：
  - `1 validation error for ProposedPlanCreate`
  - `steps Value error, items must not be empty`
- 入参中的 `steps`、`risks` 或 `verification` 使用空字符串分隔段落，例如 `["阶段一", "...", "", "阶段二"]`

### 典型原因

- 模型把 Markdown/文本计划里的空行保留成列表中的 `""` 或空白字符串
- `ProposedPlanCreate` 会校验计划列表字段，历史实现曾把任意空白列表项视为硬错误
- 这类空项通常只是展示分隔符，不代表一条真实计划步骤

### 第一落点

- [src/swe/app/plans/models.py](../../src/swe/app/plans/models.py)
- 重点看 `ProposedPlanCreate._non_empty_text_list()` 是否先清理空白项，再校验剩余有效项
- 对应回归测试：
  - [tests/unit/app/plans/test_models.py](../../tests/unit/app/plans/test_models.py)

### 第一阶段处理

- 工具调用侧不要主动传入空字符串作为分隔符，阶段标题可以作为普通步骤保留
- 后端模型层可以清理空白列表项，但清理后列表为空时仍必须报 `must not be empty`
- 不要放宽 `title`、`summary` 或未知字段校验，避免前端/模型注入未声明语义

## submit_proposed_plan 报 Input should be a valid list

### 症状

- 模型调用 `submit_proposed_plan` 时工具执行失败
- 常见报错为：
  - `1 validation error for ProposedPlanCreate`
  - `steps Input should be a valid list`
- 入参中的 `steps`、`risks` 或 `verification` 是 JSON 字符串数组，例如 `"\n[\"阶段一\", \"阶段二\"]\n"`

### 典型原因

- 模型把工具参数里的数组再次序列化为 JSON 字符串
- 工具 schema 如果只声明 `array`，部分模型仍可能把数组作为文本传入
- `ProposedPlanCreate` 的领域字段仍是 `list[str]`，未做入口归一化时会在 Pydantic 列表类型校验处失败

### 第一落点

- [src/swe/agents/tools/planning.py](../../src/swe/agents/tools/planning.py)
- 重点看 `submit_proposed_plan()` 的 `steps`、`risks`、`verification` 工具签名是否接受数组或 JSON 字符串数组
- [src/swe/app/plans/models.py](../../src/swe/app/plans/models.py)
- 重点看 `ProposedPlanCreate` 是否先把 JSON 字符串数组解码，再执行空白项清理和非空校验
- 对应回归测试：
  - [tests/unit/agents/tools/test_planning.py](../../tests/unit/agents/tools/test_planning.py)
  - [tests/unit/app/plans/test_models.py](../../tests/unit/app/plans/test_models.py)

### 第一阶段处理

- 只兼容可解析为数组的 JSON 字符串，不把普通段落或 Markdown 文本自动拆成步骤
- 解码后继续复用 `ProposedPlanCreate._non_empty_text_list()` 清理空白项，清理后为空仍报错
- 不要把 `PlanReviewCard` 或持久化模型字段改成字符串，前端和存储协议仍统一使用 `list[str]`

## ask_plan_clarification 报 str object has no attribute get

### 症状

- 模型调用 `ask_plan_clarification` 的表单模式时工具执行失败
- 工具入参中的 `fields` 是 JSON 字符串，而不是原生数组
- 常见报错为：`Error: 'str' object has no attribute 'get'`
- 模型调用选择模式时把 `options` 作为 JSON 字符串传入，前端表现为每个字符都渲染成一个选项
- 模型把 `kind` 传成 `clarification`、`choice` 等宽泛描述，后端报 `PlanClarificationCard.kind` 枚举校验失败
- 模型把表单字段写成 `{"name": "机构类型", "description": "...", "options": [...]}`，没有提供 `label`，后端报 `clarification field label is required`

### 典型原因

- 模型把表单字段数组再次序列化成 JSON 字符串
- 模型把选择项数组再次序列化成 JSON 字符串
- 计划澄清工具直接遍历字符串，导致单个字符进入字段或选项归一化逻辑
- 部分模型还会使用 `key` 代替 `id`，省略可从 `options` 推断的字段类型，或只给选项提供 `label`/`description`
- 工具 schema 如果把 `kind` 暴露为任意字符串，模型容易把工具用途描述误传成卡片协议枚举
- 部分模型会把 `name` 同时当字段标识和展示标签使用，而工具入口只把 `name` 当 `id` 兜底
- 字段内部的 `options` 也可能被再次序列化为 JSON 字符串

### 第一落点

- [src/swe/agents/tools/planning.py](../../src/swe/agents/tools/planning.py)
- 重点看 `_normalize_form_fields()` 是否在字段归一化前解析 JSON 字符串并验证对象数组结构
- 重点看 `_coerce_json_array()` 是否同时兼容 `fields` 和 `options` 的 JSON 字符串数组
- 重点看 `_normalize_form_field()` 是否兼容 `key`，以及缺失 `type` 时是否按候选项推断类型
- 重点看 `_normalize_choice_option()` 是否兼容 label-only 选项并保留 `description`
- 重点看 `ask_plan_clarification()` 的 `kind` 注解是否向工具 schema 暴露受控枚举
- 对应回归测试：
  - [tests/unit/agents/tools/test_planning.py](../../tests/unit/agents/tools/test_planning.py)

### 第一阶段处理

- 先记录工具实际入参，确认 `fields` 和 `options` 是原生数组还是 JSON 字符串
- JSON 字符串只允许解析为数组，非法 JSON 或非对象字段应返回明确参数错误
- 无候选项的缺省字段归一为 `text`，有候选项的缺省字段归一为 `single_choice`
- `kind` 必须使用 `single_choice`、`multi_choice`、`text` 或 `form`，不要放宽 `PlanClarificationCard` 领域模型
- 表单字段展示名按 `label/title/name/key/id` 顺序兜底，字段标识按 `id/key/name/label/title` 顺序兜底
- 只有工具入口做宽松归一化，`PlanClarificationCard` 和 `PlanClarificationField` 继续保持严格协议
- 错误信息需要带 `fields[index]`，便于从工具调用日志直接定位失败字段

## Shell 中运行 swe 时被 Python runtime guard 拦截 `/opt/.swe`

### 症状

- Agent shell 工具里执行 `swe ...` 命令失败
- stderr 先出现导入期 warning：
  - `swe: failed to load persisted envs on init`
- 随后 CLI 启动阶段抛出：
  - `TenantPathGuardError: Python runtime guard denied pathlib.is_file path outside the allowed workspace: /opt/.swe/config.json`
- 同一栈里也可能先看到 `/opt/.swe.secret/envs.json` 被拒绝

### 典型原因

- shell 工具会向 Python 子进程注入 `sitecustomize` runtime guard，用于限制租户路径逃逸
- shell 子进程环境如果丢失后端已确定的 `SWE_WORKING_DIR` / `SWE_SECRET_DIR`，`swe` CLI 会回退到默认 `~/.swe`
- 容器内 `~` 可能解析为 `/opt`，于是 CLI 读取 `/opt/.swe/config.json` 和 `/opt/.swe.secret/envs.json`
- 这些路径不属于当前租户 workspace，也不是普通 Python runtime 路径，因此被 guard 拦截

### 第一落点

- [src/swe/agents/tools/shell.py](../../src/swe/agents/tools/shell.py)
- 重点看 `_prepare_subprocess_env()` 是否通过 `build_runtime_env(preserve_boundary_env_keys=...)` 保留后端边界变量
- [src/swe/envs/runtime.py](../../src/swe/envs/runtime.py)
- 重点看 `PROTECTED_RUNTIME_ENV_KEYS`、`_scrub_user_tool_subprocess_env()` 和 `preserve_boundary_env_keys`
- [src/swe/security/python_runtime_path_guard.py](../../src/swe/security/python_runtime_path_guard.py)
- 重点看 `prepare_python_runtime_path_guard_env()` 注入的 trusted paths / entrypoint roots

### 第一阶段处理

- shell 子进程应保留后端进程环境中的 `SWE_WORKING_DIR` 和 `SWE_SECRET_DIR`
- 仍然不能允许租户持久化 env 或 call env 覆盖这两个变量
- 回归测试应同时断言：
  - shell 子进程 env 里存在后端 `SWE_WORKING_DIR` / `SWE_SECRET_DIR`
  - 租户 `.secret/envs.json` 中同名 key 不会覆盖
  - `PYTHONPATH` 等解释器边界变量仍被过滤

## Shell 脚本结束后报 _wrap_path_function missing specs

### 症状

- `execute_shell_command` 执行的 Python 脚本本身成功完成
- 工具输出的 stderr 额外出现：
  - `Error in sitecustomize; set PYTHONVERBOSE for traceback:`
  - `TypeError: _wrap_path_function() missing 1 required positional argument: 'specs'`

### 典型原因

- Shell 工具为 Python 子进程生成并注入 `sitecustomize.py`；它在解释器启动时安装租户路径守卫
- 运行中的服务仍使用旧 guard 源码，其中 `_wrap_path_function(module, name, specs)` 的调用遗漏了第三个参数
- Python 将 `sitecustomize` 导入失败作为 stderr warning 处理并继续执行，因此错误会和脚本输出一起在工具返回时出现

### 第一落点与处理

- [src/swe/security/python_runtime_path_guard.py](../../src/swe/security/python_runtime_path_guard.py)
- [tests/unit/test_python_runtime_path_guard.py](../../tests/unit/test_python_runtime_path_guard.py)
- 现行源码必须保证每个 `_wrap_path_function()` 调用传入 `specs`，并运行 `test_runtime_guard_starts_python_without_sitecustomize_error`
- 该 guard 被生成到子进程临时目录，修复源码后必须重新安装应用并重启 `swe app`；容器部署则需要重建镜像并滚动更新实例

## MCP 注册时报 App not Subscribe This MCP Server

### 症状

- MCP 客户端连接日志正常，但 Agent 注册工具时失败
- 堆栈停在 `register_mcp_clients()` 调用 `client.list_tools()` 的阶段
- 服务端返回 `App not Subscribe This MCP Server`
- 同一份 MCP 配置在回滚应用版本后恢复正常

### 典型原因

- Runner 构建 MCP 客户端时正确合并了静态 Header、透传 Header 和 cookie
- 但实际 `connect()` 使用了另一套 transport context，导致构建阶段的鉴权 Header 没有进入真实请求
- 基础 MCP initialize 可能允许无鉴权完成，直到 `tools/list` 才执行应用订阅校验，因此容易误判为服务端订阅或配置问题

### 第一落点

- [src/swe/app/runner/runner.py](../../src/swe/app/runner/runner.py)
- 重点看 `_create_mcp_client_with_headers()` 是否把 `merged_headers` 直接传给负责 transport 生命周期的 `HttpStatefulClient`
- [src/swe/app/mcp/stateful_client.py](../../src/swe/app/mcp/stateful_client.py)
- 重点看 `HttpStatefulClient._run_lifecycle()` 实际创建 HTTP / SSE transport 时使用的 `self.headers`
- [tests/unit/app/test_runner_mcp_http_timeouts.py](../../tests/unit/app/test_runner_mcp_http_timeouts.py)
- 回归测试必须真实调用 `connect()`，不能只断言构建阶段生成了带 Header 的临时 context

### 第一阶段处理

- transport 生命周期只由一层负责，避免 Runner 和 `HttpStatefulClient` 同时创建 context
- Runner 将合并后的 Header 和超时参数直接传给 `HttpStatefulClient`
- 用 Streamable HTTP 和 SSE 两种 transport 的 `connect()` 测试确认真实请求参数包含订阅身份 Header

## Console 复制工具输入时触发 Clipboard 权限策略报错

### 症状

- 聊天回答里的工具调用卡片点击“复制输入”或“复制输出”
- 浏览器控制台出现：
  - `[Violation] Permissions policy violation: The Clipboard API has been blocked because of a permissions policy applied to the current document`
- 常见于 Console 被嵌入 iframe，且父页面未授予 `clipboard-write` 权限的场景

### 典型原因

- 前端直接调用 `navigator.clipboard.writeText()`
- 当前文档的 `Permissions-Policy` 或 iframe `allow` 未允许 Clipboard API
- 浏览器会在调用被拦截 API 时输出 violation，即使后续业务代码捕获异常也可能留下控制台报错

### 第一落点

- [console/src/utils/clipboard.ts](../../console/src/utils/clipboard.ts)
- 重点看是否通过 `document.permissionsPolicy` / `document.featurePolicy` 先判断 `clipboard-write`
- [console/src/components/agentscope-chat/Util/copy.ts](../../console/src/components/agentscope-chat/Util/copy.ts)
- 重点看聊天内复制入口是否复用通用复制工具

### 第一阶段处理

- 权限策略明确禁止 `clipboard-write` 时，不要调用 `navigator.clipboard.writeText()`
- 直接降级到 textarea + `document.execCommand("copy")`
- Clipboard API 运行时失败时，也要降级复制；所有方式失败时返回失败状态，由调用方提示“复制失败”

## 长 MCP 调用期间 console SSE 被静默断开

### 症状

- MCP 工具调用耗时 10 秒以上时，前端 console 会话中断
- `streamable_http` MCP 本身还在执行，但 `/console/chat` 长时间没有任何 SSE 输出
- 日志可见运行被取消，例如：
  - `query_handler: <session_id> cancelled!`
  - `Runner finally block executing for session <session_id>`

### 典型原因

- 外层 `/console/chat` SSE 在长时间无事件期间没有发送心跳帧
- 代理、Ingress 或客户端对 10 到 15 秒静默连接执行 idle timeout
- 即使后端任务未失败，HTTP 流也会先被外层网络链路掐断
- `streamable_http` MCP 如果走到默认 `httpx` timeout，可能在读阶段约 5 秒无新字节时先超时或触发中断链路

### 第一落点

- [src/swe/app/routers/console.py](/Users/shixiangyi/code/Swe/src/swe/app/routers/console.py)
- 重点看 `post_console_chat()` 和 `_stream_with_keepalive()`
- [src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `_create_mcp_client_with_headers()` 是否给 `streamable_http` MCP 显式配置 `httpx.Timeout`

### 第一阶段处理

- 在 `/console/chat` 的 SSE 输出层补 comment 心跳，例如 `: keep-alive\n\n`
- 心跳周期要小于最短代理 idle timeout，当前实现默认 5 秒
- 响应头显式加 `X-Accel-Buffering: no`，避免代理缓冲导致心跳帧无法及时刷出

### 边界说明

- 这一阶段只解决“外层 SSE 静默断连”
- 不包含 MCP 内部执行进度透传；如果希望前端看到“工具执行中”，需要后续把 MCP progress/event 映射进 `TaskTracker` 或 SSE 事件流

## Console SSE 只返回 keep-alive 没有 data

### 症状

- `/api/console/chat` 返回 `200 text/event-stream`
- 响应头包含 `X-Accel-Buffering: no`，且首帧能收到 `: keep-alive`
- 随后连接结束或长期没有任何 `data:` 模型事件

### 典型原因

- Console 请求经 `_resolve_raw_console_request_data()` 回读原始 JSON 后，`content_parts` 仍是 dict
- `BaseChannel._apply_no_text_debounce()` 如果只按对象属性读取 `type/text`，会把 `{"type":"text","text":"..."}` 误判为无文本消息
- 误判后消息被缓存到 `_pending_content_by_session`，`ConsoleChannel.stream_one()` 直接返回，`TaskTracker` producer 不会调用模型

### 第一落点

- [src/swe/app/routers/console.py](/Users/shixiangyi/code/Swe/src/swe/app/routers/console.py)
- 重点看 `_extract_session_and_payload()` 是否把前端 JSON content 传入 `native_payload["content_parts"]`
- [src/swe/app/channels/base.py](/Users/shixiangyi/code/Swe/src/swe/app/channels/base.py)
- 重点看 `_content_has_text()`、`_content_has_audio()` 和 `_apply_no_text_debounce()` 是否同时兼容 runtime Content 对象与 dict
- [src/swe/app/channels/console/channel.py](/Users/shixiangyi/code/Swe/src/swe/app/channels/console/channel.py)
- 重点看 `stream_one()` 是否在 debounce 后继续构造 `AgentRequest`

### 第一阶段处理

- 用 `curl --no-buffer -N` 先确认是否至少收到首个 `: keep-alive`
- 如果只有 keep-alive，没有 `data:`，检查 `content_parts` 的实际类型
- dict 文本块应被识别为完整用户输入，再交给 `AgentRequest` 做 runtime content 类型转换
- 若已能收到 `response created/in_progress/failed`，说明 SSE 和 producer 已通，后续错误应转向模型 Provider、工具或 Runner 配置

## Console 第二轮提问报 System message must be at the beginning

### 症状

- `/api/console/chat` 首轮请求正常，沿用同一个 `session_id` 第二轮提问失败
- SSE 返回 `Unknown agent error: BadRequestError: Error code: 400`
- 模型后端错误内容包含：
  - `System message must be at the beginning.`
  - `Unexpected message role.`
  - `{'code': 20015, 'message': 'Unexpected message role.'}`
- query error dump 栈一般落在 `agentscope/model/_openai_model.py` 调用 OpenAI-compatible `chat.completions.create`

### 典型原因

- 首轮结束后的 hook additionalContext 被追加进 `agent.memory`
- 如果追加消息使用 `role=system`，第二轮历史会变成 `system/user/assistant/system/user`
- 严格 OpenAI-compatible 后端只允许第一条消息是 `system`，会拒绝非首位 system
- hook 附加上下文现在会保留 `system` 角色；formatter 只允许 hook 前缀消息继续保持非首位 `system`
- accepted plan 执行上下文现在通过内部 `assistant` tool-call 与 `tool` result 成对注入，而不是拼进主 system prompt
- 已经落盘的旧 session 即使源头修复，也可能继续携带 legacy `developer` 历史；加载时需要先迁移成 `system`

### 第一落点

- [src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `_emit_stop_hook_if_needed()` 写入 STOP hook additionalContext 时是否继续保留 `role="system"`
- [src/swe/agents/model_factory.py](/Users/shixiangyi/code/Swe/src/swe/agents/model_factory.py)
- 重点看 OpenAI / Anthropic formatter 是否保留 hook 前缀 `system`，并保持 accepted plan tool exchange 配对
- [src/swe/providers/openai_chat_model_compat.py](/Users/shixiangyi/code/Swe/src/swe/providers/openai_chat_model_compat.py)
- 重点看 Provider 层是否还保留 `developer -> user` 降级重试
- [src/swe/agents/tool_guard_mixin.py](/Users/shixiangyi/code/Swe/src/swe/agents/tool_guard_mixin.py)
- 重点看 `_record_tool_hook_result()` 是否同样写入 `role="system"` 的 hook additionalContext
- [src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- 重点看 `_build_accepted_plan_tool_exchange()` 和 `_reasoning()` 是否把 accepted plan 作为内部 tool exchange 注入当前轮次
- 对应测试：
  - [tests/unit/app/test_runner_hook_runtime.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_runner_hook_runtime.py)
  - [tests/unit/agents/test_model_factory_tenant.py](/Users/shixiangyi/code/Swe/tests/unit/agents/test_model_factory_tenant.py)
  - [tests/unit/providers/test_openai_stream_toolcall_compat.py](/Users/shixiangyi/code/Swe/tests/unit/providers/test_openai_stream_toolcall_compat.py)

### 第一阶段处理

- 新增 hook 附加上下文统一写成带 hook 前缀的 `system` 消息，不再改写为 `developer` 或 `user`
- formatter 侧保留兜底：只有 hook 前缀消息允许继续保持非首位 `system`，其他非首位 system 仍降级为 `user`
- provider 兼容层不再做 `developer -> user` 自动重试；如果后端拒绝请求，应直接暴露失败
- accepted plan 相关问题先确认 `assistant tool_use` 与后续 `tool_result` 是否成对、`tool_call_id` / `tool_use_id` 是否一致
- 如果仍报错，检查当前 session 落盘 JSON 中 `agent.memory.content` 的 role 顺序，确认是否还有非首位 system 绕过了 formatter

## 会话恢复时报 Msg.from_dict 断言失败

### 症状

- 发起已有 `session_id` 的会话时，Runner 在加载 session state 阶段直接报错
- 常见堆栈包含：
  - `SafeJSONSession.load_session_state()`
  - `ReMeInMemoryMemory.load_state_dict()`
  - `Msg.from_dict(...)`
  - `assert role in ["user", "assistant", "system"]`
- 落盘的 session JSON 里可以看到 hook additionalContext 消息使用 `role="developer"`

### 典型原因

- 旧版本保存的 hook additionalContext 可能仍带 `role="developer"`
- `Msg.from_dict()` 反序列化仍只接受 `user/assistant/system`
- 会话恢复如果直接把落盘 JSON 交给底层 memory `load_state_dict()`，就会在反序列化阶段触发断言

### 第一落点

- [src/swe/app/runner/session.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/session.py)
- 重点看 `load_session_state()` 是否在调用底层 `load_state_dict()` 前把 legacy `developer` 单向迁移成 `system`
- [src/swe/agents/hook_runtime/messages.py](/Users/shixiangyi/code/Swe/src/swe/agents/hook_runtime/messages.py)
- 重点看 hook 附加上下文是否仍通过 helper 生成标准 `system` 消息
- 对应回归测试：
  - [tests/unit/app/test_session.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_session.py)
  - [tests/unit/app/test_runner_hook_runtime.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_runner_hook_runtime.py)

### 第一阶段处理

- 不要把落盘里的 legacy `developer` 直接改成普通 `user`
- 在 session 加载边界先把 `developer` 迁移成 `system`，后续保存继续保持 `system`
- 如果用户已经产生坏 session 文件，修复代码后重新发起同一 `session_id` 即可触发兼容恢复，不需要先手工删历史

## 聊天详情接口读取 legacy developer 历史时报 500

### 症状

- 请求 `GET /api/chats/{chat_id}` 或 `GET /api/tracing/chats/{chat_id}` 返回 `500`
- 常见堆栈包含：
  - `src/swe/app/runner/api.py:_messages_from_memory_state()`
  - `memory.load_state_dict(...)`
  - `assert role in ["user", "assistant", "system"]`
- 如果先绕过 `load_state_dict()`，下一层还可能继续报：
  - `Input should be 'assistant', 'system', 'user' or 'tool'`

### 典型原因

- 落盘 session 的旧 hook additionalContext 消息仍可能保留 `role="developer"`
- 聊天详情接口直接读取原始 `memory_state` 时，如果没有复用 session 加载边界的 role 兼容逻辑，会先在 AgentScope 反序列化阶段失败
- 即使 AgentScope 内存已恢复成功，详情接口组装 `ChatMessage` 时也必须只暴露标准角色

### 第一落点

- [src/swe/app/runner/api.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/api.py)
- 重点看 `_messages_from_memory_state()` 是否在 `load_state_dict()` 前调用 session 的 role 兼容逻辑
- [src/swe/app/runner/utils.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/utils.py)
- 重点看 `agentscope_msg_to_message()` 是否最终只暴露 `system/user/assistant/tool`
- 对应回归测试：
  - [tests/unit/app/test_chat_api_message_timestamp.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_chat_api_message_timestamp.py)
  - [tests/unit/routers/test_tracing_chats_api.py](/Users/shixiangyi/code/Swe/tests/unit/routers/test_tracing_chats_api.py)

### 第一阶段处理

- 详情接口读取 memory 时，先复用 `session.py` 的 role 兼容逻辑，把 legacy `developer` 迁移为 `system`
- 进入 API 响应层后，只返回标准角色，不再恢复或暴露 `developer`

## accepted plan 注入后模型拒绝 tool 消息配对

### 症状

- 执行 accepted plan 时模型请求直接失败
- 常见错误包含：
  - `tool_call_id` 缺失
  - `tool_use_id` 不匹配
  - `tool message must follow a tool call`
- 同一轮 prompt 里看不到成对的 `assistant tool_use` 和 `tool_result`

### 典型原因

- accepted plan 被重新拼回了主 system prompt
- 内部 tool exchange 只有 `tool_result`，没有前置 `assistant` tool call
- tool call id / tool result id 不一致
- accepted plan 在 Plan Mode 或非服务端来源场景下被误注入

### 第一落点

- [src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- 重点看 `_build_accepted_plan_tool_exchange()` 是否校验 `accepted_plan_source`、`plan_mode_enabled` 并生成稳定 call id
- [src/swe/agents/model_factory.py](/Users/shixiangyi/code/Swe/src/swe/agents/model_factory.py)
- 重点看 OpenAI / Anthropic formatter 是否保留该内部 exchange 的顺序和关联 id
- 对应回归测试：
  - [tests/unit/app/test_task_progress_switch.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_task_progress_switch.py)
  - [tests/unit/agents/test_model_factory_tenant.py](/Users/shixiangyi/code/Swe/tests/unit/agents/test_model_factory_tenant.py)

### 第一阶段处理

- accepted plan 只能通过内部 `assistant` tool-call + `tool` result 注入
- `tool_call_id` / `tool_use_id` 必须复用同一个 call id
- Plan Mode 或缺少 `accepted_plan_source=server_plan_store` 时直接跳过注入

## Console 切换运行中会话时 reconnect 返回 404

### 症状

- 两个 console 会话同时流式输出，前端在会话间快速切换
- 前端发起 `/api/console/chat` reconnect 请求，body 里 `session_id` 可能是本地时间戳格式
- 后端返回 404，detail 为 `No running chat for this session`

### 典型原因

- Console 前端先创建本地时间戳 session，再等待后端创建真实 `chat.id`
- 切换会话会断开当前 SSE，并用 `reconnect=true` 重新附着到后端 `TaskTracker`
- reconnect 请求可能早于后端完成 `session_id -> chat.id -> run_key` 注册，第一次查询映射或 active run 时会查不到

### 第一落点

- [src/swe/app/routers/console.py](/Users/shixiangyi/code/Swe/src/swe/app/routers/console.py)
- 重点看 `_attach_reconnect_queue()` 对 `session_id`、`chat.id` 和 `TaskTracker.attach()` 的处理
- [src/swe/app/runner/task_tracker.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/task_tracker.py)
- 重点看 run_key 是否使用 `ChatSpec.id`
- [console/src/pages/Chat/sessionApi/index.ts](/Users/shixiangyi/code/Swe/console/src/pages/Chat/sessionApi/index.ts)
- 重点看本地时间戳 session 与真实 `chat.id` 的映射

### 第一阶段处理

- reconnect 不要只查一次；在短窗口内重试解析 `session_id -> chat.id` 并附着 active run
- 保持 run_key 统一为 `ChatSpec.id`，不要把前端本地时间戳直接当作 `TaskTracker` key
- 如果问题仍出现，抓取同一请求的 `session_id`、解析出的 `chat_id`、`TaskTracker.list_active_tasks()` 三项证据

## 长 Tool 执行后会话出现用户中断且 Chat 状态卡在 running

### 症状

- Tool 执行时间较长时，前端会话出现类似 `The tool call has been interrupted by the user` 的中断提示
- 查询 `GET /api/chats/{chat_id}` 或 `/api/chats/{chat_id}` 时，返回 `status=running`
- 实际上用户未主动点击停止，或停止已经发出但后端仍在清理资源

### 典型原因

- 前端流式请求存在客户端侧绝对超时，超时后 abort fetch，外层 agent 可能把它解释为用户中断
- 前端 abort 没有区分 `detach`、`stop` 和 `timeout`，切换会话等纯断流动作可能与停止任务混淆
- `TaskTracker.get_status()` 只看 producer task 是否 `done()`，当 stop/timeout 已发出但 producer 仍在 `finally` 清理时会继续返回 `running`
- 旧 producer 的 `finally` 如果无条件删除 `_runs[chat_id]`，可能误删同一 chat 后续新 run 的状态

### 第一落点

- [console/src/pages/Chat/index.tsx](/Users/shixiangyi/code/Swe/console/src/pages/Chat/index.tsx)
- 重点看 `/console/chat` fetch、`createTimedAbortSignal()` 和 stop 调用
- [console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx)
- 重点看 `cancelActiveRequest()` 是否传递真实 `chat_id`
- [src/swe/app/runner/task_tracker.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/task_tracker.py)
- 重点看 `request_stop()`、`mark_stopping()`、`get_status()` 和 producer `finally`
- [src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `_enforce_query_timeout()` 是否在 interrupt 前把 run 标为 `stopping`

### 第一阶段处理

- 默认不要给 chat stream 设置前端绝对超时；只在用户显式 stop 时调用 `/console/chat/stop`
- 用 abort reason 区分：
  - `detach`：切换会话或断开 SSE，只断前端流，不停止后端任务
  - `stop`：用户主动停止，调用后端 stop
  - `timeout`：显式配置的客户端超时，按配置决定是否 stop
- 后端状态使用 `idle/running/stopping`；stop 或 query timeout 发出后先返回 `stopping`，清理完成后再变 `idle`
- producer 清理 `_runs` 时必须确认当前 `_runs[chat_id]` 仍是自己，避免旧 run 清理误删新 run

### 边界说明

- 这只能避免“前端默认超时或断流误杀任务”
- 后端仍可能被配置型超时中止，例如 `SWE_QUERY_TIMEOUT_SECONDS`、`SWE_MCP_PER_NOTIFICATION_TIMEOUT`、`SWE_LOCAL_TOOL_EXECUTION_HARD_TIMEOUT` 或 shell tool 的 `timeout` 参数
- 若要允许超长 MCP tool，MCP server 应定期发送 progress notification，或调大 per-notification timeout

## Stop 预算耗尽提示流出但历史缺失

### 症状

- `Stop` 持续返回 `block` 后，前端能看到“任务未完成”提示
- 刷新或重新加载会话后，历史最后一条仍是上一轮模型回复，看不到预算耗尽提示
- Trace 或 Monitor 里最终输出也可能只记录模型回复，缺少用户实际看到的未完成状态

### 典型原因

- Runner 手动构造并 `yield` 预算耗尽提示，但没有写入 `agent.memory`
- `finally` 阶段保存 session state 时只保存 memory 内容，stream-only 消息会丢失

### 第一落点

- [src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `_stream_completion_lifecycle()` 的 Stop 预算耗尽分支，以及 `_save_regular_session_state()` 保存前 memory 中是否包含同一条提示

### 第一阶段处理

- 对用户可见、需要进入历史的 runner 合成消息，先写入 `runtime.agent.memory`，再 `yield`
- 测试同时断言 stream 输出和 session state 末尾内容，避免只验证前端当次可见

## 当前 Source 系统配置页返回 403 或保存后步骤条行为未变化

### 症状

- 打开 `system-config-page` 直接显示 403，或页面入口在菜单中不可见
- 页面能打开，但保存 `任务进度步骤条` 开关后，聊天页仍继续展示步骤条
- 后端 current-source API 返回 `Manager role required`

### 典型原因

- iframe 上下文没有透传 `isSuperManager` / `manager`，导致前端未发送 `X-User-Role`
- 前端误以为只隐藏 UI 就够了，但没有刷新 effective source config store
- 后端 raw current-source 配置虽然写入成功，但仍被旧的 effective config 缓存命中，或者下一轮请求前没有重新读取

### 第一落点

- [console/src/api/authHeaders.ts](/Users/shixiangyi/code/Swe/console/src/api/authHeaders.ts)
- 重点看 `isSuperManager -> admin`、`manager -> manager` 的头映射是否存在
- [console/src/pages/SystemConfigPage/index.tsx](/Users/shixiangyi/code/Swe/console/src/pages/SystemConfigPage/index.tsx)
- 重点看保存/删除成功后是否调用 `loadEffectiveConfig(activeSourceId)`
- [src/swe/app/source_system_config/router.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/router.py)
- 重点看 `/api/source-system-config/current` 是否只从 `request.state.source_id` 取目标 source，且仍要求 manager/admin
- [src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- [src/swe/agents/tools/update_task_progress.py](/Users/shixiangyi/code/Swe/src/swe/agents/tools/update_task_progress.py)
- [src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 prompt、tool、stream 三段是否都走了 `chat_task_progress_enabled` 判定

### 第一阶段处理

- 先确认请求头里已经带上 `X-User-Role: admin|manager`
- 再确认保存或删除后，前端已经刷新 effective config store，而不是只刷新当前页面表单
- 如果聊天页行为没变化，抓下一轮请求的 effective config，再核对 prompt、tool、stream 三处是否都已关闭

## Tenant bootstrap 时报 default workspace 缺少 agent.json

### 症状

- 首次访问租户、`ensure_bootstrap()` 自愈，或 `TenantInitializer.initialize()` 期间直接失败
- 常见异常为：
  - `FileNotFoundError: Agent config not found: <tenant>/workspaces/default/agent.json`
- 伴随日志里可能先看到：
  - `Config file not found, copying from md_files templates...`
  - `Source file not found: .../src/swe/agents/md_files/config.json`

### 典型原因

- `ensure_default_agent_exists()` 只保证 root `config.json`、`chats.json` 和 `jobs.json`，不会直接生成 default workspace 的 `agent.json`
- `ensure_default_workspace_scaffold()` 在没有模板 `agent.json` 时，如果先 `load_agent_config()`，就会在 fallback 生成之前触发异常
- cached tenant 自愈场景下，`config.json` 或 `agent.json` 被删除后再次 bootstrap，也会走到同一条缺口

### 第一落点

- [src/swe/app/workspace/tenant_initializer.py](/Users/shixiangyi/code/Swe/src/swe/app/workspace/tenant_initializer.py)
- 重点看 `ensure_default_workspace_scaffold()` 是否遵循“优先复制模板，没有模板再按 tenant root config 合成 fallback agent.json”
- [src/swe/app/migration.py](/Users/shixiangyi/code/Swe/src/swe/app/migration.py)
- 重点看 `ensure_default_agent_exists()` / `_do_ensure_default_agent()` 只负责最小 bootstrap，不要误以为它会补齐 workspace 级 `agent.json`

### 第一阶段处理

- 先确认 default 模板租户是否存在 `workspaces/default/agent.json`
- 有模板时，优先检查模板复制路径和 `workspace_dir` 重写是否正确
- 没模板时，检查 fallback `AgentProfileConfig` 是否从 tenant root `config.json` 正确构造并落盘
- 回归至少覆盖三类路径：
  - 首次初始化
  - 从 default 模板复制 agent 配置
  - cached tenant 删除 `agent.json` 后再次 `ensure_bootstrap()` 自愈

## Cron 结果索引出现同一 trace 的重复子任务

### 症状

- `swe_cron_subtasks` 在同一 `trace_id` 下存在多条成功的 `list` 子任务，或同一客户存在多条成功的 `plan` 子任务
- `/monitor/subtasks/executions/sync-async-status` 写入 `swe_cron_result_index` 时产生重复结果

### 典型原因

- 重试或历史写入留下脏子任务；索引流程若直接消费全部成功子任务，会把重复数据继续写入结果索引

### 第一落点

- [monitor/src/monitor/app/services/subtask/query_service.py](/Users/shixiangyi/code/Swe/monitor/src/monitor/app/services/subtask/query_service.py)
- 重点看成功子任务查询是否按 `trace_id + task_type`（`list`）及 `trace_id + custuid`（`plan`）去重，并优先保留最新子任务

## Cron 模型失败但执行记录显示成功

- 症状：Runtime 报 `model_call_failed`，随后 Cron 日志却出现 `completed_seen=True failed_seen=False` 和 `exec_status=success`。
- 原因：Runtime 将模型异常转换为 `response/Failed`；仅检测 `message/Failed` 会漏判，并将此前的消息完成误作执行成功。
- 排查入口：`src/swe/app/crons/executor.py` 的 `_is_failed_message_event()` 必须同时识别消息与整轮响应失败；失败优先于此前的 Completed。
- 回归：`tests/unit/app/test_cron_manager_completed_cancellation.py` 覆盖先收到 `message/Completed`、再收到 `response/Failed`，验证任务与执行记录均为 `error` 且保留错误详情。


## 聊天语音输入不可用或识别失败

- 入口：`console/src/components/agentscope-chat/DictationControl/` 与 `Sender/useSpeech.ts`。新会话和会话内输入框共用短听写能力；加号菜单的白名单「语音录制」仍走原独立链路。
- 浏览器需同时支持 `SpeechRecognition`（或 `webkitSpeechRecognition`）、麦克风采集和安全上下文（HTTPS / localhost）。嵌入页面还需要宿主允许麦克风权限。浏览器提供 API 不代表其语音服务在当前网络可达。
- `not-allowed`：检查浏览器站点麦克风权限及宿主 Permissions Policy；`audio-capture`：检查麦克风连接/占用；`network`：检查浏览器识别服务网络，不要修改录音白名单或录音服务配置来修复短听写。
- 语音识别跟随界面语言，不能使用当前仍为 `en` 的 HTML 根语言推断中文识别参数。实时结果只作预览，停止后追加草稿且不发送；取消或识别失败保留原草稿。
- 本地回归：`cd console && npm run test:run -- src/components/agentscope-chat/Sender/useSpeech.test.tsx`。浏览器 QA 的合成音频/模拟识别验证 UI 与生命周期，不能替代目标浏览器和部署网络上的真实语音服务联调。
## 分享工具栏空状态错位

- 入口：`console/src/pages/Chat/components/ChatActionGroup/index.tsx` 和同目录 `index.module.less`。
- 空状态提示作为独立 Grid 子项会触发自动排布，把关闭按钮挤到下一行。提示文字应归入选择状态区域；选择、分享操作、关闭按钮使用明确的网格区域。
- 分享栏通过 Portal 挂到 `document.body`，宽度取自 `[data-chat-messages-area]`。响应式排布应按分享栏自身容器宽度切换，不能只按浏览器视口判断嵌入区域的可用空间；固定宽度包含 padding，需使用 `box-sizing: border-box`。
- 回归覆盖：无可分享内容、全选、部分选择、取消全选、退出分享模式、键盘焦点，以及 375px 窄容器与 768/1024/1440px 布局。禁用按钮的图标应跟随禁用文字颜色，半选框保留白底与蓝色短横。
- 分享模式需要隐藏输入框时，保留 Input 挂载以维持草稿和附件状态；整体隐藏还要覆盖编辑器子层显式声明的可见性。输入框提交入口在异步 `beforeSubmit` 前后检查当前分享状态，防止校验期间进入分享仍发出消息。回归位于 Runtime `core/Chat/Input/index.test.tsx`，覆盖隐藏、恢复草稿与延迟提交拦截。
