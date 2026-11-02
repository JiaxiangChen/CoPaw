# Hook 客户示例拆分指南

这份指南把 [hook-runtime.md](hook-runtime.md) 里的运行时能力，拆成客户更容易落地的示例包。选 demo 时先看“要解决什么业务问题”，再看事件和 handler 类型。

## 先按客户目标选 demo

| 客户目标 | 推荐事件 | 推荐 handler | 示例 |
| --- | --- | --- | --- |
| 用户输入进入 Agent 前做合规检查 | `UserPromptSubmit` | `command` 或 `http` | [user-prompt-submit-command-demo](user-prompt-submit-command-demo/SKILL.md) |
| 启动会话时注入或校验入口上下文 | `SessionStart` | `prompt` | [session-start-prompt-demo](session-start-prompt-demo/SKILL.md) |
| 工具调用前做安全审批、拒绝或参数改写 | `PreToolUse` | `command` | [pre-tool-use-command-demo](pre-tool-use-command-demo/SKILL.md) |
| 企业统一远端策略中心拦截高风险工具 | `PreToolUse` | `http` | [tenant-pre-tool-use-http-policy-demo](tenant-pre-tool-use-http-policy-demo/README.md) |
| 只在特定条件下触发 hook，减少误拦截 | `PreToolUse` | `command` + `if` | [conditional-pre-tool-use-demo](conditional-pre-tool-use-demo/SKILL.md) |
| 工具成功后做审计、摘要或结果压缩 | `PostToolUse` | `http` | [hook-http-demo](hook-http-demo/SKILL.md) |
| 工具成功后同时读取工具结果和最近对话 | `PostToolUse` | `command` + 快照 | [snapshot-post-tool-audit-demo](snapshot-post-tool-audit-demo/SKILL.md) |
| 工具失败后注入统一兜底话术 | `PostToolUseFailure` | `command` | [mcp-failure-fallback-demo](mcp-failure-fallback-demo/SKILL.md) |
| HTTP 401/403 后阻止继续基于失败结果推进 | `PostToolUse` / `PostToolUseFailure` | `command` | [http-auth-failure-guard-demo](http-auth-failure-guard-demo/SKILL.md) |
| 最终回复发出前做完成度门禁 | `Stop` | `prompt` | [stop-prompt-demo](stop-prompt-demo/SKILL.md) |
| 最终回复必须满足客户交付规范 | `Stop` | `prompt` | [final-output-prompt-guard-demo](final-output-prompt-guard-demo/SKILL.md) |
| 最终回复需要结合完整会话历史审查 | `Stop` | `command` + 外部 HTTP | [stop-history-http-guard-demo](stop-history-http-guard-demo/SKILL.md) |
| 控制 `Stop` 自动续跑次数 | Agent 运行配置 | `running.hook_runtime` | 见下方“完成门禁预算配置” |
| 当前轮结束时发送审计、埋点或外部通知 | `Stop` | `command` | [stop-command-summary-demo](stop-command-summary-demo/SKILL.md) |
| 保留正文流式输出，最终放行后追加独立消息 | `Stop + outputAppend` | `command` / `http` / `prompt` | [特性总结](stop-appended-message.md)、[脚本示例](stop-output-append-command-demo/SKILL.md) |

## 再按配置层级选放置位置

| 层级 | 适合客户场景 | 配置外形 | 示例 |
| --- | --- | --- | --- |
| 租户级 | 统一策略、统一审计、统一远端审批 | `~/.swe/<tenant_id>/config.json`，根节点包含 `hooks` | [tenant-pre-tool-use-http-policy-demo](tenant-pre-tool-use-http-policy-demo/README.md) |
| Agent 级 | 某个 workspace 或 Agent 单独策略 | `~/.swe/<tenant_id>/workspaces/<workspace_id>/agent.json`，根节点包含 `hooks` | 可复用租户级示例，把文件放到 Agent 配置 |
| Skill 级 | demo、插件自带策略、随 Skill 激活后才生效 | `<skill>/hooks/hooks.json`，根对象就是 hook 配置 | 本目录大部分 demo |

