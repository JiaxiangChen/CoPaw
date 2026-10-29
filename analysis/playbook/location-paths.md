# 定位路径

## 按来源查询租户及机构过滤

- `GET /user-info/tenants/by-source` 位于 `src/swe/app/routers/user_info.py::list_tenants_by_source`；`source_id` 未传时沿用请求头来源，查询包含模板条目。
- 可选 Query 参数 `bbk_id` 通过 `TenantInitSourceStore.get_by_source` 下推 SQL 精确匹配；不传时返回该来源全部条目，无匹配时返回空列表。回归入口：`tests/unit/routers/test_user_info_tenant_source.py`、`tests/unit/workspace/test_tenant_init_source.py`。
- 在 `migrate_tenant_init_source_bbk.sql` 之后执行 `scripts/sql/migrate_tenant_init_source_source_bbk_index.sql`，为新旧数据库补充 `idx_source_bbk (source_id, bbk_id)` 联合索引；脚本检查当前数据库索引是否存在，可重复执行。

## 定时任务第一阶段全量快照迁移

- 一次提交：`POST /api/internal/cron/migration/tasks`，operation 为 preview/import/verify；后台自动翻页，不必人工传递游标。只写新定义/状态业务表，旧 CRUD、文件、内存及执行入口不切换，独立系统任务不迁移。
- 任务编排与持久进度：`src/swe/app/crons/migration_tasks.py`，复用既有 `swe_async_tasks`、`swe_async_task_items`。查询 `GET /tasks/{task_id}` 可跨实例；`pages` 是逐页诊断，`after_page` 只控制诊断读取，不驱动迁移。
- `partial_failed` 表示失败/冲突，核对时也涵盖缺失/差异/来源丢失。内存缺失另看 `progress.memory_incomplete`，全量文件扫描不代表集群内存齐全。
- 停止用 `POST /tasks/{task_id}/stop`，当前批结束后生效。停止 Postman 请求不等于停止后台 worker。没有重启自动续跑；疑似中断先确认实例，再同范围重新提交安全重跑。
- Postman、认证、已有进度表前提和保护上限见 [迁移操作说明](../../docs/deploy/cron-phase1-snapshot-migration.md)；原同步分页接口仍保留。

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
第二个 Pod 并发读取原子 JSON 替换时始终可解析且 revision 不倒退，写者逐次核对连续 revision，读者须实际读取中途水位并核对最终 revision。脚本也检查硬链接独占发布及删除暂存名后的内容保留。失败时禁止以无锁写入或 Redis 锁替代；
先保留旧版本。Job 镜像必须是包含 `scripts/verify_session_nas_lock.py` 的本次 release 镜像；示例默认使用
`agentscope/swe:latest`，生产环境应改为已验证的不可变 release tag。

通过后暂停 cron dispatch，排空现有 session 请求，将旧 Runner Pod 缩容到零，再部署新版本，确认所有新 Pod
使用同一绝对 session 路径后恢复 cron。禁止新旧 Runner writer 混跑，否则旧逻辑仍可能覆盖新事务快照。

每次验证使用唯一、全新共享测试子目录，不直接使用用户 session 目录；失败后保留现场，下一次换新目录。两份脚本 JSON 及 Pod 节点、镜像摘要、PVC/StorageClass 都须留档，单机两个进程不算两 Pod NAS 通过。2026-10-16 本地 76 项测试、迁移/旧格式导出及 `ddb683634` 旧源码继续写入已通过，详见[本地验收报告](../../docs/superpowers/specs/2026-10-16-memory-storage-verification-report.md)；真实 NAS 门禁仍未执行。

真实历史副本入口为 `scripts/rehearse_memory_history.py`，源目录只读、输出目录必须全新且在源目录外，原始数据和私有指纹不得入库。2026-10-16 已完成 17 个 session、4 份 events-only 日志的脱敏迁移/导出及旧源码追加，380 个源文件 hash 未变化；[历史副本报告](../../docs/superpowers/specs/2026-10-16-memory-history-rehearsal-report.md)同时记录耗时、Python/RSS 峰值和文件数。样本最多 3 个 turn、77 条事件，不能把该结果当作长会话冷存储收益或真实 NAS 验收。

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

- Console 创建页场景列表及看板可用能力计数由 `console/src/pages/WealthWorkbench/api.ts` 的 `fetchSceneSkills()` 使用统一 `request("/agent/workspace/skill-config/list")` 请求 `POST {API base}/api/agent/workspace/skill-config/list`，由前端 Nginx 转发，不经过 Python 场景代理。JSON 只传 iframe 上下文的 `bbkId`（缺失时为空）和 `category`（空表示全部）；通过统一 request 附加鉴权和上下文请求头。读取外部 `code/data`，HTTP/业务失败、解析异常、网络异常或 60 秒超时，列表返回空数组、计数返回 null。后端代理保留兼容及规划校验用途。
- 外部服务负责返回当前分行及 `bbkId=100` 的共享场景，本项目直接展示返回结果，不额外查询 `100` 或合并场景列表。
- 创建和修改计划均通过 `_validate_plan_scenes_for_branch()` 校验，允许当前分行及 `100` 的场景，其他分行、无归属或已失效的场景仍拒绝；请求分行缺失时仍拒绝发布。使用共享场景不会改变规划本身的 `bbk_id`，规划保留创建人的分行归属。
- 排查入口：场景查询为父系统 `iframeContext.bbk` → POST body 的 `bbkId`；规划提交/校验仍为 `X-Bbk-Id` → `request.state.bbk_id`。场景卡片来源文案 `skillBbkLabel` 只用于展示。
- 回归入口：`venv/bin/python -m pytest tests/unit/app/wealth_plans/test_router.py -q`。

