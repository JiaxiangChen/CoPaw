# 定位路径

## NAS session lock release gate

在生产等价 StorageClass 上运行以下检查；`/mnt/sessions` 必须是所有 Runner Pod 共享的同一 NAS 挂载：

```bash
python3 scripts/verify_session_nas_lock.py /mnt/sessions
kubectl get pvc swe-sessions-nas -o jsonpath='{.status.phase}{" "}{.status.accessModes[*]}{"\n"}'
test "$(kubectl get pvc swe-sessions-nas -o jsonpath='{.status.phase}')" = "Bound"
kubectl get pvc swe-sessions-nas -o jsonpath='{.status.accessModes[*]}' | grep -qw ReadWriteMany
kubectl delete job swe-session-nas-lock-verification --ignore-not-found
kubectl apply -f deploy/session-nas-lock-verification-job.yaml
kubectl wait --for=condition=complete job/swe-session-nas-lock-verification --timeout=10m
```

检查必须同时证明：第二个 Pod 在持锁期间拿不到 `.verification.json.lock`、持锁进程被 SIGKILL 后可以接管、
第二个 Pod 并发读取原子 JSON 替换时始终可解析且 revision 连续递增。失败时禁止以无锁写入或 Redis 锁替代；
先保留旧版本。Job 镜像必须是包含 `scripts/verify_session_nas_lock.py` 的本次 release 镜像；示例默认使用
`agentscope/swe:latest`，生产环境应改为已验证的不可变 release tag。

通过后暂停 cron dispatch，排空现有 session 请求，将旧 Runner Pod 缩容到零，再部署新版本，确认所有新 Pod
使用同一绝对 session 路径后恢复 cron。禁止新旧 Runner writer 混跑，否则旧逻辑仍可能覆盖新事务快照。

按问题类型给出优先查看的路径，减少无效搜索。

## Shell 子进程 / Python runtime guard / `/opt/.swe`

