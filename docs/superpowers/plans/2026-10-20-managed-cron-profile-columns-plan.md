---
title: 新版定时任务增加四个冗余查询列
status: completed
date: 2026-10-20
---

# 范围与约定

按本轮确认，在 `swe_cron_jobs_v2` 增加 `tenant_name`、`bbk_id`、
`skill_ids`、`workflow_binding_id`，同时保留 `spec_json` 原定义。
完整定义仍为执行和公开接口的来源；独立列是随定义同事务更新的查询副本。
前两列和技能空值用空字符串，Workflow 未绑定用 NULL。
列宽分别为 255、128、200、128，中文列注释齐全。
不修改旧表写入、接口返回、状态表、订阅能力或其他暂停中的功能。
`pause_reason` 继续位于状态表；`subscription_key` 留待后续迁移。
不提交、不推送，不连接或修改行内数据库。

# 实施单元

## U1 定义写入与迁移导入（测试先行）

- 创建草稿、SWE 准备结果保存、最终发布和更新均从对应 spec 提取四列。
- 分发子任务复用管理写入，使用接收人的 profile 和准备后的 Workflow 绑定。
- 存量文件导入生成四列，并纳入仓库允许字段及 schema 校验。
- 文件：`scheduler/src/scheduler/app/services/cron/managed/definition.py`、
  `operations.py`、`schema.py`；`src/swe/app/crons/migration_source.py`、
  `migration_repository.py`。
- 测试：`tests/unit/scheduler/test_managed_cron_profile_columns.py`、
  `tests/unit/app/test_cron_migration_source.py` 及既有管理/分发/迁移测试。
- 场景：创建/修改四列与 JSON 一致、准备生成绑定后同步、更新失败不提前覆盖、
  缺失字段默认值、分发接收人身份、导入及重复导入。

## U2 人工 SQL 与回填

- 更新全量建表、只读字段审计，另提供四列专用人工升级及回填 SQL。
- 回填以本行 spec_json 为准，不读取旧表，不改变任务版本/归属/排程或运行状态。
- 非法 JSON、非对象、非字符串或超长字段先提示，不截断、不批量覆盖为伪造值。
- 文件：`docs/deployment/cron_phase2_manual_create.sql`、
  `cron_phase2_schema_audit.sql`、新增四列升级和回填材料。
- 测试：静态契约检查及独立本机临时 MySQL 上执行 DDL、回填两次、核对异常行保护。

# 验证与交付

先运行新增断言确认失败，再实施最小写入修改，运行管理/分发/迁移相关回归；
使用项目虚拟环境，必要时在 WSL 运行依赖 Linux 的 SWE 测试。
检查 Python 格式、复杂度及 diff；不声称完成行内联调或独立代理审查。
本仓库要求主线程顺序执行，因此由主线程进行明确标注的自查。
Superpowers 专用工具在当前会话未提供，采用本仓库 pytest 与文档计划完成测试先行验证。
