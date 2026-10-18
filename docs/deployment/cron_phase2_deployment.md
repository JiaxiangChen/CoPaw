# 第二阶段定时任务：人工部署与 Postman 验证

本材料针对当前本地第二阶段代码，不表示已经部署或真实联调通过。三服务共用数据库；新任务不写 jobs.json 或旧 `_states`，存量接口和存量平台入口不切换。智拓实际接入属于第三阶段。

## 文件与执行顺序

1. 数据库负责人先在实际共库执行只读的 [结构核对 SQL](cron_phase2_schema_audit.sql)，保存数据库版本、缺列、列类型/精度、索引和引擎结果。
2. 缺整表时选择执行 [完整建表 SQL](cron_phase2_manual_create.sql) 中相应语句，每列与每表都有中文 COMMENT。它覆盖两张第一阶段基础表和七张新协议表，不是“再建九张不同的表”。已有表不会被 CREATE IF NOT EXISTS 自动升级。
3. 已有草案按 [人工升级 SQL](cron_phase2_manual_upgrade.sql) 逐条核对。不要一键运行全部 ALTER；同名列/索引要核对类型与定义。非空执行表缺冻结模型、顺序或水位时，先导出真实结构和相关数据再制定回填，不能虚构历史事实或删表重建。
4. 再运行结构核对，缺列结果必须为空；所有新时间为 DATETIME(6)/UTC，唯一键、自增索引与完整 CREATE 一致。静态测试只检查材料和代码契约，不替代 MySQL 实际 DDL/事务验证。
5. 部署 SWE 和 Scheduler；确认新后台已准备且正常启动，再从一个专用测试用户、少量目标、暂停批调度开始。不要对真实全量任务直接试运行。

SQL 为人工交付文件，应用不会读取执行。执行前由数据库负责人核对 MySQL 版本、InnoDB、utf8mb4、索引最大长度及行格式；四段身份组合索引需支持相应字节长度。不要在旧表上盲目整体改字符集或排序规则。不存在生产实测证据时不能宣称数据库验证通过。

## 表的分工

| 表 | 用途及主要写入者 |
| --- | --- |
| swe_cron_jobs_v2 | 任务定义；Scheduler 写新任务，第一阶段导入仍是 legacy，不自动接管。 |
| swe_cron_job_states | 共用管理/运行状态；Scheduler 写管理控制，SWE 写受理运行结果，分字段更新。 |
| swe_cron_registration_journal | CRUD、启停、三态等持久操作与平台步骤；Scheduler 写，失败可查原操作。 |
| swe_cron_execution_runs | 每次执行快照、去重、租约及主结果；Scheduler 登记/派发/settled 收敛，SWE 写真实运行结果。 |
| swe_cron_managed_broadcast_tasks | 每轮异步分发、源快照与批入口同步计划；Scheduler 写。 |
| swe_cron_managed_broadcast_items | 固定接收人、子任务身份、散列和逐项结果；Scheduler 写。 |
| swe_cron_managed_dispatch_controls | 整组调度目标及配置版本；不代表每个对象都已切换成功。 |
| swe_cron_managed_mode_items | 三态逐对象同步进度，保留成功/失败，支持混合状态。 |
| swe_cron_managed_trigger_receipts | 实际收到的平台触发及固定成员/跳过回执，重放不补生工作。 |

七张既有依赖表不是本次新建：swe_cron_jobs、swe_cron_executions、swe_cron_dispatch_scope_leases、swe_cron_dispatch_worker_capacity、swe_cron_dispatch_intents、swe_cron_dispatch_model_worker_policy、swe_cron_dispatch_worker_strategy。具体必需列由结构核对 SQL 列全。

智拓使用 Workflow 时还依赖既有的 swe_workflow_bindings 和 swe_workflow_binding_versions，分别保存技能绑定及不可变发布版本。因此人工核对材料共覆盖 18 张表（新后台启动必检 16 张，加 Workflow 的 2 张），不是本次新建 18 张。绑定表缺失时对照既有 [Workflow SQL](../../scripts/sql/cron_workflow_bindings.sql)，由数据库负责人核对其中每条语句后人工处理；本次不重复新建同义表、不自动迁移配置，既有字段长度约束仍须核对。

