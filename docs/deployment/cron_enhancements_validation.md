# 新版定时任务四项优化：部署与 Postman 验证

## 1. 发布范围与前置结构

发布 Scheduler、SWE、Console。Monitor 不需要因本次功能修改配置或新建表，但应回归新版批看板。

不新增环境变量、内部 Token、业务表或自动 DDL。原有 Scheduler→SWE、平台地址及共享数据库配置继续使用。先由数据库负责人核对并执行 `cron_enhancements_manual_upgrade.sql` 的缺失列/索引，再发布代码；应用不会执行该 SQL。

新增列共五个：

| 表                      | 列                                            |
| ----------------------- | --------------------------------------------- |
| swe_cron_jobs_v2        | run_start_date、run_end_date                  |
| swe_cron_job_states     | date_window_paused、date_window_sync_required |
| swe_cron_execution_runs | dispatch_lane                                 |

新环境完整建表材料同步在 `cron_phase2_manual_create.sql`；只读缺列检查使用 `cron_phase2_schema_audit.sql`。已建过的表不会因 CREATE IF NOT EXISTS 自动补列，不要删除重建。历史无日期的记录默认不限日期，既有执行默认为 normal。

SWE 和 Scheduler 都升级后再开放年份及日期功能。旧五段接口、旧页面、任务 ID、会话 ID和现有平台绑定 ID 保持原样，不回写 jobs.json 或旧内存状态，不改 swe_cron_executions 结构。

## 2. Postman 公共约定

`{{scheduler}}` 为行内 Scheduler 的网关根地址，不带末尾 `/api`。用户管理接口沿用原可信 Header：X-User-Id、X-Source-Id；按原环境填写 X-Tenant-Id、X-Agent-Id、X-Scope-Id 和可信角色头，不用新增 Token。

创建/暂停/日期配置的 request_id：同一动作超时重提原值及原 Body；改内容或新动作换新值。日期维护接口不传 request_id 或用户 Header。

所有示例日期只是格式示例，验证时按当前北京时间替换。维护接口不接受人工日期、角色、任务正文或任意历史执行期。

## 3. 创建时设置日期与年份

`POST {{scheduler}}/api/scheduler/cron/jobs`

```json
{
  "request_id": "enhancements-create-001",
  "dispatch_state": "ordinary",
  "spec": {
    "name": "日期及年份验证",
    "enabled": true,
    "task_type": "text",
    "text": "本地验收结果",
    "schedule": {
      "type": "cron",
      "cron": "0 9 * * MON 2026-2029",
      "timezone": "Asia/Shanghai"
    },
    "run_date_range": {
      "start_date": "2026-11-01",
      "end_date": "2026-11-30"
    }
  }
}
```

保存返回的 operation.job_id 和 operation.operation_id。创建成功一般为 201；202 表示操作尚未完成，503 也可能已经保存操作，不能随意换 request_id 重建。

`GET /api/scheduler/cron/jobs/{job_id}` 核对：spec.run_date_range、spec.schedule.cron、task.is_paused/pause_reason、definition_version、management_version、state.next_run_times。

- 未传 run_date_range 或传 null：默认不限日期段。
- 开始日在未来：用户启用意图仍为 true，但 task 显示日期暂停，平台入口保持关闭。
- 半个日期、倒置日期或非法日期：400；不能静默当作不限。
- 年份支持单年、逗号列表、连续范围；五段继续不限年份。
- MON 例子的平台注册值应为 `0 0 9 ? * 2 2026-2029`，不能变成每天。
- 五段 `0 9 * * *` 的平台值仍为 `0 0 9 * * ?`；加年份时末尾保留原年份字段。
- 年份已结束、或未来不足三次：详情返回空/不足三条时间，不应为 503。

任务在日期外时手动执行也被阻止。日期内按既有 `/run` 入口验证 SWE 实际执行及通知；结果仍由 SWE 写共库，不需要向 Scheduler 回传 HTTP 结果。

## 4. 前端展示、父修改和子继承

打开 `/scheduler-cron-jobs`。

1. 列表前部及编辑详情中可见“可执行时间区间”，没有配置显示“不限日期段”。年份任务的自定义表达式必须完整回显，不退回默认 09:00。
2. 父任务“更多操作 → 修改可执行时间区间”读取最新详情及管理版本，支持修改和清空；已有子任务也能修改，不走正文完整更新入口。
3. 保存后刷新列表/详情，并打开子任务管理核对相同日期。子任务编辑区间为只读，提示继承父任务，不需要重新分发。
4. 修改父日期后再分发一个新接收方，新子任务同样继承；客户端直接向子任务日期接口写入应拒绝，子任务完整正文更新也不能清空继承日期。
5. 有活动分发时修改日期应返回冲突，防止旧分发快照产生不一致日期；等原分发结束后重试。

独立日期修改接口：`PUT /api/scheduler/cron/jobs/{job_id}/run-date-range`。

```json
{
  "request_id": "enhancements-dates-001",
  "expected_management_version": 8,
  "run_date_range": { "start_date": "2026-11-01", "end_date": "2026-11-30" }
}
```

版本填刚查到的 management_version，不是 definition_version。清空时 run_date_range=null；该字段不能省略。保存立即改变本地日期限制并同步本组配置，平台开关由下一节接口维护，保存成功不等于平台已经全部同步。

## 5. 用户在平台创建日期维护任务

`POST {{scheduler}}/api/scheduler/cron/date-ranges/apply`

