# Hook Runtime 使用说明

Hook Runtime 用于在 Agent 的关键运行节点挂接自定义策略。你可以把它理解成一套“事件触发器”：

- 在用户输入进入 Agent 之前做检查
- 在工具调用前后追加策略
- 在回复准备结束时做完成度门禁
- 在需要时阻断、改写、补充上下文，或请求人工审批

本文面向普通使用者和管理员，重点回答 4 个问题：

1. hook 什么时候触发
2. 应该把配置写在哪里
3. handler 能返回什么
4. 返回结果会对当前会话产生什么实际影响

本文不展开内部类设计和源码细节，但所有说明都已按当前项目实现核对。

## 先看结论

如果你只想快速上手，先记住下面几条：

- `PreToolUse` 是最常用的事件：可以拒绝工具、要求审批、改写工具输入。
- `PostToolUse` 和 `PostToolUseFailure` 不能撤销已经发生的工具调用，但现在所有经 Tool Guard 执行的工具路径（包括预批准和受保护的内置工具）都会进入对应的后置 hook；它们适合审计、诊断，或用终止决策结束当前回合。
- `continue: false` 或 `decision: "stop"` 是回合级终止，而不只是“阻断当前工具”：在工具事件中命中后会阻止后续推理，并取消仍在等待的并行工具调用。
- `Stop` 是统一完成门禁：普通 handler 并发执行，合并后的 `allow` / `block` 决定候选回复是否完成。显式 `outputTransform: true` 会先串行改写候选文本，再进入普通校验；显式 `block` 可触发有上限的自动续跑，`failPolicy: block` 的执行失败则直接以未完成结束。
- `Stop + outputAppend: true` 在最终放行后生成独立 Markdown 消息；仅启用追加时正文继续流式输出。追加失败尽力跳过，不否决正文。
- Skill 自带 hook 只有在该 Skill 在当前会话里被激活后才会生效。
- 多个 handler 会并发执行，不要依赖“前一个 handler 的输出给后一个 handler 使用”。

## 适合解决什么问题

- 在每次请求开始前注入项目约束、组织规则或环境说明
- 在用户输入进入 Agent 前检查敏感内容
- 在执行 shell、文件、网络类工具前做额外策略判断
- 对高风险工具调用弹出人工审批
- 在工具执行成功或失败后补充审计和诊断信息
- 在回复结束前要求先完成测试、构建或 lint
- 在回合结束时向外部审计或埋点系统记录最终状态

## 新人推荐阅读顺序

如果你是第一次配置 hook，建议按下面顺序阅读和落配置：

1. 先看“事件与真实生效时机”，确定你要拦的是哪一个阶段。
2. 再看“配置写在哪里”和“最小配置”，先把文件位置和 JSON 外形搭起来。
3. 然后看“`command` / `http` / `prompt`”三类 handler，选一种最适合你的执行方式。
4. 最后再看 “HookContext 里有哪些字段” 和 “handler 能返回什么”，决定脚本里读哪些入参、返回哪些结果。

如果只想尽快做出第一个可验证的 hook，最稳妥的起点通常是：

- `PreToolUse + command`
- 或 `UserPromptSubmit + http`

因为这两类最容易观察是否命中，也最容易验证返回效果。

## 事件与真实生效时机

下表是最重要的部分。配置 hook 时，先确认你要拦的是哪个阶段。

| 事件 | 什么时候触发 | 用户通常会看到什么 | 适合做什么 |
| --- | --- | --- | --- |
| `SessionStart` | 每次请求进入 Agent 主流程前 | 通常无明显提示；如果被阻断，会直接返回阻断原因 | 注入本轮初始上下文、记录开始事件 |
| `UserPromptSubmit` | 当前请求含有文本用户输入时，在 Agent 处理前 | 通常无明显提示；如果被阻断，会直接返回阻断原因 | 检查输入、设置会话标题、补充本轮上下文 |
| `PreToolUse` | 工具真正执行前 | 可能直接放行、拒绝、改写输入，或弹出审批卡片 | 工具审批、参数检查、命令改写 |
| `PostToolUse` | 工具成功返回后 | 成功结果已经保留；普通 `block` 不会撤销它，终止决策会结束当前回合 | 审计记录、补充工具结果说明、在结果出现后停止继续推理 |
| `PostToolUseFailure` | 工具调用抛出失败后 | 原始失败已被记录；普通 `block` 不会吞掉它，终止决策会结束当前回合 | 记录错误、提示排查方向、在失败后停止继续推理 |
| `Stop` | 候选回复生成后、正式结束前 | 普通 Stop 时用户通常已看到候选回复；存在潜在输出变换器时，文本会先被保留 | 完成门禁、测试门禁、发布前检查、最终文本格式化 |

实际请求路径里还有一个顺序细节：如果当前请求带有文本用户输入，`UserPromptSubmit` 会在 preflight 阶段先执行；之后系统装配 Agent 主流程时才执行 `SessionStart`。所以上表按生命周期概念排序，不表示所有请求里严格按表格顺序触发。

工具侧的执行路径已经统一：工具成功后都会触发 `PostToolUse`，工具执行抛错后都会触发 `PostToolUseFailure`。这包含普通调用、Tool Guard 预批准调用，以及受保护的 source-built-in tool。后置 hook 仍然不能撤销外部副作用；它能写入上下文，或用终止决策阻止 Agent 基于该结果继续推进。

### 关于 `Stop`

`Stop` 是“现在能不能结束”的唯一事件。普通 Stop 在候选回复生成后运行一次：handler 可以记录该尝试，随后返回 `allow` 批准完成或返回 `block` 要求 Agent 在同一请求中继续。每次续跑后的新候选回复都会再次触发 Stop，因此审计系统应将其记录为独立完成尝试。

### Stop 输出变换器

在 `Stop` handler 上声明 `"outputTransform": true` 可把它变为最终文本变换器。只要事件、matcher、来源和 overlay 存在潜在匹配，候选 assistant 文本会在 `if` 求值前被保留；变换器按“租户 → Agent Profile → 已激活 Skill（按 `skill_name` 排序）”串行运行，后一个看到前一个的替换结果。之后所有普通 Stop handler 并发校验最终文本。

```json
{
  "events": {
    "Stop": [{
      "hooks": [
        {"id":"format","type":"http","url":"https://policy.example/rewrite","outputTransform":true,"timeout":5,"failPolicy":"block"},
        {"id":"audit","type":"command","argv":["python","hooks/scripts/audit_final.py"]}
      ]
    }]
  }
}
```

变换器只能返回 `allow`，并可选择完整替换文本：

```json
{"decision":"allow","reason":"formatted","hookSpecificOutput":{"replacementText":"final text"}}
```

省略 `replacementText` 表示保持当前文本。`outputTransform` 仅能用于 `Stop`，不能与 `once: true` 同用；`replacementText` 只能由变换器使用。总时限由 Agent Profile 的 `running.hook_runtime.max_stop_transform_seconds` 控制，默认 30 秒。`failPolicy: allow` 失败时保留当前文本继续；`failPolicy: block` 或预算耗尽时本轮以未完成结束，不投递文本也不自动续跑。