## 财富工作台岗位角色映射

- 财富岗位映射需同时维护 `console/src/pages/WealthWorkbench/permissions.ts` 与 `src/swe/app/wealth_plans/roles.py`；`RB0108` 为理财主管岗（综合），映射为 `president`，沿用支行行长的页面权限、分行规划可见范围、分发要求和“行长关注”发布来源。
- 行长角色查看“行长关注”时，除同一 `bbk_id` 外，还要求当前用户 ID 与规划创建人 `sap_id` 一致；同 BBK 的两位行长不能互看，即使另一位行长在分发对象中。列表的 SQL/内存过滤与详情权限均需同步维护，入口为 `WealthPlanStore.list_for_viewer()`、`_visible_to()` 和路由 `_get_visible_plan()`。分行关注仍按原管理范围可见，客户经理仍可查看同 BBK 下分发给自己的行长关注。当前用户优先取 `request.state.tenant_id`，其次取 `X-User-Id`。

## 财富规划任务类型与技能/MCP 下发

- `POST /api/agent/workspace/skill-config/list` 返回的 `mcpRelationList` 经 Console 映射为规划场景的 `mcp_relations`。
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

## 财富规划推送客户数

- 创建/修改规划的每个场景可选填“推送客户数”，必须为正整数且不超过 JavaScript 安全整数范围，空值表示不限。配置区采用紧凑单行：经营场景和经营方向为独立列，场景与方向按约 6:4 分配可用宽度，最小宽度分别为 230px 和 140px，优先展示场景；方向列仅显示输入框并铺满该列，相邻列间距 25px，任务周期和执行频率都使用直接点击的按钮组；每日/每小时保持单行，每周/自定义的频率配置允许两行（频率按钮与时刻可同行，执行日或自定义规则放下一行）；客户数列 110px，横向操作列 82px。每日/每小时行高 56px，每周/自定义展开后约 93px，数量提示移至表头悬停说明，非法数量仍在控件下方提示。表头和数据行用 CSS subgrid 共享列宽，周期和频率按内容定宽，场景和方向共同分配剩余空间，输入框宽度为方向列的 100%；场景名称过长省略并提供悬停全文。1920px 下已验证每日单行、每周/自定义两行，含自定义日期和多个执行日均无横向滚动；窄屏仅配置区内部横向滚动，不挤压控件。
- 表头统一展示“经营场景 / 经营方向 / 任务周期 / 执行频率 / 推送客户数（选填）”，使用浅蓝色背景（#F7FAFF）及 12px、600 字重的蓝灰色列名（#596E8C），与数据行连续衔接，行间用横向细分隔线。自定义重复规则下拉框固定 104px；不再显示标准或自定义频率的重复摘要，仅旧版复杂规则保留编辑提示；发布确认仍展示完整频率。
- 任务周期使用原有按钮组，通过 `onClick` 调用原 `setTaskCycle`，五种周期及选中状态直接可见；重复点击当前快捷周期按当前日期重算起止范围，点击“自定义”保持手填日期。周期、场景、方向及数量保持横向排列；每周/自定义仅频率配置区允许两行，空间仍不足时配置区域通过 `overflow-x:auto` 横向滚动，不挤压控件。执行频率使用原按钮组，仍调用原 `updateSchedule`，cron 更新逻辑保持原样。`Create/index.test.tsx` 覆盖五种当前周期的重复点击、初始渲染不改日期及其他场景数据隔离。
- `PlanItem.custNum` 由草稿状态独立保存，发布确认展示具体人数或“不限”。`api.ts` 创建 POST、修改 PUT `/zt/wealth/plans` 均在对应 `scenes[]` 条目携带数值 `custNum`；空值完全省略该字段，不能转换为 0 或 null。规划查询响应的 `scenes[].custNum` 回填至编辑草稿，缺失或 null 显示不限。此链路直接转发 Java，Python 兼容规划模块不参与。
- `/api/agent/workspace/name-list` 的 `skillIds` 为对象数组：`[{ skillId, custNum? }]`。客户数从当天有效规划场景经任务树传入查询；经营视角只传当前场景，客户视角、待触达、已完成和人数角标传当天全部场景，未配置客户数时每项仅传 `skillId`，不得携带 null 或默认数量。按技能去重，沿用任务树首个命中规划的配置。
- 回归入口：`Create/index.test.tsx`（输入、清空、无效提示、发布确认）、`api.test.ts`（名单对象参数、去重及省略客户数）和 `store.test.ts`（正整数校验、逐场景隔离、创建/修改 payload、编辑回显、各名单透传客户数）。

## 财富工作台看板与列表标题

