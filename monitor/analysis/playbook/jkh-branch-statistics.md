# 金葵花客户经理统计改造开发文档

更新日期：2026-09-09。覆盖名单过滤及后续按名单分行缩小查询范围的最终实现。

## 1. 阅读入口与适用范围

后续需求涉及以下任一事项时，先阅读本文：分行技能排行、分行客户经理汇总、金葵花名单日期选择、名单分行过滤、上述统计与下钻数据对账、名单过滤性能优化。

本文维护这次改造的业务规则、设计原因和影响边界。具体实现以当前代码为准，定位时使用方法名，避免依赖易变化的行号。业务术语见 [CONTEXT.md](../../CONTEXT.md)。

需求来源：本地需求文件 `D:/cmbwork/claw/prompt/金葵花客户经理改造20260908`，以及后续确认的 `bbk_id = jkh_user_inf.first_bbk_id` 分行约束。本文已整理完整规则，后续开发无需依赖该外部文件或聊天记录。

## 2. 实现目标与解决的问题

| 改造前的问题 | 实现目标 | 最终行为 |
| --- | --- | --- |
| 两个目标接口未按金葵花客户经理名单限定人群 | 统计对象限定在指定快照的名单内 | 执行记录和点击事件分别按实际用户字段过滤 |
| 截止日可能没有对应名单数据 | 明确可重复执行的名单日期选择规则 | 按当天、当月月末、全表最早或最新日期的优先级选择一次快照 |
| 各项指标分散在多个辅助查询中，容易漏加过滤 | 覆盖整个目标调用链 | 16 个辅助查询接入统一过滤构造方法 |
| 按分行查询时，名单子查询仍匹配全分行名单 | 利用已知分行进一步缩小名单范围 | 14 个具有单分行上下文的查询增加 `first_bbk_id` 条件 |
| 共享查询方法可能改变任务视角统计 | 保留既有调用方的统计行为 | 未传名单日期的调用继续使用原统计范围 |

目标接口是 `QueryService.get_branch_behavior` 和 `QueryService.get_branch_manager_summary`。现有日期范围、来源、业务分行、技能统计开关、响应字段和排序规则沿用原实现。本次没有修改前端、数据库表结构、名单同步流程或接口参数。

分行条件已实现，但尚未测量真实 TDSQL 的性能收益。

## 3. 数据关系与业务规则

### 3.1 名单与业务记录的对应关系

名单表为 `jkh_user_inf`，通过 monitor 现有数据库连接查询。

| 名单字段 | 含义 | 本次用途 |
| --- | --- | --- |
| `sync_date` | 名单数据日期，varchar | 选择和过滤名单快照 |
| `user_id` | 用户 ID，varchar | 关联执行人或点击人 |
| `first_bbk_id` | 分行号，varchar | 对应业务查询的 `bbk_id` |
| `first_bbk_nm` | 分行名称 | 未用于替换现有展示名称 |
| `org_id / org_nm` | 支行号、支行名称 | 未增加支行筛选 |
| `user_name` | 用户名称 | 未用于替换现有展示名称 |
| `pst_lvl` | 金葵花等级 L1/L2/L3 | 按名单成员资格过滤，未额外增加等级条件 |

匹配字段固定为：

- 执行记录：`swe_cron_executions.tenant_id = jkh_user_inf.user_id`。
- 点击记录：`swe_html_preview_click_events.user_id = jkh_user_inf.user_id`。
- 有单分行上下文时：`jkh_user_inf.first_bbk_id = bbk_id`。

执行人、点击人和任务所属用户不是可随意互换的过滤字段。即使任务已被名单过滤选中，后续执行统计仍需过滤执行人，以覆盖同一任务出现不同执行人的情况。既有按任务所属用户分组及名称展示的逻辑保持原实现，本次仅调整查询范围。

### 3.2 名单日期选择

`_resolve_jkh_sync_date(db, end_time)` 使用解析后的统计截止时间，比较时取日期部分：

