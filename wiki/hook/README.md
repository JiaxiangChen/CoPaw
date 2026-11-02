# Hook 样例索引

本文档汇总 `wiki/hook/hook-runtime.md` 对应的可运行样例。面向客户交付时，建议先看
[customer-hook-demo-guide.md](customer-hook-demo-guide.md)，按客户目标挑选 1 到 3 个示例。

特性总结：[Stop 附加消息](stop-appended-message.md)——最终放行后追加独立文本/Markdown
气泡，保留正文流式输出，并进入历史和后续模型上下文。

除租户级示例外，样例都只放在 `wiki/hook/` 下，目录结构统一遵循 skill 级 hook 的真实布局：

```text
<demo>/
├── SKILL.md
├── hooks/
│   └── hooks.json
└── scripts/
    └── ...
```

这些样例都按当前实现校对过，重点覆盖两件事：

1. 7 个 hook 事件类型全部有例子
2. 3 类 handler 类型 `command` / `http` / `prompt` 全部有例子

## 覆盖矩阵

| 事件 | 推荐样例 | handler 类型 | 说明 |
| --- | --- | --- | --- |
| `SessionStart` | [session-start-prompt-demo](session-start-prompt-demo/SKILL.md) | `prompt` | 演示会话启动前的入口策略判断 |
| `UserPromptSubmit` | [user-prompt-submit-command-demo](user-prompt-submit-command-demo/SKILL.md) | `command` | 演示用户输入预检查、补充上下文与会话标题 |
| `PreToolUse` | [pre-tool-use-command-demo](pre-tool-use-command-demo/SKILL.md) | `command` | 演示工具执行前的 deny / ask / updatedInput |
| `PreToolUse` | [tenant-pre-tool-use-http-policy-demo](tenant-pre-tool-use-http-policy-demo/README.md) | `http` | 演示租户级远端策略服务做 allow / deny / ask / updatedInput |
| `PreToolUse` | [conditional-pre-tool-use-demo](conditional-pre-tool-use-demo/SKILL.md) | `command` | 演示 `if` 条件表达式，只在命中特定工具输入时执行 handler |
| `PostToolUse` | [hook-http-demo](hook-http-demo/SKILL.md) | `http` | 演示成功工具结果 `tool_response` 通过 HTTP 发送到本地接收器，并可选附带会话快照 |
| `PostToolUse` | [snapshot-post-tool-audit-demo](snapshot-post-tool-audit-demo/SKILL.md) | `command` | 演示 handler 级 `includeConversationSnapshot` 和审计摘要 |
| `PostToolUseFailure` | [mcp-failure-fallback-demo](mcp-failure-fallback-demo/SKILL.md) | `command` | 演示失败后注入统一兜底上下文 |
| `PostToolUse` / `PostToolUseFailure` | [http-auth-failure-guard-demo](http-auth-failure-guard-demo/SKILL.md) | `command` | 演示工具返回 HTTP 401/403 后阻断继续推进并注入失败上下文 |
| `Stop` | [stop-prompt-demo](stop-prompt-demo/SKILL.md) | `prompt` | 演示结束前 gate，只允许 `allow` / `block` |
| `Stop` | [final-output-prompt-guard-demo](final-output-prompt-guard-demo/SKILL.md) | `prompt` | 演示通过提示词审查 Agent 最终输出规范，不符合则 `block`，符合则 `allow` |
| `Stop` | [stop-history-http-guard-demo](stop-history-http-guard-demo/SKILL.md) | `command` | 演示读取完整会话历史并调用外部接口判定最终输出 |
| `Stop` | [stop-command-summary-demo](stop-command-summary-demo/SKILL.md) | `command` | 演示结束时发送审计/埋点并显式批准完成 |
| `Stop` | [stop-output-transform-command-demo](stop-output-transform-command-demo/SKILL.md) | `command` | 演示以 `outputTransform: true` 串行改写最终文本，并用 `replacementText` 完整替换候选回复 |
| `Stop` | [stop-output-append-command-demo](stop-output-append-command-demo/SKILL.md) | `command` | 最终放行后用 `outputAppend: true` 追加一条参考链接消息，正文继续流式输出 |

## Handler 覆盖

| handler 类型 | 对应样例 | 关键点 |
| --- | --- | --- |
| `command` | `user-prompt-submit-command-demo`、`pre-tool-use-command-demo`、`conditional-pre-tool-use-demo`、`snapshot-post-tool-audit-demo`、`mcp-failure-fallback-demo`、`http-auth-failure-guard-demo`、`stop-history-http-guard-demo`、`stop-command-summary-demo`、`stop-output-transform-command-demo`、`stop-output-append-command-demo` | skill 级必须使用 `argv`，脚本必须放在 `scripts/` 下 |
| `http` | `hook-http-demo`、`tenant-pre-tool-use-http-policy-demo` | skill 级不能写明文 `headers` 与 `allowedEnvVars`；租户级可用 `headers` / `headerSecretRefs` 接远端策略服务 |
| `prompt` | `session-start-prompt-demo`、`stop-prompt-demo`、`final-output-prompt-guard-demo` | 普通事件支持 `allow` / `deny` / `block` / `stop`；普通 Stop 校验仅 allow/block，转换/追加模式仅 allow |