变换仅作用于可提取文本（字符串或 text block）；工具进度、审批、附件和工具卡片继续实时输出。显式请求的会话快照保持原有行为，可能含原始候选文本。应用日志只记录变换元数据、长度和 SHA-256，不记录候选或替换正文。

如果想直接复用 command handler 的最小实现，可参考
[stop-output-transform-command-demo](stop-output-transform-command-demo/SKILL.md)。它演示
从 `assistant_response` 读取候选文本，并仅在文本实际变化时返回合法的
`hookSpecificOutput.replacementText`。

### Stop 附加消息

在 Stop handler 上声明 `"outputAppend": true`，可在正文最终放行后追加一条独立 Markdown
消息。command、HTTP、prompt 共用此协议；prompt 会额外调用模型。普通问答只在最终
allow 后执行一次追加阶段，Goal 只在正式 Finalization 放行后执行；block 尝试、空正文、
计划交互边界、工具终止、固定 fallback 或 hooks 关闭时均不追加。
首期只有 Console 执行追加阶段，其他 Channel 直接跳过 handler，不产生脚本副作用。

```json
{
  "enabled": true,
  "events": {
    "Stop": [{
      "id": "completion-supplement",
      "hooks": [{
        "id": "reference-links",
        "type": "command",
        "argv": ["python", "scripts/append_reference_links.py"],
        "outputAppend": true,
        "timeout": 10
      }]
    }]
  }
}
```

三阶段顺序为“可选输出转换 → 普通 Stop 校验 → 放行后的追加”。追加 handler 不参与前两阶段。
仅启用追加不会暂存正文；如同时有潜在输出转换器，仍沿用原有暂存语义。
先交付最终正文，再发送追加消息，整个追加阶段在原请求结束前完成。

handler 输入沿用 HookContext：事件为 Stop，`assistant_response` 为已放行的最终正文。
阶段入口刷新激活 Skill 并冻结匹配计划（包括来源、matcher、overlay、`if`），
所有 handler 并发读取同一正文；请求会话快照时读取同一份追加前快照，仍受各自上限和脱敏约束。
结果按固定顺序交付：租户 → Agent Profile → Skill（按 `skill_name` 排序），
各来源内按 matcher group、handler 声明顺序；不同来源同名 ID 保持独立。
执行完成顺序不决定气泡顺序，也不决定累计预算分配。

command 须退出 0 并在 stdout 返回 JSON；HTTP 须返回 2xx JSON；prompt 返回相同结构：

```json
{
  "decision": "allow",
  "reason": "reference links collected",
  "hookSpecificOutput": {
    "appendText": "### 参考链接\n\n[规范](https://example.org/spec)"
  }
}
```

顶层只接受 `decision`、`reason`、`hookSpecificOutput`；reason 为非空诊断说明，最多
2,000 字符，不显示在聊天中。嵌套只接受 `appendText`；省略它或返回空对象表示成功但不追加。
如提供，必须是含非空白内容的字符串，原文保留。拒绝 block/deny/stop、replacementText、
角色、任意 metadata、权限与上下文字段。未声明追加的 handler 不获得此能力。
`outputAppend` 只允许 Stop，且与同一 handler 的 `outputTransform: true`、`once: true` 互斥；
保存接口拒绝非法组合。

每个 handler 最多产生一条独立消息，作为真实 assistant 消息进入 Agent memory、
会话历史、分享和后续模型上下文，属于原 Answer Turn。它遵循普通上下文预算与压缩规则，
不保证永久保留在模型输入中；不会递归触发 Stop、Goal 评审或模型续跑。
Console 的实时、刷新和重连使用运行时控制的 `metadata.stop_append.message_id` 识别同一消息，
避免与正文合并或因回放重复显示。

资源与失败边界：

- 单 handler 默认超时 10 秒；阶段生成/收集总预算默认 30 秒，由 Agent Profile 的
  `running.hook_runtime.max_stop_append_seconds` 配置，必须为正有限数。租户和 Skill
  不能提高阶段总预算。截止时回收未完成任务，保留截止前成功结果并有序交付。
- 单条最多 **16,384 UTF-8 字节**，每轮累计最多 **65,536 UTF-8 字节**。
  超限跳过、不截断；某条超过剩余额度时，后续较小消息仍可使用余额。
- 原始 command stdout、HTTP 解码响应和 prompt 响应各最多 **256 KiB**，command stderr
  最多 **64 KiB**，读取时即限制；仅追加模式使用这些限制。
- 单项执行失败、超时、非法协议或超限均尽力跳过，其他 handler 继续。有效失败策略固定为
  **allow**，保存的 `failPolicy: block` 也不改变它；不否决正文、不调用模型重试、不插错误气泡。
- 每条消息先加入 memory 并通过会话事务持久化，成功提交后才发送。存储失败不是 handler
  失败：不发送未保存消息、撤销未提交 memory 变更、停止后续追加，进入现有持久化恢复路径；
  不重新运行模型或脚本。
- 用户停止会取消剩余任务，保留已成功提交的消息，丢弃未提交结果。脚本受控进程和本地请求
  尽力清理，不承诺回滚外部副作用、远端请求或已脱离控制的进程。
- 单纯 SSE 断线后台继续执行；重连恢复已保存消息，不重跑追加阶段。进程崩溃后仅恢复已有
  消息，不补跑脚本，未提交结果允许丢失；不提供外部副作用的 exactly-once 保证。

Hook 管理页可为三种 handler 开启“追加独立消息”，显示固定有效策略及预算。
人工测试要求非空示例 `assistant_response`，实际运行一个 handler；只返回脱敏统计：
`output_append`、`append_applied`、`append_bytes`（UTF-8）、`effective_fail_policy: "allow"`、
`duration_ms`，以及既有 status/failed/failure_type 等字段。
它不写真实 Chat、Agent memory 或草稿配置，不回显 appendText、reason、原始 stdout/stderr/HTTP body；
handler 本身的外部副作用仍然真实发生。单项测试不能证明跨来源顺序、累计预算或恢复行为。
运行诊断只保留来源、handler、turn/trace、消息 ID、顺序、耗时、字节数、哈希和失败类别。

特性总结：[Stop 附加消息](stop-appended-message.md)。
可运行样例：[stop-output-append-command-demo](stop-output-append-command-demo/SKILL.md)。

## 配置写在哪里

Hook 可以配置在 3 个层级：

1. 租户级
2. Agent 级
3. Skill 级

普通场景优先用租户级。只有确实要针对某个 workspace 或某个 Skill 单独控制时，再使用后两者。

### 租户级

配置文件：

```text
~/.swe/<tenant_id>/config.json
```

例如：

```text
~/.swe/default/config.json
```

