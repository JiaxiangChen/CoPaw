# 定位路径

## W+ SOP active-session 空结果

- 接口：`src/swe/app/wplus_sop/router.py` 的 `get_active_wplus_sop_session`。Chat 存在且归属匹配、但没有活动 SOP 时返回 `200` 和 JSON `null`；Chat 不存在或身份、归属校验失败仍返回 `404`。
- 调用：`console/src/pages/Chat/components/WPlusSopActiveBar/index.tsx` 在挂载、Chat 切换、窗口 focus 和页面恢复可见时刷新。有待确认入口时跳过查询，有 SOP session ID 时查询具体会话，否则用 Chat ID 查询 active-session。
- 前端空快照清除活动状态；比较快照版本前必须判空，避免从已有活动会话刷新为 `null` 时崩溃。测试见 `tests/unit/app/wplus_sop/test_router.py` 和状态栏 `index.test.tsx`。

## 批调度暂停／恢复与关闭任务跳过

- 独立状态与旧任务归属：`scheduler/src/scheduler/app/services/cron/batch_run_state.py`。先排除普通任务和批子任务，只对批父任务初始化；状态存储在控制表，不跟随父任务后续 enabled 变化。
- 入队和交接：`batch_admission.py` 原子建批；`dispatch_gate.py` 检查 control、token 和自身任务状态。暂停退回未交接领取，已关闭任务结算为 skipped，不作为模型失败样本。
- API：SWE `/api/cron/jobs/{id}/batch-dispatch/run-state`，Scheduler 内部 `/api/scheduler/cron/dispatch/parents/{id}/run-state`。暂停先内部门控、再关闭 batch_dispatch_external_job_id；恢复先恢复该物理批定时器、再解除门控，均不操作普通定时器。外部暂停失败时内部仍暂停；Console `BatchRunStateControl.tsx` 刷新后可点“同步外部暂停”重复提交当前版本的暂停请求，刷新本身只读。
- 迁移 CLI：`python -m scheduler.migrate_batch_run_state` 默认只读预览，显式 `--apply` 才写入。部署、回滚及真实 MySQL 并发验收见 `docs/deploy/cron-dispatch-run-state-upgrade.md`；不能新旧 Scheduler 混跑。

## Worker 调整记录截断 / 模型变化折线

- Monitor 查询入口仍为 `/api/monitor/cron/dispatch/workers`。`capacity_events_next_cursor` 非空时继续携带 `capacity_cursor` 请求相同 source/时间范围；第一页有策略和当前容量，续页仅取历史。
- 分页 SQL 在 `monitor/src/monitor/app/services/cron/capacity_history.py`：每页 100 条、created_at/id keyset、固定 max(id)；不要把单页条数误认为历史总量限制。
- Console `CronBatchDispatch/workerHistory.ts` 拉取完整分页链并按 provider/model 构造阶梯线；`WorkerHistoryChart.tsx` 渲染图表。加载失败不展示不完整历史，切换范围使旧请求失效。

## 批调度优先名单 / 人工重试

- 模型限流分类包含网关错误码 `LAILGW0429`（访问请求过多）和 `LAILGW0433`（输出 Token 每分钟上限），兼容 `rate-limited` 连字符文案；Python 分类与 SQL 失败统计共用 `FAILURE_RULES`。这类错误仍按原有失败/重试反馈参与 worker 调整，与鉴权过期排除规则不同。

- 配置与代理入口：`src/swe/app/crons/batch_operations.py`；保存仅作用于广播源任务，等待 Monitor 定义同步确认，失败时保留本地配置并提示重试保存。
- Scheduler 操作：`scheduler/src/scheduler/app/services/cron/batch_operations.py`；失败分类统计覆盖全部当前失败 intent，预览与每次入队最多 200 个，以 id/attempt 防重，人工重试事件和状态更新同事务。
- 排序依据：`batch_priority.py` 与 intent payload 的 `dispatch_priority`；排序策略快照在 batch 的 `callback_metadata.batch_dispatch_priority`，重复 callback 不覆写。
- 容量边界：`capacity_claim.py`；scope 行锁保护实际领取和容量写入，已有 claimed/acknowledged/dispatched 均占用名额。
- Console：`Control/CronJobs/components/BatchPriorityEditor.tsx`、`Monitor/CronBatchDispatch/FailurePanel.tsx`。失败批次只展示结果数，补跑整个 intent，需按错误类型确认配置修复或旧执行停止。
- 验证：Scheduler 单测、`test_cron_batch_operations.py`、Monitor dispatch 查询测试及上述前端组件测试；SQLite 测试验证状态和 SQL 逻辑，不能替代部署 MySQL 多实例锁验证。

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