- shell 工具环境构造：[src/swe/agents/tools/shell.py](../../src/swe/agents/tools/shell.py)
- 重点看 `_prepare_subprocess_env()` 是否保留后端 `SWE_WORKING_DIR` / `SWE_SECRET_DIR`
- runtime env 过滤：[src/swe/envs/runtime.py](../../src/swe/envs/runtime.py)
- 重点看 `PROTECTED_RUNTIME_ENV_KEYS`、`_scrub_user_tool_subprocess_env()` 和 `preserve_boundary_env_keys`
- Python runtime guard 注入：[src/swe/security/python_runtime_path_guard.py](../../src/swe/security/python_runtime_path_guard.py)
- 重点看 `prepare_python_runtime_path_guard_env()`、trusted paths 和 trusted entrypoint roots
- 包导入期 env 加载：[src/swe/**init**.py](../../src/swe/__init__.py)、[src/swe/envs/store.py](../../src/swe/envs/store.py)
- CLI 根命令读取 last API：[src/swe/cli/main.py](../../src/swe/cli/main.py)、[src/swe/config/utils.py](../../src/swe/config/utils.py)
- 回归测试：[tests/unit/test_shell_tenant_boundary.py](../../tests/unit/test_shell_tenant_boundary.py)

## Console 复制 / Clipboard 权限策略

- 通用复制工具：[console/src/utils/clipboard.ts](../../console/src/utils/clipboard.ts)
- Chat 工具卡片复制入口：[console/src/components/agentscope-chat/Util/copy.ts](../../console/src/components/agentscope-chat/Util/copy.ts)
- 工具调用卡片渲染：[console/src/components/agentscope-chat/OperateCard/preset/ToolCall.tsx](../../console/src/components/agentscope-chat/OperateCard/preset/ToolCall.tsx)
- 复制兼容性测试：[console/src/components/agentscope-chat/Util/copy.test.ts](../../console/src/components/agentscope-chat/Util/copy.test.ts)

## Console 流式会话切换 / reconnect

- 后端入口：[src/swe/app/routers/console.py](/Users/shixiangyi/code/Swe/src/swe/app/routers/console.py)
- 运行态跟踪：[src/swe/app/runner/task_tracker.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/task_tracker.py)
- Chat 映射管理：[src/swe/app/runner/manager.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/manager.py)
- 前端会话映射：[console/src/pages/Chat/sessionApi/index.ts](/Users/shixiangyi/code/Swe/console/src/pages/Chat/sessionApi/index.ts)
- 前端 reconnect 触发：[console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Context/ChatAnywhereSessionsContext.tsx](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Context/ChatAnywhereSessionsContext.tsx)
- 前端请求 owner 透传：[console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx)

## Console 第二轮提问 / OpenAI system role 顺序

- 后端 query 生命周期：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `_emit_stop_hook_if_needed()` 是否把 STOP hook additionalContext 追加成 hook 前缀 `system` 消息
- Tool hook 结果记录：[src/swe/agents/tool_guard_mixin.py](/Users/shixiangyi/code/Swe/src/swe/agents/tool_guard_mixin.py)
- 重点看 `_record_tool_hook_result()` 是否把 tool hook additionalContext 追加成 hook 前缀 `system` 消息
- Hook 消息构造：[src/swe/agents/hook_runtime/messages.py](/Users/shixiangyi/code/Swe/src/swe/agents/hook_runtime/messages.py)
- 重点看 `build_hook_additional_context_msg()` 是否只生成标准 `system` 消息
- OpenAI-compatible formatter：[src/swe/agents/model_factory.py](/Users/shixiangyi/code/Swe/src/swe/agents/model_factory.py)
- 重点看 `_strip_top_level_message_name()` 是否保留 hook 前缀 `system`，并把其他非首位 system 降级成 `user`
- OpenAI-compatible 请求兼容层：[src/swe/providers/openai_chat_model_compat.py](/Users/shixiangyi/code/Swe/src/swe/providers/openai_chat_model_compat.py)
- 重点看是否还残留 `developer -> user` 的自动重试
- Runner 回归测试：[tests/unit/app/test_runner_hook_runtime.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_runner_hook_runtime.py)
- Formatter 回归测试：[tests/unit/agents/test_model_factory_tenant.py](/Users/shixiangyi/code/Swe/tests/unit/agents/test_model_factory_tenant.py)
- Provider 兼容回归测试：[tests/unit/providers/test_openai_stream_toolcall_compat.py](/Users/shixiangyi/code/Swe/tests/unit/providers/test_openai_stream_toolcall_compat.py)
- Tool hook 回归测试：[tests/unit/agents/test_tool_guard_hook_runtime.py](/Users/shixiangyi/code/Swe/tests/unit/agents/test_tool_guard_hook_runtime.py)

## 会话恢复 / developer role 反序列化断言

- 会话加载边界：[src/swe/app/runner/session.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/session.py)
- 重点看 `load_session_state()` 调底层 `load_state_dict()` 前是否把 legacy `developer` 单向迁移成 `system`
- Hook 消息构造：[src/swe/agents/hook_runtime/messages.py](/Users/shixiangyi/code/Swe/src/swe/agents/hook_runtime/messages.py)
- 重点看 `build_hook_additional_context_msg()` 是否仍只生成 `system`
- Runner 入口：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `get_state_loaded()` 是否把 session 恢复异常直接暴露到 query 主链路
- 会话加载回归测试：[tests/unit/app/test_session.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_session.py)

## accepted plan 内部 tool exchange

- accepted plan 注入 helper：[src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- 重点看 `_build_accepted_plan_tool_exchange()` 是否只接受 `accepted_plan_source=server_plan_store` 且避开 Plan Mode
- Agent 推理入口：[src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- 重点看 `_reasoning()` 是否仅把内部 exchange 注入当前轮次，不进入 Toolkit、ToolGuard、hook 或前端工具卡片
- OpenAI / Anthropic formatter：[src/swe/agents/model_factory.py](/Users/shixiangyi/code/Swe/src/swe/agents/model_factory.py)
- 重点看 tool call / tool result 是否保持同一个 id，且最终请求里不出现 `developer`
- Plan Mode 请求上下文装配：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `_create_agent_for_query()` 是否只把服务端 accepted plan 放进 `request_context`
- 回归测试：
  - [tests/unit/app/test_task_progress_switch.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_task_progress_switch.py)
  - [tests/unit/app/test_runner_plan_mode_state.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_runner_plan_mode_state.py)
  - [tests/unit/agents/test_model_factory_tenant.py](/Users/shixiangyi/code/Swe/tests/unit/agents/test_model_factory_tenant.py)

## Plan Interaction Card 发出后继续 reasoning

- Plan Interaction Tool 执行路径：[src/swe/agents/tool_guard_mixin.py](/Users/shixiangyi/code/Swe/src/swe/agents/tool_guard_mixin.py)
- 重点看 `_run_plan_interaction_tool_call()` 是否只在成功产出 `plan_interaction_card` metadata 后设置 turn boundary
- AgentLoop 下一轮 reasoning：[src/swe/agents/tool_guard_mixin.py](/Users/shixiangyi/code/Swe/src/swe/agents/tool_guard_mixin.py)
- 重点看 `_reasoning()` 是否消费 turn boundary 并返回空 assistant 消息，让 AgentScope 在同批工具完成后自然退出本轮
- 回归测试：[tests/unit/subagents/test_react_agent_and_guard_integration.py](/Users/shixiangyi/code/Swe/tests/unit/subagents/test_react_agent_and_guard_integration.py)

## 长 Tool 执行 / 用户中断 / running 状态

- 前端 chat 请求入口：[console/src/pages/Chat/index.tsx](/Users/shixiangyi/code/Swe/console/src/pages/Chat/index.tsx)
- 前端 abort 语义：[console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/abortReasons.ts](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/abortReasons.ts)
- 前端停止与请求 owner：[console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx)
- 后端运行态跟踪：[src/swe/app/runner/task_tracker.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/task_tracker.py)
- 后端 query timeout：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- Console stop API：[src/swe/app/routers/console.py](/Users/shixiangyi/code/Swe/src/swe/app/routers/console.py)

## Tool Result 截断 / `<<<TRUNCATED>>>`

- 内置截断标志：[src/swe/constant.py](/Users/shixiangyi/code/Swe/src/swe/constant.py)
- 文件读取首次截断：[src/swe/agents/tools/file_io.py](/Users/shixiangyi/code/Swe/src/swe/agents/tools/file_io.py)
- 文件截断 helper：[src/swe/agents/tools/utils.py](/Users/shixiangyi/code/Swe/src/swe/agents/tools/utils.py)
- Agent 运行配置默认值：[src/swe/config/config.py](/Users/shixiangyi/code/Swe/src/swe/config/config.py)
- source 覆盖合成：[src/swe/app/source_system_config/runtime.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/runtime.py)
- 历史 tool_result 压缩 hook：[src/swe/agents/hooks/memory_compaction.py](/Users/shixiangyi/code/Swe/src/swe/agents/hooks/memory_compaction.py)
- MCP 工具返回转换：[src/swe/app/mcp/**init**.py](/Users/shixiangyi/code/Swe/src/swe/app/mcp/__init__.py)
- 详细经验：[analysis/playbook/tool-result-truncation.md](tool-result-truncation.md)

## Tenant bootstrap / default workspace scaffold

- 最小 bootstrap：[src/swe/app/migration.py](/Users/shixiangyi/code/Swe/src/swe/app/migration.py)
- 重点看 `ensure_default_agent_exists()`、`_do_ensure_default_agent()` 和它们只保证到哪一层
- 租户初始化总控：[src/swe/app/workspace/tenant_initializer.py](/Users/shixiangyi/code/Swe/src/swe/app/workspace/tenant_initializer.py)
- 重点看 `initialize_minimal()`、`ensure_seeded_bootstrap()`、`ensure_default_workspace_scaffold()`
- 租户池自愈入口：[src/swe/app/workspace/tenant_pool.py](/Users/shixiangyi/code/Swe/src/swe/app/workspace/tenant_pool.py)
- 重点看 cached tenant 再次 `ensure_bootstrap()` 时是否会补齐缺失的 `config.json`、`agent.json` 和模板文件

## 当前 Source 系统配置页 / task progress 开关

- Console 页面入口：[console/src/pages/SystemConfigPage/index.tsx](/Users/shixiangyi/code/Swe/console/src/pages/SystemConfigPage/index.tsx)
- 重点看 current-source 页面只读当前 iframe/source、403 态和保存/删除后是否调用 effective config 刷新
- 前端 current-source API：[console/src/api/modules/sourceSystemConfig.ts](/Users/shixiangyi/code/Swe/console/src/api/modules/sourceSystemConfig.ts)
- 前端权限头：[console/src/api/authHeaders.ts](/Users/shixiangyi/code/Swe/console/src/api/authHeaders.ts)
- 前端聊天页步骤条渲染开关：[console/src/pages/Chat/index.tsx](/Users/shixiangyi/code/Swe/console/src/pages/Chat/index.tsx)
- 开关读取 helper：[console/src/pages/Chat/taskProgressConfig.ts](/Users/shixiangyi/code/Swe/console/src/pages/Chat/taskProgressConfig.ts)
- 后端 current-source 路由：[src/swe/app/source_system_config/router.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/router.py)
- 后端注册表与默认值裁剪：[src/swe/app/source_system_config/registry.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/registry.py)
- 后端 service 合成与裁剪入口：[src/swe/app/source_system_config/service.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/service.py)
- Agent 提示词门控：[src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- 工具 no-op 兜底：[src/swe/agents/tools/update_task_progress.py](/Users/shixiangyi/code/Swe/src/swe/agents/tools/update_task_progress.py)
- runner stream 附加门控：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)

## 定时任务会话历史清理

- 配置入口：`source_system_config.cron_task_session_cleanup`，后端默认值与校验在 [src/swe/app/source_system_config/registry.py](../../src/swe/app/source_system_config/registry.py)，运行时解析在 [src/swe/app/source_system_config/runtime.py](../../src/swe/app/source_system_config/runtime.py)
- 默认状态：清理默认关闭；在当前 Source 配置页打开并保存后，会通过 [src/swe/app/source_system_config/router.py](../../src/swe/app/source_system_config/router.py) 刷新当前 Agent 的外部系统任务注册。
- Console 管理页：[console/src/pages/SystemConfigPage/index.tsx](../../console/src/pages/SystemConfigPage/index.tsx)，前端读写/时间转 cron helper 在 [console/src/pages/SystemConfigPage/registry.ts](../../console/src/pages/SystemConfigPage/registry.ts)
- 系统任务注册与执行：[src/swe/app/crons/manager.py](../../src/swe/app/crons/manager.py)，cleanup 外部系统任务 ID 保存在 source 级 `.system_jobs/sources/.../system_jobs.json`，同一 source 换用户保存配置时复用同一个 external id；不会写入业务 `jobs.json`
- 外部调度回调分发：[src/swe/app/routers/internal.py](../../src/swe/app/routers/internal.py)，`task_type=cleanup` 不需要业务 `job_id`，并按回调 `source_id` 展开该 source 绑定的所有逻辑租户；`tenant_id` 不是单用户清理边界
- session 文件写锁：[src/swe/app/runner/session.py](../../src/swe/app/runner/session.py)，cron agent 写回路径在 [src/swe/app/runner/runner.py](../../src/swe/app/runner/runner.py)
- 数据边界：只清理文件系统 task session JSON 中的 `task_runs`、对应 `agent.memory.content` 和可判定时间的 `task_messages`；不清理 `swe_cron_executions`、Monitor、Tracing 或审计数据

## Cron callback 洪峰 / event loop lag / workspace 冷启动串行

- 外部调度回调入口：[src/swe/app/routers/internal.py](../../src/swe/app/routers/internal.py)，重点看 `/api/internal/cron/callback` 如何解析 `task_type`、`tenant_id`、`source_id` 和 `job_id`
- Cron job 定义仓库：[src/swe/app/crons/repo/json_repo.py](../../src/swe/app/crons/repo/json_repo.py)，重点看 `JsonJobRepository.load()` / `save()` 是否通过 `asyncio.to_thread()` 包住文件读写、JSON 编解码和 pydantic 校验，`get_job()` 是否命中 mtime/size 快照索引
- Dream 系统任务：[src/swe/app/crons/manager.py](../../src/swe/app/crons/manager.py)，重点看 `_load_dream_logs()` 与 `run_dream_archive_maintenance()` 是否仍通过 worker thread 执行，避免 dream 日志读取和归档维护放大普通 cron lag
- Dream 孤立文件候选与删除边界：[src/swe/app/routers/dream_logs.py](../../src/swe/app/routers/dream_logs.py)，重点看 `_scan_orphan_files()` 是否跳过隐藏目录，以及根目录 `dialog` 是否通过 `KEEP_DIRS` 与通用路径解析保持为保留目录
- Workspace 冷启动：[src/swe/app/multi_agent_manager.py](../../src/swe/app/multi_agent_manager.py)，重点看 `MultiAgentManager.get_agent()` 是否只在全局锁内访问 `agents` / `_agent_start_tasks`，不同 cache key 的配置加载、`Workspace` 构造和 `start()` 不应互相阻塞
- 首轮验证：优先跑 `venv/bin/python -m pytest tests/unit/app/test_cron_json_repo.py tests/unit/app/test_cron_dream_nonblocking.py tests/unit/app/test_multi_agent_manager_concurrency.py -q`

## 批调度 Agent 已成功但 intent 仍为 dispatched

- SWE `/api/scheduler/cron/execution` 回执仅完成身份校验和执行记录持久化；Scheduler 在调度循环中读取 `swe_cron_executions.status` 和 Monitor 写入的 `async_status`，双成功才结束 intent。
- 扫描与名额入口：[scheduler/src/scheduler/app/services/cron/scheduling_service.py](../../scheduler/src/scheduler/app/services/cron/scheduling_service.py)，`dispatch_ready_once()` 必须在容量门槛前汇总结果、回收超时。
- 结果关联及回收：[scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py](../../scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py)，检查 `reconcile_dispatched_executions()` 是否匹配 intent、batch、当前 attempt、job、tenant；不要仅按 job_id 查最新记录。
- `async_status` 为空时继续占用名额。Agent 执行和子任务等待共用现有派发超时预算；主成功但子结果缺失的回收错误为“获取子任务状态超时”。
- 先确认 Scheduler 与 Monitor 访问同一执行表，且 Monitor 原有子任务同步/聚合正常运行；沿用 Monitor 的无子任务成功规则，不应把这一行为误诊为 Scheduler 提前完成。
- 首轮验证：`$env:PYTHONPATH='scheduler/src'; .\.venv\Scripts\python.exe -m pytest tests/unit/scheduler -q`。

## 定时任务详情分行维度 Excel 导出

- 页面与按钮入口：`console/src/pages/Analytics/CronJobOverview/index.tsx` 的 `handleBranchExport`；导出时克隆当前 `RankingTable`，保留点击时的排序、序号与展示文本。
- 文件生成入口：`console/src/pages/Analytics/CronJobOverview/exportBranchTable.ts`。ExcelJS 按需加载，在浏览器生成 `.xlsx`，保留两层合并表头和全部 22 列；不调用 Monitor 导出接口。百分比、千分位、前导零按文本保存。
- 加载中、无数据或导出中禁用按钮；失败显示提示并恢复重试。核对导出内容时，比较点击时的表格快照，而非导出期间再次排序后的页面。
- 验证：`pnpm exec vitest run src/pages/Analytics/CronJobOverview`，包含实际文件生成和回读、当前排序、合并表头、特殊文本、无新增 API 请求及错误恢复。

## 我的任务自动预览文件选择

- 报告提取入口：`console/src/components/agentscope-chat/autoPreviewSelection.ts`，由任务结果卡片和页面预览共用。原始消息按从旧到新排列，选择最后一条含报告的消息及其中最后一个符合原有自动预览条件的文件；包括带 `auto-preview` 标记的 HTML，以及文件卡片中带 `resultId`、`templateId` 的动态报告，不解析文件名时间戳。同次执行优先取最终结果，没有匹配才取步骤结果。
- 页面入口：`ChatAutoPreviewHtmlProvider.tsx` 在会话加载完成后确定目标 URL；其他消息仍在生成时，已经出现的最新报告仍可自动预览。`AutoPreviewHtmlContext.tsx` 只接受该目标的文件卡片，120ms 防抖后打开一次。URL 比较沿用聊天媒体地址转换，父组件回调变化不能重置已经消费的预览机会。
- 排错注意：`MessageList` 会倒序挂载消息，不能用组件注册先后判断报告新旧。历史执行默认折叠规则在 `console/src/pages/Chat/sessionApi/index.ts`；最新执行没有报告时，不应回退到折叠的旧执行。加载期间不应消耗 5 秒候选等待窗口。
- 回归验证：在 `console/` 运行 `npm run test:run -- src/components/agentscope-chat/ChatAutoPreviewHtmlProvider.test.tsx src/components/agentscope-chat/autoPreviewSelection.test.ts src/pages/Chat/components/TaskRunGroupCard/index.test.tsx src/components/agentscope-chat/DownloadFileCard/index.test.tsx`。