租户级 hook 写在根节点的 `hooks` 字段下。

### Agent 级

配置文件：

```text
~/.swe/<tenant_id>/workspaces/<workspace_id>/agent.json
```

Agent 级 hook 同样写在根节点的 `hooks` 字段下。

### Skill 级

配置文件：

```text
~/.swe/<tenant_id>/workspaces/<workspace_id>/skills/<skill_name>/hooks/hooks.json
```

Skill 级配置和前两者不同：

- 文件根对象直接就是 hook 配置
- 不需要再包一层 `hooks`
- 只有当这个 Skill 在当前会话里被激活后，里面的 hook 才会生效

### 默认 Agent 的 Hook 管理页

控制台的 Hook 管理页管理的是默认 Agent profile 的 hook 配置；它是手工编辑 JSON 之外的受控入口。通过该入口保存配置或上传脚本时，运行时会校验配置并记录审计信息。

- `command` handler 必须使用 `argv`；管理页不接受 `command` 字符串。
- 由管理页上传的脚本只会放入受控的 `hooks/scripts/` 库，文件名不能包含路径；支持 `.py`、`.sh`、`.bash`、`.zsh`，单文件最多 1 MiB、单次最多 20 个。
- 引用脚本时必须使用 `hooks/scripts/<filename>`；符号链接、`..` 路径和越出受控库的引用都会被拒绝。保存后的脚本权限会收紧为仅属主可访问。
- 上传会经过安全扫描：扫描器明确拒绝的文件不会写入；无法判定安全的文件会带警告保留，需由管理员复核。
- “手工测试”会实际执行一个尚未保存的 handler，可能产生网络或本地命令副作用；测试结果会返回脱敏摘要并写入审计日志，不应直接使用生产敏感 payload。

## 最小配置