- 页面与按钮入口：`console/src/pages/Analytics/CronJobOverview/index.tsx`。前端传入当前日期、分行及排序，不再克隆 DOM 或使用 ExcelJS 生成文件。
- API：`GET /api/monitor/cron/export-branch-dimension`，`start_date/end_date` 必填且包含起止日；`bbk_ids` 可选；`sort_by/sort_order` 成对提供，排序指标使用白名单。
- 后端路由：`monitor/src/monitor/app/routers/cron_branch_export.py`；复用 `QueryService.get_branch_behavior` 查询全量数据，由 `monitor/src/monitor/app/services/cron/branch_export.py` 生成原始 XLSX 二进制，前端读取 blob 下载。
- 保留 22 列、两层合并表头；计数千分位、比例两位小数，同值稳定排序，序号排序后从 1 开始。洞察/电访用户比例继续以 plan_managers 为分母。分行名强制文本避免公式解析；空数据返回合法表头工作簿。
- 用户于 2026-09-09 明确本次只需沿用现有查询过滤：使用 X-Source-Id（缺省 default）及 bbk_ids；未新增身份鉴权或分行权限机制。
- 加载中、无数据或导出中禁用按钮；失败提示并允许重试。参数或导出失败使用非 200 中文 detail JSON，成功为 Excel MIME 和 Content-Disposition 附件。
- 验证：Monitor 项目虚拟环境运行 `venv/Scripts/python.exe -m pytest tests/test_branch_export.py tests/test_branch_export_api.py tests/test_export_service.py tests/test_export_integration.py -q`；前端验证入口为 `console/src/api/modules/monitor.branchExport.test.ts` 和 `console/src/pages/Analytics/CronJobOverview/branchExport.test.tsx`。

## 我的任务自动预览文件选择

- 报告提取入口：`console/src/components/agentscope-chat/autoPreviewSelection.ts`，由任务结果卡片和页面预览共用。原始消息按从旧到新排列，选择最后一条含报告的消息及其中最后一个符合原有自动预览条件的文件；包括带 `auto-preview` 标记的 HTML，以及文件卡片中带 `resultId`、`templateId` 的动态报告，不解析文件名时间戳。同次执行优先取最终结果，没有匹配才取步骤结果。
- 页面入口：`ChatAutoPreviewHtmlProvider.tsx` 在会话加载完成后确定目标 URL；其他消息仍在生成时，已经出现的最新报告仍可自动预览。`AutoPreviewHtmlContext.tsx` 只接受该目标的文件卡片，120ms 防抖后打开一次。URL 比较沿用聊天媒体地址转换，父组件回调变化不能重置已经消费的预览机会。
- 排错注意：`MessageList` 会倒序挂载消息，不能用组件注册先后判断报告新旧。历史执行默认折叠规则在 `console/src/pages/Chat/sessionApi/index.ts`；最新执行没有报告时，不应回退到折叠的旧执行。加载期间不应消耗 5 秒候选等待窗口。
- 回归验证：在 `console/` 运行 `npm run test:run -- src/components/agentscope-chat/ChatAutoPreviewHtmlProvider.test.tsx src/components/agentscope-chat/autoPreviewSelection.test.ts src/pages/Chat/components/TaskRunGroupCard/index.test.tsx src/components/agentscope-chat/DownloadFileCard/index.test.tsx`。

## 定时任务关联计划 planId

