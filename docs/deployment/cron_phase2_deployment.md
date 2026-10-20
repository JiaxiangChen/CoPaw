# 第二阶段定时任务：人工部署与 Postman 验证

2026-10-20 四列增量：`swe_cron_jobs_v2` 新增 `tenant_name`、`bbk_id`、`skill_ids`、
`workflow_binding_id` 查询副本，完整定义 JSON 继续保留。
**先人工加列，再部署 Scheduler/SWE，最后按预览回填已有定义**；
详见[四列升级与验证](cron_profile_columns.md)。本增量不涉及订阅迁移或旧表结构调整。

本材料针对当前本地第二阶段代码，不表示已经部署或真实联调通过。三服务共用数据库；新任务不写 jobs.json 或旧 `_states`，存量接口和存量平台入口不切换。智拓实际接入属于第三阶段。

2026-10-19 更新：新版平台回调按实际固定 Body 适配，不要求 Header、平台 jobId、执行流水号或触发时间。用户明确每次回调都是新触发，不做流水号、计划期或普通/批跨入口去重；同一次已入队执行的派发重试和结果落库仍按原 execution_id 保持幂等。只更新 Scheduler 即可生效，本次不新增表、列或配置。

## 文件与执行顺序

新版批调度看板展示对齐需部署Monitor、Scheduler和Console，不改调度/重试写入，无SQL/配置增量。错误详情、中文优先信息、父任务冻结模型、批次结果提示及结构化Worker详情见[展示对齐验证](cron_batch_board_display_parity.md)。Owner项已按用户最新要求从新版详情头部移除，该删除展示操作只需更新Console，后端字段不变；入口暂停仍不阻止已入队执行。

2026-10-19失败详情与最终历史修复：部署SWE及Console，无Scheduler生产接口、SQL或配置变更。全部新任务失败信息按旧版异常说明语义写入既有历史error_message及状态last_error（先脱敏、最多200字符），不再仅支持WorkflowCallError，也不追加workflow诊断后缀；run.error_code仍是稳定机器码。新版批业务重试在SWE新结果事务内取消前一历史is_final，新行is_final=1；排队、运输重投及其他批次不提前改标记。新版列表error可hover查看安全详情。以下验证仅操作测试任务：

- 分别触发Agent/子任务失败、workflow缺少用户信息或密钥、授权过期、绑定配置失败，历史error_message和详情state.last_error应保留旧执行器的具体错误说明，而非仅cron_execution_failed。超过200字符截断，令牌/授权头/Cookie脱敏；不展开异常链，run.error_code保持原机器码。
- workflow受控HTTP失败（如502）沿用原执行器文案“技能任务执行失败”，不往文案拼接HTTP状态；分类和HTTP状态保留在原有workflow执行元数据中。
- 打开新版任务页hover error，显示同一安全摘要；成功/运行中不显示旧错误，缺失详情有明确提示，不新增查询请求。
- 对该批失败成员人工重试：202受理时旧is_final仍1；新结果历史真正写入后旧0、新1，重复提交同一请求/结果不新增历史、不再清其他记录。新历史写入或标记更新失败时不得只完成一半。
- 检查无关租户、其他批次及手动运行历史未被清零。旧历史丢失的错误详情和此前产生的重复is_final不自动回填。验收窗口至少覆盖一次失败→重试→新结果，按执行ID核对共库；持续结果提交失败应暂停新增重试，保留原执行凭证排查，不能重跑业务来掩盖落库失败。

2026-10-19 异步子任务删除：本轮只更新Scheduler和Console，不新增表/字段或环境配置，复用swe_cron_registration_journal。先部署Scheduler及其后台，再部署Console；前后端需要匹配，不能把旧前端的HTTP200同步完成判断用于新202响应。SWE无需因本轮异步删除单独更新；改造前行内包不包含本轮异步删除。

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
| swe_cron_execution_runs | 每次执行快照、同一执行恢复幂等、租约及主结果；每次平台请求建立独立执行。Scheduler 登记/派发/settled 收敛，SWE 写真实运行结果。 |
| swe_cron_managed_broadcast_tasks | 每轮异步分发、源快照与批入口同步计划；Scheduler 写。 |
| swe_cron_managed_broadcast_items | 固定接收人、子任务身份、散列和逐项结果；Scheduler 写。 |
| swe_cron_managed_dispatch_controls | 整组调度目标及配置版本；不代表每个对象都已切换成功。 |
| swe_cron_managed_mode_items | 三态逐对象同步进度，保留成功/失败，支持混合状态。 |
| swe_cron_managed_trigger_receipts | 每次平台请求独立保存触发事实和成员/跳过回执；不复用旧回执，callback_key 保留兼容旧列且新写入为空。 |

