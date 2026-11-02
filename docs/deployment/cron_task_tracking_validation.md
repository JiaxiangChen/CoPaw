# 新版定时任务追踪：部署与验证

## 部署范围

- 更新 Scheduler 后端和 Console 前端；Monitor 后端不再承载此功能，不改变 SWE/Monitor/ Scheduler 现有状态写入逻辑。
- 无新增表、列、数据库写入、环境变量或后台任务；沿用 Scheduler 已配置的共用数据库。
- 入口：`/monitor/cron-task-tracking`，导航“定时任务追踪”。仅当前授权来源的 manager/admin 可访问。
- 生产权限沿用既有可信身份 Header。网关必须校验并生成/覆盖用户、来源和角色，不能将客户端可任意伪造的 Header 当成授权证明。
- `tests/manual/cron_task_tracking_app.py` 仅用于隔离的本地验收，使用内存 SQLite；不要把该测试应用当成业务 Scheduler 启动入口。
- 查询不依赖调度后台 ready 标记；后台未就绪时，只要共库仍可读，就能查询已有事实。Scheduler HTTP 进程或数据库整体不可用时仍会失败。
- 前端页面路径 `/monitor/cron-task-tracking` 和菜单不变；这只是界面组织，三个后端接口全部走 Scheduler。未发布的 Monitor 追踪路径不保留双份实现或转发。

## 读取的数据

核心来源为 `swe_cron_jobs_v2`、`swe_cron_job_states`、`swe_cron_registration_journal`、`swe_cron_managed_trigger_receipts`、`swe_cron_execution_runs`、`swe_cron_managed_dispatch_events`、`swe_cron_executions`。

补充来源为 `swe_cron_managed_batch_members`、`swe_cron_managed_broadcast_tasks`、`swe_cron_managed_broadcast_items`、`swe_cron_managed_mode_items`、`swe_cron_failure_retry_calls`。不从旧定义表或 jobs.json 回退读取。

表、列或关联缺失会显示局部不可用，不生成无证据的状态节点。若连任务身份都无法验证，返回存储不可用，而不是假报“未找到”。每条 SQL 最多等待 5 秒，一次聚合查询的数据库等待共享 15 秒预算；每个来源按 100 行 keyset 页读取，输出默认 50 条、最大 100 条。

## Postman 验证

准备环境变量：`base_url`（不含 `/api`）、`user_id`、`source_id`、`job_id`、`tenant_id`、`agent_id`、`start_time`、`end_time`。请选择一个 Scheduler 管理的真实新版任务。

三个接口统一使用 Header：

| Header      | 示例值          |
| ----------- | --------------- |
| X-User-Id   | `{{user_id}}`   |
| X-Source-Id | `{{source_id}}` |
| X-User-Role | `manager`       |

正式环境应通过现有认证网关取得这些身份，不增加新 Token 配置。

### 1. 定位任务

GET `{{base_url}}/api/scheduler/cron/task-tracking/jobs/{{job_id}}/lookup`

检查 candidates 中完整的用户、来源、Agent、任务 ID。多个候选必须明确选择；has_more=true 时用可选的 tenant_id/agent_id 缩小查询，不把首项默认当成目标。

### 2. 查询当前概况

GET `{{base_url}}/api/scheduler/cron/task-tracking/jobs/{{job_id}}/overview`

Params：tenant_id=`{{tenant_id}}`，agent_id=`{{agent_id}}`。

核对名称、任务类型、当前 Cron/时区、启停、调度模式、版本、会话和定时器绑定、最近状态、未读数。父子关系仅作标识。时间范围不会改变此处的当前快照。

### 3. 查询时间轴

GET `{{base_url}}/api/scheduler/cron/task-tracking/jobs/{{job_id}}/timeline`

| Param      | 值                                                                                          |
| ---------- | ------------------------------------------------------------------------------------------- |
| tenant_id  | `{{tenant_id}}`                                                                             |
| agent_id   | `{{agent_id}}`                                                                              |
| start_time | 例如 `2026-10-27T00:00:00+08:00`                                                            |
| end_time   | 例如 `2026-10-28T00:00:00+08:00`                                                            |
| category   | all / management / scheduling / execution / notification                                    |
| entry      | all / ordinary / batch / manual / manual_retry / daily_failure_retry / unknown / management |
| order      | asc / desc                                                                                  |
| limit      | 50；验证翻页可改为 1                                                                        |

在 Postman Params 面板填写带 `+08:00` 的时间，让 Postman 正确编码加号。区间为左闭右开，一次最多 31 天。has_more=true 时，将 next_cursor 原样放入下一次请求的 cursor，并保持任务身份、时间范围、分类、入口和排序不变。loaded_count 只表示本页数量，不是全范围总数。

重点验证：

1. 管理操作使用提交时间，并明确展示当前记录结果；不虚构中间失败/重试时刻。
2. 主执行成功与整轮结果分开。整轮节点为“Scheduler 已确认整轮结果”，不是异步实际结束时间。
3. 前一天的执行在今天通知或标读，筛选今天仍可查到。
4. 重试前 is_final=0 的失败历史不被隐藏，运输重投不变成新的业务尝试。
5. 子任务只展示自己的参与证明和结果，不能混入整批人数、名单、其他成员或父任务执行状态。
6. 通知失败无发生时间时，仅展示累计失败次数和最近错误；不制造失败时间点。
7. 多次 GET 和展开详情前后，对照原任务页或结果查询接口，未读数、is_read/read_at 不因追踪发生变化。
8. 非管理员返回 403；缺少身份头返回 400；query 中的 source_id/role 不能覆盖可信 Header；跨来源查不到其他任务。