旧 swe_cron_jobs 继续保留新任务的兼容投影，不修改原 SWE/Monitor 写入逻辑；旧 swe_cron_executions 使用既有列和主键，不新增字段。原 swe_cron_broadcast_tasks/items 不作为新版队列，不让旧后台领取新版工作。

## 部署前置核对

- 网关把 `/api/scheduler/cron/jobs` 与新版 `/api/scheduler/cron/ordinary/callback`、`/batch/callback` 转发 Scheduler；旧入口仍指向原服务。Console 新页为 `/scheduler-cron-jobs`，原页 `/cron-jobs` 不变。
- `SCHEDULER_SWE_API_BASE_URL` 为 Scheduler 可达的 SWE/网关根地址；`SWE_SERVER_DOMAIN` 为平台可达且能路由新版回调的根地址。使用现有 `SWE_CRON_SCHEDULER_*` 平台配置，不新加必配内部 Token。连接参数不全时不能创建真实启用定时器验收。
- 沿用可信网关身份：X-User-Id 为真实操作者，X-Tenant-Id/X-Source-Id/X-Agent-Id 为目标任务。角色由网关核验，不手工伪造 manager/admin。三服务共库不代表工作区/会话/产物共盘，必须验证同一身份路由到可访问实际资源的 SWE 实例。
- 外部平台须实证稳定触发日志 ID 或明确计划时间、实际平台 jobId、jobParam 透传和 Header/身份路由。平台未确认时区字段，本地 payload 沿用既有协议；本轮至少验证 Asia/Shanghai，不能据此承诺任意时区的平台触发正确。
- 2026-10-17用户确认平台没有注册查询接口：无已保存平台ID按注册失败处理，错误码platform_registration_failed，原操作人工retry再次新增；已有ID只续办后续步骤。可能留下重复/孤立定时器，删除只能关闭已知ID，验收时须知晓此风险，不再等待平台查回协议。

## Postman 准备

导入 [对接 Collection](../integrations/zhituo-scheduler-cron-api-v1.postman_collection.json)，配合 [完整接口契约](../integrations/zhituo-scheduler-cron-api-v1.md)。不要使用 Runner 一键执行全部请求，里面包含删除和真实平台写入。

设置 scheduler_base_url、swe_base_url、user_id、tenant_id、source_id、agent_id 等真实变量；基地址不含末尾 `/api`。所有 JSON 请求使用 Content-Type: application/json，并沿用现有登录认证。scope 可不传，传则必须与 tenant/source 一致。

每个新的业务动作换 request_id；同一次动作因网络不明重提必须保留原 ID、原 Body。保存 job_id、operation_id、task_id、execution_id，不混用。版本每次取最新查询：内容用 definition_version，启停/优先级用 management_version，三态用 GET dispatch-state 的 version。

## 第一轮：无执行的管理验证

1. POST `/api/scheduler/cron/jobs` 创建一个 `task_type=text` 的专用测试任务，`dispatch_state=batch_paused`，正文和 Cron 取无业务副作用值。创建成功后保存响应中的任务与操作 ID，GET 详情确认 owner=scheduler、management_status=ready、固定 chat/session 已有。
2. 原请求号和 Body 再提交一次，确认相同任务/操作，不多注册平台绑定；同请求号改正文应冲突。首次网络不明不要换新请求号。
3. 在尚未分发前，以最新 definition_version PUT 任务，确认新正文、定义版本增加，chat/session 不变；旧版本再次修改应冲突。若平台失败，查询原 operation 并人工重试，不创建第二项任务。
4. GET 列表与详情，只能看到新管理任务，legacy 快照不出现；下次时间为按当前定义计算的带时区展示时间，不是平台生效证明。包含已删除、游标分页、其他用户/source/Agent 隔离分别验证。
5. PUT `/{job_id}/enabled` 停用/恢复，核对自身 enabled/pause 与 management_version；模式仍 batch_paused。POST read 只标读，不解除暂停或恢复启用。
6. 经正常服务重启后重新 GET，定义、操作、版本、chat/session 仍一致。仅从 API 无法证明具体 SQL 写入，但跨实例/重启的一致查询可验证持久行为；可由数据库负责人按同一完整身份独立核表。