- 看板模式周、月日历仅在“今天”的已发布规划卡片右下角显示名单入口：周视图“查看名单”与发布状态同行；月视图有名单入口时不展示执行频率，规划名、技能数与右侧“查看名单”同排一行；无名单入口时，规划名、技能数与执行频率同排一行，排程过长时省略，卡片 Tooltip 提供完整执行频率。周、月入口文字跟随卡片色调（蓝色卡片深蓝、紫色卡片深紫），搭配 10px Ant Design 右箭头，悬停显示下划线；入口悬停或聚焦提示“查看客户名单”，且仅向拥有任务页权限的客户经理展示。入口与卡片详情按钮为并列控件，点击不触发详情；按 `buildTaskTree([plan], today)` 取该规划首个当天有效场景 ID，通过 `/wealth/today?skillId=...` 进入经营视角。任务页使用 ID 初始化选择，规划异步加载期间保留该参数，失效后回退任务树首项。其他日期和无当天有效场景的规划不显示入口。回归入口为 `Board/Calendar.test.tsx`、`Board/index.test.tsx` 和 `Tasks/loading.test.tsx`。

- 规划列表、日历提示与详情主状态通过 `utils.ts` 的 `planPublishLabel` 按 `publishStatus` 展示“发布中 / 发布成功 / 发布失败”，缺失或未知值展示“状态未知”。列表、日历提示和详情均不展示分发结果。`Plan.status` 仍保留Java `boardStatus` 聚合结果，继续供自动轮询和统计失效判断使用；不要用展示文案替换底层分发状态。

- 列表入口为 `console/src/pages/WealthWorkbench/Board/index.tsx`，标题使用 `Plan.name`（规划名称）。日历入口为同目录的 `Calendar.tsx`，周视图卡片标题使用规划名，下方显示“经营场景：”并汇总该规划的 `PlanItem.sceneName`，多项场景用中文逗号连接，最多两行，超出省略。月视图标题同样使用规划名，名称后用纯数字表示该规划关联的全部技能数量，不新增场景正文；两种视图统一使用 Ant Design Tooltip，悬停或键盘聚焦时分行展示完整规划名、经营场景与状态，长名称自动换行；移除原生 `title`，避免重复提示。
- 列表的“规划周期”按所选日期范围与规划 `start/end` 的闭区间交集筛选，不按 `Plan.period` 类型筛选。“自定义”显示日期范围选择器，默认本月，切换快捷周期后保留此前选定的范围；来源分类和概览统计使用同一筛选结果，日历仍使用自身的周/月区间。
- 日历按各场景排程筛选当天执行的技能，点击卡片仍传递日期与规划 ID；回归测试见 `Board/Calendar.test.tsx`，覆盖周、月两种维度。
- 日历的目标客户与已生成任务复用 `Board` 的 `planStat`，与列表一样由前端直接查询 `/api/agent/workspace/skill-stats`，请求 `planIds` 为规划 ID 数组，由 Nginx 转发；按返回项 `planId` 读取 `targetCustomerCount` 和 `targetTaskCount`，不再按场景或日期区间求和。未返回或接口不可用时显示 `--`，真实零值显示 `0`。不要直接使用规划映射中初始化为零的 `Plan.customers/tasks`；回归测试见 `Board/index.test.tsx`。

- 周/月切换仅本地筛选规划，不按周重新请求 `/zt/wealth/plans`。统计由 `Board/usePlanStats.ts` 按账户和去重排序后的规划 ID 集合缓存；相同在途请求合并，规划状态轮询不会清空缓存。成功与失败响应缓存 30 秒，到期后在下一次周期/筛选变化或规划轮询时后台更新；页面静止时不定时刷新。后台更新保留已有数字，失败提示暂显示上次数据；无已有数据时显示 `--`；加载/后台更新仅通过刷新图标旋转和禁用反馈，失败仍提示统计不可用或暂显示上次数据。发布/分发从进行中结束时使统计失效并主动查询，若请求在途则返回后串行补查；切换账户清空缓存，过期响应不覆盖当前周期。回归入口为 `Board/usePlanStats.test.ts`。
- 看板仅在存在发布中/分发中规划时自动查询：`Board/useBoardRefresh.ts` 在上一轮完成后等待 5 秒再查询，每次连续处理周期最多 3 分钟；到期静默暂停，手动“刷新看板”不会重新启动自动轮询。看板的规划与统计单次请求均由 `Board/requestTimeout.ts` 限制为 60 秒：超时取消底层请求并释放等待，保留已有数据；离开看板会立即取消在途规划请求；被取消的规划请求即使晚返回也不更新 store。自动与手动规划刷新共用在途请求，刷新入口使用图标按钮（无障碍名称“刷新看板”），在规划或统计请求期间旋转并禁用；手动成功刷新会绕过统计有效期更新当前统计。看板刷新使用严格读取，接口失败保留规划，不额外展示暂停或规划刷新失败文案，其他读取仍保持原有容错行为。回归入口：`Board/useBoardRefresh.test.ts`。

## 财富任务名单动态字段

