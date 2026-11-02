# 批调度看板（新）上线与 Postman 验证

## 交付边界

独立入口 `/monitor/cron-batch-dispatch-new`，旧批调度监控、旧查询/重试接口和旧队列不切换。Monitor 查询共库，Scheduler 受理人工重试并按下述规则自动重试新版批成员，SWE 在实际执行的原事务写状态和事件；没有最终结果回调，不扫描 jobs.json。

## 2026-10-22：批成员自动重试

- 无新增表、字段或配置。部署 Scheduler 和 Console；SWE/Monitor 沿用已有执行与查询协议。
- 每个批成员最多三次业务尝试（首次加两次自动重试），明确失败后等待五分钟；主执行失败、子任务失败及失联收敛失败均适用。异步子任务从开始执行起超过 7800 秒未完成时，按旧版规则超时重新入队。
- 鉴权过期、`workflow sapId does not match target user`、`技能任务结果不可用` 不自动重试；保留人工重试。停用、删除、自动暂停、定义版本已改变及队列已被作废的旧尝试不自动复活。
- 首次显示“重试次数：0”，第二、三轮显示 1、2，不再显示投递次数。投递仍复用同一 execution_id，退避为 5、10、20、30、60 秒，此后固定 60 秒。
- 新执行保留原批次、成员、计划时间、顺位、真实操作者；新结果写入后由 SWE 原事务把前一历史 is_final 改为 0，新历史保持 1。
- 上线会处理共库中已有、当前失败且未达上限的有效新版批成员，不限于上线后产生的失败。旧批调度队列与独立普通/手动任务不变。
- 超时/结果未知后重新执行可能重复产生外部动作，这是本次明确沿用旧版的语义，不宣称 exactly-once。

### 预发布验证

1. 创建新版批调度任务，让成员产生可重试失败：看板应变为重试次数 1，约五分钟后执行；连续失败最多出现三个不同 execution_id，且仍是同一成员。
2. 分别构造三类终止错误：看板保留失败，超过五分钟不自动产生下一轮。
3. 主任务成功但子任务失败应重试；子任务未完成时不提前释放为成功，超过 7800 秒才允许超时重试。
4. 重试入队时旧历史 is_final 仍为 1，新结果落库后才替换；两实例与人工点击并发不得生成重复下一轮。
5. 停用/删除/自动暂停后不自动重试，普通独立任务与旧看板行为不变。

### 上线观察

由部署验证人员在上线后至少观察一轮重试窗口（15 分钟）；检查看板重试次数、调度事件 `auto_retry_scheduled` 和执行历史。异常信号为超过两次自动重试、五分钟之前运行、终止错误仍重试或同成员并发产生重复尝试。回退 Scheduler 可停止新增自动重试，但已入队工作仍可能继续执行，需核查并按既有任务控制处理；不清表、不伪造成功。

新增两张表：

| 表 | 用途 |
| --- | --- |
| `swe_cron_managed_batch_members` | 固定批次成员与当前业务尝试指针，保留原计划顺位；不复制执行状态。 |
| `swe_cron_managed_dispatch_events` | 实际生命周期事实、逐项人工重试决定、请求幂等摘要和响应审计。 |

既有 `swe_cron_managed_trigger_receipts` 增加批次唯一索引和来源/时间查询索引。既有 `swe_cron_executions`、旧 Intent/事件/批次表不加字段。第一阶段及第二阶段原有建表材料仍然是前置依赖，本增量文件不替代它们；本地完整测试结构共20张依赖表，不是新增20张表。

## 人工 SQL 与升级顺序