默认给客户交付时，建议把“组织统一策略”放租户级，把“演示或 Skill 自带能力”放 Skill 级。

## 常见返回值对应示例

| 返回目标 | 推荐字段 | 示例 |
| --- | --- | --- |
| 请求人工审批 | `hookSpecificOutput.permissionDecision = "ask"` | [pre-tool-use-command-demo](pre-tool-use-command-demo/SKILL.md) |
| 拒绝工具执行 | `hookSpecificOutput.permissionDecision = "deny"` | [pre-tool-use-command-demo](pre-tool-use-command-demo/SKILL.md) |
| 阻断当前请求或阶段 | `decision = "block"` | [user-prompt-submit-command-demo](user-prompt-submit-command-demo/SKILL.md)、[final-output-prompt-guard-demo](final-output-prompt-guard-demo/SKILL.md) |
| 改写工具输入 | `hookSpecificOutput.updatedInput` | [pre-tool-use-command-demo](pre-tool-use-command-demo/SKILL.md) |
| 注入上下文 | `hookSpecificOutput.additionalContext` | [hook-http-demo](hook-http-demo/SKILL.md)、[mcp-failure-fallback-demo](mcp-failure-fallback-demo/SKILL.md) |
| 设置会话标题 | `hookSpecificOutput.sessionTitle` | [user-prompt-submit-command-demo](user-prompt-submit-command-demo/SKILL.md) |
| 最终放行后追加独立文本/Markdown 消息 | `outputAppend: true` + `hookSpecificOutput.appendText` | [stop-output-append-command-demo](stop-output-append-command-demo/SKILL.md) |
| 终止当前回合 | `continue = false` 或 `decision = "stop"` | 使用 `SessionStart`、`UserPromptSubmit` 或工具事件的对应 demo；`Stop` 不参与控制 |

## 完成门禁预算配置

`Stop` 返回 `block` 后会让 Agent 在同一次请求里继续尝试完成任务。这个自动续跑需要单独配置预算，通常写在当前 workspace 的 `agent.json`：

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

这个配置不是 handler demo，所以不需要新建 `hooks/hooks.json`。给客户交付 `Stop` 类示例时，应同时说明这两个预算值，避免规则过严导致同一请求反复续跑。

## 交付客户时的推荐拆分

1. 先交付 [README.md](README.md) 和 [hook-runtime.md](hook-runtime.md)，作为完整说明。
2. 再按客户真实目标挑 1 到 3 个 demo，不要一次交付全部目录。
3. 如果客户已有统一策略服务，优先交付租户级 HTTP 示例；如果客户只是验证效果，优先交付 Skill 级 command 示例。
4. 每个 demo 交付前都把 `matcher.tools`、URL、认证方式、`failPolicy` 和超时时间改成客户自己的值。
5. 对阻断类策略，先在测试租户用 `failPolicy: "allow"` 观察命中情况，再切到 `block`。

## 不建议混用的点

- 不要把 `tool_response` 和 `conversation_snapshot` 混成一个概念；前者是当前工具结果，后者是最近对话裁剪。
- 不要在 `Stop` 返回 `additionalContext` 或 `continue: false`；需要真实 assistant 附加消息时，使用 `outputAppend: true` 和 `appendText`。
- 普通 `Stop` 校验只返回 `allow` / `block`。显式 `block` 可触发有上限的续跑；`failPolicy: block` 的 handler 失败则以未完成结束而不续跑。输出转换与追加模式仅接受 `allow`，分别返回 `replacementText` / `appendText`；追加失败固定尽力跳过，不否决正文。
- 不要把工具后置事件的普通 `block` 当成回合终止；要结束当前回合，使用 `continue: false` 或 `decision: "stop"`。终止会取消尚未完成的并行工具调用，但不会撤销已经发生的外部副作用。
- 不要在 Skill 级 `command` handler 使用 `command` 字符串；用 `argv`。
- 不要让多个 handler 同时返回 `updatedInput`；运行时会阻断以避免结果不确定。
- 不要依赖普通 handler 的执行顺序；它们通常并发执行。Stop 转换器按固定顺序串行执行；Stop 追加处理器并发执行，但按固定来源及配置顺序展示消息。