- 经营视角“经营机会”必须显式保留 260px，不能在 fixed 表格中依赖 `width:auto` 或 `min-width` 分配剩余空间；姓名 110px、方案 135px，表格最小宽度按其余固定列合计 1029px（含边框）加指标区宽度计算。指标区按断点为 520 / 356 / 190px，空间不足时表格内部滚动，执行列继续固定在右侧。只调整经营视角，不覆盖客户视角、待触达和已完成的列宽。

- 今日任务（经营/客户视角）、待触达及已完成清单在执行/完成时间列之前展示“最近接触时间”，直接使用 `name-list.strongContactTime` 映射的 `Customer.time`，空值显示“—”，悬停可读原值。最近接触时间列宽 128px，标题保持单行；内容使用 12px 数字，日期正常展示，完整日期时间超宽时省略，单元格 title 保留原值。`.recentContactValue` 限制内容宽度，避免自动表格布局被完整时间撑宽。窄屏仅表格内部横向滚动。空态 `colSpan` 必须包含新列。已完成页原“完成时间”也沿用 `Customer.time`，暂保留原行为；将来若接口提供独立完成时间，应分别映射。回归入口：`Tasks/groupTables.test.tsx`。

- 今日任务客户视角、待触达和已完成的“经营场景”列提供单选筛选，选项从当前模块原始名单的 `skillNames` 去重生成，按展示名称精确匹配任一场景；不使用客户的首个 `skillId` 代表全部场景。同名场景合并为一个筛选项。选中标签单行显示，超长省略；悬停或键盘聚焦通过 Tooltip 查看完整名称，清除按钮不收缩。支持“全部场景”和清除，与搜索、重点标签、接触状态筛选叠加；仅过滤表格，不重算顶部后端汇总。经营视角不应用该列筛选。入口为 `Tasks/SceneFilter.tsx` 和 `Tasks/index.tsx`，回归入口为 `Tasks/groupTables.test.tsx`。

- 今日任务经营视角以场景 ID 作为唯一选中依据，高亮、名单查询、客户筛选和分类折叠状态均关联该 ID，名称仅用于展示。同分类同名但 ID 不同的场景可独立选择；场景改名或调整分类保留选择，选中 ID 失效后回退首个场景。入口为 `Tasks/index.tsx`，回归入口为 `Tasks/groupTables.test.tsx`。

- 侧栏“今日任务”红色数字角标与客户视角口径一致：`fetchTodayCustomers(..., "customer")` 查询 `/api/agent/workspace/name-list`，传当前用户 sapId、当天工作任务树全部去重技能 ID 数组与 touched=2，统计该范围内聚合名单总人数（包含已接触客户），为 0 时隐藏。store 独立保存 `todayCustomerCount`，避免经营视角按技能查询影响总数；侧栏首次加载及今日名单刷新时更新。待触达和已完成角标分别来自对应客户名单长度。入口为 `components/Sidebar.tsx` 和 `store.ts`，回归入口为 `components/Sidebar.test.tsx` 和 `store.test.ts`。