七张既有依赖表不是本次新建：swe_cron_jobs、swe_cron_executions、swe_cron_dispatch_scope_leases、swe_cron_dispatch_worker_capacity、swe_cron_dispatch_intents、swe_cron_dispatch_model_worker_policy、swe_cron_dispatch_worker_strategy。具体必需列由结构核对 SQL 列全。

智拓使用 Workflow 时还依赖既有的 swe_workflow_bindings 和 swe_workflow_binding_versions，分别保存技能绑定及不可变发布版本。因此人工核对材料共覆盖 18 张表（新后台启动必检 16 张，加 Workflow 的 2 张），不是本次新建 18 张。绑定表缺失时对照既有 [Workflow SQL](../../scripts/sql/cron_workflow_bindings.sql)，由数据库负责人核对其中每条语句后人工处理；本次不重复新建同义表、不自动迁移配置，既有字段长度约束仍须核对。

旧 swe_cron_jobs 继续保留新任务的兼容投影，不修改原 SWE/Monitor 写入逻辑；旧 swe_cron_executions 使用既有列和主键，不新增字段。原 swe_cron_broadcast_tasks/items 不作为新版队列，不让旧后台领取新版工作。

## 部署前置核对

- 网关把 `/api/scheduler/cron/jobs` 与新版 `/api/scheduler/cron/ordinary/callback`、`/batch/callback` 转发 Scheduler；旧入口仍指向原服务。Console 新页为 `/scheduler-cron-jobs`，原页 `/cron-jobs` 不变。
- `SCHEDULER_SWE_API_BASE_URL` 为 Scheduler 可达的 SWE/网关根地址；`SWE_SERVER_DOMAIN` 为平台可达且能路由新版回调的根地址。使用现有 `SWE_CRON_SCHEDULER_*` 平台配置，不新加必配内部 Token。连接参数不全时不能创建真实启用定时器验收。
- 沿用可信网关身份：X-User-Id 为真实操作者，X-Tenant-Id/X-Source-Id/X-Agent-Id 为目标任务。角色由网关核验，不手工伪造 manager/admin。三服务共库不代表工作区/会话/产物共盘，必须验证同一身份路由到可访问实际资源的 SWE 实例。
- 外部平台已确认只传注册参数解码后的固定 Body、不传 Header。新版回调也保留 jobParam 编码封装兼容。平台注册引用、完整任务身份及 scope 须匹配共库记录；平台入口沿用可信内网边界，数据库引用核验本身不是来源认证。平台未确认时区字段，注册 payload 沿用既有协议；本轮至少验证 Asia/Shanghai，不能据此承诺任意时区的平台触发正确。
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
5. 在可控预发布故障中令一个对象平台操作失败，验证 effective_state=mixed：已生效批对象走新批入口，未切换普通对象仍走普通入口。每次到达的新回调独立受理，不再以同逻辑期合并普通/批执行。失败提示保留，修复平台后人工 retry。
6. 切 batch_paused，只阻止新增批触发，已入队和执行中的工作继续；切 ordinary 先逐对象启用普通绑定，全部完成才关闭组批入口。普通新入口只用于新任务，不修改存量平台指向。

## 第三轮：实际执行和结果

先在人工确认的新平台绑定上触发，不能使用旧任务或随造 operation_id。推荐由平台实际回调验证；Postman 只对专用测试任务操作，每点一次发送都会新增执行或批次，不要用 Runner 自动重试生产回调。

两个入口分别为 POST `/api/scheduler/cron/ordinary/callback` 和 POST `/api/scheduler/cron/batch/callback`。不需要身份 Header，Body 使用平台的固定格式，替换为真实测试任务及其当前有效注册操作：

```json
{
  "tenant_id": "80280256",
  "source_id": "RMASSIST",
  "agent_id": "default",
  "job_id": "<测试任务ID>",
  "task_type": "job",
  "scopeId": "ODAyODAyNTY.Uk1BU1NJU1Q",
  "fromId": "80280256",
  "callback_protocol": "scheduler_managed_v1",
  "operation_id": "<当前有效注册操作ID>"
}
```

Header 若存在，不允许与 Body 的租户、来源或 Agent 冲突；管理角色不从 Body 读取。缺 external_job_id/jobId 时读取共库当前普通或批入口绑定，仍检查其存在及注册操作引用。缺触发时间时，以接收时间为物理触发时间，加已保存提前量形成逻辑时间，不反推 cron 或虚构平台计划时间。批入口的散列提前量只影响外部提前触发，批内成员统一受理时就绪，不再按成员偏移等待。可选明确时间仍按原排程规则校验；即使同一流水号或时间重复提交，也不会合并成旧执行。