租户级或 Agent 级配置示例：

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "PreToolUse": [
        {
          "id": "shell-policy",
          "matcher": {
            "tools": ["execute_shell_command"]
          },
          "hooks": [
            {
              "id": "check-shell",
              "type": "command",
              "argv": ["python", "hooks/check_shell.py"],
              "timeout": 5,
              "failPolicy": "block"
            }
          ]
        }
      ]
    }
  }
}
```

Skill 级 `hooks/hooks.json` 示例：

```json
{
  "enabled": true,
  "events": {
    "PreToolUse": [
      {
        "id": "shell-policy",
        "matcher": {
          "tools": ["execute_shell_command"]
        },
        "hooks": [
          {
            "id": "check-shell",
            "type": "command",
            "argv": ["python", "scripts/check_shell.py"],
            "timeout": 5,
            "failPolicy": "block"
          }
        ]
      }
    ]
  }
}
```

## 配置结构

### 根字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `enabled` | 否 | 是否启用当前 hook 配置。默认 `false`。 |
| `events` | 否 | 事件配置，key 是事件名，value 是该事件下的匹配分组列表。省略时等同于没有可执行 hook。 |

### 分组字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `events.<event>[].id` | 否 | 分组 ID。建议填写，便于排查和去重。 |
| `events.<event>[].matcher` | 否 | 匹配条件。当前最常用的是 `matcher.tools`。 |
| `events.<event>[].hooks` | 否 | 该分组下的 handler 列表。省略或为空时该分组不会执行任何 handler。 |

### `matcher.tools`

`matcher.tools` 用于限制该分组只对指定工具名生效，按工具名精确匹配：

```json
{
  "matcher": {
    "tools": ["execute_shell_command", "read_file"]
  }
}
```

如果不写 `matcher`，或 `matcher.tools` 为空，则表示该事件下全部请求都可能命中该分组。

## Handler 通用字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | handler ID。应在同一配置来源内保持唯一且稳定；普通租户级 / Agent 级配置当前不会强制校验全局唯一，重复 ID 会让 `once`、覆盖和排查变得含糊。 |
| `type` | 是 | handler 类型，支持 `command`、`http`、`prompt`。 |
| `if` | 否 | 条件表达式；结果为假时跳过该 handler。 |
| `timeout` | 否 | 单个 handler 超时时间，单位秒，默认 `10`。 |
| `statusMessage` | 否 | 阻断或审批时显示给用户的提示文案。 |
| `once` | 否 | `true` 表示同一实际生效租户、同一用户、同一会话、同一事件、同一 handler ID 只执行一次。 |
| `includeConversationSnapshot` | 否 | `true` 时，仅该 handler 会额外收到 `conversation_snapshot` 和 `conversation_snapshot_meta`。默认 `false`。 |
| `conversationSnapshotLimit` | 否 | 快照最多携带多少条最近消息。默认 `50`，最大 `200`；仅在 `includeConversationSnapshot: true` 时生效。 |
| `failPolicy` | 否 | handler 自身执行失败时的处理策略，支持 `allow` 或 `block`。`command` / `http` 默认 `allow`，`prompt` 默认 `block`。 |
| `outputTransform` | 否 | 仅 `Stop` 可用，默认 `false`。为 `true` 时 handler 串行改写最终文本，且不能设置 `once: true`。 |
| `outputAppend` | 否 | 仅 `Stop` 可用，默认 `false`。放行后追加独立消息；与同一 handler 的 `outputTransform: true`、`once: true` 互斥，有效失败策略固定为 allow。 |

### `once: true` 的实际含义

`once: true` 不是“同一条命令只执行一次”，而是：

- 同一个实际生效租户
- 同一个用户
- 同一个会话
- 同一个事件
- 同一个 handler ID

只运行一次。跨轮次也会记住，直到该会话结束或会话状态被清空。

### `includeConversationSnapshot` 的实际语义

这是新增的 handler 级开关，不是全局开关。

- 只有显式写了 `includeConversationSnapshot: true` 的 handler，才会收到快照字段。
- 快照优先来自当前 Agent 的内存消息；runner 早期事件还没有可用 Agent 时，会尝试读取 session state 中保存的 memory。它不是去读 `transcript_path` 回放完整持久化记录。
- `conversationSnapshotLimit` 是“每个 handler 自己的截断上限”，所以不同 handler 可以拿到不同长度的快照。
- 当前事件本身仍然通过 `prompt`、`tool_input`、`tool_response`、`assistant_response`、`error` 等字段单独传入；快照不会重复制造一份“当前事件副本”。

配置示例：

```json
{
  "id": "post-tool-audit",
  "type": "http",
  "url": "http://127.0.0.1:9000/hooks/mcp-posttool",
  "includeConversationSnapshot": true,
  "conversationSnapshotLimit": 20,
  "timeout": 5,
  "failPolicy": "allow"
}
```

如果运行时当前拿不到 Agent 内存，handler 仍会继续执行，但会收到：

```json
{
  "conversation_snapshot": [],
  "conversation_snapshot_meta": {
    "included_messages": 0,
    "omitted_messages": 0,
    "limit": 50,
    "unavailable": true,
    "unavailable_reason": "agent_memory_unavailable"
  }
}
```

### `if` 表达式怎么写

`if` 适合做轻量过滤。当前实现支持的语法很简单，建议只用：

- `==`
- `!=`
- `in`
- `not in`
- `and`
- `or`
- `not`
- 字段取值，例如 `tool_name`
- 字典取值，例如 `tool_input["command"]`

示例：

```json
{
  "if": "tool_name == 'execute_shell_command' and 'rm -rf' in tool_input['command']"
}
```

注意：

- `if` 表达式写错时，handler 不会报出漂亮的配置提示，通常表现为“这个 handler 没有命中”。
- 因此建议先从最简单的条件开始验证。

## 三类 Handler

当前支持 3 类 handler：`command`、`http`、`prompt`。

### 1. `command` handler

`command` handler 会在当前 workspace 内执行本地命令。

- hook 上下文通过标准输入传入，格式为 JSON
- handler 的标准输出如果非空，必须是合法 JSON 对象
- 标准错误适合写调试日志和错误信息

配置示例：

```json
{
  "id": "check-shell",
  "type": "command",
  "argv": ["python", "hooks/check_shell.py"],
  "timeout": 5,
  "failPolicy": "block"
}
```

可用字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `argv` | 二选一 | 参数数组形式。推荐写法，尤其适合调用脚本。 |
| `command` | 二选一 | shell 命令字符串。普通租户级 / Agent 级 hook 可用；Skill 级 hook 禁用。 |
| `shell` | 否 | 使用 `command` 字符串时可指定 shell，支持 `sh`、`bash`、`zsh`、`cmd`、`powershell`。 |
| `cwd` | 否 | handler 执行目录。普通 hook 必须在当前 workspace 内；Skill hook 必须在 Skill 目录和 workspace 内。 |
| `env` | 否 | 追加给子进程的字面量环境变量。Skill 级 hook 禁用。 |

模型层里还会拒绝 `async` 和 `asyncRewake`，当前 command hook 不支持异步后台执行。

#### 建议做法

- 普通租户级 / Agent 级 hook：脚本放在当前 workspace 内，例如 `hooks/check_shell.py`
- Skill 级 hook：脚本放在 Skill 自己的 `scripts/` 目录内
- stdout 只输出最终 JSON；日志一律写 stderr

#### 退出码语义

| 退出码 | 含义 |
| --- | --- |
| `0` | 执行成功；stdout 可为空，或返回 JSON 对象 |
| `2` | 直接阻断当前事件 |
| 其他非零 | 视为 handler 失败，是否阻断由 `failPolicy` 决定 |

#### 路径限制

普通 `command` handler 受 workspace 边界约束：

- `cwd` 必须在当前 workspace 内
- `argv` 里出现的绝对路径，不能越出当前 workspace
- 如果使用 `command` 字符串，里面涉及的文件路径也会按 workspace 做边界校验

这意味着：

- 你可以通过 `python`、`bash` 这类命令名调用系统 PATH 里的程序
- 但不应该把 hook 脚本或目标文件放到当前 workspace 外面
- 不要把 `/usr/bin/python` 这类绝对路径写进 `argv`；绝对路径参数同样会按 workspace 边界检查

### 2. `http` handler

`http` handler 会把 HookContext 作为 JSON 请求体，POST 到指定地址。

配置示例：

```json
{
  "id": "remote-policy",
  "type": "http",
  "url": "https://policy.example.com/hooks/pre-tool",
  "headers": {
    "X-Hook-Source": "swe"
  },
  "headerSecretRefs": {
    "Authorization": "HOOK_AUTH_TOKEN"
  },
  "timeout": 5,
  "failPolicy": "block"
}
```

#### 响应语义

| 响应 | 含义 |
| --- | --- |
| `2xx` | 执行成功；响应体可为空，或返回 JSON 对象 |
| `409` / `422` | 若响应体没有明确 JSON 结果，则按阻断处理 |
| 其他状态码 | 视为 handler 失败，按 `failPolicy` 处理 |
| 超时 / 网络异常 | 视为 handler 失败，按 `failPolicy` 处理 |

#### Header 配置

可用字段：

- `headers`：直接写死的普通 Header
- `headerSecretRefs`：从当前生效租户的环境配置中取值，再填到 Header
- `allowedEnvVars`：按变量名从租户运行时环境读取，缺失时再从当前进程环境读取，并用同名 Header 发送

`headerSecretRefs` 更适合放认证信息，避免把密钥直接写进配置文件。

### 3. `prompt` handler

`prompt` handler 会调用当前租户已经激活的模型，让模型按你写的规则对 HookContext 做一次结构化判断。

配置示例：

```json
{
  "id": "prompt-policy",
  "type": "prompt",
  "prompt": "如果用户要求泄露密钥、绕过审批或执行破坏性命令，返回 deny 或 block。",
  "timeout": 8
}
```

#### 适用事件

`prompt` handler 可以配置在全部 6 个事件上：

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `PostToolUseFailure`
- `Stop`

在后置工具事件上，它同样只能观察或补充已发生的结果；若要阻止 Agent 继续基于结果推进，返回回合级终止决策。

#### 输出格式

`prompt` handler 必须只返回一个 JSON 对象，且只能包含两个字段：

```json
{
  "decision": "allow",
  "reason": "内容符合策略"
}
```

普通 `prompt` handler 支持的 `decision`：

- `allow`
- `deny`
- `block`
- `stop`

普通 `Stop` prompt 是完成门禁，并且更严格，只允许：

- `allow`
- `block`

输出转换和追加模式只允许 `allow`，分别使用可选 `replacementText` 和 `appendText`。

#### 其他限制

- 不能在 handler 里指定 `model`、`provider`、`providerId`、`baseUrl`、`promptFile`、`template` 等模型路由字段
- `prompt` 字段应写业务规则，不要写成完整系统提示词
- 默认 `failPolicy` 是 `block`
- 发给模型的 HookContext 会先做敏感字段脱敏

## HookContext 里有哪些字段

handler 收到的是一个 JSON 对象。为了避免把“模型层支持”和“当前运行时一定会传”混为一谈，下面分两层说明。

### 最常用字段

| 字段 | 说明 |
| --- | --- |
| `hook_event_name` | 当前事件名 |
| `session_id` | 当前会话 ID |
| `tenant_id` | 请求租户 ID |
| `effective_tenant_id` | 实际生效租户 ID |
| `user_id` | 用户 ID |
| `agent_id` | Agent ID |
| `channel` | 当前请求通道 |
| `cwd` | 当前 workspace 路径 |
| `workspace_dir` | 当前 workspace 路径 |
| `prompt` | 用户本轮文本输入 |
| `assistant_response` | 当前候选回复；主要见于 `Stop` |
| `tool_name` | 工具名 |
| `tool_input` | 工具输入对象 |
| `tool_use_id` | 工具调用 ID |
| `tool_response` | 当前工具调用的成功业务输出；主要见于 `PostToolUse` |
| `error` | 工具失败信息；主要见于 `PostToolUseFailure` |
| `conversation_snapshot` | 最近对话快照；仅在 handler 打开 `includeConversationSnapshot` 时出现 |
| `conversation_snapshot_meta` | 快照的截断/脱敏/不可用说明；仅在 handler 打开 `includeConversationSnapshot` 时出现 |

### 当前实现支持的完整字段

下表按当前 `HookContext` 模型和实际构造逻辑整理。

| 字段 | 当前运行时是否会注入 | 主要出现位置 / 说明 |
| --- | --- | --- |
| `session_id` | 是 | runner 事件、tool 事件都会传 |
| `transcript_path` | 是 | runner 事件、tool 事件都会传 |
| `cwd` | 是 | 当前 workspace 根路径 |
| `hook_event_name` | 是 | 全部事件都会传 |
| `tenant_id` | 是 | 全部事件都会传 |
| `effective_tenant_id` | 是 | 全部事件都会传 |
| `user_id` | 是 | 全部事件都会传 |
| `agent_id` | 是 | 全部事件都会传 |
| `channel` | 是 | 全部事件都会传 |
| `permission_mode` | 否 | 当前模型层支持，但当前 hook 构造逻辑未注入 |
| `effort` | 否 | 当前模型层支持，但当前 hook 构造逻辑未注入 |
| `agent_type` | 否 | 当前模型层支持，但当前 hook 构造逻辑未注入 |
| `source_id` | 部分 | runner 侧事件会传；正常请求路径下 tool 侧事件也会从请求上下文传入；缺失 request context 时可能为空 |
| `workspace_dir` | 是 | 全部事件都会传，通常与 `cwd` 相同 |
| `chat_id` | 部分 | 请求上下文里有 chat 时会传 |
| `turn_id` | 部分 | 请求上下文里有 turn 时会传 |
| `source` | 部分 | `SessionStart` 这类 runner 事件会传；当前主流程常见值是 `startup` / `resume` |
| `model` | 部分 | 当前主流程只在 `SessionStart` 传当前激活模型标签 |
| `prompt` | 部分 | `UserPromptSubmit`、`Stop` 常见；tool 事件当前不传 |
| `tool_name` | 部分 | `PreToolUse` / `PostToolUse` / `PostToolUseFailure` 传 |
| `tool_input` | 部分 | `PreToolUse` / `PostToolUse` / `PostToolUseFailure` 传 |
| `tool_use_id` | 部分 | `PreToolUse` / `PostToolUse` / `PostToolUseFailure` 传 |
| `tool_response` | 部分 | 主要见于 `PostToolUse`，值为当前工具调用最终 `tool_result.output`；无法提取时省略 |
| `assistant_response` | 部分 | 主要见于 `Stop` |
| `error` | 部分 | 主要见于 `PostToolUseFailure` |
| `conversation_snapshot` | 部分 | 仅对声明了 `includeConversationSnapshot: true` 的 handler 注入；值是当前 Agent 内存或 session state memory 里的最近若干条规范化消息 |
| `conversation_snapshot_meta` | 部分 | 与 `conversation_snapshot` 配套；包含 `included_messages`、`omitted_messages`、`limit` 以及可选的 `reasoning_omitted` / `media_content_omitted` / `unavailable` 信息 |

这张表的关键结论是：

- 如果你写的是 runner 侧 hook，例如 `SessionStart`、`UserPromptSubmit`、`Stop`，重点看 `prompt`、`assistant_response`、`source`、`model`。
- 如果你写的是 tool 侧 hook，例如 `PreToolUse`、`PostToolUse`、`PostToolUseFailure`，重点看 `tool_name`、`tool_input`、`tool_use_id`、`tool_response`、`error`。其中 `tool_response` 是当前工具调用的业务输出，不是完整 `tool_result` 块，也不需要通过 `includeConversationSnapshot` 获取。
- 如果你要把“当前事件字段”与“最近对话上下文”一起送给外部策略，就同时使用事件字段和 `conversation_snapshot`，不要试图只从快照里反推当前事件。
- `permission_mode`、`effort`、`agent_type` 虽然在模型里有字段，但当前实现还没有把它们接进真实 hook payload，不要把它们当成当前可依赖入参。

### 两类典型 payload 样子

#### 1. `SessionStart` 常见 payload

```json
{
  "session_id": "session-1",
  "transcript_path": "/path/to/session-1.json",
  "cwd": "/workspace/project",
  "hook_event_name": "SessionStart",
  "tenant_id": "default",
  "effective_tenant_id": "default",
  "user_id": "user-1",
  "agent_id": "demo-agent",
  "channel": "console",
  "source_id": "console",
  "workspace_dir": "/workspace/project",
  "chat_id": "chat-1",
  "turn_id": "turn-1",
  "source": "startup",
  "model": "openai/gpt-5.4"
}
```

#### 2. `PostToolUse` 常见 payload

```json
{
  "session_id": "session-1",
  "transcript_path": "/path/to/session-1.json",
  "cwd": "/workspace/project",
  "hook_event_name": "PostToolUse",
  "tenant_id": "default",
  "effective_tenant_id": "default",
  "user_id": "user-1",
  "agent_id": "demo-agent",
  "channel": "console",
  "workspace_dir": "/workspace/project",
  "chat_id": "chat-1",
  "turn_id": "turn-1",
  "tool_name": "execute_shell_command",
  "tool_input": {
    "command": "echo hello"
  },
  "tool_use_id": "toolu_123",
  "tool_response": "hello"
}
```

`PostToolUse.tool_response` 表示当前工具调用的最终业务输出。运行时会从同一 `tool_use_id` 对应的终态 `tool_result.output` 提取该值；它不包含 live/intermediate chunks，不包含完整 `tool_result` 的 `type` / `id` / `name` 包装，也不复用 `Hook Conversation Snapshot`。如果运行时无法找到对应终态输出，会省略 `tool_response` 字段，但仍继续发送 `PostToolUse`。

#### 3. `PostToolUse` 同时带 `tool_response` 和会话快照

如果某个 handler 打开了 `includeConversationSnapshot`，它拿到的 payload 典型会像这样：

```json
{
  "hook_event_name": "PostToolUse",
  "tool_name": "execute_shell_command",
  "tool_input": {
    "command": "echo hello"
  },
  "tool_use_id": "toolu_123",
  "tool_response": "hello",
  "conversation_snapshot": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "帮我执行一个 echo 命令"
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "text",
          "text": "我先执行这个命令。"
        },
        {
          "type": "tool_use",
          "id": "toolu_123",
          "name": "execute_shell_command",
          "input": {
            "command": "echo hello"
          }
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "tool_result",
          "id": "toolu_123",
          "name": "execute_shell_command",
          "output": "hello"
        }
      ]
    }
  ],
  "conversation_snapshot_meta": {
    "included_messages": 3,
    "omitted_messages": 0,
    "limit": 20,
    "reasoning_omitted": true,
    "media_content_omitted": false
  }
}
```

这里要区分两层语义：

- `tool_response` 是“这一次工具调用最终产出的业务结果”，方便直接做审计、摘要或规则判断。
- `conversation_snapshot` 是“最近对话上下文的裁剪视图”，方便策略服务理解这次工具调用前后的语境。

不要把两者混用。最稳妥的做法是：规则判断优先读 `tool_response`，需要补上下文时再读快照。

例如 `execute_shell_command` 的工具输入字段是：

```json
{
  "tool_name": "execute_shell_command",
  "tool_input": {
    "command": "echo hello"
  }
}
```

不是 `cmd`。

## handler 能返回什么

这一节最容易误用。不同事件、不同 handler 类型，可用返回值并不完全相同。

### 1. 请求用户审批：`permissionDecision: "ask"`

典型用法：

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "ask",
    "permissionDecisionReason": "该命令会修改文件，请确认"
  }
}
```

