# Cron 批调度与独立 Scheduler

本文说明广播任务切换到“批调度”后，外部调度平台、独立 Scheduler、SWE 和 Monitor 如何共同完成提前触发、按模型作用域排序派发、执行回执、补位和重试。

返回 [Cron 定时任务模块索引](README.md)。

## 一句话理解

普通调度是一条外部任务对应一个 SWE cron job；批调度则为广播源任务注册一条提前触发的物理外部任务，由独立 Scheduler 把源任务和所有广播子任务写成 dispatch intents，再按模型作用域的容量和阅读热度逐个回调各自所属的 SWE。

批调度不是后台扫描所有父任务。父批次仍由外部调度平台的物理 timer 触发；Scheduler 的常驻循环处理已入库 intent 的双状态汇总、重试、超时回收和补位，容量调整独立运行。

## 启用条件与模式切换

SWE 和 Scheduler 都必须启用：

```text
SWE_CRON_DISPATCH_INTENTS_ENABLED=true
```

SWE 侧还需要：

| 环境变量 | 用途 |
| --- | --- |
| `SWE_SCHEDULER_API_URL` | 注册批调度物理任务及回传 execution 使用的 Scheduler API 基址；默认 `http://localhost:9100/api` |
| `SWE_CRON_SCHEDULER_BASE_URL` | 外部调度平台地址 |
| `SWE_SERVER_DOMAIN` | 当前 SWE 可被 Scheduler 回调的地址，会随父任务注册信息传入 Scheduler |
| `SWE_INTERNAL_TOKEN` | SWE 内部回调鉴权 token；也会用于 Scheduler 回调 SWE |

Scheduler 侧常用配置：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SCHEDULER_HOST` | `127.0.0.1` | 独立服务监听地址 |
| `SCHEDULER_PORT` | `9100` | 独立服务端口 |
| `SCHEDULER_DB_*` | - | Scheduler/Monitor 共享 cron 表所需数据库配置 |
| `SCHEDULER_SWE_API_BASE_URL` | - | 没有任务级 SWE 地址时的回调基址 |
| `SCHEDULER_SWE_INTERNAL_TOKEN` | - | 回调 SWE 时使用的备用内部 token；Scheduler 优先读取 `SWE_INTERNAL_TOKEN` |
| `SCHEDULER_CRON_DISPATCHED_STALE_SECONDS` | `7800` | 已派发 intent 的失联回收阈值 |
| `SCHEDULER_OPENAPI_DOCS` | - | 是否开放 Scheduler OpenAPI 文档 |

Console 广播弹窗可以选择“正常调度”或“批调度”，批调度偏移窗口为 1-24 小时。后端接口是：

```http
POST /api/cron/jobs/{job_id}/batch-dispatch/enable
Content-Type: application/json

{"offset_window_hours": 4}
```

```http
POST /api/cron/jobs/{job_id}/batch-dispatch/disable
```

只有广播源任务可以切换模式；广播子任务不能作为切换入口。模式变更会领取广播任务锁，同一个源任务已有广播或模式同步任务运行时返回 409。

启用后：

- 源任务原有的普通外部 timer 被暂停。
- SWE 创建或复用名称带 `[批调度]` 的物理外部 timer。
- 物理 timer 的 cron 会相对父任务原 cron 提前 `offset_window_hours`；无法安全平移时保留原 cron、偏移记为 0，并在 meta 中记录 warning。
- 已知广播子任务的普通外部 timer 被暂停，之后由 Scheduler 统一派发。
- 后台同步会把模式传播到广播子任务；关闭批调度时恢复源任务和子任务的普通 timer。

主要 meta：

| 字段 | 含义 |
| --- | --- |
| `broadcast_dispatch_intents_enabled` | 当前源任务/子任务是否由 Scheduler intents 管理 |
| `batch_dispatch_external_job_id` | 批调度物理外部任务 ID；关闭后保留以便复用 |
| `batch_dispatch_offset_window_hours` | 提前触发窗口小时数 |
| `batch_dispatch_offset_minutes` | 实际提前分钟数 |
| `batch_dispatch_cron` | 注册给物理 timer 的 cron |
| `batch_dispatch_parent_cron` | 源任务原 cron |
| `batch_dispatch_cron_warning` | cron 无法平移时的原因 |

## 端到端链路

```text
外部调度平台
  -> POST /api/scheduler/cron/callback
  -> Scheduler 校验父任务和批调度标记
  -> 创建 batch，写入父任务 + 广播子任务 intents
  -> 按 source/provider/model 作用域领取容量
  -> 按 dispatch_order、id 稳定排序，due_at 只控制是否可领取
  -> POST <任务所属 SWE>/api/internal/cron/callback
  -> CronManager.run_job(is_manual=False, dispatch_meta=...)
  -> SWE 执行结束后 POST /api/scheduler/cron/execution
  -> Scheduler 校验派发身份并保存 Agent 结果
  -> Monitor 按原有流程更新执行记录的 async_status
  -> Scheduler 扫描当前 attempt 的 status + async_status
  -> 完成、重试或继续等待；释放名额后在本轮补位