## 使用方式

1. 先阅读 [hook-runtime.md](hook-runtime.md)，确认事件时机与返回语义。
2. 如果是给客户交付，先阅读 [customer-hook-demo-guide.md](customer-hook-demo-guide.md)，按客户目标拆分示例。
3. 再挑一个最接近你场景的 demo，直接复用它的目录布局和字段写法。
4. 如果要把样例迁移到真实 skill，重点检查：
   - `hooks/hooks.json` 的事件名和 `matcher.tools`
   - `scripts/` 脚本路径是否仍在 skill 根目录内
   - `http` handler 是否误写了 skill 级不允许的字段
   - 只有确实需要上下文时才打开 `includeConversationSnapshot`
   - 普通 `Stop` 校验是否只返回 `allow` / `block`；输出转换和追加仅允许 `allow`
   - `Stop` 是否在记录副作用后明确返回 `allow` 或 `block`
   - `outputTransform: true` 是否只写在 `Stop`、没有同时设置 `once: true`，并且只返回 `allow` 与可选的非空 `replacementText`
   - `outputAppend: true` 是否仅用于 Stop、与 `outputTransform` / `once` 互斥，且仅返回 allow/reason 和可选非空 appendText

## 额外说明

- `hook-http-demo` 保留了原有目录名，但它覆盖的事件就是 `PostToolUse`。如果你想同时观察“当前工具结果”和“最近对话上下文”，优先看这个 demo，再按需给 handler 打开 `includeConversationSnapshot`。
- `PostToolUse` / `PostToolUseFailure` 现在覆盖普通调用、Tool Guard 预批准调用和受保护的 source-built-in tool。它们不能回滚外部副作用；若要在工具事件后直接结束当前回合，返回 `continue: false` 或 `decision: "stop"`，不要把普通 `block` 当作终止信号。
- `tenant-pre-tool-use-http-policy-demo` 是租户级配置示例，不是 skill 级目录；它演示
  `~/.swe/<tenant_id>/config.json` 里需要多包一层根字段 `hooks`。
- `conditional-pre-tool-use-demo` 专门演示 `if` 表达式。真实配置里建议先从简单条件开始验证，再逐步增加复杂度。
- `snapshot-post-tool-audit-demo` 专门演示 handler 级 `includeConversationSnapshot`。它把
  `tool_response` 当作当前工具结果，把 `conversation_snapshot_meta` 当作快照说明，避免混用两者。
- `mcp-failure-fallback-demo` 除了主要展示 `PostToolUseFailure`，还额外附带了一个
  `Stop` prompt gate，方便一起观察“失败兜底 + 结束前一致性校验”的组合写法。
- `http-auth-failure-guard-demo` 面向接口鉴权失败：当指定工具在成功返回体或失败信息里
  暴露 HTTP `401` / `403` 时，返回 `block` 并注入“当前任务已经失败”的上下文。
- `final-output-prompt-guard-demo` 面向最终输出规范检查：在 `Stop` 阶段读取
  `assistant_response`，用提示词判断最终答复是否直接回应请求、是否有验证依据、是否说明限制。
- `stop-history-http-guard-demo` 面向完整历史审查：在 `Stop` 阶段读取
  `transcript_path` 指向的完整会话 JSON，再调用外部策略接口返回 `allow` / `block`。
- `stop-command-summary-demo` 面向完成尝试的外部观测：脚本把审计记录写给外部日志采集，并在 stdout 返回 `allow` 批准候选回复。
- `stop-output-transform-command-demo` 面向最终文本规范化：它在交付前把候选文本暂存，
  command handler 通过 `hookSpecificOutput.replacementText` 进行完整替换。若变换失败，
  `failPolicy: allow` 会保留当前文本继续，`failPolicy: block` 或总时限耗尽会让本轮未完成，
  不投递候选文本也不自动续跑。
- prompt 样例目录里的 `scripts/*.py` 不是 hook runtime 自动执行的 handler，而是用于
  生成最小 `HookContext` 样本，方便你手工调试 prompt 规则。
- `stop-output-append-command-demo` 将正文中已有的 Markdown 链接整理为独立消息；没有链接时
  成功但不追加。它在普通 Stop 最终放行后执行，真实进入 memory/历史和后续模型上下文。
  失败有效策略固定 allow，不触发模型重试或 Stop 递归；首期仅 Console 执行。
  预算、顺序、停止和恢复边界见 [Stop 附加消息](hook-runtime.md#stop-附加消息)。