实际效果：

- 只有在 `PreToolUse` 上，当前系统才会把 `ask` 接到现有审批流程里
- 用户同意后，原工具调用会再次经过一次 `PreToolUse`
- 批准只可复用一次，而且必须仍是同一工具调用 ID、工具名和工具输入
- 重放时，只有本次仍返回 `ask`、且其 handler ID 都包含在已批准集合内的请求才会通过；新增的 `ask` handler 或改动后的输入都会再次弹审批

建议：

- 需要跨轮次记住已批准状态时，再配合 `once: true`
- 或者在外部策略服务里记录“该操作已经审批过”

还要注意一点：

- hook 返回 `allow` 并不会绕过系统原有的 Tool Guard
- 如果 Tool Guard 自己也要求审批，审批仍然会发生

### 2. 允许 / 拒绝工具执行

对于 `command` 和 `http` handler，真正用于工具许可控制的推荐写法是：

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "allow",
    "permissionDecisionReason": "符合策略"
  }
}
```

或：

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "deny",
    "permissionDecisionReason": "命令涉及危险路径"
  }
}
```

说明：

- `allow` / `deny` / `ask` 属于“权限型结果”
- 最适合放在 `PreToolUse`
- `deny` 会阻止当前工具执行
- 对普通 `command` / `http` handler，不建议把顶层 `decision: "allow"` 当作“放行工具”的写法；应优先使用 `permissionDecision`