- 创建接口 `POST /cron/jobs` 接受可选字符串 `planId`（最长 255 字符）；内部字段、返回值和 `jobs.json` 使用 `plan_id`，也接受 snake_case 输入。省略或 null 保存为 SQL NULL。
- 分发通过 `src/swe/app/crons/api.py` 的 `_build_broadcast_job` 复制任务；重复分发通过 `_merge_existing_child_with_source` 继承父任务当前的 `plan_id`。
- 同步链路：`monitor_sync_client.py::_build_job_sync_data` → Monitor `CronJobSyncRequest` → `SyncService.sync_job`，INSERT/UPDATE 都写 `swe_cron_jobs.plan_id`。同步仍为异步，失败查 Monitor sync 日志。
- 部署先在 Monitor 数据库执行 `deploy/migrations/2026_09_21_add_cron_plan_id.sql`，再部署 SWE/Monitor；脚本可重复执行，旧记录保持 NULL。`schema.py` 的初始化也覆盖新表与缺列升级，但当前服务启动路径不自动调用该初始化，不能只靠重启完成升级。
- 回归：`tests/unit/app/test_cron_plan_id.py`、`tests/unit/app/test_tenant_cron_api.py`。

## Claw 技能运行看板

- 页面：`/analytics/claw-data-overview`；组件：`console/src/pages/Analytics/ClawDataOverview/index.tsx`。
- 接口适配：`console/src/api/modules/taskTypeReport.ts`，使用共享 request 向 monitor 的 `/api/monitor/cron/task-type-report` 请求，继承来源与认证头。
- 日期按同一自然月限制。使用实际认证头 `X-Bbk-Id`：100 可选全部分行，非100只显示本分行并锁定；缺失身份不请求。分行和支行级联选项从 `/task-type-report/options` 查询 `jkh_user_inf` 同一名单快照；切换分行清空支行。
- 技能明细追加 `skill_detail=true` 和当前行分行/支行/user_id；客户经理查询支持 keyword（姓名/SAP号/岗位）。分行/支行全量查回、先展示20行并本地滚动追加；经理和经理技能明细请求 page/page_size=20，滚动逐页查询。切换条件取消旧请求，后续页失败保留已加载行并重试同一页。
- `ratio_unit=percent` 的值直接加百分号，不能再乘 100；null 显示“—”，比率可能超过 100%。技能明细是关联归属，不能将明细行相加作为总计。
- 本地 Vite 将 `/api/monitor` 代理至 `127.0.0.1:9090`；未启动 monitor 时可出现代理 500，先检查该服务，不要在页面伪造数据。
- 验证入口：`npm run test:run -- src/pages/Analytics/ClawDataOverview/index.test.tsx src/api/modules/taskTypeReport.test.ts`。
- 开发预览：`http://localhost:5173/analytics/claw-data-overview?demo=1`（注意是 data，不是 date）。仅开发环境启用显式模拟数据，包含三类任务、24 个默认分行及支行/经理/技能明细；不写入数据库。去掉 demo=1 恢复正式接口。

- 统计和技能明细各自通过 GET /task-type-report/export 导出后端 XLSX，传当前完整筛选、移除分页，包含未加载的行；模拟模式禁用导出。50000行以上后端返回413。契约见 docs/superpowers/specs/2026-09-14-claw-report-console/API.md。
- 客户行为同时展示去重客户数和点击总次数：`insight_customer_count` / `insight_count`、`phone_customer_count` / `phone_count`；次数列位于对应覆盖率之后，非名单方案显示“—”。

## 创建计划的经营场景可见范围

- `src/swe/app/wealth_plans/router.py` 的 `_fetch_external_scene_skills()` 调用外部 `POST /api/agent/workspace/skill-config/list`，只传当前请求分行 `bbkId` 和 `category`；产品大类为空表示全部。
- 外部服务负责返回当前分行及 `bbkId=100` 的共享场景，本项目直接展示返回结果，不额外查询 `100` 或合并场景列表。
- 创建和修改计划均通过 `_validate_plan_scenes_for_branch()` 校验，允许当前分行及 `100` 的场景，其他分行、无归属或已失效的场景仍拒绝；请求分行缺失时仍拒绝发布。使用共享场景不会改变规划本身的 `bbk_id`，规划保留创建人的分行归属。
- 排查入口：父系统 `iframeContext.bbk` → `X-Bbk-Id` → `request.state.bbk_id`；场景卡片来源文案 `skillBbkLabel` 只用于展示。
- 回归入口：`venv/bin/python -m pytest tests/unit/app/wealth_plans/test_router.py -q`。

## 财富工作台岗位角色映射