- 今日任务在 `Tasks/index.tsx` 分组后对每个分类的名单稳定排序：`touched === 1` 的已接触客户排后，其余待处理（未接触及未知状态）排前。同类状态保持接口顺序，分类顺序和 store 原始名单不变；客户视角同样适用，待触达/已完成不参与此排序。回归入口为 `Tasks/groupTables.test.tsx`。
- 今日任务、待触达和已完成页均展示“目标客户 / 今日已触达 / 七日内已触达 / 待处理”，使用各自名单响应的后端统计，搜索和筛选不重算汇总；已完成页不展示“累计已完成 / 今日已完成”，也不读取今日任务缓存统计。触达日期与人数由外部接口计算。
- Console 直接请求 `POST /api/agent/workspace/name-list`，由前端 Nginx 转发；前端把外部 `data.list`、统计及字段配置转换为 `items`、四项计数、`skillFieldList`、`allFields`。
- 今日任务、待触达和已完成分别查询客户名单，均传 `sapId` 与 `skillIds` 数组：经营视角只传当前选中场景，客户视角、待触达、已完成及今日人数角标均传当天工作任务树全部去重场景 ID。前端使用统一 `request("/agent/workspace/name-list")` POST `{API base}/api/agent/workspace/name-list`，由部署方前端 Nginx 转发外部服务，不再经过 Python 名单代理。JSON 直接传 `skillIds: ["a", "b"]`、`sapId`、`touched`，并从 iframe 上下文补充 `bbkId`、可选 `subBbkId/posId` 及固定 `platformSource/pageSource`；不显式设置 `credentials`，沿用 fetch 默认的同源 Cookie 行为；鉴权和上下文请求头由统一 request 构建。前端读取外部 `code` 与 `data.list`，转换为页面 `items`；HTTP/业务失败、网络异常或六十秒超时返回空名单。原后端代理保留兼容，但工作台不再调用。当天无场景时前端直接清空名单，不请求接口。触达状态保持今日任务 `2`、待触达 `0`、已完成 `1`，直接使用接口返回的名单。场景切换只重查选中技能，手动刷新与标签切回保持该选择，旧场景响应不覆盖新选择。回归入口为 `api.test.ts`、`store.test.ts`、`Tasks/loading.test.tsx` 和 `tests/unit/app/wealth_plans/test_router.py`。
- 客户动态值数组的正式字段名是 `fieldList`；属性仍为 `filedName / filedNameCn / filedValue`。数组名拼错会导致模型丢弃明细，前端即使收到 `allFields` 也显示“暂无指标”。
- 指标丢失先检查 `src/swe/app/wealth_plans/models.py` 和 `router.py` 是否保留客户明细及顶层字段配置，再检查 `console/src/pages/WealthWorkbench/api.ts` 的映射。
- `allFields` 提供中文名称；经营视角按技能的 `fields` 顺序与客户实际字段取交集，客户视角按全局字段顺序展示客户实际字段。不为其他客户的字段生成占位。
- 只有今日任务经营视角使用 `groupField`。组合值完全一致才合并，空数组不分组；把客户姓名配置为分组字段可能导致每人一组。每个分类对应独立表格，分类标题、人数和折叠按钮放在表格上方；多个字段以 ` / ` 分隔。
- `touched=1` 标记客户已触达；客户字段缺失不展示、返回空值显示“未提供”、数值 `0` 正常展示。
- 今日任务的经营/客户双视角在客户指标之后展示“接触状态”：保留接口原始 `Customer.touched`，`0` 显示灰色“未接触”，`1` 显示绿色“已接触”，其余或缺失显示灰色 `--`。顶部汇总使用后端独立计数字段，不按明细 `touched` 或 `done` 计算；不要由 `done` 布尔值反推接触状态 `0`。今日任务表格的状态列宽 110px，标题及筛选图标保持单行，指标空间和执行列保持可用；回归入口为 `api.test.ts`、`Tasks/groupTables.test.tsx`。
- 接触状态表头通过 `Tasks/ContactStatusFilter.tsx` 单选筛选“全部 / 未接触 / 已接触 / --”，复用重点标签浮层和已选条件的样式，支持一键清除、方向键、Escape、外部点击及滚动/缩放关闭。选项人数基于其他筛选条件下的名单（排除接触状态自身），筛选与经营任务/重点标签叠加，表格和分类人数同步更新，顶部四项后端汇总保持不变。状态列需容纳筛选图标，长筛选值不得反向撑宽表格。回归入口增加 `Tasks/ContactStatusFilter.test.tsx`。
- 指标交互入口为 `components/CustomerMetrics.tsx`；分组入口为 `Tasks/customerGroups.ts`。大量指标先查展开状态；长字段先查悬停或聚焦提示，不应依靠增加全部动态表头排查。
- 工作任务树的技能名称单行省略；统一 Tooltip 在悬停或键盘聚焦时展示完整技能名称与来源规划，移除按钮原生 `title`，避免重复提示。
- 视角切换时统计数量先增后减，先查 `Tasks/index.tsx` 是否把旧经营名单直接用于客户视角：经营名单按技能与客户组合去重，客户名单由接口聚合，计数口径不同。名单查询在绘制前进入加载状态；加载时保留缓存，但不渲染旧名单，四个汇总及总记录数显示 `--`。使用延迟接口响应验证首个客户视角绘制帧，不能只检查请求完成后的数量。
- 已完成列表不重复展示“经营任务”，技能名称统一在“经营场景”展示。客户清单使用自动表格布局，常规最小宽度 1000px、今日客户视角 1100px、今日经营视角 800px；不再通过固定指标列及大量屏宽断点抬高最小表宽。长姓名允许换行，最近接触时间保留单行与完整值 title。今日任务和待触达的最近接触时间、经营方案、执行依次吸附右侧，滚动时不得互相遮挡。经营方案列宽120px，位于执行列左侧；最近接触时间的right偏移为经营方案与执行列宽度之和。
- 客户指标通过 `components/CustomerMetrics.tsx` 展示，每项固定125px、横向间距10px；整列使用auto宽度，最小84px以保留表头可读性。内容区最小125px、最大395px，采用Flex自动换行，每行1–3项；取消inline-size隔离，让指标数量参与表格固有列宽计算。经营场景通过 `components/CustomerScenes.tsx` 每行展示一个固定140px宽的背景项，短名称也保持相同宽度，默认两项；列宽由自动表格布局随内容尺寸收紧。两者超宽均省略，支持悬停或键盘聚焦查看全文、展开更多。经营机会内容不设固定最小、最大宽度，使用单元格可用空间，最多三行并支持Tooltip。样式入口为 `index.module.less` 的 `.customerMetrics`、`.metricGrid`、`.metricItem`、`.customerScenes`、`.opportunityPreview`。验证至少包含空指标、单项、六项以上指标及展开、长场景名称、1280/1440/1920px嵌入和普通视图；较窄容器允许表内滚动，不得产生页面横向溢出。
- 今日任务进入页面、切回经营视角或点击经营视角的刷新图标时，经 `store.ts` 的 `refreshTodayCustomers` 先查询最新规划，再用新规划查询当前视角名单。页内切到客户视角只调用 `loadTodayCustomers` 查询聚合名单，不重复查询规划。刷新期间禁用重复刷新和视角切换；已失效的选中任务回退到首个任务。不自动轮询，发布尚未完成时需完成后手动刷新。
- 浏览器标签页从隐藏切回可见时，工作台入口的 `useVisibilityRefresh` 调用 `refreshTaskLists`，每次直接后台更新今日任务（保持最近查询视角）、待触达和已完成名单及侧栏数量，不再先查询或比较客户数量；人数相同但客户或明细变化时也会同步。刷新期间保留当前名单和统计，不展示列表加载占位。仅客户经理且初始化/规划加载完成后生效，刷新期间忽略重复切回请求，已有名单请求进行中时不重复发起。iframe 内同样适用；宿主内部业务页签仅通过 CSS 隐藏 iframe 时不会触发该事件。回归入口：`useVisibilityRefresh.test.tsx`、`store.test.ts` 和 `Tasks/loading.test.tsx`。