```json
{ "source_id": "RMASSIST" }
```

不需要用户 Header。只放在原可信内网/网关访问边界内，不要作为公网可任意调用的管理入口。按来源配置单路调用，避免同来源重叠运行，也尽量与人工模式切换错开。

- 由用户决定执行时间和频率，Scheduler 不自动注册本维护任务，不增加内部日期定时循环。
- 接口同步执行一次并返回当次计数；不存在 call_id、断点游标、后台续办或历史时间点追赶。
- 核对 evaluated_date、scanned_count、date_paused_count、date_released_count、platform_updated_count、failed_count、failures。
- 解除日期标记不等于启用：人工暂停、未读自动暂停及 batch_paused 都必须保留。
- 清空日期后仍应同步解除旧的日期平台暂停；失败项保留简单待同步标记，但不会在内部自行重试。
- 重复调用可能重复下发启停，但不会因此新注册定时器，也不会生成错过周期的业务执行。
- 部分失败可返回 HTTP 200 和 failed_count>0，平台调用脚本应检查失败数，不要只把 HTTP 200 视为全部成功。失败明细有数量上限，完整对象查看服务日志。
- 超时不代表完全未处理。单次调用不提供跨实例抢占、请求去重、管理并发下的平台开关原子性或重放补偿；需人工复查并决定是否再次调用。

用“明天开始”“昨天结束”“包含今天”三类配置测试，无须改服务器时间。已运行的任务继续结束；范围外的提前触发、未开始执行不补跑。维护晚于首轮触发时不承诺补发，尤其要安排好零点任务和入口开启的先后。

## 6. 整组异步暂停

`POST {{scheduler}}/api/scheduler/cron/jobs/{job_id}/pause`

```json
{ "request_id": "enhancements-pause-001", "expected_definition_version": 1 }
```

与删除同样的 Body 和 operation 包装，首次返回 202。核对 logical_delete_applied=false、action=pause_group；不应删除任务或会话。

- 本地父子任务立即暂停，旧队列失效。只被 SWE 受理但没有实际开始的也跳过；运行中执行及其异步工作继续。
- 批组变为 batch_paused，不变普通调度；后台关闭已有入口，不为暂停新注册定时器。
- 子任务单独暂停不能关闭父组共享入口，不能影响兄弟任务。
- 查询：`GET /jobs/{job_id}/operations/{operation_id}`（完整前缀为 `/api/scheduler/cron`）。父组暂停未完成时，不能另行启用或修改成员。
- 平台局部失败后仍保持数据库暂停；`POST /jobs/{job_id}/operations/{operation_id}/retry` 续办原操作，后台只处理未完成对象。
- 管理操作仍在进行或存在未就绪/失败待续办对象时可能 409；先核对各任务管理进度。不要通过开启成员绕过管理互斥。必要时先用既有三态入口暂停共享批入口，再处理原失败操作。

## 7. 前批优先与异常重跑

正常批次按来源、Provider、模型池分别取任务：较早受理批次的当前可执行成员优先，批内原用户/分行/热度顺位保留。不是等整批完成后才启动后批。

- 同池触发 A/B 两个不同任务组。A 仍有可派发成员时优先 A；A 全部待执行成员已进入运行、或只剩暂未到期重试/受单任务并发限制的成员时，B 可使用剩余真实容量。
- 容量为 10、A 正运行 6 个时，B 可启动 4 个；不能为等待中的重试预留额外容量。
- A 的自动重试到期后在正常候选中重新优先，但不抢占已经运行的 B。不同模型池继续并行。
- 重试规则、等待间隔和不重试错误保持已有逻辑；不要为测试而修改生产 Worker 上限。
- 对已经结束的批次再次异常重跑：标记 urgent，受理提交后立即尝试派发；容量满则进入加急候选等待释放。不突破池容量、单任务并发、日期及暂停限制。
- 原批次看板仍显示该成员及最新执行，业务尝试次数增加，旧/新历史的 is_final 按原逻辑切换，不新造平台批次。

验证并发需要同模型池且足够持续的任务，纯文本任务可能太快，不适合肉眼观察并发。结合新批看板的模型/成员状态和 Worker 记录检查。通过任务执行查询可查看 dispatch_lane，通过追踪事件可核对加急原因。

## 8. 追踪及上线后观察

`/monitor/cron-task-tracking` 当前属性显示区间及日期标记；时间轴显示真实提交时间的整组暂停、日期修改及带派发通道的执行事件，不伪造中间时刻。

预发布及上线后的首个日期维护周期、首批任务和首轮异常重跑由部署/调度负责人观察：

- 搜索 Scheduler 日志“日期维护调用失败”“日期维护未完成”“整组暂停平台同步失败”“加急重跑首次派发未确认”；按 source_id、job_id、operation_id/execution_id 关联。
- 健康信号：维护失败数为零；父子区间一致；日期外没有新业务实际开始；池容量不超限；前批待派发优先而后批可用空闲；原批看板仍可看到重跑。
- 异常信号：ready=false/缺列、维护持续超时或失败数非零、日期外开始、超过容量、遗失父子范围或结果关联。先暂停对应平台入口，核对日志和手工审计，再按原操作续办或回退服务；不删表、不清执行记录。
- 回退代码时保留新增可空/有默认值的列，不把既有年月、日期和加急历史批量改成虚构旧值。真实 MySQL 性能、行内平台年份注册和实际通知必须在环境中验收，本地结果不能替代。