- 财富岗位映射需同时维护 `console/src/pages/WealthWorkbench/permissions.ts` 与 `src/swe/app/wealth_plans/roles.py`；`RB0108` 为理财主管岗（综合），映射为 `president`，沿用支行行长的页面权限、分行规划可见范围、分发要求和“行长关注”发布来源。
- 行长角色查看“行长关注”时，除同一 `bbk_id` 外，还要求当前用户 ID 与规划创建人 `sap_id` 一致；同 BBK 的两位行长不能互看，即使另一位行长在分发对象中。列表的 SQL/内存过滤与详情权限均需同步维护，入口为 `WealthPlanStore.list_for_viewer()`、`_visible_to()` 和路由 `_get_visible_plan()`。分行关注仍按原管理范围可见，客户经理仍可查看同 BBK 下分发给自己的行长关注。当前用户优先取 `request.state.tenant_id`，其次取 `X-User-Id`。

## 财富规划任务类型与技能/MCP 下发

- `GET /wealth/scene-skills` 返回的 `mcpRelationList` 经 Console 映射为规划场景的 `mcp_relations`。
- `src/swe/app/wealth_plans/publish.py` 的 `_build_job_spec` 按场景判断：空数组创建 `workflow`，非空数组保持 `agent`。只判断数组是否为空，`["    "]` 仍按非空处理。
- `src/swe/app/wealth_plans/market_dispatch.py` 的 `collect_distribution_items` 只汇总非空 MCP 关联数组场景的技能 item 和 MCP item；全是 workflow 场景时不调用 market 下发接口，混合规划只下发 agent 场景。
- 财富规划的 `_broadcast_scene_job` 复用 `crons.api.broadcast_job`，显式传 `enable_batch_dispatch=True`、`enable_offset=False`、`offset_window_hours=0`；首次分发完成后启用父任务批调度并同步子任务。父批调度在外部平台的注册时间与目标子任务均使用规划原排程，不按默认 4 小时窗口提前或错峰。窗口变化时，即使父任务已启用批调度，重新分发也会更新窗口和外部平台任务。仅创建人本人为目标时仍跳过广播、沿用本人源任务。
- 已发布的存量财富规划不会仅因代码更新自动改写外部平台；部署后需修改/重新发布相应规划，确认父任务的 `batch_dispatch_offset_window_hours`、`batch_dispatch_offset_minutes` 均为 `0`，外部批任务 cron 为原排程。其他入口默认仍使用 4 小时窗口。
- 中台选多人后，定时任务主列表仍只查询当前请求租户的 `CronManager`，显示创建人的源/父任务，不能据此判定其他人的子任务未生成。通过父任务“更多 → 查看分发用户”并刷新核对目标子任务；缺失时继续查场景的 `broadcast_task_id`、广播结果的 `failed_count/results/error`，以及目标租户初始化和任务创建错误。批调度子任务的独立外部调度会被跳过或暂停，由父批任务统一触发。
- 若中台失败而行长正常，不能仅归因于主列表隔离。后台多人广播没有按这两个岗位分支；按 `wealth plan publish loaded` 检查落库重读后的 `position/source/agent/target_count`，按 `wealth plan cron broadcast requested/scheduled` 检查过滤后目标数、归一化人数和广播任务 ID；错误日志 `cron broadcast target failed` 定位目标租户创建异常，`cron broadcast task failed` 定位身份补齐、进度落库或分发后批调度同步异常。接口收到 10 人及规划状态 `published` 均不代表 10 人实际创建成功。
- `cron broadcast task started` 打印归一化后的实际分发用户 ID 列表 `target_tenant_ids`；`cron broadcast target result` 逐人打印 `tenant/success/child_job/error`。用同一 `task` 串联名单与结果；逐人成功表示子任务创建/更新完成，后续批调度同步仍需结合广播任务最终状态判断。
- 批调度切换失败查看广播任务的 `failure_summary`；检查 `SWE_CRON_DISPATCH_INTENTS_ENABLED` 与调度服务配置。不要仅给广播上下文设批调度标志，否则会漏掉父任务的调度注册与子任务同步。
- 创建/广播定时任务的顺序不变；`workflow` 保留 `scene_id` 作为 `skill_ids`，其 `text` 和 `request` 由 `CronJobSpec` 校验器清空。
- 回归入口：`venv/bin/python -m pytest tests/unit/app/wealth_plans/ -q`。

