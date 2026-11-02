# Stop 附加消息：特性总结

实现日期：2026-10-20。合并提交：`f117c49ab`。

Stop 附加消息允许 Hook 在最终正文获准完成后，生成独立的文本/Markdown 消息。
它保留正文流式输出，适合补充参考链接、报告入口或业务提示；不是正文替换，也不是新一轮问答。
首期支持 Console，覆盖普通问答和 Goal 正式交付；其他 Channel 不执行追加处理器。

## 与现有 Stop 能力的区别

| 模式 | 配置与返回 | 执行时机 | 对正文的影响 |
| --- | --- | --- | --- |
| 普通完成校验 | `allow` / `block` | 候选正文生成后 | 决定是否完成；显式 block 可触发有上限的续跑 |
| 正文转换 | `outputTransform: true`，返回 `replacementText` | 普通校验之前 | 暂存并完整替换正文，多个转换器串行执行 |
| 附加消息 | `outputAppend: true`，返回 `appendText` | 最终校验放行后 | 不改写正文，每个处理器最多追加一个独立气泡 |

仅配置 `outputAppend` **不会关闭正文流式输出**。如果还配置了潜在匹配的输出转换器，
仍遵守转换器原有的正文暂存规则。普通 Stop 处理器的 stdout 不会自动成为聊天内容，
必须显式开启追加模式并返回对应协议。

## 运行时序与消息归属

```text
正文流式输出（如有转换器，沿用其暂存规则）
  → 可选正文转换 → 普通 Stop 校验
      block：沿用既有续跑/未完成路径，不执行追加
      allow：交付最终正文
  → 并发执行追加处理器，等待全部结束或达到预算
  → 按固定顺序逐条持久化，再发送独立消息
  → 结束本轮
```

追加不是请求结束后的后台任务，用户仍可在追加阶段停止本轮。
Goal 仅在正式 Finalization 放行后追加，不在执行过程或被否决的完成尝试中追加。
空正文、计划交互边界、工具终止、Goal 固定 fallback、hooks 关闭等跳过 Stop 的路径也不追加。

每条附加消息都是原 Answer Turn 内的真实 `assistant` 消息，进入 Agent memory、
会话历史、分享和后续模型上下文；不是仅在前端显示的临时记录。
它遵守普通上下文预算和压缩规则，不保证永久保留在模型输入中。
附加内容不会再次触发 Stop、正文转换、Goal 评审或模型续跑，业务内容由处理器负责。

## 配置与统一返回协议

沿用租户、Agent Profile、已激活 Skill 三种配置来源。下面是 Skill 的
`hooks/hooks.json` 示例；租户和 Agent 配置需再包一层根字段 `hooks`。
脚本使用路径相对于对应 Skill，放在其 `scripts/` 目录内。

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

`outputAppend` 默认 false，只能用于 Stop，不能与同一处理器的
`outputTransform: true` 或 `once: true` 同用；不同处理器可以分别承担转换、校验和追加。
每个最终放行轮次最多执行一次追加阶段，后续问答轮次仍可执行。

输入沿用 HookContext，`assistant_response` 是已放行的最终正文。
`command` 从 stdin 读取 JSON，退出 0 并向 stdout 写出以下 JSON；
`http` 返回 2xx JSON；`prompt` 返回同一结构化对象。
使用 prompt 会额外调用模型，增加请求、耗时及相应费用。

```json
{
  "decision": "allow",
  "reason": "reference links collected",
  "hookSpecificOutput": {
    "appendText": "### 参考链接\n\n[规范](https://example.org/spec)"
  }
}
```

- 顶层仅接受 `decision`、`reason`、`hookSpecificOutput`；decision 只能为 allow。
- reason 必须是非空诊断说明，最多 2,000 字符，不作为聊天内容展示。
- `hookSpecificOutput` 仅接受 `appendText`；省略该字段或返回空对象表示成功但不追加。
- 提供 `appendText` 时必须是含非空白内容的字符串，原文保留；不接受角色、任意 metadata、
  `replacementText`、权限或上下文注入字段。

## 并发、顺序与资源限制