```

Scheduler 对外的两个核心入口：

| 接口 | 调用方 | 用途 |
| --- | --- | --- |
| `POST /api/scheduler/cron/callback` | 外部调度平台 | 以父任务计划时间创建一次批次及 intents |
| `POST /api/scheduler/cron/execution` | SWE | 校验身份并保存 Agent 执行结果；最终状态由调度循环扫表判定 |

批次 ID 由父任务 ID 和计划触发时间确定，因此同一父任务同一计划时间重复回调不会创建不同批次。父任务和 active 广播子任务都会进入同一批次。

Scheduler 回调 SWE 时会带上：

```json
{
  "callback_source": "dispatch_service",
  "dispatch_intent_id": 123,
  "dispatch_batch_id": "cron:...",
  "dispatch_attempt": 1,
  "provider_id": "provider-a",
  "model_id": "model-a",
  "scopeId": "tenant-a-RMASSIST",
  "fromId": "tenant-a",
  "parent_scheduled_fire_at": "2026-07-16T09:00:00+08:00"
}
```

SWE 会把完整身份写进 execution meta 的 `cron_dispatch`。缺少 intent/batch/attempt 任一项的普通执行仍同步 Monitor，不会因为只有 B3 trace header 就被误送到 Scheduler。Scheduler 回调时会继续转发 B3 headers，保持链路追踪连续。

## 排序、作用域与容量

派发作用域是：

```text
source_id + provider_id + model_id
```

同一作用域通过数据库 lease 协调领取，`effective_workers` 表示可并行派发的容量槽位，不是操作系统进程数。

同一批次内领取与派发按已保存的 `dispatch_order`、`id` 稳定排序，`due_at` 只作为退避准入门槛。阅读热度参与初始顺序生成，来自最近 30 天成功异步执行的阅读记录，并对 2/3/4/5 小时内快速阅读增加权重；值上限为 9999。源任务本身也参与排序。

容量策略保存在数据库，可按模型作用域配置基线、最小、最大 worker 和调整策略。默认约每 300 秒调整一次：近期有错误时收缩，无错误时逐步增加；调度循环确认最终结果后的补位和周期容量调整是两件不同的事。

## 双状态扫表

Scheduler 每轮先汇总当前派发轮次的执行记录，再回收失联任务、计算空闲名额和派发；一轮结束后默认等待 60 秒。即使名额全部占满，结果扫描也会运行。

- `status='success' AND async_status='success'`：完成 intent。
- Agent 明确失败：保留原错误并按策略重试；固定的 Cron 鉴权过期错误首次即终止，不重试，并明确记录为“鉴权过期”。
- `status='success' AND async_status='error'`：以“子任务执行失败”重试。
- 没有当前轮次执行记录或子任务结果未确认：保持 dispatched，继续占用名额。

执行记录通过 `dispatch_intent_id = intent.id`、`dispatch_batch_id = intent.batch_id`、`dispatch_attempt = intent.attempt_count` 关联，并校验 job、tenant；不会读取同一 job 的旧轮次结果。扫描有批量上限，尚未处理的明确终态不会被失联回收或领取兜底重新执行。

Scheduler 必须访问 Monitor 更新的同一份 `swe_cron_executions`，直接沿用现有 `async_status` 聚合语义，不增加字段或回调接口。SWE 本地成功语义保持原样。Monitor 当前“无子任务即成功”等规则也保持原样。

## 重试与失联回收

- Scheduler 请求 SWE 失败时，intent 按重试策略重新进入 pending。
- 扫描发现失败结果时，如果还没达到最大尝试次数，默认从本次扫描时间起 300 秒后重试；Cron 鉴权过期错误除外。
- 默认最多尝试 3 次；达到上限后 intent 标记 failed。
- 已派发但没有最终结果的 intent，由 `SCHEDULER_CRON_DISPATCHED_STALE_SECONDS` 判定失联并回收或终止。Agent 执行和等待子任务共用默认 7800 秒预算，收到 Agent 回执不刷新派发锁时间。
- Agent 成功但子结果未确认时，超时错误为“获取子任务状态超时”；主结果未收到时保留原失联文案。达到重试上限后进入 failed。
- execution 回执使用 `(dispatch_intent_id, dispatch_batch_id, dispatch_attempt)` 去重。
- SWE 回传完整 dispatch 身份时采用同步请求并最多尝试 3 次，避免回执丢失后容量无法释放。
- Cron 鉴权过期属于配置故障，不计入 worker 容量调整的失败反馈。

## 数据表

定义位于 `scheduler/src/scheduler/app/database/schema.py` 和 `scripts/sql/cron_tables.sql`：

| 表 | 用途 |
| --- | --- |
| `swe_cron_dispatch_batches` | 一次父任务计划触发对应的批次 |
| `swe_cron_dispatch_intents` | 父任务和子任务的待派发/派发中/完成/失败状态 |
| `swe_cron_dispatch_events` | intent 状态变化与诊断事件 |
| `swe_cron_dispatch_worker_capacity` | 模型作用域当前有效容量 |
| `swe_cron_dispatch_scope_leases` | 跨 Scheduler 实例的作用域领取租约 |
| `swe_cron_dispatch_model_worker_policy` | 模型作用域 worker 策略 |
| `swe_cron_dispatch_worker_strategy` | 容量调整策略 |
| `swe_cron_executions` | execution 表增加 dispatch intent/batch/attempt 身份列及索引 |

## Monitor 查询

Monitor 提供批次看板接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/monitor/cron/dispatch/batches` | 按 source、时间、状态分页查询批次 |
| `GET /api/monitor/cron/dispatch/batches/{batch_id}` | 查询批次、intents 和 events 详情 |
| `GET /api/monitor/cron/dispatch/workers` | 查询模型作用域策略与当前容量 |