1. 核实目标 MySQL 版本和 `SHOW CREATE TABLE`，备份。回填材料要求 MySQL 8.0.21+ 的 `JSON_TABLE`，未验证 MySQL 5.7，不可直接套用。
2. 执行 [两张增量表 CREATE](cron_batch_board_manual_create.sql)，中文表注释和列注释已包含。已有同名表时逐列核对，`IF NOT EXISTS` 不负责补列。
3. 按 [回执索引升级](cron_batch_board_manual_upgrade.sql) 检查重复值及现有索引，再执行必要 ALTER。不要重复创建同名/等价索引。
4. 在维护窗口暂停该来源新版触发与人工重试，等待已有受理事务结束；仍在执行的 Agent/Workflow 不要求取消。按 [历史成员回填](cron_batch_board_manual_backfill.sql) 替换真实 source，逐段检查。只处理新版已接受的真实批回执；普通入口先创建、后来被批次引用的执行也会回填。不能以运行 kind=batch 推断成员。
5. 回填前检查必须0行，事务内缺失关系核对也必须0行后再手动 COMMIT；异常则 ROLLBACK、排查，不覆盖已推进的重试指针。零成员回执合法，不伪造成员或成功率。升级前没记的事件不补造。
6. 部署 SWE/Scheduler/Monitor 与 Console 的配套版本，确认新版后台就绪、三服务同库后恢复入口。表必须先于新代码就绪；新执行的事件和业务事实同事务，缺表不能正常受理/提交结果，不能把它当成可忽略的监控功能错误。
7. 新查询缺表或关系不完整返回503，不回退旧表。已有新版批回执没有完成成员回填时，新看板会明确不可用，不能把503解释为没有任务。

回退代码时保留新表、回执和事件，不执行 DROP/TRUNCATE，不撤销已受理的队列。回退不等于取消人工重试产生的工作；有在途新业务尝试时先核对兼容执行能力再回退。此变更没有修改平台入口协议和注册查回策略。

## Postman 通用设置

使用网关入口，配置 `monitor_base`、`scheduler_base`（含 `/api`）和 `source_id`。真实认证方式沿用现有网关；示例不是让客户端自行授予管理员权限，网关必须认证并约束角色/身份 Header。

```text
X-User-Id: <真实管理员用户>
X-Tenant-Id: <真实用户所属租户>
X-Source-Id: {{source_id}}
X-Agent-Id: default
X-User-Role: manager 或 admin（由可信认证链路确认）
Content-Type: application/json
```

原有 scope Header 若继续发送，应与实际 tenant/source 一致；不要从目标成员改写操作者。无需另加 SWE_INTERNAL_TOKEN。测试建议只用专门创建的新链路任务和受控失败数据，不修改生产成员制造失败。

## 查询和权限验证

### 1. 列表与全局统计

`GET {{monitor_base}}/monitor/cron/managed-dispatch/batches?page=1&page_size=4`

可选 `start_time`、`end_time`、`status`、`query`。带时区 ISO 推荐，例如 `2026-10-17T00:00:00+08:00`（在 Params 中输入，避免加号未编码）。无时区输入按北京时间解释；输出为带时区 ISO。状态为 received/pending/running/completed/failed。

检查：页大小变化不改变整范围 stats；关键词匹配 Batch/任务/租户/Agent/模型而不截断整批统计；原普通/手动执行和分发轮次不混入批次；旧任务没有被写进新看板。零成员 completion_rate 为 null；主 success 且未 settled 时仍为 running，不冒充完成。dispatch_failed 是等待处理，不算已发生的业务失败。

### 2. 详情和独立分页

`GET {{monitor_base}}/monitor/cron/managed-dispatch/batches/{{batch_id}}?member_page=1&member_limit=50&event_page=1&event_limit=50`

成员筛选 `member_status`、`member_role`（parent/child）、`member_query`；事件筛选 `event_type`。改变成员页/筛选不改变 batch 整批统计，也不重置事件页。真实字符串 execution_id 原样保留；看当前业务尝试与独立 settled_status。事件包含实际操作者和时间，不返回认证 Header、派发令牌、完整正文或原始异常。

### 3. 共享池与完整容量历史

`GET {{monitor_base}}/monitor/cron/managed-dispatch/workers`

有 `capacity_events_next_cursor` 时用同一来源和时间范围继续传 `capacity_cursor`，直到 null。共享容量仍包括新旧任务，不是新任务专属；容量记录输出明确东八区时间，新执行事实输出 UTC。页面中途失败显示“容量历史不完整”，不会把前几页画成完整曲线。