### 3. 阻断当前事件：`decision: "block"`

配置示例：

```json
{
  "decision": "block",
  "reason": "blocked by tenant policy"
}
```

但要注意，不同事件上的实际效果不同：

- `SessionStart` / `UserPromptSubmit`
  会直接阻断本次请求，Agent 不再继续。
- `PreToolUse`
  会阻断当前工具执行。
- `PostToolUse`
  不会撤销已经执行完的工具，也不会仅凭 `block` 自动结束当前回合；可用 `additionalContext` 补充后续推理信息。
- `PostToolUseFailure`
  不会吞掉原始工具失败；原错误仍然会继续向上抛出。
- `Stop`
  表示“现在还不能结束，请继续当前任务”。
- `Stop`
  handler 仍会执行，但 `block`、`deny`、`stop` 和其他全部输出都不会改变当前轮。

### 4. 终止当前回合：`continue: false` 或 `decision: "stop"`

配置示例：

```json
{
  "continue": false,
  "stopReason": "stop requested by hook"
}
```

或使用等价的规范写法：

```json
{
  "decision": "stop",
  "reason": "stop requested by hook"
}
```

这两个写法都适合明确要求“当前回合就到这里”。它与 `block` 的区别在于：

- `block` 更偏向“此处不允许继续，需要转到别的处理”
- `continue: false` / `decision: "stop"` 会设置回合级终止状态

注意：

- 在 `PreToolUse` 命中时，当前工具不会执行；运行时会写入 `hook_stopped` 工具结果，并取消同一回合中仍未完成的并行工具调用。
- 在 `PostToolUse` 命中时，已完成的成功工具结果会保留，然后结束当前回合并取消等待中的并行工具调用。
- 在 `PostToolUseFailure` 命中时，hook 仍会先拿到原始失败信息；终止决策会以回合终止取代正常的失败继续路径。
- `SessionStart` / `UserPromptSubmit` 同样可以使用它结束当前流程。
- `Stop` 不支持这个字段；只能用 `allow` 或 `block`

### 5. 补充上下文：`additionalContext`

配置示例：

```json
{
  "hookSpecificOutput": {
    "additionalContext": "策略系统确认：该目录属于当前项目根目录"
  }
}
```

它的生效方式取决于事件：

- `SessionStart` / `UserPromptSubmit`
  会被追加到本轮 Agent 的初始上下文中
- `PostToolUse` / `PostToolUseFailure`
  会作为系统说明写入内存，供后续推理或下一轮继续使用