## 第二轮：分发与三态

1. POST `/{job_id}/broadcast` 选择 1～2 个真实授权测试接收人，携带散列字段，不带调度模式。202 只代表受理，保存 task_id。
2. GET `/{job_id}/broadcast/tasks/current` 找最近一轮，再 GET `/{job_id}/broadcast/tasks/{task_id}` 查询进度和全部结果游标。终态后查询 children，确认真实接收人身份、固定子 ID/资源、实际散列；存在未删除子任务时源正文修改应冲突。
3. 再发同样接收人且使用新请求号，子任务正常复用 ID/资源；真正失败在新一轮处理。用 targets=[] 验证只调整全部未删除既有子任务排程，包括停用任务，不改正文/模型/启停，不重新准备资源。
4. PUT `/{job_id}/dispatch-state` 设 batch_enabled，用 GET 返回的 version；查询目标、生效方式、逐项计数。切换中不得换目标，失败可对原 operation retry，不能把 sync_status=failed 当作全组不能触发。
5. 在可控预发布故障中令一个对象平台操作失败，验证 effective_state=mixed：已生效批对象走新批入口，未切换普通对象仍走普通入口，同逻辑期不重复执行。失败提示保留，修复平台后人工 retry。
6. 切 batch_paused，只阻止新增批触发，已入队和执行中的工作继续；切 ordinary 先逐对象启用普通绑定，全部完成才关闭组批入口。普通新入口只用于新任务，不修改存量平台指向。

## 第三轮：实际执行和结果

先在人工确认的新平台绑定上触发，不能拿旧任务平台 ID 或随造 operation_id 试验。推荐由平台实际到时回调验证；必要时平台负责人提供该绑定的真实 jobParam、jobId 和计划时间后用 Postman 重放。

新版 callback Body 可以使用平台 jobParam 封装，也可以显式传 job_id、external_job_id、operation_id、callback_protocol=scheduler_managed_v1、trigger_id、scheduled_fire_at。scheduled_fire_at 是原排程逻辑期；fire_time/trigger_time 是实际平台排程时间，散列/批提前时二者不同。必须在真实时间窗口内测试，不把当前时间随意当计划时间。

1. 一次普通/批实际触发返回 202，保存返回的 execution_id 或批成员引用。重复相同平台日志/逻辑期，复用回执和执行身份，不追加执行。批入口各条按冻结 not_before_at 等待自己的时间。
2. 单任务 POST `/{job_id}/run` 另用手动请求号，GET `/{job_id}/executions/{execution_id}` 观察 queued→accepted→running→终态；停用/运行中的任务拒绝新手动请求。手动执行与自动逻辑期独立。
3. SWE 直接写共库状态和历史，不等待最终回传 Scheduler。HTTP 成功不等于业务成功，HTTP 超时也不证明未受理；按相同执行 ID 查询，不重新生成执行。
4. 主 status=success、settled_status=null 时继续等待已有异步子任务；最终 settled_status=success/error 反映整轮，不覆盖 SWE 主状态。不因异步未确认提前释放模型池或删除运行资源。
5. 自动结果未读增加；打开新任务 Chat/报告按新接口标读。已读不恢复；恢复仅清已有边界，之后完成结果仍未读。新旧任务 Chat 列表旧在前、新在后，动作不串源。
6. 重启验证只恢复已受理工作与结果提交，不重跑已发生业务，不补造漏收到的平台周期。失败、超时、取消和结果不明分别保留安全错误码。

## 第四轮：删除、新旧隔离和异常