## 财富工作台看板与列表标题

- 列表入口为 `console/src/pages/WealthWorkbench/Board/index.tsx`，标题使用 `Plan.name`（规划名称）。日历入口为同目录的 `Calendar.tsx`，卡片标题及悬浮提示展示当天执行的技能名称 `PlanItem.sceneName`，多项技能用 ` / ` 连接，不显示规划名称。
- 列表的“规划周期”按所选日期范围与规划 `start/end` 的闭区间交集筛选，不按 `Plan.period` 类型筛选。“自定义”显示日期范围选择器，默认本月，切换快捷周期后保留此前选定的范围；来源分类和概览统计使用同一筛选结果，日历仍使用自身的周/月区间。
- 日历按各场景排程筛选当天执行的技能，点击卡片仍传递日期与规划 ID；回归测试见 `Board/Calendar.test.tsx`，覆盖周、月两种维度。
- 日历的目标客户与已生成任务复用 `Board` 的 `planStat`，与列表一样按技能及完整执行周期查询 `/wealth/skill-stats` 并汇总；未返回或接口不可用时显示 `--`，真实零值显示 `0`。不要直接使用规划映射中初始化为零的 `Plan.customers/tasks`；回归测试见 `Board/index.test.tsx`。

## 财富任务名单动态字段

- 外部接口为 `POST /api/agent/workspace/name-list`；Console 使用 `GET /api/wealth/name-list`，代理返回 `items`、`skillFieldList`、`allFields`。
- 客户动态值数组的正式字段名是 `fieldList`；属性仍为 `filedName / filedNameCn / filedValue`。数组名拼错会导致模型丢弃明细，前端即使收到 `allFields` 也显示“暂无指标”。
- 指标丢失先检查 `src/swe/app/wealth_plans/models.py` 和 `router.py` 是否保留客户明细及顶层字段配置，再检查 `console/src/pages/WealthWorkbench/api.ts` 的映射。
- `allFields` 提供中文名称；经营视角按技能的 `fields` 顺序与客户实际字段取交集，客户视角按全局字段顺序展示客户实际字段。不为其他客户的字段生成占位。
- 只有今日任务经营视角使用 `groupField`。组合值完全一致才合并，空数组不分组；把客户姓名配置为分组字段可能导致每人一组。每个分类对应独立表格，分类标题、人数和折叠按钮放在表格上方；多个字段以 ` / ` 分隔。
- `touched=1` 标记客户已触达；客户字段缺失不展示、返回空值显示“未提供”、数值 `0` 正常展示。
- 指标交互入口为 `components/CustomerMetrics.tsx`；分组入口为 `Tasks/customerGroups.ts`。大量指标先查展开状态；长字段先查悬停或聚焦提示，不应依靠增加全部动态表头排查。
- 工作任务树的技能名称单行省略；统一 Tooltip 在悬停或键盘聚焦时展示完整技能名称与来源规划，移除按钮原生 `title`，避免重复提示。
- 已完成列表不重复展示“经营任务”，技能名称统一在“经营场景”展示。指标越界覆盖触达方式或完成时间时，检查 `.sceneTable.completedTable` 的固定列宽与最小表宽：触达方式 100px、完成时间 160px、经营结果 100px，其余固定列合计后必须为指标区保留至少 520px（窄屏两列指标为 356px）。用六项以上指标并展开验证，不能只用单项指标验证列宽。
- 客户视角、待触达和已完成表格的重点标签溢出时，检查 `index.module.less` 的 `.customerTable.metricsTable.sceneTable`：标签列固定 135px，多标签内容限定在单元格内换行。经营场景列 160px，由 `components/CustomerScenes.tsx` 每行展示一个背景项，超宽省略并支持悬停或键盘聚焦查看全名；默认两项，更多通过“展开剩余 X 项”查看。经营机会列 240px，预览最多三行并通过悬停或键盘聚焦查看完整内容。今日任务客户视角的经营方案列固定 135px。新增或加宽列时同步调整表格最小宽度，保留其他列空间并使用表格内部滚动。
- 今日任务进入页面、切回经营视角或点击经营视角的刷新图标时，经 `store.ts` 的 `refreshTodayCustomers` 先查询最新规划，再用新规划查询当前视角名单。页内切到客户视角只调用 `loadTodayCustomers` 查询聚合名单，不重复查询规划。刷新期间禁用重复刷新和视角切换；已失效的选中任务回退到首个任务。不自动轮询，发布尚未完成时需完成后手动刷新。

