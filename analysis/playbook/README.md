# Swe Playbook

`analysis/playbook/` 用于沉淀重复问题的处理经验，重点记录报错样式、定位入口、日志入口和排查顺序。

## 文档索引

| 文档 | 摘要 |
|------|------|
| [ai-coding-session-retrospective.md](ai-coding-session-retrospective.md) | 近一周 AI Coding 会话复盘：返工原因、异步验收矩阵、测试选择与跨会话交接 |
| [agent-config-read-consistency.md](agent-config-read-consistency.md) | agent 控制面读写一致性边界、排查入口和与 console agent switching 的职责分界 |
| [agent-config-file-overwrite.md](agent-config-file-overwrite.md) | agent.json 手改后覆盖：回复整文件写回、模型规范化、引导自愈重建与并发丢失更新 |
| [chat-history-preload-stall.md](chat-history-preload-stall.md) | 聊天历史触顶不继续加载：240px 区域锁、本地与归档分页衔接、复现证据与验收标准 |
| [common-errors.md](common-errors.md) | 常见报错模式、典型触发点和第一落点 |
| [wealth-scene-monitoring.md](wealth-scene-monitoring.md) | 财富规划场景督办策略的接口字段、角色控制和回显验证 |
| [control-path-complexity.md](control-path-complexity.md) | 策略、审批、专家准入及 Stop append 的复杂度拆分边界、计数口径和回归入口 |
| [expert-binding-new-chat-identity.md](expert-binding-new-chat-identity.md) | 新草稿专家绑定的固定逻辑 ID、重复 ID 定位漂移、独立接口职责与复现边界 |
| [subagent-monitor-refresh.md](subagent-monitor-refresh.md) | SubAgent 会话展示、事件刷新、轮询中断与跨会话竞态的复现和验收条件 |
| [cron-execution-final-marker.md](cron-execution-final-marker.md) | 执行记录最终标记、历史兼容、重试替换与 MySQL 验证入口 |
| [database-pool-disposal.md](database-pool-disposal.md) | MySQL 故障连接强制关闭、健康空闲关闭、容量隔离及反复修复的测试盲区 |
| [event-loop-worker-boundaries.md](event-loop-worker-boundaries.md) | 五项事件循环热点的 worker 入口、取消与并发规则、回归及性能测量边界 |
| [managed-execution-model-identity.md](managed-execution-model-identity.md) | 新版批调度的任务 ID、默认模型解析与实际模型落库 |
| [external-job-list.md](external-job-list.md) | 外部 jobs 列表的无初始化接口、原接口契约及任务绑定的只读边界 |
| [hook-distribution-activation.md](hook-distribution-activation.md) | Hook 分发成功但目标未启用的根因、复现证据与事务内启用方案 |
| [location-paths.md](location-paths.md) | 按问题类型给出代码、配置与命令入口，包含财富任务动态指标与经营分组 |
| [log-entrypoints.md](log-entrypoints.md) | 运行日志、daemon logs、query error dump、Tracing 的实际入口 |
| [my-mcp-manual-validation.md](my-mcp-manual-validation.md) | MyMCP 在本地环境下按用户、来源、应用和管理员身份进行手工验证的方法 |
| [performance-optimization-plan.md](performance-optimization-plan.md) | freshness 扫描优化与官方 MySQL 异步驱动迁移的方案表、风险和验收标准 |
| [python-memory-hotspots.md](python-memory-hotspots.md) | Python 后端前五内存热点、引用持有链、运行时验证与第一阶段生命周期及临时缓存设计入口 |
| [python-installation-denial.md](python-installation-denial.md) | Shell pip 安装平台拒绝、语法识别边界、Hook 改写与前后台执行入口 |
| [skill-freshness-and-span-write-performance.md](skill-freshness-and-span-write-performance.md) | 技能递归 freshness 检查与 Tracing 批量写入的 CPU 成本、隔离基准及优化边界 |
| [skill-freshness-optimization-detail.md](skill-freshness-optimization-detail.md) | freshness 调用窗口、token 兼容性、锁优化、manifest 一致性及分阶段验收方案 |
| [stop-appended-message.md](stop-appended-message.md) | Stop 附加消息资格、人工测试脱敏、UTF-8 配额、停止/持久化和重连诊断入口 |
| [subagent-intent-and-expert-discovery.md](subagent-intent-and-expert-discovery.md) | 子代理意图、Source 专家发现、Chat 管理归属、依赖并发与 Goal 授权排查 |
| [tool-result-truncation.md](tool-result-truncation.md) | MCP / read_file 工具返回出现 `<<<TRUNCATED>>>` 时的截断点和临时调大配置 |
| [tracing-bounded-writer.md](tracing-bounded-writer.md) | Tracing 写入确认、背压、分块、关闭超时及取消回滚异常的排查边界 |
| [troubleshooting-order.md](troubleshooting-order.md) | 从复现、取证、收敛到验证的推荐顺序 |

## 使用建议

1. 先看 [troubleshooting-order.md](troubleshooting-order.md)，确定排查顺序。
2. 遇到明确错误消息时，再查 [common-errors.md](common-errors.md)。
3. 遇到 agent 控制面读写不一致时，先看 [agent-config-read-consistency.md](agent-config-read-consistency.md)。
4. 需要做 MyMCP 手工联调时，看 [my-mcp-manual-validation.md](my-mcp-manual-validation.md)。
5. 工具返回出现 `<<<TRUNCATED>>>` 时，看 [tool-result-truncation.md](tool-result-truncation.md)。
6. 需要找入口时，看 [location-paths.md](location-paths.md) 和 [log-entrypoints.md](log-entrypoints.md)。