1. 一次普通/批触发返回 202，检查 accepted=true，保存 execution_id 或 batch_id。对同一测试 Body 再发送一次，两个 ID 应不同，reused=false，分别新增回执及执行/成员。新批入口各成员的not_before_at应相同且是受理时刻，入队后只受优先级、容量及实际运行门禁约束，不因散列再次等到各自时间；logical_fire_at/原通知时间仍保留。202 且 accepted=false/status=skipped 表示实际门禁阻止执行，须查看 reason，不是执行已开始。

2026-10-19时间/批内就绪修复：部署Scheduler和Console，无新增SQL、配置或Monitor服务端修改。只影响新受理批次；历史已入队快照的not_before_at不改写。新版编辑读到详情后，列表同步相同详情定义；低于列表定义版本的详情提示刷新并禁止保存。验证时打开非默认10:00任务，表单与列表应一致，原样保存不改变Cron；如已有其他操作更新任务，以最新详情为准，不用固定09:00掩盖差异。批看板顶部为任务数（含父任务）、成员为子任务状态/完成时间，原长解释句已删除。
2. 单任务 POST `/{job_id}/run` 另用手动请求号，GET `/{job_id}/executions/{execution_id}` 观察 queued→accepted→running→终态；停用/运行中的任务拒绝新手动请求。手动执行与自动逻辑期独立。
3. SWE 直接写共库状态和历史，不等待最终回传 Scheduler。HTTP 成功不等于业务成功，HTTP 超时也不证明未受理；按相同执行 ID 查询，不重新生成执行。
4. 主 status=success、settled_status=null 时继续等待已有异步子任务；最终 settled_status=success/error 反映整轮，不覆盖 SWE 主状态。不因异步未确认提前释放模型池或删除运行资源。
5. 自动或手动成功结果未读增加；打开新任务 Chat/报告按新接口标读。新任务侧栏展示最新实际运行时间和状态，已有手动运行记录不提示尚未启动。已读不恢复，历史手动已读记录不重置；手动结果不直接触发自动未读暂停。恢复仅清已有边界，之后完成结果仍未读。新旧任务 Chat 列表旧在前、新在后，动作不串源。
6. 重启验证只恢复已受理工作与结果提交，不重跑已发生业务，不补造漏收到的平台周期。失败、超时、取消和结果不明分别保留安全错误码。

## 第四轮：删除、新旧隔离和异常

### 异步选中/全部删除的 Postman 验证

所有请求沿用源任务的X-User-Id/X-Tenant-Id/X-Source-Id/X-Agent-Id Header。只使用预发布测试父任务和子任务，以下操作会真实删除指定任务。

1. 查询全部游标页子任务，固定job_id、tenant_id、agent_id和expected_definition_version；保存确认时的未删除快照。选中删除用 `POST /api/scheduler/cron/jobs/{parent}/broadcast/children/batch-delete`（最多1000项），全部快照用同目录 `POST /delete-all`（可超过1000项）；Body为 `{"request_id":"delete-uat-001","items":[{"job_id":"child-id","tenant_id":"receiver","agent_id":"default","expected_definition_version":1}]}`。期望202和operation_id，不把受理当作完成。
2. 用返回的整批operation_id查询 `GET /api/scheduler/cron/jobs/{parent}/broadcast/children/batch-delete/tasks/{operation_id}`，观察operation_status为pending/running到completed，并检查逐项结果。`GET .../tasks/current`应查回相同操作；没有历史返回 `{"task":null}`。GET不触发删除、平台注册/停用或SWE清理。
3. 相同request_id及Body再次提交，应复用operation_id；修改清单、模式或版本使用同一个request_id应冲突。整批业务结束仍可能有pending清理项或failed项，检查logical_delete_applied及计数，不仅看operation_status。子任务版本冲突或不属于父任务时仅该项失败，不扩大删除范围。
4. Console点击“删除全部子任务”确认后关闭弹窗，再次打开能查回进度；全量提交不受筛选或分页影响，确认后新增任务不在本次快照。后台处理中禁止重复提交；结束时可结束本次操作。取消确认不发送POST。
5. 测试在途执行：已开始的继续完成；逻辑删除可先成立，整批处理完后pending_count仍可大于0；原后台清理完成后GET应看到最终完成。单项平台/SWE清理失败须在该子任务操作进度里人工retry，重复GET不应增加外部调用。
6. 测试实例重启/请求断开：已受理清单在租约到期后恢复，成功项不重复清理。整批意外失败时保留安全error_code，用 `POST .../tasks/{operation_id}/retry` 重新排队原清单，或原Body原request_id重提；不自动重试单项业务失败。后台未就绪不受理，返回503。