### 财富规划分发目标

- 前端入口：`console/src/pages/WealthWorkbench/distributeTargets.ts`，使用统一 request POST `/zt/wealth/wmadm/getUserPostionByBrn`（人员，`USER_POSITION_PATH`）与 `/zt/wealth/wmadm/branchTreeInfo`（支行，`BRANCH_TREE_PATH`），最终路径为 `{API base}/api/zt/wealth/wmadm/*`，不再调用 `/api/user-info/*/by-branch`。复用统一鉴权、上下文请求头和 401 刷新逻辑；60 秒超时取消请求，人员失败返回空列表，支行失败返回 null 以保留重试。
- 部署侧需将上述两个 `{API base}/api/zt/wealth/wmadm/*` 路径转发到对应环境的网关，并按原后端 `SWE_USER_POSITION_HEADER_KEY/VALUE` 配置附加网关凭证，保留请求 Cookie；凭证不能放进前端包。原 Python 代理保留兼容。
- 地址配置：`src/swe/config/envs/dev.json` 和 `prd.json` 中的 `SWE_USER_POSITION_API_URL`，分别使用 `wmcomsrvgatewayxc.paasst.cmbchina.cn` 和 `platform-gateway.paasoa.cmbchina.cn`；由 `SWE_ENV` 选择，环境变量可覆盖默认值；这些配置仍用于 Python 兼容代理，前端直连需在部署 Nginx 中同步对应环境的目标地址。
- 入参来自 `iframeStore`：`bbkOrgId ← bbk (vbbk)`、`mmOrgCode ← orgCode (vorgcode)`、`mmOrgLvl ← Number(orgLvl) (vorglvl)`。缺少机构参数时不会查询。
- 两个新接口统一返回 `{code,message,data,success}`，仅校验 `code="200"`，不依赖 `success`；`data` 是非空数组时读取机构或人员列表，空数组或非数组按无数据处理。业务码非 `200` 按查询失败处理，支行保留重试。旧 Python 代理的 `code=success` 及裸机构数组不适用于新接口；目标 ID 使用 `sapID`（保留前导零），姓名为 `userName`，机构名称优先 `subBranchName`，其次 `branchName`。按上游返回名单展示，不再查询本地租户或按分行号二次过滤，也不额外过滤岗位。
- 行长沿用登录支行的 `orgCode/orgLvl` 查询；分行中台先直连上述 `BranchTreeInfoService`，入参为 `mmOrgCode=bbk`、`mmOrgLvl=Number(iframeStore.orgLvl)`（cookie `vorglvl`）、`sysId=iframeStore.sysId`（cookie `sysid`），缺少必要上下文或机构级别无效时不查询，每次选择机构后继续用该条目的 `mOrgCode/mOrgLvl` 查询 `BranchTreeInfoService`；每选择一级立即用当前条目的 `mOrgCode/mOrgLvl` 查询人员，同时独立查询下级机构；有下级就展开下一列，但可停留在当前级选择人员。返回空数组才确认末级，不依赖 `leaf` 字段判定末级。
- 两个前端分发接口请求体统一使用 `mmOrgCode/mmOrgLvl`；选择机构后仍取返回条目的 `mOrgCode/mOrgLvl`，只转换请求字段名，值及类型保持不变，`bbkOrgId/sysId` 保持原样。
- 支行上游 `BranchTreeInfoService` 地址由 `SWE_BRANCH_TREE_API_URL` 在 dev/prd 中配置，分别使用 `wmcomsrvgatewayxc.paasst.cmbchina.cn` 和 `wmcomsrvgatewayxc.paas.cmbchina.cn`，复用人员接口的 header 凭证配置。
- 中台可跨支行累计选择人员；当前候选列表与全选操作仅针对当前支行，已选人员信息保留用于发布及移除。快速切换支行时过期响应不更新当前列表。

- 直连请求不再使用统一请求层的 `x-header-cookie`；浏览器自动携带同源 Cookie，机构参数仍从 iframeStore 取得。Python 兼容代理继续优先读取 `x-header-cookie`，其次读取请求 Cookie。回归入口：`distributeTargets.test.ts`、`TargetPicker.branches.test.tsx`、`TargetPicker.edit.test.tsx`。
- 中台支行级联选择器支持前端模糊搜索，对已加载的机构按支行名称或机构编码进行包含匹配，忽略英文大小写和搜索词首尾空格；搜索不额外请求支行接口，也不改变跨支行累计选择。