| 优先级 | 条件 | 返回值 |
| --- | --- | --- |
| 1 | 存在截止日当天的名单 | 截止日 |
| 2 | 当天不存在，但存在当月最后一天的名单 | 当月最后一天 |
| 3 | 前两项不存在，且全表最新日期晚于截止日 | 全表最早日期 |
| 4 | 前两项不存在，且全表最新日期早于截止日 | 全表最新日期 |
| 5 | 其余情况 | 全表最新日期 |

实现通过一次聚合查询取得当天、月末、最早和最新日期，再按上述优先级返回。月末按日历计算，覆盖闰年二月及跨年月份。

例：名单有 `2026-01-31`、`2026-05-31`、`2026-08-31`，统计截止日为 `2026-06-15`，结果是 **2026-01-31**。这是已确认的业务规则，调整为“最近的历史日期”属于业务变更。

当前实现依赖 `sync_date` 统一采用 **YYYY-MM-DD** 格式，日期最值与比较使用字符串顺序。该格式是实现约定，尚未连接真实 TDSQL 核实。若后续数据采用 YYYYMMDD 或混合格式，应同步调整解析、比较及测试。

快照选择基于全表，不按当前分行、来源或用户名单先行筛选。两个入口在每次请求内各选择一次日期，并显式传递给其全部指标查询。选定日期使用局部变量，避免并发请求共享可变日期状态；这不等于对所有查询建立数据库事务快照。

### 3.3 空名单与兼容参数

当日期选择返回 `None` 时，两个目标入口直接返回原响应结构和 `items=[]`。

过滤 helper 中 `sync_date=None` 的含义则是“保留原查询口径”，用于兼容其他调用方。这两种情况通过入口提前返回区分。后续新增名单统计入口时，应先处理空名单，再调用辅助查询，避免空名单意外变成无限制查询。

### 3.4 分行范围

最终实现保留两种查询范围：

| 查询位置 | 名单条件 |
| --- | --- |
| 分行排行循环外的分行发现、接触统计批量查询 | 日期 + 用户 |
| 分行排行循环内的 9 个查询 | 日期 + 用户 + 单个分行 |
| 客户经理汇总的 5 个查询 | 日期 + 用户 + 单个分行 |

因此，业务记录分行与名单分行不一致时，单分行查询会排除该记录；循环外的分行发现和接触统计仍可能纳入该记录。由此可能出现分行仍在列表中而部分指标为零，或分行接触统计与客户经理汇总无法严格对账的情况。这是当前范围边界；后续要求统一口径时，需要明确是否同时调整两个循环外查询。

## 4. 实现结构与改动位置

### 4.1 文件清单

| 文件 | 改动或关联职责 |
| --- | --- |
| [query_service.py](../../src/monitor/app/services/cron/query_service.py) | 新增日期选择、过滤构造方法；修改 2 个入口和 16 个查询辅助方法 |
| [test_jkh_branch_queries.py](../../tests/test_jkh_branch_queries.py) | 新增名单规则、统计范围、分行约束和兼容性测试 |
| [CONTEXT.md](../../CONTEXT.md) | 记录业务术语 |
| 本文及 [Playbook 索引](README.md) | 保存开发规则、边界和后续定位入口 |
| [cron.py 路由](../../src/monitor/app/routers/cron.py) | 两个同名路由消费新的统计结果，路由代码未改动 |
| [cron.py 模型](../../src/monitor/app/models/cron.py) | 沿用现有响应模型，字段未改动 |

### 4.2 调用链

```text
get_branch_behavior
  -> _parse_date_range
  -> _resolve_jkh_sync_date
  -> 空名单提前返回
  -> 分行发现 + 分行接触统计（批量）
  -> 按 bbk_id 循环计算 9 项查询
  -> 组装并排序 CronBranchRankingResponse

get_branch_manager_summary
  -> _parse_date_range
  -> _resolve_jkh_sync_date
  -> 空名单提前返回
  -> asyncio.gather 并行计算 5 项查询
  -> _build_manager_summary_items
  -> BranchManagerSummaryResponse

上述 16 个查询
  -> _build_jkh_filter(user_column, sync_date, bbk_id=None)
  -> 将 SQL 片段与参数加入原查询
```