- `Stop` 不支持 `additionalContext`；需要真实 assistant 消息进入 memory 时使用
  `outputAppend: true` 和 `appendText`，见 [Stop 附加消息](#stop-附加消息)

实务上最常见的用法：

- 在 `UserPromptSubmit` 注入组织规则
- 在 `PostToolUse` 写入审计说明或结果摘要
- 在 `PostToolUseFailure` 写入排查提示

### 6. 改写工具输入：`updatedInput`

只有 `PreToolUse` 适合使用 `updatedInput`。

示例：

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "allow",
    "updatedInput": {
      "command": "echo replaced-by-hook"
    }
  }
}
```

注意：

- `updatedInput` 会替换整个工具输入对象，不是局部 merge
- 同一个事件中只允许一个 handler 返回 `updatedInput`
- 如果多个 handler 同时返回 `updatedInput`，系统会直接阻断，避免结果不确定
- 只要任一 handler 返回回合终止决策，运行时会丢弃全部 `updatedInput`，不会在停止前再执行改写后的工具调用

### 7. 设置会话标题：`sessionTitle`

典型用法：

```json
{
  "hookSpecificOutput": {
    "sessionTitle": "Hook Demo Session"
  }
}
```

当前实际生效点是 `UserPromptSubmit`。

如果多个 handler 都返回了标题，系统只取第一个非空标题。

### 8. `systemMessage` 和 `suppressOutput`

这两个字段当前模型层会解析、结果合并层也会保留：

```json
{
  "systemMessage": "internal note",
  "suppressOutput": true
}
```

但要特别注意：

- 当前运行时还没有把它们接到用户可见流程里
- 也就是说，写了不代表当前前端或 Agent 主流程就会出现稳定可见效果

因此，在当前版本里应把它们视为“内部保留字段”，不要把业务能力建立在它们上面。

## 多个 hook 同时命中时，系统怎么处理

### 执行顺序

系统会先解析出命中的 handler，再统一并发执行。

配置来源顺序是：

1. 租户级
2. Agent 级
3. 当前会话里已加载的 Skill 级

但“执行”是并发的，“结果合并”按配置顺序。

这带来两个实际建议：

- 不要依赖 handler 之间的先后副作用
- 如果两个 handler 可能返回冲突结果，必须提前设计好职责边界

### 决策优先级

多个结果合并时，优先级大致如下：

```text
continue:false / decision:stop > block/deny > ask > allow > none
```

这套通用合并规则不适用于 `Stop`：普通校验合并 allow/block 完成决策；输出转换和追加各自
使用独立阶段及严格协议，追加失败不参与正文完成判定。

另外还有几条固定规则：

- `additionalContext` 按配置顺序收集
- `sessionTitle` 取第一个非空值
- `updatedInput` 只允许一个来源
- 任一终止决策都会胜过其他结果，并丢弃 `updatedInput`
- handler 自己执行失败时，按各自的 `failPolicy` 决定是否阻断

## Skill hook 的特殊规则

Skill hook 比普通租户级 / Agent 级 hook 更严格。

### 什么时候才会生效

Skill hook 不是一开始就全量加载，而是：

- 当前会话里某个 Skill 被激活后
- 该 Skill 自己的 `hooks/hooks.json` 才会被加载进本会话

加载后，它会继续在本会话里生效。

这意味着：

- 如果某次工具调用本身触发了一个 Skill 激活
- 那么这个 Skill 的后续事件，甚至同一工具调用的 `PostToolUse`，就可能已经命中 Skill hook

### Skill `command` handler 的额外限制

Skill 自带 `command` handler 必须满足：

- 必须使用 `argv`
- 不能使用 `command` 字符串
- 必须且只能有一个脚本路径参数
- 该脚本必须位于 Skill 自己的 `scripts/` 目录下
- 不能写字面量 `env`

也就是说，Skill 级 hook 更适合做“随 Skill 一起分发的小脚本策略”，而不是任意命令执行。

### Skill `http` handler 的额外限制

Skill 自带 `http` handler：

- 不允许写字面量 `headers`
- 不允许写 `allowedEnvVars`
- 可以使用 `headerSecretRefs`

当前实现里，Skill 自带 HTTP hook 默认允许加载；旧版文档里提到的“先配置 URL 白名单再允许 Skill HTTP hook”已经不是当前默认行为。

当前配置模型和 runner 里仍保留 `security.skill_hook_http.approved_urls` 读取逻辑，但默认加载路径不会把这个列表当作强制 allowlist 使用；只有内部调用者显式传入 callable 校验器时，loader 才会按校验器拒绝未批准 URL。

这也是当前实现里最需要谨慎处理的边界：

- HTTP handler 收到的是完整 HookContext 请求体，当前不会像 `prompt` handler 一样先做敏感字段脱敏。
- 如果 Skill 来源不可完全信任，Skill 自带 HTTP hook 可以把用户输入、工具输入、工具输出、候选回复和租户 / workspace 元数据发送到远端。
- `headerSecretRefs` 会从当前生效租户读取密钥并写入请求 Header，等同于允许该 hook 使用对应凭据访问远端服务。

修复或加固建议：

- 生产环境优先恢复 URL 白名单校验，至少对 Skill 自带 HTTP hook 执行域名 allowlist。
- 对 HTTP handler 的请求体增加与 `prompt` handler 一致的敏感字段脱敏，或提供显式 `redactPayload: true` / `sendFields` 白名单。
- 对 Skill `headerSecretRefs` 增加可引用密钥白名单，避免任意 Skill 读取租户级敏感 Header。
- 对外发 HTTP hook 增加审计日志，记录 handler ID、目标 URL、事件名和脱敏后的字段摘要。

## `Stop` 完成门禁

如果你只准备做一个高级 hook，通常就是它。

### 它解决什么问题

典型用途：

- 代码修改后，要求先跑目标测试
- 发布前，要求先完成 build 或 lint
- 文档生成类任务，要求先自检输出是否齐全

### 它和普通阻断最大的区别

`Stop` 返回 `block` 时，不是简单地“报错结束”，而是：

1. 给当前任务生成一条内部续跑指令
2. 让 Agent 在同一次请求里继续做事
3. 再次生成候选回复后，再次进入 `Stop`

如果持续返回 `block`，系统会用预算保护当前请求，避免无限循环。

### 预算配置

预算配置属于 Agent 运行配置，通常写在当前 workspace 的 `agent.json` 里。

配置位置：

```json
{
  "running": {
    "hook_runtime": {
      "max_stop_turns": 2,
      "max_automatic_follow_up_turns": 4
    }
  }
}
```

含义：

- `max_stop_turns`
  `Stop` 触发自动续跑的最大次数
- `max_automatic_follow_up_turns`
  自动续跑总预算；如果系统里还有别的自动续跑机制，会共享这个总预算

兼容字段仍然可读：

- `running.max_stop_turns`
- `running.max_automatic_follow_up_turns`

但如果同时配置了 `running.hook_runtime`，后者优先。

### 预算耗尽时会发生什么

系统不会无限循环。

预算耗尽后，会向用户明确输出一条“任务未完成”的消息，并带上最新阻断原因。

## 常见配置示例

下面的示例按运行生命周期排序。新人如果想建立完整心智模型，直接从上往下读最顺手。

### 示例 1：会话开始时注入启动约束

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "SessionStart": [
        {
          "id": "session-start-bootstrap",
          "hooks": [
            {
              "id": "bootstrap-context",
              "type": "http",
              "url": "https://policy.example.com/hooks/session-start",
              "timeout": 5,
              "failPolicy": "allow"
            }
          ]
        }
      ]
    }
  }
}
```

适合做：

- 注入本轮组织约束
- 追加 workspace 说明
- 对启动来源做额外审计

### 示例 2：用户输入进入前注入项目约束

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "UserPromptSubmit": [
        {
          "id": "prompt-context",
          "hooks": [
            {
              "id": "append-project-rules",
              "type": "http",
              "url": "https://policy.example.com/hooks/prompt",
              "timeout": 5,
              "failPolicy": "allow"
            }
          ]
        }
      ]
    }
  }
}
```

适合做：

- 补充组织规则
- 自动命名会话
- 对用户输入做预检查

### 示例 3：工具执行前检查 Shell 命令

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "PreToolUse": [
        {
          "id": "shell-policy",
          "matcher": {
            "tools": ["execute_shell_command"]
          },
          "hooks": [
            {
              "id": "check-shell",
              "type": "command",
              "argv": ["python", "hooks/check_shell.py"],
              "timeout": 5,
              "statusMessage": "正在检查命令策略",
              "failPolicy": "block"
            }
          ]
        }
      ]
    }
  }
}
```

适合做：

- 危险命令拦截
- 人工审批
- 参数标准化