所有追加处理器并发读取同一份已放行正文；请求会话快照时读取同一份追加前快照，
仍遵守各自的快照上限和脱敏规则，不读取其他处理器的追加结果。
追加计划遵守现有来源、matcher、overlay、`if` 规则，在阶段入口刷新激活 Skill 后冻结。

气泡展示和累计配额按固定顺序处理，与执行完成先后无关：
**租户 → Agent Profile → 已激活 Skill（按 skill_name 排序）→ 各来源内的分组及处理器声明顺序**。
不同来源的同名处理器保持独立。等待耗时主要取决于最慢处理器，并受阶段预算限制。

| 限制 | 默认值或上限 | 超限处理 |
| --- | --- | --- |
| 单处理器超时 | `timeout` 默认 10 秒 | 跳过该处理器 |
| 阶段生成/收集预算 | 默认 30 秒 | 取消未完成任务，保留截止前成功结果 |
| 单条正文 | 16,384 UTF-8 字节 | 跳过，不截断 |
| 本轮累计正文 | 65,536 UTF-8 字节 | 按固定顺序分配；放不下的跳过，后续较小结果仍可使用余额 |
| 原始 stdout / HTTP 解码响应 / prompt 响应 | 各 256 KiB | 读取时限制，视为处理器失败 |
| command stderr | 64 KiB | 读取时限制，视为处理器失败 |

阶段预算由 Agent Profile 的 `running.hook_runtime.max_stop_append_seconds` 配置，
必须为正有限数，租户和 Skill 不能提高它。该预算不替代现有会话持久化超时。

## 失败、停止与恢复

- 处理器执行失败、超时、协议非法或超限时，跳过该项并继续其他结果。
  有效失败策略固定为 allow，即使保存的 `failPolicy` 是 block，也不能否决正文成功状态。
  不触发模型重试，不在聊天中插入错误气泡；失败进入诊断日志和 Hook 人工测试结果。
- 每条消息先持久化，再发送；成功提交即视为已交付。Console 使用稳定消息身份
  保持独立气泡、固定顺序和回放去重，刷新后仍可恢复。
- 用户停止会取消剩余追加工作，保留已提交消息，丢弃未提交结果，不再补发。
  已发生的脚本外部副作用不承诺回滚，远端操作和已脱离控制的进程不承诺撤销。
- 单纯断线不等于停止，后台继续；重连恢复已保存消息，不重新执行追加阶段。
  进程崩溃后不补跑脚本，未提交结果允许丢失，不提供外部副作用的 exactly-once 保证。
- 存储失败不属于可跳过的处理器失败：不得发送未保存消息，撤销未提交的 memory 变更，
  停止后续追加并进入现有持久化恢复路径，不重跑模型或脚本。

## 管理、调试与验收

Hook 管理页支持为 command/http/prompt 开启“追加独立消息”，并显示固定有效失败策略。
人工测试需提供非空 `assistant_response`，会真实运行一个处理器，但不写真实 Chat、
Agent memory 或草稿配置；外部副作用仍会发生。
结果仅展示状态、是否追加、UTF-8 字节数、耗时和有效策略等脱敏统计，
不回显追加正文、reason 或原始输出。单项测试不验证多来源顺序、累计预算和恢复行为。

合并时的验证记录：651 项 Python 回归、145 项 Console 测试通过，前端类型检查及构建通过。
覆盖真实脚本子进程、普通/Goal 交付、持久化先于发送、停止竞争、历史恢复、后续模型输入和分享。
浏览器 QA 使用模拟 API 数据，不等同于已部署后端的端到端验证。

相关入口：

- [可运行脚本示例与本地调试](stop-output-append-command-demo/SKILL.md)
- [完整运行时契约](hook-runtime.md#stop-附加消息)
- [扩展设计与全部已确认决策](../../docs/superpowers/specs/2026-10-20-stop-appended-message-design.md)
- [实施计划与验证记录](../../docs/superpowers/plans/2026-10-20-stop-appended-message.md)
- [架构决策 ADR 0037](../../docs/adr/0037-stop-appends-run-after-approval.md)