### 4.3 统一过滤构造

`_build_jkh_filter` 返回 SQL 片段与参数列表，单分行场景的逻辑形态为：

```sql
AND EXISTS (
    SELECT 1
    FROM jkh_user_inf jkh
    WHERE jkh.user_id = <内部固定用户列>
      AND jkh.sync_date = %s
      AND jkh.first_bbk_id = %s
)
```

参数顺序为 `[sync_date, bbk_id]`；未传分行时仅有 `[sync_date]`；未传日期时返回空片段和空参数列表。SQL 中用户列由内部调用点指定，数据值均使用参数绑定。

选择 `EXISTS` 是因为需求只判断成员资格，名单存在重复用户行时不会像普通 JOIN 那样放大统计。调用方负责将参数插入到与 SQL 占位符一致的位置，尤其注意来源筛选参数前后的顺序。

### 4.4 辅助方法与指标对应表

以下方法均位于 `QueryService`，全部接入名单日期和用户过滤。

| 辅助方法 | 查询职责 | 额外传入名单分行 |
| --- | --- | --- |
| `_fetch_branch_skill_behavior_ids` | 分行列表 | 否，循环外 |
| `_fetch_branch_contact_stats` | 分行接触客户数、接触率 | 否，循环外 |
| `_fetch_branch_skill_total_tasks` | 任务数 | 是 |
| `_fetch_branch_skill_job_ids` | 执行统计所需任务 ID | 是 |
| `_fetch_branch_execution_stats` | 执行、成功、已读、错误统计 | 是，由循环显式传递 |
| `_fetch_branch_skill_count` | 技能数 | 是 |
| `_fetch_branch_skill_involved_managers` | 涉及客户经理数 | 是 |
| `_fetch_branch_skill_result_view_managers` | 查看结果客户经理数 | 是 |
| `_fetch_branch_skill_manager_click_counts` | 方案、洞察、电访客户经理数 | 是 |
| `_fetch_branch_skill_customer_click_counts` | 方案、洞察、电访客户数 | 是 |
| `_fetch_branch_skill_recommended_customers` | 推荐客户数 | 是 |
| `_fetch_manager_base_info` | 客户经理基础信息、任务数、成功数、已读数 | 是 |
| `_fetch_manager_skill_count` | 客户经理技能数 | 是 |
| `_fetch_manager_recommended_customers` | 客户经理推荐客户数 | 是 |
| `_fetch_manager_click_stats` | 客户经理查看、洞察、电访客户数 | 是 |
| `_fetch_manager_contact_stats` | 客户经理接触客户数、接触率 | 是 |

## 5. 影响边界与性能限制

`_fetch_branch_execution_stats` 同时被 `get_branch_task_behavior` 调用，新增的 `sync_date`、`bbk_id` 为可选参数。任务视角不传名单日期，继续使用原统计范围。

`get_branch_skills`、`get_branch_skill_managers`、`get_branch_skill_manager_customers`、`get_manager_skills`、`get_manager_customers` 等下钻接口未接入本次名单规则，仍可能包含非金葵花客户经理。后续下钻对账需求应先确定是否扩展名单过滤范围。

开发时 GitNexus 对已索引目标方法的修改前分析均为 LOW；共享执行统计有 2 个直接调用方，其余辅助方法各有 1 个生产调用方。仓库级差异检查曾给出 HIGH，包含已有 Console 改动及相关执行流，不能作为本次修改的独立风险结论。新增 helper 当时未入索引，已以源码核对 16 个调用点。后续改动应重新分析当前调用图。