### 示例 4：工具成功后写入审计摘要

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "PostToolUse": [
        {
          "id": "tool-audit-summary",
          "matcher": {
            "tools": ["execute_shell_command"]
          },
          "hooks": [
            {
              "id": "collect-success-summary",
              "type": "http",
              "url": "https://policy.example.com/hooks/post-tool",
              "timeout": 5,
              "failPolicy": "allow"
            }
          ]
        }
      ]
    }
  }
}
```

适合做：

- 记录工具执行结果摘要
- 把长输出压缩成后续推理可读的补充上下文
- 追加成功后的审计说明

### 示例 5：工具失败后补充诊断信息

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "PostToolUseFailure": [
        {
          "id": "failure-diagnostics",
          "hooks": [
            {
              "id": "collect-diagnostics",
              "type": "command",
              "argv": ["python", "hooks/collect_diagnostics.py"],
              "timeout": 10,
              "failPolicy": "allow"
            }
          ]
        }
      ]
    }
  }
}
```

这类 hook 的重点不是“挽回失败”，而是让后续推理更容易知道：

- 日志在哪
- 常见原因是什么
- 下一步该查什么

### 示例 6：停止前要求先完成测试

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "Stop": [
        {
          "id": "completion-gate",
          "hooks": [
            {
              "id": "task-completion-check",
              "type": "prompt",
              "prompt": "如果候选回复没有说明已完成必要测试，返回 block，并明确指出还缺什么；如果检查已完成，返回 allow。",
              "timeout": 8,
              "failPolicy": "block"
            }
          ]
        }
      ]
    }
  }
}
```

### 示例 7：在完成门禁中发送审计/埋点

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "Stop": [
        {
          "id": "final-stop-summary",
          "hooks": [
            {
              "id": "append-final-summary",
              "type": "command",
              "argv": ["python", "hooks/final_stop_summary.py"],
              "timeout": 5,
              "failPolicy": "allow"
            }
          ]
        }
      ]
    }
  }
}
```

适合做：

- 向日志、指标或审计系统记录当前候选回复
- 触发不影响会话的外部收尾动作

不适合做：

- 返回 `additionalContext` 写入记忆
- 返回 `deny`、`stop` 或 `continue: false`

需要候选回复继续时，返回 `block`；记录完成并允许结束时，返回 `allow`。

## 验证方式

建议不要一上来就配很复杂的策略，而是按下面顺序逐步验证。

### 验证 1：确认 hook 已命中

先在会产生效果的事件上用一个最简单的 handler，返回固定 `additionalContext` 或固定 `block`，确认：

- 事件名写对了
- 文件路径写对了
- `enabled` 已打开

验证 `Stop` 时不要只观察 stdout；还应检查 handler 写入的外部审计、日志或指标，以及 `allow` / `block` 对完成状态的影响。

### 验证 2：确认工具名和字段名

如果你在 `PreToolUse` 上做策略，务必确认两件事：

- `matcher.tools` 里的工具名是否真实存在
- `tool_input` 里用的字段名是否和真实工具一致

例如 `execute_shell_command` 要看的是：

```json
{
  "command": "echo hello"
}
```

### 验证 3：确认审批是否会重复

如果使用 `ask`，要额外验证：

- 用户批准后是否会再次弹审批
- 改写输入后是否会重新触发审批
- 新增或替换 `ask` handler 后是否会重新触发审批

这一步可以确认批准不会意外覆盖新的策略；只有需要跨轮次记住批准状态时，才考虑 `once: true`。

## 常见问题

### 配置后完全没有生效

按顺序检查：

1. 配置是否写在实际使用的租户或 workspace 下
2. `enabled` 是否为 `true`
3. 事件名是否正确
4. `matcher.tools` 是否与真实工具名完全一致
5. `if` 表达式是否写错
6. `command` / `argv` 路径是否越出当前 workspace
7. 如果是 Skill hook，该 Skill 是否真的已经在当前会话里被激活

### `ask` 没弹审批

先看你是不是配在了 `PreToolUse` 上。

当前审批 UI 只对 `PreToolUse` 的 `permissionDecision: "ask"` 做接线。其他事件即使返回 `ask`，也不会走同样的人工审批流程。

### 批准后又重复审批

这是 `PreToolUse` 的预期保护行为。原因通常有三个：

1. 本次 `ask` 来自未被原批准覆盖的新 handler
2. 批准后工具输入发生了变化，系统把它视为新的待审操作
3. 本次不是同一个工具调用的单次批准重放

处理建议：

- 检查返回 `ask` 的 handler ID 与工具输入是否保持不变
- 仅在确实希望跨轮次跳过审批时，再加 `once: true` 或在外部策略里记录已审批状态

### `PostToolUse` 返回了 `block`，为什么工具还是执行了

这是当前设计使然。

`PostToolUse` 发生在工具成功返回之后，所以它不能回滚已经执行完的操作。它更适合：

- 补充审计信息
- 追加结果摘要
- 告诉后续推理“这一步虽然执行了，但有风险”

当前所有经 Tool Guard 执行的成功路径都会触发 `PostToolUse`，包括预批准调用和受保护的 source-built-in tool；因此可以把它作为统一的成功后审计点。它仍不能回滚工具的外部副作用。

### `PostToolUseFailure` 返回了 `block`，为什么原错误还在

因为这个事件的职责是“补充失败诊断”，不是“吞掉原失败”。

当前实现里，工具失败后：

1. `PostToolUseFailure` 会运行
2. hook 可以写入诊断信息
3. 原始工具失败仍然会继续向上抛出

预批准调用和受保护的 source-built-in tool 同样在该失败路径内，会触发 `PostToolUseFailure`。如果需要在失败后直接结束回合，应返回 `continue: false` 或 `decision: "stop"`；普通 `block` 则仍保留原始工具失败。

### command hook 报路径越界

普通 hook 请确保脚本和工作目录都在当前 workspace 内。

推荐写法：

```json
{
  "argv": ["python", "hooks/check_shell.py"]
}
```

Skill hook 则应改为放在 Skill 自己的 `scripts/` 目录里。

### handler 输出 JSON 解析失败

当 handler 以成功状态返回时：

- stdout 为空可以
- stdout 非空时必须是合法 JSON 对象

因此：

- 日志写 stderr
- 最终结果只写一份 JSON 到 stdout

### prompt hook 误判很多

优先缩小问题范围：

1. 先把 `prompt` 规则写得非常具体
2. 只针对单一事件启用
3. 配合 `matcher.tools` 或简单 `if` 限定命中范围
4. 观察 `reason` 是否能清楚解释判断依据

## 配置建议

- 安全策略优先用 `failPolicy: "block"`
- 审计、诊断、日志类 hook 优先用 `failPolicy: "allow"`
- 需要人工审批的 `PreToolUse` 尽量配合 `once: true`
- 能用 `matcher.tools` 缩小范围时，不要让所有工具都命中
- 不要依赖多个 handler 之间的顺序副作用，因为它们会并发执行
- `Stop` 规则要尽量具体，否则容易把正常任务拖进反复续跑
- 不要把密钥明文写进配置；HTTP 认证优先用 `headerSecretRefs`