部署后由预发布验收人观察至少一次完整删除及一次重开页面过程，按operation_id检索“开始处理子任务删除清单”“批量删除处理结束”和“新链路恢复巡检暂未完成 lane=_deletions/deletions”。健康信号为数量推进、同ID重提无重复清理；持续无进度超过外部超时和120秒租约窗口、不断失败或结果缺失时，停止新增删除，保留已受理记录排查。回退前先停新增入口并确认待处理清单，不能认为回退会撤销已经逻辑删除的任务。

- DELETE 新任务先逻辑删除，含删除查询仍能看到记录及原清理操作；未开始工作跳过，在途继续，整轮结束才清资源。源删除不自动删除其全部子任务；批量删除显式选择并逐项检查结果。
- 用源管理者真实 Header 管理关联子任务，只改变目标 workspace，不把操作人换成接收人；接收人不能枚举或删除其他接收人的任务。
- 更新期间平台已引用新操作但管理日志未完成：本次合法触发应持久 skipped，原跳过记录不补跑；更新完成后新的回调请求独立判断、可正常受理。分发批入口更新响应丢失时，持久计划和已知绑定可继续有效触发，查询仍提示同步失败。
- 新接口 400/404/409/503 不回退旧写接口；模拟单来源列表失败，另一来源可更新，失败来源已知任务不被当删除。旧 SWE CRUD、旧批入口、原任务页面各做一条正向回归。
- 验证新任务没有 jobs.json/旧 _states 条目仍可查询、执行和标读。会话/报告文件仍可正常产生，这是执行产物，不等于重新启用任务文件存储。

## 上线监测、判定与回退

负责人由部署方指定；建议在专用用户完成一次完整自动计划期及其异步整轮后，再扩大接收人。验证窗口覆盖触发、排队、运行、结果、通知和重启，不只检查接口 200。

日志按 job_id、operation_id、task_id、receipt_id、execution_id 关联，检查“新版定时后台已启动”“平台触发已持久处理”“新定时任务结果提交结束”“整轮”相关安全日志。不要要求打印凭据、任务正文或原始敏感异常。

健康信号：每次有效平台回调有独立持久回执及执行，重试同一已入队 execution_id 不重新运行业务；三态失败计数可查且有效对象仍触发；模型池不超限，已结束整轮释放占位；无新版记录被旧处理器领取。异常信号：后台异常退出、持续 dispatch_failed、主成功长期不收敛、错误关联历史、清理长期等待或跨身份资源不一致。

出现同一 execution_id 重复运行业务、越权、错误绑定、丢结果或共库事务错误，应停止扩大范围，暂停确认的新批入口并停用相关新普通任务，保留全部表和操作记录。不同平台请求产生不同执行是本轮明确行为，不按错误重复执行处理。不要清表、删日志或用旧接口接管新任务；不能直接回滚到不识别新执行协议的版本而期望其处理已有新队列。先让在途结果和待清理事实收敛，再由负责人确定应用版本回退与恢复步骤，旧任务入口保持原样。

当前未完成目标业务MySQL、网关、外部平台及多实例资源路由验收。已另用独立本地MySQL 8.1执行结构及事务检查，详见[本地MySQL记录](cron_phase2_mysql_local_verification.md)；这不替代目标版本及存量数据核对。注册查回协议不再是前置条件，改按用户确认的失败/人工重试规则，但重复定时器风险仍需明确。

## 开发环境可重复检查

`tests/integrated/scheduler/test_managed_cron_http_chain.py` 在本机使用真实路由、HTTP客户端、资源准备器和共库SQL串联创建/更新/执行/删除、分发/三态及技能绑定→新Workflow创建。数据库为 SQLite 测试适配，平台、工作区资源、业务引擎及 Monitor 终态是明确边界替身，不代表实际服务验收。

真实 MySQL 测试为 `tests/integrated/scheduler/test_managed_cron_mysql.py`。仅在显式提供 `CRON_MANAGED_TEST_MYSQL_JSON`，且 database 符合 `test_cron_managed_...` 隔离库名时连接；不读取应用默认连接、不执行DDL。测试写入随机命名空间并只清理该命名空间，覆盖两表事务回滚、并发请求幂等与跨SWE/Scheduler连接的受理和结果单次写入。需先人工准备完整测试表及既有依赖；不要使用生产或预发布业务库运行该自动化测试。此变量只用于开发测试，不是应用新增配置。

历史版本在独立本地MySQL 8.1上显式配置后，10项检查均通过，含6项实际数据库场景；这不是 2026-10-19 新回调语义的 MySQL 实测证明。无配置运行仍会跳过这6项，不应计为数据库通过。行内仍以本文Postman流程为验收入口，不要求调用方运行Python；测试代码为开发侧提供可复现的验证方式。
