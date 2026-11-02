# 定时任务有效执行记录统计详细设计

## 目标与范围

运营看板 `/analytics/business-overview` 和定时任务统计页 `/analytics/cron-job-overview` 的执行表查询统一限定 `is_final=1`。用户排行成功数同时满足 `status='success' AND async_status='success'`。包含页面下钻、失败分页、导出及单条执行详情；不修改 Scheduler 写入、通知、调度去重、订阅管理和独立任务类型报表。共享执行列表接口的其他调用者同步采用有效记录口径。

## 字段契约与历史兼容

沿用 `scripts/sql/cron_execution_is_final.sql`：`TINYINT(1) NOT NULL DEFAULT 1`。字段表示当前有效记录，不表示完成或成功。普通执行全部为 1；批调度后续结果落库时，事务内将最新旧记录置 0。历史多条 1 不清洗，不添加唯一约束，不批量重置标记。重试仅入队时旧记录仍有效。跨日替换会改变过去日期范围的统计，这符合当前有效记录口径。

## 查询变更清单

### TracingQueryService

- `_get_growth_stats`：当前及上一周期共用的 cron_count 加过滤。
- `_get_branch_breakdown`：全部来源及单来源分行执行数量加过滤。
- `_build_daily_trend_read_tasks_query`、`get_hourly_trend`：已读趋势加过滤。
- `_build_cron_subquery`：用户排行共用范围加过滤；`_build_users_query` 两套 SQL 成功 CASE 同时判断异步状态。
- `_get_users_cron_reads`：共享 WHERE 同时约束 count 与分页数据。
- `get_task_status_summary`：全部来源及单来源分组查询加过滤，保留原状态分类。

### Cron QueryService

- `list_executions`：共享 conditions 保证分页 total 与 items 同口径；保留失败状态和权限过滤。
- `get_execution`：非有效 ID 返回现有 404，不扩展 API 字段。
- `get_executions_for_export`：执行历史导出与列表采用相同有效记录口径。
- `get_skill_usage_details_for_export`：执行明细导出加过滤；分行导出复用排行查询。
- `_fetch_overview_execution_counts`、`_fetch_overview_read_tasks`：概览执行与已读统计。
- `_fetch_branch_behavior_ids`、`_fetch_branch_skill_behavior_ids`：参与分行集合。
- `_fetch_branch_skill_total_tasks`、`_fetch_branch_skill_job_ids`：技能视角任务集合。
- `_fetch_branch_execution_stats`：任务及技能视角共用执行聚合。
- `_fetch_branch_skill_count`、`_fetch_branch_recommended_customers`、`_fetch_branch_skill_recommended_customers`：技能和推荐客户集合。
- `_fetch_branch_skill_involved_managers`、`_fetch_branch_result_view_managers`、`_fetch_branch_skill_result_view_managers`：参与和查看客户经理集合。
- `get_branch_error` 两条 SQL、`_fetch_cron_error_reasons`：异常范围、排行和原因。
- `_fetch_branch_skills_raw_data`：在 SQL 中过滤后沿用 Python 聚合。
- `_fetch_manager_base_info`、`_fetch_manager_skill_count`、`_fetch_manager_recommended_customers`：客户经理汇总。
- `get_branch_skill_managers`、`_fetch_manager_skill_stats`：技能和客户经理下钻。

## 实现约束

每个执行表读取在所属 SQL WHERE 中加 `e.is_final = 1`，无别名查询使用 `is_final = 1`。动态 conditions 在共用构造入口添加。保留现有时间边界、source、分行、金葵花名单、统计技能、排序及参数顺序。不把过滤放在总聚合之外，不使用全局数据库 SQL 重写器。查询点击表、任务表的独立指标保持原定义；可能继续保留被替代轮次产生的点击。

## 索引决策

本次不自动增加或替换索引，不执行 DDL。历史和普通记录全部为 1，单列 is_final 不能据此证明有收益。现有 actual_time、job_id、tenant_id+actual_time 及 dispatch 组合索引继续保留。上线前核对 SHOW INDEX 和标记分布，以真实查询计划比较 `(job_id,is_final,actual_time)`、`(is_final,actual_time)`；读趋势确有瓶颈时再评估 `(is_final,read_at)`。记录扫描行数、耗时、排序、任务先驱动与执行先驱动计划及索引维护成本，再交付单独手工迁移。无真实执行计划时不宣称优化收益。

## 部署与回滚

已有库、新建库均先执行已有手工加列脚本，再发布查询代码。字段迁移不由 schema 初始化器补做。确认 Scheduler 全部实例采用有效标记写入，避免旧实例或绕过 Scheduler 的批调度回报。查询代码可独立回滚，不回滚有效标记、不删除数据。

## 验证

用真实 SQLite 查询验证 final=0 被排除、final=1 成功/异步失败/异步未完成分类、用户排行两个来源分支、分页总数与行集、失败筛选、执行历史和技能明细导出及概览总数。新增 fixture 验证参数数量匹配；分行名单 fixture 对实际执行的执行表查询统一断言有效标记条件存在。现有 SQLite fixture 增加默认 1 字段，并通过显式列名插入保留原数据。源码清单核对其他目标 SQL 与共享条件入口。运行相关 monitor 回归和 Scheduler 标记写入测试；不运行需真实数据库且有建删表行为的集成测试。

### 本次验证结果

- 新增实际 SQL 行为用例 12 项通过；相关独立回归组合 105 项通过（包含趋势、分行名单/技能绑定、分行导出及 Scheduler 标记测试）。
- 扩展到 overview 与 BBK 历史测试时，新增概览用例前结果为 134 通过、7 失败。已在独立 pytest 进程中加载 HEAD 原版两个查询服务重新运行对应两份历史测试，复现相同 7 失败（30 通过），未改写工作区生产文件。
- 既有失败包括已读率分母断言、方案点击事件定义、已读执行成功条件、BBK SQL 别名字符串及内部技能排除项数量；不在本次范围内，不为通过测试而调整这些口径。
- 两条趋势测试的 SQL 字符串断言已适配新 WHERE，并明确断言 `is_final=1`。`git diff --check` 通过。GitNexus detect-changes 返回 LOW；由于索引落后当前代码，该结果不替代源码核对。
- 未连接生产数据库、未验证真实 MySQL 执行计划、未执行加列或建索引 DDL、未提交或推送。

## 风险与边界

统计、排行和分页变化是预期行为；运行中不会因字段名而被排除。只有已读旧轮次而最新轮次未读时，已读数会下降。单条旧执行 ID 查询 404 是有效记录接口契约。GitNexus 索引落后当前提交，影响图辅助使用，修改前逐符号运行 upstream impact 并结合源码调用核对。工作区已有用户改动不纳入本次提交。