- 级联查询失败时显示重试，不当作末级，也不阻塞当前级人员查询和选择；切换机构清空当前候选人员并使旧请求失效，立即查询新选机构的人员，但保留跨支行累计的人员选择。子支行按需加载并缓存，搜索范围仅包含已经加载的机构。
- 业务例外：`mOrgCode=121516` 在任意层级直接视为末级，跳过下级支行查询，用该条目的 `mOrgCode/mOrgLvl` 查询人员。
- 修改规划的人员回显：`api.ts::mapPlan` 保留计划返回的 `targets.sap_id/name`；`store.ts::editPlan` 恢复 ID 与姓名缓存，并重置当前支行候选状态。后续人员加载保留编辑计划的历史目标信息；历史目标无姓名时回显 ID。计划未保存机构路径，修改页的级联路径需重新选择，但已选人员无需重新勾选。

## 财富任务列表后台刷新显示

- 浏览器标签页切回引发的自动刷新由 `taskListsRefreshing` 标记。任务页在此期间保留现有名单、分类和统计，不清空为加载占位；最新名单返回后更新。首次进入页面、切换任务视角和手动刷新仍使用原加载提示。入口为 `Tasks/index.tsx`，回归入口为 `Tasks/loading.test.tsx`，覆盖今日任务、待触达和已完成三页。

工作台任务页顶部汇总随客户名单响应读取 `targetCusCount`、`todayTouchCount`、`sevenDayTouchCount`、`unTouchedCount`（外部响应 `data` 下，由前端适配到响应顶层）。今日任务经营视角使用所选当天技能的统计，其余视角/模块使用当天全部技能；搜索、标签和接触状态筛选只过滤表格，不重算后端汇总。各模块独立保存名单及统计并同步更新；首次加载显示 `--`，后台刷新保留旧值，缺失统计显示 `--`，当天无场景时显示 0 且不查询名单。列表分页条数仍按当前筛选结果展示。

今日任务经营视角原始客户名单为空且查询完成时，在当前任务标题旁展示执行时间；使用 `plans.items[].scenes[].cron_expr` 经 `mapSceneItem` 解析的排程，由任务树节点透传 `schedule` 并复用 `scheduleLabel`。日/周排程显示具体时分，小时排程显示“每小时整点”，自定义排程显示原表达式。搜索或接触状态筛选导致的空列表不触发展示；客户视角、待触达、已完成不显示。`plans` 不返回实际执行状态，空名单不能严格证明任务尚未执行。

## Stop 追加消息仅最后一条实时可见

- 入口：`src/swe/app/runner/stop_append.py::stream_stop_appended_messages`。每条追加消息必须使用结构化文本块 `[{"type": "text", "text": ...}]`，不能直接传字符串。
- AgentScope 实时流适配器会将裸字符串缓存到源生成器结束，新消息覆盖旧缓存；因此多条追加可能只显示最后一条，后续持久化失败时甚至一条也不显示，而历史中仍能恢复已提交消息。
- 回归入口：`tests/unit/app/test_runner_stop_append.py::test_live_adapter_publishes_each_append_before_next_save`，通过真实适配器验证多条成功追加逐条发送，以及第二条保存失败时第一条已送达。

## 财富工作台嵌入头部控制

- 父应用通过 iframe URL 的 `hideLogo=true` 同时隐藏整个财富工作台头部（品牌及标题）、信息播报与侧栏，例如 `/wealth/board?hideLogo=true`；未传或传 `false` 时正常显示。
- 入口与参数延续逻辑位于 `console/src/pages/WealthWorkbench/index.tsx`。内部导航省略参数时补回入口隐藏设置并保留其他查询参数，支持导航后刷新；不写入 sessionStorage，不影响独立打开的工作台。
- `.root.withoutHeader` 位于同目录 `index.module.less`，同步消除头部和播报占位，调整侧栏、正文与表单锚点偏移；全局 `MainLayout` 的 `hideMenu` 不控制该独立骨架。

- `hideLogo=true` 对所有角色统一隐藏整个侧栏（包含规划入口及任务导航）并移除占位，正文填满可用宽度；不带该参数时侧栏行为保持不变。内部导航仅延续隐藏设置，不额外补回 `origin=Y`。
- 父应用直达：`/wealth/board?origin=Y&hideLogo=true` 为看板，`/wealth/create?origin=Y&hideLogo=true` 为新建。部署在 `/console` 下时各地址加此前缀。
- `origin=Y` 初始化时若 URL 的 `hideLogo` 严格为 `true`，沿用前面已设置的初始上下文和导航参数，仅标记初始化完成后返回，跳过 `initFromUrlParams`：不查询客户信息、不初始化 Agent、不调用 `/auth/cron-auth`，也不启动客户信息刷新定时器。其他参数值沿用原流程；实现及回归位于 `console/src/utils/iframeMessage.ts` 和同名测试文件。
- 编辑仅由工作台内部触发：看板列表或日历操作调用 `editPlan(id)` 回填草稿和分发目标，再跳转 `/wealth/create`。父应用不提供编辑链接，不引入 `planId` URL 参数；编辑态沿用原有内存机制。
- 新建与修改共用页面，但侧栏「创建计划」先调用 `resetPlanForm()` 清空名称、场景、排程及分发目标，再跳转；即使已在修改页也执行重置。离开表单页同样清空状态，避免取消、发布或切换页面后再次新建沿用旧内容；修改入口仍由 `editPlan(id)` 回填。回归入口为 `Create/draftLifecycle.test.tsx`。
- 同路由新建也会更新 `planFormVersion`，重新挂载分发目标选择器，避免行长人员列表清空后不再查询，或中台保留旧支行路径/搜索条件。旧人员请求通过序号失效；发布返回仅在表单代次相同时回填状态并允许跳转，已切换表单时只更新规划列表，发布失败保留当前草稿供重试。回归入口还包括 `planFormIsolation.test.ts`。