### 4. 权限与错误

在可控环境验证普通角色403、错误来源详情404、缺身份400、非法页码/状态422、反向时间范围400。缺表和坏关系在专用测试库验证503，不在业务库删表验证。页面隐藏不是授权替代。存储错误不会泄露数据库异常或凭据。

## 人工失败重试验证

### 5. 获取候选

`GET {{scheduler_base}}/scheduler/cron/managed-dispatch/batches/{{batch_id}}/failures`

按需重复添加 `types=model_rate_limit` 等 Params。counts 为整个批次当前失败分类；items 为前200个匹配成员，matched_count/has_more 表示尚余匹配成员。分类多选只影响候选，不缩减分类总数；历史未保存原因显示其他错误，不补造原始原因。

每项带 member_id、execution_id、member_version、execution_version、definition_version、business_attempt。definition_version 是当前定义，execution_definition_version 是失败执行的旧版本。transport_only 表示只重投原请求。

### 6. 提交一次人工重试

`POST {{scheduler_base}}/scheduler/cron/managed-dispatch/batches/{{batch_id}}/retry`

```json
{
  "request_id": "postman-retry-20261017-001",
  "confirm_stopped": false,
  "candidates": [
    {
      "member_id": "复制预览member_id",
      "execution_id": "复制预览execution_id",
      "member_version": 1,
      "execution_version": 5,
      "definition_version": 1,
      "business_attempt": 1
    }
  ]
}
```

六个值必须复制真实预览，不直接使用示例数字；1～200项，不得重复，不提交正文/租户/权限。超时/结果未知/其他错误等危险分类需人工核实原执行及子任务已经停止，再把 confirm_stopped 设为 true；系统不会替你停止原工作。

返回200只表示共库受理与审计已提交，查看 queued/skipped/results，不能当成 SWE 业务已成功。新执行由原新版后台派发，不在重试 HTTP 请求中运行 Agent/Workflow。任务停用、删除、管理中、自动暂停、定义变化或候选过期时逐项跳过，不复活任务；存储异常整事务回滚或保留不确定性，客户端不得换请求 ID 盲目重跑。

### 7. 幂等、并发和历史

- 原 request_id + 原 Body 重提：reused=true，结果和第一次一致，执行记录数不增加；同 ID 改候选或确认值409。
- 响应丢失：原 Body 原 ID 重提核对；Console 在当前来源/管理员/批次下保留待核对请求，切页再回来也可原样重提。
- 两名真实管理员提交同一预览：只有一个推进当前业务尝试，另一个跳过；不能用同一个身份 Header 冒充两个人来证明权限链路。
- 业务失败：新 execution_id，business_attempt +1，逻辑成员数不变，原失败与事件保留；原 scheduled_fire_at 不变，not_before 是本次受理时间，冻结当前定义和当前发布的 Workflow 配置。
- 派发未确认且未受理：原 execution_id/原快照/尝试号不变，仅恢复运输投递；SWE 共库已受理的矛盾状态不会被当成明确失败业务重跑。
- 主 success 后异步收敛 error 仍允许显式新业务尝试；主状态与收敛状态不相互覆盖。
- 批入口暂停不阻止显式重试，不取消既有队列，也不修改普通/开启批/暂停批三态。单任务自身停用等执行条件仍然生效。

## 页面与联调验收

管理员打开新旧两个菜单，确认旧页仍只发旧请求，新页只发 managed-dispatch 请求；新页没有旧重试兜底。验证来源/用户切换、成员及事件筛选分页、200候选限制、停止确认、重复点击、错误提示和重试后的逐项结果。共享容量可展开曲线和调整详情，原 CSS/组件样式延续，不使用旧数字 Intent ID 承载新 UUID。

本地自动化及浏览器证据另见进度记录。浏览器边界 Mock 不代表真实网关、平台、SWE 多实例或 Agent/Workflow 已联调；上线前仍须用真实认证、稳定平台触发及业务执行完成以上正负场景。