查询沿用 `X-Source-Id` 做 source 隔离。批次详情支持 `intent_limit` 和 `event_limit` 限制返回数量。

## 关键源码

| 文件 | 职责 |
| --- | --- |
| `scheduler/src/scheduler/app/routers/cron.py` | 父 timer 回调和 SWE execution 回执 API |
| `scheduler/src/scheduler/app/services/cron/scheduling_service.py` | Scheduler 循环、重试、容量和派发编排 |
| `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py` | 批次/intents 创建、排序、领取、状态转换 |
| `scheduler/src/scheduler/app/services/cron/execution_sync_service.py` | execution 持久化与派发身份查询 |
| `src/swe/app/crons/manager.py` | 模式切换、物理 timer 注册、dispatch 执行 meta |
| `src/swe/app/routers/internal.py` | 接收 Scheduler 回调并拒绝批调度任务的非 dispatch 自动回调 |
| `src/swe/app/crons/monitor_sync_client.py` | dispatch execution 同步 Scheduler，普通 execution 同步 Monitor |
| `monitor/src/monitor/app/routers/cron.py` | 批次、详情和 worker 查询 |

## 排查顺序

1. SWE 和 Scheduler 的 `SWE_CRON_DISPATCH_INTENTS_ENABLED` 是否都为 true。
2. 父任务 meta 是否有 `broadcast_dispatch_intents_enabled=true` 和 `batch_dispatch_external_job_id`。
3. 外部平台批调度物理任务是否 active，回调是否指向 `/api/scheduler/cron/callback`。
4. Scheduler 数据库是否能查到对应 batch 和 intents。
5. intent 的 source/provider/model 作用域是否有可用 capacity，lease 是否过期。
6. intent event 是回调 HTTP 失败、execution 失败，还是 stale 回收。
7. SWE execution meta 是否带完整 `cron_dispatch` 身份，execution 回执是否到达 Scheduler；当前 attempt 的 `status` 与 `async_status` 是否都已确认。
8. 模式切换后子任务是否完成后台同步；不要把已移除的启动全量扫描当成兜底机制。
