# 观测能力与支撑系统

本文档整理不直接位于主执行链路中心、但对可观测性、运维和系统完整性重要的支撑模块。

## 可观测性

| 区域 | 关键文件 | 说明 |
|------|----------|------|
| Tracing | `src/swe/tracing/config.py`, `src/swe/tracing/manager.py`, `src/swe/tracing/models.py`, `src/swe/tracing/model_wrapper.py`, `src/swe/tracing/sanitizer.py`, `src/swe/tracing/store.py` | 追踪配置、模型包装、脱敏和落盘 |
| Hook Telemetry | `src/swe/agents/hook_runtime/runtime.py` | Hook handler 实际执行时输出 `HOOK_TELEMETRY ` 前缀的单行 JSON 日志，用于日志采集侧分析；不写入 tracing span |
| Runtime Diagnostic | `src/swe/app/runtime_diagnostic.py`, `src/swe/app/pod_resources.py`, `src/swe/app/middleware/sse_diagnostic.py`, `src/swe/app/_app.py` | 按 Pod 输出 `RUNTIME_DIAGNOSTIC ` 结构化日志，无特权采集 SSE 并发、事件循环延迟、当前 Python 进程资源、当前容器文件句柄与 cgroup 磁盘 IO，以及 `/opt/deployments/app` 文件系统使用情况 |
| Token Usage | `src/swe/token_usage/manager.py`, `src/swe/token_usage/model_wrapper.py` | Token 统计与包装器 |
| App 侧心跳 | `src/swe/app/service_heartbeat.py`, `src/swe/app/crons/heartbeat.py` | 服务状态和实例心跳 |

## 调度与后台任务

| 区域 | 关键文件 | 说明 |
|------|----------|------|
| Cron | `src/swe/app/crons/manager.py`, `src/swe/app/crons/executor.py`, `src/swe/app/crons/coordination.py`, `src/swe/app/crons/api.py`, `src/swe/app/crons/models.py` | 定时任务管理、执行与协调 |
| Cron Auth Mirror | src/swe/app/crons/auth_mirror.py, auth_repository.py, auth_migration*.py | 文件权威凭据镜像与内部异步运维任务；见 docs/deploy/cron-auth-database-mirror.md |
| Cron Repo | `src/swe/app/crons/repo/base.py`, `src/swe/app/crons/repo/json_repo.py` | Cron 配置持久化 |
| Skill Readiness | `src/swe/app/skill_readiness/*.py`, `console/src/pages/Market/SkillReadinessModal.tsx` | 技能市场管理侧的用户可执行性检查，按 `skill_id` 聚合拥有用户、读取自检配置、启动异步检查并展示最近一次结果 |
| Instance | `src/swe/app/instance/service.py`, `src/swe/app/instance/store.py`, `src/swe/app/instance/router.py`, `src/swe/app/instance/models.py` | 实例状态与实例管理接口 |
| Backup | `src/swe/app/backup/*.py` | 备份任务、S3 客户端、批处理、Worker 与任务存储 |

补充说明：
- Cron 执行链路会先绑定 tenant context，再进入 runner / tool 层，因此 tenant root `security.process_limits` 会沿同一上下文传播到 shell 与 MCP `stdio` 子进程启动点
- 进程 CPU 时间/内存上限属于 launch-time 保护，和 Cron 自身的任务超时/调度超时是两套边界：前者限制单个子进程资源，后者限制整个任务 wall-clock 生命周期
- 定时任务会话历史清理属于外部调度平台上的 source 级系统任务，配置来自 `source_system_config.cron_task_session_cleanup`，外部任务 ID 存在 source 级 `.system_jobs/sources/.../system_jobs.json`，不会写入业务 `jobs.json`，也不会出现在“我的任务”。回调中的 `source_id` 是清理边界，会展开到该 source 绑定的所有逻辑租户；`tenant_id` 仅作为调度回调上下文保留，不代表只清理单个用户。
- 该清理默认关闭；管理员在当前 Source 系统特性配置页开启后，会刷新当前 Agent 的外部调度平台系统任务注册。
- 该清理只处理文件系统中的任务 session JSON 历史：按 `task_runs[].ended_at` 删除超过保留天数的历史 run、对应 `agent.memory.content` 片段和可判断时间的 `task_messages`；不清理 `swe_cron_executions`、Monitor、Tracing 或其它审计数据。
- 清理和定时任务结束写回共用 task session 写锁；如果拿不到锁，本轮跳过该 session，避免运行中的任务每天同一时间执行时永久无法清理，也避免并发写覆盖。
- 定时任务可通过顶层 `skill_ids` 绑定一个或多个技能 ID，Console 创建/编辑页支持手动输入并规范化为逗号分隔字符串；`swe cron create` 未传该字段时保持空绑定。Monitor 的 `swe_cron_jobs.skill_ids` 仅保存同步后的绑定，技能可执行性检查仍以 SWE 当前实例的 cron manager/list_jobs 作为执行态来源。
- 技能可执行性检查配置保存在 SWE 的 `swe_skill_readiness_configs`，按全局 `skill_id` 查询；市场技能没有 `skill_id` 时，Console 降级使用 `skill_name` 查询并在弹窗中提示。运行结果保存在 `swe_skill_readiness_runs`、`swe_skill_readiness_user_results`、`swe_skill_readiness_check_results`，结果 API 支持按用户聚合状态和单个检查项 fail 过滤，但返回用户的全量检查项。

## 基础支撑模块

| 目录/文件 | 说明 |
|-----------|------|
| `src/swe/envs/store.py` | 环境变量持久化 |
| `src/swe/tunnel/cloudflare.py`, `src/swe/tunnel/binary_manager.py` | Cloudflare 隧道支持 |
| `src/swe/utils/fs_text.py`, `src/swe/utils/logging.py`, `src/swe/utils/system_info.py` | 文件、日志与系统信息 |
| `src/swe/tokenizer/` | Tokenizer 词表和配置资产 |

## 运维与发布资源

| 目录 | 说明 |
|------|------|
| `deploy/` | Dockerfile、Entrypoint、Supervisor 模板 |
| `scripts/` | 安装、打包、迁移、测试、站点构建脚本 |
| `docs/superpowers/specs/` | 近期开发表设计文档 |

## 关联功能域

- 模型执行链路: [model-provider-and-local-runtime.md](model-provider-and-local-runtime.md)
- 多租户配置与路径体系: [config-and-tenant-isolation.md](config-and-tenant-isolation.md)