### 财富规划分发目标

- 前端入口：`console/src/pages/WealthWorkbench/distributeTargets.ts`，调用 `POST /user-info/positions/by-branch`。
- 后端代理：`src/swe/app/routers/user_info.py::query_user_positions`，POST 到网关 `getUserPostionByBrn`；凭证来自环境配置的 `SWE_USER_POSITION_HEADER_KEY` 和 `SWE_USER_POSITION_HEADER_VALUE`，两个环境暂为 `xxx`、`yyyy`，可通过环境变量覆盖。
- 地址配置：`src/swe/config/envs/dev.json` 和 `prd.json` 中的 `SWE_USER_POSITION_API_URL`，分别使用 `wmcomsrvgatewayxc.paasst.cmbchina.cn` 和 `platform-gateway.paasoa.cmbchina.cn`；由 `SWE_ENV` 选择，环境变量可覆盖默认值，修改后重启后端生效。
- 入参来自 `iframeStore`：`bbkOrgId ← bbk (vbbk)`、`mOrgCode ← orgCode (vorgcode)`、`mOrgLvl ← Number(orgLvl) (vorglvl)`。缺少机构参数时不会查询。
- 上游成功标记为 `code=success`；目标 ID 使用 `sapID`（保留前导零），姓名为 `userName`，机构名称优先 `subBranchName`，其次 `branchName`。按上游返回名单展示，不再查询本地租户或按分行号二次过滤，也不额外过滤岗位。
- 行长沿用登录支行的 `orgCode/orgLvl` 查询；分行中台先调用 `POST /user-info/branches/by-branch`，入参为登录分行 `bbk`、`mOrgLvl=Number(iframeStore.orgLvl)`（cookie `vorglvl`）、`sysId=iframeStore.sysId`（cookie `sysid`），缺少必要上下文或机构级别无效时不查询，每次选择机构后继续用该条目的 `mOrgCode/mOrgLvl` 查询 `BranchTreeInfoService`；有下级就展开下一列，直到返回空数组才将末级机构参数传给人员接口，不依赖 `leaf` 字段判定末级。
- 支行上游 `BranchTreeInfoService` 地址由 `SWE_BRANCH_TREE_API_URL` 在 dev/prd 中配置，分别使用 `wmcomsrvgatewayxc.paasst.cmbchina.cn` 和 `wmcomsrvgatewayxc.paas.cmbchina.cn`，复用人员接口的 header 凭证配置。
- 中台可跨支行累计选择人员；当前候选列表与全选操作仅针对当前支行，已选人员信息保留用于发布及移除。快速切换支行时过期响应不更新当前列表。

- 两个外部接口均透传 `Cookie`：优先使用前端统一请求层的 `x-header-cookie`（保留切换用户后的上下文），为空时读取请求 `Cookie`；每次独立组装 header，不修改共享凭证字典。
- 中台支行级联选择器支持前端模糊搜索，对已加载的机构按支行名称或机构编码进行包含匹配，忽略英文大小写和搜索词首尾空格；搜索不额外请求支行接口，也不改变跨支行累计选择。

- 级联查询失败时显示重试，不当作末级；切换上级清空当前候选人员并使旧请求失效，但保留跨支行累计的人员选择。子支行按需加载并缓存，搜索范围仅包含已经加载的机构。
- 业务例外：`mOrgCode=121516` 在任意层级直接视为末级，跳过下级支行查询，用该条目的 `mOrgCode/mOrgLvl` 查询人员。
- 修改规划的人员回显：`api.ts::mapPlan` 保留计划返回的 `targets.sap_id/name`；`store.ts::editPlan` 恢复 ID 与姓名缓存，并重置当前支行候选状态。后续人员加载保留编辑计划的历史目标信息；历史目标无姓名时回显 ID。计划未保存机构路径，修改页的级联路径需重新选择，但已选人员无需重新勾选。