- DELETE 新任务先逻辑删除，含删除查询仍能看到记录及原清理操作；未开始工作跳过，在途继续，整轮结束才清资源。源删除不自动删除其全部子任务；批量删除显式选择并逐项检查结果。
- 用源管理者真实 Header 管理关联子任务，只改变目标 workspace，不把操作人换成接收人；接收人不能枚举或删除其他接收人的任务。
- 更新期间平台已引用新操作但管理日志未完成：本次合法触发应持久 skipped，更新完成后重放不补跑。分发批入口更新响应丢失时，持久计划和已知绑定可继续有效触发，查询仍提示同步失败。
- 新接口 400/404/409/503 不回退旧写接口；模拟单来源列表失败，另一来源可更新，失败来源已知任务不被当删除。旧 SWE CRUD、旧批入口、原任务页面各做一条正向回归。
- 验证新任务没有 jobs.json/旧 _states 条目仍可查询、执行和标读。会话/报告文件仍可正常产生，这是执行产物，不等于重新启用任务文件存储。

## 上线监测、判定与回退

负责人由部署方指定；建议在专用用户完成一次完整自动计划期及其异步整轮后，再扩大接收人。验证窗口覆盖触发、排队、运行、结果、通知和重启，不只检查接口 200。

日志按 job_id、operation_id、task_id、receipt_id、execution_id 关联，检查“新版定时后台已启动”“平台触发已持久处理”“新定时任务结果提交结束”“整轮”相关安全日志。不要要求打印凭据、任务正文或原始敏感异常。

健康信号：重放不重复；accepted 有持久记录；三态失败计数可查且有效对象仍触发；模型池不超限，已结束整轮释放占位；无新版记录被旧处理器领取。异常信号：后台异常退出、持续 dispatch_failed、主成功长期不收敛、错误关联历史、清理长期等待或跨身份资源不一致。

出现重复执行、越权、错误绑定、丢结果或共库事务错误，应停止扩大范围，暂停确认的新批入口并停用相关新普通任务，保留全部表和操作记录。不要清表、删日志或用旧接口接管新任务；不能直接回滚到不识别新执行协议的版本而期望其处理已有新队列。先让在途结果和待清理事实收敛，再由负责人确定应用版本回退与恢复步骤，旧任务入口保持原样。

当前未完成目标业务MySQL、网关、外部平台及多实例资源路由验收。已另用独立本地MySQL 8.1执行结构及事务检查，详见[本地MySQL记录](cron_phase2_mysql_local_verification.md)；这不替代目标版本及存量数据核对。注册查回协议不再是前置条件，改按用户确认的失败/人工重试规则，但重复定时器风险仍需明确。

## 开发环境可重复检查

`tests/integrated/scheduler/test_managed_cron_http_chain.py` 在本机使用真实路由、HTTP客户端、资源准备器和共库SQL串联创建/更新/执行/删除、分发/三态及技能绑定→新Workflow创建。数据库为 SQLite 测试适配，平台、工作区资源、业务引擎及 Monitor 终态是明确边界替身，不代表实际服务验收。

真实 MySQL 测试为 `tests/integrated/scheduler/test_managed_cron_mysql.py`。仅在显式提供 `CRON_MANAGED_TEST_MYSQL_JSON`，且 database 符合 `test_cron_managed_...` 隔离库名时连接；不读取应用默认连接、不执行DDL。测试写入随机命名空间并只清理该命名空间，覆盖两表事务回滚、并发请求幂等与跨SWE/Scheduler连接的受理和结果单次写入。需先人工准备完整测试表及既有依赖；不要使用生产或预发布业务库运行该自动化测试。此变量只用于开发测试，不是应用新增配置。

本轮在独立本地MySQL 8.1上显式配置后，10项检查均通过，含6项实际数据库场景；无配置运行仍会跳过这6项，不应计为数据库通过。行内仍以本文Postman流程为验收入口，不要求调用方运行Python；测试代码为开发侧提供可复现的验证方式。