每个目标请求增加一次名单日期聚合查询，其余查询增加名单存在性判断。分行排行仍按分行循环执行查询，本次没有批量化这一链路。生产环境需用实际数据、索引和 EXPLAIN 评估：

- 单分行查询可评估 `(sync_date, first_bbk_id, user_id)` 索引。
- 无名单分行条件的查询仍需评估 `(sync_date, user_id)` 索引。
- 名单日期聚合与各指标查询分别评估，不能把加入分行条件等同于已完成性能验证。

本次没有创建索引、执行数据库迁移或验证真实 TDSQL 执行计划。

## 6. 测试与已验证结果

[名单测试](../../tests/test_jkh_branch_queries.py) 使用内存 SQLite 执行实际查询，仅适配 MySQL 占位符和 FIND_IN_SET。它验证逻辑与结果，不替代 TDSQL 方言、执行计划或性能测试。

| 验证目标 | 测试入口 |
| --- | --- |
| 日期优先级、全表最早回退、普通与闰年月末、空名单 | `test_snapshot_selection` |
| 分行与客户经理各项指标、来源过滤、重复名单 | `test_branch_behavior_filters_all_metrics`、`test_manager_summary_filters_all_metrics` |
| 空名单响应与任务视角兼容性 | `test_empty_roster_returns_empty_responses`、`test_task_behavior_remains_unrestricted` |
| 同任务不同执行人、点击人与任务归属不同 | `test_execution_stats_filters_executor_even_for_shared_job`、`test_clicks_filter_actor_independently_of_job_owner` |
| 非名单执行记录的分行排除 | `test_branches_with_only_non_roster_executions_are_excluded` |
| 14 个单分行查询的跨分行排除 | `test_branch_scoped_queries_exclude_other_roster_branches` |
| 循环给共享执行统计传入分行 | `test_branch_loop_passes_branch_to_execution_stats` |

在 monitor 目录使用项目虚拟环境执行：

```powershell
./venv/Scripts/python.exe -m pytest tests/test_jkh_branch_queries.py tests/test_cron_skill_binding_queries.py -q
```

Linux 将解释器路径替换为 `venv/bin/python`。

开发阶段验证记录：初版分批通过 85 项测试；增加名单分行条件后，相关名单及技能绑定回归通过 44 项。两者包含重复用例，不应相加为独立测试数量。初版扩展回归中曾有请求校验用例组合运行挂起，原始代码和当前代码单独运行相关 3 项均通过。Black、编译及差异检查通过；未运行全仓库 pre-commit。

本次文档整理未重新运行业务测试，以上为此前开发阶段的验证记录。

## 7. 后续需求开发指引

| 新需求类型 | 优先修改入口 | 完成判据 |
| --- | --- | --- |
| 调整快照日期策略或格式 | `_resolve_jkh_sync_date` | 日期矩阵测试符合新规则；两个入口仍在请求内共享同一选择结果 |
| 新增一个统计指标 | 对应入口及新查询 helper | 正确选用执行人或点击人字段；明确分行范围；名单外用户不进入结果 |
| 增加等级、支行等名单约束 | `_build_jkh_filter` 及调用链 | 明确条件是否影响快照选择；逐一验证全部受影响查询 |
| 让汇总与下钻口径一致 | 第 5 节列出的下钻方法 | 使用相同快照与人群规则，对同一组样本完成汇总和明细对账 |
| 统一所有分行指标的名单分行口径 | 两个循环外查询与单分行查询 | 覆盖业务分行与名单分行不一致的样本，验证分行列表和接触统计 |
| 优化响应时间 | 名单日期查询、EXISTS、分行循环 | 保持统计结果一致，并记录 TDSQL EXPLAIN 和同数据规模的前后耗时 |

定位数值异常时，依次核对实际选定的快照、名单用户、名单分行与业务分行、原有来源和技能条件。每次改变规则，同步更新本文对应章节和测试，保持一处规则说明，避免在新文档复制出相互冲突的口径。