## 财富规划经营方向输入框边界

- 创建计划第 2 步的输入框位于 `console/src/pages/WealthWorkbench/Create/index.tsx`，样式为同级工作台 `index.module.less` 的 `.direction input`。默认使用常显边框和白色底色，悬停、聚焦时加强边框；排查输入框难以识别时，先检查默认边框是否被设为透明，以及覆盖样式是否影响键盘焦点轮廓。

## 财富规划 Java 网关迁移

- 2026-10-20 浏览器规划路径统一增加 `/zt` 前缀；需同步网关 location 和测试夹具。Java 上游仍是 `/wealth/plans` 时由网关剥离 `/zt`；Java 同时有此前缀时原样转发。不修改工作台页面路由或其他接口前缀。

- 规划请求在 `console/src/pages/WealthWorkbench/api.ts` 使用统一 `request("/zt/wealth/plans")`，由全局配置拼接为 `{API base}/api/zt/wealth/plans`；规划业务码仍在适配层解包。网关负责转 Java；创建后的任务编排、资源处理与状态回写由 Java 负责。
- 列表通过 Query 传 sapId/role/bbkId；创建/更新通过 camelCase JSON 传身份和业务数据；删除通过 DELETE Body 传 sapId。响应解包 code/message/data，HTTP 200 的非 200 业务码也上抛。
- 场景、名单、统计、机构人员和支行树同样通过统一 request 拼接 API base 和 /api 前缀；仅 `/expert/get-sign` 按接入约定保留原生 fetch 与原路径；不回退 Python 工作台 handler。网关必须优先匹配规划接口并透传 DELETE Body，避免 SPA 返回 HTML。具体评估与配置见 [Java 网关接入](../../docs/api/wealth-plans-java-gateway.md)，回归入口为 `planGateway.test.ts`、`store.test.ts`。

### 财富工作台多渠道 iframe 消息入口

- 父应用 iframe 地址为 `/wealth/board` 或 `/wealth/create`（部署在 `/console` 下时加此前缀），不带 `origin=Y`。父应用在每次 iframe `load` 后发送 `USER_DATA`，使用子页面的明确 origin 作为 `targetOrigin`；不新增 READY 握手。
- 消息 `data.source` 必须为非空渠道字符串（本次为 `RCZT`，后续渠道直接传自身标识，无需修改子页面），`sapId`（兼容 `userId`）传非空字符串以保留前导零；传入 `userName`、`bbkId`（兼容 `bbkOrgId`）、`positionId` 以及业务需要的 `orgCode`、`subBranchId`、`orgLvl`、`sysId`。岗位缺失或未识别仍按原权限矩阵显示暂无访问权限。
- `hideLogo: true`（兼容字符串 `"true"`）隐藏头部、播报与侧栏，内部导航持续生效，不补 URL 参数。身份和布局由消息提供；接口鉴权仍使用现有登录 Cookie，消息传参并不替代登录态。
- 子页面在注册监听器时清除该入口残留的 iframe 上下文，收到父窗口的有效 USER_DATA 消息后才挂载工作台并加载业务数据。不查询用户信息、不初始化 Agent、不同步 cron-auth、不启动客户信息定时刷新。未收到有效消息时保持等待；每次 iframe 加载后由父应用发送一次初始化消息；切换用户或岗位时重新加载 iframe 后再发送。
- 规划创建/更新请求体的 `sourceId` 使用消息传入的渠道（例如 `RCZT`）。原 `origin=Y` 财富入口继续使用 Cookie 初始化及 `RMASSIST` 来源，其他已有页面的 USER_DATA 行为不变。
- 代码入口：`console/src/utils/iframeMessage.ts`、`console/src/main.tsx`、`console/src/pages/WealthWorkbench/index.tsx`。回归入口：`iframeMessage.test.ts`、财富 `index.test.tsx`、`planGateway.test.ts`。

父应用示例（监听器先注册，再设置 iframe 地址）：

```js
const frame = document.createElement("iframe");
const childOrigin = "https://实际子应用域名";
frame.addEventListener("load", () => {
  frame.contentWindow.postMessage({
    type: "USER_DATA",
    data: {
      sapId: "00800001",
      source: "RCZT",
      hideLogo: true,
      userName: "张三",
      bbkId: "100",
      positionId: "RB0101",
      orgCode: "ORG001",
      subBranchId: "SUB001",
    },
  }, childOrigin);
});
frame.src = `${childOrigin}/wealth/board`;
document.body.appendChild(frame);
```