## 页面验收

验证查询、多个身份候选、刷新、正倒序、时间和类别/入口筛选、上一页/下一页、长错误/ID 的换行、展开详情、加载失败重试。快速切换任务/筛选时不能出现上一次任务的迟到结果；查不到不能残留旧详情。分别验证普通嵌入和 hideMenu=true。

## Post-Deploy Monitoring & Validation

- 验证责任：本次发布负责人；预发布先验证一条普通任务、一条批父任务、一条批子任务及一条失败/重试记录。
- 窗口：预发布完整走一轮上述步骤；生产发布后首个完整调度周期重点观察。
- 日志检索：`Cron tracking source unavailable`、`Cron tracking query failed`、`Cron tracking database unavailable`，结合 query_id、table 和 error_type 定位；不在日志或工单粘贴原始凭据/任务全文。
- 健康信号：GET 200，正常来源 coverage 非 unavailable；时间/入口可对应已有管理、执行和通知记录；查询不改变业务状态。
- 失败信号：持续 503、query_budget_exhausted、重复出现某表 unavailable、历史关联未确认或翻页出现重漏。先核对发布版本、手工建表范围和来源 Header，再检查只读 SQL 的执行计划。
- 在行内 MySQL 上用真实数据量核验索引和响应时间。现有事件索引并不保证按任务时间筛选的所有查询都高效；本次不自动加索引。若预算持续耗尽，由 DBA 评估对应任务/时间及关联字段的索引，不通过缩短历史、改写旧表或静默丢记录掩盖问题。
- 缓解/回滚：暂不开放该新入口，或回滚本次 Scheduler/Console 版本；由于没有业务写入和 DDL，不需要回滚任务定义或执行历史。

## 本地验证边界

初版 Monitor 承载时的历史验证记录（迁移后的验证单列于后）：

| 检查                                         | 结果                                                                                                                                                   |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 新增后端追踪用例                             | 90 通过，执行真实 SQLite SQL 和 FastAPI 路由                                                                                                           |
| 新前端及导航/原批看板相关回归                | 25 通过；其中新追踪 API/页面用例 11 个                                                                                                                 |
| Monitor 整体单元回归                         | 182 通过、11 个基线既有失败；失败项已在未修改基线快照复现                                                                                              |
| TypeScript、定向 ESLint、Black/Prettier 格式 | 通过                                                                                                                                                   |
| 新 Python 业务函数复杂度                     | 本地 guard 校验不超过 15                                                                                                                               |
| Console 生产构建                             | 通过；仍有项目的大包、静态/动态混合导入警告                                                                                                            |
| Chrome 跨层验收                              | 真实前端 + 真实 Monitor 接口 + 隔离 SQLite；4 种视口、普通导航和 hideMenu、键盘定位、查询/筛选/展开/清空旧任务通过，追踪请求全部 GET，无未捕获页面异常 |

浏览器视口为 1280×720、1440×900、1920×1080 和 768×900。验收数据是测试夹具，不是行内业务数据；组件开发告警与 jsdom 的伪元素提示未被当成业务失败，也未静默修改共享组件来消除它们。

按仓库约定由主线程顺序完成代码自检和修正，没有派发独立评审代理；GitNexus 未收录此 worktree，索引调用失败后以源码、真实查询和回归为依据，不宣称已完成调用图影响验证。代码尚未提交或推送。

本地使用真实 SQL 的 SQLite 夹具、真实 FastAPI 路由、前端单元测试及 Chrome 浏览器验证，不代表已经验证行内 MySQL 的数据完整性和执行计划。Chromium 108/360 浏览器基线仍需部署方验证。

### 后端迁入 Scheduler 后

查询服务和参数模型整体移到 Scheduler；原 90 个后端用例同步归入 `tests/unit/scheduler/test_cron_task_tracking_*.py`，并新增 4 个服务边界用例。迁移后首轮 94 个后端用例、11 个前端用例通过：真实 Scheduler 路由注册有效、Monitor 旧入口 404、调度后台未就绪仍可查库、额外展示 Header 不干扰查询身份、阻断 Monitor/SWE 包仍能导入 Scheduler 追踪功能。

本轮扩大验证：Scheduler 整体单元测试 725 个通过；前端追踪、导航与原批看板相关回归 25 个通过。TypeScript、定向 ESLint、本地 Python 函数复杂度（不超过 15）和 Console 生产构建通过。浏览器在 1440×900、1280×720、普通菜单及 hideMenu 模式下验证了查询、分类、展开、查不到后清空旧内容；实际追踪请求全部为 `/api/scheduler/cron/task-tracking/...` 的 GET，页面 URL 保持不变。截图为 `output/playwright/cron-tracking-scheduler-1440.png`、`output/playwright/cron-tracking-scheduler-1280.png`，使用隔离 SQLite 数据，不是行内业务数据。

前端页面仍在 `console/src/pages/Monitor/CronTaskTracking/`；目录名和菜单位置不决定后端服务。SWE 的执行落库、Monitor 的异步汇总及通知处理保持原职责，没有跟随只读查询迁移。

扩大 Monitor 回归时，发现旧概览统计、会话查询、子任务结果和技能统计共 11 个既有失败；已在未修改的 `d750141314fafdae9a92cd0975672a1309e42a76` 快照上复现相同失败。本次不修改这些无关逻辑。
