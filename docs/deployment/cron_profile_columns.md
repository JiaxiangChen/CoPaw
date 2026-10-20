# 新版定时任务四列升级

仅增加 `tenant_name`（用户姓名）、`bbk_id`（所属机构）、`skill_ids`（技能标识）和
`workflow_binding_id`（工作流绑定）四个查询副本。完整定义继续保留在 `spec_json`，
执行与接口读取来源不变，写入时从同一份定义提取四列并在原事务内保存。
空姓名、机构和技能使用空字符串，无工作流绑定使用 NULL。

## 部署顺序

1. 备份并核对现有表。新环境使用更新后的 `cron_phase2_manual_create.sql`。
   已有环境使用 `cron_profile_columns_manual_upgrade.sql`，按缺失列逐条执行。
2. 使用更新后的 `cron_phase2_schema_audit.sql`，确认四列均存在。
3. 部署 Scheduler 与 SWE。新代码要求四列存在，不会自行加列。
   创建草稿、SWE 准备后补全、最终创建/修改、分发子任务及存量导入都会维护四列。
4. 已有 v2 行使用 `cron_profile_columns_manual_backfill.sql`。
   在同一 SQL 连接按段执行，先审阅异常身份和数量，再执行 UPDATE。
   文件中的临时表不是业务新表，异常值不会截断，定义并发变化的行跳过后可重跑。

回填只更新四列，不修改 `spec_json`、任务版本、归属、排程、启停或执行状态。
数据量较大时由 DBA 按完整租户/来源/Agent/任务身份范围分批执行。
非法 JSON、非字符串及超长值需要人工修正；数组形式的旧技能列表需先规范为字符串。

## 验证入口

通过新版页面或 Scheduler 创建/修改任务，使用详情接口取得 `spec`，
再由数据库负责人按完整身份核对四列与 `spec` 对应字段。
Workflow 重点检查 SWE 准备阶段生成的绑定是否同步，分发重点检查接收人姓名及机构。
没有绑定的任务应为 NULL，技能清空后对应列应清空，而不是保留旧值。
回填可重复执行；旧表写入逻辑不变，暂停原因仍在 `swe_cron_job_states.pause_reason`。
订阅相关字段与能力本次不迁移。
